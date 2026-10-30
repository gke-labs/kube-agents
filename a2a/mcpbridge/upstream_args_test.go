package mcpbridge

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// nextCall returns the next tools/call the gateway received.
func nextCall(t *testing.T, gw *blockingGateway) (string, map[string]any) {
	t.Helper()
	select {
	case c := <-gw.calls:
		name, _ := c["name"].(string)
		args, _ := c["arguments"].(map[string]any)
		return name, args
	case <-time.After(stdioTestWait):
		t.Fatal("the gateway received no tools/call")
	}
	return "", nil
}

// nextCallNamed skips calls to other tools (the connect-time connection_status).
func nextCallNamed(t *testing.T, gw *blockingGateway, tool string) map[string]any {
	t.Helper()
	for {
		name, args := nextCall(t, gw)
		if name == tool {
			return args
		}
	}
}

func TestSendMessageForwardsTheServersArgumentNames(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	res := srv.CallTool(context.Background(), ToolSendMessage,
		json.RawMessage(`{"message":"hello","wait_timeout_seconds":30,"conversation":"c1"}`))
	if res.IsError {
		t.Fatalf("send_message: %+v", res)
	}
	args := nextCallNamed(t, gw, ToolSendMessage)
	if args["text"] != "hello" || args["wait_seconds"] != float64(30) || args["conversation"] != "c1" {
		t.Fatalf("forwarded send_message arguments %v; want text, wait_seconds and conversation", args)
	}
	for _, bridgeName := range []string{"message", "wait_timeout_seconds"} {
		if _, ok := args[bridgeName]; ok {
			t.Fatalf("forwarded send_message arguments %v still carry the bridge's %q", args, bridgeName)
		}
	}
}

func TestTaskControlForwardsTheServersArgumentNames(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)

	res := srv.CallTool(context.Background(), ToolTaskControl,
		json.RawMessage(`{"action":"get","task_id":"task-1","wait_timeout_seconds":12}`))
	if res.IsError {
		t.Fatalf("task_control get: %+v", res)
	}
	args := nextCallNamed(t, gw, ToolTaskControl)
	if args["action"] != "get" || args["task"] != "task-1" || args["wait_seconds"] != float64(12) {
		t.Fatalf("forwarded task_control get arguments %v; want task and wait_seconds", args)
	}
	for _, bridgeName := range []string{"task_id", "wait_timeout_seconds"} {
		if _, ok := args[bridgeName]; ok {
			t.Fatalf("forwarded task_control get arguments %v still carry the bridge's %q", args, bridgeName)
		}
	}

	// cancel is the bridge's other spelling of stop; the server knows only stop.
	res = srv.CallTool(context.Background(), ToolTaskControl, json.RawMessage(`{"action":"cancel","conversation":"c1"}`))
	if res.IsError {
		t.Fatalf("task_control cancel: %+v", res)
	}
	args = nextCallNamed(t, gw, ToolTaskControl)
	if args["action"] != "stop" || args["conversation"] != "c1" {
		t.Fatalf("forwarded task_control cancel arguments %v; want action stop on c1", args)
	}
}

func TestTaskControlActionsTheServerLacksAreHiddenAndRefused(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)

	for _, d := range srv.toolDefinitions() {
		if d.Name != ToolTaskControl {
			continue
		}
		raw, _ := json.Marshal(d.InputSchema)
		var schema struct {
			Properties map[string]struct {
				Enum []string `json:"enum"`
			} `json:"properties"`
		}
		if err := json.Unmarshal(raw, &schema); err != nil {
			t.Fatal(err)
		}
		if got := strings.Join(schema.Properties["action"].Enum, ","); got != "get,stop" {
			t.Fatalf("task_control advertises actions %q; want get,stop only", got)
		}
		for _, steerOrList := range []string{"message", "only_active", "limit"} {
			if _, ok := schema.Properties[steerOrList]; ok {
				t.Fatalf("task_control advertises %q, an argument only steer or list takes", steerOrList)
			}
		}
	}
	for _, r := range srv.resourceDefinitions() {
		if r.URI == resourceURITasks {
			t.Fatalf("%s is advertised; the in-cluster server cannot list tasks yet", resourceURITasks)
		}
	}

	for _, action := range []string{"steer", "list", "status"} {
		res := srv.CallTool(context.Background(), ToolTaskControl,
			json.RawMessage(`{"action":"`+action+`","task_id":"task-1","message":"go left"}`))
		if !res.IsError || len(res.Content) == 0 || !strings.Contains(res.Content[0].Text, "not supported by the in-cluster server yet") {
			t.Fatalf("task_control %s: want a not-supported error, got %+v", action, res)
		}
	}
	select {
	case c := <-gw.calls:
		t.Fatalf("a refused task_control action reached the gateway: %v", c)
	default:
	}
}

func TestCallRemoteToolMetaCarriesOnlyTheCalledConversation(t *testing.T) {
	metas := make(chan map[string]any, 4)
	gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var req rpcRequest
		_ = json.Unmarshal(body, &req)
		var params struct {
			Meta map[string]any `json:"_meta"`
		}
		_ = json.Unmarshal(req.Params, &params)
		metas <- params.Meta
		w.Header().Set("Content-Type", contentTypeJSON)
		_, _ = fmt.Fprintf(w, "%s\n", marshalRPCResult(req.ID, CallToolResult{
			Content: []TextContent{{Type: contentTypeText, Text: "ok"}},
		}))
	}))
	defer gw.Close()

	sess := &gatewayBridgeSession{baseURL: gw.URL, httpClient: &http.Client{}}
	snap := PersistentConfig{
		ActiveConversation: "other",
		Conversations: map[string]ConversationEntry{
			"called": {Alias: "called", Token: "tok-called"},
			"other":  {Alias: "other", Token: "tok-other"},
		},
	}
	call := func(args string) map[string]any {
		t.Helper()
		if _, _, _, err := sess.callRemoteTool(context.Background(), ToolSendMessage, json.RawMessage(args), CallMeta{}, snap, nil); err != nil {
			t.Fatal(err)
		}
		return <-metas
	}

	meta := call(`{"text":"hi","conversation":"called"}`)
	for _, key := range []string{"conversations", "kube-agents/conversations"} {
		convs, _ := meta[key].(map[string]any)
		if len(convs) != 1 || convs["called"] == nil {
			t.Fatalf("_meta[%q] = %v; want the called conversation's entry only", key, meta[key])
		}
	}
	if raw, _ := json.Marshal(meta); strings.Contains(string(raw), "tok-other") || strings.Contains(string(raw), `"other"`) {
		t.Fatalf("_meta names a conversation the call did not: %s", raw)
	}

	meta = call(`{"action":"get","task":"task-1"}`)
	if _, ok := meta["conversations"]; ok {
		t.Fatalf("_meta of a call naming no conversation carries %v", meta["conversations"])
	}
}

func TestSendMessageForwardsTheSchemasWaitDefaultAndCeiling(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	for _, tc := range []struct {
		args string
		want float64
	}{
		{`{"message":"hello"}`, 150},
		{`{"message":"hello","wait_timeout_seconds":0}`, 150},
		{`{"message":"hello","wait_timeout_seconds":900}`, 300},
		{`{"message":"hello","wait_timeout_seconds":300}`, 300},
		{`{"message":"hello","wait_timeout_seconds":"30"}`, 30},
	} {
		if res := srv.CallTool(context.Background(), ToolSendMessage, json.RawMessage(tc.args)); res.IsError {
			t.Fatalf("send_message %s: %+v", tc.args, res)
		}
		if got := nextCallNamed(t, gw, ToolSendMessage)["wait_seconds"]; got != tc.want {
			t.Fatalf("send_message %s forwarded wait_seconds %v; want %v", tc.args, got, tc.want)
		}
	}
}

func TestSendMessageRefusesANonNumericWait(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	for _, args := range []string{`{"message":"hello","wait_timeout_seconds":"soon"}`, `{"message":"hello","wait_timeout_seconds":true}`} {
		if res := srv.CallTool(context.Background(), ToolSendMessage, json.RawMessage(args)); !res.IsError || !strings.Contains(res.Content[0].Text, "invalid wait_timeout_seconds") {
			t.Fatalf("send_message %s: %+v; want it refused", args, res)
		}
	}
	noCallNamed(t, gw, ToolSendMessage, 100*time.Millisecond)
}

// sendModeEnum is the mode enum send_message advertises.
func sendModeEnum(t *testing.T, srv *Server) []string {
	t.Helper()
	for _, d := range srv.toolDefinitions() {
		if d.Name != ToolSendMessage {
			continue
		}
		raw, _ := json.Marshal(d.InputSchema)
		var schema struct {
			Properties map[string]struct {
				Enum []string `json:"enum"`
			} `json:"properties"`
		}
		if err := json.Unmarshal(raw, &schema); err != nil {
			t.Fatal(err)
		}
		return schema.Properties["mode"].Enum
	}
	t.Fatal("send_message is not advertised")
	return nil
}

func TestSendMessageModesTheServerLacksAreHiddenAndRefused(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	if got := strings.Join(sendModeEnum(t, srv), ","); got != "auto" {
		t.Fatalf("send_message advertises modes %q; want auto only", got)
	}
	for _, mode := range []string{"session", "delegate", "platform", "Session"} {
		res := srv.CallTool(context.Background(), ToolSendMessage,
			json.RawMessage(`{"message":"hello","conversation":"c1","mode":"`+mode+`"}`))
		if !res.IsError || !strings.Contains(res.Content[0].Text, "not supported by the in-cluster server yet") {
			t.Fatalf("send_message mode %s: %+v; want it refused as not supported yet", mode, res)
		}
		if srv.IsSessionHealthy() {
			t.Fatalf("send_message mode %s connected before refusing", mode)
		}
		if c, ok := srv.Store().LookupConversation("c1"); ok && c.SessionMode {
			t.Fatalf("send_message mode %s marked c1 as a session", mode)
		}
	}

	res := srv.CallTool(context.Background(), ToolSendMessage, json.RawMessage(`{"message":"hello","mode":"auto"}`))
	if res.IsError {
		t.Fatalf("send_message mode auto: %+v", res)
	}
	if args := nextCallNamed(t, gw, ToolSendMessage); args["mode"] != nil {
		t.Fatalf("forwarded send_message arguments %v carry mode; the server has none", args)
	}
}

func TestSendMessageSessionTextMarksTheSessionOnlyWhenTheTurnIsTaken(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	gw.srv.Close()
	if res := srv.CallTool(context.Background(), ToolSendMessage, json.RawMessage(`{"message":"/session","conversation":"c1"}`)); !res.IsError {
		t.Fatalf("send_message to a closed gateway succeeded: %+v", res)
	}
	if c, ok := srv.Store().LookupConversation("c1"); ok && c.SessionMode {
		t.Fatal("a /session turn the server never took marked c1 as a session")
	}

	gw2 := startBlockingGateway(t, "")
	srv2 := newStdioTestServer(t, gw2)
	if res := srv2.CallTool(context.Background(), ToolSendMessage, json.RawMessage(`{"message":"/session","conversation":"c1"}`)); res.IsError {
		t.Fatalf("send_message /session: %+v", res)
	}
	if c, ok := srv2.Store().LookupConversation("c1"); !ok || !c.SessionMode {
		t.Fatalf("a taken /session turn left c1 %+v; want SessionMode", c)
	}
}

func TestCallRemoteToolGatesAndTranslatesLikeTheBridgesTools(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	ctx := context.Background()

	for _, refused := range []struct{ tool, args string }{
		{ToolTaskControl, `{"action":"list"}`},
		{ToolTaskControl, `{"action":"steer","task_id":"task-1","message":"go"}`},
		{ToolSendMessage, `{"message":"hello","mode":"session"}`},
	} {
		if _, _, _, err := srv.CallRemoteTool(ctx, refused.tool, json.RawMessage(refused.args), CallMeta{}, nil); err == nil ||
			!strings.Contains(err.Error(), "not supported by the in-cluster server yet") {
			t.Fatalf("CallRemoteTool %s %s: err %v; want it refused as not supported yet", refused.tool, refused.args, err)
		}
	}
	noCallNamed(t, gw, ToolTaskControl, 100*time.Millisecond)

	if _, _, _, err := srv.CallRemoteTool(ctx, ToolSendMessage, json.RawMessage(`{"message":"hello","conversation":"c1"}`), CallMeta{}, nil); err != nil {
		t.Fatalf("CallRemoteTool send_message: %v", err)
	}
	if args := nextCallNamed(t, gw, ToolSendMessage); args["text"] != "hello" || args["wait_seconds"] != float64(150) || args["message"] != nil {
		t.Fatalf("CallRemoteTool forwarded send_message arguments %v; want text and wait_seconds", args)
	}
	if _, _, _, err := srv.CallRemoteTool(ctx, ToolTaskControl, json.RawMessage(`{"action":"get","task_id":"task-1"}`), CallMeta{}, nil); err != nil {
		t.Fatalf("CallRemoteTool task_control get: %v", err)
	}
	if args := nextCallNamed(t, gw, ToolTaskControl); args["task"] != "task-1" || args["task_id"] != nil {
		t.Fatalf("CallRemoteTool forwarded task_control arguments %v; want task", args)
	}
	if _, _, _, err := srv.CallRemoteTool(ctx, ToolTaskControl, json.RawMessage(`{"action":"cancel","conversation":"c1"}`), CallMeta{}, nil); err != nil {
		t.Fatalf("CallRemoteTool task_control cancel: %v", err)
	}
	if args := nextCallNamed(t, gw, ToolTaskControl); args["action"] != "stop" || args["conversation"] != "c1" {
		t.Fatalf("CallRemoteTool forwarded task_control cancel arguments %v; want stop on c1", args)
	}
}
