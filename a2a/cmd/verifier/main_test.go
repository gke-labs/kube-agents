package main

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A bus connection that ends for good has to end the process with it.
//
// MaxReconnects(-1) reads like "this can never happen", and that is the trap:
// nats.go aborts its own reconnect loop when the same server answers the same
// authorization error twice running (processAuthError, v1.53.1). The verifier
// authenticates with a projected token, so a revoked identity or a callout
// that has lost the verifier's user lands exactly there.
//
// What made it silent is the probe split. /readyz is honest — it reports
// nc.IsConnected() — but /healthz is unconditional, deliberately, so that a
// transient disconnect does not kill a verifier that is about to reconnect.
// The liveness probe is on /healthz. So without the handler this test pins,
// the pod goes NotReady and stays there: nothing restarts it, and while it is
// gone every executor in the install refuses every task.
func TestAClosedBusConnectionEndsTheProcess(t *testing.T) {
	opts := &natsserver.Options{Host: "127.0.0.1", Port: -1, NoLog: true, NoSigs: true}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("nats server: %v", err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats server did not come up")
	}
	t.Cleanup(srv.Shutdown)

	// The deployment's own auth path: a projected token file, not the static
	// password. The test server requires no auth, so the token's content is
	// irrelevant -- what matters is that connect takes the branch the pod
	// takes, since that is the branch processAuthError aborts on.
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("not-checked-by-an-open-server"), 0o600); err != nil {
		t.Fatalf("write the token file: %v", err)
	}
	t.Setenv(lib.EnvBusTokenFile, tokenFile)

	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	closed := make(chan struct{})
	nc, err := connect(context.Background(), log, srv.ClientURL(), func() { close(closed) })
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(nc.Close)

	// Close stands in for the abort: both end the connection for good, and
	// ClosedHandler is the only callback either one reaches. Driving a real
	// double auth error would need a server that rejects the token twice,
	// which tests nats.go rather than this wiring.
	nc.Close()

	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("the connection closed for good and onClosed never fired, so run() would " +
			"sit on <-ctx.Done() forever: NotReady, liveness still green, never restarted")
	}
}
