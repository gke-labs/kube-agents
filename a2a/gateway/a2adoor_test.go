package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The A2A door against a real gateway on an embedded server: the same rig as
// the inject door's tests, with the A2A door as the only ingress.

const (
	a2aTestCaller        = "agent-1001"
	a2aTestPrincipal     = "eval:bnaylor"
	a2aTestOtherCaller   = "agent-1002"
	a2aTestOtherPrincpal = "eval:other"
	a2aTestUnknownCaller = "agent-9999"
	a2aTestToken         = "test-a2a-token"
	a2aTestGrace         = 90 * time.Second
	a2aTestPublicURL     = "https://kube-agents.example.test/a2a"
)

type a2aRig struct {
	g       *Gateway
	door    *A2ADoor
	bus     *lib.Client
	url     string
	base    string
	salt    []byte
	nextRPC int
}

func startA2ARig(t *testing.T) *a2aRig {
	t.Helper()
	return startA2ARigWith(t, func(door *A2ADoor) Adapter { return door })
}

// startA2ARigWith lets a test choose what the gateway drives: the door
// itself, or the door under the composite the shipped gateway builds.
func startA2ARigWith(t *testing.T, stack func(*A2ADoor) Adapter) *a2aRig {
	t.Helper()
	return startA2ARigOpts(t, stack, nil, nil)
}

// startA2ARigOpts is startA2ARigWith with a session spawner (nil: none) and
// a hook into the Config before New sees it.
func startA2ARigOpts(t *testing.T, stack func(*A2ADoor) Adapter, spawn *fakeSpawner, tweak func(*Config)) *a2aRig {
	t.Helper()
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)

	mapFile := filepath.Join(t.TempDir(), "a2a-door-principal-map")
	fixture := fmt.Sprintf("%s%s %s\n%s%s %s\n",
		a2aPrincipalPrefix, a2aTestCaller, a2aTestPrincipal,
		a2aPrincipalPrefix, a2aTestOtherCaller, a2aTestOtherPrincpal)
	if err := os.WriteFile(mapFile, []byte(fixture), 0o600); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	client, err := lib.Connect(ctx, url, lib.WithName("a2a-gateway-test"),
		lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)
	bus, err := lib.Connect(ctx, url, lib.WithName("a2a-executor-test"))
	if err != nil {
		t.Fatalf("executor client: %v", err)
	}
	t.Cleanup(bus.Close)

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	door, err := NewA2ADoor(ln.Addr().String(), a2aTestToken, A2ADoorOptions{
		PublicURL:        a2aTestPublicURL,
		DefaultAddressee: "platform",
	})
	if err != nil {
		t.Fatalf("NewA2ADoor: %v", err)
	}
	door.listener = ln

	salt := []byte("test-salt")
	cfg := &Config{
		NATSURL:                 url,
		PrincipalMapPath:        filepath.Join(t.TempDir(), "no-chat-principal-map"),
		A2ADoorListen:           ln.Addr().String(),
		A2ADoorToken:            a2aTestToken,
		A2ADoorPrincipalMapPath: mapFile,
		DefaultAddressee:        "platform",
		IdleTTL:                 30 * time.Minute,
		FirstEventGrace:         a2aTestGrace,
		AttributionSalt:         salt,
	}
	if tweak != nil {
		tweak(cfg)
	}
	opts := Options{Client: client, Adapter: stack(door), Config: cfg}
	if spawn != nil {
		opts.Spawner = spawn
	}
	g, err := New(opts)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if g.backend != a2aBackend {
		t.Fatalf("a gateway whose only ingress is the A2A door attributes to %q, want %q", g.backend, a2aBackend)
	}
	go func() { _ = g.Run(ctx) }()

	rig := &a2aRig{g: g, door: door, bus: bus, url: url, base: "http://" + ln.Addr().String(), salt: salt}
	waitFor(t, "the door to install its handler", func() bool {
		door.handlerMu.RLock()
		defer door.handlerMu.RUnlock()
		return door.handler != nil
	})
	return rig
}

// rpc posts one JSON-RPC request as caller and returns the decoded
// response. An empty caller sends no caller header.
func (r *a2aRig) rpc(t *testing.T, caller, method string, params any) rpcResponse {
	t.Helper()
	resp, status := r.rawRPC(t, a2aTestToken, caller, method, params)
	if status != http.StatusOK {
		t.Fatalf("%s: HTTP %d", method, status)
	}
	return resp
}

func (r *a2aRig) rawRPC(t *testing.T, token, caller, method string, params any) (rpcResponse, int) {
	t.Helper()
	r.nextRPC++
	body, err := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": r.nextRPC, "method": method, "params": params,
	})
	if err != nil {
		t.Fatal(err)
	}
	req, err := http.NewRequest(http.MethodPost, r.base+a2aRPCPath, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set(authorizationHeader, "Bearer "+token)
	}
	if caller != "" {
		req.Header.Set(a2aCallerHeader, caller)
	}
	res, err := a2aTestClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(res.Body)
	var out rpcResponse
	if res.StatusCode == http.StatusOK {
		if err := json.Unmarshal(raw, &out); err != nil {
			t.Fatalf("%s: undecodable response %q: %v", method, raw, err)
		}
	}
	return out, res.StatusCode
}

// a2aTestClient bounds every request: a blocking send the door holds for
// a2aBlockingWait must fail the test in under a minute, not five.
var a2aTestClient = &http.Client{Timeout: 60 * time.Second}

// sendAsync posts one request from a goroutine that calls nothing on t, so
// the test goroutine stays free to play the executor and then collect the
// response (or its error) with a bound of its own.
func (r *a2aRig) sendAsync(caller string, params any) <-chan asyncRPC {
	r.nextRPC++
	body, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": r.nextRPC, "method": a2aMethodSend, "params": params})
	out := make(chan asyncRPC, 1)
	go func() {
		req, err := http.NewRequest(http.MethodPost, r.base+a2aRPCPath, bytes.NewReader(body))
		if err != nil {
			out <- asyncRPC{err: err}
			return
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set(authorizationHeader, "Bearer "+a2aTestToken)
		req.Header.Set(a2aCallerHeader, caller)
		res, err := a2aTestClient.Do(req)
		if err != nil {
			out <- asyncRPC{err: err}
			return
		}
		defer res.Body.Close()
		raw, _ := io.ReadAll(res.Body)
		var resp rpcResponse
		if err := json.Unmarshal(raw, &resp); err != nil {
			out <- asyncRPC{err: fmt.Errorf("decode %q: %w", raw, err)}
			return
		}
		out <- asyncRPC{resp: resp}
	}()
	return out
}

type asyncRPC struct {
	resp rpcResponse
	err  error
}

// collect waits for an async send on the test goroutine.
func collect(t *testing.T, ch <-chan asyncRPC) rpcResponse {
	t.Helper()
	select {
	case a := <-ch:
		if a.err != nil {
			t.Fatalf("async send: %v", a.err)
		}
		return a.resp
	case <-time.After(90 * time.Second):
		t.Fatal("the async send did not return inside 90s")
	}
	return rpcResponse{}
}

// sendParams builds message/send params for one text.
func sendParams(text, messageID, contextID string, blocking bool) map[string]any {
	msg := map[string]any{
		"role":      "user",
		"parts":     []map[string]any{{"kind": "text", "text": text}},
		"messageId": messageID,
	}
	if contextID != "" {
		msg["contextId"] = contextID
	}
	params := map[string]any{"message": msg}
	if blocking {
		params["configuration"] = map[string]any{"blocking": true}
	}
	return params
}

func taskOf(t *testing.T, resp rpcResponse) a2aTaskObject {
	t.Helper()
	if resp.Error != nil {
		t.Fatalf("rpc error %d: %s", resp.Error.Code, resp.Error.Message)
	}
	raw, err := json.Marshal(resp.Result)
	if err != nil {
		t.Fatal(err)
	}
	var task a2aTaskObject
	if err := json.Unmarshal(raw, &task); err != nil {
		t.Fatalf("result is not a Task: %v (%s)", err, raw)
	}
	if task.Kind != a2aKindTask {
		t.Fatalf("result kind = %q, want %q", task.Kind, a2aKindTask)
	}
	return task
}

func (r *a2aRig) awaitTask(t *testing.T, addressee string) *lib.Envelope {
	t.Helper()
	var found *lib.Envelope
	waitFor(t, "task submission on "+addressee, func() bool {
		for _, env := range inSubjectEnvelopes(t, r.url, addressee) {
			if env.Kind == lib.KindMessage && found == nil {
				found = env
				return true
			}
		}
		return false
	})
	return found
}

func (r *a2aRig) execFor(t *testing.T, origin *lib.Envelope, addressee string) *lib.TaskExecution {
	t.Helper()
	exec, err := r.bus.NewTaskExecution(origin, lib.Party{Session: addressee, AgentType: "test-executor"}, addressee)
	if err != nil {
		t.Fatalf("NewTaskExecution: %v", err)
	}
	return exec
}

// complete plays the executor: working, a result artifact, completed.
func (r *a2aRig) complete(t *testing.T, origin *lib.Envelope, result string) {
	t.Helper()
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{
		Name:  lib.ArtifactResult,
		Parts: []lib.Part{{Kind: "text", Text: result}},
	}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
}

// getUntil polls tasks/get until cond holds on the Task.
func (r *a2aRig) getUntil(t *testing.T, caller, taskID, what string, cond func(a2aTaskObject) bool) a2aTaskObject {
	t.Helper()
	var last a2aTaskObject
	waitFor(t, what, func() bool {
		resp := r.rpc(t, caller, a2aMethodGet, map[string]any{"id": taskID})
		if resp.Error != nil {
			return false
		}
		last = taskOf(t, resp)
		return cond(last)
	})
	return last
}

// TestA2AResultArtifactIsWholeWhenTheRelayChunksIt: the relay posts a
// result in chat-sized chunks (discordChunk), one Post per chunk, and the
// composite delivers each to the door. The artifact is the deliverable the
// relay handed over whole (DeliverableObserver), not the last chunk; the
// chunks are the task's history.
func TestA2AResultArtifactIsWholeWhenTheRelayChunksIt(t *testing.T) {
	primary := newFakeAdapter()
	r := startA2ARigWith(t, func(door *A2ADoor) Adapter {
		return WithSideDoors(primary, []DoorSpec{A2ADoorSpec(door)}, nil)
	})
	report := strings.Repeat("fleet report line with enough text to need several chunks\n", 120)
	if len(report) <= 2*discordChunk {
		t.Fatalf("the fixture is %d bytes; it has to exceed two chunks (%d) to prove anything", len(report), 2*discordChunk)
	}
	sent := r.sendAsync(a2aTestCaller, sendParams("audit the fleet", "m-1", "", true))
	r.complete(t, r.awaitTask(t, "platform"), report)
	task := taskOf(t, collect(t, sent))
	if task.Status.State != lib.StateCompleted {
		t.Fatalf("state = %q, want completed", task.Status.State)
	}
	if len(task.Artifacts) != 1 || joinTextParts(task.Artifacts[0].Parts) != report {
		got := ""
		if len(task.Artifacts) == 1 {
			got = joinTextParts(task.Artifacts[0].Parts)
		}
		t.Fatalf("the result artifact is %d bytes, want the whole %d-byte deliverable (artifacts=%d)", len(got), len(report), len(task.Artifacts))
	}
	chunks := 0
	for _, m := range task.History {
		// chatChunks cuts at a newline, so a later chunk starts with one.
		if m.Role == a2aRoleAgent && strings.HasPrefix(strings.TrimSpace(joinTextParts(m.Parts)), "fleet report line") {
			chunks++
		}
	}
	if chunks < 3 {
		t.Errorf("history carries %d result chunks, want the relay's chunked posts (>= 3)", chunks)
	}
}

// TestA2ATaskMessagesCarryTheKindDiscriminator: every Message inside a Task
// (status.message, each history entry) goes out with kind: "message", as
// the top-level reply already does; lib.Message has no kind and is not
// what the door puts on the wire.
func TestA2ATaskMessagesCarryTheKindDiscriminator(t *testing.T) {
	r := startA2ARig(t)
	sent := r.sendAsync(a2aTestCaller, sendParams("kinds?", "m-1", "", true))
	r.complete(t, r.awaitTask(t, "platform"), "done")
	resp := collect(t, sent)
	raw, err := json.Marshal(resp.Result)
	if err != nil {
		t.Fatal(err)
	}
	var obj struct {
		Kind   string `json:"kind"`
		Status struct {
			Message map[string]any `json:"message"`
		} `json:"status"`
		History []map[string]any `json:"history"`
	}
	if err := json.Unmarshal(raw, &obj); err != nil {
		t.Fatal(err)
	}
	if obj.Kind != a2aKindTask {
		t.Fatalf("result kind = %q", obj.Kind)
	}
	if obj.Status.Message == nil || obj.Status.Message["kind"] != a2aKindMessage {
		t.Errorf("status.message = %v, want kind %q", obj.Status.Message, a2aKindMessage)
	}
	if len(obj.History) < 2 {
		t.Fatalf("history = %v, want the ask and the answer", obj.History)
	}
	for i, m := range obj.History {
		if m["kind"] != a2aKindMessage {
			t.Errorf("history[%d] = %v, want kind %q", i, m, a2aKindMessage)
		}
	}
}

// bareDoor is a door with no listener and no gateway, for the tests that
// drive its state machine directly.
func bareDoor(t *testing.T) *A2ADoor {
	t.Helper()
	d, err := NewA2ADoor("127.0.0.1:0", a2aTestToken, A2ADoorOptions{DefaultAddressee: "platform"})
	if err != nil {
		t.Fatal(err)
	}
	return d
}

// TestA2ATurnSlotIsHeldUntilTurnFinished: the accept returns message/send,
// but the turn runs on (a session spawn is seconds); the slot frees on
// TurnFinished, so a second claim inside the gap waits rather than reading
// counters one turn short.
func TestA2ATurnSlotIsHeldUntilTurnFinished(t *testing.T) {
	d := bareDoor(t)
	key := a2aConversationKey(a2aTestCaller, "ctx-1")
	user := lib.Message{Role: a2aRoleUser, Parts: textParts("go"), MessageID: "m-1", ContextID: "ctx-1"}
	if _, ok := d.claimTurn(context.Background(), key, a2aTestCaller, "ctx-1", user, time.Now().Add(a2aSubmitWait)); !ok {
		t.Fatal("the first claim on an idle conversation was refused")
	}
	d.TaskStarted(key, "task-1")
	d.TaskAccepted(key, "task-1")
	busy := func() bool {
		d.mu.Lock()
		defer d.mu.Unlock()
		return d.conversations[key].busy
	}
	if !busy() {
		t.Fatal("the slot was released at the accept; a second send in the accept-to-turn-end gap would be answered from the first turn's end")
	}
	d.TurnFinished(key)
	if busy() {
		t.Fatal("TurnFinished did not free the slot")
	}
	if _, ok := d.claimTurn(context.Background(), key, a2aTestCaller, "ctx-1", user, time.Now().Add(a2aSubmitWait)); !ok {
		t.Fatal("a claim after the turn ended was refused")
	}
}

// TestA2AClaimRefusesWithLessThanATurnLeft: a free slot with less than
// turnTimeout of the bound left is not handed over, so a refusal can never
// be pinned for a message the gateway goes on to act on (the premise
// handleInbound's routing states for both doors).
func TestA2AClaimRefusesWithLessThanATurnLeft(t *testing.T) {
	d := bareDoor(t)
	key := a2aConversationKey(a2aTestCaller, "ctx-1")
	user := lib.Message{Role: a2aRoleUser, Parts: textParts("go"), MessageID: "m-1", ContextID: "ctx-1"}
	start := time.Now()
	if _, ok := d.claimTurn(context.Background(), key, a2aTestCaller, "ctx-1", user, time.Now().Add(turnTimeout/2)); ok {
		t.Fatal("a claim with half a turn left of its bound was handed over")
	}
	if time.Since(start) > 2*time.Second {
		t.Errorf("the refusal took %s; it should not wait out the bound", time.Since(start))
	}
	d.mu.Lock()
	busy := d.conversations[key].busy
	d.mu.Unlock()
	if busy {
		t.Error("the refused claim left the slot taken")
	}
}

// TestA2ACapEvictsIdleConversationsNotLive: at the cap, the oldest IDLE
// conversation goes; one with a turn in flight or a task the relay posts
// under keeps its record, so its waiter and the relay never read a fresh
// incarnation with zeroed counters.
func TestA2ACapEvictsIdleConversationsNotLive(t *testing.T) {
	d := bareDoor(t)
	busyKey := a2aConversationKey(a2aTestCaller, "busy")
	activeKey := a2aConversationKey(a2aTestCaller, "active")
	user := lib.Message{Role: a2aRoleUser, Parts: textParts("go"), MessageID: "m-1"}
	if _, ok := d.claimTurn(context.Background(), busyKey, a2aTestCaller, "busy", user, time.Now().Add(a2aSubmitWait)); !ok {
		t.Fatal("claim")
	}
	d.mu.Lock()
	active := d.conversationLocked(activeKey)
	d.mu.Unlock()
	d.TaskStarted(activeKey, "task-live")
	d.mu.Lock()
	busy := d.conversations[busyKey]
	for i := 0; i < a2aMaxConversations+16; i++ {
		d.conversationLocked(a2aConversationKey(a2aTestCaller, fmt.Sprintf("idle-%d", i)))
	}
	n := len(d.conversations)
	sameBusy := d.conversations[busyKey] == busy
	sameActive := d.conversations[activeKey] == active
	_, oldestIdle := d.conversations[a2aConversationKey(a2aTestCaller, "idle-0")]
	d.mu.Unlock()
	if !sameBusy || !sameActive {
		t.Fatalf("a live conversation was evicted at the cap (busy kept=%v, active kept=%v)", sameBusy, sameActive)
	}
	if n != a2aMaxConversations {
		t.Errorf("%d conversations held, want the cap %d", n, a2aMaxConversations)
	}
	if oldestIdle {
		t.Error("the oldest idle conversation survived while the cap was enforced")
	}
}

// TestA2AConversationTaskLogIsBounded: a caller that keeps one contextId for
// the life of the pod does not grow the conversation's task log without
// bound; a waiter positioned past the eviction is told so rather than
// answered from the wrong id.
func TestA2AConversationTaskLogIsBounded(t *testing.T) {
	d := bareDoor(t)
	key := a2aConversationKey(a2aTestCaller, "forever")
	n := injectMaxEntries + 10
	for i := 0; i < n; i++ {
		d.TaskStarted(key, fmt.Sprintf("task-%d", i))
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	conv := d.conversations[key]
	if conv.tasks.total != n || len(conv.tasks.ids) != injectMaxEntries {
		t.Fatalf("log total=%d ids=%d, want total %d and ids capped at %d", conv.tasks.total, len(conv.tasks.ids), n, injectMaxEntries)
	}
	if _, evicted := conv.tasks.at(0); !evicted {
		t.Error("a waiter at position 0 is not told its id was evicted")
	}
	if id, evicted := conv.tasks.at(n - 1); evicted || id != fmt.Sprintf("task-%d", n-1) {
		t.Errorf("at(last) = %q evicted=%v", id, evicted)
	}
}

// TestA2ALiveConversationsAgeOutOfTheCapExemption: a conversation whose
// active task never terminates (an addressee with no executor) is exempt
// from eviction only until the gateway's task deadline; past it the record
// is idle and the cap holds, so a caller minting fresh contextIds cannot
// grow the door without bound.
func TestA2ALiveConversationsAgeOutOfTheCapExemption(t *testing.T) {
	d := bareDoor(t)
	d.taskDeadline = time.Minute
	stale := a2aConversationKey(a2aTestCaller, "stale")
	fresh := a2aConversationKey(a2aTestCaller, "fresh")
	d.TaskStarted(stale, "task-stale")
	d.TaskStarted(fresh, "task-fresh")
	d.mu.Lock()
	d.tasks["task-stale"].created = time.Now().Add(-2 * time.Minute)
	freshConv := d.conversations[fresh]
	for i := 0; i < a2aMaxConversations+8; i++ {
		d.conversationLocked(a2aConversationKey(a2aTestCaller, fmt.Sprintf("n-%d", i)))
	}
	_, staleKept := d.conversations[stale]
	freshKept := d.conversations[fresh] == freshConv
	n := len(d.conversations)
	d.mu.Unlock()
	if staleKept {
		t.Error("a conversation whose active task is past the task deadline survived the cap")
	}
	if !freshKept {
		t.Error("a conversation with a fresh active task was evicted")
	}
	if n != a2aMaxConversations {
		t.Errorf("%d conversations held, want the cap %d", n, a2aMaxConversations)
	}
	// And an active task that fell off the task cap no longer pins its
	// conversation either.
	d.mu.Lock()
	delete(d.tasks, "task-fresh")
	live := d.liveLocked(freshConv)
	d.mu.Unlock()
	if live {
		t.Error("a conversation whose active task is no longer held counts as live")
	}
}

// TestA2AResultArtifactBoundIsMarked: a deliverable past a2aMaxResultBytes
// is cut and the Task says so; one under it is kept whole, bytes for bytes.
func TestA2AResultArtifactBoundIsMarked(t *testing.T) {
	d := bareDoor(t)
	key := a2aConversationKey(a2aTestCaller, "big")
	d.TaskStarted(key, "task-big")
	big := strings.Repeat("x", a2aMaxResultBytes+10)
	d.TaskDelivered(key, "task-big", big)
	d.TaskTerminal(key, "task-big", lib.StateCompleted, TerminalFromExecutor, "")
	obj := d.taskObject("task-big")
	if got := obj.Metadata["resultTruncatedFrom"]; got != len(big) {
		t.Errorf("resultTruncatedFrom = %v, want %d", got, len(big))
	}
	if len(obj.Artifacts) != 1 || len(joinTextParts(obj.Artifacts[0].Parts)) > a2aMaxResultBytes+len("…") {
		t.Errorf("the cut artifact is not bounded: %d artifacts", len(obj.Artifacts))
	}
	d.TaskStarted(key, "task-100k")
	whole := strings.Repeat("y", 100*1024)
	d.TaskDelivered(key, "task-100k", whole)
	d.TaskTerminal(key, "task-100k", lib.StateCompleted, TerminalFromExecutor, "")
	obj = d.taskObject("task-100k")
	if _, cut := obj.Metadata["resultTruncatedFrom"]; cut || len(obj.Artifacts) != 1 || joinTextParts(obj.Artifacts[0].Parts) != whole {
		t.Errorf("a 100 KiB deliverable was not kept whole (cut=%v)", cut)
	}
}

// TestA2ASecondClaimDoesNotWipeTheFirstTurnsReply: the first waiter reads
// the posts past the offset it saw at its claim, so a second claim that
// wins the wakeup race cannot erase the reply before it is read.
func TestA2ASecondClaimDoesNotWipeTheFirstTurnsReply(t *testing.T) {
	d := bareDoor(t)
	key := a2aConversationKey(a2aTestCaller, "ctx-1")
	user := lib.Message{Role: a2aRoleUser, Parts: textParts("status?"), MessageID: "m-1", ContextID: "ctx-1"}
	prior1, ok := d.claimTurn(context.Background(), key, a2aTestCaller, "ctx-1", user, time.Now().Add(a2aSubmitWait))
	if !ok {
		t.Fatal("claim 1")
	}
	if _, err := d.Post(key, "✏️ steering sent"); err != nil {
		t.Fatal(err)
	}
	d.TurnFinished(key)
	// The second claim lands before the first waiter runs.
	if _, ok := d.claimTurn(context.Background(), key, a2aTestCaller, "ctx-1", user, time.Now().Add(a2aSubmitWait)); !ok {
		t.Fatal("claim 2")
	}
	taskID, reply, rerr := d.awaitTurn(context.Background(), key, prior1, time.Now().Add(time.Second))
	if rerr != nil || taskID != "" || reply == nil || !strings.Contains(joinTextParts(reply.Parts), "steering sent") {
		t.Fatalf("the first waiter got task=%q reply=%+v err=%+v, want its own turn's post", taskID, reply, rerr)
	}
}

// TestA2ATextPartWithAFilePayloadIsRefused: a kind: text part carrying a
// file or data member is refused like a file part, not accepted with the
// payload dropped from the turn and kept in history.
func TestA2ATextPartWithAFilePayloadIsRefused(t *testing.T) {
	r := startA2ARig(t)
	params := sendParams("hi", "m-1", "", false)
	params["message"].(map[string]any)["parts"] = []map[string]any{{"kind": "text", "text": "hi", "file": map[string]any{"bytes": "QUJD"}}}
	resp := r.rpc(t, a2aTestCaller, a2aMethodSend, params)
	if resp.Error == nil || resp.Error.Code != a2aErrContentTypeNotSupp {
		t.Fatalf("a text part with a file member was not refused: %+v", resp)
	}
	params["message"].(map[string]any)["parts"] = []map[string]any{{"kind": "text", "text": "hi", "data": map[string]any{"k": "v"}}}
	if resp := r.rpc(t, a2aTestCaller, a2aMethodSend, params); resp.Error == nil || resp.Error.Code != a2aErrContentTypeNotSupp {
		t.Fatalf("a text part with a data member was not refused: %+v", resp)
	}
	// A client that serialises nil members writes data: null; that is text.
	params["message"].(map[string]any)["parts"] = []map[string]any{{"kind": "text", "text": "hi", "data": nil, "file": nil}}
	if resp := r.rpc(t, a2aTestCaller, a2aMethodSend, params); resp.Error != nil {
		t.Fatalf("a text part with data: null was refused: %+v", resp.Error)
	}
}

// TestA2AReusedMessageIDOnAnotherContextIsRefused: the dedupe key is the
// caller's message id, but the id names one message on one context; a
// reused id on another context is answered with a refusal that says so,
// not with the first context's task.
func TestA2AReusedMessageIDOnAnotherContextIsRefused(t *testing.T) {
	r := startA2ARig(t)
	first := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("ask A", "m-1", "ctx-A", false)))
	again := r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("ask A", "m-1", "ctx-A", false))
	if again.Error != nil || taskOf(t, again).ID != first.ID {
		t.Fatalf("a retry on the same context was not answered with the same task: %+v", again)
	}
	other := r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("a different ask", "m-1", "ctx-B", false))
	if other.Error == nil || other.Error.Code != rpcInvalidParams {
		t.Fatalf("a reused messageId on another context was accepted: %+v", other)
	}
}

// TestA2AARefusedClaimDoesNotPinTheMessageID: a not-handed-over refusal is
// forgotten, so the same id can be sent again and routed afresh.
func TestA2AARefusedClaimDoesNotPinTheMessageID(t *testing.T) {
	d := bareDoor(t)
	key := a2aConversationKey(a2aTestCaller, "ctx-1")
	o, seen := d.claimSubmission(a2aTestCaller, "m-7", key)
	if seen {
		t.Fatal("first claim seen")
	}
	d.completeOutcome(o, "", nil, &rpcError{Code: rpcInternalError, Message: earlierTurnNote()})
	d.forgetSubmission(a2aTestCaller, "m-7", o)
	if _, seen := d.claimSubmission(a2aTestCaller, "m-7", key); seen {
		t.Fatal("the refused id is still pinned; a retry would be refused for good")
	}
	// A different outcome claimed under the id in the meantime is not the
	// one forgotten.
	o2, _ := d.claimSubmission(a2aTestCaller, "m-8", key)
	d.forgetSubmission(a2aTestCaller, "m-8", o)
	if o3, seen := d.claimSubmission(a2aTestCaller, "m-8", key); !seen || o3 != o2 {
		t.Fatal("forgetSubmission dropped an outcome it did not own")
	}
}

// TestA2ACardURLFollowsTheRequestWhenNoPublicURLIsSet: without
// A2A_DOOR_PUBLIC_URL the card points at the address it was fetched from,
// not the pod's loopback; forwarded headers win behind a proxy; a
// configured public URL is served as is.
func TestA2ACardURLFollowsTheRequestWhenNoPublicURLIsSet(t *testing.T) {
	d := bareDoor(t)
	url := func(host, proto, fhost string) string {
		req := httptest.NewRequest(http.MethodGet, "http://"+host+a2aCardPath, nil)
		req.Host = host
		if proto != "" {
			req.Header.Set("X-Forwarded-Proto", proto)
		}
		if fhost != "" {
			req.Header.Set("X-Forwarded-Host", fhost)
		}
		rec := httptest.NewRecorder()
		d.handleCard(rec, req)
		var card a2aAgentCard
		if err := json.Unmarshal(rec.Body.Bytes(), &card); err != nil {
			t.Fatal(err)
		}
		return card.URL
	}
	if got := url("localhost:18098", "", ""); got != "http://localhost:18098"+a2aRPCPath {
		t.Errorf("port-forward card url = %q", got)
	}
	if got := url("door.internal", "https", "agents.example.com"); got != "https://agents.example.com"+a2aRPCPath {
		t.Errorf("forwarded card url = %q", got)
	}
	fixed, err := NewA2ADoor("127.0.0.1:0", a2aTestToken, A2ADoorOptions{PublicURL: "https://fixed.example/a2a"})
	if err != nil {
		t.Fatal(err)
	}
	d = fixed
	if got := url("localhost:18098", "", ""); got != "https://fixed.example/a2a" {
		t.Errorf("configured card url = %q", got)
	}
}

// TestA2ARetainedBytesStayUnderTheBudget: what the door keeps per task
// (the caller's text, the posts, the result) is summed across tasks and
// bounded; past the budget the oldest tasks are gone whole, the newest is
// never the one to go, and the caller-only shape (big asks, no executor)
// is bounded the same as the result shape.
func TestA2ARetainedBytesStayUnderTheBudget(t *testing.T) {
	d := bareDoor(t)
	key := a2aConversationKey(a2aTestCaller, "big")
	ask := strings.Repeat("a", 200*1024)
	n := a2aRetainedBudgetBytes/len(ask) + 3
	for i := 0; i < n; i++ {
		id := fmt.Sprintf("task-%d", i)
		d.mu.Lock()
		d.conversationLocked(key).pending = lib.Message{Role: a2aRoleUser, Parts: textParts(ask), MessageID: id}
		d.mu.Unlock()
		d.TaskStarted(key, id)
	}
	d.mu.Lock()
	total, held := d.retainedBytes, len(d.tasks)
	_, oldest := d.tasks["task-0"]
	_, newest := d.tasks[fmt.Sprintf("task-%d", n-1)]
	d.mu.Unlock()
	if total > a2aRetainedBudgetBytes {
		t.Fatalf("retained %d bytes, budget %d", total, a2aRetainedBudgetBytes)
	}
	if oldest || !newest || held >= n {
		t.Errorf("oldest kept=%v newest kept=%v held=%d of %d; want the oldest evicted whole and the newest kept", oldest, newest, held, n)
	}
	// Results count too, and a task that is only results is bounded alike.
	d2 := bareDoor(t)
	each := a2aMaxResultBytes - 1
	m := a2aRetainedBudgetBytes/each + 2
	for i := 0; i < m; i++ {
		id := fmt.Sprintf("r-%d", i)
		d2.TaskStarted(key, id)
		d2.TaskDelivered(key, id, strings.Repeat("r", each))
	}
	d2.mu.Lock()
	total2 := d2.retainedBytes
	_, first := d2.tasks["r-0"]
	d2.mu.Unlock()
	if total2 > a2aRetainedBudgetBytes || first {
		t.Errorf("results: retained %d (budget %d), oldest kept=%v", total2, a2aRetainedBudgetBytes, first)
	}
	if obj := d2.taskObject(fmt.Sprintf("r-%d", m-1)); len(obj.Artifacts) != 0 && obj.Status.State == lib.StateCompleted {
		t.Errorf("unexpected shape: %+v", obj.Status)
	}
}

// TestA2ATurnLogIsBoundedInBytesAndSaysSo: one conversation's turn log is
// bounded by bytes as well as entries, and a reply whose head fell off the
// window is marked rather than returned as whole.
func TestA2ATurnLogIsBoundedInBytesAndSaysSo(t *testing.T) {
	d := bareDoor(t)
	key := a2aConversationKey(a2aTestCaller, "ctx-1")
	user := lib.Message{Role: a2aRoleUser, Parts: textParts("go"), MessageID: "m-1", ContextID: "ctx-1"}
	prior, ok := d.claimTurn(context.Background(), key, a2aTestCaller, "ctx-1", user, time.Now().Add(a2aSubmitWait))
	if !ok {
		t.Fatal("claim")
	}
	chunk := strings.Repeat("c", 1900)
	for i := 0; i < a2aMaxTurnPostBytes/len(chunk)+8; i++ {
		if _, err := d.Post(key, chunk); err != nil {
			t.Fatal(err)
		}
	}
	d.mu.Lock()
	logBytes := d.conversations[key].turnPosts.bytes
	d.mu.Unlock()
	if logBytes > a2aMaxTurnPostBytes {
		t.Fatalf("turn log holds %d bytes, bound %d", logBytes, a2aMaxTurnPostBytes)
	}
	d.TurnFinished(key)
	_, reply, rerr := d.awaitTurn(context.Background(), key, prior, time.Now().Add(time.Second))
	if rerr != nil || reply == nil {
		t.Fatalf("reply=%+v err=%+v", reply, rerr)
	}
	if reply.Metadata["postsEvicted"] != true {
		t.Errorf("a reply missing its head is not marked: metadata=%v", reply.Metadata)
	}
}

// TestA2AFollowUpOnACancelledTaskIsRefused: a follow-up naming a task whose
// cancel is published but unconfirmed is refused at the door (the gateway
// would route it as a new task or a steer of the next one), as is a
// follow-up on a task that is no longer the conversation's active one.
func TestA2AFollowUpOnACancelledTaskIsRefused(t *testing.T) {
	r := startA2ARig(t)
	task := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("long job", "m-1", "ctx-1", false)))
	r.awaitTask(t, "platform")
	if resp := r.rpc(t, a2aTestCaller, a2aMethodCancel, map[string]any{"id": task.ID}); resp.Error != nil {
		t.Fatalf("cancel: %+v", resp.Error)
	}
	params := sendParams("also check the PDBs", "m-2", "ctx-1", false)
	params["message"].(map[string]any)["taskId"] = task.ID
	resp := r.rpc(t, a2aTestCaller, a2aMethodSend, params)
	if resp.Error == nil || resp.Error.Code != rpcInvalidParams || !strings.Contains(resp.Error.Message, "cancel is pending") {
		t.Fatalf("a follow-up on a cancelled task was not refused as such: %+v", resp)
	}
	// Not the active task: a bare door, task A then task B on one conversation.
	d := bareDoor(t)
	key := a2aConversationKey(a2aTestCaller, "ctx-2")
	d.TaskStarted(key, "task-A")
	d.TaskStarted(key, "task-B")
	if got := d.activeTaskOf(key); got != "task-B" {
		t.Fatalf("activeTaskOf = %q", got)
	}
}

// TestA2ACardURLTakesTheFirstForwardedValue: behind two forwarding hops the
// headers are lists; the first element is used and the scheme is validated.
func TestA2ACardURLTakesTheFirstForwardedValue(t *testing.T) {
	d := bareDoor(t)
	req := httptest.NewRequest(http.MethodGet, "http://door.internal"+a2aCardPath, nil)
	req.Header.Set("X-Forwarded-Proto", "https, http")
	req.Header.Set("X-Forwarded-Host", "a2a.example.com, 10.8.0.5:8098")
	if got := d.rpcURLFor(req); got != "https://a2a.example.com"+a2aRPCPath {
		t.Errorf("list-valued forwarded headers gave %q", got)
	}
	req.Header.Set("X-Forwarded-Proto", "javascript")
	if got := d.rpcURLFor(req); got != "http://a2a.example.com"+a2aRPCPath {
		t.Errorf("an invalid forwarded scheme was reflected: %q", got)
	}
}

// TestA2ACardIsServedWithoutAToken: discovery reads the card before it knows
// which scheme to present, so the card is the one unauthenticated route, and
// it says the endpoint wants a bearer token.
func TestA2ACardIsServedWithoutAToken(t *testing.T) {
	r := startA2ARig(t)
	res, err := http.Get(r.base + a2aCardPath)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		t.Fatalf("card: HTTP %d", res.StatusCode)
	}
	var card a2aAgentCard
	if err := json.NewDecoder(res.Body).Decode(&card); err != nil {
		t.Fatal(err)
	}
	if card.URL != a2aTestPublicURL {
		t.Errorf("card url = %q, want the configured public URL %q", card.URL, a2aTestPublicURL)
	}
	if card.ProtocolVersion != a2aProtocolVersion {
		t.Errorf("protocolVersion = %q, want %q", card.ProtocolVersion, a2aProtocolVersion)
	}
	if len(card.Skills) != 1 || card.Skills[0].ID != "platform" {
		t.Errorf("skills = %+v, want the default addressee alone", card.Skills)
	}
	if card.Capabilities.Streaming {
		t.Error("the card advertises streaming, which the door does not serve yet")
	}
	if scheme, ok := card.SecuritySchemes["bearer"]; !ok || scheme.Scheme != "bearer" {
		t.Errorf("securitySchemes = %+v, want a bearer scheme", card.SecuritySchemes)
	}
}

// TestA2ARPCRequiresTheBearerToken: no token and a wrong token are both a
// 401 before the body is read, and the refusal does not say which.
func TestA2ARPCRequiresTheBearerToken(t *testing.T) {
	r := startA2ARig(t)
	for _, token := range []string{"", "wrong-token", strings.ToUpper(a2aTestToken)} {
		_, status := r.rawRPC(t, token, a2aTestCaller, a2aMethodSend, sendParams("hi", "m-1", "", false))
		if status != http.StatusUnauthorized {
			t.Errorf("token %q: HTTP %d, want 401", token, status)
		}
	}
	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 0 {
		t.Fatalf("an unauthenticated request reached the bus: %d envelopes", len(envs))
	}
}

// TestA2AMessageSendStartsATaskTheBusAttributesToTheDoor: the send answers
// with the Task once its submission is on the bus; the envelope carries the
// text, and its authority block names the door and the mapped eval identity.
func TestA2AMessageSendStartsATaskTheBusAttributesToTheDoor(t *testing.T) {
	r := startA2ARig(t)
	task := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("do the thing", "m-1", "ctx-1", false)))
	if task.ID == "" {
		t.Fatal("no task id")
	}
	if task.ContextID != "ctx-1" {
		t.Errorf("contextId = %q, want the caller's ctx-1", task.ContextID)
	}
	if task.Status.State != lib.StateSubmitted && task.Status.State != lib.StateWorking {
		t.Errorf("state = %q before any executor event, want submitted or working", task.Status.State)
	}
	if len(task.History) == 0 || task.History[0].Role != a2aRoleUser || joinTextParts(task.History[0].Parts) != "do the thing" {
		t.Errorf("history does not start with the caller's message: %+v", task.History)
	}

	origin := r.awaitTask(t, "platform")
	if origin.TaskID != task.ID {
		t.Fatalf("the bus carries task %q, the door answered %q", origin.TaskID, task.ID)
	}
	var m lib.Message
	if err := json.Unmarshal(origin.Payload, &m); err != nil {
		t.Fatal(err)
	}
	if joinTextParts(m.Parts) != "do the thing" {
		t.Errorf("payload text = %q", joinTextParts(m.Parts))
	}
	var authority Authority
	if err := json.Unmarshal(origin.Authority, &authority); err != nil {
		t.Fatal(err)
	}
	if authority.Requester.Backend != a2aBackend {
		t.Errorf("backend = %q, want %q", authority.Requester.Backend, a2aBackend)
	}
	if authority.Requester.VerifiedBy != a2aVerifiedBy {
		t.Errorf("verifiedBy = %q, want %q", authority.Requester.VerifiedBy, a2aVerifiedBy)
	}
	if authority.Requester.VerifiedBy == injectVerifiedBy || authority.Requester.VerifiedBy == "principal-map" {
		t.Errorf("the A2A door is indistinguishable from another ingress downstream: %q", authority.Requester.VerifiedBy)
	}
	want := NewPseudonymizer(r.salt).Hash(a2aTestPrincipal)
	if authority.Requester.Principal != want {
		t.Errorf("principal = %q, want the pseudonym of %s (the door's map resolves the caller)", authority.Requester.Principal, a2aTestPrincipal)
	}
	// The audience is the door's key (in the clear, as every backend's is),
	// kind dm, and a complete roster of one: the caller.
	if authority.Audience.Conversation != "a2a:"+a2aTestCaller+":ctx-1" || authority.Audience.Kind != a2aConversationKind ||
		len(authority.Audience.Roster) != 1 || !authority.Audience.RosterComplete {
		t.Errorf("audience = %+v", authority.Audience)
	}
}

// TestA2ATasksGetCarriesTheResultAsAnArtifact: after the executor completes,
// the Task has a completed status and the deliverable as its result
// artifact, which is where an A2A client reads the answer.
func TestA2ATasksGetCarriesTheResultAsAnArtifact(t *testing.T) {
	r := startA2ARig(t)
	task := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("audit the fleet", "m-1", "", false)))
	origin := r.awaitTask(t, "platform")
	r.complete(t, origin, "the fleet is fine")

	done := r.getUntil(t, a2aTestCaller, task.ID, "the task to complete", func(task a2aTaskObject) bool {
		return task.Status.State == lib.StateCompleted
	})
	if len(done.Artifacts) != 1 || done.Artifacts[0].Name != lib.ArtifactResult {
		t.Fatalf("artifacts = %+v, want one result artifact", done.Artifacts)
	}
	if got := joinTextParts(done.Artifacts[0].Parts); got != "the fleet is fine" {
		t.Errorf("result artifact text = %q", got)
	}
	if done.Status.Message == nil || !strings.Contains(joinTextParts(done.Status.Message.Parts), string(lib.StateCompleted)) {
		t.Errorf("status message = %+v, want the relay's terminal line", done.Status.Message)
	}
	if done.Metadata["terminalSource"] != string(TerminalFromExecutor) {
		t.Errorf("terminalSource = %v, want %q", done.Metadata["terminalSource"], TerminalFromExecutor)
	}
	// History: the ask, then what the relay posted (the deliverable).
	if len(done.History) < 2 || done.History[len(done.History)-1].Role != a2aRoleAgent {
		t.Errorf("history = %+v, want the ask followed by the agent's posts", done.History)
	}
}

// TestA2ABlockingSendReturnsTheCompletedTask: configuration.blocking holds
// the send until the terminal, which is the one-call curl demo.
func TestA2ABlockingSendReturnsTheCompletedTask(t *testing.T) {
	r := startA2ARig(t)
	sent := r.sendAsync(a2aTestCaller, sendParams("what is the answer?", "m-1", "", true))
	r.complete(t, r.awaitTask(t, "platform"), "42")
	task := taskOf(t, collect(t, sent))
	if task.Status.State != lib.StateCompleted {
		t.Fatalf("a blocking send returned state %q, want completed", task.Status.State)
	}
	if len(task.Artifacts) != 1 || joinTextParts(task.Artifacts[0].Parts) != "42" {
		t.Errorf("artifacts = %+v", task.Artifacts)
	}
}

// TestA2AUnmappedCallerIsRefusedAndStartsNothing: a caller the door's map
// does not carry is dropped by the gateway, the send says so, and nothing
// reaches the bus. Nothing is defaulted.
func TestA2AUnmappedCallerIsRefusedAndStartsNothing(t *testing.T) {
	r := startA2ARig(t)
	resp := r.rpc(t, a2aTestUnknownCaller, a2aMethodSend, sendParams("let me in", "m-1", "", false))
	if resp.Error == nil || resp.Error.Code != a2aErrAuthenticationFail {
		t.Fatalf("response = %+v, want error %d", resp, a2aErrAuthenticationFail)
	}
	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 0 {
		t.Fatalf("an unmapped caller reached the bus: %d envelopes", len(envs))
	}
	// And an unnamed caller is refused at the door, before the gateway.
	resp = r.rpc(t, "", a2aMethodSend, sendParams("anonymous", "m-2", "", false))
	if resp.Error == nil || resp.Error.Code != a2aErrAuthenticationFail {
		t.Fatalf("unnamed caller: response = %+v, want error %d", resp, a2aErrAuthenticationFail)
	}
}

// TestA2AMetadataCallerIsHonouredWhenThereIsNoHeader: a client that cannot
// set headers names itself in the message metadata.
func TestA2AMetadataCallerIsHonouredWhenThereIsNoHeader(t *testing.T) {
	r := startA2ARig(t)
	params := sendParams("hello from metadata", "m-1", "", false)
	params["message"].(map[string]any)["metadata"] = map[string]any{a2aCallerMetadataKey: a2aTestCaller}
	task := taskOf(t, r.rpc(t, "", a2aMethodSend, params))
	origin := r.awaitTask(t, "platform")
	if origin.TaskID != task.ID {
		t.Fatalf("bus task %q != answered %q", origin.TaskID, task.ID)
	}
}

// TestA2ATasksGetIsScopedToTheCaller: another mapped caller asking for the
// task is told it does not exist, and so is anyone asking for an id the
// door never minted.
func TestA2ATasksGetIsScopedToTheCaller(t *testing.T) {
	r := startA2ARig(t)
	task := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("mine", "m-1", "", false)))
	own := r.rpc(t, a2aTestCaller, a2aMethodGet, map[string]any{"id": task.ID})
	if own.Error != nil {
		t.Fatalf("the owner cannot read its task: %+v", own.Error)
	}
	other := r.rpc(t, a2aTestOtherCaller, a2aMethodGet, map[string]any{"id": task.ID})
	if other.Error == nil || other.Error.Code != a2aErrTaskNotFound {
		t.Errorf("another caller read the task: %+v", other)
	}
	unknown := r.rpc(t, a2aTestCaller, a2aMethodGet, map[string]any{"id": "no-such-task"})
	if unknown.Error == nil || unknown.Error.Code != a2aErrTaskNotFound {
		t.Errorf("an unknown id: %+v", unknown)
	}
}

// TestA2ADuplicateMessageIDAnswersWithTheSameTask: a retry of message/send
// with the same messageId is the same submission, not a second task.
func TestA2ADuplicateMessageIDAnswersWithTheSameTask(t *testing.T) {
	r := startA2ARig(t)
	first := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("once", "m-dup", "ctx-1", false)))
	second := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("once", "m-dup", "ctx-1", false)))
	if first.ID != second.ID {
		t.Fatalf("a retry started a second task: %q then %q", first.ID, second.ID)
	}
	time.Sleep(500 * time.Millisecond)
	var messages int
	for _, env := range inSubjectEnvelopes(t, r.url, "platform") {
		if env.Kind == lib.KindMessage {
			messages++
		}
	}
	if messages != 1 {
		t.Fatalf("%d submissions on the bus, want 1", messages)
	}
}

// TestA2ACancelPublishesAKindCancel: tasks/cancel on a running task puts a
// cancel for it on the bus; the Task comes back as it stands and the client
// polls for the executor's terminal.
func TestA2ACancelPublishesAKindCancel(t *testing.T) {
	r := startA2ARig(t)
	task := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("long job", "m-1", "", false)))
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	r.getUntil(t, a2aTestCaller, task.ID, "the working event to reach the door", func(task a2aTaskObject) bool {
		return task.Status.State == lib.StateWorking
	})

	canceled := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodCancel, map[string]any{"id": task.ID}))
	if canceled.ID != task.ID {
		t.Fatalf("cancel answered with task %q", canceled.ID)
	}
	var cancelEnv *lib.Envelope
	waitFor(t, "the cancel envelope on the in subject", func() bool {
		for _, env := range inSubjectEnvelopes(t, r.url, "platform") {
			if env.Kind == lib.KindCancel {
				cancelEnv = env
				return true
			}
		}
		return false
	})
	if cancelEnv.TaskID != task.ID {
		t.Fatalf("the cancel names task %q, want %q", cancelEnv.TaskID, task.ID)
	}
	if len(cancelEnv.Authority) == 0 {
		t.Error("the cancel carries no authority block")
	}
	if empty := r.rpc(t, a2aTestCaller, a2aMethodCancel, map[string]any{"id": ""}); empty.Error == nil || empty.Error.Code != rpcInvalidParams {
		t.Errorf("tasks/cancel with an empty id: %+v, want -32602 as tasks/get answers", empty)
	}
	// A retry by the owner (a lost response, a client timeout) is answered
	// with the task, not -32002: the cancel is on the bus already.
	again := r.rpc(t, a2aTestCaller, a2aMethodCancel, map[string]any{"id": task.ID})
	if again.Error != nil {
		t.Errorf("a retried cancel was refused: %d %s", again.Error.Code, again.Error.Message)
	} else if taskOf(t, again).ID != task.ID {
		t.Errorf("the retried cancel answered with another task")
	}
	// Another caller cannot cancel it.
	other := r.rpc(t, a2aTestOtherCaller, a2aMethodCancel, map[string]any{"id": task.ID})
	if other.Error == nil || other.Error.Code != a2aErrTaskNotFound {
		t.Errorf("another caller canceled the task: %+v", other)
	}
}

// TestA2AProtocolRefusals: the door answers in JSON-RPC's own terms for a
// method it does not have, a stream it does not serve yet, and a part it
// cannot route.
func TestA2AProtocolRefusals(t *testing.T) {
	r := startA2ARig(t)
	if resp := r.rpc(t, a2aTestCaller, "tasks/resubscribe", map[string]any{"id": "x"}); resp.Error == nil || resp.Error.Code != rpcMethodNotFound {
		t.Errorf("unknown method: %+v", resp)
	}
	if resp := r.rpc(t, a2aTestCaller, a2aMethodStream, sendParams("hi", "m-1", "", false)); resp.Error == nil || resp.Error.Code != a2aErrUnsupportedOp {
		t.Errorf("stream: %+v", resp)
	}
	params := sendParams("", "m-2", "", false)
	params["message"].(map[string]any)["parts"] = []map[string]any{{"kind": "data", "data": map[string]any{"a": 1}}}
	if resp := r.rpc(t, a2aTestCaller, a2aMethodSend, params); resp.Error == nil || resp.Error.Code != a2aErrContentTypeNotSupp {
		t.Errorf("data part: %+v", resp)
	}
	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 0 {
		t.Fatalf("a refused request reached the bus: %d envelopes", len(envs))
	}
}

// TestA2ADoorRefusesToBuildWithoutAToken: "unauthenticated" is not a thing a
// caller can choose, at the constructor as well as in FromEnv.
func TestA2ADoorRefusesToBuildWithoutAToken(t *testing.T) {
	if _, err := NewA2ADoor("127.0.0.1:0", "", A2ADoorOptions{}); err == nil {
		t.Fatal("a door with no token was built")
	}
	if _, err := NewA2ADoor("127.0.0.1:0", "   ", A2ADoorOptions{}); err == nil {
		t.Fatal("a door with a blank token was built")
	}
}

// TestA2AConfigGuards: the door alone starts a gateway, and a listen address
// without a token is refused.
func TestA2AConfigGuards(t *testing.T) {
	// setBaseEnv first, so a developer's exported knobs (a Slack pair, a
	// door map path, a spawn flag) cannot leak into either FromEnv below.
	setBaseEnv(t)
	t.Setenv("A2A_ATTRIBUTION_SALT", "test-salt")
	t.Setenv("DISCORD_TOKEN", "")
	t.Setenv("A2A_DOOR_LISTEN", "127.0.0.1:9999")
	t.Setenv("A2A_DOOR_TOKEN", "")
	if _, err := FromEnv(); err == nil || !strings.Contains(err.Error(), "A2A_DOOR_TOKEN") {
		t.Fatalf("a door without a token was accepted: %v", err)
	}
	t.Setenv("A2A_DOOR_TOKEN", "t")
	cfg, err := FromEnv()
	if err != nil {
		t.Fatalf("the door alone should start a gateway: %v", err)
	}
	if !cfg.A2ADoorArmed() || cfg.Backend() != "" {
		t.Fatalf("armed=%v backend=%q", cfg.A2ADoorArmed(), cfg.Backend())
	}
	if cfg.A2ADoorPrincipalMapPath != defaultA2ADoorPrincipalMapPath {
		t.Errorf("map path = %q", cfg.A2ADoorPrincipalMapPath)
	}
}

// TestA2AFollowUpOnARunningTaskReturnsTheGatewaysReply: a message/send that
// names the caller's running task is a steer, which the gateway answers
// with a notice and no new task. The door returns that notice as a Message
// rather than an error, and the steer is on the bus.
func TestA2AFollowUpOnARunningTaskReturnsTheGatewaysReply(t *testing.T) {
	r := startA2ARig(t)
	task := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("long job", "m-1", "ctx-1", false)))
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	r.getUntil(t, a2aTestCaller, task.ID, "the working event to reach the door", func(task a2aTaskObject) bool {
		return task.Status.State == lib.StateWorking
	})

	params := sendParams("also check the PDBs", "m-2", "ctx-1", false)
	params["message"].(map[string]any)["taskId"] = task.ID
	resp := r.rpc(t, a2aTestCaller, a2aMethodSend, params)
	if resp.Error != nil {
		t.Fatalf("a follow-up on a running task was refused: %d %s", resp.Error.Code, resp.Error.Message)
	}
	raw, _ := json.Marshal(resp.Result)
	var reply a2aMessageObject
	if err := json.Unmarshal(raw, &reply); err != nil || reply.Kind != a2aKindMessage || reply.Role != a2aRoleAgent {
		t.Fatalf("result = %s, want an agent Message", raw)
	}
	if text := joinTextParts(reply.Parts); !strings.Contains(text, "steer") {
		t.Errorf("reply text = %q, want the gateway's steering notice", text)
	}
	// The steer reached the bus as a second message on the task.
	waitFor(t, "the steer on the in subject", func() bool {
		n := 0
		for _, env := range inSubjectEnvelopes(t, r.url, "platform") {
			if env.Kind == lib.KindMessage && env.TaskID == task.ID {
				n++
			}
		}
		return n >= 2
	})
	// And the task's history carries the notice too.
	got := r.getUntil(t, a2aTestCaller, task.ID, "the notice in the task history", func(task a2aTaskObject) bool {
		for _, m := range task.History {
			if strings.Contains(joinTextParts(m.Parts), "steer") {
				return true
			}
		}
		return false
	})
	if got.Status.State != lib.StateWorking {
		t.Errorf("the steer changed the task's state to %q", got.Status.State)
	}
}

// TestA2AMetadataCallerReachesGetAndCancel: the client that cannot set
// headers can poll and cancel what it started.
func TestA2AMetadataCallerReachesGetAndCancel(t *testing.T) {
	r := startA2ARig(t)
	params := sendParams("hello from metadata", "m-1", "", false)
	params["message"].(map[string]any)["metadata"] = map[string]any{a2aCallerMetadataKey: a2aTestCaller}
	task := taskOf(t, r.rpc(t, "", a2aMethodSend, params))
	got := r.rpc(t, "", a2aMethodGet, map[string]any{"id": task.ID, "metadata": map[string]any{a2aCallerMetadataKey: a2aTestCaller}})
	if got.Error != nil {
		t.Fatalf("tasks/get with a metadata caller: %+v", got.Error)
	}
	if bare := r.rpc(t, "", a2aMethodGet, map[string]any{"id": task.ID}); bare.Error == nil || bare.Error.Code != a2aErrAuthenticationFail {
		t.Errorf("tasks/get with no caller at all: %+v", bare)
	}
	if colon := r.rpc(t, "alice:x", a2aMethodGet, map[string]any{"id": task.ID}); colon.Error == nil || colon.Error.Code != rpcInvalidParams {
		t.Errorf("a caller with a colon was accepted: %+v", colon)
	}
	// And cancel, the half this test is named for: no header, the caller in
	// metadata, answered with the task (the cancel reached the bus) rather
	// than -32010.
	r.awaitTask(t, "platform")
	canceled := r.rpc(t, "", a2aMethodCancel, map[string]any{"id": task.ID, "metadata": map[string]any{a2aCallerMetadataKey: a2aTestCaller}})
	if canceled.Error != nil {
		t.Fatalf("tasks/cancel with a metadata caller: %d %s", canceled.Error.Code, canceled.Error.Message)
	}
	if taskOf(t, canceled).ID != task.ID {
		t.Errorf("the metadata-caller cancel answered with another task")
	}
}

// TestA2ADoorUnderTheCompositeStillSeesItsTasks: the shipped topology. The
// door sits under WithSideDoors beside a chat backend, and the gateway's
// observers reach it only through the composite's prefix routing. A send
// through that stack still returns the task, and the chat backend sees no
// post for it.
func TestA2ADoorUnderTheCompositeStillSeesItsTasks(t *testing.T) {
	primary := newFakeAdapter()
	r := startA2ARigWith(t, func(door *A2ADoor) Adapter {
		return WithSideDoors(primary, []DoorSpec{A2ADoorSpec(door)}, nil)
	})
	sent := r.sendAsync(a2aTestCaller, sendParams("composite?", "m-1", "", true))
	r.complete(t, r.awaitTask(t, "platform"), "through the composite")
	task := taskOf(t, collect(t, sent))
	if task.Status.State != lib.StateCompleted || len(task.Artifacts) != 1 || joinTextParts(task.Artifacts[0].Parts) != "through the composite" {
		t.Fatalf("task through the composite = %+v", task)
	}
	primary.mu.Lock()
	defer primary.mu.Unlock()
	for _, p := range primary.posts {
		if strings.HasPrefix(p.Conversation, a2aKeyPrefix) {
			t.Errorf("the chat backend received a post for the door's conversation: %+v", p)
		}
	}
}
