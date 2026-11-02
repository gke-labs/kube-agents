package lib

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// shortenStreamWait shrinks the bind window and the wait's log and warning
// intervals for one test, so a wait past the window takes a second, not a
// minute.
func shortenStreamWait(t *testing.T, window, logEvery, warnEvery time.Duration) {
	t.Helper()
	oldWindow, oldLog, oldWarn, oldBackoff := subscribeBindWindow, streamWaitLogInterval, streamWaitWarnEvery, bindBackoff
	subscribeBindWindow, streamWaitLogInterval, streamWaitWarnEvery = window, logEvery, warnEvery
	t.Cleanup(func() {
		subscribeBindWindow, streamWaitLogInterval, streamWaitWarnEvery, bindBackoff = oldWindow, oldLog, oldWarn, oldBackoff
	})
}

// fixBindBackoff spaces the bind's attempts evenly for one test, so a
// count of what the wait logged does not depend on the jitter.
func fixBindBackoff(d time.Duration) {
	bindBackoff = func(int) time.Duration { return d }
}

func createTasksStream(t *testing.T, js jetstream.JetStream, subjects ...string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if len(subjects) == 0 {
		subjects = []string{"a2a.tasks.>"}
	}
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{Name: TasksStream, Subjects: subjects}); err != nil {
		t.Fatalf("create %s: %v", TasksStream, err)
	}
}

func (l *logCapture) count(substr string) int {
	l.mu.Lock()
	defer l.mu.Unlock()
	n := 0
	for _, line := range l.lines {
		if strings.Contains(line, substr) {
			n++
		}
	}
	return n
}

// With WaitForStream, a stream that appears well after the bind window has
// closed is still bound to: the first bind waited for it instead of failing,
// and said so in the log.
func TestSubscribeDurable_WaitForStreamBindsAStreamThatAppearsAfterTheWindow(t *testing.T) {
	shortenStreamWait(t, 300*time.Millisecond, time.Hour, time.Hour)
	s := startServer(t)
	capture := &logCapture{}
	c, err := Connect(context.Background(), clientURL(s), WithName("relay"), WithLogger(slog.New(capture)))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	admin, err := Connect(context.Background(), clientURL(s), WithName("provision"))
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	bound := make(chan error, 1)
	go func() {
		sub, err := c.SubscribeDurable(ctx, SubscribeConfig{
			Stream: TasksStream, Subject: "a2a.tasks.*.*.events",
			Durable: "relay-waits", Session: agreementSupervisor, WaitForStream: true,
		}, func(*Envelope) {})
		if err == nil {
			sub.Stop()
		}
		bound <- err
	}()

	// Three windows' worth of no stream.
	select {
	case err := <-bound:
		t.Fatalf("SubscribeDurable returned before the stream existed: %v", err)
	case <-time.After(3 * subscribeBindWindow):
	}
	createTasksStream(t, admin.js)
	select {
	case err := <-bound:
		if err != nil {
			t.Fatalf("SubscribeDurable after the stream appeared: %v", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("SubscribeDurable never bound the stream once it existed")
	}
	if !capture.contains("waiting for the stream to be created stream=TASKS durable=relay-waits") {
		t.Errorf("the wait was not logged:\n%s", strings.Join(capture.lines, "\n"))
	}
	if !capture.contains("durable bound after retry durable=relay-waits") {
		t.Errorf("the bind after the wait was not logged:\n%s", strings.Join(capture.lines, "\n"))
	}
	// The hour-long log interval held the wait to its first line.
	if n := capture.count("waiting for the stream to be created"); n != 1 {
		t.Errorf("the wait logged %d lines inside one log interval, want 1", n)
	}
}

// A stream that never comes keeps the bind waiting, warning on each interval,
// until the caller's context ends: loud, not silent, and not an exit.
func TestSubscribeDurable_WaitForStreamWarnsWhileTheStreamNeverComes(t *testing.T) {
	shortenStreamWait(t, 100*time.Millisecond, 50*time.Millisecond, 400*time.Millisecond)
	fixBindBackoff(20 * time.Millisecond)
	s := startServer(t)
	capture := &logCapture{}
	c, err := Connect(context.Background(), clientURL(s), WithName("relay"), WithLogger(slog.New(capture)))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 1500*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, err = c.SubscribeDurable(ctx, SubscribeConfig{
		Stream: TasksStream, Subject: "a2a.tasks.*.*.events",
		Durable: "relay-never", Session: agreementSupervisor, WaitForStream: true,
	}, func(*Envelope) {})
	elapsed := time.Since(start)
	if err == nil {
		t.Fatal("SubscribeDurable succeeded against a stream that does not exist")
	}
	// The last attempt's error, or the context's when the deadline lands
	// mid-request.
	if !errors.Is(err, jetstream.ErrStreamNotFound) && !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("the error = %v, want the missing stream's or the context's", err)
	}
	if elapsed < time.Second {
		t.Errorf("the bind gave up after %v, inside the caller's 1.5s; it should wait for the context", elapsed)
	}
	// One warning per interval waited: attempts every 20ms over ~1.5s at a
	// 400ms interval are three, a fourth only if the deadline falls late.
	n := capture.count("still waiting for the stream; nothing has created it stream=TASKS durable=relay-never")
	if limit := int(elapsed / streamWaitWarnEvery); n < 2 || n > limit {
		t.Errorf("%d still-waiting warnings over %v at a %v interval, want 2 to %d:\n%s",
			n, elapsed, streamWaitWarnEvery, limit, strings.Join(capture.lines, "\n"))
	}
	if capture.contains("durable bind failed; retrying") {
		t.Errorf("the missing stream was retried on the bounded path:\n%s", strings.Join(capture.lines, "\n"))
	}
}

// Only a missing stream waits. A stream that exists but refuses the consumer
// keeps the bounded window, WaitForStream or not, so a config the server
// will never accept still fails the process.
func TestSubscribeDurable_WaitForStreamKeepsTheWindowForOtherErrors(t *testing.T) {
	shortenStreamWait(t, 300*time.Millisecond, time.Hour, time.Hour)
	s := startServer(t)
	c, err := Connect(context.Background(), clientURL(s), WithName("relay"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	// The durable already exists with no acks, and a consumer's ack policy
	// cannot be updated, so the server refuses the relay's explicit-ack bind.
	createTasksStream(t, c.js)
	setup, cancelSetup := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancelSetup()
	if _, err := c.js.CreateConsumer(setup, TasksStream, jetstream.ConsumerConfig{
		Durable: "relay-refused", AckPolicy: jetstream.AckNonePolicy, FilterSubject: "a2a.tasks.*.*.events",
	}); err != nil {
		t.Fatalf("create the conflicting durable: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	start := time.Now()
	_, err = c.SubscribeDurable(ctx, SubscribeConfig{
		Stream: TasksStream, Subject: "a2a.tasks.*.*.events",
		Durable: "relay-refused", Session: agreementSupervisor, WaitForStream: true,
	}, func(*Envelope) {})
	if err == nil {
		t.Fatal("SubscribeDurable bound over a durable whose ack policy it cannot change")
	}
	if errors.Is(err, jetstream.ErrStreamNotFound) {
		t.Fatalf("the refusal read as a missing stream: %v", err)
	}
	if elapsed := time.Since(start); elapsed > 10*time.Second {
		t.Errorf("a refused consumer was retried for %v; it should fail once the 300ms window closes", elapsed)
	}
}

// Without WaitForStream nothing changes: a missing stream fails the first
// bind once the window closes, well before the caller's context would.
func TestSubscribeDurable_WithoutWaitForStreamAMissingStreamFailsAtTheWindow(t *testing.T) {
	shortenStreamWait(t, 300*time.Millisecond, time.Hour, time.Hour)
	s := startServer(t)
	c, err := Connect(context.Background(), clientURL(s), WithName("worker"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	start := time.Now()
	if _, err := c.SubscribeDurable(ctx, SubscribeConfig{
		Stream: TasksStream, Subject: "a2a.tasks.*.*.events",
		Durable: "worker-never", Session: agreementSupervisor,
	}, func(*Envelope) {}); err == nil {
		t.Fatal("SubscribeDurable succeeded against a stream that does not exist")
	}
	if ctx.Err() != nil {
		t.Fatal("the bind ran until the caller's context ended; without WaitForStream it must fail at the window")
	}
	if elapsed := time.Since(start); elapsed > 10*time.Second {
		t.Errorf("the bind took %v with a 300ms window", elapsed)
	}
}
