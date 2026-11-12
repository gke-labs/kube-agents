package gateway

import (
	"context"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// One stop cancels the conversation's whole task tree (spec-chatops-gateway.md,
// "Stop"): cancelTaskVoiced cancels the task holding the conversation and the
// profile parents waiting on it, and stopTree walks down from the chain's
// root to every other live task, a fan-out's children included. Nothing a
// canceled task writes afterwards reaches the room (applyCanceledEvent), so
// the stop's own notice is the only output.

// reasonStopped is the reason token on a stopped fan-out's chain end, and
// the text its children's ends carry.
const reasonStopped = "stopped"

// stoppedEndReason is a stopped fan-out's chain end, as observers read it.
const stoppedEndReason = "reason: " + reasonStopped + " - the conversation's tasks were stopped"

// noticeTreeTaskStopped closes the line of a task the tree-wide stop
// canceled, which never held the conversation's own line.
const noticeTreeTaskStopped = "🛑 stopped"

// stopTree cancels every live task in the tree of stoppedID, the task the
// stop just canceled, other than stoppedID itself: from the chain's root
// down each task's Children. Each live task is sent a cancel on its own
// addressee's .in under the stop's authority, marked canceled (whether or
// not the cancel went out, so the heal and the batch can still end it),
// marked quiet when the stop was, and given its stop record for a profile
// task; its own line, when it keeps one, is closed. A task already ended is
// left alone, and one whose earlier cancel never went out is sent it again.
// A cancel doesn't end a task: each stays live (liveChild) until its
// terminal, or the heal, ends it, and a stopped fan-out ends, with no wake,
// when its last child does (wakeBatch). A fan-out caught partway through
// minting mints no more (markStoppedBatch). Called under the session lock;
// the caller writes the record.
func (g *Gateway) stopTree(ctx context.Context, rec *SessionRecord, stoppedID string, authority Authority, quiet bool) {
	stopped, ok := rec.TaskRefFor(stoppedID)
	if !ok {
		return
	}
	root, ok := rec.TaskRefFor(stopped.rootID())
	if !ok {
		root = stopped
	}
	seen := map[string]bool{}
	queue := []string{root.ID}
	var batches []string
	for len(queue) > 0 && len(seen) <= len(rec.Tasks) {
		id := queue[0]
		queue = queue[1:]
		if seen[id] {
			continue
		}
		seen[id] = true
		ref, held := rec.TaskRefFor(id)
		if !held {
			continue
		}
		queue = append(queue, ref.Children...)
		if ref.fanOut() {
			batches = append(batches, id)
		}
		if id == stoppedID || !g.liveNode(ctx, rec, ref) {
			continue
		}
		g.cancelTreeNode(ctx, rec, ref, authority, quiet)
	}
	for _, id := range batches {
		rec.markStoppedBatch(id)
	}
}

// liveNode reports a task of the tree the gateway hasn't seen end: not
// already canceled, no end recorded, and still routed to this conversation (the relay retires a
// task's route at its terminal). A lookup that errors can't rule the task
// out, so it counts as live: a cancel to a task that has ended is harmless.
//
// A task marked canceled whose cancel never went out (CancelUnsent) is live
// while it is routed, so a later stop sends its cancel again.
func (g *Gateway) liveNode(ctx context.Context, rec *SessionRecord, ref TaskRef) bool {
	if ref.ChainEnd != nil || ref.TerminalObserved {
		return false
	}
	if !ref.CancelUnsent && (ref.Canceled || ref.BatchEnd != nil) {
		return false
	}
	key, err := g.lookupTaskSession(ctx, ref.ID)
	if err != nil {
		g.log.Error("task index lookup failed; stopping the task anyway", "taskId", ref.ID, "conversation", rec.Key, "err", err)
		return true
	}
	return key == rec.Key
}

// cancelTreeNode cancels one live task of a stopped tree (stopTree).
func (g *Gateway) cancelTreeNode(ctx context.Context, rec *SessionRecord, ref TaskRef, authority Authority, quiet bool) {
	addressee := rec.AddresseeFor(ref.ID)
	env, err := lib.NewCancelEnvelope(gatewayParty, ref.ID, rec.ContextID, ref.CorrelationID,
		lib.WithTo(lib.Party{Session: addressee}),
		lib.WithAuthority(authority.Render(ref.Capability)))
	if err == nil {
		err = g.client.Publish(ctx, lib.TaskInSubject(addressee, ref.ID), env)
	}
	if err != nil {
		g.log.Error("cancel publish failed for a task in the stopped tree; it stays marked canceled, its cancel unsent",
			"taskId", ref.ID, "conversation", rec.Key, "addressee", addressee, "err", err)
	} else {
		g.log.Info("stop: canceled a task in the tree", "taskId", ref.ID, "conversation", rec.Key, "addressee", addressee)
	}
	rec.MarkCanceled(ref.ID)
	rec.setCancelUnsent(ref.ID, err != nil)
	rec.setStopPending(ref.ID, true)
	if quiet {
		rec.markQuietCancel(ref.ID)
	}
	g.publishDispatchStop(ctx, ref)
	if ref.StatusMsgID != "" {
		g.editLine(rec.Key, ref.StatusMsgID, withLineNote(noticeTreeTaskStopped, ref.LineNote), "")
		setParentLine(rec, ref.ID, "")
	}
}

// markStoppedBatch records that a stopped fan-out caught partway through
// minting mints no more (DelegationEnd), so only the children on the record
// count toward its end (batchLeft) and the last of them ends the chain.
func (rec *SessionRecord) markStoppedBatch(parentID string) {
	parent, ok := rec.TaskRefFor(parentID)
	if ok && parent.fanOut() && parent.Canceled && !parent.batchComplete() {
		rec.markDelegationEnd(parentID, notStartedEnd(whyTurnStopped))
	}
}

// setCancelUnsent sets or clears a task's CancelUnsent mark.
func (rec *SessionRecord) setCancelUnsent(taskID string, unsent bool) {
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == taskID {
			rec.Tasks[i].CancelUnsent = unsent
		}
	}
}

// resendUnsentCancels sends again the cancel of each task marked canceled
// whose cancel never reached the bus (CancelUnsent) and which is still
// routed to this conversation, under its delegation's authority as a
// withdrawal's cancel is (no stopper's authority is to hand at the heal).
// One that goes out is cleared and marked StopPending, so the heal ends it
// if it never confirms; it still counts as live until it ends. One that has
// ended (no route here) is cleared. It reports whether the record changed.
func (g *Gateway) resendUnsentCancels(ctx context.Context, rec *SessionRecord) bool {
	changed := false
	for _, ref := range rec.Tasks {
		if !ref.CancelUnsent {
			continue
		}
		key, err := g.lookupTaskSession(ctx, ref.ID)
		if err != nil {
			g.log.Error("task index lookup failed; re-sending the cancel anyway", "taskId", ref.ID, "conversation", rec.Key, "err", err)
		} else if key != rec.Key {
			rec.setCancelUnsent(ref.ID, false)
			changed = true
			continue
		}
		parent, _ := rec.TaskRefFor(ref.ParentTaskID)
		authority, ok := delegationAuthority(parent)
		if !ok {
			// No executor checks a cancel's grants, so the cancel still
			// goes, with the via that names the delegation.
			g.log.Warn("the delegation's attribution is not readable; re-sending the cancel with an empty requester",
				"taskId", ref.ID, "parent", parent.ID, "conversation", rec.Key)
			authority.Via = &AuthorityVia{TaskID: parent.ID, Session: parent.Addressee}
		}
		if !g.publishBatchCancel(ctx, rec, ref, authority, cancelResent) {
			continue
		}
		g.log.Info("re-sent the cancel of a task whose first cancel never went out", "taskId", ref.ID, "conversation", rec.Key)
		g.publishDispatchStop(ctx, ref)
		rec.setCancelUnsent(ref.ID, false)
		rec.setStopPending(ref.ID, true)
		changed = true
	}
	return changed
}

// cancelResent names a re-sent cancel in publishBatchCancel's log lines.
const cancelResent = "a task whose first cancel never went out"

// setStopPending sets or clears a task's StopPending mark.
func (rec *SessionRecord) setStopPending(taskID string, pending bool) {
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == taskID {
			rec.Tasks[i].StopPending = pending
		}
	}
}

// healStoppedTasks ends each task a tree-wide stop canceled whose terminal
// the relay hasn't delivered (StopPending): a terminal on its stream is
// logged as the relay would have, and a task with nothing on its stream past
// FirstEventGrace is logged as never started. Either way its route is
// retired and the mark cleared, and nothing is posted: a canceled task
// writes nothing to the room. A task still running is left for its
// terminal. It reports whether the record changed.
func (g *Gateway) healStoppedTasks(ctx context.Context, rec *SessionRecord) bool {
	changed := g.resendUnsentCancels(ctx, rec)
	for _, ref := range rec.Tasks {
		if !ref.StopPending {
			continue
		}
		addressee := rec.AddresseeFor(ref.ID)
		task, terminalSubject, _, err := g.client.TasksGetTerminal(ctx, addressee, ref.ID)
		switch {
		case err == nil && task.Final:
			source := TerminalFromExecutor
			if terminalSubject == lib.TaskSupervisorSubject(addressee, ref.ID) {
				source = TerminalFromSupervisor
			}
			g.log.Info("healing a stopped task whose terminal the relay never delivered", "conversation", rec.Key, "taskId", ref.ID, "state", task.State)
			g.logTaskTerminal(rec, addressee, ref.ID, task.State, source, finalMessageText(task))
		case noFirstEventPastGrace(&ActiveTask{SubmittedAt: ref.StartedAt}, isTaskNotFound(err), g.cfg.FirstEventGrace, time.Now()):
			g.log.Info("a stopped task never started; ending it", "conversation", rec.Key, "taskId", ref.ID,
				"addressee", addressee, "grace", g.cfg.FirstEventGrace)
			g.logTaskTerminal(rec, addressee, ref.ID, lib.StateCanceled, TerminalNeverStarted, "")
		default:
			continue
		}
		g.retireTaskRoute(ctx, ref.ID)
		rec.setStopPending(ref.ID, false)
		changed = true
		// A fan-out child not yet counted ended (a withdrawn batch's kept
		// child) ends its batch here, as its terminal would have.
		if fresh, _ := rec.TaskRefFor(ref.ID); fresh.BatchEnd == nil && rec.fanOutChild(fresh) {
			state := lib.StateCanceled
			if err == nil && task.Final {
				state = task.State
			}
			g.batchChildEnded(ctx, rec, fresh, BatchEnd{State: state, Text: reasonStopped, Source: TerminalFromGateway}, stoppedEndReason)
		}
	}
	return changed
}
