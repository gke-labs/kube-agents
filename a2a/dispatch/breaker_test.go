package dispatch

import (
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A breaker opens at its threshold of expiries in a row, refuses until its
// cooldown ends, then admits half open: the next expiry opens it at once,
// and a finished task closes it. Each (profile, tier) has its own.
func TestABreakerOpensRefusesAndCloses(t *testing.T) {
	b := newBreakers()
	cfg := lib.DispatchConfig{BreakerThreshold: 2, BreakerCooldownSeconds: 60}
	t0 := time.Unix(1_000_000, 0)

	b.expired(cfg, "tse", "read", t0)
	if r := b.admit1("tse", "read", t0); r != nil {
		t.Fatalf("open after one expiry of two: %+v", r)
	}
	b.expired(cfg, "tse", "read", t0)
	r := b.admit1("tse", "read", t0.Add(time.Second))
	if r == nil || r.reason != ReasonBreakerOpen {
		t.Fatalf("closed after two expiries: %+v", r)
	}
	if r := b.admit1("tse", "unattended", t0.Add(time.Second)); r != nil {
		t.Errorf("another tier's breaker opened too: %+v", r)
	}
	if got := b.open(t0.Add(time.Second)); len(got) != 1 || got[0] != "tse/read" {
		t.Errorf("open = %v", got)
	}

	// Past the cooldown: half open, admitting, and one expiry opens it.
	half := t0.Add(61 * time.Second)
	if r := b.admit1("tse", "read", half); r != nil {
		t.Fatalf("still refusing after the cooldown: %+v", r)
	}
	b.expired(cfg, "tse", "read", half)
	if r := b.admit1("tse", "read", half.Add(time.Second)); r == nil {
		t.Fatal("a half-open breaker stayed closed after an expiry")
	}

	// A finished task closes it outright: the threshold applies again.
	b.finished("tse", "read")
	later := half.Add(2 * time.Second)
	if r := b.admit1("tse", "read", later); r != nil {
		t.Fatalf("open after a finished task: %+v", r)
	}
	b.expired(cfg, "tse", "read", later)
	if r := b.admit1("tse", "read", later); r != nil {
		t.Errorf("one expiry after closing opened it: %+v", r)
	}
}

// A config that leaves the knobs at 0 takes the defaults.
func TestBreakerDefaults(t *testing.T) {
	if breakerThreshold(lib.DispatchConfig{}) != defaultBreakerThreshold || breakerCooldown(lib.DispatchConfig{}) != defaultBreakerCooldown {
		t.Error("zero knobs did not take the defaults")
	}
}

// End to end: two tasks whose workers stop renewing open the breaker at a
// threshold of 2, and a third submission is rejected breaker-open, with the
// refusal tombstoned and the breaker shown in health.
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
	ts := b.waitTerminal("task-3", 10*time.Second)
	if ts.state != lib.StateRejected || !hasReason(ts, ReasonBreakerOpen) {
		t.Errorf("task-3 terminal %+v, want rejected breaker-open", ts)
	}
	if got := d.Health().OpenBreakers; len(got) != 1 || got[0] != "tse/read" {
		t.Errorf("health open breakers = %v", got)
	}
	waitFor(t, 5*time.Second, "task-3's refusal tombstone", func() bool {
		_, ok := b.tombstones()[lib.ProfileLeaseKey("tse", "read", lib.ProfileLeaseRefusedPod, "task-3")]
		return ok
	})
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

// admit1 is admit with nothing in flight, for the state tests.
func (b *breakers) admit1(profile, tier string, now time.Time) *refusal {
	r, _ := b.admit(profile, tier, now, false)
	return r
}

// Half open, the breaker lets one task through at a time: with a task of
// that profile and tier in flight, a submission waits rather than being
// refused; with none, it is admitted.
func TestAHalfOpenBreakerTriesOneTaskAtATime(t *testing.T) {
	b := newBreakers()
	cfg := lib.DispatchConfig{BreakerThreshold: 1, BreakerCooldownSeconds: 60}
	t0 := time.Unix(1_000_000, 0)
	b.expired(cfg, "tse", "read", t0)
	half := t0.Add(61 * time.Second)
	if r, wait := b.admit("tse", "read", half, true); r != nil || !wait {
		t.Errorf("half open with a task in flight: refusal %+v, wait %v; want wait", r, wait)
	}
	if r, wait := b.admit("tse", "read", half, false); r != nil || wait {
		t.Errorf("half open with nothing in flight: refusal %+v, wait %v; want admitted", r, wait)
	}
	if r, wait := b.admit("tse", "read", t0.Add(time.Second), false); r == nil || wait {
		t.Errorf("open: refusal %+v, wait %v; want refused", r, wait)
	}
}

// End to end: after the cooldown a half-open breaker runs one task while the
// next waits queued, never rejected, and the second runs once the first has
// finished.
func TestAHalfOpenBreakerQueuesTheNextTask(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1", "tse-2")
	b.ensure("tse", 2, 0, "read")
	dead := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	dead.noRenew = true
	w1 := b.worker("tse", "read", "tse-1", answerAccept, 3*time.Second)
	w2 := b.worker("tse", "read", "tse-2", answerAccept, 3*time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.BreakerThreshold, c.BreakerCooldownSeconds = 1, 2 })
	d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 20*time.Second); !hasReason(ts, ReasonLeaseExpired) {
		t.Fatalf("task-1 terminal %+v, want lease-expired", ts)
	}
	ti.pool.Set("tse", "read", "tse-1", "tse-2")
	time.Sleep(2500 * time.Millisecond) // past the cooldown: half open

	b.submit("tse", "read", lib.SubmitKindTop, "task-2")
	b.submit("tse", "read", lib.SubmitKindTop, "task-3")
	waitFor(t, 10*time.Second, "the probe task started", func() bool { return w1.startedCount()+w2.startedCount() == 1 })
	time.Sleep(time.Second)
	if n := w1.startedCount() + w2.startedCount(); n != 1 {
		t.Fatalf("%d tasks started while half open; want one at a time", n)
	}
	// The held-back one is queued and shown waiting, as over a cap.
	if n := d.Health().Waiting[lib.ProfileDurableName("tse", "read", lib.SubmitKindTop)]; n != 1 {
		t.Errorf("%d submissions shown waiting while the probe runs; want 1", n)
	}
	waitFor(t, 20*time.Second, "both tasks finished", func() bool {
		return len(b.terminalsOf("task-2")) > 0 && len(b.terminalsOf("task-3")) > 0
	})
	for _, id := range []string{"task-2", "task-3"} {
		if ts := b.terminalsOf(id)[0]; hasReason(ts, ReasonBreakerOpen) || ts.state == lib.StateRejected {
			t.Errorf("%s was refused while half open: %+v", id, ts)
		}
	}
}
