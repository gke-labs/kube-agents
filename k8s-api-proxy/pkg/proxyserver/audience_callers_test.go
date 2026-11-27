package proxyserver

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"

	authenticationv1 "k8s.io/api/authentication/v1"
	"k8s.io/client-go/kubernetes/scheme"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	testEmbedderAudience = "embedder-audience"
	testSessionAudience  = "session-audience"
	testEmbedderCaller   = "system:serviceaccount:ns:worker"
	testSessionCaller    = "system:serviceaccount:ns:session"
	testUnlistedCaller   = "system:serviceaccount:ns:stranger"
	testUnusedAPIServer  = "http://127.0.0.1:18080"
)

// reviewedToken is what the test API server's TokenReview says of one bearer token.
type reviewedToken struct {
	username string
	audience string
}

// newReviewServer is an API server that reviews tokens as the real one does: a token is
// authenticated only for an audience the review asks about. Every token is bound to the pod
// testCallerPod describes, at the address httptest.NewRequest gives a request's peer, so
// the pod binding passes for each caller in the namespace "ns".
func newReviewServer(t *testing.T, tokens map[string]reviewedToken) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", testContentJSON)
		if r.Method == http.MethodGet && r.URL.Path == testCallerPodPath {
			_, _ = io.WriteString(w, testCallerPod)
			return
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		// The client sends protobuf unless told otherwise; this reads either encoding.
		obj, _, err := scheme.Codecs.UniversalDeserializer().Decode(body, nil, nil)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		review, ok := obj.(*authenticationv1.TokenReview)
		if !ok {
			http.Error(w, "not a TokenReview", http.StatusBadRequest)
			return
		}
		token, known := tokens[review.Spec.Token]
		if !known || !slices.Contains(review.Spec.Audiences, token.audience) {
			_, _ = io.WriteString(w, testReviewRefused)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"apiVersion": "authentication.k8s.io/v1",
			"kind":       "TokenReview",
			"status": map[string]any{
				"authenticated": true,
				"audiences":     []string{token.audience},
				"user": map[string]any{
					"username": token.username,
					"extra": map[string][]string{
						"authentication.kubernetes.io/pod-name": {"caller-0"},
						"authentication.kubernetes.io/pod-uid":  {"caller-uid"},
					},
				},
			},
		})
	}))
	t.Cleanup(srv.Close)
	return srv
}

// tokenReviewArgs are the flags of a proxy in front of upstreamURL that reviews caller
// tokens at apiServerURL and admits testReviewCaller for testReviewAudience.
func tokenReviewArgs(upstreamURL, apiServerURL string, more ...string) []string {
	return append([]string{
		"--upstream-url=" + upstreamURL,
		"--allowed-callers=" + testReviewCaller,
		"--audience=" + testReviewAudience,
		"--kube-api-server=" + apiServerURL,
		"--kube-ca-file=",
		"--kube-token-file=",
	}, more...)
}

// assembled is the configuration Run assembles from args in a binary that passes opts.
func assembled(t *testing.T, args []string, opts Options) proxyConfig {
	t.Helper()
	cfg, _, versionOnly, err := assemble(testFlagSet(), args, opts)
	if err != nil || versionOnly {
		t.Fatalf("assemble(%q) = versionOnly %v, err %v; want a configuration", args, versionOnly, err)
	}
	return cfg
}

// An embedder's audience admits the callers listed for it and nobody else, and leaves the
// server's own audience and the session audience admitting exactly who they did.
func TestOptionsAudienceCallersAdmitTheListedCallersOnly(t *testing.T) {
	clearProxyEnv(t)
	t.Setenv(sessionAudienceEnv, testSessionAudience)
	t.Setenv(sessionCallersEnv, testSessionCaller)
	up := newCountingUpstream(t)
	review := newReviewServer(t, map[string]reviewedToken{
		"own":                         {testReviewCaller, testReviewAudience},
		"session":                     {testSessionCaller, testSessionAudience},
		"listed":                      {testEmbedderCaller, testEmbedderAudience},
		"unlisted":                    {testUnlistedCaller, testEmbedderAudience},
		"own-caller-embedder-token":   {testReviewCaller, testEmbedderAudience},
		"session-embedder-token":      {testSessionCaller, testEmbedderAudience},
		"listed-caller-own-token":     {testEmbedderCaller, testReviewAudience},
		"listed-caller-session-token": {testEmbedderCaller, testSessionAudience},
	})
	args := tokenReviewArgs(up.srv.URL, review.URL, "--cache-max-bytes=0")
	logger := slog.New(slog.DiscardHandler)

	stock := buildTestHandler(t, assembled(t, args, Options{}), logger)
	embedded := buildTestHandler(t, assembled(t, args, Options{
		AudienceCallers: map[string][]string{testEmbedderAudience: {" " + testEmbedderCaller + " "}},
	}), logger)

	for _, tc := range []struct {
		token        string
		stock, added int
	}{
		{"own", http.StatusOK, http.StatusOK},
		{"session", http.StatusOK, http.StatusOK},
		{"listed", http.StatusUnauthorized, http.StatusOK},
		{"unlisted", http.StatusUnauthorized, http.StatusUnauthorized},
		{"own-caller-embedder-token", http.StatusUnauthorized, http.StatusUnauthorized},
		{"session-embedder-token", http.StatusUnauthorized, http.StatusUnauthorized},
		{"listed-caller-own-token", http.StatusUnauthorized, http.StatusUnauthorized},
		{"listed-caller-session-token", http.StatusUnauthorized, http.StatusUnauthorized},
		{"unknown", http.StatusUnauthorized, http.StatusUnauthorized},
	} {
		for _, h := range []struct {
			name    string
			handler http.Handler
			want    int
		}{
			{"without Options", stock, tc.stock},
			{"with AudienceCallers", embedded, tc.added},
		} {
			before := up.calls.Load()
			rec := getPods(h.handler, tc.token)
			if rec.Code != h.want {
				t.Errorf("token %q %s: code = %d, want %d; body = %s", tc.token, h.name, rec.Code, h.want, rec.Body.String())
			}
			if forwarded := up.calls.Load() - before; h.want != http.StatusOK && forwarded != 0 {
				t.Errorf("token %q %s: the upstream got %d requests for a refused caller", tc.token, h.name, forwarded)
			}
		}
	}
}

// A request from another address than the caller's pod is refused for an embedder's
// audience as it is for the server's own.
func TestOptionsAudienceCallersAreHeldToTheirPod(t *testing.T) {
	clearProxyEnv(t)
	up := newCountingUpstream(t)
	review := newReviewServer(t, map[string]reviewedToken{"listed": {testEmbedderCaller, testEmbedderAudience}})
	h := buildTestHandler(t, assembled(t, tokenReviewArgs(up.srv.URL, review.URL), Options{
		AudienceCallers: map[string][]string{testEmbedderAudience: {testEmbedderCaller}},
	}), slog.New(slog.DiscardHandler))

	req := httptest.NewRequest(http.MethodGet, testPodsPath, nil)
	req.Header.Set("Authorization", "Bearer listed")
	req.RemoteAddr = "198.51.100.7:40000"
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized || up.calls.Load() != 0 {
		t.Errorf("a listed caller from another address: code = %d with %d upstream calls, want 401 and none", rec.Code, up.calls.Load())
	}
}

func TestOptionsAudienceCallersFailStartup(t *testing.T) {
	jwtArgs := []string{"--auth-mode=jwt", "--upstream-url=" + testUnusedAPIServer, "--jwt-issuer=https://issuer.test", "--jwt-audiences=" + testJWTAudience}
	noneArgs := []string{"--auth-mode=none", "--listen-addr=127.0.0.1:18443", "--upstream-url=" + testUnusedAPIServer}
	listed := []string{testEmbedderCaller}
	for _, tc := range []struct {
		name    string
		args    []string
		env     map[string]string
		callers map[string][]string
		want    string
	}{
		{name: "the server's own audience", callers: map[string][]string{testReviewAudience: listed}, want: "already configured by --audience"},
		{
			name:    "one of the server's own audiences",
			args:    []string{"--audience=first-audience, " + testEmbedderAudience},
			callers: map[string][]string{testEmbedderAudience: listed},
			want:    "already configured by --audience",
		},
		{
			name:    "the session audience",
			env:     map[string]string{sessionAudienceEnv: " " + testSessionAudience + " ", sessionCallersEnv: testSessionCaller},
			callers: map[string][]string{testSessionAudience: listed},
			want:    "already configured by " + sessionAudienceEnv,
		},
		{name: "an empty audience", callers: map[string][]string{"": listed}, want: "is not one audience name"},
		{name: "a blank audience", callers: map[string][]string{"  ": listed}, want: "is not one audience name"},
		{name: "a padded audience", callers: map[string][]string{testEmbedderAudience + " ": listed}, want: "is not one audience name"},
		{name: "two audiences in one key", callers: map[string][]string{testEmbedderAudience + ",other": listed}, want: "is not one audience name"},
		{name: "no caller list", callers: map[string][]string{testEmbedderAudience: nil}, want: "names no caller"},
		{name: "an empty caller list", callers: map[string][]string{testEmbedderAudience: {}}, want: "names no caller"},
		{name: "a blank caller", callers: map[string][]string{testEmbedderAudience: {testEmbedderCaller, " "}}, want: "has a blank caller"},
		{name: "an empty caller", callers: map[string][]string{testEmbedderAudience: {""}}, want: "has a blank caller"},
		{
			name:    "a valid audience beside an invalid one",
			callers: map[string][]string{"a-" + testEmbedderAudience: listed, testEmbedderAudience: nil},
			want:    "names no caller",
		},
		{
			name:    "a caller the server's own audience admits",
			callers: map[string][]string{testEmbedderAudience: {testReviewCaller}},
			want:    "cannot be bound to both",
		},
		{
			name:    "a caller the session audience admits",
			env:     map[string]string{sessionAudienceEnv: testSessionAudience, sessionCallersEnv: testSessionCaller},
			callers: map[string][]string{testEmbedderAudience: {testSessionCaller}},
			want:    "cannot be bound to both",
		},
		{name: "auth mode none", args: noneArgs, callers: map[string][]string{testEmbedderAudience: listed}, want: "--auth-mode=none cannot be combined with Options.AudienceCallers"},
		{name: "auth mode jwt alone", args: jwtArgs, callers: map[string][]string{testEmbedderAudience: listed}, want: `Options.AudienceCallers requires "token-review"`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			clearProxyEnv(t)
			for name, value := range tc.env {
				t.Setenv(name, value)
			}
			args := tc.args
			if len(args) == 0 || strings.HasPrefix(args[0], "--audience=") {
				// A later --audience replaces the one tokenReviewArgs sets.
				args = tokenReviewArgs(testUnusedAPIServer, testUnusedAPIServer, tc.args...)
			}
			logger := slog.New(slog.DiscardHandler)

			// The same flags and environment start without the embedder's audiences.
			if _, err := buildHandler(context.Background(), assembled(t, args, Options{}), k8sapiproxy.ModeReadOnlyWithDryRun, logger, k8sapiproxy.NewMetrics(), nil); err != nil {
				t.Fatalf("buildHandler without Options: %v", err)
			}
			_, err := buildHandler(context.Background(), assembled(t, args, Options{AudienceCallers: tc.callers}), k8sapiproxy.ModeReadOnlyWithDryRun, logger, k8sapiproxy.NewMetrics(), nil)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Errorf("buildHandler with AudienceCallers %v: err = %v, want one containing %q", tc.callers, err, tc.want)
			}
		})
	}
}

// No embedder audiences is the stock server: the map the environment configured goes to
// the authenticator as it is, and an empty map is no audience under any auth mode.
func TestOptionsAudienceCallersUnsetChangeNothing(t *testing.T) {
	session := map[string][]string{testSessionAudience: {testSessionCaller}}
	for name, embedder := range map[string]map[string][]string{"nil": nil, "empty": {}} {
		got, err := mergeAudienceCallers(nil, embedder, testReviewAudience)
		if err != nil || got != nil {
			t.Errorf("%s map, no session audience: merged = %v, %v; want nil, as before", name, got, err)
		}
		got, err = mergeAudienceCallers(session, embedder, testReviewAudience)
		if err != nil || len(got) != 1 || !slices.Equal(got[testSessionAudience], session[testSessionAudience]) {
			t.Errorf("%s map: merged = %v, %v; want the session audience alone", name, got, err)
		}
	}

	// The embedder's map is not the one handed on, and its lists are not changed.
	embedder := map[string][]string{testEmbedderAudience: {" " + testEmbedderCaller}}
	got, err := mergeAudienceCallers(session, embedder, testReviewAudience)
	if err != nil {
		t.Fatalf("mergeAudienceCallers: %v", err)
	}
	if len(got) != 2 || !slices.Equal(got[testEmbedderAudience], []string{testEmbedderCaller}) || !slices.Equal(got[testSessionAudience], session[testSessionAudience]) {
		t.Errorf("merged = %v, want both audiences with their callers trimmed", got)
	}
	if len(session) != 1 || len(embedder) != 1 || embedder[testEmbedderAudience][0] != " "+testEmbedderCaller {
		t.Errorf("the inputs changed: session %v, embedder %v", session, embedder)
	}

	clearProxyEnv(t)
	up := newHeaderUpstream(t)
	cfg := embedderConfig(t, up.srv.URL, Options{AudienceCallers: map[string][]string{}})
	if rec := getPods(buildTestHandler(t, cfg, slog.New(slog.DiscardHandler)), testCallerToken); rec.Code != http.StatusOK {
		t.Errorf("--auth-mode=none with an empty AudienceCallers: code = %d, want 200", rec.Code)
	}
}
