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

// TestAFailedRecordReadGivesTheBatchBack: on a running gateway, a batch
// whose session record can't be read goes back to the stream after
// requeueDelay, delivery by delivery, and nothing is acked: the stream, not
// the process, holds what nobody rendered, and the hold on the durable ends.
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
