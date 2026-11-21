package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A fan-out child's rolling line (#6535): each child keeps its line on its
// own entry rather than on the active task, and moves it with its own
// working and progress events until its terminal closes it, as a single
// delegated child's does. A stopped child's "🛑 stopped" line is never
// written again.

// clusterLineNote is the note a batch child to the cluster profile carries.
var clusterLineNote = fmt.Sprintf(delegatedProfileLineNote, "cluster")

// eventItem is a relay item carrying an event of origin's task from
// addressee, as the relay durable would deliver it, with no delivery to
// settle: a test hands a batch of them to relayBatch directly.
func eventItem(t *testing.T, origin *lib.Envelope, addressee string, kind lib.Kind, payload any) relayItem {
	t.Helper()
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	from := lib.Party{Session: addressee, AgentType: "test-executor"}
	var env *lib.Envelope
	if kind == lib.KindStatusUpdate {
		env, err = lib.NewStatusUpdateEnvelope(from, origin.TaskID, origin.ContextID, origin.CorrelationID, raw)
	} else {
		env, err = lib.NewArtifactUpdateEnvelope(from, origin.TaskID, origin.ContextID, origin.CorrelationID, raw)
	}
	if err != nil {
		t.Fatal(err)
	}
	return relayItem{env: env, subject: lib.TaskEventsSubject(addressee, origin.TaskID)}
}

// workingItem is a non-final working status of origin's task.
func workingItem(t *testing.T, origin *lib.Envelope, addressee string) relayItem {
	t.Helper()
	return eventItem(t, origin, addressee, lib.KindStatusUpdate,
		lib.StatusUpdate{TaskID: origin.TaskID, ContextID: origin.ContextID, Status: lib.TaskStatus{State: lib.StateWorking}})
}

// progressItem is a progress artifact of origin's task saying text.
func progressItem(t *testing.T, origin *lib.Envelope, addressee, text string) relayItem {
	t.Helper()
	return eventItem(t, origin, addressee, lib.KindArtifactUpdate,
		lib.ArtifactUpdate{TaskID: origin.TaskID, ContextID: origin.ContextID,
			Artifact: lib.Artifact{ArtifactID: "p-" + text, Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: text}}}})
}

// lastEditIs reports whether messageID's newest edit reads want.
func lastEditIs(r *rig, messageID, want string) func() bool {
	return func() bool {
		edits := r.adapter.editsOf(messageID)
		return len(edits) > 0 && edits[len(edits)-1] == want
	}
}

// TestAFanOutChildsLineMovesWithItsOwnEvents: each child's line moves to
// working and then to its progress, and its terminal closes it, carrying its
// delegation note throughout; the other child's line is left alone.
func TestAFanOutChildsLineMovesWithItsOwnEvents(t *testing.T) {
	t.Parallel()
	r, spawn, _ := startFanOutRig(t, 2)
	ctx := context.Background()
	conv := "discord:g1/t-fanout-line-moves"
	f := fanOut(t, r, spawn, conv, clusterTask("web-1", "a"), clusterTask("web-2", "b"))
	for i, k := range f.kids {
		if k.StatusMsgID == "" {
			t.Fatalf("child %d has no line: %+v", i, k)
		}
	}
	exec := r.execFor(t, f.envs[0], "cluster")
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	working := withLineNote(statusLine(lib.StateWorking, ""), clusterLineNote)
	waitFor(t, "the child's working line", lastEditIs(r, f.kids[0].StatusMsgID, working))
	if err := exec.PublishArtifact(ctx, lib.Artifact{ArtifactID: "p-1", Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "checking nodes"}}}); err != nil {
		t.Fatal(err)
	}
	progress := withLineNote(statusLine(lib.StateWorking, "checking nodes"), clusterLineNote)
	waitFor(t, "the child's progress line", lastEditIs(r, f.kids[0].StatusMsgID, progress))
	if got := r.adapter.editsOf(f.kids[1].StatusMsgID); len(got) != 0 {
		t.Fatalf("the other child's line was edited: %q", got)
	}
	completeTask(t, exec, "web-1 is green")
	done := withLineNote(terminalLine(lib.StateCompleted, ""), clusterLineNote)
	waitFor(t, "the child's terminal line", lastEditIs(r, f.kids[0].StatusMsgID, done))
	if got, want := r.adapter.editsOf(f.kids[0].StatusMsgID), []string{working, progress, done}; strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("child line edits = %q, want %q", got, want)
	}
}

// TestAFanOutChildsLineKeepsTheDefaultModesQuiet: under the default display
// mode a child's line moves to working, as the active task's does, and a
// progress artifact edits nothing: the line carries state, not narration.
func TestAFanOutChildsLineKeepsTheDefaultModesQuiet(t *testing.T) {
	t.Parallel()
	r, spawn, _ := startStopRig(t, func(c *Config) { c.DisplayMode = displayModeDefault })
	ctx := context.Background()
	conv := "discord:g1/t-fanout-line-default"
	f := fanOut(t, r, spawn, conv, clusterTask("web-1", "a"), clusterTask("web-2", "b"))
	exec := r.execFor(t, f.envs[0], "cluster")
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	working := withLineNote(statusLine(lib.StateWorking, ""), clusterLineNote)
	waitFor(t, "the child's working line", lastEditIs(r, f.kids[0].StatusMsgID, working))
	if err := exec.PublishArtifact(ctx, lib.Artifact{ArtifactID: "p-1", Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "checking nodes"}}}); err != nil {
		t.Fatal(err)
	}
	completeTask(t, exec, "web-1 is green")
	done := withLineNote(terminalLine(lib.StateCompleted, ""), clusterLineNote)
	waitFor(t, "the child's terminal line", lastEditIs(r, f.kids[0].StatusMsgID, done))
	if got, want := r.adapter.editsOf(f.kids[0].StatusMsgID), []string{working, done}; strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("child line edits = %q, want %q", got, want)
	}
}

// TestOneRelayBatchMovesEveryTasksLine: a relay batch carrying events for
// the running delegating turn and for both of its fan-out children moves
// all three lines, each with one edit at its task's last event, rather than
// only the line of the task whose event came last.
func TestOneRelayBatchMovesEveryTasksLine(t *testing.T) {
	t.Parallel()
	r, spawn, _ := startFanOutRig(t, 2)
	conv := "discord:g1/t-fanout-line-batch"
	exec, origin, session := sessionTurn(t, r, spawn, conv, "how are the clusters?")
	f := fanOutFrom(t, r, conv, exec, origin, clusterTask("web-1", "a"), clusterTask("web-2", "b"))
	rec, err := r.g.reg.Get(context.Background(), conv)
	if err != nil || rec == nil || rec.ActiveTask == nil || rec.ActiveTask.TaskID != origin.TaskID || rec.ActiveTask.StatusMsgID == "" {
		t.Fatalf("the delegating turn should hold the conversation with a line: %+v, %v", rec, err)
	}
	parentLine := rec.ActiveTask.StatusMsgID
	r.g.relayBatch(conv, []relayItem{
		workingItem(t, f.envs[0], "cluster"),
		workingItem(t, f.envs[1], "cluster"),
		progressItem(t, origin, session, "waiting on the checks"),
		progressItem(t, f.envs[0], "cluster", "checking web-1"),
		progressItem(t, f.envs[1], "cluster", "checking web-2"),
	})
	if want := statusLine(lib.StateWorking, "waiting on the checks"); !lastEditIs(r, parentLine, want)() {
		t.Fatalf("the turn's line edits = %q, want the last %q", r.adapter.editsOf(parentLine), want)
	}
	for i, step := range []string{"checking web-1", "checking web-2"} {
		want := withLineNote(statusLine(lib.StateWorking, step), clusterLineNote)
		if got := r.adapter.editsOf(f.kids[i].StatusMsgID); len(got) != 1 || got[0] != want {
			t.Fatalf("child %d line edits = %q, want one %q", i, got, want)
		}
	}
}

// TestAStoppedFanOutChildsLineIsNeverWrittenAgain: a running child's line
// moves; a stop closes both children's lines once at "🛑 stopped" with their
// notes; later working and progress events, off the bus and in one relay
// batch, and the children's terminals, are dropped and edit neither line.
func TestAStoppedFanOutChildsLineIsNeverWrittenAgain(t *testing.T) {
	t.Parallel()
	r, spawn, _ := startStopRig(t, nil)
	ctx := context.Background()
	conv := "discord:g1/t-fanout-line-stopped"
	f := fanOut(t, r, spawn, conv, clusterTask("web-1", "a"), clusterTask("web-2", "b"))
	exec0 := r.execFor(t, f.envs[0], "cluster")
	if err := exec0.PublishArtifact(ctx, lib.Artifact{ArtifactID: "p-1", Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "checking nodes"}}}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the running child's progress line", lastEditIs(r, f.kids[0].StatusMsgID, withLineNote(statusLine("", "checking nodes"), clusterLineNote)))

	sessionRigTurn(r, conv, "stop-1", "stop")
	stopped := withLineNote(noticeTreeTaskStopped, clusterLineNote)
	for _, k := range f.kids {
		waitFor(t, "child "+k.ID+" closed at stopped", lastEditIs(r, k.StatusMsgID, stopped))
	}
	before := [][]string{r.adapter.editsOf(f.kids[0].StatusMsgID), r.adapter.editsOf(f.kids[1].StatusMsgID)}

	if err := exec0.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := exec0.PublishArtifact(ctx, lib.Artifact{ArtifactID: "p-2", Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "late progress"}}}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the late progress dropped", loggedContaining(r, "dropped a canceled task's write", f.kids[0].ID, "what=progress"))
	r.g.relayBatch(conv, []relayItem{
		workingItem(t, f.envs[1], "cluster"),
		progressItem(t, f.envs[1], "cluster", "late batch progress"),
	})
	waitFor(t, "the batch's progress dropped", loggedContaining(r, "dropped a canceled task's write", f.kids[1].ID, "what=progress"))
	for i, k := range f.kids {
		if err := r.execFor(t, f.envs[i], "cluster").PublishStatus(ctx, lib.StateCanceled, true); err != nil {
			t.Fatal(err)
		}
		childEndedLogged(t, r, k.ID)
	}
	for i, k := range f.kids {
		if got := r.adapter.editsOf(k.StatusMsgID); strings.Join(got, "|") != strings.Join(before[i], "|") {
			t.Fatalf("child %d line edits after the stop = %q, want them left at %q", i, got, before[i])
		}
	}
}

// stopLinesOn counts the posts and edits of messageID that carried a Stop
// control.
func stopLinesOn(r *rig, messageID string) int {
	n := 0
	for _, l := range r.adapter.stopLinesCopy() {
		if l.MessageID == messageID {
			n++
		}
	}
	return n
}

// TestAParkedParentLineIsNotMovedByTheParentsEvents: a single child holds
// the active task, and its delegating turn's line waits on the turn's entry
// for the turn's own terminal. A late working or progress event from the
// still-running turn edits that line not at all, and so never draws its Stop
// control again.
func TestAParkedParentLineIsNotMovedByTheParentsEvents(t *testing.T) {
	t.Parallel()
	r, spawn := startRigWithSpawner(t)
	ctx := context.Background()
	conv := "discord:g1/t-parked-parent-line"
	exec, origin, session := sessionTurn(t, r, spawn, conv, "how is the fleet?")
	before, _ := r.g.reg.Get(ctx, conv)
	parentLine := before.ActiveTask.StatusMsgID
	if parentLine == "" {
		t.Fatal("the parent turn has no rolling line")
	}
	if err := exec.PublishArtifact(ctx, delegateArtifact(t, "platform", "report fleet health")); err != nil {
		t.Fatal(err)
	}
	child := r.awaitTask(t, targetPlatform)
	waitFor(t, "the child holds the conversation and the parent's line is parked", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		pref, _ := rec.TaskRefFor(origin.TaskID)
		return rec.ActiveTask != nil && rec.ActiveTask.TaskID == child.TaskID && pref.StatusMsgID == parentLine
	})
	edits, stops := r.adapter.editsOf(parentLine), stopLinesOn(r, parentLine)
	r.g.relayBatch(conv, []relayItem{workingItem(t, origin, session)})
	r.g.relayBatch(conv, []relayItem{progressItem(t, origin, session, "late parent progress")})
	if got := r.adapter.editsOf(parentLine); strings.Join(got, "|") != strings.Join(edits, "|") {
		t.Fatalf("the parked parent line was edited: %q, want it left at %q", got, edits)
	}
	if got := stopLinesOn(r, parentLine); got != stops {
		t.Fatalf("the parked parent line drew its Stop control again: %d writes with it, want %d", got, stops)
	}
}

// TestLiveLineOfFindsOnlyAnOpenLine: the live line is the active task's,
// with its Stop control unless detached and none once a stop closed it, or a
// fan-out child's while it runs. A child canceled or ended, and a delegating
// turn's line parked on its entry, have none, even with a message still on
// the entry: every path that cancels or ends a child takes its message off
// too, so these entries are the guard's own cases.
func TestLiveLineOfFindsOnlyAnOpenLine(t *testing.T) {
	t.Parallel()
	parent := TaskRef{ID: "t-parent", BatchSize: 2, Children: []string{"t-kid"}}
	kid := TaskRef{ID: "t-kid", Role: taskRoleChild, ParentTaskID: "t-parent", BatchIndex: 1, StatusMsgID: "m-kid", LineNote: "(note)"}
	parked := TaskRef{ID: "t-turn", StatusMsgID: "m-turn"}
	single := TaskRef{ID: "t-single", Role: taskRoleChild, ParentTaskID: "t-turn"}
	with := func(edit func(*TaskRef)) TaskRef { k := kid; edit(&k); return k }
	type want struct{ msg, note, stop string }
	for _, c := range []struct {
		name   string
		active *ActiveTask
		tasks  []TaskRef
		taskID string
		want   want
	}{
		{"the active task", &ActiveTask{TaskID: "t-a", StatusMsgID: "m-a", LineNote: "(n)"}, nil, "t-a", want{"m-a", "(n)", "t-a"}},
		{"a detached active task", &ActiveTask{TaskID: "t-a", StatusMsgID: "m-a", Detached: true}, nil, "t-a", want{"m-a", "", ""}},
		{"an active task whose stop closed its line", &ActiveTask{TaskID: "t-a", StatusMsgID: "m-a", LineClosed: true}, nil, "t-a", want{}},
		{"an active task with no line", &ActiveTask{TaskID: "t-a"}, nil, "t-a", want{}},
		{"a running fan-out child", nil, []TaskRef{parent, kid}, "t-kid", want{"m-kid", "(note)", "t-kid"}},
		{"a canceled fan-out child", nil, []TaskRef{parent, with(func(k *TaskRef) { k.Canceled = true })}, "t-kid", want{}},
		{"an ended fan-out child", nil, []TaskRef{parent, with(func(k *TaskRef) { k.BatchEnd = &BatchEnd{State: lib.StateFailed} })}, "t-kid", want{}},
		{"a fan-out child with no line", nil, []TaskRef{parent, with(func(k *TaskRef) { k.StatusMsgID = "" })}, "t-kid", want{}},
		{"a delegating turn's parked line", &ActiveTask{TaskID: "t-single", StatusMsgID: "m-single"}, []TaskRef{parked, single}, "t-turn", want{}},
		{"a task the record does not hold", nil, nil, "t-gone", want{}},
	} {
		rec := &SessionRecord{ActiveTask: c.active, Tasks: c.tasks}
		msg, note, stop := liveLineOf(rec, c.taskID)
		if got := (want{msg, note, stop}); got != c.want {
			t.Errorf("%s: liveLineOf = %+v, want %+v", c.name, got, c.want)
		}
	}
}
