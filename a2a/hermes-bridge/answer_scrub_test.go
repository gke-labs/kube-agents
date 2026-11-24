package hermesbridge

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/answerscrub"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nuid"
)

// Synthetic values only: none of these is, or was copied from, a real
// credential.
const (
	synthSecretPlain  = "synthetic-pass-0001"
	synthSecretPlain2 = "synthetic-token-0002"
	synthCardToken    = "quietpasture-synthetic-0003"
)

func b64(s string) string { return base64.StdEncoding.EncodeToString([]byte(s)) }

// secretJSON is a Secret as kubectl's -o json prints it.
func secretJSON(t *testing.T, typ string, data map[string]string) string {
	t.Helper()
	enc := map[string]string{}
	for k, v := range data {
		enc[k] = b64(v)
	}
	raw, err := json.MarshalIndent(map[string]any{
		"apiVersion": "v1", "kind": "Secret", "type": typ,
		"metadata": map[string]any{"name": "app-db", "namespace": "prod"},
		"data":     enc,
	}, "", "    ")
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

// terminalResult is a terminal tool's result as hermes's post_tool_call
// carries it: a JSON string whose content is JSON with the command's
// output in it.
func terminalResult(t *testing.T, output string) json.RawMessage {
	t.Helper()
	inner, _ := json.Marshal(map[string]any{"output": output, "exit_code": 0, "error": nil})
	outer, _ := json.Marshal(string(inner))
	return outer
}

// secretYAMLResult is a terminal result printing one Secret as YAML, its
// data in the order given.
func secretYAMLResult(t *testing.T, vals []string) json.RawMessage {
	t.Helper()
	var sb strings.Builder
	sb.WriteString("apiVersion: v1\nkind: Secret\ndata:\n")
	for i, v := range vals {
		fmt.Fprintf(&sb, "  k%04d: %s\n", i, b64(v))
	}
	return terminalResult(t, sb.String())
}

func synthVals(prefix string, n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = fmt.Sprintf("synthetic-%s-%06d", prefix, i)
	}
	return out
}

// At finalize the bridge logs how many values the set refused, at debug,
// as a count with no value in the line.
func TestSeenSecrets_TheRefusalCountIsLoggedWithoutValues(t *testing.T) {
	// answerscrub's Secret pool reserve and one result's allowance in it.
	const seenSecretPoolEntries, seenSecretResultEntries = 384, 128
	var logs lockedBuffer
	b := &Bridge{cfg: Config{Logger: slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug}))}}
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-refused"}}
	act := newActivityState(false)
	run.act.Store(act)
	per := seenSecretResultEntries / 2
	for r := 0; r < seenSecretPoolEntries/seenSecretResultEntries; r++ {
		act.secrets.CollectToolResult(secretYAMLResult(t, synthVals(fmt.Sprintf("r%d", r), per)))
	}
	late := synthVals("late", 2)
	act.secrets.CollectToolResult(secretYAMLResult(t, late))
	b.clearSeenSecrets(run)
	out := logs.String()
	if !strings.Contains(out, "seen-set refused 4 values") {
		t.Errorf("log = %q, want the refusal count", out)
	}
	for _, v := range late {
		if strings.Contains(out, v) || strings.Contains(out, b64(v)) {
			t.Errorf("the log carried a refused value")
		}
	}
}

// A card's report that echoes a Secret value a worker's tool result held
// (no shape of its own, the class a shape rule cannot see) loses it in the
// answer the turn folds the card into.
func TestSeenSecrets_ACardReportEchoingAWorkersValueIsRedacted(t *testing.T) {
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-card-echo"}, state: stateRunning}
	act := newSessionActivityState("a2a-ctx-card-echo", false)
	run.act.Store(act)
	// The worker printed the Secret; the door collects it.
	act.secrets.CollectToolResult(terminalResult(t, secretJSON(t, "Opaque", map[string]string{"token": synthCardToken})))
	card := kanbanCardRow{ID: "t_echo", Title: "Check prod-a", Status: "done", Result: "Log in with " + synthCardToken + " and retry."}
	got := scrubAnswer(run, cardAnswerText(card))
	if strings.Contains(got, synthCardToken) || !strings.Contains(got, "Log in with [redacted] and retry.") {
		t.Errorf("card answer = %q, want the echoed token redacted", got)
	}
}

// postToolResult delivers a post_tool_call carrying a result, signed with
// the pod's shared key.
func postToolResult(t *testing.T, url, key, sessionID, callID string, result json.RawMessage) {
	t.Helper()
	body, _ := json.Marshal(map[string]any{
		"hook_event_name": hookPostToolCall, "tool_name": "terminal", "tool_input": map[string]any{"command": "kubectl get secret app-db -o json"},
		"session_id": sessionID, "delivery_id": nuid.Next(), "timestamp": "2026-11-09T12:00:00Z",
		"extra": map[string]any{"tool_call_id": callID, "duration_ms": 7, "status": "ok", "result": result},
	})
	req, _ := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write(body)
	req.Header.Set(hookSignatureHeader, hookSignaturePrefix+hex.EncodeToString(mac.Sum(nil)))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Errorf("delivery: %v", err)
		return
	}
	resp.Body.Close()
}

// End to end on the API executor: a tool result through the door carries
// a Secret; both turns' answers repeat its value, plain and base64. The turn
// artifact and the result carry the marker instead, no message on any
// subject and no log line carries either form, and the set is cleared once
// the task has finalized.
func TestAPI_SeenSecretValuesNeverLeaveTheBridge(t *testing.T) {
	_, url := startServer(t)
	wireConn, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer wireConn.Close()
	var wire lockedBuffer
	if _, err := wireConn.Subscribe(">", func(m *nats.Msg) {
		fmt.Fprintf(&wire, "%s %v %s\n", m.Subject, m.Header, m.Data)
	}); err != nil {
		t.Fatal(err)
	}
	var logs lockedBuffer

	const shared = "pod-wide-activity-key"
	var doorURL atomic.Value
	door := func() string { return doorURL.Load().(string) }
	release := make(chan struct{})
	result := terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": synthSecretPlain}))
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		if !strings.Contains(c.idempotency, "/") {
			select {
			case <-release:
			case <-r.Context().Done():
				return
			}
			postDelivery(t, door(), shared, c.sessionID, hookPreToolCall, "terminal", "c1")
			postToolResult(t, door(), shared, c.sessionID, "c1", result)
			writeCompletion(w, c.sessionID, "Turn one: the Secret holds "+synthSecretPlain+" (stored as "+b64(synthSecretPlain)+").")
			return
		}
		writeCompletion(w, c.sessionID, "Turn two: still "+synthSecretPlain+".")
	})
	var captured atomic.Pointer[taskRun]
	var sizeAtResult atomic.Int64
	b, _ := startAPIBridgeHooked(t, url, stub, func(cfg *Config) {
		cfg.ActivityListen = "127.0.0.1:0"
		cfg.ActivitySecret = shared
		cfg.Logger = slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	}, func(b *Bridge) {
		orig := b.resultPublish
		b.resultPublish = func(ctx context.Context, run *taskRun, output string, chat []byte) error {
			captured.Store(run)
			if a := run.act.Load(); a != nil {
				sizeAtResult.Store(int64(a.secrets.Size()))
			}
			return orig(ctx, run, output, chat)
		}
	}, func(net.Addr) bool { return true })
	doorURL.Store(b.ActivityURL())
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-seen-secret", "ctx-seen-secret", "read the db secret")
	waitFor(t, 10*time.Second, "turn 1 in flight", func() bool { return len(stub.seen()) == 1 })
	sendSteer(t, c, origin, "and again")
	waitFor(t, 10*time.Second, "queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	close(release)
	task := waitTerminal(t, c, origin.TaskID)

	text := func(name string) string {
		var sb strings.Builder
		for _, a := range task.Artifacts {
			if a.Name != name {
				continue
			}
			for _, p := range a.Parts {
				sb.WriteString(p.Text)
			}
		}
		return sb.String()
	}
	if turn := text(lib.ArtifactTurn); !strings.Contains(turn, "Turn one: the Secret holds [redacted] (stored as [redacted]).") {
		t.Errorf("turn artifact = %q, want both forms redacted", turn)
	}
	if res := text(lib.ArtifactResult); !strings.Contains(res, "Turn two: still [redacted].") {
		t.Errorf("result = %q, want the value redacted", res)
	}
	if sizeAtResult.Load() == 0 {
		t.Error("the set was empty when the result was published: the door collected nothing")
	}
	time.Sleep(200 * time.Millisecond) // let the bus deliver the last events to the wire tap
	for _, form := range []string{synthSecretPlain, b64(synthSecretPlain)} {
		if strings.Contains(wire.String(), form) {
			t.Errorf("a message on the bus carried %q", form)
		}
		if strings.Contains(logs.String(), form) {
			t.Errorf("a log line carried %q", form)
		}
	}
	waitFor(t, 5*time.Second, "the set cleared at finalize", func() bool {
		run := captured.Load()
		return run != nil && run.act.Load() != nil && run.act.Load().secrets.Size() == 0
	})
}

// A card's worker can run its tool calls while the coordinator's turn
// still goes on (a card runs as soon as it is filed), before the task waits
// on its cards, so its deliveries match no task's trace. Their Secret
// values still reach the set of the task that filed the card, whose answer
// folds the card's report in; a card no task tracks feeds nothing.
func TestActivity_CardWorkerSecretsReachTheSetBeforeTheWait(t *testing.T) {
	const secret = "pod-wide-shared-secret"
	b := &Bridge{cfg: Config{ActivitySecret: secret, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}, tasks: make(map[string]*taskRun)}
	a := newActivityState(false)
	a.sessionID = "a2a-coord-in-turn"
	a.inTurn.Store(true)
	a.SyncKanbanCard("t_card42", "Check prod-a", "")
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-in-turn"}, state: stateRunning}
	run.act.Store(a)
	b.tasks["task-in-turn"] = run

	deliver := func(cwd, value string) {
		body, _ := json.Marshal(map[string]any{
			"hook_event_name": hookPostToolCall, "tool_name": "terminal", "tool_input": map[string]any{"command": "kubectl get secret app-db -o json"},
			"session_id": "20990101_120001_worker", "cwd": cwd, "delivery_id": nuid.Next(), "timestamp": "2026-11-09T12:00:00Z",
			"extra": map[string]any{"tool_call_id": "w1", "status": "ok", "result": terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": value}))},
		})
		mac := hmac.New(sha256.New, []byte(secret))
		mac.Write(body)
		req := httptest.NewRequest(http.MethodPost, "/", bytes.NewReader(body))
		req.Header.Set(hookSignatureHeader, hookSignaturePrefix+hex.EncodeToString(mac.Sum(nil)))
		b.handleActivity(httptest.NewRecorder(), req)
	}
	deliver("/var/lib/hermes/kanban/workspaces/t_card99_other/repo", synthSecretPlain2)
	if a.secrets.Size() != 0 {
		t.Fatal("a worker of a card no task tracks fed the set")
	}
	deliver("/var/lib/hermes/kanban/workspaces/t_card42/repo", synthSecretPlain)
	if !a.secrets.Has(synthSecretPlain) {
		t.Error("the card's worker's Secret value did not reach the task's set before the wait")
	}
	if a.hasWorkerSession("20990101_120001_worker") {
		t.Error("the secrets path changed the task's card map")
	}
}

// A card the task's kanban_create made counts as the task's whatever its
// title: a create whose title scrubs or shortens to nothing still claims
// its card's worker deliveries for the set.
func TestActivity_AnUntitledFiledCardStillFeedsTheSet(t *testing.T) {
	a := newActivityState(false)
	a.sessionID = "a2a-coord-untitled"
	create := func(result string) {
		d := hookDelivery{ToolName: kanbanCreateTool, SessionID: a.sessionID}
		d.Extra.Result = json.RawMessage(result)
		a.mu.Lock()
		a.observeWorkerContextLocked(d, json.RawMessage(`{"title":"  "}`), createdKanbanCardID(kanbanCreateTool, d.Extra.Result))
		a.mu.Unlock()
	}
	create(`{"id":"t_card77","status":"todo"}`)
	// A rejected create that still reports ok names another task's card in
	// its text: an untitled create must not file it.
	create(`{"error":"title required; duplicate of t_other1"}`)
	create(`"title required: see t_other2"`)
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-untitled"}, state: stateRunning}
	run.act.Store(a)
	other := newActivityState(false)
	other.sessionID = "a2a-coord-other"
	other.waitingCard.Store(true)
	otherRun := &taskRun{origin: &lib.Envelope{TaskID: "task-other"}, state: stateRunning}
	otherRun.act.Store(other)
	runs := []*taskRun{otherRun, run}
	d := hookDelivery{SessionID: "20990101_120001_worker", Cwd: "/var/lib/hermes/kanban/workspaces/t_card77/repo", ToolName: "terminal"}
	if got := cardWorkerRun(runs, d); got != run {
		t.Error("the untitled card's worker was not claimed for the task that filed it")
	}
	missed := d
	missed.Cwd = "/var/lib/hermes/kanban/workspaces/t_card78/repo"
	if got := cardWorkerRun(runs, missed); got != nil {
		t.Error("a card no task filed was claimed")
	}
	for _, other := range []string{"t_other1", "t_other2"} {
		named := d
		named.Cwd = "/var/lib/hermes/kanban/workspaces/" + other + "/repo"
		if got := cardWorkerRun(runs, named); got != nil {
			t.Errorf("a card an error text named (%s) was claimed", other)
		}
	}
	// Waiting on its cards, the filing task, not another waiting one, takes
	// the worker's activity.
	a.waitingCard.Store(true)
	if got := matchWorkerDeliveryRun(runs, d); got != run {
		t.Error("the untitled card's worker activity was not attributed to the task that filed it")
	}
}

// A failure's status tails carry what an answer would: a failed CLI run's
// stdout tail (the answer stream) and an API error's body and error tails
// are scrubbed as an answer is before they go on TASKS.
func TestFailureTailsAreScrubbed(t *testing.T) {
	const word = "quietpasture"
	t.Run("cli stdout tail", func(t *testing.T) {
		_, url := startServer(t)
		startBridge(t, url, script(t, `echo "the password is `+word+`"
echo "export API_TOKEN=Synth3tic-Pass-0001"
echo "session_id: 20260925_181506_ab12cd" >&2
exit 1`))
		c := gatewayClient(t, url)
		submit(t, c, "task-fail-tail", "doomed")
		task := waitTerminal(t, c, "task-fail-tail")
		reason := terminalReason(t, task)
		if strings.Contains(reason, word) || strings.Contains(reason, "Synth3tic-Pass-0001") || !strings.Contains(reason, "session: 20260925_181506_ab12cd") {
			t.Errorf("terminal reason = %q, want the tail scrubbed and the session kept", reason)
		}
	})
	for _, tc := range []struct {
		name   string
		handle func(w http.ResponseWriter, r *http.Request, c apiCall)
	}{
		{"api 5xx body tail", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = io.WriteString(w, "partial answer: the password is "+word)
		}},
		{"api unreadable body tail", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			_, _ = io.WriteString(w, `{"choices":[],"note":"password: `+word+`"}`)
		}},
		{"api turn error tail", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.Header().Set("X-Hermes-Completed", "false")
			_, _ = io.WriteString(w, `{"choices":[{"message":{"content":"x"},"finish_reason":"error"}],`+
				`"hermes":{"completed":false,"partial":false,"failed":true,"error":"tool said: the password is `+word+`","error_code":"agent_error"}}`)
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, url := startServer(t)
			startAPIBridge(t, url, newAPIStub(t, tc.handle), nil)
			c := gatewayClient(t, url)
			taskID := "task-tail-" + strings.ReplaceAll(tc.name, " ", "-")
			submitIn(t, c, taskID, "ctx-tail", "doomed")
			task := waitTerminal(t, c, taskID)
			if reason := terminalReason(t, task); task.State != lib.StateFailed || strings.Contains(reason, word) || !strings.Contains(reason, "[redacted]") {
				t.Errorf("state = %s reason = %q, want failed with the tail scrubbed", task.State, reason)
			}
		})
	}
}

// The door verifies and decodes a pod-wide delivery once: routeDelivery
// hands back the delivery it decoded and the tasks it matched against, and
// the card-worker path reads those, so a signed delivery that matches no
// trace feeds the card's task without a second check or decode. A body the
// shared secret did not sign decodes to nothing.
func TestActivity_RouteDeliveryHandsBackWhatItRead(t *testing.T) {
	const secret = "pod-wide-shared-secret"
	b := &Bridge{cfg: Config{ActivitySecret: secret, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}, tasks: make(map[string]*taskRun)}
	a := newActivityState(false)
	a.sessionID = "a2a-coord-route"
	a.inTurn.Store(true)
	a.SyncKanbanCard("t_card77", "Check prod-b", "")
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-route"}, state: stateRunning}
	run.act.Store(a)
	b.tasks["task-route"] = run

	body, _ := json.Marshal(map[string]any{
		"hook_event_name": hookPostToolCall, "tool_name": "terminal", "session_id": "20990101_120002_worker",
		"cwd": "/var/lib/hermes/kanban/workspaces/t_card77/repo", "delivery_id": nuid.Next(),
		"extra": map[string]any{"tool_call_id": "w2", "status": "ok", "result": terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": synthSecretPlain}))},
	})
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	sig := hookSignaturePrefix + hex.EncodeToString(mac.Sum(nil))

	got, d, runs := b.routeDelivery(sig, body)
	if got != nil {
		t.Fatalf("a worker delivery before the wait matched a trace: %+v", got)
	}
	if d == nil || d.SessionID != "20990101_120002_worker" || d.Event != hookPostToolCall || len(runs) != 1 || runs[0] != run {
		t.Fatalf("routeDelivery handed back delivery %+v and %d runs, want the decoded delivery and the one task", d, len(runs))
	}
	b.collectCardWorkerSecrets(runs, d)
	if !a.secrets.Has(synthSecretPlain) {
		t.Error("the card worker's Secret value did not reach the task's set from the decoded delivery")
	}
	if _, d, _ := b.routeDelivery(hookSignaturePrefix+strings.Repeat("0", 64), body); d != nil {
		t.Error("a body the shared secret did not sign was decoded")
	}
}

// A failed CLI run's tails are scrubbed before they are cut to the size
// the reason quotes: a private key whose header is further from the end
// than that, and a labelled value that crosses the cut, are redacted.
func TestFailureTailsAreScrubbedBeforeTheCut(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, script(t, `echo "-----BEGIN RSA PRIVATE KEY-----"
i=0
while [ $i -lt 60 ]; do echo "SYNTHKEYBODYc3ludGhldGljLWtleS1ib2R5LWxpbmUtc3ludGhldGljLWtleS1i"; i=$((i+1)); done
echo "-----END RSA PRIVATE KEY-----"
{ i=0; while [ $i -lt 80 ]; do echo "stderr filler line $i ............................"; i=$((i+1)); done; printf 'password: Synth-'; head -c 3000 /dev/zero | tr '\0' 'q'; printf 'Z9tail\n'; } >&2
exit 1`))
	c := gatewayClient(t, url)
	submit(t, c, "task-fail-cut", "doomed")
	task := waitTerminal(t, c, "task-fail-cut")
	reason := terminalReason(t, task)
	if strings.Contains(reason, "SYNTHKEYBODY") || strings.Contains(reason, "qqqqZ9tail") {
		t.Errorf("terminal reason = %q, want the key and the value redacted", reason)
	}
	if !strings.Contains(reason, "stdout tail: ") || !strings.Contains(reason, "stderr tail: ") || !strings.Contains(reason, "[redacted]") {
		t.Errorf("terminal reason = %q, want both tails, scrubbed", reason)
	}
	if _, errTail, _ := strings.Cut(reason, "; stderr tail: "); len(errTail) > stderrTailBytes || len(reason) > stdoutTailBytes+stderrTailBytes+512 {
		t.Errorf("terminal reason is %d bytes, its stderr tail %d, want the tails still cut", len(reason), len(errTail))
	}
}

// The scrub's JSON readers read a result of any size the door accepts
// whole: answerscrub's parse cap is the door's body cap.
func TestActivityBodyCapIsTheScrubParseCap(t *testing.T) {
	if activityBodyCap != answerscrub.ToolResultParseCap {
		t.Errorf("activityBodyCap = %d, answerscrub.ToolResultParseCap = %d: want them equal", activityBodyCap, answerscrub.ToolResultParseCap)
	}
}
