package busmigrate

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// The forward restore's max_consumers is the snapshot's own raised to the
// render's, never lowered; no limit stays no limit, and a render of zero
// decides nothing.
func TestWidenedMaxConsumers(t *testing.T) {
	for _, c := range []struct{ snapshot, rendered, want int }{
		{110, 150, 150}, // narrower: widened
		{150, 110, 150}, // wider: kept
		{110, 110, 110},
		{-1, 150, -1}, // no limit: kept
		{0, 150, 0},
		{110, 0, 110}, // the render decides nothing
		{110, -1, 110},
	} {
		if got := widenedMaxConsumers(c.snapshot, c.rendered); got != c.want {
			t.Errorf("widenedMaxConsumers(%d, %d) = %d, want %d", c.snapshot, c.rendered, got, c.want)
		}
	}
}

// restoredMaxConsumers restores TASKS, snapshotted at snapshotWidth, under a
// policy rendering rendered, and returns the restored stream's max_consumers.
func restoredMaxConsumers(t *testing.T, snapshotWidth, rendered int) int {
	t.Helper()
	meta, data := snapshotTasks(t)
	meta.Config.MaxConsumers = snapshotWidth
	policy := policyOf(meta.Config)
	policy.MaxConsumers = rendered
	err, _, nc := restoreTasksUnder(t, meta, data, policy)
	if err != nil {
		t.Fatalf("snapshot %d, render %d: %v", snapshotWidth, rendered, err)
	}
	js, _ := nc.JetStream()
	info, err := js.StreamInfo("TASKS")
	if err != nil {
		t.Fatal(err)
	}
	if info.State.Msgs != meta.State.Msgs {
		t.Errorf("restored %d messages, want the snapshot's %d", info.State.Msgs, meta.State.Msgs)
	}
	return info.Config.MaxConsumers
}

// A TASKS snapshotted narrower than the render comes back at the render's
// width: the one moment JetStream lets max_consumers change.
func TestARestoreWidensANarrowerStream(t *testing.T) {
	if got := restoredMaxConsumers(t, 110, 150); got != 150 {
		t.Errorf("restored max_consumers %d, want the render's 150", got)
	}
}

// A TASKS wider than the render, or with no limit, keeps its width: its
// consumers must still fit.
func TestARestoreNeverNarrowsAStream(t *testing.T) {
	if got := restoredMaxConsumers(t, 200, 150); got != 200 {
		t.Errorf("restored max_consumers %d, want the snapshot's 200", got)
	}
	if got := restoredMaxConsumers(t, -1, 150); got != -1 {
		t.Errorf("restored max_consumers %d, want no limit (-1)", got)
	}
}

// A run file from an operator that renders no max_consumers keeps the
// snapshot's own, as before.
func TestARenderWithoutMaxConsumersKeepsTheSnapshots(t *testing.T) {
	if got := restoredMaxConsumers(t, 110, 0); got != 110 {
		t.Errorf("restored max_consumers %d, want the snapshot's 110", got)
	}
}

// The policy holds the stream to the width it was sent with: a restored
// stream at any other width fails the read-back, and a policy rendering a
// width is compared on it while one rendering none is not.
func TestCheckComparesMaxConsumersOnlyWhenRendered(t *testing.T) {
	meta, _ := snapshotTasks(t)
	cfg := meta.Config
	cfg.MaxConsumers = 110
	policy := policyOf(cfg)
	if d := policy.Check(cfg); len(d) != 0 {
		t.Errorf("no width rendered: %v, want a match", d)
	}
	policy.MaxConsumers = 150
	if d := policy.Check(cfg); len(d) != 1 || !strings.Contains(d[0], "max_consumers: want 150, got 110") {
		t.Errorf("width rendered: %v, want max_consumers named", d)
	}
}

// The way back puts the stream back as the snapshot recorded it, its width
// included, whatever the render says.
func TestTheWayBackKeepsTheSnapshotsWidth(t *testing.T) {
	meta, data := snapshotTasks(t)
	meta.Config.MaxConsumers = 110
	policy := policyOf(meta.Config)
	policy.MaxConsumers = 150
	dst := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	defer shutdown(dst)
	nc, err := nats.Connect(dst.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	if err := RestoreAsItWas(context.Background(), nc, meta.Config, meta.State, opener(data), Digest(data), policy); err != nil {
		t.Fatalf("the way back: %v", err)
	}
	js, _ := nc.JetStream()
	info, err := js.StreamInfo("TASKS")
	if err != nil {
		t.Fatal(err)
	}
	if info.Config.MaxConsumers != 110 {
		t.Errorf("way back max_consumers %d, want the snapshot's 110", info.Config.MaxConsumers)
	}
}

// The forward restore pod, into three servers, widens a narrower TASKS and
// keeps a wider one, and a pod run again takes the restored stream as done
// rather than restoring it again, at either width; the way back, into one,
// restores the snapshot's width.
func TestTheRestorePodWidensForwardAndNotOnTheWayBack(t *testing.T) {
	ctx := context.Background()
	for _, c := range []struct{ snapshot, want int }{{110, 150}, {200, 200}} {
		meta, data := snapshotTasks(t)
		meta.Config.MaxConsumers = c.snapshot
		policy := policyOf(meta.Config)
		policy.MaxConsumers = 150
		dir, run := stageTasks(t, meta, data, policy)
		base := t.TempDir()
		s0 := startServer(t, "a2a-nats-0", filepath.Join(base, "0"), 16251)
		s1 := startServer(t, "a2a-nats-1", filepath.Join(base, "1"), 16252)
		s2 := startServer(t, "a2a-nats-2", filepath.Join(base, "2"), 16253)
		nc, err := nats.Connect(s1.ClientURL())
		if err != nil {
			t.Fatal(err)
		}
		js, _ := nc.JetStream()
		for deadline := time.Now().Add(30 * time.Second); ; time.Sleep(250 * time.Millisecond) {
			if _, err := js.AccountInfo(); err == nil {
				break
			} else if time.Now().After(deadline) {
				t.Fatalf("the cluster's JetStream never came up: %v", err)
			}
		}
		var created time.Time
		for i := 1; i <= 2; i++ {
			if _, err := restoreAll(ctx, nc, []string{"TASKS"}, dir, run, 3); err != nil {
				t.Fatalf("snapshot %d, run %d: %v", c.snapshot, i, err)
			}
			info, err := streamInfo(ctx, nc, "TASKS")
			if err != nil {
				t.Fatal(err)
			}
			if info.Config.MaxConsumers != c.want || info.Config.Replicas != 3 || info.State.Msgs != meta.State.Msgs {
				t.Errorf("snapshot %d, run %d: max_consumers %d, %d replicas, %d msgs; want %d, 3 and %d", c.snapshot, i, info.Config.MaxConsumers, info.Config.Replicas, info.State.Msgs, c.want, meta.State.Msgs)
			}
			if i == 1 {
				created = info.Created
			} else if !info.Created.Equal(created) {
				t.Errorf("snapshot %d: the second run re-created the stream", c.snapshot)
			}
		}
		nc.Close()
		shutdown(s2, s1, s0)
	}

	meta, data := snapshotTasks(t)
	meta.Config.MaxConsumers = 110
	policy := policyOf(meta.Config)
	policy.MaxConsumers = 150
	dir, run := stageTasks(t, meta, data, policy)
	dst := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	defer shutdown(dst)
	nc, err := nats.Connect(dst.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	for i := 1; i <= 2; i++ {
		if _, err := restoreAll(ctx, nc, []string{"TASKS"}, dir, run, 1); err != nil {
			t.Fatalf("way back, run %d: %v", i, err)
		}
	}
	info, err := streamInfo(ctx, nc, "TASKS")
	if err != nil {
		t.Fatal(err)
	}
	if info.Config.MaxConsumers != 110 {
		t.Errorf("way back max_consumers %d, want the snapshot's 110", info.Config.MaxConsumers)
	}
}
