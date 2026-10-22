package mcp

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

// Rendering: step details, Kanban card state, and the normal/debug envelopes.

func TestNormalAndDebugModeStreamingAndPanels(t *testing.T) {
	now := time.Now().UTC()
	rec := &TaskRecord{
		TaskID:          "task-debug-panel-1",
		ContextID:       "ctx-debug-panel-1",
		Addressee:       "platform",
		Conversation:    "console:tok-dbg",
		State:           lib.StateWorking,
		Terminal:        false,
		CreatedAt:       now.Add(-45 * time.Second),
		UpdatedAt:       now,
		EnvelopeCount:   24,
		SteerTimestamps: []time.Time{now.Add(-15 * time.Second)},
		EnvelopeCountsByKind: map[string]int{
			"message":         1,
			"status-update":   2,
			"artifact-update": 21,
		},
		Thinking: strings.Join([]string{
			"Loading diagnostic playbook.",
			"Creating specialist cards for drift-peer-ap-1 and kube-agents-host.",
			"Re-checking reference section after card dispatch.",
		}, "\n"),
		Steps: []StepRecord{
			{Step: planStep("Reading skill gke-cluster-diagnostics/SKILL.md.", "Loading diagnostic playbook.", "c1"), At: now.Add(-41 * time.Second)},
			{Step: planStep("Dispatching 2 specialist tasks on the Kanban board.", "Creating specialist cards for drift-peer-ap-1 and kube-agents-host.", "c2", "c3"), At: now.Add(-36 * time.Second)},
			{Step: planStep("Reading skill gke-cluster-diagnostics/SKILL.md.", "Re-checking reference section after card dispatch.", "c3b"), At: now.Add(-30 * time.Second)},
			{Step: stepmsg.Step{
				Kind:    stepmsg.KindCard,
				Title:   "Specialist check completed: drift-peer-ap-1.",
				Thought: "Cluster RUNNING (v1.34.1-gke.2037001), 0 pod restarts.",
				Card:    &stepmsg.Card{ID: "t_01", Title: "Live health check for drift-peer-ap-1", Assignee: "cluster-diagnostics", Status: "done", Verdict: "healthy", Summary: "Cluster RUNNING (v1.34.1-gke.2037001), 0 pod restarts.", Settled: 1, Total: 2},
			}, At: now.Add(-9 * time.Second)},
		},
		Activities: []ActivityItem{
			{
				Index:         1,
				Tool:          "read_file",
				Input:         `{"path":"/var/lib/hermes/skills/gke-cluster-diagnostics/SKILL.md"}`,
				CallID:        "c1",
				Status:        "ok",
				ResultPreview: "# GKE Cluster Diagnostics Skill (112 lines)",
				SessionID:     "a2a-platform-1",
				Worker:        "platform",
				DurationMs:    38,
				At:            now.Add(-40 * time.Second).Format(time.RFC3339),
			},
			{
				Index:         2,
				Tool:          "kanban_create",
				Input:         `{"assignee":"cluster-diagnostics","title":"Live health check for drift-peer-ap-1"}`,
				CallID:        "c2",
				Status:        "ok",
				ResultPreview: "card_id=t_01 status=pending",
				CardID:        "t_01",
				SessionID:     "a2a-platform-1",
				Worker:        "platform",
				DurationMs:    19,
				At:            now.Add(-35 * time.Second).Format(time.RFC3339),
			},
			{
				Index:         3,
				Tool:          "kanban_create",
				Input:         `{"assignee":"cluster-diagnostics","title":"Live health check for kube-agents-host"}`,
				CallID:        "c3",
				Status:        "ok",
				ResultPreview: "card_id=t_02 status=pending",
				CardID:        "t_02",
				SessionID:     "a2a-platform-1",
				Worker:        "platform",
				DurationMs:    21,
				At:            now.Add(-34 * time.Second).Format(time.RFC3339),
			},
			{
				Index:         4,
				Tool:          "kanban_done",
				Input:         `{"summary":"Cluster RUNNING, 0 pod restarts","task_id":"t_01","verdict":"healthy"}`,
				CallID:        "c4",
				Status:        "ok",
				ResultPreview: "card_id=t_01 status=done",
				SessionID:     "worker-sess-01",
				Worker:        "card:t_01 (drift-peer-ap-1)",
				CardID:        "t_01",
				DurationMs:    15,
				At:            now.Add(-10 * time.Second).Format(time.RFC3339),
			},
			{
				Index:         5,
				Tool:          "kanban_heartbeat",
				Input:         `{"note":"Checking kubeagents-system pods on kube-agents-host","task_id":"t_02"}`,
				CallID:        "c5",
				Status:        "ok",
				ResultPreview: "heartbeat ok",
				SessionID:     "worker-sess-02",
				Worker:        "card:t_02 (kube-agents-host)",
				CardID:        "t_02",
				DurationMs:    12,
				At:            now.Add(-3 * time.Second).Format(time.RFC3339),
			},
		},
	}

	outcome := &TurnOutcome{
		ConversationAlias:     "default",
		ConversationToken:     "tok-dbg",
		PrimaryTask:           rec,
		TimedOut:              true,
		PrevStepCount:         4,
		ReportedStepCount:     4,
		PrevActivityCount:     4,
		ReportedActivityCount: 5,
	}

	// Verify A2: Consecutive-only coalescing keeps both non-consecutive "Reading skill..." steps (4 total steps, not 3).
	details := CollectOutcomeStepDetails(outcome)
	if len(details) != 4 {
		t.Fatalf("expected 4 steps with consecutive-only coalescing, got %d: %+v", len(details), details)
	}

	// Verify A1: When a poll times out with no new step (PrevStepCount == ReportedStepCount == 4),
	// it shows live Kanban card progress + delta activity (c5) and NEVER repeats c1 (`read_file`).
	waitTitle, waitMD, _ := FormatTurnCustomToolEnvelope(outcome, false)
	if !strings.Contains(waitTitle, "Checking kubeagents-system pods on kube-agents-host (1/2 cards done)") {
		t.Fatalf("unexpected waitTitle: %q", waitTitle)
	}
	if strings.Contains(waitMD, "read_file") {
		t.Fatalf("wait step markdown repeated stale read_file from earlier step:\n%s", waitMD)
	}
	if !strings.Contains(waitMD, "kanban_heartbeat") || !strings.Contains(waitMD, "Checking kubeagents-system pods on kube-agents-host") {
		t.Fatalf("wait step markdown missing delta kanban_heartbeat activity:\n%s", waitMD)
	}

	// Verify B1 + B2 + B3 in Debug Mode when task completes: all 4 collapsible panels render.
	rec.State = lib.StateCompleted
	rec.Terminal = true
	rec.ResultText = "Both clusters are healthy."
	outcome.TimedOut = false
	_, dbgMD, _ := FormatTurnCustomToolEnvelope(outcome, true)
	for _, needle := range []string{
		"Bus Envelope &amp; Task Telemetry",
		"Kanban Specialist Cards",
		"Full Tool Activity Stream",
		"Raw LLM API Turn Log",
		"card:t_01 (drift-peer-ap-1)",
		"# GKE Cluster Diagnostics Skill (112 lines)",
		"Both clusters are healthy.",
	} {
		if !strings.Contains(dbgMD, needle) {
			t.Errorf("debug mode output missing %q:\n%s", needle, dbgMD)
		}
	}
}

func TestDedupeNonAdjacentStepsAndBoundDebugOutput(t *testing.T) {
	// 1. Alternating step records A, B, A, B across >8 records collapse to A, B with merged
	// thoughts, and each activity files under the step that planned its call id.
	var alt []stepmsg.Step
	for _, c := range []string{"drift-peer-ap-1", "drift-peer-ap-2"} {
		alt = append(alt,
			planStep("Checking Kubernetes node status and readiness.", "Checking "+c+" nodes.", "nodes-"+c),
			planStep("Inspecting Kubernetes pods and workload health.", "Checking "+c+" pods.", "pods-"+c),
			planStep("Checking Kubernetes node status and readiness.", "Re-checking "+c+" nodepools.", "pools-"+c),
			planStep("Inspecting Kubernetes pods and workload health.", "Re-checking "+c+" events.", "events-"+c),
		)
	}
	alt = append(alt, planStep("Checking Kubernetes node status and readiness.", "Fleet-wide live status and node count check (16).", "fleet"))
	rec := &TaskRecord{
		Steps: stepRecords(alt...),
		Activities: []ActivityItem{
			{Tool: "kubectl", CallID: "pods-drift-peer-ap-2", Status: "completed", At: "2026-10-17T12:00:03Z"},
			{Tool: "kubectl", CallID: "fleet", Status: "completed", At: "2026-10-17T12:00:09Z"},
			{Tool: "kubectl", CallID: "unplanned", Status: "completed", At: "2026-10-17T12:00:02Z"},
		},
	}
	// Nine alternating records fold to four rows: a title keeps its first repeat as its own
	// row (the turn came back to it) and later repeats fold into the latest row of that title,
	// decided per record so a streamed list never shrinks.
	steps := collectRecStepDetails(rec)
	if len(steps) != 4 || steps[0].Title != steps[2].Title || steps[1].Title != steps[3].Title {
		t.Fatalf("expected 4 rows (A, B, A, B) from alternating records, got %d: %+v", len(steps), steps)
	}
	if !strings.Contains(steps[2].Thought, "drift-peer-ap-1 nodepools") || !strings.Contains(steps[2].Thought, "drift-peer-ap-2 nodepools") || !strings.Contains(steps[2].Thought, "Fleet-wide") {
		t.Errorf("expected later repeats merged into the latest A row, got %q", steps[2].Thought)
	}
	if len(steps[2].Activities) != 2 || steps[2].Activities[0].CallID != "fleet" || steps[2].Activities[1].CallID != "unplanned" {
		t.Errorf("expected the fleet call (by call id) and the unplanned call (by time: the latest row started by 12:00:02) under the second A row, got %+v", steps[2].Activities)
	}
	if len(steps[3].Activities) != 1 || steps[3].Activities[0].CallID != "pods-drift-peer-ap-2" {
		t.Errorf("expected the pods call to file under the row its record folded into (the second B row), got %+v", steps[3].Activities)
	}
	// A list rebuilt from any prefix of the records is a prefix of the full list: nothing a
	// client already showed is taken back.
	for n := 1; n < len(rec.Steps); n++ {
		prefix := collectRecStepDetails(&TaskRecord{Steps: rec.Steps[:n]})
		if len(prefix) > len(steps) {
			t.Fatalf("prefix of %d records yields %d rows, more than the full %d", n, len(prefix), len(steps))
		}
		for i := range prefix {
			if prefix[i].Title != steps[i].Title {
				t.Fatalf("row %d changed title between prefix %d (%q) and the full list (%q)", i, n, prefix[i].Title, steps[i].Title)
			}
		}
	}

	// 2. Large 16-cluster fan-out (>40 step records + 235 tool calls) keeps every specialist
	// completion as its own row while repeated diagnostic titles share one, and the debug
	// rendering of the record stays under 18.5 KB.
	var big []stepmsg.Step
	for i := 1; i <= 16; i++ {
		c := fmt.Sprintf("drift-peer-std-%d", i)
		big = append(big,
			planStep("Checking Kubernetes node status and readiness.", "Nodes check on "+c+"."),
			planStep("Inspecting Kubernetes pods, nodes, and cluster events.", "Pods check on "+c+"."),
			planStep("Inspecting files in the workspace.", "Workspace check on "+c+"."),
			stepmsg.Step{Kind: stepmsg.KindCard, Title: "Specialist check completed: " + c + ".", Thought: "Cluster " + c + " is healthy.",
				Card: &stepmsg.Card{ID: fmt.Sprintf("t_%02d", i), Title: "Live health check for " + c, Status: "done", Verdict: "healthy", Settled: i, Total: 16}},
		)
	}
	var acts []ActivityItem
	for i := 1; i <= 235; i++ {
		acts = append(acts, ActivityItem{
			Index:         i,
			Tool:          "kubectl",
			Input:         fmt.Sprintf(`{"command":"kubectl get nodes -o wide --context=cluster-%d"}`, i),
			Status:        "completed",
			Worker:        fmt.Sprintf("card:t_%02d (drift-peer-std-%d)", (i%16)+1, (i%16)+1),
			CardID:        fmt.Sprintf("t_%02d", (i%16)+1),
			SessionID:     fmt.Sprintf("20261017_sess_%02d", (i%16)+1),
			DurationMs:    340,
			At:            "2026-10-17T15:10:00Z",
			ResultPreview: "NAME STATUS ROLES AGE VERSION gke-node-1 Ready <none> 4d v1.33.5-gke.1100",
		})
	}
	rec = &TaskRecord{
		TaskID:            "task-big-fanout",
		ContextID:         "ctx-big",
		Conversation:      "console:soak",
		Addressee:         "platform",
		State:             lib.StateCompleted,
		Terminal:          true,
		Steps:             stepRecords(big...),
		Activities:        acts,
		IntermediateTurns: []string{strings.Repeat("Raw card report line. ", 600)},
		ResultText:        "All 16 clusters are checked.",
		EnvelopeCount:     310,
	}
	parsedBig := collectRecStepDetails(rec)
	if len(parsedBig) != 22 { // 3 diagnostic titles x 2 rows each + 16 specialist completion rows
		t.Fatalf("expected 22 consolidated steps (3 diagnostic titles x 2 rows + 16 specialist completions), got %d", len(parsedBig))
	}
	cards, done, running, total, _ := extractKanbanCardsFromTask(rec)
	if len(cards) != 16 || done != 16 || running != 0 || total != 16 || cards[0].Verdict != "healthy" {
		t.Fatalf("expected 16 settled cards from the card step records, got cards=%d done=%d running=%d total=%d", len(cards), done, running, total)
	}
	debugMD := FormatTaskRecordMarkdownWithOptions(rec, "Primary Task", true)
	if len(debugMD) > 18500 {
		t.Fatalf("expected 16-cluster debug markdown <= 18.5 KB to prevent output.txt spill, got %d bytes", len(debugMD))
	}

	// 3. Verify 48-char remote-truncated card step titles ("Live state pull: example-gitops namespaces for.")
	// expand cleanly from full card titles without dangling prepositions or double-dot ("..") artifacts.
	driftRec := &TaskRecord{
		TaskID:    "task-drift-1",
		ContextID: "ctx-drift-1",
		Addressee: "platform",
		State:     lib.StateWorking,
		Terminal:  false,
		Steps: []StepRecord{
			{
				Step: planStep("Delegating specialist task: Live state pull: example-gitops namespaces for.", "Creating card for namespace pull."),
				At:   time.Date(2026, 10, 18, 23, 23, 18, 0, time.UTC),
			},
			{
				Step: stepmsg.Step{
					Kind:    stepmsg.KindCard,
					Title:   "Specialist check completed: Live state pull: example-gitops namespaces for.",
					Thought: "Live state matches declared manifests across all four namespaces.",
					Card: &stepmsg.Card{
						ID:      "t_1061d0b3",
						Title:   "Live state pull: example-gitops namespaces for drift check",
						Status:  "done",
						Verdict: "done",
						Settled: 1,
						Total:   2,
					},
				},
				At: time.Date(2026, 10, 18, 23, 25, 0, 0, time.UTC),
			},
		},
		Activities: []ActivityItem{
			{
				Index:  1,
				Tool:   "kanban_create",
				Input:  `{"assignee":"cluster-diagnostics","title":"Live state pull: example-gitops namespaces for drift check"}`,
				CardID: "t_1061d0b3",
				Status: "ok",
				At:     "2026-10-18T23:23:18Z",
			},
			{
				Index:  2,
				Tool:   "kanban_heartbeat",
				Input:  `{"note":"Live-state pull complete for all 4 namespaces; writing report now.","task_id":"t_1061d0b3"}`,
				CardID: "t_1061d0b3",
				Status: "ok",
				At:     "2026-10-18T23:24:40Z",
			},
		},
	}
	driftSteps := collectRecStepDetails(driftRec)
	if len(driftSteps) != 2 {
		t.Fatalf("expected 2 drift steps, got %d: %+v", len(driftSteps), driftSteps)
	}
	if driftSteps[0].Title != "Delegating specialist task: Live state pull: example-gitops namespaces for drift check." {
		t.Errorf("expected expanded plan step title without dangling 'for.', got %q", driftSteps[0].Title)
	}
	if driftSteps[1].Title != "Specialist check completed: Live state pull: example-gitops namespaces for drift check (done — 1/2 cards done)." {
		t.Errorf("expected expanded card completion title without dangling 'for', got %q", driftSteps[1].Title)
	}
	waitOut := &TurnOutcome{
		PrimaryTask:           driftRec,
		TimedOut:              true,
		PrevStepCount:         2,
		ReportedStepCount:     2,
		PrevActivityCount:     1,
		ReportedActivityCount: 2,
	}
	waitHbTitle, _, waitHbLLM := FormatTurnCustomToolEnvelope(waitOut, false)
	if strings.Contains(waitHbTitle, "no..") || strings.Contains(waitHbLLM, "no..") {
		t.Errorf("expected word-boundary truncation without double-dot 'no..', got title=%q llm=%q", waitHbTitle, waitHbLLM)
	}
}
