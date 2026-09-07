package hermesbridge

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// ---- harness -------------------------------------------------------------

var testPort atomic.Int32

func init() { testPort.Store(24222) }

func startServer(t *testing.T) (*natsserver.Server, string) {
	t.Helper()
	opts := &natsserver.Options{
		Host:      "127.0.0.1",
		Port:      int(testPort.Add(1)),
		JetStream: true,
		StoreDir:  t.TempDir(),
		NoLog:     true,
		NoSigs:    true,
	}
	s, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("server not ready")
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
	ctx := testCtx(t)
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{
		Name:      lib.TasksStream,
		Subjects:  []string{"a2a.tasks.>"},
		Retention: jetstream.LimitsPolicy,
		MaxAge:    72 * time.Hour,
	}); err != nil {
		t.Fatalf("create TASKS: %v", err)
	}
	if _, err := js.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: "runtime-state"}); err != nil {
		t.Fatalf("create runtime-state: %v", err)
	}
	return s, url
}

func testCtx(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	t.Cleanup(cancel)
	return ctx
}

// script writes an executable stub standing in for the hermes CLI. The
// bridge appends the prompt as the final argument, so stubs see it in "$5"
// under the default arg shape ["-p", profile, "chat", "-q", prompt] - tests
// pass Command themselves, so stubs read "$1".
func script(t *testing.T, body string) []string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "hermes-stub")
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"+body+"\n"), 0o755); err != nil {
		t.Fatalf("write stub: %v", err)
	}
	return []string{path}
}

// startBridge runs a bridge until test cleanup and waits for its durable
// consumer, so a submission published right after cannot race the subscribe.
func startBridge(t *testing.T, url string, command []string) {
	t.Helper()
	startBridgeN(t, url, command, 0)
}

func startBridgeN(t *testing.T, url string, command []string, concurrency int) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	b, err := New(ctx, Config{
		NATSURL:      url,
		Command:      command,
		Concurrency:  concurrency,
		TaskDeadline: 20 * time.Second,
		KillGrace:    500 * time.Millisecond,
	})
	if err != nil {
		cancel()
		t.Fatalf("bridge new: %v", err)
	}
	done := make(chan error, 1)
	go func() { done <- b.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-done:
			if err != nil {
				t.Logf("bridge exited: %v", err)
			}
		case <-time.After(10 * time.Second):
			t.Error("bridge did not shut down")
		}
	})
	waitFor(t, 10*time.Second, "bridge durable consumer", func() bool {
		nc, err := nats.Connect(url)
		if err != nil {
			return false
		}
		defer nc.Close()
		js, err := jetstream.New(nc)
		if err != nil {
			return false
		}
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_, err = js.Consumer(ctx, lib.TasksStream, "bridge-platform")
		return err == nil
	})
}

func gatewayClient(t *testing.T, url string) *lib.Client {
	t.Helper()
	c, err := lib.Connect(testCtx(t), url, lib.WithName("test-gateway"))
	if err != nil {
		t.Fatalf("gateway connect: %v", err)
	}
	t.Cleanup(c.Close)
	return c
}

var gatewayParty = lib.Party{Session: "chatops", AgentType: "gateway"}

func messagePayload(t *testing.T, taskID, contextID, text string) json.RawMessage {
	t.Helper()
	payload, err := json.Marshal(lib.Message{
		Role:      "user",
		MessageID: "msg-" + nuid.Next(),
		Parts:     []lib.Part{{Kind: "text", Text: text}},
		TaskID:    taskID,
		ContextID: contextID,
	})
	if err != nil {
		t.Fatal(err)
	}
	return payload
}

// submit publishes a task submission addressed to platform and returns its
// envelope (the ids ride on it).
func submit(t *testing.T, c *lib.Client, taskID, prompt string) *lib.Envelope {
	t.Helper()
	contextID := "ctx-" + taskID
	corrID := "corr-" + taskID
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, contextID, corrID,
		messagePayload(t, taskID, contextID, prompt), lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatalf("submission envelope: %v", err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", taskID), env); err != nil {
		t.Fatalf("submission publish: %v", err)
	}
	return env
}

// replayEvents reads the task's events subject in stream order.
func replayEvents(t *testing.T, url, taskID string) []*lib.Envelope {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("replay connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx := testCtx(t)
	cons, err := js.OrderedConsumer(ctx, lib.TasksStream, jetstream.OrderedConsumerConfig{
		FilterSubjects: []string{lib.TaskEventsSubject("platform", taskID)},
		DeliverPolicy:  jetstream.DeliverAllPolicy,
	})
	if err != nil {
		t.Fatal(err)
	}
	// One bounded fetch: repeated fetches on an ordered consumer restart
	// from sequence 1, and a task's event count is far below the batch.
	var events []*lib.Envelope
	msgs, err := cons.FetchNoWait(1000)
	if err != nil {
		t.Fatal(err)
	}
	for msg := range msgs.Messages() {
		env, err := lib.ParseEnvelope(msg.Data())
		if err != nil {
			t.Fatalf("unparseable event on the stream: %v", err)
		}
		events = append(events, env)
	}
	return events
}

func fold(t *testing.T, c *lib.Client, taskID string) *lib.Task {
	t.Helper()
	task, err := c.TasksGet(testCtx(t), "platform", taskID)
	if err != nil {
		t.Fatalf("tasks/get %s: %v", taskID, err)
	}
	return task
}

func waitTerminal(t *testing.T, c *lib.Client, taskID string) *lib.Task {
	t.Helper()
	var task *lib.Task
	waitFor(t, 20*time.Second, "terminal event on "+taskID, func() bool {
		got, err := c.TasksGet(testCtx(t), "platform", taskID)
		if err != nil {
			return false
		}
		task = got
		return task.Final
	})
	return task
}

func waitFor(t *testing.T, d time.Duration, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func statusState(t *testing.T, env *lib.Envelope) (lib.TaskState, bool) {
	t.Helper()
	if env.Kind != lib.KindStatusUpdate {
		t.Fatalf("expected status-update, got %s", env.Kind)
	}
	var s lib.StatusUpdate
	if err := json.Unmarshal(env.Payload, &s); err != nil {
		t.Fatal(err)
	}
	return s.Status.State, s.Final
}

// ---- lifecycle conformance ------------------------------------------------

// Assertions 9, 10, 14, 15, 18: the happy path produces submitted, working,
// a result artifact carrying the subprocess output, and exactly one final
// terminal event, all on the originating message's ids.
func TestLifecycle_HappyPath(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, script(t, `echo "the platform answer for $1"`))
	c := gatewayClient(t, url)

	origin := submit(t, c, "task-happy", "what is the fleet status")
	task := waitTerminal(t, c, "task-happy")

	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	if err := task.ValidateArtifacts(); err != nil {
		t.Fatalf("assertion 18: %v", err)
	}
	result := task.Artifact(lib.ArtifactResult)
	if result == nil || len(result.Parts) == 0 {
		t.Fatal("assertion 18: no result artifact")
	}
	if got := result.Parts[0].Text; !strings.Contains(got, "the platform answer for what is the fleet status") {
		t.Fatalf("result = %q, want the stub's stdout", got)
	}

	events := replayEvents(t, url, "task-happy")
	if len(events) < 3 {
		t.Fatalf("want >=3 events, got %d", len(events))
	}
	// Assertion 9: first event is submitted.
	if state, final := statusState(t, events[0]); state != lib.StateSubmitted || final {
		t.Fatalf("assertion 9: first event %s final=%v, want non-final submitted", state, final)
	}
	// Assertion 10: exactly one final, terminal, and it is the last event.
	finals := 0
	for _, env := range events {
		if env.Kind != lib.KindStatusUpdate {
			continue
		}
		var s lib.StatusUpdate
		if err := json.Unmarshal(env.Payload, &s); err != nil {
			t.Fatal(err)
		}
		if s.Final {
			finals++
			if !s.Status.State.Terminal() {
				t.Fatalf("assertion 10: final event has non-terminal state %s", s.Status.State)
			}
		}
	}
	if finals != 1 {
		t.Fatalf("assertion 10: %d final events, want exactly 1", finals)
	}
	if state, final := statusState(t, events[len(events)-1]); !final {
		t.Fatalf("assertion 10: last event %s is not the final one", state)
	}
	// Assertions 14, 15: every event carries the originating ids verbatim.
	for i, env := range events {
		if env.TaskID != origin.TaskID || env.ContextID != origin.ContextID || env.CorrelationID != origin.CorrelationID {
			t.Fatalf("assertion 14/15: event %d ids = (%s,%s,%s), want origin's (%s,%s,%s)",
				i, env.TaskID, env.ContextID, env.CorrelationID,
				origin.TaskID, origin.ContextID, origin.CorrelationID)
		}
	}
}

// Assertion 12 (steering half): a follow-up during working is answered with
// a non-final status and does not by itself change task state - the task
// still completes on its own.
func TestLifecycle_SteerRefusedHonestly(t *testing.T) {
	_, url := startServer(t)
	marker := filepath.Join(t.TempDir(), "started")
	startBridge(t, url, script(t, fmt.Sprintf(`touch %s
sleep 2
echo done-after-steer`, marker)))
	c := gatewayClient(t, url)

	origin := submit(t, c, "task-steer", "long question")
	waitFor(t, 10*time.Second, "subprocess start", func() bool {
		_, err := os.Stat(marker)
		return err == nil
	})

	steer, err := lib.NewFollowUpEnvelope(origin, gatewayParty,
		messagePayload(t, origin.TaskID, origin.ContextID, "also check the east region"),
		lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", origin.TaskID), steer); err != nil {
		t.Fatal(err)
	}

	// The refusal arrives as a non-final working status with a message.
	waitFor(t, 10*time.Second, "steer refusal status", func() bool {
		for _, env := range replayEvents(t, url, origin.TaskID) {
			if env.Kind != lib.KindStatusUpdate {
				continue
			}
			var s lib.StatusUpdate
			if json.Unmarshal(env.Payload, &s) != nil {
				continue
			}
			if !s.Final && s.Status.State == lib.StateWorking && s.Status.Message != nil &&
				strings.Contains(s.Status.Message.Parts[0].Text, "cannot accept mid-run input") {
				return true
			}
		}
		return false
	})

	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted {
		t.Fatalf("state after steer = %s, want completed - a steer must not change state", task.State)
	}
	if task.PostFinalDropped != 0 {
		t.Fatalf("assertion 10: %d events after final", task.PostFinalDropped)
	}
}

// Assertion 13: cancel produces terminal canceled, never a silent stop, and
// the subprocess actually dies.
func TestLifecycle_Cancel(t *testing.T) {
	_, url := startServer(t)
	pidfile := filepath.Join(t.TempDir(), "pid")
	startBridge(t, url, script(t, fmt.Sprintf(`echo $$ > %s
sleep 60
echo never`, pidfile)))
	c := gatewayClient(t, url)

	origin := submit(t, c, "task-cancel", "run forever")
	waitFor(t, 10*time.Second, "subprocess start", func() bool {
		_, err := os.Stat(pidfile)
		return err == nil
	})

	cancelEnv, err := lib.NewCancelEnvelope(gatewayParty, origin.TaskID, origin.ContextID, origin.CorrelationID,
		lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", origin.TaskID), cancelEnv); err != nil {
		t.Fatal(err)
	}

	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCanceled {
		t.Fatalf("state = %s, want canceled", task.State)
	}
	raw, err := os.ReadFile(pidfile)
	if err != nil {
		t.Fatal(err)
	}
	pid := strings.TrimSpace(string(raw))
	waitFor(t, 5*time.Second, "subprocess death", func() bool {
		return !processAlive(pid)
	})
}

func processAlive(pidStr string) bool {
	pid, err := strconv.Atoi(pidStr)
	if err != nil {
		return false
	}
	return syscall.Kill(pid, 0) == nil
}

// A nonzero hermes exit is terminal failed carrying the evidence.
func TestLifecycle_FailedWithEvidence(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, script(t, `echo "boom: config missing" >&2
exit 3`))
	c := gatewayClient(t, url)

	submit(t, c, "task-fail", "doomed")
	task := waitTerminal(t, c, "task-fail")
	if task.State != lib.StateFailed {
		t.Fatalf("state = %s, want failed", task.State)
	}
	events := replayEvents(t, url, "task-fail")
	last := events[len(events)-1]
	var s lib.StatusUpdate
	if err := json.Unmarshal(last.Payload, &s); err != nil {
		t.Fatal(err)
	}
	if s.Status.Message == nil || !strings.Contains(s.Status.Message.Parts[0].Text, "boom: config missing") {
		t.Fatal("failed event does not carry the stderr evidence")
	}
	if !strings.Contains(s.Status.Message.Parts[0].Text, "exit status 3") {
		t.Fatal("failed event does not carry the exit code")
	}
}

// A submission with no text parts is terminal rejected - an executor
// refusing work before starting it.
func TestLifecycle_RejectedNoTextParts(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, script(t, `echo unreachable`))
	c := gatewayClient(t, url)

	taskID := "task-reject"
	contextID := "ctx-" + taskID
	payload, err := json.Marshal(lib.Message{
		Role:      "user",
		MessageID: "msg-" + nuid.Next(),
		Parts:     []lib.Part{{Kind: "data", Data: json.RawMessage(`{"structured":"only"}`)}},
		TaskID:    taskID,
		ContextID: contextID,
	})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, contextID, "corr-"+taskID, payload,
		lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", taskID), env); err != nil {
		t.Fatal(err)
	}
	task := waitTerminal(t, c, taskID)
	if task.State != lib.StateRejected {
		t.Fatalf("state = %s, want rejected", task.State)
	}
}

// In traffic for a task with a terminal event is acked with a warning and
// produces no events (the dispatcher rule; assertion 10's post-final ban).
func TestLifecycle_TerminalTaskTrafficIgnored(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, script(t, `echo quick`))
	c := gatewayClient(t, url)

	origin := submit(t, c, "task-done", "quick one")
	waitTerminal(t, c, "task-done")
	before := len(replayEvents(t, url, "task-done"))

	follow, err := lib.NewFollowUpEnvelope(origin, gatewayParty,
		messagePayload(t, origin.TaskID, origin.ContextID, "one more thing"),
		lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", origin.TaskID), follow); err != nil {
		t.Fatal(err)
	}
	time.Sleep(1 * time.Second)
	if after := len(replayEvents(t, url, "task-done")); after != before {
		t.Fatalf("post-terminal follow-up grew events %d -> %d", before, after)
	}
}

// Assertion 12 for a QUEUED task: a steer arriving while the run waits for
// a worker slot answers with the task's actual state (submitted), not
// working - a follow-up must not change folded state by itself.
func TestLifecycle_SteerToQueuedTaskAnswersSubmitted(t *testing.T) {
	_, url := startServer(t)
	marker := filepath.Join(t.TempDir(), "started")
	startBridgeN(t, url, script(t, fmt.Sprintf(`touch %s.$1
sleep 3
echo done`, marker)), 1)
	c := gatewayClient(t, url)

	// First task occupies the single worker slot; second stays queued.
	submit(t, c, "task-slot", "hold-the-slot")
	waitFor(t, 10*time.Second, "first subprocess start", func() bool {
		_, err := os.Stat(marker + ".hold-the-slot")
		return err == nil
	})
	queued := submit(t, c, "task-queued", "wait-your-turn")
	waitFor(t, 10*time.Second, "queued task submitted", func() bool {
		task, err := c.TasksGet(testCtx(t), "platform", "task-queued")
		return err == nil && task.State == lib.StateSubmitted
	})

	steer, err := lib.NewFollowUpEnvelope(queued, gatewayParty,
		messagePayload(t, queued.TaskID, queued.ContextID, "hurry up"),
		lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", queued.TaskID), steer); err != nil {
		t.Fatal(err)
	}

	waitFor(t, 10*time.Second, "steer refusal on queued task", func() bool {
		for _, env := range replayEvents(t, url, queued.TaskID) {
			if env.Kind != lib.KindStatusUpdate {
				continue
			}
			var s lib.StatusUpdate
			if json.Unmarshal(env.Payload, &s) != nil {
				continue
			}
			if s.Status.Message != nil && strings.Contains(s.Status.Message.Parts[0].Text, "cannot accept mid-run input") {
				if s.Status.State != lib.StateSubmitted {
					t.Fatalf("refusal state = %s, want submitted for a queued task", s.Status.State)
				}
				return true
			}
		}
		return false
	})
	task := fold(t, c, queued.TaskID)
	if task.State != lib.StateSubmitted {
		t.Fatalf("folded state after steer = %s, want still submitted", task.State)
	}
	// Both tasks still finish clean.
	if got := waitTerminal(t, c, "task-queued"); got.State != lib.StateCompleted {
		t.Fatalf("queued task ended %s, want completed", got.State)
	}
}

// ---- supervision ------------------------------------------------------------

// A task a prior incarnation accepted and never finished gets terminal
// failed from the startup sweep.
func TestSweep_OrphanFinalized(t *testing.T) {
	_, url := startServer(t)
	c := gatewayClient(t, url)

	// Fabricate the prior incarnation: submission, submitted+working events,
	// in-flight KV key, no terminal.
	taskID := "task-orphan"
	origin := submit(t, c, taskID, "died midway")
	bridgeParty := lib.Party{Session: "platform-bridge", AgentType: "hermes-bridge", Profile: "platform"}
	x, err := c.NewTaskExecution(origin, bridgeParty, "platform")
	if err != nil {
		t.Fatal(err)
	}
	if err := x.PublishStatus(testCtx(t), lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := x.PublishStatus(testCtx(t), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	kv, err := js.KeyValue(testCtx(t), "runtime-state")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := kv.Put(testCtx(t), "bridge.platform."+taskID, []byte("platform-bridge")); err != nil {
		t.Fatal(err)
	}
	// Ack the submission the way the dead bridge would have: create its
	// durable and consume the pending message, so the new bridge is not
	// simply re-running the task instead of sweeping it.
	cons, err := js.CreateOrUpdateConsumer(testCtx(t), lib.TasksStream, jetstream.ConsumerConfig{
		Durable:       "bridge-platform",
		FilterSubject: "a2a.tasks.platform.*.in",
		AckPolicy:     jetstream.AckExplicitPolicy,
	})
	if err != nil {
		t.Fatal(err)
	}
	msgs, err := cons.FetchNoWait(10)
	if err != nil {
		t.Fatal(err)
	}
	for msg := range msgs.Messages() {
		_ = msg.Ack()
	}

	startBridge(t, url, script(t, `echo unreachable`))

	task := waitTerminal(t, c, taskID)
	if task.State != lib.StateFailed {
		t.Fatalf("state = %s, want failed", task.State)
	}
	events := replayEvents(t, url, taskID)
	var s lib.StatusUpdate
	if err := json.Unmarshal(events[len(events)-1].Payload, &s); err != nil {
		t.Fatal(err)
	}
	if s.Status.Message == nil || !strings.Contains(s.Status.Message.Parts[0].Text, "bridge-died-without-terminal-event") {
		t.Fatal("sweep terminal does not name its evidence")
	}
	waitFor(t, 5*time.Second, "kv key cleared", func() bool {
		_, err := kv.Get(testCtx(t), "bridge.platform."+taskID)
		return err != nil
	})
}

// The sweep must not double-finalize: a task whose terminal event landed
// (the flush won the race) is left alone.
func TestSweep_TerminalTaskLeftAlone(t *testing.T) {
	_, url := startServer(t)
	c := gatewayClient(t, url)

	taskID := "task-flushed"
	origin := submit(t, c, taskID, "flushed on the way down")
	bridgeParty := lib.Party{Session: "platform-bridge", AgentType: "hermes-bridge", Profile: "platform"}
	x, err := c.NewTaskExecution(origin, bridgeParty, "platform")
	if err != nil {
		t.Fatal(err)
	}
	for _, step := range []struct {
		state lib.TaskState
		final bool
	}{{lib.StateSubmitted, false}, {lib.StateWorking, false}, {lib.StateFailed, true}} {
		if err := x.PublishStatus(testCtx(t), step.state, step.final); err != nil {
			t.Fatal(err)
		}
	}
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	kv, err := js.KeyValue(testCtx(t), "runtime-state")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := kv.Put(testCtx(t), "bridge.platform."+taskID, []byte("platform-bridge")); err != nil {
		t.Fatal(err)
	}
	before := len(replayEvents(t, url, taskID))

	startBridge(t, url, script(t, `echo unreachable`))
	waitFor(t, 5*time.Second, "kv key cleared", func() bool {
		_, err := kv.Get(testCtx(t), "bridge.platform."+taskID)
		return err != nil
	})
	if after := len(replayEvents(t, url, taskID)); after != before {
		t.Fatalf("sweep double-finalized: events %d -> %d", before, after)
	}
	task := fold(t, c, taskID)
	if task.PostFinalDropped != 0 {
		t.Fatalf("assertion 10: %d events after final", task.PostFinalDropped)
	}
}

// ---- chunking ------------------------------------------------------------

// chunkString must never cut inside a UTF-8 rune: each chunk is JSON-
// marshaled independently, and encoding/json replaces invalid UTF-8 with
// U+FFFD, so a byte-boundary split corrupts the reassembled artifact.
func TestChunkString_NeverSplitsARune(t *testing.T) {
	cases := []struct {
		name string
		in   string
		size int
	}{
		{"empty is one empty chunk (assertion 18)", "", 4},
		{"ascii exact multiple", "abcdefgh", 4},
		{"multibyte straddles the boundary", "ab日c", 3},
		{"multibyte everywhere", strings.Repeat("日本語", 100), 7},
		{"size smaller than one rune", "日", 1},
		{"emoji at every boundary", strings.Repeat("🙂", 50), 5},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			chunks := chunkString(tc.in, tc.size)
			if tc.in == "" && (len(chunks) != 1 || chunks[0] != "") {
				t.Fatalf("empty input: got %q", chunks)
			}
			var rebuilt strings.Builder
			for i, chunk := range chunks {
				// The JSON round trip is the corruption mechanism the
				// publish path applies to each chunk on its own.
				raw, err := json.Marshal(chunk)
				if err != nil {
					t.Fatal(err)
				}
				var back string
				if err := json.Unmarshal(raw, &back); err != nil {
					t.Fatal(err)
				}
				if back != chunk {
					t.Fatalf("chunk %d corrupted by JSON round trip: %q -> %q", i, chunk, back)
				}
				rebuilt.WriteString(back)
			}
			if rebuilt.String() != tc.in {
				t.Fatalf("reassembly mismatch: got %q want %q", rebuilt.String(), tc.in)
			}
		})
	}
}
