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
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"runtime/debug"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/clusterprofiles"
)

const (
	// memoryLimitEnv carries the container's memory limit in bytes. The
	// operator sets it through the Downward API from the agent-api-auth
	// container's limits.memory (see buildAgentAPIAuthSidecar), so the watcher
	// learns the ceiling it shares without a flag that could drift from it.
	memoryLimitEnv = "EVENT_WATCHER_MEMORY_LIMIT_BYTES"
	// goMemLimitEnv is the Go runtime's own soft-limit variable. When it is
	// set the runtime has already applied it before main runs, and it is an
	// explicit choice that this process does not second-guess.
	goMemLimitEnv = "GOMEMLIMIT"
	// memoryLimitFraction is the share of the container limit the watcher
	// claims as its soft limit. The Python API authenticator lives in the same
	// container, so the whole limit is not the watcher's to spend; half leaves
	// the collector working well before the kernel's OOM killer is the only
	// thing enforcing the ceiling. A soft limit makes GC more aggressive as
	// the heap approaches it; it does not cap live heap.
	memoryLimitFraction = 0.5
	// defaultGatewayWait outlasts two of the agent container's startup probe
	// budgets (agentAPIProbe(10, 90) in platformagent_manifests.go, 905s)
	// plus the kill grace period and restart backoff between them. A boot
	// that overruns one budget is restarted, and the restarted entrypoint
	// redoes its profile steps before the gateway binds, so a single budget
	// would give up on exactly the slowest boots.
	defaultGatewayWait = 35 * time.Minute

	// defaultQuietAfter is how long an alerted incident's object goes
	// unsighted before the daemon is told: three times kubelet's 5m restart
	// back-off ceiling, so a crash loop still going is never reported quiet
	// between two of its back-offs, with a margin for a slow recorder; the
	// stall watch's tick is the same length.
	defaultQuietAfter = 15 * time.Minute
	// sweepInterval is how often each cluster's dispatcher ages its settle
	// holds and looks for quiet incidents: the dedup snapshot's cadence, so a
	// follow-up lands within half a minute of its period.
	sweepInterval = 30 * time.Second
	// settleClearedFloor is the least time a cleared key's tombstone is kept
	// (settleClearedTTL): the API server's default event TTL.
	settleClearedFloor = time.Hour
	// followupResumeGrace is how long after the informer becomes delivering
	// the sweep waits before sending follow-ups: the relist that follows a
	// returned watch re-delivers every event still inside the API server's
	// TTL within seconds, and the grace puts the sweep behind it.
	followupResumeGrace = 2 * time.Minute
	// followupMessage is the Message of a k8s-event-followup payload: what
	// the watcher knows, and no more.
	followupMessage = "no new events for this object since the last sighting"
	// The outcomes of k8s_event_watcher_followups_total: the daemon accepted
	// the line for the session's thread (it posts after answering, so a chat
	// failure past that point is invisible here, as for the alert), it had
	// no thread to post into, or the inject failed.
	followupOutcomeSent     = "sent"
	followupOutcomeUnrouted = "unrouted"
	followupOutcomeError    = "error"
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
	// failedSchedulingMinCount is the FailedScheduling backstop when
	// cluster-autoscaler has said nothing about the pod; scaleUpHold is how
	// long its TriggeredScaleUp holds the pod's events. See filter.go.
	failedSchedulingMinCount int
	scaleUpHold              time.Duration
	// autopilotScaleToZeroHold holds the FailedScheduling an Autopilot cluster
	// emits for GKE's own system pods once it has scaled to zero nodes. On by
	// default, so the flag exists to turn it off; see filter.go.
	autopilotScaleToZeroHold bool
	// settle is the settle period for the settle family, and quietAfter how
	// long an alerted incident's object goes unsighted before the daemon is
	// told. See filter.go for the first and dispatcher.Sweep for the second.
	settle     time.Duration
	quietAfter time.Duration

	inCluster        bool
	kubeconfig       string
	profilesDir      string
	clusterName      string
	logLevel         string
	dryRun           bool
	metricsAddr      string
	dataVolume       string
	snapshotInterval time.Duration
	gatewayURL       string
	gatewayWait      time.Duration
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
	fs.IntVar(&f.failedSchedulingMinCount, "failedscheduling-min-count", defaultFailedSchedulingMinCount, "Require this many consecutive FailedScheduling events before firing, when cluster-autoscaler has recorded no verdict on the pod. A NotTriggerScaleUp on the pod fires at any count; a TriggeredScaleUp holds at any count for --scaleup-hold. Both need their reason in --reason. 1 = fire on the first event.")
	fs.DurationVar(&f.scaleUpHold, "scaleup-hold", defaultScaleUpHold, "How long a TriggeredScaleUp on a pod holds its FailedScheduling events, measured from the autoscaler's event to the FailedScheduling's own last sighting. Past it the count threshold applies again. Cluster-autoscaler's default node-provision timeout.")
	fs.DurationVar(&f.settle, "settle", defaultSettle, "How long a failure in the settle family (the crash-loop family, FailedMount, Unhealthy; the reasons kubelet re-emits while they last) has to have lasted, by the event's own first and last sighting, before it is forwarded; a failure that clears inside it is not forwarded, and a repeat blip on the same event object is measured from its own start while the in-process memo remembers the first. Applied after the count thresholds, never to OOMKilled, the image-pull family or FailedScheduling. 0 = off.")
	fs.DurationVar(&f.quietAfter, "quiet-after", defaultQuietAfter, "How long after its last sighting an incident whose alert went out is reported quiet: the daemon gets one k8s-event-followup inject for it, which it posts into the alert's thread. Silence is read off the events alone; no object is read back. 0 = off.")
	fs.BoolVar(&f.autopilotScaleToZeroHold, "autopilot-scale-to-zero-hold", true, "Hold FailedScheduling saying 'no nodes available to schedule pods' on an Autopilot cluster with no cluster-autoscaler verdict on the pod: an Autopilot cluster with no user workloads scales to zero and leaves GKE's own system pods Pending by design. =false reports them, on the count backstop as before. Zero nodes on a GKE Standard cluster is a fault and still fires. Requires both TriggeredScaleUp and NotTriggerScaleUp in --reason, without which no verdict can be recorded and 'no verdict' would not distinguish an abandoned pod from a declined one; the hold turns itself off and logs when either is missing, which includes the shipped --reason default.")

	// Kubernetes client.
	fs.BoolVar(&f.inCluster, "in-cluster", false, "Use in-cluster service account credentials. Auto-detected inside a pod.")
	fs.StringVar(&f.kubeconfig, "kubeconfig", "", "Explicit kubeconfig path (single cluster). Used outside a pod.")
	fs.StringVar(&f.profilesDir, "profiles-dir", "", "Hermes profiles directory (normally /opt/data/profiles). Enables multi-cluster fan-in: every Cluster Agent profile found becomes a watched cluster, addressed by asking the GKE API about that profile's cluster_identity. Combines with --in-cluster / --kubeconfig, which add one directly-reachable cluster on top; that combination also requires --cluster-name to name it.")
	fs.StringVar(&f.clusterName, "cluster-name", "", "Human-readable cluster name included in every inject payload (single-cluster mode only; with --profiles-dir the name comes from each profile's cluster_identity).")

	// Operational.
	fs.StringVar(&f.logLevel, "log-level", "info", "One of: debug, info, warn, error.")
	fs.BoolVar(&f.dryRun, "dry-run", false, "Print inject payloads to stdout without calling the daemon.")
	fs.StringVar(&f.metricsAddr, "metrics-addr", "", "Prometheus /metrics + /healthz listener address (host:port). Empty = disabled.")
	fs.StringVar(&f.dataVolume, "data-volume", "", "Path on the agent's data volume (normally /opt/data). When set, /metrics also exports the volume's size and free space, read at each scrape. Empty = not exported.")
	fs.DurationVar(&f.snapshotInterval, "snapshot-interval", 30*time.Second, "How often to persist the dedup cache when --dedup-persist is set. 0 = only on shutdown.")
	fs.StringVar(&f.gatewayURL, "gateway-url", "", "Base URL of the agent gateway the daemon starts triage turns on. When set, events are not watched until it answers or --gateway-wait passes. Empty = do not wait.")
	fs.DurationVar(&f.gatewayWait, "gateway-wait", defaultGatewayWait, "Longest time to wait for --gateway-url to answer before watching events anyway.")

	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	return f, nil
}

// filterThresholds maps the parsed flags onto the filter's threshold group. A
// method rather than a literal at the one call site because of the sign flip
// it carries: --autopilot-scale-to-zero-hold reads positively and the group's
// field negatively (filterThresholds explains why), and a stray edit to that
// one "!" ships a binary with the hold off by default — the exact behaviour
// the hold exists to prevent, and invisible, since every other test builds its
// own filterThresholds and would stay green. Inlined at the call site that
// negation sat inside realMain, which no test calls; here it is the unit
// TestFlagsFilterThresholds covers.
func (f *flags) filterThresholds() filterThresholds {
	return filterThresholds{
		unhealthyMinCount:               f.unhealthyMinCount,
		backoffMinCount:                 f.backoffMinCount,
		imagePullTransientMinCount:      f.imagePullTransientMinCount,
		failedSchedulingMinCount:        f.failedSchedulingMinCount,
		scaleUpHold:                     f.scaleUpHold,
		disableAutopilotScaleToZeroHold: !f.autopilotScaleToZeroHold,
		settle:                          settleThreshold(f.settle),
	}
}

// settleClearedTTL is how long the settle memo keeps a cleared key's
// tombstone: the dedup window, past which a repeat is a new incident anyway,
// but never less than settleClearedFloor, the API server's default event
// TTL, inside which kubelet's recorder still holds the blip's event object
// and a repeat arrives with the old first sighting. The binary's own 5m
// window default would otherwise forget a blip twelve minutes after it
// cleared and forward its repeat at once.
func settleClearedTTL(dedupWindow time.Duration) time.Duration {
	if dedupWindow > settleClearedFloor {
		return dedupWindow
	}
	return settleClearedFloor
}

// settleThreshold turns the flag's reading into the config's: on the flag 0
// is off and the default is the flag's own; in filterThresholds 0 is the
// default, as it is for every count, so off travels as a negative.
func settleThreshold(flagValue time.Duration) time.Duration {
	if flagValue == 0 {
		return -1
	}
	return flagValue
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
		if !sessionIDPattern.MatchString(f.targetSession) {
			return fmt.Errorf("--target-session %q is not a session id the daemon issues (%s)", f.targetSession, sessionIDPattern)
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
	if f.settle < 0 {
		return errors.New("--settle must be >= 0 (0 = off)")
	}
	if f.quietAfter < 0 {
		return errors.New("--quiet-after must be >= 0 (0 = off)")
	}
	if f.gatewayURL != "" && f.gatewayWait <= 0 {
		return errors.New("--gateway-wait must be > 0 when --gateway-url is set")
	}
	if strings.HasSuffix(f.gatewayURL, "/") {
		return fmt.Errorf("--gateway-url must not end with '/' (got %q)", f.gatewayURL)
	}
	// Checked here because a URL the probe cannot use fails every attempt the
	// way a gateway that is not up yet does, and would hold the informers for
	// the whole wait.
	if f.gatewayURL != "" {
		if u, err := url.Parse(f.gatewayURL); err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
			return fmt.Errorf("--gateway-url must be an http:// or https:// URL with a host (got %q)", f.gatewayURL)
		}
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
	// Autopilot says this is a GKE Autopilot cluster, read off the describe
	// call discovery already makes (clusterprofiles.Cluster.Autopilot). The
	// FailedScheduling gate is the only reader: an Autopilot cluster with no
	// user workloads scales itself to zero nodes, which looks from inside
	// exactly like a Standard cluster whose nodes have gone, and means the
	// opposite thing.
	//
	// The direct --in-cluster/--kubeconfig cluster has no cluster_identity of
	// its own and so no describe behind it. Where a profile covers that same
	// cluster — the ordinary install — buildWatchSet carries the bit over from
	// the profile it absorbs, so only an uncovered direct cluster reads false.
	// Uncovered and --in-cluster is fail-open and costs nothing: the agent's
	// own pod runs there, so the cluster always has a node and can never emit
	// the message the gate reads. Uncovered and --kubeconfig can be any
	// cluster at all, including an Autopilot one at zero nodes, where the hold
	// would then not apply; that combination is not a deployed path, since the
	// entrypoint passes --in-cluster with --profiles-dir.
	Autopilot bool
}

// identity names this cluster in logs and in the startup line, in the form
// that tells two clusters apart. A GKE cluster name is unique only within a
// project and location, so a fleet can legitimately run "prod" in us-central1
// and "prod" in europe-west1, and a line naming only "prod" says nothing about
// which one it means.
//
// Not the deduplication key, despite the matching format: profile clusters are
// deduplicated inside clusterprofiles.Discover, on Identity.String(), before
// they ever become a targetCluster. Changing the format here changes what the
// logs say and nothing else.
//
// ProjectID and Location are empty for the direct --in-cluster/--kubeconfig
// cluster, which has no cluster_identity to read; the bare name still
// distinguishes it from the profile clusters.
func (tc targetCluster) identity() string {
	return tc.ProjectID + "/" + tc.Location + "/" + tc.Name
}

// discovery is the profile scan discoverClusterProfiles runs. Its zero value
// reaches the real GKE API with this pod's own credentials, or through the
// credential proxy when clusterprofiles.BrokerFromEnv names one; the tests replace
// its Describe and TokenSource fields so the scan needs neither. OnSkip is set
// per call instead, because it needs the metrics registry the caller holds.
var discovery clusterprofiles.Discoverer

// discoverClusterProfiles scans a Hermes profiles directory (normally
// /opt/data/profiles) and returns one targetCluster per Cluster Agent profile
// found. clusterprofiles.Discoverer.Discover does the scanning and the
// addressing — read its doc comment for what counts as a cluster profile and
// which failures are fatal — and this function adds the two things that are the
// watcher's rather than the package's: a Kubernetes client per cluster, and
// what a skipped profile looks like in the logs and in clusterDiscoveryErrors.
//
// The package returns configs rather than clients because the drift detector
// wants a dynamic.Interface from the same scan. Building the informer-backed
// kubernetes.Interface here is what keeps that choice with the caller who makes
// it.
//
// A profile that looks like a cluster profile but fails to load is skipped, not
// fatal. Dropping one cluster is bad; the alternative is worse, because these
// errors would propagate out of buildWatchSet before it has even built the
// direct client, so a single unparseable config.yaml would stop the watcher
// monitoring anything at all — including the management cluster, whose client
// would have been fine. Every skip logs and increments
// clusterDiscoveryErrors{profile}, so "this cluster is not being watched" stays
// visible and alertable without being fatal.
func discoverClusterProfiles(ctx context.Context, dir string, m *metrics) ([]targetCluster, error) {
	skip := func(profile string, err error) {
		if profile == clusterprofiles.NoProfile {
			// The directory itself, not a profile in it — the error already
			// says which directory and what it means.
			log.Printf("k8s-event-watcher: %v", err)
		} else {
			log.Printf("k8s-event-watcher: skipping profile %s, its cluster will NOT be watched: %v", profile, err)
		}
		m.clusterDiscoveryErrors.WithLabelValues(profile).Inc()
	}

	// A copy, so setting OnSkip does not write to the package-level seam.
	d := discovery
	d.OnSkip = skip
	// The credential proxy, when the operator names one, in place of this
	// pod's Workload Identity; a half-set pair is fatal (BrokerFromEnv). A
	// Broker already on the seam is a test's and is kept.
	if d.Broker == nil {
		broker, err := clusterprofiles.BrokerFromEnv()
		if err != nil {
			return nil, err
		}
		d.Broker = broker
	}
	discovered, err := d.Discover(ctx, dir)
	if err != nil {
		return nil, err
	}

	clusters := make([]targetCluster, 0, len(discovered))
	for _, c := range discovered {
		client, err := kubernetes.NewForConfig(c.Config)
		if err != nil {
			skip(c.Profile, fmt.Errorf("kubernetes client: %w", err))
			continue
		}
		clusters = append(clusters, targetCluster{
			Name:      c.Identity.Cluster,
			ProjectID: c.Identity.Project,
			Location:  c.Identity.Location,
			Profile:   c.Profile,
			Client:    client,
			Autopilot: c.Autopilot,
		})
	}
	return clusters, nil
}

// initialSyncGrace is how long the process will run with nothing synced before
// giving up and letting its supervisor restart it. Generous on purpose: it has
// to cover a cold API server, a slow first list on a large cluster, and token
// minting, and the cost of being wrong is a restart loop. It is not a per
// cluster deadline — individual informers keep retrying indefinitely, and one
// cluster syncing is enough to satisfy it.
const initialSyncGrace = 2 * time.Minute

// dispatcher coordinates the filter, deduplication, HTTP injector, and metrics for streamed events.
// One dispatcher is built per watched cluster, each owning that cluster's dedup
// cache and memos; filter, injector, and metrics are shared across all of them.
// An event's payload reads its cluster off the TriageEvent; the dispatcher also
// keeps the cluster (set by main) for the sweep, which has no event in hand.
//
// Dispatch holds no dispatcher-wide lock, and does not need one. Each
// cluster's watcher delivers from a single goroutine of its own (see Run in
// watcher.go), so Dispatch is only ever entered by its own cluster's watcher,
// one event at a time; the sweep (runSweepLoop) is the one other goroutine
// that enters a dispatcher, and everything the two share — the dedup cache,
// the memos, the metrics — carries its own lock. Across clusters the
// dispatchers share nothing mutable. A lock here would have served only to make
// one cluster's slow daemon round-trip stall every other cluster.
type dispatcher struct {
	filter *filter
	dedup  *dedupCache
	// pullClasses carries the cause of an image-pull failure from the event that
	// names it to the causeless back-off event that follows. Per-cluster like
	// dedup, so one cluster churning through pods cannot evict another's entries
	// out of the shared bound.
	pullClasses *pullClassMemo
	// scaleUps carries cluster-autoscaler's verdict on a pod from the event that
	// records it to the FailedScheduling events it qualifies. Per-cluster for
	// the same reason; pod UIDs are unique across clusters regardless.
	scaleUps  *scaleUpMemo
	settles   *settleMemo
	injector  *injector
	metrics   *metrics
	mode      string // "per-incident" or "shared"
	targetSid string // for shared mode
	dryRun    bool
	// quietAfter is --quiet-after; zero sends no follow-ups. cluster labels
	// the sweep's log lines and metrics, since no event is in hand there.
	quietAfter time.Duration
	cluster    targetCluster
	// now is the sweep's clock, overridable in tests.
	now func() time.Time
	// readyAt is the watcher's clock at which the cluster's informer last
	// became delivering (main's onWatching: set once the first list has
	// completed and again when a watch returns after a failure, zero while a
	// 403 holds the watch or any other list or watch error stands). While it
	// is zero, and for followupResumeGrace after it is set, the sweep sends
	// no follow-up: an entry's LastSeen moves only on a sighting the informer
	// delivers, so a watcher that is not listening would call every open
	// incident quiet, and a watch that has just returned re-delivers every
	// live entry's events in the relist that follows, which the grace lets
	// land first.
	readyAt atomic.Int64
	// lastDelivery is the watcher's clock at the last event the informer
	// delivered to this dispatcher, any event, admitted or not. The 403 hold
	// is the one outage the watcher reports; a control-plane outage, a bad
	// credential or a partition keeps the reflector in its own backoff and
	// says nothing, so the sweep also wants to have seen the cluster speak
	// inside the quiet period before it calls anything on it quiet.
	lastDelivery atomic.Int64
	// noFollowupLogged keeps the "daemon serves no follow-ups" line to one
	// per change of answer.
	noFollowupLogged atomic.Bool
}

func (d *dispatcher) clock() time.Time {
	if d.now != nil {
		return d.now()
	}
	return time.Now()
}

// newDispatcher builds a dispatcher around one cluster's dedup cache. filter,
// injector, and metrics are shared across every cluster — they are stateless
// or goroutine-safe — while dedup and the two memos are per-cluster. The
// scale-up memo lives as long as the dedup window, and at least as long as a
// mark can still be consulted (scaleUpMemoTTL): past that the pod's next
// FailedScheduling is a new incident, and a verdict that old no longer
// describes a scale-up anyone is waiting on. The hold is read off the
// filter's config rather than the flag so the default applies when the flag
// was left at zero.
func newDispatcher(f *flags, filter *filter, dedup *dedupCache, inj *injector, m *metrics) *dispatcher {
	return &dispatcher{
		filter:      filter,
		dedup:       dedup,
		pullClasses: newPullClassMemo(defaultPullClassTTL, defaultPullClassEntries),
		scaleUps:    newScaleUpMemo(scaleUpMemoTTL(f.dedupWindow, filter.cfg.scaleUpHold), defaultScaleUpEntries),
		settles:     newSettleMemo(defaultSettleEntries, settleClearedTTL(f.dedupWindow)),
		injector:    inj,
		metrics:     m,
		mode:        f.mode,
		targetSid:   f.targetSession,
		dryRun:      f.dryRun,
		quietAfter:  f.quietAfter,
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

// RecordScaleUpMark puts a cluster-autoscaler verdict on record and nothing
// else: no metric, no log line, no dedup entry. The watcher calls it for the
// marks a list or watch-list stream carries before the informer delivers
// that batch (watcher.go, recordMark), and the same event comes through
// Dispatch when the batch is delivered, where it is counted and logged once.
// The filter decides admission here as it does there, so the --reason list
// and the namespace rules still say which verdicts are remembered, and the
// mark is filed under the event's own namespace with the UID, so it is found
// only by a FailedScheduling on a pod in that namespace (scaleup.go).
func (d *dispatcher) RecordScaleUpMark(ev TriageEvent) bool {
	d.lastDelivery.Store(d.clock().UnixNano())
	if d.filter.Decide(ev) != gateScaleUpMark {
		return false
	}
	d.scaleUps.Record(ev.Namespace, ev.Key.UID, scaleUpVerdictFor(ev.Key.Reason), ev.LastSeen)
	return true
}

// Dispatch is the entry point that runs an event through filtering, deduplication, and HTTP injection.
func (d *dispatcher) Dispatch(ctx context.Context, ev TriageEvent) {
	d.metrics.eventsSeen.WithLabelValues(ev.Cluster, ev.Project, ev.Location, ev.Key.Reason).Inc()
	d.lastDelivery.Store(d.clock().UnixNano())
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
	// Stamped before the filter for the same reason PullClass is: the gate
	// that reads the verdict runs inside Decide, and the verdict arrived on a
	// different event. Only FailedScheduling carries it; the marks themselves
	// are recorded below, once the filter has admitted them, so the --reason
	// list and the namespace rules decide which verdicts are remembered. The
	// memo is keyed on the namespace with the UID, so a mark written in one
	// namespace is not found by a pod in another whose UID it named.
	if ev.Key.Reason == reasonFailedScheduling {
		ev.ScaleUp = d.scaleUps.Lookup(ev.Namespace, ev.Key.UID)
	}
	canonical := canonicalizeReason(ev.Key.Reason, ev.Message)
	// A repeat blip on an event object kubelet kept from an earlier one
	// carries that one's first sighting; the memo knows where this blip
	// began, and the gate measures the span from there (settleMemo.BlipStart).
	if settleApplies(canonical) {
		if start, ok := d.settles.BlipStart(ev, canonical, d.clock()); ok && start.After(ev.FirstSeen) {
			ev.FirstSeen = start
		}
	}
	if gate := d.filter.Decide(ev); gate != gateAccepted {
		if gate == gateSettle {
			d.settles.Hold(ev, canonical, d.clock())
			log.Printf("held %s pod=%s/%s (count=%d, lasted %s of the %s settle period); not forwarded yet",
				ev.Key.Reason, ev.Namespace, ev.Name, ev.Count, ev.LastSeen.Sub(ev.FirstSeen).Round(time.Second), d.filter.cfg.settle)
		}
		// A settle-family sighting the count gate stopped is a blip the memo
		// has to remember too: one that clears at two sightings and repeats
		// inside the event's TTL would otherwise arrive at count three with
		// the object's old first sighting and no tombstone to measure from.
		if (gate == gateBackoffMinCount || gate == gateUnhealthyMinCount) && d.filter.cfg.settle > 0 && settleApplies(canonical) {
			d.settles.Hold(ev, canonical, d.clock())
		}
		if gate == gateScaleUpMark {
			verdict := scaleUpVerdictFor(ev.Key.Reason)
			d.scaleUps.Record(ev.Namespace, ev.Key.UID, verdict, ev.LastSeen)
			log.Printf("recorded %s pod=%s/%s as scale-up %s (%s); not forwarded",
				ev.Key.Reason, ev.Namespace, ev.Name, verdict, ev.Message)
		}
		if gate == gateScaleUpMarkReporter {
			log.Printf("ignored %s pod=%s/%s reported by %q, not %s; no verdict recorded, not forwarded",
				ev.Key.Reason, ev.Namespace, ev.Name, ev.Reporter, scaleUpReporter)
		}
		// A sighting a gate stopped is still a sighting: an alerted object
		// the scale-up hold keeps from Observe is not quiet.
		d.dedup.Sighted(ev.Key, ev.Message, ev.LastSeen)
		d.metrics.eventsFiltered.WithLabelValues(ev.Cluster, ev.Project, ev.Location, string(gate)).Inc()
		return
	}
	if d.settles.Release(ev, canonical) {
		d.metrics.settleOutcomes.WithLabelValues(ev.Cluster, ev.Project, ev.Location, canonical, settleOutcomeForwarded).Inc()
		log.Printf("settled %s pod=%s/%s (count=%d, lasted %s); forwarding",
			ev.Key.Reason, ev.Namespace, ev.Name, ev.Count, ev.LastSeen.Sub(ev.FirstSeen).Round(time.Second))
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
	// opened says this dispatch created the session, which is what
	// triageSessions counts once the inject below is delivered.
	sid := d.targetSid
	opened := false
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
		opened = true
		d.metrics.sessionCreates.WithLabelValues(ev.Cluster, ev.Project, ev.Location, "ok").Inc()
		d.dedup.BindSession(ev.Key, ev.Message, sid)
		d.dedup.BindObject(ev.Key, ev.Message, ev.Namespace, ev.KindOfObject, ev.Name, ev.FirstSeen, ev.Count)
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
	answer, err := d.injector.InjectDetailed(ctx, sid, payload)
	status := answer.Status
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
	if status == injectStatusFolded {
		// 2xx, and the daemon recorded it against an incident already
		// admitted for this workload. Kept rather than rolled back, and not
		// flagged: see injectStatusFolded.
		d.metrics.eventsFolded.WithLabelValues(ev.Cluster, ev.Project, ev.Location, ev.Key.Reason, ev.Namespace).Inc()
		log.Printf("folded %s pod=%s/%s (sid=%s) — the workload's incident is already open",
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
	if answer.Supports(injectFeatureFollowup) {
		d.dedup.MarkFollowupSupported(ev.Key, ev.Message)
	}
	if opened {
		d.metrics.triageSessions.WithLabelValues(ev.Cluster, ev.Project, ev.Location).Inc()
	}
	log.Printf("fire %s pod=%s/%s → sid=%s (mode=%s)",
		ev.Key.Reason, ev.Namespace, ev.Name, sid, d.mode)
}

// Sweep ages the settle holds and sends the quiet follow-ups, as of now. It
// runs every sweepInterval per cluster (runSweepLoop) and is safe to call
// from tests with a fixed clock.
//
// A held key unsighted for the settle period plus settleQuietSlack cleared on
// its own: counted self_resolved and forgotten, which is the one reading of
// the gate's suppression rate the counter gives. An incident whose alert
// went out and whose object has been unsighted for quietAfter gets one
// k8s-event-followup inject on its own session, which the daemon posts into
// the alert's thread (session_kv_server, _inject_followup). What the watcher
// knows is silence, and the payload says so: the object was not read back,
// since the broker's watcher route serves no pods, and a deleted pod and a
// recovered one both go quiet. One per incident, held on the dedup entry so
// it survives a restart with the entry; an inject that fails is retried on
// the next sweep. Dry-run prints the payload as Dispatch does.
func (d *dispatcher) Sweep(ctx context.Context) {
	now := d.clock()
	quiet := d.filter.cfg.settle + settleQuietSlack
	for _, h := range d.settles.Expire(now, quiet) {
		d.metrics.settleOutcomes.WithLabelValues(h.Cluster, h.Project, h.Location, h.Reason, settleOutcomeSelfResolved).Inc()
	}
	if d.quietAfter <= 0 || d.mode != "per-incident" {
		return
	}
	readyAt := d.readyAt.Load()
	if readyAt == 0 || now.Sub(time.Unix(0, readyAt)) < followupResumeGrace {
		return
	}
	// The cluster has to have spoken inside the quiet period: a watch down
	// for any reason the watcher does not report keeps LastSeen from moving
	// just as a quiet object does, and the two cannot be told apart.
	if last := d.lastDelivery.Load(); last == 0 || now.Sub(time.Unix(0, last)) >= d.quietAfter {
		return
	}
	candidates := d.dedup.QuietIncidents(now, d.quietAfter)
	if len(candidates) == 0 {
		return
	}
	if d.dryRun || d.injector == nil {
		// Dry-run builds no injector. A candidate reaches it only through a
		// snapshot a live run wrote; print it as Dispatch prints payloads.
		for _, q := range candidates {
			out, _ := json.MarshalIndent(d.followupPayload(q), "", "  ")
			fmt.Printf("--- dry-run follow-up for session %q ---\n%s\n", q.SessionID, string(out))
			d.dedup.MarkQuietNotified(q.Key)
		}
		return
	}
	// The daemon of this moment, not the one that answered the alert: an
	// agent image rolled back to a build without the follow-up branch would
	// take the kind down the event path and post a second alert.
	features, err := d.injector.Features(ctx)
	if err != nil {
		log.Printf("dispatcher: follow-ups held, daemon health unreadable: %v", err)
		return
	}
	if !hasFeature(features, injectFeatureFollowup) {
		if !d.noFollowupLogged.Swap(true) {
			log.Printf("dispatcher: follow-ups held, the daemon lists no %q under %s (an older session server); they go out once it does", injectFeatureFollowup, injectFeaturesKey)
		}
		return
	}
	d.noFollowupLogged.Store(false)
	for _, q := range candidates {
		answer, err := d.injector.InjectDetailed(ctx, q.SessionID, d.followupPayload(q))
		if err != nil {
			d.metrics.followups.WithLabelValues(d.cluster.Name, d.cluster.ProjectID, d.cluster.Location, q.Key.Reason, followupOutcomeError).Inc()
			log.Printf("dispatcher: follow-up for %s/%s (sid=%s): %v", q.Namespace, q.Name, q.SessionID, err)
			continue
		}
		// Marked, then written out at once: the snapshot loop's next tick is
		// up to 30s away, and a process killed before it would restore the
		// entry unmarked and post the line a second time.
		d.dedup.MarkQuietNotified(q.Key)
		if err := d.dedup.Snapshot(); err != nil {
			log.Printf("dedup snapshot after follow-up: %v", err)
		}
		outcome := followupOutcomeSent
		if answer.Status == injectStatusUnrouted {
			// The daemon had no thread for the session (the alert never
			// reached chat), so it posted nothing; there is nothing to retry.
			outcome = followupOutcomeUnrouted
		}
		d.metrics.followups.WithLabelValues(d.cluster.Name, d.cluster.ProjectID, d.cluster.Location, q.Key.Reason, outcome).Inc()
		log.Printf("followup %s pod=%s/%s → sid=%s %s (quiet since %s, %s ago)",
			q.Key.Reason, q.Namespace, q.Name, q.SessionID, outcome, q.LastSeen.UTC().Format(time.RFC3339), now.Sub(q.LastSeen).Round(time.Second))
	}
}

// followupPayload is the k8s-event-followup inject for a quiet incident: the
// object and the alerted event's own first sighting and count, the last real
// sighting, and the dispatcher's cluster.
func (d *dispatcher) followupPayload(q quietIncident) InjectPayload {
	return InjectPayload{
		Kind:         injectKindFollowup,
		Reason:       q.Key.Reason,
		Namespace:    q.Namespace,
		KindOfObject: q.Kind,
		Name:         q.Name,
		UID:          q.Key.UID,
		Message:      followupMessage,
		Count:        q.Count,
		FirstSeen:    q.FirstSeen,
		LastSeen:     q.LastSeen,
		Cluster:      d.cluster.Name,
		Project:      d.cluster.ProjectID,
		Location:     d.cluster.Location,
		Type:         eventTypeWarning,
	}
}

// runSweepLoop calls Sweep every interval until ctx ends.
func runSweepLoop(ctx context.Context, d *dispatcher, interval time.Duration) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			d.Sweep(ctx)
		}
	}
}

// deriveMemoryLimit decides the Go soft memory limit from the two variables
// that can set it. It returns the limit in bytes and true when
// memoryLimitEnv should be applied, or zero, false and the reason when the
// runtime's own setting should stand: GOMEMLIMIT already set (the runtime
// applied it and it wins), the container limit absent (running outside the
// operator's Deployment), or unparseable or non-positive (the value is not a
// byte count, so nothing is derived from it).
func deriveMemoryLimit(goMemLimit, containerLimit string) (int64, bool, string) {
	if goMemLimit != "" {
		return 0, false, fmt.Sprintf("%s=%s is set and takes precedence", goMemLimitEnv, goMemLimit)
	}
	if containerLimit == "" {
		return 0, false, fmt.Sprintf("%s is not set; the runtime default applies", memoryLimitEnv)
	}
	limitBytes, err := strconv.ParseInt(containerLimit, 10, 64)
	if err != nil || limitBytes <= 0 {
		return 0, false, fmt.Sprintf("%s=%q is not a positive byte count; the runtime default applies", memoryLimitEnv, containerLimit)
	}
	return int64(float64(limitBytes) * memoryLimitFraction), true, ""
}

// applyMemoryLimit sets the Go runtime's soft memory limit to
// memoryLimitFraction of the container's, when the operator has told the
// watcher what that is and nothing else has set a limit. debug.SetMemoryLimit
// is the same knob GOMEMLIMIT turns; doing it here rather than in the
// entrypoint keeps the derivation under test. One line either way, so the
// startup log says which limit the process is running under.
func applyMemoryLimit() {
	limitBytes, apply, reason := deriveMemoryLimit(os.Getenv(goMemLimitEnv), os.Getenv(memoryLimitEnv))
	if !apply {
		log.Printf("k8s-event-watcher: memory limit not derived: %s", reason)
		return
	}
	debug.SetMemoryLimit(limitBytes)
	log.Printf("k8s-event-watcher: Go soft memory limit set to %d bytes (%s=%s × %g)",
		limitBytes, memoryLimitEnv, os.Getenv(memoryLimitEnv), memoryLimitFraction)
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

	applyMemoryLimit()

	// Resolve bearer token from env (unless dry-run).
	var token string
	if !f.dryRun {
		token = os.Getenv(f.tokenEnv)
		if token == "" {
			return fmt.Errorf("bearer token env var %s is empty", f.tokenEnv)
		}
	}

	// Build components.
	filterCfg := newFilterConfig(splitCSV(f.reasons), splitCSV(f.namespaces), splitCSV(f.excludeNamespaces), f.filterThresholds())
	filter := newFilter(filterCfg)

	m := newMetrics()
	if f.dataVolume != "" {
		m.registry.MustRegister(newDataVolumeCollector(f.dataVolume, dataVolumeStatfs))
	}

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

	// A metrics listener that cannot bind costs the watcher its /metrics, not
	// its job: the events it exists to triage are still watched. Exiting here
	// would hand the process to the entrypoint's supervisor, which retries with
	// backoff and after three short exits reports that NO cluster events are
	// being watched — true of a port conflict only if this line makes it so.
	// The ALERT line is the signal instead: it names the consequence and stays
	// greppable beside the supervisor's own. startMetrics returns a nil server
	// on failure, which Run treats as "no listener".
	metricsSrv, err := startMetrics(f.metricsAddr, m)
	if err != nil {
		log.Printf("k8s-event-watcher: ALERT %v — running without a /metrics listener; cluster events are still watched", err)
	}
	if metricsSrv != nil {
		// The bound address, so a scrape target that is down can be read
		// against what the process opened: the entrypoint forwards whatever
		// port it was given, and a port other than the declared one binds fine.
		log.Printf("k8s-event-watcher: /metrics listening on %s", metricsSrv.ln.Addr())
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

	// After buildWatchSet, so a watch set that cannot be built still exits
	// at once and counts as a failed start to the entrypoint's supervisor;
	// before any dedup cache, so nothing is restored or snapshotted while
	// the process may yet be a standby. cluster_up is published at 0 first,
	// so the wait reads as "registered, not yet watching" rather than as no
	// watcher at all.
	if f.gatewayURL != "" && !f.dryRun {
		for _, tc := range clusters {
			m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(0)
		}
		waitForGateway(ctx, f.gatewayURL, f.gatewayWait, gatewayProbeInterval)
		if ctx.Err() != nil {
			return nil
		}
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
		clusterDisp.cluster = tc
		if f.dedupPersist != "" && f.snapshotInterval > 0 {
			go runSnapshotLoop(ctx, cache, f.snapshotInterval)
		}
		go runSweepLoop(ctx, clusterDisp, sweepInterval)
		wg.Add(1)
		go func(tc targetCluster, disp *dispatcher) {
			defer wg.Done()
			w := newWatcher(tc.Client, disp, tc)
			log.Printf("k8s-event-watcher: [%s] starting: preflight, then informer (source=%s project=%s location=%s)",
				tc.Name, tc.Profile, tc.ProjectID, tc.Location)
			// Starts at 0 and only reaches 1 once the initial list completes.
			// Setting it before Run would have reported every cluster up the
			// instant its goroutine started, including ones whose control plane
			// was unreachable — those block inside Run forever rather than
			// failing, so liveness of the goroutine proves nothing.
			m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(0)
			defer m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(0)
			// The gauge follows every transition the watcher reports: 1 on the
			// initial sync and again when a held cluster recovers, 0 while a
			// 403 holds it. Only the first 1 counts toward synced — a recovery
			// is the same cluster coming back, not another one syncing — and
			// the guard is atomic because the watcher calls in from its own
			// goroutines.
			var counted atomic.Bool
			onWatching := func(watching bool) {
				if !watching {
					m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(0)
					// Blind again: nothing it does not see can be called quiet.
					disp.readyAt.Store(0)
					log.Printf("k8s-event-watcher: [%s] events are not being delivered (the watch was refused or failed), no longer watching until the next successful attempt", tc.Name)
					return
				}
				m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(1)
				disp.readyAt.Store(disp.clock().UnixNano())
				if !counted.Swap(true) {
					synced.Add(1)
					log.Printf("k8s-event-watcher: [%s] informer synced, now watching", tc.Name)
					return
				}
				log.Printf("k8s-event-watcher: [%s] events permitted again, watching resumed", tc.Name)
			}
			if err := w.Run(ctx, onWatching); err != nil {
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
		return fmt.Errorf("no cluster synced within %s; %d cluster(s) started but none completed an initial list (a cluster denied at preflight never attempts one; see its preflight line)", initialSyncGrace, started)
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
// also carries a credential that can be refused: it authenticates as a GSA
// (the broker's, through the credential proxy, or the pod's without one), so a
// permission set without roles/container.viewer, or master authorized networks
// that exclude that identity's egress, denies it. The direct entry uses the
// pod's own service account against kubernetes.default.svc and never leaves the
// cluster.
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
			direct := targetCluster{Name: f.clusterName, Profile: directProfile}
			if len(candidates) == 1 {
				covered := candidates[0]
				clusters = removeProfile(clusters, covered.Profile)
				// Autopilot travels with the identity: the absorbed profile's
				// describe call already answered it, and dropping it here
				// would leave the one cluster the watcher knows most about
				// reading as Standard.
				direct.ProjectID, direct.Location = covered.ProjectID, covered.Location
				direct.Autopilot = covered.Autopilot
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
