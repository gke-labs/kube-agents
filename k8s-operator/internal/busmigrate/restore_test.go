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

// One to three, end to end on embedded servers: snapshot every stream off a
// standalone server, restart its store in a three-server cluster (which
// deletes the standalone streams as orphans), restore each from bytes whose
// digest matches with the operator's config at R1, raise it to R3, and read
// everything back through another server. A snapshot changed after hashing is
// refused before any restore request.
func TestOneToThreeKeepsTheBus(t *testing.T) {
	ctx := context.Background()
	base := t.TempDir()
	d0 := filepath.Join(base, "0")
	streams := []string{"TASKS", "KV_cap"}

	alone := startServer(t, "a2a-nats-0", d0, 0)
	seedBus(t, alone.ClientURL(), 1)
	rendered := streamConfigs(t, alone.ClientURL(), streams...) // the operator's render, in this test
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
	if err := RestoreVerified(ctx, nc, withReplicas(rendered["TASKS"], 1), metas["TASKS"].State, tampered, digests["TASKS"]); !errors.Is(err, ErrDigestMismatch) {
		t.Fatalf("a snapshot changed after hashing: %v, want ErrDigestMismatch", err)
	}
	if _, err := streamInfo(ctx, nc, "TASKS"); err == nil {
		t.Fatal("a refused snapshot still created TASKS")
	}

	for _, n := range streams {
		var err error
		for i := 0; i < 30; i++ {
			if err = RestoreVerified(ctx, nc, withReplicas(rendered[n], 1), metas[n].State, data[n], digests[n]); err == nil {
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

func withReplicas(cfg nats.StreamConfig, n int) nats.StreamConfig {
	cfg.Replicas = n
	return cfg
}

// A restored stream whose config reads back as anything but the operator's is
// deleted: here a stream capturing another subject.
func TestARestoredStreamWithTheWrongConfigIsDeleted(t *testing.T) {
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
	if err := verifyOrDelete(ctx, nc, want); err == nil || !strings.Contains(err.Error(), "deleted") {
		t.Fatalf("verifyOrDelete: %v, want the mismatch deleted", err)
	}
	if _, err := js.StreamInfo("TASKS"); err == nil {
		t.Fatal("the mismatched stream is still there")
	}
	// And a matching one stands: zero fields compare as the server's defaults.
	if _, err := js.AddStream(&want); err != nil {
		t.Fatal(err)
	}
	if err := verifyOrDelete(ctx, nc, want); err != nil {
		t.Fatalf("a stream with the operator's config: %v", err)
	}
}
