package hermesbridge

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// The bridge waits for the bus instead of exiting (#5962). The operator now
// renders it from its first pass, before the provisioning Job has created the
// runtime-state bucket and possibly before the bus serves; a bridge that
// exited then would crash-loop the agent pod.

// bareServer is a JetStream server with nothing provisioned.
func bareServer(t *testing.T, port int) *natsserver.Server {
	t.Helper()
	s, err := natsserver.NewServer(&natsserver.Options{
		Host: "127.0.0.1", Port: port, JetStream: true, StoreDir: t.TempDir(), NoLog: true, NoSigs: true,
	})
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("server not ready")
	}
	t.Cleanup(s.Shutdown)
	return s
}

func busWaitConfig(url string) Config {
	return Config{NATSURL: url, KVBucket: "runtime-state", BusWaitMax: 100 * time.Millisecond}
}

// newAsync runs New in the background and hands back its result.
func newAsync(ctx context.Context, cfg Config) <-chan error {
	done := make(chan error, 1)
	go func() {
		b, err := New(ctx, cfg)
		if err == nil {
			b.close()
		}
		done <- err
	}()
	return done
}

func TestNewWaitsForTheRuntimeStateBucket(t *testing.T) {
	port := int(testPort.Add(1))
	bareServer(t, port)
	url := fmt.Sprintf("nats://127.0.0.1:%d", port)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	done := newAsync(ctx, busWaitConfig(url))
	select {
	case err := <-done:
		t.Fatalf("New returned before the bucket existed: %v", err)
	case <-time.After(500 * time.Millisecond):
	}

	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	if _, err := js.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: "runtime-state"}); err != nil {
		t.Fatalf("create runtime-state: %v", err)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("New after the bucket appeared: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("New did not return once the bucket existed")
	}
}

func TestNewWaitsForTheBusToServe(t *testing.T) {
	port := int(testPort.Add(1))
	url := fmt.Sprintf("nats://127.0.0.1:%d", port)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	done := newAsync(ctx, busWaitConfig(url))
	select {
	case err := <-done:
		t.Fatalf("New returned with no bus: %v", err)
	case <-time.After(500 * time.Millisecond):
	}

	bareServer(t, port)
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	if _, err := js.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: "runtime-state"}); err != nil {
		t.Fatalf("create runtime-state: %v", err)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("New after the bus came up: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("New did not return once the bus served")
	}
}

func TestNewStopsWaitingWhenItsContextEnds(t *testing.T) {
	url := fmt.Sprintf("nats://127.0.0.1:%d", int(testPort.Add(1)))
	ctx, cancel := context.WithCancel(context.Background())
	done := newAsync(ctx, busWaitConfig(url))
	time.Sleep(300 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("New succeeded with no bus")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("New kept waiting after its context ended")
	}
}

func TestAConfigErrorIsNotWaitedOn(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cfg := busWaitConfig("nats://127.0.0.1:1")
	cfg.Executor = "neither"
	start := time.Now()
	if _, err := New(ctx, cfg); err == nil {
		t.Fatal("New accepted an unknown executor")
	}
	if time.Since(start) > time.Second {
		t.Fatalf("a config error took %s; it must fail before any wait", time.Since(start))
	}
}

// A bus that is up but refuses the bridge's credentials is said at Error, in
// the words the troubleshooting guide keys on, and still waited on: the
// password or grant may be fixed without restarting the pod.
func TestARefusedBridgeIsSaidAtErrorAndWaited(t *testing.T) {
	port := int(testPort.Add(1))
	s, err := natsserver.NewServer(&natsserver.Options{
		Host: "127.0.0.1", Port: port, JetStream: true, StoreDir: t.TempDir(), NoLog: true, NoSigs: true,
		Username: "bridge", Password: "right",
	})
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("server not ready")
	}
	t.Cleanup(s.Shutdown)

	var logs lockedBuffer
	cfg := busWaitConfig(fmt.Sprintf("nats://127.0.0.1:%d", port))
	cfg.NATSOptions = []nats.Option{nats.UserInfo("bridge", "wrong")}
	cfg.Logger = slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelInfo}))
	ctx, cancel := context.WithTimeout(context.Background(), 1500*time.Millisecond)
	defer cancel()
	if _, err := New(ctx, cfg); err == nil {
		t.Fatal("New succeeded with the wrong password")
	}
	out := logs.String()
	if !strings.Contains(out, "level=ERROR") || !strings.Contains(out, "the bus refused this bridge") {
		t.Fatalf("a refused bridge was not said at Error with the troubleshooting words:\n%s", out)
	}
	if strings.Count(out, "the bus refused this bridge") < 2 {
		t.Fatalf("a refused bridge stopped waiting after one attempt:\n%s", out)
	}
}

func TestBusRefusedTellsARefusalFromAnAbsence(t *testing.T) {
	for err, want := range map[error]bool{
		nats.ErrAuthorization:                       true,
		fmt.Errorf("kv: %w", nats.ErrAuthorization): true,
		nats.ErrPermissionViolation:                 true,
		nats.ErrNoServers:                           false,
		jetstream.ErrBucketNotFound:                 false,
		context.DeadlineExceeded:                    false,
	} {
		if got := busRefused(err); got != want {
			t.Errorf("busRefused(%v) = %v, want %v", err, got, want)
		}
	}
}
