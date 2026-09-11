package workeradapter

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The suite proves the adapter's half of the lifecycle conformance
// assertions against a real embedded JetStream server and a stub harness
// that speaks the stream-json contract: 9 (first event submitted), 10 (one
// final, nothing after it), 12 (follow-up during working delivered, no
// state change), 13 (cancel always ends in a terminal), 14/15 (correlation
// verbatim), 18 (completed carries result), and 21 (a steer reaches the
// harness stdin exactly once, including under redelivery - the assertion
// whose executable test the payload spec assigns to the worker adapter).

var testPort atomic.Int64

func init() { testPort.Store(26222) }

func startServer(t *testing.T) string {
	t.Helper()
	opts := &server.Options{
		Host:      "127.0.0.1",
		Port:      int(testPort.Add(1)),
		JetStream: true,
		StoreDir:  t.TempDir(),
		NoLog:     true,
		NoSigs:    true,
	}
	s, err := server.NewServer(opts)
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats server not ready")
	}
	t.Cleanup(s.Shutdown)

	url := fmt.Sprintf("nats://127.0.0.1:%d", opts.Port)
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("provision connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("provision jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{
		Name:      lib.TasksStream,
		Subjects:  []string{"a2a.tasks.>"},
		Retention: jetstream.LimitsPolicy,
		MaxAge:    72 * time.Hour,
	}); err != nil {
		t.Fatalf("provision TASKS: %v", err)
	}
	return url
}

func testClient(t *testing.T, url string) *lib.Client {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	c, err := lib.Connect(ctx, url)
	if err != nil {
		t.Fatalf("test client: %v", err)
	}
	t.Cleanup(c.Close)
	return c
}

// stub writes a shell script standing in for the harness binary and returns
// the argv to run it.
// stub writes a fake harness. BASH, not sh, and the difference is not
// cosmetic: the steer stub below paces itself with `read -t`, which is a bash
// extension POSIX does not have. macOS /bin/sh is bash and accepts it; Ubuntu
// /bin/sh is dash and answers "read: Illegal option -t", so on CI the read
// returned instantly instead of waiting.
//
// That did not fail loudly. It made the task race through `working` to
// terminal before waitState's poller could observe the transition, so the test
// failed as "task never reached working" -- a state machine complaint about a
// shell portability bug. Raising the deadline only made it fail slower, which
// is how it was misdiagnosed as a slow-runner flake first time round.
func stub(t *testing.T, body string) []string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "stub.sh")
	if err := os.WriteFile(path, []byte("#!/bin/bash\n"+body), 0o755); err != nil {
		t.Fatalf("write stub: %v", err)
	}
	return []string{"/bin/bash", path}
}

var gatewayParty = lib.Party{Session: "gateway", AgentType: "a2a-gateway"}

// submit publishes a task submission the way the gateway does and returns
// the origin envelope.
func submit(t *testing.T, c *lib.Client, addressee, taskID, text string) *lib.Envelope {
	t.Helper()
	parts := []lib.Part{{Kind: "text", Text: text}}
	if text == "" {
		parts = []lib.Part{{Kind: "data", Data: json.RawMessage(`{"structured":"only"}`)}}
	}
	payload, err := json.Marshal(lib.Message{
		Role: "user", Parts: parts, MessageID: "msg-" + taskID,
		TaskID: taskID, ContextID: "ctx-" + taskID,
	})
	if err != nil {
		t.Fatalf("marshal message: %v", err)
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, "ctx-"+taskID, "corr-"+taskID,
		payload, lib.WithTo(lib.Party{Session: addressee}))
	if err != nil {
		t.Fatalf("submission envelope: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := c.Publish(ctx, lib.TaskInSubject(addressee, taskID), env); err != nil {
		t.Fatalf("publish submission: %v", err)
	}
	return env
}

func adapterConfig(url, taskID, session string, harness []string) Config {
	return Config{
		NATSURL:        url,
		TaskID:         taskID,
		Profile:        "chat",
		Session:        session,
		HarnessCommand: harness,
		HarnessEnv:     os.Environ(),
		TaskDeadline:   20 * time.Second,
		KillGrace:      500 * time.Millisecond,
	}
}

type runOutcome struct {
	res Result
	err error
}

func runAdapter(ctx context.Context, cfg Config) <-chan runOutcome {
	done := make(chan runOutcome, 1)
	go func() {
		res, err := Run(ctx, cfg)
		done <- runOutcome{res, err}
	}()
	return done
}

func waitOutcome(t *testing.T, done <-chan runOutcome, within time.Duration) runOutcome {
	t.Helper()
	select {
	case out := <-done:
		return out
	case <-time.After(within):
		t.Fatal("adapter did not finish in time")
		return runOutcome{}
	}
}

// waitState polls the fold until the task reaches the given state.
// waitDeadline bounds the polling helpers below. It is deliberately generous:
// every one of them returns the moment the condition holds, so the value costs
// a passing run nothing and only decides how long a genuine failure takes to
// report. 15s was not generous enough -- TestAssertion21_SteerReachesStdinExactlyOnce
// failed CI at 15.12s while taking 3.5s locally, because the shared runner is
// slower under -race and these tests drive a real embedded JetStream server and
// a shell harness. A flake here reads as a product defect, which is worse than
// a slow failure.
const waitDeadline = 90 * time.Second

func waitState(t *testing.T, c *lib.Client, addressee, taskID string, state lib.TaskState) {
	t.Helper()
	deadline := time.Now().Add(waitDeadline)
	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		task, err := c.TasksGet(ctx, addressee, taskID)
		cancel()
		if err == nil && task.State == state {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("task %s never reached %s", taskID, state)
}

func foldTask(t *testing.T, c *lib.Client, addressee, taskID string) *lib.Task {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	task, err := c.TasksGet(ctx, addressee, taskID)
	if err != nil {
		t.Fatalf("tasks/get %s: %v", taskID, err)
	}
	return task
}

// replayEvents reads the raw event envelopes in stream order.
func replayEvents(t *testing.T, url, addressee, taskID string) []*lib.Envelope {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("replay connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("replay jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cons, err := js.OrderedConsumer(ctx, lib.TasksStream, jetstream.OrderedConsumerConfig{
		FilterSubjects: []string{lib.TaskEventsSubject(addressee, taskID)},
		DeliverPolicy:  jetstream.DeliverAllPolicy,
	})
	if err != nil {
		t.Fatalf("replay consumer: %v", err)
	}
	batch, err := cons.FetchNoWait(1000)
	if err != nil {
		t.Fatalf("replay fetch: %v", err)
	}
	var events []*lib.Envelope
	for msg := range batch.Messages() {
		env, err := lib.ParseEnvelope(msg.Data())
		if err != nil {
			t.Fatalf("replay parse: %v", err)
		}
		events = append(events, env)
	}
	return events
}

func statusOf(t *testing.T, env *lib.Envelope) lib.StatusUpdate {
	t.Helper()
	var s lib.StatusUpdate
	if err := json.Unmarshal(env.Payload, &s); err != nil {
		t.Fatalf("status payload: %v", err)
	}
	return s
}

func artifactText(task *lib.Task, name string) string {
	a := task.Artifact(name)
	if a == nil {
		return ""
	}
	var b strings.Builder
	for _, p := range a.Parts {
		b.WriteString(p.Text)
	}
	return b.String()
}

// TestLifecycle_HappyPath: assertions 9, 10, 14, 15, 18, plus the artifact
// mapping - thinking to thinking, tool_use to activity, prose to progress,
// deliverable to result.
func TestLifecycle_HappyPath(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-otter-a1b2", "task-happy-1"
	origin := submit(t, c, session, taskID, "write a haiku about message buses")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-1"}'
echo '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"pondering buses"}]}}'
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"drafting the haiku now"},{"type":"tool_use","name":"Write","input":{"file_path":"haiku.txt"}}]}}'
echo '{"type":"result","subtype":"success","result":"buses hum softly / envelopes drift downstream / an ack, then silence"}'
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}

	events := replayEvents(t, url, session, taskID)
	if len(events) == 0 {
		t.Fatal("no events")
	}
	// Assertion 9: the first event is status-update submitted.
	first := events[0]
	if first.Kind != lib.KindStatusUpdate {
		t.Fatalf("first event kind %q", first.Kind)
	}
	if s := statusOf(t, first); s.Status.State != lib.StateSubmitted || s.Final {
		t.Fatalf("first event %+v", s)
	}
	// Assertion 10: exactly one final, it is terminal, and it is last.
	finals := 0
	for i, env := range events {
		if env.Kind != lib.KindStatusUpdate {
			continue
		}
		if s := statusOf(t, env); s.Final {
			finals++
			if !s.Status.State.Terminal() {
				t.Fatalf("final event state %q not terminal", s.Status.State)
			}
			if i != len(events)-1 {
				t.Fatalf("final event at %d of %d", i, len(events))
			}
		}
	}
	if finals != 1 {
		t.Fatalf("finals = %d", finals)
	}
	// Assertions 14/15: every event carries the origin's identifiers
	// verbatim.
	for _, env := range events {
		if env.TaskID != origin.TaskID || env.ContextID != origin.ContextID ||
			env.CorrelationID != origin.CorrelationID {
			t.Fatalf("identifier drift on %s: %+v", env.Kind, env)
		}
	}
	// Assertion 18 and the artifact mapping.
	task := foldTask(t, c, session, taskID)
	if err := task.ValidateArtifacts(); err != nil {
		t.Fatalf("artifacts: %v", err)
	}
	if got := artifactText(task, lib.ArtifactResult); !strings.Contains(got, "buses hum softly") {
		t.Fatalf("result artifact %q", got)
	}
	if got := artifactText(task, lib.ArtifactProgress); !strings.Contains(got, "drafting the haiku") {
		t.Fatalf("progress artifact %q", got)
	}
	if got := artifactText(task, lib.ArtifactThinking); !strings.Contains(got, "pondering") {
		t.Fatalf("thinking artifact %q", got)
	}
	activity := task.Artifact(lib.ArtifactActivity)
	if activity == nil || len(activity.Parts) == 0 || !strings.Contains(string(activity.Parts[0].Data), "Write") {
		t.Fatalf("activity artifact %+v", activity)
	}
}

// TestAssertion21_SteerReachesStdinExactlyOnce: the adapter half of
// assertion 21, against a stub that echoes its stdin - a steer published
// while the task runs reaches the harness stdin exactly once even when the
// envelope is redelivered, and the follow-up implies no state change
// (assertion 12's second half).
func TestAssertion21_SteerReachesStdinExactlyOnce(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-lynx-c3d4", "task-steer-1"
	origin := submit(t, c, session, taskID, "opening prompt")

	// The stub reads its stdin (the opening prompt, then anything steered),
	// counts lines and STEERWORD occurrences, and answers one result per
	// turn so the adapter's turn accounting settles.
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-steer"}'
read first || exit 1
count=1
all="$first"
while read -t 3 line; do
  count=$((count+1))
  all="$all $line"
done
steers=$(printf '%s' "$all" | grep -o STEERWORD | wc -l | tr -d ' ')
i=1
while [ "$i" -lt "$count" ]; do
  echo '{"type":"result","subtype":"success","result":"interim turn"}'
  i=$((i+1))
done
printf '{"type":"result","subtype":"success","result":"turns=%s steers=%s"}\n' "$count" "$steers"
`)
	done := runAdapter(context.Background(), adapterConfig(url, taskID, session, harness))
	waitState(t, c, session, taskID, lib.StateWorking)

	// Build one steer envelope and publish it twice with distinct JetStream
	// message ids: a redelivery in the only place the library's dedup can't
	// see for us (the adapter's own raw consumer).
	steerPayload, err := json.Marshal(lib.Message{
		Role: "user", Parts: []lib.Part{{Kind: "text", Text: "STEERWORD make it about NATS"}},
		MessageID: "msg-steer-1", TaskID: taskID, ContextID: origin.ContextID,
	})
	if err != nil {
		t.Fatalf("steer payload: %v", err)
	}
	steer, err := lib.NewFollowUpEnvelope(origin, gatewayParty, steerPayload,
		lib.WithTo(lib.Party{Session: session}))
	if err != nil {
		t.Fatalf("steer envelope: %v", err)
	}
	raw, err := json.Marshal(steer)
	if err != nil {
		t.Fatalf("steer marshal: %v", err)
	}
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("steer connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("steer jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	subject := lib.TaskInSubject(session, taskID)
	for i := 0; i < 2; i++ {
		if _, err := js.Publish(ctx, subject, raw,
			jetstream.WithMsgID(fmt.Sprintf("dup-%d-%s", i, steer.EnvelopeID))); err != nil {
			t.Fatalf("steer publish %d: %v", i, err)
		}
	}

	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	task := foldTask(t, c, session, taskID)
	// Exactly once: the stub saw two stdin lines (prompt + one steer) and
	// exactly one STEERWORD, despite two publishes.
	if got := artifactText(task, lib.ArtifactResult); got != "turns=2 steers=1" {
		t.Fatalf("steer delivery: result artifact %q", got)
	}
	// Assertion 12: the follow-up did not by itself change task state - the
	// history is submitted, working, completed, nothing else.
	want := []lib.TaskState{lib.StateSubmitted, lib.StateWorking, lib.StateCompleted}
	if fmt.Sprint(task.StatusHistory) != fmt.Sprint(want) {
		t.Fatalf("status history %v", task.StatusHistory)
	}
	if task.PostFinalDropped != 0 {
		t.Fatalf("post-final events: %d", task.PostFinalDropped)
	}
}

// TestSteerAfterDeliverableIsRefusedVisibly: a steer that loses the race with
// the adapter's choice of deliverable cannot be absorbed, and the payload
// spec requires that refusal be on the stream rather than in a log line -
// nothing else marks this window, because the choice of deliverable is
// adapter-internal. The refusal is a non-final status-update carrying the
// task's current state, published before the terminal event.
func TestSteerAfterDeliverableIsRefusedVisibly(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibis-g7h8", "task-late-steer-1"
	const deliverable = "the pre-steer answer"
	origin := submit(t, c, session, taskID, "opening prompt")

	// The stub answers its one turn, touches a marker file so the test knows
	// the deliverable is out, then lingers - holding the supervise loop open
	// so the late steer lands while the adapter is still running.
	marker := filepath.Join(t.TempDir(), "deliverable-emitted")
	harness := stub(t, fmt.Sprintf(`
echo '{"type":"system","subtype":"init","session_id":"stub-late-steer"}'
read first || exit 1
printf '{"type":"result","subtype":"success","result":"%s"}\n' %q
: > %q
sleep 10
`, deliverable, deliverable, marker))
	done := runAdapter(context.Background(), adapterConfig(url, taskID, session, harness))
	waitState(t, c, session, taskID, lib.StateWorking)

	deadline := time.Now().Add(waitDeadline)
	for {
		if _, err := os.Stat(marker); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("harness never emitted its deliverable")
		}
		time.Sleep(20 * time.Millisecond)
	}
	// The marker says the result is on the harness's stdout; give the adapter
	// a beat to read it, so the steer below is genuinely late. If it were not
	// late the harness would take another turn and the assertions fail - this
	// test cannot pass by racing the wrong way.
	time.Sleep(500 * time.Millisecond)

	const steerText = "STEERWORD actually make it about NATS"
	steerPayload, err := json.Marshal(lib.Message{
		Role: "user", Parts: []lib.Part{{Kind: "text", Text: steerText}},
		MessageID: "msg-late-steer-1", TaskID: taskID, ContextID: origin.ContextID,
	})
	if err != nil {
		t.Fatalf("steer payload: %v", err)
	}
	steer, err := lib.NewFollowUpEnvelope(origin, gatewayParty, steerPayload,
		lib.WithTo(lib.Party{Session: session}))
	if err != nil {
		t.Fatalf("steer envelope: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := c.Publish(ctx, lib.TaskInSubject(session, taskID), steer); err != nil {
		t.Fatalf("steer publish: %v", err)
	}

	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}

	// The refusal is on the stream, before the terminal, and it is the only
	// one: one refused message, one refusal.
	events := replayEvents(t, url, session, taskID)
	refusals, refusalAt, terminalAt := 0, -1, -1
	for i, env := range events {
		if env.Kind != lib.KindStatusUpdate {
			continue
		}
		s := statusOf(t, env)
		if s.Final {
			terminalAt = i
			continue
		}
		if s.Status.Message == nil {
			continue
		}
		text := joinParts(s.Status.Message.Parts)
		if !strings.Contains(text, "steer refused") {
			continue
		}
		refusals++
		refusalAt = i
		if s.Status.State != lib.StateWorking {
			t.Errorf("refusal carries state %q, want the task's current state %q",
				s.Status.State, lib.StateWorking)
		}
		if !strings.Contains(text, steerText) {
			t.Errorf("refusal does not identify the refused message: %q", text)
		}
	}
	if refusals != 1 {
		t.Fatalf("refusal events on the stream: %d, want 1", refusals)
	}
	if terminalAt < 0 {
		t.Fatal("no terminal event")
	}
	if refusalAt > terminalAt {
		t.Errorf("refusal published after the terminal (refusal %d, terminal %d)", refusalAt, terminalAt)
	}

	// The refusal did not steal the turn: the deliverable is still the answer
	// the harness chose before the steer arrived.
	task := foldTask(t, c, session, taskID)
	if got := artifactText(task, lib.ArtifactResult); got != deliverable {
		t.Errorf("result artifact %q, want %q", got, deliverable)
	}
	// Assertion 12: no state CHANGE came from the follow-up. The refusal
	// re-states working, which is why it appears in the history twice.
	want := []lib.TaskState{lib.StateSubmitted, lib.StateWorking, lib.StateWorking, lib.StateCompleted}
	if fmt.Sprint(task.StatusHistory) != fmt.Sprint(want) {
		t.Errorf("status history %v, want %v", task.StatusHistory, want)
	}
	if task.PostFinalDropped != 0 {
		t.Errorf("post-final events: %d", task.PostFinalDropped)
	}
}

func joinParts(parts []lib.Part) string {
	var b strings.Builder
	for _, p := range parts {
		b.WriteString(p.Text)
	}
	return b.String()
}

// TestLifecycle_Cancel: assertion 13 - cancel produces terminal canceled and
// the harness process is actually dead.
func TestLifecycle_Cancel(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-wren-e5f6", "task-cancel-1"
	origin := submit(t, c, session, taskID, "run forever")

	pidFile := filepath.Join(t.TempDir(), "pid")
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-cancel"}'
echo "$$" > `+pidFile+`
exec sleep 60
`)
	done := runAdapter(context.Background(), adapterConfig(url, taskID, session, harness))
	waitState(t, c, session, taskID, lib.StateWorking)

	env, err := lib.NewCancelEnvelope(gatewayParty, taskID, origin.ContextID, origin.CorrelationID,
		lib.WithTo(lib.Party{Session: session}))
	if err != nil {
		t.Fatalf("cancel envelope: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := c.Publish(ctx, lib.TaskInSubject(session, taskID), env); err != nil {
		t.Fatalf("publish cancel: %v", err)
	}

	out := waitOutcome(t, done, 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCanceled {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	pidRaw, err := os.ReadFile(pidFile)
	if err != nil {
		t.Fatalf("pid file: %v", err)
	}
	var pid int
	fmt.Sscanf(strings.TrimSpace(string(pidRaw)), "%d", &pid)
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if syscall.Kill(pid, 0) != nil {
			return // dead, as required
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("harness pid %d still alive after cancel", pid)
}

// TestLifecycle_FailedWithEvidence: a harness that dies without a result
// yields terminal failed carrying the exit status and the stderr tail.
func TestLifecycle_FailedWithEvidence(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-tapir-a7b8", "task-fail-1"
	submit(t, c, session, taskID, "explode please")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-fail"}'
echo "stub exploded spectacularly" >&2
exit 3
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.res.State != lib.StateFailed {
		t.Fatalf("state %q", out.res.State)
	}
	task := foldTask(t, c, session, taskID)
	if task.State != lib.StateFailed || !task.Final {
		t.Fatalf("folded %+v", task)
	}
	events := replayEvents(t, url, session, taskID)
	last := statusOf(t, events[len(events)-1])
	text := last.Status.Message.Parts[0].Text
	if !strings.Contains(text, "exit status 3") || !strings.Contains(text, "stub exploded spectacularly") {
		t.Fatalf("failure evidence missing: %q", text)
	}
}

// TestLifecycle_HarnessErrorResult: a result with an error subtype maps to
// terminal failed with that subtype as the reason.
func TestLifecycle_HarnessErrorResult(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-vole-c9d0", "task-errres-1"
	submit(t, c, session, taskID, "hit the turn limit")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-err"}'
echo '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"ran out of turns"}'
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.res.State != lib.StateFailed {
		t.Fatalf("state %q", out.res.State)
	}
	events := replayEvents(t, url, session, taskID)
	last := statusOf(t, events[len(events)-1])
	if !last.Final || last.Status.State != lib.StateFailed {
		t.Fatalf("last event %+v", last)
	}
	if text := last.Status.Message.Parts[0].Text; !strings.Contains(text, "error_max_turns") {
		t.Fatalf("reason %q", text)
	}
}

// TestLifecycle_RejectedNoTextParts: a submission with no text parts is
// refused before any harness spawn - terminal rejected.
func TestLifecycle_RejectedNoTextParts(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-e1f2", "task-reject-1"
	submit(t, c, session, taskID, "") // data-only parts

	// A harness path that cannot exist proves nothing was spawned.
	out := waitOutcome(t, runAdapter(context.Background(),
		adapterConfig(url, taskID, session, []string{"/nonexistent/harness"})), 30*time.Second)
	if out.res.State != lib.StateRejected {
		t.Fatalf("state %q err %v", out.res.State, out.err)
	}
	task := foldTask(t, c, session, taskID)
	if task.State != lib.StateRejected || !task.Final {
		t.Fatalf("folded %+v", task)
	}
}

// TestAlreadyTerminal: a respawned pod finding its task terminal publishes
// nothing and exits cleanly (the dispatcher rule, worn by the executor).
func TestAlreadyTerminal(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-newt-a3b4", "task-done-1"
	origin := submit(t, c, session, taskID, "already handled")

	// A dead predecessor ran the whole lifecycle.
	prev, err := c.NewTaskExecution(origin, lib.Party{Session: session, AgentType: "claude-code"}, session)
	if err != nil {
		t.Fatalf("predecessor execution: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := prev.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatalf("predecessor submitted: %v", err)
	}
	if err := prev.PublishArtifact(ctx, lib.Artifact{
		ArtifactID: "artifact-" + taskID + "-result", Name: lib.ArtifactResult,
		Parts: []lib.Part{{Kind: "text", Text: "the old answer"}},
	}); err != nil {
		t.Fatalf("predecessor result: %v", err)
	}
	if err := prev.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatalf("predecessor terminal: %v", err)
	}
	before := len(replayEvents(t, url, session, taskID))

	out := waitOutcome(t, runAdapter(context.Background(),
		adapterConfig(url, taskID, session, []string{"/nonexistent/harness"})), 30*time.Second)
	if out.err != nil || out.res.State != "" {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	if after := len(replayEvents(t, url, session, taskID)); after != before {
		t.Fatalf("events grew %d -> %d", before, after)
	}
}

// TestEviction: context cancellation (the SIGTERM path) yields terminal
// failed reason worker-evicted and the evicted exit contract.
func TestEviction(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-mole-c5d6", "task-evict-1"
	submit(t, c, session, taskID, "long haul")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-evict"}'
exec sleep 60
`)
	ctx, cancel := context.WithCancel(context.Background())
	done := runAdapter(ctx, adapterConfig(url, taskID, session, harness))
	waitState(t, c, session, taskID, lib.StateWorking)
	cancel()

	out := waitOutcome(t, done, 30*time.Second)
	if !out.res.Evicted || out.res.State != lib.StateFailed {
		t.Fatalf("run: %+v err=%v", out.res, out.err)
	}
	events := replayEvents(t, url, session, taskID)
	last := statusOf(t, events[len(events)-1])
	if !last.Final || last.Status.State != lib.StateFailed ||
		!strings.Contains(last.Status.Message.Parts[0].Text, "worker-evicted") {
		t.Fatalf("terminal %+v", last)
	}
}
