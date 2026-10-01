package hermesbridge

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The activity door, end to end: a stub standing in for hermes delivers the
// outbound-webhook bodies hermes would (agent/outbound_webhooks.py, signed
// with the key the bridge put in its environment), and the bridge turns them
// into the task's activity and progress artifacts.

// hermesStub writes a python3 executable standing in for hermes. The body
// runs after a prelude that knows how to sign and POST a delivery the way
// hermes does; the prompt is sys.argv[-1]. Skips when python3 is absent.
func hermesStub(t *testing.T, body string) []string {
	t.Helper()
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH; the hermes stub needs it")
	}
	prelude := `#!/usr/bin/env python3
import hashlib, hmac, json, os, sys, time, urllib.request, uuid
URL = os.environ.get("` + ActivityURLEnv + `", "")
KEY = os.environ.get("` + ActivitySecretEnv + `", "")
def post(event, tool, args, extra, sign=True):
    body = json.dumps({"hook_event_name": event, "profile": "platform", "tool_name": tool,
                       "tool_input": args, "session_id": "s1", "cwd": "/opt/data", "extra": extra,
                       "delivery_id": uuid.uuid4().hex, "timestamp": "2026-09-25T20:00:00Z"}).encode()
    headers = {"Content-Type": "application/json", "X-Hermes-Event": event}
    if sign and KEY:
        headers["X-Hermes-Signature-256"] = "sha256=" + hmac.new(KEY.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status
def call(tool, args, call_id, status="ok", error_type=None, ms=12):
    post("pre_tool_call", tool, args, {"tool_call_id": call_id, "task_id": "", "session_id": "s1"})
    post("post_tool_call", tool, args, {"tool_call_id": call_id, "task_id": "", "session_id": "s1",
                                         "duration_ms": ms, "status": status, "error_type": error_type,
                                         "error_message": None, "result": "not published"})
`
	path := filepath.Join(t.TempDir(), "hermes-stub.py")
	if err := os.WriteFile(path, []byte(prelude+body+"\n"), 0o755); err != nil {
		t.Fatalf("write stub: %v", err)
	}
	return []string{path}
}

// startBridgeCfg is startBridge with the caller's Config; the door is opened
// on an ephemeral port unless the caller says otherwise. The lifecycle and
// the consumer wait are startBridgeConfig's.
func startBridgeCfg(t *testing.T, url string, command []string, mutate func(*Config)) *Bridge {
	t.Helper()
	cfg := Config{
		// Full values, so the tests below can read what the scrub kept; the
		// shape default has a test of its own.
		ActivityInputValues: InputValuesFull,
		NATSURL:             url,
		Command:             command,
		TaskDeadline:        20 * time.Second,
		KillGrace:           500 * time.Millisecond,
		ActivityListen:      "127.0.0.1:0",
	}
	if mutate != nil {
		mutate(&cfg)
	}
	b, _ := startBridgeConfig(t, cfg, nil)
	return b
}

func activityEntries(t *testing.T, task *lib.Task) []ActivityEntry {
	t.Helper()
	art := task.Artifact(lib.ArtifactActivity)
	if art == nil {
		return nil
	}
	var out []ActivityEntry
	for i, p := range art.Parts {
		if p.Kind != "data" {
			t.Fatalf("activity part %d is %q, want data (assertion 18)", i, p.Kind)
		}
		var e ActivityEntry
		if err := json.Unmarshal(p.Data, &e); err != nil {
			t.Fatalf("activity part %d: %v", i, err)
		}
		out = append(out, e)
	}
	return out
}

// eventTrail returns, in stream order, the artifact name of every
// artifact-update and "<state>/final" for every status-update.
func eventTrail(t *testing.T, events []*lib.Envelope) []string {
	t.Helper()
	var trail []string
	for _, env := range events {
		switch env.Kind {
		case lib.KindArtifactUpdate:
			var a lib.ArtifactUpdate
			if err := json.Unmarshal(env.Payload, &a); err != nil {
				t.Fatal(err)
			}
			trail = append(trail, a.Artifact.Name)
		case lib.KindStatusUpdate:
			state, final := statusState(t, env)
			if final {
				trail = append(trail, string(state)+"/final")
			} else {
				trail = append(trail, string(state))
			}
		}
	}
	return trail
}

func TestActivity_ToolCallsBecomeTheActivityArtifact(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
call("kubectl", {"cmd": "get pods", "token": "hunter2", "nested": {"api_key": "k", "keep": 1}}, "call_1")
call("mcp__gke__list_clusters", {"project": "p"}, "call_2", status="error", error_type="tool_error", ms=340)
print("the answer")
`), nil)
	c := gatewayClient(t, url)

	submit(t, c, "task-activity", "list the fleet")
	task := waitTerminal(t, c, "task-activity")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	if err := task.ValidateArtifacts(); err != nil {
		t.Fatalf("assertion 18: %v", err)
	}
	entries := activityEntries(t, task)
	if len(entries) != 2 {
		t.Fatalf("activity entries = %d, want 2: %+v", len(entries), entries)
	}
	first := entries[0]
	if first.Tool != "kubectl" || first.CallID != "call_1" || first.Status != ActivityStatusCompleted || first.DurationMs != 12 || first.At == "" {
		t.Fatalf("first entry = %+v", first)
	}
	var input map[string]any
	if err := json.Unmarshal(first.Input, &input); err != nil {
		t.Fatal(err)
	}
	if input["cmd"] != "get pods" || input["token"] != redactedValue {
		t.Fatalf("input not redacted as designed: %v", input)
	}
	if nested := input["nested"].(map[string]any); nested["api_key"] != redactedValue || nested["keep"] != float64(1) {
		t.Fatalf("nested input not redacted as designed: %v", nested)
	}
	if second := entries[1]; second.Tool != "mcp__gke__list_clusters" || second.Status != ActivityStatusError || second.DurationMs != 340 {
		t.Fatalf("second entry = %+v", second)
	}
	for _, e := range entries {
		if strings.Contains(string(e.Input), "not published") {
			t.Fatalf("a tool result reached the bus: %s", e.Input)
		}
	}

	// Order on the wire: the trace precedes the result, and the result the
	// final; the first activity part opens the artifact, the second appends.
	trail := eventTrail(t, replayEvents(t, url, "task-activity"))
	want := []string{"submitted", "working", "activity", "activity", "result", "completed/final"}
	if strings.Join(trail, " ") != strings.Join(want, " ") {
		t.Fatalf("event trail = %v, want %v", trail, want)
	}
}

func TestActivity_UnsignedAndUnknownDeliveriesAreDropped(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
# A kanban worker under the same profile: no key, unsigned delivery.
post("post_tool_call", "terminal", {"cmd": "ls"}, {"tool_call_id": "c1", "status": "ok", "duration_ms": 1}, sign=False)
# Signed with somebody else's key.
KEY = "00" * 32
post("post_tool_call", "terminal", {"cmd": "ls"}, {"tool_call_id": "c2", "status": "ok", "duration_ms": 1})
print("done")
`), nil)
	c := gatewayClient(t, url)

	submit(t, c, "task-unsigned", "hello")
	task := waitTerminal(t, c, "task-unsigned")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	if got := activityEntries(t, task); len(got) != 0 {
		t.Fatalf("unsigned deliveries became activity: %+v", got)
	}
}

func TestActivity_AnOpenCallIsReportedInterruptedAtTheDeadline(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
post("pre_tool_call", "terminal", {"cmd": "sleep 60", "secret": "x"}, {"tool_call_id": "c-open", "task_id": ""})
time.sleep(30)
`), func(c *Config) {
		c.TaskDeadline = 2 * time.Second
		c.KillGrace = 200 * time.Millisecond
	})
	c := gatewayClient(t, url)

	submit(t, c, "task-interrupted", "hang")
	task := waitTerminal(t, c, "task-interrupted")
	if task.State != lib.StateFailed || !strings.Contains(terminalReason(t, task), "deadline-exceeded") {
		t.Fatalf("state = %s msg = %v, want failed deadline-exceeded", task.State, task.FinalMessage)
	}
	entries := activityEntries(t, task)
	if len(entries) != 1 || entries[0].Tool != "terminal" || entries[0].Status != ActivityStatusInterrupted || entries[0].CallID != "c-open" {
		t.Fatalf("interrupted entry = %+v, want one interrupted terminal call", entries)
	}
	if !strings.Contains(string(entries[0].Input), redactedValue) {
		t.Fatalf("interrupted entry input not redacted: %s", entries[0].Input)
	}
	trail := eventTrail(t, replayEvents(t, url, "task-interrupted"))
	if got := trail[len(trail)-1]; got != "failed/final" {
		t.Fatalf("last event = %s, want failed/final; trail %v", got, trail)
	}
	if trail[len(trail)-2] != "activity" {
		t.Fatalf("the interrupted call did not precede the terminal: %v", trail)
	}
}

func TestActivity_HeartbeatOnTheProgressArtifact(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
call("kubectl", {"cmd": "get nodes"}, "c1")
time.sleep(1.2)
print("slow answer")
`), func(c *Config) { c.ProgressInterval = 250 * time.Millisecond })
	c := gatewayClient(t, url)

	submit(t, c, "task-heartbeat", "take your time")
	task := waitTerminal(t, c, "task-heartbeat")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	if err := task.ValidateArtifacts(); err != nil {
		t.Fatalf("assertion 18: %v", err)
	}
	progress := task.Artifact(lib.ArtifactProgress)
	if progress == nil || len(progress.Parts) < 2 {
		t.Fatalf("progress artifact = %+v, want at least two heartbeats", progress)
	}
	last := progress.Parts[len(progress.Parts)-1]
	if last.Kind != "text" || !strings.HasPrefix(last.Text, "running ") || !strings.Contains(last.Text, "1 tool call(s), last kubectl") {
		t.Fatalf("heartbeat = %q", last.Text)
	}
	trail := eventTrail(t, replayEvents(t, url, "task-heartbeat"))
	if trail[len(trail)-1] != "completed/final" || trail[len(trail)-2] != "result" {
		t.Fatalf("heartbeat landed after the result or the final: %v", trail)
	}
}

// With the door closed no delivery can move the count, so the heartbeat says
// the trace is off rather than reporting zero calls; with it open the count
// is there from the first line.
func TestActivity_HeartbeatWithTheDoorClosedSaysTheTraceIsOff(t *testing.T) {
	now := time.Now()
	closed := newActivityState(false, InputValuesFull)
	closed.startedAt = now.Add(-5 * time.Minute)
	if got := closed.progressLine(now); got != "running 5m0s, tool trace off" {
		t.Fatalf("door-closed heartbeat = %q", got)
	}
	open := newActivityState(true, InputValuesFull)
	open.startedAt = now.Add(-5 * time.Minute)
	if got := open.progressLine(now); got != "running 5m0s, 0 tool call(s)" {
		t.Fatalf("door-open heartbeat = %q", got)
	}
}

func TestActivity_DoorClosedLeavesTheChildWithoutAKey(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
print("key=" + ("set" if KEY else "unset") + " url=" + ("set" if URL else "unset"))
`), func(c *Config) { c.ActivityListen = "" })
	c := gatewayClient(t, url)

	submit(t, c, "task-closed", "hello")
	task := waitTerminal(t, c, "task-closed")
	if got := task.Artifact(lib.ArtifactResult).Parts[0].Text; !strings.Contains(got, "key=unset url=unset") {
		t.Fatalf("result = %q, want no door in the child's environment", got)
	}
	if err := task.ValidateArtifacts(); err != nil {
		t.Fatal(err)
	}
}

// Each child gets its own managed scope: the source scope's config with the
// door's hook added, its .env verbatim, named by HERMES_MANAGED_DIR, and
// removed once the child has exited.
func TestActivity_ChildGetsItsOwnManagedScope(t *testing.T) {
	src := t.TempDir()
	if err := os.WriteFile(filepath.Join(src, "config.yaml"), []byte("model:\n  default: pinned/model\nhooks:\n  outbound:\n    - name: theirs\n      url: https://audit.example/\n      events: [post_tool_call]\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(src, ".env"), []byte("PINNED_KEY=pinned-value\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	scratch := t.TempDir()
	_, url := startServer(t)
	b := startBridgeCfg(t, url, hermesStub(t, `
d = os.environ["HERMES_MANAGED_DIR"]
print("DIR=" + d)
print(open(os.path.join(d, "config.yaml")).read())
print("ENV=" + open(os.path.join(d, ".env")).read().strip())
`), func(c *Config) { c.ManagedScopeDir = src; c.ScratchDir = scratch })
	c := gatewayClient(t, url)
	submit(t, c, "task-scope", "show your scope")
	task := waitTerminal(t, c, "task-scope")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s", task.State)
	}
	out := task.Artifact(lib.ArtifactResult).Parts[0].Text
	want := filepath.Join(scratch, "task-scope")
	if !strings.Contains(out, "DIR="+want) {
		t.Fatalf("child scope dir: %s", out)
	}
	for _, needle := range []string{"default: pinned/model", "name: theirs", "name: " + hookEntryName, "url: " + b.ActivityURL(), "secret_env: " + ActivitySecretEnv, "- pre_tool_call", "- post_tool_call", "timeout: 10", "ENV=PINNED_KEY=pinned-value"} {
		if !strings.Contains(out, needle) {
			t.Fatalf("child scope lacks %q:\n%s", needle, out)
		}
	}
	// The terminal is on the stream before runTask's deferred removal runs,
	// so poll rather than stat once.
	waitFor(t, 5*time.Second, "child scope removal", func() bool {
		_, err := os.Stat(want)
		return os.IsNotExist(err)
	})
}

// The scope's marker is written first, its .env last, so a scope cut short
// between any two writes is one the sweep removes (it keys on the marker)
// rather than a credential copy it never sees. Observed through the write
// seam, since back-to-back writes share a file-time tick.
func TestChildManagedScope_WritesTheConfigBeforeTheEnv(t *testing.T) {
	src := t.TempDir()
	for name, body := range map[string]string{managedConfigFile: "model: {default: m}\n", managedEnvFile: "K=v\n"} {
		if err := os.WriteFile(filepath.Join(src, name), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	var order []string
	prev := writeScopeFile
	writeScopeFile = func(name string, data []byte, perm os.FileMode) error {
		order = append(order, filepath.Base(name))
		return os.WriteFile(name, data, perm)
	}
	t.Cleanup(func() { writeScopeFile = prev })
	scratch := t.TempDir()
	b := &Bridge{cfg: Config{ScratchDir: scratch, ManagedScopeDir: src}}
	if _, err := b.childManagedScope("task-order"); err != nil {
		t.Fatal(err)
	}
	if len(order) != 3 || order[0] != scopeMarkerFile || order[1] != managedConfigFile || order[2] != managedEnvFile {
		t.Fatalf("scope files written in order %v, want [%s %s %s]", order, scopeMarkerFile, managedConfigFile, managedEnvFile)
	}
}

// Without a source scope the child still gets one, holding only the hook.
func TestActivity_ChildScopeWithoutASourceHoldsOnlyTheHook(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
d = os.environ["HERMES_MANAGED_DIR"]
print(open(os.path.join(d, "config.yaml")).read())
print("ENV_PRESENT=" + str(os.path.exists(os.path.join(d, ".env"))))
`), func(c *Config) { c.ManagedScopeDir = filepath.Join(t.TempDir(), "absent"); c.ScratchDir = t.TempDir() })
	c := gatewayClient(t, url)
	submit(t, c, "task-scope-bare", "show your scope")
	task := waitTerminal(t, c, "task-scope-bare")
	out := task.Artifact(lib.ArtifactResult).Parts[0].Text
	if !strings.Contains(out, "name: "+hookEntryName) || strings.Contains(out, "model:") || !strings.Contains(out, "ENV_PRESENT=False") {
		t.Fatalf("bare scope = %s", out)
	}
}

func TestRedactInput(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want func(t *testing.T, out json.RawMessage)
	}{
		{"null is absent", "null", func(t *testing.T, out json.RawMessage) {
			if out != nil {
				t.Fatalf("got %s", out)
			}
		}},
		{"secret keys at every depth", `{"Authorization":"Bearer x","list":[{"PASSWORD":"p","ok":true}],"cmd":"ls"}`, func(t *testing.T, out json.RawMessage) {
			s := string(out)
			if strings.Contains(s, "Bearer x") || strings.Contains(s, `"p"`) || !strings.Contains(s, `"cmd":"ls"`) || !strings.Contains(s, `"ok":true`) {
				t.Fatalf("got %s", s)
			}
		}},
		{"over the cap keeps a head", `{"blob":"` + strings.Repeat("é", activityInputCap) + `"}`, func(t *testing.T, out json.RawMessage) {
			var v struct {
				Truncated bool   `json:"truncated"`
				Bytes     int    `json:"bytes"`
				Head      string `json:"head"`
			}
			if err := json.Unmarshal(out, &v); err != nil || !v.Truncated || v.Bytes <= activityInputCap || len(v.Head) == 0 || len(v.Head) > activityInputHead {
				t.Fatalf("got %s (%v)", out, err)
			}
			if !strings.HasPrefix(v.Head, `{"blob":"`) {
				t.Fatalf("head = %q", v.Head)
			}
		}},
		{"unparseable is named", `{not json`, func(t *testing.T, out json.RawMessage) {
			if string(out) != `{"unparseable":true}` {
				t.Fatalf("got %s", out)
			}
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) { tc.want(t, redactInput(InputValuesFull, "terminal", json.RawMessage(tc.in))) })
	}
}

// The entry's other fields are bounded from the delivery: a long tool name,
// call id or timestamp cannot make a part the size of the door's body cap.
func TestActivity_EntryFieldsAreBounded(t *testing.T) {
	a := newActivityState(false, InputValuesShape)
	long := strings.Repeat("n", 4096)
	var d hookDelivery
	d.Event, d.ToolName, d.Timestamp = hookPostToolCall, long, long
	d.Extra.ToolCallID, d.Extra.Status = long, "ok"
	e, ok := a.observe(d)
	if !ok || len(e.Tool) != activityToolNameCap || len(e.CallID) != activityCallIDCap || len(e.At) != activityWordCap {
		t.Fatalf("entry fields not bounded: ok=%v tool=%d callId=%d at=%d", ok, len(e.Tool), len(e.CallID), len(e.At))
	}
}

// An unreadable signed delivery is logged and not counted: whichever event
// it was, the call is in the trace (whole from its post, or interrupted at
// the drain), so a marker would overcount.
func TestActivity_UnreadableSignedDeliveryIsNotCounted(t *testing.T) {
	b := &Bridge{cfg: Config{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}, tasks: map[string]*taskRun{}}
	a := newActivityState(true, InputValuesShape)
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-bad", ContextID: "ctx", CorrelationID: "corr"}}
	run.act.Store(a)
	b.tasks["task-bad"] = run
	body := []byte(`{"hook_event_name":"post_tool_call","extra":"not an object"`)
	mac := hmac.New(sha256.New, []byte(a.key))
	mac.Write(body)
	req := httptest.NewRequest(http.MethodPost, ActivityPath, bytes.NewReader(body))
	req.Header.Set(hookSignatureHeader, hookSignaturePrefix+hex.EncodeToString(mac.Sum(nil)))
	rec := httptest.NewRecorder()
	b.handleActivity(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("status %d", rec.Code)
	}
	if m, ok := a.truncationMarker(); ok {
		t.Fatalf("marker after an unreadable delivery = %+v; the call is not missing", m)
	}
	// The readable post that follows an unreadable pre is the call, whole.
	good := []byte(`{"hook_event_name":"post_tool_call","tool_name":"terminal","tool_input":{"command":"ls"},"extra":{"tool_call_id":"c1","status":"ok"}}`)
	var d hookDelivery
	if err := json.Unmarshal(good, &d); err != nil {
		t.Fatal(err)
	}
	if e, ok := a.observe(d); !ok || e.Tool != "terminal" || e.Status != ActivityStatusCompleted {
		t.Fatalf("post without an open pre was not published whole: %+v %v", e, ok)
	}
}

// A fractional duration_ms (a Python emitter's 12.5) is one field's shape,
// not a reason to drop the delivery.
func TestActivity_FractionalDurationIsRead(t *testing.T) {
	var d hookDelivery
	if err := json.Unmarshal([]byte(`{"hook_event_name":"post_tool_call","tool_name":"terminal","extra":{"tool_call_id":"c1","duration_ms":12.5,"status":"ok"}}`), &d); err != nil {
		t.Fatalf("a fractional duration_ms failed to parse: %v", err)
	}
	if got := durationMillis(d.Extra.DurationMs); got != 12 {
		t.Fatalf("durationMillis(12.5) = %d, want 12", got)
	}
	if got := durationMillis(json.Number("40")); got != 40 {
		t.Fatalf("durationMillis(40) = %d", got)
	}
	for _, out := range []string{"9223372036854775807.0", "9.223372036854775808e18", "9223372036854775808", "-5.5", "-5", "1e400"} {
		if got := durationMillis(json.Number(out)); got != 0 {
			t.Fatalf("durationMillis(%s) = %d, want 0", out, got)
		}
	}
}

func TestActivityStatus(t *testing.T) {
	mk := func(status, errType string) hookDelivery {
		var d hookDelivery
		d.Extra.Status, d.Extra.ErrorType = status, errType
		return d
	}
	if got := activityStatus(mk("ok", "")); got != ActivityStatusCompleted {
		t.Fatalf("ok -> %s", got)
	}
	if got := activityStatus(mk("", "")); got != ActivityStatusCompleted {
		t.Fatalf("unset -> %s", got)
	}
	if got := activityStatus(mk("error", "tool_error")); got != ActivityStatusError {
		t.Fatalf("error -> %s", got)
	}
	if got := activityStatus(mk("ok", "tool_error")); got != ActivityStatusError {
		t.Fatalf("error_type alone -> %s", got)
	}
}

// Two tasks at once: each delivery lands on the task whose key signed it,
// never on the other. The stubs overlap by sleeping after their calls.
func TestActivity_ConcurrentTasksKeepTheirOwnTraces(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
prompt = sys.argv[-1]
tool = "tool-for-" + prompt
call(tool, {"which": prompt}, "call-" + prompt)
time.sleep(1.5)
call(tool + "-again", {"which": prompt}, "call2-" + prompt)
print("answer for " + prompt)
`), func(c *Config) { c.Concurrency = 2 })
	c := gatewayClient(t, url)

	// The two stubs each sleep 1.5 s between their calls; run together both
	// terminals land well inside twice that, run one after the other they
	// cannot. The bound is what makes "at once" a tested claim.
	started := time.Now()
	submit(t, c, "task-a", "A")
	submit(t, c, "task-b", "B")
	ta := waitTerminal(t, c, "task-a")
	tb := waitTerminal(t, c, "task-b")
	if elapsed := time.Since(started); elapsed > 3*time.Second {
		t.Fatalf("the two tasks did not overlap: both terminals took %s, serial runs would", elapsed)
	}
	for _, tc := range []struct {
		task *lib.Task
		want string
	}{{ta, "A"}, {tb, "B"}} {
		entries := activityEntries(t, tc.task)
		if len(entries) != 2 {
			t.Fatalf("task %s: %d entries, want 2: %+v", tc.want, len(entries), entries)
		}
		for _, e := range entries {
			if !strings.HasPrefix(e.Tool, "tool-for-"+tc.want) || !strings.Contains(string(e.Input), `"which":"`+tc.want+`"`) {
				t.Fatalf("task %s carries another task's call: %+v", tc.want, e)
			}
		}
	}
}

// hermes retries a timed-out delivery once with the same delivery_id; the
// trace records the call once.
func TestActivity_ARetriedDeliveryIsOneCall(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
extra = {"tool_call_id": "c1", "status": "ok", "duration_ms": 3}
body = json.dumps({"hook_event_name": "post_tool_call", "profile": "platform", "tool_name": "kubectl",
                   "tool_input": {"cmd": "get ns"}, "session_id": "s1", "cwd": "/opt/data", "extra": extra,
                   "delivery_id": "same-delivery", "timestamp": "2026-09-25T20:00:00Z"}).encode()
sig = "sha256=" + hmac.new(KEY.encode(), body, hashlib.sha256).hexdigest()
for _ in range(2):
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json", "X-Hermes-Signature-256": sig}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()
print("done")
`), nil)
	c := gatewayClient(t, url)
	submit(t, c, "task-retry", "hello")
	task := waitTerminal(t, c, "task-retry")
	if got := activityEntries(t, task); len(got) != 1 {
		t.Fatalf("entries = %d, want 1 (retry deduped): %+v", len(got), got)
	}
}

func TestActivity_ErrorTypeKeepsHermesVerdict(t *testing.T) {
	mk := func(status, errType string) hookDelivery {
		var d hookDelivery
		d.Event, d.ToolName = hookPostToolCall, "terminal"
		d.Extra.Status, d.Extra.ErrorType = status, errType
		return d
	}
	a := newActivityState(false, InputValuesFull)
	if e, _ := a.observe(mk("blocked", "")); e.Status != ActivityStatusError || e.ErrorType != "blocked" {
		t.Fatalf("blocked -> %+v", e)
	}
	if e, _ := a.observe(mk("error", "tool_error")); e.Status != ActivityStatusError || e.ErrorType != "tool_error" {
		t.Fatalf("error/tool_error -> %+v", e)
	}
	if e, _ := a.observe(mk("ok", "")); e.Status != ActivityStatusCompleted || e.ErrorType != "" {
		t.Fatalf("ok -> %+v", e)
	}
}

func TestRedactInput_ValuesUnderInnocentKeys(t *testing.T) {
	in := `{"command": "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI; aws s3 ls --secret-access-key hunter6; kubectl --token eyJhbGciOiJSUzI1NiIsImtpZCI6In0 get pods; gcloud x --password hunter5; curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123' -u admin:hunter2 -d '{\"password\":\"hunter3\", \"token\": \"hunter4\"}' -H 'Authorization: Basic dXNlcjpodW50ZXIy' https://x; export GH=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345; gcloud --access-token=ya29.a0AfH6SMBxyzxyzxyzxyzxyzxyz ls", "plain": "kubectl get pods -n kube-system", "id": 9007199254740993}`
	out := string(redactInput(InputValuesFull, "terminal", json.RawMessage(in)))
	for _, leaked := range []string{"abcdefghijklmnopqrstuvwxyz0123", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "ya29.a0AfH6SMB", "hunter2", "hunter3", "hunter4", "dXNlcjpodW50ZXIy", "eyJhbGciOiJSUzI1NiIsImtpZCI6In0", "hunter5", "wJalrXUtnFEMI", "hunter6"} {
		if strings.Contains(out, leaked) {
			t.Fatalf("leaked %q in %s", leaked, out)
		}
	}
	if !strings.Contains(out, "kubectl get pods -n kube-system") {
		t.Fatalf("innocent value was damaged: %s", out)
	}
	if !strings.Contains(out, `"id":9007199254740993`) {
		t.Fatalf("a 64-bit id lost digits through the scrub: %s", out)
	}
}

// Ordinary text and names are not credentials: the scrub leaves them, so the
// trace carries what the worker adapter's would.
func TestRedactInput_LeavesOrdinaryTextAlone(t *testing.T) {
	in := `{"command": "git commit -m 'basic refactoring'; date -u 12:30; sort -u a:b; kubectl get secret my-secret -o yaml", "secretName": "db-creds", "tokenizer": "cl100k", "max_tokens": 4096, "SECRET_KEY": "s3"}`
	out := string(redactInput(InputValuesFull, "terminal", json.RawMessage(in)))
	for _, kept := range []string{"basic refactoring", "date -u 12:30", "sort -u a:b", "kubectl get secret my-secret -o yaml", `"secretName":"db-creds"`, `"tokenizer":"cl100k"`} {
		if !strings.Contains(out, kept) {
			t.Fatalf("ordinary text %q was scrubbed: %s", kept, out)
		}
	}
	// The accepted price: a key that ends in a secret word is blanked even
	// when it is a count, so SECRET_KEY-style keys are caught.
	if !strings.Contains(out, `"max_tokens":"[redacted]"`) || strings.Contains(out, `"s3"`) {
		t.Fatalf("component rule not applied as documented: %s", out)
	}
}

// The shape default, end to end: a bridge whose Config says nothing about
// input values puts shapes on the bus, with the redaction marker and the
// numbers kept, and the raw argument text absent from every part.
func TestActivity_ShapeIsTheDefaultOnTheBus(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
call("kubectl", {"cmd": "get pods", "token": "hunter2", "nested": {"api_key": "k", "keep": 1}, "project": "p"}, "call_1")
print("the answer")
`), func(c *Config) { c.ActivityInputValues = "" })
	c := gatewayClient(t, url)
	submit(t, c, "task-shape", "list the fleet")
	task := waitTerminal(t, c, "task-shape")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	entries := activityEntries(t, task)
	if len(entries) != 1 {
		t.Fatalf("activity entries = %d, want 1: %+v", len(entries), entries)
	}
	var input map[string]any
	if err := json.Unmarshal(entries[0].Input, &input); err != nil {
		t.Fatal(err)
	}
	if input["cmd"] != shapeOf("get pods") || input["token"] != redactedValue || input["project"] != shapeOf("p") {
		t.Fatalf("default input not shaped as designed: %v", input)
	}
	if nested := input["nested"].(map[string]any); nested["api_key"] != redactedValue || nested["keep"] != float64(1) {
		t.Fatalf("nested default input not shaped as designed: %v", nested)
	}
	for _, art := range task.Artifacts {
		for _, p := range art.Parts {
			if strings.Contains(string(p.Data), "get pods") || strings.Contains(string(p.Data), "hunter2") || strings.Contains(p.Text, "hunter2") {
				t.Fatalf("raw argument text on the bus under %s: %s %s", art.Name, p.Data, p.Text)
			}
		}
	}
}

// Shape mode, the default: no string value leaves the pod, whatever its
// key; the structure, numbers, booleans and the redaction markers stay, and
// the one exception is a tool_call wrapper's own nested tool names, which a
// grader reads. No grammar keeps a "name": a resource name and a credential
// under the same key are the same length to the trace.
func TestRedactInput_ShapeIsTheDefaultAndKeepsOnlyWrapperNames(t *testing.T) {
	in := `{"command":"psql postgresql://admin:hunter2@db/app","PGPASSWORD":"hunter3","name":"seeded-a","namespace":"kube-system","count":3,"dry_run":true,"nested":{"token":"t","id":"abc","note":"free text"},"calls":[{"name":"kanban_create","arguments":{"title":"x","body":"long body"}},{"name":"mcp__cloudmonitoringdashboards__listDashboardsForProjectsAndFolders"}],"resource":"apiVersion: v1\nkind: Secret","project":"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY","cluster":"name with space","kind":"PodDisruptionBudget"}`
	for _, mode := range []string{"", InputValuesShape, "anything-else"} {
		out := string(redactInput(mode, hermesToolCallWrapper, json.RawMessage(in)))
		for _, leaked := range []string{"hunter2", "hunter3", "psql", "free text", "long body", `"title":"x"`, "kind: Secret", "wJalrXUtnFEMI", "name with space", "seeded-a", "kube-system", "PodDisruptionBudget", `"id":"abc"`} {
			if strings.Contains(out, leaked) {
				t.Fatalf("mode %q published a string value %q: %s", mode, leaked, out)
			}
		}
		for _, kept := range []string{`"count":3`, `"dry_run":true`, `"name":"kanban_create"`, `"name":"mcp__cloudmonitoringdashboards__listDashboardsForProjectsAndFolders"`, `"PGPASSWORD":"[redacted]"`, `"token":"[redacted]"`, `"command":"\u003cstring, `, `"note":"\u003cstring, 9 chars\u003e"`, `"name":"\u003cstring, 8 chars\u003e"`} {
			if !strings.Contains(out, kept) {
				t.Fatalf("mode %q lost %q: %s", mode, kept, out)
			}
		}
	}
	// Keys are model-written text too: a credential in a key slot, a map
	// keyed by user data, is shaped; a schema key stays.
	keyed := string(redactInput(InputValuesShape, "http_request", json.RawMessage(`{"headers":{"Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.x.y":"","Accept":"json"},"env":{"GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345":"","PGHOST":"db"},"labels":{"app.kubernetes.io/name":"web","9f3c2a1b7d9f3c2a1b7d9f3c2a1b7d9f":"x"},"dry_run":true,"maxResults":3}`)))
	for _, leaked := range []string{"eyJhbGciOiJIUzI1NiJ9", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "9f3c2a1b7d", "app.kubernetes.io/name"} {
		if strings.Contains(keyed, leaked) {
			t.Fatalf("a key carried text onto the trace: %q in %s", leaked, keyed)
		}
	}
	for _, kept := range []string{`"headers":`, `"Accept":`, `"env":`, `"PGHOST":`, `"labels":`, `"dry_run":true`, `"maxResults":3`, `"\u003ckey 1, `} {
		if !strings.Contains(keyed, kept) {
			t.Fatalf("shape mode lost %q: %s", kept, keyed)
		}
	}
	// A calls[].name below the wrapper's own array is an argument value.
	deep := string(redactInput(InputValuesShape, hermesToolCallWrapper, json.RawMessage(`{"calls":[{"name":"kanban_create","arguments":{"calls":[{"name":"my password is hunter2"}]}}],"other":{"calls":[{"name":"not a tool name either"}]}}`)))
	if strings.Contains(deep, "hunter2") || strings.Contains(deep, "not a tool name") || !strings.Contains(deep, `"name":"kanban_create"`) {
		t.Fatalf("a nested calls[].name below the wrapper's array was kept: %s", deep)
	}
	// Only an element of the root's calls array is a call: a "calls" that is
	// a map, at the root or under another key, or an array nested in the
	// calls array, carries the model's text under "name".
	mapForm := string(redactInput(InputValuesShape, hermesToolCallWrapper, json.RawMessage(`{"calls":{"name":"map form hunter4"},"x":{"calls":{"name":"deeper map hunter5"}},"y":{"calls":[{"name":"deeper array hunter6"}]},"z":[{"calls":[{"name":"array in array hunter7"}]}]}`)))
	for _, leaked := range []string{"hunter4", "hunter5", "hunter6", "hunter7"} {
		if strings.Contains(mapForm, leaked) {
			t.Fatalf("a name off the wrapper's calls array was kept: %q in %s", leaked, mapForm)
		}
	}
	// Under any other tool a calls[].name is the model's text, not a tool name.
	other := string(redactInput(InputValuesShape, "http_request", json.RawMessage(`{"calls":[{"name":"admin:hunter2@db"},{"name":"kanban_create"}]}`)))
	if strings.Contains(other, "hunter2") || strings.Contains(other, `"name":"kanban_create"`) {
		t.Fatalf("calls[].name kept under a tool that is not the wrapper: %s", other)
	}
	full := string(redactInput(InputValuesFull, hermesToolCallWrapper, json.RawMessage(in)))
	if !strings.Contains(full, `"note":"free text"`) || strings.Contains(full, "hunter2") {
		t.Fatalf("full mode: %s", full)
	}
}

// Calls the drain could not report are counted, so the marker says the
// trace is short rather than vouching for it: with the drain's budget
// already spent, two open calls become dropped=2 on the marker.
func TestActivity_DrainSkippedCallsAreCountedDropped(t *testing.T) {
	prev := activityDrainBudget
	activityDrainBudget = 0
	t.Cleanup(func() { activityDrainBudget = prev })
	_, url := startServer(t)
	b := startBridgeCfg(t, url, []string{"true"}, nil)
	a := newActivityState(false, InputValuesFull)
	for _, id := range []string{"c1", "c2"} {
		var d hookDelivery
		d.Event, d.ToolName, d.Extra.ToolCallID = hookPreToolCall, "terminal", id
		a.observe(d)
	}
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-drain", ContextID: "ctx", CorrelationID: "corr"}}
	run.act.Store(a)
	if _, ok := a.truncationMarker(); ok {
		t.Fatal("a marker before the drain")
	}
	b.drainActivity(run)
	m, ok := a.truncationMarker()
	if !ok || m.Dropped != 2 || m.Tool != activityTruncatedTool {
		t.Fatalf("marker after a spent drain = %+v, %v", m, ok)
	}
}

// camelCase is the native key style of most MCP and HTTP tool schemas: a
// secret word that starts a component there is blanked like its snake_case
// twin, while a name that merely starts with the word (secretName,
// tokenizer) still ships.
func TestRedactInput_CamelCaseKeys(t *testing.T) {
	in := `{"accessToken":"eyJhbGciOi","clientSecret":"GOCSPX-abc","dbPassword":"hunter1","authToken":"t1","xApiKey":"k1","gcpAPIKey":"k2","awsSecretAccessKey":"w1","refreshTokens":["r1"],"accessTokenExpiry":3600,"secretName":"db-creds","tokenizer":"cl100k","apiKey":"k3","credentialsPath":"/x","sessionCookie":"sid=c1","cookieHeader":"c2","SESSIONCOOKIE":"c3","AWSSecretAccessKey":"w2","DBPassword":"hunter9","IDToken":"i1","TLSPassphrase":"p9","SecretAccessKey":"w3","TokenValue":"t3","SecretName":"a-name","secretAccessKey":"w4","secretKey":"w5","passwordHash":"h1","tokenPath":"/t","credentialsFile":"/c","SecretRef":"r-name"}`
	out := string(redactInput(InputValuesFull, "http_request", json.RawMessage(in)))
	for _, leaked := range []string{"eyJhbGciOi", "GOCSPX-abc", "hunter1", `"t1"`, `"k1"`, `"k2"`, `"w1"`, `"r1"`, `"k3"`, `"w2"`, "hunter9", `"i1"`, `"p9"`, `"w3"`, `"t3"`, `"w4"`, `"w5"`, `"h1"`, "sid=c1", `"c2"`, `"c3"`} {
		if strings.Contains(out, leaked) {
			t.Fatalf("leaked %q in %s", leaked, out)
		}
	}
	// The accepted price, the camelCase twin of max_tokens.
	if !strings.Contains(out, `"accessTokenExpiry":"[redacted]"`) {
		t.Fatalf("component rule not applied to a camelCase suffix: %s", out)
	}
	// A key that opens with the word is a name only when it goes on as a
	// name, reference or location (secretName, SecretRef, tokenPath,
	// credentialsFile); otherwise it is a credential (secretAccessKey).
	for _, kept := range []string{`"secretName":"db-creds"`, `"tokenizer":"cl100k"`, `"credentialsPath":"/x"`, `"SecretName":"a-name"`, `"tokenPath":"/t"`, `"credentialsFile":"/c"`, `"SecretRef":"r-name"`} {
		if !strings.Contains(out, kept) {
			t.Fatalf("name %q was scrubbed: %s", kept, out)
		}
	}
}

// A quoted secret with spaces in it is scrubbed whole: the tail of a
// passphrase must not ship behind a marker that says it was redacted.
func TestRedactInput_QuotedValuesWithSpaces(t *testing.T) {
	in := `{"command": "mysql --password \"my pass word\" -h db; curl -d '{\"password\": \"my pass word\"}' https://x; export TOKEN='one two three'; gcloud --api-key=\"k e y\" ls; psql --password 'p q' -c 'select 1'; echo --token unterminated\" tail; curl -u \"admin:my pass\" https://y; curl -uadmin:hunter8 https://z; curl --cookie 'session=9f3c2a1b7d' https://k; curl -b sid=c0ffee https://m; export DB_PASSWORD=p@ss,w0rd; echo token=ab}cd; curl -u 'admin:p&ss' https://w && echo done", "plain": "kubectl get pods -h db"}`
	out := string(redactInput(InputValuesFull, "terminal", json.RawMessage(in)))
	for _, leaked := range []string{"pass word", "one two three", "k e y", `'p q'`, "unterminated", "my pass", "hunter8", "w0rd", "}cd", "p&ss", "session=9f3c2a1b7d", "sid=c0ffee"} {
		if strings.Contains(out, leaked) {
			t.Fatalf("leaked %q in %s", leaked, out)
		}
	}
	for _, kept := range []string{"-h db", "https://x", "-c 'select 1'", `"plain":"kubectl get pods -h db"`, "https://y", "https://z", "https://k", "https://m", "https://w ", "echo done"} {
		if !strings.Contains(out, kept) {
			t.Fatalf("text after the secret was lost: %q missing in %s", kept, out)
		}
	}
	// An unterminated quote falls back to the whitespace rule: the first
	// word is scrubbed and the rest of the line survives.
	if !strings.Contains(out, ` tail;`) {
		t.Fatalf("unterminated-quote fallback lost the line: %s", out)
	}
}

// A curl command continued over backslash-newlines is one command: its -u
// and -b on a continuation line are scrubbed, the lines around them kept,
// and the next command after the separator is untouched.
func TestRedactInput_CurlContinuationLines(t *testing.T) {
	in := `{"command": "curl -sS https://api.example.com/v1 \\\n  -u admin:hunter2 \\\n  -H 'Accept: json'; curl https://q \\\r\n -b sid=deadbeef99 \\\n -o out.json\necho -u notcurl"}`
	out := string(redactInput(InputValuesFull, "terminal", json.RawMessage(in)))
	for _, leaked := range []string{"hunter2", "deadbeef99"} {
		if strings.Contains(out, leaked) {
			t.Fatalf("leaked %q in %s", leaked, out)
		}
	}
	for _, kept := range []string{"https://api.example.com/v1", "-H 'Accept: json'", "https://q", "-o out.json", "echo -u notcurl"} {
		if !strings.Contains(out, kept) {
			t.Fatalf("expected %q in %s", kept, out)
		}
	}
}

// The scheme token is case-insensitive, and a credential can arrive under a
// key outside the first list of words: a private key, a passphrase.
func TestRedactInput_LowercaseBasicAndKeyMaterial(t *testing.T) {
	in := `{"env": {"PGPASSWORD": "hunter11", "MYSQLPASSWORD": "hunter12", "PGHOST": "db"}, "headers": {"Cookie": "session=9f3c2a1b", "Accept": "json"}, "yaml": "authorization:\n  mode: Webhook\npassword:\n  valueFrom:\n    secretKeyRef: x\n", "urls": "psql postgresql://admin:hunter13@db.internal:5432/app; git clone https://user:glpat-xxxx@gitlab.example/r.git; curl -H 'Cookie: session=abc123' https://c", "kubeconfig": {"users": [{"user": {"client-key-data": "LS0tLS1CRUdJTi", "client-certificate-data": "cert-ok"}}]}, "cmd2": "kubectl config set-credentials u --client-key-data=LS0tLS1CRUdJTi2; curl -H 'Authorization: lin_api_9f3c2a1b7d' https://api.linear.app; curl -H \"Authorization: 9f3c2a1b7d\" https://r", "content": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7\nabc\n-----END RSA PRIVATE KEY-----\n", "command": "cat > id_rsa <<EOF\n-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXkt\n-----END OPENSSH PRIVATE KEY-----\nEOF; curl -H 'authorization: basic dXNlcjpodW50ZXIy' https://x; curl -sS https://api.example.com/v1/clusters -H 'Authorization: Basic cm9vdDp0b29y' -u admin:pw; curl -H 'Authorization: Token 0123456789abcdef0123' https://api; curl -H \"Authorization: Bearer abc123def456\" https://b; curl -H 'Authorization: Digest username=\"u\"' https://d; ssh-keygen -N 'my long pass' ; tool --private-key /dev/stdin --passphrase hunter7; echo ssh_key=AAAAB3NzaC1yc2E", "private_key": "-----BEGIN PRIVATE KEY-----\nMIIE", "passphrase": "p1", "sshKey": "k1", "signing_key_id": "s1", "privateKeyPath": "/x", "basic": "basic refactoring"}`
	out := string(redactInput(InputValuesFull, "terminal", json.RawMessage(in)))
	for _, leaked := range []string{"dXNlcjpodW50ZXIy", "cm9vdDp0b29y", "admin:pw", "0123456789abcdef0123", "abc123def456", `username=\"u\"`, "BEGIN PRIVATE KEY", "MIIEowIBAAKCAQEA7", "b3BlbnNzaC1rZXkt", "LS0tLS1CRUdJTi", "lin_api_9f3c2a1b7d", "9f3c2a1b7d", "hunter11", "hunter12", "session=9f3c2a1b", "hunter13", "glpat-xxxx", "session=abc123", `"p1"`, `"k1"`, `"s1"`, "hunter7", "AAAAB3NzaC1yc2E", "/dev/stdin"} {
		if strings.Contains(out, leaked) {
			t.Fatalf("leaked %q in %s", leaked, out)
		}
	}
	// A short word after "basic" is not a credential, and a camelCase key
	// that starts with the word is a name, as for secretName.
	// The curl command's URL and method survive its credential.
	for _, kept := range []string{`"basic":"basic refactoring"`, `"privateKeyPath":"/x"`, "curl -sS https://api.example.com/v1/clusters -H '[redacted]'[redacted]", "https://api", "https://b", "https://d", `"client-certificate-data":"cert-ok"`, "https://api.linear.app", "https://r", `"PGHOST":"db"`, `"Accept":"json"`, "mode: Webhook", "secretKeyRef: x", "postgresql://[redacted]@db.internal:5432/app", "https://[redacted]@gitlab.example/r.git", "https://c"} {
		if !strings.Contains(out, kept) {
			t.Fatalf("expected %q in %s", kept, out)
		}
	}
}

// hermes's tool_call wrapper over the cap keeps its nested tool names: each
// call's arguments is capped on its own, so a tool_called check on a nested
// tool still sees it, and the wrapper is not the whole-input stand-in.
func TestRedactInput_WrapperOverTheCapKeepsNestedNames(t *testing.T) {
	big := strings.Repeat("x", activityInputCap)
	in := `{"calls":[{"name":"kanban_create","arguments":{"title":"a","body":"` + big + `","token":"s1"}},{"name":"kanban_comment","arguments":{"body":"` + big + `"}},{"name":"kanban_list"},{"name":"kanban_get","arguments":{"id":7}}]}`
	out := redactInput(InputValuesFull, hermesToolCallWrapper, json.RawMessage(in))
	if len(out) > activityInputCap {
		t.Fatalf("wrapper still over the cap: %d bytes", len(out))
	}
	var v struct {
		Truncated *bool `json:"truncated"`
		Calls     []struct {
			Name string         `json:"name"`
			Args map[string]any `json:"arguments"`
		} `json:"calls"`
	}
	if err := json.Unmarshal(out, &v); err != nil || v.Truncated != nil {
		t.Fatalf("wrapper became the whole stand-in: %s (%v)", out, err)
	}
	if len(v.Calls) != 4 || v.Calls[0].Name != "kanban_create" || v.Calls[1].Name != "kanban_comment" || v.Calls[2].Name != "kanban_list" || v.Calls[3].Name != "kanban_get" {
		t.Fatalf("nested names lost: %s", out)
	}
	// A small sibling of a large call stays verbatim: no stand-in that is
	// larger than what it replaces and says truncated of nothing.
	if v.Calls[3].Args["id"] != float64(7) || v.Calls[3].Args["truncated"] != nil {
		t.Fatalf("small nested arguments were replaced: %s", out)
	}
	for i := 0; i < 2; i++ {
		if v.Calls[i].Args["truncated"] != true || v.Calls[i].Args["bytes"] == nil {
			t.Fatalf("call %d arguments not a stand-in: %s", i, out)
		}
		head, _ := v.Calls[i].Args["head"].(string)
		if len(head) == 0 || len(head) > activityInputCallHead {
			t.Fatalf("call %d head = %d bytes", i, len(head))
		}
	}
	if strings.Contains(string(out), "s1") {
		t.Fatalf("secret under a nested key survived: %s", out)
	}
	if v.Calls[2].Args != nil {
		t.Fatalf("a call without arguments grew some: %s", out)
	}

	// The same wrapper under another tool name is capped whole, as before.
	whole := redactInput(InputValuesFull, "terminal", json.RawMessage(in))
	if !strings.HasPrefix(string(whole), `{"bytes":`) && !strings.Contains(string(whole), `"truncated":true`) {
		t.Fatalf("non-wrapper over the cap was not the stand-in: %s", whole)
	}
	if strings.Contains(string(whole), `"name":"kanban_comment"`) {
		t.Fatalf("whole stand-in carried a full nested call: %s", whole)
	}

	// Many small calls that are over the cap together: the second pass
	// replaces every arguments object, head-less, and the names survive.
	var small []string
	for i := 0; i < 12; i++ {
		small = append(small, `{"name":"kanban_comment","arguments":{"id":`+strings.Repeat("7", 3)+`,"body":"`+strings.Repeat("s", 200)+`"}}`)
	}
	manySmall := redactInput(InputValuesFull, hermesToolCallWrapper, json.RawMessage(`{"calls":[`+strings.Join(small, ",")+`]}`))
	if len(manySmall) > activityInputCap || strings.Count(string(manySmall), `"name":"kanban_comment"`) != 12 || strings.Contains(string(manySmall), `"head":"{`) {
		t.Fatalf("many small calls lost their names or kept heads: %d bytes %s", len(manySmall), manySmall[:min(len(manySmall), 160)])
	}

	// Too many calls to fit even with the heads dropped fall back to the
	// whole stand-in rather than an over-cap wrapper.
	var many []string
	for i := 0; i < activityInputCap/8; i++ {
		many = append(many, `{"name":"kanban_create_`+strings.Repeat("y", 40)+`","arguments":{"b":"`+big+`"}}`)
	}
	fallback := redactInput(InputValuesFull, hermesToolCallWrapper, json.RawMessage(`{"calls":[`+strings.Join(many, ",")+`]}`))
	if len(fallback) > activityInputCap || !strings.Contains(string(fallback), `"truncated":true`) {
		t.Fatalf("oversized wrapper did not fall back: %d bytes %s", len(fallback), fallback[:min(len(fallback), 120)])
	}
}

// A source managed file that exists but cannot be read fails the scope
// rather than starting the child without the operator's pins; nothing is
// left behind.
func TestChildManagedScope_FailsOnAnUnreadableSourceAndLeavesNothing(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root reads everything")
	}
	src := t.TempDir()
	if err := os.WriteFile(filepath.Join(src, "config.yaml"), []byte("model: {default: x}\n"), 0o000); err != nil {
		t.Fatal(err)
	}
	scratch := t.TempDir()
	b := &Bridge{cfg: Config{ScratchDir: scratch, ManagedScopeDir: src}}
	if dir, err := b.childManagedScope("task-unreadable"); err == nil {
		t.Fatalf("unreadable source accepted: %s", dir)
	}
	if entries, _ := os.ReadDir(scratch); len(entries) != 0 {
		t.Fatalf("a failed scope left files: %v", entries)
	}
	// Absent is different: nothing to copy, the hook alone.
	b2 := &Bridge{cfg: Config{ScratchDir: t.TempDir(), ManagedScopeDir: filepath.Join(t.TempDir(), "absent")}}
	if _, err := b2.childManagedScope("task-absent"); err != nil {
		t.Fatalf("absent source refused: %v", err)
	}
}

// Past the budget, calls are counted, not published, and one marker at the
// terminal says how many; the lifecycle events keep their room on the subject.
func TestActivity_CallsPastTheBudgetBecomeOneMarker(t *testing.T) {
	prev, prevReserve := activityEntryBudget, activityHeartbeatReserve
	activityEntryBudget, activityHeartbeatReserve = 3, 0
	t.Cleanup(func() { activityEntryBudget, activityHeartbeatReserve = prev, prevReserve })
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
for i in range(5):
    call("kubectl", {"n": i}, "c%d" % i)
print("done")
`), nil)
	c := gatewayClient(t, url)
	submit(t, c, "task-budget", "loop")
	task := waitTerminal(t, c, "task-budget")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s", task.State)
	}
	entries := activityEntries(t, task)
	if len(entries) != 4 {
		t.Fatalf("entries = %d, want 3 calls + 1 marker: %+v", len(entries), entries)
	}
	marker := entries[3]
	if marker.Tool != activityTruncatedTool || marker.Status != ActivityStatusTruncated || marker.Dropped != 2 {
		t.Fatalf("marker = %+v, want %s/%s dropped=2", marker, activityTruncatedTool, ActivityStatusTruncated)
	}
	trail := eventTrail(t, replayEvents(t, url, "task-budget"))
	if trail[len(trail)-1] != "completed/final" || trail[len(trail)-2] != "result" || trail[len(trail)-3] != "activity" {
		t.Fatalf("marker not ahead of the result: %v", trail)
	}
}

// The trace stops short of the budget by the heartbeat's reserve, so the
// heartbeat runs to the terminal on a looping run: with a budget of 4 and a
// reserve of 2, three calls become two parts and a marker while the
// heartbeat still gets its two.
func TestActivity_TraceLeavesTheHeartbeatItsReserve(t *testing.T) {
	prev, prevReserve := activityEntryBudget, activityHeartbeatReserve
	activityEntryBudget, activityHeartbeatReserve = 4, 2
	t.Cleanup(func() { activityEntryBudget, activityHeartbeatReserve = prev, prevReserve })
	a := newActivityState(false, InputValuesFull)
	got := 0
	for i := 0; i < 3; i++ {
		if a.underBudget() {
			got++
		}
	}
	if got != 2 || a.dropped != 1 {
		t.Fatalf("trace parts = %d, dropped = %d; want 2 and 1", got, a.dropped)
	}
	beats := 0
	for i := 0; i < 3; i++ {
		if a.heartbeatUnderBudget() {
			beats++
		}
	}
	if beats != 2 {
		t.Fatalf("heartbeats = %d, want the reserve (2)", beats)
	}
	// And the other way: a heartbeat that spent its share took nothing from
	// the trace's.
	b := newActivityState(false, InputValuesFull)
	for i := 0; i < 5; i++ {
		b.heartbeatUnderBudget()
	}
	if !b.underBudget() || !b.underBudget() || b.underBudget() {
		t.Fatalf("the trace's share moved with the heartbeat's spend: published=%d dropped=%d", b.published, b.dropped)
	}
}

// The heartbeat has a share of its own, the reserve: a short interval under
// a long run cannot spend the subject either. Past its share it stops, and
// the marker counts calls, not heartbeats.
func TestActivity_HeartbeatStopsAtItsShare(t *testing.T) {
	prev, prevReserve := activityEntryBudget, activityHeartbeatReserve
	activityEntryBudget, activityHeartbeatReserve = 2, 2
	t.Cleanup(func() { activityEntryBudget, activityHeartbeatReserve = prev, prevReserve })
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
time.sleep(1.0)
print("done")
`), func(c *Config) { c.ProgressInterval = 50 * time.Millisecond })
	c := gatewayClient(t, url)
	submit(t, c, "task-heartbeat-budget", "wait")
	task := waitTerminal(t, c, "task-heartbeat-budget")
	progress := task.Artifact(lib.ArtifactProgress)
	if progress == nil || len(progress.Parts) != 2 {
		t.Fatalf("progress parts = %v, want exactly the budget (2)", progress)
	}
	if got := activityEntries(t, task); len(got) != 0 {
		t.Fatalf("heartbeats produced a marker or entries: %+v", got)
	}
}

// The task id becomes a directory name under ScratchDir; one that is not a
// plain path segment is refused before anything is written.
func TestChildManagedScope_RefusesATaskIDThatIsNotAPathSegment(t *testing.T) {
	scratch := t.TempDir()
	b := &Bridge{cfg: Config{ScratchDir: scratch}}
	for _, bad := range []string{"../escape", "a/b", "..", "", "task with space", strings.Repeat("x", 129)} {
		if dir, err := b.childManagedScope(bad); err == nil {
			t.Fatalf("task id %q accepted: %s", bad, dir)
		}
	}
	// A task id that names something already in the scratch dir is refused
	// rather than adopted: a shared mount's directory, a symlink a same-uid
	// process planted. Neither is written into or removed.
	if err := os.MkdirAll(filepath.Join(scratch, "cache", "keep"), 0o755); err != nil {
		t.Fatal(err)
	}
	target := t.TempDir()
	if err := os.Symlink(target, filepath.Join(scratch, "linked")); err != nil {
		t.Fatal(err)
	}
	for _, taken := range []string{"cache", "linked"} {
		if dir, err := b.childManagedScope(taken); err == nil {
			t.Fatalf("task id %q adopted an existing entry: %s", taken, dir)
		}
	}
	if _, err := os.Stat(filepath.Join(scratch, "cache", "keep")); err != nil {
		t.Fatalf("a refused id removed the directory it named: %v", err)
	}
	if entries, _ := os.ReadDir(target); len(entries) != 0 {
		t.Fatalf("a refused id wrote through the symlink: %v", entries)
	}
	// Any legal path segment the bus accepts as a task id gets a scope: the
	// sweep keys on the marker, not on a name shape, so no id is refused
	// for its spelling.
	for _, ok := range []string{"smoke-1", "abc", "t1"} {
		dir, err := b.childManagedScope(ok)
		if err != nil {
			t.Fatalf("task id %q refused: %v", ok, err)
		}
		if _, err := os.Stat(filepath.Join(dir, scopeMarkerFile)); err != nil {
			t.Fatalf("scope for %q carries no marker: %v", ok, err)
		}
		_ = os.RemoveAll(dir)
	}
	entries, _ := os.ReadDir(scratch)
	for _, e := range entries {
		if e.Name() != "cache" && e.Name() != "linked" {
			t.Fatalf("a refused id left something in the scratch dir: %v", entries)
		}
	}
	if dir, err := b.childManagedScope("task-ok_1"); err != nil || filepath.Dir(dir) != scratch {
		t.Fatalf("a plain id was refused or misplaced: %s %v", dir, err)
	}
}

// The start-time sweep takes a previous incarnation's task scopes, known by
// the marker file, and nothing else: BRIDGE_SCRATCH_DIR may name a mount
// the bridge shares, so the directory itself, a file in it, and a
// subdirectory without the marker all survive a restart.
func TestListenActivity_SweepsOnlyTaskScopesAtStart(t *testing.T) {
	scratch := t.TempDir()
	leftover := filepath.Join(scratch, "task-0123abcd")
	if err := os.MkdirAll(leftover, 0o700); err != nil {
		t.Fatal(err)
	}
	for name, body := range map[string]string{scopeMarkerFile: "", managedConfigFile: "model: {}\n", managedEnvFile: "SECRET=x\n"} {
		if err := os.WriteFile(filepath.Join(leftover, name), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	// A scope under any task id goes too, by its marker: the bus accepts
	// any DNS-1123 label, so the name shape is not the mark.
	bareName := filepath.Join(scratch, "smoke-1")
	if err := os.MkdirAll(bareName, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(bareName, scopeMarkerFile), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	foreignFile := filepath.Join(scratch, "notes")
	if err := os.WriteFile(foreignFile, []byte("not the bridge's\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// BRIDGE_SCRATCH_DIR may name a directory the bridge shares: a directory
	// without the marker, however it is named, is not a scope the bridge
	// wrote.
	foreignDirs := []string{filepath.Join(scratch, "lost+found", "inner"), filepath.Join(scratch, "cache", "inner"), filepath.Join(scratch, "data"), filepath.Join(scratch, "task-foreign", "inner")}
	for _, d := range foreignDirs {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}

	// A scope nobody can remove (a same-uid writer in the pod can make
	// one) is logged and left; the bridge still starts.
	stuck := filepath.Join(scratch, "task-stuck")
	if err := os.MkdirAll(filepath.Join(stuck, "inner"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stuck, scopeMarkerFile), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if os.Geteuid() != 0 {
		if err := os.Chmod(stuck, 0o500); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = os.Chmod(stuck, 0o700) })
	}

	b := &Bridge{cfg: Config{ScratchDir: scratch, ActivityListen: "127.0.0.1:0", Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}}
	if err := b.listenActivity(); err != nil {
		t.Fatalf("listenActivity: %v", err)
	}
	t.Cleanup(func() { _ = b.activityLn.Close() })

	for _, gone := range []string{leftover, bareName} {
		if _, err := os.Stat(gone); !os.IsNotExist(err) {
			t.Fatalf("a previous incarnation's scope %s survived the start: stat err = %v", gone, err)
		}
	}
	for _, kept := range append([]string{scratch, foreignFile}, foreignDirs...) {
		if _, err := os.Stat(kept); err != nil {
			t.Fatalf("the sweep took %s, which is not a task scope: %v", kept, err)
		}
	}
	// With the door closed the sweep still runs: a fresh leftover goes.
	again := filepath.Join(scratch, "task-again")
	if err := os.MkdirAll(again, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(again, scopeMarkerFile), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	closed := &Bridge{cfg: Config{ScratchDir: scratch, ActivityListen: "", Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}}
	if err := closed.listenActivity(); err != nil {
		t.Fatalf("listenActivity (door closed): %v", err)
	}
	if _, err := os.Stat(again); !os.IsNotExist(err) {
		t.Fatalf("a door-closed start left a scope behind: stat err = %v", err)
	}
	// A scratch dir that cannot exist (under a file) fails the start only
	// when the door is open and scopes would be written there; closed, the
	// executor starts and says so in the log.
	unusable := filepath.Join(foreignFile, "scratch")
	closedUnusable := &Bridge{cfg: Config{ScratchDir: unusable, ActivityListen: "", Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}}
	if err := closedUnusable.listenActivity(); err != nil {
		t.Fatalf("door closed, unusable scratch dir refused the start: %v", err)
	}
	openUnusable := &Bridge{cfg: Config{ScratchDir: unusable, ActivityListen: "127.0.0.1:0", Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}}
	if err := openUnusable.listenActivity(); err == nil {
		_ = openUnusable.activityLn.Close()
		t.Fatal("door open, unusable scratch dir did not fail the start")
	}
	// A directory still has to be a directory to go: a file named like a
	// task id is not a scope the bridge wrote.
	fileNamedLikeATask := filepath.Join(scratch, "task-file")
	if err := os.WriteFile(fileNamedLikeATask, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := sweepTaskScopes(scratch, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(fileNamedLikeATask); err != nil {
		t.Fatalf("the sweep took a file: %v", err)
	}
}

// The dedupe remembers the most recent activitySeenCap delivery ids: past
// the cap the oldest is forgotten and the newest still dedupes, so a late
// retry of a recent post is one call however long the run.
func TestActivity_DedupeKeepsTheMostRecentIDs(t *testing.T) {
	a := newActivityState(false, InputValuesShape)
	post := func(id string) (ActivityEntry, bool) {
		var d hookDelivery
		if err := json.Unmarshal([]byte(`{"hook_event_name":"post_tool_call","tool_name":"terminal","delivery_id":"`+id+`","extra":{"tool_call_id":"`+id+`","status":"ok"}}`), &d); err != nil {
			t.Fatal(err)
		}
		return a.observe(d)
	}
	for i := 0; i < activitySeenCap+1; i++ {
		if _, ok := post(fmt.Sprintf("d-%d", i)); !ok {
			t.Fatalf("delivery %d not observed", i)
		}
	}
	if _, ok := post(fmt.Sprintf("d-%d", activitySeenCap)); ok {
		t.Fatal("a retry of the newest delivery past the cap was counted again")
	}
	if _, ok := post("d-0"); !ok {
		t.Fatal("the oldest id was not the one forgotten")
	}
	if a.calls != activitySeenCap+2 {
		t.Fatalf("calls = %d", a.calls)
	}
}

// curl's -u folded into a cluster of boolean short flags, and a URL
// password holding / or @, are scrubbed in full mode; --user-agent is not
// --user, and a URL with no userinfo keeps an @ in its path.
func TestRedactInput_CurlClustersAndUserinfoWithSlashes(t *testing.T) {
	in := `{"command": "curl -su admin:hunter2 https://h; curl -fsSLu root:hunter3 https://i -o f; curl --user-agent ua https://j; psql postgres://u:pa/ss@h:5432/db; curl https://u:pa@ss@k/x; echo https://plain.example/a@b; curl http://api.internal:8080/notify?to=ops@example.com; curl http://user:p4ss@h:8080/x"}`
	out := string(redactInput(InputValuesFull, "terminal", json.RawMessage(in)))
	for _, leaked := range []string{"hunter2", "hunter3", "pa/ss", "pa@ss", "ss@k", "p4ss"} {
		if strings.Contains(out, leaked) {
			t.Fatalf("leaked %q in %s", leaked, out)
		}
	}
	for _, kept := range []string{"https://h;", "https://i -o f", "--user-agent ua https://j", "postgres://[redacted]@h:5432/db", "https://[redacted]@k/x", "https://plain.example/a@b", "http://api.internal:8080/notify?to=ops@example.com", "http://[redacted]@h:8080/x"} {
		if !strings.Contains(out, kept) {
			t.Fatalf("expected %q in %s", kept, out)
		}
	}
}

// Shape mode never publishes a string, so it skips the credential scrub
// over values: the length it reports is the string's own, and the markers
// it keeps are the key-based ones.
func TestRedactInput_ShapeModeSkipsTheValueScrub(t *testing.T) {
	out := string(redactInput(InputValuesShape, "terminal", json.RawMessage(`{"h":"Bearer abcdefghijklmnop","token":"x"}`)))
	if !strings.Contains(out, `"h":"\u003cstring, 23 chars\u003e"`) || !strings.Contains(out, `"token":"[redacted]"`) {
		t.Fatalf("shape mode: %s", out)
	}
}

// One field of a surprising type does not drop the delivery: the call is
// observed with that field zero, so a finished call is never recorded
// interrupted for a word in duration_ms or a number in tool_call_id.
func TestActivity_OneFieldsTypeDoesNotDropTheDelivery(t *testing.T) {
	a := newActivityState(false, InputValuesShape)
	var d hookDelivery
	if err := json.Unmarshal([]byte(`{"hook_event_name":"post_tool_call","tool_name":"terminal","tool_input":{"command":"ls"},"timestamp":{"x":1},"delivery_id":12,"extra":{"tool_call_id":7,"duration_ms":"fast","status":"ok","error_type":null}}`), &d); err != nil {
		t.Fatalf("a field's type dropped the delivery: %v", err)
	}
	e, ok := a.observe(d)
	if !ok || e.Tool != "terminal" || e.Status != ActivityStatusCompleted || e.DurationMs != 0 || e.CallID != "7" || e.At != "" {
		t.Fatalf("observed %+v %v", e, ok)
	}
	if err := json.Unmarshal([]byte(`{"hook_event_name":"post_tool_call","tool_name":"terminal","extra":"not an object"}`), &d); err != nil || d.Extra.ToolCallID != "" || d.Event != "post_tool_call" {
		t.Fatalf("a non-object extra: %v %+v", err, d)
	}
	if err := json.Unmarshal([]byte(`{"hook_event_name":"post_tool_call","extra":"not an object"`), &d); err == nil {
		t.Fatal("a body that is not JSON parsed")
	}
}

// A key shaped like a credential is shaped whatever its body: a known
// prefix, a long single-case run, or classes that churn like base62; a
// camelCase schema key with a few words stays.
func TestRedactInput_TokenShapedKeys(t *testing.T) {
	out := string(redactInput(InputValuesShape, "terminal", json.RawMessage(`{"ghp_abcdefghijklmnopqrstuvwABCDEFGHIJKL":1,"AIzaSyA_bcdefghijklmnopqrstuvwxyzABC":1,"a1B2c3D4e5F6g7":1,"QWERTYUIOPASDFGHJKLZXCVB":1,"resourceVersion":1,"includeUninitializedResourceVersion":1,"sha256Digest":1}`)))
	for _, leaked := range []string{"ghp_", "AIza", "a1B2c3D4", "QWERTYUIOPASDFGHJKLZXCVB"} {
		if strings.Contains(out, leaked) {
			t.Fatalf("a token-shaped key was published: %q in %s", leaked, out)
		}
	}
	for _, kept := range []string{`"resourceVersion":1`, `"includeUninitializedResourceVersion":1`, `"sha256Digest":1`} {
		if !strings.Contains(out, kept) {
			t.Fatalf("a schema key was shaped: %q missing in %s", kept, out)
		}
	}
}

// The child's config is the operator's as written: an id above 2^53 keeps
// its digits, the operator's own outbound hooks stay ahead of the door's
// entry, and a hooks or hooks.outbound of another shape fails the spawn
// like an unreadable file rather than being replaced.
func TestChildManagedScope_KeepsLargeIntegersAndRefusesMisshapenHooks(t *testing.T) {
	src := t.TempDir()
	if err := os.WriteFile(filepath.Join(src, "config.yaml"), []byte("channel_id: 123456789012345678\nhooks:\n  outbound:\n    - name: theirs\n      url: https://audit.example/\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	b := &Bridge{cfg: Config{ScratchDir: t.TempDir(), ManagedScopeDir: src}}
	dir, err := b.childManagedScope("task-ints")
	if err != nil {
		t.Fatal(err)
	}
	out, err := os.ReadFile(filepath.Join(dir, managedConfigFile))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), "channel_id: 123456789012345678\n") || !strings.Contains(string(out), "name: theirs") || !strings.Contains(string(out), "name: "+hookEntryName) || strings.Index(string(out), "name: theirs") > strings.Index(string(out), "name: "+hookEntryName) {
		t.Fatalf("child config rewrote the operator's: %s", out)
	}
	for _, bad := range []string{"hooks:\n  outbound:\n    name: theirs\n", "hooks: 3\n", "hooks:\n  outbound: theirs\n"} {
		if err := os.WriteFile(filepath.Join(src, "config.yaml"), []byte(bad), 0o600); err != nil {
			t.Fatal(err)
		}
		scratch := t.TempDir()
		b := &Bridge{cfg: Config{ScratchDir: scratch, ManagedScopeDir: src}}
		if dir, err := b.childManagedScope("task-bad"); err == nil {
			t.Fatalf("misshapen hooks %q accepted: %s", bad, dir)
		}
		if entries, _ := os.ReadDir(scratch); len(entries) != 0 {
			t.Fatalf("a refused scope left files: %v", entries)
		}
	}
	// A null hooks is an absence, not a shape.
	if err := os.WriteFile(filepath.Join(src, "config.yaml"), []byte("hooks:\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := (&Bridge{cfg: Config{ScratchDir: t.TempDir(), ManagedScopeDir: src}}).childManagedScope("task-null"); err != nil {
		t.Fatalf("null hooks refused: %v", err)
	}
}
