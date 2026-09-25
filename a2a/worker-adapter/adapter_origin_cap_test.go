package workeradapter

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestFetchOriginScanTakesASteerOnceTheCapEvictsTheSubmission pins the
// degradation the scan still has, and the reason the scan is no longer what a
// gateway-spawned worker uses.
//
// TASKS carries max_msgs_per_subject with discard=old, and that cap lands on a
// task's ...in subject as much as its ...events one. The oldest message on
// ...in is the originating kind:message, so a task that takes more inbound
// messages than the cap loses its submission first. The scan reads the subject
// from the beginning and takes the first kind:message it finds; steers are
// kind:message too and no envelope field marks the submission, so what it
// hands back after that eviction is a steer, and the worker runs against it.
// Nothing observes the swap - the events side has lib.Task.SubmittedMissing,
// this side has nothing, because nothing folds ...in.
//
// The scan cannot detect this itself: it would need the stream's first
// sequence (STREAM.INFO) or a get-by-subject, and a session's grants withhold
// both because neither is subject-scoped. So this stays true, and the fix is
// the test below - the spawner names the submission and the worker refuses a
// substitute. This case is what a run with no spawner to name it still gets.
func TestFetchOriginScanTakesASteerOnceTheCapEvictsTheSubmission(t *testing.T) {
	js, cleanup := capTasksStream(t, 1)
	defer cleanup()

	const session, taskID = "sess", "t1"
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	in := lib.TaskInSubject(session, taskID)
	publishMessage(ctx, t, js, in, "the originating request")
	publishMessage(ctx, t, js, in, "a steer sent later")

	a := &adapter{cfg: Config{Session: session, TaskID: taskID}, log: slog.Default(), js: js}
	origin, seq, err := a.fetchOrigin(ctx)
	if err != nil {
		t.Fatalf("fetchOrigin: %v", err)
	}
	if !strings.Contains(string(origin.Payload), "a steer sent later") {
		t.Fatalf("expected the surviving steer back as the origin, got seq=%d %s", seq, origin.Payload)
	}
	if strings.Contains(string(origin.Payload), "the originating request") {
		t.Fatalf("the submission should have been evicted by the cap, got %s", origin.Payload)
	}
}

// TestFetchOriginRefusesASteerWhenTheCapEvictedTheSubmission is the case
// above with the spawner's sequence supplied.
//
// It also measures the server behaviour the refusal is built on, which is the
// part that is easy to get wrong by assumption: nats-server does NOT reject a
// start sequence that has been evicted. The consumer is created happily and
// delivers the next message the filter matches, which here is the steer at
// sequence 2. So "the message is gone" cannot be read off an error and has to
// be read off the sequence that came back.
func TestFetchOriginRefusesASteerWhenTheCapEvictedTheSubmission(t *testing.T) {
	js, cleanup := capTasksStream(t, 1)
	defer cleanup()

	const session, taskID = "sess", "t1"
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	in := lib.TaskInSubject(session, taskID)
	publishMessage(ctx, t, js, in, "the originating request") // stream sequence 1
	publishMessage(ctx, t, js, in, "a steer sent later")      // sequence 2, evicts 1

	a := &adapter{cfg: Config{Session: session, TaskID: taskID, OriginSeq: 1, OriginSeqStated: true},
		log: slog.Default(), js: js}
	origin, seq, err := a.fetchOrigin(ctx)
	if err == nil {
		t.Fatalf("expected a refusal, got seq=%d %s", seq, origin.Payload)
	}
	// The message has to name what happened, because the operator reading it
	// has no other way to tell this from a bus outage.
	for _, want := range []string{"evicted", "sequence 1", in} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("refusal does not mention %q: %v", want, err)
		}
	}
}

// TestFetchOriginTakesTheNamedSequence is the happy path: the submission is
// still there and the named sequence is what comes back, ahead of the steer
// the scan would also have found first.
func TestFetchOriginTakesTheNamedSequence(t *testing.T) {
	js, cleanup := capTasksStream(t, 4096)
	defer cleanup()

	const session, taskID = "sess", "t1"
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	in := lib.TaskInSubject(session, taskID)
	publishMessage(ctx, t, js, in, "the originating request")
	publishMessage(ctx, t, js, in, "a steer sent later")

	a := &adapter{cfg: Config{Session: session, TaskID: taskID, OriginSeq: 1, OriginSeqStated: true},
		log: slog.Default(), js: js}
	origin, seq, err := a.fetchOrigin(ctx)
	if err != nil {
		t.Fatalf("fetchOrigin: %v", err)
	}
	if seq != 1 {
		t.Fatalf("expected stream sequence 1, got %d", seq)
	}
	if !strings.Contains(string(origin.Payload), "the originating request") {
		t.Fatalf("expected the submission, got %s", origin.Payload)
	}
}

// capTasksStream is the render's TASKS with max_msgs_per_subject dialled down
// to perSubject, so a handful of messages is "past the cap". Every other flag matches
// what platformagent_a2a_manifests.go creates.
func capTasksStream(t *testing.T, perSubject int64) (jetstream.JetStream, func()) {
	t.Helper()
	opts := &natsserver.Options{Host: "127.0.0.1", Port: -1, JetStream: true,
		StoreDir: t.TempDir(), NoLog: true, NoSigs: true}
	s, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	nc, err := nats.Connect(fmt.Sprintf("nats://%s", s.Addr().String()))
	if err != nil {
		s.Shutdown()
		t.Fatalf("connect: %v", err)
	}
	js, err := jetstream.New(nc)
	if err != nil {
		nc.Close()
		s.Shutdown()
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{
		Name: lib.TasksStream, Subjects: []string{"a2a.tasks.>"},
		Retention: jetstream.LimitsPolicy, Discard: jetstream.DiscardOld,
		MaxAge: 72 * time.Hour, MaxMsgsPerSubject: perSubject, AllowDirect: true,
	}); err != nil {
		nc.Close()
		s.Shutdown()
		t.Fatalf("create TASKS: %v", err)
	}
	return js, func() { nc.Close(); s.Shutdown() }
}

func publishMessage(ctx context.Context, t *testing.T, js jetstream.JetStream, subject, text string) {
	t.Helper()
	payload, err := json.Marshal(map[string]any{
		"role":      "user",
		"messageId": text,
		"parts":     []map[string]string{{"kind": "text", "text": text}},
	})
	if err != nil {
		t.Fatalf("payload: %v", err)
	}
	env, err := lib.NewMessageEnvelope(
		lib.Party{Session: "requester", AgentType: "agent"}, "t1", "ctx-1", "corr-1", payload)
	if err != nil {
		t.Fatalf("envelope: %v", err)
	}
	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if _, err := js.Publish(ctx, subject, raw); err != nil {
		t.Fatalf("publish %s: %v", subject, err)
	}
}
