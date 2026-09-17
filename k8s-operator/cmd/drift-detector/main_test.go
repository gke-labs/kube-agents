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
	"errors"
	"flag"
	"strings"
	"testing"
)

func TestParseFlagsDefaults(t *testing.T) {
	f, err := parseFlags([]string{"--project", "example-project"})
	if err != nil {
		t.Fatalf("parseFlags returned error: %v", err)
	}
	if f.project != "example-project" {
		t.Errorf("project = %q, want example-project", f.project)
	}
	if f.subscription != defaultSubscriptionName {
		t.Errorf("subscription = %q, want %q", f.subscription, defaultSubscriptionName)
	}
	if f.maxMessages != defaultMaxMessages {
		t.Errorf("maxMessages = %d, want %d", f.maxMessages, defaultMaxMessages)
	}
}

// The classifier flags default to the permissive end: no allowlist, and any
// domain counts as human. A default that silently narrowed the human test
// would drop real drift with nothing in the log to say so.
func TestParseFlagsClassifierDefaults(t *testing.T) {
	f, err := parseFlags([]string{"--project", "example-project"})
	if err != nil {
		t.Fatalf("parseFlags returned error: %v", err)
	}
	if f.automationPrincipals != "" {
		t.Errorf("automationPrincipals = %q, want empty", f.automationPrincipals)
	}
	if f.humanDomains != "" {
		t.Errorf("humanDomains = %q, want empty", f.humanDomains)
	}
	if f.logDropped {
		t.Error("logDropped = true, want false: a line per drop is the whole audit stream")
	}
}

func TestParseFlagsClassifierValues(t *testing.T) {
	f, err := parseFlags([]string{
		"--project", "example-project",
		"--automation-principals", "ci-bot@example.com,kubelet-nodepool-bootstrap",
		"--human-domains", "corp.example",
		"--log-dropped",
	})
	if err != nil {
		t.Fatalf("parseFlags returned error: %v", err)
	}
	if want := "ci-bot@example.com,kubelet-nodepool-bootstrap"; f.automationPrincipals != want {
		t.Errorf("automationPrincipals = %q, want %q", f.automationPrincipals, want)
	}
	if f.humanDomains != "corp.example" {
		t.Errorf("humanDomains = %q, want corp.example", f.humanDomains)
	}
	if !f.logDropped {
		t.Error("logDropped = false, want true")
	}
}

// The two classifier flags are both strings, so transposing them at the
// NewClassifier call site compiles and vets clean, and every test that builds a
// Classifier directly still passes while the deployed detector classifies the
// whole stream wrongly. This drives the wiring from parsed flags instead, which
// is the only place that mistake is visible.
func TestNewFilterFromFlagsWiring(t *testing.T) {
	f, err := parseFlags([]string{
		"--project", "example-project",
		"--automation-principals", "ci-bot@example.com",
		"--human-domains", "corp.example",
		"--log-dropped",
	})
	if err != nil {
		t.Fatalf("parseFlags returned error: %v", err)
	}
	// nil getter: this test is about the flags reaching the classifier, and a
	// nil getter keeps the join from being the thing under test.
	filter, _ := newFilterFromFlags(f, nil)

	if !filter.logDropped {
		t.Error("logDropped did not reach the filter")
	}

	for _, principal := range []string{
		"ci-bot@example.com", // the allowlist: automation
		"ada@corp.example",   // the configured domain: human, and actionable
		"mallory@other.test", // a domain, but not a configured one
	} {
		filter.Handle(context.Background(), AuditRecord{Principal: principal, StatusCode: statusCodeOK})
	}

	counts := filter.Counts()
	if got := counts.ByTier[TierAutomation]; got != 1 {
		t.Errorf("automation = %d, want 1 -- the allowlist did not reach the classifier", got)
	}
	if got := counts.ByTier[TierHuman]; got != 1 {
		t.Errorf("human = %d, want 1 -- the domain list did not reach the classifier", got)
	}
	if got := counts.ByTier[TierUnattributed]; got != 1 {
		t.Errorf("unattributed = %d, want 1", got)
	}
	if counts.Actionable != 1 {
		t.Errorf("actionable = %d, want 1", counts.Actionable)
	}
}

// The cluster identity is assembled from three flag values, one of which
// (--project) is not named after the field it fills, so a transposition or an
// omission here is invisible to the compiler and to go vet -- the same hazard
// NewClassifier has and the reason newFilterFromFlags exists at all. The result
// would be a detector that matches no record and reports the whole stream
// unreachable, and no test constructing a joiner directly would see it.
func TestNewFilterFromFlagsWiresTheJoin(t *testing.T) {
	f, err := parseFlags([]string{
		"--project", "example-project",
		"--cluster-name", "prod-a",
		"--cluster-location", "us-central1",
		"--gitops-managers", "argocd-controller",
		"--in-cluster",
	})
	if err != nil {
		t.Fatalf("parseFlags returned error: %v", err)
	}

	stub := &stubGetter{obj: managedFieldsObject()}
	filter, join := newFilterFromFlags(f, stub)

	// The project half comes from --project rather than a flag of its own, so
	// this also pins that wiring: a joiner built with an empty project matches
	// nothing, and every record would come out unreachable.
	want := clusterIdentity{Project: "example-project", Location: "us-central1", Cluster: "prod-a"}
	if join.cluster != want {
		t.Errorf("joiner.cluster = %+v, want %+v", join.cluster, want)
	}
	if !join.gitopsManagers["argocd-controller"] {
		t.Errorf("joiner.gitopsManagers = %v, want argocd-controller in it", join.gitopsManagers)
	}
	if join.getter == nil {
		t.Error("the getter did not reach the joiner")
	}

	// End to end through the filter: a human change on this cluster has to come
	// out the other side as an enriched join, which is the only assertion that
	// covers the filter and the joiner being connected at all.
	// All three identity fields, because the gate ANDs them: a record carrying
	// only the cluster name comes out unreachable and Enriched stays zero.
	filter.Handle(context.Background(), AuditRecord{
		Principal:  "ada@example.com",
		Project:    "example-project",
		Location:   "us-central1",
		Cluster:    "prod-a",
		Verb:       "patch",
		StatusCode: statusCodeOK,
		Resource:   ResourceRef{Group: "apps", Version: "v1", Namespace: "prod", Resource: "deployments", Name: "api"},
	})

	if got := join.Counts().Enriched; got != 1 {
		t.Errorf("enriched = %d, want 1 -- the filter is not forwarding to the join", got)
	}
}

// realMain rejects bad configuration before it builds a client, so these cases
// need no credentials and no subscription.
func TestRealMainRejectsBadConfiguration(t *testing.T) {
	tests := []struct {
		name    string
		argv    []string
		wantErr string
	}{
		{
			name:    "project is required",
			argv:    []string{},
			wantErr: "--project is required",
		},
		{
			name:    "subscription must not be empty",
			argv:    []string{"--project", "p", "--subscription", ""},
			wantErr: "--subscription must not be empty",
		},
		{
			name:    "max-messages below one",
			argv:    []string{"--project", "p", "--max-messages", "0"},
			wantErr: "--max-messages must be between",
		},
		{
			name:    "max-messages above the API ceiling",
			argv:    []string{"--project", "p", "--max-messages", "1001"},
			wantErr: "--max-messages must be between",
		},
		{
			// Zero would expire every batch before its first lookup, so the
			// join would be off with nothing in the output saying so.
			name:    "batch-join-budget of zero",
			argv:    []string{"--project", "p", "--batch-join-budget", "0s"},
			wantErr: "--batch-join-budget must be between",
		},
		{
			// Above the ceiling the budget guarantees the redelivery it exists
			// to prevent: Pub/Sub's own maximum ack deadline is 600s.
			name:    "batch-join-budget above the ceiling",
			argv:    []string{"--project", "p", "--batch-join-budget", "10m"},
			wantErr: "--batch-join-budget must be between",
		},
		{
			name:    "in-cluster and kubeconfig both name the one cluster the join reads",
			argv:    []string{"--project", "p", "--in-cluster", "--kubeconfig", "/tmp/kubeconfig", "--cluster-name", "prod-a"},
			wantErr: "mutually exclusive",
		},
		{
			// Refused rather than defaulted: without a name the join cannot
			// tell a record from this cluster from one about a same-named
			// object elsewhere, and guessing reads the wrong object silently.
			name:    "in-cluster without a cluster name",
			argv:    []string{"--project", "p", "--in-cluster"},
			wantErr: "--cluster-name and --cluster-location are both required",
		},
		{
			name:    "kubeconfig without a cluster name",
			argv:    []string{"--project", "p", "--kubeconfig", "/tmp/kubeconfig"},
			wantErr: "--cluster-name and --cluster-location are both required",
		},
		{
			// The half-identity case, and the one the name-only check used to
			// let through: a GKE cluster name is unique within a project and
			// location, so "prod-a" with no location matches a same-named
			// cluster in every other region the subscription carries.
			name:    "cluster name without its location",
			argv:    []string{"--project", "p", "--in-cluster", "--cluster-name", "prod-a"},
			wantErr: "--cluster-name and --cluster-location are both required",
		},
		{
			name:    "cluster location without its name",
			argv:    []string{"--project", "p", "--in-cluster", "--cluster-location", "us-central1"},
			wantErr: "--cluster-name and --cluster-location are both required",
		},
		{
			// The inverse misconfiguration: a name with nothing to read
			// through would otherwise start a detector that enriches nothing.
			name:    "cluster name with no credentials to reach it",
			argv:    []string{"--project", "p", "--cluster-name", "prod-a"},
			wantErr: "without --in-cluster or --kubeconfig",
		},
		{
			name:    "cluster location with no credentials to reach it",
			argv:    []string{"--project", "p", "--cluster-location", "us-central1"},
			wantErr: "without --in-cluster or --kubeconfig",
		},
		{
			// A project number pulls the subscription perfectly well and then
			// matches no record at all, because the join compares it against
			// project_id, which is always the ID. Everything comes out
			// unreachable -- which is also what a healthy single-cluster
			// detector reports for the rest of the project, so nothing at
			// runtime tells the two apart.
			name:    "project given as a number with the join enabled",
			argv:    []string{"--project", "123456789012", "--in-cluster", "--cluster-name", "prod-a", "--cluster-location", "us-central1"},
			wantErr: "is a project number",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := realMain(tc.argv)
			if err == nil {
				t.Fatalf("realMain(%v) succeeded, want error", tc.argv)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("realMain(%v) error = %q, want it to contain %q", tc.argv, err, tc.wantErr)
			}
		})
	}
}

func TestRealMainRefusesAClusterTheCredentialsDoNotReach(t *testing.T) {
	// The one case in this file that runs past the flag validation, and the
	// reason realMain builds the cluster side before the Pub/Sub client: every
	// other case returns before newPubsubSource, which wants credentials, so
	// nothing downstream of it was reachable from a test at all. What is pinned
	// here is the wiring rather than the comparison -- TestVerifyClusterIdentity
	// covers the comparison, and it stays green if this call site logs the
	// mismatch instead of returning it, which would leave a detector that
	// announces it is reading the wrong cluster and then reads it.
	//
	// No credentials are needed: building a client from a kubeconfig connects to
	// nothing, and the identity comes out of the file's own context name.
	const reachesUSEast4 = `apiVersion: v1
kind: Config
clusters:
  - name: prod-a
    cluster:
      server: https://127.0.0.1:1
contexts:
  - name: gke_example-project_us-east4_prod-a
    context:
      cluster: prod-a
      user: prod-a
current-context: gke_example-project_us-east4_prod-a
users:
  - name: prod-a
    user:
      token: not-a-real-token
`

	argv := []string{
		"--project", "example-project",
		"--kubeconfig", writeKubeconfig(t, reachesUSEast4),
		"--cluster-name", "prod-a",
		"--cluster-location", "us-central1",
	}

	err := realMain(argv)
	if err == nil {
		t.Fatalf("realMain(%v) succeeded, want it to refuse a cluster the credentials do not reach", argv)
	}
	if want := "refusing to enrich"; !strings.Contains(err.Error(), want) {
		t.Errorf("realMain error = %q, want it to contain %q", err, want)
	}
}

func TestLooksLikeProjectNumber(t *testing.T) {
	// The whole test is "nothing but digits", and it is exact rather than
	// heuristic because a GCP project ID must begin with a lowercase letter.
	// Worth pinning both directions: widened, this rejects real project IDs at
	// startup and the detector will not run at all.
	for value, want := range map[string]bool{
		"123456789012":        true,
		"1":                   true,
		"example-project":     false,
		"project-2":           false,
		"2nd-project":         false, // not a legal project ID either, but not this check's business
		"":                    false,
		"123456789012-backup": false,
	} {
		if got := looksLikeProjectNumber(value); got != want {
			t.Errorf("looksLikeProjectNumber(%q) = %v, want %v", value, got, want)
		}
	}
}

// --help must not be reported as a failure: main swallows flag.ErrHelp and
// exits zero, and that only works if parseFlags propagates it unwrapped.
func TestParseFlagsHelpIsErrHelp(t *testing.T) {
	_, err := parseFlags([]string{"--help"})
	if !errors.Is(err, flag.ErrHelp) {
		t.Errorf("parseFlags(--help) error = %v, want flag.ErrHelp", err)
	}
}

// The drift-pubsub module's subscription_id output is fully qualified and its
// README feeds it to --subscription, while the default is a bare id. Prefixing
// the qualified form would pull projects/P/subscriptions/projects/P/... which
// does not exist -- and an empty subscription reads exactly like no drift.
func TestSubscriptionPath(t *testing.T) {
	tests := []struct {
		name         string
		project      string
		subscription string
		want         string
	}{
		{
			name:         "bare id is qualified with the project",
			project:      "example-project",
			subscription: defaultSubscriptionName,
			want:         "projects/example-project/subscriptions/platform-agent-drift-audit-sub",
		},
		{
			name:         "already-qualified name passes through",
			project:      "example-project",
			subscription: "projects/other-project/subscriptions/platform-agent-drift-audit-sub",
			want:         "projects/other-project/subscriptions/platform-agent-drift-audit-sub",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := subscriptionPath(tc.project, tc.subscription); got != tc.want {
				t.Errorf("subscriptionPath(%q, %q) = %q, want %q", tc.project, tc.subscription, got, tc.want)
			}
		})
	}
}
