package mcp

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

// BusSession ingestion: envelope folding, task linking, replay order, and bounds.

func TestWireActivityObjectAndConsoleBoolEdit(t *testing.T) {
	item, ok := parseActivityItem(`{"tool":"mcp__gke__list_clusters","input":{"parent":"\u003cstring, 35 chars\u003e"},"callId":"call-99","status":"completed","durationMs":2337}`)
	if !ok {
		t.Fatal("expected parseActivityItem to succeed on JSON object input")
	}
	if item.Tool != "mcp__gke__list_clusters" || item.CallID != "call-99" || item.DurationMs != 2337 {
		t.Fatalf("unexpected parsed ActivityItem: %+v", item)
	}
	if !strings.Contains(item.Input, "<string, 35 chars>") {
		t.Fatalf("expected unescaped angle brackets in Input, got %q", item.Input)
	}

	s := &BusSession{}
	s.ingestConsoleOut("chat.console.tok1.out", []byte(`{"messageId":"m-1","text":"⏳ submitted…"}`))
	s.ingestConsoleOut("chat.console.tok1.out", []byte(`{"messageId":"m-1","text":"🔧 running kubectl get nodes…","edit":true}`))
	notices := s.consoleNoticesSince("tok1", 0)
	if len(notices) != 1 || notices[0].Text != "🔧 running kubectl get nodes…" {
		t.Fatalf("expected in-place edited console notice, got %+v", notices)
	}
}

func TestViaAuthorityDelegationAndSynthesisStepOrdering(t *testing.T) {
	s := &BusSession{
		tasks:         make(map[string]*TaskRecord),
		seenEnvelopes: make(map[string]bool),
	}
	now := time.Now().UTC()

	// 1. Ingest parent /session task that emits ArtifactDelegate to platform.
	s.ingestTaskEnvelope("a2a.tasks.chat-puffin-1.task-parent-via.in", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-1",
		TS:         now,
		TaskID:     "task-parent-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindMessage,
		Authority:  mustJSON(map[string]any{"audience": map[string]any{"conversation": "console:tok"}}),
		Payload:    mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "check all clusters"}}}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.chat-puffin-1.task-parent-via.out", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-2",
		TS:         now.Add(1 * time.Second),
		TaskID:     "task-parent-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID:   "task-parent-via",
			Artifact: lib.Artifact{Name: lib.ArtifactDelegate, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"addressee":"platform","text":"Run fleet health check across 16 clusters"}`)}}},
		}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.chat-puffin-1.task-parent-via.out", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-3",
		TS:         now.Add(2 * time.Second),
		TaskID:     "task-parent-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindStatusUpdate,
		Payload:    mustJSON(lib.StatusUpdate{TaskID: "task-parent-via", Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
	}), true)

	// 2. Ingest delegated child task on platform using gateway's real `authority.via.taskId` wire format,
	// with conversational thinking lines and an intermediate "Synthesizing findings into final response." before Kanban steps.
	s.ingestTaskEnvelope("a2a.tasks.platform.task-child-via.in", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-4",
		TS:         now.Add(3 * time.Second),
		TaskID:     "task-child-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindMessage,
		Authority: mustJSON(map[string]any{
			"audience": map[string]any{"conversation": "console:tok"},
			"via":      map[string]any{"taskId": "task-parent-via", "session": "chat-puffin-1"},
		}),
		Payload: mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "Run fleet health check across 16 clusters"}}}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.platform.task-child-via.out", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-5",
		TS:         now.Add(10 * time.Second),
		TaskID:     "task-child-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID: "task-child-via",
			Append: true,
			Artifact: stepsArtifact(
				planStep("Listing GKE cluster profiles in the fleet.", ""),
				stepmsg.Step{Kind: stepmsg.KindSynthesis, Title: "Synthesizing findings into final response."},
				planStep("Preflight ok.", "Preflight ok. Now let's run diagnostics"),
				planStep("12/16 done, 4 still running.", "12/16 done, 4 still running. Wait 25s and re-check."),
				stepmsg.Step{Kind: stepmsg.KindSynthesis, Title: "Synthesizing findings into final response."},
			),
		}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.platform.task-child-via.out", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-6",
		TS:         now.Add(20 * time.Second),
		TaskID:     "task-child-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindStatusUpdate,
		Payload:    mustJSON(lib.StatusUpdate{TaskID: "task-child-via", Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
	}), true)

	// 3. Ingest gateway's real wake synthesis prompt ("The task you delegated to platform (task task-child-via) returned:...").
	s.ingestTaskEnvelope("a2a.tasks.chat-puffin-1.task-wake-via.in", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-7",
		TS:         now.Add(21 * time.Second),
		TaskID:     "task-wake-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindMessage,
		Authority:  mustJSON(map[string]any{"audience": map[string]any{"conversation": "console:tok"}}),
		Payload:    mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "You were asked:\n```\ncheck all clusters\n```\n\nYou delegated to platform (task task-child-via), which returned:\n\n16/16 clusters healthy."}}}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.chat-puffin-1.task-wake-via.out", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-8",
		TS:         now.Add(25 * time.Second),
		TaskID:     "task-wake-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID:   "task-wake-via",
			Artifact: lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "All 16 clusters are healthy."}}},
		}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.chat-puffin-1.task-wake-via.out", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-9",
		TS:         now.Add(26 * time.Second),
		TaskID:     "task-wake-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindStatusUpdate,
		Payload:    mustJSON(lib.StatusUpdate{TaskID: "task-wake-via", Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
	}), true)

	if !s.IsTaskTreeSettled("task-parent-via") {
		t.Fatal("expected task tree to be settled once parent, via-child, and wake tasks complete")
	}
	outcome := s.BuildTaskOutcome("task-parent-via", "fleet-health", "tok", "check all clusters", false)
	if len(outcome.ChildTasks) != 1 || outcome.ChildTasks[0].TaskID != "task-child-via" {
		t.Fatalf("expected child task linked via authority.via.taskId, got %+v", outcome.ChildTasks)
	}
	if outcome.WakeTask == nil || outcome.WakeTask.TaskID != "task-wake-via" {
		t.Fatalf("expected wake task linked via gateway wake prompt, got %+v", outcome.WakeTask)
	}

	details := CollectOutcomeStepDetails(outcome)
	wantTitles := []string{
		"Delegating task to platform.",
		"Listing GKE cluster profiles in the fleet.",
		"Preflight ok.",
		"12/16 done, 4 still running.",
		"Synthesizing findings into final response.",
	}
	if len(details) != len(wantTitles) {
		t.Fatalf("got %d step details (%+v), want %d (%v)", len(details), details, len(wantTitles), wantTitles)
	}
	for i, want := range wantTitles {
		if details[i].Title != want {
			t.Errorf("step[%d].Title = %q, want %q", i, details[i].Title, want)
		}
	}
	if details[2].Thought != "Preflight ok. Now let's run diagnostics" {
		t.Errorf("step[2].Thought = %q, want conversational line preserved in Thought", details[2].Thought)
	}
	if details[3].Thought != "12/16 done, 4 still running. Wait 25s and re-check." {
		t.Errorf("step[3].Thought = %q, want progress detail preserved in Thought", details[3].Thought)
	}
}

// TestKanbanCardTasksLinkToForegroundTaskAndStreamAsSteps reproduces the 2026-10-16 "check the fresh
// health of all my clusters" session: the platform agent fanned out 15 task-card-* specialist tasks
// in the same conversation. Before the fix they (a) fired 15 task_completed push events that were
// piggybacked onto a STREAMING_STEP_CONTINUE frame (12KB), (b) were invisible as progress, and (c)
// a steer reset the step cursor so all 18 steps re-streamed as duplicates.
func TestKanbanCardTasksLinkToForegroundTaskAndStreamAsSteps(t *testing.T) {
	var pushed []PushEvent
	s := &BusSession{
		tasks:             make(map[string]*TaskRecord),
		seenEnvelopes:     make(map[string]bool),
		foregroundTasks:   make(map[string]time.Time),
		watchedTasks:      make(map[string]int),
		watchedConvs:      make(map[string]int),
		lastReportedSteps: make(map[string]int),
		bootstrapped:      true,
		onPushEvent:       func(ev PushEvent) { pushed = append(pushed, ev) },
	}
	now := time.Now().UTC()
	conv := "console:agy-tok"

	// Foreground platform task, being polled by task_control(get) (foreground lease, no conv watch).
	s.ingestTaskEnvelope("a2a.tasks.platform.task-fg.in", mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "fg-1", TS: now, TaskID: "task-fg", ContextID: "ctx-fg",
		Kind:      lib.KindMessage,
		Authority: mustJSON(map[string]any{"audience": map[string]any{"conversation": conv}}),
		Payload:   mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "check the fresh health of all my clusters"}}}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.platform.task-fg.out", mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "fg-2", TS: now.Add(time.Second), TaskID: "task-fg", ContextID: "ctx-fg",
		Kind:    lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{TaskID: "task-fg", Artifact: lib.Artifact{Name: lib.ArtifactThinking, Parts: []lib.Part{{Kind: "text", Text: "Running kanban_create.\nWaiting for specialist agent tasks to complete."}}}}),
	}), true)
	s.markForegroundStepTask("task-fg", false)

	// Two Kanban card tasks in the same conversation, no authority.via / delegatedFrom link.
	for i, name := range []string{"drift-peer-std-9", "ap-ap-deploy-test"} {
		id := fmt.Sprintf("task-card-t-%d", i)
		base := now.Add(time.Duration(10+i) * time.Second)
		s.ingestTaskEnvelope("a2a.tasks.platform."+id+".in", mustJSON(lib.Envelope{
			Protocol: lib.Protocol, EnvelopeID: id + "-1", TS: base, TaskID: id, ContextID: "ctx-fg",
			Kind:      lib.KindMessage,
			Authority: mustJSON(map[string]any{"audience": map[string]any{"conversation": conv}}),
			Payload:   mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "Live health check for cluster " + name}}}),
		}), true)
		s.ingestTaskEnvelope("a2a.tasks.platform."+id+".out", mustJSON(lib.Envelope{
			Protocol: lib.Protocol, EnvelopeID: id + "-2", TS: base.Add(20 * time.Second), TaskID: id, ContextID: "ctx-fg",
			Kind:    lib.KindArtifactUpdate,
			Payload: mustJSON(lib.ArtifactUpdate{TaskID: id, Artifact: lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "**Cluster `" + name + "` is healthy.**\n\nLive checks: 2/2 nodes Ready, 0 pods pending."}}}}),
		}), true)
		s.ingestTaskEnvelope("a2a.tasks.platform."+id+".out", mustJSON(lib.Envelope{
			Protocol: lib.Protocol, EnvelopeID: id + "-3", TS: base.Add(21 * time.Second), TaskID: id, ContextID: "ctx-fg",
			Kind:    lib.KindStatusUpdate,
			Payload: mustJSON(lib.StatusUpdate{TaskID: id, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
		}), true)
	}

	if got := s.GetTask("task-card-t-0").CardParentTaskID; got != "task-fg" {
		t.Fatalf("card task not linked to foreground task: CardParentTaskID=%q", got)
	}
	if len(pushed) != 0 {
		t.Errorf("card completions must not fire push events while the parent is in the foreground; got %d: %+v", len(pushed), pushed)
	}

	outcome := s.BuildTaskOutcome("task-fg", "default", "agy-tok", "check the fresh health of all my clusters", true)
	if len(outcome.CardTasks) != 2 {
		t.Fatalf("expected 2 card tasks on outcome, got %d", len(outcome.CardTasks))
	}
	steps := CollectOutcomeSteps(outcome)
	joined := strings.Join(steps, "\n")
	for _, want := range []string{
		"Specialist check completed: Cluster `drift-peer-std-9` is healthy.",
		"Specialist check completed: Cluster `ap-ap-deploy-test` is healthy.",
	} {
		if !strings.Contains(joined, want) {
			t.Errorf("card progress missing from steps; want %q in:\n%s", want, joined)
		}
	}
	details := CollectOutcomeStepDetails(outcome)
	var cardMD string
	for _, d := range details {
		if strings.HasPrefix(d.Title, "Specialist check completed: Cluster `drift-peer-std-9`") {
			cardMD = FormatStepDetailMarkdown(d, 30, true)
		}
	}
	if !strings.Contains(cardMD, "(task `task-card-t-0`)") || !strings.Contains(cardMD, "2/2 nodes Ready") {
		t.Errorf("expanded card step should carry card task id and result thought:\n%s", cardMD)
	}

	// STREAMING_STEP_CONTINUE frame with no new step must say so and report card progress.
	s.recordStepProgress("task-fg", len(steps), len(steps))
	outcome = s.BuildTaskOutcome("task-fg", "default", "agy-tok", "", true)
	_, _, llm := FormatTurnCustomToolEnvelope(outcome, false)
	for _, want := range []string{streamingStepContinuePrefix, "No new step since the last poll", "Specialist cards: 2/2 completed", "Do NOT call `list`, `steer`"} {
		if !strings.Contains(llm, want) {
			t.Errorf("no-new-step frame missing %q:\n%s", want, llm)
		}
	}

	// Piggyback is bounded.
	var many []PushEvent
	for i := 0; i < 15; i++ {
		many = append(many, PushEvent{Kind: eventKindTaskCompleted, Title: fmt.Sprintf("card %d", i), Summary: strings.Repeat("x", 900)})
	}
	md := FormatPushEventsMarkdownBounded(many, maxPiggybackEvents, piggybackSummaryMaxChars)
	if strings.Count(md, "task_completed") != maxPiggybackEvents || !strings.Contains(md, "+ 9 earlier events omitted") || len(md) > 4000 {
		t.Errorf("bounded piggyback not bounded (len=%d, events=%d):\n%s", len(md), strings.Count(md, "task_completed"), md)
	}
}

// TestStepCursorSurvivesSessionRestart reproduces soak round 4 (T13): Antigravity relaunches the MCP
// server after a crash/upgrade and the fresh process re-streamed all 17 already-shown steps as
// duplicate rows ("1 step recorded" right after "17 steps recorded") because the cursor lived
// only in memory. It also pins CreatedAt back-dating when live delivery races the bootstrap.
func TestStepCursorSurvivesSessionRestart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "step-progress.json")
	s1 := &BusSession{tasks: make(map[string]*TaskRecord), seenEnvelopes: make(map[string]bool)}
	s1.EnableStepProgressPersistence(path)
	s1.recordStepProgress("task-x", 16, 17)
	s1.recordStepProgress("task-y", 0, 3)

	s2 := &BusSession{tasks: make(map[string]*TaskRecord), seenEnvelopes: make(map[string]bool)}
	s2.EnableStepProgressPersistence(path)
	if got := s2.getReportedStepCount("task-x"); got != 17 {
		t.Fatalf("cursor for task-x not restored: got %d want 17", got)
	}
	if prev, rep := s2.getStepProgress("task-y"); prev != 0 || rep != 3 {
		t.Fatalf("cursor for task-y not restored: got %d/%d", prev, rep)
	}

	// Live envelope first (TS now), then the earlier history arrives via bootstrap.
	now := time.Now().UTC()
	s2.ingestTaskEnvelope("a2a.tasks.platform.task-late.out", mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "late-2", TS: now, TaskID: "task-late", ContextID: "ctx",
		Kind:    lib.KindStatusUpdate,
		Payload: mustJSON(lib.StatusUpdate{TaskID: "task-late", Status: lib.TaskStatus{State: lib.StateWorking}}),
	}), true)
	s2.ingestTaskEnvelope("a2a.tasks.platform.task-late.in", mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "late-1", TS: now.Add(-3 * time.Minute), TaskID: "task-late", ContextID: "ctx",
		Kind:    lib.KindMessage,
		Payload: mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "hi"}}}),
	}), false)
	if got := s2.GetTask("task-late").CreatedAt; !got.Equal(now.Add(-3 * time.Minute)) {
		t.Fatalf("CreatedAt should be back-dated to the submission, got %v", got)
	}
}

func TestOutOfOrderBootstrapReplayDoesNotCorruptCompletedTask(t *testing.T) {
	s := &BusSession{
		tasks:             make(map[string]*TaskRecord),
		seenEnvelopes:     make(map[string]bool),
		foregroundTasks:   make(map[string]time.Time),
		watchedTasks:      make(map[string]int),
		watchedConvs:      make(map[string]int),
		lastReportedSteps: make(map[string]int),
		bootstrapped:      true,
	}
	t0 := time.Date(2026, 10, 17, 14, 0, 30, 0, time.UTC)
	tEnd := t0.Add(514 * time.Second)
	taskID := "task-24c523717a80ef25"
	outSubj := "a2a.tasks.platform." + taskID + ".out"
	inSubj := "a2a.tasks.platform." + taskID + ".in"

	// 1. Live subscription receives the final completion envelopes FIRST (at tEnd = 14:09:04Z).
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-live-act-38", TS: tEnd.Add(-2 * time.Second), TaskID: taskID, ContextID: "ctx-1",
		Kind: lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID: taskID, Append: true,
			Artifact: lib.Artifact{Name: lib.ArtifactActivity, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"tool":"kanban_complete","input":{"id":"t_9ebe3ccb","result":"Scaled store-v1 to 2 replicas in PR #225"},"callId":"call-38","status":"completed","sessionId":"20261017_140036_e0c3f7","worker":"card:t_9ebe3ccb","durationMs":40,"at":"2026-10-17T14:09:02Z"}`)}}},
		}),
	}), true)
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-live-think-end", TS: tEnd.Add(-time.Second), TaskID: taskID, ContextID: "ctx-1",
		Kind: lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID: taskID, Append: true,
			Artifact: stepsArtifact(stepmsg.Step{
				Kind:    stepmsg.KindCard,
				Title:   "Specialist check completed: Increase replica count on a workload.",
				Thought: "Opened PR #225 scaling store-v1 from 1 to 2 replicas.",
				Card:    &stepmsg.Card{ID: "t_9ebe3ccb", Title: "Increase replica count on a workload", Status: "done", Verdict: "completed", Summary: "Opened PR #225 scaling store-v1 from 1 to 2 replicas.", Settled: 1, Total: 1},
			}),
		}),
	}), true)
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-live-res", TS: tEnd, TaskID: taskID, ContextID: "ctx-1",
		Kind: lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID: taskID, Append: false,
			Artifact: lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "Increased replica count on `store-v1` from 1 to 2 via PR #225."}}},
		}),
	}), true)
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-live-done", TS: tEnd, TaskID: taskID, ContextID: "ctx-1",
		Kind:    lib.KindStatusUpdate,
		Payload: mustJSON(lib.StatusUpdate{TaskID: taskID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
	}), true)

	// 2. Background stream bootstrap replays the OLDER start-of-task envelopes AFTER completion (at t0 = 14:00:30Z).
	s.ingestTaskEnvelope(inSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-boot-msg", TS: t0, TaskID: taskID, ContextID: "ctx-1",
		Kind:    lib.KindMessage,
		Payload: mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "increase replica count on a workload"}}}),
	}), false)
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-boot-working", TS: t0.Add(time.Second), TaskID: taskID, ContextID: "ctx-1",
		Kind:    lib.KindStatusUpdate,
		Payload: mustJSON(lib.StatusUpdate{TaskID: taskID, Status: lib.TaskStatus{State: lib.StateWorking}, Final: false}),
	}), false)
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-boot-think-1", TS: t0.Add(4 * time.Second), TaskID: taskID, ContextID: "ctx-1",
		Kind: lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID: taskID, Append: false,
			Artifact: stepsArtifact(planStep("Delegating specialist task: Increase replica count on a workload.", "Creating Kanban card.")),
		}),
	}), false)
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-boot-act-1", TS: t0.Add(5 * time.Second), TaskID: taskID, ContextID: "ctx-1",
		Kind: lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID: taskID, Append: false,
			Artifact: lib.Artifact{Name: lib.ArtifactActivity, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"tool":"kanban_create","input":{"assignee":"platform","title":"Increase replica count on a workload"},"callId":"call-1","status":"completed","resultPreview":"Created task t_9ebe3ccb: Increase replica count on a workload","sessionId":"a2a-ctx-1","worker":"platform","durationMs":22,"at":"2026-10-17T14:00:35Z"}`)}}},
		}),
	}), false)

	rec := s.GetTask(taskID)
	if !rec.Terminal || rec.State != lib.StateCompleted {
		t.Fatalf("expected task to remain completed/terminal after out-of-order replay, got state=%s terminal=%v", rec.State, rec.Terminal)
	}
	if len(rec.Steps) != 2 || !strings.HasPrefix(rec.Steps[0].Title, "Delegating specialist task") || !strings.HasPrefix(rec.Steps[1].Title, "Specialist check completed") {
		t.Fatalf("expected both initial and final step records preserved in chronological order, got: %+v", rec.Steps)
	}
	if len(rec.Activities) != 2 || rec.Activities[0].Tool != "kanban_create" || rec.Activities[1].Tool != "kanban_complete" {
		t.Fatalf("expected activities sorted chronologically [kanban_create, kanban_complete], got: %+v", rec.Activities)
	}
	cards, doneCount, runningCount, totalCount, _ := extractKanbanCardsFromTask(rec)
	if totalCount != 1 || doneCount != 1 || runningCount != 0 || cards[0].ID != "t_9ebe3ccb" {
		t.Fatalf("expected 1 completed card t_9ebe3ccb (no trailing colon duplicate), got total=%d done=%d running=%d cards=%+v",
			totalCount, doneCount, runningCount, cards)
	}
}

func TestBoundedSessionMapEvictionAndGenericStepFallback(t *testing.T) {
	s := &BusSession{
		tasks:             make(map[string]*TaskRecord),
		seenEnvelopes:     make(map[string]bool),
		foregroundTasks:   make(map[string]time.Time),
		watchedTasks:      make(map[string]int),
		watchedConvs:      make(map[string]int),
		lastReportedSteps: make(map[string]int),
	}
	now := time.Now().UTC()

	// 1. Populate seenEnvelopes + seenEnvelopeOrder past maxSeenEnvelopes and verify FIFO eviction.
	for i := 0; i < maxSeenEnvelopes+100; i++ {
		id := fmt.Sprintf("env-%d", i)
		s.seenEnvelopes[id] = true
		s.seenEnvelopeOrder = append(s.seenEnvelopeOrder, id)
	}
	// Populate tasks past maxRetainedTasks: 1 active task, 1 watched terminal task, and maxRetainedTasks+50 terminal tasks.
	s.tasks["task-active"] = &TaskRecord{TaskID: "task-active", Terminal: false, UpdatedAt: now.Add(-2 * time.Hour)}
	s.tasks["task-watched"] = &TaskRecord{TaskID: "task-watched", Terminal: true, UpdatedAt: now.Add(-2 * time.Hour)}
	s.watchedTasks["task-watched"] = 1
	for i := 0; i < maxRetainedTasks+50; i++ {
		id := fmt.Sprintf("task-term-%04d", i)
		s.tasks[id] = &TaskRecord{TaskID: id, Terminal: true, UpdatedAt: now.Add(time.Duration(i) * time.Second)}
	}
	s.pruneSessionMapsLocked()

	if len(s.seenEnvelopes) > maxSeenEnvelopes*3/4 {
		t.Fatalf("seenEnvelopes not pruned to 75%% watermark: got %d > %d", len(s.seenEnvelopes), maxSeenEnvelopes*3/4)
	}
	if s.seenEnvelopes["env-0"] {
		t.Fatal("oldest envelope env-0 should have been evicted from seenEnvelopes")
	}
	if !s.seenEnvelopes[fmt.Sprintf("env-%d", maxSeenEnvelopes+99)] {
		t.Fatal("newest envelope should be retained in seenEnvelopes after FIFO pruning")
	}
	if len(s.tasks) > maxRetainedTasks {
		t.Fatalf("tasks not pruned: got %d > %d", len(s.tasks), maxRetainedTasks)
	}
	if s.tasks["task-active"] == nil {
		t.Fatal("non-terminal task-active must not be evicted")
	}
	if s.tasks["task-watched"] == nil {
		t.Fatal("watched task-watched must not be evicted")
	}
	if s.tasks["task-term-0000"] != nil {
		t.Fatal("oldest terminal task-term-0000 should have been evicted")
	}

	// 2. A task without step records (a Claude worker, an older executor): the reasoning text it
	// published makes the rows, in the model's own words, and each tool call files under the row
	// current when it ran.
	fallback := &TaskRecord{
		Activities: []ActivityItem{
			{Tool: "terminal", Input: `{"command":"KUBECONFIG=/tmp/k kubectl get nodes -o wide"}`, At: "2026-10-17T12:00:01Z"},
			{Tool: "terminal", Input: `{"command":"kubectl get pods -A"}`, At: "2026-10-17T12:00:02Z"},
			{Tool: "mcp__gke__list_clusters", Input: `{"project":"my-proj"}`, Title: "Listing GKE clusters in the fleet.", At: "2026-10-17T12:00:05Z"},
		},
		thinkingChunks: []timestampedChunk{
			{ts: time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC), text: "Start with the nodes. Two kubectl calls should do it."},
			{ts: time.Date(2026, 10, 17, 12, 0, 4, 0, time.UTC), text: "Now the fleet inventory."},
		},
	}
	synth := collectRecStepDetails(fallback)
	if len(synth) != 2 || synth[0].Title != "Start with the nodes." || synth[1].Title != "Now the fleet inventory." {
		t.Fatalf("unexpected fallback steps: %+v", synth)
	}
	if synth[0].Thought != "Start with the nodes. Two kubectl calls should do it." || synth[1].Thought != "" {
		t.Fatalf("expected the whole reasoning kept as the thought of a multi-sentence row only, got %+v", synth)
	}
	if len(synth[0].Activities) != 2 || len(synth[1].Activities) != 1 || synth[1].Activities[0].Tool != "mcp__gke__list_clusters" {
		t.Fatalf("expected calls filed under the row current at their time, got %+v", synth)
	}
	// Without reasoning either, consecutive same-titled calls share a row, titled by the
	// executor's own title when it sent one.
	fallback.thinkingChunks = nil
	synth = collectRecStepDetails(fallback)
	if len(synth) != 2 || synth[0].Title != "Running cluster and workspace diagnostics." || synth[1].Title != "Listing GKE clusters in the fleet." || len(synth[0].Activities) != 2 {
		t.Fatalf("unexpected activity-derived fallback steps: %+v", synth)
	}
}

func TestDirectoryOperatorSubjectsTombstonesAndProfileSync(t *testing.T) {
	s := &BusSession{
		agentCards:    make(map[string]map[string]any),
		seenEnvelopes: make(map[string]bool),
	}
	now := time.Now().UTC()

	// 1. Operator publishes AgentProfile card to a2a.agents.cluster-upgrade (without .card suffix).
	s.ingestA2AMessage("a2a.agents.cluster-upgrade", mustJSON(lib.Envelope{
		Protocol:      lib.Protocol,
		EnvelopeID:    "dir-1",
		TS:            now,
		From:          lib.Party{Session: "operator", Profile: "cluster-upgrade"},
		CorrelationID: "agentprofile-cluster-upgrade",
		Kind:          lib.KindAgentCard,
		Payload:       mustJSON(map[string]any{"name": "cluster-upgrade", "description": "GKE cluster upgrade specialist"}),
	}), false)

	cards := s.AgentCards()
	if cards["cluster-upgrade"] == nil {
		t.Fatalf("expected cluster-upgrade in AgentCards, got %+v", cards)
	}
	if len(s.Profile().AgentProfiles) != 0 {
		t.Fatalf("DIRECTORY specialist cards must not mutate Profile.AgentProfiles, got %+v", s.Profile().AgentProfiles)
	}

	// 2. Operator publishes agent-closed tombstone when AgentProfile is deleted.
	s.ingestA2AMessage("a2a.agents.cluster-upgrade", mustJSON(lib.Envelope{
		Protocol:      lib.Protocol,
		EnvelopeID:    "dir-2",
		TS:            now.Add(time.Second),
		From:          lib.Party{Session: "operator", Profile: "cluster-upgrade"},
		CorrelationID: "agentprofile-cluster-upgrade",
		Kind:          lib.KindAgentClosed,
		Payload:       []byte("{}"),
	}), false)

	if s.AgentCards()["cluster-upgrade"] != nil {
		t.Fatalf("expected tombstone to remove cluster-upgrade from AgentCards, got %+v", s.AgentCards())
	}
}

func TestDeriveActiveSessionPodsFromBusState(t *testing.T) {
	srv, nc, js := startEmbeddedJetStream(t)
	ctx := context.Background()
	now := time.Now().UTC()

	// Seed a completed session turn for s-09bb21da on TASKS.
	inSubj := "a2a.tasks.s-09bb21da.task-sess-1.in"
	outSubj := "a2a.tasks.s-09bb21da.task-sess-1.out"
	publishTaskEnv(t, nc, inSubj, lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "sess-in-1",
		TS:         now,
		TaskID:     "task-sess-1",
		ContextID:  "ctx-sess-1",
		To:         &lib.Party{Session: "s-09bb21da"},
		Kind:       lib.KindMessage,
		Authority:  mustJSON(map[string]any{"audience": map[string]any{"conversation": "console:session-poet"}}),
		Payload:    mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "hello poet"}}}),
	})
	publishTaskEnv(t, nc, outSubj, lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "sess-out-1",
		TS:         now.Add(time.Second),
		TaskID:     "task-sess-1",
		ContextID:  "ctx-sess-1",
		From:       lib.Party{Session: "s-09bb21da", Profile: "default"},
		Kind:       lib.KindStatusUpdate,
		Payload:    mustJSON(lib.StatusUpdate{TaskID: "task-sess-1", Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
	})

	// Seed a stale, orphaned non-terminal session task (>2m old) whose pod exited without
	// publishing a terminal envelope and whose consumer no longer exists on TASKS.
	publishTaskEnv(t, nc, "a2a.tasks.s-zombie00.task-zombie-1.in", lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "zombie-in-1",
		TS:         now.Add(-10 * time.Minute),
		TaskID:     "task-zombie-1",
		ContextID:  "ctx-zombie-1",
		To:         &lib.Party{Session: "s-zombie00"},
		Kind:       lib.KindMessage,
		Authority:  mustJSON(map[string]any{"audience": map[string]any{"conversation": "console:session-zombie"}}),
		Payload:    mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "zombie"}}}),
	})

	// Create the live worker-adapter origin consumer on TASKS.
	st, err := js.Stream(ctx, streamTasks)
	if err != nil {
		t.Fatalf("Stream(TASKS): %v", err)
	}
	originName := lib.SessionConsumerName("s-09bb21da", lib.SessionConsumerOrigin)
	if _, err := st.CreateOrUpdateConsumer(ctx, jetstream.ConsumerConfig{
		Name:          originName,
		Durable:       originName,
		FilterSubject: inSubj,
		AckPolicy:     jetstream.AckExplicitPolicy,
	}); err != nil {
		t.Fatalf("CreateOrUpdateConsumer(%s): %v", originName, err)
	}

	sess, err := ConnectBusSession(ctx, DeploymentProfile{
		Name:          "test-cluster",
		Namespace:     testNamespace,
		DirectNATSURL: srv.ClientURL(),
	}, nil)
	if err != nil {
		t.Fatalf("ConnectBusSession: %v", err)
	}
	defer sess.Close()
	sess.WaitForDeepBootstrap(5 * time.Second)

	// Guard against regressions: console user in rendered-nats.conf is denied
	// $JS.API.CONSUMER.NAMES.> and $JS.API.CONSUMER.LIST.>, so DeriveActiveSessionPods
	// must only probe $JS.API.CONSUMER.INFO.TASKS.<name>.
	var forbiddenSubjectMu sync.Mutex
	var forbiddenSubjects []string
	for _, pat := range []string{"$JS.API.CONSUMER.NAMES.>", "$JS.API.CONSUMER.LIST.>"} {
		sub, subErr := nc.Subscribe(pat, func(m *nats.Msg) {
			forbiddenSubjectMu.Lock()
			forbiddenSubjects = append(forbiddenSubjects, m.Subject)
			forbiddenSubjectMu.Unlock()
		})
		if subErr != nil {
			t.Fatalf("Subscribe(%s): %v", pat, subErr)
		}
		defer func() { _ = sub.Unsubscribe() }()
	}
	_ = nc.Flush()

	pods := sess.DeriveActiveSessionPods(ctx)
	if len(pods) != 1 || pods[0].Addressee != "s-09bb21da" || pods[0].SessionKey != "console:session-poet" || pods[0].TaskID != "task-sess-1" {
		t.Fatalf("expected DeriveActiveSessionPods to discover s-09bb21da from TASKS + consumer, got %+v", pods)
	}
	_ = nc.Flush()
	forbiddenSubjectMu.Lock()
	if len(forbiddenSubjects) > 0 {
		t.Fatalf("DeriveActiveSessionPods published to ACL-denied JetStream subjects: %v", forbiddenSubjects)
	}
	forbiddenSubjectMu.Unlock()

	// When the worker pod exits and its consumer is reaped (ErrConsumerNotFound),
	// DeriveActiveSessionPods must drop it immediately.
	if err := st.DeleteConsumer(ctx, originName); err != nil {
		t.Fatalf("DeleteConsumer(%s): %v", originName, err)
	}
	if podsAfterReap := sess.DeriveActiveSessionPods(ctx); len(podsAfterReap) != 0 {
		t.Fatalf("expected reaped consumer to mark s-09bb21da inactive, got %+v", podsAfterReap)
	}

	// Re-create consumer, verify discovery, then simulate /session off notice arriving after task completion.
	if _, err := st.CreateOrUpdateConsumer(ctx, jetstream.ConsumerConfig{
		Name:          originName,
		Durable:       originName,
		FilterSubject: inSubj,
		AckPolicy:     jetstream.AckExplicitPolicy,
	}); err != nil {
		t.Fatalf("CreateOrUpdateConsumer(%s): %v", originName, err)
	}
	if podsRestored := sess.DeriveActiveSessionPods(ctx); len(podsRestored) != 1 {
		t.Fatalf("expected consumer recreation to restore s-09bb21da, got %+v", podsRestored)
	}

	sess.mu.Lock()
	sess.consoleNotices = append(sess.consoleNotices, ConsoleOutFrame{
		MessageID: "off-1",
		Text:      "session route off — returning to platform",
		Token:     "session-poet",
		Timestamp: now.Add(5 * time.Second),
	})
	sess.mu.Unlock()

	if podsAfterOff := sess.DeriveActiveSessionPods(ctx); len(podsAfterOff) != 0 {
		t.Fatalf("expected session route off notice to mark s-09bb21da inactive, got %+v", podsAfterOff)
	}
}

func TestTopicEnvelopeRawJSONAndProfileEnrichment(t *testing.T) {
	s := &BusSession{
		topics:            make(map[string]TopicSnapshot),
		provisionedTopics: make(map[string]TopicSnapshot),
		seenEnvelopes:     make(map[string]bool),
	}
	now := time.Now().UTC()

	// Seed an unwritten provisioned topic subject; it must appear in ListTopicsOrProvisioned()
	// (with "—" for UpdatedAt) without polluting ListTopics() or Swarm board/milestones.
	s.provisionedTopics["a2a.topics.cluster.upgrade-readiness"] = TopicSnapshot{
		Stream:        streamTopicsState,
		Subject:       "a2a.topics.cluster.upgrade-readiness",
		Topic:         "upgrade-readiness",
		Class:         topicClassState,
		WriterSession: "cluster",
		Summary:       "(provisioned — no entries published yet)",
	}
	if len(s.ListTopics()) != 0 {
		t.Fatalf("unwritten provisioned topics must not appear in ListTopics(), got %+v", s.ListTopics())
	}
	if len(s.ListTopicsOrProvisioned()) != 1 {
		t.Fatalf("expected 1 topic in ListTopicsOrProvisioned(), got %+v", s.ListTopicsOrProvisioned())
	}
	if md := FormatTopicsMarkdown(s.ListTopicsOrProvisioned()); strings.Contains(md, "0001-01-01") {
		t.Fatalf("FormatTopicsMarkdown must render '—' instead of zero timestamp: %s", md)
	}

	s.ingestTopicEnvelope("a2a.topics.state.blueprint", mustJSON(lib.Envelope{
		Protocol:      lib.Protocol,
		EnvelopeID:    "top-raw-1",
		TS:            now,
		From:          lib.Party{Session: "platform", Profile: "platform"},
		TaskID:        "task-bp-1",
		CorrelationID: "corr-bp-1",
		Kind:          lib.KindArtifactUpdate,
		Payload:       []byte(`{"clusterName":"kube-agents-host","projectId":"example-project","location":"us-east4","gitRepo":"https://github.com/example-org/kube-agents","version":"480636be","summary":"Host blueprint ready"}`),
	}))

	topics := s.ListTopics()
	if len(topics) != 1 || topics[0].Topic != "blueprint" || topics[0].Class != topicClassState || topics[0].Summary != "Host blueprint ready" {
		t.Fatalf("expected raw JSON TOPICS-STATE envelope to be ingested, got %+v", topics)
	}
	prof := s.Profile()
	if prof.ClusterName != "kube-agents-host" || prof.ProjectID != "example-project" || prof.Location != "us-east4" || prof.GitHubRepo != "https://github.com/example-org/kube-agents" || prof.Version != "480636be" {
		t.Fatalf("expected Profile enriched from TOPICS-STATE, got %+v", prof)
	}
}

func TestResolveDirectNATSURLAndBearerTokenDetection(t *testing.T) {
	var currentPass = "ws-auto-pass"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/config.json" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(w, `{"user":"console","pass":%q}`, currentPass)
	}))
	defer srv.Close()

	prof := DeploymentProfile{DirectNATSURL: srv.URL}
	resolved := resolveDirectNATSURL(context.Background(), &prof)
	wantWS := strings.Replace(srv.URL, "http://", "ws://", 1) + "/bus"
	if resolved != wantWS {
		t.Fatalf("resolveDirectNATSURL(%q) = %q, want %q", srv.URL, resolved, wantWS)
	}
	if prof.BusUser != "console" || prof.BusAuthValue != "ws-auto-pass" {
		t.Fatalf("expected /config.json credentials populated, got user=%q pass=%q", prof.BusUser, prof.BusAuthValue)
	}

	// Simulate credential rotation on /config.json and verify refreshDeploymentCredentials updates BusAuthValue.
	currentPass = "ws-rotated-pass"
	if !refreshDeploymentCredentials(context.Background(), &prof) {
		t.Fatal("expected refreshDeploymentCredentials to return true when /config.json password rotates")
	}
	if prof.BusAuthValue != "ws-rotated-pass" {
		t.Fatalf("expected rotated password 'ws-rotated-pass', got %q", prof.BusAuthValue)
	}

	if !isBearerTokenValue(googleOAuthTokenPrefix + "test-oauth-token") {
		t.Fatal("expected ya29.* Google OAuth access token to be detected as bearer token")
	}
	if !isBearerTokenValue("eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl") {
		t.Fatal("expected JWT token to be detected as bearer token")
	}
	if isBearerTokenValue("plain-password-value") {
		t.Fatal("plain password must not be detected as bearer token")
	}
}
