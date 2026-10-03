package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The sibling of TestAClosedBusConnectionEndsTheProcess, and the half it could
// not cover.
//
// That test hands connect() a callback of its own and asserts the callback
// fires. It therefore pins ClosedHandler, which is necessary but is not the
// property anyone cares about: run() has to WIRE that callback to something
// that ends the process. Deleting run()'s `closeOnce.Do(close(busClosed))` and
// its `case <-busClosed: return 1` leaves that test green — measured, not
// assumed — and leaves the pod in the exact state the comment describes:
// NotReady for good, /healthz still green so the kubelet never restarts it,
// and every executor in the install refusing every task.
//
// So this one drives run() itself and asserts the exit code, through a real
// permanent close rather than a stand-in for one: the server's token is
// changed underneath a live connection, so the client is disconnected and its
// reconnect answers the old token, which is the double authorization error
// nats.go's processAuthError aborts the reconnect loop on.
func TestRunExitsNonZeroWhenTheBusConnectionEndsForGood(t *testing.T) {
	const goodToken = "the-token-the-verifier-holds"

	opts := &natsserver.Options{
		Host: "127.0.0.1", Port: -1, NoLog: true, NoSigs: true,
		JetStream: true, StoreDir: t.TempDir(),
		Authorization: goodToken,
	}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("nats server: %v", err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats server did not come up")
	}
	t.Cleanup(srv.Shutdown)

	// run() binds the capability bucket at boot and exits 1 if it cannot, so
	// without this the test would pass for the wrong reason.
	ctx := context.Background()
	setup, err := nats.Connect(srv.ClientURL(), nats.Token(goodToken))
	if err != nil {
		t.Fatalf("setup connect: %v", err)
	}
	js, err := jetstream.New(setup)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	if _, err := js.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: capability.Bucket}); err != nil {
		t.Fatalf("create the capability bucket: %v", err)
	}
	setup.Close()

	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte(goodToken), 0o600); err != nil {
		t.Fatalf("write the token file: %v", err)
	}
	t.Setenv(lib.EnvBusTokenFile, tokenFile)
	t.Setenv("NATS_URL", srv.ClientURL())

	exit := make(chan int, 1)
	go func() { exit <- run() }()

	// Wait until run() is actually serving before breaking anything, or the
	// race is with its boot rather than with its exit path.
	waitFor(t, 15*time.Second, func() bool { return srv.NumClients() > 0 })

	// Rotate the token out from under the live connection. The server
	// re-authenticates on reload and disconnects the client; its reconnect
	// presents the old token and is refused, twice, which is what aborts
	// nats.go's reconnect loop and fires ClosedHandler for good.
	reloaded := *opts
	reloaded.Authorization = "a-token-the-verifier-does-not-have"
	if err := srv.ReloadOptions(&reloaded); err != nil {
		t.Fatalf("reload the server with a new token: %v", err)
	}

	select {
	case code := <-exit:
		if code != 1 {
			t.Errorf("run() returned %d, want 1: the pod would not restart", code)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("the bus connection ended for good and run() never returned: the pod would stay " +
			"NotReady with /healthz green, so nothing would restart it and every task in the install would be refused")
	}
}

func waitFor(t *testing.T, limit time.Duration, done func() bool) {
	t.Helper()
	deadline := time.Now().Add(limit)
	for time.Now().Before(deadline) {
		if done() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("timed out waiting for the verifier to come up")
}
