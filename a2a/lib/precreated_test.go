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

// The fixed TASKS durables the provision Job creates (#4173): a Precreated
// subscriber binds to one and never creates or updates it, waits for it as
// for the stream when it isn't there yet, and refuses one made differently.

const precreatedFilter = "a2a.tasks.*.*.events"

// operatorDurable creates the durable as the provision Job would, with a
// metadata mark a create-or-update from the subscriber would wipe.
func operatorDurable(t *testing.T, js jetstream.JetStream, name, filter string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := js.CreateConsumer(ctx, TasksStream, jetstream.ConsumerConfig{
		Durable:       name,
		FilterSubject: filter,
		AckPolicy:     jetstream.AckExplicitPolicy,
		Metadata:      map[string]string{"made-by": "provision"},
	}); err != nil {
		t.Fatalf("create %s: %v", name, err)
	}
}

func TestPrecreated_BindsTheOperatorsDurableWithoutChangingIt(t *testing.T) {
	s := startServer(t)
	admin, err := Connect(context.Background(), clientURL(s), WithName("provision"))
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	createTasksStreamIfAbsent(t, admin.js)
	operatorDurable(t, admin.js, "relay-pre", precreatedFilter)

	c, err := Connect(context.Background(), clientURL(s), WithName("relay"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	ctx := testCtx(t)
	got := &collector{}
	sub, err := c.SubscribeDurable(ctx, SubscribeConfig{
		Stream: TasksStream, Subject: precreatedFilter, Durable: "relay-pre",
		Session: agreementRequester, Precreated: true,
	}, got.handle)
	if err != nil {
		t.Fatalf("bind: %v", err)
	}
	defer sub.Stop()

	// It reads what lands on the durable.
	replayFixture(t, clientURL(s), "task-pre", []TaskState{StateSubmitted, StateWorking})
	waitFor(t, deliveryWait, "the two events", func() bool { return got.count() == 2 })

	// And it left the durable as the operator made it: a create-or-update
	// would have replaced the config, mark and all.
	cons, err := admin.js.Consumer(ctx, TasksStream, "relay-pre")
	if err != nil {
		t.Fatal(err)
	}
	if cons.CachedInfo().Config.Metadata["made-by"] != "provision" {
		t.Errorf("the bind rewrote the durable: metadata %v", cons.CachedInfo().Config.Metadata)
	}
}

func TestPrecreated_WaitsForTheOperatorToCreateIt(t *testing.T) {
	shortenStreamWait(t, 300*time.Millisecond, time.Hour, time.Hour)
	s := startServer(t)
	admin, err := Connect(context.Background(), clientURL(s), WithName("provision"))
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	createTasksStreamIfAbsent(t, admin.js)
	capture := &logCapture{}
	c, err := Connect(context.Background(), clientURL(s), WithName("relay"), WithLogger(slog.New(capture)))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	bound := make(chan error, 1)
	go func() {
		sub, err := c.SubscribeDurable(ctx, SubscribeConfig{
			Stream: TasksStream, Subject: precreatedFilter, Durable: "relay-later",
			Session: agreementRequester, Precreated: true,
		}, func(*Envelope) {})
		if err == nil {
			sub.Stop()
		}
		bound <- err
	}()
	select {
	case err := <-bound:
		t.Fatalf("the bind returned before the durable existed: %v", err)
	case <-time.After(3 * subscribeBindWindow):
	}
	operatorDurable(t, admin.js, "relay-later", precreatedFilter)
	select {
	case err := <-bound:
		if err != nil {
			t.Fatalf("bind after the durable appeared: %v", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("the bind never took the durable once it existed")
	}
	if !capture.contains("waiting for the operator to create the durable") {
		t.Errorf("the wait was not logged:\n%s", strings.Join(capture.lines, "\n"))
	}
}

func TestPrecreated_RefusesADurableMadeDifferently(t *testing.T) {
	s := startServer(t)
	admin, err := Connect(context.Background(), clientURL(s), WithName("provision"))
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	createTasksStreamIfAbsent(t, admin.js)
	operatorDurable(t, admin.js, "relay-wrong", "a2a.tasks.*.*.in")

	c, err := Connect(context.Background(), clientURL(s), WithName("relay"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	start := time.Now()
	_, err = c.SubscribeDurable(ctx, SubscribeConfig{
		Stream: TasksStream, Subject: precreatedFilter, Durable: "relay-wrong",
		Session: agreementRequester, Precreated: true,
	}, func(*Envelope) {})
	if !errors.Is(err, ErrPrecreatedBindRefused) {
		t.Fatalf("bind of a durable with another filter: %v, want ErrPrecreatedBindRefused", err)
	}
	// Refused at once: retrying can't change the durable.
	if waited := time.Since(start); waited > 3*time.Second {
		t.Errorf("the refusal took %s; it should not wait out the bind window", waited)
	}
}

// createTasksStreamIfAbsent is createTasksStream for a server whose start may
// already have made TASKS.
func createTasksStreamIfAbsent(t *testing.T, js jetstream.JetStream) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := js.Stream(ctx, TasksStream); err == nil {
		return
	}
	createTasksStream(t, js)
}

func TestPrecreated_RefusesADurableTheServerWouldReap(t *testing.T) {
	s := startServer(t)
	admin, err := Connect(context.Background(), clientURL(s), WithName("provision"))
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	createTasksStreamIfAbsent(t, admin.js)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := admin.js.CreateConsumer(ctx, TasksStream, jetstream.ConsumerConfig{
		Durable: "relay-reaped", FilterSubject: precreatedFilter,
		AckPolicy: jetstream.AckExplicitPolicy, InactiveThreshold: time.Hour,
	}); err != nil {
		t.Fatal(err)
	}
	c, err := Connect(context.Background(), clientURL(s), WithName("relay"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_, err = c.SubscribeDurable(ctx, SubscribeConfig{
		Stream: TasksStream, Subject: precreatedFilter, Durable: "relay-reaped",
		Session: agreementRequester, Precreated: true,
	}, func(*Envelope) {})
	if !errors.Is(err, ErrPrecreatedBindRefused) || !strings.Contains(err.Error(), "inactive") {
		t.Fatalf("bind of a durable with an inactivity threshold: %v, want a refusal naming it", err)
	}
}

func TestPrecreated_RefusesAHeadersOnlyDurable(t *testing.T) {
	s := startServer(t)
	admin, err := Connect(context.Background(), clientURL(s), WithName("provision"))
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	createTasksStreamIfAbsent(t, admin.js)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := admin.js.CreateConsumer(ctx, TasksStream, jetstream.ConsumerConfig{
		Durable: "relay-headers", FilterSubject: precreatedFilter,
		AckPolicy: jetstream.AckExplicitPolicy, HeadersOnly: true,
	}); err != nil {
		t.Fatal(err)
	}
	c, err := Connect(context.Background(), clientURL(s), WithName("relay"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_, err = c.SubscribeDurable(ctx, SubscribeConfig{
		Stream: TasksStream, Subject: precreatedFilter, Durable: "relay-headers",
		Session: agreementRequester, Precreated: true,
	}, func(*Envelope) {})
	if !errors.Is(err, ErrPrecreatedBindRefused) || !strings.Contains(err.Error(), "headers only") {
		t.Fatalf("bind of a headers-only durable: %v, want a headers-only refusal", err)
	}
}
