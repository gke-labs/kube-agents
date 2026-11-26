package taskgate

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	// testUserHeader names the caller to testAuthenticator, standing in for a bearer token.
	testUserHeader   = "X-Test-User"
	testOtherPods    = "/api/v1/namespaces/kube-system/pods"
	testPodList      = `{"kind":"PodList","apiVersion":"v1","metadata":{"resourceVersion":"1"},"items":[]}`
	testContentType  = "application/json"
	testCacheTTL     = time.Minute
	testCacheStatus  = "Cache-Status"
	testCacheHit     = "k8s-api-proxy; hit"
	testListKind     = "PodList"
	testForwardedHdr = "X-Test-Forwarded"
	// testForwardedValue is what testForwardedHdr carries, to show a request went upstream
	// as it was sent.
	testForwardedValue = "yes"
)

// testAuthenticator admits the caller testUserHeader names; testWorker is bound to its pod,
// as a TokenReview of its projected token would say.
type testAuthenticator struct{}

func (testAuthenticator) AuthenticateRequest(r *http.Request) (*k8sapiproxy.CallerIdentity, error) {
	switch user := r.Header.Get(testUserHeader); user {
	case testWorker:
		return &k8sapiproxy.CallerIdentity{Username: user, Extra: testExtra()}, nil
	case testNonWorker:
		return &k8sapiproxy.CallerIdentity{Username: user}, nil
	}
	return nil, errors.New("no caller")
}

// testUpstream answers every forwarded request with an empty PodList and keeps the
// headers it was sent.
type testUpstream struct {
	mu      sync.Mutex
	headers []http.Header
}

func (u *testUpstream) RoundTripCluster(_ context.Context, _ string, req *http.Request) (*http.Response, error) {
	u.mu.Lock()
	u.headers = append(u.headers, req.Header.Clone())
	u.mu.Unlock()
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{testContentType}},
		Body:       io.NopCloser(strings.NewReader(testPodList)),
		Request:    req,
	}, nil
}

func (u *testUpstream) sent() []http.Header {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([]http.Header(nil), u.headers...)
}

// testAudit keeps every audit event the handler records.
type testAudit struct {
	mu     sync.Mutex
	events []k8sapiproxy.AuditEvent
}

func (a *testAudit) Record(_ context.Context, ev k8sapiproxy.AuditEvent) {
	a.mu.Lock()
	a.events = append(a.events, ev)
	a.mu.Unlock()
}

func (a *testAudit) last(t *testing.T) k8sapiproxy.AuditEvent {
	t.Helper()
	a.mu.Lock()
	defer a.mu.Unlock()
	if len(a.events) == 0 {
		t.Fatal("no audit event was recorded")
	}
	return a.events[len(a.events)-1]
}

func serveAs(h http.Handler, user, path string, credentials ...string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, "/clusters/"+testClusterID+path, nil)
	req.Header.Set(testUserHeader, user)
	req.Header.Set(testForwardedHdr, testForwardedValue)
	for _, credential := range credentials {
		req.Header.Add(CredentialHeader, credential)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

// Through the proxy's handler: a worker without its task's credential is refused before
// the cache, though the response it asked for is already there; the refusal's reason and
// detail reach the audit record and only the fixed message reaches the caller; and an
// admitted worker's credential header is not forwarded.
func TestHandlerRefusesBeforeTheCacheAndStripsTheCredential(t *testing.T) {
	kp, pub := newTestKey(t)
	upstream, audit := &testUpstream{}, &testAudit{}
	h, err := k8sapiproxy.NewHandler(k8sapiproxy.Config{
		Authenticator: testAuthenticator{},
		Upstream:      upstream,
		Policy:        k8sapiproxy.Policy{Mode: k8sapiproxy.ModeReadOnlyWithDryRun, RedactSecrets: true},
		Audit:         audit,
		Logger:        slog.New(slog.DiscardHandler),
		Cache:         &k8sapiproxy.CacheConfig{DefaultResourceTTL: testCacheTTL},
		Authorize: New(Config{Identities: testIdentities(), SigningKeys: []string{pub}, MaxLifetime: testLifetime},
			slog.New(slog.DiscardHandler), nil),
		ExtraCallerHeaders: CallerHeaders(),
		DefaultClusterID:   k8sapiproxy.InClusterID,
	})
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}

	// A caller the gate does not name warms the cache.
	for i := 0; i < 2; i++ {
		if rec := serveAs(h, testNonWorker, testPodsPath); rec.Code != http.StatusOK {
			t.Fatalf("non-worker request %d: code = %d: %s", i, rec.Code, rec.Body.String())
		}
	}
	if got := len(upstream.sent()); got != 1 {
		t.Fatalf("upstream calls = %d after two identical reads, want 1: the cache is not warm", got)
	}

	// The worker asks for the cached URL with no credential, then with one for another pod.
	otherPod := testClaims()
	otherPod.Pod = "auditor-read-other"
	foreign := mintTest(t, kp, otherPod)
	for _, tc := range []struct {
		name        string
		credentials []string
		detail      string
	}{
		{"no credential", nil, "no task credential"},
		{"another pod's credential", []string{foreign}, "another pod"},
	} {
		rec := serveAs(h, testWorker, testPodsPath, tc.credentials...)
		if rec.Code != http.StatusForbidden {
			t.Fatalf("%s: code = %d, want 403: %s", tc.name, rec.Code, rec.Body.String())
		}
		body := rec.Body.String()
		if strings.Contains(body, testListKind) || rec.Header().Get(testCacheStatus) != "" {
			t.Errorf("%s: the refused worker was served from the cache (Cache-Status %q): %s", tc.name, rec.Header().Get(testCacheStatus), body)
		}
		var status struct {
			Message string `json:"message"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &status); err != nil || status.Message != messageTaskCredential {
			t.Errorf("%s: caller-facing message = %q (%v), want the fixed %q", tc.name, status.Message, err, messageTaskCredential)
		}
		ev := audit.last(t)
		if ev.Reason != ReasonTaskCredential || ev.Err == nil || !strings.Contains(ev.Err.Error(), tc.detail) {
			t.Errorf("%s: audit event = (reason %q, err %v), want %q and a cause holding %q", tc.name, ev.Reason, ev.Err, ReasonTaskCredential, tc.detail)
		}
		if strings.Contains(body, tc.detail) {
			t.Errorf("%s: the caller was shown the audit detail: %s", tc.name, body)
		}
		for _, credential := range tc.credentials {
			assertNoCredentialText(t, body, credential)
			if ev.Err != nil {
				assertNoCredentialText(t, ev.Err.Error(), credential)
			}
		}
	}
	if got := len(upstream.sent()); got != 1 {
		t.Errorf("upstream calls = %d after the refusals, want 1: a refused request was forwarded", got)
	}

	// With its task's credential the worker reads the same entry, so the refusals above
	// had a warm entry to be served from.
	credential := mintTest(t, kp, testClaims())
	rec := serveAs(h, testWorker, testPodsPath, credential)
	if rec.Code != http.StatusOK || rec.Header().Get(testCacheStatus) != testCacheHit {
		t.Fatalf("admitted worker on the cached URL = (%d, Cache-Status %q), want (200, %q): %s",
			rec.Code, rec.Header().Get(testCacheStatus), testCacheHit, rec.Body.String())
	}

	// A URL the cache does not hold goes upstream, without the credential.
	if rec := serveAs(h, testWorker, testOtherPods, credential); rec.Code != http.StatusOK {
		t.Fatalf("admitted worker on an uncached URL: code = %d: %s", rec.Code, rec.Body.String())
	}
	sent := upstream.sent()
	if len(sent) != 2 {
		t.Fatalf("upstream calls = %d, want 2", len(sent))
	}
	if got := sent[1].Get(testForwardedHdr); got != testForwardedValue {
		t.Errorf("upstream %s = %q, want %q: the request was not forwarded as sent", testForwardedHdr, got, testForwardedValue)
	}
	for _, headers := range sent {
		if got := headers.Values(CredentialHeader); len(got) != 0 {
			t.Errorf("the upstream was sent %s", CredentialHeader)
		}
		for name, values := range headers {
			for _, value := range values {
				if strings.Contains(value, credential) {
					t.Errorf("the upstream was sent the credential in %s", name)
				}
			}
		}
	}

	// The same credential on the default cluster's path is refused: that path names no
	// cluster the credential could match.
	req := httptest.NewRequest(http.MethodGet, testPodsPath, nil)
	req.Header.Set(testUserHeader, testWorker)
	req.Header.Set(CredentialHeader, credential)
	bare := httptest.NewRecorder()
	h.ServeHTTP(bare, req)
	if bare.Code != http.StatusForbidden || audit.last(t).Reason != ReasonTaskTarget {
		t.Errorf("worker on a path with no cluster = (%d, reason %q), want (403, %q): %s",
			bare.Code, audit.last(t).Reason, ReasonTaskTarget, bare.Body.String())
	}
}
