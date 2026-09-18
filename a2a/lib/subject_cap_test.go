package lib

// The per-subject message limit TASKS carries, from the reader's side.
//
// The deployment gives TASKS --max-msgs-per-subject with --discard=old, which
// stops one runaway task from evicting every other session's history. The
// price is paid on the runaway's own subject and it is paid at the head: the
// OLDEST message goes first, and the oldest event on a task's ...events
// subject is the `submitted` status-update assertion 9 requires. These tests
// pin what that costs a replay, and that the fold says so rather than handing
// back a short history indistinguishable from a real one.

import (
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// provisionCappedTasksStream creates TASKS the way the deployment does when it
// bounds one subject's history: limits retention, discard old, and a
// per-subject cap. Two, rather than the deployment's four thousand, so the
// limit can actually be reached in a test.
func provisionCappedTasksStream(t *testing.T, url string, perSubject int64) {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:              TasksStream,
		Subjects:          []string{"a2a.tasks.>"},
		Retention:         jetstream.LimitsPolicy,
		Discard:           jetstream.DiscardOld,
		MaxAge:            72 * time.Hour,
		MaxMsgsPerSubject: perSubject,
	}); err != nil {
		t.Fatalf("create capped TASKS stream: %v", err)
	}
}

// The limit-reached case, end to end against a real server: three events onto
// a subject that holds two, then a replay.
//
// What must be true is deliberately both halves. The terminal survives — the
// cap evicts from the head, so tasks/get still answers with the state the task
// actually reached, which is the answer that matters most. And the head is
// gone — the history no longer opens at `submitted`, and SubmittedMissing says
// so, so a caller can tell a truncated replay from a complete one.
func TestAPerSubjectCapEvictsTheHeadAndTheFoldReportsIt(t *testing.T) {
	s := startServer(t)
	provisionCappedTasksStream(t, clientURL(s), 2)

	replayFixture(t, clientURL(s), "task-cap", []TaskState{StateSubmitted, StateWorking, StateCompleted})
	ctx := testCtx(t)

	// One replay, read two ways. The struct below and the log line further
	// down are two reports of the same TasksGet, so a client whose logger the
	// test can read serves both -- and the two halves cannot drift apart into
	// describing different replays.
	c, logs := replayLog(t, clientURL(s))
	task, err := c.TasksGet(ctx, replayAddressee("task-cap"), "task-cap")
	if err != nil {
		t.Fatalf("TasksGet: %v", err)
	}
	if task.State != StateCompleted || !task.Final {
		t.Errorf("folded %s final=%v, want the terminal to survive the eviction", task.State, task.Final)
	}
	if len(task.StatusHistory) != 2 {
		t.Fatalf("history %v, want the two the cap left", task.StatusHistory)
	}
	if task.StatusHistory[0] != StateWorking {
		t.Errorf("history opens at %s, want the cap to have evicted submitted from the head", task.StatusHistory[0])
	}
	if !task.SubmittedMissing {
		t.Error("SubmittedMissing is false on a replay whose head the cap evicted; a truncated history must not read like a complete one")
	}
	if task.PostFinalDropped != 0 {
		t.Errorf("PostFinalDropped = %d, want 0: eviction is not a post-final write", task.PostFinalDropped)
	}

	// The field on its own is not the deliverable. Nothing outside this
	// package reads it, so a truncated replay is only observable if the
	// replay path SAYS so — the same place PostFinalDropped is said.
	line := logs.String()
	if !strings.Contains(line, "a2a task replayed without its submitted event") {
		t.Errorf("the replay of a truncated task logged nothing; the eviction is invisible to anyone not reading the struct\ngot:\n%s", line)
	}
	if !strings.Contains(line, "task-cap") || !strings.Contains(line, "opensAt=working") {
		t.Errorf("the warning does not say which task or what it opens at, which is what makes it actionable\ngot:\n%s", line)
	}
}

// replayLog is a client whose log lines a test can read.
func replayLog(t *testing.T, url string) (*Client, *logCapture) {
	t.Helper()
	logs := &logCapture{}
	c, err := Connect(testCtx(t), url,
		WithName("replay-log-test"),
		WithLogger(slog.New(logs)))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	t.Cleanup(c.Close)
	return c, logs
}

// The control the test above needs to mean anything: the same three events on
// an uncapped stream replay whole, and SubmittedMissing stays false. Without
// this, a SubmittedMissing that was true unconditionally would pass.
func TestAnUncappedReplayKeepsItsHead(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))

	replayFixture(t, clientURL(s), "task-nocap", []TaskState{StateSubmitted, StateWorking, StateCompleted})
	ctx := testCtx(t)

	c, logs := replayLog(t, clientURL(s))
	task, err := c.TasksGet(ctx, replayAddressee("task-nocap"), "task-nocap")
	if err != nil {
		t.Fatalf("TasksGet: %v", err)
	}
	if len(task.StatusHistory) != 3 || task.StatusHistory[0] != StateSubmitted {
		t.Fatalf("history %v, want all three opening at submitted", task.StatusHistory)
	}
	if task.SubmittedMissing {
		t.Error("SubmittedMissing is true on a complete replay")
	}

	// And the warning is conditional. A line that fires on every replay
	// is noise an operator learns to filter, which costs the truncated
	// case the only thing that makes it visible.
	if strings.Contains(logs.String(), "replayed without its submitted event") {
		t.Errorf("a complete replay warned anyway:\n%s", logs.String())
	}
}

// The two edges the field has to get right on its own, without a server.
func TestSubmittedMissingEdges(t *testing.T) {
	statusEnv := func(state TaskState, final bool) *Envelope {
		t.Helper()
		payload, err := json.Marshal(StatusUpdate{
			TaskID: "task-e", ContextID: "ctx-e",
			Status: TaskStatus{State: state},
			Final:  final,
		})
		if err != nil {
			t.Fatal(err)
		}
		env, err := NewStatusUpdateEnvelope(Party{Session: "worker-task-e"},
			"task-e", "ctx-e", "corr-e", payload)
		if err != nil {
			t.Fatal(err)
		}
		return env
	}

	// Nothing on the stream is not a missing head. A task with no events is
	// a case the caller can already see, and reporting a truncation there
	// would be a second, wrong, explanation for it.
	empty, err := FoldTask("task-e", nil)
	if err != nil {
		t.Fatalf("FoldTask on no events: %v", err)
	}
	if empty.SubmittedMissing {
		t.Error("SubmittedMissing is true on an empty fold")
	}

	// A history that opens at submitted is whole even when more follows.
	whole, err := FoldTask("task-e", []*Envelope{
		statusEnv(StateSubmitted, false),
		statusEnv(StateCompleted, true),
	})
	if err != nil {
		t.Fatalf("FoldTask: %v", err)
	}
	if whole.SubmittedMissing {
		t.Error("SubmittedMissing is true on a history that opens at submitted")
	}

	// And one that opens anywhere else is not.
	cut, err := FoldTask("task-e", []*Envelope{
		statusEnv(StateWorking, false),
		statusEnv(StateCompleted, true),
	})
	if err != nil {
		t.Fatalf("FoldTask: %v", err)
	}
	if !cut.SubmittedMissing {
		t.Error("SubmittedMissing is false on a history that opens at working")
	}
}
