package gateway

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// TestSessionRecordDeletedAfterSessionTTL verifies Defect 1:
// An idle session record whose last activity is older than SessionTTL,
// with no running pod and no active task, is pruned from the session-state
// bucket during the reap pass.
func TestSessionRecordDeletedAfterSessionTTL(t *testing.T) {
	r := startRig(t)
	r.g.cfg.SessionTTL = 24 * time.Hour
	conv := "discord:g1/expired-session"

	rec := &SessionRecord{
		Key:          conv,
		ContextID:    "ctx-expired",
		Kind:         "group",
		Addressee:    "platform",
		LastActivity: time.Now().UTC().Add(-48 * time.Hour), // 2 days ago > 24h
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}

	// Verify record exists before reap
	got, err := r.g.reg.Get(context.Background(), conv)
	if err != nil || got == nil {
		t.Fatalf("session record missing before reap: %v", err)
	}

	r.g.reapOnce(context.Background())

	// Verify record was deleted by the reaper
	got, err = r.g.reg.Get(context.Background(), conv)
	if err != nil {
		t.Fatalf("registry Get returned error: %v", err)
	}
	if got != nil {
		t.Fatalf("session record was not pruned after SessionTTL: %+v", got)
	}
}

// TestSessionRecordRetainedBeforeSessionTTL verifies Defect 1:
// A session whose last activity is within SessionTTL retains its record
// (holding contextId across pod incarnations).
func TestSessionRecordRetainedBeforeSessionTTL(t *testing.T) {
	r := startRig(t)
	r.g.cfg.SessionTTL = 24 * time.Hour
	conv := "discord:g1/recent-session"

	rec := &SessionRecord{
		Key:          conv,
		ContextID:    "ctx-recent",
		Kind:         "group",
		Addressee:    "platform",
		LastActivity: time.Now().UTC().Add(-2 * time.Hour), // 2h ago < 24h
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}

	r.g.reapOnce(context.Background())

	got, err := r.g.reg.Get(context.Background(), conv)
	if err != nil {
		t.Fatalf("registry Get error: %v", err)
	}
	if got == nil {
		t.Fatal("session record was incorrectly deleted before SessionTTL")
	}
	if got.ContextID != "ctx-recent" {
		t.Fatalf("got contextId = %s, want ctx-recent", got.ContextID)
	}
}

// TestSessionRecordRetainedWhilePodOrTaskActive verifies Defect 1:
// Even if last activity is old, a session with an active pod or active task
// is not deleted out from under running work.
func TestSessionRecordRetainedWhilePodOrTaskActive(t *testing.T) {
	r := startRig(t)
	r.g.cfg.SessionTTL = 24 * time.Hour

	// Session with active pod
	convPod := "discord:g1/old-with-pod"
	recPod := &SessionRecord{
		Key:          convPod,
		ContextID:    "ctx-old-pod",
		PodName:      "chat-pod-1",
		LastActivity: time.Now().UTC().Add(-48 * time.Hour),
	}
	if err := r.g.reg.Put(context.Background(), recPod); err != nil {
		t.Fatal(err)
	}

	// Session with active task
	convTask := "discord:g1/old-with-task"
	recTask := &SessionRecord{
		Key:          convTask,
		ContextID:    "ctx-old-task",
		ActiveTask:   &ActiveTask{TaskID: "task-live-1"},
		LastActivity: time.Now().UTC().Add(-48 * time.Hour),
	}
	if err := r.g.reg.Put(context.Background(), recTask); err != nil {
		t.Fatal(err)
	}

	r.g.reapOnce(context.Background())

	gotPod, err := r.g.reg.Get(context.Background(), convPod)
	if err != nil || gotPod == nil {
		t.Fatalf("session with pod was deleted: %v", err)
	}

	gotTask, err := r.g.reg.Get(context.Background(), convTask)
	if err != nil || gotTask == nil {
		t.Fatalf("session with task was deleted: %v", err)
	}
}

// TestScanSessionsResumableCursor verifies Defect 2:
// ScanSessions streams records via a callback and supports pausing and
// resuming across a cursor without rescanning from the beginning.
func TestScanSessionsResumableCursor(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()

	// Seed 6 sessions
	for i := 1; i <= 6; i++ {
		key := fmt.Sprintf("discord:g1/cursor-test-%02d", i)
		rec := &SessionRecord{
			Key:          key,
			ContextID:    fmt.Sprintf("ctx-%02d", i),
			LastActivity: time.Now().UTC(),
		}
		if err := r.g.reg.Put(ctx, rec); err != nil {
			t.Fatal(err)
		}
	}

	// First pass: halt after 3 records
	var firstBatch []string
	nextCursor, done, err := r.g.reg.ScanSessions(ctx, "", func(rec *SessionRecord) (bool, error) {
		firstBatch = append(firstBatch, rec.Key)
		return len(firstBatch) < 3, nil
	})
	if err != nil {
		t.Fatalf("first scan error: %v", err)
	}
	if done {
		t.Fatal("first scan reported done, expected incomplete")
	}
	if len(firstBatch) != 3 {
		t.Fatalf("first batch processed %d records, want 3", len(firstBatch))
	}
	if nextCursor == "" {
		t.Fatal("first scan returned empty nextCursor")
	}

	// Second pass: resume from nextCursor
	var secondBatch []string
	finalCursor, done, err := r.g.reg.ScanSessions(ctx, nextCursor, func(rec *SessionRecord) (bool, error) {
		secondBatch = append(secondBatch, rec.Key)
		return true, nil
	})
	if err != nil {
		t.Fatalf("second scan error: %v", err)
	}
	if !done {
		t.Fatal("second scan reported not done, expected done")
	}
	if finalCursor != "" {
		t.Fatalf("expected empty cursor on completion, got %q", finalCursor)
	}
	if len(secondBatch) != 3 {
		t.Fatalf("second batch processed %d records, want 3", len(secondBatch))
	}

	// Ensure no duplicate keys between batches
	seen := make(map[string]bool)
	for _, k := range firstBatch {
		seen[k] = true
	}
	for _, k := range secondBatch {
		if seen[k] {
			t.Fatalf("key %q was processed in both first and second batch", k)
		}
	}
}

// TestSessionLocksPrunedWhenIdle verifies Defect 3:
// sessionLocks map entries are refcounted and deleted once locks are unlocked,
// preventing unbounded memory growth.
func TestSessionLocksPrunedWhenIdle(t *testing.T) {
	r := startRig(t)

	conv := "discord:g1/lock-prune-test"

	r.g.mu.Lock()
	initialCount := len(r.g.sessionLocks)
	r.g.mu.Unlock()

	l1 := r.g.lockSession(conv)
	l1.Lock()

	r.g.mu.Lock()
	duringHold := len(r.g.sessionLocks)
	entry, ok := r.g.sessionLocks[conv]
	r.g.mu.Unlock()

	if !ok || duringHold != initialCount+1 {
		t.Fatalf("lock entry not created in map during hold: count=%d, ok=%v", duringHold, ok)
	}
	if entry.refcount != 1 {
		t.Fatalf("expected refcount 1, got %d", entry.refcount)
	}

	// Second concurrent acquirer increments refcount
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		l2 := r.g.lockSession(conv)
		l2.Lock()
		// l2 acquired after l1 unlocks
		l2.Unlock()
	}()

	// Brief pause to ensure goroutine calls lockSession
	time.Sleep(20 * time.Millisecond)
	r.g.mu.Lock()
	refAfterSecond := r.g.sessionLocks[conv].refcount
	r.g.mu.Unlock()
	if refAfterSecond != 2 {
		t.Fatalf("expected refcount 2 with two acquirers, got %d", refAfterSecond)
	}

	l1.Unlock()
	wg.Wait()

	// Once all holders unlock, the map entry must be completely removed
	r.g.mu.Lock()
	afterRelease := len(r.g.sessionLocks)
	_, stillExists := r.g.sessionLocks[conv]
	r.g.mu.Unlock()

	if stillExists || afterRelease != initialCount {
		t.Fatalf("lock entry was not pruned from map after release: count=%d, exists=%v",
			afterRelease, stillExists)
	}
}

// TestIsMaxBytesDetection verifies Defect 5:
// isMaxBytes distinguishes NATS JetStream bucket capacity limits.
func TestIsMaxBytesDetection(t *testing.T) {
	cases := []struct {
		err  error
		want bool
	}{
		{nil, false},
		{errors.New("other network failure"), false},
		{jetstream.ErrMaxBytesExceeded, true},
		{fmt.Errorf("stream write: %w", jetstream.ErrMaxBytesExceeded), true},
		{errors.New("nats: maximum bytes exceeded"), true},
		{errors.New("nats: max bytes exceeded"), true},
		{errors.New("10047: maximum bytes exceeded"), true},
		{errors.New("nats: 10047 stream limit reached"), true},
	}
	for _, tc := range cases {
		got := isMaxBytes(tc.err)
		if got != tc.want {
			t.Errorf("isMaxBytes(%v) = %v, want %v", tc.err, got, tc.want)
		}
	}
}
