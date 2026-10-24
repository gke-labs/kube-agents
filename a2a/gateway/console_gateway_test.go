package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A console turn on a gateway configured for discord: the backend on the
// authority block is console, the principal is the fixed console principal,
// and no principal-map entry was needed.
func TestConsoleTurnCarriesTheConsoleBackendAndPrincipal(t *testing.T) {
	r := startRig(t) // gateway_test.go:171; Backend: "discord", the map holds 1001 only
	// startRig's fakeAdapter stands in for whichever backend is configured
	// and its default roster ("1001") is that backend's, not the console's;
	// a console conversation is a 1:1 DM, so the room is just the sender
	// (mux_test.go:50 sets the same field the same way for the same reason).
	// Backend is set on each message below because that is what the console
	// adapter stamps (ConsoleAdapter.inbound); the fake does not derive it
	// from the conversation id, and neither does the gateway.
	r.adapter.roster = []string{consoleAuthor}
	r.adapter.inbox <- InboundMessage{
		Conversation: "console:tab-9", Kind: "dm", Backend: consoleBackend,
		AuthorID: consoleAuthor, MessageID: "m1", Text: "console hello",
	}
	var auth Authority
	waitFor(t, "the console submission", func() bool {
		env, a := latestSubmissionOrNil(r.bus)
		if env == nil {
			return false
		}
		var m lib.Message
		if json.Unmarshal(env.Payload, &m) != nil || joinTextParts(m.Parts) != "console hello" {
			return false
		}
		auth = a
		return true
	})
	if auth.Requester.Backend != consoleBackend {
		t.Errorf("backend = %q, want %q", auth.Requester.Backend, consoleBackend)
	}
	if auth.Requester.VerifiedBy != consoleVerifiedBy {
		t.Errorf("verifiedBy = %q, want %q", auth.Requester.VerifiedBy, consoleVerifiedBy)
	}
	if want := r.g.ps.Hash(consolePrincipal); auth.Requester.Principal != want {
		t.Errorf("principal = %q, want the hashed console principal", auth.Requester.Principal)
	}
	if auth.Audience.Kind != "dm" || len(auth.Audience.Roster) != 1 {
		t.Errorf("audience = %+v", auth.Audience)
	}
	// The requester is always present in its own audience snapshot
	// (spec-chatops-gateway.md, "Roster"). Console roster ids arrive as
	// "console" and the principal map does not know that name, so before
	// rosterResolver the snapshot held H("console") against a requester
	// principal of H("nats:console") - the one backend where the invariant
	// forked. Comparing the two is what pins it; a length check cannot.
	if !slices.Contains(auth.Audience.Roster, auth.Requester.Principal) {
		t.Errorf("requester %q absent from its own roster %v",
			auth.Requester.Principal, auth.Audience.Roster)
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "can't verify") {
			t.Errorf("console sender was dropped as unverified: %q", p)
		}
	}
}

// Review focus 5: a discord turn on the same gateway still goes through the
// principal map and still says discord.
func TestDiscordTurnStillResolvesThroughTheMapAfterThePerMessageBackend(t *testing.T) {
	r := startRig(t)
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread-pm", Kind: "group",
		AuthorID: "1001", MessageID: "m2", Text: "discord hello",
	}
	var auth Authority
	waitFor(t, "the discord submission", func() bool {
		env, a := latestSubmissionOrNil(r.bus)
		if env == nil {
			return false
		}
		var m lib.Message
		if json.Unmarshal(env.Payload, &m) != nil || joinTextParts(m.Parts) != "discord hello" {
			return false
		}
		auth = a
		return true
	})
	if auth.Requester.Backend != "discord" || auth.Requester.VerifiedBy != "principal-map" {
		t.Errorf("requester = %+v", auth.Requester)
	}
	// And an unmapped discord sender is still dropped with the map remedy.
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread-pm2", Kind: "group",
		AuthorID: "9999", MessageID: "m3", Text: "who am i",
	}
	waitFor(t, "the drop notice", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "can't verify who you are on discord") && strings.Contains(p, "principal map") {
				return true
			}
		}
		return false
	})
}

// A console author id arriving on a NON-console conversation must not get
// the console principal: the fixed principal is only as good as the grant,
// and the grant is on the console subject, not on the string "console".
func TestTheConsolePrincipalIsBoundToTheConsoleConversation(t *testing.T) {
	r := startRig(t)
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread-spoof", Kind: "group",
		AuthorID: consoleAuthor, MessageID: "m4", Text: "spoof",
	}
	waitFor(t, "the drop notice", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "can't verify who you are on discord") {
				return true
			}
		}
		return false
	})
}

func latestSubmissionOrNil(bus *lib.Client) (*lib.Envelope, Authority) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	env, err := bus.ReadTopicLatest(ctx, "TASKS", "a2a.tasks.platform.*.in")
	if err != nil || env == nil {
		return nil, Authority{}
	}
	var a Authority
	_ = json.Unmarshal(env.Authority, &a)
	return env, a
}

// With the console running beside the configured backend, an empty
// principal map no longer drops every inbound message, only the configured
// backend's: the warning names that backend.
func TestEmptyPrincipalMapWarningNamesTheBackend(t *testing.T) {
	s := startServer(t)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("gateway-test"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	logs := &lockedBuffer{}
	cfg := &Config{
		NATSURL:          s.ClientURL(),
		PrincipalMapPath: filepath.Join(t.TempDir(), "absent"),
		DefaultAddressee: "platform",
		IdleTTL:          30 * time.Minute,
		AttributionSalt:  []byte("test-salt"),
	}
	if _, err := New(Options{Client: client, Adapter: newFakeAdapter(), Config: cfg, Backend: "discord", Logger: slog.New(slog.NewTextHandler(logs, nil))}); err != nil {
		t.Fatal(err)
	}
	if want := "principal map is empty; every discord message will be dropped at verification"; !strings.Contains(logs.String(), want) {
		t.Errorf("log lacks %q:\n%s", want, logs.String())
	}
}

// The console page renders answers off TASKS, so the deliverable never goes
// out as a notice on chat.console.<token>.out. Here the backend takes no
// terminal frames, so the answer goes nowhere on the console subject at all.
func TestConsoleTerminalAnswerStaysOffTheNoticeSubject(t *testing.T) {
	r := startRig(t)
	r.adapter.roster = []string{consoleAuthor}
	r.adapter.inbox <- InboundMessage{
		Conversation: "console:tab-7", Kind: "dm", Backend: consoleBackend,
		AuthorID: consoleAuthor, MessageID: "m1", Text: "how is the fleet",
	}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()

	const answer = "the fleet is fine"
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: answer}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	// Anchor on the rolling line reaching completed before asserting the
	// absence - without it this test passes just as well when the terminal
	// was never processed at all.
	waitFor(t, "the rolling line to reach completed", func() bool {
		edits := r.adapter.editTexts()
		return len(edits) > 0 && strings.Contains(edits[len(edits)-1], string(lib.StateCompleted))
	})
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, answer) {
			t.Errorf("the answer was posted as a console notice: %q", p)
		}
	}
}

// Failures are notices, not answers, so keeping the answer off the notice
// subject must not take them with it: a console page has no other way to
// learn the task died. The notice goes out whatever the backend does with
// terminal frames; this one takes none.
func TestConsoleStillGetsTerminalNotices(t *testing.T) {
	r := startRig(t)
	r.adapter.roster = []string{consoleAuthor}
	r.adapter.inbox <- InboundMessage{
		Conversation: "console:tab-8", Kind: "dm", Backend: consoleBackend,
		AuthorID: consoleAuthor, MessageID: "m1", Text: "how is the fleet",
	}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateFailed, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the failure notice on the console", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "the task failed") {
				return true
			}
		}
		return false
	})
}

// A console conversation takes the answer as a terminal frame naming its
// task, so a console client (the MCP server) reads it there. It is
// still never a notice.
func TestAConsoleAnswerIsATerminalFrame(t *testing.T) {
	r := startRig(t)
	r.adapter.setTakesTerminals()
	r.adapter.roster = []string{consoleAuthor}
	r.adapter.inbox <- InboundMessage{
		Conversation: "console:tab-9", Kind: "dm", Backend: consoleBackend,
		AuthorID: consoleAuthor, MessageID: "m1", Text: "how is the fleet",
	}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	const answer = "the fleet is fine"
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: answer}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the terminal frame", func() bool { return len(r.adapter.terminalPosts()) > 0 })
	got := r.adapter.terminalPosts()
	want := fakeTerminal{"console:tab-9", ConsoleTerminal{TaskID: origin.TaskID, Addressee: "platform", State: lib.StateCompleted, Text: answer}}
	if len(got) != 1 || got[0] != want {
		t.Fatalf("terminal frames = %+v, want exactly %+v", got, want)
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, answer) {
			t.Errorf("the answer was also posted as a console notice: %q", p)
		}
	}
}

// A turn that handed work to kanban cards (the bridge's handoff artifact)
// says so on its frame, so a console client waits for the cards' reports;
// one that did not leaves the field off (TestAConsoleAnswerIsATerminalFrame
// pins that), and the client need not wait.
func TestAConsoleTurnThatFiledCardsSaysSoOnItsFrame(t *testing.T) {
	r := startRig(t)
	r.adapter.setTakesTerminals()
	r.adapter.roster = []string{consoleAuthor}
	r.adapter.inbox <- InboundMessage{
		Conversation: "console:tab-cards", Kind: "dm", Backend: consoleBackend,
		AuthorID: consoleAuthor, MessageID: "m1", Text: "how is the fleet",
	}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactHandoff, Parts: []lib.Part{{Kind: "text", Text: lib.HandoffCards}}}); err != nil {
		t.Fatal(err)
	}
	completeTask(t, exec, "checking the fleet.")
	waitFor(t, "the terminal frame", func() bool { return len(r.adapter.terminalPosts()) > 0 })
	if got := r.adapter.terminalPosts()[0]; !got.CardsPending || got.TaskID != origin.TaskID {
		t.Fatalf("terminal frame = %+v, want cardsPending for %s", got, origin.TaskID)
	}
}

// A failure on a console conversation is a terminal frame too, carrying the
// notice text.
func TestAConsoleFailureIsATerminalFrame(t *testing.T) {
	r := startRig(t)
	r.adapter.setTakesTerminals()
	r.adapter.roster = []string{consoleAuthor}
	r.adapter.inbox <- InboundMessage{
		Conversation: "console:tab-10", Kind: "dm", Backend: consoleBackend,
		AuthorID: consoleAuthor, MessageID: "m1", Text: "how is the fleet",
	}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateFailed, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the terminal frame", func() bool { return len(r.adapter.terminalPosts()) > 0 })
	got := r.adapter.terminalPosts()
	want := fakeTerminal{"console:tab-10", ConsoleTerminal{TaskID: origin.TaskID, Addressee: "platform", State: lib.StateFailed, Text: "❌ the task failed"}}
	if len(got) != 1 || got[0] != want {
		t.Fatalf("terminal frames = %+v, want exactly %+v", got, want)
	}
	// The page still gets the gateway's notice, which carries no kind.
	waitFor(t, "the failure notice", postedContaining(r, "the task failed"))
}

// The heal tells a console client the end it finds, as it tells every
// observer: a task whose terminal the relay missed still reaches .out. Here
// the relay saw it too, so the heal's frame repeats the relay's for the same
// task, which a client drops on its repeated id.
func TestAHealedConsoleTaskIsAFrame(t *testing.T) {
	r := startRig(t)
	r.adapter.setTakesTerminals()
	r.adapter.roster = []string{consoleAuthor}
	conv := "console:tab-heal"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", Backend: consoleBackend, AuthorID: consoleAuthor, MessageID: "m1", Text: "start"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	completeTask(t, exec, "done")
	waitFor(t, "the relay's frame", func() bool { return len(r.adapter.terminalPosts()) == 1 })

	// Fake the relay having missed the terminal: restore ActiveTask in KV,
	// the state a transient KV failure on the final event leaves.
	ctx := context.Background()
	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil || rec == nil {
		t.Fatal(err)
	}
	rec.ActiveTask = &ActiveTask{TaskID: origin.TaskID, CorrelationID: origin.CorrelationID}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", Backend: consoleBackend, AuthorID: consoleAuthor, MessageID: "m2", Text: "next question"}
	waitFor(t, "the heal's frame", func() bool { return len(r.adapter.terminalPosts()) == 2 })
	got := r.adapter.terminalPosts()
	if got[1] != got[0] || got[1].TaskID != origin.TaskID || got[1].Text != "done" {
		t.Fatalf("heal frame = %+v, want a repeat of %+v", got[1], got[0])
	}
}

// A delegation chain ends once toward a console client: no frame for the turn
// that delegated or for its child, and one for the wake's end, under the task
// the turn started (observedAs), as every observer is told.
func TestAConsoleDelegationChainEndsInOneFrame(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	r.adapter.setTakesTerminals()
	conv := consoleKeyPrefix + "t-chain"
	origin, session, child := delegated(t, r, spawn, conv, "")
	completeTask(t, r.execFor(t, child, targetPlatform), "fleet is green")
	waitFor(t, "wake spawn", func() bool { return len(spawn.calls()) == 2 })
	wakeSession := spawn.calls()[1].Session
	wake := r.awaitTask(t, wakeSession)
	completeTask(t, r.execFor(t, wake, wakeSession), "the fleet is green, all three clusters")
	waitFor(t, "the chain's frame", func() bool { return len(r.adapter.terminalPosts()) > 0 })
	got := r.adapter.terminalPosts()
	want := fakeTerminal{conv, ConsoleTerminal{TaskID: origin.TaskID, Addressee: session, State: lib.StateCompleted, Text: "the fleet is green, all three clusters"}}
	if len(got) != 1 || got[0] != want {
		t.Fatalf("terminal frames = %+v, want exactly %+v", got, want)
	}
}

// A session turn whose delegate request minted no child ends failed with the
// reason, though its executor said completed: the hand-off line ("delegated
// to ...") is never an answer (handOffEnd).
func TestAConsoleHandOffWithNoChildIsAFailedFrame(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	r.adapter.setTakesTerminals()
	conv := consoleKeyPrefix + "t-handoff"
	exec, origin, _ := sessionTurnVia(t, r, spawn, conv, "", "how is the fleet?")
	if err := exec.PublishArtifact(context.Background(), delegateArtifact(t, "nobody-here", "report fleet health")); err != nil {
		t.Fatal(err)
	}
	// The refusal's notice follows the turn's answer, so wait on the record.
	waitFor(t, "the refusal on the record", func() bool {
		rec, _ := r.g.reg.Get(context.Background(), conv)
		ref, _ := rec.TaskRefFor(origin.TaskID)
		return ref.DelegationEnd != ""
	})
	completeTask(t, exec, "delegated to nobody-here")
	waitFor(t, "the frame", func() bool { return len(r.adapter.terminalPosts()) > 0 })
	got := r.adapter.terminalPosts()
	if len(got) != 1 || got[0].TaskID != origin.TaskID || got[0].State != lib.StateFailed ||
		!strings.HasPrefix(got[0].Text, "❌ failed: ") || !strings.Contains(got[0].Text, "delegation refused") ||
		strings.Contains(got[0].Text, "delegated to nobody-here") {
		t.Fatalf("terminal frames = %+v, want one failed frame for %s with the reason", got, origin.TaskID)
	}
}

// A chat conversation's terminals are notices as before: the relay asks for
// a terminal frame only on a console conversation.
func TestAChatConversationGetsNoTerminalFrames(t *testing.T) {
	r := startRig(t)
	r.adapter.setTakesTerminals()
	conv := "discord:g1/c-terminal"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m1", Text: "how is the fleet"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	completeTask(t, exec, "the fleet is fine")
	waitFor(t, "the answer posted", postedContaining(r, "the fleet is fine"))
	if got := r.adapter.terminalPosts(); len(got) != 0 {
		t.Fatalf("a chat conversation got terminal frames: %+v", got)
	}
}

// A console answer relayed before a restart and completed after it is read
// back from the stream: the render state that held it is gone, and the frame
// carries the answer, not the non-text stand-in.
func TestAConsoleAnswerSurvivesLostRenderState(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	r.adapter.roster = []string{consoleAuthor}
	conv := "console:tab-lost"
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "dm", Backend: consoleBackend,
		AuthorID: consoleAuthor, MessageID: "m1", Text: "how is the fleet",
	}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	r2, _ := restartRig(t, r, func() {
		if err := exec.PublishArtifact(context.Background(), lib.Artifact{ArtifactID: "a-r", Name: lib.ArtifactResult,
			Parts: []lib.Part{{Kind: "text", Text: "fleet is green"}}}); err != nil {
			t.Fatal(err)
		}
		drainRelayDurable(t, r.url)
	})
	r2.adapter.setTakesTerminals()
	if err := exec.PublishStatus(context.Background(), lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the terminal frame", func() bool { return len(r2.adapter.terminalPosts()) > 0 })
	if got := r2.adapter.terminalPosts()[0]; got.Text != "fleet is green" || got.TaskID != origin.TaskID {
		t.Fatalf("terminal frame = %+v, want the replayed answer for task %s", got, origin.TaskID)
	}
}

// A console conversation with consoleQueueCap turns already waiting refuses
// the next one, logs it, and posts one notice to the conversation however
// many more arrive. A chat backend's turns are not bounded.
func TestConsoleTurnsPastTheQueueCapAreDroppedWithOneNotice(t *testing.T) {
	adapter := newFakeAdapter()
	logs := &lockedBuffer{}
	g := &Gateway{adapter: adapter, log: slog.New(slog.NewTextHandler(logs, nil))}
	release := make(chan struct{})
	var releaseOnce sync.Once
	t.Cleanup(func() { releaseOnce.Do(func() { close(release) }) })
	taken := make(chan struct{}, 1)
	var handled []string
	var mu sync.Mutex
	g.inbox = newKeyedQueue(func(_ string, batch []InboundMessage) {
		select {
		case taken <- struct{}{}:
		default:
		}
		<-release
		mu.Lock()
		for _, m := range batch {
			handled = append(handled, m.MessageID)
		}
		mu.Unlock()
	})
	const conv = "console:tab-q"
	turn := func(id string) InboundMessage {
		return InboundMessage{Conversation: conv, Kind: "dm", Backend: consoleBackend, MessageID: id, Text: "hi"}
	}
	g.enqueueInbound(turn("in-flight"))
	select {
	case <-taken:
	case <-time.After(5 * time.Second):
		t.Fatal("the worker never took the first turn")
	}
	for i := range consoleQueueCap {
		g.enqueueInbound(turn(fmt.Sprintf("w%d", i)))
	}
	if got := adapter.postTexts(); len(got) != 0 {
		t.Fatalf("posted before the cap was reached: %q", got)
	}
	g.enqueueInbound(turn("over-1"))
	g.enqueueInbound(turn("over-2"))

	posts := adapter.postTexts()
	if want := fmt.Sprintf(consoleQueueFullNotice, consoleQueueCap); len(posts) != 1 || posts[0] != want {
		t.Fatalf("posts = %q, want exactly [%q]", posts, want)
	}
	if n := strings.Count(logs.String(), `reason="queue full"`); n != 1 {
		t.Fatalf("queue-full log lines = %d, want 1:\n%s", n, logs.String())
	}
	if !strings.Contains(logs.String(), "messageId=over-1") {
		t.Errorf("the drop log does not name the first refused frame:\n%s", logs.String())
	}

	// The same depth on a chat backend's conversation is all accepted.
	for i := range consoleQueueCap + 2 {
		g.enqueueInbound(InboundMessage{Conversation: "discord:1/2", Kind: "dm", Backend: "discord", MessageID: fmt.Sprintf("d%d", i)})
	}
	releaseOnce.Do(func() { close(release) })
	want := 1 + consoleQueueCap + consoleQueueCap + 2
	deadline := time.Now().Add(5 * time.Second)
	for {
		mu.Lock()
		got := slices.Clone(handled)
		mu.Unlock()
		if len(got) >= want {
			if len(got) != want || slices.Contains(got, "over-1") || slices.Contains(got, "over-2") {
				t.Fatalf("handled = %q, want the in-flight turn, %d console and %d discord turns, no over-*", got, consoleQueueCap, consoleQueueCap+2)
			}
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("handled %d of %d turns: %q", len(got), want, got)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// TestAnUnaddressedMessageFromAnUnverifiedSenderPostsNoNotice: a reply typed
// under one of the bot's posts without a mention (Unaddressed) is often people
// talking to each other. An unverified sender there is dropped without the
// public refusal, and is not counted as notified, so their first real ask
// still gets it (gke-labs/kube-agents#3246).
func TestAnUnaddressedMessageFromAnUnverifiedSenderPostsNoNotice(t *testing.T) {
	r := startRig(t)
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/alert", Kind: "group",
		AuthorID: "9999", MessageID: "u1", Text: "looks like the db again", Unaddressed: true,
	}
	// Same conversation, so the same queue: once this sentinel is submitted,
	// the unaddressed message ahead of it has been decided.
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/alert", Kind: "group",
		AuthorID: "1001", MessageID: "u2", Text: "sentinel ask",
	}
	waitFor(t, "the sentinel submission", func() bool {
		env, _ := latestSubmissionOrNil(r.bus)
		if env == nil {
			return false
		}
		var m lib.Message
		return json.Unmarshal(env.Payload, &m) == nil && joinTextParts(m.Parts) == "sentinel ask"
	})
	for _, p := range r.adapter.postTextsFor("discord:g1/alert") {
		if strings.Contains(p, "can't verify who you are") {
			t.Fatalf("an unaddressed message got the public refusal: %q", p)
		}
	}
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/ask", Kind: "group",
		AuthorID: "9999", MessageID: "u3", Text: "who am i",
	}
	waitFor(t, "the refusal on the sender's first real ask", func() bool {
		for _, p := range r.adapter.postTextsFor("discord:g1/ask") {
			if strings.Contains(p, "can't verify who you are on discord") {
				return true
			}
		}
		return false
	})
}

// The console's stash gets an entry only when a terminal will take it: a
// task whose terminal was already observed (the heal reached it before the
// relay) puts nothing, so the relay's late delivery leaves nothing behind.
func TestAConsoleDeliveryAfterAnObservedTerminalIsNotStashed(t *testing.T) {
	g := &Gateway{}
	for _, tc := range []struct {
		observed bool
		want     bool
	}{{false, true}, {true, false}} {
		rec := &SessionRecord{Key: "console:t-stash", Tasks: []TaskRef{{ID: "task-1", TerminalObserved: tc.observed}}}
		g.observeDelivered(rec, "task-1", "the answer")
		if _, got := g.consoleResults.take("task-1"); got != tc.want {
			t.Errorf("terminal observed %v: stashed %v, want %v", tc.observed, got, tc.want)
		}
	}
}

// At the cap the stash makes room by evicting one entry, not by clearing
// every deliverable another worker may be about to take.
func TestTheConsoleStashEvictsOneEntryAtTheCap(t *testing.T) {
	var c consoleResultStash
	for i := 0; i < consoleResultStashCap; i++ {
		c.put(fmt.Sprintf("task-%d", i), "r")
	}
	c.put("task-new", "r")
	if len(c.m) != consoleResultStashCap {
		t.Fatalf("stash holds %d after a put at the cap, want %d", len(c.m), consoleResultStashCap)
	}
	if _, ok := c.take("task-new"); !ok {
		t.Fatal("the new entry was not kept")
	}
	// Putting a key already held at the cap evicts nothing.
	c.put("task-new", "r")
	c.put("task-new", "r2")
	if len(c.m) != consoleResultStashCap {
		t.Fatalf("re-putting a held key changed the size to %d", len(c.m))
	}
}

// A card filed in a delegated child's turn names the child in its reply_to;
// the console client was told of the chain only under its root, so the
// report's replyTo is mapped to the root. A task the record does not hold
// passes through unchanged.
func TestAConsoleReportFromADelegatedChildRepliesToTheRoot(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := consoleKeyPrefix + "t-replyto"
	origin, _, child := delegated(t, r, spawn, conv, "")
	conversations := r.g.Conversations(r.adapter).(gatewayConversations)
	ctx := context.Background()
	if got := conversations.ReplyToRoot(ctx, conv, child.TaskID); got != origin.TaskID {
		t.Errorf("the child's reply_to maps to %q, want the root %q", got, origin.TaskID)
	}
	if got := conversations.ReplyToRoot(ctx, conv, "task-unknown"); got != "task-unknown" {
		t.Errorf("an unknown task maps to %q, want it unchanged", got)
	}
	if got := conversations.ReplyToRoot(ctx, conv, origin.TaskID); got != origin.TaskID {
		t.Errorf("the root maps to %q, want itself", got)
	}
}

// An end the gateway declares itself carries no executor reason; its frame
// says what the page's notice says (the bus was unreachable, or the heal
// released a task that produced nothing), not the bare "the task failed".
// An executor's failure without a reason keeps the bare line.
func TestAConsoleGatewayDeclaredEndIsWordedBySource(t *testing.T) {
	a := newFakeAdapter()
	a.setTakesTerminals()
	g := &Gateway{adapter: a, log: slog.Default()}
	rec := &SessionRecord{Key: "console:t-source", Tasks: []TaskRef{{ID: "task-1", Addressee: "platform"}}}
	for _, tc := range []struct {
		source TerminalSource
		want   string
	}{
		{TerminalFromGateway, noticeBusUnreachable},
		{TerminalNeverStarted, consoleNeverStartedText},
		{TerminalFromExecutor, "❌ the task failed"},
	} {
		g.consoleFrame(rec, "task-1", lib.StateFailed, tc.source, "")
		got := a.terminalPosts()
		if last := got[len(got)-1]; last.Text != tc.want {
			t.Errorf("source %v: frame text %q, want %q", tc.source, last.Text, tc.want)
		}
	}
}

// The cards are usually filed in a delegated child's turn: the child's
// handoff marks the chain's root, and the chain's one frame (the wake's end,
// under the root) carries cardsPending.
func TestAConsoleChainWhoseChildFiledCardsSaysSoOnItsFrame(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	r.adapter.setTakesTerminals()
	conv := consoleKeyPrefix + "t-chain-cards"
	origin, _, child := delegated(t, r, spawn, conv, "")
	cexec := r.execFor(t, child, targetPlatform)
	if err := cexec.PublishArtifact(context.Background(), lib.Artifact{Name: lib.ArtifactHandoff, Parts: []lib.Part{{Kind: "text", Text: lib.HandoffCards}}}); err != nil {
		t.Fatal(err)
	}
	completeTask(t, cexec, "checking the fleet.")
	waitFor(t, "wake spawn", func() bool { return len(spawn.calls()) == 2 })
	wakeSession := spawn.calls()[1].Session
	wake := r.awaitTask(t, wakeSession)
	completeTask(t, r.execFor(t, wake, wakeSession), "platform is checking the fleet; its report will follow.")
	waitFor(t, "the chain's frame", func() bool { return len(r.adapter.terminalPosts()) > 0 })
	got := r.adapter.terminalPosts()
	if len(got) != 1 || got[0].TaskID != origin.TaskID || !got[0].CardsPending {
		t.Fatalf("terminal frames = %+v, want one for %s with cardsPending", got, origin.TaskID)
	}
}

// The heal reads the task's fold from the stream, handoff included, so a
// frame it produces carries cardsPending as the relay's does.
func TestAHealedConsoleTaskThatFiledCardsSaysSo(t *testing.T) {
	r := startRig(t)
	r.adapter.setTakesTerminals()
	r.adapter.roster = []string{consoleAuthor}
	conv := "console:tab-heal-cards"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", Backend: consoleBackend, AuthorID: consoleAuthor, MessageID: "m1", Text: "start"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactHandoff, Parts: []lib.Part{{Kind: "text", Text: lib.HandoffCards}}}); err != nil {
		t.Fatal(err)
	}
	completeTask(t, exec, "checking.")
	waitFor(t, "the relay's frame", func() bool { return len(r.adapter.terminalPosts()) == 1 })

	// Fake the relay having missed the terminal, as the heal finds it.
	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil || rec == nil {
		t.Fatal(err)
	}
	rec.ActiveTask = &ActiveTask{TaskID: origin.TaskID, CorrelationID: origin.CorrelationID}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", Backend: consoleBackend, AuthorID: consoleAuthor, MessageID: "m2", Text: "next question"}
	waitFor(t, "the heal's frame", func() bool { return len(r.adapter.terminalPosts()) == 2 })
	if got := r.adapter.terminalPosts()[1]; !got.CardsPending || got.TaskID != origin.TaskID {
		t.Fatalf("the heal's frame = %+v, want cardsPending for %s", got, origin.TaskID)
	}
}
