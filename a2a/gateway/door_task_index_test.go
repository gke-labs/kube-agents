package gateway

import (
	"context"
	"fmt"
	"slices"
	"sync"
	"testing"
	"time"
)

// doorRecord is a door conversation's record listing taskIDs, none active.
func doorRecord(key string, taskIDs ...string) *SessionRecord {
	rec := &SessionRecord{Key: key, ContextID: "ctx-doorindex", Addressee: "platform", Kind: a2aConversationKind, LastActivity: time.Now().UTC()}
	for _, id := range taskIDs {
		rec.Tasks = append(rec.Tasks, TaskRef{ID: id, Addressee: "platform"})
	}
	return rec
}

func doorKeyExists(t *testing.T, g *Gateway, taskID string) bool {
	t.Helper()
	kv, err := g.reg.kv(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	_, err = kv.Get(context.Background(), doorTaskKey(taskID))
	return err == nil
}

// The door index leads from a task id to the record that lists it.
func TestLocateDoorTaskFollowsTheDoorIndexToTheRecord(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	key := a2aConversationKey(a2aTestCaller, "ctx-locate")
	if err := r.g.reg.Put(ctx, doorRecord(key, "task-locate-1")); err != nil {
		t.Fatal(err)
	}
	if err := r.g.reg.IndexDoorTask(ctx, "task-locate-1", key); err != nil {
		t.Fatal(err)
	}
	if got, err := r.g.reg.LocateDoorTask(ctx, "task-locate-1"); err != nil || got != key {
		t.Fatalf("LocateDoorTask = %q, %v; want %q", got, err, key)
	}
}

// A task an older gateway started has no door entry, only its route; while
// it runs the route still leads to its record.
func TestLocateDoorTaskFallsBackToTheRouteIndex(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	key := a2aConversationKey(a2aTestCaller, "ctx-fallback")
	if err := r.g.reg.Put(ctx, doorRecord(key, "task-fallback-1")); err != nil {
		t.Fatal(err)
	}
	if err := r.g.reg.IndexTask(ctx, "task-fallback-1", key); err != nil {
		t.Fatal(err)
	}
	if got, err := r.g.reg.LocateDoorTask(ctx, "task-fallback-1"); err != nil || got != key {
		t.Fatalf("LocateDoorTask = %q, %v; want %q", got, err, key)
	}
}

// A door entry whose record no longer lists the task (or is gone) is a miss,
// and the read deletes it.
func TestAnOrphanDoorEntryIsAMissAndIsDeletedOnRead(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	key := a2aConversationKey(a2aTestCaller, "ctx-orphan")
	if err := r.g.reg.Put(ctx, doorRecord(key, "task-orphan-kept")); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"task-orphan-gone", "task-orphan-kept"} {
		if err := r.g.reg.IndexDoorTask(ctx, id, key); err != nil {
			t.Fatal(err)
		}
	}
	if err := r.g.reg.IndexDoorTask(ctx, "task-orphan-norecord", a2aConversationKey(a2aTestCaller, "ctx-never")); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"task-orphan-gone", "task-orphan-norecord"} {
		if got, err := r.g.reg.LocateDoorTask(ctx, id); err != nil || got != "" {
			t.Errorf("%s: LocateDoorTask = %q, %v; want a miss", id, got, err)
		}
		if doorKeyExists(t, r.g, id) {
			t.Errorf("%s: the orphan entry survived the read", id)
		}
	}
	if !doorKeyExists(t, r.g, "task-orphan-kept") {
		t.Error("the listed task's entry went with the orphans")
	}
}

// trimHistory says which tasks it dropped, so their door entries go too.
func TestTrimHistoryNamesTheTasksItDropped(t *testing.T) {
	t.Parallel()
	var ids []string
	for i := range taskHistoryCap + 2 {
		ids = append(ids, fmt.Sprintf("task-trim-%02d", i))
	}
	rec := doorRecord("a2a:x:ctx", ids...)
	if dropped := rec.trimHistory(); !slices.Equal(dropped, ids[:2]) {
		t.Errorf("dropped = %q, want %q", dropped, ids[:2])
	}
	if len(rec.Tasks) != taskHistoryCap {
		t.Errorf("kept %d, want %d", len(rec.Tasks), taskHistoryCap)
	}
}

// A door task pushed out of its record's history by a new task loses its
// door entry in the same turn.
func TestADoorTaskTrimmedFromItsHistoryLosesItsEntry(t *testing.T) {
	t.Parallel()
	r := startA2ADoorRig(t)
	ctx := context.Background()
	const contextID = "ctx-trimmed00000001"
	key := a2aConversationKey(a2aTestCaller, contextID)
	var ids []string
	for i := range taskHistoryCap {
		ids = append(ids, fmt.Sprintf("task-trimmed%08d", i))
	}
	rec := doorRecord(key, ids...)
	rec.ContextID = contextID
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	for _, id := range ids {
		if err := r.g.reg.IndexDoorTask(ctx, id, key); err != nil {
			t.Fatal(err)
		}
	}
	taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("one more", "m-trim", contextID, false)))
	waitFor(t, "the oldest task's door entry to go", func() bool { return !doorKeyExists(t, r.g, ids[0]) })
	if !doorKeyExists(t, r.g, ids[1]) {
		t.Error("a task still in the history lost its entry")
	}
}

// The reap that deletes a door session drops its tasks' door entries.
func TestReapingADoorSessionDropsItsDoorEntries(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	r.g.cfg.SessionTTL = 24 * time.Hour
	ctx := context.Background()
	key := a2aConversationKey(a2aTestCaller, "ctx-reaped")
	rec := doorRecord(key, "task-reaped-1", "task-reaped-2")
	rec.LastActivity = time.Now().UTC().Add(-48 * time.Hour)
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"task-reaped-1", "task-reaped-2"} {
		if err := r.g.reg.IndexDoorTask(ctx, id, key); err != nil {
			t.Fatal(err)
		}
	}
	r.g.reapOnce(ctx)
	if got, _ := r.g.reg.Get(ctx, key); got != nil {
		t.Fatalf("the expired door session was kept: %+v", got)
	}
	for _, id := range []string{"task-reaped-1", "task-reaped-2"} {
		if doorKeyExists(t, r.g, id) {
			t.Errorf("%s: door entry kept after its session was reaped", id)
		}
	}
}

// The term-start sweep drops entries whose record doesn't list their task
// (left by a rollback to a gateway that never drops them) and keeps the rest.
func TestTheDoorIndexSweepDropsOrphansAndKeepsListedTasks(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	key := a2aConversationKey(a2aTestCaller, "ctx-sweep")
	if err := r.g.reg.Put(ctx, doorRecord(key, "task-sweep-kept")); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"task-sweep-kept", "task-sweep-orphan"} {
		if err := r.g.reg.IndexDoorTask(ctx, id, key); err != nil {
			t.Fatal(err)
		}
	}
	kept, dropped, err := r.g.reg.SweepDoorTasks(ctx)
	if err != nil || kept != 1 || dropped != 1 {
		t.Fatalf("SweepDoorTasks = %d kept, %d dropped, %v; want 1, 1", kept, dropped, err)
	}
	if !doorKeyExists(t, r.g, "task-sweep-kept") || doorKeyExists(t, r.g, "task-sweep-orphan") {
		t.Error("the sweep kept the wrong entry")
	}
}

// A delegating turn is one task to the door: its children and the wakes
// that continue it are listed on the same record, and never answered for.
func TestLocateDoorTaskAnswersOnlyForAChainsRoot(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	key := a2aConversationKey(a2aTestCaller, "ctx-chain")
	rec := doorRecord(key, "task-chain-root")
	rec.Tasks = append(rec.Tasks,
		TaskRef{ID: "task-chain-child", Addressee: "platform", ParentTaskID: "task-chain-root", RootTaskID: "task-chain-root"},
		TaskRef{ID: "task-chain-wake", Addressee: "platform", RootTaskID: "task-chain-root"})
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"task-chain-root", "task-chain-child", "task-chain-wake"} {
		if err := r.g.reg.IndexTask(ctx, id, key); err != nil {
			t.Fatal(err)
		}
	}
	if got, err := r.g.reg.LocateDoorTask(ctx, "task-chain-root"); err != nil || got != key {
		t.Errorf("root: LocateDoorTask = %q, %v; want %q", got, err, key)
	}
	for _, id := range []string{"task-chain-child", "task-chain-wake"} {
		if got, err := r.g.reg.LocateDoorTask(ctx, id); err != nil || got != "" {
			t.Errorf("%s: LocateDoorTask = %q, %v; want a miss", id, got, err)
		}
	}
}

// The door entry is written after the record lists the task, never before:
// a term-start sweep reading between the two would take a fresh entry for
// an orphan and drop it.
func TestADoorTaskEntryIsWrittenOnlyOnceItsRecordListsIt(t *testing.T) {
	t.Parallel()
	r := startA2ADoorRig(t)
	var mu sync.Mutex
	var early []string
	// Runs on the gateway's goroutine, so it touches nothing of t.
	r.g.reg.beforePut = func(rec *SessionRecord) error {
		if !isDoorSession(rec.Key) {
			return nil
		}
		ctx := context.Background()
		kv, err := r.g.reg.kv(ctx)
		if err != nil {
			return nil
		}
		stored, _ := r.g.reg.Get(ctx, rec.Key)
		for _, ref := range rec.Tasks {
			if _, err := kv.Get(ctx, doorTaskKey(ref.ID)); err == nil && (stored == nil || !stored.listsTask(ref.ID)) {
				mu.Lock()
				early = append(early, ref.ID)
				mu.Unlock()
			}
		}
		return nil
	}
	task := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("ordered", "m-order", "", false)))
	waitFor(t, "the door entry", func() bool { return doorKeyExists(t, r.g, task.ID) })
	mu.Lock()
	defer mu.Unlock()
	if len(early) != 0 {
		t.Errorf("door entries written before their record listed them: %q", early)
	}
}
