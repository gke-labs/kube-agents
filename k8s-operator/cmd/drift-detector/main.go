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
// This is CUJ 3, built in four stages. T1, here, is the ingestion path: pull,
// parse, and log. T2 classifies principals, T3 joins managedFields off the
// live object, and T4 emits the inject. See
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
}

// parseFlags reads argv, leaving validation to realMain so that a usage error
// and a configuration error are reported the same way.
func parseFlags(args []string) (*flags, error) {
	fs := flag.NewFlagSet(commandName, flag.ContinueOnError)
	f := &flags{}

	fs.StringVar(&f.project, "project", "", "GCP project holding the drift audit subscription. Required.")
	fs.StringVar(&f.subscription, "subscription", defaultSubscriptionName, "Pub/Sub subscription to pull audit records from.")
	fs.Int64Var(&f.maxMessages, "max-messages", defaultMaxMessages, "Messages requested per pull.")

	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	return f, nil
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

	// Log the path actually pulled, not the flag: --subscription accepts a bare
	// id or a fully qualified name, and reporting the raw flag back would hide
	// which of the two this run resolved to.
	sub := newSubscriber(source, logRecord, f.maxMessages)
	log.Printf("%s: pulling %s (max-messages=%d)", commandName, source.subscription, f.maxMessages)

	runErr := sub.Run(ctx)
	counts := sub.Counts()
	log.Printf("%s: stopping (parsed=%d skipped=%d failed=%d)", commandName, counts.Parsed, counts.Skipped, counts.Failed)

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
