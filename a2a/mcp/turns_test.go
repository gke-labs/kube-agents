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
	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
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

// pub publishes for the fake gateway and the tests' background publishers,
// which run on goroutines the test does not wait for. Once the test has
// ended, its context is done and the bus is being torn down, so a publish
// that fails then is the teardown, not a finding; and a Fatal from any
// goroutine after the test ended panics the whole test binary, so no later
// test in the package runs. While the test runs, a failed publish is
// reported with Error, on every path: FailNow is only for the test's own
// goroutine, and these helpers also run on the subscription's.
func pub(t *testing.T, js jetstream.JetStream, subject string, env *lib.Envelope) {
	t.Helper()
	ctx := t.Context()
	if _, err := js.Publish(ctx, subject, mustJSON(t, env)); err != nil {
		if ctx.Err() != nil {
			return
		}
		t.Error(err)
	}
}

// pause waits d on a goroutine the test does not wait for, and reports false
// as soon as the test ends instead, so the caller can stop rather than touch
// a bus that is being torn down.
func pause(t *testing.T, d time.Duration) bool {
	select {
	case <-time.After(d):
		return true
	case <-t.Context().Done():
		return false
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
	finish   bool      // the root task completes with answerText
	delegate bool      // the root delegates; a child runs and a wake task carries the answer
	handoff  bool      // the root acknowledges and hands its work to kanban cards (no wake task ever)
	bigChild bool      // with delegate: the child's events total more than readMaxBytes, and no wake task comes
	decoys   bool      // before the root: another conversation's turn, a delegated child here, an earlier turn's late submission, and a cancel here
	frames   *frameLog // when set, every frame the gateway reads is recorded
	// after, when set, runs once the turn's tasks are published, on its
	// own goroutine, with the gateway's connection: a card's posts come
	// from there (publishNotify).
	after func(nc *nats.Conn, token string, turn int32)
	// root, when set, names the turn's task and says whether it finishes:
	// a turn given an earlier turn's task is the gateway steering it.
	root func(turn int32) (id string, finish bool)
	// lateStart delays the root's first event after its submission, as an
	// executor's cold start does.
	lateStart time.Duration
	// lateNotice, when set, posts the acknowledgement notice after the
	// root's submission, by this much: the order the two connections the
	// gateway publishes on can produce. A delay inside noticeSettle is the
	// notice the window exists to catch; past it, the notice is dropped.
	lateNotice time.Duration
}

// sentFrame is one console frame as the fake gateway read it.
type sentFrame struct{ token, text string }

type frameLog struct {
	mu     sync.Mutex
	frames []sentFrame
}

func (l *frameLog) add(f sentFrame) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.frames = append(l.frames, f)
}

func (l *frameLog) all() []sentFrame {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]sentFrame(nil), l.frames...)
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
		if sc.frames != nil {
			sc.frames.add(sentFrame{token, f.Text})
		}
		notice := func(text string) {
			_ = nc.Publish(fmt.Sprintf(consoleOutSubject, token), mustJSON(t, ConsoleOutFrame{MessageID: "n1", Text: text}))
		}
		switch f.Text {
		case statusQuery, statusText:
			notice("Nothing is running in this conversation.")
			return
		case stopText:
			notice("Stopped the running task.")
			return
		case sessionOnText:
			notice("Session started.")
			return
		case sessionOffText:
			notice("Session ended.")
			return
		}
		i := n.Add(1)
		// An earlier task's terminal, posted to the conversation as the
		// console relay does (kind "result"), is not a notice about this
		// turn and must not be taken for one.
		_ = nc.Publish(fmt.Sprintf(consoleOutSubject, token), mustJSON(t, ConsoleOutFrame{MessageID: "r-task-earlier", Text: "an earlier answer", Kind: "result"}))
		if sc.lateNotice == 0 {
			notice("Working on it…")
		}
		root, finish := fmt.Sprintf("task-root%d", i), sc.finish
		if sc.root != nil {
			root, finish = sc.root(i)
		}
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
		if sc.lateNotice > 0 {
			time.Sleep(sc.lateNotice)
			notice("Working on it…")
		}
		switch {
		case sc.delegate:
			publishRun(t, js, fakeAddressee, root, "", false)
			publishArtifact(t, js, fakeAddressee, root, lib.ArtifactDelegate, "cluster-a: look at checkout")
			publishStatus(t, js, fakeAddressee, root, lib.StateCompleted, true)
			child := fmt.Sprintf("task-deleg%d", i)
			publishSubmission(t, js, "cluster-a", child, conv, "look at checkout", root)
			if sc.bigChild {
				publishRun(t, js, "cluster-a", child, "", false)
				for range readMaxBytes/(800<<10) + 1 {
					publishArtifact(t, js, "cluster-a", child, lib.ArtifactResult, strings.Repeat("x", 800<<10))
				}
				return
			}
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
			if !pause(t, sc.lateStart) {
				return
			}
			publishRun(t, js, fakeAddressee, root, answerText, finish)
		}
		if sc.after != nil {
			go sc.after(nc, token, i)
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
	convs := NewConversations().WithEvents(b.Events())
	return mustServer(t, SendMessageTool(b, convs), TaskControlTool(b, convs), PollEventsTool(b.Events(), convs)), js, n, b
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

// A delegated task too large to read is read once, not again every 200 ms
// for the rest of the wait, each read holding a reader slot until every
// other caller is refused.
func TestAFollowedTaskTooLargeToReadIsReadOnce(t *testing.T) {
	url, js := bigBus(t)
	runFakeGateway(t, url, js, gatewayScenario{delegate: true, bigChild: true})
	var reads atomic.Int32
	sub, err := js.Conn().Subscribe("$JS.API.CONSUMER.CREATE.>", func(m *nats.Msg) {
		if strings.Contains(string(m.Data), "task-deleg1") {
			reads.Add(1)
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	b := dial(t, url)
	s := mustServer(t, SendMessageTool(b, NewConversations()))
	res := callTool(t, s, ToolSendMessage, map[string]any{"text": "delegate check the rollout", "wait_seconds": 3})
	if res.IsError {
		t.Fatalf("send_message failed: %s", res.Content[0].Text)
	}
	if n := reads.Load(); n != 1 {
		t.Errorf("read the over-budget task %d times in a 3s wait; want once", n)
	}
	// The turn leaves that task as the conversation's running one, and the
	// next message can't wait on a task it can't read: it goes, rather than
	// the conversation refusing every message from now on.
	res = callTool(t, s, ToolSendMessage, map[string]any{"text": "delegate check it again", "wait_seconds": 3})
	if res.IsError || strings.Contains(res.Content[0].Text, "running task") {
		t.Fatalf("a second message after a too-large task: %s", res.Content[0].Text)
	}
}

// oneMsgSource is a read whose every wait brings one message.
type oneMsgSource struct{}

func (oneMsgSource) Fetch(int, ...nats.PullOpt) ([]*nats.Msg, error) {
	return []*nats.Msg{{Subject: "a2a.tasks.x", Data: []byte("{}")}}, nil
}
func (oneMsgSource) Unsubscribe() error { return nil }

// A task read past maxTaskEvents fails as too large to read, which a turn
// takes as final for that task rather than reading it again.
func TestATaskPastItsEventCapIsTooLargeToRead(t *testing.T) {
	w := &taskWatch{ref: TaskRef{Addressee: fakeAddressee, TaskID: "task-chatty"}, r: &budgetReader{sub: oneMsgSource{}, batch: 1, budget: readMaxBytes}, seen: maxTaskEvents, pending: 1, release: func(bool) {}}
	err := w.next(context.Background())
	if !tooLargeToRead(err) {
		t.Fatalf("a read past %d events returned %v; want it too large to read", maxTaskEvents, err)
	}
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

// The profile dispatcher writes the terminal of a profile task it fails or
// refuses on the task's supervisor subject, under its own session. The task
// read folds it as final, as the gateway's fold does; anyone else's terminal
// there is still dropped.
func TestADispatcherTerminalFoldsAsFinal(t *testing.T) {
	url, js := testBus(t)
	b := dial(t, url)
	const profile, taskID = "tse", "task-d"
	publishSubmission(t, js, profile, taskID, "console:x", "q", "")
	publishRun(t, js, profile, taskID, "", false)
	terminal := func(from lib.Party) {
		p := mustJSON(t, lib.StatusUpdate{TaskID: taskID, ContextID: "ctx-" + taskID,
			Status: lib.TaskStatus{State: lib.StateFailed}, Final: true})
		env, err := lib.NewStatusUpdateEnvelope(from, taskID, "ctx-"+taskID, "corr-"+taskID, p)
		if err != nil {
			t.Fatal(err)
		}
		pub(t, js, lib.TaskSupervisorSubject(profile, taskID), env)
	}
	ref := TaskRef{Addressee: profile, TaskID: taskID}
	terminal(lib.Party{Session: otherSession, AgentType: "a2a-dispatcher"})
	task, err := b.ReadTask(context.Background(), ref)
	if err != nil {
		t.Fatal(err)
	}
	if task.State != lib.StateWorking || task.Final {
		t.Fatalf("a terminal from %q was folded: state %s, final %v", otherSession, task.State, task.Final)
	}
	terminal(lib.Party{Session: lib.DispatcherSession, AgentType: "a2a-dispatcher"})
	task, err = b.WaitForTask(context.Background(), ref, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if task.State != lib.StateFailed || !task.Final {
		t.Errorf("the dispatcher's terminal was not folded: state %s, final %v", task.State, task.Final)
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

// shortWaitRead is the whole wait the short-wait test gives WaitForTask on
// a running task. The wait opens the task's consumer first, two JetStream
// calls that took past the test's former 50 ms under the race detector on a
// loaded runner, so the read failed before any fetch. The floor itself is
// checked on a watch that is already open, with no time left, up to
// floorFetchAttempts times: one floor's fetch on a loaded runner can end
// before the server delivers, and without the floor no attempt reads at all.
const (
	shortWaitRead      = 2 * time.Second
	floorFetchAttempts = 20
)

// A wait with less left than one fetch's floor still returns what it read,
// not an error: the read's end is a deadline like any other.
func TestAShortWaitKeepsWhatItRead(t *testing.T) {
	url, js := testBus(t)
	publishSubmission(t, js, fakeAddressee, "task-short", "console:x", "q", "")
	publishStatus(t, js, fakeAddressee, "task-short", lib.StateWorking, false)
	b := dial(t, url)
	ref := TaskRef{Addressee: fakeAddressee, TaskID: "task-short"}

	// The floor: with no time left at all, what is pending still arrives.
	w, err := b.watchTask(context.Background(), ref)
	if err != nil {
		t.Fatal(err)
	}
	spent, cancel := context.WithDeadline(context.Background(), time.Now())
	defer cancel()
	<-spent.Done()
	for i := 0; i < floorFetchAttempts && len(w.events) == 0; i++ {
		if err := w.next(spent); err != nil && !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("a fetch with no time left: %v", err)
		}
	}
	folded, err := w.fold()
	w.close()
	if err != nil || len(w.events) == 0 {
		t.Fatalf("%d fetches with no time left read %d events, fold err %v; want the pending ones",
			floorFetchAttempts, len(w.events), err)
	}
	if folded.Final {
		t.Error("the running task folded as final")
	}

	// The end of the wait: the running task as read, not an error.
	got, err := b.WaitForTask(context.Background(), ref, shortWaitRead)
	if err != nil || got == nil {
		t.Fatalf("a %s wait on a running task: task %v, err %v", shortWaitRead, got, err)
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

// emptySource is a read whose every wait comes back empty, as a stalled
// read's would.
type emptySource struct{}

func (emptySource) Fetch(int, ...nats.PullOpt) ([]*nats.Msg, error) { return nil, nats.ErrTimeout }
func (emptySource) Unsubscribe() error                              { return nil }

// A task read whose fetch comes back empty while events are still pending
// is an error, not the task's end folded from what was read so far.
func TestAStalledTaskReadIsAnError(t *testing.T) {
	w := &taskWatch{ref: TaskRef{Addressee: fakeAddressee, TaskID: "task-stall"}, r: &budgetReader{sub: emptySource{}, budget: readMaxBytes}, pending: 5, release: func(bool) {}}
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
		if !pause(t, time.Second) {
			return
		}
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

// The gateway's acknowledgement can reach the server after the submission it
// precedes (two connections, one of them unflushed); a turn whose notice trails
// its submission still shows the notice.
func TestANoticeThatTrailsTheSubmissionIsStillShown(t *testing.T) {
	s, _, _, _ := turnServer(t, gatewayScenario{finish: true, lateNotice: noticeSettle / 4})
	res := callTool(t, s, ToolSendMessage, map[string]any{"text": "hi", "wait_seconds": 10})
	if res.IsError {
		t.Fatalf("send_message failed: %s", res.Content[0].Text)
	}
	mustContain(t, res.Content[0].Text, "Working on it", fakeAddressee+"/task-root1", answerText)
}

// A turn whose task was read as nothing yet is still the conversation's
// running turn.
func TestAnUnreadRootIsUnfinished(t *testing.T) {
	root := TaskRef{Addressee: fakeAddressee, TaskID: "task-root1"}
	if got := (TurnOutcome{Root: root}).Unfinished(); got == nil || *got != root {
		t.Errorf("Unfinished() = %v, want the root", got)
	}
}

// Streaming task_control get on an unknown <addressee>/<task-id> fails
// immediately with ErrTaskNotFound rather than spinning for 6s and returning
// STREAMING_STEP_CONTINUE, and leaves no taskStepState in convs.steps.
func TestWaitingOnAnUnknownTaskWithStreamStepsEndsAtOnce(t *testing.T) {
	url, _ := testBus(t)
	b := dial(t, url)
	convs := NewConversations()
	s := mustServer(t, TaskControlTool(b, convs))

	start := time.Now()
	res := callTool(t, s, ToolTaskControl, map[string]any{
		"action":       "get",
		"task_id":      "platform/task-does-not-exist",
		"stream_steps": true,
	})
	elapsed := time.Since(start)
	if !res.IsError {
		t.Fatalf("expected error on unknown task, got: %s", res.Content[0].Text)
	}
	mustContain(t, res.Content[0].Text, ErrTaskNotFound.Error())
	mustNotContain(t, res.Content[0].Text, StreamingStepContinuePrefix)
	if elapsed > 500*time.Millisecond {
		t.Errorf("streaming task_control get on unknown task took %s, want < 500ms", elapsed)
	}
	convs.mu.Lock()
	stepsCount := len(convs.steps)
	convs.mu.Unlock()
	if stepsCount != 0 {
		t.Errorf("len(convs.steps) = %d, want 0", stepsCount)
	}
}

// A streamed get of a finished delegated root with no turn watch is a re-read:
// it settles at once without discovering children, and says so, so the root's
// delegate artifact is not taken for the turn's whole answer.
func TestAStreamedReReadOfADelegatedTurnSaysItsChildrenWereNotFollowed(t *testing.T) {
	url, js := testBus(t)
	conv := "console:tok-reread"
	publishSubmission(t, js, fakeAddressee, "task-reread-deleg", conv, "delegate", "")
	publishRun(t, js, fakeAddressee, "task-reread-deleg", "", false)
	publishArtifact(t, js, fakeAddressee, "task-reread-deleg", lib.ArtifactDelegate, "cluster-a: check")
	publishStatus(t, js, fakeAddressee, "task-reread-deleg", lib.StateCompleted, true)
	publishSubmission(t, js, "cluster-a", "task-reread-child", conv, "check", "task-reread-deleg")
	publishRun(t, js, "cluster-a", "task-reread-child", "child finished answer", true)
	publishSubmission(t, js, fakeAddressee, "task-reread-plain", conv, "plain", "")
	publishRun(t, js, fakeAddressee, "task-reread-plain", answerText, true)

	b := dial(t, url)
	s := mustServer(t, TaskControlTool(b, NewConversations()))
	const note = "This is a re-read of a finished turn: its delegated tasks were not followed live"
	for task, delegating := range map[string]bool{"platform/task-reread-deleg": true, "platform/task-reread-plain": false} {
		res := callTool(t, s, ToolTaskControl, map[string]any{"action": "get", "task_id": task, "wait_seconds": 5, "stream_steps": true})
		if res.IsError {
			t.Fatalf("task_control get %s: %s", task, res.Content[0].Text)
		}
		var env CustomToolResultEnvelope
		if err := json.Unmarshal([]byte(res.Content[0].Text), &env); err != nil {
			t.Fatalf("unmarshal envelope for %s: %v", task, err)
		}
		if strings.HasPrefix(env.Result, StreamingStepContinuePrefix) {
			t.Fatalf("re-read of finished %s did not settle:\n%s", task, env.Result)
		}
		if delegating {
			mustContain(t, env.Result, note, "task_control get on each child task")
		} else {
			mustNotContain(t, env.Result, note)
		}
	}
}

// When a streamed turn finishes on the bus before the caller polls its
// terminal step with task_control get, the next send_message on the same
// conversation clears the active step state cleanly without busy-polling or
// exhausting maxTaskReaders.
func TestAwaitRunningTurnClearsFinishedStreamedTurn(t *testing.T) {
	url, js := testBus(t)
	runFakeGateway(t, url, js, gatewayScenario{
		root: func(turn int32) (string, bool) {
			return fmt.Sprintf("task-root%d", turn), turn > 1
		},
	})
	b := dial(t, url)
	convs := NewConversations().WithEvents(b.Events())
	s := mustServer(t, SendMessageTool(b, convs), TaskControlTool(b, convs))

	res1 := callTool(t, s, ToolSendMessage, map[string]any{
		"text":         "first streamed turn",
		"wait_seconds": 1,
		"stream_steps": true,
	})
	if res1.IsError {
		t.Fatalf("first send_message failed: %s", res1.Content[0].Text)
	}
	mustContain(t, res1.Content[0].Text, StreamingStepContinuePrefix)

	// Finish the first task on the bus without calling task_control get.
	publishResult(t, js, fakeAddressee, "task-root1", answerText)
	publishStatus(t, js, fakeAddressee, "task-root1", lib.StateCompleted, true)

	// Immediately send a second message on the same conversation.
	res2 := callTool(t, s, ToolSendMessage, map[string]any{
		"text":         "second turn after finish",
		"wait_seconds": 5,
	})
	if res2.IsError {
		t.Fatalf("second send_message failed: %s", res2.Content[0].Text)
	}
	mustContain(t, res2.Content[0].Text, fakeAddressee+"/task-root2", answerText)
}

// During step streaming, a followed child task that exceeds readMaxBytes
// caches its tooLargeToRead error without re-opening consumers on subsequent
// polls and settles the chain cleanly.
func TestStreamingStepsFollowedTaskTooLargeToReadIsReadOnceAndSettles(t *testing.T) {
	url, js := bigBus(t)
	runFakeGateway(t, url, js, gatewayScenario{delegate: true, bigChild: true})
	var reads atomic.Int32
	sub, err := js.Conn().Subscribe("$JS.API.CONSUMER.CREATE.>", func(m *nats.Msg) {
		if strings.Contains(string(m.Data), "task-deleg1") {
			reads.Add(1)
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sub.Unsubscribe() }()

	b := dial(t, url)
	convs := NewConversations().WithEvents(b.Events())
	s := mustServer(t, SendMessageTool(b, convs), TaskControlTool(b, convs))

	res := callTool(t, s, ToolSendMessage, map[string]any{
		"text":         "delegate check the rollout",
		"wait_seconds": 10,
		"stream_steps": true,
	})
	if res.IsError {
		t.Fatalf("send_message failed: %s", res.Content[0].Text)
	}
	var env CustomToolResultEnvelope
	if err := json.Unmarshal([]byte(res.Content[0].Text), &env); err != nil {
		t.Fatalf("unmarshal envelope: %v", err)
	}
	if strings.HasPrefix(env.Result, StreamingStepContinuePrefix) {
		// If the first step returned before the child settled, one follow-up poll settles it.
		res2 := callTool(t, s, ToolTaskControl, map[string]any{
			"action":       "get",
			"task_id":      "platform/task-root1",
			"wait_seconds": 10,
			"stream_steps": true,
		})
		if res2.IsError {
			t.Fatalf("task_control get failed: %s", res2.Content[0].Text)
		}
		if err := json.Unmarshal([]byte(res2.Content[0].Text), &env); err != nil {
			t.Fatalf("unmarshal envelope 2: %v", err)
		}
	}
	if strings.HasPrefix(env.Result, StreamingStepContinuePrefix) {
		t.Fatalf("expected chain to settle, got still-running result:\n%s", env.Result)
	}
	mustContain(t, env.Result, "cluster-a/task-deleg1", "not read")
	if n := reads.Load(); n != 1 {
		t.Errorf("read the over-budget child task %d times during step streaming; want once", n)
	}
}

// When a streamed delegated turn has aliased step state under root R and
// followed children C1, C2 while running[token] points to C1, a follow-up
// send_message's awaitRunningTurn waits for the children once without
// multiplying readers, marks the aliased step state superseded (left for its
// streaming client, no longer the conversation's running turn), and does not
// loop again on R. The streaming client's next poll then settles it with the
// children's answer and removes it across all keys.
func TestAwaitRunningTurnSupersedesAliasedDelegatedStepState(t *testing.T) {
	url, js := testBus(t)
	nc, err := nats.Connect(url, nats.UserInfo("admin", "admin"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)

	var turnNum atomic.Int32
	_, err = nc.Subscribe("chat.console.*.in", func(m *nats.Msg) {
		token := strings.Split(m.Subject, ".")[2]
		conv := "console:" + token
		var f ConsoleInFrame
		if json.Unmarshal(m.Data, &f) != nil {
			return
		}
		i := turnNum.Add(1)
		root := fmt.Sprintf("task-root%d", i)
		publishSubmission(t, js, fakeAddressee, root, conv, f.Text, "")
		if i == 1 {
			publishRun(t, js, fakeAddressee, root, "", false)
			publishArtifact(t, js, fakeAddressee, root, lib.ArtifactDelegate, "cluster-a, cluster-b: inspect")
			publishStatus(t, js, fakeAddressee, root, lib.StateCompleted, true)
			publishSubmission(t, js, "cluster-a", "task-deleg1", conv, "inspect a", root)
			publishRun(t, js, "cluster-a", "task-deleg1", "", false)
			publishSubmission(t, js, "cluster-b", "task-deleg2", conv, "inspect b", root)
			publishRun(t, js, "cluster-b", "task-deleg2", "", false)
			return
		}
		publishRun(t, js, fakeAddressee, root, answerText, true)
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}

	b := dial(t, url)
	convs := NewConversations().WithEvents(b.Events())
	s := mustServer(t, SendMessageTool(b, convs), TaskControlTool(b, convs))

	res1 := callTool(t, s, ToolSendMessage, map[string]any{
		"text":         "first delegated turn",
		"wait_seconds": 1,
		"stream_steps": true,
	})
	if res1.IsError {
		t.Fatalf("first send_message failed: %s", res1.Content[0].Text)
	}
	mustContain(t, res1.Content[0].Text, StreamingStepContinuePrefix)

	// Verify the step state is aliased under root and both children, while
	// running[token] tracks an unfinished child rather than the root.
	convs.mu.Lock()
	if len(convs.steps) < 3 {
		t.Fatalf("expected aliased step state under root + 2 children, got %d keys", len(convs.steps))
	}
	convs.mu.Unlock()

	// Complete both delegated children on the bus without calling task_control get.
	publishResult(t, js, "cluster-a", "task-deleg1", "child a done")
	publishStatus(t, js, "cluster-a", "task-deleg1", lib.StateCompleted, true)
	publishResult(t, js, "cluster-b", "task-deleg2", "child b done")
	publishStatus(t, js, "cluster-b", "task-deleg2", lib.StateCompleted, true)

	var rootReads, c1Reads, c2Reads atomic.Int32
	sub, err := js.Conn().Subscribe("$JS.API.CONSUMER.CREATE.>", func(m *nats.Msg) {
		data := string(m.Data)
		switch {
		case strings.Contains(data, "task-root1"):
			rootReads.Add(1)
		case strings.Contains(data, "task-deleg1"):
			c1Reads.Add(1)
		case strings.Contains(data, "task-deleg2"):
			c2Reads.Add(1)
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sub.Unsubscribe() }()

	// Send the second turn; awaitRunningTurn must settle the prior delegated turn cleanly.
	res2 := callTool(t, s, ToolSendMessage, map[string]any{
		"text":         "second turn after delegated finish",
		"wait_seconds": 5,
	})
	if res2.IsError {
		t.Fatalf("second send_message failed: %s", res2.Content[0].Text)
	}
	mustContain(t, res2.Content[0].Text, fakeAddressee+"/task-root2", answerText)

	if r := rootReads.Load(); r != 0 {
		t.Errorf("awaitRunningTurn re-read finished root task-root1 %d times, want 0", r)
	}
	if c1 := c1Reads.Load(); c1 != 1 {
		t.Errorf("awaitRunningTurn read task-deleg1 %d times, want 1", c1)
	}
	if c2 := c2Reads.Load(); c2 != 1 {
		t.Errorf("awaitRunningTurn read task-deleg2 %d times, want 1", c2)
	}

	convs.mu.Lock()
	first := convs.steps["task-root1"]
	convs.mu.Unlock()
	if first == nil || !first.superseded.Load() {
		t.Fatalf("the first turn's step state after awaitRunningTurn = %v, want it kept and superseded", first)
	}

	// The first turn's streaming client polls again: it settles with a
	// child's answer, not the root's empty one, and the state goes.
	res3 := callTool(t, s, ToolTaskControl, map[string]any{
		"action":       "get",
		"task":         fakeAddressee + "/task-root1",
		"wait_seconds": 5,
		"stream_steps": true,
	})
	if res3.IsError {
		t.Fatalf("task_control get on the first turn failed: %s", res3.Content[0].Text)
	}
	mustContain(t, res3.Content[0].Text, "child b done")
	mustNotContain(t, res3.Content[0].Text, StreamingStepContinuePrefix)
	convs.mu.Lock()
	remainingSteps := len(convs.steps)
	convs.mu.Unlock()
	if remainingSteps != 0 {
		t.Errorf("len(convs.steps) after the first turn settled = %d, want 0", remainingSteps)
	}
}

// A second message on a conversation whose first turn is still being
// streamed must not take the first turn's step state from under its client:
// the client's next poll still follows the delegated child (here still
// running, so it reports the turn running rather than settling on the
// root's empty answer), and settles with the child's answer once the child ends. The mutant this kills: a
// second message that deletes and closes the state (the client's next poll
// starts a bare one that settles on the root at once).
func TestASecondMessageLeavesTheFirstTurnsStreamToItsClient(t *testing.T) {
	url, js := testBus(t)
	nc, err := nats.Connect(url, nats.UserInfo("admin", "admin"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)

	var turnNum atomic.Int32
	_, err = nc.Subscribe("chat.console.*.in", func(m *nats.Msg) {
		token := strings.Split(m.Subject, ".")[2]
		conv := "console:" + token
		var f ConsoleInFrame
		if json.Unmarshal(m.Data, &f) != nil {
			return
		}
		i := turnNum.Add(1)
		root := fmt.Sprintf("task-second-root%d", i)
		publishSubmission(t, js, fakeAddressee, root, conv, f.Text, "")
		if i == 1 {
			publishRun(t, js, fakeAddressee, root, "", false)
			publishArtifact(t, js, fakeAddressee, root, lib.ArtifactDelegate, "cluster-a: inspect")
			publishStatus(t, js, fakeAddressee, root, lib.StateCompleted, true)
			publishSubmission(t, js, "cluster-a", "task-second-child", conv, "inspect a", root)
			publishRun(t, js, "cluster-a", "task-second-child", "", false)
			return
		}
		publishRun(t, js, fakeAddressee, root, answerText, true)
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}

	b := dial(t, url)
	convs := NewConversations().WithEvents(b.Events())
	token, _, err := convs.ResolveCall("", "")
	if err != nil {
		t.Fatal(err)
	}
	res, w, err := b.SendTurn(t.Context(), convs, token, "first delegated turn", 5*time.Second)
	if err != nil || res.Task == nil {
		t.Fatalf("SendTurn: res=%+v err=%v", res, err)
	}
	convs.initTaskStepState(*res.Task, token, DefaultConversation, false, res.Notices, w)
	first, err := b.WaitForTurnStep(t.Context(), convs, w, *res.Task, 500*time.Millisecond)
	if err != nil || !first.StillRunning {
		t.Fatalf("first poll: StillRunning=%v err=%v, want a running turn", first.StillRunning, err)
	}

	// The second message waits out the turn's tasks: the child finishes
	// on the bus while it waits, then the second turn goes.
	go func() {
		time.Sleep(300 * time.Millisecond)
		publishResult(t, js, "cluster-a", "task-second-child", wakeAnswer)
		publishStatus(t, js, "cluster-a", "task-second-child", lib.StateCompleted, true)
	}()
	res2, w2, err := b.SendTurn(t.Context(), convs, token, "second turn", 5*time.Second)
	if err != nil || res2.Task == nil {
		t.Fatalf("second SendTurn: res=%+v err=%v", res2, err)
	}
	w2.close()

	var out TurnStepOutcome
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		out, err = b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, 500*time.Millisecond)
		if err != nil {
			t.Fatalf("first turn's poll after the second message: %v", err)
		}
		if !out.StillRunning {
			break
		}
	}
	if out.StillRunning {
		t.Fatal("the first turn never settled after the second message")
	}
	_, _, resultText := FormatTurnCustomToolEnvelope(out)
	mustContain(t, resultText, wakeAnswer)
	if ref, ok := convs.runningTask(token); !ok || ref != *res2.Task {
		t.Errorf("running task after the first turn settled = %v (%v), want the second turn's %v", ref, ok, *res2.Task)
	}
}

func TestStepStateEvictionPrefersInactiveAndRemovesAllAliasedKeys(t *testing.T) {
	convs := NewConversations()
	activeWatch := &turnWatch{stop: make(chan struct{})}
	activeRoot := TaskRef{Addressee: fakeAddressee, TaskID: "task-active-root"}
	activeState := convs.initTaskStepState(activeRoot, "tok-active", "active", false, nil, activeWatch)
	convs.aliasTaskStepState(TaskRef{Addressee: "cluster-a", TaskID: "task-active-child"}, activeState)

	// Fill remaining slots with inactive (closed) step states, including one with multiple aliased keys.
	aliasedInactive := convs.initTaskStepState(TaskRef{Addressee: fakeAddressee, TaskID: "task-inactive-0"}, "tok-0", "s0", false, nil, nil)
	convs.aliasTaskStepState(TaskRef{Addressee: "cluster-a", TaskID: "task-inactive-0-child1"}, aliasedInactive)
	convs.aliasTaskStepState(TaskRef{Addressee: "cluster-b", TaskID: "task-inactive-0-child2"}, aliasedInactive)

	for i := 1; len(convs.steps) < maxConversations; i++ {
		convs.initTaskStepState(TaskRef{Addressee: fakeAddressee, TaskID: fmt.Sprintf("task-inactive-%d", i)}, fmt.Sprintf("tok-%d", i), "", true, nil, nil)
	}

	// Inserting additional entries at capacity must evict inactive entries (removing all aliased keys)
	// and never evict or close the active turn.
	for i := 0; i < 64; i++ {
		w := &turnWatch{stop: make(chan struct{})}
		st := convs.initTaskStepState(TaskRef{Addressee: fakeAddressee, TaskID: fmt.Sprintf("task-new-%d", i)}, fmt.Sprintf("tok-new-%d", i), "", true, nil, w)
		st.closeAll()
	}

	if !activeState.active.Load() {
		t.Fatal("active taskStepState was evicted/closed while inactive entries were present")
	}
	convs.mu.Lock()
	_, hasRoot := convs.steps[activeRoot.TaskID]
	_, hasChild := convs.steps["task-active-child"]
	totalKeys := len(convs.steps)
	convs.mu.Unlock()
	if !hasRoot || !hasChild {
		t.Fatalf("active step state keys missing from convs.steps (hasRoot=%v, hasChild=%v)", hasRoot, hasChild)
	}
	if totalKeys > maxConversations {
		t.Fatalf("len(convs.steps) = %d, exceeded maxConversations (%d)", totalKeys, maxConversations)
	}
}

// A poll that re-keys a followed task's id from another state closes that
// state only after it has released its own mu, so two polls each evicting
// the other's state cannot deadlock; and it cuts short the other state's
// in-flight poll rather than wait out its window.
func TestAPollClosesAStateItEvictsOnlyAfterReleasingItsOwnLock(t *testing.T) {
	url, js := testBus(t)
	conv := "console:tok-evict"
	root := TaskRef{Addressee: fakeAddressee, TaskID: "task-evict-root"}
	child := TaskRef{Addressee: "cluster-a", TaskID: "task-evict-child"}
	publishSubmission(t, js, root.Addressee, root.TaskID, conv, "delegate", "")
	publishRun(t, js, root.Addressee, root.TaskID, "", false)
	publishArtifact(t, js, root.Addressee, root.TaskID, lib.ArtifactDelegate, "cluster-a: check")
	publishStatus(t, js, root.Addressee, root.TaskID, lib.StateCompleted, true)
	publishSubmission(t, js, child.Addressee, child.TaskID, conv, "check", root.TaskID)
	publishRun(t, js, child.Addressee, child.TaskID, "child answer", true)

	b := dial(t, url)
	convs := NewConversations()
	mine := convs.initTaskStepState(root, "tok-mine", "", true, nil, nil)
	mine.followedRefs = []TaskRef{child}
	// Another state keyed under the child's id, its poll in flight (it
	// holds its mu).
	other := convs.initTaskStepState(child, "tok-other", "", true, nil, nil)
	var otherCancelled atomic.Bool
	other.setCancelPoll(func() { otherCancelled.Store(true) })
	other.mu.Lock()
	otherLocked := true
	defer func() {
		if otherLocked {
			other.mu.Unlock()
		}
	}()

	done := make(chan error, 1)
	go func() {
		_, err := b.WaitForTurnStep(t.Context(), convs, nil, root, 5*time.Second)
		done <- err
	}()
	rekeyed := false
	for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		convs.mu.Lock()
		rekeyed = convs.steps[child.TaskID] == mine
		convs.mu.Unlock()
		if rekeyed {
			break
		}
	}
	if !rekeyed {
		t.Fatal("the poll never keyed its state under the followed task's id")
	}
	// From here mine.mu is held for the rest of the poll; it must come
	// free while the evicted state is still busy.
	released := false
	for deadline := time.Now().Add(delegationSettle + 8*time.Second); time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		if mine.mu.TryLock() {
			mine.mu.Unlock()
			released = true
			break
		}
	}
	if !released {
		t.Error("the poll held its own lock while closing the state it evicted")
	}
	if !otherCancelled.Load() {
		t.Error("the evicted state's in-flight poll was not cut short")
	}
	other.mu.Unlock()
	otherLocked = false
	select {
	case err := <-done:
		if err != nil {
			t.Errorf("WaitForTurnStep: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("WaitForTurnStep did not return once the evicted state was free")
	}
}

func TestConcurrentStepStateOperationsNoDeadlockOrRace(t *testing.T) {
	convs := NewConversations()
	const workers = 8
	const iterations = 100
	var wg sync.WaitGroup
	wg.Add(workers)
	for w := 0; w < workers; w++ {
		go func(id int) {
			defer wg.Done()
			token := fmt.Sprintf("tok-%d", id%2)
			for i := 0; i < iterations; i++ {
				root := TaskRef{Addressee: fakeAddressee, TaskID: fmt.Sprintf("task-r-%d-%d", id, i%4)}
				child := TaskRef{Addressee: "cluster-a", TaskID: fmt.Sprintf("task-c-%d-%d", id, i%4)}
				tw := &turnWatch{
					stop:     make(chan struct{}),
					followed: []TaskRef{child},
				}
				st := convs.getOrCreateTaskStepState(root, tw)
				st.mu.Lock()
				evicted := convs.aliasTaskStepState(child, st)
				convs.setRunning(token, &child)
				st.mu.Unlock()
				closeEvicted(evicted)

				_, _ = convs.knownTaskRef(child.TaskID)
				_ = convs.activeStepFollowedTasks(token, root)
				_, _ = convs.hasActiveStepState(token)
				convs.stopTaskStepState(token, child)
			}
		}(w)
	}
	wg.Wait()
}

type errOnFirstStepBus struct {
	fakeStepTurnBus
	firstErr error
}

func (b *errOnFirstStepBus) SendTurn(_ context.Context, convs *Conversations, token, _ string, _ time.Duration) (TurnResult, *turnWatch, error) {
	r := b.ref
	convs.setRunning(token, &r)
	w := &turnWatch{stop: make(chan struct{}), token: token}
	return TurnResult{Token: token, Task: &r, Notices: []string{"Working on it…"}}, w, nil
}

func (b *errOnFirstStepBus) WaitForTurnStep(ctx context.Context, convs *Conversations, w *turnWatch, root TaskRef, wait time.Duration) (TurnStepOutcome, error) {
	if b.firstErr != nil {
		err := b.firstErr
		b.firstErr = nil
		if tooLargeToRead(err) {
			st := convs.getOrCreateTaskStepState(root, w)
			st.closeAll()
		}
		return TurnStepOutcome{}, err
	}
	return b.fakeStepTurnBus.WaitForTurnStep(ctx, convs, w, root, wait)
}

func TestSendAndStreamStepKeepsStepStateOnTransientReadError(t *testing.T) {
	ref := TaskRef{Addressee: fakeAddressee, TaskID: "task-transient-err"}
	bus := &errOnFirstStepBus{
		fakeStepTurnBus: fakeStepTurnBus{ref: ref},
		firstErr:        errReadersBusy,
	}
	convs := NewConversations()
	s := mustServer(t, SendMessageTool(bus, convs), TaskControlTool(bus, convs))

	res := callTool(t, s, ToolSendMessage, map[string]any{
		"text":         "check cluster",
		"stream_steps": true,
	})
	if res.IsError {
		t.Fatalf("expected non-error degraded message when turn is running, got error: %s", res.Content[0].Text)
	}
	mustContain(t, res.Content[0].Text, "platform/task-transient-err", "reading it failed")

	// Step state and watch must remain active for follow-up task_control get.
	convs.mu.Lock()
	st := convs.steps[ref.TaskID]
	convs.mu.Unlock()
	if st == nil || !st.active.Load() {
		t.Fatalf("expected taskStepState for %s to remain active after transient error, got st=%v", ref, st)
	}

	// Follow-up task_control get with bare task ID resolves via convs.knownTaskRef and succeeds.
	res2 := callTool(t, s, ToolTaskControl, map[string]any{
		"action":       "get",
		"task_id":      ref.TaskID,
		"stream_steps": true,
	})
	if res2.IsError {
		t.Fatalf("follow-up task_control get failed: %s", res2.Content[0].Text)
	}
	mustContain(t, res2.Content[0].Text, StreamingStepContinuePrefix)
}

func TestWaitForTurnStepPersistsDelegationSettleAcrossShortPolls(t *testing.T) {
	url, js := testBus(t)
	nc, err := nats.Connect(url, nats.UserInfo("admin", "admin"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)

	_, err = nc.Subscribe("chat.console.*.in", func(m *nats.Msg) {
		token := strings.Split(m.Subject, ".")[2]
		conv := "console:" + token
		var f ConsoleInFrame
		if json.Unmarshal(m.Data, &f) != nil {
			return
		}
		root := "task-short-poll-root"
		publishSubmission(t, js, fakeAddressee, root, conv, f.Text, "")
		publishRun(t, js, fakeAddressee, root, "", false)
		publishArtifact(t, js, fakeAddressee, root, lib.ArtifactDelegate, "cluster-a: check")
		publishStatus(t, js, fakeAddressee, root, lib.StateCompleted, true)
		publishSubmission(t, js, "cluster-a", "task-short-poll-child", conv, "check", root)
		publishRun(t, js, "cluster-a", "task-short-poll-child", "child finished answer", true)
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}

	b := dial(t, url)
	convs := NewConversations().WithEvents(b.Events())
	token, _, err := convs.ResolveCall("", "")
	if err != nil {
		t.Fatal(err)
	}
	res, w, err := b.SendTurn(t.Context(), convs, token, "delegate short polls", 5*time.Second)
	if err != nil || res.Task == nil {
		t.Fatalf("SendTurn: res=%+v err=%v", res, err)
	}
	convs.initTaskStepState(*res.Task, token, DefaultConversation, false, res.Notices, w)

	// Poll exclusively with a 100ms wait (< delegationSettle = 2s);
	// settledDelegationsAt on taskStepState must persist across calls and settle.
	deadline := time.Now().Add(delegationSettle + 2*time.Second)
	settled := false
	for time.Now().Before(deadline) {
		stepOut, err := b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, 100*time.Millisecond)
		if err != nil {
			t.Fatalf("WaitForTurnStep: %v", err)
		}
		if !stepOut.StillRunning {
			settled = true
			break
		}
		time.Sleep(80 * time.Millisecond)
	}
	if !settled {
		t.Fatal("delegated turn never settled across short-window WaitForTurnStep polls")
	}
}

// A streamed delegated turn whose wake task trails its child's end (the
// relay's lag) settles on the wake task's answer, as the non-streamed wait
// does: not on the child's interim result inside delegationSettle, and not
// at all while no task has answered with a result.
func TestAStreamedDelegationSettlesOnTheWakeTasksAnswer(t *testing.T) {
	for _, tc := range []struct {
		name        string
		childResult bool
		lag         time.Duration
	}{
		{"child with a result", true, time.Second},
		{"child without a result", false, delegationSettle + time.Second},
	} {
		t.Run(tc.name, func(t *testing.T) {
			url, js := testBus(t)
			nc, err := nats.Connect(url, nats.UserInfo("admin", "admin"))
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(nc.Close)
			rootID, childID, wakeID := "task-lag-root", "task-lag-child", "task-lag-wake"
			_, err = nc.Subscribe("chat.console.*.in", func(m *nats.Msg) {
				conv := "console:" + strings.Split(m.Subject, ".")[2]
				var f ConsoleInFrame
				if json.Unmarshal(m.Data, &f) != nil {
					return
				}
				publishSubmission(t, js, fakeAddressee, rootID, conv, f.Text, "")
				publishRun(t, js, fakeAddressee, rootID, "", false)
				publishArtifact(t, js, fakeAddressee, rootID, lib.ArtifactDelegate, "cluster-a: inspect")
				publishStatus(t, js, fakeAddressee, rootID, lib.StateCompleted, true)
				publishSubmission(t, js, "cluster-a", childID, conv, "inspect", rootID)
				publishRun(t, js, "cluster-a", childID, "child notes", tc.childResult)
				if !tc.childResult {
					publishStatus(t, js, "cluster-a", childID, lib.StateCompleted, true)
				}
				go func() {
					time.Sleep(tc.lag)
					publishSubmission(t, js, fakeAddressee, wakeID, conv, "child finished", childID)
					publishRun(t, js, fakeAddressee, wakeID, wakeAnswer, true)
				}()
			})
			if err != nil {
				t.Fatal(err)
			}
			if err := nc.Flush(); err != nil {
				t.Fatal(err)
			}
			b := dial(t, url)
			convs := NewConversations().WithEvents(b.Events())
			token, _, err := convs.ResolveCall("", "")
			if err != nil {
				t.Fatal(err)
			}
			res, w, err := b.SendTurn(t.Context(), convs, token, "delegate", 5*time.Second)
			if err != nil || res.Task == nil {
				t.Fatalf("SendTurn: res=%+v err=%v", res, err)
			}
			convs.initTaskStepState(*res.Task, token, DefaultConversation, false, res.Notices, w)
			var out TurnStepOutcome
			for i := 0; i < 40; i++ {
				out, err = b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, 60*time.Second)
				if err != nil {
					t.Fatalf("WaitForTurnStep: %v", err)
				}
				if !out.StillRunning {
					break
				}
			}
			if out.StillRunning {
				t.Fatal("the streamed delegated turn never settled")
			}
			_, _, txt := FormatTurnCustomToolEnvelope(out)
			if out.Outcome.AnswerFrom == nil || out.Outcome.AnswerFrom.TaskID != wakeID {
				t.Errorf("answer from %v; want %s/%s", out.Outcome.AnswerFrom, fakeAddressee, wakeID)
			}
			mustContain(t, txt, wakeAnswer)
		})
	}
}

// A poll whose caller is cut off mid-poll leaves the step state as it was:
// mid-turn its steps stay unseen, and on the settling poll the state is not
// torn down, so the caller's retry reads the same steps on the same turn.
func TestACutOffStreamedPollLeavesItsStepsForTheRetry(t *testing.T) {
	for _, tc := range []struct {
		name   string
		finish bool
		cutOff time.Duration
	}{
		// Inside minStepBatchWait, before the poll would return the step.
		{"mid-turn", false, 300 * time.Millisecond},
		// While the settling poll gathers the cards' reports.
		{"settling", true, 1500 * time.Millisecond},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// The settling poll gathers the handed-off cards' reports
			// until its caller is cut off.
			setReportQuiet(t, 30*time.Second)
			url, js := testBus(t)
			nc, err := nats.Connect(url, nats.UserInfo("admin", "admin"))
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(nc.Close)
			rootID := "task-cutoff-root"
			stepPart := mustStepPart(t, stepmsg.Step{Kind: stepmsg.KindSynthesis, Title: "Summarising the nodes"})
			_, err = nc.Subscribe("chat.console.*.in", func(m *nats.Msg) {
				conv := "console:" + strings.Split(m.Subject, ".")[2]
				var f ConsoleInFrame
				if json.Unmarshal(m.Data, &f) != nil {
					return
				}
				publishSubmission(t, js, fakeAddressee, rootID, conv, f.Text, "")
				publishRun(t, js, fakeAddressee, rootID, "", false)
				publishParts(t, js, fakeAddressee, rootID, stepmsg.ArtifactName, stepPart)
				if tc.finish {
					publishArtifact(t, js, fakeAddressee, rootID, lib.ArtifactHandoff, "cards")
					publishResult(t, js, fakeAddressee, rootID, answerText)
					publishStatus(t, js, fakeAddressee, rootID, lib.StateCompleted, true)
				}
			})
			if err != nil {
				t.Fatal(err)
			}
			if err := nc.Flush(); err != nil {
				t.Fatal(err)
			}
			b := dial(t, url)
			convs := NewConversations().WithEvents(b.Events())
			token, _, err := convs.ResolveCall("", "")
			if err != nil {
				t.Fatal(err)
			}
			res, w, err := b.SendTurn(t.Context(), convs, token, "check the nodes", 5*time.Second)
			if err != nil || res.Task == nil {
				t.Fatalf("SendTurn: res=%+v err=%v", res, err)
			}
			st := convs.initTaskStepState(*res.Task, token, DefaultConversation, false, res.Notices, w)

			cut, cancel := context.WithTimeout(t.Context(), tc.cutOff)
			defer cancel()
			if _, err := b.WaitForTurnStep(cut, convs, nil, *res.Task, 10*time.Second); err == nil {
				t.Errorf("a cut-off poll returned no error")
			}
			convs.mu.Lock()
			kept := convs.steps[rootID] == st
			convs.mu.Unlock()
			if !kept {
				t.Errorf("the cut-off poll removed the turn's step state")
			}

			retry, err := b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, time.Second)
			if err != nil {
				t.Fatalf("retry WaitForTurnStep: %v", err)
			}
			if len(retry.NewStepDetails) != 1 {
				t.Errorf("retry new steps = %+v; want the step the cut-off poll read", retry.NewStepDetails)
			}
			if retry.Token != token {
				t.Errorf("retry token = %q; want the turn's %q", retry.Token, token)
			}
			if retry.StillRunning == tc.finish {
				t.Errorf("retry still running = %v; want %v", retry.StillRunning, !tc.finish)
			}
		})
	}
}

func TestWaitForTurnStepReusesTaskWatchAcrossPollsWithReapDelay(t *testing.T) {
	url, js := testBus(t)
	nc, err := nats.Connect(url, nats.UserInfo("admin", "admin"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)

	rootID := "task-reuse-watch-root"
	_, err = nc.Subscribe("chat.console.*.in", func(m *nats.Msg) {
		token := strings.Split(m.Subject, ".")[2]
		conv := "console:" + token
		var f ConsoleInFrame
		if json.Unmarshal(m.Data, &f) != nil {
			return
		}
		publishSubmission(t, js, fakeAddressee, rootID, conv, f.Text, "")
		publishRun(t, js, fakeAddressee, rootID, "", false)
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}

	var created atomic.Int32
	sub, err := js.Conn().Subscribe("$JS.API.CONSUMER.CREATE.>", func(m *nats.Msg) {
		if strings.Contains(string(m.Data), rootID) {
			created.Add(1)
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sub.Unsubscribe() }()

	b := dial(t, url)
	// Use the production 5s reapDelay so releasing a reader on every poll would
	// exhaust maxTaskReaders (8) before 12 polls complete.
	b.reapDelay = taskConsumerIdle
	convs := NewConversations().WithEvents(b.Events())
	token, _, err := convs.ResolveCall("", "")
	if err != nil {
		t.Fatal(err)
	}
	res, w, err := b.SendTurn(t.Context(), convs, token, "long streaming turn", 5*time.Second)
	if err != nil || res.Task == nil {
		t.Fatalf("SendTurn: res=%+v err=%v", res, err)
	}
	convs.initTaskStepState(*res.Task, token, DefaultConversation, false, res.Notices, w)

	// Poll 12 times (> maxTaskReaders = 8) while the task is still running.
	for i := 0; i < 12; i++ {
		stepOut, err := b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, 60*time.Millisecond)
		if err != nil {
			t.Fatalf("WaitForTurnStep poll %d failed: %v", i+1, err)
		}
		if !stepOut.StillRunning {
			t.Fatalf("WaitForTurnStep poll %d unexpectedly finished early", i+1)
		}
	}

	publishResult(t, js, fakeAddressee, rootID, answerText)
	publishStatus(t, js, fakeAddressee, rootID, lib.StateCompleted, true)

	stepOut, err := b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, 2*time.Second)
	if err != nil {
		t.Fatalf("final WaitForTurnStep failed: %v", err)
	}
	if stepOut.StillRunning {
		t.Fatal("expected turn to finish after completed status")
	}
	if n := created.Load(); n != 1 {
		t.Errorf("created %d JetStream consumers across 13 polls, want 1", n)
	}
}

// publishParts puts one artifact with the given parts on a task's events.
func publishParts(t *testing.T, js jetstream.JetStream, addressee, taskID, name string, parts ...lib.Part) {
	t.Helper()
	p := mustJSON(t, lib.ArtifactUpdate{TaskID: taskID, ContextID: "ctx-" + taskID,
		Artifact: lib.Artifact{Name: name, Parts: parts}, LastChunk: true})
	env, err := lib.NewArtifactUpdateEnvelope(executor(addressee), taskID, "ctx-"+taskID, "corr-"+taskID, p)
	if err != nil {
		t.Fatal(err)
	}
	pub(t, js, lib.TaskEventsSubject(addressee, taskID), env)
}

// The api executor publishes a plan step with its call ids when a call
// starts and the call's activity only when it ends. A call that outlasts
// stepActivityWaitMax has its step reported bare; the step is reported
// again with its Tool Activity once the activity lands.
func TestWaitForTurnStepReportsALateActivityOfAStepAlreadyShown(t *testing.T) {
	url, js := testBus(t)
	nc, err := nats.Connect(url, nats.UserInfo("admin", "admin"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)

	rootID := "task-late-activity-root"
	stepPart := mustStepPart(t, stepmsg.Step{Kind: stepmsg.KindPlan, Title: "Checking cluster nodes", CallIDs: []string{"c1"}})
	_, err = nc.Subscribe("chat.console.*.in", func(m *nats.Msg) {
		token := strings.Split(m.Subject, ".")[2]
		conv := "console:" + token
		var f ConsoleInFrame
		if json.Unmarshal(m.Data, &f) != nil {
			return
		}
		publishSubmission(t, js, fakeAddressee, rootID, conv, f.Text, "")
		publishRun(t, js, fakeAddressee, rootID, "", false)
		publishParts(t, js, fakeAddressee, rootID, stepmsg.ArtifactName, stepPart)
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}

	b := dial(t, url)
	convs := NewConversations().WithEvents(b.Events())
	token, _, err := convs.ResolveCall("", "")
	if err != nil {
		t.Fatal(err)
	}
	res, w, err := b.SendTurn(t.Context(), convs, token, "check the nodes", 5*time.Second)
	if err != nil || res.Task == nil {
		t.Fatalf("SendTurn: res=%+v err=%v", res, err)
	}
	convs.initTaskStepState(*res.Task, token, DefaultConversation, false, res.Notices, w)

	first, err := b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, 3*time.Second)
	if err != nil {
		t.Fatalf("first WaitForTurnStep: %v", err)
	}
	if !first.StillRunning || len(first.NewStepDetails) != 1 || len(first.NewStepDetails[0].Activities) != 0 {
		t.Fatalf("first poll = running %v, new steps %+v; want the bare plan step", first.StillRunning, first.NewStepDetails)
	}

	publishParts(t, js, fakeAddressee, rootID, lib.ArtifactActivity,
		mustDataPart(t, map[string]any{"tool": "kubectl", "callId": "c1", "title": "kubectl get nodes", "status": "completed"}))

	second, err := b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, 3*time.Second)
	if err != nil {
		t.Fatalf("second WaitForTurnStep: %v", err)
	}
	if len(second.NewStepDetails) != 1 || len(second.NewStepDetails[0].Activities) != 1 {
		t.Fatalf("second poll new steps = %+v, want the step again with its activity", second.NewStepDetails)
	}
	if second.LastReportedSteps != 1 {
		t.Errorf("second poll LastReportedSteps = %d, want 1 (the step is not counted twice)", second.LastReportedSteps)
	}
	if _, md, _ := FormatTurnCustomToolEnvelope(second); !strings.Contains(md, "**Tool Activity**") {
		t.Errorf("second poll markdown has no Tool Activity line:\n%s", md)
	}

	third, err := b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, 300*time.Millisecond)
	if err != nil {
		t.Fatalf("third WaitForTurnStep: %v", err)
	}
	if len(third.NewStepDetails) != 0 {
		t.Errorf("third poll new steps = %+v, want none (the step is reported whole)", third.NewStepDetails)
	}
}

func TestWaitForTurnStepFollowsDelegatedChildAfterTurnWatchCloses(t *testing.T) {
	url, js := testBus(t)
	nc, err := nats.Connect(url, nats.UserInfo("admin", "admin"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)

	rootID := "task-long-deleg-root"
	childID := "task-long-deleg-child"
	_, err = nc.Subscribe("chat.console.*.in", func(m *nats.Msg) {
		token := strings.Split(m.Subject, ".")[2]
		conv := "console:" + token
		var f ConsoleInFrame
		if json.Unmarshal(m.Data, &f) != nil {
			return
		}
		publishSubmission(t, js, fakeAddressee, rootID, conv, f.Text, "")
		publishRun(t, js, fakeAddressee, rootID, "", false)
		publishArtifact(t, js, fakeAddressee, rootID, lib.ArtifactDelegate, "cluster-a: inspect")
		publishStatus(t, js, fakeAddressee, rootID, lib.StateCompleted, true)
		publishSubmission(t, js, "cluster-a", childID, conv, "inspect", rootID)
		publishRun(t, js, "cluster-a", childID, "", false)
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}

	b := dial(t, url)
	convs := NewConversations().WithEvents(b.Events())
	token, _, err := convs.ResolveCall("", "")
	if err != nil {
		t.Fatal(err)
	}
	res, w, err := b.SendTurn(t.Context(), convs, token, "delegate long child", 5*time.Second)
	if err != nil || res.Task == nil {
		t.Fatalf("SendTurn: res=%+v err=%v", res, err)
	}
	st := convs.initTaskStepState(*res.Task, token, DefaultConversation, false, res.Notices, w)

	// Poll 1 discovers the running child task.
	stepOut1, err := b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, 200*time.Millisecond)
	if err != nil {
		t.Fatalf("WaitForTurnStep 1: %v", err)
	}
	if !stepOut1.StillRunning {
		t.Fatal("expected delegated turn to still be running while child is working")
	}

	// Simulate watchTimer expiring (st.closeAll) while child is still running.
	st.closeAll()

	// Next poll must still see the unfinished child via persisted st.followedRefs, not settle early.
	stepOut2, err := b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, 150*time.Millisecond)
	if err != nil {
		t.Fatalf("WaitForTurnStep 2: %v", err)
	}
	if !stepOut2.StillRunning {
		t.Fatal("expected delegated turn to stay running after st.closeAll while child is unfinished")
	}

	// Finish the child and verify the turn settles with the child's answer.
	publishResult(t, js, "cluster-a", childID, wakeAnswer)
	publishStatus(t, js, "cluster-a", childID, lib.StateCompleted, true)

	// Settling waits out delegationSettle after the child's end, so the
	// deadline has to outlast it.
	deadline := time.Now().Add(delegationSettle + 3*time.Second)
	var finalOut TurnStepOutcome
	for time.Now().Before(deadline) {
		finalOut, err = b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, 150*time.Millisecond)
		if err != nil {
			t.Fatalf("WaitForTurnStep final: %v", err)
		}
		if !finalOut.StillRunning {
			break
		}
		time.Sleep(80 * time.Millisecond)
	}
	if finalOut.StillRunning {
		t.Fatal("delegated turn did not settle after child completed")
	}
	_, _, resultText := FormatTurnCustomToolEnvelope(finalOut)
	mustContain(t, resultText, wakeAnswer)
}

// stubFetcher is a msgFetcher that fetches nothing and counts Unsubscribe,
// so a taskWatch built on it says whether it was closed.
type stubFetcher struct{ unsubscribed atomic.Int32 }

func (f *stubFetcher) Fetch(int, ...nats.PullOpt) ([]*nats.Msg, error) {
	return nil, nats.ErrTimeout
}

func (f *stubFetcher) Unsubscribe() error {
	f.unsubscribed.Add(1)
	return nil
}

// An idle-release firing that was waiting on the state's lock while a new
// poll ran is stale once that poll re-arms (or disarms) the release: it
// must leave the watches the poll just kept alone. Only the armed
// generation releases them. The mutant this kills: a release that ignores
// its generation and closes whatever watches it finds.
func TestIdleReleaseIgnoresAStaleGeneration(t *testing.T) {
	ref := TaskRef{Addressee: fakeAddressee, TaskID: "task-stale-idle"}
	f := &stubFetcher{}
	st := &taskStepState{
		ref: ref,
		taskWatches: map[TaskRef]*taskWatch{
			ref: {ref: ref, r: &budgetReader{sub: f}, release: func(bool) {}},
		},
	}

	st.mu.Lock()
	st.armIdleReleaseLocked()
	stale := st.idleGen
	// The next poll begins: it disarms the pending release, polls, and
	// arms a fresh one. The first timer's body then gets the lock.
	st.disarmIdleReleaseLocked()
	st.armIdleReleaseLocked()
	current := st.idleGen
	st.mu.Unlock()

	st.releaseIdleWatches(stale)
	st.mu.Lock()
	kept := len(st.taskWatches) == 1 && st.idleTimer != nil && st.idleGen == current
	st.mu.Unlock()
	if !kept || f.unsubscribed.Load() != 0 {
		t.Fatalf("a stale idle release closed the watches a later poll kept (watches kept=%v, unsubscribed=%d)", kept, f.unsubscribed.Load())
	}

	st.releaseIdleWatches(current)
	st.mu.Lock()
	released := len(st.taskWatches) == 0 && st.idleTimer == nil
	st.mu.Unlock()
	if !released || f.unsubscribed.Load() != 1 {
		t.Fatalf("the armed idle release did not close the watches (released=%v, unsubscribed=%d)", released, f.unsubscribed.Load())
	}
}

// The gateway's relay mints a delegating turn's child after the root's
// terminal, and can trail it by minutes (its relayLagGrace). A streamed
// turn whose child arrives later than the old two-second follow grace is
// still followed: the polls before it report the turn running, and the
// child's answer is the turn's. The mutant this kills: a poll that settles
// on the root's own answer once a short grace passes with no child.
func TestAChildThatTrailsItsDelegatingRootIsStillStreamed(t *testing.T) {
	url, js := testBus(t)
	nc, err := nats.Connect(url, nats.UserInfo("admin", "admin"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)

	rootID := "task-trailing-root"
	childID := "task-trailing-child"
	_, err = nc.Subscribe("chat.console.*.in", func(m *nats.Msg) {
		token := strings.Split(m.Subject, ".")[2]
		conv := "console:" + token
		var f ConsoleInFrame
		if json.Unmarshal(m.Data, &f) != nil {
			return
		}
		publishSubmission(t, js, fakeAddressee, rootID, conv, f.Text, "")
		publishRun(t, js, fakeAddressee, rootID, "", false)
		publishArtifact(t, js, fakeAddressee, rootID, lib.ArtifactDelegate, "cluster-a: inspect")
		publishStatus(t, js, fakeAddressee, rootID, lib.StateCompleted, true)
		go func() {
			time.Sleep(3 * time.Second)
			publishSubmission(t, js, "cluster-a", childID, conv, "inspect", rootID)
			publishRun(t, js, "cluster-a", childID, "", false)
			publishResult(t, js, "cluster-a", childID, wakeAnswer)
			publishStatus(t, js, "cluster-a", childID, lib.StateCompleted, true)
		}()
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}

	b := dial(t, url)
	convs := NewConversations().WithEvents(b.Events())
	token, _, err := convs.ResolveCall("", "")
	if err != nil {
		t.Fatal(err)
	}
	res, w, err := b.SendTurn(t.Context(), convs, token, "delegate trailing child", 5*time.Second)
	if err != nil || res.Task == nil {
		t.Fatalf("SendTurn: res=%+v err=%v", res, err)
	}
	convs.initTaskStepState(*res.Task, token, DefaultConversation, false, res.Notices, w)

	var out TurnStepOutcome
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		out, err = b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, 500*time.Millisecond)
		if err != nil {
			t.Fatalf("WaitForTurnStep: %v", err)
		}
		if !out.StillRunning {
			break
		}
	}
	if out.StillRunning {
		t.Fatal("the delegated turn never settled after its trailing child finished")
	}
	_, _, resultText := FormatTurnCustomToolEnvelope(out)
	mustContain(t, resultText, wakeAnswer)
}

// A stop is not held up by a streamed poll in flight on the conversation
// (WaitForTurnStep holds the step state's lock for its whole window): it
// goes out, and task_control returns, well inside the window. The mutants
// this kills: a stop that discards the step state before sending, and a
// discard that waits for the poll's lock rather than cancel the poll.
func TestAStopDuringAStreamedPollGoesOutPromptly(t *testing.T) {
	url, js := testBus(t)
	frames := &frameLog{}
	runFakeGateway(t, url, js, gatewayScenario{
		root:   func(int32) (string, bool) { return "task-stop-root", false },
		frames: frames,
	})
	b := dial(t, url)
	convs := NewConversations().WithEvents(b.Events())
	s := mustServer(t, SendMessageTool(b, convs), TaskControlTool(b, convs))

	res1 := callTool(t, s, ToolSendMessage, map[string]any{
		"text":         "a long streamed turn",
		"wait_seconds": 1,
		"stream_steps": true,
	})
	if res1.IsError {
		t.Fatalf("send_message failed: %s", res1.Content[0].Text)
	}
	mustContain(t, res1.Content[0].Text, StreamingStepContinuePrefix)

	polled := make(chan time.Duration, 1)
	go func() {
		start := time.Now()
		_, _ = b.WaitForTurnStep(context.Background(), convs, nil, TaskRef{Addressee: fakeAddressee, TaskID: "task-stop-root"}, 0)
		polled <- time.Since(start)
	}()
	time.Sleep(500 * time.Millisecond) // the poll holds the state's lock

	start := time.Now()
	res := callTool(t, s, ToolTaskControl, map[string]any{"action": "stop"})
	elapsed := time.Since(start)
	if res.IsError {
		t.Fatalf("stop failed: %s", res.Content[0].Text)
	}
	mustContain(t, res.Content[0].Text, "Stopped the running task")
	if elapsed > 2*time.Second {
		t.Errorf("stop during a streamed poll took %s, want well under the poll window", elapsed)
	}
	select {
	case d := <-polled:
		if d > 3*time.Second {
			t.Errorf("the in-flight poll ran %s after the stop, want it cut short", d)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the in-flight poll never returned")
	}
	convs.mu.Lock()
	st := convs.steps["task-stop-root"]
	convs.mu.Unlock()
	if st == nil {
		t.Fatal("the stop dropped the streamed step state, want it kept closed")
	}
	st.mu.Lock()
	open := st.active.Load() || st.watch != nil || len(st.taskWatches) > 0
	st.mu.Unlock()
	if open {
		t.Error("the streamed step state is still open after the stop, want its watches closed")
	}
}

// failingStopBus is a step bus whose stop never goes out.
type failingStopBus struct{ fakeStepTurnBus }

func (b *failingStopBus) SendStop(context.Context, *Conversations, string) (TurnResult, error) {
	return TurnResult{}, errors.New("publish failed")
}

// A stop that fails to go out leaves the conversation's streamed step state
// as it was: its turn is still running, and a client polling it must not
// start over and repeat every step. The mutant this kills: discarding the
// state before the stop is sent.
func TestAFailedStopKeepsTheStreamedStepState(t *testing.T) {
	ref := TaskRef{Addressee: fakeAddressee, TaskID: "task-failed-stop"}
	bus := &failingStopBus{fakeStepTurnBus{ref: ref}}
	convs := NewConversations()
	token, _, err := convs.ResolveCall("", "")
	if err != nil {
		t.Fatal(err)
	}
	st := convs.initTaskStepState(ref, token, DefaultConversation, false, nil, &turnWatch{stop: make(chan struct{})})
	s := mustServer(t, TaskControlTool(bus, convs))

	res := callTool(t, s, ToolTaskControl, map[string]any{"action": "stop"})
	if !res.IsError {
		t.Fatalf("a failed stop reported success: %s", res.Content[0].Text)
	}
	convs.mu.Lock()
	kept := convs.steps[ref.TaskID] == st
	convs.mu.Unlock()
	if !kept || !st.active.Load() {
		t.Errorf("a failed stop discarded the streamed step state (kept=%v, active=%v)", kept, st.active.Load())
	}
	st.closeAll()
}

// A stop closes the streamed step state but keeps the steps it reported: the
// stopped turn's closing poll, which the conversation's running task still
// leads to, reports it canceled without sending every step again. The mutant
// this kills: a stop that drops the state, so the closing poll starts over.
func TestTheClosingPollAfterAStopDoesNotRepeatSteps(t *testing.T) {
	url, js := testBus(t)
	nc, err := nats.Connect(url, nats.UserInfo("admin", "admin"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)

	rootID := "task-stop-repeat-root"
	stepPart := mustStepPart(t, stepmsg.Step{Kind: stepmsg.KindPlan, Title: "Checking cluster nodes", CallIDs: []string{"c1"}})
	_, err = nc.Subscribe("chat.console.*.in", func(m *nats.Msg) {
		token := strings.Split(m.Subject, ".")[2]
		conv := "console:" + token
		var f ConsoleInFrame
		if json.Unmarshal(m.Data, &f) != nil {
			return
		}
		publishSubmission(t, js, fakeAddressee, rootID, conv, f.Text, "")
		publishRun(t, js, fakeAddressee, rootID, "", false)
		publishParts(t, js, fakeAddressee, rootID, stepmsg.ArtifactName, stepPart)
		publishParts(t, js, fakeAddressee, rootID, lib.ArtifactActivity,
			mustDataPart(t, map[string]any{"tool": "kubectl", "callId": "c1", "title": "kubectl get nodes", "status": "completed"}))
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}

	b := dial(t, url)
	convs := NewConversations().WithEvents(b.Events())
	token, _, err := convs.ResolveCall("", "")
	if err != nil {
		t.Fatal(err)
	}
	res, w, err := b.SendTurn(t.Context(), convs, token, "check the nodes", 5*time.Second)
	if err != nil || res.Task == nil {
		t.Fatalf("SendTurn: res=%+v err=%v", res, err)
	}
	convs.initTaskStepState(*res.Task, token, DefaultConversation, false, res.Notices, w)

	var first TurnStepOutcome
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) && len(first.StepDetails) == 0 {
		first, err = b.WaitForTurnStep(t.Context(), convs, nil, *res.Task, 3*time.Second)
		if err != nil {
			t.Fatalf("first WaitForTurnStep: %v", err)
		}
	}
	if !first.StillRunning || len(first.StepDetails) != 1 {
		t.Fatalf("first poll = running %v, steps %+v; want the running step", first.StillRunning, first.StepDetails)
	}

	convs.stopTaskStepState(token)
	publishStatus(t, js, fakeAddressee, rootID, lib.StateCanceled, true)

	ref, ok := convs.knownTaskRef(rootID)
	if !ok {
		t.Fatal("the stopped turn's task no longer resolves")
	}
	closing, err := b.WaitForTurnStep(t.Context(), convs, nil, ref, 3*time.Second)
	if err != nil {
		t.Fatalf("closing WaitForTurnStep: %v", err)
	}
	if closing.StillRunning {
		t.Fatal("the closing poll after the stop still reads the turn as running")
	}
	if len(closing.NewStepDetails) != 0 {
		t.Errorf("the closing poll after the stop repeats steps already shown: %+v", closing.NewStepDetails)
	}
	convs.mu.Lock()
	remaining := len(convs.steps)
	convs.mu.Unlock()
	if remaining != 0 {
		t.Errorf("len(convs.steps) after the closing poll = %d, want 0", remaining)
	}
}

// A streamed delegated turn following more live tasks than
// maxStepTaskWatches holds no more watches than that between polls, so the
// server's other readers still find a reader slot. The mutant this kills: a
// state that holds a watch on every followed task, pinning every slot.
func TestAStreamedTurnLeavesAReaderSlotForOthers(t *testing.T) {
	url, js := testBus(t)
	b := dial(t, url)
	b.reapDelay = 0
	convs := NewConversations().WithEvents(b.Events())

	root := TaskRef{Addressee: fakeAddressee, TaskID: "task-slots-root"}
	publishSubmission(t, js, fakeAddressee, root.TaskID, "console:slots", "fan out", "")
	publishRun(t, js, fakeAddressee, root.TaskID, "", false)
	publishArtifact(t, js, fakeAddressee, root.TaskID, lib.ArtifactDelegate, "cluster-a: inspect")
	publishStatus(t, js, fakeAddressee, root.TaskID, lib.StateCompleted, true)
	var children []TaskRef
	for i := range maxFollowedTasks {
		child := TaskRef{Addressee: "cluster-a", TaskID: fmt.Sprintf("task-slots-child%d", i)}
		publishSubmission(t, js, child.Addressee, child.TaskID, "console:slots", "inspect", root.TaskID)
		publishRun(t, js, child.Addressee, child.TaskID, "", false)
		children = append(children, child)
	}
	if len(children) <= maxStepTaskWatches {
		t.Fatalf("the test needs more followed tasks (%d) than maxStepTaskWatches (%d)", len(children), maxStepTaskWatches)
	}
	tw := &turnWatch{stop: make(chan struct{}), followed: children}
	st := convs.getOrCreateTaskStepState(root, tw)
	t.Cleanup(st.closeAll)

	// A window long enough to reach every followed task: each live one's
	// read waits stepPollWait.
	out, err := b.WaitForTurnStep(t.Context(), convs, nil, root, time.Duration(maxFollowedTasks+2)*stepPollWait)
	if err != nil {
		t.Fatalf("WaitForTurnStep: %v", err)
	}
	if !out.StillRunning {
		t.Fatal("the turn settled with its followed tasks still running")
	}
	st.mu.Lock()
	held := len(st.taskWatches)
	st.mu.Unlock()
	if held > maxStepTaskWatches {
		t.Errorf("the streamed state holds %d task watches, want at most %d", held, maxStepTaskWatches)
	}
	if _, err := b.ReadTask(t.Context(), root); err != nil {
		t.Errorf("a concurrent ReadTask = %v, want a free reader slot", err)
	}
}

// A poll waiting out delegationSettle on tasks all read final (and cached)
// does not parse their steps again on every idle tick: nothing it reads can
// change. The mutant this kills: re-collecting the steps each iteration.
func TestAPollOnCachedFinalTasksDoesNotReparseThem(t *testing.T) {
	url, js := testBus(t)
	b := dial(t, url)
	convs := NewConversations().WithEvents(b.Events())

	root := TaskRef{Addressee: fakeAddressee, TaskID: "task-cached-root"}
	child := TaskRef{Addressee: "cluster-a", TaskID: "task-cached-child"}
	publishSubmission(t, js, fakeAddressee, root.TaskID, "console:cached", "delegate", "")
	publishRun(t, js, fakeAddressee, root.TaskID, "", false)
	publishArtifact(t, js, fakeAddressee, root.TaskID, lib.ArtifactDelegate, "cluster-a: inspect")
	publishStatus(t, js, fakeAddressee, root.TaskID, lib.StateCompleted, true)
	publishSubmission(t, js, child.Addressee, child.TaskID, "console:cached", "inspect", root.TaskID)
	publishRun(t, js, child.Addressee, child.TaskID, wakeAnswer, true)

	st := convs.getOrCreateTaskStepState(root, &turnWatch{stop: make(chan struct{}), followed: []TaskRef{child}})
	t.Cleanup(st.closeAll)

	var collects atomic.Int32
	collectTurnStepDetails = func(out TurnOutcome) ([]StepDetail, int) {
		collects.Add(1)
		return CollectTurnStepDetails(out)
	}
	t.Cleanup(func() { collectTurnStepDetails = CollectTurnStepDetails })

	// Inside delegationSettle: the chain is answered but not yet settled.
	out, err := b.WaitForTurnStep(t.Context(), convs, nil, root, delegationSettle/2)
	if err != nil {
		t.Fatalf("WaitForTurnStep: %v", err)
	}
	if !out.StillRunning {
		t.Fatal("the turn settled inside delegationSettle")
	}
	// The first look reads both tasks off the bus; every look after it
	// finds them cached.
	if n := collects.Load(); n > 2 {
		t.Errorf("the poll parsed the cached tasks' steps %d times, want at most 2", n)
	}
}
