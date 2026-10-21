package lib

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// The held-delivery mode (#3655, docs/designs/spec-profiles-dispatch.md,
// "Admission and queueing"): a durable whose handler keeps each delivery
// unacknowledged for as long as the work it starts runs, so the consumer's
// MaxAckPending - not a pool inside the process - is what bounds how much of
// that work is in flight. A worker that dies mid-task gives the task back to
// the stream within one AckWait instead of keeping its slot.
//
// Two halves, for two principals:
//   - EnsureHeldConsumer is the operator's: it creates (or updates) a
//     profile's one shared durable with its MaxAckPending and AckWait.
//   - SubscribeHeld is every worker's (and the Job dispatcher's): it binds to
//     that durable and never creates, updates or deletes it, so one pod can
//     never rewrite the consumer another pod is pulling from. Binding needs
//     only $JS.API.CONSUMER.INFO and $JS.API.CONSUMER.MSG.NEXT on the
//     durable, and $JS.ACK.<stream>.<durable>.> for acks and heartbeats.
//
// SubscribeDurable's auto-ack mode is unchanged: it creates its own consumer
// and acks when the handler returns, and every existing caller keeps that.

// DefaultHeldAckWait is how long the server waits for a held delivery's next
// sign of life before it redelivers. The lib renews it every third of that, so
// a live handler never loses its delivery, and a dead one gives it back inside
// this window.
const DefaultHeldAckWait = 2 * time.Minute

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
	c.mu.Lock()
	js := c.js
	c.mu.Unlock()
	if js == nil {
		return fmt.Errorf("EnsureHeldConsumer: not connected")
	}
	_, err := js.CreateOrUpdateConsumer(ctx, cfg.Stream, jetstream.ConsumerConfig{
		Durable:       cfg.Durable,
		FilterSubject: cfg.FilterSubject,
		AckPolicy:     jetstream.AckExplicitPolicy,
		MaxAckPending: cfg.MaxAckPending,
		AckWait:       cfg.ackWait(),
	})
	if err != nil {
		return fmt.Errorf("held consumer %s on %s: %w", cfg.Durable, cfg.Stream, err)
	}
	return nil
}

// Delivery is one held envelope. Exactly one of Ack, Nak and Term disposes
// of it; until then the lib keeps it alive with InProgress. A handler that
// returns without disposing of it has its delivery Nak'd, so the task comes
// back rather than being dropped or held by nobody.
type Delivery interface {
	Envelope() *Envelope
	// Subject is the subject the envelope arrived on.
	Subject() string
	// Received is when the stream stored the message: what a queue timeout
	// measures against. Zero if the server sent no metadata.
	Received() time.Time
	// NumDelivered counts deliveries of this message, 1 the first time. More
	// than 1 means an earlier holder died, let it go, or lost its connection:
	// after a connection rebuild a handler still running on the old delivery
	// can see the same task redelivered beside it. The caller's own
	// idempotency (by task id) decides what that means.
	NumDelivered() uint64
	// Ack disposes of the delivery: the work is done or settled.
	Ack() error
	// Nak gives the delivery back for redelivery after delay (0: at once).
	Nak(delay time.Duration) error
	// Term disposes of the delivery as one that must never be redelivered.
	Term() error
}

// SubscribeHeld binds to an existing held durable (EnsureHeldConsumer) and
// hands each delivery to handler on its own goroutine, with a context that is
// cancelled when the subscription stops. It never creates, updates or deletes
// the consumer: a missing durable, or one whose filter is not cfg.Subject, is
// an error. The heartbeat is paced from the durable's own AckWait.
// Envelope-subject agreement and the session filter apply exactly as in
// SubscribeDurable; the envelopeId dedup does not, because a redelivery is
// expected here and is the caller's to recognise (spec: a redelivered task
// that had already started is a terminal failure, never a re-run).
func (c *Client) SubscribeHeld(ctx context.Context, cfg SubscribeConfig, handler func(context.Context, Delivery)) (Subscription, error) {
	if handler == nil {
		return nil, fmt.Errorf("SubscribeHeld: handler is required")
	}
	if len(cfg.Subjects) > 0 {
		return nil, fmt.Errorf("SubscribeHeld binds one intake subject; set Subject, not Subjects")
	}
	hctx, cancel := context.WithCancel(context.Background())
	return c.subscribe(ctx, cfg, func(s *durableSub) {
		s.held = &heldState{handler: handler, ctx: hctx, cancel: cancel}
	})
}

// heldState is a durableSub's held-mode half. nil on an auto-ack sub.
type heldState struct {
	handler func(context.Context, Delivery)
	ctx     context.Context
	cancel  context.CancelFunc
	wg      sync.WaitGroup

	mu      sync.Mutex
	ackWait time.Duration // the bound durable's, read at each bind
}

// bind looks up the durable without creating it, checks it is the intake the
// caller expects and records its AckWait for the heartbeat.
func (h *heldState) bind(ctx context.Context, js jetstream.JetStream, cfg SubscribeConfig) (jetstream.Consumer, error) {
	cons, err := js.Consumer(ctx, cfg.Stream, cfg.Durable)
	if err != nil {
		return nil, fmt.Errorf("bind held durable %s on %s (the operator creates it): %w", cfg.Durable, cfg.Stream, err)
	}
	cc := cons.CachedInfo().Config
	if cc.FilterSubject != cfg.Subject {
		return nil, fmt.Errorf("held durable %s filters %q, not %q; refusing to bind", cfg.Durable, cc.FilterSubject, cfg.Subject)
	}
	if cc.AckPolicy != jetstream.AckExplicitPolicy {
		return nil, fmt.Errorf("held durable %s is not explicit-ack; refusing to bind", cfg.Durable)
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

// dispatch hands one delivery to the handler on its own goroutine. The
// server will not deliver more than MaxAckPending unacknowledged messages, so
// this never runs more than that many handlers per connection.
func (h *heldState) dispatch(msg jetstream.Msg, env *Envelope) {
	d := &heldDelivery{msg: msg, env: env}
	if md, err := msg.Metadata(); err == nil && md != nil {
		d.received = md.Timestamp
		d.delivered = md.NumDelivered
	}
	h.wg.Add(1)
	go func() {
		defer h.wg.Done()
		stopBeat := d.heartbeat(h.beat())
		defer stopBeat()
		h.handler(h.ctx, d)
		// Returned without disposing: give it back rather than hold it with
		// nobody behind it.
		_ = d.Nak(0)
	}()
}

// stop cancels every running handler's context and waits for them to return.
func (h *heldState) stop() {
	h.cancel()
	h.wg.Wait()
}

type heldDelivery struct {
	msg       jetstream.Msg
	env       *Envelope
	received  time.Time
	delivered uint64

	mu       sync.Mutex
	disposed bool
}

func (d *heldDelivery) Envelope() *Envelope  { return d.env }
func (d *heldDelivery) Subject() string      { return d.msg.Subject() }
func (d *heldDelivery) Received() time.Time  { return d.received }
func (d *heldDelivery) NumDelivered() uint64 { return d.delivered }
func (d *heldDelivery) Ack() error           { return d.dispose(d.msg.Ack) }
func (d *heldDelivery) Term() error          { return d.dispose(d.msg.Term) }
func (d *heldDelivery) Nak(delay time.Duration) error {
	if delay <= 0 {
		return d.dispose(d.msg.Nak)
	}
	return d.dispose(func() error { return d.msg.NakWithDelay(delay) })
}

// dispose runs the first disposition and makes every later one a no-op, so a
// handler's Ack and the lib's fallback Nak cannot both reach the server.
func (d *heldDelivery) dispose(f func() error) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.disposed {
		return nil
	}
	d.disposed = true
	return f()
}

// heartbeat renews the delivery's AckWait every interval until it is
// disposed of or the returned stop is called.
func (d *heldDelivery) heartbeat(interval time.Duration) (stop func()) {
	if interval <= 0 {
		interval = time.Second
	}
	done := make(chan struct{})
	go func() {
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			select {
			case <-done:
				return
			case <-t.C:
				d.mu.Lock()
				disposed := d.disposed
				if !disposed {
					_ = d.msg.InProgress()
				}
				d.mu.Unlock()
				if disposed {
					return
				}
			}
		}
	}()
	var once sync.Once
	return func() { once.Do(func() { close(done) }) }
}
