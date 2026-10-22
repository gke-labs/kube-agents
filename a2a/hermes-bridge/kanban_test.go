package hermesbridge

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
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
  ("t_blocked", "Approve rollout",                  "blocked", "release",             "a2a-ctx-1", "",                 "", since + 13, None),
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

func TestKanbanDBScript_CancelSettlesOpenCardsAndSignalsWorkspaces(t *testing.T) {
	since := time.Now().Unix() - 3600
	db := kanbanFixtureDB(t, since)
	ctx := context.Background()

	// A recording stand-in for pkill, so the test asserts which workspaces the cancel signals
	// without signaling anything on this machine.
	binDir := t.TempDir()
	log := filepath.Join(binDir, "pkill.log")
	stub := "#!/bin/sh\necho \"$@\" >> " + log + "\nexit 1\n"
	if err := os.WriteFile(filepath.Join(binDir, "pkill"), []byte(stub), 0o755); err != nil {
		t.Fatal(err)
	}
	cmd := exec.CommandContext(ctx, "python3", "-c", kanbanDBPyScript, "cancel", db, "a2a-ctx-1")
	cmd.Env = append(os.Environ(), "PATH="+binDir+string(os.PathListSeparator)+os.Getenv("PATH"))
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("cancel script: %v\n%s", err, out)
	}

	rows, err := querySessionKanbanCards(ctx, "python3", db, "a2a-ctx-1", since-1000, []string{"t_child"})
	if err != nil {
		t.Fatal(err)
	}
	got := kanbanRowsByID(rows)
	for _, id := range []string{"t_running", "t_blocked"} {
		if got[id].Status != "canceled" || got[id].Err != "Canceled by user (A2A task stopped)" {
			t.Errorf("%s after cancel = status %q err %q; want canceled with the user-facing reason", id, got[id].Status, got[id].Err)
		}
	}
	for _, id := range []string{"t_done", "t_failed", "t_old"} {
		if got[id].Status == "canceled" {
			t.Errorf("%s was already settled and must not be re-marked canceled", id)
		}
	}
	// The child card of a canceled card is canceled too, even though a worker session owns it.
	wild, err := querySessionKanbanCards(ctx, "python3", db, "*", since-1000, nil)
	if err != nil {
		t.Fatal(err)
	}
	all := kanbanRowsByID(wild)
	if other := all["t_other"]; other.Status != "done" {
		t.Errorf("another session's card was touched by the cancel: %+v", other)
	}
	signaled, _ := os.ReadFile(log)
	for _, id := range []string{"t_running", "t_blocked", "t_child"} {
		if !strings.Contains(string(signaled), "/kanban/workspaces/"+id) {
			t.Errorf("cancel did not signal the workspace of %s; pkill calls:\n%s", id, signaled)
		}
	}
	if strings.Contains(string(signaled), "/kanban/workspaces/t_done") {
		t.Errorf("cancel signaled a settled card's workspace; pkill calls:\n%s", signaled)
	}
}
