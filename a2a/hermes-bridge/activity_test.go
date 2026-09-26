package hermesbridge

import (
	"encoding/json"
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
// the consumer wait are startBridgeWith's.
func startBridgeCfg(t *testing.T, url string, command []string, mutate func(*Config)) *Bridge {
	t.Helper()
	cfg := Config{
		NATSURL:        url,
		Command:        command,
		TaskDeadline:   20 * time.Second,
		KillGrace:      500 * time.Millisecond,
		ActivityListen: "127.0.0.1:0",
	}
	if mutate != nil {
		mutate(&cfg)
	}
	return startBridgeWith(t, cfg)
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

// artifactNames returns, in stream order, the artifact name of every
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
	if task.State != lib.StateFailed || task.FinalMessage == nil || !strings.Contains(joinText(task.FinalMessage.Parts), "deadline-exceeded") {
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
	if _, err := os.Stat(want); !os.IsNotExist(err) {
		t.Fatalf("child scope %s not removed after the task (err=%v)", want, err)
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
		t.Run(tc.name, func(t *testing.T) { tc.want(t, redactInput(json.RawMessage(tc.in))) })
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

func joinText(parts []lib.Part) string {
	var b strings.Builder
	for _, p := range parts {
		b.WriteString(p.Text)
	}
	return b.String()
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

	submit(t, c, "task-a", "A")
	submit(t, c, "task-b", "B")
	ta := waitTerminal(t, c, "task-a")
	tb := waitTerminal(t, c, "task-b")
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
	a, _ := newActivityState(false)
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
	in := `{"command": "kubectl --token eyJhbGciOiJSUzI1NiIsImtpZCI6In0 get pods; gcloud x --password hunter5; curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123' -u admin:hunter2 -d '{\"password\":\"hunter3\", \"token\": \"hunter4\"}' -H 'Authorization: Basic dXNlcjpodW50ZXIy' https://x; export GH=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345; gcloud --access-token=ya29.a0AfH6SMBxyzxyzxyzxyzxyzxyz ls", "plain": "kubectl get pods -n kube-system", "id": 9007199254740993}`
	out := string(redactInput(json.RawMessage(in)))
	for _, leaked := range []string{"abcdefghijklmnopqrstuvwxyz0123", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "ya29.a0AfH6SMB", "hunter2", "hunter3", "hunter4", "dXNlcjpodW50ZXIy", "eyJhbGciOiJSUzI1NiIsImtpZCI6In0", "hunter5"} {
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

// Past the budget, calls are counted, not published, and one marker at the
// terminal says how many; the lifecycle events keep their room on the subject.
func TestActivity_CallsPastTheBudgetBecomeOneMarker(t *testing.T) {
	prev := activityEntryBudget
	activityEntryBudget = 3
	t.Cleanup(func() { activityEntryBudget = prev })
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

// The heartbeat shares the budget: a short interval under a long run cannot
// spend the subject either. Past the budget it simply stops, and the marker
// counts calls, not heartbeats.
func TestActivity_HeartbeatSharesTheBudget(t *testing.T) {
	prev := activityEntryBudget
	activityEntryBudget = 2
	t.Cleanup(func() { activityEntryBudget = prev })
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
	entries, _ := os.ReadDir(scratch)
	if len(entries) != 0 {
		t.Fatalf("a refused id left something in the scratch dir: %v", entries)
	}
	if dir, err := b.childManagedScope("task-ok_1"); err != nil || filepath.Dir(dir) != scratch {
		t.Fatalf("a plain id was refused or misplaced: %s %v", dir, err)
	}
}
