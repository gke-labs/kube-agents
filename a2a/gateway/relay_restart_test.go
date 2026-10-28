package gateway

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// relayAckPending is how many deliveries on the relay durable are held
// unsettled: taken by a gateway and not yet acked.
func relayAckPending(t *testing.T, url, durable string) int {
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
	c, err := js.Consumer(context.Background(), lib.TasksStream, durable)
	if err != nil {
		t.Fatal(err)
	}
	info, err := c.Info(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return info.NumAckPending
}

func postsContaining(r *rig, needle string) int {
	n := 0
	for _, p := range append(r.adapter.postTexts(), r.adapter.editTexts()...) {
		if strings.Contains(p, needle) {
			n++
		}
	}
	return n
}

// TestAGatewayStoppedMidBatchLosesAndRepeatsNothing: a gateway that has taken
// a task's result and terminal off the relay durable, and stops before it
// renders them, gives them back; the next gateway renders each exactly once,
// and the stopped one rendered none. Before the relay acked after render,
// both events were acked on enqueue and lost.
func TestAGatewayStoppedMidBatchLosesAndRepeatsNothing(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	conv := "discord:g1/t-mid-batch"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-mid", Text: "how is the fleet?"}
	origin := r.awaitTask(t, targetPlatform)
	exec := r.execFor(t, origin, targetPlatform)
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the working status rendered and acked", func() bool { return relayAckPending(t, r.url, r.g.relayDurable) == 0 })

	// Hold the conversation's lock, as a slow render would: the relay takes
	// the next events off the durable and queues them, but can't render.
	l := r.g.lockSession(conv)
	l.Lock()
	const answer = "the fleet is healthy: 12 clusters"
	completeTask(t, exec, answer)
	waitFor(t, "the result and the terminal taken and held", func() bool { return relayAckPending(t, r.url, r.g.relayDurable) == 2 })

	// Stop the gateway with both still queued; release its worker only once
	// the second gateway is up, so its render can't race the restart.
	r.stop()
	select {
	case <-r.ran:
	case <-time.After(10 * time.Second):
		t.Fatal("the first gateway did not stop")
	}
	r2, _ := restartRig(t, r)
	l.Unlock()

	waitFor(t, "the answer on the new gateway", func() bool { return postsContaining(r2, answer) > 0 })
	waitFor(t, "the terminal on the new gateway", func() bool {
		rec, _ := r2.g.reg.Get(context.Background(), conv)
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && len(ref.RelayedSeqs) >= 3
	})
	// Settle: nothing more should arrive.
	time.Sleep(time.Second)
	if n := postsContaining(r2, answer); n != 1 {
		t.Fatalf("the answer was posted %d times on the new gateway, want once: %q", n, r2.adapter.postTexts())
	}
	if n := postsContaining(r, answer); n != 0 {
		t.Fatalf("the stopped gateway posted the answer %d times", n)
	}
	if n := relayAckPending(t, r.url, r.g.relayDurable); n != 0 {
		t.Fatalf("%d deliveries still held after the new gateway rendered them", n)
	}
}

// TestARedeliveredEventAlreadyRenderedIsNotPostedAgain: an event whose stream
// sequence is on the record was rendered by a gateway that died before its
// ack, and is acked without posting again.
func TestARedeliveredEventAlreadyRenderedIsNotPostedAgain(t *testing.T) {
	rec := &SessionRecord{Tasks: []TaskRef{{ID: "task-1"}}}
	rec.markRelayed("task-1", 7)
	if !rec.relayed("task-1", 7) {
		t.Fatal("a rendered sequence isn't recognised")
	}
	if rec.relayed("task-1", 8) || rec.relayed("task-2", 7) {
		t.Fatal("an unrendered sequence, or another task's, was taken as rendered")
	}
	if rec.relayed("task-1", 0) {
		t.Fatal("a delivery without a sequence was taken as rendered")
	}
	for seq := uint64(100); seq < 100+relayedSeqCap+5; seq++ {
		rec.markRelayed("task-1", seq)
	}
	if got := len(rec.Tasks[0].RelayedSeqs); got != relayedSeqCap {
		t.Fatalf("kept %d sequences, want the cap %d", got, relayedSeqCap)
	}
	if !rec.relayed("task-1", 100+relayedSeqCap+4) || rec.relayed("task-1", 7) {
		t.Fatal("the cap kept the oldest sequences instead of the newest")
	}
}

// fakeDelivery is a DeferredDelivery with a fixed sequence that counts its
// acks, for driving relayBatch directly.
type fakeDelivery struct {
	env  *lib.Envelope
	subj string
	seq  uint64
	acks int
}

func (f *fakeDelivery) Envelope() *lib.Envelope { return f.env }
func (f *fakeDelivery) Subject() string         { return f.subj }
func (f *fakeDelivery) Received() time.Time     { return time.Time{} }
func (f *fakeDelivery) NumDelivered() uint64    { return 1 }
func (f *fakeDelivery) Sequence() uint64        { return f.seq }
func (f *fakeDelivery) Ack() error              { f.acks++; return nil }
func (f *fakeDelivery) Nak(time.Duration) error { return nil }
func (f *fakeDelivery) Term() error             { return nil }

// TestTheRelayRendersADeliveryItAlreadyRenderedOnce: a question a task
// posts while it runs, redelivered to the next gateway after the first one
// rendered it and died before its ack, is acked there without being posted
// again. The record's sequences are all that says it was rendered: the next
// gateway's in-memory relay state is empty, and the task is still live.
func TestTheRelayRendersADeliveryItAlreadyRenderedOnce(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	conv := "discord:g1/t-redelivered"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-red", Text: "how is the fleet?"}
	origin := r.awaitTask(t, targetPlatform)
	const question = "which cluster did you mean, 42?"
	payload, err := json.Marshal(lib.StatusUpdate{Status: lib.TaskStatus{State: lib.StateInputRequired,
		Message: &lib.Message{Role: "agent", Parts: []lib.Part{{Kind: "text", Text: question}}}}})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(lib.Party{Session: targetPlatform}, origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	subject := lib.TaskEventsSubject(targetPlatform, origin.TaskID)
	d := &fakeDelivery{env: env, subj: subject, seq: 999}
	r.g.relayBatch(conv, []relayItem{{env: env, subject: subject, d: d}})
	if postsContaining(r, question) != 1 {
		t.Fatalf("the question wasn't posted the first time: %q", r.adapter.postTexts())
	}

	r2, _ := restartRig(t, r)
	r2.g.relayBatch(conv, []relayItem{{env: env, subject: subject, d: d}})
	if n := postsContaining(r2, question); n != 0 {
		t.Fatalf("the redelivered question was posted again on the next gateway (%d times)", n)
	}
	if d.acks != 2 {
		t.Fatalf("acked %d times, want both deliveries acked", d.acks)
	}
}
