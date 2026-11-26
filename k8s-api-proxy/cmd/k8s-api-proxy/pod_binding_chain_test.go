package main

import (
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4/jwt"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

// projectedToken is a token shaped like a pod's projected ServiceAccount token, signed by is:
// the kubernetes.io claim names the namespace, the pod and the ServiceAccount.
func (is *testJWTIssuer) projectedToken(audience string) string {
	is.t.Helper()
	now := time.Now()
	raw, err := jwt.Signed(is.signer).Claims(map[string]any{
		"iss": is.srv.URL,
		"sub": testReviewCaller,
		"aud": audience,
		"iat": now.Unix(),
		"exp": now.Add(testJWTTTL).Unix(),
		"kubernetes.io": map[string]any{
			"namespace":      "ns",
			"pod":            map[string]any{"name": "caller-0", "uid": "caller-uid"},
			"serviceaccount": map[string]any{"name": "sa", "uid": "sa-uid"},
		},
	}).Serialize()
	if err != nil {
		is.t.Fatal(err)
	}
	return raw
}

// A pod's token that TokenReview accepts but that comes from another address is refused,
// in either order of a chain whose jwt authenticator trusts the same issuer and audience:
// the token-review refusal is final, and jwt does not admit a projected ServiceAccount token.
func TestChainKeepsThePodBindingOfAProjectedToken(t *testing.T) {
	for _, order := range []string{"token-review,jwt", "jwt,token-review"} {
		t.Run(order, func(t *testing.T) {
			clearProxyEnv(t)
			is := newTestJWTIssuer(t)
			up := newCountingUpstream(t)
			apiServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", testContentJSON)
				if r.Method == http.MethodGet && r.URL.Path == testCallerPodPath {
					_, _ = io.WriteString(w, testCallerPod)
					return
				}
				_, _ = io.WriteString(w, testReviewAccepted)
			}))
			t.Cleanup(apiServer.Close)
			cfg := parseTestFlags(t, is.jwtArgs(up.srv.URL,
				"--auth-mode="+order,
				"--allowed-callers="+testReviewCaller,
				"--audience="+testReviewAudience,
				"--kube-api-server="+apiServer.URL,
				"--kube-ca-file=",
				"--kube-token-file=",
			)...)
			h := buildTestHandler(t, cfg, slog.New(slog.DiscardHandler))
			token := is.projectedToken(testJWTAudience)

			req := httptest.NewRequest(http.MethodGet, testPodsPath, nil)
			req.Header.Set("Authorization", "Bearer "+token)
			req.RemoteAddr = "198.51.100.7:40000"
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, req)
			if rec.Code != http.StatusUnauthorized {
				t.Errorf("a projected token from another address: code = %d, want 401", rec.Code)
			}
			if got := up.calls.Load(); got != 0 {
				t.Errorf("the upstream was called %d times, want 0", got)
			}
		})
	}
}

// The pod binding's refusal is final: a token the jwt authenticator admits on its own, which
// TokenReview identifies as a pod's token, is refused when it comes from another address,
// and jwt never sees it.
func TestChainStopsAtThePodBindingRefusal(t *testing.T) {
	clearProxyEnv(t)
	is := newTestJWTIssuer(t)
	up := newCountingUpstream(t)
	apiServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", testContentJSON)
		if r.Method == http.MethodGet && r.URL.Path == testCallerPodPath {
			_, _ = io.WriteString(w, testCallerPod)
			return
		}
		_, _ = io.WriteString(w, testReviewAccepted)
	}))
	t.Cleanup(apiServer.Close)
	// No kubernetes.io claim: the jwt authenticator admits it.
	token := is.sign(testReviewCaller, testJWTAudience, nil)
	fromElsewhere := func(h http.Handler) int {
		req := httptest.NewRequest(http.MethodGet, testPodsPath, nil)
		req.Header.Set("Authorization", "Bearer "+token)
		req.RemoteAddr = "198.51.100.7:40000"
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		return rec.Code
	}
	jwtOnly := buildTestHandler(t, parseTestFlags(t, is.jwtArgs(up.srv.URL)...), slog.New(slog.DiscardHandler))
	if code := fromElsewhere(jwtOnly); code != http.StatusOK {
		t.Fatalf("jwt alone: code = %d, want 200 (the token must be one jwt accepts)", code)
	}
	served := is.served.Load()
	chain := buildTestHandler(t, parseTestFlags(t, is.jwtArgs(up.srv.URL,
		"--auth-mode=token-review,jwt",
		"--allowed-callers="+testReviewCaller,
		"--audience="+testReviewAudience,
		"--kube-api-server="+apiServer.URL,
		"--kube-ca-file=",
		"--kube-token-file=",
	)...), slog.New(slog.DiscardHandler))
	before := up.calls.Load()
	if code := fromElsewhere(chain); code != http.StatusUnauthorized {
		t.Errorf("token-review,jwt: code = %d, want 401", code)
	}
	if got := up.calls.Load(); got != before {
		t.Errorf("the upstream was called %d more times, want 0", got-before)
	}
	if got := is.served.Load(); got != served {
		t.Errorf("the jwt issuer got %d more requests: the token reached jwt", got-served)
	}
}

// refusingAuthenticator refuses with err; acceptingAuthenticator admits everyone.
type refusingAuthenticator struct{ err error }

func (a refusingAuthenticator) AuthenticateRequest(*http.Request) (*k8sapiproxy.CallerIdentity, error) {
	return nil, a.err
}

type acceptingAuthenticator struct{ called *bool }

func (a acceptingAuthenticator) AuthenticateRequest(*http.Request) (*k8sapiproxy.CallerIdentity, error) {
	*a.called = true
	return &k8sapiproxy.CallerIdentity{Username: "anyone"}, nil
}

func TestChainAuthenticatorStopsAtAFinalRefusal(t *testing.T) {
	var called bool
	final := fmt.Errorf("%w: not from its pod", k8sapiproxy.ErrCallerRefused)
	chain := newChainAuthenticator([]k8sapiproxy.CallerAuthenticator{
		namedAuthenticator{name: authModeTokenReview, next: refusingAuthenticator{final}},
		namedAuthenticator{name: authModeJWT, next: acceptingAuthenticator{&called}},
	})
	caller, err := chain.AuthenticateRequest(httptest.NewRequest(http.MethodGet, "/", nil))
	if caller != nil || !errors.Is(err, k8sapiproxy.ErrCallerRefused) || called {
		t.Errorf("caller %v, err %v, next called %v; want a final refusal", caller, err, called)
	}
	// A plain refusal still falls through.
	chain = newChainAuthenticator([]k8sapiproxy.CallerAuthenticator{
		refusingAuthenticator{errors.New("unknown token")},
		acceptingAuthenticator{&called},
	})
	if caller, err := chain.AuthenticateRequest(httptest.NewRequest(http.MethodGet, "/", nil)); err != nil || caller == nil {
		t.Errorf("a plain refusal did not fall through: %v", err)
	}
}
