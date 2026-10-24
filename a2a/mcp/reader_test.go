package mcp

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
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

// readerTestBus is testBus's server with max_payload raised to maxPayload and
// one unrestricted user, for messages the operator's bus would refuse.
func readerTestBus(t *testing.T, maxPayload int32) (string, jetstream.JetStream) {
	t.Helper()
	opts := &natsserver.Options{Host: "127.0.0.1", Port: -1, JetStream: true, StoreDir: t.TempDir(), NoLog: true, NoSigs: true, MaxPayload: maxPayload}
	opts.Users = []*natsserver.User{{Username: testUser, Password: testPass}}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server did not start")
	}
	t.Cleanup(srv.Shutdown)
	nc, err := nats.Connect(srv.ClientURL(), nats.UserInfo(testUser, testPass))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := js.CreateStream(context.Background(), jetstream.StreamConfig{Name: lib.TasksStream, Subjects: []string{"a2a.tasks.>"}, MaxBytes: 64 << 20}); err != nil {
		t.Fatal(err)
	}
	return srv.ClientURL(), js
}

func drainTasks(t *testing.T, b *NATSBus, subject string) (int, error) {
	t.Helper()
	ctx := context.Background()
	s, err := b.client.JetStream().Stream(ctx, lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	return b.drainConsumer(ctx, s, jetstream.ConsumerConfig{
		FilterSubject: subject,
		DeliverPolicy: jetstream.DeliverAllPolicy,
	}, maxTaskEvents, func(jetstream.Msg) {})
}

// A drain of large messages stops at its byte budget, with the messages that
// fit it read, rather than reading all of them into the pod.
func TestADrainStopsAtItsByteBudget(t *testing.T) {
	url, js := testBus(t)
	ctx := context.Background()
	cfg, err := js.Stream(ctx, lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	sc := cfg.CachedInfo().Config
	sc.MaxBytes = 64 << 20
	if _, err := js.UpdateStream(ctx, sc); err != nil {
		t.Fatal(err)
	}
	const subject = "a2a.tasks.platform.task-big.in"
	payload := make([]byte, 700<<10)
	size := len(payload) + len(subject)
	fit := drainMaxBytes / size
	for i := 0; i < fit+2; i++ {
		if _, err := js.Publish(ctx, subject, payload); err != nil {
			t.Fatal(err)
		}
	}
	read, err := drainTasks(t, dial(t, url), subject)
	if !errors.Is(err, ErrReadByteBudget) || !strings.Contains(err.Error(), "before the byte budget") {
		t.Fatalf("read %d with err %v; want the byte budget reported", read, err)
	}
	if read != fit {
		t.Fatalf("read %d messages of %d bytes under a %d-byte budget; want %d", read, size, drainMaxBytes, fit)
	}
}

// A message over the per-fetch cap, on a bus that accepts one, is refused
// with a clear error, not read, hung on, or reported as a stalled read.
func TestADrainRefusesAMessageOverTheFetchCap(t *testing.T) {
	url, js := readerTestBus(t, 8<<20)
	ctx := context.Background()
	const subject = "a2a.tasks.platform.task-huge.in"
	for _, data := range [][]byte{[]byte("{}"), make([]byte, drainFetchBytes+1)} {
		if _, err := js.Publish(ctx, subject, data); err != nil {
			t.Fatal(err)
		}
	}
	read, err := drainTasks(t, dial(t, url), subject)
	if !errors.Is(err, ErrReadByteBudget) || !strings.Contains(err.Error(), "over the") {
		t.Fatalf("read %d with err %v; want the over-size message reported", read, err)
	}
	if read != 1 {
		t.Fatalf("read %d; want the one message before the over-size one", read)
	}
}

// A fetch asks for as many messages as fit the per-fetch cap at the bus's
// max_payload, and always for at least one.
func TestDrainBatchFitsTheFetchCap(t *testing.T) {
	for _, c := range []struct {
		maxPayload int64
		want       int
	}{
		{0, 1},
		{1 << 20, drainFetchBytes / (1 << 20)},
		{drainFetchBytes, 1},
		{8 << 20, 1},
		{1 << 10, drainFetchBatch},
	} {
		if got := drainBatch(c.maxPayload); got != c.want {
			t.Errorf("drainBatch(%d) = %d; want %d", c.maxPayload, got, c.want)
		}
	}
}
