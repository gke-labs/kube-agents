package hermesbridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
	"unicode/utf8"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// ---- harness -------------------------------------------------------------

var testPort atomic.Int32

func init() { testPort.Store(24222) }

func startServer(t *testing.T) (*natsserver.Server, string) {
	t.Helper()
	return startServerOpt(t, true)
}

// startServerNoVerifier is the outage: everything provisioned, nothing
// answering on the verify subject.
func startServerNoVerifier(t *testing.T) (*natsserver.Server, string) {
	t.Helper()
	return startServerOpt(t, false)
}

func startServerOpt(t *testing.T, withVerifier bool) (*natsserver.Server, string) {
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
	// `nats kv add cap`, history 1, the way the operator's provision Job
	// makes it. Every lifecycle test in this file now runs through the
	// armed capability path, because that is how the sidecar runs in a
	// pod: submit() mints a real root naming `platform` as its delegate
	// and a real verifier answers. A suite that set CapabilityOptional to
	// keep the old tests passing would have proved the harness works and
	// nothing about the control.
	if _, err := js.CreateKeyValue(ctx, jetstream.KeyValueConfig{
		Bucket: capability.Bucket, History: 1,
	}); err != nil {
		t.Fatalf("create cap: %v", err)
	}
	if withVerifier {
		startVerifier(t, url)
	}
	return s, url
}

// startVerifier runs the real verifier service against the test server - the
// same Service the verifier Deployment runs, resolving out of the same bucket.
// Not a stub: a stub would answer "allowed" for chains that do not exist.
func startVerifier(t *testing.T, url string) {
	t.Helper()
	nc, err := nats.Connect(url, nats.Name("cap-verifier-test"))
	if err != nil {
		t.Fatalf("verifier connect: %v", err)
	}
	t.Cleanup(nc.Close)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("verifier jetstream: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	store, err := capability.NewStore(ctx, js)
	if err != nil {
		t.Fatalf("verifier store: %v", err)
	}
	svc := &capability.Service{Resolver: &capability.Resolver{Store: store}, Log: slog.Default()}
	sub, err := svc.Subscribe(ctx, nc)
	if err != nil {
		t.Fatalf("verifier subscribe: %v", err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
}

// mintFor writes the root capability the gateway would have minted for this
// task, naming the bridge's addressee as its delegate, and returns the
// reference.
func mintFor(t *testing.T, c *lib.Client, taskID, delegate string) capability.Ref {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	ref, err := capability.NewMinter(c.JetStream()).Mint(ctx, taskID, capability.Entry{
		Tier:     capability.TierDeveloperTeam,
		Scope:    capability.NamespaceScope(""),
		Delegate: delegate,
	})
	if err != nil {
		t.Fatalf("mint %s for %s: %v", taskID, delegate, err)
	}
	return ref
}

// authorityFor renders the authority block the gateway puts on a submission.
// Only `grants` matters to the executor; the advisory halves are omitted
// because nothing in this package reads them.
func authorityFor(t *testing.T, ref capability.Ref) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(map[string]any{
		"grants": map[string]any{"capability": ref},
	})
	if err != nil {
		t.Fatalf("authority: %v", err)
	}
	return raw
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

// startBridgeCancelable hands the shutdown to the caller, for the tests whose
// subject is what happens to a task in flight when the bridge goes away.
// Cleanup still cancels, so cancelling twice is expected and harmless.
func startBridgeCancelable(t *testing.T, url string, command []string) context.CancelFunc {
	t.Helper()
	return startBridgeCap(t, url, command, 0, false, nil)
}

// startBridgeOptional runs a bridge with A2A_CAPABILITY_REQUIRED=false's
// effect: the mixed-version rollout window, and the only configuration in
// which an uncapabled submission runs.
func startBridgeOptional(t *testing.T, url string, command []string) {
	t.Helper()
	startBridgeCap(t, url, command, 0, true, nil)
}

func startBridgeN(t *testing.T, url string, command []string, concurrency int) {
	t.Helper()
	startBridgeWith(t, url, command, concurrency, nil)
}

// startBridgeWith is startBridgeN with a hook that sees the bridge between
// New and Run, for a test that swaps the look-ahead seam, and returns the
// bridge's shutdown for a test that ends it early. The capability control
// stays armed: a seam test is not a reason to run the bridge unguarded.
func startBridgeWith(t *testing.T, url string, command []string, concurrency int, mutate func(*Bridge)) context.CancelFunc {
	t.Helper()
	return startBridgeCap(t, url, command, concurrency, false, mutate)
}

// startBridgeCap is the one that builds the bridge with the capability
// control set explicitly. Both axes the suite
// needs are here: `optional` is the mixed-version window, and `mutate` is
// the between-New-and-Run hook.
func startBridgeCap(t *testing.T, url string, command []string, concurrency int, optional bool, mutate func(*Bridge)) context.CancelFunc {
	t.Helper()
	_, cancel := startBridgeConfig(t, Config{
		NATSURL:      url,
		Command:      command,
		Concurrency:  concurrency,
		TaskDeadline: 20 * time.Second,
		KillGrace:    500 * time.Millisecond,
		// The scope the capability is checked at, matching what mintFor
		// writes. Every caller but startBridgeOptional leaves
		// CapabilityOptional at its zero value: the lifecycle tests run
		// with the control armed, because that is how the sidecar runs.
		Scope:              capability.NamespaceScope(""),
		CapabilityOptional: optional,
	}, mutate)
	return cancel
}

// startBridgeConfig runs a bridge from the caller's Config until test cleanup
// and waits for its durable consumer, so a submission published right after
// cannot race the subscribe. mutate, when set, sees the bridge between New
// and Run.
func startBridgeConfig(t *testing.T, cfg Config, mutate func(*Bridge)) (*Bridge, context.CancelFunc) {
	t.Helper()
	// A scratch dir of the test's own: the default is the host's shared
	// $TMPDIR/hermes-bridge, which the start-time sweep would clear under
	// any other bridge on the machine.
	if cfg.ScratchDir == "" {
		cfg.ScratchDir = t.TempDir()
	}
	ctx, cancel := context.WithCancel(context.Background())
	b, err := New(ctx, cfg)
	if err != nil {
		cancel()
		t.Fatalf("bridge new: %v", err)
	}
	if mutate != nil {
		mutate(b)
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
		nc, err := nats.Connect(cfg.NATSURL)
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
	return b, cancel
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
		messagePayload(t, taskID, contextID, prompt),
		lib.WithTo(lib.Party{Session: "platform"}),
		lib.WithAuthority(authorityFor(t, mintFor(t, c, taskID, "platform"))))
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

// ---- follow-ups (steers) ----------------------------------------------------

// steerSeen is one steer notice on a task's events, with the status it rode.
type steerSeen struct {
	lib.SteerNotice
	state lib.TaskState
	final bool
	seq   int    // position among the task's events
	text  string // the notice's text part, for a reader without the data part
}

func steerNotices(t *testing.T, url, taskID string) []steerSeen {
	t.Helper()
	var out []steerSeen
	for i, env := range replayEvents(t, url, taskID) {
		if env.Kind != lib.KindStatusUpdate {
			continue
		}
		var s lib.StatusUpdate
		if json.Unmarshal(env.Payload, &s) != nil || s.Status.Message == nil {
			continue
		}
		if n, ok := lib.SteerNoticeOf(s.Status.Message.Parts); ok {
			seen := steerSeen{SteerNotice: n, state: s.Status.State, final: s.Final, seq: i}
			for _, p := range s.Status.Message.Parts {
				if p.Kind == "text" {
					seen.text = p.Text
				}
			}
			out = append(out, seen)
		}
	}
	return out
}

// finalSeq is the position of the task's final status among its events.
func finalSeq(t *testing.T, url, taskID string) int {
	t.Helper()
	for i, env := range replayEvents(t, url, taskID) {
		if lib.IsFinalStatus(env) {
			return i
		}
	}
	t.Fatalf("no final status on %s", taskID)
	return -1
}

func sendSteer(t *testing.T, c *lib.Client, origin *lib.Envelope, text string) *lib.Envelope {
	t.Helper()
	steer, err := lib.NewFollowUpEnvelope(origin, gatewayParty,
		messagePayload(t, origin.TaskID, origin.ContextID, text),
		lib.WithTo(lib.Party{Session: "platform"}), lib.WithAuthority(origin.Authority))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", origin.TaskID), steer); err != nil {
		t.Fatal(err)
	}
	return steer
}

// blockingStub is a hermes stand-in whose first turn holds until release
// exists, so follow-ups arrive while the task is running.
func blockingStub(t *testing.T) (cmd []string, started, release string) {
	t.Helper()
	dir := t.TempDir()
	started, release = filepath.Join(dir, "started"), filepath.Join(dir, "release")
	return script(t, fmt.Sprintf(`touch %s
while [ ! -f %s ]; do sleep 0.05; done
echo first-answer
echo "session_id: sess-1" >&2`, started, release)), started, release
}

func waitStarted(t *testing.T, path string) {
	t.Helper()
	waitFor(t, 10*time.Second, "subprocess start", func() bool { _, err := os.Stat(path); return err == nil })
}

// runOf is the bridge's live run for taskID, nil once finalized.
func runOf(b *Bridge, taskID string) *taskRun {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.tasks[taskID]
}

// A follow-up during working is queued, said so on the stream as a
// non-final working status, and changes no state by itself (assertion 12).
// Cancelled with follow-ups queued, each is refused task-ended BEFORE the
// terminal (assertion 10), in words that say the task ended.
func TestSteer_QueuedThenRefusedAtCancel(t *testing.T) {
	_, url := startServer(t)
	cmd, started, _ := blockingStub(t)
	startBridge(t, url, cmd)
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-steer-q", "long question")
	waitStarted(t, started)
	s1 := sendSteer(t, c, origin, "also check the east region")
	s2 := sendSteer(t, c, origin, "and the west")
	waitFor(t, 10*time.Second, "two queued notices", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 2 })
	for i, n := range steerNotices(t, url, origin.TaskID) {
		want := []string{s1.EnvelopeID, s2.EnvelopeID}[i]
		if n.Steer != lib.SteerQueued || n.EnvelopeID != want || n.state != lib.StateWorking || n.final {
			t.Fatalf("notice %d = %+v, want queued %s on a non-final working status", i, n, want)
		}
	}
	if task := fold(t, c, origin.TaskID); task.State != lib.StateWorking {
		t.Fatalf("folded state after two follow-ups = %s, want still working", task.State)
	}
	publishCancel(t, c, origin)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCanceled || task.PostFinalDropped != 0 {
		t.Fatalf("state %s, post-final %d", task.State, task.PostFinalDropped)
	}
	final := finalSeq(t, url, origin.TaskID)
	var refused []string
	for _, n := range steerNotices(t, url, origin.TaskID) {
		if n.Steer != lib.SteerRefused {
			continue
		}
		if n.Reason != lib.SteerReasonTaskEnded || n.final || n.seq > final {
			t.Fatalf("refusal %+v (final at %d), want task-ended, non-final, before the terminal", n, final)
		}
		if !strings.Contains(n.text, "ended") || strings.Contains(n.text, "continues") {
			t.Fatalf("task-ended refusal text %q does not say the task ended", n.text)
		}
		refused = append(refused, n.EnvelopeID)
	}
	if len(refused) != 2 || refused[0] != s1.EnvelopeID || refused[1] != s2.EnvelopeID {
		t.Fatalf("refusals at cancel = %v, want [%s %s] in queue order", refused, s1.EnvelopeID, s2.EnvelopeID)
	}
}

func TestSteer_SeventeenthIsRefused(t *testing.T) {
	_, url := startServer(t)
	cmd, started, _ := blockingStub(t)
	startBridge(t, url, cmd)
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-steer-17", "long question")
	waitStarted(t, started)
	var last *lib.Envelope
	for i := 0; i < steerQueueCapacity+1; i++ {
		last = sendSteer(t, c, origin, fmt.Sprintf("follow-up %d", i+1))
	}
	waitFor(t, 15*time.Second, "17 notices", func() bool { return len(steerNotices(t, url, origin.TaskID)) == steerQueueCapacity+1 })
	ns := steerNotices(t, url, origin.TaskID)
	for _, n := range ns[:steerQueueCapacity] {
		if n.Steer != lib.SteerQueued {
			t.Fatalf("within the bound: %+v", n)
		}
	}
	if n := ns[steerQueueCapacity]; n.Steer != lib.SteerRefused || n.Reason != lib.SteerReasonQueueFull || n.EnvelopeID != last.EnvelopeID {
		t.Fatalf("17th = %+v, want refused queue-full for %s", n, last.EnvelopeID)
	}
	publishCancel(t, c, origin)
	waitTerminal(t, c, origin.TaskID)
}

// A redelivered follow-up (same envelope id) is answered once; the task's own
// submission, redelivered while it runs, is never queued as its follow-up; a
// follow-up with no text is refused no-text.
func TestSteer_DuplicateAndSubmissionAreNotQueued(t *testing.T) {
	_, url := startServer(t)
	cmd, started, _ := blockingStub(t)
	b, _ := startBridgeConfig(t, Config{NATSURL: url, Command: cmd, KillGrace: 500 * time.Millisecond,
		Scope: capability.NamespaceScope("")}, nil)
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-steer-dup", "long question")
	waitStarted(t, started)
	steer := sendSteer(t, c, origin, "once only")
	waitFor(t, 10*time.Second, "queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	b.deliver(testCtx(t), steer)  // the durable redelivering it
	b.deliver(testCtx(t), origin) // the submission redelivered while running
	empty, err := lib.NewFollowUpEnvelope(origin, gatewayParty,
		json.RawMessage(`{"role":"user","messageId":"m-empty","parts":[{"kind":"data","data":{"x":1}}]}`),
		lib.WithTo(lib.Party{Session: "platform"}), lib.WithAuthority(origin.Authority))
	if err != nil {
		t.Fatal(err)
	}
	b.deliver(testCtx(t), empty)
	waitFor(t, 10*time.Second, "no-text refusal", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 2 })
	ns := steerNotices(t, url, origin.TaskID)
	if ns[1].Steer != lib.SteerRefused || ns[1].Reason != lib.SteerReasonNoText || ns[1].EnvelopeID != empty.EnvelopeID {
		t.Fatalf("second notice = %+v, want refused no-text", ns[1])
	}
	run := runOf(b, origin.TaskID)
	run.mu.Lock()
	queued := len(run.steers)
	run.mu.Unlock()
	if queued != 1 {
		t.Fatalf("queue holds %d, want 1 (no duplicate, no submission)", queued)
	}
	publishCancel(t, c, origin)
	waitTerminal(t, c, origin.TaskID)
}

// Once the turns are closed (the deliverable chosen, or finalize begun), a
// follow-up is refused task-ending, not queued behind a terminal, and the
// words do not promise a turn that will never run.
func TestSteer_TurnsClosedRefusesTaskEnding(t *testing.T) {
	_, url := startServer(t)
	cmd, started, release := blockingStub(t)
	b, _ := startBridgeConfig(t, Config{NATSURL: url, Command: cmd, KillGrace: 500 * time.Millisecond,
		Scope: capability.NamespaceScope("")}, nil)
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-steer-ending", "long question")
	waitStarted(t, started)
	run := runOf(b, origin.TaskID)
	run.mu.Lock()
	run.turnsClosed = true
	run.mu.Unlock()
	late := sendSteer(t, c, origin, "one more thing")
	waitFor(t, 10*time.Second, "task-ending refusal", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	n := steerNotices(t, url, origin.TaskID)[0]
	if n.Steer != lib.SteerRefused || n.Reason != lib.SteerReasonTaskEnding || n.EnvelopeID != late.EnvelopeID ||
		n.state != lib.StateWorking || n.final {
		t.Fatalf("notice = %+v, want refused task-ending on a non-final working status", n)
	}
	if strings.Contains(n.text, "queued") || !strings.Contains(n.text, "already chosen") {
		t.Fatalf("task-ending text %q does not say the answer is already chosen", n.text)
	}
	if err := os.WriteFile(release, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted || task.PostFinalDropped != 0 {
		t.Fatalf("state %s, post-final %d", task.State, task.PostFinalDropped)
	}
	if got := len(steerNotices(t, url, origin.TaskID)); got != 1 {
		t.Fatalf("%d notices, want the one refusal (nothing was queued to refuse at the end)", got)
	}
}

// A stalled bus holds a follow-up's notice, never run.mu: the notice
// publishes with mu released, so a cancel arriving mid-stall still kills the
// subprocess at once, and no notice lands after the terminal once the bus is
// back (ruling: notices outside run.mu, bounded). The stall is bounded by
// steerNoticeTimeout, not unbounded: a notice whose publish outlives it is
// dropped, so the test asserts no notice follows the terminal, not that the
// queued one landed.
func TestSteer_StalledNoticeDoesNotHoldRunLock(t *testing.T) {
	s, url := startServer(t)
	cmd, started, _ := blockingStub(t)
	b, _ := startBridgeConfig(t, Config{NATSURL: url, Command: cmd, KillGrace: 500 * time.Millisecond,
		Scope: capability.NamespaceScope("")}, nil)
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-steer-stall", "long question")
	waitStarted(t, started)
	run := runOf(b, origin.TaskID)

	steer, err := lib.NewFollowUpEnvelope(origin, gatewayParty,
		messagePayload(t, origin.TaskID, origin.ContextID, "while the bus is down"),
		lib.WithTo(lib.Party{Session: "platform"}), lib.WithAuthority(origin.Authority))
	if err != nil {
		t.Fatal(err)
	}
	cancelEnv, err := lib.NewCancelEnvelope(gatewayParty, origin.TaskID, origin.ContextID, origin.CorrelationID,
		lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	storeDir, port := s.JetStreamConfig().StoreDir, s.Addr().(*net.TCPAddr).Port
	s.Shutdown()
	s.WaitForShutdown()

	delivered := make(chan struct{})
	go func() { defer close(delivered); b.deliver(context.Background(), steer) }()
	// TryLock, so a notice publishing under mu reads as this failure, not
	// as a poll that blocks until the publish gives up.
	waitFor(t, 5*time.Second, "follow-up queued with run.mu free (a stall under mu fails here)", func() bool {
		if !run.mu.TryLock() {
			return false
		}
		defer run.mu.Unlock()
		return len(run.steers) == 1
	})
	select {
	case <-delivered:
		t.Fatal("the notice publish returned with the bus down; this test needs it stalled")
	case <-time.After(500 * time.Millisecond):
	}
	canceled := make(chan struct{})
	go func() { defer close(canceled); b.handleCancel(context.Background(), cancelEnv) }()
	select {
	case <-canceled:
	case <-time.After(2 * time.Second):
		t.Fatal("handleCancel blocked behind a stalled steer notice: the notice holds run.mu")
	}
	select {
	case <-delivered:
		t.Fatal("the notice publish returned with the bus still down")
	default:
	}

	// The bus comes back where it was; the notice, then the refusal, then
	// the terminal.
	s2, err := natsserver.NewServer(&natsserver.Options{Host: "127.0.0.1", Port: port, JetStream: true,
		StoreDir: storeDir, NoLog: true, NoSigs: true})
	if err != nil {
		t.Fatal(err)
	}
	go s2.Start()
	if !s2.ReadyForConnections(10 * time.Second) {
		t.Fatal("server not back")
	}
	t.Cleanup(s2.Shutdown)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCanceled || task.PostFinalDropped != 0 {
		t.Fatalf("state %s, post-final %d", task.State, task.PostFinalDropped)
	}
	<-delivered
	// Out of b.tasks means finalize is done with the bus (the registry
	// clear), so the cleanup's shutdown cannot strand it mid-write.
	waitFor(t, 15*time.Second, "run released", func() bool { return runOf(b, origin.TaskID) == nil })
	final := finalSeq(t, url, origin.TaskID)
	for _, n := range steerNotices(t, url, origin.TaskID) {
		if n.seq > final {
			t.Fatalf("notice %+v after the terminal at %d", n, final)
		}
	}
}

// finalize releases run.mu while it refuses a pending task's queued
// follow-ups. A worker freed in that gap still sees the run pending, takes
// it, and reaches its working publish; that publish must see the run ended
// and stay silent, never land a `working` after the canceled terminal
// (assertion 10). The bus is down across the gap so finalize holds it open
// on cue; the look-ahead seam holds the worker until finalize is in it.
func TestSteer_WorkerInFinalizeGapPublishesNoWorking(t *testing.T) {
	for _, executor := range []string{ExecutorCLI, ExecutorAPI} {
		t.Run(executor, func(t *testing.T) { testWorkerInFinalizeGap(t, executor) })
	}
}

func testWorkerInFinalizeGap(t *testing.T, executor string) {
	s, url := startServer(t)
	marker := filepath.Join(t.TempDir(), "spawned")
	cfg := Config{NATSURL: url, Command: script(t, fmt.Sprintf("touch %s\necho never", marker)),
		Concurrency: 1, TaskDeadline: 20 * time.Second, KillGrace: 500 * time.Millisecond,
		Scope: capability.NamespaceScope("")}
	var stub *apiStub
	if executor == ExecutorAPI {
		stub = newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			_ = os.WriteFile(marker, nil, 0o600)
			writeCompletion(w, "sess-gap", "never")
		})
		cfg.Executor, cfg.APIURL, cfg.APIKey = ExecutorAPI, stub.srv.URL+"/v1/chat/completions", testAPIKey
	}
	entered, gate := make(chan struct{}), make(chan struct{})
	var opened atomic.Bool
	openGate := func() {
		if opened.CompareAndSwap(false, true) {
			close(gate)
		}
	}
	t.Cleanup(openGate)
	b, _ := startBridgeConfig(t, cfg, func(b *Bridge) {
		b.lookAhead = func(ctx context.Context, run *taskRun) (bool, error) {
			close(entered)
			<-gate
			return false, nil
		}
	})
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-steer-gap", "never runs")
	<-entered // the worker holds the run, still pending
	sendSteer(t, c, origin, "first follow-up")
	sendSteer(t, c, origin, "second follow-up")
	waitFor(t, 10*time.Second, "two queued notices", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 2 })
	run := runOf(b, origin.TaskID)
	cancelEnv, err := lib.NewCancelEnvelope(gatewayParty, origin.TaskID, origin.ContextID, origin.CorrelationID,
		lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}

	storeDir, port := s.JetStreamConfig().StoreDir, s.Addr().(*net.TCPAddr).Port
	s.Shutdown()
	s.WaitForShutdown()
	go b.handleCancel(context.Background(), cancelEnv) // pending: finalize, stalled in its refusals
	waitFor(t, 5*time.Second, "finalize in its refusals", func() bool {
		if !run.mu.TryLock() {
			return false
		}
		defer run.mu.Unlock()
		return run.turnsClosed && run.state == statePending
	})
	openGate()
	waitFor(t, 5*time.Second, "worker took the run in the gap", func() bool {
		if !run.mu.TryLock() {
			return false
		}
		defer run.mu.Unlock()
		return run.state == stateRunning
	})
	time.Sleep(200 * time.Millisecond) // the worker reaches its working publish, behind noticeMu

	s2, err := natsserver.NewServer(&natsserver.Options{Host: "127.0.0.1", Port: port, JetStream: true,
		StoreDir: storeDir, NoLog: true, NoSigs: true})
	if err != nil {
		t.Fatal(err)
	}
	go s2.Start()
	if !s2.ReadyForConnections(10 * time.Second) {
		t.Fatal("server not back")
	}
	t.Cleanup(s2.Shutdown)
	task := waitTerminal(t, c, origin.TaskID)
	waitFor(t, 15*time.Second, "run released", func() bool { return runOf(b, origin.TaskID) == nil })
	time.Sleep(500 * time.Millisecond) // room for a late working publish to land
	task = waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCanceled || task.PostFinalDropped != 0 {
		t.Fatalf("state %s, post-final %d: the worker published after the terminal", task.State, task.PostFinalDropped)
	}
	final := finalSeq(t, url, origin.TaskID)
	for i, env := range replayEvents(t, url, origin.TaskID) {
		var su lib.StatusUpdate
		if env.Kind == lib.KindStatusUpdate && json.Unmarshal(env.Payload, &su) == nil &&
			su.Status.State == lib.StateWorking && su.Status.Message == nil {
			t.Fatalf("working status at %d (final at %d): a canceled-before-start task never works", i, final)
		}
	}
	// The refusals were published into the outage: one whose ack the
	// restart swallowed times out and is logged, so their count is not this
	// test's to assert, only that none follows the terminal.
	for _, n := range steerNotices(t, url, origin.TaskID) {
		if n.seq > final {
			t.Fatalf("notice %+v after the terminal at %d", n, final)
		}
	}
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("the worker ran the canceled task")
	}
}

// Every refusal's text names its own reason, and none claims the task goes
// on where it does not: a reader without the data part reads the truth.
func TestSteerNoticeText(t *testing.T) {
	for _, tc := range []struct {
		n          lib.SteerNotice
		want, deny []string
	}{
		{lib.SteerNotice{Steer: lib.SteerQueued}, []string{"queued", "next turn"}, nil},
		{lib.SteerNotice{Steer: lib.SteerRefused, Reason: lib.SteerReasonQueueFull}, []string{"queue-full", "16"}, nil},
		{lib.SteerNotice{Steer: lib.SteerRefused, Reason: lib.SteerReasonNoText}, []string{"no-text", "no text"}, nil},
		{lib.SteerNotice{Steer: lib.SteerRefused, Reason: lib.SteerReasonTaskEnding}, []string{"task-ending", "already chosen"}, []string{"continues"}},
		{lib.SteerNotice{Steer: lib.SteerRefused, Reason: lib.SteerReasonTaskEnded}, []string{"task-ended", "ended"}, []string{"continues", "after the answer"}},
		{lib.SteerNotice{Steer: lib.SteerRefused, Reason: lib.SteerReasonCapability}, []string{"capability"}, nil},
		{lib.SteerNotice{Steer: lib.SteerRefused, Reason: lib.SteerReasonNoResume}, []string{"no-resume"}, nil},
	} {
		text := steerNoticeText(tc.n)
		for _, w := range tc.want {
			if !strings.Contains(text, w) {
				t.Errorf("%+v: %q lacks %q", tc.n, text, w)
			}
		}
		for _, d := range tc.deny {
			if strings.Contains(text, d) {
				t.Errorf("%+v: %q says %q, which is not true for it", tc.n, text, d)
			}
		}
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

// publishCancel puts a cancel for origin on its in subject, addressed to the
// platform profile the way the gateway addresses one.
func publishCancel(t *testing.T, c *lib.Client, origin *lib.Envelope) *lib.Envelope {
	t.Helper()
	cancelEnv, err := lib.NewCancelEnvelope(gatewayParty, origin.TaskID, origin.ContextID, origin.CorrelationID,
		lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", origin.TaskID), cancelEnv); err != nil {
		t.Fatal(err)
	}
	return cancelEnv
}

// terminalReason is the text of the fold's final status message.
func terminalReason(t *testing.T, task *lib.Task) string {
	t.Helper()
	if task.FinalMessage == nil || len(task.FinalMessage.Parts) == 0 {
		t.Fatalf("terminal %s carries no reason", task.State)
	}
	return task.FinalMessage.Parts[0].Text
}

// bridgeDurableInfo reads the bridge's durable consumer.
func bridgeDurableInfo(t *testing.T, url string) *jetstream.ConsumerInfo {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	cons, err := js.Consumer(testCtx(t), lib.TasksStream, "bridge-platform")
	if err != nil {
		t.Fatal(err)
	}
	info, err := cons.Info(testCtx(t))
	if err != nil {
		t.Fatal(err)
	}
	return info
}

// ---- the pre-spawn look-ahead ------------------------------------------------

// A cancel already on the task's in subject when the bridge binds - the
// eval harness abandoning a submission nobody took, or any cancel inside the
// retention window - is honoured without a spawn: the worker's look-ahead
// finds it behind the submission, the run ends canceled-before-start, no
// working is ever published, and the durable's later delivery of the same
// cancel to handle is a no-op (a read, not a consume).
//
// The durable's delivery of that cancel is held until the terminal has been
// read. Its queued path in handleCancel writes the same record when it
// reaches the run before the worker's read returns, and on an embedded
// server it usually does; with the delivery held, the terminal can only be
// the worker's, and the look-ahead's own answer is asserted too. A look-ahead
// that read the subject and answered "no", or a worker that spawned
// regardless of the answer, fails here instead of passing on the durable's
// timing.
func TestLookAhead_CancelOnStreamBeforeBindNeverSpawns(t *testing.T) {
	_, url := startServer(t)
	c := gatewayClient(t, url)
	marker := filepath.Join(t.TempDir(), "spawned")

	origin := submit(t, c, "task-stale", "the stale prompt")
	publishCancel(t, c, origin)

	type answer struct {
		canceled bool
		err      error
		pending  bool // the run's state as the read returned
	}
	answers := make(chan answer, 1)
	release := make(chan struct{})
	var released atomic.Bool
	releaseCancel := func() {
		if released.CompareAndSwap(false, true) {
			close(release)
		}
	}
	t.Cleanup(releaseCancel)
	startBridgeWith(t, url, script(t, fmt.Sprintf(`touch %s
echo never`, marker)), 0, func(b *Bridge) {
		realLookAhead, realDeliver := b.lookAhead, b.deliver
		b.lookAhead = func(ctx context.Context, run *taskRun) (bool, error) {
			canceled, err := realLookAhead(ctx, run)
			answers <- answer{canceled: canceled, err: err, pending: run.pending()}
			return canceled, err
		}
		b.deliver = func(ctx context.Context, env *lib.Envelope) {
			if env.Kind == lib.KindCancel {
				<-release
			}
			realDeliver(ctx, env)
		}
	})

	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCanceled {
		t.Fatalf("state = %s, want canceled", task.State)
	}
	if reason := terminalReason(t, task); !strings.Contains(reason, "canceled-before-start") {
		t.Fatalf("reason = %q, want canceled-before-start", reason)
	}
	for _, s := range task.StatusHistory {
		if s == lib.StateWorking {
			t.Fatalf("history %v shows working for a run that was cancelled before it started", task.StatusHistory)
		}
	}
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("the stub ran: the stale prompt was spawned despite the cancel on the stream")
	}

	// The terminal landed with the durable's cancel still held, so the
	// worker wrote it, and it did so on the read's answer: the real replay
	// of the in subject said "cancel" while the run was still pending.
	var got answer
	select {
	case got = <-answers:
	default:
		t.Fatal("terminal written before the look-ahead returned: some path other than the worker's finalized the run")
	}
	if got.err != nil || !got.canceled {
		t.Fatalf("look-ahead answered (canceled=%v, err=%v), want (true, nil)", got.canceled, got.err)
	}
	if !got.pending {
		t.Fatal("the run was already finalized when the look-ahead answered, so the worker's branch was not what ended it")
	}

	// Now let the durable deliver the cancel: it is read and acked, and
	// handle does nothing with it - no event after the terminal.
	releaseCancel()
	waitFor(t, 10*time.Second, "durable to consume the trailing cancel", func() bool {
		info := bridgeDurableInfo(t, url)
		return info.NumPending == 0 && info.NumAckPending == 0 && info.Delivered.Consumer >= 2
	})
	time.Sleep(500 * time.Millisecond)
	after := fold(t, c, origin.TaskID)
	if after.PostFinalDropped != 0 {
		t.Fatalf("assertion 10: %d events after final - the durable's cancel was not a no-op", after.PostFinalDropped)
	}
	if n := len(replayEvents(t, url, origin.TaskID)); n != 2 {
		t.Fatalf("events = %d, want exactly submitted and the terminal", n)
	}
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("the stub ran after the terminal")
	}
}

// No cancel on the stream: the run spawns as before, and working is
// published only once the look-ahead has answered - while the read is
// outstanding the task still reads submitted.
func TestLookAhead_AbsentCancelSpawnsAndWorkingFollowsTheRead(t *testing.T) {
	_, url := startServer(t)
	c := gatewayClient(t, url)
	// The worker parks on the gate, so a failure before it opens must still
	// open it, or the bridge never leaves wg.Wait and cleanup reports a
	// shutdown failure that is this test's, not the bridge's.
	gate := make(chan struct{})
	var opened atomic.Bool
	openGate := func() {
		if opened.CompareAndSwap(false, true) {
			close(gate)
		}
	}
	var real func(context.Context, *taskRun) (bool, error)
	startBridgeWith(t, url, script(t, `echo "answer for $1"`), 0, func(b *Bridge) {
		real = b.lookAhead
		b.lookAhead = func(ctx context.Context, run *taskRun) (bool, error) {
			<-gate
			return real(ctx, run)
		}
	})
	// Registered after startBridgeWith, so it runs before the bridge's own
	// cleanup (t.Cleanup is last-added, first-called): the gate has to be
	// open before that cleanup waits on Run's wg.Wait.
	t.Cleanup(openGate)

	origin := submit(t, c, "task-clean", "a live prompt")
	waitFor(t, 10*time.Second, "submitted", func() bool {
		task, err := c.TasksGet(testCtx(t), "platform", origin.TaskID)
		return err == nil && task.State == lib.StateSubmitted
	})
	// The look-ahead is held; nothing past submitted may appear.
	time.Sleep(1 * time.Second)
	if task := fold(t, c, origin.TaskID); task.State != lib.StateSubmitted {
		t.Fatalf("state = %s while the look-ahead is outstanding, want submitted", task.State)
	}
	openGate()

	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	want := []lib.TaskState{lib.StateSubmitted, lib.StateWorking, lib.StateCompleted}
	if fmt.Sprint(task.StatusHistory) != fmt.Sprint(want) {
		t.Fatalf("history = %v, want %v", task.StatusHistory, want)
	}
}

// A look-ahead that fails is logged and the run spawns: a read failure must
// not become a dropped task.
func TestLookAhead_ReadFailureSpawns(t *testing.T) {
	_, url := startServer(t)
	c := gatewayClient(t, url)
	startBridgeWith(t, url, script(t, `echo "ran despite the failed read"`), 0, func(b *Bridge) {
		b.lookAhead = func(context.Context, *taskRun) (bool, error) {
			return false, fmt.Errorf("injected: the in subject could not be read")
		}
	})

	origin := submit(t, c, "task-blindspawn", "spawn me")
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	result := task.Artifact(lib.ArtifactResult)
	if result == nil || !strings.Contains(result.Parts[0].Text, "ran despite the failed read") {
		t.Fatal("the run did not spawn after the failed look-ahead")
	}
}

// Shutdown arriving while a worker is inside its look-ahead is not a read
// failure to spawn past: the worker leaves the run pending for shutdownTasks,
// whose terminal names bridge-shutdown, and the stub never runs.
//
// shutdownTasks is held out of the race until the worker has answered. It
// finalizes the same run with the same terminal, so a worker that spawned
// into the dead context and lost the run's mutex to it would leave the same
// record; holding b.mu, which shutdownTasks takes before it touches any run,
// makes the moment after the read the worker's alone, and what the run and
// the stream show then is the worker's branch and nothing else's.
func TestLookAhead_ShutdownMidReadLeavesTheRunToShutdownTasks(t *testing.T) {
	_, url := startServer(t)
	c := gatewayClient(t, url)
	marker := filepath.Join(t.TempDir(), "spawned")
	gate := make(chan struct{})
	var opened atomic.Bool
	openGate := func() {
		if opened.CompareAndSwap(false, true) {
			close(gate)
		}
	}
	type answer struct {
		run     *taskRun
		err     error
		dead    bool // ctx.Err() != nil as the read returned
		pending bool // the run's state as the read returned
	}
	answers := make(chan answer, 1)
	var b *Bridge
	shutdown := startBridgeWith(t, url, script(t, fmt.Sprintf(`touch %s
echo never`, marker)), 0, func(br *Bridge) {
		b = br
		real := br.lookAhead
		br.lookAhead = func(ctx context.Context, run *taskRun) (bool, error) {
			<-gate
			canceled, err := real(ctx, run)
			answers <- answer{run: run, err: err, dead: ctx.Err() != nil, pending: run.pending()}
			return canceled, err
		}
	})
	// After startBridgeWith for the reason the absent-cancel test gives:
	// the gate opens before the bridge's cleanup waits on its workers.
	t.Cleanup(openGate)

	origin := submit(t, c, "task-shutdown-midread", "a prompt")
	waitFor(t, 10*time.Second, "submitted", func() bool {
		task, err := c.TasksGet(testCtx(t), "platform", origin.TaskID)
		return err == nil && task.State == lib.StateSubmitted
	})

	// Hold shutdownTasks; it takes b.mu before it reads the run table.
	b.mu.Lock()
	held := true
	release := func() {
		if held {
			held = false
			b.mu.Unlock()
		}
	}
	t.Cleanup(release)
	shutdown()
	openGate()

	var got answer
	select {
	case got = <-answers:
	case <-time.After(10 * time.Second):
		t.Fatal("the look-ahead did not return after shutdown")
	}
	if !got.dead || got.err == nil {
		t.Fatalf("look-ahead returned err=%v with ctx dead=%v; the read did not fail on the dead context, so this test is not on the branch it covers", got.err, got.dead)
	}
	if !got.pending {
		t.Fatal("the run was not pending when the read returned; something other than the worker moved it while shutdownTasks was held")
	}
	// A worker that treated the dead context as a read failure would now
	// flip the run to running and publish working on that context; give it
	// long enough to have done so, then read what it left.
	time.Sleep(500 * time.Millisecond)
	if !got.run.pending() {
		t.Fatal("the worker moved the run past pending after shutdown: it spawned into the dead context instead of leaving the run to shutdownTasks")
	}
	if task := fold(t, c, origin.TaskID); task.Final || task.State != lib.StateSubmitted {
		t.Fatalf("state = %s (final=%v) with shutdownTasks held: the worker published after shutdown", task.State, task.Final)
	}
	release()

	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateFailed {
		t.Fatalf("state = %s, want failed", task.State)
	}
	if reason := terminalReason(t, task); !strings.Contains(reason, "bridge-shutdown") {
		t.Fatalf("reason = %q, want bridge-shutdown", reason)
	}
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("the stub ran after shutdown")
	}
}

// testJetStream is a JetStream handle of the test's own, for reads that must
// open no consumer.
func testJetStream(t *testing.T, url string) jetstream.JetStream {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	return js
}

// lastOnSubject is the newest envelope on a subject by direct get, nil when
// the subject holds nothing. It opens no consumer, so a test counting the
// stream's consumers can poll with it.
func lastOnSubject(t *testing.T, js jetstream.JetStream, subject string) *lib.Envelope {
	t.Helper()
	stream, err := js.Stream(testCtx(t), lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	msg, err := stream.GetLastMsgForSubject(testCtx(t), subject)
	if err != nil {
		if errors.Is(err, jetstream.ErrMsgNotFound) {
			return nil
		}
		t.Fatal(err)
	}
	env, err := lib.ParseEnvelope(msg.Data)
	if err != nil {
		t.Fatalf("unparseable message on %s: %v", subject, err)
	}
	return env
}

// streamConsumers is how many consumers TASKS holds right now.
func streamConsumers(t *testing.T, js jetstream.JetStream) int {
	t.Helper()
	stream, err := js.Stream(testCtx(t), lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	info, err := stream.Info(testCtx(t))
	if err != nil {
		t.Fatal(err)
	}
	return info.State.Consumers
}

// A bind that finds a backlog of abandoned submissions, each with its cancel
// behind it in the harness's order (a fan-out of submissions, then their
// cancels), refuses every one without a spawn and without a consumer slot:
// the look-ahead answers from the in subject's newest message, and the
// durable's later cancels find the terminal from the events subject's the
// same way. Before that, each pair cost two five-second ephemerals at bus
// speed, and on a 64-consumer TASKS a backlog of thirty pairs failed the
// look-ahead and spawned the stale prompts the read exists to refuse.
func TestLookAhead_StaleBurstOnBindSpawnsNothingAndHoldsNoConsumer(t *testing.T) {
	_, url := startServer(t)
	c := gatewayClient(t, url)
	js := testJetStream(t, url)
	markers := t.TempDir()
	const pairs, fanOut = 40, 4

	var origins []*lib.Envelope
	for i := 0; i < pairs; i += fanOut {
		var batch []*lib.Envelope
		for j := 0; j < fanOut; j++ {
			batch = append(batch, submit(t, c, fmt.Sprintf("task-stale-%02d", i+j), "the stale prompt"))
		}
		for _, o := range batch {
			publishCancel(t, c, o)
		}
		origins = append(origins, batch...)
	}
	startBridgeN(t, url, script(t, fmt.Sprintf(`touch %s/$1
echo never`, markers)), fanOut)

	// Every task final, read from its events subject's newest message so
	// the polling here opens none of the consumers counted below.
	waitFor(t, 60*time.Second, "every stale submission to be refused", func() bool {
		for _, o := range origins {
			if !lib.IsFinalStatus(lastOnSubject(t, js, lib.TaskEventsSubject("platform", o.TaskID))) {
				return false
			}
		}
		return true
	})
	for _, o := range origins {
		env := lastOnSubject(t, js, lib.TaskEventsSubject("platform", o.TaskID))
		var s lib.StatusUpdate
		if err := json.Unmarshal(env.Payload, &s); err != nil {
			t.Fatal(err)
		}
		if s.Status.State != lib.StateCanceled || s.Status.Message == nil ||
			!strings.Contains(s.Status.Message.Parts[0].Text, "canceled-before-start") {
			t.Fatalf("%s ended %s (%v), want canceled-before-start", o.TaskID, s.Status.State, s.Status.Message)
		}
	}
	waitFor(t, 20*time.Second, "durable to consume every submission and cancel", func() bool {
		info := bridgeDurableInfo(t, url)
		return info.NumPending == 0 && info.NumAckPending == 0 && info.Delivered.Consumer >= 2*pairs
	})
	if entries, err := os.ReadDir(markers); err != nil || len(entries) != 0 {
		t.Fatalf("%d stale prompts spawned (%v)", len(entries), err)
	}
	// No ephemeral outlived the refusals: the durable is the stream's only
	// consumer. A replay per look-ahead or per orphan cancel would still be
	// here, inside its five-second inactive threshold.
	if n := streamConsumers(t, js); n != 1 {
		t.Fatalf("TASKS holds %d consumers after the bind, want the durable alone: the refusals opened replays", n)
	}
}

// A backlog whose newest in message answers nothing, here a write no envelope
// parser accepts behind each cancel, reaches the fallback replay on every
// task, and the replay is paced: at most Concurrency of them are in hand at
// once, each held until its ephemeral is reaped, so the look-ahead holds at
// most that many consumer slots however long the backlog is. This test holds
// the sampled peak to twice Concurrency, the shape of the operator reserve's
// row with its tail factor (which the reserve counts at the default
// Concurrency), since the server's reaping lags the clock by a little.
// Every task is still refused without a spawn; it just takes the windows it
// takes. Unpaced, twelve such tasks opened twelve ephemerals inside a tenth
// of a second.
//
// The durable's delivery of the cancels is held until the workers have
// refused everything, as in the fresh-bind test: delivered, a cancel
// finalizes the pending run on handleCancel's queued path before the
// worker's read returns, and nothing would reach the replay this test is
// about. The submissions go on the stream first, so the durable accepts all
// of them before it meets the first held cancel.
func TestLookAhead_FallbackReplayIsPacedToTheReserve(t *testing.T) {
	_, url := startServer(t)
	c := gatewayClient(t, url)
	js := testJetStream(t, url)
	markers := t.TempDir()
	const tasks, workers = 12, 4
	const slotCeiling = 2 * workers // in flight and the tail, the reserve row's shape

	var origins []*lib.Envelope
	for i := 0; i < tasks; i++ {
		origins = append(origins, submit(t, c, fmt.Sprintf("task-fallback-%02d", i), "the stale prompt"))
	}
	for _, o := range origins {
		publishCancel(t, c, o)
		if _, err := js.Publish(testCtx(t), lib.TaskInSubject("platform", o.TaskID), []byte("not an envelope")); err != nil {
			t.Fatal(err)
		}
	}
	release := make(chan struct{})
	var released atomic.Bool
	releaseCancels := func() {
		if released.CompareAndSwap(false, true) {
			close(release)
		}
	}
	t.Cleanup(releaseCancels)
	startBridgeWith(t, url, script(t, fmt.Sprintf(`touch %s/$1
echo never`, markers)), workers, func(b *Bridge) {
		realDeliver := b.deliver
		b.deliver = func(ctx context.Context, env *lib.Envelope) {
			if env.Kind == lib.KindCancel {
				<-release
			}
			realDeliver(ctx, env)
		}
	})

	// Sample the consumer count while the backlog drains; the durable is
	// one, and every fallback replay is one more for five seconds.
	peak := 0
	waitFor(t, 60*time.Second, "every task to be refused through the paced replay", func() bool {
		if n := streamConsumers(t, js); n > peak {
			peak = n
		}
		for _, o := range origins {
			if !lib.IsFinalStatus(lastOnSubject(t, js, lib.TaskEventsSubject("platform", o.TaskID))) {
				return false
			}
		}
		return true
	})
	for _, o := range origins {
		var s lib.StatusUpdate
		if err := json.Unmarshal(lastOnSubject(t, js, lib.TaskEventsSubject("platform", o.TaskID)).Payload, &s); err != nil {
			t.Fatal(err)
		}
		if s.Status.State != lib.StateCanceled || s.Status.Message == nil ||
			!strings.Contains(s.Status.Message.Parts[0].Text, "canceled-before-start") {
			t.Fatalf("%s ended %s (%v), want canceled-before-start", o.TaskID, s.Status.State, s.Status.Message)
		}
	}
	if entries, err := os.ReadDir(markers); err != nil || len(entries) != 0 {
		t.Fatalf("%d stale prompts spawned (%v)", len(entries), err)
	}
	if peak > 1+slotCeiling {
		t.Fatalf("TASKS peaked at %d consumers, want at most the durable plus %d: the fallback replay is not paced to the reserve", peak, slotCeiling)
	}
	// Now the durable reads the cancels: each finds a final task from its
	// newest event and adds nothing.
	releaseCancels()
	waitFor(t, 20*time.Second, "durable to consume the held cancels", func() bool {
		info := bridgeDurableInfo(t, url)
		return info.NumPending == 0 && info.NumAckPending == 0
	})
	for _, o := range origins {
		if n := len(replayEvents(t, url, o.TaskID)); n != 2 {
			t.Fatalf("%s has %d events, want submitted and the terminal", o.TaskID, n)
		}
	}
}

// A run the durable's cancel has already finalized on handleCancel's queued
// path, which on a live bridge is where a task's own cancel usually lands
// while the worker's direct get is out, gets no fallback replay: no slot
// taken, no wait, no consumer. The order is pinned in three steps: the
// durable's cancels are held until every worker is inside the look-ahead
// with a pending run, then released so the queued path ends every run, and
// only then are the workers let through, so the real read runs on
// finalized runs only. (Left to timing, a cancel that lands before the
// dequeue is caught by the worker's own pending check and the look-ahead is
// never entered, which is correct and not what this test is about.)
func TestLookAhead_FinalizedRunSkipsTheFallbackReplay(t *testing.T) {
	_, url := startServer(t)
	c := gatewayClient(t, url)
	js := testJetStream(t, url)
	markers := t.TempDir()
	const workers = 4

	var origins []*lib.Envelope
	for i := 0; i < workers; i++ {
		origins = append(origins, submit(t, c, fmt.Sprintf("task-dead-%02d", i), "the stale prompt"))
	}
	for _, o := range origins {
		publishCancel(t, c, o)
		if _, err := js.Publish(testCtx(t), lib.TaskInSubject("platform", o.TaskID), []byte("not an envelope")); err != nil {
			t.Fatal(err)
		}
	}
	type answer struct {
		canceled bool
		err      error
		took     time.Duration
	}
	answers := make(chan answer, workers)
	var entered atomic.Int32
	gate := make(chan struct{})
	var opened atomic.Bool
	openGate := func() {
		if opened.CompareAndSwap(false, true) {
			close(gate)
		}
	}
	release := make(chan struct{})
	var released atomic.Bool
	releaseCancels := func() {
		if released.CompareAndSwap(false, true) {
			close(release)
		}
	}
	var bridge *Bridge
	startBridgeWith(t, url, script(t, fmt.Sprintf(`touch %s/$1
echo never`, markers)), workers, func(b *Bridge) {
		bridge = b
		real, realDeliver := b.lookAhead, b.deliver
		b.lookAhead = func(ctx context.Context, run *taskRun) (bool, error) {
			entered.Add(1)
			<-gate
			start := time.Now()
			canceled, err := real(ctx, run)
			answers <- answer{canceled: canceled, err: err, took: time.Since(start)}
			return canceled, err
		}
		b.deliver = func(ctx context.Context, env *lib.Envelope) {
			if env.Kind == lib.KindCancel {
				<-release
			}
			realDeliver(ctx, env)
		}
	})
	t.Cleanup(openGate)
	t.Cleanup(releaseCancels)

	// Every worker holds a pending run inside the look-ahead; now the
	// durable's cancels end every run under them.
	waitFor(t, 20*time.Second, "every worker to be inside the look-ahead", func() bool {
		return entered.Load() == workers
	})
	releaseCancels()
	waitFor(t, 20*time.Second, "the queued cancel path to finalize every task", func() bool {
		for _, o := range origins {
			if !lib.IsFinalStatus(lastOnSubject(t, js, lib.TaskEventsSubject("platform", o.TaskID))) {
				return false
			}
		}
		return true
	})
	openGate()
	for i := 0; i < workers; i++ {
		select {
		case got := <-answers:
			if got.err != nil || got.canceled {
				t.Fatalf("look-ahead on a finalized run answered (canceled=%v, err=%v), want (false, nil)", got.canceled, got.err)
			}
			if got.took >= lib.EphemeralConsumerInactiveThreshold {
				t.Fatalf("look-ahead on a finalized run took %s: it waited on a replay slot", got.took)
			}
		case <-time.After(20 * time.Second):
			t.Fatal("a held worker never returned from the look-ahead")
		}
	}
	if n := streamConsumers(t, js); n != 1 {
		t.Fatalf("TASKS holds %d consumers, want the durable alone: a finalized run was replayed", n)
	}
	// No slot was taken either: a slot stands for a consumer for the whole
	// threshold, so one taken here would hold the next real replay for a
	// consumer that never existed.
	if n := len(bridge.replaySlots); n != 0 {
		t.Fatalf("%d replay slots in hand after look-aheads on finalized runs, want none", n)
	}
	if entries, err := os.ReadDir(markers); err != nil || len(entries) != 0 {
		t.Fatalf("%d stale prompts spawned (%v)", len(entries), err)
	}
}

// cancelInStream's rule: a cancel counts when it is on the task's in
// subject after the submission; a cancel before the submission, or a
// follow-up, does not; an empty subject is "no" rather than an error.
func TestCancelInStream_Cases(t *testing.T) {
	_, url := startServer(t)
	c := gatewayClient(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	b, err := New(ctx, Config{NATSURL: url, Command: []string{"true"}, Concurrency: 8, ScratchDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cancel(); b.close() })
	// A held slot's release stays pending instead of firing on the real
	// threshold, so a case's delta cannot shift by a timer armed by an
	// earlier case firing between its two reads. holdsScheduled counts the
	// holds, which must match the slots each case leaves in hand.
	holdsScheduled := 0
	b.holdReplaySlot = func(func()) { holdsScheduled++ }

	runFor := func(origin *lib.Envelope) *taskRun {
		x, err := b.c.NewTaskExecution(origin, b.from, "platform")
		if err != nil {
			t.Fatal(err)
		}
		return &taskRun{origin: origin, exec: x}
	}
	unpublished := func(taskID string) *lib.Envelope {
		env, err := lib.NewMessageEnvelope(gatewayParty, taskID, "ctx-"+taskID, "corr-"+taskID,
			messagePayload(t, taskID, "ctx-"+taskID, "never sent"), lib.WithTo(lib.Party{Session: "platform"}))
		if err != nil {
			t.Fatal(err)
		}
		return env
	}

	// holds says whether the case leaves a replay slot in hand afterwards:
	// only a fallback replay that opened a consumer does. The direct-get
	// answers take no slot, and a fallback over an empty subject gives its
	// slot straight back. The delta is read case by case; the held slots'
	// releases never fire here (holdReplaySlot above).
	cases := []struct {
		name  string
		run   func() *taskRun
		want  bool
		holds bool
	}{
		{"submission alone", func() *taskRun {
			return runFor(submit(t, c, "la-alone", "p"))
		}, false, false},
		{"cancel after the submission", func() *taskRun {
			o := submit(t, c, "la-after", "p")
			publishCancel(t, c, o)
			return runFor(o)
		}, true, false},
		{"cancel before the submission is not newer", func() *taskRun {
			o := unpublished("la-before")
			publishCancel(t, c, o)
			if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", o.TaskID), o); err != nil {
				t.Fatal(err)
			}
			return runFor(o)
		}, false, false},
		// The newest message is a follow-up, so the answer comes from the
		// full replay, whose cursor starts after the submission: the cancel
		// before it is not newer, as in the direct-get case above.
		{"cancel before the submission, follow-up behind it, is still not newer", func() *taskRun {
			o := unpublished("la-before-steer")
			publishCancel(t, c, o)
			if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", o.TaskID), o); err != nil {
				t.Fatal(err)
			}
			steer, err := lib.NewFollowUpEnvelope(o, gatewayParty,
				messagePayload(t, o.TaskID, o.ContextID, "more"), lib.WithTo(lib.Party{Session: "platform"}))
			if err != nil {
				t.Fatal(err)
			}
			if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", o.TaskID), steer); err != nil {
				t.Fatal(err)
			}
			return runFor(o)
		}, false, true},
		{"follow-up after the submission is not a cancel", func() *taskRun {
			o := submit(t, c, "la-steer", "p")
			steer, err := lib.NewFollowUpEnvelope(o, gatewayParty,
				messagePayload(t, o.TaskID, o.ContextID, "more"), lib.WithTo(lib.Party{Session: "platform"}))
			if err != nil {
				t.Fatal(err)
			}
			if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", o.TaskID), steer); err != nil {
				t.Fatal(err)
			}
			return runFor(o)
		}, false, true},
		{"nothing on the subject", func() *taskRun {
			return runFor(unpublished("la-empty"))
		}, false, false},
		// The newest message is a follow-up, so the answer comes from the
		// full replay, which finds the cancel between.
		{"cancel behind the submission, follow-up behind the cancel", func() *taskRun {
			o := submit(t, c, "la-cancel-then-steer", "p")
			publishCancel(t, c, o)
			steer, err := lib.NewFollowUpEnvelope(o, gatewayParty,
				messagePayload(t, o.TaskID, o.ContextID, "more"), lib.WithTo(lib.Party{Session: "platform"}))
			if err != nil {
				t.Fatal(err)
			}
			if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", o.TaskID), steer); err != nil {
				t.Fatal(err)
			}
			return runFor(o)
		}, true, true},
		{"follow-up behind the submission, cancel behind the follow-up", func() *taskRun {
			o := submit(t, c, "la-steer-then-cancel", "p")
			steer, err := lib.NewFollowUpEnvelope(o, gatewayParty,
				messagePayload(t, o.TaskID, o.ContextID, "more"), lib.WithTo(lib.Party{Session: "platform"}))
			if err != nil {
				t.Fatal(err)
			}
			if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", o.TaskID), steer); err != nil {
				t.Fatal(err)
			}
			publishCancel(t, c, o)
			return runFor(o)
		}, true, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			before, beforeHolds := len(b.replaySlots), holdsScheduled
			got, err := b.cancelInStream(ctx, tc.run())
			if err != nil {
				t.Fatalf("cancelInStream: %v", err)
			}
			if got != tc.want {
				t.Fatalf("cancelInStream = %v, want %v", got, tc.want)
			}
			want := map[bool]int{true: 1, false: 0}[tc.holds]
			if held := len(b.replaySlots) - before; held != want {
				t.Fatalf("replay slots held by this case = %d, want %d", held, want)
			}
			if scheduled := holdsScheduled - beforeHolds; scheduled != want {
				t.Fatalf("slot holds scheduled by this case = %d, want %d", scheduled, want)
			}
		})
	}
}

// finalize writes exactly one terminal however many callers reach it, and
// handleCancel on an already-final run does nothing - whether the run is
// still in the bridge's table (the window before finalize removes it) or
// already gone (the orphan path, which finds the terminal on the stream).
func TestFinalize_IdempotentAndCancelAfterFinalIsANoOp(t *testing.T) {
	_, url := startServer(t)
	c := gatewayClient(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	b, err := New(ctx, Config{NATSURL: url, Command: []string{"true"}, ScratchDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cancel(); b.close() })

	origin := submit(t, c, "task-final-twice", "p")
	x, err := b.c.NewTaskExecution(origin, b.from, "platform")
	if err != nil {
		t.Fatal(err)
	}
	if err := x.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	run := &taskRun{origin: origin, exec: x}
	b.mu.Lock()
	b.tasks[origin.TaskID] = run
	b.mu.Unlock()

	b.finalize(run, lib.StateCanceled, canceledBeforeStartReason, nil)
	b.finalize(run, lib.StateFailed, "reason: a second finalizer", nil)
	run.mu.Lock()
	state := run.state
	run.mu.Unlock()
	if state != stateDone {
		t.Fatalf("state = %v, want done", state)
	}

	cancelEnv, err := lib.NewCancelEnvelope(gatewayParty, origin.TaskID, origin.ContextID, origin.CorrelationID,
		lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	// Still in the table, already final.
	b.mu.Lock()
	b.tasks[origin.TaskID] = run
	b.mu.Unlock()
	b.handleCancel(ctx, cancelEnv)
	// Gone from the table: the orphan path.
	b.mu.Lock()
	delete(b.tasks, origin.TaskID)
	b.mu.Unlock()
	b.handleCancel(ctx, cancelEnv)

	events := replayEvents(t, url, origin.TaskID)
	finals := 0
	for _, env := range events {
		if _, final := statusState(t, env); final {
			finals++
		}
	}
	if finals != 1 || len(events) != 2 {
		t.Fatalf("events = %d with %d finals, want submitted plus exactly one terminal", len(events), finals)
	}
	task := fold(t, c, origin.TaskID)
	if task.State != lib.StateCanceled || task.PostFinalDropped != 0 {
		t.Fatalf("state = %s, post-final drops = %d; want canceled and 0", task.State, task.PostFinalDropped)
	}
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

// A failed turn's diagnosis is on stdout: `hermes chat -Q` prints a failed
// turn's final_response there and exits 1, and prints the session id last on
// stderr. The terminal carries both, so the loss class #2036 recorded (an
// "Error: max retries exhausted" discarded with the exit) is in the status
// message and the transcript is findable from it.
func TestLifecycle_FailedKeepsStdoutAndSessionID(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, script(t, `echo "Error: max retries exhausted after 6 attempts"
echo "[hermes-otel] disabled" >&2
echo "" >&2
echo "session_id: 20260925_181506_ab12cd" >&2
exit 1`))
	c := gatewayClient(t, url)

	submit(t, c, "task-fail-out", "doomed")
	task := waitTerminal(t, c, "task-fail-out")
	if task.State != lib.StateFailed {
		t.Fatalf("state = %s, want failed", task.State)
	}
	reason := terminalReason(t, task)
	for _, want := range []string{
		"reason: hermes-exited-nonzero - exit status 1",
		"session: 20260925_181506_ab12cd",
		"stdout tail: Error: max retries exhausted after 6 attempts",
		"stderr tail: ",
	} {
		if !strings.Contains(reason, want) {
			t.Errorf("terminal reason lacks %q:\n%s", want, reason)
		}
	}
}

// Exit 75 is Hermes's EX_TEMPFAIL for a turn that gave up on the provider's
// rate limit. The terminal names it, so the harness classes a quota storm as
// infrastructure rather than the persona's failure.
func TestLifecycle_RateLimitedExitIsNamed(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, script(t, `echo "Error: rate limit retries exhausted"
echo "session_id: 20260925_181506_rl" >&2
exit 75`))
	c := gatewayClient(t, url)

	submit(t, c, "task-rl", "doomed")
	task := waitTerminal(t, c, "task-rl")
	if task.State != lib.StateFailed {
		t.Fatalf("state = %s, want failed", task.State)
	}
	reason := terminalReason(t, task)
	if !strings.HasPrefix(reason, "reason: hermes-rate-limited - exit status 75") {
		t.Errorf("reason = %q, want the rate-limited token", reason)
	}
	if strings.Contains(reason, "hermes-exited-nonzero") {
		t.Errorf("reason names the generic token beside the specific one: %q", reason)
	}
}

// The tails are bounded and cut on a rune boundary.
func TestTail_BoundedOnARuneBoundary(t *testing.T) {
	long := strings.Repeat("é", 2000) // 4000 bytes, every rune two of them
	// An odd budget lands the byte cut inside a rune, so the walk has to move.
	got := tail(long, 2047)
	if len(got) != 2046 || !utf8.ValidString(got) || !strings.HasSuffix(long, got) {
		t.Fatalf("tail: len=%d (want 2046) valid=%v suffix=%v", len(got), utf8.ValidString(got), strings.HasSuffix(long, got))
	}
	if tail("short", 2048) != "short" {
		t.Fatal("a short string is returned whole")
	}
	// The stderr buffer cuts on bytes as it fills and opens on a rune when read.
	tb := newTailBuffer(4)
	_, _ = tb.Write([]byte("aé")) // 3 bytes
	_, _ = tb.Write([]byte("éb")) // +3 = 6; the last 4 bytes open inside the first é
	if got := tb.String(); !utf8.ValidString(got) || got != "éb" {
		t.Fatalf("tailBuffer.String() = %q (valid=%v), want \"éb\"", got, utf8.ValidString(got))
	}
}

// A transient lookup failure on a submission is retried, not dropped: the
// lib acks after the handler returns, so without the retry the task was lost.
// Driven through the tasksGet seam so no bus fault has to be staged.
func TestLookup_TransientErrorIsRetriedThenAccepted(t *testing.T) {
	_, url := startServer(t)
	var calls atomic.Int32
	stop := startBridgeWith(t, url, script(t, `echo ok`), 1, func(b *Bridge) {
		real := b.c.TasksGetOpened
		b.tasksGet = func(ctx context.Context, addressee, taskID string) (*lib.Task, bool, error) {
			if calls.Add(1) < 2 {
				return nil, false, errors.New("nats: timeout on the horizon get")
			}
			return real(ctx, addressee, taskID)
		}
	})
	defer stop()
	c := gatewayClient(t, url)

	submit(t, c, "task-retry", "hello")
	task := waitTerminal(t, c, "task-retry")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed after the lookup retries", task.State)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("lookup attempts = %d, want 2 (one transient failure, then the real read)", got)
	}
}

// The cancel's retry schedule outlasts the fault it is named for: a
// consumer-cap refusal clears when the ephemeral consumers holding the cap
// are reaped, after the lib's inactive threshold. (The submission's lookup
// never opens a consumer for a new task, so its schedule is one quick retry.) Pinned as arithmetic, because the
// constant it has to beat lives in another package and could move.
func TestLookup_ScheduleOutlastsTheConsumerInactiveThreshold(t *testing.T) {
	var total time.Duration
	for attempt := 1; attempt < cancelLookupAttempts; attempt++ {
		total += cancelLookupBackoff * time.Duration(attempt)
	}
	if total <= lib.EphemeralConsumerInactiveThreshold {
		t.Fatalf("retry schedule waits %s in total, which does not outlast the %s inactive threshold a cap refusal clears on",
			total, lib.EphemeralConsumerInactiveThreshold)
	}
}

// The same retry stands behind an orphan's cancel: a cancel for a task with
// non-final events and no live executor used to be dropped on the first
// lookup error, leaving the orphan non-terminal for the retention window.
// The fixture is TestSweep_OrphanFinalized's prior incarnation without the
// in-flight KV key, so the start-up sweep leaves it alone and the cancel is
// what finalizes it.
func TestLookup_OrphanCancelRetriesThenSynthesizes(t *testing.T) {
	_, url := startServer(t)
	c := gatewayClient(t, url)
	taskID := "task-orphan-cancel"
	origin := submit(t, c, taskID, "died midway, nobody holds me")
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

	var calls atomic.Int32
	stop := startBridgeWith(t, url, script(t, `echo unreachable`), 1, func(b *Bridge) {
		real := b.c.TasksGetOpened
		b.tasksGet = func(ctx context.Context, addressee, id string) (*lib.Task, bool, error) {
			if id == taskID && calls.Add(1) < 3 {
				return nil, false, errors.New("nats: maximum consumers limit reached")
			}
			return real(ctx, addressee, id)
		}
	})
	defer stop()

	publishCancel(t, c, origin)
	task := waitTerminal(t, c, taskID)
	if task.State != lib.StateCanceled {
		t.Fatalf("state = %s, want canceled", task.State)
	}
	if reason := terminalReason(t, task); !strings.Contains(reason, "canceled-while-orphaned") {
		t.Fatalf("reason = %q, want canceled-while-orphaned", reason)
	}
	if got := calls.Load(); got != 3 {
		t.Fatalf("lookup attempts on the cancel = %d, want 3 (two refusals, then the read)", got)
	}
}

// A lookup that keeps failing is still dropped, after the bounded attempts,
// and a not-found answer is never retried.
func TestLookup_BoundedAndNotFoundIsAnAnswer(t *testing.T) {
	_, url := startServer(t)
	var calls atomic.Int32
	stop := startBridgeWith(t, url, script(t, `echo ok`), 1, func(b *Bridge) {
		b.tasksGet = func(ctx context.Context, addressee, taskID string) (*lib.Task, bool, error) {
			calls.Add(1)
			return nil, false, errors.New("nats: timeout")
		}
	})
	defer stop()
	c := gatewayClient(t, url)
	submit(t, c, "task-drop", "hello")
	time.Sleep(3 * time.Second) // the submission schedule (one 200ms retry) plus slack
	if got := calls.Load(); got != submissionLookupAttempts {
		t.Fatalf("lookup attempts = %d, want %d", got, submissionLookupAttempts)
	}
	if ev := replayEvents(t, url, "task-drop"); len(ev) != 0 {
		t.Fatalf("a dropped submission published %d events, want none", len(ev))
	}

	b := &Bridge{cfg: Config{Profile: "platform", Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}}
	var nf atomic.Int32
	b.tasksGet = func(ctx context.Context, addressee, taskID string) (*lib.Task, bool, error) {
		nf.Add(1)
		return nil, false, &lib.A2AError{Code: lib.CodeTaskNotFound}
	}
	if _, attempts, err := b.lookupTask(context.Background(), "x", cancelLookupAttempts, cancelLookupBackoff); !isTaskNotFound(err) || nf.Load() != 1 || attempts != 1 {
		t.Fatalf("not-found was retried or lost: err=%v calls=%d attempts=%d", err, nf.Load(), attempts)
	}

	// A context that ends inside the first backoff reports one attempt, not
	// the bound: the drop log must not count a shutdown as a cap incident.
	ctx, cancel := context.WithCancel(context.Background())
	b.tasksGet = func(context.Context, string, string) (*lib.Task, bool, error) {
		cancel()
		return nil, false, errors.New("nats: timeout")
	}
	if _, attempts, err := b.lookupTask(ctx, "y", cancelLookupAttempts, cancelLookupBackoff); err == nil || attempts != 1 {
		t.Fatalf("interrupted lookup: err=%v attempts=%d, want an error and 1", err, attempts)
	}

	// An error after the read opened its consumer is not retried: that
	// consumer is live for the inactive threshold, and another attempt
	// would open another against the same cap.
	var opened atomic.Int32
	b.tasksGet = func(context.Context, string, string) (*lib.Task, bool, error) {
		opened.Add(1)
		return nil, true, errors.New("protocol error folding events")
	}
	if _, attempts, err := b.lookupTask(context.Background(), "z", cancelLookupAttempts, cancelLookupBackoff); err == nil || attempts != 1 || opened.Load() != 1 {
		t.Fatalf("post-create failure: err=%v attempts=%d calls=%d, want an error after exactly one lookup", err, attempts, opened.Load())
	}
}

// The session id is the CLI's own line, the last one, and a label with
// nothing after it is no id at all rather than the next line's first word.
func TestFailureReason_SessionIDIsTheLastWholeLine(t *testing.T) {
	err := errors.New("exit status 1")
	cases := []struct{ stderr, want string }{
		{"session_id: a1\n[tool] nested run said\nsession_id: b2\n", "session: b2"},
		{"session_id:\n[hermes-otel] disabled\n", ""},
		{"nothing here\n", ""},
	}
	for _, c := range cases {
		got := failureReason(err, "", c.stderr, 1)
		if c.want == "" {
			if strings.Contains(got, "session:") {
				t.Errorf("stderr %q: reason reports a session id: %q", c.stderr, got)
			}
			continue
		}
		if !strings.Contains(got, c.want) || strings.Contains(got, "session: a1") || strings.Contains(got, "session: [hermes-otel]") {
			t.Errorf("stderr %q: reason = %q, want %q and not an earlier line or the next line's word", c.stderr, got, c.want)
		}
	}
}

// A submission with no text parts is terminal rejected - an executor
// refusing work before starting it. It carries a real capability: without
// one this test passes on the capability refusal instead, asserting rejected
// for a reason that has nothing to do with the content check.
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
		lib.WithTo(lib.Party{Session: "platform"}),
		lib.WithAuthority(authorityFor(t, mintFor(t, c, taskID, "platform"))))
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
	if text := terminalText(t, url, taskID); strings.Contains(text, "capability-refused") {
		t.Fatalf("refused for the wrong reason, so this test proves nothing about content: %q", text)
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

// Assertion 12 for a QUEUED task: a follow-up arriving while the run waits
// for a worker slot is queued on a submitted status, not working - a
// follow-up must not change folded state by itself.
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

	waitFor(t, 10*time.Second, "steer queued on queued task", func() bool {
		for _, n := range steerNotices(t, url, queued.TaskID) {
			if n.Steer == lib.SteerQueued && n.EnvelopeID == steer.EnvelopeID {
				if n.state != lib.StateSubmitted || n.final {
					t.Fatalf("queued notice %+v, want a non-final submitted status for a queued task", n)
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
	// Both tasks still finish clean. The stub prints no session id, so the
	// queued follow-up cannot run as a resumed turn: it is refused
	// no-resume, ahead of the terminal.
	if got := waitTerminal(t, c, "task-queued"); got.State != lib.StateCompleted || got.PostFinalDropped != 0 {
		t.Fatalf("queued task ended %s (post-final %d), want completed", got.State, got.PostFinalDropped)
	}
	ns := steerNotices(t, url, queued.TaskID)
	if len(ns) != 2 || ns[1].Steer != lib.SteerRefused || ns[1].Reason != lib.SteerReasonNoResume ||
		ns[1].seq > finalSeq(t, url, queued.TaskID) {
		t.Fatalf("notices %+v, want queued then refused no-resume before the terminal", ns)
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

// ---- follow-up turns (cli executor) ------------------------------------------

func TestResumeArgv(t *testing.T) {
	got, err := resumeArgv([]string{"hermes", "-p", "platform", "chat", "-Q", "-q"}, "sess-9", "and the west")
	want := []string{"hermes", "-p", "platform", "chat", "-Q", "--resume", "sess-9", "--query=and the west"}
	if err != nil || !slices.Equal(got, want) {
		t.Fatalf("resumeArgv = %q, %v; want %q", got, err, want)
	}
	if _, err := resumeArgv([]string{"/stub"}, "sess-9", "x"); err == nil {
		t.Fatal("a command not ending in -q must be refused: there is nowhere to put --resume")
	}
}

// A prompt that starts with "-" is one --query= token on every turn: after a
// bare -q, Hermes's argparse (hermes_cli/_parser.py, chat's -q/--query)
// reads "--force" as an option and exits 2, failing the turn. A command not
// ending in -q gets the prompt appended unchanged.
func TestPromptArgvKeepsADashLedPromptTheQuery(t *testing.T) {
	cmd := []string{"hermes", "-p", "platform", "chat", "-Q", "-q"}
	for _, prompt := range []string{"--force", "-p evil", "--help", "plain words"} {
		want := []string{"hermes", "-p", "platform", "chat", "-Q", "--query=" + prompt}
		if got := promptArgv(cmd, prompt); !slices.Equal(got, want) {
			t.Errorf("promptArgv(%q) = %q, want %q", prompt, got, want)
		}
		got, err := resumeArgv(cmd, "sess-9", prompt)
		if want := []string{"hermes", "-p", "platform", "chat", "-Q", "--resume", "sess-9", "--query=" + prompt}; err != nil || !slices.Equal(got, want) {
			t.Errorf("resumeArgv(%q) = %q, %v; want %q", prompt, got, err, want)
		}
	}
	if got := promptArgv([]string{"/stub"}, "--force"); !slices.Equal(got, []string{"/stub", "--force"}) {
		t.Errorf("promptArgv on a non -q command = %q, want the prompt appended", got)
	}
	if got := promptArgv(cmd, "x"); &got[0] == &cmd[0] || cmd[len(cmd)-1] != "-q" {
		t.Error("promptArgv aliased or changed the configured command")
	}
}

func TestLastSessionID(t *testing.T) {
	if got := lastSessionID("noise\nsession_id: nested-1\nmore\nsession_id: sess-2\n"); got != "sess-2" {
		t.Fatalf("got %q, want the last line's id", got)
	}
	if got := lastSessionID("session_id: 20260925_181506_ab12cd\n\n  \n"); got != "20260925_181506_ab12cd" {
		t.Fatalf("trailing blank lines: got %q, want the id", got)
	}
	if got := lastSessionID("session_id:\n"); got != "" {
		t.Fatalf("an empty label yields %q, want none", got)
	}
}

// The id goes into a child's argv after --resume, so one that could read as
// an option, or carries anything past the id's shape, is no id: the
// follow-ups are refused no-resume rather than resuming it.
func TestLastSessionIDRefusesADashLedOrMalformedID(t *testing.T) {
	for _, stderr := range []string{
		"session_id: --yolo\n",
		"session_id: -sess-1\n",
		"session_id: sess-1 --yolo\n",
		"session_id: sess/../other\n",
		"session_id: " + strings.Repeat("a", 129) + "\n",
	} {
		if got := lastSessionID(stderr); got != "" {
			t.Errorf("stderr %q: got %q, want no id", stderr, got)
		}
	}
}

// Only the last non-blank line is the CLI's own: an id followed by more
// output (a tool's subprocess writing after it, sharing the stream) is not
// taken, so a turn cannot be pointed at another conversation's session.
func TestLastSessionIDIsOnlyTheLastNonBlankLine(t *testing.T) {
	if got := lastSessionID("session_id: sess-1\n[tool] still talking\n"); got != "" {
		t.Fatalf("got %q, want none: the id line was not last", got)
	}
	if got := lastSessionID("session_id: sess-1\nsession_id: other-conv\n"); got != "other-conv" {
		t.Fatalf("got %q, want the last line's id", got)
	}
}

func TestTurnNote(t *testing.T) {
	if got := turnNote(1); got != "" {
		t.Fatalf("turnNote(1) = %q, want none on the first turn", got)
	}
	if got := turnNote(3); got != "; turn: 3" {
		t.Fatalf("turnNote(3) = %q", got)
	}
	reason := failureReason(errors.New("exit status 1"), "out", "session_id: s-1\n", 2)
	if !strings.Contains(reason, "; session: s-1; turn: 2; stdout tail: ") {
		t.Fatalf("reason %q, want the turn note between the session and the tails", reason)
	}
}

// artifactsNamed is each artifact of the given name on the task's events,
// its chunks joined, in the order the artifacts first appeared.
func artifactsNamed(t *testing.T, url, taskID, name string) []string {
	t.Helper()
	var ids []string
	texts := map[string]string{}
	for _, env := range replayEvents(t, url, taskID) {
		if env.Kind != lib.KindArtifactUpdate {
			continue
		}
		var a lib.ArtifactUpdate
		if json.Unmarshal(env.Payload, &a) != nil || a.Artifact.Name != name {
			continue
		}
		id := a.Artifact.ArtifactID
		if _, ok := texts[id]; !ok {
			ids = append(ids, id)
		}
		var sb strings.Builder
		for _, p := range a.Artifact.Parts {
			sb.WriteString(p.Text)
		}
		if a.Append {
			texts[id] += sb.String()
		} else {
			texts[id] = sb.String()
		}
	}
	out := make([]string, 0, len(ids))
	for _, id := range ids {
		out = append(out, texts[id])
	}
	return out
}

// resumeStub: turn 1 blocks until release, then every turn answers with its
// own prompt (the --query= token's value) and the session id; ARGS records
// each child's argv. resumeExit is the --resume branch's exit code.
func resumeStub(t *testing.T, resumeExit int, printSession bool) (cmd []string, started, release, args string) {
	t.Helper()
	dir := t.TempDir()
	started, release, args = filepath.Join(dir, "started"), filepath.Join(dir, "release"), filepath.Join(dir, "args")
	sess := `echo "session_id: sess-1" >&2`
	if !printSession {
		sess = ":"
	}
	body := fmt.Sprintf(`echo "$*" >> %[3]s
for last; do :; done
last=${last#--query=}
case "$*" in
*--resume*) echo "answer to $last"; %[4]s; exit %[5]d ;;
*) touch %[1]s; while [ ! -f %[2]s ]; do sleep 0.05; done; echo "answer to $last"; %[4]s ;;
esac`, started, release, args, sess, resumeExit)
	return append(script(t, body), "-q"), started, release, args
}

func TestCLI_FollowUpsRunAsResumedTurnsInOrder(t *testing.T) {
	_, url := startServer(t)
	cmd, started, release, args := resumeStub(t, 0, true)
	startBridge(t, url, cmd)
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-cli-turns", "long question")
	waitStarted(t, started)
	sendSteer(t, c, origin, "also east")
	sendSteer(t, c, origin, "and west")
	waitFor(t, 10*time.Second, "two queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 2 })
	if err := os.WriteFile(release, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted || task.PostFinalDropped != 0 {
		t.Fatalf("state %s, post-final %d", task.State, task.PostFinalDropped)
	}
	if got := artifactsNamed(t, url, origin.TaskID, lib.ArtifactTurn); !slices.Equal(got, []string{"answer to long question\n", "answer to also east\n"}) {
		t.Fatalf("turn answers = %q", got)
	}
	if got := artifactsNamed(t, url, origin.TaskID, lib.ArtifactResult); !slices.Equal(got, []string{"answer to and west\n"}) {
		t.Fatalf("result = %q, want the last turn's answer only", got)
	}
	raw, _ := os.ReadFile(args)
	want := "--query=long question\n--resume sess-1 --query=also east\n--resume sess-1 --query=and west\n"
	if string(raw) != want {
		t.Fatalf("argv per child:\n%s\nwant:\n%s", raw, want)
	}
	for _, n := range steerNotices(t, url, origin.TaskID) {
		if n.Steer != lib.SteerQueued {
			t.Fatalf("a follow-up that ran was also refused: %+v", n)
		}
	}
}

func TestCLI_ResumeFailureFailsTheTaskAfterTheFirstAnswer(t *testing.T) {
	_, url := startServer(t)
	cmd, started, release, _ := resumeStub(t, 1, true)
	startBridge(t, url, cmd)
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-cli-resume-fail", "long question")
	waitStarted(t, started)
	sendSteer(t, c, origin, "also east")
	sendSteer(t, c, origin, "and west")
	waitFor(t, 10*time.Second, "two queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 2 })
	_ = os.WriteFile(release, nil, 0o600)
	task := waitTerminal(t, c, origin.TaskID)
	reason := terminalReason(t, task)
	if task.State != lib.StateFailed || !strings.Contains(reason, "reason: hermes-exited-nonzero") || !strings.Contains(reason, "; turn: 2") {
		t.Fatalf("state %s reason %q", task.State, reason)
	}
	if got := artifactsNamed(t, url, origin.TaskID, lib.ArtifactTurn); len(got) != 1 {
		t.Fatalf("turn 1's answer must have gone out before turn 2 ran: %q", got)
	}
	ns := steerNotices(t, url, origin.TaskID)
	if last := ns[len(ns)-1]; last.Steer != lib.SteerRefused || last.Reason != lib.SteerReasonTaskEnded {
		t.Fatalf("the unrun follow-up: %+v, want refused task-ended", last)
	}
	if len(ns) != 3 || ns[len(ns)-1].seq > finalSeq(t, url, origin.TaskID) {
		t.Fatalf("notices %+v, want two queued and one refusal, all before the terminal", ns)
	}
}

func TestCLI_NoSessionIDRefusesFollowUpsAndCompletes(t *testing.T) {
	_, url := startServer(t)
	cmd, started, release, args := resumeStub(t, 0, false)
	startBridge(t, url, cmd)
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-cli-nosess", "long question")
	waitStarted(t, started)
	sendSteer(t, c, origin, "also east")
	waitFor(t, 10*time.Second, "queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	_ = os.WriteFile(release, nil, 0o600)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted {
		t.Fatalf("state %s", task.State)
	}
	ns := steerNotices(t, url, origin.TaskID)
	if len(ns) != 2 || ns[1].Steer != lib.SteerRefused || ns[1].Reason != lib.SteerReasonNoResume {
		t.Fatalf("notices = %+v, want queued then refused no-resume", ns)
	}
	if raw, _ := os.ReadFile(args); strings.Count(string(raw), "\n") != 1 {
		t.Fatalf("a follow-up ran with no session to resume: %s", raw)
	}
	if got := artifactsNamed(t, url, origin.TaskID, lib.ArtifactResult); !slices.Equal(got, []string{"answer to long question\n"}) {
		t.Fatalf("result = %q, want turn 1's answer", got)
	}
}

func TestCLI_CancelDuringAFollowUpTurn(t *testing.T) {
	_, url := startServer(t)
	dir := t.TempDir()
	turn1, turn2 := filepath.Join(dir, "turn1"), filepath.Join(dir, "turn2")
	cmd := append(script(t, fmt.Sprintf(`case "$*" in
*--resume*) touch %s; sleep 30 ;;
*) touch %s; echo first; echo "session_id: sess-1" >&2; sleep 1 ;;
esac`, turn2, turn1)), "-q")
	startBridge(t, url, cmd)
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-cli-cancel2", "q")
	waitStarted(t, turn1) // turn 1 is sleeping its 1s
	sendSteer(t, c, origin, "second")
	sendSteer(t, c, origin, "third")
	waitFor(t, 10*time.Second, "turn 2 running", func() bool { _, err := os.Stat(turn2); return err == nil })
	publishCancel(t, c, origin)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCanceled || terminalReason(t, task) != "reason: canceled-by-request" {
		t.Fatalf("state %s reason %q", task.State, terminalReason(t, task))
	}
	ns := steerNotices(t, url, origin.TaskID)
	if last := ns[len(ns)-1]; last.Reason != lib.SteerReasonTaskEnded {
		t.Fatalf("the third follow-up: %+v, want refused task-ended", last)
	}
}

// Once the worker has closed the queue (no follow-up to run), a follow-up is
// refused task-ending, before the terminal.
func TestCLI_FollowUpAfterTheQueueClosesIsRefused(t *testing.T) {
	_, url := startServer(t)
	cmd, started, release, args := resumeStub(t, 0, true)
	b, _ := startBridgeConfig(t, Config{NATSURL: url, Command: cmd, KillGrace: 500 * time.Millisecond,
		Scope: capability.NamespaceScope("")}, nil)
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-cli-closed", "long question")
	waitStarted(t, started)
	run := runOf(b, origin.TaskID)
	if s := b.nextSteer(run); s != nil { // the worker's decision, taken early
		t.Fatalf("nextSteer = %v on an empty queue", s)
	}
	late := sendSteer(t, c, origin, "too late")
	waitFor(t, 10*time.Second, "refusal", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	if n := steerNotices(t, url, origin.TaskID)[0]; n.Reason != lib.SteerReasonTaskEnding || n.EnvelopeID != late.EnvelopeID {
		t.Fatalf("notice = %+v, want refused task-ending", n)
	}
	_ = os.WriteFile(release, nil, 0o600)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted || task.PostFinalDropped != 0 {
		t.Fatalf("state %s post-final %d", task.State, task.PostFinalDropped)
	}
	if raw, _ := os.ReadFile(args); strings.Count(string(raw), "\n") != 1 {
		t.Fatalf("the refused follow-up ran: %s", raw)
	}
}

// A follow-up whose capability (the task's, carried on the steer) does not
// pass is refused capability when its turn comes and skipped; the next one
// runs.
func TestCLI_FollowUpWithoutAuthorityIsRefusedAndSkipped(t *testing.T) {
	_, url := startServer(t)
	cmd, started, release, args := resumeStub(t, 0, true)
	startBridge(t, url, cmd)
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-cli-capability", "long question")
	waitStarted(t, started)
	bare, err := lib.NewFollowUpEnvelope(origin, gatewayParty,
		messagePayload(t, origin.TaskID, origin.ContextID, "no authority"),
		lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", origin.TaskID), bare); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 10*time.Second, "bare queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	sendSteer(t, c, origin, "and west")
	waitFor(t, 10*time.Second, "two queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 2 })
	_ = os.WriteFile(release, nil, 0o600)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted || task.PostFinalDropped != 0 {
		t.Fatalf("state %s post-final %d", task.State, task.PostFinalDropped)
	}
	ns := steerNotices(t, url, origin.TaskID)
	if len(ns) != 3 || ns[2].Reason != lib.SteerReasonCapability || ns[2].EnvelopeID != bare.EnvelopeID || ns[2].state != lib.StateWorking {
		t.Fatalf("notices %+v, want the bare follow-up refused capability on a working status", ns)
	}
	raw, _ := os.ReadFile(args)
	if want := "--query=long question\n--resume sess-1 --query=and west\n"; string(raw) != want {
		t.Fatalf("argv per child:\n%s\nwant:\n%s", raw, want)
	}
	if got := artifactsNamed(t, url, origin.TaskID, lib.ArtifactResult); !slices.Equal(got, []string{"answer to and west\n"}) {
		t.Fatalf("result = %q", got)
	}
}

// idleRun is a running taskRun the bridge holds and no worker drives, with
// one follow-up queued, for driving cliTurn by hand.
func idleRun(t *testing.T, b *Bridge, taskID string) (*taskRun, *lib.Envelope) {
	t.Helper()
	origin, err := lib.NewMessageEnvelope(gatewayParty, taskID, "ctx-"+taskID, "corr-"+taskID,
		messagePayload(t, taskID, "ctx-"+taskID, "q"), lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	x, err := b.c.NewTaskExecution(origin, b.from, b.cfg.Profile)
	if err != nil {
		t.Fatal(err)
	}
	steer, err := lib.NewFollowUpEnvelope(origin, gatewayParty,
		messagePayload(t, taskID, origin.ContextID, "next"), lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	run := &taskRun{origin: origin, exec: x, state: stateRunning, steers: []*lib.Envelope{steer}}
	b.mu.Lock()
	b.tasks[taskID] = run
	b.mu.Unlock()
	return run, steer
}

// Ruling: a follow-up turn never spawns once the task is canceled, past its
// deadline (fired or not yet fired), or finalized; the follow-up it would
// have run is refused task-ended exactly once, before the terminal.
func TestCLI_FollowUpTurnDoesNotSpawnAfterTheTaskStops(t *testing.T) {
	_, url := startServer(t)
	marker := filepath.Join(t.TempDir(), "spawned")
	b, _ := startBridgeConfig(t, Config{NATSURL: url, Command: []string{"/bin/true", "-q"},
		TaskDeadline: 20 * time.Second, KillGrace: 500 * time.Millisecond, Scope: capability.NamespaceScope("")}, nil)
	argv := script(t, "touch "+marker)
	future := time.Now().Add(time.Hour)
	cases := []struct {
		name       string
		prepare    func(run *taskRun)
		deadlineAt time.Time
		state      lib.TaskState
		reason     string
	}{
		{"canceled", func(run *taskRun) { run.canceled.Store(true) }, future, lib.StateCanceled, "reason: canceled-by-request"},
		{"deadline-fired", func(run *taskRun) { run.deadlineHit.Store(true) }, future, lib.StateFailed, "reason: deadline-exceeded"},
		{"deadline-passed", func(*taskRun) {}, time.Now().Add(-time.Second), lib.StateFailed, "reason: deadline-exceeded"},
		{"finalized", func(run *taskRun) { b.finalize(run, lib.StateFailed, shutdownReason, nil) }, future, lib.StateFailed, shutdownReason},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			taskID := "task-cli-nospawn-" + tc.name
			run, steer := idleRun(t, b, taskID)
			if got := b.nextSteer(run); got != steer { // the worker's pick, as runnableSteer makes it
				t.Fatalf("nextSteer = %v, want the queued follow-up", got)
			}
			tc.prepare(run)
			if _, _, _, spawned := b.cliTurn(run, argv, nil, nil, steer, tc.deadlineAt); spawned {
				t.Fatal("a follow-up turn spawned after the task stopped")
			}
			if _, err := os.Stat(marker); err == nil {
				t.Fatal("the follow-up's child ran")
			}
			var final *lib.StatusUpdate
			for _, env := range replayEvents(t, url, taskID) {
				if lib.IsFinalStatus(env) {
					final = &lib.StatusUpdate{}
					_ = json.Unmarshal(env.Payload, final)
				}
			}
			if final == nil || final.Status.State != tc.state || final.Status.Message == nil ||
				!strings.HasPrefix(final.Status.Message.Parts[0].Text, tc.reason) {
				t.Fatalf("terminal %+v, want %s %q", final, tc.state, tc.reason)
			}
			ns := steerNotices(t, url, taskID)
			if len(ns) != 1 || ns[0].EnvelopeID != steer.EnvelopeID || ns[0].Reason != lib.SteerReasonTaskEnded ||
				ns[0].seq > finalSeq(t, url, taskID) {
				t.Fatalf("notices %+v, want the follow-up refused task-ended once, before the terminal", ns)
			}
		})
	}
	// Control: with nothing stopping it, the same call spawns and the
	// follow-up leaves the queue as its turn starts.
	run, steer := idleRun(t, b, "task-cli-nospawn-control")
	if _, _, err, spawned := b.cliTurn(run, argv, nil, nil, steer, future); !spawned || err != nil {
		t.Fatalf("control: spawned %v err %v", spawned, err)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatal("control: the child did not run")
	}
	run.mu.Lock()
	left := len(run.steers)
	run.mu.Unlock()
	if left != 0 {
		t.Fatalf("control: %d follow-ups still queued after the turn started", left)
	}
	b.finalize(run, lib.StateCompleted, "", nil)
}

// Ruling: the first turn checks the deadline before it spawns, as a
// follow-up's does. Its timer is armed before turn 1's spawn, so one that
// fires first finds no child to kill; a child spawned after it would run
// with nothing left to stop it.
func TestCLI_FirstTurnDoesNotSpawnPastTheDeadline(t *testing.T) {
	_, url := startServer(t)
	marker := filepath.Join(t.TempDir(), "spawned")
	b, _ := startBridgeConfig(t, Config{NATSURL: url, Command: []string{"/bin/true", "-q"},
		TaskDeadline: 20 * time.Second, KillGrace: 500 * time.Millisecond, Scope: capability.NamespaceScope("")}, nil)
	argv := script(t, "touch "+marker)
	future := time.Now().Add(time.Hour)
	cases := []struct {
		name       string
		prepare    func(run *taskRun)
		deadlineAt time.Time
	}{
		{"deadline-fired", func(run *taskRun) { run.deadlineHit.Store(true) }, future},
		{"deadline-passed", func(*taskRun) {}, time.Now().Add(-time.Second)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			taskID := "task-cli-first-nospawn-" + tc.name
			run, _ := idleRun(t, b, taskID)
			run.steers = nil // turn 1: nothing queued yet
			tc.prepare(run)
			if _, _, _, spawned := b.cliTurn(run, argv, nil, newActivityState(false), nil, tc.deadlineAt); spawned {
				t.Fatal("the first turn spawned past the deadline")
			}
			if _, err := os.Stat(marker); err == nil {
				t.Fatal("the first turn's child ran")
			}
			var final *lib.StatusUpdate
			for _, env := range replayEvents(t, url, taskID) {
				if lib.IsFinalStatus(env) {
					final = &lib.StatusUpdate{}
					_ = json.Unmarshal(env.Payload, final)
				}
			}
			if final == nil || final.Status.State != lib.StateFailed || final.Status.Message == nil ||
				!strings.HasPrefix(final.Status.Message.Parts[0].Text, "reason: deadline-exceeded") {
				t.Fatalf("terminal %+v, want failed deadline-exceeded", final)
			}
		})
	}
}
