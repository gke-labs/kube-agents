package dispatch

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats.go/jetstream"
)

// An unreadable lease record has one meaning for every reader (#4813): a
// started task that is not over, last seen alive when its entry was written.
// A record that decodes but carries neither a renewal nor a finish is not
// this dispatcher's shape and means the same.
func TestAnUnreadableRecordDecodesAsStartedAtItsWrite(t *testing.T) {
	b := newBus(t, "tse-0")
	ti := oneProfile("tse", 2, "tse-0")
	d, err := New(testConfig(), b.connect("dispatcher"), ti.src, ti.pool, &fakeVerifier{}, quietLog())
	if err != nil {
		t.Fatal(err)
	}
	key := lib.ProfileLeaseKey("tse", "read", "tse-0", "task-1")
	written := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	lease := seconds(ti.src.Config().LeaseSeconds)
	for name, value := range map[string]string{
		"not json":       "{not a lease record",
		"empty object":   "{}",
		"no times":       `{"taskId":"task-1","pod":"tse-0","kind":"top"}`,
		"another schema": `{"v":2,"holder":"tse-0"}`,
		"no expiry":      `{"taskId":"task-1","pod":"tse-0","renewedAt":"2026-11-06T11:00:00Z"}`,
	} {
		rec, readable := d.decodeLease(key, []byte(value), written)
		if readable {
			t.Errorf("%s: decoded as readable", name)
		}
		want := lib.LeaseRecord{TaskID: "task-1", Pod: "tse-0", ConfirmedAt: written, RenewedAt: written, ExpiresAt: written.Add(lease)}
		if rec != want {
			t.Errorf("%s: decoded %+v, want %+v", name, rec, want)
		}
		if rec.Finished() {
			t.Errorf("%s: decoded as a tombstone", name)
		}
	}

	running := lib.LeaseRecord{TaskID: "task-1", Pod: "tse-0", Kind: lib.SubmitKindTop,
		ConfirmedAt: written, RenewedAt: written, ExpiresAt: written.Add(time.Minute)}
	tombstone := running
	tombstone.FinishedAt = written
	for name, want := range map[string]lib.LeaseRecord{"running": running, "tombstone": tombstone} {
		data, _ := json.Marshal(want)
		rec, readable := d.decodeLease(key, data, written.Add(time.Hour))
		if !readable || !rec.ConfirmedAt.Equal(want.ConfirmedAt) || !rec.ExpiresAt.Equal(want.ExpiresAt) || rec.Finished() != want.Finished() {
			t.Errorf("%s: decoded %+v (readable %v), want %+v", name, rec, readable, want)
		}
	}
}

// The round-4 case: a worker renews a task whose record is unreadable
// while its submission is away, after this dispatcher's restart grace has
// passed. The renewal is the task's sign of life from the pod its key names,
// so the dispatcher (the bucket's one writer) writes the record back readable;
// when the submission comes back and is re-adopted, the record is live and
// the first tick doesn't fail a running task.
func TestARenewalRewritesAnUnreadableRecordForALaterReadoption(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	key := b.putUnreadable("tse", "read", "tse-0", "task-1")
	b.runDispatcher(testConfig(), oneProfile("tse", 2, "tse-0"), &fakeVerifier{})
	// Past the 2s restart grace and the 3s lease counted from the write.
	time.Sleep(5500 * time.Millisecond)

	w.publish(lib.DispatchLeaseSubject("tse", "read", "tse-0"), lib.LeaseRenewal{TaskID: "task-1"})
	waitFor(t, 5*time.Second, "the unreadable record rewritten by the renewal", func() bool {
		rec := b.records()[key]
		return rec.TaskID == "task-1" && rec.Pod == "tse-0" && time.Since(rec.RenewedAt) < 3*time.Second
	})

	renewing := make(chan struct{})
	renewed := make(chan struct{})
	go func() {
		defer close(renewed)
		tick := time.NewTicker(500 * time.Millisecond)
		defer tick.Stop()
		for {
			select {
			case <-renewing:
				return
			case <-tick.C:
				w.publish(lib.DispatchLeaseSubject("tse", "read", "tse-0"), lib.LeaseRenewal{TaskID: "task-1"})
			}
		}
	}()
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	time.Sleep(2 * time.Second) // several ticks of the re-adopted hold
	close(renewing)
	<-renewed
	if ts := b.terminalsOf("task-1"); len(ts) != 0 {
		t.Fatalf("a re-adopted task its worker still renews was ended: %+v", ts)
	}
	w.publish(lib.DispatchLeaseSubject("tse", "read", "tse-0"), lib.LeaseRenewal{TaskID: "task-1", Final: true})
	waitFor(t, 5*time.Second, "the task settled", func() bool { return b.tombstones()[key].Finished() })
	if ts := b.terminalsOf("task-1"); len(ts) != 0 {
		t.Errorf("a settled task got a terminal from the dispatcher: %+v", ts)
	}
	if n := w.offerCount(); n != 0 {
		t.Errorf("a task its record says started was offered %d times", n)
	}
}

// leasedPods reads an unreadable record as any other: it keeps its pod from
// new work while its lease, counted from its write, runs, and not after.
func TestAnUnreadableRecordHoldsItsPodOnlyUntilItExpires(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	ti := oneProfile("tse", 2, "tse-0")
	d, err := New(testConfig(), b.connect("dispatcher"), ti.src, ti.pool, &fakeVerifier{}, quietLog())
	if err != nil {
		t.Fatal(err)
	}
	b.putUnreadable("tse", "read", "tse-0", "task-1")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	pods, err := d.leasedPods(ctx, "tse", "read", 0)
	if err != nil || !pods["tse-0"] {
		t.Fatalf("a fresh unreadable record: leased pods %v (%v), want tse-0", pods, err)
	}
	lease := seconds(ti.src.Config().LeaseSeconds)
	d.now = func() time.Time { return time.Now().Add(lease + time.Second) }
	pods, err = d.leasedPods(ctx, "tse", "read", 0)
	if err != nil || pods["tse-0"] {
		t.Fatalf("an expired unreadable record: leased pods %v (%v), want none", pods, err)
	}
}

// The lease bucket is read in one pass: every record under the filter once,
// and a pass cut short by its context returns the error that cut it, which
// the sweep logs ("lease sweep cut short") rather than skip the rest
// without a word.
func TestALeaseWatchPassReadsEveryRecordOrSaysWhyNot(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	for _, task := range []string{"task-1", "task-2", "task-3"} {
		b.putUnreadable("tse", "read", "tse-0", task)
	}
	kv, err := b.js.KeyValue(b.ctx, lib.ProfileLeaseBucket)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	if err := watchLeases(b.ctx, kv, ">", func(e jetstream.KeyValueEntry) { seen[e.Key()] = true }); err != nil {
		t.Fatalf("a full pass: %v", err)
	}
	if len(seen) != 3 {
		t.Errorf("a full pass read %v, want the three records", seen)
	}

	ctx, cancel := context.WithCancel(b.ctx)
	cancel()
	if err := watchLeases(ctx, kv, ">", func(jetstream.KeyValueEntry) {}); err == nil {
		t.Errorf("a pass with no time left reported no error")
	}
}

// A failed task's terminal goes on its profile's .supervisor, a grant fixed
// per connection from the profiles the config lists. Once the profile has
// left the config the publish can be refused for good, so it is given up on
// after DroppedTerminalGiveUp and the task settled anyway (#4813). A profile
// still listed is retried for as long as it takes.
func TestADroppedProfilesTerminalIsGivenUpOnAfterABound(t *testing.T) {
	b := newBusWith(t, dispatcherUsers(), "tse-0")
	var g grantSwitch
	g.set("disp-old") // may not publish on tse2's .supervisor
	cfg := testConfig()
	// Above one attempt (each bounded by kvOpTimeout), so a give-up at the
	// first refusal would come in under it and be caught.
	cfg.DroppedTerminalGiveUp = kvOpTimeout + 2*time.Second
	h := &holding{profile: "tse2", tier: "read", kind: lib.SubmitKindTop, taskID: "task-1"}
	del := &envDelivery{env: submissionEnv(t, "tse2", "task-1", "", map[string]string{})}

	dropped := oneProfile("tse", 2, "tse-0") // tse2 is not in the config
	d, err := New(cfg, b.connectAs(&g), dropped.src, dropped.pool, &fakeVerifier{}, quietLog())
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(b.ctx, 30*time.Second)
	defer cancel()
	start := time.Now()
	if !d.publishTerminalRetry(ctx, quietLog(), del, h, lib.StateFailed, "lease expired", "") {
		t.Fatalf("a dropped profile's refused terminal was not given up on")
	}
	if took := time.Since(start); took < cfg.DroppedTerminalGiveUp {
		t.Errorf("given up after %v, before the %v bound", took, cfg.DroppedTerminalGiveUp)
	}

	listed := oneProfile("tse", 2, "tse-0")
	listed.update(func(c *lib.DispatchConfig) { c.Profiles = append(c.Profiles, testProfile("tse2", 2)) })
	d2, err := New(cfg, b.connectAs(&g), listed.src, listed.pool, &fakeVerifier{}, quietLog())
	if err != nil {
		t.Fatal(err)
	}
	// Room for several attempts (each bounded by kvOpTimeout), so a give-up
	// past the bound would have happened.
	short, cancelShort := context.WithTimeout(b.ctx, 3*kvOpTimeout)
	defer cancelShort()
	if d2.publishTerminalRetry(short, quietLog(), del, h, lib.StateFailed, "lease expired", "") {
		t.Errorf("a listed profile's refused terminal was given up on")
	}
}
