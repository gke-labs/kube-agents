package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The rules a delegation is refused or ignored under, as the audit line
// names them (phase-2 delegation spec §8).
const (
	ruleDelegationAllowedUsers  = "delegation.allowed-users"
	ruleDelegationTarget        = "delegation.target"
	ruleDelegationSteerAuthor   = "delegation.steer-author"
	ruleDelegationSessionAuthor = "delegation.session-author"
	ruleDelegationBusy          = "delegation.busy"
	ruleDelegationDepth         = "delegation.depth"
	ruleDelegationNoRequester   = "delegation.no-requester"
	ruleDelegationStale         = "delegation.stale"
	ruleDelegationMalformed     = "delegation.malformed"
	ruleDelegationDoorUnlisted  = "delegation.door-unlisted"
)

// reasonWakeNotStarted is the reason token on a delegation chain's root
// terminal when the child ended and no wake could run (observeChildEnd).
const reasonWakeNotStarted = "wake-not-started"

// delegatedLineNote suffixes a child's rolling line, so the room can tell the
// task the session handed on from one a human asked for.
const delegatedLineNote = "(delegated to platform)"

// requesterGone is the one spelling of "the turn's requester has aged out of
// the record" (AskTTL clears it), for the delegation refusal and the wake
// that cannot run for the same reason.
const requesterGone = "requester is no longer on record"

const (
	noticeDelegationNoRequester       = "⚠️ delegation refused: this turn's " + requesterGone + "; ask again"
	noticeDelegationNotAllowed        = "🚫 not allowed to reach " + targetPlatform + " from here"
	noticeDelegationSessionIncomplete = "⚠️ delegation refused: this session's authors can no longer all be checked; ask again in a new turn"
	noticeDelegationSteerOverflow     = "⚠️ delegation refused: more people steered this turn than can be checked; ask again"
	noticeWakeNoRequester             = "ℹ️ the delegated task finished, but the delegating turn's " + requesterGone + "; the session was not woken"
)

// handleDelegateRequest is the gateway's side of the delegation primitive: a
// session turn's lib.ArtifactDelegate, accepted, checked, and minted as a
// child task to the platform agent. Called from the relay under the session
// lock with the record the relay writes back.
//
// The cases that cannot be a live request from the conversation's own session
// (a straggler, a repeat, a malformed part) are ignored: logged at warning,
// nothing posted, nothing minted. The cases that are a real request this
// gateway will not serve are refused: logged likewise, and the conversation
// told, naming the target and never the requester.
func (g *Gateway) handleDelegateRequest(ctx context.Context, rec *SessionRecord, subject, taskID string, parts []lib.Part) {
	session, _, _, _ := lib.ParseTaskSubject(subject)
	parent, known := rec.TaskRefFor(taskID)
	var req lib.DelegateRequest
	parsed := len(parts) == 1 && parts[0].Kind == "data" && json.Unmarshal(parts[0].Data, &req) == nil
	addressee := strings.TrimSpace(req.Addressee)

	log := g.log.With("task", taskID, "session", session, "conversation", rec.Key)
	log.Info("delegation requested", "addressee", addressee, "depth", parent.Depth)
	// Every refusal and ignore line carries the rule, the backend and the
	// requester as the record stores it: already hashed.
	audit := []any{"addressee", addressee, "depth", parent.Depth}
	if parent.Requester != nil {
		audit = append(audit, "backend", parent.Requester.Backend, "requester", parent.Requester.Subject)
	}
	ignore := func(rule string, extra ...any) {
		log.Warn("delegation ignored", append(append([]any{"rule", rule}, audit...), extra...)...)
	}
	// A refusal's notice waits for the delegating turn's terminal, so the
	// room reads the session's "delegated to platform" first and the
	// refusal after it (spec §3).
	refuse := func(rule, notice string, extra ...any) {
		log.Warn("delegation refused", append(append([]any{"rule", rule}, audit...), extra...)...)
		g.deferNotice(taskID, notice)
	}

	// Only the task the gateway started, from the incarnation that owns it
	// now: the subject's session is the record's bus session and the task
	// was addressed to it. That is a session-routed turn or a delegate:
	// incarnation; a platform task, or a pod a later turn retired, is not.
	if !known || rec.BusSession == "" || session != rec.BusSession || parent.Addressee != rec.BusSession {
		ignore(ruleDelegationStale, "busSession", rec.BusSession)
		return
	}
	// One child at a time (decision 3), and a request is accepted once per
	// task. A slice, so fan-out is one condition here later. Checked before
	// the active task, so a repeat from the parent logs as what it is.
	if len(parent.Children) > 0 {
		ignore(ruleDelegationBusy, "child", parent.Children[len(parent.Children)-1])
		return
	}
	if active := rec.ActiveTask; active == nil || active.TaskID != taskID {
		ignore(ruleDelegationStale)
		return
	}
	// The adapter holds the same cap and refuses blank text; a request that
	// breaks either reached the bus some other way.
	if !parsed || strings.TrimSpace(req.Text) == "" || len(req.Text) > lib.DelegateTextCap {
		ignore(ruleDelegationMalformed, "textBytes", len(req.Text))
		return
	}

	if addressee != targetPlatform {
		refuse(ruleDelegationTarget, "⚠️ delegation refused: only platform can be delegated to today")
		return
	}
	// One live child per conversation (decision 3), whichever turn asked
	// for it: a stop detaches a child without ending it.
	if live := g.liveChild(ctx, rec); live != "" {
		refuse(ruleDelegationBusy, fmt.Sprintf("⚠️ delegation refused: a delegated task is still running (task %s)", live), "child", live)
		return
	}
	if parent.Depth >= g.cfg.DelegationDepthMax {
		refuse(ruleDelegationDepth, fmt.Sprintf("⚠️ delegation refused: this conversation has delegated as deep as it may (%d)", g.cfg.DelegationDepthMax))
		return
	}
	authority, err := AuthorityFromAttribution(parent.Attribution)
	if parent.Requester == nil || len(parent.Attribution) == 0 || err != nil {
		refuse(ruleDelegationNoRequester, noticeDelegationNoRequester)
		return
	}
	// The A2A door's callers delegate only under a list of their own,
	// which nothing renders yet (gke-labs#2478): an absent list there is
	// nobody, where on a chat backend it is everyone the ingress admits.
	if g.doorUnlisted(addressee, parent.Requester.Backend) {
		refuse(ruleDelegationDoorUnlisted, noticeDelegationNotAllowed)
		return
	}
	if !g.targetAllows(addressee, parent.Requester.Backend, parent.Requester.Subject) {
		refuse(ruleDelegationAllowedUsers, noticeDelegationNotAllowed)
		return
	}
	// Anyone in the room can steer the turn, so everyone who did is checked
	// as the requester is: otherwise an off-list human steers the session
	// into delegating under the requester's name.
	if parent.SteerAuthorsOverflow {
		refuse(ruleDelegationSteerAuthor, noticeDelegationSteerOverflow, "steerAuthors", "over-cap")
		return
	}
	for _, a := range parent.SteerAuthors {
		if rule := g.authorRefusal(addressee, a, ruleDelegationSteerAuthor); rule != "" {
			refuse(rule, noticeDelegationNotAllowed, "steerBackend", a.Backend, "steerAuthor", a.Subject)
			return
		}
	}
	// And everyone whose text reached this incarnation, on any turn: the
	// pod keeps what it was told, so an off-list human's earlier turn can
	// leave the instruction a later on-list turn delegates on. The check
	// above pinned the parent to rec.BusSession, so the set is the parent's
	// incarnation's.
	sessionAuthors, sessionUnknown, _ := rec.sessionAuthorsOf()
	if sessionUnknown {
		refuse(ruleDelegationSessionAuthor, noticeDelegationSessionIncomplete, "sessionAuthors", "incomplete")
		return
	}
	for _, a := range sessionAuthors {
		if rule := g.authorRefusal(addressee, a, ruleDelegationSessionAuthor); rule != "" {
			refuse(rule, noticeDelegationNotAllowed, "sessionBackend", a.Backend, "sessionAuthor", a.Subject)
			return
		}
	}
	authority.Via = &AuthorityVia{TaskID: taskID, Session: rec.BusSession}

	// The child is a fixed-route task: addressed to platform so steers and a
	// stop reach it, with BusSession left in place for the wake turn.
	// The correlationId is the parent's (the library's child-envelope rule),
	// read off the active task, which is the parent and always carries one.
	prevAddressee, prevActive := rec.Addressee, rec.ActiveTask
	// The parent's rolling line lives on ActiveTask, which the child is
	// about to take; kept on the parent's entry, it still closes on the
	// parent's terminal (relayTerminal).
	setParentLine(rec, taskID, prevActive.StatusMsgID)
	rec.Addressee = addressee
	childID, ok := g.startTaskWith(ctx, rec, taskStart{
		Text:                 req.Text,
		Requester:            *parent.Requester,
		Authority:            authority,
		CorrelationID:        prevActive.CorrelationID,
		Role:                 taskRoleChild,
		ParentTaskID:         taskID,
		Depth:                parent.Depth + 1,
		LineNote:             delegatedLineNote,
		SteerAuthors:         parent.SteerAuthors,
		SteerAuthorsOverflow: parent.SteerAuthorsOverflow,
		RootTaskID:           parent.rootID(),
		// The parent's link goes into the same record write as the child's
		// entry: written after, a restart or a failed relay write in
		// between would leave the child on record with a parent that looks
		// like it never delegated.
		LinkParent: true,
	})
	if !ok {
		// startTaskWith has said why, in the log and to the room; the
		// parent is still the conversation's task, and a child that never
		// reached the bus leaves no entry in the chain, no link on the
		// parent and no route.
		rec.Addressee, rec.ActiveTask = prevAddressee, prevActive
		setParentLine(rec, taskID, "")
		g.dropFailedChildren(ctx, rec, taskID)
		return
	}
	log.Info("delegation minted", "parent", taskID, "child", childID, "addressee", addressee)
}

// doorUnlisted reports a delegation to target checked against someone who
// came in through the A2A door when no list for that backend exists. The
// door is the exception to targetAllows's absent-list rule: a chat backend's
// ingress allowlist is a list of people an operator chose, while the door's
// principal map admits programs, so the door delegates only under a list of
// its own (spec-chatops-gateway.md, "Sessions by default"; the CR field that
// would render one is gke-labs#2478).
//
// The inject door (backend "inject") is deliberately not included, and is
// not an oversight to fix: it exists only where an operator arms it on a dev
// or eval install, its principal map already names who may use it, and the
// live runs and evals exercise delegation through it. Its absent list stays
// everyone its map admits, as a chat backend's does.
func (g *Gateway) doorUnlisted(target, backend string) bool {
	return backend == a2aBackend && g.targetAllowed[target][backend] == nil
}

// authorRefusal is the rule a steer author or incarnation-set member refuses
// a delegation to target under, or "" when they pass: the door's rule first,
// then the target's list under the caller's own rule.
func (g *Gateway) authorRefusal(target string, a TaskRequester, rule string) string {
	if g.doorUnlisted(target, a.Backend) {
		return ruleDelegationDoorUnlisted
	}
	if !g.targetAllows(target, a.Backend, a.Subject) {
		return rule
	}
	return ""
}

// wakeTruncatedNote follows the "…" truncateRunes leaves on a wake body cut
// at the cap.
const wakeTruncatedNote = " (truncated; the full result is in the conversation)"

// wakeFenceMax bounds the fence around the child's result in the wake text.
// The fence is one backtick longer than the longest backtick run in the body
// (at least three), so the body goes in verbatim and no line of it can close
// the block early: a CommonMark closing fence must be at least as long as
// the opening one. A body with a run of wakeFenceMax or more backticks has
// each such run broken by a zero-width space before fencing, which keeps the
// fence, and with it the overhead the cap reserves, bounded.
const wakeFenceMax = 16

// wakeResultLabel is the line between the wake's header and the fenced
// result: the text below is the addressee's output, not the user's ask.
const wakeResultLabel = "Result from " + targetPlatform + " (not from the user):"

// wakeAskLabel opens the wake's text when the delegating turn's request is
// on record: the wake's pod starts with no memory, so without it the session
// reads a result to a question it never saw.
const wakeAskLabel = "You were asked:"

// wakeAskCap bounds the request copy on a history entry (TaskRef.Request), in
// bytes, marker included. Smaller than lib.DelegateTextCap on purpose: every
// human turn stores one, the record holds up to taskHistoryCap entries, and
// 16 KiB each would put a full history near the KV's message-size ceiling,
// where 1 KiB keeps it at 50 KiB. A question longer than that is rare, and
// the wake needs the question, not every word of it.
const wakeAskCap = 1024

// wakeAskTruncatedNote follows the "…" truncateRunes leaves on an ask cut at
// wakeAskCap.
const wakeAskTruncatedNote = " (truncated)"

// capAsk is text held to wakeAskCap bytes, cut on a rune boundary and marked.
func capAsk(text string) string {
	if len(text) <= wakeAskCap {
		return text
	}
	return truncateRunes(text, wakeAskCap-len("…")-len(wakeAskTruncatedNote)) + wakeAskTruncatedNote
}

// wakeText is the wake turn's prompt (spec §4). With the delegating turn's
// request on record it opens with the label and the request fenced - the
// human's text, untrusted like the result, so it is fenced the same way and
// no line of it can close the block and pass for the gateway's own - then
// the header naming the child and its outcome; with none (a legacy entry) the
// header alone opens it. Then, when there is a result or reason, the label
// and the body fenced. Everything after the header line is held to
// lib.DelegateTextCap bytes, label and fences included; the body is cut on a
// rune boundary and marked. The request is held to wakeAskCap before the
// fence, so the whole text is bounded by the two caps and their fences. The
// relay has already posted the whole result to the conversation.
func wakeText(state lib.TaskState, childID, ask, result, reason string) string {
	outcome, body := "completed", result
	switch state {
	case lib.StateFailed, lib.StateCanceled: // a canceled the gateway did not publish
		outcome, body = "failed", reason
	case lib.StateRejected:
		outcome, body = "was rejected", reason
	}
	var text string
	if ask = strings.TrimSpace(ask); ask != "" {
		ask = breakBacktickRuns(capAsk(ask), wakeFenceMax-1)
		fence := wakeFence(ask)
		text = wakeAskLabel + "\n" + fence + "\n" + ask + "\n" + fence + "\n" +
			fmt.Sprintf("You delegated to %s (task %s), which %s.", targetPlatform, childID, outcome)
	} else {
		text = fmt.Sprintf("The task you delegated to %s (task %s) %s.", targetPlatform, childID, outcome)
	}
	if body = strings.TrimSpace(body); body != "" {
		text += "\n" + fenceWakeBody(body)
	}
	return text
}

// fenceWakeBody is the label, the fence, the capped body and the fence.
func fenceWakeBody(body string) string {
	body = breakBacktickRuns(body, wakeFenceMax-1)
	fence := wakeFence(body)
	// label \n fence \n body \n fence
	budget := lib.DelegateTextCap - len(wakeResultLabel) - 2*len(fence) - 3
	if len(body) > budget {
		body = truncateRunes(body, budget-len("…")-len(wakeTruncatedNote)) + wakeTruncatedNote
		fence = wakeFence(body) // a prefix has no longer run: it can only shrink
	}
	return wakeResultLabel + "\n" + fence + "\n" + body + "\n" + fence
}

// wakeFence is a backtick fence one longer than body's longest run, at least
// three.
func wakeFence(body string) string {
	longest, run := 0, 0
	for i := 0; i < len(body); i++ {
		if body[i] == '`' {
			run++
			longest = max(longest, run)
		} else {
			run = 0
		}
	}
	return strings.Repeat("`", max(3, longest+1))
}

// breakBacktickRuns inserts a zero-width space after every n consecutive
// backticks, so no run in the body is longer than n.
func breakBacktickRuns(body string, n int) string {
	if !strings.Contains(body, strings.Repeat("`", n+1)) {
		return body
	}
	var b strings.Builder
	run := 0
	for _, r := range body {
		if r == '`' {
			if run == n {
				b.WriteString("​")
				run = 0
			}
			run++
		} else {
			run = 0
		}
		b.WriteRune(r)
	}
	return b.String()
}

// liveChild names a child task of this conversation that has not ended: a
// child entry whose task the gateway still routes. The task index is the
// liveness the relay itself keeps; relayTerminal retires it on the child's
// terminal, from the executor or the supervisor, and healActiveTask on the
// heal that releases one.
//
// A lookup that errors cannot rule the child out, so it counts as live: a
// refused request can be asked again, a second live child cannot be undone.
func (g *Gateway) liveChild(ctx context.Context, rec *SessionRecord) string {
	for _, ref := range rec.Tasks {
		if ref.Role != taskRoleChild {
			continue
		}
		key, err := g.lookupTaskSession(ctx, ref.ID)
		if err != nil {
			g.log.Error("task index lookup failed; counting the child as live", "taskId", ref.ID, "conversation", rec.Key, "err", err)
			return ref.ID
		}
		if key != "" {
			return ref.ID
		}
	}
	return ""
}

// setParentLine records (or, given "", clears) the delegating turn's
// rolling-line message on its history entry.
func setParentLine(rec *SessionRecord, taskID, statusMsgID string) {
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == taskID {
			rec.Tasks[i].StatusMsgID = statusMsgID
		}
	}
}

// dropFailedChildren removes the entry, the index and the parent's link of a
// child of taskID whose submission never reached the bus. The parent had no
// child on record before (the busy check), so any child entry naming it is
// the failed one, and so is every link it holds.
func (g *Gateway) dropFailedChildren(ctx context.Context, rec *SessionRecord, taskID string) {
	kept := rec.Tasks[:0]
	for _, ref := range rec.Tasks {
		if ref.Role != taskRoleChild || ref.ParentTaskID != taskID {
			kept = append(kept, ref)
			continue
		}
		g.retireTaskRoute(ctx, ref.ID)
	}
	rec.Tasks = kept
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == taskID {
			rec.Tasks[i].Children = nil
		}
	}
}

// dropFailedWake removes the entry and the index of a wake of childID whose
// submission never reached the bus. A child starts one wake at most, so any
// wake entry naming it is the failed one.
func (g *Gateway) dropFailedWake(ctx context.Context, rec *SessionRecord, childID string) {
	kept := rec.Tasks[:0]
	for _, ref := range rec.Tasks {
		if ref.Role != taskRoleWake || ref.ParentTaskID != childID {
			kept = append(kept, ref)
			continue
		}
		g.retireTaskRoute(ctx, ref.ID)
	}
	rec.Tasks = kept
}

// deferNotice holds a notice for the task until its terminal relays
// (flushNotices). Render state is cache, so a gateway restart in between
// loses it; the audit line is the record.
func (g *Gateway) deferNotice(taskID, notice string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	rs, ok := g.relays[taskID]
	if !ok {
		rs = &relayState{}
		g.relays[taskID] = rs
	}
	rs.notices = append(rs.notices, notice)
}

// flushNotices posts the notices held for a task, once.
func (g *Gateway) flushNotices(conversation string, rs *relayState) {
	g.mu.Lock()
	notices := rs.notices
	rs.notices = nil
	g.mu.Unlock()
	for _, n := range notices {
		g.post(conversation, n)
	}
}

// wakeSession starts the session's next turn on a delegated child's terminal
// (spec §4): a fresh incarnation and the ordinary spawn, as a human turn
// gets, with gateway-authored text carrying the outcome and the delegating
// turn's stored attribution, so the wake runs under the requester who asked.
// Called under the session lock, after the child's result or failure is
// posted and its ActiveTask released: from relayTerminal on the child's
// relayed terminal, and from healActiveTask on a terminal the heal found on
// the stream. The caller writes the record back.
//
// It reports whether the wake reached the bus, and when it did not, why, in
// a phrase for the chain root's terminal reason (observeChildEnd). A stop
// the requester asked for reports false and no reason.
func (g *Gateway) wakeSession(ctx context.Context, rec *SessionRecord, child TaskRef, state lib.TaskState, result, reason string) (bool, string) {
	log := g.log.With("child", child.ID, "conversation", rec.Key, "state", string(state))
	// The gateway published a cancel for the child: the human said stop,
	// and whatever the executor answered with, waking the session would act
	// against it. A canceled nobody asked for (a supervisor's) is a failure
	// below, and wakes.
	if child.Canceled {
		log.Info("no wake: the child was stopped by its requester")
		return false, ""
	}
	// A later turn holds the conversation (the child was stopped and a human
	// moved on before its end arrived). A fresh incarnation now would retire
	// that turn's pod.
	if rec.ActiveTask != nil {
		log.Info("no wake: another task holds the conversation", "active", rec.ActiveTask.TaskID)
		return false, "another task holds the conversation"
	}
	if g.spawner == nil {
		log.Warn("no wake: no session spawner")
		return false, "no session spawner"
	}
	parent, ok := rec.TaskRefFor(child.ParentTaskID)
	authority, err := AuthorityFromAttribution(parent.Attribution)
	if !ok || parent.Requester == nil || len(parent.Attribution) == 0 || err != nil {
		log.Warn("no wake: the delegating turn's requester is not on record", "parent", child.ParentTaskID)
		g.post(rec.Key, noticeWakeNoRequester)
		return false, "the delegating turn's " + requesterGone
	}
	// The session that delegated: the parent's addressee is the incarnation
	// it ran on, which the mint checked was the record's bus session.
	authority.Via = &AuthorityVia{TaskID: child.ID, Session: parent.Addressee}

	// The delegating turn's request, carried down a longer chain: the
	// human's question, not an intermediate wake's gateway-authored text.
	text := wakeText(state, child.ID, parent.Request, result, reason)

	if rec.Profile == "" {
		rec.Profile = sessionProfile
	}
	// The wake's pod starts with no memory of the parent's (one task per
	// pod; the rehydration primer has no reader yet), but its text carries
	// the child's result, and the child's ask was written by the parent's
	// incarnation. So the wake's incarnation starts from the parent's set,
	// taken before freshIncarnation rotates it away.
	inherited, inheritedUnknown, inheritedSince := rec.sessionAuthorsOf()
	// The cap holds and the previous pod is retired here; a refusal has
	// posted the standard notice, and the child's result stands as relayed.
	if !g.freshIncarnation(ctx, rec) {
		log.Info("no wake: the session could not be started")
		return false, "the session could not be started"
	}
	rec.seedSessionAuthors(inherited, inheritedUnknown, inheritedSince)
	// The wake is the delegating turn's successor: the chain's correlation
	// id (a task spawned in service of another inherits it) and the child's
	// depth, so depth counts delegations rather than turns.
	// The wake reads the child's result, which steers into the child
	// shaped, and continues the parent's turn: it carries both entries'
	// steer authors, so a delegation from it is checked against them.
	var steered TaskRef
	steered.carrySteerAuthors(parent)
	steered.carrySteerAuthors(child)
	wakeID, ok := g.startTaskWith(ctx, rec, taskStart{
		Text:                 text,
		Requester:            *parent.Requester,
		Authority:            authority,
		CorrelationID:        child.CorrelationID,
		Role:                 taskRoleWake,
		ParentTaskID:         child.ID,
		Depth:                child.Depth,
		SteerAuthors:         steered.SteerAuthors,
		SteerAuthorsOverflow: steered.SteerAuthorsOverflow,
		RootTaskID:           child.rootID(),
		Request:              parent.Request,
	})
	if !ok {
		// startTaskWith has said why. A wake that never reached the bus
		// leaves no entry and no route, as a failed child does
		// (dropFailedChildren): otherwise a read of the settled chain
		// follows the child to a wake with nothing on its stream, not to
		// the end observeChildEnd records on the child.
		g.dropFailedWake(ctx, rec, child.ID)
		return false, "the wake could not be started"
	}
	log.Info("session woken", "wake", wakeID, "session", rec.BusSession)
	return true, ""
}

// observeChildEnd announces a delegation chain's root terminal when its
// child ended and no wake runs: the one end the observers are owed, since the
// turn that delegated and the child both ended quietly (observedAs). A stop
// the requester asked for is the root canceled; otherwise the root failed,
// with the child's terminal source and a reason token saying the session was
// not woken and why. Nothing is delivered: the child's result is the wake's
// to digest, and it was posted to the conversation.
func (g *Gateway) observeChildEnd(rec *SessionRecord, child TaskRef, state lib.TaskState, source TerminalSource, reason, why string) {
	end := ChainEnd{State: lib.StateCanceled, Source: source, Reason: reason}
	if !child.Canceled {
		end = ChainEnd{State: lib.StateFailed, Source: source,
			Reason: fmt.Sprintf("reason: %s - %s; the delegated task %s ended %s", reasonWakeNotStarted, why, child.ID, state)}
	}
	// On the child's entry, so a read of the settled chain reports the same
	// end (probeConversation); the caller writes the record.
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == child.ID {
			rec.Tasks[i].ChainEnd = &end
		}
	}
	g.observeTaskTerminal(rec.Key, child.rootID(), end.State, end.Source, end.Reason)
}
