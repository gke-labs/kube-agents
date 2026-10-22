package mcp

import (
	"encoding/json"
	"path/filepath"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func TestIsSessionSubagentEntryIgnoresAliasNaming(t *testing.T) {
	// A coordinator's own /session conversation keeps the default "session-<unix>" alias that
	// manage_session mints; naming alone must never demote it to a subagent entry.
	own := ConversationEntry{Alias: "session-1760000000", Token: "agy-aaaa", SessionMode: true, AGYConversationID: "11111111-1111-4111-8111-111111111111"}
	if isSessionSubagentEntry(own) {
		t.Fatalf("alias naming must not classify %+v as a subagent entry", own)
	}
	byRole := ConversationEntry{Alias: "anything", SubagentRole: "Session Poet"}
	if !isSessionSubagentEntry(byRole) {
		t.Fatalf("a recorded SubagentRole must classify %+v as a subagent entry", byRole)
	}
	byParent := ConversationEntry{Alias: "x", AGYConversationID: "22222222-2222-4222-8222-222222222222", ParentAGYConversationID: "33333333-3333-4333-8333-333333333333"}
	if !isSessionSubagentEntry(byParent) {
		t.Fatalf("a distinct parent conversation must classify %+v as a subagent entry", byParent)
	}
	selfParent := ConversationEntry{Alias: "x", AGYConversationID: "22222222-2222-4222-8222-222222222222", ParentAGYConversationID: "22222222-2222-4222-8222-222222222222"}
	if isSessionSubagentEntry(selfParent) {
		t.Fatalf("a self-referential parent must not classify %+v as a subagent entry", selfParent)
	}
}

func TestLineageIsSessionSubagentIgnoresRoleWording(t *testing.T) {
	typed := ConversationLineage{IsSubagent: true, SubagentType: subagentTypeKubeSession, SubagentRole: "Upgrade Triage"}
	if !typed.IsSessionSubagent() {
		t.Fatal("kube-session typed subagent must be a session subagent regardless of role wording")
	}
	wordedOnly := ConversationLineage{IsSubagent: true, SubagentType: "kube-agents", SubagentRole: "Session Upgrade Triage"}
	if wordedOnly.IsSessionSubagent() {
		t.Fatal("role wording alone must not make a generic subagent a session subagent")
	}
	notSub := ConversationLineage{IsSubagent: false, SubagentType: subagentTypeKubeSession}
	if notSub.IsSessionSubagent() {
		t.Fatal("a non-subagent lineage is never a session subagent")
	}
}

func TestIngestLinksWakeAndChildFromAuthorityVia(t *testing.T) {
	s := &BusSession{
		tasks:         make(map[string]*TaskRecord),
		seenEnvelopes: make(map[string]bool),
	}
	now := time.Now().UTC()
	conv := "console:agy-via"
	ingest := func(subject, envID, taskID string, ts time.Time, auth map[string]any, text string) {
		t.Helper()
		var authRaw json.RawMessage
		if auth != nil {
			authRaw = mustJSON(auth)
		}
		s.ingestTaskEnvelope(subject, mustJSON(lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: envID,
			TS:         ts,
			TaskID:     taskID,
			ContextID:  "ctx-via-link",
			Kind:       lib.KindMessage,
			Authority:  authRaw,
			Payload:    mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: text}}}),
		}), true)
	}

	// The session's own turn, then a delegated child (via -> the turn, addressed to platform),
	// then the wake (via -> the child, addressed to the session worker). The wake's prompt is
	// deliberately free text so the link can only come from the authority block.
	ingest("a2a.tasks.otter-heron-1a2b.task-turn.in", "l-1", "task-turn", now,
		map[string]any{"audience": map[string]any{"conversation": conv}}, "upgrade all clusters")
	ingest("a2a.tasks.platform.task-child.in", "l-2", "task-child", now.Add(time.Second),
		map[string]any{"audience": map[string]any{"conversation": conv}, "via": map[string]any{"taskId": "task-turn", "session": "otter-heron-1a2b"}},
		"upgrade all clusters")
	ingest("a2a.tasks.otter-heron-1a2b.task-wake.in", "l-3", "task-wake", now.Add(2*time.Second),
		map[string]any{"audience": map[string]any{"conversation": conv}, "via": map[string]any{"taskId": "task-child", "session": "otter-heron-1a2b"}},
		"whatever wording the gateway chooses for its wake header")

	child := s.GetTask("task-child")
	if child == nil || child.DelegatedFromTaskID != "task-turn" || child.WakeFromChildTaskID != "" {
		t.Fatalf("delegated child must link to the delegating turn only, got %+v", child)
	}
	wake := s.GetTask("task-wake")
	if wake == nil || wake.WakeFromChildTaskID != "task-child" || wake.DelegatedFromTaskID != "" {
		t.Fatalf("wake must link to the finished child via WakeFromChildTaskID only, got %+v", wake)
	}
	if w := s.findWakeTask("task-child"); w == nil || w.TaskID != "task-wake" {
		t.Fatalf("findWakeTask must resolve the wake structurally, got %+v", w)
	}
	if !isSessionWorkerTask(wake) || isSessionWorkerTask(child) {
		t.Fatal("the wake is a session-worker turn and the child is not")
	}

	// A Kanban card task addressed to a specialist with a via block is a delegation, never a wake.
	ingest("a2a.tasks.cluster-gke-1.task-card-77.in", "l-4", "task-card-77", now.Add(3*time.Second),
		map[string]any{"audience": map[string]any{"conversation": conv}, "via": map[string]any{"taskId": "task-child", "session": "otter-heron-1a2b"}},
		"audit node pools")
	card := s.GetTask("task-card-77")
	if card == nil || card.WakeFromChildTaskID != "" || card.DelegatedFromTaskID != "task-child" {
		t.Fatalf("card tasks must never be classified as wakes, got %+v", card)
	}
}

func TestDrainPendingEventsByID(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	_ = store.AppendPendingEvents(
		PushEvent{
			ID:        "ev-first",
			Kind:      eventKindTaskCompleted,
			Title:     "First",
			Summary:   "FIRST_EVENT",
			Timestamp: time.Now().UTC(),
		},
		PushEvent{
			ID:        "ev-second",
			Kind:      eventKindTaskCompleted,
			Title:     "Second",
			Summary:   "SECOND_EVENT",
			Timestamp: time.Now().UTC(),
		},
	)

	if err := store.DrainPendingEventsByID([]string{"ev-first"}); err != nil {
		t.Fatal(err)
	}
	pending := store.PeekPendingEvents()
	if len(pending) != 1 || pending[0].ID != "ev-second" {
		t.Fatalf("expected only ev-second to remain, got %+v", pending)
	}
	if err := store.DrainPendingEventsByID([]string{"never-queued"}); err != nil {
		t.Fatalf("draining unknown IDs must be a no-op, got %v", err)
	}
}
