package mcpbridge

import (
	"context"
	"encoding/json"
	"testing"
	"time"
)

// cancelTaskGet cancels a task_control get on task-1 while the gateway holds it, and returns
// the remote stop the bridge sent for it, if any.
func cancelTaskGet(t *testing.T, srv *Server, gw *blockingGateway) (map[string]any, bool) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan CallToolResult, 1)
	go func() {
		done <- srv.CallTool(ctx, ToolTaskControl, json.RawMessage(`{"action":"get","task_id":"task-1"}`))
	}()
	select {
	case <-gw.started:
	case <-time.After(stdioTestWait):
		t.Fatal("the gateway never received the task_control get")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(stdioTestWait):
		t.Fatal("the cancelled task_control get did not return")
	}
	gw.Release()
	srv.bgWG.Wait()
	select {
	case stop := <-gw.stops:
		return stop, true
	default:
		return nil, false
	}
}

func TestCancelledTaskGetSendsNoStopWhenTheTasksConversationIsUnknown(t *testing.T) {
	gw := startBlockingGateway(t, ToolTaskControl)
	srv := newStdioTestServer(t, gw)
	if stop, ok := cancelTaskGet(t, srv, gw); ok {
		t.Fatalf("a cancelled get of a task with no known conversation sent stop %v", stop)
	}
}

func TestCancelledTaskGetStopsTheTasksOwnConversation(t *testing.T) {
	gw := startBlockingGateway(t, ToolTaskControl)
	srv := newStdioTestServer(t, gw)
	// task-1 was returned still running by a send_message on c1; the active conversation is
	// another one.
	srv.mu.Lock()
	srv.activeStreamingTasks["task:task-1"] = &activeStreamingTask{TaskID: "task-1", ConvAlias: "c1", UpdatedAt: time.Now()}
	srv.mu.Unlock()
	stop, ok := cancelTaskGet(t, srv, gw)
	if !ok {
		t.Fatal("a cancelled get of c1's task sent no stop")
	}
	if conv, _ := stop["conversation"].(string); conv != "c1" {
		t.Fatalf("the cancelled get stopped conversation %q (args %v); want the task's own c1", conv, stop)
	}
}

func TestCancelledTaskGetStopsTheConversationAServerReplyNamedRunning(t *testing.T) {
	gw := startBlockingGateway(t, ToolTaskControl)
	// The in-cluster server's answer for a turn still running (a2a/mcp writeTask).
	gw.sendReply = "Task `platform/task-1`: **working** (still running; read it again with task_control get)\n\nProgress: `reading`\n"
	srv := newStdioTestServer(t, gw)
	if res := srv.CallTool(context.Background(), ToolSendMessage, json.RawMessage(`{"message":"look at prod","conversation":"c1"}`)); res.IsError {
		t.Fatalf("send_message: %+v", res)
	}
	stop, ok := cancelTaskGet(t, srv, gw)
	if !ok {
		t.Fatal("a cancelled get of the task send_message reported running sent no stop")
	}
	if conv, _ := stop["conversation"].(string); conv != "c1" {
		t.Fatalf("the cancelled get stopped conversation %q (args %v); want c1", conv, stop)
	}
}

func TestRunningTaskRef(t *testing.T) {
	for text, want := range map[string]string{
		"Task `platform/abc-1`: **working** (still running; read it again with task_control get)\n": "platform/abc-1",
		"Task `platform/abc-1`: **completed**\n\nDone.\n":                                           "",
		"some answer mentioning task-9\n":                                                           "",
	} {
		if got := runningTaskRef(text); got != want {
			t.Errorf("runningTaskRef(%q) = %q; want %q", text, got, want)
		}
	}
	if !sameTaskRef("platform/abc", "abc") || !sameTaskRef("abc", "platform/abc") || sameTaskRef("a/abc", "b/abc") || sameTaskRef("", "") {
		t.Error("sameTaskRef does not match a reference to its bare task id, or matches two addressees")
	}
}
