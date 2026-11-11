package hermesbridge

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
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

// A cancel's own card stop (markSessionCanceled) reads session-kv's answer
// and logs it, as the card-stop control does (#5407): a card stopped
// mid-claim, an archive still running at the deadline, a card it could not
// stop, and a refusal all reach the log rather than being dropped.
func TestCancelSessionCards_LogsSessionKVsAnswer(t *testing.T) {
	for _, tc := range []struct {
		name   string
		status int
		body   string
		want   []string
	}{
		{
			name:   "caveats",
			status: http.StatusOK,
			body: `{"session_id":"a2a-ctx-1","stopped":[{"id":"t_a","title":"A","prior_status":"running"}],` +
				`"failed":[{"id":"t_f","title":"F","error":"board locked"}],` +
				`"pending":[{"id":"t_p","title":"P"}],"unresolved":[{"id":"t_u","title":"U"}]}`,
			want: []string{
				`msg="cancel's cards stopped"`, "stopped=1", "failed=1", "pending=1", "unresolved=1",
				`msg="card not stopped"`, "card=t_f", `err="board locked"`,
				`msg="card's archive still running at session-kv's deadline"`, "card=t_p",
				`msg="card stopped mid-claim; a worker may still start for it"`, "card=t_u",
			},
		},
		{
			name:   "refused",
			status: http.StatusServiceUnavailable,
			body:   `{"detail":"board unavailable"}`,
			want:   []string{`msg="cancel's card stop refused by session-kv"`, "status=503", "board unavailable"},
		},
		{
			name:   "unreadable",
			status: http.StatusOK,
			body:   `not json`,
			want:   []string{`msg="cancel's card stop: session-kv's answer is unreadable"`},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			done := make(chan struct{})
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer srv.Close()
			var logs lockedBuffer
			b := &Bridge{
				cfg: Config{
					RouteURL:        srv.URL,
					RouteKey:        "session-kv-token",
					CardStopTimeout: 5 * time.Second,
					Logger:          slog.New(slog.NewTextHandler(&logs, nil)),
				},
				routeClient: srv.Client(),
			}
			go func() { b.stopSessionCards("a2a-ctx-1"); close(done) }()
			select {
			case <-done:
			case <-time.After(5 * time.Second):
				t.Fatal("stopSessionCards did not return")
			}
			got := logs.String()
			for _, w := range tc.want {
				if !strings.Contains(got, w) {
					t.Errorf("log lacks %q:\n%s", w, got)
				}
			}
		})
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
	// No publisher goroutine runs for a hand-built run: mark it joined, so
	// finalize's waitActivity does not sit out activityPublishTimeout.
	close(act.done)
	run.act.Store(act)

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

// A settled card's step names the card (id, title, status, verdict) and
// carries none of its report, and a verdict outside the known words (the
// worker's model writes it) falls back to the card's status: the report is text the card's worker wrote,
// which can repeat a value it read, and the step goes to TASKS.
func TestCardStepCarriesNoReportText(t *testing.T) {
	const canary = "tok_card_report_canary_8f3a"
	for _, c := range []kanbanCardRow{
		{ID: "t_1", Title: "Check prod-a", Status: "done", Result: "The authentication token, " + canary + ", expired; rotated it."},
		{ID: "t_2", Title: "Check prod-b", Status: "failed", Err: "psql: FATAL " + canary},
		{ID: "t_3", Title: "Check prod-c", Status: "blocked", BlockPayload: `{"question":"use ` + canary + `?"}`},
		{ID: "t_4", Title: "Check prod-d", Status: "done", Verdict: canary},
		{ID: "t_5", Title: "Check prod-e", Status: "done", Verdict: "healthy but the key is " + canary},
	} {
		s := cardStep(c, 1, 3, "task-parent")
		raw, err := json.Marshal(s)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(raw), canary) || s.Thought != "" || s.Subtask == nil || s.Subtask.Summary != "" {
			t.Errorf("card %s step carries report text: %s", c.ID, raw)
		}
		if s.Subtask == nil || s.Subtask.ID != c.ID || s.Subtask.Title != c.Title || s.Subtask.Status != c.Status || s.Subtask.ParentTaskID != "task-parent" {
			t.Errorf("card %s step lost its identity: %+v", c.ID, s.Subtask)
		}
		if s.Subtask != nil && c.Verdict != "" && s.Subtask.Verdict != c.Status {
			t.Errorf("card %s free-text verdict %q became %q, want the card's status %q", c.ID, c.Verdict, s.Subtask.Verdict, c.Status)
		}
	}
}

// A known verdict word passes through, lower-cased.
func TestAKnownCardVerdictPassesThrough(t *testing.T) {
	if got := cardStep(kanbanCardRow{ID: "t_1", Title: "x", Status: "done", Verdict: " Degraded "}, 1, 1, "").Subtask.Verdict; got != "degraded" {
		t.Errorf("verdict = %q, want degraded", got)
	}
}

// The delivered-card record the in-turn wait reads to skip a settled card it
// already folded is bounded both by age and by count: a record past the
// retention window is forgotten, the oldest past maxTrackedKanbanEntries are
// evicted one by one, and every recent one is kept.
func TestDeliveredCardsAreBoundedByAgeAndCount(t *testing.T) {
	b := &Bridge{}
	baseNow := time.Now()
	prevNow := kanbanNow
	var offsetSecs atomic.Int64
	kanbanNow = func() time.Time { return baseNow.Add(time.Duration(offsetSecs.Load()) * time.Second) }
	t.Cleanup(func() { kanbanNow = prevNow })

	offsetSecs.Store(-int64(kanbanTrackedRetentionSecs + 100))
	b.markKanbanCardDeliveredForStatus("t_expired", "done")
	offsetSecs.Store(0)
	for i := 0; i < maxTrackedKanbanEntries+100; i++ {
		offsetSecs.Add(1)
		b.markKanbanCardDeliveredForStatus("t_card_"+strconv.Itoa(10000+i), "done")
	}
	if b.isKanbanCardDeliveredForStatus("t_expired", "done") {
		t.Error("a delivered card past the retention window is still recorded")
	}
	if b.isKanbanCardDeliveredForStatus("t_card_10000", "done") {
		t.Error("the oldest delivered card past maxTrackedKanbanEntries is still recorded")
	}
	for i := 100; i < maxTrackedKanbanEntries+100; i++ {
		if id := "t_card_" + strconv.Itoa(10000+i); !b.isKanbanCardDeliveredForStatus(id, "done") {
			t.Fatalf("recent delivered card %s was forgotten", id)
		}
	}
}

// A card's title is model-written, so it passes the scrubber before it names
// the step and the subtask.
func TestCardStepScrubsTheTitle(t *testing.T) {
	const secret = "hunter2xyzzy-title"
	s := cardStep(kanbanCardRow{ID: "t_1", Title: "Rotate creds password=" + secret, Status: "done"}, 1, 1, "")
	raw, _ := json.Marshal(s)
	if strings.Contains(string(raw), secret) {
		t.Errorf("a card title's secret reached the step: %s", raw)
	}
}

// What of a settled card stands in a task's answer with no synthesis: a
// completed card's report, a blocked card's question, and for a failed card a
// line naming it and its status, its error text only with the debug switch.
func TestCardAnswerTextKeepsErrorTextBehindTheDebugSwitch(t *testing.T) {
	// The off half holds the default, whatever the shell running it exports.
	t.Setenv(debugErrorTextEnv, "")
	const errSecret = "tok_card_error_text_canary"
	done := kanbanCardRow{ID: "t_1", Title: "Check prod-a", Status: "done", Result: "prod-a is healthy"}
	failed := kanbanCardRow{ID: "t_2", Title: "Check prod-b", Status: "failed", Err: "psql: FATAL " + errSecret}
	blocked := kanbanCardRow{ID: "t_3", Title: "Check prod-c", Status: "blocked", BlockPayload: `{"question":"May I restart prod-c?"}`}
	if got := cardAnswerText(done); got != "prod-a is healthy" {
		t.Errorf("completed card answer = %q, want its report", got)
	}
	if got := cardAnswerText(blocked); got != "May I restart prod-c?" {
		t.Errorf("blocked card answer = %q, want its question", got)
	}
	got := cardAnswerText(failed)
	if strings.Contains(got, errSecret) || !strings.Contains(got, "t_2") || !strings.Contains(got, "failed") {
		t.Errorf("failed card answer = %q, want a status line without the error text", got)
	}
	if got := cardAnswerText(kanbanCardRow{ID: "t_4", Title: "x", Status: "done", Err: errSecret}); strings.Contains(got, errSecret) {
		t.Errorf("a completed card with no report answered with its error text: %q", got)
	}
	// A blocked card with no question in its payload answers with a status
	// line, never its error text.
	for _, c := range []kanbanCardRow{
		{ID: "t_5", Title: "x", Status: "blocked", Err: errSecret},
		{ID: "t_6", Title: "x", Status: "blocked", BlockPayload: `{"error":"` + errSecret + `"}`, Err: errSecret},
	} {
		if got := cardAnswerText(c); strings.Contains(got, errSecret) || !strings.Contains(got, c.ID) {
			t.Errorf("blocked card %s answer = %q, want a status line without the error text", c.ID, got)
		}
	}
	// The status line's title is scrubbed.
	if got := cardAnswerText(kanbanCardRow{ID: "t_7", Title: "Rotate password=hunter2xyzzy-status", Status: "failed"}); strings.Contains(got, "hunter2xyzzy-status") {
		t.Errorf("status line carries the title's secret: %q", got)
	}
	t.Setenv(debugErrorTextEnv, "true")
	if got := cardAnswerText(failed); !strings.Contains(got, "psql: FATAL") {
		t.Errorf("failed card answer with the debug switch on = %q, want its error text", got)
	}
	// Under the switch, as on an activity entry: the error's first line,
	// scrubbed and capped, ahead of the worker's run summary.
	loud := kanbanCardRow{ID: "t_8", Title: "x", Status: "failed", Result: "worker summary " + errSecret,
		Err: "psql: FATAL password=hunter2loud " + strings.Repeat("y", 2*debugErrorTextCap) + "\nsecond line"}
	got = cardAnswerText(loud)
	if strings.Contains(got, "hunter2loud") || strings.Contains(got, "second line") || strings.Contains(got, errSecret) ||
		len(got) > debugErrorTextCap || !strings.Contains(got, "psql: FATAL") {
		t.Errorf("failed card answer with the debug switch on = %q (%d bytes), want the error's first line, scrubbed, within %d bytes", got, len(got), debugErrorTextCap)
	}
}

// A settled card's step names its own end, and the progress line counts
// settled cards, not completed ones.
func TestACardStepNamesItsEnd(t *testing.T) {
	for status, want := range map[string]string{"done": "completed", "blocked": "blocked", "failed": "failed", "gave_up": "failed", "canceled": "canceled"} {
		st := cardStep(kanbanCardRow{ID: "t_1", Title: "Check prod-a", Status: status}, 1, 2, "")
		if want := "Specialist check " + want + ": Check prod-a."; st.Title != want {
			t.Errorf("%s card step title = %q, want %q", status, st.Title, want)
		}
	}
}

// The failed read's error carries the script's reason, not just its exit.
func TestAFailedBoardReadNamesItsCause(t *testing.T) {
	pythonBin := resolvePythonBin()
	if _, err := exec.LookPath(pythonBin); err != nil {
		t.Skip("python3 not on PATH")
	}
	db := filepath.Join(t.TempDir(), "kanban.db")
	if err := os.WriteFile(db, []byte("not a sqlite database"), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := querySessionKanbanCards(context.Background(), pythonBin, db, "a2a-ctx-1", 0, nil)
	if err == nil || !strings.Contains(err.Error(), "not a database") {
		t.Fatalf("err = %v, want the script's reason (file is not a database)", err)
	}
}

// waitOnRunningCard starts waitForKanbanCards on a session with a card that
// keeps running, and returns its result channel.
func waitOnRunningCard(t *testing.T, b *Bridge, taskCtx context.Context, since int64, taskID string) (*taskRun, <-chan [2]any) {
	t.Helper()
	sessionID := "a2a-ctx-1"
	// answerHeld as apiTurn leaves it on a response.
	run := &taskRun{origin: &lib.Envelope{TaskID: taskID, ContextID: "ctx-1", CorrelationID: "corr-" + taskID}, state: stateRunning, answerHeld: true}
	act := newSessionActivityState(sessionID, false)
	act.cardsFiled.Store(true)
	close(act.done)
	run.act.Store(act)
	out := make(chan [2]any, 1)
	go func() {
		got, ok := b.waitForKanbanCards(run, taskCtx, act, sessionID, since, 1, "The coordinator's finished answer.")
		out <- [2]any{got, ok}
	}()
	return run, out
}

// The task's deadline ends a wait on cards with the turn's finished answer,
// not a failure: the coordinator's work is done.
func TestTheDeadlineEndsACardWaitWithTheTurnsAnswer(t *testing.T) {
	since := time.Now().Unix() - 60
	t.Setenv(hermesKanbanDBEnv, kanbanFixtureDB(t, since))
	_, url := startServer(t)
	b := startAPIBridge(t, url, newAPIStub(t, nil), nil)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, out := waitOnRunningCard(t, b, ctx, since, "task-deadline-wait")
	select {
	case r := <-out:
		if r[0] != "The coordinator's finished answer." || r[1] != true {
			t.Fatalf("waitForKanbanCards = (%q, %v), want the turn's answer", r[0], r[1])
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the wait did not end at the deadline")
	}
}

// A shutdown ends a wait on cards with the turn's finished answer: the
// answer stays held, so shutdownTasks leaves the run to its worker.
func TestAShutdownEndsACardWaitWithTheTurnsAnswer(t *testing.T) {
	since := time.Now().Unix() - 60
	t.Setenv(hermesKanbanDBEnv, kanbanFixtureDB(t, since))
	_, url := startServer(t)
	b := startAPIBridge(t, url, newAPIStub(t, nil), nil)
	run, out := waitOnRunningCard(t, b, context.Background(), since, "task-shutdown-wait")
	waitFor(t, 5*time.Second, "the wait to start", func() bool { return b.hasActiveKanbanWait("a2a-ctx-1") })
	time.Sleep(1500 * time.Millisecond)
	run.mu.Lock()
	held := run.answerHeld
	run.mu.Unlock()
	if !held {
		t.Fatal("the wait released the held answer, so a shutdown would fail the task")
	}
	b.closing.Store(true)
	defer b.closing.Store(false)
	select {
	case r := <-out:
		if r[0] != "The coordinator's finished answer." || r[1] != true {
			t.Fatalf("waitForKanbanCards = (%q, %v), want the turn's answer", r[0], r[1])
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the wait did not end on the shutdown")
	}
}

// End to end through the in-turn wait: a turn's one card fails with an error
// that repeats a value, and the task's answer names the card and how it
// ended, without the error text (that is the debugging switch's to add).
func TestWaitForKanbanCards_AFailedCardsAnswerOmitsItsErrorText(t *testing.T) {
	t.Setenv(debugErrorTextEnv, "")
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH; the kanban fixture needs it")
	}
	const canary = "tok_failed_card_canary_51c2"
	since := time.Now().Unix() - 10
	db := filepath.Join(t.TempDir(), "kanban.db")
	initScript := `
import sqlite3, sys
db, since, canary = sys.argv[1], int(sys.argv[2]), sys.argv[3]
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, creator_session_id TEXT,
  result TEXT, last_failure_error TEXT, created_at REAL, updated_at REAL, parent_id TEXT);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, error TEXT, session_id TEXT, verdict TEXT, metadata TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);
""")
c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
  ("t_fail", "Check prod-b", "failed", "cluster-diagnostics", "a2a-ctx-fail", "", "psql: FATAL " + canary, since + 1, since + 2, None))
c.execute("INSERT INTO task_runs (task_id, summary, error, session_id) VALUES (?,?,?,?)", ("t_fail", "", "psql: FATAL " + canary, "w-1"))
c.commit()
`
	if out, err := exec.Command("python3", "-c", initScript, db, strconv.FormatInt(since, 10), canary).CombinedOutput(); err != nil {
		t.Fatalf("init db: %v\n%s", err, out)
	}
	t.Setenv(hermesKanbanDBEnv, db)
	_, url := startServer(t)
	b := startAPIBridge(t, url, newAPIStub(t, nil), nil)
	sessionID := "a2a-ctx-fail"
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-fail", ContextID: "ctx-fail", CorrelationID: "corr-fail"}, state: stateRunning}
	act := newSessionActivityState(sessionID, false)
	act.cardsFiled.Store(true)
	run.act.Store(act)

	got, ok := b.waitForKanbanCards(run, context.Background(), act, sessionID, since, 1, "Dispatched a card.")
	if !ok {
		t.Fatal("waitForKanbanCards returned ok = false")
	}
	if strings.Contains(got, canary) || !strings.Contains(got, "t_fail") || !strings.Contains(got, "failed") {
		t.Errorf("answer = %q, want a line naming t_fail and its failure, without the error text", got)
	}
}

// A kanban.db that can't be read ends the wait after a bounded run of failed
// polls, with the turn's own text, rather than holding the turn to its
// deadline.
func TestWaitForKanbanCards_AnUnreadableBoardEndsTheWait(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH; the kanban fixture needs it")
	}
	db := filepath.Join(t.TempDir(), "kanban.db")
	if err := os.WriteFile(db, []byte("not a sqlite database"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(hermesKanbanDBEnv, db)
	_, url := startServer(t)
	b := startAPIBridge(t, url, newAPIStub(t, nil), nil)
	sessionID := "a2a-ctx-corrupt"
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-corrupt", ContextID: "ctx-corrupt", CorrelationID: "corr-corrupt"}, state: stateRunning}
	act := newSessionActivityState(sessionID, false)
	act.cardsFiled.Store(true)
	run.act.Store(act)
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(kanbanMaxQueryFailures+30)*kanbanPollInterval)
	defer cancel()
	start := time.Now()
	got, ok := b.waitForKanbanCards(run, ctx, act, sessionID, time.Now().Unix()-10, 1, "Dispatched a card.")
	if !ok || got != "Dispatched a card." {
		t.Fatalf("waitForKanbanCards = (%q, %v), want the turn's own text", got, ok)
	}
	if ctx.Err() != nil {
		t.Errorf("the wait ran to the deadline (%s) instead of giving up on the unreadable board", time.Since(start))
	}
}

// A shutdown ends a synthesis turn in flight: the run's answer is held, so
// nothing else would, and the cards' joined reports stand in.
func TestASynthesisTurnEndsOnShutdown(t *testing.T) {
	reqArrived := make(chan struct{})
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		close(reqArrived)
		<-r.Context().Done()
	})
	b := &Bridge{cfg: Config{APIURL: stub.srv.URL + "/v1/chat/completions", APIKey: testAPIKey, APIModel: DefaultAPIModel}, apiClient: stub.srv.Client()}
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-synth-close", ContextID: "ctx-synth-close"}, state: stateRunning}
	cards := []kanbanCardRow{{ID: "t_1", Title: "Cluster 1", Status: "done", Result: "Nodes Ready"}, {ID: "t_2", Title: "Cluster 2", Status: "done", Result: "Pods Healthy"}}
	done := make(chan string, 1)
	go func() {
		done <- b.synthesizeCardAnswer(run, context.Background(), "a2a-ctx-synth-close", 1, "ack", cards, []string{"Nodes Ready", "Pods Healthy"})
	}()
	select {
	case <-reqArrived:
	case <-time.After(5 * time.Second):
		t.Fatal("the synthesis request never arrived")
	}
	b.closing.Store(true)
	select {
	case got := <-done:
		if !strings.Contains(got, "Nodes Ready") || !strings.Contains(got, "Pods Healthy") {
			t.Errorf("answer after shutdown = %q, want the joined reports", got)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("the synthesis turn did not end on the shutdown")
	}
}

// The task's deadline during the cards' synthesis keeps the answers in hand
// rather than failing the task.
func TestTheDeadlineDuringASynthesisKeepsTheAnswer(t *testing.T) {
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
for i, r in ((1, "Nodes Ready"), (2, "Pods Healthy")):
  c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
    ("t_s%d" % i, "Cluster %d" % i, "done", "cluster-diagnostics", "a2a-ctx-synth-dl", r, "", since + i, since + i + 1, None))
c.commit()
`
	if out, err := exec.Command("python3", "-c", initScript, db, strconv.FormatInt(since, 10)).CombinedOutput(); err != nil {
		t.Fatalf("init db: %v\n%s", err, out)
	}
	t.Setenv(hermesKanbanDBEnv, db)
	_, url := startServer(t)
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) { <-r.Context().Done() })
	b := startAPIBridge(t, url, stub, nil)
	sessionID := "a2a-ctx-synth-dl"
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-synth-dl", ContextID: "ctx-synth-dl", CorrelationID: "corr-synth-dl"}, state: stateRunning, answerHeld: true}
	act := newSessionActivityState(sessionID, false)
	act.cardsFiled.Store(true)
	close(act.done)
	run.act.Store(act)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	got, ok := b.waitForKanbanCards(run, ctx, act, sessionID, since, 1, "ok")
	if !ok || !strings.Contains(got, "Nodes Ready") || !strings.Contains(got, "Pods Healthy") {
		t.Fatalf("waitForKanbanCards = (%q, %v), want the cards' reports, not a failure", got, ok)
	}
}

// A synthesis step is announced only when a synthesis turn runs.
func TestASynthesisIsAnnouncedOnlyWhenOneRuns(t *testing.T) {
	one := []kanbanCardRow{{ID: "t_1"}}
	two := []kanbanCardRow{{ID: "t_1"}, {ID: "t_2"}}
	if cardSynthesisRuns(nil, 1, "", one) {
		t.Error("one card: a synthesis announced, but the card stands in as it is")
	}
	if !cardSynthesisRuns(nil, 1, "", two) {
		t.Error("two cards and no answer of the turn's own: no synthesis announced")
	}
	if cardSynthesisRuns(nil, 1, strings.Repeat("a full answer of the turn's own. ", 20), two) {
		t.Error("a substantive answer of the turn's own stands in: no synthesis runs")
	}
}

// staleCardDB holds one card on the session, created and last updated long
// before the task began and never settled: a worker that died mid-run.
func staleCardDB(t *testing.T, sessionID string, since int64) string {
	t.Helper()
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH; the kanban fixture needs it")
	}
	db := filepath.Join(t.TempDir(), "kanban.db")
	initScript := `
import sqlite3, sys
db, sess, since = sys.argv[1], sys.argv[2], int(sys.argv[3])
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, creator_session_id TEXT,
  result TEXT, last_failure_error TEXT, created_at REAL, updated_at REAL, parent_id TEXT);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, error TEXT, session_id TEXT, verdict TEXT, metadata TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);
""")
c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
  ("t_stale1", "A check whose worker died", "running", "cluster-diagnostics", sess, "", "", since - 86400, since - 86000, None))
c.commit()
`
	if out, err := exec.Command("python3", "-c", initScript, db, sessionID, strconv.FormatInt(since, 10)).CombinedOutput(); err != nil {
		t.Fatalf("init db: %v\n%s", err, out)
	}
	return db
}

// A session's poll reads the cards its task filed or tracks: an older card
// that never settled is not one of them.
func TestASessionPollSkipsAnOldUnsettledCard(t *testing.T) {
	since := time.Now().Unix() - 10
	db := staleCardDB(t, "a2a-ctx-stale", since)
	rows, err := querySessionKanbanCards(context.Background(), resolvePythonBin(), db, "a2a-ctx-stale", since, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 0 {
		t.Fatalf("rows = %+v, want none: the card predates the task and is not tracked", rows)
	}
	// A tracked card is still read, whatever its age.
	rows, err = querySessionKanbanCards(context.Background(), resolvePythonBin(), db, "a2a-ctx-stale", since, []string{"t_stale1"})
	if err != nil || len(rows) != 1 {
		t.Fatalf("tracked: rows = %+v, err = %v; want the tracked card", rows, err)
	}
}

// A turn on a session with an old card that never settled ends with its own
// answer instead of waiting on that card to the deadline.
func TestAnOldUnsettledCardDoesNotHoldALaterTurn(t *testing.T) {
	since := time.Now().Unix() - 10
	sessionID := "a2a-ctx-stale"
	t.Setenv(hermesKanbanDBEnv, staleCardDB(t, sessionID, since))
	_, url := startServer(t)
	b := startAPIBridge(t, url, newAPIStub(t, nil), nil)
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-stale", ContextID: "ctx-stale", CorrelationID: "corr-stale"}, state: stateRunning, answerHeld: true}
	act := newSessionActivityState(sessionID, false)
	close(act.done)
	run.act.Store(act)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	start := time.Now()
	got, ok := b.waitForKanbanCards(run, ctx, act, sessionID, since, 1, "Here is the answer.")
	if !ok || got != "Here is the answer." || time.Since(start) > 5*time.Second {
		t.Fatalf("waitForKanbanCards = (%q, %v) after %s, want the turn's own answer at once", got, ok, time.Since(start))
	}
}
