package tokenreview

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"

	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/rest"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	testContentTypeJSON   = "application/json"
	testHeaderContentType = "Content-Type"
	testAudience          = "kube-agents-k8s-api-proxy"
	testReviewerToken     = "proxy-sa-token"
	testCallerToken       = "caller-token"
	shellUsername         = "system:serviceaccount:agents:shell"
	tokenReviewPath       = "/apis/authentication.k8s.io/v1/tokenreviews"
	podsPathPrefix        = "/api/v1/namespaces/"
	// The shell's pod, which shellReview names, at the address httptest.NewRequest gives
	// every request's peer.
	shellPodNamespace = "agents"
	shellPodName      = "shell-0"
	shellPodUID       = "pod-uid-1"
	shellPodIP        = "192.0.2.1"
	// The Basic credentials are fake. This package's tests build their Basic headers at run
	// time with basicAuthHeader, so no literal in this package reads as a credential to a
	// secret scanner.
	placeholderBasicUser = "placeholder-user"
	placeholderBasicPass = "placeholder-pass"
)

// jsonContent makes the client send JSON, which the fake decodes; it defaults to protobuf.
var jsonContent = rest.ContentConfig{ContentType: testContentTypeJSON}

// fakeTokenReviewServer answers TokenReviews with status and records each request. It also
// answers a GET of a pod in pods (keyed namespace/name), or with podCode when that is set;
// shellPod when pods is nil.
type fakeTokenReviewServer struct {
	mu       sync.Mutex
	status   authenticationv1.TokenReviewStatus
	code     int
	reviews  []authenticationv1.TokenReview
	authzHdr []string
	pods     map[string]*corev1.Pod
	podCode  int
	podGets  int
}

func (f *fakeTokenReviewServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, podsPathPrefix) {
		f.servePod(w, r)
		return
	}
	if r.Method != http.MethodPost || r.URL.Path != tokenReviewPath {
		http.NotFound(w, r)
		return
	}
	var review authenticationv1.TokenReview
	if err := json.NewDecoder(r.Body).Decode(&review); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	f.reviews = append(f.reviews, review)
	f.authzHdr = append(f.authzHdr, r.Header.Get("Authorization"))
	if f.code != 0 {
		w.WriteHeader(f.code)
		return
	}
	review.Status = f.status
	w.Header().Set(testHeaderContentType, testContentTypeJSON)
	_ = json.NewEncoder(w).Encode(review)
}

// servePod answers GET /api/v1/namespaces/<namespace>/pods/<name>.
func (f *fakeTokenReviewServer) servePod(w http.ResponseWriter, r *http.Request) {
	f.podGets++
	if f.podCode != 0 {
		w.WriteHeader(f.podCode)
		return
	}
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, podsPathPrefix), "/")
	if len(parts) != 3 || parts[1] != "pods" {
		http.NotFound(w, r)
		return
	}
	pods := f.pods
	if pods == nil {
		pods = map[string]*corev1.Pod{shellPodNamespace + "/" + shellPodName: shellPod()}
	}
	pod, ok := pods[parts[0]+"/"+parts[2]]
	if !ok {
		w.Header().Set(testHeaderContentType, testContentTypeJSON)
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"kind":"Status","apiVersion":"v1","status":"Failure","reason":"NotFound","code":404}`)
		return
	}
	w.Header().Set(testHeaderContentType, testContentTypeJSON)
	_ = json.NewEncoder(w).Encode(pod)
}

func (f *fakeTokenReviewServer) podCalls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.podGets
}

func (f *fakeTokenReviewServer) calls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.reviews)
}

func shellReview() authenticationv1.TokenReviewStatus {
	return authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     []string{testAudience},
		User: authenticationv1.UserInfo{
			Username: shellUsername, UID: "uid-1", Groups: []string{"system:serviceaccounts"},
			Extra: podExtra(shellPodName, shellPodUID),
		},
	}
}

// podExtra is the user.extra a TokenReview fills for a token issued to the pod name/uid.
func podExtra(name, uid string) map[string]authenticationv1.ExtraValue {
	return map[string]authenticationv1.ExtraValue{extraPodName: {name}, extraPodUID: {uid}}
}

// shellPod is the running pod shellReview names, with shellPodIP.
func shellPod() *corev1.Pod {
	return testPod(shellPodNamespace, shellPodName, shellPodUID, corev1.PodRunning, shellPodIP)
}

func testPod(namespace, name, uid string, phase corev1.PodPhase, ips ...string) *corev1.Pod {
	pod := &corev1.Pod{}
	pod.Namespace, pod.Name, pod.UID = namespace, name, types.UID(uid)
	pod.Status.Phase = phase
	for _, ip := range ips {
		pod.Status.PodIPs = append(pod.Status.PodIPs, corev1.PodIP{IP: ip})
	}
	if len(ips) > 0 {
		pod.Status.PodIP = ips[0]
	}
	return pod
}

func newTestTokenReviewAuthenticator(t *testing.T, fake *fakeTokenReviewServer) k8sapiproxy.CallerAuthenticator {
	t.Helper()
	srv := httptest.NewServer(fake)
	t.Cleanup(srv.Close)
	a, err := NewAuthenticator(Config{
		Kubernetes:     &rest.Config{Host: srv.URL, BearerToken: testReviewerToken, ContentConfig: jsonContent},
		Audience:       testAudience,
		AllowedCallers: []string{shellUsername},
	})
	if err != nil {
		t.Fatalf("NewAuthenticator: %v", err)
	}
	return a
}

// basicAuthHeader returns an RFC 7617 Basic credential for an Authorization-style header.
func basicAuthHeader(user, pass string) string {
	return "Basic " + base64.StdEncoding.EncodeToString([]byte(user+":"+pass))
}

func bearerRequest(token string) *http.Request {
	r := httptest.NewRequest(http.MethodGet, "/clusters/in-cluster/api/v1/pods", nil)
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	return r
}

func TestNewTokenReviewAuthenticatorConfig(t *testing.T) {
	valid := func() Config {
		return Config{Kubernetes: &rest.Config{Host: "https://127.0.0.1"}, Audience: testAudience, AllowedCallers: []string{shellUsername}}
	}
	if _, err := NewAuthenticator(valid()); err != nil {
		t.Fatalf("valid config: %v", err)
	}
	for name, mutate := range map[string]func(*Config){
		"no Kubernetes": func(c *Config) { c.Kubernetes = nil },
		"no audience":   func(c *Config) { c.Audience = "  " },
		"no callers":    func(c *Config) { c.AllowedCallers = nil },
		"blank callers": func(c *Config) { c.AllowedCallers = []string{"", " "} },
	} {
		t.Run(name, func(t *testing.T) {
			cfg := valid()
			mutate(&cfg)
			if _, err := NewAuthenticator(cfg); err == nil {
				t.Error("NewAuthenticator accepted the config")
			}
		})
	}
}

func TestTokenReviewAuthenticatorAccepts(t *testing.T) {
	fake := &fakeTokenReviewServer{status: shellReview()}
	a := newTestTokenReviewAuthenticator(t, fake)

	id, err := a.AuthenticateRequest(bearerRequest(testCallerToken))
	if err != nil {
		t.Fatalf("AuthenticateRequest: %v", err)
	}
	if id.Username != shellUsername || id.UID != "uid-1" || !slices.Equal(id.Groups, []string{"system:serviceaccounts"}) {
		t.Errorf("identity = %+v", id)
	}
	review := fake.reviews[0]
	if review.Spec.Token != testCallerToken || !slices.Equal(review.Spec.Audiences, []string{testAudience}) {
		t.Errorf("review spec = %+v, want the caller's token and audience %q", review.Spec, testAudience)
	}
	if fake.authzHdr[0] != "Bearer "+testReviewerToken {
		t.Errorf("review sent as %q, want the proxy's own token", fake.authzHdr[0])
	}

	// A second request with the same token is answered from the cache; another token is not.
	if _, err := a.AuthenticateRequest(bearerRequest(testCallerToken)); err != nil {
		t.Fatalf("cached AuthenticateRequest: %v", err)
	}
	if _, err := a.AuthenticateRequest(bearerRequest("other-token")); err != nil {
		t.Fatalf("AuthenticateRequest: %v", err)
	}
	if got := fake.calls(); got != 2 {
		t.Errorf("TokenReviews = %d, want 2", got)
	}
}

// The review's status.user.extra becomes CallerIdentity.Extra, as a copy each caller of
// AuthenticateRequest owns: the review is cached, and one request must not change the next's.
func TestTokenReviewAuthenticatorCarriesExtra(t *testing.T) {
	const podNameKey, podUIDKey = "authentication.kubernetes.io/pod-name", "authentication.kubernetes.io/pod-uid"
	// shellReview names the shell's pod in user.extra, as the pod check requires.
	fake := &fakeTokenReviewServer{status: shellReview()}
	a := newTestTokenReviewAuthenticator(t, fake)

	id, err := a.AuthenticateRequest(bearerRequest(testCallerToken))
	if err != nil {
		t.Fatalf("AuthenticateRequest: %v", err)
	}
	if len(id.Extra) != 2 || !slices.Equal(id.Extra[podNameKey], []string{shellPodName}) || !slices.Equal(id.Extra[podUIDKey], []string{shellPodUID}) {
		t.Fatalf("Extra = %v, want the review's user.extra", id.Extra)
	}

	id.Extra[podNameKey][0] = "another-pod"
	delete(id.Extra, podUIDKey)
	again, err := a.AuthenticateRequest(bearerRequest(testCallerToken))
	if err != nil {
		t.Fatalf("cached AuthenticateRequest: %v", err)
	}
	if !slices.Equal(again.Extra[podNameKey], []string{shellPodName}) || !slices.Equal(again.Extra[podUIDKey], []string{shellPodUID}) {
		t.Errorf("cached Extra = %v: a caller's change to its copy reached the cached review", again.Extra)
	}
	if got := fake.calls(); got != 1 {
		t.Errorf("TokenReviews = %d, want 1: the second request is the cache's", got)
	}
}

// Groups are the caller's own copy too, for the same reason.
func TestTokenReviewAuthenticatorCopiesGroups(t *testing.T) {
	fake := &fakeTokenReviewServer{status: shellReview()}
	a := newTestTokenReviewAuthenticator(t, fake)

	id, err := a.AuthenticateRequest(bearerRequest(testCallerToken))
	if err != nil {
		t.Fatalf("AuthenticateRequest: %v", err)
	}
	if !slices.Equal(id.Groups, []string{"system:serviceaccounts"}) {
		t.Fatalf("Groups = %v", id.Groups)
	}
	id.Groups[0] = "system:masters"
	again, err := a.AuthenticateRequest(bearerRequest(testCallerToken))
	if err != nil {
		t.Fatalf("cached AuthenticateRequest: %v", err)
	}
	if !slices.Equal(again.Groups, []string{"system:serviceaccounts"}) {
		t.Errorf("cached Groups = %v: a caller's change to its copy reached the cached review", again.Groups)
	}
	if got := fake.calls(); got != 1 {
		t.Errorf("TokenReviews = %d, want 1: the second request is the cache's", got)
	}
}

func TestTokenReviewAuthenticatorRefuses(t *testing.T) {
	tests := []struct {
		name   string
		status func(*authenticationv1.TokenReviewStatus)
		code   int
		// authorization replaces the caller's bearer token when set; "-" sends none.
		authorization string
		// reviews is the TokenReviews after two identical requests: a refusal is cached, a
		// failed review is not.
		reviews     int
		unavailable bool
	}{
		{name: "not authenticated", status: func(s *authenticationv1.TokenReviewStatus) {
			s.Authenticated, s.User, s.Error = false, authenticationv1.UserInfo{}, "token expired"
		}, reviews: 1},
		{name: "caller not allowed", status: func(s *authenticationv1.TokenReviewStatus) { s.User.Username = "system:serviceaccount:agents:other" }, reviews: 1},
		{name: "other audience", status: func(s *authenticationv1.TokenReviewStatus) { s.Audiences = []string{"https://kubernetes.default.svc"} }, reviews: 1},
		{name: "no audience in the review", status: func(s *authenticationv1.TokenReviewStatus) { s.Audiences = nil }, reviews: 1},
		{name: "review failed", code: http.StatusForbidden, reviews: 2, unavailable: true},
		{name: "no token", authorization: "-"},
		{name: "basic scheme", authorization: basicAuthHeader(placeholderBasicUser, placeholderBasicPass)},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			fake := &fakeTokenReviewServer{status: shellReview(), code: tc.code}
			if tc.status != nil {
				tc.status(&fake.status)
			}
			a := newTestTokenReviewAuthenticator(t, fake)
			for range 2 {
				r := bearerRequest(testCallerToken)
				switch tc.authorization {
				case "":
				case "-":
					r.Header.Del("Authorization")
				default:
					r.Header.Set("Authorization", tc.authorization)
				}
				id, err := a.AuthenticateRequest(r)
				if err == nil {
					t.Fatalf("AuthenticateRequest accepted the caller as %+v", id)
				}
				if strings.Contains(err.Error(), testCallerToken) {
					t.Errorf("error %q contains the caller's token", err)
				}
				if got := errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable); got != tc.unavailable {
					t.Errorf("error %q: unavailable = %t, want %t", err, got, tc.unavailable)
				}
			}
			if got := fake.calls(); got != tc.reviews {
				t.Errorf("TokenReviews = %d, want %d", got, tc.reviews)
			}
		})
	}
}

// recordingUpstream answers every forwarded request with an empty PodList and keeps the last one.
type recordingUpstream struct{ req *http.Request }

func (u *recordingUpstream) RoundTripCluster(_ context.Context, _ string, req *http.Request) (*http.Response, error) {
	u.req = req.Clone(context.Background())
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{testHeaderContentType: []string{testContentTypeJSON}},
		Body:       io.NopCloser(strings.NewReader(`{"kind":"PodList","apiVersion":"v1","items":[]}`)),
		Request:    req,
	}, nil
}

func newTestHandler(t *testing.T, a k8sapiproxy.CallerAuthenticator) (http.Handler, *recordingUpstream) {
	t.Helper()
	upstream := &recordingUpstream{}
	h, err := k8sapiproxy.NewHandler(k8sapiproxy.Config{
		Authenticator: a,
		Upstream:      upstream,
		Policy:        k8sapiproxy.Policy{Mode: k8sapiproxy.ModeReadOnlyWithDryRun, RedactSecrets: true},
		Logger:        slog.New(slog.DiscardHandler),
	})
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}
	return h, upstream
}

func serve(h http.Handler, req *http.Request) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestHandlerWithTokenReviewAuthenticator(t *testing.T) {
	a := newTestTokenReviewAuthenticator(t, &fakeTokenReviewServer{status: shellReview()})
	h, upstream := newTestHandler(t, a)
	if rec := serve(h, bearerRequest(testCallerToken)); rec.Code != http.StatusOK {
		t.Fatalf("code = %d: %s", rec.Code, rec.Body.String())
	}
	if got := upstream.req.Header.Get("Authorization"); got != "" {
		t.Errorf("upstream Authorization = %q, want it removed", got)
	}
	if rec := serve(h, bearerRequest("")); rec.Code != http.StatusUnauthorized {
		t.Errorf("code without a token = %d, want 401", rec.Code)
	}

	// A failed review is a 503 that does not name the API server.
	fake := &fakeTokenReviewServer{code: http.StatusInternalServerError}
	srv := httptest.NewServer(fake)
	t.Cleanup(srv.Close)
	a, err := NewAuthenticator(Config{
		Kubernetes: &rest.Config{Host: srv.URL, ContentConfig: jsonContent}, Audience: testAudience, AllowedCallers: []string{shellUsername},
	})
	if err != nil {
		t.Fatal(err)
	}
	h, _ = newTestHandler(t, a)
	rec := serve(h, bearerRequest(testCallerToken))
	if rec.Code != http.StatusServiceUnavailable || strings.Contains(rec.Body.String(), srv.Listener.Addr().String()) {
		t.Errorf("failed review: code %d, body %s", rec.Code, rec.Body.String())
	}
}

func TestTokenReviewAuthenticatorMultipleAudiences(t *testing.T) {
	const (
		sessionAudience = "kubeagents-credential-proxy-session"
		profileAudience = "kubeagents-credential-proxy-profile"
		sessionUsername = "system:serviceaccount:agents:agent-a2a-session"
		profileUsername = "system:serviceaccount:agents-profile-sre:sre-worker"
	)
	fake := &fakeTokenReviewServer{
		status: authenticationv1.TokenReviewStatus{
			Authenticated: true,
			Audiences:     []string{profileAudience},
			User:          authenticationv1.UserInfo{Username: profileUsername, UID: "uid-p", Extra: podExtra("sre-worker-0", "uid-pod-p")},
		},
		pods: map[string]*corev1.Pod{
			"agents-profile-sre/sre-worker-0": testPod("agents-profile-sre", "sre-worker-0", "uid-pod-p", corev1.PodRunning, shellPodIP),
		},
	}
	srv := httptest.NewServer(fake)
	t.Cleanup(srv.Close)
	a, err := NewAuthenticator(Config{
		Kubernetes:     &rest.Config{Host: srv.URL, BearerToken: testReviewerToken, ContentConfig: jsonContent},
		Audience:       testAudience,
		AllowedCallers: []string{shellUsername},
		AudienceCallers: map[string][]string{
			sessionAudience: {sessionUsername},
			profileAudience: {profileUsername},
		},
	})
	if err != nil {
		t.Fatalf("NewAuthenticator: %v", err)
	}
	id, err := a.AuthenticateRequest(bearerRequest("profile-token"))
	if err != nil {
		t.Fatalf("AuthenticateRequest: %v", err)
	}
	if id.Username != profileUsername {
		t.Errorf("Username = %q, want %q", id.Username, profileUsername)
	}
	if len(fake.reviews[0].Spec.Audiences) != 3 {
		t.Errorf("Audiences = %v, want 3 audiences", fake.reviews[0].Spec.Audiences)
	}

	// Cross-audience caller is rejected: shellUsername cannot present profileAudience.
	fake.status.User.Username = shellUsername
	if _, err := a.AuthenticateRequest(bearerRequest("cross-audience-token")); err == nil {
		t.Errorf("expected cross-audience caller %q on %q to be rejected", shellUsername, profileAudience)
	}

	// Colliding audience between Audience and AudienceCallers is rejected at construction.
	if _, err := NewAuthenticator(Config{
		Kubernetes:      &rest.Config{Host: srv.URL, ContentConfig: jsonContent},
		Audience:        testAudience,
		AllowedCallers:  []string{shellUsername},
		AudienceCallers: map[string][]string{testAudience: {sessionUsername}},
	}); err == nil {
		t.Errorf("expected duplicate audience %q in AudienceCallers to be rejected", testAudience)
	}

	// Overlapping caller across AllowedCallers and AudienceCallers is rejected at construction.
	if _, err := NewAuthenticator(Config{
		Kubernetes:      &rest.Config{Host: srv.URL, ContentConfig: jsonContent},
		Audience:        testAudience,
		AllowedCallers:  []string{shellUsername, sessionUsername},
		AudienceCallers: map[string][]string{sessionAudience: {sessionUsername}},
	}); err == nil {
		t.Errorf("expected caller %q bound to two audiences to be rejected", sessionUsername)
	}
}
