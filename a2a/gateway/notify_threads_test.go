package gateway

import (
	"context"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The made-up alert thread every test here names: a Slack ts in the
// threadedReporter's home channel.
const (
	alertThreadTS   = "1763100000.000100"
	threadDelivJob  = "cluster-triage"
	threadDelivSrc  = "a2a-triage"
	threadedHomeID  = "C0HOME"
	otherChannelID  = "C0OTHER"
	unrecordedTS    = "1763100999.000999"
	threadedPlatfrm = lib.NotifyPlatformSlack
)

func threadDelivery() lib.ScheduleDelivery {
	return lib.ScheduleDelivery{Mode: lib.ScheduleDeliveryThread, JobKey: threadDelivJob,
		Platform: threadedPlatfrm, ChatID: threadedHomeID, ThreadID: alertThreadTS, IncidentSource: threadDelivSrc}
}

func threadSubmit(edit func(*lib.ScheduleDelivery)) lib.TriggerSubmit {
	s := goodScheduleSubmit()
	s.Delivery = threadDelivery()
	if edit != nil {
		edit(&s.Delivery)
	}
	return s
}

// recordAlertThread puts the alert thread on g's record, as a keyed home
// post would.
func recordAlertThread(t *testing.T, g *Gateway, chatID, ts string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := g.notifyThreadStore(ctx).putNotifyThread(NotifyThread{Platform: threadedPlatfrm, ChatID: chatID, ThreadID: ts, PostedAt: time.Now().UTC()}); err != nil {
		t.Fatal(err)
	}
}

func threadKnown(t *testing.T, g *Gateway, chatID, ts string) bool {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	known, err := g.notifyThreadStore(ctx).notifyThreadKnown(threadedPlatfrm, chatID, ts)
	if err != nil {
		t.Fatal(err)
	}
	return known
}

// TestAThreadDeliveryIsRefusedUnlessEveryCheckHolds: each of the ingress
// checks refuses a thread delivery on its own, with nothing submitted.
func TestAThreadDeliveryIsRefusedUnlessEveryCheckHolds(t *testing.T) {
	trusted := startTriggerRig(t, func(c *Config) { c.TriggerThreadSources = []string{"schedule"} })
	trusted.g.setHomeReporters(&threadedReporter{})
	recordAlertThread(t, trusted.g, threadedHomeID, alertThreadTS)
	recordAlertThread(t, trusted.g, otherChannelID, alertThreadTS)

	untrusted := startTriggerRig(t, nil)
	untrusted.g.setHomeReporters(&threadedReporter{})
	recordAlertThread(t, untrusted.g, threadedHomeID, alertThreadTS)

	noHome := startTriggerRig(t, func(c *Config) { c.TriggerThreadSources = []string{"schedule"} })
	recordAlertThread(t, noHome.g, threadedHomeID, alertThreadTS)

	for _, tc := range []struct {
		name string
		tr   *triggerRig
		body lib.TriggerSubmit
	}{
		{"a source the install does not trust for it", untrusted, threadSubmit(nil)},
		{"an install with no home channel", noHome, threadSubmit(nil)},
		{"another platform", trusted, threadSubmit(func(d *lib.ScheduleDelivery) { d.Platform = lib.NotifyPlatformGchat })},
		{"a chat that is not the home channel, though on the record", trusted, threadSubmit(func(d *lib.ScheduleDelivery) { d.ChatID = otherChannelID })},
		{"a thread qualified by another channel", trusted, threadSubmit(func(d *lib.ScheduleDelivery) { d.ThreadID = otherChannelID + "/" + alertThreadTS })},
		{"a thread the gateway never opened", trusted, threadSubmit(func(d *lib.ScheduleDelivery) { d.ThreadID = unrecordedTS })},
		{"a thread named without the thread mode", trusted, func() lib.TriggerSubmit {
			s := threadSubmit(nil)
			s.Delivery.Mode = lib.ScheduleDeliveryChat
			return s
		}()},
		{"an incident source outside the gateway's", trusted, threadSubmit(func(d *lib.ScheduleDelivery) { d.IncidentSource = "kanban" })},
	} {
		t.Run(tc.name, func(t *testing.T) {
			before := len(tc.tr.submissions())
			reply := tc.tr.submit(t, "schedule", tc.body)
			if reply.Refused == nil || reply.Refused.Reason != lib.TriggerRefusedInvalid || reply.TaskID != "" {
				t.Fatalf("reply = %+v, want invalid", reply)
			}
			time.Sleep(100 * time.Millisecond)
			if n := len(tc.tr.submissions()); n != before {
				t.Fatalf("%d submissions published by the refusal", n-before)
			}
		})
	}
}

// TestAThreadDeliveryOnTheRecordIsTaken: from a trusted source, into the
// home channel, naming a thread on the record (bare or channel-qualified),
// the submission starts its task with the delivery recorded.
func TestAThreadDeliveryOnTheRecordIsTaken(t *testing.T) {
	tr := startTriggerRig(t, func(c *Config) { c.TriggerThreadSources = []string{"schedule"} })
	tr.g.setHomeReporters(&threadedReporter{})
	recordAlertThread(t, tr.g, threadedHomeID, alertThreadTS)
	for i, thread := range []string{alertThreadTS, threadedHomeID + "/" + alertThreadTS} {
		s := threadSubmit(func(d *lib.ScheduleDelivery) { d.ThreadID = thread })
		s.IdempotencyKey = "schedule/kubeagents-system/compliance-audit/176310000" + string(rune('0'+i))
		reply := tr.submit(t, "schedule", s)
		if reply.Refused != nil || reply.TaskID == "" {
			t.Fatalf("thread %q: reply = %+v, want a task", thread, reply)
		}
	}
}

// TestAThreadRecordThatCannotBeReadRefusesAsUnavailable: with the record's
// bucket gone, a thread delivery is refused as worth a retry, never taken
// on trust.
func TestAThreadRecordThatCannotBeReadRefusesAsUnavailable(t *testing.T) {
	tr := startTriggerRig(t, func(c *Config) { c.TriggerThreadSources = []string{"schedule"} })
	tr.g.setHomeReporters(&threadedReporter{})
	js, err := jetstream.New(tr.nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := js.DeleteKeyValue(ctx, lib.NotifyThreadsBucket); err != nil {
		t.Fatal(err)
	}
	reply := tr.submit(t, "schedule", threadSubmit(nil))
	if reply.Refused == nil || reply.Refused.Reason != lib.TriggerRefusedUnavailable {
		t.Fatalf("reply = %+v, want unavailable", reply)
	}
}

// TestAKeyedHomePostRecordsItsThread: a keyed home post (an alert's) that
// opens a thread tells the recorder; an unkeyed one, or a keyed reply into
// a thread the request named, does not.
func TestAKeyedHomePostRecordsItsThread(t *testing.T) {
	p := &fakeNotifyPoster{landsIn: testHome + "/threads/T1"}
	n := newTestNotifier(t, p)
	var got []string
	n.setThreadRecorder(func(platform, chatID, thread string) { got = append(got, platform+"|"+chatID+"|"+thread) })
	if r := serveJSON(t, n, lib.NotifyRequest{Text: "Plain notice."}); r.Error != "" {
		t.Fatalf("unkeyed post: %+v", r)
	}
	if len(got) != 0 {
		t.Fatalf("an unkeyed post was recorded: %v", got)
	}
	if r := serveJSON(t, n, lib.NotifyRequest{Text: "Alert.", Update: "alert-s1"}); r.Error != "" {
		t.Fatalf("keyed post: %+v", r)
	}
	if want := lib.NotifyPlatformGchat + "|" + testHome + "|" + testHome + "/threads/T1"; len(got) != 1 || got[0] != want {
		t.Fatalf("recorded = %v, want [%s]", got, want)
	}
	// A keyed reply into a thread the request names opened nothing.
	if r := serveJSON(t, n, lib.NotifyRequest{Text: "Reply.", Thread: testHome + "/threads/T9", Update: "alert-s2"}); r.Error != "" {
		t.Fatalf("keyed reply: %+v", r)
	}
	if len(got) != 1 {
		t.Fatalf("a keyed reply into a named thread was recorded: %v", got)
	}
}

// TestARecordedThreadIsKeptInItsOneForm: the record keeps a Slack thread as
// its bare ts, so either spelling finds it; a thread of another channel is
// not recorded.
func TestARecordedThreadIsKeptInItsOneForm(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	r.g.recordNotifyThread(threadedPlatfrm, threadedHomeID, threadedHomeID+"/"+alertThreadTS)
	waitFor(t, "the thread on the record", func() bool { return threadKnown(t, r.g, threadedHomeID, alertThreadTS) })
	r.g.recordNotifyThread(threadedPlatfrm, threadedHomeID, otherChannelID+"/"+unrecordedTS)
	time.Sleep(200 * time.Millisecond)
	if threadKnown(t, r.g, threadedHomeID, unrecordedTS) || threadKnown(t, r.g, otherChannelID, unrecordedTS) {
		t.Fatal("a thread of another channel was recorded")
	}
}

// threadRun is a finished run whose delivery is d.
func threadRun(taskID string, d lib.ScheduleDelivery) (*SessionRecord, TaskRef, lib.StatusUpdate) {
	rec := &SessionRecord{Key: scheduleConversation, Delivery: &d}
	ref := TaskRef{ID: taskID, Addressee: "cluster", Delivery: &d,
		Requester: &TaskRequester{Backend: triggerRequesterBackend, Subject: "schedule/compliance-audit"}}
	return rec, ref, lib.StatusUpdate{TaskID: taskID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}
}

// TestAThreadDeliveryPostsIntoItsThreadAndKeepsTheIncident: the report
// replies in the named thread, opens no day thread, and is kept there under
// the delivery's incident source.
func TestAThreadDeliveryPostsIntoItsThreadAndKeepsTheIncident(t *testing.T) {
	t.Parallel()
	r, home, clock, incidents := threadRig(t)
	const taskID = "task-00000000000000f1"
	rec, ref, s := threadRun(taskID, threadDelivery())
	if r.g.deliverScheduledReport(context.Background(), rec, ref, taskID, s, "Triage: the pod is out of memory.", terminalTry{redeliverable: true, delivery: 1}) {
		t.Fatal("handed back")
	}
	got := readScheduledReport(t, r, threadDelivJob, taskID)
	if got.Status != scheduledPosted || got.ChatID != threadedHomeID || got.ThreadID != alertThreadTS {
		t.Fatalf("record = %+v, want posted in %s", got, alertThreadTS)
	}
	if posts := home.all(); len(posts) != 1 || posts[0].thread != alertThreadTS {
		t.Fatalf("posts = %+v, want one reply in the alert's thread", posts)
	}
	if kept := incidents.all(); len(kept) != 1 || kept[0] != threadedPlatfrm+"|"+threadedHomeID+"|"+alertThreadTS+"|Triage: the pod is out of memory." {
		t.Fatalf("incidents = %v", kept)
	}
	if incidents.sources[0] != threadDelivSrc {
		t.Fatalf("incident source = %q, want %q", incidents.sources[0], threadDelivSrc)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	day, _, _, err := getScheduleThread(r.g.threadStore(ctx), scheduleThreadKey(threadDelivJob, clock.now()))
	if err != nil || day != nil {
		t.Fatalf("day thread = %+v, %v; want none", day, err)
	}
}

// TestAThreadDeliveryWithoutASourceKeepsTheDefault: a delivery naming no
// incident source keeps the row under the thread default, never the
// scheduled reports'.
func TestAThreadDeliveryWithoutASourceKeepsTheDefault(t *testing.T) {
	t.Parallel()
	r, _, _, incidents := threadRig(t)
	d := threadDelivery()
	d.IncidentSource = ""
	const taskID = "task-00000000000000f2"
	rec, ref, s := threadRun(taskID, d)
	r.g.deliverScheduledReport(context.Background(), rec, ref, taskID, s, "Triage report.", terminalTry{redeliverable: true, delivery: 1})
	if len(incidents.sources) != 1 || incidents.sources[0] != lib.ScheduleDeliveryThreadSource {
		t.Fatalf("sources = %v, want [%s]", incidents.sources, lib.ScheduleDeliveryThreadSource)
	}
}

// TestAThreadDeliveryOutsideTodaysHomeChannelIsNotPosted: a home channel
// changed since the submission refuses the post, which is given up on once
// the terminal can't go back.
func TestAThreadDeliveryOutsideTodaysHomeChannelIsNotPosted(t *testing.T) {
	t.Parallel()
	r, home, _, incidents := threadRig(t)
	d := threadDelivery()
	d.ChatID = otherChannelID
	const taskID = "task-00000000000000f3"
	rec, ref, s := threadRun(taskID, d)
	r.g.deliverScheduledReport(context.Background(), rec, ref, taskID, s, "Triage report.", terminalTry{redeliverable: false, delivery: 1})
	got := readScheduledReport(t, r, threadDelivJob, taskID)
	if got.Status != scheduledPostFailed {
		t.Fatalf("record = %+v, want post_failed", got)
	}
	if posts := home.all(); len(posts) != 0 {
		t.Fatalf("posts = %+v, want none", posts)
	}
	if len(incidents.all()) != 0 {
		t.Fatal("an incident was kept for an unposted report")
	}
}

// TestAThreadDeliveryWhosePostFailsKeepsNoIncident: the reply refused, the
// incident row is not written.
func TestAThreadDeliveryWhosePostFailsKeepsNoIncident(t *testing.T) {
	t.Parallel()
	r, home, _, incidents := threadRig(t)
	home.fail = func(int) bool { return true }
	const taskID = "task-00000000000000f4"
	rec, ref, s := threadRun(taskID, threadDelivery())
	r.g.deliverScheduledReport(context.Background(), rec, ref, taskID, s, "Triage report.", terminalTry{redeliverable: false, delivery: 1})
	if got := readScheduledReport(t, r, threadDelivJob, taskID); got.Status != scheduledPostFailed {
		t.Fatalf("record = %+v, want post_failed", got)
	}
	if posts := home.all(); len(posts) != 1 || posts[0].thread != alertThreadTS {
		t.Fatalf("posts = %+v, want one try in the alert's thread and no new message", posts)
	}
	if len(incidents.all()) != 0 {
		t.Fatal("an incident was kept for a failed post")
	}
}
