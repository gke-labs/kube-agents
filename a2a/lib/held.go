package lib

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// The held-delivery mode (#3655; the design is the profiles dispatch spec in
// #3660, "Admission and queueing"): a
// durable whose handler keeps each delivery unacknowledged for as long as the
// work it starts runs, so the consumer's MaxAckPending - not a pool inside
// the process - bounds how much of that work is in flight. A worker that dies
// mid-task gives the task back to the stream within one AckWait instead of
// keeping its slot.
//
// Two halves, for two principals:
//   - EnsureHeldConsumer is the operator's: it creates (or updates) a
//     profile's one shared durable with its MaxAckPending and AckWait.
//   - SubscribeHeld is every worker's (and the Job dispatcher's): it binds to
//     that durable and never creates, updates or deletes it, so one pod can
//     never rewrite the consumer another pod is pulling from. Binding needs
//     only $JS.API.CONSUMER.INFO and $JS.API.CONSUMER.MSG.NEXT on the
//     durable, and publish on $JS.ACK.<stream>.<durable>.> for acks and
//     heartbeats.
//
// Acks, Naks and heartbeats are published to the delivery's reply subject on
// whatever connection the client holds at that moment, not on the one that
// delivered the message, so a held delivery survives a connection rebuild.
// If the rebuild outlasts AckWait and the server redelivers the message to
// this process, the redelivery is adopted by the handler already running it
// (matched by stream sequence) instead of starting a second one.
//
// SubscribeDurable's auto-ack mode is unchanged: it creates its own consumer
// and acks when the handler returns, and every existing caller keeps that.

// DefaultHeldAckWait is how long the server waits for a held delivery's next
// sign of life before it redelivers. The holder renews it every third of that,
// so a live handler never loses its delivery and a dead one gives it back
// inside this window. A worker binds at the durable's AckWait; if the operator
// later shortens it, workers pick the new value up when they next bind.
const DefaultHeldAckWait = 2 * time.Minute

// abandonedNakDelay is how long a delivery whose handler returned without
// disposing of it waits before redelivery, so a handler that keeps doing that
// can't spin the stream. Shutdown (a cancelled handler context) gives the
// delivery back at once, for another worker to take.
const abandonedNakDelay = time.Second

// heldMarkerKey is the consumer metadata EnsureHeldConsumer stamps and
// SubscribeHeld requires, so a worker binds only to a durable the operator
// made for this mode: a server-default MaxAckPending (1000) looks exactly
// like a chosen one, and an explicit-ack durable that merely shares the name
// must not be taken for it.
const (
	heldMarkerKey   = "kube-agents.held-delivery"
	heldMarkerValue = "v1"
)

// HeldConsumerConfig is a held durable as the operator creates it.
type HeldConsumerConfig struct {
	Stream  string
	Durable string
	// FilterSubject is the profile's intake, `a2a.tasks.<profile>.*.in`.
	FilterSubject string
	// MaxAckPending is the most deliveries held unacknowledged at once.
	// Required, at least 1. For a profile it is its effective concurrency.
	MaxAckPending int
	// AckWait is the redelivery window; zero means DefaultHeldAckWait. The
	// holder's heartbeat renews it, so it bounds how long a dead holder keeps
	// a task, not how long a task may run.
	AckWait time.Duration
}

func (h HeldConsumerConfig) ackWait() time.Duration {
	if h.AckWait <= 0 {
		return DefaultHeldAckWait
	}
	return h.AckWait
}

// EnsureHeldConsumer creates the held durable, or updates it in place to
// cfg. It is the operator's call; workers only bind (SubscribeHeld).
func (c *Client) EnsureHeldConsumer(ctx context.Context, cfg HeldConsumerConfig) error {
	switch {
	case cfg.Stream == "" || cfg.Durable == "" || cfg.FilterSubject == "":
		return fmt.Errorf("HeldConsumerConfig: Stream, Durable and FilterSubject are required")
	case cfg.MaxAckPending < 1:
		return fmt.Errorf("HeldConsumerConfig.MaxAckPending must be at least 1, got %d", cfg.MaxAckPending)
	}
	_, js := c.conn()
	if js == nil {
		return fmt.Errorf("EnsureHeldConsumer: not connected")
	}
	_, err := js.CreateOrUpdateConsumer(ctx, cfg.Stream, jetstream.ConsumerConfig{
		Durable:       cfg.Durable,
		FilterSubject: cfg.FilterSubject,
		AckPolicy:     jetstream.AckExplicitPolicy,
		MaxAckPending: cfg.MaxAckPending,
		AckWait:       cfg.ackWait(),
		Metadata:      map[string]string{heldMarkerKey: heldMarkerValue},
	})
	if err != nil {
		return fmt.Errorf("held consumer %s on %s: %w", cfg.Durable, cfg.Stream, err)
	}
	return nil
}

// Delivery is one held envelope. Exactly one of Ack, Nak and Term disposes
// of it; until then the lib keeps it alive with in-progress acks. A handler
// that returns without disposing of it has its delivery Nak'd, so the task
// comes back rather than being dropped or held by nobody.
type Delivery interface {
	Envelope() *Envelope
	// Subject is the subject the envelope arrived on.
	Subject() string
	// Received is when the stream stored the message: what a queue timeout
	// measures against. Zero if the server sent no metadata.
	Received() time.Time
	// NumDelivered counts deliveries of this message, 1 the first time. More
	// than 1 on a handler's first look means an earlier holder died or let it
	// go; the caller decides what that means by task id (the spec: a task
	// that had already started is failed, never re-run). A redelivery to the
	// process already running it is adopted, not handed to a second handler.
	NumDelivered() uint64
	// Ack disposes of the delivery: the work is done or settled.
	Ack() error
	// Nak gives the delivery back for redelivery after delay (0: at once).
	Nak(delay time.Duration) error
	// Term disposes of the delivery as one that must never be redelivered.
	Term() error
}

// HeldSubscription is a held durable's subscription. Nothing on it waits for
// a running handler except Wait, so none of it can deadlock a handler that is
// itself shutting down.
type HeldSubscription interface {
	// Stop stops taking deliveries and cancels every running handler's
	// context. It does not wait; call Wait for that.
	Stop()
	// StopIntake stops taking deliveries and lets running handlers finish:
	// what a worker does to drain before it exits. Deliveries already on
	// their way are given back.
	StopIntake()
	// Wait blocks until no handler is running or ctx is done. Never call it
	// from a handler: it would wait for itself.
	Wait(ctx context.Context) error
}

// SubscribeHeld binds to an existing held durable (EnsureHeldConsumer) and
// hands each delivery to handler on its own goroutine, with a context that is
// cancelled by Stop or Close. It never creates, updates or deletes the
// consumer: a missing durable, or one that isn't an explicit-ack consumer
// filtering cfg.Subject with a real MaxAckPending, is an error (a missing
// one is retried for the bind window first, in case the operator has not
// created it yet). The heartbeat is paced from the durable's own AckWait.
// Envelope-subject agreement and the session filter apply as in
// SubscribeDurable, and cfg.Session must be the intake's addressee, so the
// filter can't silently drop every task. The envelopeId dedup does not apply:
// a redelivery is expected and is the caller's to recognise by task id.
//
// Shutting down: StopIntake, then Wait, then Close. Close alone cancels the
// handlers but doesn't wait for them, and anything they publish after the
// connection closes is lost.
func (c *Client) SubscribeHeld(ctx context.Context, cfg SubscribeConfig, handler func(context.Context, Delivery)) (HeldSubscription, error) {
	if handler == nil {
		return nil, fmt.Errorf("SubscribeHeld: handler is required")
	}
	if len(cfg.Subjects) > 0 {
		return nil, fmt.Errorf("SubscribeHeld binds one intake subject; set Subject, not Subjects")
	}
	if addressee := intakeAddressee(cfg.Subject); addressee != "" && addressee != "*" && cfg.Session != addressee {
		return nil, fmt.Errorf("SubscribeHeld: Session %q is not the intake's addressee %q; the session filter would drop every task", cfg.Session, addressee)
	}
	hctx, cancel := context.WithCancel(context.Background())
	h := &heldState{c: c, handler: handler, ctx: hctx, cancel: cancel,
		live: map[uint64]*heldDelivery{}, changed: make(chan struct{})}
	sub, err := c.subscribe(ctx, cfg, func(s *durableSub) { s.held = h })
	if err != nil {
		return nil, err
	}
	return &heldSubscription{s: sub.(*durableSub), h: h}, nil
}

// intakeAddressee is the addressee token of `a2a.tasks.<addressee>.<task>.in`,
// or "" for a subject of another shape.
func intakeAddressee(subject string) string {
	parts := strings.Split(subject, ".")
	if len(parts) != 5 || parts[0] != "a2a" || parts[1] != "tasks" || parts[4] != "in" {
		return ""
	}
	return parts[2]
}

type heldSubscription struct {
	s *durableSub
	h *heldState
}

func (hs *heldSubscription) Stop()                          { hs.s.Stop() }
func (hs *heldSubscription) StopIntake()                    { hs.s.stopIntake(); hs.s.unregister() }
func (hs *heldSubscription) Wait(ctx context.Context) error { return hs.h.wait(ctx) }

// heldState is a durableSub's held-mode half. nil on an auto-ack sub.
type heldState struct {
	c       *Client
	handler func(context.Context, Delivery)
	ctx     context.Context
	cancel  context.CancelFunc

	mu      sync.Mutex
	ackWait time.Duration // the bound durable's, read at each bind
	closed  bool          // intake stopped: late deliveries are given back
	live    map[uint64]*heldDelivery
	running int
	changed chan struct{} // closed and replaced whenever running drops
}

// bind looks up the durable without creating it, checks it is the intake the
// caller expects with the cap this mode depends on, and records its AckWait.
func (h *heldState) bind(ctx context.Context, js jetstream.JetStream, cfg SubscribeConfig) (jetstream.Consumer, error) {
	cons, err := js.Consumer(ctx, cfg.Stream, cfg.Durable)
	if err != nil {
		return nil, fmt.Errorf("bind held durable %s on %s (the operator creates it): %w", cfg.Durable, cfg.Stream, err)
	}
	cc := cons.CachedInfo().Config
	switch {
	case len(cc.FilterSubjects) > 0 || cc.FilterSubject != cfg.Subject:
		return nil, fmt.Errorf("held durable %s filters %q%v, not %q; refusing to bind", cfg.Durable, cc.FilterSubject, cc.FilterSubjects, cfg.Subject)
	case cc.AckPolicy != jetstream.AckExplicitPolicy:
		return nil, fmt.Errorf("held durable %s is not explicit-ack; refusing to bind", cfg.Durable)
	case cc.MaxAckPending < 1:
		return nil, fmt.Errorf("held durable %s has no MaxAckPending cap (%d); refusing to bind", cfg.Durable, cc.MaxAckPending)
	case cc.AckWait <= 0:
		return nil, fmt.Errorf("held durable %s has no AckWait; refusing to bind", cfg.Durable)
	case cc.Metadata[heldMarkerKey] != heldMarkerValue:
		return nil, fmt.Errorf("held durable %s was not made by EnsureHeldConsumer (no %s metadata); refusing to bind", cfg.Durable, heldMarkerKey)
	}
	h.mu.Lock()
	h.ackWait = cc.AckWait
	h.mu.Unlock()
	return cons, nil
}

func (h *heldState) beat() time.Duration {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.ackWait <= 0 {
		return DefaultHeldAckWait / 3
	}
	return h.ackWait / 3
}

// dispatch hands one delivery to the handler on its own goroutine, or adopts
// it into the handler already running that message, or gives it back once
// intake has stopped. The server will not deliver more than MaxAckPending
// unacknowledged messages, and a redelivery of one this process holds starts
// no second handler, so a process never runs more handlers than that.
func (h *heldState) dispatch(msg jetstream.Msg, env *Envelope) {
	var seq, delivered uint64
	var received time.Time
	if md, err := msg.Metadata(); err == nil && md != nil {
		seq, delivered, received = md.Sequence.Stream, md.NumDelivered, md.Timestamp
	}
	h.mu.Lock()
	if h.closed {
		h.mu.Unlock()
		_ = msg.Nak()
		return
	}
	if d, ok := h.live[seq]; ok && seq != 0 {
		// Release h.mu first: adopt takes d.mu, and a heartbeat holding d.mu
		// can be waiting on the client lock behind Close, which waits on h.mu.
		h.mu.Unlock()
		d.adopt(msg.Reply(), delivered)
		return
	}
	d := &heldDelivery{c: h.c, subject: msg.Subject(), reply: msg.Reply(), env: env,
		received: received, delivered: delivered}
	if seq != 0 {
		h.live[seq] = d
	}
	h.running++
	h.mu.Unlock()

	go func() {
		defer h.done(seq)
		stopBeat := d.heartbeat(h.beat)
		defer stopBeat()
		h.handler(h.ctx, d)
		delay := abandonedNakDelay
		if h.ctx.Err() != nil {
			delay = 0 // shutting down: let another worker take it now
		}
		_ = d.Nak(delay)
	}()
}

func (h *heldState) done(seq uint64) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if seq != 0 {
		delete(h.live, seq)
	}
	h.running--
	close(h.changed)
	h.changed = make(chan struct{})
}

// stopIntake makes every later delivery go back to the stream.
func (h *heldState) stopIntake() {
	h.mu.Lock()
	h.closed = true
	h.mu.Unlock()
}

// wait blocks until no handler is running or ctx is done.
func (h *heldState) wait(ctx context.Context) error {
	for {
		h.mu.Lock()
		if h.running == 0 {
			h.mu.Unlock()
			return nil
		}
		ch := h.changed
		h.mu.Unlock()
		select {
		case <-ch:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

type heldDelivery struct {
	c        *Client
	subject  string
	env      *Envelope
	received time.Time

	mu        sync.Mutex
	reply     string // the latest delivery's ack subject
	delivered uint64
	disposed  bool   // a disposition reached the connection
	last      []byte // that disposition, re-sent if the message comes back
}

func (d *heldDelivery) Envelope() *Envelope { return d.env }
func (d *heldDelivery) Subject() string     { return d.subject }
func (d *heldDelivery) Received() time.Time { return d.received }
func (d *heldDelivery) NumDelivered() uint64 {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.delivered
}
func (d *heldDelivery) Ack() error  { return d.dispose([]byte("+ACK")) }
func (d *heldDelivery) Term() error { return d.dispose([]byte("+TERM")) }
func (d *heldDelivery) Nak(delay time.Duration) error {
	if delay <= 0 {
		return d.dispose([]byte("-NAK"))
	}
	return d.dispose([]byte(fmt.Sprintf(`-NAK {"delay": %d}`, delay.Nanoseconds())))
}

// adopt points the delivery at a redelivery of the same message, so its next
// heartbeat and its disposition reach the server under the current reply. If
// the handler had already disposed of it, the redelivery means the server
// didn't get that disposition, so it is sent again under the new reply.
func (d *heldDelivery) adopt(reply string, delivered uint64) {
	nc, _ := d.c.conn()
	d.mu.Lock()
	defer d.mu.Unlock()
	d.reply = reply
	d.delivered = delivered
	if d.disposed && d.last != nil {
		_ = d.publish(nc, d.last)
	}
}

// dispose sends a disposition. Only one that reaches the connection counts:
// after it, every later disposition is a no-op (so a handler's Ack and the
// lib's fallback Nak can't both reach the server), but a failed send leaves
// the delivery live, so the handler can try again and the heartbeat goes on.
func (d *heldDelivery) dispose(body []byte) error {
	nc, _ := d.c.conn() // outside d.mu: the client lock may be held by Close
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.disposed {
		return nil
	}
	if err := d.publish(nc, body); err != nil {
		return err
	}
	d.disposed, d.last = true, body
	return nil
}

// publish sends an ack body to the delivery's reply subject on nc, the
// client's connection when the caller looked. Callers hold d.mu.
func (d *heldDelivery) publish(nc *nats.Conn, body []byte) error {
	if d.reply == "" {
		return fmt.Errorf("held delivery has no reply subject")
	}
	if nc == nil {
		return fmt.Errorf("held delivery: not connected")
	}
	return nc.Publish(d.reply, body)
}

// heartbeat sends an in-progress ack every interval() until the delivery is
// disposed of or the returned stop is called. The interval is read each time,
// so a rebind that shortened the durable's AckWait speeds up deliveries
// already running.
func (d *heldDelivery) heartbeat(interval func() time.Duration) (stop func()) {
	next := func() time.Duration {
		if iv := interval(); iv > 0 {
			return iv
		}
		return time.Second
	}
	done := make(chan struct{})
	go func() {
		t := time.NewTimer(next())
		defer t.Stop()
		for {
			select {
			case <-done:
				return
			case <-t.C:
				nc, _ := d.c.conn()
				d.mu.Lock()
				disposed := d.disposed
				if !disposed {
					_ = d.publish(nc, []byte("+WPI"))
				}
				d.mu.Unlock()
				if disposed {
					return
				}
				t.Reset(next())
			}
		}
	}()
	var once sync.Once
	return func() { once.Do(func() { close(done) }) }
}
