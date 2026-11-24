package gateway

import (
	"context"
	"strings"
	"testing"
	"time"
)

// seedWaitingChild puts a conversation whose active task is a delegated
// `cluster` child at tier, minted by a turn that came in on backend, aged age,
// with a status line (msgID) and relay state still at submitted.
func seedWaitingChild(t *testing.T, r *rig, conv, taskID, msgID, tier, backend string, age time.Duration) *SessionRecord {
	t.Helper()
	parent := "parent-" + taskID
	rec := &SessionRecord{
		Key: conv, ContextID: "ctx-" + taskID, Kind: "group", LastActivity: time.Now().UTC(),
		BusSession: "chat-otter-1", Addressee: "chat-otter-1",
		ActiveTask: &ActiveTask{TaskID: taskID, CorrelationID: "corr-" + taskID,
			Ask: "check the cluster", SubmittedAt: time.Now().Add(-age), StatusMsgID: msgID},
		Tasks: []TaskRef{
			{ID: parent, Addressee: "chat-otter-1", Requester: &TaskRequester{Backend: backend, Subject: "s"}},
			{ID: taskID, Addressee: "cluster", Tier: tier, Role: taskRoleChild, ParentTaskID: parent},
		},
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	r.g.mu.Lock()
	r.g.relays[taskID] = &relayState{}
	r.g.mu.Unlock()
	return rec
}

func waitingEdits(a *fakeAdapter, msgID string) []string {
	var out []string
	for _, e := range a.editsOf(msgID) {
		if strings.Contains(e, "**waiting**") {
			out = append(out, e)
		}
	}
	return out
}

// A child past the threshold with no first event gets the notice on its own
// line, in place, and nothing on its stream; one inside the threshold doesn't.
func TestAWaitingChildsLineSaysItsWaiting(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	seedWaitingChild(t, r, "discord:g1/wait-new", "task-young", "msg-young", "read", "discord", 10*time.Second)
	seedWaitingChild(t, r, "discord:g1/wait-old", "task-old", "msg-old", "read", "discord", waitingNoticeAfter+10*time.Second)
	r.g.reapOnce(ctx)
	if got := waitingEdits(r.adapter, "msg-young"); len(got) != 0 {
		t.Fatalf("a child inside the threshold got the notice: %q", got)
	}
	got := waitingEdits(r.adapter, "msg-old")
	if len(got) != 1 || !strings.Contains(got[0], "the cluster agent's workers are all busy") || !strings.Contains(got[0], "1 other request sent through me") {
		t.Fatalf("waiting edits = %q, want one naming cluster and the 1 other (young) child", got)
	}
	if _, err := r.client.TasksGet(ctx, "cluster", "task-old"); !isTaskNotFound(err) {
		t.Fatalf("the notice put something on the task's stream: %v", err)
	}
	rec, _ := r.g.reg.Get(ctx, "discord:g1/wait-old")
	if rec.ActiveTask.WaitingNoticeAt.IsZero() || rec.ActiveTask.WaitingNoticeOthers != 1 || !rec.ActiveTask.NoFirstEventNoticeAt.IsZero() {
		t.Fatalf("record after the notice: %+v; the grace marker must stay unset", rec.ActiveTask)
	}
	r.g.mu.Lock()
	shown := r.g.relays["task-old"].busyShown
	r.g.mu.Unlock()
	if !shown {
		t.Fatal("busyShown not set, so the relay's submitted render would replace the notice")
	}
}

// The count is this gateway's other waiting children for the same profile
// and tier; the line is edited again only when it changes.
func TestTheWaitingCountIsSameTierAndEditsOnlyOnChange(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	age := waitingNoticeAfter + 10*time.Second
	seedWaitingChild(t, r, "discord:g1/count-a", "task-a", "msg-a", "read", "discord", age)
	seedWaitingChild(t, r, "discord:g1/count-other-tier", "task-x", "msg-x", "unattended", "discord", age)
	r.g.reapOnce(ctx)
	r.g.reapOnce(ctx)
	if got := waitingEdits(r.adapter, "msg-a"); len(got) != 1 || !strings.Contains(got[0], "it starts as soon as one frees up") {
		t.Fatalf("edits = %q, want one notice with no others (the other tier isn't counted) after two passes", got)
	}
	seedWaitingChild(t, r, "discord:g1/count-b", "task-b", "msg-b", "read", "discord", age)
	r.g.reapOnce(ctx)
	got := waitingEdits(r.adapter, "msg-a")
	if len(got) != 2 || !strings.Contains(got[1], "1 other request") {
		t.Fatalf("edits = %q, want a second edit once a same-tier child waits too", got)
	}
}

// No notice for a child whose parent came in through a door that reads the
// line as data, or whose line has moved past submitted.
func TestTheWaitingNoticeSkipsDoorsAndStartedChildren(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	age := waitingNoticeAfter + 10*time.Second
	seedWaitingChild(t, r, "a2a:caller/door", "task-door", "msg-door", "read", a2aBackend, age)
	seedWaitingChild(t, r, "inject:eval/door", "task-inject", "msg-inject", "read", injectBackend, age)
	seedWaitingChild(t, r, "discord:g1/started", "task-started", "msg-started", "read", "discord", age)
	r.g.mu.Lock()
	r.g.relays["task-started"].state = "working"
	r.g.mu.Unlock()
	r.g.reapOnce(ctx)
	for _, m := range []string{"msg-door", "msg-inject", "msg-started"} {
		if got := waitingEdits(r.adapter, m); len(got) != 0 {
			t.Fatalf("%s got the notice: %q", m, got)
		}
	}
}

// At the grace, a waiting child's line is closed in place (no ⏳ left, says
// it didn't start and what the next message does) instead of the grace's
// separate post; the grace itself is unchanged.
func TestTheGraceClosesAWaitingLineInPlace(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	seedWaitingChild(t, r, "discord:g1/grace", "task-grace", "msg-grace", "read", "discord", defaultFirstEventGrace+time.Minute)
	r.g.reapOnce(ctx)
	edits := r.adapter.editsOf("msg-grace")
	if len(edits) < 2 {
		t.Fatalf("edits = %q, want the waiting notice then the closing line", edits)
	}
	last := edits[len(edits)-1]
	if strings.Contains(last, "⏳") || !strings.Contains(last, "**didn't start**") || !strings.Contains(last, "send it again to start a new request") || !strings.Contains(last, "minutes") {
		t.Fatalf("closing line = %q", last)
	}
	if got := noticePosts(r.adapter, "task-grace"); len(got) != 0 {
		t.Fatalf("the grace posted its notice as well: %q", got)
	}
	rec, _ := r.g.reg.Get(ctx, "discord:g1/grace")
	if rec.ActiveTask.NoFirstEventNoticeAt.IsZero() {
		t.Fatal("the grace marker wasn't set: the grace must run as before")
	}
	r.g.reapOnce(ctx)
	if n := len(r.adapter.editsOf("msg-grace")); n != len(edits) {
		t.Fatalf("a pass after the grace edited the line again (%d edits, was %d)", n, len(edits))
	}
}

func TestGraceWords(t *testing.T) {
	for d, want := range map[time.Duration]string{time.Minute: "1 minute", 10 * time.Minute: "10 minutes", 90 * time.Second: "1m30s"} {
		if got := graceWords(d); got != want {
			t.Errorf("graceWords(%v) = %q, want %q", d, got, want)
		}
	}
}
