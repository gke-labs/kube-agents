package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
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
	_ Adapter      = (*InjectAdapter)(nil)
	_ TaskObserver = (*InjectAdapter)(nil)
)

const (
	// The mapped and unmapped authors in the rig's principal map, matching
	// the fixture startRig uses.
	injectTestAuthor        = "1001"
	injectTestPrincipal     = "test:bnaylor"
	injectTestUnknownAuthor = "9999"
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

	mapFile := filepath.Join(t.TempDir(), "principal-map")
	fixture := fmt.Sprintf("%s %s\n", injectTestAuthor, injectTestPrincipal)
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
	adapter, err := NewInjectAdapter(ln.Addr().String(), nil)
	if err != nil {
		t.Fatalf("NewInjectAdapter: %v", err)
	}
	adapter.listener = ln

	cfg := &Config{
		NATSURL:          url,
		PrincipalMapPath: mapFile,
		InjectListen:     ln.Addr().String(),
		DefaultAddressee: "platform",
		IdleTTL:          30 * time.Minute,
		AttributionSalt:  []byte("test-salt"),
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

// inject posts one message and returns the decoded reply.
func (r *injectRig) inject(t *testing.T, conversation, author, text string) injectResponse {
	t.Helper()
	body, err := json.Marshal(injectRequest{Conversation: conversation, Author: author, Text: text})
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(r.base+injectPath, "application/json", bytes.NewReader(body))
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
	resp, err := http.Get(target)
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

// TestInjectAuthorityNamesTheNetworkEdge: the authority block is the audit
// record, and on this backend it must not claim a verification that did not
// happen. The principal map resolved WHO, and nothing authenticated that the
// caller is that author -- so verifiedBy says the network edge, and the
// identifiers are pseudonymized exactly as every other backend's are.
func TestInjectAuthorityNamesTheNetworkEdge(t *testing.T) {
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
		t.Fatalf("verifiedBy = %q, want %q -- the map resolved the author, nothing authenticated it",
			authority.Requester.VerifiedBy, injectVerifiedBy)
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

// TestInjectGradesAFailedTaskRatherThanHidingIt: a task an executor took and
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
	// Waited for rather than read once: POST /inject returns as soon as the
	// task is announced, which startTask does before it writes the record, so
	// an immediate read races the turn's own KV write.
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
		{"foreign backend", "gchat:spaces/AAA/threads/BBB", "", true},
		{"newline", "case\n1", "", true},
		{"too long", strings.Repeat("x", injectMaxKeyRunes+1), "", true},
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
	} {
		t.Run(tc.name, func(t *testing.T) {
			resp, err := http.Post(r.base+injectPath, "application/json", strings.NewReader(tc.body))
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
	resp, err := http.Get(r.base + injectPath)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("GET /inject = %d, want 405", resp.StatusCode)
	}
	postResp, err := http.Post(r.base+conversationsPath+"case-1", "application/json", strings.NewReader("{}"))
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
	adapter, err := NewInjectAdapter("127.0.0.1:0", nil)
	if err != nil {
		t.Fatal(err)
	}
	key := injectKeyPrefix + "sources"
	adapter.TaskStarted(key, "task-executor")
	adapter.TaskTerminal(key, "task-executor", lib.StateFailed, TerminalFromExecutor)
	adapter.TaskStarted(key, "task-gateway")
	adapter.TaskTerminal(key, "task-gateway", lib.StateFailed, TerminalFromGateway)

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
	inject := &InjectAdapter{}
	if _, ok := any(inject).(TaskObserver); !ok {
		t.Error("the inject adapter does not implement TaskObserver, so POST /inject can never return a task id")
	}
}

// TestInjectBoundsWhatItRetains: the transcript is bounded memory on a
// long-lived pod, and the bound must not break the polling contract -- a
// reader's sequence still advances monotonically once the oldest entries have
// been dropped.
func TestInjectBoundsWhatItRetains(t *testing.T) {
	adapter, err := NewInjectAdapter("127.0.0.1:0", nil)
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
