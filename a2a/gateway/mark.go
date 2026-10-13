package gateway

import "github.com/gke-labs/kube-agents/a2a/lib"

// The mark: reaction state for the human message that started a chain
// (spec-a2a-payloads.md, "mark"). The gateway owns the state rules -- whose
// mark counts, what a terminal without one means, which message it is drawn
// on -- and draws only the difference through Adapter.React and
// RemoveReaction; which emoji a token shows is the adapter's.
const (
	// markSettleBlocked is the provisional settle: lifted by a mark without
	// it, and by the newest task ending with no mark.
	markSettleBlocked = "blocked"
	// markSettleFailed is what a newest task ending failed with no mark sets.
	markSettleFailed = "failed"
)

// MarkState is one message's whole mark: the ask kind and the settle, each
// a token or empty.
type MarkState struct {
	Ask    string `json:"ask,omitempty"`
	Settle string `json:"settle,omitempty"`
}

// tokens is the mark's tokens, in draw order: the ask first, so a reaction
// added beside it lands after it.
func (m MarkState) tokens() []string {
	var out []string
	for _, t := range []string{m.Ask, m.Settle} {
		if t != "" {
			out = append(out, t)
		}
	}
	return out
}

// markDiff is what drawing next over prev takes: the tokens to remove (in
// prev and not next) and to add (in next and not prev). A token that stays
// is neither.
func markDiff(prev, next MarkState) (remove, add []string) {
	has := func(m MarkState, t string) bool { return m.Ask == t || m.Settle == t }
	for _, t := range prev.tokens() {
		if !has(next, t) {
			remove = append(remove, t)
		}
	}
	for _, t := range next.tokens() {
		if !has(prev, t) {
			add = append(add, t)
		}
	}
	return remove, add
}

// chainNewest reports whether taskID is its chain's newest task: one that
// has minted no child, and, for a child, one whose end has started no wake.
// A minting turn's marks are ignored from the moment it mints, so a stale
// parent mark cannot undo the child's (spec: "applies a mark only from the
// chain's newest task"). An unknown task is not newest.
func (rec *SessionRecord) chainNewest(taskID string) bool {
	ref, ok := rec.TaskRefFor(taskID)
	if !ok || len(ref.Children) > 0 {
		return false
	}
	_, woke := rec.wakeOf(taskID)
	return !woke
}

// chainRootIndex is the index in rec.Tasks of taskID's chain root, or -1
// when the root has fallen off the history cap.
func (rec *SessionRecord) chainRootIndex(taskID string) int {
	ref, ok := rec.TaskRefFor(taskID)
	if !ok {
		return -1
	}
	root := ref.rootID()
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == root {
			return i
		}
	}
	return -1
}

// latestChainIndex is the index of the conversation's latest human turn
// that carries an ask message, the message a conversation notify marks, or
// -1 when there is none.
func (rec *SessionRecord) latestChainIndex() int {
	for i := len(rec.Tasks) - 1; i >= 0; i-- {
		if rec.Tasks[i].Role == "" && rec.Tasks[i].AskMessageID != "" {
			return i
		}
	}
	return -1
}

// applyTaskMark draws a mark a task's own chat message carried. Only the
// chain's newest task's mark counts. The caller holds the session lock and
// writes the record after.
func (g *Gateway) applyTaskMark(rec *SessionRecord, taskID string, next MarkState) {
	if !rec.chainNewest(taskID) {
		return
	}
	g.drawMark(rec, rec.chainRootIndex(taskID), next)
}

// applyTerminalMark is the default for the newest task ending with no mark
// on its terminal, including a supervisor terminal: the ask comes off, a
// blocked settle lifts, and failed sets the failed settle. A done or failed
// settle already drawn stays. Not called for a terminal that carried a mark;
// that one is applyTaskMark's.
func (g *Gateway) applyTerminalMark(rec *SessionRecord, taskID string, state lib.TaskState) {
	if !rec.chainNewest(taskID) {
		return
	}
	i := rec.chainRootIndex(taskID)
	if i < 0 {
		return
	}
	next := MarkState{}
	if prev := rec.Tasks[i].Mark; prev != nil && prev.Settle != markSettleBlocked {
		next.Settle = prev.Settle
	}
	if state == lib.StateFailed {
		next.Settle = markSettleFailed
	}
	g.drawMark(rec, i, next)
}

// applyNotifyMark draws a mark a conversation notify carried (card work
// after the task ended): on the conversation's latest chain message, and
// only once every task in it has ended -- while a chain is running, the
// notify's mark is dropped.
func (g *Gateway) applyNotifyMark(rec *SessionRecord, next MarkState) {
	if rec.ActiveTask != nil {
		return
	}
	g.drawMark(rec, rec.latestChainIndex(), next)
}

// drawMark draws next on the ask message of rec.Tasks[i] over what was drawn
// there before, records it on the entry, and tells a MarkObserver. Best
// effort: a reaction that fails is logged and the state still advances, so
// a reaction refused once (a missing scope) is not retried on every event.
func (g *Gateway) drawMark(rec *SessionRecord, i int, next MarkState) {
	if i < 0 || i >= len(rec.Tasks) || rec.Tasks[i].AskMessageID == "" {
		return
	}
	ref := &rec.Tasks[i]
	prev := MarkState{}
	if ref.Mark != nil {
		prev = *ref.Mark
	}
	if prev == next {
		return
	}
	remove, add := markDiff(prev, next)
	for _, t := range remove {
		if err := g.adapter.RemoveReaction(rec.Key, ref.AskMessageID, t); err != nil {
			g.log.Warn("mark: removing a reaction failed", "conversation", rec.Key, "token", t, "err", err)
		}
	}
	for _, t := range add {
		if err := g.adapter.React(rec.Key, ref.AskMessageID, t); err != nil {
			g.log.Warn("mark: adding a reaction failed", "conversation", rec.Key, "token", t, "err", err)
		}
	}
	if next == (MarkState{}) {
		ref.Mark = nil
	} else {
		stored := next
		ref.Mark = &stored
	}
	if observer, ok := g.adapter.(MarkObserver); ok {
		observer.MarkChanged(rec.Key, next.Ask, next.Settle)
	}
}
