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
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
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
	fs.StringVar(&f.profilesDir, "profiles-dir", "", "Hermes profiles directory (normally /opt/data/profiles). Enables multi-cluster fan-in: every Cluster Agent profile found becomes a watched cluster, using that profile's own kubeconfig.yaml and cluster_identity. Combines with --in-cluster / --kubeconfig, which add one directly-reachable cluster on top; that combination also requires --cluster-name to name it.")
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
	// Cluster sources are additive, not exclusive: --profiles-dir contributes
	// every Cluster Agent profile, and --in-cluster / --kubeconfig contributes
	// one directly-reachable cluster on top. The operator passes both, because
	// the management cluster deliberately never gets a Cluster Agent profile
	// (cluster_agent_reconcile.py excludes it) yet still has to be watched — it
	// is where the platform agent itself runs.
	//
	// The one thing the combination needs is a name for that direct cluster:
	// profile clusters are named by their cluster_identity, so an unnamed peer
	// alongside them would report an empty cluster label on every payload and
	// metric.
	if f.profilesDir != "" && (f.inCluster || f.kubeconfig != "") && f.clusterName == "" {
		return errors.New("--cluster-name is required when combining --profiles-dir with --in-cluster or --kubeconfig (it names the directly-watched cluster; profile clusters are named by their cluster_identity)")
	}
	// With no profiles at all there is no cluster_identity to fall back on, so
	// the name is the only source of cluster identity the watcher has.
	// In-cluster deploys always pass one; a hand-run or dry-run may not.
	if f.profilesDir == "" && f.clusterName == "" {
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
// the directory is the inventory of clusters we should be watching, and each
// profile already holds credentials scoped to its own cluster.
//
// This runs once, from buildWatchSet at startup, so the result is a snapshot
// rather than something the watcher keeps in step with the directory. A cluster
// onboarded later is not watched until the process restarts, and one torn down
// later leaves an informer retrying against a control plane that is gone.
// Re-scanning on an interval and reconciling the running informers against the
// result is deliberate follow-up work, not done here.
//
// A subdirectory is treated as a cluster profile only if it has both a
// kubeconfig.yaml and a config.yaml carrying a complete cluster_identity
// block. That is how non-cluster profiles ("default", "platform") are skipped:
// testing for the data we need is more durable than hardcoding a list of names
// that the Python side may extend.
//
// A profile that looks like a cluster profile but fails to load is skipped, not
// fatal. Dropping one cluster is bad; the alternative is worse, because these
// errors would propagate out of buildWatchSet before it has even built the
// direct client, so a single unparseable config.yaml would stop the watcher
// monitoring anything at all — including the management cluster, whose client
// would have been fine. Every skip logs and increments
// clusterDiscoveryErrors{profile}, so "this cluster is not being watched" stays
// visible and alertable without being fatal.
//
// Failing to read the directory splits two ways, because a restart fixes one
// kind of failure and not the other.
//
// A directory that does not exist yet is fatal. It is written by another
// process that may simply not have run, so exiting is what makes this
// self-healing: whatever supervises the watcher restarts it, and the next
// attempt succeeds once the directory appears. Degrading instead would be
// permanent — discovery runs once, so a watcher that starts without profiles
// keeps watching only the direct cluster until something else restarts it,
// which is a far worse outcome than a few seconds of restarts at boot.
//
// Any other read error — permissions, I/O — is not something a restart will
// fix, so those degrade rather than crashloop forever.
//
// Finding none is not an error either. A single-cluster install has no Cluster
// Agent profiles at all — reconcile only creates them for clusters other than
// the management one — so an empty result is a normal steady state, not a
// misconfiguration. The caller decides whether the combined watch set is empty.
func discoverClusterProfiles(ctx context.Context, dir string, m *metrics) ([]targetCluster, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, fmt.Errorf("profiles dir %s does not exist yet (it is created by the platform agent; exiting so the next start can pick it up): %w", dir, err)
		}
		log.Printf("k8s-event-watcher: cannot read profiles dir %s, no profile clusters will be watched: %v", dir, err)
		m.clusterDiscoveryErrors.WithLabelValues("-").Inc()
		return nil, nil
	}
	var clusters []targetCluster
	// Minted on first use, then shared: every profile authenticates as the same
	// pod identity, and a process with no Google credentials at all (a local
	// run against hand-written kubeconfigs) should not pay for one it never needs.
	var tokenSource oauth2.TokenSource
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
		skip := func(format string, args ...any) {
			log.Printf("k8s-event-watcher: skipping profile %s, its cluster will NOT be watched: %s",
				e.Name(), fmt.Sprintf(format, args...))
			m.clusterDiscoveryErrors.WithLabelValues(e.Name()).Inc()
		}

		identity, err := readClusterIdentity(filepath.Join(home, "config.yaml"))
		if err != nil {
			skip("%v", err)
			continue
		}
		if identity == nil {
			continue // not a cluster profile
		}
		if prev, dup := seen[identity.Cluster]; dup {
			skip("cluster %q is already claimed by profile %s", identity.Cluster, prev)
			continue
		}

		cfg, err := clientcmd.BuildConfigFromFlags("", kubeconfig)
		if err != nil {
			skip("kubeconfig %s: %v", kubeconfig, err)
			continue
		}
		if cfg.ExecProvider != nil {
			if tokenSource == nil {
				tokenSource, err = google.DefaultTokenSource(ctx, gkeAuthScope)
				if err != nil {
					skip("kubeconfig authenticates via the %q exec plugin, which is not in this image; falling back to Google credentials failed: %v",
						cfg.ExecProvider.Command, err)
					continue
				}
			}
			useGoogleTokenSource(cfg, tokenSource)
		}
		client, err := kubernetes.NewForConfig(cfg)
		if err != nil {
			skip("kubernetes client: %v", err)
			continue
		}
		seen[identity.Cluster] = e.Name()
		clusters = append(clusters, targetCluster{
			Name:      identity.Cluster,
			ProjectID: identity.Project,
			Location:  identity.Location,
			Profile:   e.Name(),
			Client:    client,
		})
	}
	return clusters, nil
}

// gkeAuthScope is the scope gke-gcloud-auth-plugin requests. A cloud-platform
// token is accepted by the GKE control plane as a bearer credential.
const gkeAuthScope = "https://www.googleapis.com/auth/cloud-platform"

// useGoogleTokenSource swaps a kubeconfig's exec-plugin authentication for a
// bearer token minted from this process's Google credentials.
//
// Cluster Agent profile kubeconfigs come from `gcloud container clusters
// get-credentials`, so they authenticate by shelling out to
// gke-gcloud-auth-plugin. That binary is deliberately absent here: the image
// build refuses to ship credential-aware CLIs into the agent's containers
// (deploy/docker/Dockerfile), concentrating them in the credential proxy
// instead. Rather than widen that boundary, mint the token directly — the pod
// already authenticates to Google as this identity via Workload Identity, which
// is the same identity the plugin would have used.
//
// Only the credential is replaced. The API server address and CA certificate
// still come from the kubeconfig, and are untouched.
func useGoogleTokenSource(cfg *rest.Config, ts oauth2.TokenSource) {
	cfg.ExecProvider = nil
	cfg.AuthProvider = nil
	cfg.Wrap(func(rt http.RoundTripper) http.RoundTripper {
		return &oauth2.Transport{Source: ts, Base: rt}
	})
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

	clusters, err := buildWatchSet(ctx, f, m)
	if err != nil {
		return err
	}
	if f.dryRun {
		log.Printf("k8s-event-watcher: running in --dry-run mode; watching %d cluster(s) without calling the daemon", len(clusters))
	}
	log.Printf("k8s-event-watcher: watching %d cluster(s) → daemon %s (mode=%s, owner=%s)",
		len(clusters), f.daemonURL, f.mode, f.owner)

	// One watcher goroutine per cluster, each with its own dedup cache and
	// dispatcher so a noisy cluster cannot evict a quiet one's incidents.
	caches := make([]*dedupCache, 0, len(clusters))
	var wg sync.WaitGroup
	started := 0
	for _, tc := range clusters {
		cache, err := newDedupCache(f.dedupWindow, dedupPersistPath(f.dedupPersist, tc.Name))
		if err != nil {
			// Same reasoning as profile discovery: one cluster failing to start
			// must not take the fleet down with it. Each cluster has its own
			// snapshot file, so an unreadable one is a per-cluster problem.
			log.Printf("k8s-event-watcher: [%s] dedup cache failed, this cluster will NOT be watched: %v", tc.Name, err)
			m.clusterUp.WithLabelValues(tc.Name).Set(0)
			continue
		}
		caches = append(caches, cache)
		started++
		clusterDisp := newDispatcher(f, filter, cache, inj, m)
		if f.dedupPersist != "" && f.snapshotInterval > 0 {
			go runSnapshotLoop(ctx, cache, f.snapshotInterval)
		}
		wg.Add(1)
		go func(tc targetCluster, disp *dispatcher) {
			defer wg.Done()
			w := newWatcher(tc.Client, disp, tc.Name, 0)
			log.Printf("k8s-event-watcher: [%s] starting informer (source=%s project=%s location=%s)",
				tc.Name, tc.Profile, tc.ProjectID, tc.Location)
			m.clusterUp.WithLabelValues(tc.Name).Set(1)
			// Whatever happens next, this cluster stops being watched when Run
			// returns — a failed initial sync, a cancelled context, anything.
			// The gauge is the only signal for that: unlike discovery, these
			// failures are environmental (RBAC, an unreachable control plane, an
			// expired CA) and leave no trace in the profile directory.
			defer m.clusterUp.WithLabelValues(tc.Name).Set(0)
			if err := w.Run(ctx); err != nil {
				// Log and continue — one cluster's informer failing
				// must not blind the rest. The peer goroutines keep
				// running.
				log.Printf("k8s-event-watcher: [%s] informer exited: %v", tc.Name, err)
			}
		}(tc, clusterDisp)
	}
	if started == 0 {
		// Every cluster failed before its informer began. Exiting non-zero
		// matters because the alternative is wg.Wait() returning immediately
		// and the process reporting success while watching nothing.
		return fmt.Errorf("no clusters could be started: all %d failed to build a dedup cache", len(clusters))
	}
	wg.Wait()
	for _, cache := range caches {
		if snapErr := cache.Snapshot(); snapErr != nil {
			log.Printf("dedup snapshot on shutdown: %v", snapErr)
		}
	}
	return nil
}

// buildWatchSet assembles every cluster this process should watch. Sources are
// additive rather than exclusive:
//
//   - --profiles-dir contributes one entry per Cluster Agent profile.
//   - --in-cluster / --kubeconfig contributes one directly-reachable cluster.
//     Absent --profiles-dir this is the only source, which is the original
//     single-cluster behavior.
//
// The operator passes both. The management cluster never gets a Cluster Agent
// profile — cluster_agent_reconcile.py excludes it, and prunes one if it ever
// appears — but it runs the platform agent itself, so watching only the
// profiles would silently stop monitoring the very cluster whose failure
// breaks everything else.
func buildWatchSet(ctx context.Context, f *flags, m *metrics) ([]targetCluster, error) {
	var clusters []targetCluster

	if f.profilesDir != "" {
		discovered, err := discoverClusterProfiles(ctx, f.profilesDir, m)
		if err != nil {
			return nil, err
		}
		if len(discovered) == 0 {
			// Normal for a single-cluster install: reconcile only creates
			// profiles for clusters other than the management one.
			log.Printf("k8s-event-watcher: no Cluster Agent profiles in %s (nothing to fan out to yet)", f.profilesDir)
		}
		clusters = append(clusters, discovered...)
	}

	// Add the directly-reachable cluster when asked for explicitly, or when
	// --profiles-dir was not given at all (the single-cluster default).
	if f.profilesDir == "" || f.inCluster || f.kubeconfig != "" {
		client, err := buildKubeClient(f)
		if err != nil {
			return nil, err
		}
		clusters = append(clusters, targetCluster{
			Name:    f.clusterName,
			Profile: "direct",
			Client:  client,
		})
	}

	if len(clusters) == 0 {
		return nil, fmt.Errorf("no clusters to watch: %s contained no Cluster Agent profiles and neither --in-cluster nor --kubeconfig was given", f.profilesDir)
	}
	return clusters, nil
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
