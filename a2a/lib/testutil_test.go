package lib

import (
	"context"
	"fmt"
	"net"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// runJetStreamServer starts a real nats-server with JetStream on a random port.
// storeDir persists across restarts of the same test so the restart assertion
// can bring the stream back.
func runJetStreamServer(t *testing.T, port int, storeDir string, mutate func(*natsserver.Options)) *natsserver.Server {
	t.Helper()
	opts := &natsserver.Options{
		Host:      "127.0.0.1",
		Port:      port,
		JetStream: true,
		StoreDir:  storeDir,
		NoLog:     true,
		NoSigs:    true,
	}
	if mutate != nil {
		mutate(opts)
	}
	s, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	return s
}

// startServer is the common case: random port, per-test store dir, shutdown on
// cleanup.
func startServer(t *testing.T) *natsserver.Server {
	t.Helper()
	s := runJetStreamServer(t, -1, t.TempDir(), nil)
	t.Cleanup(s.Shutdown)
	return s
}

// provisionTasksStream creates the TASKS stream the way W2's deployment will:
// one limits-retention stream over a2a.tasks.>.
func provisionTasksStream(t *testing.T, url string) {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err = js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:      "TASKS",
		Subjects:  []string{"a2a.tasks.>"},
		Retention: jetstream.LimitsPolicy,
		MaxAge:    72 * time.Hour,
	})
	if err != nil {
		t.Fatalf("create TASKS stream: %v", err)
	}
}

// publishRaw injects arbitrary bytes onto a subject, bypassing the library's
// emit validation — how a foreign or hostile publisher looks to a consumer.
func publishRaw(t *testing.T, url, subject string, data []byte) {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := js.Publish(ctx, subject, data); err != nil {
		t.Fatalf("publish raw: %v", err)
	}
}

// waitFor polls until cond is true or the deadline passes.
func waitFor(t *testing.T, d time.Duration, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func testCtx(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	t.Cleanup(cancel)
	return ctx
}

func serverPort(s *natsserver.Server) int {
	return s.Addr().(*net.TCPAddr).Port
}

func clientURL(s *natsserver.Server) string {
	return fmt.Sprintf("nats://%s", s.Addr().String())
}

// streamMsgCount returns how many messages a stream holds.
func streamMsgCount(t *testing.T, url, stream string) uint64 {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	st, err := js.Stream(ctx, stream)
	if err != nil {
		t.Fatalf("stream %s: %v", stream, err)
	}
	info, err := st.Info(ctx)
	if err != nil {
		t.Fatalf("stream info: %v", err)
	}
	return info.State.Msgs
}
