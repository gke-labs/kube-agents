package busmigrate

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"testing"

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
	if _, err := restoreAllDirected(ctx, open(t), []string{"TASKS"}, dir, run, 1, true); !errors.Is(err, ErrOverRenderedLimits) {
		t.Errorf("forward into one server over the cap: %v, want ErrOverRenderedLimits", err)
	}
	// Back into one server: the same snapshot goes back as it was.
	if _, err := restoreAllDirected(ctx, open(t), []string{"TASKS"}, dir, run, 1, false); err != nil {
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
		if _, err := restoreAllDirected(ctx, nc, []string{"TASKS"}, dir, run, 1, c.forward); err != nil {
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
