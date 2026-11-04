package k8sapiproxy

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestMemoryCacheStoreLRUEvictionAndFrequency(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	store := NewMemoryCacheStore(30)
	store.now = func() time.Time { return now }

	val1 := []byte("0123456789")
	val2 := []byte("abcdefghij")
	val3 := []byte("klmnopqrst")
	val4 := []byte("uvwxyz0123")

	// Verify defensive byte cloning on Set and Get.
	callerBuf := []byte("0123456789")
	if err := store.Set(ctx, "k1", callerBuf, 10*time.Second); err != nil {
		t.Fatalf("Set k1: %v", err)
	}
	callerBuf[0] = 'X'
	got1, ok, err := store.Get(ctx, "k1")
	if err != nil || !ok || !bytes.Equal(got1, val1) {
		t.Fatalf("Get k1 = %q, ok=%v, err=%v; want %q", got1, ok, err, val1)
	}
	got1[0] = 'Z'
	got1Again, ok, err := store.Get(ctx, "k1")
	if err != nil || !ok || !bytes.Equal(got1Again, val1) {
		t.Fatalf("Get k1 after caller slice mutation = %q, want %q", got1Again, val1)
	}

	// k1 now has hits > 0. Insert cold entries k2 and k3 to fill the 30-byte capacity.
	if err := store.Set(ctx, "k2", val2, 10*time.Second); err != nil {
		t.Fatalf("Set k2: %v", err)
	}
	if err := store.Set(ctx, "k3", val3, 10*time.Second); err != nil {
		t.Fatalf("Set k3: %v", err)
	}
	if store.Len() != 3 || store.UsedBytes() != 30 {
		t.Fatalf("Len=%d UsedBytes=%d, want Len=3 UsedBytes=30", store.Len(), store.UsedBytes())
	}

	// Insert k4. k1 is at the back of the LRU list, but its hit counter gives it a second chance,
	// so the oldest cold entry k2 (hits == 0) is evicted instead.
	if err := store.Set(ctx, "k4", val4, 10*time.Second); err != nil {
		t.Fatalf("Set k4: %v", err)
	}
	if store.Len() != 3 || store.UsedBytes() != 30 {
		t.Fatalf("after eviction Len=%d UsedBytes=%d, want Len=3 UsedBytes=30", store.Len(), store.UsedBytes())
	}
	if st := store.Stats(); st != (CacheStats{Bytes: 30, Entries: 3, CapacityEvicted: 1, ExpiredEvicted: 0}) {
		t.Fatalf("after capacity eviction Stats() = %+v, want {Bytes:30 Entries:3 CapacityEvicted:1 ExpiredEvicted:0}", st)
	}
	if _, ok, _ := store.Get(ctx, "k2"); ok {
		t.Errorf("cold entry k2 should have been evicted")
	}
	if _, ok, _ := store.Get(ctx, "k1"); !ok {
		t.Errorf("frequent entry k1 should have survived eviction")
	}
	if _, ok, _ := store.Get(ctx, "k3"); !ok {
		t.Errorf("entry k3 should still be present")
	}
	if _, ok, _ := store.Get(ctx, "k4"); !ok {
		t.Errorf("entry k4 should still be present")
	}

	// Verify explicit Delete updates Len and UsedBytes.
	if err := store.Delete(ctx, "k3"); err != nil {
		t.Fatalf("Delete k3: %v", err)
	}
	if store.Len() != 2 || store.UsedBytes() != 20 {
		t.Fatalf("after Delete Len=%d UsedBytes=%d, want Len=2 UsedBytes=20", store.Len(), store.UsedBytes())
	}
	if st := store.Stats(); st != (CacheStats{Bytes: 20, Entries: 2, CapacityEvicted: 1, ExpiredEvicted: 0}) {
		t.Fatalf("after Delete Stats() = %+v, want {Bytes:20 Entries:2 CapacityEvicted:1 ExpiredEvicted:0}", st)
	}
	if _, ok, _ := store.Get(ctx, "k3"); ok {
		t.Errorf("deleted entry k3 is still present")
	}

	// Verify TTL expiration with the controllable clock.
	now = now.Add(11 * time.Second)
	if _, ok, err := store.Get(ctx, "k1"); err != nil || ok {
		t.Errorf("expired entry k1 returned ok=%v, err=%v; want ok=false", ok, err)
	}
	if _, ok, err := store.Get(ctx, "k4"); err != nil || ok {
		t.Errorf("expired entry k4 returned ok=%v, err=%v; want ok=false", ok, err)
	}
	if store.Len() != 0 || store.UsedBytes() != 0 {
		t.Errorf("after TTL expiration Len=%d UsedBytes=%d, want 0", store.Len(), store.UsedBytes())
	}
	if st := store.Stats(); st != (CacheStats{Bytes: 0, Entries: 0, CapacityEvicted: 1, ExpiredEvicted: 2}) {
		t.Fatalf("after TTL expiration Stats() = %+v, want {Bytes:0 Entries:0 CapacityEvicted:1 ExpiredEvicted:2}", st)
	}

	// Verify pure cold LRU order when all entries have hits == 0.
	_ = store.Set(ctx, "c1", val1, 10*time.Second)
	_ = store.Set(ctx, "c2", val2, 10*time.Second)
	_ = store.Set(ctx, "c3", val3, 10*time.Second)
	_ = store.Set(ctx, "c4", val4, 10*time.Second)
	if _, ok, _ := store.Get(ctx, "c1"); ok {
		t.Errorf("oldest cold entry c1 should have been evicted first")
	}
	if st := store.Stats(); st != (CacheStats{Bytes: 30, Entries: 3, CapacityEvicted: 2, ExpiredEvicted: 2}) {
		t.Fatalf("final Stats() = %+v, want {Bytes:30 Entries:3 CapacityEvicted:2 ExpiredEvicted:2}", st)
	}
}

type faultyCacheStore struct {
	mu          sync.Mutex
	inner       *MemoryCacheStore
	getCalls    int
	setCalls    int
	deleteCalls int
	getErr      error
	setErr      error
	deleteErr   error
}

func (s *faultyCacheStore) Get(ctx context.Context, key string) ([]byte, bool, error) {
	s.mu.Lock()
	s.getCalls++
	err := s.getErr
	s.mu.Unlock()
	if err != nil {
		return nil, false, err
	}
	return s.inner.Get(ctx, key)
}

func (s *faultyCacheStore) Set(ctx context.Context, key string, value []byte, ttl time.Duration) error {
	s.mu.Lock()
	s.setCalls++
	err := s.setErr
	s.mu.Unlock()
	if err != nil {
		return err
	}
	return s.inner.Set(ctx, key, value, ttl)
}

func (s *faultyCacheStore) Delete(ctx context.Context, key string) error {
	s.mu.Lock()
	s.deleteCalls++
	err := s.deleteErr
	s.mu.Unlock()
	if err != nil {
		return err
	}
	return s.inner.Delete(ctx, key)
}

func TestHandlerCustomCacheStoreAndFailOpen(t *testing.T) {
	custom := &faultyCacheStore{inner: NewMemoryCacheStore(DefaultCacheMaxBytes)}
	h, upstream := newTestHandler(t, Config{
		Policy: Policy{Mode: ModeReadWrite, RedactSecrets: true},
		Cache:  &CacheConfig{Store: custom},
	})

	// Healthy custom store caches on miss and serves from cache on hit.
	rec1 := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil))
	if rec1.Code != http.StatusOK || rec1.Header().Get(headerCacheStatus) != cacheStatusMiss {
		t.Fatalf("first GET code=%d Cache-Status=%q", rec1.Code, rec1.Header().Get(headerCacheStatus))
	}
	rec2 := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil))
	if rec2.Code != http.StatusOK || rec2.Header().Get(headerCacheStatus) != cacheStatusHit {
		t.Fatalf("second GET code=%d Cache-Status=%q", rec2.Code, rec2.Header().Get(headerCacheStatus))
	}
	if upstream.calls != 1 {
		t.Fatalf("upstream.calls = %d, want 1", upstream.calls)
	}

	// Inject errors into Get, Set, and Delete; requests must fail open and succeed with 200 OK.
	custom.mu.Lock()
	custom.getErr = errors.New("redis get timeout")
	custom.setErr = errors.New("redis set timeout")
	custom.deleteErr = errors.New("redis delete timeout")
	custom.mu.Unlock()

	rec3 := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil))
	if rec3.Code != http.StatusOK {
		t.Fatalf("GET with failing store code = %d, want 200: %s", rec3.Code, rec3.Body.String())
	}
	if upstream.calls != 2 {
		t.Fatalf("upstream.calls = %d, want 2 after fail-open Get", upstream.calls)
	}

	recDel := serve(h, httptest.NewRequest(http.MethodDelete, "/api/v1/namespaces/default/pods/p1", nil))
	if recDel.Code != http.StatusOK {
		t.Fatalf("DELETE with failing store code = %d, want 200: %s", recDel.Code, recDel.Body.String())
	}
	custom.mu.Lock()
	getCalls, setCalls, delCalls := custom.getCalls, custom.setCalls, custom.deleteCalls
	custom.mu.Unlock()
	if getCalls < 3 || setCalls < 2 || delCalls < 1 {
		t.Errorf("store calls get=%d set=%d delete=%d, want >= 3, 2, 1", getCalls, setCalls, delCalls)
	}
}

func TestHandlerCacheHitMissAndObservability(t *testing.T) {
	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	var nowMu sync.Mutex
	clock := func() time.Time {
		nowMu.Lock()
		defer nowMu.Unlock()
		return now
	}
	advance := func(d time.Duration) {
		nowMu.Lock()
		now = now.Add(d)
		nowMu.Unlock()
	}

	metrics := NewMetrics()
	store := NewMemoryCacheStore(DefaultCacheMaxBytes)
	store.now = clock

	var upstreamCalls atomic.Int32
	gate := make(chan struct{})
	close(gate)

	upstream := &fakeUpstream{
		handler: func(_ string, req *http.Request) (*http.Response, error) {
			if req.URL.Path == "/api/v1/namespaces/default/services" {
				<-gate
			}
			n := upstreamCalls.Add(1)
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{headerContentType: []string{contentTypeJSON}},
				Body:       io.NopCloser(strings.NewReader(`{"kind":"PodList","apiVersion":"v1","call":` + strconv.Itoa(int(n)) + `}`)),
			}, nil
		},
	}

	h, err := NewHandler(Config{
		Authenticator: shellCaller,
		Upstream:      upstream,
		Policy:        Policy{Mode: ModeReadOnlyWithDryRun, RedactSecrets: true},
		Logger:        slog.New(slog.DiscardHandler),
		Metrics:       metrics,
		Cache: &CacheConfig{
			Store: store,
			Now:   clock,
		},
	})
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}

	// 1. Resource LIST and GET: cache miss followed by cache hit with Age header.
	for _, path := range []string{
		"/api/v1/namespaces/default/pods",
		"/api/v1/namespaces/default/pods/pod-a",
		"/openapi/v2",
		"/apis",
	} {
		recMiss := serve(h, httptest.NewRequest(http.MethodGet, path, nil))
		if recMiss.Code != http.StatusOK {
			t.Fatalf("GET %s miss code = %d: %s", path, recMiss.Code, recMiss.Body.String())
		}
		if got := recMiss.Header().Get(headerCacheStatus); got != cacheStatusMiss {
			t.Errorf("GET %s first Cache-Status = %q, want %q", path, got, cacheStatusMiss)
		}

		advance(2 * time.Second)
		recHit := serve(h, httptest.NewRequest(http.MethodGet, path, nil))
		if recHit.Code != http.StatusOK {
			t.Fatalf("GET %s hit code = %d: %s", path, recHit.Code, recHit.Body.String())
		}
		if got := recHit.Header().Get(headerCacheStatus); got != cacheStatusHit {
			t.Errorf("GET %s second Cache-Status = %q, want %q", path, got, cacheStatusHit)
		}
		if got := recHit.Header().Get(headerAge); got != "2" {
			t.Errorf("GET %s second Age = %q, want 2", path, got)
		}
		if recHit.Body.String() != recMiss.Body.String() {
			t.Errorf("GET %s hit body = %q, want %q", path, recHit.Body.String(), recMiss.Body.String())
		}
	}

	// 2. Non-cached paths and verbs: /healthz, /livez, /readyz, watch=true, pods/log, and ?dryRun=All writes.
	nonCached := []struct {
		method string
		path   string
		body   string
	}{
		{http.MethodGet, "/healthz", ""},
		{http.MethodGet, "/livez", ""},
		{http.MethodGet, "/readyz", ""},
		{http.MethodGet, "/api/v1/namespaces/default/pods?watch=true", ""},
		{http.MethodGet, "/api/v1/namespaces/default/pods/pod-a/log", ""},
		{http.MethodPost, "/api/v1/namespaces/default/configmaps?dryRun=All", `{"apiVersion":"v1","kind":"ConfigMap"}`},
	}
	for _, tc := range nonCached {
		before := upstreamCalls.Load()
		for i := 0; i < 2; i++ {
			var bodyReader io.Reader
			if tc.body != "" {
				bodyReader = strings.NewReader(tc.body)
			}
			rec := serve(h, httptest.NewRequest(tc.method, tc.path, bodyReader))
			if rec.Code != http.StatusOK {
				t.Fatalf("%s %s code = %d: %s", tc.method, tc.path, rec.Code, rec.Body.String())
			}
			if got := rec.Header().Get(headerCacheStatus); got != "" {
				t.Errorf("%s %s unexpected Cache-Status = %q", tc.method, tc.path, got)
			}
		}
		if got := upstreamCalls.Load() - before; got != 2 {
			t.Errorf("%s %s upstream calls = %d, want 2 (never cached)", tc.method, tc.path, got)
		}
	}

	// 3. Trigger a bypass metric via Cache-Control: no-store.
	bypassReq := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil)
	bypassReq.Header.Set(headerCacheControl, "no-store")
	if rec := serve(h, bypassReq); rec.Header().Get(headerCacheStatus) != cacheStatusBypass {
		t.Errorf("no-store Cache-Status = %q, want %q", rec.Header().Get(headerCacheStatus), cacheStatusBypass)
	}

	// 4. Trigger a coalesced metric with two concurrent requests on /api/v1/namespaces/default/services.
	gate = make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(2)
	for i := 0; i < 2; i++ {
		go func() {
			defer wg.Done()
			serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/services", nil))
		}()
	}
	time.Sleep(20 * time.Millisecond)
	close(gate)
	wg.Wait()

	exposition := scrapeMetricsText(t, metrics)
	for _, want := range []string{
		`kubeagents_kube_proxy_cache_requests_total{resource="pods",result="miss",verb="list"} 1`,
		`kubeagents_kube_proxy_cache_requests_total{resource="pods",result="hit",verb="list"} 1`,
		`kubeagents_kube_proxy_cache_requests_total{resource="pods",result="bypass",verb="list"} 1`,
		`kubeagents_kube_proxy_cache_requests_total{resource="discovery",result="miss",verb="discovery"} 2`,
		`kubeagents_kube_proxy_cache_requests_total{resource="discovery",result="hit",verb="discovery"} 2`,
		`kubeagents_kube_proxy_cache_requests_total{resource="services",result="miss",verb="list"} 1`,
		`kubeagents_kube_proxy_cache_requests_total{resource="services",result="coalesced",verb="list"} 1`,
	} {
		if !strings.Contains(exposition, want) {
			t.Errorf("metrics exposition missing %q\nFull exposition:\n%s", want, exposition)
		}
	}

	// 5. Trigger TTL expiration and capacity eviction metrics and verify occupancy gauges.
	// Advance to t=13s so /api/v1/namespaces/default/pods (stored at t=0, TTL 10s) and
	// pod-a (stored at t=2s, TTL 10s) are expired while services (stored at t=8s, TTL 10s) remains fresh.
	advance(5 * time.Second)
	if !nonCachedDiscoveryPaths.Has("/metrics") {
		t.Fatalf("nonCachedDiscoveryPaths must include /metrics so live Prometheus scrapes are not cached")
	}
	if rec := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods/pod-a", nil)); rec.Header().Get(headerCacheStatus) != cacheStatusMiss {
		t.Fatalf("expired pod-a status = %q, want miss", rec.Header().Get(headerCacheStatus))
	}
	// Tightening maxBytes forces Set to evict the remaining expired pods entry and one cold entry by capacity.
	store.mu.Lock()
	store.maxBytes = store.usedBytes - 10
	store.mu.Unlock()
	if rec := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/configmaps", nil)); rec.Header().Get(headerCacheStatus) != cacheStatusMiss {
		t.Fatalf("configmaps miss status = %q, want miss", rec.Header().Get(headerCacheStatus))
	}

	st := store.Stats()
	if st.ExpiredEvicted != 2 || st.CapacityEvicted != 1 || st.Entries != 4 || st.Bytes <= 0 {
		t.Fatalf("Stats() = %+v, want ExpiredEvicted=2 CapacityEvicted=1 Entries=4 Bytes>0", st)
	}
	expositionAfterEvict := scrapeMetricsText(t, metrics)
	for _, want := range []string{
		`kubeagents_kube_proxy_cache_evictions_total{reason="expired"} 2`,
		`kubeagents_kube_proxy_cache_evictions_total{reason="capacity"} 1`,
		`kubeagents_kube_proxy_cache_bytes ` + strconv.FormatInt(st.Bytes, 10),
		`kubeagents_kube_proxy_cache_entries ` + strconv.Itoa(st.Entries),
	} {
		if !strings.Contains(expositionAfterEvict, want) {
			t.Errorf("eviction metrics exposition missing %q\nFull exposition:\n%s", want, expositionAfterEvict)
		}
	}
}

func TestHandlerCacheControlDirectives(t *testing.T) {
	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	store := NewMemoryCacheStore(DefaultCacheMaxBytes)
	store.now = func() time.Time { return now }

	var respCacheControl string
	var callCount int
	upstream := &recordingTransport{
		response: func(req *http.Request) *http.Response {
			callCount++
			hdr := http.Header{headerContentType: []string{contentTypeJSON}}
			if respCacheControl != "" {
				hdr.Set(headerCacheControl, respCacheControl)
			}
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     hdr,
				Body:       io.NopCloser(strings.NewReader(`{"kind":"PodList","apiVersion":"v1","seq":` + strconv.Itoa(callCount) + `}`)),
				Request:    req,
			}
		},
	}

	h, _ := newTestHandler(t, Config{
		Upstream: upstream,
		Cache: &CacheConfig{
			Store:              store,
			DefaultResourceTTL: 5 * time.Second,
			Now:                func() time.Time { return now },
		},
	})

	const podsPath = "/api/v1/namespaces/default/pods"

	// 1. Request Cache-Control: no-store bypasses read and write.
	reqNoStore := httptest.NewRequest(http.MethodGet, podsPath, nil)
	reqNoStore.Header.Set(headerCacheControl, "no-store")
	rec := serve(h, reqNoStore)
	if got := rec.Header().Get(headerCacheStatus); got != cacheStatusBypass {
		t.Fatalf("no-store Cache-Status = %q, want %q", got, cacheStatusBypass)
	}
	if store.Len() != 0 {
		t.Fatalf("store.Len() = %d after no-store, want 0", store.Len())
	}

	// Populate cache (seq 2).
	rec = serve(h, httptest.NewRequest(http.MethodGet, podsPath, nil))
	if rec.Header().Get(headerCacheStatus) != cacheStatusMiss || !strings.Contains(rec.Body.String(), `"seq":2`) {
		t.Fatalf("populate got status=%q body=%s", rec.Header().Get(headerCacheStatus), rec.Body.String())
	}

	// 2. Request Cache-Control: no-cache forces upstream refresh (seq 3) and updates cache.
	reqNoCache := httptest.NewRequest(http.MethodGet, podsPath, nil)
	reqNoCache.Header.Set(headerCacheControl, "no-cache")
	rec = serve(h, reqNoCache)
	if rec.Header().Get(headerCacheStatus) != cacheStatusMiss || !strings.Contains(rec.Body.String(), `"seq":3`) {
		t.Fatalf("no-cache got status=%q body=%s", rec.Header().Get(headerCacheStatus), rec.Body.String())
	}
	rec = serve(h, httptest.NewRequest(http.MethodGet, podsPath, nil))
	if rec.Header().Get(headerCacheStatus) != cacheStatusHit || !strings.Contains(rec.Body.String(), `"seq":3`) {
		t.Fatalf("after no-cache, hit got status=%q body=%s", rec.Header().Get(headerCacheStatus), rec.Body.String())
	}

	// 3. Request Cache-Control: max-age=0 forces upstream refresh (seq 4) and updates cache.
	reqZeroMaxAge := httptest.NewRequest(http.MethodGet, podsPath, nil)
	reqZeroMaxAge.Header.Set(headerCacheControl, "max-age=0")
	rec = serve(h, reqZeroMaxAge)
	if rec.Header().Get(headerCacheStatus) != cacheStatusMiss || !strings.Contains(rec.Body.String(), `"seq":4`) {
		t.Fatalf("max-age=0 got status=%q body=%s", rec.Header().Get(headerCacheStatus), rec.Body.String())
	}
	rec = serve(h, httptest.NewRequest(http.MethodGet, podsPath, nil))
	if rec.Header().Get(headerCacheStatus) != cacheStatusHit || !strings.Contains(rec.Body.String(), `"seq":4`) {
		t.Fatalf("after max-age=0, hit got status=%q body=%s", rec.Header().Get(headerCacheStatus), rec.Body.String())
	}

	// 4. Request Cache-Control: max-age=2 rejects a 3-second-old entry, whereas normal GET accepts it.
	now = now.Add(3 * time.Second)
	rec = serve(h, httptest.NewRequest(http.MethodGet, podsPath, nil))
	if rec.Header().Get(headerCacheStatus) != cacheStatusHit || rec.Header().Get(headerAge) != "3" {
		t.Fatalf("at age 3s normal GET got status=%q age=%q", rec.Header().Get(headerCacheStatus), rec.Header().Get(headerAge))
	}
	reqMaxAge2 := httptest.NewRequest(http.MethodGet, podsPath, nil)
	reqMaxAge2.Header.Set(headerCacheControl, "max-age=2")
	rec = serve(h, reqMaxAge2)
	if rec.Header().Get(headerCacheStatus) != cacheStatusMiss || !strings.Contains(rec.Body.String(), `"seq":5`) {
		t.Fatalf("at age 3s max-age=2 got status=%q body=%s", rec.Header().Get(headerCacheStatus), rec.Body.String())
	}

	// 5. Request Cache-Control: max-stale=5 accepts an entry past its FreshTTL when stored TTL permits.
	// Set upstream Cache-Control: max-age=2 so FreshTTL is 2s while storeTTL remains 5s.
	respCacheControl = "max-age=2"
	reqRefresh := httptest.NewRequest(http.MethodGet, podsPath, nil)
	reqRefresh.Header.Set(headerCacheControl, "no-cache")
	rec = serve(h, reqRefresh)
	if rec.Header().Get(headerCacheStatus) != cacheStatusMiss || !strings.Contains(rec.Body.String(), `"seq":6`) {
		t.Fatalf("refresh with upstream max-age=2 got status=%q body=%s", rec.Header().Get(headerCacheStatus), rec.Body.String())
	}
	respCacheControl = ""

	// Advance 4s: entry is still in MemoryCacheStore (storeTTL=5s), but age (4s) > FreshTTL (2s).
	now = now.Add(4 * time.Second)
	reqMaxStale := httptest.NewRequest(http.MethodGet, podsPath, nil)
	reqMaxStale.Header.Set(headerCacheControl, "max-stale=5")
	rec = serve(h, reqMaxStale)
	if rec.Header().Get(headerCacheStatus) != cacheStatusHit || rec.Header().Get(headerAge) != "4" {
		t.Fatalf("max-stale=5 at age 4s got status=%q age=%q, want hit and age 4", rec.Header().Get(headerCacheStatus), rec.Header().Get(headerAge))
	}

	// Without max-stale, the 4s-old entry (FreshTTL=2s) is stale and triggers a miss (seq 7).
	rec = serve(h, httptest.NewRequest(http.MethodGet, podsPath, nil))
	if rec.Header().Get(headerCacheStatus) != cacheStatusMiss || !strings.Contains(rec.Body.String(), `"seq":7`) {
		t.Fatalf("without max-stale at age 4s got status=%q body=%s", rec.Header().Get(headerCacheStatus), rec.Body.String())
	}

	// 6. Upstream response Cache-Control: no-store and max-age=0 bypass storing, whereas
	// kube-apiserver's default "no-cache, private" header caches under the configured TTL.
	for _, directive := range []string{"no-store", "max-age=0"} {
		respCacheControl = directive
		target := "/api/v1/namespaces/default/services?mode=" + directive
		beforeCalls := callCount
		rec1 := serve(h, httptest.NewRequest(http.MethodGet, target, nil))
		if got := rec1.Header().Get(headerCacheStatus); got != cacheStatusBypass {
			t.Errorf("upstream %s first Cache-Status = %q, want %q", directive, got, cacheStatusBypass)
		}
		rec2 := serve(h, httptest.NewRequest(http.MethodGet, target, nil))
		if got := rec2.Header().Get(headerCacheStatus); got != cacheStatusBypass {
			t.Errorf("upstream %s second Cache-Status = %q, want %q", directive, got, cacheStatusBypass)
		}
		if callCount-beforeCalls != 2 {
			t.Errorf("upstream %s callCount delta = %d, want 2", directive, callCount-beforeCalls)
		}
	}

	respCacheControl = "no-cache, private"
	const apiserverDefaultTarget = "/api/v1/namespaces/default/services?mode=apiserver-default"
	beforeCalls := callCount
	rec1 := serve(h, httptest.NewRequest(http.MethodGet, apiserverDefaultTarget, nil))
	if got := rec1.Header().Get(headerCacheStatus); got != cacheStatusMiss {
		t.Errorf("upstream no-cache, private first Cache-Status = %q, want %q", got, cacheStatusMiss)
	}
	rec2 := serve(h, httptest.NewRequest(http.MethodGet, apiserverDefaultTarget, nil))
	if got := rec2.Header().Get(headerCacheStatus); got != cacheStatusHit {
		t.Errorf("upstream no-cache, private second Cache-Status = %q, want %q", got, cacheStatusHit)
	}
	if callCount-beforeCalls != 1 {
		t.Errorf("upstream no-cache, private callCount delta = %d, want 1", callCount-beforeCalls)
	}
}

func TestHandlerCacheAcceptAndClusterIsolation(t *testing.T) {
	var calls int
	upstream := &recordingTransport{
		response: func(req *http.Request) *http.Response {
			calls++
			body := `{"cluster":` + strconv.Quote(req.Header.Get("X-Test-Cluster")) +
				`,"accept":` + strconv.Quote(req.Header.Get(headerAccept)) +
				`,"encoding":` + strconv.Quote(req.Header.Get(headerAcceptEncoding)) +
				`,"call":` + strconv.Itoa(calls) + `}`
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{headerContentType: []string{contentTypeJSON}},
				Body:       io.NopCloser(strings.NewReader(body)),
				Request:    req,
			}
		},
	}

	h, _ := newTestHandler(t, Config{
		Upstream: upstream,
		Cache:    &CacheConfig{},
	})

	// 1. Accept: Table vs Accept: application/json do not collide.
	reqJSON := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil)
	reqJSON.Header.Set(headerAccept, contentTypeJSON)
	recJSON1 := serve(h, reqJSON)
	if recJSON1.Header().Get(headerCacheStatus) != cacheStatusMiss {
		t.Fatalf("JSON first request status = %q, want miss", recJSON1.Header().Get(headerCacheStatus))
	}

	reqTable := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil)
	reqTable.Header.Set(headerAccept, acceptTable)
	recTable1 := serve(h, reqTable)
	if recTable1.Header().Get(headerCacheStatus) != cacheStatusMiss {
		t.Fatalf("Table first request status = %q, want miss", recTable1.Header().Get(headerCacheStatus))
	}
	if recJSON1.Body.String() == recTable1.Body.String() {
		t.Errorf("Table and JSON responses collided: %s", recTable1.Body.String())
	}

	// 2. Accept-Encoding: gzip vs identity do not collide.
	reqGzip := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil)
	reqGzip.Header.Set(headerAccept, contentTypeJSON)
	reqGzip.Header.Set(headerAcceptEncoding, encodingGzip)
	recGzip := serve(h, reqGzip)
	if recGzip.Header().Get(headerCacheStatus) != cacheStatusMiss {
		t.Fatalf("gzip first request status = %q, want miss", recGzip.Header().Get(headerCacheStatus))
	}

	// 3. Different /clusters/{id} prefixes do not collide.
	recClusterA := serve(h, httptest.NewRequest(http.MethodGet, "/clusters/kind-alpha/api/v1/namespaces/default/pods", nil))
	if recClusterA.Header().Get(headerCacheStatus) != cacheStatusMiss {
		t.Fatalf("kind-alpha first request status = %q, want miss", recClusterA.Header().Get(headerCacheStatus))
	}
	recClusterB := serve(h, httptest.NewRequest(http.MethodGet, "/clusters/kind-beta/api/v1/namespaces/default/pods", nil))
	if recClusterB.Header().Get(headerCacheStatus) != cacheStatusMiss {
		t.Fatalf("kind-beta first request status = %q, want miss", recClusterB.Header().Get(headerCacheStatus))
	}
	recClusterAHit := serve(h, httptest.NewRequest(http.MethodGet, "/clusters/kind-alpha/api/v1/namespaces/default/pods", nil))
	if recClusterAHit.Header().Get(headerCacheStatus) != cacheStatusHit || recClusterAHit.Body.String() != recClusterA.Body.String() {
		t.Fatalf("kind-alpha second request status = %q, want hit matching first response", recClusterAHit.Header().Get(headerCacheStatus))
	}

	// 4. Query parameter canonicalization: ?b=2&a=1 hits cache populated by ?a=1&b=2.
	recQuery1 := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods?a=1&b=2", nil))
	if recQuery1.Header().Get(headerCacheStatus) != cacheStatusMiss {
		t.Fatalf("?a=1&b=2 status = %q, want miss", recQuery1.Header().Get(headerCacheStatus))
	}
	recQuery2 := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods?b=2&a=1", nil))
	if recQuery2.Header().Get(headerCacheStatus) != cacheStatusHit || recQuery2.Body.String() != recQuery1.Body.String() {
		t.Fatalf("?b=2&a=1 status = %q, want hit matching ?a=1&b=2", recQuery2.Header().Get(headerCacheStatus))
	}
}

type countingGetStore struct {
	*MemoryCacheStore
	gets    atomic.Int32
	allSeen chan struct{}
	target  int32
}

func (s *countingGetStore) Get(ctx context.Context, key string) ([]byte, bool, error) {
	if s.gets.Add(1) == s.target {
		close(s.allSeen)
	}
	return s.MemoryCacheStore.Get(ctx, key)
}

func TestHandlerCacheSingleflightCoalescing(t *testing.T) {
	const workers = 10
	store := &countingGetStore{
		MemoryCacheStore: NewMemoryCacheStore(DefaultCacheMaxBytes),
		allSeen:          make(chan struct{}),
		target:           workers,
	}
	releaseUpstream := make(chan struct{})
	var upstreamCalls atomic.Int32

	upstream := &fakeUpstream{
		handler: func(_ string, req *http.Request) (*http.Response, error) {
			upstreamCalls.Add(1)
			<-releaseUpstream
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{headerContentType: []string{contentTypeJSON}},
				Body:       io.NopCloser(strings.NewReader(`{"kind":"PodList","apiVersion":"v1","items":[]}`)),
				Request:    req,
			}, nil
		},
	}

	h, _ := newTestHandler(t, Config{
		Upstream: upstream,
		Cache:    &CacheConfig{Store: store},
	})

	recorders := make([]*httptest.ResponseRecorder, workers)
	var wg sync.WaitGroup
	wg.Add(workers)
	for i := 0; i < workers; i++ {
		go func(idx int) {
			defer wg.Done()
			recorders[idx] = serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil))
		}(i)
	}

	// Wait until all 10 requests have checked Store.Get and entered singleflight.
	<-store.allSeen
	time.Sleep(15 * time.Millisecond)
	close(releaseUpstream)
	wg.Wait()

	if got := upstreamCalls.Load(); got != 1 {
		t.Fatalf("upstream.calls = %d, want 1", got)
	}

	var misses, coalesced int
	for i, rec := range recorders {
		if rec.Code != http.StatusOK {
			t.Fatalf("worker %d code = %d, want 200", i, rec.Code)
		}
		switch rec.Header().Get(headerCacheStatus) {
		case cacheStatusMiss:
			misses++
		case cacheStatusCoalesced:
			coalesced++
			if age := rec.Header().Get(headerAge); age != "0" {
				t.Errorf("coalesced response Age = %q, want 0", age)
			}
		default:
			t.Errorf("worker %d unexpected Cache-Status = %q", i, rec.Header().Get(headerCacheStatus))
		}
	}
	if misses != 1 || coalesced != workers-1 {
		t.Errorf("misses=%d coalesced=%d, want misses=1 coalesced=%d", misses, coalesced, workers-1)
	}
}

func TestHandlerCacheStreamingBypassOverMaxEntryBytes(t *testing.T) {
	const maxEntry = 64
	largeBody := `{"kind":"PodList","apiVersion":"v1","padding":"` + strings.Repeat("x", 256) + `"}`

	for _, tc := range []struct {
		name          string
		contentLength int64
	}{
		{"known Content-Length > MaxEntryBytes", int64(len(largeBody))},
		{"chunked ContentLength -1 > MaxEntryBytes", -1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := NewMemoryCacheStore(DefaultCacheMaxBytes)
			upstream := &recordingTransport{
				response: func(req *http.Request) *http.Response {
					hdr := http.Header{headerContentType: []string{contentTypeJSON}}
					if tc.contentLength >= 0 {
						hdr.Set(headerContentLength, strconv.FormatInt(tc.contentLength, 10))
					}
					return &http.Response{
						StatusCode:    http.StatusOK,
						Header:        hdr,
						Body:          io.NopCloser(strings.NewReader(largeBody)),
						ContentLength: tc.contentLength,
						Request:       req,
					}
				},
			}

			h, _ := newTestHandler(t, Config{
				Upstream: upstream,
				Cache: &CacheConfig{
					Store:         store,
					MaxEntryBytes: maxEntry,
				},
			})

			rec := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil))
			if rec.Code != http.StatusOK {
				t.Fatalf("code = %d, want 200", rec.Code)
			}
			if got := rec.Header().Get(headerCacheStatus); got != cacheStatusBypass {
				t.Errorf("Cache-Status = %q, want %q", got, cacheStatusBypass)
			}
			if rec.Body.String() != largeBody {
				t.Errorf("body length = %d, want full uncorrupted body of length %d", rec.Body.Len(), len(largeBody))
			}
			if store.Len() != 0 || store.UsedBytes() != 0 {
				t.Errorf("store Len=%d UsedBytes=%d, want 0", store.Len(), store.UsedBytes())
			}
		})
	}
}

func TestHandlerCacheSecretRedactionAndMutationInvalidation(t *testing.T) {
	const rawSecretToken = "super-secret-token"
	encodedSecretToken := base64.StdEncoding.EncodeToString([]byte(rawSecretToken))

	secretObjectFixture := `{"apiVersion":"v1","kind":"Secret","type":"Opaque",` +
		`"metadata":{"name":"s1","namespace":"default","annotations":{` +
		`"kubectl.kubernetes.io/last-applied-configuration":"{\"data\":{\"token\":\"` + encodedSecretToken + `\"}}"}},` +
		`"data":{"token":"` + encodedSecretToken + `"},"stringData":{"plain":"` + rawSecretToken + `"}}`

	secretPartialFixture := `{"apiVersion":"meta.k8s.io/v1","kind":"PartialObjectMetadata",` +
		`"metadata":{"name":"s1","namespace":"default","annotations":{` +
		`"kubectl.kubernetes.io/last-applied-configuration":"{\"data\":{\"token\":\"` + encodedSecretToken + `\"}}","team":"core"},` +
		`"managedFields":[{"manager":"kubectl","fieldsV1":{"f:data":{".":{},"f:token":{}}}}]}}`

	secretTableFixture := `{"apiVersion":"meta.k8s.io/v1","kind":"Table","columnDefinitions":[],` +
		`"rows":[{"cells":["s1","Opaque",1,"1m"],"object":` + secretObjectFixture + `}]}`

	store := NewMemoryCacheStore(DefaultCacheMaxBytes)
	var statusOverride int
	var callSeq int

	upstream := &recordingTransport{
		response: func(req *http.Request) *http.Response {
			callSeq++
			if statusOverride != 0 {
				return &http.Response{
					StatusCode: statusOverride,
					Header:     http.Header{headerContentType: []string{contentTypeJSON}},
					Body:       io.NopCloser(strings.NewReader(`{"apiVersion":"v1","kind":"Status","status":"Failure","code":` + strconv.Itoa(statusOverride) + `}`)),
					Request:    req,
				}
			}
			if strings.Contains(req.URL.Path, "/secrets") {
				body := secretPartialFixture
				if strings.Contains(req.Header.Get(headerAccept), "as=Table") {
					body = secretTableFixture
				}
				return &http.Response{
					StatusCode: http.StatusOK,
					Header:     http.Header{headerContentType: []string{contentTypeJSON}},
					Body:       io.NopCloser(strings.NewReader(body)),
					Request:    req,
				}
			}
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{headerContentType: []string{contentTypeJSON}},
				Body:       io.NopCloser(strings.NewReader(`{"kind":"PodList","apiVersion":"v1","seq":` + strconv.Itoa(callSeq) + `}`)),
				Request:    req,
			}
		},
	}

	metrics := NewMetrics()
	h, _ := newTestHandler(t, Config{
		Upstream: upstream,
		Policy:   Policy{Mode: ModeReadWrite, RedactSecrets: true},
		Metrics:  metrics,
		Cache:    &CacheConfig{Store: store},
	})

	// 1. Secret redaction before cache storage and isolation across Secret, PartialObjectMetadata, and Table views.
	secretViews := []struct {
		name       string
		accept     string
		wantKind   string
		wantRedVal bool
	}{
		{"Secret view", contentTypeJSON, kindSecret, true},
		{"PartialObjectMetadata view", acceptPartial, asPartial, false},
		{"Table view", acceptTable, asTable, true},
	}
	for _, sv := range secretViews {
		req1 := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/s1", nil)
		req1.Header.Set(headerAccept, sv.accept)
		rec1 := serve(h, req1)
		if rec1.Code != http.StatusOK || rec1.Header().Get(headerCacheStatus) != cacheStatusMiss {
			t.Fatalf("%s first GET code=%d status=%q: %s", sv.name, rec1.Code, rec1.Header().Get(headerCacheStatus), rec1.Body.String())
		}
		obj := decodeObject(t, rec1.Body.Bytes())
		if obj[fieldKind] != sv.wantKind {
			t.Errorf("%s kind = %v, want %q", sv.name, obj[fieldKind], sv.wantKind)
		}
		if sv.wantRedVal && !strings.Contains(rec1.Body.String(), redactedValue) {
			t.Errorf("%s body missing redacted placeholder %q: %s", sv.name, redactedValue, rec1.Body.String())
		}

		req2 := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/s1", nil)
		req2.Header.Set(headerAccept, sv.accept)
		rec2 := serve(h, req2)
		if rec2.Code != http.StatusOK || rec2.Header().Get(headerCacheStatus) != cacheStatusHit {
			t.Fatalf("%s second GET code=%d status=%q", sv.name, rec2.Code, rec2.Header().Get(headerCacheStatus))
		}
	}

	// Inspect raw bytes in MemoryCacheStore to verify raw secret material never entered memory.
	store.mu.Lock()
	if len(store.items) != 3 {
		t.Errorf("store has %d secret entries, want 3 isolated views", len(store.items))
	}
	for k, ent := range store.items {
		if bytes.Contains(ent.value, []byte(rawSecretToken)) || bytes.Contains(ent.value, []byte(encodedSecretToken)) {
			t.Errorf("cache key %q contains unredacted secret material: %s", k, ent.value)
		}
	}
	store.mu.Unlock()

	// 2. Mutation invalidation in ModeReadWrite:
	// Populate GET and LIST for default/pods, cluster-wide /api/v1/pods, and kube-system/pods.
	pathsToWarm := []string{
		"/api/v1/namespaces/default/pods/p1",
		"/api/v1/namespaces/default/pods",
		"/api/v1/pods",
		"/api/v1/namespaces/kube-system/pods",
	}
	for _, p := range pathsToWarm {
		if rec := serve(h, httptest.NewRequest(http.MethodGet, p, nil)); rec.Header().Get(headerCacheStatus) != cacheStatusMiss {
			t.Fatalf("warm %s status = %q, want miss", p, rec.Header().Get(headerCacheStatus))
		}
	}

	// A dryRun=All write does not invalidate cached entries.
	dryRunPost := httptest.NewRequest(http.MethodPost, "/api/v1/namespaces/default/pods?dryRun=All", strings.NewReader(`{"apiVersion":"v1","kind":"Pod"}`))
	if rec := serve(h, dryRunPost); rec.Code != http.StatusOK {
		t.Fatalf("dryRun POST code = %d", rec.Code)
	}
	if rec := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil)); rec.Header().Get(headerCacheStatus) != cacheStatusHit {
		t.Fatalf("after dryRun POST, default/pods status = %q, want hit", rec.Header().Get(headerCacheStatus))
	}

	// A non-dry-run POST on default/pods invalidates default/pods GET, default/pods LIST, and cluster-wide /api/v1/pods,
	// while leaving kube-system/pods cached.
	realPost := httptest.NewRequest(http.MethodPost, "/api/v1/namespaces/default/pods", strings.NewReader(`{"apiVersion":"v1","kind":"Pod"}`))
	if rec := serve(h, realPost); rec.Code != http.StatusOK {
		t.Fatalf("POST default/pods code = %d", rec.Code)
	}
	for _, invalidated := range []string{
		"/api/v1/namespaces/default/pods/p1",
		"/api/v1/namespaces/default/pods",
		"/api/v1/pods",
	} {
		if rec := serve(h, httptest.NewRequest(http.MethodGet, invalidated, nil)); rec.Header().Get(headerCacheStatus) != cacheStatusMiss {
			t.Errorf("after POST default/pods, %s status = %q, want miss", invalidated, rec.Header().Get(headerCacheStatus))
		}
	}
	if rec := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/kube-system/pods", nil)); rec.Header().Get(headerCacheStatus) != cacheStatusHit {
		t.Errorf("after POST default/pods, kube-system/pods status = %q, want hit", rec.Header().Get(headerCacheStatus))
	}

	// Non-dry-run DELETE on default/pods/p1 also invalidates default/pods and /api/v1/pods.
	if rec := serve(h, httptest.NewRequest(http.MethodDelete, "/api/v1/namespaces/default/pods/p1", nil)); rec.Code != http.StatusOK {
		t.Fatalf("DELETE default/pods/p1 code = %d", rec.Code)
	}
	for _, invalidated := range []string{
		"/api/v1/namespaces/default/pods/p1",
		"/api/v1/namespaces/default/pods",
		"/api/v1/pods",
	} {
		if rec := serve(h, httptest.NewRequest(http.MethodGet, invalidated, nil)); rec.Header().Get(headerCacheStatus) != cacheStatusMiss {
			t.Errorf("after DELETE default/pods/p1, %s status = %q, want miss", invalidated, rec.Header().Get(headerCacheStatus))
		}
	}

	// 3. Upstream 403 Forbidden and 401 Unauthorized on a resource invalidate cached entries for that resource scope.
	for _, authCode := range []int{http.StatusForbidden, http.StatusUnauthorized} {
		// Ensure default/pods is cached.
		_ = serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil))
		if rec := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil)); rec.Header().Get(headerCacheStatus) != cacheStatusHit {
			t.Fatalf("before %d test, default/pods status = %q, want hit", authCode, rec.Header().Get(headerCacheStatus))
		}

		// Trigger upstream 403/401 on another pod in the same namespace resource scope.
		statusOverride = authCode
		recAuth := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods/forbidden-pod", nil))
		if recAuth.Code != authCode {
			t.Fatalf("expected %d, got %d", authCode, recAuth.Code)
		}
		statusOverride = 0

		// Verify default/pods was invalidated by the auth failure.
		if rec := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil)); rec.Header().Get(headerCacheStatus) != cacheStatusMiss {
			t.Errorf("after upstream %d, default/pods status = %q, want miss", authCode, rec.Header().Get(headerCacheStatus))
		}
	}

	st := store.Stats()
	if st.Entries != 5 || st.Bytes <= 0 {
		t.Fatalf("Stats() = %+v, want Entries=5 Bytes>0", st)
	}
	exposition := scrapeMetricsText(t, metrics)
	for _, want := range []string{
		`kubeagents_kube_proxy_cache_evictions_total{reason="invalidated"} 10`,
		`kubeagents_kube_proxy_cache_bytes ` + strconv.FormatInt(st.Bytes, 10),
		`kubeagents_kube_proxy_cache_entries ` + strconv.Itoa(st.Entries),
	} {
		if !strings.Contains(exposition, want) {
			t.Errorf("metrics exposition missing %q\nFull exposition:\n%s", want, exposition)
		}
	}
}

func TestHandlerCacheLeaderCancelRefreshEvictionAndExpiredInvalidation(t *testing.T) {
	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	var nowMu sync.Mutex
	clock := func() time.Time {
		nowMu.Lock()
		defer nowMu.Unlock()
		return now
	}
	advance := func(d time.Duration) {
		nowMu.Lock()
		now = now.Add(d)
		nowMu.Unlock()
	}

	store := NewMemoryCacheStore(DefaultCacheMaxBytes)
	store.now = clock
	metrics := NewMetrics()

	var statusOverride atomic.Int32
	var respCCOverride atomic.Value
	respCCOverride.Store("")
	leaderEntered := make(chan struct{})
	releaseLeader := make(chan struct{})
	var closeOnce sync.Once
	var callSeq atomic.Int32

	upstream := &fakeUpstream{
		handler: func(_ string, req *http.Request) (*http.Response, error) {
			if req.URL.Path == "/api/v1/namespaces/default/services" {
				closeOnce.Do(func() { close(leaderEntered) })
				<-releaseLeader
				if err := req.Context().Err(); err != nil {
					return nil, err
				}
			}
			seq := callSeq.Add(1)
			code := int(statusOverride.Load())
			if code == 0 {
				code = http.StatusOK
			}
			hdr := http.Header{headerContentType: []string{contentTypeJSON}}
			if cc, _ := respCCOverride.Load().(string); cc != "" {
				hdr.Set(headerCacheControl, cc)
			}
			return &http.Response{
				StatusCode: code,
				Header:     hdr,
				Body:       io.NopCloser(strings.NewReader(`{"kind":"List","apiVersion":"v1","seq":` + strconv.Itoa(int(seq)) + `}`)),
				Request:    req,
			}, nil
		},
	}

	h, err := NewHandler(Config{
		Authenticator: shellCaller,
		Upstream:      upstream,
		Policy:        Policy{Mode: ModeReadWrite, RedactSecrets: true},
		Logger:        slog.New(slog.DiscardHandler),
		Metrics:       metrics,
		Cache: &CacheConfig{
			Store:              store,
			DefaultResourceTTL: 10 * time.Second,
			Now:                clock,
		},
	})
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}

	// 1. Singleflight leader context cancellation does not fail coalesced follower.
	ctxLeader, cancelLeader := context.WithCancel(context.Background())
	reqLeader := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/services", nil).WithContext(ctxLeader)
	reqFollower := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/services", nil)

	var recLeader, recFollower *httptest.ResponseRecorder
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		recLeader = serve(h, reqLeader)
	}()
	<-leaderEntered
	go func() {
		defer wg.Done()
		recFollower = serve(h, reqFollower)
	}()
	time.Sleep(15 * time.Millisecond)
	cancelLeader()
	time.Sleep(5 * time.Millisecond)
	close(releaseLeader)
	wg.Wait()

	if recLeader.Code != http.StatusBadGateway {
		t.Fatalf("canceled leader code = %d, want 502", recLeader.Code)
	}
	if recFollower.Code != http.StatusOK {
		t.Fatalf("coalesced follower code = %d, want 200: %s", recFollower.Code, recFollower.Body.String())
	}

	// 2. Forced refresh (no-cache / max-age=0) receiving non-200 or no-store evicts older entry.
	const podPath = "/api/v1/namespaces/default/pods/p1"
	if rec := serve(h, httptest.NewRequest(http.MethodGet, podPath, nil)); rec.Header().Get(headerCacheStatus) != cacheStatusMiss {
		t.Fatalf("initial GET status = %q, want miss", rec.Header().Get(headerCacheStatus))
	}
	if rec := serve(h, httptest.NewRequest(http.MethodGet, podPath, nil)); rec.Header().Get(headerCacheStatus) != cacheStatusHit {
		t.Fatalf("second GET status = %q, want hit", rec.Header().Get(headerCacheStatus))
	}

	statusOverride.Store(http.StatusNotFound)
	reqRefresh404 := httptest.NewRequest(http.MethodGet, podPath, nil)
	reqRefresh404.Header.Set(headerCacheControl, "no-cache")
	if rec := serve(h, reqRefresh404); rec.Code != http.StatusNotFound {
		t.Fatalf("refresh 404 code = %d, want 404", rec.Code)
	}
	statusOverride.Store(0)

	// Subsequent normal GET must miss because the 404 refresh evicted the stale 200 entry.
	if rec := serve(h, httptest.NewRequest(http.MethodGet, podPath, nil)); rec.Header().Get(headerCacheStatus) != cacheStatusMiss {
		t.Fatalf("GET after 404 refresh status = %q, want miss", rec.Header().Get(headerCacheStatus))
	}

	// Force refresh that returns 200 with upstream Cache-Control: no-store also evicts the older entry.
	respCCOverride.Store("no-store")
	reqRefreshNoStore := httptest.NewRequest(http.MethodGet, podPath, nil)
	reqRefreshNoStore.Header.Set(headerCacheControl, "max-age=0")
	if rec := serve(h, reqRefreshNoStore); rec.Header().Get(headerCacheStatus) != cacheStatusBypass {
		t.Fatalf("refresh no-store status = %q, want bypass", rec.Header().Get(headerCacheStatus))
	}
	respCCOverride.Store("")
	if rec := serve(h, httptest.NewRequest(http.MethodGet, podPath, nil)); rec.Header().Get(headerCacheStatus) != cacheStatusMiss {
		t.Fatalf("GET after no-store refresh status = %q, want miss", rec.Header().Get(headerCacheStatus))
	}

	// 3. Scope invalidation does not double-count entries that already expired before the mutation.
	advance(15 * time.Second)
	if rec := serve(h, httptest.NewRequest(http.MethodDelete, podPath, nil)); rec.Code != http.StatusOK {
		t.Fatalf("DELETE after TTL expiration code = %d, want 200", rec.Code)
	}
	exposition := scrapeMetricsText(t, metrics)
	if strings.Contains(exposition, `kubeagents_kube_proxy_cache_evictions_total{reason="invalidated"}`) {
		t.Errorf("expired entry should not be counted as invalidated eviction:\n%s", exposition)
	}
}

type auditRecordingSink struct {
	mu     sync.Mutex
	events []AuditEvent
}

func (s *auditRecordingSink) Record(_ context.Context, ev AuditEvent) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = append(s.events, ev)
}

func (s *auditRecordingSink) snapshot() []AuditEvent {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]AuditEvent, len(s.events))
	copy(out, s.events)
	return out
}

func TestHandlerCacheSecretRefusalAuditAndLeaderCancelRace(t *testing.T) {
	store := &countingGetStore{
		MemoryCacheStore: NewMemoryCacheStore(DefaultCacheMaxBytes),
		allSeen:          make(chan struct{}),
		target:           2,
	}
	releaseUpstream := make(chan struct{})
	auditSink := &auditRecordingSink{}

	upstream := &fakeUpstream{
		handler: func(_ string, req *http.Request) (*http.Response, error) {
			<-releaseUpstream
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{headerContentType: []string{contentTypeJSON}},
				Body:       io.NopCloser(strings.NewReader("not-valid-json")),
				Request:    req,
			}, nil
		},
	}

	h, err := NewHandler(Config{
		Authenticator: shellCaller,
		Upstream:      upstream,
		Policy:        Policy{Mode: ModeReadOnlyWithDryRun, RedactSecrets: true},
		Audit:         auditSink,
		Logger:        slog.New(slog.DiscardHandler),
		Cache:         &CacheConfig{Store: store},
	})
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}

	var rec1, rec2 *httptest.ResponseRecorder
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		rec1 = serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/bad", nil))
	}()
	go func() {
		defer wg.Done()
		rec2 = serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/bad", nil))
	}()

	<-store.allSeen
	time.Sleep(15 * time.Millisecond)
	close(releaseUpstream)
	wg.Wait()

	if rec1.Code != http.StatusBadGateway || rec2.Code != http.StatusBadGateway {
		t.Fatalf("codes = (%d, %d), want (502, 502)", rec1.Code, rec2.Code)
	}
	events := auditSink.snapshot()
	if len(events) != 2 {
		t.Fatalf("len(events) = %d, want 2", len(events))
	}
	for i, ev := range events {
		if ev.StatusCode != http.StatusBadGateway || ev.Err == nil {
			t.Errorf("event[%d] StatusCode=%d Err=%v, want StatusCode=502 and non-nil Err", i, ev.StatusCode, ev.Err)
		}
	}

	// Verify no data race on AuditEvent.Err when the leader cancels while a follower remains waiting
	// on an unredactable Secret fetch.
	storeRace := &countingGetStore{
		MemoryCacheStore: NewMemoryCacheStore(DefaultCacheMaxBytes),
		allSeen:          make(chan struct{}),
		target:           2,
	}
	releaseRace := make(chan struct{})
	hRace, err := NewHandler(Config{
		Authenticator: shellCaller,
		Upstream: &fakeUpstream{
			handler: func(_ string, req *http.Request) (*http.Response, error) {
				<-releaseRace
				return &http.Response{
					StatusCode: http.StatusOK,
					Header:     http.Header{headerContentType: []string{contentTypeJSON}},
					Body:       io.NopCloser(strings.NewReader("not-valid-json")),
					Request:    req,
				}, nil
			},
		},
		Policy: Policy{Mode: ModeReadOnlyWithDryRun, RedactSecrets: true},
		Audit:  &auditRecordingSink{},
		Logger: slog.New(slog.DiscardHandler),
		Cache:  &CacheConfig{Store: storeRace},
	})
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}

	ctxCancel, cancel := context.WithCancel(context.Background())
	var wgRace sync.WaitGroup
	wgRace.Add(2)
	go func() {
		defer wgRace.Done()
		serve(hRace, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/race", nil).WithContext(ctxCancel))
	}()
	go func() {
		defer wgRace.Done()
		serve(hRace, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/race", nil))
	}()
	<-storeRace.allSeen
	cancel()
	close(releaseRace)
	wgRace.Wait()
}

type trackingReadCloser struct {
	io.Reader
	closed atomic.Bool
}

func (r *trackingReadCloser) Close() error {
	r.closed.Store(true)
	return nil
}

func TestHandlerCacheFlightCancelTimeoutAndTooLargeCleanup(t *testing.T) {
	// 1. Sole waiter cancellation aborts upstream fetch immediately and does not wedge the key.
	entered := make(chan struct{})
	var callCount atomic.Int32
	upstream := &fakeUpstream{
		handler: func(_ string, req *http.Request) (*http.Response, error) {
			if callCount.Add(1) == 1 {
				close(entered)
				<-req.Context().Done()
				return nil, req.Context().Err()
			}
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{headerContentType: []string{contentTypeJSON}},
				Body:       io.NopCloser(strings.NewReader(`{"kind":"PodList","apiVersion":"v1","items":[]}`)),
				Request:    req,
			}, nil
		},
	}

	h, _ := newTestHandler(t, Config{
		Upstream: upstream,
		Cache:    &CacheConfig{},
	})

	ctx1, cancel1 := context.WithCancel(context.Background())
	done1 := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		done1 <- serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil).WithContext(ctx1))
	}()
	<-entered
	cancel1()
	rec1 := <-done1
	if rec1.Code != http.StatusBadGateway {
		t.Fatalf("canceled sole waiter code = %d, want 502", rec1.Code)
	}

	rec2 := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil))
	if rec2.Code != http.StatusOK {
		t.Fatalf("subsequent GET after canceled flight code = %d, want 200", rec2.Code)
	}

	// 2. Upstream stall is bounded by fetchTimeout.
	hTimeout, _ := newTestHandler(t, Config{
		Upstream: &fakeUpstream{
			handler: func(_ string, req *http.Request) (*http.Response, error) {
				<-req.Context().Done()
				return nil, req.Context().Err()
			},
		},
		Cache: &CacheConfig{},
	})
	hTimeout.(*handler).cache.fetchTimeout = 30 * time.Millisecond
	recTimeout := serve(hTimeout, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil))
	if recTimeout.Code != http.StatusBadGateway {
		t.Fatalf("stalled upstream code = %d, want 502", recTimeout.Code)
	}

	// 3. Unclaimed tooLarge streamResp.Body is closed when all waiters cancel.
	largeBody := strings.Repeat("x", 256)
	trackedBody := &trackingReadCloser{Reader: strings.NewReader(largeBody)}
	enteredLarge := make(chan struct{})
	releaseLarge := make(chan struct{})
	hLarge, _ := newTestHandler(t, Config{
		Upstream: &fakeUpstream{
			handler: func(_ string, req *http.Request) (*http.Response, error) {
				close(enteredLarge)
				<-releaseLarge
				return &http.Response{
					StatusCode:    http.StatusOK,
					Header:        http.Header{headerContentType: []string{contentTypeJSON}, headerContentLength: []string{strconv.Itoa(len(largeBody))}},
					Body:          trackedBody,
					ContentLength: int64(len(largeBody)),
					Request:       req,
				}, nil
			},
		},
		Cache: &CacheConfig{MaxEntryBytes: 32},
	})

	ctxLarge, cancelLarge := context.WithCancel(context.Background())
	doneLarge := make(chan struct{})
	go func() {
		defer close(doneLarge)
		serve(hLarge, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil).WithContext(ctxLarge))
	}()
	<-enteredLarge
	cancelLarge()
	<-doneLarge
	close(releaseLarge)

	deadline := time.Now().Add(time.Second)
	for !trackedBody.closed.Load() && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if !trackedBody.closed.Load() {
		t.Errorf("unclaimed tooLarge streamResp.Body was not closed after caller canceled")
	}
}

func TestHandlerCacheConditionalHeadersDoNotLeak304ToCoalescedCallers(t *testing.T) {
	store := &countingGetStore{
		MemoryCacheStore: NewMemoryCacheStore(DefaultCacheMaxBytes),
		allSeen:          make(chan struct{}),
		target:           2,
	}
	releaseUpstream := make(chan struct{})
	var sawConditional atomic.Bool

	upstream := &fakeUpstream{
		handler: func(_ string, req *http.Request) (*http.Response, error) {
			if req.Header.Get(headerIfNoneMatch) != "" || req.Header.Get(headerIfModifiedSince) != "" {
				sawConditional.Store(true)
				<-releaseUpstream
				return &http.Response{
					StatusCode: http.StatusNotModified,
					Header:     http.Header{"Etag": []string{`"v1"`}},
					Body:       io.NopCloser(strings.NewReader("")),
					Request:    req,
				}, nil
			}
			<-releaseUpstream
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{headerContentType: []string{contentTypeJSON}, "Etag": []string{`"v1"`}},
				Body:       io.NopCloser(strings.NewReader(`{"swagger":"2.0"}`)),
				Request:    req,
			}, nil
		},
	}

	h, _ := newTestHandler(t, Config{
		Upstream: upstream,
		Cache:    &CacheConfig{Store: store},
	})

	reqCond := httptest.NewRequest(http.MethodGet, "/openapi/v2", nil)
	reqCond.Header.Set(headerIfNoneMatch, `"v1"`)
	reqCond.Header.Set(headerIfModifiedSince, "Wed, 21 Oct 2015 07:28:00 GMT")
	reqUncond := httptest.NewRequest(http.MethodGet, "/openapi/v2", nil)

	var recCond, recUncond *httptest.ResponseRecorder
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		recCond = serve(h, reqCond)
	}()
	go func() {
		defer wg.Done()
		recUncond = serve(h, reqUncond)
	}()

	<-store.allSeen
	time.Sleep(15 * time.Millisecond)
	close(releaseUpstream)
	wg.Wait()

	if sawConditional.Load() {
		t.Errorf("upstream received conditional headers from cacheable fetch")
	}
	for name, rec := range map[string]*httptest.ResponseRecorder{"conditional": recCond, "unconditional": recUncond} {
		if rec.Code != http.StatusOK || rec.Body.String() != `{"swagger":"2.0"}` {
			t.Errorf("%s caller got code=%d body=%q, want 200 and full body", name, rec.Code, rec.Body.String())
		}
	}
}

func TestHandlerCacheScopeKeysPrunedOnExpiryAndEviction(t *testing.T) {
	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	var nowMu sync.Mutex
	clock := func() time.Time {
		nowMu.Lock()
		defer nowMu.Unlock()
		return now
	}
	advance := func(d time.Duration) {
		nowMu.Lock()
		now = now.Add(d)
		nowMu.Unlock()
	}

	store := NewMemoryCacheStore(220)
	store.now = clock
	h, _ := newTestHandler(t, Config{
		Cache: &CacheConfig{
			Store:              store,
			DefaultResourceTTL: 5 * time.Second,
			Now:                clock,
		},
	})
	rc := h.(*handler).cache

	// Insert two entries in ns-a and ns-b; tight store capacity evicts ns-a when ns-b is stored.
	serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/ns-a/pods", nil))
	serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/ns-b/pods", nil))

	rc.scopeMu.Lock()
	if _, exists := rc.scopeKeys[resourceScopeID(InClusterID, "", "v1", "pods", "ns-a")]; exists {
		t.Errorf("capacity-evicted ns-a scope should have been removed from scopeKeys")
	}
	rc.scopeMu.Unlock()

	// Advance past TTL and record a new key in ns-c; expired ns-b scope must be pruned.
	advance(6 * time.Second)
	serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/ns-c/pods", nil))

	rc.scopeMu.Lock()
	if _, exists := rc.scopeKeys[resourceScopeID(InClusterID, "", "v1", "pods", "ns-b")]; exists {
		t.Errorf("expired ns-b scope should have been pruned from scopeKeys")
	}
	if len(rc.keyScope) != 1 {
		t.Errorf("len(rc.keyScope) = %d, want 1", len(rc.keyScope))
	}
	rc.scopeMu.Unlock()
}

func TestHandlerCacheForcedRefreshTooLargeEvictsStaleEntry(t *testing.T) {
	const maxEntry = 64
	smallBody := `{"kind":"PodList","apiVersion":"v1","items":[]}`
	largeBody := `{"kind":"PodList","apiVersion":"v1","padding":"` + strings.Repeat("x", 128) + `"}`

	for _, chunked := range []bool{false, true} {
		var returnLarge atomic.Bool
		var seq atomic.Int32
		store := NewMemoryCacheStore(DefaultCacheMaxBytes)
		upstream := &recordingTransport{
			response: func(req *http.Request) *http.Response {
				n := seq.Add(1)
				body := smallBody
				if returnLarge.Load() {
					body = largeBody
				} else {
					body = `{"kind":"PodList","apiVersion":"v1","seq":` + strconv.Itoa(int(n)) + `}`
				}
				cl := int64(len(body))
				hdr := http.Header{headerContentType: []string{contentTypeJSON}}
				if chunked {
					cl = -1
				} else {
					hdr.Set(headerContentLength, strconv.FormatInt(cl, 10))
				}
				return &http.Response{
					StatusCode:    http.StatusOK,
					Header:        hdr,
					Body:          io.NopCloser(strings.NewReader(body)),
					ContentLength: cl,
					Request:       req,
				}
			},
		}

		h, _ := newTestHandler(t, Config{
			Upstream: upstream,
			Cache: &CacheConfig{
				Store:         store,
				MaxEntryBytes: maxEntry,
			},
		})

		const path = "/api/v1/namespaces/default/pods"
		if rec := serve(h, httptest.NewRequest(http.MethodGet, path, nil)); rec.Header().Get(headerCacheStatus) != cacheStatusMiss {
			t.Fatalf("chunked=%v initial GET status = %q, want miss", chunked, rec.Header().Get(headerCacheStatus))
		}
		if rec := serve(h, httptest.NewRequest(http.MethodGet, path, nil)); rec.Header().Get(headerCacheStatus) != cacheStatusHit {
			t.Fatalf("chunked=%v second GET status = %q, want hit", chunked, rec.Header().Get(headerCacheStatus))
		}

		// Force refresh when upstream response now exceeds MaxEntryBytes.
		returnLarge.Store(true)
		reqRefresh := httptest.NewRequest(http.MethodGet, path, nil)
		reqRefresh.Header.Set(headerCacheControl, "no-cache")
		recRefresh := serve(h, reqRefresh)
		if recRefresh.Header().Get(headerCacheStatus) != cacheStatusBypass || recRefresh.Body.String() != largeBody {
			t.Fatalf("chunked=%v refresh got status=%q bodyLen=%d", chunked, recRefresh.Header().Get(headerCacheStatus), recRefresh.Body.Len())
		}

		// Return to small responses and verify the stale pre-refresh entry was evicted.
		returnLarge.Store(false)
		recAfter := serve(h, httptest.NewRequest(http.MethodGet, path, nil))
		if recAfter.Header().Get(headerCacheStatus) != cacheStatusMiss || !strings.Contains(recAfter.Body.String(), `"seq":3`) {
			t.Errorf("chunked=%v GET after tooLarge refresh got status=%q body=%s, want miss with seq 3", chunked, recAfter.Header().Get(headerCacheStatus), recAfter.Body.String())
		}
	}
}

func TestParseCacheControlLargeDeltaSecondsClamping(t *testing.T) {
	reqHdr := http.Header{
		headerCacheControl: []string{"max-age=9999999999, max-stale=999999999999999999999999"},
	}
	reqCC := parseRequestCacheControl(reqHdr)
	wantMax := time.Duration(maxDeltaSeconds) * time.Second
	if !reqCC.hasMaxAge || reqCC.maxAge != wantMax {
		t.Errorf("reqCC.maxAge = %v (has=%v), want %v", reqCC.maxAge, reqCC.hasMaxAge, wantMax)
	}
	if !reqCC.hasStale || reqCC.maxStale != wantMax {
		t.Errorf("reqCC.maxStale = %v (has=%v), want %v", reqCC.maxStale, reqCC.hasStale, wantMax)
	}

	invalidStale := parseRequestCacheControl(http.Header{headerCacheControl: []string{"max-stale=invalid"}})
	if invalidStale.hasStale {
		t.Errorf("max-stale=invalid set hasStale=true")
	}

	respCC := parseResponseCacheControl(http.Header{headerCacheControl: []string{"max-age=9999999999"}})
	if !respCC.hasMaxAge || respCC.maxAge != wantMax {
		t.Errorf("respCC.maxAge = %v (has=%v), want %v", respCC.maxAge, respCC.hasMaxAge, wantMax)
	}

	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	entry := &cachedResponse{
		meta: cachedResponseMeta{
			StoredAt: now.Add(-5 * time.Second).UnixNano(),
			FreshTTL: int64(10 * time.Second),
		},
	}
	if !isEntryFreshForRequest(entry, reqCC, now) {
		t.Errorf("entry with large clamped max-age and max-stale should be fresh")
	}
}

func TestNormalizeAcceptEncodingQualityAndOutboundHeader(t *testing.T) {
	cases := []struct {
		header string
		want   string
	}{
		{"", encodingIdentity},
		{"gzip", encodingGzip},
		{"gzip;q=1.0", encodingGzip},
		{"br, gzip;q=0.8", encodingGzip},
		{"gzip;q=0", encodingIdentity},
		{"gzip;q=0.000, identity;q=1", encodingIdentity},
		{"gzip;q=invalid", encodingIdentity},
		{"br, deflate", encodingIdentity},
	}
	for _, tc := range cases {
		h := http.Header{}
		if tc.header != "" {
			h.Set(headerAcceptEncoding, tc.header)
		}
		if got := normalizeAcceptEncoding(h); got != tc.want {
			t.Errorf("normalizeAcceptEncoding(%q) = %q, want %q", tc.header, got, tc.want)
		}
	}

	var seenEncodings []string
	upstream := &recordingTransport{
		response: func(req *http.Request) *http.Response {
			enc := req.Header.Get(headerAcceptEncoding)
			seenEncodings = append(seenEncodings, enc)
			hdr := http.Header{headerContentType: []string{contentTypeJSON}}
			if enc == encodingGzip {
				hdr.Set(headerContentEncoding, encodingGzip)
			}
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     hdr,
				Body:       io.NopCloser(strings.NewReader(`{"kind":"PodList","apiVersion":"v1","enc":` + strconv.Quote(enc) + `}`)),
				Request:    req,
			}
		},
	}
	h, _ := newTestHandler(t, Config{
		Upstream: upstream,
		Cache:    &CacheConfig{},
	})

	reqBrGzip := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil)
	reqBrGzip.Header.Set(headerAcceptEncoding, "br, gzip")
	rec1 := serve(h, reqBrGzip)
	if rec1.Header().Get(headerCacheStatus) != cacheStatusMiss || !strings.Contains(rec1.Body.String(), `"enc":"gzip"`) {
		t.Fatalf("br, gzip got status=%q body=%s", rec1.Header().Get(headerCacheStatus), rec1.Body.String())
	}

	reqRejectGzip := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil)
	reqRejectGzip.Header.Set(headerAcceptEncoding, "gzip;q=0, identity;q=1")
	rec2 := serve(h, reqRejectGzip)
	if rec2.Header().Get(headerCacheStatus) != cacheStatusMiss || !strings.Contains(rec2.Body.String(), `"enc":""`) {
		t.Fatalf("gzip;q=0 got status=%q body=%s, want miss with empty outbound Accept-Encoding", rec2.Header().Get(headerCacheStatus), rec2.Body.String())
	}
}
