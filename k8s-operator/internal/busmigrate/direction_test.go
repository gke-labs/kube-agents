package busmigrate

import (
	"bytes"
	"context"
	"errors"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// A reshape restores into the bus at the count it runs, so the forward
// restore can be into one server: named forward, a single-server restore
// holds the snapshot to the rendered limits (refusing one over them) and
// widens its max_consumers; named back, it restores the stream as it was.
func TestTheDirectionDecidesTheRestoreNotTheServerCount(t *testing.T) {
	ctx := context.Background()
	open := func(t *testing.T) *nats.Conn {
		t.Helper()
		dst := startServer(t, "a2a-nats-0", t.TempDir(), 0)
		t.Cleanup(func() { shutdown(dst) })
		nc, err := nats.Connect(dst.ClientURL())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(nc.Close)
		return nc
	}

	// Forward into one server: over the rendered cap is refused.
	meta, data := snapshotTasks(t)
	over := policyOf(meta.Config)
	over.MaxBytes = int64(meta.State.Bytes) - 1
	dir, run := stageTasks(t, meta, data, over)
	if _, _, err := restoreAllDirected(ctx, open(t), []string{"TASKS"}, dir, run, 1, true); !errors.Is(err, ErrOverRenderedLimits) {
		t.Errorf("forward into one server over the cap: %v, want ErrOverRenderedLimits", err)
	}
	// Back into one server: the same snapshot goes back as it was.
	if _, _, err := restoreAllDirected(ctx, open(t), []string{"TASKS"}, dir, run, 1, false); err != nil {
		t.Errorf("back into one server: %v", err)
	}

	// Forward into one server widens max_consumers; back keeps the snapshot's.
	for _, c := range []struct {
		forward bool
		want    int
	}{{true, 150}, {false, 110}} {
		meta, data := snapshotTasks(t)
		meta.Config.MaxConsumers = 110
		policy := policyOf(meta.Config)
		policy.MaxConsumers = 150
		dir, run := stageTasks(t, meta, data, policy)
		nc := open(t)
		if _, _, err := restoreAllDirected(ctx, nc, []string{"TASKS"}, dir, run, 1, c.forward); err != nil {
			t.Fatalf("forward %v: %v", c.forward, err)
		}
		info, err := streamInfo(ctx, nc, "TASKS")
		if err != nil {
			t.Fatal(err)
		}
		if info.Config.MaxConsumers != c.want || info.Config.Replicas != 1 || info.State.Msgs != meta.State.Msgs {
			t.Errorf("forward %v into one server: max_consumers %d, %d replicas, %d msgs; want %d, 1 and %d", c.forward, info.Config.MaxConsumers, info.Config.Replicas, info.State.Msgs, c.want, meta.State.Msgs)
		}
	}
}

// Without --direction the restore pod keeps today's rule (forward into more
// than one server, back into one), so a pod an older operator rendered runs
// as it did; a --direction that is neither forward nor back is refused
// before anything connects.
func TestTheRestorePodsDirectionFlag(t *testing.T) {
	var stderr bytes.Buffer
	code := Main(context.Background(), []string{"restore", "--streams", "TASKS", "--dir", t.TempDir(), "--run-file", "/nonexistent", "--direction", "sideways"}, &stderr)
	if code != 2 || !strings.Contains(stderr.String(), `--direction "sideways" is neither forward nor back`) {
		t.Errorf("exit %d, stderr %q; want 2 naming the bad direction", code, stderr.String())
	}
}

// A reshape of a three-server bus snapshots a TASKS at three replicas, and
// the operator's policy is at one (a2aStreamPolicies(agent, 1)). Both
// directions restore it at one replica and raise it to three, keeping its
// messages; neither refuses it for its replica count.
func TestAThreeReplicaSnapshotRestoresIntoThreeServersBothWays(t *testing.T) {
	ctx := context.Background()
	for _, forward := range []bool{true, false} {
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
		seedBus(t, s1.ClientURL(), 3)
		var buf bytes.Buffer
		meta, err := Snapshot(ctx, nc, "TASKS", &buf)
		if err != nil {
			t.Fatal(err)
		}
		if meta.Config.Replicas != 3 {
			t.Fatalf("the snapshot records %d replicas, want 3", meta.Config.Replicas)
		}
		if err := js.DeleteStream("TASKS"); err != nil {
			t.Fatal(err)
		}
		policy := policyOf(meta.Config)
		policy.Replicas = 1 // the operator's policy, at one replica
		dir, run := stageTasks(t, meta, buf.Bytes(), policy)
		if _, _, err := restoreAllDirected(ctx, nc, []string{"TASKS"}, dir, run, 3, forward); err != nil {
			t.Fatalf("forward %v: %v", forward, err)
		}
		info, err := streamInfo(ctx, nc, "TASKS")
		if err != nil {
			t.Fatal(err)
		}
		if info.Config.Replicas != 3 || info.State.Msgs != meta.State.Msgs || info.State.LastSeq != meta.State.LastSeq {
			t.Errorf("forward %v: %d replicas, msgs %d last %d; want 3, %d and %d", forward, info.Config.Replicas, info.State.Msgs, info.State.LastSeq, meta.State.Msgs, meta.State.LastSeq)
		}
		// A pod run again finds it already restored and raised.
		if _, _, err := restoreAllDirected(ctx, nc, []string{"TASKS"}, dir, run, 3, forward); err != nil {
			t.Errorf("forward %v, run again: %v", forward, err)
		}
		nc.Close()
		shutdown(s2, s1, s0)
	}
}

// The way back into three servers (a reshape's rollback) waits for the
// consumers it restored to be assigned before it raises the stream, as the
// forward restore does: the server proposes them only after the restore
// answers, and a raise before that would leave them out of the remap.
func TestTheWayBackIntoThreeServersWaitsForItsConsumers(t *testing.T) {
	ctx := context.Background()
	base := t.TempDir()
	s0 := startServer(t, "a2a-nats-0", filepath.Join(base, "0"), 16251)
	s1 := startServer(t, "a2a-nats-1", filepath.Join(base, "1"), 16252)
	s2 := startServer(t, "a2a-nats-2", filepath.Join(base, "2"), 16253)
	defer shutdown(s2, s1, s0)
	nc, err := nats.Connect(s1.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, _ := nc.JetStream()
	for deadline := time.Now().Add(30 * time.Second); ; time.Sleep(250 * time.Millisecond) {
		if _, err := js.AccountInfo(); err == nil {
			break
		} else if time.Now().After(deadline) {
			t.Fatalf("the cluster's JetStream never came up: %v", err)
		}
	}
	seedBus(t, s1.ClientURL(), 3)
	var buf bytes.Buffer
	meta, err := Snapshot(ctx, nc, "TASKS", &buf)
	if err != nil {
		t.Fatal(err)
	}
	if err := js.DeleteStream("TASKS"); err != nil {
		t.Fatal(err)
	}
	policy := policyOf(meta.Config)
	policy.Replicas = 1
	dir, run := stageTasks(t, meta, buf.Bytes(), policy)
	const lagging = 4
	reads := 0
	defer func(l func(context.Context, *nats.Conn, string) ([]string, error)) { listAssigned = l }(listAssigned)
	real := listAssigned
	listAssigned = func(ctx context.Context, nc *nats.Conn, stream string) ([]string, error) {
		reads++
		if reads <= lagging {
			return nil, nil // not assigned yet
		}
		return real(ctx, nc, stream)
	}
	if _, _, err := restoreAllDirected(ctx, nc, []string{"TASKS"}, dir, run, 3, false); err != nil {
		t.Fatal(err)
	}
	if reads <= lagging {
		t.Errorf("the way back read the consumer list %d times, want more than %d: it raised before its consumers were assigned", reads, lagging)
	}
}
