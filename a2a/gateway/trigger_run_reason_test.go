// Copyright 2026 The Kubernetes Authors.
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

package gateway

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/taskguard"
)

// TestRunReasonToken: a scheduled run's record carries the bare token of a
// terminal message the gateway knows, and nothing for anything else.
func TestRunReasonToken(t *testing.T) {
	for _, tc := range []struct{ name, message, want string }{
		{"a known token with its detail", "reason: deadline-exceeded - the task ran past 15m", "deadline-exceeded"},
		{"a known bare token", "reason: worker-evicted", "worker-evicted"},
		{"a recovered panic", taskguard.Reason, taskguard.ReasonToken},
		{"a detail-shown token", "reason: verifier-refused - the cluster is out of scope", "verifier-refused"},
		{"a canceled terminal's own token", "reason: canceled-by-request", "canceled-by-request"},
		{"a stopped run", "reason: stopped - the operator stopped the run", "stopped"},
		{"a canceled token the notice explains", "reason: tier-ended - an administrator ended the drain", "tier-ended"},
		{"a script run's end", "reason: script-timeout - exceeded 300s", "script-timeout"},
		{"a refused delegation", "reason: delegation-refused - the target is not allowed", "delegation-refused"},
		{"an unknown token", "reason: something-new - detail", ""},
		{"a malformed token", "reason: Not_A_Token!", ""},
		{"plain text", "executor died", ""},
		{"nothing", "", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := runReasonToken(tc.message); got != tc.want {
				t.Fatalf("runReasonToken(%q) = %q, want %q", tc.message, got, tc.want)
			}
		})
	}
}

// TestRunReasonTokensFitTheOperatorsPattern: every token the gateway can put
// in a run's reason passes the AgentSchedule CRD's own pattern for the field,
// so the operator, a separate module, never drops a token the gateway sends.
// A token added to a gateway table that breaks the pattern fails here.
func TestRunReasonTokensFitTheOperatorsPattern(t *testing.T) {
	src, err := os.ReadFile(filepath.Join("..", "..", "k8s-operator", "api", "v1alpha1", "agentschedule_types.go"))
	if err != nil {
		t.Fatal(err)
	}
	m := regexp.MustCompile("(?m)\\+kubebuilder:validation:Pattern=`([^`]+)`\\s*\\n\\s*// \\+optional\\s*\\n\\s*Reason string").FindSubmatch(src)
	if m == nil {
		t.Fatal("AgentScheduleRun.Reason's kubebuilder pattern was not found in agentschedule_types.go")
	}
	pattern := regexp.MustCompile(string(m[1]))
	tokens := runReasonTokens()
	if len(tokens) < 40 {
		t.Fatalf("only %d run reason tokens; the tables look unread", len(tokens))
	}
	for _, tok := range tokens {
		if !pattern.MatchString(tok) {
			t.Errorf("gateway run reason %q does not match the operator's pattern %s", tok, m[1])
		}
		if got := runReasonToken(reasonPrefix + tok); got != tok {
			t.Errorf("runReasonToken(%q) = %q; every listed token must round-trip", reasonPrefix+tok, got)
		}
	}
}

// TestATriggersStatusCarriesTheRunsReason: a scheduled run that ended with
// a reason the gateway knows answers its status with that token, and never
// the detail after it.
func TestATriggersStatusCarriesTheRunsReason(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	var origin lib.Envelope
	if err := json.Unmarshal(tr.submissions()[0].Data, &origin); err != nil {
		t.Fatal(err)
	}
	executor := lib.Party{Session: "fleet-audit", AgentType: "test-executor"}
	publishTerminal(t, tr.rig, &origin, executor, lib.TaskEventsSubject("fleet-audit", origin.TaskID), lib.StateFailed,
		"reason: deadline-exceeded - the run took longer than its deadline")
	var got lib.TriggerStatusReply
	waitFor(t, "the terminal in the status", func() bool {
		got = tr.status(t, s, reply.TaskID)
		return got.Terminal
	})
	if got.State != lib.StateFailed || got.Reason != "deadline-exceeded" {
		t.Fatalf("the ended run's status = %+v, want failed with reason deadline-exceeded", got)
	}
}

// TestACompletedRunCarriesNoReason: a run that completed records no reason,
// even when its terminal message names a known token.
func TestACompletedRunCarriesNoReason(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	var origin lib.Envelope
	if err := json.Unmarshal(tr.submissions()[0].Data, &origin); err != nil {
		t.Fatal(err)
	}
	executor := lib.Party{Session: "fleet-audit", AgentType: "test-executor"}
	publishTerminal(t, tr.rig, &origin, executor, lib.TaskEventsSubject("fleet-audit", origin.TaskID), lib.StateCompleted,
		"reason: deadline-exceeded")
	var got lib.TriggerStatusReply
	waitFor(t, "the terminal in the status", func() bool {
		got = tr.status(t, s, reply.TaskID)
		return got.Terminal
	})
	if got.State != lib.StateCompleted || got.Reason != "" {
		t.Fatalf("the completed run's status = %+v, want completed with no reason", got)
	}
}

// A recovered panic's terminal (taskguard.Reason, from the hermes-bridge or
// the worker adapter) reads in chat as the failure notice plus a sentence,
// not the bare notice: the token is in the gateway's vocabulary.
func TestTheRecoveredPanicTokenReadsInPlainWords(t *testing.T) {
	words, ok := reasonInPlainWords(taskguard.Reason)
	if !ok || words == "" {
		t.Fatalf("reasonInPlainWords(%q) = %q, %v; want a sentence", taskguard.Reason, words, ok)
	}
	if strings.Contains(words, taskguard.ReasonToken) {
		t.Fatalf("the sentence %q shows the raw token", words)
	}
	// The panic can come after the run's work (the scrub, the chat fold, a
	// publish), so asking again can repeat a write: the sentence says to
	// check what was done first, never a bare "ask again".
	if !strings.Contains(words, "check that before you ask again") || strings.HasSuffix(words, "; ask again") {
		t.Fatalf("the sentence %q doesn't say to check what was done before asking again", words)
	}
}
