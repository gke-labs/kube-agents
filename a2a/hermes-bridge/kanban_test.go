package hermesbridge

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
	"unicode/utf8"

	lib "github.com/gke-labs/kube-agents/a2a/lib"
)

// kanbanFixtureDB builds a Hermes-shaped kanban.db (tasks, task_runs, task_events) with
// python3's sqlite3 module, the same interpreter the bridge polls with. Skips without python3.
func kanbanFixtureDB(t *testing.T, since int64) string {
	t.Helper()
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH; the kanban fixture needs it")
	}
	db := filepath.Join(t.TempDir(), "kanban.db")
	script := `
import sqlite3, sys
db, since = sys.argv[1], int(sys.argv[2])
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, creator_session_id TEXT,
  result TEXT, last_failure_error TEXT, created_at REAL, updated_at REAL, parent_id TEXT);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, error TEXT, session_id TEXT, verdict TEXT, metadata TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);
""")
rows = [
  ("t_done",    "Health check for drift-peer-ap-1", "done",    "cluster-diagnostics", "a2a-ctx-1", "All nodes Ready.", "", since + 10, None),
  ("t_running", "Health check for drift-peer-ap-2", "running", "cluster-diagnostics", "a2a-ctx-1", "",                 "", since + 11, None),
  ("t_child",   "Sub-check for drift-peer-ap-2",    "running", "cluster-diagnostics", "worker-9",  "",                 "", since + 12, "t_running"),
  ("t_blocked", "Approve rollout",                  "blocked", "release",             "a2a-ctx-1", "Checked staging cluster; ready for prod.", "", since + 13, None),
  ("t_failed",  "Broken check",                     "failed",  "cluster-diagnostics", "a2a-ctx-1", "",                 "boom", since + 14, None),
  ("t_other",   "Someone else's card",              "done",    "cluster-diagnostics", "a2a-ctx-2", "n/a",              "", since + 15, None),
  ("t_old",     "Card from an earlier turn",        "done",    "cluster-diagnostics", "a2a-ctx-1", "old result",       "", since - 100, None),
]
c.executemany("INSERT INTO tasks (id, title, status, assignee, creator_session_id, result, last_failure_error, created_at, updated_at, parent_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
  [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[7], r[8]) for r in rows])
c.executemany("INSERT INTO task_runs (task_id, summary, error, session_id, verdict, metadata) VALUES (?,?,?,?,?,?)", [
  ("t_done",    "",                 "", "20261017_100000_aaaa", "",        '{"health_status":"healthy"}'),
  ("t_running", "",                 "", "20261017_100100_bbbb", "",        ""),
  ("t_failed",  "could not reach",  "", "20261017_100200_cccc", "failed",  ""),
])
c.execute("INSERT INTO task_events (task_id, kind, payload) VALUES (?,?,?)", ("t_blocked", "blocked", '{"question":"Approve the rollout to prod?"}'))
c.commit()
`
	cmd := exec.Command("python3", "-c", script, db, strconv.FormatInt(since, 10))
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("building kanban fixture: %v\n%s", err, out)
	}
	return db
}

func kanbanRowsByID(rows []kanbanCardRow) map[string]kanbanCardRow {
	out := make(map[string]kanbanCardRow, len(rows))
	for _, r := range rows {
		out[r.ID] = r
	}
	return out
}

func TestKanbanDBScript_PollReadsCardsRunsAndEvents(t *testing.T) {
	since := time.Now().Unix() - 3600
	db := kanbanFixtureDB(t, since)
	ctx := context.Background()

	rows, err := querySessionKanbanCards(ctx, "python3", db, "a2a-ctx-1", since, nil)
	if err != nil {
		t.Fatal(err)
	}
	got := kanbanRowsByID(rows)
	for _, want := range []string{"t_done", "t_running", "t_blocked", "t_failed"} {
		if _, ok := got[want]; !ok {
			t.Errorf("poll missing %s; got %v", want, rows)
		}
	}
	for _, unwanted := range []string{"t_other", "t_old", "t_child"} {
		if _, ok := got[unwanted]; ok {
			t.Errorf("poll returned %s, which belongs to another session or an earlier turn", unwanted)
		}
	}
	if d := got["t_done"]; d.WorkerSessionID != "20261017_100000_aaaa" || d.Verdict != "healthy" || d.Result != "All nodes Ready." || d.Assignee != "cluster-diagnostics" {
		t.Errorf("t_done row did not join its run (worker session, verdict from metadata): %+v", d)
	}
	if f := got["t_failed"]; f.Verdict != "failed" || f.Err != "boom" || f.Result != "could not reach" {
		t.Errorf("t_failed row did not carry its verdict, error, and run summary: %+v", f)
	}
	if b := got["t_blocked"]; !strings.Contains(b.BlockPayload, "Approve the rollout") {
		t.Errorf("t_blocked row did not carry its blocked event payload: %+v", b)
	}
	if formatKanbanCardOutput(got["t_blocked"]) != "Approve the rollout to prod?" {
		t.Errorf("blocked card output = %q, want the question asked", formatKanbanCardOutput(got["t_blocked"]))
	}

	// A tracked id from an earlier turn stays in the poll even though it predates the window.
	rows, err = querySessionKanbanCards(ctx, "python3", db, "a2a-ctx-1", since, []string{"t_old"})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := kanbanRowsByID(rows)["t_old"]; !ok {
		t.Errorf("tracked card t_old missing from poll: %v", rows)
	}

	// The out-of-turn watcher's wildcard poll sees every a2a-* session's cards in the window.
	rows, err = querySessionKanbanCards(ctx, "python3", db, "*", since, nil)
	if err != nil {
		t.Fatal(err)
	}
	got = kanbanRowsByID(rows)
	if _, ok := got["t_other"]; !ok {
		t.Errorf("wildcard poll missing t_other (another a2a session): %v", rows)
	}
	if _, ok := got["t_child"]; ok {
		t.Errorf("wildcard poll returned t_child, whose session is a worker's, not an a2a-* session")
	}
}

func TestCancelSessionCards_CallsSessionKVCardStopEndpoint(t *testing.T) {
	var (
		mu       sync.Mutex
		gotPath  string
		gotAuth  string
		gotReq   cardStopRequest
		calledCh = make(chan struct{}, 1)
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		gotPath = r.URL.Path
		gotAuth = r.Header.Get("Authorization")
		_ = json.NewDecoder(r.Body).Decode(&gotReq)
		mu.Unlock()
		select {
		case calledCh <- struct{}{}:
		default:
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"session_id":"a2a-ctx-1","stopped":[{"id":"t_running","title":"Check","prior_status":"running"}]}`))
	}))
	defer srv.Close()

	b := &Bridge{
		cfg: Config{
			RouteURL:        srv.URL,
			RouteKey:        "session-kv-token",
			CardStopTimeout: 5 * time.Second,
		},
		routeClient: srv.Client(),
	}
	b.rememberRoute("a2a-ctx-1", conversationRoute{
		Platform:     lib.NotifyPlatformConsole,
		Conversation: "console:conv-1",
		ContextID:    "ctx-1",
	})

	b.markSessionCanceled("a2a-ctx-1")
	if !b.isSessionCanceled("a2a-ctx-1") {
		t.Fatal("isSessionCanceled(a2a-ctx-1) = false, want true")
	}

	select {
	case <-calledCh:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for session-kv /cards/stop call")
	}

	mu.Lock()
	defer mu.Unlock()
	if gotPath != "/v1/sessions/a2a-ctx-1/cards/stop" {
		t.Errorf("card stop path = %q, want /v1/sessions/a2a-ctx-1/cards/stop", gotPath)
	}
	if gotAuth != "Bearer session-kv-token" {
		t.Errorf("Authorization = %q, want Bearer session-kv-token", gotAuth)
	}
	if gotReq.ContextID != "ctx-1" || gotReq.Conversation != "console:conv-1" {
		t.Errorf("cardStopRequest = %+v, want ContextID=ctx-1 Conversation=console:conv-1", gotReq)
	}
}

func TestCancelQueuedTask_DoesNotCancelActiveSessionOrStopCards(t *testing.T) {
	_, url := startServer(t)
	var stopCalls atomic.Int32
	routeSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/cards/stop") {
			stopCalls.Add(1)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer routeSrv.Close()

	stub := newAPIStub(t, nil)
	b := startAPIBridge(t, url, stub, func(cfg *Config) {
		cfg.RouteURL = routeSrv.URL
		cfg.RouteKey = "route-key"
	})

	sessionID := "a2a-ctx-cwait-cards"
	release := b.turns.acquire(context.Background(), sessionID)
	if release == nil {
		t.Fatal("could not acquire session turn")
	}
	t.Cleanup(release)
	b.beginKanbanWait(sessionID)
	defer b.endKanbanWait(sessionID)

	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-queued-cancel", "ctx-cwait-cards", "second")
	waitFor(t, 10*time.Second, "the second task queued on the session turn", func() bool {
		b.turns.mu.Lock()
		defer b.turns.mu.Unlock()
		slot, ok := b.turns.slots[sessionID]
		return ok && slot.refs == 2
	})

	publishCancel(t, c, origin)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCanceled {
		t.Fatalf("queued task state = %s, want canceled", task.State)
	}
	if b.isSessionCanceled(sessionID) {
		t.Fatal("canceling a queued task waiting for the session turn must not mark the active session canceled")
	}
	if !b.hasActiveKanbanWait(sessionID) {
		t.Fatal("active task's kanban wait should still be active")
	}
	if got := stopCalls.Load(); got != 0 {
		t.Fatalf("session-kv /cards/stop calls = %d, want 0", got)
	}
}

func TestCancelActiveKanbanWait_MarksSessionCanceledBeforeWaitEnds(t *testing.T) {
	since := time.Now().Unix() - 60
	db := kanbanFixtureDB(t, since)
	t.Setenv(hermesKanbanDBEnv, db)

	_, url := startServer(t)
	stub := newAPIStub(t, nil)
	b := startAPIBridge(t, url, stub, nil)

	sessionID := "a2a-ctx-1"
	taskCtx, cancelTask := context.WithCancel(context.Background())
	run := &taskRun{
		origin: &lib.Envelope{TaskID: "task-active-cancel", ContextID: "ctx-1", CorrelationID: "corr-active-cancel"},
		state:  stateRunning,
	}
	act := newSessionActivityState(sessionID, false)
	act.cardsFiled.Store(true)

	waitDone := make(chan struct{})
	go func() {
		defer close(waitDone)
		b.waitForKanbanCards(run, taskCtx, act, sessionID, since, 1, "working")
	}()

	waitFor(t, 5*time.Second, "waitForKanbanCards to enter active wait", func() bool {
		return b.hasActiveKanbanWait(sessionID)
	})

	var sawUncanceledAfterWaitEnded atomic.Bool
	observerStop := make(chan struct{})
	observerDone := make(chan struct{})
	go func() {
		defer close(observerDone)
		for {
			select {
			case <-observerStop:
				return
			default:
				if !b.hasActiveKanbanWait(sessionID) && !b.isSessionCanceled(sessionID) {
					sawUncanceledAfterWaitEnded.Store(true)
					return
				}
			}
		}
	}()

	run.canceled.Store(true)
	cancelTask()

	select {
	case <-waitDone:
	case <-time.After(5 * time.Second):
		close(observerStop)
		t.Fatal("waitForKanbanCards did not exit after cancel")
	}
	close(observerStop)
	<-observerDone

	if sawUncanceledAfterWaitEnded.Load() {
		t.Fatal("observed a window where hasActiveKanbanWait was false before isSessionCanceled became true")
	}

	if b.hasActiveKanbanWait(sessionID) {
		t.Fatal("hasActiveKanbanWait(a2a-ctx-1) = true after waitForKanbanCards returned, want false")
	}
	if !b.isSessionCanceled(sessionID) {
		t.Fatal("isSessionCanceled(a2a-ctx-1) = false when active wait ended, want true")
	}
	if _, ok := b.shouldDeliverOutOfTurnKanbanCard("t_done", sessionID); ok {
		t.Fatal("shouldDeliverOutOfTurnKanbanCard returned true for a canceled session")
	}
}

func TestOutOfTurnWatcher_LazyDBCreationAndSlidingWindow(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH; the kanban fixture needs it")
	}
	prevInterval := outOfTurnPollInterval
	outOfTurnPollInterval = 25 * time.Millisecond
	t.Cleanup(func() { outOfTurnPollInterval = prevInterval })

	lazyDir := t.TempDir()
	lazyDB := filepath.Join(lazyDir, "kanban.db")
	t.Setenv(hermesKanbanDBEnv, lazyDB)

	b := &Bridge{}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		b.watchOutOfTurnKanbanCards(ctx)
	}()
	t.Cleanup(func() {
		cancel()
		<-done
	})

	// Give the watcher a couple ticks while kanban.db does not exist yet.
	time.Sleep(70 * time.Millisecond)

	// Now create kanban.db lazily with a settled card in the active window.
	now := time.Now().Unix()
	srcDB := kanbanFixtureDB(t, now-10)
	if out, err := exec.Command("cp", srcDB, lazyDB).CombinedOutput(); err != nil {
		t.Fatalf("copying fixture db to lazy path: %v\n%s", err, out)
	}

	waitFor(t, 5*time.Second, "watcher to pick up lazily created kanban.db and deliver t_done", func() bool {
		return b.isKanbanCardDelivered("t_done")
	})
}

func TestOutOfTurnWatcher_DeliveredIDsBoundedEvictionDoesNotReplayRecentCards(t *testing.T) {
	b := &Bridge{}
	baseNow := time.Now()
	prevNow := kanbanNow
	var offsetSecs atomic.Int64
	kanbanNow = func() time.Time {
		return baseNow.Add(time.Duration(offsetSecs.Load()) * time.Second)
	}
	t.Cleanup(func() { kanbanNow = prevNow })

	// Record an expired card (older than 2*kanbanOutOfTurnWindowSecs) and a set of recent cards.
	offsetSecs.Store(-int64(kanbanTrackedRetentionSecs + 100))
	b.markKanbanCardDelivered("t_expired")

	offsetSecs.Store(0)
	for i := 0; i < maxTrackedKanbanEntries+100; i++ {
		offsetSecs.Add(1)
		b.markKanbanCardDelivered(fmt.Sprintf("t_card_%04d", i))
	}

	if b.isKanbanCardDelivered("t_expired") {
		t.Errorf("t_expired should have been evicted past retention window")
	}
	// The oldest cards beyond maxTrackedKanbanEntries were evicted one-by-one,
	// while the most recent maxTrackedKanbanEntries cards remain delivered.
	if b.isKanbanCardDelivered("t_card_0000") {
		t.Errorf("oldest card t_card_0000 should have been evicted when exceeding maxTrackedKanbanEntries")
	}
	for i := 100; i < maxTrackedKanbanEntries+100; i++ {
		id := fmt.Sprintf("t_card_%04d", i)
		if !b.isKanbanCardDelivered(id) {
			t.Fatalf("recent card %s was forgotten after exceeding maxTrackedKanbanEntries", id)
		}
		if _, ok := b.shouldDeliverOutOfTurnKanbanCard(id, "a2a-ctx-1"); ok {
			t.Fatalf("recent card %s was re-delivered after exceeding maxTrackedKanbanEntries", id)
		}
	}
}

func TestSynthesizeCardAnswer_CapsPromptOnUTF8Boundary(t *testing.T) {
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		writeCompletion(w, c.sessionID, "synthesized fleet summary")
	})
	b := &Bridge{
		cfg: Config{
			APIURL:   stub.srv.URL + "/v1/chat/completions",
			APIKey:   testAPIKey,
			APIModel: DefaultAPIModel,
		},
		apiClient: stub.srv.Client(),
	}
	run := &taskRun{
		origin: &lib.Envelope{TaskID: "task-synth-cap", ContextID: "ctx-synth"},
		state:  stateRunning,
	}

	// Build multi-byte UTF-8 card outputs ("🔥" is 4 bytes) exceeding cardSynthesisInputCap.
	hugeChunk := strings.Repeat("集群状态正常🔥", (cardSynthesisInputCap/10)+500)
	cards := []kanbanCardRow{
		{ID: "t_1", Title: "Cluster 1", Status: "done", Result: hugeChunk},
		{ID: "t_2", Title: "Cluster 2", Status: "done", Result: hugeChunk},
	}
	parts := []string{hugeChunk, hugeChunk}

	got := b.synthesizeCardAnswer(run, context.Background(), "a2a-ctx-synth", 1, "ack", cards, parts)
	if got != "synthesized fleet summary" {
		t.Fatalf("synthesizeCardAnswer = %q, want synthesized fleet summary", got)
	}
	calls := stub.seen()
	if len(calls) != 1 {
		t.Fatalf("apiSynthesisTurn calls = %d, want 1", len(calls))
	}
	prompt := calls[0].prompt
	if len(prompt) > cardSynthesisInputCap {
		t.Errorf("synthesis prompt length = %d, want <= %d (cardSynthesisInputCap)", len(prompt), cardSynthesisInputCap)
	}
	if !utf8.ValidString(prompt) {
		t.Errorf("synthesis prompt is not valid UTF-8 after truncation")
	}
	if !strings.HasSuffix(prompt, cardSynthesisTruncationNote) {
		t.Errorf("synthesis prompt missing truncation note suffix")
	}
}

func TestSynthesizeCardAnswer_CancelStopsHermesTurn(t *testing.T) {
	reqArrived := make(chan struct{})
	stopCalled := make(chan string, 1)
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		close(reqArrived)
		<-r.Context().Done()
	})
	stub.stop = func(w http.ResponseWriter, r *http.Request) {
		select {
		case stopCalled <- r.URL.Path:
		default:
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"turns":1,"interrupted":1,"ended":true}`))
	}

	b := &Bridge{
		cfg: Config{
			APIURL:   stub.srv.URL + "/v1/chat/completions",
			APIKey:   testAPIKey,
			APIModel: DefaultAPIModel,
		},
		apiClient: stub.srv.Client(),
	}
	run := &taskRun{
		origin: &lib.Envelope{TaskID: "task-synth-cancel", ContextID: "ctx-synth-cancel"},
		state:  stateRunning,
	}
	taskCtx, cancelTask := context.WithCancel(context.Background())

	cards := []kanbanCardRow{
		{ID: "t_1", Title: "Cluster 1", Status: "done", Result: "Nodes Ready"},
		{ID: "t_2", Title: "Cluster 2", Status: "done", Result: "Pods Healthy"},
	}
	parts := []string{"Nodes Ready", "Pods Healthy"}

	done := make(chan string, 1)
	go func() {
		done <- b.synthesizeCardAnswer(run, taskCtx, "a2a-ctx-synth-cancel", 1, "ack", cards, parts)
	}()

	select {
	case <-reqArrived:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for synthesis request to reach API server")
	}

	run.canceled.Store(true)
	cancelTask()

	select {
	case stopPath := <-stopCalled:
		wantPath := "/v1/kube-agents/sessions/a2a-ctx-synth-cancel/stop"
		if stopPath != wantPath {
			t.Errorf("turn stop path = %q, want %q", stopPath, wantPath)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for turn-stop endpoint to be called after synthesis cancel")
	}

	select {
	case fallback := <-done:
		if !utf8.ValidString(fallback) {
			t.Errorf("fallback answer is not valid UTF-8")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("synthesizeCardAnswer did not return after cancel")
	}
}

func TestKanbanBlockedCard_UnblockedCompletionDeliveredAndSyntheticTaskFinalized(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, nil)
	b := startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)

	sessionID := "a2a-ctx-unblock"
	origin := &lib.Envelope{
		TaskID:        "task-origin-unblock",
		ContextID:     "ctx-unblock",
		CorrelationID: "corr-unblock",
		Authority:     authorityFor(t, mintFor(t, c, "task-card-t-unblock", "platform")),
	}
	b.rememberConsoleSession(sessionID, origin)
	b.kanban.rememberCardOrigin("t_unblock", origin.TaskID)

	blockedRow := kanbanCardRow{
		ID:           "t_unblock",
		Title:        "Approve prod rollout",
		Status:       "blocked",
		SessionID:    sessionID,
		BlockPayload: `{"question":"Approve prod rollout?"}`,
	}

	// First confirm poll records pendingSettle; second confirm poll delivers the "blocked" state.
	if _, ok := b.shouldDeliverOutOfTurnKanbanCardForStatus(blockedRow.ID, blockedRow.Status, sessionID); ok {
		t.Fatal("expected first confirm poll for blocked card to wait for confirmation")
	}
	meta, ok := b.shouldDeliverOutOfTurnKanbanCardForStatus(blockedRow.ID, blockedRow.Status, sessionID)
	if !ok {
		t.Fatal("expected second confirm poll for blocked card to deliver")
	}
	b.publishOutOfTurnKanbanCard(testCtx(t), blockedRow, meta)

	// While still "blocked", the card must not be re-delivered.
	if !b.isKanbanCardDeliveredForStatus(blockedRow.ID, "blocked") {
		t.Fatal("isKanbanCardDeliveredForStatus(t_unblock, blocked) = false, want true")
	}
	if _, ok := b.shouldDeliverOutOfTurnKanbanCardForStatus(blockedRow.ID, "blocked", sessionID); ok {
		t.Fatal("blocked card was re-delivered while still blocked")
	}

	synthTaskID := sanitizeKanbanTaskToken(blockedRow.ID)
	blockedTask := fold(t, c, synthTaskID)
	if blockedTask.State != lib.StateInputRequired || blockedTask.Final {
		t.Fatalf("blocked synthetic task state=%s final=%v, want state=input-required final=false", blockedTask.State, blockedTask.Final)
	}

	// Once the card is unblocked and finishes "done", it is not yet delivered for "done".
	doneRow := kanbanCardRow{
		ID:        "t_unblock",
		Title:     "Approve prod rollout",
		Status:    "done",
		SessionID: sessionID,
		Result:    "Rollout completed across all prod clusters.",
	}
	if b.isKanbanCardDeliveredForStatus(doneRow.ID, doneRow.Status) {
		t.Fatal("isKanbanCardDeliveredForStatus(t_unblock, done) = true before terminal delivery, want false")
	}
	if _, ok := b.shouldDeliverOutOfTurnKanbanCardForStatus(doneRow.ID, doneRow.Status, sessionID); ok {
		t.Fatal("expected first confirm poll for done state to wait for confirmation")
	}
	meta, ok = b.shouldDeliverOutOfTurnKanbanCardForStatus(doneRow.ID, doneRow.Status, sessionID)
	if !ok {
		t.Fatal("expected second confirm poll for unblocked done card to deliver")
	}
	b.publishOutOfTurnKanbanCard(testCtx(t), doneRow, meta)

	finalTask := fold(t, c, synthTaskID)
	if finalTask.SubmittedMissing {
		t.Fatal("final synthetic task fold has SubmittedMissing = true")
	}
	if finalTask.PostFinalDropped != 0 {
		t.Fatalf("final synthetic task fold has PostFinalDropped = %d, want 0", finalTask.PostFinalDropped)
	}
	if finalTask.State != lib.StateCompleted || !finalTask.Final {
		t.Fatalf("final synthetic task state=%s final=%v, want state=completed final=true", finalTask.State, finalTask.Final)
	}
	if got := finalTask.Artifact(lib.ArtifactResult).Parts[0].Text; got != doneRow.Result {
		t.Fatalf("final synthetic task result = %q, want %q", got, doneRow.Result)
	}
}

func TestWaitForKanbanCards_CarriedOverCardAcrossSteerPreservesFollowUpAnswer(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH; the kanban fixture needs it")
	}
	since := time.Now().Unix() - 10
	db := filepath.Join(t.TempDir(), "kanban.db")
	initScript := `
import sqlite3, sys
db, since = sys.argv[1], int(sys.argv[2])
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, creator_session_id TEXT,
  result TEXT, last_failure_error TEXT, created_at REAL, updated_at REAL, parent_id TEXT);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, error TEXT, session_id TEXT, verdict TEXT, metadata TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);
""")
c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
  ("t_carry", "Cluster audit", "running", "cluster-diagnostics", "a2a-ctx-carry", "", "", since + 1, since + 1, None))
c.commit()
`
	if out, err := exec.Command("python3", "-c", initScript, db, strconv.FormatInt(since, 10)).CombinedOutput(); err != nil {
		t.Fatalf("init carry db: %v\n%s", err, out)
	}
	t.Setenv(hermesKanbanDBEnv, db)

	_, url := startServer(t)
	stub := newAPIStub(t, nil)
	b := startAPIBridge(t, url, stub, nil)

	sessionID := "a2a-ctx-carry"
	run := &taskRun{
		origin: &lib.Envelope{TaskID: "task-carry", ContextID: "ctx-carry", CorrelationID: "corr-carry"},
		state:  stateRunning,
		steers: []*lib.Envelope{{EnvelopeID: "steer-1"}},
	}
	act := newSessionActivityState(sessionID, false)
	act.cardsFiled.Store(true)
	run.act.Store(act)

	// Turn 1 breaks out early because a steer is queued while t_carry is still running.
	gotTurn1, ok := b.waitForKanbanCards(run, context.Background(), act, sessionID, since, 1, "Dispatched cluster audit card.")
	if !ok || gotTurn1 != "Dispatched cluster audit card." {
		t.Fatalf("Turn 1 waitForKanbanCards = (%q, %v), want (\"Dispatched cluster audit card.\", true)", gotTurn1, ok)
	}
	if !act.cardsFiled.Load() {
		t.Fatal("expected cardsFiled to remain true when Turn 1 broke out early with unsettled card")
	}

	// Simulate Turn 2: no steers left, Turn 2 did not file cards itself, and t_carry settles after Turn 2's first poll.
	run.mu.Lock()
	run.steers = nil
	run.mu.Unlock()
	act.beginTurn()
	act.cardsFiled.Store(false)

	go func() {
		time.Sleep(150 * time.Millisecond)
		updateScript := `
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("UPDATE tasks SET status = 'done', result = 'Card report: all 3 clusters healthy.' WHERE id = 't_carry'")
c.commit()
`
		_ = exec.Command("python3", "-c", updateScript, db).Run()
	}()

	followUpAnswer := "Answer to your follow-up question: maintenance window is 02:00 UTC."
	gotTurn2, ok := b.waitForKanbanCards(run, context.Background(), act, sessionID, since, 2, followUpAnswer)
	if !ok {
		t.Fatal("Turn 2 waitForKanbanCards returned ok = false, want true")
	}
	if !strings.Contains(gotTurn2, followUpAnswer) {
		t.Fatalf("Turn 2 final answer dropped follow-up response: got %q, want it to contain %q", gotTurn2, followUpAnswer)
	}
	if !strings.Contains(gotTurn2, "Card report: all 3 clusters healthy.") {
		t.Fatalf("Turn 2 final answer missing carried-over card report: got %q", gotTurn2)
	}
}

func TestWaitForKanbanCards_CompletedTurnWithNoCardsWinsCancelRace(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH; the kanban fixture needs it")
	}
	since := time.Now().Unix() - 10
	// Fixture DB has cards for a2a-ctx-1, NOT for a2a-ctx-nocards.
	db := kanbanFixtureDB(t, since)
	t.Setenv(hermesKanbanDBEnv, db)

	var stopCalls atomic.Int32
	routeSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/cards/stop") {
			stopCalls.Add(1)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer routeSrv.Close()

	_, url := startServer(t)
	c := gatewayClient(t, url)
	var (
		originMu sync.Mutex
		origin   *lib.Envelope
		bridge   *Bridge
	)
	originReady := make(chan struct{})

	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, call apiCall) {
		if call.sessionID == "a2a-ctx-nocards-e2e" {
			<-originReady
			originMu.Lock()
			orig := origin
			originMu.Unlock()
			if orig != nil {
				bridge.mu.Lock()
				r := bridge.tasks[orig.TaskID]
				bridge.mu.Unlock()
				if r != nil {
					r.canceled.Store(true)
				}
			}
		}
		writeCompletion(w, call.sessionID, "completed answer before cancel")
	})
	bridge = startAPIBridge(t, url, stub, func(cfg *Config) {
		cfg.RouteURL = routeSrv.URL
		cfg.RouteKey = "route-key"
	})

	// Also verify waitForKanbanCards directly when taskCtx is already canceled on a turn that filed no cards.
	sessionID := "a2a-ctx-nocards"
	canceledCtx, cancel := context.WithCancel(context.Background())
	cancel()
	run := &taskRun{
		origin: &lib.Envelope{TaskID: "task-nocards-direct", ContextID: "ctx-nocards", CorrelationID: "corr-nocards"},
		state:  stateRunning,
	}
	run.canceled.Store(true)
	act := newSessionActivityState(sessionID, false)

	gotText, ok := bridge.waitForKanbanCards(run, canceledCtx, act, sessionID, since, 1, "completed answer before cancel")
	if !ok || gotText != "completed answer before cancel" {
		t.Fatalf("waitForKanbanCards on canceled context with no cards = (%q, %v), want (\"completed answer before cancel\", true)", gotText, ok)
	}
	if bridge.isSessionCanceled(sessionID) {
		t.Fatal("session was marked canceled even though no cards were filed or tracked")
	}
	if got := stopCalls.Load(); got != 0 {
		t.Fatalf("session-kv /cards/stop calls = %d, want 0", got)
	}

	// End-to-end via runTaskAPI: cancel arrives just as apiTurn finishes its response on a turn with no cards.
	originMu.Lock()
	origin = submitIn(t, c, "task-nocards-e2e", "ctx-nocards-e2e", "quick question")
	originMu.Unlock()
	close(originReady)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted {
		t.Fatalf("task state = %s, want completed", task.State)
	}
}

func TestKanbanQuery_WildcardUpdatedAtGaveUpAndRepeatBlockAfterUnblock(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH; the kanban fixture needs it")
	}
	now := time.Now().Unix()
	since := now - kanbanOutOfTurnWindowSecs
	oldCreated := since - 3600
	db := filepath.Join(t.TempDir(), "kanban.db")
	initScript := `
import sqlite3, sys
db, old_created, now_ts = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, creator_session_id TEXT,
  result TEXT, last_failure_error TEXT, created_at REAL, updated_at REAL, parent_id TEXT);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, error TEXT, session_id TEXT, verdict TEXT, metadata TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);
""")
# 1. Card created before the 2-hour window that recently settled (updated_at >= since).
c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
  ("t_old_settled", "Long-lived card", "done", "cluster-a", "a2a-ctx-1", "Finally done", "", old_created, now_ts, None))
# 2. Card parked in status='blocked' by a breaker trip whose newest event is kind='gave_up'.
c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
  ("t_breaker", "Poison card", "blocked", "cluster-b", "a2a-ctx-1", "", "worker crashed 3 times", old_created, now_ts, None))
c.execute("INSERT INTO task_events (id, task_id, kind, payload) VALUES (?,?,?,?)",
  (10, "t_breaker", "gave_up", '{"failures":3,"effective_limit":3,"limit_source":"dispatcher","error":"breaker tripped after 3 crashes","trigger_outcome":"crashed"}'))
# 3. Card that blocked, was unblocked, and then failed without a new block_payload.
c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
  ("t_unblocked_err", "Unblocked then failed", "failed", "cluster-c", "a2a-ctx-1", "", "post-unblock failure", now_ts, now_ts, None))
c.execute("INSERT INTO task_events (id, task_id, kind, payload) VALUES (?,?,?,?)",
  (20, "t_unblocked_err", "blocked", '{"question":"Stale question before unblock?"}'))
c.execute("INSERT INTO task_events (id, task_id, kind, payload) VALUES (?,?,?,?)",
  (21, "t_unblocked_err", "unblocked", '{}'))
# 4. Card that blocked (id=30), was unblocked (id=31), and blocked a second time (id=32).
c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
  ("t_reblock", "Re-blocked card", "blocked", "cluster-d", "a2a-ctx-1", "", "", now_ts, now_ts, None))
c.execute("INSERT INTO task_events (id, task_id, kind, payload) VALUES (?,?,?,?)",
  (30, "t_reblock", "blocked", '{"question":"First question?"}'))
# 5. Card recovered via reassign_task (no 'unblocked' event) that later failed.
c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
  ("t_reassigned_err", "Reassigned then failed", "failed", "cluster-e", "a2a-ctx-1", "", "post-reassign failure", now_ts, now_ts, None))
c.execute("INSERT INTO task_events (id, task_id, kind, payload) VALUES (?,?,?,?)",
  (40, "t_reassigned_err", "blocked", '{"question":"Stale question before reassign?"}'))
c.commit()
`
	if out, err := exec.Command("python3", "-c", initScript, db, strconv.FormatInt(oldCreated, 10), strconv.FormatInt(now, 10)).CombinedOutput(); err != nil {
		t.Fatalf("init db: %v\n%s", err, out)
	}

	rows, err := querySessionKanbanCards(context.Background(), "python3", db, "*", since, nil)
	if err != nil {
		t.Fatalf("wildcard querySessionKanbanCards: %v", err)
	}
	byID := map[string]kanbanCardRow{}
	for _, r := range rows {
		byID[r.ID] = r
	}

	// 1. Wildcard query includes t_old_settled via updated_at >= since.
	if _, ok := byID["t_old_settled"]; !ok {
		t.Fatal("wildcard query omitted t_old_settled whose created_at < since but updated_at >= since")
	}

	// 2. Breaker trip with newest event 'gave_up' reclassifies status to "gave_up" (terminal) and formats error text instead of raw JSON.
	breaker, ok := byID["t_breaker"]
	if !ok {
		t.Fatal("wildcard query omitted t_breaker")
	}
	if breaker.Status != "gave_up" || !isKanbanStatusTerminal(breaker.Status) || isKanbanStatusBlocked(breaker.Status) {
		t.Fatalf("t_breaker status = %q (terminal=%v, blocked=%v), want status=\"gave_up\" terminal=true blocked=false",
			breaker.Status, isKanbanStatusTerminal(breaker.Status), isKanbanStatusBlocked(breaker.Status))
	}
	if gotOut := formatKanbanCardOutput(breaker); gotOut != "worker crashed 3 times" {
		t.Fatalf("formatKanbanCardOutput(t_breaker) = %q, want %q", gotOut, "worker crashed 3 times")
	}

	// 3. Unblocked card drops stale block_payload and surfaces c.Err.
	unblockedErr, ok := byID["t_unblocked_err"]
	if !ok {
		t.Fatal("wildcard query omitted t_unblocked_err")
	}
	if unblockedErr.BlockPayload != "" {
		t.Fatalf("t_unblocked_err BlockPayload = %q after unblocked event, want empty", unblockedErr.BlockPayload)
	}
	if gotOut := formatKanbanCardOutput(unblockedErr); gotOut != "post-unblock failure" {
		t.Fatalf("formatKanbanCardOutput(t_unblocked_err) = %q, want %q", gotOut, "post-unblock failure")
	}

	reassignedErr, ok := byID["t_reassigned_err"]
	if !ok {
		t.Fatal("wildcard query omitted t_reassigned_err")
	}
	if reassignedErr.BlockPayload != "" {
		t.Fatalf("t_reassigned_err BlockPayload = %q when status is failed, want empty", reassignedErr.BlockPayload)
	}
	if gotOut := formatKanbanCardOutput(reassignedErr); gotOut != "post-reassign failure" {
		t.Fatalf("formatKanbanCardOutput(t_reassigned_err) = %q, want %q", gotOut, "post-reassign failure")
	}

	// 4. Repeat block after unblock has a distinct delivery key and is not suppressed by the first block's delivery.
	reblock1 := byID["t_reblock"]
	if reblock1.BlockEventID != 30 {
		t.Fatalf("reblock1 BlockEventID = %d, want 30", reblock1.BlockEventID)
	}
	b := &Bridge{}
	b.markKanbanCardDeliveredForStatus(reblock1.ID, reblock1.Status, reblock1.BlockEventID)
	if !b.isKanbanCardDeliveredForStatus(reblock1.ID, reblock1.Status, reblock1.BlockEventID) {
		t.Fatal("expected first block event (id=30) to be marked delivered")
	}

	reblockScript := `
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("INSERT INTO task_events (id, task_id, kind, payload) VALUES (31, 't_reblock', 'unblocked', '{}')")
c.execute("INSERT INTO task_events (id, task_id, kind, payload) VALUES (32, 't_reblock', 'blocked', '{\"question\":\"Second question?\"}')")
c.commit()
`
	if out, err := exec.Command("python3", "-c", reblockScript, db).CombinedOutput(); err != nil {
		t.Fatalf("reblock script: %v\n%s", err, out)
	}
	rows2, err := querySessionKanbanCards(context.Background(), "python3", db, "*", since, nil)
	if err != nil {
		t.Fatalf("second wildcard query: %v", err)
	}
	var reblock2 kanbanCardRow
	for _, r := range rows2 {
		if r.ID == "t_reblock" {
			reblock2 = r
		}
	}
	if reblock2.BlockEventID != 32 {
		t.Fatalf("reblock2 BlockEventID = %d, want 32", reblock2.BlockEventID)
	}
	if b.isKanbanCardDeliveredForStatus(reblock2.ID, reblock2.Status, reblock2.BlockEventID) {
		t.Fatal("second block event (id=32) after unblock was treated as already delivered")
	}
	if gotOut := formatKanbanCardOutput(reblock2); gotOut != "Second question?" {
		t.Fatalf("formatKanbanCardOutput(reblock2) = %q, want %q", gotOut, "Second question?")
	}
}

func TestWaitForKanbanCards_FollowUpTurnFilingOwnCardDoesNotInvertCarriedOver(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH; the kanban fixture needs it")
	}
	since := time.Now().Unix() - 10
	db := filepath.Join(t.TempDir(), "kanban.db")
	initScript := `
import sqlite3, sys
db, since = sys.argv[1], int(sys.argv[2])
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, creator_session_id TEXT,
  result TEXT, last_failure_error TEXT, created_at REAL, updated_at REAL, parent_id TEXT);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, error, session_id TEXT, verdict TEXT, metadata TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);
""")
c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
  ("t_turn2_short", "Cluster B check", "done", "cluster-diagnostics", "a2a-ctx-turn2-short", "Cluster B healthy.", "", since + 1, since + 1, None))
c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
  ("t_turn2_sub", "Cluster C check", "done", "cluster-diagnostics", "a2a-ctx-turn2-sub", "Raw card output that should not be appended.", "", since + 2, since + 2, None))
c.commit()
`
	if out, err := exec.Command("python3", "-c", initScript, db, strconv.FormatInt(since, 10)).CombinedOutput(); err != nil {
		t.Fatalf("init turn2 db: %v\n%s", err, out)
	}

	_, url := startServer(t)
	stub := newAPIStub(t, nil)
	b := startAPIBridge(t, url, stub, nil)
	t.Setenv(hermesKanbanDBEnv, db)

	// Subcase 1: Turn 2 files its own card (cardsFiled == true), already settled on first poll (sawUnsettled == false),
	// with a short acknowledgement (< cardAnswerSubstantiveChars). The short ack must be replaced by card output, NOT prepended.
	sessionShort := "a2a-ctx-turn2-short"
	runShort := &taskRun{
		origin: &lib.Envelope{TaskID: "task-turn2-short", ContextID: "ctx-turn2-short", CorrelationID: "corr-turn2-short"},
		state:  stateRunning,
	}
	actShort := newSessionActivityState(sessionShort, false)
	actShort.cardsFiled.Store(true)
	runShort.act.Store(actShort)

	gotShort, ok := b.waitForKanbanCards(runShort, context.Background(), actShort, sessionShort, since, 2, "Dispatched card; here is the summary.")
	if !ok {
		t.Fatal("waitForKanbanCards (short ack on turn 2) returned ok = false, want true")
	}
	if gotShort != "Cluster B healthy." {
		t.Fatalf("waitForKanbanCards (short ack on turn 2) = %q, want %q (short ack should be replaced, not prepended as carriedOver)", gotShort, "Cluster B healthy.")
	}
	if actShort.cardsFiled.Load() {
		t.Fatal("expected cardsFiled to be cleared after waitForKanbanCards settled all cards")
	}

	// Subcase 2: Turn 2 files its own card (cardsFiled == true), already settled on first poll (sawUnsettled == false),
	// with a substantive (>= cardAnswerSubstantiveChars) synthesis. It must be returned directly without appending raw card output.
	sessionSub := "a2a-ctx-turn2-sub"
	runSub := &taskRun{
		origin: &lib.Envelope{TaskID: "task-turn2-sub", ContextID: "ctx-turn2-sub", CorrelationID: "corr-turn2-sub"},
		state:  stateRunning,
	}
	actSub := newSessionActivityState(sessionSub, false)
	actSub.cardsFiled.Store(true)
	runSub.act.Store(actSub)

	substantiveAck := strings.Repeat("Detailed synthesis of Cluster C status and findings. ", 15)
	if len(strings.TrimSpace(substantiveAck)) < cardAnswerSubstantiveChars {
		t.Fatalf("substantiveAck length = %d, must be >= %d", len(strings.TrimSpace(substantiveAck)), cardAnswerSubstantiveChars)
	}
	gotSub, ok := b.waitForKanbanCards(runSub, context.Background(), actSub, sessionSub, since, 2, substantiveAck)
	if !ok {
		t.Fatal("waitForKanbanCards (substantive ack on turn 2) returned ok = false, want true")
	}
	if gotSub != substantiveAck {
		t.Fatalf("waitForKanbanCards (substantive ack on turn 2) = %q, want substantiveAck returned directly without appending raw card output", gotSub)
	}
	if actSub.cardsFiled.Load() {
		t.Fatal("expected cardsFiled to be cleared after waitForKanbanCards settled all cards")
	}
}
