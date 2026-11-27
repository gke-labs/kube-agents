package jwtauth

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
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

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	testIssuer   = "https://issuer.test"
	testAudience = "kube-proxy"
	testSubject  = "actor/space-a/session-1"
	testKeyID    = "key-1"
	testRSABits  = 2048
	testTokenTTL = 5 * time.Minute
	testPodsPath = "/clusters/" + k8sapiproxy.InClusterID + "/api/v1/pods"
	contentJSON  = "application/json"
	// testWaitLimit bounds a wait for a goroutine; testSettle lets others reach a held fetch.
	testWaitLimit = 5 * time.Second
	testSettle    = 50 * time.Millisecond
)

var testNow = time.Date(2026, 11, 19, 12, 0, 0, 0, time.UTC)

// issuer signs tokens and serves its key set; served counts key set fetches.
type issuer struct {
	t      *testing.T
	mu     sync.Mutex
	keys   map[string]any // kid -> private key
	public jose.JSONWebKeySet
	extra  []json.RawMessage // published as they are, after the public keys
	down   atomic.Bool
	gate   atomic.Pointer[chan struct{}] // when set, a fetch waits for the channel to close
	served atomic.Int32
	srv    *httptest.Server
}

func newIssuer(t *testing.T) *issuer {
	t.Helper()
	is := &issuer{t: t, keys: map[string]any{}}
	is.srv = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		is.served.Add(1)
		if g := is.gate.Load(); g != nil {
			<-*g
		}
		if is.down.Load() {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		is.mu.Lock()
		defer is.mu.Unlock()
		keys := make([]json.RawMessage, 0, len(is.public.Keys)+len(is.extra))
		for _, k := range is.public.Keys {
			b, err := json.Marshal(k)
			if err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}
			keys = append(keys, b)
		}
		keys = append(keys, is.extra...)
		w.Header().Set("Content-Type", contentJSON)
		_ = json.NewEncoder(w).Encode(map[string][]json.RawMessage{"keys": keys})
	}))
	t.Cleanup(is.srv.Close)
	is.addRSA(testKeyID)
	return is
}

func (is *issuer) addRSA(kid string) {
	is.t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, testRSABits)
	if err != nil {
		is.t.Fatal(err)
	}
	is.add(kid, key, &key.PublicKey, jose.RS256)
}

func (is *issuer) addEC(kid string) {
	is.t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		is.t.Fatal(err)
	}
	is.add(kid, key, &key.PublicKey, jose.ES256)
}

func (is *issuer) add(kid string, private, public any, alg jose.SignatureAlgorithm) {
	is.mu.Lock()
	defer is.mu.Unlock()
	is.keys[kid] = private
	is.public.Keys = append(is.public.Keys, jose.JSONWebKey{Key: public, KeyID: kid, Algorithm: string(alg), Use: "sig"})
}

// publishRaw adds a key set entry exactly as given, such as one the library cannot parse.
func (is *issuer) publishRaw(entry string) {
	is.mu.Lock()
	defer is.mu.Unlock()
	is.extra = append(is.extra, json.RawMessage(entry))
}

// sign returns a token signed with kid's key and naming kid; mutate edits the claims first.
func (is *issuer) sign(kid string, alg jose.SignatureAlgorithm, mutate func(c *jwt.Claims, extra map[string]any)) string {
	is.t.Helper()
	return is.signWith(kid, true, alg, mutate)
}

func (is *issuer) signWith(kid string, nameKid bool, alg jose.SignatureAlgorithm, mutate func(c *jwt.Claims, extra map[string]any)) string {
	is.t.Helper()
	is.mu.Lock()
	key := is.keys[kid]
	is.mu.Unlock()
	opts := (&jose.SignerOptions{}).WithType("JWT")
	if nameKid {
		opts = opts.WithHeader(jose.HeaderKey("kid"), kid)
	}
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: alg, Key: key}, opts)
	if err != nil {
		is.t.Fatal(err)
	}
	c := jwt.Claims{
		Issuer:   testIssuer,
		Subject:  testSubject,
		Audience: jwt.Audience{testAudience},
		IssuedAt: jwt.NewNumericDate(testNow),
		Expiry:   jwt.NewNumericDate(testNow.Add(testTokenTTL)),
	}
	extra := map[string]any{}
	if mutate != nil {
		mutate(&c, extra)
	}
	raw, err := jwt.Signed(signer).Claims(c).Claims(extra).Serialize()
	if err != nil {
		is.t.Fatal(err)
	}
	return raw
}

// signWithKey signs a default token with a key the issuer holds outside its kid map.
func (is *issuer) signWithKey(key any, kid string, alg jose.SignatureAlgorithm) string {
	is.t.Helper()
	return is.signWithKeyAt(key, kid, alg, testNow)
}

func (is *issuer) signWithKeyAt(key any, kid string, alg jose.SignatureAlgorithm, at time.Time) string {
	is.t.Helper()
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: alg, Key: key},
		(&jose.SignerOptions{}).WithType("JWT").WithHeader(jose.HeaderKey("kid"), kid))
	if err != nil {
		is.t.Fatal(err)
	}
	raw, err := jwt.Signed(signer).Claims(jwt.Claims{
		Issuer: testIssuer, Subject: testSubject, Audience: jwt.Audience{testAudience},
		IssuedAt: jwt.NewNumericDate(at), Expiry: jwt.NewNumericDate(at.Add(testTokenTTL)),
	}).Serialize()
	if err != nil {
		is.t.Fatal(err)
	}
	return raw
}

func (is *issuer) config() Config {
	return Config{
		Issuer:     testIssuer,
		KeySetURL:  is.srv.URL,
		Audiences:  []string{testAudience},
		HTTPClient: is.srv.Client(),
		Now:        func() time.Time { return testNow },
	}
}

func newAuth(t *testing.T, cfg Config) k8sapiproxy.CallerAuthenticator {
	t.Helper()
	a, err := NewAuthenticator(cfg)
	if err != nil {
		t.Fatalf("NewAuthenticator: %v", err)
	}
	return a
}

func bearer(token string) *http.Request {
	r := httptest.NewRequest(http.MethodGet, testPodsPath, nil)
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	return r
}

func TestNewAuthenticatorValidatesConfig(t *testing.T) {
	ok := Config{Issuer: testIssuer, KeySetURL: "https://issuer.test/keys", Audiences: []string{testAudience}}
	if _, err := NewAuthenticator(ok); err != nil {
		t.Fatalf("valid config: %v", err)
	}
	for name, mutate := range map[string]func(*Config){
		"no issuer": func(c *Config) { c.Issuer = "" },
		"no key set URL and an issuer that is not a URL": func(c *Config) { c.Issuer, c.KeySetURL = "issuer-name", "" },
		"no key set URL and an http issuer":              func(c *Config) { c.Issuer, c.KeySetURL = "http://issuer.test", "" },
		"no key set URL and an issuer with a query":      func(c *Config) { c.Issuer, c.KeySetURL = "https://issuer.test?tenant=a", "" },
		"no key set URL and an issuer with a bare ?":     func(c *Config) { c.Issuer, c.KeySetURL = "https://issuer.test/a?", "" },
		"no key set URL and an issuer with a fragment":   func(c *Config) { c.Issuer, c.KeySetURL = "https://issuer.test/a#keys", "" },
		"no key set URL and an issuer with a bare #":     func(c *Config) { c.Issuer, c.KeySetURL = "https://issuer.test/a#", "" },
		"allowed partitions with only empty entries":     func(c *Config) { c.PartitionClaim, c.AllowedPartitions = "tenant", []string{"", "  "} },
		"one empty allowed partition":                    func(c *Config) { c.AllowedPartitions = []string{""} },
		"empty key in a claim path":                      func(c *Config) { c.UsernameClaim = "a..b" },
		"claim path ending in a separator":               func(c *Config) { c.PartitionClaim = "a." },
		"allowed partitions without a partition claim":   func(c *Config) { c.AllowedPartitions = []string{"team-a"} },
		"non-http URL":   func(c *Config) { c.KeySetURL = "file:///etc/keys" },
		"no audience":    func(c *Config) { c.Audiences = nil },
		"blank audience": func(c *Config) { c.Audiences = []string{" "} },
		"symmetric alg":  func(c *Config) { c.SignatureAlgorithms = []jose.SignatureAlgorithm{jose.HS256} },
	} {
		t.Run(name, func(t *testing.T) {
			cfg := ok
			mutate(&cfg)
			if _, err := NewAuthenticator(cfg); err == nil {
				t.Error("NewAuthenticator accepted an invalid config")
			}
		})
	}
}

func TestAuthenticatorAcceptsAValidToken(t *testing.T) {
	is := newIssuer(t)
	is.addEC("ec-1")
	cfg := is.config()
	cfg.GroupsClaim = "groups"
	a := newAuth(t, cfg)

	for _, tc := range []struct {
		name string
		kid  string
		alg  jose.SignatureAlgorithm
	}{
		{"RS256", testKeyID, jose.RS256},
		{"ES256", "ec-1", jose.ES256},
	} {
		t.Run(tc.name, func(t *testing.T) {
			token := is.sign(tc.kid, tc.alg, func(_ *jwt.Claims, extra map[string]any) {
				extra["groups"] = []string{"tenant:space-a", "agents"}
			})
			id, err := a.AuthenticateRequest(bearer(token))
			if err != nil {
				t.Fatalf("AuthenticateRequest: %v", err)
			}
			if id.Username != testSubject || id.UID != testSubject {
				t.Errorf("identity = %+v, want username and UID %q", id, testSubject)
			}
			if strings.Join(id.Groups, ",") != "tenant:space-a,agents" {
				t.Errorf("groups = %v", id.Groups)
			}
		})
	}
}

func TestAuthenticatorRefusesAnInvalidToken(t *testing.T) {
	is := newIssuer(t)
	other := newIssuer(t) // a key the issuer never published, under the same kid
	a := newAuth(t, is.config())

	for _, tc := range []struct {
		name  string
		token string
	}{
		{"no token", ""},
		{"not a JWT", "not-a-jwt"},
		{"wrong issuer", is.sign(testKeyID, jose.RS256, func(c *jwt.Claims, _ map[string]any) { c.Issuer = "https://other.test" })},
		{"wrong audience", is.sign(testKeyID, jose.RS256, func(c *jwt.Claims, _ map[string]any) { c.Audience = jwt.Audience{"kube-apiserver"} })},
		{"expired", is.sign(testKeyID, jose.RS256, func(c *jwt.Claims, _ map[string]any) {
			c.Expiry = jwt.NewNumericDate(testNow.Add(-2 * clockLeeway))
		})},
		{"not yet valid", is.sign(testKeyID, jose.RS256, func(c *jwt.Claims, _ map[string]any) {
			c.NotBefore = jwt.NewNumericDate(testNow.Add(2 * clockLeeway))
		})},
		{"no expiry", is.sign(testKeyID, jose.RS256, func(c *jwt.Claims, _ map[string]any) { c.Expiry = nil })},
		{"no kid", is.signWith(testKeyID, false, jose.RS256, nil)},
		{"signed by another key", other.sign(testKeyID, jose.RS256, nil)},
		{"no username claim", is.sign(testKeyID, jose.RS256, func(c *jwt.Claims, _ map[string]any) { c.Subject = "" })},
	} {
		t.Run(tc.name, func(t *testing.T) {
			id, err := a.AuthenticateRequest(bearer(tc.token))
			if err == nil {
				t.Fatalf("accepted as %+v", id)
			}
			if errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
				t.Errorf("err = %v, want a refusal, not unavailable", err)
			}
		})
	}
}

// An issuer that also publishes a symmetric (oct) key cannot be used for an HMAC token
// signed with it: HS256 is never an accepted algorithm, and symmetric keys are dropped from
// the key set.
func TestAuthenticatorRefusesAnHMACToken(t *testing.T) {
	is := newIssuer(t)
	secret := []byte("shared-secret-of-sufficient-length!")
	is.publishRaw(`{"kty":"oct","kid":"hmac","k":"` + base64.RawURLEncoding.EncodeToString(secret) + `"}`)
	a := newAuth(t, is.config())
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.HS256, Key: secret},
		(&jose.SignerOptions{}).WithHeader(jose.HeaderKey("kid"), "hmac"))
	if err != nil {
		t.Fatal(err)
	}
	token, err := jwt.Signed(signer).Claims(jwt.Claims{
		Issuer: testIssuer, Subject: testSubject, Audience: jwt.Audience{testAudience},
		Expiry: jwt.NewNumericDate(testNow.Add(testTokenTTL)),
	}).Serialize()
	if err != nil {
		t.Fatal(err)
	}
	if id, err := a.AuthenticateRequest(bearer(token)); err == nil {
		t.Fatalf("HS256 token accepted as %+v", id)
	}
}

// A key set entry the library cannot parse, or one meant for encryption, is skipped; the
// rest of the set still verifies tokens.
func TestAuthenticatorSkipsUnusableKeys(t *testing.T) {
	is := newIssuer(t)
	is.publishRaw(`{"kty":"AKP","kid":"pq-1","alg":"ML-DSA-44","pub":"AAAA"}`)
	is.publishRaw(`{"kty":"OKP","crv":"X25519","kid":"enc-1","x":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}`)
	a := newAuth(t, is.config())
	if _, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil))); err != nil {
		t.Fatalf("valid token with unusable keys in the set: %v", err)
	}
}

// Among keys published under one kid, the one fit for the token's algorithm verifies it:
// an ECDSA key listed first under the same kid is passed over for an RS256 token, and the
// RSA key under it is passed over for an ES256 one.
func TestAuthenticatorPicksTheSigningKeyUnderASharedKid(t *testing.T) {
	is := newIssuer(t)
	ec, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	is.mu.Lock()
	is.keys["ec-shared"] = ec
	is.public.Keys = append([]jose.JSONWebKey{{Key: &ec.PublicKey, KeyID: testKeyID, Algorithm: string(jose.ES256), Use: "sig"}}, is.public.Keys...)
	is.mu.Unlock()
	a := newAuth(t, is.config())

	if _, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil))); err != nil {
		t.Errorf("RS256 token under a kid whose first key is ECDSA: %v", err)
	}
	ecToken := is.signWithKey(ec, testKeyID, jose.ES256)
	if _, err := a.AuthenticateRequest(bearer(ecToken)); err != nil {
		t.Errorf("ES256 token under a kid whose other key is RSA: %v", err)
	}
}

// A key of a new type published under a kid the cache already holds is fetched: the cache
// counts only keys that can verify the token's algorithm.
func TestAuthenticatorFetchesANewKeyTypeUnderAKnownKid(t *testing.T) {
	is := newIssuer(t)
	clock := testNow
	cfg := is.config()
	cfg.Now = func() time.Time { return clock }
	a := newAuth(t, cfg)
	if _, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil))); err != nil {
		t.Fatalf("RSA key: %v", err)
	}

	ec, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	is.mu.Lock()
	is.public.Keys = append(is.public.Keys, jose.JSONWebKey{Key: &ec.PublicKey, KeyID: testKeyID, Algorithm: string(jose.ES256), Use: "sig"})
	is.mu.Unlock()
	clock = clock.Add(minKeySetFetchInterval)
	if _, err := a.AuthenticateRequest(bearer(is.signWithKeyAt(ec, testKeyID, jose.ES256, clock))); err != nil {
		t.Errorf("ES256 key newly published under a cached kid: %v", err)
	}
}

// Requests that arrive while the first key set fetch runs wait for it rather than being
// answered unavailable, and they share it.
func TestAuthenticatorRequestsDuringAFetchShareIt(t *testing.T) {
	is := newIssuer(t)
	release := make(chan struct{})
	is.gate.Store(&release)
	a := newAuth(t, is.config())
	token := is.sign(testKeyID, jose.RS256, nil)

	const callers = 5
	errs := make(chan error, callers)
	for range callers {
		go func() {
			_, err := a.AuthenticateRequest(bearer(token))
			errs <- err
		}()
	}
	deadline := time.Now().Add(testWaitLimit)
	for is.served.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	// Give the other callers time to reach the shared fetch while it is held open.
	time.Sleep(testSettle)
	close(release)
	for range callers {
		if err := <-errs; err != nil {
			t.Errorf("caller during the fetch: %v", err)
		}
	}
	if got := is.served.Load(); got != 1 {
		t.Errorf("key set fetched %d times, want 1 shared fetch", got)
	}
}

func TestAuthenticatorUsesConfiguredClaims(t *testing.T) {
	is := newIssuer(t)
	cfg := is.config()
	cfg.UsernameClaim = "actor"
	cfg.GroupsClaim = "tenant"
	a := newAuth(t, cfg)

	id, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, func(_ *jwt.Claims, extra map[string]any) {
		extra["actor"] = "session-1"
		extra["tenant"] = "space-a"
	})))
	if err != nil {
		t.Fatalf("AuthenticateRequest: %v", err)
	}
	if id.Username != "session-1" || len(id.Groups) != 1 || id.Groups[0] != "space-a" {
		t.Errorf("identity = %+v, want username session-1 and group space-a", id)
	}

	if _, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, func(_ *jwt.Claims, extra map[string]any) {
		extra["actor"] = "session-1"
		extra["tenant"] = 7
	}))); err == nil {
		t.Error("a groups claim that is not a string or list of strings was accepted")
	}
}

func TestAuthenticatorAllowDecides(t *testing.T) {
	is := newIssuer(t)
	cfg := is.config()
	cfg.Allow = func(id *k8sapiproxy.CallerIdentity) error {
		if !strings.HasPrefix(id.Username, "actor/space-a/") {
			return errors.New("not in space-a")
		}
		return nil
	}
	a := newAuth(t, cfg)

	if _, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil))); err != nil {
		t.Errorf("allowed caller refused: %v", err)
	}
	if _, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, func(c *jwt.Claims, _ map[string]any) {
		c.Subject = "actor/space-b/session-9"
	}))); err == nil {
		t.Error("caller Allow refused was accepted")
	}
}

// A key the issuer rotates in is picked up on the first token that names it, once the
// refetch interval has passed since the last fetch.
func TestAuthenticatorFetchesARotatedKey(t *testing.T) {
	is := newIssuer(t)
	clock := testNow
	cfg := is.config()
	cfg.Now = func() time.Time { return clock }
	a := newAuth(t, cfg)
	if _, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil))); err != nil {
		t.Fatalf("first key: %v", err)
	}

	is.addEC("key-2")
	clock = clock.Add(minKeySetFetchInterval)
	if _, err := a.AuthenticateRequest(bearer(is.sign("key-2", jose.ES256, func(c *jwt.Claims, _ map[string]any) {
		c.IssuedAt = jwt.NewNumericDate(clock)
	}))); err != nil {
		t.Errorf("rotated key: %v", err)
	}
	if got := is.served.Load(); got != 2 {
		t.Errorf("key set fetched %d times, want 2 (initial and rotation)", got)
	}
}

// Tokens naming unknown keys cannot make the authenticator fetch the key set more than once
// per minKeySetFetchInterval.
func TestAuthenticatorBoundsKeySetFetches(t *testing.T) {
	is := newIssuer(t)
	a := newAuth(t, is.config())
	if _, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil))); err != nil {
		t.Fatal(err)
	}
	before := is.served.Load()
	other := newIssuer(t)
	for i := range 20 {
		kid := "unknown-" + string(rune('a'+i))
		other.addEC(kid)
		_, _ = a.AuthenticateRequest(bearer(other.sign(kid, jose.ES256, nil)))
	}
	if got := is.served.Load() - before; got > 1 {
		t.Errorf("key set fetched %d times for 20 unknown-key tokens within the interval, want at most 1", got)
	}
}

// When the key set cannot be fetched and no cached key matches, the authenticator cannot
// decide: ErrAuthenticationUnavailable, so the handler answers 503. A known key keeps working.
func TestAuthenticatorReportsAnUnreachableIssuer(t *testing.T) {
	is := newIssuer(t)
	is.down.Store(true)
	a := newAuth(t, is.config())
	_, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil)))
	if !errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
		t.Fatalf("err = %v, want ErrAuthenticationUnavailable", err)
	}

	is.down.Store(false)
	clock := testNow
	cfg := is.config()
	cfg.KeySetTTL = time.Minute
	cfg.Now = func() time.Time { return clock }
	a = newAuth(t, cfg)
	if _, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil))); err != nil {
		t.Fatalf("issuer up: %v", err)
	}
	is.down.Store(true)
	clock = clock.Add(2 * time.Minute) // past the TTL, so the next token triggers a fetch
	if _, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, func(c *jwt.Claims, _ map[string]any) {
		c.IssuedAt = jwt.NewNumericDate(clock)
		c.Expiry = jwt.NewNumericDate(clock.Add(testTokenTTL))
	}))); err != nil {
		t.Errorf("known key with the issuer down: %v", err)
	}
}

// With the issuer down and nothing cached, a burst of tokens makes one fetch, not one each:
// the rest are answered unavailable until the fetch interval passes.
func TestAuthenticatorBoundsFetchesWhileTheIssuerIsDown(t *testing.T) {
	is := newIssuer(t)
	is.down.Store(true)
	a := newAuth(t, is.config())
	for range 20 {
		_, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil)))
		if !errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
			t.Fatalf("err = %v, want ErrAuthenticationUnavailable", err)
		}
	}
	if got := is.served.Load(); got != 1 {
		t.Errorf("key set fetched %d times for 20 tokens with the issuer down, want 1", got)
	}
}

// A redirect off https is refused, so the key set cannot be fetched from a plain-text URL.
func TestAuthenticatorRefusesAnInsecureRedirect(t *testing.T) {
	is := newIssuer(t)
	redirect := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://"+strings.TrimPrefix(is.srv.URL, "https://"), http.StatusFound)
	}))
	t.Cleanup(redirect.Close)
	cfg := is.config()
	cfg.KeySetURL = redirect.URL
	cfg.HTTPClient = redirect.Client()
	a := newAuth(t, cfg)
	_, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil)))
	if !errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
		t.Errorf("err = %v, want ErrAuthenticationUnavailable after a redirect to http", err)
	}
	if got := is.served.Load(); got != 0 {
		t.Errorf("issuer served %d plain-text fetches, want 0", got)
	}
}

// An https redirect loop stops at the redirect limit instead of running until the fetch
// timeout.
func TestAuthenticatorStopsARedirectLoop(t *testing.T) {
	is := newIssuer(t)
	var hops atomic.Int32
	var loop *httptest.Server
	loop = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hops.Add(1)
		http.Redirect(w, r, loop.URL, http.StatusFound)
	}))
	t.Cleanup(loop.Close)
	cfg := is.config()
	cfg.KeySetURL = loop.URL
	cfg.HTTPClient = loop.Client()
	a := newAuth(t, cfg)
	_, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil)))
	if !errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
		t.Errorf("err = %v, want ErrAuthenticationUnavailable", err)
	}
	if got := hops.Load(); got > maxKeySetRedirects+1 {
		t.Errorf("followed %d hops, want at most %d", got, maxKeySetRedirects+1)
	}
}

// A known key keeps verifying while the issuer is down, but not beyond maxStaleKeySet past
// the set's TTL, so a key the issuer has removed stops working within a bounded time.
func TestAuthenticatorBoundsAStaleKeySet(t *testing.T) {
	is := newIssuer(t)
	clock := testNow
	cfg := is.config()
	cfg.KeySetTTL = time.Minute
	cfg.Now = func() time.Time { return clock }
	a := newAuth(t, cfg)
	tokenAt := func(at time.Time) string {
		return is.sign(testKeyID, jose.RS256, func(c *jwt.Claims, _ map[string]any) {
			c.IssuedAt = jwt.NewNumericDate(at)
			c.Expiry = jwt.NewNumericDate(at.Add(testTokenTTL))
		})
	}
	if _, err := a.AuthenticateRequest(bearer(tokenAt(clock))); err != nil {
		t.Fatalf("issuer up: %v", err)
	}
	is.down.Store(true)
	clock = clock.Add(time.Minute + maxStaleKeySet - time.Minute)
	if _, err := a.AuthenticateRequest(bearer(tokenAt(clock))); err != nil {
		t.Errorf("known key within the stale bound: %v", err)
	}
	clock = clock.Add(2 * time.Minute)
	_, err := a.AuthenticateRequest(bearer(tokenAt(clock)))
	if !errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
		t.Errorf("past the stale bound: err = %v, want ErrAuthenticationUnavailable", err)
	}
}

// recordingUpstream answers every forwarded request with an empty PodList.
type recordingUpstream struct{ calls atomic.Int32 }

func (u *recordingUpstream) RoundTripCluster(_ context.Context, _ string, req *http.Request) (*http.Response, error) {
	u.calls.Add(1)
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{contentJSON}},
		Body:       io.NopCloser(strings.NewReader(`{"kind":"PodList","apiVersion":"v1","items":[]}`)),
		Request:    req,
	}, nil
}

// Through the handler: a valid token is forwarded, a bad one is 401, and an unreachable
// issuer is 503.
func TestHandlerWithJWTAuthenticator(t *testing.T) {
	is := newIssuer(t)
	upstream := &recordingUpstream{}
	h, err := k8sapiproxy.NewHandler(k8sapiproxy.Config{
		Authenticator: newAuth(t, is.config()),
		Upstream:      upstream,
		Policy:        k8sapiproxy.Policy{Mode: k8sapiproxy.ModeReadOnlyWithDryRun, RedactSecrets: true},
		Logger:        slog.New(slog.DiscardHandler),
	})
	if err != nil {
		t.Fatal(err)
	}
	serve := func(r *http.Request) int {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, r)
		return rec.Code
	}

	if code := serve(bearer(is.sign(testKeyID, jose.RS256, nil))); code != http.StatusOK {
		t.Errorf("valid token: code %d, want 200", code)
	}
	if code := serve(bearer(is.sign(testKeyID, jose.RS256, func(c *jwt.Claims, _ map[string]any) { c.Audience = jwt.Audience{"other"} }))); code != http.StatusUnauthorized {
		t.Errorf("wrong audience: code %d, want 401", code)
	}

	down := newIssuer(t)
	down.down.Store(true)
	h, err = k8sapiproxy.NewHandler(k8sapiproxy.Config{
		Authenticator: newAuth(t, down.config()),
		Upstream:      upstream,
		Policy:        k8sapiproxy.Policy{Mode: k8sapiproxy.ModeReadOnlyWithDryRun, RedactSecrets: true},
		Logger:        slog.New(slog.DiscardHandler),
	})
	if err != nil {
		t.Fatal(err)
	}
	if code := serve(bearer(down.sign(testKeyID, jose.RS256, nil))); code != http.StatusServiceUnavailable {
		t.Errorf("unreachable issuer: code %d, want 503", code)
	}
	if got := upstream.calls.Load(); got != 1 {
		t.Errorf("upstream calls = %d, want 1 (only the valid token)", got)
	}
}

// writeCertPEM writes cert as a PEM bundle in a temporary directory and returns its path.
func writeCertPEM(t *testing.T, cert *x509.Certificate) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "ca.crt")
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw}), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// unrelatedCA is a self-signed CA that signed nothing the tests serve.
func unrelatedCA(t *testing.T) *x509.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "unrelated-ca"},
		NotBefore:             testNow.Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return cert
}

// With CAFile, the key set's server is verified against that bundle rather than the
// system roots: the issuer's CA lets the fetch through, and any other CA refuses it.
func TestAuthenticatorVerifiesTheKeySetServerAgainstCAFile(t *testing.T) {
	is := newIssuer(t)
	cfg := is.config()
	cfg.HTTPClient = nil
	cfg.CAFile = writeCertPEM(t, is.srv.Certificate())
	if _, err := newAuth(t, cfg).AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil))); err != nil {
		t.Fatalf("key set server signed by CAFile: %v", err)
	}

	cfg.CAFile = writeCertPEM(t, unrelatedCA(t))
	_, err := newAuth(t, cfg).AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil)))
	if !errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
		t.Errorf("key set server not signed by CAFile: err = %v, want ErrAuthenticationUnavailable", err)
	}
}

func TestNewAuthenticatorValidatesCAFile(t *testing.T) {
	is := newIssuer(t)
	cfg := is.config()
	cfg.CAFile = writeCertPEM(t, is.srv.Certificate())
	if _, err := NewAuthenticator(cfg); err == nil {
		t.Error("NewAuthenticator accepted CAFile with an HTTPClient that sets its own Transport")
	}

	cfg.HTTPClient = &http.Client{Timeout: testWaitLimit}
	if _, err := NewAuthenticator(cfg); err != nil {
		t.Errorf("CAFile with an HTTPClient without a Transport: %v", err)
	}

	empty := filepath.Join(t.TempDir(), "empty.crt")
	if err := os.WriteFile(empty, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	for name, path := range map[string]string{
		"missing": filepath.Join(t.TempDir(), "absent.crt"),
		"empty":   empty,
	} {
		t.Run(name, func(t *testing.T) {
			cfg := is.config()
			cfg.HTTPClient = nil
			cfg.CAFile = path
			if _, err := NewAuthenticator(cfg); err == nil {
				t.Error("NewAuthenticator accepted an unusable CAFile")
			}
		})
	}
}
