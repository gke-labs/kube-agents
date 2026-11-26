package hermesbridge

import (
	"context"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/taskguard"
)

// A panic while one task is handled ends that task and nothing else. The
// recovers sit at these goroutine boundaries, and nowhere else:
//
//   - each worker's task: guardTask around runTask;
//   - a card stop's goroutine: guardTask around cardStop;
//   - the activity door, for a delivery routed to a task: takeDelivery
//     (the decode, the tool result's secret collection, the observation),
//     with observeDelivery recovering the observation itself under run.mu,
//     and collectCardWorkerSecrets for a card worker's delivery;
//   - each heartbeat tick (runActivity).
//
// taskguard logs each at Error under taskguard.LogMessage and counts it in
// process (taskguard.Count; nothing exports the count, so the log line is
// what an operator alerts on). Everything else is unguarded and a panic in
// it still ends the process, as before: the durable's consumer goroutine
// (accept, a cancel or follow-up delivery, and the finalizes it runs), the
// deadline and kill-grace timer callbacks, the API executor's recordOrigin
// goroutine, abortCanceledSessionKanbanCards, the synthesis's closing
// watcher, the board's action goroutine, and the activity door's routing
// before a delivery is matched to a task (net/http recovers a handler's
// panic there; no task is marked).
//
// Failing closed means the task ends failed with taskguard.Reason and the
// recovery path publishes no answer: it finalizes with no result, so no
// scrub, chat fold or result publish runs on the answer in hand, and a run
// marked panicked (taskRun.panicked) has any later finalize's result
// dropped the same way, and no further turn artifact. What was published
// before the panic stays: earlier turn artifacts, and the chunks of a
// result publish the panic interrupted, as a result publish that fails
// partway leaves them today.
//
// The task locks are released by defer in every critical section a panic
// on a guarded goroutine can reach with a call between the Lock and the
// Unlock, so the finalize the recovery runs does not wait on a lock the
// panic left held. The exceptions hold the lock over nothing that can
// panic short of a runtime fault in the bridge's own bookkeeping: state
// reads and stores, atomic flags, context cancel functions, a goroutine
// start, and slice and map bookkeeping (takeSteerLocked,
// noticeStateLocked, the session-turn slots, the kanban canceled-session
// map, the task table).

// guardTask runs fn, one task's work, under taskguard.Guard; a panic in it
// fails the run closed (failPanicked) and returns.
func (b *Bridge) guardTask(what string, run *taskRun, fn func()) {
	if taskguard.Guard(b.cfg.Logger, what, []any{"task", run.origin.TaskID}, fn) == nil {
		return
	}
	b.failPanicked(run)
}

// failPanicked ends a run a recovered panic interrupted: failed, with
// taskguard.Reason, and no result. The ordinary finalize runs first, with
// no result; when the panic came inside a finalize after it set stateDone,
// that finalize is a no-op and no terminal went out yet, so
// publishPanicTerminal writes it. Each step is guarded too: a panic in
// either leaves the worker serving and the task in the KV registry for the
// sweep.
func (b *Bridge) failPanicked(run *taskRun) {
	run.panicked.Store(true)
	attrs := []any{"task", run.origin.TaskID}
	_ = taskguard.Guard(b.cfg.Logger, "finalize after a recovered panic", attrs, func() {
		b.finalize(run, lib.StateFailed, taskguard.Reason, nil)
	})
	_ = taskguard.Guard(b.cfg.Logger, "terminal after a recovered panic", attrs, func() {
		b.publishPanicTerminal(run)
	})
}

// publishPanicTerminal writes the failed taskguard.Reason terminal of a run
// whose finalize panicked after stateDone, before its terminal publish: no
// result, no activity drain (the drain may be what panicked), the heartbeat
// stopped. A run whose terminal was already tried is left alone.
func (b *Bridge) publishPanicTerminal(run *taskRun) {
	tried, err := b.panicTerminalLocked(run)
	if !tried {
		return
	}
	b.afterTerminal(run, lib.StateFailed, err)
}

// panicTerminalLocked is publishPanicTerminal's critical section, under
// noticeMu and mu, both released by defer. tried false means a terminal was
// already tried and nothing was published.
func (b *Bridge) panicTerminalLocked(run *taskRun) (tried bool, err error) {
	run.noticeMu.Lock()
	defer run.noticeMu.Unlock()
	run.mu.Lock()
	defer run.mu.Unlock()
	if run.terminalTried {
		return false, nil
	}
	if a := run.act.Load(); a != nil {
		a.signalStop()
	}
	run.state = stateDone
	ctx, cancel := context.WithTimeout(context.Background(), finalizePublishTimeout)
	defer cancel()
	err = b.publishTerminal(ctx, run, lib.StateFailed, taskguard.Reason)
	run.terminalTried = true
	b.clearSeenSecrets(run)
	return true, err
}
