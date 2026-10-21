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
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
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
	h.ensure(1, 3*time.Second)
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
	time.Sleep(7 * time.Second) // more than two AckWaits
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

// Stop cancels every running handler's context; Wait waits for them.
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
	wctx, cancel := context.WithTimeout(h.ctx, 5*time.Second)
	defer cancel()
	if err := sub.Wait(wctx); err != nil {
		t.Fatalf("Wait after Stop: %v", err)
	}
	if n := ended.Load(); n != 2 {
		t.Errorf("%d handlers returned by the time Wait did, want 2", n)
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
// callout's profile narrowing will give a worker on its profile's durable
// (a later #3655 pull request; today it grants per-pod ack-none consumers).
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

// Close never waits on a handler, so a handler that publishes on its way out
// can't hang the process (the publish fails once the connection is closed,
// which is why a worker drains before it closes).
func TestCloseDoesNotWaitForAHeldHandler(t *testing.T) {
	h, worker := newHeldHarness(t, "closing")
	h.ensure(1, 0)
	started := make(chan struct{})
	_, err := worker.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
		close(started)
		<-ctx.Done()
		env, _ := NewMessageEnvelope(Party{Session: "closing"}, "task-x", "ctx-x", "corr-x", validMessagePayload())
		_ = worker.Publish(context.Background(), "a2a.tasks.closing.task-x.events", env)
	})
	if err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	h.submit("task-x")
	<-started
	done := make(chan struct{})
	go func() { worker.Close(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Close hung on a handler that publishes after its context is cancelled")
	}
}

// A handler may stop its own subscription without deadlocking on itself.
func TestStopFromInsideAHeldHandlerReturns(t *testing.T) {
	h, worker := newHeldHarness(t, "selfstop")
	h.ensure(1, 0)
	var sub HeldSubscription
	ready := make(chan struct{})
	returned := make(chan struct{})
	var err error
	sub, err = worker.SubscribeHeld(h.ctx, h.cfg(), func(_ context.Context, d Delivery) {
		<-ready
		sub.Stop()
		_ = d.Ack()
		close(returned)
	})
	if err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	close(ready)
	h.submit("task-self")
	select {
	case <-returned:
	case <-time.After(5 * time.Second):
		t.Fatal("Stop called from inside a handler did not return")
	}
}

// StopIntake drains: the running handler finishes and acks, and nothing new
// is taken.
func TestHeldStopIntakeLetsRunningWorkFinish(t *testing.T) {
	h, worker := newHeldHarness(t, "drain")
	h.ensure(2, 0)
	release := make(chan struct{})
	var started, acked atomic.Int32
	sub, err := worker.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
		started.Add(1)
		select {
		case <-release:
		case <-ctx.Done():
			return
		}
		if d.Ack() == nil {
			acked.Add(1)
		}
	})
	if err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	h.submit("task-running")
	waitFor(t, 5e9, "the first task running", func() bool { return started.Load() == 1 })
	sub.StopIntake()
	h.submit("task-late")
	time.Sleep(500 * time.Millisecond)
	close(release)
	wctx, cancel := context.WithTimeout(h.ctx, 5*time.Second)
	defer cancel()
	if err := sub.Wait(wctx); err != nil {
		t.Fatalf("Wait: %v", err)
	}
	if s, a := started.Load(), acked.Load(); s != 1 || a != 1 {
		t.Errorf("after StopIntake: %d handler(s) started and %d acked, want 1 and 1 (drained, nothing new taken)", s, a)
	}
}

// A held task survives a server restart that outlasts AckWait: the
// redelivery to this process is adopted by the handler already running it,
// no second handler starts, and that handler's Ack on the new connection
// settles it.
func TestHeldDeliverySurvivesARebuild(t *testing.T) {
	dir := t.TempDir()
	s1 := runJetStreamServer(t, -1, dir, nil)
	port := serverPort(s1)
	provisionTasksStream(t, clientURL(s1))
	ctx := testCtx(t)
	op, err := Connect(ctx, clientURL(s1), WithName("operator"))
	if err != nil {
		t.Fatalf("Connect operator: %v", err)
	}
	defer op.Close()
	h := &heldHarness{t: t, ctx: ctx, req: op, profile: "rebuild"}
	h.ensure(1, 2*time.Second)
	worker, err := Connect(ctx, clientURL(s1), WithName("worker"),
		WithNATSOptions(nats.MaxReconnects(-1), nats.ReconnectWait(100*time.Millisecond)))
	if err != nil {
		t.Fatalf("Connect worker: %v", err)
	}
	defer worker.Close()

	var starts atomic.Int32
	release := make(chan struct{})
	ackErr := make(chan error, 1)
	sub, err := worker.SubscribeHeld(ctx, h.cfg(), func(ctx context.Context, d Delivery) {
		starts.Add(1)
		select {
		case <-release:
			ackErr <- d.Ack()
		case <-ctx.Done():
		}
	})
	if err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	defer sub.Stop()
	h.submit("task-survivor")
	waitFor(t, 5e9, "the task held", func() bool { return starts.Load() == 1 })

	s1.Shutdown()
	s1.WaitForShutdown()
	time.Sleep(3 * time.Second) // longer than AckWait
	s2 := runJetStreamServer(t, port, dir, nil)
	t.Cleanup(s2.Shutdown)
	time.Sleep(6 * time.Second) // reconnect, rebind, and let any redelivery arrive
	if n := starts.Load(); n != 1 {
		t.Fatalf("%d handlers started for one task across a rebuild, want 1", n)
	}
	close(release)
	if err := <-ackErr; err != nil {
		t.Fatalf("Ack after the rebuild: %v", err)
	}
	time.Sleep(5 * time.Second) // two AckWaits: an unsettled ack would bring it back
	if n := starts.Load(); n != 1 {
		t.Errorf("the task came back after its Ack (%d starts)", n)
	}
}

// Binding refuses a durable without the cap this mode depends on, and a
// session that isn't the intake's addressee.
func TestSubscribeHeldRefusesADurableItCannotGate(t *testing.T) {
	h, worker := newHeldHarness(t, "uncapped")
	h.req.mu.Lock()
	js := h.req.js
	h.req.mu.Unlock()
	c := h.cfg()
	if _, err := js.CreateOrUpdateConsumer(h.ctx, c.Stream, jetstream.ConsumerConfig{
		Durable: c.Durable, FilterSubject: c.Subject, AckPolicy: jetstream.AckExplicitPolicy, MaxAckPending: -1,
	}); err != nil {
		t.Fatalf("create uncapped consumer: %v", err)
	}
	short, cancel := context.WithTimeout(h.ctx, 2*time.Second)
	defer cancel()
	if _, err := worker.SubscribeHeld(short, c, func(context.Context, Delivery) {}); err == nil ||
		!strings.Contains(err.Error(), "MaxAckPending") {
		t.Errorf("bound a durable with no MaxAckPending cap: %v", err)
	}

	other := h.cfg()
	other.Session = "some-pod"
	if _, err := worker.SubscribeHeld(h.ctx, other, func(context.Context, Delivery) {}); err == nil {
		t.Error("accepted a Session that the session filter would use to drop every task")
	}
}
