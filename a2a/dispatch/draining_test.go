package dispatch

import (
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A profile that leaves the install drains (#5074): the config lists it
// with no durables until DrainingUntil. The dispatcher takes such a config,
// a task it holds runs on and settles as any other, and nothing new is
// dispatched for it: a submission that reaches its stream afterwards isn't
// offered within several retry delays. The harness has no auth callout, so
// grants aren't enforced here; that the draining profile keeps its
// .supervisor grant is the operator's identity-map test.
func TestADrainingProfileFinishesWhatItHoldsAndTakesNothingNew(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 3*time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "task-1 started", func() bool { return w.startedCount() == 1 })

	ti.update(func(c *lib.DispatchConfig) {
		p := c.Profiles[0]
		p.Durables, p.DrainingUntil = nil, time.Now().Add(time.Hour)
		c.Profiles[0] = p
	})
	key := lib.ProfileLeaseKey("tse", "read", "tse-0", "task-1")
	waitFor(t, 15*time.Second, "the held task settled", func() bool { return b.tombstones()[key].Finished() })
	if ts := b.terminalsOf("task-1"); len(ts) != 1 || ts[0].state != lib.StateCompleted {
		t.Errorf("task-1's terminals %+v, want its worker's completed alone", ts)
	}

	b.submit("tse", "read", lib.SubmitKindTop, "task-2")
	time.Sleep(3 * time.Second) // several retry delays and refreshes
	if n := w.offerCount(); n != 1 {
		t.Errorf("the draining profile's worker was offered %d tasks, want only task-1", n)
	}
}

// In the window before the dispatcher's next refresh drops a draining
// profile's durable, a delivery for it (a new submission, or one of its
// backlog redelivered) stays queued: the profile is listed so its held
// tasks' terminals land, not to take new work.
func TestADrainingProfilesDurableTakesNothingBeforeItIsDropped(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	cfg := testConfig()
	cfg.RefreshInterval = time.Hour // the durable stays bound for the whole test
	b.runDispatcher(cfg, ti, &fakeVerifier{})

	// Bound and taking work while live.
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "task-1 offered while live", func() bool { return w.offerCount() == 1 })

	ti.update(func(c *lib.DispatchConfig) {
		p := c.Profiles[0]
		p.Durables, p.DrainingUntil = nil, time.Now().Add(time.Hour)
		c.Profiles[0] = p
	})
	b.submit("tse", "read", lib.SubmitKindTop, "task-2")
	time.Sleep(2 * time.Second) // several retry delays, no refresh
	if n := w.offerCount(); n != 1 {
		t.Errorf("a draining profile's still-bound durable was offered %d tasks, want only task-1", n)
	}
}
