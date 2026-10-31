package busmigrate

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// The snapshot leg stages every stream under its own directory, and bus-hash
// writes their digests to the termination log as the operator reads them:
// each the digest of exactly the staged data.
func TestTheSnapshotLegStagesWhatBusHashDigests(t *testing.T) {
	s := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	defer shutdown(s)
	seedBus(t, s.ClientURL(), 1)
	dir := t.TempDir()
	var stderr bytes.Buffer
	if code := Main(context.Background(), []string{"snapshot", "--server", s.ClientURL(), "--streams", "TASKS,KV_cap", "--dir", dir}, &stderr); code != 0 {
		t.Fatalf("snapshot leg exited %d: %s", code, stderr.String())
	}
	log := filepath.Join(t.TempDir(), "termination-log")
	if code := HashMain([]string{"--dir", dir, "--streams", "TASKS,KV_cap", "--termination-log", log}, &stderr); code != 0 {
		t.Fatalf("bus-hash exited %d: %s", code, stderr.String())
	}
	b, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]string
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatal(err)
	}
	for _, n := range []string{"TASKS", "KV_cap"} {
		data, err := os.ReadFile(filepath.Join(dir, n, SnapshotDataFile))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := os.Stat(filepath.Join(dir, n, SnapshotMetaFile)); err != nil {
			t.Errorf("%s: no metadata staged: %v", n, err)
		}
		if got[n] != Digest(data) || len(data) == 0 {
			t.Errorf("%s: termination-log digest %q, want %q of %d staged bytes", n, got[n], Digest(data), len(data))
		}
	}
}

// Missing flags are a usage error, not a run against nothing.
func TestBusMigrateRefusesAnIncompleteInvocation(t *testing.T) {
	var stderr bytes.Buffer
	for _, args := range [][]string{nil, {"pin", "--server", "nats://127.0.0.1:1", "--streams", "TASKS"}, {"snapshot", "--server", "x"}, {"bogus", "--streams", "A"}} {
		if code := Main(context.Background(), args, &stderr); code != 2 {
			t.Errorf("bus-migrate %v exited %d, want 2", args, code)
		}
	}
	if code := HashMain([]string{"--dir", t.TempDir()}, &stderr); code != 2 {
		t.Errorf("bus-hash without --streams exited %d, want 2", code)
	}
}
