package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// discordChunk leaves headroom under Discord's 2000-char message cap.
const discordChunk = 1900

// progressCap bounds the progress text embedded in rolling lines and status
// answers, so one artifact can't blow a chat edit past the backend cap.
const progressCap = 300

// KV access rides withRetry with these shapes: enough to ride out a
// connection rebuild window without inventing a second resilience layer,
// and a short requeue pause where a whole batch has to come back.
const (
	kvRetryAttempts = 3
	kvRetryPause    = 200 * time.Millisecond
	requeueDelay    = 2 * time.Second
)

// relayState is the in-memory render state for one task's rolling line. It
// is cache: a gateway restart loses it, and the terminal path falls back to
// a stream replay to recover the result — the stream is the record.
type relayState struct {
	state    lib.TaskState
	progress string
	result   []lib.Part
	// lastLine is the rolling line as last rendered, so a display mode that
	// drops the narration does not burn a backend edit per progress artifact
	// re-rendering an unchanged line.
	lastLine string
	// notices wait for the task's terminal and post after its deliverable:
	// a delegation refusal follows the turn's "delegated to platform".
	notices []string
	// lagSeen is when this gateway first saw the task final on the stream
	// with its delegate request unrelayed, for a terminal the replay could
	// not time (relayLagStart). Zero otherwise.
	lagSeen time.Time
	// sawDelegate is set once this gateway has run the task's delegate
	// request (applyArtifact, or relayTerminal from the fold). A terminal
	// with it unset reads the fold for a request this process never saw
	// (relayTerminal).
	sawDelegate bool
	// local is set when this gateway started the task (startTaskWith), so
	// every event of it reached this process's relay: an event acked and
	// lost before its batch takes a crash, and a restart starts the task's
	// relay state afresh without it.
	local bool
}

// relayItem is one queued event with the subject it arrived on. The relay's
// durable spans a task's `…events` and `…supervisor` subjects, and the
// subject is the only thing that says whose word a terminal is: the
// executor's off the first, the supervisor's -- this gateway's, about an
// executor that died or never ran -- off the second. The envelope does not
// carry it, so it rides beside the envelope to the terminal.
type relayItem struct {
	env     *lib.Envelope
	subject string
}

// terminalSourceOf attributes a terminal by the subject it arrived on, the
// rule the heal (healActiveTask) and the read route (probeConversation)
// apply to the fold's terminal subject, so one supervisor terminal is the
// supervisor's on every path it can reach the adapter by. The envelope has
// already been held to its subject at delivery (CheckSubjectAgreement), so
// the class token is the whole decision.
func terminalSourceOf(subject string) TerminalSource {
	if _, _, class, ok := lib.ParseTaskSubject(subject); ok && class == lib.TaskClassSupervisor {
		return TerminalFromSupervisor
	}
	return TerminalFromExecutor
}

// relayEvent routes one event to its conversation's queue. Runs on the
// durable consumer's dispatch goroutine, so it must not block: the actual
// rendering — session lock, KV, chat REST calls — happens on the per-session
// worker, where one slow conversation stalls only itself.
func (g *Gateway) relayEvent(ctx context.Context, subject string, env *lib.Envelope) {
	if env.Kind != lib.KindStatusUpdate && env.Kind != lib.KindArtifactUpdate {
		return
	}
	sessionKey := g.sessionForTask(ctx, env.TaskID)
	if sessionKey == "" {
		// Not a task this gateway submitted (another requester's traffic on
		// the shared events wildcard, or a post-terminal straggler whose
		// index was already retired); not ours to render.
		return
	}
	g.events.enqueue(sessionKey, relayItem{env: env, subject: subject})
}

// relayBatch renders a session's queued events in order. Rolling-line edits
// are coalesced: only the last event of the batch renders the line, so a
// backlog of progress artifacts becomes one edit instead of a rate-limited
// stampede. Posts (results, failures, input asks) always render.
func (g *Gateway) relayBatch(sessionKey string, batch []relayItem) {
	l := g.lockSession(sessionKey)
	l.Lock()
	defer l.Unlock()

	ctx, cancel := context.WithTimeout(g.runCtx, turnTimeout)
	defer cancel()

	var rec *SessionRecord
	err := withRetry(kvRetryAttempts, func() error {
		var e error
		rec, e = g.reg.Get(ctx, sessionKey)
		return e
	})
	if err != nil {
		// The events stay unrendered but the stream keeps them; requeue the
		// batch so a transient KV failure on a terminal event cannot wedge
		// the conversation with the result never posted.
		g.log.Error("relay: session record unavailable; requeueing batch", "session", sessionKey, "err", err)
		go func() {
			time.Sleep(requeueDelay)
			for _, item := range batch {
				g.events.enqueue(sessionKey, item)
			}
		}()
		return
	}
	if rec == nil {
		// The session record was deleted/retired past SessionTTL; the conversation
		// no longer exists. Drop the batch and retire the task routing state so
		// subsequent stragglers are discarded immediately rather than looping.
		g.log.Warn("relay: session record retired; dropping batch", "session", sessionKey)
		for _, item := range batch {
			taskID := item.env.TaskID
			g.mu.Lock()
			delete(g.relays, taskID)
			delete(g.taskSessions, taskID)
			g.mu.Unlock()
			if err := g.reg.DropTask(ctx, taskID); err != nil {
				g.log.Warn("relay: task index cleanup failed", "taskId", taskID, "err", err)
			}
		}
		return
	}

	for i, item := range batch {
		g.applyEvent(ctx, rec, item, i == len(batch)-1)
	}

	if err := withRetry(kvRetryAttempts, func() error { return g.reg.Put(ctx, rec) }); err != nil {
		g.log.Error("relay: session record write failed", "session", rec.Key, "err", err)
	}
}

// applyEvent folds one event into the render state. render gates only the
// rolling-line edit; posts always happen.
func (g *Gateway) applyEvent(ctx context.Context, rec *SessionRecord, item relayItem, render bool) {
	env := item.env
	g.mu.Lock()
	rs, ok := g.relays[env.TaskID]
	if !ok {
		rs = &relayState{}
		g.relays[env.TaskID] = rs
	}
	g.mu.Unlock()

	switch env.Kind {
	case lib.KindStatusUpdate:
		var s lib.StatusUpdate
		if err := json.Unmarshal(env.Payload, &s); err != nil {
			g.log.Error("relay: malformed status-update", "taskId", env.TaskID, "err", err)
			return
		}
		g.applyStatus(ctx, rec, rs, env.TaskID, s, terminalSourceOf(item.subject), render)
	case lib.KindArtifactUpdate:
		var a lib.ArtifactUpdate
		if err := json.Unmarshal(env.Payload, &a); err != nil {
			g.log.Error("relay: malformed artifact-update", "taskId", env.TaskID, "err", err)
			return
		}
		g.applyArtifact(ctx, rec, rs, item.subject, env.TaskID, a, render)
	}
}

func (g *Gateway) applyStatus(ctx context.Context, rec *SessionRecord, rs *relayState, taskID string, s lib.StatusUpdate, source TerminalSource, render bool) {
	rs.state = s.Status.State
	switch {
	case s.Final:
		g.relayTerminal(ctx, rec, rs, taskID, s, source)
	case s.Status.State == lib.StateInputRequired:
		ask := ""
		if s.Status.Message != nil {
			ask = joinTextParts(s.Status.Message.Parts)
		}
		if ask == "" {
			ask = "the task needs input to continue"
		}
		g.post(rec.Key, "❓ "+ask)
		g.updateRollingLine(rec, taskID, rs)
	default:
		// A non-final status message (eg W7's honest "Hermes cannot absorb
		// mid-run input" answer to a steer) is worth the room seeing.
		if s.Status.Message != nil {
			if note := joinTextParts(s.Status.Message.Parts); note != "" {
				g.post(rec.Key, "ℹ️ "+note)
			}
		}
		if render {
			g.updateRollingLine(rec, taskID, rs)
		}
	}
}

func (g *Gateway) applyArtifact(ctx context.Context, rec *SessionRecord, rs *relayState, subject, taskID string, a lib.ArtifactUpdate, render bool) {
	switch a.Artifact.Name {
	case lib.ArtifactProgress:
		// The rolling progress line: one edited chat message as progress
		// artifacts arrive — no model calls, zero marginal cost.
		if text := lastTextPart(a.Artifact.Parts); text != "" {
			rs.progress = truncateRunes(text, progressCap)
		}
		if render {
			g.updateRollingLine(rec, taskID, rs)
		}
	case lib.ArtifactResult:
		if a.Append {
			rs.result = append(rs.result, a.Artifact.Parts...)
		} else {
			rs.result = append([]lib.Part(nil), a.Artifact.Parts...)
		}
	case lib.ArtifactDelegate:
		// A request to the gateway, never rendered to chat.
		rs.sawDelegate = true
		g.handleDelegateRequest(ctx, rec, subject, taskID, a.Artifact.Parts)
	case lib.ArtifactThinking, lib.ArtifactActivity:
		// Debug/audit views only; never rendered to chat.
	}
}

// completedNonTextResult stands in for a completed task's result that has no
// text, in the room and in a wake.
const completedNonTextResult = "(completed with a non-text result; see the stream)"

// relayTerminal posts the deliverable (or the failure), releases the
// session's serialization, and retires the task's index — the stream is
// the durable record; the index only exists to route live events. source is
// whose word the terminal is, read off the subject it arrived on.
func (g *Gateway) relayTerminal(ctx context.Context, rec *SessionRecord, rs *relayState, taskID string, s lib.StatusUpdate, source TerminalSource) {
	result := joinTextParts(rs.result)
	// The console never posts the deliverable (see the StateCompleted arm), so
	// replaying the stream to recover it would buy nothing. Checking here and
	// not there is the difference between skipping the replay and paying for
	// one whose result is then dropped.
	// A child's result feeds its wake as well as the room, so a child on a
	// console conversation still has it replayed.
	ref, _ := rec.TaskRefFor(taskID)
	needResult := result == "" && s.Status.State == lib.StateCompleted &&
		(!isConsoleConversation(rec.Key) || ref.Role == taskRoleChild)
	// A session turn that ends completed may have asked to delegate in an
	// event this process never ran: the artifact's delivery was acked and
	// lost to a crash before its batch, and only the terminal was
	// redelivered. Only a task this process did not start (local) can
	// have lost an event that way. The record is the witness that a request was handled (a
	// child linked, or a refusal marked, each written when it is made), so
	// with neither on record and the request never seen here, the fold is
	// read for it and the request runs first, every check applying, before
	// the turn ends - otherwise the hand-off line would end the chain.
	needDelegate := s.Status.State == lib.StateCompleted && !rs.local && !rs.sawDelegate && rec.mayHaveUnhandledDelegate(taskID)
	evidence := delegateAbsent
	if needResult || needDelegate {
		// Render state is cache; if a restart lost it, the stream still has
		// everything. Replay against the addressee the task's own subjects
		// carried - after a Delegate re-home, rec.Addressee is not it.
		addressee := rec.AddresseeFor(taskID)
		if task, err := g.replayForTerminal(ctx, addressee, taskID); err == nil {
			if art := task.Artifact(lib.ArtifactResult); art != nil && needResult {
				result = joinTextParts(art.Parts)
			}
			if task.Artifact(lib.ArtifactDelegate) != nil {
				evidence = delegateSeen
			}
			if needDelegate {
				g.runFoldedDelegate(ctx, rec, rs, taskID, addressee, task)
			}
		} else {
			g.log.Error("relay: terminal replay fallback failed", "taskId", taskID, "err", err)
			// The request this process never saw cannot be read: whether
			// the turn asked to delegate is unknown, so its own result is
			// not trusted as a deliverable (settleHandOff).
			if needDelegate {
				evidence = delegateUnknown
			}
		}
	}
	if rs.sawDelegate {
		evidence = delegateSeen
	}
	// The hand-off line is never a deliverable: a session turn whose
	// request minted no child ends failed toward the observers, with the
	// reason on its entry (handOffEnd, read by observeDelivered and
	// observeEnded below and by the read route).
	g.settleHandOff(rec, taskID, evidence)

	switch s.Status.State {
	case lib.StateCompleted:
		// Whole, to the adapter whose caller is a program, before the chunked
		// posts below (DeliverableObserver). A non-text result hands over
		// nothing: the notice that replaces it is not the deliverable.
		if result != "" {
			g.observeDelivered(rec, taskID, result)
		}
		if result == "" {
			result = completedNonTextResult
		}
		// The console renders answers straight off TASKS, so posting the
		// deliverable here too would show it twice and put a burst of
		// answer-sized frames on a subject documented to carry only notices
		// (console.go and spec-chatops-gateway.md, "The console adapter").
		// The other terminal arms below are notices, not answers, and go to
		// every backend. Chat backends have no TASKS view, so for them this
		// post IS the answer. The replay above is skipped for the same
		// backends, so reaching here with an empty result costs nothing.
		if !isConsoleConversation(rec.Key) {
			g.post(rec.Key, result)
		}
	case lib.StateFailed:
		reason := ""
		if s.Status.Message != nil {
			reason = joinTextParts(s.Status.Message.Parts)
		}
		if reason != "" {
			g.post(rec.Key, "❌ failed: "+reason)
		} else {
			g.post(rec.Key, "❌ the task failed")
		}
	case lib.StateCanceled:
		g.post(rec.Key, "🛑 canceled")
	case lib.StateRejected:
		// Same shape as failed above, and for the same reason. Both
		// executors put the cause in the terminal message -- a capability
		// refusal names the rule the verifier returned, and an unreachable
		// verifier says so -- and a bare "rejected" sends the user looking
		// at their own prompt for a fault that is in the install. Before
		// the capability check only an empty submission reached rejected,
		// where there was nothing useful to add; now an outage does.
		reason := ""
		if s.Status.Message != nil {
			reason = joinTextParts(s.Status.Message.Parts)
		}
		if reason != "" {
			g.post(rec.Key, "🚫 the executor rejected the task: "+reason)
		} else {
			g.post(rec.Key, "🚫 the executor rejected the task")
		}
	}
	g.flushNotices(rec.Key, rs)

	if active := rec.ActiveTask; active != nil && active.TaskID == taskID {
		if active.StatusMsgID != "" {
			// The same display gate as the rolling line: under default the
			// terminal edit carries the state and never the narration.
			progress := rs.progress
			if g.cfg.DisplayMode == displayModeDefault {
				progress = ""
			}
			g.editLine(rec.Key, active.StatusMsgID, withLineNote(terminalLine(s.Status.State, progress), active.LineNote))
		}
		rec.ActiveTask = nil
		// An executor's end of a live task is activity. The idle TTL that
		// bounds the session (the reap, and the Slack adapter's session-
		// thread rule through hasSession) counts from here, not from the ask
		// that started the task: a long task's thread must not go quiet the
		// instant its answer posts, and the user's follow-up right after the
		// result is the most ordinary message a session carries. The
		// executor's confirmation of a stop is an answer too: a detached
		// task's terminal from the executor counts. The supervisor's does
		// not: that is the reap's own word about a session it has just
		// judged idle, and stamping it would re-open the window the reap
		// closed.
		if source == TerminalFromExecutor {
			now := time.Now().UTC()
			rec.LastActivity = now
			rec.LastTaskActivity = now
		}
	} else if parent, ok := rec.TaskRefFor(taskID); ok && parent.StatusMsgID != "" {
		// A session turn that delegated ends after its child took the
		// active task - while the child runs, or after the child's own
		// terminal and the wake it started, since nothing orders the two
		// tasks' events. Its line closes as any turn's does, from the entry
		// that kept it. Its answer ("delegated to platform") is task
		// activity for the session-thread rule all the same; LastActivity
		// stays the active task's business.
		progress := rs.progress
		if g.cfg.DisplayMode == displayModeDefault {
			progress = ""
		}
		g.editLine(rec.Key, parent.StatusMsgID, terminalLine(s.Status.State, progress))
		setParentLine(rec, taskID, "")
		if source == TerminalFromExecutor {
			rec.LastTaskActivity = time.Now().UTC()
		}
	}
	// Retire the routing state. A post-final straggler then finds no route
	// and is dropped rather than re-rendered (assertion 10 lives in the lib
	// and the fold; the gateway's job is only to never replay the result at
	// the room).
	g.retireTaskRoute(ctx, taskID)
	// Last, after the deliverable is posted and the rolling line edited, so
	// an observer that treats this as "the task is over" has already been
	// handed everything the conversation received for it. See TaskObserver.
	//
	// With whose word it is. A supervisor terminal reaches this same path
	// (the relay's durable covers both `…events` and `…supervisor`), and that
	// one is the gateway's word about an executor that died or never ran,
	// not an answer; only the subject distinguishes the two, so the subject
	// rides with the envelope from the durable to here (relayItem) and is
	// attributed the way the heal and the read route attribute the fold's
	// terminal. A program grading off the door takes the executor's terminal
	// and classifies the supervisor's as the install's, and it must read the
	// same answer whichever of the three paths delivered the terminal first.
	//
	// The reason is the terminal's status message, verbatim: the bridge and
	// the worker adapter write `reason: <token>[ - detail]` there, and a
	// program classifying a failed terminal reads the token. Passed through
	// rather than parsed here, because the tokens are the executors'
	// definitions and the gateway has no business knowing them.
	reason := ""
	if s.Status.Message != nil {
		reason = joinTextParts(s.Status.Message.Parts)
	}
	//
	// Under the chain's root, and only for the task whose end is the
	// chain's (observedAs): a turn that delegated and a child end quietly,
	// and the root's one terminal comes from the wake or, when none runs,
	// from observeChildEnd below.
	g.observeEnded(rec, taskID, s.Status.State, source, reason)

	// A delegated child's end wakes the session that asked.
	if ref, ok := rec.TaskRefFor(taskID); ok && ref.Role == taskRoleChild {
		if woken, why := g.wakeSession(ctx, rec, ref, s.Status.State, result, reason); !woken {
			g.observeChildEnd(rec, ref, s.Status.State, source, reason, why)
		}
	}
}

// replayForTerminal is relayTerminal's read of the task's stream.
func (g *Gateway) replayForTerminal(ctx context.Context, addressee, taskID string) (*lib.Task, error) {
	if g.terminalReplayHook != nil {
		if err := g.terminalReplayHook(taskID); err != nil {
			return nil, err
		}
	}
	return g.client.TasksGet(ctx, addressee, taskID)
}

// updateRollingLine edits the task's single status message in place. Under
// the "default" display mode the line carries the state but never the
// turn-by-turn narration — the existing Chat integration's default-vs-debug
// split, honoured here rather than reinvented.
func (g *Gateway) updateRollingLine(rec *SessionRecord, taskID string, rs *relayState) {
	active := rec.ActiveTask
	if active == nil || active.TaskID != taskID || active.StatusMsgID == "" {
		return
	}
	progress := rs.progress
	if g.cfg.DisplayMode == displayModeDefault {
		progress = ""
	}
	line := withLineNote(statusLine(rs.state, progress), active.LineNote)
	if line == rs.lastLine {
		return
	}
	// Recorded only once the edit landed: under default mode every later
	// artifact renders this same line, so a failed edit recorded as sent
	// would never be retried and the line would sit at the previous state
	// until the terminal.
	if g.editLine(rec.Key, active.StatusMsgID, line) {
		rs.lastLine = line
	}
}

// editLine edits one status message and reports whether the edit landed.
func (g *Gateway) editLine(conversation, messageID, line string) bool {
	if err := g.adapter.Edit(conversation, messageID, truncateRunes(line, discordChunk)); err != nil {
		g.log.Warn("rolling line edit failed", "conversation", conversation, "err", err)
		return false
	}
	return true
}

// withLineNote suffixes a rolling line with the task's note, if it has one.
func withLineNote(line, note string) string {
	if note == "" {
		return line
	}
	return line + " " + note
}

func statusLine(state lib.TaskState, progress string) string {
	icon := map[lib.TaskState]string{
		lib.StateSubmitted:     "⏳",
		lib.StateWorking:       "⚙️",
		lib.StateInputRequired: "❓",
	}[state]
	if icon == "" {
		icon = "⏳"
	}
	label := string(state)
	if label == "" {
		label = "submitted"
	}
	line := fmt.Sprintf("%s **%s**", icon, label)
	if progress != "" {
		line += " — " + progress
	}
	return line
}

func terminalLine(state lib.TaskState, progress string) string {
	icon := map[lib.TaskState]string{
		lib.StateCompleted: "✅",
		lib.StateFailed:    "❌",
		lib.StateCanceled:  "🛑",
		lib.StateRejected:  "🚫",
	}[state]
	line := fmt.Sprintf("%s **%s**", icon, state)
	// No tail on completed: the result is posted as its own message right
	// before this edit on every backend that posts one at all (the console
	// does not - it reads answers off TASKS), and the worker adapter's
	// progress deviation (no
	// explicit progress tool — assistant text becomes `progress`, the final
	// text becomes `result`) makes the last narration routinely BE the
	// result on a single-turn task, so keeping it rendered the answer
	// twice. The other terminals post no result, so their last narration
	// is genuine context ("🛑 canceled — was checking node pressure").
	if progress != "" && state != lib.StateCompleted {
		line += " — " + progress
	}
	return line
}

// post writes to the conversation, chunked under the backend cap, logging
// rather than failing the relay — chat delivery is best-effort; the stream
// is the record.
func (g *Gateway) post(conversation, text string) {
	if strings.TrimSpace(text) == "" {
		return
	}
	for _, chunk := range chatChunks(text, discordChunk) {
		if _, err := g.adapter.Post(conversation, chunk); err != nil {
			g.log.Error("post failed", "conversation", conversation, "err", err)
			return
		}
	}
}

// sessionForTask resolves a task to its conversation: the in-memory cache
// first, the KV task index after a restart.
func (g *Gateway) sessionForTask(ctx context.Context, taskID string) string {
	key, err := g.lookupTaskSession(ctx, taskID)
	if err != nil {
		g.log.Error("task index lookup failed", "taskId", taskID, "err", err)
		return ""
	}
	return key
}

// lookupTaskSession is sessionForTask with the KV error returned, for a
// caller that must not read a failed lookup as "no route" (liveChild).
func (g *Gateway) lookupTaskSession(ctx context.Context, taskID string) (string, error) {
	g.mu.Lock()
	key := g.taskSessions[taskID]
	g.mu.Unlock()
	if key != "" {
		return key, nil
	}
	key, err := g.reg.SessionForTask(ctx, taskID)
	if err != nil {
		return "", err
	}
	if key != "" {
		g.mu.Lock()
		g.taskSessions[taskID] = key
		g.mu.Unlock()
	}
	return key, nil
}

// retireTaskRoute drops a task's routing state, the render cache and the
// in-memory and KV task index, once the task has ended for the gateway. A
// straggler for it then finds no route and is dropped.
func (g *Gateway) retireTaskRoute(ctx context.Context, taskID string) {
	g.mu.Lock()
	delete(g.relays, taskID)
	delete(g.taskSessions, taskID)
	g.mu.Unlock()
	if err := g.reg.DropTask(ctx, taskID); err != nil {
		g.log.Warn("relay: task index cleanup failed", "taskId", taskID, "err", err)
	}
}

// withRetry runs f up to n times with a short linear-backoff pause.
func withRetry(n int, f func() error) error {
	var err error
	for i := 0; i < n; i++ {
		if err = f(); err == nil {
			return nil
		}
		time.Sleep(time.Duration(i+1) * kvRetryPause)
	}
	return err
}
