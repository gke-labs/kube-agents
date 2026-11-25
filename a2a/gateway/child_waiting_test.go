package gateway

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
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
			Ask: "check the cluster", SubmittedAt: time.Now().Add(-age), StatusMsgID: msgID,
			LineNote: fmt.Sprintf(delegatedProfileLineNote, "cluster")},
		Tasks: []TaskRef{
			{ID: parent, Addressee: "chat-otter-1", Requester: &TaskRequester{Backend: backend, Subject: "s"}},
			{ID: taskID, Addressee: "cluster", Tier: tier, Role: taskRoleChild, ParentTaskID: parent,
				StartedAt: time.Now().Add(-age).UTC()},
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

// startChild puts a working status on a child's own events subject, as its
// profile worker does when it starts.
func startChild(t *testing.T, r *rig, rec *SessionRecord, taskID string) {
	t.Helper()
	payload, err := messagePayload("seeded", taskID, rec.ContextID)
	if err != nil {
		t.Fatal(err)
	}
	origin, err := lib.NewMessageEnvelope(gatewayParty, taskID, rec.ContextID,
		"corr-"+taskID, payload, lib.WithTo(lib.Party{Session: "cluster"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := r.execFor(t, origin, "cluster").PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
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
	if len(got) != 1 || !strings.Contains(got[0], "the cluster agent hasn't started on this yet") || !strings.Contains(got[0], "1 other request sent through me") {
		t.Fatalf("waiting edits = %q, want one naming cluster and the 1 other (young) child", got)
	}
	if strings.Contains(got[0], "delegated to") {
		t.Fatalf("the line repeats the delegation note: %q", got[0])
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
	if got := waitingEdits(r.adapter, "msg-a"); len(got) != 1 || strings.Contains(got[0], "other request") {
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
	seedWaitingChild(t, r, "discord:g1/grace", "task-grace", "msg-grace", "read", "discord", waitingNoticeAfter+10*time.Second)
	r.g.reapOnce(ctx) // the waiting notice
	rec, _ := r.g.reg.Get(ctx, "discord:g1/grace")
	rec.ActiveTask.SubmittedAt = time.Now().Add(-(defaultFirstEventGrace + time.Minute))
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	r.g.reapOnce(ctx) // the grace
	edits := r.adapter.editsOf("msg-grace")
	if len(edits) < 2 {
		t.Fatalf("edits = %q, want the waiting notice then the closing line", edits)
	}
	last := edits[len(edits)-1]
	if strings.Contains(last, "⏳") || strings.Contains(last, "delegated to") || !strings.Contains(last, "**didn't start**") || !strings.Contains(last, "ask again to start a new request") || !strings.Contains(last, "minutes") {
		t.Fatalf("closing line = %q", last)
	}
	if got := noticePosts(r.adapter, "task-grace"); len(got) != 0 {
		t.Fatalf("the grace posted its notice as well: %q", got)
	}
	rec, _ = r.g.reg.Get(ctx, "discord:g1/grace")
	if rec.ActiveTask.NoFirstEventNoticeAt.IsZero() {
		t.Fatal("the grace marker wasn't set: the grace must run as before")
	}
	r.g.reapOnce(ctx)
	if n := len(r.adapter.editsOf("msg-grace")); n != len(edits) {
		t.Fatalf("a pass after the grace edited the line again (%d edits, was %d)", n, len(edits))
	}
}

func TestGraceWords(t *testing.T) {
	for d, want := range map[time.Duration]string{time.Minute: "1 minute", 10 * time.Minute: "10 minutes", 90 * time.Second: "90 seconds", time.Second: "1 second"} {
		if got := graceWords(d); got != want {
			t.Errorf("graceWords(%v) = %q, want %q", d, got, want)
		}
	}
}

// The count reads each child's stream, not this gateway's relay state: a
// fan-out's children are counted, and a child that has started (an event on
// its stream) or ended isn't, whatever its relay shows.
func TestTheWaitingCountReadsTheStreamAndCountsBatchChildren(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	age := waitingNoticeAfter + 10*time.Second
	seedWaitingChild(t, r, "discord:g1/count-me", "task-me", "msg-me", "read", "discord", age)
	started := seedWaitingChild(t, r, "discord:g1/count-started", "task-started", "msg-started", "read", "discord", age)
	startChild(t, r, started, "task-started")
	// A fan-out on another conversation: two children at the same profile
	// and tier, one still waiting and one that ended.
	batch := &SessionRecord{
		Key: "discord:g1/batch", ContextID: "ctx-batch", Kind: "group", LastActivity: time.Now().UTC(),
		BusSession: "chat-otter-1", Addressee: "chat-otter-1",
		Tasks: []TaskRef{
			{ID: "batch-parent", Addressee: "chat-otter-1", Requester: &TaskRequester{Backend: "discord", Subject: "s"}},
			{ID: "batch-0", Addressee: "cluster", Tier: "read", Role: taskRoleChild, ParentTaskID: "batch-parent",
				StartedAt: time.Now().Add(-time.Minute).UTC()},
			{ID: "batch-1", Addressee: "cluster", Tier: "read", Role: taskRoleChild, ParentTaskID: "batch-parent",
				StartedAt: time.Now().Add(-time.Minute).UTC(), BatchEnd: &BatchEnd{}},
		},
	}
	if err := r.g.reg.Put(ctx, batch); err != nil {
		t.Fatal(err)
	}
	r.g.reapOnce(ctx)
	got := waitingEdits(r.adapter, "msg-me")
	if len(got) != 1 || !strings.Contains(got[0], "1 other request sent through me") {
		t.Fatalf("edits = %q, want one counting only the waiting batch child", got)
	}
	if got := waitingEdits(r.adapter, "msg-started"); len(got) != 0 {
		t.Fatalf("a started child got the notice: %q", got)
	}
}

// One count per profile and tier per pass, however many children ask.
func TestTheWaitingCountIsCachedForThePass(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	age := waitingNoticeAfter + 10*time.Second
	seedWaitingChild(t, r, "discord:g1/once-a", "task-once-a", "msg-once-a", "read", "discord", age)
	seedWaitingChild(t, r, "discord:g1/once-b", "task-once-b", "msg-once-b", "read", "discord", age)
	r.g.reapOnce(ctx)
	r.g.mu.Lock()
	n, ok := r.g.waitingPass["cluster\x00read"]
	r.g.mu.Unlock()
	if !ok || n != 2 {
		t.Fatalf("pass cache = %d (%v), want 2 for cluster/read", n, ok)
	}
	// A child minted after the count isn't seen until the next pass.
	seedWaitingChild(t, r, "discord:g1/once-c", "task-once-c", "msg-once-c", "read", "discord", age)
	if got, _ := r.g.waitingChildren(ctx, "cluster", "read"); got != 2 {
		t.Fatalf("waitingChildren inside the pass = %d, want the cached 2", got)
	}
	r.g.reapOnce(ctx)
	r.g.mu.Lock()
	n = r.g.waitingPass["cluster\x00read"]
	r.g.mu.Unlock()
	if n != 3 {
		t.Fatalf("next pass count = %d, want 3", n)
	}
}

// A child past its grace with an empty stream isn't waiting: it never
// started and was released, or its events aged out of the stream. Nor is
// an entry with no submission time. Neither is counted.
func TestTheWaitingCountLeavesOutChildrenPastTheirGrace(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	seedWaitingChild(t, r, "discord:g1/past-me", "task-past-me", "msg-past-me", "read", "discord", waitingNoticeAfter+10*time.Second)
	old := &SessionRecord{
		Key: "discord:g1/past-old", ContextID: "ctx-past-old", Kind: "group", LastActivity: time.Now().UTC(),
		BusSession: "chat-otter-1", Addressee: "chat-otter-1",
		Tasks: []TaskRef{
			{ID: "old-parent", Addressee: "chat-otter-1", Requester: &TaskRequester{Backend: "discord", Subject: "s"}},
			{ID: "old-child", Addressee: "cluster", Tier: "read", Role: taskRoleChild, ParentTaskID: "old-parent",
				StartedAt: time.Now().Add(-4 * 24 * time.Hour).UTC()},
			{ID: "undated-child", Addressee: "cluster", Tier: "read", Role: taskRoleChild, ParentTaskID: "old-parent"},
		},
	}
	if err := r.g.reg.Put(ctx, old); err != nil {
		t.Fatal(err)
	}
	r.g.reapOnce(ctx)
	got := waitingEdits(r.adapter, "msg-past-me")
	if len(got) != 1 || strings.Contains(got[0], "other request") {
		t.Fatalf("edits = %q, want one notice counting no others", got)
	}
}

// A restart drops the relay's render state; a child still waiting gets the
// notice anyway, and its line is still closed in place at the grace.
func TestTheWaitingNoticeAndItsCloseSurviveARestart(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	seedWaitingChild(t, r, "discord:g1/restart", "task-restart", "msg-restart", "read", "discord", waitingNoticeAfter+10*time.Second)
	r.g.mu.Lock()
	delete(r.g.relays, "task-restart")
	r.g.mu.Unlock()
	r.g.reapOnce(ctx)
	if got := waitingEdits(r.adapter, "msg-restart"); len(got) != 1 {
		t.Fatalf("edits after a restart = %q, want the notice", got)
	}
	// Restart again, then let the grace pass.
	rec, _ := r.g.reg.Get(ctx, "discord:g1/restart")
	rec.ActiveTask.SubmittedAt = time.Now().Add(-(defaultFirstEventGrace + time.Minute))
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	r.g.mu.Lock()
	delete(r.g.relays, "task-restart")
	r.g.mu.Unlock()
	r.g.reapOnce(ctx)
	edits := r.adapter.editsOf("msg-restart")
	if last := edits[len(edits)-1]; !strings.Contains(last, "**didn't start**") {
		t.Fatalf("closing line after a restart = %q", last)
	}
	if got := noticePosts(r.adapter, "task-restart"); len(got) != 0 {
		t.Fatalf("the grace posted its notice as well: %q", got)
	}
}

// A child already past the grace when first seen (a gateway down across it)
// gets no waiting notice, and so the grace's own post, as before.
func TestAChildPastTheGraceGetsNoWaitingNotice(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	seedWaitingChild(t, r, "discord:g1/late", "task-late", "msg-late", "read", "discord", defaultFirstEventGrace+time.Minute)
	r.g.reapOnce(ctx)
	if got := waitingEdits(r.adapter, "msg-late"); len(got) != 0 {
		t.Fatalf("a child past the grace got the waiting notice: %q", got)
	}
	if got := noticePosts(r.adapter, "task-late"); len(got) != 1 {
		t.Fatalf("grace posts = %q, want the grace's own post", got)
	}
}
