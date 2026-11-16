package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const scheduleConversation = "trigger:schedule/kubeagents-system/compliance-audit"

// fakeHomeReporter is a chat.notify route as the scheduled delivery sees it:
// a home channel ("" for none) and a post that fails while fail says so.
type fakeHomeReporter struct {
	mu    sync.Mutex
	home  string
	fail  func(call int) bool
	posts []string
}

func (f *fakeHomeReporter) homeChannel() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.home
}

func (f *fakeHomeReporter) postHomeReport(_ context.Context, home, thread, text string) (homePost, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.posts = append(f.posts, text)
	if f.fail != nil && f.fail(len(f.posts)) {
		return homePost{}, errors.New("slack answered 503")
	}
	return homePost{Platform: lib.NotifyPlatformSlack, ChatID: home, ThreadID: "1763100000.000100", MessageID: "1763100000.000100"}, nil
}

func (f *fakeHomeReporter) calls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.posts...)
}

// runScheduled submits s on a trigger rig, lets tweak change the
// conversation's record before the run ends, and ends the run with result.
// It returns the run's task id.
func runScheduled(t *testing.T, tr *triggerRig, s lib.TriggerSubmit, result string, tweak func(*SessionRecord)) string {
	t.Helper()
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	var origin lib.Envelope
	if err := json.Unmarshal(tr.submissions()[0].Data, &origin); err != nil {
		t.Fatal(err)
	}
	if tweak != nil {
		ctx := context.Background()
		rec, err := tr.g.reg.Get(ctx, scheduleConversation)
		if err != nil || rec == nil {
			t.Fatalf("trigger record: %v", err)
		}
		tweak(rec)
		if err := tr.g.reg.Put(ctx, rec); err != nil {
			t.Fatal(err)
		}
	}
	completeTask(t, tr.execFor(t, &origin, s.Profile), result)
	return reply.TaskID
}

// readScheduledReport is the run's record, nil when there is none.
func readScheduledReport(t *testing.T, r *rig, jobKey, taskID string) *ScheduledReport {
	t.Helper()
	kv, err := r.client.KV(context.Background(), lib.ScheduledReportsBucket)
	if err != nil {
		t.Fatal(err)
	}
	rep, _, err := getScheduledReport(context.Background(), kv, scheduledReportKey(jobKey, taskID))
	if err != nil {
		t.Fatal(err)
	}
	return rep
}

// waitForScheduledReport polls the run's record until cond holds.
func waitForScheduledReport(t *testing.T, r *rig, jobKey, taskID, what string, cond func(*ScheduledReport) bool) *ScheduledReport {
	t.Helper()
	var rep *ScheduledReport
	waitFor(t, what, func() bool {
		rep = readScheduledReport(t, r, jobKey, taskID)
		return rep != nil && cond(rep)
	})
	return rep
}

// waitForRunEnded waits for the run's terminal to finish on the record.
func waitForRunEnded(t *testing.T, tr *triggerRig) {
	t.Helper()
	waitForRecord(t, tr.rig, scheduleConversation, "the run's terminal", func(rec *SessionRecord) bool {
		return rec.ActiveTask == nil
	})
}

// TestAScheduledRunThatFoundNothingIsRecordedSilentAndNotPosted: a
// silent-when-clean schedule whose run reports [SILENT] is recorded silent
// and posts nothing.
func TestAScheduledRunThatFoundNothingIsRecordedSilentAndNotPosted(t *testing.T) {
	t.Parallel()
	tr := startTriggerRig(t, nil)
	home := &fakeHomeReporter{home: "C0HOME"}
	tr.g.setHomeReporters(home)
	taskID := runScheduled(t, tr, goodScheduleSubmit(), " [SILENT]\n", nil)
	rep := waitForScheduledReport(t, tr.rig, "compliance-audit", taskID, "the silent record", func(r *ScheduledReport) bool { return true })
	if rep.Status != scheduledSilent || !rep.Silent {
		t.Fatalf("record = %+v, want silent", rep)
	}
	if rep.TriggerName != "schedule/compliance-audit" || rep.Profile != "fleet-audit" || rep.State != lib.StateCompleted || rep.FinishedAt.IsZero() {
		t.Fatalf("record = %+v", rep)
	}
	waitForRunEnded(t, tr)
	if posts := home.calls(); len(posts) != 0 {
		t.Fatalf("posted %q", posts)
	}
}

// TestALocalScheduledRunIsRecordedAndNotPosted: delivery mode local keeps
// the report and posts nothing, findings or not.
func TestALocalScheduledRunIsRecordedAndNotPosted(t *testing.T) {
	t.Parallel()
	tr := startTriggerRig(t, nil)
	home := &fakeHomeReporter{home: "C0HOME"}
	tr.g.setHomeReporters(home)
	s := goodScheduleSubmit()
	s.Delivery.Mode = lib.ScheduleDeliveryLocal
	taskID := runScheduled(t, tr, s, "Two findings.", nil)
	rep := waitForScheduledReport(t, tr.rig, "compliance-audit", taskID, "the local record", func(r *ScheduledReport) bool { return true })
	if rep.Status != scheduledLocal || rep.Text != "Two findings." || rep.Silent {
		t.Fatalf("record = %+v, want local with the report", rep)
	}
	waitForRunEnded(t, tr)
	if posts := home.calls(); len(posts) != 0 {
		t.Fatalf("posted %q", posts)
	}
}

// TestAScheduledRunWithNoDeliveryIsRecordedAndNotPosted: a conversation
// whose record carries no delivery posts nothing, as before, and the run is
// recorded no_delivery under a job key taken from the trigger's name.
func TestAScheduledRunWithNoDeliveryIsRecordedAndNotPosted(t *testing.T) {
	t.Parallel()
	tr := startTriggerRig(t, nil)
	home := &fakeHomeReporter{home: "C0HOME"}
	tr.g.setHomeReporters(home)
	s := goodScheduleSubmit()
	s.Delivery.JobKey = "daily-audit"
	taskID := runScheduled(t, tr, s, "Two findings.", func(rec *SessionRecord) {
		rec.Delivery = nil
		for i := range rec.Tasks {
			rec.Tasks[i].Delivery = nil
		}
	})
	rep := waitForScheduledReport(t, tr.rig, "compliance-audit", taskID, "the no_delivery record", func(r *ScheduledReport) bool { return true })
	if rep.Status != scheduledNoDelivery || rep.JobKey != "compliance-audit" {
		t.Fatalf("record = %+v, want no_delivery under compliance-audit", rep)
	}
	waitForRunEnded(t, tr)
	if posts := home.calls(); len(posts) != 0 {
		t.Fatalf("posted %q", posts)
	}
}

// TestAScheduledRunWithNoHomeChannelIsStoredAndTheTaskEnds: chat delivery on
// an install with no home channel records stored_no_home, and the run's
// terminal finishes as any other: the task is not failed or held.
func TestAScheduledRunWithNoHomeChannelIsStoredAndTheTaskEnds(t *testing.T) {
	t.Parallel()
	tr := startTriggerRig(t, nil)
	home := &fakeHomeReporter{home: ""}
	tr.g.setHomeReporters(home)
	taskID := runScheduled(t, tr, goodScheduleSubmit(), "Two findings.", nil)
	rep := waitForScheduledReport(t, tr.rig, "compliance-audit", taskID, "the stored record", func(r *ScheduledReport) bool { return true })
	if rep.Status != scheduledStoredNoHome || rep.Text != "Two findings." || rep.State != lib.StateCompleted {
		t.Fatalf("record = %+v, want stored_no_home", rep)
	}
	waitForRunEnded(t, tr)
	if posts := home.calls(); len(posts) != 0 {
		t.Fatalf("posted %q", posts)
	}
	waitFor(t, "the run's route retired", func() bool { return tr.g.sessionForTask(context.Background(), taskID) == "" })
}

// TestAScheduledRunPostsToTheHomeChannel: chat delivery with findings posts
// the report once and records where it landed.
func TestAScheduledRunPostsToTheHomeChannel(t *testing.T) {
	t.Parallel()
	tr := startTriggerRig(t, nil)
	tr.g.setHomeReporters(&fakeHomeReporter{home: ""}, &fakeHomeReporter{home: "C0HOME"})
	home := tr.g.homeReporters[1].(*fakeHomeReporter)
	taskID := runScheduled(t, tr, goodScheduleSubmit(), "Two findings.", nil)
	rep := waitForScheduledReport(t, tr.rig, "compliance-audit", taskID, "the posted record", func(r *ScheduledReport) bool { return r.Status == scheduledPosted })
	if rep.Platform != lib.NotifyPlatformSlack || rep.ChatID != "C0HOME" || rep.ThreadID == "" || rep.Attempts != 1 || rep.LastError != "" {
		t.Fatalf("record = %+v", rep)
	}
	waitForRunEnded(t, tr)
	if posts := home.calls(); len(posts) != 1 || posts[0] != "Two findings." {
		t.Fatalf("posts = %q, want the report once", posts)
	}
}

// TestAFailedScheduledPostIsRetriedThenGivenUp: a post that fails leaves the
// record posting with the error and hands the terminal back, which holds the
// run open; each redelivery tries again, and the last attempt marks the
// record post_failed and lets the terminal finish.
func TestAFailedScheduledPostIsRetriedThenGivenUp(t *testing.T) {
	t.Parallel()
	tr := startTriggerRig(t, nil)
	tr.g.scheduledRetry = 300 * time.Millisecond
	home := &fakeHomeReporter{home: "C0HOME", fail: func(int) bool { return true }}
	tr.g.setHomeReporters(home)
	taskID := runScheduled(t, tr, goodScheduleSubmit(), "Two findings.", nil)
	rep := waitForScheduledReport(t, tr.rig, "compliance-audit", taskID, "the first failed attempt", func(r *ScheduledReport) bool { return r.Attempts == 1 })
	if rep.Status != scheduledPosting || rep.LastError == "" {
		t.Fatalf("record after one failure = %+v, want posting with the error", rep)
	}
	rec, err := tr.g.reg.Get(context.Background(), scheduleConversation)
	if err != nil || rec == nil || rec.ActiveTask == nil || rec.ActiveTask.TaskID != taskID {
		t.Fatalf("the run ended before its post settled: %+v (%v)", rec, err)
	}
	rep = waitForScheduledReport(t, tr.rig, "compliance-audit", taskID, "post_failed", func(r *ScheduledReport) bool { return r.Status == scheduledPostFailed })
	if rep.Attempts != scheduledPostAttempts || rep.LastError == "" {
		t.Fatalf("record = %+v, want %d attempts", rep, scheduledPostAttempts)
	}
	waitForRunEnded(t, tr)
	time.Sleep(3 * tr.g.scheduledRetry)
	if posts := home.calls(); len(posts) != scheduledPostAttempts {
		t.Fatalf("%d posts, want %d", len(posts), scheduledPostAttempts)
	}
}

// TestAFailedScheduledPostThatLandsOnRetryIsPosted: a post that fails once
// and lands on the redelivery is posted, once.
func TestAFailedScheduledPostThatLandsOnRetryIsPosted(t *testing.T) {
	t.Parallel()
	tr := startTriggerRig(t, nil)
	tr.g.scheduledRetry = 100 * time.Millisecond
	home := &fakeHomeReporter{home: "C0HOME", fail: func(call int) bool { return call == 1 }}
	tr.g.setHomeReporters(home)
	taskID := runScheduled(t, tr, goodScheduleSubmit(), "Two findings.", nil)
	rep := waitForScheduledReport(t, tr.rig, "compliance-audit", taskID, "the posted record", func(r *ScheduledReport) bool { return r.Status == scheduledPosted })
	if rep.Attempts != 2 || rep.LastError != "" {
		t.Fatalf("record = %+v, want posted on the second attempt", rep)
	}
	waitForRunEnded(t, tr)
	if posts := home.calls(); len(posts) != 2 {
		t.Fatalf("%d post attempts, want 2", len(posts))
	}
}

// TestARedeliveredTerminalOfAPostedRunPostsNothing: a terminal delivered
// again after its run was posted (an ack that never landed) reads the
// record and posts nothing.
func TestARedeliveredTerminalOfAPostedRunPostsNothing(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	home := &fakeHomeReporter{home: "C0HOME"}
	r.g.setHomeReporters(home)
	rec := &SessionRecord{Key: scheduleConversation,
		Delivery: &lib.ScheduleDelivery{Mode: lib.ScheduleDeliveryChat, JobKey: "compliance-audit"}}
	ref := TaskRef{ID: "task-0123456789abcdef", Addressee: "fleet-audit",
		Requester: &TaskRequester{Backend: triggerRequesterBackend, Subject: "schedule/compliance-audit"}}
	s := lib.StatusUpdate{TaskID: ref.ID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}
	ctx := context.Background()
	try := terminalTry{redeliverable: true, delivery: 1}
	if r.g.deliverScheduledReport(ctx, rec, ref, ref.ID, s, "Two findings.", try) {
		t.Fatal("a post that landed asked for redelivery")
	}
	try.delivery = 2
	if r.g.deliverScheduledReport(ctx, rec, ref, ref.ID, s, "Two findings.", try) {
		t.Fatal("the redelivery of a posted run asked for another")
	}
	if posts := home.calls(); len(posts) != 1 {
		t.Fatalf("%d posts, want 1", len(posts))
	}
	if rep := readScheduledReport(t, r, "compliance-audit", ref.ID); rep == nil || rep.Status != scheduledPosted || rep.Attempts != 1 {
		t.Fatalf("record = %+v", rep)
	}
}

// TestAScheduledRecordThatCannotBeWrittenPostsNothing: with no bucket, the
// record can't go first, so nothing is posted; the terminal goes back while
// it has deliveries left and is given up on past them, and a run with no
// delivery is never handed back for it.
func TestAScheduledRecordThatCannotBeWrittenPostsNothing(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	js := r.client.JetStream()
	if err := js.DeleteKeyValue(context.Background(), lib.ScheduledReportsBucket); err != nil {
		t.Fatal(err)
	}
	home := &fakeHomeReporter{home: "C0HOME"}
	r.g.setHomeReporters(home)
	rec := &SessionRecord{Key: scheduleConversation,
		Delivery: &lib.ScheduleDelivery{Mode: lib.ScheduleDeliveryChat, JobKey: "compliance-audit"}}
	ref := TaskRef{ID: "task-0123456789abcdef", Addressee: "fleet-audit"}
	s := lib.StatusUpdate{TaskID: ref.ID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}
	ctx := context.Background()
	if !r.g.deliverScheduledReport(ctx, rec, ref, ref.ID, s, "Two findings.", terminalTry{redeliverable: true, delivery: 1}) {
		t.Fatal("an unwritten record did not hand the terminal back")
	}
	if r.g.deliverScheduledReport(ctx, rec, ref, ref.ID, s, "Two findings.", terminalTry{redeliverable: true, delivery: scheduledRecordAttempts}) {
		t.Fatal("the last delivery was handed back")
	}
	rec.Delivery = nil
	if r.g.deliverScheduledReport(ctx, rec, ref, ref.ID, s, "Two findings.", terminalTry{redeliverable: true, delivery: 1}) {
		t.Fatal("a run with no delivery was handed back")
	}
	if posts := home.calls(); len(posts) != 0 {
		t.Fatalf("posted %q with no record", posts)
	}
}

// TestScheduledReportStatusReadsTheRunsRecord: the status a trigger status
// reply carries is the record's, and "" before there is one.
func TestScheduledReportStatusReadsTheRunsRecord(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	got, err := r.g.scheduledReportStatus(ctx, "compliance-audit", "task-0123456789abcdef")
	if err != nil || got != "" {
		t.Fatalf("status with no record = %q, %v; want empty", got, err)
	}
	kv, err := r.client.KV(ctx, lib.ScheduledReportsBucket)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := putScheduledReport(ctx, kv, scheduledReportKey("compliance-audit", "task-0123456789abcdef"),
		ScheduledReport{JobKey: "compliance-audit", TaskID: "task-0123456789abcdef", Status: scheduledStoredNoHome}, 0); err != nil {
		t.Fatal(err)
	}
	if got, err = r.g.scheduledReportStatus(ctx, "compliance-audit", "task-0123456789abcdef"); err != nil || got != scheduledStoredNoHome {
		t.Fatalf("status = %q, %v; want %s", got, err, scheduledStoredNoHome)
	}
}

// TestScheduledReportHelpers: the report source's clean test and the
// derived job key.
func TestScheduledReportHelpers(t *testing.T) {
	t.Parallel()
	if _, silent := scheduledReportFrom(lib.StateCompleted, "  [SILENT] \n", ""); !silent {
		t.Error("[SILENT] with whitespace is not clean")
	}
	if _, silent := scheduledReportFrom(lib.StateCompleted, "[SILENT] but one finding", ""); silent {
		t.Error("a report that says more than [SILENT] is clean")
	}
	if text, silent := scheduledReportFrom(lib.StateFailed, "[SILENT]", "reason: deadline"); silent || text == "" {
		t.Errorf("a failed run = %q, %v; want its notice, not clean", text, silent)
	}
	if text, _ := scheduledReportFrom(lib.StateCompleted, "", ""); text != completedNonTextResult {
		t.Errorf("an empty result = %q", text)
	}
	if got := scheduledJobKeyFromTrigger("schedule/audit.v2"); got != "audit_v2" {
		t.Errorf("job key = %q, want audit_v2", got)
	}
	if !isScheduleConversation(scheduleConversation) || isScheduleConversation("trigger:webhook/ns/x") || isScheduleConversation("slack:C1/1") {
		t.Error("isScheduleConversation")
	}
}

// TestPostHomeReportGoesThroughTheNotifyWorker: the route's home post, as a
// chat.notify home request posts, answered with where it landed; a route not
// started refuses at once.
func TestPostHomeReportGoesThroughTheNotifyWorker(t *testing.T) {
	t.Parallel()
	p := &fakeNotifyPoster{landsIn: testHome + "/threads/t1"}
	n := newTestNotifier(t, p)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := n.postHomeReport(ctx, testHome, "", "Two findings."); err == nil {
		t.Fatal("a route not started took the post")
	}
	if n.homeChannel() != testHome {
		t.Fatalf("home = %q", n.homeChannel())
	}
	n.jobs, n.done = make(chan notifyJob, notifyQueueDepth), make(chan struct{})
	go n.work()
	t.Cleanup(func() { close(n.jobs) })
	post, err := n.postHomeReport(ctx, testHome, "", "Two findings.")
	if err != nil {
		t.Fatal(err)
	}
	if post.Platform != lib.NotifyPlatformGchat || post.ChatID != testHome || post.ThreadID != testHome+"/threads/t1" || post.MessageID == "" {
		t.Fatalf("post = %+v", post)
	}
	if got := p.all(); len(got) != 1 || got[0].space != testHome || got[0].text != "Two findings." {
		t.Fatalf("posts = %+v", got)
	}
	p.failAt = 2
	if _, err := n.postHomeReport(ctx, testHome, "", "Again."); err == nil {
		t.Fatal("a failed post answered as posted")
	}
	none, err := NewGchatNotifier(p, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if none.homeChannel() != "" {
		t.Fatal("a route with no home names one")
	}
}

// countingBlockingPoster holds every post until release closes, and counts
// the posts that landed.
type countingBlockingPoster struct {
	release chan struct{}
	mu      sync.Mutex
	landed  int
}

func (b *countingBlockingPoster) PostNotify(space, thread, text string) (string, string, error) {
	<-b.release
	b.mu.Lock()
	defer b.mu.Unlock()
	b.landed++
	return space + "/messages/1", space + "/threads/1", nil
}

func (b *countingBlockingPoster) count() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.landed
}

// scheduledChatRun is a chat-delivery run's record and entry, for the cases
// that call deliverScheduledReport directly.
func scheduledChatRun(taskID string) (*SessionRecord, TaskRef, lib.StatusUpdate) {
	d := &lib.ScheduleDelivery{Mode: lib.ScheduleDeliveryChat, JobKey: "compliance-audit"}
	rec := &SessionRecord{Key: scheduleConversation, Delivery: d}
	ref := TaskRef{ID: taskID, Addressee: "fleet-audit", Delivery: d,
		Requester: &TaskRequester{Backend: triggerRequesterBackend, Subject: "schedule/compliance-audit"}}
	return rec, ref, lib.StatusUpdate{TaskID: taskID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}
}

// TestAScheduledPostThatOutlastsItsTimeIsUnknownAndNotRetried: a post the
// notify route does not answer in time (here, a backend that hangs) is
// recorded post_unknown and not handed back, so no second post is queued;
// a post still waiting in the route's queue past its time is dropped
// unposted, and only the one already posting lands.
func TestAScheduledPostThatOutlastsItsTimeIsUnknownAndNotRetried(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	poster := &countingBlockingPoster{release: make(chan struct{})}
	n, err := NewGchatNotifier(poster, testHome, nil)
	if err != nil {
		t.Fatal(err)
	}
	n.jobs, n.done = make(chan notifyJob, notifyQueueDepth), make(chan struct{})
	go n.work()
	t.Cleanup(func() { close(n.jobs) })
	r.g.setHomeReporters(n)

	// The batch's budget leaves the post half a second past the outcome's
	// reserve.
	ctx, cancel := context.WithTimeout(context.Background(), scheduledKVReserve+500*time.Millisecond)
	defer cancel()
	try := terminalTry{redeliverable: true, delivery: 1}
	for _, taskID := range []string{"task-00000000000000a1", "task-00000000000000a2"} {
		rec, ref, s := scheduledChatRun(taskID)
		start := time.Now()
		if r.g.deliverScheduledReport(ctx, rec, ref, taskID, s, "Two findings.", try) {
			t.Fatalf("%s: a post whose outcome is unknown was handed back", taskID)
		}
		if waited := time.Since(start); waited > 5*time.Second {
			t.Fatalf("%s: the post waited %s, past its own timeout", taskID, waited)
		}
		rep := readScheduledReport(t, r, "compliance-audit", taskID)
		if rep == nil || rep.Status != scheduledPostUnknown || rep.Attempts != 1 || !rep.PostStartedAt.IsZero() {
			t.Fatalf("%s: record = %+v, want post_unknown after one attempt", taskID, rep)
		}
		ctx, cancel = context.WithTimeout(context.Background(), scheduledKVReserve+500*time.Millisecond)
		defer cancel()
	}
	// A redelivery of the first posts nothing more.
	rec, ref, s := scheduledChatRun("task-00000000000000a1")
	if r.g.deliverScheduledReport(context.Background(), rec, ref, ref.ID, s, "Two findings.", terminalTry{redeliverable: true, delivery: 2}) {
		t.Fatal("the redelivery of an unknown post was handed back")
	}
	close(poster.release)
	time.Sleep(300 * time.Millisecond)
	if got := poster.count(); got != 1 {
		t.Fatalf("%d posts landed, want only the one already posting", got)
	}
}

// TestAScheduledPostLeftStartedIsUnknownOnRedelivery: a record still saying
// a post started, older than a post's timeout (the outcome's write failed,
// or the gateway stopped mid-post), is post_unknown on redelivery and is not
// posted again.
func TestAScheduledPostLeftStartedIsUnknownOnRedelivery(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	home := &fakeHomeReporter{home: "C0HOME"}
	r.g.setHomeReporters(home)
	rec, ref, s := scheduledChatRun("task-00000000000000b1")
	kv, err := r.client.KV(context.Background(), lib.ScheduledReportsBucket)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := putScheduledReport(context.Background(), kv, scheduledReportKey("compliance-audit", ref.ID), ScheduledReport{
		JobKey: "compliance-audit", TaskID: ref.ID, Status: scheduledPosting, Text: "Two findings.",
		PostStartedAt: time.Now().Add(-2 * scheduledPostTimeout),
	}, 0); err != nil {
		t.Fatal(err)
	}
	if r.g.deliverScheduledReport(context.Background(), rec, ref, ref.ID, s, "Two findings.", terminalTry{redeliverable: true, delivery: 2}) {
		t.Fatal("handed back")
	}
	if rep := readScheduledReport(t, r, "compliance-audit", ref.ID); rep == nil || rep.Status != scheduledPostUnknown {
		t.Fatalf("record = %+v, want post_unknown", rep)
	}
	if posts := home.calls(); len(posts) != 0 {
		t.Fatalf("posted %q again", posts)
	}
}

// TestAFailedScheduledPostWithNoRedeliveryLeftIsFinal: a post that fails on
// a try that can't go back (the heal's, or an item requeued in memory), or
// on the durable's fifth delivery whatever the record counted, is the last.
func TestAFailedScheduledPostWithNoRedeliveryLeftIsFinal(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	r.g.setHomeReporters(&fakeHomeReporter{home: "C0HOME", fail: func(int) bool { return true }})
	for taskID, try := range map[string]terminalTry{
		"task-00000000000000c1": {},
		"task-00000000000000c2": {redeliverable: true, delivery: scheduledPostAttempts},
	} {
		rec, ref, s := scheduledChatRun(taskID)
		if r.g.deliverScheduledReport(context.Background(), rec, ref, taskID, s, "Two findings.", try) {
			t.Fatalf("%s (%+v): handed back", taskID, try)
		}
		if rep := readScheduledReport(t, r, "compliance-audit", taskID); rep == nil || rep.Status != scheduledPostFailed || rep.Attempts != 1 {
			t.Fatalf("%s: record = %+v, want post_failed after one attempt", taskID, rep)
		}
	}
}

// TestALongScheduledReportIsCutAndStoredUnescaped: a report past
// scheduledTextMax is cut with a note saying so, and the record keeps '<',
// '>' and '&' as they are rather than HTML-escaped.
func TestALongScheduledReportIsCutAndStoredUnescaped(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	home := &fakeHomeReporter{home: ""}
	r.g.setHomeReporters(home)
	rec, ref, s := scheduledChatRun("task-00000000000000d1")
	long := strings.Repeat("<a&b> é", 20000)
	if r.g.deliverScheduledReport(context.Background(), rec, ref, ref.ID, s, long, terminalTry{redeliverable: true, delivery: 1}) {
		t.Fatal("handed back")
	}
	kv, err := r.client.KV(context.Background(), lib.ScheduledReportsBucket)
	if err != nil {
		t.Fatal(err)
	}
	e, err := kv.Get(context.Background(), scheduledReportKey("compliance-audit", ref.ID))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(e.Value()), `\u003c`) || !strings.Contains(string(e.Value()), "<a&b>") {
		t.Fatal("the stored report is HTML-escaped")
	}
	rep := readScheduledReport(t, r, "compliance-audit", ref.ID)
	if !utf8.ValidString(rep.Text) || len(rep.Text) > scheduledTextMax+len(scheduledTruncatedNote)+16 ||
		!strings.Contains(rep.Text, "[report truncated:") || rep.Status != scheduledStoredNoHome {
		t.Fatalf("record text is %d bytes (valid %v), status %s", len(rep.Text), utf8.ValidString(rep.Text), rep.Status)
	}
	if got := truncateReport("short"); got != "short" {
		t.Fatalf("a short report was changed: %q", got)
	}
}

// TestAScheduledRunUsesItsOwnDelivery: the delivery a run was submitted with
// routes its report, though a later submission has since rewritten the
// conversation's.
func TestAScheduledRunUsesItsOwnDelivery(t *testing.T) {
	t.Parallel()
	tr := startTriggerRig(t, nil)
	home := &fakeHomeReporter{home: "C0HOME"}
	tr.g.setHomeReporters(home)
	taskID := runScheduled(t, tr, goodScheduleSubmit(), "Two findings.", func(rec *SessionRecord) {
		rec.Delivery = &lib.ScheduleDelivery{Mode: lib.ScheduleDeliveryLocal, JobKey: "compliance-audit"}
	})
	rep := waitForScheduledReport(t, tr.rig, "compliance-audit", taskID, "the run's record", func(r *ScheduledReport) bool { return r.Status == scheduledPosted })
	if rep.ChatID != "C0HOME" {
		t.Fatalf("record = %+v", rep)
	}
	if posts := home.calls(); len(posts) != 1 {
		t.Fatalf("%d posts, want 1", len(posts))
	}
}

// The status reader resolves the job key the writer recorded under, for each
// of the run's own delivery, the conversation's, and none.
func TestScheduledRunJobKeyMatchesTheWriter(t *testing.T) {
	own := &lib.ScheduleDelivery{Mode: lib.ScheduleDeliveryChat, JobKey: "own-job"}
	conv := &lib.ScheduleDelivery{Mode: lib.ScheduleDeliveryChat, JobKey: "conv-job"}
	cases := []struct {
		name string
		rec  *SessionRecord
		want string
	}{
		{"the run's own delivery wins", &SessionRecord{Key: "trigger:schedule/ns/x", Delivery: conv,
			Tasks: []TaskRef{{ID: "t1", Delivery: own}}}, "own-job"},
		{"the conversation's for an older entry", &SessionRecord{Key: "trigger:schedule/ns/x", Delivery: conv,
			Tasks: []TaskRef{{ID: "t1"}}}, "conv-job"},
		{"no delivery: the conversation key", &SessionRecord{Key: "trigger:schedule/ns/x",
			Tasks: []TaskRef{{ID: "t1"}}}, scheduledJobKeyFromTrigger("schedule/ns/x")},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := scheduledRunJobKey(c.rec, "t1"); got != c.want {
				t.Errorf("scheduledRunJobKey = %q, want %q", got, c.want)
			}
		})
	}
}
