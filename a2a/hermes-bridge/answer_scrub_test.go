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
	"log/slog"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nuid"
)

// Synthetic values only: none of these is, or was copied from, a real
// credential.
const (
	synthSecretPlain   = "synthetic-pass-0001"
	synthSecretPlain2  = "synthetic-token-0002"
	synthCardToken     = "quietpasture-synthetic-0003"
	synthShortSecret   = "abc1234" // seven bytes: under the floor
	synthSANamespace   = "kube-system"
	synthSecretYAMLVal = "synthetic-yaml-0004"
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

// A Secret's data values (decoded and as given), read from a terminal
// result's nested JSON, are redacted from an answer by exact match, in both
// forms; a value under eight bytes is not tracked.
func TestSeenSecrets_SecretDataRedactedDecodedAndBase64(t *testing.T) {
	var s seenSecrets
	s.collectToolResult(terminalResult(t, secretJSON(t, "Opaque", map[string]string{
		"password": synthSecretPlain, "api-token": synthSecretPlain2 + "\n", "pin": synthShortSecret,
	})))
	answer := fmt.Sprintf("The Secret holds %s and %s (stored as %s); the pin is short: %s.",
		synthSecretPlain, synthSecretPlain2, b64(synthSecretPlain), synthShortSecret)
	got := scrubAnswerText(answer, &s)
	for _, leaked := range []string{synthSecretPlain, synthSecretPlain2, b64(synthSecretPlain)} {
		if strings.Contains(got, leaked) {
			t.Errorf("answer leaked %q: %q", leaked, got)
		}
	}
	if !strings.Contains(got, synthShortSecret) {
		t.Errorf("a value under %d bytes was redacted: %q", seenSecretMinBytes, got)
	}
	if s.has(synthShortSecret) || s.has(b64(synthShortSecret)) {
		t.Errorf("a value under %d bytes is tracked", seenSecretMinBytes)
	}
}

// The same from YAML output, from stringData, and from JSON cut short.
func TestSeenSecrets_YAMLStringDataAndTruncatedJSON(t *testing.T) {
	var s seenSecrets
	yaml := "apiVersion: v1\nkind: Secret\nmetadata:\n  name: app\ndata:\n  password: " + b64(synthSecretYAMLVal) + "\nstringData:\n  token: \"" + synthSecretPlain2 + "\"\n"
	s.collectToolResult(terminalResult(t, yaml))
	cut := `{"apiVersion": "v1", "kind": "Secret", "data": {"password": "` + b64(synthSecretPlain) + `"}, "metadata": {"name": "app", "annot`
	s.collectToolResult(terminalResult(t, cut))
	for _, v := range []string{synthSecretYAMLVal, synthSecretPlain2, synthSecretPlain, b64(synthSecretPlain), b64(synthSecretPlain2)} {
		if !s.has(v) {
			t.Errorf("%q not tracked", v)
		}
	}
}

// A service account token Secret's namespace is a name, not a credential:
// it is not tracked, so the answer keeps it.
func TestSeenSecrets_AServiceAccountTokensNamespaceIsNotTracked(t *testing.T) {
	var s seenSecrets
	s.collectToolResult(terminalResult(t, secretJSON(t, "kubernetes.io/service-account-token", map[string]string{
		"namespace": synthSANamespace, "token": synthSecretPlain,
	})))
	got := scrubAnswerText("The token in "+synthSANamespace+" is "+synthSecretPlain+".", &s)
	if !strings.Contains(got, synthSANamespace) || strings.Contains(got, synthSecretPlain) {
		t.Errorf("answer = %q, want the namespace kept and the token gone", got)
	}
}

// A value a shape rule catches in a tool result is tracked, so the answer
// loses it where it stands with no shape of its own.
func TestSeenSecrets_AShapeCaughtValueIsTracked(t *testing.T) {
	var s seenSecrets
	s.collectToolResult(terminalResult(t, "export DB_PASSWORD="+synthSecretPlain+"\nok"))
	got := scrubAnswerText("Connected using "+synthSecretPlain+" as the database user's credential.", &s)
	if strings.Contains(got, synthSecretPlain) {
		t.Errorf("answer leaked the value a shape rule caught in the result: %q", got)
	}
}

// The longest value wins where two overlap, so no fragment is left over.
func TestSeenSecrets_TheLongestValueWins(t *testing.T) {
	var s seenSecrets
	s.add(synthSecretPlain)
	s.add(synthSecretPlain + "-suffix")
	if got := scrubAnswerText("x "+synthSecretPlain+"-suffix y", &s); got != "x [redacted] y" {
		t.Errorf("redact = %q, want one marker for the longer value", got)
	}
}

// The cap: past seenSecretMaxEntries entries, or seenSecretMaxBytes bytes,
// the oldest entry is evicted first; a value over seenSecretMaxValueBytes is
// not tracked.
func TestSeenSecrets_TheCapEvictsTheOldestFirst(t *testing.T) {
	var s seenSecrets
	first := fmt.Sprintf("synthetic-entry-%06d", 0)
	s.mu.Lock()
	for i := 0; i < seenSecretMaxEntries+10; i++ {
		s.addLocked(fmt.Sprintf("synthetic-entry-%06d", i))
	}
	s.mu.Unlock()
	if n := s.size(); n != seenSecretMaxEntries {
		t.Fatalf("size = %d, want the cap %d", n, seenSecretMaxEntries)
	}
	if s.has(first) || s.has(fmt.Sprintf("synthetic-entry-%06d", 9)) {
		t.Error("the oldest entries survived the cap")
	}
	if !s.has(fmt.Sprintf("synthetic-entry-%06d", 10)) || !s.has(fmt.Sprintf("synthetic-entry-%06d", seenSecretMaxEntries+9)) {
		t.Error("an entry within the cap was evicted")
	}

	var big seenSecrets
	val := func(i int) string { return fmt.Sprintf("%06d", i) + strings.Repeat("s", seenSecretMaxValueBytes-6) }
	big.mu.Lock()
	n := seenSecretMaxBytes/seenSecretMaxValueBytes + 3
	for i := 0; i < n; i++ {
		big.addLocked(val(i))
	}
	bytesHeld := big.bytes
	big.mu.Unlock()
	if bytesHeld > seenSecretMaxBytes || big.has(val(0)) || !big.has(val(n-1)) {
		t.Errorf("bytes = %d (cap %d), oldest kept = %v, newest kept = %v: want the oldest evicted under the byte cap",
			bytesHeld, seenSecretMaxBytes, big.has(val(0)), big.has(val(n-1)))
	}
	big.add(strings.Repeat("x", seenSecretMaxValueBytes+1))
	if big.has(strings.Repeat("x", seenSecretMaxValueBytes+1)) {
		t.Error("a value over the per-value cap is tracked")
	}
	big.clear()
	if big.size() != 0 {
		t.Error("clear left values behind")
	}
}

// A card's report that echoes a value a worker's tool result held (no
// shape of its own, the class a shape rule cannot see) loses it in the
// answer the turn folds the card into.
func TestSeenSecrets_ACardReportEchoingAWorkersValueIsRedacted(t *testing.T) {
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-card-echo"}, state: stateRunning}
	act := newSessionActivityState("a2a-ctx-card-echo", false)
	run.act.Store(act)
	// The worker read a config holding the token; the door collects it.
	act.secrets.collectToolResult(terminalResult(t, `{"client": {"token": "`+synthCardToken+`"}}`))
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
				sizeAtResult.Store(int64(a.secrets.size()))
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
		return run != nil && run.act.Load() != nil && run.act.Load().secrets.size() == 0
	})
}
