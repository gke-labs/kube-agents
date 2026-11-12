package gateway

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// More of the tree-wide stop (#5682 PR 3): what a stopped task hands a
// program, the drops nothing else covers, the heal's re-send and end of a
// stopped or withdrawn child, and the record keeping them.

// withdrawnKept runs a batch withdrawn partway whose first child (cluster)
// stays on the record, its cancel unsent because TASKS was narrowed to the
// session's .in. It returns the parent's submission, the kept child's, and
// the stream's subjects before the narrowing.
func withdrawnKept(t *testing.T, r *rig, spawn *fakeSpawner, conv string) (*lib.Envelope, *lib.Envelope, []string) {
	t.Helper()
	ctx := context.Background()
	exec, origin, session := sessionTurn(t, r, spawn, conv, "x")
	wide := tasksSubjects(t, r.url)
	narrowTasksStream(t, r.url, session)
	if err := exec.PublishArtifact(ctx, delegateBatchArtifact(t, clusterTask("dev", "first"), platformTask("second"))); err != nil {
		t.Fatal(err)
	}
	first := submissionOn(t, r, "cluster")
	waitFor(t, "the failed cancel", loggedContaining(r, "cancel publish failed for a withdrawn batch child", first.TaskID))
	completeTask(t, exec, "delegated the checks")
	waitFor(t, "parent terminal relayed", postedContaining(r, "delegated the checks"))
	return origin, first, wide
}

// refOf reads one entry of conv's stored record.
func refOf(t *testing.T, r *rig, conv, taskID string) TaskRef {
	t.Helper()
	rec, _ := r.g.reg.Get(context.Background(), conv)
	ref, _ := rec.TaskRefFor(taskID)
	return ref
}

// TestAStoppedTaskHandsAProgramNoAnswer: a stopped turn that completes
// anyway hands the door's observer no deliverable and ends canceled, and a
// console's terminal frame says canceled with no answer text.
func TestAStoppedTaskHandsAProgramNoAnswer(t *testing.T) {
	t.Run("observer", func(t *testing.T) {
		r, _, obs := startObservedRig(t, nil)
		conv := "discord:g1/t-stopped-no-delivery"
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m1", Text: "check the fleet"}
		origin := r.awaitTask(t, "platform")
		exec := r.execFor(t, origin, "platform")
		_ = exec.PublishStatus(context.Background(), lib.StateWorking, false)
		sessionRigTurn(r, conv, "stop-1", "stop")
		waitFor(t, "stopped", func() bool { return refOf(t, r, conv, origin.TaskID).Canceled })
		completeTask(t, exec, "the fleet is fine")
		waitFor(t, "the root's terminal", func() bool { _, ok := obs.terminalFor(origin.TaskID); return ok })
		if end, _ := obs.terminalFor(origin.TaskID); end.state != lib.StateCanceled {
			t.Fatalf("root terminal = %+v, want canceled", end)
		}
		assertNoDelivery(t, obs)
	})
	t.Run("console", func(t *testing.T) {
		r := startRig(t)
		r.adapter.setTakesTerminals()
		r.adapter.roster = []string{consoleAuthor}
		conv := "console:tab-stop"
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", Backend: consoleBackend, AuthorID: consoleAuthor, MessageID: "m1", Text: "how is the fleet"}
		origin := r.awaitTask(t, "platform")
		exec := r.execFor(t, origin, "platform")
		_ = exec.PublishStatus(context.Background(), lib.StateWorking, false)
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", Backend: consoleBackend, AuthorID: consoleAuthor, MessageID: "m2", Text: "stop"}
		waitFor(t, "stopped", func() bool { return refOf(t, r, conv, origin.TaskID).Canceled })
		completeTask(t, exec, "the fleet is fine")
		waitFor(t, "the terminal frame", func() bool { return len(r.adapter.terminalPosts()) > 0 })
		for _, f := range r.adapter.terminalPosts() {
			if f.State != lib.StateCanceled || strings.Contains(f.Text, "the fleet is fine") {
				t.Fatalf("a stopped task's frame = %+v, want canceled with no answer", f)
			}
		}
	})
}

// TestAStoppedTasksDeferredNoticeIsDropped: a refusal held for a turn's end
// is not posted when the turn was stopped first.
func TestAStoppedTasksDeferredNoticeIsDropped(t *testing.T) {
	r, spawn, _ := startStopRig(t, nil)
	ctx := context.Background()
	conv := "discord:g1/t-stop-deferred"
	exec, origin, _ := sessionTurn(t, r, spawn, conv, "x")
	_ = exec.PublishArtifact(ctx, delegateArtifact(t, "nope", "do it"))
	waitFor(t, "the refusal held", loggedContaining(r, "delegation refused", origin.TaskID))
	sessionRigTurn(r, conv, "stop-1", "stop")
	waitFor(t, "stopped", func() bool { return refOf(t, r, conv, origin.TaskID).Canceled })
	completeTask(t, exec, "delegated")
	waitFor(t, "the notice dropped", loggedContaining(r, "dropped a canceled task's write", origin.TaskID, `what="deferred notice"`))
	if postedContaining(r, "delegation refused")() {
		t.Fatal("a stopped turn's deferred refusal was posted")
	}
}

// TestAStoppedTasksSteerShortfallIsDropped: a follow-up queued and never run
// gets no "did not run" line when the task was stopped.
func TestAStoppedTasksSteerShortfallIsDropped(t *testing.T) {
	r, _, _ := startObservedRig(t, nil)
	conv := "discord:g1/t-stop-shortfall"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "s-1", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	_ = r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "s-2", Text: "only prod"}
	waitFor(t, "steer published", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == 2 })
	publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerQueued, EnvelopeID: lastInSubject(t, r, origin).EnvelopeID})
	waitFor(t, "the queued notice relayed", func() bool {
		r.g.mu.Lock()
		defer r.g.mu.Unlock()
		return r.g.steerNoticesFrom["platform"]
	})
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "s-3", Text: "stop"}
	waitFor(t, "stopped", func() bool { return refOf(t, r, conv, origin.TaskID).Canceled })
	publishFinal(t, r, origin, "platform", lib.StateCanceled, "reason: canceled-by-request")
	waitFor(t, "the shortfall dropped", loggedContaining(r, "dropped a canceled task's write", origin.TaskID, `what="steer shortfall"`))
	if postedContaining(r, fmt.Sprintf(noticeSteersUnrun, 1))() {
		t.Fatal("a stopped task's steer shortfall was posted")
	}
}

// TestAStoppedTasksFirstDropClosesItsLine: a typed stop leaves the line
// open; the stopped task's first dropped write closes it on "cancel sent"
// with its Stop control gone.
func TestAStoppedTasksFirstDropClosesItsLine(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/t-stop-first-drop"
	origin := startChatTask(t, r, conv, false)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-stop", Text: "stop"}
	waitFor(t, "stopped", func() bool { return refOf(t, r, conv, origin.TaskID).Canceled })
	if err := r.execFor(t, origin, "platform").PublishArtifact(context.Background(), lib.Artifact{ArtifactID: "p-1", Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "late progress"}}}); err != nil {
		t.Fatal(err)
	}
	waitForRecord(t, r, conv, "the line closed", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.LineClosed
	})
	edits := r.adapter.editTexts()
	if len(edits) == 0 || !strings.Contains(edits[len(edits)-1], "cancel sent") {
		t.Fatalf("line edits = %q, want the last on cancel sent", edits)
	}
	for _, l := range r.adapter.stopLinesCopy() {
		if strings.Contains(l.Text, "cancel sent") {
			t.Fatalf("the closed line kept its Stop control: %+v", l)
		}
	}
}

// TestAWithdrawnChildsTerminalClearsCancelUnsent: a kept child whose cancel
// never went out has ended once its terminal relays: nothing re-sends it.
func TestAWithdrawnChildsTerminalClearsCancelUnsent(t *testing.T) {
	r, spawn, _ := startFanOutRig(t, 2)
	conv := "discord:g1/t-withdrawn-terminal"
	_, first, _ := withdrawnKept(t, r, spawn, conv)
	if !refOf(t, r, conv, first.TaskID).CancelUnsent {
		t.Fatal("the kept child's cancel is not marked unsent")
	}
	_ = r.execFor(t, first, "cluster").PublishStatus(context.Background(), lib.StateCanceled, true)
	waitFor(t, "the mark cleared", func() bool { return !refOf(t, r, conv, first.TaskID).CancelUnsent })
}

// TestTheHealEndsAWithdrawnBatchsKeptChild: once its re-sent cancel goes
// out, a kept child that never starts is ended by the heal past
// FirstEventGrace, and its end ends the chain once, canceled.
func TestTheHealEndsAWithdrawnBatchsKeptChild(t *testing.T) {
	r, spawn, obs := startFanOutRig(t, 2)
	ctx := context.Background()
	conv := "discord:g1/t-withdrawn-heal"
	origin, first, wide := withdrawnKept(t, r, spawn, conv)
	setTasksSubjects(t, r.url, wide)
	putRecord(t, r, conv, func(rec *SessionRecord) {
		for i := range rec.Tasks {
			if rec.Tasks[i].ID == first.TaskID {
				rec.Tasks[i].StartedAt = time.Now().Add(-r.g.cfg.FirstEventGrace - time.Minute)
			}
		}
	})
	sessionRigTurn(r, conv, "h-1", "status")
	waitFor(t, "the re-sent cancel", func() bool { return cancelSentTo(t, r, "cluster", first.TaskID) })
	waitFor(t, "the root's terminal", func() bool { _, ok := obs.terminalFor(origin.TaskID); return ok })
	if end, _ := obs.terminalFor(origin.TaskID); end.state != lib.StateCanceled {
		t.Fatalf("root terminal = %+v, want canceled", end)
	}
	terminals := 0
	for _, e := range obs.events() {
		if e.task == origin.TaskID && e.kind == "terminal" {
			terminals++
		}
	}
	if terminals != 1 {
		t.Fatalf("root terminals = %d, want 1: %v", terminals, obs.kinds())
	}
	if key, _ := r.g.reg.SessionForTask(ctx, first.TaskID); key != "" {
		t.Fatalf("the kept child is still routed: %q", key)
	}
}

// TestAStopResendsACancelThatNeverWentOut: stopping a tree with a task whose
// earlier cancel failed sends that cancel again.
func TestAStopResendsACancelThatNeverWentOut(t *testing.T) {
	r, spawn, _ := startFanOutRig(t, 2)
	ctx := context.Background()
	conv := "discord:g1/t-stop-resend"
	origin, first, wide := withdrawnKept(t, r, spawn, conv)
	setTasksSubjects(t, r.url, wide)
	l := r.g.lockSession(conv)
	l.Lock()
	rec, _ := r.g.reg.Get(ctx, conv)
	r.g.stopTree(ctx, rec, origin.TaskID, Authority{}, false)
	l.Unlock()
	if !cancelSentTo(t, r, "cluster", first.TaskID) {
		t.Fatal("the stop did not send the unsent cancel again")
	}
	if ref, _ := rec.TaskRefFor(first.TaskID); ref.CancelUnsent {
		t.Fatal("the cancel went out but is still marked unsent")
	}
}

// TestAStopCaughtMidMintEndsWithItsChildren: a stop on a batch cut short
// (fewer children on record than planned) mints no more, so the batch ends
// once its children on record have.
func TestAStopCaughtMidMintEndsWithItsChildren(t *testing.T) {
	r, spawn, obs := startStopRig(t, nil)
	ctx := context.Background()
	conv := "discord:g1/t-stop-midmint"
	f := fanOut(t, r, spawn, conv, platformTask("one"), platformTask("two"))
	putRecord(t, r, conv, func(rec *SessionRecord) { rec.setBatchSize(f.origin.TaskID, 3) })
	sessionRigTurn(r, conv, "stop-1", "stop")
	waitFor(t, "the cancels", func() bool {
		return cancelSentTo(t, r, targetPlatform, f.kids[0].ID) && cancelSentTo(t, r, targetPlatform, f.kids[1].ID)
	})
	for i := range f.kids {
		_ = r.execFor(t, f.envs[i], targetPlatform).PublishStatus(ctx, lib.StateCanceled, true)
	}
	waitFor(t, "the root's terminal", func() bool { _, ok := obs.terminalFor(f.origin.TaskID); return ok })
	if end, _ := obs.terminalFor(f.origin.TaskID); end.state != lib.StateCanceled {
		t.Fatalf("root terminal = %+v, want canceled", end)
	}
}

// TestTheFanOutHealPostsNothingForACanceledChild: a child the gateway
// canceled outside a tree stop (a named cancel) is healed silently: no
// status card for a terminal the relay missed, no never-started notice.
func TestTheFanOutHealPostsNothingForACanceledChild(t *testing.T) {
	r, spawn := startBatchRig(t, 2)
	conv := "discord:g1/t-fanout-heal-canceled"
	f := fanOut(t, r, spawn, conv, platformTask("one"), platformTask("two"))
	putRecord(t, r, conv, func(rec *SessionRecord) {
		for i := range rec.Tasks {
			if rec.Tasks[i].ID == f.kids[0].ID || rec.Tasks[i].ID == f.kids[1].ID {
				rec.Tasks[i].Canceled = true
			}
			if rec.Tasks[i].ID == f.kids[0].ID {
				rec.Tasks[i].StartedAt = time.Now().Add(-r.g.cfg.FirstEventGrace - time.Minute)
			}
		}
	})
	e1 := r.execFor(t, f.envs[1], targetPlatform)
	r2, _ := restartRig(t, r, func() {
		completeTask(t, e1, "two done")
		drainRelayDurable(t, r.url)
	})
	posts := len(r2.adapter.postTexts())
	sessionRigTurn(r2, conv, "h-heal", "status")
	waitFor(t, "both children healed", func() bool {
		return loggedContaining(r2, "fan-out child ended", "child="+f.kids[0].ID)() &&
			loggedContaining(r2, "fan-out child ended", "child="+f.kids[1].ID)()
	})
	for _, p := range r2.adapter.postTexts()[posts:] {
		if strings.Contains(p, f.kids[0].ID) || strings.Contains(p, f.kids[1].ID) {
			t.Fatalf("the heal posted about a canceled child: %q", p)
		}
	}
}

// TestLiveNodeNeedsThisConversationsLiveRoute: a node counts as live only
// while it is routed to this conversation and has no observed end; a
// re-send goes only to a task routed here.
func TestLiveNodeNeedsThisConversationsLiveRoute(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()
	rec := &SessionRecord{Key: "discord:g1/t-livenode", ContextID: "ctx-livenode"}
	r.g.mu.Lock()
	r.g.taskSessions["task-00000000000000a1"] = rec.Key
	r.g.taskSessions["task-00000000000000b2"] = "discord:g1/other"
	r.g.mu.Unlock()
	for _, tc := range []struct {
		name string
		ref  TaskRef
		want bool
	}{
		{"routed here", TaskRef{ID: "task-00000000000000a1"}, true},
		{"routed to another conversation", TaskRef{ID: "task-00000000000000b2"}, false},
		{"terminal observed", TaskRef{ID: "task-00000000000000a1", TerminalObserved: true}, false},
		{"chain ended", TaskRef{ID: "task-00000000000000a1", ChainEnd: &ChainEnd{State: lib.StateCanceled}}, false},
		{"canceled", TaskRef{ID: "task-00000000000000a1", Canceled: true}, false},
		{"canceled, cancel unsent", TaskRef{ID: "task-00000000000000a1", Canceled: true, CancelUnsent: true}, true},
	} {
		if got := r.g.liveNode(ctx, rec, tc.ref); got != tc.want {
			t.Errorf("%s: liveNode = %v, want %v", tc.name, got, tc.want)
		}
	}
	rec.Tasks = []TaskRef{
		{ID: "task-00000000000000c3", Addressee: "chat-x", Children: []string{"task-00000000000000b2"}},
		{ID: "task-00000000000000b2", Addressee: "platform", Role: taskRoleChild, ParentTaskID: "task-00000000000000c3", Canceled: true, CancelUnsent: true, CorrelationID: "corr-000000000000000000000001"},
	}
	rec.Tasks = append(rec.Tasks, TaskRef{ID: "task-00000000000000a1", Addressee: "platform", Role: taskRoleChild,
		ParentTaskID: "task-00000000000000c3", Canceled: true, CancelUnsent: true, CorrelationID: "corr-000000000000000000000002"})
	r.g.resendUnsentCancels(ctx, rec)
	if !cancelSentTo(t, r, "platform", "task-00000000000000a1") {
		t.Fatal("no cancel re-sent for a task routed here")
	}
	if cancelSentTo(t, r, "platform", "task-00000000000000b2") {
		t.Fatal("a cancel was re-sent for a task routed to another conversation")
	}
}

// TestTheHistoryCapKeepsAStoppedTaskStillRunning: a task whose cancel never
// went out, or whose stop is not yet confirmed, is never evicted.
func TestTheHistoryCapKeepsAStoppedTaskStillRunning(t *testing.T) {
	rec := &SessionRecord{Key: "k"}
	rec.Tasks = append(rec.Tasks,
		TaskRef{ID: "unsent", Role: taskRoleChild, Canceled: true, CancelUnsent: true},
		TaskRef{ID: "pending", Role: taskRoleChild, Canceled: true, StopPending: true})
	for i := 0; i < taskHistoryCap+5; i++ {
		rec.Tasks = append(rec.Tasks, TaskRef{ID: fmt.Sprintf("t%d", i)})
	}
	rec.trimHistory()
	for _, id := range []string{"unsent", "pending"} {
		if _, ok := rec.TaskRefFor(id); !ok {
			t.Errorf("%s was evicted", id)
		}
	}
}
