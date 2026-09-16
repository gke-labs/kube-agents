package authcallout

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// The real worker adapter, running a whole task, under the real callout, with
// nothing but its own session's grants.
//
// The grant tests next door assert subject by subject that a session may do
// what the adapter needs. That is an enumeration of what somebody believed the
// adapter does. This runs the adapter — the actual Run loop, an actual harness
// subprocess, actual named consumers, an actual terminal event — against a
// server that will refuse anything the enumeration got wrong. The two failure
// directions it catches are the ones a grant list cannot:
//
//   - a grant that is missing, which no negative test finds because nobody
//     thought to assert the thing the adapter quietly needs;
//   - a grant that is present but unusable, because the client library asks
//     for it in a shape the permission does not match. That is not
//     hypothetical here: ordered consumers were exactly this, granted in
//     spirit and unnameable in fact.
//
// It lives in this package rather than in worker-adapter because the harness
// is here: the real rendered nats.conf, the real map, the real callout. A
// second copy over there would be a second thing to keep in step.
func TestASessionAdapterRunsAWholeTaskUnderItsOwnGrants(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	provisionTasksStream(t, h)
	const taskID = "task-e2e-1"
	submitAs(t, h, podA, taskID, "do the thing")

	res, err := workeradapter.Run(ctx, workeradapter.Config{
		NATSURL:      h.url,
		BusTokenFile: tokenFile(t, tokenPodA),
		PodName:      podA,
		TaskID:       taskID,
		Profile:      "chat",
		Session:      podA,
		HarnessCommand: harnessStub(t, `
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"working on it"}]}}'
echo '{"type":"result","subtype":"success","result":"the thing is done"}'
`),
		HarnessEnv:   os.Environ(),
		TaskDeadline: 30 * time.Second,
		KillGrace:    time.Second,
	})
	if err != nil {
		t.Fatalf("the adapter could not complete a task under its own session grants: %v", err)
	}
	if res.State != lib.StateCompleted {
		t.Fatalf("terminal state = %q, want completed", res.State)
	}

	// And the events actually landed on the bus, read back by a principal
	// that is allowed to look. Run returning completed only says the adapter
	// believes it published.
	events := readEvents(t, h, podA, taskID)
	if len(events) == 0 {
		t.Fatal("no events on the session's own subject; the adapter reported success and published nothing")
	}
	var sawFinal bool
	for _, e := range events {
		if e.Kind == lib.KindStatusUpdate && strings.Contains(string(e.Payload), `"final":true`) {
			sawFinal = true
		}
	}
	if !sawFinal {
		t.Error("no final status event on the session's own subject")
	}

	// The consumers it created are named the way the grant expects, which is
	// the contract lib.SessionConsumerName exists to hold. A library that
	// quietly renamed them would still pass the run above only until the
	// grants tightened.
	names := consumerNames(t, h)
	for _, role := range lib.SessionConsumerRoles {
		want := lib.SessionConsumerName(podA, role)
		if role == lib.SessionConsumerEvents {
			// Deleted as soon as the respawn check is done, so it is
			// legitimately absent by now.
			continue
		}
		if !containsString(names, want) {
			t.Errorf("no consumer named %q on TASKS; found %v", want, names)
		}
	}
	for _, n := range names {
		if !strings.HasPrefix(n, podA+"-") && n != relayDurable && n != "test-reader" {
			t.Errorf("consumer %q is not this session's, the gateway's relay, or the provisioner's; a session created something outside its naming contract", n)
		}
	}
}

// The same adapter, the same server, one thing different: the token belongs to
// the pod next door. Everything the run above did is refused, and it is refused
// in a way the operator can read.
//
// This is the demonstration gke-labs#1270 asks for, in test form. A credential
// lifted out of one session pod — by /proc/1/environ or by reading the token
// file, both of which the harness can do — buys nothing in another session.
func TestASessionAdapterCannotRunAnotherSessionsTask(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	provisionTasksStream(t, h)
	const taskID = "task-e2e-2"
	// The task belongs to podA, and podA's pod is where it would run.
	submitAs(t, h, podA, taskID, "do the thing")

	// The adapter's own log, captured, because half of what this test is
	// checking is whether an operator could tell what happened. A refused
	// JetStream publish gets no reply, so the call waits out its context and
	// reports a deadline — the reason arrives separately, on the async error
	// handler, and if it were not logged there it would arrive nowhere.
	var logs safeBuffer
	logger := slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug}))

	// podB's credential, pointed at podA's task. The adapter is told it is
	// podA — Session, TaskID and addressee all say so — and only the token
	// disagrees, because the token is the only thing it cannot choose.
	_, err := workeradapter.Run(ctx, workeradapter.Config{
		Logger:         logger,
		NATSURL:        h.url,
		BusTokenFile:   tokenFile(t, tokenPodB),
		PodName:        podA,
		TaskID:         taskID,
		Profile:        "chat",
		Session:        podA,
		HarnessCommand: harnessStub(t, `echo '{"type":"result","subtype":"success","result":"stolen"}'`),
		HarnessEnv:     os.Environ(),
		TaskDeadline:   15 * time.Second,
		KillGrace:      time.Second,
	})
	if err == nil {
		t.Fatal("an adapter holding another session's token ran the task to completion")
	}
	t.Logf("refused, as it should be: %v", err)

	// The deadline error alone names the wrong problem. The log has to carry
	// the real one, and name the subject that was refused.
	out := logs.String()
	if !strings.Contains(out, "the bus refused this session") {
		t.Errorf("the refusal never reached the log; an operator would see only a timeout.\n%s", out)
	}
	if !strings.Contains(out, "Permissions Violation") {
		t.Errorf("the log does not say the refusal was a permissions violation.\n%s", out)
	}

	// Nothing of podA's was written. A refusal that still leaked a
	// submitted event would corrupt the real session's lifecycle.
	if events := readEvents(t, h, podA, taskID); len(events) != 0 {
		t.Errorf("%d events reached podA's subject from a pod holding podB's credential", len(events))
	}
}

// A pod whose A2A_SESSION and A2A_POD_NAME disagree fails at once, with both
// names in the message.
//
// The two are equal by construction — the gateway names the pod after the bus
// session — so a disagreement means a spawner bug or a hand-edited pod. Left
// to run, the adapter would authenticate perfectly well, pin its inbox to the
// wrong name and then hang on the first JetStream call, which reads like a bus
// outage.
func TestAnAdapterRefusesAPodNameAndSessionThatDisagree(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	_, err := workeradapter.Run(ctx, workeradapter.Config{
		NATSURL:        h.url,
		BusTokenFile:   tokenFile(t, tokenPodA),
		PodName:        podA,
		TaskID:         "task-e2e-3",
		Profile:        "chat",
		Session:        podB,
		HarnessCommand: harnessStub(t, `echo '{"type":"result","subtype":"success","result":"x"}'`),
		HarnessEnv:     os.Environ(),
		TaskDeadline:   10 * time.Second,
	})
	if err == nil {
		t.Fatal("the adapter started with a pod name and a session name that disagree")
	}
	if !strings.Contains(err.Error(), podA) || !strings.Contains(err.Error(), podB) {
		t.Errorf("the error names neither side of the disagreement: %v", err)
	}
}

// The quiet half of the case above. An unset A2A_SESSION is not a disagreement
// the eye catches: Addressee falls back to Profile, so the adapter publishes as
// `chat` while the callout derived its grants from the pod. Every publish is
// refused and no reply ever arrives, which is the failure mode this check
// exists to convert into a startup error.
func TestAnAdapterRefusesAPodNameWithNoSessionAtAll(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	_, err := workeradapter.Run(ctx, workeradapter.Config{
		NATSURL:        h.url,
		BusTokenFile:   tokenFile(t, tokenPodA),
		PodName:        podA,
		TaskID:         "task-e2e-4",
		Profile:        "chat",
		HarnessCommand: harnessStub(t, `echo '{"type":"result","subtype":"success","result":"x"}'`),
		HarnessEnv:     os.Environ(),
		TaskDeadline:   10 * time.Second,
	})
	if err == nil {
		t.Fatal("the adapter started with a bus token and no session name, so it would have published as its profile")
	}
	// The addressee it would have used, not the empty string: the error has to
	// name the wrong thing it was about to be.
	if !strings.Contains(err.Error(), podA) || !strings.Contains(err.Error(), "chat") {
		t.Errorf("the error names neither the pod it is bound to nor the addressee it would have used: %v", err)
	}
}

// --- helpers ---------------------------------------------------------------

// safeBuffer is a bytes.Buffer the async error handler and the test goroutine
// can both touch. Without the mutex this test is a data race that passes.
type safeBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *safeBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *safeBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// tokenFile writes a token where the adapter's own file-reading path will find
// it, rather than handing the adapter a string. The rotation re-read is part of
// what is under test in the run above.
func tokenFile(t *testing.T, token string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(p, []byte(token), 0o600); err != nil {
		t.Fatalf("writing the token file: %v", err)
	}
	return p
}

func harnessStub(t *testing.T, body string) []string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "stub.sh")
	if err := os.WriteFile(p, []byte("#!/bin/bash\n"+body+"\n"), 0o755); err != nil {
		t.Fatalf("writing the harness stub: %v", err)
	}
	return []string{"/bin/bash", p}
}

// gatewayConn is the privileged side of these tests: it provisions the stream
// and submits the task, exactly as the gateway does in the deployment. It is a
// mapped principal with broad grants, which is the point — the session under
// test is the constrained one.
func gatewayConn(t *testing.T, h *harness) *nats.Conn {
	t.Helper()
	nc, _ := h.connectAs(t, "gateway", gatewayToken)
	return nc
}

func provisionTasksStream(t *testing.T, h *harness) {
	t.Helper()
	js, err := jetstream.New(gatewayConn(t, h))
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{
		Name:      lib.TasksStream,
		Subjects:  []string{"a2a.tasks.>"},
		Retention: jetstream.LimitsPolicy,
		MaxAge:    72 * time.Hour,
	}); err != nil {
		t.Fatalf("provisioning TASKS: %v", err)
	}
}

func submitAs(t *testing.T, h *harness, addressee, taskID, text string) {
	t.Helper()
	payload, err := json.Marshal(lib.Message{
		Role: "user", Parts: []lib.Part{{Kind: "text", Text: text}},
		MessageID: "msg-" + taskID, TaskID: taskID, ContextID: "ctx-" + taskID,
	})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	env, err := lib.NewMessageEnvelope(
		lib.Party{Session: "gateway", AgentType: "a2a-gateway"},
		taskID, "ctx-"+taskID, "corr-"+taskID, payload,
		lib.WithTo(lib.Party{Session: addressee}))
	if err != nil {
		t.Fatalf("submission envelope: %v", err)
	}
	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatalf("marshal envelope: %v", err)
	}
	nc := gatewayConn(t, h)
	if err := nc.Publish(lib.TaskInSubject(addressee, taskID), raw); err != nil {
		t.Fatalf("publishing the submission: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("flush: %v", err)
	}
}

// readEvents drains a task's events subject as the gateway, which may read
// what the session may only write.
func readEvents(t *testing.T, h *harness, addressee, taskID string) []*lib.Envelope {
	t.Helper()
	js, err := jetstream.New(gatewayConn(t, h))
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	cons, err := js.CreateOrUpdateConsumer(ctx, lib.TasksStream, jetstream.ConsumerConfig{
		// Named, so the consumer-naming assertion above can tell the test's
		// own reader from something the session created.
		Name:          "test-reader",
		FilterSubject: lib.TaskEventsSubject(addressee, taskID),
		DeliverPolicy: jetstream.DeliverAllPolicy,
		AckPolicy:     jetstream.AckNonePolicy,
	})
	if err != nil {
		t.Fatalf("reader consumer: %v", err)
	}
	batch, err := cons.FetchNoWait(256)
	if err != nil {
		t.Fatalf("fetch: %v", err)
	}
	var out []*lib.Envelope
	for msg := range batch.Messages() {
		env, err := lib.ParseEnvelope(msg.Data())
		if err != nil {
			t.Errorf("unparseable event on the bus: %v", err)
			continue
		}
		out = append(out, env)
	}
	return out
}

func consumerNames(t *testing.T, h *harness) []string {
	t.Helper()
	js, err := jetstream.New(gatewayConn(t, h))
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	stream, err := js.Stream(ctx, lib.TasksStream)
	if err != nil {
		t.Fatalf("stream: %v", err)
	}
	var names []string
	lister := stream.ConsumerNames(ctx)
	for name := range lister.Name() {
		names = append(names, name)
	}
	if err := lister.Err(); err != nil {
		t.Fatalf("listing consumers: %v", err)
	}
	return names
}
