package lib

import (
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// deferredCollector records deferred deliveries in the order the handler saw them.
type deferredCollector struct {
	mu  sync.Mutex
	got []DeferredDelivery
}

func (c *deferredCollector) add(d DeferredDelivery) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.got = append(c.got, d)
}

func (c *deferredCollector) snapshot() []DeferredDelivery {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]DeferredDelivery(nil), c.got...)
}

func waitUntil(t *testing.T, what string, within time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// Deliveries arrive in stream order, with rising sequences, and stay held
// after the handler returns until the caller settles them.
func TestDeferredDeliveriesArriveInOrderAndAreSettledLater(t *testing.T) {
	h, worker := newHeldHarness(t, "relay")
	var c deferredCollector
	sub, err := worker.SubscribeDurableDeferred(h.ctx, h.cfg(), c.add)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(sub.Stop)
	for _, id := range []string{"t1", "t2", "t3"} {
		h.submit(id)
	}
	waitUntil(t, "three deliveries", 5*time.Second, func() bool { return len(c.snapshot()) == 3 })
	got := c.snapshot()
	for i, want := range []string{"t1", "t2", "t3"} {
		if got[i].Envelope().TaskID != want {
			t.Fatalf("delivery %d is %s, want %s", i, got[i].Envelope().TaskID, want)
		}
		if i > 0 && got[i].Sequence() <= got[i-1].Sequence() {
			t.Fatalf("sequence %d after %d", got[i].Sequence(), got[i-1].Sequence())
		}
	}
	// Nothing settled yet: all three are pending on the consumer.
	info := consumerInfo(t, h)
	if info.NumAckPending != 3 {
		t.Fatalf("ack pending = %d, want 3 before any settles", info.NumAckPending)
	}
	for _, d := range got {
		if err := d.Ack(); err != nil {
			t.Fatal(err)
		}
	}
	waitUntil(t, "the acks", 5*time.Second, func() bool { return consumerInfo(t, h).NumAckPending == 0 })
}

// A Nak gives the delivery back, and it comes again with NumDelivered 2.
func TestDeferredNakRedelivers(t *testing.T) {
	h, worker := newHeldHarness(t, "relay")
	var c deferredCollector
	sub, err := worker.SubscribeDurableDeferred(h.ctx, h.cfg(), c.add)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(sub.Stop)
	h.submit("t1")
	waitUntil(t, "the delivery", 5*time.Second, func() bool { return len(c.snapshot()) == 1 })
	if err := c.snapshot()[0].Nak(0); err != nil {
		t.Fatal(err)
	}
	waitUntil(t, "the redelivery", 5*time.Second, func() bool { return len(c.snapshot()) == 2 })
	if d := c.snapshot()[1]; d.NumDelivered() != 2 || d.Envelope().TaskID != "t1" {
		t.Fatalf("redelivery = %s, delivered %d", d.Envelope().TaskID, d.NumDelivered())
	}
}

// Stop gives every unsettled delivery back at once: a second subscriber on
// the durable gets them well inside AckWait, and one already settled stays
// settled.
func TestDeferredStopGivesUnsettledDeliveriesBack(t *testing.T) {
	h, first := newHeldHarness(t, "relay")
	var c1 deferredCollector
	sub1, err := first.SubscribeDurableDeferred(h.ctx, h.cfg(), c1.add)
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"t1", "t2", "t3"} {
		h.submit(id)
	}
	waitUntil(t, "three deliveries", 5*time.Second, func() bool { return len(c1.snapshot()) == 3 })
	if err := c1.snapshot()[0].Ack(); err != nil {
		t.Fatal(err)
	}
	sub1.Stop()

	second, err := Connect(h.ctx, h.url, WithName("second"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(second.Close)
	var c2 deferredCollector
	sub2, err := second.SubscribeDurableDeferred(h.ctx, h.cfg(), c2.add)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(sub2.Stop)
	// The durable's AckWait is the server default, 30s; a give-back that
	// didn't happen would only arrive after it.
	waitUntil(t, "the two unsettled deliveries on the second subscriber", 5*time.Second, func() bool { return len(c2.snapshot()) == 2 })
	for i, want := range []string{"t2", "t3"} {
		if got := c2.snapshot()[i].Envelope().TaskID; got != want {
			t.Fatalf("second subscriber's delivery %d is %s, want %s", i, got, want)
		}
	}
}

// A delivery settled after the subscription stopped is a no-op, not an error
// that sends a second disposition.
func TestDeferredSettleAfterStopIsQuiet(t *testing.T) {
	h, worker := newHeldHarness(t, "relay")
	var c deferredCollector
	sub, err := worker.SubscribeDurableDeferred(h.ctx, h.cfg(), c.add)
	if err != nil {
		t.Fatal(err)
	}
	h.submit("t1")
	waitUntil(t, "the delivery", 5*time.Second, func() bool { return len(c.snapshot()) == 1 })
	sub.Stop()
	if err := c.snapshot()[0].Ack(); err != nil {
		t.Fatalf("an Ack after the stop's give-back: %v", err)
	}
	// The give-back's nak was the delivery's one disposition: the Ack after it
	// reaches no server, so the message is still pending for the next subscriber.
	if info := consumerInfo(t, h); info.NumAckPending != 1 {
		t.Fatalf("ack pending = %d after an Ack on a given-back delivery, want 1", info.NumAckPending)
	}
}

func consumerInfo(t *testing.T, h *heldHarness) *jetstream.ConsumerInfo {
	t.Helper()
	cons, err := h.req.JetStream().Consumer(h.ctx, h.cfg().Stream, h.cfg().Durable)
	if err != nil {
		t.Fatal(err)
	}
	info, err := cons.Info(h.ctx)
	if err != nil {
		t.Fatal(err)
	}
	return info
}

// Deliveries that reach the client while a stop is draining are held for the
// give-back, not nak'd ahead of it, so the next subscriber gets every
// delivery back in stream order: the one the handler had and the ones still
// buffered behind it.
func TestDeferredDeliveriesArrivingDuringTheDrainComeBackInOrder(t *testing.T) {
	h, first := newHeldHarness(t, "relay")
	release := make(chan struct{})
	var c1 deferredCollector
	sub1, err := first.SubscribeDurableDeferred(h.ctx, h.cfg(), func(d DeferredDelivery) {
		c1.add(d)
		if d.Envelope().TaskID == "t1" {
			<-release // the next deliveries wait in the client's buffer
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"t1", "t2", "t3"} {
		h.submit(id)
	}
	waitUntil(t, "the first delivery in the handler", 5*time.Second, func() bool { return len(c1.snapshot()) == 1 })
	stopped := make(chan struct{})
	go func() { sub1.Stop(); close(stopped) }()
	time.Sleep(200 * time.Millisecond) // the stop is draining, behind the handler
	close(release)
	<-stopped

	second, err := Connect(h.ctx, h.url, WithName("second"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(second.Close)
	var c2 deferredCollector
	sub2, err := second.SubscribeDurableDeferred(h.ctx, h.cfg(), c2.add)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(sub2.Stop)
	waitUntil(t, "all three back on the second subscriber", 5*time.Second, func() bool { return len(c2.snapshot()) == 3 })
	for i, want := range []string{"t1", "t2", "t3"} {
		if got := c2.snapshot()[i].Envelope().TaskID; got != want {
			t.Fatalf("redelivery %d is %s, want %s: the order went %v", i, got, want, taskIDs(c2.snapshot()))
		}
	}
}

func taskIDs(ds []DeferredDelivery) []string {
	out := make([]string, 0, len(ds))
	for _, d := range ds {
		out = append(out, d.Envelope().TaskID)
	}
	return out
}

// TestAFailedNakStillReleasesTheDelivery: a nak that can't reach the
// connection stops the heartbeat and the tracking, so the server redelivers
// the message after AckWait instead of a heartbeat renewing it for the life
// of the subscription.
func TestAFailedNakStillReleasesTheDelivery(t *testing.T) {
	state := &deferredState{pending: map[*deferredDelivery]struct{}{}}
	beatStopped := false
	// No connection and no reply subject: the nak fails, as it does on a dead connection.
	dd := &deferredDelivery{state: state, heldDelivery: &heldDelivery{c: &Client{}}, stopBeat: func() { beatStopped = true }}
	state.pending[dd] = struct{}{}
	if err := dd.Nak(time.Second); err == nil {
		t.Fatal("a nak with nowhere to go reported success")
	}
	if !beatStopped {
		t.Fatal("a failed nak left the heartbeat renewing the delivery")
	}
	if len(state.pending) != 0 {
		t.Fatal("a failed nak left the delivery pending")
	}
}
