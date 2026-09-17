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
// This is CUJ 3, built in four stages, of which three ship here. T1 is the
// ingestion path (pull and parse), T2 is principal classification (assign a
// tier, drop the calls that changed nothing), and T3 joins managedFields off
// the live object for the one cluster this process has credentials for --
// records from any other cluster in the project are forwarded unenriched and
// counted unreachable. T4 emits the gitops-drift inject and is not built, so
// nothing consumes what T3 produces beyond a log line. See
// docs/designs/drift-detection.md for the design.
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
	fs.StringVar(&f.kubeconfig, "kubeconfig", "",
		"Path to a kubeconfig for the cluster whose live objects the join reads. An operator-supplied path for local runs, not a discovery mechanism. Mutually exclusive with --in-cluster; setting neither of the two disables the join.")
	fs.BoolVar(&f.inCluster, "in-cluster", false,
		"Read live objects using the Pod's own ServiceAccount. Mutually exclusive with --kubeconfig; setting neither of the two disables the join.")
	fs.StringVar(&f.clusterName, "cluster-name", "",
		"GKE cluster name the join's credentials reach. Required with --kubeconfig or --in-cluster; records from any other cluster are counted unreachable rather than looked up on the wrong one. Checked at startup against the cluster those credentials actually reach, and a disagreement stops the process.")
	fs.StringVar(&f.clusterLocation, "cluster-location", "",
		"GKE location (region or zone) of --cluster-name. Required with it: a cluster name is unique only within a project and location, so without this a same-named cluster elsewhere would be read as this one.")
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
// It is also where the cluster identity is assembled, and the project it uses
// is --project: the subscription is a project-level sink, so the clusters it
// carries are that project's. A subscription pointed at another project's sink
// therefore matches nothing and reports every record unreachable, which is the
// direction to fail in -- the alternative is enriching one project's records
// from another's clusters.
func newFilterFromFlags(f *flags, getter objectGetter) (*driftFilter, *joiner) {
	identity := clusterIdentity{Project: f.project, Location: f.clusterLocation, Cluster: f.clusterName}
	join := newJoiner(getter, identity, parseGitopsManagers(f.gitopsManagers), logDriftEvent)
	return newDriftFilter(NewClassifier(f.automationPrincipals, f.humanDomains), join.Handle, f.logDropped), join
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
	// with no credentials is right to take it as given; the join then compares
	// the same string against resource.labels.project_id, which is always the
	// ID. A number therefore matches no record at all, and does it silently --
	// every lookup is counted unreachable, which is also what a correctly
	// configured single-cluster detector reports for the rest of the project.
	// Nothing distinguishes the two at runtime, so the distinction is made here.
	if hasCredentials && looksLikeProjectNumber(f.project) {
		return fmt.Errorf("--project=%s is a project number, but the join matches it against each record's project_id, which is always the project ID: pass the ID, or drop --in-cluster/--kubeconfig to run without the join", f.project)
	}

	// Cancelled on SIGINT or SIGTERM, which stops the pull loop. Settling the
	// batch it was working on does not run on this context -- see
	// subscriber.settleContext, which is why an interrupted batch is acked
	// rather than redelivered.
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

	filter, join := newFilterFromFlags(f, getter)

	// Say which mode the join is in at startup rather than leaving it to be
	// inferred from the counts at shutdown. An operator who forgot the
	// credential flags otherwise sees DRIFT lines with no ownership on them and
	// no statement anywhere that the join never ran.
	if getter == nil {
		// Not "every record": a delete, and any record naming no object, is
		// counted no_object before the join reaches the credential check, so
		// those two do not move the unreachable counter even with the join off.
		log.Printf("%s: no cluster credentials (--in-cluster or --kubeconfig); live-object join disabled, every record naming a live object will be counted unreachable", commandName)
		// Said separately because it is the flag most likely to have been set by
		// someone who believed the join was on: with no getter nothing reads
		// managedFields, so no manager can be matched against this list and the
		// value has no effect on anything this run prints.
		if f.gitopsManagers != "" {
			log.Printf("%s: --gitops-managers=%q has no effect while the join is disabled; no ownership is read, so no reconcile can be claimed", commandName, f.gitopsManagers)
		}
	} else {
		log.Printf("%s: live-object join enabled for cluster %q (gitops-managers=%q)", commandName, join.cluster, f.gitopsManagers)

		// Checked against join.cluster rather than a second identity built from
		// the same flags, so the value verified here is the one matches() will
		// use rather than a copy that could drift from it.
		//
		// Fatal on a mismatch. See verifyClusterIdentity: this is the one
		// startup check whose failure mode produces confident wrong output
		// instead of a count, so it is the one that refuses to run.
		line, err := verifyClusterIdentity(ctx, join.cluster, func(probeCtx context.Context) (clusterIdentity, error) {
			return observeCluster(probeCtx, f.kubeconfig, f.inCluster)
		})
		if err != nil {
			return err
		}
		if line != "" {
			log.Printf("%s: %s", commandName, line)
		}
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
