package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
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

// bigBus is testBus with every stream's byte cap raised, for reads of
// messages near the bus's 1 MiB limit.
func bigBus(t *testing.T) (string, jetstream.JetStream) {
	t.Helper()
	url, js := testBus(t)
	ctx := context.Background()
	for _, name := range []string{lib.TasksStream, streamDirectory} {
		s, err := js.Stream(ctx, name)
		if err != nil {
			t.Fatal(err)
		}
		cfg := s.CachedInfo().Config
		cfg.MaxBytes = 64 << 20
		if _, err := js.UpdateStream(ctx, cfg); err != nil {
			t.Fatal(err)
		}
	}
	return url, js
}

// readerTestBus is a server with max_payload raised to maxPayload and one
// unrestricted user, for a message the operator's bus would refuse.
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

// publishN publishes n copies of payload on subject.
func publishN(t *testing.T, js jetstream.JetStream, subject string, payload []byte, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		if _, err := js.Publish(context.Background(), subject, payload); err != nil {
			t.Fatal(err)
		}
	}
}

// A drain of large messages stops at its byte budget, with the messages that
// fit it read, rather than reading all of them into the pod.
func TestADrainStopsAtItsByteBudget(t *testing.T) {
	url, js := bigBus(t)
	const subject = "a2a.tasks.platform.task-big.in"
	payload := make([]byte, 700<<10)
	fit := readMaxBytes / (len(payload) + len(subject))
	publishN(t, js, subject, payload, fit+2)
	read, err := drainTasks(t, dial(t, url), subject)
	if !errors.Is(err, ErrReadByteBudget) || !strings.Contains(err.Error(), "before the byte budget") {
		t.Fatalf("read %d with err %v; want the byte budget reported", read, err)
	}
	if read != fit {
		t.Fatalf("read %d messages under a %d-byte budget; want %d", read, readMaxBytes, fit)
	}
}

// A task read stops at the byte budget rather than folding every event into
// the pod; its event count bound alone would let 4096 large events through.
func TestATaskReadStopsAtItsByteBudget(t *testing.T) {
	url, js := bigBus(t)
	ref := TaskRef{Addressee: fakeAddressee, TaskID: "task-big"}
	publishN(t, js, lib.TaskEventsSubject(ref.Addressee, ref.TaskID), make([]byte, 700<<10), 8)
	_, err := dial(t, url).ReadTask(context.Background(), ref)
	if !errors.Is(err, ErrReadByteBudget) || !strings.Contains(err.Error(), "before the byte budget") {
		t.Fatalf("err %v; want the byte budget reported", err)
	}
}

// A DIRECTORY read stops at the byte budget, keeping the cards read before
// it and saying it stopped, rather than reading the whole stream.
func TestADirectoryReadStopsAtItsByteBudget(t *testing.T) {
	url, js := bigBus(t)
	desc := strings.Repeat("x", 700<<10)
	const n = 8
	for i := 0; i < n; i++ {
		publishCard(t, js, fmt.Sprintf("agent-%d", i), map[string]any{"name": "A", "description": desc})
	}
	cards, err := dial(t, url).agentCards(context.Background())
	if !errors.Is(err, ErrReadByteBudget) || !strings.Contains(err.Error(), "before the byte budget") {
		t.Fatalf("read %d cards with err %v; want the byte budget reported", len(cards), err)
	}
	if len(cards) == 0 || len(cards) >= n {
		t.Fatalf("kept %d of %d cards; want those read before the budget", len(cards), n)
	}
}

// Every pull a read sends carries the per-pull byte cap, and the server
// applies it: it ends a pull at the cap with a 409 that nothing but a pull's
// max_bytes produces, though the pull asked for more messages than it got.
func TestTheServerCapsEveryPull(t *testing.T) {
	url, js := bigBus(t)
	nc := js.Conn()
	var mu sync.Mutex
	var pulls []map[string]any
	capped := 0
	pullSub, err := nc.Subscribe("$JS.API.CONSUMER.MSG.NEXT."+lib.TasksStream+".>", func(m *nats.Msg) {
		var req map[string]any
		if json.Unmarshal(m.Data, &req) == nil {
			mu.Lock()
			pulls = append(pulls, req)
			mu.Unlock()
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = pullSub.Unsubscribe() }()
	inboxSub, err := nc.Subscribe("_INBOX.>", func(m *nats.Msg) {
		if m.Header.Get("Status") == "409" && strings.Contains(m.Header.Get("Description"), "Exceeds MaxBytes") {
			mu.Lock()
			capped++
			mu.Unlock()
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = inboxSub.Unsubscribe() }()
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	const subject = "a2a.tasks.platform.task-cap.in"
	// Six of 300 KiB: under the read's budget, under its batch, over one
	// pull's cap, so only the server's cap can split them.
	const n = 6
	publishN(t, js, subject, make([]byte, 300<<10), n)
	read, err := drainTasks(t, dial(t, url), subject)
	if err != nil || read != n {
		t.Fatalf("read %d with err %v; want all %d", read, err, n)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(pulls) == 0 {
		t.Fatal("saw no pull requests")
	}
	for _, p := range pulls {
		if mb, _ := p["max_bytes"].(float64); int(mb) != pullMaxBytes {
			t.Errorf("pull %v; want max_bytes %d", p, pullMaxBytes)
		}
	}
	if capped == 0 {
		t.Errorf("no pull ended at its byte cap over %d pulls; the server did not apply one", len(pulls))
	}
}

// A message over the per-pull cap, on a bus that accepts one, is never sent
// into the pod: the server refuses it, and the read stops at the first
// refusal with a clear error, not hung on, not pulled for again and again,
// and not reported as done.
func TestADrainStopsAtAMessageOverThePullCap(t *testing.T) {
	url, js := readerTestBus(t, 8<<20)
	ctx := context.Background()
	const subject = "a2a.tasks.platform.task-huge.in"
	for _, data := range [][]byte{[]byte("{}"), make([]byte, pullMaxBytes+1), []byte("{}")} {
		if _, err := js.Publish(ctx, subject, data); err != nil {
			t.Fatal(err)
		}
	}
	var pulls atomic.Int64
	sub, err := js.Conn().Subscribe("$JS.API.CONSUMER.MSG.NEXT.>", func(*nats.Msg) { pulls.Add(1) })
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	start := time.Now()
	read, err := drainTasks(t, dial(t, url), subject)
	took := time.Since(start)
	t.Logf("stopped after %s and %d pull requests", took, pulls.Load())
	if !errors.Is(err, ErrReadByteBudget) || !strings.Contains(err.Error(), "pull cap") {
		t.Fatalf("read %d with err %v; want the over-cap message reported", read, err)
	}
	if read != 1 {
		t.Fatalf("read %d; want the one message before the over-cap one", read)
	}
	// The first pull the server refuses ends the read: no storm of pulls
	// against the shared bus, and no wait for the stall timer.
	if n := pulls.Load(); n > 3 {
		t.Errorf("sent %d pull requests; want the read to stop on the first refused one", n)
	}
	if took > time.Second {
		t.Errorf("took %s to stop; want it at the first refused pull", took)
	}
}

// A task watch sizes each pull from what is pending then, not when the watch
// began: a watch opened on a task with no events yet reads a burst of later
// ones in a few pulls, not one pull per event.
func TestATaskWatchSizesEachPullFromWhatIsPending(t *testing.T) {
	url, js := bigBus(t)
	var pulls atomic.Int64
	sub, err := js.Conn().Subscribe("$JS.API.CONSUMER.MSG.NEXT."+lib.TasksStream+".>", func(*nats.Msg) { pulls.Add(1) })
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	ref := TaskRef{Addressee: fakeAddressee, TaskID: "task-later"}
	w, err := dial(t, url).watchTask(context.Background(), ref)
	if err != nil {
		t.Fatal(err)
	}
	defer w.close()
	const n = 50
	publishN(t, js, lib.TaskEventsSubject(ref.Addressee, ref.TaskID), []byte("{}"), n)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for w.seen < n && ctx.Err() == nil {
		if err := w.next(ctx); err != nil {
			t.Fatal(err)
		}
	}
	if w.seen != n {
		t.Fatalf("read %d of %d events", w.seen, n)
	}
	if got := pulls.Load(); got > 5 {
		t.Errorf("sent %d pulls for %d events; want each pull sized from what is pending", got, n)
	}
}
