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
	"regexp"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/dispatch"
)

// reasonTokenPattern is the shape a reason token keeps: up to 64 letters,
// digits, dots, underscores and hyphens, lowercase-hyphen by convention.
var reasonTokenPattern = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// TestEveryDispatchRefusalReadsAsOnePlainLine holds the relay to the
// dispatcher's terminal reasons: each one reads in chat as a sentence of its
// own or as its detail, never as the bare "the executor rejected the task"
// (.agents/rules/chat_ux.md, "A failure is one plain line"). A reason added
// to the dispatcher without a line here fails this test.
func TestEveryDispatchRefusalReadsAsOnePlainLine(t *testing.T) {
	for _, reason := range []string{
		dispatch.ReasonTierNotAllowed,
		dispatch.ReasonTierWithdrawn,
		dispatch.ReasonVerifierRefused,
		dispatch.ReasonParametersInvalid,
		dispatch.ReasonQueueTimeout,
		dispatch.ReasonNoWorker,
		dispatch.ReasonLeaseExpired,
		dispatch.ReasonDeadlineExceeded,
		dispatch.ReasonChangeNeedsJob,
		dispatch.ReasonParentUnchecked,
		dispatch.ReasonParentCycle,
		dispatch.ReasonOutsideParentScope,
		// A tier-ended cancel says why in its own sentence (#5850).
		dispatch.ReasonTierEnded,
		// A task settled with no terminal on a latched install (#5301).
		dispatch.ReasonTerminalUnpublished,
		// dispatch.ReasonStopped is left out: it ends the task canceled, and
		// the room reads a stopped task as its own notice (noticeCanceled).
	} {
		if !reasonTokenPattern.MatchString(reason) {
			t.Errorf("dispatch reason %q does not fit %s", reason, reasonTokenPattern)
		}
		_, sentence := reasonSentences[reason]
		if !sentence && !reasonDetailShown[reason] {
			t.Errorf("dispatch reason %q has no line in reasonSentences or reasonDetailShown", reason)
		}
	}
}
