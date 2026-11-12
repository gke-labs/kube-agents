package busmigrate

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// snapshotTasks seeds a one-server bus and snapshots TASKS from it.
func snapshotTasks(t *testing.T) (*Metadata, []byte) {
	t.Helper()
	src := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	defer shutdown(src)
	seedBus(t, src.ClientURL(), 1)
	nc, err := nats.Connect(src.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	var buf bytes.Buffer
	meta, err := Snapshot(context.Background(), nc, "TASKS", &buf)
	if err != nil {
		t.Fatal(err)
	}
	return meta, buf.Bytes()
}

// restoreTasksUnder restores TASKS into a fresh server under policy, counting the
// RESTORE requests sent, and returns the error and the server's connection.
func restoreTasksUnder(t *testing.T, meta *Metadata, data []byte, policy StreamPolicy) (error, int, *nats.Conn) {
	t.Helper()
	dst := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	t.Cleanup(func() { shutdown(dst) })
	nc, err := nats.Connect(dst.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	sent := 0
	sub, err := nc.Subscribe("$JS.API.STREAM.RESTORE.>", func(*nats.Msg) { sent++ })
	if err != nil {
		t.Fatal(err)
	}
	defer sub.Unsubscribe()
	err = RestoreVerified(context.Background(), nc, meta.Config, meta.State, meta.TakenAt, opener(data), Digest(data), policy)
	if ferr := nc.Flush(); ferr != nil {
		t.Fatal(ferr)
	}
	return err, sent, nc
}

// The restore sends the rendered max_bytes and max_age, not the snapshot's,
// and the read-back holds the stream to them.
func TestARestoreSendsTheRenderedLimits(t *testing.T) {
	meta, data := snapshotTasks(t)
	policy := policyOf(meta.Config)
	policy.MaxBytes = meta.Config.MaxBytes * 2
	policy.MaxAge = 72 * time.Hour
	if meta.Config.MaxBytes == policy.MaxBytes || meta.Config.MaxAge == policy.MaxAge {
		t.Fatal("the render must differ from the snapshot for this test to mean anything")
	}
	err, _, nc := restoreTasksUnder(t, meta, data, policy)
	if err != nil {
		t.Fatalf("a snapshot that fits the rendered limits: %v", err)
	}
	js, _ := nc.JetStream()
	info, err := js.StreamInfo("TASKS")
	if err != nil {
		t.Fatal(err)
	}
	if info.Config.MaxBytes != policy.MaxBytes || info.Config.MaxAge != policy.MaxAge {
		t.Errorf("restored max_bytes %d max_age %s, want the render's %d and %s", info.Config.MaxBytes, info.Config.MaxAge, policy.MaxBytes, policy.MaxAge)
	}
	if info.State.Msgs != meta.State.Msgs {
		t.Errorf("restored %d messages, want the snapshot's %d", info.State.Msgs, meta.State.Msgs)
	}
}

// A snapshot holding more bytes than the rendered max_bytes is refused before
// anything is sent, naming the cause, and leaves no stream behind.
func TestASnapshotOverTheRenderedBytesIsRefused(t *testing.T) {
	meta, data := snapshotTasks(t)
	if meta.State.Bytes < 2 {
		t.Fatalf("the seed holds %d bytes", meta.State.Bytes)
	}
	policy := policyOf(meta.Config)
	policy.MaxBytes = int64(meta.State.Bytes) - 1
	err, sent, nc := restoreTasksUnder(t, meta, data, policy)
	if !errors.Is(err, ErrOverRenderedLimits) {
		t.Fatalf("err = %v, want ErrOverRenderedLimits", err)
	}
	if sent != 0 {
		t.Errorf("%d RESTORE requests sent for a snapshot over the rendered cap", sent)
	}
	js, _ := nc.JetStream()
	if _, err := js.StreamInfo("TASKS"); !errors.Is(err, nats.ErrStreamNotFound) {
		t.Errorf("TASKS after the refusal: %v, want no stream", err)
	}
}

// A snapshot whose oldest message was already older than the rendered
// max_age when it was taken (a stream an earlier install kept longer) is
// refused the same way.
func TestASnapshotOlderThanTheRenderedAgeIsRefused(t *testing.T) {
	meta, data := snapshotTasks(t)
	if meta.State.Msgs == 0 || meta.State.FirstTime.IsZero() {
		t.Fatal("the seed has no oldest message")
	}
	policy := policyOf(meta.Config)
	policy.MaxAge = time.Second
	time.Sleep(1100 * time.Millisecond)
	meta.TakenAt = time.Now().UTC() // taken once the oldest message was past max_age
	err, sent, _ := restoreTasksUnder(t, meta, data, policy)
	if !errors.Is(err, ErrOverRenderedLimits) {
		t.Fatalf("err = %v, want ErrOverRenderedLimits", err)
	}
	if sent != 0 {
		t.Errorf("%d RESTORE requests sent for a snapshot older than the rendered max_age", sent)
	}
}

// A stream inside the rendered max_age when its snapshot was taken is
// restored though its oldest message ages past it during the freeze: the
// live bus trims to the same age, so the restored stream's own max_age
// trims only what the bus would have dropped anyway.
func TestAStreamThatAgedDuringTheFreezeIsRestored(t *testing.T) {
	meta, data := snapshotTasks(t)
	if meta.TakenAt.IsZero() || meta.State.FirstTime.IsZero() {
		t.Fatal("the snapshot records no time, or the seed no oldest message")
	}
	policy := policyOf(meta.Config)
	policy.MaxAge = 3 * time.Minute // at least the stream's 2m duplicate window
	// Taken a minute after the oldest message: inside max_age then, so it
	// is restored whatever the time is now.
	inside := meta
	inside.TakenAt = meta.State.FirstTime.Add(time.Minute)
	if err, _, _ := restoreTasksUnder(t, inside, data, policy); err != nil {
		t.Fatalf("taken inside max_age: err = %v, want the stream restored", err)
	}
	// Taken four minutes after it: past max_age then, so it is refused,
	// though only seconds have passed since the seed.
	past := meta
	past.TakenAt = meta.State.FirstTime.Add(4 * time.Minute)
	if err, _, _ := restoreTasksUnder(t, past, data, policy); !errors.Is(err, ErrOverRenderedLimits) {
		t.Fatalf("taken past max_age: err = %v, want ErrOverRenderedLimits", err)
	}
}

// The fit check's edges: at the cap and inside the age it passes, an empty
// stream has no oldest message, and a limit of zero is none.
func TestFitsRenderedLimitsEdges(t *testing.T) {
	now := time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC)
	p := StreamPolicy{MaxBytes: 100, MaxAge: time.Hour}
	for _, tc := range []struct {
		name  string
		state nats.StreamState
		p     StreamPolicy
		ok    bool
	}{
		{"at the cap", nats.StreamState{Bytes: 100, Msgs: 1, FirstTime: now.Add(-time.Minute)}, p, true},
		{"one over the cap", nats.StreamState{Bytes: 101, Msgs: 1, FirstTime: now.Add(-time.Minute)}, p, false},
		{"exactly max_age old", nats.StreamState{Bytes: 1, Msgs: 1, FirstTime: now.Add(-time.Hour)}, p, true},
		{"older than max_age", nats.StreamState{Bytes: 1, Msgs: 1, FirstTime: now.Add(-time.Hour - time.Second)}, p, false},
		{"empty stream, old first time", nats.StreamState{Msgs: 0, FirstTime: now.Add(-48 * time.Hour)}, p, true},
		{"no limits", nats.StreamState{Bytes: 1 << 40, Msgs: 1, FirstTime: now.Add(-1000 * time.Hour)}, StreamPolicy{}, true},
	} {
		err := fitsRenderedLimits("TASKS", tc.state, tc.p, now)
		if (err == nil) != tc.ok {
			t.Errorf("%s: err = %v, want ok=%v", tc.name, err, tc.ok)
		}
		if err != nil && !errors.Is(err, ErrOverRenderedLimits) {
			t.Errorf("%s: err = %v, want ErrOverRenderedLimits", tc.name, err)
		}
	}
}

// The way back restores the stream as it was, its own limits included, so a
// snapshot the forward restore refuses for not fitting the render still goes
// back into one server, read back against its own limits.
func TestTheWayBackRestoresAStreamThatDoesNotFitTheRender(t *testing.T) {
	meta, data := snapshotTasks(t)
	policy := policyOf(meta.Config)
	policy.MaxBytes = int64(meta.State.Bytes) - 1
	dst := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	defer shutdown(dst)
	nc, err := nats.Connect(dst.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	ctx := context.Background()
	if err := RestoreVerified(ctx, nc, meta.Config, meta.State, meta.TakenAt, opener(data), Digest(data), policy); !errors.Is(err, ErrOverRenderedLimits) {
		t.Fatalf("forward: %v, want ErrOverRenderedLimits", err)
	}
	if err := RestoreAsItWas(ctx, nc, meta.Config, meta.State, opener(data), Digest(data), policy); err != nil {
		t.Fatalf("the way back: %v", err)
	}
	js, _ := nc.JetStream()
	info, err := js.StreamInfo("TASKS")
	if err != nil {
		t.Fatal(err)
	}
	if info.Config.MaxBytes != meta.Config.MaxBytes || info.State.Msgs != meta.State.Msgs {
		t.Errorf("way back: max_bytes %d msgs %d, want the snapshot's %d and %d", info.Config.MaxBytes, info.State.Msgs, meta.Config.MaxBytes, meta.State.Msgs)
	}
	// Every field but the limits is still the policy's: a widened subject is
	// refused on the way back too.
	widened := meta.Config
	widened.Subjects = []string{"a2a.>"}
	if err := RestoreAsItWas(ctx, nc, widened, meta.State, opener(data), Digest(data), policy); !errors.Is(err, ErrOutsidePolicy) {
		t.Errorf("way back with a widened subject: %v, want ErrOutsidePolicy", err)
	}
}

// stageTasks writes TASKS's snapshot the way the snapshot pod stages it, and
// the run file the operator writes for it under policy.
func stageTasks(t *testing.T, meta *Metadata, data []byte, policy StreamPolicy) (string, RunFile) {
	t.Helper()
	dir := t.TempDir()
	metaBytes, err := json.Marshal(meta)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(dir, "TASKS"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "TASKS", SnapshotMetaFile), metaBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "TASKS", SnapshotDataFile), data, 0o600); err != nil {
		t.Fatal(err)
	}
	return dir, RunFile{
		Digests:  map[string]StagedDigest{"TASKS": {Data: Digest(data), Meta: Digest(metaBytes)}},
		Policies: []StreamPolicy{policy},
	}
}

// The restore pod picks its restore by where it restores to: into three
// servers a snapshot over the rendered cap is refused at once, not retried;
// into one (the way back) it goes back as it was, and a pod run again finds
// it already built rather than restoring onto an existing name.
func TestTheRestorePodPicksItsRestoreByLeg(t *testing.T) {
	meta, data := snapshotTasks(t)
	policy := policyOf(meta.Config)
	policy.MaxBytes = int64(meta.State.Bytes) - 1
	dir, run := stageTasks(t, meta, data, policy)
	dst := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	defer shutdown(dst)
	nc, err := nats.Connect(dst.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	ctx := context.Background()

	start := time.Now()
	if _, err := restoreAll(ctx, nc, []string{"TASKS"}, dir, run, 3); !errors.Is(err, ErrOverRenderedLimits) {
		t.Fatalf("into three servers: %v, want ErrOverRenderedLimits", err)
	}
	if took := time.Since(start); took > 5*time.Second {
		t.Errorf("the refusal took %v; it was retried", took)
	}
	for i := 1; i <= 2; i++ {
		if _, err := restoreAll(ctx, nc, []string{"TASKS"}, dir, run, 1); err != nil {
			t.Fatalf("into one server, run %d: %v", i, err)
		}
	}
	js, _ := nc.JetStream()
	info, err := js.StreamInfo("TASKS")
	if err != nil {
		t.Fatal(err)
	}
	if info.Config.MaxBytes != meta.Config.MaxBytes || info.State.Msgs != meta.State.Msgs {
		t.Errorf("way back: max_bytes %d msgs %d, want the snapshot's %d and %d", info.Config.MaxBytes, info.State.Msgs, meta.Config.MaxBytes, meta.State.Msgs)
	}
}
