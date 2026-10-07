package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
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

// The A2A door's Google access-token class: the verifier against a fake
// tokeninfo endpoint, then the class end to end on the door's rig.

const (
	googleTestClientID    = "client-123.apps.googleusercontent.com"
	googleTestEmail       = "dev@example.com"
	googleTestOtherEmail  = "stranger@example.com"
	googleTestToken       = "ya29.good"
	googleTestOtherToken  = "ya29.stranger"
	googleTestUnknown     = "ya29.unknown"
	googleTestIntruderKey = "intruder@example.com"
)

// fakeTokeninfo answers tokeninfo for the tokens it is given and counts the
// calls. A token it does not know is a 400, as Google answers one.
type fakeTokeninfo struct {
	mu      sync.Mutex
	answers map[string]map[string]any
	calls   atomic.Int64
	srv     *httptest.Server
}

func newFakeTokeninfo(t *testing.T) *fakeTokeninfo {
	t.Helper()
	f := &fakeTokeninfo{answers: map[string]map[string]any{}}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.calls.Add(1)
		f.mu.Lock()
		if r.Method != http.MethodPost || r.URL.Query().Has(a2aGoogleTokeninfoParam) {
			// The token must travel in the body, never the URL.
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		answer, ok := f.answers[r.PostFormValue(a2aGoogleTokeninfoParam)]
		f.mu.Unlock()
		if !ok {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"error_description":"Invalid Value"}`))
			return
		}
		_ = json.NewEncoder(w).Encode(answer)
	}))
	t.Cleanup(f.srv.Close)
	return f
}

// set answers token as Google does for a live token: strings throughout.
func (f *fakeTokeninfo) set(token string, fields map[string]any) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.answers[token] = fields
}

func liveTokeninfo(aud, email string) map[string]any {
	return map[string]any{
		"aud": aud, "azp": aud, "email": email, "email_verified": "true",
		"exp": fmt.Sprint(time.Now().Add(time.Hour).Unix()), "expires_in": "3600",
	}
}

func testVerifier(f *fakeTokeninfo) *googleTokenVerifier {
	v := newGoogleTokenVerifier(googleTestClientID)
	v.tokeninfoURL = f.srv.URL
	return v
}

func TestGoogleVerifierAdmitsATokenIssuedForTheClient(t *testing.T) {
	f := newFakeTokeninfo(t)
	f.set(googleTestToken, liveTokeninfo(googleTestClientID, "Dev@Example.com"))
	email, err := testVerifier(f).verify(context.Background(), googleTestToken)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if email != "Dev@Example.com" {
		t.Errorf("email = %q, want it as Google sent it, case-preserved (the Chat adapter keeps the case for the audit join)", email)
	}
}

func TestGoogleVerifierAdmitsOnTheAuthorizedPartyAlone(t *testing.T) {
	f := newFakeTokeninfo(t)
	info := liveTokeninfo("some-other-audience", googleTestEmail)
	info["azp"] = googleTestClientID
	f.set(googleTestToken, info)
	if _, err := testVerifier(f).verify(context.Background(), googleTestToken); err != nil {
		t.Fatalf("a token whose azp is the client was refused: %v", err)
	}
}

// TestGoogleVerifierRefusals: each reason a token is refused on its merits,
// each a refusal (errGoogleTokenRefused), none cached.
func TestGoogleVerifierRefusals(t *testing.T) {
	expired := liveTokeninfo(googleTestClientID, googleTestEmail)
	expired["exp"] = fmt.Sprint(time.Now().Add(-time.Minute).Unix())
	unverified := liveTokeninfo(googleTestClientID, googleTestEmail)
	unverified["email_verified"] = "false"
	noEmail := liveTokeninfo(googleTestClientID, "")
	colon := liveTokeninfo(googleTestClientID, "a:b@example.com")
	noExpiry := liveTokeninfo(googleTestClientID, googleTestEmail)
	delete(noExpiry, "exp")
	delete(noExpiry, "expires_in")
	cases := map[string]map[string]any{
		"another client's token": liveTokeninfo("another-client", googleTestEmail),
		"an expired token":       expired,
		"an unverified email":    unverified,
		"no email":               noEmail,
		"a colon in the email":   colon,
		"no expiry at all":       noExpiry,
	}
	for name, info := range cases {
		t.Run(name, func(t *testing.T) {
			f := newFakeTokeninfo(t)
			f.set(googleTestToken, info)
			v := testVerifier(f)
			for i := 0; i < 2; i++ {
				_, err := v.verify(context.Background(), googleTestToken)
				if !errors.Is(err, errGoogleTokenRefused) {
					t.Fatalf("attempt %d: err = %v, want a refusal", i, err)
				}
			}
			if got := f.calls.Load(); got != 2 {
				t.Errorf("tokeninfo was asked %d times for two attempts: a refusal was cached", got)
			}
		})
	}
	t.Run("a token Google does not recognise", func(t *testing.T) {
		f := newFakeTokeninfo(t)
		if _, err := testVerifier(f).verify(context.Background(), googleTestUnknown); !errors.Is(err, errGoogleTokenRefused) {
			t.Fatalf("err = %v, want a refusal", err)
		}
	})
}

// TestGoogleVerifierCoalescesConcurrentChecksOfOneToken: a burst of
// requests with one fresh token costs one tokeninfo call, and none of it is
// refused for want of a slot, even when the burst is wider than the slots.
func TestGoogleVerifierCoalescesConcurrentChecksOfOneToken(t *testing.T) {
	release := make(chan struct{})
	var calls atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		<-release
		_ = json.NewEncoder(w).Encode(liveTokeninfo(googleTestClientID, googleTestEmail))
	}))
	defer srv.Close()
	v := newGoogleTokenVerifier(googleTestClientID)
	v.tokeninfoURL = srv.URL
	burst := 2 * a2aGoogleTokeninfoConcurrency
	errs := make(chan error, burst)
	for i := 0; i < burst; i++ {
		go func() {
			_, err := v.verify(context.Background(), googleTestToken)
			errs <- err
		}()
	}
	waitFor(t, "the first check to reach tokeninfo", func() bool { return calls.Load() >= 1 })
	time.Sleep(100 * time.Millisecond) // let the rest of the burst arrive and join it
	close(release)
	for i := 0; i < burst; i++ {
		if err := <-errs; err != nil {
			t.Errorf("request %d of the burst: %v", i, err)
		}
	}
	if got := calls.Load(); got != 1 {
		t.Errorf("tokeninfo was asked %d times for one token, want 1", got)
	}
}

// TestGoogleVerifierReadsANumericExpiry: tokeninfo sends exp as a string
// today, but a number must parse too, not come back as "1.7598e+09".
func TestGoogleVerifierReadsANumericExpiry(t *testing.T) {
	f := newFakeTokeninfo(t)
	info := liveTokeninfo(googleTestClientID, googleTestEmail)
	info["exp"] = time.Now().Add(time.Hour).Unix()
	delete(info, "expires_in")
	info["email_verified"] = true
	f.set(googleTestToken, info)
	if _, err := testVerifier(f).verify(context.Background(), googleTestToken); err != nil {
		t.Fatalf("a token with a numeric exp and no expires_in was refused: %v", err)
	}
}

// TestGoogleVerifierGoogleErrorIsNotARefusal: a 5xx or a 429 from
// tokeninfo is Google failing to answer, so the caller is not told its token
// is bad (which would send it round a sign-in that cannot help).
func TestGoogleVerifierGoogleErrorIsNotARefusal(t *testing.T) {
	for _, status := range []int{http.StatusInternalServerError, http.StatusServiceUnavailable, http.StatusTooManyRequests} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(status) }))
		v := newGoogleTokenVerifier(googleTestClientID)
		v.tokeninfoURL = srv.URL
		_, err := v.verify(context.Background(), googleTestToken)
		srv.Close()
		if err == nil || errors.Is(err, errGoogleTokenRefused) {
			t.Errorf("HTTP %d: err = %v, want a failure to check, not a refusal", status, err)
		}
	}
}

// TestGoogleVerifierNeverSendsANonGoogleBearer: a bearer without Google's
// access-token prefix is refused locally, and Google is never asked.
func TestGoogleVerifierNeverSendsANonGoogleBearer(t *testing.T) {
	f := newFakeTokeninfo(t)
	v := testVerifier(f)
	for _, token := range []string{"stale-static-token", "xoxb-1234", "ghp_abc", "eyJhbGciOi.jwt.like", "YA29.upper", ""} {
		if _, err := v.verify(context.Background(), token); !errors.Is(err, errGoogleTokenRefused) {
			t.Errorf("%q: err = %v, want a refusal", token, err)
		}
	}
	if got := f.calls.Load(); got != 0 {
		t.Errorf("tokeninfo was asked %d times for bearers that are not Google access tokens", got)
	}
}

// TestGoogleVerifierOutageIsNotARefusalAndLeaksNoToken: an endpoint that
// cannot be reached is a failure to check, not a verdict on the token, and
// the error the caller is shown does not quote the URL the token rides in.
func TestGoogleVerifierOutageIsNotARefusalAndLeaksNoToken(t *testing.T) {
	f := newFakeTokeninfo(t)
	v := testVerifier(f)
	f.srv.Close()
	_, err := v.verify(context.Background(), googleTestToken)
	if err == nil {
		t.Fatal("an unreachable tokeninfo admitted the token")
	}
	if errors.Is(err, errGoogleTokenRefused) {
		t.Errorf("an outage reads as a refusal: %v", err)
	}
	if strings.Contains(err.Error(), googleTestToken) {
		t.Errorf("the error quotes the token: %v", err)
	}
}

func TestGoogleVerifierCachesAnAdmissionUntilItsBound(t *testing.T) {
	f := newFakeTokeninfo(t)
	f.set(googleTestToken, liveTokeninfo(googleTestClientID, googleTestEmail))
	v := testVerifier(f)
	now := time.Now()
	v.now = func() time.Time { return now }
	for i := 0; i < 3; i++ {
		if _, err := v.verify(context.Background(), googleTestToken); err != nil {
			t.Fatal(err)
		}
	}
	if got := f.calls.Load(); got != 1 {
		t.Fatalf("tokeninfo asked %d times for three checks inside the bound, want 1", got)
	}
	// Past the cache bound (the token itself still lives an hour), Google
	// is asked again: a revocation lands within a2aGoogleCacheTTL.
	now = now.Add(a2aGoogleCacheTTL + time.Second)
	if _, err := v.verify(context.Background(), googleTestToken); err != nil {
		t.Fatal(err)
	}
	if got := f.calls.Load(); got != 2 {
		t.Errorf("tokeninfo asked %d times after the bound, want 2", got)
	}
}

func TestGoogleVerifierCacheStopsAtTheTokensOwnExpiry(t *testing.T) {
	f := newFakeTokeninfo(t)
	info := liveTokeninfo(googleTestClientID, googleTestEmail)
	now := time.Now()
	info["exp"] = fmt.Sprint(now.Add(time.Minute).Unix())
	f.set(googleTestToken, info)
	v := testVerifier(f)
	v.now = func() time.Time { return now }
	if _, err := v.verify(context.Background(), googleTestToken); err != nil {
		t.Fatal(err)
	}
	now = now.Add(2 * time.Minute)
	if _, err := v.verify(context.Background(), googleTestToken); !errors.Is(err, errGoogleTokenRefused) {
		t.Fatalf("a cached token past its own expiry was admitted: %v", err)
	}
}

func TestGoogleVerifierCacheIsBoundedInBytes(t *testing.T) {
	v := newGoogleTokenVerifier(googleTestClientID)
	far := time.Now().Add(time.Hour)
	for i := 0; i < a2aGoogleCacheBudgetBytes/a2aGoogleCacheEntryOverhead; i++ {
		v.remember(googleTokenKey(fmt.Sprint("token-", i)), googleTestEmail, far)
		if v.bytes > a2aGoogleCacheBudgetBytes {
			t.Fatalf("after %d entries the cache holds %d bytes, over the %d budget", i+1, v.bytes, a2aGoogleCacheBudgetBytes)
		}
	}
	if _, ok := v.cached(googleTokenKey("token-0")); ok {
		t.Error("the oldest entry survived past the budget; eviction is not oldest-first")
	}
	if len(v.cache) != len(v.order) {
		t.Errorf("cache %d entries, order %d", len(v.cache), len(v.order))
	}
}

func TestGoogleVerifierBoundsChecksInFlight(t *testing.T) {
	v := newGoogleTokenVerifier(googleTestClientID)
	for i := 0; i < a2aGoogleTokeninfoConcurrency; i++ {
		v.inflight <- struct{}{}
	}
	_, err := v.verify(context.Background(), googleTestToken)
	if err == nil || errors.Is(err, errGoogleTokenRefused) {
		t.Fatalf("err = %v, want a failure to check (not a refusal) once the bound is spent", err)
	}
}

// startA2AGoogleRig is the door's rig with the Google class armed against a
// fake tokeninfo: googleTestToken verifies as googleTestEmail (allowed) and
// googleTestOtherToken as googleTestOtherEmail (not allowed). The chat
// principal map carries the Google caller's door id mapped to an intruder,
// so a roster that resolved through the chat map would say so.
func startA2AGoogleRig(t *testing.T) (*a2aRig, *fakeTokeninfo) {
	t.Helper()
	f := newFakeTokeninfo(t)
	f.set(googleTestToken, liveTokeninfo(googleTestClientID, googleTestEmail))
	f.set(googleTestOtherToken, liveTokeninfo(googleTestClientID, googleTestOtherEmail))
	chatMap := filepath.Join(t.TempDir(), "chat-principal-map")
	if err := os.WriteFile(chatMap, []byte(a2aGoogleCaller(googleTestEmail)+" "+googleTestIntruderKey+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	r := startA2ARigTuned(t, func(door *A2ADoor) Adapter { return door }, a2aRigTuning{
		doorOptions: func(o *A2ADoorOptions) {
			o.GoogleClientID = googleTestClientID
			o.GoogleAllowedUsers = []string{"Dev@Example.com"}
		},
		builtDoor: func(d *A2ADoor) { d.google.tokeninfoURL = f.srv.URL },
		config: func(c *Config) {
			c.A2ADoorGoogleClientID = googleTestClientID
			c.A2ADoorAllowedUsers = []string{"Dev@Example.com"}
			c.PrincipalMapPath = chatMap
		},
	})
	return r, f
}

// TestA2AGoogleCallerStartsATaskAttributedToTheirEmail: the developer class
// end to end. The bus carries the verified email as the principal, the
// class's own backend and mechanism, and a roster resolved the class's way,
// never through the chat map.
func TestA2AGoogleCallerStartsATaskAttributedToTheirEmail(t *testing.T) {
	r, _ := startA2AGoogleRig(t)
	resp, status := r.rawRPC(t, googleTestToken, "", a2aMethodSend, sendParams("hello as me", "m-1", "ctx-1", false))
	if status != http.StatusOK {
		t.Fatalf("HTTP %d", status)
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
	if authority.Requester.Backend != a2aGoogleBackend || authority.Requester.VerifiedBy != a2aGoogleVerifiedBy {
		t.Errorf("requester = %+v, want backend %q verifiedBy %q", authority.Requester, a2aGoogleBackend, a2aGoogleVerifiedBy)
	}
	if authority.Requester.Principal != ps.Hash(googleTestEmail) {
		t.Errorf("principal = %q, want the pseudonym of %s", authority.Requester.Principal, googleTestEmail)
	}
	if task.Metadata["backend"] != a2aGoogleBackend {
		t.Errorf("task metadata backend = %v, want %q, as the authority block says", task.Metadata["backend"], a2aGoogleBackend)
	}
	wantKey := a2aKeyPrefix + a2aGoogleCaller(googleTestEmail) + ":ctx-1"
	if authority.Audience.Conversation != wantKey {
		t.Errorf("conversation = %q, want %q", authority.Audience.Conversation, wantKey)
	}
	if len(authority.Audience.Roster) != 1 || authority.Audience.Roster[0] != ps.Hash(googleTestEmail) {
		t.Errorf("roster = %v, want [pseudonym of %s]; the pseudonym of the chat map's %s means the roster resolved through the chat map",
			authority.Audience.Roster, googleTestEmail, googleTestIntruderKey)
	}
}

// TestA2AGoogleCallerOffTheAllowlistIsRefusedBeforeTheDoorHoldsAnything:
// a verified account off the list is a 403 at the door, before it creates
// a conversation or a submission, so a stream of such accounts cannot push
// an allowed developer's state out of the door's bounds.
func TestA2AGoogleCallerOffTheAllowlistIsRefusedBeforeTheDoorHoldsAnything(t *testing.T) {
	r, _ := startA2AGoogleRig(t)
	_, status := r.rawRPC(t, googleTestOtherToken, "", a2aMethodSend, sendParams("let me in", "m-1", "", false))
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

// TestA2AGoogleCallerMayNotAlsoNameACaller: a token travels with no name
// beside it, in the header or in the metadata.
func TestA2AGoogleCallerMayNotAlsoNameACaller(t *testing.T) {
	r, _ := startA2AGoogleRig(t)
	resp, _ := r.rawRPC(t, googleTestToken, a2aTestCaller, a2aMethodSend, sendParams("as someone else", "m-1", "", false))
	if resp.Error == nil || resp.Error.Code != rpcInvalidParams {
		t.Errorf("header: response = %+v, want error %d", resp, rpcInvalidParams)
	}
	params := sendParams("as someone else", "m-2", "", false)
	params["message"].(map[string]any)["metadata"] = map[string]any{a2aCallerMetadataKey: a2aTestCaller}
	resp, _ = r.rawRPC(t, googleTestToken, "", a2aMethodSend, params)
	if resp.Error == nil || resp.Error.Code != rpcInvalidParams {
		t.Errorf("metadata: response = %+v, want error %d", resp, rpcInvalidParams)
	}
	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 0 {
		t.Fatalf("a named Google caller reached the bus: %d envelopes", len(envs))
	}
}

// TestA2AEvalCallerCannotReachAGoogleCallersTask: the static token's holder
// names their caller freely, so the two classes must not share a scope. An
// eval caller spelling the email, or spelling "google" with the email in
// the context id, reaches neither the task nor the conversation.
func TestA2AEvalCallerCannotReachAGoogleCallersTask(t *testing.T) {
	r, _ := startA2AGoogleRig(t)
	resp, _ := r.rawRPC(t, googleTestToken, "", a2aMethodSend, sendParams("mine", "m-1", "ctx-1", false))
	task := taskOf(t, resp)
	for _, caller := range []string{googleTestEmail, "google", a2aTestCaller} {
		got, _ := r.rawRPC(t, a2aTestToken, caller, a2aMethodGet, map[string]any{"id": task.ID})
		if got.Error == nil || got.Error.Code != a2aErrTaskNotFound {
			t.Errorf("eval caller %q read the Google caller's task: %+v", caller, got)
		}
	}
	// The key an eval caller "google" builds from the context
	// "<email>:ctx-1" is not the Google caller's key.
	if a2aConversationKey("google", googleTestEmail+":ctx-1") == a2aConversationKey(a2aGoogleCaller(googleTestEmail), "ctx-1") {
		t.Error("an eval caller can spell a Google caller's conversation key")
	}
	// And the Google caller does not read an eval caller's task.
	evalTask := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("eval's", "m-2", "", false)))
	got, _ := r.rawRPC(t, googleTestToken, "", a2aMethodGet, map[string]any{"id": evalTask.ID})
	if got.Error == nil || got.Error.Code != a2aErrTaskNotFound {
		t.Errorf("the Google caller read an eval caller's task: %+v", got)
	}
}

// TestA2AGoogleClassRefusalsAreTransportStatuses: a bad token is a 401 the
// client answers by signing in again; an outage is a 503 it does not.
func TestA2AGoogleClassRefusalsAreTransportStatuses(t *testing.T) {
	r, f := startA2AGoogleRig(t)
	body := sendParams("x", "m-1", "", false)
	if _, status := r.rawRPC(t, googleTestUnknown, "", a2aMethodSend, body); status != http.StatusUnauthorized {
		t.Errorf("an unrecognised token: HTTP %d, want 401", status)
	}
	f.srv.Close()
	if _, status := r.rawRPC(t, "ya29.fresh-during-outage", "", a2aMethodSend, body); status != http.StatusServiceUnavailable {
		t.Errorf("tokeninfo unreachable: HTTP %d, want 503", status)
	}
	// The static token is untouched by the class.
	if resp := r.rpc(t, a2aTestCaller, a2aMethodSend, body); resp.Error != nil {
		t.Errorf("the eval class broke with the Google class armed: %+v", resp.Error)
	}
}

func TestA2AStaticTokenRigRefusesOtherBearersWithoutAskingGoogle(t *testing.T) {
	r := startA2ARig(t)
	if r.door.google != nil {
		t.Fatal("the class is armed with no client id")
	}
	if _, status := r.rawRPC(t, googleTestToken, a2aTestCaller, a2aMethodSend, sendParams("x", "m-1", "", false)); status != http.StatusUnauthorized {
		t.Errorf("HTTP %d, want 401", status)
	}
}

func TestA2ACardListsGoogleSignInOnlyWhenArmed(t *testing.T) {
	plain := (&A2ADoor{}).card("http://x")
	if _, ok := plain.SecuritySchemes[a2aGoogleSchemeName]; ok || len(plain.Security) != 1 {
		t.Errorf("unarmed card: schemes %v security %v", plain.SecuritySchemes, plain.Security)
	}
	armed := (&A2ADoor{google: newGoogleTokenVerifier(googleTestClientID)}).card("http://x")
	scheme, ok := armed.SecuritySchemes[a2aGoogleSchemeName]
	if !ok || scheme.Type != "openIdConnect" || scheme.OpenIDConnectURL != a2aGoogleOpenIDConfigURL {
		t.Errorf("armed card scheme = %+v", scheme)
	}
	if len(armed.Security) != 2 {
		t.Errorf("armed card security = %v, want the two schemes as alternatives", armed.Security)
	}
}

func TestA2AGoogleResolverRequiresTheClassPrefix(t *testing.T) {
	g := &Gateway{a2aGoogleAllowed: map[string]bool{googleTestEmail: true}}
	if got := g.resolveA2AGooglePrincipal(a2aGoogleCaller(googleTestEmail)); got != googleTestEmail {
		t.Errorf("prefixed allowed caller resolved to %q", got)
	}
	for _, id := range []string{googleTestEmail, "google:" + googleTestEmail, a2aGoogleCallerPrefix} {
		if got := g.resolveA2AGooglePrincipal(id); got != "" {
			t.Errorf("%q resolved to %q, want nothing", id, got)
		}
	}
	if got := g.resolveA2AGooglePrincipal(a2aGoogleCaller("Dev@Example.com")); got != "Dev@Example.com" {
		t.Errorf("a mixed-case allowed email resolved to %q, want it case-preserved", got)
	}
	if got := (&Gateway{}).resolveA2AGooglePrincipal(a2aGoogleCaller(googleTestEmail)); got != "" {
		t.Errorf("an empty allowlist admitted %q", got)
	}
}

func TestA2AGoogleConfigGuards(t *testing.T) {
	setBaseEnv(t)
	t.Setenv("A2A_ATTRIBUTION_SALT", "test-salt")
	t.Setenv("DISCORD_TOKEN", "x")
	t.Setenv("A2A_DOOR_LISTEN", "")
	t.Setenv("A2A_DOOR_GOOGLE_CLIENT_ID", googleTestClientID)
	if _, err := FromEnv(); err == nil || !strings.Contains(err.Error(), "A2A_DOOR_LISTEN") {
		t.Fatalf("a client id without the door was accepted: %v", err)
	}
	t.Setenv("A2A_DOOR_LISTEN", "127.0.0.1:9999")
	t.Setenv("A2A_DOOR_TOKEN", "t")
	t.Setenv("A2A_DOOR_ALLOWED_USERS", " dev@example.com, ,other@example.com ")
	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.A2ADoorGoogleClientID != googleTestClientID {
		t.Errorf("client id = %q", cfg.A2ADoorGoogleClientID)
	}
	if len(cfg.A2ADoorAllowedUsers) != 2 || cfg.A2ADoorAllowedUsers[0] != "dev@example.com" {
		t.Errorf("allowlist = %q", cfg.A2ADoorAllowedUsers)
	}
}

// TestA2AGoogleTurnClockStartsBeforeTheSessionLock: a Google-verified
// caller's turn is a door turn, so its clock runs through the session lock
// wait as the inject door's does (TestInjectTheTurnClockStartsBeforeTheSessionLock
// has the reasoning). handleInbound chooses the order by backend, and the
// class's backend must be on the door side of that choice, or a turn that
// waited out a lock-hold starts a task after the door told its caller it
// did not.
func TestA2AGoogleTurnClockStartsBeforeTheSessionLock(t *testing.T) {
	r, _ := startA2AGoogleRig(t)
	caller := a2aGoogleCaller(googleTestEmail)
	key := a2aConversationKey(caller, "held-lock")
	msg := InboundMessage{
		Conversation: key,
		Kind:         a2aConversationKind,
		AuthorID:     caller,
		MessageID:    "held-lock-1",
		Text:         "how is the fleet?",
		Backend:      a2aGoogleBackend,
	}
	const budget = 500 * time.Millisecond
	r.g.turnBudget = budget
	l := r.g.lockSession(key)
	l.Lock()

	done := make(chan struct{})
	go func() {
		defer close(done)
		r.g.handleInbound(msg)
	}()
	select {
	case <-done:
		t.Fatal("the turn returned while the session lock was still held")
	case <-time.After(4 * budget):
	}
	l.Unlock()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the turn did not return once the lock was released")
	}
	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 0 {
		t.Fatalf("a turn whose clock ran out waiting for the lock reached the bus: %d envelopes", len(envs))
	}
}

// TestA2AGoogleRefusalsTellTheOperatorWhatToFix: the 403 for an account off
// the list logs the email the admin would add, and a bearer that is neither
// the static token nor a token Google accepts is told both doors exist.
func TestA2AGoogleRefusalsTellTheOperatorWhatToFix(t *testing.T) {
	f := newFakeTokeninfo(t)
	f.set(googleTestOtherToken, liveTokeninfo(googleTestClientID, googleTestOtherEmail))
	logs := &recordingHandler{}
	door, err := NewA2ADoor("127.0.0.1:0", a2aTestToken, A2ADoorOptions{
		GoogleClientID: googleTestClientID, GoogleAllowedUsers: []string{googleTestEmail}, Logger: slog.New(logs),
	})
	if err != nil {
		t.Fatal(err)
	}
	door.google.tokeninfoURL = f.srv.URL
	call := func(token string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		r := httptest.NewRequest(http.MethodPost, a2aRPCPath, nil)
		r.Header.Set(authorizationHeader, "Bearer "+token)
		door.identify(w, r)
		return w
	}
	if w := call(googleTestOtherToken); w.Code != http.StatusForbidden {
		t.Fatalf("off-list account: HTTP %d, want 403", w.Code)
	}
	if !recordedAttr(logs, "email", googleTestOtherEmail) {
		t.Error("the 403's log line does not name the refused email")
	}
	w := call("stale-static-token")
	if w.Code != http.StatusUnauthorized || !strings.Contains(w.Body.String(), "neither the A2A door's token nor a Google access token") {
		t.Errorf("a stale static token: HTTP %d %q, want a 401 naming both kinds of bearer", w.Code, w.Body.String())
	}
}

// recordedAttr reports whether any record h kept carries key=value. A
// function rather than a recordingHandler method, so it cannot collide with
// one another change adds.
func recordedAttr(h *recordingHandler, key, value string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, r := range h.records {
		found := false
		r.Attrs(func(a slog.Attr) bool {
			found = a.Key == key && a.Value.String() == value
			return !found
		})
		if found {
			return true
		}
	}
	return false
}
