package lib

import (
	"log/slog"
	"sync/atomic"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

// A core subscription is re-bound by a rebuild the way a durable is. nats.go
// restores core subscriptions across an ordinary reconnect by itself; this is
// the terminal-close path, where the subscription's connection is gone for
// good and only the client's own record of it can bring it back. Without
// resubscribeCore the request below is never answered.
func TestSubscribeCoreSurvivesARebuild(t *testing.T) {
	dir := t.TempDir()
	s1 := runJetStreamServer(t, -1, dir, nil)
	port := serverPort(s1)
	url := clientURL(s1)

	capture := &logCapture{}
	ctx := testCtx(t)
	c, err := Connect(ctx, url, WithName("core-rebuild"), WithLogger(slog.New(capture)),
		WithNATSOptions(nats.MaxReconnects(1), nats.ReconnectWait(50*time.Millisecond)))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c.Close()

	var heard atomic.Int32
	sub, err := c.SubscribeCore("core.rebuild.test", func(m *nats.Msg) {
		heard.Add(1)
		_ = m.Respond([]byte("ok"))
	})
	if err != nil {
		t.Fatalf("SubscribeCore: %v", err)
	}
	defer sub.Stop()

	s1.Shutdown()
	s1.WaitForShutdown()
	waitFor(t, 10e9, "terminal close logged", func() bool { return capture.contains("nats connection closed") })
	s2 := runJetStreamServer(t, port, dir, nil)
	t.Cleanup(s2.Shutdown)
	waitFor(t, 15e9, "rebuild completes", func() bool { return c.rebuilds.Load() == 1 })

	other, err := nats.Connect(clientURL(s2))
	if err != nil {
		t.Fatalf("connect requester: %v", err)
	}
	defer other.Close()
	if _, err := other.Request("core.rebuild.test", []byte("ping"), 5*time.Second); err != nil {
		t.Fatalf("request after rebuild: %v (the core subscription was not re-bound)", err)
	}
	if got := heard.Load(); got != 1 {
		t.Errorf("handler ran %d times, want 1", got)
	}
}

// Stop takes the subscription out of the rebuild set, so a stopped listener
// does not come back to life on the next rebuild.
func TestSubscribeCoreStopLeavesTheRebuildSet(t *testing.T) {
	s := runJetStreamServer(t, -1, t.TempDir(), nil)
	ctx := testCtx(t)
	c, err := Connect(ctx, clientURL(s), WithName("core-stop"))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c.Close()
	sub, err := c.SubscribeCore("core.stop.test", func(*nats.Msg) {})
	if err != nil {
		t.Fatalf("SubscribeCore: %v", err)
	}
	sub.Stop()
	c.mu.Lock()
	n := len(c.cores)
	c.mu.Unlock()
	if n != 0 {
		t.Errorf("cores = %d after Stop, want 0", n)
	}
}

// A second bind releases the first: a message is delivered once, not once per
// binding. Re-binding without releasing is how a rebuild that re-dials while
// the old connection is still up would double every delivery.
func TestARebindReleasesTheEarlierBinding(t *testing.T) {
	s := runJetStreamServer(t, -1, t.TempDir(), nil)
	ctx := testCtx(t)
	c, err := Connect(ctx, clientURL(s), WithName("core-rebind"))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c.Close()
	var heard atomic.Int32
	sub, err := c.SubscribeCore("core.rebind.test", func(*nats.Msg) { heard.Add(1) })
	if err != nil {
		t.Fatal(err)
	}
	defer sub.Stop()
	nc, _ := c.conn()
	if err := sub.(*coreSub).start(nc); err != nil {
		t.Fatalf("second bind: %v", err)
	}
	other, err := nats.Connect(clientURL(s))
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	if err := other.Publish("core.rebind.test", []byte("x")); err != nil {
		t.Fatal(err)
	}
	_ = other.Flush()
	time.Sleep(300 * time.Millisecond)
	if got := heard.Load(); got != 1 {
		t.Errorf("delivered %d times, want 1", got)
	}
}

// A rebuild binds core subscriptions before durables: they need no
// JetStream, so a stream that has not recovered (here, a server with no
// JetStream at all) must not hold them unbound on a live connection.
func TestARebuildBindsCoreSubscriptionsBeforeDurables(t *testing.T) {
	dir := t.TempDir()
	s1 := runJetStreamServer(t, -1, dir, nil)
	port := serverPort(s1)
	provisionTasksStream(t, clientURL(s1))

	capture := &logCapture{}
	ctx := testCtx(t)
	c, err := Connect(ctx, clientURL(s1), WithName("core-first"), WithLogger(slog.New(capture)),
		WithNATSOptions(nats.MaxReconnects(1), nats.ReconnectWait(50*time.Millisecond)))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c.Close()
	if _, err := c.SubscribeDurable(ctx, SubscribeConfig{
		Stream: "TASKS", Subject: TaskInSubject("chatops", "task-core-first"),
		Durable: "core-first", Session: "chatops",
	}, func(*Envelope) {}); err != nil {
		t.Fatalf("SubscribeDurable: %v", err)
	}
	sub, err := c.SubscribeCore("core.first.test", func(m *nats.Msg) { _ = m.Respond([]byte("ok")) })
	if err != nil {
		t.Fatalf("SubscribeCore: %v", err)
	}
	defer sub.Stop()

	s1.Shutdown()
	s1.WaitForShutdown()
	waitFor(t, 10e9, "terminal close logged", func() bool { return capture.contains("nats connection closed") })
	s2 := runJetStreamServer(t, port, dir, func(o *natsserver.Options) { o.JetStream = false })
	t.Cleanup(s2.Shutdown)
	waitFor(t, 15e9, "the durable's re-subscribe failing", func() bool {
		return capture.contains("nats rebuild re-subscribe failed")
	})

	other, err := nats.Connect(clientURL(s2))
	if err != nil {
		t.Fatalf("connect requester: %v", err)
	}
	defer other.Close()
	if _, err := other.Request("core.first.test", []byte("ping"), 5*time.Second); err != nil {
		t.Fatalf("request while the durable cannot come back: %v (core bind held behind it)", err)
	}
}

// A stopped core subscription in a rebuild's snapshot is skipped, not logged
// as re-subscribed: nothing is bound for it.
func TestARebuildSkipsAStoppedCoreSubscription(t *testing.T) {
	s := runJetStreamServer(t, -1, t.TempDir(), nil)
	capture := &logCapture{}
	ctx := testCtx(t)
	c, err := Connect(ctx, clientURL(s), WithName("core-stopped"), WithLogger(slog.New(capture)))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c.Close()
	stopped := &coreSub{c: c, subject: "core.stopped.test", handler: func(*nats.Msg) {}}
	stopped.stopped.Store(true)
	nc, _ := c.conn()
	if !c.resubscribeCore([]*coreSub{stopped}, nc) {
		t.Fatal("resubscribeCore reported the live connection dead")
	}
	if capture.contains("re-subscribed") {
		t.Errorf("a stopped subscription was logged as re-subscribed:\n%s", capture.String())
	}
}
