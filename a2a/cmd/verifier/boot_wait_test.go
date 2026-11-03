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
	"runtime"
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

// holdReservedPorts says whether reservePort keeps the port it picks bound
// for the whole test. The hold rests on Linux's reuse-address rule, under
// which two SO_REUSEADDR sockets share an exact address and port as long as
// neither is listening; BSD-derived kernels, macOS among them, let an exact
// duplicate in only when both sockets carry SO_REUSEPORT, which Go's listeners
// do not, so there the bus could never bind a held port and reservePort picks
// and releases instead, as it did before the hold. CI runs the module on
// Linux, which is where the sibling-package collision the hold removes occurs.
const holdReservedPorts = runtime.GOOS == "linux"

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
		failBusNotUp(t, srv, port)
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

// TestAReservedPortIsHeldAndStillRefusesConnections pins the three properties
// the other tests here rest on without saying so. The port is held, so a bind
// to it without the reuse-address option is refused, which a picked-and-
// released port would accept: this is the assertion that goes red if the hold
// is ever simplified away, and the flake it removes comes back silently. The
// verifier is pointed at the port while nothing answers, so a connection to it
// has to be refused, not accepted or left hanging. And the bus is started on
// the same port without the hold being released, so a listener has to be able
// to come up on it while it is held. A kernel that broke the last two would
// fail the tests above in ways that look like the verifier's fault; this one
// names the hold.
func TestAReservedPortIsHeldAndStillRefusesConnections(t *testing.T) {
	if !holdReservedPorts {
		t.Skipf("reservePort does not hold ports on %s", runtime.GOOS)
	}
	port := reservePort(t)
	addr := fmt.Sprintf("127.0.0.1:%d", port)

	fd, err := syscall.Socket(syscall.AF_INET, syscall.SOCK_STREAM, 0)
	if err != nil {
		t.Fatalf("socket: %v", err)
	}
	t.Cleanup(func() { _ = syscall.Close(fd) })
	err = syscall.Bind(fd, &syscall.SockaddrInet4{Addr: [4]byte{127, 0, 0, 1}, Port: port})
	if !errors.Is(err, syscall.EADDRINUSE) {
		t.Fatalf("bind %s without SO_REUSEADDR = %v, want EADDRINUSE: the port is not held, so "+
			"another test process can be handed it while the verifier is parked against it", addr, err)
	}

	conn, err := net.DialTimeout("tcp", addr, 2*time.Second)
	if err == nil {
		_ = conn.Close()
		t.Fatalf("a connection to the reserved port %s was accepted, want it refused: the hold is "+
			"listening, and the verifier would connect to it instead of waiting", addr)
	}
	if !errors.Is(err, syscall.ECONNREFUSED) {
		t.Fatalf("dial %s = %v, want a refused connection: the verifier would see a different "+
			"failure from the one these tests put it in", addr, err)
	}

	l, err := net.Listen("tcp", addr)
	if err != nil {
		t.Fatalf("listen on the reserved port %s = %v, want a listener: a bus started on a reserved "+
			"port could never bind it", addr, err)
	}
	_ = l.Close()
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

// reservePort picks a port for a bus that is started later in the test and,
// where holdReservedPorts allows, holds it until the test ends. The gap is the
// point: the verifier is pointed at the port while nothing is listening on it.
//
// The hold is a TCP socket bound to the port with the reuse-address option and
// never put into the listening state. Picking a port and closing the socket
// left the port unheld for the whole wait, and `go test ./...` runs every
// package's tests alongside this one: a sibling asking the kernel for any free
// port could be handed this one, and the bus then failed to start with its
// listen error swallowed. Linux's free-port search skips a port with any bound
// socket on it, so the sibling is never handed this one; a connection to it is
// refused at once, exactly as with nothing bound, so the verifier sees what
// the tests mean it to see; and a listener with the reuse-address option
// (every net.Listen, the bus included) still binds and listens on it while the
// hold is in place, so nothing has to be released before startBus.
// TestAReservedPortIsHeldAndStillRefusesConnections pins all three.
func reservePort(t *testing.T) int {
	t.Helper()
	if !holdReservedPorts {
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
	fd, err := syscall.Socket(syscall.AF_INET, syscall.SOCK_STREAM, 0)
	if err != nil {
		t.Fatalf("reserve a port: socket: %v", err)
	}
	t.Cleanup(func() { _ = syscall.Close(fd) })
	syscall.CloseOnExec(fd)
	if err := syscall.SetsockoptInt(fd, syscall.SOL_SOCKET, syscall.SO_REUSEADDR, 1); err != nil {
		t.Fatalf("reserve a port: set SO_REUSEADDR: %v", err)
	}
	if err := syscall.Bind(fd, &syscall.SockaddrInet4{Addr: [4]byte{127, 0, 0, 1}, Port: 0}); err != nil {
		t.Fatalf("reserve a port: bind: %v", err)
	}
	sa, err := syscall.Getsockname(fd)
	if err != nil {
		t.Fatalf("reserve a port: getsockname: %v", err)
	}
	return sa.(*syscall.SockaddrInet4).Port
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
		failBusNotUp(t, srv, port)
	}
	t.Cleanup(srv.Shutdown)
	return srv
}

// failBusNotUp ends the test when a bus did not report ready, and says why.
// The server runs unlogged, so the error its listener returned is never
// printed and a bare timeout is all that reached the log when another test
// process held the port. Shutting the server down first releases whatever it
// did bind, so a plain listen on the port then either names what else holds
// it or shows that the port was not the problem. The test's own hold is one
// candidate: a kernel that refused a listener beside it would fail here too,
// and TestAReservedPortIsHeldAndStillRefusesConnections says whether it does.
func failBusNotUp(t *testing.T, srv *natsserver.Server, port int) {
	t.Helper()
	srv.Shutdown()
	addr := fmt.Sprintf("127.0.0.1:%d", port)
	l, err := net.Listen("tcp", addr)
	if err != nil {
		t.Fatalf("nats server did not come up on %s, and a plain listen on it fails too: %v; "+
			"something else holds the port, or this kernel does not let a listener in beside the "+
			"test's own hold on it (held: %t)", addr, err, holdReservedPorts)
	}
	_ = l.Close()
	t.Fatalf("nats server did not come up on %s, and the port is free now that the server is shut "+
		"down: the failure was not the bind, or whatever took the port has released it since", addr)
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
