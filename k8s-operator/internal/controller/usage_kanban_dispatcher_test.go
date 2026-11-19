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

// The True message names the board, the last error class and the rule, and no
// tick count or age. A longer streak on the same board with the same error is
// the same message, so a later poll writes nothing; a change of the error class
// or of the board is a new message, and the poll that reads it writes it.
func TestKanbanDispatcher_MessageNamesTheBoardAndFollowsIt(t *testing.T) {
	h := dispatcherHarness(t)
	h.stub.setDispatcher(chatAddr(), usageDispatcherFailure{Board: "default", Error: "OperationalError", Ticks: kanbanDispatcherFailingTicks})
	h.poll(5)
	cond := dispatcherCondition(h)
	want := `The kanban dispatcher's ticks fail on board "default" (last error OperationalError): 60 or more in a row, or 3 or more in a streak at least 5m0s old.`
	if cond == nil || !strings.HasPrefix(cond.Message, want+" ") || !strings.Contains(cond.Message, kanbanDispatcherFailingHint) {
		t.Fatalf("True message = %+v", cond)
	}
	// A later poll: the streak is longer and has an age. Nothing in the message changes.
	written, patches := cond.Message, h.patches
	h.stub.setDispatcher(chatAddr(), usageDispatcherFailure{Board: "default", Error: "OperationalError", Ticks: kanbanDispatcherFailingTicks * 2, Seconds: 600, HasAge: true})
	h.poll(10)
	if again := dispatcherCondition(h); again.Message != written || h.patches != patches {
		t.Fatalf("a longer streak rewrote the condition: %q -> %q, %d -> %d patches", written, again.Message, patches, h.patches)
	}
	// The error class changes: the message on the CR names the new one.
	h.stub.setDispatcher(chatAddr(), usageDispatcherFailure{Board: "default", Error: "DatabaseError", Ticks: kanbanDispatcherFailingTicks * 3, Seconds: 900, HasAge: true})
	h.poll(15)
	if again := dispatcherCondition(h); !strings.Contains(again.Message, `on board "default" (last error DatabaseError)`) || again.Reason != kanbanDispatcherReasonTicksFailing {
		t.Fatalf("after the error class changed: condition %+v, want the new class in the message", again)
	}
	// Another board is the failing one now, under the same reason. An old
	// transition time first, so that a write that moved it would show.
	agent := h.agent()
	transition := metav1.NewTime(usageClock(0).Add(-48 * time.Hour).Truncate(time.Second))
	meta.FindStatusCondition(agent.Status.Conditions, kanbanDispatcherFailingCondition).LastTransitionTime = transition
	if err := h.cl.Status().Update(context.Background(), agent); err != nil {
		t.Fatalf("setting an old transition time: %v", err)
	}
	h.stub.setDispatcher(chatAddr(), usageDispatcherFailure{Board: "audits", Error: "DatabaseError", Ticks: kanbanDispatcherFailingTicks})
	h.poll(20)
	again := dispatcherCondition(h)
	if !strings.Contains(again.Message, `on board "audits"`) || strings.Contains(again.Message, `"default"`) {
		t.Fatalf("after another board began to fail: message %q, want it to name audits only", again.Message)
	}
	if !again.LastTransitionTime.Equal(&transition) {
		t.Fatalf("a message change moved lastTransitionTime: %v -> %v", transition, again.LastTransitionTime)
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
	if !strings.Contains(cond.Message, `ticks fail on board "default"`) || strings.Contains(cond.Message, "6m15s") {
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

// A fully locked board: its first tick runs past the limit before any
// failure. The plugin reports it stuck, and the condition turns True at that
// poll, with no failed tick; it holds while the board stays stuck and clears
// after a good tick.
func TestKanbanDispatcher_AStuckTickSetsItBeforeAnyFailure(t *testing.T) {
	h := dispatcherHarness(t)
	h.stub.setDispatcherStuck(chatAddr(), []usageDispatcherStuck{{Board: "default", Seconds: 400}})
	h.poll(5)
	cond := dispatcherCondition(h)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != kanbanDispatcherReasonTickStuck {
		t.Fatalf("stuck at 400 s: condition %+v, want True with reason %s", cond, kanbanDispatcherReasonTickStuck)
	}
	if !strings.Contains(cond.Message, `tick on board "default" waited on its database (it ran past 5m0s, or failed on a lock after a busy timeout)`) || !strings.Contains(cond.Message, "and no good tick has followed; the board may be locked.") {
		t.Fatalf("message = %q", cond.Message)
	}
	written := cond.Message
	// Still stuck, now with one failure below the streak rules.
	h.stub.setDispatcherStuck(chatAddr(), []usageDispatcherStuck{{Board: "default", Seconds: 2160}},
		usageDispatcherFailure{Board: "default", Error: "OperationalError", Ticks: 1, Seconds: 1440, HasAge: true})
	h.poll(10)
	// The message is written once per reason; 36 minutes in, it is still true,
	// because it gives no duration.
	if cond := dispatcherCondition(h); cond == nil || cond.Status != metav1.ConditionTrue || cond.Message != written {
		t.Fatalf("stuck with one failure: condition %+v, want True held with the message as written", cond)
	}
	h.stub.setDispatcher(chatAddr())
	h.poll(15)
	if cond := dispatcherCondition(h); cond == nil || cond.Status != metav1.ConditionFalse || !strings.Contains(cond.Message, "no board is stuck on its database") {
		t.Fatalf("after a good tick: condition %+v, want False", cond)
	}
}

// A stuck board is named before a board in a streak, under its own reason,
// and both count.
func TestKanbanDispatcher_AStuckBoardIsNamedBeforeAStreak(t *testing.T) {
	h := dispatcherHarness(t)
	h.stub.setDispatcherStuck(chatAddr(), []usageDispatcherStuck{{Board: "locked", Seconds: 600}},
		usageDispatcherFailure{Board: "default", Error: "OSError", Ticks: kanbanDispatcherFailingTicks})
	h.poll(5)
	cond := dispatcherCondition(h)
	if cond == nil || cond.Reason != kanbanDispatcherReasonTickStuck || !strings.Contains(cond.Message, `tick on board "locked" waited on its database`) ||
		!strings.Contains(cond.Message, "2 boards are failing") {
		t.Fatalf("condition %+v, want the stuck board named under %s and two boards counted", cond, kanbanDispatcherReasonTickStuck)
	}
}

// The stuck message follows what it names. A board that stays stuck is one
// message however long it waits, so a later poll writes nothing. When another
// board is the stuck one, or the count of failing boards changes, the reason is
// the same and the message is not: the poll that reads it writes it.
func TestKanbanDispatcher_TheStuckMessageFollowsTheBoardAndTheCount(t *testing.T) {
	h := dispatcherHarness(t)
	h.stub.setDispatcherStuck(chatAddr(), []usageDispatcherStuck{{Board: "locked", Seconds: 400}})
	h.poll(5)
	first := dispatcherCondition(h)
	if first == nil || first.Reason != kanbanDispatcherReasonTickStuck || !strings.Contains(first.Message, `tick on board "locked" waited`) {
		t.Fatalf("condition %+v, want %s naming locked", first, kanbanDispatcherReasonTickStuck)
	}
	patches := h.patches
	h.stub.setDispatcherStuck(chatAddr(), []usageDispatcherStuck{{Board: "locked", Seconds: 4000}})
	h.poll(10)
	if again := dispatcherCondition(h); again.Message != first.Message || h.patches != patches {
		t.Fatalf("a board that stayed stuck rewrote the condition: %q -> %q, %d -> %d patches", first.Message, again.Message, patches, h.patches)
	}
	// "locked" recovered and "queue" is stuck: the same reason, another board.
	h.stub.setDispatcherStuck(chatAddr(), []usageDispatcherStuck{{Board: "queue", Seconds: 320}})
	h.poll(15)
	moved := dispatcherCondition(h)
	if moved.Reason != kanbanDispatcherReasonTickStuck || !strings.Contains(moved.Message, `tick on board "queue" waited`) || strings.Contains(moved.Message, `"locked"`) {
		t.Fatalf("after another board got stuck: condition %+v, want it to name queue only", moved)
	}
	if moved.Status != first.Status {
		t.Fatalf("the status changed with the board: %s -> %s", first.Status, moved.Status)
	}
	// A second board fails: the count joins the message.
	h.stub.setDispatcherStuck(chatAddr(), []usageDispatcherStuck{{Board: "queue", Seconds: 620}},
		usageDispatcherFailure{Board: "default", Error: "OSError", Ticks: kanbanDispatcherFailingTicks})
	h.poll(20)
	if counted := dispatcherCondition(h); !strings.Contains(counted.Message, "2 boards are failing.") {
		t.Fatalf("after a second board failed: message %q, want the count of 2", counted.Message)
	}
}

// Two streaks that began in the same second, as on a full disk, are named by
// board. Their tick counts differ by a tick from poll to poll; a name chosen by
// the count would move between the boards and write the condition each time.
func TestKanbanDispatcher_StreaksOfOneAgeAreNamedByBoard(t *testing.T) {
	h := dispatcherHarness(t)
	streak := func(board string, ticks int64) usageDispatcherFailure {
		return usageDispatcherFailure{Board: board, Error: "OSError", Ticks: ticks, Seconds: 900, HasAge: true}
	}
	h.stub.setDispatcher(chatAddr(), streak("audits", 70), streak("default", 71))
	h.poll(5)
	first := dispatcherCondition(h)
	if first == nil || !strings.Contains(first.Message, `on board "audits"`) {
		t.Fatalf("condition %+v, want audits named: the first board by name", first)
	}
	patches := h.patches
	h.stub.setDispatcher(chatAddr(), streak("audits", 131), streak("default", 130))
	h.poll(10)
	if again := dispatcherCondition(h); again.Message != first.Message || h.patches != patches {
		t.Fatalf("a tick's difference between two boards rewrote the condition: %q -> %q, %d -> %d patches", first.Message, again.Message, patches, h.patches)
	}
}

// A board that is stuck and also meets a streak rule, as a locked board does
// after about an hour, stays under the stuck reason with the lock hint, and
// counts once.
func TestKanbanDispatcher_ABoardBothStuckAndInAStreakCountsOnce(t *testing.T) {
	h := dispatcherHarness(t)
	locked := usageDispatcherFailure{Board: "default", Error: "OperationalError", Ticks: kanbanDispatcherMinFailingTicks, Seconds: 2900, HasAge: true}
	h.stub.setDispatcherStuck(chatAddr(), []usageDispatcherStuck{{Board: "default", Seconds: 3100}}, locked)
	h.poll(5)
	cond := dispatcherCondition(h)
	if cond == nil || cond.Reason != kanbanDispatcherReasonTickStuck || !strings.Contains(cond.Message, "Look for another process") {
		t.Fatalf("condition %+v, want %s with the lock hint", cond, kanbanDispatcherReasonTickStuck)
	}
	if strings.Contains(cond.Message, "boards are failing") {
		t.Fatalf("message = %q, want one board counted once", cond.Message)
	}
	// With a second board in a streak, the count is two, not three.
	h2 := dispatcherHarness(t)
	h2.stub.setDispatcherStuck(chatAddr(), []usageDispatcherStuck{{Board: "default", Seconds: 3100}}, locked,
		usageDispatcherFailure{Board: "other", Error: "OSError", Ticks: kanbanDispatcherFailingTicks})
	h2.poll(5)
	if cond := dispatcherCondition(h2); cond == nil || !strings.Contains(cond.Message, "2 boards are failing") {
		t.Fatalf("condition %+v, want two boards counted", cond)
	}
}

// The message and its hint follow the cause: a streak, then a lock on the same
// board, then a streak again. Each change of reason rewrites the condition.
func TestKanbanDispatcher_TheMessageFollowsTheCause(t *testing.T) {
	h := dispatcherHarness(t)
	streak := usageDispatcherFailure{Board: "default", Error: "OSError", Ticks: kanbanDispatcherFailingTicks}
	h.stub.setDispatcher(chatAddr(), streak)
	h.poll(5)
	cond := dispatcherCondition(h)
	if cond == nil || cond.Reason != kanbanDispatcherReasonTicksFailing || !strings.Contains(cond.Message, "tick failed") {
		t.Fatalf("a streak: condition %+v, want %s with the log hint", cond, kanbanDispatcherReasonTicksFailing)
	}
	h.stub.setDispatcherStuck(chatAddr(), []usageDispatcherStuck{{Board: "default", Seconds: 400}}, streak)
	h.poll(10)
	cond = dispatcherCondition(h)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != kanbanDispatcherReasonTickStuck || !strings.Contains(cond.Message, "Look for another process") {
		t.Fatalf("then a lock: condition %+v, want %s with the lock hint", cond, kanbanDispatcherReasonTickStuck)
	}
	h.stub.setDispatcher(chatAddr(), streak)
	h.poll(15)
	cond = dispatcherCondition(h)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != kanbanDispatcherReasonTicksFailing || strings.Contains(cond.Message, "Look for another process") {
		t.Fatalf("then a streak again: condition %+v, want %s without the lock hint", cond, kanbanDispatcherReasonTicksFailing)
	}
}

// The stuck message gives no duration: it is written once per reason, so it
// must read the same however long the board has been stuck. That also covers a
// board that recovers and locks again between two polls.
func TestKanbanDispatcher_TheStuckMessageGivesNoDuration(t *testing.T) {
	messages := map[string]int64{}
	for _, seconds := range []int64{0, 45, 312, 2160, 7260} {
		h := dispatcherHarness(t)
		h.stub.setDispatcherStuck(chatAddr(), []usageDispatcherStuck{{Board: "default", Seconds: seconds}})
		h.poll(5)
		cond := dispatcherCondition(h)
		if cond == nil || cond.Reason != kanbanDispatcherReasonTickStuck {
			t.Fatalf("stuck for %d s: condition %+v, want reason %s", seconds, cond, kanbanDispatcherReasonTickStuck)
		}
		messages[cond.Message] = seconds
	}
	if len(messages) != 1 {
		t.Errorf("the stuck message changes with the duration: %v", messages)
	}
}
