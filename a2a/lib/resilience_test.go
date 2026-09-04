package lib

// The client resilience contract from the NATS deployment spec: NR-1
// (terminal vs transient), NR-2 (rebuild, never retry into a dead context),
// NR-3 (all four connection callbacks registered and logged), and the
// incident's conformance assertion, number 19 in the payload spec's suite:
// the client survives a NATS server restart and resumes delivery without a
// process restart. The server is killed at the transport level — Shutdown
// drops every client connection with no drain and no lame-duck period.

import (
	"context"
	"encoding/json"
	"log/slog"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// logCapture is a slog.Handler that records formatted lines for assertions.
type logCapture struct {
	mu    sync.Mutex
	lines []string
}

func (l *logCapture) Enabled(context.Context, slog.Level) bool { return true }

func (l *logCapture) Handle(_ context.Context, r slog.Record) error {
	var sb strings.Builder
	sb.WriteString(r.Message)
	r.Attrs(func(a slog.Attr) bool {
		sb.WriteString(" ")
		sb.WriteString(a.Key)
		sb.WriteString("=")
		sb.WriteString(a.Value.String())
		return true
	})
	l.mu.Lock()
	defer l.mu.Unlock()
	l.lines = append(l.lines, sb.String())
	return nil
}

func (l *logCapture) WithAttrs([]slog.Attr) slog.Handler { return l }
func (l *logCapture) WithGroup(string) slog.Handler      { return l }

func (l *logCapture) contains(substr string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, line := range l.lines {
		if strings.Contains(line, substr) {
			return true
		}
	}
	return false
}

// Assertion 19: the client survives a NATS server restart and resumes
// delivery without a process restart. The reconnect here is nats.go's
// transient path (NR-1): the same client object rides it out, nothing is
// rebuilt. NR-3's log evidence is asserted on the way.
func TestAssertion19_SurvivesServerRestart(t *testing.T) {
	dir := t.TempDir()
	s1 := runJetStreamServer(t, -1, dir, nil)
	port := serverPort(s1)
	url := clientURL(s1)
	provisionTasksStream(t, url)

	capture := &logCapture{}
	ctx := testCtx(t)
	c, err := Connect(ctx, url, WithName("survivor"), WithLogger(slog.New(capture)),
		WithNATSOptions(nats.ReconnectWait(50*time.Millisecond)))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c.Close()

	col := &collector{}
	_, err = c.SubscribeDurable(ctx, SubscribeConfig{
		Stream:  "TASKS",
		Subject: TaskInSubject("chatops", "task-r19"),
		Durable: "r19-consumer",
		Session: "chatops",
	}, col.handle)
	if err != nil {
		t.Fatalf("SubscribeDurable: %v", err)
	}

	pub := func(id string) {
		env, err := NewMessageEnvelope(Party{Session: "w"}, "task-r19", "ctx-1", "corr-1",
			validMessagePayload(), WithEnvelopeID(id))
		if err != nil {
			t.Fatal(err)
		}
		if err := c.Publish(ctx, TaskInSubject("chatops", "task-r19"), env); err != nil {
			t.Fatalf("publish %s: %v", id, err)
		}
	}
	pub("env-r19-before")
	waitFor(t, 5e9, "pre-restart delivery", func() bool { return col.count() == 1 })

	// Transport-level kill: no drain, no lame duck; every connection drops.
	s1.Shutdown()
	s1.WaitForShutdown()
	waitFor(t, 10e9, "disconnect logged", func() bool { return capture.contains("nats disconnected") })

	s2 := runJetStreamServer(t, port, dir, nil)
	t.Cleanup(s2.Shutdown)

	waitFor(t, 15e9, "reconnect logged", func() bool { return capture.contains("nats reconnected") })
	pub("env-r19-after")
	waitFor(t, 15e9, "post-restart delivery", func() bool { return col.count() == 2 })

	if got := c.rebuilds.Load(); got != 0 {
		t.Errorf("rebuilds = %d; a routine restart is the transient path, nothing is torn down (NR-1)", got)
	}
}

// Assertion 20: after a reconnect the consumer resumes with no gap, and
// assertion 5 (dedup) still holds. The reconnect is real - the server is
// killed at the transport level and restarted on the same port and store.
// The gap risk is a message published while the consumer is still
// disconnected, so env-a20-4 is published from a fresh connection the moment
// the restarted server accepts it, racing the consumer's reconnect; whichever
// side wins the race, the consumer must deliver it.
func TestAssertion20_ReconnectNoGapDedupHolds(t *testing.T) {
	dir := t.TempDir()
	s1 := runJetStreamServer(t, -1, dir, nil)
	port := serverPort(s1)
	url := clientURL(s1)
	provisionTasksStream(t, url)

	capture := &logCapture{}
	ctx := testCtx(t)
	c, err := Connect(ctx, url, WithName("a20"), WithLogger(slog.New(capture)),
		WithNATSOptions(nats.ReconnectWait(50*time.Millisecond)))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c.Close()

	subject := TaskInSubject("chatops", "task-a20")
	col := &collector{}
	_, err = c.SubscribeDurable(ctx, SubscribeConfig{
		Stream:  "TASKS",
		Subject: subject,
		Durable: "a20-consumer",
		Session: "chatops",
	}, col.handle)
	if err != nil {
		t.Fatalf("SubscribeDurable: %v", err)
	}

	build := func(id string) *Envelope {
		env, err := NewMessageEnvelope(Party{Session: "w"}, "task-a20", "ctx-1", "corr-1",
			validMessagePayload(), WithEnvelopeID(id))
		if err != nil {
			t.Fatal(err)
		}
		return env
	}
	pub := func(via *Client, id string) {
		if err := via.Publish(ctx, subject, build(id)); err != nil {
			t.Fatalf("publish %s: %v", id, err)
		}
	}
	pub(c, "env-a20-1")
	pub(c, "env-a20-2")
	pub(c, "env-a20-3")
	waitFor(t, 5e9, "pre-restart delivery", func() bool { return col.count() == 3 })

	s1.Shutdown()
	s1.WaitForShutdown()
	waitFor(t, 10e9, "disconnect logged", func() bool { return capture.contains("nats disconnected") })

	s2 := runJetStreamServer(t, port, dir, nil)
	t.Cleanup(s2.Shutdown)

	// Published before waiting for the consumer's reconnect: the no-gap half.
	pubC, err := Connect(ctx, clientURL(s2), WithName("a20-pub"))
	if err != nil {
		t.Fatalf("Connect publisher: %v", err)
	}
	defer pubC.Close()
	pub(pubC, "env-a20-4")

	waitFor(t, 15e9, "reconnect logged", func() bool { return capture.contains("nats reconnected") })

	// Assertion 5 across the reconnect: the same envelopeId hitting the
	// subject again is what a redelivery looks like to the application.
	// publishRaw carries no Nats-Msg-Id, so the server's dedup window cannot
	// swallow it first - the client's own set is what is under test.
	dup, err := json.Marshal(build("env-a20-2"))
	if err != nil {
		t.Fatal(err)
	}
	publishRaw(t, clientURL(s2), subject, dup)
	pub(pubC, "env-a20-5")

	waitFor(t, 15e9, "post-reconnect delivery", func() bool {
		for _, e := range col.all() {
			if e.EnvelopeID == "env-a20-5" {
				return true
			}
		}
		return false
	})

	var got []string
	for _, e := range col.all() {
		got = append(got, e.EnvelopeID)
	}
	want := []string{"env-a20-1", "env-a20-2", "env-a20-3", "env-a20-4", "env-a20-5"}
	if !slices.Equal(got, want) {
		t.Errorf("delivered %v\nwant      %v (every envelope once, in stream order: no gap, no duplicate)", got, want)
	}
	if got := c.rebuilds.Load(); got != 0 {
		t.Errorf("rebuilds = %d; a routine restart rides the transient path (NR-1)", got)
	}
}

// NR-1 terminal half and NR-2: when nats.go gives up (terminal close), the
// library rebuilds — fresh connection, fresh JetStream, durables re-subscribed
// from their specs — and never issues a call against the dead connection's
// objects.
func TestNR2_TerminalCloseRebuild(t *testing.T) {
	dir := t.TempDir()
	s1 := runJetStreamServer(t, -1, dir, nil)
	port := serverPort(s1)
	url := clientURL(s1)
	provisionTasksStream(t, url)

	capture := &logCapture{}
	ctx := testCtx(t)
	// MaxReconnects(1) with a short wait forces the terminal path quickly.
	c, err := Connect(ctx, url, WithName("terminal"), WithLogger(slog.New(capture)),
		WithNATSOptions(nats.MaxReconnects(1), nats.ReconnectWait(50*time.Millisecond)))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c.Close()

	col := &collector{}
	_, err = c.SubscribeDurable(ctx, SubscribeConfig{
		Stream:  "TASKS",
		Subject: TaskInSubject("chatops", "task-nr2"),
		Durable: "nr2-consumer",
		Session: "chatops",
	}, col.handle)
	if err != nil {
		t.Fatalf("SubscribeDurable: %v", err)
	}

	oldConn, _ := c.conn()

	s1.Shutdown()
	s1.WaitForShutdown()
	// One reconnect attempt fails against a dead port; the connection flips to
	// terminal closed and the rebuild loop starts retrying.
	waitFor(t, 10e9, "terminal close logged", func() bool { return capture.contains("nats connection closed") })

	s2 := runJetStreamServer(t, port, dir, nil)
	t.Cleanup(s2.Shutdown)
	waitFor(t, 15e9, "rebuild completes", func() bool { return c.rebuilds.Load() == 1 })

	newConn, _ := c.conn()
	if newConn == oldConn {
		t.Fatal("rebuild reused the dead connection object (NR-2 violation)")
	}
	if !oldConn.IsClosed() {
		t.Error("old connection still open after rebuild")
	}

	// Delivery resumes on the rebuilt connection through the re-subscribed
	// durable, publishing via a separate connection.
	pubC, err := Connect(ctx, clientURL(s2), WithName("nr2-pub"))
	if err != nil {
		t.Fatalf("Connect publisher: %v", err)
	}
	defer pubC.Close()
	env, err := NewMessageEnvelope(Party{Session: "w"}, "task-nr2", "ctx-1", "corr-1", validMessagePayload())
	if err != nil {
		t.Fatal(err)
	}
	if err := pubC.Publish(ctx, TaskInSubject("chatops", "task-nr2"), env); err != nil {
		t.Fatalf("publish after rebuild: %v", err)
	}
	waitFor(t, 15e9, "delivery after rebuild", func() bool { return col.count() == 1 })

	if !capture.contains("nats rebuild complete") {
		t.Error("rebuild path not logged")
	}
}

// NR-3: all four connection callbacks — disconnected, reconnected, closed,
// error — are registered at construction, and a forced disconnect produces
// the log line carrying the server error.
func TestNR3_CallbacksRegisteredAndLogged(t *testing.T) {
	s := startServer(t)
	capture := &logCapture{}
	ctx := testCtx(t)
	c, err := Connect(ctx, clientURL(s), WithName("nr3"), WithLogger(slog.New(capture)),
		WithNATSOptions(nats.ReconnectWait(50*time.Millisecond)))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c.Close()

	nc, _ := c.conn()
	_ = nc
	if nc.Opts.DisconnectedErrCB == nil {
		t.Error("DisconnectedErrCB not registered")
	}
	if nc.Opts.ReconnectedCB == nil {
		t.Error("ReconnectedCB not registered")
	}
	if nc.Opts.ClosedCB == nil {
		t.Error("ClosedCB not registered")
	}
	if nc.Opts.AsyncErrorCB == nil {
		t.Error("AsyncErrorCB not registered")
	}

	// The library's callbacks are appended after caller-supplied options, so
	// a WithNATSOptions handler cannot displace them.
	c2, err := Connect(ctx, clientURL(s), WithName("nr3-override"),
		WithNATSOptions(nats.ClosedHandler(nil), nats.DisconnectErrHandler(nil)))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c2.Close()
	nc2, _ := c2.conn()
	if nc2.Opts.ClosedCB == nil || nc2.Opts.DisconnectedErrCB == nil {
		t.Error("user-supplied nats options displaced the library's connection callbacks")
	}

	// Forced disconnect produces the log line.
	s.Shutdown()
	s.WaitForShutdown()
	waitFor(t, 10e9, "disconnect log line", func() bool { return capture.contains("nats disconnected") })
}

// A rebuild must not declare success while a durable failed to re-subscribe:
// after a restart the server can accept connections before streams recover,
// and abandoning the durable then is the silently-deaf-consumer incident. The
// restarted server here comes up with an empty store - no TASKS stream - so
// re-subscribe fails until the stream is provisioned again.
func TestNR2_RebuildRetriesSubscribe(t *testing.T) {
	s1 := runJetStreamServer(t, -1, t.TempDir(), nil)
	port := serverPort(s1)
	url := clientURL(s1)
	provisionTasksStream(t, url)

	capture := &logCapture{}
	ctx := testCtx(t)
	c, err := Connect(ctx, url, WithName("retry-sub"), WithLogger(slog.New(capture)),
		WithNATSOptions(nats.MaxReconnects(1), nats.ReconnectWait(50*time.Millisecond)))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c.Close()

	col := &collector{}
	_, err = c.SubscribeDurable(ctx, SubscribeConfig{
		Stream:  "TASKS",
		Subject: TaskInSubject("chatops", "task-rr"),
		Durable: "rr-consumer",
		Session: "chatops",
	}, col.handle)
	if err != nil {
		t.Fatalf("SubscribeDurable: %v", err)
	}

	s1.Shutdown()
	s1.WaitForShutdown()
	waitFor(t, 10e9, "terminal close logged", func() bool { return capture.contains("nats connection closed") })

	// Same port, fresh store: the connection dial succeeds but the TASKS
	// stream does not exist, so the durable cannot come back yet.
	s2 := runJetStreamServer(t, port, t.TempDir(), nil)
	t.Cleanup(s2.Shutdown)
	waitFor(t, 10e9, "re-subscribe failure logged", func() bool {
		return capture.contains("re-subscribe failed")
	})
	if got := c.rebuilds.Load(); got != 0 {
		t.Fatalf("rebuild declared complete (%d) while its durable is dead", got)
	}

	provisionTasksStream(t, clientURL(s2))
	waitFor(t, 15e9, "rebuild completes after stream returns", func() bool { return c.rebuilds.Load() == 1 })

	pubC, err := Connect(ctx, clientURL(s2), WithName("rr-pub"))
	if err != nil {
		t.Fatalf("Connect publisher: %v", err)
	}
	defer pubC.Close()
	env, err := NewMessageEnvelope(Party{Session: "w"}, "task-rr", "ctx-1", "corr-1", validMessagePayload())
	if err != nil {
		t.Fatal(err)
	}
	if err := pubC.Publish(ctx, TaskInSubject("chatops", "task-rr"), env); err != nil {
		t.Fatalf("publish after recovery: %v", err)
	}
	waitFor(t, 15e9, "delivery after recovery", func() bool { return col.count() == 1 })
}

// NR-6: randomized exponential backoff with full jitter on all connection and
// reconnection attempts - a restart must not turn every client into one
// synchronized thundering herd.
func TestNR6_JitteredBackoff(t *testing.T) {
	t.Run("registered_on_connection", func(t *testing.T) {
		s := startServer(t)
		ctx := testCtx(t)
		c, err := Connect(ctx, clientURL(s), WithName("nr6"))
		if err != nil {
			t.Fatalf("Connect: %v", err)
		}
		defer c.Close()
		nc, _ := c.conn()
		if nc.Opts.CustomReconnectDelayCB == nil {
			t.Error("custom reconnect delay (jittered backoff) not registered")
		}
	})

	t.Run("attempts_spread_not_aligned", func(t *testing.T) {
		const attempt = 4
		seen := map[time.Duration]int{}
		for i := 0; i < 64; i++ {
			d := fullJitterBackoff(attempt)
			if d < 0 || d >= backoffCap {
				t.Fatalf("backoff(%d) = %v, outside [0, %v)", attempt, d, backoffCap)
			}
			if ceil := backoffBase << (attempt - 1); d >= ceil && ceil < backoffCap {
				t.Fatalf("backoff(%d) = %v, above the exponential ceiling %v", attempt, d, ceil)
			}
			seen[d]++
		}
		if len(seen) < 8 {
			t.Errorf("64 samples produced only %d distinct delays; full jitter must spread, not align", len(seen))
		}
	})

	t.Run("large_attempt_capped_no_overflow", func(t *testing.T) {
		for i := 0; i < 32; i++ {
			if d := fullJitterBackoff(200); d < 0 || d >= backoffCap {
				t.Fatalf("backoff(200) = %v, outside [0, %v)", d, backoffCap)
			}
		}
	})
}
