package mcp

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

func mustStepPart(t *testing.T, s stepmsg.Step) lib.Part {
	t.Helper()
	p, err := s.Part()
	if err != nil {
		t.Fatalf("stepmsg.Step.Part: %v", err)
	}
	return p
}

func mustDataPart(t *testing.T, v any) lib.Part {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("json.Marshal: %v", err)
	}
	return lib.Part{Kind: "data", Data: raw}
}

func TestFormatStepDetailMarkdownAndCustomToolEnvelope(t *testing.T) {
	ref := TaskRef{Addressee: "platform", TaskID: "t-100"}
	stepPart1 := mustStepPart(t, stepmsg.Step{
		Kind:    stepmsg.KindPlan,
		Title:   "Checking cluster nodes and warning events",
		Thought: "I will check the cluster nodes and recent warning events first.",
		CallIDs: []string{"c1"},
	})
	stepPart2 := mustStepPart(t, stepmsg.Step{
		Kind:  stepmsg.KindSubtask,
		Title: "Investigate crashlooping pod",
		Subtask: &stepmsg.Subtask{
			ID:      "42",
			Title:   "Investigate crashlooping pod",
			Status:  "done",
			Verdict: "ACTIONABLE",
			Summary: "Pod crashloops due to missing ConfigMap.",
			Settled: 1,
			Total:   1,
		},
	})

	task := &lib.Task{
		ID:    "t-100",
		State: lib.StateWorking,
		Final: false,
		Artifacts: []lib.Artifact{
			{
				Name:  stepmsg.ArtifactName,
				Parts: []lib.Part{stepPart1, stepPart2},
			},
			{
				Name: lib.ArtifactActivity,
				Parts: []lib.Part{
					mustDataPart(t, map[string]any{
						"tool":          "kubectl",
						"callId":        "c1",
						"title":         "kubectl get nodes -o wide",
						"resultPreview": "3 Ready nodes",
						"worker":        "platform",
					}),
				},
			},
		},
	}

	details, totalActs := CollectTaskStepDetails(ref, task)
	if len(details) != 2 {
		t.Fatalf("len(details) = %d, want 2", len(details))
	}
	if totalActs != 1 {
		t.Fatalf("totalActs = %d, want 1", totalActs)
	}

	// Step 1 streaming envelope (still running).
	title1, md1, res1 := FormatTurnCustomToolEnvelope(TurnStepOutcome{
		Outcome:           TurnOutcome{Root: ref, Task: task},
		Token:             "mcp0123456789abcdef01234567",
		Alias:             "default",
		StepDetails:       details,
		TotalActivities:   totalActs,
		PrevReportedSteps: 0,
		LastReportedSteps: 1,
		PrevReportedActs:  0,
		LastReportedActs:  1,
		Elapsed:           2 * time.Second,
		StillRunning:      true,
	})
	if title1 != "Step 1: Checking cluster nodes and warning events" {
		t.Errorf("title1 = %q, want %q", title1, "Step 1: Checking cluster nodes and warning events")
	}
	for _, want := range []string{
		"- **Agent**: `@platform`",
		"- **Thought**: *I will check the cluster nodes and recent warning events first.*",
		"- **Tool Activity**: `kubectl get nodes -o wide` 3 Ready nodes",
	} {
		if !strings.Contains(md1, want) {
			t.Errorf("md1 missing %q:\n%s", want, md1)
		}
	}
	if !strings.HasPrefix(res1, StreamingStepContinuePrefix) {
		t.Errorf("res1 does not start with %q:\n%s", StreamingStepContinuePrefix, res1)
	}
	if !strings.Contains(res1, `task: "platform/t-100"`) {
		t.Errorf("res1 missing task reference:\n%s", res1)
	}

	// Step 2 terminal envelope (Kanban card + completed).
	task.State = lib.StateCompleted
	task.Final = true
	task.Artifacts = append(task.Artifacts, lib.Artifact{
		Name:  lib.ArtifactResult,
		Parts: []lib.Part{{Kind: "text", Text: "All nodes healthy; 1 crashlooping pod triaged."}},
	})
	title2, md2, res2 := FormatTurnCustomToolEnvelope(TurnStepOutcome{
		Outcome:           TurnOutcome{Root: ref, Task: task},
		Token:             "mcp0123456789abcdef01234567",
		Alias:             "default",
		StepDetails:       details,
		TotalActivities:   totalActs,
		PrevReportedSteps: 1,
		LastReportedSteps: 2,
		PrevReportedActs:  1,
		LastReportedActs:  1,
		Elapsed:           5 * time.Second,
		StillRunning:      false,
	})
	if !strings.Contains(title2, "Kanban Card #42 (ACTIONABLE)") {
		t.Errorf("title2 = %q, want Kanban Card #42 (ACTIONABLE)", title2)
	}
	if !strings.Contains(md2, "**Kanban Specialist Cards**:") {
		t.Errorf("md2 missing Kanban Specialist Cards table:\n%s", md2)
	}
	if strings.HasPrefix(res2, StreamingStepContinuePrefix) {
		t.Errorf("terminal res2 should not start with %q:\n%s", StreamingStepContinuePrefix, res2)
	}
	if !strings.Contains(res2, "All nodes healthy; 1 crashlooping pod triaged.") {
		t.Errorf("terminal res2 missing final answer:\n%s", res2)
	}
}

type fakeStepTurnBus struct {
	ref      TaskRef
	stepCall int
}

func (f *fakeStepTurnBus) SendTurn(_ context.Context, _ *Conversations, token, _ string, _ time.Duration) (TurnResult, *turnWatch, error) {
	r := f.ref
	return TurnResult{Token: token, Task: &r, Notices: []string{"Routing to @platform"}}, nil, nil
}

func (f *fakeStepTurnBus) WaitForTurn(_ context.Context, _ *turnWatch, root TaskRef, _ time.Duration) (TurnOutcome, error) {
	return TurnOutcome{Root: root}, nil
}

func (f *fakeStepTurnBus) WaitForTurnStep(_ context.Context, _ *Conversations, _ *turnWatch, root TaskRef, _ time.Duration) (TurnStepOutcome, error) {
	f.stepCall++
	d := StepDetail{
		Step: stepmsg.Step{
			Schema:  stepmsg.Schema,
			Kind:    stepmsg.KindPlan,
			Title:   "Checking cluster nodes",
			Thought: "Checking node readiness.",
			CallIDs: []string{"c1"},
		},
		Agent:   "platform",
		Thought: "Checking node readiness.",
		Activities: []ActivityItem{
			{Tool: "kubectl", CallID: "c1", Title: "kubectl get nodes"},
		},
	}
	if f.stepCall == 1 {
		return TurnStepOutcome{
			Outcome:           TurnOutcome{Root: root, Task: &lib.Task{ID: root.TaskID, State: lib.StateWorking}},
			Token:             "mcp0123456789abcdef01234567",
			Alias:             "default",
			Notices:           []string{"Routing to @platform"},
			StepDetails:       []StepDetail{d},
			TotalActivities:   1,
			PrevReportedSteps: 0,
			LastReportedSteps: 1,
			PrevReportedActs:  0,
			LastReportedActs:  1,
			Elapsed:           time.Second,
			StillRunning:      true,
		}, nil
	}
	doneTask := &lib.Task{
		ID:    root.TaskID,
		State: lib.StateCompleted,
		Final: true,
		Artifacts: []lib.Artifact{
			{
				Name:  lib.ArtifactResult,
				Parts: []lib.Part{{Kind: "text", Text: "Cluster is healthy."}},
			},
		},
	}
	return TurnStepOutcome{
		Outcome:           TurnOutcome{Root: root, Task: doneTask},
		Token:             "mcp0123456789abcdef01234567",
		Alias:             "default",
		StepDetails:       []StepDetail{d},
		TotalActivities:   1,
		PrevReportedSteps: 1,
		LastReportedSteps: 1,
		PrevReportedActs:  1,
		LastReportedActs:  1,
		Elapsed:           2 * time.Second,
		StillRunning:      false,
	}, nil
}

func (f *fakeStepTurnBus) SendStop(_ context.Context, _ *Conversations, token string) (TurnResult, error) {
	return TurnResult{Token: token}, nil
}

func (f *fakeStepTurnBus) ReadTask(_ context.Context, _ TaskRef) (*lib.Task, error) {
	return nil, nil
}

func (f *fakeStepTurnBus) WaitForTask(_ context.Context, _ TaskRef, _ time.Duration) (*lib.Task, error) {
	return nil, nil
}

func (f *fakeStepTurnBus) ResolveAddressee(_ context.Context, _ string) (string, error) {
	return f.ref.Addressee, nil
}

func TestHandleJSONRPC_ToolsCallMetaEnablesStepStreaming(t *testing.T) {
	bus := &fakeStepTurnBus{ref: TaskRef{Addressee: "platform", TaskID: "t-stream-1"}}
	convs := NewConversations()
	srv, err := NewServer(SendMessageTool(bus, convs), TaskControlTool(bus, convs))
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}

	// 1. send_message with _meta.conversation_id streams Step 1 custom envelope.
	req1 := `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"send_message","arguments":{"text":"check cluster"},"_meta":{"conversation_id":"conv-1"}}}`
	raw1 := srv.HandleJSONRPC(context.Background(), []byte(req1))
	var resp1 struct {
		Result CallToolResult `json:"result"`
	}
	if err := json.Unmarshal(raw1, &resp1); err != nil {
		t.Fatalf("unmarshal resp1: %v", err)
	}
	if len(resp1.Result.Content) != 1 {
		t.Fatalf("len(content) = %d, want 1", len(resp1.Result.Content))
	}
	var env1 CustomToolResultEnvelope
	if err := json.Unmarshal([]byte(resp1.Result.Content[0].Text), &env1); err != nil {
		t.Fatalf("unmarshal custom envelope 1: %v (raw: %s)", err, resp1.Result.Content[0].Text)
	}
	if !env1.CustomToolResult {
		t.Fatalf("env1.CustomToolResult = false, want true")
	}
	if env1.Title != "Step 1: Checking cluster nodes" {
		t.Errorf("env1.Title = %q, want %q", env1.Title, "Step 1: Checking cluster nodes")
	}
	if !strings.HasPrefix(env1.Result, StreamingStepContinuePrefix) {
		t.Errorf("env1.Result missing %q:\n%s", StreamingStepContinuePrefix, env1.Result)
	}

	// 2. task_control get with bare task_id resolves via convs.knownTaskRef and finishes.
	req2 := `{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"task_control","arguments":{"action":"get","task_id":"t-stream-1","wait_timeout_seconds":60},"_meta":{"conversation_id":"conv-1"}}}`
	raw2 := srv.HandleJSONRPC(context.Background(), []byte(req2))
	var resp2 struct {
		Result CallToolResult `json:"result"`
	}
	if err := json.Unmarshal(raw2, &resp2); err != nil {
		t.Fatalf("unmarshal resp2: %v", err)
	}
	var env2 CustomToolResultEnvelope
	if err := json.Unmarshal([]byte(resp2.Result.Content[0].Text), &env2); err != nil {
		t.Fatalf("unmarshal custom envelope 2: %v", err)
	}
	if strings.HasPrefix(env2.Result, StreamingStepContinuePrefix) {
		t.Errorf("env2.Result should be terminal, got:\n%s", env2.Result)
	}
	if !strings.Contains(env2.Result, "Cluster is healthy.") {
		t.Errorf("env2.Result missing final answer:\n%s", env2.Result)
	}
}
