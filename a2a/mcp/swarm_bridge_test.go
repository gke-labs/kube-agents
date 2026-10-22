package mcp

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

func TestConfigStoreLineageIsolation(t *testing.T) {
	cfgPath := filepath.Join(t.TempDir(), "mcp-config.json")
	store, err := NewConfigStore(cfgPath)
	if err != nil {
		t.Fatalf("NewConfigStore: %v", err)
	}

	rootID := "9dd87ed9-09b7-48b9-a2ab-f5aa78525166"
	subID := "c02e5921-7d9a-4411-9988-112233445566"

	rootLin := ConversationLineage{
		ConversationID:     rootID,
		RootConversationID: rootID,
		IsSubagent:         false,
	}
	subLin := ConversationLineage{
		ConversationID:       subID,
		ParentConversationID: rootID,
		RootConversationID:   rootID,
		SubagentType:         "kube-session",
		SubagentRole:         "Upgrade Triage",
		IsSubagent:           true,
	}

	// 1. Root coordinator claims "default".
	rootConv := store.ConversationForAGYWithLineage(rootLin)
	if rootConv.Alias != defaultConvAlias || rootConv.AGYConversationID != rootID {
		t.Fatalf("expected root coordinator to bind default conversation, got %+v", rootConv)
	}

	// 2. Subagent mints its own isolated session-<slug> conversation without stealing "default" or Push.AGYConversationID.
	subConv := store.ConversationForAGYWithLineage(subLin)
	if subConv.Alias != "session-upgrade-triage" {
		t.Fatalf("expected subagent alias 'session-upgrade-triage', got %q", subConv.Alias)
	}
	if subConv.AGYConversationID != subID || subConv.ParentAGYConversationID != rootID {
		t.Fatalf("unexpected subagent conversation binding: %+v", subConv)
	}
	if subConv.Token == rootConv.Token {
		t.Fatalf("subagent must not share console token with root coordinator")
	}

	snap := store.Snapshot()
	if snap.ActiveConversation != defaultConvAlias {
		t.Fatalf("subagent call must not clobber ActiveConversation: got %q, want %q", snap.ActiveConversation, defaultConvAlias)
	}
	if snap.Push.AGYConversationID != rootID {
		t.Fatalf("subagent call must not overwrite Push.AGYConversationID: got %q, want %q", snap.Push.AGYConversationID, rootID)
	}

	// 3. Event routing resolves subagent token -> subID and root token -> rootID.
	if got := store.ResolveAGYConversationIDForEvent("console:" + subConv.Token); got != subID {
		t.Fatalf("expected subagent event route %q, got %q", subID, got)
	}
	if got := store.ResolveAGYConversationIDForEvent("console:" + rootConv.Token); got != rootID {
		t.Fatalf("expected root event route %q, got %q", rootID, got)
	}

	// 4. Session lifecycle state transitions: session_on -> SessionWorkerID -> session_off.
	if _, err := store.SetConversationSessionMode(subConv.Alias, true); err != nil {
		t.Fatalf("SetConversationSessionMode(true): %v", err)
	}
	store.SetConversationSessionWorkerID(subConv.Alias, "s-c02e59217d9a")
	if _, err := store.SetConversationSessionMode(subConv.Alias, false); err != nil {
		t.Fatalf("SetConversationSessionMode(false): %v", err)
	}
	afterOff := store.Snapshot().Conversations[subConv.Alias]
	if afterOff.SessionMode || !afterOff.SessionTerminated || afterOff.SessionWorkerID != "s-c02e59217d9a" {
		t.Fatalf("expected SessionMode=false, SessionTerminated=true, SessionWorkerID preserved; got %+v", afterOff)
	}
}

func TestBuildSwarmStateProjectionAndIdempotency(t *testing.T) {
	now := time.Date(2026, 10, 18, 14, 30, 0, 0, time.UTC)
	rootID := "root-coord-uuid"
	subID := "sub-session-uuid"
	otherRootID := "other-root-uuid"

	input := SwarmProjectionInput{
		RootConversationID: rootID,
		BrainDir:           t.TempDir(),
		Deployment: DeploymentProfile{
			Name:              "kube-agents-host",
			Namespace:         "kubeagents-system",
			PlatformAgentName: "platform-agent",
			ClusterName:       "kube-agents-host",
			AgentProfiles:     []string{"cluster-upgrade", "sre"},
			LastConnectedAt:   now.Add(-10 * time.Minute),
			// A live session pod serving another front end (a Slack thread): visible in the
			// namespace inventory but not this coordinator's to project.
			ActiveSessionPods: []SessionPodInfo{
				{Name: "s-998877665544", Addressee: "s-998877665544", SessionKey: "slack:C0123/1712345.6789", Phase: "Running", CreatedAt: now.Add(-5 * time.Minute)},
			},
			SessionPodsProbedAt: now,
		},
		Connected: true,
		Conversations: map[string]ConversationEntry{
			"default": {
				Alias:             "default",
				Token:             "agy-root111",
				AGYConversationID: rootID,
				CreatedAt:         now.Add(-10 * time.Minute),
			},
			"session-upgrade-triage": {
				Alias:                   "session-upgrade-triage",
				Token:                   "agy-sub222",
				SessionMode:             true,
				AGYConversationID:       subID,
				ParentAGYConversationID: rootID,
				SubagentRole:            "Upgrade Triage",
				CreatedAt:               now.Add(-8 * time.Minute),
			},
			"session-other-tab": {
				Alias:                   "session-other-tab",
				Token:                   "agy-other333",
				SessionMode:             true,
				AGYConversationID:       "other-sub-uuid",
				ParentAGYConversationID: otherRootID,
				CreatedAt:               now.Add(-9 * time.Minute),
			},
		},
		Subagents: map[string]ConversationLineage{
			subID: {
				ConversationID:       subID,
				ParentConversationID: rootID,
				RootConversationID:   rootID,
				SubagentType:         "kube-session",
				SubagentRole:         "Upgrade Triage",
				IsSubagent:           true,
			},
		},
		// DIRECTORY cards come from standing agents only; session pods never publish one.
		AgentCards: map[string]map[string]any{
			"platform": {"version": "0.4.0", "description": "Hermes Platform Agent"},
		},
		Tasks: []*TaskRecord{
			{
				TaskID:       "task-sess-1",
				Addressee:    "s-c02e59217d9a",
				Conversation: "console:agy-sub222",
				State:        lib.StateCompleted,
				Terminal:     true,
				PromptText:   "Inspect cluster upgrade readiness and delegate deep check to platform",
				ResultText:   "Delegated deep check to platform.",
				CreatedAt:    now.Add(-7 * time.Minute),
				UpdatedAt:    now.Add(-6 * time.Minute),
			},
			{
				TaskID:              "task-del-2",
				Addressee:           "platform",
				Conversation:        "console:agy-sub222",
				DelegatedFromTaskID: "task-sess-1",
				State:               lib.StateCompleted,
				Terminal:            true,
				PromptText:          "Run cluster-upgrade specialist preflight",
				ResultText:          "Preflight completed with 1 pdb blocker.",
				CreatedAt:           now.Add(-6 * time.Minute),
				UpdatedAt:           now.Add(-4 * time.Minute),
			},
			{
				TaskID:           "task-card-upgrade-1",
				Addressee:        "platform",
				Conversation:     "console:agy-sub222",
				CardParentTaskID: "task-del-2",
				State:            lib.StateCompleted,
				Terminal:         true,
				PromptText:       "Audit PDBs and deprecated APIs",
				ResultText:       "Found legacy-pdb blocking drain.",
				Steps: []StepRecord{
					{
						Step: stepmsg.Step{
							Kind:  stepmsg.KindCard,
							Title: "Audit PDBs and deprecated APIs",
							Card: &stepmsg.Card{
								ID:           "card-upgrade-1",
								Title:        "Audit PDBs and deprecated APIs",
								Assignee:     "cluster-upgrade",
								Status:       "done",
								ParentTaskID: "task-del-2",
							},
						},
						At: now.Add(-5 * time.Minute),
					},
				},
				CreatedAt: now.Add(-5 * time.Minute),
				UpdatedAt: now.Add(-4*time.Minute - 30*time.Second),
			},
			{
				TaskID:              "task-wake-3",
				Addressee:           "s-c02e59217d9a",
				Conversation:        "console:agy-sub222",
				WakeFromChildTaskID: "task-del-2",
				State:               lib.StateWorking,
				Terminal:            false,
				PromptText:          "[delegated task task-del-2] Synthesize upgrade blockers",
				Steps: []StepRecord{
					{
						Step: stepmsg.Step{
							Kind:  stepmsg.KindPlan,
							Title: "Drafting PDB remediation plan",
						},
						At: now.Add(-1 * time.Minute),
					},
				},
				CreatedAt: now.Add(-3 * time.Minute),
				UpdatedAt: now.Add(-1 * time.Minute),
			},
		},
		Topics: []TopicSnapshot{
			{
				Stream:        streamTopicsState,
				Subject:       "a2a.topics.cluster.upgrade-readiness",
				Topic:         "upgrade-readiness",
				Class:         topicClassState,
				WriterProfile: "cluster-upgrade",
				Summary:       "Cluster 1.31 -> 1.32 readiness: 1 blocker found",
				Data:          json.RawMessage(`{"blockers":[{"title":"PDB default/zk-budget blocks node drain","severity":"high"}]}`),
				UpdatedAt:     now.Add(-2 * time.Minute),
			},
		},
		Now: now,
	}

	snap1, ledger1 := BuildSwarmState(input, BoardLedger{})

	// 1. Verify roster members and roles (specialists/cluster agents must NOT be added, and
	// neither may a session pod serving another front end's conversation).
	wantRoles := map[string]string{
		"coordinator":            "coordinator",
		"narrator":               "narrator",
		"platform":               "worker",
		"session-upgrade-triage": "worker",
	}
	for name, wantRole := range wantRoles {
		m, ok := snap1.Members[name]
		if !ok {
			t.Fatalf("missing expected roster member %q; got %+v", name, snap1.Members)
		}
		if m.Role != wantRole {
			t.Fatalf("member %q role = %q, want %q", name, m.Role, wantRole)
		}
	}
	for _, unexpected := range []string{"cluster-upgrade", "sre", "session-other-tab", "s-998877665544"} {
		if _, leaked := snap1.Members[unexpected]; leaked {
			t.Fatalf("member %q must not appear on Swarm roster: %+v", unexpected, snap1.Members)
		}
	}

	// 2. Verify structural join between session-upgrade-triage, subagent UUID, and remote s-c02e59217d9a.
	sessMember := snap1.Members["session-upgrade-triage"]
	if sessMember.ConversationID != subID {
		t.Fatalf("session-upgrade-triage ConversationID = %q, want %q", sessMember.ConversationID, subID)
	}
	if !strings.Contains(sessMember.Model, "s-c02e59217d9a") || !strings.Contains(sessMember.Model, "Drafting PDB remediation plan") {
		t.Fatalf("expected session-upgrade-triage model to include remote pod ID and active step, got %q", sessMember.Model)
	}
	if sessMember.WakeMode != swarmWakeWorking {
		t.Fatalf("expected session-upgrade-triage WakeMode=%q while task-wake-3 is active, got %q", swarmWakeWorking, sessMember.WakeMode)
	}

	// 3. Verify board messages contain @mentions and #seq citations and use Agent/Time fields,
	// and do NOT contain specialist/cluster agent messages.
	if snap1.Title != "kube-agents swarm" {
		t.Fatalf("expected Title=%q, got %q", "kube-agents swarm", snap1.Title)
	}
	if len(snap1.NewMessages) == 0 {
		t.Fatalf("expected board messages to be emitted")
	}
	var allText strings.Builder
	for _, msg := range snap1.NewMessages {
		if msg.Agent == "" || msg.Time == "" {
			t.Fatalf("board message must populate Agent and Time for Swarm UI compatibility: %+v", msg)
		}
		allText.WriteString(msg.Agent + ": " + msg.Text + "\n")
	}
	boardDump := allText.String()
	for _, wantSubstr := range []string{
		"Connected @platform to kube-agents deployment",
		"@session-upgrade-triage",
		"@platform delegated task `task-del-2`",
		"@session-upgrade-triage wake synthesis for delegated task `task-del-2`",
		"published topic `upgrade-readiness`",
	} {
		if !strings.Contains(boardDump, wantSubstr) {
			t.Fatalf("expected board dump to contain %q, got:\n%s", wantSubstr, boardDump)
		}
	}
	for _, unwantedSubstr := range []string{
		"Registered @coordinator fleet topology",
		"Kanban specialist card",
		"@cluster-upgrade",
	} {
		if strings.Contains(boardDump, unwantedSubstr) {
			t.Fatalf("expected board dump NOT to contain specialist/cluster post %q, got:\n%s", unwantedSubstr, boardDump)
		}
	}

	// 4. Verify view.json schema rules enforced by swarm_board/app.js sanitizeView.
	head := ledger1.NextSeq - 1
	if snap1.View.AsOfSeq != head {
		t.Fatalf("View.AsOfSeq = %d, want head = %d", snap1.View.AsOfSeq, head)
	}
	if snap1.Members["narrator"].LastReadSeq != head {
		t.Fatalf("narrator LastReadSeq = %d, want head = %d", snap1.Members["narrator"].LastReadSeq, head)
	}
	idRe := regexp.MustCompile(`^[a-z0-9-]{1,40}$`)
	for _, att := range snap1.View.Attention {
		if !idRe.MatchString(att.ID) {
			t.Fatalf("invalid attention item ID %q", att.ID)
		}
		if len(att.Refs) == 0 {
			t.Fatalf("attention item %q has no refs", att.ID)
		}
	}
	foundOpenConv := false
	for _, ws := range snap1.View.Workstreams {
		if !idRe.MatchString(ws.ID) {
			t.Fatalf("invalid workstream ID %q", ws.ID)
		}
		for _, act := range ws.Actions {
			if act.Type == "open_conv" && act.Target == "session-upgrade-triage" {
				foundOpenConv = true
			}
		}
	}
	if !foundOpenConv {
		t.Fatalf("expected workstreams to include open_conv action for session-upgrade-triage")
	}
	if len(snap1.View.Risks) == 0 || !strings.Contains(snap1.View.Risks[0].Text, "PDB default/zk-budget") {
		t.Fatalf("expected structured risk from upgrade-readiness topic, got %+v", snap1.View.Risks)
	}

	// 5. Verify idempotency: re-running with ledger1 emits 0 new messages.
	snap2, ledger2 := BuildSwarmState(input, ledger1)
	if len(snap2.NewMessages) != 0 {
		t.Fatalf("expected 0 new messages on idempotent re-run, got %d: %+v", len(snap2.NewMessages), snap2.NewMessages)
	}
	if ledger2.NextSeq != ledger1.NextSeq {
		t.Fatalf("expected NextSeq unchanged (%d), got %d", ledger1.NextSeq, ledger2.NextSeq)
	}
}

func TestBuildSwarmStateFiltersNonSessionSubagentsAndCardWorkers(t *testing.T) {
	now := time.Date(2026, 10, 18, 14, 45, 0, 0, time.UTC)
	rootID := "root-coord-filter-test"

	input := SwarmProjectionInput{
		RootConversationID: rootID,
		Deployment: DeploymentProfile{
			Name:              "kube-agents-host",
			Namespace:         "kubeagents-system",
			PlatformAgentName: "platform-agent",
		},
		Connected: true,
		Now:       now,
		Conversations: map[string]ConversationEntry{
			"soak-r16": {
				Alias:                   "soak-r16",
				Token:                   "soak-r16",
				SessionMode:             false,
				AGYConversationID:       "sub-soak-non-session",
				ParentAGYConversationID: rootID,
				SubagentRole:            "Soak R16-B: session list & transcript compare",
				CreatedAt:               now,
			},
			"session-upgrade-triage": {
				Alias:                   "session-upgrade-triage",
				Token:                   "tok-upgrade",
				SessionMode:             false,
				AGYConversationID:       "sub-upgrade-session",
				ParentAGYConversationID: rootID,
				SubagentRole:            "Session Upgrade Triage",
				CreatedAt:               now,
			},
		},
		Subagents: map[string]ConversationLineage{
			"sub-soak-non-session": {
				ConversationID:       "sub-soak-non-session",
				ParentConversationID: rootID,
				RootConversationID:   rootID,
				SubagentType:         "kube-agents",
				SubagentRole:         "Soak R16-B: session list & transcript compare",
				SubagentState:        "SUBAGENT_STATE_ALIVE",
				IsSubagent:           true,
			},
			"sub-killed-session": {
				ConversationID:       "sub-killed-session",
				ParentConversationID: rootID,
				RootConversationID:   rootID,
				SubagentType:         "kube-session",
				SubagentRole:         "Session Old Killed",
				SubagentState:        "SUBAGENT_STATE_KILLED",
				IsSubagent:           true,
			},
			"sub-upgrade-session": {
				ConversationID:       "sub-upgrade-session",
				ParentConversationID: rootID,
				RootConversationID:   rootID,
				SubagentType:         "kube-agents",
				SubagentRole:         "Session Upgrade Triage",
				SubagentState:        "SUBAGENT_STATE_ALIVE",
				IsSubagent:           true,
			},
		},
		Tasks: []*TaskRecord{
			{
				TaskID:     "task-fleet-1",
				Addressee:  "platform",
				PromptText: "Check fleet clusters",
				State:      lib.StateCompleted,
				Terminal:   true,
				CreatedAt:  now,
				UpdatedAt:  now,
				Steps: []StepRecord{
					{
						Step: stepmsg.Step{
							Title: "drift-peer-ap-1",
							Card: &stepmsg.Card{
								ID:       "t_035c5dd0",
								Title:    "drift-peer-ap-1",
								Assignee: "cluster:example-project/drift-peer-ap-1/us-east4",
								Status:   "done",
							},
						},
						At: now,
					},
				},
				Activities: []ActivityItem{
					{
						Tool:   "kubectl_get",
						Worker: "card:t_035c5dd0:drift-peer-ap-1",
						Status: "ok",
					},
				},
			},
			{
				TaskID:       "task-sess-badger-1",
				Addressee:    "chat-badger-eff4bb07",
				Conversation: "console:tok-upgrade",
				PromptText:   "Check kubeagents-system pods",
				State:        lib.StateCompleted,
				Terminal:     true,
				CreatedAt:    now.Add(time.Second),
				UpdatedAt:    now.Add(2 * time.Second),
			},
		},
	}

	snap, _ := BuildSwarmState(input, BoardLedger{})

	// 1. Non-session soak subagent, killed unbound session subagent, and cluster specialist cards must NOT be in members.
	for name := range snap.Members {
		if strings.Contains(name, "soak") || strings.Contains(name, "killed") || strings.HasPrefix(name, "card-") || strings.HasPrefix(name, "cluster-") {
			t.Fatalf("unexpected member %q in Swarm roster: %+v", name, snap.Members[name])
		}
	}
	// 2. Dedicated Session Upgrade Triage subagent MUST be in members with conversation_id and chat-badger-eff4bb07 pod model.
	upMem, ok := snap.Members["session-upgrade-triage"]
	if !ok || upMem.ConversationID != "sub-upgrade-session" || !strings.Contains(upMem.Model, "chat-badger-eff4bb07") {
		t.Fatalf("expected session-upgrade-triage with conversation_id=sub-upgrade-session and model containing chat-badger-eff4bb07, got %+v", upMem)
	}
}

func TestBuildSwarmStateChronologicalAndCausalEventOrdering(t *testing.T) {
	t0 := time.Date(2026, 10, 18, 14, 0, 0, 0, time.UTC)

	// Task A starts at t0 and finishes at t0+10m.
	// Task B starts at t0+1m and finishes at t0+5m (overlapping inside Task A's window).
	// Task C (wake synthesis from B) starts at t0+5m (same timestamp as B's completion).
	input := SwarmProjectionInput{
		RootConversationID: "root-order-test",
		Deployment: DeploymentProfile{
			Name:              "kube-agents-host",
			Namespace:         "kubeagents-system",
			PlatformAgentName: "platform-agent",
			LastConnectedAt:   t0.Add(20 * time.Minute),
		},
		Connected: true,
		Now:       t0.Add(20 * time.Minute),
		Tasks: []*TaskRecord{
			{
				TaskID:     "task-A",
				Addressee:  "platform",
				PromptText: "Long running audit",
				State:      lib.StateCompleted,
				Terminal:   true,
				CreatedAt:  t0,
				UpdatedAt:  t0.Add(10 * time.Minute),
				ResultText: "Audit complete.",
			},
			{
				TaskID:     "task-B",
				Addressee:  "platform",
				PromptText: "Quick check",
				State:      lib.StateCompleted,
				Terminal:   true,
				CreatedAt:  t0.Add(1 * time.Minute),
				UpdatedAt:  t0.Add(5 * time.Minute),
				ResultText: "Quick check complete.",
			},
			{
				TaskID:              "task-C-wake",
				Addressee:           "chat-badger-111",
				Conversation:        "console:tok-111",
				WakeFromChildTaskID: "task-B",
				PromptText:          "Wake synthesis",
				State:               lib.StateCompleted,
				Terminal:            true,
				CreatedAt:           t0.Add(5 * time.Minute),
				UpdatedAt:           t0.Add(6 * time.Minute),
				ResultText:          "Synthesized.",
			},
		},
	}

	snap, _ := BuildSwarmState(input, BoardLedger{})
	if len(snap.NewMessages) != 9 {
		t.Fatalf("expected 9 board messages (deploy + session-start + 3*(in+done) + session-stop), got %d: %+v", len(snap.NewMessages), snap.NewMessages)
	}

	// Seq 1 must be deployment connection.
	if !strings.Contains(snap.NewMessages[0].Text, "Connected @platform") {
		t.Fatalf("expected seq 1 to be deployment connection, got %+v", snap.NewMessages[0])
	}
	// Verify task-B completion (t0+5m) comes BEFORE task-A completion (t0+10m),
	// and task-B completion comes BEFORE task-C-wake start (both at t0+5m).
	findSeq := func(substr string) int {
		for _, m := range snap.NewMessages {
			if strings.Contains(m.Text, substr) {
				return m.Seq
			}
		}
		return 0
	}
	seqAIn := findSeq("[task `task-A`]")
	seqBIn := findSeq("[task `task-B`]")
	seqBDone := findSeq("`completed` #" + fmt.Sprint(seqBIn) + " (`task-B`)")
	seqCWake := findSeq("wake synthesis for delegated task `task-B` (#" + fmt.Sprint(seqBDone) + ")")
	seqADone := findSeq("`completed` #" + fmt.Sprint(seqAIn) + " (`task-A`)")

	if !(seqAIn < seqBIn && seqBIn < seqBDone && seqBDone < seqCWake && seqCWake < seqADone) {
		t.Fatalf("expected strict chronological + causal order A_in(%d) < B_in(%d) < B_done(%d) < C_wake(%d) < A_done(%d), messages=%+v",
			seqAIn, seqBIn, seqBDone, seqCWake, seqADone, snap.NewMessages)
	}
}

func TestPopulateDeploymentInventoryAndInitialSwarmPreCreation(t *testing.T) {
	now := time.Date(2026, 10, 18, 16, 40, 0, 0, time.UTC)
	prof := DeploymentProfile{
		Name:              "kube-agents-host",
		KubeContext:       "gke_example-project_us-east4_kube-agents-host",
		Namespace:         "kubeagents-system",
		PlatformAgentName: "platform-agent",
		ProjectID:         "example-project",
		ClusterName:       "kube-agents-host",
		Location:          "us-east4",
		LastConnectedAt:   now,
	}

	sess := &BusSession{
		profile: prof,
		tasks: map[string]*TaskRecord{
			"task-console-111": {
				TaskID:       "task-console-111",
				Addressee:    "chat-badger-bfde9405",
				Conversation: "console:agy-835cc7f83f54",
				State:        lib.StateWorking,
				Terminal:     false,
				CreatedAt:    now.Add(-30 * time.Minute),
				UpdatedAt:    now.Add(-5 * time.Minute),
			},
			"task-console-222": {
				TaskID:       "task-console-222",
				Addressee:    "chat-marten-4d134d0d",
				Conversation: "console:agy-f71ceb116d6d",
				State:        lib.StateWorking,
				Terminal:     false,
				CreatedAt:    now.Add(-25 * time.Minute),
				UpdatedAt:    now.Add(-2 * time.Minute),
			},
		},
	}
	sess.DeriveActiveSessionPods(context.Background())
	prof = sess.Profile()
	if len(prof.AgentProfiles) != 0 {
		t.Fatalf("expected 0 cluster/specialist AgentProfiles autopopulated on connect, got %d: %v", len(prof.AgentProfiles), prof.AgentProfiles)
	}
	if len(prof.ActiveSessionPods) != 2 {
		t.Fatalf("expected 2 active session pods, got %d: %+v", len(prof.ActiveSessionPods), prof.ActiveSessionPods)
	}

	snap, _ := BuildSwarmState(SwarmProjectionInput{
		RootConversationID: "new-root-conv",
		Deployment:         prof,
		Connected:          true,
		Now:                now,
		Conversations: map[string]ConversationEntry{
			"session-cluster-explorer": {
				Alias:                   "session-cluster-explorer",
				Token:                   "agy-835cc7f83f54",
				SessionMode:             false,
				AGYConversationID:       "sub-explorer-uuid",
				ParentAGYConversationID: "earlier-root-conv",
				SubagentRole:            "Session Cluster Explorer",
				CreatedAt:               now.Add(-30 * time.Minute),
			},
			"session-dead-yesterday": {
				Alias:                   "session-dead-yesterday",
				Token:                   "agy-dead000",
				SessionWorkerID:         "chat-badger-eff4bb07",
				SessionMode:             false,
				AGYConversationID:       "sub-dead-uuid",
				ParentAGYConversationID: "earlier-root-conv",
				SubagentRole:            "Session Dead Yesterday",
				CreatedAt:               now.Add(-24 * time.Hour),
				LastUsedAt:              now.Add(-24 * time.Hour),
			},
		},
	}, BoardLedger{})

	if snap.Title != "kube-agents swarm" {
		t.Fatalf("expected title 'kube-agents swarm', got %q", snap.Title)
	}
	for _, wantMember := range []string{
		"coordinator",
		"narrator",
		"platform",
		"session-cluster-explorer",
		"chat-marten-4d134d0d",
	} {
		if _, exists := snap.Members[wantMember]; !exists {
			t.Fatalf("expected pre-created member %q on initial connection, got %+v", wantMember, snap.Members)
		}
	}
	for _, unwantedCluster := range []string{
		"cluster-kube-agents-host",
		"cluster-drift-peer-std-1",
		"cluster-scale-ap-01",
		"session-dead-yesterday",
	} {
		if _, leaked := snap.Members[unwantedCluster]; leaked {
			t.Fatalf("member %q must not appear on initial connection roster: %+v", unwantedCluster, snap.Members)
		}
	}
	if snap.Members["session-cluster-explorer"].ConversationID != "sub-explorer-uuid" {
		t.Fatalf("expected session-cluster-explorer to preserve clickable subagent UUID 'sub-explorer-uuid', got %q", snap.Members["session-cluster-explorer"].ConversationID)
	}
}

func TestBuildSwarmStateMultiPodSessionRespawnAndTaskIsolation(t *testing.T) {
	now := time.Date(2026, 10, 18, 20, 0, 0, 0, time.UTC)
	rootID := "a9505313-76e7-443a-b723-e054857203b3"
	poetID := "f017a8f2-234c-4471-bb8e-30c30ace1dbf"
	otherRootID := "9dd87ed9-09b7-48b9-a2ab-f5aa78525166"

	input := SwarmProjectionInput{
		RootConversationID: rootID,
		Deployment: DeploymentProfile{
			Name:                "kube-agents-host",
			Namespace:           "kubeagents-system",
			PlatformAgentName:   "platform-agent",
			LastConnectedAt:     now.Add(-5 * time.Minute),
			SessionPodsProbedAt: now,
		},
		Connected: true,
		Now:       now,
		Conversations: map[string]ConversationEntry{
			"agy-a9505313": {
				Alias:             "agy-a9505313",
				Token:             "agy-ee0a75104381",
				AGYConversationID: rootID,
				CreatedAt:         now.Add(-5 * time.Minute),
				LastUsedAt:        now.Add(-5 * time.Minute),
			},
			"session-poet": {
				Alias:                   "session-poet",
				Token:                   "agy-f71ceb116d6d",
				SessionMode:             true,
				SessionWorkerID:         "chat-marten-4d134d0d", // stale older pod ID in config
				AGYConversationID:       poetID,
				ParentAGYConversationID: rootID,
				SubagentRole:            "Session Poet",
				CreatedAt:               now.Add(-3 * time.Hour),
				LastUsedAt:              now.Add(-3 * time.Minute),
			},
			"agy-9dd87ed9": {
				Alias:             "agy-9dd87ed9",
				Token:             "agy-6be11b2b0c33",
				AGYConversationID: otherRootID,
				CreatedAt:         now.Add(-1 * time.Hour),
				LastUsedAt:        now.Add(-1 * time.Minute),
			},
			"soak-live": {
				Alias:      "soak-live",
				Token:      "soak-tok-999",
				CreatedAt:  now.Add(-2 * time.Minute),
				LastUsedAt: now.Add(-1 * time.Minute),
			},
		},
		Tasks: []*TaskRecord{
			// Pod incarnation 1 (3 hours ago): chat-marten-4d134d0d
			{
				TaskID:       "task-poet-1",
				Addressee:    "chat-marten-4d134d0d",
				Conversation: "console:agy-f71ceb116d6d",
				PromptText:   "Write a short poem about Kubernetes",
				ResultText:   "Pods in the night",
				State:        lib.StateCompleted,
				Terminal:     true,
				CreatedAt:    now.Add(-3 * time.Hour),
				UpdatedAt:    now.Add(-3*time.Hour + 5*time.Second),
			},
			// Pod incarnation 2 (2 hours ago): chat-marten-eb19c4a8
			{
				TaskID:       "task-poet-2",
				Addressee:    "chat-marten-eb19c4a8",
				Conversation: "console:agy-f71ceb116d6d",
				PromptText:   "are you still here?",
				ResultText:   "Still here!",
				State:        lib.StateCompleted,
				Terminal:     true,
				CreatedAt:    now.Add(-2 * time.Hour),
				UpdatedAt:    now.Add(-2*time.Hour + 5*time.Second),
			},
			// Pod incarnation 3 (3 minutes ago): chat-heron-6690e678
			{
				TaskID:       "task-poet-3",
				Addressee:    "chat-heron-6690e678",
				Conversation: "console:agy-f71ceb116d6d",
				PromptText:   "tell me another poem",
				ResultText:   "etcd's Whisper",
				State:        lib.StateCompleted,
				Terminal:     true,
				CreatedAt:    now.Add(-3 * time.Minute),
				UpdatedAt:    now.Add(-3*time.Minute + 5*time.Second),
			},
			// Unrelated Antigravity conversation's platform task (must be excluded from board)
			{
				TaskID:       "task-other-root",
				Addressee:    "platform",
				Conversation: "console:agy-6be11b2b0c33",
				PromptText:   "soak test prompt from other root conversation",
				ResultText:   "ok",
				State:        lib.StateCompleted,
				Terminal:     true,
				CreatedAt:    now.Add(-2 * time.Minute),
				UpdatedAt:    now.Add(-2*time.Minute + 5*time.Second),
			},
			// Soak conversation's task (must be excluded from board)
			{
				TaskID:       "task-soak-live",
				Addressee:    "platform",
				Conversation: "console:soak-tok-999",
				PromptText:   "soak-live check",
				ResultText:   "ok",
				State:        lib.StateCompleted,
				Terminal:     true,
				CreatedAt:    now.Add(-1 * time.Minute),
				UpdatedAt:    now.Add(-1*time.Minute + 5*time.Second),
			},
		},
	}

	snap, _ := BuildSwarmState(input, BoardLedger{})

	// 1. Only @session-poet should be in members (never @chat-marten-eb19c4a8 or @chat-heron-6690e678).
	if _, ok := snap.Members["session-poet"]; !ok {
		t.Fatalf("expected session-poet in members: %+v", snap.Members)
	}
	for _, fake := range []string{"chat-marten-4d134d0d", "chat-marten-eb19c4a8", "chat-heron-6690e678", "soak-live", "agy-9dd87ed9"} {
		if _, leaked := snap.Members[fake]; leaked {
			t.Fatalf("unexpected member %q in Swarm roster: %+v", fake, snap.Members)
		}
	}
	// 2. session-poet's Model must reflect the newest pod ID (chat-heron-6690e678), not the stale chat-marten-4d134d0d.
	if !strings.Contains(snap.Members["session-poet"].Model, "chat-heron-6690e678") {
		t.Fatalf("expected session-poet model to reflect newest pod chat-heron-6690e678, got %q", snap.Members["session-poet"].Model)
	}
	// 3. All 3 poet tasks across all 3 pod incarnations must appear on the board attributed to session-poet,
	// and tasks from agy-9dd87ed9 / soak-live must NOT appear on the board.
	seenPoetTasks := map[string]bool{}
	for _, msg := range snap.NewMessages {
		if strings.Contains(msg.Text, "task-other-root") || strings.Contains(msg.Text, "task-soak-live") {
			t.Fatalf("unrelated or soak conversation task leaked onto coordinator board: %+v", msg)
		}
		if strings.Contains(msg.Agent, "chat-") {
			t.Fatalf("raw pod name leaked as board agent: %+v", msg)
		}
		for _, tid := range []string{"task-poet-1", "task-poet-2", "task-poet-3"} {
			if strings.Contains(msg.Text, tid) && msg.Agent == "session-poet" {
				seenPoetTasks[tid] = true
			}
		}
	}
	for _, tid := range []string{"task-poet-1", "task-poet-2", "task-poet-3"} {
		if !seenPoetTasks[tid] {
			t.Fatalf("expected %s completion attributed to session-poet across pod respawns, seen=%+v, messages=%+v", tid, seenPoetTasks, snap.NewMessages)
		}
	}
}

func TestBuildSwarmStateCrossConversationSoakIsolationAndIdleSessionRetention(t *testing.T) {
	now := time.Date(2026, 10, 19, 16, 40, 0, 0, time.UTC)
	kageRootID := "ed17511a-eb6b-4ba0-8f98-6c3421212ca2"
	agyMcpRootID := "9dd87ed9-09b7-48b9-a2ab-f5aa78525166"
	poetSubID := "f017a8f2-234c-4471-bb8e-30c30ace1dbf"
	explorerSubID := "638a8ddb-6a7e-4871-bba8-dbadbe63fafb"
	agySoakSubID := "fb65c0f9-7982-4080-a34c-fc09b5891ddc"
	deletedOldRootID := "176abbd9-c662-4ff9-86c8-246f35668b60"

	rootDir := t.TempDir()
	brainDir := filepath.Join(rootDir, "brain")
	convDir := filepath.Join(rootDir, "conversations")
	if err := os.MkdirAll(brainDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(convDir, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, liveID := range []string{kageRootID, agyMcpRootID, poetSubID, explorerSubID, agySoakSubID} {
		if err := os.WriteFile(filepath.Join(convDir, liveID+".db"), []byte("sqlite"), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(filepath.Join(brainDir, liveID), 0o755); err != nil {
			t.Fatal(err)
		}
	}

	input := SwarmProjectionInput{
		RootConversationID: kageRootID,
		BrainDir:           brainDir,
		Deployment: DeploymentProfile{
			Name:              "kube-agents-host",
			Namespace:         "kubeagents-system",
			PlatformAgentName: "platform-agent",
			LastConnectedAt:   now.Add(-10 * time.Minute),
			// Leftover soak pods belonging to agy-mcp's soak-sess-4 and soak-sess-8:
			ActiveSessionPods: []SessionPodInfo{
				{Name: "chat-heron-c81ad99f", Addressee: "chat-heron-c81ad99f", SessionKey: "console:agy-e3ac81cd521a", Phase: "Running", CreatedAt: now.Add(-15 * time.Minute)},
				{Name: "chat-stoat-48fc0157", Addressee: "chat-stoat-48fc0157", SessionKey: "console:agy-6037a4dacc31", Phase: "Running", CreatedAt: now.Add(-15 * time.Minute)},
			},
			SessionPodsProbedAt: now,
		},
		Connected: true,
		Now:       now,
		Conversations: map[string]ConversationEntry{
			"agy-ed17511a": {
				Alias:             "agy-ed17511a",
				Token:             "agy-0e43a555ae82",
				AGYConversationID: kageRootID,
				CreatedAt:         now.Add(-10 * time.Minute),
				LastUsedAt:        now.Add(-1 * time.Minute),
			},
			"agy-9dd87ed9": {
				Alias:             "agy-9dd87ed9",
				Token:             "agy-6be11b2b0c33",
				AGYConversationID: agyMcpRootID,
				CreatedAt:         now.Add(-3 * time.Hour),
				LastUsedAt:        now.Add(-5 * time.Minute),
			},
			// Active /session subagents whose 30m idle pods were reaped >2h ago (no live pod, no recent task):
			"session-poet": {
				Alias:                   "session-poet",
				Token:                   "agy-f71ceb116d6d",
				SessionMode:             true,
				SessionWorkerID:         "chat-heron-6690e678",
				AGYConversationID:       poetSubID,
				ParentAGYConversationID: deletedOldRootID, // even before adoption completes
				SubagentRole:            "Session Poet",
				CreatedAt:               now.Add(-4 * time.Hour),
				LastUsedAt:              now.Add(-3 * time.Hour),
			},
			"session-cluster-explorer": {
				Alias:                   "session-cluster-explorer",
				Token:                   "agy-835cc7f83f54",
				SessionMode:             true,
				SessionWorkerID:         "chat-lynx-6404f129",
				AGYConversationID:       explorerSubID,
				ParentAGYConversationID: kageRootID,
				SubagentRole:            "Session Cluster Explorer",
				CreatedAt:               now.Add(-4 * time.Hour),
				LastUsedAt:              now.Add(-3 * time.Hour),
			},
			// agy-mcp's soak sessions (both running and terminated) and terminated subagent:
			"soak-sess-1": {
				Alias:             "soak-sess-1",
				Token:             "agy-soak1",
				SessionMode:       false,
				SessionTerminated: true,
				SessionWorkerID:   "chat-heron-111",
				AGYConversationID: agyMcpRootID,
				CreatedAt:         now.Add(-20 * time.Minute),
				LastUsedAt:        now.Add(-18 * time.Minute),
			},
			"soak-sess-4": {
				Alias:             "soak-sess-4",
				Token:             "agy-e3ac81cd521a",
				SessionMode:       true,
				SessionWorkerID:   "chat-heron-c81ad99f",
				AGYConversationID: agyMcpRootID,
				CreatedAt:         now.Add(-15 * time.Minute),
				LastUsedAt:        now.Add(-14 * time.Minute),
			},
			"soak-sess-8": {
				Alias:             "soak-sess-8",
				Token:             "agy-6037a4dacc31",
				SessionMode:       true,
				SessionWorkerID:   "chat-stoat-48fc0157",
				AGYConversationID: agyMcpRootID,
				CreatedAt:         now.Add(-15 * time.Minute),
				LastUsedAt:        now.Add(-14 * time.Minute),
			},
			"session-zero-kubectl-soak": {
				Alias:                   "session-zero-kubectl-soak",
				Token:                   "agy-c018d0d0800f",
				SessionMode:             false,
				SessionTerminated:       true,
				SessionWorkerID:         "chat-lynx-48870827",
				AGYConversationID:       agySoakSubID,
				ParentAGYConversationID: agyMcpRootID,
				SubagentRole:            "Session Zero-Kubectl Soak",
				CreatedAt:               now.Add(-25 * time.Minute),
				LastUsedAt:              now.Add(-24 * time.Minute),
			},
		},
		Subagents: map[string]ConversationLineage{
			poetSubID: {
				ConversationID:       poetSubID,
				ParentConversationID: kageRootID,
				RootConversationID:   kageRootID,
				SubagentType:         "kube-session",
				SubagentRole:         "Session Poet",
				IsSubagent:           true,
			},
			explorerSubID: {
				ConversationID:       explorerSubID,
				ParentConversationID: kageRootID,
				RootConversationID:   kageRootID,
				SubagentType:         "kube-session",
				SubagentRole:         "Session Cluster Explorer",
				IsSubagent:           true,
			},
		},
	}

	snap, _ := BuildSwarmState(input, BoardLedger{})

	// 1. Both coordinator and platform must link back to kageRootID.
	if snap.Members["coordinator"].ConversationID != kageRootID {
		t.Fatalf("coordinator ConversationID = %q, want %q", snap.Members["coordinator"].ConversationID, kageRootID)
	}
	if snap.Members["platform"].ConversationID != kageRootID {
		t.Fatalf("platform ConversationID = %q, want %q", snap.Members["platform"].ConversationID, kageRootID)
	}

	// 2. session-poet and session-cluster-explorer must be active (Killed == false) and link to their subagent UUIDs.
	for name, wantSubID := range map[string]string{
		"session-poet":             poetSubID,
		"session-cluster-explorer": explorerSubID,
	} {
		m, ok := snap.Members[name]
		if !ok {
			t.Fatalf("missing expected member %q in %+v", name, snap.Members)
		}
		if m.Killed {
			t.Fatalf("member %q with SessionMode=true must NOT be marked Killed when its 30m idle pod was reaped: %+v", name, m)
		}
		if m.ConversationID != wantSubID {
			t.Fatalf("member %q ConversationID = %q, want %q", name, m.ConversationID, wantSubID)
		}
	}

	// 3. Zero soak sessions or agy-mcp conversations may appear in snap.Members.
	for name, m := range snap.Members {
		if strings.Contains(name, "soak") || strings.Contains(name, "heron") || strings.Contains(name, "stoat") {
			t.Fatalf("leaked soak session or pod %q into Connect To Example Cluster Swarm roster: %+v", name, m)
		}
		if m.ConversationID == agyMcpRootID || m.ConversationID == agySoakSubID {
			t.Fatalf("member %q leaked agy-mcp conversation_id %q", name, m.ConversationID)
		}
	}

	// 4. Verify DeleteConversation and ClearKilledSessions on ConfigStore.
	store, err := NewConfigStore(filepath.Join(rootDir, "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	for alias, entry := range input.Conversations {
		_, _ = store.EnsureConversationWithOptions(alias, false, entry.Description, false)
		if entry.SessionMode {
			_, _ = store.SetConversationSessionMode(alias, true)
		} else if entry.SessionTerminated {
			_, _ = store.SetConversationSessionMode(alias, true)
			_, _ = store.SetConversationSessionMode(alias, false)
		}
	}
	if deleted, ok := store.DeleteConversation("session-soak-sess-4"); !ok || deleted.Alias != "soak-sess-4" {
		t.Fatalf("expected DeleteConversation('session-soak-sess-4') to delete soak-sess-4, got ok=%v entry=%+v", ok, deleted)
	}
	removed := store.ClearKilledSessions(nil)
	if len(removed) == 0 {
		t.Fatalf("expected ClearKilledSessions to remove terminated/inactive sessions")
	}
	afterClear := store.Snapshot().Conversations
	if _, stillHasSoak1 := afterClear["soak-sess-1"]; stillHasSoak1 {
		t.Fatalf("expected ClearKilledSessions to remove soak-sess-1")
	}
	if _, stillHasZeroSoak := afterClear["session-zero-kubectl-soak"]; stillHasZeroSoak {
		t.Fatalf("expected ClearKilledSessions to remove session-zero-kubectl-soak")
	}
	if _, hasPoet := afterClear["session-poet"]; !hasPoet {
		t.Fatalf("expected ClearKilledSessions to preserve active session-poet")
	}
	if _, hasExplorer := afterClear["session-cluster-explorer"]; !hasExplorer {
		t.Fatalf("expected ClearKilledSessions to preserve active session-cluster-explorer")
	}

	// 5. Verify ViewHTML handles narrator_bootstrap.js onData(view, meta) and navigate(action) protocol.
	if !strings.Contains(snap.ViewHTML, "var v = (d && d.view) ? d.view : (d || {});") {
		t.Fatalf("expected ViewHTML to unwrap either d.view or direct view argument from narrator.onData")
	}
	if !strings.Contains(snap.ViewHTML, "window.narrator.navigate(") {
		t.Fatalf("expected ViewHTML to use window.narrator.navigate(action)")
	}
}

func TestDuplicateSessionSubagentsStayIsolatedAndVisibleOnGraph(t *testing.T) {
	baseDir := t.TempDir()
	cfgPath := filepath.Join(baseDir, "mcp-config.json")
	store, err := NewConfigStore(cfgPath)
	if err != nil {
		t.Fatal(err)
	}

	rootID := "ad232663-8050-4c77-9b1b-d80e0968906b"
	sub1ID := "11111111-1111-1111-1111-111111111111"
	sub2ID := "43ee6fca-bbaf-4e67-aa58-3abf48629a1d"

	now := time.Now().UTC()

	// 1. Seed an old unbound "session-test" (cid=="", pid=="") and verify that once the coordinator
	// binds its own conversation before syncSwarm (matching handleConnectDeployment order),
	// BuildSwarmState does NOT leak the unbound session-test onto the new coordinator's board.
	_, err = store.EnsureConversationWithOptions("session-test", false, "Old test", false)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = store.SetConversationSessionMode("session-test", true)

	// Coordinator binds its own conversation before syncSwarm (matching handleConnectDeployment order).
	coordLin := ConversationLineage{
		ConversationID:     rootID,
		RootConversationID: rootID,
	}
	store.BindConversationWithLineage("default", coordLin)

	initialState, _ := BuildSwarmState(SwarmProjectionInput{
		RootConversationID: rootID,
		Connected:          true,
		Conversations:      store.Snapshot().Conversations,
		Now:                now,
	}, BoardLedger{})
	if _, leaked := initialState.Members["session-test"]; leaked {
		t.Fatalf("expected unbound session-test not to leak onto coordinator board when hasOwnCoordinatorEntry=true, got %+v", initialState.Members["session-test"])
	}
	_, _ = store.DeleteConversation("session-test")

	// 2. Subagent 1 ("Session Test") spawns and runs connect_deployment -> session_on(alias: "session-test") -> send_message(conversation: "session-test").
	lin1 := ConversationLineage{
		ConversationID:       sub1ID,
		ParentConversationID: rootID,
		RootConversationID:   rootID,
		SubagentType:         "kube-session",
		SubagentRole:         "Session Test",
		SubagentState:        "SUBAGENT_STATE_WAITING",
		IsSubagent:           true,
	}
	c1Connect, err := store.EnsureConversationForSubagent(lin1, "", false, "")
	if err != nil {
		t.Fatal(err)
	}
	if c1Connect.Alias != "session-test" || c1Connect.AGYConversationID != sub1ID {
		t.Fatalf("expected sub1 connect alias=session-test cid=%s, got %+v", sub1ID, c1Connect)
	}
	c1SessionOn, err := store.EnsureConversationForSubagent(lin1, "session-test", false, "")
	if err != nil {
		t.Fatal(err)
	}
	if c1SessionOn.Alias != "session-test" || c1SessionOn.AGYConversationID != sub1ID {
		t.Fatalf("expected sub1 session_on alias=session-test cid=%s, got %+v", sub1ID, c1SessionOn)
	}
	_, _ = store.SetConversationSessionMode(c1SessionOn.Alias, true)
	store.SetConversationSessionWorkerID(c1SessionOn.Alias, "s-test1")

	// 3. Subagent 2 (also named "Session Test" / passing alias "session-test") spawns and runs connect_deployment -> session_on(alias: "session-test") -> send_message(conversation: "session-test").
	lin2 := ConversationLineage{
		ConversationID:       sub2ID,
		ParentConversationID: rootID,
		RootConversationID:   rootID,
		SubagentType:         "kube-session",
		SubagentRole:         "Session Test",
		SubagentState:        "SUBAGENT_STATE_WAITING",
		IsSubagent:           true,
	}
	c2Connect, err := store.EnsureConversationForSubagent(lin2, "", false, "")
	if err != nil {
		t.Fatal(err)
	}
	if c2Connect.Alias != "session-test-2" || c2Connect.AGYConversationID != sub2ID {
		t.Fatalf("expected sub2 connect to disambiguate to alias=session-test-2 cid=%s, got %+v", sub2ID, c2Connect)
	}
	// Even when Subagent 2 explicitly passes alias="session-test" to session_on or send_message,
	// it must stay on its own "session-test-2" entry and NEVER steal or unbind "session-test"!
	c2SessionOn, err := store.EnsureConversationForSubagent(lin2, "session-test", false, "")
	if err != nil {
		t.Fatal(err)
	}
	if c2SessionOn.Alias != "session-test-2" || c2SessionOn.AGYConversationID != sub2ID {
		t.Fatalf("expected sub2 session_on(alias=session-test) to stay on session-test-2 cid=%s, got %+v", sub2ID, c2SessionOn)
	}
	_, _ = store.SetConversationSessionMode(c2SessionOn.Alias, true)
	store.SetConversationSessionWorkerID(c2SessionOn.Alias, "s-test2")

	// Verify Subagent 1 is still bound to "session-test" with its ConversationID intact.
	snap := store.Snapshot()
	afterC1, ok1 := snap.Conversations["session-test"]
	afterC2, ok2 := snap.Conversations["session-test-2"]
	if !ok1 || afterC1.AGYConversationID != sub1ID {
		t.Fatalf("expected session-test to remain bound to sub1 (%s), got %+v", sub1ID, afterC1)
	}
	if !ok2 || afterC2.AGYConversationID != sub2ID {
		t.Fatalf("expected session-test-2 to remain bound to sub2 (%s), got %+v", sub2ID, afterC2)
	}

	// 4. Build swarm state with tasks for both sessions and verify both appear in Members and on the board (for Graph view).
	tasks := []*TaskRecord{
		{
			TaskID:       "t-sub1",
			Addressee:    "s-test1",
			Conversation: "console:" + afterC1.Token,
			State:        lib.StateCompleted,
			Terminal:     true,
			PromptText:   "First session prompt",
			ResultText:   "First session result",
			CreatedAt:    now.Add(-15 * time.Second),
			UpdatedAt:    now.Add(-12 * time.Second),
		},
		{
			TaskID:       "t-sub2",
			Addressee:    "s-test2",
			Conversation: "console:" + afterC2.Token,
			State:        lib.StateCompleted,
			Terminal:     true,
			PromptText:   "Second session prompt",
			ResultText:   "Second session result",
			CreatedAt:    now.Add(-4 * time.Second),
			UpdatedAt:    now.Add(-1 * time.Second),
		},
	}

	input := SwarmProjectionInput{
		RootConversationID: rootID,
		Deployment: DeploymentProfile{
			Name:              "kube-agents-host",
			Namespace:         "kubeagents-system",
			PlatformAgentName: "platform-agent",
			ClusterName:       "kube-agents-host",
			LastConnectedAt:   now.Add(-30 * time.Second),
		},
		Connected:     true,
		Conversations: snap.Conversations,
		Subagents: map[string]ConversationLineage{
			sub1ID: lin1,
			sub2ID: lin2,
		},
		Tasks: tasks,
		Now:   now,
	}

	state, _ := BuildSwarmState(input, BoardLedger{})
	m1, hasM1 := state.Members["session-test"]
	m2, hasM2 := state.Members["session-test-2"]
	if !hasM1 || m1.ConversationID != sub1ID {
		t.Fatalf("expected Members[session-test] with ConversationID=%s, got ok=%v member=%+v", sub1ID, hasM1, m1)
	}
	if !hasM2 || m2.ConversationID != sub2ID {
		t.Fatalf("expected Members[session-test-2] with ConversationID=%s, got ok=%v member=%+v", sub2ID, hasM2, m2)
	}

	var allText strings.Builder
	for _, msg := range state.NewMessages {
		allText.WriteString(msg.Agent + ": " + msg.Text + "\n")
	}
	boardStr := allText.String()
	if !strings.Contains(boardStr, "@session-test ") || !strings.Contains(boardStr, "@session-test-2 ") {
		t.Fatalf("expected board messages to contain @session-test and @session-test-2 posts for Graph view, got:\n%s", boardStr)
	}
	if !strings.Contains(boardStr, "session-test:") || !strings.Contains(boardStr, "session-test-2:") {
		t.Fatalf("expected board messages to contain replies from both session-test and session-test-2, got:\n%s", boardStr)
	}
}

func TestPeerRelayPrefixAttributesBoardEdgesBetweenSessions(t *testing.T) {
	for _, tc := range []struct {
		in       string
		wantPeer string
		wantRest string
	}{
		{"From @session-player-a: Move: 5\nboard", "session-player-a", "Move: 5\nboard"},
		{"  from @Session-Player-B :   O at 1", "Session-Player-B", "O at 1"},
		{"Move: 5 (From @session-player-a)", "", ""},
		{"@session-player-a: hello", "", ""},
		{"", "", ""},
	} {
		peer, rest := parsePeerRelayPrefix(tc.in)
		if peer != tc.wantPeer || rest != tc.wantRest {
			t.Fatalf("parsePeerRelayPrefix(%q) = (%q, %q), want (%q, %q)", tc.in, peer, rest, tc.wantPeer, tc.wantRest)
		}
	}

	now := time.Date(2026, 10, 20, 0, 0, 0, 0, time.UTC)
	rootID := "root-coord-uuid"
	subA := "sub-a-uuid"
	subB := "sub-b-uuid"
	input := SwarmProjectionInput{
		RootConversationID: rootID,
		BrainDir:           t.TempDir(),
		Deployment: DeploymentProfile{
			Name:              "kube-agents-host",
			Namespace:         "kubeagents-system",
			PlatformAgentName: "platform-agent",
			LastConnectedAt:   now.Add(-10 * time.Minute),
		},
		Connected: true,
		Conversations: map[string]ConversationEntry{
			"default": {Alias: "default", Token: "agy-root111", AGYConversationID: rootID, CreatedAt: now.Add(-10 * time.Minute)},
			"session-player-a": {
				Alias: "session-player-a", Token: "agy-aaa", SessionMode: true, SessionWorkerID: "chat-puffin-aaa",
				AGYConversationID: subA, ParentAGYConversationID: rootID, SubagentRole: "Player A", CreatedAt: now.Add(-9 * time.Minute),
			},
			"session-player-b": {
				Alias: "session-player-b", Token: "agy-bbb", SessionMode: true, SessionWorkerID: "chat-puffin-bbb",
				AGYConversationID: subB, ParentAGYConversationID: rootID, SubagentRole: "Player B", CreatedAt: now.Add(-9 * time.Minute),
			},
		},
		Subagents: map[string]ConversationLineage{
			subA: {ConversationID: subA, ParentConversationID: rootID, RootConversationID: rootID, SubagentType: "kube-session", SubagentRole: "Player A", IsSubagent: true},
			subB: {ConversationID: subB, ParentConversationID: rootID, RootConversationID: rootID, SubagentType: "kube-session", SubagentRole: "Player B", IsSubagent: true},
		},
		Tasks: []*TaskRecord{
			{
				TaskID: "task-a1", Addressee: "chat-puffin-aaa", Conversation: "console:agy-aaa",
				State: lib.StateCompleted, Terminal: true,
				PromptText: "You start. Board: empty.", ResultText: "Move: 5\n. | . | .\n. | X | .\n. | . | .",
				CreatedAt: now.Add(-8 * time.Minute), UpdatedAt: now.Add(-7 * time.Minute),
			},
			{
				TaskID: "task-b1", Addressee: "chat-puffin-bbb", Conversation: "console:agy-bbb",
				State: lib.StateCompleted, Terminal: true,
				PromptText: "From @session-player-a: Move: 5\n. | . | .\n. | X | .\n. | . | .", ResultText: "Move: 1\nO | . | .\n. | X | .\n. | . | .",
				CreatedAt: now.Add(-6 * time.Minute), UpdatedAt: now.Add(-5 * time.Minute),
			},
			{
				TaskID: "task-a2", Addressee: "chat-puffin-aaa", Conversation: "console:agy-aaa",
				State: lib.StateWorking, Terminal: false,
				PromptText: "From @session-player-b: Move: 1\nO | . | .\n. | X | .\n. | . | .",
				CreatedAt:  now.Add(-4 * time.Minute), UpdatedAt: now.Add(-3 * time.Minute),
			},
			{
				// A self-prefixed or unknown-member prefix must fall back to the coordinator.
				TaskID: "task-b2", Addressee: "chat-puffin-bbb", Conversation: "console:agy-bbb",
				State: lib.StateCompleted, Terminal: true,
				PromptText: "From @session-referee: are you ready?", ResultText: "Ready.",
				CreatedAt: now.Add(-2 * time.Minute), UpdatedAt: now.Add(-1 * time.Minute),
			},
		},
		Now: now,
	}

	snap, _ := BuildSwarmState(input, BoardLedger{})
	byText := func(substr string) *SwarmBoardMessage {
		for i := range snap.NewMessages {
			if strings.Contains(snap.NewMessages[i].Text, substr) {
				return &snap.NewMessages[i]
			}
		}
		return nil
	}

	// Opening turn with no prefix: coordinator -> session-player-a as before.
	if m := byText("[task `task-a1`]"); m == nil || m.Agent != swarmMemberCoordinator {
		t.Fatalf("expected task-a1 prompt to be posted by coordinator, got %+v", m)
	}
	// Relayed turn: session-player-a -> session-player-b, with the prefix stripped so the only @mention is the target.
	if m := byText("[task `task-b1`]"); m == nil || m.Agent != "session-player-a" || !strings.HasPrefix(m.Text, "@session-player-b [task `task-b1`] Move: 5") || strings.Contains(m.Text, "From @") {
		t.Fatalf("expected task-b1 prompt to be posted by session-player-a addressed to @session-player-b without the relay prefix, got %+v", m)
	}
	// Its completion answers the peer, not the coordinator.
	if m := byText("(`task-b1`): Move: 1"); m == nil || m.Agent != "session-player-b" || !strings.HasPrefix(m.Text, "@session-player-a `completed`") {
		t.Fatalf("expected task-b1 completion to be posted by session-player-b to @session-player-a, got %+v", m)
	}
	// The reverse hop while still running: session-player-b -> session-player-a.
	if m := byText("[task `task-a2`]"); m == nil || m.Agent != "session-player-b" || !strings.HasPrefix(m.Text, "@session-player-a [task `task-a2`] Move: 1") {
		t.Fatalf("expected task-a2 prompt to be posted by session-player-b addressed to @session-player-a, got %+v", m)
	}
	// Unknown member in the prefix: coordinator keeps authorship and the text is left untouched.
	if m := byText("[task `task-b2`]"); m == nil || m.Agent != swarmMemberCoordinator || !strings.Contains(m.Text, "From @session-referee: are you ready?") {
		t.Fatalf("expected task-b2 prompt to stay attributed to coordinator with its text untouched, got %+v", m)
	}
	if m := byText("(`task-b2`): Ready."); m == nil || !strings.HasPrefix(m.Text, "@coordinator `completed`") {
		t.Fatalf("expected task-b2 completion to answer @coordinator, got %+v", m)
	}
}
