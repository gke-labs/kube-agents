package dispatch

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// PROFILE_SUBMITS keeps acked submissions for its whole max age (limits
// retention), so a durable re-created with DeliverAll replays every task it
// ever held. A finished task leaves a tombstone under its lease key, and a
// replay that finds one is acked and nothing else.

// putTombstone writes a finished task's tombstone as a previous dispatcher
// would have; its lease still runs an hour, which must not matter.
func (b *bus) putTombstone(profile, tier, pod, taskID string, finishedAt time.Time) string {
	b.t.Helper()
	kv, err := b.js.KeyValue(b.ctx, lib.ProfileLeaseBucket)
	if err != nil {
		b.t.Fatal(err)
	}
	key := lib.ProfileLeaseKey(profile, tier, pod, taskID)
	data, _ := json.Marshal(lib.LeaseRecord{TaskID: taskID, Pod: pod, Kind: lib.SubmitKindTop,
		ConfirmedAt: finishedAt.Add(-time.Minute), RenewedAt: finishedAt, ExpiresAt: time.Now().Add(time.Hour),
		FinishedAt: finishedAt})
	if _, err := kv.Put(b.ctx, key, data); err != nil {
		b.t.Fatal(err)
	}
	return key
}

// recreateDurables deletes the profile's durables and has the operator make
// them again: a new consumer starts at the stream's first message.
func (b *bus) recreateDurables(profile string, tiers ...string) {
	b.t.Helper()
	for _, tier := range tiers {
		for _, kind := range submitKinds {
			if err := b.js.DeleteConsumer(b.ctx, lib.ProfileSubmitStream, lib.ProfileDurableName(profile, tier, kind)); err != nil {
				b.t.Fatal(err)
			}
		}
	}
	b.ensure(profile, 2, 0, tiers...)
}

// terminalCounts is each task's terminal count.
func (b *bus) terminalCounts(tasks ...string) map[string]int {
	out := map[string]int{}
	for _, task := range tasks {
		out[task] = len(b.terminalsOf(task))
	}
	return out
}

// A durable re-created over finished work: two tasks that completed and
// one refused at admission. The replays are acked; none is offered again
// (the refused one not even with the grant now permitted), and no second
// terminal is written for any.
func TestARecreatedDurableDoesNotReRunFinishedWork(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 300*time.Millisecond)
	ti := oneProfile("tse", 1, "tse-0")
	_, stop1 := b.runDispatcher(testConfig(), ti, &fakeVerifier{refuse: map[string]bool{"cap-bad": true}})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	b.submit("tse", "read", lib.SubmitKindTop, "task-2")
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-refused", "cap-bad", map[string]string{})
	durable := lib.ProfileDurableName("tse", "read", lib.SubmitKindTop)
	waitFor(t, 20*time.Second, "all three settled", func() bool {
		ack, num := b.pending(durable)
		return w.completed.Load() == 2 && len(b.terminalsOf("task-refused")) == 1 && ack == 0 && num == 0
	})
	stop1()
	tombs := b.tombstones()
	for _, key := range []string{
		lib.ProfileLeaseKey("tse", "read", "tse-0", "task-1"),
		lib.ProfileLeaseKey("tse", "read", "tse-0", "task-2"),
		lib.ProfileLeaseKey("tse", "read", lib.ProfileLeaseRefusedPod, "task-refused"),
	} {
		if _, ok := tombs[key]; !ok {
			t.Errorf("no tombstone at %s: %v", key, b.records())
		}
	}
	if l := b.leases(); len(l) != 0 {
		t.Errorf("running leases left: %v", l)
	}
	offers := w.offerCount()
	before := b.terminalCounts("task-1", "task-2", "task-refused")

	b.recreateDurables("tse", "read")
	if _, num := b.pending(durable); num != 3 {
		t.Fatalf("the re-created durable has %d to deliver, want the 3 replays", num)
	}
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	waitFor(t, 10*time.Second, "the replays acked", func() bool {
		ack, num := b.pending(durable)
		return ack == 0 && num == 0
	})
	time.Sleep(4 * time.Second) // more than a lease and the restart grace
	if n := w.offerCount(); n != offers {
		t.Errorf("offered %d times after the replay, want %d: finished work ran again", n, offers)
	}
	if after := b.terminalCounts("task-1", "task-2", "task-refused"); after["task-1"] != before["task-1"] ||
		after["task-2"] != before["task-2"] || after["task-refused"] != before["task-refused"] {
		t.Errorf("terminals %v after the replay, want %v", after, before)
	}
}

// A task the dispatcher failed lease-expired is just as finished: a replay
// neither offers it nor fails it again.
func TestAFailedTaskIsNotReRunOnReplay(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	ti := oneProfile("tse", 2, "tse-0")
	_, stop1 := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the offer", func() bool { return w.offerCount() == 1 })
	time.Sleep(1500 * time.Millisecond)
	w.kill()
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateFailed || !hasReason(ts, ReasonLeaseExpired) {
		t.Fatalf("terminal %+v, want failed lease-expired", ts)
	}
	durable := lib.ProfileDurableName("tse", "read", lib.SubmitKindTop)
	waitFor(t, 10*time.Second, "the failure settled", func() bool {
		ack, num := b.pending(durable)
		return ack == 0 && num == 0
	})
	stop1()
	if _, ok := b.tombstones()[lib.ProfileLeaseKey("tse", "read", "tse-0", "task-1")]; !ok {
		t.Fatalf("no tombstone for the failed task: %v", b.records())
	}

	w2 := b.worker("tse", "read", "tse-0", answerAccept, 300*time.Millisecond)
	b.recreateDurables("tse", "read")
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	waitFor(t, 10*time.Second, "the replay acked", func() bool {
		ack, num := b.pending(durable)
		return ack == 0 && num == 0
	})
	time.Sleep(4 * time.Second)
	if n := w2.offerCount(); n != 0 {
		t.Errorf("a failed task was offered %d times on replay", n)
	}
	if n := len(b.terminalsOf("task-1")); n != 1 {
		t.Errorf("%d terminals for the failed task, want 1: %+v", n, b.terminalsOf("task-1"))
	}
}

// A restarted dispatcher meets a submission whose task has a tombstone (it
// finished while its ack was in flight) and one whose running lease went
// stale while nobody listened. The first is acked: not adopted, not failed,
// not offered. The second is adopted and, with no renewal in the restart
// grace, failed lease-expired: a stale running lease is never a tombstone.
func TestARestartSkipsATombstoneAndStillFailsAStaleLease(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1")
	b.ensure("tse", 2, 0, "read")
	w0 := b.worker("tse", "read", "tse-0", answerAccept, 300*time.Millisecond)
	w1 := b.worker("tse", "read", "tse-1", answerSilent, 0)
	tomb := b.putTombstone("tse", "read", "tse-0", "task-done", time.Now().Add(-time.Minute))
	b.putRecord("tse", "read", "tse-1", "task-stale", time.Now().Add(-time.Minute))
	b.submit("tse", "read", lib.SubmitKindTop, "task-done")
	b.submit("tse", "read", lib.SubmitKindTop, "task-stale")
	ti := oneProfile("tse", 2, "tse-0", "tse-1")
	// The stale record names no deadline; the profile's, from its confirm a
	// minute back, must not be what fails it.
	ti.update(func(c *lib.DispatchConfig) { c.Profiles[0].ActiveDeadlineSeconds = 600 })
	cfg := testConfig()
	cfg.SweepAfter = 2 * time.Hour
	b.runDispatcher(cfg, ti, &fakeVerifier{})
	ts := b.waitTerminal("task-stale", 15*time.Second)
	if ts.state != lib.StateFailed || !hasReason(ts, ReasonLeaseExpired) {
		t.Errorf("the stale lease's terminal %+v, want failed lease-expired", ts)
	}
	durable := lib.ProfileDurableName("tse", "read", lib.SubmitKindTop)
	waitFor(t, 10*time.Second, "both settled", func() bool {
		ack, num := b.pending(durable)
		return ack == 0 && num == 0
	})
	time.Sleep(time.Second)
	if ts := b.terminalsOf("task-done"); len(ts) != 0 {
		t.Errorf("the finished task got terminals %+v", ts)
	}
	if n := w0.offerCount() + w1.offerCount(); n != 0 {
		t.Errorf("%d offers, want none: neither task may be offered", n)
	}
	if w0.abandonCount() != 0 {
		t.Errorf("the finished task's pod was told to abandon it")
	}
	if _, ok := b.tombstones()[tomb]; !ok {
		t.Errorf("the tombstone is gone: %v", b.records())
	}
}

// The sweep keeps a tombstone while a replay of its submission can still
// come (the submit stream's retention plus the margin) and deletes it after.
// The young one here is already past the retention alone.
func TestTheSweepKeepsATombstoneForTheRetention(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	now := time.Now()
	young := b.putTombstone("tse", "read", "tse-0", "task-young", now.Add(-lib.ProfileSubmitRetention-tombstoneMargin/2))
	fresh := b.putTombstone("tse", "read", "tse-0", "task-fresh", now.Add(-time.Minute))
	old := b.putTombstone("tse", "read", "tse-0", "task-old", now.Add(-lib.ProfileSubmitRetention-2*tombstoneMargin))
	cfg := testConfig()
	cfg.SweepAfter = time.Second
	b.runDispatcher(cfg, oneProfile("tse", 2, "tse-0"), &fakeVerifier{})
	waitFor(t, 10*time.Second, "the old tombstone swept", func() bool {
		_, ok := b.records()[old]
		return !ok
	})
	time.Sleep(time.Second) // several more sweeps
	tombs := b.tombstones()
	for _, key := range []string{young, fresh} {
		if _, ok := tombs[key]; !ok {
			t.Errorf("the sweep removed %s, inside the retention and margin", key)
		}
	}
}

// A tombstone whose lease would still run keeps no pod busy: its task is
// over, so the pod is offered new work at once.
func TestATombstoneDoesNotKeepItsPodBusy(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	b.putTombstone("tse", "read", "tse-0", "task-old", time.Now())
	w := b.worker("tse", "read", "tse-0", answerAccept, 300*time.Millisecond)
	cfg := testConfig()
	cfg.SweepAfter = 2 * time.Hour
	b.runDispatcher(cfg, oneProfile("tse", 2, "tse-0"), &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted || ts.from != "tse-0" {
		t.Errorf("terminal %+v, want tse-0's completed", ts)
	}
	if w.offerCount() != 1 {
		t.Errorf("offered %d times", w.offerCount())
	}
}
