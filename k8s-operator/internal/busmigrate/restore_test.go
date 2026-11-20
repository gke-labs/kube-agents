package busmigrate

import (
	"bytes"
	"context"
	"errors"
	"fmt"
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
	if err := RestoreVerified(ctx, nc, metas["TASKS"].Config, metas["TASKS"].State, metas["TASKS"].TakenAt, opener(tampered), digests["TASKS"], policyOf(withReplicas(rendered["TASKS"], 1))); !errors.Is(err, ErrDigestMismatch) {
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
		err := RestoreVerified(ctx, nc, metas["TASKS"].Config, metas["TASKS"].State, metas["TASKS"].TakenAt, late, digests["TASKS"], policyOf(withReplicas(rendered["TASKS"], 1)))
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
		var restored int
		for i := 0; i < 30; i++ {
			if restored, err = RestoreVerifiedCounting(ctx, nc, metas[n].Config, metas[n].State, metas[n].TakenAt, opener(data[n]), digests[n], policyOf(withReplicas(rendered[n], 1))); err == nil {
				break
			}
			t.Logf("restore %s attempt %d: %v", n, i, err)
			// As the migration Job does (cli.go): a restore that stalled
			// mid-stream (a chunk with no responder) leaves its name held,
			// and every later attempt is refused 10130 until it is freed.
			if isRestoreNameInUse(err) {
				if derr := request(ctx, nc, fmt.Sprintf(streamDeleteSubject, n), nil, nil); derr != nil && !isStreamNotFound(derr) {
					t.Logf("restore %s: freeing the name: %v", n, derr)
				}
			}
			time.Sleep(time.Second)
		}
		if err != nil {
			t.Fatalf("restore %s: %v", n, err)
		}
		// As the restore pod does (cli.go's restoreAll): the server assigns
		// the consumers a restore brought back only after it answers the
		// restore, and a raise sent before then can bring a consumer up on
		// the new replicas without its restored state (ack floor 0, every
		// message pending; #6643). So wait for the assignments, then raise.
		actx, cancel := context.WithTimeout(ctx, restoreAssignTimeout)
		_, err = AssignedDurables(actx, nc, n, restored, metas[n].Config, 3)
		cancel()
		if err != nil {
			t.Fatalf("%s: waiting for its restored consumers' assignments: %v", n, err)
		}
		if err := RaiseReplicas(ctx, nc, n, 3); err != nil {
			t.Fatalf("%s to R3: %v", n, err)
		}
	}
	rc, err := nats.Connect(s2.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer rc.Close()
	js, _ := rc.JetStream()
	// RaiseReplicas only updates the stream's config: the new replicas catch
	// up, and the restored consumer reappears on its new leader, some time
	// after. On a CI runner a fixed 2s read found the consumer missing (#6174),
	// and then, with only the stream and the consumer's presence waited for,
	// present but answering from a group not yet settled (0 pending). So wait
	// for the stream at three replicas with its followers current, and for the
	// consumer's own group: a leader, both replicas current, and the restored
	// state.
	// How long it took is logged as a diagnostic: it prints with -v, and on a
	// failing run; CI's go test runs without -v, so a passing run there
	// doesn't keep it.
	allCurrent := func(peers []*nats.PeerInfo) bool {
		for _, p := range peers {
			if p == nil || !p.Current {
				return false
			}
		}
		return true
	}
	waitStart := time.Now()
	deadline := waitStart.Add(30 * time.Second)
	for {
		short := ""
		si, serr := js.StreamInfo("TASKS")
		ci, cerr := js.ConsumerInfo("TASKS", "worker")
		switch {
		case serr != nil:
			short = "stream: " + serr.Error()
		case si.Config.Replicas != 3 || si.Cluster == nil || len(si.Cluster.Replicas) != 2 || !allCurrent(si.Cluster.Replicas):
			short = "stream not at three replicas with its followers current"
		case cerr != nil:
			short = "consumer: " + cerr.Error()
		case ci.Cluster == nil || ci.Cluster.Leader == "" || len(ci.Cluster.Replicas) != 2 || !allCurrent(ci.Cluster.Replicas):
			short = "consumer's group not settled (leader, two replicas current)"
		case ci.AckFloor.Stream != 2 || ci.NumPending != 3:
			short = fmt.Sprintf("consumer's state not restored yet (ack floor %d, %d pending)", ci.AckFloor.Stream, ci.NumPending)
		}
		if short == "" {
			t.Logf("restore-window: consumer settled after %s (stream R3, consumer group current, state restored)", time.Since(waitStart).Round(time.Millisecond))
			break
		}
		if time.Now().After(deadline) {
			t.Logf("restore-window: not settled after %s: %s", time.Since(waitStart).Round(time.Millisecond), short)
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
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
	if err := verifyOrDelete(ctx, nc, policy); err == nil || !strings.Contains(err.Error(), "deleted") || !errors.Is(err, ErrOutsidePolicy) {
		t.Fatalf("verifyOrDelete: %v, want the mismatch deleted as ErrOutsidePolicy, which the restore never retries", err)
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
	if err := RestoreVerified(ctx, nc, widened, meta.State, meta.TakenAt, opener(buf.Bytes()), Digest(buf.Bytes()), policy); !errors.Is(err, ErrOutsidePolicy) {
		t.Fatalf("a widened snapshot config: %v, want ErrOutsidePolicy", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	if sent != 0 {
		t.Errorf("%d RESTORE requests sent for a config outside the policy", sent)
	}
	if err := RestoreVerified(ctx, nc, meta.Config, meta.State, meta.TakenAt, opener(buf.Bytes()), Digest(buf.Bytes()), policy); err != nil {
		t.Fatalf("the snapshot's own config, inside the policy: %v", err)
	}
}
