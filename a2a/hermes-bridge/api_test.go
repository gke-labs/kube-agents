package hermesbridge

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/gke-labs/kube-agents/a2a/capability"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
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
	s.srv = httptest.NewServer(http.HandlerFunc(s.serve))
	t.Cleanup(s.srv.Close)
	return s
}

// serve records the call and answers it with handle, or with an echo.
func (s *apiStub) serve(w http.ResponseWriter, r *http.Request) {
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

// startAPIBridge runs a bridge on the API executor against stub, with the
// test's door, on a port the kernel picked, standing in for the address the
// pod-wide hook posts to.
func startAPIBridge(t *testing.T, url string, stub *apiStub, mutate func(*Config)) *Bridge {
	t.Helper()
	return startAPIBridgeReaching(t, url, stub, mutate, func(net.Addr) bool { return true })
}

// startAPIBridgeReaching is startAPIBridge with the hook-reach check given.
// It is set before the bridge starts and restored at cleanup, because the
// bridge's goroutines read it.
func startAPIBridgeReaching(t *testing.T, url string, stub *apiStub, mutate func(*Config), reaches func(net.Addr) bool) *Bridge {
	t.Helper()
	cfg := Config{
		NATSURL:      url,
		Executor:     ExecutorAPI,
		APIURL:       stub.srv.URL + "/v1/chat/completions",
		APIKey:       testAPIKey,
		TaskDeadline: 20 * time.Second,
		KillGrace:    500 * time.Millisecond,
		// Armed, as startBridgeCap's are: the scope mintFor writes.
		Scope: capability.NamespaceScope(""),
	}
	if mutate != nil {
		mutate(&cfg)
	}
	prev := activityHookReaches
	activityHookReaches = reaches
	t.Cleanup(func() { activityHookReaches = prev })
	b, _ := startBridgeConfig(t, cfg, nil)
	return b
}

// submitIn is submit with the caller's contextId, so two tasks can share a
// conversation.
func submitIn(t *testing.T, c *lib.Client, taskID, contextID, prompt string) *lib.Envelope {
	t.Helper()
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, contextID, "corr-"+taskID,
		messagePayload(t, taskID, contextID, prompt), lib.WithTo(lib.Party{Session: "platform"}),
		lib.WithAuthority(authorityFor(t, mintFor(t, c, taskID, "platform"))))
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

// A task whose deadline passes while it waits behind its session's previous
// turn says so: no request was sent, which is not the persona running long.
func TestAPI_DeadlineWhileWaitingIsNamed(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		writeCompletion(w, c.sessionID, "unexpected")
	})
	b := startAPIBridge(t, url, stub, func(cfg *Config) { cfg.TaskDeadline = 2 * time.Second })
	// The session's turn is held for longer than the task's deadline, as a
	// sibling's long turn would hold it.
	release := b.turns.acquire(context.Background(), "a2a-ctx-wait")
	if release == nil {
		t.Fatal("could not take the session's turn")
	}
	t.Cleanup(release)
	c := gatewayClient(t, url)
	submitIn(t, c, "task-waiter", "ctx-wait", "second")
	task := waitTerminal(t, c, "task-waiter")
	if task.State != lib.StateFailed {
		t.Fatalf("state = %s, want failed", task.State)
	}
	want := "reason: session-busy - waited 2s for the session's previous turn; no request was sent"
	if reason := terminalReason(t, task); reason != want {
		t.Fatalf("reason = %q, want %q", reason, want)
	}
	if n := len(stub.seen()); n != 0 {
		t.Fatalf("requests = %d, want 0: the waiter never reached the server", n)
	}
	if trail := eventTrail(t, replayEvents(t, url, "task-waiter")); slices.Contains(trail, string(lib.StateWorking)) {
		t.Fatalf("events = %v, want no working: the waiter never started a turn", trail)
	}
}

// A cancel that reaches a task still waiting for its session's turn ends it
// as one cancelled out of the queue: no request was sent, nothing ran.
func TestAPI_CancelWhileWaitingIsBeforeStart(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, nil)
	b := startAPIBridge(t, url, stub, nil)
	release := b.turns.acquire(context.Background(), "a2a-ctx-cwait")
	if release == nil {
		t.Fatal("could not take the session's turn")
	}
	t.Cleanup(release)
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-cwait", "ctx-cwait", "second")
	waitFor(t, 10*time.Second, "the task queued on the session", func() bool {
		b.turns.mu.Lock()
		defer b.turns.mu.Unlock()
		slot, ok := b.turns.slots["a2a-ctx-cwait"]
		return ok && slot.refs == 2
	})
	publishCancel(t, c, origin)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCanceled {
		t.Fatalf("state = %s, want canceled", task.State)
	}
	if reason := terminalReason(t, task); reason != canceledBeforeStartReason {
		t.Fatalf("reason = %q, want %q", reason, canceledBeforeStartReason)
	}
	if n := len(stub.seen()); n != 0 {
		t.Fatalf("requests = %d, want 0", n)
	}
	if trail := eventTrail(t, replayEvents(t, url, origin.TaskID)); slices.Contains(trail, string(lib.StateWorking)) {
		t.Fatalf("events = %v, want no working", trail)
	}
}

// A turn the server marks incomplete but not failed (truncated, partial)
// completes with its text, as `hermes chat -Q` exits zero on it.
func TestAPI_PartialTurnCompletes(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
		w.Header().Set("X-Hermes-Completed", "false")
		_, _ = io.WriteString(w, `{"choices":[{"message":{"content":"half an answer"},"finish_reason":"length"}],`+
			`"hermes":{"completed":false,"partial":true,"failed":false,"error":"output truncated","error_code":"output_truncated"}}`)
	})
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)
	submitIn(t, c, "task-partial", "ctx-partial", "go")
	task := waitTerminal(t, c, "task-partial")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
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

// A cancel that lands after the server sent its headers, while the body is
// still coming, ends the request too: the body read is the request's.
func TestAPI_CancelAfterTheHeadersEndsTheRequest(t *testing.T) {
	_, url := startServer(t)
	headersSent := make(chan struct{})
	gone := make(chan struct{})
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		close(headersSent)
		<-r.Context().Done()
		close(gone)
	})
	// A deadline far past the wait below, so only the cancel can end it.
	startAPIBridge(t, url, stub, func(cfg *Config) { cfg.TaskDeadline = 5 * time.Minute })
	c := gatewayClient(t, url)

	origin := submitIn(t, c, "task-cancel-body", "ctx-cancel-body", "answer slowly")
	select {
	case <-headersSent:
	case <-time.After(10 * time.Second):
		t.Fatal("the server never sent its headers")
	}
	// Past the header read on the bridge's side, so the cancel lands in the
	// body read.
	time.Sleep(200 * time.Millisecond)
	publishCancel(t, c, origin)
	select {
	case <-gone:
	case <-time.After(5 * time.Second):
		t.Fatal("the request outlived the cancel")
	}
	if task := waitTerminal(t, c, origin.TaskID); task.State != lib.StateCanceled {
		t.Fatalf("state = %s (%s), want canceled", task.State, terminalReason(t, task))
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
		{"run cap", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = io.WriteString(w, `{"error":{"message":"Too many concurrent runs (max 10)"}}`)
		}, "reason: hermes-rate-limited - HTTP 429; session: a2a-ctx-fail; body tail: "},
		{"turn failed with text", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.Header().Set("X-Hermes-Completed", "false")
			_, _ = io.WriteString(w, `{"choices":[{"message":{"content":"The provider rate-limited me."},"finish_reason":"error"}],`+
				`"hermes":{"completed":false,"partial":false,"failed":true,"error":"HTTP 429 from provider","error_code":"agent_error"}}`)
		}, "reason: hermes-api-failed - HTTP 200, turn failed; session: a2a-ctx-fail; error: HTTP 429 from provider"},
		{"turn gave up on the provider's rate limit", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.Header().Set("X-Hermes-Failure-Reason", "rate_limit")
			_, _ = io.WriteString(w, `{"choices":[{"message":{"content":"Rate limited."},"finish_reason":"error"}],`+
				`"hermes":{"completed":false,"partial":false,"failed":true,"error":"HTTP 429","error_code":"agent_error"}}`)
		}, "reason: hermes-rate-limited - HTTP 200, turn failed; session: a2a-ctx-fail; error: HTTP 429"},
		{"billing failure with no text", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.Header().Set("X-Hermes-Failure-Reason", "billing")
			w.WriteHeader(http.StatusBadGateway)
			_, _ = io.WriteString(w, `{"error":{"message":"credits exhausted","code":"agent_incomplete"}}`)
		}, "reason: hermes-rate-limited - HTTP 502; session: a2a-ctx-fail; body tail: "},
		{"other reason with no text", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.Header().Set("X-Hermes-Failure-Reason", "tool_error")
			w.WriteHeader(http.StatusBadGateway)
			_, _ = io.WriteString(w, `{"error":{"message":"boom","code":"agent_incomplete"}}`)
		}, "reason: hermes-api-failed - HTTP 502; session: a2a-ctx-fail; body tail: "},
		{"server error", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = io.WriteString(w, "boom")
		}, "reason: hermes-api-failed - HTTP 500; session: a2a-ctx-fail; body tail: boom"},
		{"key refused", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = io.WriteString(w, `{"error":{"message":"Invalid API key"}}`)
		}, "reason: hermes-api-refused - HTTP 401; session: a2a-ctx-fail; body tail: "},
		{"profile refused", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.WriteHeader(http.StatusNotFound)
			_, _ = io.WriteString(w, `{"error":"Unknown or unconfigured profile"}`)
		}, "reason: hermes-api-refused - HTTP 404; session: a2a-ctx-fail; body tail: "},
		{"no choices", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			_, _ = io.WriteString(w, `{"choices":[]}`)
		}, "reason: hermes-api-unreadable - HTTP 200; session: a2a-ctx-fail"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, url := startServer(t)
			startAPIBridge(t, url, newAPIStub(t, tc.handle), nil)
			c := gatewayClient(t, url)
			taskID := "task-fail-" + strings.NewReplacer(" ", "-", "'", "").Replace(tc.name)
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

// A deadline that ends inside the connect retry ended a wait for a server
// that never listened: infrastructure, as past the retry window, and not a
// turn that ran out of time.
func TestAPI_DeadlineWhileTheServerIsAbsentIsUnreachable(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, nil)
	stub.srv.Close()
	startAPIBridge(t, url, stub, func(cfg *Config) {
		cfg.APIConnectRetry = time.Minute
		cfg.TaskDeadline = 2 * time.Second
	})
	c := gatewayClient(t, url)
	submitIn(t, c, "task-absent", "ctx-absent", "anyone there")
	task := waitTerminal(t, c, "task-absent")
	want := "reason: hermes-api-unreachable - task deadline 2s ended before the server accepted a connection; no request was sent"
	if reason := terminalReason(t, task); task.State != lib.StateFailed || !strings.HasPrefix(reason, want) {
		t.Fatalf("state = %s reason = %q, want failed with prefix %q", task.State, reason, want)
	}
}

func TestAPI_NewRefusesAURLItCannotSendTo(t *testing.T) {
	_, url := startServer(t)
	for _, apiURL := range []string{
		"localhost:8642/v1/chat/completions",
		"foo",
		"ftp://127.0.0.1/v1/chat/completions",
		"http:///v1/chat/completions",
	} {
		cfg := Config{NATSURL: url, Executor: ExecutorAPI, APIURL: apiURL, APIKey: testAPIKey, ScratchDir: t.TempDir()}
		if b, err := New(testCtx(t), cfg); err == nil {
			b.close()
			t.Errorf("New accepted APIURL %q", apiURL)
		}
	}
	for _, apiURL := range []string{"http://127.0.0.1:8642/v1/chat/completions", "https://hermes.example/v1/chat/completions"} {
		if err := apiExecutorValid(&Config{APIURL: apiURL, APIKey: testAPIKey}); err != nil {
			t.Errorf("apiExecutorValid(%q) = %v, want nil", apiURL, err)
		}
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
	for _, tc := range []struct{ ctx, want string }{
		{"ctx-0123abcd", "a2a-ctx-0123abcd"},
		{long, hashedLong},
	} {
		if got := apiSessionID(tc.ctx); got != tc.want {
			t.Errorf("apiSessionID(%q) = %q, want %q", tc.ctx, got, tc.want)
		}
	}
	// Anything that could be a path, or is not the gateway's shape, is
	// hashed: stable, prefixed, and free of separators.
	for _, ctx := range []string{"../etc", "a/b", `a\b`, "..", "ctx.1", "ctx 1", "ctx-é"} {
		got := apiSessionID(ctx)
		if !strings.HasPrefix(got, "a2a-h-") || strings.ContainsAny(got, `/\. `) {
			t.Errorf("apiSessionID(%q) = %q, want a hashed id", ctx, got)
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

// An API task's heartbeat counts calls when a delivery can reach it (the
// door open, with the shared key to check one against) and says the trace
// is off when none can.
func TestAPI_HeartbeatSaysWhetherTheTraceIsOn(t *testing.T) {
	for _, tc := range []struct {
		name, secret, want string
	}{
		{"door open with the key", "pod-wide-activity-key", "0 tool call(s)"},
		{"door open without the key", "", "tool trace off"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, url := startServer(t)
			var b *Bridge
			ready := make(chan struct{})
			lines := make(chan string, 1)
			stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
				<-ready
				b.mu.Lock()
				run := b.tasks[c.idempotency]
				b.mu.Unlock()
				lines <- run.act.Load().progressLine(time.Now())
				writeCompletion(w, c.sessionID, "done")
			})
			b = startAPIBridge(t, url, stub, func(cfg *Config) {
				cfg.ActivityListen = "127.0.0.1:0"
				cfg.ActivitySecret = tc.secret
			})
			close(ready)
			c := gatewayClient(t, url)
			submitIn(t, c, "task-beat", "ctx-beat", "go")
			waitTerminal(t, c, "task-beat")
			if line := <-lines; !strings.Contains(line, tc.want) {
				t.Fatalf("heartbeat = %q, want it to say %q", line, tc.want)
			}
		})
	}
}

// A door open with the key but not where the pod-wide hook posts hears no
// call, so its heartbeat says the trace is off rather than counting zero.
func TestAPI_DoorElsewhereIsUntraced(t *testing.T) {
	_, url := startServer(t)
	var b *Bridge
	ready := make(chan struct{})
	lines := make(chan string, 1)
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		<-ready
		b.mu.Lock()
		run := b.tasks[c.idempotency]
		b.mu.Unlock()
		lines <- run.act.Load().progressLine(time.Now())
		writeCompletion(w, c.sessionID, "done")
	})
	b = startAPIBridgeReaching(t, url, stub, func(cfg *Config) {
		cfg.ActivityListen = "127.0.0.1:0"
		cfg.ActivitySecret = "pod-wide-activity-key"
	}, doorReceivesHook)
	close(ready)
	c := gatewayClient(t, url)
	submitIn(t, c, "task-elsewhere", "ctx-elsewhere", "go")
	waitTerminal(t, c, "task-elsewhere")
	if line := <-lines; !strings.Contains(line, "tool trace off") {
		t.Fatalf("heartbeat = %q, want it to say the trace is off", line)
	}
}

// The pod-wide hook posts to DefaultActivityListen alone: a door hears it on
// that port, bound to that host or to a wildcard.
func TestDoorReceivesHook(t *testing.T) {
	cases := []struct {
		addr net.Addr
		want bool
	}{
		{&net.TCPAddr{IP: net.ParseIP("127.0.0.1"), Port: 8651}, true},
		{&net.TCPAddr{IP: net.IPv4zero, Port: 8651}, true},
		{&net.TCPAddr{IP: net.IPv6unspecified, Port: 8651}, true},
		{&net.TCPAddr{IP: net.ParseIP("127.0.0.1"), Port: 9999}, false},
		{&net.TCPAddr{IP: net.ParseIP("127.0.0.2"), Port: 8651}, false},
		{&net.TCPAddr{IP: net.ParseIP("10.0.0.1"), Port: 8651}, false},
		{&net.UnixAddr{Name: "/tmp/door", Net: "unix"}, false},
	}
	for _, tc := range cases {
		if got := doorReceivesHook(tc.addr); got != tc.want {
			t.Errorf("doorReceivesHook(%s) = %v, want %v", tc.addr, got, tc.want)
		}
	}
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
		b.turns.mu.Lock()
		defer b.turns.mu.Unlock()
		slot, ok := b.turns.slots["a2a-ctx-queue"]
		return ok && slot.refs == 2
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
	stub.srv = httptest.NewUnstartedServer(http.HandlerFunc(stub.serve))
	cfg := Config{
		NATSURL: url, Executor: ExecutorAPI, APIURL: "http://" + addr + "/v1/chat/completions", APIKey: testAPIKey,
		TaskDeadline: 20 * time.Second, KillGrace: 500 * time.Millisecond,
		Scope: capability.NamespaceScope(""),
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
	if n := len(stub.seen()); n != 1 {
		t.Fatalf("server saw %d request(s), want 1: a refused connection sent nothing", n)
	}
}

// keyedStub stands in for a Hermes server's idempotency cache, keyed on the
// Idempotency-Key alone: a repeated key replays its first answer. The real
// cache also matches the request body's fingerprint, so this replays more
// readily than Hermes, which is the strict side for a test that a follow-up
// never reuses turn 1's key. The first request blocks until release closes.
func keyedStub(t *testing.T, release <-chan struct{}) *apiStub {
	var mu sync.Mutex
	cache := map[string]string{}
	first := true
	return newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		mu.Lock()
		if text, ok := cache[c.idempotency]; ok {
			mu.Unlock()
			writeCompletion(w, c.sessionID, text)
			return
		}
		block := first
		first = false
		mu.Unlock()
		if block {
			<-release
		}
		text := "answer to " + c.prompt
		mu.Lock()
		cache[c.idempotency] = text
		mu.Unlock()
		writeCompletion(w, c.sessionID, text)
	})
}

// A follow-up sent during the opening turn runs as a second turn in the same
// session, under its own Idempotency-Key "<taskId>/<envelopeId>": the stub
// replays on a repeated key, so a reused task-id key would hand back turn 1.
func TestAPI_FollowUpIsASecondTurnInTheSameSession(t *testing.T) {
	_, url := startServer(t)
	release := make(chan struct{})
	stub := keyedStub(t, release)
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-api-turns", "ctx-turns", "long question")
	waitFor(t, 10*time.Second, "turn 1 in flight", func() bool { return len(stub.seen()) == 1 })
	steer := sendSteer(t, c, origin, "also east")
	waitFor(t, 10*time.Second, "queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	if n := steerNotices(t, url, origin.TaskID)[0]; n.Steer != lib.SteerQueued || n.state != lib.StateWorking {
		t.Fatalf("notice %+v, want queued on a working status", n)
	}
	close(release)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted || task.PostFinalDropped != 0 {
		t.Fatalf("state %s post-final %d", task.State, task.PostFinalDropped)
	}
	calls := stub.seen()
	if len(calls) != 2 {
		t.Fatalf("%d requests, want 2", len(calls))
	}
	for _, call := range calls {
		if call.sessionID != "a2a-ctx-turns" || call.sessionKey != "a2a-ctx-turns" {
			t.Fatalf("a turn left the session: %+v", call)
		}
	}
	if calls[0].idempotency != origin.TaskID || calls[1].idempotency != origin.TaskID+"/"+steer.EnvelopeID {
		t.Fatalf("keys %q, %q", calls[0].idempotency, calls[1].idempotency)
	}
	if calls[1].prompt != "also east" {
		t.Fatalf("turn 2 prompt %q", calls[1].prompt)
	}
	if got := artifactsNamed(t, url, origin.TaskID, lib.ArtifactResult); !slices.Equal(got, []string{"answer to also east"}) {
		t.Fatalf("result %q: a reused key would have replayed turn 1", got)
	}
	if got := artifactsNamed(t, url, origin.TaskID, lib.ArtifactTurn); !slices.Equal(got, []string{"answer to long question"}) {
		t.Fatalf("turn answers %q", got)
	}
	if ns := steerNotices(t, url, origin.TaskID); len(ns) != 1 {
		t.Fatalf("notices %+v, want only the queued one", ns)
	}
}

func TestAPISessionTurnKey(t *testing.T) {
	steer := &lib.Envelope{EnvelopeID: "env-7"}
	if got := apiTurnKey("task-1", nil); got != "task-1" {
		t.Fatalf("opening turn key %q", got)
	}
	if got := apiTurnKey("task-1", steer); got != "task-1/env-7" {
		t.Fatalf("follow-up key %q", got)
	}
}

// A follow-up turn that fails fails the task, naming the turn; a follow-up
// still queued behind it is refused task-ended before the terminal.
func TestAPI_FollowUpFailureNamesTheTurn(t *testing.T) {
	_, url := startServer(t)
	release := make(chan struct{})
	var n atomic.Int32
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		if n.Add(1) == 1 {
			<-release
			writeCompletion(w, c.sessionID, "first")
			return
		}
		http.Error(w, "boom", http.StatusBadGateway)
	})
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-api-fail2", "ctx-fail2", "q")
	waitFor(t, 10*time.Second, "turn 1 in flight", func() bool { return len(stub.seen()) == 1 })
	sendSteer(t, c, origin, "second")
	third := sendSteer(t, c, origin, "third")
	waitFor(t, 10*time.Second, "queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 2 })
	close(release)
	task := waitTerminal(t, c, origin.TaskID)
	want := "reason: hermes-api-failed - HTTP 502; session: a2a-ctx-fail2; turn: 2; body tail: "
	if task.State != lib.StateFailed || !strings.HasPrefix(terminalReason(t, task), want) {
		t.Fatalf("state %s reason %q, want prefix %q", task.State, terminalReason(t, task), want)
	}
	if got := artifactsNamed(t, url, origin.TaskID, lib.ArtifactTurn); !slices.Equal(got, []string{"first"}) {
		t.Fatalf("turn answers %q", got)
	}
	ns := steerNotices(t, url, origin.TaskID)
	if len(ns) != 3 || ns[2].EnvelopeID != third.EnvelopeID || ns[2].Reason != lib.SteerReasonTaskEnded ||
		ns[2].seq > finalSeq(t, url, origin.TaskID) {
		t.Fatalf("notices %+v, want the third refused task-ended before the terminal", ns)
	}
}

// Review Focus 10: a follow-up is a turn, so it is checked; one without a
// capability is refused and skipped, and turn 1's answer is the deliverable.
func TestAPI_FollowUpWithoutACapabilityIsSkipped(t *testing.T) {
	_, url := startServer(t)
	release := make(chan struct{})
	stub := keyedStub(t, release)
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-api-nocap", "ctx-nocap", "q")
	waitFor(t, 10*time.Second, "turn 1 in flight", func() bool { return len(stub.seen()) == 1 })
	bare, err := lib.NewFollowUpEnvelope(origin, gatewayParty,
		messagePayload(t, origin.TaskID, origin.ContextID, "unsigned"), lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", origin.TaskID), bare); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 10*time.Second, "queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	close(release)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted || len(stub.seen()) != 1 {
		t.Fatalf("state %s, %d requests; want completed on one request", task.State, len(stub.seen()))
	}
	ns := steerNotices(t, url, origin.TaskID)
	if len(ns) != 2 || ns[1].Steer != lib.SteerRefused || ns[1].Reason != lib.SteerReasonCapability || ns[1].state != lib.StateWorking {
		t.Fatalf("notices %+v, want the follow-up refused capability on a working status", ns)
	}
	if got := artifactsNamed(t, url, origin.TaskID, lib.ArtifactResult); !slices.Equal(got, []string{"answer to q"}) {
		t.Fatalf("result %q", got)
	}
}

// A cancel during a follow-up turn ends that turn's request and the task
// canceled; a follow-up queued behind it is refused task-ended.
func TestAPI_CancelDuringAFollowUpTurn(t *testing.T) {
	_, url := startServer(t)
	release, gone := make(chan struct{}), make(chan struct{})
	var n atomic.Int32
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		if n.Add(1) == 1 {
			<-release
			writeCompletion(w, c.sessionID, "first")
			return
		}
		<-r.Context().Done() // turn 2 runs until the bridge ends the request
		close(gone)
	})
	// A deadline past waitTerminal's wait: only the cancel can end turn 2 in time.
	startAPIBridge(t, url, stub, func(cfg *Config) { cfg.TaskDeadline = 2 * time.Minute })
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-api-cancel2", "ctx-cancel2", "q")
	waitFor(t, 10*time.Second, "turn 1 in flight", func() bool { return len(stub.seen()) == 1 })
	sendSteer(t, c, origin, "second")
	waitFor(t, 10*time.Second, "queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	close(release)
	waitFor(t, 10*time.Second, "turn 2 in flight", func() bool { return len(stub.seen()) == 2 })
	third := sendSteer(t, c, origin, "third")
	waitFor(t, 10*time.Second, "third queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 2 })
	publishCancel(t, c, origin)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCanceled || terminalReason(t, task) != "reason: canceled-by-request" {
		t.Fatalf("state %s reason %q", task.State, terminalReason(t, task))
	}
	select {
	case <-gone:
	case <-time.After(5 * time.Second):
		t.Fatal("turn 2's request outlived the cancel")
	}
	ns := steerNotices(t, url, origin.TaskID)
	if len(ns) != 3 || ns[2].EnvelopeID != third.EnvelopeID || ns[2].Reason != lib.SteerReasonTaskEnded ||
		ns[2].seq > finalSeq(t, url, origin.TaskID) {
		t.Fatalf("notices %+v, want the third refused task-ended before the terminal", ns)
	}
}

// The task deadline landing while a follow-up's request is in flight ends
// that request, and the terminal names the turn it ended (finalizeAPIError's
// deadline arm), as the in-band failure arms do.
func TestAPI_DeadlineDuringAFollowUpTurn(t *testing.T) {
	_, url := startServer(t)
	release, gone := make(chan struct{}), make(chan struct{})
	var n atomic.Int32
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		if n.Add(1) == 1 {
			<-release
			writeCompletion(w, c.sessionID, "first")
			return
		}
		<-r.Context().Done() // turn 2 runs until the bridge ends the request
		close(gone)
	})
	startAPIBridge(t, url, stub, func(cfg *Config) { cfg.TaskDeadline = 4 * time.Second })
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-api-deadline2", "ctx-deadline2", "q")
	waitFor(t, 10*time.Second, "turn 1 in flight", func() bool { return len(stub.seen()) == 1 })
	sendSteer(t, c, origin, "second")
	third := sendSteer(t, c, origin, "third")
	waitFor(t, 10*time.Second, "two queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 2 })
	close(release)
	task := waitTerminal(t, c, origin.TaskID)
	if reason := terminalReason(t, task); task.State != lib.StateFailed ||
		reason != "reason: deadline-exceeded - request ended after 4s; turn: 2" {
		t.Fatalf("state %s reason %q, want failed deadline-exceeded naming turn 2", task.State, reason)
	}
	select {
	case <-gone:
	case <-time.After(5 * time.Second):
		t.Fatal("turn 2's request outlived the deadline")
	}
	if got := len(stub.seen()); got != 2 {
		t.Fatalf("%d requests, want turn 1 and turn 2", got)
	}
	ns := steerNotices(t, url, origin.TaskID)
	if len(ns) != 3 || ns[2].EnvelopeID != third.EnvelopeID || ns[2].Reason != lib.SteerReasonTaskEnded ||
		ns[2].seq > finalSeq(t, url, origin.TaskID) {
		t.Fatalf("notices %+v, want the third refused task-ended before the terminal", ns)
	}
}

// Once the worker has chosen the current answer as the deliverable, a
// follow-up is refused task-ending, not queued behind the terminal.
func TestAPI_FollowUpAfterTheQueueClosesIsRefused(t *testing.T) {
	_, url := startServer(t)
	release := make(chan struct{})
	stub := keyedStub(t, release)
	b := startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-api-closed", "ctx-closed", "q")
	waitFor(t, 10*time.Second, "turn 1 in flight", func() bool { return len(stub.seen()) == 1 })
	if s := b.nextSteer(runOf(b, origin.TaskID)); s != nil { // the worker's decision, taken early
		t.Fatalf("nextSteer = %v on an empty queue", s)
	}
	late := sendSteer(t, c, origin, "too late")
	waitFor(t, 10*time.Second, "refusal", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	if n := steerNotices(t, url, origin.TaskID)[0]; n.Reason != lib.SteerReasonTaskEnding || n.EnvelopeID != late.EnvelopeID {
		t.Fatalf("notice = %+v, want refused task-ending", n)
	}
	close(release)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted || task.PostFinalDropped != 0 || len(stub.seen()) != 1 {
		t.Fatalf("state %s post-final %d requests %d", task.State, task.PostFinalDropped, len(stub.seen()))
	}
}

// Ruling: a follow-up turn sends nothing once the task is canceled, past its
// deadline, or finalized; the follow-up is refused task-ended exactly once,
// before the terminal. The check and the take share one critical section.
func TestAPI_FollowUpTurnDoesNotSendAfterTheTaskStops(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, nil)
	b := startAPIBridge(t, url, stub, nil)
	expired, cancelExpired := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancelExpired()
	cases := []struct {
		name    string
		prepare func(run *taskRun)
		ctx     context.Context
		state   lib.TaskState
		reason  string
		exact   bool // the whole reason, not a prefix: no turn is named
	}{
		{"canceled", func(run *taskRun) { run.canceled.Store(true) }, context.Background(), lib.StateCanceled, "reason: canceled-by-request", false},
		{"deadline-fired", func(run *taskRun) { run.deadlineHit.Store(true) }, context.Background(), lib.StateFailed, "reason: deadline-exceeded", false},
		{"deadline-passed", func(*taskRun) {}, expired, lib.StateFailed, "reason: deadline-exceeded", false},
		{"finalized", func(run *taskRun) { b.finalize(run, lib.StateFailed, shutdownReason, nil) }, context.Background(), lib.StateFailed, shutdownReason, false},
		// The bridge stopping before the send: the turn never left the
		// bridge, so the terminal does not name it, as the CLI's pre-spawn
		// gate does not.
		{"closing", func(*taskRun) { b.closing.Store(true) }, context.Background(), lib.StateFailed, shutdownReason, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			taskID := "task-api-nosend-" + tc.name
			run, steer := idleRun(t, b, taskID)
			if got := b.nextSteer(run); got != steer {
				t.Fatalf("nextSteer = %v, want the queued follow-up", got)
			}
			tc.prepare(run)
			defer b.closing.Store(false)
			if _, ok := b.apiTurn(run, tc.ctx, "a2a-ctx-"+taskID, "next", steer, 2); ok {
				t.Fatal("a follow-up turn answered after the task stopped")
			}
			if n := len(stub.seen()); n != 0 {
				t.Fatalf("%d requests sent after the task stopped", n)
			}
			var final *lib.StatusUpdate
			for _, env := range replayEvents(t, url, taskID) {
				if lib.IsFinalStatus(env) {
					final = &lib.StatusUpdate{}
					_ = json.Unmarshal(env.Payload, final)
				}
			}
			if final == nil || final.Status.State != tc.state || final.Status.Message == nil ||
				!strings.HasPrefix(final.Status.Message.Parts[0].Text, tc.reason) ||
				(tc.exact && final.Status.Message.Parts[0].Text != tc.reason) {
				t.Fatalf("terminal %+v, want %s %q", final, tc.state, tc.reason)
			}
			ns := steerNotices(t, url, taskID)
			if len(ns) != 1 || ns[0].EnvelopeID != steer.EnvelopeID || ns[0].Reason != lib.SteerReasonTaskEnded ||
				ns[0].seq > finalSeq(t, url, taskID) {
				t.Fatalf("notices %+v, want the follow-up refused task-ended once, before the terminal", ns)
			}
		})
	}
	// Control: with nothing stopping it, the same call sends the turn under
	// the follow-up's key, and the follow-up leaves the queue.
	run, steer := idleRun(t, b, "task-api-nosend-control")
	text, ok := b.apiTurn(run, context.Background(), "a2a-ctx-control", "next", steer, 2)
	if !ok || text != "answer to next" {
		t.Fatalf("control: ok %v text %q", ok, text)
	}
	if calls := stub.seen(); len(calls) != 1 || calls[0].idempotency != "task-api-nosend-control/"+steer.EnvelopeID {
		t.Fatalf("control: calls %+v", calls)
	}
	run.mu.Lock()
	left := len(run.steers)
	run.mu.Unlock()
	if left != 0 {
		t.Fatalf("control: %d follow-ups still queued after the turn started", left)
	}
	b.finalize(run, lib.StateCompleted, "", nil)
}

// Ruling: a notice's state follows the working publish, not the activity
// state, so a notice between working and the activity store on the API
// executor does not fold the task back to submitted.
func TestNoticeState_FollowsTheWorkingPublish(t *testing.T) {
	_, url := startServer(t)
	b := startAPIBridge(t, url, newAPIStub(t, nil), nil)
	run, _ := idleRun(t, b, "task-notice-state")
	read := func() lib.TaskState {
		run.mu.Lock()
		defer run.mu.Unlock()
		return b.noticeStateLocked(run)
	}
	if got := read(); got != lib.StateSubmitted {
		t.Fatalf("before working: %s, want submitted", got)
	}
	if err := b.publishWorking(testCtx(t), run); err != nil {
		t.Fatal(err)
	}
	if run.act.Load() != nil {
		t.Fatal("the activity state is stored; the window under test is gone")
	}
	if got := read(); got != lib.StateWorking {
		t.Fatalf("after working, before the activity store: %s, want working", got)
	}
	b.finalize(run, lib.StateCompleted, "", nil)
}

// holdingStub is an API server whose first request holds until release is
// called or the request ends, so follow-ups arrive while the task runs;
// every later request answers at once. in is closed once the first request
// is in.
func holdingStub(t *testing.T) (stub *apiStub, in <-chan struct{}, release func()) {
	t.Helper()
	inCh, rel := make(chan struct{}), make(chan struct{})
	var inOnce, relOnce sync.Once
	var first atomic.Bool
	stub = newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		if first.CompareAndSwap(false, true) {
			inOnce.Do(func() { close(inCh) })
			select {
			case <-rel:
			case <-r.Context().Done():
				return
			}
		}
		writeCompletion(w, c.sessionID, "answer to "+c.prompt)
	})
	release = func() { relOnce.Do(func() { close(rel) }) }
	t.Cleanup(release) // before the stub's Close, which waits for its handlers
	return stub, inCh, release
}

// A follow-up whose text is blank to Hermes is refused no-text when it
// arrives, never acked and then sent. Hermes refuses a turn whose text
// Python's str.strip() empties (400, "No user message found"), and that
// strips U+001C-U+001F, which Go's TrimSpace keeps; a text of NUL bytes
// alone survives both, and asks nothing. Queued, either would fail or waste
// the turn after turn 1's answer.
func TestAPI_BlankFollowUpIsRefusedNoText(t *testing.T) {
	_, url := startServer(t)
	stub, in, release := holdingStub(t)
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-api-blank", "ctx-blank", "long question")
	<-in
	blanks := []string{"\x00", "\x1f\x1c", " \x00\n\x1e\t", "　\x1d"}
	var ids []string
	for _, text := range blanks {
		ids = append(ids, sendSteer(t, c, origin, text).EnvelopeID)
	}
	waitFor(t, 10*time.Second, "four notices", func() bool { return len(steerNotices(t, url, origin.TaskID)) == len(blanks) })
	for i, n := range steerNotices(t, url, origin.TaskID) {
		if n.Steer != lib.SteerRefused || n.Reason != lib.SteerReasonNoText || n.EnvelopeID != ids[i] {
			t.Fatalf("notice for %q = %+v, want refused no-text", blanks[i], n)
		}
	}
	release()
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted || len(stub.seen()) != 1 {
		t.Fatalf("state %s, %d requests; want completed on the opening request alone", task.State, len(stub.seen()))
	}
	if got := artifactsNamed(t, url, origin.TaskID, lib.ArtifactResult); !slices.Equal(got, []string{"answer to long question"}) {
		t.Fatalf("result %q", got)
	}
}

// heldVerifier holds the verify subject and never replies, closing got when
// the first check arrives: a Check against it blocks until its timeout or
// its context ends.
func heldVerifier(t *testing.T, url string) <-chan struct{} {
	t.Helper()
	nc, err := nats.Connect(url, nats.Name("cap-verifier-held"))
	if err != nil {
		t.Fatalf("held verifier connect: %v", err)
	}
	t.Cleanup(nc.Close)
	got := make(chan struct{})
	var once sync.Once
	if _, err := nc.QueueSubscribe(capability.VerifySubscribe, capability.VerifyQueue,
		func(*nats.Msg) { once.Do(func() { close(got) }) }); err != nil {
		t.Fatalf("held verifier subscribe: %v", err)
	}
	return got
}

// A shutdown between a turn's answer and its publication keeps the answer.
// The worker holds turn 1's answer across the follow-up's capability check;
// shutdownTasks, landing then, must not write bridge-shutdown over it. The
// answer is the result, the follow-up is refused task-ended, and the task
// completes. shutdownTasks is called by hand while the check is parked on a
// verifier that never answers, so the worker cannot finish first and hide
// the race; the bridge's own shutdown follows and ends the check.
func TestAPI_ShutdownWhileAnAnswerIsHeldKeepsTheAnswer(t *testing.T) {
	_, url := startServerNoVerifier(t)
	checked := heldVerifier(t, url)
	stub, in, release := holdingStub(t)
	b, stop := startBridgeConfig(t, Config{
		NATSURL: url, Executor: ExecutorAPI, APIURL: stub.srv.URL + "/v1/chat/completions", APIKey: testAPIKey,
		TaskDeadline: 20 * time.Second, KillGrace: 500 * time.Millisecond, Scope: capability.NamespaceScope(""),
		// The opening runs unchecked, so the one check the verifier sees
		// is the follow-up's.
		CapabilityOptional: true,
	}, nil)
	c := gatewayClient(t, url)
	const taskID = "task-api-held"
	origin, err := lib.NewMessageEnvelope(gatewayParty, taskID, "ctx-held", "corr-"+taskID,
		messagePayload(t, taskID, "ctx-held", "long question"), lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", taskID), origin); err != nil {
		t.Fatal(err)
	}
	<-in
	steer, err := lib.NewFollowUpEnvelope(origin, gatewayParty, messagePayload(t, taskID, "ctx-held", "and the west"),
		lib.WithTo(lib.Party{Session: "platform"}),
		lib.WithAuthority(authorityFor(t, capability.Ref{Key: "root." + taskID, Revision: 1})))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", taskID), steer); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 10*time.Second, "queued", func() bool { return len(steerNotices(t, url, taskID)) == 1 })
	release()
	select {
	case <-checked: // the worker holds turn 1's answer, parked in the follow-up's check
	case <-time.After(10 * time.Second):
		t.Fatal("the follow-up's capability check never reached the verifier")
	}
	b.closing.Store(true)
	b.shutdownTasks()
	stop()
	task := waitTerminal(t, c, taskID)
	if task.State != lib.StateCompleted {
		t.Fatalf("state %s (%q), want completed: the shutdown wrote over a finished answer", task.State, terminalText(t, url, taskID))
	}
	if got := artifactsNamed(t, url, taskID, lib.ArtifactResult); !slices.Equal(got, []string{"answer to long question"}) {
		t.Fatalf("result %q, want turn 1's answer", got)
	}
	ns := steerNotices(t, url, taskID)
	if len(ns) != 2 || ns[1].EnvelopeID != steer.EnvelopeID || ns[1].Reason != lib.SteerReasonTaskEnded ||
		ns[1].seq > finalSeq(t, url, taskID) {
		t.Fatalf("notices %+v, want the follow-up refused task-ended before the terminal", ns)
	}
	if n := len(stub.seen()); n != 1 {
		t.Fatalf("%d requests, want the opening one only", n)
	}
}

// steerQueueCapacity bounds the follow-ups a task runs in total, not the
// ones waiting at one moment: a thread that sends one follow-up per turn,
// each taken off the queue as its turn starts, still stops at the cap, and
// the next is refused queue-full.
func TestAPI_FollowUpsStopAtTheTaskCap(t *testing.T) {
	_, url := startServer(t)
	const turns = steerQueueCapacity + 1
	var started, released [turns + 1]chan struct{}
	for i := range started {
		started[i], released[i] = make(chan struct{}), make(chan struct{})
	}
	var n atomic.Int32
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		i := int(n.Add(1))
		if i <= turns {
			close(started[i])
			select {
			case <-released[i]:
			case <-r.Context().Done():
				return
			}
		}
		writeCompletion(w, c.sessionID, fmt.Sprintf("answer %d", i))
	})
	var once sync.Once
	t.Cleanup(func() {
		once.Do(func() {
			for i := 1; i <= turns; i++ {
				select {
				case <-released[i]:
				default:
					close(released[i])
				}
			}
		})
	})
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-api-cap", "ctx-cap", "q")
	for turn := 1; turn <= turns; turn++ {
		select {
		case <-started[turn]:
		case <-time.After(10 * time.Second):
			t.Fatalf("turn %d never started", turn)
		}
		steer := sendSteer(t, c, origin, fmt.Sprintf("follow-up %d", turn))
		waitFor(t, 10*time.Second, fmt.Sprintf("notice %d", turn), func() bool { return len(steerNotices(t, url, origin.TaskID)) == turn })
		got := steerNotices(t, url, origin.TaskID)[turn-1]
		want := lib.SteerNotice{Steer: lib.SteerQueued, EnvelopeID: steer.EnvelopeID}
		if turn > steerQueueCapacity {
			want = lib.SteerNotice{Steer: lib.SteerRefused, EnvelopeID: steer.EnvelopeID, Reason: lib.SteerReasonQueueFull}
		}
		if got.SteerNotice != want {
			t.Fatalf("follow-up %d (none waiting, %d taken): notice %+v, want %+v", turn, turn-1, got.SteerNotice, want)
		}
		close(released[turn])
	}
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted {
		t.Fatalf("state %s reason %q", task.State, terminalReason(t, task))
	}
	if got := len(stub.seen()); got != turns {
		t.Fatalf("%d turns ran, want the opening turn plus %d follow-ups", got, steerQueueCapacity)
	}
}

// A message whose text parts hold only blank runes asks nothing, whichever
// executor would carry it; one with any other rune is asked as it is.
func TestPromptFromMessage_BlankRunes(t *testing.T) {
	msg := func(texts ...string) json.RawMessage {
		parts := make([]lib.Part, 0, len(texts))
		for _, s := range texts {
			parts = append(parts, lib.Part{Kind: "text", Text: s})
		}
		raw, err := json.Marshal(lib.Message{Role: "user", MessageID: "m", Parts: parts})
		if err != nil {
			t.Fatal(err)
		}
		return raw
	}
	for _, blank := range []string{"", " \t\n", "\x00", "\x1c\x1d\x1e\x1f", " 　\x00"} {
		if got, ok := promptFromMessage(msg(blank, blank)); ok {
			t.Errorf("parts %q: prompt %q, want nothing to ask", blank, got)
		}
	}
	if got, ok := promptFromMessage(msg("\x00", " a\x00 ")); !ok || got != " a\x00 " {
		t.Errorf("prompt %q, %v; want the one askable part as it is", got, ok)
	}
}
