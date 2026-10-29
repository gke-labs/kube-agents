package gateway

import (
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// waitRunning waits for Run to have started (it sets runCtx first, then
// logs the feedback state), so a test touches nothing Run hasn't set.
func waitRunning(t *testing.T, r *rig) {
	t.Helper()
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
	select {
	case <-r.adapter.stopped:
	case <-time.After(10 * time.Second):
		t.Fatal("the gateway did not stop")
	}
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
