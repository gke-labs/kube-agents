package teamsinbound

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"io"
	"log/slog"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const (
	testAppID      = "544a301d-d416-4c68-9fdf-0e32bc6ed126"
	testTenantID   = "759d68f0-65ed-44cd-8487-ce6221b72b2a"
	testServiceURL = "https://smba.trafficmanager.net/amer/"
	testKid        = "key-1"
)

// microsoft is a fake Bot Framework: an OpenID configuration naming a key set,
// and the key set, served over TLS. keys maps kid to the key it publishes;
// endorsements maps kid to the channels it endorses.
type microsoft struct {
	srv          *httptest.Server
	keys         map[string]*rsa.PrivateKey
	endorsements map[string][]string
	fetches      atomic.Int32
	failKeys     atomic.Bool
}

func newMicrosoft(t *testing.T) *microsoft {
	t.Helper()
	m := &microsoft{keys: map[string]*rsa.PrivateKey{}, endorsements: map[string][]string{}}
	m.addKey(t, testKid, "msteams")
	mux := http.NewServeMux()
	m.srv = httptest.NewTLSServer(mux)
	mux.HandleFunc("/v1/.well-known/openidconfiguration", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{"jwks_uri": m.srv.URL + "/v1/.well-known/keys"})
	})
	mux.HandleFunc("/v1/.well-known/keys", func(w http.ResponseWriter, _ *http.Request) {
		m.fetches.Add(1)
		if m.failKeys.Load() {
			http.Error(w, "down", http.StatusBadGateway)
			return
		}
		var keys []map[string]any
		for kid, k := range m.keys {
			keys = append(keys, map[string]any{
				"kty": "RSA", "kid": kid, "use": "sig",
				"n":            base64.RawURLEncoding.EncodeToString(k.PublicKey.N.Bytes()),
				"e":            base64.RawURLEncoding.EncodeToString(big.NewInt(int64(k.PublicKey.E)).Bytes()),
				"endorsements": m.endorsements[kid],
			})
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"keys": keys})
	})
	t.Cleanup(m.srv.Close)
	return m
}

func (m *microsoft) addKey(t *testing.T, kid string, endorsements ...string) {
	t.Helper()
	k, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	m.keys[kid] = k
	m.endorsements[kid] = endorsements
}

func (m *microsoft) configURL() string { return m.srv.URL + "/v1/.well-known/openidconfiguration" }

// claimsAt are the claims of a token Bot Framework would mint now.
func claimsAt(now time.Time) map[string]any {
	return map[string]any{
		"iss": botFrameworkIssuer, "aud": testAppID,
		"exp": now.Add(time.Hour).Unix(), "nbf": now.Add(-time.Minute).Unix(),
		"serviceurl": testServiceURL,
	}
}

// mint signs claims with the key named kid, under the given alg header.
func (m *microsoft) mint(t *testing.T, kid, alg string, claims map[string]any) string {
	t.Helper()
	header, _ := json.Marshal(map[string]string{"alg": alg, "typ": "JWT", "kid": kid})
	payload, _ := json.Marshal(claims)
	signing := base64.RawURLEncoding.EncodeToString(header) + "." + base64.RawURLEncoding.EncodeToString(payload)
	k, ok := m.keys[kid]
	if !ok {
		k = m.keys[testKid]
	}
	digest := sha256.Sum256([]byte(signing))
	sig, err := rsa.SignPKCS1v15(rand.Reader, k, crypto.SHA256, digest[:])
	if err != nil {
		t.Fatal(err)
	}
	return signing + "." + base64.RawURLEncoding.EncodeToString(sig)
}

// broker is a fake credential broker that records what it was handed.
type broker struct {
	srv    *httptest.Server
	status atomic.Int32
	calls  atomic.Int32
	bearer atomic.Value
	body   atomic.Value
}

func newBroker(t *testing.T) *broker {
	t.Helper()
	b := &broker{}
	b.status.Store(http.StatusAccepted)
	b.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != relayEventsPath {
			http.NotFound(w, r)
			return
		}
		b.calls.Add(1)
		b.bearer.Store(r.Header.Get("Authorization"))
		body, _ := io.ReadAll(r.Body)
		b.body.Store(string(body))
		w.WriteHeader(int(b.status.Load()))
		_, _ = w.Write([]byte(`{"status":"accepted"}`))
	}))
	t.Cleanup(b.srv.Close)
	return b
}

type rig struct {
	ms     *microsoft
	br     *broker
	srv    *Server
	now    time.Time
	handle http.Handler
}

func newRig(t *testing.T) *rig {
	t.Helper()
	ms := newMicrosoft(t)
	br := newBroker(t)
	tokenPath := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenPath, []byte("projected-sa-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := FromEnv(func(k string) string {
		return map[string]string{
			EnvAppID: testAppID, EnvTenantID: testTenantID, EnvRelayURL: br.srv.URL,
			EnvTokenPath: tokenPath, EnvOpenIDConfigURL: ms.configURL(),
		}[k]
	})
	if err != nil {
		t.Fatal(err)
	}
	srv := NewServer(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)))
	now := time.Date(2026, 11, 20, 20, 0, 0, 0, time.UTC)
	srv.verifier.client = ms.srv.Client()
	srv.verifier.now = func() time.Time { return now }
	return &rig{ms: ms, br: br, srv: srv, now: now, handle: srv.Handler()}
}

func activityJSON(overrides map[string]any) string {
	a := map[string]any{
		"type": "message", "channelId": "msteams", "serviceUrl": testServiceURL, "text": "hello",
		"from":         map[string]any{"id": "29:1abc", "aadObjectId": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"},
		"conversation": map[string]any{"id": "19:thread@thread.tacv2", "tenantId": testTenantID},
		"channelData":  map[string]any{"tenant": map[string]any{"id": testTenantID}},
	}
	for k, v := range overrides {
		if v == nil {
			delete(a, k)
		} else {
			a[k] = v
		}
	}
	b, _ := json.Marshal(a)
	return string(b)
}

func (r *rig) post(t *testing.T, token, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, EventsPath, strings.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	r.handle.ServeHTTP(rec, req)
	return rec
}

func TestAVerifiedTeamsActivityReachesTheBrokerUnderThePodToken(t *testing.T) {
	r := newRig(t)
	rec := r.post(t, r.ms.mint(t, testKid, signingAlgorithm, claimsAt(r.now)), activityJSON(nil))
	if rec.Code != http.StatusAccepted {
		t.Fatalf("status = %d, body %q", rec.Code, rec.Body.String())
	}
	if r.br.calls.Load() != 1 || r.br.bearer.Load() != "Bearer projected-sa-token" {
		t.Fatalf("broker calls = %d, bearer = %v", r.br.calls.Load(), r.br.bearer.Load())
	}
	if !strings.Contains(r.br.body.Load().(string), `"text":"hello"`) {
		t.Fatalf("the broker did not get the activity: %v", r.br.body.Load())
	}
	if !r.srv.verifier.Ready() {
		t.Fatal("the verifier is not ready after a successful check")
	}
}

func TestARequestTheTokenDoesNotVouchForNeverReachesTheBroker(t *testing.T) {
	r := newRig(t)
	good := claimsAt(r.now)
	with := func(k string, v any) map[string]any {
		c := claimsAt(r.now)
		c[k] = v
		return c
	}
	hs256 := func() string {
		header, _ := json.Marshal(map[string]string{"alg": "HS256", "typ": "JWT", "kid": testKid})
		payload, _ := json.Marshal(good)
		return base64.RawURLEncoding.EncodeToString(header) + "." + base64.RawURLEncoding.EncodeToString(payload) + ".c2ln"
	}
	for _, tc := range []struct {
		name   string
		token  string
		body   string
		status int
		reason string
	}{
		{"no bearer", "", activityJSON(nil), http.StatusUnauthorized, "no bearer"},
		{"not a token", "garbage", activityJSON(nil), http.StatusUnauthorized, "not shaped"},
		{"HS256", hs256(), activityJSON(nil), http.StatusUnauthorized, "not signed with RS256"},
		{"unknown key after a fresh set", r.ms.mint(t, "key-9", signingAlgorithm, good), activityJSON(nil), http.StatusUnauthorized, "does not publish"},
		{"another issuer", r.ms.mint(t, testKid, signingAlgorithm, with("iss", "https://sts.windows.net/x/")), activityJSON(nil), http.StatusUnauthorized, "not issued by Bot Framework"},
		{"another bot", r.ms.mint(t, testKid, signingAlgorithm, with("aud", "00000000-0000-0000-0000-000000000000")), activityJSON(nil), http.StatusUnauthorized, "not for this bot"},
		{"expired", r.ms.mint(t, testKid, signingAlgorithm, with("exp", r.now.Add(-10*time.Minute).Unix())), activityJSON(nil), http.StatusUnauthorized, "expired"},
		{"not yet valid", r.ms.mint(t, testKid, signingAlgorithm, with("nbf", r.now.Add(10*time.Minute).Unix())), activityJSON(nil), http.StatusUnauthorized, "not yet valid"},
		{"service URL mismatch", r.ms.mint(t, testKid, signingAlgorithm, with("serviceurl", "https://smba.trafficmanager.net/emea/")), activityJSON(nil), http.StatusUnauthorized, "another service URL"},
		{"activity without a service URL", r.ms.mint(t, testKid, signingAlgorithm, good), activityJSON(map[string]any{"serviceUrl": nil}), http.StatusUnauthorized, "another service URL"},
		{"another channel", r.ms.mint(t, testKid, signingAlgorithm, good), activityJSON(map[string]any{"channelId": "webchat"}), http.StatusUnauthorized, "not endorsed"},
		{"another tenant", r.ms.mint(t, testKid, signingAlgorithm, good), activityJSON(map[string]any{"channelData": map[string]any{"tenant": map[string]any{"id": "11111111-2222-4333-8444-555555555555"}}, "conversation": map[string]any{"id": "x"}}), http.StatusForbidden, "not from the tenant"},
		{"no tenant at all", r.ms.mint(t, testKid, signingAlgorithm, good), activityJSON(map[string]any{"channelData": nil, "conversation": map[string]any{"id": "x"}}), http.StatusForbidden, "not from the tenant"},
		{"not JSON", r.ms.mint(t, testKid, signingAlgorithm, good), "not json", http.StatusBadRequest, "not a JSON object"},
		{"JSON null", r.ms.mint(t, testKid, signingAlgorithm, good), " null ", http.StatusBadRequest, "not a JSON object"},
		{"JSON array", r.ms.mint(t, testKid, signingAlgorithm, good), "[]", http.StatusBadRequest, "not a JSON object"},
		{"oversize", r.ms.mint(t, testKid, signingAlgorithm, good), `{"text":"` + strings.Repeat("x", maxBodyBytes) + `"}`, http.StatusRequestEntityTooLarge, "larger"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			before := r.br.calls.Load()
			rec := r.post(t, tc.token, tc.body)
			if rec.Code != tc.status || !strings.Contains(rec.Body.String(), tc.reason) {
				t.Fatalf("status = %d, body %q; want %d containing %q", rec.Code, rec.Body.String(), tc.status, tc.reason)
			}
			if r.br.calls.Load() != before {
				t.Fatal("the broker was reached")
			}
			if strings.Contains(rec.Body.String(), tc.token) && tc.token != "" && tc.token != "garbage" {
				t.Fatal("the refusal echoed the token")
			}
		})
	}
	req := httptest.NewRequest(http.MethodGet, EventsPath, nil)
	rec := httptest.NewRecorder()
	r.handle.ServeHTTP(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET = %d", rec.Code)
	}
	req = httptest.NewRequest(http.MethodPost, "/other", strings.NewReader("{}"))
	rec = httptest.NewRecorder()
	r.handle.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("other path = %d", rec.Code)
	}
}

func TestAnAudienceArrayAndAChannelWithoutEndorsementsAreAccepted(t *testing.T) {
	r := newRig(t)
	r.ms.addKey(t, "key-any")
	c := claimsAt(r.now)
	c["aud"] = []string{"other", strings.ToUpper(testAppID)}
	if rec := r.post(t, r.ms.mint(t, "key-any", signingAlgorithm, c), activityJSON(nil)); rec.Code != http.StatusAccepted {
		t.Fatalf("status = %d, body %q", rec.Code, rec.Body.String())
	}
}

func TestARotatedKeyIsFetchedOnceAndJunkKidsDoNotHammerMicrosoft(t *testing.T) {
	r := newRig(t)
	if rec := r.post(t, r.ms.mint(t, testKid, signingAlgorithm, claimsAt(r.now)), activityJSON(nil)); rec.Code != http.StatusAccepted {
		t.Fatalf("first: %d %s", rec.Code, rec.Body.String())
	}
	// Microsoft rotates: a new key appears; a token under it arrives after
	// the minimum refresh interval.
	r.ms.addKey(t, "key-2", "msteams")
	r.now = r.now.Add(2 * time.Minute)
	r.srv.verifier.now = func() time.Time { return r.now }
	if rec := r.post(t, r.ms.mint(t, "key-2", signingAlgorithm, claimsAt(r.now)), activityJSON(nil)); rec.Code != http.StatusAccepted {
		t.Fatalf("rotated: %d %s", rec.Code, rec.Body.String())
	}
	fetches := r.ms.fetches.Load()
	// Within the minute, an unknown kid cannot trigger another fetch: it is
	// an unchecked 503, and Microsoft's endpoint is not called.
	if rec := r.post(t, r.ms.mint(t, "key-junk", signingAlgorithm, claimsAt(r.now)), activityJSON(nil)); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("junk kid inside the minute: %d %s", rec.Code, rec.Body.String())
	}
	if r.ms.fetches.Load() != fetches {
		t.Fatal("a junk kid inside the minute fetched the keys again")
	}
}

func TestAKeyOutageIsUncheckedNotRefusedAndCachedKeysStillServe(t *testing.T) {
	r := newRig(t)
	if rec := r.post(t, r.ms.mint(t, testKid, signingAlgorithm, claimsAt(r.now)), activityJSON(nil)); rec.Code != http.StatusAccepted {
		t.Fatalf("first: %d", rec.Code)
	}
	r.ms.failKeys.Store(true)
	r.now = r.now.Add(2 * time.Minute)
	r.srv.verifier.now = func() time.Time { return r.now }
	// A cached key still serves during the outage.
	if rec := r.post(t, r.ms.mint(t, testKid, signingAlgorithm, claimsAt(r.now)), activityJSON(nil)); rec.Code != http.StatusAccepted {
		t.Fatalf("cached key during outage: %d %s", rec.Code, rec.Body.String())
	}
	// An unknown key during the outage is a 503, not a 401: nothing vouched
	// either way.
	if rec := r.post(t, r.ms.mint(t, "key-new", signingAlgorithm, claimsAt(r.now)), activityJSON(nil)); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("unknown key during outage: %d %s", rec.Code, rec.Body.String())
	}
}

func TestAFailedStartupReadRetriesOnItsOwnUntilReady(t *testing.T) {
	r := newRig(t)
	r.ms.failKeys.Store(true)
	fails := 0
	r.srv.verifier.WarmUntilReady(context.Background(), time.Second, time.Millisecond, 4*time.Millisecond, func(err error, next time.Duration) {
		if err == nil {
			t.Fatal("onFail called without an error")
		}
		fails++
		if fails == 3 {
			// Microsoft recovers before the fourth attempt.
			r.ms.failKeys.Store(false)
		}
	})
	if fails != 3 || !r.srv.verifier.Ready() {
		t.Fatalf("fails=%d ready=%v: the read should have been retried until it succeeded", fails, r.srv.verifier.Ready())
	}
	if rec := r.post(t, r.ms.mint(t, testKid, signingAlgorithm, claimsAt(r.now)), activityJSON(nil)); rec.Code != http.StatusAccepted {
		t.Fatalf("after recovery: %d %s", rec.Code, rec.Body.String())
	}
}

func TestWarmUntilReadyStopsWhenTheProcessDoes(t *testing.T) {
	r := newRig(t)
	r.ms.failKeys.Store(true)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		r.srv.verifier.WarmUntilReady(ctx, time.Second, time.Hour, time.Hour, func(error, time.Duration) { cancel() })
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("WarmUntilReady kept waiting after its context ended")
	}
	if r.srv.verifier.Ready() {
		t.Fatal("ready without a key set")
	}
}

func TestAFailedRefreshIsRetriedAfterAMinuteNotAfterADay(t *testing.T) {
	r := newRig(t)
	if rec := r.post(t, r.ms.mint(t, testKid, signingAlgorithm, claimsAt(r.now)), activityJSON(nil)); rec.Code != http.StatusAccepted {
		t.Fatalf("first: %d", rec.Code)
	}
	// The set goes stale and the refresh fails; the cached key still serves.
	r.ms.failKeys.Store(true)
	r.now = r.now.Add(keysTTL + time.Minute)
	r.srv.verifier.now = func() time.Time { return r.now }
	before := r.ms.fetches.Load()
	if rec := r.post(t, r.ms.mint(t, testKid, signingAlgorithm, claimsAt(r.now)), activityJSON(nil)); rec.Code != http.StatusAccepted {
		t.Fatalf("stale set during outage: %d %s", rec.Code, rec.Body.String())
	}
	if r.ms.fetches.Load() != before+1 {
		t.Fatalf("a stale set should be refreshed once: fetches %d -> %d", before, r.ms.fetches.Load())
	}
	// Inside the minute after the failed attempt: served from the cache, no fetch.
	r.now = r.now.Add(30 * time.Second)
	if rec := r.post(t, r.ms.mint(t, testKid, signingAlgorithm, claimsAt(r.now)), activityJSON(nil)); rec.Code != http.StatusAccepted || r.ms.fetches.Load() != before+1 {
		t.Fatalf("inside the minute: %d, fetches %d", rec.Code, r.ms.fetches.Load())
	}
	// A minute after it: tried again, not a day after.
	r.now = r.now.Add(31 * time.Second)
	if rec := r.post(t, r.ms.mint(t, testKid, signingAlgorithm, claimsAt(r.now)), activityJSON(nil)); rec.Code != http.StatusAccepted || r.ms.fetches.Load() != before+2 {
		t.Fatalf("after the minute: %d, fetches %d", rec.Code, r.ms.fetches.Load())
	}
	// Microsoft recovers: the next retry reads the set and a new key is found.
	r.ms.failKeys.Store(false)
	r.ms.addKey(t, "key-2", "msteams")
	r.now = r.now.Add(2 * time.Minute)
	if rec := r.post(t, r.ms.mint(t, "key-2", signingAlgorithm, claimsAt(r.now)), activityJSON(nil)); rec.Code != http.StatusAccepted {
		t.Fatalf("after recovery under the rotated key: %d %s", rec.Code, rec.Body.String())
	}
}

func TestTheBrokersAnswerIsMappedAndItsFaultsAreNotMicrosofts(t *testing.T) {
	r := newRig(t)
	tok := r.ms.mint(t, testKid, signingAlgorithm, claimsAt(r.now))
	r.br.status.Store(http.StatusServiceUnavailable)
	if rec := r.post(t, tok, activityJSON(nil)); rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), "queue is full") {
		t.Fatalf("queue full: %d %s", rec.Code, rec.Body.String())
	}
	r.br.status.Store(http.StatusInternalServerError)
	if rec := r.post(t, tok, activityJSON(nil)); rec.Code != http.StatusBadGateway {
		t.Fatalf("broker 500: %d %s", rec.Code, rec.Body.String())
	}
	r.br.srv.Close()
	if rec := r.post(t, tok, activityJSON(nil)); rec.Code != http.StatusBadGateway {
		t.Fatalf("broker down: %d %s", rec.Code, rec.Body.String())
	}
}

func TestReadinessFollowsTheFirstKeyRead(t *testing.T) {
	r := newRig(t)
	status := r.srv.StatusHandler()
	get := func(path string) int {
		rec := httptest.NewRecorder()
		status.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		return rec.Code
	}
	if get(HealthzPath) != http.StatusOK || get(ReadyzPath) != http.StatusServiceUnavailable {
		t.Fatal("before the first read: healthz should be 200 and readyz 503")
	}
	if err := r.srv.verifier.Warm(context.Background()); err != nil {
		t.Fatal(err)
	}
	if get(ReadyzPath) != http.StatusOK {
		t.Fatal("after Warm: readyz should be 200")
	}
	rec := httptest.NewRecorder()
	status.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, MetricsPath, nil))
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "kubeagents_teams_inbound_refusals_total") {
		t.Fatalf("metrics: %d", rec.Code)
	}
}

func TestFromEnvRefusesWhatTheOperatorWouldNeverRender(t *testing.T) {
	good := map[string]string{
		EnvAppID: testAppID, EnvTenantID: testTenantID, EnvRelayURL: "http://broker:8765/",
		EnvTokenPath: "/var/run/token", EnvOpenIDConfigURL: "",
	}
	cfg, err := FromEnv(func(k string) string { return good[k] })
	if err != nil || cfg.Listen != DefaultListen || cfg.StatusListen != DefaultStatusListen || cfg.OpenIDConfigURL != DefaultOpenIDConfigURL || cfg.RelayURL != "http://broker:8765" {
		t.Fatalf("good config: %+v, %v", cfg, err)
	}
	for name, bad := range map[string]map[string]string{
		"app id not a GUID":       {EnvAppID: "kube-agents-door"},
		"tenant missing":          {EnvTenantID: ""},
		"relay not a URL":         {EnvRelayURL: "broker"},
		"token path missing":      {EnvTokenPath: ""},
		"openid config not https": {EnvOpenIDConfigURL: "http://login.example/x"},
	} {
		t.Run(name, func(t *testing.T) {
			env := map[string]string{}
			for k, v := range good {
				env[k] = v
			}
			for k, v := range bad {
				env[k] = v
			}
			if _, err := FromEnv(func(k string) string { return env[k] }); err == nil {
				t.Fatal("accepted")
			}
		})
	}
}

func TestServiceURLComparison(t *testing.T) {
	for _, tc := range []struct {
		claimed, named string
		want           bool
	}{
		{"https://smba.trafficmanager.net/amer/", "https://smba.trafficmanager.net/amer", true},
		{"https://SMBA.trafficmanager.net/amer", "https://smba.trafficmanager.net/amer/", true},
		{"https://smba.trafficmanager.net/amer", "https://smba.trafficmanager.net/emea", false},
		{"https://smba.trafficmanager.net/amer", "http://smba.trafficmanager.net/amer", false},
		{"", "https://smba.trafficmanager.net/amer", false},
		{"https://smba.trafficmanager.net/amer", "", false},
	} {
		if got := sameServiceURL(tc.claimed, tc.named); got != tc.want {
			t.Errorf("sameServiceURL(%q, %q) = %v", tc.claimed, tc.named, got)
		}
	}
}
