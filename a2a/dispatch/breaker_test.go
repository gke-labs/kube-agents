package dispatch

import (
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A breaker opens at its threshold of expiries in a row and holds until its
// cooldown ends, then is half open: the next expiry opens it at once, and a
// finished task closes it. Each (profile, tier) has its own.
func TestABreakerOpensHoldsAndCloses(t *testing.T) {
	b := newBreakers()
	cfg := lib.DispatchConfig{BreakerThreshold: 2, BreakerCooldownSeconds: 60}
	t0 := time.Unix(1_000_000, 0)
	held := func(at time.Time) bool { w, _ := b.wait("tse", "read", at, false, false); return w }

	b.expired(cfg, "tse", "read", t0)
	if held(t0) {
		t.Fatal("held after one expiry of two")
	}
	b.expired(cfg, "tse", "read", t0)
	if w, open := b.wait("tse", "read", t0.Add(time.Second), false, false); !w || !open {
		t.Fatalf("after two expiries: wait %v, open %v; want both", w, open)
	}
	if w, _ := b.wait("tse", "unattended", t0.Add(time.Second), false, false); w {
		t.Error("another tier's breaker opened too")
	}
	if got := b.open(t0.Add(time.Second)); len(got) != 1 || got[0] != "tse/read" {
		t.Errorf("open = %v", got)
	}

	half := t0.Add(61 * time.Second)
	if held(half) {
		t.Fatal("still holding after the cooldown with nothing in flight")
	}
	b.expired(cfg, "tse", "read", half)
	if !held(half.Add(time.Second)) {
		t.Fatal("a half-open breaker didn't open again on an expiry")
	}

	b.finished("tse", "read")
	later := half.Add(2 * time.Second)
	if held(later) {
		t.Fatal("held after a finished task")
	}
	b.expired(cfg, "tse", "read", later)
	if held(later) {
		t.Error("one expiry after closing opened it")
	}
}

// Half open, the breaker tries one task at a time, the oldest held: with a
// task in flight, or an older submission waiting, a submission waits; with
// neither, it is admitted. None of these is "open".
func TestAHalfOpenBreakerTriesTheOldestOneAtATime(t *testing.T) {
	b := newBreakers()
	cfg := lib.DispatchConfig{BreakerThreshold: 1, BreakerCooldownSeconds: 60}
	t0 := time.Unix(1_000_000, 0)
	b.expired(cfg, "tse", "read", t0)
	half := t0.Add(61 * time.Second)
	for _, c := range []struct {
		busy, older, want bool
	}{{true, false, true}, {false, true, true}, {false, false, false}} {
		if w, open := b.wait("tse", "read", half, c.busy, c.older); w != c.want || open {
			t.Errorf("busy %v older %v: wait %v open %v; want wait %v, not open", c.busy, c.older, w, open, c.want)
		}
	}
}

// A config that leaves the knobs at 0 takes the defaults.
func TestBreakerDefaults(t *testing.T) {
	if breakerThreshold(lib.DispatchConfig{}) != defaultBreakerThreshold || breakerCooldown(lib.DispatchConfig{}) != defaultBreakerCooldown {
		t.Error("zero knobs did not take the defaults")
	}
}

// End to end: two tasks whose workers stop renewing open the breaker at a
// threshold of 2. A third submission is held queued, not refused: no
// terminal, shown waiting, and the breaker in health.
func TestLeaseExpiriesOpenTheBreaker(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1")
	b.ensure("tse", 2, 0, "read")
	for _, pod := range []string{"tse-0", "tse-1"} {
		w := b.worker("tse", "read", pod, answerAccept, time.Hour)
		w.noRenew = true
	}
	ti := oneProfile("tse", 2, "tse-0", "tse-1")
	ti.update(func(c *lib.DispatchConfig) { c.BreakerThreshold, c.BreakerCooldownSeconds = 2, 600 })
	d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	b.submit("tse", "read", lib.SubmitKindTop, "task-2")
	for _, id := range []string{"task-1", "task-2"} {
		if ts := b.waitTerminal(id, 20*time.Second); !hasReason(ts, ReasonLeaseExpired) {
			t.Fatalf("%s terminal %+v, want lease-expired", id, ts)
		}
	}
	b.submit("tse", "read", lib.SubmitKindTop, "task-3")
	waitFor(t, 10*time.Second, "task-3 shown waiting", func() bool {
		return d.Health().Waiting[lib.ProfileDurableName("tse", "read", lib.SubmitKindTop)] == 1
	})
	time.Sleep(2 * time.Second) // several retries
	if ts := b.terminalsOf("task-3"); len(ts) != 0 {
		t.Errorf("task-3 terminals %+v while the breaker is open; want it held", ts)
	}
	if got := d.Health().OpenBreakers; len(got) != 1 || got[0] != "tse/read" {
		t.Errorf("health open breakers = %v", got)
	}
}

// A task that runs to its end clears its (profile, tier)'s run of expiries.
func TestAFinishedTaskClearsTheBreaker(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 1, 0, "read")
	b.worker("tse", "read", "tse-0", answerAccept, 300*time.Millisecond)
	d, _ := b.runDispatcher(testConfig(), oneProfile("tse", 1, "tse-0"), &fakeVerifier{})
	d.breakers.expired(lib.DispatchConfig{}, "tse", "read", time.Now())

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 15*time.Second, "the expiry count cleared by the finished task", func() bool {
		d.breakers.mu.Lock()
		defer d.breakers.mu.Unlock()
		return len(d.breakers.m) == 0
	})
}

// End to end: tasks submitted while the breaker is open wait; after the
// cooldown the oldest held one is tried alone, the next waits queued (never
// refused) and runs once the first has finished. The task whose expiry
// opened the breaker, submitted again under its id, is never run again.
func TestAHalfOpenBreakerTriesTheOldestHeldTask(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1", "tse-2")
	b.ensure("tse", 2, 0, "read")
	dead := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	dead.noRenew = true
	w1 := b.worker("tse", "read", "tse-1", answerAccept, 3*time.Second)
	w2 := b.worker("tse", "read", "tse-2", answerAccept, 3*time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.BreakerThreshold, c.BreakerCooldownSeconds = 1, 4 })
	d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 20*time.Second); !hasReason(ts, ReasonLeaseExpired) {
		t.Fatalf("task-1 terminal %+v, want lease-expired", ts)
	}
	ti.pool.Set("tse", "read", "tse-1", "tse-2")
	b.submit("tse", "read", lib.SubmitKindTop, "task-2")
	time.Sleep(500 * time.Millisecond)
	b.submit("tse", "read", lib.SubmitKindTop, "task-3")
	b.submit("tse", "read", lib.SubmitKindTop, "task-1") // the poison input, again

	waitFor(t, 15*time.Second, "the probe task started", func() bool { return w1.startedCount()+w2.startedCount() == 1 })
	started := func(w *fakeWorker, id string) bool { return w.startedTask(id) }
	if !started(w1, "task-2") && !started(w2, "task-2") {
		t.Errorf("the probe was not the oldest held task, task-2")
	}
	time.Sleep(time.Second)
	if n := w1.startedCount() + w2.startedCount(); n != 1 {
		t.Fatalf("%d tasks started while half open; want one at a time", n)
	}
	if n := d.Health().Waiting[lib.ProfileDurableName("tse", "read", lib.SubmitKindTop)]; n != 1 {
		t.Errorf("%d submissions shown waiting while the probe runs; want task-3", n)
	}
	waitFor(t, 20*time.Second, "both tasks finished", func() bool {
		return len(b.terminalsOf("task-2")) > 0 && len(b.terminalsOf("task-3")) > 0
	})
	for _, id := range []string{"task-2", "task-3"} {
		if ts := b.terminalsOf(id)[0]; ts.state == lib.StateRejected || ts.state == lib.StateFailed {
			t.Errorf("%s ended %+v; want it run", id, ts)
		}
	}
	if started(w1, "task-1") || started(w2, "task-1") {
		t.Error("the task whose expiry opened the breaker ran again")
	}
}

// startedTask reports whether the worker started taskID.
func (w *fakeWorker) startedTask(taskID string) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, id := range w.started {
		if id == taskID {
			return true
		}
	}
	return false
}

// A submission an open breaker holds still ends at its profile's queue
// timeout, rejected queue-timeout, rather than waiting for ever.
func TestAHeldSubmissionEndsAtItsQueueTimeout(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 1, 0, "read")
	b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	ti := oneProfile("tse", 1, "tse-0")
	ti.update(func(c *lib.DispatchConfig) {
		c.BreakerThreshold, c.BreakerCooldownSeconds = 1, 600
		c.Profiles[0].QueueTimeoutSeconds = 2
	})
	d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	d.breakers.expired(ti.src.Config(), "tse", "read", time.Now())

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	ts := b.waitTerminal("task-1", 15*time.Second)
	if ts.state != lib.StateRejected || !hasReason(ts, ReasonQueueTimeout) {
		t.Errorf("terminal %+v, want rejected queue-timeout", ts)
	}
}

// A half-open breaker's probe that ends at its deadline (its worker renewed
// to the end) or is stopped by its requester was no worker loss: the breaker
// closes, as on a finish, rather than serialising the tier until one.
func TestAHalfOpenProbeEndingWithoutAWorkerLossClosesTheBreaker(t *testing.T) {
	for name, end := range map[string]func(b *bus, ti *testInstall){
		"deadline": func(_ *bus, ti *testInstall) {
			ti.update(func(c *lib.DispatchConfig) { c.Profiles[0].ActiveDeadlineSeconds = 2 })
		},
		"stopped": func(b *bus, ti *testInstall) {
			withStopStream(b.t, b)
			ti.update(func(c *lib.DispatchConfig) { c.StopGraceSeconds = 1 })
		},
	} {
		t.Run(name, func(t *testing.T) {
			b := newBus(t, "tse-0")
			b.ensure("tse", 1, 0, "read")
			w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
			ti := oneProfile("tse", 1, "tse-0")
			end(b, ti)
			d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
			d.breakers.expired(lib.DispatchConfig{BreakerThreshold: 1, BreakerCooldownSeconds: 1}, "tse", "read", time.Now())
			time.Sleep(1200 * time.Millisecond) // past the cooldown: half open
			b.submit("tse", "read", lib.SubmitKindTop, "task-1")
			waitFor(t, 10*time.Second, "the probe running", func() bool { return w.startedTask("task-1") })
			if name == "stopped" {
				b.stop("tse", "task-1")
			}
			if ts := b.waitTerminal("task-1", 15*time.Second); ts.state == lib.StateCompleted {
				t.Fatalf("terminal %+v: the probe should have been ended by the dispatcher", ts)
			}
			waitFor(t, 5*time.Second, "the breaker closed", func() bool {
				d.breakers.mu.Lock()
				defer d.breakers.mu.Unlock()
				return len(d.breakers.m) == 0
			})
		})
	}
}
