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
	ruleDelegationAllowedUsers = "delegation.allowed-users"
	ruleDelegationTarget       = "delegation.target"
	ruleDelegationBusy         = "delegation.busy"
	ruleDelegationDepth        = "delegation.depth"
	ruleDelegationNoRequester  = "delegation.no-requester"
	ruleDelegationStale        = "delegation.stale"
	ruleDelegationMalformed    = "delegation.malformed"
)

// delegatedLineNote suffixes a child's rolling line, so the room can tell the
// task the session handed on from one a human asked for.
const delegatedLineNote = "(delegated to platform)"

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
		refuse(ruleDelegationNoRequester, "⚠️ delegation refused: this turn's requester is no longer on record; ask again")
		return
	}
	if !g.targetAllows(addressee, parent.Requester.Backend, parent.Requester.Subject) {
		refuse(ruleDelegationAllowedUsers, "🚫 not allowed to reach "+targetPlatform+" from here")
		return
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
		Text:          req.Text,
		Requester:     *parent.Requester,
		Authority:     authority,
		CorrelationID: prevActive.CorrelationID,
		Role:          taskRoleChild,
		ParentTaskID:  taskID,
		Depth:         parent.Depth + 1,
		LineNote:      delegatedLineNote,
	})
	if !ok {
		// startTaskWith has said why, in the log and to the room; the
		// parent is still the conversation's task, and a child that never
		// reached the bus leaves no entry in the chain and no route.
		rec.Addressee, rec.ActiveTask = prevAddressee, prevActive
		setParentLine(rec, taskID, "")
		g.dropFailedChildren(ctx, rec, taskID)
		return
	}
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == taskID {
			rec.Tasks[i].Children = append(rec.Tasks[i].Children, childID)
		}
	}
	log.Info("delegation minted", "parent", taskID, "child", childID, "addressee", addressee)
}

// liveChild names a child task of this conversation that has not ended: a
// child entry whose task the gateway still routes. The task index is the
// liveness the relay itself keeps; relayTerminal retires it on the child's
// terminal, from the executor or the supervisor.
func (g *Gateway) liveChild(ctx context.Context, rec *SessionRecord) string {
	for _, ref := range rec.Tasks {
		if ref.Role == taskRoleChild && g.sessionForTask(ctx, ref.ID) != "" {
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

// dropFailedChildren removes the entry and the index of a child of taskID
// whose submission never reached the bus. The parent has no child on record
// yet (the busy check), so any child entry naming it is the failed one.
func (g *Gateway) dropFailedChildren(ctx context.Context, rec *SessionRecord, taskID string) {
	kept := rec.Tasks[:0]
	for _, ref := range rec.Tasks {
		if ref.Role != taskRoleChild || ref.ParentTaskID != taskID {
			kept = append(kept, ref)
			continue
		}
		g.mu.Lock()
		delete(g.taskSessions, ref.ID)
		delete(g.relays, ref.ID)
		g.mu.Unlock()
		if err := g.reg.DropTask(ctx, ref.ID); err != nil {
			g.log.Warn("delegation: failed child's index cleanup failed", "taskId", ref.ID, "err", err)
		}
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
