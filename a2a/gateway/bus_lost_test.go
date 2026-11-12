package gateway

import (
	"context"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"k8s.io/client-go/kubernetes/fake"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// busLostRig is a gateway on an embedded server that a test can stop and
// start again on the same port and store, so the gateway's client loses the
// bus and gets it back.
type busLostRig struct {
	g      *Gateway
	client *lib.Client
	url    string
	opts   *natsserver.Options
	srv    *natsserver.Server
}

func newBusLostRig(t *testing.T) *busLostRig {
	t.Helper()
	r := &busLostRig{opts: &natsserver.Options{Host: "127.0.0.1", Port: -1, JetStream: true, StoreDir: t.TempDir(), NoLog: true, NoSigs: true}}
	r.start(t)
	// Every restart takes the port the first start picked.
	r.opts.Port = r.srv.Addr().(*net.TCPAddr).Port
	r.url = r.srv.ClientURL()
	provision(t, r.url)
	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("1001 test:bnaylor\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	client, err := lib.Connect(ctx, r.url, lib.WithName("gateway-test"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)
	g, err := New(Options{Client: client, Adapter: newFakeAdapter(), Backend: "discord", Config: &Config{
		NATSURL: r.url, PrincipalMapPath: mapFile, DefaultAddressee: "platform",
		IdleTTL: 30 * time.Minute, AttributionSalt: []byte("test-salt"),
	}})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	r.g, r.client = g, client
	return r
}

func (r *busLostRig) start(t *testing.T) {
	t.Helper()
	s, err := natsserver.NewServer(r.opts)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(s.Shutdown)
	r.srv = s
}

// stop takes the bus away and waits until the gateway's client has noticed.
func (r *busLostRig) stop(t *testing.T) {
	t.Helper()
	r.srv.Shutdown()
	r.srv.WaitForShutdown()
	waitFor(t, "the gateway's client to lose the bus", func() bool { return !r.client.Connected() })
}

// restart brings the bus back and waits until the client has reconnected.
func (r *busLostRig) restart(t *testing.T) {
	t.Helper()
	r.start(t)
	waitFor(t, "the gateway's client to reconnect", r.client.Connected)
}

// term reads the registry's term marker straight from the bus.
func (r *busLostRig) term(t *testing.T) uint64 {
	t.Helper()
	nc, err := nats.Connect(r.url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	kv, err := js.KeyValue(ctx, lib.SessionStateBucket)
	if err != nil {
		t.Fatalf("session-state bucket: %v", err)
	}
	e, err := kv.Get(ctx, termKey)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return 0
	}
	if err != nil {
		t.Fatalf("term marker: %v", err)
	}
	n, err := strconv.ParseUint(string(e.Value()), 10, 64)
	if err != nil {
		t.Fatalf("term marker %q: %v", e.Value(), err)
	}
	return n
}

// shortBusWatch makes the watchdog look often and give up soon.
func shortBusWatch(t *testing.T, limit time.Duration) {
	t.Helper()
	oldEvery, oldMax := busWatchEvery, termWaitBusMax
	busWatchEvery, termWaitBusMax = 50*time.Millisecond, limit
	t.Cleanup(func() { busWatchEvery, termWaitBusMax = oldEvery, oldMax })
}

// A bound relay whose connection is down delivers nothing, so readiness
// follows the connection after the bind as well as before it, and comes back
// with it.
func TestABoundGatewayOffTheBusIsNotReady(t *testing.T) {
	r := newBusLostRig(t)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { _ = r.g.Run(ctx) }()
	waitFor(t, "the relay to bind", func() bool { ready, _ := r.g.Readiness(); return ready })

	r.stop(t)
	ready, why := r.g.Readiness()
	if ready || !strings.Contains(why, "waiting for the bus") || !strings.Contains(why, "bound event relay") {
		t.Fatalf("Readiness bound and off the bus = %v %q, want not ready naming the bus", ready, why)
	}
	// The elected path hands the holder the same answer.
	view := &LeaseView{}
	view.set("pod-a")
	if ready, _ := ElectedReadiness(r.g, view, "pod-a")(); ready {
		t.Fatal("the lease holder read ready off the bus")
	}

	r.restart(t)
	waitFor(t, "readiness to return with the bus", func() bool { ready, _ := r.g.Readiness(); return ready })
}

// An elected run off the bus for the limit ends with errTermBusGaveUp, the
// error Election.Run releases the lease and stands back on.
func TestARunOffTheBusForTheLimitGivesUp(t *testing.T) {
	shortBusWatch(t, time.Second)
	r := newBusLostRig(t)
	done := make(chan error, 1)
	go func() { done <- r.g.RunWith(context.Background(), RunOptions{BusDownLimit: termWaitBusMax}) }()
	waitFor(t, "the relay to bind", func() bool { ready, _ := r.g.Readiness(); return ready })
	r.stop(t)
	err := receive(t, done, 10*time.Second, "the run to give up on the bus")
	if !errors.Is(err, errTermBusGaveUp) {
		t.Fatalf("run off the bus = %v, want errTermBusGaveUp", err)
	}
}

// A blip shorter than the limit moves nothing: the count starts again on a
// reconnect, so two blips that add up past it don't either.
func TestABusBlipDoesNotEndTheRun(t *testing.T) {
	shortBusWatch(t, 3*time.Second)
	r := newBusLostRig(t)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- r.g.RunWith(ctx, RunOptions{BusDownLimit: termWaitBusMax}) }()
	waitFor(t, "the relay to bind", func() bool { ready, _ := r.g.Readiness(); return ready })
	var down time.Duration
	// Blips of about a second each, until together they pass twice the
	// limit: the reset on each reconnect is the only thing holding the run.
	for i := 0; i < 12 && down < 2*termWaitBusMax; i++ {
		start := time.Now()
		r.stop(t)
		time.Sleep(500 * time.Millisecond)
		r.restart(t)
		down += time.Since(start)
	}
	if down < 2*termWaitBusMax {
		t.Fatalf("the blips added up to %s, under twice the %s limit", down, termWaitBusMax)
	}
	select {
	case err := <-done:
		t.Fatalf("the run ended after blips shorter than the limit: %v", err)
	case <-time.After(1500 * time.Millisecond):
	}
	cancel()
	if err := receive(t, done, 10*time.Second, "the run to stop"); err != nil {
		t.Fatalf("run after cancel = %v, want nil", err)
	}
}

// A run without a limit, the unelected gateway's, never gives up on the bus.
func TestARunWithoutALimitWaitsOutTheBus(t *testing.T) {
	shortBusWatch(t, 500*time.Millisecond)
	r := newBusLostRig(t)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- r.g.Run(ctx) }()
	waitFor(t, "the relay to bind", func() bool { ready, _ := r.g.Readiness(); return ready })
	r.stop(t)
	never(t, done, 2*time.Second, "an unelected run gave up on the bus")
	cancel()
	_ = receive(t, done, 10*time.Second, "the run to stop")
}

// A full outage under the election: the leader gives its term up, and the
// lease cycles (released, retaken, the term start waits out the bus and
// gives up again) without a term number being written; the first term once
// the bus is back is the next number, not one per cycle.
func TestAFullOutageCyclesTheLeaseAndWritesNoTerm(t *testing.T) {
	shortBusWatch(t, time.Second)
	r := newBusLostRig(t)
	cs := fake.NewClientset(heldLease("", 1))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	e := Election{
		Client: cs, Namespace: testNamespace, Lease: testLease, Identity: "pod-a",
		LeaseDuration: testLeaseDuration, RenewDeadline: testRenewDeadline, RetryPeriod: testRetryPeriod,
	}
	leads := make(chan struct{}, 16)
	done := make(chan error, 1)
	go func() {
		done <- e.Run(ctx, r.g, func(work context.Context, o RunOptions) error {
			leads <- struct{}{}
			return r.g.RunWith(work, o)
		})
	}()
	receive(t, leads, 10*time.Second, "the first term")
	waitFor(t, "the relay to bind", func() bool { ready, _ := r.g.Readiness(); return ready })
	before := r.term(t)
	if before == 0 {
		t.Fatal("precondition: the first term wrote no term number")
	}

	r.stop(t)
	// The run gives up, the lease is released, and the replica campaigns
	// again: at least two releases while the bus is down.
	released := 0
	deadline := time.Now().Add(20 * time.Second)
	for released < 2 && time.Now().Before(deadline) {
		if leaseHolder(t, cs) == "" {
			released++
			for leaseHolder(t, cs) == "" && time.Now().Before(deadline) {
				time.Sleep(testRetryPeriod / 4)
			}
		}
		time.Sleep(testRetryPeriod / 4)
	}
	if released < 2 {
		t.Fatalf("lease released %d times during the outage, want at least 2", released)
	}
	select {
	case err := <-done:
		t.Fatalf("Run returned during the outage: %v", err)
	default:
	}

	r.restart(t)
	waitFor(t, "a term once the bus is back", func() bool { ready, _ := r.g.Readiness(); return ready })
	if after := r.term(t); after != before+1 {
		t.Fatalf("term after the outage = %d, want %d: a term number was written while the bus was down", after, before+1)
	}
}
