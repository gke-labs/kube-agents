package hermesbridge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	lib "github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

const (
	kanbanPollInterval  = time.Second
	kanbanMaxEmptyPolls = 3
	// kanbanMaxQueryFailures bounds how many polls in a row may fail to read
	// kanban.db before the turn stops waiting on its cards and ends with its
	// own text: a board that can't be read (no python3 or sqlite3, a schema
	// change, a corrupt file) must not hold every turn to its deadline.
	kanbanMaxQueryFailures      = 30
	kanbanOutOfTurnWindowSecs   = 7200
	kanbanTrackedRetentionSecs  = 2 * kanbanOutOfTurnWindowSecs
	hermesVenvPythonPath        = "/opt/hermes/.venv/bin/python3"
	defaultPythonBin            = "python3"
	maxTrackedKanbanEntries     = 2048
	cardAnswerSubstantiveChars  = 600
	cardSynthesisInputCap       = 120 << 10
	cardSynthesisTimeout        = 4 * time.Minute
	cardSynthesisTruncationNote = "\n\n[... further card results truncated for synthesis ...]"
	emptyCardOutputPlaceholder  = "(no output recorded)"
	silentSentinel              = "[SILENT]"
	defaultKanbanDBPath         = "/opt/data/kanban.db"
	hermesKanbanDBEnv           = "HERMES_KANBAN_DB"
	hermesHomeEnv               = "HERMES_HOME"
	platformAgentHomeEnv        = "PLATFORM_AGENT_HOME"
	kanbanDBFilename            = "kanban.db"
	kanbanQueryTimeout          = 5 * time.Second
	cardStepTitleCap            = 160
	maxSubtaskIDBytes           = 128
	maxSubtaskStatusBytes       = 64
	kanbanBlockedDeliveryPrefix = "blocked:"
	kanbanStatusBlocked         = "blocked"
	defaultResultChunkSize      = 256 << 10

	kanbanDBPyScript = `
import json, sqlite3, sys

mode, db_path, session_id = sys.argv[1], sys.argv[2], sys.argv[3]
settled = ("done", "archived", "completed", "blocked", "failed", "gave_up", "canceled", "cancelled")
settled_sql = ",".join(repr(s) for s in settled)

since_ts = float(sys.argv[4])
tracked = [x for x in (sys.argv[5].split(",") if len(sys.argv) > 5 and sys.argv[5] else []) if x]
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
conn.row_factory = sqlite3.Row
cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
sess_col = "creator_session_id" if "creator_session_id" in cols else "session_id"
err_col = "last_failure_error" if "last_failure_error" in cols else "''"
assignee_col = "assignee" if "assignee" in cols else "''"
select_sql = f"SELECT id, title, status, COALESCE({assignee_col}, '') AS assignee, COALESCE({sess_col}, '') AS session_id, COALESCE(result, '') AS result, COALESCE({err_col}, '') AS err FROM tasks"
conds, params = [f"created_at >= ? OR LOWER(COALESCE(status, '')) NOT IN ({settled_sql})"], [since_ts]
if "updated_at" in cols:
    conds.append("COALESCE(updated_at, 0) >= ?")
    params.append(since_ts)
if session_id == "*":
    rows = conn.execute(f"{select_sql} WHERE {sess_col} LIKE 'a2a-%' AND ({' OR '.join(conds)}) ORDER BY created_at ASC, id ASC", params).fetchall()
else:
    if tracked:
        conds.append(f"id IN ({','.join('?' for _ in tracked)})")
        params.extend(tracked)
    rows = conn.execute(f"{select_sql} WHERE {sess_col} = ? AND ({' OR '.join(conds)}) ORDER BY created_at ASC, id ASC", [session_id] + params).fetchall()

run_cols = set()
try:
    run_cols = {c[1] for c in conn.execute("PRAGMA table_info(task_runs)")}
except Exception:
    pass
ws_col = "session_id" if "session_id" in run_cols else ("worker_session_id" if "worker_session_id" in run_cols else "''")
verd_col = "verdict" if "verdict" in run_cols else "''"
meta_col = "metadata" if "metadata" in run_cols else "''"

out = []
for r in rows:
    item = dict(r)
    if run_cols:
        try:
            rr = conn.execute(
                f"SELECT COALESCE(summary, '') AS summary, COALESCE(error, '') AS error, COALESCE({ws_col}, '') AS worker_session_id, COALESCE({verd_col}, '') AS verdict, COALESCE({meta_col}, '') AS metadata "
                "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (r["id"],)
            ).fetchone()
            if rr:
                item["result"] = item["result"] or rr["summary"]
                item["err"] = item["err"] or rr["error"]
                if rr["worker_session_id"]:
                    item["worker_session_id"] = rr["worker_session_id"]
                if rr["verdict"]:
                    item["verdict"] = rr["verdict"]
                if rr["metadata"]:
                    meta = json.loads(rr["metadata"])
                    if isinstance(meta, dict):
                        if not item.get("worker_session_id") and meta.get("worker_session_id"):
                            item["worker_session_id"] = str(meta["worker_session_id"])
                        if not item.get("verdict"):
                            for vk in ("health_status", "verdict", "status"):
                                if meta.get(vk):
                                    item["verdict"] = str(meta[vk])
                                    break
        except Exception:
            pass
    try:
        ev = conn.execute("SELECT id, kind, COALESCE(payload, '') AS payload FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'unblocked', 'gave_up') ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        if ev:
            ev_kind = (ev["kind"] or "").strip().lower()
            eff_status = (item.get("status") or "").strip().lower()
            if ev_kind == "gave_up" and eff_status == "blocked":
                item["status"] = "gave_up"
                eff_status = "gave_up"
            if ev_kind in ("blocked", "gave_up") and eff_status in ("blocked", "gave_up"):
                if ev["payload"]:
                    item["block_payload"] = ev["payload"]
                if ev["id"] is not None:
                    item["block_event_id"] = int(ev["id"])
    except Exception:
        pass
    out.append(item)
print(json.dumps(out))
`
)

var (
	kanbanNow = time.Now
)

type kanbanTracker struct {
	mu                  sync.Mutex
	activeWaits         map[string]int
	deliveredIDs        map[string]int64
	deliveredOrder      []string
	canceledSessions    map[string]int64
	canceledOrder       []string
	consoleSessions     map[string]consoleSessionMeta
	consoleSessionOrder []string
}

type kanbanCardRow struct {
	ID              string `json:"id"`
	Title           string `json:"title"`
	Status          string `json:"status"`
	Assignee        string `json:"assignee"`
	SessionID       string `json:"session_id"`
	WorkerSessionID string `json:"worker_session_id"`
	Verdict         string `json:"verdict"`
	Result          string `json:"result"`
	Err             string `json:"err"`
	BlockPayload    string `json:"block_payload"`
	BlockEventID    int64  `json:"block_event_id,omitempty"`
}

type consoleSessionMeta struct {
	ContextID string
	Authority json.RawMessage
}

func pruneKanbanTimestampMap(m map[string]int64, order *[]string, cutoff int64) {
	for k, ts := range m {
		if ts < cutoff {
			delete(m, k)
		}
	}
	if len(*order) > 0 {
		kept := (*order)[:0]
		seen := make(map[string]bool, len(*order))
		for _, k := range *order {
			if _, ok := m[k]; ok && !seen[k] {
				kept = append(kept, k)
				seen[k] = true
			}
		}
		*order = kept
	}
	for len(m) > maxTrackedKanbanEntries {
		if len(*order) > 0 {
			oldest := (*order)[0]
			*order = (*order)[1:]
			delete(m, oldest)
			continue
		}
		var oldestKey string
		var oldestTS int64
		first := true
		for k, ts := range m {
			if first || ts < oldestTS || (ts == oldestTS && k < oldestKey) {
				oldestKey, oldestTS, first = k, ts, false
			}
		}
		if !first {
			delete(m, oldestKey)
		} else {
			break
		}
	}
}

func pruneKanbanBoundedMap[V any](m map[string]V, order *[]string) {
	if len(*order) > 0 {
		kept := (*order)[:0]
		seen := make(map[string]bool, len(*order))
		for _, k := range *order {
			if _, ok := m[k]; ok && !seen[k] {
				kept = append(kept, k)
				seen[k] = true
			}
		}
		*order = kept
	}
	for len(m) > maxTrackedKanbanEntries {
		if len(*order) > 0 {
			oldest := (*order)[0]
			*order = (*order)[1:]
			delete(m, oldest)
			continue
		}
		var oldestKey string
		first := true
		for k := range m {
			if first || k < oldestKey {
				oldestKey, first = k, false
			}
		}
		if !first {
			delete(m, oldestKey)
		} else {
			break
		}
	}
}

func (k *kanbanTracker) ensureMapsLocked() {
	if k.activeWaits == nil {
		k.activeWaits = make(map[string]int)
	}
	if k.deliveredIDs == nil {
		k.deliveredIDs = make(map[string]int64)
	}
	if k.canceledSessions == nil {
		k.canceledSessions = make(map[string]int64)
	}
	if k.consoleSessions == nil {
		k.consoleSessions = make(map[string]consoleSessionMeta)
	}
	cutoff := kanbanNow().Unix() - kanbanTrackedRetentionSecs
	pruneKanbanTimestampMap(k.deliveredIDs, &k.deliveredOrder, cutoff)
	pruneKanbanTimestampMap(k.canceledSessions, &k.canceledOrder, cutoff)
	pruneKanbanBoundedMap(k.consoleSessions, &k.consoleSessionOrder)
}

// cardStep is the step record one settled card stands for: the card's id,
// title, status and verdict, and none of its report. The report is text the
// card's worker wrote, which can repeat a value it read, and the step goes to
// TASKS; the bridge's own synthesis reads the report from kanban.db instead.
// A findings preview returns with a live channel that persists nothing
// (#4975).
func cardStep(c kanbanCardRow, settled, total int, parentTaskID string) stepmsg.Step {
	// The title is model-written too (the coordinator names the card when it
	// files it), so it passes the scrubber as a net, like an argument value.
	title := scrubInlineSecrets(strings.TrimSpace(c.Title))
	shortTitle := shortCardClusterOrTitle(title)
	if shortTitle == "" {
		shortTitle = c.ID
	}
	return stepmsg.Step{
		Kind:  stepmsg.KindSubtask,
		Title: capBytes(fmt.Sprintf("Specialist check %s: %s.", cardSettledWord(c.Status), shortTitle), maxStepTitleBytes),
		Subtask: &stepmsg.Subtask{
			ID:           capBytes(c.ID, maxSubtaskIDBytes),
			Title:        capBytes(capRunes(title, cardStepTitleCap), maxStepTitleBytes),
			Status:       capBytes(strings.ToLower(strings.TrimSpace(c.Status)), maxSubtaskStatusBytes),
			Verdict:      capBytes(inferKanbanCardVerdict(c), maxSubtaskStatusBytes),
			Settled:      settled,
			Total:        total,
			ParentTaskID: capBytes(parentTaskID, maxSubtaskIDBytes),
		},
	}
}

func (b *Bridge) markSessionCanceled(sessionID string) {
	if sessionID == "" {
		return
	}
	b.kanban.mu.Lock()
	b.kanban.ensureMapsLocked()
	_, alreadyCanceled := b.kanban.canceledSessions[sessionID]
	if !alreadyCanceled {
		b.kanban.canceledOrder = append(b.kanban.canceledOrder, sessionID)
	}
	b.kanban.canceledSessions[sessionID] = kanbanNow().Unix()
	pruneKanbanTimestampMap(b.kanban.canceledSessions, &b.kanban.canceledOrder, kanbanNow().Unix()-kanbanTrackedRetentionSecs)
	b.kanban.mu.Unlock()

	if !alreadyCanceled {
		go b.abortCanceledSessionKanbanCards(sessionID)
	}
}

func (b *Bridge) abortCanceledSessionKanbanCards(sessionID string) {
	b.stopSessionCards(sessionID)
}

func (b *Bridge) stopSessionCards(sessionID string) {
	if sessionID == "" || b.cfg.RouteURL == "" || b.closing.Load() {
		return
	}
	route := b.recordedRoute(sessionID)
	contextID := strings.TrimSpace(route.ContextID)
	conversation := strings.TrimSpace(route.Conversation)

	b.kanban.mu.Lock()
	meta := b.kanban.consoleSessions[sessionID]
	b.kanban.mu.Unlock()

	if contextID == "" {
		contextID = strings.TrimSpace(meta.ContextID)
	}
	if contextID == "" {
		contextID = strings.TrimPrefix(sessionID, apiSessionIDPrefix)
	}
	if conversation == "" && len(meta.Authority) > 0 {
		if r, err := routeFor(&lib.Envelope{ContextID: contextID, Authority: meta.Authority}); err == nil {
			conversation = r.Conversation
		}
	}

	raw, err := json.Marshal(cardStopRequest{
		ContextID:    contextID,
		Conversation: conversation,
	})
	if err != nil {
		return
	}
	parent := b.stopping
	if parent == nil {
		parent = context.Background()
	}
	timeout := b.cfg.CardStopTimeout
	if timeout <= 0 {
		timeout = DefaultCardStopTimeout
	}
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()

	target := strings.TrimRight(b.cfg.RouteURL, "/") + fmt.Sprintf(cardStopPathFormat, url.PathEscape(sessionID))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, target, bytes.NewReader(raw))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	if key := strings.TrimSpace(b.cfg.RouteKey); key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	client := b.routeClient
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, cardStopResponseCap))
}

func (b *Bridge) rememberConsoleSession(sessionID string, origin *lib.Envelope) {
	if sessionID == "" || origin == nil {
		return
	}
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	b.kanban.ensureMapsLocked()
	if _, exists := b.kanban.consoleSessions[sessionID]; !exists {
		b.kanban.consoleSessionOrder = append(b.kanban.consoleSessionOrder, sessionID)
	}
	b.kanban.consoleSessions[sessionID] = consoleSessionMeta{
		ContextID: origin.ContextID,
		Authority: append(json.RawMessage(nil), origin.Authority...),
	}
	pruneKanbanBoundedMap(b.kanban.consoleSessions, &b.kanban.consoleSessionOrder)
}

func (b *Bridge) beginKanbanWait(sessionID string) {
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	b.kanban.ensureMapsLocked()
	if b.kanban.activeWaits[sessionID] == 0 {
		delete(b.kanban.canceledSessions, sessionID)
	}
	b.kanban.activeWaits[sessionID]++
}

func (b *Bridge) endKanbanWait(sessionID string) {
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	if b.kanban.activeWaits[sessionID] > 0 {
		b.kanban.activeWaits[sessionID]--
		if b.kanban.activeWaits[sessionID] == 0 {
			delete(b.kanban.activeWaits, sessionID)
		}
	}
}

func (b *Bridge) isSessionCanceled(sessionID string) bool {
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	b.kanban.ensureMapsLocked()
	_, canceled := b.kanban.canceledSessions[sessionID]
	return canceled
}

func (b *Bridge) hasActiveKanbanWait(sessionID string) bool {
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	return b.kanban.activeWaits[sessionID] > 0
}

func (b *Bridge) markKanbanCardDeliveredForStatus(cardID, status string, blockEventID ...int64) {
	key := kanbanDeliveryKey(cardID, status, blockEventID...)
	if key == "" {
		return
	}
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	b.kanban.ensureMapsLocked()
	now := kanbanNow().Unix()
	if _, exists := b.kanban.deliveredIDs[key]; !exists {
		b.kanban.deliveredOrder = append(b.kanban.deliveredOrder, key)
	}
	b.kanban.deliveredIDs[key] = now
	pruneKanbanTimestampMap(b.kanban.deliveredIDs, &b.kanban.deliveredOrder, now-kanbanTrackedRetentionSecs)
}

func (b *Bridge) clearBlockedKanbanCardDelivery(cardID string) {
	if cardID == "" {
		return
	}
	key := kanbanBlockedDeliveryPrefix + cardID
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	b.kanban.ensureMapsLocked()
	delete(b.kanban.deliveredIDs, key)
}

func (b *Bridge) isKanbanCardDeliveredForStatus(cardID, status string, blockEventID ...int64) bool {
	if cardID == "" {
		return false
	}
	key := kanbanDeliveryKey(cardID, status, blockEventID...)
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	b.kanban.ensureMapsLocked()
	if _, terminalDelivered := b.kanban.deliveredIDs[cardID]; terminalDelivered {
		return true
	}
	_, delivered := b.kanban.deliveredIDs[key]
	return delivered
}

func hasQueuedSteer(run *taskRun) bool {
	run.mu.Lock()
	defer run.mu.Unlock()
	return run.state == stateRunning && len(run.steers) > 0
}

func (b *Bridge) resolveKanbanDBPath() string {
	return resolveKanbanDBPath()
}

func resolveKanbanDBPath() string {
	for _, envKey := range []string{hermesKanbanDBEnv, hermesHomeEnv, platformAgentHomeEnv} {
		if v := strings.TrimSpace(os.Getenv(envKey)); v != "" {
			p := v
			if envKey != hermesKanbanDBEnv {
				p = filepath.Join(v, kanbanDBFilename)
			}
			if info, err := os.Stat(p); err == nil && !info.IsDir() {
				return p
			}
		}
	}
	if info, err := os.Stat(defaultKanbanDBPath); err == nil && !info.IsDir() {
		return defaultKanbanDBPath
	}
	return ""
}

func resolvePythonBin() string {
	if info, err := os.Stat(hermesVenvPythonPath); err == nil && !info.IsDir() {
		return hermesVenvPythonPath
	}
	return defaultPythonBin
}

func isKanbanStatusBlocked(status string) bool {
	return strings.EqualFold(strings.TrimSpace(status), kanbanStatusBlocked)
}

func isKanbanStatusTerminal(status string) bool {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "done", "archived", "completed", "failed", "gave_up", "canceled", "cancelled":
		return true
	default:
		return false
	}
}

func isKanbanStatusSettled(status string) bool {
	return isKanbanStatusBlocked(status) || isKanbanStatusTerminal(status)
}

func kanbanDeliveryKey(cardID, status string, blockEventID ...int64) string {
	if cardID == "" {
		return ""
	}
	if isKanbanStatusBlocked(status) {
		if len(blockEventID) > 0 && blockEventID[0] > 0 {
			return fmt.Sprintf("%s%s:%d", kanbanBlockedDeliveryPrefix, cardID, blockEventID[0])
		}
		return kanbanBlockedDeliveryPrefix + cardID
	}
	return cardID
}

// knownKanbanVerdicts are the verdict words a subtask step may carry. The
// card's verdict comes from task_runs.metadata, a dict the worker's model
// writes when it completes the card, so it is free text: anything outside
// this set is replaced by the card's status (kanban's own column), since the
// step goes to TASKS.
var knownKanbanVerdicts = map[string]bool{
	"pass": true, "passed": true, "ok": true, "success": true, "succeeded": true, "healthy": true,
	"fail": true, "failed": true, "failure": true, "unhealthy": true, "degraded": true, "critical": true,
	"warn": true, "warning": true, "error": true, "unknown": true, "inconclusive": true,
	"blocked": true, "done": true, "completed": true, "skipped": true, "canceled": true, "cancelled": true,
}

func inferKanbanCardVerdict(c kanbanCardRow) string {
	if v := strings.ToLower(strings.TrimSpace(c.Verdict)); knownKanbanVerdicts[v] {
		return v
	}
	if st := strings.ToLower(strings.TrimSpace(c.Status)); st != "" {
		return st
	}
	return "completed"
}

// cardAnswerText is what of a settled card may stand in a task's answer when
// no synthesis turn rewrites it (one card, or a failed synthesis): a blocked
// card's question, which the user has to answer; a completed card's report,
// which is the answer the user asked for; and for any other end, a line
// naming the card and how it ended. Its error text is the debugging switch's
// to add (BRIDGE_DEBUG_ERROR_TEXT), as for a tool call's.
func cardAnswerText(c kanbanCardRow) string {
	st := strings.ToLower(strings.TrimSpace(c.Status))
	switch {
	case isKanbanStatusBlocked(st):
		if q := blockedCardQuestion(c); q != "" {
			return q
		}
	case kanbanStatusSucceeded(st):
		if res := strings.TrimSpace(c.Result); res != "" {
			return res
		}
	case debugErrorTextOn():
		// As on an activity entry: the error's first line (else the run's),
		// scrubbed and capped.
		text := strings.TrimSpace(c.Err)
		if text == "" {
			text = strings.TrimSpace(formatKanbanCardOutput(c))
		}
		if out := oneLinePreview(scrubInlineSecrets(text), debugErrorTextCap); out != "" {
			return out
		}
	}
	title := shortCardClusterOrTitle(scrubInlineSecrets(strings.TrimSpace(c.Title)))
	if title == "" {
		return fmt.Sprintf("Kanban card `%s` finished with status `%s`.", c.ID, st)
	}
	return fmt.Sprintf("Kanban card `%s` (%s) finished with status `%s`.", c.ID, title, st)
}

// blockedCardQuestion is what a blocked card asks the user, from its block
// payload's question, reason, message or summary (not its error), or "".
func blockedCardQuestion(c kanbanCardRow) string {
	bp := strings.TrimSpace(c.BlockPayload)
	if bp == "" {
		return ""
	}
	var parsed map[string]any
	if json.Unmarshal([]byte(bp), &parsed) != nil {
		return bp // a plain-text payload is the question itself
	}
	for _, k := range []string{"question", "reason", "message", "summary"} {
		if v, ok := parsed[k].(string); ok && strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

// kanbanStatusSucceeded reports whether a card's status is a successful end.
// cardSettledWord is the one word a settled card's step title gives its end,
// so a blocked or failed card is not announced as a completed check.
func cardSettledWord(status string) string {
	switch st := strings.ToLower(strings.TrimSpace(status)); {
	case kanbanStatusSucceeded(st):
		return "completed"
	case isKanbanStatusBlocked(st):
		return "blocked"
	case st == "canceled" || st == "cancelled":
		return "canceled"
	case st == "failed" || st == "gave_up":
		return "failed"
	default:
		return "settled"
	}
}

func kanbanStatusSucceeded(status string) bool {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "done", "completed", "archived":
		return true
	}
	return false
}

func formatKanbanCardOutput(c kanbanCardRow) string {
	if isKanbanStatusBlocked(c.Status) {
		if bp := strings.TrimSpace(c.BlockPayload); bp != "" {
			var parsed map[string]any
			if json.Unmarshal([]byte(bp), &parsed) == nil {
				for _, k := range []string{"question", "reason", "message", "summary", "error"} {
					if v, ok := parsed[k].(string); ok && strings.TrimSpace(v) != "" {
						return strings.TrimSpace(v)
					}
				}
			}
			return bp
		}
	}
	if res := strings.TrimSpace(c.Result); res != "" {
		return res
	}
	if !isKanbanStatusBlocked(c.Status) {
		if errText := strings.TrimSpace(c.Err); errText != "" {
			return errText
		}
	}
	if bp := strings.TrimSpace(c.BlockPayload); bp != "" {
		var parsed map[string]any
		if json.Unmarshal([]byte(bp), &parsed) == nil {
			for _, k := range []string{"question", "reason", "message", "summary", "error"} {
				if v, ok := parsed[k].(string); ok && strings.TrimSpace(v) != "" {
					return strings.TrimSpace(v)
				}
			}
			if errText := strings.TrimSpace(c.Err); errText != "" {
				return errText
			}
		}
	}
	return strings.TrimSpace(c.Err)
}

func querySessionKanbanCards(ctx context.Context, pythonBin, dbPath, sessionID string, sinceUnix int64, trackedIDs []string) ([]kanbanCardRow, error) {
	qCtx, cancel := context.WithTimeout(ctx, kanbanQueryTimeout)
	defer cancel()
	cmd := exec.CommandContext(qCtx, pythonBin, "-c", kanbanDBPyScript, "poll", dbPath, sessionID, strconv.FormatInt(sinceUnix, 10), strings.Join(trackedIDs, ","))
	out, err := cmd.Output()
	if err != nil {
		// The script's traceback names the cause (a corrupt file, a schema
		// change, no sqlite3 module); the exit status alone does not.
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) && len(exitErr.Stderr) > 0 {
			return nil, fmt.Errorf("%w: %s", err, tail(strings.TrimSpace(string(exitErr.Stderr)), apiBodyTailBytes))
		}
		return nil, err
	}
	var rows []kanbanCardRow
	if err := json.Unmarshal(out, &rows); err != nil {
		return nil, err
	}
	return rows, nil
}

func (b *Bridge) waitForKanbanCards(run *taskRun, taskCtx context.Context, act *activityState, sessionID string, turnStartUnix int64, turn int, ackText string) (string, bool) {
	var trackedIDs []string
	turnFiledCards := act != nil && act.cardsFiled.Load()
	b.beginKanbanWait(sessionID)
	defer func() {
		if run != nil && run.canceled.Load() && (turnFiledCards || (act != nil && act.cardsFiled.Load()) || len(trackedIDs) > 0) {
			b.markSessionCanceled(sessionID)
		}
		b.endKanbanWait(sessionID)
	}()

	dbPath := b.resolveKanbanDBPath()
	if dbPath == "" {
		return ackText, true
	}
	pythonBin := resolvePythonBin()

	run.mu.Lock()
	if run.state != stateRunning {
		run.mu.Unlock()
		return "", false
	}
	// The turn's answer is finished and stays held while its cards run, so a
	// shutdown leaves the run to this worker (shutdownTasks), which sees
	// closing at its next poll and ends the task with that answer.
	run.mu.Unlock()

	// waitEnded decides a wait whose taskCtx ended: a cancel ends it as
	// finalizeAPIError does (completed, with the answer, when no card was
	// filed), and the deadline keeps the finished answer rather than failing
	// a turn whose own work is done.
	waitEnded := func() (keepAnswer bool) {
		if run.canceled.Load() && !b.closing.Load() && !run.deadlineHit.Load() {
			return !turnFiledCards && (act == nil || !act.cardsFiled.Load()) && len(trackedIDs) == 0
		}
		if run.canceled.Load() {
			return false
		}
		b.cfg.Logger.Warn("the task's deadline or a shutdown ended the wait on its cards; the turn ends with its own answer", "task", run.origin.TaskID)
		return true
	}

	if act != nil {
		act.inTurn.Store(false)
		act.waitingCard.Store(true)
		defer func() {
			act.waitingCard.Store(false)
			act.inTurn.Store(true)
		}()
	}

	trackedSet := make(map[string]bool)
	sawUnsettled := false
	emptyPolls := 0
	queryFailures := 0
	ticker := time.NewTicker(kanbanPollInterval)
	defer ticker.Stop()

poll:
	for {
		if act != nil && act.cardsFiled.Load() {
			turnFiledCards = true
		}
		cards, err := querySessionKanbanCards(taskCtx, pythonBin, dbPath, sessionID, turnStartUnix, trackedIDs)
		if err == nil {
			queryFailures = 0
			var relevant []kanbanCardRow
			for _, c := range cards {
				if !isKanbanStatusBlocked(c.Status) {
					b.clearBlockedKanbanCardDelivery(c.ID)
				}
				if isKanbanStatusSettled(c.Status) && !trackedSet[c.ID] && b.isKanbanCardDeliveredForStatus(c.ID, c.Status, c.BlockEventID) {
					continue
				}
				if !trackedSet[c.ID] {
					trackedSet[c.ID] = true
					trackedIDs = append(trackedIDs, c.ID)
				}
				relevant = append(relevant, c)
			}
			if len(relevant) == 0 {
				if act == nil || !act.cardsFiled.Load() {
					break
				}
				emptyPolls++
				if emptyPolls >= kanbanMaxEmptyPolls {
					break
				}
			} else {
				settledCount := 0
				for _, c := range relevant {
					if act != nil {
						act.SyncKanbanCard(c.ID, c.Title, c.WorkerSessionID)
					}
					if isKanbanStatusSettled(c.Status) {
						settledCount++
					}
				}
				totalCount := len(relevant)
				if settledCount < totalCount {
					sawUnsettled = true
				}
				for _, c := range relevant {
					if !isKanbanStatusSettled(c.Status) || (act != nil && !act.markCardSettledReported(kanbanDeliveryKey(c.ID, c.Status, c.BlockEventID))) {
						continue
					}
					st := cardStep(c, settledCount, totalCount, "")
					progText := fmt.Sprintf("Kanban specialist checks: %d/%d settled (%d running) — latest: %s (%s)",
						settledCount, totalCount, totalCount-settledCount, st.Subtask.Title, st.Subtask.Verdict)
					run.mu.Lock()
					if act != nil {
						b.publishSteps(run, act, []stepmsg.Step{st})
						if run.state == stateRunning && act.heartbeatUnderBudget() {
							b.publishProgress(run, progText)
						}
					}
					run.mu.Unlock()
				}

				if settledCount == totalCount {
					var parts []string
					for _, c := range relevant {
						b.markKanbanCardDeliveredForStatus(c.ID, c.Status, c.BlockEventID)
						if out := cardAnswerText(c); out != "" {
							parts = append(parts, out)
						}
					}
					if len(parts) > 0 {
						run.mu.Lock()
						if act != nil {
							b.publishSteps(run, act, []stepmsg.Step{{
								Kind:    stepmsg.KindSynthesis,
								Title:   fmt.Sprintf("Synthesizing findings across %d specialist card(s).", len(relevant)),
								Thought: "Combining specialist reports into unified fleet response.",
							}})
						}
						run.mu.Unlock()
						if !turnFiledCards {
							cardReport := b.synthesizeCardAnswer(run, taskCtx, sessionID, turn, "", relevant, parts)
							if act != nil {
								act.cardsFiled.Store(false)
							}
							if taskCtx.Err() != nil {
								b.finalizeAPIError(run, taskCtx, taskCtx.Err(), turn)
								return "", false
							}
							if trimmedAck := strings.TrimSpace(ackText); trimmedAck != "" && !isSilentAnswer(trimmedAck) {
								if trimmedReport := strings.TrimSpace(cardReport); trimmedReport != "" {
									ackText = trimmedAck + "\n\n" + trimmedReport
								} else {
									ackText = trimmedAck
								}
							} else {
								ackText = cardReport
							}
						} else {
							synthAck := ackText
							if sawUnsettled {
								synthAck = ""
							}
							ackText = b.synthesizeCardAnswer(run, taskCtx, sessionID, turn, synthAck, relevant, parts)
							if act != nil {
								act.cardsFiled.Store(false)
							}
							if taskCtx.Err() != nil {
								b.finalizeAPIError(run, taskCtx, taskCtx.Err(), turn)
								return "", false
							}
						}
					} else if act != nil {
						act.cardsFiled.Store(false)
					}
					break
				}
			}
		} else if taskCtx.Err() != nil {
			if waitEnded() {
				break
			}
			b.finalizeAPIError(run, taskCtx, taskCtx.Err(), turn)
			return "", false
		} else {
			queryFailures++
			if queryFailures == 1 {
				b.cfg.Logger.Warn("can't read the kanban board; still waiting on the turn's cards", "task", run.origin.TaskID, "err", err)
			}
			if queryFailures >= kanbanMaxQueryFailures {
				b.cfg.Logger.Error("the kanban board stayed unreadable; the turn ends without waiting on its cards",
					"task", run.origin.TaskID, "failures", queryFailures, "err", err)
				break
			}
		}

		if hasQueuedSteer(run) {
			if sawUnsettled && act != nil {
				act.cardsFiled.Store(true)
			}
			break
		}

		if b.closing.Load() && !run.canceled.Load() {
			b.cfg.Logger.Warn("shutdown ended the wait on the turn's cards; the turn ends with its own answer", "task", run.origin.TaskID)
			break
		}

		select {
		case <-taskCtx.Done():
			if waitEnded() {
				break poll
			}
			b.finalizeAPIError(run, taskCtx, taskCtx.Err(), turn)
			return "", false
		case <-ticker.C:
		}
	}

	run.mu.Lock()
	defer run.mu.Unlock()
	if run.state != stateRunning {
		return "", false
	}
	run.answerHeld = true
	return ackText, true
}

func (b *Bridge) synthesizeCardAnswer(run *taskRun, taskCtx context.Context, sessionID string, turn int, ackText string, cards []kanbanCardRow, parts []string) string {
	joined := strings.Join(parts, "\n\n")
	trimmedAck := strings.TrimSpace(ackText)
	carriedOver := turn > 1 && trimmedAck != "" && !isSilentAnswer(trimmedAck) &&
		(run == nil || run.act.Load() == nil || !run.act.Load().cardsFiled.Load())

	if !carriedOver && len(trimmedAck) >= cardAnswerSubstantiveChars && !isSilentAnswer(ackText) {
		return ackText
	}
	if len(cards) == 1 {
		if carriedOver && joined != "" {
			return trimmedAck + "\n\n" + joined
		}
		return joined
	}
	if len(joined) > cardSynthesisInputCap {
		joined = capBytes(joined, cardSynthesisInputCap) + cardSynthesisTruncationNote
	}
	var sb strings.Builder
	fmt.Fprintf(&sb, "All %d specialist card(s) you filed for this request have now completed. Their results are below. ", len(cards))
	sb.WriteString("IMPORTANT: this conversation has no chat thread, so the gateway has NOT posted these results anywhere; ")
	sb.WriteString("the user has seen nothing yet and your reply is the only answer they will get. [SILENT] is therefore not an ")
	sb.WriteString("acceptable reply here. ")
	sb.WriteString("Write the final answer to the user's original request as a synthesis: lead with the overall verdict, ")
	sb.WriteString("then a compact per-item table (one row per card/cluster) with status and the one key fact, then only the ")
	sb.WriteString("findings that need action with concrete next steps. Do not paste the raw reports; keep it under about ")
	sb.WriteString("400 words unless the user explicitly asked for full detail. Do not run more tools or file more cards.\n\n")
	for i, c := range cards {
		if sb.Len() >= cardSynthesisInputCap {
			break
		}
		title := strings.TrimSpace(c.Title)
		if title == "" {
			title = c.ID
		}
		cardOut := strings.TrimSpace(formatKanbanCardOutput(c))
		if cardOut == "" {
			cardOut = emptyCardOutputPlaceholder
		}
		fmt.Fprintf(&sb, "### Card %d: %s (%s)\n%s\n\n", i+1, title, strings.TrimSpace(c.Status), cardOut)
	}
	prompt := sb.String()
	if len(prompt) > cardSynthesisInputCap {
		budget := cardSynthesisInputCap - len(cardSynthesisTruncationNote)
		if budget < 0 {
			budget = cardSynthesisInputCap
		}
		prompt = capBytes(prompt, budget) + cardSynthesisTruncationNote
	}
	synth, ok := b.apiSynthesisTurn(run, taskCtx, sessionID, prompt, turn)
	if !ok || isSilentAnswer(synth) {
		if carriedOver && joined != "" {
			return trimmedAck + "\n\n" + joined
		}
		return joined
	}
	if carriedOver {
		return trimmedAck + "\n\n" + strings.TrimSpace(synth)
	}
	return synth
}

func isSilentAnswer(text string) bool {
	t := strings.TrimSpace(text)
	return t == "" || strings.EqualFold(t, silentSentinel) || strings.EqualFold(strings.Trim(t, "`*_ "), silentSentinel)
}

func (b *Bridge) stopInterruptedSynthesisTurn(run *taskRun, sessionID string, err error) {
	if b.cfg.Logger == nil {
		b.cfg.Logger = slog.Default()
	}
	if run != nil && run.origin != nil && run.canceled.Load() {
		b.stopCanceledTurn(run, sessionID, err)
		return
	}
	if !b.closing.Load() && !errors.Is(err, errNeverConnected) {
		_, _ = b.postTurnStop(sessionID)
	}
}

func (b *Bridge) apiSynthesisTurn(run *taskRun, taskCtx context.Context, sessionID, prompt string, turn int) (string, bool) {
	if b.cfg.Logger == nil {
		b.cfg.Logger = slog.Default()
	}
	var taskID string
	if run != nil && run.origin != nil {
		taskID = run.origin.TaskID
	}
	body, err := json.Marshal(apiChatRequest{
		Model:    b.cfg.APIModel,
		Messages: []apiChatMessage{{Role: "user", Content: prompt}},
	})
	if err != nil {
		return "", false
	}
	ctx, cancel := context.WithTimeout(taskCtx, cardSynthesisTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, b.cfg.APIURL, bytes.NewReader(body))
	if err != nil {
		return "", false
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+b.cfg.APIKey)
	req.Header.Set(apiSessionKeyHeader, sessionID)
	req.Header.Set(apiSessionIDHeader, sessionID)
	req.Header.Set(apiIdempotencyHeader, fmt.Sprintf("%s/synth-%d", taskID, turn))

	resp, connectedAt, err := b.sendAPI(ctx, req, body)
	if err != nil {
		if taskCtx.Err() != nil || ctx.Err() != nil {
			b.stopInterruptedSynthesisTurn(run, sessionID, err)
		}
		b.cfg.Logger.Warn("card synthesis turn failed; delivering raw card results", "task", taskID, "err", err)
		return "", false
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, apiResponseCap+1))
	took := time.Since(connectedAt)
	if err != nil && (taskCtx.Err() != nil || ctx.Err() != nil) {
		b.stopInterruptedSynthesisTurn(run, sessionID, err)
		b.cfg.Logger.Warn("card synthesis turn interrupted; delivering raw card results", "task", taskID, "err", err)
		return "", false
	}
	if err != nil || resp.StatusCode < 200 || resp.StatusCode > 299 || len(raw) > apiResponseCap {
		b.cfg.Logger.Warn("card synthesis turn refused; delivering raw card results",
			"task", taskID, "status", resp.StatusCode, "err", err)
		return "", false
	}
	var out apiChatResponse
	if json.Unmarshal(raw, &out) != nil || len(out.Choices) == 0 || apiTurnFailed(&out) {
		return "", false
	}
	content := out.Choices[0].Message.Content
	if opensWithUnstructuredReasoning(content) {
		b.cfg.Logger.Warn("card synthesis response opened with unstructured reasoning; delivering raw card results",
			"task", taskID)
		return "", false
	}
	if run == nil {
		return content, true
	}
	run.mu.Lock()
	defer run.mu.Unlock()
	if run.state != stateRunning {
		return "", false
	}
	u, dropped := apiTurnUsage(&out, b.cfg.APIModel, took)
	if len(dropped) > 0 {
		b.cfg.Logger.Warn("usage telemetry on the card synthesis response did not decode; those figures are not on the bus",
			"task", taskID, "fields", dropped)
	}
	if b.c != nil {
		b.publishUsageLocked(run, u)
	}
	return content, true
}
