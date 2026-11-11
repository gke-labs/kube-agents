package hermesbridge

import (
	"context"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// cardWaitTurn is a console or A2A door turn holding for the card it filed,
// on a bridge whose session-kv is store, ready to be canceled.
type cardWaitTurn struct {
	c         *lib.Client
	b         *Bridge
	stop      context.CancelFunc
	store     *stopStore
	taskID    string
	sessionID string
	origin    *lib.Envelope
}

// startCardWaitTurn submits a turn that files one running card and holds for
// it, as #5529's mid-wait cancel test does, and returns once it is holding.
func startCardWaitTurn(t *testing.T, name, conversation string, store *stopStore, mutate func(*Config)) *cardWaitTurn {
	t.Helper()
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH; the kanban fixture needs it")
	}
	t.Setenv(slackUXEnv, "")
	const shared = "pod-wide-activity-key"
	contextID := "ctx-report-" + name
	sessionID := apiSessionID(contextID)
	db := filepath.Join(t.TempDir(), "kanban.db")
	initScript := `
import sqlite3, sys, time
db, sess = sys.argv[1], sys.argv[2]
now = time.time()
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, creator_session_id TEXT,
  result TEXT, last_failure_error TEXT, created_at REAL, updated_at REAL, parent_id TEXT);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, error TEXT, session_id TEXT, verdict TEXT, metadata TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);
""")
c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
  ("t_stop1", "Scale checkout-gateway", "running", "platform-agent", sess, "", "", now, now, None))
c.commit()
`
	if out, err := exec.Command("python3", "-c", initScript, db, sessionID).CombinedOutput(); err != nil {
		t.Fatalf("init db: %v\n%s", err, out)
	}
	t.Setenv(hermesKanbanDBEnv, db)
	_, url := startServer(t)
	var door atomic.Value
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		u := door.Load().(string)
		postDelivery(t, u, shared, c.sessionID, hookPreToolCall, kanbanCreateTool, "call_1")
		postDelivery(t, u, shared, c.sessionID, hookPostToolCall, kanbanCreateTool, "call_1")
		writeCompletion(w, c.sessionID, "Filed a card to scale checkout-gateway.")
	})
	b, stop := startAPIBridgeHooked(t, url, stub, func(cfg *Config) {
		cfg.ActivityListen = "127.0.0.1:0"
		cfg.ActivitySecret = shared
		withStore(store)(cfg)
		if mutate != nil {
			mutate(cfg)
		}
	}, nil, func(net.Addr) bool { return true })
	door.Store(b.ActivityURL())
	c := gatewayClient(t, url)
	taskID := "task-report-" + name
	origin := submitFromEnv(t, c, taskID, contextID, conversation, "scale checkout-gateway to 4")
	waitFor(t, 10*time.Second, "the turn to hold for its card", func() bool {
		return b.hasActiveKanbanWait(sessionID)
	})
	return &cardWaitTurn{c: c, b: b, stop: stop, store: store, taskID: taskID, sessionID: sessionID, origin: origin}
}

// cardStops is the card-stop POSTs session-kv has seen for the turn's
// session: the store also records the bridge's route writes.
func (w *cardWaitTurn) cardStops() int {
	want := "POST " + strings.Replace(cardStopPathFormat, "%s", w.sessionID, 1)
	n := 0
	for _, p := range w.store.seen() {
		if p.path == want {
			n++
		}
	}
	return n
}

// awaitCardStop waits for the turn's first card-stop POST.
func (w *cardWaitTurn) awaitCardStop(t *testing.T) {
	t.Helper()
	waitFor(t, 10*time.Second, "the cancel's card stop at session-kv", func() bool { return w.cardStops() > 0 })
}

// cancel sends the gateway's KindCancel for the turn's task.
func (w *cardWaitTurn) cancel(t *testing.T) {
	t.Helper()
	env, err := lib.NewCancelEnvelope(gatewayParty, w.origin.TaskID, w.origin.ContextID,
		w.origin.CorrelationID, lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := w.c.Publish(testCtx(t), lib.TaskInSubject("platform", w.taskID), env); err != nil {
		t.Fatal(err)
	}
}

// A cancel during the in-turn card wait holds the canceled terminal until
// session-kv answers the card stop, then carries the answer: the text and the
// cardStop part the card-stop control's answer carries (#5528). A2A terminals
// are final, so a result after "canceled" would reach no one. One POST goes
// out for the run, though both the wait and runTaskAPI defer a card stop.
func TestAPI_ACancelDuringTheCardWaitReportsWhetherTheCardsStopped(t *testing.T) {
	for _, tc := range []struct{ name, conversation string }{
		{"console", "console:tab-report"},
		{"a2a-door", "a2a:caller-1:ctx-report"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := newStopStore(t, http.StatusOK, stopAnswer)
			release := store.holdAnswers(t)
			w := startCardWaitTurn(t, tc.name, tc.conversation, store, nil)
			w.cancel(t)

			w.awaitCardStop(t)
			// Held: the answer isn't in yet, so the task isn't final.
			time.Sleep(500 * time.Millisecond)
			if held, err := w.c.TasksGet(testCtx(t), "platform", w.taskID); err != nil {
				t.Fatalf("read the held task: %v", err)
			} else if held.Final {
				t.Fatalf("the canceled terminal went out before session-kv answered: %s", held.State)
			}

			release()
			task := waitTerminalFor(t, w.c, w.taskID, 10*time.Second)
			if task.State != lib.StateCanceled {
				t.Fatalf("state = %s, want canceled", task.State)
			}
			stop, text := resultCardStop(t, task)
			if !strings.HasPrefix(text, "canceled; stopped 2 cards") {
				t.Errorf("result text = %q, want it to start %q", text, "canceled; stopped 2 cards")
			}
			if got := len(stop.CardStopResult.Stopped); got != 2 {
				t.Errorf("cardStop part names %d stopped cards, want 2: %+v", got, stop.CardStopResult)
			}
			// The two deferred card stops find the run's stop already sent.
			time.Sleep(time.Second)
			if n := w.cardStops(); n != 1 {
				t.Errorf("card stop POSTs = %d, want 1 for the run", n)
			}
		})
	}
}

// A card stop session-kv never answers ends the hold at CardStopTimeout, and
// the terminal says the stop was not confirmed rather than reporting nothing.
func TestAPI_ACancelsCardStopThatTimesOutSaysItWasNotConfirmed(t *testing.T) {
	store := newStopStore(t, http.StatusOK, stopAnswer)
	_ = store.holdAnswers(t)
	w := startCardWaitTurn(t, "timeout", "a2a:caller-1:ctx-timeout", store, func(cfg *Config) {
		cfg.CardStopTimeout = time.Second
	})
	w.cancel(t)
	task := waitTerminalFor(t, w.c, w.taskID, 10*time.Second)
	if task.State != lib.StateCanceled {
		t.Fatalf("state = %s, want canceled", task.State)
	}
	a := task.Artifact(lib.ArtifactResult)
	if a == nil {
		t.Fatal("no result artifact")
	}
	if text := joinTexts(a.Parts); text != "canceled; the card stop was not confirmed (session-kv did not answer in 1s)" {
		t.Errorf("result text = %q", text)
	}
	if _, ok := lib.CardStopOf(a.Parts); ok {
		t.Errorf("an unconfirmed stop carries a cardStop part: %s", mustMarshal(t, a.Parts))
	}
}

// A shutdown during the hold ends it at once: the terminal is canceled and
// says the stop was not confirmed because the bridge is shutting down, well
// inside the pod's termination grace.
func TestAPI_AShutdownDuringTheCancelsHoldEndsItAtOnce(t *testing.T) {
	store := newStopStore(t, http.StatusOK, stopAnswer)
	_ = store.holdAnswers(t)
	w := startCardWaitTurn(t, "shutdown", "console:tab-shutdown", store, nil)
	w.cancel(t)
	w.awaitCardStop(t)
	start := time.Now()
	w.stop()
	task := waitTerminalFor(t, w.c, w.taskID, 10*time.Second)
	if took := time.Since(start); took > 5*time.Second {
		t.Errorf("the hold outlived the shutdown by %s", took)
	}
	if task.State != lib.StateCanceled {
		t.Fatalf("state = %s, want canceled", task.State)
	}
	a := task.Artifact(lib.ArtifactResult)
	if a == nil {
		t.Fatal("no result artifact")
	}
	if text := joinTexts(a.Parts); text != "canceled; the card stop was not confirmed (the bridge is shutting down)" {
		t.Errorf("result text = %q", text)
	}
}

// The cancel's card stop is deduped per run, not per session: a second run on
// the same session, canceled later, posts its own stop, so cards a later turn
// filed before reaching its wait are stopped too. One run posts once however
// many paths ask.
func TestMarkRunCanceled_PostsOncePerRunNotOncePerSession(t *testing.T) {
	store := newStopStore(t, http.StatusOK, stopAnswer)
	b := &Bridge{
		cfg: Config{
			RouteURL:        store.srv.URL,
			RouteKey:        "kv-key",
			CardStopTimeout: 5 * time.Second,
			Logger:          slog.New(slog.NewTextHandler(io.Discard, nil)),
		},
		routeClient: store.srv.Client(),
	}
	first, second := &taskRun{}, &taskRun{}
	b.markRunCanceled(first, "a2a-ctx-1")
	b.markRunCanceled(first, "a2a-ctx-1")
	waitFor(t, 5*time.Second, "the first run's stop", func() bool { return len(store.seen()) == 1 })
	b.markRunCanceled(second, "a2a-ctx-1")
	waitFor(t, 5*time.Second, "the second run's stop", func() bool { return len(store.seen()) == 2 })
	time.Sleep(300 * time.Millisecond)
	if n := len(store.seen()); n != 2 {
		t.Errorf("card stop POSTs = %d, want one per run (2)", n)
	}
}
