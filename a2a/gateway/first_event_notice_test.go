package gateway

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// noticePosts returns the posts that are the reap scan's no-first-event
// notice for the given task.
func noticePosts(a *fakeAdapter, taskID string) []string {
	var out []string
	for _, p := range a.postTexts() {
		if strings.Contains(p, taskID) && strings.Contains(p, "your next message here starts a new task instead of going to it") {
			out = append(out, p)
		}
	}
	return out
}

// seedTasklessFixed is seedTasklessDelegate on the fixed route: the task went
// to the standing executor (platform), which never picked it up.
func seedTasklessFixed(t *testing.T, r *rig, conv string, age time.Duration) *SessionRecord {
	t.Helper()
	rec := seedTasklessDelegate(t, r, conv, age)
	rec.BusSession, rec.Addressee = "", "platform"
	rec.Tasks = []TaskRef{{ID: rec.ActiveTask.TaskID, Addressee: "platform"}}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	return rec
}

// startedNoticeAge is how old the tests that seed a started task make it:
// past the grace and inside the notice ceiling (noticeCeilingGraces × grace,
// 30m at the default), the only window in which firstEventOverdue reads the
// stream. A started task aged past the ceiling is turned away by the age
// bound before the read, so a test about what the read finds would pass
// whatever the read does. countNoticeReads fails a test whose seeds have
// left the window, so a ceiling change cannot empty one silently.
const startedNoticeAge = defaultFirstEventGrace + 5*time.Minute

// countNoticeReads arms the notice's stream-read hook on r and returns the
// number of reads made so far for a task ID. It first checks that
// startedNoticeAge is still inside the window the read is made in.
func countNoticeReads(t *testing.T, r *rig) func(taskID string) int {
	t.Helper()
	grace := r.g.cfg.FirstEventGrace
	if startedNoticeAge <= grace || startedNoticeAge > noticeCeilingGraces*grace {
		t.Fatalf("startedNoticeAge %v is outside (grace %v, ceiling %v]; the notice would not read the stream",
			startedNoticeAge, grace, noticeCeilingGraces*grace)
	}
	var mu sync.Mutex
	reads := map[string]int{}
	r.g.noticeStreamReadHook = func(taskID string) {
		mu.Lock()
		reads[taskID]++
		mu.Unlock()
	}
	return func(taskID string) int {
		mu.Lock()
		defer mu.Unlock()
		return reads[taskID]
	}
}

// TestNoFirstEventNoticePostsOnceWithoutATurn (#2405): a task with nothing on
// its event stream past FirstEventGrace is announced by the reap scan, with no
// inbound message, exactly once however many passes run. The notice releases
// nothing and publishes nothing: the record still serializes on the task, the
// stream is still empty, and the conversation's next message is what the heal
// releases it on, as the notice says.
func TestNoFirstEventNoticePostsOnceWithoutATurn(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()
	conv := "discord:g1/thread-notice-once"
	seedTasklessFixed(t, r, conv, defaultFirstEventGrace+time.Minute)

	r.g.reapOnce(ctx)
	if got := noticePosts(r.adapter, "task-never"); len(got) != 1 {
		t.Fatalf("after one reap pass, %d notices, want 1; posts: %q", len(got), r.adapter.postTexts())
	}
	r.g.reapOnce(ctx)
	r.g.reapOnce(ctx)
	if got := noticePosts(r.adapter, "task-never"); len(got) != 1 {
		t.Fatalf("after three reap passes, %d notices, want 1; posts: %q", len(got), r.adapter.postTexts())
	}

	// Not released early: the record still holds the task, with the marker,
	// and nothing was published for the task on either event subject.
	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil || rec == nil || rec.ActiveTask == nil || rec.ActiveTask.TaskID != "task-never" {
		t.Fatalf("the notice released the record: %+v (err=%v)", rec, err)
	}
	if rec.ActiveTask.NoFirstEventNoticeAt.IsZero() {
		t.Fatal("the notice was posted but its marker is not on the record")
	}
	if _, err := r.client.TasksGet(ctx, "platform", "task-never"); !isTaskNotFound(err) {
		t.Fatalf("the notice put something on the task's stream: TasksGet err = %v", err)
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "this conversation is released") {
			t.Fatalf("the reap scan posted the heal's release line: %q", p)
		}
	}

	// The notice's promise holds: the next message starts a new task.
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "n-1", Text: "check the fleet again"}
	waitFor(t, "a new task after the notice", func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
			if e.Kind == lib.KindMessage && e.TaskID != "task-never" && e.ContextID == rec.ContextID {
				return true
			}
		}
		return false
	})
}

// TestNoFirstEventNoticeNotRepeatedAfterRestart: the once-per-task rule holds
// across a gateway restart. A second gateway on the same bus has none of the
// first one's memory; the marker on the record is what stops it posting the
// same notice again.
func TestNoFirstEventNoticeNotRepeatedAfterRestart(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()
	conv := "discord:g1/thread-notice-restart"
	seedTasklessFixed(t, r, conv, defaultFirstEventGrace+time.Minute)

	r.g.reapOnce(ctx)
	if got := noticePosts(r.adapter, "task-never"); len(got) != 1 {
		t.Fatalf("first gateway: %d notices, want 1; posts: %q", len(got), r.adapter.postTexts())
	}

	restarted := newFakeAdapter()
	g2, err := New(Options{Client: r.client, Adapter: restarted, Config: r.g.cfg, Backend: "discord"})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	g2.reapOnce(ctx)
	g2.reapOnce(ctx)
	if got := restarted.postTexts(); len(got) != 0 {
		t.Fatalf("the restarted gateway posted again for a task already noticed: %q", got)
	}
}

// TestNoFirstEventNoticeSkipsTasksThatStarted: a task whose first event
// arrived, past the grace and inside the ceiling where the notice reads the
// stream, and a task with nothing yet but still inside the grace, get no
// notice.
func TestNoFirstEventNoticeSkipsTasksThatStarted(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()

	started := "discord:g1/thread-notice-started"
	r.adapter.inbox <- InboundMessage{Conversation: started, Kind: "group",
		AuthorID: "1001", MessageID: "s-1", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	if err := r.execFor(t, origin, "platform").PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	// Age the started task past the grace but inside the ceiling
	// (startedNoticeAge), under the session lock so the relay's own write of
	// the record cannot interleave with this one. Past the ceiling the age
	// bound alone would keep the notice away, and the submitted event would
	// go untested.
	l := r.g.lockSession(started)
	l.Lock()
	rec, err := r.g.reg.Get(ctx, started)
	if err != nil || rec == nil || rec.ActiveTask == nil || rec.ActiveTask.TaskID != origin.TaskID {
		l.Unlock()
		t.Fatalf("started task not on the record: %+v (err=%v)", rec, err)
	}
	rec.ActiveTask.SubmittedAt = time.Now().Add(-startedNoticeAge)
	if err := r.g.reg.Put(ctx, rec); err != nil {
		l.Unlock()
		t.Fatal(err)
	}
	l.Unlock()

	young := "discord:g1/thread-notice-young"
	seedTasklessFixed(t, r, young, time.Minute)

	// A task whose only event is on its supervisor subject is not empty
	// (the fold reads both subjects): a spawn failure's terminal, say,
	// before the relay has rendered it. Any message there counts, as it
	// does for the replay.
	supervised := "discord:g1/thread-notice-supervised"
	rec2 := seedTasklessFixed(t, r, supervised, defaultFirstEventGrace+time.Minute)
	rec2.ActiveTask.TaskID = "task-supervised"
	rec2.Tasks = []TaskRef{{ID: "task-supervised", Addressee: "platform"}}
	if err := r.g.reg.Put(ctx, rec2); err != nil {
		t.Fatal(err)
	}
	if _, err := r.client.JetStream().Publish(ctx, lib.TaskSupervisorSubject("platform", "task-supervised"), []byte("{}")); err != nil {
		t.Fatal(err)
	}

	reads := countNoticeReads(t, r)
	r.g.reapOnce(ctx)
	r.g.reapOnce(ctx)
	if got := noticePosts(r.adapter, origin.TaskID); len(got) != 0 {
		t.Fatalf("a task with a first event was noticed: %q", got)
	}
	for _, id := range []string{origin.TaskID, "task-supervised"} {
		if reads(id) == 0 {
			t.Fatalf("the reap scan never read the stream for %s; a bound answered first, so its event went untested", id)
		}
	}
	if got := noticePosts(r.adapter, "task-never"); len(got) != 0 {
		t.Fatalf("a task inside the grace was noticed: %q", got)
	}
	if got := noticePosts(r.adapter, "task-supervised"); len(got) != 0 {
		t.Fatalf("a task with an event on its supervisor subject was noticed: %q", got)
	}
}

// TestNoFirstEventNoticeSkipsDetachedAndStaleScans: a detached task no longer
// holds the conversation and gets no notice; and a scan that read the record
// before a turn swapped in a new task neither posts for the old task nor
// marks the new one, because the fresh record under the lock is what decides.
func TestNoFirstEventNoticeSkipsDetachedAndStaleScans(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()

	detached := "discord:g1/thread-notice-detached"
	rec := seedTasklessFixed(t, r, detached, defaultFirstEventGrace+time.Minute)
	rec.ActiveTask.Detached = true
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	r.g.reapOnce(ctx)
	if got := noticePosts(r.adapter, "task-never"); len(got) != 0 {
		t.Fatalf("a detached task was noticed: %q", got)
	}

	swapped := "discord:g1/thread-notice-swapped"
	current := seedTasklessFixed(t, r, swapped, defaultFirstEventGrace+time.Minute)
	current.ActiveTask.TaskID = "task-current"
	current.Tasks = append(current.Tasks, TaskRef{ID: "task-current", Addressee: "platform"})
	if err := r.g.reg.Put(ctx, current); err != nil {
		t.Fatal(err)
	}
	stale := *current
	staleTask := *current.ActiveTask
	staleTask.TaskID = "task-never"
	stale.ActiveTask = &staleTask
	r.g.noticeNoFirstEvent(ctx, &stale)
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "starts a new task instead of going to it") {
			t.Fatalf("a stale scan posted a notice: %q", p)
		}
	}
	got, err := r.g.reg.Get(ctx, swapped)
	if err != nil || got == nil || got.ActiveTask == nil {
		t.Fatalf("record lost: %+v (err=%v)", got, err)
	}
	if !got.ActiveTask.NoFirstEventNoticeAt.IsZero() {
		t.Fatal("a stale scan marked the task that replaced the one it read")
	}
}

// TestNoFirstEventNoticeOpensNoConsumer: the reap scan asks every record past
// the grace whether its task has a first event, once a minute, for as long as
// the record holds the task. A replay per question would open an ephemeral
// consumer on TASKS each time, out of a consumer budget sized without this
// caller; the question is answered with direct gets instead.
func TestNoFirstEventNoticeOpensNoConsumer(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()
	const conversations = 5
	var taskIDs []string
	for i := 0; i < conversations; i++ {
		conv := "discord:g1/thread-notice-budget-" + string(rune('a'+i))
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
			AuthorID: "1001", MessageID: "b-" + conv, Text: "check the fleet"}
		var taskID string
		waitFor(t, "task on the record for "+conv, func() bool {
			rec, err := r.g.reg.Get(ctx, conv)
			if err == nil && rec != nil && rec.ActiveTask != nil {
				taskID = rec.ActiveTask.TaskID
				return true
			}
			return false
		})
		var origin *lib.Envelope
		waitFor(t, "submission for "+conv, func() bool {
			for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
				if e.Kind == lib.KindMessage && e.TaskID == taskID {
					origin = e
					return true
				}
			}
			return false
		})
		if err := r.execFor(t, origin, "platform").PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
			t.Fatal(err)
		}
		taskIDs = append(taskIDs, taskID)
		// Inside the ceiling (startedNoticeAge), so the scan reads the
		// stream for this task; past it no read is made and a replay there
		// would never run.
		l := r.g.lockSession(conv)
		l.Lock()
		rec, err := r.g.reg.Get(ctx, conv)
		if err == nil && rec != nil && rec.ActiveTask != nil {
			rec.ActiveTask.SubmittedAt = time.Now().Add(-startedNoticeAge)
			err = r.g.reg.Put(ctx, rec)
		}
		l.Unlock()
		if err != nil {
			t.Fatal(err)
		}
	}
	stream, err := r.client.JetStream().Stream(ctx, lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	consumers := func() int {
		info, err := stream.Info(ctx)
		if err != nil {
			t.Fatal(err)
		}
		return info.State.Consumers
	}
	reads := countNoticeReads(t, r)
	before := consumers()
	r.g.reapOnce(ctx)
	r.g.reapOnce(ctx)
	for _, id := range taskIDs {
		if reads(id) == 0 {
			t.Fatalf("the reap scan never read the stream for %s; a bound answered first, so the read went untested", id)
		}
	}
	if after := consumers(); after > before {
		t.Fatalf("reap passes over %d started tasks past the grace opened consumers: %d before, %d after", conversations, before, after)
	}
}

// TestSteerOpensNoConsumerOfItsOwn: the steer acknowledgement asks the stream
// whether the task has a first event with direct gets. The turn's heal still
// replays the task (one ephemeral consumer, as before this check existed);
// the steer must not add a second.
func TestSteerOpensNoConsumerOfItsOwn(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()
	conv := "discord:g1/thread-steer-budget"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "sb-1", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	if err := r.execFor(t, origin, "platform").PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	stream, err := r.client.JetStream().Stream(ctx, lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	consumers := func() int {
		info, err := stream.Info(ctx)
		if err != nil {
			t.Fatal(err)
		}
		return info.State.Consumers
	}
	before := consumers()
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "sb-2", Text: "actually only prod"}
	waitFor(t, "steer ack", func() bool {
		for _, p := range r.adapter.postTexts() {
			// Either route's ack: the fixed route queues it ("I'll take that
			// next"), the no-first-event line says "steering sent".
			if strings.Contains(p, "steering sent") || p == ackSteerQueued {
				return true
			}
		}
		return false
	})
	if after := consumers(); after > before+1 {
		t.Fatalf("a steer turn opened %d consumers; the heal's replay accounts for one", after-before)
	}
}

// TestSteerIntoTaskWithNoFirstEventPromisesNoReply (#2405 b): a steer into a
// task with nothing on its stream is still sent, but the acknowledgement
// promises no reply on either route, and says when the conversation frees up.
func TestSteerIntoTaskWithNoFirstEventPromisesNoReply(t *testing.T) {
	for name, seed := range map[string]func(*testing.T, *rig, string, time.Duration) *SessionRecord{
		"fixed":   seedTasklessFixed,
		"session": seedTasklessDelegate,
	} {
		t.Run(name, func(t *testing.T) {
			r := startRig(t)
			conv := "discord:g1/thread-steer-silent-" + name
			rec := seed(t, r, conv, time.Minute)
			r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
				AuthorID: "1001", MessageID: "ss-" + name, Text: "make it about otters"}
			var ack string
			waitFor(t, "steer ack", func() bool {
				for _, p := range r.adapter.postTexts() {
					if strings.Contains(p, "steering sent") {
						ack = p
						return true
					}
				}
				return false
			})
			for _, promise := range []string{"its reply will say so", "picks it up at its next turn boundary"} {
				if strings.Contains(ack, promise) {
					t.Fatalf("steer into a task with no first event promised a reply: %q", ack)
				}
			}
			if !strings.Contains(ack, "task-never") || !strings.Contains(ack, "no reply is promised") ||
				!strings.Contains(ack, defaultFirstEventGrace.String()) {
				t.Fatalf("ack does not say the task is silent and when the conversation frees up: %q", ack)
			}
			// Still sent: the steer is on the task's in subject.
			var sent bool
			for _, e := range inSubjectEnvelopes(t, r.url, rec.Addressee) {
				if e.Kind == lib.KindMessage && e.TaskID == "task-never" {
					sent = true
				}
			}
			if !sent {
				t.Fatal("the steer was not published")
			}
		})
	}
}

// TestNoFirstEventPastGrace pins the shared test the heal and the notice
// both draw the line with: an empty stream only, strictly past the grace, and
// never on a task with no age.
func TestNoFirstEventPastGrace(t *testing.T) {
	now := time.Now()
	grace := defaultFirstEventGrace
	aged := func(age time.Duration) *ActiveTask { return &ActiveTask{TaskID: "t", SubmittedAt: now.Add(-age)} }
	for name, tc := range map[string]struct {
		active *ActiveTask
		empty  bool
		want   bool
	}{
		"empty past the grace":   {aged(grace + time.Second), true, true},
		"empty exactly at grace": {aged(grace), true, false},
		"empty inside the grace": {aged(time.Minute), true, false},
		"events past the grace":  {aged(grace + time.Hour), false, false},
		"no submittedAt":         {&ActiveTask{TaskID: "t"}, true, false},
		"no active task":         {nil, true, false},
	} {
		if got := noFirstEventPastGrace(tc.active, tc.empty, grace, now); got != tc.want {
			t.Errorf("%s: got %v, want %v", name, got, tc.want)
		}
	}
}

// seedTasklessFixedAs is seedTasklessFixed with its own task id, so several
// records in one rig can be told apart in the posts.
func seedTasklessFixedAs(t *testing.T, r *rig, conv, taskID string, age time.Duration) *SessionRecord {
	t.Helper()
	rec := seedTasklessFixed(t, r, conv, age)
	rec.ActiveTask.TaskID = taskID
	rec.Tasks = []TaskRef{{ID: taskID, Addressee: "platform"}}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	return rec
}

// TestNoFirstEventNoticeAgeCeiling (round-1 review of #2412): the notice is
// for the placeholder somebody may still be watching. A task twice the grace
// old is noticed, once, however many passes run; one four times the grace old
// is past the ceiling and is not, so the first pass after a rollout does not
// post into every conversation that wedged in the last SessionTTL.
func TestNoFirstEventNoticeAgeCeiling(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()
	seedTasklessFixedAs(t, r, "discord:g1/thread-notice-2x", "task-2x", 2*defaultFirstEventGrace)
	seedTasklessFixedAs(t, r, "discord:g1/thread-notice-4x", "task-4x", 4*defaultFirstEventGrace)

	r.g.reapOnce(ctx)
	r.g.reapOnce(ctx)
	if got := noticePosts(r.adapter, "task-2x"); len(got) != 1 {
		t.Fatalf("task at 2x the grace: %d notices, want 1; posts: %q", len(got), r.adapter.postTexts())
	}
	if got := noticePosts(r.adapter, "task-4x"); len(got) != 0 {
		t.Fatalf("task at 4x the grace, past the ceiling, was noticed: %q", got)
	}
	rec, err := r.g.reg.Get(ctx, "discord:g1/thread-notice-4x")
	if err != nil || rec == nil || rec.ActiveTask == nil {
		t.Fatalf("record past the ceiling lost: %+v (err=%v)", rec, err)
	}
	if !rec.ActiveTask.NoFirstEventNoticeAt.IsZero() {
		t.Fatal("a task past the ceiling was marked as noticed")
	}
}

// TestNoFirstEventNoticeSkipsRecordDueForPrune (round-1 review of #2412): a
// record the same reap pass deletes past SessionTTL gets no notice, because
// the conversation would be told about a task whose record is gone a few
// lines later. Two shapes: the ordinary one, where the task is as old as the
// record and the ceiling alone would skip it; and one whose task is inside
// the ceiling, which only the prune check stops.
func TestNoFirstEventNoticeSkipsRecordDueForPrune(t *testing.T) {
	r := startRigWith(t, func(c *Config) {
		c.SessionTTL = 24 * time.Hour
		c.TaskDeadline = defaultFirstEventGrace
	})
	ctx := context.Background()

	cases := map[string]time.Duration{
		"discord:g1/thread-notice-prune-old":   25 * time.Hour,
		"discord:g1/thread-notice-prune-young": 2 * defaultFirstEventGrace,
	}
	for conv, taskAge := range cases {
		taskID := "task-" + conv[len("discord:g1/thread-notice-"):]
		rec := seedTasklessFixedAs(t, r, conv, taskID, taskAge)
		rec.LastActivity = time.Now().Add(-25 * time.Hour).UTC()
		if err := r.g.reg.Put(ctx, rec); err != nil {
			t.Fatal(err)
		}
	}

	r.g.reapOnce(ctx)
	for conv := range cases {
		taskID := "task-" + conv[len("discord:g1/thread-notice-"):]
		if got := noticePosts(r.adapter, taskID); len(got) != 0 {
			t.Errorf("%s: a record pruned on the same pass was noticed: %q", conv, got)
		}
		if rec, err := r.g.reg.Get(ctx, conv); err != nil || rec != nil {
			t.Errorf("%s: record not pruned (rec=%+v err=%v); the test no longer exercises the prune", conv, rec, err)
		}
	}
}

// TestWithinNoticeCeiling pins the notice's upper bound: inclusive at
// noticeCeilingGraces × grace (30m at the 10m default), out one second past.
func TestWithinNoticeCeiling(t *testing.T) {
	now := time.Now()
	grace := defaultFirstEventGrace
	if noticeCeilingGraces*grace != 30*time.Minute {
		t.Fatalf("ceiling at the default grace = %v, want 30m", noticeCeilingGraces*grace)
	}
	for name, tc := range map[string]struct {
		age  time.Duration
		want bool
	}{
		"2x the grace":            {2 * grace, true},
		"exactly at the ceiling":  {3 * grace, true},
		"one second past ceiling": {3*grace + time.Second, false},
		"4x the grace":            {4 * grace, false},
	} {
		if got := withinNoticeCeiling(now.Add(-tc.age), grace, now); got != tc.want {
			t.Errorf("%s: got %v, want %v", name, got, tc.want)
		}
	}
}
