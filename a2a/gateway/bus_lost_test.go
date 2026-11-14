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
	g       *Gateway
	client  *lib.Client
	url     string
	mapFile string
	opts    *natsserver.Options
	srv     *natsserver.Server
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
	r.mapFile = mapFile
	r.client = client
	r.g = r.gatewayOn(t, client)
	return r
}

func (r *busLostRig) gatewayOn(t *testing.T, client *lib.Client) *Gateway {
	t.Helper()
	g, err := New(Options{Client: client, Adapter: newFakeAdapter(), Backend: "discord", Config: &Config{
		NATSURL: r.url, PrincipalMapPath: r.mapFile, DefaultAddressee: "platform",
		IdleTTL: 30 * time.Minute, AttributionSalt: []byte("test-salt"),
	}})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return g
}

// newGateway is a second gateway on its own client, as a restarted
// container's process has. Made while the bus is up: lib.Connect dials once.
func (r *busLostRig) newGateway(t *testing.T) *Gateway {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	client, err := lib.Connect(ctx, r.url, lib.WithName("gateway-test-2"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("second gateway client: %v", err)
	}
	t.Cleanup(client.Close)
	return r.gatewayOn(t, client)
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

// An elected run off the bus for the limit ends with ErrTermBusLost, a lost
// leadership: the process exits once the campaign has released the lease.
func TestARunOffTheBusForTheLimitGivesUp(t *testing.T) {
	shortBusWatch(t, time.Second)
	r := newBusLostRig(t)
	done := make(chan error, 1)
	go func() { done <- r.g.RunWith(context.Background(), RunOptions{BusDownLimit: termWaitBusMax}) }()
	waitFor(t, "the relay to bind", func() bool { ready, _ := r.g.Readiness(); return ready })
	r.stop(t)
	err := receive(t, done, 10*time.Second, "the run to give up on the bus")
	if !errors.Is(err, ErrTermBusLost) || !errors.Is(err, ErrLeadershipLost) || errors.Is(err, errTermBusGaveUp) {
		t.Fatalf("run off the bus = %v, want ErrTermBusLost (a lost leadership, not a term-start give-up)", err)
	}
}

// A blip shorter than the limit moves nothing, and the count starts again on
// a reconnect, so blips that add up past the limit don't either; down for
// the limit without a break does. On a stepped clock: what the count does
// is the question, not how fast a runner reconnects (#5904).
func TestTheBusDownCountStartsAgainOnAReconnect(t *testing.T) {
	const limit = 60 * time.Second
	var b busDownTracker
	at := time.Unix(0, 0)
	look := func(connected bool, step time.Duration) (time.Duration, bool) {
		at = at.Add(step)
		return b.observe(connected, at, limit)
	}
	// Ten blips of 50 s each, 500 s down in all, each followed by one
	// connected look: none reaches the limit.
	for i := 0; i < 10; i++ {
		if _, giveUp := look(false, time.Second); giveUp {
			t.Fatalf("blip %d: gave up on its first disconnected look", i)
		}
		if down, giveUp := look(false, 50*time.Second); giveUp {
			t.Fatalf("blip %d: gave up after %s down, under the %s limit", i, down, limit)
		}
		if down, giveUp := look(true, time.Second); giveUp || down != 0 {
			t.Fatalf("blip %d: a connected look = %s, %v, want the count reset", i, down, giveUp)
		}
	}
	// Down without a break: the look at 59 s holds, the look at 60 s gives up.
	look(false, time.Second)
	if down, giveUp := look(false, 59*time.Second); giveUp {
		t.Fatalf("gave up at %s, under the limit", down)
	}
	if down, giveUp := look(false, time.Second); !giveUp || down != limit {
		t.Fatalf("down %s without a break = %v, want a give-up at the limit", down, giveUp)
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

// A full outage under the election. The leader gives its term up, releases
// the lease and Run returns a lost leadership, so the process exits as on a
// lost lease. The restarted container (a fresh gateway here) campaigns: it
// takes the lease, its term start waits out the bus and releases it, and it
// stands back and campaigns again rather than exiting, all without a term
// number being written; the first term once the bus is back is the next
// number, not one per cycle.
func TestAFullOutageCyclesTheLeaseAndWritesNoTerm(t *testing.T) {
	shortBusWatch(t, time.Second)
	r := newBusLostRig(t)
	cs := fake.NewClientset(heldLease("", 1))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	elect := func(identity string) Election {
		return Election{
			Client: cs, Namespace: testNamespace, Lease: testLease, Identity: identity,
			LeaseDuration: testLeaseDuration, RenewDeadline: testRenewDeadline, RetryPeriod: testRetryPeriod,
		}
	}
	first := make(chan error, 1)
	go func() { first <- elect("pod-a").Run(ctx, r.g, r.g.RunWith) }()
	waitFor(t, "the relay to bind", func() bool { ready, _ := r.g.Readiness(); return ready })
	before := r.term(t)
	if before == 0 {
		t.Fatal("precondition: the first term wrote no term number")
	}

	// The restarted container's gateway, made now because its client dials
	// once; it campaigns only after the first process has exited.
	g2 := r.newGateway(t)
	r.stop(t)
	err := receive(t, first, 15*time.Second, "the leader to give its term up")
	if !errors.Is(err, ErrTermBusLost) || !errors.Is(err, ErrLeadershipLost) {
		t.Fatalf("Run after a mid-term outage = %v, want ErrTermBusLost", err)
	}
	if h := leaseHolder(t, cs); h != "" {
		t.Fatalf("lease holder after the give-up = %q, want it released", h)
	}

	second := make(chan error, 1)
	go func() { second <- elect("pod-a").Run(ctx, g2, g2.RunWith) }()
	// A real release: a holder seen, then none. g2 takes the empty lease
	// first, waits out the bus at its term start, and releases it; it then
	// stands back and campaigns again, so its Run keeps going. A term
	// start's bus wait retries every termWaitRetry, and each try waits out
	// the client's request timeout, so one cycle takes several seconds.
	released, prev := 0, leaseHolder(t, cs)
	deadline := time.Now().Add(45 * time.Second)
	for released < 1 && time.Now().Before(deadline) {
		time.Sleep(testRetryPeriod / 4)
		cur := leaseHolder(t, cs)
		if prev != "" && cur == "" {
			released++
		}
		prev = cur
	}
	if released < 1 {
		t.Fatalf("the restarted gateway never released the lease during the outage")
	}
	// Past a stand-back it is still campaigning, not exited.
	never(t, second, testLeaseDuration+3*testRetryPeriod, "the restarted gateway's Run returned during the outage")

	r.restart(t)
	waitFor(t, "a term once the bus is back", func() bool { ready, _ := g2.Readiness(); return ready })
	if after := r.term(t); after != before+1 {
		t.Fatalf("term after the outage = %d, want %d: a term number was written while the bus was down", after, before+1)
	}
	select {
	case err := <-second:
		t.Fatalf("the restarted gateway's Run returned once the bus was back: %v", err)
	default:
	}
}

// A give-up while the relay's bind is still waiting (here for a TASKS that
// doesn't exist yet) is reported as the give-up, not as the bind's failure
// on the cancel: the bind error would be fatal rather than a lost leadership.
func TestAGiveUpDuringTheBindIsTheGiveUp(t *testing.T) {
	shortBusWatch(t, time.Second)
	r := newBusLostRig(t)
	nc, err := nats.Connect(r.url)
	if err != nil {
		t.Fatal(err)
	}
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	if err := js.DeleteStream(context.Background(), lib.TasksStream); err != nil {
		t.Fatalf("delete TASKS: %v", err)
	}
	nc.Close()
	done := make(chan error, 1)
	go func() { done <- r.g.RunWith(context.Background(), RunOptions{BusDownLimit: termWaitBusMax}) }()
	waitFor(t, "the relay to wait for its stream", func() bool {
		_, why := r.g.Readiness()
		return strings.Contains(why, "waiting for stream")
	})
	r.stop(t)
	err = receive(t, done, 10*time.Second, "the run to give up during the bind")
	if !errors.Is(err, ErrTermBusLost) {
		t.Fatalf("run that gave up during the bind = %v, want ErrTermBusLost", err)
	}
}
