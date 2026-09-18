package gateway

import (
	"context"
	"strconv"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestStartTaskSpawnsWithThePublishedOriginSequence closes the gap between
// the two halves of this fix. TestSpawnCarriesTheOriginSequence proves the
// spawner renders whatever sequence it is handed, and the worker-adapter
// tests prove fetchOrigin refuses a substitute when it is handed a real one.
// Neither says the number travelling between them is the submission's. Both
// pass against a startTask that discards the PubAck and spawns with 0 -
// measured: dropping the sequence on the floor in startTask and passing 0
// leaves the whole gateway suite green, because 0 is the legal "could not
// tell" sentinel and every pod then silently takes the unverified scan.
//
// So this asserts against the stream rather than against a literal: whatever
// the spawner received must be the stream sequence of a real message, and
// that message must be this task's submission.
func TestStartTaskSpawnsWithThePublishedOriginSequence(t *testing.T) {
	r, spawn := startRigWithSpawner(t)

	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread-origin-seq", Kind: "group",
		AuthorID: "1001", MessageID: "c-900", Text: "Delegate: write a haiku",
	}
	waitFor(t, "the delegation spawned", func() bool { return len(spawn.calls()) == 1 })
	call := spawn.calls()[0]

	if call.OriginSeq == 0 {
		t.Fatalf("spawned with the %q sentinel on a task whose submission the gateway had just published and acked; the worker will scan, which is what this fix exists to stop", lib.OriginSeqUnknown)
	}

	nc, err := nats.Connect(r.url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	stream, err := js.Stream(ctx, lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	msg, err := stream.GetMsg(ctx, call.OriginSeq)
	if err != nil {
		t.Fatalf("nothing on %s at stream sequence %d, the sequence the spawner was given: %v", lib.TasksStream, call.OriginSeq, err)
	}
	want := lib.TaskInSubject(call.Session, call.TaskID)
	if msg.Subject != want {
		t.Fatalf("sequence %d is on %s, not this task's in subject %s", call.OriginSeq, msg.Subject, want)
	}
	env, err := lib.ParseEnvelope(msg.Data)
	if err != nil {
		t.Fatalf("sequence %d does not parse as an envelope: %v", call.OriginSeq, err)
	}
	if env.Kind != lib.KindMessage {
		t.Fatalf("sequence %d is kind %q, want the submission's %q", call.OriginSeq, env.Kind, lib.KindMessage)
	}
	if env.TaskID != call.TaskID {
		t.Fatalf("sequence %d carries task %q, but the pod was spawned for %q", call.OriginSeq, env.TaskID, call.TaskID)
	}

	// And it is the head of the subject, not merely a message on it - the
	// submission is what a restarted worker must re-read, so a sequence
	// pointing at a later steer would satisfy everything above and still be
	// the bug.
	first := firstSeqOnSubject(t, js, want)
	if call.OriginSeq != first {
		t.Fatalf("spawned with sequence %d, but the first message on %s is %d; that is a follow-up, not the submission", call.OriginSeq, want, first)
	}

	// The rendered pod env is the same number, spelled.
	if got := originSeqValue(call.OriginSeq); got != strconv.FormatUint(first, 10) {
		t.Fatalf("pod env would read %q for sequence %d", got, first)
	}
}

// firstSeqOnSubject returns the stream sequence of the oldest message on a
// subject, which on a task's in subject is its submission.
func firstSeqOnSubject(t *testing.T, js jetstream.JetStream, subject string) uint64 {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cons, err := js.OrderedConsumer(ctx, lib.TasksStream, jetstream.OrderedConsumerConfig{
		FilterSubjects: []string{subject},
	})
	if err != nil {
		t.Fatal(err)
	}
	batch, err := cons.Fetch(1)
	if err != nil {
		t.Fatal(err)
	}
	for msg := range batch.Messages() {
		meta, err := msg.Metadata()
		if err != nil {
			t.Fatal(err)
		}
		return meta.Sequence.Stream
	}
	t.Fatalf("no message at all on %s", subject)
	return 0
}
