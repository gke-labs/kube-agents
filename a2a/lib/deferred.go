package lib

import (
	"context"
	"sort"
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

// deferredDrainTimeout bounds how long a stop waits for the consume context to
// drain. A connection already gone never closes it; the unsettled deliveries
// are given back either way.
const deferredDrainTimeout = 2 * time.Second

// giveBackSpacing is the step between the delays a stop gives deliveries back
// with, which is what orders their redelivery. A thousand held deliveries
// come back within a second.
const giveBackSpacing = time.Millisecond

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

	mu        sync.Mutex
	ackWait   time.Duration // the durable's, read at each bind
	closed    bool          // stopping: deliveries are held for the give-back, not handled
	givenBack bool          // the give-back ran: a later delivery goes straight back
	pending   map[*deferredDelivery]struct{}
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
	// The heartbeat starts before the delivery is visible to giveBack, which
	// reads stopBeat when it settles it.
	dd.stopBeat = dd.heartbeat(d.beat)
	d.mu.Lock()
	switch {
	case d.givenBack:
		d.mu.Unlock()
		dd.stopBeat()
		_ = msg.Nak()
		return
	case d.closed:
		// Stopping, and draining: held for the give-back, which returns
		// every pending delivery in stream order. Nak'd here, it would be
		// redelivered ahead of older ones still pending.
		d.pending[dd] = struct{}{}
		d.mu.Unlock()
		return
	}
	d.pending[dd] = struct{}{}
	d.mu.Unlock()
	d.handler(dd)
}

// close stops intake: every later delivery is given back as it arrives.
func (d *deferredState) close() {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.closed = true
}

// giveBack stops intake and gives every unsettled delivery back at once.
func (d *deferredState) giveBack() {
	d.mu.Lock()
	d.closed = true
	d.givenBack = true
	pending := make([]*deferredDelivery, 0, len(d.pending))
	for dd := range d.pending {
		pending = append(pending, dd)
	}
	d.mu.Unlock()
	// In stream order, so the next subscriber gets them back in the order
	// they first came. A run of immediate naks comes back in no order the
	// server promises (newest first, measured on 2.15), but a delayed one is
	// redelivered when its delay ends, so the delays set the order.
	sort.Slice(pending, func(i, j int) bool { return pending[i].seq < pending[j].seq })
	for i, dd := range pending {
		_ = dd.Nak(time.Duration(i+1) * giveBackSpacing)
		// Given back or not (a nak with no connection fails), nothing renews
		// it after a stop: the server redelivers it within AckWait anyway.
		dd.release()
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

// Nak gives the delivery back. A nak that doesn't reach the connection still
// releases it, as the stop's give-back does: with nothing renewing it, the
// server redelivers it once AckWait runs out. Left pending, its heartbeat
// would renew it on a rebuilt connection for the life of the subscription,
// and nobody would retry the nak.
func (dd *deferredDelivery) Nak(delay time.Duration) error {
	err := dd.heldDelivery.Nak(delay)
	dd.release()
	return err
}

// settled stops tracking the delivery once an Ack or Term reached the
// connection; a failed one leaves it pending, renewed, for a retry.
func (dd *deferredDelivery) settled(err error) error {
	if err != nil {
		return err
	}
	dd.release()
	return nil
}

// release stops the delivery's heartbeat and tracking.
func (dd *deferredDelivery) release() {
	if dd.stopBeat != nil {
		dd.stopBeat()
	}
	dd.state.mu.Lock()
	delete(dd.state.pending, dd)
	dd.state.mu.Unlock()
}
