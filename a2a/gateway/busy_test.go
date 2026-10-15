package gateway

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// busyPrefix is what every busy notice starts with, for asserting absence.
const busyPrefix = "⏳ **queued**"

// placeholderText is startTaskWith's placeholder, the status line's first text.
const placeholderText = "⏳ submitted…"

// seedFixedRouteTask writes a session record holding an active task addressed
// to platform, submitted age ago, with nothing published for it: another
// conversation's outstanding work, as session-state holds it.
func seedFixedRouteTask(t *testing.T, r *rig, conv string, age time.Duration) *SessionRecord {
	t.Helper()
	taskID := "task-" + randHex(taskIDHexWidth)
	rec := &SessionRecord{
		Key: conv, ContextID: "ctx-" + randHex(4), Kind: "group", Addressee: "platform",
		LastActivity: time.Now().UTC(),
		ActiveTask: &ActiveTask{TaskID: taskID, CorrelationID: "corr-" + randHex(4),
			SubmittedAt: time.Now().Add(-age)},
		Tasks: []TaskRef{{ID: taskID, Addressee: "platform"}},
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	return rec
}

// seededExec is the executor side of a seeded task, for publishing its
// events as an executor that took it would.
func seededExec(t *testing.T, r *rig, rec *SessionRecord) *lib.TaskExecution {
	t.Helper()
	payload, err := messagePayload("seeded", rec.ActiveTask.TaskID, rec.ContextID)
	if err != nil {
		t.Fatal(err)
	}
	origin, err := lib.NewMessageEnvelope(gatewayParty, rec.ActiveTask.TaskID, rec.ContextID,
		rec.ActiveTask.CorrelationID, payload, lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	return r.execFor(t, origin, "platform")
}

// publishFirstEvent puts a working status on a seeded task's events subject.
func publishFirstEvent(t *testing.T, r *rig, rec *SessionRecord) *lib.TaskExecution {
	t.Helper()
	exec := seededExec(t, r, rec)
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	return exec
}

// busyTurn runs one human turn in conv through routeTurn, synchronously and
// under the conversation's session lock as handleInbound runs it, so every
// post and edit it makes is in the fake adapter when it returns.
func busyTurn(r *rig, conv, backend, text string) {
	l := r.g.lockSession(conv)
	l.Lock()
	defer l.Unlock()
	r.g.routeTurn(context.Background(), InboundMessage{
		Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m-" + randHex(4), Text: text,
	}, backend, "test:bnaylor")
}

// postsIn returns every post into conv.
func postsIn(r *rig, conv string) []fakePost {
	r.adapter.mu.Lock()
	defer r.adapter.mu.Unlock()
	var out []fakePost
	for _, p := range r.adapter.posts {
		if p.Conversation == conv {
			out = append(out, p)
		}
	}
	return out
}

// statusLineID is the message id of conv's status line: its placeholder post.
func statusLineID(t *testing.T, r *rig, conv string) string {
	t.Helper()
	for _, p := range postsIn(r, conv) {
		if p.Text == placeholderText {
			return p.MessageID
		}
	}
	t.Fatalf("no placeholder posted into %s: %q", conv, r.adapter.postTexts())
	return ""
}

// busyLinesIn returns the busy notices edited onto conv's status line.
func busyLinesIn(t *testing.T, r *rig, conv string) []string {
	t.Helper()
	var out []string
	for _, e := range r.adapter.editsOf(statusLineID(t, r, conv)) {
		if strings.HasPrefix(e, busyPrefix) {
			out = append(out, e)
		}
	}
	return out
}

// assertBusyLine checks that conv's status line was edited to want, once,
// and that the turn posted nothing beside its placeholder.
func assertBusyLine(t *testing.T, r *rig, conv, want string) {
	t.Helper()
	if got := busyLinesIn(t, r, conv); len(got) != 1 || got[0] != want {
		t.Fatalf("busy edits of the status line = %q, want exactly %q", got, want)
	}
	if got := postsIn(r, conv); len(got) != 1 {
		t.Fatalf("posts = %+v, want the placeholder alone", got)
	}
}

// busyPostsIn returns the busy notices posted into conv.
func busyPostsIn(r *rig, conv string) []string {
	r.adapter.mu.Lock()
	defer r.adapter.mu.Unlock()
	var out []string
	for _, p := range r.adapter.posts {
		if p.Conversation == conv && strings.HasPrefix(p.Text, busyPrefix) {
			out = append(out, p.Text)
		}
	}
	return out
}

// submittedTexts is the text of every submission on platform's in subjects.
func submittedTexts(t *testing.T, r *rig) []string {
	t.Helper()
	var out []string
	for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
		if e.Kind == lib.KindMessage {
			out = append(out, envText(t, e))
		}
	}
	return out
}

func withBusyNoticeAt(n int) func(*Config) {
	return func(c *Config) { c.BusyNoticeAt = n }
}

// TestBusyNoticeWording: one is "request is", anything else "requests are",
// with the number in the sentence, as a queued state of the status line.
func TestBusyNoticeWording(t *testing.T) {
	cases := map[int]string{
		1:  "⏳ **queued** — 1 request is ahead of yours; I'll start on it as soon as there's room",
		2:  "⏳ **queued** — 2 requests are ahead of yours; I'll start on it as soon as there's room",
		10: "⏳ **queued** — 10 requests are ahead of yours; I'll start on it as soon as there's room",
	}
	for n, want := range cases {
		if got := busyNotice(n); got != want {
			t.Errorf("busyNotice(%d) = %q, want %q", n, got, want)
		}
	}
}

// TestBusyNoticeBelowTheThresholdPostsNothing: two ahead of a threshold of
// three is not busy; the turn starts and nothing else is said.
func TestBusyNoticeBelowTheThresholdPostsNothing(t *testing.T) {
	r := startRigWith(t, withBusyNoticeAt(3))
	seedFixedRouteTask(t, r, "discord:g1/busy-a", 0)
	seedFixedRouteTask(t, r, "discord:g1/busy-b", 0)

	conv := "discord:g1/busy-new"
	busyTurn(r, conv, discordBackend, "how is the fleet?")
	if got := submittedTexts(t, r); len(got) != 1 || got[0] != "how is the fleet?" {
		t.Fatalf("submissions = %q, want the one turn", got)
	}
	if got := busyLinesIn(t, r, conv); len(got) != 0 {
		t.Fatalf("busy notice below the threshold: %q", got)
	}
	if got := postsIn(r, conv); len(got) != 1 {
		t.Fatalf("posts below the threshold = %+v, want the placeholder alone", got)
	}
}

// TestBusyNoticeAtTheThresholdSaysHowManyAndStillSubmits: at the threshold
// and above it, the turn's task is submitted as always, and its status line
// (the placeholder, edited) says how many tasks are ahead of it. Nothing is
// posted beside the placeholder.
func TestBusyNoticeAtTheThresholdSaysHowManyAndStillSubmits(t *testing.T) {
	r := startRigWith(t, withBusyNoticeAt(3))
	for _, c := range []string{"a", "b", "c"} {
		seedFixedRouteTask(t, r, "discord:g1/busy-"+c, 0)
	}

	conv := "discord:g1/busy-at"
	busyTurn(r, conv, discordBackend, "restart the canary")
	if got := submittedTexts(t, r); len(got) != 1 || got[0] != "restart the canary" {
		t.Fatalf("submissions = %q, want the turn submitted despite the backlog", got)
	}
	rec, err := r.g.reg.Get(context.Background(), conv)
	if err != nil || rec == nil || rec.ActiveTask == nil {
		t.Fatalf("the busy turn holds no active task: %+v %v", rec, err)
	}
	assertBusyLine(t, r, conv, busyNotice(3))
	if got := r.adapter.editsOf(rec.ActiveTask.StatusMsgID); len(got) != 1 || got[0] != busyNotice(3) {
		t.Fatalf("edits of the active task's status line = %q, want exactly %q", got, busyNotice(3))
	}

	// Above it: two more, and the next turn is told five.
	seedFixedRouteTask(t, r, "discord:g1/busy-d", 0)
	conv2 := "discord:g1/busy-above"
	busyTurn(r, conv2, discordBackend, "and the other one")
	if got := busyLinesIn(t, r, conv2); len(got) != 1 || got[0] != busyNotice(5) {
		t.Fatalf("busy notices = %q, want exactly %q (three seeded, one more, and the turn before)", got, busyNotice(5))
	}
}

// TestBusyCountLeavesOutTasksThatNeverStarted: a task past the first-event
// grace with nothing on its stream is not ahead of anyone. A task past the
// grace that did produce an event, and a task inside the grace, both are.
func TestBusyCountLeavesOutTasksThatNeverStarted(t *testing.T) {
	r := startRigWith(t, func(c *Config) {
		c.BusyNoticeAt = 1
		c.FirstEventGrace = time.Minute
	})
	seedFixedRouteTask(t, r, "discord:g1/fresh-a", 0)
	seedFixedRouteTask(t, r, "discord:g1/fresh-b", 0)
	seedFixedRouteTask(t, r, "discord:g1/stale-a", 2*time.Minute)
	seedFixedRouteTask(t, r, "discord:g1/stale-b", 2*time.Minute)
	started := seedFixedRouteTask(t, r, "discord:g1/started", 2*time.Minute)
	publishFirstEvent(t, r, started)

	n, err := r.g.fixedRouteBacklog(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	if n != 3 {
		t.Fatalf("backlog = %d, want 3 (two inside the grace, one past it that started; the two that never started left out)", n)
	}
	conv := "discord:g1/never-started-turn"
	busyTurn(r, conv, discordBackend, "anything")
	assertBusyLine(t, r, conv, busyNotice(3))
}

// TestBusyCountLeavesOutTasksThatAlreadyEnded: past the grace, a record
// whose task's newest event is its terminal is a record whose clear was lost
// (the shape the heal releases on that conversation's next turn), and it is
// not ahead of anyone. A supervisor terminal counts the same as the
// executor's.
func TestBusyCountLeavesOutTasksThatAlreadyEnded(t *testing.T) {
	r := startRigWith(t, func(c *Config) { c.FirstEventGrace = time.Minute })
	running := seedFixedRouteTask(t, r, "discord:g1/ended-running", 2*time.Minute)
	publishFirstEvent(t, r, running)
	ended := seedFixedRouteTask(t, r, "discord:g1/ended-done", 2*time.Minute)
	completeTask(t, publishFirstEvent(t, r, ended), "done")
	supervised := seedFixedRouteTask(t, r, "discord:g1/ended-supervisor", 2*time.Minute)
	publishFirstEvent(t, r, supervised)
	if err := r.g.publishSupervisorTerminal(context.Background(), "platform", supervised.ActiveTask.TaskID,
		supervised.ContextID, supervised.ActiveTask.CorrelationID, lib.StateFailed, "test"); err != nil {
		t.Fatal(err)
	}

	n, err := r.g.fixedRouteBacklog(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("backlog = %d, want 1 (the running task; the two whose terminal is on the stream left out)", n)
	}
}

// TestBusyCountFailuresSayNothingAndCountWhatTheyCannotRuleOut: a count that
// fails posts no notice; a stream read that fails counts the task, because a
// failed read cannot rule out events.
func TestBusyCountFailuresSayNothingAndCountWhatTheyCannotRuleOut(t *testing.T) {
	r := startRigWith(t, func(c *Config) {
		c.BusyNoticeAt = 1
		c.FirstEventGrace = time.Minute
	})
	rec := seedFixedRouteTask(t, r, "discord:g1/fail-a", 0)
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if n, busy := r.g.fixedRouteAhead(canceled, rec, discordBackend, ""); n != 0 || busy {
		t.Fatalf("fixedRouteAhead on a failed count = %d, %v; want 0, false", n, busy)
	}

	seedFixedRouteTask(t, r, "discord:g1/fail-stale", 2*time.Minute)
	if n, err := r.g.fixedRouteBacklog(context.Background(), ""); err != nil || n != 1 {
		t.Fatalf("backlog = %d, %v; want 1 with the never-started task left out", n, err)
	}
	waitForRelayDurable(t, r)
	deleteTasksStream(t, r.url)
	if n, err := r.g.fixedRouteBacklog(context.Background(), ""); err != nil || n != 2 {
		t.Fatalf("backlog with TASKS unreadable = %d, %v; want 2, the stale task counted", n, err)
	}
}

// waitForRelayDurable waits until the gateway's relay has bound its durable
// on TASKS, so deleting the stream does not race the gateway's Run.
func waitForRelayDurable(t *testing.T, r *rig) {
	t.Helper()
	waitFor(t, "the relay durable", func() bool {
		_, err := r.client.JetStream().Consumer(context.Background(), lib.TasksStream, relayDurable)
		return err == nil
	})
}

// TestBusyCountIsOnlyTheFixedAddressee: a task addressed to a conversation's
// own session pod is not in the fixed addressee's line, and the doors whose
// callers are programs get no notice even when the line is long.
func TestBusyCountIsOnlyTheFixedAddressee(t *testing.T) {
	r := startRigWith(t, withBusyNoticeAt(1))
	sess := &SessionRecord{
		Key: "discord:g1/own-session", ContextID: "ctx-own", Kind: "group",
		BusSession: "session-x1", Addressee: "session-x1", LastActivity: time.Now().UTC(),
		ActiveTask: &ActiveTask{TaskID: "task-own1", SubmittedAt: time.Now()},
		Tasks:      []TaskRef{{ID: "task-own1", Addressee: "session-x1"}},
	}
	if err := r.g.reg.Put(context.Background(), sess); err != nil {
		t.Fatal(err)
	}
	if n, err := r.g.fixedRouteBacklog(context.Background(), ""); err != nil || n != 0 {
		t.Fatalf("backlog = %d, %v; want 0 with only a session-addressed task", n, err)
	}

	seedFixedRouteTask(t, r, "discord:g1/one-ahead", 0)
	for _, backend := range []string{injectBackend, a2aBackend, a2aGoogleBackend} {
		conv := "discord:g1/door-" + backend
		busyTurn(r, conv, backend, "from a program")
		if got := busyPostsIn(r, conv); len(got) != 0 {
			t.Fatalf("busy notice posted through the %s door: %q", backend, got)
		}
		if got := r.adapter.editsOf(statusLineID(t, r, conv)); len(got) != 0 {
			t.Fatalf("status line edited through the %s door: %q", backend, got)
		}
	}
	// The seeded task and the three door turns are all ahead: the doors get
	// no notice, but their tasks still count.
	conv := "discord:g1/console-like"
	busyTurn(r, conv, consoleBackend, "from a person")
	assertBusyLine(t, r, conv, busyNotice(4))
}

// startBusyRig is a rig restartRig can replace, with the busy threshold set.
func startBusyRig(t *testing.T, at int) *rig {
	t.Helper()
	r, _ := startRigWithSpawnerCap(t, "platform", 0, withBusyNoticeAt(at))
	return r
}

// TestBusyCountIsRebuiltFromSessionStateAfterARestart: the count lives in
// session-state, not in the gateway's memory, so a gateway that has just
// started counts the tasks the previous one submitted.
func TestBusyCountIsRebuiltFromSessionStateAfterARestart(t *testing.T) {
	r := startBusyRig(t, 2)
	busyTurn(r, "discord:g1/r-a", discordBackend, "first")
	busyTurn(r, "discord:g1/r-b", discordBackend, "second")
	if got := submittedTexts(t, r); len(got) != 2 {
		t.Fatalf("submissions = %q, want two", got)
	}

	r2, _ := restartRig(t, r)
	conv := "discord:g1/r-c"
	busyTurn(r2, conv, discordBackend, "third")
	if got := busyLinesIn(t, r2, conv); len(got) != 1 || got[0] != busyNotice(2) {
		t.Fatalf("busy notices after restart = %q, want exactly %q", got, busyNotice(2))
	}
}

// TestBusyCountFallsWhenATaskEnds: a terminal deletes the active task from
// its record, and the count drops with it.
func TestBusyCountFallsWhenATaskEnds(t *testing.T) {
	r := startBusyRig(t, 2)
	busyTurn(r, "discord:g1/e-a", discordBackend, "first")
	busyTurn(r, "discord:g1/e-b", discordBackend, "second")
	origin := r.awaitTask(t, "platform")
	completeTask(t, r.execFor(t, origin, "platform"), "done")
	waitFor(t, "the terminal clears its record", activeTaskCleared(r, origin.TaskID))

	conv := "discord:g1/e-c"
	busyTurn(r, conv, discordBackend, "third")
	if got := busyLinesIn(t, r, conv); len(got) != 0 {
		t.Fatalf("busy notice with one task left of a threshold of two: %q", got)
	}
}

// TestBusyCountFallsWithTerminalsPublishedWhileTheGatewayWasDown: a terminal
// published while no gateway runs is delivered by the relay's durable once
// one is back, so the count a restarted gateway reads does not stay inflated
// by a task that ended during the gap.
func TestBusyCountFallsWithTerminalsPublishedWhileTheGatewayWasDown(t *testing.T) {
	r := startBusyRig(t, 2)
	busyTurn(r, "discord:g1/d-a", discordBackend, "first")
	busyTurn(r, "discord:g1/d-b", discordBackend, "second")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")

	r2, _ := restartRig(t, r, func() { completeTask(t, exec, "done while down") })
	waitFor(t, "the restarted relay clears the record", activeTaskCleared(r2, origin.TaskID))
	if n, err := r2.g.fixedRouteBacklog(context.Background(), ""); err != nil || n != 1 {
		t.Fatalf("backlog = %d, %v; want 1 once the missed terminal is relayed", n, err)
	}
	conv := "discord:g1/d-c"
	busyTurn(r2, conv, discordBackend, "third")
	if got := busyLinesIn(t, r2, conv); len(got) != 0 {
		t.Fatalf("busy notice from a count the missed terminal should have lowered: %q", got)
	}
}

// activeTaskCleared reports whether no session record still holds taskID as
// its active task.
func activeTaskCleared(r *rig, taskID string) func() bool {
	return func() bool {
		recs, err := r.g.reg.Sessions(context.Background())
		if err != nil {
			return false
		}
		for _, rec := range recs {
			if rec.ActiveTask != nil && rec.ActiveTask.TaskID == taskID {
				return false
			}
		}
		return true
	}
}

// TestFromEnvBusyNoticeAt: unset is the default of 10, a count passes
// through, and anything under 1 or not a number refuses at boot.
func TestFromEnvBusyNoticeAt(t *testing.T) {
	setBaseEnv(t)
	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.BusyNoticeAt != 10 {
		t.Fatalf("default BusyNoticeAt = %d, want 10", cfg.BusyNoticeAt)
	}
	t.Setenv("A2A_BUSY_NOTICE_AT", "4")
	if cfg, err = FromEnv(); err != nil || cfg.BusyNoticeAt != 4 {
		t.Fatalf("BusyNoticeAt = %+v, %v; want 4", cfg, err)
	}
	for _, bad := range []string{"0", "-1", "junk"} {
		t.Setenv("A2A_BUSY_NOTICE_AT", bad)
		if _, err := FromEnv(); err == nil {
			t.Fatalf("A2A_BUSY_NOTICE_AT=%q accepted", bad)
		}
	}
}

// Status-line states the relay renders in the rig, which leaves the display
// mode at debug.
const (
	workingLine   = "⚙️ **working**"
	completedLine = "✅ **completed**"
)

// relayStateOf reads a task's relay state under its conversation's session
// lock, the lock the relay renders under: once it shows a state, every edit
// the batch that set it makes has landed.
func relayStateOf(r *rig, conv, taskID string) (lib.TaskState, bool) {
	l := r.g.lockSession(conv)
	l.Lock()
	defer l.Unlock()
	r.g.mu.Lock()
	rs := r.g.relays[taskID]
	r.g.mu.Unlock()
	if rs == nil {
		return "", false
	}
	return rs.state, true
}

// showBusyLocked runs showBusy under the conversation's session lock, as
// routeTurn runs it.
func showBusyLocked(r *rig, rec *SessionRecord, taskID string, ahead int) {
	l := r.g.lockSession(rec.Key)
	l.Lock()
	defer l.Unlock()
	r.g.showBusy(rec, taskID, ahead)
}

// awaitLastEdit waits until the last edit of messageID is want.
func awaitLastEdit(t *testing.T, r *rig, messageID, want string) {
	t.Helper()
	waitFor(t, "the status line to read "+want, func() bool {
		edits := r.adapter.editsOf(messageID)
		return len(edits) > 0 && edits[len(edits)-1] == want
	})
}

// TestBusyLineIsReplacedByTheRelaysLaterEdits: the queued line holds through
// the executor's own submitted event (the bridge publishes one on accept,
// before a worker is free), and the relay's working and terminal edits then
// replace it, so the finished turn's line reads completed and the notice is
// nowhere in the thread.
func TestBusyLineIsReplacedByTheRelaysLaterEdits(t *testing.T) {
	r := startRigWith(t, withBusyNoticeAt(1))
	seedFixedRouteTask(t, r, "discord:g1/over-a", 0)

	conv := "discord:g1/over-turn"
	busyTurn(r, conv, discordBackend, "what is 2 + 2?")
	line := statusLineID(t, r, conv)
	assertBusyLine(t, r, conv, busyNotice(1))

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the relay to take the submitted event", func() bool {
		st, ok := relayStateOf(r, conv, origin.TaskID)
		return ok && st == lib.StateSubmitted
	})
	if got := r.adapter.editsOf(line); len(got) != 1 || got[0] != busyNotice(1) {
		t.Fatalf("edits after the executor's submitted = %q, want the queued line alone", got)
	}

	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	awaitLastEdit(t, r, line, workingLine)
	completeTask(t, exec, "2 + 2 is 4.")
	awaitLastEdit(t, r, line, completedLine)

	want := []string{busyNotice(1), workingLine, completedLine}
	if got := r.adapter.editsOf(line); strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("status line edits = %q, want %q", got, want)
	}
	if got := busyPostsIn(r, conv); len(got) != 0 {
		t.Fatalf("busy notice posted: %q", got)
	}
}

// TestBusyLinePastSubmittedIsLeftAlone: the line never moves backwards. A
// busy notice that arrives after the relay has rendered working, or the
// terminal, is skipped, and nothing is posted in its place. From routeTurn
// the session lock keeps the relay out until the turn ends, so the notice
// always finds the line submitted there; this drives showBusy directly with
// the line already past it, the race the guard is for if that ordering ever
// changes.
func TestBusyLinePastSubmittedIsLeftAlone(t *testing.T) {
	r := startRigWith(t, withBusyNoticeAt(100))
	conv := "discord:g1/race-turn"
	busyTurn(r, conv, discordBackend, "anything")
	line := statusLineID(t, r, conv)
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")

	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	awaitLastEdit(t, r, line, workingLine)
	rec, err := r.g.reg.Get(context.Background(), conv)
	if err != nil || rec == nil || rec.ActiveTask == nil || rec.ActiveTask.TaskID != origin.TaskID {
		t.Fatalf("record = %+v, %v; want the turn's task active", rec, err)
	}
	showBusyLocked(r, rec, origin.TaskID, 3)
	if got := r.adapter.editsOf(line); len(got) != 1 || got[0] != workingLine {
		t.Fatalf("edits after a late busy notice on a working line = %q, want working alone", got)
	}
	if got := postsIn(r, conv); len(got) != 1 {
		t.Fatalf("posts after a late busy notice = %+v, want the placeholder alone", got)
	}

	// The terminal: rec still names the task active, as a count that began
	// before the terminal would hold it.
	completeTask(t, exec, "done")
	awaitLastEdit(t, r, line, completedLine)
	posts := len(postsIn(r, conv))
	showBusyLocked(r, rec, origin.TaskID, 3)
	if got := r.adapter.editsOf(line); got[len(got)-1] != completedLine || len(busyLinesIn(t, r, conv)) != 0 {
		t.Fatalf("edits after a late busy notice on a finished line = %q, want completed last and no queued", got)
	}
	if got := len(postsIn(r, conv)); got != posts {
		t.Fatalf("a late busy notice on a finished line posted %d messages", got-posts)
	}
}

// TestBusyNoticeWithNoStatusLineIsPosted: a turn whose placeholder post
// failed has no line to edit, and gets the notice as a post.
func TestBusyNoticeWithNoStatusLineIsPosted(t *testing.T) {
	r := startRigWith(t, nil)
	conv := "discord:g1/no-line"
	rec := &SessionRecord{Key: conv, ActiveTask: &ActiveTask{TaskID: "task-noline"}}
	showBusyLocked(r, rec, "task-noline", 2)
	if got := busyPostsIn(r, conv); len(got) != 1 || got[0] != busyNotice(2) {
		t.Fatalf("busy posts with no status line = %q, want exactly %q", got, busyNotice(2))
	}
	if got := r.adapter.editTexts(); len(got) != 0 {
		t.Fatalf("edits with no status line: %q", got)
	}
}
