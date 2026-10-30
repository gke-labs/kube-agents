package dispatch

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// withStopStream provisions DISPATCH_STOPS on the test bus as the operator
// does: one record per subject, direct get on.
func withStopStream(t *testing.T, b *bus) {
	t.Helper()
	if _, err := b.js.CreateOrUpdateStream(b.ctx, jetstream.StreamConfig{Name: lib.DispatchStopStream,
		Subjects: []string{lib.DispatchStopPrefix + ">"}, Retention: jetstream.LimitsPolicy, MaxAge: 24 * time.Hour,
		MaxMsgsPerSubject: 1, AllowDirect: true}); err != nil {
		t.Fatal(err)
	}
}

func (b *bus) stop(profile, taskID string) {
	b.t.Helper()
	if err := b.gateway.PublishDispatchStop(b.ctx, profile, taskID); err != nil {
		b.t.Fatal(err)
	}
}

// A stop record names one task of one profile: the subject's profile and
// task, a version-1 body naming the same task. Anything else stops nothing.
func TestStopRecordsAreRead(t *testing.T) {
	for _, c := range []struct {
		subject, body string
		want          string
	}{
		{"a2a.stop.tse.task-1", `{"v":1,"taskId":"task-1"}`, "task-1"},
		{"a2a.stop.tse.task-1", `{"v":1,"taskId":"task-2"}`, ""},
		{"a2a.stop.tse.task-1", `{"v":2,"taskId":"task-1"}`, ""},
		{"a2a.stop.other.task-1", `{"v":1,"taskId":"task-1"}`, ""},
		{"a2a.stop.tse.task-1.x", `{"v":1,"taskId":"task-1.x"}`, ""},
		{"a2a.stop.tse.task-1", `not json`, ""},
	} {
		got, ok := stopFor("tse", c.subject, []byte(c.body))
		if (c.want != "") != ok || got != c.want {
			t.Errorf("stopFor(%s, %s) = %q %v; want %q", c.subject, c.body, got, ok, c.want)
		}
	}
}

// A task stopped while it waits over a cap ends canceled (reason stopped)
// when it comes back, is tombstoned, and is never offered.
func TestAQueuedTaskStoppedIsCanceled(t *testing.T) {
	b := newBus(t, "tse-0")
	withStopStream(t, b)
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	b.runDispatcher(testConfig(), oneProfile("tse", 1, "tse-0"), &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "task-1 running", func() bool { return w.startedTask("task-1") })
	b.submit("tse", "read", lib.SubmitKindTop, "task-2") // over the profile's cap of 1
	time.Sleep(500 * time.Millisecond)
	b.stop("tse", "task-2")
	ts := b.waitTerminal("task-2", 10*time.Second)
	if ts.state != lib.StateCanceled || !hasReason(ts, ReasonStopped) || ts.class != lib.TaskClassSupervisor {
		t.Errorf("task-2 terminal %+v, want the dispatcher's canceled stopped", ts)
	}
	if w.offerCount() != 1 {
		t.Errorf("%d offers; the stopped task-2 must never be offered", w.offerCount())
	}
	waitFor(t, 5*time.Second, "task-2's tombstone", func() bool {
		_, ok := b.tombstones()[lib.ProfileLeaseKey("tse", "read", lib.ProfileLeaseRefusedPod, "task-2")]
		return ok
	})
}

// A stop published while no dispatcher runs is found on the stream when the
// task's submission is admitted: canceled, never offered.
func TestAStopMissedLiveIsFoundOnTheStream(t *testing.T) {
	b := newBus(t, "tse-0")
	withStopStream(t, b)
	b.ensure("tse", 1, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	b.stop("tse", "task-1")
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	b.runDispatcher(testConfig(), oneProfile("tse", 1, "tse-0"), &fakeVerifier{})

	ts := b.waitTerminal("task-1", 10*time.Second)
	if ts.state != lib.StateCanceled || !hasReason(ts, ReasonStopped) {
		t.Errorf("terminal %+v, want canceled stopped", ts)
	}
	if w.offerCount() != 0 {
		t.Errorf("a task stopped before admission was offered %d times", w.offerCount())
	}
}

// A stop heard live counts even when the stream can't be read: here there
// is no stop stream at all, so the record reaches the dispatcher's
// subscription and nowhere else, and the submission that follows is
// canceled from what it heard.
func TestAStopHeardLiveCountsWithoutTheStream(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 1, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	b.runDispatcher(testConfig(), oneProfile("tse", 1, "tse-0"), &fakeVerifier{})
	time.Sleep(500 * time.Millisecond) // its stop subscription
	body, err := json.Marshal(lib.DispatchStop{V: 1, TaskID: "task-1"})
	if err != nil {
		t.Fatal(err)
	}
	if err := b.admin.Publish(lib.DispatchStopSubject("tse", "task-1"), body); err != nil {
		t.Fatal(err)
	}
	if err := b.admin.Flush(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(200 * time.Millisecond)
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")

	ts := b.waitTerminal("task-1", 10*time.Second)
	if ts.state != lib.StateCanceled || !hasReason(ts, ReasonStopped) {
		t.Errorf("terminal %+v, want canceled stopped from the stop heard live", ts)
	}
	if w.offerCount() != 0 {
		t.Errorf("a task stopped before admission was offered %d times", w.offerCount())
	}
}

// A running task whose worker doesn't stop within stopGraceSeconds is
// abandoned on its worker and ends canceled (reason stopped), tombstoned.
func TestARunningTaskThatDoesNotStopIsAbandoned(t *testing.T) {
	b := newBus(t, "tse-0")
	withStopStream(t, b)
	b.ensure("tse", 1, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	ti := oneProfile("tse", 1, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.StopGraceSeconds = 1 })
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "task-1 running", func() bool { return w.startedTask("task-1") })
	start := time.Now()
	b.stop("tse", "task-1")
	ts := b.waitTerminal("task-1", 10*time.Second)
	if ts.state != lib.StateCanceled || !hasReason(ts, ReasonStopped) || ts.class != lib.TaskClassSupervisor {
		t.Errorf("terminal %+v, want the dispatcher's canceled stopped", ts)
	}
	if took := time.Since(start); took < time.Second {
		t.Errorf("ended %s after the stop, inside the 1s grace", took)
	}
	waitFor(t, 5*time.Second, "the abandon on the worker", func() bool { return w.abandonCount() > 0 })
	waitFor(t, 5*time.Second, "task-1's tombstone", func() bool {
		_, ok := b.tombstones()[lib.ProfileLeaseKey("tse", "read", "tse-0", "task-1")]
		return ok
	})
}

// A stopped unattended task's Job is deleted once the task is canceled.
func TestAStoppedJobIsDeleted(t *testing.T) {
	b := newBus(t, "job-task-1")
	withStopStream(t, b)
	b.ensure("inv", 1, 0, "unattended")
	w := b.worker("inv", "unattended", "job-task-1", answerAccept, time.Hour)
	jobs := newFakeJobs(0)
	b.runDispatcherWith(testConfig(), unattendedInstall(1, func(c *lib.DispatchConfig) { c.StopGraceSeconds = 1 }), &fakeVerifier{}, jobs)

	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "task-1 running on its Job", func() bool { return w.startedTask("task-1") })
	b.stop("inv", "task-1")
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCanceled {
		t.Fatalf("terminal %+v, want canceled", ts)
	}
	waitFor(t, 5*time.Second, "the stopped task's Job deleted", func() bool { return jobs.deleted("task-1") })
}

// A running task whose executor ends it within the grace ends as its
// executor says, with no terminal of the dispatcher's and no abandon.
func TestATaskEndingWithinTheStopGraceKeepsItsOwnEnd(t *testing.T) {
	b := newBus(t, "tse-0")
	withStopStream(t, b)
	b.ensure("tse", 1, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 1500*time.Millisecond)
	ti := oneProfile("tse", 1, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.StopGraceSeconds = 5 })
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "task-1 running", func() bool { return w.startedTask("task-1") })
	b.stop("tse", "task-1")
	ts := b.waitTerminal("task-1", 10*time.Second)
	if ts.class != lib.TaskClassEvents || hasReason(ts, ReasonStopped) {
		t.Errorf("terminal %+v, want the executor's own", ts)
	}
	time.Sleep(time.Second)
	if n := len(b.terminalsOf("task-1")); n != 1 {
		t.Errorf("%d terminals; the dispatcher added one to a task that ended itself", n)
	}
	if w.abandonCount() != 0 {
		t.Error("a task that ended within the grace was abandoned")
	}
}

// A stop that comes while the offer is out (after admission, before the
// confirm) abandons the offer on the pod when it accepts: never confirmed,
// so the worker never starts, and the task ends canceled, tombstoned, not
// offered to anyone else.
func TestAStopDuringTheOfferAbandonsThePod(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1")
	withStopStream(t, b)
	b.ensure("tse", 1, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	w.acceptDelay = 800 * time.Millisecond // inside the 1s accept window
	other := b.worker("tse", "read", "tse-1", answerAccept, time.Hour)
	other.acceptDelay = 800 * time.Millisecond
	b.runDispatcher(testConfig(), oneProfile("tse", 1, "tse-0", "tse-1"), &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the offer out", func() bool { return w.offerCount()+other.offerCount() == 1 })
	offered := w
	if other.offerCount() == 1 {
		offered = other
	}
	b.stop("tse", "task-1")

	ts := b.waitTerminal("task-1", 10*time.Second)
	if ts.state != lib.StateCanceled || !hasReason(ts, ReasonStopped) {
		t.Errorf("terminal %+v, want canceled stopped", ts)
	}
	if w.confirmCount()+other.confirmCount() != 0 || w.startedCount()+other.startedCount() != 0 {
		t.Errorf("a task stopped before its confirm was confirmed (%d) or started (%d)",
			w.confirmCount()+other.confirmCount(), w.startedCount()+other.startedCount())
	}
	if w.offerCount()+other.offerCount() != 1 {
		t.Errorf("%d offers; a stopped task is offered to no one else", w.offerCount()+other.offerCount())
	}
	waitFor(t, 5*time.Second, "the stopped abandon on the offered pod", func() bool {
		offered.mu.Lock()
		defer offered.mu.Unlock()
		for _, a := range offered.abandons {
			if a.Reason == abandonStopped {
				return true
			}
		}
		return false
	})
	waitFor(t, 5*time.Second, "task-1's tombstone", func() bool {
		_, ok := b.tombstones()[lib.ProfileLeaseKey("tse", "read", lib.ProfileLeaseRefusedPod, "task-1")]
		return ok
	})
	b.noRedelivery("tse", "read")
}

// A stop during an offer that then goes unanswered offers the task to no
// other pod: it ends canceled where it was found, never put back in the
// queue.
func TestAStopDuringAnUnansweredOfferOffersNoOneElse(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1")
	withStopStream(t, b)
	b.ensure("tse", 1, 0, "read")
	w0 := b.worker("tse", "read", "tse-0", answerSilent, time.Hour)
	w1 := b.worker("tse", "read", "tse-1", answerSilent, time.Hour)
	b.runDispatcher(testConfig(), oneProfile("tse", 1, "tse-0", "tse-1"), &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the offer out", func() bool { return w0.offerCount()+w1.offerCount() == 1 })
	b.stop("tse", "task-1")
	ts := b.waitTerminal("task-1", 10*time.Second)
	if ts.state != lib.StateCanceled || !hasReason(ts, ReasonStopped) {
		t.Errorf("terminal %+v, want canceled stopped", ts)
	}
	if n := w0.offerCount() + w1.offerCount(); n != 1 {
		t.Errorf("%d offers; a task stopped during its first offer is offered to no one else", n)
	}
	b.noRedelivery("tse", "read")
}

// noRedelivery fails the test unless the one submission on the (profile,
// tier) top durable was delivered once: not given back and redelivered.
// NumRedelivered counts only redeliveries still pending, so this reads the
// consumer's delivered sequence, which counts every delivery.
func (b *bus) noRedelivery(profile, tier string) {
	b.t.Helper()
	cons, err := b.js.Consumer(b.ctx, lib.ProfileSubmitStream, lib.ProfileDurableName(profile, tier, lib.SubmitKindTop))
	if err != nil {
		b.t.Fatal(err)
	}
	time.Sleep(time.Second) // a give-back's redelivery comes RetryDelay later
	if info, err := cons.Info(b.ctx); err != nil || info.Delivered.Consumer != 1 {
		b.t.Errorf("delivered %d times (%v): a stopped task is ended where it is found, not put back in the queue", info.Delivered.Consumer, err)
	}
}

// A stop while the task's Job is still starting ends the wait: the Job is
// deleted, nothing is offered, and the task ends canceled, not no-worker,
// and doesn't count against the breaker.
func TestAStopWhileTheJobStartsDeletesIt(t *testing.T) {
	b := newBus(t, "job-task-1")
	withStopStream(t, b)
	b.ensure("inv", 1, 0, "unattended")
	w := b.worker("inv", "unattended", "job-task-1", answerAccept, time.Hour)
	jobs := newFakeJobs(time.Hour)
	d, _ := b.runDispatcherWith(testConfig(), unattendedInstall(1, func(c *lib.DispatchConfig) { c.BreakerThreshold = 1 }), &fakeVerifier{}, jobs)

	b.submit("inv", "unattended", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the Job launched", func() bool { return jobs.launchCount() == 1 })
	b.stop("inv", "task-1")
	ts := b.waitTerminal("task-1", 10*time.Second)
	if ts.state != lib.StateCanceled || !hasReason(ts, ReasonStopped) {
		t.Errorf("terminal %+v, want canceled stopped", ts)
	}
	waitFor(t, 5*time.Second, "the Job deleted", func() bool { return jobs.deleted("task-1") })
	if w.offerCount() != 0 {
		t.Errorf("%d offers to a stopped task's Job pod", w.offerCount())
	}
	if open := d.Health().OpenBreakers; len(open) != 0 {
		t.Errorf("open breakers %v: a stop is not a worker failure", open)
	}
}
