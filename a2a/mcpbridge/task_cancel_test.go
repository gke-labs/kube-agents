package mcpbridge

import (
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
	"strings"
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

func TestTaskControlStopDuringASendSendsOneStop(t *testing.T) {
	gw := startBlockingGateway(t, ToolSendMessage)
	srv := newStdioTestServer(t, gw)
	done := make(chan CallToolResult, 1)
	go func() {
		done <- srv.CallTool(context.Background(), ToolSendMessage, json.RawMessage(`{"message":"look at prod","conversation":"c1"}`))
	}()
	select {
	case <-gw.started:
	case <-time.After(stdioTestWait):
		t.Fatal("the gateway never received the send_message")
	}
	if res := srv.CallTool(context.Background(), ToolTaskControl, json.RawMessage(`{"action":"stop","conversation":"c1"}`)); res.IsError {
		t.Fatalf("task_control stop: %+v", res)
	}
	select {
	case <-done:
	case <-time.After(stdioTestWait):
		t.Fatal("the stopped send_message did not return")
	}
	gw.Release()
	srv.bgWG.Wait()
	if n := len(gw.stops); n != 1 {
		t.Fatalf("a task_control stop during a send sent %d remote stops; want 1", n)
	}
}

func TestTaskControlStopByTaskIDGoesToTheTasksConversation(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	ctx := context.Background()
	track := func() {
		// platform/task-1 was returned still running by a send_message on c1; the active
		// conversation is another one.
		srv.mu.Lock()
		srv.activeStreamingTasks["task:platform/task-1"] = &activeStreamingTask{TaskID: "platform/task-1", ConvAlias: "c1", UpdatedAt: time.Now()}
		srv.mu.Unlock()
	}
	nextStop := func(path string) map[string]any {
		t.Helper()
		select {
		case stop := <-gw.stops:
			return stop
		case <-time.After(stdioTestWait):
			t.Fatalf("%s: the gateway received no stop", path)
		}
		return nil
	}
	if active := srv.ReadOnlyConversationFor(ctx, "").Alias; active == "c1" {
		t.Fatalf("the active conversation is already c1; the test needs another")
	}

	track()
	if res := srv.CallTool(ctx, ToolTaskControl, json.RawMessage(`{"action":"stop","task_id":"task-1"}`)); res.IsError {
		t.Fatalf("task_control stop by task_id: %+v", res)
	}
	if conv := nextStop("task_control")["conversation"]; conv != "c1" {
		t.Fatalf("task_control stop of c1's task stopped conversation %v; want c1", conv)
	}
	track()
	if _, _, _, err := srv.CallRemoteTool(ctx, ToolTaskControl, json.RawMessage(`{"action":"stop","task_id":"task-1"}`), CallMeta{}, nil); err != nil {
		t.Fatalf("CallRemoteTool stop by task_id: %v", err)
	}
	if conv := nextStop("CallRemoteTool")["conversation"]; conv != "c1" {
		t.Fatalf("CallRemoteTool stop of c1's task stopped conversation %v; want c1", conv)
	}

	// A task the bridge is not tracking: refused, not sent to the active conversation.
	if res := srv.CallTool(ctx, ToolTaskControl, json.RawMessage(`{"action":"stop","task_id":"task-9"}`)); !res.IsError ||
		!strings.Contains(res.Content[0].Text, "stop needs the task's conversation") {
		t.Fatalf("task_control stop of an untracked task: %+v; want it refused", res)
	}
	if _, _, _, err := srv.CallRemoteTool(ctx, ToolTaskControl, json.RawMessage(`{"action":"stop","task_id":"task-9"}`), CallMeta{}, nil); !errors.Is(err, errStopNeedsConversation) {
		t.Fatalf("CallRemoteTool stop of an untracked task: %v; want it refused", err)
	}
	select {
	case stop := <-gw.stops:
		t.Fatalf("a stop of an untracked task was sent: %v", stop)
	case <-time.After(100 * time.Millisecond):
	}
}

func TestTrackedTasksAreSweptWithoutAHost(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	if srv.host != nil {
		t.Fatal("the test server has a HostIntegration; the bare binary has none")
	}
	srv.mu.Lock()
	srv.activeStreamingTasks["task:platform/task-old"] = &activeStreamingTask{TaskID: "platform/task-old", ConvAlias: "c1", UpdatedAt: time.Now().Add(-staleStreamingTaskAge - time.Minute)}
	srv.activeStreamingTasks["task:platform/task-new"] = &activeStreamingTask{TaskID: "platform/task-new", ConvAlias: "c1", UpdatedAt: time.Now()}
	srv.mu.Unlock()
	srv.checkActiveStreamingTasksForCancel(context.Background())
	if owner := srv.taskOwnerConversation("task-old"); owner != "" {
		t.Fatalf("a task untouched for over %v still answers with owner %q in a bridge with no host", staleStreamingTaskAge, owner)
	}
	if owner := srv.taskOwnerConversation("task-new"); owner != "c1" {
		t.Fatalf("the sweep dropped a fresh task (owner %q)", owner)
	}
}

func TestAFinalGetDropsTheTaskUnderItsOtherSpelling(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	// A send on c1 left platform/task-1 running; a later get spells it task-1 and finds it done.
	srv.mu.Lock()
	srv.activeStreamingTasks["task:platform/task-1"] = &activeStreamingTask{TaskID: "platform/task-1", ConvAlias: "c1", UpdatedAt: time.Now()}
	srv.mu.Unlock()
	srv.registerStreamingCallStart("task:task-1", "task-1", "c1", "", "", func() {})
	srv.finishStreamingCall("task:task-1", "c1", "", "", "Task `platform/task-1`: **completed**\n\nDone.\n", nil, nil)
	srv.mu.Lock()
	n := len(srv.activeStreamingTasks)
	srv.mu.Unlock()
	if n != 0 {
		t.Fatalf("after a get found task-1 done, %d tracked entr(ies) remain; want none", n)
	}
}

// A stop of an untracked task is refused before the bridge connects: no scan, no dial.
func TestTaskControlStopOfAnUntrackedTaskRefusesBeforeConnecting(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	srv, err := NewServer(ServerOptions{Store: store, Cluster: failingClusterAccess{discoverErr: errors.New("the stop scanned the kubeconfig")}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Close)
	res := srv.CallTool(context.Background(), ToolTaskControl, json.RawMessage(`{"action":"stop","task_id":"task-9"}`))
	if !res.IsError || !strings.Contains(res.Content[0].Text, "stop needs the task's conversation") || strings.Contains(res.Content[0].Text, "scanned") {
		t.Fatalf("task_control stop of an untracked task with no session: %+v; want the refusal, before any connect", res)
	}
}

// A get by bare id that fails or is cancelled brought no answer: the owner the send recorded
// under the task's other spelling is kept, so a later stop by task_id still finds it.
func TestAFailedOrCancelledGetKeepsTheTasksOwner(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	for _, tc := range []struct {
		name            string
		ctxErr, callErr error
	}{
		{"failed", nil, errors.New("tunnel hiccup")},
		{"cancelled", context.Canceled, nil},
	} {
		srv.mu.Lock()
		srv.activeStreamingTasks["task:platform/task-1"] = &activeStreamingTask{TaskID: "platform/task-1", ConvAlias: "c1", UpdatedAt: time.Now()}
		srv.mu.Unlock()
		srv.registerStreamingCallStart("task:task-1", "task-1", "c1", "", "", func() {})
		srv.finishStreamingCall("task:task-1", "c1", "", "", "", tc.ctxErr, tc.callErr)
		srv.bgWG.Wait()
		if owner := srv.taskOwnerConversation("task-1"); owner != "c1" {
			t.Fatalf("%s get: the task's owner is %q; want c1 kept", tc.name, owner)
		}
	}
}
