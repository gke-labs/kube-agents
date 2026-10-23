package mcp

import (
	"context"
	"strings"
	"sync"
	"testing"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A drain whose messages go away under it (here a purge after the first
// fetch) reports the read as stalled, not the stream as holding only what it
// read.
func TestAStalledDrainIsAnError(t *testing.T) {
	url, js := testBus(t)
	ctx := context.Background()
	const n = drainFetchBatch + 44
	for i := 0; i < n; i++ {
		if _, err := js.Publish(ctx, "a2a.tasks.platform.task-drain.in", []byte("{}")); err != nil {
			t.Fatal(err)
		}
	}
	admin, err := js.Stream(ctx, lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	b := dial(t, url)
	s, err := b.client.JetStream().Stream(ctx, lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	var once sync.Once
	read, err := b.drainConsumer(ctx, s, jetstream.ConsumerConfig{
		FilterSubject: "a2a.tasks.platform.task-drain.in",
		DeliverPolicy: jetstream.DeliverAllPolicy,
	}, maxTaskEvents, func(jetstream.Msg) {
		once.Do(func() {
			if err := admin.Purge(ctx); err != nil {
				t.Error(err)
			}
		})
	})
	if err == nil || !strings.Contains(err.Error(), "before a fetch came back empty") {
		t.Fatalf("read %d of %d with err %v; want the stalled read reported", read, n, err)
	}
	if read >= n {
		t.Fatalf("read all %d despite the purge; the test did not stall the drain", read)
	}
}
