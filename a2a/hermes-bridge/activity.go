package hermesbridge

// The activity door: how the persona's tool calls become the task's
// `activity` artifact, and its heartbeat the `progress` artifact.
//
// Under -Q hermes writes nothing to stdout until the final response, so the
// bridge cannot learn about tool calls from the pipe it already holds. What
// hermes does offer is its outbound webhooks (agent/outbound_webhooks.py): a
// `hooks.outbound` config entry POSTs every pre_tool_call and post_tool_call
// to a URL, fire-and-forget through a bounded queue, signed with HMAC-SHA256
// when the variable named by `secret_env` is set. The bridge listens on a
// loopback address (the sidecar shares the pod's network namespace), and the
// platform profile's config carries one entry pointing at it
// (a2a/persona/platform/hooks.overlay.yaml, merged in only under mode: next).
//
// Correlation is the signature. Nothing in the delivery names the A2A task:
// hermes's own task_id is the kanban card or a fresh UUID, cwd and profile
// are shared by every process under the profile, and the URL does not expand
// environment variables. So each child gets a random key in its environment
// under ActivitySecretEnv, and a delivery belongs to whichever in-flight task's
// key verifies its signature - at most Concurrency keys to try. Unsigned or
// unmatched deliveries (kanban workers and cron ticks under the same profile)
// are answered 204 and dropped, so they cost their sender nothing per call.
//
// Trust boundary, stated: everything in the pod is reachable from the
// persona's own terminal tool, its environment included. The trace is "as
// reported by the executor's process", the worker adapter's posture too; the
// key rejects cross-talk, not adversaries.

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// DefaultActivityListen is the loopback address the door binds. It is
	// the host:port in the profile's hooks.overlay.yaml, and a test pins the
	// two to each other. The pod's other listeners are hermes's API server
	// on 8642 and the agent-api-auth container on 8643 (bound on every
	// interface, so a loopback bind there fails too), the dashboard on 9119;
	// 8651 is clear of all of them.
	DefaultActivityListen = "127.0.0.1:8651"
	// ActivityPath is the door's one route; hooks.overlay.yaml names it too.
	ActivityPath = "/hermes/tool-events"
	// ActivitySecretEnv is the variable hermes reads the signing key from
	// (the `secret_env` of the hooks.outbound entry); the bridge sets it per
	// child. In a single-profile CLI process hermes's secret scope falls
	// through to the process environment, which is what makes this work.
	ActivitySecretEnv = "A2A_ACTIVITY_SECRET"
	// ActivityURLEnv tells the child where its deliveries go. hermes does
	// not read it - the URL lives in its config - it is for the record and
	// for test stubs standing in for hermes.
	ActivityURLEnv = "A2A_ACTIVITY_URL"
	// DefaultProgressInterval is the heartbeat cadence. The relay renders
	// progress as one edited chat line, so this is one edit per minute.
	DefaultProgressInterval = 60 * time.Second

	// activityInputCap bounds one call's redacted input on the bus; it is the
	// tool_call_audit plugin's own _PAYLOAD_LOG_LIMIT.
	activityInputCap = 2048
	// activityInputHead is how much of an over-cap input survives, as text.
	activityInputHead = 1024
	// activityBodyCap bounds one delivery read; hermes's own payloads are
	// tool inputs and results, never more than a few KiB.
	activityBodyCap = 1 << 20
	// activityPublishTimeout bounds one artifact publish; the trace is
	// telemetry and must never stall the run or the terminal. hermes waits
	// on the delivery for hooks.overlay.yaml's timeout, which is longer, so
	// a slow publish is not a retried (duplicated) delivery.
	activityPublishTimeout = 5 * time.Second
	// activityDrainTimeout bounds finalize's flush of the calls still open,
	// so a slow bus cannot spend the terminal's budget.
	activityDrainTimeout = 5 * time.Second
	// activitySeenCap bounds the delivery ids remembered per task for
	// dedupe; hermes retries a delivery at most once, so the set only
	// grows with the calls.
	activitySeenCap  = 8192
	activityKeyBytes = 32
	// The door's HTTP timeouts: a client on loopback that has not sent its
	// headers or body in these is broken, and the response is one status
	// line. activityShutdownTimeout bounds Serve's drain on bridge exit.
	activityReadHeaderTimeout = 5 * time.Second
	activityReadTimeout       = 10 * time.Second
	activityWriteTimeout      = 5 * time.Second
	activityShutdownTimeout   = 2 * time.Second

	hookPreToolCall     = "pre_tool_call"
	hookPostToolCall    = "post_tool_call"
	hookSignatureHeader = "X-Hermes-Signature-256"
	hookSignaturePrefix = "sha256="
	hookStatusOK        = "ok"

	// Statuses on the activity entry. completed and error are the api path's
	// vocabulary; interrupted is a call whose end never arrived before the
	// task's terminal - deadline, cancel, or a crash mid-tool.
	ActivityStatusCompleted   = "completed"
	ActivityStatusError       = "error"
	ActivityStatusInterrupted = "interrupted"

	redactedValue = "[redacted]"
)

// Two redactions, because the worker adapter publishes tool_use input
// verbatim and this stream is retained for days and copied into eval
// records. redactedKeyPattern names input keys whose values never go on the
// bus. redactedValuePatterns catch the credential shapes a value can carry
// under an innocent key - a terminal command is one string under "command" -
// and are best-effort by nature: a bearer token, Google OAuth access token,
// Google API key, GitHub token, or a "key=value" pair whose key looks like
// a secret. Anything else the model pastes into a command line ships.
var (
	redactedKeyPattern    = regexp.MustCompile(`(?i)token|secret|password|passwd|authorization|api[_-]?key|credential`)
	redactedValuePatterns = []*regexp.Regexp{
		regexp.MustCompile(`(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}`),
		regexp.MustCompile(`ya29\.[A-Za-z0-9._-]{20,}`),
		regexp.MustCompile(`AIza[0-9A-Za-z_-]{35}`),
		regexp.MustCompile(`gh[pousr]_[A-Za-z0-9]{20,}`),
		regexp.MustCompile(`(?i)(token|secret|password|passwd|api[_-]?key|credential)s?[=:]\s*\S+`),
	}
)

// ActivityEntry is one data part of the activity artifact: one tool
// invocation. tool and input are the worker adapter's shape; the rest is
// what hermes's hook adds. Results are deliberately absent - no check reads
// them and they are the riskiest payload in the pod.
type ActivityEntry struct {
	Tool   string          `json:"tool"`
	Input  json.RawMessage `json:"input,omitempty"`
	CallID string          `json:"callId,omitempty"`
	Status string          `json:"status,omitempty"`
	// ErrorType keeps hermes's own verdict when Status is error: its
	// status word (blocked, cancelled, timeout, error) or error_type
	// (tool_error), so a guardrail refusal stays distinguishable from a
	// tool failure in the trace.
	ErrorType  string `json:"errorType,omitempty"`
	DurationMs int64  `json:"durationMs,omitempty"`
	At         string `json:"at,omitempty"`
}

// hookDelivery is the subset of hermes's outbound webhook body the door
// reads (agent/shell_hooks.py _payload_fields plus the delivery metadata).
type hookDelivery struct {
	Event      string          `json:"hook_event_name"`
	ToolName   string          `json:"tool_name"`
	ToolInput  json.RawMessage `json:"tool_input"`
	Timestamp  string          `json:"timestamp"`
	DeliveryID string          `json:"delivery_id"`
	Extra      struct {
		ToolCallID string `json:"tool_call_id"`
		DurationMs int64  `json:"duration_ms"`
		Status     string `json:"status"`
		ErrorType  string `json:"error_type"`
	} `json:"extra"`
}

// activityState is one task's side of the door.
type activityState struct {
	// key signs this task's deliveries: the child's env value, verbatim.
	// hermes HMACs with the secret's text bytes (target.secret.encode()),
	// so the hex string is the key, not the bytes it spells.
	key string

	mu        sync.Mutex
	open      map[string]ActivityEntry // calls started and not yet ended
	openOrder []string                 // their ids, in start order
	seen      map[string]struct{}      // delivery ids, so a hermes retry is one call
	calls     int
	lastTool  string
	startedAt time.Time
	appended  map[string]bool // artifact name -> a first part went out

	// The heartbeat goroutine's lifecycle. Entries are not queued: the door
	// publishes each one on the delivering request, under run.mu, so no
	// entry can sit between a queue and a drain when finalize runs.
	stop     chan struct{}
	stopOnce sync.Once
	done     chan struct{}
}

func newActivityState(withKey bool) (*activityState, error) {
	a := &activityState{
		open:      make(map[string]ActivityEntry),
		seen:      make(map[string]struct{}),
		startedAt: time.Now(),
		appended:  make(map[string]bool),
		stop:      make(chan struct{}),
		done:      make(chan struct{}),
	}
	if withKey {
		raw := make([]byte, activityKeyBytes)
		if _, err := rand.Read(raw); err != nil {
			return nil, fmt.Errorf("activity key: %w", err)
		}
		a.key = hex.EncodeToString(raw)
	}
	return a, nil
}

// childEnv is what the door adds to the hermes child's environment.
func (a *activityState) childEnv(url string) []string {
	if a.key == "" {
		return nil
	}
	return []string{
		ActivitySecretEnv + "=" + a.key,
		ActivityURLEnv + "=" + url,
	}
}

// signed reports whether sig (the X-Hermes-Signature-256 header) is this
// task's HMAC over body.
func (a *activityState) signed(sig string, body []byte) bool {
	if a.key == "" || !strings.HasPrefix(sig, hookSignaturePrefix) {
		return false
	}
	got, err := hex.DecodeString(strings.TrimPrefix(sig, hookSignaturePrefix))
	if err != nil {
		return false
	}
	mac := hmac.New(sha256.New, []byte(a.key))
	mac.Write(body)
	return hmac.Equal(got, mac.Sum(nil))
}

// observe records one delivery. A start opens a call; an end closes it and
// yields the entry to publish. Ends are the entries (one per invocation, the
// spec's rule); starts exist so an interrupted call can still be reported.
func (a *activityState) observe(d hookDelivery) (ActivityEntry, bool) {
	id := d.Extra.ToolCallID
	if id == "" {
		// Inline executors carry no call id; the tool name pairs start to
		// end well enough for the interrupted flush, which is all the id
		// is for.
		id = "tool:" + d.ToolName
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if d.DeliveryID != "" {
		if _, dup := a.seen[d.DeliveryID]; dup {
			return ActivityEntry{}, false
		}
		if len(a.seen) < activitySeenCap {
			a.seen[d.DeliveryID] = struct{}{}
		}
	}
	a.lastTool = d.ToolName
	switch d.Event {
	case hookPreToolCall:
		if _, seen := a.open[id]; !seen {
			a.openOrder = append(a.openOrder, id)
		}
		a.open[id] = ActivityEntry{
			Tool:   d.ToolName,
			Input:  redactInput(d.ToolInput),
			CallID: d.Extra.ToolCallID,
			At:     d.Timestamp,
		}
		return ActivityEntry{}, false
	case hookPostToolCall:
		if _, seen := a.open[id]; seen {
			delete(a.open, id)
			a.openOrder = removeString(a.openOrder, id)
		}
		a.calls++
		status := activityStatus(d)
		e := ActivityEntry{
			Tool:       d.ToolName,
			Input:      redactInput(d.ToolInput),
			CallID:     d.Extra.ToolCallID,
			Status:     status,
			DurationMs: d.Extra.DurationMs,
			At:         d.Timestamp,
		}
		if status == ActivityStatusError {
			e.ErrorType = activityErrorType(d)
		}
		return e, true
	}
	return ActivityEntry{}, false
}

// interrupted returns the calls still open, in start order, as entries, and
// forgets them. Called once, from finalize.
func (a *activityState) interrupted() []ActivityEntry {
	a.mu.Lock()
	defer a.mu.Unlock()
	out := make([]ActivityEntry, 0, len(a.openOrder))
	for _, id := range a.openOrder {
		e := a.open[id]
		e.Status = ActivityStatusInterrupted
		out = append(out, e)
	}
	a.open = make(map[string]ActivityEntry)
	a.openOrder = nil
	return out
}

// progressLine is the heartbeat text: what a reader of the rolling line, or
// of a stalled task's probe, needs to tell slow from stuck.
func (a *activityState) progressLine(now time.Time) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	line := fmt.Sprintf("running %s, %d tool call(s)", now.Sub(a.startedAt).Round(time.Second), a.calls)
	if a.lastTool != "" {
		line += ", last " + a.lastTool
	}
	return line
}

func (a *activityState) signalStop() {
	a.stopOnce.Do(func() { close(a.stop) })
}

// activityErrorType is hermes's own verdict for a failed call: its status
// word when that is not the generic "error", else its error_type.
func activityErrorType(d hookDelivery) string {
	if d.Extra.Status != "" && d.Extra.Status != hookStatusOK && d.Extra.Status != ActivityStatusError {
		return d.Extra.Status
	}
	return d.Extra.ErrorType
}

func activityStatus(d hookDelivery) string {
	if d.Extra.ErrorType != "" || (d.Extra.Status != "" && d.Extra.Status != hookStatusOK) {
		return ActivityStatusError
	}
	return ActivityStatusCompleted
}

func removeString(list []string, s string) []string {
	out := list[:0]
	for _, v := range list {
		if v != s {
			out = append(out, v)
		}
	}
	return out
}

// redactInput returns the tool input fit for the bus: secret-looking keys
// blanked at every depth, and the whole thing capped. Over the cap the entry
// carries the size and a rune-safe head rather than a JSON fragment.
func redactInput(raw json.RawMessage) json.RawMessage {
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" || trimmed == "null" {
		return nil
	}
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return json.RawMessage(`{"unparseable":true}`)
	}
	out, err := json.Marshal(redactValue(v))
	if err != nil {
		return json.RawMessage(`{"unparseable":true}`)
	}
	if len(out) > activityInputCap {
		head := chunkString(string(out), activityInputHead)[0]
		out, _ = json.Marshal(map[string]any{"truncated": true, "bytes": len(out), "head": head})
	}
	return out
}

func redactValue(v any) any {
	switch t := v.(type) {
	case map[string]any:
		for k, val := range t {
			if redactedKeyPattern.MatchString(k) {
				t[k] = redactedValue
			} else {
				t[k] = redactValue(val)
			}
		}
		return t
	case []any:
		for i := range t {
			t[i] = redactValue(t[i])
		}
		return t
	case string:
		for _, re := range redactedValuePatterns {
			t = re.ReplaceAllString(t, redactedValue)
		}
		return t
	}
	return v
}

// --- the door ---

// listenActivity binds the door. Called from New so the address is known
// before Run; Run serves it.
func (b *Bridge) listenActivity() error {
	if b.cfg.ActivityListen == "" {
		return nil
	}
	ln, err := net.Listen("tcp", b.cfg.ActivityListen)
	if err != nil {
		return fmt.Errorf("activity door listen %s: %w", b.cfg.ActivityListen, err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc(ActivityPath, b.handleActivity)
	b.activityLn = ln
	b.activitySrv = &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: activityReadHeaderTimeout,
		ReadTimeout:       activityReadTimeout,
		WriteTimeout:      activityWriteTimeout,
	}
	return nil
}

// ActivityURL is where this bridge's children deliver; "" when the door is
// off.
func (b *Bridge) ActivityURL() string {
	if b.activityLn == nil {
		return ""
	}
	return "http://" + b.activityLn.Addr().String() + ActivityPath
}

func (b *Bridge) serveActivity(ctx context.Context) {
	if b.activitySrv == nil {
		return
	}
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.Background(), activityShutdownTimeout)
		defer cancel()
		_ = b.activitySrv.Shutdown(sctx)
	}()
	go func() {
		if err := b.activitySrv.Serve(b.activityLn); err != nil && err != http.ErrServerClosed {
			b.cfg.Logger.Error("activity door stopped", "err", err)
		}
	}()
	b.cfg.Logger.Info("activity door listening", "url", b.ActivityURL())
}

// handleActivity is the door. Every answer past the method check is 204:
// hermes retries connection errors and 5xx and warns on 4xx, and a delivery
// the door cannot use (unsigned, unmatched, unparseable) is not the sender's
// problem to hear about per call. An entry is published on this request,
// under run.mu: hermes delivers from one background thread in order, so
// the trace keeps call order, a publish costs the tool call nothing, and
// there is no queue for finalize to race.
func (b *Bridge) handleActivity(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, activityBodyCap+1))
	if err != nil || len(body) > activityBodyCap {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	run := b.runForSignature(r.Header.Get(hookSignatureHeader), body)
	if run == nil {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	var d hookDelivery
	if err := json.Unmarshal(body, &d); err != nil {
		b.cfg.Logger.Warn("activity delivery unparseable", "task", run.origin.TaskID, "err", err)
		w.WriteHeader(http.StatusNoContent)
		return
	}
	act := run.act.Load()
	if entry, ok := act.observe(d); ok {
		run.mu.Lock()
		if run.state == stateRunning {
			b.publishActivityEntry(run, entry)
		} else {
			b.cfg.Logger.Warn("activity delivery after the terminal; dropped", "task", run.origin.TaskID, "tool", entry.Tool)
		}
		run.mu.Unlock()
	}
	w.WriteHeader(http.StatusNoContent)
}

// runForSignature finds the in-flight task whose key signed body.
func (b *Bridge) runForSignature(sig string, body []byte) *taskRun {
	if sig == "" {
		return nil
	}
	b.mu.Lock()
	runs := make([]*taskRun, 0, len(b.tasks))
	for _, r := range b.tasks {
		runs = append(runs, r)
	}
	b.mu.Unlock()
	for _, r := range runs {
		if a := r.act.Load(); a != nil && a.signed(sig, body) {
			return r
		}
	}
	return nil
}

// --- the heartbeat ---

// runActivity is one task's heartbeat: a progress part on the interval
// until finalize stops it. Each publish takes run.mu and checks the state,
// so nothing lands after the terminal.
func (b *Bridge) runActivity(run *taskRun) {
	a := run.act.Load()
	defer close(a.done)
	var tick <-chan time.Time
	if b.cfg.ProgressInterval > 0 {
		t := time.NewTicker(b.cfg.ProgressInterval)
		defer t.Stop()
		tick = t.C
	}
	for {
		select {
		case <-a.stop:
			return
		case <-tick:
			run.mu.Lock()
			if run.state == stateRunning {
				b.publishProgress(run, a.progressLine(time.Now()))
			}
			run.mu.Unlock()
		}
	}
}

// drainActivity is finalize's half: with run.mu held and the state still
// running, report every call still open as interrupted, then stop the
// heartbeat. Bounded, because the result and the terminal are the publishes
// that matter.
func (b *Bridge) drainActivity(run *taskRun) {
	a := run.act.Load()
	if a == nil {
		return
	}
	a.signalStop()
	deadline := time.Now().Add(activityDrainTimeout)
	for _, e := range a.interrupted() {
		if !time.Now().Before(deadline) {
			b.cfg.Logger.Warn("activity drain budget spent; interrupted calls not all reported", "task", run.origin.TaskID)
			break
		}
		b.publishActivityEntry(run, e)
	}
}

// waitActivity joins the publisher after finalize; the goroutine is gone
// before the run is forgotten.
func (b *Bridge) waitActivity(run *taskRun) {
	a := run.act.Load()
	if a == nil {
		return
	}
	a.signalStop()
	select {
	case <-a.done:
	case <-time.After(activityPublishTimeout):
	}
}

// publishActivityEntry publishes one data part onto the activity artifact.
// Caller holds run.mu.
func (b *Bridge) publishActivityEntry(run *taskRun, e ActivityEntry) {
	data, err := json.Marshal(e)
	if err != nil {
		b.cfg.Logger.Warn("activity entry marshal failed", "task", run.origin.TaskID, "err", err)
		return
	}
	b.publishArtifactPart(run, lib.ArtifactActivity, lib.Part{Kind: "data", Data: data})
}

// publishProgress publishes one text part onto the progress artifact.
// Caller holds run.mu.
func (b *Bridge) publishProgress(run *taskRun, text string) {
	b.publishArtifactPart(run, lib.ArtifactProgress, lib.Part{Kind: "text", Text: text})
}

// publishArtifactPart is the worker adapter's publishArtifactChunk: one part
// appended onto the named artifact, artifactId "artifact-<task>-<name>",
// append after the first, never lastChunk, best-effort. The result artifact
// keeps its own publisher because it is chunked and load-bearing.
func (b *Bridge) publishArtifactPart(run *taskRun, name string, part lib.Part) {
	a := run.act.Load()
	payload, err := json.Marshal(lib.ArtifactUpdate{
		TaskID:    run.origin.TaskID,
		ContextID: run.origin.ContextID,
		Artifact: lib.Artifact{
			ArtifactID: "artifact-" + run.origin.TaskID + "-" + name,
			Name:       name,
			Parts:      []lib.Part{part},
		},
		Append: a.appended[name],
	})
	if err != nil {
		b.cfg.Logger.Warn("artifact marshal failed", "task", run.origin.TaskID, "name", name, "err", err)
		return
	}
	env, err := lib.NewArtifactUpdateEnvelope(b.from, run.origin.TaskID, run.origin.ContextID, run.origin.CorrelationID, payload)
	if err != nil {
		b.cfg.Logger.Warn("artifact envelope failed", "task", run.origin.TaskID, "name", name, "err", err)
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), activityPublishTimeout)
	defer cancel()
	if err := b.c.Publish(ctx, lib.TaskEventsSubject(b.cfg.Profile, run.origin.TaskID), env); err != nil {
		b.cfg.Logger.Warn("artifact publish failed", "task", run.origin.TaskID, "name", name, "err", err)
		return
	}
	a.appended[name] = true
}
