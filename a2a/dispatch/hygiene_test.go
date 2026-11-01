package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// putRecord writes a lease record into the bucket as a previous dispatcher
// would have.
func (b *bus) putRecord(profile, tier, pod, taskID string, expiresAt time.Time) string {
	b.t.Helper()
	kv, err := b.js.KeyValue(b.ctx, lib.ProfileLeaseBucket)
	if err != nil {
		b.t.Fatal(err)
	}
	key := lib.ProfileLeaseKey(profile, tier, pod, taskID)
	data, _ := json.Marshal(lib.LeaseRecord{TaskID: taskID, Pod: pod, Kind: lib.SubmitKindTop,
		ConfirmedAt: expiresAt.Add(-time.Minute), RenewedAt: expiresAt.Add(-3 * time.Second), ExpiresAt: expiresAt})
	if _, err := kv.Put(b.ctx, key, data); err != nil {
		b.t.Fatal(err)
	}
	return key
}

// The sweep removes a record nothing holds once it is long expired and its
// durables are bound (its submission is gone: a settle whose delete failed,
// a crash between the ack and the delete). A live record, and one for a
// profile the config doesn't list (its submission could still come back),
// stay.
func TestTheSweepRemovesARecordNothingHolds(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	gone := b.putRecord("tse", "read", "tse-0", "task-gone", time.Now().Add(-time.Hour))
	live := b.putRecord("tse", "read", "tse-0", "task-live", time.Now().Add(time.Hour))
	unbound := b.putRecord("other", "read", "other-0", "task-x", time.Now().Add(-time.Hour))
	cfg := testConfig()
	cfg.SweepAfter = time.Second
	b.runDispatcher(cfg, oneProfile("tse", 2, "tse-0"), &fakeVerifier{})
	waitFor(t, 10*time.Second, "the expired record swept", func() bool {
		_, ok := b.leases()[gone]
		return !ok
	})
	time.Sleep(time.Second) // several more sweeps
	l := b.leases()
	if _, ok := l[live]; !ok {
		t.Errorf("the sweep removed a live record")
	}
	if _, ok := l[unbound]; !ok {
		t.Errorf("the sweep removed the record of a profile it doesn't bind")
	}
}

// A long-expired record doesn't keep its pod busy: it is no worker's
// current task.
func TestAnExpiredRecordDoesNotKeepItsPodBusy(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	b.putRecord("tse", "read", "tse-0", "task-old", time.Now().Add(-time.Hour))
	w := b.worker("tse", "read", "tse-0", answerAccept, 300*time.Millisecond)
	cfg := testConfig()
	cfg.SweepAfter = 2 * time.Hour // not swept: the expiry alone frees the pod
	b.runDispatcher(cfg, oneProfile("tse", 2, "tse-0"), &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted || ts.from != "tse-0" {
		t.Errorf("terminal %+v, want tse-0's completed", ts)
	}
	if w.offerCount() != 1 {
		t.Errorf("offered %d times", w.offerCount())
	}
}

// stubDelivery is a delivery whose AckSync fails syncFails times first.
type stubDelivery struct {
	mu        sync.Mutex
	syncFails int
	acks      int
	syncs     int
}

func (s *stubDelivery) Envelope() *lib.Envelope { return &lib.Envelope{} }
func (s *stubDelivery) Subject() string         { return "" }
func (s *stubDelivery) Received() time.Time     { return time.Time{} }
func (s *stubDelivery) NumDelivered() uint64    { return 1 }
func (s *stubDelivery) Nak(time.Duration) error { return nil }
func (s *stubDelivery) Term() error             { return nil }
func (s *stubDelivery) Ack() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.acks++
	return nil
}
func (s *stubDelivery) AckSync(context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.syncs++
	if s.syncs <= s.syncFails {
		return errors.New("no answer from the server")
	}
	return nil
}

// A settle writes the task's tombstone before it acks, and frees the slot
// only once the server has confirmed the ack: the submission is never
// without a record, so one that comes back (an unconfirmed ack, a replay)
// is never offered again.
func TestASettleWaitsForTheConfirmedAck(t *testing.T) {
	b := newBus(t)
	d, err := New(testConfig(), b.connect("dispatcher"), oneProfile("tse", 2).src, NewStaticPool(), &fakeVerifier{}, quietLog())
	if err != nil {
		t.Fatal(err)
	}
	key := b.putRecord("tse", "read", "tse-0", "task-1", time.Now().Add(time.Hour))
	rec := b.records()[key]
	h, _ := d.reserve(testDispatchConfig(6, testProfile("tse", 2)), testProfile("tse", 2), "read", lib.SubmitKindTop, "task-1")
	del := &stubDelivery{syncFails: 3}
	done := make(chan struct{})
	go func() {
		defer close(done)
		d.settle(b.ctx, quietLog(), del, h, key, rec)
	}()
	time.Sleep(700 * time.Millisecond) // inside the unconfirmed retries
	if _, ok := b.tombstones()[key]; !ok {
		t.Fatalf("no tombstone while the ack is unconfirmed: %v", b.records())
	}
	if d.holder(h.key()) == nil {
		t.Fatal("the slot was freed before the server confirmed the ack")
	}
	<-done
	if tomb, ok := b.tombstones()[key]; !ok || tomb.TaskID != "task-1" || tomb.Pod != "tse-0" {
		t.Errorf("after the settle the record is %+v (tombstone %v), want task-1's tombstone on tse-0", tomb, ok)
	}
	if del.syncs != 4 {
		t.Errorf("%d AckSync calls, want 4 (three unanswered, one confirmed)", del.syncs)
	}
	if d.holder(h.key()) != nil {
		t.Error("the slot was not freed")
	}
}

// A worker flooding the dispatcher with answers for tasks it doesn't hold
// (accepts for nothing offered, renewals for unknown task ids) gets a few
// answered per second and the rest dropped: no abandon, KV read, goroutine
// or log line per message. Real work goes on meanwhile.
func TestAFloodOfUnknownAnswersIsBounded(t *testing.T) {
	b := newBus(t, "tse-0", "noisy")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	noisy := b.worker("tse", "read", "noisy", answerSilent, 0)
	d, _ := b.runDispatcher(testConfig(), oneProfile("tse", 2, "tse-0"), &fakeVerifier{})
	time.Sleep(300 * time.Millisecond)

	const flood = 500
	start := time.Now()
	for i := 0; i < flood; i++ {
		id := fmt.Sprintf("bogus-%d", i)
		noisy.publish(lib.DispatchAcceptSubject("tse", "read", "noisy"), lib.DispatchAccept{TaskID: id, Accepted: true})
		noisy.publish(lib.DispatchLeaseSubject("tse", "read", "noisy"), lib.LeaseRenewal{TaskID: id})
	}
	if err := noisy.nc.Flush(); err != nil {
		t.Fatal(err)
	}
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Errorf("the real task: %+v", ts)
	}
	elapsed := time.Since(start)
	windows := int(elapsed/unknownAnswerWindow) + 2
	if n := noisy.abandonCount(); n > windows*unknownAnswersPerWindow {
		t.Errorf("%d abandons for %d unasked accepts in %v, want at most %d", n, flood, elapsed, windows*unknownAnswersPerWindow)
	}
	if dropped := d.droppedAnswers.Load(); dropped < flood {
		t.Errorf("%d answers dropped of %d, want most", dropped, 2*flood)
	}
	if w.offerCount() != 1 {
		t.Errorf("the real task was offered %d times", w.offerCount())
	}
}

// Retries of a submission over a cap reuse the verifier's permit rather
// than calling it on each retry delay.
func TestOverCapRetriesReuseTheVerifiersPermit(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	b.worker("tse", "read", "tse-0", answerAccept, 2500*time.Millisecond)
	v := &fakeVerifier{}
	b.runDispatcher(testConfig(), oneProfile("tse", 1, "tse-0"), v)
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "task-1 admitted", func() bool { return len(v.callList()) == 1 })
	b.submit("tse", "read", lib.SubmitKindTop, "task-2") // over the profile's concurrency of 1 for ~2s
	if ts := b.waitTerminal("task-2", 20*time.Second); ts.state != lib.StateCompleted {
		t.Errorf("task-2: %+v", ts)
	}
	if calls := v.callList(); len(calls) != 2 {
		t.Errorf("%d verifier calls for two tasks, one retried over its cap many times: %v", len(calls), calls)
	}
}

// A task id is one submission per (profile, tier, kind): the same id in two
// profiles is two tasks, each run on its own profile's worker.
func TestATaskIDIsHeldPerProfile(t *testing.T) {
	b := newBus(t, "tse-0", "tse2-0")
	b.ensure("tse", 2, 0, "read")
	b.ensure("tse2", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 1500*time.Millisecond)
	w2 := b.worker("tse2", "read", "tse2-0", answerAccept, 1500*time.Millisecond)
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.Profiles = append(c.Profiles, testProfile("tse2", 2)) })
	ti.pool.Set("tse2", "read", "tse2-0")
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	b.submit("tse2", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 15*time.Second, "both tasks done", func() bool { return w.completed.Load() == 1 && w2.completed.Load() == 1 })
	waitFor(t, 5*time.Second, "both submissions settled", func() bool {
		a1, n1 := b.pending(lib.ProfileDurableName("tse", "read", lib.SubmitKindTop))
		a2, n2 := b.pending(lib.ProfileDurableName("tse2", "read", lib.SubmitKindTop))
		return a1+a2 == 0 && n1+n2 == 0 && len(b.leases()) == 0
	})
}

// A task re-adopted after a restart keeps the deadline its offer carried,
// not the profile's current limit.
func TestARestartKeepsTheOffersDeadline(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.Profiles[0].ActiveDeadlineSeconds = 4 })
	_, stop1 := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the confirm", func() bool { return w.confirmCount() == 1 })
	confirmed := time.Now()
	time.Sleep(time.Second)
	stop1()
	ti.update(func(c *lib.DispatchConfig) { c.Profiles[0].ActiveDeadlineSeconds = 60 })
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	ts := b.waitTerminal("task-1", 10*time.Second)
	if ts.state != lib.StateFailed || !hasReason(ts, ReasonDeadlineExceeded) {
		t.Errorf("terminal %+v, want failed deadline-exceeded", ts)
	}
	if took := time.Since(confirmed); took > 8*time.Second {
		t.Errorf("failed %v after the confirm; the offer's deadline was 4s", took)
	}
}

// A regrant that can't succeed (the grants never catch up) backs off for
// many seconds; meanwhile the Run loop goes on binding the durables it can,
// so another profile's work is not held up behind it.
func TestARegrantDoesNotHoldUpTheRunLoop(t *testing.T) {
	b := newBusWith(t, dispatcherUsers(), "tse-0")
	b.worker("tse", "read", "tse-0", answerAccept, 300*time.Millisecond)
	var g grantSwitch
	g.set("disp-old") // never granted tse2
	cfg := fastRegrant()
	cfg.RegrantBackoff = 2 * time.Second // 2+4+8+16s of backoff in all
	ti := oneProfile("tse", 2, "tse-0")
	d, _ := b.runDispatcherOn(b.connectAs(&g), cfg, ti, &fakeVerifier{})
	// tse's durables don't exist yet: the first binds give up after their
	// window, and the retries are the Run loop's to start.
	time.Sleep(1500 * time.Millisecond)
	// tse2 needs a regrant, which will fail.
	ti.update(func(c *lib.DispatchConfig) { c.Version = "v2"; c.Profiles = append(c.Profiles, testProfile("tse2", 2)) })
	waitFor(t, 10*time.Second, "the regrant started", func() bool { return d.Reconnects() >= 1 })
	b.ensure("tse", 2, 0, "read")
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 8*time.Second); ts.state != lib.StateCompleted {
		t.Errorf("task-1: %+v", ts)
	}
	if n := d.Reconnects(); n >= int64(cfg.RegrantAttempts) {
		t.Errorf("%d reconnects: the regrant finished first, so this proves nothing", n)
	}
}

// envDelivery is a stub delivery carrying a given envelope.
type envDelivery struct {
	stubDelivery
	env *lib.Envelope
}

func (e *envDelivery) Envelope() *lib.Envelope { return e.env }

// The verifier's permit covers only the capability and target it was given
// for. PROFILE_SUBMITS keeps one submission per subject and a newer one
// replaces it, so a retry under the same task id can name another capability
// or another cluster: that is checked afresh, never the cached permit
// stretched over it.
func TestARetryNamingAnotherTargetOrCapabilityIsCheckedAgain(t *testing.T) {
	v := &fakeVerifier{}
	d := &Dispatcher{verifier: v, verified: map[holdKey]permit{}}
	prof := testProfile("tse", 1)
	snap := lib.DispatchConfig{Namespace: "kubeagents-system", Profiles: []lib.DispatchProfile{prof}}
	hk := holdKey{"tse", "read", lib.SubmitKindTop, "task-1"}
	now := time.Now()
	clusterA := map[string]string{paramProject: "p", paramLocation: "us-central1", paramCluster: "a"}
	clusterB := map[string]string{paramProject: "p", paramLocation: "us-central1", paramCluster: "b"}
	admit := func(capKey string, params map[string]string) {
		t.Helper()
		del := &envDelivery{env: submissionEnv(t, "tse", "task-1", capKey, params)}
		if _, ref, err := d.admit(context.Background(), del, snap, prof, hk, now); err != nil || ref != nil {
			t.Fatalf("admit: refusal %+v, err %v", ref, err)
		}
	}
	admit("cap-a", clusterA)
	admit("cap-a", clusterA) // the same ref and target: the permit is reused
	if n := len(v.callList()); n != 1 {
		t.Fatalf("%d verifier calls for one submission retried unchanged; want 1", n)
	}
	admit("cap-a", clusterB) // another cluster under the same task id
	admit("cap-b", clusterB) // another capability
	if calls := v.callList(); len(calls) != 3 {
		t.Fatalf("verifier calls %v; want each new target and capability checked", calls)
	}
}
