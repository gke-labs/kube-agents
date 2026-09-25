package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
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

// TestSessionRecordRetainedWhilePodActive verifies Defect 1:
// Even if last activity is old, a session with an active pod
// is not deleted out from under running work.
func TestSessionRecordRetainedWhilePodActive(t *testing.T) {
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

	r.g.reapOnce(context.Background())

	gotPod, err := r.g.reg.Get(context.Background(), convPod)
	if err != nil || gotPod == nil {
		t.Fatalf("session with pod was deleted: %v", err)
	}
}

// TestSessionRecordPrunedWithStaleActiveTask verifies that a session whose pod
// has been reaped (or never incarnated) but has a stale ActiveTask whose executor
// died without a terminal or was abandoned is pruned once SessionTTL has elapsed.
func TestSessionRecordPrunedWithStaleActiveTask(t *testing.T) {
	r := startRig(t)
	r.g.cfg.SessionTTL = 24 * time.Hour
	ctx := context.Background()

	convTask := "discord:g1/old-stale-task"
	taskID := "task-stale-99"
	recTask := &SessionRecord{
		Key:          convTask,
		ContextID:    "ctx-stale-task",
		ActiveTask:   &ActiveTask{TaskID: taskID},
		Tasks:        []TaskRef{{ID: taskID}},
		LastActivity: time.Now().UTC().Add(-48 * time.Hour),
	}
	if err := r.g.reg.Put(ctx, recTask); err != nil {
		t.Fatal(err)
	}
	if err := r.g.reg.IndexTask(ctx, taskID, convTask); err != nil {
		t.Fatal(err)
	}

	r.g.reapOnce(ctx)

	gotTask, err := r.g.reg.Get(ctx, convTask)
	if err == nil && gotTask != nil {
		t.Fatalf("session with stale active task was retained: %+v", gotTask)
	}

	// Verify task routing was cleaned up
	routedConv, err := r.g.reg.SessionForTask(ctx, taskID)
	if err == nil && routedConv != "" {
		t.Fatalf("stale task routing was retained for %s: %s", taskID, routedConv)
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
// isMaxBytes distinguishes NATS JetStream bucket capacity and storage exhaustion limits
// without false positives from conversation keys (e.g. Discord snowflakes embedding digits).
func TestIsMaxBytesDetection(t *testing.T) {
	cases := []struct {
		err  error
		want bool
	}{
		{nil, false},
		{errors.New("other network failure"), false},
		// A snowflake or arbitrary digit sequence in the conversation key wrapping an unrelated error must NOT match:
		{fmt.Errorf("session discord:100471234567890123/987654321: %w", errors.New("connection timeout")), false},
		{fmt.Errorf("session discord:987654321/100471234567890123: %w", errors.New("auth refusal")), false},
		{jetstream.ErrMaxBytesExceeded, true},
		{fmt.Errorf("stream write: %w", jetstream.ErrMaxBytesExceeded), true},
		{errors.New("nats: maximum bytes exceeded"), true},
		{errors.New("nats: max bytes exceeded"), true},
		// Typed jetstream.APIError with ErrorCode 10047 (JSStorageResourcesExceededErr):
		{&jetstream.APIError{Code: 500, ErrorCode: jsErrCodeStorageResourcesExceeded, Description: "insufficient storage resources available"}, true},
		{fmt.Errorf("session discord:123/456: %w", &jetstream.APIError{Code: 500, ErrorCode: jsErrCodeStorageResourcesExceeded, Description: "insufficient storage resources available"}), true},
		// Typed jetstream.APIError with max bytes description:
		{&jetstream.APIError{Code: 503, ErrorCode: 10077, Description: "maximum bytes exceeded"}, true},
	}
	for _, tc := range cases {
		got := isMaxBytes(tc.err)
		if got != tc.want {
			t.Errorf("isMaxBytes(%v) = %v, want %v", tc.err, got, tc.want)
		}
	}
}

// TestReapOnceResumableCursor verifies Defect 2:
// Gateway.reapOnce carries the resumption cursor across passes in g.reapCursor,
// so when a pass stops short, the next pass resumes without rescanning already-visited records.
func TestReapOnceResumableCursor(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()

	// Seed 6 sessions
	var seededKeys []string
	for i := 1; i <= 6; i++ {
		key := fmt.Sprintf("discord:g1/reap-cursor-%02d", i)
		seededKeys = append(seededKeys, key)
		rec := &SessionRecord{
			Key:          key,
			ContextID:    fmt.Sprintf("ctx-reap-%02d", i),
			LastActivity: time.Now().UTC(),
		}
		if err := r.g.reg.Put(ctx, rec); err != nil {
			t.Fatal(err)
		}
	}

	// First pass: halt after 3 records via reapScanHook
	var firstPass []string
	r.g.reapScanHook = func(rec *SessionRecord) bool {
		firstPass = append(firstPass, rec.Key)
		return len(firstPass) < 3
	}

	r.g.reapOnce(context.Background())

	if len(firstPass) != 3 {
		t.Fatalf("first reap pass visited %d records, want 3", len(firstPass))
	}

	r.g.mu.Lock()
	savedCursor := r.g.reapCursor
	r.g.mu.Unlock()

	if savedCursor == "" {
		t.Fatal("expected reapCursor to be preserved after partial reap pass, got empty")
	}

	// Second pass: resume from reapCursor to completion
	var secondPass []string
	r.g.reapScanHook = func(rec *SessionRecord) bool {
		secondPass = append(secondPass, rec.Key)
		return true
	}

	r.g.reapOnce(context.Background())

	if len(secondPass) != 3 {
		t.Fatalf("second reap pass visited %d records, want 3", len(secondPass))
	}

	// Verify cursor was reset to empty upon completion
	r.g.mu.Lock()
	finalCursor := r.g.reapCursor
	r.g.mu.Unlock()

	if finalCursor != "" {
		t.Fatalf("expected reapCursor to reset to empty on completion, got %q", finalCursor)
	}

	// Ensure no duplicate keys across passes (asserting resumption)
	seen := make(map[string]bool)
	for _, k := range firstPass {
		seen[k] = true
	}
	for _, k := range secondPass {
		if seen[k] {
			t.Fatalf("record %q was visited in both reap passes", k)
		}
	}

	// Also test the context-cancellation/deadline-exceeded path
	// Seed 4 more sessions
	for i := 1; i <= 4; i++ {
		key := fmt.Sprintf("discord:g1/reap-timeout-%02d", i)
		rec := &SessionRecord{
			Key:          key,
			ContextID:    fmt.Sprintf("ctx-timeout-%02d", i),
			LastActivity: time.Now().UTC(),
		}
		if err := r.g.reg.Put(ctx, rec); err != nil {
			t.Fatal(err)
		}
	}

	cancelCtx, cancel := context.WithCancel(context.Background())
	var timeoutPass []string
	r.g.reapScanHook = func(rec *SessionRecord) bool {
		timeoutPass = append(timeoutPass, rec.Key)
		if len(timeoutPass) == 2 {
			cancel() // simulate deadline exceeded / cancellation mid-scan
		}
		return true
	}

	r.g.reapOnce(cancelCtx)

	r.g.mu.Lock()
	timeoutCursor := r.g.reapCursor
	r.g.mu.Unlock()

	if timeoutCursor == "" {
		t.Fatal("expected reapCursor to be saved when reap pass is interrupted by context cancellation")
	}
}

// TestLateTaskEventForPrunedSessionDroppedWithoutRequeueLoop verifies that a late task
// event for a session record pruned past SessionTTL drops the batch and cleans up the
// task routing index instead of entering an unbounded 2-second requeue loop.
func TestLateTaskEventForPrunedSessionDroppedWithoutRequeueLoop(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()

	conv := "discord:g1/pruned-conv"
	taskID := "task-late-straggler"

	// Index task in KV and cache in memory
	if err := r.g.reg.IndexTask(ctx, taskID, conv); err != nil {
		t.Fatal(err)
	}
	r.g.mu.Lock()
	r.g.taskSessions[taskID] = conv
	r.g.mu.Unlock()

	// Ensure session record is absent
	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil {
		t.Fatal(err)
	}
	if rec != nil {
		t.Fatal("session record must not exist")
	}

	payload, err := json.Marshal(lib.StatusUpdate{
		Status: lib.TaskStatus{State: lib.StateWorking},
	})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(
		lib.Party{Session: "platform"},
		taskID,
		"ctx-straggler",
		"corr-straggler",
		payload,
	)
	if err != nil {
		t.Fatal(err)
	}

	item := relayItem{
		env:     env,
		subject: "a2a.tasks.platform." + taskID + ".events",
	}

	// Directly invoke relayBatch
	r.g.relayBatch(conv, []relayItem{item})

	// Wait briefly to ensure no requeue goroutine executes
	time.Sleep(50 * time.Millisecond)

	// Verify in-memory task routing was retired
	r.g.mu.Lock()
	cached, ok := r.g.taskSessions[taskID]
	r.g.mu.Unlock()
	if ok || cached != "" {
		t.Fatalf("taskSessions was not deleted: %q", cached)
	}

	// Verify KV task index was dropped
	sessionFromKV, err := r.g.reg.SessionForTask(ctx, taskID)
	if err != nil {
		t.Fatalf("SessionForTask error: %v", err)
	}
	if sessionFromKV != "" {
		t.Fatalf("KV task index was not dropped: %q", sessionFromKV)
	}

	// Verify sessionForTask resolves to empty
	resolved := r.g.sessionForTask(ctx, taskID)
	if resolved != "" {
		t.Fatalf("sessionForTask resolved pruned task to %q", resolved)
	}
}

// TestLiveSessionLifecycleAgainstCluster exercises session record pruning and retention against
// a live NATS deployment when A2A_LIVE_NATS_URL is set (e.g. against kyber-test).
func TestLiveSessionLifecycleAgainstCluster(t *testing.T) {
	url := os.Getenv("A2A_LIVE_NATS_URL")
	if url == "" {
		t.Skip("A2A_LIVE_NATS_URL not set; skipping live cluster verification")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("nats connect: %v", err)
	}
	defer nc.Close()

	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}

	_, _ = js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:      lib.TasksStream,
		Subjects:  []string{"a2a.tasks.>"},
		Retention: jetstream.LimitsPolicy,
		MaxAge:    72 * time.Hour,
	})
	kv, err := js.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: lib.SessionStateBucket})
	if err != nil {
		kv, err = js.KeyValue(ctx, lib.SessionStateBucket)
		if err != nil {
			t.Fatalf("kv session-state: %v", err)
		}
	}

	client, err := lib.Connect(ctx, url, lib.WithName("gateway-livetest-client"))
	if err != nil {
		t.Fatalf("lib.Connect: %v", err)
	}
	defer client.Close()

	reg := NewRegistry(client)

	expiredKey := "discord:g1/live-expired-conv"
	expiredRec := &SessionRecord{
		Key:          expiredKey,
		ContextID:    "ctx-live-expired",
		Kind:         "group",
		Addressee:    "platform",
		LastActivity: time.Now().UTC().Add(-48 * time.Hour),
	}
	if err := reg.Put(ctx, expiredRec); err != nil {
		t.Fatalf("put expired: %v", err)
	}

	recentKey := "discord:g1/live-recent-conv"
	recentRec := &SessionRecord{
		Key:          recentKey,
		ContextID:    "ctx-live-recent",
		Kind:         "group",
		Addressee:    "platform",
		LastActivity: time.Now().UTC().Add(-10 * time.Minute),
	}
	if err := reg.Put(ctx, recentRec); err != nil {
		t.Fatalf("put recent: %v", err)
	}

	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("1001 test:bnaylor\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	cfg := &Config{
		NATSURL:          url,
		PrincipalMapPath: mapFile,
		DefaultAddressee: "platform",
		IdleTTL:          30 * time.Minute,
		SessionTTL:       24 * time.Hour,
		AttributionSalt:  []byte("test-salt"),
	}

	g, err := New(Options{
		Client:  client,
		Adapter: newFakeAdapter(),
		Config:  cfg,
		Backend: "discord",
	})
	if err != nil {
		t.Fatalf("New gateway: %v", err)
	}

	// Run reap pass against the live cluster NATS
	g.reapOnce(ctx)

	// Verify in KV bucket
	gotExpired, err := reg.Get(ctx, expiredKey)
	if err != nil {
		t.Fatalf("Get expired: %v", err)
	}
	if gotExpired != nil {
		t.Fatalf("expected expired session record to be pruned from KV bucket, found: %+v", gotExpired)
	}

	gotRecent, err := reg.Get(ctx, recentKey)
	if err != nil {
		t.Fatalf("Get recent: %v", err)
	}
	if gotRecent == nil {
		t.Fatal("expected recent session record to be retained in KV bucket, got nil")
	}

	// Clean up recent record
	_ = kv.Delete(ctx, kvKey(recentKey))
}
