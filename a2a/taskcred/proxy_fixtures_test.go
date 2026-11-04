package taskcred

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/jwt/v2"
	"github.com/nats-io/nkeys"
)

// The credential proxy verifies AudienceCredentialProxy in Python
// (agents/platform/scripts/task_credential.py), and the two verifiers must
// agree. These fixtures are how: tokens signed here with fixed seeds, each
// with the verdict Verify gives it (go_ok) and the verdict the proxy must give
// it (proxy_ok). TestCredentialProxyFixtures re-verifies the checked-in file
// with Verify on every run, and the Python suite (test_task_credential.py)
// verifies the same file, so a change to either verifier that moves a verdict
// fails one of them.
//
// The two verdicts differ only where the proxy is deliberately stricter:
// a credential with no target (Verify accepts it for this audience; a proxy
// credential with no target confines nothing), and the encodings the Python
// verifier refuses (non-canonical base64url, a JSON null, header keys beyond
// typ and alg).
//
// To regenerate after changing a case or the format:
//
//	cd a2a && go test ./taskcred -run TestCredentialProxyFixtures -update-proxy-fixtures
//
// then run the Python side:
//
//	cd agents/platform/scripts && python3 -m unittest test_task_credential
//
// Every hand-signed token is deterministic (Ed25519 signatures are, and the
// times are fixed), so regenerating changes nothing but the one case minted
// through Mint, whose iat and nonce are the wall clock's and crypto/rand's.
var updateProxyFixtures = flag.Bool("update-proxy-fixtures", false, "rewrite testdata/credential_proxy_fixtures.json")

const proxyFixturesPath = "testdata/credential_proxy_fixtures.json"

// The verifier settings every case uses unless it says otherwise: the
// callout's skew, and an hour's ceiling so the lifetime cases stay readable.
const (
	fixtureMaxLifetime = time.Hour
	fixtureSkew        = 30 * time.Second
	// 2030-01-01T00:00:00Z: well clear of the wall clock, so no case depends
	// on when the suite runs.
	fixtureNow int64 = 1893456000
)

type proxyFixtureFile struct {
	Comment            string         `json:"comment"`
	Regenerate         string         `json:"regenerate"`
	Audience           string         `json:"audience"`
	Trusted            []string       `json:"trusted"`
	Untrusted          string         `json:"untrusted"`
	MaxLifetimeSeconds int64          `json:"max_lifetime_seconds"`
	SkewSeconds        int64          `json:"skew_seconds"`
	Cases              []proxyFixture `json:"cases"`
}

type proxyFixture struct {
	Name    string `json:"name"`
	Token   string `json:"token"`
	Now     int64  `json:"now"`
	GoOK    bool   `json:"go_ok"`
	ProxyOK bool   `json:"proxy_ok"`
	// Trusted overrides the file's list for one case (the untrusted-key and
	// rotation cases).
	Trusted []string `json:"trusted,omitempty"`
	// Claims is what a verifier that accepts the token must read out of it.
	Claims *proxyFixtureClaims `json:"claims,omitempty"`
}

type proxyFixtureClaims struct {
	Task    string  `json:"task"`
	Profile string  `json:"profile"`
	Tier    string  `json:"tier"`
	Pod     string  `json:"pod"`
	PodUID  string  `json:"pod_uid"`
	Expires int64   `json:"exp"`
	Target  *Target `json:"target"`
}

// fixtureKey is a task-signing key from a fixed 32-byte seed.
func fixtureKey(t *testing.T, fill byte) (nkeys.KeyPair, string) {
	t.Helper()
	kp, err := nkeys.FromRawSeed(nkeys.PrefixByteCluster, bytes.Repeat([]byte{fill}, 32))
	if err != nil {
		t.Fatal(err)
	}
	pub, err := kp.PublicKey()
	if err != nil {
		t.Fatal(err)
	}
	return kp, pub
}

var fixtureTarget = Target{Project: "proj-a", Location: "us-central1", Cluster: "cluster-a"}

// fixturePayload is a credential-proxy payload in the shape Mint writes, with
// fixed times. edits replace substrings to make each variant.
func fixturePayload(iss string, iat, exp int64, edits ...string) string {
	p := fmt.Sprintf(`{"jti":"FIXTUREJTI","iat":%d,"iss":%q,"sub":"fixturenonce","aud":"credential-proxy","exp":%d,`+
		`"nats":{"type":"kube-agents-task","version":2,"task":"task-1","profile":"auditor","tier":"read",`+
		`"pod":"auditor-read-x7k2p","pod_uid":"0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0",`+
		`"target":{"project":"proj-a","location":"us-central1","cluster":"cluster-a"}}}`, iat, iss, exp)
	for i := 0; i+1 < len(edits); i += 2 {
		p = strings.Replace(p, edits[i], edits[i+1], 1)
	}
	return p
}

func buildProxyFixtures(t *testing.T) proxyFixtureFile {
	kp, pub := fixtureKey(t, 0x11)
	otherKP, otherPub := fixtureKey(t, 0x22)
	iat, exp := fixtureNow-60, fixtureNow+600
	base := fixturePayload(pub, iat, exp)
	wantClaims := &proxyFixtureClaims{
		Task: "task-1", Profile: "auditor", Tier: "read", Pod: "auditor-read-x7k2p",
		PodUID: "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0", Expires: exp, Target: &fixtureTarget,
	}
	sign := func(payload string) string { return signRaw(t, kp, jwt.AlgorithmNkey, payload) }
	edited := func(edits ...string) string {
		p := fixturePayload(pub, iat, exp, edits...)
		if p == base {
			t.Fatalf("fixture edit %q did not apply", edits)
		}
		return sign(p)
	}
	refused := func(name, token string) proxyFixture {
		return proxyFixture{Name: name, Token: token, Now: fixtureNow}
	}

	var cases []proxyFixture
	add := func(c proxyFixture) { cases = append(cases, c) }

	// Accepted.
	add(proxyFixture{Name: "valid", Token: sign(base), Now: fixtureNow, GoOK: true, ProxyOK: true, Claims: wantClaims})
	mintNow := time.Now()
	minted, err := Mint(kp, Claims{
		Task: "task-1", Profile: "auditor", Tier: "read", Pod: "auditor-read-x7k2p",
		PodUID: "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0", Audience: AudienceCredentialProxy,
		Expires: mintNow.Add(10 * time.Minute), Target: &fixtureTarget,
	})
	if err != nil {
		t.Fatal(err)
	}
	mintedClaims := *wantClaims
	mintedClaims.Expires = mintNow.Add(10 * time.Minute).Unix()
	add(proxyFixture{Name: "minted by taskcred.Mint", Token: minted, Now: mintNow.Unix(), GoOK: true, ProxyOK: true, Claims: &mintedClaims})
	add(proxyFixture{Name: "the second of two trusted keys (rotation)", Token: sign(base), Now: fixtureNow, GoOK: true, ProxyOK: true,
		Trusted: []string{otherPub, pub}, Claims: wantClaims})
	add(proxyFixture{Name: "expired inside the skew", Token: sign(base), Now: exp + 29, GoOK: true, ProxyOK: true, Claims: wantClaims})
	add(proxyFixture{Name: "a lowercase typ (jwt/v2 upper-cases it)", Token: signWithHeader(t, kp, `{"typ":"jwt","alg":"ed25519-nkey"}`, base),
		Now: fixtureNow, GoOK: true, ProxyOK: true, Claims: wantClaims})

	// Go accepts, the proxy refuses: stricter by design.
	noTarget := edited(`,"target":{"project":"proj-a","location":"us-central1","cluster":"cluster-a"}`, ``)
	add(proxyFixture{Name: "no target", Token: noTarget, Now: fixtureNow, GoOK: true})
	add(proxyFixture{Name: "a null name", Token: edited(`"sub":"fixturenonce"`, `"sub":"fixturenonce","name":null`), Now: fixtureNow, GoOK: true})
	add(proxyFixture{Name: "a header key beyond typ and alg", Token: signWithHeader(t, kp, `{"typ":"JWT","alg":"ed25519-nkey","kid":"x"}`, base),
		Now: fixtureNow, GoOK: true})
	add(proxyFixture{Name: "a non-canonical base64url signature", Token: nonCanonicalSignature(t, sign(base)), Now: fixtureNow, GoOK: true})

	// Signature and issuer.
	parts := strings.Split(sign(base), ".")
	raw, _ := base64.RawURLEncoding.DecodeString(parts[1])
	tampered := strings.Replace(string(raw), `"task":"task-1"`, `"task":"task-2"`, 1)
	add(refused("tampered after signing", parts[0]+"."+base64.RawURLEncoding.EncodeToString([]byte(tampered))+"."+parts[2]))
	add(refused("signed by an untrusted key, naming it", signRaw(t, otherKP, jwt.AlgorithmNkey, fixturePayload(otherPub, iat, exp))))
	add(refused("signed by an untrusted key, naming the trusted one", signRaw(t, otherKP, jwt.AlgorithmNkey, base)))
	add(proxyFixture{Name: "a valid token, verified by a verifier that trusts another key", Token: sign(base), Now: fixtureNow, Trusted: []string{otherPub}})
	add(refused("a v1 header, signed the v1 way", signRaw(t, kp, jwt.AlgorithmNkeyOld, base)))
	add(refused("a v1 header over a v2 signature", func() string {
		v1, _ := json.Marshal(jwt.Header{Type: jwt.TokenTypeJwt, Algorithm: jwt.AlgorithmNkeyOld})
		p := strings.Split(sign(base), ".")
		return base64.RawURLEncoding.EncodeToString(v1) + "." + p[1] + "." + p[2]
	}()))
	add(refused("not a JWT", "a.b"))
	valid := sign(base)
	add(refused("the signature truncated", valid[:len(valid)-4]))

	// Strict JSON.
	add(refused("a duplicate aud", edited(`"aud":"credential-proxy"`, `"aud":"nats","aud":"credential-proxy"`)))
	add(refused("a duplicate nats key", edited(`"task":"task-1"`, `"task":"task-2","task":"task-1"`)))
	add(refused("a duplicate target key", edited(`"cluster":"cluster-a"`, `"cluster":"other","cluster":"cluster-a"`)))
	add(refused("a case-variant AUD beside aud", edited(`"aud":"credential-proxy"`, `"aud":"credential-proxy","AUD":"nats"`)))
	add(refused("a case-variant Task", edited(`"task":"task-1"`, `"Task":"task-1"`)))
	add(refused("a case-variant target key", edited(`"cluster":"cluster-a"`, `"Cluster":"cluster-a"`)))
	add(refused("an unknown top-level claim", edited(`"sub":"fixturenonce"`, `"sub":"fixturenonce","grants":"a2a.>"`)))
	add(refused("an unknown nats claim", edited(`"tier":"read"`, `"tier":"read","grants":"a2a.>"`)))
	add(refused("an unknown target claim", edited(`"cluster":"cluster-a"`, `"cluster":"cluster-a","server":"https://x"`)))
	add(refused("a NaN", edited(`"version":2`, `"version":NaN`)))

	// Types.
	add(refused("version 1", edited(`"version":2`, `"version":1`)))
	add(refused("version as a string", edited(`"version":2`, `"version":"2"`)))
	add(refused("version as a bool", edited(`"version":2`, `"version":true`)))
	add(refused("version as a float", edited(`"version":2`, `"version":2.5`)))
	add(refused("exp as a string", edited(fmt.Sprintf(`"exp":%d`, exp), fmt.Sprintf(`"exp":"%d"`, exp))))
	add(refused("exp in exponent form", edited(fmt.Sprintf(`"exp":%d`, exp), fmt.Sprintf(`"exp":%de0`, exp))))
	add(refused("aud as a list", edited(`"aud":"credential-proxy"`, `"aud":["credential-proxy"]`)))
	add(refused("another type", edited(`"type":"kube-agents-task"`, `"type":"user"`)))

	// Audience.
	add(refused("the nats audience", edited(`"aud":"credential-proxy"`, `"aud":"nats"`)))
	add(refused("the nats audience with no target",
		sign(fixturePayload(pub, iat, exp, `"aud":"credential-proxy"`, `"aud":"nats"`,
			`,"target":{"project":"proj-a","location":"us-central1","cluster":"cluster-a"}`, ``))))
	add(refused("an unknown audience", edited(`"aud":"credential-proxy"`, `"aud":"credential-proxy-x"`)))

	// Times, with the verifier's clock moved rather than the token's.
	add(proxyFixture{Name: "expired a minute ago", Token: sign(base), Now: exp + 60})
	add(proxyFixture{Name: "expired exactly skew ago", Token: sign(base), Now: exp + 30})
	add(proxyFixture{Name: "issued in the future past the skew", Token: sign(base), Now: iat - 31})
	add(refused("expiry past the ceiling plus skew from now", sign(fixturePayload(pub, fixtureNow, fixtureNow+3600+31))))
	add(proxyFixture{Name: "a span past the ceiling, with exp-now inside it",
		Token: sign(fixturePayload(pub, fixtureNow-3600, fixtureNow+60)), Now: fixtureNow})
	add(refused("not valid yet (nbf)", edited(`"sub":"fixturenonce"`, fmt.Sprintf(`"sub":"fixturenonce","nbf":%d`, fixtureNow+120))))
	add(refused("no expiry", edited(fmt.Sprintf(`"exp":%d`, exp), `"exp":0`)))
	add(refused("no issue time", edited(fmt.Sprintf(`"iat":%d,`, iat), ``)))
	add(refused("an empty jti", edited(`"jti":"FIXTUREJTI"`, `"jti":""`)))
	add(refused("an empty subject", edited(`"sub":"fixturenonce"`, `"sub":""`)))

	// Shapes.
	add(refused("an uppercase pod", edited(`"pod":"auditor-read-x7k2p"`, `"pod":"Auditor-read-x7k2p"`)))
	add(refused("a dotted task", edited(`"task":"task-1"`, `"task":"task.1"`)))
	add(refused("a pod name past 63 bytes", edited(`"pod":"auditor-read-x7k2p"`, `"pod":"`+strings.Repeat("p", 64)+`"`)))
	add(refused("a pod_uid with a slash", edited(`"pod_uid":"0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"`, `"pod_uid":"a/b"`)))
	add(refused("a target field with a space", edited(`"project":"proj-a"`, `"project":"proj a"`)))
	add(refused("an empty target field", edited(`"cluster":"cluster-a"`, `"cluster":""`)))

	return proxyFixtureFile{
		Comment: "Task credentials for the credential-proxy audience, with the verdict taskcred.Verify gives each (go_ok) " +
			"and the one agents/platform/scripts/task_credential.py must give (proxy_ok). Generated by proxy_fixtures_test.go; " +
			"the seeds are fixed test values, never a deployed key.",
		Regenerate:         "cd a2a && go test ./taskcred -run TestCredentialProxyFixtures -update-proxy-fixtures",
		Audience:           string(AudienceCredentialProxy),
		Trusted:            []string{pub},
		Untrusted:          otherPub,
		MaxLifetimeSeconds: int64(fixtureMaxLifetime / time.Second),
		SkewSeconds:        int64(fixtureSkew / time.Second),
		Cases:              cases,
	}
}

// signWithHeader signs payload under a hand-written v2 header.
func signWithHeader(t *testing.T, kp nkeys.KeyPair, header, payload string) string {
	t.Helper()
	h := base64.RawURLEncoding.EncodeToString([]byte(header))
	p := base64.RawURLEncoding.EncodeToString([]byte(payload))
	sig, err := kp.Sign([]byte(h + "." + p))
	if err != nil {
		t.Fatal(err)
	}
	return h + "." + p + "." + base64.RawURLEncoding.EncodeToString(sig)
}

func TestCredentialProxyFixtures(t *testing.T) {
	if *updateProxyFixtures {
		doc, err := json.MarshalIndent(buildProxyFixtures(t), "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(filepath.Dir(proxyFixturesPath), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(proxyFixturesPath, append(doc, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	raw, err := os.ReadFile(proxyFixturesPath)
	if err != nil {
		t.Fatalf("%v; regenerate with -update-proxy-fixtures", err)
	}
	var file proxyFixtureFile
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	if file.Audience != string(AudienceCredentialProxy) || len(file.Cases) == 0 {
		t.Fatalf("fixture file names audience %q and %d cases", file.Audience, len(file.Cases))
	}
	maxLifetime := time.Duration(file.MaxLifetimeSeconds) * time.Second
	skew := time.Duration(file.SkewSeconds) * time.Second
	seen := map[string]bool{}
	for _, c := range file.Cases {
		if seen[c.Name] {
			t.Errorf("two cases are named %q", c.Name)
		}
		seen[c.Name] = true
		trusted := file.Trusted
		if c.Trusted != nil {
			trusted = c.Trusted
		}
		got, err := Verify(c.Token, trusted, AudienceCredentialProxy, time.Unix(c.Now, 0), maxLifetime, skew)
		if (err == nil) != c.GoOK {
			t.Errorf("%s: Verify err = %v, fixture says go_ok=%v", c.Name, err, c.GoOK)
			continue
		}
		if c.ProxyOK && !c.GoOK {
			t.Errorf("%s: the proxy may be stricter than Verify, never laxer", c.Name)
		}
		if err == nil && c.Claims != nil {
			w := c.Claims
			if got.Task != w.Task || got.Profile != w.Profile || got.Tier != w.Tier || got.Pod != w.Pod ||
				got.PodUID != w.PodUID || got.Expires.Unix() != w.Expires || got.Target == nil || *got.Target != *w.Target {
				t.Errorf("%s: claims = %+v (target %+v), fixture says %+v", c.Name, got, got.Target, w)
			}
		}
	}
}

// nonCanonicalSignature sets the unused low bits of the signature segment's
// last character. A 64-byte Ed25519 signature is 86 base64url characters, and
// the last carries 2 bits of signature and 4 unused ones: Go's non-strict
// RawURLEncoding ignores them, so the token still verifies, while the proxy
// re-encodes the decoded bytes and refuses a segment that doesn't round-trip.
func nonCanonicalSignature(t *testing.T, token string) string {
	t.Helper()
	const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
	parts := strings.Split(token, ".")
	signature := parts[2]
	if len(signature) != 86 {
		t.Fatalf("signature segment is %d characters, want 86", len(signature))
	}
	last := strings.IndexByte(alphabet, signature[85])
	if last < 0 || last&0x0f != 0 {
		t.Fatalf("signature's last character %q is not canonical to begin with", signature[85])
	}
	parts[2] = signature[:85] + string(alphabet[last|0x01])
	return strings.Join(parts, ".")
}
