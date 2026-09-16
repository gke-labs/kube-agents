package authcallout

import (
	"context"
	"fmt"
	"log/slog"
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

	// lastErr holds the reason the most recent update was refused, if it
	// was. A refused update leaves the previous map serving, which is the
	// right behaviour — the alternative is to drop every identity because
	// one entry was malformed — but it must not be silent, or the callout
	// reports healthy while serving a version nobody rendered.
	lastErr atomic.Pointer[string]

	log *slog.Logger
}

// NewStore returns an empty store. It serves nothing until an update lands, and
// Ready reports false until then, so a callout that cannot read its map refuses
// connections rather than inventing grants for them.
func NewStore(log *slog.Logger) *Store {
	if log == nil {
		log = slog.Default()
	}
	return &Store{log: log}
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

// LastError returns the reason the most recent update was refused, if it was.
func (s *Store) LastError() string {
	if e := s.lastErr.Load(); e != nil {
		return *e
	}
	return ""
}

// Update parses and installs a rendered map.
//
// A map that does not parse is refused and the previous one keeps serving. That
// is deliberate: the callout is on the connection path, so the blast radius of
// accepting a bad map is every future connection, while the blast radius of
// keeping a good one is that a rendered change has not taken effect yet — and
// the version the callout reports says so, which is what stops the operator
// marking BusCredentialsReady on a version that never landed.
func (s *Store) Update(raw []byte) error {
	m, err := ParseIdentityMap(raw)
	if err != nil {
		msg := err.Error()
		s.lastErr.Store(&msg)
		serving := s.Version()
		if serving == "" {
			s.log.Error("identity map refused and none is being served; every connection will be refused", "error", err)
		} else {
			s.log.Error("identity map refused; continuing to serve the previous version", "error", err, "serving", serving)
		}
		return err
	}

	previous := s.Version()
	s.current.Store(m)
	s.lastErr.Store(nil)

	// Logged at every change, because "the map says X" has to be checkable
	// against the running system rather than against the rendered object.
	if previous == "" {
		s.log.Info("serving identity map", "version", m.Version, "identities", len(m.Identities), "users", m.Users())
	} else if previous != m.Version {
		s.log.Info("identity map changed", "from", previous, "to", m.Version, "identities", len(m.Identities), "users", m.Users())
	}
	return nil
}

// WatchConfigMap keeps the store current from one ConfigMap, and blocks until
// the context is cancelled.
//
// The watch is narrowed to a single object by name. A namespace-wide ConfigMap
// informer would put every ConfigMap in the namespace into this process's cache
// for the sake of one — the same class of mistake as the cluster-wide Secret
// and Job informers a dark A2A install used to start (W6 finding #3).
func (s *Store) WatchConfigMap(ctx context.Context, client kubernetes.Interface, namespace, name, key string) error {
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
		_ = s.Update([]byte(raw))
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
	return ctx.Err()
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
			if e := s.LastError(); e != "" {
				return fmt.Errorf("no identity map after %s; the last one was refused: %s", timeout, e)
			}
			return fmt.Errorf("no identity map after %s", timeout)
		case <-tick.C:
		}
	}
}
