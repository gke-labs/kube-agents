package dispatch

import (
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A dispatcher stopped and replaced while a task runs: the new one re-adopts
// it from the lease record, though the record went stale while nobody read
// the renewals, because a renewal arrives inside the restart grace. The task
// finishes normally and was offered once.
func TestARestartReadoptsALiveTask(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 12*time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	_, stop1 := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the offer", func() bool { return w.offerCount() == 1 })
	time.Sleep(1500 * time.Millisecond)
	stop1()
	time.Sleep(4 * time.Second) // longer than a lease: the record is stale
	for _, rec := range b.leases() {
		if !rec.Expired(time.Now(), 0) {
			t.Fatalf("the record is still live after the outage (%v); the test proves nothing", rec.ExpiresAt)
		}
	}
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	ts := b.waitTerminal("task-1", 20*time.Second)
	if ts.state != lib.StateCompleted || ts.from != "tse-0" {
		t.Errorf("terminal %+v, want the worker's completed", ts)
	}
	time.Sleep(time.Second)
	if n := len(b.terminalsOf("task-1")); n != 1 {
		t.Errorf("%d terminals: %+v", n, b.terminalsOf("task-1"))
	}
	if n := w.offerCount(); n != 1 {
		t.Errorf("offered %d times across the restart, want 1", n)
	}
}

// A dispatcher that dies without giving its holds back: the submission
// returns only after the durable's AckWait, later than the restart grace.
// Meanwhile the new dispatcher keeps the record fresh from the renewals it
// hears (under that pod's own key), so the late re-adoption finds the lease
// live.
func TestACrashReadoptsALiveTaskAfterAckWait(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 6*time.Second, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 16*time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	c1 := b.connect("dispatcher-1")
	_, stop1 := b.runDispatcherOn(c1, testConfig(), ti, &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the offer", func() bool { return w.offerCount() == 1 })
	time.Sleep(1500 * time.Millisecond)
	c1.Close() // no Nak reaches the server
	stop1()
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	ts := b.waitTerminal("task-1", 30*time.Second)
	if ts.state != lib.StateCompleted || ts.from != "tse-0" {
		t.Errorf("terminal %+v, want the worker's completed", ts)
	}
	if n := w.offerCount(); n != 1 {
		t.Errorf("offered %d times across the crash, want 1", n)
	}
}

// A started task whose worker died while the dispatcher was down: the new
// dispatcher fails it lease-expired, but only after the restart grace has
// passed with no renewal, and never offers it again.
func TestARestartFailsADeadTaskAfterTheGrace(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	ti := oneProfile("tse", 2, "tse-0")
	_, stop1 := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the offer", func() bool { return w.offerCount() == 1 })
	time.Sleep(1500 * time.Millisecond)
	stop1()
	w.kill()
	time.Sleep(4 * time.Second)
	ti.update(func(c *lib.DispatchConfig) { c.RestartGraceSeconds = 3 })
	grace := 3 * time.Second
	started := time.Now()
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	ts := b.waitTerminal("task-1", 15*time.Second)
	if ts.state != lib.StateFailed || !hasReason(ts, ReasonLeaseExpired) {
		t.Errorf("terminal %+v, want failed lease-expired", ts)
	}
	if waited := time.Since(started); waited < grace {
		t.Errorf("failed %v after the restart, inside the %v grace", waited, grace)
	}
	time.Sleep(2 * time.Second)
	if n := w.offerCount(); n != 1 {
		t.Errorf("offered %d times, want 1", n)
	}
	if l := b.leases(); len(l) != 0 {
		t.Errorf("leases left: %v", l)
	}
}

// A task that never started (no worker was free) is dispatched normally by
// the next dispatcher.
func TestARestartDispatchesAnUnstartedTask(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	ti := oneProfile("tse", 2)
	_, stop1 := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	time.Sleep(1500 * time.Millisecond)
	stop1()
	if l := b.leases(); len(l) != 0 {
		t.Fatalf("an unstarted task has a lease: %v", l)
	}
	w := b.worker("tse", "read", "tse-0", answerAccept, 500*time.Millisecond)
	ti.pool.Set("tse", "read", "tse-0")
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	if ts := b.waitTerminal("task-1", 15*time.Second); ts.state != lib.StateCompleted {
		t.Errorf("terminal %+v, want completed", ts)
	}
	if w.offerCount() != 1 {
		t.Errorf("offered %d times", w.offerCount())
	}
}

// A task that finishes while its submission is between holders (the old
// dispatcher crashed, the redelivery hasn't come): the new dispatcher hears
// the final renewal under that pod's key and settles the task when the
// submission arrives, with no failure terminal after the worker's.
func TestAFinishBetweenHoldersIsSettledNotFailed(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 6*time.Second, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 3*time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	c1 := b.connect("dispatcher-1")
	_, stop1 := b.runDispatcherOn(c1, testConfig(), ti, &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the offer", func() bool { return w.offerCount() == 1 })
	c1.Close()
	stop1()
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	waitFor(t, 20*time.Second, "the submission settled", func() bool {
		ack, num := b.pending(lib.ProfileDurableName("tse", "read", lib.SubmitKindTop))
		return w.completed.Load() == 1 && ack == 0 && num == 0 && len(b.leases()) == 0
	})
	time.Sleep(time.Second)
	if ts := b.terminalsOf("task-1"); len(ts) != 1 || ts[0].state != lib.StateCompleted {
		t.Errorf("terminals %+v, want only the worker's completed", ts)
	}
}
