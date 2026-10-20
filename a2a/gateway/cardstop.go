package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The card stop (gke-labs#3263). A `stop` cancels the conversation's active
// task, and under next that task is one front-door Hermes turn, which has
// usually ended seconds after it filed a kanban card. What still runs is the
// card, which the gateway can neither see nor reach. So, armed (Config.CardStop),
// a stop also sends the default addressee a control: a task whose message is
// the cardStop data part alone (lib.CardStopPart), which the hermes-bridge
// answers with no Hermes turn by having session-kv archive every open card
// in the conversation, and their descendants (a2a/hermes-bridge/cardstop.go).
// The gateway only sends the control and words the reply; the executor does
// the stopping and says what it stopped, in the cardStop result part on the
// control's result artifact.
//
// The control is not a turn. It is never ActiveTask and never in Tasks
// (SessionRecord.CardStop), so it steers nothing, holds no serialization, is
// no status ask's subject, and is not counted as a task the conversation
// ran, by the busy count, the reap or anything else that reads Tasks. A
// person's next message while it runs is an ordinary turn, which the bridge
// queues behind the control on the session's turn.
//
// Who can send one: only the gateway publishes on a2a.tasks.*.*.in, and it
// builds every other payload from text (messagePayload), so no one can type
// the control. Where it goes: the default addressee only, on a conversation
// not on the session route, because only the bridge answers it and only the
// bridge's session holds the conversation's cards (cardStopWanted).
//
// It is answered or given up, never claimed: a rejected control (a bridge
// older than the part answers no-text-parts), a failed or canceled one, one
// that completes with no cardStop part (the bus refused the part beside the
// text), and one past Config.CardStopTimeout all answer
// cardStopUnconfirmedNotice, never a success.
//
// One control at a time. A stop that arrives while one is in flight marks it
// (CardStopRun.Again): the turn that stop cancelled, or one started since the
// control went out, may have filed cards the control has already passed by.
// When the control ends, however it ends, one follow-up control starts
// (followUpCardStop). It is quiet: no placeholder, and it posts only when it
// stopped a card or could not finish. One mark is held, however many stops
// arrive.

// The posts the card stop makes beside its reply (cardStopReply).
const (
	// cardStopPlaceholder is the control's line until its answer replaces it.
	cardStopPlaceholder = "🛑 stopping this conversation's cards…"
	// cardStopAlreadyNotice answers a stop while a control is in flight.
	cardStopAlreadyNotice = "🛑 already stopping this conversation's cards — the answer comes when the executor confirms"
	// cardStopUnconfirmedNotice is what a control that got no usable answer
	// posts, on an install where the card stop is armed. It says what is
	// known: the stop may or may not have reached the cards.
	// nothingRunningCardNotice, which says stopping a card is not supported,
	// is the unarmed reply only.
	cardStopUnconfirmedNotice = "⚠️ couldn't confirm that this conversation's cards stopped; if the agent filed a card for it, it may still be working"
	// cardStopSkippedNotice is what a marked control posts when it ends with
	// a newer turn running: the follow-up would stop that turn's cards too,
	// which nobody asked for, so it is not sent.
	cardStopSkippedNotice = "⚠️ cards filed since the last stop weren't stopped, because a newer question is running; say stop again to stop them"
	// cardStopOffNotice is the same skip when the card stop was turned off
	// while the control ran (a restart with A2A_CARD_STOP off).
	cardStopOffNotice = "⚠️ cards filed since the last stop weren't stopped, because stopping cards from chat has been turned off"
	// cardStopMovedNotice is the same skip when the conversation moved to a
	// session worker while the control ran, where stop doesn't reach cards.
	cardStopMovedNotice = "⚠️ cards filed since the last stop weren't stopped, because this conversation now goes to a session worker"
	// cardStopNoCardNotice is a stop's answer when it found no open card
	// but a newer question is running, which "nothing is running" would
	// deny (cardStopReply).
	cardStopNoCardNotice = "🤷 no card was open in this conversation"
	// cardStopDoorNotice is the A2A door's stand-in for the placeholder
	// (cardStopPlaceholderOn).
	cardStopDoorNotice = "🛑 sent a stop to this conversation's cards; this door doesn't report how it went"
)

// The reply's bounds: at most cardStopNamed titles per list, each at most
// cardStopTitleRunes runes with its "…".
const (
	cardStopNamed      = 5
	cardStopTitleRunes = 80
)

// cardStopRetiredKept is how many ended controls a record remembers
// (SessionRecord.RetiredCardStops): a control and the follow-ups a repeated
// stop chains behind it, with room to spare.
const cardStopRetiredKept = 4

// The outcomes the "card stop ended" log line names, one per way a control
// ends. INSTALL.md's troubleshooting quotes them.
const (
	cardStopOutcomeAnswered   = "answered"    // completed with a cardStop result
	cardStopOutcomeNoResult   = "no-result"   // completed with no cardStop part
	cardStopOutcomeExecutor   = "not-stopped" // the executor ended it rejected, failed or canceled
	cardStopOutcomeTimedOut   = "timed-out"   // no terminal inside the bound
	cardStopOutcomeExpired    = "expired"     // past the bound with no timer: it started before a restart
	cardStopOutcomeNotStarted = "not-started" // never reached the bus
)

// CardStopRun is the card-stop control in flight on a conversation
// (SessionRecord.CardStop): what its terminal, its timeout and a cancel of it
// need, kept on the record so a gateway restart still answers it.
type CardStopRun struct {
	TaskID        string          `json:"taskId"`
	CorrelationID string          `json:"correlationId"`
	Addressee     string          `json:"addressee"`
	Capability    *capability.Ref `json:"capability,omitempty"`
	// StatusMsgID is the placeholder's message, which the answer edits; empty
	// when there is none (a quiet follow-up, an A2A door caller, a failed
	// post), and the answer posts instead.
	StatusMsgID string    `json:"statusMsgId,omitempty"`
	StartedAt   time.Time `json:"startedAt"`
	// Quiet marks a follow-up (followUpCardStop): it posts only a stop that
	// stopped something, and a failure.
	Quiet bool `json:"quiet,omitempty"`
	// Promised is set when a stop told "the answer comes"
	// (cardStopAlreadyNotice) marked this control, so it answers in full
	// even when Quiet.
	Promised bool `json:"promised,omitempty"`
	// Again is set by a stop that arrived while this control was in flight:
	// that stop's authority, which the follow-up carries. Nil when none did.
	Again *Authority `json:"again,omitempty"`
}

// cardStopWanted reports whether this stop sends a card-stop control: armed,
// a stop that names no task (a program's cancel of a named task stays a
// cancel of that task alone), not one that roots its own conversation (it has
// no cards), and on a conversation whose addressee is the default one, the
// bridge. Never on the session route: a session pod answers no control.
func (g *Gateway) cardStopWanted(rec *SessionRecord, msg InboundMessage) bool {
	return g.cfg.CardStop && msg.TaskID == "" && !msg.TopLevel && g.cardStopRoute(rec)
}

// cardStopRoute reports whether rec's tasks go where a control is answered:
// the default addressee, off the session route.
func (g *Gateway) cardStopRoute(rec *SessionRecord) bool {
	return g.cfg.DefaultAddressee != RouteSession && !rec.SessionRouted &&
		rec.Addressee == g.cfg.DefaultAddressee
}

// markCardStopAgain records a stop that arrived while rec's control is in
// flight, for followUpCardStop. The latest stop's authority is kept; there is
// one mark however many arrive.
func markCardStopAgain(rec *SessionRecord, authority Authority) {
	a := authority
	rec.CardStop.Again = &a
}

// cardStopPayload is the control's message: the cardStop part in its request
// form, and no text.
func cardStopPayload(taskID, contextID string) ([]byte, error) {
	part, err := lib.CardStopPart(lib.CardStop{})
	if err != nil {
		return nil, err
	}
	return messagePayloadParts([]lib.Part{part}, taskID, contextID)
}

// cardStopPlaceholderOn reports whether the control posts its placeholder on
// backend. Not on the A2A door: its Edit addresses only a task's line, so the
// answer could never replace the placeholder there and the caller would read
// "stopping…" for good. The door answers a turn with the posts the turn
// made, and the answer comes after the turn, with no task to carry it, so
// there it posts cardStopDoorNotice instead, which promises no answer. The
// answer is still posted when it comes; the door shows it only on a task
// running by then.
func cardStopPlaceholderOn(backend string) bool {
	return backend != a2aBackend && backend != a2aGoogleBackend
}

// stopCards starts a card-stop control on rec's addressee and records it on
// rec (CardStop); the caller writes the record. backend is the door the stop
// came through ("" for a follow-up). The control is registered with the relay
// before it is published, as startTaskWith registers a task. It is no task a
// caller submitted, so the observers are told only that the stop reached the
// bus (CancelPublished), which is what a program asking to cancel waits on.
// A control that cannot be started answers the fallback at once.
func (g *Gateway) stopCards(ctx context.Context, rec *SessionRecord, authority Authority, backend string, quiet bool) {
	taskID := "task-" + randHex(taskIDHexWidth)
	correlationID := "corr-" + randHex(correlationIDHexWidth)
	addressee := rec.Addressee
	g.log.Info("card stop", "correlationId", correlationID, "taskId", taskID,
		"conversation", rec.Key, "addressee", addressee, "followUp", quiet)
	notStarted := func(why string, err error) {
		g.log.Error("card stop did not start: "+why, "taskId", taskID, "conversation", rec.Key, "err", err)
		g.logCardStopEnded(rec.Key, taskID, "", "", cardStopOutcomeNotStarted)
	}

	capRef, err := g.mintCapability(ctx, taskID, addressee)
	if err != nil {
		if !g.cfg.CapabilityOptional {
			notStarted("capability mint failed", err)
			g.post(rec.Key, cardStopUnconfirmedNotice)
			return
		}
		g.log.Warn("A2A_CAPABILITY_REQUIRED=false; sending the card stop with no capability", "taskId", taskID, "err", err)
		capRef = nil
	}
	payload, err := cardStopPayload(taskID, rec.ContextID)
	if err != nil {
		notStarted("payload build failed", err)
		g.post(rec.Key, cardStopUnconfirmedNotice)
		return
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, rec.ContextID, correlationID, payload,
		lib.WithTo(lib.Party{Session: addressee}),
		lib.WithAuthority(authority.Render(capRef)))
	if err != nil {
		notStarted("envelope build failed", err)
		g.post(rec.Key, cardStopUnconfirmedNotice)
		return
	}

	// The placeholder is posted on every chat backend, a StatusShower's too:
	// the control is announced as no task, so nothing else would show that
	// the stop is under way while the executor works.
	statusMsgID := ""
	switch {
	case quiet:
	case cardStopPlaceholderOn(backend):
		if statusMsgID, err = g.adapter.Post(rec.Key, cardStopPlaceholder); err != nil {
			g.log.Error("card stop placeholder post failed", "conversation", rec.Key, "err", err)
			statusMsgID = ""
		}
	default:
		g.post(rec.Key, cardStopDoorNotice)
	}
	rec.CardStop = &CardStopRun{TaskID: taskID, CorrelationID: correlationID, Addressee: addressee,
		Capability: capRef, StatusMsgID: statusMsgID, StartedAt: time.Now().UTC(), Quiet: quiet}
	g.mu.Lock()
	g.taskSessions[taskID] = rec.Key
	g.relays[taskID] = &relayState{local: true}
	g.mu.Unlock()
	if err := g.reg.IndexTask(ctx, taskID, rec.Key); err != nil {
		g.log.Error("task index write failed", "taskId", taskID, "err", err)
	}
	if err := g.reg.Put(ctx, rec); err != nil {
		g.log.Error("session record write failed", "conversation", rec.Key, "err", err)
	}
	if err := g.client.Publish(ctx, lib.TaskInSubject(addressee, taskID), env); err != nil {
		notStarted("publish failed", err)
		rec.CardStop = nil
		g.retireTaskRoute(ctx, taskID)
		g.answerCardStop(rec.Key, statusMsgID, cardStopUnconfirmedNotice)
		return
	}
	// Only a stop's own turn tells a door its cancel went out. A quiet
	// follow-up runs from the relay or the timer, inside no turn, and a
	// door's cancel waiting on this conversation must not read it as its
	// own.
	if !quiet {
		g.observeCancelPublished(rec.Key, taskID)
	}
	g.armCardStopTimer(rec.Key, taskID, authority)
}

// logCardStopEnded is the one line per control's end, whatever ended it: the
// terminal's state and reason token when there was one, and the outcome
// (cardStopOutcome*). A rejection with reason no-text-parts is a bridge
// older than the control.
func (g *Gateway) logCardStopEnded(conversation, taskID string, state lib.TaskState, reason, outcome string, attrs ...any) {
	g.log.Info("card stop ended", append([]any{"conversation", conversation, "taskId", taskID,
		"state", state, "reason", reasonToken(reason), "outcome", outcome}, attrs...)...)
}

// answerCardStop puts the control's answer where its placeholder is, or posts
// it when there is none or the edit fails.
func (g *Gateway) answerCardStop(conversation, statusMsgID, text string) {
	if statusMsgID != "" {
		err := g.adapter.Edit(conversation, statusMsgID, text)
		if err == nil {
			return
		}
		g.log.Warn("card stop answer edit failed; posting it", "conversation", conversation, "err", err)
	}
	g.post(conversation, text)
}

// applyCardStopEvent folds one of the control's events: its result
// artifact, kept for the terminal, and its terminal. Nothing else the
// executor publishes for it is rendered: the placeholder is the whole line.
func (g *Gateway) applyCardStopEvent(ctx context.Context, rec *SessionRecord, rs *relayState, item relayItem) {
	env := item.env
	switch env.Kind {
	case lib.KindArtifactUpdate:
		var a lib.ArtifactUpdate
		if err := json.Unmarshal(env.Payload, &a); err != nil {
			g.log.Error("relay: malformed artifact-update", "taskId", env.TaskID, "err", err)
			return
		}
		if a.Artifact.Name != lib.ArtifactResult {
			return
		}
		if a.Append {
			rs.result = append(rs.result, a.Artifact.Parts...)
		} else {
			rs.result = append([]lib.Part(nil), a.Artifact.Parts...)
		}
	case lib.KindStatusUpdate:
		var s lib.StatusUpdate
		if err := json.Unmarshal(env.Payload, &s); err != nil {
			g.log.Error("relay: malformed status-update", "taskId", env.TaskID, "err", err)
			return
		}
		rs.state = s.Status.State
		if s.Final {
			g.finishCardStop(ctx, rec, rs, env.TaskID, s, terminalSourceOf(item.subject))
		}
	}
}

// finishCardStop answers the control's terminal: the reply worded from its
// cardStop result part when it completed with one, the fallback otherwise.
// None of the task's other terminal work runs (relayTerminal's deliverable,
// delivery observation, hand-off, wake): the control is not a turn.
func (g *Gateway) finishCardStop(ctx context.Context, rec *SessionRecord, rs *relayState, taskID string, s lib.StatusUpdate, source TerminalSource) {
	run := rec.CardStop
	reason := ""
	if s.Status.Message != nil {
		reason = joinTextParts(s.Status.Message.Parts)
	}
	answer, post, outcome, stopped := cardStopUnconfirmedNotice, true, cardStopOutcomeExecutor, 0
	if s.Status.State == lib.StateCompleted {
		outcome = cardStopOutcomeNoResult
		result, ok := cardStopResultOf(rs.result)
		if !ok && !rs.local {
			// A restart lost the render state; the stream has the result.
			if task, err := g.replayForTerminal(ctx, run.Addressee, taskID); err == nil {
				if art := task.Artifact(lib.ArtifactResult); art != nil {
					result, ok = cardStopResultOf(art.Parts)
				}
			} else {
				g.log.Error("relay: card stop replay failed", "taskId", taskID, "err", err)
			}
		}
		if ok {
			outcome, stopped = cardStopOutcomeAnswered, len(result.Stopped)
			answer = cardStopReply(result, rec.ActiveTask != nil && !rec.ActiveTask.Detached)
			// A follow-up that found nothing more says nothing: the first
			// control already answered the stop.
			post = !run.Quiet || run.Promised || cardStopNews(result)
		}
	}
	if post {
		g.answerCardStop(rec.Key, run.StatusMsgID, answer)
	}
	g.logCardStopEnded(rec.Key, taskID, s.Status.State, reason, outcome, "stopped", stopped, "source", source, "followUp", run.Quiet)
	g.endCardStop(ctx, rec)
}

// cardStopNews reports whether a follow-up's result is worth a post: it
// stopped a card, or it could not promise everything was stopped.
func cardStopNews(r *lib.CardStopResult) bool {
	return len(r.Stopped) > 0 || r.Failed > 0 || r.Pending > 0 || r.Unresolved > 0 || r.Truncated || r.Deadline
}

// endCardStop clears rec's control, whatever ended it: its timer, its route
// (a late event finds no route, and RetiredCardStops catches one whose
// index outlived the drop), and then the follow-up a later stop asked for.
func (g *Gateway) endCardStop(ctx context.Context, rec *SessionRecord) {
	run := rec.CardStop
	rec.CardStop = nil
	rec.RetiredCardStops = append(rec.RetiredCardStops, run.TaskID)
	if n := len(rec.RetiredCardStops); n > cardStopRetiredKept {
		rec.RetiredCardStops = slices.Clone(rec.RetiredCardStops[n-cardStopRetiredKept:])
	}
	g.stopCardStopTimer(run.TaskID)
	g.retireTaskRoute(ctx, run.TaskID)
	if run.Again != nil {
		g.followUpCardStop(ctx, rec, *run.Again)
	}
}

// followUpCardStop runs the one follow-up a stop asked for while a control
// was in flight. That stop already cancelled whatever turn it found, so there
// is nothing to cancel here. It is not sent, and the room is told why, when
// the card stop has been turned off since (a restart with A2A_CARD_STOP
// off), when the conversation has moved off the route a control is answered
// on (/session, or a delegate's re-home), or when a turn is running: that
// turn started after the stop, and a control would wait for it and archive
// its cards too, which nobody asked for.
func (g *Gateway) followUpCardStop(ctx context.Context, rec *SessionRecord, authority Authority) {
	var why, notice string
	switch active := rec.ActiveTask; {
	case !g.cfg.CardStop:
		why, notice = "card stop off", cardStopOffNotice
	case !g.cardStopRoute(rec):
		why, notice = "route moved", cardStopMovedNotice
	case active != nil && !active.Detached:
		why, notice = "newer turn", cardStopSkippedNotice
	}
	if notice != "" {
		g.log.Info("card stop follow-up not sent", "conversation", rec.Key, "why", why)
		g.post(rec.Key, notice)
		return
	}
	g.stopCards(ctx, rec, authority, "", true)
}

// cardStopResultOf finds the result form of the cardStop part among parts.
func cardStopResultOf(parts []lib.Part) (*lib.CardStopResult, bool) {
	c, ok := lib.CardStopOf(parts)
	if !ok || c.CardStopResult == nil {
		return nil, false
	}
	return c.CardStopResult, true
}

// armCardStopTimer bounds the wait for the control's terminal
// (Config.CardStopTimeout). The timer holds the stop's authority, which a
// cancel of the control carries; a restart loses both, and expireCardStop
// gives the control up on the conversation's next turn instead.
func (g *Gateway) armCardStopTimer(conversation, taskID string, authority Authority) {
	t := time.AfterFunc(g.cfg.CardStopTimeout, func() { g.cardStopTimedOut(conversation, taskID, authority) })
	g.mu.Lock()
	g.cardStopTimers[taskID] = t
	g.mu.Unlock()
}

// stopCardStopTimer drops the control's timer, stopping it if it has not
// fired.
func (g *Gateway) stopCardStopTimer(taskID string) {
	g.mu.Lock()
	t := g.cardStopTimers[taskID]
	delete(g.cardStopTimers, taskID)
	g.mu.Unlock()
	if t != nil {
		t.Stop()
	}
}

// cardStopTimedOut runs when the control's bound passes: under the
// conversation's lock, as every writer of the record runs, and only if the
// control is still the one in flight (its terminal may have just landed).
func (g *Gateway) cardStopTimedOut(conversation, taskID string, authority Authority) {
	if g.runCtx.Err() != nil {
		return
	}
	l := g.lockSession(conversation)
	l.Lock()
	defer l.Unlock()
	g.stopCardStopTimer(taskID)
	ctx, cancel := context.WithTimeout(g.runCtx, turnTimeout)
	defer cancel()
	var rec *SessionRecord
	if err := withRetry(kvRetryAttempts, func() error {
		var e error
		rec, e = g.reg.Get(ctx, conversation)
		return e
	}); err != nil {
		g.log.Error("card stop timeout: session record unavailable", "conversation", conversation, "taskId", taskID, "err", err)
		return
	}
	if rec == nil || rec.CardStop == nil || rec.CardStop.TaskID != taskID {
		return
	}
	g.abandonCardStop(ctx, rec, authority, cardStopOutcomeTimedOut)
	if err := withRetry(kvRetryAttempts, func() error { return g.reg.Put(ctx, rec) }); err != nil {
		g.log.Error("session record write failed", "conversation", rec.Key, "err", err)
	}
}

// expireCardStop gives up a control past its bound that no timer of this
// process will (it started before a restart). The caller writes the record.
func (g *Gateway) expireCardStop(ctx context.Context, rec *SessionRecord, authority Authority) {
	run := rec.CardStop
	if run == nil || time.Since(run.StartedAt) <= g.cfg.CardStopTimeout {
		return
	}
	g.mu.Lock()
	_, timed := g.cardStopTimers[run.TaskID]
	g.mu.Unlock()
	if timed {
		// The timer fired and waits on this conversation's lock, which this
		// turn holds; it answers once the turn ends.
		g.log.Info("card stop past its bound; its timer answers after this turn", "conversation", rec.Key, "taskId", run.TaskID)
		return
	}
	g.abandonCardStop(ctx, rec, authority, cardStopOutcomeExpired)
	// Written here as well as at the end of the turn: a turn that returns
	// before its write would give the control up again on the next one.
	if err := g.reg.Put(ctx, rec); err != nil {
		g.log.Error("session record write failed", "conversation", rec.Key, "err", err)
	}
}

// abandonCardStop ends the wait for rec's control: it cancels the control,
// so a bridge still waiting on the session's turn stops there (a POST
// already sent runs to its own end), answers the fallback, since whether any
// card stopped is unknown, and ends the control (endCardStop). outcome is
// why: cardStopOutcomeTimedOut from the timer, cardStopOutcomeExpired for a
// control that started before a restart.
func (g *Gateway) abandonCardStop(ctx context.Context, rec *SessionRecord, authority Authority, outcome string) {
	run := rec.CardStop
	if outcome == cardStopOutcomeExpired {
		g.log.Warn("card stop started before this gateway restarted and is past its bound; answering the fallback",
			"conversation", rec.Key, "taskId", run.TaskID, "timeout", g.cfg.CardStopTimeout)
	} else {
		g.log.Warn("card stop timed out; answering the fallback", "conversation", rec.Key, "taskId", run.TaskID,
			"timeout", g.cfg.CardStopTimeout)
	}
	env, err := lib.NewCancelEnvelope(gatewayParty, run.TaskID, rec.ContextID, run.CorrelationID,
		lib.WithTo(lib.Party{Session: run.Addressee}),
		lib.WithAuthority(authority.Render(run.Capability)))
	if err == nil {
		err = g.client.Publish(ctx, lib.TaskInSubject(run.Addressee, run.TaskID), env)
	}
	if err != nil {
		g.log.Warn("card stop cancel not sent", "taskId", run.TaskID, "err", err)
	}
	g.answerCardStop(rec.Key, run.StatusMsgID, cardStopUnconfirmedNotice)
	g.logCardStopEnded(rec.Key, run.TaskID, "", "", outcome, "followUp", run.Quiet)
	g.endCardStop(ctx, rec)
}

// cardStopReply words a completed control's result for the room. The
// executor's own text says the same for a reader that does not know the
// part (a2a/hermes-bridge/cardstop.go, cardStopText); this wording is the
// gateway's. It names at most cardStopNamed titles per list, each cleaned of
// anything that could ping or lay out (cardStopTitle), and says plainly what
// the stop could not promise. A stop that found nothing open says nothing is
// running, which is now true: the executor looked. It looked at cards only,
// so with a newer turn running (turnRunning) it says no card was open.
func cardStopReply(r *lib.CardStopResult, turnRunning bool) string {
	var clauses []string
	if n := r.Unresolved; n > 0 && len(r.Stopped) > 0 {
		clauses = append(clauses, fmt.Sprintf("a worker may still start for %d of them, claimed just before the stop", n))
	} else if n > 0 {
		clauses = append(clauses, fmt.Sprintf("a worker may still start for %s claimed just before the stop", cardCount(n)))
	}
	if n := r.Failed; n > 0 {
		clauses = append(clauses, cardCount(n)+" could not be stopped")
	}
	if n := r.Pending; n > 0 {
		them := "they"
		if n == 1 {
			them = "it"
		}
		clauses = append(clauses, cardCount(n)+" still being stopped when time ran out, so "+them+" may still be running")
	} else if r.Deadline {
		clauses = append(clauses, "the stop ran out of time before it finished, so some may still be open")
	}
	if r.Truncated {
		clauses = append(clauses, "this conversation had more cards than one stop takes, so some may still be open")
	}
	// ListsCut is one flag for both lists; the one that was cut is the one
	// at the executor's cap (lib.CardStopListMax).
	stoppedCut := r.ListsCut && len(r.Stopped) >= lib.CardStopListMax
	settledCut := r.ListsCut && len(r.Settled) >= lib.CardStopListMax
	if len(r.Settled) > 0 {
		clauses = append(clauses, "already finished: "+cardTitles(r.Settled, settledCut))
	}

	var head string
	switch {
	case len(r.Stopped) > 0:
		count := cardCount(len(r.Stopped))
		if stoppedCut {
			count = "at least " + count
		}
		head = "🛑 stopped " + count + ": " + cardTitles(r.Stopped, stoppedCut)
	case r.Failed > 0 || r.Pending > 0 || r.Deadline || r.Truncated || r.Unresolved > 0:
		head = "⚠️ no card was stopped"
	case turnRunning:
		head = cardStopNoCardNotice
	default:
		head = nothingRunningNotice
	}
	return strings.Join(append([]string{head}, clauses...), "; ")
}

// cardTitles names the first cardStopNamed cards and counts the rest. cut
// says the list itself was cut short, so the rest is a lower bound.
func cardTitles(cards []lib.CardStopCard, cut bool) string {
	names := make([]string, 0, min(len(cards), cardStopNamed))
	for _, c := range cards[:min(len(cards), cardStopNamed)] {
		names = append(names, cardStopTitle(c.Title))
	}
	out := strings.Join(names, ", ")
	switch rest := len(cards) - len(names); {
	case rest > 0 && cut:
		out += fmt.Sprintf(" and at least %d more", rest)
	case rest > 0:
		out += fmt.Sprintf(" and %d more", rest)
	case cut:
		out += " and more"
	}
	return out
}

// cardStopTitle is a card's title as the reply shows it. A title is model
// text, so it may hold anything a chat surface acts on. Every run of
// whitespace, control and format characters (newlines, zero-widths, bidi
// overrides) becomes one space, so a title is one line and cannot hide what
// follows it. Angle brackets become look-alikes, so no Slack <@U…>, <!here>
// or <#C…>, Discord <@…> or Google Chat <users/…> survives, and @ becomes the
// full-width ＠, so no bare @here, @channel or @everyone pings. Markdown
// that could make a masked link or bleed formatting into the rest of the
// reply is neutralised the same way: [ and ] become ( and ), a backtick an
// apostrophe, ~ a hyphen, | a broken bar, and * and _ a space, so
// [x](https://…) is no link and an unclosed ` or * opens nothing. Every dot
// an autolinker reads as one (titleDots) becomes the one-dot leader ․ and
// every colon (titleColons) the look-alike ꞉, so a bare URL or domain
// (https://evil.example, evil.example) reads the same but no surface links
// it. It is at most cardStopTitleRunes runes, a cut one ending in "…".
// titleDots are the runes an IDNA-aware autolinker takes as a domain's dot:
// ASCII ., the ideographic full stop, and the full-width and half-width
// full stops. titleColons are ASCII : and the full-width colon.
var (
	titleDots   = map[rune]bool{'.': true, '。': true, '．': true, '｡': true}
	titleColons = map[rune]bool{':': true, '：': true}
)

func cardStopTitle(s string) string {
	var b strings.Builder
	space := false
	for _, r := range s {
		switch {
		case unicode.IsSpace(r) || unicode.IsControl(r) || unicode.Is(unicode.Cf, r) || r == utf8.RuneError:
			space = b.Len() > 0
			continue
		case r == '<':
			r = '‹'
		case r == '>':
			r = '›'
		case r == '@':
			r = '＠'
		case r == '[':
			r = '('
		case r == ']':
			r = ')'
		case r == '`':
			r = '\''
		case r == '~':
			r = '-'
		case r == '|':
			r = '¦'
		case titleDots[r]:
			r = '․'
		case titleColons[r]:
			r = '꞉'
		case r == '*' || r == '_':
			space = b.Len() > 0
			continue
		}
		if space {
			b.WriteByte(' ')
			space = false
		}
		b.WriteRune(r)
	}
	out := b.String()
	if out == "" {
		return "(untitled card)"
	}
	if utf8.RuneCountInString(out) > cardStopTitleRunes {
		out = string([]rune(out)[:cardStopTitleRunes-1]) + "…"
	}
	return out
}

// cardCount is n with the noun "card".
func cardCount(n int) string {
	if n == 1 {
		return "1 card"
	}
	return fmt.Sprintf("%d cards", n)
}
