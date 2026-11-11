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

package controller

import (
	"context"
	"fmt"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The KanbanDispatcherFailing condition, from the chat plugin's count of the
// kanban dispatcher's consecutive failed ticks per board, which the usage
// poller reads off the gateway's chat_metrics listener. The dispatcher logs a
// failed tick and tries again at the next one, so a dispatcher whose every
// tick fails (a full data volume, an unreadable board) leaves cards where they
// are and says so only in the gateway's log. The condition does not touch
// Ready, since the agent still answers.
const (
	kanbanDispatcherFailingCondition = "KanbanDispatcherFailing"
	// kanbanDispatcherFailingTicks is the streak that sets the condition: the
	// ticks one poll interval holds at the configured dispatch interval, so the
	// streak covers about one poll (a tick's own work lengthens it, the wake
	// nudge can shorten it); a failure the next tick undoes does not set it.
	kanbanDispatcherFailingTicks = int64(usageCountersPollInterval / (kanbanDispatchIntervalSeconds * time.Second))
	// kanbanDispatcherFailingAge and kanbanDispatcherMinFailingTicks are the
	// other way a board sets it, from the plugin's streak age: a streak at least
	// one poll old with at least this many failures. On a locked board each tick
	// waits out the SQLite busy timeout (120 s) before it fails, so 60 ticks take
	// two hours while the age reaches a poll in three. The minimum keeps one slow
	// failure, or one failure before a tick that hangs, from setting it.
	kanbanDispatcherFailingAge      = usageCountersPollInterval
	kanbanDispatcherMinFailingTicks = int64(3)

	kanbanDispatcherReasonTicksFailing = "TicksFailing"
	kanbanDispatcherReasonTicking      = "Ticking"

	// kanbanDispatcherLabelMax bounds the board and error class the message
	// quotes, both label values off the listener.
	kanbanDispatcherLabelMax    = 128
	kanbanDispatcherFailingHint = "Cards on that board are not dispatched. Check the data volume (the DataVolumeNearlyFull condition) and the gateway's log for \"kanban dispatcher: tick failed\"."
)

// usageDispatcherForPoll is the dispatcher reading this poll projects: the
// chat_metrics body's, or nil when no such body was read this poll, which
// leaves the condition as it stands. Only the leader gateway replica's body
// is read, so there is at most one.
func usageDispatcherForPoll(scraped []usageScrapedPod) *usageDispatcherReading {
	for _, s := range scraped {
		if s.Dispatcher != nil {
			return s.Dispatcher
		}
	}
	return nil
}

func kanbanDispatcherLabel(value string) string {
	return truncateForCondition(value, kanbanDispatcherLabelMax)
}

// kanbanDispatcherBoardFailing reports whether one board's streak sets the
// condition: 60 ticks in a row, or, where the plugin serves the streak's age,
// at least one poll old with at least the minimum ticks.
func kanbanDispatcherBoardFailing(f usageDispatcherFailure) bool {
	if f.Ticks >= kanbanDispatcherFailingTicks {
		return true
	}
	return f.HasAge && f.Ticks >= kanbanDispatcherMinFailingTicks && f.Seconds >= int64(kanbanDispatcherFailingAge/time.Second)
}

// kanbanDispatcherCondition is the condition a reading calls for, given the
// one the CR carries. True while a board's streak is at the threshold; False
// once every streak is below it, but only on a CR that carried the
// condition, so an agent whose dispatcher never failed gets none. nil means
// leave the CR as it is.
func kanbanDispatcherCondition(agent *agentv1alpha1.PlatformAgent, reading *usageDispatcherReading) *metav1.Condition {
	var worst *usageDispatcherFailure
	failing := 0
	for i := range reading.Failing {
		f := &reading.Failing[i]
		if !kanbanDispatcherBoardFailing(*f) {
			continue
		}
		failing++
		if worst == nil || f.Seconds > worst.Seconds || (f.Seconds == worst.Seconds && f.Ticks > worst.Ticks) ||
			(f.Seconds == worst.Seconds && f.Ticks == worst.Ticks && f.Board < worst.Board) {
			worst = f
		}
	}
	cond := &metav1.Condition{Type: kanbanDispatcherFailingCondition, ObservedGeneration: agent.Generation}
	if worst != nil {
		over := ""
		if worst.HasAge {
			over = " over " + (time.Duration(worst.Seconds) * time.Second).String()
		}
		msg := fmt.Sprintf("The kanban dispatcher failed %d ticks in a row%s on board %q (last error %s).",
			worst.Ticks, over, kanbanDispatcherLabel(worst.Board), kanbanDispatcherLabel(worst.Error))
		if failing > 1 {
			msg += fmt.Sprintf(" %d boards are failing.", failing)
		}
		cond.Status, cond.Reason, cond.Message = metav1.ConditionTrue, kanbanDispatcherReasonTicksFailing, msg+" "+kanbanDispatcherFailingHint
		return cond
	}
	if meta.FindStatusCondition(agent.Status.Conditions, kanbanDispatcherFailingCondition) == nil {
		return nil
	}
	cond.Status, cond.Reason = metav1.ConditionFalse, kanbanDispatcherReasonTicking
	cond.Message = fmt.Sprintf("No board's kanban dispatcher has failed %d ticks in a row, or %d in a streak %s old.",
		kanbanDispatcherFailingTicks, kanbanDispatcherMinFailingTicks, kanbanDispatcherFailingAge)
	return cond
}

// projectKanbanDispatcher writes the condition when its status or reason
// changed. The message is left as written while the status holds, as the data
// volume's is. The patch carries the resource version, because it replaces
// the conditions list whole; a conflict is left for the next poll.
func (p *UsageCounterPoller) projectKanbanDispatcher(ctx context.Context, agent *agentv1alpha1.PlatformAgent, reading *usageDispatcherReading) error {
	if reading == nil {
		return nil
	}
	want := kanbanDispatcherCondition(agent, reading)
	if want == nil {
		return nil
	}
	if existing := meta.FindStatusCondition(agent.Status.Conditions, kanbanDispatcherFailingCondition); existing != nil &&
		existing.Status == want.Status && existing.Reason == want.Reason {
		return nil
	}
	base := agent.DeepCopy()
	meta.SetStatusCondition(&agent.Status.Conditions, *want)
	if err := p.r.Status().Patch(ctx, agent, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
		if apierrors.IsConflict(err) {
			logf.FromContext(ctx).WithName(usagePollerLogName).Info("the CR changed under the kanban dispatcher condition write; it is retried next poll",
				"platformagent", client.ObjectKeyFromObject(agent).String())
			return nil
		}
		return fmt.Errorf("patching the %s condition: %w", kanbanDispatcherFailingCondition, err)
	}
	return nil
}
