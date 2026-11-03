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
	old.setEpoch(1)
	cur.setEpoch(2)
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
	if _, err := old.BeginTerm(ctx); err != nil {
		t.Fatal(err)
	}
	untouched := "slack:T1/C1/untouched"
	if err := old.Put(ctx, &SessionRecord{Key: untouched, Addressee: "platform"}); err != nil {
		t.Fatal(err)
	}
	if err := old.IndexTask(ctx, "task-old", untouched); err != nil {
		t.Fatal(err)
	}
	if got, err := cur.BeginTerm(ctx); err != nil || got != 2 {
		t.Fatalf("BeginTerm = %d, %v, want 2", got, err)
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

func TestBeginTermCountsUpFromTheMarkerWhateverTheRegistryHeld(t *testing.T) {
	// The term comes from the marker, not from the registry or the Lease, so
	// a registry that starts at 0 (a recreated Lease, a fresh process) still
	// takes a term past every record already written.
	ctx := context.Background()
	r := startRig(t)
	first, second := NewRegistry(r.client), NewRegistry(r.client)
	got, err := first.BeginTerm(ctx)
	if err != nil || got != 1 || first.Epoch() != 1 {
		t.Fatalf("first BeginTerm = %d, %v (epoch %d), want 1", got, err, first.Epoch())
	}
	key := "slack:T1/C1/terms"
	if err := first.Put(ctx, &SessionRecord{Key: key, Addressee: "first"}); err != nil {
		t.Fatal(err)
	}
	got, err = second.BeginTerm(ctx)
	if err != nil || got != 2 || second.Epoch() != 2 {
		t.Fatalf("second BeginTerm = %d, %v (epoch %d), want 2", got, err, second.Epoch())
	}
	if err := first.Put(ctx, &SessionRecord{Key: key, Addressee: "deposed"}); !errors.Is(err, ErrFenced) {
		t.Fatalf("Put from term 1 after term 2 began = %v, want ErrFenced", err)
	}
	if err := second.Put(ctx, &SessionRecord{Key: key, Addressee: "second"}); err != nil {
		t.Fatalf("Put at term 2: %v", err)
	}
	// A third leader whose own count restarted (SetEpoch(0) stands in for a
	// recreated Lease) still lands past the records.
	third := NewRegistry(r.client)
	third.setEpoch(0)
	if got, err := third.BeginTerm(ctx); err != nil || got != 3 {
		t.Fatalf("BeginTerm after a reset = %d, %v, want 3", got, err)
	}
	if err := third.Put(ctx, &SessionRecord{Key: key, Addressee: "third"}); err != nil {
		t.Fatalf("Put at term 3 over a term 2 record: %v", err)
	}
}

func TestASameEpochRaceRetriesAndWins(t *testing.T) {
	// Within one term the last write wins, as with kv.Put: losing the
	// revision race to a same-epoch write re-checks and writes again.
	ctx := context.Background()
	_, cur := fencedPair(t)
	other := NewRegistry(cur.c)
	other.setEpoch(2)
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
	other.setEpoch(2)
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
	reg.setEpoch(1) // term 0 doesn't read the marker
	err = reg.Put(ctx, &SessionRecord{Key: key, Addressee: "blocked"})
	if err == nil || !strings.Contains(err.Error(), termKey) {
		t.Fatalf("Put under an unreadable marker = %v, want an error naming %s", err, termKey)
	}
}

func TestAReplicatedStreamsConflictIsARevisionConflict(t *testing.T) {
	// Replicated streams answer a lost compare-and-swap with 10164, which
	// nats.go wraps as ErrKeyRevisionMismatch rather than the 10071 an R1
	// test server returns.
	replicated := fmt.Errorf("%w: %w", &jetstream.APIError{ErrorCode: jetstream.JSErrCodeStreamWrongLastSequenceConstant}, jetstream.ErrKeyRevisionMismatch)
	if !isRevisionConflict(replicated) {
		t.Fatal("a replicated stream's conflict isn't a revision conflict")
	}
	if isRevisionConflict(errors.New("timeout")) {
		t.Fatal("an unrelated error is a revision conflict")
	}
}

func TestTermZeroIsUnfencedSoAPreElectionBuildCanRollBack(t *testing.T) {
	// A build without the election writes at term 0. Rolled back to after
	// terms have started, its writes must still land.
	ctx := context.Background()
	r := startRig(t)
	leader, rolledBack := NewRegistry(r.client), NewRegistry(r.client)
	if _, err := leader.BeginTerm(ctx); err != nil {
		t.Fatal(err)
	}
	key := "slack:T1/C1/rollback"
	if err := leader.Put(ctx, &SessionRecord{Key: key, Addressee: "term-1"}); err != nil {
		t.Fatal(err)
	}
	if err := rolledBack.Put(ctx, &SessionRecord{Key: key, Addressee: "term-0"}); err != nil {
		t.Fatalf("Put at term 0 after terms began: %v", err)
	}
	if err := rolledBack.Create(ctx, &SessionRecord{Key: "slack:T1/C1/rollback-new"}); err != nil {
		t.Fatalf("Create at term 0 after terms began: %v", err)
	}
	if err := rolledBack.IndexTask(ctx, "task-rb", key); err != nil {
		t.Fatalf("IndexTask at term 0: %v", err)
	}
	if err := rolledBack.DropTask(ctx, "task-rb"); err != nil {
		t.Fatalf("DropTask at term 0: %v", err)
	}
	if err := rolledBack.DeleteSession(ctx, key); err != nil {
		t.Fatalf("DeleteSession at term 0: %v", err)
	}
}

// TestTermZeroIsTheUnfencedRegistry: at term 0 the registry never reads the
// counter, so even one that won't parse can't stop a write, and Put and the
// deletes are the unconditional calls they always were.
func TestTermZeroIsTheUnfencedRegistry(t *testing.T) {
	ctx := context.Background()
	r := startRig(t)
	kv, err := r.client.KV(ctx, lib.SessionStateBucket)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := kv.Put(ctx, termKey, []byte("not a term")); err != nil {
		t.Fatal(err)
	}
	reg := NewRegistry(r.client)
	key := "slack:T1/C1/term-zero"
	if err := reg.Put(ctx, &SessionRecord{Key: key, Addressee: "platform"}); err != nil {
		t.Fatalf("Put at term 0 under an unreadable counter: %v", err)
	}
	if err := reg.Create(ctx, &SessionRecord{Key: "slack:T1/C1/term-zero-new"}); err != nil {
		t.Fatalf("Create at term 0 under an unreadable counter: %v", err)
	}
	if err := reg.IndexTask(ctx, "task-tz", key); err != nil {
		t.Fatalf("IndexTask at term 0: %v", err)
	}
	if err := reg.DropTask(ctx, "task-tz"); err != nil {
		t.Fatalf("DropTask at term 0: %v", err)
	}
	if err := reg.DeleteSession(ctx, key); err != nil {
		t.Fatalf("DeleteSession at term 0: %v", err)
	}
}

// TestACreateOverADeletedKeyLosingARaceIsARevisionConflict: Create over a
// deleted key returns the wrong-last-sequence error unwrapped, which a
// fenced Put must still read as a lost race and retry.
func TestACreateOverADeletedKeyLosingARaceIsARevisionConflict(t *testing.T) {
	for _, code := range []jetstream.ErrorCode{jetstream.JSErrCodeStreamWrongLastSequence, jetstream.JSErrCodeStreamWrongLastSequenceConstant} {
		if !isRevisionConflict(&jetstream.APIError{ErrorCode: code}) {
			t.Errorf("an unwrapped %d isn't a revision conflict", code)
		}
	}
}

// TestBeginTermRefusesToWrapPastTheMaximum: a counter at MaxUint64 would wrap
// to term 0, the unfenced path; BeginTerm refuses and names the key instead.
func TestBeginTermRefusesToWrapPastTheMaximum(t *testing.T) {
	ctx := context.Background()
	r := startRig(t)
	kv, err := r.client.KV(ctx, lib.SessionStateBucket)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := kv.Put(ctx, termKey, []byte("18446744073709551615")); err != nil {
		t.Fatal(err)
	}
	reg := NewRegistry(r.client)
	if got, err := reg.BeginTerm(ctx); err == nil || !strings.Contains(err.Error(), termKey) || got != 0 || reg.Epoch() != 0 {
		t.Fatalf("BeginTerm at the maximum = %d, %v (epoch %d), want a refusal naming %s", got, err, reg.Epoch(), termKey)
	}
	if term, _, _ := readTerm(ctx, kv); term == 0 {
		t.Fatal("the counter wrapped to 0")
	}
}

// TestADeletedCounterIsRefusedNotRestartedAtOne: a deleted counter isn't read
// as never raised, which would restart terms beneath the records.
func TestADeletedCounterIsRefusedNotRestartedAtOne(t *testing.T) {
	ctx := context.Background()
	r := startRig(t)
	reg := NewRegistry(r.client)
	if _, err := reg.BeginTerm(ctx); err != nil {
		t.Fatal(err)
	}
	kv, err := r.client.KV(ctx, lib.SessionStateBucket)
	if err != nil {
		t.Fatal(err)
	}
	if err := kv.Delete(ctx, termKey); err != nil {
		t.Fatal(err)
	}
	next := NewRegistry(r.client)
	if got, err := next.BeginTerm(ctx); err == nil || !strings.Contains(err.Error(), "was deleted") {
		t.Fatalf("BeginTerm over a deleted counter = %d, %v, want a refusal", got, err)
	}
}

// TestARecordAboveTheCounterSaysTheCounterWasReset: a write refused because a
// record's term is above the counter names the counter, not just the session.
func TestARecordAboveTheCounterSaysTheCounterWasReset(t *testing.T) {
	ctx := context.Background()
	r := startRig(t)
	high := NewRegistry(r.client)
	high.setEpoch(3)
	key := "slack:T1/C1/reset"
	if err := high.Put(ctx, &SessionRecord{Key: key, Addressee: "platform"}); err != nil {
		t.Fatal(err)
	}
	kv, err := r.client.KV(ctx, lib.SessionStateBucket)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := kv.Put(ctx, termKey, []byte("1")); err != nil {
		t.Fatal(err)
	}
	low := NewRegistry(r.client)
	low.setEpoch(1)
	stepped := false
	low.SetOnFenced(func() { stepped = true })
	err = low.Put(ctx, &SessionRecord{Key: key, Addressee: "after-reset"})
	if !errors.Is(err, ErrFenced) || !strings.Contains(err.Error(), termKey) || !strings.Contains(err.Error(), "reset") {
		t.Fatalf("Put under a reset counter = %v, want ErrFenced naming %s and the reset", err, termKey)
	}
	// No newer term exists, so the gateway doesn't step down over it: the
	// next replica would be refused the same way.
	if stepped {
		t.Fatal("a reset counter stepped the gateway down")
	}
}

// historyFails is a bucket whose counter Get says missing and whose History
// read fails, as a watch can during a bus leader election.
type historyFails struct {
	jetstream.KeyValue
}

func (historyFails) Get(context.Context, string) (jetstream.KeyValueEntry, error) {
	return nil, jetstream.ErrKeyNotFound
}

func (historyFails) History(context.Context, string, ...jetstream.WatchOpt) ([]jetstream.KeyValueEntry, error) {
	return nil, errors.New("no responders available for request")
}

// TestAnUnreadableCounterHistoryIsAnErrorNotTermOne: only "no history" means
// the counter was never raised; a history read that fails can't tell a
// deleted counter from a fresh one.
func TestAnUnreadableCounterHistoryIsAnErrorNotTermOne(t *testing.T) {
	_, _, err := readTerm(context.Background(), historyFails{})
	if err == nil || !strings.Contains(err.Error(), termKey) {
		t.Fatalf("readTerm with an unreadable history = %v, want an error naming %s", err, termKey)
	}
}

// TestANewTermBetweenTheReadsIsNotCalledAReset: a record above the counter
// read a moment earlier is a new term that began in between, not a reset.
func TestANewTermBetweenTheReadsIsNotCalledAReset(t *testing.T) {
	ctx := context.Background()
	r := startRig(t)
	old, cur := NewRegistry(r.client), NewRegistry(r.client)
	if _, err := old.BeginTerm(ctx); err != nil {
		t.Fatal(err)
	}
	key := "slack:T1/C1/term-race"
	if err := old.Put(ctx, &SessionRecord{Key: key, Addressee: "platform"}); err != nil {
		t.Fatal(err)
	}
	// The new term begins and writes the record between the deposed
	// gateway's counter read and its record read.
	old.afterTermRead = func() {
		old.afterTermRead = nil
		if _, err := cur.BeginTerm(ctx); err != nil {
			t.Errorf("BeginTerm: %v", err)
		}
		if err := cur.Put(ctx, &SessionRecord{Key: key, Addressee: "new-leader"}); err != nil {
			t.Errorf("new leader's Put: %v", err)
		}
	}
	stepped := false
	old.SetOnFenced(func() { stepped = true })
	err := old.Put(ctx, &SessionRecord{Key: key, Addressee: "deposed"})
	if !errors.Is(err, ErrFenced) || strings.Contains(err.Error(), "reset") {
		t.Fatalf("deposed Put across a new term = %v, want ErrFenced without a reset", err)
	}
	if !stepped {
		t.Fatal("a write fenced by a newer term didn't step the gateway down")
	}
}

// TestACounterPastTheWritersTermNamesTheCounter: a write refused at the
// counter check names gateway-term and the value read, so a counter bumped
// by hand points at the key rather than at a phantom second gateway.
func TestACounterPastTheWritersTermNamesTheCounter(t *testing.T) {
	ctx := context.Background()
	r := startRig(t)
	kv, err := r.client.KV(ctx, lib.SessionStateBucket)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := kv.Put(ctx, termKey, []byte("9")); err != nil {
		t.Fatal(err)
	}
	reg := NewRegistry(r.client)
	reg.setEpoch(2)
	for name, err := range map[string]error{
		"Put":    reg.Put(ctx, &SessionRecord{Key: "slack:T1/C1/bumped", Addressee: "platform"}),
		"Create": reg.Create(ctx, &SessionRecord{Key: "slack:T1/C1/bumped-new"}),
	} {
		if !errors.Is(err, ErrFenced) || !strings.Contains(err.Error(), termKey) || !strings.Contains(err.Error(), "9") {
			t.Errorf("%s under a counter past its term = %v, want ErrFenced naming %s and 9", name, err, termKey)
		}
	}
}
