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
	"encoding/base64"
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
	"sync/atomic"
	"syscall"
	"time"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
	container "google.golang.org/api/container/v1"
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
	backoffMinCount   int
	// imagePullTransientMinCount gates only the self-clearing half of the
	// image-pull family; see filter.go.
	imagePullTransientMinCount int

	inCluster        bool
	kubeconfig       string
	profilesDir      string
	clusterName      string
	logLevel         string
	dryRun           bool
	metricsAddr      string
	snapshotInterval time.Duration
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
	fs.IntVar(&f.backoffMinCount, "backoff-min-count", 3, "Require this many consecutive crash-loop (BackOff/CrashLoopBackOff) events before firing. Suppresses startup races that resolve on their own. 1 = fire on the first event.")
	fs.IntVar(&f.imagePullTransientMinCount, "imagepull-transient-min-count", 3, "Require this many consecutive image-pull failures before firing, when the error looks self-clearing (registry 429/5xx, timeouts). Terminal causes such as a bad tag, and any cause the classifier does not recognize, always fire on the first event. 1 = fire on the first event.")

	// Kubernetes client.
	fs.BoolVar(&f.inCluster, "in-cluster", false, "Use in-cluster service account credentials. Auto-detected inside a pod.")
	fs.StringVar(&f.kubeconfig, "kubeconfig", "", "Explicit kubeconfig path (single cluster). Used outside a pod.")
	fs.StringVar(&f.profilesDir, "profiles-dir", "", "Hermes profiles directory (normally /opt/data/profiles). Enables multi-cluster fan-in: every Cluster Agent profile found becomes a watched cluster, addressed by asking the GKE API about that profile's cluster_identity. Combines with --in-cluster / --kubeconfig, which add one directly-reachable cluster on top; that combination also requires --cluster-name to name it.")
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
	// Profile is the directory name. Unique by construction — the Python side
	// derives it from the whole triple — so it doubles as the per-cluster
	// filename for dedup snapshots, where the bare name would collide.
	Profile string
	Client  kubernetes.Interface
}

// identity is what makes this cluster distinct from every other watched
// cluster. A GKE cluster name is unique only within a project and location, so
// a fleet can legitimately run "prod" in us-central1 and "prod" in
// europe-west1 — the Platform Agent creates a profile for each, keyed on the
// full triple. Keying on the bare name here would treat the second as a
// duplicate of the first and silently leave it unmonitored.
//
// Empty for the direct --in-cluster/--kubeconfig cluster, which has no
// cluster_identity to read. That is safe: there is only ever one of it, so it
// cannot collide with itself, and its name still distinguishes it from the
// profile clusters.
func (tc targetCluster) identity() string {
	return tc.ProjectID + "/" + tc.Location + "/" + tc.Name
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
// profile records which cluster it is scoped to.
//
// This runs once, from buildWatchSet at startup, so the result is a snapshot
// rather than something the watcher keeps in step with the directory. A cluster
// onboarded later is not watched until the process restarts, and one torn down
// later leaves an informer retrying against a control plane that is gone.
// Re-scanning on an interval and reconciling the running informers against the
// result is deliberate follow-up work, not done here.
//
// A subdirectory is treated as a cluster profile only if its config.yaml
// carries a complete cluster_identity block. That is how non-cluster profiles
// ("default", "platform") are skipped: testing for the data we need is more
// durable than hardcoding a list of names that the Python side may extend.
//
// The identity is also the whole of what a client is built from, and that is a
// deliberate change of source. Discovery used to require a kubeconfig.yaml
// beside config.yaml and read the API server address out of it, which stopped
// working the moment the shell moved into its own pod: `gcloud container
// clusters get-credentials` now runs there, so the file it writes lands on the
// sandbox's volume and this pod never sees it. Every profile created after that
// change would have been silently unwatched. Asking the GKE API for the address
// removes the dependency rather than reinstating it — and it has to be removed
// rather than reinstated, because anything read back out of the sandbox is
// writable by the model, and this process attaches a cloud-platform token to
// whatever host it is told to talk to.
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
// Finding none is not an error either. A freshly installed harness has no
// Cluster Agent profiles until the first cluster-agent-reconcile tick creates
// them, so an empty result is a normal startup state rather than a
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
	// pod identity, and a process with no cluster profiles at all -- which is
	// every single-cluster install -- should not pay for a credential it never
	// uses.
	var tokenSource oauth2.TokenSource
	// Keyed on the full project/location/cluster triple, not the bare name:
	// two clusters called "prod" in different locations are two clusters, and
	// both get their own profile. See targetCluster.identity.
	seen := make(map[string]string) // identity -> profile it came from
	for _, e := range entries {
		if !e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		home := filepath.Join(dir, e.Name())
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
		tc := targetCluster{
			Name:      identity.Cluster,
			ProjectID: identity.Project,
			Location:  identity.Location,
			Profile:   e.Name(),
		}
		if prev, dup := seen[tc.identity()]; dup {
			skip("cluster %s is already claimed by profile %s", tc.identity(), prev)
			continue
		}

		if tokenSource == nil {
			tokenSource, err = newTokenSource(ctx)
			if err != nil {
				skip("this pod has no Google credentials, so its control plane cannot be reached: %v", err)
				continue
			}
		}
		described, err := describeCluster(ctx, *identity)
		if err != nil {
			skip("asking the GKE API where %s is: %v", tc.identity(), err)
			continue
		}
		cfg, err := clientConfigForIdentity(described)
		if err != nil {
			skip("%v", err)
			continue
		}
		useGoogleTokenSource(cfg, tokenSource)
		client, err := kubernetes.NewForConfig(cfg)
		if err != nil {
			skip("kubernetes client: %v", err)
			continue
		}
		seen[tc.identity()] = e.Name()
		tc.Client = client
		clusters = append(clusters, tc)
	}
	return clusters, nil
}

// clusterResourceFormat is the GKE Cluster Manager API's name for one cluster.
// The location segment takes a region or a zone, so one form covers both.
const clusterResourceFormat = "projects/%s/locations/%s/clusters/%s"

// describeCluster reads one cluster's control-plane addressing from the GKE
// API. A variable rather than a plain function so the tests can answer it
// without a Google credential or a network call; nothing else reassigns it.
var describeCluster = describeClusterViaAPI

// newTokenSource mints the credential every watched control plane is reached
// with. A variable for the same reason as describeCluster.
var newTokenSource = func(ctx context.Context) (oauth2.TokenSource, error) {
	return google.DefaultTokenSource(ctx, gkeAuthScope)
}

func describeClusterViaAPI(ctx context.Context, identity clusterIdentity) (*container.Cluster, error) {
	svc, err := container.NewService(ctx)
	if err != nil {
		return nil, fmt.Errorf("GKE API client: %w", err)
	}
	name := fmt.Sprintf(clusterResourceFormat, identity.Project, identity.Location, identity.Cluster)
	cluster, err := svc.Projects.Locations.Clusters.Get(name).Context(ctx).Do()
	if err != nil {
		return nil, fmt.Errorf("get %s: %w", name, err)
	}
	return cluster, nil
}

// clientConfigForIdentity turns a GKE cluster description into a rest.Config
// addressing that cluster's control plane. It carries no credential; the caller
// attaches one with useGoogleTokenSource.
//
// The DNS endpoint wins wherever the cluster publishes one that accepts
// external traffic, which is the same rule agents/platform/scripts/
// gke_endpoint.py applies when it decides whether to pass --dns-endpoint to
// `gcloud container clusters get-credentials`. Keep the two in step: they exist
// to reach the same control planes, and a cluster whose IP endpoint this pod
// cannot route to is exactly the case the DNS endpoint was added for. It needs
// no CA of its own — it terminates on a Google frontend with a WebPKI
// certificate — where the IP endpoint is signed by the cluster's own CA.
//
// An address is required. Returning a config with an empty Host would hand
// rest.Config a relative URL and produce a client that talks to nothing in a
// way no error names.
func clientConfigForIdentity(cluster *container.Cluster) (*rest.Config, error) {
	if cluster == nil {
		return nil, errors.New("the GKE API returned no cluster")
	}
	if endpoints := cluster.ControlPlaneEndpointsConfig; endpoints != nil && endpoints.DnsEndpointConfig != nil {
		dns := endpoints.DnsEndpointConfig
		if dns.AllowExternalTraffic && dns.Endpoint != "" {
			return &rest.Config{Host: "https://" + dns.Endpoint}, nil
		}
	}
	if cluster.Endpoint == "" {
		return nil, errors.New("the cluster publishes neither an externally reachable DNS endpoint nor an IP endpoint")
	}
	if cluster.MasterAuth == nil || cluster.MasterAuth.ClusterCaCertificate == "" {
		return nil, errors.New("the cluster's IP endpoint has no CA certificate to verify it against")
	}
	ca, err := base64.StdEncoding.DecodeString(cluster.MasterAuth.ClusterCaCertificate)
	if err != nil {
		return nil, fmt.Errorf("decode the cluster CA certificate: %w", err)
	}
	return &rest.Config{
		Host:            "https://" + cluster.Endpoint,
		TLSClientConfig: rest.TLSClientConfig{CAData: ca},
	}, nil
}

// gkeAuthScope is the scope gke-gcloud-auth-plugin requests. A cloud-platform
// token is accepted by the GKE control plane as a bearer credential.
const gkeAuthScope = "https://www.googleapis.com/auth/cloud-platform"

// initialSyncGrace is how long the process will run with nothing synced before
// giving up and letting its supervisor restart it. Generous on purpose: it has
// to cover a cold API server, a slow first list on a large cluster, and token
// minting, and the cost of being wrong is a restart loop. It is not a per
// cluster deadline — individual informers keep retrying indefinitely, and one
// cluster syncing is enough to satisfy it.
const initialSyncGrace = 2 * time.Minute

// useGoogleTokenSource attaches a bearer token minted from this process's
// Google credentials to every request the config makes.
//
// A kubeconfig from `gcloud container clusters get-credentials` would
// authenticate by shelling out to gke-gcloud-auth-plugin. That binary is
// deliberately absent here: the image build refuses to ship credential-aware
// CLIs into the agent's containers (deploy/docker/Dockerfile), concentrating
// them in the credential proxy instead. Rather than widen that boundary, mint
// the token directly — the pod already authenticates to Google as this identity
// via Workload Identity, which is the same identity the plugin would have used.
//
// Only the credential is set. The API server address and CA certificate come
// from clientConfigForIdentity, and are untouched.
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
	data, err := os.ReadFile(path) // #nosec G304 -- Path to profile config file supplied via flag / discovery
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
	filter *filter
	dedup  *dedupCache
	// pullClasses carries the cause of an image-pull failure from the event that
	// names it to the causeless back-off event that follows. Per-cluster like
	// dedup, so one cluster churning through pods cannot evict another's entries
	// out of the shared bound.
	pullClasses *pullClassMemo
	injector    *injector
	metrics     *metrics
	mode        string // "per-incident" or "shared"
	targetSid   string // for shared mode
	dryRun      bool
}

// newDispatcher builds a dispatcher around one cluster's dedup cache. filter,
// injector, and metrics are shared across every cluster — they are stateless
// or goroutine-safe — while dedup and the pull-class memo are per-cluster.
func newDispatcher(f *flags, filter *filter, dedup *dedupCache, inj *injector, m *metrics) *dispatcher {
	return &dispatcher{
		filter:      filter,
		dedup:       dedup,
		pullClasses: newPullClassMemo(defaultPullClassTTL, defaultPullClassEntries),
		injector:    inj,
		metrics:     m,
		mode:        f.mode,
		targetSid:   f.targetSession,
		dryRun:      f.dryRun,
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

const eventTypeWarning = "Warning"

// daemonWouldAlert reports whether session_kv_server would grade an event of
// this Event.Type above Info — that is, post it to chat rather than record it
// for the daily recap.
//
// Two lines decide that, and reading only the second gets it wrong.
// inject_message coerces the type before grading it:
//
//	event_type = payload.get("type") or "Warning"
//	... event_lower == "warning" ? Warning/Critical : Info
//
// So an absent or empty type is graded *Warning*, not Info. `InjectPayload.Type`
// carries no omitempty, so an empty Go string reaches the daemon as `"type": ""`,
// which is falsy in Python and takes that coercion. get_severity_details read on
// its own says the opposite, and a guard written against it alone withholds the
// reopen from an event the daemon goes on to alert on — the silence this whole
// path exists to prevent.
//
// EqualFold because the daemon lowercases before comparing. One function so the
// rule has one home: the fake daemon in dispatcher_test.go is written against
// these same two lines, and a fixture that mirrors the wrong one turns a
// regression into a passing test.
func daemonWouldAlert(eventType string) bool {
	return eventType == "" || strings.EqualFold(eventType, eventTypeWarning)
}

// reopenPolicyFiltered reports whether a deduplicated event should escape
// suppression because the entry holding its key was left behind by an event the
// daemon policy-filtered. Only an event the daemon would alert on gets that
// chance.
//
// Admitting anything wider spends a budget that does not come back. The reopen
// is bounded at one firing per window by the sticky Reopened flag, so an event
// this guard admits and the daemon then grades Info takes the family's only
// reopen and returns an entry that no later Warning can reopen, while Observe's
// Case 3 slides LastSeen on every sighting so it never expires either. Barring
// only a literal "Normal" admitted exactly those events: an empty or lowercase
// Type passed and came back Info. Hence daemonWouldAlert rather than a test
// against one string.
//
// The mirror is not load-bearing on its own. Grading lives in another language
// on another image and can drift, so MarkPolicyFiltered drops a reopened entry
// whose own inject comes back filtered rather than leaving it held — the next
// sighting then opens a fresh incident instead of the family going quiet.
//
// The dedupResult it returns is the one the caller must go on to inject with,
// and it counts from 1 like any other new incident. The result Observe handed
// back describes the entry that has just been replaced: its Count is every
// sighting in the canonical family, almost all of which reached nobody. Putting
// that number on the wire would write it to `occurrences` in the ledger, and
// the recap sums that column to report how many events were forwarded.
func (d *dispatcher) reopenPolicyFiltered(ev TriageEvent, replay bool) (dedupResult, bool) {
	// An informer rotation re-delivers events whose LastTimestamp has not
	// moved. Alerting on one costs a stale page and the family's one firing;
	// an ongoing failure reopens on its next real sighting instead.
	if replay {
		return dedupResult{}, false
	}
	if !daemonWouldAlert(ev.Type) {
		return dedupResult{}, false
	}
	if !d.dedup.ReopenIfPolicyFiltered(ev.Key, ev.Message, ev.LastSeen) {
		return dedupResult{}, false
	}
	d.metrics.eventsPolicyReopened.WithLabelValues(ev.Cluster, ev.Project, ev.Location, ev.Key.Reason, ev.Namespace).Inc()
	log.Printf("reopening %s pod=%s/%s — type=%q outranks the policy-filtered event holding its dedup key",
		ev.Key.Reason, ev.Namespace, ev.Name, ev.Type)
	return dedupResult{Kind: dedupNewIncident, Count: 1}, true
}

// Dispatch is the entry point that runs an event through filtering, deduplication, and HTTP injection.
func (d *dispatcher) Dispatch(ctx context.Context, ev TriageEvent) {
	d.metrics.eventsSeen.WithLabelValues(ev.Cluster, ev.Project, ev.Location, ev.Key.Reason).Inc()
	// Resolved before the filter, deliberately. kubelet splits an image-pull
	// incident across four events and only one of them names the cause; that one is
	// reason=Failed, which the shipped default --reason list does not carry. The
	// informer applies no reason pre-filter, so the cause-bearing event still
	// reaches this line even when the allow-list is about to drop it — and the
	// memo is what lets the causeless back-off that follows inherit its class and
	// its error text. Classifying inside the filter would see only the events that
	// got that far, and would run after the gate that needs the answer.
	if canonicalizeReason(ev.Key.Reason, ev.Message) == "ImagePullBackOff" {
		res := d.pullClasses.Resolve(ev.Key.UID, ev.Message)
		ev.PullClass = res.Class
		if res.Cause != ev.Message {
			ev.PullCause = res.Cause
		}
	}
	if gate := d.filter.Decide(ev); gate != gateAccepted {
		d.metrics.eventsFiltered.WithLabelValues(ev.Cluster, ev.Project, ev.Location, string(gate)).Inc()
		return
	}
	result := d.dedup.Observe(ev.Key, ev.Message, ev.LastSeen)
	d.metrics.activeIncidents.WithLabelValues(ev.Cluster, ev.Project, ev.Location).Set(float64(d.dedup.Len()))
	if result.Kind == dedupDuplicate {
		reopened, ok := d.reopenPolicyFiltered(ev, result.Replay)
		if !ok {
			d.metrics.eventsDedupSuppress.WithLabelValues(ev.Cluster, ev.Project, ev.Location, ev.Key.Reason, ev.Namespace).Inc()
			log.Printf("dedup %s pod=%s/%s (count=%d, window active)",
				ev.Key.Reason, ev.Namespace, ev.Name, result.Count)
			return
		}
		// Overwritten, not merged: the entry this event now owns was created
		// by the reopen and counts from 1, and everything downstream of the
		// payload treats Count as the number of sightings this row stands for.
		result = reopened
	}
	// Create or reuse a troubleshooter session, then inject event telemetry.
	sid := d.targetSid
	if d.mode == "per-incident" && !d.dryRun {
		newSid, err := d.injector.CreateSession(ctx)
		if err != nil {
			// Roll back the entry Observe just wrote. Nobody was told
			// about this failure, so the next sighting must be free to
			// open the incident rather than be suppressed against an
			// alert that never went out. See dedupCache.Forget.
			d.dedup.Forget(ev.Key, ev.Message)
			log.Printf("dispatcher: create session for %s/%s: %v", ev.Namespace, ev.Name, err)
			d.metrics.sessionCreates.WithLabelValues(ev.Cluster, ev.Project, ev.Location, "error").Inc()
			d.metrics.injectErrors.WithLabelValues(ev.Cluster, ev.Project, ev.Location, ev.Key.Reason, "session_create").Inc()
			return
		}
		sid = newSid
		d.metrics.sessionCreates.WithLabelValues(ev.Cluster, ev.Project, ev.Location, "ok").Inc()
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
		PullCause:    ev.PullCause,
		Count:        result.Count,
		FirstSeen:    ev.FirstSeen,
		LastSeen:     ev.LastSeen,
		Cluster:      ev.Cluster,
		Project:      ev.Project,
		Location:     ev.Location,
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
		d.metrics.eventsInjected.WithLabelValues(ev.Cluster, ev.Project, ev.Location, ev.Key.Reason, ev.Namespace).Inc()
		log.Printf("would-fire %s pod=%s/%s (sid=%s, mode=%s, dry-run)",
			ev.Key.Reason, ev.Namespace, ev.Name, sid, d.mode)
		return
	}
	status, err := d.injector.Inject(ctx, sid, payload)
	if err != nil {
		// Same rollback as the CreateSession path above. A session may
		// have been created for this attempt and is now orphaned; it
		// ages out on the daemon's own TTL, and leaving the failure
		// permanently unreportable would be the worse trade.
		d.dedup.Forget(ev.Key, ev.Message)
		log.Printf("dispatcher: inject for %s/%s (sid=%s): %v", ev.Namespace, ev.Name, sid, err)
		d.metrics.injectErrors.WithLabelValues(ev.Cluster, ev.Project, ev.Location, ev.Key.Reason, "inject").Inc()
		return
	}
	if status == injectStatusFiltered {
		// 2xx, and nobody was told — but by policy rather than by
		// exhaustion, so the dedup entry stays. The daemon graded the
		// event Info and will report it as a count in the daily recap.
		// Nothing about the next sighting would grade differently, so
		// rolling back here would re-offer the same routine churn every
		// time the workload re-emits, each round trip costing a session
		// row and a ledger row for an alert nobody was ever going to get.
		//
		// Flagged rather than merely kept, because the key it holds belongs
		// to a whole canonical family and the rest of that family may well
		// grade Warning. See dedupCache.ReopenIfPolicyFiltered.
		d.dedup.MarkPolicyFiltered(ev.Key, ev.Message)
		d.metrics.eventsPolicyFiltered.WithLabelValues(ev.Cluster, ev.Project, ev.Location, ev.Key.Reason, ev.Namespace).Inc()
		log.Printf("policy-filtered %s pod=%s/%s (sid=%s) — daemon graded it Info; counted in the daily recap",
			ev.Key.Reason, ev.Namespace, ev.Name, sid)
		return
	}
	if status == injectStatusSuppressed {
		// 2xx, and nobody was told: the daemon spent the day's ceiling
		// for this severity and dropped the alert. Rolled back for the
		// same reason as an error — the dedup entry exists to stop a
		// second copy of a delivered alert, and nothing was delivered.
		// The ceiling resets at 00:00 UTC while this entry would
		// otherwise outlive the reset by most of a day.
		d.dedup.Forget(ev.Key, ev.Message)
		d.metrics.eventsQuotaSuppress.WithLabelValues(ev.Cluster, ev.Project, ev.Location, ev.Key.Reason, ev.Namespace).Inc()
		log.Printf("quota-suppressed %s pod=%s/%s (sid=%s) — daemon dropped the alert, incident reopened",
			ev.Key.Reason, ev.Namespace, ev.Name, sid)
		return
	}
	d.metrics.eventsInjected.WithLabelValues(ev.Cluster, ev.Project, ev.Location, ev.Key.Reason, ev.Namespace).Inc()
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
	filterCfg := newFilterConfig(splitCSV(f.reasons), splitCSV(f.namespaces), splitCSV(f.excludeNamespaces), filterThresholds{
		unhealthyMinCount:          f.unhealthyMinCount,
		backoffMinCount:            f.backoffMinCount,
		imagePullTransientMinCount: f.imagePullTransientMinCount,
	})
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
	// started counts clusters that got as far as launching an informer; synced
	// counts those whose initial list actually completed. The gap between them
	// is the whole problem: an informer that never syncs never returns either,
	// so "still running" says nothing about whether a cluster is being watched.
	// Only synced does.
	started := 0
	var synced atomic.Int64
	for _, tc := range clusters {
		// Suffixed with the profile, not the cluster name: two clusters can
		// share a name across locations, and they must not share a snapshot.
		cache, err := newDedupCache(f.dedupWindow, dedupPersistPath(f.dedupPersist, tc.Profile))
		if err != nil {
			// Same reasoning as profile discovery: one cluster failing to start
			// must not take the fleet down with it. Each cluster has its own
			// snapshot file, so an unreadable one is a per-cluster problem.
			log.Printf("k8s-event-watcher: [%s] dedup cache failed, this cluster will NOT be watched: %v", tc.Name, err)
			m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(0)
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
			w := newWatcher(tc.Client, disp, tc, 0)
			log.Printf("k8s-event-watcher: [%s] starting informer (source=%s project=%s location=%s)",
				tc.Name, tc.Profile, tc.ProjectID, tc.Location)
			// Starts at 0 and only reaches 1 once the initial list completes.
			// Setting it before Run would have reported every cluster up the
			// instant its goroutine started, including ones whose control plane
			// was unreachable — those block inside Run forever rather than
			// failing, so liveness of the goroutine proves nothing.
			m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(0)
			defer m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(0)
			onSynced := func() {
				synced.Add(1)
				m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(1)
				log.Printf("k8s-event-watcher: [%s] informer synced, now watching", tc.Name)
			}
			if err := w.Run(ctx, onSynced); err != nil {
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

	// Refuse to sit there watching nothing. Individual informers deliberately
	// never give up — the reflector retries a failed list forever, so a cluster
	// that comes back recovers on its own without a restart — but that same
	// patience means a process where *nothing* ever syncs looks identical to a
	// healthy one: goroutines alive, no errors returned, exit 0 on SIGTERM.
	// Cross-cluster RBAC missing on first rollout is exactly that state.
	//
	// So bound it once, at the level where it is unambiguous: if no cluster at
	// all has synced within the grace period, the run is not working and the
	// supervisor should restart it. A cluster that syncs late still counts, and
	// partial failure is left alone — one unreachable cluster out of seven is a
	// per-cluster problem, reported by cluster_up, not grounds for tearing down
	// the six that work.
	syncFailed := make(chan struct{})
	go func() {
		t := time.NewTimer(initialSyncGrace)
		defer t.Stop()
		select {
		case <-ctx.Done():
		case <-t.C:
			if synced.Load() == 0 {
				log.Printf("k8s-event-watcher: no cluster synced within %s — check cross-cluster RBAC and API server reachability; exiting so the supervisor retries", initialSyncGrace)
				close(syncFailed)
				cancel()
			}
		}
	}()

	wg.Wait()
	for _, cache := range caches {
		if snapErr := cache.Snapshot(); snapErr != nil {
			log.Printf("dedup snapshot on shutdown: %v", snapErr)
		}
	}
	select {
	case <-syncFailed:
		return fmt.Errorf("no cluster synced within %s; %d informer(s) started but none completed their initial list", initialSyncGrace, started)
	default:
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
// The operator passes both, and the management cluster is reached through both:
// it runs the platform agent, so --in-cluster covers it from the first second of
// a fresh install, and cluster_agent_reconcile.py now also gives it a Cluster
// Agent profile like every other cluster in the project.
//
// Which means the two sources overlap, and the overlap has to be resolved here.
// Each watched cluster gets its OWN dedup cache (see the loop in run), and
// EventKey is (UID, reason) with no cluster in it, so two entries for one
// cluster are not deduplicated anywhere downstream: the same pod crash would
// raise two alerts, open two sessions, and burn two slots of the daily ceiling.
//
// The two entries are not interchangeable, so which one survives matters. The
// profile carries the project/location/cluster identity that stamps the payload
// and labels every metric; the direct entry knows only a name. But the profile
// also carries a credential that can be refused: it authenticates as the GSA,
// so a permission set without roles/container.viewer, or master authorized
// networks that exclude the pod's egress, denies it. The direct entry uses the pod's own service account against
// kubernetes.default.svc and never leaves the cluster.
//
// Nothing here would notice the difference. discoverClusterProfiles contacts
// the GKE API for the control plane's address, but nothing exercises that
// address until the informer's initial list — so a profile still displaces on
// the strength of an answer that says where the cluster is rather than one that
// says the cluster will talk to us, and by the time it will not, the entry it
// displaced is gone. On a single-cluster install that leaves a watch set of one,
// and a watch set of one that never syncs trips the initialSyncGrace check in
// run: the process exits and the pod crashloops watching nothing. On a fleet it
// is quieter and worse — the peers sync, nothing restarts, and the one cluster
// whose failure breaks everything else is silently unmonitored.
//
// So the direct entry wins and takes the profile's identity triple with it.
// Reachability is the half that cannot be reconstructed; project and location are
// the half that can. The profile is untouched — it is still the agent that
// answers the triage, it is just not how the events are read.
//
// Unless the name is ambiguous. The direct entry can only match on the bare
// name, and two profiles can share one, so a collision makes the match a guess:
// the watch set then keeps every profile and drops the direct entry instead.
func buildWatchSet(ctx context.Context, f *flags, m *metrics) ([]targetCluster, error) {
	var clusters []targetCluster
	// Cluster name -> every profile with that name. A slice because the name is
	// not unique: GKE names are unique per (project, location), so one project
	// with two regions is enough to collide.
	profilesNamed := make(map[string][]targetCluster)

	if f.profilesDir != "" {
		discovered, err := discoverClusterProfiles(ctx, f.profilesDir, m)
		if err != nil {
			return nil, err
		}
		if len(discovered) == 0 {
			// Normal before the first reconcile tick of a fresh install, and
			// the reason --in-cluster is still passed at all.
			log.Printf("k8s-event-watcher: no Cluster Agent profiles in %s (nothing to fan out to yet)", f.profilesDir)
		}
		for _, tc := range discovered {
			profilesNamed[tc.Name] = append(profilesNamed[tc.Name], tc)
		}
		clusters = append(clusters, discovered...)
	}

	// Add the directly-reachable cluster when asked for explicitly, or when
	// --profiles-dir was not given at all (the single-cluster default).
	if f.profilesDir == "" || f.inCluster || f.kubeconfig != "" {
		// Matched on the bare name: --in-cluster reads no cluster_identity, so
		// there is no triple to compare.
		candidates := profilesNamed[f.clusterName]
		if len(candidates) > 1 {
			// Ambiguous, so absorb nothing and add no direct entry. Picking one
			// would unwatch the other cluster and stamp this one with its
			// location, and the duplicate would survive through the profile not
			// picked. Safe only here: two or more profiles means the watch set
			// cannot be the single never-syncing entry initialSyncGrace guards.
			log.Printf("k8s-event-watcher: %d profiles are named %q (%s) and the in-cluster client cannot say which one it is; watching them through their profiles and adding no direct entry",
				len(candidates), f.clusterName, identities(candidates))
		} else {
			// Profile stays "direct" whether or not a profile was absorbed: it is
			// the per-cluster filename for dedup snapshots, and every install
			// already on disk has this cluster's cache under that name. Renaming
			// it would silently resume from an empty cache after an upgrade.
			direct := targetCluster{Name: f.clusterName, Profile: "direct"}
			if len(candidates) == 1 {
				covered := candidates[0]
				clusters = removeProfile(clusters, covered.Profile)
				direct.ProjectID, direct.Location = covered.ProjectID, covered.Location
				log.Printf("k8s-event-watcher: %s is covered by profile %s and by the direct client; watching it once, directly, as %s (the pod's own credential cannot be denied by IAM or by master authorized networks)",
					f.clusterName, covered.Profile, direct.identity())
			}
			client, err := buildKubeClient(f)
			if err != nil {
				return nil, err
			}
			direct.Client = client
			clusters = append(clusters, direct)
		}
	}

	if len(clusters) == 0 {
		return nil, fmt.Errorf("no clusters to watch: %s contained no Cluster Agent profiles and neither --in-cluster nor --kubeconfig was given", f.profilesDir)
	}
	return clusters, nil
}

// identities renders clusters as "profile=project/location/name" for a log line.
func identities(clusters []targetCluster) string {
	parts := make([]string, 0, len(clusters))
	for _, tc := range clusters {
		parts = append(parts, tc.Profile+"="+tc.identity())
	}
	return strings.Join(parts, ", ")
}

// removeProfile drops the entry discovered from profile, preserving the order of
// the rest. Profile directory names are unique by construction (the Python side
// derives them from the whole project/location/cluster triple), so this removes
// exactly one entry.
func removeProfile(clusters []targetCluster, profile string) []targetCluster {
	kept := make([]targetCluster, 0, len(clusters))
	for _, tc := range clusters {
		if tc.Profile != profile {
			kept = append(kept, tc)
		}
	}
	return kept
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
