package capability

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// The reach window, on a real bus: Check rides out a verifier it cannot reach
// for DefaultReachWindow, and only that. gke-labs/kube-agents#3270 is the
// shape under test: after a bus restart the executor reconnected 3.75s before
// the verifiers did, and a task that landed in the gap was refused 2ms after
// it was accepted, on a no-responders reply.

const (
	// verifierGap is how long no verifier is subscribed before one is, in
	// the gap test: roughly the gap #3270 observed, inside the window.
	verifierGap = 3 * time.Second
	// shortWindow is the reach window for the tests that need it to run
	// out, and shortAsk one ask's timeout there.
	shortWindow = time.Second
	shortAsk    = 200 * time.Millisecond
	// cancelAfter is when the cancel test cancels, well inside the window.
	cancelAfter = 300 * time.Millisecond
	// promptly bounds how long a check that should end at once may take:
	// a few round trips on a local server, and well under the first
	// backoff's worth of a second ask.
	promptly = reachBackoffInitial
	// slack is the scheduling margin added to a bound a test expects the
	// check to end at.
	slack = 500 * time.Millisecond
)

// verifierOn subscribes a verifier for store s on its own connection to the
// server nc is connected to, the way the verifier Deployment is a separate
// client of the bus.
func verifierOn(t *testing.T, nc *nats.Conn, s Store) {
	t.Helper()
	vc, err := nats.Connect(nc.ConnectedUrl())
	if err != nil {
		t.Errorf("verifier connect: %v", err)
		return
	}
	t.Cleanup(vc.Close)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	if _, err := (&Service{Resolver: &Resolver{Store: s}}).Subscribe(ctx, vc); err != nil {
		t.Errorf("verifier subscribe: %v", err)
	}
}

// countAsks counts every request that reaches the verify subject, from a
// plain (non-queue) subscription beside the verifier's queue group, so it
// sees each ask once whether or not anything answers it.
func countAsks(t *testing.T, nc *nats.Conn) *atomic.Int32 {
	t.Helper()
	var n atomic.Int32
	sub, err := nc.Subscribe(VerifySubscribe, func(*nats.Msg) { n.Add(1) })
	if err != nil {
		t.Fatalf("count subscribe: %v", err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
	if err := nc.Flush(); err != nil {
		t.Fatalf("flush: %v", err)
	}
	return &n
}

// syncBuffer is a log sink the client can write while the test reads.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// The #3270 gap: nothing subscribed on the verify subject for three seconds,
// then a verifier is. On the code before the reach window this is refused
// "the verifier could not be reached" within milliseconds, on the first
// no-responders reply.
func TestAVerifierThatComesBackInsideTheReachWindowIsWaitedFor(t *testing.T) {
	s, _, hop := twoHop(t)
	nc := runServer(t)
	c := clientAs(t, nc, podB)

	timer := time.AfterFunc(verifierGap, func() { verifierOn(t, nc, s) })
	t.Cleanup(func() { timer.Stop() })

	start := time.Now()
	err := c.Check(context.Background(), hop, VerbTaskExecute, "project/P/cluster/C")
	if err != nil {
		t.Fatalf("refused %s in, with a verifier due at %s: %v", time.Since(start), verifierGap, err)
	}
	if elapsed := time.Since(start); elapsed < verifierGap {
		t.Fatalf("permitted at %s, before any verifier was subscribed", elapsed)
	}
}

// The line it logs when the wait is what let a check through, so a reconnect
// gap the window rode out shows up in the executor's log.
func TestARescuedCheckSaysSo(t *testing.T) {
	s, _, hop := twoHop(t)
	nc := runServer(t)
	c := clientAs(t, nc, podB)
	var buf syncBuffer
	c.Log = slog.New(slog.NewTextHandler(&buf, nil))

	timer := time.AfterFunc(cancelAfter, func() { verifierOn(t, nc, s) })
	t.Cleanup(func() { timer.Stop() })

	if err := c.Check(context.Background(), hop, VerbTaskExecute, "project/P/cluster/C"); err != nil {
		t.Fatalf("refused: %v", err)
	}
	logged := buf.String()
	if !strings.Contains(logged, "capability check reached the verifier after a retry") ||
		!strings.Contains(logged, "allowed=true") {
		t.Fatalf("no rescue line: %q", logged)
	}
}

// A first-time answer logs nothing: the line is for the retry, not the check.
func TestAFirstTimeAnswerLogsNoRescue(t *testing.T) {
	s, _, hop := twoHop(t)
	nc := serveVerifier(t, s)
	c := clientAs(t, nc, podB)
	var buf syncBuffer
	c.Log = slog.New(slog.NewTextHandler(&buf, nil))
	if err := c.Check(context.Background(), hop, VerbTaskExecute, "project/P/cluster/C"); err != nil {
		t.Fatalf("refused: %v", err)
	}
	if logged := buf.String(); logged != "" {
		t.Fatalf("logged on a first-time answer: %q", logged)
	}
}

// A verifier that stays away is refused with the reason it always was, once
// the window is spent and not before.
func TestAVerifierAwayPastTheWindowIsRefusedAsUnreachable(t *testing.T) {
	nc := runServer(t) // no verifier, ever
	c := clientAs(t, nc, podB)
	c.Timeout, c.ReachWindow = shortAsk, shortWindow

	start := time.Now()
	err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P")
	elapsed := time.Since(start)
	mustRefuse(t, err, "the verifier could not be reached")
	if elapsed < shortWindow {
		t.Errorf("refused at %s, inside the %s window", elapsed, shortWindow)
	}
	if elapsed > shortWindow+slack {
		t.Errorf("refused at %s, well past the %s window", elapsed, shortWindow)
	}
}

// An ask that times out is asked again too: something subscribed that does not
// answer (a queue member on a connection the server has not yet noticed is
// dead, say) is as unreachable as nothing subscribed.
func TestASilentVerifierIsAskedAgainInsideTheWindow(t *testing.T) {
	nc := runServer(t)
	asks := countAsks(t, nc) // subscribed, never answers
	c := clientAs(t, nc, podB)
	c.Timeout, c.ReachWindow = shortAsk, shortWindow

	start := time.Now()
	err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P")
	elapsed := time.Since(start)
	mustRefuse(t, err, "the verifier could not be reached")
	if elapsed > shortWindow+slack {
		t.Errorf("refused at %s, well past the %s window", elapsed, shortWindow)
	}
	if n := asks.Load(); n < 2 {
		t.Errorf("asked %d times inside the window; a timeout was not retried", n)
	}
}

// A negative window is one ask, the behaviour before the window existed.
func TestANegativeWindowAsksOnce(t *testing.T) {
	nc := runServer(t)
	c := clientAs(t, nc, podB)
	c.Timeout, c.ReachWindow = shortAsk, -1
	asks := countAsks(t, nc)

	start := time.Now()
	err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P")
	mustRefuse(t, err, "the verifier could not be reached")
	if elapsed := time.Since(start); elapsed > shortAsk+slack {
		t.Errorf("took %s with retries off", elapsed)
	}
	if n := asks.Load(); n != 1 {
		t.Errorf("asked %d times with retries off", n)
	}
}

// A deny is an answer. It is refused at once and asked exactly once: the
// window is for a verifier that is not there, not a second opinion.
func TestADenyIsRefusedAtOnceAndNotRetried(t *testing.T) {
	s, _, hop := twoHop(t)
	nc := serveVerifier(t, s)
	asks := countAsks(t, nc)
	c := clientAs(t, nc, podEvil) // the hop names podB; podEvil holds a reference it was not handed

	start := time.Now()
	err := c.Check(context.Background(), hop, VerbTaskExecute, "project/P/cluster/C")
	elapsed := time.Since(start)
	mustRefuse(t, err, WalkRefused)
	if elapsed > promptly {
		t.Errorf("a deny took %s; it waited as if the verifier were away", elapsed)
	}
	// Give a retry, had there been one, time to reach the counter.
	time.Sleep(reachBackoffMax)
	if n := asks.Load(); n != 1 {
		t.Errorf("a deny was asked %d times", n)
	}
}

// An answer that does not parse is an answer too: something is subscribed and
// replying, and asking it again will not make it make sense.
func TestAMalformedAnswerIsNotRetried(t *testing.T) {
	nc := runServer(t)
	sub, err := nc.QueueSubscribe(VerifySubscribe, VerifyQueue, func(m *nats.Msg) {
		_ = m.Respond([]byte("not json"))
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
	asks := countAsks(t, nc)
	c := clientAs(t, nc, podB)

	err = c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P")
	mustRefuse(t, err, "the verifier's answer was not well-formed")
	time.Sleep(reachBackoffMax)
	if n := asks.Load(); n != 1 {
		t.Errorf("a malformed answer was asked %d times", n)
	}
}

// A cancel inside the wait ends it at once, with the unreachable refusal, so
// a caller that checks ctx.Err() still tells "stopped" from "refused".
func TestACancelDuringTheReachWaitEndsTheCheck(t *testing.T) {
	nc := runServer(t)
	c := clientAs(t, nc, podB)
	ctx, cancel := context.WithCancel(context.Background())
	timer := time.AfterFunc(cancelAfter, cancel)
	t.Cleanup(func() { timer.Stop(); cancel() })

	start := time.Now()
	err := c.Check(ctx, Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P")
	elapsed := time.Since(start)
	mustRefuse(t, err, "the verifier could not be reached")
	if ctx.Err() == nil {
		t.Fatal("returned before the cancel")
	}
	if elapsed > cancelAfter+slack {
		t.Errorf("returned %s after a cancel at %s; the wait did not see it", elapsed, cancelAfter)
	}
}

// The caller's own deadline caps the window: a task with less time left than
// the window does not wait past it.
func TestTheCallersDeadlineCapsTheWindow(t *testing.T) {
	nc := runServer(t)
	c := clientAs(t, nc, podB)
	ctx, cancel := context.WithTimeout(context.Background(), cancelAfter)
	t.Cleanup(cancel)

	start := time.Now()
	err := c.Check(ctx, Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P")
	mustRefuse(t, err, "the verifier could not be reached")
	if elapsed := time.Since(start); elapsed > cancelAfter+slack {
		t.Errorf("waited %s past a %s deadline", elapsed, cancelAfter)
	}
}

// A closed connection will not come back, so there is nothing to wait for.
func TestAClosedConnectionIsNotWaitedOn(t *testing.T) {
	nc := runServer(t)
	c := clientAs(t, nc, podB)
	nc.Close()

	start := time.Now()
	err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P")
	mustRefuse(t, err, "the verifier could not be reached")
	if elapsed := time.Since(start); elapsed > promptly {
		t.Errorf("waited %s on a closed connection", elapsed)
	}
}

// slowAnswer is how long the slow verifier takes to answer the first ask:
// longer than one ask's timeout, inside the window.
const slowAnswer = shortAsk + 100*time.Millisecond

// A verifier that answers after the ask's own timeout still gets its verdict
// used. Every ask of one check shares a reply subscription, so the late answer
// to the first ask is read while the second is waiting, rather than dropped
// with the first ask's subscription and asked for again from a verifier that
// may be slow because it is loaded.
func TestALateAnswerToAnEarlierAskIsUsed(t *testing.T) {
	nc := runServer(t)
	var asks atomic.Int32
	sub, err := nc.QueueSubscribe(VerifySubscribe, VerifyQueue, func(m *nats.Msg) {
		if asks.Add(1) != 1 {
			return // only the first ask is ever answered, and late
		}
		reply := m.Reply
		time.AfterFunc(slowAnswer, func() { _ = nc.Publish(reply, []byte(`{"allowed":true}`)) })
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
	c := clientAs(t, nc, podB)
	c.Timeout, c.ReachWindow = shortAsk, shortWindow

	if err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P"); err != nil {
		t.Fatalf("the late answer to the first ask was dropped (%d asks): %v", asks.Load(), err)
	}
}
