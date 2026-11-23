package k8sapiproxy

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const (
	testTenantHeader   = "X-Test-Tenant"
	testPartitionTTL   = time.Minute
	testArrivalTimeout = 2 * time.Second
	testPodsPath       = "/api/v1/namespaces/default/pods"
	// noPartition is what partitionEchoUpstream answers when the handler set none.
	noPartition = "none"
)

// tenantAuthenticator names the caller after the tenant header, standing in for an
// authenticator that maps a session token to a tenant.
type tenantAuthenticator struct{}

func (tenantAuthenticator) AuthenticateRequest(r *http.Request) (*CallerIdentity, error) {
	tenant := r.Header.Get(testTenantHeader)
	if tenant == "" {
		return nil, errors.New("no tenant")
	}
	return &CallerIdentity{Username: "actor/" + tenant + "/session", Groups: []string{"tenant:" + tenant}}, nil
}

// partitionByTenant puts every session of a tenant in one partition.
func partitionByTenant(c *CallerIdentity) string {
	return strings.TrimPrefix(c.Groups[0], "tenant:")
}

// partitionEchoUpstream answers with the partition it was handed, so a response shows
// which credential fetched it.
func partitionEchoUpstream(calls *atomic.Int32, gate func()) *fakeUpstream {
	return &fakeUpstream{handler: func(_ string, req *http.Request) (*http.Response, error) {
		calls.Add(1)
		if gate != nil {
			gate()
		}
		partition, ok := PartitionFromContext(req.Context())
		if !ok {
			partition = noPartition
		}
		body := `{"kind":"PodList","apiVersion":"v1","metadata":{"resourceVersion":"` + partition + `"},"items":[]}`
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{headerContentType: []string{contentTypeJSON}},
			Body:       io.NopCloser(strings.NewReader(body)),
		}, nil
	}}
}

func tenantRequest(tenant string) *http.Request {
	req := newClusterRequest(http.MethodGet, testPodsPath, nil)
	req.Header.Set(testTenantHeader, tenant)
	return req
}

func fetchedWith(t *testing.T, body, partition string) {
	t.Helper()
	if want := `"resourceVersion":"` + partition + `"`; !strings.Contains(body, want) {
		t.Errorf("body = %s, want one fetched with partition %q", body, partition)
	}
}

// A response stored for one partition is never served to another: with a per-tenant
// Upstream, it was fetched with the other tenant's credential.
func TestHandlerCallerPartitionSeparatesCachedResponses(t *testing.T) {
	var calls atomic.Int32
	h, _ := newTestHandler(t, Config{
		Authenticator:   tenantAuthenticator{},
		Upstream:        partitionEchoUpstream(&calls, nil),
		Cache:           &CacheConfig{DefaultResourceTTL: testPartitionTTL},
		CallerPartition: partitionByTenant,
	})

	a := serve(h, tenantRequest("a"))
	b := serve(h, tenantRequest("b"))
	again := serve(h, tenantRequest("a"))

	fetchedWith(t, a.Body.String(), "a")
	fetchedWith(t, b.Body.String(), "b")
	fetchedWith(t, again.Body.String(), "a")
	if got := b.Header().Get(headerCacheStatus); got == cacheStatusHit {
		t.Errorf("tenant b Cache-Status = %q, want a miss: the stored response is tenant a's", got)
	}
	if got := again.Header().Get(headerCacheStatus); got != cacheStatusHit {
		t.Errorf("tenant a's second Cache-Status = %q, want %q", got, cacheStatusHit)
	}
	if got := calls.Load(); got != 2 {
		t.Errorf("upstream calls = %d, want 2 (one per partition)", got)
	}
}

// Concurrent requests coalesce only within a partition. Coalescing runs even with
// resource caching off, so this holds under the default zero resource TTL.
func TestHandlerCallerPartitionSeparatesCoalescedFetches(t *testing.T) {
	var calls atomic.Int32
	arrived := make(chan struct{}, 2)
	release := make(chan struct{})
	h, _ := newTestHandler(t, Config{
		Authenticator:   tenantAuthenticator{},
		Upstream:        partitionEchoUpstream(&calls, func() { arrived <- struct{}{}; <-release }),
		Cache:           &CacheConfig{},
		CallerPartition: partitionByTenant,
	})

	bodies := make(map[string]string)
	var mu sync.Mutex
	var wg sync.WaitGroup
	for _, tenant := range []string{"a", "b"} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			rec := serve(h, tenantRequest(tenant))
			mu.Lock()
			bodies[tenant] = rec.Body.String()
			mu.Unlock()
		}()
	}
	for range 2 {
		select {
		case <-arrived:
		case <-time.After(testArrivalTimeout):
			close(release)
			wg.Wait()
			t.Fatalf("only %d of 2 requests reached the upstream: the partitions shared a fetch", calls.Load())
		}
	}
	close(release)
	wg.Wait()

	fetchedWith(t, bodies["a"], "a")
	fetchedWith(t, bodies["b"], "b")
}

// Two callers in one partition still share: the partition, not the caller, decides.
func TestHandlerCallerPartitionSharesWithinAPartition(t *testing.T) {
	var calls atomic.Int32
	h, _ := newTestHandler(t, Config{
		Authenticator:   tenantAuthenticator{},
		Upstream:        partitionEchoUpstream(&calls, nil),
		Cache:           &CacheConfig{DefaultResourceTTL: testPartitionTTL},
		CallerPartition: func(*CallerIdentity) string { return "shared" },
	})

	serve(h, tenantRequest("a"))
	rec := serve(h, tenantRequest("b"))

	if got := rec.Header().Get(headerCacheStatus); got != cacheStatusHit {
		t.Errorf("Cache-Status = %q, want %q", got, cacheStatusHit)
	}
	if got := calls.Load(); got != 1 {
		t.Errorf("upstream calls = %d, want 1", got)
	}
}

// Without CallerPartition the Upstream sees no partition and callers share, as before.
func TestHandlerWithoutCallerPartitionSharesAcrossCallers(t *testing.T) {
	var calls atomic.Int32
	h, _ := newTestHandler(t, Config{
		Authenticator: tenantAuthenticator{},
		Upstream:      partitionEchoUpstream(&calls, nil),
		Cache:         &CacheConfig{DefaultResourceTTL: testPartitionTTL},
	})

	a := serve(h, tenantRequest("a"))
	b := serve(h, tenantRequest("b"))

	fetchedWith(t, a.Body.String(), noPartition)
	if got := b.Header().Get(headerCacheStatus); got != cacheStatusHit {
		t.Errorf("Cache-Status = %q, want %q", got, cacheStatusHit)
	}
	if got := calls.Load(); got != 1 {
		t.Errorf("upstream calls = %d, want 1", got)
	}
}

// The Upstream gets the partition on a request the cache does not handle too.
func TestPartitionFromContextReachesTheUpstreamWithoutACache(t *testing.T) {
	var calls atomic.Int32
	h, _ := newTestHandler(t, Config{
		Authenticator:   tenantAuthenticator{},
		Upstream:        partitionEchoUpstream(&calls, nil),
		CallerPartition: partitionByTenant,
	})

	fetchedWith(t, serve(h, tenantRequest("b")).Body.String(), "b")
}

func TestPartitionFromContextReportsWhetherOneWasSet(t *testing.T) {
	if got, ok := PartitionFromContext(context.Background()); ok || got != "" {
		t.Errorf("PartitionFromContext(Background) = %q, %v; want \"\", false", got, ok)
	}
	ctx := context.WithValue(context.Background(), partitionKey{}, "")
	if got, ok := PartitionFromContext(ctx); !ok || got != "" {
		t.Errorf("PartitionFromContext(empty partition) = %q, %v; want \"\", true", got, ok)
	}
}

// A panic in the embedder's func is audited as a panic, not as a forwarded request.
func TestHandlerCallerPartitionPanicIsNotRecordedAsForwarded(t *testing.T) {
	sink := &recordingSink{}
	h, upstream := newTestHandler(t, Config{
		Authenticator:   tenantAuthenticator{},
		CallerPartition: func(*CallerIdentity) string { panic("partition func") },
		Audit:           sink,
	})

	func() {
		defer func() { _ = recover() }()
		serve(h, tenantRequest("a"))
	}()

	if upstream.calls != 0 {
		t.Errorf("upstream calls = %d, want 0", upstream.calls)
	}
	if len(sink.events) != 1 || sink.events[0].Outcome == OutcomeForwarded {
		t.Errorf("audit events = %+v, want one not recorded as forwarded", sink.events)
	}
}

// A partition cannot spell its way into another partition's key.
func TestBuildCacheKeyKeepsPartitionsApart(t *testing.T) {
	req := newClusterRequest(http.MethodGet, testPodsPath, nil)
	keys := map[string]string{
		"none":       buildCacheKey(InClusterID, "", req, ""),
		"a":          buildCacheKey(InClusterID, "a", req, ""),
		"b":          buildCacheKey(InClusterID, "b", req, ""),
		"nul-suffix": buildCacheKey(InClusterID, "a\x00", req, ""),
	}
	seen := make(map[string]string)
	for name, key := range keys {
		if other, ok := seen[key]; ok {
			t.Errorf("partitions %q and %q share key %q", name, other, key)
		}
		seen[key] = name
	}
}

// forbiddingUpstream answers 403 for the partitions in forbid and an echo of the partition
// otherwise, counting calls per partition.
type forbiddingUpstream struct {
	mu     sync.Mutex
	forbid map[string]bool
	calls  map[string]int
}

func (u *forbiddingUpstream) RoundTripCluster(_ context.Context, _ string, req *http.Request) (*http.Response, error) {
	partition, _ := PartitionFromContext(req.Context())
	u.mu.Lock()
	u.calls[partition]++
	forbidden := u.forbid[partition]
	u.mu.Unlock()
	status, body := http.StatusOK, `{"kind":"PodList","apiVersion":"v1","metadata":{"resourceVersion":"`+partition+`"},"items":[]}`
	if forbidden {
		status, body = http.StatusForbidden, `{"kind":"Status","apiVersion":"v1","status":"Failure","code":403}`
	}
	return &http.Response{
		StatusCode: status,
		Header:     http.Header{headerContentType: []string{contentTypeJSON}},
		Body:       io.NopCloser(strings.NewReader(body)),
		Request:    req,
	}, nil
}

func (u *forbiddingUpstream) set(partition string, forbidden bool) {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.forbid[partition] = forbidden
}

func (u *forbiddingUpstream) count(partition string) int {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.calls[partition]
}

// A 403 in one partition evicts that partition's entries only: another tenant's
// credential, and so its cached responses, are unaffected.
func TestHandlerAuthFailureInvalidatesItsOwnPartitionOnly(t *testing.T) {
	upstream := &forbiddingUpstream{forbid: map[string]bool{}, calls: map[string]int{}}
	h, _ := newTestHandler(t, Config{
		Authenticator:   tenantAuthenticator{},
		Upstream:        upstream,
		Cache:           &CacheConfig{DefaultResourceTTL: testPartitionTTL},
		CallerPartition: partitionByTenant,
	})
	serve(h, tenantRequest("a"))
	serve(h, tenantRequest("b"))

	upstream.set("a", true)
	// a's entry is still fresh, so no-cache sends a's request upstream, where it gets 403.
	req := tenantRequest("a")
	req.Header.Set("Cache-Control", "no-cache")
	if rec := serve(h, req); rec.Code != http.StatusForbidden {
		t.Fatalf("tenant a after losing access: code %d, want 403", rec.Code)
	}

	rec := serve(h, tenantRequest("b"))
	if got := rec.Header().Get(headerCacheStatus); got != cacheStatusHit {
		t.Errorf("tenant b after tenant a's 403: Cache-Status %q, want %q", got, cacheStatusHit)
	}
	if got := upstream.count("b"); got != 1 {
		t.Errorf("tenant b upstream calls = %d, want 1", got)
	}

	upstream.set("a", false)
	beforeA := upstream.count("a")
	if rec := serve(h, tenantRequest("a")); rec.Header().Get(headerCacheStatus) == cacheStatusHit {
		t.Errorf("tenant a after its own 403: served from cache, want a fresh fetch")
	}
	if got := upstream.count("a"); got != beforeA+1 {
		t.Errorf("tenant a upstream calls after its 403 = %d, want %d", got, beforeA+1)
	}
}

// A write evicts the scope in every partition: the data changed for every caller.
func TestHandlerWriteInvalidatesEveryPartition(t *testing.T) {
	upstream := &forbiddingUpstream{forbid: map[string]bool{}, calls: map[string]int{}}
	h, _ := newTestHandler(t, Config{
		Authenticator:   tenantAuthenticator{},
		Upstream:        upstream,
		Policy:          Policy{Mode: ModeReadWrite, RedactSecrets: true},
		Cache:           &CacheConfig{DefaultResourceTTL: testPartitionTTL},
		CallerPartition: partitionByTenant,
	})
	serve(h, tenantRequest("a"))
	serve(h, tenantRequest("b"))

	patch := newClusterRequest(http.MethodPatch, testPodsPath+"/web-0", strings.NewReader(`{}`))
	patch.Header.Set(testTenantHeader, "a")
	patch.Header.Set(headerContentType, "application/merge-patch+json")
	serve(h, patch)

	if got := serve(h, tenantRequest("b")).Header().Get(headerCacheStatus); got == cacheStatusHit {
		t.Errorf("tenant b after tenant a's write: served from cache, want a fresh fetch")
	}
}

// Without CallerPartition every caller is in one partition, so a 403 evicts the scope for
// all of them, as it did before partitions existed.
func TestHandlerAuthFailureWithoutPartitionsInvalidatesEveryone(t *testing.T) {
	upstream := &forbiddingUpstream{forbid: map[string]bool{}, calls: map[string]int{}}
	h, _ := newTestHandler(t, Config{
		Authenticator: tenantAuthenticator{},
		Upstream:      upstream,
		Cache:         &CacheConfig{DefaultResourceTTL: testPartitionTTL},
	})
	serve(h, tenantRequest("a"))

	upstream.set("", true)
	req := tenantRequest("b")
	req.Header.Set("Cache-Control", "no-cache")
	if rec := serve(h, req); rec.Code != http.StatusForbidden {
		t.Fatalf("code %d, want 403", rec.Code)
	}
	upstream.set("", false)
	if got := serve(h, tenantRequest("a")).Header().Get(headerCacheStatus); got == cacheStatusHit {
		t.Errorf("after a 403 with no partitions: served from cache, want a fresh fetch")
	}
}

// A write refused with 403 is an auth failure too: it evicts the caller's partition, not
// the others, whose cached responses the refused write did not change.
func TestHandlerRefusedWriteInvalidatesItsOwnPartitionOnly(t *testing.T) {
	upstream := &forbiddingUpstream{forbid: map[string]bool{}, calls: map[string]int{}}
	h, _ := newTestHandler(t, Config{
		Authenticator:   tenantAuthenticator{},
		Upstream:        upstream,
		Policy:          Policy{Mode: ModeReadWrite, RedactSecrets: true},
		Cache:           &CacheConfig{DefaultResourceTTL: testPartitionTTL},
		CallerPartition: partitionByTenant,
	})
	serve(h, tenantRequest("a"))
	serve(h, tenantRequest("b"))

	upstream.set("a", true)
	patch := newClusterRequest(http.MethodPatch, testPodsPath+"/web-0", strings.NewReader(`{}`))
	patch.Header.Set(testTenantHeader, "a")
	patch.Header.Set(headerContentType, "application/merge-patch+json")
	if rec := serve(h, patch); rec.Code != http.StatusForbidden {
		t.Fatalf("tenant a's write: code %d, want 403", rec.Code)
	}

	if got := serve(h, tenantRequest("b")).Header().Get(headerCacheStatus); got != cacheStatusHit {
		t.Errorf("tenant b after tenant a's refused write: Cache-Status %q, want %q", got, cacheStatusHit)
	}
}

// gatedUpstream answers like forbiddingUpstream but holds partition hold's requests until
// release is closed, signalling arrived when one starts.
type gatedUpstream struct {
	*forbiddingUpstream
	hold    string
	arrived chan struct{}
	release chan struct{}
}

func (u *gatedUpstream) RoundTripCluster(ctx context.Context, clusterID string, req *http.Request) (*http.Response, error) {
	if partition, _ := PartitionFromContext(req.Context()); partition == u.hold {
		u.arrived <- struct{}{}
		<-u.release
	}
	return u.forbiddingUpstream.RoundTripCluster(ctx, clusterID, req)
}

// A 403 in one partition while another partition's fetch is in flight leaves that fetch
// free to store its result: only the failing partition's invalidation count moves.
func TestHandlerAuthFailureLeavesOtherPartitionsFlightsStoring(t *testing.T) {
	upstream := &gatedUpstream{
		forbiddingUpstream: &forbiddingUpstream{forbid: map[string]bool{"a": true}, calls: map[string]int{}},
		hold:               "b",
		arrived:            make(chan struct{}, 1),
		release:            make(chan struct{}),
	}
	h, _ := newTestHandler(t, Config{
		Authenticator:   tenantAuthenticator{},
		Upstream:        upstream,
		Cache:           &CacheConfig{DefaultResourceTTL: testPartitionTTL},
		CallerPartition: partitionByTenant,
	})

	done := make(chan *httptest.ResponseRecorder, 1)
	go func() { done <- serve(h, tenantRequest("b")) }()
	select {
	case <-upstream.arrived:
	case <-time.After(testArrivalTimeout):
		close(upstream.release)
		t.Fatal("tenant b's fetch never reached the upstream")
	}

	if rec := serve(h, tenantRequest("a")); rec.Code != http.StatusForbidden {
		close(upstream.release)
		t.Fatalf("tenant a: code %d, want 403", rec.Code)
	}
	close(upstream.release)
	if rec := <-done; rec.Code != http.StatusOK {
		t.Fatalf("tenant b's held fetch: code %d, want 200", rec.Code)
	}

	if got := serve(h, tenantRequest("b")).Header().Get(headerCacheStatus); got != cacheStatusHit {
		t.Errorf("tenant b after its fetch finished during tenant a's 403: Cache-Status %q, want %q (the fetch's result was discarded)", got, cacheStatusHit)
	}
	if got := upstream.count("b"); got != 1 {
		t.Errorf("tenant b upstream calls = %d, want 1", got)
	}
}
