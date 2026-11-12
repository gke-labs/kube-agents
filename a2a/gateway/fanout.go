package gateway

import (
	"context"
	"fmt"
	"log/slog"
	"slices"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// A fan-out is a session turn's batch of two or more children (mintBatch).
// The turn that asked keeps the conversation while they run: its entry holds
// the active slot, so a stop reaches it, a steer is refused and a status
// lists the children (spec-chatops-gateway.md, "Sessions by default"). Each
// child's end is kept on its own entry (TaskRef.BatchEnd), and the last to
// end wakes the session once with every one of them (batchChildEnded).

// The wake's text for a fan-out (batchWakeText).
const (
	// wakeBatchHeaderAsk follows the ask block; wakeBatchHeaderBare opens
	// a wake whose delegating turn's request isn't on record.
	wakeBatchHeaderAsk  = "You delegated %d tasks, and all of them have ended."
	wakeBatchHeaderBare = "The %d tasks you delegated have all ended."
	// wakeBatchResultsLabel says the entries below are the children's
	// output, not the user's ask.
	wakeBatchResultsLabel = "Results from the delegated tasks (not from the user):"
	// wakeBatchEntry heads each child's fenced text: its place, its name
	// (batchChildName) and its outcome (batchOutcome).
	wakeBatchEntry = "%d. %s, which %s:"
	// wakeBatchCutNote follows the "…" truncateRunes leaves on a child's
	// text cut to its share of the wake.
	wakeBatchCutNote = " (cut to its share of a wake carrying %d results)"
	// wakeBatchQuestionCutNote follows a child's question cut the same way:
	// a question is posted nowhere else, so the session asks it again.
	wakeBatchQuestionCutNote = " (question cut to its share of a wake carrying %d results; it was not posted anywhere, so ask the task again for the rest)"
	// wakeBatchQuestionGuide is added when a child ended on a question
	// (batchChildAsks): the session asks the person once, for all of them.
	wakeBatchQuestionGuide = "A task that needed input ended with its question instead of asking the person. Ask them once, in your own words, for everything those questions need."
	// wakeBatchNoText stands in for a child's empty reason, so every
	// entry carries a block.
	wakeBatchNoText = "(no reason given)"
)

// Each child's outcome in the wake's entry for it (batchOutcome).
const (
	batchOutcomeCompleted = "completed"
	batchOutcomeAsked     = "needs input; its question"
	batchOutcomeRejected  = "was rejected"
	batchOutcomeStopped   = "was stopped"
	batchOutcomeFailed    = "failed"
)

// batchHeaderMax bounds an entry's header line in the wake, in bytes: its
// place, the child's addressee (cut to delegateAddresseeLogCap), its target
// (batchTargetCap) and the longest outcome, with room to spare. The share
// each child's text gets (batchShare) reserves it.
const batchHeaderMax = 288

// batchEntryNewlines is an entry's newlines: after the header, the opening
// fence, the text and the closing fence.
const batchEntryNewlines = 4

// batchWakeReserve is what a fan-out's wake spends, after its header, on
// everything but the entries: the label, the cards note, the question guide
// and the longer reply guide, each on its own line.
var batchWakeReserve = len(wakeBatchResultsLabel) + 1 + len(wakeCardsPendingNote) + 1 +
	len(wakeBatchQuestionGuide) + 1 + wakeReplyGuideMax + 1

// batchShare is the bytes of text each of n children gets in the wake: the
// single child's budget (lib.DelegateTextCap, for everything after the
// header) divided by n, less each entry's header and fences. So a fan-out's
// wake is no larger than one child's.
func batchShare(n int) int {
	return (lib.DelegateTextCap-batchWakeReserve)/max(n, 1) - batchHeaderMax - 2*wakeFenceMax - batchEntryNewlines
}

// cutBatchText is a child's text as its entry in a wake of n carries it:
// backtick runs broken (so its fence stays bounded), then cut on a rune
// boundary to its share, the cut named: a result's as posted whole in the
// conversation, a question's as posted nowhere (wakeBatchQuestionCutNote).
func cutBatchText(text string, n int, question bool) string {
	text = breakBacktickRuns(strings.TrimSpace(text), wakeFenceMax-1)
	share := batchShare(n)
	if len(text) <= share {
		return text
	}
	note := fmt.Sprintf(wakeBatchCutNote, n)
	if question {
		note = fmt.Sprintf(wakeBatchQuestionCutNote, n)
	}
	return truncateRunes(text, share-len("…")-len(note)) + note
}

// wakeBatchHeader is the header a fan-out's wake of n children opens with
// after the ask block.
func wakeBatchHeader(n int) string {
	return fmt.Sprintf(wakeBatchHeaderAsk, n)
}

// batchOutcome is how a child's end reads in the wake. A canceled the
// gateway didn't send is a failure, as a single child's is (wakeText).
func batchOutcome(end BatchEnd) string {
	switch end.State {
	case lib.StateCompleted:
		return batchOutcomeCompleted
	case lib.StateInputRequired:
		return batchOutcomeAsked
	case lib.StateRejected:
		return batchOutcomeRejected
	case lib.StateCanceled:
		if end.Stopped {
			return batchOutcomeStopped
		}
	}
	return batchOutcomeFailed
}

// batchChildName names a child in the wake and a status: its addressee, and
// its target in brackets when it has one.
func batchChildName(ref TaskRef) string {
	name := truncateRunes(ref.Addressee, delegateAddresseeLogCap)
	if ref.Target != "" {
		name += " (" + ref.Target + ")"
	}
	return name
}

// batchWakeText is a fan-out's wake prompt: the ask block when the
// delegating turn's request is on record, the header, then each child in
// batch order with its outcome and its text fenced (its result, its
// question, or its reason), already cut to its share (cutBatchText). A child
// that failed, timed out or was stopped is listed with that outcome, never
// left out. Then the cards note when a child handed work to cards, the
// question guide when one asked, and the reply guide, as wakeText closes.
func batchWakeText(ask string, kids []TaskRef, fullAnswer, cards bool) string {
	slices.SortStableFunc(kids, func(a, b TaskRef) int { return a.BatchIndex - b.BatchIndex })
	var lines []string
	if ask = strings.TrimSpace(ask); ask != "" {
		ask = capAsk(breakBacktickRuns(ask, wakeFenceMax-1))
		fence := wakeFence(ask)
		lines = append(lines, wakeAskLabel, fence, ask, fence, wakeBatchHeader(len(kids)))
	} else {
		lines = append(lines, fmt.Sprintf(wakeBatchHeaderBare, len(kids)))
	}
	lines = append(lines, wakeBatchResultsLabel)
	asked := false
	for i, k := range kids {
		end := BatchEnd{State: lib.StateFailed}
		if k.BatchEnd != nil {
			end = *k.BatchEnd
		}
		asked = asked || end.State == lib.StateInputRequired
		body := end.Text
		if strings.TrimSpace(body) == "" {
			body = wakeBatchNoText
		}
		fence := wakeFence(body)
		lines = append(lines, fmt.Sprintf(wakeBatchEntry, i+1, batchChildName(k), batchOutcome(end)), fence, body, fence)
	}
	if cards {
		lines = append(lines, wakeCardsPendingNote)
	}
	if asked {
		lines = append(lines, wakeBatchQuestionGuide)
	}
	if fullAnswer {
		lines = append(lines, wakeReplyGuideFull)
	} else {
		lines = append(lines, wakeReplyGuide)
	}
	return strings.Join(lines, "\n")
}

// batchChildEnded records a fan-out child's end on its entry, and when it is
// the last of its siblings to end, wakes the session once with all of them
// (wakeBatch). Called under the session lock from the relay's terminal, the
// heal, and a child's question (batchChildAsks); the caller writes the
// record. reason is the child's terminal reason as the observers would read
// it, for the chain's end when no wake runs.
//
// A child already recorded is left alone: its question ended it, and its
// later terminal (the cancel that question sent) changes nothing. A canceled
// child counts as ended like any other; whether it wakes is wakeBatch's. The
// chain ends once, at the wake or at the last child when none runs, never at
// the children before it.
func (g *Gateway) batchChildEnded(ctx context.Context, rec *SessionRecord, child TaskRef, end BatchEnd, reason string) {
	log := g.log.With("child", child.ID, "parent", child.ParentTaskID, "conversation", rec.Key)
	if child.BatchEnd != nil {
		log.Info("fan-out child's end was already recorded", "state", end.State, "recorded", child.BatchEnd.State)
		return
	}
	parent, known := rec.TaskRefFor(child.ParentTaskID)
	end.Stopped = child.Canceled
	end.Text = cutBatchText(end.Text, max(parent.BatchSize, len(parent.Children)), end.State == lib.StateInputRequired)
	rec.setBatchEnd(child.ID, &end)
	left := 0
	if known {
		left = rec.batchLeft(parent)
	}
	log.Info("fan-out child ended", "state", end.State, "left", left)
	if left > 0 {
		return
	}
	child.BatchEnd = &end
	woken, why, stopped := g.wakeBatch(ctx, rec, parent, child, log)
	if !woken {
		g.observeChainEnd(rec, child, stopped, end.State, end.Source, reason, why)
	}
}

// wakeBatch wakes the session once for a fan-out whose last child, last,
// has just ended. The parent's hold on the conversation ends here whatever
// follows, its rolling line kept on its entry when its own terminal hasn't
// closed it yet. No wake runs when the delegating turn was stopped (a stop
// while the batch ran canceled it), when the batch was withdrawn, or when
// every child was stopped: those report stopped, and the chain ends
// canceled. Otherwise it reports, as wakeSession does, whether the wake
// reached the bus and why not.
func (g *Gateway) wakeBatch(ctx context.Context, rec *SessionRecord, parent, last TaskRef, log *slog.Logger) (woken bool, why string, stopped bool) {
	detached := false
	if a := rec.ActiveTask; a != nil && a.TaskID == parent.ID {
		detached = a.Detached
		if a.StatusMsgID != "" {
			setParentLine(rec, parent.ID, a.StatusMsgID)
		}
		rec.ActiveTask = nil
	}
	kids := rec.batchChildren(parent.ID)
	allStopped := len(kids) > 0
	for _, k := range kids {
		allStopped = allStopped && k.BatchEnd != nil && k.BatchEnd.Stopped
	}
	switch {
	case parent.Canceled || detached:
		log.Info("no wake: the delegating turn was stopped")
		return false, "", true
	case parent.DelegationEnd != "":
		log.Info("no wake: the batch was withdrawn")
		return false, "", true
	case allStopped:
		log.Info("no wake: every delegated task was stopped")
		return false, "", true
	case rec.ActiveTask != nil:
		log.Info("no wake: another task holds the conversation", "active", rec.ActiveTask.TaskID)
		return false, "another task holds the conversation", false
	}
	parentRef, authority, why, ok := g.wakeParent(rec, last, log)
	if !ok {
		return false, why, false
	}
	full, cards := wakeToDoor(&parentRef), false
	var steered TaskRef
	steered.carrySteerAuthors(parentRef)
	for _, k := range kids {
		steered.carrySteerAuthors(k)
		if k.BatchEnd != nil {
			full = full || k.BatchEnd.Unposted
			cards = cards || k.BatchEnd.Cards
		}
	}
	text := batchWakeText(parentRef.Request, kids, full, cards)
	woken, why = g.startWake(ctx, rec, last, parentRef, authority, steered, text, log)
	return woken, why, false
}

// fanOutChild reports an entry that is a child of a batch of two or more,
// whose end goes to batchChildEnded rather than to a wake of its own.
func (rec *SessionRecord) fanOutChild(ref TaskRef) bool {
	if ref.Role != taskRoleChild || ref.BatchIndex == 0 {
		return false
	}
	parent, ok := rec.TaskRefFor(ref.ParentTaskID)
	return !ok || parent.fanOut()
}

// noticeBatchChildAsked closes the line of a fan-out child that asked for
// input: its question goes back to the session with the batch's results.
const noticeBatchChildAsked = "❓ needs input: the question goes back with the other results"

// batchChildAsks ends a fan-out child that reached input-required, with its
// question as its result (batchChildEnded), rather than pausing the
// conversation on it: several children each asking the room at once would be
// unreadable, so the session asks the person once, in its own words. The
// child is sent a cancel, quiet so its terminal posts nothing, under the
// delegation's authority as its mint was, and its line is closed. A cancel
// that can't be built or published leaves the child counted as ended all
// the same, and its route retired, so it holds neither the batch nor the
// conversation's one live delegation (liveChild); a late event of its is
// dropped. A single delegation, and a batch of one, keep the pause
// (applyStatus).
func (g *Gateway) batchChildAsks(ctx context.Context, rec *SessionRecord, child TaskRef, question string) {
	if child.BatchEnd != nil {
		return
	}
	if child.StatusMsgID != "" {
		g.editLine(rec.Key, child.StatusMsgID, noticeBatchChildAsked, "")
		setParentLine(rec, child.ID, "")
	}
	g.batchChildEnded(ctx, rec, child, BatchEnd{State: lib.StateInputRequired, Text: question, Source: TerminalFromExecutor}, "")
	// No answer is coming, so the task is told to stop rather than wait.
	g.publishDispatchStop(ctx, child)
	parent, _ := rec.TaskRefFor(child.ParentTaskID)
	authority, ok := delegationAuthority(parent)
	if !ok {
		g.log.Error("cancel not sent for "+batchCancelAsked+": the delegation's authority is not on record", "taskId", child.ID)
	} else if g.publishBatchCancel(ctx, rec, child, authority, batchCancelAsked) {
		rec.MarkCanceled(child.ID)
		rec.markQuietCancel(child.ID)
		return
	}
	g.retireTaskRoute(ctx, child.ID)
}

// noticeBatchChildNeverStarted is what the room reads when the heal ends a
// fan-out child that produced nothing past FirstEventGrace.
const noticeBatchChildNeverStarted = "⚠️ delegated task `%s` has produced nothing on its event stream in %s; it is counted as not started"

// healFanOut is healActiveTask's case for a fan-out parent holding the
// conversation while its batch runs: the parent's own end is the hand-off
// line, so it is not healed as a turn. Instead each child not yet ended is
// looked at once, as the heal looks at a single child: a terminal on the
// stream is the child's end, and a child with nothing past FirstEventGrace
// never started. Each goes to batchChildEnded, so the last wakes the session
// as the relay would have. The parent's line is closed when its terminal is
// on the stream. Writes the record when anything changed.
func (g *Gateway) healFanOut(ctx context.Context, rec *SessionRecord, active *ActiveTask, parent TaskRef) {
	changed := false
	if active.StatusMsgID != "" {
		if task, _, _, err := g.client.TasksGetTerminal(ctx, rec.AddresseeFor(parent.ID), parent.ID); err == nil && task.Final {
			g.editLine(rec.Key, active.StatusMsgID, withLineNote(terminalLine(task.State, ""), active.LineNote), "")
			active.StatusMsgID = ""
			changed = true
		}
	}
	for _, kid := range rec.batchChildren(parent.ID) {
		// A child's heal may have ended the batch, and the wake taken the
		// conversation: the rest are already recorded.
		if fresh, _ := rec.TaskRefFor(kid.ID); fresh.BatchEnd != nil {
			continue
		}
		changed = g.healBatchChild(ctx, rec, kid) || changed
	}
	if changed {
		if err := withRetry(kvRetryAttempts, func() error { return g.reg.Put(ctx, rec) }); err != nil {
			g.log.Error("healed record write failed", "conversation", rec.Key, "err", err)
		}
	}
}

// healBatchChild heals one fan-out child, as healActiveTask heals a single
// child: a terminal found on the stream posts the status card, is logged and
// marked, the child's route retired and its line closed, and its end
// recorded with its result unposted (the card carries none); a child with no
// first event past FirstEventGrace is ended as never started, with its stop
// record for a profile task. It reports whether it healed.
func (g *Gateway) healBatchChild(ctx context.Context, rec *SessionRecord, kid TaskRef) bool {
	task, terminalSubject, _, err := g.client.TasksGetTerminal(ctx, kid.Addressee, kid.ID)
	switch {
	case err == nil && task.Final:
		source := TerminalFromExecutor
		if terminalSubject == lib.TaskSupervisorSubject(kid.Addressee, kid.ID) {
			source = TerminalFromSupervisor
		}
		g.log.Info("healing a fan-out child whose terminal the relay never delivered", "conversation", rec.Key, "taskId", kid.ID, "state", task.State)
		g.post(rec.Key, formatTaskStatus(task, "", kid.StartedAt))
		g.logTaskTerminal(rec, kid.Addressee, kid.ID, task.State, source, finalMessageText(task))
		rec.MarkTerminalObserved(kid.ID)
		var terminal chatmsg.Reading
		if task.FinalMessage != nil {
			terminal = g.readChat(rec.Key, kid.ID, chatmsg.Terminal, task.FinalMessage.Parts)
		}
		g.terminalMark(rec, kid.ID, task.State, terminal)
		g.retireTaskRoute(ctx, kid.ID)
		if kid.StatusMsgID != "" {
			g.editLine(rec.Key, kid.StatusMsgID, terminalLine(task.State, ""), "")
			setParentLine(rec, kid.ID, "")
		}
		toCards := cardsPending(task.State, terminal) || handedOff(task.State, foldHandedOff(task))
		if toCards {
			rec.markCardsAnswer(kid.ID)
			if isConsoleConversation(rec.Key) && handedOff(task.State, foldHandedOff(task)) {
				rec.markCardsPending(kid.rootID())
			}
		}
		_, result, reason := healedChildOutcome(task)
		text := reason
		if task.State == lib.StateCompleted {
			text = result
		}
		g.batchChildEnded(ctx, rec, kid, BatchEnd{State: task.State, Text: text, Source: source, Unposted: true, Cards: toCards}, reason)
		return true
	case noFirstEventPastGrace(&ActiveTask{SubmittedAt: kid.StartedAt}, isTaskNotFound(err), g.cfg.FirstEventGrace, time.Now()):
		g.log.Info("healing a fan-out child with no first event inside the grace", "conversation", rec.Key, "taskId", kid.ID,
			"addressee", kid.Addressee, "grace", g.cfg.FirstEventGrace)
		g.post(rec.Key, fmt.Sprintf(noticeBatchChildNeverStarted, kid.ID, g.cfg.FirstEventGrace))
		g.logTaskTerminal(rec, kid.Addressee, kid.ID, lib.StateFailed, TerminalNeverStarted, "")
		g.publishDispatchStop(ctx, kid)
		g.retireTaskRoute(ctx, kid.ID)
		if kid.StatusMsgID != "" {
			g.editLine(rec.Key, kid.StatusMsgID, terminalLine(lib.StateFailed, ""), "")
			setParentLine(rec, kid.ID, "")
		}
		g.batchChildEnded(ctx, rec, kid, BatchEnd{State: lib.StateFailed, Text: childNeverStarted, Source: TerminalNeverStarted, Unposted: true}, childNeverStarted)
		return true
	}
	return false
}

// The status answer and the steer refusal while a fan-out runs.
const (
	// noticeFanOutStatusHead opens a status answer during a fan-out: how
	// many of the batch are still running.
	noticeFanOutStatusHead = "🔀 %d of %d delegated tasks are still running:"
	// noticeFanOutStatusLine is one running child: its name and state.
	noticeFanOutStatusLine = "• %s: %s"
	// noticeFanOutSteer refuses text while batch children run: the parent's
	// turn has ended, and which child it was meant for isn't the gateway's
	// to guess.
	noticeFanOutSteer = "⚠️ %d delegated tasks are running. Say stop to cancel them."
	// noticeFanOutSteerOne is noticeFanOutSteer for one.
	noticeFanOutSteerOne = "⚠️ 1 delegated task is running. Say stop to cancel it."
)

// The state words a status gives a running child.
const (
	fanOutStateWaiting = "waiting to start"
	fanOutStateUnknown = "state unknown (the stream could not be read)"
)

// answerFanOutStatus answers a status question during a fan-out from the
// tree: each child still running, by its addressee and target, with the
// state its stream shows (a child with no events yet is waiting to start,
// usually queued behind the profile's concurrency). Children that ended are
// counted out; their results are already in the conversation.
func (g *Gateway) answerFanOutStatus(ctx context.Context, rec *SessionRecord, parent TaskRef) {
	kids := rec.batchChildren(parent.ID)
	slices.SortStableFunc(kids, func(a, b TaskRef) int { return a.BatchIndex - b.BatchIndex })
	lines := []string{fmt.Sprintf(noticeFanOutStatusHead, rec.batchLeft(parent), parent.BatchSize)}
	for _, k := range kids {
		if k.BatchEnd != nil {
			continue
		}
		state := fanOutStateUnknown
		task, err := g.client.TasksGet(ctx, k.Addressee, k.ID)
		switch {
		case err == nil:
			state = string(task.State)
		case isTaskNotFound(err):
			state = fanOutStateWaiting
		default:
			g.log.Error("status replay failed for a fan-out child", "taskId", k.ID, "err", err)
		}
		lines = append(lines, fmt.Sprintf(noticeFanOutStatusLine, batchChildName(k), state))
	}
	g.post(rec.Key, strings.Join(lines, "\n"))
}

// refuseFanOutSteer answers text that would steer the turn while its batch
// runs: nothing is sent, and the notice counts the children still running.
func (g *Gateway) refuseFanOutSteer(rec *SessionRecord, parent TaskRef) {
	left := rec.batchLeft(parent)
	g.log.Info("steer refused: a fan-out is running", "conversation", rec.Key, "taskId", parent.ID, "running", left)
	if left == 1 {
		g.post(rec.Key, noticeFanOutSteerOne)
		return
	}
	g.post(rec.Key, fmt.Sprintf(noticeFanOutSteer, left))
}
