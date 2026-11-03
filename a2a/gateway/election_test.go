package gateway

import (
	"context"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	coordinationv1 "k8s.io/api/coordination/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The election's timings in these tests: a lease of 2s (client-go writes
// whole seconds), renewal given up after 1s, retried every 200ms.
const (
	testLeaseDuration = 2 * time.Second
	testRenewDeadline = time.Second
	testRetryPeriod   = 200 * time.Millisecond
	testNamespace     = "kubeagents-system"
	testLease         = "platform-agent-a2a-gateway"
)

// fakeLeader counts the terms it is asked to start.
type fakeLeader struct {
	mu      sync.Mutex
	terms   uint64
	termErr error
	fenced  func()
}

func (f *fakeLeader) BeginTerm(context.Context) (uint64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.termErr != nil {
		return 0, f.termErr
	}
	f.terms++
	return f.terms, nil
}

func (f *fakeLeader) OnFenced(fn func()) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.fenced = fn
}

func (f *fakeLeader) fence() {
	f.mu.Lock()
	fn := f.fenced
	f.mu.Unlock()
	fn()
}

func (f *fakeLeader) started() uint64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.terms
}

// campaign is one replica's Election.Run, with its work replaced by a stub
// that runs until its context ends.
type campaign struct {
	leader *fakeLeader
	// leading receives the options the work started with; stopped receives
	// the relay drain the work would have stopped with, and holderAtStop
	// who held the lease at that moment.
	leading      chan RunOptions
	stopped      chan time.Duration
	holderAtStop chan string
	done         chan error
	cancel       context.CancelFunc
}

func runCampaign(t *testing.T, cs kubernetes.Interface, identity string) *campaign {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	c := &campaign{
		leader:       &fakeLeader{},
		leading:      make(chan RunOptions, 1),
		stopped:      make(chan time.Duration, 1),
		holderAtStop: make(chan string, 1),
		done:         make(chan error, 1),
		cancel:       cancel,
	}
	e := Election{
		Client: cs, Namespace: testNamespace, Lease: testLease, Identity: identity,
		LeaseDuration: testLeaseDuration, RenewDeadline: testRenewDeadline, RetryPeriod: testRetryPeriod,
	}
	go func() {
		c.done <- e.Run(ctx, c.leader, func(ctx context.Context, o RunOptions) error {
			c.leading <- o
			<-ctx.Done()
			// A stop takes a while (the relay's drain): long enough for a
			// release that didn't wait for it to land.
			time.Sleep(3 * testRetryPeriod)
			c.holderAtStop <- leaseHolder(t, cs)
			c.stopped <- o.StopDrain()
			return nil
		})
	}()
	t.Cleanup(cancel)
	return c
}

func leaseHolder(t *testing.T, cs kubernetes.Interface) string {
	t.Helper()
	lease, err := cs.CoordinationV1().Leases(testNamespace).Get(context.Background(), testLease, metav1.GetOptions{})
	if err != nil {
		t.Errorf("reading the lease: %v", err)
		return ""
	}
	if lease.Spec.HolderIdentity == nil {
		return ""
	}
	return *lease.Spec.HolderIdentity
}

// heldLease is a Lease held by holder, renewed now, for a lease of
// seconds.
func heldLease(holder string, seconds int32) *coordinationv1.Lease {
	now := metav1.NewMicroTime(time.Now())
	return &coordinationv1.Lease{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: testLease},
		Spec: coordinationv1.LeaseSpec{
			HolderIdentity:       &holder,
			LeaseDurationSeconds: &seconds,
			AcquireTime:          &now,
			RenewTime:            &now,
		},
	}
}

func receive[T any](t *testing.T, ch <-chan T, within time.Duration, what string) T {
	t.Helper()
	select {
	case v := <-ch:
		return v
	case <-time.After(within):
		t.Fatalf("timed out after %s waiting for %s", within, what)
		var zero T
		return zero
	}
}

func never[T any](t *testing.T, ch <-chan T, during time.Duration, what string) {
	t.Helper()
	select {
	case <-ch:
		t.Fatalf("%s", what)
	case <-time.After(during):
	}
}

// TestOneGatewayLeadsAndTheLeaseMovesOnSIGTERM: of two replicas, one does
// the work and the other waits; SIGTERM stops the leader's work while it
// still holds the lease, then releases it, and the standby starts at once,
// with no hold on its relay since the lease was given up cleanly.
func TestOneGatewayLeadsAndTheLeaseMovesOnSIGTERM(t *testing.T) {
	cs := fake.NewClientset(heldLease("", 1))
	a := runCampaign(t, cs, "pod-a")
	opts := receive(t, a.leading, 5*time.Second, "pod-a leading")
	if opts.RelayBindDelay != 0 {
		t.Fatalf("relay delay after a released lease = %s, want none", opts.RelayBindDelay)
	}
	if a.leader.started() != 1 {
		t.Fatalf("pod-a started %d terms before its work, want 1", a.leader.started())
	}
	b := runCampaign(t, cs, "pod-b")
	never(t, b.leading, 2*testLeaseDuration, "pod-b led while pod-a held the lease")
	if b.leader.started() != 0 {
		t.Fatalf("the standby started %d terms", b.leader.started())
	}

	a.cancel()
	if h := receive(t, a.holderAtStop, 5*time.Second, "pod-a stopping"); h != "pod-a" {
		t.Fatalf("lease holder while pod-a stopped = %q, want pod-a: the lease is released only after the work stops", h)
	}
	if d := receive(t, a.stopped, time.Second, "pod-a's drain"); d != relayStopDrainTimeout {
		t.Fatalf("SIGTERM drain = %s, want %s", d, relayStopDrainTimeout)
	}
	if err := receive(t, a.done, 5*time.Second, "pod-a's Run to return"); err != nil {
		t.Fatalf("Run after SIGTERM = %v, want nil", err)
	}
	// A released lease is taken on the standby's next retry, well inside
	// the lease's duration.
	opts = receive(t, b.leading, testLeaseDuration, "pod-b leading after the release")
	if opts.RelayBindDelay != 0 {
		t.Fatalf("relay delay after a release = %s, want none", opts.RelayBindDelay)
	}
	if b.leader.started() != 1 {
		t.Fatalf("pod-b started %d terms, want 1", b.leader.started())
	}
}

// TestATakeoverFromAnExpiredLeaseHoldsTheRelayBack: a holder that never
// released (it crashed) is waited out for the lease's duration, and the new
// leader's relay waits AckWait for the dead one's held events.
func TestATakeoverFromAnExpiredLeaseHoldsTheRelayBack(t *testing.T) {
	cs := fake.NewClientset(heldLease("pod-dead", int32(testLeaseDuration/time.Second)))
	start := time.Now()
	b := runCampaign(t, cs, "pod-b")
	opts := receive(t, b.leading, 3*testLeaseDuration, "pod-b taking the expired lease")
	if waited := time.Since(start); waited < testLeaseDuration-testRetryPeriod {
		t.Fatalf("took a live lease after %s, before its %s ran out", waited, testLeaseDuration)
	}
	if opts.RelayBindDelay != relayAckWait {
		t.Fatalf("relay delay after a crash = %s, want %s", opts.RelayBindDelay, relayAckWait)
	}
}

// TestARestartInItsOwnPodRetakesTheLeaseAtOnce: the identity is the pod's
// name, so a container restarting in its pod takes its own lease without
// waiting it out, still under a new term, and still holds its relay back,
// since its previous process died holding events.
func TestARestartInItsOwnPodRetakesTheLeaseAtOnce(t *testing.T) {
	cs := fake.NewClientset(heldLease("pod-a", int32(testLeaseDuration/time.Second)))
	start := time.Now()
	a := runCampaign(t, cs, "pod-a")
	opts := receive(t, a.leading, testLeaseDuration, "pod-a re-taking its own lease")
	if waited := time.Since(start); waited >= testLeaseDuration/2 {
		t.Fatalf("re-taking its own lease took %s; it shouldn't wait the lease out", waited)
	}
	if a.leader.started() != 1 {
		t.Fatalf("started %d terms, want a new one", a.leader.started())
	}
	if opts.RelayBindDelay != relayAckWait {
		t.Fatalf("relay delay after a same-pod restart = %s, want %s", opts.RelayBindDelay, relayAckWait)
	}
}

// TestNoLeaseYetHoldsTheRelayBack: with no Lease to read, nothing shows
// the last gateway released its events, so the first leader holds its
// relay as after a crash.
func TestNoLeaseYetHoldsTheRelayBack(t *testing.T) {
	a := runCampaign(t, fake.NewClientset(), "pod-a")
	if opts := receive(t, a.leading, 5*time.Second, "pod-a leading"); opts.RelayBindDelay != relayAckWait {
		t.Fatalf("relay delay with no lease = %s, want %s", opts.RelayBindDelay, relayAckWait)
	}
}

// TestACutShortCrashTakeoverKeepsTheMarker: a term that followed a crash and
// ended inside the relay's AckWait (SIGTERM, a failed term, a fenced write)
// doesn't release the lease: the dead holder's events may still be pending,
// and a release would tell the next taker nobody holds any. Left to expire,
// it still names a holder, and the next taker holds its relay too.
func TestACutShortCrashTakeoverKeepsTheMarker(t *testing.T) {
	cs := fake.NewClientset(heldLease("pod-dead", int32(testLeaseDuration/time.Second)))
	b := runCampaign(t, cs, "pod-b")
	if opts := receive(t, b.leading, 3*testLeaseDuration, "pod-b taking the expired lease"); opts.RelayBindDelay != relayAckWait {
		t.Fatalf("relay delay after a crash = %s, want %s", opts.RelayBindDelay, relayAckWait)
	}
	b.cancel()
	if err := receive(t, b.done, 5*time.Second, "pod-b's Run to return"); err != nil {
		t.Fatalf("Run after SIGTERM = %v", err)
	}
	if h := leaseHolder(t, cs); h != "pod-b" {
		t.Fatalf("lease holder after a cut-short crash takeover = %q, want pod-b left to expire", h)
	}
	c := runCampaign(t, cs, "pod-c")
	if opts := receive(t, c.leading, 3*testLeaseDuration, "pod-c taking over"); opts.RelayBindDelay != relayAckWait {
		t.Fatalf("next taker's relay delay = %s, want %s", opts.RelayBindDelay, relayAckWait)
	}
}

// TestAReleasedLeaseStartsWithoutTheHoldBack: a lease its last holder gave
// up holds nobody's events, so the relay binds at once.
func TestAReleasedLeaseStartsWithoutTheHoldBack(t *testing.T) {
	cs := fake.NewClientset(heldLease("", 1))
	b := runCampaign(t, cs, "pod-b")
	opts := receive(t, b.leading, testLeaseDuration, "pod-b taking a released lease")
	if opts.RelayBindDelay != 0 {
		t.Fatalf("relay delay after a release = %s, want none", opts.RelayBindDelay)
	}
}

// TestAFencedWriteStepsTheGatewayDown: a registry write refused by a newer
// term means another gateway leads, so the work stops on the short drain,
// the lease is given up, and Run reports the lost leadership the process
// exits on.
func TestAFencedWriteStepsTheGatewayDown(t *testing.T) {
	cs := fake.NewClientset(heldLease("", 1))
	a := runCampaign(t, cs, "pod-a")
	receive(t, a.leading, 5*time.Second, "pod-a leading")
	a.leader.fence()
	if d := receive(t, a.stopped, 5*time.Second, "pod-a stopping"); d != lostLeaseRelayDrain {
		t.Fatalf("drain after a fenced write = %s, want %s", d, lostLeaseRelayDrain)
	}
	err := receive(t, a.done, 5*time.Second, "Run to return")
	if !errors.Is(err, ErrLeadershipLost) || !strings.Contains(err.Error(), "newer gateway term") {
		t.Fatalf("Run after a fenced write = %v, want ErrLeadershipLost naming the term", err)
	}
	if h := leaseHolder(t, cs); h != "" {
		t.Fatalf("lease holder after stepping down = %q, want it released", h)
	}
}

// failRenewalsOf makes identity's own lease writes fail once the returned
// switch is on, as an unreachable API server would; a release (no holder)
// and another holder's write still go through. It's installed before the
// campaign starts, since the fake's reactor chain isn't safe to change
// under a running elector.
func failRenewalsOf(cs *fake.Clientset, identity string) *atomic.Bool {
	var failing atomic.Bool
	cs.PrependReactor("update", "leases", func(a k8stesting.Action) (bool, runtime.Object, error) {
		if !failing.Load() {
			return false, nil, nil
		}
		lease, ok := a.(k8stesting.UpdateAction).GetObject().(*coordinationv1.Lease)
		if ok && lease.Spec.HolderIdentity != nil && *lease.Spec.HolderIdentity == identity {
			return true, nil, errors.New("the API server is unreachable")
		}
		return false, nil, nil
	})
	return &failing
}

// TestLosingTheLeaseStepsDownWithTheShortDrain: a leader that can't renew
// stops its work on the drain that fits before a standby can take over,
// still named the holder while it stops, gives the lease up only once its
// work has stopped, and reports the lost leadership.
func TestLosingTheLeaseStepsDownWithTheShortDrain(t *testing.T) {
	cs := fake.NewClientset(heldLease("", 1))
	failing := failRenewalsOf(cs, "pod-a")
	a := runCampaign(t, cs, "pod-a")
	receive(t, a.leading, 5*time.Second, "pod-a leading")
	failing.Store(true)
	if h := receive(t, a.holderAtStop, 3*testLeaseDuration, "pod-a stopping"); h != "pod-a" {
		t.Fatalf("lease holder while pod-a stopped = %q, want pod-a: a release before the work stops lets a standby overlap it", h)
	}
	if d := receive(t, a.stopped, time.Second, "pod-a's drain"); d != lostLeaseRelayDrain {
		t.Fatalf("drain after losing the lease = %s, want %s", d, lostLeaseRelayDrain)
	}
	if err := receive(t, a.done, 5*time.Second, "Run to return"); !errors.Is(err, ErrLeadershipLost) {
		t.Fatalf("Run after losing the lease = %v, want ErrLeadershipLost", err)
	}
	if h := leaseHolder(t, cs); h != "" {
		t.Fatalf("lease holder after pod-a stopped = %q, want it released", h)
	}
}

// TestAFencedGatewayLeavesTheNewLeadersLeaseAlone: a deposed replica that
// learns of the new term from a fenced write, before its stale view of the
// lease catches up, must not clear the new leader's hold when it stops.
func TestAFencedGatewayLeavesTheNewLeadersLeaseAlone(t *testing.T) {
	// From a released lease, so pod-a's term isn't a crash takeover, which
	// keeps its lease without reaching the release at all.
	cs := fake.NewClientset(heldLease("", 1))
	failing := failRenewalsOf(cs, "pod-a")
	a := runCampaign(t, cs, "pod-a")
	receive(t, a.leading, 5*time.Second, "pod-a leading")
	// pod-a pauses; pod-b takes the lease meanwhile.
	failing.Store(true)
	lease, err := cs.CoordinationV1().Leases(testNamespace).Get(context.Background(), testLease, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	holder := "pod-b"
	lease.Spec.HolderIdentity = &holder
	if _, err := cs.CoordinationV1().Leases(testNamespace).Update(context.Background(), lease, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	a.leader.fence()
	if err := receive(t, a.done, 5*time.Second, "Run to return"); !errors.Is(err, ErrLeadershipLost) {
		t.Fatalf("Run after a fenced write = %v, want ErrLeadershipLost", err)
	}
	if h := leaseHolder(t, cs); h != "pod-b" {
		t.Fatalf("lease holder after the deposed replica stopped = %q, want pod-b's hold left alone", h)
	}
}

// TestAStandbyStopsAtOnceOnSIGTERM: a replica waiting for the lease has no
// work to stop.
func TestAStandbyStopsAtOnceOnSIGTERM(t *testing.T) {
	cs := fake.NewClientset(heldLease("pod-x", 3600))
	b := runCampaign(t, cs, "pod-b")
	time.Sleep(3 * testRetryPeriod)
	b.cancel()
	if err := receive(t, b.done, time.Second, "the standby's Run to return"); err != nil {
		t.Fatalf("standby's Run after SIGTERM = %v, want nil", err)
	}
	never(t, b.leading, testRetryPeriod, "the standby led")
	if h := leaseHolder(t, cs); h != "pod-x" {
		t.Fatalf("lease holder = %q, want pod-x untouched", h)
	}
}

// TestATermThatWontStartLeadsNothing: a gateway that can't start a term
// would write under an old one, so it doesn't start its work at all, gives
// the lease up, and fails.
func TestATermThatWontStartLeadsNothing(t *testing.T) {
	cs := fake.NewClientset(heldLease("", 1))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	leader := &fakeLeader{termErr: errors.New("the session-state bucket is unreachable")}
	e := Election{
		Client: cs, Namespace: testNamespace, Lease: testLease, Identity: "pod-a",
		LeaseDuration: testLeaseDuration, RenewDeadline: testRenewDeadline, RetryPeriod: testRetryPeriod,
	}
	led := false
	err := e.Run(ctx, leader, func(context.Context, RunOptions) error {
		led = true
		return nil
	})
	if err == nil || !strings.Contains(err.Error(), "starting a gateway term") {
		t.Fatalf("Run = %v, want the term's failure", err)
	}
	if led {
		t.Fatal("the work started without a term")
	}
	if h := leaseHolder(t, cs); h != "" {
		t.Fatalf("lease holder = %q, want it released", h)
	}
}

// electedRig is a second gateway on r's bus and bucket, not yet run.
func electedRig(t *testing.T, r *rig) *rig {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	client, err := lib.Connect(ctx, r.url, lib.WithName("gateway-test-b"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)
	adapter := newFakeAdapter()
	cfg := *r.g.cfg
	g, err := New(Options{Client: client, Adapter: adapter, Config: &cfg, Backend: "discord", Spawner: &fakeSpawner{}})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return &rig{g: g, adapter: adapter, client: client, bus: r.bus, url: r.url, stop: cancel, ran: make(chan struct{})}
}

// elect runs a gateway under the election on cs until r.stop.
func elect(t *testing.T, cs kubernetes.Interface, r *rig, identity string) <-chan error {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	done := make(chan error, 1)
	stop := r.stop
	r.stop = func() { cancel(); stop() }
	e := Election{
		Client: cs, Namespace: testNamespace, Lease: testLease, Identity: identity,
		LeaseDuration: testLeaseDuration, RenewDeadline: testRenewDeadline, RetryPeriod: testRetryPeriod,
	}
	go func() {
		done <- e.Run(ctx, r.g, func(ctx context.Context, o RunOptions) error {
			// No dead holder in these tests: the relay's hold is pinned by
			// the campaign tests and the crash test below.
			o.RelayBindDelay = 0
			return r.g.RunWith(ctx, o)
		})
	}()
	return done
}

// TestTheWorkFollowsTheLease: two gateways on one bus, one lease. The
// leader starts term 1 and the standby runs nothing, so a message sent to
// the standby's backend waits. On the leader's SIGTERM the standby takes
// the lease, starts term 2, and answers the message.
func TestTheWorkFollowsTheLease(t *testing.T) {
	base, _ := startRigWithSpawnerCap(t, targetPlatform, 0, nil)
	base.stop()
	<-base.ran
	cs := fake.NewClientset()
	a := electedRig(t, base)
	aDone := elect(t, cs, a, "pod-a")
	waitFor(t, "pod-a's term", func() bool { return a.g.reg.Epoch() == 1 })

	b := electedRig(t, base)
	bDone := elect(t, cs, b, "pod-b")
	b.adapter.inbox <- InboundMessage{Conversation: "discord:g1/t-follow", Kind: "dm", AuthorID: "1001", MessageID: "m-follow", Text: "how is the fleet?"}
	time.Sleep(2 * testLeaseDuration)
	if n := len(b.adapter.inbox); n != 1 {
		t.Fatalf("the standby took %d message(s) off its backend", 1-n)
	}
	if b.g.reg.Epoch() != 0 {
		t.Fatalf("the standby is at term %d", b.g.reg.Epoch())
	}

	a.stop()
	if err := receive(t, aDone, 20*time.Second, "pod-a's Run to return"); err != nil {
		t.Fatalf("pod-a after SIGTERM: %v", err)
	}
	waitFor(t, "pod-b's term", func() bool { return b.g.reg.Epoch() == 2 })
	origin := b.awaitTask(t, targetPlatform)
	if origin.TaskID == "" {
		t.Fatal("the new leader submitted no task")
	}
	b.stop()
	if err := receive(t, bDone, 20*time.Second, "pod-b's Run to return"); err != nil {
		t.Fatalf("pod-b after SIGTERM: %v", err)
	}
}

// TestADeposedGatewaysFencedWriteStepsItDown: a gateway whose term another
// has superseded finds out at its next record write, through the registry,
// and stops.
func TestADeposedGatewaysFencedWriteStepsItDown(t *testing.T) {
	base, _ := startRigWithSpawnerCap(t, targetPlatform, 0, nil)
	base.stop()
	<-base.ran
	cs := fake.NewClientset()
	a := electedRig(t, base)
	aDone := elect(t, cs, a, "pod-a")
	waitFor(t, "pod-a's term", func() bool { return a.g.reg.Epoch() == 1 })

	// A newer leader, as a paused process would find on waking.
	if term, err := NewRegistry(base.bus).BeginTerm(context.Background()); err != nil || term != 2 {
		t.Fatalf("the newer term = %d, %v", term, err)
	}
	a.adapter.inbox <- InboundMessage{Conversation: "discord:g1/t-deposed", Kind: "dm", AuthorID: "1001", MessageID: "m-deposed", Text: "how is the fleet?"}
	err := receive(t, aDone, 20*time.Second, "the deposed gateway's Run to return")
	if !errors.Is(err, ErrLeadershipLost) {
		t.Fatalf("Run = %v, want ErrLeadershipLost", err)
	}
}

// TestATurnTakenAtTheStopIsDropped: once the gateway is stopping, a turn
// still queued is logged and dropped, not run on an ended context.
func TestATurnTakenAtTheStopIsDropped(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	waitRunning(t, r)
	r.stop()
	<-r.ran
	r.g.handleInbound(InboundMessage{Conversation: "discord:g1/t-dropped", Kind: "dm", AuthorID: "1001", MessageID: "m-dropped", Text: "how is the fleet?"})
	if !strings.Contains(r.logs.String(), "turn dropped: the gateway is stopping") || !strings.Contains(r.logs.String(), "discord:g1/t-dropped") {
		t.Fatalf("no drop logged with the conversation:\n%s", r.logs.String())
	}
	if envs := inSubjectEnvelopes(t, r.url, targetPlatform); len(envs) != 0 {
		t.Fatalf("a dropped turn submitted %d envelope(s)", len(envs))
	}
}

// TestATurnWaitingOnTheLockWhenTheStopBeginsIsDropped: a turn that passed
// the first check while the gateway ran, then waited on its conversation's
// lock (held by a relay render, a reap or a sweep), is dropped once it gets
// the lock after the stop began.
func TestATurnWaitingOnTheLockWhenTheStopBeginsIsDropped(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	waitRunning(t, r)
	conv := "discord:g1/t-lock-wait"
	l := r.g.lockSession(conv)
	l.Lock()
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-lock-wait", Text: "how is the fleet?"}
	waitFor(t, "the turn taken off the backend", func() bool { return len(r.adapter.inbox) == 0 })
	time.Sleep(200 * time.Millisecond)
	r.stop()
	<-r.ran
	l.Unlock()
	waitFor(t, "the drop logged", func() bool {
		return strings.Contains(r.logs.String(), "turn dropped: the gateway is stopping") && strings.Contains(r.logs.String(), conv)
	})
	if envs := inSubjectEnvelopes(t, r.url, targetPlatform); len(envs) != 0 {
		t.Fatalf("a turn dropped at the stop submitted %d envelope(s)", len(envs))
	}
}

// TestAHeldBackRelayBindsAfterItsDelay: ingress starts at once and the
// relay's durable is bound only once the delay has passed.
func TestAHeldBackRelayBindsAfterItsDelay(t *testing.T) {
	const delay = 2 * time.Second
	base, _ := startRigWithSpawnerCap(t, targetPlatform, 0, nil)
	// The base rig may have bound the durable before it stopped; it's
	// cleared below, inside the delay, so only the held-back bind counts.
	r, _ := restartRigWith(t, base, nil, RunOptions{RelayBindDelay: delay})
	nc, err := nats.Connect(r.url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	if err := js.DeleteConsumer(context.Background(), lib.TasksStream, r.g.relayDurable); err != nil && !errors.Is(err, jetstream.ErrConsumerNotFound) {
		t.Fatalf("clearing the base rig's durable: %v", err)
	}
	r.adapter.inbox <- InboundMessage{Conversation: "discord:g1/t-held", Kind: "dm", AuthorID: "1001", MessageID: "m-held", Text: "how is the fleet?"}
	r.awaitTask(t, targetPlatform)
	if _, err := js.Consumer(context.Background(), lib.TasksStream, r.g.relayDurable); !errors.Is(err, jetstream.ErrConsumerNotFound) {
		t.Fatalf("the relay's durable during the delay: %v, want not bound yet", err)
	}
	waitFor(t, "the relay bound after its delay", func() bool {
		_, err := js.Consumer(context.Background(), lib.TasksStream, r.g.relayDurable)
		return err == nil
	})
	if !strings.Contains(r.logs.String(), "relay bind held back") {
		t.Fatal("the hold wasn't logged")
	}
}

// TestACrashTakeoverRendersTheDeadLeadersHeldEventsFirst: a gateway died
// holding a task's notice unacked, and the task posted a second one after.
// A new leader that holds its relay back for AckWait gets the held notice
// redelivered before the newer one, so they post in the order the task sent
// them. Binding at once would post the second, then the first once its
// AckWait ran out. Takes AckWait (30s), the server's default.
func TestACrashTakeoverRendersTheDeadLeadersHeldEventsFirst(t *testing.T) {
	if testing.Short() {
		t.Skip("waits out the relay's AckWait")
	}
	r, _ := startRigWithSpawner(t)
	conv := "discord:g1/t-crash"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-crash", Text: "how is the fleet?"}
	origin := r.awaitTask(t, targetPlatform)
	exec := r.execFor(t, origin, targetPlatform)
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the working status rendered and acked", func() bool { return relayAckPending(t, r.url, r.g.relayDurable) == 0 })

	const first, second = "notice one: draining node pool a", "notice two: node pool a drained"
	notice := func(id, text string) {
		t.Helper()
		if err := exec.PublishArtifact(context.Background(), lib.Artifact{ArtifactID: id, Name: lib.ArtifactNotice, Parts: []lib.Part{{Kind: "text", Text: text}}}); err != nil {
			t.Fatal(err)
		}
	}
	// The dead leader: it bound the relay durable, took the first notice,
	// and died without acking or giving it back.
	dies := func() {
		nc, err := nats.Connect(r.url)
		if err != nil {
			t.Fatal(err)
		}
		js, err := jetstream.New(nc)
		if err != nil {
			t.Fatal(err)
		}
		cons, err := js.CreateOrUpdateConsumer(context.Background(), lib.TasksStream, jetstream.ConsumerConfig{
			Durable:        r.g.relayDurable,
			AckPolicy:      jetstream.AckExplicitPolicy,
			MaxAckPending:  relayMaxAckPending,
			FilterSubjects: []string{"a2a.tasks.*.*." + lib.TaskClassEvents, "a2a.tasks.*.*." + lib.TaskClassSupervisor},
		})
		if err != nil {
			t.Fatal(err)
		}
		notice("n-1", first)
		batch, err := cons.Fetch(1, jetstream.FetchMaxWait(5*time.Second))
		if err != nil {
			t.Fatal(err)
		}
		if _, ok := <-batch.Messages(); !ok {
			t.Fatal("the dead leader took nothing")
		}
		nc.Close()
		notice("n-2", second)
	}
	r2, _ := restartRigWith(t, r, nil, RunOptions{RelayBindDelay: relayAckWait}, dies)
	if !pollUntilWithin(relayAckWait+15*time.Second, func() bool { return postsContaining(r2, first) > 0 && postsContaining(r2, second) > 0 }) {
		t.Fatalf("the new leader didn't post both notices; posts %q", r2.adapter.postTexts())
	}
	time.Sleep(time.Second)
	posts := r2.adapter.postTexts()
	at := func(needle string) []int {
		var idx []int
		for i, p := range posts {
			if strings.Contains(p, needle) {
				idx = append(idx, i)
			}
		}
		return idx
	}
	one, two := at(first), at(second)
	if len(one) != 1 || len(two) != 1 || one[0] > two[0] {
		t.Fatalf("posts %q: want each notice once, the held one first", posts)
	}
}

func pollUntilWithin(d time.Duration, cond func() bool) bool {
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return true
		}
		time.Sleep(50 * time.Millisecond)
	}
	return false
}
