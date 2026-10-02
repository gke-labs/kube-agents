package hermesbridge

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/nuid"

	lib "github.com/gke-labs/kube-agents/a2a/lib"
)

const testAPIKey = "loopback-key"

// apiCall is what the stub API server saw of one request.
type apiCall struct {
	auth, sessionKey, sessionID, idempotency string
	prompt                                   string
}

// apiStub is a Hermes API server standing in for the pod's: it records each
// request and answers with handle, or with a chat completion naming the
// prompt when handle is nil.
type apiStub struct {
	mu     sync.Mutex
	calls  []apiCall
	srv    *httptest.Server
	handle func(w http.ResponseWriter, r *http.Request, c apiCall)
}

func newAPIStub(t *testing.T, handle func(w http.ResponseWriter, r *http.Request, c apiCall)) *apiStub {
	t.Helper()
	s := &apiStub{handle: handle}
	s.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req apiChatRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		c := apiCall{
			auth:        r.Header.Get("Authorization"),
			sessionKey:  r.Header.Get(apiSessionKeyHeader),
			sessionID:   r.Header.Get(apiSessionIDHeader),
			idempotency: r.Header.Get(apiIdempotencyHeader),
		}
		if len(req.Messages) > 0 {
			c.prompt = req.Messages[len(req.Messages)-1].Content
		}
		s.mu.Lock()
		s.calls = append(s.calls, c)
		s.mu.Unlock()
		if s.handle != nil {
			s.handle(w, r, c)
			return
		}
		writeCompletion(w, c.sessionID, "answer to "+c.prompt)
	}))
	t.Cleanup(s.srv.Close)
	return s
}

func (s *apiStub) seen() []apiCall {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]apiCall(nil), s.calls...)
}

func writeCompletion(w http.ResponseWriter, sessionID, text string) {
	w.Header().Set(apiSessionIDHeader, sessionID)
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": text}, "finish_reason": "stop"}},
	})
}

// startAPIBridge runs a bridge on the API executor against stub.
func startAPIBridge(t *testing.T, url string, stub *apiStub, mutate func(*Config)) *Bridge {
	t.Helper()
	cfg := Config{
		NATSURL:      url,
		Executor:     ExecutorAPI,
		APIURL:       stub.srv.URL + "/v1/chat/completions",
		APIKey:       testAPIKey,
		TaskDeadline: 20 * time.Second,
		KillGrace:    500 * time.Millisecond,
	}
	if mutate != nil {
		mutate(&cfg)
	}
	b, _ := startBridgeConfig(t, cfg, nil)
	return b
}

// submitIn is submit with the caller's contextId, so two tasks can share a
// conversation.
func submitIn(t *testing.T, c *lib.Client, taskID, contextID, prompt string) *lib.Envelope {
	t.Helper()
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, contextID, "corr-"+taskID,
		messagePayload(t, taskID, contextID, prompt), lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatalf("submission envelope: %v", err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", taskID), env); err != nil {
		t.Fatalf("submission publish: %v", err)
	}
	return env
}

// The point of the executor: two tasks in one conversation are two turns in
// one Hermes session, named by the contextId; another conversation gets
// another session. Each request carries the key and the task id as its
// replay guard, and the answer is the completion's text.
func TestAPI_AConversationIsOneSession(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, nil)
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)

	submitIn(t, c, "task-one", "ctx-shared", "pick a fruit")
	first := waitTerminal(t, c, "task-one")
	submitIn(t, c, "task-two", "ctx-shared", "which fruit did you pick")
	second := waitTerminal(t, c, "task-two")
	submitIn(t, c, "task-other", "ctx-elsewhere", "hello")
	waitTerminal(t, c, "task-other")

	for _, task := range []*lib.Task{first, second} {
		if task.State != lib.StateCompleted {
			t.Fatalf("%s state = %s, want completed", task.ID, task.State)
		}
	}
	if got := second.Artifact(lib.ArtifactResult).Parts[0].Text; got != "answer to which fruit did you pick" {
		t.Fatalf("result = %q, want the completion's text", got)
	}
	calls := stub.seen()
	if len(calls) != 3 {
		t.Fatalf("requests = %d, want 3", len(calls))
	}
	for i, want := range []struct{ session, task string }{
		{"a2a-ctx-shared", "task-one"}, {"a2a-ctx-shared", "task-two"}, {"a2a-ctx-elsewhere", "task-other"},
	} {
		got := calls[i]
		if got.sessionID != want.session || got.sessionKey != want.session {
			t.Errorf("request %d session headers = (%q, %q), want %q for both", i, got.sessionID, got.sessionKey, want.session)
		}
		if got.idempotency != want.task {
			t.Errorf("request %d Idempotency-Key = %q, want the task id %q", i, got.idempotency, want.task)
		}
		if got.auth != "Bearer "+testAPIKey {
			t.Errorf("request %d Authorization = %q", i, got.auth)
		}
	}
}

// Two tasks in one conversation in flight at once take turns: the second
// request reaches the server only after the first has answered, while a
// task in another conversation is not held behind them.
func TestAPI_TurnsInOneSessionAreSerialized(t *testing.T) {
	_, url := startServer(t)
	release := make(chan struct{})
	var inShared, maxShared atomic.Int32
	otherDone := make(chan struct{})
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		if c.sessionID == "a2a-ctx-other" {
			close(otherDone)
			writeCompletion(w, c.sessionID, "other")
			return
		}
		n := inShared.Add(1)
		for {
			m := maxShared.Load()
			if n <= m || maxShared.CompareAndSwap(m, n) {
				break
			}
		}
		select {
		case <-release:
		case <-r.Context().Done():
		}
		inShared.Add(-1)
		writeCompletion(w, c.sessionID, "shared")
	})
	var releaseOnce sync.Once
	open := func() { releaseOnce.Do(func() { close(release) }) }
	// A failed assertion must not leave handlers parked: the stub's Close
	// waits for them.
	t.Cleanup(open)
	startAPIBridge(t, url, stub, func(cfg *Config) { cfg.Concurrency = 3 })
	c := gatewayClient(t, url)

	submitIn(t, c, "task-a", "ctx-turns", "first")
	submitIn(t, c, "task-b", "ctx-turns", "second")
	waitFor(t, 10*time.Second, "the first turn's request", func() bool { return inShared.Load() >= 1 })
	submitIn(t, c, "task-c", "ctx-other", "elsewhere")
	select {
	case <-otherDone:
	case <-time.After(10 * time.Second):
		t.Fatal("a task in another conversation waited behind the held session")
	}
	if n := len(stub.seen()); n != 2 {
		t.Fatalf("requests while the first turn is held = %d, want 2 (the held turn and the other conversation)", n)
	}
	open()
	for _, id := range []string{"task-a", "task-b", "task-c"} {
		if task := waitTerminal(t, c, id); task.State != lib.StateCompleted {
			t.Fatalf("%s state = %s, want completed", id, task.State)
		}
	}
	if maxShared.Load() != 1 {
		t.Fatalf("turns in flight at once in one session = %d, want 1", maxShared.Load())
	}
}

// A cancel ends the request: the server sees its client go away, and the
// task's terminal is canceled.
func TestAPI_CancelEndsTheRequest(t *testing.T) {
	_, url := startServer(t)
	started := make(chan struct{})
	gone := make(chan struct{})
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		close(started)
		<-r.Context().Done()
		close(gone)
	})
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)

	origin := submitIn(t, c, "task-cancel-api", "ctx-cancel", "run forever")
	select {
	case <-started:
	case <-time.After(10 * time.Second):
		t.Fatal("the request never reached the server")
	}
	publishCancel(t, c, origin)
	if task := waitTerminal(t, c, origin.TaskID); task.State != lib.StateCanceled {
		t.Fatalf("state = %s, want canceled", task.State)
	}
	select {
	case <-gone:
	case <-time.After(5 * time.Second):
		t.Fatal("the request outlived the cancel")
	}
}

// Each way the server can fail names itself in the terminal, with the
// session, so a reader can find the turn in the profile's store.
func TestAPI_FailuresAreNamed(t *testing.T) {
	cases := []struct {
		name   string
		handle func(w http.ResponseWriter, r *http.Request, c apiCall)
		want   string
	}{
		{"rate limited", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = io.WriteString(w, `{"error":{"message":"quota"}}`)
		}, "reason: hermes-rate-limited - HTTP 429; session: a2a-ctx-fail; body tail: "},
		{"server error", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = io.WriteString(w, "boom")
		}, "reason: hermes-api-failed - HTTP 500; session: a2a-ctx-fail; body tail: boom"},
		{"no choices", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			_, _ = io.WriteString(w, `{"choices":[]}`)
		}, "reason: hermes-api-unreadable - HTTP 200; session: a2a-ctx-fail"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, url := startServer(t)
			startAPIBridge(t, url, newAPIStub(t, tc.handle), nil)
			c := gatewayClient(t, url)
			taskID := "task-fail-" + strings.ReplaceAll(tc.name, " ", "-")
			submitIn(t, c, taskID, "ctx-fail", "doomed")
			task := waitTerminal(t, c, taskID)
			if task.State != lib.StateFailed {
				t.Fatalf("state = %s, want failed", task.State)
			}
			if reason := terminalReason(t, task); !strings.HasPrefix(reason, tc.want) {
				t.Fatalf("reason = %q, want prefix %q", reason, tc.want)
			}
		})
	}
}

// A server that is not there is named as such, not as a hermes failure,
// once the connect retry gives up on it.
func TestAPI_UnreachableServerIsNamed(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, nil)
	stub.srv.Close()
	startAPIBridge(t, url, stub, func(cfg *Config) { cfg.APIConnectRetry = 1500 * time.Millisecond })
	c := gatewayClient(t, url)
	submitIn(t, c, "task-nobody", "ctx-nobody", "anyone there")
	task := waitTerminal(t, c, "task-nobody")
	if reason := terminalReason(t, task); task.State != lib.StateFailed || !strings.HasPrefix(reason, "reason: hermes-api-unreachable - ") {
		t.Fatalf("state = %s reason = %q, want failed hermes-api-unreachable", task.State, reason)
	}
}

func TestAPI_NewRefusesAMissingKeyAndAnUnknownExecutor(t *testing.T) {
	_, url := startServer(t)
	for _, cfg := range []Config{
		{NATSURL: url, Executor: ExecutorAPI},
		{NATSURL: url, Executor: "carrier-pigeon"},
	} {
		cfg.ScratchDir = t.TempDir()
		if b, err := New(testCtx(t), cfg); err == nil {
			b.close()
			t.Fatalf("New(%+v) accepted it", cfg.Executor)
		}
	}
}

func TestAPISessionID(t *testing.T) {
	long := strings.Repeat("a", apiContextIDMaxLen+1)
	sum := sha256.Sum256([]byte(long))
	hashedLong := "a2a-h-" + hex.EncodeToString(sum[:])[:apiHashedSessionHexLen]
	for _, tc := range []struct{ ctx, task, want string }{
		{"ctx-0123abcd", "t", "a2a-ctx-0123abcd"},
		{"", "task-9", "a2a-task-task-9"},
		{long, "t", hashedLong},
	} {
		if got := apiSessionID(tc.ctx, tc.task); got != tc.want {
			t.Errorf("apiSessionID(%q) = %q, want %q", tc.ctx, got, tc.want)
		}
	}
	// Anything that could be a path, or is not the gateway's shape, is
	// hashed: stable, prefixed, and free of separators.
	for _, ctx := range []string{"../etc", "a/b", `a\b`, "..", "ctx.1", "ctx 1", "ctx-é"} {
		got := apiSessionID(ctx, "t")
		if !strings.HasPrefix(got, "a2a-h-") || strings.ContainsAny(got, `/\. `) || got != apiSessionID(ctx, "other") {
			t.Errorf("apiSessionID(%q) = %q, want a stable hashed id", ctx, got)
		}
	}
}

// postDelivery signs and POSTs one hook delivery the way hermes does.
func postDelivery(t *testing.T, url, key, sessionID, event, tool, callID string) {
	t.Helper()
	body, _ := json.Marshal(map[string]any{
		"hook_event_name": event, "tool_name": tool, "tool_input": map[string]any{"q": "x"},
		"session_id": sessionID, "delivery_id": nuid.Next(), "timestamp": "2026-10-02T12:00:00Z",
		"extra": map[string]any{"tool_call_id": callID, "duration_ms": 7, "status": "ok"},
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

// The door attributes an API task's tool calls by the session the pod-wide
// hook's payload names, signed with the shared key. A call in another
// session, or signed with another key, is not the task's.
func TestAPI_DoorAttributesBySession(t *testing.T) {
	_, url := startServer(t)
	const shared = "pod-wide-activity-key"
	var door atomic.Value
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		u := door.Load().(string)
		for _, d := range []struct{ key, session, tool, id string }{
			{shared, c.sessionID, "session_search", "call_mine"},
			{shared, "a2a-ctx-someone-else", "kubectl", "call_theirs"},
			{shared, "20261002_120000_cli", "kanban_create", "call_kanban"},
			{"not-the-key", c.sessionID, "forged", "call_forged"},
		} {
			postDelivery(t, u, d.key, d.session, hookPreToolCall, d.tool, d.id)
			postDelivery(t, u, d.key, d.session, hookPostToolCall, d.tool, d.id)
		}
		writeCompletion(w, c.sessionID, "done")
	})
	b := startAPIBridge(t, url, stub, func(cfg *Config) {
		cfg.ActivityListen = "127.0.0.1:0"
		cfg.ActivitySecret = shared
	})
	door.Store(b.ActivityURL())
	c := gatewayClient(t, url)

	submitIn(t, c, "task-door", "ctx-door", "search your memory")
	task := waitTerminal(t, c, "task-door")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	entries := activityEntries(t, task)
	if len(entries) != 1 || entries[0].Tool != "session_search" || entries[0].CallID != "call_mine" {
		t.Fatalf("activity = %+v, want the one call in the task's session", entries)
	}
}

// Without the shared key the door claims nothing for an API task, signed or
// not: an empty key verifies no signature.
func TestAPI_DoorWithoutTheSharedKeyClaimsNothing(t *testing.T) {
	_, url := startServer(t)
	var door atomic.Value
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		u := door.Load().(string)
		postDelivery(t, u, "", c.sessionID, hookPreToolCall, "kubectl", "call_1")
		postDelivery(t, u, "", c.sessionID, hookPostToolCall, "kubectl", "call_1")
		writeCompletion(w, c.sessionID, "done")
	})
	b := startAPIBridge(t, url, stub, func(cfg *Config) { cfg.ActivityListen = "127.0.0.1:0" })
	door.Store(b.ActivityURL())
	c := gatewayClient(t, url)
	submitIn(t, c, "task-nokey", "ctx-nokey", "go")
	task := waitTerminal(t, c, "task-nokey")
	if entries := activityEntries(t, task); len(entries) != 0 {
		t.Fatalf("activity = %+v, want none", entries)
	}
}

// A task waiting for its turn shares the session id with the task holding
// it, so the session alone cannot tell them apart: only the turn holder's
// tool calls are in flight, and every delivery is its. Ten deliveries make a
// lucky map order that hides a wrong match a one-in-a-thousand pass.
func TestAPI_DoorAttributesToTheTurnHolderNotTheWaiter(t *testing.T) {
	_, url := startServer(t)
	const shared = "pod-wide-activity-key"
	const deliveries = 10
	var door atomic.Value
	fire := make(chan struct{})
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		if c.idempotency == "task-holder" {
			select {
			case <-fire:
			case <-r.Context().Done():
				return
			}
			u := door.Load().(string)
			for i := 0; i < deliveries; i++ {
				id := fmt.Sprintf("call_%d", i)
				postDelivery(t, u, shared, c.sessionID, hookPreToolCall, "kubectl", id)
				postDelivery(t, u, shared, c.sessionID, hookPostToolCall, "kubectl", id)
			}
		}
		writeCompletion(w, c.sessionID, "done")
	})
	var fireOnce sync.Once
	t.Cleanup(func() { fireOnce.Do(func() { close(fire) }) })
	b := startAPIBridge(t, url, stub, func(cfg *Config) {
		cfg.Concurrency = 2
		cfg.ActivityListen = "127.0.0.1:0"
		cfg.ActivitySecret = shared
	})
	door.Store(b.ActivityURL())
	c := gatewayClient(t, url)

	submitIn(t, c, "task-holder", "ctx-queue", "first")
	waitFor(t, 10*time.Second, "the holder's request", func() bool { return len(stub.seen()) == 1 })
	submitIn(t, c, "task-waiter", "ctx-queue", "second")
	waitFor(t, 10*time.Second, "the waiter queued on the session", func() bool {
		b.mu.Lock()
		defer b.mu.Unlock()
		r, ok := b.tasks["task-waiter"]
		return ok && r.act.Load() != nil
	})
	fireOnce.Do(func() { close(fire) })

	holder := waitTerminal(t, c, "task-holder")
	waiter := waitTerminal(t, c, "task-waiter")
	if got := len(activityEntries(t, holder)); got != deliveries {
		t.Errorf("holder activity = %d entries, want %d", got, deliveries)
	}
	if got := activityEntries(t, waiter); len(got) != 0 {
		t.Errorf("waiter activity = %+v, want none: it never held the turn", got)
	}
}

// The operator's pod-wide entry posts to the same door with the shared key.
// A CLI child keeps the operator's other hooks but not that one, or every
// tool call would arrive twice: once under the task's key, once under the
// shared one.
func TestChildManagedScope_DropsThePodWideDoorEntry(t *testing.T) {
	src := t.TempDir()
	cfg := "hooks:\n  outbound:\n    - name: theirs\n      url: https://audit.example/\n    - name: " + hookEntryName + "\n      url: http://pod-wide.example/hermes/tool-events\n      secret_env: " + ActivitySecretEnv + "\n"
	if err := os.WriteFile(filepath.Join(src, managedConfigFile), []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}
	b := &Bridge{cfg: Config{ScratchDir: t.TempDir(), ManagedScopeDir: src}}
	dir, err := b.childManagedScope("task-cli")
	if err != nil {
		t.Fatal(err)
	}
	out, err := os.ReadFile(filepath.Join(dir, managedConfigFile))
	if err != nil {
		t.Fatal(err)
	}
	if n := strings.Count(string(out), "name: "+hookEntryName); n != 1 {
		t.Fatalf("door entries in the child's config = %d, want 1 (its own): %s", n, out)
	}
	if strings.Contains(string(out), "pod-wide.example") || !strings.Contains(string(out), "name: theirs") {
		t.Fatalf("child config kept the pod-wide entry or lost the operator's: %s", out)
	}
}

// The sidecar can be consuming before hermes's API server listens: a refused
// connection is retried, and a server that comes up inside the window
// answers the task as if it had been there all along.
func TestAPI_AServerThatStartsLateStillAnswers(t *testing.T) {
	_, url := startServer(t)
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	l.Close()
	stub := &apiStub{}
	stub.srv = httptest.NewUnstartedServer(newAPIStub(t, nil).srv.Config.Handler)
	cfg := Config{
		NATSURL: url, Executor: ExecutorAPI, APIURL: "http://" + addr + "/v1/chat/completions", APIKey: testAPIKey,
		TaskDeadline: 20 * time.Second, KillGrace: 500 * time.Millisecond,
	}
	startBridgeConfig(t, cfg, nil)
	c := gatewayClient(t, url)
	submitIn(t, c, "task-early", "ctx-early", "are you up")
	time.Sleep(1500 * time.Millisecond)
	late, err := net.Listen("tcp", addr)
	if err != nil {
		t.Skipf("the port was taken in between: %v", err)
	}
	stub.srv.Listener = late
	stub.srv.Start()
	t.Cleanup(stub.srv.Close)
	task := waitTerminal(t, c, "task-early")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s (%s), want completed once the server came up", task.State, terminalReason(t, task))
	}
}
