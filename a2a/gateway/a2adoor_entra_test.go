package gateway

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// The A2A door's Microsoft Entra token class: the verifier against a fake
// tenant (a signing key and a key-set endpoint), then the class end to end
// on the door's rig.

const (
	entraTestTenant     = "72f988bf-86f1-41af-91ab-2d7cd011db47"
	entraTestClientID   = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
	entraTestObjectID   = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
	entraTestOtherOID   = "9f8e7d6c-5b4a-4938-a7b6-c5d4e3f2a1b0"
	entraTestUsername   = "dev@kubeagents.onmicrosoft.com"
	entraTestKid        = "key-1"
	entraTestRotatedKid = "key-2"
	entraTestIntruder   = "intruder@example.com"
)

// entraTestKeys are the fake tenant's signing keys, generated once: a
// 2048-bit key takes long enough that one per test would dominate the
// package's run.
var (
	entraTestKeysOnce sync.Once
	entraTestKey      *rsa.PrivateKey
	entraTestRotated  *rsa.PrivateKey
	entraTestStranger *rsa.PrivateKey
)

func entraKeys(t *testing.T) (signing, rotated, stranger *rsa.PrivateKey) {
	t.Helper()
	entraTestKeysOnce.Do(func() {
		for _, k := range []**rsa.PrivateKey{&entraTestKey, &entraTestRotated, &entraTestStranger} {
			key, err := rsa.GenerateKey(rand.Reader, 2048)
			if err != nil {
				panic(err)
			}
			*k = key
		}
	})
	return entraTestKey, entraTestRotated, entraTestStranger
}

// fakeTenant serves the key set and counts the fetches. Which keys it
// publishes, and whether it answers at all, can change under a test.
type fakeTenant struct {
	mu      sync.Mutex
	keys    map[string]*rsa.PublicKey
	status  int
	fetches atomic.Int64
	srv     *httptest.Server
}

func newFakeTenant(t *testing.T) *fakeTenant {
	t.Helper()
	signing, _, _ := entraKeys(t)
	f := &fakeTenant{keys: map[string]*rsa.PublicKey{entraTestKid: &signing.PublicKey}, status: http.StatusOK}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.fetches.Add(1)
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.status != http.StatusOK {
			w.WriteHeader(f.status)
			return
		}
		var set entraKeySet
		for kid, key := range f.keys {
			set.Keys = append(set.Keys, struct {
				Kty string `json:"kty"`
				Kid string `json:"kid"`
				N   string `json:"n"`
				E   string `json:"e"`
			}{
				Kty: "RSA", Kid: kid,
				N: base64.RawURLEncoding.EncodeToString(key.N.Bytes()),
				E: base64.RawURLEncoding.EncodeToString(big.NewInt(int64(key.E)).Bytes()),
			})
		}
		_ = json.NewEncoder(w).Encode(set)
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeTenant) publish(kid string, key *rsa.PublicKey) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.keys[kid] = key
}

func (f *fakeTenant) answer(status int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.status = status
}

// entraClaimsFor are the claims of a live v2.0 token for the test
// application, which a test then edits.
func entraClaimsFor() map[string]any { return entraClaimsAt(time.Now()) }

// entraClaimsAt is entraClaimsFor for a token issued at now, for tests that
// move the verifier's clock.
func entraClaimsAt(now time.Time) map[string]any {
	return map[string]any{
		"iss": fmt.Sprintf(a2aEntraIssuerV2Format, entraTestTenant),
		"aud": entraTestClientID, "tid": entraTestTenant, "oid": entraTestObjectID,
		"preferred_username": entraTestUsername,
		"exp":                now.Add(time.Hour).Unix(), "nbf": now.Add(-time.Minute).Unix(), "iat": now.Add(-time.Minute).Unix(),
	}
}

// mintEntra signs a token with key under kid and alg. Header and claims are
// whatever the test says: this is the tenant, and also the forger.
func mintEntra(t *testing.T, key *rsa.PrivateKey, header map[string]any, claims map[string]any) string {
	t.Helper()
	if header == nil {
		header = map[string]any{"alg": a2aEntraAlgorithm, "typ": "JWT", "kid": entraTestKid}
	}
	h, err := json.Marshal(header)
	if err != nil {
		t.Fatal(err)
	}
	c, err := json.Marshal(claims)
	if err != nil {
		t.Fatal(err)
	}
	signing := base64.RawURLEncoding.EncodeToString(h) + "." + base64.RawURLEncoding.EncodeToString(c)
	digest := sha256.Sum256([]byte(signing))
	sig, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, digest[:])
	if err != nil {
		t.Fatal(err)
	}
	return signing + "." + base64.RawURLEncoding.EncodeToString(sig)
}

func liveEntraToken(t *testing.T) string {
	t.Helper()
	signing, _, _ := entraKeys(t)
	return mintEntra(t, signing, nil, entraClaimsFor())
}

func testEntraVerifier(f *fakeTenant) *entraTokenVerifier {
	v := newEntraTokenVerifier(entraTestTenant, entraTestClientID)
	v.keysURL = f.srv.URL
	return v
}

func TestEntraVerifierAdmitsATokenOfTheTenantForTheApplication(t *testing.T) {
	t.Parallel()
	v := testEntraVerifier(newFakeTenant(t))
	id, err := v.verify(context.Background(), liveEntraToken(t))
	if err != nil {
		t.Fatal(err)
	}
	if id.objectID != entraTestObjectID || id.username != entraTestUsername {
		t.Errorf("identity = %+v", id)
	}
}

// TestEntraVerifierAdmitsEveryShapeTheTenantIssues: a v1.0 issuer, the
// application id URI as the audience, and an audience array are all tokens
// the install's tenant issues for the install's application.
func TestEntraVerifierAdmitsEveryShapeTheTenantIssues(t *testing.T) {
	t.Parallel()
	v := testEntraVerifier(newFakeTenant(t))
	signing, _, _ := entraKeys(t)
	for name, edit := range map[string]func(map[string]any){
		"v1 issuer":  func(c map[string]any) { c["iss"] = fmt.Sprintf(a2aEntraIssuerV1Format, entraTestTenant) },
		"api:// aud": func(c map[string]any) { c["aud"] = a2aEntraAudienceURIPrefix + entraTestClientID },
		"aud array":  func(c map[string]any) { c["aud"] = []string{"other", entraTestClientID} },
		"no nbf":     func(c map[string]any) { delete(c, "nbf") },
		"upper-case oid": func(c map[string]any) {
			c["oid"] = strings.ToUpper(entraTestObjectID)
		},
	} {
		claims := entraClaimsFor()
		edit(claims)
		id, err := v.verify(context.Background(), mintEntra(t, signing, nil, claims))
		if err != nil {
			t.Errorf("%s: %v", name, err)
			continue
		}
		if !strings.EqualFold(id.objectID, entraTestObjectID) {
			t.Errorf("%s: object id %q", name, id.objectID)
		}
		if name == "upper-case oid" && id.objectID != strings.ToUpper(entraTestObjectID) {
			t.Errorf("the object id was not returned as the tenant sent it: %q", id.objectID)
		}
	}
}

// TestEntraVerifierRefusals: every claim the class checks, refused on its
// own, each a verdict against the token (errEntraTokenRefused), never a
// failure to check.
func TestEntraVerifierRefusals(t *testing.T) {
	t.Parallel()
	f := newFakeTenant(t)
	v := testEntraVerifier(f)
	signing, _, stranger := entraKeys(t)
	rs256 := func(kid string) map[string]any {
		return map[string]any{"alg": a2aEntraAlgorithm, "typ": "JWT", "kid": kid}
	}
	cases := map[string]struct {
		token string
		want  string
		// fresh runs the case on a verifier that has not fetched yet: an
		// unknown key is a refusal only once a fresh set has been read.
		fresh bool
	}{
		"not a JWT":          {token: "ya29.not-a-jwt", want: "not shaped"},
		"two segments":       {token: "aGVhZGVy.cGF5bG9hZA", want: "not shaped"},
		"empty segment":      {token: "aGVhZGVy..c2ln", want: "not shaped"},
		"oversized":          {token: strings.Repeat("a", a2aEntraMaxTokenBytes) + ".b.c", want: "not shaped"},
		"header not JSON":    {token: "bm90LWpzb24.cGF5bG9hZA.c2ln", want: "header does not decode"},
		"alg none":           {token: mintEntra(t, signing, map[string]any{"alg": "none", "kid": entraTestKid}, entraClaimsFor()), want: "not signed with RS256"},
		"alg HS256":          {token: mintEntra(t, signing, map[string]any{"alg": "HS256", "kid": entraTestKid}, entraClaimsFor()), want: "not signed with RS256"},
		"no kid":             {token: mintEntra(t, signing, map[string]any{"alg": a2aEntraAlgorithm}, entraClaimsFor()), want: "names no signing key"},
		"unknown kid":        {token: mintEntra(t, signing, rs256("invented"), entraClaimsFor()), want: "does not publish", fresh: true},
		"stranger's key":     {token: mintEntra(t, stranger, nil, entraClaimsFor()), want: "signature does not verify"},
		"wrong issuer":       {token: mintEntra(t, signing, nil, edited(entraClaimsFor(), "iss", "https://login.microsoftonline.com/other-tenant/v2.0")), want: "not issued by this install's tenant"},
		"wrong tid":          {token: mintEntra(t, signing, nil, edited(entraClaimsFor(), "tid", "other-tenant")), want: "not issued by this install's tenant"},
		"wrong audience":     {token: mintEntra(t, signing, nil, edited(entraClaimsFor(), "aud", "another-app")), want: "not issued for this install's application"},
		"no audience":        {token: mintEntra(t, signing, nil, edited(entraClaimsFor(), "aud", nil)), want: "not issued for this install's application"},
		"expired":            {token: mintEntra(t, signing, nil, edited(entraClaimsFor(), "exp", time.Now().Add(-a2aEntraClockSkew-time.Minute).Unix())), want: "expired"},
		"no exp":             {token: mintEntra(t, signing, nil, edited(entraClaimsFor(), "exp", nil)), want: "expired"},
		"not yet valid":      {token: mintEntra(t, signing, nil, edited(entraClaimsFor(), "nbf", time.Now().Add(a2aEntraClockSkew+time.Minute).Unix())), want: "not valid yet"},
		"no oid":             {token: mintEntra(t, signing, nil, edited(entraClaimsFor(), "oid", nil)), want: "carries no object id"},
		"oid not a GUID":     {token: mintEntra(t, signing, nil, edited(entraClaimsFor(), "oid", "dev@example.com")), want: "not shaped like one"},
		"oid with a colon":   {token: mintEntra(t, signing, nil, edited(entraClaimsFor(), "oid", "entra:"+entraTestObjectID)), want: "not shaped like one"},
		"claims not JSON":    {token: tamperedPayload(t, signing, "not-json"), want: "claims do not decode"},
		"tampered claims":    {token: tamperedClaims(t, liveEntraToken(t), "oid", entraTestOtherOID), want: "signature does not verify"},
		"signature garbage":  {token: strings.Join(strings.Split(liveEntraToken(t), ".")[:2], ".") + ".c2ln", want: "signature does not verify"},
		"signature not b64u": {token: strings.Join(strings.Split(liveEntraToken(t), ".")[:2], ".") + ".c2ln=", want: "not shaped"},
	}
	for name, c := range cases {
		verifier := v
		if c.fresh {
			verifier = testEntraVerifier(f)
		}
		_, err := verifier.verify(context.Background(), c.token)
		if err == nil {
			t.Errorf("%s: admitted", name)
			continue
		}
		if !errors.Is(err, errEntraTokenRefused) {
			t.Errorf("%s: not a refusal: %v", name, err)
		}
		if !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: err = %v, want it to say %q", name, err, c.want)
		}
		if strings.Contains(err.Error(), c.token[:min(len(c.token), 12)]) && len(c.token) > 12 {
			t.Errorf("%s: the error carries the token", name)
		}
	}
	// A still-live token stays admitted, so the refusals above were not a
	// verifier that refuses everything.
	if _, err := v.verify(context.Background(), liveEntraToken(t)); err != nil {
		t.Fatal(err)
	}
}

// edited returns claims with one claim set, or dropped when value is nil.
func edited(claims map[string]any, claim string, value any) map[string]any {
	if value == nil {
		delete(claims, claim)
	} else {
		claims[claim] = value
	}
	return claims
}

// tamperedClaims rewrites one claim of a signed token without re-signing.
func tamperedClaims(t *testing.T, token, claim string, value any) string {
	t.Helper()
	segments := strings.Split(token, ".")
	payload, err := base64.RawURLEncoding.DecodeString(segments[1])
	if err != nil {
		t.Fatal(err)
	}
	var claims map[string]any
	if err := json.Unmarshal(payload, &claims); err != nil {
		t.Fatal(err)
	}
	claims[claim] = value
	edited, err := json.Marshal(claims)
	if err != nil {
		t.Fatal(err)
	}
	return segments[0] + "." + base64.RawURLEncoding.EncodeToString(edited) + "." + segments[2]
}

// tamperedPayload signs a payload that is not JSON, so the signature holds
// and the claims do not decode.
func tamperedPayload(t *testing.T, key *rsa.PrivateKey, payload string) string {
	t.Helper()
	h, _ := json.Marshal(map[string]any{"alg": a2aEntraAlgorithm, "kid": entraTestKid})
	signing := base64.RawURLEncoding.EncodeToString(h) + "." + base64.RawURLEncoding.EncodeToString([]byte(payload))
	digest := sha256.Sum256([]byte(signing))
	sig, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, digest[:])
	if err != nil {
		t.Fatal(err)
	}
	return signing + "." + base64.RawURLEncoding.EncodeToString(sig)
}

// TestEntraVerifierNeverFetchesForABearerThatIsNotAJWT: the shape check
// and the algorithm check come before any key is read, so junk bearers and
// tokens naming another algorithm cost the tenant nothing.
func TestEntraVerifierNeverFetchesForABearerThatIsNotAJWT(t *testing.T) {
	t.Parallel()
	f := newFakeTenant(t)
	v := testEntraVerifier(f)
	signing, _, _ := entraKeys(t)
	for _, token := range []string{
		"ya29.google-token", "static-token", "a.b", strings.Repeat("x", a2aEntraMaxTokenBytes+1),
		mintEntra(t, signing, map[string]any{"alg": "none", "kid": entraTestKid}, entraClaimsFor()),
	} {
		if _, err := v.verify(context.Background(), token); err == nil {
			t.Errorf("%.20q admitted", token)
		}
	}
	if n := f.fetches.Load(); n != 0 {
		t.Errorf("the tenant's keys were fetched %d times for bearers refused by shape", n)
	}
}

// TestEntraVerifierCachesTheKeySet: one fetch serves every token signed
// with a published key, for the set's lifetime.
func TestEntraVerifierCachesTheKeySet(t *testing.T) {
	t.Parallel()
	f := newFakeTenant(t)
	v := testEntraVerifier(f)
	for i := 0; i < 5; i++ {
		if _, err := v.verify(context.Background(), liveEntraToken(t)); err != nil {
			t.Fatal(err)
		}
	}
	if n := f.fetches.Load(); n != 1 {
		t.Errorf("fetched %d times for five tokens under one key", n)
	}
}

// TestEntraVerifierFollowsAKeyRotation: a token under a key the cached set
// does not hold costs one refetch, which finds the new key; the old key's
// tokens still verify through the overlap; and invented key ids cannot
// drive a refetch more than once a minute.
func TestEntraVerifierFollowsAKeyRotation(t *testing.T) {
	t.Parallel()
	f := newFakeTenant(t)
	v := testEntraVerifier(f)
	clock := time.Now()
	v.now = func() time.Time { return clock }
	signing, rotated, _ := entraKeys(t)
	if _, err := v.verify(context.Background(), liveEntraToken(t)); err != nil {
		t.Fatal(err)
	}
	rotatedToken := mintEntra(t, rotated, map[string]any{"alg": a2aEntraAlgorithm, "kid": entraTestRotatedKid}, entraClaimsFor())
	// Before the rotation is published: an unknown key, within the minute
	// of the last fetch, is a failure to check, not a refusal.
	if _, err := v.verify(context.Background(), rotatedToken); err == nil || errors.Is(err, errEntraTokenRefused) {
		t.Fatalf("an unknown key inside the refresh window: err = %v, want a failure to check", err)
	}
	if n := f.fetches.Load(); n != 1 {
		t.Fatalf("an unknown key inside the refresh window fetched: %d fetches", n)
	}
	clock = clock.Add(a2aEntraKeysMinRefresh)
	f.publish(entraTestRotatedKid, &rotated.PublicKey)
	id, err := v.verify(context.Background(), rotatedToken)
	if err != nil {
		t.Fatalf("the rotated key was not found: %v", err)
	}
	if id.objectID != entraTestObjectID {
		t.Errorf("identity = %+v", id)
	}
	if n := f.fetches.Load(); n != 2 {
		t.Errorf("the rotation cost %d fetches, want one more", n-1)
	}
	if _, err := v.verify(context.Background(), mintEntra(t, signing, nil, entraClaimsFor())); err != nil {
		t.Errorf("the previous key's token no longer verifies through the overlap: %v", err)
	}
	// Invented key ids: one more fetch for the first, none for the rest
	// inside the minute, and each a refusal since the fresh set was read.
	clock = clock.Add(a2aEntraKeysMinRefresh)
	for i := 0; i < 5; i++ {
		token := mintEntra(t, signing, map[string]any{"alg": a2aEntraAlgorithm, "kid": fmt.Sprintf("invented-%d", i)}, entraClaimsFor())
		_, err := v.verify(context.Background(), token)
		if i == 0 && !errors.Is(err, errEntraTokenRefused) {
			t.Errorf("an invented key after a fresh fetch: err = %v, want a refusal", err)
		}
		if i > 0 && (err == nil || errors.Is(err, errEntraTokenRefused)) {
			t.Errorf("invented key %d inside the refresh window: err = %v, want a failure to check", i, err)
		}
	}
	if n := f.fetches.Load(); n != 3 {
		t.Errorf("five invented key ids drove %d fetches, want one", n-2)
	}
}

// TestEntraVerifierOutageIsNotARefusal: with the tenant's keys unreachable
// and nothing cached, a token cannot be checked and is not refused; once
// a set is cached, its keys keep verifying through an outage, and a key it
// does not hold is still a failure to check rather than a refusal.
func TestEntraVerifierOutageIsNotARefusal(t *testing.T) {
	t.Parallel()
	f := newFakeTenant(t)
	v := testEntraVerifier(f)
	clock := time.Now()
	v.now = func() time.Time { return clock }
	f.answer(http.StatusServiceUnavailable)
	_, err := v.verify(context.Background(), liveEntraToken(t))
	if err == nil || errors.Is(err, errEntraTokenRefused) {
		t.Fatalf("keys unreachable, nothing cached: err = %v, want a failure to check", err)
	}
	if strings.Contains(err.Error(), f.srv.URL) {
		t.Errorf("the error leaks the key-set URL: %v", err)
	}
	f.answer(http.StatusOK)
	clock = clock.Add(a2aEntraKeysMinRefresh)
	if _, err := v.verify(context.Background(), liveEntraToken(t)); err != nil {
		t.Fatal(err)
	}
	f.srv.Close()
	clock = clock.Add(a2aEntraKeysTTL)
	signing, rotated, _ := entraKeys(t)
	if _, err := v.verify(context.Background(), mintEntra(t, signing, nil, entraClaimsAt(clock))); err != nil {
		t.Errorf("a cached key stopped verifying when the set could not be refreshed: %v", err)
	}
	rotatedToken := mintEntra(t, rotated, map[string]any{"alg": a2aEntraAlgorithm, "kid": entraTestRotatedKid}, entraClaimsAt(clock))
	clock = clock.Add(a2aEntraKeysMinRefresh)
	if _, err := v.verify(context.Background(), rotatedToken); err == nil || errors.Is(err, errEntraTokenRefused) {
		t.Errorf("an unknown key during an outage: err = %v, want a failure to check", err)
	}
}

func TestEntraVerifierReadsAKeySetWithoutRSAKeysAsNoKeys(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"keys":[{"kty":"EC","kid":"key-1","crv":"P-256","x":"AA","y":"AA"},{"kty":"RSA","kid":"","n":"AQAB","e":"AQAB"}]}`))
	}))
	t.Cleanup(srv.Close)
	v := newEntraTokenVerifier(entraTestTenant, entraTestClientID)
	v.keysURL = srv.URL
	if _, err := v.verify(context.Background(), liveEntraToken(t)); err == nil || errors.Is(err, errEntraTokenRefused) {
		t.Errorf("err = %v, want a failure to check", err)
	}
}

// startA2AEntraRig is the door's rig with the Entra class armed against a
// fake tenant: a token for entraTestObjectID (allowed, listed in another
// case) and one for entraTestOtherOID (not allowed). The chat principal
// map carries the Entra caller's door id mapped to an intruder, so a roster
// that resolved through the chat map would say so.
func startA2AEntraRig(t *testing.T) (*a2aRig, *fakeTenant) {
	t.Helper()
	f := newFakeTenant(t)
	chatMap := filepath.Join(t.TempDir(), "chat-principal-map")
	if err := os.WriteFile(chatMap, []byte(a2aEntraCaller(entraTestObjectID)+" "+entraTestIntruder+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	r := startA2ARigTuned(t, func(door *A2ADoor) Adapter { return door }, a2aRigTuning{
		doorOptions: func(o *A2ADoorOptions) {
			o.EntraTenantID = entraTestTenant
			o.EntraClientID = entraTestClientID
			o.EntraAllowedUsers = []string{strings.ToUpper(entraTestObjectID)}
		},
		builtDoor: func(d *A2ADoor) { d.entra.keysURL = f.srv.URL },
		config: func(c *Config) {
			c.A2ADoorEntraTenantID = entraTestTenant
			c.A2ADoorEntraClientID = entraTestClientID
			c.A2ADoorEntraAllowedUsers = []string{strings.ToUpper(entraTestObjectID)}
			c.PrincipalMapPath = chatMap
		},
	})
	return r, f
}

func otherEntraToken(t *testing.T) string {
	t.Helper()
	signing, _, _ := entraKeys(t)
	return mintEntra(t, signing, nil, edited(entraClaimsFor(), "oid", entraTestOtherOID))
}

// TestA2AEntraCallerStartsATaskAttributedToTheirObjectID: the class end to
// end. The bus carries the object id as the principal, the class's own
// backend and mechanism, and a roster resolved the class's way, never
// through the chat map.
func TestA2AEntraCallerStartsATaskAttributedToTheirObjectID(t *testing.T) {
	t.Parallel()
	r, _ := startA2AEntraRig(t)
	resp, status := r.rawRPC(t, liveEntraToken(t), "", a2aMethodSend, sendParams("hello as me", "m-1", "ctx-1", false))
	if status != http.StatusOK {
		t.Fatalf("HTTP %d: %+v", status, resp)
	}
	task := taskOf(t, resp)
	origin := r.awaitTask(t, "platform")
	if origin.TaskID != task.ID {
		t.Fatalf("bus task %q != answered %q", origin.TaskID, task.ID)
	}
	var authority Authority
	if err := json.Unmarshal(origin.Authority, &authority); err != nil {
		t.Fatal(err)
	}
	ps := NewPseudonymizer(r.salt)
	if authority.Requester.Backend != a2aEntraBackend || authority.Requester.VerifiedBy != a2aEntraVerifiedBy {
		t.Errorf("requester = %+v, want backend %q verifiedBy %q", authority.Requester, a2aEntraBackend, a2aEntraVerifiedBy)
	}
	if authority.Requester.Principal != ps.Hash(entraTestObjectID) {
		t.Errorf("principal = %q, want the pseudonym of %s", authority.Requester.Principal, entraTestObjectID)
	}
	if task.Metadata["backend"] != a2aEntraBackend {
		t.Errorf("task metadata backend = %v, want %q", task.Metadata["backend"], a2aEntraBackend)
	}
	wantKey := a2aKeyPrefix + a2aEntraCaller(entraTestObjectID) + ":ctx-1"
	if authority.Audience.Conversation != wantKey {
		t.Errorf("conversation = %q, want %q", authority.Audience.Conversation, wantKey)
	}
	if len(authority.Audience.Roster) != 1 || authority.Audience.Roster[0] != ps.Hash(entraTestObjectID) {
		t.Errorf("roster = %v, want [pseudonym of %s]; the pseudonym of the chat map's %s means the roster resolved through the chat map",
			authority.Audience.Roster, entraTestObjectID, entraTestIntruder)
	}
}

// TestA2AEntraCallerOffTheAllowlistIsRefusedBeforeTheDoorHoldsAnything: a
// verified account off the list is a 403 at the door, before it creates a
// conversation or a submission.
func TestA2AEntraCallerOffTheAllowlistIsRefusedBeforeTheDoorHoldsAnything(t *testing.T) {
	t.Parallel()
	r, _ := startA2AEntraRig(t)
	_, status := r.rawRPC(t, otherEntraToken(t), "", a2aMethodSend, sendParams("let me in", "m-1", "", false))
	if status != http.StatusForbidden {
		t.Fatalf("HTTP %d, want 403", status)
	}
	r.door.mu.Lock()
	conversations, submissions := len(r.door.conversations), len(r.door.submissions)
	r.door.mu.Unlock()
	if conversations != 0 || submissions != 0 {
		t.Errorf("the door holds %d conversations and %d submissions for a refused account", conversations, submissions)
	}
	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 0 {
		t.Fatalf("a caller off the allowlist reached the bus: %d envelopes", len(envs))
	}
}

// TestA2AEntraCallerMayNotAlsoNameACaller: a token travels with no name
// beside it, in the header or in the metadata.
func TestA2AEntraCallerMayNotAlsoNameACaller(t *testing.T) {
	t.Parallel()
	r, _ := startA2AEntraRig(t)
	token := liveEntraToken(t)
	resp, _ := r.rawRPC(t, token, a2aTestCaller, a2aMethodSend, sendParams("as someone else", "m-1", "", false))
	if resp.Error == nil || resp.Error.Code != rpcInvalidParams {
		t.Errorf("header: response = %+v, want error %d", resp, rpcInvalidParams)
	}
	params := sendParams("as someone else", "m-2", "", false)
	params["message"].(map[string]any)["metadata"] = map[string]any{a2aCallerMetadataKey: a2aTestCaller}
	resp, _ = r.rawRPC(t, token, "", a2aMethodSend, params)
	if resp.Error == nil || resp.Error.Code != rpcInvalidParams {
		t.Errorf("metadata: response = %+v, want error %d", resp, rpcInvalidParams)
	}
	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 0 {
		t.Fatalf("a named Entra caller reached the bus: %d envelopes", len(envs))
	}
}

// TestA2AEvalCallerCannotReachAnEntraCallersTask: the static token's holder
// names their caller freely, so the two classes must not share a scope.
func TestA2AEvalCallerCannotReachAnEntraCallersTask(t *testing.T) {
	r, _ := startA2AEntraRig(t)
	resp, _ := r.rawRPC(t, liveEntraToken(t), "", a2aMethodSend, sendParams("mine", "m-1", "ctx-1", false))
	task := taskOf(t, resp)
	for _, caller := range []string{entraTestObjectID, "entra", a2aTestCaller} {
		got, _ := r.rawRPC(t, a2aTestToken, caller, a2aMethodGet, map[string]any{"id": task.ID})
		if got.Error == nil || got.Error.Code != a2aErrTaskNotFound {
			t.Errorf("eval caller %q read the Entra caller's task: %+v", caller, got)
		}
	}
	if a2aConversationKey("entra", entraTestObjectID+":ctx-1") == a2aConversationKey(a2aEntraCaller(entraTestObjectID), "ctx-1") {
		t.Error("an eval caller can spell an Entra caller's conversation key")
	}
	evalTask := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("eval's", "m-2", "", false)))
	got, _ := r.rawRPC(t, liveEntraToken(t), "", a2aMethodGet, map[string]any{"id": evalTask.ID})
	if got.Error == nil || got.Error.Code != a2aErrTaskNotFound {
		t.Errorf("the Entra caller read an eval caller's task: %+v", got)
	}
}

// TestA2AEntraClassRefusalsAreTransportStatuses: a bad token is a 401 the
// client answers by signing in again; a token the door cannot reach a
// verdict on is a 503 it does not; the static token is untouched.
func TestA2AEntraClassRefusalsAreTransportStatuses(t *testing.T) {
	r, _ := startA2AEntraRig(t)
	signing, rotated, _ := entraKeys(t)
	body := sendParams("x", "m-1", "", false)
	if _, status := r.rawRPC(t, liveEntraToken(t), "", a2aMethodSend, body); status != http.StatusOK {
		t.Fatalf("a live token: HTTP %d", status)
	}
	expired := mintEntra(t, signing, nil, edited(entraClaimsFor(), "exp", time.Now().Add(-time.Hour).Unix()))
	if _, status := r.rawRPC(t, expired, "", a2aMethodSend, body); status != http.StatusUnauthorized {
		t.Errorf("an expired token: HTTP %d, want 401", status)
	}
	if _, status := r.rawRPC(t, "not-a-token-at-all", "", a2aMethodSend, body); status != http.StatusUnauthorized {
		t.Errorf("a junk bearer: HTTP %d, want 401", status)
	}
	// A key the cached set does not hold, inside the minute of the last
	// fetch: no verdict, so a 503 rather than a sign-in loop.
	unknownKey := mintEntra(t, rotated, map[string]any{"alg": a2aEntraAlgorithm, "kid": entraTestRotatedKid}, entraClaimsFor())
	if _, status := r.rawRPC(t, unknownKey, "", a2aMethodSend, body); status != http.StatusServiceUnavailable {
		t.Errorf("an unknown key inside the refresh window: HTTP %d, want 503", status)
	}
	if resp := r.rpc(t, a2aTestCaller, a2aMethodSend, body); resp.Error != nil {
		t.Errorf("the eval class broke with the Entra class armed: %+v", resp.Error)
	}
}

func TestA2AStaticTokenRigRefusesJWTsWithoutAnEntraClass(t *testing.T) {
	r := startA2ARig(t)
	if r.door.entra != nil {
		t.Fatal("the class is armed with no tenant")
	}
	if _, status := r.rawRPC(t, liveEntraToken(t), a2aTestCaller, a2aMethodSend, sendParams("x", "m-1", "", false)); status != http.StatusUnauthorized {
		t.Errorf("HTTP %d, want 401", status)
	}
}

// TestA2ADoorDispatchesBetweenTheGoogleAndEntraClasses: with both armed, a
// bearer shaped like a Google access token goes to Google and one shaped
// like a JWT to the tenant's keys, and neither class sees the other's
// bearer.
func TestA2ADoorDispatchesBetweenTheGoogleAndEntraClasses(t *testing.T) {
	tokeninfo := newFakeTokeninfo(t)
	tokeninfo.set(googleTestToken, liveTokeninfo(googleTestClientID, googleTestEmail))
	tenant := newFakeTenant(t)
	door, err := NewA2ADoor("127.0.0.1:0", a2aTestToken, A2ADoorOptions{
		GoogleClientID: googleTestClientID, GoogleAllowedUsers: []string{googleTestEmail},
		EntraTenantID: entraTestTenant, EntraClientID: entraTestClientID, EntraAllowedUsers: []string{entraTestObjectID},
		Logger: slog.New(slog.NewTextHandler(os.Stderr, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	door.google.tokeninfoURL = tokeninfo.srv.URL
	door.entra.keysURL = tenant.srv.URL
	identify := func(token string) (string, int) {
		w := httptest.NewRecorder()
		r := httptest.NewRequest(http.MethodPost, a2aRPCPath, nil)
		r.Header.Set(authorizationHeader, "Bearer "+token)
		verified, _ := door.identify(w, r)
		return verified, w.Code
	}
	if verified, _ := identify(googleTestToken); verified != a2aGoogleCaller(googleTestEmail) {
		t.Errorf("a Google token verified as %q", verified)
	}
	if verified, _ := identify(liveEntraToken(t)); verified != a2aEntraCaller(entraTestObjectID) {
		t.Errorf("an Entra token verified as %q", verified)
	}
	if verified, _ := identify(a2aTestToken); verified != "" {
		t.Errorf("the static token verified as %q", verified)
	}
	if tokeninfo.calls.Load() != 1 || tenant.fetches.Load() != 1 {
		t.Errorf("tokeninfo was asked %d times and the tenant %d, want one each", tokeninfo.calls.Load(), tenant.fetches.Load())
	}
	if _, code := identify("neither-kind"); code != http.StatusUnauthorized {
		t.Errorf("a junk bearer with both classes armed: HTTP %d, want 401", code)
	}
	if tokeninfo.calls.Load() != 1 {
		t.Error("a junk bearer was sent to Google")
	}
}

func TestA2ACardListsEntraSignInOnlyWhenArmed(t *testing.T) {
	plain := (&A2ADoor{}).card("http://x")
	if _, ok := plain.SecuritySchemes[a2aEntraSchemeName]; ok || len(plain.Security) != 1 {
		t.Errorf("unarmed card: schemes %v security %v", plain.SecuritySchemes, plain.Security)
	}
	armed := (&A2ADoor{entra: newEntraTokenVerifier(entraTestTenant, entraTestClientID)}).card("http://x")
	scheme, ok := armed.SecuritySchemes[a2aEntraSchemeName]
	want := "https://login.microsoftonline.com/" + entraTestTenant + "/v2.0/.well-known/openid-configuration"
	if !ok || scheme.Type != "openIdConnect" || scheme.OpenIDConnectURL != want {
		t.Errorf("armed card scheme = %+v, want openIdConnect at %s", scheme, want)
	}
	if len(armed.Security) != 2 {
		t.Errorf("armed card security = %v, want the two schemes as alternatives", armed.Security)
	}
	both := (&A2ADoor{google: newGoogleTokenVerifier(googleTestClientID), entra: newEntraTokenVerifier(entraTestTenant, entraTestClientID)}).card("http://x")
	if len(both.Security) != 3 || len(both.SecuritySchemes) != 3 {
		t.Errorf("both classes armed: security %v schemes %v, want three alternatives", both.Security, both.SecuritySchemes)
	}
}

func TestA2AEntraResolverRequiresTheClassPrefix(t *testing.T) {
	g := &Gateway{a2aEntraAllowed: map[string]bool{entraTestObjectID: true}}
	if got := g.resolveA2AEntraPrincipal(a2aEntraCaller(entraTestObjectID)); got != entraTestObjectID {
		t.Errorf("prefixed allowed caller resolved to %q", got)
	}
	for _, id := range []string{entraTestObjectID, "entra:" + entraTestObjectID, a2aEntraCallerPrefix, a2aGoogleCaller(entraTestObjectID)} {
		if got := g.resolveA2AEntraPrincipal(id); got != "" {
			t.Errorf("%q resolved to %q, want nothing", id, got)
		}
	}
	upper := strings.ToUpper(entraTestObjectID)
	if got := g.resolveA2AEntraPrincipal(a2aEntraCaller(upper)); got != upper {
		t.Errorf("an upper-case allowed object id resolved to %q, want it case-preserved", got)
	}
	if got := (&Gateway{}).resolveA2AEntraPrincipal(a2aEntraCaller(entraTestObjectID)); got != "" {
		t.Errorf("an empty allowlist admitted %q", got)
	}
	if got := (&Gateway{a2aGoogleAllowed: map[string]bool{entraTestObjectID: true}}).resolveA2AEntraPrincipal(a2aEntraCaller(entraTestObjectID)); got != "" {
		t.Errorf("the Google allowlist admitted an Entra caller: %q", got)
	}
}

func TestA2AEntraConfigGuards(t *testing.T) {
	setBaseEnv(t)
	t.Setenv("A2A_ATTRIBUTION_SALT", "test-salt")
	t.Setenv("DISCORD_TOKEN", "x")
	t.Setenv("A2A_DOOR_LISTEN", "")
	t.Setenv(envA2ADoorEntraTenantID, entraTestTenant)
	t.Setenv(envA2ADoorEntraClientID, "")
	if _, err := FromEnv(); err == nil || !strings.Contains(err.Error(), envA2ADoorEntraClientID) {
		t.Fatalf("a tenant without a client id was accepted: %v", err)
	}
	t.Setenv(envA2ADoorEntraClientID, entraTestClientID)
	if _, err := FromEnv(); err == nil || !strings.Contains(err.Error(), "A2A_DOOR_LISTEN") {
		t.Fatalf("the class without the door was accepted: %v", err)
	}
	t.Setenv("A2A_DOOR_LISTEN", "127.0.0.1:9999")
	t.Setenv("A2A_DOOR_TOKEN", "t")
	t.Setenv(envA2ADoorEntraAllowedUsers, " "+entraTestObjectID+", ,"+entraTestOtherOID+" ")
	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.A2ADoorEntraTenantID != entraTestTenant || cfg.A2ADoorEntraClientID != entraTestClientID {
		t.Errorf("tenant %q client %q", cfg.A2ADoorEntraTenantID, cfg.A2ADoorEntraClientID)
	}
	if len(cfg.A2ADoorEntraAllowedUsers) != 2 || cfg.A2ADoorEntraAllowedUsers[0] != entraTestObjectID {
		t.Errorf("allowlist = %q", cfg.A2ADoorEntraAllowedUsers)
	}
	if entraVerifierFor("", "") != nil || entraVerifierFor(entraTestTenant, "") != nil {
		t.Error("a verifier was built with the class off")
	}
}

// TestA2AEntraRefusalsTellTheOperatorWhatToFix: the 403's log line names
// the object id the admin adds and the username that says who it is; the
// 401 names both kinds of bearer, since a stale static token lands here too.
func TestA2AEntraRefusalsTellTheOperatorWhatToFix(t *testing.T) {
	f := newFakeTenant(t)
	logs := &recordingHandler{}
	door, err := NewA2ADoor("127.0.0.1:0", a2aTestToken, A2ADoorOptions{
		EntraTenantID: entraTestTenant, EntraClientID: entraTestClientID, EntraAllowedUsers: []string{entraTestObjectID}, Logger: slog.New(logs),
	})
	if err != nil {
		t.Fatal(err)
	}
	door.entra.keysURL = f.srv.URL
	call := func(token string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		r := httptest.NewRequest(http.MethodPost, a2aRPCPath, nil)
		r.Header.Set(authorizationHeader, "Bearer "+token)
		door.identify(w, r)
		return w
	}
	w := call(otherEntraToken(t))
	if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), envA2ADoorEntraAllowedUsers) {
		t.Fatalf("off-list account: HTTP %d %q, want a 403 naming the list", w.Code, w.Body.String())
	}
	if !recordedAttr(logs, "objectID", entraTestOtherOID) || !recordedAttr(logs, "username", entraTestUsername) {
		t.Error("the 403's log line does not name the refused object id and username")
	}
	w = call("stale-static-token")
	if w.Code != http.StatusUnauthorized || !strings.Contains(w.Body.String(), "neither the A2A door's token nor a Microsoft Entra token") {
		t.Errorf("a stale static token: HTTP %d %q, want a 401 naming both kinds of bearer", w.Code, w.Body.String())
	}
	if w.Header().Get("WWW-Authenticate") == "" {
		t.Error("the 401 carries no WWW-Authenticate")
	}
}

// TestA2AEntraCallersDelegateOnlyUnderAListOfTheirOwn: the door's rule for
// delegation covers the Entra class as it covers the Google class.
func TestA2AEntraCallersDelegateOnlyUnderAListOfTheirOwn(t *testing.T) {
	g := &Gateway{targetAllowed: targetAllowed{}}
	if !g.doorUnlisted("platform", a2aEntraBackend) {
		t.Error("an Entra-verified door caller with no list may delegate; the door's rule says nobody")
	}
	g.targetAllowed = targetAllowed{"platform": {a2aEntraBackend: {"x": true}}}
	if g.doorUnlisted("platform", a2aEntraBackend) {
		t.Error("a list for the class did not lift the door's rule")
	}
}

// TestA2AEntraBackendIsADoorBackendEverywhere: every per-backend switch
// that treats the Google class as a door class treats the Entra class the
// same way.
func TestA2AEntraBackendIsADoorBackendEverywhere(t *testing.T) {
	if busyNoticeBackend(a2aEntraBackend) {
		t.Error("the Entra class gets the busy notice")
	}
	if cardStopPlaceholderOn(a2aEntraBackend) {
		t.Error("the Entra class gets the card-stop placeholder")
	}
	if !wakeToDoor(&TaskRef{Requester: &TaskRequester{Backend: a2aEntraBackend}}) {
		t.Error("a child of an Entra caller's task does not wake to the door")
	}
	if _, ok := metricsPlatformLabel(a2aEntraBackend); ok {
		t.Error("the Entra class has a chat platform label")
	}
	if got := verifiedByFor(a2aEntraBackend); got != a2aEntraVerifiedBy {
		t.Errorf("verifiedBy = %q", got)
	}
	if got := a2aBackendForCaller(a2aEntraCaller(entraTestObjectID)); got != a2aEntraBackend {
		t.Errorf("backend for an Entra caller = %q", got)
	}
}
