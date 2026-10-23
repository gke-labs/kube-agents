package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	fakeAddressee = "platform"
	otherSession  = "session-x"
	statusQuery   = "status?"
	answerText    = "Three pods are crash-looping in `checkout`."
	wakeAnswer    = "The cluster agent found the bad rollout."
	forgedAnswer  = "FORGED: everything is fine."
	ackText       = "checking the cluster."
)

var fakeGateway = lib.Party{Session: gatewaySupervisor, AgentType: "a2a-gateway"}

func executor(addressee string) lib.Party { return lib.Party{Session: addressee, Profile: addressee} }

func mustJSON(t *testing.T, v any) json.RawMessage {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func authorityFor(t *testing.T, conversation, viaTask string) json.RawMessage {
	t.Helper()
	a := map[string]any{
		"requester": map[string]any{"principal": "nats:console", "backend": "console"},
		"audience":  map[string]any{"conversation": conversation, "kind": "dm"},
		"grants":    []any{},
	}
	if viaTask != "" {
		a["via"] = map[string]any{"taskId": viaTask, "session": fakeAddressee}
	}
	return mustJSON(t, a)
}

func pub(t *testing.T, js jetstream.JetStream, subject string, env *lib.Envelope) {
	t.Helper()
	if _, err := js.Publish(context.Background(), subject, mustJSON(t, env)); err != nil {
		t.Fatal(err)
	}
}

// publishSubmission puts a submission on TASKS as the gateway would.
func publishSubmission(t *testing.T, js jetstream.JetStream, addressee, taskID, conversation, text, viaTask string) {
	t.Helper()
	msg := mustJSON(t, lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: text}}, MessageID: "m-" + taskID})
	env, err := lib.NewMessageEnvelope(fakeGateway, taskID, "ctx-"+taskID, "corr-"+taskID, msg, lib.WithAuthority(authorityFor(t, conversation, viaTask)))
	if err != nil {
		t.Fatal(err)
	}
	pub(t, js, lib.TaskInSubject(addressee, taskID), env)
}

func publishStatus(t *testing.T, js jetstream.JetStream, addressee, taskID string, state lib.TaskState, final bool) {
	t.Helper()
	p := mustJSON(t, lib.StatusUpdate{TaskID: taskID, ContextID: "ctx-" + taskID, Status: lib.TaskStatus{State: state}, Final: final})
	env, err := lib.NewStatusUpdateEnvelope(executor(addressee), taskID, "ctx-"+taskID, "corr-"+taskID, p)
	if err != nil {
		t.Fatal(err)
	}
	pub(t, js, lib.TaskEventsSubject(addressee, taskID), env)
}

func publishResult(t *testing.T, js jetstream.JetStream, addressee, taskID, text string) {
	t.Helper()
	publishArtifact(t, js, addressee, taskID, lib.ArtifactResult, text)
}

func publishArtifact(t *testing.T, js jetstream.JetStream, addressee, taskID, name, text string) {
	t.Helper()
	p := mustJSON(t, lib.ArtifactUpdate{TaskID: taskID, ContextID: "ctx-" + taskID,
		Artifact: lib.Artifact{Name: name, Parts: []lib.Part{{Kind: "text", Text: text}}}, LastChunk: true})
	env, err := lib.NewArtifactUpdateEnvelope(executor(addressee), taskID, "ctx-"+taskID, "corr-"+taskID, p)
	if err != nil {
		t.Fatal(err)
	}
	pub(t, js, lib.TaskEventsSubject(addressee, taskID), env)
}

// publishRun puts a task's events on TASKS: submitted, working and, when
// finish, the answer and completed.
func publishRun(t *testing.T, js jetstream.JetStream, addressee, taskID, answer string, finish bool) {
	t.Helper()
	publishStatus(t, js, addressee, taskID, lib.StateSubmitted, false)
	publishStatus(t, js, addressee, taskID, lib.StateWorking, false)
	if finish {
		publishResult(t, js, addressee, taskID, answer)
		publishStatus(t, js, addressee, taskID, lib.StateCompleted, true)
	}
}

// gatewayScenario says what the fake gateway does with an ordinary turn.
type gatewayScenario struct {
	finish   bool // the root task completes with answerText
	delegate bool // the root delegates; a child runs and a wake task carries the answer
	handoff  bool // the root acknowledges and hands its work to kanban cards (no wake task ever)
	decoys   bool // before the root: another conversation's turn, a delegated child here, an earlier turn's late submission, and a cancel here
	// lateStart delays the root's first event after its submission, as an
	// executor's cold start does.
	lateStart time.Duration
}

// runFakeGateway answers console frames the way the console door does: a
// status question or a stop gets a notice and no task; anything else gets a
// submission for the conversation, with the scenario's extras.
func runFakeGateway(t *testing.T, url string, js jetstream.JetStream, sc gatewayScenario) *atomic.Int32 {
	t.Helper()
	nc, err := nats.Connect(url, nats.UserInfo("admin", "admin"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	var n atomic.Int32
	_, err = nc.Subscribe("chat.console.*.in", func(m *nats.Msg) {
		token := strings.Split(m.Subject, ".")[2]
		conv := "console:" + token
		var f ConsoleInFrame
		if json.Unmarshal(m.Data, &f) != nil {
			return
		}
		notice := func(text string) {
			_ = nc.Publish(fmt.Sprintf(consoleOutSubject, token), mustJSON(t, ConsoleOutFrame{MessageID: "n1", Text: text}))
		}
		switch f.Text {
		case statusQuery:
			notice("Nothing is running in this conversation.")
			return
		case stopText:
			notice("Stopped the running task.")
			return
		}
		i := n.Add(1)
		// An earlier task's terminal, posted to the conversation as the
		// console relay does (kind "result"), is not a notice about this
		// turn and must not be taken for one.
		_ = nc.Publish(fmt.Sprintf(consoleOutSubject, token), mustJSON(t, ConsoleOutFrame{MessageID: "r-task-earlier", Text: "an earlier answer", Kind: "result"}))
		notice("Working on it…")
		root := fmt.Sprintf("task-root%d", i)
		text := f.Text
		if rest, ok := strings.CutPrefix(text, "delegate "); ok {
			text = rest
		}
		if sc.decoys {
			publishSubmission(t, js, fakeAddressee, fmt.Sprintf("task-other%d", i), "console:someone-else", text, "")
			publishSubmission(t, js, fakeAddressee, fmt.Sprintf("task-child%d", i), conv, text, "task-elsewhere")
			publishSubmission(t, js, fakeAddressee, fmt.Sprintf("task-late%d", i), conv, "an earlier question", "")
			cancel, _ := lib.NewCancelEnvelope(fakeGateway, "task-running", "ctx", "corr", lib.WithAuthority(authorityFor(t, conv, "")))
			pub(t, js, lib.TaskInSubject(fakeAddressee, "task-running"), cancel)
		}
		publishSubmission(t, js, fakeAddressee, root, conv, text, "")
		switch {
		case sc.delegate:
			publishRun(t, js, fakeAddressee, root, "", false)
			publishArtifact(t, js, fakeAddressee, root, lib.ArtifactDelegate, "cluster-a: look at checkout")
			publishStatus(t, js, fakeAddressee, root, lib.StateCompleted, true)
			child := fmt.Sprintf("task-deleg%d", i)
			publishSubmission(t, js, "cluster-a", child, conv, "look at checkout", root)
			publishRun(t, js, "cluster-a", child, "child notes", true)
			wake := fmt.Sprintf("task-wake%d", i)
			publishSubmission(t, js, fakeAddressee, wake, conv, "child finished", child)
			publishRun(t, js, fakeAddressee, wake, wakeAnswer, true)
		case sc.handoff:
			publishRun(t, js, fakeAddressee, root, "", false)
			publishArtifact(t, js, fakeAddressee, root, lib.ArtifactHandoff, lib.HandoffCards)
			publishResult(t, js, fakeAddressee, root, ackText)
			publishStatus(t, js, fakeAddressee, root, lib.StateCompleted, true)
		default:
			time.Sleep(sc.lateStart)
			publishRun(t, js, fakeAddressee, root, answerText, sc.finish)
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	return &n
}

func callTool(t *testing.T, s *Server, name string, args any) CallToolResult {
	t.Helper()
	return s.CallTool(context.Background(), name, mustJSON(t, args))
}

func turnServer(t *testing.T, sc gatewayScenario) (*Server, jetstream.JetStream, *atomic.Int32, *NATSBus) {
	t.Helper()
	url, js := testBus(t)
	n := runFakeGateway(t, url, js, sc)
	b := dial(t, url)
	convs := NewConversations()
	return mustServer(t, SendMessageTool(b, convs), TaskControlTool(b, convs)), js, n, b
}

func mustContain(t *testing.T, text string, want ...string) {
	t.Helper()
	for _, w := range want {
		if !strings.Contains(text, w) {
			t.Errorf("result does not contain %q:\n%s", w, text)
		}
	}
}

func mustNotContain(t *testing.T, text string, not ...string) {
	t.Helper()
	for _, n := range not {
		if strings.Contains(text, n) {
			t.Errorf("result contains %q:\n%s", n, text)
		}
	}
}

// A turn is matched to its own root task, by conversation and text, not to
// another conversation's, a delegated child, an earlier turn's late
// submission, or a cancel on the same conversation; and it is answered from
// TASKS, under its addressee.
func TestSendMessageAnswersFromTheTurnsOwnTask(t *testing.T) {
	s, _, _, _ := turnServer(t, gatewayScenario{finish: true, decoys: true})
	res := callTool(t, s, ToolSendMessage, map[string]any{"text": "what is broken?", "wait_seconds": 10})
	if res.IsError {
		t.Fatalf("send_message failed: %s", res.Content[0].Text)
	}
	text := res.Content[0].Text
	mustContain(t, text, fakeAddressee+"/task-root1", "completed", answerText, "Working on it")
	mustNotContain(t, text, "task-other1", "task-child1", "task-late1", "task-running", "an earlier answer")
}

// A delegate turn is matched under its stripped text, and the answer is taken
// from the wake task the delegation ends in, not the delegator's empty result.
func TestADelegationsAnswerComesFromTheWakeTask(t *testing.T) {
	s, _, _, _ := turnServer(t, gatewayScenario{delegate: true})
	res := callTool(t, s, ToolSendMessage, map[string]any{"text": "delegate check the rollout", "wait_seconds": 10})
	if res.IsError {
		t.Fatalf("send_message failed: %s", res.Content[0].Text)
	}
	mustContain(t, res.Content[0].Text, "answer came from a delegated task", fakeAddressee+"/task-wake1", wakeAnswer, "cluster-a/task-deleg1")
}

// A turn that hands its work to kanban cards completes with its
// acknowledgement, and send_message returns at once rather than following
// the handoff as if a wake task were coming: the cards report over the
// console door, never on TASKS.
func TestAHandoffToCardsIsNotFollowed(t *testing.T) {
	s, _, _, _ := turnServer(t, gatewayScenario{handoff: true})
	start := time.Now()
	res := callTool(t, s, ToolSendMessage, map[string]any{"text": "check the cluster", "wait_seconds": 20})
	if res.IsError {
		t.Fatalf("send_message failed: %s", res.Content[0].Text)
	}
	if took := time.Since(start); took > 10*time.Second {
		t.Errorf("a handed-off turn took %s; it was followed as a delegation", took)
	}
	mustContain(t, res.Content[0].Text, ackText, "completed")
	mustNotContain(t, res.Content[0].Text, "delegated task")
}

func TestConversationsArePerName(t *testing.T) {
	s, _, _, _ := turnServer(t, gatewayScenario{finish: true})
	token := func(conv string) string {
		text := callTool(t, s, ToolSendMessage, map[string]any{"text": "hi " + conv, "conversation": conv, "wait_seconds": 10}).Content[0].Text
		i := strings.Index(text, "token `")
		if i < 0 {
			t.Fatalf("no token in %q", text)
		}
		rest := text[i+len("token `"):]
		return rest[:strings.Index(rest, "`")]
	}
	a1, a2, b := token("a"), token("a"), token("b")
	if a1 != a2 {
		t.Errorf("conversation a got two tokens: %s, %s", a1, a2)
	}
	if a1 == b {
		t.Error("conversations a and b share a token")
	}
}

func TestANoticeOnlyTurnReturnsTheNotice(t *testing.T) {
	s, _, n, _ := turnServer(t, gatewayScenario{finish: true})
	res := callTool(t, s, ToolSendMessage, map[string]any{"text": statusQuery})
	if res.IsError || !strings.Contains(res.Content[0].Text, "Nothing is running") {
		t.Fatalf("result = %+v", res)
	}
	if n.Load() != 0 {
		t.Error("the fake gateway opened a task for a status question")
	}
}

// A turn still running when the wait ends returns its task, and task_control
// reads it by its reference, or by its bare id.
func TestARunningTurnReturnsItsTaskForTaskControl(t *testing.T) {
	s, js, _, _ := turnServer(t, gatewayScenario{})
	res := callTool(t, s, ToolSendMessage, map[string]any{"text": "long job", "wait_seconds": 3})
	if res.IsError || !strings.Contains(res.Content[0].Text, "still running") {
		t.Fatalf("result = %+v", res)
	}
	ref := fakeAddressee + "/task-root1"
	got := callTool(t, s, ToolTaskControl, map[string]any{"action": "get", "task": ref})
	if got.IsError || !strings.Contains(got.Content[0].Text, "working") {
		t.Errorf("get by reference = %+v", got)
	}
	got = callTool(t, s, ToolTaskControl, map[string]any{"action": "get", "task": "task-root1"})
	if got.IsError || !strings.Contains(got.Content[0].Text, ref) {
		t.Errorf("get by bare id = %+v", got)
	}
	publishResult(t, js, fakeAddressee, "task-root1", answerText)
	publishStatus(t, js, fakeAddressee, "task-root1", lib.StateCompleted, true)
	got = callTool(t, s, ToolTaskControl, map[string]any{"action": "get", "task": ref, "wait_seconds": 10})
	if got.IsError || !strings.Contains(got.Content[0].Text, answerText) {
		t.Errorf("get after finish = %+v", got)
	}
}

// Another principal publishing under its own addressee with the same task id
// does not reach the fold: the read is pinned to the task's addressee. And a
// bare id submitted to two addressees is refused rather than guessed.
func TestAForgedTaskUnderAnotherAddresseeIsNotFolded(t *testing.T) {
	url, js := testBus(t)
	b := dial(t, url)
	publishSubmission(t, js, fakeAddressee, "task-v", "console:x", "q", "")
	publishRun(t, js, fakeAddressee, "task-v", "", false)
	// The forgery lands while the real task is still working: under the
	// forger's own addressee, for the victim's task id, finishing first.
	publishResult(t, js, otherSession, "task-v", forgedAnswer)
	publishStatus(t, js, otherSession, "task-v", lib.StateCompleted, true)
	publishResult(t, js, fakeAddressee, "task-v", answerText)
	publishStatus(t, js, fakeAddressee, "task-v", lib.StateCompleted, true)

	task, err := b.ReadTask(context.Background(), TaskRef{Addressee: fakeAddressee, TaskID: "task-v"})
	if err != nil {
		t.Fatal(err)
	}
	if got := partsText(task.Artifact(lib.ArtifactResult).Parts); got != answerText {
		t.Errorf("the fold's answer is %q, want the task's own %q", got, answerText)
	}

	publishSubmission(t, js, otherSession, "task-v", "console:y", "q", "")
	if _, err := b.ResolveAddressee(context.Background(), "task-v"); err == nil || !strings.Contains(err.Error(), "more than one addressee") {
		t.Errorf("a bare id under two addressees resolved: %v", err)
	}
}

// A whole wait holds one consumer, however long it waits, and the server
// holds no more than maxTaskReaders at once.
func TestAWaitHoldsOneConsumer(t *testing.T) {
	url, js := testBus(t)
	b := dial(t, url)
	publishSubmission(t, js, fakeAddressee, "task-w", "console:x", "q", "")
	publishRun(t, js, fakeAddressee, "task-w", "", false)
	ref := TaskRef{Addressee: fakeAddressee, TaskID: "task-w"}

	done := make(chan struct{})
	go func() {
		defer close(done)
		_, _ = b.WaitForTask(context.Background(), ref, 4*time.Second)
	}()
	time.Sleep(3 * time.Second)
	s, err := js.Stream(context.Background(), lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	if got := s.CachedInfo().State.Consumers; got != 1 {
		t.Errorf("a 3s wait holds %d consumers, want 1", got)
	}
	<-done

	var wg sync.WaitGroup
	var busy atomic.Int32
	for i := 0; i < 2*maxTaskReaders; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := b.WaitForTask(context.Background(), ref, time.Second); err == errReadersBusy {
				busy.Add(1)
			}
		}()
	}
	wg.Wait()
	if busy.Load() < maxTaskReaders {
		t.Errorf("%d of %d concurrent waits were refused, want at least %d", busy.Load(), 2*maxTaskReaders, maxTaskReaders)
	}
}

// A caller that gives up while another turn holds the conversation's lock
// never sends its own.
func TestACallerThatGivesUpOnTheLockDoesNotSend(t *testing.T) {
	url, js := testBus(t)
	n := runFakeGateway(t, url, js, gatewayScenario{finish: true})
	b := dial(t, url)
	convs := NewConversations()
	token, _ := convs.Resolve("c", "")
	unlock, err := convs.lock(context.Background(), token)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	errc := make(chan error, 1)
	go func() {
		_, _, err := b.SendTurn(ctx, convs, token, "queued", time.Minute)
		errc <- err
	}()
	select {
	case err := <-errc:
		if err == nil {
			t.Fatal("a send behind a held lock succeeded after its caller gave up")
		}
	case <-time.After(2 * time.Second):
		unlock()
		t.Fatal("a send behind a held lock did not give up with its caller")
	}
	unlock()
	time.Sleep(500 * time.Millisecond)
	if n.Load() != 0 {
		t.Error("a caller that gave up still sent its turn")
	}
}

func TestTaskControlStopSendsTheStopWord(t *testing.T) {
	s, _, n, _ := turnServer(t, gatewayScenario{finish: true})
	res := callTool(t, s, ToolTaskControl, map[string]any{"action": "stop", "conversation": "ops"})
	if res.IsError || !strings.Contains(res.Content[0].Text, "Stopped the running task") {
		t.Fatalf("stop = %+v", res)
	}
	if n.Load() != 0 {
		t.Error("stop opened a task")
	}
}

func TestTurnFate(t *testing.T) {
	for text, want := range map[string]struct {
		settled bool
		texts   []string
	}{
		"what is broken?":           {false, []string{"what is broken?"}},
		"delegate check checkout":   {false, []string{"delegate check checkout", "check checkout"}},
		"stop":                      {true, nil},
		"/session":                  {true, nil},
		"/session off":              {true, nil},
		"/session look at pods":     {false, []string{"look at pods"}},
		"/feedback":                 {true, nil},
		"/feedback the bot is slow": {false, []string{"/feedback the bot is slow"}},
		"/feedback summary":         {true, nil},
	} {
		settled, texts := turnFate(text)
		if settled != want.settled || strings.Join(texts, "|") != strings.Join(want.texts, "|") {
			t.Errorf("turnFate(%q) = %v %q, want %v %q", text, settled, texts, want.settled, want.texts)
		}
	}
}

// Bad input is refused before anything reaches the bus: each case fails fast,
// which a refusal that came from the bus or a timeout would not.
func TestTurnToolsRefuseBadInput(t *testing.T) {
	s, _, n, _ := turnServer(t, gatewayScenario{finish: true})
	for name, args := range map[string]struct {
		tool string
		args map[string]any
	}{
		"an empty message":     {ToolSendMessage, map[string]any{"text": "  "}},
		"an oversized message": {ToolSendMessage, map[string]any{"text": strings.Repeat("x", consoleTextCap+1)}},
		"a wildcard token":     {ToolSendMessage, map[string]any{"text": "hi", "conversation_token": "*"}},
		"a dotted token":       {ToolSendMessage, map[string]any{"text": "hi", "conversation_token": "a.b"}},
		"get without a task":   {ToolTaskControl, map[string]any{"action": "get"}},
		"a wildcard task id":   {ToolTaskControl, map[string]any{"action": "get", "task": ">"}},
		"a wildcard addressee": {ToolTaskControl, map[string]any{"action": "get", "task": "*/task-x"}},
		"an unknown action":    {ToolTaskControl, map[string]any{"action": "steer"}},
	} {
		start := time.Now()
		res := callTool(t, s, args.tool, args.args)
		if !res.IsError {
			t.Errorf("%s was accepted: %+v", name, res)
		}
		if elapsed := time.Since(start); elapsed > time.Second {
			t.Errorf("%s took %s to refuse; it must be refused before the bus", name, elapsed)
		}
	}
	if n.Load() != 0 {
		t.Error("a refused call reached the gateway")
	}
}

func TestReadTaskOfAnUnknownTask(t *testing.T) {
	url, _ := testBus(t)
	if _, err := dial(t, url).ReadTask(context.Background(), TaskRef{Addressee: fakeAddressee, TaskID: "task-nope"}); err != ErrTaskNotFound {
		t.Errorf("err = %v, want ErrTaskNotFound", err)
	}
}

// Neither table fills for good: aliases past the cap forget the least
// recently used, and a send lock goes when its last holder releases it.
func TestConversationTablesCannotBeFilled(t *testing.T) {
	c := NewConversations()
	keep, err := c.Resolve("keep", "")
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2*maxConversations; i++ {
		if _, err := c.Resolve(fmt.Sprintf("a%d", i), ""); err != nil {
			t.Fatalf("alias %d refused: %v", i, err)
		}
		if i%100 == 0 {
			if again, _ := c.Resolve("keep", ""); again != keep {
				t.Fatal("a recently used alias was forgotten")
			}
		}
	}
	if _, err := c.Resolve(strings.Repeat("é", maxAliasRunes+1), ""); err == nil {
		t.Error("a name past the cap was taken")
	}
	if _, err := c.Resolve(strings.Repeat("é", maxAliasRunes), ""); err != nil {
		t.Errorf("a name at the cap was refused: %v", err)
	}
	if len(c.byAlias) > maxConversations {
		t.Errorf("alias table holds %d, cap %d", len(c.byAlias), maxConversations)
	}
	for i := 0; i < 2*maxConversations; i++ {
		unlock, err := c.lock(context.Background(), fmt.Sprintf("tok%d", i))
		if err != nil {
			t.Fatalf("lock %d refused: %v", i, err)
		}
		unlock()
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	held, _ := c.lock(context.Background(), "busy")
	if _, err := c.lock(ctx, "busy"); err == nil {
		t.Fatal("a cancelled waiter took a held lock")
	}
	held()
	if len(c.sending) != 0 {
		t.Errorf("%d send locks remain with nothing holding them", len(c.sending))
	}
}

// A released reader slot stays taken until the read's consumer would be
// reaped, so rapid reads cannot hold more consumers than the cap: past it a
// read is refused at once rather than piled up against the stream.
func TestReaderSlotsBoundConsumersHeld(t *testing.T) {
	url, js := testBus(t)
	publishSubmission(t, js, fakeAddressee, "task-s", "console:x", "q", "")
	publishRun(t, js, fakeAddressee, "task-s", answerText, true)
	b := dial(t, url)
	ref := TaskRef{Addressee: fakeAddressee, TaskID: "task-s"}
	ok, busy := 0, 0
	for i := 0; i < 2*maxTaskReaders; i++ {
		switch _, err := b.ReadTask(context.Background(), ref); err {
		case nil:
			ok++
		case errReadersBusy:
			busy++
		default:
			t.Fatalf("read %d: %v", i, err)
		}
	}
	if ok != maxTaskReaders || busy != maxTaskReaders {
		t.Errorf("%d reads succeeded and %d were refused, want %d and %d", ok, busy, maxTaskReaders, maxTaskReaders)
	}
	s, err := js.Stream(context.Background(), lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	if got := s.CachedInfo().State.Consumers; got > maxTaskReaders {
		t.Errorf("TASKS holds %d consumers, more than the %d slots", got, maxTaskReaders)
	}
}

// A slot whose read made no consumer goes back at once; one whose read may
// have made one stays taken until that consumer would be reaped. A refusal
// by the server made nothing; any other failure may have.
func TestASlotWithNothingToReapGoesBackAtOnce(t *testing.T) {
	b := &NATSBus{readers: make(chan struct{}, maxTaskReaders), reapDelay: time.Hour}
	for i := 0; i < 2*maxTaskReaders; i++ {
		release, err := b.readerSlot()
		if err != nil {
			t.Fatalf("slot %d refused after every earlier one made nothing: %v", i, err)
		}
		release(false)
	}
	for i := 0; i < maxTaskReaders; i++ {
		release, err := b.readerSlot()
		if err != nil {
			t.Fatal(err)
		}
		release(true)
	}
	if _, err := b.readerSlot(); err != errReadersBusy {
		t.Errorf("a slot came back before its consumer's reap: err = %v", err)
	}
	if consumerMayExist(&jetstream.APIError{Code: 400, ErrorCode: jetstream.JSErrCodeConsumerCreate}) {
		t.Error("a server refusal counted as a consumer that may exist")
	}
	if !consumerMayExist(context.DeadlineExceeded) {
		t.Error("a timed-out create counted as one that made nothing")
	}
}

// A wait with less left than one fetch's floor still returns what it read,
// not an error: the read's end is a deadline like any other.
func TestAShortWaitKeepsWhatItRead(t *testing.T) {
	url, js := testBus(t)
	publishSubmission(t, js, fakeAddressee, "task-short", "console:x", "q", "")
	publishStatus(t, js, fakeAddressee, "task-short", lib.StateWorking, false)
	b := dial(t, url)
	got, err := b.WaitForTask(context.Background(), TaskRef{Addressee: fakeAddressee, TaskID: "task-short"}, 50*time.Millisecond)
	if err != nil || got == nil {
		t.Fatalf("a 50ms wait on a running task: task %v, err %v", got, err)
	}
	if got.Final {
		t.Error("the running task folded as final")
	}
}

// A turn the gateway settles with a notice listens to its notices alone, not
// to every addressee's submissions, which nothing would drain.
func TestASettledTurnTakesNoSubmissions(t *testing.T) {
	url, _ := testBus(t)
	b := dial(t, url)
	before := b.client.Conn().NumSubscriptions()
	w, err := b.openTurn("tok", nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if got := b.client.Conn().NumSubscriptions() - before; got != 1 {
		t.Errorf("a settled turn holds %d subscriptions, want 1 (its notices)", got)
	}
	w.close()

	// And a stop, the settled turn the tools send, opens it that way.
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _, _ = b.SendStop(ctx, NewConversations(), "tok-stop") }()
	deadline := time.Now().Add(5 * time.Second)
	for b.client.Conn().NumSubscriptions() == before && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	got := b.client.Conn().NumSubscriptions() - before
	cancel()
	<-done
	if got != 1 {
		t.Errorf("a stop waiting on its notice holds %d subscriptions, want 1", got)
	}
}

// A caller already out of time when it asks for a free lock gets its own
// context error, not one that reads as another turn holding the conversation.
func TestALockAskedForTooLateSaysSo(t *testing.T) {
	c := NewConversations()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for i := 0; i < 50; i++ {
		_, err := c.lock(ctx, "tok")
		if !errors.Is(err, context.Canceled) || strings.Contains(err.Error(), "another turn") {
			t.Fatalf("lock with a done context and no holder: %v", err)
		}
	}
	if len(c.sending) != 0 {
		t.Errorf("%d send locks left behind", len(c.sending))
	}
}

// A conversation resumed by its token is named by the token, not as the
// default conversation, which is another conversation.
func TestAResumedConversationIsNamedByItsToken(t *testing.T) {
	s, _, _, _ := turnServer(t, gatewayScenario{finish: true})
	res := callTool(t, s, ToolSendMessage, map[string]any{"text": "hi", "conversation_token": "mcpresumed01", "wait_seconds": 10})
	if res.IsError {
		t.Fatalf("send_message failed: %s", res.Content[0].Text)
	}
	mustContain(t, res.Content[0].Text, "Conversation by token `mcpresumed01`")
	mustNotContain(t, res.Content[0].Text, "Conversation `default`")
}

// emptyFetchConsumer is a consumer whose every fetch comes back empty, as a
// stalled read's would.
type emptyFetchConsumer struct{ jetstream.Consumer }

func (emptyFetchConsumer) Fetch(int, ...jetstream.FetchOpt) (jetstream.MessageBatch, error) {
	return emptyBatch{}, nil
}

type emptyBatch struct{}

func (emptyBatch) Messages() <-chan jetstream.Msg {
	c := make(chan jetstream.Msg)
	close(c)
	return c
}
func (emptyBatch) Error() error { return nil }

// A task read whose fetch comes back empty while events are still pending
// is an error, not the task's end folded from what was read so far.
func TestAStalledTaskReadIsAnError(t *testing.T) {
	w := &taskWatch{ref: TaskRef{Addressee: fakeAddressee, TaskID: "task-stall"}, cons: emptyFetchConsumer{}, pending: 5, release: func(bool) {}}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := w.next(ctx); err == nil || !strings.Contains(err.Error(), "still pending") {
		t.Errorf("a stalled fetch with 5 pending returned %v, want the stall reported", err)
	}
	w.pending = 0
	if err := w.next(ctx); err != nil {
		t.Errorf("an empty fetch with nothing pending (waiting for new events) returned %v", err)
	}
}

// A second message on a conversation whose turn is still running waits for
// it rather than being steered into it, and says the conversation is busy,
// naming the running task, when its wait runs out first. Once the task ends
// the next message goes.
func TestASecondMessageWaitsForTheRunningTurn(t *testing.T) {
	s, js, n, _ := turnServer(t, gatewayScenario{})
	res := callTool(t, s, ToolSendMessage, map[string]any{"text": "long job", "wait_seconds": 2})
	if res.IsError || !strings.Contains(res.Content[0].Text, "still running") {
		t.Fatalf("first turn = %+v", res)
	}
	ref := fakeAddressee + "/task-root1"
	start := time.Now()
	res = callTool(t, s, ToolSendMessage, map[string]any{"text": "and another thing", "wait_seconds": 2})
	if !res.IsError {
		t.Fatalf("a message behind a running turn was sent: %+v", res)
	}
	mustContain(t, res.Content[0].Text, "conversation busy: a turn is still running", ref)
	if took := time.Since(start); took < 2*time.Second {
		t.Errorf("the busy message came after %s, before the wait ran out", took)
	}
	if n.Load() != 1 {
		t.Errorf("the gateway got %d turns, want 1", n.Load())
	}

	go func() {
		time.Sleep(time.Second)
		publishResult(t, js, fakeAddressee, "task-root1", answerText)
		publishStatus(t, js, fakeAddressee, "task-root1", lib.StateCompleted, true)
	}()
	res = callTool(t, s, ToolSendMessage, map[string]any{"text": "and another thing", "wait_seconds": 4})
	if res.IsError || !strings.Contains(res.Content[0].Text, fakeAddressee+"/task-root2") {
		t.Fatalf("the message after the running turn ended = %+v", res)
	}
	if n.Load() != 2 {
		t.Errorf("the gateway got %d turns, want 2", n.Load())
	}
}

// A message sent while another call's turn on the conversation is still
// being waited on queues behind it, and gives up as busy, naming its task.
func TestAConcurrentMessageQueuesBehindTheTurn(t *testing.T) {
	s, _, n, _ := turnServer(t, gatewayScenario{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		callTool(t, s, ToolSendMessage, map[string]any{"text": "long job", "wait_seconds": 6})
	}()
	deadline := time.Now().Add(5 * time.Second)
	for n.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	time.Sleep(500 * time.Millisecond)
	res := callTool(t, s, ToolSendMessage, map[string]any{"text": "and another thing", "wait_seconds": 1})
	if !res.IsError {
		t.Fatalf("a message behind a turn being waited on was sent: %+v", res)
	}
	mustContain(t, res.Content[0].Text, "conversation busy", fakeAddressee+"/task-root1")
	<-done
	if n.Load() != 1 {
		t.Errorf("the gateway got %d turns, want 1", n.Load())
	}
}

// task_control get on a reference with no events says so at once, rather
// than holding a reader for the whole wait.
func TestWaitingOnAnUnknownTaskEndsAtOnce(t *testing.T) {
	url, _ := testBus(t)
	start := time.Now()
	_, err := dial(t, url).WaitForTask(context.Background(), TaskRef{Addressee: fakeAddressee, TaskID: "task-typo"}, time.Minute)
	if err != ErrTaskNotFound {
		t.Errorf("err = %v, want ErrTaskNotFound", err)
	}
	if took := time.Since(start); took > 5*time.Second {
		t.Errorf("took %s", took)
	}
}

// An edit of the gateway's rolling status line is not a notice about the
// turn; a fresh post is.
func TestAnEditIsNotANotice(t *testing.T) {
	edit := &nats.Msg{Data: mustJSON(t, ConsoleOutFrame{MessageID: "s1", Text: "⚙️ **working**", Edit: true})}
	if got := noticeText(edit); got != "" {
		t.Errorf("an edit read as the notice %q", got)
	}
	post := &nats.Msg{Data: mustJSON(t, ConsoleOutFrame{MessageID: "n2", Text: "🛑 cancel sent"})}
	if got := noticeText(post); got != "🛑 cancel sent" {
		t.Errorf("a post read as %q", got)
	}
}

// A notice is shown whole, line breaks, backticks and all: for a settled turn
// it is the answer.
func TestANoticeIsShownWhole(t *testing.T) {
	notice := "```\nusage summary\n" + strings.Repeat("usage: counter `x` 12345\n", 20) + "```\nhttps://gke-labs.github.io/kube-agents/feedback"
	var sb strings.Builder
	writeNotices(&sb, []string{notice})
	for _, line := range strings.Split(notice, "\n") {
		mustContain(t, sb.String(), "> "+line+"\n")
	}
}

// A finished turn leaves nothing for the next to wait on, and a turn read as
// finished since is forgotten by the next send.
func TestAFinishedTurnIsForgotten(t *testing.T) {
	url, js := testBus(t)
	runFakeGateway(t, url, js, gatewayScenario{})
	b := dial(t, url)
	convs := NewConversations()
	s := mustServer(t, SendMessageTool(b, convs))
	token, _ := convs.Resolve("", "")
	callTool(t, s, ToolSendMessage, map[string]any{"text": "long job", "wait_seconds": 2})
	if ref, ok := convs.runningTask(token); !ok || ref.TaskID != "task-root1" {
		t.Fatalf("running = %v %v, want task-root1", ref, ok)
	}
	publishResult(t, js, fakeAddressee, "task-root1", answerText)
	publishStatus(t, js, fakeAddressee, "task-root1", lib.StateCompleted, true)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := b.awaitRunningTurn(ctx, convs, token); err != nil {
		t.Fatal(err)
	}
	if ref, ok := convs.runningTask(token); ok {
		t.Errorf("a finished task is still waited on: %v", ref)
	}

	url2, js2 := testBus(t)
	runFakeGateway(t, url2, js2, gatewayScenario{finish: true})
	b2 := dial(t, url2)
	convs2 := NewConversations()
	s2 := mustServer(t, SendMessageTool(b2, convs2))
	token2, _ := convs2.Resolve("", "")
	callTool(t, s2, ToolSendMessage, map[string]any{"text": "quick", "wait_seconds": 10})
	if ref, ok := convs2.runningTask(token2); ok {
		t.Errorf("a finished turn left %v to wait on", ref)
	}
}

// A sent turn holds the conversation's send lock until its watch closes, so
// no send slips in between its submission and its answer being read.
func TestATurnHoldsTheLockUntilItsWatchCloses(t *testing.T) {
	url, js := testBus(t)
	runFakeGateway(t, url, js, gatewayScenario{finish: true})
	b := dial(t, url)
	convs := NewConversations()
	token, _ := convs.Resolve("c", "")
	_, w, err := b.SendTurn(context.Background(), convs, token, "hi", time.Minute)
	if err != nil || w == nil {
		t.Fatalf("SendTurn = %v, %v", w, err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	if unlock, err := convs.lock(ctx, token); !errors.Is(err, ErrConversationBusy) {
		if unlock != nil {
			unlock()
		}
		t.Fatalf("lock while the turn's watch is open: %v, want busy", err)
	}
	w.close()
	ctx2, cancel2 := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel2()
	unlock, err := convs.lock(ctx2, token)
	if err != nil {
		t.Fatalf("lock after the watch closed: %v", err)
	}
	unlock()
}

// A turn whose first event trails its submission, as a cold executor's does,
// is waited on, not reported as having no events and forgotten.
func TestATurnWhoseFirstEventIsLateIsWaitedOn(t *testing.T) {
	s, _, _, _ := turnServer(t, gatewayScenario{finish: true, lateStart: time.Second})
	res := callTool(t, s, ToolSendMessage, map[string]any{"text": "hi", "wait_seconds": 10})
	if res.IsError {
		t.Fatalf("send_message failed: %s", res.Content[0].Text)
	}
	mustContain(t, res.Content[0].Text, answerText)
	mustNotContain(t, res.Content[0].Text, "no events yet")
}

// A turn whose task was read as nothing yet is still the conversation's
// running turn.
func TestAnUnreadRootIsUnfinished(t *testing.T) {
	root := TaskRef{Addressee: fakeAddressee, TaskID: "task-root1"}
	if got := (TurnOutcome{Root: root}).Unfinished(); got == nil || *got != root {
		t.Errorf("Unfinished() = %v, want the root", got)
	}
}
