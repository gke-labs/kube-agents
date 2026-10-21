package lib

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
)

// heldHarness is a TASKS stream, a requester and a held subscription on one
// profile's intake: what a profile's warm workers or Job dispatcher see.
type heldHarness struct {
	t       *testing.T
	ctx     context.Context
	req     *Client
	profile string
}

func newHeldHarness(t *testing.T, profile string) (*heldHarness, *Client) {
	t.Helper()
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	ctx := testCtx(t)
	req, err := Connect(ctx, clientURL(s), WithName("requester"))
	if err != nil {
		t.Fatalf("Connect requester: %v", err)
	}
	t.Cleanup(req.Close)
	worker, err := Connect(ctx, clientURL(s), WithName("worker"))
	if err != nil {
		t.Fatalf("Connect worker: %v", err)
	}
	t.Cleanup(worker.Close)
	return &heldHarness{t: t, ctx: ctx, req: req, profile: profile}, worker
}

func (h *heldHarness) cfg() SubscribeConfig {
	return SubscribeConfig{
		Stream:  "TASKS",
		Subject: fmt.Sprintf("a2a.tasks.%s.*.in", h.profile),
		Durable: "profile-" + h.profile,
		Session: h.profile,
	}
}

// ensure creates the profile's durable as the operator does.
func (h *heldHarness) ensure(maxAckPending int, ackWait time.Duration) {
	h.t.Helper()
	c := h.cfg()
	if err := h.req.EnsureHeldConsumer(h.ctx, HeldConsumerConfig{
		Stream: c.Stream, Durable: c.Durable, FilterSubject: c.Subject,
		MaxAckPending: maxAckPending, AckWait: ackWait,
	}); err != nil {
		h.t.Fatalf("EnsureHeldConsumer: %v", err)
	}
}

func (h *heldHarness) submit(taskID string) {
	h.t.Helper()
	env, err := NewMessageEnvelope(Party{Session: "chatops"}, taskID, "ctx-"+taskID, "corr-"+taskID,
		validMessagePayload(), WithTo(Party{Session: h.profile}))
	if err != nil {
		h.t.Fatalf("build submission: %v", err)
	}
	if err := h.req.Publish(h.ctx, TaskInSubject(h.profile, taskID), env); err != nil {
		h.t.Fatalf("publish submission: %v", err)
	}
}

// running tracks which tasks a handler currently holds.
type running struct {
	mu   sync.Mutex
	held map[string]Delivery
	seen []string
}

func (r *running) add(d Delivery) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.held == nil {
		r.held = map[string]Delivery{}
	}
	r.held[d.Envelope().TaskID] = d
	r.seen = append(r.seen, d.Envelope().TaskID)
}

func (r *running) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.held)
}

func (r *running) any() (string, Delivery) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for id, d := range r.held {
		delete(r.held, id)
		return id, d
	}
	return "", nil
}

// The consumer's MaxAckPending is the gate: with two slots, a third task
// waits on the stream until a held one is acknowledged.
func TestHeldDeliveryMaxAckPendingIsTheGate(t *testing.T) {
	h, worker := newHeldHarness(t, "gate")
	r := &running{}
	h.ensure(2, 0)
	sub, err := worker.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
		r.add(d)
		<-ctx.Done() // hold until the test disposes of it or the sub stops
	})
	if err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	defer sub.Stop()

	for _, id := range []string{"task-a", "task-b", "task-c"} {
		h.submit(id)
	}
	waitFor(t, 5e9, "two tasks held", func() bool { return r.count() == 2 })
	time.Sleep(500 * time.Millisecond)
	if got := r.count(); got != 2 {
		t.Fatalf("%d tasks held with MaxAckPending 2, want 2", got)
	}

	_, d := r.any()
	if err := d.Ack(); err != nil {
		t.Fatalf("Ack: %v", err)
	}
	waitFor(t, 5e9, "the third task delivered after an ack", func() bool { return r.count() == 2 })
	r.mu.Lock()
	seen := len(r.seen)
	r.mu.Unlock()
	if seen != 3 {
		t.Errorf("handlers started %d times, want 3", seen)
	}
}

// The lib's heartbeat keeps a delivery alive past AckWait, so a long task is
// not redelivered while its holder is alive.
func TestHeldDeliveryIsKeptAlivePastAckWait(t *testing.T) {
	h, worker := newHeldHarness(t, "beat")
	var starts atomic.Int32
	release := make(chan struct{})
	h.ensure(1, time.Second)
	sub, err := worker.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
		starts.Add(1)
		select {
		case <-release:
			_ = d.Ack()
		case <-ctx.Done():
		}
	})
	if err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	defer sub.Stop()

	h.submit("task-long")
	waitFor(t, 5e9, "the task delivered", func() bool { return starts.Load() == 1 })
	time.Sleep(3500 * time.Millisecond) // three and a half AckWaits
	if got := starts.Load(); got != 1 {
		t.Fatalf("delivered %d times while its holder was alive, want 1", got)
	}
	close(release)
}

// A handler that returns without disposing of its delivery gives it back:
// the task is redelivered rather than held by nobody.
func TestHeldDeliveryReturnedWithoutDisposingIsRedelivered(t *testing.T) {
	h, worker := newHeldHarness(t, "drop")
	var mu sync.Mutex
	var deliveries []uint64
	h.ensure(1, 0)
	sub, err := worker.SubscribeHeld(h.ctx, h.cfg(), func(_ context.Context, d Delivery) {
		mu.Lock()
		deliveries = append(deliveries, d.NumDelivered())
		n := len(deliveries)
		mu.Unlock()
		if n >= 2 {
			_ = d.Ack()
		}
		// the first time: return without Ack, Nak or Term
	})
	if err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	defer sub.Stop()

	h.submit("task-dropped")
	waitFor(t, 5e9, "the task redelivered", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(deliveries) >= 2
	})
	mu.Lock()
	defer mu.Unlock()
	if deliveries[0] != 1 || deliveries[1] != 2 {
		t.Errorf("NumDelivered = %v, want [1 2 ...]", deliveries)
	}
}

// Nak with a delay comes back after it; Term never comes back.
func TestHeldDeliveryNakRedeliversAndTermDoesNot(t *testing.T) {
	h, worker := newHeldHarness(t, "naks")
	var mu sync.Mutex
	times := map[string][]time.Time{}
	h.ensure(4, 0)
	sub, err := worker.SubscribeHeld(h.ctx, h.cfg(), func(_ context.Context, d Delivery) {
		id := d.Envelope().TaskID
		mu.Lock()
		times[id] = append(times[id], time.Now())
		n := len(times[id])
		mu.Unlock()
		switch {
		case id == "task-nak" && n == 1:
			_ = d.Nak(time.Second)
		case id == "task-term":
			_ = d.Term()
		default:
			_ = d.Ack()
		}
	})
	if err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	defer sub.Stop()

	h.submit("task-nak")
	h.submit("task-term")
	waitFor(t, 5e9, "the Nak'd task redelivered", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(times["task-nak"]) == 2
	})
	time.Sleep(1500 * time.Millisecond)
	mu.Lock()
	defer mu.Unlock()
	if gap := times["task-nak"][1].Sub(times["task-nak"][0]); gap < 900*time.Millisecond {
		t.Errorf("Nak(1s) redelivered after %v", gap)
	}
	if n := len(times["task-term"]); n != 1 {
		t.Errorf("a Term'd task was delivered %d times, want 1", n)
	}
}

// Received is the stream's ingest time: what a queue timeout measures.
func TestHeldDeliveryCarriesTheIngestTime(t *testing.T) {
	h, worker := newHeldHarness(t, "age")
	got := make(chan time.Time, 1)
	before := time.Now().Add(-time.Second)
	h.submit("task-aged")
	time.Sleep(1200 * time.Millisecond)
	h.ensure(1, 0)
	sub, err := worker.SubscribeHeld(h.ctx, h.cfg(), func(_ context.Context, d Delivery) {
		got <- d.Received()
		_ = d.Ack()
	})
	if err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	defer sub.Stop()
	select {
	case rec := <-got:
		if rec.IsZero() || rec.Before(before) || time.Since(rec) < time.Second {
			t.Errorf("Received = %v, want the ingest time over a second ago", rec)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no delivery")
	}
}

// The session filter still applies: an envelope addressed to another session
// is not handed to the handler.
func TestHeldDeliveryKeepsTheSessionFilter(t *testing.T) {
	h, worker := newHeldHarness(t, "mine")
	var got atomic.Int32
	cfg := h.cfg()
	cfg.Subject = "a2a.tasks.*.*.in"
	if err := h.req.EnsureHeldConsumer(h.ctx, HeldConsumerConfig{
		Stream: cfg.Stream, Durable: cfg.Durable, FilterSubject: cfg.Subject, MaxAckPending: 2,
	}); err != nil {
		t.Fatalf("EnsureHeldConsumer: %v", err)
	}
	sub, err := worker.SubscribeHeld(h.ctx, cfg, func(_ context.Context, d Delivery) {
		got.Add(1)
		_ = d.Ack()
	})
	if err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	defer sub.Stop()
	h.submit("task-mine")
	other := &heldHarness{t: t, ctx: h.ctx, req: h.req, profile: "theirs"}
	other.submit("task-theirs")
	waitFor(t, 5e9, "the addressed task delivered", func() bool { return got.Load() >= 1 })
	time.Sleep(500 * time.Millisecond)
	if n := got.Load(); n != 1 {
		t.Errorf("handler ran %d times, want 1 (the other session's envelope must be filtered)", n)
	}
}

// Stop cancels every running handler's context and waits for them.
func TestHeldDeliveryStopCancelsHandlers(t *testing.T) {
	h, worker := newHeldHarness(t, "stop")
	var started, ended atomic.Int32
	h.ensure(2, 0)
	sub, err := worker.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
		started.Add(1)
		<-ctx.Done()
		ended.Add(1)
	})
	if err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	h.submit("task-one")
	h.submit("task-two")
	waitFor(t, 5e9, "both tasks held", func() bool { return started.Load() == 2 })
	sub.Stop()
	if n := ended.Load(); n != 2 {
		t.Errorf("%d handlers returned by the time Stop did, want 2", n)
	}
}

func TestHeldConfigIsRefusedWhenIncomplete(t *testing.T) {
	h, worker := newHeldHarness(t, "bad")
	c := h.cfg()
	if err := h.req.EnsureHeldConsumer(h.ctx, HeldConsumerConfig{
		Stream: c.Stream, Durable: c.Durable, FilterSubject: c.Subject,
	}); err == nil {
		t.Error("EnsureHeldConsumer accepted MaxAckPending 0")
	}
	h.ensure(1, 0)
	if _, err := worker.SubscribeHeld(h.ctx, c, nil); err == nil {
		t.Error("SubscribeHeld accepted a nil handler")
	}
	multi := c
	multi.Subject, multi.Subjects = "", []string{c.Subject, "a2a.tasks.x.*.in"}
	if _, err := worker.SubscribeHeld(h.ctx, multi, func(context.Context, Delivery) {}); err == nil {
		t.Error("SubscribeHeld accepted several filter subjects")
	}
}

// Workers bind and never create: with no durable there is nothing to bind,
// and the error says the operator creates it.
func TestSubscribeHeldNeverCreatesTheDurable(t *testing.T) {
	h, worker := newHeldHarness(t, "nocreate")
	// A worker that starts before the operator retries the bind for a while;
	// a short context ends that, so the test sees the error at once.
	short, cancel := context.WithTimeout(h.ctx, 2*time.Second)
	defer cancel()
	_, err := worker.SubscribeHeld(short, h.cfg(), func(context.Context, Delivery) {})
	if err == nil {
		t.Fatal("bound to a durable that does not exist")
	}
	if !strings.Contains(err.Error(), "operator creates it") {
		t.Errorf("error = %v, want it to say the operator creates the durable", err)
	}
}

// A worker binding does not rewrite the operator's consumer, and refuses one
// whose filter is not its intake.
func TestSubscribeHeldLeavesTheConsumerAsTheOperatorMadeIt(t *testing.T) {
	h, worker := newHeldHarness(t, "asmade")
	h.ensure(3, 45*time.Second)
	sub, err := worker.SubscribeHeld(h.ctx, h.cfg(), func(_ context.Context, d Delivery) { _ = d.Ack() })
	if err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	sub.Stop()
	h.req.mu.Lock()
	js := h.req.js
	h.req.mu.Unlock()
	cons, err := js.Consumer(h.ctx, "TASKS", h.cfg().Durable)
	if err != nil {
		t.Fatalf("consumer info: %v", err)
	}
	if cc := cons.CachedInfo().Config; cc.MaxAckPending != 3 || cc.AckWait != 45*time.Second {
		t.Errorf("after a worker bound: MaxAckPending=%d AckWait=%v, want 3 and 45s as the operator set",
			cc.MaxAckPending, cc.AckWait)
	}

	wrong := h.cfg()
	wrong.Subject = "a2a.tasks.someone-else.*.in"
	short, cancel := context.WithTimeout(h.ctx, 2*time.Second)
	defer cancel()
	if _, err := worker.SubscribeHeld(short, wrong, func(context.Context, Delivery) {}); err == nil {
		t.Error("bound a durable whose filter is not the requested intake")
	}
}

// The bind-only grant set is enough, and nothing more is needed: a worker
// whose user may only look up the durable, fetch from it and ack binds, takes
// a task and acks it, and cannot create a consumer. This is the grant the
// callout's profile narrowing gives a worker on its profile's durable.
func TestHeldDeliveryWorksWithOnlyTheBindGrants(t *testing.T) {
	const durable = "profile-perm"
	allow := func(subjects ...string) *natsserver.SubjectPermission {
		return &natsserver.SubjectPermission{Allow: subjects}
	}
	s := runJetStreamServer(t, -1, t.TempDir(), func(o *natsserver.Options) {
		o.Users = []*natsserver.User{
			{Username: "operator", Password: "op"},
			{Username: "worker", Password: "wk", Permissions: &natsserver.Permissions{
				Publish: allow(
					"$JS.API.CONSUMER.INFO.TASKS."+durable,
					"$JS.API.CONSUMER.MSG.NEXT.TASKS."+durable,
					"$JS.ACK.TASKS."+durable+".>",
				),
				Subscribe: allow("_INBOX.>"),
			}},
		}
	})
	t.Cleanup(s.Shutdown)
	url := func(user, pass string) string { return fmt.Sprintf("nats://%s:%s@%s", user, pass, s.Addr().String()) }
	provisionTasksStream(t, url("operator", "op"))
	ctx := testCtx(t)
	op, err := Connect(ctx, url("operator", "op"), WithName("operator"))
	if err != nil {
		t.Fatalf("Connect operator: %v", err)
	}
	t.Cleanup(op.Close)
	h := &heldHarness{t: t, ctx: ctx, req: op, profile: "perm"}
	h.ensure(1, 0)

	worker, err := Connect(ctx, url("worker", "wk"), WithName("worker"))
	if err != nil {
		t.Fatalf("Connect worker: %v", err)
	}
	t.Cleanup(worker.Close)
	acked := make(chan error, 1)
	sub, err := worker.SubscribeHeld(ctx, h.cfg(), func(_ context.Context, d Delivery) { acked <- d.Ack() })
	if err != nil {
		t.Fatalf("SubscribeHeld with only the bind grants: %v", err)
	}
	defer sub.Stop()
	h.submit("task-perm")
	select {
	case err := <-acked:
		if err != nil {
			t.Fatalf("Ack with only the bind grants: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no delivery to a worker holding only the bind grants")
	}

	c := h.cfg()
	short, cancel := context.WithTimeout(ctx, 2*time.Second) // a refused request is answered by nothing
	defer cancel()
	if err := worker.EnsureHeldConsumer(short, HeldConsumerConfig{
		Stream: c.Stream, Durable: c.Durable, FilterSubject: c.Subject, MaxAckPending: 5,
	}); err == nil {
		t.Error("a worker holding only the bind grants updated the operator's consumer")
	}
}
