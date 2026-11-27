package lib

import (
	"encoding/json"
	"testing"
)

// A dispatcher that restarts between its terminal's publish and its settle
// writes the terminal again under a new envelope id (gke-labs/kube-agents#6883).
// A reader's replay of the task (TasksGetTerminal, which the MCP server's and
// the gateway's reads fold through) takes the first final status as the end:
// the second identical one is a post-final drop, and the task's state,
// message and terminal subject are the first's.
func TestASecondIdenticalSixTokenTerminalChangesNothingOnReplay(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	ctx := testCtx(t)
	c, err := Connect(ctx, clientURL(s), WithName("reader"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Close)
	const addressee, taskID = "fleet-audit", "task-dup"
	payload, err := json.Marshal(StatusUpdate{TaskID: taskID, ContextID: "ctx-dup",
		Status: TaskStatus{State: StateFailed, Message: &Message{Role: "agent", MessageID: "m-dup",
			Parts: []Part{{Kind: "text", Text: "reason: lease-expired"}}}}, Final: true})
	if err != nil {
		t.Fatal(err)
	}
	subject := ProfileSupervisorSubject(addressee, taskID)
	for range 2 {
		env, err := NewStatusUpdateEnvelope(Party{Session: DispatcherSession, AgentType: "a2a-dispatcher"}, taskID, "ctx-dup", "corr-dup", payload)
		if err != nil {
			t.Fatal(err)
		}
		if _, dup, err := c.PublishAck(ctx, subject, env); err != nil || dup {
			t.Fatalf("publish: dup %v err %v", dup, err)
		}
	}
	task, got, _, err := c.TasksGetTerminal(ctx, addressee, taskID)
	if err != nil {
		t.Fatal(err)
	}
	if !task.Final || task.State != StateFailed || got != subject {
		t.Fatalf("task = %s final=%v on %q, want failed and final on %q", task.State, task.Final, got, subject)
	}
	if task.FinalMessage == nil || len(task.FinalMessage.Parts) != 1 || task.FinalMessage.Parts[0].Text != "reason: lease-expired" {
		t.Errorf("final message = %+v, want the dispatcher's notice", task.FinalMessage)
	}
	if task.PostFinalDropped != 1 {
		t.Errorf("PostFinalDropped = %d, want the second terminal dropped", task.PostFinalDropped)
	}
	if len(task.StatusHistory) != 1 {
		t.Errorf("status history %v, want the first terminal's state alone", task.StatusHistory)
	}
}
