package busmigrate

import (
	"bytes"
	"context"
	"errors"
	"io"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// One to three, end to end on embedded servers: snapshot every stream off a
// standalone server, restart its store in a three-server cluster (which
// deletes the standalone streams as orphans), restore each from bytes whose
// digest matches with the operator's config at R1, raise it to R3, and read
// everything back through another server. A snapshot changed after hashing is
// refused before the restore's final message, and no stream is created.
func TestOneToThreeKeepsTheBus(t *testing.T) {
	ctx := context.Background()
	base := t.TempDir()
	d0 := filepath.Join(base, "0")
	streams := []string{"TASKS", "KV_cap"}

	alone := startServer(t, "a2a-nats-0", d0, 0)
	seedBus(t, alone.ClientURL(), 1)
	rendered := streamConfigs(t, alone.ClientURL(), streams...) // the operator's policy, in this test
	nc, err := nats.Connect(alone.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	data, metas, digests := map[string][]byte{}, map[string]*Metadata{}, map[string]string{}
	for _, n := range streams {
		var buf bytes.Buffer
		meta, err := Snapshot(ctx, nc, n, &buf)
		if err != nil {
			t.Fatalf("snapshot %s: %v", n, err)
		}
		data[n], metas[n], digests[n] = buf.Bytes(), meta, Digest(buf.Bytes())
	}
	nc.Close()
	shutdown(alone)

	s0 := startServer(t, "a2a-nats-0", d0, 16251)
	s1 := startServer(t, "a2a-nats-1", filepath.Join(base, "1"), 16252)
	s2 := startServer(t, "a2a-nats-2", filepath.Join(base, "2"), 16253)
	defer shutdown(s2, s1, s0)
	nc, err = nats.Connect(s1.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	// The conversion empties the bus: wait for the orphan cleanup.
	for i := 0; ; i++ {
		_, err := streamInfo(ctx, nc, "TASKS")
		if err != nil && strings.Contains(err.Error(), "not found") {
			break
		}
		if i > 60 {
			t.Fatalf("TASKS still there after the conversion: %v", err)
		}
		time.Sleep(time.Second)
	}

	tampered := append([]byte(nil), data["TASKS"]...)
	tampered[len(tampered)/2] ^= 0xff
	if err := RestoreVerified(ctx, nc, metas["TASKS"].Config, metas["TASKS"].State, opener(tampered), digests["TASKS"], policyOf(withReplicas(rendered["TASKS"], 1))); !errors.Is(err, ErrDigestMismatch) {
		t.Fatalf("a snapshot changed after hashing: %v, want ErrDigestMismatch", err)
	}
	if _, err := streamInfo(ctx, nc, "TASKS"); err == nil {
		t.Fatal("a refused snapshot still created TASKS")
	}

	// A snapshot that changes after the pre-pass (a second open returns other
	// bytes) is caught as it is sent: no stream, and the name is free again.
	opens := 0
	late := func() (io.ReadCloser, error) {
		opens++
		if opens == 1 {
			return io.NopCloser(bytes.NewReader(data["TASKS"])), nil
		}
		return io.NopCloser(bytes.NewReader(tampered)), nil
	}
	for i := 0; ; i++ {
		err := RestoreVerified(ctx, nc, metas["TASKS"].Config, metas["TASKS"].State, late, digests["TASKS"], policyOf(withReplicas(rendered["TASKS"], 1)))
		if errors.Is(err, ErrDigestMismatch) {
			break
		}
		if i > 30 {
			t.Fatalf("a snapshot changed after the pre-pass: %v, want ErrDigestMismatch", err)
		}
		opens = 0
		time.Sleep(time.Second)
	}
	if _, err := streamInfo(ctx, nc, "TASKS"); err == nil {
		t.Fatal("a late-tampered snapshot created TASKS")
	}

	for _, n := range streams {
		var err error
		for i := 0; i < 30; i++ {
			if err = RestoreVerified(ctx, nc, metas[n].Config, metas[n].State, opener(data[n]), digests[n], policyOf(withReplicas(rendered[n], 1))); err == nil {
				break
			}
			t.Logf("restore %s attempt %d: %v", n, i, err)
			time.Sleep(time.Second)
		}
		if err != nil {
			t.Fatalf("restore %s: %v", n, err)
		}
		if err := RaiseReplicas(ctx, nc, n, 3); err != nil {
			t.Fatalf("%s to R3: %v", n, err)
		}
	}
	time.Sleep(2 * time.Second)

	rc, err := nats.Connect(s2.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer rc.Close()
	js, _ := rc.JetStream()
	si, err := js.StreamInfo("TASKS")
	if err != nil || si.State.Msgs != 5 || si.State.FirstSeq != 1 || si.State.LastSeq != 5 || si.Config.Replicas != 3 {
		t.Fatalf("TASKS after the restore: %+v, err %v; want 5 msgs at 1-5, R3", si, err)
	}
	ci, err := js.ConsumerInfo("TASKS", "worker")
	if err != nil || ci.AckFloor.Stream != 2 || ci.NumPending != 3 {
		t.Errorf("worker after the restore: %+v, err %v; want ack floor 2 and 3 pending", ci, err)
	}
	kv, err := js.KeyValue("cap")
	if err != nil {
		t.Fatal(err)
	}
	e, err := kv.Get("k")
	if err != nil || string(e.Value()) != "3" || e.Revision() != 3 {
		t.Errorf("cap/k after the restore: %v, err %v; want 3 at revision 3", e, err)
	}
}

func opener(b []byte) Opener {
	return func() (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(b)), nil }
}

func withReplicas(cfg nats.StreamConfig, n int) nats.StreamConfig {
	cfg.Replicas = n
	return cfg
}

// A restored stream whose config reads back outside its policy is deleted:
// here a stream capturing another subject.
func TestARestoredStreamOutsideItsPolicyIsDeleted(t *testing.T) {
	ctx := context.Background()
	s := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	defer shutdown(s)
	nc, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, _ := nc.JetStream()
	if _, err := js.AddStream(&nats.StreamConfig{Name: "TASKS", Subjects: []string{"other.>"}}); err != nil {
		t.Fatal(err)
	}
	want := nats.StreamConfig{Name: "TASKS", Subjects: []string{"a2a.tasks.>"}, Replicas: 1}
	si, _ := js.StreamInfo("TASKS")
	policy := policyOf(withReplicas(si.Config, 1))
	policy.Subjects = want.Subjects
	if err := verifyOrDelete(ctx, nc, policy); err == nil || !strings.Contains(err.Error(), "deleted") {
		t.Fatalf("verifyOrDelete: %v, want the mismatch deleted", err)
	}
	if _, err := js.StreamInfo("TASKS"); err == nil {
		t.Fatal("the mismatched stream is still there")
	}
	// And a matching one stands.
	if _, err := js.AddStream(&want); err != nil {
		t.Fatal(err)
	}
	if err := verifyOrDelete(ctx, nc, policy); err != nil {
		t.Fatalf("a stream inside its policy: %v", err)
	}
}

// A snapshot config outside the policy sends nothing: no RESTORE, so no
// stream and no held name, and the right snapshot restores straight after.
func TestASnapshotConfigOutsideThePolicySendsNothing(t *testing.T) {
	ctx := context.Background()
	src := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	seedBus(t, src.ClientURL(), 1)
	nc, err := nats.Connect(src.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	meta, err := Snapshot(ctx, nc, "TASKS", &buf)
	if err != nil {
		t.Fatal(err)
	}
	nc.Close()
	shutdown(src)
	policy := policyOf(meta.Config)

	dst := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	defer shutdown(dst)
	nc, err = nats.Connect(dst.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	sent := 0
	sub, err := nc.Subscribe("$JS.API.STREAM.RESTORE.>", func(*nats.Msg) { sent++ })
	if err != nil {
		t.Fatal(err)
	}
	defer sub.Unsubscribe()
	widened := meta.Config
	widened.Subjects = []string{"a2a.>"}
	if err := RestoreVerified(ctx, nc, widened, meta.State, opener(buf.Bytes()), Digest(buf.Bytes()), policy); !errors.Is(err, ErrOutsidePolicy) {
		t.Fatalf("a widened snapshot config: %v, want ErrOutsidePolicy", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	if sent != 0 {
		t.Errorf("%d RESTORE requests sent for a config outside the policy", sent)
	}
	if err := RestoreVerified(ctx, nc, meta.Config, meta.State, opener(buf.Bytes()), Digest(buf.Bytes()), policy); err != nil {
		t.Fatalf("the snapshot's own config, inside the policy: %v", err)
	}
}
