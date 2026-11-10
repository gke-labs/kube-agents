package authcallout

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/cache"
)

const (
	// mapPollInterval is how often WaitForMap re-checks whether the informer
	// has delivered a map. It bounds startup latency rather than steady-state
	// cost: the loop runs only until the first map lands, and every tick that
	// finds nothing is one the process would otherwise spend not ready.
	mapPollInterval = 50 * time.Millisecond

	// nameFieldSelector is the field the ConfigMap watch is narrowed on. It
	// is the API server's own field name rather than a Go identifier, so
	// nothing here checks the spelling — see WatchConfigMap for why the watch
	// is narrowed at all.
	nameFieldSelector = "metadata.name"

	// shardNameSeparator joins the shard name stem and the index, as the
	// operator joins them: <agent>-a2a-authmap-<i>.
	shardNameSeparator = "-"

	// unshardedCount is the shard count a store that was told nothing
	// expects: one, the whole map in one document.
	unshardedCount = 1

	// detailSeparator joins the refusals that stand into LastError, and the
	// refusal and the pending note into the reason a store serves nothing.
	detailSeparator = "; "
)

// Store holds the identity map the callout is currently serving, kept current
// by an API informer.
//
// An informer rather than a mounted ConfigMap, and the deployment spec is
// explicit about why: kubelet syncs a ConfigMap volume on a period that can
// reach a minute, and a workload can be spawned seconds after its entry is
// rendered. That gap ends in an Authorization Violation for a legitimate client
// holding a perfectly good token — the failure that looks like the security
// layer working correctly and is in fact a race. A watch sees the write when
// the API server commits it.
//
// The map is served from an atomic pointer because it is read on the
// authorization path — once per connection attempt, under whatever burst a NATS
// restart produces — and written only when the ConfigMap changes. A lock here
// would put every reconnecting client behind one mutex during exactly the storm
// NR-6's jittered backoff exists to spread out.
type Store struct {
	current atomic.Pointer[IdentityMap]

	// lastErr holds the refusals that stand: why the latest document under
	// each refused index was not taken, and why the complete set was not
	// assembled, if it was not. A refused update leaves the previous map
	// serving, which is the right behaviour — the alternative is to drop
	// every identity because one entry was malformed — but it must not be
	// silent, or the callout reports healthy while serving a version nobody
	// rendered. It is recomputed from refused and setErr by settle.
	lastErr atomic.Pointer[string]

	// The shards. The operator cuts the map into `shards` ConfigMaps, and
	// a document that arrives is one of them; pending holds the latest
	// document taken for each index, and the map is re-assembled and
	// installed whenever every index holds a document of one version. Until
	// then the previous map keeps serving and pendingNote says what is
	// missing, so a re-render half-landed, or stuck half-landed, is visible
	// in the status rather than indistinguishable from nothing rendered.
	// refused holds, per index, why the latest document under it was not
	// taken, until one is; setErr why the complete set in pending was not
	// assembled, until the set changes. Kept apart from pending so that a
	// sibling shard redelivered unchanged, which re-assembles the set
	// pending still holds, cannot clear what a broken shard left on record.
	// mu covers pending, refused and setErr: it is taken on the informer's
	// path, never on the authorization path, which reads current.
	shards      int
	mu          sync.Mutex
	pending     []*IdentityMap
	refused     map[int]string
	setErr      string
	pendingNote atomic.Pointer[string]

	log *slog.Logger
}

// NewStore returns an empty store expecting the whole map in one document. It
// serves nothing until an update lands, and Ready reports false until then, so
// a callout that cannot read its map refuses connections rather than inventing
// grants for them.
func NewStore(log *slog.Logger) *Store {
	s, _ := NewShardedStore(log, unshardedCount)
	return s
}

// NewShardedStore returns an empty store expecting the map in shards
// documents, which the operator renders as that many ConfigMaps and tells the
// callout the count of. A count under one is refused: a store expecting no
// shards could never serve anything.
func NewShardedStore(log *slog.Logger, shards int) (*Store, error) {
	if log == nil {
		log = slog.Default()
	}
	if shards < 1 {
		return nil, fmt.Errorf("the identity map is cut into %d shards; it takes at least one", shards)
	}
	return &Store{log: log, shards: shards, pending: make([]*IdentityMap, shards), refused: map[int]string{}}, nil
}

// Shards is how many documents the store expects the map in.
func (s *Store) Shards() int {
	return s.shards
}

// Pending says why the shards the store holds are not yet a map it serves, or
// is empty when the last complete set was installed.
func (s *Store) Pending() string {
	if n := s.pendingNote.Load(); n != nil {
		return *n
	}
	return ""
}

// Current returns the map being served, or nil if none is.
func (s *Store) Current() *IdentityMap {
	return s.current.Load()
}

// Version returns the version being served, empty if none is.
func (s *Store) Version() string {
	if m := s.current.Load(); m != nil {
		return m.Version
	}
	return ""
}

// Ready reports whether the store is serving a map. It is the callout's
// readiness signal, and it is what makes "every replica is ready" mean "every
// replica is serving the rendered map" — which is what the operator reads
// BusCredentialsReady off.
func (s *Store) Ready() bool {
	return s.current.Load() != nil
}

// LastError returns the refusals that stand, if any: per refused shard, why
// its latest document was not taken, then why the complete set was not
// assembled. Empty once every refused index has had a document taken and the
// set in hand assembled, or was never refused.
func (s *Store) LastError() string {
	if e := s.lastErr.Load(); e != nil {
		return *e
	}
	return ""
}

// Update parses and installs a rendered map delivered as one document: shard
// 0, the whole map on a store expecting one shard.
//
// A map that does not parse is refused and the previous one keeps serving. That
// is deliberate: the callout is on the connection path, so the blast radius of
// accepting a bad map is every future connection, while the blast radius of
// keeping a good one is that a rendered change has not taken effect yet — and
// the version the callout reports says so, which is what stops the operator
// marking BusCredentialsReady on a version that never landed.
func (s *Store) Update(raw []byte) error {
	return s.UpdateShard(0, raw)
}

// UpdateShard parses one shard of a rendered map, the document that arrived
// under the shard name with this index, and installs the map once every shard
// of one version is in hand.
//
// A document is refused, with the previous map kept serving, when it does not
// parse, when its stamps name a count other than the one this store was told
// (A2A_AUTHMAP_SHARDS) or an index other than the name it arrived under, or
// when the assembled set fails the checks each shard passed alone. A document
// with no stamps at all is a map from before sharding, or one written by hand;
// it is a whole map, and is taken only by a store expecting one shard. A set
// that is merely incomplete — a shard not yet seen, or two versions among them
// while a re-render lands — is not refused: the previous map keeps serving and
// Pending says what is missing.
//
// A refusal stays on record (LastError) until it is superseded: a document
// refused under an index, until a document under that index is taken; a
// complete set refused at assembly, until the set changes. A document taken
// under one index therefore clears nothing another index left, and a sibling
// shard redelivered unchanged — a relist, a metadata-only update — re-assembles
// the set but does not clear the refusal of the shard whose live document is
// still broken. A complete set, taken or refused, leaves nothing pending.
func (s *Store) UpdateShard(index int, raw []byte) error {
	m, err := ParseIdentityMap(raw)
	if err == nil {
		err = s.checkStamps(index, m)
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	if err != nil {
		s.refused[index] = fmt.Sprintf("shard %d: %v", index, err)
		s.settle()
		s.logRefusal(err)
		return err
	}
	if m.Shards == 0 {
		// Unstamped: the whole map, which checkStamps admitted only on a
		// one-shard store. Stamp it so assembly sees one shape.
		m.Shard, m.Shards = 0, unshardedCount
	}
	delete(s.refused, index)
	s.setErr = ""
	s.pending[index] = m
	if note := s.incomplete(); note != "" {
		n := note
		s.pendingNote.Store(&n)
		s.settle()
		s.log.Info("identity map shard arrived; waiting for the rest", "shard", index, "of", s.shards, "version", m.Version, "pending", note)
		return nil
	}
	s.pendingNote.Store(nil)
	assembled, err := AssembleIdentityMap(s.pending)
	if err != nil {
		s.setErr = err.Error()
		s.settle()
		s.logRefusal(err)
		return err
	}
	s.install(assembled)
	s.settle()
	return nil
}

// checkStamps holds a parsed document against the index it arrived under and
// the count the store expects.
func (s *Store) checkStamps(index int, m *IdentityMap) error {
	if index < 0 || index >= s.shards {
		return fmt.Errorf("arrived under shard index %d; this callout watches %d shard(s)", index, s.shards)
	}
	if m.Shards == 0 {
		if s.shards != unshardedCount {
			return fmt.Errorf("carries no shard stamps; this callout expects %d shards", s.shards)
		}
		return nil
	}
	if m.Shards != s.shards {
		return fmt.Errorf("is stamped shard %d of %d; this callout expects %d shards", m.Shard, m.Shards, s.shards)
	}
	if m.Shard != index {
		return fmt.Errorf("is stamped shard %d but arrived under the name of shard %d", m.Shard, index)
	}
	return nil
}

// incomplete says what keeps the pending shards from being a map: the indices
// not yet seen, or the versions among them when there is more than one. Empty
// when every shard holds a document of one version. Called under mu.
func (s *Store) incomplete() string {
	var missing []string
	versions := map[string]bool{}
	for i, shard := range s.pending {
		if shard == nil {
			missing = append(missing, strconv.Itoa(i))
			continue
		}
		versions[shard.Version] = true
	}
	if len(missing) > 0 {
		return fmt.Sprintf("shard(s) %s of %d not yet seen", strings.Join(missing, ","), s.shards)
	}
	if len(versions) > 1 {
		list := make([]string, 0, len(versions))
		for v := range versions {
			list = append(list, v)
		}
		sort.Strings(list)
		return fmt.Sprintf("shards carry %d versions (%s); waiting for one", len(list), strings.Join(list, ", "))
	}
	return ""
}

// settle recomputes LastError from what stands: the refused indices in
// order, then the set refused at assembly. Called under mu after every change
// to refused or setErr, so LastError is never a refusal a later document
// resolved and never empty while one stands.
func (s *Store) settle() {
	indices := make([]int, 0, len(s.refused))
	for i := range s.refused {
		indices = append(indices, i)
	}
	sort.Ints(indices)
	parts := make([]string, 0, len(indices)+1)
	for _, i := range indices {
		parts = append(parts, s.refused[i])
	}
	if s.setErr != "" {
		parts = append(parts, s.setErr)
	}
	if len(parts) == 0 {
		s.lastErr.Store(nil)
		return
	}
	msg := strings.Join(parts, detailSeparator)
	s.lastErr.Store(&msg)
}

// logRefusal says that an update was refused and the previous map keeps
// serving; the caller has put the refusal on record.
func (s *Store) logRefusal(err error) {
	serving := s.Version()
	if serving == "" {
		s.log.Error("identity map refused and none is being served; every connection will be refused", "error", err)
	} else {
		s.log.Error("identity map refused; continuing to serve the previous version", "error", err, "serving", serving)
	}
}

// Unserved says why no map is being served: the refusals that stand, the
// shards still pending, both, or nothing when no document has arrived at all.
// Empty while a map is being served. It is what WaitForMap and the readiness
// probe report, so a callout that starts and serves nothing names the shard
// whose watch delivered nothing, or the render it refused, rather than only
// the time it waited.
func (s *Store) Unserved() string {
	if s.Ready() {
		return ""
	}
	var parts []string
	if e := s.LastError(); e != "" {
		parts = append(parts, "the last one was refused: "+e)
	}
	if p := s.Pending(); p != "" {
		parts = append(parts, "pending: "+p)
	}
	return strings.Join(parts, detailSeparator)
}

// install swaps in an assembled map.
func (s *Store) install(m *IdentityMap) {
	previous := s.Version()
	s.current.Store(m)

	// Logged at every change, because "the map says X" has to be checkable
	// against the running system rather than against the rendered object.
	if previous == "" {
		s.log.Info("serving identity map", "version", m.Version, "shards", s.shards, "identities", len(m.Identities), "users", m.Users())
	} else if previous != m.Version {
		s.log.Info("identity map changed", "from", previous, "to", m.Version, "shards", s.shards, "identities", len(m.Identities), "users", m.Users())
	}
}

// ShardName is the ConfigMap holding shard i of the map whose names are built
// on stem: what the operator names them, spelled once on each side.
func ShardName(stem string, i int) string {
	return stem + shardNameSeparator + strconv.Itoa(i)
}

// WatchConfigMapShards keeps the store current from every shard of the map,
// one by-name watch per shard on ShardName(stem, i) for each index the store
// expects, and blocks until the context is cancelled. The Role the operator
// renders names each of those objects and no other, which is why the watches
// are by name rather than by a label the shards share: a label selector would
// need list and watch over every ConfigMap in the namespace.
func (s *Store) WatchConfigMapShards(ctx context.Context, client kubernetes.Interface, namespace, stem, key string) error {
	var wg sync.WaitGroup
	for i := 0; i < s.shards; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			s.watchShard(ctx, client, namespace, ShardName(stem, i), key, i)
		}(i)
	}
	wg.Wait()
	return ctx.Err()
}

// WatchConfigMap keeps the store current from one ConfigMap, the whole map in
// one document, and blocks until the context is cancelled.
//
// The watch is narrowed to a single object by name. A namespace-wide ConfigMap
// informer would put every ConfigMap in the namespace into this process's cache
// for the sake of one — the same class of mistake as the cluster-wide Secret
// and Job informers a dark A2A install used to start (W6 finding #3).
func (s *Store) WatchConfigMap(ctx context.Context, client kubernetes.Interface, namespace, name, key string) error {
	s.watchShard(ctx, client, namespace, name, key, 0)
	return ctx.Err()
}

// watchShard runs one by-name informer on name and feeds what it delivers to
// UpdateShard as shard index.
func (s *Store) watchShard(ctx context.Context, client kubernetes.Interface, namespace, name, key string, index int) {
	byName := fields.OneTermEqualSelector(nameFieldSelector, name).String()

	lw := &cache.ListWatch{
		ListFunc: func(opts metav1.ListOptions) (runtime.Object, error) {
			opts.FieldSelector = byName
			return client.CoreV1().ConfigMaps(namespace).List(ctx, opts)
		},
		WatchFunc: func(opts metav1.ListOptions) (watch.Interface, error) {
			opts.FieldSelector = byName
			return client.CoreV1().ConfigMaps(namespace).Watch(ctx, opts)
		},
	}

	apply := func(obj any) {
		cm, ok := obj.(*corev1.ConfigMap)
		if !ok {
			s.log.Error("watch delivered something that is not a ConfigMap", "type", fmt.Sprintf("%T", obj))
			return
		}
		raw, ok := cm.Data[key]
		if !ok {
			s.log.Error("identity map ConfigMap has no such key", "configmap", cm.Name, "key", key)
			return
		}
		_ = s.UpdateShard(index, []byte(raw))
	}

	_, informer := cache.NewInformerWithOptions(cache.InformerOptions{
		ListerWatcher: lw,
		ObjectType:    &corev1.ConfigMap{},
		ResyncPeriod:  0,
		Handler: cache.ResourceEventHandlerFuncs{
			AddFunc:    apply,
			UpdateFunc: func(_, newObj any) { apply(newObj) },
			// Deliberately no DeleteFunc. If the map is deleted the
			// callout keeps serving what it last had rather than
			// refusing every connection: the object going away is
			// far more likely to be an operator mishap or a
			// mid-flight re-render than an instruction to revoke
			// every identity on the bus at once. The operator
			// re-renders it on the next reconcile, and until then
			// the version the callout reports is one the ConfigMap
			// no longer carries, which is what surfaces it.
		},
	})

	informer.Run(ctx.Done())
}

// WaitForMap blocks until a map is being served or the timeout expires. Used at
// startup so the process does not report ready before it can answer anything.
func (s *Store) WaitForMap(ctx context.Context, timeout time.Duration) error {
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	tick := time.NewTicker(mapPollInterval)
	defer tick.Stop()

	for {
		if s.Ready() {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-deadline.C:
			if why := s.Unserved(); why != "" {
				return fmt.Errorf("no identity map after %s; %s", timeout, why)
			}
			return fmt.Errorf("no identity map after %s", timeout)
		case <-tick.C:
		}
	}
}
