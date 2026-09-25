package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
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
		FirstEventGrace:  a2aTestGrace,
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
	g, err := New(Options{Client: client, Adapter: stack(door), Config: cfg})
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
	req.Header.Set("Content-Type", a2aContentType)
	if token != "" {
		req.Header.Set(authorizationHeader, "Bearer "+token)
	}
	if caller != "" {
		req.Header.Set(a2aCallerHeader, caller)
	}
	res, err := http.DefaultClient.Do(req)
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
	go func() {
		origin := r.awaitTask(t, "platform")
		r.complete(t, origin, "42")
	}()
	task := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("what is the answer?", "m-1", "", true)))
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
	t.Setenv("NATS_URL", "nats://127.0.0.1:4222")
	t.Setenv("A2A_ATTRIBUTION_SALT", "test-salt")
	t.Setenv("DISCORD_TOKEN", "")
	t.Setenv("A2A_GCHAT_RELAY_URL", "")
	t.Setenv("A2A_INJECT_LISTEN", "")
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
	go func() {
		origin := r.awaitTask(t, "platform")
		r.complete(t, origin, "through the composite")
	}()
	task := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("composite?", "m-1", "", true)))
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
