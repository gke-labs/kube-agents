package gateway

import (
	"context"
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

// TestARequeueWaitingWhenTheGatewayStopsIsDroppedWithOneLine: a batch whose
// record read failed on a running gateway is requeued; if the gateway stops
// while the requeue waits, the batch is dropped with the stop line instead of
// enqueued, so the log says what happened to it.
func TestARequeueWaitingWhenTheGatewayStopsIsDroppedWithOneLine(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	waitRunning(t, r)
	// The gateway is running but its client is gone, so the read fails and
	// the batch is requeued.
	r.client.Close()
	r.g.relayBatch("discord:g1/t-requeue", stoppedBatch())
	if !strings.Contains(r.logs.String(), "requeueing batch") {
		t.Fatalf("the failed read didn't requeue:\n%s", r.logs.String())
	}
	r.stop()
	waitFor(t, "the waiting requeue's drop line", func() bool { return strings.Contains(r.logs.String(), "batch not rendered") })
	time.Sleep(requeueDelay + time.Second)
	text := r.logs.String()
	if n := strings.Count(text, "requeueing batch"); n != 1 {
		t.Fatalf("requeued %d times, want once before the stop:\n%s", n, text)
	}
	if n := strings.Count(text, "batch not rendered"); n != 1 {
		t.Fatalf("stop line logged %d times, want once:\n%s", n, text)
	}
}
