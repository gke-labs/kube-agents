package gateway

import (
	"encoding/json"
	"log/slog"
	"slices"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// markObserver wraps the fake adapter to record MarkChanged.
type markObserver struct {
	*fakeAdapter
	changes []string
}

func (m *markObserver) MarkChanged(conversation, ask, settle string) {
	m.changes = append(m.changes, conversation+" "+ask+"/"+settle)
}

func markRig() (*Gateway, *markObserver) {
	obs := &markObserver{fakeAdapter: newFakeAdapter()}
	return &Gateway{adapter: obs, log: slog.Default(), metrics: NewMetrics()}, obs
}

// A chain: human turn h (ask message 10.0) minted child c; c ended and woke w.
func chainRecord() *SessionRecord {
	return &SessionRecord{Key: "slack:C1/10.0", Tasks: []TaskRef{
		{ID: "h", RootTaskID: "h", AskMessageID: "10.0", Children: []string{"c"}},
		{ID: "c", RootTaskID: "h", Role: taskRoleChild, ParentTaskID: "h"},
		{ID: "w", RootTaskID: "h", Role: taskRoleWake, ParentTaskID: "c"},
	}}
}

func TestMarkDiff(t *testing.T) {
	remove, add := markDiff(MarkState{Ask: "upgrade", Settle: "blocked"}, MarkState{Ask: "upgrade", Settle: "failed"})
	if !slices.Equal(remove, []string{"blocked"}) || !slices.Equal(add, []string{"failed"}) {
		t.Errorf("remove %v add %v", remove, add)
	}
	remove, add = markDiff(MarkState{}, MarkState{Ask: "fleet"})
	if len(remove) != 0 || !slices.Equal(add, []string{"fleet"}) {
		t.Errorf("remove %v add %v", remove, add)
	}
}

func TestMarkOnlyTheChainsNewestTaskCounts(t *testing.T) {
	g, obs := markRig()
	rec := chainRecord()
	g.applyTaskMark(rec, "h", MarkState{Ask: "question"})
	g.applyTaskMark(rec, "c", MarkState{Ask: "question"})
	if got := obs.reactionCalls(); len(got) != 0 {
		t.Fatalf("a minting turn's and a woken child's marks drew %v", got)
	}
	g.applyTaskMark(rec, "w", MarkState{Ask: "upgrade"})
	g.applyTaskMark(rec, "w", MarkState{Ask: "upgrade", Settle: "blocked"})
	g.applyTaskMark(rec, "w", MarkState{Ask: "upgrade", Settle: "blocked"})
	g.applyTaskMark(rec, "nope", MarkState{Ask: "fleet"})
	want := []string{"slack:C1/10.0 +upgrade@10.0", "slack:C1/10.0 +blocked@10.0"}
	if got := obs.reactionCalls(); !slices.Equal(got, want) {
		t.Errorf("reactions = %v, want %v (on the root's ask message, differences only)", got, want)
	}
	if m := rec.Tasks[0].Mark; m == nil || *m != (MarkState{Ask: "upgrade", Settle: "blocked"}) {
		t.Errorf("root mark = %+v", m)
	}
	if want := []string{"slack:C1/10.0 upgrade/", "slack:C1/10.0 upgrade/blocked"}; !slices.Equal(obs.changes, want) {
		t.Errorf("MarkChanged = %v, want %v", obs.changes, want)
	}
}

func TestMarkTerminalDefault(t *testing.T) {
	cases := []struct {
		name  string
		prev  MarkState
		state lib.TaskState
		want  []string
	}{
		{"completed takes the ask off and lifts blocked", MarkState{Ask: "fleet", Settle: "blocked"}, lib.StateCompleted,
			[]string{"slack:C1/10.0 -fleet@10.0", "slack:C1/10.0 -blocked@10.0"}},
		{"failed sets failed", MarkState{Ask: "fleet"}, lib.StateFailed,
			[]string{"slack:C1/10.0 -fleet@10.0", "slack:C1/10.0 +failed@10.0"}},
		{"a failed settle already drawn stays", MarkState{Settle: "failed"}, lib.StateCanceled, nil},
	}
	for _, c := range cases {
		g, obs := markRig()
		rec := chainRecord()
		prev := c.prev
		rec.Tasks[0].Mark = &prev
		g.applyTerminalMark(rec, "w", c.state)
		if got := obs.reactionCalls(); !slices.Equal(got, c.want) {
			t.Errorf("%s: reactions = %v, want %v", c.name, got, c.want)
		}
	}
	g, obs := markRig()
	rec := chainRecord()
	rec.Tasks[0].Mark = &MarkState{Ask: "fleet"}
	g.applyTerminalMark(rec, "h", lib.StateCompleted)
	if got := obs.reactionCalls(); len(got) != 0 {
		t.Errorf("the minting turn's terminal moved the mark: %v", got)
	}
}

// A session turn whose delegate request minted no child ends failed to
// every observer (handOffEnd), and its mark settles the same way.
func TestMarkTerminalDefaultFollowsTheHandOffEnd(t *testing.T) {
	g, obs := markRig()
	rec := &SessionRecord{Key: "slack:C1/10.0", Tasks: []TaskRef{{ID: "h", RootTaskID: "h", AskMessageID: "10.0",
		DelegationEnd: "reason: delegation-refused - not on the list", Mark: &MarkState{Ask: "fleet"}}}}
	g.applyTerminalMark(rec, "h", lib.StateCompleted)
	want := []string{"slack:C1/10.0 -fleet@10.0", "slack:C1/10.0 +failed@10.0"}
	if got := obs.reactionCalls(); !slices.Equal(got, want) {
		t.Errorf("reactions = %v, want %v", got, want)
	}
}

// A notify mark that names no chain (no update key bound to one) is dropped,
// logged and counted while a chain runs, and marks the latest chain once
// none does.
func TestMarkUnattributedNotifyIsDroppedWhileAChainRuns(t *testing.T) {
	g, obs := markRig()
	rec := chainRecord()
	rec.Tasks = append(rec.Tasks, TaskRef{ID: "h2", RootTaskID: "h2", AskMessageID: "20.0", StartedAt: time.Now()})
	rec.ActiveTask = &ActiveTask{TaskID: "h2"}
	if g.applyNotifyMark(rec, "", MarkState{Ask: "board", Settle: "blocked"}) {
		t.Error("an unattributed notify mark reported a change while a chain ran")
	}
	if got := obs.reactionCalls(); len(got) != 0 {
		t.Fatalf("a notify mark drew while a chain ran: %v", got)
	}
	if got := counterValue(t, g.metrics.notifyMarksDropped); got != 1 {
		t.Errorf("dropped marks counted %v, want 1", got)
	}
	rec.ActiveTask = nil
	g.applyNotifyMark(rec, "", MarkState{Ask: "board", Settle: "blocked"})
	want := []string{"slack:C1/10.0 +board@20.0", "slack:C1/10.0 +blocked@20.0"}
	if got := obs.reactionCalls(); !slices.Equal(got, want) {
		t.Errorf("reactions = %v, want %v (the latest chain's message)", got, want)
	}
}

// A card's notify marks the chain whose ask it was first seen under, whatever
// chain has started since: an older ask's card settles while a newer chain
// runs, and the newer ask and the observer, which holds the newer ask's
// state, are untouched.
func TestMarkNotifyMarksTheChainItsKeyIsBoundTo(t *testing.T) {
	g, obs := markRig()
	rec := &SessionRecord{Key: "slack:C1/10.0", Tasks: []TaskRef{{ID: "a", RootTaskID: "a", AskMessageID: "10.0"}}}
	if !g.applyNotifyMark(rec, "plan-1", MarkState{Ask: "investigate"}) {
		t.Fatal("the card's first mark drew nothing")
	}
	rec.Tasks = append(rec.Tasks, TaskRef{ID: "b", RootTaskID: "b", AskMessageID: "20.0", Mark: &MarkState{Ask: "upgrade"}})
	rec.ActiveTask = &ActiveTask{TaskID: "b"}
	if !g.applyNotifyMark(rec, "plan-1", MarkState{Ask: "investigate", Settle: "blocked"}) {
		t.Fatal("the card's ⏸️ was dropped while a newer chain ran")
	}
	if !g.applyNotifyMark(rec, "plan-1", MarkState{}) {
		t.Fatal("the card's final settle was dropped while a newer chain ran")
	}
	want := []string{"slack:C1/10.0 +investigate@10.0", "slack:C1/10.0 +blocked@10.0",
		"slack:C1/10.0 -investigate@10.0", "slack:C1/10.0 -blocked@10.0"}
	if got := obs.reactionCalls(); !slices.Equal(got, want) {
		t.Errorf("reactions = %v, want %v (all on the older ask)", got, want)
	}
	if m := rec.Tasks[0].Mark; m != nil {
		t.Errorf("older ask's stored mark = %+v, want settled", m)
	}
	if m := rec.Tasks[1].Mark; m == nil || *m != (MarkState{Ask: "upgrade"}) {
		t.Errorf("newer ask's stored mark = %+v, want untouched", m)
	}
	if want := []string{"slack:C1/10.0 investigate/"}; !slices.Equal(obs.changes, want) {
		t.Errorf("MarkChanged = %v, want %v (the newer ask's state held)", obs.changes, want)
	}
	if got := counterValue(t, g.metrics.notifyMarksDropped); got != 0 {
		t.Errorf("dropped marks counted %v, want 0", got)
	}
}

// The binding is on the record, so a gateway restarted between a card's
// first mark and its settle still marks the ask the card belongs to.
func TestMarkNotifyBindingSurvivesARestart(t *testing.T) {
	g, _ := markRig()
	rec := &SessionRecord{Key: "slack:C1/10.0", Tasks: []TaskRef{{ID: "a", RootTaskID: "a", AskMessageID: "10.0"}}}
	g.applyNotifyMark(rec, "plan-1", MarkState{Ask: "investigate"})
	data, err := json.Marshal(rec)
	if err != nil {
		t.Fatal(err)
	}
	var stored SessionRecord
	if err := json.Unmarshal(data, &stored); err != nil {
		t.Fatal(err)
	}
	stored.Tasks = append(stored.Tasks, TaskRef{ID: "b", RootTaskID: "b", AskMessageID: "20.0"})
	stored.ActiveTask = &ActiveTask{TaskID: "b"}
	restarted, obs := markRig()
	if !restarted.applyNotifyMark(&stored, "plan-1", MarkState{}) {
		t.Fatal("the settle after a restart drew nothing")
	}
	if got, want := obs.reactionCalls(), []string{"slack:C1/10.0 -investigate@10.0"}; !slices.Equal(got, want) {
		t.Errorf("reactions = %v, want %v", got, want)
	}
}

// A key bound to an ask follows it into a chain that continues it (a click
// or a typed answer marks the ask it answered): the mark draws from what
// that chain last drew, and is dropped while that chain runs, since the
// running chain is the ask's newest writer.
func TestMarkNotifyKeyFollowsItsAskIntoAContinuingChain(t *testing.T) {
	g, obs := markRig()
	rec := &SessionRecord{Key: "slack:C1/10.0", Tasks: []TaskRef{
		{ID: "a", RootTaskID: "a", AskMessageID: "10.0", NotifyKeys: []string{"plan-1"}, Mark: &MarkState{Ask: "investigate", Settle: "blocked"}},
		{ID: "c", RootTaskID: "c", AskMessageID: "10.0", Mark: &MarkState{Ask: "investigate"}},
	}}
	rec.ActiveTask = &ActiveTask{TaskID: "c"}
	if g.applyNotifyMark(rec, "plan-1", MarkState{}) {
		t.Error("a notify mark drew on an ask whose own chain is running")
	}
	if got := counterValue(t, g.metrics.notifyMarksDropped); got != 0 {
		t.Errorf("a mark dropped for its own chain running was counted unattributed (%v)", got)
	}
	rec.ActiveTask = nil
	g.applyNotifyMark(rec, "plan-1", MarkState{})
	if got, want := obs.reactionCalls(), []string{"slack:C1/10.0 -investigate@10.0"}; !slices.Equal(got, want) {
		t.Errorf("reactions = %v, want %v (from the continuing chain's state)", got, want)
	}
	if rec.Tasks[1].Mark != nil {
		t.Errorf("continuing chain's mark = %+v, want settled", rec.Tasks[1].Mark)
	}
}

// The Agent Session follows the newest chain, but holds processing while an
// older chain's cards are still open, and stops once every chain's have
// settled.
func TestMarkSessionHoldsUntilEveryChainsCardsSettle(t *testing.T) {
	g, obs := markRig()
	rec := &SessionRecord{Key: "slack:C1/10.0", Tasks: []TaskRef{
		{ID: "a", RootTaskID: "a", AskMessageID: "10.0", NotifyKeys: []string{"plan-1"}, Mark: &MarkState{Ask: "investigate"}},
		{ID: "b", RootTaskID: "b", AskMessageID: "20.0", Mark: &MarkState{Ask: "upgrade"}},
	}}
	g.applyTerminalMark(rec, "b", lib.StateCompleted)
	g.applyNotifyMark(rec, "plan-1", MarkState{})
	if want := []string{"slack:C1/10.0 investigate/", "slack:C1/10.0 /"}; !slices.Equal(obs.changes, want) {
		t.Errorf("MarkChanged = %v, want %v", obs.changes, want)
	}
}

// An older ask holds the session only while a keyed notify can still settle
// it: one whose open mark no notify key reaches (a keyless card, or a record
// from before keys were bound) would hold Working… for good.
func TestMarkSessionIgnoresAnOlderAskNoKeyReaches(t *testing.T) {
	g, obs := markRig()
	rec := &SessionRecord{Key: "slack:C1/10.0", Tasks: []TaskRef{
		{ID: "a", RootTaskID: "a", AskMessageID: "10.0", Mark: &MarkState{Ask: "investigate"}},
		{ID: "b", RootTaskID: "b", AskMessageID: "20.0", Mark: &MarkState{Ask: "upgrade"}},
	}}
	g.applyTerminalMark(rec, "b", lib.StateCompleted)
	if want := []string{"slack:C1/10.0 /"}; !slices.Equal(obs.changes, want) {
		t.Errorf("MarkChanged = %v, want %v (the session closes with the newest chain)", obs.changes, want)
	}
}

// TestMarkAnOlderChainsLateTerminalDoesNotTellTheObserver: an older chain's
// terminal redraws its own ask, but the observer holds the latest ask's
// state, so a newer ask blocked on the person keeps its session suspended.
func TestMarkAnOlderChainsLateTerminalDoesNotTellTheObserver(t *testing.T) {
	g, obs := markRig()
	rec := &SessionRecord{Key: "slack:C1/10.0", Tasks: []TaskRef{
		{ID: "a", RootTaskID: "a", AskMessageID: "10.0", Mark: &MarkState{Ask: "fleet"}},
		{ID: "b", RootTaskID: "b", AskMessageID: "20.0", Mark: &MarkState{Ask: "upgrade", Settle: "blocked"}},
	}}
	g.applyTerminalMark(rec, "a", lib.StateCompleted)
	if got, want := obs.reactionCalls(), []string{"slack:C1/10.0 -fleet@10.0"}; !slices.Equal(got, want) {
		t.Errorf("reactions = %v, want %v", got, want)
	}
	if len(obs.changes) != 0 {
		t.Errorf("the older chain's terminal told the observer %v", obs.changes)
	}
	g.applyTerminalMark(rec, "b", lib.StateCompleted)
	if want := []string{"slack:C1/10.0 /"}; !slices.Equal(obs.changes, want) {
		t.Errorf("changes = %v, want %v (the latest ask's end)", obs.changes, want)
	}
}
