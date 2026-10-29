package k8sapiproxy

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"

	authenticationv1 "k8s.io/api/authentication/v1"
	"k8s.io/client-go/rest"
)

const (
	testAudience      = "kube-agents-k8s-api-proxy"
	testReviewerToken = "proxy-sa-token"
	testCallerToken   = "caller-token"
	shellUsername     = "system:serviceaccount:agents:shell"
	tokenReviewPath   = "/apis/authentication.k8s.io/v1/tokenreviews"
)

// fakeTokenReviewServer answers TokenReviews with status and records each request.
type fakeTokenReviewServer struct {
	mu       sync.Mutex
	status   authenticationv1.TokenReviewStatus
	code     int
	reviews  []authenticationv1.TokenReview
	authzHdr []string
}

func (f *fakeTokenReviewServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
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
	w.Header().Set(headerContentType, contentTypeJSON)
	_ = json.NewEncoder(w).Encode(review)
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
		User:          authenticationv1.UserInfo{Username: shellUsername, UID: "uid-1", Groups: []string{"system:serviceaccounts"}},
	}
}

func newTestTokenReviewAuthenticator(t *testing.T, fake *fakeTokenReviewServer) CallerAuthenticator {
	t.Helper()
	srv := httptest.NewServer(fake)
	t.Cleanup(srv.Close)
	a, err := NewTokenReviewAuthenticator(TokenReviewConfig{
		Kubernetes:     &rest.Config{Host: srv.URL, BearerToken: testReviewerToken},
		Audience:       testAudience,
		AllowedCallers: []string{shellUsername},
	})
	if err != nil {
		t.Fatalf("NewTokenReviewAuthenticator: %v", err)
	}
	return a
}

func bearerRequest(token string) *http.Request {
	r := httptest.NewRequest(http.MethodGet, "/api/v1/pods", nil)
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	return r
}

func TestNewTokenReviewAuthenticatorConfig(t *testing.T) {
	valid := func() TokenReviewConfig {
		return TokenReviewConfig{Kubernetes: &rest.Config{Host: "https://127.0.0.1"}, Audience: testAudience, AllowedCallers: []string{shellUsername}}
	}
	if _, err := NewTokenReviewAuthenticator(valid()); err != nil {
		t.Fatalf("valid config: %v", err)
	}
	for name, mutate := range map[string]func(*TokenReviewConfig){
		"no Kubernetes": func(c *TokenReviewConfig) { c.Kubernetes = nil },
		"no audience":   func(c *TokenReviewConfig) { c.Audience = "  " },
		"no callers":    func(c *TokenReviewConfig) { c.AllowedCallers = nil },
		"blank callers": func(c *TokenReviewConfig) { c.AllowedCallers = []string{"", " "} },
	} {
		t.Run(name, func(t *testing.T) {
			cfg := valid()
			mutate(&cfg)
			if _, err := NewTokenReviewAuthenticator(cfg); err == nil {
				t.Error("NewTokenReviewAuthenticator accepted the config")
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

func TestTokenReviewAuthenticatorRefuses(t *testing.T) {
	tests := []struct {
		name   string
		status func(*authenticationv1.TokenReviewStatus)
		code   int
		// authorization replaces the caller's bearer token when set; "-" sends none.
		authorization string
		reviews       int
	}{
		{name: "not authenticated", status: func(s *authenticationv1.TokenReviewStatus) {
			*s = authenticationv1.TokenReviewStatus{Error: "token expired"}
		}, reviews: 1},
		{name: "caller not allowed", status: func(s *authenticationv1.TokenReviewStatus) { s.User.Username = "system:serviceaccount:agents:other" }, reviews: 1},
		{name: "other audience", status: func(s *authenticationv1.TokenReviewStatus) { s.Audiences = []string{"https://kubernetes.default.svc"} }, reviews: 1},
		{name: "no audience in the review", status: func(s *authenticationv1.TokenReviewStatus) { s.Audiences = nil }, reviews: 1},
		{name: "review refused", code: http.StatusForbidden, reviews: 1},
		{name: "no token", authorization: "-"},
		{name: "basic scheme", authorization: "Basic dXNlcjpwYXNz"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			fake := &fakeTokenReviewServer{status: shellReview(), code: tc.code}
			if tc.status != nil {
				tc.status(&fake.status)
			}
			a := newTestTokenReviewAuthenticator(t, fake)
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
			if got := fake.calls(); got != tc.reviews {
				t.Errorf("TokenReviews = %d, want %d", got, tc.reviews)
			}
		})
	}
}

func TestHandlerWithTokenReviewAuthenticator(t *testing.T) {
	a := newTestTokenReviewAuthenticator(t, &fakeTokenReviewServer{status: shellReview()})
	h, upstream := newTestHandler(t, Config{Authenticator: a})
	if rec := serve(h, bearerRequest(testCallerToken)); rec.Code != http.StatusOK {
		t.Fatalf("code = %d: %s", rec.Code, rec.Body.String())
	}
	if got := upstream.req.Header.Get("Authorization"); got != "" {
		t.Errorf("upstream Authorization = %q, want it removed", got)
	}
	if rec := serve(h, bearerRequest("")); rec.Code != http.StatusUnauthorized {
		t.Errorf("code without a token = %d, want 401", rec.Code)
	}
}
