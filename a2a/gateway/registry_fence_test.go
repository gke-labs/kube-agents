package gateway

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// fencedPair is two gateways' registries over one bucket: a deposed leader
// at epoch 1 and the leader that replaced it at epoch 2.
func fencedPair(t *testing.T) (old, cur *Registry) {
	t.Helper()
	r := startRig(t)
	old, cur = NewRegistry(r.client), NewRegistry(r.client)
	old.SetEpoch(1)
	cur.SetEpoch(2)
	return old, cur
}

func TestPutRefusesAWriteFromAnOlderEpoch(t *testing.T) {
	ctx := context.Background()
	old, cur := fencedPair(t)
	key := "slack:T1/C1/fence"
	if err := old.Put(ctx, &SessionRecord{Key: key, Addressee: "platform"}); err != nil {
		t.Fatalf("first write at epoch 1: %v", err)
	}
	if err := cur.Put(ctx, &SessionRecord{Key: key, Addressee: "new-leader"}); err != nil {
		t.Fatalf("epoch 2 over epoch 1: %v", err)
	}
	err := old.Put(ctx, &SessionRecord{Key: key, Addressee: "deposed"})
	if !errors.Is(err, ErrFenced) {
		t.Fatalf("epoch 1 over epoch 2 = %v, want ErrFenced", err)
	}
	rec, err := cur.Get(ctx, key)
	if err != nil || rec == nil {
		t.Fatalf("Get: %v", err)
	}
	if rec.Addressee != "new-leader" || rec.Epoch != 2 {
		t.Fatalf("record = %q at epoch %d, want the epoch 2 write to stand", rec.Addressee, rec.Epoch)
	}
	// The same epoch writes again: last write wins within a term.
	if err := cur.Put(ctx, &SessionRecord{Key: key, Addressee: "again"}); err != nil {
		t.Fatalf("epoch 2 over epoch 2: %v", err)
	}
}

func TestANewerWriteBetweenCheckAndWriteFencesTheOlder(t *testing.T) {
	// The deposed leader passes its epoch check, then the new leader writes
	// before the deposed one does: the conditional write loses, the re-check
	// sees epoch 2, and the deposed write is refused.
	ctx := context.Background()
	old, cur := fencedPair(t)
	key := "slack:T1/C1/fence-race"
	if err := old.Put(ctx, &SessionRecord{Key: key, Addressee: "platform"}); err != nil {
		t.Fatal(err)
	}
	once := false
	old.afterCheck = func() {
		if once {
			return
		}
		once = true
		if err := cur.Put(ctx, &SessionRecord{Key: key, Addressee: "new-leader"}); err != nil {
			t.Errorf("epoch 2 write in the gap: %v", err)
		}
	}
	if err := old.Put(ctx, &SessionRecord{Key: key, Addressee: "deposed"}); !errors.Is(err, ErrFenced) {
		t.Fatalf("deposed write after a newer one landed = %v, want ErrFenced", err)
	}
	if rec, _ := cur.Get(ctx, key); rec == nil || rec.Addressee != "new-leader" {
		t.Fatalf("record = %+v, want the epoch 2 write to stand", rec)
	}
}

func TestARecordWithNoEpochIsWritableAtEpochZero(t *testing.T) {
	// Every record written before the field existed, and every write until
	// the election exists, is epoch 0.
	ctx := context.Background()
	r := startRig(t)
	kv, err := r.client.KV(ctx, lib.SessionStateBucket)
	if err != nil {
		t.Fatal(err)
	}
	key := "discord:g/legacy"
	if _, err := kv.Put(ctx, kvKey(key), []byte(`{"key":"discord:g/legacy","contextId":"c","addressee":"platform","kind":"group"}`)); err != nil {
		t.Fatal(err)
	}
	reg := NewRegistry(r.client)
	if err := reg.Put(ctx, &SessionRecord{Key: key, ContextID: "c", Addressee: "platform"}); err != nil {
		t.Fatalf("epoch 0 over a legacy record: %v", err)
	}
	if err := reg.IndexTask(ctx, "task-legacy", key); err != nil {
		t.Fatalf("IndexTask: %v", err)
	}
	if err := reg.DeleteSession(ctx, key); err != nil {
		t.Fatalf("DeleteSession: %v", err)
	}
}

func TestTheTaskIndexAndDeletesAreFencedToo(t *testing.T) {
	ctx := context.Background()
	old, cur := fencedPair(t)
	key := "slack:T1/C1/fence-index"
	if err := cur.Put(ctx, &SessionRecord{Key: key, Addressee: "platform"}); err != nil {
		t.Fatal(err)
	}
	if err := cur.IndexTask(ctx, "task-a", key); err != nil {
		t.Fatalf("IndexTask at the record's epoch: %v", err)
	}
	if err := old.IndexTask(ctx, "task-b", key); !errors.Is(err, ErrFenced) {
		t.Fatalf("IndexTask from an older epoch = %v, want ErrFenced", err)
	}
	if got, _ := cur.SessionForTask(ctx, "task-b"); got != "" {
		t.Fatalf("a refused index write landed: %q", got)
	}
	if err := old.DropTask(ctx, "task-a"); !errors.Is(err, ErrFenced) {
		t.Fatalf("DropTask from an older epoch = %v, want ErrFenced", err)
	}
	if got, _ := cur.SessionForTask(ctx, "task-a"); got != key {
		t.Fatalf("a refused drop removed the index: %q", got)
	}
	if err := old.DeleteSession(ctx, key); !errors.Is(err, ErrFenced) {
		t.Fatalf("DeleteSession from an older epoch = %v, want ErrFenced", err)
	}
	if rec, _ := cur.Get(ctx, key); rec == nil {
		t.Fatal("a refused delete removed the record")
	}
	if err := cur.DropTask(ctx, "task-a"); err != nil {
		t.Fatalf("DropTask at the record's epoch: %v", err)
	}
	if err := cur.DeleteSession(ctx, key); err != nil {
		t.Fatalf("DeleteSession at the record's epoch: %v", err)
	}
	if rec, _ := cur.Get(ctx, key); rec != nil {
		t.Fatal("record survived its delete")
	}
	// A deleted record is recreated at the writer's epoch.
	if err := old.Put(ctx, &SessionRecord{Key: key, Addressee: "after-delete"}); err != nil {
		t.Fatalf("Put over a tombstone: %v", err)
	}
}

func TestAStaleRevisionIsARevisionConflict(t *testing.T) {
	ctx := context.Background()
	r := startRig(t)
	kv, err := r.client.KV(ctx, lib.SessionStateBucket)
	if err != nil {
		t.Fatal(err)
	}
	rev, err := kv.Put(ctx, "sessions.race", []byte(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := kv.Put(ctx, "sessions.race", []byte(`{}`)); err != nil {
		t.Fatal(err)
	}
	_, err = kv.Update(ctx, "sessions.race", []byte(`{}`), rev)
	if err == nil || !isRevisionConflict(err) {
		t.Fatalf("Update at a stale revision = %v, want a revision conflict", err)
	}
	_, err = kv.Create(ctx, "sessions.race", []byte(`{}`))
	if err == nil || !isRevisionConflict(err) {
		t.Fatalf("Create over a live key = %v, want a revision conflict", err)
	}
}

func TestWithRetryStopsOnAFencedWrite(t *testing.T) {
	calls := 0
	err := withRetry(kvRetryAttempts, func() error {
		calls++
		return ErrFenced
	})
	if !errors.Is(err, ErrFenced) || calls != 1 {
		t.Fatalf("withRetry = %v after %d calls, want ErrFenced after 1", err, calls)
	}
}

func TestTheTermMarkerFencesRecordsTheNewLeaderHasNotWritten(t *testing.T) {
	ctx := context.Background()
	old, cur := fencedPair(t)
	untouched := "slack:T1/C1/untouched"
	if err := old.Put(ctx, &SessionRecord{Key: untouched, Addressee: "platform"}); err != nil {
		t.Fatal(err)
	}
	if err := old.IndexTask(ctx, "task-old", untouched); err != nil {
		t.Fatal(err)
	}
	if err := cur.RaiseTerm(ctx); err != nil {
		t.Fatalf("RaiseTerm at epoch 2: %v", err)
	}
	// The new leader hasn't written this record, and the deposed one is
	// refused on it anyway.
	if err := old.Put(ctx, &SessionRecord{Key: untouched, Addressee: "deposed"}); !errors.Is(err, ErrFenced) {
		t.Fatalf("Put after the term moved = %v, want ErrFenced", err)
	}
	if err := old.Create(ctx, &SessionRecord{Key: "slack:T1/C1/brand-new"}); !errors.Is(err, ErrFenced) {
		t.Fatalf("Create after the term moved = %v, want ErrFenced", err)
	}
	if err := old.IndexTask(ctx, "task-late", untouched); !errors.Is(err, ErrFenced) {
		t.Fatalf("IndexTask after the term moved = %v, want ErrFenced", err)
	}
	if err := old.DropTask(ctx, "task-old"); !errors.Is(err, ErrFenced) {
		t.Fatalf("DropTask after the term moved = %v, want ErrFenced", err)
	}
	if err := old.DeleteSession(ctx, untouched); !errors.Is(err, ErrFenced) {
		t.Fatalf("DeleteSession after the term moved = %v, want ErrFenced", err)
	}
	if rec, _ := cur.Get(ctx, untouched); rec == nil || rec.Addressee != "platform" {
		t.Fatalf("record = %+v, want the deposed writes refused", rec)
	}
	// The current term writes it as usual.
	if err := cur.Put(ctx, &SessionRecord{Key: untouched, Addressee: "new-leader"}); err != nil {
		t.Fatalf("Put at the current term: %v", err)
	}
}

func TestRaiseTermNeverLowersTheMarker(t *testing.T) {
	ctx := context.Background()
	old, cur := fencedPair(t)
	if err := old.RaiseTerm(ctx); err != nil {
		t.Fatalf("RaiseTerm at epoch 1: %v", err)
	}
	if err := cur.RaiseTerm(ctx); err != nil {
		t.Fatalf("RaiseTerm at epoch 2 over 1: %v", err)
	}
	if err := cur.RaiseTerm(ctx); err != nil {
		t.Fatalf("RaiseTerm at its own epoch again: %v", err)
	}
	if err := old.RaiseTerm(ctx); !errors.Is(err, ErrFenced) {
		t.Fatalf("RaiseTerm at epoch 1 over 2 = %v, want ErrFenced", err)
	}
	kv, err := cur.kv(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if term, _, err := readTerm(ctx, kv); err != nil || term != 2 {
		t.Fatalf("marker = %d, %v, want 2", term, err)
	}
}

func TestASameEpochRaceRetriesAndWins(t *testing.T) {
	// Within one term the last write wins, as with kv.Put: losing the
	// revision race to a same-epoch write re-checks and writes again.
	ctx := context.Background()
	_, cur := fencedPair(t)
	other := NewRegistry(cur.c)
	other.SetEpoch(2)
	key := "slack:T1/C1/same-term"
	if err := cur.Put(ctx, &SessionRecord{Key: key, Addressee: "first"}); err != nil {
		t.Fatal(err)
	}
	raced := false
	cur.afterCheck = func() {
		if !raced {
			raced = true
			if err := other.Put(ctx, &SessionRecord{Key: key, Addressee: "racer"}); err != nil {
				t.Errorf("racing write: %v", err)
			}
		}
	}
	if err := cur.Put(ctx, &SessionRecord{Key: key, Addressee: "last"}); err != nil {
		t.Fatalf("Put after losing a same-epoch race = %v, want it to retry and win", err)
	}
	if rec, _ := cur.Get(ctx, key); rec == nil || rec.Addressee != "last" {
		t.Fatalf("record = %+v, want the last write", rec)
	}
}

func TestDeletesAreConditionalOnTheCheckedRevision(t *testing.T) {
	// A write landing between a delete's check and the delete keeps the
	// record: the delete was for the revision it checked.
	ctx := context.Background()
	_, cur := fencedPair(t)
	other := NewRegistry(cur.c)
	other.SetEpoch(2)
	key := "slack:T1/C1/delete-race"
	if err := cur.Put(ctx, &SessionRecord{Key: key, Addressee: "platform"}); err != nil {
		t.Fatal(err)
	}
	if err := cur.IndexTask(ctx, "task-race", key); err != nil {
		t.Fatal(err)
	}
	cur.afterCheck = func() {
		if err := other.Put(ctx, &SessionRecord{Key: key, Addressee: "rewritten"}); err != nil {
			t.Errorf("racing write: %v", err)
		}
		if err := other.IndexTask(ctx, "task-race", key); err != nil {
			t.Errorf("racing index: %v", err)
		}
	}
	if err := cur.DeleteSession(ctx, key); err == nil {
		t.Fatal("DeleteSession deleted a record rewritten after its check")
	}
	if rec, _ := cur.Get(ctx, key); rec == nil || rec.Addressee != "rewritten" {
		t.Fatalf("record = %+v, want the rewrite to survive", rec)
	}
	if err := cur.DropTask(ctx, "task-race"); err == nil {
		t.Fatal("DropTask dropped an index entry rewritten after its check")
	}
	if got, _ := cur.SessionForTask(ctx, "task-race"); got != key {
		t.Fatalf("index = %q, want the rewrite to survive", got)
	}
}

func TestAnUnreadableMarkerNamesItselfAndARecordThatDoesNotParseIsReplaced(t *testing.T) {
	ctx := context.Background()
	r := startRig(t)
	reg := NewRegistry(r.client)
	kv, err := r.client.KV(ctx, lib.SessionStateBucket)
	if err != nil {
		t.Fatal(err)
	}
	key := "discord:g/corrupt"
	if _, err := kv.Put(ctx, kvKey(key), []byte("not json")); err != nil {
		t.Fatal(err)
	}
	if err := reg.Put(ctx, &SessionRecord{Key: key, Addressee: "repaired"}); err != nil {
		t.Fatalf("Put over a record that doesn't parse = %v, want it replaced", err)
	}
	if rec, _ := reg.Get(ctx, key); rec == nil || rec.Addressee != "repaired" {
		t.Fatalf("record = %+v, want the repair", rec)
	}
	if _, err := kv.Put(ctx, termKey, []byte("x")); err != nil {
		t.Fatal(err)
	}
	err = reg.Put(ctx, &SessionRecord{Key: key, Addressee: "blocked"})
	if err == nil || !strings.Contains(err.Error(), termKey) {
		t.Fatalf("Put under an unreadable marker = %v, want an error naming %s", err, termKey)
	}
}

func TestAReplicatedStreamsConflictIsARevisionConflict(t *testing.T) {
	// Replicated streams answer a lost compare-and-swap with 10164, which
	// nats.go wraps as ErrKeyRevisionMismatch rather than the 10071 an R1
	// test server returns.
	replicated := fmt.Errorf("%w: %w", &jetstream.APIError{ErrorCode: 10164}, jetstream.ErrKeyRevisionMismatch)
	if !isRevisionConflict(replicated) {
		t.Fatal("a replicated stream's conflict isn't a revision conflict")
	}
	if isRevisionConflict(errors.New("timeout")) {
		t.Fatal("an unrelated error is a revision conflict")
	}
}
