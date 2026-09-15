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
	filter := newFilterFromFlags(f)

	if !filter.logDropped {
		t.Error("logDropped did not reach the filter")
	}

	for _, principal := range []string{
		"ci-bot@example.com", // the allowlist: automation
		"ada@corp.example",   // the configured domain: human, and actionable
		"mallory@other.test", // a domain, but not a configured one
	} {
		filter.Handle(AuditRecord{Principal: principal, StatusCode: statusCodeOK})
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
