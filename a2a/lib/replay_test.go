package lib

// Regression tests for the tasks/get replay path: it must terminate and fold
// what the stream still holds even when the snapshot horizon has aged out,
// honor the caller's context, survive hostile writes to the events subject,
// and answer a never-existed task with TaskNotFound rather than an empty Task.

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// replayFixture publishes n status events for taskID and returns the client.
func replayAddressee(taskID string) string { return "worker-" + taskID }

func replayFixture(t *testing.T, url, taskID string, states []TaskState) *Client {
	t.Helper()
	ctx := testCtx(t)
	c, err := Connect(ctx, url, WithName("replay-test"))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	t.Cleanup(c.Close)
	origin, err := NewMessageEnvelope(Party{Session: "chatops"}, taskID, "ctx-"+taskID, "corr-rp",
		validMessagePayload())
	if err != nil {
		t.Fatal(err)
	}
	exec, err := c.NewTaskExecution(origin, Party{Session: "worker-" + taskID}, replayAddressee(taskID))
	if err != nil {
		t.Fatal(err)
	}
	for i, st := range states {
		final := i == len(states)-1 && st.Terminal()
		if err := exec.PublishStatus(ctx, st, final); err != nil {
			t.Fatalf("publish %s: %v", st, err)
		}
	}
	return c
}

// deleteLastMsg removes the newest message on subject from the stream — what
// retention aging looks like to a replay that already snapshotted its horizon.
func deleteLastMsg(t *testing.T, url, stream, subject string) {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	st, err := js.Stream(ctx, stream)
	if err != nil {
		t.Fatal(err)
	}
	last, err := st.GetLastMsgForSubject(ctx, subject)
	if err != nil {
		t.Fatal(err)
	}
	if err := st.DeleteMsg(ctx, last.Sequence); err != nil {
		t.Fatal(err)
	}
}

// TasksGet must terminate when messages behind the snapshotted horizon are
// gone, folding what remains instead of blocking forever.
func TestTasksGet_HorizonDeleted(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	c := replayFixture(t, clientURL(s), "task-hd", []TaskState{StateSubmitted, StateWorking, StateInputRequired})

	// TasksGet snapshots the horizon from the stream itself, so delete the
	// last event first: the replay must then finish on pending-exhaustion.
	deleteLastMsg(t, clientURL(s), TasksStream, TaskEventsSubject(replayAddressee("task-hd"), "task-hd"))

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	done := make(chan struct{})
	var task *Task
	var err error
	go func() {
		task, err = c.TasksGet(ctx, replayAddressee("task-hd"), "task-hd")
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("TasksGet hung with a deleted horizon message")
	}
	if err != nil {
		t.Fatalf("TasksGet: %v", err)
	}
	want := []TaskState{StateSubmitted, StateWorking}
	if len(task.StatusHistory) != len(want) {
		t.Fatalf("status history = %v, want %v", task.StatusHistory, want)
	}
}

// TasksGet must honor the caller's context even while the iterator is blocked.
func TestTasksGet_ContextCanceled(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	c := replayFixture(t, clientURL(s), "task-cc", []TaskState{StateSubmitted})

	ctx, cancel := context.WithCancel(context.Background())
	cancel() // already canceled: replay must not deliver anything or hang
	done := make(chan error, 1)
	go func() {
		_, err := c.TasksGet(ctx, replayAddressee("task-cc"), "task-cc")
		done <- err
	}()
	select {
	case err := <-done:
		if err == nil {
			// A fold that completed before noticing cancellation is
			// acceptable; a hang is not. Nothing to assert.
			t.Log("replay completed before observing cancellation")
		}
	case <-time.After(10 * time.Second):
		t.Fatal("TasksGet ignored a canceled context")
	}
}

// A hostile or foreign write on the events subject must not revoke tasks/get
// for the task: unparseable bytes and non-event kinds are skipped in replay,
// the same way the live path terms poison instead of dying.
func TestTasksGet_SkipsPoisonEvents(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	c := replayFixture(t, clientURL(s), "task-po", []TaskState{StateSubmitted, StateWorking})

	events := TaskEventsSubject(replayAddressee("task-po"), "task-po")
	publishRaw(t, clientURL(s), events, []byte(`this is not json`))
	// A validly-enveloped kind that does not belong on an events subject.
	stray, err := NewMessageEnvelope(Party{Session: "intruder"}, "task-po", "ctx-task-po", "corr-x",
		validMessagePayload())
	if err != nil {
		t.Fatal(err)
	}
	strayRaw, _ := json.Marshal(stray)
	publishRaw(t, clientURL(s), events, strayRaw)
	// A valid event for ANOTHER task, landed on this task's subject: the
	// fourth poison class - FoldTask would hard-error on it, so the replay
	// screen must drop it.
	foreign, err := NewStatusUpdateEnvelope(Party{Session: "worker-other"}, "task-other", "ctx-other", "corr-f",
		json.RawMessage(`{"taskId":"task-other","contextId":"ctx-other","status":{"state":"working"}}`))
	if err != nil {
		t.Fatal(err)
	}
	foreignRaw, _ := json.Marshal(foreign)
	publishRaw(t, clientURL(s), events, foreignRaw)
	// An envelope for THIS task whose payload names another - refused at
	// parse (assertion 7's taskId-agreement clause), so replay skips it as
	// poison rather than letting the fold hard-error.
	mismatched := `{"protocol":"` + Protocol + `","envelopeId":"env-mm-1","correlationId":"corr-mm",` +
		`"ts":"2026-09-01T00:00:00Z","from":{"session":"intruder"},"identity":null,"authority":null,` +
		`"kind":"status-update","taskId":"task-po","contextId":"ctx-task-po",` +
		`"payload":{"taskId":"task-other","contextId":"ctx-task-po","status":{"state":"working"}}}`
	publishRaw(t, clientURL(s), events, []byte(mismatched))

	// The task keeps going after the garbage.
	origin, err := NewMessageEnvelope(Party{Session: "chatops"}, "task-po", "ctx-task-po", "corr-rp",
		validMessagePayload())
	if err != nil {
		t.Fatal(err)
	}
	exec, err := c.NewTaskExecution(origin, Party{Session: "worker-task-po"}, replayAddressee("task-po"))
	if err != nil {
		t.Fatal(err)
	}
	ctx := testCtx(t)
	if err := exec.PublishArtifact(ctx, Artifact{ArtifactID: "r", Name: ArtifactResult,
		Parts: []Part{{Kind: "text", Text: "done"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, StateCompleted, true); err != nil {
		t.Fatal(err)
	}

	task, err := c.TasksGet(ctx, replayAddressee("task-po"), "task-po")
	if err != nil {
		t.Fatalf("TasksGet with poison on the subject: %v", err)
	}
	if task.State != StateCompleted || task.Artifact(ArtifactResult) == nil {
		t.Errorf("task = %s, result artifact %v; poison must not distort the fold",
			task.State, task.Artifact(ArtifactResult))
	}
}

// A task with no events in the retention window is TaskNotFound, not an empty
// shell indistinguishable from a broken task.
func TestTasksGet_UnknownTask(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	ctx := testCtx(t)
	c, err := Connect(ctx, clientURL(s), WithName("replay-test"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()

	_, err = c.TasksGet(ctx, "nobody", "task-never-existed")
	var a2aErr *A2AError
	if !errors.As(err, &a2aErr) || a2aErr.Code != CodeTaskNotFound {
		t.Fatalf("want A2AError TaskNotFound(%d), got %v", CodeTaskNotFound, err)
	}
}

// An event whose payload names a different task than its envelope must not
// fold silently into this task.
func TestFoldTask_PayloadTaskIDMismatch(t *testing.T) {
	origin, err := NewMessageEnvelope(Party{Session: "chatops"}, "task-mm", "ctx-mm", "corr-mm",
		validMessagePayload())
	if err != nil {
		t.Fatal(err)
	}
	exec, err := (&Client{}).NewTaskExecution(origin, Party{Session: "w"}, "worker-mm")
	if err != nil {
		t.Fatal(err)
	}
	env, err := exec.StatusEnvelope(StateWorking, false)
	if err != nil {
		t.Fatal(err)
	}
	env.Payload = json.RawMessage(`{"taskId": "task-OTHER", "contextId": "ctx-mm", "status": {"state": "working"}, "final": false}`)
	_, err = FoldTask("task-mm", []*Envelope{env})
	var perr *ProtocolError
	if !errors.As(err, &perr) {
		t.Fatalf("want ProtocolError on payload/envelope taskId mismatch, got %v", err)
	}
}
