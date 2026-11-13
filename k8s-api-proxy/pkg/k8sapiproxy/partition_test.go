package k8sapiproxy

import (
	"context"
	"errors"
	"io"
	"net/http"
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

type recordingAuditSink struct{ events *[]AuditEvent }

func (s recordingAuditSink) Record(_ context.Context, e AuditEvent) { *s.events = append(*s.events, e) }

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
	var events []AuditEvent
	h, upstream := newTestHandler(t, Config{
		Authenticator:   tenantAuthenticator{},
		CallerPartition: func(*CallerIdentity) string { panic("partition func") },
		Audit:           recordingAuditSink{events: &events},
	})

	func() {
		defer func() { _ = recover() }()
		serve(h, tenantRequest("a"))
	}()

	if upstream.calls != 0 {
		t.Errorf("upstream calls = %d, want 0", upstream.calls)
	}
	if len(events) != 1 || events[0].Outcome == OutcomeForwarded {
		t.Errorf("audit events = %+v, want one not recorded as forwarded", events)
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
