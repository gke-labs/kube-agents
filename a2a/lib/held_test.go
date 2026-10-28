package lib

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
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
	got := make(chan Delivery, 4)
	h.ensure(1, 3*time.Second)
	sub, err := worker.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
		starts.Add(1)
		got <- d
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
	if n := starts.Load(); n != 1 {
		t.Fatalf("delivered %d times while its holder was alive, want 1", n)
	}
	if d := <-got; d.NumDelivered() != 1 {
		t.Errorf("NumDelivered = %d, want 1", d.NumDelivered())
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

// A held durable is one profile's intake, `a2a.tasks.<profile>.*.in`. A
// wildcard or `>` addressee is refused: the session filter would ack other
// addressees' tasks off the durable every worker shares, consuming them for
// all. So is a longer subject the publisher and envelope-subject agreement
// don't know.
func TestSubscribeHeldRefusesAnIntakeThatIsNotOneProfiles(t *testing.T) {
	h, worker := newHeldHarness(t, "mine")
	h.ensure(1, 0)
	for _, subject := range []string{"a2a.tasks.*.*.in", "a2a.tasks.mine.>", "a2a.tasks.>", "a2a.tasks.mine.*.events",
		"a2a.tasks.mine.>.in", "a2a.tasks.mine.task-1.in", "a2a.tasks.mine.read.top.*.in"} {
		cfg := h.cfg()
		cfg.Subject = subject
		if _, err := worker.SubscribeHeld(h.ctx, cfg, func(context.Context, Delivery) {}); err == nil ||
			!strings.Contains(err.Error(), "not one profile's intake") {
			t.Errorf("SubscribeHeld(%q) = %v, want it refused as not one profile's intake", subject, err)
		}
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
	if _, err := worker.SubscribeHeld(h.ctx, multi, func(context.Context, Delivery) {}); err == nil ||
		!strings.Contains(err.Error(), "one intake subject") {
		t.Errorf("SubscribeHeld with several filter subjects = %v, want it refused for that", err)
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
	_, js := h.req.conn()
	cons, err := js.Consumer(h.ctx, "TASKS", h.cfg().Durable)
	if err != nil {
		t.Fatalf("consumer info: %v", err)
	}
	if cc := cons.CachedInfo().Config; cc.MaxAckPending != 3 || cc.AckWait != 45*time.Second {
		t.Errorf("after a worker bound: MaxAckPending=%d AckWait=%v, want 3 and 45s as the operator set",
			cc.MaxAckPending, cc.AckWait)
	}

	// Otherwise valid, so it is bind's filter check that refuses it. The
	// refusal is read from the durable's config and is not retried, so the
	// call returns it at once under the harness's own context.
	wrong := h.cfg()
	wrong.Subject, wrong.Session = "a2a.tasks.someone-else.*.in", "someone-else"
	if _, err := worker.SubscribeHeld(h.ctx, wrong, func(context.Context, Delivery) {}); err == nil ||
		!errors.Is(err, ErrHeldBindRefused) || !strings.Contains(err.Error(), "filters") {
		t.Errorf("binding a durable whose filter is not the requested intake = %v, want ErrHeldBindRefused for its filter", err)
	}
}

// A refusal read from the durable's stored config is final on the first
// attempt: no retry, no "retrying" log line, and the caller gets the refusal
// rather than whatever error the bind window happened to end on.
func TestSubscribeHeldRefusalIsNotRetried(t *testing.T) {
	h, _ := newHeldHarness(t, "norefusalretry")
	h.ensure(1, 30*time.Second)
	capture := &logCapture{}
	nc, _ := h.req.conn()
	worker, err := Connect(h.ctx, nc.ConnectedUrl(), WithName("worker-logged"), WithLogger(slog.New(capture)))
	if err != nil {
		t.Fatalf("Connect worker: %v", err)
	}
	t.Cleanup(worker.Close)

	wrong := h.cfg()
	wrong.Subject, wrong.Session = "a2a.tasks.someone-else.*.in", "someone-else"
	started := time.Now()
	_, err = worker.SubscribeHeld(h.ctx, wrong, func(context.Context, Delivery) {})
	took := time.Since(started)
	if err == nil || !errors.Is(err, ErrHeldBindRefused) {
		t.Fatalf("SubscribeHeld on a mismatched filter = %v, want ErrHeldBindRefused", err)
	}
	if h.ctx.Err() != nil {
		t.Fatalf("the refusal consumed the whole context (%v); it was retried for the bind window", took)
	}
	if capture.contains("durable bind failed; retrying") {
		t.Errorf("a refusal was retried:\n%s", capture.String())
	}
}

// The other half of the rule: a durable that does not exist yet is retried
// for the bind window, so a worker that starts before the operator binds once
// the operator has run EnsureHeldConsumer, instead of exiting at once.
func TestSubscribeHeldWaitsForTheOperatorToCreateTheDurable(t *testing.T) {
	h, _ := newHeldHarness(t, "waitsforop")
	capture := &logCapture{}
	nc, _ := h.req.conn()
	worker, err := Connect(h.ctx, nc.ConnectedUrl(), WithName("worker-logged"), WithLogger(slog.New(capture)))
	if err != nil {
		t.Fatalf("Connect worker: %v", err)
	}
	t.Cleanup(worker.Close)

	bound := make(chan error, 1)
	go func() {
		sub, err := worker.SubscribeHeld(h.ctx, h.cfg(), func(context.Context, Delivery) {})
		if err == nil {
			sub.Stop()
		}
		bound <- err
	}()
	// The operator arrives only after the worker's first attempt has been
	// refused for a missing durable and logged as a retry.
	deadline := time.Now().Add(10 * time.Second)
	for !capture.contains("durable bind failed; retrying") {
		select {
		case err := <-bound:
			t.Fatalf("SubscribeHeld returned before the operator created the durable: %v", err)
		default:
		}
		if time.Now().After(deadline) {
			t.Fatalf("no retry of the missing durable was logged:\n%s", capture.String())
		}
		time.Sleep(10 * time.Millisecond)
	}
	h.ensure(1, 30*time.Second)
	select {
	case err := <-bound:
		if err != nil {
			t.Fatalf("SubscribeHeld after the operator created the durable = %v, want it bound", err)
		}
	case <-h.ctx.Done():
		t.Fatalf("SubscribeHeld did not bind once the durable existed:\n%s", capture.String())
	}
	if !capture.contains("durable bound after retry") {
		t.Errorf("the bind that followed the operator was not logged as a retry's success:\n%s", capture.String())
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

// A server restart that outlasts AckWait gives the task back: the
// redelivery starts a second handler, which sees NumDelivered above 1 (the
// caller's signal to look the task up by id), and its Ack settles it. The
// lib doesn't adopt a redelivery into a handler it thinks is still running.
func TestHeldDeliveryOutlastingAckWaitIsRedelivered(t *testing.T) {
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
	second := make(chan uint64, 1)
	sub, err := worker.SubscribeHeld(ctx, h.cfg(), func(ctx context.Context, d Delivery) {
		if starts.Add(1) == 1 {
			<-ctx.Done() // the holder that lost the task
			return
		}
		second <- d.NumDelivered()
		_ = d.Ack()
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
	select {
	case n := <-second:
		if n < 2 {
			t.Errorf("the redelivery's NumDelivered = %d, want more than 1", n)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("no redelivery after a restart that outlasted AckWait")
	}
	time.Sleep(5 * time.Second) // two AckWaits: an unsettled ack would bring it back
	if n := starts.Load(); n != 2 {
		t.Errorf("%d handler starts, want 2 (the holder, then the redelivery that acked)", n)
	}
}

// Binding refuses a durable without the cap this mode depends on, and a
// session that isn't the intake's addressee.
func TestSubscribeHeldRefusesADurableItCannotGate(t *testing.T) {
	h, worker := newHeldHarness(t, "uncapped")
	_, js := h.req.conn()
	c := h.cfg()
	if _, err := js.CreateOrUpdateConsumer(h.ctx, c.Stream, jetstream.ConsumerConfig{
		Durable: c.Durable, FilterSubject: c.Subject, AckPolicy: jetstream.AckExplicitPolicy, MaxAckPending: -1,
	}); err != nil {
		t.Fatalf("create uncapped consumer: %v", err)
	}
	if _, err := worker.SubscribeHeld(h.ctx, c, func(context.Context, Delivery) {}); err == nil ||
		!errors.Is(err, ErrHeldBindRefused) || !strings.Contains(err.Error(), "MaxAckPending") {
		t.Errorf("bound a durable with no MaxAckPending cap: %v", err)
	}

	other := h.cfg()
	other.Session = "some-pod"
	if _, err := worker.SubscribeHeld(h.ctx, other, func(context.Context, Delivery) {}); err == nil ||
		!strings.Contains(err.Error(), "is not the intake's addressee") {
		t.Errorf("SubscribeHeld with a Session the filter would use to drop every task = %v, want it refused for that", err)
	}
}

// A held task survives the lib's own rebuild (NR-2) inside AckWait: the
// connection is closed for good and replaced, so the delivering connection
// and the current one differ. Heartbeats resume on the new one, the task
// starts once, and its Ack on the new connection settles it.
func TestHeldDeliverySurvivesTheLibsRebuild(t *testing.T) {
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
	h := &heldHarness{t: t, ctx: ctx, req: op, profile: "nr2"}
	h.ensure(1, 10*time.Second)
	capture := &logCapture{}
	worker, err := Connect(ctx, clientURL(s1), WithName("worker"), WithLogger(slog.New(capture)),
		WithNATSOptions(nats.MaxReconnects(1), nats.ReconnectWait(50*time.Millisecond)))
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
	h.submit("task-nr2")
	waitFor(t, 5e9, "the task held", func() bool { return starts.Load() == 1 })

	s1.Shutdown()
	s1.WaitForShutdown()
	waitFor(t, 10e9, "the terminal close", func() bool { return capture.contains("nats connection closed") })
	s2 := runJetStreamServer(t, port, dir, nil)
	t.Cleanup(s2.Shutdown)
	waitFor(t, 20e9, "the lib's rebuild", func() bool { return capture.contains("nats rebuild complete") })
	if n := worker.rebuilds.Load(); n != 1 {
		t.Fatalf("rebuilds = %d, want 1: the test must cross the lib's rebuild, not a nats.go reconnect", n)
	}
	time.Sleep(12 * time.Second) // past AckWait: only the heartbeat on the new connection keeps it
	if n := starts.Load(); n != 1 {
		t.Fatalf("%d handlers started for one task across the lib's rebuild, want 1", n)
	}
	close(release)
	if err := <-ackErr; err != nil {
		t.Fatalf("Ack on the rebuilt connection: %v", err)
	}
	time.Sleep(12 * time.Second) // past AckWait: an unsettled ack would bring it back
	if n := starts.Load(); n != 1 {
		t.Errorf("the task came back after its Ack (%d starts)", n)
	}
}

// A disposition that didn't reach the connection leaves the delivery live:
// the handler's retry is sent, not swallowed as already disposed.
func TestAFailedDispositionCanBeRetried(t *testing.T) {
	h, worker := newHeldHarness(t, "retry")
	h.ensure(1, 0)
	got := make(chan Delivery, 1)
	if _, err := worker.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
		got <- d
		<-ctx.Done()
	}); err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	h.submit("task-retry")
	d := <-got
	worker.Close()
	if err := d.Ack(); err == nil {
		t.Fatal("Ack on a closed connection reported success")
	}
	if err := d.Ack(); err == nil {
		t.Error("the retried Ack was swallowed as already disposed; a failed send must leave the delivery live")
	}
}

// A durable the operator didn't make for this mode is refused even when it
// looks capped: a server-default MaxAckPending is 1000, indistinguishable
// from a chosen one, so the bind requires EnsureHeldConsumer's marker.
func TestSubscribeHeldRefusesAServerDefaultDurable(t *testing.T) {
	h, worker := newHeldHarness(t, "default")
	_, js := h.req.conn()
	c := h.cfg()
	if _, err := js.CreateOrUpdateConsumer(h.ctx, c.Stream, jetstream.ConsumerConfig{
		Durable: c.Durable, FilterSubject: c.Subject, AckPolicy: jetstream.AckExplicitPolicy,
	}); err != nil {
		t.Fatalf("create default consumer: %v", err)
	}
	if _, err := worker.SubscribeHeld(h.ctx, c, func(context.Context, Delivery) {}); err == nil ||
		!errors.Is(err, ErrHeldBindRefused) || !strings.Contains(err.Error(), "EnsureHeldConsumer") {
		t.Errorf("bound a server-default durable: %v", err)
	}
}

// StopIntake leaves the subscription registered, so Close still cancels a
// handler that outlives the drain's Wait.
func TestCloseCancelsAStragglerAfterStopIntake(t *testing.T) {
	h, worker := newHeldHarness(t, "straggler")
	h.ensure(1, 0)
	started := make(chan struct{})
	sub, err := worker.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
		close(started)
		<-ctx.Done()
	})
	if err != nil {
		t.Fatalf("SubscribeHeld: %v", err)
	}
	h.submit("task-straggler")
	<-started
	sub.StopIntake()
	short, cancel := context.WithTimeout(h.ctx, 200*time.Millisecond)
	defer cancel()
	if err := sub.Wait(short); err == nil {
		t.Fatal("Wait returned with a handler still blocked")
	}
	worker.Close()
	wctx, wcancel := context.WithTimeout(h.ctx, 5*time.Second)
	defer wcancel()
	if err := sub.Wait(wctx); err != nil {
		t.Errorf("after StopIntake and Close the straggler was not cancelled: %v", err)
	}
}

// The operator can change a live durable's MaxAckPending, and nothing else
// bound workers depend on: not its AckWait (their heartbeat pace), in either
// direction, and not its filter (the intake they checked at bind).
func TestEnsureHeldConsumerOnlyChangesMaxAckPendingInPlace(t *testing.T) {
	h, _ := newHeldHarness(t, "inplace")
	h.ensure(1, 90*time.Second)
	c := h.cfg()
	ensure := func(filter string, maxAckPending int, ackWait time.Duration) error {
		return h.req.EnsureHeldConsumer(h.ctx, HeldConsumerConfig{
			Stream: c.Stream, Durable: c.Durable, FilterSubject: filter, MaxAckPending: maxAckPending, AckWait: ackWait,
		})
	}
	for _, w := range []time.Duration{60 * time.Second, 120 * time.Second, 0} {
		if err := ensure(c.Subject, 1, w); err == nil || !strings.Contains(err.Error(), "AckWait") {
			t.Errorf("changing a live durable's 90s AckWait to %v = %v, want it refused for AckWait", w, err)
		}
	}
	if err := ensure("a2a.tasks.other.*.in", 1, 90*time.Second); err == nil || !strings.Contains(err.Error(), "filters") {
		t.Errorf("moving a live durable to another intake = %v, want it refused for its filter", err)
	}
	if err := ensure(c.Subject, 4, 90*time.Second); err != nil {
		t.Errorf("refused a MaxAckPending change: %v", err)
	}
}

// A lookup that fails for any reason but not-found leaves the durable's
// state unknown, so EnsureHeldConsumer returns the error rather than update
// a live durable past its guards. The operator here may create the consumer
// but not look it up, so only the lookup fails.
func TestEnsureHeldConsumerRefusesWhenTheLookupFails(t *testing.T) {
	const durable = "profile-blind"
	allow := func(subjects ...string) *natsserver.SubjectPermission {
		return &natsserver.SubjectPermission{Allow: subjects}
	}
	s := runJetStreamServer(t, -1, t.TempDir(), func(o *natsserver.Options) {
		o.Users = []*natsserver.User{
			{Username: "operator", Password: "op"},
			{Username: "blind", Password: "bl", Permissions: &natsserver.Permissions{
				Publish: allow(
					"$JS.API.CONSUMER.CREATE.TASKS."+durable,
					"$JS.API.CONSUMER.CREATE.TASKS."+durable+".>",
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
	h := &heldHarness{t: t, ctx: ctx, req: op, profile: "blind"}
	h.ensure(1, 90*time.Second)

	blind, err := Connect(ctx, url("blind", "bl"), WithName("blind"))
	if err != nil {
		t.Fatalf("Connect blind: %v", err)
	}
	t.Cleanup(blind.Close)
	c := h.cfg()
	// No deadline: the denied lookup then fails at the library's default API
	// timeout and leaves the create its own, as a reconciler's call would.
	nodeadline, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := blind.EnsureHeldConsumer(nodeadline, HeldConsumerConfig{
		Stream: c.Stream, Durable: c.Durable, FilterSubject: c.Subject, MaxAckPending: 1, AckWait: 30 * time.Second,
	}); err == nil || !strings.Contains(err.Error(), "look it up") {
		t.Errorf("EnsureHeldConsumer with a failing lookup = %v, want the lookup's error", err)
	}
	_, js := op.conn()
	cons, err := js.Consumer(ctx, c.Stream, c.Durable)
	if err != nil {
		t.Fatalf("consumer info: %v", err)
	}
	if w := cons.CachedInfo().Config.AckWait; w != 90*time.Second {
		t.Errorf("after a failed lookup the durable's AckWait is %v, want 90s unchanged", w)
	}
}
