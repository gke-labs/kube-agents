// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	"sigs.k8s.io/yaml"
)

// flags holds the CLI-based configurations parsed once during startup.
type flags struct {
	daemonURL         string
	tokenEnv          string
	mode              string
	targetSession     string
	owner             string
	reasons           string
	namespaces        string
	excludeNamespaces string
	dedupWindow       time.Duration
	dedupPersist      string
	unhealthyMinCount int
	inCluster         bool
	kubeconfig        string
	profilesDir       string
	clusterName       string
	logLevel          string
	dryRun            bool
	metricsAddr       string
	snapshotInterval  time.Duration
}

// parseFlags reads command-line arguments into the flags struct.
func parseFlags(args []string) (*flags, error) {
	fs := flag.NewFlagSet("k8s-event-watcher", flag.ContinueOnError)
	f := &flags{}

	// Required.
	fs.StringVar(&f.daemonURL, "daemon-url", "", "Base URL of the core-agent daemon (http://... or https://...). Required.")
	fs.StringVar(&f.tokenEnv, "token-env", "", "Env var name holding the bearer token for the daemon. Required.")

	// Session routing.
	fs.StringVar(&f.mode, "mode", "per-incident", "Session routing mode: per-incident (create per (uid,reason)) or shared (all to --target-session).")
	fs.StringVar(&f.targetSession, "target-session", "", "Required when --mode=shared: SessionID to post all injects to.")
	fs.StringVar(&f.owner, "owner", "", "X-Asserted-Caller value for POST /sessions in per-incident mode. Sidecar must be in daemon's proxy_identities.")

	// Event filtering.
	fs.StringVar(&f.reasons, "reason", "", "Comma-separated allow-list of Event.Reason values. Empty = shipped default set.")
	fs.StringVar(&f.namespaces, "namespace", "", "Comma-separated allow-list of namespaces. Empty = all namespaces.")
	fs.StringVar(&f.excludeNamespaces, "exclude-namespace", "", "Comma-separated deny-list of namespaces.")

	// Dedup.
	fs.DurationVar(&f.dedupWindow, "dedup-window", 5*time.Minute, "Rolling window for (uid,reason) dedup.")
	fs.StringVar(&f.dedupPersist, "dedup-persist", "", "Optional path to persist dedup cache across sidecar restart.")
	fs.IntVar(&f.unhealthyMinCount, "unhealthy-min-count", 3, "Require this many consecutive Unhealthy events before firing.")

	// Kubernetes client.
	fs.BoolVar(&f.inCluster, "in-cluster", false, "Use in-cluster service account credentials. Auto-detected inside a pod.")
	fs.StringVar(&f.kubeconfig, "kubeconfig", "", "Explicit kubeconfig path (single cluster). Used outside a pod.")
	fs.StringVar(&f.profilesDir, "profiles-dir", "", "Hermes profiles directory (normally /opt/data/profiles). Enables multi-cluster fan-in: every Cluster Agent profile found becomes a watched cluster, using that profile's own kubeconfig.yaml and cluster_identity. Mutually exclusive with --kubeconfig / --in-cluster / --cluster-name.")
	fs.StringVar(&f.clusterName, "cluster-name", "", "Human-readable cluster name included in every inject payload (single-cluster mode only; with --profiles-dir the name comes from each profile's cluster_identity).")

	// Operational.
	fs.StringVar(&f.logLevel, "log-level", "info", "One of: debug, info, warn, error.")
	fs.BoolVar(&f.dryRun, "dry-run", false, "Print inject payloads to stdout without calling the daemon.")
	fs.StringVar(&f.metricsAddr, "metrics-addr", "", "Prometheus /metrics + /healthz listener address (host:port). Empty = disabled.")
	fs.DurationVar(&f.snapshotInterval, "snapshot-interval", 30*time.Second, "How often to persist the dedup cache when --dedup-persist is set. 0 = only on shutdown.")

	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	return f, nil
}

// validate checks for invalid or missing flag combinations before starting services.
func (f *flags) validate() error {
	if !f.dryRun && f.daemonURL == "" {
		return errors.New("--daemon-url is required (unless --dry-run)")
	}
	if !f.dryRun && f.tokenEnv == "" {
		return errors.New("--token-env is required (unless --dry-run)")
	}
	if strings.HasSuffix(f.daemonURL, "/") {
		return fmt.Errorf("--daemon-url must not end with '/' (got %q)", f.daemonURL)
	}
	switch f.mode {
	case "per-incident":
		// --owner only ever becomes the X-Asserted-Caller header on requests
		// to the daemon. --dry-run makes none, so it is not required there.
		// Note this is the only check --dry-run exempts: everything below
		// still applies, since bad flag combinations are worth catching in a
		// dry run too — a dry run is where they are most likely to be tripped.
		if !f.dryRun && f.owner == "" {
			return errors.New("--owner is required in per-incident mode (must match a proxy identity in the daemon config)")
		}
	case "shared":
		if f.targetSession == "" {
			return errors.New("--target-session is required in shared mode")
		}
	default:
		return fmt.Errorf("--mode must be per-incident or shared (got %q)", f.mode)
	}
	if f.dedupWindow <= 0 {
		return errors.New("--dedup-window must be > 0")
	}
	if f.snapshotInterval < 0 {
		return errors.New("--snapshot-interval must be >= 0")
	}
	if f.profilesDir != "" {
		if f.kubeconfig != "" {
			return errors.New("--profiles-dir and --kubeconfig are mutually exclusive")
		}
		if f.inCluster {
			return errors.New("--profiles-dir and --in-cluster are mutually exclusive")
		}
		if f.clusterName != "" {
			return errors.New("--cluster-name must be empty when --profiles-dir is set (names come from each profile's cluster_identity)")
		}
	} else if f.clusterName == "" {
		// With profiles, each cluster is named by its own cluster_identity.
		// Without, there is nothing to fall back on, so an unset name would
		// label every payload and metric series with the empty string.
		return errors.New("--cluster-name is required (it labels every inject payload and metric series)")
	}
	return nil
}

// splitCSV parses a comma-separated string slice, trimming whitespace and ignoring empty items.
func splitCSV(s string) []string {
	if s == "" {
		return nil
	}
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

// buildKubeClient creates a Kubernetes client interface, prioritizing explicit
// kubeconfig flags, then in-cluster settings, and falling back to default contexts.
func buildKubeClient(f *flags) (kubernetes.Interface, error) {
	var (
		cfg *rest.Config
		err error
	)
	switch {
	case f.kubeconfig != "":
		if _, statErr := os.Stat(f.kubeconfig); statErr == nil {
			cfg, err = clientcmd.BuildConfigFromFlags("", f.kubeconfig)
			if err != nil {
				return nil, fmt.Errorf("kubeconfig %s: %w", f.kubeconfig, err)
			}
			break
		} else if !errors.Is(statErr, os.ErrNotExist) {
			return nil, fmt.Errorf("kubeconfig %s stat: %w", f.kubeconfig, statErr)
		}
		// Fallback to in-cluster config if explicit kubeconfig file does not exist
		log.Printf("kubeconfig file %s not found, falling back to in-cluster config", f.kubeconfig)
		fallthrough
	case f.inCluster || os.Getenv("KUBERNETES_SERVICE_HOST") != "":
		cfg, err = rest.InClusterConfig()
		if err != nil {
			return nil, fmt.Errorf("in-cluster config: %w", err)
		}
	default:
		// Fallback to default kubeconfig search (KUBECONFIG env,
		// then $HOME/.kube/config). Fine for local dev; a real
		// deployment always sets --in-cluster or --kubeconfig.
		loader := clientcmd.NewDefaultClientConfigLoadingRules()
		cfg, err = clientcmd.NewNonInteractiveDeferredLoadingClientConfig(loader, &clientcmd.ConfigOverrides{}).ClientConfig()
		if err != nil {
			return nil, fmt.Errorf("default kubeconfig: %w", err)
		}
	}
	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		return nil, fmt.Errorf("kubernetes client: %w", err)
	}
	return client, nil
}

// targetCluster is one cluster the watcher should monitor, discovered from a
// Cluster Agent profile on the shared PVC.
type targetCluster struct {
	// Name, ProjectID and Location come from the profile's cluster_identity
	// block, which the Platform Agent writes as machine-readable metadata.
	// Deliberately not derived from the profile directory name: that name is
	// sanitized and hash-truncated past 63 chars, so it is lossy.
	Name      string
	ProjectID string
	Location  string
	// Profile is the directory name, carried for log and error messages.
	Profile string
	Client  kubernetes.Interface
}

// clusterIdentity is the cluster_identity block the Platform Agent writes into
// each Cluster Agent profile's config.yaml. sigs.k8s.io/yaml converts YAML to
// JSON, hence the json tags.
type clusterIdentity struct {
	Project  string `json:"project"`
	Cluster  string `json:"cluster"`
	Location string `json:"location"`
}

// profileConfig is the subset of a profile's config.yaml that we read.
type profileConfig struct {
	ClusterIdentity clusterIdentity `json:"cluster_identity"`
}

// discoverClusterProfiles scans a Hermes profiles directory (normally
// /opt/data/profiles) and returns one targetCluster per Cluster Agent profile
// found. The Platform Agent creates these on cluster onboarding and removes
// them on teardown — see agents/platform/scripts/cluster_agent_profile.py — so
// the directory is the live inventory of clusters we should be watching, and
// each profile already holds credentials scoped to its own cluster.
//
// A subdirectory is treated as a cluster profile only if it has both a
// kubeconfig.yaml and a config.yaml carrying a complete cluster_identity
// block. That is how non-cluster profiles ("default", "platform") are skipped:
// testing for the data we need is more durable than hardcoding a list of names
// that the Python side may extend.
//
// A profile that looks like a cluster profile but fails to load is a fatal
// error rather than a skip — silently dropping a cluster would mean silently
// not monitoring it.
func discoverClusterProfiles(dir string) ([]targetCluster, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("read profiles dir %s: %w", dir, err)
	}
	var clusters []targetCluster
	seen := make(map[string]string) // cluster name -> profile it came from
	for _, e := range entries {
		if !e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		home := filepath.Join(dir, e.Name())
		kubeconfig := filepath.Join(home, "kubeconfig.yaml")
		if _, err := os.Stat(kubeconfig); err != nil {
			continue // not a cluster profile
		}
		identity, err := readClusterIdentity(filepath.Join(home, "config.yaml"))
		if err != nil {
			return nil, fmt.Errorf("profile %s: %w", e.Name(), err)
		}
		if identity == nil {
			continue // not a cluster profile
		}
		if prev, dup := seen[identity.Cluster]; dup {
			return nil, fmt.Errorf("profiles %s and %s both claim cluster %q", prev, e.Name(), identity.Cluster)
		}
		seen[identity.Cluster] = e.Name()

		cfg, err := clientcmd.BuildConfigFromFlags("", kubeconfig)
		if err != nil {
			return nil, fmt.Errorf("profile %s: kubeconfig %s: %w", e.Name(), kubeconfig, err)
		}
		client, err := kubernetes.NewForConfig(cfg)
		if err != nil {
			return nil, fmt.Errorf("profile %s: kubernetes client: %w", e.Name(), err)
		}
		clusters = append(clusters, targetCluster{
			Name:      identity.Cluster,
			ProjectID: identity.Project,
			Location:  identity.Location,
			Profile:   e.Name(),
			Client:    client,
		})
	}
	if len(clusters) == 0 {
		return nil, fmt.Errorf("profiles dir %s: no Cluster Agent profiles found (expected subdirectories containing kubeconfig.yaml and a config.yaml with a cluster_identity block)", dir)
	}
	return clusters, nil
}

// readClusterIdentity parses the cluster_identity block out of a profile's
// config.yaml. Returns nil (not an error) when the file is absent or the block
// is missing or incomplete — that means "not a cluster profile", matching what
// cluster_agent_profile.read_cluster_identity treats as absent. A config.yaml
// that exists but cannot be parsed is a real error.
func readClusterIdentity(path string) (*clusterIdentity, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	var cfg profileConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	id := cfg.ClusterIdentity
	if id.Cluster == "" || id.Project == "" || id.Location == "" {
		return nil, nil
	}
	return &id, nil
}

// dispatcher coordinates the filter, deduplication, HTTP injector, and metrics for streamed events.
// One dispatcher is built per watched cluster, each owning that cluster's dedup
// cache; filter, injector, and metrics are shared across all of them. The source
// cluster is still read off each TriageEvent rather than stored here, so the
// payload is correct regardless of how dispatchers are wired.
//
// Dispatch holds no dispatcher-wide lock, and does not need one. client-go
// delivers events to a handler from a single per-informer processorListener
// goroutine, so a given dispatcher is only ever entered by its own cluster's
// watcher, one event at a time. Across clusters the dispatchers share nothing
// mutable. A lock here would have served only to make one cluster's slow daemon
// round-trip stall every other cluster.
type dispatcher struct {
	filter    *filter
	dedup     *dedupCache
	injector  *injector
	metrics   *metrics
	mode      string // "per-incident" or "shared"
	targetSid string // for shared mode
	dryRun    bool
}

// newDispatcher builds a dispatcher around one cluster's dedup cache. filter,
// injector, and metrics are shared across every cluster — they are stateless
// or goroutine-safe — while dedup is per-cluster.
func newDispatcher(f *flags, filter *filter, dedup *dedupCache, inj *injector, m *metrics) *dispatcher {
	return &dispatcher{
		filter:    filter,
		dedup:     dedup,
		injector:  inj,
		metrics:   m,
		mode:      f.mode,
		targetSid: f.targetSession,
		dryRun:    f.dryRun,
	}
}

// dedupPersistPath derives a per-cluster snapshot path from the --dedup-persist
// base, since each cluster keeps its own cache and they cannot all write the
// same file: "/var/lib/w/dedup.json" + "prod-us" → "/var/lib/w/dedup-prod-us.json".
// Returns "" (persistence disabled) when base is empty. cluster is a GKE
// cluster name, so it never contains a path separator.
func dedupPersistPath(base, cluster string) string {
	if base == "" {
		return ""
	}
	ext := filepath.Ext(base)
	return strings.TrimSuffix(base, ext) + "-" + cluster + ext
}

// Dispatch is the entry point that runs an event through filtering, deduplication, and HTTP injection.
func (d *dispatcher) Dispatch(ctx context.Context, ev TriageEvent) {
	d.metrics.eventsSeen.WithLabelValues(ev.Cluster, ev.Key.Reason).Inc()
	if !d.filter.Accept(ev) {
		return
	}
	result := d.dedup.Observe(ev.Key, ev.Message, ev.LastSeen)
	d.metrics.activeIncidents.WithLabelValues(ev.Cluster).Set(float64(d.dedup.Len()))
	if result.Kind == dedupDuplicate {
		d.metrics.eventsDedupSuppress.WithLabelValues(ev.Cluster, ev.Key.Reason, ev.Namespace).Inc()
		log.Printf("dedup %s pod=%s/%s (count=%d, window active)",
			ev.Key.Reason, ev.Namespace, ev.Name, result.Count)
		return
	}
	// Create or reuse a troubleshooter session, then inject event telemetry.
	sid := d.targetSid
	if d.mode == "per-incident" && !d.dryRun {
		newSid, err := d.injector.CreateSession(ctx)
		if err != nil {
			log.Printf("dispatcher: create session for %s/%s: %v", ev.Namespace, ev.Name, err)
			d.metrics.sessionCreates.WithLabelValues(ev.Cluster, "error").Inc()
			d.metrics.injectErrors.WithLabelValues(ev.Cluster, ev.Key.Reason, "session_create").Inc()
			return
		}
		sid = newSid
		d.metrics.sessionCreates.WithLabelValues(ev.Cluster, "ok").Inc()
		d.dedup.BindSession(ev.Key, ev.Message, sid)
	}
	payload := InjectPayload{
		Kind:         injectKindEvent,
		Reason:       ev.Key.Reason,
		Namespace:    ev.Namespace,
		KindOfObject: ev.KindOfObject,
		Name:         ev.Name,
		Container:    ev.Container,
		UID:          ev.Key.UID,
		Message:      ev.Message,
		Count:        result.Count,
		FirstSeen:    ev.FirstSeen,
		LastSeen:     ev.LastSeen,
		Cluster:      ev.Cluster,
		Type:         ev.Type,
		Context: PayloadContext{
			ControllerRef: ev.ControllerRef,
			Node:          ev.Node,
			Labels:        ev.Labels,
		},
	}
	if d.dryRun {
		out, _ := json.MarshalIndent(payload, "", "  ")
		fmt.Printf("--- dry-run payload for session %q ---\n%s\n", sid, string(out))
		d.metrics.eventsInjected.WithLabelValues(ev.Cluster, ev.Key.Reason, ev.Namespace).Inc()
		log.Printf("would-fire %s pod=%s/%s (sid=%s, mode=%s, dry-run)",
			ev.Key.Reason, ev.Namespace, ev.Name, sid, d.mode)
		return
	}
	if err := d.injector.Inject(ctx, sid, payload); err != nil {
		log.Printf("dispatcher: inject for %s/%s (sid=%s): %v", ev.Namespace, ev.Name, sid, err)
		d.metrics.injectErrors.WithLabelValues(ev.Cluster, ev.Key.Reason, "inject").Inc()
		return
	}
	d.metrics.eventsInjected.WithLabelValues(ev.Cluster, ev.Key.Reason, ev.Namespace).Inc()
	log.Printf("fire %s pod=%s/%s → sid=%s (mode=%s)",
		ev.Key.Reason, ev.Namespace, ev.Name, sid, d.mode)
}

func main() {
	if err := realMain(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "k8s-event-watcher:", err)
		os.Exit(1)
	}
}

func realMain(argv []string) error {
	f, err := parseFlags(argv)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if err := f.validate(); err != nil {
		return err
	}

	// Resolve bearer token from env (unless dry-run).
	var token string
	if !f.dryRun {
		token = os.Getenv(f.tokenEnv)
		if token == "" {
			return fmt.Errorf("bearer token env var %s is empty", f.tokenEnv)
		}
	}

	// Build components.
	filterCfg := newFilterConfig(splitCSV(f.reasons), splitCSV(f.namespaces), splitCSV(f.excludeNamespaces), f.unhealthyMinCount)
	filter := newFilter(filterCfg)

	m := newMetrics()

	var inj *injector
	if !f.dryRun {
		inj, err = newInjector(injectorConfig{
			daemonURL:      f.daemonURL,
			bearerToken:    token,
			assertedCaller: f.owner,
		})
		if err != nil {
			return fmt.Errorf("injector: %w", err)
		}
	}

	// The dedup cache and its dispatcher are built per cluster further down —
	// see the two run paths below. Everything constructed here (filter,
	// metrics, injector) is stateless or goroutine-safe and is shared.

	metricsSrv, err := startMetrics(f.metricsAddr, m)
	if err != nil {
		return fmt.Errorf("metrics server start: %w", err)
	}

	// Set up context cancellation on SIGINT/SIGTERM for clean shutdown.
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	// Start the background Prometheus metrics server.
	go func() {
		if err := metricsSrv.Run(ctx); err != nil {
			log.Printf("metrics server: %v", err)
		}
	}()

	if f.dryRun {
		if f.profilesDir != "" {
			log.Printf("k8s-event-watcher: running in --dry-run mode; watching clusters from %s without calling the daemon", f.profilesDir)
		} else {
			log.Printf("k8s-event-watcher: running in --dry-run mode; watching cluster %q without calling the daemon", f.clusterName)
		}
	}

	// Multi-cluster fan-in path: one watcher goroutine per Cluster Agent
	// profile in --profiles-dir, each with its own dedup cache and dispatcher.
	if f.profilesDir != "" {
		clusters, err := discoverClusterProfiles(f.profilesDir)
		if err != nil {
			return err
		}
		log.Printf("k8s-event-watcher: multi-cluster mode: %d cluster(s) from %s → daemon %s (mode=%s, owner=%s)",
			len(clusters), f.profilesDir, f.daemonURL, f.mode, f.owner)

		caches := make([]*dedupCache, 0, len(clusters))
		var wg sync.WaitGroup
		for _, tc := range clusters {
			cache, err := newDedupCache(f.dedupWindow, dedupPersistPath(f.dedupPersist, tc.Name))
			if err != nil {
				return fmt.Errorf("dedup cache for cluster %s: %w", tc.Name, err)
			}
			caches = append(caches, cache)
			clusterDisp := newDispatcher(f, filter, cache, inj, m)
			if f.dedupPersist != "" && f.snapshotInterval > 0 {
				go runSnapshotLoop(ctx, cache, f.snapshotInterval)
			}
			wg.Add(1)
			go func(tc targetCluster, disp *dispatcher) {
				defer wg.Done()
				w := newWatcher(tc.Client, disp, tc.Name, 0)
				log.Printf("k8s-event-watcher: [%s] starting informer (project=%s location=%s profile=%s)",
					tc.Name, tc.ProjectID, tc.Location, tc.Profile)
				if err := w.Run(ctx); err != nil {
					// Log and continue — one cluster's informer
					// failing must not blind the fleet. The peer
					// goroutines keep running.
					log.Printf("k8s-event-watcher: [%s] informer exited: %v", tc.Name, err)
				}
			}(tc, clusterDisp)
		}
		wg.Wait()
		for _, cache := range caches {
			if snapErr := cache.Snapshot(); snapErr != nil {
				log.Printf("dedup snapshot on shutdown: %v", snapErr)
			}
		}
		return nil
	}

	// Single-cluster path (backward compatible).
	dedup, err := newDedupCache(f.dedupWindow, f.dedupPersist)
	if err != nil {
		return fmt.Errorf("dedup cache: %w", err)
	}
	if f.dedupPersist != "" && f.snapshotInterval > 0 {
		go runSnapshotLoop(ctx, dedup, f.snapshotInterval)
	}
	disp := newDispatcher(f, filter, dedup, inj, m)

	client, err := buildKubeClient(f)
	if err != nil {
		return err
	}

	w := newWatcher(client, disp, f.clusterName, 0)
	log.Printf("k8s-event-watcher: starting on cluster %q → daemon %s (mode=%s, owner=%s)",
		f.clusterName, f.daemonURL, f.mode, f.owner)
	err = w.Run(ctx)
	if snapErr := dedup.Snapshot(); snapErr != nil {
		log.Printf("dedup snapshot on shutdown: %v", snapErr)
	}
	return err
}

// runSnapshotLoop periodically triggers a cache persistence snapshot.
func runSnapshotLoop(ctx context.Context, cache *dedupCache, interval time.Duration) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if err := cache.Snapshot(); err != nil {
				log.Printf("dedup snapshot: %v", err)
			}
		}
	}
}
