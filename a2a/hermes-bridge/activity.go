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
// loopback address (the sidecar shares the pod's network namespace), and
// hands each child the entry through hermes's managed scope: a per-task
// directory holding the operator's managed config.yaml and .env with a
// hooks.outbound entry added, named by HERMES_MANAGED_DIR in the child's
// environment. Only the bridge's children carry the hook, so a kanban
// worker or cron tick under the same profile never POSTs anywhere, and a
// pod with no bridge has nothing to POST at.
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
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"sigs.k8s.io/yaml"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// DefaultActivityListen is the loopback address the door binds; the
	// URL each child is handed is whatever the door actually bound. The
	// pod's other listeners are hermes's API server
	// on 8642 and the agent-api-auth container on 8643 (bound on every
	// interface, so a loopback bind there fails too), the dashboard on 9119;
	// 8651 is clear of all of them.
	DefaultActivityListen = "127.0.0.1:8651"
	// ActivityPath is the door's one route.
	ActivityPath = "/hermes/tool-events"
	// ActivitySecretEnv is the variable hermes reads the signing key from
	// (the `secret_env` of the hooks.outbound entry); the bridge sets it per
	// child. In a single-profile CLI process hermes's secret scope falls
	// through to the process environment, which is what makes this work.
	ActivitySecretEnv = "A2A_ACTIVITY_SECRET"
	// ActivityURLEnv tells the child where its deliveries go. hermes reads
	// the URL from the managed config the bridge writes; the variable is for
	// the record and for test stubs standing in for hermes.
	ActivityURLEnv = "A2A_ACTIVITY_URL"
	// ManagedDirEnv is hermes's managed-scope override: a directory whose
	// config.yaml is deep-merged per leaf over the profile's and whose .env
	// is loaded last. The operator sets it on the agent container (and so
	// on the sidecar) to /etc/hermes; the bridge points each child at its
	// own copy with the hook added.
	ManagedDirEnv = "HERMES_MANAGED_DIR"
	// DefaultManagedDir is hermes's managed-scope default when the variable
	// is unset, read only when the directory exists.
	DefaultManagedDir = "/etc/hermes"
	managedConfigFile = "config.yaml"
	managedEnvFile    = ".env"
	// The hooks.outbound entry the bridge writes for its child. The timeout
	// is longer than activityPublishTimeout: the door publishes on the
	// delivery, and a timed-out delivery is retried, which would be a
	// duplicate call in the trace.
	hookEntryName      = "a2a-bridge-activity"
	hookTimeoutSeconds = 10
	hooksKey           = "hooks"
	hooksOutboundKey   = "outbound"
	// childScopeDirMode: the copy carries the managed .env, credentials
	// included, so it is the bridge's alone.
	childScopeDirMode  = 0o700
	childScopeFileMode = 0o600
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
	// on the delivery for the hook entry's timeout, which is longer, so a
	// slow publish is not a retried (duplicated) delivery.
	activityPublishTimeout = 5 * time.Second
	// activityDrainTimeout bounds finalize's flush of the calls still open,
	// so a slow bus cannot spend the terminal's budget.
	activityDrainTimeout = 5 * time.Second
	// activitySeenCap bounds the delivery ids remembered per task for
	// dedupe; hermes retries a delivery at most once, so the set only
	// grows with the calls.
	activitySeenCap  = 8192
	activityKeyBytes = 32
	// activityTruncatedTool names the one entry published in place of the
	// calls past the budget, with Dropped saying how many.
	activityTruncatedTool   = "activity-budget"
	ActivityStatusTruncated = "truncated"
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
// and are best-effort by nature: a bearer or basic authorization value,
// a Google OAuth access token, a Google API key, a GitHub token, a
// user:password given to curl's -u, or a key that looks like a secret
// followed by its value - with or without the quotes and spaces a JSON body
// or a header puts around the separator, and as a command-line flag with
// its value after a space (--token v, --access-token v, --password v).
// Anything else the model pastes into a command line ships, a bare -p v
// included, since -p is a port as often as a password.
// activityEntryBudget bounds the parts one task publishes on its trace and
// its heartbeat together. Both ride the task's own events subject, which
// the TASKS stream caps at 4096 messages per subject with discard-old, so a
// run that published without bound - a looping persona, or a short
// heartbeat interval under a long deadline - would evict its own submitted
// and working events and read as never started. One counter for both
// artifacts keeps the sum under the cap whatever the knobs say; 3000 leaves
// room for the four lifecycle events, a chunked result and the truncation
// marker. A variable only so a test can lower it.
var activityEntryBudget = 3000

// taskIDPattern is what a task id may look like before it becomes a path
// segment under ScratchDir: the gateway mints task-<hex>, tests use words.
// The id arrives on a bus envelope, and the lib checks it against the
// subject token it rode in on - which cannot hold a dot, so no ".." - but
// the sink that writes files does not lean on that.
var taskIDPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

var (
	// A key is secret-looking when one of the words is a whole component of
	// it (access_token, AWS_SECRET_ACCESS_KEY, api-key), not a substring
	// (tokenizer, secretName): the latter are names, and blanking them would
	// put "[redacted]" where the worker adapter's trace carries the value.
	// A count or a path that ends in the word (max_tokens, credentials_file)
	// is blanked too; that is the accepted price of catching SECRET_KEY.
	redactedKeyPattern    = regexp.MustCompile(`(?i)(?:^|[_.-])(?:token|secret|password|passwd|authorization|api[_-]?key|credential)s?(?:$|[_.-])`)
	redactedValuePatterns = []*regexp.Regexp{
		regexp.MustCompile(`(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}`),
		regexp.MustCompile(`ya29\.[A-Za-z0-9._-]{20,}`),
		regexp.MustCompile(`AIza[0-9A-Za-z_-]{35}`),
		regexp.MustCompile(`gh[pousr]_[A-Za-z0-9]{20,}`),
		// The header form, capitalised as HTTP writes it and long enough to
		// be a credential, so "basic refactoring" in a commit message is
		// not one.
		regexp.MustCompile(`Basic\s+[A-Za-z0-9+/]{16,}={0,2}`),
		// curl's -u user:password, on a curl command: "date -u 12:30" and
		// "sort -u a:b" are not.
		regexp.MustCompile(`(?i)\bcurl\b[^;|&\n]*\s(?:-u|--user)[\s=]+\S+:\S+`),
		// key=value / key: value / "key": "value", where a secret word is a
		// whole component of the key (SECRET_KEY, AWS_SECRET_ACCESS_KEY).
		regexp.MustCompile(`(?i)(?:^|[^A-Za-z0-9])[A-Za-z0-9_-]*(?:token|secret|password|passwd|api[_-]?key|credential)s?(?:[_-][A-Za-z0-9_-]*)?["']?\s*[=:]\s*["']?[^"'\s,}]+`),
		// --flag value, the word a component of the flag (--token, --secret-access-key).
		regexp.MustCompile(`(?i)(?:^|\s)--?[a-z0-9-]*(?:token|secret|password|passwd|api[_-]?key|credential)s?(?:-[a-z0-9-]+)?\s+\S+`),
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
	// Dropped is set only on the activityTruncatedTool entry: how many
	// calls past activityEntryBudget were counted and not published.
	Dropped int `json:"dropped,omitempty"`
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
	published int // trace and heartbeat parts sent; the budget counts these
	dropped   int // calls past the budget, reported once at the terminal
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

// childEnv is what the door adds to the hermes child's environment: the
// signing key, the door's URL for the record, and the managed scope that
// carries the hook. Last wins among duplicates in exec.Cmd.Env, so the
// scope override replaces the sidecar's inherited one.
func (a *activityState) childEnv(url, managedDir string) []string {
	if a.key == "" {
		return nil
	}
	return []string{
		ActivitySecretEnv + "=" + a.key,
		ActivityURLEnv + "=" + url,
		ManagedDirEnv + "=" + managedDir,
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

// underBudget says whether the next trace part may go out, counting it
// either way: past the budget the call is counted as dropped and reported
// once by the marker finalize publishes. Caller holds run.mu, which is what
// orders this against finalize's drain.
func (a *activityState) underBudget() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.published < activityEntryBudget {
		a.published++
		return true
	}
	a.dropped++
	return false
}

// heartbeatUnderBudget is underBudget for a progress part: past the budget
// the heartbeat simply stops, uncounted - the marker counts calls.
func (a *activityState) heartbeatUnderBudget() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.published < activityEntryBudget {
		a.published++
		return true
	}
	return false
}

// truncationMarker is the entry that stands for the calls the budget cut,
// or false when nothing was cut.
func (a *activityState) truncationMarker() (ActivityEntry, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.dropped == 0 {
		return ActivityEntry{}, false
	}
	return ActivityEntry{Tool: activityTruncatedTool, Status: ActivityStatusTruncated, Dropped: a.dropped, At: time.Now().UTC().Format(time.RFC3339)}, true
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
	// UseNumber: a number decoded into float64 and written back loses the
	// low digits of a 64-bit id, and the worker adapter publishes the same
	// argument verbatim; json.Number falls through redactValue and marshals
	// as its literal.
	dec := json.NewDecoder(strings.NewReader(trimmed))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
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

// --- the child's managed scope ---

// childManagedScope writes the per-task managed directory: the source scope's
// config.yaml with the door's hooks.outbound entry added (appended to any the
// source already carries) and its .env verbatim. Returns the directory; the
// caller removes it once the child has exited.
func (b *Bridge) childManagedScope(taskID string) (dir string, err error) {
	if !taskIDPattern.MatchString(taskID) {
		return "", fmt.Errorf("task id %q is not a path segment", taskID)
	}
	scratch, err := filepath.Abs(b.cfg.ScratchDir)
	if err != nil {
		return "", fmt.Errorf("scratch dir: %w", err)
	}
	dir = filepath.Join(scratch, taskID)
	if rel, err := filepath.Rel(scratch, dir); err != nil || rel != taskID {
		return "", fmt.Errorf("task id %q leaves the scratch dir", taskID)
	}
	if err := os.MkdirAll(dir, childScopeDirMode); err != nil {
		return "", fmt.Errorf("child scope dir: %w", err)
	}
	// Nothing half-written survives a failure: the copy carries the
	// managed .env, and a directory left behind would hold it for the
	// pod's lifetime.
	made := dir
	defer func() {
		if err != nil {
			_ = os.RemoveAll(made)
		}
	}()
	// A source file that exists but cannot be read is a fault, not an
	// absence: a child started on a hook-only scope would run without the
	// operator's pins, silently. Only a missing file means nothing to copy.
	cfg := map[string]any{}
	src := b.cfg.ManagedScopeDir
	if src != "" {
		raw, rerr := os.ReadFile(filepath.Join(src, managedConfigFile))
		switch {
		case rerr == nil:
			if err := yaml.Unmarshal(raw, &cfg); err != nil {
				return "", fmt.Errorf("managed config %s: %w", src, err)
			}
			if cfg == nil {
				cfg = map[string]any{}
			}
		case !errors.Is(rerr, os.ErrNotExist):
			return "", fmt.Errorf("managed config %s: %w", src, rerr)
		}
		raw, rerr = os.ReadFile(filepath.Join(src, managedEnvFile))
		switch {
		case rerr == nil:
			if err := os.WriteFile(filepath.Join(dir, managedEnvFile), raw, childScopeFileMode); err != nil {
				return "", fmt.Errorf("child scope env: %w", err)
			}
		case !errors.Is(rerr, os.ErrNotExist):
			return "", fmt.Errorf("managed env %s: %w", src, rerr)
		}
	}
	hooks, _ := cfg[hooksKey].(map[string]any)
	if hooks == nil {
		hooks = map[string]any{}
	}
	outbound, _ := hooks[hooksOutboundKey].([]any)
	outbound = append(outbound, map[string]any{
		"name":       hookEntryName,
		"url":        b.ActivityURL(),
		"events":     []any{hookPreToolCall, hookPostToolCall},
		"secret_env": ActivitySecretEnv,
		"timeout":    hookTimeoutSeconds,
	})
	hooks[hooksOutboundKey] = outbound
	cfg[hooksKey] = hooks
	out, err := yaml.Marshal(cfg)
	if err != nil {
		return "", fmt.Errorf("child scope config: %w", err)
	}
	if err := os.WriteFile(filepath.Join(dir, managedConfigFile), out, childScopeFileMode); err != nil {
		return "", fmt.Errorf("child scope config: %w", err)
	}
	return dir, nil
}

// --- the door ---

// listenActivity binds the door and claims the scratch dir. Called from New
// so the address is known before Run; Run serves it. The scratch dir is the
// bridge's alone and starts empty: a scope a previous incarnation left
// behind (killed mid-task, its defers never run) held a copy of the managed
// .env, and the sweep that finalizes that incarnation's tasks does not
// know about files.
func (b *Bridge) listenActivity() error {
	if b.cfg.ActivityListen == "" {
		return nil
	}
	if err := os.RemoveAll(b.cfg.ScratchDir); err != nil {
		return fmt.Errorf("scratch dir %s: %w", b.cfg.ScratchDir, err)
	}
	if err := os.MkdirAll(b.cfg.ScratchDir, childScopeDirMode); err != nil {
		return fmt.Errorf("scratch dir %s: %w", b.cfg.ScratchDir, err)
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
	// observe under run.mu: it closes the call, and a finalize slipping in
	// between the close and the publish would find nothing open to report
	// and the entry would then be dropped as post-terminal - present in the
	// trace neither as completed nor as interrupted.
	act := run.act.Load()
	run.mu.Lock()
	if entry, ok := act.observe(d); ok {
		if run.state == stateRunning {
			if act.underBudget() {
				b.publishActivityEntry(run, entry)
			}
		} else {
			b.cfg.Logger.Warn("activity delivery after the terminal; dropped", "task", run.origin.TaskID, "tool", entry.Tool)
		}
	}
	run.mu.Unlock()
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
			if run.state == stateRunning && a.heartbeatUnderBudget() {
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
		if a.underBudget() {
			b.publishActivityEntry(run, e)
		}
	}
	if marker, ok := a.truncationMarker(); ok && time.Now().Before(deadline) {
		b.publishActivityEntry(run, marker)
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
	// Marked before the publish, as the worker adapter does: a publish whose
	// ack times out after the server stored it (a reconnect) must not make
	// the next part a non-append that replaces the artifact in every fold.
	// A lost first part costs one entry; a reset costs the whole trace.
	a.appended[name] = true
	ctx, cancel := context.WithTimeout(context.Background(), activityPublishTimeout)
	defer cancel()
	if err := b.c.Publish(ctx, lib.TaskEventsSubject(b.cfg.Profile, run.origin.TaskID), env); err != nil {
		b.cfg.Logger.Warn("artifact publish failed", "task", run.origin.TaskID, "name", name, "err", err)
	}
}
