package lib

// Stop and the rebind paths of a held subscription (#4849): a stop during a
// reconnect, and a rebind that races a stop, still hand the task on at once
// rather than after AckWait. The tests here were written in #4881's review.

import (
	"context"
	"fmt"
	"io"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

type cutProxy struct {
	ln     net.Listener
	target string
	mu     sync.Mutex
	conns  []net.Conn
	paused bool
}

func newCutProxy(t *testing.T, target string) *cutProxy {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	p := &cutProxy{ln: ln, target: target}
	t.Cleanup(func() { ln.Close(); p.cut() })
	go p.serve()
	return p
}

func (p *cutProxy) url() string { return "nats://" + p.ln.Addr().String() }

func (p *cutProxy) serve() {
	for {
		c, err := p.ln.Accept()
		if err != nil {
			return
		}
		p.mu.Lock()
		if p.paused {
			p.mu.Unlock()
			c.Close()
			continue
		}
		u, err := net.Dial("tcp", p.target)
		if err != nil {
			p.mu.Unlock()
			c.Close()
			continue
		}
		p.conns = append(p.conns, c, u)
		p.mu.Unlock()
		go func() { _, _ = io.Copy(u, c); u.Close(); c.Close() }()
		go func() { _, _ = io.Copy(c, u); u.Close(); c.Close() }()
	}
}

func (p *cutProxy) cut() {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, c := range p.conns {
		c.Close()
	}
	p.conns = nil
}

func (p *cutProxy) pause()  { p.mu.Lock(); p.paused = true; p.mu.Unlock(); p.cut() }
func (p *cutProxy) resume() { p.mu.Lock(); p.paused = false; p.mu.Unlock() }

func TestHeldStopWhileReconnectingHandsBackAtOnce(t *testing.T) {
	h, _ := newHeldHarness(t, "reconnstop")
	h.ensure(1, time.Minute)
	target := h.url[len("nats://"):]
	direct, err := Connect(h.ctx, h.url, WithName("direct"))
	if err != nil {
		t.Fatalf("Connect direct: %v", err)
	}
	t.Cleanup(direct.Close)
	for round := 0; round < 5; round++ {
		proxy := newCutProxy(t, target)
		x, err := Connect(h.ctx, proxy.url(), WithName("proxied"),
			WithNATSOptions(nats.ReconnectWait(50*time.Millisecond)))
		if err != nil {
			t.Fatalf("Connect proxied: %v", err)
		}
		task := "task-rc"
		if round == 0 {
			h.submit(task)
		}
		held := make(chan struct{}, 1)
		sub, err := x.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
			select {
			case held <- struct{}{}:
			default:
			}
			<-ctx.Done()
		})
		if err != nil {
			t.Fatalf("round %d: SubscribeHeld: %v", round, err)
		}
		select {
		case <-held:
		case <-time.After(5 * time.Second):
			t.Fatalf("round %d: the task wasn't handed over within 5s", round)
		}
		proxy.pause()
		nc, _ := x.conn()
		deadline := time.Now().Add(5 * time.Second)
		for nc.Status() != nats.RECONNECTING {
			if time.Now().After(deadline) {
				t.Fatalf("round %d: never saw RECONNECTING (status %v)", round, nc.Status())
			}
			time.Sleep(5 * time.Millisecond)
		}
		sub.Stop()
		// Past heldDrainTimeout (heldDrainTimeout): the
		// handler is cancelled and its Nak(0) waits in the reconnect buffer.
		wctx, cancel := context.WithTimeout(h.ctx, 5*time.Second)
		err = sub.Wait(wctx)
		cancel()
		if err != nil {
			t.Fatalf("round %d: Wait: %v", round, err)
		}
		got := make(chan struct{}, 4)
		next, err := direct.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
			select {
			case got <- struct{}{}:
			default:
			}
			<-ctx.Done()
		})
		if err != nil {
			t.Fatalf("round %d: SubscribeHeld next: %v", round, err)
		}
		time.Sleep(200 * time.Millisecond) // next's pull is queued behind the stopped one
		proxy.resume()
		select {
		case <-got:
		case <-time.After(5 * time.Second):
			next.Stop()
			t.Fatalf("round %d: the task wasn't handed back within 5s of the reconnect (AckWait is a minute)", round)
		}
		next.Stop()
		wctx, cancel = context.WithTimeout(h.ctx, 5*time.Second)
		_ = next.Wait(wctx)
		cancel()
		x.Close()
	}
}

// The same, but the reconnect lands inside Stop's drain window, before the
// server prunes the stopped pull's waiting request.
func TestHeldStopJustBeforeAReconnectHandsBackAtOnce(t *testing.T) {
	h, _ := newHeldHarness(t, "reconnfast")
	h.ensure(1, time.Minute)
	target := h.url[len("nats://"):]
	direct, err := Connect(h.ctx, h.url, WithName("direct"))
	if err != nil {
		t.Fatalf("Connect direct: %v", err)
	}
	t.Cleanup(direct.Close)
	for round := 0; round < 1; round++ {
		proxy := newCutProxy(t, target)
		x, err := Connect(h.ctx, proxy.url(), WithName("proxied"),
			WithNATSOptions(nats.ReconnectWait(50*time.Millisecond)))
		if err != nil {
			t.Fatalf("Connect proxied: %v", err)
		}
		task := "task-rc"
		if round == 0 {
			h.submit(task)
		}
		held := make(chan struct{}, 1)
		sub, err := x.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
			select {
			case held <- struct{}{}:
			default:
			}
			<-ctx.Done()
		})
		if err != nil {
			t.Fatalf("round %d: SubscribeHeld: %v", round, err)
		}
		select {
		case <-held:
		case <-time.After(5 * time.Second):
			t.Fatalf("round %d: the task wasn't handed over within 5s", round)
		}
		proxy.pause()
		nc, _ := x.conn()
		deadline := time.Now().Add(5 * time.Second)
		for nc.Status() != nats.RECONNECTING {
			if time.Now().After(deadline) {
				t.Fatalf("round %d: never saw RECONNECTING (status %v)", round, nc.Status())
			}
			time.Sleep(5 * time.Millisecond)
		}
		sub.Stop()
		proxy.resume() // the reconnect lands inside the drain window
		// Past heldDrainTimeout (heldDrainTimeout): the
		// handler is cancelled and its Nak(0) waits in the reconnect buffer.
		wctx, cancel := context.WithTimeout(h.ctx, 5*time.Second)
		err = sub.Wait(wctx)
		cancel()
		if err != nil {
			t.Fatalf("round %d: Wait: %v", round, err)
		}
		got := make(chan struct{}, 4)
		next, err := direct.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
			select {
			case got <- struct{}{}:
			default:
			}
			<-ctx.Done()
		})
		if err != nil {
			t.Fatalf("round %d: SubscribeHeld next: %v", round, err)
		}
		select {
		case <-got:
		case <-time.After(5 * time.Second):
			next.Stop()
			_, djs := direct.conn()
			info := "?"
			if cons, err := djs.Consumer(h.ctx, "TASKS", h.cfg().Durable); err == nil {
				if ci, err := cons.Info(h.ctx); err == nil {
					info = fmt.Sprintf("ackPending=%d waiting=%d redelivered=%d pending=%d", ci.NumAckPending, ci.NumWaiting, ci.NumRedelivered, ci.NumPending)
				}
			}
			t.Fatalf("round %d: the task wasn't handed back within 5s of the reconnect (AckWait is a minute); consumer: %s", round, info)
		}
		next.Stop()
		wctx, cancel = context.WithTimeout(h.ctx, 5*time.Second)
		_ = next.Wait(wctx)
		cancel()
		x.Close()
	}
}

func registered(c *Client, s *durableSub) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	for _, x := range c.subs {
		if x == s {
			return true
		}
	}
	return false
}

func waitUnregistered(t *testing.T, c *Client, s *durableSub) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for registered(c, s) {
		if time.Now().After(deadline) {
			t.Fatalf("held sub still registered 5s after Stop")
		}
		time.Sleep(time.Millisecond)
	}
}

// A rebind that lands after Stop pulls a pending task on its fresh consume
// context; the stopped branch must hand it back, not drop it.
func TestHeldRebindAfterStopHandsBackTheInFlightTask(t *testing.T) {
	h, first := newHeldHarness(t, "rebindback")
	h.ensure(1, time.Minute)
	second, err := Connect(h.ctx, h.url, WithName("second"))
	if err != nil {
		t.Fatalf("Connect second: %v", err)
	}
	t.Cleanup(second.Close)
	holders := []*Client{first, second}
	for round := 0; round < 50; round++ {
		x, y := holders[round%2], holders[(round+1)%2]
		sub, err := x.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
			t.Errorf("round %d: the stopped sub's handler ran", round)
		})
		if err != nil {
			t.Fatalf("round %d: SubscribeHeld: %v", round, err)
		}
		s := sub.(*heldSubscription).s
		_, js := x.conn()
		cons, err := js.Consumer(h.ctx, "TASKS", h.cfg().Durable)
		if err != nil {
			t.Fatalf("round %d: Consumer: %v", round, err)
		}
		sub.Stop()
		waitUnregistered(t, x, s)
		task := fmt.Sprintf("task-rb-%d", round)
		h.submit(task)
		// The rebind lands now: its initial pull takes the pending task.
		if err := s.consume(cons); err != nil {
			t.Fatalf("round %d: consume: %v", round, err)
		}
		got := make(chan string, 4)
		next, err := y.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
			got <- d.Envelope().TaskID
			_ = d.Ack()
		})
		if err != nil {
			t.Fatalf("round %d: SubscribeHeld next: %v", round, err)
		}
		select {
		case id := <-got:
			if id != task {
				t.Fatalf("round %d: got %s, want %s", round, id, task)
			}
		case <-time.After(5 * time.Second):
			next.Stop()
			t.Fatalf("round %d: the task the rebind pulled wasn't handed back within 5s (AckWait is a minute)", round)
		}
		next.Stop()
		wctx, cancel := context.WithTimeout(h.ctx, 5*time.Second)
		err = next.Wait(wctx)
		cancel()
		if err != nil {
			t.Fatalf("round %d: Wait: %v", round, err)
		}
	}
}

// A rebind inside Stop's drain window: awaitDrain must follow the new
// context, cancel the handlers only once it is drained, and the held task
// must reach the next holder at once.
func TestHeldRebindDuringStopDrainCancelsAfterTheNewContextDrains(t *testing.T) {
	h, first := newHeldHarness(t, "rebinddrain")
	h.ensure(1, time.Minute)
	second, err := Connect(h.ctx, h.url, WithName("second"))
	if err != nil {
		t.Fatalf("Connect second: %v", err)
	}
	t.Cleanup(second.Close)
	holders := []*Client{first, second}
	h.submit("task-rd")
	for round := 0; round < 50; round++ {
		x := holders[round%2]
		held := make(chan struct{}, 1)
		early := make(chan string, 1)
		var s *durableSub
		ready := make(chan struct{})
		sub, err := x.SubscribeHeld(h.ctx, h.cfg(), func(ctx context.Context, d Delivery) {
			select {
			case held <- struct{}{}:
			default:
			}
			<-ctx.Done()
			<-ready
			s.mu.Lock()
			cc := s.cc
			s.mu.Unlock()
			select {
			case <-cc.Closed():
			default:
				early <- "handler cancelled before the current consume context drained"
			}
		})
		if err != nil {
			t.Fatalf("round %d: SubscribeHeld: %v", round, err)
		}
		s = sub.(*heldSubscription).s
		close(ready)
		select {
		case <-held:
		case <-time.After(5 * time.Second):
			sub.Stop()
			t.Fatalf("round %d: the task wasn't handed over within 5s (AckWait is a minute)", round)
		}
		_, js := x.conn()
		cons, err := js.Consumer(h.ctx, "TASKS", h.cfg().Durable)
		if err != nil {
			t.Fatalf("round %d: Consumer: %v", round, err)
		}
		sub.Stop()
		if err := s.consume(cons); err != nil {
			t.Fatalf("round %d: consume: %v", round, err)
		}
		wctx, cancel := context.WithTimeout(h.ctx, 5*time.Second)
		err = sub.Wait(wctx)
		cancel()
		if err != nil {
			t.Fatalf("round %d: Wait after Stop: %v", round, err)
		}
		select {
		case msg := <-early:
			t.Fatalf("round %d: %s", round, msg)
		default:
		}
		waitUnregistered(t, x, s)
	}
}
