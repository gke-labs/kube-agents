package gateway

import (
	"slices"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

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
// that carries an ask message, the chain a new notify key is bound to, or
// -1 when there is none.
func (rec *SessionRecord) latestChainIndex() int {
	for i := len(rec.Tasks) - 1; i >= 0; i-- {
		if rec.Tasks[i].Role == "" && rec.Tasks[i].AskMessageID != "" {
			return i
		}
	}
	return -1
}

// continuedAskMessage is the ask message, and the mark drawn on it, that a
// chain answering a question takes over (a click, or a typed answer to an
// open question): the conversation's latest chain's. The answer continues
// that ask's work, so its chain marks the ask whose question it answered,
// starting from what is already drawn there so the next mark draws only its
// difference, and the card's later settle lands on the ask rather than on
// the answer. Empty when the conversation has no such chain.
func (rec *SessionRecord) continuedAskMessage() (string, *MarkState) {
	i := rec.latestChainIndex()
	if i < 0 {
		return "", nil
	}
	var mark *MarkState
	if m := rec.Tasks[i].Mark; m != nil {
		copied := *m
		mark = &copied
	}
	return rec.Tasks[i].AskMessageID, mark
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
// blocked settle lifts, and failed sets the failed settle. Otherwise a done
// or failed settle already drawn stays (a failed end replaces a done).
// Not called for a terminal that carried a mark;
// that one is applyTaskMark's. The state is the end every observer is told
// (handOffEnd): a session turn whose delegate request minted no child ends
// failed, whatever its hand-off line's completed says.
func (g *Gateway) applyTerminalMark(rec *SessionRecord, taskID string, state lib.TaskState) {
	if !rec.chainNewest(taskID) {
		return
	}
	if replaced, _, handOff := rec.handOffEnd(taskID, state); handOff {
		state = replaced
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

// notifyChainIndex is the index of the chain root a notify update key is
// bound to (NotifyKeys), or -1 when no entry in the history holds it.
func (rec *SessionRecord) notifyChainIndex(update string) int {
	if update == "" {
		return -1
	}
	for i := len(rec.Tasks) - 1; i >= 0; i-- {
		if slices.Contains(rec.Tasks[i].NotifyKeys, update) {
			return i
		}
	}
	return -1
}

// bindNotifyKey binds update to the chain root at rec.Tasks[i], keeping the
// newest notifyUpdateKeysMax keys there.
func (rec *SessionRecord) bindNotifyKey(i int, update string) {
	keys := append(rec.Tasks[i].NotifyKeys, update)
	if len(keys) > notifyUpdateKeysMax {
		keys = keys[len(keys)-notifyUpdateKeysMax:]
	}
	rec.Tasks[i].NotifyKeys = keys
}

// askIndex is the index of the newest chain root marking askMessageID: the
// chain that holds what is drawn there now, which a chain continuing an ask
// (continuedAskMessage) took over from the one before it. -1 for none.
func (rec *SessionRecord) askIndex(askMessageID string) int {
	for i := len(rec.Tasks) - 1; i >= 0; i-- {
		if rec.Tasks[i].Role == "" && rec.Tasks[i].AskMessageID == askMessageID {
			return i
		}
	}
	return -1
}

// runningAsk is the ask message the running chain marks, or "" when no
// chain runs or its root has left the history.
func (rec *SessionRecord) runningAsk() string {
	if rec.ActiveTask == nil {
		return ""
	}
	if i := rec.chainRootIndex(rec.ActiveTask.TaskID); i >= 0 {
		return rec.Tasks[i].AskMessageID
	}
	return ""
}

// applyNotifyMark draws a mark a conversation notify carried (card work
// after the task ended) on the ask of the chain the card belongs to. A
// notify names no task, so its update key is what names the chain: the
// first time a key arrives with a mark it is bound to the conversation's
// latest chain (the chain whose turn filed the card is the latest one
// while its cards start), and every later mark with that key marks that
// chain's ask, whatever chain has started since, as the chain's newest
// writer once its own tasks have ended -- while a chain marking the same ask
// runs, the mark is dropped. A mark with no key names no chain: it marks
// the latest chain once nothing runs, and is dropped, logged and counted
// while a chain runs. It reports whether the record changed.
func (g *Gateway) applyNotifyMark(rec *SessionRecord, update string, next MarkState) bool {
	i := rec.notifyChainIndex(update)
	bound := false
	if i < 0 && update != "" {
		if i = rec.latestChainIndex(); i >= 0 {
			rec.bindNotifyKey(i, update)
			bound = true
		}
	}
	if i < 0 {
		if rec.ActiveTask != nil {
			g.log.Info("notify: mark names no chain and a chain runs in the conversation; dropped", "conversation", rec.Key)
			g.metrics.notifyMarkDropped()
			return false
		}
		return g.drawMark(rec, rec.latestChainIndex(), next)
	}
	ask := rec.Tasks[i].AskMessageID
	if ask != "" && ask == rec.runningAsk() {
		g.log.Info("notify: mark dropped while its chain's ask has a chain running", "conversation", rec.Key)
		return bound
	}
	return g.drawMark(rec, rec.askIndex(ask), next) || bound
}

// sessionMark is the mark the conversation's Agent Session follows: the
// latest chain's, unless that holds no open work and an older ask still
// does (its cards still running, or blocked on the person), in which case
// the newest such ask's, so the session holds until every chain's cards
// have settled. Each ask counts once, by the chain holding what is drawn
// there now.
func (rec *SessionRecord) sessionMark() MarkState {
	open := func(m MarkState) bool { return m.Ask != "" || m.Settle == markSettleBlocked }
	latest := MarkState{}
	seen := map[string]bool{}
	for i := len(rec.Tasks) - 1; i >= 0; i-- {
		ref := rec.Tasks[i]
		if ref.Role != "" || ref.AskMessageID == "" || seen[ref.AskMessageID] {
			continue
		}
		m := MarkState{}
		if ref.Mark != nil {
			m = *ref.Mark
		}
		if len(seen) == 0 {
			latest = m
		}
		seen[ref.AskMessageID] = true
		if open(m) {
			return m
		}
	}
	return latest
}

// drawMark draws next on the ask message of rec.Tasks[i] over what was drawn
// there before, records it on the entry, and tells a MarkObserver when the
// conversation's session mark (sessionMark) moved; it reports whether
// anything changed. Best effort: a reaction that fails is logged and the
// state still advances, so a reaction refused once (a missing scope) is not
// retried on every event.
func (g *Gateway) drawMark(rec *SessionRecord, i int, next MarkState) bool {
	if i < 0 || i >= len(rec.Tasks) || rec.Tasks[i].AskMessageID == "" {
		return false
	}
	ref := &rec.Tasks[i]
	prev := MarkState{}
	if ref.Mark != nil {
		prev = *ref.Mark
	}
	if prev == next {
		return false
	}
	session := rec.sessionMark()
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
	// The observer holds the session mark, so an older chain's late mark (a
	// stopped task ending after a newer ask) redraws its own message without
	// telling it, unless the session was holding on that ask's open work.
	if observer, ok := g.adapter.(MarkObserver); ok {
		if now := rec.sessionMark(); now != session {
			observer.MarkChanged(rec.Key, now.Ask, now.Settle)
		}
	}
	return true
}
