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
	"strings"
	"sync"
	"syscall"
	"time"

	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
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
	fs.StringVar(&f.kubeconfig, "kubeconfig", "", "Explicit kubeconfig path. Used outside a pod.")
	fs.StringVar(&f.clusterName, "cluster-name", "", "Human-readable cluster name included in every inject payload.")

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
		if f.dryRun {
			return nil
		}
		if f.owner == "" {
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
		cfg, err = clientcmd.BuildConfigFromFlags("", f.kubeconfig)
		if err != nil {
			return nil, fmt.Errorf("kubeconfig %s: %w", f.kubeconfig, err)
		}
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

// dispatcher coordinates the filter, deduplication, HTTP injector, and metrics for streamed events.
type dispatcher struct {
	filter    *filter
	dedup     *dedupCache
	injector  *injector
	metrics   *metrics
	cluster   string
	mode      string // "per-incident" or "shared"
	targetSid string // for shared mode
	dryRun    bool
	// injectLock serializes session creation to prevent parallel events from spawning duplicate sessions.
	injectLock sync.Mutex
}

// Dispatch is the entry point that runs an event through filtering, deduplication, and HTTP injection.
func (d *dispatcher) Dispatch(ctx context.Context, ev TriageEvent) {
	d.metrics.eventsSeen.WithLabelValues(ev.Key.Reason, ev.Namespace).Inc()
	if !d.filter.Accept(ev) {
		return
	}
	result := d.dedup.Observe(ev.Key, ev.LastSeen)
	d.metrics.activeIncidents.Set(float64(d.dedup.Len()))
	if result.Kind == dedupDuplicate {
		d.metrics.eventsDedupSuppress.WithLabelValues(ev.Key.Reason, ev.Namespace).Inc()
		log.Printf("dedup %s pod=%s/%s (count=%d, window active)",
			ev.Key.Reason, ev.Namespace, ev.Name, result.Count)

		if result.SessionID != "" {
			payload := InjectPayload{
				Kind:         injectKindFollowup,
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
				Cluster:      d.cluster,
				Type:         ev.Type,
				Context: PayloadContext{
					ControllerRef: ev.ControllerRef,
					Node:          ev.Node,
					Labels:        ev.Labels,
				},
			}
			if d.dryRun {
				out, _ := json.MarshalIndent(payload, "", "  ")
				fmt.Printf("--- dry-run follow-up payload for session %q ---\n%s\n", result.SessionID, string(out))
				return
			}
			if err := d.injector.Inject(ctx, result.SessionID, payload); err != nil {
				log.Printf("dispatcher: inject follow-up for %s/%s (sid=%s): %v", ev.Namespace, ev.Name, result.SessionID, err)
				d.metrics.injectErrors.WithLabelValues(ev.Key.Reason, "inject_followup").Inc()
				return
			}
			d.metrics.eventsInjected.WithLabelValues(ev.Key.Reason, ev.Namespace).Inc()
			log.Printf("fire follow-up %s pod=%s/%s → sid=%s",
				ev.Key.Reason, ev.Namespace, ev.Name, result.SessionID)
		}
		return
	}
	// Create or reuse a troubleshooter session, then inject event telemetry.
	d.injectLock.Lock()
	defer d.injectLock.Unlock()
	sid := d.targetSid
	if d.mode == "per-incident" && !d.dryRun {
		newSid, err := d.injector.CreateSession(ctx)
		if err != nil {
			log.Printf("dispatcher: create session for %s/%s: %v", ev.Namespace, ev.Name, err)
			d.metrics.sessionCreates.WithLabelValues("error").Inc()
			d.metrics.injectErrors.WithLabelValues(ev.Key.Reason, "session_create").Inc()
			return
		}
		sid = newSid
		d.metrics.sessionCreates.WithLabelValues("ok").Inc()
		d.dedup.BindSession(ev.Key, sid)
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
		Cluster:      d.cluster,
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
		d.metrics.eventsInjected.WithLabelValues(ev.Key.Reason, ev.Namespace).Inc()
		log.Printf("would-fire %s pod=%s/%s (sid=%s, mode=%s, dry-run)",
			ev.Key.Reason, ev.Namespace, ev.Name, sid, d.mode)
		return
	}
	if err := d.injector.Inject(ctx, sid, payload); err != nil {
		log.Printf("dispatcher: inject for %s/%s (sid=%s): %v", ev.Namespace, ev.Name, sid, err)
		d.metrics.injectErrors.WithLabelValues(ev.Key.Reason, "inject").Inc()
		return
	}
	d.metrics.eventsInjected.WithLabelValues(ev.Key.Reason, ev.Namespace).Inc()
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

	dedup, err := newDedupCache(f.dedupWindow, f.dedupPersist)
	if err != nil {
		return fmt.Errorf("dedup cache: %w", err)
	}

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

	disp := &dispatcher{
		filter:    filter,
		dedup:     dedup,
		injector:  inj,
		metrics:   m,
		cluster:   f.clusterName,
		mode:      f.mode,
		targetSid: f.targetSession,
		dryRun:    f.dryRun,
	}

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

	// Start the background cache persistence loop if enabled.
	if f.dedupPersist != "" && f.snapshotInterval > 0 {
		go runSnapshotLoop(ctx, dedup, f.snapshotInterval)
	}

	// Build the kube client.
	if f.dryRun {
		log.Printf("k8s-event-watcher: --dry-run: skipping kube client; would watch cluster %q", f.clusterName)
		<-ctx.Done()
		if err := dedup.Snapshot(); err != nil {
			log.Printf("dedup snapshot on shutdown: %v", err)
		}
		return nil
	}
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
