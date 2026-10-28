package gateway

import (
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestAStoppedGatewayDoesNotRequeueAFailedBatch: once the gateway's run
// context is done, a batch whose session record can't be read is dropped
// with one line, not requeued every requeueDelay for the life of the process.
func TestAStoppedGatewayDoesNotRequeueAFailedBatch(t *testing.T) {
	logs := &lockedBuffer{}
	r := startRigWithLogger(t, nil, slog.New(slog.NewTextHandler(logs, nil)))
	// Run sets runCtx as it starts; swap in a done one only after that.
	waitFor(t, "the gateway running", func() bool { return strings.Contains(logs.String(), "answer feedback") })
	stopped, cancel := context.WithCancel(context.Background())
	cancel()
	r.g.runCtx = stopped
	// A stopped gateway's client is closed, so its record read fails.
	r.client.Close()
	r.g.relayBatch("discord:g1/t-stopped", []relayItem{{env: &lib.Envelope{TaskID: "task-x"}, subject: "a2a.tasks.platform.task-x.events"}})
	time.Sleep(requeueDelay + time.Second)
	text := logs.String()
	if n := strings.Count(text, "batch not rendered"); n != 1 {
		t.Fatalf("stop line logged %d times, want once:\n%s", n, text)
	}
	if strings.Contains(text, "requeueing batch") {
		t.Fatalf("a stopped gateway requeued its batch:\n%s", text)
	}
}
