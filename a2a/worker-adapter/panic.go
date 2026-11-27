package workeradapter

import (
	"errors"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/taskguard"
)

// A panic while the adapter handles one task ends that task and nothing
// else: a session pod or a profile pod runs its next task in the same
// process. The recovers sit here, and nowhere else:
//
//   - one task's run: execute, the funnel every caller (Run, the session
//     loop, a profile worker's task goroutine) goes through, guards run,
//     and failPanicked fails the task closed;
//   - each stdout line's decode, on the harness's scanner goroutine
//     (startHarnessWith): the scanner marks the harness (decodePanicked),
//     kills it, and drains the rest of its stdout, and the task fails
//     closed from the mark on, whichever terminal path ends it;
//   - startHarnessWith after the harness started and before supervise holds
//     it: the harness is killed and reaped there and the panic goes on to
//     execute's recovery.
//
// taskguard logs each at Error under taskguard.LogMessage and counts it in
// process (taskguard.Count). Everything else is unguarded and a panic in it
// still ends the process, as before: the gateway proxy, the delegate
// listener's goroutines, a profile worker's loop with its reset and
// heartbeat goroutines, the script runner's goroutines, the task's
// in-subject consumer callback, and the supervise reaper goroutine.
//
// Failing closed means the task ends failed with taskguard.Reason and the
// recovery publishes no answer: no result, no partial output, no stderr. A
// task failing closed (failClosed: adapter.panicked, or its harness's
// decodePanicked) publishes no result, no further stream artifact
// (thinking, progress, activity, usage) and no delegation, and finalize
// turns any terminal into the failed taskguard.Reason one. What went out
// before the panic stays.
//
// A known limit: finalize marks the terminal tried after its publish
// returns, so a panic after the bus took the terminal and before the mark
// gets the fallback's failed terminal too. The fold keeps the first, so the
// duplicate is harmless; marking before the publish would instead risk no
// terminal at all. The bridge does the same.
//
// The adapter's lock (adapter.mu) is held only over field reads and writes,
// and released by defer wherever a call sits between the Lock and the
// Unlock, so the finalize the recovery runs never waits on a lock the
// panic left held.

// Seam points: where a test's Config.panicSeam runs.
const (
	// seamBeforeExec is in run before the task's execution exists; detail
	// is the task id.
	seamBeforeExec = "before-exec"
	// seamActivity is in supervise as an activity event is handled; detail
	// is the tool's name.
	seamActivity = "activity"
	// seamResult is in publishResult before the first chunk; detail is the
	// task id.
	seamResult = "result"
	// seamFinalize is in finalize after the claim, before the publish;
	// detail is the task id.
	seamFinalize = "finalize"
)

// errPanicked refuses a result publish for a task a recovered panic ended.
var errPanicked = errors.New("a recovered panic ended the task; nothing of its answer is published")

// seam runs the test's panic seam at point, if it set one.
func (a *adapter) seam(point, detail string) {
	if a.cfg.panicSeam != nil {
		a.cfg.panicSeam(point, detail)
	}
}

// setHarness records the task's harness for the panic recovery.
func (a *adapter) setHarness(proc *harnessProc) {
	a.mu.Lock()
	defer a.mu.Unlock()
	// A one-turn-per-process driver replaces the process between turns
	// (respawn). A decode panic in the one being replaced still fails the
	// task closed: its mark moves onto the adapter before the swap.
	if prev := a.harness; prev != nil && prev.decodePanicked.Load() {
		a.panicked.Store(true)
	}
	a.harness = proc
}

// failClosed reports whether the task fails closed: a recovered panic ended
// it, or its harness's scanner recovered one decoding stdout.
func (a *adapter) failClosed() bool {
	if a.panicked.Load() {
		return true
	}
	h := a.runningHarness()
	return h != nil && h.decodePanicked.Load()
}

// panicReapBound is how long failPanicked waits for the reap of the harness
// it killed: the reap's own bounds, the stderr wait (KillGrace) and the
// leftovers' (leftoverReapBound). A process outside the group that holds
// the harness's stdout keeps the reap from starting, and past this bound
// the terminal goes out without it.
func (a *adapter) panicReapBound() time.Duration {
	return a.cfg.KillGrace + leftoverReapBound
}

// runningHarness is the task's harness, or nil before one was supervised.
func (a *adapter) runningHarness() *harnessProc {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.harness
}

// failPanicked ends a task a recovered panic interrupted. The task is
// marked, so nothing of its answer is published from here on. A harness
// that was running is killed, its stdout drained (or the scanner blocks on
// a full channel with nobody reading and keeps its pipe), and its reap
// waited for, so nothing of this task's is running when the next one
// starts, and the reap's sweep of leftovers can't reach the next task's
// harness. A reap that doesn't finish within panicReapBound is logged and
// left running, and the outcome is podBroken, so a reused pod ends rather
// than start a task beside it. Then the failed terminal goes out with taskguard.Reason and
// nothing else: the ordinary finalize, and when the panic came inside a
// finalize after its claim, the fallback (publishPanicTerminal). Each is
// guarded. A panic before the task's execution existed publishes nothing
// and returns the panic as the error.
//
// Otherwise the outcome is not podBroken: a panic is the input's doing, not
// the pod's. launched says whether a harness ran.
func (a *adapter) failPanicked(perr error) turnOutcome {
	a.panicked.Store(true)
	out := turnOutcome{harnessSession: a.harnessSession}
	if proc := a.runningHarness(); proc != nil {
		out.launched = true
		proc.kill(0)
		go func() {
			for range proc.events {
			}
		}()
		bound := time.NewTimer(a.panicReapBound())
		select {
		case <-proc.reapDone:
		case <-bound.C:
			a.log.Error("the panicked task's harness was not reaped in time; publishing its terminal and ending the pod after it",
				"task", a.cfg.TaskID, "bound", a.panicReapBound())
			out.podBroken = true
		}
		bound.Stop()
	}
	if a.exec == nil {
		out.err = perr
		return out
	}
	attrs := []any{"task", a.cfg.TaskID}
	var err error
	if gerr := taskguard.Guard(a.log, "finalize after a recovered panic", attrs, func() {
		err = a.finalize(lib.StateFailed, taskguard.Reason, "")
	}); gerr != nil {
		err = gerr
	}
	if gerr := taskguard.Guard(a.log, "terminal after a recovered panic", attrs, func() {
		if tried, ferr := a.publishPanicTerminal(); tried {
			err = ferr
		}
	}); gerr != nil {
		err = gerr
	}
	if errors.Is(err, errAbandoned) {
		out.res = Result{Abandoned: true}
		return out
	}
	out.res = Result{State: a.claimedState()}
	out.err = err
	return out
}

// claimedState is the state of the terminal finalize claimed, failed when
// none was.
func (a *adapter) claimedState() lib.TaskState {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.finalState == "" {
		return lib.StateFailed
	}
	return a.finalState
}

// publishPanicTerminal writes the failed taskguard.Reason terminal of a task
// whose finalize panicked after its claim and before its publish. tried is
// false, and nothing is published, when a terminal was already tried, no
// finalize claimed one (the ordinary finalize then ran and tried it), or
// the dispatcher abandoned the task.
func (a *adapter) publishPanicTerminal() (tried bool, err error) {
	if a.abandoned() || !a.claimPanicTerminal() {
		return false, nil
	}
	err = a.publishTerminal(lib.StateFailed, taskguard.Reason, "")
	a.recordTerminal(err)
	return true, err
}

// claimPanicTerminal claims the fallback terminal: true when finalize was
// claimed and no terminal tried, and then marks one tried, so the fallback
// runs at most once.
func (a *adapter) claimPanicTerminal() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.finalized || a.terminalTried {
		return false
	}
	a.terminalTried, a.finalState = true, lib.StateFailed
	return true
}
