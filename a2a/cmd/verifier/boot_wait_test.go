package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The verifier has to survive starting before the things it depends on.
//
// Both tests here are the same shape and the same window, with the two causes
// separated: a bus that is not answering yet, and a bus that is answering with
// no bucket in it. Both are ordinary on a fresh `mode: next` install --
// reconcileA2A applies this Deployment ahead of the provision Job that creates
// the bucket -- and both used to be spelled as `return 1`.
//
// What made that expensive is what happens after the dependency arrives. The
// kubelet's restart backoff climbs to five minutes and does not know the bus
// came back, so a thirty-second NATS rollout bought minutes of a verifier that
// could have been serving sitting in CrashLoopBackOff; with both replicas in
// the window, every executor in the install refuses every task for the whole
// of it. connect already refuses to trade a wait for a restart -- that is what
// RetryOnFailedConnect and MaxReconnects(-1) are -- and before this the bind
// three lines later gave the trade straight back, because nats.Connect returns
// a reconnecting connection with no error and the bind then times out against
// it on jetstream's own 5s default.
//
// Each test asserts the three things that together are the fix: run() does not
// exit while it waits, it binds and serves once the dependency arrives, and a
// SIGTERM during the wait is still a clean stop rather than a failure.

// TestTheVerifierWaitsForABusThatIsNotThereYet starts the process with nothing
// listening at NATS_URL at all. Before the fix this returned 1 after a single
// jetstream API timeout, roughly five seconds in.
func TestTheVerifierWaitsForABusThatIsNotThereYet(t *testing.T) {
	port := reservePort(t)
	url := fmt.Sprintf("nats://127.0.0.1:%d", port)
	verifierEnv(t, url)

	exit := make(chan int, 1)
	go func() { exit <- run() }()

	// Comfortably past the one API timeout the old bind took to give up.
	mustNotExit(t, exit, 12*time.Second, "the bus was not up yet")

	srv := startBus(t, port)
	createBucket(t, srv)

	assertAnswersVerify(t, srv, 30*time.Second)
	assertCleanStopOnSIGTERM(t, exit)
}

// TestTheVerifierWaitsForTheBucketToBeCreated is the fresh-install ordering:
// the bus answers, but the provision Job has not run yet, so the bind is
// refused with ErrBucketNotFound -- immediately, with no timeout to soften it.
func TestTheVerifierWaitsForTheBucketToBeCreated(t *testing.T) {
	port := reservePort(t)
	srv := startBus(t, port)
	verifierEnv(t, srv.ClientURL())

	exit := make(chan int, 1)
	go func() { exit <- run() }()

	// ErrBucketNotFound comes back at once, so several retries have run by
	// here; the old code was already gone on the first.
	mustNotExit(t, exit, 8*time.Second, "the capability bucket did not exist yet")

	// The status server has to be listening DURING the wait, not after it.
	// Waiting instead of exiting only helps if the kubelet lets the pod wait,
	// and liveness is on /healthz at 10s x 6 from container start: bind the
	// bucket first and a long enough wait is killed by the probe rather than
	// by the exit code. This is the only assertion that pins the ordering, so
	// it goes over the real port the Deployment probes.
	if got := probeListener(t, "/healthz"); got != http.StatusOK {
		t.Errorf("GET %s/healthz = %d during the bucket wait, want %d: the status server is not "+
			"up before the bind, so the liveness probe kills a pod that is waiting correctly "+
			"(or something else on this host holds %s)", readyPort, got, http.StatusOK, readyPort)
	}
	if got := probeListener(t, "/readyz"); got != http.StatusServiceUnavailable {
		t.Errorf("GET %s/readyz = %d during the bucket wait, want %d", readyPort, got,
			http.StatusServiceUnavailable)
	}

	createBucket(t, srv)

	assertAnswersVerify(t, srv, 30*time.Second)
	assertCleanStopOnSIGTERM(t, exit)
}

// TestASignalDuringTheBindWaitIsACleanStop covers the other way out of the
// wait, and it is the one that is easy to get wrong in the direction that
// costs something.
//
// A verifier parked in bindStore and then drained off the node -- an upgrade,
// a scale-down, a node drain -- never subscribed to anything, so there is
// nothing in flight and nothing to report. Exiting non-zero there would write
// a container termination with a failure code into the install's events on
// every ordinary rollout that happens to catch a pod mid-wait, which is noise
// that looks exactly like the fault this whole fix is about.
func TestASignalDuringTheBindWaitIsACleanStop(t *testing.T) {
	port := reservePort(t)
	srv := startBus(t, port)
	verifierEnv(t, srv.ClientURL())

	// No bucket for the whole test: run() stays in the bind wait.
	exit := make(chan int, 1)
	go func() { exit <- run() }()
	waitFor(t, 15*time.Second, func() bool { return srv.NumClients() > 0 })
	mustNotExit(t, exit, 4*time.Second, "the capability bucket did not exist yet")

	assertCleanStopOnSIGTERM(t, exit)
}

// TestTheBindWaitEndsWhenTheBusEndsForGood is the limit on waiting, and it is
// the same hazard TestRunExitsNonZeroWhenTheBusConnectionEndsForGood pins one
// stage later.
//
// Retrying forever is right while the dependency can still arrive. It is the
// wrong answer when the connection is gone for good -- a revoked identity, a
// callout that has lost the verifier's user -- because then nothing will ever
// bind, /healthz is green by design, and the pod sits there for good with
// every executor in the install refusing every task. Making the bind wait
// patient reopened that door in a window the earlier test cannot reach: it
// waits for the verifier to be serving before it breaks anything, and here the
// verifier never gets that far.
func TestTheBindWaitEndsWhenTheBusEndsForGood(t *testing.T) {
	const goodToken = "the-token-the-verifier-holds"
	port := reservePort(t)
	opts := &natsserver.Options{
		Host: "127.0.0.1", Port: port, NoLog: true, NoSigs: true,
		JetStream: true, StoreDir: t.TempDir(), Authorization: goodToken,
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

	// No bucket, deliberately: run() is parked in bindStore for the whole
	// test, which is the window being covered.
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte(goodToken), 0o600); err != nil {
		t.Fatalf("write the token file: %v", err)
	}
	t.Setenv(lib.EnvBusTokenFile, tokenFile)
	t.Setenv("NATS_URL", srv.ClientURL())

	exit := make(chan int, 1)
	go func() { exit <- run() }()
	waitFor(t, 15*time.Second, func() bool { return srv.NumClients() > 0 })

	// Rotate the token out from under the live connection: the reconnect
	// presents the old one and is refused twice, which is what aborts
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
	case <-time.After(45 * time.Second):
		t.Fatal("the bus connection ended for good while the verifier was waiting to bind the " +
			"bucket, and run() never returned: it would retry a bind that can never succeed on a " +
			"connection that is gone, with /healthz green so nothing restarts it")
	}
}

// TestReadinessIsFalseUntilTheBucketIsBound pins the other half of starting
// the status server early. The listener now comes up before the bind, so
// readiness cannot be nc.IsConnected() alone: during the bucket wait the
// connection is live and nothing is subscribed, and a verifier that reported
// Ready there would be handed requests it cannot answer.
func TestReadinessIsFalseUntilTheBucketIsBound(t *testing.T) {
	port := reservePort(t)
	srv := startBus(t, port)
	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(nc.Close)

	var serving atomic.Bool
	mux := readyMux(nc, &serving)

	// Connected but still binding: the fresh-install window.
	if got := probe(mux, "/readyz"); got != http.StatusServiceUnavailable {
		t.Errorf("/readyz = %d while the bucket was unbound, want %d: a verifier that has not "+
			"subscribed would join the Service and be handed requests it cannot answer",
			got, http.StatusServiceUnavailable)
	}
	// Liveness must not notice the wait at all, or the kubelet kills the pod
	// at 10s x 6 for doing exactly what it is supposed to do.
	if got := probe(mux, "/healthz"); got != http.StatusOK {
		t.Errorf("/healthz = %d during the bucket wait, want %d", got, http.StatusOK)
	}

	serving.Store(true)
	if got := probe(mux, "/readyz"); got != http.StatusOK {
		t.Errorf("/readyz = %d once bound and connected, want %d", got, http.StatusOK)
	}

	// And the original rule still holds on top of the new one: bound is not
	// enough if the connection is gone.
	srv.Shutdown()
	waitFor(t, 10*time.Second, func() bool { return !nc.IsConnected() })
	if got := probe(mux, "/readyz"); got != http.StatusServiceUnavailable {
		t.Errorf("/readyz = %d while disconnected, want %d", got, http.StatusServiceUnavailable)
	}
}

// probeListener asks the real listener on readyPort, the way the kubelet
// does. The retry is for the listener's own goroutine getting scheduled, not
// for the bind it is being asked about.
func probeListener(t *testing.T, path string) int {
	t.Helper()
	client := &http.Client{Timeout: 2 * time.Second}
	var last error
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := client.Get("http://127.0.0.1" + readyPort + path)
		if err != nil {
			last = err
			time.Sleep(100 * time.Millisecond)
			continue
		}
		_ = resp.Body.Close()
		return resp.StatusCode
	}
	t.Fatalf("nothing answered %s%s: %v", readyPort, path, last)
	return 0
}

func probe(mux *http.ServeMux, path string) int {
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	return rec.Code
}

// reservePort picks a port and gives it straight back, so a bus can be started
// on it later in the test. The gap is the point: the verifier is pointed at it
// while nothing is listening.
func reservePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("reserve a port: %v", err)
	}
	port := l.Addr().(*net.TCPAddr).Port
	if err := l.Close(); err != nil {
		t.Fatalf("release the reserved port: %v", err)
	}
	return port
}

func startBus(t *testing.T, port int) *natsserver.Server {
	t.Helper()
	srv, err := natsserver.NewServer(&natsserver.Options{
		Host: "127.0.0.1", Port: port, NoLog: true, NoSigs: true,
		JetStream: true, StoreDir: t.TempDir(),
	})
	if err != nil {
		t.Fatalf("nats server: %v", err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats server did not come up")
	}
	t.Cleanup(srv.Shutdown)
	return srv
}

func createBucket(t *testing.T, srv *natsserver.Server) {
	t.Helper()
	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatalf("setup connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	if _, err := js.CreateKeyValue(context.Background(),
		jetstream.KeyValueConfig{Bucket: capability.Bucket}); err != nil {
		t.Fatalf("create the capability bucket: %v", err)
	}
}

// verifierEnv puts run() on the deployment's own auth path: a projected token
// file rather than the static password. The test servers require no auth, so
// the token's content is irrelevant; the branch is what matters.
func verifierEnv(t *testing.T, url string) {
	t.Helper()
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("not-checked-by-this-server"), 0o600); err != nil {
		t.Fatalf("write the token file: %v", err)
	}
	t.Setenv(lib.EnvBusTokenFile, tokenFile)
	t.Setenv("NATS_URL", url)
}

func mustNotExit(t *testing.T, exit <-chan int, within time.Duration, why string) {
	t.Helper()
	select {
	case code := <-exit:
		t.Fatalf("run() returned %d within %s because %s; the pod would enter CrashLoopBackOff and "+
			"sit out a kubelet backoff that reaches five minutes after the dependency arrived",
			code, within, why)
	case <-time.After(within):
	}
}

// assertAnswersVerify proves the verifier got past the bind and subscribed, by
// the only means a broker has: asking it something. A key that was never
// written walks to a refusal, which is a real reply off a real bound bucket --
// run() subscribes only after bindStore returns.
func assertAnswersVerify(t *testing.T, srv *natsserver.Server, limit time.Duration) {
	t.Helper()
	// Ask through capability.Client rather than nats.Request. Answers ride
	// a2a.cap.reply.<caller>.>, not _INBOX, and the service drops a request
	// whose reply subject is outside the caller's own namespace -- so a
	// hand-rolled nats.Request is answered with silence, and the test would
	// read the anti-impersonation rule as a failure to bind.
	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatalf("asker connect: %v", err)
	}
	defer nc.Close()
	client, err := capability.NewClient(nc, "some-broker")
	if err != nil {
		t.Fatalf("verifier client: %v", err)
	}
	client.Timeout = 2 * time.Second

	deadline := time.Now().Add(limit)
	for time.Now().Before(deadline) {
		err := client.Check(context.Background(),
			capability.Ref{Key: "a-key-that-was-never-written", Revision: 1},
			capability.Verb("get"), capability.Scope(""))
		// Both outcomes are denials -- the client fails closed either way --
		// so the test has to read WHICH denial. WalkRefused is a verdict the
		// verifier reached over a bound bucket; the unreachable refusal is
		// the client's own timeout with nobody listening.
		var refusal *capability.Refusal
		if errors.As(err, &refusal) && refusal.Rule == capability.WalkRefused {
			return
		}
		if err == nil {
			t.Fatal("a key that was never written was allowed")
		}
		time.Sleep(200 * time.Millisecond)
	}
	t.Fatal("the dependency arrived and the verifier never started answering: it waited instead of " +
		"exiting, which is right, but it has to pick the work up when the wait ends")
}

// assertCleanStopOnSIGTERM closes the loop on the exit code. A wait that ends
// in a signal is an ordinary stop: nothing was ever subscribed on the paths
// above it, so there is nothing to drain and nothing to report as a failure.
func assertCleanStopOnSIGTERM(t *testing.T, exit <-chan int) {
	t.Helper()
	if err := syscall.Kill(syscall.Getpid(), syscall.SIGTERM); err != nil {
		t.Fatalf("signal the process: %v", err)
	}
	select {
	case code := <-exit:
		if code != 0 {
			t.Errorf("run() returned %d on SIGTERM, want 0", code)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("run() did not return on SIGTERM")
	}
}
