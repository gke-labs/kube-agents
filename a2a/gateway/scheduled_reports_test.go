package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"

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
	taskID := runScheduled(t, tr, s, "Two findings.", func(rec *SessionRecord) { rec.Delivery = nil })
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
