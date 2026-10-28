package lib

import (
	"context"
	"sync"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// The deferred-ack mode: a durable read in delivery order, as SubscribeDurable
// reads it, whose deliveries the caller settles when the work they start is
// done rather than when the handler returns. It is for a consumer that hands
// each envelope to a queue and only knows later whether it was acted on, the
// gateway's event relay being the one: acked on return, an event the
// process then fails to act on is gone from the stream, and a process that
// stops with events queued loses every one of them.
//
// It differs from the held mode (held.go) in two ways the relay needs. The
// handler is called in delivery order on the dispatch goroutine, never
// concurrently, so a task's events arrive in stream order. And the consumer
// is created or updated as SubscribeDurable creates it, so the caller needs
// no CONSUMER.INFO grant and no operator-made durable.
//
// Settling: exactly one of Ack, Nak and Term, at any time after the handler
// returns. Until then the lib renews the delivery with in-progress acks paced
// from the durable's AckWait. Stop gives every unsettled delivery back at
// once, so another subscriber on the durable gets it without waiting out
// AckWait. A redelivery is expected (after a stop, or a holder that died) and
// is the caller's to recognise: the envelopeId dedup doesn't apply, and
// Sequence is what to recognise it by.

// DeferredDelivery is one delivery in the deferred-ack mode.
type DeferredDelivery interface {
	Delivery
	// Sequence is the message's stream sequence, 0 if the server sent no
	// metadata. On one subject a later message has a higher one, so the
	// highest sequence a caller has acted on for a subject tells a
	// redelivery of something already done from new work.
	Sequence() uint64
}

// SubscribeDurableDeferred creates or updates the durable as SubscribeDurable
// does and hands each delivery to handler in order, with the envelope-subject
// agreement and the session filter applied as there (the lib settles what
// those refuse). The handler must not block for long: it runs on the
// dispatch goroutine, and a later delivery waits for it.
func (c *Client) SubscribeDurableDeferred(ctx context.Context, cfg SubscribeConfig, handler func(DeferredDelivery)) (Subscription, error) {
	d := &deferredState{c: c, handler: handler, pending: map[*deferredDelivery]struct{}{}}
	return c.subscribe(ctx, cfg, func(s *durableSub) { s.deferred = d })
}

// deferredState is a durableSub's deferred-mode half. nil on the other modes.
type deferredState struct {
	c       *Client
	handler func(DeferredDelivery)

	mu      sync.Mutex
	ackWait time.Duration // the durable's, read at each bind
	closed  bool          // stopped: late deliveries are given back
	pending map[*deferredDelivery]struct{}
}

// bound records the AckWait the heartbeat paces from.
func (d *deferredState) bound(cons jetstream.Consumer) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.ackWait = cons.CachedInfo().Config.AckWait
}

func (d *deferredState) beat() time.Duration {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.ackWait <= 0 {
		return DefaultHeldAckWait / heartbeatsPerAckWait
	}
	return d.ackWait / heartbeatsPerAckWait
}

// dispatch hands one delivery to the handler, or gives it back once the
// subscription has stopped.
func (d *deferredState) dispatch(msg jetstream.Msg, env *Envelope) {
	dd := &deferredDelivery{state: d, heldDelivery: &heldDelivery{c: d.c, subject: msg.Subject(), reply: msg.Reply(), env: env}}
	if md, err := msg.Metadata(); err == nil && md != nil {
		dd.delivered, dd.received, dd.seq = md.NumDelivered, md.Timestamp, md.Sequence.Stream
	}
	d.mu.Lock()
	if d.closed {
		d.mu.Unlock()
		_ = msg.Nak()
		return
	}
	d.pending[dd] = struct{}{}
	d.mu.Unlock()
	dd.stopBeat = dd.heartbeat(d.beat)
	d.handler(dd)
}

// giveBack stops intake and gives every unsettled delivery back at once.
func (d *deferredState) giveBack() {
	d.mu.Lock()
	d.closed = true
	pending := make([]*deferredDelivery, 0, len(d.pending))
	for dd := range d.pending {
		pending = append(pending, dd)
	}
	d.mu.Unlock()
	for _, dd := range pending {
		_ = dd.Nak(0)
	}
}

type deferredDelivery struct {
	*heldDelivery
	state    *deferredState
	seq      uint64
	stopBeat func()
}

func (dd *deferredDelivery) Sequence() uint64 { return dd.seq }
func (dd *deferredDelivery) Ack() error       { return dd.settled(dd.heldDelivery.Ack()) }
func (dd *deferredDelivery) Term() error      { return dd.settled(dd.heldDelivery.Term()) }
func (dd *deferredDelivery) Nak(delay time.Duration) error {
	return dd.settled(dd.heldDelivery.Nak(delay))
}

// settled stops tracking the delivery once a disposition reached the
// connection; a failed one leaves it pending, renewed, for a retry.
func (dd *deferredDelivery) settled(err error) error {
	if err != nil {
		return err
	}
	if dd.stopBeat != nil {
		dd.stopBeat()
	}
	dd.state.mu.Lock()
	delete(dd.state.pending, dd)
	dd.state.mu.Unlock()
	return nil
}
