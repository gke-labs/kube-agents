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
// This is CUJ 3, built in four stages. T1 is the ingestion path (pull and
// parse) and T2 is principal classification (assign a tier, drop the calls
// that changed nothing); both ship here. T3 joins managedFields off the live
// object and T4 emits the inject, and neither is built. See
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
)

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
}

// parseFlags reads argv, leaving validation to realMain so that a usage error
// and a configuration error are reported the same way.
func parseFlags(args []string) (*flags, error) {
	fs := flag.NewFlagSet(commandName, flag.ContinueOnError)
	f := &flags{}

	fs.StringVar(&f.project, "project", "", "GCP project holding the drift audit subscription. Required.")
	fs.StringVar(&f.subscription, "subscription", defaultSubscriptionName, "Pub/Sub subscription to pull audit records from.")
	fs.Int64Var(&f.maxMessages, "max-messages", defaultMaxMessages, "Messages requested per pull.")
	fs.StringVar(&f.automationPrincipals, "automation-principals", "",
		"Comma-separated principals to classify as automation rather than human, in addition to every *"+gcpServiceAccountSuffix+" service account. Applies to every cluster the subscription carries; changes without a rebuild.")
	fs.StringVar(&f.humanDomains, "human-domains", "",
		"Comma-separated domains whose accounts count as human. Empty means any principal carrying a domain.")
	fs.BoolVar(&f.logDropped, "log-dropped", false,
		"Log every filtered record. Verbose: the drop rate exceeds 99% on a live cluster.")

	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	return f, nil
}

// newFilterFromFlags builds the classifier and the filter the run loop drives.
// It is separate from realMain so that the wiring is reachable from a test:
// NewClassifier takes two strings, so transposing them is invisible to the
// compiler and to go vet, and the result is a detector that treats the
// allowlist as a domain list and classifies the whole stream wrongly while
// every test that constructs a Classifier directly still passes.
func newFilterFromFlags(f *flags) *driftFilter {
	return newDriftFilter(NewClassifier(f.automationPrincipals, f.humanDomains), logActionable, f.logDropped)
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

	// Cancelled on SIGINT or SIGTERM, which stops the pull loop. Settling the
	// batch it was working on does not run on this context -- see
	// subscriber.settleContext, which is why an interrupted batch is acked
	// rather than redelivered.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	source, err := newPubsubSource(ctx, f.project, f.subscription)
	if err != nil {
		return err
	}

	filter := newFilterFromFlags(f)

	// Log the path actually pulled, not the flag: --subscription accepts a bare
	// id or a fully qualified name, and reporting the raw flag back would hide
	// which of the two this run resolved to.
	sub := newSubscriber(source, filter.Handle, f.maxMessages)
	log.Printf("%s: pulling %s (max-messages=%d)", commandName, source.subscription, f.maxMessages)

	runErr := sub.Run(ctx)
	counts := sub.Counts()

	// parse_failures rather than failed: the tier line below reports
	// failed_calls, which is the API server rejecting a write, and the two
	// numbers mean unrelated things. Printed adjacently they would otherwise
	// read as the same counter measured twice.
	log.Printf("%s: stopping (parsed=%d skipped=%d parse_failures=%d)", commandName, counts.Parsed, counts.Skipped, counts.Failed)
	log.Printf("%s: tiers (%s)", commandName, filter.Counts())

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
