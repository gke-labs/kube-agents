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
}

// relayEvent routes one event to its conversation's queue. Runs on the
// durable consumer's dispatch goroutine, so it must not block: the actual
// rendering — session lock, KV, chat REST calls — happens on the per-session
// worker, where one slow conversation stalls only itself.
func (g *Gateway) relayEvent(ctx context.Context, env *lib.Envelope) {
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
	g.events.enqueue(sessionKey, env)
}

// relayBatch renders a session's queued events in order. Rolling-line edits
// are coalesced: only the last event of the batch renders the line, so a
// backlog of progress artifacts becomes one edit instead of a rate-limited
// stampede. Posts (results, failures, input asks) always render.
func (g *Gateway) relayBatch(sessionKey string, batch []*lib.Envelope) {
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
	if err != nil || rec == nil {
		// The events stay unrendered but the stream keeps them; requeue the
		// batch so a transient KV failure on a terminal event cannot wedge
		// the conversation with the result never posted.
		g.log.Error("relay: session record unavailable; requeueing batch", "session", sessionKey, "err", err)
		go func() {
			time.Sleep(requeueDelay)
			for _, env := range batch {
				g.events.enqueue(sessionKey, env)
			}
		}()
		return
	}

	for i, env := range batch {
		g.applyEvent(ctx, rec, env, i == len(batch)-1)
	}

	if err := withRetry(kvRetryAttempts, func() error { return g.reg.Put(ctx, rec) }); err != nil {
		g.log.Error("relay: session record write failed", "session", rec.Key, "err", err)
	}
}

// applyEvent folds one event into the render state. render gates only the
// rolling-line edit; posts always happen.
func (g *Gateway) applyEvent(ctx context.Context, rec *SessionRecord, env *lib.Envelope, render bool) {
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
		g.applyStatus(ctx, rec, rs, env.TaskID, s, render)
	case lib.KindArtifactUpdate:
		var a lib.ArtifactUpdate
		if err := json.Unmarshal(env.Payload, &a); err != nil {
			g.log.Error("relay: malformed artifact-update", "taskId", env.TaskID, "err", err)
			return
		}
		g.applyArtifact(rec, rs, env.TaskID, a, render)
	}
}

func (g *Gateway) applyStatus(ctx context.Context, rec *SessionRecord, rs *relayState, taskID string, s lib.StatusUpdate, render bool) {
	rs.state = s.Status.State
	switch {
	case s.Final:
		g.relayTerminal(ctx, rec, rs, taskID, s)
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

func (g *Gateway) applyArtifact(rec *SessionRecord, rs *relayState, taskID string, a lib.ArtifactUpdate, render bool) {
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
	case lib.ArtifactThinking, lib.ArtifactActivity:
		// Debug/audit views only; never rendered to chat.
	}
}

// relayTerminal posts the deliverable (or the failure), releases the
// session's serialization, and retires the task's index — the stream is
// the durable record; the index only exists to route live events.
func (g *Gateway) relayTerminal(ctx context.Context, rec *SessionRecord, rs *relayState, taskID string, s lib.StatusUpdate) {
	result := joinTextParts(rs.result)
	if result == "" && s.Status.State == lib.StateCompleted {
		// Render state is cache; if a restart lost it, the stream still has
		// everything. Replay against the addressee the task's own subjects
		// carried - after a Delegate re-home, rec.Addressee is not it.
		if task, err := g.client.TasksGet(ctx, rec.AddresseeFor(taskID), taskID); err == nil {
			if art := task.Artifact(lib.ArtifactResult); art != nil {
				result = joinTextParts(art.Parts)
			}
		} else {
			g.log.Error("relay: terminal replay fallback failed", "taskId", taskID, "err", err)
		}
	}

	switch s.Status.State {
	case lib.StateCompleted:
		if result == "" {
			result = "(completed with a non-text result; see the stream)"
		}
		g.post(rec.Key, result)
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
		g.post(rec.Key, "🚫 the executor rejected the task")
	}

	if active := rec.ActiveTask; active != nil && active.TaskID == taskID {
		if active.StatusMsgID != "" {
			g.editLine(rec.Key, active.StatusMsgID, terminalLine(s.Status.State, rs.progress))
		}
		rec.ActiveTask = nil
	}
	// Retire the routing state. A post-final straggler then finds no route
	// and is dropped rather than re-rendered (assertion 10 lives in the lib
	// and the fold; the gateway's job is only to never replay the result at
	// the room).
	g.mu.Lock()
	delete(g.relays, taskID)
	delete(g.taskSessions, taskID)
	g.mu.Unlock()
	if err := g.reg.DropTask(ctx, taskID); err != nil {
		g.log.Warn("relay: task index cleanup failed", "taskId", taskID, "err", err)
	}
}

// updateRollingLine edits the task's single status message in place.
func (g *Gateway) updateRollingLine(rec *SessionRecord, taskID string, rs *relayState) {
	active := rec.ActiveTask
	if active == nil || active.TaskID != taskID || active.StatusMsgID == "" {
		return
	}
	g.editLine(rec.Key, active.StatusMsgID, statusLine(rs.state, rs.progress))
}

func (g *Gateway) editLine(conversation, messageID, line string) {
	if err := g.adapter.Edit(conversation, messageID, truncateRunes(line, discordChunk)); err != nil {
		g.log.Warn("rolling line edit failed", "conversation", conversation, "err", err)
	}
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
	// before this edit, and the worker adapter's progress deviation (no
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
	g.mu.Lock()
	key := g.taskSessions[taskID]
	g.mu.Unlock()
	if key != "" {
		return key
	}
	key, err := g.reg.SessionForTask(ctx, taskID)
	if err != nil {
		g.log.Error("task index lookup failed", "taskId", taskID, "err", err)
		return ""
	}
	if key != "" {
		g.mu.Lock()
		g.taskSessions[taskID] = key
		g.mu.Unlock()
	}
	return key
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
