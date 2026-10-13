package gateway

import (
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
	return &Gateway{adapter: obs, log: slog.Default()}, obs
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

func TestMarkNotifyIsDroppedWhileAChainRuns(t *testing.T) {
	g, obs := markRig()
	rec := chainRecord()
	rec.Tasks = append(rec.Tasks, TaskRef{ID: "h2", RootTaskID: "h2", AskMessageID: "20.0", StartedAt: time.Now()})
	rec.ActiveTask = &ActiveTask{TaskID: "h2"}
	g.applyNotifyMark(rec, MarkState{Ask: "board", Settle: "blocked"})
	if got := obs.reactionCalls(); len(got) != 0 {
		t.Fatalf("a notify mark drew while a chain ran: %v", got)
	}
	rec.ActiveTask = nil
	g.applyNotifyMark(rec, MarkState{Ask: "board", Settle: "blocked"})
	want := []string{"slack:C1/10.0 +board@20.0", "slack:C1/10.0 +blocked@20.0"}
	if got := obs.reactionCalls(); !slices.Equal(got, want) {
		t.Errorf("reactions = %v, want %v (the latest chain's message)", got, want)
	}
}
