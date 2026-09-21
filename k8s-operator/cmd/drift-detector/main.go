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

// Command drift-detector reads GKE audit records from the Pub/Sub subscription
// the terraform/modules/drift-pubsub module provisions, and turns out-of-band
// cluster changes into gitops-drift injects on the AutoOps pipeline.
//
// Kubernetes audit is not served by the Kubernetes API on GKE: the control
// plane is managed, so the audit stream surfaces only in Cloud Logging. A Log
// Router sink exports it to a topic, and this binary pulls the subscription.
//
// This is CUJ 3, built in four stages, all of which ship here. T1 is the
// ingestion path (pull and parse), T2 is principal classification (assign a
// tier, drop the calls that changed nothing), and T3 joins managedFields off
// the live object -- on the cluster this process holds credentials for, and on
// every cluster in the project that has a Cluster Agent profile under
// --profiles-dir. A record naming a cluster in neither is still forwarded, and
// counted unreachable. With --daemon-url set, what survives is posted to the
// core-agent daemon as a gitops-drift inject, which is where the pipeline the
// design describes takes over: session, agent, chat, human approval, GitOps PR.
//
// The inject is off unless --daemon-url is set, and off is the default. No
// image builds or launches this binary yet, so reaching that pipeline is
// something an operator does by hand today. See docs/designs/drift-detection.md
// for the design.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

const (
	// commandName prefixes every log line and names the flag set, matching the
	// event watcher's convention.
	commandName = "drift-detector"

	// defaultSubscriptionName is the subscription drift-pubsub creates. Kept in
	// step with that module's subscription_name variable; overriding one
	// without the other is the likeliest reason this binary finds nothing.
	defaultSubscriptionName = "platform-agent-drift-audit-sub"

	// maxMessagesCeiling is the largest batch the Pub/Sub pull API accepts. A
	// larger request is rejected on every pull, so rejecting it at startup
	// turns a loop that backs off forever into one line at launch.
	maxMessagesCeiling = 1000

	// exitFailure is the status returned when realMain reports an error.
	exitFailure = 1

	// projectNumberDigits is the character set a project number is made of, and
	// the whole of the test for one: a GCP project ID must start with a
	// lowercase letter, so a value that is nothing but digits cannot be an ID.
	projectNumberDigits = "0123456789"

	// absorbedProfileSeparator separates profile names in the startup line that
	// reports which profiles the direct credentials already cover.
	absorbedProfileSeparator = ", "
)

// looksLikeProjectNumber reports whether --project was given as a project
// number rather than a project ID.
func looksLikeProjectNumber(project string) bool {
	return project != "" && strings.TrimLeft(project, projectNumberDigits) == ""
}

// flags holds the parsed command line.
type flags struct {
	project      string
	subscription string
	maxMessages  int64

	// automationPrincipals and humanDomains configure the tier classifier.
	// They are flags rather than a mounted config file because that is how
	// deployment configuration already reaches a sibling adapter: the
	// PlatformAgent CR sets an environment variable, deploy/shared/start-
	// services.sh turns it into a flag, exactly as EVENT_WATCHER_* does for
	// k8s-event-watcher. Both satisfy T2's requirement that the allowlist
	// change without a rebuild; only one of them matches the deployment path
	// the operator already reconciles.
	automationPrincipals string
	humanDomains         string

	// logDropped turns on a log line per filtered record.
	logDropped bool

	// daemonURL is the core-agent daemon the gitops-drift inject is posted to.
	// Empty is a supported mode and the default: the detector classifies and
	// joins exactly as before and stops at the DRIFT log line, which is how it
	// has run since T1 and is what a local run against a real subscription
	// wants. Setting it is what escalates drift to a human.
	daemonURL string

	// tokenEnv names the environment variable holding the daemon's bearer
	// token, rather than carrying the token itself: a flag value is visible in
	// the process table and in any log that echoes argv, and this binary's
	// startup line prints its configuration. Required once daemonURL is set.
	tokenEnv string

	// owner is the X-Asserted-Caller the session is attributed to. The daemon
	// does not read that header today: POST /sessions is guarded by the bearer
	// token alone and stamps its own metadata. It is sent so the value is on
	// the wire and in the daemon's request log from the first release, which is
	// what makes turning it into an authorisation check later a daemon-side
	// change rather than a flag day across both binaries.
	owner string

	// kubeconfig and inCluster select how the join reaches the live object.
	// Neither set means no cluster access and no join, which is how the binary
	// has run since T1.
	kubeconfig string
	inCluster  bool

	// clusterName and clusterLocation, with project above, name the GKE cluster
	// the credentials reach. Required alongside them, because a record names its
	// own cluster and the join has to refuse the ones it cannot serve: looking
	// up "prod/deployments/api" on the wrong cluster does not fail, it silently
	// reads a different object.
	//
	// The location is as load-bearing as the name. A GKE cluster name is unique
	// within a project and location, so a fleet with "prod" in two regions is
	// ordinary, and a name-only match would enrich one region's records from the
	// other's cluster.
	clusterName     string
	clusterLocation string

	// profilesDir is the Hermes profiles directory the Platform Agent writes a
	// Cluster Agent profile into per cluster it has onboarded. It is the join's
	// second credential source and the one that scales: the flags above name one
	// cluster, this one names however many the fleet has, without a redeploy when
	// the next is onboarded.
	//
	// Additive with them rather than an alternative, and both may be set. Empty
	// means the fan-in is off and the join covers the direct cluster alone.
	profilesDir string

	// gitopsManagers names the field managers that are the GitOps controller.
	// Empty means the detector reports ownership without claiming any of it is
	// a reconcile.
	gitopsManagers string

	// batchJoinBudget caps how long one batch may spend on its lookups. A flag
	// because the ack deadline it has to fit inside lives in Terraform, not
	// here: the default is sized against the drift-pubsub module's 60 seconds,
	// and a subscription created outside the module carries Pub/Sub's own 10.
	batchJoinBudget time.Duration
}

// parseFlags reads argv, leaving validation to realMain so that a usage error
// and a configuration error are reported the same way.
func parseFlags(args []string) (*flags, error) {
	fs := flag.NewFlagSet(commandName, flag.ContinueOnError)
	f := &flags{}

	fs.StringVar(&f.project, "project", "",
		"GCP project holding the drift audit subscription. Required. With the join on this must be the project ID and not the project number: a Pub/Sub path accepts either, but the join matches this against each record's project_id, so a number would match nothing.")
	fs.StringVar(&f.subscription, "subscription", defaultSubscriptionName, "Pub/Sub subscription to pull audit records from.")
	fs.Int64Var(&f.maxMessages, "max-messages", defaultMaxMessages, "Messages requested per pull.")
	fs.StringVar(&f.automationPrincipals, "automation-principals", "",
		"Comma-separated principals to classify as automation rather than human, in addition to every *"+gcpServiceAccountSuffix+" service account. Applies to every cluster the subscription carries; changes without a rebuild.")
	fs.StringVar(&f.humanDomains, "human-domains", "",
		"Comma-separated domains whose accounts count as human. Empty means any principal carrying a domain.")
	fs.BoolVar(&f.logDropped, "log-dropped", false,
		"Log every filtered record. Verbose: the drop rate exceeds 99% on a live cluster.")
	fs.StringVar(&f.daemonURL, "daemon-url", "",
		"Base URL of the core-agent daemon (http://... or https://..., no trailing slash) to post the "+injectKindDrift+" inject to. Empty disables the inject: records are still classified, joined and logged, and nothing is escalated to a human.")
	fs.StringVar(&f.tokenEnv, "token-env", "",
		"Name of the environment variable holding the daemon's bearer token. Required with --daemon-url. The variable's name rather than its value, because a flag is visible in the process table.")
	fs.StringVar(&f.owner, "owner", "",
		"X-Asserted-Caller for the session the inject opens. Sent and logged, but not authorised against anything today: the daemon guards POST /sessions with the bearer token alone.")
	fs.StringVar(&f.kubeconfig, "kubeconfig", "",
		"Path to a kubeconfig for the cluster whose live objects the join reads. An operator-supplied path for local runs, not a discovery mechanism. Mutually exclusive with --in-cluster; with no --profiles-dir either, the join is disabled.")
	fs.BoolVar(&f.inCluster, "in-cluster", false,
		"Read live objects using the Pod's own ServiceAccount. Mutually exclusive with --kubeconfig; with no --profiles-dir either, the join is disabled.")
	fs.StringVar(&f.clusterName, "cluster-name", "",
		"GKE cluster name the join's credentials reach. Required with --kubeconfig or --in-cluster; records from any cluster neither these credentials nor a --profiles-dir profile covers are counted unreachable rather than looked up on the wrong one. Checked at startup against the cluster those credentials actually reach, and a disagreement stops the process.")
	fs.StringVar(&f.clusterLocation, "cluster-location", "",
		"GKE location (region or zone) of --cluster-name. Required with it: a cluster name is unique only within a project and location, so without this a same-named cluster elsewhere would be read as this one.")
	fs.StringVar(&f.profilesDir, "profiles-dir", "",
		"Hermes profiles directory (normally /opt/data/profiles). Enables multi-cluster fan-in: every Cluster Agent profile whose cluster is in --project becomes a joinable cluster, addressed by asking the GKE API about that profile's cluster_identity. Combines with --in-cluster / --kubeconfig, which add the directly-reachable cluster on top and win if a profile names the same one.")
	fs.StringVar(&f.gitopsManagers, "gitops-managers", "",
		"Comma-separated managedFields managers that are the GitOps controller (for example argocd-controller). Matched exactly, and only on writes to the object in a second later than the audited change: a claim made through a subresource such as status does not count, and neither does one sharing the change's own second, which a person applying under the manager's name would produce. Empty means ownership is reported without any reconciliation claim.")
	fs.DurationVar(&f.batchJoinBudget, "batch-join-budget", defaultBatchJoinBudget,
		"Longest one batch may spend on live-object lookups before the rest fail open. Keep it to half the subscription's ack deadline or less, leaving the rest for the batch's Ack; startup reads the real deadline and warns when it does not. Exceeding the whole deadline means Pub/Sub redelivers the batch this process is still working on.")

	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	return f, nil
}

// newFilterFromFlags builds the classifier, the join, and the filter the run
// loop drives, returning the join separately so the shutdown report can read
// its counts.
//
// It is separate from realMain so that the wiring is reachable from a test:
// NewClassifier takes two strings, so transposing them is invisible to the
// compiler and to go vet, and the result is a detector that treats the
// allowlist as a domain list and classifies the whole stream wrongly while
// every test that constructs a Classifier directly still passes.
//
// The cluster set is built by the caller rather than here, because discovering
// the profile half of it needs a context and reaches the GKE API, and this has
// to stay callable from a test that stands up neither. onDrift arrives the same
// way and for the same reason: building it needs a validated token out of the
// environment, which a test should not have to set to exercise the wiring.
func newFilterFromFlags(f *flags, clusters map[clusterIdentity]objectGetter, onDrift driftEventHandler) (*driftFilter, *joiner) {
	join := newJoiner(clusters, parseGitopsManagers(f.gitopsManagers), onDrift)
	return newDriftFilter(NewClassifier(f.automationPrincipals, f.humanDomains), join.Handle, f.logDropped), join
}

// directClusterIdentity names the cluster --in-cluster or --kubeconfig reaches.
//
// The project is --project rather than a flag of its own: the subscription is a
// project-level sink, so every cluster it carries records for is in that
// project. A subscription pointed at another project's sink therefore matches
// nothing and reports every record unreachable, which is the direction to fail
// in -- the alternative is enriching one project's records from another's
// clusters.
//
// A function rather than a literal at the one call site because the three
// fields are adjacent strings assembled from three flags, which is the
// transposition clusterIdentity's doc comment describes: --cluster-location and
// --cluster-name swapped compiles, passes go vet, and matches no record at all.
// Named here, it is assertable.
func directClusterIdentity(f *flags) clusterIdentity {
	return clusterIdentity{
		Project:  f.project,
		Location: f.clusterLocation,
		Cluster:  f.clusterName,
	}
}

// joinDisabledReason says why the live-object join has no clusters, in the one
// line an operator gets. Four ways to reach nought clusters, and they call for
// different action, so they are not reported alike: naming --profiles-dir as
// un-set to someone who set it sends them to check the one thing that is
// already right, and calling a directory empty when every profile in it failed
// sends them to cluster-agent-reconcile when the cause is IAM, a mistyped
// --project, or a GKE API that was down for the seconds this process spent
// starting. The unreadable directory is the same mistake once more: it reaches
// this function looking exactly like an empty one, no clusters and nothing
// skipped, and only profileScan.DirUnreadable tells the two apart.
//
// It matters more than a log line usually would: discovery runs once, nothing
// here retries and nothing exits, so this sentence is the whole account of why
// the fan-in is off for the life of the pod.
//
// A function rather than a switch at the call site so the four branches can be
// asserted without standing up a subscription.
func joinDisabledReason(profilesDir string, scan profileScan) string {
	switch {
	case scan.DirUnreadable:
		// Before the skip count rather than after, though the two cannot both
		// be set today -- nothing is skipped per-profile until the directory has
		// been read. Ordered on which would matter more if that changed: a
		// directory nobody could open is the cause, and stragglers inside it
		// would be a consequence.
		//
		// The error itself was logged as it happened and is not repeated here;
		// what this adds is that it accounts for the whole fan-in being off,
		// which the skip line on its own does not say.
		return fmt.Sprintf("the Cluster Agent profiles in %s could not be read at all (the error is above, and a restart will not clear it), and no --in-cluster or --kubeconfig", profilesDir)
	case scan.Skipped > 0:
		// Reached only with a profiles dir: discoverProfileClusters returns
		// before scanning when it is unset, so Skipped cannot be positive here
		// without one to name.
		return fmt.Sprintf("all %d Cluster Agent profile(s) in %s were skipped for the reasons above, and no --in-cluster or --kubeconfig", scan.Skipped, profilesDir)
	case profilesDir != "":
		// Normal before a fresh install's first cluster-agent-reconcile tick,
		// which is why this degrades rather than refusing to start: the detector
		// still parses, classifies and forwards, and only the ownership is
		// missing. The watcher exits here instead because with no clusters it
		// has nothing left to do at all.
		return fmt.Sprintf("no Cluster Agent profiles in %s yet, and no --in-cluster or --kubeconfig", profilesDir)
	default:
		return "no cluster credentials (--in-cluster, --kubeconfig or --profiles-dir)"
	}
}

func main() {
	if err := realMain(os.Args[1:]); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return
		}
		log.Printf("%s: %v", commandName, err)
		os.Exit(exitFailure)
	}
}

// realMain is separated from main so the startup path is testable: everything
// except the process exit happens here.
func realMain(argv []string) error {
	f, err := parseFlags(argv)
	if err != nil {
		return err
	}
	if f.project == "" {
		return errors.New("--project is required")
	}
	if f.subscription == "" {
		return errors.New("--subscription must not be empty")
	}
	if f.maxMessages < 1 || f.maxMessages > maxMessagesCeiling {
		return fmt.Errorf("--max-messages must be between 1 and %d, got %d", maxMessagesCeiling, f.maxMessages)
	}
	// A non-positive budget would expire every batch before its first lookup,
	// turning the join off in a way nothing in the output names. The ceiling is
	// half Pub/Sub's own maximum ack deadline; a budget above it guarantees the
	// redelivery the budget exists to prevent.
	if f.batchJoinBudget <= 0 || f.batchJoinBudget > batchJoinBudgetCeiling {
		return fmt.Errorf("--batch-join-budget must be between 1ns and %s, got %s", batchJoinBudgetCeiling, f.batchJoinBudget)
	}
	if f.inCluster && f.kubeconfig != "" {
		return errors.New("--in-cluster and --kubeconfig are mutually exclusive: both name the one cluster the join reads")
	}
	// The inject's flags are checked together, here, rather than when the first
	// drift arrives. Every one of these is a startup-time fact, and a detector
	// that pulls happily for an hour and then fails its first escalation has
	// spent that hour looking correct.
	if f.tokenEnv != "" && f.daemonURL == "" {
		return errors.New("--token-env was given without --daemon-url, so nothing would be injected and the token would go unused")
	}
	if f.owner != "" && f.daemonURL == "" {
		return errors.New("--owner was given without --daemon-url, so no session would be opened for it to be asserted on")
	}
	if f.daemonURL != "" && f.tokenEnv == "" {
		return errors.New("--token-env is required with --daemon-url: the daemon rejects an unauthenticated session create")
	}
	// Refused rather than defaulted. Without the full identity the join cannot
	// tell a record from this cluster from a record about a same-named object on
	// another, and guessing wrong does not error -- it reads the wrong object
	// and reports its ownership as though it were the audited one. Both parts
	// are required because either one alone leaves that ambiguity: the name
	// repeats across locations, and the location is shared by every cluster in
	// it.
	hasCredentials := f.inCluster || f.kubeconfig != ""
	if hasCredentials && (f.clusterName == "" || f.clusterLocation == "") {
		return errors.New("--cluster-name and --cluster-location are both required with --in-cluster or --kubeconfig: with --project they name the cluster those credentials reach")
	}
	if !hasCredentials && (f.clusterName != "" || f.clusterLocation != "") {
		return errors.New("--cluster-name or --cluster-location was given without --in-cluster or --kubeconfig, so nothing would read live objects from it")
	}
	// Refused only with the join on, because the two consumers of --project
	// disagree about what it may be. A Pub/Sub resource path accepts a project
	// number as readily as an ID, so the pull works either way and a detector
	// with no join is right to take it as given; the join then compares
	// the same string against resource.labels.project_id, which is always the
	// ID. A number therefore matches no record at all, and does it silently --
	// every lookup is counted unreachable, which is also what a correctly
	// configured single-cluster detector reports for the rest of the project.
	// Nothing distinguishes the two at runtime, so the distinction is made here.
	//
	// Keyed on the join being on at all, not on the direct credentials. The
	// profile path compares --project twice -- once against each record's
	// project_id as above, and once against each discovered profile's own project
	// to decide which clusters to register -- so a number there discards every
	// profile at discovery and then matches no record either, which reads as an
	// empty fleet rather than as a bad flag.
	joinEnabled := hasCredentials || f.profilesDir != ""
	if joinEnabled && looksLikeProjectNumber(f.project) {
		return fmt.Errorf("--project=%s is a project number, but the join matches it against each record's project_id, which is always the project ID: pass the ID, or drop --in-cluster/--kubeconfig/--profiles-dir to run without the join", f.project)
	}

	// Cancelled on SIGINT or SIGTERM, which stops the pull loop. Settling the
	// batch it was working on does not run on this context -- see
	// subscriber.settleContext -- so the records the loop had not reached when
	// the signal arrived are nacked and redelivered to the next instance
	// rather than acked undelivered. Only a non-graceful exit, where nothing
	// settles at all, leaves the whole batch to the subscription's ack
	// deadline.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// The cluster side is built and checked before the Pub/Sub client, and the
	// order is load-bearing in two ways. It fails on a local misconfiguration
	// without first opening a connection to a remote service -- and it is what
	// makes the refusal below reachable from a test, since newPubsubSource wants
	// credentials and nothing past it can be exercised without them. Building a
	// client from a kubeconfig connects to nothing, so a test can drive this far
	// on a temporary file.
	getter, err := newObjectGetter(f.kubeconfig, f.inCluster)
	if err != nil {
		return err
	}

	// Built once and used for both the check below and the routing table, so the
	// identity verified against the credentials is the same value the join will
	// route on rather than a second copy of it that could drift.
	direct := directClusterIdentity(f)

	// The direct cluster is verified before the profile scan runs, so a run with
	// both sources configured still refuses to start on the mismatch below
	// without first minting a token per profile. The scan is the slower and more
	// forgiving of the two -- it degrades on almost everything -- and the check
	// it would delay is the only one here that is fatal.
	if getter != nil {
		// Fatal on a mismatch. See verifyClusterIdentity: this is the one
		// startup check whose failure mode produces confident wrong output
		// instead of a count, so it is the one that refuses to run.
		//
		// The direct cluster only. A profile's identity is read from the same
		// cluster_identity that addressed its endpoint, so there are not two
		// claims to disagree; the flags are the only place a human states which
		// cluster a credential reaches.
		line, err := verifyClusterIdentity(ctx, direct, func(probeCtx context.Context) (clusterIdentity, error) {
			return observeCluster(probeCtx, f.kubeconfig, f.inCluster)
		})
		if err != nil {
			return err
		}
		if line != "" {
			log.Printf("%s: %s", commandName, line)
		}
	}

	// Only when the direct credentials exist. With them, the profile reconcile
	// writes for this same cluster is redundant and the scan declines it before
	// spending a GKE describe on a getter buildClusterSet would discard; without
	// them, that profile is the only way the cluster is reached at all.
	var directlyReached *clusterIdentity
	if getter != nil {
		directlyReached = &direct
	}

	// Fatal error propagated rather than degraded: internal/clusterprofiles
	// returns one only for a --profiles-dir that is not there, which discovery
	// runs once against and a restart fixes. Everything survivable has already
	// been logged and recorded in the scan by this point.
	scan, err := discoverProfileClusters(ctx, f.profilesDir, f.project, directlyReached)
	if err != nil {
		return err
	}

	clusters := buildClusterSet(getter, direct, scan.Clusters)

	// The token is read here rather than inside the injector so that an empty
	// one is a startup error naming the variable. Read from the environment and
	// never logged: the startup line below prints the daemon URL and the owner,
	// both of which are addresses, and neither is this.
	var inject *driftInjector
	if f.daemonURL != "" {
		token := os.Getenv(f.tokenEnv)
		if token == "" {
			return fmt.Errorf("the bearer token environment variable named by --token-env=%s is unset or empty", f.tokenEnv)
		}
		inject, err = newDriftInjector(driftInjectorConfig{
			daemonURL:      f.daemonURL,
			bearerToken:    token,
			assertedCaller: f.owner,
		})
		if err != nil {
			return err
		}
	}

	injectHandler := newDriftInjectHandler(inject)
	filter, join := newFilterFromFlags(f, clusters, injectHandler.Handle)

	// Said at startup, because the difference between the two modes is invisible
	// in every later line: a run with the inject off emits exactly the DRIFT
	// lines a run with it on does, and only the shutdown tally distinguishes
	// them. An operator who expected escalation and did not get it should find
	// the reason at launch rather than after the first change goes unreported.
	if inject == nil {
		log.Printf("%s: no --daemon-url; %s injects are disabled and drift will be logged only, not escalated to anyone", commandName, injectKindDrift)
	} else {
		log.Printf("%s: %s injects enabled, posting to %s (owner=%q, token from $%s)", commandName, injectKindDrift, f.daemonURL, f.owner, f.tokenEnv)
	}

	// Say which mode the join is in at startup rather than leaving it to be
	// inferred from the counts at shutdown. An operator who forgot the
	// credential flags otherwise sees DRIFT lines with no ownership on them and
	// no statement anywhere that the join never ran.
	if join.Clusters() == 0 {
		reason := joinDisabledReason(f.profilesDir, scan)
		// Not "every record": a delete, and any record naming no object, is
		// counted no_object before the join reaches the cluster lookup, so those
		// two do not move the unreachable counter even with the join off.
		log.Printf("%s: %s; live-object join disabled, every record naming a live object will be counted unreachable", commandName, reason)
		// Said separately because it is the flag most likely to have been set by
		// someone who believed the join was on: with no clusters nothing reads
		// managedFields, so no manager can be matched against this list and the
		// value has no effect on anything this run prints.
		if f.gitopsManagers != "" {
			log.Printf("%s: --gitops-managers=%q has no effect while the join is disabled; no ownership is read, so no reconcile can be claimed", commandName, f.gitopsManagers)
		}
	} else {
		log.Printf("%s: live-object join enabled for %d cluster(s) (direct=%t profiles=%d gitops-managers=%q)",
			commandName, join.Clusters(), getter != nil, len(scan.Clusters), f.gitopsManagers)
	}

	// Reported whether or not the join ended up with clusters, and separately
	// from the count above, because a skip is the difference between a fleet of
	// six and a fleet of seven this run reached six of. The count alone reads
	// identically either way.
	if scan.Skipped > 0 {
		log.Printf("%s: %d profile(s) skipped and will NOT be joined; records from their clusters will be counted unreachable", commandName, scan.Skipped)
	}
	// Not a skip: the cluster is joined, through the direct credentials instead.
	// Logged so that a profile count that does not match the cluster count has
	// an explanation in the same place as the counts, and said in as many words
	// because the line above it is about clusters that were lost.
	if len(scan.Absorbed) > 0 {
		log.Printf("%s: cluster(s) %s have a profile naming the cluster --in-cluster/--kubeconfig already reaches; joined through those credentials instead, and their profile was left unread",
			commandName, strings.Join(scan.Absorbed, absorbedProfileSeparator))
	}

	source, err := newPubsubSource(ctx, f.project, f.subscription)
	if err != nil {
		return err
	}

	// Log the path actually pulled, not the flag: --subscription accepts a bare
	// id or a fully qualified name, and reporting the raw flag back would hide
	// which of the two this run resolved to.
	sub := newSubscriber(source, filter.Handle, f.maxMessages, f.batchJoinBudget)
	log.Printf("%s: pulling %s (max-messages=%d batch-join-budget=%s)", commandName, source.subscription, f.maxMessages, f.batchJoinBudget)

	// --batch-join-budget is validated against Pub/Sub's own maximum deadline,
	// which no install has to use, so startup validation cannot tell whether the
	// budget fits the subscription this run is pointed at. Read the real deadline
	// and say so. Advisory on both sides: a budget that overruns is a warning
	// rather than a refusal because the overrun costs redelivery and not
	// correctness, and a probe that fails is a warning because the grant it needs
	// is one roles/pubsub.subscriber does not include.
	if line := ackDeadlinePreflight(ctx, source.AckDeadline, f.batchJoinBudget); line != "" {
		log.Printf("%s: %s", commandName, line)
	}

	runErr := sub.Run(ctx)
	counts := sub.Counts()

	// parse_failures rather than failed: the tier line below reports
	// failed_calls, which is the API server rejecting a write, and the two
	// numbers mean unrelated things. Printed adjacently they would otherwise
	// read as the same counter measured twice.
	log.Printf("%s: stopping (parsed=%d skipped=%d parse_failures=%d)", commandName, counts.Parsed, counts.Skipped, counts.Failed)
	log.Printf("%s: tiers (%s)", commandName, filter.Counts())
	log.Printf("%s: join (%s)", commandName, join.Counts())

	// Only with the inject on. With it off every field is zero by construction,
	// and a line of zeroes beside the join's real numbers reads as an inject
	// that was tried and never worked -- the startup line already said it was
	// never attempted.
	if inject != nil {
		log.Printf("%s: inject (%s)", commandName, injectHandler.Counts())
	}

	// The unreachable clusters are named on the way out, for the same reason the
	// unattributed principals below are: the count says the join missed records,
	// this says which cluster to onboard so that it stops. With one cluster in
	// the set the list was inferable -- everything else in the project -- and
	// after the fan-in it is not.
	//
	// Not necessarily a misconfiguration. A project holding a cluster nobody
	// intends to onboard reports it here every run, which is the honest answer:
	// the detector cannot tell that cluster from one whose profile failed to
	// write.
	if unreachable := join.UnreachableClusters(); len(unreachable) > 0 {
		log.Printf("%s: unreachable clusters (no credentials and no Cluster Agent profile; their records were forwarded without ownership): %s",
			commandName, strings.Join(unreachable, unreachableListSeparator))
	}

	// The unattributed principals are logged by name on the way out, not just
	// counted. A non-empty list is the signal that a rule is missing -- and
	// without the names, an operator reading the count has nothing to write it
	// from. One entry is not a missing rule: unauthenticatedPrincipalLabel
	// stands for requests that carried no identity at all, and there is
	// nothing to classify them as.
	if unattributed := filter.Unattributed(); len(unattributed) > 0 {
		log.Printf("%s: unattributed principals (no rule matched; %s aside, classify these before trusting the human count): %s",
			commandName, unauthenticatedPrincipalLabel, strings.Join(unattributed, unattributedListSeparator))
	}

	// A cancelled context is how this binary is meant to stop; anything else
	// is a failure worth a non-zero exit.
	if errors.Is(runErr, context.Canceled) {
		return nil
	}
	if runErr != nil {
		return fmt.Errorf("subscriber: %w", runErr)
	}
	return nil
}
