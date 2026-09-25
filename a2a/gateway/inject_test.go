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
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The inject backend, end to end against a real nats-server: a POST is an
// inbound chat message, the gateway routes it the way it routes any other
// message, and the GET is the conversation the relay posted into. Every test
// here drives the executor side through the lib, as the bridge does.

// Compile-time contract. The inject backend is the one adapter that has to
// answer ABOUT a task, and the chat backends deliberately do not -- a human
// reads the chat, so the rendered text is their whole interface. The negative
// assertions are in TestOnlyTheInjectBackendObservesTasks below, which a
// compile-time var cannot express.
var (
	_ Adapter         = (*InjectAdapter)(nil)
	_ TaskObserver    = (*InjectAdapter)(nil)
	_ InboundObserver = (*InjectAdapter)(nil)
)

const (
	// The mapped and unmapped authors in the rig's principal map, matching
	// the fixture startRig uses. The mapped principal is an eval identity
	// because the door refuses anything else (resolveInjectPrincipal), which
	// TestInjectRefusesAPrincipalOutsideTheEvalNamespace pins.
	injectTestAuthor        = "1001"
	injectTestPrincipal     = "eval:bnaylor"
	injectTestUnknownAuthor = "9999"
	// injectTestToken is the door's bearer token in these tests.
	injectTestToken = "test-inject-token"
	// injectTestGrace is the gateway's first-event grace in the rig. Short,
	// so a test that needs the never-started heal does not wait ten minutes
	// for it; above the 1m floor FromEnv enforces, which this rig bypasses.
	injectTestGrace = 90 * time.Second
	// injectTestWait is the `wait` a polling GET asks for in these tests:
	// long enough that a healthy relay always answers inside it, short
	// enough that a broken one fails the test rather than hanging it.
	injectTestWait = 10
)

type injectRig struct {
	g       *Gateway
	adapter *InjectAdapter
	bus     *lib.Client
	url     string
	base    string
}

// startInjectRig assembles a gateway on an embedded server with the inject
// backend armed on a loopback port, and runs it.
func startInjectRig(t *testing.T) *injectRig {
	t.Helper()
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)

	// The door's own map, keys prefixed: the lookup is
	// injectPrincipalPrefix + author, so an unprefixed entry is unreachable
	// (TestInjectResolvesOnlyThroughItsOwnPrefixedSection).
	mapFile := filepath.Join(t.TempDir(), "inject-principal-map")
	fixture := fmt.Sprintf("%s%s %s\n", injectPrincipalPrefix, injectTestAuthor, injectTestPrincipal)
	if err := os.WriteFile(mapFile, []byte(fixture), 0o600); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	client, err := lib.Connect(ctx, url, lib.WithName("inject-gateway-test"),
		lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)
	bus, err := lib.Connect(ctx, url, lib.WithName("inject-executor-test"))
	if err != nil {
		t.Fatalf("executor client: %v", err)
	}
	t.Cleanup(bus.Close)

	// An already-bound loopback listener rather than a fixed port: the test
	// learns the address from it, and two tests in the same package cannot
	// collide on a port.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	adapter, err := NewInjectAdapter(ln.Addr().String(), injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatalf("NewInjectAdapter: %v", err)
	}
	adapter.listener = ln

	cfg := &Config{
		NATSURL:                url,
		PrincipalMapPath:       filepath.Join(t.TempDir(), "no-chat-principal-map"),
		InjectListen:           ln.Addr().String(),
		InjectToken:            injectTestToken,
		InjectPrincipalMapPath: mapFile,
		DefaultAddressee:       "platform",
		IdleTTL:                30 * time.Minute,
		FirstEventGrace:        injectTestGrace,
		AttributionSalt:        []byte("test-salt"),
	}
	g, err := New(Options{Client: client, Adapter: adapter, Config: cfg, Backend: injectBackend})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	go func() { _ = g.Run(ctx) }()

	rig := &injectRig{
		g:       g,
		adapter: adapter,
		bus:     bus,
		url:     url,
		base:    "http://" + ln.Addr().String(),
	}
	// The listener is bound already, so the only race is Run installing the
	// handler; a POST before that is refused with 503 by design.
	waitFor(t, "the inject backend to accept messages", func() bool {
		adapter.handlerMu.RLock()
		defer adapter.handlerMu.RUnlock()
		return adapter.handler != nil
	})
	return rig
}

// do issues one request to the door, with the bearer token unless token is
// empty. Every endpoint requires it, so the helper carries it rather than
// each caller.
func (r *injectRig) do(t *testing.T, method, target string, body []byte, token string) (*http.Response, error) {
	t.Helper()
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, target, reader)
	if err != nil {
		t.Fatalf("building %s %s: %v", method, target, err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set(authorizationHeader, "Bearer "+token)
	}
	return http.DefaultClient.Do(req)
}

// cancel calls the explicit cancel route on a conversation, for whatever it
// is running.
func (r *injectRig) cancel(t *testing.T, key, author string) injectResponse {
	t.Helper()
	return r.cancelTask(t, key, author, "")
}

// cancelTask calls the cancel route naming a task, the way the harness does
// with the id its POST was answered with.
func (r *injectRig) cancelTask(t *testing.T, key, author, taskID string) injectResponse {
	t.Helper()
	body, err := json.Marshal(cancelRequest{Author: author, TaskID: taskID})
	if err != nil {
		t.Fatal(err)
	}
	target := r.base + conversationsPath + key + cancelSuffix
	resp, err := r.do(t, http.MethodPost, target, body, injectTestToken)
	if err != nil {
		t.Fatalf("POST %s: %v", target, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("POST %s returned %d", target, resp.StatusCode)
	}
	var out injectResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decoding the cancel reply: %v", err)
	}
	return out
}

// inject posts one message and returns the decoded reply.
func (r *injectRig) inject(t *testing.T, conversation, author, text string) injectResponse {
	t.Helper()
	return r.injectWithID(t, conversation, author, text, "")
}

// injectWithID is inject with the caller's own backend message id, which is
// the dedupe key.
func (r *injectRig) injectWithID(t *testing.T, conversation, author, text, messageID string) injectResponse {
	t.Helper()
	body, err := json.Marshal(injectRequest{Conversation: conversation, Author: author, Text: text, MessageID: messageID})
	if err != nil {
		t.Fatal(err)
	}
	resp, err := r.do(t, http.MethodPost, r.base+injectPath, body, injectTestToken)
	if err != nil {
		t.Fatalf("POST %s: %v", injectPath, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("POST %s returned %d", injectPath, resp.StatusCode)
	}
	var out injectResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decoding the inject reply: %v", err)
	}
	return out
}

// conversation issues one GET, optionally awaiting a task's terminal.
func (r *injectRig) conversation(t *testing.T, key string, after int, taskID string, wait int) conversationResponse {
	t.Helper()
	target := fmt.Sprintf("%s%s%s?after=%d&wait=%d", r.base, conversationsPath, key, after, wait)
	if taskID != "" {
		target += "&task=" + taskID
	}
	resp, err := r.do(t, http.MethodGet, target, nil, injectTestToken)
	if err != nil {
		t.Fatalf("GET %s: %v", target, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET %s returned %d", target, resp.StatusCode)
	}
	var out conversationResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decoding the conversation reply: %v", err)
	}
	return out
}

// probe is the read route: one GET with probe=1 and no wait, which is how a
// caller asks what the gateway's record holds without sending a turn.
func (r *injectRig) probe(t *testing.T, key, taskID string) conversationResponse {
	t.Helper()
	target := fmt.Sprintf("%s%s%s?after=0&wait=0&%s=1", r.base, conversationsPath, key, probeParam)
	if taskID != "" {
		target += "&task=" + taskID
	}
	resp, err := r.do(t, http.MethodGet, target, nil, injectTestToken)
	if err != nil {
		t.Fatalf("GET %s: %v", target, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET %s returned %d", target, resp.StatusCode)
	}
	var out conversationResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decoding the read route's reply: %v", err)
	}
	if out.Probe == nil {
		t.Fatalf("GET %s answered without a probe report", target)
	}
	return out
}

// awaitTurnEnd blocks until no turn this door handed to the gateway is still
// running on the conversation. A POST is answered at the accept, and the
// gateway's end-of-turn record write lands after that and before the turn
// signal; a test that reads and rewrites the record on the strength of the
// POST's reply alone races that write, and Registry.Put has no revision
// guard, so whichever Put lands second wins.
func (r *injectRig) awaitTurnEnd(t *testing.T, key string) {
	t.Helper()
	waitFor(t, "the turn on "+key+" to end", func() bool {
		r.adapter.mu.Lock()
		defer r.adapter.mu.Unlock()
		conv, ok := r.adapter.conversations[key]
		return ok && conv.handed <= conv.turns
	})
}

// awaitRecordedTask blocks until the session record carries an active task
// AND the turn that wrote it has ended, for the reason awaitTurnEnd gives.
func (r *injectRig) awaitRecordedTask(t *testing.T, key string) {
	t.Helper()
	r.awaitTurnEnd(t, key)
	waitFor(t, "the session record to carry the active task", func() bool {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		rec, err := r.g.reg.Get(ctx, key)
		return err == nil && rec != nil && rec.ActiveTask != nil
	})
}

// awaitRecordRelease blocks until the record no longer holds an active task:
// the relay hands the door the terminal from inside relayTerminal and writes
// the release afterwards, so a test restoring ActiveTask on the strength of
// the door's terminal entry alone races that write.
func (r *injectRig) awaitRecordRelease(t *testing.T, key string) {
	t.Helper()
	waitFor(t, "the relay to release the record for "+key, func() bool {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		rec, err := r.g.reg.Get(ctx, key)
		return err == nil && rec != nil && rec.ActiveTask == nil
	})
}

func (r *injectRig) awaitTask(t *testing.T, addressee string) *lib.Envelope {
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

func (r *injectRig) execFor(t *testing.T, origin *lib.Envelope, addressee string) *lib.TaskExecution {
	t.Helper()
	exec, err := r.bus.NewTaskExecution(origin, lib.Party{Session: addressee, AgentType: "test-executor"}, addressee)
	if err != nil {
		t.Fatalf("NewTaskExecution: %v", err)
	}
	return exec
}

// entryTexts is the text of every post entry, which is what a chat user would
// have read.
func entryTexts(entries []InjectEntry, kind string) []string {
	var out []string
	for _, entry := range entries {
		if entry.Kind == kind {
			out = append(out, entry.Text)
		}
	}
	return out
}

// TestInjectSubmitsThroughHandleInbound is the whole point of the backend: a
// POST is an ordinary inbound message, so it mints a session, publishes a
// submission to the configured addressee with the ids and the authority block
// every other backend's message carries, and the POST learns the task id --
// which is what makes "await terminal of task id X" expressible at all.
func TestInjectSubmitsThroughHandleInbound(t *testing.T) {
	r := startInjectRig(t)

	reply := r.inject(t, "case-1", injectTestAuthor, "how is the fleet?")
	if !reply.Accepted || reply.TaskID == "" {
		t.Fatalf("submission was not accepted: %+v", reply)
	}
	if reply.Conversation != injectKeyPrefix+"case-1" {
		t.Fatalf("conversation = %q, want the prefixed key", reply.Conversation)
	}

	origin := r.awaitTask(t, "platform")
	if origin.TaskID != reply.TaskID {
		t.Fatalf("the POST returned task %q but the bus carries %q", reply.TaskID, origin.TaskID)
	}
	if origin.To == nil || origin.To.Session != "platform" {
		t.Fatalf("to = %+v, want platform", origin.To)
	}
	if !strings.HasPrefix(origin.CorrelationID, "corr-") || origin.ContextID == "" {
		t.Fatalf("ids look wrong: correlation %q context %q", origin.CorrelationID, origin.ContextID)
	}

	var message lib.Message
	if err := json.Unmarshal(origin.Payload, &message); err != nil {
		t.Fatal(err)
	}
	if message.Role != "user" || joinTextParts(message.Parts) != "how is the fleet?" {
		t.Fatalf("payload = %+v", message)
	}
}

// TestInjectAuthorityNamesTheBearerDoor: the authority block is the audit
// record, and on this door it must name its own mechanism. The door's map
// resolved WHO and its bearer token admitted the caller -- neither is the
// authenticated websocket Discord has nor the IAM-locked topic Chat has, so
// verifiedBy is the door's own value and nothing downstream can mistake an
// eval submission for a Chat message. The identifiers are pseudonymized
// exactly as every other backend's are.
func TestInjectAuthorityNamesTheBearerDoor(t *testing.T) {
	r := startInjectRig(t)
	r.inject(t, "case-authority", injectTestAuthor, "audit me")

	origin := r.awaitTask(t, "platform")
	var authority Authority
	if err := json.Unmarshal(origin.Authority, &authority); err != nil {
		t.Fatalf("authority block: %v", err)
	}
	if authority.Requester.Backend != injectBackend {
		t.Fatalf("backend = %q, want %q", authority.Requester.Backend, injectBackend)
	}
	if authority.Requester.VerifiedBy != injectVerifiedBy {
		t.Fatalf("verifiedBy = %q, want %q", authority.Requester.VerifiedBy, injectVerifiedBy)
	}
	if authority.Requester.VerifiedBy == gchatVerifiedBy || authority.Requester.VerifiedBy == "principal-map" {
		t.Fatalf("verifiedBy = %q: the door must not borrow a real backend's mechanism",
			authority.Requester.VerifiedBy)
	}
	if !strings.HasPrefix(authority.Requester.Principal, "hmac:") {
		t.Fatalf("principal is not pseudonymized: %q", authority.Requester.Principal)
	}
	if authority.Requester.Principal == injectTestPrincipal {
		t.Fatal("the plaintext principal reached the bus")
	}
	if authority.Audience.Conversation != injectKeyPrefix+"case-authority" {
		t.Fatalf("audience conversation = %q, want the prefixed key", authority.Audience.Conversation)
	}
	// The requester is always in their own audience, and on a synthetic
	// one-participant conversation that is the whole roster.
	if len(authority.Audience.Roster) == 1 && authority.Audience.Roster[0] != authority.Requester.Principal {
		t.Fatalf("the requester's roster entry %q is not their principal %q: the door's audience "+
			"resolved the raw author id instead of the map's inject: section",
			authority.Audience.Roster[0], authority.Requester.Principal)
	}
	if len(authority.Audience.Roster) != 1 || !authority.Audience.RosterComplete {
		t.Fatalf("audience = %+v", authority.Audience)
	}
}

// TestInjectConversationCarriesTheReplyAndTheTerminal: the GET returns what
// the relay would have posted, terminal included. This is the whole contract
// the eval transport reads -- the deliverable is a chat post, because that is
// what a customer receives.
func TestInjectConversationCarriesTheReplyAndTheTerminal(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-reply", injectTestAuthor, "do the thing")

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{
		Name:  lib.ArtifactResult,
		Parts: []lib.Part{{Kind: "text", Text: "the fleet is fine"}},
	}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}

	// One blocking GET, the way the harness awaits a terminal: poll from the
	// start of the conversation, naming the task.
	var terminal string
	var posts []string
	after := 0
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) && terminal == "" {
		page := r.conversation(t, reply.Conversation, after, reply.TaskID, injectTestWait)
		posts = append(posts, entryTexts(page.Entries, InjectEntryPost)...)
		after = page.LastSeq
		terminal = page.Terminal
	}
	if terminal != string(lib.StateCompleted) {
		t.Fatalf("terminal = %q, want completed", terminal)
	}
	// The deliverable is a post, and it arrived BEFORE the terminal: a
	// reader that stops at the terminal must already have the answer.
	if len(posts) == 0 || posts[len(posts)-1] != "the fleet is fine" {
		t.Fatalf("the result was not the last post the conversation received: %v", posts)
	}
	if posts[0] != "⏳ submitted…" {
		t.Fatalf("the placeholder is missing; posts = %v", posts)
	}
}

// TestInjectTerminalIsAnsweredAfterTheFact: a reader that arrives late must
// still learn the answer. The terminal is recorded, not merely signalled, so a
// harness whose poll lost a race does not wait out its deadline for an event
// that has already happened.
func TestInjectTerminalIsAnsweredAfterTheFact(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-late", injectTestAuthor, "answer then leave")

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishArtifact(ctx, lib.Artifact{
		Name:  lib.ArtifactResult,
		Parts: []lib.Part{{Kind: "text", Text: "done already"}},
	}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the terminal to be recorded", func() bool {
		_, _, terminal := r.adapter.snapshot(reply.Conversation, 0, reply.TaskID)
		return terminal != ""
	})

	// Ask with `after` past every entry and no wait: there is nothing new to
	// report, and the terminal must still come back.
	_, lastSeq, _ := r.adapter.snapshot(reply.Conversation, 0, "")
	page := r.conversation(t, reply.Conversation, lastSeq, reply.TaskID, 0)
	if page.Terminal != string(lib.StateCompleted) {
		t.Fatalf("a late reader got terminal %q, want completed", page.Terminal)
	}
	if len(page.Entries) != 0 {
		t.Fatalf("nothing was new, but the GET returned %d entries", len(page.Entries))
	}
}

// TestInjectGradesAFailedTask: a task an executor took and
// ended `failed` is an outcome, not an infrastructure fault, and both the
// reason and the terminal state have to reach the caller.
func TestInjectGradesAFailedTask(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-failed", injectTestAuthor, "break")

	origin := r.awaitTask(t, "platform")
	// Built by hand rather than through PublishStatus: a terminal that
	// carries a reason needs a Message on the status, which the execution
	// helper does not take.
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID: origin.TaskID, ContextID: origin.ContextID,
		Status: lib.TaskStatus{State: lib.StateFailed, Message: &lib.Message{
			Role: "agent", MessageID: "msg-fail",
			Parts: []lib.Part{{Kind: "text", Text: "the cluster is unreachable"}},
		}},
		Final: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(lib.Party{Session: "platform"},
		origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(context.Background(), lib.TaskEventsSubject("platform", origin.TaskID), env); err != nil {
		t.Fatal(err)
	}

	waitFor(t, "the failure to be recorded", func() bool {
		_, _, terminal := r.adapter.snapshot(reply.Conversation, 0, reply.TaskID)
		return terminal != ""
	})
	entries, _, terminal := r.adapter.snapshot(reply.Conversation, 0, reply.TaskID)
	if terminal != string(lib.StateFailed) {
		t.Fatalf("terminal = %q, want failed", terminal)
	}
	posts := strings.Join(entryTexts(entries, InjectEntryPost), "\n")
	if !strings.Contains(posts, "the cluster is unreachable") {
		t.Fatalf("the failure reason never reached the conversation: %q", posts)
	}
}

// TestInjectDropsAnUnmappedAuthor: the principal map is what admits a sender,
// and the inject backend gets no exemption from it. The reply says no task
// started and carries the drop notice, so a misconfigured harness sees the
// reason rather than a silent empty conversation.
func TestInjectDropsAnUnmappedAuthor(t *testing.T) {
	r := startInjectRig(t)

	reply := r.inject(t, "case-unmapped", injectTestUnknownAuthor, "let me in")
	if reply.Accepted || reply.TaskID != "" {
		t.Fatalf("an unmapped author started a task: %+v", reply)
	}
	if reply.Note == "" {
		t.Fatal("the reply carries no note saying why nothing started")
	}
	if reply.Refusal != injectRefusalUnverifiedAuthor {
		t.Fatalf("refusal = %q, want %q", reply.Refusal, injectRefusalUnverifiedAuthor)
	}
	posts := strings.Join(entryTexts(reply.Entries, InjectEntryPost), "\n")
	if !strings.Contains(posts, "can't verify") {
		t.Fatalf("the drop notice is not in the reply: %q", posts)
	}
	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 0 {
		t.Fatalf("an unverified sender reached the bus: %d envelopes", len(envs))
	}
}

// TestInjectEntriesArePollableWithoutGapsOrRepeats: the sequence is the whole
// of the polling contract. A reader that passes back the last sequence it saw
// must get every later entry exactly once -- a gap loses the deliverable and a
// repeat double-counts it.
func TestInjectEntriesArePollableWithoutGapsOrRepeats(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-seq", injectTestAuthor, "narrate")

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	for _, note := range []string{"reading the fleet", "checking pods", "writing it up"} {
		if err := exec.PublishArtifact(ctx, lib.Artifact{
			Name:  lib.ArtifactProgress,
			Parts: []lib.Part{{Kind: "text", Text: note}},
		}); err != nil {
			t.Fatal(err)
		}
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the terminal", func() bool {
		_, _, terminal := r.adapter.snapshot(reply.Conversation, 0, reply.TaskID)
		return terminal != ""
	})

	var seqs []int
	after := 0
	for {
		page := r.conversation(t, reply.Conversation, after, reply.TaskID, 0)
		if len(page.Entries) == 0 {
			break
		}
		for _, entry := range page.Entries {
			seqs = append(seqs, entry.Seq)
		}
		after = page.LastSeq
	}
	if len(seqs) == 0 {
		t.Fatal("the conversation is empty")
	}
	for i, seq := range seqs {
		if seq != i+1 {
			t.Fatalf("sequence %d of the poll is %d; the numbering must be dense and start at 1: %v", i, seq, seqs)
		}
	}
}

// TestInjectFollowUpReachesTheSameSession: two POSTs with one conversation key
// are two turns of one conversation, which is what lets the case runner send a
// follow-up the way a second chat message would arrive.
func TestInjectFollowUpReachesTheSameSession(t *testing.T) {
	r := startInjectRig(t)
	first := r.inject(t, "case-followup", injectTestAuthor, "first ask")

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the first task to end", func() bool {
		_, _, terminal := r.adapter.snapshot(first.Conversation, 0, first.TaskID)
		return terminal != ""
	})

	second := r.inject(t, "case-followup", injectTestAuthor, "second ask")
	if !second.Accepted || second.TaskID == first.TaskID {
		t.Fatalf("the follow-up did not start its own task: %+v", second)
	}

	// Same conversation means one session record and therefore one contextId
	// across both tasks -- the durable name of the conversation on the bus.
	//
	// Waited for rather than read once: POST /inject answers on the accept,
	// and the turn's record write lands after the answer, so an immediate
	// read races the end-of-turn KV write.
	var rec *SessionRecord
	waitFor(t, "both turns on the session record", func() bool {
		var err error
		rec, err = r.g.reg.Get(context.Background(), first.Conversation)
		return err == nil && rec != nil && len(rec.Tasks) >= 2
	})
	ids := map[string]bool{}
	for _, ref := range rec.Tasks {
		ids[ref.ID] = true
	}
	if !ids[first.TaskID] || !ids[second.TaskID] {
		t.Fatalf("the session records tasks %+v, want both %s and %s",
			rec.Tasks, first.TaskID, second.TaskID)
	}
}

// TestInjectConversationKey pins the validation. The prefix is what keeps a
// synthetic conversation distinguishable from a real backend's in the session
// registry and in every authority block, so a caller must not be able to spell
// one that looks like a Chat thread.
func TestInjectConversationKey(t *testing.T) {
	for _, tc := range []struct {
		name  string
		in    string
		want  string
		wantE bool
	}{
		{"plain", "case-1", "inject:case-1", false},
		{"already prefixed", "inject:case-1", "inject:case-1", false},
		{"trimmed", "  case-1  ", "inject:case-1", false},
		{"empty", "", "", true},
		{"only spaces", "   ", "", true},
		{"bare prefix", injectKeyPrefix, "", true},
		{"bare prefix and spaces", " " + injectKeyPrefix + " ", "", true},
		{"foreign backend", "gchat:spaces/AAA/threads/BBB", "", true},
		{"newline", "case\n1", "", true},
		{"too long", strings.Repeat("x", injectMaxKeyRunes+1), "", true},
		// The bound is on the key with its prefix, the same on the way in and
		// on the way back: a raw value that fits only without the prefix is
		// refused at the POST rather than accepted there and refused on the GET.
		{"longest raw that fits with the prefix", strings.Repeat("x", injectMaxKeyRunes-len([]rune(injectKeyPrefix))),
			injectKeyPrefix + strings.Repeat("x", injectMaxKeyRunes-len([]rune(injectKeyPrefix))), false},
		{"raw that fits only without the prefix", strings.Repeat("x", injectMaxKeyRunes-len([]rune(injectKeyPrefix))+1), "", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := injectConversationKey(tc.in)
			if tc.wantE {
				if err == nil {
					t.Fatalf("injectConversationKey(%q) = %q, want an error", tc.in, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("injectConversationKey(%q): %v", tc.in, err)
			}
			if got != tc.want {
				t.Fatalf("injectConversationKey(%q) = %q, want %q", tc.in, got, tc.want)
			}
			// The key a POST answers with is handed straight back on the read
			// and cancel routes, which run it through this validator again.
			if again, err := injectConversationKey(got); err != nil || again != got {
				t.Fatalf("injectConversationKey(%q) handed back = (%q, %v), want it accepted unchanged", got, again, err)
			}
		})
	}
}

// TestInjectRefusesMalformedRequests: every field the handler needs is
// checked, and the refusal names what is missing. A harness misconfiguration
// has to read as a 400 with a reason, never as a conversation that stays
// silent.
func TestInjectRefusesMalformedRequests(t *testing.T) {
	r := startInjectRig(t)

	for _, tc := range []struct {
		name string
		body string
		want int
	}{
		{"not json", "{", http.StatusBadRequest},
		{"no conversation", `{"author":"1001","text":"hi"}`, http.StatusBadRequest},
		{"no author", `{"conversation":"c","text":"hi"}`, http.StatusBadRequest},
		{"no text", `{"conversation":"c","author":"1001"}`, http.StatusBadRequest},
		{"author too long", fmt.Sprintf(`{"conversation":"c","author":%q,"text":"hi"}`,
			strings.Repeat("a", injectMaxAuthorRunes+1)), http.StatusBadRequest},
		{"author with a newline", `{"conversation":"c","author":"10\n01","text":"hi"}`, http.StatusBadRequest},
		{"blank text", `{"conversation":"c","author":"1001","text":"   "}`, http.StatusBadRequest},
		{"message id with a newline", `{"conversation":"c","author":"1001","text":"hi","messageId":"m\n1"}`, http.StatusBadRequest},
		{"message id too long", fmt.Sprintf(`{"conversation":"c","author":"1001","text":"hi","messageId":%q}`,
			strings.Repeat("m", injectMaxMessageIDRunes+1)), http.StatusBadRequest},
	} {
		t.Run(tc.name, func(t *testing.T) {
			resp, err := r.do(t, http.MethodPost, r.base+injectPath, []byte(tc.body), injectTestToken)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			if resp.StatusCode != tc.want {
				t.Fatalf("status = %d, want %d", resp.StatusCode, tc.want)
			}
		})
	}

	// The method guards: neither endpoint may be driven the other way round.
	// Authenticated, because the door refuses an unauthenticated caller
	// before it looks at the method at all.
	resp, err := r.do(t, http.MethodGet, r.base+injectPath, nil, injectTestToken)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("GET /inject = %d, want 405", resp.StatusCode)
	}
	// A POST to a conversation that is not the cancel route: reading is a
	// GET, and the only POST under /conversations is /cancel.
	postResp, err := r.do(t, http.MethodPost, r.base+conversationsPath+"case-1", []byte("{}"), injectTestToken)
	if err != nil {
		t.Fatal(err)
	}
	defer postResp.Body.Close()
	if postResp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("POST /conversations = %d, want 405", postResp.StatusCode)
	}
}

// TestInjectNamesWhoDeclaredATerminal: a task the gateway could not put on
// the bus ends `failed` exactly like one an executor took and failed, and the
// two mean opposite things to a program deciding whether it has an answer. The
// source is what tells them apart, and without it an eval scores a bus outage
// against the agent.
func TestInjectNamesWhoDeclaredATerminal(t *testing.T) {
	adapter, err := NewInjectAdapter("127.0.0.1:0", injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	key := injectKeyPrefix + "sources"
	adapter.TaskStarted(key, "task-executor")
	adapter.TaskTerminal(key, "task-executor", lib.StateFailed, TerminalFromExecutor, "")
	adapter.TaskStarted(key, "task-gateway")
	adapter.TaskTerminal(key, "task-gateway", lib.StateFailed, TerminalFromGateway, "")

	entries, _, _ := adapter.snapshot(key, 0, "")
	sources := map[string]string{}
	for _, entry := range entries {
		if entry.Kind == InjectEntryTerminal {
			sources[entry.TaskID] = entry.Source
		}
	}
	if sources["task-executor"] != string(TerminalFromExecutor) {
		t.Errorf("executor terminal source = %q, want %q", sources["task-executor"], TerminalFromExecutor)
	}
	if sources["task-gateway"] != string(TerminalFromGateway) {
		t.Errorf("gateway terminal source = %q, want %q", sources["task-gateway"], TerminalFromGateway)
	}
}

// TestInjectPostsTheWholeOfALongAnswer: Gateway.post chunks anything past the
// backend cap into separate posts, and a reader that keeps only the last one
// grades a long report on its closing fragment. The transport reassembles
// them, so the adapter has to record every chunk rather than collapsing them.
func TestInjectPostsTheWholeOfALongAnswer(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-long", injectTestAuthor, "write it all down")

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	// Comfortably past discordChunk, with the phrase a verifier would look
	// for in the FIRST chunk.
	answer := "Root cause: OOMKilled.\n" + strings.Repeat("detail line\n", 400)
	if err := exec.PublishArtifact(ctx, lib.Artifact{
		Name:  lib.ArtifactResult,
		Parts: []lib.Part{{Kind: "text", Text: answer}},
	}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the terminal", func() bool {
		_, _, terminal := r.adapter.snapshot(reply.Conversation, 0, reply.TaskID)
		return terminal != ""
	})

	entries, _, _ := r.adapter.snapshot(reply.Conversation, 0, "")
	edited := map[string]bool{}
	for _, entry := range entries {
		if entry.Kind == InjectEntryEdit {
			edited[entry.MessageID] = true
		}
	}
	var rebuilt strings.Builder
	chunks := 0
	for _, entry := range entries {
		if entry.Kind == InjectEntryPost && !edited[entry.MessageID] {
			rebuilt.WriteString(entry.Text)
			chunks++
		}
	}
	if chunks < 2 {
		t.Fatalf("the answer arrived in %d unedited post(s); this test only means something "+
			"when the gateway chunked it", chunks)
	}
	if rebuilt.String() != answer {
		t.Errorf("the unedited posts do not reassemble the answer: got %d bytes, want %d",
			rebuilt.Len(), len(answer))
	}
}

// TestOnlyTheInjectBackendObservesTasks: TaskObserver is an optional
// extension, and the chat backends must stay outside it. If a chat adapter
// ever implements it by accident, the gateway starts calling into it on every
// task with no test covering what it does there.
func TestOnlyTheInjectBackendObservesTasks(t *testing.T) {
	discord := &DiscordAdapter{}
	if _, ok := any(discord).(TaskObserver); ok {
		t.Error("the Discord adapter implements TaskObserver; the gateway now calls into it untested")
	}
	gchat := &GoogleChatAdapter{}
	if _, ok := any(gchat).(TaskObserver); ok {
		t.Error("the Google Chat adapter implements TaskObserver; the gateway now calls into it untested")
	}
	if _, ok := any(discord).(InboundObserver); ok {
		t.Error("the Discord adapter implements InboundObserver; the gateway now calls into it untested")
	}
	if _, ok := any(gchat).(InboundObserver); ok {
		t.Error("the Google Chat adapter implements InboundObserver; the gateway now calls into it untested")
	}
	inject := &InjectAdapter{}
	if _, ok := any(inject).(TaskObserver); !ok {
		t.Error("the inject adapter does not implement TaskObserver, so POST /inject can never return a task id")
	}
	if _, ok := any(inject).(InboundObserver); !ok {
		t.Error("the inject adapter does not implement InboundObserver, so a second drop from one author is a silence")
	}
}

// TestInjectBoundsWhatItRetains: the transcript is bounded memory on a
// long-lived pod, and the bound must not break the polling contract -- a
// reader's sequence still advances monotonically once the oldest entries have
// been dropped.
func TestInjectBoundsWhatItRetains(t *testing.T) {
	adapter, err := NewInjectAdapter("127.0.0.1:0", injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	key := injectKeyPrefix + "bounded"
	for i := 0; i < injectMaxEntries+10; i++ {
		if _, err := adapter.Post(key, fmt.Sprintf("line %d", i)); err != nil {
			t.Fatal(err)
		}
	}
	entries, lastSeq, _ := adapter.snapshot(key, 0, "")
	if len(entries) != injectMaxEntries {
		t.Fatalf("retained %d entries, want the cap %d", len(entries), injectMaxEntries)
	}
	if lastSeq != injectMaxEntries+10 {
		t.Fatalf("lastSeq = %d, want %d: the sequence must keep counting across evictions, "+
			"or a polling reader is handed a sequence it has already consumed",
			lastSeq, injectMaxEntries+10)
	}
	if entries[0].Seq <= 10 {
		t.Fatalf("the oldest retained entry is seq %d; the cap dropped the wrong end", entries[0].Seq)
	}

	for i := 0; i < injectMaxConversations+5; i++ {
		if _, err := adapter.Post(fmt.Sprintf("%sconv-%d", injectKeyPrefix, i), "x"); err != nil {
			t.Fatal(err)
		}
	}
	adapter.mu.Lock()
	held := len(adapter.conversations)
	adapter.mu.Unlock()
	if held > injectMaxConversations {
		t.Fatalf("holding %d conversations, want at most %d", held, injectMaxConversations)
	}
}

// ---- the bearer token (#1704, bnaylor's review of the design doc) ---------

// TestInjectRefusesEveryRequestWithoutTheToken is the door's own control.
//
// The NetworkPolicy in front of it does not govern the path its caller uses:
// a port-forward enters from the node, which is exempt. So without this, the
// population that can drive the platform persona -- with the install's
// cluster and GitHub credentials, past the allowed-users gate -- would be
// everyone holding pods/portforward in the namespace. Every route, because a
// door with one unauthenticated endpoint is an unauthenticated door: the GET
// alone would read every reply on every conversation.
func TestInjectRefusesEveryRequestWithoutTheToken(t *testing.T) {
	r := startInjectRig(t)
	accepted := r.inject(t, "case-auth", injectTestAuthor, "something to read back")

	body, err := json.Marshal(injectRequest{Conversation: "case-auth-2", Author: injectTestAuthor, Text: "let me in"})
	if err != nil {
		t.Fatal(err)
	}
	cancelBody, err := json.Marshal(cancelRequest{Author: injectTestAuthor})
	if err != nil {
		t.Fatal(err)
	}
	conversationURL := r.base + conversationsPath + accepted.Conversation

	cases := []struct {
		name   string
		method string
		target string
		body   []byte
		token  string
		header string
	}{
		{"submit with no token", http.MethodPost, r.base + injectPath, body, "", ""},
		{"submit with the wrong token", http.MethodPost, r.base + injectPath, body, "not-the-token", ""},
		{"read with no token", http.MethodGet, conversationURL, nil, "", ""},
		{"read with the wrong token", http.MethodGet, conversationURL, nil, "hunter2", ""},
		{"cancel with no token", http.MethodPost, conversationURL + cancelSuffix, cancelBody, "", ""},
		{"a prefix of the token", http.MethodPost, r.base + injectPath, body, injectTestToken[:4], ""},
		{"the token without the scheme", http.MethodPost, r.base + injectPath, body, "", injectTestToken},
		{"another scheme", http.MethodPost, r.base + injectPath, body, "", "Basic " + injectTestToken},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var reader io.Reader
			if tc.body != nil {
				reader = bytes.NewReader(tc.body)
			}
			req, err := http.NewRequest(tc.method, tc.target, reader)
			if err != nil {
				t.Fatal(err)
			}
			switch {
			case tc.header != "":
				req.Header.Set(authorizationHeader, tc.header)
			case tc.token != "":
				req.Header.Set(authorizationHeader, "Bearer "+tc.token)
			}
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatalf("%s %s: %v", tc.method, tc.target, err)
			}
			defer resp.Body.Close()
			if resp.StatusCode != http.StatusUnauthorized {
				t.Fatalf("%s %s returned %d, want 401", tc.method, tc.target, resp.StatusCode)
			}
			if got := resp.Header.Get("WWW-Authenticate"); got == "" {
				t.Error("a 401 with no WWW-Authenticate header")
			}
		})
	}

	// And nothing the refused requests carried reached the bus or the
	// conversation: exactly the one task the authorized submission started.
	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 1 {
		t.Fatalf("%d envelopes on the bus, want just the authorized one", len(envs))
	}
}

// TestInjectAcceptsTheTokenCaseInsensitivelyBySchemeOnly: RFC 7235 makes the
// scheme a case-insensitive token and the credential exact. Getting this
// backwards either refuses a legitimate client or accepts a wrong token.
func TestInjectAcceptsTheTokenCaseInsensitivelyBySchemeOnly(t *testing.T) {
	r := startInjectRig(t)
	resp, err := r.do(t, http.MethodGet, r.base+conversationsPath+"nothing-here?wait=0", nil, "")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("no token returned %d, want 401", resp.StatusCode)
	}

	req, err := http.NewRequest(http.MethodGet, r.base+conversationsPath+"nothing-here?wait=0", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set(authorizationHeader, "bEaReR "+injectTestToken)
	lower, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer lower.Body.Close()
	if lower.StatusCode != http.StatusOK {
		t.Fatalf("a lowercase scheme returned %d, want 200: the scheme is case-insensitive", lower.StatusCode)
	}

	req, err = http.NewRequest(http.MethodGet, r.base+conversationsPath+"nothing-here?wait=0", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set(authorizationHeader, "Bearer "+strings.ToUpper(injectTestToken))
	upper, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer upper.Body.Close()
	if upper.StatusCode != http.StatusUnauthorized {
		t.Fatalf("an upper-cased CREDENTIAL returned %d, want 401", upper.StatusCode)
	}
}

// TestInjectRefusesToArmWithoutAToken: there is no unauthenticated mode, and
// the constructor is the second place that says so (FromEnv is the first).
// An embedder or a test that could build one would make "no token" a
// configuration rather than an impossibility.
func TestInjectRefusesToArmWithoutAToken(t *testing.T) {
	if _, err := NewInjectAdapter("127.0.0.1:0", "", injectTestGrace, nil); err == nil {
		t.Fatal("the door armed with no bearer token")
	}
	if _, err := NewInjectAdapter("127.0.0.1:0", "   ", injectTestGrace, nil); err == nil {
		t.Fatal("whitespace passed as a bearer token")
	}
}

// ---- identity: the door's own prefixed section ---------------------------

// TestInjectResolvesOnlyThroughItsOwnPrefixedSection: the door takes its
// author from a request body, so what stops it minting a task as anyone is
// that the lookup is prefixed and lives in a map of its own. An entry in the
// chat backends' map is unreachable from here, and so is an unprefixed entry
// in the door's own.
func TestInjectResolvesOnlyThroughItsOwnPrefixedSection(t *testing.T) {
	dir := t.TempDir()

	chatMap := filepath.Join(dir, "chat-principal-map")
	if err := os.WriteFile(chatMap, []byte("2002 corp:real-person\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	injectMap := filepath.Join(dir, "inject-principal-map")
	fixture := "3003 eval:unprefixed-key\n" + injectPrincipalPrefix + "4004 eval:properly-mapped\n"
	if err := os.WriteFile(injectMap, []byte(fixture), 0o600); err != nil {
		t.Fatal(err)
	}

	g, err := New(Options{
		Client:  &lib.Client{},
		Adapter: newFakeAdapter(),
		Backend: injectBackend,
		Config: &Config{
			NATSURL:                "nats://127.0.0.1:4222",
			PrincipalMapPath:       chatMap,
			InjectListen:           "127.0.0.1:0",
			InjectToken:            injectTestToken,
			InjectPrincipalMapPath: injectMap,
			DefaultAddressee:       "platform",
			AttributionSalt:        []byte("test-salt"),
		},
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	if got := g.resolvePrincipal(injectBackend, "4004"); got != "eval:properly-mapped" {
		t.Fatalf("a prefixed eval entry resolved to %q", got)
	}
	if got := g.resolvePrincipal(injectBackend, "2002"); got != "" {
		t.Fatalf("the door reached the chat backends' map and resolved %q: an entry written for a "+
			"real backend's sender must be unreachable from a door that takes its author from a body", got)
	}
	if got := g.resolvePrincipal(injectBackend, "3003"); got != "" {
		t.Fatalf("an unprefixed entry in the door's own map resolved to %q; the prefix is the section", got)
	}
	// And the chat side is unchanged: its own map still answers, and the
	// door's entries are not a second source of identities for it.
	if got := g.resolvePrincipal(discordBackend, "2002"); got != "corp:real-person" {
		t.Fatalf("the chat backend's own map stopped resolving: %q", got)
	}
	if got := g.resolvePrincipal(discordBackend, "4004"); got != "" {
		t.Fatalf("a chat message resolved through the door's map and got %q", got)
	}
}

// TestInjectRefusesAPrincipalOutsideTheEvalNamespace: the second half of the
// same property. The map is the only thing between a token holder and a
// principal of their choosing, so an entry pointing at a cloud identity is
// refused rather than honoured -- a mistake in the map is a lockout, never a
// privilege.
func TestInjectRefusesAPrincipalOutsideTheEvalNamespace(t *testing.T) {
	injectMap := filepath.Join(t.TempDir(), "inject-principal-map")
	fixture := injectPrincipalPrefix + "5005 serviceAccount:agent@project.iam.gserviceaccount.com\n" +
		injectPrincipalPrefix + "6006 " + injectEvalPrincipalPrefix + "devops-bench\n"
	if err := os.WriteFile(injectMap, []byte(fixture), 0o600); err != nil {
		t.Fatal(err)
	}

	g, err := New(Options{
		Client:  &lib.Client{},
		Adapter: newFakeAdapter(),
		Backend: injectBackend,
		Config: &Config{
			NATSURL:                "nats://127.0.0.1:4222",
			InjectListen:           "127.0.0.1:0",
			InjectToken:            injectTestToken,
			InjectPrincipalMapPath: injectMap,
			DefaultAddressee:       "platform",
			AttributionSalt:        []byte("test-salt"),
		},
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if got := g.resolvePrincipal(injectBackend, "5005"); got != "" {
		t.Fatalf("the door asserted %q: a cloud principal is exactly what a door taking its author "+
			"from a request body must be structurally unable to claim", got)
	}
	if got := g.resolvePrincipal(injectBackend, "6006"); got == "" {
		t.Fatal("a conforming eval entry was refused too, so the rule is a lockout rather than a boundary")
	}
}

// ---- the five adapter operations -----------------------------------------

// TestInjectRosterIsTheRequesterAloneAndComplete: a synthetic conversation
// has one participant. Reporting it complete is the truth -- there is no
// membership API here to be incomplete about -- and reporting the requester
// rather than nothing is what makes the audience snapshot say who could have
// read the answer.
func TestInjectRosterIsTheRequesterAloneAndComplete(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-roster", injectTestAuthor, "who is here?")

	ids, complete, err := r.adapter.Roster(reply.Conversation)
	if err != nil {
		t.Fatal(err)
	}
	if !complete {
		t.Error("the roster reports itself incomplete; there is nothing here to be incomplete about")
	}
	if len(ids) != 1 || ids[0] != injectTestAuthor {
		t.Fatalf("roster = %v, want the requester alone", ids)
	}
}

// TestInjectOpenDirectReturnsTheSameConversation: the DM switch has nowhere
// to switch to on this door -- every conversation already has exactly one
// participant. Minting a second key would strand a reply on a conversation
// its caller never polls.
func TestInjectOpenDirectReturnsTheSameConversation(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-direct", injectTestAuthor, "talk to me")

	direct, err := r.adapter.OpenDirect(injectTestAuthor)
	if err != nil {
		t.Fatal(err)
	}
	if direct != reply.Conversation {
		t.Fatalf("OpenDirect = %q, want the conversation the user is in (%q)", direct, reply.Conversation)
	}
	// A user the door has not seen still gets an answer rather than an error.
	unseen, err := r.adapter.OpenDirect("7007")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(unseen, injectKeyPrefix) {
		t.Fatalf("OpenDirect for an unseen user = %q, want a qualified key", unseen)
	}
}

// TestInjectImplementsAllFiveAdapterOperations: the test-backend section
// names five, and a door implementing four leaves the session manager to
// special-case the fifth. A compile-time assertion for the interface, and a
// live call to each so that a method that exists and panics is caught too.
func TestInjectImplementsAllFiveAdapterOperations(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-five", injectTestAuthor, "one")

	messageID, err := r.adapter.Post(reply.Conversation, "two")
	if err != nil || messageID == "" {
		t.Fatalf("Post: %q %v", messageID, err)
	}
	if err := r.adapter.Edit(reply.Conversation, messageID, "three"); err != nil {
		t.Fatalf("Edit: %v", err)
	}
	if _, _, err := r.adapter.Roster(reply.Conversation); err != nil {
		t.Fatalf("Roster: %v", err)
	}
	if _, err := r.adapter.OpenDirect(injectTestAuthor); err != nil {
		t.Fatalf("OpenDirect: %v", err)
	}
	// Run is the fifth, and the rig is already inside it: the submission
	// above went through the handler it installed.
}

// ---- cancel, and the window that is the gateway's -------------------------

// TestInjectCancelRouteLandsAsKindCancel: the explicit route, not the stop
// text. A program should not have to spell a phrase to reach a control path,
// and what goes on the bus must be identical to what the text route puts
// there -- the executor cannot tell which door the cancel came through.
func TestInjectCancelRouteLandsAsKindCancel(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-cancel", injectTestAuthor, "long job")
	origin := r.awaitTask(t, "platform")

	// The executor says something, so the task is not a never-started one:
	// this test is about the cancel, not about the heal.
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}

	out := r.cancel(t, reply.Conversation, injectTestAuthor)
	if out.TaskID != "" || out.Accepted {
		t.Fatalf("a cancel started a task: %+v", out)
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
	if cancelEnv.TaskID != reply.TaskID {
		t.Fatalf("the cancel names task %q, want %q", cancelEnv.TaskID, reply.TaskID)
	}
	if len(cancelEnv.Authority) == 0 {
		t.Error("the cancel carries no authority block; it is an authority-bearing action like any other")
	}
	// The conversation is told, the way it is told when a human types stop.
	// Read from the conversation rather than from the cancel's own reply:
	// the reply carries what had landed when the door answered, and the
	// relay's rolling-line edit races the acknowledgement post.
	r.waitForPost(t, out.Conversation, "cancel sent")
}

// waitForPost blocks until some post on a conversation contains want.
func (r *injectRig) waitForPost(t *testing.T, key, want string) {
	t.Helper()
	waitFor(t, fmt.Sprintf("a post containing %q on %s", want, key), func() bool {
		entries, _, _ := r.adapter.snapshot(key, 0, "")
		return strings.Contains(strings.Join(entryTexts(entries, InjectEntryPost), "\n"), want)
	})
}

// waitForTerminal blocks until a task has a terminal entry, and returns it.
func (r *injectRig) waitForTerminal(t *testing.T, key, taskID string) InjectEntry {
	t.Helper()
	var found InjectEntry
	waitFor(t, "a terminal entry for "+taskID, func() bool {
		entries, _, _ := r.adapter.snapshot(key, 0, "")
		for _, entry := range terminalEntries(entries) {
			if entry.TaskID == taskID {
				found = entry
				return true
			}
		}
		return false
	})
	return found
}

// TestInjectCancelDoesNotGoThroughTheStopText: the same call must not depend
// on the phrase list. If the route were implemented by sending "stop", a
// change to stopWords would silently break the harness -- and an ask that
// happened to be the word "stop" would be indistinguishable from a control
// action.
func TestInjectCancelDoesNotGoThroughTheStopText(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-cancel-text", injectTestAuthor, "long job")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}

	r.cancel(t, reply.Conversation, injectTestAuthor)

	waitFor(t, "the cancel envelope", func() bool {
		for _, env := range inSubjectEnvelopes(t, r.url, "platform") {
			if env.Kind == lib.KindCancel {
				return true
			}
		}
		return false
	})
	// No message envelope carrying the stop phrase: the route delivered an
	// intent, and nothing was classified out of text.
	for _, env := range inSubjectEnvelopes(t, r.url, "platform") {
		if env.Kind != lib.KindMessage {
			continue
		}
		var message lib.Message
		if err := json.Unmarshal(env.Payload, &message); err != nil {
			continue
		}
		if isStop(joinTextParts(message.Parts)) {
			t.Fatalf("the cancel route sent the stop text as a message: %q", joinTextParts(message.Parts))
		}
	}
}

// TestInjectReportsTheGatewaysFirstEventGrace: the caller must not run a
// clock of its own for "nobody took this task". The gateway owns that window
// (A2A_FIRST_EVENT_GRACE and the never-started heal), so the submission
// reports it and the caller sets its deadline above it. A harness that
// guessed would cancel inside the grace, where the cancel reaches no
// executor and is answered by nothing.
func TestInjectReportsTheGatewaysFirstEventGrace(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-grace", injectTestAuthor, "how long have I got?")
	if reply.FirstEventGraceSeconds != int(injectTestGrace/time.Second) {
		t.Fatalf("firstEventGraceSeconds = %d, want %d",
			reply.FirstEventGraceSeconds, int(injectTestGrace/time.Second))
	}
}

// TestInjectReadRouteIsAPureRead: the read reports the facts the
// never-started heal decides on -- an active task, nothing on its stream, an
// age past the grace -- and changes nothing: no heal, no post, no publish, no
// write. The heal is a write under the conversation's lock inside the keyed
// queue, and a read that also wrote would be a second writer racing the next
// inbound message. The classification is the caller's; the gateway's own
// verdict still lands on the next turn, which the second half shows.
func TestInjectReadRouteIsAPureRead(t *testing.T) {
	r := startInjectRig(t)
	// Nothing ever consumes this addressee, which is an install with no
	// executor -- the state an eval install has before its bridge is
	// declared.
	reply := r.inject(t, "case-never", injectTestAuthor, "is anybody there?")
	if reply.TaskID == "" {
		t.Fatal("no task to abandon")
	}
	r.awaitRecordedTask(t, reply.Conversation)
	// Age the submission past the grace rather than waiting it out: a test
	// that slept would be a 90-second test.
	ageActiveTask(t, r, reply.Conversation, -2*injectTestGrace)

	page := r.probe(t, reply.Conversation, reply.TaskID)
	probe := page.Probe
	if !probe.Active || probe.TaskID != reply.TaskID || probe.ExecutorState != "" || probe.Final {
		t.Fatalf("probe = %+v, want the active task %s with no executor state", probe, reply.TaskID)
	}
	if time.Duration(probe.AgeSeconds)*time.Second <= injectTestGrace ||
		probe.GraceSeconds != int(injectTestGrace/time.Second) {
		t.Fatalf("age %ds against grace %ds: the caller cannot classify from that", probe.AgeSeconds, probe.GraceSeconds)
	}
	if probe.SubmittedAt == "" {
		t.Fatal("the read carries no submittedAt; the caller's own clock would have to stand in")
	}
	if probe.Error != "" {
		t.Fatalf("a read that could look reported an error: %s", probe.Error)
	}
	// Nothing changed. No terminal in the transcript, no notice posted, no
	// envelope beyond the submission on the in subject, one start, and the
	// record still holds the task.
	if len(terminalEntries(page.Entries)) != 0 || page.Terminal != "" {
		t.Fatalf("a read produced a terminal: %+v", page)
	}
	if got := entryTexts(page.Entries, InjectEntryPost); len(got) != 1 {
		t.Fatalf("posts after a read = %q, want the placeholder alone", got)
	}
	if n := len(inSubjectEnvelopes(t, r.url, "platform")); n != 1 {
		t.Fatalf("%d envelopes on the in subject after a read, want the one submission", n)
	}
	if starts := r.adapter.counts(reply.Conversation).starts; starts != 1 {
		t.Fatalf("%d tasks started on the conversation after a read, want 1", starts)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	rec, err := r.g.reg.Get(ctx, reply.Conversation)
	if err != nil || rec == nil || rec.ActiveTask == nil || rec.ActiveTask.TaskID != reply.TaskID {
		t.Fatalf("the read released the record (rec=%+v err=%v); the heal is the turn's, not the read's", rec, err)
	}
	// A second read says the same: reads are idempotent because they write
	// nothing.
	if again := r.probe(t, reply.Conversation, reply.TaskID).Probe; !again.Active || again.ExecutorState != "" {
		t.Fatalf("second read = %+v, want the same active task", again)
	}

	// The heal still exists, where it always was: the next turn on the
	// conversation releases the task and tells the door so.
	second := r.inject(t, "case-never", injectTestAuthor, "still there?")
	r.waitForPost(t, reply.Conversation, "produced nothing on its event stream")
	terminal := r.waitForTerminal(t, reply.Conversation, reply.TaskID)
	if terminal.Source != string(TerminalNeverStarted) || terminal.State != string(lib.StateFailed) {
		t.Fatalf("the turn's heal recorded %+v, want a failed terminal from %s", terminal, TerminalNeverStarted)
	}
	if second.TaskID == "" || second.TaskID == reply.TaskID {
		t.Fatalf("the turn after the heal started %q, want a fresh task", second.TaskID)
	}
}

// TestInjectReadRouteReportsTheExecutorsState: the same read, in the order a
// task passes through it. Before any event the stream holds nothing; once an
// executor publishes, the read carries its latest state; at the terminal it
// carries the state and final, and after the relay releases the record the
// task is no longer active and the terminal sits beside the read. None of
// the reads reaches the bus or starts anything.
func TestInjectReadRouteReportsTheExecutorsState(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-read", injectTestAuthor, "take your time")
	r.awaitRecordedTask(t, reply.Conversation)

	waiting := r.probe(t, reply.Conversation, reply.TaskID).Probe
	if !waiting.Active || waiting.TaskID != reply.TaskID || waiting.ExecutorState != "" {
		t.Fatalf("before any event: probe = %+v, want active with no executor state", waiting)
	}
	if waiting.AgeSeconds < 0 || time.Duration(waiting.AgeSeconds)*time.Second > injectTestGrace {
		t.Fatalf("age = %ds, want inside the grace", waiting.AgeSeconds)
	}
	if waiting.Detached {
		t.Fatal("a task nothing cancelled reads as detached")
	}
	if waiting.ReachedWorking {
		t.Fatal("a stream with no events reads as having reached working")
	}

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the read route to see the executor's submitted event", func() bool {
		return r.probe(t, reply.Conversation, reply.TaskID).Probe.ExecutorState == string(lib.StateSubmitted)
	})
	if queued := r.probe(t, reply.Conversation, reply.TaskID).Probe; queued.ReachedWorking {
		t.Fatalf("queued probe = %+v, want reachedWorking false at submitted", queued)
	}
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the read route to see the executor working", func() bool {
		return r.probe(t, reply.Conversation, reply.TaskID).Probe.ExecutorState == string(lib.StateWorking)
	})
	running := r.probe(t, reply.Conversation, reply.TaskID).Probe
	if running.Final || !running.Active {
		t.Fatalf("running probe = %+v, want active and not final", running)
	}
	if !running.ReachedWorking {
		t.Fatalf("running probe = %+v, want reachedWorking", running)
	}
	if running.LastPost == nil || running.LastPost.Kind != InjectEntryPost {
		t.Fatalf("running probe carries no last post: %+v", running)
	}

	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	r.waitForTerminal(t, reply.Conversation, reply.TaskID)
	waitFor(t, "the relay to release the finished task", func() bool {
		return !r.probe(t, reply.Conversation, reply.TaskID).Probe.Active
	})
	done := r.probe(t, reply.Conversation, reply.TaskID)
	if done.Terminal != string(lib.StateCompleted) {
		t.Fatalf("released read carries terminal %q, want completed beside it", done.Terminal)
	}
	if done.Probe.Backend != injectBackend || !done.Probe.InjectOnly {
		t.Fatalf("probe names backend %q injectOnly %v; this rig runs on the door alone", done.Probe.Backend, done.Probe.InjectOnly)
	}

	// Many reads, one task: the in subject holds the submission and nothing
	// else, and every read left the door's start count where the POST put it.
	for _, env := range inSubjectEnvelopes(t, r.url, "platform") {
		if env.Kind != lib.KindMessage {
			t.Fatalf("a read published a %s envelope", env.Kind)
		}
	}
	if starts := r.adapter.counts(reply.Conversation).starts; starts != 1 {
		t.Fatalf("%d tasks started, want 1: a read minted a task", starts)
	}
}

// TestInjectReadRouteReportsATerminalOnTheStreamBeforeTheRelay: an executor's
// terminal reaches the stream a moment before the relay posts it, and a read
// at that instant reports final without touching the record -- the relay is
// the one that releases it.
func TestInjectReadRouteReportsATerminalOnTheStreamBeforeTheRelay(t *testing.T) {
	door, err := NewInjectAdapter("127.0.0.1:0", injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	door.SetProbe(func(context.Context, string) (ConversationState, error) {
		return ConversationState{Active: true, TaskID: "task-1", ExecutorState: lib.StateFailed, Final: true, Grace: injectTestGrace}, nil
	})
	report := door.runProbe(context.Background(), injectKeyPrefix+"any")
	if !report.Active || !report.Final || report.ExecutorState != string(lib.StateFailed) {
		t.Fatalf("report = %+v, want an active task whose stream is final", report)
	}
}

// TestInjectReadRouteReportsADetachedTask: after the cancel route fires the
// record is detached, and the read says so rather than leaving the caller to
// send a second cancel.
func TestInjectReadRouteReportsADetachedTask(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-stopping", injectTestAuthor, "long job")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	r.cancel(t, reply.Conversation, injectTestAuthor)
	r.waitForPost(t, reply.Conversation, "cancel sent")
	// cancelTask detaches in memory and posts; the record is written at the
	// end of the turn, and the probe reads Detached from the record.
	r.awaitTurnEnd(t, reply.Conversation)

	probe := r.probe(t, reply.Conversation, reply.TaskID).Probe
	if !probe.Active || !probe.Detached || probe.TaskID != reply.TaskID {
		t.Fatalf("after a cancel: probe = %+v, want the task active and detached", probe)
	}
	if probe.ExecutorState != string(lib.StateWorking) {
		t.Fatalf("a detached task's stream state = %q, want working", probe.ExecutorState)
	}
}

// TestInjectReadRouteSaysWhenItCannotLook: a door run outside a gateway has
// no probe, and a probe whose stream read failed says so in the report's
// error -- a caller deciding whether anything executed must not read the
// absence of a probe, or an empty executor state beside an error, as "no
// executor".
func TestInjectReadRouteSaysWhenItCannotLook(t *testing.T) {
	door, err := NewInjectAdapter("127.0.0.1:0", injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	report := door.runProbe(context.Background(), injectKeyPrefix+"orphan")
	if report.Error == "" || report.Active {
		t.Fatalf("probe with no gateway = %+v, want an error and nothing asserted", report)
	}
	failing := func(context.Context, string) (ConversationState, error) {
		return ConversationState{Active: true, TaskID: "task-1"}, fmt.Errorf("the stream is unreachable")
	}
	door.SetProbe(failing)
	report = door.runProbe(context.Background(), injectKeyPrefix+"orphan")
	if !strings.Contains(report.Error, "unreachable") || !report.Active || report.TaskID != "task-1" {
		t.Fatalf("probe that failed to look = %+v, want the error beside what was learned", report)
	}
}

// TestInjectReadRouteAnswersForAnUnknownConversation: a read on a key
// nothing has been injected into is the caller's preflight -- it learns the
// grace and whether the gateway runs on the door alone before it starts
// anything, and starts nothing by asking.
func TestInjectReadRouteAnswersForAnUnknownConversation(t *testing.T) {
	r := startInjectRig(t)
	page := r.probe(t, injectKeyPrefix+"never-used", "")
	if page.Probe.Active || page.Probe.TaskID != "" {
		t.Fatalf("an unknown conversation reads as active: %+v", page.Probe)
	}
	if page.Probe.GraceSeconds != int(injectTestGrace/time.Second) || !page.Probe.InjectOnly {
		t.Fatalf("preflight = %+v, want the grace and inject-only", page.Probe)
	}
	if _, ok := r.adapter.conversations[injectKeyPrefix+"never-used"]; ok {
		t.Fatal("a read minted a conversation")
	}
}

// TestInjectDedupesAPostOnItsMessageID: a harness retries an opening POST
// whose connection dropped with the same body, and by then startTask has
// written the active task, so an undeduped retry is routed as a steer on
// the running task and the harness awaits a task id it never learns. The
// backend message id says it is the same message: the retry is answered with
// the first task id, and nothing is routed for it.
func TestInjectDedupesAPostOnItsMessageID(t *testing.T) {
	r := startInjectRig(t)
	const messageID = "run-1/case-dedupe/1"
	first := r.injectWithID(t, "case-dedupe", injectTestAuthor, "how is the fleet?", messageID)
	if first.TaskID == "" || first.Deduplicated {
		t.Fatalf("first POST: %+v", first)
	}
	retry := r.injectWithID(t, "case-dedupe", injectTestAuthor, "how is the fleet?", messageID)
	if retry.TaskID != first.TaskID || !retry.Accepted || !retry.Deduplicated {
		t.Fatalf("retry = %+v, want the first task id %s marked deduplicated", retry, first.TaskID)
	}
	if starts := r.adapter.counts(first.Conversation).starts; starts != 1 {
		t.Fatalf("%d tasks started, want 1", starts)
	}
	if n := len(inSubjectEnvelopes(t, r.url, "platform")); n != 1 {
		t.Fatalf("%d envelopes on the in subject, want the one submission: the retry was routed", n)
	}
	// A different id on the same conversation is a new message, and on a
	// conversation whose task is still running it is a steer -- which is
	// exactly what the dedupe keeps a retry from becoming.
	other := r.injectWithID(t, "case-dedupe", injectTestAuthor, "and the nodes?", "run-1/case-dedupe/2")
	if other.Deduplicated || other.TaskID != "" {
		t.Fatalf("a second message = %+v, want routed (as a steer) and not deduplicated", other)
	}

	// The same id on another conversation is a caller bug, refused rather
	// than answered with a task on a conversation it did not name.
	body, _ := json.Marshal(injectRequest{Conversation: "case-elsewhere", Author: injectTestAuthor, Text: "x", MessageID: messageID})
	resp, err := r.do(t, http.MethodPost, r.base+injectPath, body, injectTestToken)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("same id on another conversation = %d, want 409", resp.StatusCode)
	}
}

// TestInjectDedupesAPostStillInFlight: the retry that matters most arrives
// while the first POST's turn is still running -- the client's connection
// dropped, the server's handler did not. The duplicate waits on the
// original's answer instead of routing a second message.
func TestInjectDedupesAPostStillInFlight(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	door, err := NewInjectAdapter(ln.Addr().String(), injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	door.listener = ln
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	entered := make(chan struct{})
	release := make(chan struct{})
	var turns int
	handler := func(msg InboundMessage) {
		turns++
		close(entered)
		<-release
		door.TaskStarted(msg.Conversation, "task-first")
		door.TaskAccepted(msg.Conversation, "task-first")
	}
	go func() { _ = door.Run(ctx, handler) }()
	waitFor(t, "the door to accept messages", func() bool {
		door.handlerMu.RLock()
		defer door.handlerMu.RUnlock()
		return door.handler != nil
	})

	post := func() injectResponse {
		body, _ := json.Marshal(injectRequest{Conversation: "in-flight", Author: injectTestAuthor, Text: "go", MessageID: "run/in-flight/1"})
		req, _ := http.NewRequest(http.MethodPost, "http://"+ln.Addr().String()+injectPath, bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set(authorizationHeader, "Bearer "+injectTestToken)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Error(err)
			return injectResponse{}
		}
		defer resp.Body.Close()
		var out injectResponse
		_ = json.NewDecoder(resp.Body).Decode(&out)
		return out
	}
	replies := make(chan injectResponse, 2)
	go func() { replies <- post() }()
	<-entered
	go func() { replies <- post() }()
	// The duplicate is waiting on the first turn, not running one of its own.
	time.Sleep(100 * time.Millisecond)
	if turns != 1 {
		t.Fatalf("%d turns ran with the first still in flight, want 1", turns)
	}
	close(release)
	got := []injectResponse{<-replies, <-replies}
	var deduped int
	for _, reply := range got {
		if reply.TaskID != "task-first" {
			t.Fatalf("reply = %+v, want task-first", reply)
		}
		if reply.Deduplicated {
			deduped++
		}
	}
	if deduped != 1 || turns != 1 {
		t.Fatalf("%d replies marked deduplicated across %d turns, want exactly one of each", deduped, turns)
	}
}

// TestInjectDedupesAPostWhoseCallerDisconnected: the case the dedupe exists
// for. The first client's connection drops while its turn is still running;
// the turn goes on and starts a task; the retry with the same id must be
// answered with that task, not with "the caller went away".
func TestInjectDedupesAPostWhoseCallerDisconnected(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	door, err := NewInjectAdapter(ln.Addr().String(), injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	door.listener = ln
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	entered := make(chan struct{})
	release := make(chan struct{})
	var turns int
	handler := func(msg InboundMessage) {
		turns++
		close(entered)
		// The gateway's handler only enqueues; the turn runs after it
		// returns. Model that: start the task once released, off this call.
		go func() {
			<-release
			door.TaskStarted(msg.Conversation, "task-first")
			door.TaskAccepted(msg.Conversation, "task-first")
		}()
	}
	go func() { _ = door.Run(ctx, handler) }()
	waitFor(t, "the door to accept messages", func() bool {
		door.handlerMu.RLock()
		defer door.handlerMu.RUnlock()
		return door.handler != nil
	})

	body, _ := json.Marshal(injectRequest{Conversation: "dropped", Author: injectTestAuthor, Text: "go", MessageID: "run/dropped/1"})
	post := func(reqCtx context.Context) (injectResponse, error) {
		req, _ := http.NewRequestWithContext(reqCtx, http.MethodPost, "http://"+ln.Addr().String()+injectPath, bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set(authorizationHeader, "Bearer "+injectTestToken)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return injectResponse{}, err
		}
		defer resp.Body.Close()
		var out injectResponse
		return out, json.NewDecoder(resp.Body).Decode(&out)
	}

	// The first client drops mid-turn.
	firstCtx, drop := context.WithCancel(context.Background())
	firstDone := make(chan error, 1)
	go func() { _, err := post(firstCtx); firstDone <- err }()
	<-entered
	drop()
	if err := <-firstDone; err == nil {
		t.Fatal("the first client was meant to drop its connection")
	}
	// The turn finishes after the client is gone.
	close(release)

	retry, err := post(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if retry.TaskID != "task-first" || !retry.Deduplicated || !retry.Accepted {
		t.Fatalf("retry after a dropped connection = %+v, want task-first, deduplicated", retry)
	}
	if turns != 1 {
		t.Fatalf("%d turns ran, want 1: the retry was routed", turns)
	}
}

// TestInjectTerminalCarriesTheExecutorsReason: the bridge and the worker
// adapter write why a task failed as the terminal's status message
// (`reason: <token> - detail`), and a caller classifying a failed terminal
// -- the persona's failure, or the executor's own -- needs the token. The
// terminal entry carries the message verbatim.
func TestInjectTerminalCarriesTheExecutorsReason(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-reason", injectTestAuthor, "break")
	origin := r.awaitTask(t, "platform")
	from := lib.Party{Session: "platform", AgentType: "test-executor"}
	const reason = "reason: hermes-exited-nonzero - exit status 1; stderr tail: boom"
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID:    origin.TaskID,
		ContextID: origin.ContextID,
		Status: lib.TaskStatus{
			State: lib.StateFailed,
			Message: &lib.Message{
				Role: "agent", MessageID: "msg-reason", TaskID: origin.TaskID, ContextID: origin.ContextID,
				Parts: []lib.Part{{Kind: "text", Text: reason}},
			},
		},
		Final: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(from, origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(context.Background(), lib.TaskEventsSubject("platform", origin.TaskID), env); err != nil {
		t.Fatal(err)
	}
	terminal := r.waitForTerminal(t, reply.Conversation, reply.TaskID)
	if terminal.State != string(lib.StateFailed) || terminal.Source != string(TerminalFromExecutor) {
		t.Fatalf("terminal = %+v", terminal)
	}
	if terminal.Reason != reason {
		t.Fatalf("terminal reason = %q, want the executor's message verbatim", terminal.Reason)
	}
	// And a terminal that carried no message carries no reason, rather than
	// an invented one.
	door, err := NewInjectAdapter("127.0.0.1:0", injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	door.TaskTerminal(injectKeyPrefix+"bare", "task-bare", lib.StateCompleted, TerminalFromExecutor, "")
	entries, _, _ := door.snapshot(injectKeyPrefix+"bare", 0, "")
	if got := terminalEntries(entries); len(got) != 1 || got[0].Reason != "" {
		t.Fatalf("bare terminal = %+v", got)
	}
}

// ageActiveTask backdates a conversation's active task, so a test can reach
// the never-started heal without waiting out the grace.
func ageActiveTask(t *testing.T, r *injectRig, conversation string, by time.Duration) {
	t.Helper()
	g := r.g
	r.awaitTurnEnd(t, conversation)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	rec, err := g.reg.Get(ctx, conversation)
	if err != nil || rec == nil || rec.ActiveTask == nil {
		t.Fatalf("no active task on %s to age (rec=%v err=%v)", conversation, rec, err)
	}
	rec.ActiveTask.SubmittedAt = rec.ActiveTask.SubmittedAt.Add(by)
	if err := g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
}

// terminalEntries is every terminal entry in a page.
func terminalEntries(entries []InjectEntry) []InjectEntry {
	var out []InjectEntry
	for _, entry := range entries {
		if entry.Kind == InjectEntryTerminal {
			out = append(out, entry)
		}
	}
	return out
}

// restoreActiveTask rewrites the session record so it holds taskID as active
// again: the shape the relay leaves behind when it acked the terminal and
// then lost the record write (or the pod restarted between the two).
func (r *injectRig) restoreActiveTask(t *testing.T, key string, origin *lib.Envelope, age time.Duration) {
	t.Helper()
	r.awaitTurnEnd(t, key)
	if _, _, terminal := r.adapter.snapshot(key, 0, origin.TaskID); terminal != "" {
		// The door has the terminal, so the relay's release of the record
		// is in flight behind it; a task nobody took has no release coming.
		r.awaitRecordRelease(t, key)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	rec, err := r.g.reg.Get(ctx, key)
	if err != nil || rec == nil {
		t.Fatalf("record for %s: %v", key, err)
	}
	rec.ActiveTask = &ActiveTask{TaskID: origin.TaskID, CorrelationID: origin.CorrelationID,
		SubmittedAt: time.Now().Add(-age)}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
}

// TestInjectReadRouteCarriesTheFoldOfAFinishedTask: the relay acks a terminal
// before it clears ActiveTask and writes the record, so a restart or a failed
// write leaves an active record on a finished task -- and on a key that is
// never reused, no heal ever arrives. The read route therefore returns the
// fold of the task's stream: the terminal, whose word it is, the result text
// and the reason. A caller grades from it and sends no cancel.
func TestInjectReadRouteCarriesTheFoldOfAFinishedTask(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-lost-write", injectTestAuthor, "answer me")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{ArtifactID: "r", Name: lib.ArtifactResult,
		Parts: []lib.Part{{Kind: "text", Text: "the answer"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	r.waitForTerminal(t, reply.Conversation, reply.TaskID)

	// The lost record write.
	r.restoreActiveTask(t, reply.Conversation, origin, time.Minute)

	probe := r.probe(t, reply.Conversation, reply.TaskID).Probe
	if !probe.Active || probe.TaskID != reply.TaskID || !probe.Final {
		t.Fatalf("probe = %+v, want the record active on a task whose stream is final", probe)
	}
	if probe.ExecutorState != string(lib.StateCompleted) || probe.TerminalSource != string(TerminalFromExecutor) {
		t.Fatalf("probe = %+v, want the executor's completed terminal", probe)
	}
	if probe.Result != "the answer" {
		t.Fatalf("probe.Result = %q, want the result artifact's text", probe.Result)
	}
	// Still a pure read: the record is exactly as it was left.
	rec, err := r.g.reg.Get(ctx, reply.Conversation)
	if err != nil || rec == nil || rec.ActiveTask == nil || rec.ActiveTask.TaskID != reply.TaskID {
		t.Fatalf("the read route changed the record: %+v (err %v)", rec, err)
	}
}

// TestInjectReadRouteNamesTheSupervisorsTerminalAsItsOwn: a terminal on the
// supervisor subject is the supervisor's word about an executor that died or
// never ran, not an executor's answer. The fold says whose it is, so a caller
// grading off the read does not score an outage as the agent's failure.
func TestInjectReadRouteNamesTheSupervisorsTerminalAsItsOwn(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-supervised", injectTestAuthor, "answer me")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	const reason = "reason: worker-evicted"
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID: origin.TaskID, ContextID: origin.ContextID,
		Status: lib.TaskStatus{State: lib.StateFailed, Message: &lib.Message{
			Role: "agent", MessageID: "msg-sup", Parts: []lib.Part{{Kind: "text", Text: reason}},
		}},
		Final: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(gatewayParty, origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(ctx, lib.TaskSupervisorSubject("platform", origin.TaskID), env); err != nil {
		t.Fatal(err)
	}
	relayed := r.waitForTerminal(t, reply.Conversation, reply.TaskID)
	if relayed.Source != string(TerminalFromSupervisor) {
		t.Fatalf("the relay's terminal entry source = %q, want the supervisor's own source", relayed.Source)
	}
	r.restoreActiveTask(t, reply.Conversation, origin, time.Minute)

	probe := r.probe(t, reply.Conversation, reply.TaskID).Probe
	if !probe.Final || probe.ExecutorState != string(lib.StateFailed) {
		t.Fatalf("probe = %+v, want the failed terminal", probe)
	}
	if probe.TerminalSource != string(TerminalFromSupervisor) {
		t.Fatalf("terminalSource = %q, want the supervisor's own source", probe.TerminalSource)
	}
	if probe.Reason != reason {
		t.Fatalf("probe.Reason = %q, want the terminal's message verbatim", probe.Reason)
	}
}

// TestInjectRelayAndReadRouteAgreeOnWhoseTerminalItIs: one terminal, three
// paths to the door -- the live relay, the heal, and the read route -- and
// the same attribution on each, because a program grading off the door
// folds whichever arrives first and keeps that source. The relay's durable
// spans the events and the supervisor subject and the envelope carries no
// subject, so the relay used to label every terminal the executor's while
// the other two labelled by subject; the subject now rides with the envelope
// to the terminal. Checked for an executor's terminal off `…events` and the
// supervisor's off `…supervisor`.
func TestInjectRelayAndReadRouteAgreeOnWhoseTerminalItIs(t *testing.T) {
	cases := []struct {
		name    string
		from    lib.Party
		subject func(addressee, taskID string) string
		want    TerminalSource
	}{
		{"executor", lib.Party{Session: "platform", AgentType: "test-executor"}, lib.TaskEventsSubject, TerminalFromExecutor},
		{"supervisor", gatewayParty, lib.TaskSupervisorSubject, TerminalFromSupervisor},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := startInjectRig(t)
			reply := r.inject(t, "case-whose-"+tc.name, injectTestAuthor, "answer me")
			origin := r.awaitTask(t, "platform")
			ctx := context.Background()
			if err := r.execFor(t, origin, "platform").PublishStatus(ctx, lib.StateWorking, false); err != nil {
				t.Fatal(err)
			}
			const reason = "reason: worker-evicted - SIGTERM"
			payload, err := json.Marshal(lib.StatusUpdate{
				TaskID: origin.TaskID, ContextID: origin.ContextID,
				Status: lib.TaskStatus{State: lib.StateFailed, Message: &lib.Message{
					Role: "agent", MessageID: "msg-whose", Parts: []lib.Part{{Kind: "text", Text: reason}},
				}},
				Final: true,
			})
			if err != nil {
				t.Fatal(err)
			}
			env, err := lib.NewStatusUpdateEnvelope(tc.from, origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
			if err != nil {
				t.Fatal(err)
			}
			if err := r.bus.Publish(ctx, tc.subject("platform", origin.TaskID), env); err != nil {
				t.Fatal(err)
			}

			// The live relay's delivery.
			relayed := r.waitForTerminal(t, reply.Conversation, reply.TaskID)
			if relayed.State != string(lib.StateFailed) || relayed.Source != string(tc.want) || relayed.Reason != reason {
				t.Fatalf("the relay's terminal entry = %+v, want failed from %q with the reason verbatim", relayed, tc.want)
			}
			// The read route's fold of the same terminal.
			r.restoreActiveTask(t, reply.Conversation, origin, time.Minute)
			probe := r.probe(t, reply.Conversation, reply.TaskID).Probe
			if !probe.Final || probe.TerminalSource != string(tc.want) || probe.Reason != reason {
				t.Fatalf("the read route's probe = %+v, want the same terminal from %q", probe, tc.want)
			}
		})
	}
}

// TestInjectTheTurnClockStartsBeforeTheSessionLock: the door hands a
// message over only with a whole turnTimeout of its submit bound left, and
// reads a bound that expires with the turn unfinished as the gateway being
// stuck -- a refusal that has to mean nothing started. That holds only if
// the turn's clock starts at hand-over and not once the conversation's
// session lock is acquired: the relay, the reap and the sweep each hold that
// lock for up to a turn of their own, and a turn that waited out a lock-hold
// and then began its own turnTimeout would outlive the door's bound and
// start a task after the door had told its caller it did not. So the clock
// runs through the lock wait, and a turn whose clock ran out while it waited
// does nothing once it has the lock. A chat turn keeps the other order
// (the lock, then a whole turn), because its caller is a person for whom a
// late answer beats none; handleInbound chooses by backend. Driven through
// handleInbound itself, which is where the ordering lives, with the
// gateway's turn budget shortened so the test does not wait a real
// turnTimeout; the relay standing in for the lock-holder is the test itself,
// and it holds the lock for four whole budgets, so a turn goroutine that
// was slow to be scheduled under load has still run out of clock. With the door branch
// reordered to lock-then-mint, the turn runs a fresh budget after the
// release and its submission reaches the bus, which is the failure.
func TestInjectTheTurnClockStartsBeforeTheSessionLock(t *testing.T) {
	r := startInjectRig(t)
	key := injectKeyPrefix + "held-lock"
	msg := InboundMessage{
		Conversation: key,
		Kind:         injectConversationKind,
		AuthorID:     injectTestAuthor,
		MessageID:    "held-lock-1",
		Text:         "how is the fleet?",
		Backend:      injectBackend,
	}
	const budget = 500 * time.Millisecond
	r.g.turnBudget = budget
	l := r.g.lockSession(key)
	l.Lock()

	done := make(chan struct{})
	go func() {
		defer close(done)
		r.g.handleInbound(msg)
	}()
	select {
	case <-done:
		t.Fatal("the turn returned while the session lock was still held")
	case <-time.After(4 * budget):
	}
	l.Unlock()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the turn did not return once the lock was released")
	}

	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 0 {
		t.Fatalf("a turn whose clock ran out waiting for the lock reached the bus: %d envelopes", len(envs))
	}
	page := r.probe(t, key, "")
	if page.Probe.Active || page.Probe.TaskID != "" || len(page.Entries) != 0 {
		t.Fatalf("a turn whose clock ran out waiting for the lock started something: %+v, %d entries",
			page.Probe, len(page.Entries))
	}
}

// TestInjectCancelNamesTheTaskAfterTheHealReleasedIt: the production sequence
// for a task nobody took. The harness classifies it from the read, past the
// grace, and then cancels naming the task id its POST was answered with. The
// cancel turn runs the never-started heal first, which releases the record --
// so a cancel routed on the active task alone would stop nothing, while the
// submission is still on the in subject for a bridge that binds later. The
// named cancel reaches the bus regardless, on the task's own chain.
func TestInjectCancelNamesTheTaskAfterTheHealReleasedIt(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-nobody", injectTestAuthor, "into the void")
	origin := r.awaitTask(t, "platform")
	r.awaitRecordedTask(t, reply.Conversation)
	// No executor touches it, and it ages past the grace.
	r.restoreActiveTask(t, reply.Conversation, origin, injectTestGrace+time.Minute)

	out := r.cancelTask(t, reply.Conversation, injectTestAuthor, reply.TaskID)
	if out.TaskID != "" {
		t.Fatalf("a cancel started a task: %+v", out)
	}
	var cancelEnv *lib.Envelope
	waitFor(t, "the cancel envelope for the released task", func() bool {
		for _, env := range inSubjectEnvelopes(t, r.url, "platform") {
			if env.Kind == lib.KindCancel && env.TaskID == reply.TaskID {
				cancelEnv = env
				return true
			}
		}
		return false
	})
	if cancelEnv.CorrelationID != origin.CorrelationID {
		t.Fatalf("the cancel rides correlation %q, want the task's own %q", cancelEnv.CorrelationID, origin.CorrelationID)
	}
	if len(cancelEnv.Authority) == 0 {
		t.Error("the cancel carries no authority block")
	}
	// The heal ran first and said so; the cancel then went out anyway.
	r.waitForPost(t, reply.Conversation, "produced nothing on its event stream")
	r.waitForPost(t, reply.Conversation, "no longer holds")
	// MarkCanceled rides the end-of-turn record write, which lands after the
	// post; read the record only once that write is in.
	r.awaitTurnEnd(t, reply.Conversation)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	rec, err := r.g.reg.Get(ctx, reply.Conversation)
	if err != nil || rec == nil {
		t.Fatalf("record: %v", err)
	}
	if rec.ActiveTask != nil {
		t.Fatalf("the record still holds the released task: %+v", rec.ActiveTask)
	}
	if !rec.TaskCanceled(reply.TaskID) {
		t.Fatal("the published cancel is not recorded on the task's history entry")
	}
}

// TestInjectCancelRefusesATaskTheConversationNeverHeld: naming a task is not
// a way to publish cancels for tasks this conversation did not start.
func TestInjectCancelRefusesATaskTheConversationNeverHeld(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-stranger", injectTestAuthor, "hello")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	out := r.cancelTask(t, reply.Conversation, injectTestAuthor, "task-someone-elses")
	if out.TaskID != "" {
		t.Fatalf("a cancel started a task: %+v", out)
	}
	r.waitForPost(t, reply.Conversation, "never held task")
	for _, env := range inSubjectEnvelopes(t, r.url, "platform") {
		if env.Kind == lib.KindCancel {
			t.Fatalf("a cancel was published for a task the conversation never held: %+v", env)
		}
	}
	// And the running task is untouched.
	probe := r.probe(t, reply.Conversation, reply.TaskID).Probe
	if !probe.Active || probe.Detached || probe.TaskID != reply.TaskID {
		t.Fatalf("probe = %+v, want the original task active and not detached", probe)
	}
}

// TestInjectRefusesAPostWhoseSubmissionCannotReachTheBus: the POST answers a
// task id only for a task that is on the bus. startTask announces the id,
// posts the placeholder and writes the record BEFORE it publishes, so a
// caller answered at the announcement would hold an id for a task no
// executor can ever see -- and would wait out its whole budget for a
// terminal that cannot come. The stream is taken away here so the publish
// fails for real.
func TestInjectRefusesAPostWhoseSubmissionCannotReachTheBus(t *testing.T) {
	r := startInjectRig(t)
	deleteTasksStream(t, r.url)

	reply := r.inject(t, "case-no-bus", injectTestAuthor, "how is the fleet?")

	if reply.TaskID != "" || reply.Accepted {
		t.Fatalf("a task that never reached the bus was answered as accepted: %+v", reply)
	}
	if reply.Refusal != injectRefusalPublishFailed {
		t.Fatalf("refusal = %q, want %q", reply.Refusal, injectRefusalPublishFailed)
	}
	if !strings.Contains(reply.Note, "could not put it on the bus") {
		t.Fatalf("note = %q, want it to say the submission never reached the bus", reply.Note)
	}
	// What a human would read for the same failure, which is the gateway's
	// and not this door's: the placeholder edited in place.
	edits := strings.Join(entryTexts(reply.Entries, InjectEntryEdit), "\n")
	if !strings.Contains(edits, "could not reach the bus") {
		t.Fatalf("edits = %q, want the gateway's failure edit", edits)
	}
	// And the structural half a program reads: the gateway's own terminal
	// for the task, which is what makes the refusal knowable without
	// matching on that sentence.
	terminals := terminalEntries(reply.Entries)
	if len(terminals) != 1 || terminals[0].Source != string(TerminalFromGateway) {
		t.Fatalf("terminal entries = %+v, want one the gateway declared", terminals)
	}
	// The conversation is released, so nothing is wedged behind a task that
	// cannot end. Waited for rather than read once: startTask writes the
	// record with the active task before it publishes and clears it in
	// memory on the failure, and the write that persists the clear is
	// handleInbound's at the end of the turn -- which lands AFTER this POST
	// has been answered. That ordering is why the refusal, and not a read of
	// the record, is what tells a caller its submission never reached the
	// bus: for a moment the record names a task that is nowhere.
	waitFor(t, "the conversation to be released", func() bool {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		rec, err := r.g.reg.Get(ctx, reply.Conversation)
		return err == nil && (rec == nil || rec.ActiveTask == nil)
	})
}

// TestInjectAnswersARetriedPostWithTheSameRefusal: the dedupe answers a retry
// with what the first POST got, and a refusal is an answer. Without this a
// retry of a POST the gateway could not publish would be routed as a fresh
// message and mint a second doomed task.
func TestInjectAnswersARetriedPostWithTheSameRefusal(t *testing.T) {
	r := startInjectRig(t)
	deleteTasksStream(t, r.url)
	const messageID = "run-1/case-no-bus/1"

	first := r.injectWithID(t, "case-no-bus", injectTestAuthor, "how is the fleet?", messageID)
	if first.Refusal != injectRefusalPublishFailed || first.Deduplicated {
		t.Fatalf("first POST = %+v, want the publish-failed refusal", first)
	}
	retry := r.injectWithID(t, "case-no-bus", injectTestAuthor, "how is the fleet?", messageID)
	if retry.Refusal != first.Refusal || retry.Note != first.Note || !retry.Deduplicated {
		t.Fatalf("retry = %+v, want the first refusal marked deduplicated", retry)
	}
	if starts := r.adapter.counts(first.Conversation).starts; starts != 1 {
		t.Fatalf("%d tasks minted, want 1: the retry was routed", starts)
	}
}

// TestInjectReadRouteRemembersWorkingBehindALaterState: the read reports the
// stream's latest state, and two events can land between a caller's reads --
// working, then input-required. A caller deciding "a model ran" from the
// states it saw would file a run that happened as one that never started, so
// the read also says whether working was ever on the stream.
func TestInjectReadRouteRemembersWorkingBehindALaterState(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-parked-after-working", injectTestAuthor, "take your time")
	r.awaitRecordedTask(t, reply.Conversation)
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	for _, state := range []lib.TaskState{lib.StateSubmitted, lib.StateWorking, lib.StateInputRequired} {
		if err := exec.PublishStatus(ctx, state, false); err != nil {
			t.Fatal(err)
		}
	}
	waitFor(t, "the read route to see the executor parked", func() bool {
		return r.probe(t, reply.Conversation, reply.TaskID).Probe.ExecutorState == string(lib.StateInputRequired)
	})
	parked := r.probe(t, reply.Conversation, reply.TaskID).Probe
	if !parked.ReachedWorking {
		t.Fatalf("parked probe = %+v, want reachedWorking behind the later state", parked)
	}
	if parked.Final || !parked.Active {
		t.Fatalf("parked probe = %+v, want active and not final", parked)
	}
}

// TestInjectRefusesASecondDropWithoutWaitingForTheBound: the gateway tells an
// unverifiable sender once per sender, so the second message from that author
// posts nothing at all. A door watching only the transcript would hold that
// POST until its accept bound and then answer "nothing visible happened",
// which reads as a stuck gateway rather than as the answer it already gave.
func TestInjectRefusesASecondDropWithoutWaitingForTheBound(t *testing.T) {
	r := startInjectRig(t)

	first := r.inject(t, "case-twice-unmapped", injectTestUnknownAuthor, "let me in")
	if first.Refusal != injectRefusalUnverifiedAuthor {
		t.Fatalf("first drop = %+v, want the unverified-author refusal", first)
	}
	entriesBefore := r.adapter.counts(first.Conversation).entries
	// The memory is per backend and author: the same id arriving through a
	// chat backend is another sender and owed its own notice.
	r.g.mu.Lock()
	notified := r.g.droppedNotices[droppedNoticeKey(injectBackend, injectTestUnknownAuthor)]
	r.g.mu.Unlock()
	if !notified {
		t.Fatalf("the first drop is not remembered under the door's key")
	}

	started := time.Now()
	second := r.inject(t, "case-twice-unmapped", injectTestUnknownAuthor, "let me in again")
	elapsed := time.Since(started)

	if second.Refusal != injectRefusalUnverifiedAuthor {
		t.Fatalf("second drop = %+v, want the same refusal", second)
	}
	if elapsed > injectSubmitWait/2 {
		t.Fatalf("the second drop took %s, want an answer well inside the %s bound", elapsed, injectSubmitWait)
	}
	if entries := r.adapter.counts(first.Conversation).entries; entries != entriesBefore {
		t.Fatalf("the conversation gained %d entries on the second drop; the notice is once per sender",
			entries-entriesBefore)
	}
	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 0 {
		t.Fatalf("an unverified sender reached the bus: %d envelopes", len(envs))
	}
}

// TestInjectWaitsThroughThePlaceholderForTheAccept: the placeholder post now
// lands between a task's announcement and its publish, and the rule that a
// post without a task means "the turn answered without starting one" must not
// fire on it. Were the wait to classify from the entries alone rather than
// only once the turn has ended (awaitTurn reads the counts only after
// TurnFinished has moved `turns`), a gateway whose record write and publish
// take longer than a poll interval -- a loaded KV, a bus retry -- would have
// its submissions refused as `no-task`, and the harness would call a healthy
// run infrastructure. The handler here is deliberately slow between
// the two halves; the real one is fast, which is why nothing else catches it.
func TestInjectWaitsThroughThePlaceholderForTheAccept(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	door, err := NewInjectAdapter(ln.Addr().String(), injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	door.listener = ln
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	handler := func(msg InboundMessage) {
		go func() {
			// startTask's order: announce, post the placeholder, write the
			// record, publish, accept.
			door.TaskStarted(msg.Conversation, "task-slow")
			if _, err := door.Post(msg.Conversation, "⏳ submitted…"); err != nil {
				t.Error(err)
			}
			time.Sleep(4 * injectPollInterval)
			door.TaskAccepted(msg.Conversation, "task-slow")
		}()
	}
	go func() { _ = door.Run(ctx, handler) }()
	waitFor(t, "the door to accept messages", func() bool {
		door.handlerMu.RLock()
		defer door.handlerMu.RUnlock()
		return door.handler != nil
	})

	body, _ := json.Marshal(injectRequest{Conversation: "slow-publish", Author: injectTestAuthor, Text: "go"})
	req, _ := http.NewRequest(http.MethodPost, "http://"+ln.Addr().String()+injectPath, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(authorizationHeader, "Bearer "+injectTestToken)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var reply injectResponse
	if err := json.NewDecoder(resp.Body).Decode(&reply); err != nil {
		t.Fatal(err)
	}
	if reply.TaskID != "task-slow" || reply.Refusal != "" {
		t.Fatalf("reply = %+v, want task-slow with no refusal: the placeholder was read as an answer", reply)
	}
}

// TestInjectIDLogSurvivesItsOwnEviction: the wait indexes these logs by
// absolute position, and they evict oldest-first. Without the total the
// indices shift under an eviction and a waiter is handed a later task's id --
// which it would then await forever. Evicted past is its own answer, not
// "nothing happened".
func TestInjectIDLogSurvivesItsOwnEviction(t *testing.T) {
	var log injectIDLog
	for i := range injectMaxEntries + 2 {
		log.add(fmt.Sprintf("task-%d", i))
	}
	if id, evicted := log.at(injectMaxEntries + 1); evicted || id != fmt.Sprintf("task-%d", injectMaxEntries+1) {
		t.Fatalf("at(last) = %q, evicted=%v; want the newest id", id, evicted)
	}
	if id, evicted := log.at(injectMaxEntries + 2); evicted || id != "" {
		t.Fatalf("at(past the end) = %q, evicted=%v; want nothing yet", id, evicted)
	}
	if id, evicted := log.at(0); !evicted || id != "" {
		t.Fatalf("at(0) = %q, evicted=%v; want the evicted answer rather than a later task's id", id, evicted)
	}
}

// TestInjectCancelFromAnUnknownAuthorDoesNotHoldTheConnection: the cancel
// route's wait has the same once-per-sender problem the POST's does. A second
// cancel from an author the map does not carry posts nothing, and a wait
// watching only the transcript would hold it for the whole bound.
func TestInjectCancelFromAnUnknownAuthorDoesNotHoldTheConnection(t *testing.T) {
	r := startInjectRig(t)
	// The first drop posts the notice; the second is the silent one.
	r.inject(t, "case-cancel-unmapped", injectTestUnknownAuthor, "let me in")

	started := time.Now()
	reply := r.cancel(t, injectKeyPrefix+"case-cancel-unmapped", injectTestUnknownAuthor)
	elapsed := time.Since(started)

	if elapsed > injectSubmitWait/2 {
		t.Fatalf("the cancel took %s, want an answer well inside the %s bound", elapsed, injectSubmitWait)
	}
	if !strings.Contains(reply.Note, "dropped this cancel") {
		t.Fatalf("note = %q, want it to say the cancel was dropped", reply.Note)
	}
}

// TestInjectNamedCancelOfTheActiveTaskDetachesTheRecord: the shape every
// cancel the harness sends actually takes. It always names the task, and the
// commonest case -- working at the budget -- names the one the record still
// holds as active, which must route to the ordinary cancel and detach the
// record. A regression that sent it to the named-cancel path instead would
// still publish `kind: cancel` and mark the history entry, so the bus-side
// assertions elsewhere would pass while the record stayed attached: the read
// route would report `detached:false`, the harness would grade the timeout
// off an undetached record, and the next message would steer a cancelled
// task.
func TestInjectNamedCancelOfTheActiveTaskDetachesTheRecord(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-named-active", injectTestAuthor, "long job")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}

	r.cancelTask(t, reply.Conversation, injectTestAuthor, reply.TaskID)
	r.waitForPost(t, reply.Conversation, "cancel sent")
	// The detach is persisted by the end-of-turn write, not by the post.
	r.awaitTurnEnd(t, reply.Conversation)

	page := r.probe(t, reply.Conversation, reply.TaskID)
	probe := page.Probe
	if !probe.Active || !probe.Detached || probe.TaskID != reply.TaskID {
		t.Fatalf("after a named cancel of the active task: probe = %+v, want it active and detached", probe)
	}
	// And the named-cancel path's own refusal did not fire: the conversation
	// holds this task, so the cancel is the ordinary one. Read off the page
	// the probe just fetched, not the POST's reply, which predates the cancel.
	posts := strings.Join(entryTexts(page.Entries, InjectEntryPost), "\n")
	if strings.Contains(posts, "no longer holds") {
		t.Fatalf("the active task took the released-task path: %q", posts)
	}
	cancels := 0
	for _, env := range inSubjectEnvelopes(t, r.url, "platform") {
		if env.Kind == lib.KindCancel {
			cancels++
		}
	}
	if cancels != 1 {
		t.Fatalf("%d cancel envelopes on the in subject, want 1", cancels)
	}
}

// TestInjectHealedTerminalCarriesTheExecutorsReason: the heal hands the door
// the terminal the relay should have delivered, and its copy must carry the
// same reason. A caller tells an executor's own failure from the persona's by
// that token, so an empty one on the heal's copy grades an install fault as
// the agent's answer -- and the heal's copy is the one a caller sees when the
// terminal lands in the window between its last read and the turn it sends
// next.
func TestInjectHealedTerminalCarriesTheExecutorsReason(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-healed-reason", injectTestAuthor, "long job")
	origin := r.awaitTask(t, "platform")
	ctx := context.Background()
	const reason = "reason: bridge-shutdown - draining"
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID: origin.TaskID, ContextID: origin.ContextID,
		Status: lib.TaskStatus{State: lib.StateFailed, Message: &lib.Message{
			Role: "agent", MessageID: "msg-healed", TaskID: origin.TaskID, ContextID: origin.ContextID,
			Parts: []lib.Part{{Kind: "text", Text: reason}},
		}},
		Final: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(lib.Party{Session: "platform", AgentType: "test-executor"},
		origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(ctx, lib.TaskEventsSubject("platform", origin.TaskID), env); err != nil {
		t.Fatal(err)
	}
	r.waitForTerminal(t, reply.Conversation, reply.TaskID)

	// The shape the heal exists for: the relay's record write was lost, so
	// the conversation still holds a task whose stream is already terminal,
	// and the next turn is what discovers it.
	r.restoreActiveTask(t, reply.Conversation, origin, time.Minute)
	before, _, _ := r.adapter.snapshot(reply.Conversation, 0, "")
	r.inject(t, "case-healed-reason", injectTestAuthor, "anything")

	entries, _, _ := r.adapter.snapshot(reply.Conversation, len(before), "")
	healed := terminalEntries(entries)
	if len(healed) == 0 {
		t.Fatalf("the heal handed the door no terminal; entries = %+v", entries)
	}
	for _, entry := range healed {
		if entry.TaskID != reply.TaskID {
			continue
		}
		if entry.Reason != reason {
			t.Fatalf("the healed terminal's reason = %q, want the executor's %q", entry.Reason, reason)
		}
	}
}

// TestInjectHealedTerminalNamesTheSupervisorsAsItsOwn: the heal is the third
// reader of the fold, after the relay and the read route, and the only one
// that delivers a terminal the relay lost. A supervisor's terminal (a session
// pod that died or could not be spawned) healed as the executor's would reach
// the door as `source: executor`, and a caller that adopts a healed terminal
// on its settle read would grade an outage as the agent failing.
func TestInjectHealedTerminalNamesTheSupervisorsAsItsOwn(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-healed-supervisor", injectTestAuthor, "long job")
	origin := r.awaitTask(t, "platform")
	ctx := context.Background()
	const reason = "reason: spawn-failed"
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID: origin.TaskID, ContextID: origin.ContextID,
		Status: lib.TaskStatus{State: lib.StateFailed, Message: &lib.Message{
			Role: "agent", MessageID: "msg-healed-sup", TaskID: origin.TaskID, ContextID: origin.ContextID,
			Parts: []lib.Part{{Kind: "text", Text: reason}},
		}},
		Final: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(gatewayParty, origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(ctx, lib.TaskSupervisorSubject("platform", origin.TaskID), env); err != nil {
		t.Fatal(err)
	}
	r.waitForTerminal(t, reply.Conversation, reply.TaskID)

	r.restoreActiveTask(t, reply.Conversation, origin, time.Minute)
	before, _, _ := r.adapter.snapshot(reply.Conversation, 0, "")
	r.inject(t, "case-healed-supervisor", injectTestAuthor, "anything")

	entries, _, _ := r.adapter.snapshot(reply.Conversation, len(before), "")
	var healed *InjectEntry
	for _, entry := range terminalEntries(entries) {
		if entry.TaskID == reply.TaskID {
			healed = &entry
		}
	}
	if healed == nil {
		t.Fatalf("the heal handed the door no terminal for %s; entries = %+v", reply.TaskID, entries)
	}
	if healed.Source != string(TerminalFromSupervisor) {
		t.Fatalf("the healed terminal's source = %q, want %q", healed.Source, TerminalFromSupervisor)
	}
	if healed.Reason != reason {
		t.Fatalf("the healed terminal's reason = %q, want %q", healed.Reason, reason)
	}
}

// TestInjectCancelSaysWhetherItReachedTheBus: every cancel route answers the
// conversation with a post -- the acknowledgement, "this conversation never
// held task X", "task X predates this record's correlation ids", "could not
// send the stop" -- so a 200 with an entry is not evidence a cancel went. A
// caller that recorded one as published would tell its reader a stray run
// was bounded while it runs. The door answers the publish itself.
func TestInjectCancelSaysWhetherItReachedTheBus(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-cancel-code", injectTestAuthor, "long job")
	r.awaitTask(t, "platform")
	r.awaitRecordedTask(t, reply.Conversation)

	refused := r.cancelTask(t, reply.Conversation, injectTestAuthor, "task-never-held")
	if refused.CancelPublished {
		t.Fatalf("a cancel for a task the conversation never held reported published: %+v", refused)
	}
	if refused.Refusal != injectRefusalNoCancel {
		t.Fatalf("refusal = %q, want %s", refused.Refusal, injectRefusalNoCancel)
	}
	if !strings.Contains(refused.Note, "still doing") {
		t.Fatalf("note = %q, want it to say the task is still running", refused.Note)
	}

	sent := r.cancelTask(t, reply.Conversation, injectTestAuthor, reply.TaskID)
	if !sent.CancelPublished || sent.Refusal != "" {
		t.Fatalf("a cancel that reached the bus answered %+v, want published with no refusal", sent)
	}
}

// fakeDoor is an InjectAdapter run over a handler the test supplies in place
// of the gateway, for the waits whose subject is the ORDER of observer calls
// rather than what the gateway does. It answers a POST or a cancel the way
// the rig's helpers do.
type fakeDoor struct {
	door *InjectAdapter
	addr string
}

func startFakeDoor(t *testing.T, handler func(InboundMessage)) *fakeDoor {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	door, err := NewInjectAdapter(ln.Addr().String(), injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	door.listener = ln
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { _ = door.Run(ctx, handler) }()
	waitFor(t, "the door to accept messages", func() bool {
		door.handlerMu.RLock()
		defer door.handlerMu.RUnlock()
		return door.handler != nil
	})
	return &fakeDoor{door: door, addr: ln.Addr().String()}
}

func (f *fakeDoor) post(t *testing.T, target string, body any) injectResponse {
	t.Helper()
	raw, _ := json.Marshal(body)
	req, _ := http.NewRequest(http.MethodPost, "http://"+f.addr+target, bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(authorizationHeader, "Bearer "+injectTestToken)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var reply injectResponse
	if err := json.NewDecoder(resp.Body).Decode(&reply); err != nil {
		t.Fatal(err)
	}
	return reply
}

func (f *fakeDoor) inject(t *testing.T, conversation, text string) injectResponse {
	t.Helper()
	return f.post(t, injectPath, injectRequest{Conversation: conversation, Author: injectTestAuthor, Text: text})
}

func (f *fakeDoor) cancel(t *testing.T, key string) injectResponse {
	t.Helper()
	return f.post(t, conversationsPath+key+cancelSuffix, cancelRequest{Author: injectTestAuthor})
}

// evictEverything mints more conversations than the door retains, so that
// every conversation minted before the call is gone.
func (f *fakeDoor) evictEverything(t *testing.T, tag string) {
	t.Helper()
	for i := range injectMaxConversations {
		if _, err := f.door.Post(fmt.Sprintf("%s-filler-%d", tag, i), "filler"); err != nil {
			t.Error(err)
		}
	}
}

// TestInjectAnswersAnAcceptThatLandsWithTheTurnEnd pins the invariant the
// wait classifies by: a turn that has ended after recording an accept
// answers the accept, however the two land relative to the waiter's looks.
// The handler runs startTask's whole sequence to completion before the wait
// takes its first look, so the first reading already shows the turn over --
// the ordering that, read as two lookups (logs, then counts), lets an accept
// recorded between them be classified as a turn that ended without one.
// Deterministic for the "turn already over" ordering; the descheduled-waiter
// ordering is the same reading and cannot be forced from here.
func TestInjectAnswersAnAcceptThatLandsWithTheTurnEnd(t *testing.T) {
	var f *fakeDoor
	ready := make(chan struct{})
	handler := func(msg InboundMessage) {
		<-ready
		id := "task-" + msg.MessageID
		f.door.TaskStarted(msg.Conversation, id)
		if _, err := f.door.Post(msg.Conversation, "⏳ submitted…"); err != nil {
			t.Error(err)
		}
		f.door.TaskAccepted(msg.Conversation, id)
		f.door.TurnFinished(msg.Conversation)
	}
	f = startFakeDoor(t, handler)
	close(ready)

	const turns = 25
	for i := range turns {
		reply := f.inject(t, fmt.Sprintf("turn-end-%d", i), "go")
		if reply.TaskID == "" || reply.Refusal != "" {
			t.Fatalf("POST %d: reply = %+v, want the accepted task id with no refusal", i, reply)
		}
	}
}

// TestInjectRefusesWhenItsConversationWasEvictedUnderTheWait: the door evicts
// a conversation wholesale at its cap and mints it again with every counter
// at zero. A waiter that read its counts before that would otherwise classify
// from the new incarnation: on a conversation's second turn, prior holds one
// turn and one accept, the new incarnation's single accept sits at position
// zero where prior already looked past, and its turn count never exceeds
// prior's -- so the wait sees nothing move and holds the connection for the
// whole bound while the task it was waiting on runs. The wait compares the
// mint generation instead and says what happened, at once.
func TestInjectRefusesWhenItsConversationWasEvictedUnderTheWait(t *testing.T) {
	var f *fakeDoor
	ready := make(chan struct{})
	var turn atomic.Int32
	handler := func(msg InboundMessage) {
		<-ready
		id := fmt.Sprintf("task-%d", turn.Add(1))
		f.door.TaskStarted(msg.Conversation, id)
		if _, err := f.door.Post(msg.Conversation, "⏳ submitted…"); err != nil {
			t.Error(err)
		}
		if turn.Load() > 1 {
			// Between the announcement and the accept, every conversation
			// the door held is evicted; the accept mints this one again.
			f.evictEverything(t, "evicted-under-wait")
		}
		f.door.TaskAccepted(msg.Conversation, id)
		f.door.TurnFinished(msg.Conversation)
	}
	f = startFakeDoor(t, handler)
	close(ready)

	if first := f.inject(t, "evicted-under-wait", "go"); first.TaskID != "task-1" {
		t.Fatalf("first turn: reply = %+v, want task-1", first)
	}
	started := time.Now()
	reply := f.inject(t, "evicted-under-wait", "again")
	if elapsed := time.Since(started); elapsed > injectSubmitWait/2 {
		t.Fatalf("the POST took %s, want an answer well inside the %s bound", elapsed, injectSubmitWait)
	}
	if reply.Refusal != injectRefusalNoAnswer || reply.TaskID != "" {
		t.Fatalf("reply = %+v, want a %s refusal and no task id", reply, injectRefusalNoAnswer)
	}
	if !strings.Contains(reply.Note, "evicted this conversation") {
		t.Fatalf("note = %q, want it to name the eviction rather than a turn that did nothing", reply.Note)
	}
}

// TestInjectCancelRefusesWhenItsConversationWasEvictedUnderTheWait: the
// cancel's wait reads the same counters and has the same hole; without the
// generation it would wait out the whole bound on a conversation whose reply
// it can no longer see.
func TestInjectCancelRefusesWhenItsConversationWasEvictedUnderTheWait(t *testing.T) {
	var f *fakeDoor
	ready := make(chan struct{})
	handler := func(msg InboundMessage) {
		<-ready
		go func() {
			f.evictEverything(t, "cancel-evicted")
			if _, err := f.door.Post(msg.Conversation, "nothing is running here"); err != nil {
				t.Error(err)
			}
			f.door.TurnFinished(msg.Conversation)
		}()
	}
	f = startFakeDoor(t, handler)
	close(ready)

	started := time.Now()
	reply := f.cancel(t, injectKeyPrefix+"cancel-evicted")
	if elapsed := time.Since(started); elapsed > injectSubmitWait/2 {
		t.Fatalf("the cancel took %s, want an answer well inside the %s bound", elapsed, injectSubmitWait)
	}
	if !strings.Contains(reply.Note, "evicted this conversation") {
		t.Fatalf("note = %q, want it to name the eviction", reply.Note)
	}
}

// TestInjectRefusesAnUnboundedKeyOnTheReadAndCancelRoutes: the POST route
// bounds a caller's key and refuses control characters because the key
// becomes a session record's key and an ingress log line. The cancel route
// hands its key to the same handleInbound, and the read route logs it, so a
// key that the POST would refuse must not get in by the path.
func TestInjectRefusesAnUnboundedKeyOnTheReadAndCancelRoutes(t *testing.T) {
	r := startInjectRig(t)
	long := strings.Repeat("k", injectMaxKeyRunes+1)
	for _, tc := range []struct{ name, key string }{
		{"too long", long},
		{"control character", "case%0Aone"},
		{"bare colon", "not:ours"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			resp, err := r.do(t, http.MethodGet, r.base+conversationsPath+tc.key+"?probe=1", nil, injectTestToken)
			if err != nil {
				t.Fatal(err)
			}
			resp.Body.Close()
			if resp.StatusCode != http.StatusBadRequest {
				t.Fatalf("GET with a %s key: status %d, want 400", tc.name, resp.StatusCode)
			}
			body, _ := json.Marshal(cancelRequest{Author: injectTestAuthor})
			resp, err = r.do(t, http.MethodPost, r.base+conversationsPath+tc.key+cancelSuffix, body, injectTestToken)
			if err != nil {
				t.Fatal(err)
			}
			resp.Body.Close()
			if resp.StatusCode != http.StatusBadRequest {
				t.Fatalf("cancel with a %s key: status %d, want 400", tc.name, resp.StatusCode)
			}
		})
	}
}

// TestInjectProbeDescribesTheInstantTheWaitEnded: a blocking poll's probe
// used to be taken before the wait, so the reply paired entries that had
// just landed with a probe up to `wait` seconds old. A caller classifying
// "nothing on the stream past the grace" from that probe would call a task
// whose first event arrived during the wait -- the relay's edit is what wakes
// the wait -- one that nobody took, and cancel it. The probe is taken again
// after a wait that blocked.
func TestInjectProbeDescribesTheInstantTheWaitEnded(t *testing.T) {
	r := startInjectRig(t)
	reply := r.inject(t, "case-probe-instant", injectTestAuthor, "answer me")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	first := r.conversation(t, reply.Conversation, 0, reply.TaskID, 0)

	type answer struct {
		resp *http.Response
		err  error
	}
	got := make(chan answer, 1)
	go func() {
		target := r.base + conversationsPath + reply.Conversation +
			fmt.Sprintf("?after=%d&task=%s&wait=20&probe=1", first.LastSeq, reply.TaskID)
		req, _ := http.NewRequest(http.MethodGet, target, nil)
		req.Header.Set(authorizationHeader, "Bearer "+injectTestToken)
		resp, err := http.DefaultClient.Do(req)
		got <- answer{resp, err}
	}()
	// Let the poll take its first probe and settle into the wait.
	time.Sleep(4 * injectPollInterval)
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	a := <-got
	if a.err != nil {
		t.Fatal(a.err)
	}
	defer a.resp.Body.Close()
	var out conversationResponse
	if err := json.NewDecoder(a.resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if len(out.Entries) == 0 {
		t.Fatalf("the wait ended with no entries; the relay's edit should have woken it: %+v", out)
	}
	if out.Probe == nil || out.Probe.ExecutorState != string(lib.StateWorking) {
		t.Fatalf("probe = %+v, want executorState working: the probe predates the entry beside it", out.Probe)
	}
}

// TestInjectASecondPostWaitsForTheEarlierTurnToEnd: a POST is answered at
// the accept, before its turn has ended (the end-of-turn record write and the
// turn signal come after). A second POST on the same key inside that gap
// used to read counts one turn short and classify from the first turn's
// work: `no-answer` for a message the worker had not yet run, while that
// message's own task then ran with nobody waiting on it. The door now hands
// a message over only once the earlier turn has ended.
func TestInjectASecondPostWaitsForTheEarlierTurnToEnd(t *testing.T) {
	var f *fakeDoor
	ready := make(chan struct{})
	var turnMu sync.Mutex
	var turn atomic.Int32
	handler := func(msg InboundMessage) {
		<-ready
		// Queued and serialised per conversation, as the inbox worker does.
		go func() {
			turnMu.Lock()
			defer turnMu.Unlock()
			id := fmt.Sprintf("task-%d", turn.Add(1))
			f.door.TaskStarted(msg.Conversation, id)
			if _, err := f.door.Post(msg.Conversation, "⏳ submitted…"); err != nil {
				t.Error(err)
			}
			f.door.TaskAccepted(msg.Conversation, id)
			if id == "task-1" {
				// The gap: the record write and whatever else the turn does
				// after the publish.
				time.Sleep(4 * injectPollInterval)
			}
			f.door.TurnFinished(msg.Conversation)
		}()
	}
	f = startFakeDoor(t, handler)
	close(ready)

	if first := f.inject(t, "second-post", "go"); first.TaskID != "task-1" {
		t.Fatalf("first POST: reply = %+v, want task-1", first)
	}
	second := f.inject(t, "second-post", "and again")
	if second.TaskID != "task-2" || second.Refusal != "" {
		t.Fatalf("second POST: reply = %+v, want task-2 with no refusal: it classified from the first turn", second)
	}
}

// TestInjectACancelIsHandedOverAfterItsClientLeft: a cancel that arrives
// while an earlier turn is still running waits for that turn to end before
// it is handed over, and a client that gives up during that wait has still
// asked for the stop. Were the claim taken on the request's context, the
// client's departure would end it with the cancel never handed over, and
// the task the client meant to stop would run on with nobody left to stop
// it. So the claim is taken on a context the client cannot end and only the
// bound ends it; the cancel reaches the gateway once the turn ends.
func TestInjectACancelIsHandedOverAfterItsClientLeft(t *testing.T) {
	var f *fakeDoor
	ready := make(chan struct{})
	var cancels atomic.Int32
	handler := func(msg InboundMessage) {
		<-ready
		go func() {
			if msg.Intent == IntentCancel {
				cancels.Add(1)
				f.door.CancelPublished(msg.Conversation, "task-1")
				f.door.TurnFinished(msg.Conversation)
				return
			}
			f.door.TaskStarted(msg.Conversation, "task-1")
			if _, err := f.door.Post(msg.Conversation, "⏳ submitted…"); err != nil {
				t.Error(err)
			}
			f.door.TaskAccepted(msg.Conversation, "task-1")
			// The turn runs on past the accept, longer than the client
			// below is prepared to wait.
			time.Sleep(6 * injectPollInterval)
			f.door.TurnFinished(msg.Conversation)
		}()
	}
	f = startFakeDoor(t, handler)
	close(ready)

	const conversation = "client-left"
	if first := f.inject(t, conversation, "go"); first.TaskID != "task-1" {
		t.Fatalf("first POST: reply = %+v, want task-1", first)
	}
	// A cancel whose client waits one poll interval and then hangs up,
	// while the first turn still has several to run.
	ctx, cancel := context.WithTimeout(context.Background(), injectPollInterval)
	defer cancel()
	raw, _ := json.Marshal(cancelRequest{Author: injectTestAuthor})
	target := "http://" + f.addr + conversationsPath + injectKeyPrefix + conversation + cancelSuffix
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, target, bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(authorizationHeader, "Bearer "+injectTestToken)
	if resp, err := http.DefaultClient.Do(req); err == nil {
		resp.Body.Close()
		t.Fatal("the cancel answered inside one poll interval; the earlier turn should still have held it")
	}
	if got := cancels.Load(); got != 0 {
		t.Fatalf("the cancel was handed over while the earlier turn was still running (%d)", got)
	}
	waitFor(t, "the cancel to be handed over once the earlier turn ended", func() bool {
		return cancels.Load() == 1
	})
}

// TestInjectEvictionDropsEveryAuthorsMappingToTheConversation: directOf is
// bounded by the conversation cap only if an eviction drops every author that
// still maps to the evicted key, not only the last one to speak on it.
func TestInjectEvictionDropsEveryAuthorsMappingToTheConversation(t *testing.T) {
	f := startFakeDoor(t, func(InboundMessage) {})
	const key = injectKeyPrefix + "two-authors"
	f.door.noteRequester(key, "author-a")
	f.door.noteRequester(key, "author-b")
	f.evictEverything(t, "two-authors")
	f.door.mu.Lock()
	defer f.door.mu.Unlock()
	for _, author := range []string{"author-a", "author-b"} {
		if conversation, ok := f.door.directOf[author]; ok {
			t.Errorf("%s still maps to %q after its conversation was evicted", author, conversation)
		}
		for _, queued := range f.door.directOrder {
			if queued == author {
				t.Errorf("%s still has a place in the cap's order after its conversation was evicted", author)
			}
		}
	}
}

// TestInjectDirectOfIsCappedAcrossDistinctAuthors: an eviction drops the
// authors mapped to the evicted conversation, but a token holder can name a
// fresh author on every request to one live conversation, which no eviction
// on that side ever reaches. The cap does, oldest author first.
func TestInjectDirectOfIsCappedAcrossDistinctAuthors(t *testing.T) {
	f := startFakeDoor(t, func(InboundMessage) {})
	const key = injectKeyPrefix + "many-authors"
	const over = 3
	for i := range injectMaxDirectAuthors + over {
		f.door.noteRequester(key, fmt.Sprintf("author-%d", i))
	}
	f.door.mu.Lock()
	defer f.door.mu.Unlock()
	if len(f.door.directOf) != injectMaxDirectAuthors || len(f.door.directOrder) != injectMaxDirectAuthors {
		t.Fatalf("directOf holds %d authors (order %d), want the cap of %d",
			len(f.door.directOf), len(f.door.directOrder), injectMaxDirectAuthors)
	}
	if _, ok := f.door.directOf["author-0"]; ok {
		t.Error("the oldest author is still mapped past the cap")
	}
	if got := f.door.directOf[fmt.Sprintf("author-%d", injectMaxDirectAuthors+over-1)]; got != key {
		t.Errorf("the newest author maps to %q, want %q", got, key)
	}
}

// TestInjectAReMintedConversationContinuesItsSequence: a conversation evicted
// under a running task is minted again by the relay's next post, and that
// post must not be numbered from 1. A poller holding `after` from before the
// eviction filters on the number, so a restart would hide the deliverable
// from it for good; InjectEntry.Seq promises otherwise.
func TestInjectAReMintedConversationContinuesItsSequence(t *testing.T) {
	f := startFakeDoor(t, func(InboundMessage) {})
	const key = injectKeyPrefix + "long-runner"
	for _, text := range []string{"⏳ submitted…", "⚙️ working"} {
		if _, err := f.door.Post(key, text); err != nil {
			t.Fatal(err)
		}
	}
	if _, last, _ := f.door.snapshot(key, 0, ""); last != 2 {
		t.Fatalf("lastSeq before the eviction = %d, want 2", last)
	}
	f.evictEverything(t, "long-runner")
	if _, err := f.door.Post(key, "the answer"); err != nil {
		t.Fatal(err)
	}
	entries, last, _ := f.door.snapshot(key, 2, "")
	if len(entries) != 1 || entries[0].Seq != 3 || last != 3 {
		t.Fatalf("after the eviction a poller at after=2 sees entries=%+v lastSeq=%d; want the one new post at seq 3", entries, last)
	}
}

// TestInjectAClaimWithLessThanATurnLeftIsRefusedNotHandedOver: a message
// behind an earlier turn that ends late in this message's bound is refused at
// the claim rather than handed over with less of the bound left than the
// turn timeout handleInbound runs under. Handed over, the wait would fall due
// while the turn legitimately ran, answer `no-answer`, pin that answer for
// every retry of the message id, and the turn would go on to mint a task
// nobody was told about -- the outcome a refusal exists to rule out.
func TestInjectAClaimWithLessThanATurnLeftIsRefusedNotHandedOver(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ln.Close()
	door, err := NewInjectAdapter(ln.Addr().String(), injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	const key = injectKeyPrefix + "late-handover"
	ctx := context.Background()
	if _, ok := door.claimTurn(ctx, key, time.Now().Add(injectSubmitWait)); !ok {
		t.Fatal("the first claim on a quiet conversation must succeed")
	}
	type outcome struct {
		ok      bool
		elapsed time.Duration
	}
	late := make(chan outcome, 1)
	started := time.Now()
	go func() {
		_, ok := door.claimTurn(ctx, key, time.Now().Add(turnTimeout-time.Second))
		late <- outcome{ok, time.Since(started)}
	}()
	// Let the claim take its first look and start waiting, then end the
	// earlier turn with less than a turn of the second message's bound left.
	time.Sleep(2 * injectPollInterval)
	door.TurnFinished(key)
	select {
	case got := <-late:
		if got.ok {
			t.Fatal("the message was handed over with less than a turn of its bound left")
		}
		if got.elapsed > injectSubmitWait/4 {
			t.Fatalf("the refusal took %s; it should follow the earlier turn's end, not the deadline", got.elapsed)
		}
	case <-time.After(injectSubmitWait / 4):
		t.Fatal("the claim did not return once the earlier turn ended")
	}
	// The same message with a whole bound left is handed over at once.
	if _, ok := door.claimTurn(ctx, key, time.Now().Add(injectSubmitWait)); !ok {
		t.Fatal("a claim with a whole bound left behind an ended turn must be handed over")
	}
}

// TestInjectAReMintedConversationKeepsItsOutstandingTurn: the one-turn-at-a-
// time guard has to survive an eviction under a running turn the way the
// sequence does. Re-minted with nothing handed over, the old turn's end would
// count on the new incarnation as a turn it never handed, and two overlapping
// messages would both be handed over, the second answered with the first's
// task and routed as a steer onto it.
func TestInjectAReMintedConversationKeepsItsOutstandingTurn(t *testing.T) {
	f := startFakeDoor(t, func(InboundMessage) {})
	const key = injectKeyPrefix + "evicted-mid-turn"
	ctx := context.Background()
	if _, ok := f.door.claimTurn(ctx, key, time.Now().Add(injectSubmitWait)); !ok {
		t.Fatal("the first claim on a quiet conversation must succeed")
	}
	f.evictEverything(t, "evicted-mid-turn")
	// The old turn ends after the eviction; its end is what re-mints the key.
	f.door.TurnFinished(key)
	if _, ok := f.door.claimTurn(ctx, key, time.Now().Add(injectSubmitWait)); !ok {
		t.Fatal("a claim behind an ended turn must be handed over")
	}
	// A second message while that turn runs must wait for it, not be handed
	// over beside it.
	second := make(chan bool, 1)
	go func() {
		_, ok := f.door.claimTurn(ctx, key, time.Now().Add(injectSubmitWait))
		second <- ok
	}()
	select {
	case ok := <-second:
		t.Fatalf("a second message was handed over (%v) while the first's turn was still running: the guard did not survive the eviction", ok)
	case <-time.After(4 * injectPollInterval):
	}
	f.door.TurnFinished(key)
	select {
	case ok := <-second:
		if !ok {
			t.Fatal("the second message was refused once the first turn ended")
		}
	case <-time.After(injectSubmitWait / 4):
		t.Fatal("the second claim did not return once the first turn ended")
	}
}

// TestInjectOneBoundCoversTheClaimAndTheWait: a duplicate POST waits one
// submit bound from its own arrival for the first POST's answer. Were the
// claim (waiting for an earlier turn to end) and the wait each given a whole
// bound, the first POST could answer up to two bounds after it arrived, the
// duplicate would have given up and told its caller nothing started, and the
// turn would then accept with nobody watching. So the request sets one
// deadline at its arrival and both take it. Pinned at the seam, with a short
// deadline: the bound itself is seventy seconds.
func TestInjectOneBoundCoversTheClaimAndTheWait(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ln.Close()
	door, err := NewInjectAdapter(ln.Addr().String(), injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	const key = injectKeyPrefix + "one-bound"
	ctx := context.Background()
	// A turn handed over and never ended.
	if _, ok := door.claimTurn(ctx, key, time.Now().Add(injectSubmitWait)); !ok {
		t.Fatal("the first claim on a quiet conversation must succeed")
	}
	deadline := time.Now().Add(4 * injectPollInterval)
	started := time.Now()
	if _, ok := door.claimTurn(ctx, key, deadline); ok {
		t.Fatal("a claim behind an unfinished turn must fail at the deadline, not succeed")
	}
	if elapsed := time.Since(started); elapsed > injectSubmitWait/4 {
		t.Fatalf("the claim held for %s past a %s deadline; it took a bound of its own", elapsed, 4*injectPollInterval)
	}
	// The wait takes the same deadline rather than minting its own.
	prior := door.counts(key)
	started = time.Now()
	_, _, refusal := door.awaitTurn(ctx, key, prior, time.Now().Add(4*injectPollInterval))
	if refusal != injectRefusalNoAnswer {
		t.Fatalf("refusal = %q, want %s at the passed deadline", refusal, injectRefusalNoAnswer)
	}
	if elapsed := time.Since(started); elapsed > injectSubmitWait/4 {
		t.Fatalf("the wait held for %s past its deadline; it took a bound of its own", elapsed)
	}
	started = time.Now()
	published, note, refusal := door.awaitCancel(ctx, key, prior, time.Now().Add(4*injectPollInterval))
	if published || note == "" || refusal != injectRefusalNoAnswer {
		t.Fatalf("awaitCancel = (%v, %q, %q) at the passed deadline, want an unpublished %s",
			published, note, refusal, injectRefusalNoAnswer)
	}
	if elapsed := time.Since(started); elapsed > injectSubmitWait/4 {
		t.Fatalf("the cancel's wait held for %s past its deadline; it took a bound of its own", elapsed)
	}
}
