package k8sapiproxy

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	// testCredentialHeader stands in for a header that carries a caller's own credential to
	// Config.Authorize and that the upstream must not see.
	testCredentialHeader = "X-Test-Credential"
	testCredentialValue  = "for-the-hook-only"
	// refusedTenant is the tenant refuseTenant turns away.
	refusedTenant = "b"
	// hookDetail is text only the operator should read.
	hookDetail = "signing key kms://internal/key-7 rejected the credential"
)

// refuseTenant refuses every request of refusedTenant's callers and counts its calls.
func refuseTenant(calls *atomic.Int32) func(AuthorizeAttributes) error {
	return func(a AuthorizeAttributes) error {
		calls.Add(1)
		if a.Caller.Username == "actor/"+refusedTenant+"/session" {
			return errors.New(hookDetail)
		}
		return nil
	}
}

// The hook is handed the caller, the cluster, the classification and the request as it
// arrived, and a nil answer forwards the request.
func TestHandlerAuthorizeIsHandedTheRequest(t *testing.T) {
	caller := &CallerIdentity{Username: "system:serviceaccount:agents:shell", Extra: map[string][]string{"example.io/pod": {"p"}}}
	var got AuthorizeAttributes
	calls := 0
	h, upstream := newTestHandler(t, Config{
		Authenticator: staticAuthenticator{caller: caller},
		Authorize: func(a AuthorizeAttributes) error {
			calls++
			got = a
			return nil
		},
	})
	req := newClusterRequest(http.MethodGet, "/clusters/"+excludedID+"/apis/apps/v1/namespaces/team/deployments/web", nil)
	req.Header.Set(testCredentialHeader, testCredentialValue)

	if rec := serve(h, req); rec.Code != http.StatusOK {
		t.Fatalf("code = %d: %s", rec.Code, rec.Body.String())
	}
	if calls != 1 || upstream.calls != 1 {
		t.Fatalf("hook calls = %d, upstream calls = %d, want 1 and 1", calls, upstream.calls)
	}
	if got.Caller != caller {
		t.Errorf("Caller = %+v, want the authenticator's identity", got.Caller)
	}
	if got.ClusterID != excludedID {
		t.Errorf("ClusterID = %q, want %q", got.ClusterID, excludedID)
	}
	info := got.RequestInfo
	if info == nil || info.Verb != "get" || info.APIGroup != "apps" || info.Resource != "deployments" || info.Namespace != "team" || info.Name != "web" {
		t.Errorf("RequestInfo = %+v", info)
	}
	if info != nil && info.Path != "/apis/apps/v1/namespaces/team/deployments/web" {
		t.Errorf("RequestInfo.Path = %q, want the API path without the cluster prefix", info.Path)
	}
	if got.Request == nil || got.Request.Header.Get(testCredentialHeader) != testCredentialValue {
		t.Errorf("the hook's request does not carry %s", testCredentialHeader)
	}
	if got.Request != nil && !strings.HasPrefix(got.Request.URL.Path, "/clusters/"+excludedID+"/") {
		t.Errorf("Request.URL.Path = %q, want the path as received", got.Request.URL.Path)
	}
}

// Any error is a 403. The caller reads a fixed message, or an AuthorizeError's own; the
// audit event carries the error and the reason, and the rejection metric a closed label.
func TestHandlerAuthorizeRefusal(t *testing.T) {
	const customReason RejectionReason = "task.wrong-cluster"
	const customMessage = "this task may not reach that cluster"
	for _, tc := range []struct {
		name        string
		err         error
		wantReason  RejectionReason
		wantMessage string
		wantMetric  string
	}{
		{"a plain error", errors.New(hookDetail), ReasonAuthorizeRefused, authorizeRefusedMessage, `reason="authorize.refused",resource="pods",subresource="none",verb="list"} 1`},
		{"an empty AuthorizeError", &AuthorizeError{}, ReasonAuthorizeRefused, authorizeRefusedMessage, `reason="authorize.refused",resource="pods",subresource="none",verb="list"} 1`},
		{
			"a wrapped AuthorizeError",
			fmt.Errorf("%s: %w", hookDetail, &AuthorizeError{Reason: customReason, Message: customMessage}),
			customReason, customMessage, `reason="` + metricsLabelOther + `",resource="pods",subresource="none",verb="list"} 1`,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sink := &auditRecordingSink{}
			metrics := NewMetrics()
			h, upstream := newTestHandler(t, Config{
				Audit:     sink,
				Metrics:   metrics,
				Authorize: func(AuthorizeAttributes) error { return tc.err },
			})

			rec := serve(h, newClusterRequest(http.MethodGet, testPodsPath, nil))
			if rec.Code != http.StatusForbidden {
				t.Fatalf("code = %d, want 403: %s", rec.Code, rec.Body.String())
			}
			status := decodeStatus(t, rec)
			if status.Reason != metav1.StatusReasonForbidden || status.Message != tc.wantMessage {
				t.Errorf("Status = (%q, %q), want (%q, %q)", status.Reason, status.Message, metav1.StatusReasonForbidden, tc.wantMessage)
			}
			if strings.Contains(rec.Body.String(), "kms://") {
				t.Errorf("the hook's error text reached the caller: %s", rec.Body.String())
			}
			if upstream.calls != 0 {
				t.Errorf("upstream calls = %d for a refused request", upstream.calls)
			}

			events := sink.snapshot()
			if len(events) != 1 {
				t.Fatalf("audit events = %d, want 1", len(events))
			}
			ev := events[0]
			if ev.Outcome != OutcomeRefused || ev.Reason != tc.wantReason || ev.StatusCode != http.StatusForbidden {
				t.Errorf("audit = (%q, %q, %d), want (%q, %q, 403)", ev.Outcome, ev.Reason, ev.StatusCode, OutcomeRefused, tc.wantReason)
			}
			if !errors.Is(ev.Err, tc.err) {
				t.Errorf("audit Err = %v, want the hook's error", ev.Err)
			}
			if ev.Verb != "list" || ev.Resource != "pods" || ev.ClusterID != InClusterID {
				t.Errorf("audit = (%q, %q, %q), want the refused request classified", ev.Verb, ev.Resource, ev.ClusterID)
			}
			exposition := scrapeMetricsText(t, metrics)
			if want := `kubeagents_kube_proxy_rejected_requests_total{mode="read-only-dry-run",` + tc.wantMetric; !strings.Contains(exposition, want) {
				t.Errorf("metrics missing a rejection with %s\n%s", tc.wantMetric, exposition)
			}
		})
	}
}

// kubectl's discovery client cannot decode a Status, so a refusal there is plain text, as a
// scope refusal is.
func TestHandlerAuthorizeRefusalOnADiscoveryPathIsPlainText(t *testing.T) {
	h, upstream := newTestHandler(t, Config{Authorize: func(AuthorizeAttributes) error { return errors.New(hookDetail) }})
	for _, p := range []string{"/api", "/apis", "/apis/apps/v1", "/version"} {
		rec := serve(h, newClusterRequest(http.MethodGet, p, nil))
		if rec.Code != http.StatusForbidden {
			t.Fatalf("%s: code = %d, want 403", p, rec.Code)
		}
		if ct := rec.Header().Get(headerContentType); ct != contentTypeTextPlain {
			t.Errorf("%s: Content-Type = %q, want %q", p, ct, contentTypeTextPlain)
		}
		if body := rec.Body.String(); body != authorizeRefusedMessage+"\n" {
			t.Errorf("%s: body = %q, want the fixed message", p, body)
		}
	}
	if upstream.calls != 0 {
		t.Errorf("a refused discovery request reached the upstream")
	}
}

// The hook decides before the policy does, and only on requests that got as far as a
// caller, a cluster and a classification.
func TestHandlerAuthorizeOrder(t *testing.T) {
	var calls atomic.Int32
	sink := &auditRecordingSink{}
	h, upstream := newTestHandler(t, Config{
		Authenticator: tenantAuthenticator{},
		Audit:         sink,
		Authorize:     refuseTenant(&calls),
	})

	// No caller, no cluster, no classification: answered before the hook.
	for name, req := range map[string]*http.Request{
		"unauthenticated": newClusterRequest(http.MethodGet, testPodsPath, nil),
		"no cluster":      func() *http.Request { r := tenantRequest("a"); r.URL.Path = testPodsPath; return r }(),
		"unclassifiable": func() *http.Request {
			r := tenantRequest("a")
			r.URL.Path = testClusterPrefix + "/api/v1/watch"
			return r
		}(),
	} {
		if rec := serve(h, req); rec.Code == http.StatusOK || rec.Code == http.StatusForbidden {
			t.Errorf("%s: code = %d, want a 400 or 401", name, rec.Code)
		}
	}
	if got := calls.Load(); got != 0 {
		t.Fatalf("hook calls = %d before a request was authenticated, routed and classified, want 0", got)
	}

	// A write the policy would refuse too: the hook's refusal is the one recorded.
	write := tenantRequest(refusedTenant)
	write.Method = http.MethodDelete
	if rec := serve(h, write); rec.Code != http.StatusForbidden {
		t.Fatalf("refused tenant's delete: code = %d, want 403", rec.Code)
	}
	events := sink.snapshot()
	if last := events[len(events)-1]; last.Reason != ReasonAuthorizeRefused {
		t.Errorf("rejection reason = %q, want %q: the hook runs before the policy", last.Reason, ReasonAuthorizeRefused)
	}
	// The policy still applies to a caller the hook admits.
	allowed := tenantRequest("a")
	allowed.Method = http.MethodDelete
	if rec := serve(h, allowed); rec.Code != http.StatusForbidden {
		t.Fatalf("admitted tenant's delete: code = %d, want the policy's 403", rec.Code)
	}
	events = sink.snapshot()
	if last := events[len(events)-1]; last.Reason != ReasonVerbBlocked {
		t.Errorf("rejection reason = %q, want %q", last.Reason, ReasonVerbBlocked)
	}
	if upstream.calls != 0 {
		t.Errorf("upstream calls = %d, want 0", upstream.calls)
	}
}

// A response cached for a caller the hook admits is not served to one it refuses, though
// both ask for the same URL in the same partition.
func TestHandlerAuthorizeRefusalIsNotServedFromTheCache(t *testing.T) {
	var hookCalls, upstreamCalls atomic.Int32
	h, _ := newTestHandler(t, Config{
		Authenticator: tenantAuthenticator{},
		Upstream:      partitionEchoUpstream(&upstreamCalls, nil),
		Cache:         &CacheConfig{DefaultResourceTTL: testPartitionTTL},
		Authorize:     refuseTenant(&hookCalls),
	})

	for i, want := range []string{cacheStatusMiss, cacheStatusHit} {
		rec := serve(h, tenantRequest("a"))
		if rec.Code != http.StatusOK || rec.Header().Get(headerCacheStatus) != want {
			t.Fatalf("admitted request %d = (%d, Cache-Status %q), want (200, %q)", i, rec.Code, rec.Header().Get(headerCacheStatus), want)
		}
	}

	rec := serve(h, tenantRequest(refusedTenant))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("refused caller with a warm entry: code = %d, want 403: %s", rec.Code, rec.Body.String())
	}
	if cs := rec.Header().Get(headerCacheStatus); cs != "" {
		t.Errorf("Cache-Status = %q on a refusal, want none", cs)
	}
	if strings.Contains(rec.Body.String(), "PodList") {
		t.Errorf("the refused caller was served the cached response: %s", rec.Body.String())
	}
	if got := upstreamCalls.Load(); got != 1 {
		t.Errorf("upstream calls = %d, want 1 (the admitted caller's miss)", got)
	}
	// The hook ran for the cache hit as well: a cached answer does not skip it.
	if got := hookCalls.Load(); got != 3 {
		t.Errorf("hook calls = %d, want 3 (one per request)", got)
	}

	// The entry is still the admitted caller's to read.
	if rec := serve(h, tenantRequest("a")); rec.Header().Get(headerCacheStatus) != cacheStatusHit {
		t.Errorf("admitted caller after the refusal: Cache-Status = %q, want %q", rec.Header().Get(headerCacheStatus), cacheStatusHit)
	}
}

// A refused caller does not join a fetch in flight for the same URL: it is answered 403
// while the fetch is still held, and the upstream sees one request.
func TestHandlerAuthorizeRefusalDoesNotJoinAnInFlightFetch(t *testing.T) {
	var hookCalls, upstreamCalls atomic.Int32
	arrived := make(chan struct{}, 2)
	release := make(chan struct{})
	h, _ := newTestHandler(t, Config{
		Authenticator: tenantAuthenticator{},
		Upstream:      partitionEchoUpstream(&upstreamCalls, func() { arrived <- struct{}{}; <-release }),
		Cache:         &CacheConfig{},
		Authorize:     refuseTenant(&hookCalls),
	})

	admitted := make(chan *httptest.ResponseRecorder, 1)
	go func() { admitted <- serve(h, tenantRequest("a")) }()
	select {
	case <-arrived:
	case <-time.After(testArrivalTimeout):
		close(release)
		t.Fatal("the admitted request never reached the upstream")
	}

	refused := make(chan *httptest.ResponseRecorder, 1)
	go func() { refused <- serve(h, tenantRequest(refusedTenant)) }()
	select {
	case rec := <-refused:
		if rec.Code != http.StatusForbidden {
			t.Errorf("refused caller during the fetch: code = %d, want 403: %s", rec.Code, rec.Body.String())
		}
		if cs := rec.Header().Get(headerCacheStatus); cs != "" {
			t.Errorf("Cache-Status = %q on a refusal, want none", cs)
		}
	case <-time.After(testArrivalTimeout):
		close(release)
		<-admitted
		t.Fatal("the refused request waited on the admitted caller's fetch instead of being answered")
	}

	close(release)
	if rec := <-admitted; rec.Code != http.StatusOK {
		t.Errorf("admitted caller: code = %d, want 200", rec.Code)
	}
	if got := upstreamCalls.Load(); got != 1 {
		t.Errorf("upstream calls = %d, want 1", got)
	}
}

// The hook cannot take the body from the upstream or change what the handler classified.
func TestHandlerAuthorizeCannotConsumeTheBodyOrReclassify(t *testing.T) {
	const body = `{"kind":"ConfigMap","apiVersion":"v1","metadata":{"name":"c"}}`
	sink := &auditRecordingSink{}
	var read string
	h, upstream := newTestHandler(t, Config{
		Audit: sink,
		Authorize: func(a AuthorizeAttributes) error {
			b, _ := io.ReadAll(a.Request.Body)
			read = string(b)
			_ = a.Request.Body.Close()
			a.RequestInfo.Verb, a.RequestInfo.Resource = "get", "nothing"
			return nil
		},
	})

	rec := serve(h, newClusterRequest(http.MethodPost, "/api/v1/namespaces/default/configmaps?dryRun=All", strings.NewReader(body)))
	if rec.Code != http.StatusOK {
		t.Fatalf("code = %d: %s", rec.Code, rec.Body.String())
	}
	if read != "" {
		t.Errorf("the hook read %q from the body, want nothing", read)
	}
	if upstream.body != body {
		t.Errorf("upstream body = %q, want %q", upstream.body, body)
	}
	if ev := sink.snapshot()[0]; ev.Verb != "create" || ev.Resource != "configmaps" {
		t.Errorf("audit = (%q, %q), want (create, configmaps): the hook changed the handler's classification", ev.Verb, ev.Resource)
	}
}

// The headers Config.ExtraCallerHeaders names reach the authenticator and the hook and
// never the upstream, on a plain request, a watch and an upgrade.
func TestHandlerStripsExtraCallerHeaders(t *testing.T) {
	for _, tc := range []struct {
		name   string
		policy Policy
		method string
		target string
		header map[string]string
	}{
		{name: "plain", method: http.MethodGet, target: testPodsPath},
		{name: "watch", method: http.MethodGet, target: testPodsPath + "?watch=true"},
		{
			name:   "upgrade",
			policy: Policy{Mode: ModeReadWrite, AllowUpgradeSubresources: true},
			method: http.MethodPost,
			target: testPodsPath + "/p/exec?command=sh",
			header: map[string]string{"Upgrade": "SPDY/3.1", "Connection": "Upgrade"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var hookSaw string
			h, upstream := newTestHandler(t, Config{
				Authenticator: tenantAuthenticator{},
				Policy:        tc.policy,
				// Named in another case than the request carries them in.
				ExtraCallerHeaders: []string{strings.ToLower(testTenantHeader), strings.ToUpper(testCredentialHeader)},
				Authorize: func(a AuthorizeAttributes) error {
					hookSaw = a.Request.Header.Get(testCredentialHeader)
					return nil
				},
			})
			req := newClusterRequest(tc.method, tc.target, nil)
			req.Header.Set(testTenantHeader, "a")
			req.Header.Set(testCredentialHeader, testCredentialValue)
			req.Header.Set("User-Agent", "kubectl/v1.37.0")
			for k, v := range tc.header {
				req.Header.Set(k, v)
			}

			// A 200 says the authenticator read the tenant header: without it, it answers 401.
			if rec := serve(h, req); rec.Code != http.StatusOK {
				t.Fatalf("code = %d: %s", rec.Code, rec.Body.String())
			}
			if hookSaw != testCredentialValue {
				t.Errorf("the hook saw %s = %q, want %q", testCredentialHeader, hookSaw, testCredentialValue)
			}
			if upstream.calls != 1 {
				t.Fatalf("upstream calls = %d, want 1", upstream.calls)
			}
			for _, name := range []string{testTenantHeader, testCredentialHeader} {
				if got := upstream.req.Header.Values(name); len(got) != 0 {
					t.Errorf("%s reached the upstream: %q", name, got)
				}
			}
			if upstream.req.Header.Get("User-Agent") == "" {
				t.Error("User-Agent was dropped")
			}
			if tc.header != nil && upstream.req.Header.Get("Upgrade") != tc.header["Upgrade"] {
				t.Errorf("upstream Upgrade = %q, want %q", upstream.req.Header.Get("Upgrade"), tc.header["Upgrade"])
			}
		})
	}
}

// Without Config.ExtraCallerHeaders the same header is forwarded: the list is what strips it.
func TestHandlerForwardsAHeaderNotNamed(t *testing.T) {
	h, upstream := newTestHandler(t, Config{})
	req := newClusterRequest(http.MethodGet, testPodsPath, nil)
	req.Header.Set(testCredentialHeader, testCredentialValue)
	if rec := serve(h, req); rec.Code != http.StatusOK {
		t.Fatalf("code = %d: %s", rec.Code, rec.Body.String())
	}
	if got := upstream.req.Header.Get(testCredentialHeader); got != testCredentialValue {
		t.Errorf("upstream %s = %q, want %q", testCredentialHeader, got, testCredentialValue)
	}
}

func TestSanitizeRequestHeadersStripsExtraNamesInAnyCase(t *testing.T) {
	extra, err := newExtraCallerHeaders([]string{"X-Test-Credential", "x-test-credential", "X-Other"})
	if err != nil {
		t.Fatalf("newExtraCallerHeaders: %v", err)
	}
	if len(extra) != 2 {
		t.Errorf("names = %v, want the duplicate folded", extra)
	}
	// Keys a server would have canonicalized, and ones set past it.
	h := http.Header{"X-Test-Credential": {"a"}, "x-test-credential": {"b"}, "X-OTHER": {"c"}, "X-Test-Credential-2": {"kept"}, "Accept": {"kept"}}
	sanitizeRequestHeaders(h, extra, false)
	if len(h) != 2 || h.Get("Accept") != "kept" || h.Get("X-Test-Credential-2") != "kept" {
		t.Errorf("headers = %v, want only Accept and X-Test-Credential-2: a name is not a prefix", h)
	}
}

func TestNewHandlerValidatesExtraCallerHeaders(t *testing.T) {
	base := Config{Authenticator: shellCaller, Upstream: &recordingTransport{}, Policy: Policy{Mode: ModeReadOnly}}
	for _, names := range [][]string{nil, {}, {"X-Test-Credential"}, {"x-test-credential", "X-Forwarded-Client-Cert"}, {"Authorization"}} {
		cfg := base
		cfg.ExtraCallerHeaders = names
		if _, err := NewHandler(cfg); err != nil {
			t.Errorf("ExtraCallerHeaders %q: NewHandler = %v", names, err)
		}
	}
	for _, name := range []string{
		"", " ", "X Test", " X-Test", "X-Test:", "X-Test\n", "X-Tést", "X-A,X-B",
		"Host", "content-length", "Transfer-Encoding", "CONNECTION", "upgrade",
	} {
		cfg := base
		cfg.ExtraCallerHeaders = []string{"X-Test-Credential", name}
		if _, err := NewHandler(cfg); err == nil {
			t.Errorf("ExtraCallerHeaders entry %q: NewHandler accepted it", name)
		}
	}
}
