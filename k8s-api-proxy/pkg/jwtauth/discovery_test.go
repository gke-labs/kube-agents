package jwtauth

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	testKeySetPath = "/keys"
	testBurst      = 20
	testMovedKeyID = "key-2"
	// testLongIssuerBytes is far over the bound on a quoted issuer and under the metadata cap.
	testLongIssuerBytes = 64 << 10
)

// discovery serves issuer metadata for an issuer whose keys is serves from another host.
// Its own URL is the issuer; the metadata's issuer and jwks_uri can be overridden.
type discovery struct {
	srv       *httptest.Server
	served    atomic.Int32
	down      atomic.Bool
	issuer    atomic.Pointer[string]
	keySetURL atomic.Pointer[string]
}

func newDiscovery(t *testing.T, is *issuer) *discovery {
	t.Helper()
	d := &discovery{}
	d.srv = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != discoveryPath {
			http.NotFound(w, r)
			return
		}
		d.served.Add(1)
		if d.down.Load() {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		metadata := map[string]string{"issuer": d.srv.URL, "jwks_uri": is.srv.URL + testKeySetPath}
		if v := d.issuer.Load(); v != nil {
			metadata["issuer"] = *v
		}
		if v := d.keySetURL.Load(); v != nil {
			metadata["jwks_uri"] = *v
		}
		w.Header().Set("Content-Type", contentJSON)
		_ = json.NewEncoder(w).Encode(metadata)
	}))
	t.Cleanup(d.srv.Close)
	return d
}

// config has no KeySetURL: the authenticator must discover it from the issuer d is.
func (d *discovery) config(is *issuer) Config {
	cfg := is.config()
	cfg.Issuer = d.srv.URL
	cfg.KeySetURL = ""
	return cfg
}

// signFrom returns a token from issuerName, signed with is's key.
func signFrom(is *issuer, issuerName string) string {
	return is.sign(testKeyID, jose.RS256, func(c *jwt.Claims, _ map[string]any) { c.Issuer = issuerName })
}

func TestAuthenticatorDiscoversTheKeySet(t *testing.T) {
	is := newIssuer(t)
	d := newDiscovery(t, is)
	a := newAuth(t, d.config(is))

	for range 3 {
		id, err := a.AuthenticateRequest(bearer(signFrom(is, d.srv.URL)))
		if err != nil {
			t.Fatalf("AuthenticateRequest: %v", err)
		}
		if id.Username != testSubject {
			t.Errorf("username = %q, want %q", id.Username, testSubject)
		}
	}
	if got := d.served.Load(); got != 1 {
		t.Errorf("issuer metadata fetched %d times for 3 tokens, want 1", got)
	}
	if got := is.served.Load(); got != 1 {
		t.Errorf("key set fetched %d times for 3 tokens, want 1", got)
	}

	// The discovered keys are still only for the configured issuer.
	if id, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil))); err == nil {
		t.Errorf("token from another issuer accepted as %+v", *id)
	}
}

// An issuer written with a trailing slash is discovered at the same well-known path, and
// its metadata must name it with the slash.
func TestAuthenticatorDiscoversForAnIssuerWithATrailingSlash(t *testing.T) {
	is := newIssuer(t)
	d := newDiscovery(t, is)
	name := d.srv.URL + "/"
	d.issuer.Store(&name)
	cfg := d.config(is)
	cfg.Issuer = name
	if _, err := newAuth(t, cfg).AuthenticateRequest(bearer(signFrom(is, name))); err != nil {
		t.Fatalf("AuthenticateRequest: %v", err)
	}
}

// Metadata that names another issuer, or a key set that is not on https, is never used:
// the proxy cannot decide (503) and fetches no keys.
func TestAuthenticatorRefusesUnusableIssuerMetadata(t *testing.T) {
	for _, tc := range []struct {
		name  string
		alter func(d *discovery, is *issuer)
	}{
		{"issuer mismatch", func(d *discovery, _ *issuer) {
			other := "https://other-issuer.test"
			d.issuer.Store(&other)
		}},
		{"issuer differing by a trailing slash", func(d *discovery, _ *issuer) {
			other := d.srv.URL + "/"
			d.issuer.Store(&other)
		}},
		{"no issuer", func(d *discovery, _ *issuer) {
			other := ""
			d.issuer.Store(&other)
		}},
		{"http jwks_uri", func(d *discovery, is *issuer) {
			plain := "http://" + is.srv.Listener.Addr().String() + testKeySetPath
			d.keySetURL.Store(&plain)
		}},
		{"no jwks_uri", func(d *discovery, _ *issuer) {
			none := ""
			d.keySetURL.Store(&none)
		}},
		{"jwks_uri without a host", func(d *discovery, _ *issuer) {
			relative := testKeySetPath
			d.keySetURL.Store(&relative)
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			is := newIssuer(t)
			d := newDiscovery(t, is)
			tc.alter(d, is)
			a := newAuth(t, d.config(is))
			_, err := a.AuthenticateRequest(bearer(signFrom(is, d.srv.URL)))
			if !errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
				t.Fatalf("err = %v, want ErrAuthenticationUnavailable", err)
			}
			if got := is.served.Load(); got != 0 {
				t.Errorf("key set fetched %d times from unusable metadata, want 0", got)
			}
		})
	}
}

// Metadata naming a very long issuer is refused with an error that quotes only its start,
// since the error is repeated in every audit record until the next fetch.
func TestAuthenticatorBoundsTheIssuerQuotedInAnError(t *testing.T) {
	is := newIssuer(t)
	d := newDiscovery(t, is)
	long := "https://" + strings.Repeat("a", testLongIssuerBytes) + ".test"
	d.issuer.Store(&long)
	_, err := newAuth(t, d.config(is)).AuthenticateRequest(bearer(signFrom(is, d.srv.URL)))
	if !errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
		t.Fatalf("err = %v, want ErrAuthenticationUnavailable", err)
	}
	if strings.Contains(err.Error(), long) {
		t.Error("the error quotes the whole issuer the metadata named")
	}
	if !strings.Contains(err.Error(), long[:maxQuotedIssuerBytes]) || strings.Contains(err.Error(), long[:maxQuotedIssuerBytes+1]) {
		t.Errorf("the error does not quote exactly the first %d bytes of the named issuer", maxQuotedIssuerBytes)
	}
	if got, limit := len(err.Error()), testLongIssuerBytes; got >= limit {
		t.Errorf("the error is %d bytes, want under %d", got, limit)
	}
}

// While the metadata cannot be fetched every token is answered unavailable (503 through
// the handler), a burst makes one fetch, and discovery recovers once the interval passed.
func TestAuthenticatorReportsADiscoveryOutage(t *testing.T) {
	is := newIssuer(t)
	d := newDiscovery(t, is)
	d.down.Store(true)
	clock := testNow
	cfg := d.config(is)
	cfg.Now = func() time.Time { return clock }
	a := newAuth(t, cfg)

	for range testBurst {
		_, err := a.AuthenticateRequest(bearer(signFrom(is, d.srv.URL)))
		if !errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
			t.Fatalf("err = %v, want ErrAuthenticationUnavailable", err)
		}
	}
	if got := d.served.Load(); got != 1 {
		t.Errorf("issuer metadata fetched %d times for %d tokens during the outage, want 1", got, testBurst)
	}
	if got := is.served.Load(); got != 0 {
		t.Errorf("key set fetched %d times without metadata, want 0", got)
	}

	h, err := k8sapiproxy.NewHandler(k8sapiproxy.Config{
		Authenticator: a,
		Upstream:      &recordingUpstream{},
		Policy:        k8sapiproxy.Policy{Mode: k8sapiproxy.ModeReadOnlyWithDryRun, RedactSecrets: true},
		Logger:        slog.New(slog.DiscardHandler),
	})
	if err != nil {
		t.Fatal(err)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, bearer(signFrom(is, d.srv.URL)))
	if rec.Code != http.StatusServiceUnavailable {
		t.Errorf("discovery outage through the handler: code %d, want 503", rec.Code)
	}

	d.down.Store(false)
	clock = clock.Add(2 * minKeySetFetchInterval)
	if _, err := a.AuthenticateRequest(bearer(signFrom(is, d.srv.URL))); err != nil {
		t.Errorf("after the issuer recovered: %v", err)
	}
}

// The metadata is read again on every key set fetch, so a key set the issuer moved is
// followed even while the old URL keeps answering with the old keys.
func TestAuthenticatorFollowsAMovedKeySet(t *testing.T) {
	old := newIssuer(t)
	moved := newIssuer(t)
	moved.addRSA(testMovedKeyID)
	d := newDiscovery(t, old)
	clock := testNow
	cfg := d.config(old)
	cfg.Now = func() time.Time { return clock }
	a := newAuth(t, cfg)
	newToken := func() string {
		return moved.sign(testMovedKeyID, jose.RS256, func(c *jwt.Claims, _ map[string]any) { c.Issuer = d.srv.URL })
	}

	if _, err := a.AuthenticateRequest(bearer(signFrom(old, d.srv.URL))); err != nil {
		t.Fatalf("key at the first URL: %v", err)
	}
	if id, err := a.AuthenticateRequest(bearer(newToken())); err == nil {
		t.Fatalf("key that is not published yet accepted as %+v", *id)
	}

	movedURL := moved.srv.URL + testKeySetPath
	d.keySetURL.Store(&movedURL)
	clock = clock.Add(2 * minKeySetFetchInterval)
	if _, err := a.AuthenticateRequest(bearer(newToken())); err != nil {
		t.Fatalf("key only at the moved URL, after the next fetch: %v", err)
	}
	if got := moved.served.Load(); got != 1 {
		t.Errorf("moved key set fetched %d times, want 1", got)
	}
	if got := d.served.Load(); got != 2 {
		t.Errorf("issuer metadata fetched %d times, want 2: once per key set fetch", got)
	}
}

// A token that names another issuer is refused from its unverified iss, before any key
// lookup: it causes no metadata or key set fetch, and does not wait on a fetch in flight.
func TestAuthenticatorRefusesAnotherIssuersTokenWithoutFetching(t *testing.T) {
	is := newIssuer(t)
	foreign := newIssuer(t)
	foreign.addRSA(testMovedKeyID)
	d := newDiscovery(t, is)
	a := newAuth(t, d.config(is))
	// An unknown kid, which would otherwise trigger a fetch.
	foreignToken := foreign.sign(testMovedKeyID, jose.RS256, func(c *jwt.Claims, _ map[string]any) {
		c.Issuer = "https://kubernetes.default.svc"
	})

	refused := func(what string) {
		t.Helper()
		id, err := a.AuthenticateRequest(bearer(foreignToken))
		// Errorf, not Fatalf: it also runs off the test's goroutine.
		if err == nil {
			t.Errorf("%s: accepted as %+v", what, *id)
		} else if errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
			t.Errorf("%s: err = %v, want a refusal, not unavailable", what, err)
		}
	}
	refused("nothing fetched yet")
	if d.served.Load() != 0 || is.served.Load() != 0 {
		t.Errorf("fetched metadata %d times and the key set %d times, want 0 and 0", d.served.Load(), is.served.Load())
	}

	// With a key set fetch held open by the issuer, the foreign token is still answered.
	gate := make(chan struct{})
	is.gate.Store(&gate)
	held := make(chan error, 1)
	go func() {
		_, err := a.AuthenticateRequest(bearer(signFrom(is, d.srv.URL)))
		held <- err
	}()
	deadline := time.Now().Add(testWaitLimit)
	for is.served.Load() == 0 {
		if time.Now().After(deadline) {
			t.Fatal("the key set fetch did not reach the issuer")
		}
		time.Sleep(testSettle)
	}
	answered := make(chan struct{})
	go func() {
		defer close(answered)
		refused("during a held fetch")
	}()
	select {
	case <-answered:
	case <-time.After(testWaitLimit):
		t.Error("another issuer's token waited on this issuer's key set fetch")
	}
	close(gate)
	select {
	case err := <-held:
		if err != nil {
			t.Errorf("this issuer's token after the fetch was released: %v", err)
		}
	case <-time.After(testWaitLimit):
		t.Error("the held fetch never finished")
	}
	if got := is.served.Load(); got != 1 {
		t.Errorf("key set fetched %d times, want 1: only for this issuer's token", got)
	}
}

// A redirect to another host is refused even on https: the key set and the metadata come
// only from the host that was configured or discovered.
func TestAuthenticatorRefusesACrossHostRedirect(t *testing.T) {
	t.Run("key set", func(t *testing.T) {
		is := newIssuer(t)
		redirect := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, is.srv.URL+testKeySetPath, http.StatusFound)
		}))
		t.Cleanup(redirect.Close)
		cfg := is.config()
		cfg.KeySetURL = redirect.URL + testKeySetPath
		a := newAuth(t, cfg)
		_, err := a.AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil)))
		if !errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
			t.Errorf("err = %v, want ErrAuthenticationUnavailable after a redirect to another host", err)
		}
		if got := is.served.Load(); got != 0 {
			t.Errorf("the other host served %d key set fetches, want 0", got)
		}
	})

	t.Run("issuer metadata", func(t *testing.T) {
		is := newIssuer(t)
		d := newDiscovery(t, is)
		redirect := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, d.srv.URL+r.URL.Path, http.StatusFound)
		}))
		t.Cleanup(redirect.Close)
		// The metadata would pass its issuer check if the redirect were followed.
		d.issuer.Store(&redirect.URL)
		cfg := d.config(is)
		cfg.Issuer = redirect.URL
		a := newAuth(t, cfg)
		_, err := a.AuthenticateRequest(bearer(signFrom(is, redirect.URL)))
		if !errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
			t.Errorf("err = %v, want ErrAuthenticationUnavailable after a redirect to another host", err)
		}
		if got := d.served.Load(); got != 0 {
			t.Errorf("the other host served %d metadata fetches, want 0", got)
		}
	})
}

// A redirect that stays on the host and port being fetched is followed.
func TestAuthenticatorFollowsASameHostRedirect(t *testing.T) {
	is := newIssuer(t)
	var hops atomic.Int32
	var srv *httptest.Server
	srv = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != testKeySetPath {
			hops.Add(1)
			http.Redirect(w, r, srv.URL+testKeySetPath, http.StatusFound)
			return
		}
		is.mu.Lock()
		defer is.mu.Unlock()
		w.Header().Set("Content-Type", contentJSON)
		_ = json.NewEncoder(w).Encode(is.public)
	}))
	t.Cleanup(srv.Close)
	cfg := is.config()
	cfg.KeySetURL = srv.URL + "/moved"
	if _, err := newAuth(t, cfg).AuthenticateRequest(bearer(is.sign(testKeyID, jose.RS256, nil))); err != nil {
		t.Fatalf("AuthenticateRequest: %v", err)
	}
	if got := hops.Load(); got != 1 {
		t.Errorf("redirected %d times, want 1", got)
	}
}
