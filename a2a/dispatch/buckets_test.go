package dispatch

import (
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// With bucketed grants (#5381) every per-profile subject rides the profile's
// bucket connection, and a profile the config adds reconnects that bucket
// alone: the control connection and the other buckets keep their sessions.
// Each connection here authenticates as whichever user its own switch names,
// the way the callout grants each bucket from the map it holds when that
// bucket connects.
func TestBucketedAnAddedProfileReconnectsOnlyItsBucket(t *testing.T) {
	if lib.DispatcherBucket("tse") == lib.DispatcherBucket("tse2") {
		t.Fatal("the test needs tse and tse2 in different buckets")
	}
	b := newBusWith(t, dispatcherUsers(), "tse-0", "tse2-0")
	b.ensure("tse", 2, 0, "read")
	b.ensure("tse2", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 300*time.Millisecond)
	w2 := b.worker("tse2", "read", "tse2-0", answerAccept, 300*time.Millisecond)

	var control grantSwitch
	control.set("disp-old")
	switches := make([]*grantSwitch, lib.DispatcherBuckets)
	buckets := make([]*lib.Client, lib.DispatcherBuckets)
	for i := range buckets {
		switches[i] = &grantSwitch{}
		switches[i].set("disp-old")
		buckets[i] = b.connectAs(switches[i])
	}
	ti := oneProfile("tse", 2, "tse-0")
	d, _ := b.runPrepared(b.connectAs(&control), fastRegrant(), ti, &fakeVerifier{}, func(d *Dispatcher) {
		if err := d.UseBuckets(buckets); err != nil {
			t.Fatal(err)
		}
	})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 15*time.Second); ts.state != lib.StateCompleted || ts.from != "tse-0" {
		t.Fatalf("task-1 on its bucket: %+v", ts)
	}

	// The operator renders tse2; the callout grants it on tse2's bucket.
	b2 := lib.DispatcherBucket("tse2")
	switches[b2].set("disp-new")
	ti.pool.Set("tse2", "read", "tse2-0")
	ti.update(func(c *lib.DispatchConfig) { c.Version = "v2"; c.Profiles = append(c.Profiles, testProfile("tse2", 2)) })
	waitFor(t, 15*time.Second, "tse2's bucket reconnect", func() bool { return d.BucketReconnects(b2) == 1 })
	b.submit("tse2", "read", lib.SubmitKindTop, "task-2")
	if ts := b.waitTerminal("task-2", 15*time.Second); ts.state != lib.StateCompleted || ts.from != "tse2-0" {
		t.Errorf("task-2 on the added profile's bucket: %+v", ts)
	}
	// The dispatcher's own terminal for tse2 rides the bucket too.
	b.submitWith("tse2", "read", lib.SubmitKindTop, "task-refused", "", map[string]string{})
	if ts := b.waitTerminal("task-refused", 15*time.Second); ts.state != lib.StateRejected || ts.class != lib.TaskClassSupervisor {
		t.Errorf("the dispatcher's terminal on tse2's bucket: %+v", ts)
	}
	for i := range buckets {
		if i != b2 && d.BucketReconnects(i) != 0 {
			t.Errorf("bucket %d reconnected %d times; only tse2's bucket (%d) should", i, d.BucketReconnects(i), b2)
		}
	}
	if d.Reconnects() != 1 {
		t.Errorf("%d reconnects in all, want 1", d.Reconnects())
	}
	if w.offerCount() != 1 || w2.offerCount() != 1 {
		t.Errorf("offers: tse %d, tse2 %d; want 1 each", w.offerCount(), w2.offerCount())
	}
}

// The control connection alone can't read a profile's durables when the
// grants are bucketed: the bucket connection is what the profile's subjects
// go over, so a dispatcher that never opened its buckets (an image from before
// them, under a bucketed map) binds nothing. It stays not Ready instead of
// quietly dispatching nothing.
func TestBucketedSubjectsGoOverTheBucketNotTheControlConnection(t *testing.T) {
	b := newBusWith(t, dispatcherUsers(), "tse2-0")
	b.ensure("tse2", 2, 0, "read")
	var control grantSwitch
	control.set("disp-old") // denied tse2's durables, as a bucketed control connection is
	ctl := b.connectAs(&control)
	switches := make([]*grantSwitch, lib.DispatcherBuckets)
	buckets := make([]*lib.Client, lib.DispatcherBuckets)
	for i := range buckets {
		switches[i] = &grantSwitch{}
		switches[i].set("disp-new")
		buckets[i] = b.connectAs(switches[i])
	}
	d, err := New(fastRegrant(), ctl, NewStaticSource(testDispatchConfig(6, testProfile("tse2", 2))), NewStaticPool(), &fakeVerifier{}, quietLog())
	if err != nil {
		t.Fatal(err)
	}
	if d.busFor("tse2") != ctl {
		t.Fatal("with no buckets, every profile rides the one connection")
	}
	if err := d.UseBuckets(buckets); err != nil {
		t.Fatal(err)
	}
	if got := d.busFor("tse2"); got != buckets[lib.DispatcherBucket("tse2")] {
		t.Fatal("tse2's subjects don't go over its bucket")
	}
	if err := d.UseBuckets(buckets[:3]); err == nil {
		t.Error("UseBuckets took 3 connections")
	}
	if err := d.UseBuckets(append(buckets[:lib.DispatcherBuckets-1:lib.DispatcherBuckets-1], nil)); err == nil {
		t.Error("UseBuckets took a nil connection")
	}
}
