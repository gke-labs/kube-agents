package mcp

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

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
	if title1 != "Checking cluster nodes and warning events" {
		t.Errorf("title1 = %q, want %q", title1, "Checking cluster nodes and warning events")
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
	if env1.Title != "Checking cluster nodes" {
		t.Errorf("env1.Title = %q, want %q", env1.Title, "Checking cluster nodes")
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

func TestHumanizeStepTitleSubtaskStatuses(t *testing.T) {
	cases := []struct {
		name    string
		status  string
		verdict string
		want    string
	}{
		{
			name:   "failed without verdict",
			status: "failed",
			want:   "Kanban Card #7 Failed: Inspect ingress controller",
		},
		{
			name:    "failed with distinct verdict",
			status:  "error",
			verdict: "TIMEOUT",
			want:    "Kanban Card #7 Failed (TIMEOUT): Inspect ingress controller",
		},
		{
			name:   "blocked",
			status: "blocked",
			want:   "Kanban Card #7 Blocked: Inspect ingress controller",
		},
		{
			name:   "gave_up",
			status: "gave_up",
			want:   "Kanban Card #7 Gave up: Inspect ingress controller",
		},
		{
			name:   "canceled",
			status: "canceled",
			want:   "Kanban Card #7 Canceled: Inspect ingress controller",
		},
		{
			name:   "cancelled",
			status: "cancelled",
			want:   "Kanban Card #7 Canceled: Inspect ingress controller",
		},
		{
			name:   "archived",
			status: "archived",
			want:   "Kanban Card #7 Archived: Inspect ingress controller",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := humanizeStepTitle(StepDetail{
				Step: stepmsg.Step{
					Kind: stepmsg.KindSubtask,
					Subtask: &stepmsg.Subtask{
						ID:      "7",
						Title:   "Inspect ingress controller",
						Status:  tc.status,
						Verdict: tc.verdict,
					},
				},
			})
			if got != tc.want {
				t.Errorf("humanizeStepTitle() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestTruncateInlineTextPreservesValidUTF8(t *testing.T) {
	inputs := []string{
		"abc—defghij",  // '—' is 3 bytes at index 3..5
		"abcd🚀efghijk", // '🚀' is 4 bytes at index 4..7
		"• bullet—item🚀end",
	}
	for _, s := range inputs {
		for maxLen := 1; maxLen <= len(s)+2; maxLen++ {
			got := truncateInlineText(s, maxLen)
			if !utf8.ValidString(got) {
				t.Errorf("truncateInlineText(%q, %d) produced invalid UTF-8: %q", s, maxLen, got)
			}
			if len(got) > maxLen {
				t.Errorf("len(truncateInlineText(%q, %d)) = %d > %d (%q)", s, maxLen, len(got), maxLen, got)
			}
		}
	}
}

func TestFormatTurnCustomToolEnvelopeTerminalStates(t *testing.T) {
	ref := TaskRef{Addressee: "platform", TaskID: "t-term"}
	details := []StepDetail{
		{
			Step: stepmsg.Step{
				Schema: stepmsg.Schema,
				Kind:   stepmsg.KindPlan,
				Title:  "Checking control plane pods",
			},
			Agent: "platform",
		},
		{
			Step: stepmsg.Step{
				Schema: stepmsg.Schema,
				Kind:   stepmsg.KindPlan,
				Title:  "Inspecting kube-apiserver logs",
			},
			Agent: "platform",
		},
	}

	cases := []struct {
		state     lib.TaskState
		wantTitle string
	}{
		{
			state:     lib.StateFailed,
			wantTitle: "Failed (2 steps): Inspecting kube-apiserver logs",
		},
		{
			state:     lib.StateCanceled,
			wantTitle: "Canceled (2 steps): Inspecting kube-apiserver logs",
		},
		{
			state:     lib.StateRejected,
			wantTitle: "Rejected (2 steps): Inspecting kube-apiserver logs",
		},
	}

	for _, tc := range cases {
		for _, prevSteps := range []int{0, 1, 2} {
			t.Run(fmt.Sprintf("%s_prev%d", tc.state, prevSteps), func(t *testing.T) {
				task := &lib.Task{
					ID:    ref.TaskID,
					State: tc.state,
					Final: true,
				}
				title, _, _ := FormatTurnCustomToolEnvelope(TurnStepOutcome{
					Outcome:           TurnOutcome{Root: ref, Task: task},
					Token:             "mcp0123456789abcdef01234567",
					Alias:             "default",
					StepDetails:       details,
					PrevReportedSteps: prevSteps,
					LastReportedSteps: 2,
					Elapsed:           3 * time.Second,
					StillRunning:      false,
				})
				if title != tc.wantTitle {
					t.Errorf("title for %s (prevSteps=%d) = %q, want %q", tc.state, prevSteps, title, tc.wantTitle)
				}
				if strings.HasPrefix(title, "Completed") {
					t.Errorf("title for %s (prevSteps=%d) should not start with Completed: %q", tc.state, prevSteps, title)
				}
			})
		}
	}
}

func TestFormatTurnCustomToolEnvelopeUsesRunningFollowedTask(t *testing.T) {
	rootRef := TaskRef{Addressee: "platform", TaskID: "t-root"}
	rootTask := &lib.Task{
		ID:    rootRef.TaskID,
		State: lib.StateCompleted,
		Final: true,
		Artifacts: []lib.Artifact{
			{
				Name:  lib.ArtifactProgress,
				Parts: []lib.Part{{Kind: "text", Text: "delegated to cluster-a"}},
			},
		},
	}
	childRef := TaskRef{Addressee: "cluster-a", TaskID: "t-child1"}
	childTask := &lib.Task{
		ID:    childRef.TaskID,
		State: lib.StateWorking,
		Final: false,
		Artifacts: []lib.Artifact{
			{
				Name:  lib.ArtifactProgress,
				Parts: []lib.Part{{Kind: "text", Text: "inspecting checkout pods"}},
			},
		},
	}

	out := TurnOutcome{
		Root:       rootRef,
		Task:       rootTask,
		AnswerFrom: &rootRef,
		Answer:     rootTask,
		Followed: []FollowedTask{
			{Ref: childRef, Task: childTask},
		},
	}

	title, markdown, _ := FormatTurnCustomToolEnvelope(TurnStepOutcome{
		Outcome:      out,
		Token:        "mcp0123456789abcdef01234567",
		Alias:        "default",
		Elapsed:      2 * time.Second,
		StillRunning: true,
	})

	if title != "inspecting checkout pods" {
		t.Errorf("title = %q, want running child progress 'inspecting checkout pods'", title)
	}
	if !strings.Contains(markdown, "cluster-a") {
		t.Errorf("markdown = %q, want running child agent cluster-a", markdown)
	}
	if !strings.Contains(markdown, "inspecting checkout pods") {
		t.Errorf("markdown = %q, want running child progress 'inspecting checkout pods'", markdown)
	}
}

func TestCollectTaskStepDetailsDoesNotMisattributeThinkingToToolOnlyStep(t *testing.T) {
	ref := TaskRef{Addressee: "platform", TaskID: "t-mixed-thoughts"}
	stepPart1 := mustStepPart(t, stepmsg.Step{
		Kind:    stepmsg.KindPlan,
		Title:   "Checking cluster nodes",
		Thought: "Checking cluster nodes across zones.",
		CallIDs: []string{"c1"},
	})
	stepPart2 := mustStepPart(t, stepmsg.Step{
		Kind:    stepmsg.KindPlan,
		Title:   "Inspecting pod logs",
		CallIDs: []string{"c2"},
	})

	task := &lib.Task{
		ID:    ref.TaskID,
		State: lib.StateWorking,
		Artifacts: []lib.Artifact{
			{
				Name:  stepmsg.ArtifactName,
				Parts: []lib.Part{stepPart1, stepPart2},
			},
			{
				Name:  lib.ArtifactThinking,
				Parts: []lib.Part{{Kind: "text", Text: "Checking cluster nodes across zones."}},
			},
		},
	}

	details, _ := CollectTaskStepDetails(ref, task)
	if len(details) != 2 {
		t.Fatalf("len(details) = %d, want 2", len(details))
	}
	if details[0].Step.Thought != "Checking cluster nodes across zones." {
		t.Errorf("step 1 Thought = %q, want inline thought", details[0].Step.Thought)
	}
	if details[1].Step.Thought != "" {
		t.Errorf("step 2 Thought = %q, want empty (must not inherit thinking[0])", details[1].Step.Thought)
	}
}

func TestFormatTurnCustomToolEnvelopeOmitsEmptyConversationLine(t *testing.T) {
	ref := TaskRef{Addressee: "platform", TaskID: "t-bare"}
	runningTask := &lib.Task{ID: ref.TaskID, State: lib.StateWorking}
	_, _, runningJSON := FormatTurnCustomToolEnvelope(TurnStepOutcome{
		Outcome:      TurnOutcome{Root: ref, Task: runningTask},
		Token:        "",
		Alias:        "default",
		Elapsed:      time.Second,
		StillRunning: true,
	})
	if strings.Contains(runningJSON, "Conversation `") {
		t.Errorf("running envelope with empty token should not include Conversation line:\n%s", runningJSON)
	}

	doneTask := &lib.Task{ID: ref.TaskID, State: lib.StateCompleted, Final: true}
	_, _, doneJSON := FormatTurnCustomToolEnvelope(TurnStepOutcome{
		Outcome:      TurnOutcome{Root: ref, Task: doneTask},
		Token:        "",
		Alias:        "default",
		Elapsed:      time.Second,
		StillRunning: false,
	})
	if strings.Contains(doneJSON, "Conversation `") {
		t.Errorf("completed envelope with empty token should not include Conversation line:\n%s", doneJSON)
	}
}

func TestSummarizeThoughtTitlePreservesDottedTokensAndAbbreviations(t *testing.T) {
	cases := []struct {
		thought string
		want    string
	}{
		{
			thought: "Checking v1.29 cluster nodes. Everything is healthy.",
			want:    "Checking v1.29 cluster nodes.",
		},
		{
			thought: "Inspecting values.yaml in 10.0.0.1",
			want:    "Inspecting values.yaml in 10.0.0.1",
		},
		{
			thought: "Checking pods (e.g. kube-dns) in kube-system. Next I will check logs.",
			want:    "Checking pods (e.g. kube-dns) in kube-system.",
		},
	}

	for _, tc := range cases {
		if got := summarizeThoughtTitle(tc.thought); got != tc.want {
			t.Errorf("summarizeThoughtTitle(%q) = %q, want %q", tc.thought, got, tc.want)
		}
	}
}

func TestFormatTurnCustomToolEnvelopePrefersUnfinishedFollowedTaskWhileRunning(t *testing.T) {
	rootRef := TaskRef{Addressee: "platform", TaskID: "t-root"}
	child1Ref := TaskRef{Addressee: "cluster-a", TaskID: "t-child1"}
	child2Ref := TaskRef{Addressee: "cluster-b", TaskID: "t-child2"}
	rootTask := &lib.Task{ID: rootRef.TaskID, State: lib.StateCompleted, Final: true}
	child1Done := &lib.Task{
		ID:    child1Ref.TaskID,
		State: lib.StateCompleted,
		Final: true,
		Artifacts: []lib.Artifact{
			{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "child 1 done"}}},
		},
	}
	child2Running := &lib.Task{
		ID:    child2Ref.TaskID,
		State: lib.StateWorking,
		Final: false,
	}

	_, md, _ := FormatTurnCustomToolEnvelope(TurnStepOutcome{
		Outcome: TurnOutcome{
			Root:       rootRef,
			Task:       rootTask,
			AnswerFrom: &child1Ref,
			Answer:     child1Done,
			Followed: []FollowedTask{
				{Ref: child1Ref, Task: child1Done},
				{Ref: child2Ref, Task: child2Running},
			},
		},
		StillRunning: true,
		Elapsed:      2 * time.Second,
	})
	if !strings.Contains(md, "cluster-b/t-child2") {
		t.Errorf("in-progress wait card did not attribute to running child cluster-b/t-child2:\n%s", md)
	}
}

func TestSynthesizeWaitStepTitleAndEnvelopeSelectActiveFollowedTask(t *testing.T) {
	rootRef := TaskRef{Addressee: "platform", TaskID: "t-root"}
	child1Ref := TaskRef{Addressee: "cluster-a", TaskID: "t-child1"}
	child2Ref := TaskRef{Addressee: "cluster-b", TaskID: "t-child2"}

	rootDoneWithStaleProgress := &lib.Task{
		ID:    rootRef.TaskID,
		State: lib.StateCompleted,
		Final: true,
		Artifacts: []lib.Artifact{
			{Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "stale root progress"}}},
		},
	}
	child1Running := &lib.Task{
		ID:    child1Ref.TaskID,
		State: lib.StateWorking,
		Final: false,
		Artifacts: []lib.Artifact{
			{Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "inspecting cluster-a nodes"}}},
		},
	}
	child2Done := &lib.Task{
		ID:    child2Ref.TaskID,
		State: lib.StateCompleted,
		Final: true,
		Artifacts: []lib.Artifact{
			{Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "finished cluster-b progress"}}},
		},
	}

	// 1. child1 is still running while child2 (later in slice) is already finished.
	out1 := TurnOutcome{
		Root: rootRef,
		Task: rootDoneWithStaleProgress,
		Followed: []FollowedTask{
			{Ref: child1Ref, Task: child1Running},
			{Ref: child2Ref, Task: child2Done},
		},
	}
	if got := synthesizeWaitStepTitle(out1, nil, 3*time.Second, ""); got != "inspecting cluster-a nodes" {
		t.Errorf("synthesizeWaitStepTitle() = %q, want running child1 progress %q", got, "inspecting cluster-a nodes")
	}
	title1, md1, _ := FormatTurnCustomToolEnvelope(TurnStepOutcome{
		Outcome:      out1,
		StillRunning: true,
		Elapsed:      3 * time.Second,
	})
	if title1 != "inspecting cluster-a nodes" {
		t.Errorf("title1 = %q, want %q", title1, "inspecting cluster-a nodes")
	}
	if !strings.Contains(md1, "`@cluster-a`") {
		t.Errorf("md1 missing @cluster-a badge:\n%s", md1)
	}

	// 2. Pending delegated child (Task == nil, Err == ErrTaskNotFound) while root is finished with stale progress.
	out2 := TurnOutcome{
		Root:       rootRef,
		Task:       rootDoneWithStaleProgress,
		AnswerFrom: &rootRef,
		Answer:     rootDoneWithStaleProgress,
		Followed: []FollowedTask{
			{Ref: child1Ref, Task: nil, Err: ErrTaskNotFound},
		},
	}
	rootDetails := []StepDetail{
		{
			Step:  stepmsg.Step{Kind: stepmsg.KindPlan, Title: "Delegating to cluster-a"},
			Agent: "platform",
		},
	}
	waitTitle2 := synthesizeWaitStepTitle(out2, rootDetails, 4*time.Second, "")
	if waitTitle2 != "Waiting for @cluster-a response (elapsed 4s)" {
		t.Errorf("synthesizeWaitStepTitle(pending child) = %q, want %q", waitTitle2, "Waiting for @cluster-a response (elapsed 4s)")
	}
	title2, md2, _ := FormatTurnCustomToolEnvelope(TurnStepOutcome{
		Outcome:           out2,
		StepDetails:       rootDetails,
		PrevReportedSteps: 1,
		LastReportedSteps: 1,
		StillRunning:      true,
		Elapsed:           4 * time.Second,
	})
	if strings.Contains(title2, "stale root progress") || strings.Contains(md2, "stale root progress") {
		t.Errorf("pending child envelope leaked finished root's stale progress: title=%q md=%q", title2, md2)
	}
	if !strings.Contains(md2, "cluster-a/t-child1") {
		t.Errorf("pending child md2 did not attribute to cluster-a/t-child1:\n%s", md2)
	}
}

func TestCollectTaskStepDetailsUnmatchedActivitiesAndTruncationMarker(t *testing.T) {
	ref := TaskRef{Addressee: "platform", TaskID: "t-unmatched"}
	stepPart1 := mustStepPart(t, stepmsg.Step{
		Kind:    stepmsg.KindPlan,
		Title:   "Analyzing cluster topology",
		Thought: "I will analyze the topology before running commands.",
	})
	stepPart2 := mustStepPart(t, stepmsg.Step{
		Kind:    stepmsg.KindPlan,
		Title:   "Checking nodes",
		CallIDs: []string{"c1"},
	})

	task := &lib.Task{
		ID:    ref.TaskID,
		State: lib.StateWorking,
		Artifacts: []lib.Artifact{
			{
				Name:  stepmsg.ArtifactName,
				Parts: []lib.Part{stepPart1, stepPart2},
			},
			{
				Name: lib.ArtifactActivity,
				Parts: []lib.Part{
					mustDataPart(t, map[string]any{
						"tool":   "kubectl",
						"callId": "c1",
						"title":  "kubectl get nodes",
						"worker": "platform",
					}),
					mustDataPart(t, map[string]any{
						"tool":   "kubectl",
						"callId": "c-sub",
						"title":  "kubectl get pods -A",
						"worker": "subagent-1",
					}),
					mustDataPart(t, map[string]any{
						"tool":    "activity-budget",
						"status":  "truncated",
						"dropped": 7,
					}),
				},
			},
		},
	}

	details, _ := CollectTaskStepDetails(ref, task)
	if len(details) != 3 {
		t.Fatalf("len(details) = %d, want 3 (thought step, matched tool step, synthesized unmatched tool step)", len(details))
	}
	if len(details[0].Activities) != 0 {
		t.Errorf("thought-only step 0 should have 0 activities, got %d", len(details[0].Activities))
	}
	if details[0].Agent != "platform" {
		t.Errorf("step 0 Agent = %q, want %q", details[0].Agent, "platform")
	}
	if details[1].Agent != "platform" || len(details[1].Activities) != 1 || details[1].Activities[0].CallID != "c1" {
		t.Errorf("step 1 = %+v, want platform with c1 activity", details[1])
	}
	if details[2].Agent != "subagent-1" {
		t.Errorf("step 2 Agent = %q, want %q", details[2].Agent, "subagent-1")
	}
	md := FormatStepDetailMarkdown(details[2])
	if !strings.Contains(md, "*(activity trace truncated: 7 entries omitted)*") {
		t.Errorf("step 2 markdown missing truncation note:\n%s", md)
	}
	if strings.Contains(md, "`activity-budget`") {
		t.Errorf("step 2 markdown should not render raw `activity-budget` tool call:\n%s", md)
	}
}
