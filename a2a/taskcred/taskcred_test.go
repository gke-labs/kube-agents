package taskcred

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/jwt/v2"
	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nkeys"
)

const (
	testLifetime = time.Hour
	testSkew     = 30 * time.Second
)

func signingKey(t *testing.T) (nkeys.KeyPair, string) {
	t.Helper()
	kp, err := NewSigningKey()
	if err != nil {
		t.Fatalf("NewSigningKey: %v", err)
	}
	pub, err := kp.PublicKey()
	if err != nil {
		t.Fatalf("PublicKey: %v", err)
	}
	return kp, pub
}

func validClaims(now time.Time) Claims {
	return Claims{
		Task:     "task-1",
		Profile:  "auditor",
		Tier:     "read",
		Pod:      "auditor-read-x7k2p",
		PodUID:   "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0",
		Audience: AudienceNATS,
		Expires:  now.Add(10 * time.Minute),
	}
}

func mint(t *testing.T, kp nkeys.KeyPair, c Claims) string {
	t.Helper()
	tok, err := Mint(kp, c)
	if err != nil {
		t.Fatalf("Mint: %v", err)
	}
	return tok
}

func TestAMintedCredentialVerifiesAndCarriesItsClaims(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	want := validClaims(now)
	got, err := Verify(mint(t, kp, want), []string{pub}, AudienceNATS, now, testLifetime, testSkew)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if got.Task != want.Task || got.Profile != want.Profile || got.Tier != want.Tier ||
		got.Pod != want.Pod || got.PodUID != want.PodUID || got.Audience != want.Audience {
		t.Errorf("claims = %+v, want %+v", got, want)
	}
	if got.Expires.Unix() != want.Expires.Unix() {
		t.Errorf("exp = %v, want %v", got.Expires, want.Expires)
	}
	if got.Issuer != pub || got.ID == "" || got.IssuedAt.IsZero() || got.Target != nil {
		t.Errorf("iss/jti/iat/target = %q/%q/%v/%v", got.Issuer, got.ID, got.IssuedAt, got.Target)
	}
}

// Two credentials minted in the same second for the same claims still differ:
// jwt/v2 hashes the standard claims into the jti, and the subject is a nonce.
func TestEveryMintHasItsOwnJTI(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	c := validClaims(now)
	a, err := Verify(mint(t, kp, c), []string{pub}, AudienceNATS, now, testLifetime, testSkew)
	if err != nil {
		t.Fatal(err)
	}
	b, err := Verify(mint(t, kp, c), []string{pub}, AudienceNATS, now, testLifetime, testSkew)
	if err != nil {
		t.Fatal(err)
	}
	if a.ID == b.ID {
		t.Errorf("two mints share jti %q", a.ID)
	}
}

func TestACredentialFromAnUntrustedKeyIsRefused(t *testing.T) {
	kp, _ := signingKey(t)
	_, other := signingKey(t)
	now := time.Now()
	if _, err := Verify(mint(t, kp, validClaims(now)), []string{other}, AudienceNATS, now, testLifetime, testSkew); err == nil {
		t.Fatal("a credential signed by a key the verifier does not trust was accepted")
	}
}

// Rotation: the verifier trusts the old key and the new one at once.
func TestTheSecondTrustedKeyIsAccepted(t *testing.T) {
	_, oldPub := signingKey(t)
	kp, newPub := signingKey(t)
	now := time.Now()
	if _, err := Verify(mint(t, kp, validClaims(now)), []string{oldPub, newPub}, AudienceNATS, now, testLifetime, testSkew); err != nil {
		t.Fatalf("a credential from the second trusted key was refused: %v", err)
	}
}

func TestTheTrustedKeyListIsBounded(t *testing.T) {
	kp, pub := signingKey(t)
	_, b := signingKey(t)
	_, c := signingKey(t)
	now := time.Now()
	tok := mint(t, kp, validClaims(now))
	for name, trusted := range map[string][]string{
		"none":                {},
		"three":               {pub, b, c},
		"an account key":      {accountPub(t)},
		"garbage beside ours": {pub, "not-a-key"},
	} {
		if _, err := Verify(tok, trusted, AudienceNATS, now, testLifetime, testSkew); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func accountPub(t *testing.T) string {
	t.Helper()
	kp, err := nkeys.CreateAccount()
	if err != nil {
		t.Fatal(err)
	}
	pub, _ := kp.PublicKey()
	return pub
}

// A credential signed by an account key, whose public half is then trusted,
// is still refused: the issuer must be a task-signing key, not merely listed.
func TestOnlyATaskSigningKeyCanIssue(t *testing.T) {
	acct, err := nkeys.CreateAccount()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Mint(acct, validClaims(time.Now())); err == nil {
		t.Error("Mint signed with an account key")
	}
	seed, _ := acct.Seed()
	if _, err := ParseSigningKey(seed); err == nil {
		t.Error("ParseSigningKey accepted an account seed")
	}
	kp, _ := signingKey(t)
	cseed, _ := kp.Seed()
	if _, err := ParseSigningKey(cseed); err != nil {
		t.Errorf("ParseSigningKey refused a cluster seed: %v", err)
	}

	// Hand-signed with the account key through jwt/v2 directly.
	now := time.Now()
	gc := jwt.NewGenericClaims("nonce")
	gc.Audience = string(AudienceNATS)
	gc.Expires = now.Add(time.Minute).Unix()
	gc.Data = map[string]interface{}{"type": TokenType, "task": "t", "profile": "p", "tier": "read", "pod": "p-1", "pod_uid": "u"}
	tok, err := gc.Encode(acct)
	if err != nil {
		t.Fatal(err)
	}
	acctPub, _ := acct.PublicKey()
	if _, err := Verify(tok, []string{acctPub}, AudienceNATS, now, testLifetime, testSkew); err == nil {
		t.Error("a credential issued by a trusted account key was accepted")
	}
}

func TestExpiryAndLifetimeAreRefusedNotClamped(t *testing.T) {
	kp, pub := signingKey(t)
	// Mint stamps iat with the wall clock, so the credential's expiry is
	// fixed and each case moves the verifier's clock instead: remaining is
	// exp minus the verifier's now. The span is the ceiling plus skew, the
	// most the span check allows, so "past the ceiling plus skew" puts the
	// verifier's clock just behind iat (inside the skew) and is refused by
	// the exp-now check alone.
	exp := time.Unix(time.Now().Add(testLifetime+testSkew).Unix(), 0)
	c := validClaims(time.Now())
	c.Expires = exp
	tok := mint(t, kp, c)
	cases := []struct {
		name      string
		remaining time.Duration
		accept    bool
	}{
		{"expired a minute ago", -time.Minute, false},
		{"expired exactly skew ago", -testSkew, false},
		{"expired inside the skew", -testSkew + 2*time.Second, true},
		{"well inside the ceiling", 30 * time.Minute, true},
		{"at the ceiling plus skew", testLifetime + testSkew, true},
		{"past the ceiling plus skew", testLifetime + testSkew + 2*time.Second, false},
		{"a day out", 24 * time.Hour, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			now := exp.Add(-tc.remaining)
			got, err := Verify(tok, []string{pub}, AudienceNATS, now, testLifetime, testSkew)
			if tc.accept && err != nil {
				t.Fatalf("refused: %v", err)
			}
			if !tc.accept && err == nil {
				t.Fatalf("accepted with exp %v at %v", got.Expires, now)
			}
			if tc.accept && got.Expires.Unix() != exp.Unix() {
				t.Errorf("exp came back as %v, want %v unchanged", got.Expires, exp)
			}
		})
	}
}

// iat is the wall clock at Mint; a verifier whose clock is behind by more than
// the skew refuses it.
func TestACredentialFromTheFutureIsRefused(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	tok := mint(t, kp, validClaims(now))
	if _, err := Verify(tok, []string{pub}, AudienceNATS, now.Add(-2*testSkew), testLifetime, testSkew); err == nil {
		t.Error("a credential issued after the verifier's now+skew was accepted")
	}
}

func TestTheAudienceMustMatch(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	nats := mint(t, kp, validClaims(now))
	if _, err := Verify(nats, []string{pub}, AudienceCredentialProxy, now, testLifetime, testSkew); err == nil {
		t.Error("a nats credential verified for the credential proxy")
	}
	proxy := validClaims(now)
	proxy.Audience = AudienceCredentialProxy
	if _, err := Verify(mint(t, kp, proxy), []string{pub}, AudienceNATS, now, testLifetime, testSkew); err == nil {
		t.Error("a credential-proxy credential verified for nats")
	}
}

func TestUnknownAudiencesAreRefused(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	c := validClaims(now)
	c.Audience = "gateway"
	if _, err := Mint(kp, c); err == nil {
		t.Error("Mint accepted an unknown audience")
	}
	if _, err := Verify(mint(t, kp, validClaims(now)), []string{pub}, "gateway", now, testLifetime, testSkew); err == nil {
		t.Error("Verify accepted an unknown expected audience")
	}
	if _, err := Verify(mint(t, kp, validClaims(now)), []string{pub}, "", now, testLifetime, testSkew); err == nil {
		t.Error("Verify accepted an empty expected audience")
	}
	// Signed with an unknown audience, bypassing Mint's check.
	tok := handMint(t, kp, now, func(gc *jwt.GenericClaims) { gc.Audience = "gateway" })
	if _, err := Verify(tok, []string{pub}, "gateway", now, testLifetime, testSkew); err == nil {
		t.Error("a credential naming an unknown audience verified")
	}
}

// handMint signs a credential through jwt/v2 directly, so a test can write
// claims Mint would refuse to.
func handMint(t *testing.T, kp nkeys.KeyPair, now time.Time, edit func(*jwt.GenericClaims)) string {
	t.Helper()
	c := validClaims(now)
	gc := jwt.NewGenericClaims("nonce-for-a-hand-minted-credential")
	gc.Audience = string(c.Audience)
	gc.Expires = c.Expires.Unix()
	gc.Data = map[string]interface{}{
		"type": TokenType, "task": c.Task, "profile": c.Profile, "tier": c.Tier, "pod": c.Pod, "pod_uid": c.PodUID,
	}
	edit(gc)
	tok, err := gc.Encode(kp)
	if err != nil {
		t.Fatal(err)
	}
	return tok
}

func TestMalformedClaimsAreRefused(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	bad := []string{"", "a.b", "*", ">", "a*", "a>", "has space", "tab\there", "UPPER", "-lead", strings.Repeat("a", 64), "a\nb"}
	for _, field := range []string{"task", "profile", "tier", "pod"} {
		for _, v := range bad {
			c := validClaims(now)
			switch field {
			case "task":
				c.Task = v
			case "profile":
				c.Profile = v
			case "tier":
				c.Tier = v
			case "pod":
				c.Pod = v
			}
			if _, err := Mint(kp, c); err == nil {
				t.Errorf("Mint accepted %s %q", field, v)
			}
			tok := handMint(t, kp, now, func(gc *jwt.GenericClaims) { gc.Data[field] = v })
			if _, err := Verify(tok, []string{pub}, AudienceNATS, now, testLifetime, testSkew); err == nil {
				t.Errorf("Verify accepted %s %q", field, v)
			}
		}
	}
	for _, v := range []string{"", "has space", "a.b", strings.Repeat("a", 65), "UID"} {
		tok := handMint(t, kp, now, func(gc *jwt.GenericClaims) { gc.Data["pod_uid"] = v })
		if _, err := Verify(tok, []string{pub}, AudienceNATS, now, testLifetime, testSkew); err == nil {
			t.Errorf("Verify accepted pod_uid %q", v)
		}
	}
	for name, edit := range map[string]func(*jwt.GenericClaims){
		"a missing task":      func(gc *jwt.GenericClaims) { delete(gc.Data, "task") },
		"a non-string tier":   func(gc *jwt.GenericClaims) { gc.Data["tier"] = 7 },
		"an unknown claim":    func(gc *jwt.GenericClaims) { gc.Data["grants"] = "a2a.>" },
		"another token type":  func(gc *jwt.GenericClaims) { gc.Data["type"] = "user" },
		"no token type":       func(gc *jwt.GenericClaims) { delete(gc.Data, "type") },
		"no expiry":           func(gc *jwt.GenericClaims) { gc.Expires = 0 },
		"a future not-before": func(gc *jwt.GenericClaims) { gc.NotBefore = now.Add(time.Hour).Unix() },
		"a target under nats": func(gc *jwt.GenericClaims) { gc.Data["target"] = Target{"p", "l", "c"} },
		"an empty target": func(gc *jwt.GenericClaims) {
			gc.Audience = string(AudienceCredentialProxy)
			gc.Data["target"] = Target{}
		},
		"a spaced target name": func(gc *jwt.GenericClaims) {
			gc.Audience = string(AudienceCredentialProxy)
			gc.Data["target"] = Target{"p", "l", "c d"}
		},
	} {
		aud := AudienceNATS
		tok := handMint(t, kp, now, func(gc *jwt.GenericClaims) {
			edit(gc)
			aud = Audience(gc.Audience)
		})
		if _, err := Verify(tok, []string{pub}, aud, now, testLifetime, testSkew); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestATargetIsForTheCredentialProxyAlone(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	target := &Target{Project: "my-project", Location: "us-central1", Cluster: "prod-1"}

	c := validClaims(now)
	c.Target = target
	if _, err := Mint(kp, c); err == nil {
		t.Error("Mint put a target on a nats credential")
	}

	c.Audience = AudienceCredentialProxy
	got, err := Verify(mint(t, kp, c), []string{pub}, AudienceCredentialProxy, now, testLifetime, testSkew)
	if err != nil {
		t.Fatalf("a proxy credential with a target was refused: %v", err)
	}
	if got.Target == nil || *got.Target != *target {
		t.Errorf("target = %+v, want %+v", got.Target, target)
	}

	c.Target = nil
	got, err = Verify(mint(t, kp, c), []string{pub}, AudienceCredentialProxy, now, testLifetime, testSkew)
	if err != nil || got.Target != nil {
		t.Errorf("a proxy credential without a target: %+v, %v", got.Target, err)
	}
}

// Any edit to the payload after signing breaks the signature.
func TestATamperedPayloadIsRefused(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	tok := mint(t, kp, validClaims(now))
	parts := strings.Split(tok, ".")
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		t.Fatal(err)
	}
	var body map[string]interface{}
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatal(err)
	}
	body["nats"].(map[string]interface{})["task"] = "task-2"
	edited, _ := json.Marshal(body)
	parts[1] = base64.RawURLEncoding.EncodeToString(edited)
	if _, err := Verify(strings.Join(parts, "."), []string{pub}, AudienceNATS, now, testLifetime, testSkew); err == nil {
		t.Error("a credential whose task was edited after signing was accepted")
	}

	// A v1 header over the same payload and signature is refused too.
	v1, _ := json.Marshal(jwt.Header{Type: jwt.TokenTypeJwt, Algorithm: jwt.AlgorithmNkeyOld})
	parts = strings.Split(tok, ".")
	parts[0] = base64.RawURLEncoding.EncodeToString(v1)
	if _, err := Verify(strings.Join(parts, "."), []string{pub}, AudienceNATS, now, testLifetime, testSkew); err == nil {
		t.Error("a v1-header credential was accepted")
	}

	for _, junk := range []string{"", "a.b", "a.b.c", tok + "x"} {
		if _, err := Verify(junk, []string{pub}, AudienceNATS, now, testLifetime, testSkew); err == nil {
			t.Errorf("%q was accepted", junk)
		}
	}
}

// signRaw signs a hand-written payload. v1 signs the payload segment alone,
// the way jwt v1 did; v2 signs header.payload.
func signRaw(t *testing.T, kp nkeys.KeyPair, alg, payload string) string {
	t.Helper()
	head, _ := json.Marshal(jwt.Header{Type: jwt.TokenTypeJwt, Algorithm: alg})
	h := base64.RawURLEncoding.EncodeToString(head)
	p := base64.RawURLEncoding.EncodeToString([]byte(payload))
	input := h + "." + p
	if alg == jwt.AlgorithmNkeyOld {
		input = p
	}
	sig, err := kp.Sign([]byte(input))
	if err != nil {
		t.Fatal(err)
	}
	return h + "." + p + "." + base64.RawURLEncoding.EncodeToString(sig)
}

// A genuine v1 credential, signed the v1 way by a trusted key, so its
// signature verifies: refused for its header alone.
func TestARealV1CredentialIsRefused(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	parts := strings.Split(mint(t, kp, validClaims(now)), ".")
	raw, _ := base64.RawURLEncoding.DecodeString(parts[1])
	v1 := signRaw(t, kp, jwt.AlgorithmNkeyOld, string(raw))
	if _, err := jwt.DecodeGeneric(v1); err != nil {
		t.Fatalf("the hand-made v1 token is not a valid v1 JWT, so this test proves nothing: %v", err)
	}
	if _, err := Verify(v1, []string{pub}, AudienceNATS, now, testLifetime, testSkew); err == nil || !strings.Contains(err.Error(), "header") {
		t.Errorf("a validly signed v1 credential: %v, want a header refusal", err)
	}
}

// The payload is read with exact keys: no duplicates, no case variants of an
// allowed key, no unknown top-level claim. Each variant below is validly
// signed by a trusted key and decodes under encoding/json to a credential that
// would otherwise verify.
func TestClaimKeysAreReadExactly(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	base := fmt.Sprintf(`{"jti":"J","iat":%d,"iss":%q,"sub":"nonce","aud":"nats","exp":%d,`+
		`"nats":{"type":"kube-agents-task","version":2,"task":"task-1","profile":"auditor","tier":"read","pod":"auditor-read-x7k2p","pod_uid":"uid-1"}}`,
		now.Unix(), pub, now.Add(10*time.Minute).Unix())
	if _, err := Verify(signRaw(t, kp, jwt.AlgorithmNkey, base), []string{pub}, AudienceNATS, now, testLifetime, testSkew); err != nil {
		t.Fatalf("the base payload was refused, so the variants prove nothing: %v", err)
	}
	for name, edit := range map[string][2]string{
		"a duplicate aud":             {`"aud":"nats"`, `"aud":"credential-proxy","aud":"nats"`},
		"an AUD beside aud":           {`"aud":"nats"`, `"aud":"credential-proxy","AUD":"nats"`},
		"a duplicate task":            {`"task":"task-1"`, `"task":"task-2","task":"task-1"`},
		"a case-variant Task":         {`"task":"task-1"`, `"Task":"task-1"`},
		"a case-variant TYPE":         {`"type":"kube-agents-task"`, `"TYPE":"kube-agents-task"`},
		"a case-variant Exp":          {`"exp":`, `"Exp":`},
		"an unknown top-level claim":  {`"sub":"nonce"`, `"sub":"nonce","grants":"a2a.>"`},
		"an unknown top-level object": {`"sub":"nonce"`, `"sub":"nonce","perms":{"pub":">"}`},
	} {
		payload := strings.Replace(base, edit[0], edit[1], 1)
		if payload == base {
			t.Fatalf("%s: the edit did not apply", name)
		}
		if _, err := Verify(signRaw(t, kp, jwt.AlgorithmNkey, payload), []string{pub}, AudienceNATS, now, testLifetime, testSkew); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestTheConsumerStemIsUnambiguous(t *testing.T) {
	if got := ConsumerStem("auditor-read-x7k2p", "task-1"); got != "auditor-read-x7k2p_task-1" {
		t.Errorf("ConsumerStem = %q", got)
	}
	// "-" would let pod "a-b" with task "c" collide with pod "a" and task
	// "b-c"; "_" is in neither half's alphabet.
	if ConsumerStem("a-b", "c") == ConsumerStem("a", "b-c") {
		t.Error("two (pod, task) pairs share a stem")
	}
	// The longest inputs: a pod name and a task id are each a DNS-1123
	// label, at most 63 bytes.
	long := ConsumerStem(strings.Repeat("p", 63), strings.Repeat("t", 63))
	if len(long) != 127 {
		t.Errorf("the longest stem is %d bytes, want 127", len(long))
	}
	for _, role := range []string{"origin", "in", "events"} {
		if n := len(long + "-" + role); n > natsserver.JSMaxNameLen {
			t.Errorf("the longest %s consumer name is %d bytes, past nats-server's JSMaxNameLen (%d)", role, n, natsserver.JSMaxNameLen)
		}
	}
}

// The credential's own span is held to the ceiling too: one minted for longer
// is refused even once enough of it has passed that exp-now would fit.
func TestACredentialSpanningMoreThanTheCeilingIsRefused(t *testing.T) {
	kp, pub := signingKey(t)
	minted := time.Now()
	c := validClaims(minted)
	c.Expires = minted.Add(testLifetime + testSkew + 5*time.Minute)
	tok := mint(t, kp, c)
	later := minted.Add(10 * time.Minute) // exp-now is now inside the ceiling
	if _, err := Verify(tok, []string{pub}, AudienceNATS, later, testLifetime, testSkew); err == nil || !strings.Contains(err.Error(), "spans") {
		t.Errorf("a credential spanning past the ceiling: %v, want a span refusal", err)
	}
	// Control: the same check passes a span inside the ceiling plus skew.
	c.Expires = minted.Add(testLifetime + testSkew - time.Second)
	if _, err := Verify(mint(t, kp, c), []string{pub}, AudienceNATS, later, testLifetime, testSkew); err != nil {
		t.Errorf("a span inside the ceiling plus skew was refused: %v", err)
	}
}

// nats.version is written by jwt/v2 on every Encode, so Mint's credentials
// always carry 2; Verify requires exactly that. Hand-signed with a trusted key,
// so only the version check stands between each variant and acceptance.
func TestTheClaimsVersionIsJWTv2s(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	parts := strings.Split(mint(t, kp, validClaims(now)), ".")
	raw, _ := base64.RawURLEncoding.DecodeString(parts[1])
	if !strings.Contains(string(raw), `"version":2`) {
		t.Fatalf("a minted credential's payload carries no version 2: %s", raw)
	}
	base := fmt.Sprintf(`{"jti":"J","iat":%d,"iss":%q,"sub":"nonce","aud":"nats","exp":%d,`+
		`"nats":{"type":"kube-agents-task","version":2,"task":"task-1","profile":"auditor","tier":"read","pod":"auditor-read-x7k2p","pod_uid":"uid-1"}}`,
		now.Unix(), pub, now.Add(10*time.Minute).Unix())
	if _, err := Verify(signRaw(t, kp, jwt.AlgorithmNkey, base), []string{pub}, AudienceNATS, now, testLifetime, testSkew); err != nil {
		t.Fatalf("the version-2 control was refused: %v", err)
	}
	for name, repl := range map[string]string{
		"version 1":    `"version":1,`,
		"version 3":    `"version":3,`,
		"no version":   ``,
		"a string":     `"version":"2",`,
		"a fractional": `"version":2.5,`,
	} {
		payload := strings.Replace(base, `"version":2,`, repl, 1)
		if _, err := Verify(signRaw(t, kp, jwt.AlgorithmNkey, payload), []string{pub}, AudienceNATS, now, testLifetime, testSkew); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}
