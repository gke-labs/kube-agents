package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The MCP server end to end over the bus: tool calls, progress, and step streaming.

func TestBusSessionAndMCPServerEndToEnd(t *testing.T) {
	srv, nc, _ := startEmbeddedJetStream(t)
	ctx := context.Background()

	seedBusTopicsAndCard(t, nc)
	startSimulatedGatewayAndWorkers(t, nc)

	cfgPath := filepath.Join(t.TempDir(), "mcp-config.json")
	store, err := NewConfigStore(cfgPath)
	if err != nil {
		t.Fatalf("NewConfigStore: %v", err)
	}
	mcpSrv, err := NewServer(ServerOptions{Store: store})
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	t.Cleanup(mcpSrv.Close)

	// 1. Test initialize over JSON-RPC.
	initResp := mcpSrv.HandleJSONRPC(ctx, []byte(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}`))
	if !strings.Contains(string(initResp), `"name":"kube-agents"`) {
		t.Fatalf("unexpected initialize response: %s", initResp)
	}

	// 2. Test tools/list.
	listResp := mcpSrv.HandleJSONRPC(ctx, []byte(`{"jsonrpc":"2.0","id":2,"method":"tools/list"}`))
	for _, requiredTool := range []string{
		toolDiscoverDeployments,
		toolConnectDeployment,
		toolConnectionStatus,
		toolSendMessage,
		toolManageSession,
		toolTaskControl,
		toolReadTopics,
		toolInspectPR,
		toolPollEvents,
	} {
		if !strings.Contains(string(listResp), requiredTool) {
			t.Errorf("tools/list missing %q", requiredTool)
		}
	}

	// 3. Connect to embedded NATS deployment.
	connectArgs, _ := json.Marshal(map[string]any{
		"deployment":      testClusterName,
		"namespace":       testNamespace,
		"direct_nats_url": srv.ClientURL(),
	})
	connRes := mcpSrv.CallTool(ctx, toolConnectDeployment, connectArgs)
	if connRes.IsError || !strings.Contains(connRes.Content[0].Text, "TASKS") {
		t.Fatalf("connect_deployment failed: %+v", connRes)
	}

	// 4. Read blackboard topics.
	topicsRes := mcpSrv.CallTool(ctx, toolReadTopics, json.RawMessage(`{"action":"read","topic":"upgrade-readiness"}`))
	if topicsRes.IsError || !strings.Contains(topicsRes.Content[0].Text, "Cluster is ready for 1.32") {
		t.Fatalf("read_topics failed: %+v", topicsRes)
	}

	// 5. Send message in session mode triggering thinking -> activity -> delegate -> child -> wake -> chat/v1 result!
	// By default (debug == false), thinking is rendered collapsed (<details><summary>Thought...) and tool call tables/timings are hidden.
	sendArgs, _ := json.Marshal(map[string]any{
		"message":              "Check upgrade readiness and open a PR if needed",
		"mode":                 modeSession,
		"wait_timeout_seconds": 15,
	})
	sendRes := mcpSrv.CallTool(ctx, toolSendMessage, sendArgs)
	if sendRes.IsError {
		t.Fatalf("send_message returned error: %s", sendRes.Content[0].Text)
	}
	outText := sendRes.Content[0].Text
	for _, expectedSnippet := range []string{
		"<details>\n<summary>Thought",
		"Analyzing cluster upgrade readiness",
		"Upgrade Readiness Confirmed & PR Opened",
		testPRURL,
	} {
		if !strings.Contains(outText, expectedSnippet) {
			t.Errorf("send_message default output missing %q:\n%s", expectedSnippet, outText)
		}
	}
	for _, hiddenByDefault := range []string{
		"<details open>",
		"Tool Activity Stream",
		"run_kubectl",
		"flowchart LR",
	} {
		if strings.Contains(outText, hiddenByDefault) {
			t.Errorf("send_message default output should NOT contain %q when debug=false:\n%s", hiddenByDefault, outText)
		}
	}

	// 6. Verify task_control 'get' with debug=true shows the full chain, tool calls, timings, and task IDs.
	tcRes := mcpSrv.CallTool(ctx, toolTaskControl, json.RawMessage(`{"action":"get","task_id":"`+testParentTaskID+`","debug":true}`))
	if tcRes.IsError {
		t.Fatalf("task_control get failed: %s", tcRes.Content[0].Text)
	}
	debugText := tcRes.Content[0].Text
	for _, expectedDebugSnippet := range []string{
		testWakeTaskID,
		"Tool Activity Stream",
		"run_kubectl",
		"85ms",
		"Delegated Child Task #1",
		"Session Wake Synthesis Task",
	} {
		if !strings.Contains(debugText, expectedDebugSnippet) {
			t.Errorf("task_control debug=true output missing %q:\n%s", expectedDebugSnippet, debugText)
		}
	}
	// Also test manage_session debug_on and debug_off toggles.
	msDebugOn := mcpSrv.CallTool(ctx, toolManageSession, json.RawMessage(`{"action":"debug_on"}`))
	// Debug mode is scoped to the conversation (soak round 6: a global flag flipped every
	// concurrent conversation into debug output).
	if msDebugOn.IsError || !store.DebugModeFor(store.ActiveConversation().Alias) || store.DebugMode() {
		t.Fatalf("manage_session debug_on failed: %+v", msDebugOn)
	}
	if other, _ := store.EnsureConversation("unrelated-conv", true, ""); store.DebugModeFor(other.Alias) {
		t.Fatalf("debug_on must not leak to other conversations")
	}
	_, _ = store.EnsureConversation("default", false, "")
	msDebugOff := mcpSrv.CallTool(ctx, toolManageSession, json.RawMessage(`{"action":"debug_off"}`))
	if msDebugOff.IsError || store.DebugModeFor("default") {
		t.Fatalf("manage_session debug_off failed: %+v", msDebugOff)
	}

	// 7. Verify manage_session list, new, switch, transcript, and task_control list.
	msNew := mcpSrv.CallTool(ctx, toolManageSession, json.RawMessage(`{"action":"new","alias":"triage-2","description":"Second session"}`))
	if msNew.IsError || !strings.Contains(msNew.Content[0].Text, "triage-2") {
		t.Fatalf("manage_session new failed: %+v", msNew)
	}
	msList := mcpSrv.CallTool(ctx, toolManageSession, json.RawMessage(`{"action":"list"}`))
	if msList.IsError || !strings.Contains(msList.Content[0].Text, "triage-2") {
		t.Fatalf("manage_session list failed: %+v", msList)
	}
	msSwitch := mcpSrv.CallTool(ctx, toolManageSession, json.RawMessage(`{"action":"switch","alias":"default"}`))
	if msSwitch.IsError || !strings.Contains(msSwitch.Content[0].Text, "default") {
		t.Fatalf("manage_session switch failed: %+v", msSwitch)
	}
	msTranscript := mcpSrv.CallTool(ctx, toolManageSession, json.RawMessage(`{"action":"transcript","alias":"default"}`))
	if msTranscript.IsError || !strings.Contains(msTranscript.Content[0].Text, testParentTaskID) {
		t.Fatalf("manage_session transcript failed: %+v", msTranscript)
	}
	tcList := mcpSrv.CallTool(ctx, toolTaskControl, json.RawMessage(`{"action":"list"}`))
	if tcList.IsError || !strings.Contains(tcList.Content[0].Text, testParentTaskID) {
		t.Fatalf("task_control list failed: %+v", tcList)
	}

	// 8. Verify ServeStdio with resources/list, resources/read, ping, notifications/initialized.
	stdioIn := strings.Join([]string{
		`{"jsonrpc":"2.0","method":"notifications/initialized"}`,
		`{"jsonrpc":"2.0","id":10,"method":"ping"}`,
		`{"jsonrpc":"2.0","id":11,"method":"resources/list"}`,
		`{"jsonrpc":"2.0","id":12,"method":"resources/read","params":{"uri":"kube-agents://status"}}`,
		`{"jsonrpc":"2.0","id":13,"method":"resources/read","params":{"uri":"kube-agents://topics"}}`,
		`{"jsonrpc":"2.0","id":14,"method":"resources/read","params":{"uri":"kube-agents://tasks"}}`,
		`{"jsonrpc":"2.0","id":15,"method":"resources/read","params":{"uri":"kube-agents://events"}}`,
		`{"jsonrpc":"2.0","id":16,"method":"prompts/list"}`,
	}, "\n") + "\n"
	var stdioOut bytes.Buffer
	if err := mcpSrv.ServeStdio(ctx, strings.NewReader(stdioIn), &stdioOut); err != nil {
		t.Fatalf("ServeStdio: %v", err)
	}
	outLines := strings.Split(strings.TrimSpace(stdioOut.String()), "\n")
	if len(outLines) != 7 {
		t.Fatalf("expected 7 JSON-RPC responses (1 notification ignored), got %d:\n%s", len(outLines), stdioOut.String())
	}
}

func TestProgressNotificationsAndAutoMetaBinding(t *testing.T) {
	srv, nc, _ := startEmbeddedJetStream(t)
	ctx := context.Background()
	startSimulatedGatewayAndWorkers(t, nc)

	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	mcpSrv, err := NewServer(ServerOptions{Store: store})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(mcpSrv.Close)

	connectArgs, _ := json.Marshal(map[string]any{
		"deployment":      testClusterName,
		"namespace":       testNamespace,
		"direct_nats_url": srv.ClientURL(),
	})
	_ = mcpSrv.CallTool(ctx, toolConnectDeployment, connectArgs)

	// 1. Default mode (debug=false): suppresses notifications/progress so Antigravity's tool card stays collapsed by default,
	// and wraps output in _custom_tool_result with collapsed <details><summary>Thought...</summary></details>.
	callLineDefault := `{"jsonrpc":"2.0","id":77,"method":"tools/call","params":{"name":"send_message","arguments":{"message":"Check upgrade readiness","mode":"session","wait_timeout_seconds":10},"_meta":{"progressToken":"mcp-progress-77","antigravity.google/conversation_id":"conv-auto-bound-77"}}}` + "\n"
	var stdioOutDefault bytes.Buffer
	if err := mcpSrv.ServeStdio(ctx, strings.NewReader(callLineDefault), &stdioOutDefault); err != nil {
		t.Fatalf("ServeStdio default: %v", err)
	}
	outputDefault := stdioOutDefault.String()
	if strings.Contains(outputDefault, `"method":"notifications/progress"`) {
		t.Fatalf("expected NO notifications/progress frames when debug=false (keeps tool card collapsed), got:\n%s", outputDefault)
	}
	if !strings.Contains(outputDefault, `_custom_tool_result`) {
		t.Fatalf("expected _custom_tool_result wrapper in stdio output, got:\n%s", outputDefault)
	}
	if gotConv := store.ResolveAGYConversationID(); gotConv != "conv-auto-bound-77" {
		t.Fatalf("ResolveAGYConversationID = %q, want conv-auto-bound-77", gotConv)
	}

	// 2. Debug mode (debug=true): emits live MCP notifications/progress frames with Thinking and Tool Activity.
	srv2, nc2, _ := startEmbeddedJetStream(t)
	startSimulatedGatewayAndWorkers(t, nc2)
	store2, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config-2.json"))
	if err != nil {
		t.Fatal(err)
	}
	mcpSrv2, err := NewServer(ServerOptions{Store: store2})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(mcpSrv2.Close)
	connectArgs2, _ := json.Marshal(map[string]any{
		"deployment":      testClusterName,
		"namespace":       testNamespace,
		"direct_nats_url": srv2.ClientURL(),
	})
	_ = mcpSrv2.CallTool(ctx, toolConnectDeployment, connectArgs2)

	callLineDebug := `{"jsonrpc":"2.0","id":78,"method":"tools/call","params":{"name":"send_message","arguments":{"message":"Check upgrade readiness","mode":"session","wait_timeout_seconds":10,"debug":true},"_meta":{"progressToken":"mcp-progress-78"}}}` + "\n"
	var stdioOutDebug bytes.Buffer
	if err := mcpSrv2.ServeStdio(ctx, strings.NewReader(callLineDebug), &stdioOutDebug); err != nil {
		t.Fatalf("ServeStdio debug: %v", err)
	}
	outputDebug := stdioOutDebug.String()
	if !strings.Contains(outputDebug, `"method":"notifications/progress"`) || !strings.Contains(outputDebug, `"progressToken":"mcp-progress-78"`) {
		t.Fatalf("expected MCP notifications/progress frames in stdio output when debug=true, got:\n%s", outputDebug)
	}
	if !strings.Contains(outputDebug, "Task t-parent-101") || !strings.Contains(outputDebug, "Tool Activity Stream") {
		t.Fatalf("expected live task progress and Tool Activity Stream when debug=true, got:\n%s", outputDebug)
	}
}

func TestIncrementalStepStreamingAndCustomEnvelope(t *testing.T) {
	srv, nc, _ := startEmbeddedJetStream(t)
	ctx := context.Background()

	const (
		multiStepTaskID = "task-multistep-501"
		multiStepCtxID  = "ctx-multistep-501"
	)

	// Simulate a multi-step platform-agent task where:
	// - Step 1 & 2 arrive within 200ms (so send_message returns after ~1.3s with STREAMING_STEP_CONTINUE).
	// - Step 3 and final completion arrive ~2.0s after task start (captured by the follow-up task_control get call).
	sub, err := nc.Subscribe("chat.console.*.in", func(msg *nats.Msg) {
		var in ConsoleInFrame
		_ = json.Unmarshal(msg.Data, &in)
		token := strings.TrimPrefix(msg.Subject, subjectConsoleInPrefix)
		token = strings.TrimSuffix(token, subjectConsoleInSuffix)
		convName := consoleConvPrefix + token
		authRaw := mustJSON(map[string]any{
			"audience": map[string]any{"conversation": convName},
		})
		now := time.Now().UTC()

		pReqSubj := fmt.Sprintf("a2a.tasks.platform.%s.in", multiStepTaskID)
		pOutSubj := fmt.Sprintf("a2a.tasks.platform.%s.out", multiStepTaskID)

		publishTaskEnv(t, nc, pReqSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "ms-req-1",
			TS:         now,
			TaskID:     multiStepTaskID,
			ContextID:  multiStepCtxID,
			Kind:       lib.KindMessage,
			From:       lib.Party{Session: "gateway"},
			Authority:  authRaw,
			Payload:    mustJSON(lib.Message{Role: "user", MessageID: "m-ms-1", Parts: []lib.Part{{Kind: "text", Text: in.Text}}}),
		})
		publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "ms-out-1",
			TS:         now.Add(20 * time.Millisecond),
			TaskID:     multiStepTaskID,
			ContextID:  multiStepCtxID,
			Kind:       lib.KindStatusUpdate,
			From:       lib.Party{Session: "platform-agent"},
			Payload:    mustJSON(lib.StatusUpdate{TaskID: multiStepTaskID, ContextID: multiStepCtxID, Status: lib.TaskStatus{State: lib.StateWorking}}),
		})
		publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "ms-out-2",
			TS:         now.Add(50 * time.Millisecond),
			TaskID:     multiStepTaskID,
			ContextID:  multiStepCtxID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: "platform-agent"},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    multiStepTaskID,
				ContextID: multiStepCtxID,
				Append:    true,
				Artifact: stepsArtifact(
					planStep("Searching available tools.", "Looking up GKE and kubectl tools first.", "c-ts-1"),
					planStep("Listing GKE clusters in the fleet.", "Querying fleet cluster inventory.", "c-gke-1"),
				),
			}),
		})
		publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "ms-out-2b",
			TS:         now.Add(60 * time.Millisecond),
			TaskID:     multiStepTaskID,
			ContextID:  multiStepCtxID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: "platform-agent"},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    multiStepTaskID,
				ContextID: multiStepCtxID,
				Artifact:  lib.Artifact{Name: lib.ArtifactActivity, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"tool":"tool_search","input":{"query":"\u003cstring, 11 chars\u003e"},"callId":"c-ts-1","status":"completed","durationMs":45}`)}, {Kind: "data", Data: json.RawMessage(`{"tool":"mcp__gke__list_clusters","input":{"project":"\u003cstring, 14 chars\u003e"},"callId":"c-gke-1","status":"completed","durationMs":210}`)}}},
			}),
		})

		go func() {
			time.Sleep(1800 * time.Millisecond)
			publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
				Protocol:   lib.Protocol,
				EnvelopeID: "ms-out-3",
				TS:         time.Now().UTC(),
				TaskID:     multiStepTaskID,
				ContextID:  multiStepCtxID,
				Kind:       lib.KindArtifactUpdate,
				From:       lib.Party{Session: "platform-agent"},
				Payload: mustJSON(lib.ArtifactUpdate{
					TaskID:    multiStepTaskID,
					ContextID: multiStepCtxID,
					Append:    true,
					Artifact:  stepsArtifact(planStep("Checking Kubernetes node status and readiness.", "All 5 node pools RUNNING, now checking pods and nodes.", "c-term-1")),
				}),
			})
			publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
				Protocol:   lib.Protocol,
				EnvelopeID: "ms-out-3b",
				TS:         time.Now().UTC(),
				TaskID:     multiStepTaskID,
				ContextID:  multiStepCtxID,
				Kind:       lib.KindArtifactUpdate,
				From:       lib.Party{Session: "platform-agent"},
				Payload: mustJSON(lib.ArtifactUpdate{
					TaskID:    multiStepTaskID,
					ContextID: multiStepCtxID,
					Append:    true,
					Artifact:  lib.Artifact{Name: lib.ArtifactActivity, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"tool":"terminal","input":{"command":"\u003cstring, 48 chars\u003e"},"callId":"c-term-1","status":"completed","durationMs":620}`)}}},
				}),
			})
			publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
				Protocol:   lib.Protocol,
				EnvelopeID: "ms-out-4",
				TS:         time.Now().UTC(),
				TaskID:     multiStepTaskID,
				ContextID:  multiStepCtxID,
				Kind:       lib.KindArtifactUpdate,
				From:       lib.Party{Session: "platform-agent"},
				Payload: mustJSON(lib.ArtifactUpdate{
					TaskID:    multiStepTaskID,
					ContextID: multiStepCtxID,
					Artifact:  lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "All 9 pods in kubeagents-system are Running and Ready."}}},
				}),
			})
			publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
				Protocol:   lib.Protocol,
				EnvelopeID: "ms-out-5",
				TS:         time.Now().UTC(),
				TaskID:     multiStepTaskID,
				ContextID:  multiStepCtxID,
				Kind:       lib.KindStatusUpdate,
				From:       lib.Party{Session: "platform-agent"},
				Payload:    mustJSON(lib.StatusUpdate{TaskID: multiStepTaskID, ContextID: multiStepCtxID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
			})
		}()
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })

	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	mcpSrv, err := NewServer(ServerOptions{Store: store})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(mcpSrv.Close)

	connectArgs, _ := json.Marshal(map[string]any{
		"deployment":      testClusterName,
		"namespace":       testNamespace,
		"direct_nats_url": srv.ClientURL(),
	})
	_ = mcpSrv.CallTool(ctx, toolConnectDeployment, connectArgs)

	// 1. Call send_message over JSON-RPC with Antigravity _meta: should return Step 1 ONLY with rich Agent + Thought + Tool Activity!
	req1 := `{"jsonrpc":"2.0","id":201,"method":"tools/call","params":{"name":"send_message","arguments":{"message":"Check all pods in kubeagents-system"},"_meta":{"antigravity.google/conversation_id":"conv-stream-test"}}}`
	resp1Raw := mcpSrv.HandleJSONRPC(ctx, []byte(req1))
	var resp1 struct {
		Result MCPCallToolResult `json:"result"`
	}
	if err := json.Unmarshal(resp1Raw, &resp1); err != nil {
		t.Fatalf("unmarshal resp1: %v", err)
	}
	var env1 struct {
		Custom bool   `json:"_custom_tool_result"`
		Title  string `json:"title"`
		MD     string `json:"markdown"`
		Result string `json:"result"`
	}
	if err := json.Unmarshal([]byte(resp1.Result.Content[0].Text), &env1); err != nil {
		t.Fatalf("unmarshal env1: %v\nraw: %s", err, resp1.Result.Content[0].Text)
	}
	if !env1.Custom {
		t.Fatalf("expected _custom_tool_result=true in env1")
	}
	if !strings.Contains(env1.Title, "Searching available tools") {
		t.Errorf("unexpected step 1 title: %q", env1.Title)
	}
	for _, wantInMD1 := range []string{
		"**Agent**: `platform`",
		"**Thought**: *Looking up GKE and kubectl tools first.*",
		"**`tool_search`**",
		"45ms",
	} {
		if !strings.Contains(env1.MD, wantInMD1) {
			t.Errorf("step 1 expanded markdown missing %q:\n%s", wantInMD1, env1.MD)
		}
	}
	if strings.Contains(env1.MD, "Listing GKE clusters") {
		t.Errorf("expected step 1 markdown to contain ONLY step 1:\n%s", env1.MD)
	}
	if !strings.Contains(env1.Result, "STREAMING_STEP_CONTINUE") || !strings.Contains(env1.Result, multiStepTaskID) {
		t.Errorf("expected STREAMING_STEP_CONTINUE in LLM result:\n%s", env1.Result)
	}

	// 2. Call task_control(action: "get") #1: advances by 1 step to Step 2 ("Listing GKE clusters in the fleet")
	// and shows Step 2's Thought + Tool Activity!
	req2 := `{"jsonrpc":"2.0","id":202,"method":"tools/call","params":{"name":"task_control","arguments":{"action":"get","task_id":"` + multiStepTaskID + `"},"_meta":{"antigravity.google/conversation_id":"conv-stream-test"}}}`
	resp2Raw := mcpSrv.HandleJSONRPC(ctx, []byte(req2))
	var resp2 struct {
		Result MCPCallToolResult `json:"result"`
	}
	if err := json.Unmarshal(resp2Raw, &resp2); err != nil {
		t.Fatalf("unmarshal resp2: %v", err)
	}
	var env2 struct {
		Custom bool   `json:"_custom_tool_result"`
		Title  string `json:"title"`
		MD     string `json:"markdown"`
		Result string `json:"result"`
	}
	if err := json.Unmarshal([]byte(resp2.Result.Content[0].Text), &env2); err != nil {
		t.Fatalf("unmarshal env2: %v", err)
	}
	if !strings.Contains(env2.Title, "Listing GKE clusters in the fleet") {
		t.Errorf("unexpected step 2 title: %q", env2.Title)
	}
	for _, wantInMD2 := range []string{
		"**Thought**: *Querying fleet cluster inventory.*",
		"**`mcp__gke__list_clusters`**",
		"210ms",
	} {
		if !strings.Contains(env2.MD, wantInMD2) {
			t.Errorf("step 2 expanded markdown missing %q:\n%s", wantInMD2, env2.MD)
		}
	}
	if strings.Contains(env2.MD, "Searching available tools") {
		t.Errorf("expected step 2 markdown to contain ONLY step 2 (not repeating step 1):\n%s", env2.MD)
	}

	// 3. Call task_control(action: "get") #2: waits for step 3 & completion and returns ONLY step 3 in the step row,
	// while the LLM result has ONLY the final answer (NO duplicate <details><summary>Thought...</summary> block!).
	req3 := `{"jsonrpc":"2.0","id":203,"method":"tools/call","params":{"name":"task_control","arguments":{"action":"get","task_id":"` + multiStepTaskID + `"},"_meta":{"antigravity.google/conversation_id":"conv-stream-test"}}}`
	resp3Raw := mcpSrv.HandleJSONRPC(ctx, []byte(req3))
	var resp3 struct {
		Result MCPCallToolResult `json:"result"`
	}
	if err := json.Unmarshal(resp3Raw, &resp3); err != nil {
		t.Fatalf("unmarshal resp3: %v", err)
	}
	var env3 struct {
		Custom bool   `json:"_custom_tool_result"`
		Title  string `json:"title"`
		MD     string `json:"markdown"`
		Result string `json:"result"`
	}
	if err := json.Unmarshal([]byte(resp3.Result.Content[0].Text), &env3); err != nil {
		t.Fatalf("unmarshal env3: %v", err)
	}
	if !strings.Contains(env3.Title, "Checking Kubernetes node status and readiness") {
		t.Errorf("unexpected completion step title: %q", env3.Title)
	}
	for _, wantInMD3 := range []string{
		"**Thought**: *All 5 node pools RUNNING, now checking pods and nodes.*",
		"**`terminal`**",
		"`command` (48 chars)",
		"620ms",
	} {
		if !strings.Contains(env3.MD, wantInMD3) {
			t.Errorf("step 3 expanded markdown missing %q:\n%s", wantInMD3, env3.MD)
		}
	}
	if !strings.Contains(env3.Result, "All 9 pods in kubeagents-system are Running and Ready.") {
		t.Errorf("final LLM result missing answer:\n%s", env3.Result)
	}
	if strings.Contains(env3.Result, "<details>\n<summary>Thought") {
		t.Errorf("final LLM result in Antigravity custom envelope should NOT contain duplicate <details><summary>Thought block:\n%s", env3.Result)
	}
}
