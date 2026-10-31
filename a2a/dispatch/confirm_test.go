package dispatch

import (
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The confirm step (#3655 contract, addendum 5): offer, accept, then
// confirm or abandon. A worker starts only on a confirm, the dispatcher
// confirms only the pod its live offer is with, and the lease starts at the
// confirm.

// A lost accept: tse-0 takes the offer, but its accept never reaches the
// dispatcher. The offer times out, tse-0 is told to abandon it before the
// task goes to tse-1, and only tse-1 is confirmed: the task runs once.
func TestALostAcceptIsAbandonedBeforeTheReoffer(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1")
	b.ensure("tse", 2, 0, "read")
	lost := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	lost.dropAccept = true
	good := b.worker("tse", "read", "tse-1", answerAccept, time.Second)
	b.runDispatcher(testConfig(), oneProfile("tse", 2, "tse-0", "tse-1"), &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 15*time.Second); ts.state != lib.StateCompleted || ts.from != "tse-1" {
		t.Fatalf("terminal %+v, want tse-1's completed", ts)
	}
	if lost.offerCount() != 1 || lost.abandonCount() != 1 {
		t.Errorf("tse-0: %d offers, %d abandons; want its timed-out offer abandoned once", lost.offerCount(), lost.abandonCount())
	}
	if lost.confirmCount() != 0 || lost.startedCount() != 0 {
		t.Errorf("tse-0 was confirmed (%d) or started (%d) after its accept was lost", lost.confirmCount(), lost.startedCount())
	}
	if good.confirmCount() != 1 || good.startedCount() != 1 {
		t.Errorf("tse-1: %d confirms, %d starts; want one each", good.confirmCount(), good.startedCount())
	}
	// The abandon reached tse-0 before tse-1 was offered the task.
	lost.mu.Lock()
	abandon := lost.abandons[0]
	lost.mu.Unlock()
	if abandon.TaskID != "task-1" || abandon.Reason != abandonTimedOut {
		t.Errorf("tse-0's abandon %+v", abandon)
	}
}

// A late accept: tse-0 accepts after the accept window, by which time the
// task has gone to tse-1. tse-0's accept is answered with an abandon, never
// a confirm, and the task runs once, on tse-1.
func TestALateAcceptAfterAReofferIsAbandoned(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1")
	b.ensure("tse", 2, 0, "read")
	late := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	late.acceptDelay = 1500 * time.Millisecond // the window is 1s
	good := b.worker("tse", "read", "tse-1", answerAccept, 3*time.Second)
	b.runDispatcher(testConfig(), oneProfile("tse", 2, "tse-0", "tse-1"), &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "tse-1 confirmed", func() bool { return good.confirmCount() == 1 })
	// One abandon when the offer timed out, one for the late accept.
	waitFor(t, 5*time.Second, "the late accept abandoned", func() bool { return late.abandonCount() == 2 })
	late.mu.Lock()
	reasons := []string{late.abandons[0].Reason, late.abandons[1].Reason}
	late.mu.Unlock()
	if reasons[0] != abandonTimedOut || reasons[1] != abandonWithdrawn {
		t.Errorf("tse-0's abandons %q, want the timeout's then the late accept's", reasons)
	}
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted || ts.from != "tse-1" {
		t.Errorf("terminal %+v, want tse-1's completed", ts)
	}
	if late.confirmCount() != 0 || late.startedCount() != 0 {
		t.Errorf("the late accepter was confirmed (%d) or started (%d)", late.confirmCount(), late.startedCount())
	}
	if n := len(b.terminalsOf("task-1")); n != 1 {
		t.Errorf("%d terminals, want 1", n)
	}
}

// A lost confirm: tse-0 accepts, the dispatcher writes the lease and
// confirms, and the confirm never reaches tse-0. It never starts, no renewal
// comes, and the task fails lease-expired. It is not offered again, to tse-1
// or anyone: the dispatcher can't tell a lost confirm from a dead worker.
func TestALostConfirmFailsTheTaskAndNeverRunsItTwice(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	w.dropConfirm = true
	other := b.worker("tse", "read", "tse-1", answerAccept, time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the confirm", func() bool { return w.confirmCount() == 1 })
	ti.pool.Set("tse", "read", "tse-0", "tse-1")
	ts := b.waitTerminal("task-1", 10*time.Second)
	if ts.state != lib.StateFailed || !hasReason(ts, ReasonLeaseExpired) || ts.from != selfSession || ts.class != lib.TaskClassSupervisor {
		t.Errorf("terminal %+v, want the dispatcher's failed lease-expired", ts)
	}
	time.Sleep(3 * time.Second) // many retry delays
	if w.startedCount() != 0 || other.offerCount() != 0 || w.offerCount() != 1 {
		t.Errorf("tse-0 started %d, offered %d; tse-1 offered %d: want one offer and no run", w.startedCount(), w.offerCount(), other.offerCount())
	}
	if n := len(b.terminalsOf("task-1")); n != 1 {
		t.Errorf("%d terminals, want 1", n)
	}
	waitFor(t, 5*time.Second, "the expired lease abandoned on tse-0", func() bool { return w.abandonCount() == 1 })
	w.mu.Lock()
	reason := w.abandons[0].Reason
	w.mu.Unlock()
	if reason != abandonLeaseExpired {
		t.Errorf("abandon reason %q", reason)
	}
}

// The lease starts at the confirm, so nothing is written while an offer is
// out, and a dispatcher stopped mid-offer leaves no record. The next one
// dispatches the task afresh. The first pod was told to drop the offer and
// never heard a confirm, so the task runs once.
func TestARestartMidOfferDispatchesAfresh(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1")
	b.ensure("tse", 2, 0, "read")
	first := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	first.dropAccept = true // took the offer; the dispatcher never hears
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.AcceptTimeoutSeconds = 5 })
	_, stop1 := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the offer", func() bool { return first.offerCount() == 1 })
	time.Sleep(300 * time.Millisecond)
	if l := b.leases(); len(l) != 0 {
		t.Fatalf("a lease was written before any confirm: %v", l)
	}
	stop1()
	if l := b.leases(); len(l) != 0 {
		t.Fatalf("a dispatcher stopped mid-offer left a lease: %v", l)
	}

	second := b.worker("tse", "read", "tse-1", answerAccept, 500*time.Millisecond)
	ti.update(func(c *lib.DispatchConfig) { c.AcceptTimeoutSeconds = 1 })
	ti.pool.Set("tse", "read", "tse-1")
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	if ts := b.waitTerminal("task-1", 15*time.Second); ts.state != lib.StateCompleted || ts.from != "tse-1" {
		t.Errorf("terminal %+v, want tse-1's completed", ts)
	}
	if first.confirmCount() != 0 || first.startedCount() != 0 {
		t.Errorf("the first pod was confirmed (%d) or started (%d)", first.confirmCount(), first.startedCount())
	}
	if second.startedCount() != 1 {
		t.Errorf("the second pod started %d times", second.startedCount())
	}
	if n := len(b.terminalsOf("task-1")); n != 1 {
		t.Errorf("%d terminals, want 1", n)
	}
}

// The offer carries the config's accept window and passes the contract's
// checks. The record is written at the confirm with the confirm's time, and
// carries the offer's deadline and intervals.
func TestTheLeaseIsWrittenAtTheConfirm(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 3*time.Second)
	w.acceptDelay = 600 * time.Millisecond
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.AcceptTimeoutSeconds = 4 })
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the offer", func() bool { return w.offerCount() == 1 })
	offered := time.Now()
	waitFor(t, 5*time.Second, "the confirm", func() bool { return w.confirmCount() == 1 })
	recs := b.leases()
	rec, ok := recs[lib.ProfileLeaseKey("tse", "read", "tse-0", "task-1")]
	if !ok {
		t.Fatalf("no record after the confirm: %v", recs)
	}
	if rec.ConfirmedAt.Before(offered.Add(400 * time.Millisecond)) {
		t.Errorf("confirmedAt %v is before the accept came (offer at %v)", rec.ConfirmedAt, offered)
	}
	w.mu.Lock()
	offer := w.offers[0]
	w.mu.Unlock()
	if offer.AcceptSeconds != 4 {
		t.Errorf("the offer's acceptSeconds is %d, want the config's 4", offer.AcceptSeconds)
	}
	if err := offer.Validate(); err != nil {
		t.Errorf("the offer fails the contract: %v", err)
	}
	if !rec.Deadline.Equal(offer.Deadline) || rec.LeaseSeconds != offer.LeaseSeconds || rec.RenewSeconds != offer.RenewSeconds {
		t.Errorf("record %+v does not carry the offer's deadline %v and intervals %d/%d", rec, offer.Deadline, offer.LeaseSeconds, offer.RenewSeconds)
	}
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Errorf("terminal %+v", ts)
	}
}
