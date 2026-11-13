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
	"strings"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func dispatcherCondition(h *usageHarness) *metav1.Condition {
	return meta.FindStatusCondition(h.agent().Status.Conditions, kanbanDispatcherFailingCondition)
}

func dispatcherHarness(t *testing.T) *usageHarness {
	t.Helper()
	return dataVolumeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)))
}

// The threshold is one poll interval of ticks at the dispatch interval the
// operator configures: 60 at 5 minutes and 5 seconds.
func TestKanbanDispatcher_ThresholdIsOnePollOfTicks(t *testing.T) {
	if kanbanDispatcherFailingTicks != 60 {
		t.Fatalf("kanbanDispatcherFailingTicks = %d, want 60", kanbanDispatcherFailingTicks)
	}
}

// A streak below the threshold writes nothing; at the threshold the condition
// turns True with the board and the error class; once every streak is below it
// the condition turns False. Ready is never written.
func TestKanbanDispatcher_ConditionSetsAtTheThresholdAndClears(t *testing.T) {
	h := dispatcherHarness(t)
	steps := []struct {
		ticks      int64
		want       metav1.ConditionStatus
		wantReason string
	}{
		{0, "", ""},
		{kanbanDispatcherFailingTicks - 1, "", ""},
		{kanbanDispatcherFailingTicks, metav1.ConditionTrue, kanbanDispatcherReasonTicksFailing},
		{kanbanDispatcherFailingTicks * 3, metav1.ConditionTrue, kanbanDispatcherReasonTicksFailing},
		{0, metav1.ConditionFalse, kanbanDispatcherReasonTicking},
		{kanbanDispatcherFailingTicks - 1, metav1.ConditionFalse, kanbanDispatcherReasonTicking},
		{kanbanDispatcherFailingTicks, metav1.ConditionTrue, kanbanDispatcherReasonTicksFailing},
	}
	for i, step := range steps {
		if step.ticks == 0 {
			h.stub.setDispatcher(chatAddr())
		} else {
			h.stub.setDispatcher(chatAddr(), usageDispatcherFailure{Board: "default", Error: "OperationalError", Ticks: step.ticks})
		}
		h.poll(5 * (i + 1))
		cond := dispatcherCondition(h)
		if step.want == "" {
			if cond != nil {
				t.Fatalf("step %d (%d ticks): condition %+v, want none", i, step.ticks, cond)
			}
			continue
		}
		if cond == nil || cond.Status != step.want || cond.Reason != step.wantReason {
			t.Fatalf("step %d (%d ticks): condition %+v, want %s/%s", i, step.ticks, cond, step.want, step.wantReason)
		}
		if meta.FindStatusCondition(h.agent().Status.Conditions, "Ready") != nil {
			t.Fatalf("step %d: the poller wrote Ready", i)
		}
	}
}

// The True message names the board, the streak and the last error class, and
// it is held while the status holds.
func TestKanbanDispatcher_MessageNamesTheBoardAndIsHeld(t *testing.T) {
	h := dispatcherHarness(t)
	h.stub.setDispatcher(chatAddr(), usageDispatcherFailure{Board: "default", Error: "OperationalError", Ticks: kanbanDispatcherFailingTicks})
	h.poll(5)
	cond := dispatcherCondition(h)
	if cond == nil || !strings.Contains(cond.Message, `failed 60 ticks in a row on board "default" (last error OperationalError)`) || !strings.Contains(cond.Message, kanbanDispatcherFailingHint) {
		t.Fatalf("True message = %+v", cond)
	}
	h.stub.setDispatcher(chatAddr(), usageDispatcherFailure{Board: "default", Error: "DatabaseError", Ticks: kanbanDispatcherFailingTicks * 2})
	h.poll(10)
	if again := dispatcherCondition(h); again.Message != cond.Message {
		t.Fatalf("a held True condition was rewritten: %q -> %q", cond.Message, again.Message)
	}
}

// Of several failing boards the message names the longest streak and counts
// the boards at the threshold; a board below it is not counted.
func TestKanbanDispatcher_SeveralBoardsNameTheWorst(t *testing.T) {
	h := dispatcherHarness(t)
	h.stub.setDispatcher(chatAddr(),
		usageDispatcherFailure{Board: "a", Error: "OSError", Ticks: kanbanDispatcherFailingTicks},
		usageDispatcherFailure{Board: "b", Error: "OperationalError", Ticks: kanbanDispatcherFailingTicks + 7},
		usageDispatcherFailure{Board: "c", Error: "OSError", Ticks: 1},
	)
	h.poll(5)
	cond := dispatcherCondition(h)
	if cond == nil || !strings.Contains(cond.Message, `on board "b" (last error OperationalError)`) || !strings.Contains(cond.Message, "2 boards are failing.") {
		t.Fatalf("message = %+v", cond)
	}
}

// A poll that reads no chat_metrics body leaves the condition as it is: a
// down listener neither sets nor clears it.
func TestKanbanDispatcher_AnUnreadListenerLeavesTheCondition(t *testing.T) {
	h := dispatcherHarness(t)
	h.stub.setDispatcher(chatAddr(), usageDispatcherFailure{Board: "default", Error: "OSError", Ticks: kanbanDispatcherFailingTicks})
	h.poll(5)
	h.stub.fail(chatAddr(), usageScrapeKindConnect)
	h.poll(10)
	if cond := dispatcherCondition(h); cond == nil || cond.Status != metav1.ConditionTrue {
		t.Fatalf("after an unread poll: condition %+v, want it still True", cond)
	}
}

// A long label is cut in the message.
func TestKanbanDispatcher_ALongBoardIsCut(t *testing.T) {
	h := dispatcherHarness(t)
	h.stub.setDispatcher(chatAddr(), usageDispatcherFailure{Board: strings.Repeat("x", kanbanDispatcherLabelMax+50), Error: "OSError", Ticks: kanbanDispatcherFailingTicks})
	h.poll(5)
	cond := dispatcherCondition(h)
	if cond == nil || strings.Contains(cond.Message, strings.Repeat("x", kanbanDispatcherLabelMax+1)) || !strings.Contains(cond.Message, strings.Repeat("x", kanbanDispatcherLabelMax)+"…") {
		t.Fatalf("message = %+v", cond)
	}
}

// A locked board: each tick waits out the SQLite busy timeout, so failures
// come about 125 s apart. The condition turns True at the first poll after
// the streak is a poll old with at least the minimum ticks, long before 60.
func TestKanbanDispatcher_ALockedBoardSetsItByTheStreaksAge(t *testing.T) {
	h := dispatcherHarness(t)
	locked := func(ticks, seconds int64) usageDispatcherFailure {
		return usageDispatcherFailure{Board: "default", Error: "OperationalError", Ticks: ticks, Seconds: seconds, HasAge: true}
	}
	h.stub.setDispatcher(chatAddr(), locked(2, 250))
	h.poll(5)
	if cond := dispatcherCondition(h); cond != nil {
		t.Fatalf("at 2 ticks over 250 s: condition %+v, want none", cond)
	}
	h.stub.setDispatcher(chatAddr(), locked(3, 375))
	h.poll(10)
	cond := dispatcherCondition(h)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != kanbanDispatcherReasonTicksFailing {
		t.Fatalf("at 3 ticks over 375 s: condition %+v, want True", cond)
	}
	if !strings.Contains(cond.Message, `failed 3 ticks in a row over 6m15s on board "default"`) {
		t.Fatalf("message = %q", cond.Message)
	}
	h.stub.setDispatcher(chatAddr())
	h.poll(15)
	if cond := dispatcherCondition(h); cond == nil || cond.Status != metav1.ConditionFalse {
		t.Fatalf("after a good tick: condition %+v, want False", cond)
	}
}

// One slow failure (or one before a tick that hangs) never sets it, however
// old, nor do two; recovery leaves the CR without the condition.
func TestKanbanDispatcher_FewerThanTheMinimumTicksNeverSetIt(t *testing.T) {
	h := dispatcherHarness(t)
	for i, ticks := range []int64{1, kanbanDispatcherMinFailingTicks - 1} {
		h.stub.setDispatcher(chatAddr(), usageDispatcherFailure{Board: "default", Error: "OperationalError", Ticks: ticks, Seconds: 900, HasAge: true})
		h.poll(5 * (i + 1))
		if cond := dispatcherCondition(h); cond != nil {
			t.Fatalf("at %d ticks over 900 s: condition %+v, want none", ticks, cond)
		}
	}
	h.stub.setDispatcher(chatAddr())
	h.poll(15)
	if cond := dispatcherCondition(h); cond != nil {
		t.Fatalf("after recovery: condition %+v, want none", cond)
	}
}

// A plugin without the age family keeps the tick rule: an old streak of 30
// ticks does not set it, 60 does.
func TestKanbanDispatcher_WithoutTheAgeTheTickRuleStands(t *testing.T) {
	h := dispatcherHarness(t)
	h.stub.setDispatcher(chatAddr(), usageDispatcherFailure{Board: "default", Error: "OSError", Ticks: kanbanDispatcherFailingTicks / 2})
	h.poll(5)
	if cond := dispatcherCondition(h); cond != nil {
		t.Fatalf("at 30 ticks with no age: condition %+v, want none", cond)
	}
	h.stub.setDispatcher(chatAddr(), usageDispatcherFailure{Board: "default", Error: "OSError", Ticks: kanbanDispatcherFailingTicks})
	h.poll(10)
	if cond := dispatcherCondition(h); cond == nil || cond.Status != metav1.ConditionTrue || strings.Contains(cond.Message, " over ") {
		t.Fatalf("at 60 ticks with no age: condition %+v, want True with no age in the message", cond)
	}
}
