package mcp

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	swarmDirName            = "swarm"
	swarmMembersFileName    = "members.json"
	swarmBoardFileName      = "board.jsonl"
	swarmViewJSONFileName   = "view.json"
	swarmViewHTMLFileName   = "view.html"
	swarmMetaFileName       = "swarm.json"
	swarmLedgerFileName     = "kube_agents_swarm_state.json"
	swarmLockFileName       = "kube_agents_swarm.lock"
	swarmDefaultTitle       = "kube-agents swarm"
	swarmLedgerSchema       = 8
	swarmManagedByTag       = "kube-agents"
	swarmRoleCoordinator    = "coordinator"
	swarmRoleNarrator       = "narrator"
	swarmRoleWorker         = "worker"
	swarmWakeMentions       = "mentions"
	swarmWakeWorking        = "working"
	swarmMemberCoordinator  = "coordinator"
	swarmMemberNarrator     = "narrator"
	swarmMemberPlatform     = "platform"
	swarmMemberGateway      = "gateway"
	maxBoardEventTextChars  = 240
	maxViewHeadlineChars    = 280
	maxViewTitleChars       = 150
	maxViewWhyChars         = 360
	maxViewTextItemChars    = 280
	maxWorkstreamsInView    = 24
	maxAttentionItemsInView = 16
	maxMilestonesInView     = 16
	maxRisksInView          = 12
)

// SwarmSink persists projected kube-agents fleet state into a Swarm view.
type SwarmSink interface {
	Sync(ctx context.Context, input SwarmProjectionInput) error
}

// SwarmProjectionInput holds the structured state inputs used to project a kube-agents
// deployment, its NATS JetStream bus state, and its Antigravity conversations/subagents into
// a native Antigravity Swarm board.
type SwarmProjectionInput struct {
	RootConversationID string
	BrainDir           string
	Deployment         DeploymentProfile
	Connected          bool
	Conversations      map[string]ConversationEntry
	Subagents          map[string]ConversationLineage
	AgentCards         map[string]map[string]any
	Tasks              []*TaskRecord
	Topics             []TopicSnapshot
	Now                time.Time
}

// SwarmRosterFile represents the top-level .system_generated/swarm/members.json document
// parsed by Antigravity's native Swarm UI sidecar (parseRoster in swarm_board/main.mjs).
type SwarmRosterFile struct {
	Title   string                 `json:"title"`
	Members map[string]SwarmMember `json:"members"`
}

// SwarmMember represents a single entry in .system_generated/swarm/members.json.
type SwarmMember struct {
	Name               string `json:"name"`
	Role               string `json:"role"`
	Model              string `json:"model,omitempty"`
	ConversationID     string `json:"conversation_id"`
	WakeMode           string `json:"wake_mode"`
	WaitSince          int    `json:"wait_since"`
	LastReadSeq        int    `json:"last_read_seq"`
	LastNudgedSeq      int    `json:"last_nudged_seq"`
	LastParkBlockedSeq int    `json:"last_park_blocked_seq"`
	JoinedAt           string `json:"joined_at,omitempty"`
	Killed             bool   `json:"killed"`
	ManagedBy          string `json:"managed_by,omitempty"`
}

// SwarmBoardMessage represents a single line in .system_generated/swarm/board.jsonl
// parsed by Antigravity's native Swarm UI sidecar (readBoardTail in swarm_board/main.mjs).
type SwarmBoardMessage struct {
	Seq   int    `json:"seq"`
	Agent string `json:"agent"`
	Text  string `json:"text"`
	Time  string `json:"time"`
}

// SwarmRef cites a board post (#seq) or a roster member in view.json.
type SwarmRef struct {
	Seq    int    `json:"seq,omitempty"`
	Member string `json:"member,omitempty"`
}

// SwarmAction represents a validated navigation button in view.json.
type SwarmAction struct {
	Type   string `json:"type"`
	Target any    `json:"target"`
	Label  string `json:"label"`
}

// SwarmPhase describes the current high-level phase in view.json.
type SwarmPhase struct {
	Label string `json:"label"`
	Index int    `json:"index"`
	Total int    `json:"total"`
}

// SwarmAttentionItem represents an item in view.json's attention list.
type SwarmAttentionItem struct {
	ID       string        `json:"id"`
	Kind     string        `json:"kind"`
	Severity string        `json:"severity"`
	Title    string        `json:"title"`
	Why      string        `json:"why,omitempty"`
	Refs     []SwarmRef    `json:"refs"`
	Actions  []SwarmAction `json:"actions,omitempty"`
}

// SwarmWorkstream represents an item in view.json's workstreams list.
type SwarmWorkstream struct {
	ID      string        `json:"id"`
	Title   string        `json:"title"`
	Owners  []string      `json:"owners"`
	State   string        `json:"state"`
	Note    string        `json:"note,omitempty"`
	Refs    []SwarmRef    `json:"refs"`
	Actions []SwarmAction `json:"actions,omitempty"`
}

// SwarmTextItem represents a milestone or risk item in view.json.
type SwarmTextItem struct {
	ID      string        `json:"id"`
	Text    string        `json:"text"`
	Refs    []SwarmRef    `json:"refs"`
	Actions []SwarmAction `json:"actions,omitempty"`
}

// SwarmView represents the structured .system_generated/swarm/view.json document.
type SwarmView struct {
	Version     int                  `json:"version"`
	AsOfSeq     int                  `json:"as_of_seq"`
	UpdatedAt   string               `json:"updated_at"`
	Headline    string               `json:"headline"`
	Phase       *SwarmPhase          `json:"phase,omitempty"`
	Attention   []SwarmAttentionItem `json:"attention"`
	Workstreams []SwarmWorkstream    `json:"workstreams"`
	Milestones  []SwarmTextItem      `json:"milestones"`
	Risks       []SwarmTextItem      `json:"risks"`
}

// BoardLedger tracks already-emitted event keys and their assigned board sequence numbers
// so board.jsonl is strictly append-only and idempotent across syncs and restarts.
type BoardLedger struct {
	SchemaVersion int            `json:"schemaVersion,omitempty"`
	NextSeq       int            `json:"nextSeq"`
	EventSeqs     map[string]int `json:"eventSeqs"`
	// Authors records every kube-agents-managed member name this projection has written board
	// lines as, so a later sync can tell a line left behind by a member that no longer exists
	// (and must trigger a rebuild) from a line posted by a member Antigravity or the user added.
	Authors map[string]bool `json:"authors,omitempty"`
}

// SwarmSnapshot is the complete projected state produced by BuildSwarmState.
type SwarmSnapshot struct {
	Title       string                 `json:"title"`
	Members     map[string]SwarmMember `json:"members"`
	NewMessages []SwarmBoardMessage    `json:"newMessages,omitempty"`
	View        SwarmView              `json:"view"`
	ViewHTML    string                 `json:"viewHtml,omitempty"`
}

type candidateBoardEvent struct {
	key         string
	ts          time.Time
	pinnedFirst bool
	priority    int
	orderIdx    int
	from        string
	citeKey     string
	taskID      string
	topicSub    string
	render      func(citeSeq int) string
}

type sessionWorkerProjection struct {
	memberName     string
	alias          string
	token          string
	workerID       string
	conversationID string
	description    string
	sessionMode    bool
	terminated     bool
	working        bool
	activeStep     string
	joinedAt       time.Time
}

func taskEffectiveCreatedAt(t *TaskRecord) time.Time {
	if t == nil {
		return time.Time{}
	}
	if !t.CreatedAt.IsZero() {
		return t.CreatedAt
	}
	return t.UpdatedAt
}

// BuildSwarmState is a pure, deterministic projection from SwarmProjectionInput and the
// previous BoardLedger to a SwarmSnapshot and updated BoardLedger.
func BuildSwarmState(input SwarmProjectionInput, prev BoardLedger) (SwarmSnapshot, BoardLedger) {
	now := input.Now
	if now.IsZero() {
		now = time.Now().UTC()
	}
	ledger := BoardLedger{
		SchemaVersion: swarmLedgerSchema,
		NextSeq:       prev.NextSeq,
		EventSeqs:     make(map[string]int, len(prev.EventSeqs)),
		Authors:       make(map[string]bool, len(prev.Authors)+4),
	}
	for name := range prev.Authors {
		ledger.Authors[name] = true
	}
	for k, v := range prev.EventSeqs {
		ledger.EventSeqs[k] = v
		if v >= ledger.NextSeq {
			ledger.NextSeq = v + 1
		}
	}
	if ledger.NextSeq <= 0 {
		ledger.NextSeq = 1
	}

	depName := firstNonEmptyStr(input.Deployment.Name, input.Deployment.ClusterName, "kube-agents")
	title := swarmDefaultTitle

	// Sort tasks chronologically by effective CreatedAt (with deterministic TaskID tie-breaker).
	rawTasks := make([]*TaskRecord, 0, len(input.Tasks))
	for _, t := range input.Tasks {
		if t != nil && t.TaskID != "" {
			rawTasks = append(rawTasks, t)
		}
	}
	sort.Slice(rawTasks, func(i, j int) bool {
		ti := taskEffectiveCreatedAt(rawTasks[i])
		tj := taskEffectiveCreatedAt(rawTasks[j])
		if ti.Equal(tj) {
			return rawTasks[i].TaskID < rawTasks[j].TaskID
		}
		if ti.IsZero() {
			return false
		}
		if tj.IsZero() {
			return true
		}
		return ti.Before(tj)
	})

	// 1. Discover and join /session workers structurally across Conversations, Subagents, AgentCards, and Tasks.
	sessions, tokenToSessionMember, workerIDToSessionMember, skippedTokens, skippedWorkers := projectSessionWorkers(input, rawTasks, now)

	// Filter out tasks belonging to expired historical /session workers as well as internal
	// Kanban specialist card sub-tasks (task-card-*) so only interactive platform & /session
	// conversations appear in the Swarm UI.
	skippedTaskIDs := make(map[string]bool)
	for _, t := range rawTasks {
		if strings.HasPrefix(t.TaskID, cardTaskIDPrefix) {
			skippedTaskIDs[t.TaskID] = true
			continue
		}
		token := strings.TrimPrefix(strings.TrimSpace(t.Conversation), consoleConvPrefix)
		if (token != "" && skippedTokens[token]) || (isSessionWorkerTask(t) && skippedWorkers[t.Addressee]) {
			skippedTaskIDs[t.TaskID] = true
		}
	}
	tasks := make([]*TaskRecord, 0, len(rawTasks))
	for _, t := range rawTasks {
		if skippedTaskIDs[t.TaskID] || (t.DelegatedFromTaskID != "" && skippedTaskIDs[t.DelegatedFromTaskID]) || (t.WakeFromChildTaskID != "" && skippedTaskIDs[t.WakeFromChildTaskID]) {
			skippedTaskIDs[t.TaskID] = true
			continue
		}
		tasks = append(tasks, t)
	}

	// 2. Determine active working state & live step titles for platform.
	platformWorking := false
	platformStep := ""

	for _, t := range tasks {
		if t.Terminal {
			continue
		}
		stepTitle := latestActiveStepTitle(t)
		if t.Addressee == "" || t.Addressee == swarmMemberPlatform {
			platformWorking = true
			if stepTitle != "" {
				platformStep = stepTitle
			}
		}
	}

	// 3. Build roster members map (coordinator, narrator for Overview tab, platform, and /session workers).
	joinedDefault := now.UTC().Format(time.RFC3339)
	if !input.Deployment.LastConnectedAt.IsZero() {
		joinedDefault = input.Deployment.LastConnectedAt.UTC().Format(time.RFC3339)
	}

	members := make(map[string]SwarmMember)
	members[swarmMemberCoordinator] = SwarmMember{
		Name:           swarmMemberCoordinator,
		Role:           swarmRoleCoordinator,
		Model:          fmt.Sprintf("kube-agents · %s", depName),
		ConversationID: strings.TrimSpace(input.RootConversationID),
		WakeMode:       swarmWakeMentions,
		JoinedAt:       joinedDefault,
		ManagedBy:      swarmManagedByTag,
	}
	members[swarmMemberNarrator] = SwarmMember{
		Name:           swarmMemberNarrator,
		Role:           swarmRoleNarrator,
		Model:          "a2a-jetstream/0.4 · topics-state",
		ConversationID: "",
		WakeMode:       swarmWakeMentions,
		JoinedAt:       joinedDefault,
		ManagedBy:      swarmManagedByTag,
	}

	platformModel := fmt.Sprintf("hermes · %s", firstNonEmptyStr(input.Deployment.PlatformAgentName, depName))
	if platformWorking && platformStep != "" {
		platformModel += " · ⚡ " + platformStep
	}
	platformWake := swarmWakeMentions
	if platformWorking {
		platformWake = swarmWakeWorking
	}
	members[swarmMemberPlatform] = SwarmMember{
		Name:           swarmMemberPlatform,
		Role:           swarmRoleWorker,
		Model:          platformModel,
		ConversationID: strings.TrimSpace(input.RootConversationID),
		WakeMode:       platformWake,
		JoinedAt:       joinedDefault,
		ManagedBy:      swarmManagedByTag,
	}

	for _, sess := range sessions {
		model := "claude-code · /session"
		if sess.workerID != "" {
			model = "claude-code · " + sess.workerID
		} else if sess.token != "" {
			model = "claude-code · console:" + truncateSwarmText(sess.token, 14)
		}
		if sess.working && sess.activeStep != "" {
			model += " · ⚡ " + sess.activeStep
		}
		wake := swarmWakeMentions
		if sess.working && !sess.terminated {
			wake = swarmWakeWorking
		}
		joined := joinedDefault
		if !sess.joinedAt.IsZero() {
			joined = sess.joinedAt.UTC().Format(time.RFC3339)
		}
		members[sess.memberName] = SwarmMember{
			Name:           sess.memberName,
			Role:           swarmRoleWorker,
			Model:          model,
			ConversationID: sess.conversationID,
			WakeMode:       wake,
			JoinedAt:       joined,
			Killed:         sess.terminated && !sess.sessionMode,
			ManagedBy:      swarmManagedByTag,
		}
	}

	// 5. Project discrete A2A lifecycle events onto board.jsonl using the idempotent ledger,
	// sorted chronologically and causally so multi-task histories always land in strict order.
	var newMessages []SwarmBoardMessage
	emit := func(key string, ts time.Time, from string, text string) int {
		if seq, exists := ledger.EventSeqs[key]; exists {
			return seq
		}
		if ts.IsZero() {
			ts = now
		}
		if _, ok := members[from]; !ok {
			from = swarmMemberPlatform
		}
		seq := ledger.NextSeq
		ledger.NextSeq++
		ledger.EventSeqs[key] = seq
		ledger.Authors[from] = true
		newMessages = append(newMessages, SwarmBoardMessage{
			Seq:   seq,
			Agent: from,
			Text:  strings.TrimSpace(text),
			Time:  ts.UTC().Format(time.RFC3339),
		})
		return seq
	}

	var candidates []candidateBoardEvent
	nextOrderIdx := 0
	addCandidate := func(ev candidateBoardEvent) {
		if ev.ts.IsZero() {
			ev.ts = now
		}
		ev.orderIdx = nextOrderIdx
		nextOrderIdx++
		candidates = append(candidates, ev)
	}

	// Deployment connection event (always pinned first on a new board).
	connTS := input.Deployment.LastConnectedAt
	if connTS.IsZero() {
		connTS = now
	}
	nsAndPA := firstNonEmptyStr(input.Deployment.Namespace, "kubeagents-system") + "/" + firstNonEmptyStr(input.Deployment.PlatformAgentName, "platform-agent")
	addCandidate(candidateBoardEvent{
		key:         "deploy:" + depName,
		ts:          connTS,
		pinnedFirst: true,
		priority:    0,
		from:        swarmMemberCoordinator,
		render: func(_ int) string {
			return fmt.Sprintf("Connected @%s to kube-agents deployment `%s` (`%s`).", swarmMemberPlatform, depName, nsAndPA)
		},
	})

	// Session start and bus registration events.
	for _, sess := range sessions {
		sessCopy := sess
		if sessCopy.sessionMode || sessCopy.terminated || sessCopy.workerID != "" {
			tokenLabel := ""
			if sessCopy.token != "" {
				tokenLabel = "console:" + sessCopy.token
			}
			workerLabel := firstNonEmptyStr(sessCopy.workerID, tokenLabel, sessCopy.memberName)
			descSuffix := ""
			if sessCopy.description != "" {
				descSuffix = " — " + truncateSwarmText(sessCopy.description, 100)
			}
			startKey := "session-start:" + sessCopy.memberName
			addCandidate(candidateBoardEvent{
				key:      startKey,
				ts:       sessCopy.joinedAt,
				priority: 1,
				from:     swarmMemberCoordinator,
				render: func(_ int) string {
					return fmt.Sprintf("@%s started persistent `/session` worker pod (`%s`)%s.", sessCopy.memberName, workerLabel, descSuffix)
				},
			})
			if !sessCopy.terminated {
				addCandidate(candidateBoardEvent{
					key:      "session-ready:" + sessCopy.memberName,
					ts:       sessCopy.joinedAt,
					priority: 2,
					from:     sessCopy.memberName,
					citeKey:  startKey,
					render: func(citeSeq int) string {
						cite := ""
						if citeSeq > 0 {
							cite = fmt.Sprintf(" (#%d)", citeSeq)
						}
						return fmt.Sprintf("Connected `/session` pod `%s`%s to @%s and @%s.", workerLabel, cite, swarmMemberPlatform, swarmMemberCoordinator)
					},
				})
			}
		}
	}

	// Helper to resolve which roster member owns a task.
	taskByID := make(map[string]*TaskRecord, len(tasks))
	for _, t := range tasks {
		taskByID[t.TaskID] = t
	}
	resolveTaskMember := func(t *TaskRecord) string {
		if t == nil {
			return swarmMemberPlatform
		}
		if m, ok := workerIDToSessionMember[t.Addressee]; ok {
			return m
		}
		token := strings.TrimPrefix(strings.TrimSpace(t.Conversation), consoleConvPrefix)
		if isSessionWorkerTask(t) {
			if m, ok := tokenToSessionMember[token]; ok {
				return m
			}
			cand := sanitizeMemberName(t.Addressee)
			if _, ok := members[cand]; ok {
				return cand
			}
		}
		cand := sanitizeMemberName(t.Addressee)
		if cand != "" {
			if _, ok := members[cand]; ok {
				return cand
			}
		}
		return swarmMemberPlatform
	}

	taskInSeq := make(map[string]int, len(tasks))
	taskLastSeq := make(map[string]int, len(tasks))
	// peerRelayFrom records, per task, the roster member a `From @<member>:` relay prefix named
	// as the real author of the prompt (the Session Playbook's peer relay), so the board shows a
	// session-to-session edge instead of routing every hop through @coordinator.
	peerRelayFrom := make(map[string]string, len(tasks))

	for _, t := range tasks {
		tCopy := t
		targetMember := resolveTaskMember(tCopy)
		promptSummary := truncateSwarmText(firstLine(firstNonEmptyStr(tCopy.PromptText, tCopy.StatusText, tCopy.TaskID)), maxBoardEventTextChars)
		createdTS := taskEffectiveCreatedAt(tCopy)
		if createdTS.IsZero() {
			createdTS = now
		}
		updatedTS := tCopy.UpdatedAt
		if updatedTS.IsZero() || updatedTS.Before(createdTS) {
			updatedTS = createdTS
		}
		inKey := "task-in:" + tCopy.TaskID

		switch {
		case tCopy.WakeFromChildTaskID != "":
			citeKey := "task-in:" + tCopy.WakeFromChildTaskID
			if childTask, ok := taskByID[tCopy.WakeFromChildTaskID]; ok && childTask.Terminal {
				citeKey = fmt.Sprintf("task-done:%s:%s", childTask.TaskID, childTask.State)
			}
			addCandidate(candidateBoardEvent{
				key:      inKey,
				ts:       createdTS,
				priority: 6,
				from:     swarmMemberPlatform,
				citeKey:  citeKey,
				taskID:   tCopy.TaskID,
				render: func(citeSeq int) string {
					cite := ""
					if citeSeq > 0 {
						cite = fmt.Sprintf(" (#%d)", citeSeq)
					}
					return fmt.Sprintf("@%s wake synthesis for delegated task `%s`%s: %s", targetMember, tCopy.WakeFromChildTaskID, cite, promptSummary)
				},
			})
		case tCopy.DelegatedFromTaskID != "":
			parentMember := swarmMemberCoordinator
			if parentTask, ok := taskByID[tCopy.DelegatedFromTaskID]; ok {
				parentMember = resolveTaskMember(parentTask)
			} else if token := strings.TrimPrefix(tCopy.Conversation, consoleConvPrefix); token != "" {
				if sm, ok := tokenToSessionMember[token]; ok {
					parentMember = sm
				}
			}
			addCandidate(candidateBoardEvent{
				key:      inKey,
				ts:       createdTS,
				priority: 3,
				from:     parentMember,
				citeKey:  "task-in:" + tCopy.DelegatedFromTaskID,
				taskID:   tCopy.TaskID,
				render: func(citeSeq int) string {
					cite := ""
					if citeSeq > 0 {
						cite = fmt.Sprintf(" (#%d)", citeSeq)
					}
					return fmt.Sprintf("@%s delegated task `%s`%s: %s", targetMember, tCopy.TaskID, cite, promptSummary)
				},
			})
		default:
			fromMember := swarmMemberCoordinator
			inSummary := promptSummary
			if peer, rest := parsePeerRelayPrefix(tCopy.PromptText); peer != "" {
				if cand := sanitizeMemberName(peer); cand != "" && cand != targetMember {
					if _, ok := members[cand]; ok {
						fromMember = cand
						peerRelayFrom[tCopy.TaskID] = cand
						if r := strings.TrimSpace(firstLine(rest)); r != "" {
							inSummary = truncateSwarmText(r, maxBoardEventTextChars)
						}
					}
				}
			}
			addCandidate(candidateBoardEvent{
				key:      inKey,
				ts:       createdTS,
				priority: 2,
				from:     fromMember,
				taskID:   tCopy.TaskID,
				render: func(_ int) string {
					return fmt.Sprintf("@%s [task `%s`] %s", targetMember, tCopy.TaskID, inSummary)
				},
			})
		}

		// Mid-flight steer turns.
		for idx, steer := range tCopy.SteerTurns {
			steerIdx := idx + 1
			steerText := truncateSwarmText(firstLine(steer), maxBoardEventTextChars)
			steerTS := updatedTS
			if idx < len(tCopy.SteerTimestamps) && !tCopy.SteerTimestamps[idx].IsZero() {
				steerTS = tCopy.SteerTimestamps[idx]
			}
			addCandidate(candidateBoardEvent{
				key:      fmt.Sprintf("task-steer:%s:%d", tCopy.TaskID, steerIdx),
				ts:       steerTS,
				priority: 4,
				from:     swarmMemberCoordinator,
				citeKey:  inKey,
				taskID:   tCopy.TaskID,
				render: func(citeSeq int) string {
					return fmt.Sprintf("@%s steer on #%d (`%s`): %s", targetMember, citeSeq, tCopy.TaskID, steerText)
				},
			})
		}

		// Input-required state.
		if tCopy.State == lib.StateInputRequired {
			prompt := truncateSwarmText(firstLine(firstNonEmptyStr(tCopy.StatusText, tCopy.ProgressText, tCopy.ResultText, "awaiting user input")), maxBoardEventTextChars)
			addCandidate(candidateBoardEvent{
				key:      "task-input:" + tCopy.TaskID,
				ts:       updatedTS,
				priority: 5,
				from:     targetMember,
				citeKey:  inKey,
				taskID:   tCopy.TaskID,
				render: func(citeSeq int) string {
					return fmt.Sprintf("@%s input required on #%d (`%s`): %s", swarmMemberCoordinator, citeSeq, tCopy.TaskID, prompt)
				},
			})
		}

		// Terminal completion or failure.
		if tCopy.Terminal {
			recipient := swarmMemberCoordinator
			if strings.HasPrefix(tCopy.TaskID, cardTaskIDPrefix) {
				recipient = swarmMemberPlatform
			} else if tCopy.DelegatedFromTaskID != "" {
				if parentTask, ok := taskByID[tCopy.DelegatedFromTaskID]; ok {
					recipient = resolveTaskMember(parentTask)
				}
			} else if peer, ok := peerRelayFrom[tCopy.TaskID]; ok {
				// A peer-relayed turn answers the session that asked, not the coordinator.
				recipient = peer
			}
			resSummary := extractTaskResultSummary(tCopy)
			addCandidate(candidateBoardEvent{
				key:      fmt.Sprintf("task-done:%s:%s", tCopy.TaskID, tCopy.State),
				ts:       updatedTS,
				priority: 5,
				from:     targetMember,
				citeKey:  inKey,
				taskID:   tCopy.TaskID,
				render: func(citeSeq int) string {
					return fmt.Sprintf("@%s `%s` #%d (`%s`): %s", recipient, tCopy.State, citeSeq, tCopy.TaskID, resSummary)
				},
			})
		}
	}

	// Blackboard TOPICS-STATE updates.
	topics := append([]TopicSnapshot(nil), input.Topics...)
	sort.Slice(topics, func(i, j int) bool {
		if topics[i].UpdatedAt.Equal(topics[j].UpdatedAt) {
			return topics[i].Subject < topics[j].Subject
		}
		return topics[i].UpdatedAt.Before(topics[j].UpdatedAt)
	})
	topicSeq := make(map[string]int, len(topics))
	for _, top := range topics {
		topCopy := top
		if topCopy.Class != topicClassState && topCopy.Stream != streamTopicsState {
			continue
		}
		writer := sanitizeMemberName(topCopy.WriterProfile)
		if _, ok := members[writer]; !ok || writer == "" {
			if m, ok := workerIDToSessionMember[topCopy.WriterSession]; ok {
				writer = m
			} else {
				writer = swarmMemberPlatform
			}
		}
		tsKey := topCopy.UpdatedAt.UTC().Format(time.RFC3339)
		summary := truncateSwarmText(firstLine(firstNonEmptyStr(topCopy.Summary, topCopy.Subject)), maxBoardEventTextChars)
		addCandidate(candidateBoardEvent{
			key:      fmt.Sprintf("topic:%s:%s", topCopy.Subject, tsKey),
			ts:       topCopy.UpdatedAt,
			priority: 6,
			from:     writer,
			topicSub: topCopy.Subject,
			render: func(_ int) string {
				return fmt.Sprintf("@%s published topic `%s` (`%s`): %s", swarmMemberCoordinator, topCopy.Topic, topCopy.Stream, summary)
			},
		})
	}

	// Session termination events.
	for _, sess := range sessions {
		sessCopy := sess
		if sessCopy.terminated && !sessCopy.sessionMode {
			addCandidate(candidateBoardEvent{
				key:      "session-stop:" + sessCopy.memberName,
				ts:       now,
				priority: 8,
				from:     swarmMemberCoordinator,
				citeKey:  "session-start:" + sessCopy.memberName,
				render: func(_ int) string {
					return fmt.Sprintf("@%s stopped `/session` worker pod (`/session off`); routing returned to @%s.", sessCopy.memberName, swarmMemberPlatform)
				},
			})
		}
	}

	// Causal clamping: ensure every dependent event sorts after the event it cites.
	byKey := make(map[string]*candidateBoardEvent, len(candidates))
	for i := range candidates {
		byKey[candidates[i].key] = &candidates[i]
	}
	for pass := 0; pass < 6; pass++ {
		changed := false
		for i := range candidates {
			c := &candidates[i]
			if c.citeKey == "" {
				continue
			}
			parent, ok := byKey[c.citeKey]
			if !ok {
				continue
			}
			if c.ts.Before(parent.ts) {
				c.ts = parent.ts
				changed = true
			}
			if c.ts.Equal(parent.ts) && c.priority <= parent.priority {
				c.priority = parent.priority + 1
				changed = true
			}
		}
		if !changed {
			break
		}
	}

	sort.Slice(candidates, func(i, j int) bool {
		a, b := candidates[i], candidates[j]
		if a.pinnedFirst != b.pinnedFirst {
			return a.pinnedFirst
		}
		if !a.ts.Equal(b.ts) {
			return a.ts.Before(b.ts)
		}
		if a.priority != b.priority {
			return a.priority < b.priority
		}
		return a.orderIdx < b.orderIdx
	})

	for _, c := range candidates {
		citeSeq := 0
		if c.citeKey != "" {
			citeSeq = ledger.EventSeqs[c.citeKey]
		}
		seq := emit(c.key, c.ts, c.from, c.render(citeSeq))
		if c.taskID != "" {
			if strings.HasPrefix(c.key, "task-in:") {
				taskInSeq[c.taskID] = seq
			}
			if seq > taskLastSeq[c.taskID] {
				taskLastSeq[c.taskID] = seq
			}
		}
		if c.topicSub != "" {
			topicSeq[c.topicSub] = seq
		}
	}

	head := ledger.NextSeq - 1
	if head < 1 {
		head = 1
	}
	for name, m := range members {
		m.WaitSince = head
		m.LastReadSeq = head
		m.LastNudgedSeq = head
		m.LastParkBlockedSeq = head
		members[name] = m
	}

	// 6. Build validated view.json and view.html.
	view := buildSwarmOverview(input, depName, members, sessions, tasks, taskInSeq, taskLastSeq, topics, topicSeq, head, now)
	viewHTML := buildSwarmOverviewHTML()

	return SwarmSnapshot{
		Title:       title,
		Members:     members,
		NewMessages: newMessages,
		View:        view,
		ViewHTML:    viewHTML,
	}, ledger
}

// isSessionWorkerTask reports whether t is a console conversation's own turn executed by a
// /session worker pod. The classification is topological rather than name-based: the gateway
// routes a console conversation's top-level turns either to the platform agent or to the
// session worker bound to that conversation, while every specialist task reaches the bus as a
// delegation (Authority.Via / DelegatedFrom) or as a Kanban card (CardParentTaskID). A
// top-level console task whose addressee is not the platform agent is therefore a session
// worker's, whatever the deployment named its session profiles or specialists.
func isSessionWorkerTask(t *TaskRecord) bool {
	if t == nil {
		return false
	}
	if !strings.HasPrefix(strings.TrimSpace(t.Conversation), consoleConvPrefix) {
		return false
	}
	if t.DelegatedFromTaskID != "" || t.CardParentTaskID != "" || strings.HasPrefix(t.TaskID, cardTaskIDPrefix) {
		return false
	}
	addr := strings.TrimSpace(t.Addressee)
	return addr != "" && addr != swarmMemberPlatform && addr != swarmMemberGateway
}

func projectSessionWorkers(
	input SwarmProjectionInput,
	tasks []*TaskRecord,
	now time.Time,
) ([]sessionWorkerProjection, map[string]string, map[string]string, map[string]bool, map[string]bool) {
	tokenToWorkerID := make(map[string]string)
	tokenAllWorkers := make(map[string][]string)
	workerLatestTask := make(map[string]*TaskRecord)
	tokenLatestTask := make(map[string]*TaskRecord)
	skippedTokens := make(map[string]bool)
	skippedWorkers := make(map[string]bool)

	rootID := strings.TrimSpace(input.RootConversationID)
	hasOwnCoordinatorEntry := false
	knownTokens := make(map[string]bool, len(input.Conversations)*2)
	otherRootIDs := make(map[string]bool)
	for alias, conv := range input.Conversations {
		if alias != "" {
			knownTokens[alias] = true
		}
		if conv.Token != "" {
			knownTokens[conv.Token] = true
		}
		cid := strings.TrimSpace(conv.AGYConversationID)
		if rootID != "" && cid == rootID {
			hasOwnCoordinatorEntry = true
		} else if cid != "" && !isSessionSubagentEntry(conv) {
			otherRootIDs[cid] = true
		}
	}

	hasPodInventory := !input.Deployment.SessionPodsProbedAt.IsZero()
	livePodByAddr := make(map[string]SessionPodInfo, len(input.Deployment.ActiveSessionPods))
	livePodByToken := make(map[string]SessionPodInfo, len(input.Deployment.ActiveSessionPods))
	for _, pod := range input.Deployment.ActiveSessionPods {
		addr := firstNonEmptyStr(strings.TrimSpace(pod.Addressee), strings.TrimSpace(pod.Name))
		if addr == "" {
			continue
		}
		token := strings.TrimPrefix(strings.TrimSpace(pod.SessionKey), consoleConvPrefix)
		if hasOwnCoordinatorEntry && token != "" && !knownTokens[token] {
			skippedTokens[token] = true
			skippedWorkers[addr] = true
			continue
		}
		livePodByAddr[addr] = pod
		if token != "" {
			livePodByToken[token] = pod
			tokenToWorkerID[token] = addr
			tokenAllWorkers[token] = appendUniqueStr(tokenAllWorkers[token], addr)
		}
	}

	for _, t := range tasks {
		token := strings.TrimPrefix(strings.TrimSpace(t.Conversation), consoleConvPrefix)
		if hasOwnCoordinatorEntry && token != "" && !knownTokens[token] {
			skippedTokens[token] = true
			if isSessionWorkerTask(t) {
				skippedWorkers[t.Addressee] = true
			}
			continue
		}
		if token != "" {
			tokenLatestTask[token] = t
		}
		if isSessionWorkerTask(t) {
			workerLatestTask[t.Addressee] = t
			if token != "" {
				tokenAllWorkers[token] = appendUniqueStr(tokenAllWorkers[token], t.Addressee)
				if _, hasLive := livePodByToken[token]; !hasLive {
					tokenToWorkerID[token] = t.Addressee
				}
			}
		}
	}

	var projections []sessionWorkerProjection
	usedNames := map[string]bool{
		swarmMemberCoordinator: true,
		swarmMemberNarrator:    true,
		swarmMemberPlatform:    true,
	}
	tokenToMember := make(map[string]string)
	workerIDToMember := make(map[string]string)
	coveredSubagents := make(map[string]bool)

	markConvSkipped := func(conv ConversationEntry, workerID string) {
		if conv.Token != "" {
			skippedTokens[conv.Token] = true
			for _, wid := range tokenAllWorkers[conv.Token] {
				skippedWorkers[wid] = true
			}
		}
		if workerID != "" {
			skippedWorkers[workerID] = true
		}
		if conv.SessionWorkerID != "" {
			skippedWorkers[conv.SessionWorkerID] = true
		}
	}

	aliases := make([]string, 0, len(input.Conversations))
	for alias := range input.Conversations {
		aliases = append(aliases, alias)
	}
	sort.Strings(aliases)

	for _, alias := range aliases {
		conv := input.Conversations[alias]
		workerID := firstNonEmptyStr(tokenToWorkerID[conv.Token], conv.SessionWorkerID)
		livePod, hasLivePod := livePodByAddr[workerID]
		if !hasLivePod && conv.Token != "" {
			if lp, ok := livePodByToken[conv.Token]; ok {
				livePod = lp
				hasLivePod = true
				if lpAddr := firstNonEmptyStr(lp.Addressee, lp.Name); lpAddr != "" {
					workerID = lpAddr
				}
			}
		}

		cid := strings.TrimSpace(conv.AGYConversationID)
		pid := strings.TrimSpace(conv.ParentAGYConversationID)
		subLin, hasSubLin := input.Subagents[cid]
		isSubagentConv := hasSubLin || isSessionSubagentEntry(conv)
		belongsToRootSubagent := hasSubLin || (rootID != "" && pid == rootID)
		belongsToRoot := belongsToRootSubagent ||
			(!hasOwnCoordinatorEntry && cid == "") ||
			(rootID != "" && cid == rootID)
		// A conversation is a dedicated /session worker when its Antigravity subagent was invoked as
		// the kube-session type, or when it demonstrably owns (or owned) a session worker: a
		// live pod keyed to its token, a session-worker task on its token, or recorded session
		// state. Role and alias wording chosen by the model is never consulted.
		isDedicatedSessionSubagent := (hasSubLin && subLin.IsSessionSubagent()) ||
			hasLivePod || workerID != "" || conv.SessionMode || conv.SessionTerminated

		recentTaskOnBus := false
		for _, lt := range []*TaskRecord{workerLatestTask[workerID], tokenLatestTask[conv.Token]} {
			if lt == nil {
				continue
			}
			ts := lt.UpdatedAt
			if ts.IsZero() {
				ts = lt.CreatedAt
			}
			if !ts.IsZero() && now.Sub(ts) <= 2*time.Hour {
				recentTaskOnBus = true
				break
			}
		}

		// Never leak conversations owned by another root Antigravity conversation (e.g. "agy-mcp" soak
		// sessions) onto this coordinator's Swarm board. An unadopted entry may only be pre-created
		// if it is a genuine subagent whose parent conversation is no longer alive and has a live pod.
		canAdoptOrphanSubagent := isSubagentConv &&
			!otherRootIDs[cid] &&
			!otherRootIDs[pid] &&
			hasLivePod &&
			!conv.SessionTerminated
		if !belongsToRoot && !canAdoptOrphanSubagent {
			markConvSkipped(conv, workerID)
			continue
		}
		if !conv.SessionMode && !conv.SessionTerminated && workerID == "" && !isDedicatedSessionSubagent && !hasLivePod {
			continue
		}
		if cid != "" {
			coveredSubagents[cid] = true
		}

		working := false
		activeStep := ""
		for _, t := range tasks {
			if t.Terminal {
				continue
			}
			sameToken := conv.Token != "" && t.Conversation == consoleConvPrefix+conv.Token
			sameWorker := workerID != "" && t.Addressee == workerID
			if sameToken || sameWorker {
				working = true
				activeStep = firstNonEmptyStr(latestActiveStepTitle(t), truncateSwarmText(firstLine(t.PromptText), 60))
			}
		}

		staleNonDedicated := hasSubLin && !isDedicatedSessionSubagent && !hasLivePod && !working && !conv.LastUsedAt.IsZero() && now.Sub(conv.LastUsedAt) > 30*time.Minute
		podGoneOnCluster := hasPodInventory && !belongsToRootSubagent && !conv.SessionMode && !hasLivePod && !working && !recentTaskOnBus
		isKilled := (conv.SessionTerminated && !conv.SessionMode && !hasLivePod) || (hasSubLin && subLin.IsKilled() && !hasLivePod) || staleNonDedicated || podGoneOnCluster
		isActiveSession := (hasLivePod || conv.SessionMode || isDedicatedSessionSubagent) && !isKilled
		expiredHistorical := !isActiveSession && !isDedicatedSessionSubagent && !working && !conv.LastUsedAt.IsZero() && now.Sub(conv.LastUsedAt) > time.Hour
		if isKilled || (hasSubLin && !isDedicatedSessionSubagent && !isActiveSession && !working) || expiredHistorical {
			markConvSkipped(conv, workerID)
			continue
		}

		baseName := deriveSessionMemberName(conv, subLin)
		memberName := uniqueMemberName(baseName, usedNames)

		desc := conv.Description
		if subLin.SubagentRole != "" {
			desc = subLin.SubagentRole
		} else if conv.SubagentRole != "" {
			desc = conv.SubagentRole
		}

		joinedAt := conv.CreatedAt
		if !livePod.CreatedAt.IsZero() && (joinedAt.IsZero() || livePod.CreatedAt.Before(joinedAt)) {
			joinedAt = livePod.CreatedAt
		}
		if joinedAt.IsZero() {
			joinedAt = now
		}

		convID := cid
		if convID == rootID || !isSubagentConv || otherRootIDs[convID] {
			convID = ""
		}

		proj := sessionWorkerProjection{
			memberName:     memberName,
			alias:          conv.Alias,
			token:          conv.Token,
			workerID:       workerID,
			conversationID: convID,
			description:    desc,
			sessionMode:    isActiveSession,
			terminated:     isKilled,
			working:        working,
			activeStep:     activeStep,
			joinedAt:       joinedAt,
		}
		projections = append(projections, proj)
		if conv.Token != "" {
			tokenToMember[conv.Token] = memberName
			for _, wid := range tokenAllWorkers[conv.Token] {
				workerIDToMember[wid] = memberName
			}
		}
		if workerID != "" {
			workerIDToMember[workerID] = memberName
		}
		if conv.SessionWorkerID != "" {
			workerIDToMember[conv.SessionWorkerID] = memberName
		}
	}

	// Include any kube-session subagents spawned under RootConversationID that haven't made
	// their first MCP tool call yet, so they appear on the Swarm roster immediately on spawn.
	subIDs := make([]string, 0, len(input.Subagents))
	for id := range input.Subagents {
		subIDs = append(subIDs, id)
	}
	sort.Strings(subIDs)
	for _, subID := range subIDs {
		if coveredSubagents[subID] {
			continue
		}
		lin := input.Subagents[subID]
		if !lin.IsSessionSubagent() || lin.IsKilled() {
			continue
		}
		baseName := deriveSessionMemberName(ConversationEntry{Alias: lin.SubagentRole}, lin)
		memberName := uniqueMemberName(baseName, usedNames)
		projections = append(projections, sessionWorkerProjection{
			memberName:     memberName,
			conversationID: subID,
			description:    firstNonEmptyStr(lin.SubagentRole, "Kube-Agents /session worker"),
			sessionMode:    true,
			joinedAt:       now,
		})
	}

	// Include any unbound remote session pods observed in Kubernetes or on TASKS. DIRECTORY
	// cards are published by standing agents (platform, specialists), never by session pods,
	// so they are not a source of workers.
	remoteWorkerIDs := make(map[string]bool)
	for addr := range livePodByAddr {
		remoteWorkerIDs[addr] = true
	}
	for wid := range workerLatestTask {
		remoteWorkerIDs[wid] = true
	}
	sortedRemote := make([]string, 0, len(remoteWorkerIDs))
	for wid := range remoteWorkerIDs {
		if _, already := workerIDToMember[wid]; !already {
			sortedRemote = append(sortedRemote, wid)
		}
	}
	sort.Strings(sortedRemote)
	for _, wid := range sortedRemote {
		if skippedWorkers[wid] {
			continue
		}
		livePod, hasLivePod := livePodByAddr[wid]
		if hasPodInventory && !hasLivePod {
			skippedWorkers[wid] = true
			continue
		}
		lastTask := workerLatestTask[wid]
		_, hasLiveCard := input.AgentCards[wid]
		working := lastTask != nil && !lastTask.Terminal
		recentTask := lastTask != nil && ((!lastTask.UpdatedAt.IsZero() && now.Sub(lastTask.UpdatedAt) <= time.Hour) || (!lastTask.CreatedAt.IsZero() && now.Sub(lastTask.CreatedAt) <= time.Hour))
		if !hasLivePod && !hasLiveCard && !working && !recentTask {
			skippedWorkers[wid] = true
			continue
		}
		isActive := hasLivePod || hasLiveCard || working
		memberName := uniqueMemberName(sanitizeMemberName(wid), usedNames)
		activeStep := ""
		joinedAt := now
		if !livePod.CreatedAt.IsZero() {
			joinedAt = livePod.CreatedAt
		} else if lastTask != nil && !lastTask.CreatedAt.IsZero() {
			joinedAt = lastTask.CreatedAt
		}
		if lastTask != nil && working {
			activeStep = firstNonEmptyStr(latestActiveStepTitle(lastTask), truncateSwarmText(firstLine(lastTask.PromptText), 60))
		}
		token := strings.TrimPrefix(strings.TrimSpace(livePod.SessionKey), consoleConvPrefix)
		projections = append(projections, sessionWorkerProjection{
			memberName:  memberName,
			token:       token,
			workerID:    wid,
			description: "Remote /session worker pod",
			sessionMode: isActive,
			terminated:  !isActive,
			working:     working,
			activeStep:  activeStep,
			joinedAt:    joinedAt,
		})
		workerIDToMember[wid] = memberName
		if token != "" {
			tokenToMember[token] = memberName
		}
	}

	return projections, tokenToMember, workerIDToMember, skippedTokens, skippedWorkers
}

func deriveSessionMemberName(conv ConversationEntry, lin ConversationLineage) string {
	for _, candidate := range []string{conv.Alias, lin.SubagentRole, conv.SubagentRole} {
		slug := sanitizeAliasSlug(candidate)
		if slug == "" || slug == defaultConvAlias {
			continue
		}
		if strings.HasPrefix(slug, "session-") || strings.HasPrefix(slug, "s-") {
			return sanitizeMemberName(slug)
		}
		return sanitizeMemberName("session-" + slug)
	}
	if conv.SessionWorkerID != "" {
		return sanitizeMemberName(conv.SessionWorkerID)
	}
	if conv.AGYConversationID != "" {
		short := conv.AGYConversationID
		if len(short) > 8 {
			short = short[:8]
		}
		return sanitizeMemberName("session-" + short)
	}
	return "session-worker"
}

// peerRelayPrefixRe matches the Session Playbook's relay prefix, `From @<member>: <text>`,
// which a kube-session subagent puts in front of a peer session's message when it forwards
// it to its own pod. The member name is what the roster uses (sanitizeMemberName output).
var peerRelayPrefixRe = regexp.MustCompile(`(?i)^\s*from\s+@([a-z0-9][a-z0-9_-]*)\s*:\s*`)

// parsePeerRelayPrefix returns the member named by a leading `From @<member>:` relay prefix
// and the text after it, or ("", "") when the prompt carries no such prefix.
func parsePeerRelayPrefix(text string) (peer, rest string) {
	m := peerRelayPrefixRe.FindStringSubmatchIndex(text)
	if m == nil {
		return "", ""
	}
	return text[m[2]:m[3]], text[m[1]:]
}

func uniqueMemberName(base string, used map[string]bool) string {
	clean := sanitizeMemberName(base)
	if clean == "" {
		clean = "session-worker"
	}
	if !used[clean] {
		used[clean] = true
		return clean
	}
	for i := 2; i < 100; i++ {
		cand := fmt.Sprintf("%s-%d", clean, i)
		if !used[cand] {
			used[cand] = true
			return cand
		}
	}
	return clean
}

func sanitizeMemberName(raw string) string {
	lower := strings.ToLower(strings.TrimSpace(raw))
	if lower == "" {
		return ""
	}
	var b strings.Builder
	lastDash := false
	for _, r := range lower {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '_' {
			b.WriteRune(r)
			lastDash = false
		} else if !lastDash && b.Len() > 0 {
			b.WriteByte('-')
			lastDash = true
		}
	}
	return strings.Trim(b.String(), "-")
}

func sanitizeItemID(raw string) string {
	lower := strings.ToLower(strings.TrimSpace(raw))
	var b strings.Builder
	lastDash := false
	for _, r := range lower {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
			lastDash = false
		} else if !lastDash && b.Len() > 0 {
			b.WriteByte('-')
			lastDash = true
		}
		if b.Len() >= 36 {
			break
		}
	}
	res := strings.Trim(b.String(), "-")
	if res == "" {
		return "item"
	}
	return res
}

func latestActiveStepTitle(t *TaskRecord) string {
	if t == nil {
		return ""
	}
	for i := len(t.Steps) - 1; i >= 0; i-- {
		st := t.Steps[i]
		if strings.TrimSpace(st.Title) != "" {
			return truncateSwarmText(strings.TrimSpace(st.Title), 60)
		}
	}
	for i := len(t.Activities) - 1; i >= 0; i-- {
		act := t.Activities[i]
		if strings.TrimSpace(act.Title) != "" {
			return truncateSwarmText(strings.TrimSpace(act.Title), 60)
		}
		if strings.TrimSpace(act.Tool) != "" {
			return truncateSwarmText(strings.TrimSpace(act.Tool), 60)
		}
	}
	return ""
}

func extractTaskResultSummary(t *TaskRecord) string {
	if t == nil {
		return ""
	}
	if t.ResultChat != nil && strings.TrimSpace(t.ResultChat.Headline) != "" {
		return truncateSwarmText(strings.TrimSpace(t.ResultChat.Headline), maxBoardEventTextChars)
	}
	for _, s := range []string{t.ResultText, t.StatusText, t.ProgressText, t.PromptText} {
		line := firstLine(s)
		if line != "" {
			return truncateSwarmText(line, maxBoardEventTextChars)
		}
	}
	return string(t.State)
}

func firstLine(s string) string {
	for _, line := range strings.Split(strings.TrimSpace(s), "\n") {
		trimmed := strings.TrimSpace(strings.TrimLeft(line, "#*- >"))
		if trimmed != "" {
			return trimmed
		}
	}
	return ""
}

func truncateSwarmText(s string, maxChars int) string {
	s = strings.TrimSpace(strings.ReplaceAll(s, "\n", " "))
	runes := []rune(s)
	if len(runes) <= maxChars || maxChars <= 3 {
		return s
	}
	return string(runes[:maxChars-1]) + "…"
}

func firstPositiveInt(vals ...int) int {
	for _, v := range vals {
		if v > 0 {
			return v
		}
	}
	return 0
}

func firstNonEmptyAnyStr(vals ...any) string {
	for _, v := range vals {
		if s, ok := v.(string); ok && strings.TrimSpace(s) != "" {
			return strings.TrimSpace(s)
		}
	}
	return ""
}

func appendUniqueStr(slice []string, val string) []string {
	val = strings.TrimSpace(val)
	if val == "" {
		return slice
	}
	for _, existing := range slice {
		if existing == val {
			return slice
		}
	}
	return append(slice, val)
}

func boardHasOrphanedManagedAuthor(lines []string, prevLedger BoardLedger, members map[string]SwarmMember) bool {
	for eventKey := range prevLedger.EventSeqs {
		for _, prefix := range []string{"session-start:", "session-ready:", "session-stop:"} {
			if strings.HasPrefix(eventKey, prefix) {
				memberName := strings.TrimSpace(strings.TrimPrefix(eventKey, prefix))
				if memberName != "" {
					if _, ok := members[memberName]; !ok {
						return true
					}
				}
			}
		}
	}
	if len(prevLedger.Authors) == 0 {
		return false
	}
	for _, line := range lines {
		var hdr struct {
			Agent string `json:"agent"`
		}
		if json.Unmarshal([]byte(line), &hdr) != nil {
			continue
		}
		ag := strings.TrimSpace(hdr.Agent)
		if ag == "" || !prevLedger.Authors[ag] {
			continue
		}
		if _, ok := members[ag]; !ok {
			return true
		}
	}
	return false
}
