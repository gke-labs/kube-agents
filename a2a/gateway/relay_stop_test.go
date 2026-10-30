package gateway

import (
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// waitRunning waits until Run has bound its relay durable on the server,
// which it does after setting runCtx and before it starts the adapter: a test
// that then touches runCtx reads what Run set, and a stop that follows
// reaches a gateway past its bind, so Run gets to the adapter and returns
// through it.
func waitRunning(t *testing.T, r *rig) {
	t.Helper()
	nc, err := nats.Connect(r.url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the gateway's relay bound", func() bool {
		_, err := js.Consumer(context.Background(), lib.TasksStream, r.g.relayDurable)
		return err == nil
	})
	// Bound, and then Run starts the adapter: wait for that too, so a stop
	// is answered by the adapter returning.
	waitFor(t, "the gateway running", func() bool { return strings.Contains(r.logs.String(), "answer feedback") })
}

func stoppedBatch() []relayItem {
	return []relayItem{{env: &lib.Envelope{TaskID: "task-x"}, subject: "a2a.tasks.platform.task-x.events"}}
}

// TestAStoppedGatewayDropsABatchWithOneLine: once the gateway has stopped, a
// batch is dropped with one line before any read, not requeued every
// requeueDelay for the life of the process.
func TestAStoppedGatewayDropsABatchWithOneLine(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	waitRunning(t, r)
	r.stop()
	// Run wrote runCtx once, before the log line waitRunning read under the
	// buffer's lock, so reading it here doesn't race that write.
	waitFor(t, "the gateway stopped", func() bool { return r.g.runCtx.Err() != nil })
	start := time.Now()
	r.g.relayBatch("discord:g1/t-stopped", stoppedBatch())
	if took := time.Since(start); took > time.Second {
		t.Fatalf("the drop took %v: it ran the record read's retries first", took)
	}
	time.Sleep(requeueDelay + time.Second)
	text := r.logs.String()
	if n := strings.Count(text, "batch not rendered"); n != 1 {
		t.Fatalf("stop line logged %d times, want once:\n%s", n, text)
	}
	if strings.Contains(text, "requeueing batch") {
		t.Fatalf("a stopped gateway requeued its batch:\n%s", text)
	}
}

// TestAFailedRecordReadGivesTheBatchBack: on a running gateway, a batch
// whose session record can't be read goes back to the stream after
// requeueDelay, delivery by delivery, and nothing is acked: the stream, not
// the process, holds what nobody rendered.
func TestAFailedRecordReadGivesTheBatchBack(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	waitRunning(t, r)
	// The gateway is running but its client is gone, so the read fails.
	r.client.Close()
	d := &nakRecorder{fakeDelivery: fakeDelivery{env: &lib.Envelope{TaskID: "task-x"}, subj: "a2a.tasks.platform.task-x.events", seq: 5}}
	r.g.relayBatch("discord:g1/t-giveback", []relayItem{{env: d.env, subject: d.subj, d: d}})
	if !strings.Contains(r.logs.String(), "giving the batch back") {
		t.Fatalf("the failed read didn't give the batch back:\n%s", r.logs.String())
	}
	if d.acks != 0 || len(d.naks) != 1 || d.naks[0] != requeueDelay {
		t.Fatalf("acks %d, naks %v: want one nak with requeueDelay and no ack", d.acks, d.naks)
	}
}

// nakRecorder is a fakeDelivery that records each Nak's delay.
type nakRecorder struct {
	fakeDelivery
	naks []time.Duration
}

func (n *nakRecorder) Nak(delay time.Duration) error {
	n.naks = append(n.naks, delay)
	return nil
}

// TestAFailedTaskLookupGivesTheEventBack: an event whose task index can't be
// read isn't taken for another requester's and acked away; it goes back to
// the stream for a later try.
func TestAFailedTaskLookupGivesTheEventBack(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	waitRunning(t, r)
	r.client.Close() // the index read fails
	env := &lib.Envelope{Kind: lib.KindStatusUpdate, TaskID: "task-unindexed"}
	d := &nakRecorder{fakeDelivery: fakeDelivery{env: env, subj: "a2a.tasks.platform.task-unindexed.events", seq: 9}}
	r.g.relayDelivery(r.g.runCtx, d)
	if d.acks != 0 || len(d.naks) != 1 || d.naks[0] != requeueDelay {
		t.Fatalf("acks %d, naks %v: want one nak with requeueDelay and no ack", d.acks, d.naks)
	}
}

// closeRecorder is the rig's adapter with a Close that records whether the
// relay's stop drain had already begun when the gateway closed it.
type closeRecorder struct {
	*fakeAdapter
	g          **Gateway
	closed     chan struct{}
	afterDrain bool
}

func (c *closeRecorder) Close() {
	f := &(*c.g).relayInFlight
	f.mu.Lock()
	c.afterDrain = f.stopping
	f.mu.Unlock()
	close(c.closed)
}

// TestTheGatewayClosesItsAdaptersAfterTheRelayDrain: a batch rendering when
// the stop begins posts through the adapters, so they close only once the
// drain is done, not when ingress stops.
func TestTheGatewayClosesItsAdaptersAfterTheRelayDrain(t *testing.T) {
	var g *Gateway
	rec := &closeRecorder{g: &g, closed: make(chan struct{})}
	r, _ := startRigWithSpawnerAdapter(t, "platform", 0, nil, func(f *fakeAdapter) Adapter {
		rec.fakeAdapter = f
		return rec
	})
	g = r.g
	waitRunning(t, r)
	r.stop()
	select {
	case <-rec.closed:
	case <-time.After(10 * time.Second):
		t.Fatal("the gateway never closed its adapter")
	}
	if !rec.afterDrain {
		t.Fatal("the adapter closed before the relay's stop drain began")
	}
}

// TestADeliveryDuringTheStopIsLeftForTheGiveBack: once Run's ctx is
// cancelled, a delivery the relay receives is neither acked nor nak'd with a
// delay, so the subscription's stop gives it back in stream order.
func TestADeliveryDuringTheStopIsLeftForTheGiveBack(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	waitRunning(t, r)
	r.stop()
	waitFor(t, "the gateway stopped", func() bool { return r.g.runCtx.Err() != nil })
	env := &lib.Envelope{Kind: lib.KindStatusUpdate, TaskID: "task-during-stop"}
	d := &nakRecorder{fakeDelivery: fakeDelivery{env: env, subj: "a2a.tasks.platform.task-during-stop.events", seq: 11}}
	r.g.relayDelivery(r.g.runCtx, d)
	if d.acks != 0 || len(d.naks) != 0 {
		t.Fatalf("acks %d, naks %v: want the delivery left unsettled for the give-back", d.acks, d.naks)
	}
	if strings.Contains(r.logs.String(), "task index lookup failed") {
		t.Fatalf("the stop read the task index on a cancelled ctx and logged it as a failure:\n%s", r.logs.String())
	}
	r.g.giveBackLater(relayItem{env: env, subject: d.subj, d: d})
	if len(d.naks) != 0 {
		t.Fatalf("giveBackLater nak'd during the stop: %v", d.naks)
	}
}

// TestADrainThatRunsOutMarksTheBatchesStillRenderingAbandoned: only a wait
// that ran out with a batch inside sets abandoned; one that finished, or had
// nothing to wait for, doesn't.
func TestADrainThatRunsOutMarksTheBatchesStillRenderingAbandoned(t *testing.T) {
	var idle inFlight
	idle.drain(time.Millisecond)
	if idle.abandoned() {
		t.Fatal("a drain with nothing rendering marked a batch abandoned")
	}

	var finished inFlight
	if !finished.enter() {
		t.Fatal("enter refused before any drain")
	}
	go func() {
		time.Sleep(10 * time.Millisecond)
		finished.leave()
	}()
	finished.drain(5 * time.Second)
	if finished.abandoned() {
		t.Fatal("a drain the batch finished inside marked it abandoned")
	}

	var overrun inFlight
	if !overrun.enter() {
		t.Fatal("enter refused before any drain")
	}
	overrun.drain(10 * time.Millisecond)
	if !overrun.abandoned() {
		t.Fatal("a drain that ran out on a rendering batch didn't mark it abandoned")
	}
	overrun.leave()
}

// redelivered is a fakeDelivery the server has sent before.
type redelivered struct{ fakeDelivery }

func (r *redelivered) NumDelivered() uint64 { return 2 }

// TestAGiveBackIsAnErrorOnlyOnTheFirstDelivery: during a KV outage every
// held event comes back every requeueDelay, so only its first failure logs
// at Error.
func TestAGiveBackIsAnErrorOnlyOnTheFirstDelivery(t *testing.T) {
	first := &fakeDelivery{env: &lib.Envelope{TaskID: "t"}, subj: "s", seq: 1}
	again := &redelivered{fakeDelivery: fakeDelivery{env: &lib.Envelope{TaskID: "t"}, subj: "s", seq: 1}}
	if got := firstDeliveryLevel(relayItem{env: first.env, d: first}); got != slog.LevelError {
		t.Errorf("first delivery logs at %v, want Error", got)
	}
	if got := firstDeliveryLevel(relayItem{env: again.env, d: again}); got != slog.LevelDebug {
		t.Errorf("a redelivery logs at %v, want Debug", got)
	}
	if got := firstDeliveryLevel(relayItem{env: first.env}); got != slog.LevelError {
		t.Errorf("an item off no durable logs at %v, want Error", got)
	}
}
