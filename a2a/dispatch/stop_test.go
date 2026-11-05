package dispatch

import (
	"context"
	"encoding/json"
	"sync/atomic"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats.go"
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
	// A threshold of 1: counting this still-renewing worker as lost would
	// open the breaker.
	ti.update(func(c *lib.DispatchConfig) { c.StopGraceSeconds, c.BreakerThreshold = 1, 1 })
	d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})

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
	if open := d.Health().OpenBreakers; len(open) != 0 {
		t.Errorf("breakers %v open after abandoning a worker that kept renewing; it wasn't lost", open)
	}
}

// A worker that answers the stop's abandon with its own failed terminal (an
// eviction) can't make the stopped task end failed: the canceled terminal
// is written before the abandon is sent.
func TestAStopEndsCanceledWhenTheWorkerEvictsOnTheAbandon(t *testing.T) {
	b := newBus(t, "tse-0")
	withStopStream(t, b)
	b.ensure("tse", 1, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	w.evictOnAbandon = true
	ti := oneProfile("tse", 1, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.StopGraceSeconds = 1 })
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "task-1 running", func() bool { return w.startedTask("task-1") })
	b.stop("tse", "task-1")
	waitFor(t, 10*time.Second, "both terminals", func() bool { return len(b.terminalsOf("task-1")) >= 2 })
	ts := b.terminalsOf("task-1")
	if ts[0].state != lib.StateCanceled || ts[0].class != lib.TaskClassSupervisor {
		t.Errorf("terminals %+v, want the dispatcher's canceled first, before the worker's eviction", ts)
	}
	if ts[1].state != lib.StateFailed {
		t.Errorf("second terminal %+v, want the worker's eviction (the test's own premise)", ts[1])
	}
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
	info, err := cons.Info(b.ctx)
	if err != nil {
		b.t.Fatal(err)
	}
	if info.Delivered.Consumer != 1 {
		b.t.Errorf("delivered %d times: a stopped task is ended where it is found, not put back in the queue", info.Delivered.Consumer)
	}
}

// With no stop stream the dispatcher says so, in Health and once in its log,
// rather than taking every task as not stopped in silence.
func TestAMissingStopStreamIsReported(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 1, 0, "read")
	b.worker("tse", "read", "tse-0", answerAccept, 200*time.Millisecond)
	d, _ := b.runDispatcher(testConfig(), oneProfile("tse", 1, "tse-0"), &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	b.waitTerminal("task-1", 10*time.Second)
	if h := d.Health(); h.StopStream == "" {
		t.Error("Health doesn't say the stop stream can't be read")
	}
	withStopStream(t, b)
	d.mu.Lock()
	d.stopReadRetry = time.Time{} // past the backoff
	d.mu.Unlock()
	b.submit("tse", "read", lib.SubmitKindTop, "task-2")
	b.waitTerminal("task-2", 10*time.Second)
	if h := d.Health(); h.StopStream != "" {
		t.Errorf("Health still reports %q once the stream reads", h.StopStream)
	}
}

// After a failed read of the stop stream the dispatcher doesn't read it again
// for stopReadBackoff: a broken stream or grant costs one read a period, not
// one (with its timeout) per admission. A responder here answers each direct
// get with a server error status, and counts them. (A 503 would not do:
// nats.go turns it into ErrNoResponders, the error path.)
func TestAFailedStopReadBacksOff(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	b.worker("tse", "read", "tse-0", answerAccept, 100*time.Millisecond)
	var reads atomic.Int32
	sub, err := b.admin.Subscribe(directGetPrefix+lib.DispatchStopStream+".>", func(m *nats.Msg) {
		reads.Add(1)
		reply := nats.NewMsg(m.Reply)
		reply.Header.Set("Status", "500")
		reply.Header.Set("Description", "Internal Server Error")
		_ = m.RespondMsg(reply)
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
	if err := b.admin.Flush(); err != nil {
		t.Fatal(err)
	}
	d, _ := b.runDispatcher(testConfig(), oneProfile("tse", 1, "tse-0"), &fakeVerifier{})
	for _, id := range []string{"task-1", "task-2", "task-3"} {
		b.submit("tse", "read", lib.SubmitKindTop, id)
		b.waitTerminal(id, 10*time.Second)
	}
	if n := reads.Load(); n != 1 {
		t.Errorf("%d reads of the stop stream for three admissions inside the backoff, want 1", n)
	}
	if d.Health().StopStream == "" {
		t.Error("Health doesn't report the failed read")
	}
}

// stopDuringVerify is a verifier that publishes the task's stop live while
// it is asked: a stop heard after admission read the stream, before the
// task is reserved.
type stopDuringVerify struct {
	b *bus
}

func (v *stopDuringVerify) CheckProfileRun(_ context.Context, _ capability.Ref, profile string, _ capability.Tier, _ capability.Scope) error {
	body, err := json.Marshal(lib.DispatchStop{V: 1, TaskID: "task-1"})
	if err != nil {
		return err
	}
	if err := v.b.admin.Publish(lib.DispatchStopSubject(profile, "task-1"), body); err != nil {
		return err
	}
	_ = v.b.admin.Flush()
	time.Sleep(300 * time.Millisecond) // the dispatcher hears it
	return nil
}

// A stop heard between admission's read of the stream and the task's
// reservation still counts: the new holding is marked from what was heard,
// so no offer goes out and the task ends canceled.
func TestAStopHeardDuringAdmissionIsNotLost(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 1, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	b.runDispatcher(testConfig(), oneProfile("tse", 1, "tse-0"), &stopDuringVerify{b: b})
	time.Sleep(500 * time.Millisecond) // the stop subscription
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	ts := b.waitTerminal("task-1", 10*time.Second)
	if ts.state != lib.StateCanceled || !hasReason(ts, ReasonStopped) {
		t.Errorf("terminal %+v, want canceled stopped", ts)
	}
	if w.offerCount() != 0 || w.startedCount() != 0 {
		t.Errorf("a task stopped during admission was offered %d times, started %d", w.offerCount(), w.startedCount())
	}
}

// The stop subscriptions are made afresh every stopResubscribe, so one the
// bus refused for want of a grant (which still succeeds client-side) starts
// A dispatcher whose connection can't subscribe to stops (grants are fixed
// per connection, and this one was made without a2a.stop.>) still ends a
// running task that is stopped: recheckStops finds the record on the
// stream.
func TestAStopTheLiveSubscriptionMissesIsFoundOnTheStream(t *testing.T) {
	b := newBusWith(t, []*natsserver.User{{Username: "disp-nostop", Password: dispPass, Permissions: &natsserver.Permissions{
		Subscribe: &natsserver.SubjectPermission{Allow: []string{">"}, Deny: []string{lib.DispatchStopPrefix + ">"}}}}}, "tse-0")
	withStopStream(t, b)
	b.ensure("tse", 1, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	var g grantSwitch
	g.set("disp-nostop")
	ti := oneProfile("tse", 1, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.StopGraceSeconds = 1 })
	d, _ := b.runDispatcherOn(b.connectAs(&g), testConfig(), ti, &fakeVerifier{})
	// A stop remembered past stoppedMemory: the re-check's write prunes it,
	// with no live stop ever arriving to.
	d.mu.Lock()
	d.stopped["tse/task-old"] = d.now().Add(-stoppedMemory - time.Hour)
	d.mu.Unlock()

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "task-1 running", func() bool { return w.startedTask("task-1") })
	b.stop("tse", "task-1")
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCanceled || !hasReason(ts, ReasonStopped) {
		t.Errorf("terminal %+v, want canceled stopped", ts)
	}
	d.mu.Lock()
	_, kept := d.stopped["tse/task-old"]
	d.mu.Unlock()
	if kept {
		t.Error("a stop remembered past stoppedMemory outlived the re-check")
	}
}

// A hand-off that fails (here every lease write is denied, so the task is
// never confirmed) keeps the submission shown waiting, hand-off-failed,
// through every retry: the next offer round doesn't drop the entry while
// it runs, so health and the breaker's olderWaiting keep seeing it.
func TestAFailedHandOffStaysShownWaiting(t *testing.T) {
	noLeases := []*natsserver.User{{Username: "disp-nokv", Password: dispPass, Permissions: &natsserver.Permissions{
		Publish: &natsserver.SubjectPermission{Allow: []string{">"}, Deny: []string{"$KV." + lib.ProfileLeaseBucket + ".>"}}}}}
	b := newBusWith(t, noLeases, "tse-0")
	b.ensure("tse", 2, 0, "read")
	b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	var g grantSwitch
	g.set("disp-nokv")
	d, _ := b.runDispatcherOn(b.connectAs(&g), testConfig(), oneProfile("tse", 2, "tse-0"), &fakeVerifier{})
	durable := lib.ProfileDurableName("tse", "read", lib.SubmitKindTop)

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 20*time.Second, "task-1 shown waiting", func() bool { return d.Health().Waiting[durable] == 1 })
	if n := d.Health().HandOffFailed[durable]; n != 1 {
		t.Errorf("health counts %d hand-off failures, want the one waiting", n)
	}
	// Through the next retries, each of which offers again and blocks on
	// the denied lease write.
	for end := time.Now().Add(2*kvOpTimeout + 2*time.Second); time.Now().Before(end); time.Sleep(100 * time.Millisecond) {
		if n := d.Health().Waiting[durable]; n != 1 {
			t.Fatalf("task-1 shown waiting %d times during a retry's offer round, want 1", n)
		}
	}
}

// A read that finds the connection closed (mid-reconnect or regrant) is the
// connection's fault, not the stream's: no backoff and no stream report.
func TestAClosedConnectionDoesNotBackTheStopStreamOff(t *testing.T) {
	b := newBus(t)
	c := b.connect("dispatcher")
	c.Close()
	d := bareDispatcher()
	d.bus = c
	if d.stopRequested(context.Background(), quietLog(), "tse", "task-1") {
		t.Fatal("a stop read on a closed connection reported a stop")
	}
	if d.stopStreamErr != "" || !d.stopReadRetry.IsZero() {
		t.Errorf("stop stream error %q, retry at %v; want neither for a closed connection", d.stopStreamErr, d.stopReadRetry)
	}
}
