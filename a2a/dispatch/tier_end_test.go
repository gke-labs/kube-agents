package dispatch

import (
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// tierEndInstall runs tse with read and oncall live, a worker on each, and a
// task holding on oncall; it returns the bus, the oncall worker, the install
// and the dispatcher.
func tierEndInstall(t *testing.T) (*bus, *fakeWorker, *testInstall, *Dispatcher) {
	t.Helper()
	b := newBus(t, "tse-0", "tse-1")
	b.ensure("tse", 4, 0, "read", "oncall")
	b.worker("tse", "read", "tse-0", answerAccept, 200*time.Millisecond)
	w := b.worker("tse", "oncall", "tse-1", answerAccept, time.Hour)
	ti := oneProfile("tse", 4, "tse-0")
	ti.pool.Set("tse", "oncall", "tse-1")
	ti.update(func(c *lib.DispatchConfig) {
		c.Profiles[0] = testProfile("tse", 4, "read", "oncall")
		c.BreakerThreshold = 1
	})
	d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	b.submit("tse", "oncall", lib.SubmitKindTop, "task-oncall")
	waitFor(t, 10*time.Second, "task-oncall running", func() bool { return w.startedTask("task-oncall") })
	return b, w, ti, d
}

// drainOncall moves oncall from live to draining, ending it or not.
func drainOncall(ti *testInstall, ending bool) {
	ti.update(func(c *lib.DispatchConfig) {
		p := testProfile("tse", 4, "read", "oncall")
		p.Tiers = []string{"read"}
		p.DrainingTiers = []lib.DrainingTier{{Tier: "oncall", Mode: "interactive", Until: time.Now().Add(time.Hour), Ending: ending}}
		c.Profiles[0] = p
	})
}

// An administrator ending a draining tier (#5850) ends the task held there at
// once: canceled tier-ended, the worker told to abandon it, the submission
// tombstoned, and no breaker opened, since no worker was lost.
func TestEndingADrainingTierEndsItsHeldTasks(t *testing.T) {
	b, w, ti, d := tierEndInstall(t)
	start := time.Now()
	drainOncall(ti, true)
	ts := b.waitTerminal("task-oncall", 10*time.Second)
	if ts.state != lib.StateCanceled || !hasReason(ts, ReasonTierEnded) || ts.class != lib.TaskClassSupervisor {
		t.Fatalf("terminal %+v, want the dispatcher's canceled %s", ts, ReasonTierEnded)
	}
	if took := time.Since(start); took > 5*time.Second {
		t.Errorf("ended %s after the drain was ended; want it within a refresh or two", took)
	}
	waitFor(t, 5*time.Second, "the abandon on the worker", func() bool { return w.abandonCount() > 0 })
	waitFor(t, 5*time.Second, "task-oncall's tombstone", func() bool {
		_, ok := b.tombstones()[lib.ProfileLeaseKey("tse", "oncall", "tse-1", "task-oncall")]
		return ok
	})
	if open := d.Health().OpenBreakers; len(open) != 0 {
		t.Errorf("breakers %v open after an administrator ended a tier; no worker was lost", open)
	}
	b.submit("tse", "read", lib.SubmitKindTop, "task-read")
	if ts := b.waitTerminal("task-read", 10*time.Second); ts.state != lib.StateCompleted {
		t.Errorf("the live tier's task after the end: %+v", ts)
	}
}

// A drain that is not being ended leaves the held task running to its end.
func TestADrainNotEndedLeavesItsHeldTasksRunning(t *testing.T) {
	b, _, ti, _ := tierEndInstall(t)
	drainOncall(ti, false)
	time.Sleep(time.Second) // several refreshes
	if ts := b.terminalsOf("task-oncall"); len(ts) != 0 {
		t.Fatalf("task-oncall ended on a drain nobody ended: %+v", ts[0])
	}
}
