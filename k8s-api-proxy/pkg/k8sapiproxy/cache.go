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

	"golang.org/x/sync/singleflight"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/apiserver/pkg/endpoints/request"
)

const (
	// DefaultCacheMaxBytes is the default capacity of the built-in memory cache (64 MiB).
	DefaultCacheMaxBytes int64 = 64 << 20
	// DefaultCacheMaxEntryBytes is the default maximum size of a single cached response body (2 MiB).
	DefaultCacheMaxEntryBytes int64 = 2 << 20
	// DefaultResourceCacheTTL is the default TTL for cached resource get and list responses.
	DefaultResourceCacheTTL = 30 * time.Second
	// DefaultDiscoveryCacheTTL is the default TTL for cached discovery responses.
	DefaultDiscoveryCacheTTL = 15 * time.Minute

	headerCacheControl = "Cache-Control"
	headerCacheStatus  = "Cache-Status"
	headerAge          = "Age"

	cacheStatusHit       = "k8s-api-proxy; hit"
	cacheStatusCoalesced = "k8s-api-proxy; hit; coalesced"
	cacheStatusMiss      = "k8s-api-proxy; miss"
	cacheStatusBypass    = "k8s-api-proxy; bypass"

	cacheEnvelopeMagic   = "KPC1"
	cacheEnvelopeMinSize = 8
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
	// DefaultCacheMaxEntryBytes when <= 0. Responses larger than MaxEntryBytes stream
	// directly to the caller without full buffering.
	MaxEntryBytes int64
	// DefaultResourceTTL is the default TTL for resource get and list responses;
	// DefaultResourceCacheTTL when <= 0.
	DefaultResourceTTL time.Duration
	// DefaultDiscoveryTTL is the default TTL for discovery responses;
	// DefaultDiscoveryCacheTTL when <= 0.
	DefaultDiscoveryTTL time.Duration
	// Now returns the current time for TTL and Age calculations; time.Now when nil.
	Now func() time.Time
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
	onEvict         func(reason string)
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
			s.onEvict(CacheEvictionExpired)
		}
		if s.onOccupancy != nil {
			s.onOccupancy(s.usedBytes, len(s.items))
		}
		return nil, false, nil
	}
	if ent.hits < 15 {
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
				s.onEvict(CacheEvictionExpired)
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
				s.onEvict(CacheEvictionCapacity)
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
		s.removeEntryLocked(back.Value.(*memoryCacheEntry))
		s.capacityEvicted++
		if s.onEvict != nil {
			s.onEvict(CacheEvictionCapacity)
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

type responseCache struct {
	store               CacheStore
	maxEntryBytes       int64
	defaultResourceTTL  time.Duration
	defaultDiscoveryTTL time.Duration
	now                 func() time.Time
	metrics             *Metrics
	group               singleflight.Group

	scopeMu   sync.Mutex
	scopeKeys map[string]map[string]struct{}
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
	if mem, ok := store.(*MemoryCacheStore); ok {
		mem.mu.Lock()
		if mem.onEvict == nil {
			mem.onEvict = func(reason string) {
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
	maxEntry := cfg.MaxEntryBytes
	if maxEntry <= 0 {
		maxEntry = DefaultCacheMaxEntryBytes
	}
	resTTL := cfg.DefaultResourceTTL
	if resTTL <= 0 {
		resTTL = DefaultResourceCacheTTL
	}
	discTTL := cfg.DefaultDiscoveryTTL
	if discTTL <= 0 {
		discTTL = DefaultDiscoveryCacheTTL
	}
	return &responseCache{
		store:               store,
		maxEntryBytes:       maxEntry,
		defaultResourceTTL:  resTTL,
		defaultDiscoveryTTL: discTTL,
		now:                 nowFn,
		metrics:             metrics,
		scopeKeys:           make(map[string]map[string]struct{}),
	}
}

type sfResult struct {
	statusCode    int
	header        http.Header
	body          []byte
	cacheStatus   string
	metric        string
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

	if info != nil && info.IsResourceRequest && !ev.DryRun && mutatingVerbs.Has(info.Verb) {
		resp, err := next(req)
		if err == nil && resp != nil && shouldInvalidateOnMutation(resp.StatusCode) {
			c.invalidateResourceScope(req.Context(), ev.ClusterID, info)
		}
		return resp, err
	}

	cacheable, isDiscovery := isCacheableRequest(req, info, ev.DryRun)
	if !cacheable {
		resp, err := next(req)
		if err == nil && resp != nil && info != nil && info.IsResourceRequest && isAuthFailureStatus(resp.StatusCode) {
			c.invalidateResourceScope(req.Context(), ev.ClusterID, info)
		}
		return resp, err
	}

	verbLabel, resLabel, _ := ClassifyRequestLabels(req, info)
	reqCC := parseRequestCacheControl(req.Header)
	if reqCC.noStore {
		c.metrics.recordCacheRequest(CacheResultBypass, verbLabel, resLabel)
		resp, err := next(req)
		if err == nil && resp != nil {
			if info != nil && info.IsResourceRequest && isAuthFailureStatus(resp.StatusCode) {
				c.invalidateResourceScope(req.Context(), ev.ClusterID, info)
			}
			resp.Header.Set(headerCacheStatus, cacheStatusBypass)
		}
		return resp, err
	}

	key := buildCacheKey(ev.ClusterID, req, meta.callerAccept)
	now := c.now()

	if !reqCC.noCache && !reqCC.zeroMaxAge {
		if raw, ok, err := c.store.Get(req.Context(), key); err == nil && ok {
			if entry, err := decodeCachedResponse(raw); err == nil && isEntryFreshForRequest(entry, reqCC, now) {
				c.metrics.recordCacheRequest(CacheResultHit, verbLabel, resLabel)
				return buildResponseFromCache(req, entry, now), nil
			}
		}
	}

	ch := c.group.DoChan(key, func() (any, error) {
		return c.fetchAndPopulate(req.WithContext(context.WithoutCancel(req.Context())), key, ev.ClusterID, info, isDiscovery, next)
	})

	var res singleflight.Result
	select {
	case <-req.Context().Done():
		return nil, req.Context().Err()
	case res = <-ch:
	}
	if res.Err != nil {
		return nil, res.Err
	}

	out := res.Val.(*sfResult)
	if out.tooLarge {
		c.metrics.recordCacheRequest(CacheResultBypass, verbLabel, resLabel)
		if out.claimStream() {
			out.streamResp.Header.Set(headerCacheStatus, cacheStatusBypass)
			return out.streamResp, nil
		}
		resp, err := next(req)
		if err == nil && resp != nil {
			resp.Header.Set(headerCacheStatus, cacheStatusBypass)
		}
		return resp, err
	}

	statusHeader := out.cacheStatus
	metricResult := out.metric
	if res.Shared && out.cacheStatus == cacheStatusMiss && !out.claimLeader() {
		statusHeader = cacheStatusCoalesced
		metricResult = CacheResultCoalesced
	}
	c.metrics.recordCacheRequest(metricResult, verbLabel, resLabel)

	hdr := cloneHeader(out.header)
	if statusHeader != "" {
		hdr.Set(headerCacheStatus, statusHeader)
	}
	if statusHeader == cacheStatusCoalesced {
		hdr.Set(headerAge, "0")
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

func (c *responseCache) fetchAndPopulate(
	req *http.Request,
	key string,
	clusterID string,
	info *request.RequestInfo,
	isDiscovery bool,
	next func(*http.Request) (*http.Response, error),
) (*sfResult, error) {
	resp, err := next(req)
	if err != nil {
		return nil, err
	}
	if info != nil && info.IsResourceRequest && isAuthFailureStatus(resp.StatusCode) {
		c.invalidateResourceScope(req.Context(), clusterID, info)
	}

	if resp.ContentLength > c.maxEntryBytes {
		return &sfResult{tooLarge: true, streamResp: resp}, nil
	}

	rawBody, exceeded, err := readBoundedBody(resp.Body, c.maxEntryBytes)
	if err != nil {
		_ = resp.Body.Close()
		return nil, err
	}
	if exceeded {
		resp.Body = &prefixedReadCloser{
			Reader: io.MultiReader(bytes.NewReader(rawBody), resp.Body),
			Closer: resp.Body,
		}
		return &sfResult{tooLarge: true, streamResp: resp}, nil
	}
	_ = resp.Body.Close()

	respCC := parseResponseCacheControl(resp.Header)
	if resp.StatusCode != http.StatusOK || respCC.noStore || respCC.zeroMaxAge {
		_ = c.store.Delete(req.Context(), key)
		if sp, ok := c.store.(CacheStatsProvider); ok {
			st := sp.Stats()
			c.metrics.setCacheOccupancy(st.Bytes, st.Entries)
		}
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
	now := c.now()
	encoded, err := encodeCachedResponse(cachedResponseMeta{
		StatusCode: resp.StatusCode,
		Header:     cleanHeaders,
		StoredAt:   now.UnixNano(),
		FreshTTL:   int64(freshTTL),
	}, rawBody)
	if err == nil {
		if setErr := c.store.Set(req.Context(), key, encoded, storeTTL); setErr == nil {
			if sp, ok := c.store.(CacheStatsProvider); ok {
				st := sp.Stats()
				c.metrics.setCacheOccupancy(st.Bytes, st.Entries)
			}
			if info != nil && info.IsResourceRequest {
				c.recordScopeKey(clusterID, info, key)
			}
		}
	}

	return &sfResult{
		statusCode:  resp.StatusCode,
		header:      cleanHeaders,
		body:        rawBody,
		cacheStatus: cacheStatusMiss,
		metric:      CacheResultMiss,
	}, nil
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

func resourceScopeID(clusterID, group, version, resource, namespace string) string {
	return clusterID + "|" + group + "|" + version + "|" + resource + "|" + namespace
}

func (c *responseCache) recordScopeKey(clusterID string, info *request.RequestInfo, key string) {
	c.scopeMu.Lock()
	defer c.scopeMu.Unlock()

	nsScope := resourceScopeID(clusterID, info.APIGroup, info.APIVersion, info.Resource, info.Namespace)
	c.addScopeKeyLocked(nsScope, key)
}

func (c *responseCache) addScopeKeyLocked(scope, key string) {
	keys, ok := c.scopeKeys[scope]
	if !ok {
		keys = make(map[string]struct{})
		c.scopeKeys[scope] = keys
	}
	keys[key] = struct{}{}
}

func (c *responseCache) invalidateResourceScope(ctx context.Context, clusterID string, info *request.RequestInfo) {
	c.scopeMu.Lock()
	var toDelete []string
	collect := func(scope string) {
		if keys, ok := c.scopeKeys[scope]; ok {
			for k := range keys {
				toDelete = append(toDelete, k)
			}
			delete(c.scopeKeys, scope)
		}
	}

	if info.Namespace == "" {
		prefix := clusterID + "|" + info.APIGroup + "|" + info.APIVersion + "|" + info.Resource + "|"
		for scope := range c.scopeKeys {
			if strings.HasPrefix(scope, prefix) {
				collect(scope)
			}
		}
	} else {
		collect(resourceScopeID(clusterID, info.APIGroup, info.APIVersion, info.Resource, info.Namespace))
		collect(resourceScopeID(clusterID, info.APIGroup, info.APIVersion, info.Resource, ""))
	}
	c.scopeMu.Unlock()

	for _, k := range toDelete {
		_, ok, getErr := c.store.Get(ctx, k)
		if err := c.store.Delete(ctx, k); err == nil && getErr == nil && ok {
			c.metrics.recordCacheEviction(CacheEvictionInvalidated)
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
					if secs, err := strconv.Atoi(val); err == nil && secs >= 0 {
						if secs == 0 {
							cc.zeroMaxAge = true
						} else {
							cc.hasMaxAge = true
							cc.maxAge = time.Duration(secs) * time.Second
						}
					}
				}
			case "max-stale":
				cc.hasStale = true
				if hasVal {
					if secs, err := strconv.Atoi(val); err == nil && secs >= 0 {
						cc.maxStale = time.Duration(secs) * time.Second
					}
				} else {
					cc.maxStale = 24 * time.Hour
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
					if secs, err := strconv.Atoi(val); err == nil && secs >= 0 {
						if secs == 0 {
							cc.zeroMaxAge = true
						} else {
							cc.hasMaxAge = true
							cc.maxAge = time.Duration(secs) * time.Second
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
	out := make([]byte, cacheEnvelopeMinSize+len(metaBytes)+len(body))
	copy(out[:4], cacheEnvelopeMagic)
	binary.BigEndian.PutUint32(out[4:8], uint32(len(metaBytes)))
	copy(out[8:8+len(metaBytes)], metaBytes)
	copy(out[8+len(metaBytes):], body)
	return out, nil
}

func decodeCachedResponse(raw []byte) (*cachedResponse, error) {
	if len(raw) < cacheEnvelopeMinSize || string(raw[:4]) != cacheEnvelopeMagic {
		return nil, errors.New("invalid cache envelope header")
	}
	metaLen := int(binary.BigEndian.Uint32(raw[4:8]))
	if metaLen < 0 || cacheEnvelopeMinSize+metaLen > len(raw) {
		return nil, errors.New("invalid cache envelope metadata length")
	}
	var meta cachedResponseMeta
	if err := json.Unmarshal(raw[8:8+metaLen], &meta); err != nil {
		return nil, err
	}
	return &cachedResponse{
		meta: meta,
		body: raw[8+metaLen:],
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
		allowed += reqCC.maxStale
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
	return "k8s:" + clusterID + ":" + hex.EncodeToString(sum[:16])
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
	for _, v := range h.Values(headerAcceptEncoding) {
		for _, part := range strings.Split(v, ",") {
			token, _, _ := strings.Cut(strings.TrimSpace(part), ";")
			if strings.EqualFold(strings.TrimSpace(token), encodingGzip) {
				return encodingGzip
			}
		}
	}
	return encodingIdentity
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
