package k8sapiproxy

import (
	"bytes"
	"container/list"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/apiserver/pkg/endpoints/request"
)

const (
	// DefaultCacheMaxBytes is the default capacity of the built-in memory cache (64 MiB).
	DefaultCacheMaxBytes int64 = 64 << 20
	// DefaultCacheMaxEntryBytes is the default maximum size of a single cached response body (2 MiB).
	DefaultCacheMaxEntryBytes int64 = 2 << 20
	// DefaultResourceCacheTTL is the default TTL for cached resource get and list responses.
	// It defaults to 0 so per-pod MemoryCacheStore replicas never return non-monotonic state across consecutive kubectl get calls.
	// The proxy still coalesces concurrent in-flight GET and LIST requests when the TTL is 0.
	// Set --cache-resource-ttl (KUBE_PROXY_CACHE_RESOURCE_TTL) or CacheConfig.DefaultResourceTTL to cache resource responses after a flight only when callers stick to one replica (sessionAffinity): invalidation is per replica.
	DefaultResourceCacheTTL time.Duration = 0
	// DefaultDiscoveryCacheTTL is the default TTL for cached discovery responses.
	// It is short because nothing the proxy sees invalidates discovery: a CRD or
	// APIService installed out of band must reach kubectl within one TTL.
	DefaultDiscoveryCacheTTL = 30 * time.Second

	headerCacheControl      = "Cache-Control"
	headerCacheStatus       = "Cache-Status"
	headerAuditID           = "Audit-Id"
	headerAge               = "Age"
	headerIfNoneMatch       = "If-None-Match"
	headerIfModifiedSince   = "If-Modified-Since"
	headerIfMatch           = "If-Match"
	headerIfUnmodifiedSince = "If-Unmodified-Since"
	headerIfRange           = "If-Range"

	cacheStatusHit       = "k8s-api-proxy; hit"
	cacheStatusCoalesced = "k8s-api-proxy; hit; coalesced"
	cacheStatusMiss      = "k8s-api-proxy; miss"
	// #nosec G101 -- RFC 9211 Cache-Status header value, not a credential.
	cacheStatusBypass = "k8s-api-proxy; bypass"

	cacheEnvelopeMagic     = "KPC1"
	cacheEnvelopeMagicSize = 4
	cacheEnvelopeMinSize   = 8
	maxCacheMetaBytes      = 1 << 20
	maxCacheBodyBytes      = 64 << 20
	maxEntryHitCount       = 15
	cacheKeyHashBytes      = 16

	defaultMaxStaleDuration     = 24 * time.Hour
	maxDeltaSeconds             = 1 << 31
	defaultUpstreamFetchTimeout = 60 * time.Second
)

var (
	nonCachedDiscoveryPaths = sets.New("/healthz", "/livez", "/readyz", "/metrics")
	mutatingVerbs           = sets.New(verbCreate, verbUpdate, verbPatch, verbDelete, verbDeleteCollection)
	hopByHopResponseHeaders = sets.New(
		"Connection",
		"Keep-Alive",
		"Proxy-Authenticate",
		"Proxy-Authorization",
		"Te",
		"Trailer",
		"Transfer-Encoding",
		"Upgrade",
		"Date",
		"Age",
		"Cache-Status",
		// The API server's ID for one request; replayed, it would name another caller's request.
		"Audit-Id",
	)
)

// CacheStore stores and retrieves serialized HTTP responses by key.
// Implementations must be safe for concurrent use by multiple goroutines.
type CacheStore interface {
	Get(ctx context.Context, key string) ([]byte, bool, error)
	Set(ctx context.Context, key string, value []byte, ttl time.Duration) error
	Delete(ctx context.Context, key string) error
}

// CacheStats holds point-in-time occupancy and eviction counters for a CacheStore.
type CacheStats struct {
	Bytes           int64
	Entries         int
	CapacityEvicted uint64
	ExpiredEvicted  uint64
}

// CacheStatsProvider is an optional interface that a CacheStore may implement to
// report its current byte size and entry count.
type CacheStatsProvider interface {
	Stats() CacheStats
}

// CacheConfig configures HTTP response caching and in-flight request coalescing.
type CacheConfig struct {
	// Store holds cached responses; NewMemoryCacheStore(MaxBytes) when nil.
	Store CacheStore
	// MaxBytes bounds the built-in memory store in bytes; DefaultCacheMaxBytes when <= 0.
	// Ignored when Store is non-nil.
	MaxBytes int64
	// MaxEntryBytes bounds the maximum response body size in bytes that can be cached;
	// DefaultCacheMaxEntryBytes when <= 0, and at most 64 MiB and the memory store's
	// MaxBytes. Responses larger than MaxEntryBytes stream
	// directly to the caller without full buffering.
	MaxEntryBytes int64
	// DefaultResourceTTL is the default TTL for resource get and list responses;
	// DefaultResourceCacheTTL (0s, which coalesces in-flight requests without post-flight storage) when <= 0.
	DefaultResourceTTL time.Duration
	// DefaultDiscoveryTTL is the default TTL for discovery responses;
	// DefaultDiscoveryCacheTTL when <= 0.
	DefaultDiscoveryTTL time.Duration
	// Now returns the current time for TTL and Age calculations; time.Now when nil.
	Now func() time.Time
	// HostClusterID is the gke_ ID the upstream also routes to the in-cluster API server, if
	// any. The cache files it under InClusterID so a write through either name invalidates
	// reads through the other.
	HostClusterID string
}

type memoryCacheEntry struct {
	key       string
	value     []byte
	expiresAt time.Time
	hits      uint32
	element   *list.Element
}

// MemoryCacheStore is a thread-safe, byte-bounded in-memory CacheStore with TTL expiration
// and frequency-aware LRU eviction.
type MemoryCacheStore struct {
	mu              sync.Mutex
	maxBytes        int64
	usedBytes       int64
	capacityEvicted uint64
	expiredEvicted  uint64
	items           map[string]*memoryCacheEntry
	order           *list.List
	now             func() time.Time
	onEvict         func(key, reason string)
	onOccupancy     func(bytes int64, entries int)
}

// NewMemoryCacheStore returns a MemoryCacheStore bounded to maxBytes.
// When maxBytes <= 0, DefaultCacheMaxBytes is used.
func NewMemoryCacheStore(maxBytes int64) *MemoryCacheStore {
	if maxBytes <= 0 {
		maxBytes = DefaultCacheMaxBytes
	}
	return &MemoryCacheStore{
		maxBytes: maxBytes,
		items:    make(map[string]*memoryCacheEntry),
		order:    list.New(),
		now:      time.Now,
	}
}

// Get returns a copy of the cached value for key when present and not expired.
func (s *MemoryCacheStore) Get(ctx context.Context, key string) ([]byte, bool, error) {
	if err := ctx.Err(); err != nil {
		return nil, false, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	ent, ok := s.items[key]
	if !ok {
		return nil, false, nil
	}
	if !s.now().Before(ent.expiresAt) {
		s.removeEntryLocked(ent)
		s.expiredEvicted++
		if s.onEvict != nil {
			s.onEvict(ent.key, CacheEvictionExpired)
		}
		if s.onOccupancy != nil {
			s.onOccupancy(s.usedBytes, len(s.items))
		}
		return nil, false, nil
	}
	if ent.hits < maxEntryHitCount {
		ent.hits++
	}
	s.order.MoveToFront(ent.element)
	return bytes.Clone(ent.value), true, nil
}

// Set stores value under key for ttl, evicting expired and cold entries to stay within maxBytes.
func (s *MemoryCacheStore) Set(ctx context.Context, key string, value []byte, ttl time.Duration) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	if existing, ok := s.items[key]; ok {
		s.removeEntryLocked(existing)
		if s.onOccupancy != nil {
			s.onOccupancy(s.usedBytes, len(s.items))
		}
	}
	if ttl <= 0 || int64(len(value)) > s.maxBytes {
		return nil
	}

	now := s.now()
	needed := int64(len(value))
	if s.usedBytes+needed > s.maxBytes {
		s.evictExpiredLocked(now)
	}
	for s.usedBytes+needed > s.maxBytes && s.order.Len() > 0 {
		s.evictOneLocked()
	}

	copied := bytes.Clone(value)
	ent := &memoryCacheEntry{
		key:       key,
		value:     copied,
		expiresAt: now.Add(ttl),
	}
	ent.element = s.order.PushFront(ent)
	s.items[key] = ent
	s.usedBytes += needed
	if s.onOccupancy != nil {
		s.onOccupancy(s.usedBytes, len(s.items))
	}
	return nil
}

// Delete removes key from the store if present.
func (s *MemoryCacheStore) Delete(ctx context.Context, key string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	if ent, ok := s.items[key]; ok {
		s.removeEntryLocked(ent)
		if s.onOccupancy != nil {
			s.onOccupancy(s.usedBytes, len(s.items))
		}
	}
	return nil
}

// Stats returns point-in-time occupancy and eviction counters for the store.
func (s *MemoryCacheStore) Stats() CacheStats {
	s.mu.Lock()
	defer s.mu.Unlock()
	return CacheStats{
		Bytes:           s.usedBytes,
		Entries:         len(s.items),
		CapacityEvicted: s.capacityEvicted,
		ExpiredEvicted:  s.expiredEvicted,
	}
}

// UsedBytes returns the current number of value bytes stored in the cache.
func (s *MemoryCacheStore) UsedBytes() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.usedBytes
}

// Len returns the current number of entries in the cache.
func (s *MemoryCacheStore) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.items)
}

func (s *MemoryCacheStore) evictExpiredLocked(now time.Time) {
	for el := s.order.Back(); el != nil; {
		prev := el.Prev()
		ent := el.Value.(*memoryCacheEntry)
		if !now.Before(ent.expiresAt) {
			s.removeEntryLocked(ent)
			s.expiredEvicted++
			if s.onEvict != nil {
				s.onEvict(ent.key, CacheEvictionExpired)
			}
			if s.onOccupancy != nil {
				s.onOccupancy(s.usedBytes, len(s.items))
			}
		}
		el = prev
	}
}

// evictOneLocked evicts one entry using second-chance frequency decay so frequently accessed
// entries survive one-time list scans while equal-frequency entries follow strict LRU order.
func (s *MemoryCacheStore) evictOneLocked() {
	maxSteps := s.order.Len()
	for i := 0; i < maxSteps; i++ {
		back := s.order.Back()
		if back == nil {
			return
		}
		ent := back.Value.(*memoryCacheEntry)
		if ent.hits == 0 {
			s.removeEntryLocked(ent)
			s.capacityEvicted++
			if s.onEvict != nil {
				s.onEvict(ent.key, CacheEvictionCapacity)
			}
			if s.onOccupancy != nil {
				s.onOccupancy(s.usedBytes, len(s.items))
			}
			return
		}
		ent.hits >>= 1
		s.order.MoveToFront(back)
	}
	if back := s.order.Back(); back != nil {
		ent := back.Value.(*memoryCacheEntry)
		s.removeEntryLocked(ent)
		s.capacityEvicted++
		if s.onEvict != nil {
			s.onEvict(ent.key, CacheEvictionCapacity)
		}
		if s.onOccupancy != nil {
			s.onOccupancy(s.usedBytes, len(s.items))
		}
	}
}

func (s *MemoryCacheStore) removeEntryLocked(ent *memoryCacheEntry) {
	delete(s.items, ent.key)
	s.order.Remove(ent.element)
	s.usedBytes -= int64(len(ent.value))
}

type cacheRequestMetaKey struct{}

type cacheRequestMeta struct {
	info         *request.RequestInfo
	callerAccept string
}

type inflightCall struct {
	done   chan struct{}
	cancel context.CancelFunc
	// epoch is responseCache.epoch when the flight started.
	epoch uint64

	val *sfResult
	err error

	// Guarded by responseCache.flightMu.
	waiters  int
	shared   bool
	finished bool
}

type responseCache struct {
	store               CacheStore
	maxEntryBytes       int64
	defaultResourceTTL  time.Duration
	defaultDiscoveryTTL time.Duration
	fetchTimeout        time.Duration
	now                 func() time.Time
	metrics             *Metrics
	hostClusterID       string

	flightMu sync.Mutex
	flights  map[string]*inflightCall
	// epoch counts invalidations. A flight started before one may carry
	// pre-write state, so a later request does not join it and its result
	// is not stored.
	epoch atomic.Uint64

	scopeMu   sync.Mutex
	scopeKeys map[string]map[string]time.Time
	keyScope  map[string]string
	// staleKeys holds keys whose eviction the store refused. A read treats them as a miss
	// until a later store or delete of the key succeeds.
	staleKeys map[string]struct{}
}

func newResponseCache(cfg *CacheConfig, metrics *Metrics) *responseCache {
	if cfg == nil {
		return nil
	}
	nowFn := cfg.Now
	if nowFn == nil {
		nowFn = time.Now
	}
	store := cfg.Store
	if store == nil {
		mem := NewMemoryCacheStore(cfg.MaxBytes)
		mem.now = nowFn
		store = mem
	}
	maxEntry := cfg.MaxEntryBytes
	if maxEntry <= 0 {
		maxEntry = DefaultCacheMaxEntryBytes
	}
	// A body the envelope or the memory store cannot hold would be buffered and never stored.
	maxEntry = min(maxEntry, maxCacheBodyBytes)
	if mem, ok := store.(*MemoryCacheStore); ok {
		maxEntry = min(maxEntry, mem.maxBytes)
	}
	resTTL := cfg.DefaultResourceTTL
	if resTTL <= 0 {
		resTTL = DefaultResourceCacheTTL
	}
	discTTL := cfg.DefaultDiscoveryTTL
	if discTTL <= 0 {
		discTTL = DefaultDiscoveryCacheTTL
	}
	c := &responseCache{
		store:               store,
		maxEntryBytes:       maxEntry,
		defaultResourceTTL:  resTTL,
		defaultDiscoveryTTL: discTTL,
		fetchTimeout:        defaultUpstreamFetchTimeout,
		now:                 nowFn,
		metrics:             metrics,
		hostClusterID:       cfg.HostClusterID,
		flights:             make(map[string]*inflightCall),
		scopeKeys:           make(map[string]map[string]time.Time),
		keyScope:            make(map[string]string),
		staleKeys:           make(map[string]struct{}),
	}
	if mem, ok := store.(*MemoryCacheStore); ok {
		mem.mu.Lock()
		if mem.onEvict == nil {
			mem.onEvict = func(key, reason string) {
				c.removeScopeKey(key)
				metrics.recordCacheEviction(reason)
			}
		}
		if mem.onOccupancy == nil {
			mem.onOccupancy = func(bytes int64, entries int) {
				metrics.setCacheOccupancy(bytes, entries)
			}
		}
		mem.mu.Unlock()
	}
	if sp, ok := store.(CacheStatsProvider); ok {
		st := sp.Stats()
		metrics.setCacheOccupancy(st.Bytes, st.Entries)
	}
	return c
}

type sfResult struct {
	statusCode  int
	header      http.Header
	body        []byte
	cacheStatus string
	metric      string
	auditErr    error
	// auditID is the upstream's Audit-Id, which only the leader's response carries.
	auditID       string
	leaderClaimed atomic.Bool

	tooLarge      bool
	streamClaimed atomic.Bool
	streamResp    *http.Response
}

func (r *sfResult) claimLeader() bool {
	return r.leaderClaimed.CompareAndSwap(false, true)
}

func (r *sfResult) claimStream() bool {
	return r.streamClaimed.CompareAndSwap(false, true)
}

func (c *responseCache) roundTrip(req *http.Request, next func(*http.Request) (*http.Response, error)) (*http.Response, error) {
	ev := eventFrom(req)
	meta, _ := req.Context().Value(cacheRequestMetaKey{}).(cacheRequestMeta)
	info := meta.info
	clusterID := c.cacheClusterID(ev.ClusterID)

	if info != nil && info.IsResourceRequest && !ev.DryRun && mutatingVerbs.Has(info.Verb) {
		resp, err := next(req)
		if err == nil && resp != nil && shouldInvalidateOnMutation(resp.StatusCode) {
			c.invalidateResourceScope(req.Context(), clusterID, info)
		}
		return resp, err
	}

	cacheable, isDiscovery := isCacheableRequest(req, info, ev.DryRun)
	if !cacheable {
		resp, err := next(req)
		if err == nil && resp != nil && info != nil && info.IsResourceRequest && isAuthFailureStatus(resp.StatusCode) {
			c.invalidateResourceScope(req.Context(), clusterID, info)
		}
		return resp, err
	}

	verbLabel, resLabel, _ := ClassifyRequestLabels(req, info)
	recordResult := func(result string) {
		ev.Cache = result
		c.metrics.recordCacheRequest(result, verbLabel, resLabel)
	}
	reqCC := parseRequestCacheControl(req.Header)
	if reqCC.noStore || isConditionalRequest(req.Header) {
		recordResult(CacheResultBypass)
		resp, err := next(req)
		if err == nil && resp != nil {
			if info != nil && info.IsResourceRequest && isAuthFailureStatus(resp.StatusCode) {
				c.invalidateResourceScope(req.Context(), clusterID, info)
			}
			resp.Header.Set(headerCacheStatus, cacheStatusBypass)
		}
		return resp, err
	}

	key := buildCacheKey(clusterID, req, meta.callerAccept)
	now := c.now()

	if !reqCC.noCache && !reqCC.zeroMaxAge {
		if raw, ok, err := c.store.Get(req.Context(), key); err == nil && ok && !c.isStale(key) {
			if entry, err := decodeCachedResponse(raw); err == nil && isEntryFreshForRequest(entry, reqCC, now) {
				recordResult(CacheResultHit)
				return buildResponseFromCache(req, entry, now), nil
			}
		}
	}

	upReq := prepareCacheableUpstreamRequest(req)
	call := c.acquireFlight(upReq, key, clusterID, info, isDiscovery, next)

	select {
	case <-req.Context().Done():
		c.releaseWaiterOnCancel(key, call)
		return nil, req.Context().Err()
	case <-call.done:
	}
	if call.err != nil {
		return nil, call.err
	}

	out := call.val
	if out.auditErr != nil {
		ev.Err = out.auditErr
	}
	if out.tooLarge {
		recordResult(CacheResultBypass)
		if out.claimStream() {
			out.streamResp.Header.Set(headerCacheStatus, cacheStatusBypass)
			return out.streamResp, nil
		}
		resp, err := next(upReq)
		if err == nil && resp != nil {
			resp.Header.Set(headerCacheStatus, cacheStatusBypass)
		}
		return resp, err
	}

	statusHeader := out.cacheStatus
	metricResult := out.metric
	if call.shared && !out.claimLeader() {
		statusHeader = cacheStatusCoalesced
		metricResult = CacheResultCoalesced
	}
	recordResult(metricResult)

	hdr := cloneHeader(out.header)
	if statusHeader != "" {
		hdr.Set(headerCacheStatus, statusHeader)
	}
	if statusHeader == cacheStatusCoalesced {
		hdr.Set(headerAge, "0")
	}
	if statusHeader != cacheStatusCoalesced && out.auditID != "" {
		hdr.Set(headerAuditID, out.auditID)
	}
	hdr.Set(headerContentLength, strconv.Itoa(len(out.body)))
	return &http.Response{
		StatusCode:    out.statusCode,
		Status:        strconv.Itoa(out.statusCode) + " " + http.StatusText(out.statusCode),
		Proto:         "HTTP/1.1",
		ProtoMajor:    1,
		ProtoMinor:    1,
		Header:        hdr,
		Body:          io.NopCloser(bytes.NewReader(out.body)),
		ContentLength: int64(len(out.body)),
		Request:       req,
	}, nil
}

func (c *responseCache) acquireFlight(
	req *http.Request,
	key string,
	clusterID string,
	info *request.RequestInfo,
	isDiscovery bool,
	next func(*http.Request) (*http.Response, error),
) *inflightCall {
	c.flightMu.Lock()
	epoch := c.epoch.Load()
	if call, ok := c.flights[key]; ok && call.epoch == epoch {
		call.waiters++
		call.shared = true
		c.flightMu.Unlock()
		return call
	}
	fetchTimeout := c.fetchTimeout
	if fetchTimeout <= 0 {
		fetchTimeout = defaultUpstreamFetchTimeout
	}
	fetchCtx, cancel := context.WithTimeout(context.WithoutCancel(req.Context()), fetchTimeout)
	call := &inflightCall{
		done:    make(chan struct{}),
		cancel:  cancel,
		epoch:   epoch,
		waiters: 1,
	}
	c.flights[key] = call
	c.flightMu.Unlock()

	go c.runFlight(fetchCtx, key, call, clusterID, req, info, isDiscovery, next)
	return call
}

func (c *responseCache) runFlight(
	fetchCtx context.Context,
	key string,
	call *inflightCall,
	clusterID string,
	req *http.Request,
	info *request.RequestInfo,
	isDiscovery bool,
	next func(*http.Request) (*http.Response, error),
) {
	sfEv := &AuditEvent{ClusterID: clusterID}
	sfReq := req.WithContext(context.WithValue(fetchCtx, auditEventKey{}, sfEv))
	val, err := c.fetchAndPopulate(sfReq, sfEv, key, clusterID, info, isDiscovery, next, call.cancel, call.epoch)

	var closeUnclaimed io.Closer
	c.flightMu.Lock()
	if c.flights[key] == call {
		delete(c.flights, key)
	}
	call.finished = true
	call.val = val
	call.err = err
	if val != nil && val.tooLarge && val.streamResp != nil && call.waiters == 0 {
		if val.claimStream() {
			closeUnclaimed = val.streamResp.Body
		}
	}
	close(call.done)
	c.flightMu.Unlock()

	if closeUnclaimed != nil {
		_ = closeUnclaimed.Close()
		call.cancel()
	} else if val == nil || !val.tooLarge {
		call.cancel()
	}
}

func (c *responseCache) releaseWaiterOnCancel(key string, call *inflightCall) {
	var closeUnclaimed io.Closer
	var shouldCancel bool

	c.flightMu.Lock()
	call.waiters--
	if call.waiters == 0 {
		if !call.finished {
			if c.flights[key] == call {
				delete(c.flights, key)
			}
			shouldCancel = true
		} else if call.val != nil && call.val.tooLarge && call.val.streamResp != nil && call.val.claimStream() {
			closeUnclaimed = call.val.streamResp.Body
			shouldCancel = true
		}
	}
	c.flightMu.Unlock()

	if closeUnclaimed != nil {
		_ = closeUnclaimed.Close()
	}
	if shouldCancel {
		call.cancel()
	}
}

func (c *responseCache) fetchAndPopulate(
	req *http.Request,
	sfEv *AuditEvent,
	key string,
	clusterID string,
	info *request.RequestInfo,
	isDiscovery bool,
	next func(*http.Request) (*http.Response, error),
	cancelFetch context.CancelFunc,
	epoch uint64,
) (*sfResult, error) {
	resp, err := next(req)
	if err != nil {
		return nil, err
	}
	if info != nil && info.IsResourceRequest && isAuthFailureStatus(resp.StatusCode) {
		c.invalidateResourceScope(req.Context(), clusterID, info)
	}

	if resp.ContentLength > c.maxEntryBytes {
		c.evictKey(req.Context(), key)
		resp.Body = &cancelOnCloseReadCloser{
			ReadCloser: resp.Body,
			cancel:     cancelFetch,
		}
		return &sfResult{tooLarge: true, streamResp: resp, auditErr: sfEv.Err}, nil
	}

	rawBody, exceeded, err := readBoundedBody(resp.Body, c.maxEntryBytes)
	if err != nil {
		_ = resp.Body.Close()
		return nil, err
	}
	if exceeded {
		c.evictKey(req.Context(), key)
		resp.Body = &cancelOnCloseReadCloser{
			ReadCloser: &prefixedReadCloser{
				Reader: io.MultiReader(bytes.NewReader(rawBody), resp.Body),
				Closer: resp.Body,
			},
			cancel: cancelFetch,
		}
		return &sfResult{tooLarge: true, streamResp: resp, auditErr: sfEv.Err}, nil
	}
	_ = resp.Body.Close()

	respCC := parseResponseCacheControl(resp.Header)
	if resp.StatusCode != http.StatusOK || respCC.noStore || respCC.zeroMaxAge {
		c.evictKey(req.Context(), key)
		cacheStatus := cacheStatusMiss
		metric := CacheResultMiss
		if respCC.noStore || respCC.zeroMaxAge {
			cacheStatus = cacheStatusBypass
			metric = CacheResultBypass
		}
		return &sfResult{
			statusCode:  resp.StatusCode,
			header:      sanitizeCachedHeaders(resp.Header),
			body:        rawBody,
			cacheStatus: cacheStatus,
			metric:      metric,
			auditErr:    sfEv.Err,
			auditID:     resp.Header.Get(headerAuditID),
		}, nil
	}

	freshTTL := c.defaultResourceTTL
	if isDiscovery {
		freshTTL = c.defaultDiscoveryTTL
	}
	storeTTL := freshTTL
	if respCC.hasMaxAge && respCC.maxAge < freshTTL {
		freshTTL = respCC.maxAge
	}

	cleanHeaders := sanitizeCachedHeaders(resp.Header)
	if storeTTL > 0 && freshTTL > 0 && c.epoch.Load() == epoch {
		now := c.now()
		encoded, err := encodeCachedResponse(cachedResponseMeta{
			StatusCode: resp.StatusCode,
			Header:     cleanHeaders,
			StoredAt:   now.UnixNano(),
			FreshTTL:   int64(freshTTL),
		}, rawBody)
		if err == nil {
			if setErr := c.store.Set(req.Context(), key, encoded, storeTTL); setErr == nil {
				c.setStale(key, false)
				if sp, ok := c.store.(CacheStatsProvider); ok {
					st := sp.Stats()
					c.metrics.setCacheOccupancy(st.Bytes, st.Entries)
				}
				if info != nil && info.IsResourceRequest {
					c.recordScopeKey(clusterID, info, key, now, storeTTL)
				}
				// A write that bumped the epoch after the check above may have collected the
				// scope before this key joined it.
				if c.epoch.Load() != epoch {
					c.evictKey(req.Context(), key)
				}
			}
		}
	}

	return &sfResult{
		statusCode:  resp.StatusCode,
		header:      cleanHeaders,
		body:        rawBody,
		cacheStatus: cacheStatusMiss,
		metric:      CacheResultMiss,
		auditErr:    sfEv.Err,
		auditID:     resp.Header.Get(headerAuditID),
	}, nil
}

// cacheClusterID names the host cluster one way whichever ID the caller addressed it by.
func (c *responseCache) cacheClusterID(clusterID string) string {
	if c.hostClusterID != "" && clusterID == c.hostClusterID {
		return InClusterID
	}
	return clusterID
}

// evictKey deletes key even when the caller has gone, and keeps it indexed if the store
// refused, so the next invalidation of its scope tries again.
func (c *responseCache) evictKey(ctx context.Context, key string) {
	if err := c.store.Delete(context.WithoutCancel(ctx), key); err != nil {
		c.setStale(key, true)
	} else {
		c.setStale(key, false)
		c.removeScopeKey(key)
	}
	if sp, ok := c.store.(CacheStatsProvider); ok {
		st := sp.Stats()
		c.metrics.setCacheOccupancy(st.Bytes, st.Entries)
	}
}

func prepareCacheableUpstreamRequest(req *http.Request) *http.Request {
	upReq := req.Clone(req.Context())
	if normalizeAcceptEncoding(upReq.Header) == encodingGzip {
		upReq.Header.Set(headerAcceptEncoding, encodingGzip)
	} else {
		upReq.Header.Del(headerAcceptEncoding)
	}
	return upReq
}

// isConditionalRequest reports whether the caller is revalidating its own copy.
// kubectl's disk cache sends If-None-Match on every discovery refetch, including
// the one after a resource type misses. Such a request bypasses the cache with
// its headers intact: the API server answers 304 or the current document, and a
// stored copy is never handed to it, nor its 304 to a coalesced caller.
func isConditionalRequest(h http.Header) bool {
	for _, name := range []string{headerIfNoneMatch, headerIfModifiedSince, headerIfMatch, headerIfUnmodifiedSince, headerIfRange} {
		if h.Get(name) != "" {
			return true
		}
	}
	return false
}

func isCacheableRequest(req *http.Request, info *request.RequestInfo, dryRun bool) (cacheable bool, isDiscovery bool) {
	if req == nil || info == nil || dryRun || req.Method != http.MethodGet || HasUpgradeHeader(req) {
		return false, false
	}
	if !info.IsResourceRequest {
		if nonCachedDiscoveryPaths.Has(info.Path) || !isDiscoveryPath(info.Path) {
			return false, false
		}
		return true, true
	}
	if info.Subresource != "" {
		return false, false
	}
	if info.Verb != verbGet && info.Verb != verbList {
		return false, false
	}
	if req.URL != nil && strings.Contains(strings.ToLower(req.URL.RawQuery), "watch=") {
		return false, false
	}
	return true, false
}

func shouldInvalidateOnMutation(statusCode int) bool {
	return (statusCode >= http.StatusOK && statusCode < http.StatusMultipleChoices) || isAuthFailureStatus(statusCode)
}

func isAuthFailureStatus(statusCode int) bool {
	return statusCode == http.StatusUnauthorized || statusCode == http.StatusForbidden
}

// resourceScopeID leaves out the API version: every served version reads the same stored object.
func resourceScopeID(clusterID, group, resource, namespace string) string {
	return clusterID + "|" + group + "|" + resource + "|" + namespace
}

func (c *responseCache) recordScopeKey(clusterID string, info *request.RequestInfo, key string, now time.Time, ttl time.Duration) {
	c.scopeMu.Lock()
	defer c.scopeMu.Unlock()

	c.pruneExpiredScopeKeysLocked(now)
	nsScope := resourceScopeID(clusterID, info.APIGroup, info.Resource, info.Namespace)
	if prevScope, ok := c.keyScope[key]; ok && prevScope != nsScope {
		if prevKeys, exists := c.scopeKeys[prevScope]; exists {
			delete(prevKeys, key)
			if len(prevKeys) == 0 {
				delete(c.scopeKeys, prevScope)
			}
		}
	}
	keys, ok := c.scopeKeys[nsScope]
	if !ok {
		keys = make(map[string]time.Time)
		c.scopeKeys[nsScope] = keys
	}
	keys[key] = now.Add(ttl)
	c.keyScope[key] = nsScope
}

func (c *responseCache) removeScopeKey(key string) {
	c.scopeMu.Lock()
	defer c.scopeMu.Unlock()

	scope, ok := c.keyScope[key]
	if !ok {
		return
	}
	delete(c.keyScope, key)
	if keys, exists := c.scopeKeys[scope]; exists {
		delete(keys, key)
		if len(keys) == 0 {
			delete(c.scopeKeys, scope)
		}
	}
}

func (c *responseCache) isStale(key string) bool {
	c.scopeMu.Lock()
	defer c.scopeMu.Unlock()
	_, ok := c.staleKeys[key]
	return ok
}

func (c *responseCache) setStale(key string, stale bool) {
	c.scopeMu.Lock()
	defer c.scopeMu.Unlock()
	if stale {
		c.staleKeys[key] = struct{}{}
	} else {
		delete(c.staleKeys, key)
	}
}

// indexedKey is a scope index entry taken out for invalidation.
type indexedKey struct {
	key       string
	scope     string
	expiresAt time.Time
}

// restoreScopeKey puts back an entry the store refused to delete, unless a newer store
// indexed the key meanwhile.
func (c *responseCache) restoreScopeKey(k indexedKey) {
	c.scopeMu.Lock()
	defer c.scopeMu.Unlock()

	if _, ok := c.keyScope[k.key]; ok {
		return
	}
	keys, ok := c.scopeKeys[k.scope]
	if !ok {
		keys = make(map[string]time.Time)
		c.scopeKeys[k.scope] = keys
	}
	keys[k.key] = k.expiresAt
	c.keyScope[k.key] = k.scope
}

func (c *responseCache) pruneExpiredScopeKeysLocked(now time.Time) {
	for scope, keys := range c.scopeKeys {
		for k, exp := range keys {
			if !now.Before(exp) {
				delete(keys, k)
				delete(c.keyScope, k)
			}
		}
		if len(keys) == 0 {
			delete(c.scopeKeys, scope)
		}
	}
}

func (c *responseCache) invalidateResourceScope(ctx context.Context, clusterID string, info *request.RequestInfo) {
	c.epoch.Add(1)
	// A caller that disconnects must not leave a deleted-from-index entry in the store.
	ctx = context.WithoutCancel(ctx)
	now := c.now()
	c.scopeMu.Lock()
	c.pruneExpiredScopeKeysLocked(now)
	var toDelete []indexedKey
	collect := func(scope string) {
		if keys, ok := c.scopeKeys[scope]; ok {
			for k, exp := range keys {
				toDelete = append(toDelete, indexedKey{key: k, scope: scope, expiresAt: exp})
				delete(c.keyScope, k)
			}
			delete(c.scopeKeys, scope)
		}
	}

	if info.Namespace == "" {
		prefix := resourceScopeID(clusterID, info.APIGroup, info.Resource, "")
		for scope := range c.scopeKeys {
			if strings.HasPrefix(scope, prefix) {
				collect(scope)
			}
		}
	} else {
		collect(resourceScopeID(clusterID, info.APIGroup, info.Resource, info.Namespace))
		collect(resourceScopeID(clusterID, info.APIGroup, info.Resource, ""))
	}
	c.scopeMu.Unlock()

	for _, k := range toDelete {
		_, ok, getErr := c.store.Get(ctx, k.key)
		err := c.store.Delete(ctx, k.key)
		switch {
		case err != nil:
			c.setStale(k.key, true)
			c.restoreScopeKey(k)
		default:
			c.setStale(k.key, false)
			if getErr == nil && ok {
				c.metrics.recordCacheEviction(CacheEvictionInvalidated)
			}
		}
	}
	if len(toDelete) > 0 {
		if sp, ok := c.store.(CacheStatsProvider); ok {
			st := sp.Stats()
			c.metrics.setCacheOccupancy(st.Bytes, st.Entries)
		}
	}
}

type requestCacheControl struct {
	noCache    bool
	noStore    bool
	zeroMaxAge bool
	hasMaxAge  bool
	maxAge     time.Duration
	hasStale   bool
	maxStale   time.Duration
}

func parseDeltaSeconds(val string) (time.Duration, bool) {
	if val == "" {
		return 0, false
	}
	for i := 0; i < len(val); i++ {
		if val[i] < '0' || val[i] > '9' {
			return 0, false
		}
	}
	secs, err := strconv.ParseInt(val, 10, 64)
	if err != nil {
		if errors.Is(err, strconv.ErrRange) {
			return time.Duration(maxDeltaSeconds) * time.Second, true
		}
		return 0, false
	}
	if secs < 0 {
		return 0, false
	}
	if secs > maxDeltaSeconds {
		secs = maxDeltaSeconds
	}
	return time.Duration(secs) * time.Second, true
}

func parseRequestCacheControl(h http.Header) requestCacheControl {
	var cc requestCacheControl
	for _, raw := range h.Values(headerCacheControl) {
		for _, part := range strings.Split(raw, ",") {
			token := strings.TrimSpace(part)
			if token == "" {
				continue
			}
			name, val, hasVal := strings.Cut(token, "=")
			name = strings.ToLower(strings.TrimSpace(name))
			val = strings.Trim(strings.TrimSpace(val), `"`)
			switch name {
			case "no-cache":
				cc.noCache = true
			case "no-store":
				cc.noStore = true
			case "max-age":
				if hasVal {
					if d, ok := parseDeltaSeconds(val); ok {
						if d == 0 {
							cc.zeroMaxAge = true
						} else {
							cc.hasMaxAge = true
							cc.maxAge = d
						}
					}
				}
			case "max-stale":
				if !hasVal {
					cc.hasStale = true
					cc.maxStale = defaultMaxStaleDuration
				} else if d, ok := parseDeltaSeconds(val); ok {
					cc.hasStale = true
					cc.maxStale = d
				}
			}
		}
	}
	return cc
}

type responseCacheControl struct {
	noStore    bool
	zeroMaxAge bool
	hasMaxAge  bool
	maxAge     time.Duration
}

func parseResponseCacheControl(h http.Header) responseCacheControl {
	var cc responseCacheControl
	for _, raw := range h.Values(headerCacheControl) {
		for _, part := range strings.Split(raw, ",") {
			token := strings.TrimSpace(part)
			if token == "" {
				continue
			}
			name, val, hasVal := strings.Cut(token, "=")
			name = strings.ToLower(strings.TrimSpace(name))
			val = strings.Trim(strings.TrimSpace(val), `"`)
			switch name {
			case "no-store":
				cc.noStore = true
			case "max-age":
				if hasVal {
					if d, ok := parseDeltaSeconds(val); ok {
						if d == 0 {
							cc.zeroMaxAge = true
						} else {
							cc.hasMaxAge = true
							cc.maxAge = d
						}
					}
				}
			}
		}
	}
	return cc
}

type cachedResponseMeta struct {
	StatusCode int         `json:"statusCode"`
	Header     http.Header `json:"header"`
	StoredAt   int64       `json:"storedAt"`
	FreshTTL   int64       `json:"freshTTL"`
}

type cachedResponse struct {
	meta cachedResponseMeta
	body []byte
}

func encodeCachedResponse(meta cachedResponseMeta, body []byte) ([]byte, error) {
	metaBytes, err := json.Marshal(meta)
	if err != nil {
		return nil, err
	}
	if len(metaBytes) > maxCacheMetaBytes || len(body) > maxCacheBodyBytes {
		return nil, errors.New("cache entry exceeds envelope size limit")
	}
	out := make([]byte, cacheEnvelopeMinSize+len(metaBytes)+len(body))
	copy(out[:cacheEnvelopeMagicSize], cacheEnvelopeMagic)
	// #nosec G115 -- len(metaBytes) is non-negative and bounded by maxCacheMetaBytes (1 MiB) above.
	binary.BigEndian.PutUint32(out[cacheEnvelopeMagicSize:cacheEnvelopeMinSize], uint32(len(metaBytes)))
	copy(out[cacheEnvelopeMinSize:cacheEnvelopeMinSize+len(metaBytes)], metaBytes)
	copy(out[cacheEnvelopeMinSize+len(metaBytes):], body)
	return out, nil
}

func decodeCachedResponse(raw []byte) (*cachedResponse, error) {
	if len(raw) < cacheEnvelopeMinSize || string(raw[:cacheEnvelopeMagicSize]) != cacheEnvelopeMagic {
		return nil, errors.New("invalid cache envelope header")
	}
	metaLen := int(binary.BigEndian.Uint32(raw[cacheEnvelopeMagicSize:cacheEnvelopeMinSize]))
	if metaLen < 0 || cacheEnvelopeMinSize+metaLen > len(raw) {
		return nil, errors.New("invalid cache envelope metadata length")
	}
	var meta cachedResponseMeta
	if err := json.Unmarshal(raw[cacheEnvelopeMinSize:cacheEnvelopeMinSize+metaLen], &meta); err != nil {
		return nil, err
	}
	return &cachedResponse{
		meta: meta,
		body: raw[cacheEnvelopeMinSize+metaLen:],
	}, nil
}

func isEntryFreshForRequest(entry *cachedResponse, reqCC requestCacheControl, now time.Time) bool {
	storedAt := time.Unix(0, entry.meta.StoredAt)
	age := now.Sub(storedAt)
	if age < 0 {
		age = 0
	}
	allowed := time.Duration(entry.meta.FreshTTL)
	if reqCC.hasMaxAge && reqCC.maxAge < allowed {
		allowed = reqCC.maxAge
	}
	if reqCC.hasStale {
		maxAllowed := time.Duration(maxDeltaSeconds) * time.Second
		if maxAllowed-allowed < reqCC.maxStale {
			allowed = maxAllowed
		} else {
			allowed += reqCC.maxStale
		}
	}
	return age <= allowed
}

func buildResponseFromCache(req *http.Request, entry *cachedResponse, now time.Time) *http.Response {
	storedAt := time.Unix(0, entry.meta.StoredAt)
	ageSec := int(now.Sub(storedAt).Seconds())
	if ageSec < 0 {
		ageSec = 0
	}
	hdr := cloneHeader(entry.meta.Header)
	hdr.Set(headerCacheStatus, cacheStatusHit)
	hdr.Set(headerAge, strconv.Itoa(ageSec))
	hdr.Set(headerContentLength, strconv.Itoa(len(entry.body)))
	return &http.Response{
		StatusCode:    entry.meta.StatusCode,
		Status:        strconv.Itoa(entry.meta.StatusCode) + " " + http.StatusText(entry.meta.StatusCode),
		Proto:         "HTTP/1.1",
		ProtoMajor:    1,
		ProtoMinor:    1,
		Header:        hdr,
		Body:          io.NopCloser(bytes.NewReader(entry.body)),
		ContentLength: int64(len(entry.body)),
		Request:       req,
	}
}

func buildCacheKey(clusterID string, req *http.Request, callerAccept string) string {
	query := canonicalQuery(req.URL.RawQuery)
	accept := callerAccept
	if accept == "" {
		accept = normalizeAcceptHeader(req.Header)
	}
	encoding := normalizeAcceptEncoding(req.Header)
	raw := clusterID + "\x00" + req.Method + "\x00" + req.URL.Path + "\x00" + query + "\x00" + accept + "\x00" + encoding
	sum := sha256.Sum256([]byte(raw))
	return "k8s:" + clusterID + ":" + hex.EncodeToString(sum[:cacheKeyHashBytes])
}

func canonicalQuery(rawQuery string) string {
	if rawQuery == "" {
		return ""
	}
	vals, err := url.ParseQuery(rawQuery)
	if err != nil {
		return rawQuery
	}
	return vals.Encode()
}

func normalizeAcceptHeader(h http.Header) string {
	raw := strings.TrimSpace(strings.Join(h.Values(headerAccept), ","))
	if raw == "" {
		return contentTypeJSON
	}
	return raw
}

func normalizeAcceptEncoding(h http.Header) string {
	var seenGzip, gzipAccepted bool
	for _, v := range h.Values(headerAcceptEncoding) {
		for _, part := range strings.Split(v, ",") {
			token, params, hasParams := strings.Cut(strings.TrimSpace(part), ";")
			if !strings.EqualFold(strings.TrimSpace(token), encodingGzip) {
				continue
			}
			seenGzip = true
			gzipAccepted = !hasParams || !hasZeroQuality(params)
		}
	}
	if seenGzip && gzipAccepted {
		return encodingGzip
	}
	return encodingIdentity
}

func hasZeroQuality(params string) bool {
	for _, rawParam := range strings.Split(params, ";") {
		k, v, ok := strings.Cut(strings.TrimSpace(rawParam), "=")
		if !ok || !strings.EqualFold(strings.TrimSpace(k), "q") {
			continue
		}
		q, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
		if err != nil || q <= 0 || q > 1 {
			return true
		}
	}
	return false
}

func sanitizeCachedHeaders(h http.Header) http.Header {
	out := make(http.Header, len(h))
	for k, vals := range h {
		ck := http.CanonicalHeaderKey(k)
		if hopByHopResponseHeaders.Has(ck) {
			continue
		}
		copied := make([]string, len(vals))
		copy(copied, vals)
		out[ck] = copied
	}
	return out
}

func cloneHeader(h http.Header) http.Header {
	out := make(http.Header, len(h))
	for k, vals := range h {
		copied := make([]string, len(vals))
		copy(copied, vals)
		out[k] = copied
	}
	return out
}

func readBoundedBody(r io.Reader, maxBytes int64) (data []byte, exceeded bool, err error) {
	if r == nil {
		return nil, false, nil
	}
	buf, err := io.ReadAll(io.LimitReader(r, maxBytes+1))
	if err != nil {
		return nil, false, err
	}
	if int64(len(buf)) > maxBytes {
		return buf, true, nil
	}
	return buf, false, nil
}

type prefixedReadCloser struct {
	io.Reader
	io.Closer
}

type cancelOnCloseReadCloser struct {
	io.ReadCloser
	once   sync.Once
	cancel context.CancelFunc
}

func (r *cancelOnCloseReadCloser) Close() error {
	err := r.ReadCloser.Close()
	r.once.Do(func() {
		if r.cancel != nil {
			r.cancel()
		}
	})
	return err
}
