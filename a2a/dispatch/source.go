package dispatch

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"slices"
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

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// defaultQueueTimeout guards a profile whose queueTimeoutSeconds the
	// operator left unresolved (zero): the spec's default.
	defaultQueueTimeout = 3600 * time.Second
	// configPollInterval paces WaitForConfig.
	configPollInterval = 50 * time.Millisecond
	// nameFieldSelector narrows the ConfigMap watch to one object.
	nameFieldSelector = "metadata.name"
)

// Source hands out the dispatch config the operator rendered (contract
// addendum 3: lib.DispatchConfig in the `<agent>-a2a-dispatch` ConfigMap).
// It is read once per decision, so a change applies without a restart: to
// new offers only, since a task handed out keeps the lease and renewal its
// offer carried. Every value in it is resolved; the dispatcher derives
// nothing. A zero config (none loaded) has no profiles, so nothing binds.
type Source interface {
	Config() lib.DispatchConfig
}

// Pool reports the ready worker pods of a (profile, tier) pool: who the
// dispatcher may offer a task to. OPEN: the contract doesn't say where this
// comes from yet (see the dispatcher's PR); StaticPool serves the tests.
type Pool interface {
	Pods(profile, tier string) []string
}

// StaticSource is a Source set whole by the caller.
type StaticSource struct {
	mu  sync.RWMutex
	cfg lib.DispatchConfig
}

// NewStaticSource starts a source at cfg.
func NewStaticSource(cfg lib.DispatchConfig) *StaticSource { return &StaticSource{cfg: cfg} }

// Config implements Source.
func (s *StaticSource) Config() lib.DispatchConfig {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.cfg
}

// Set replaces the config.
func (s *StaticSource) Set(cfg lib.DispatchConfig) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cfg = cfg
}

// StaticPool is a Pool from a map keyed "<profile>/<tier>".
type StaticPool struct {
	mu   sync.RWMutex
	pods map[string][]string
}

// NewStaticPool starts an empty pool.
func NewStaticPool() *StaticPool { return &StaticPool{pods: map[string][]string{}} }

// Set replaces one pool's pods.
func (p *StaticPool) Set(profile, tier string, pods ...string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.pods[profile+"/"+tier] = pods
}

// Pods implements Pool.
func (p *StaticPool) Pods(profile, tier string) []string {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return slices.Clone(p.pods[profile+"/"+tier])
}

// ConfigStore is the binary's Source: the dispatch ConfigMap, watched
// through the API (as the auth callout watches its identity map), so a
// change lands when the API server commits it rather than on a kubelet
// volume sync. Each rendered config is parsed and held to
// lib.DispatchConfig.Validate (which bounds the lease at three renewals and
// the restart grace at two); one that fails is refused, logged, and the last
// good one keeps serving.
type ConfigStore struct {
	current atomic.Pointer[lib.DispatchConfig]
	lastErr atomic.Pointer[string]
	log     *slog.Logger
}

// NewConfigStore returns an empty store: no profiles until a config lands.
func NewConfigStore(log *slog.Logger) *ConfigStore {
	if log == nil {
		log = slog.Default()
	}
	return &ConfigStore{log: log}
}

// Config implements Source.
func (s *ConfigStore) Config() lib.DispatchConfig {
	if c := s.current.Load(); c != nil {
		return *c
	}
	return lib.DispatchConfig{}
}

// Ready reports whether a config has landed.
func (s *ConfigStore) Ready() bool { return s.current.Load() != nil }

// LastError is why the most recent config was refused, if it was.
func (s *ConfigStore) LastError() string {
	if e := s.lastErr.Load(); e != nil {
		return *e
	}
	return ""
}

// Update parses, validates and installs one rendered config.
func (s *ConfigStore) Update(raw []byte) error {
	var cfg lib.DispatchConfig
	err := json.Unmarshal(raw, &cfg)
	if err == nil {
		err = cfg.Validate()
	}
	if err != nil {
		msg := err.Error()
		s.lastErr.Store(&msg)
		if prev := s.current.Load(); prev != nil {
			s.log.Error("dispatch config refused; keeping the last good one", "err", err, "serving", prev.Version)
		} else {
			s.log.Error("dispatch config refused and none is loaded; nothing is dispatched", "err", err)
		}
		return err
	}
	prev := s.current.Swap(&cfg)
	s.lastErr.Store(nil)
	if prev == nil || prev.Version != cfg.Version {
		s.log.Info("dispatch config loaded", "version", cfg.Version, "profiles", len(cfg.Profiles), "ceiling", cfg.Ceiling)
	}
	return nil
}

// WatchConfigMap keeps the store current from the one ConfigMap name in
// namespace, reading key, and blocks until ctx ends. The watch is narrowed
// to that name, so no other ConfigMap enters the process. A deleted map
// changes nothing: the last good config keeps serving until the operator
// re-renders it.
func (s *ConfigStore) WatchConfigMap(ctx context.Context, client kubernetes.Interface, namespace, name, key string) error {
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
			s.log.Error("dispatch ConfigMap has no such key", "configmap", cm.Name, "key", key)
			return
		}
		_ = s.Update([]byte(raw))
	}
	_, informer := cache.NewInformerWithOptions(cache.InformerOptions{
		ListerWatcher: lw,
		ObjectType:    &corev1.ConfigMap{},
		Handler: cache.ResourceEventHandlerFuncs{
			AddFunc:    apply,
			UpdateFunc: func(_, obj any) { apply(obj) },
		},
	})
	informer.Run(ctx.Done())
	return ctx.Err()
}

// WaitForConfig blocks until a config has landed, ctx ends or timeout
// passes.
func (s *ConfigStore) WaitForConfig(ctx context.Context, timeout time.Duration) error {
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	tick := time.NewTicker(configPollInterval)
	defer tick.Stop()
	for !s.Ready() {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-deadline.C:
			if e := s.LastError(); e != "" {
				return fmt.Errorf("no dispatch config after %s; the last one was refused: %s", timeout, e)
			}
			return fmt.Errorf("no dispatch config after %s", timeout)
		case <-tick.C:
		}
	}
	return nil
}

// profileOf finds a profile by name.
func profileOf(cfg lib.DispatchConfig, name string) (lib.DispatchProfile, bool) {
	for _, p := range cfg.Profiles {
		if p.Name == name {
			return p, true
		}
	}
	return lib.DispatchProfile{}, false
}

func allowsTier(p lib.DispatchProfile, tier string) bool { return slices.Contains(p.Tiers, tier) }

func queueTimeout(p lib.DispatchProfile) time.Duration {
	if p.QueueTimeoutSeconds <= 0 {
		return defaultQueueTimeout
	}
	return seconds(p.QueueTimeoutSeconds)
}

func seconds(n int) time.Duration { return time.Duration(n) * time.Second }
