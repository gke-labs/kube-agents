package mcp

// The swarm projection: a pure function from what the gateway reads off the
// bus (console conversations, their tasks, the agent cards) and the board
// ledger the workstation bridge kept from the last call, to the snapshot the
// bridge writes into Antigravity's Swarm UI directory.
//
// Ported from Adam Parco's BuildSwarmState and buildSwarmOverview
// (adam/agy-mcp-new: a2a/mcp/swarm_bridge.go, a2a/mcp/swarm_overview.go). The
// wire types are copied from the bridge that reads them (#3689:
// a2a/mcpbridge/swarm_sink.go) and must stay byte-compatible with it.
//
// Everything that reaches this file from the bus or from a client is
// untrusted: conversation aliases and descriptions, task ids, addressees and
// text, card names, and the ledger itself, which the client sends back. It is
// sanitised on the way in and every list it can grow is capped, on the way in
// and on the way out.

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	swarmDefaultTitle      = "kube-agents swarm"
	swarmLedgerSchema      = 8
	swarmManagedByTag      = "kube-agents"
	swarmRoleCoordinator   = "coordinator"
	swarmRoleNarrator      = "narrator"
	swarmRoleWorker        = "worker"
	swarmWakeMentions      = "mentions"
	swarmWakeWorking       = "working"
	swarmMemberCoordinator = "coordinator"
	swarmMemberNarrator    = "narrator"
	swarmMemberPlatform    = "platform"
	swarmMemberGateway     = "gateway"
	swarmConsolePrefix     = "console:"
	swarmEllipsis          = "…"

	// Adam's text and view caps, unchanged.
	maxBoardEventTextChars  = 240
	maxViewHeadlineChars    = 280
	maxViewTitleChars       = 150
	maxViewWhyChars         = 360
	maxViewTextItemChars    = 280
	maxWorkstreamsInView    = 24
	maxAttentionItemsInView = 16
	maxMilestonesInView     = 16

	// Caps this port adds. The input caps bound the work; the output caps
	// bound what one call can hand the bridge; the ledger caps bound what a
	// client can make the gateway hold, since the ledger comes back in.
	maxSwarmTitleChars       = 120
	maxSwarmDescriptionChars = 100
	maxSwarmStepChars        = 60
	maxSwarmInputConvs       = 512
	maxSwarmInputTasks       = 512
	maxSwarmMembers          = 48 // coordinator, narrator and platform included
	maxNewBoardMessages      = 100
	maxLedgerEvents          = 2048
	maxLedgerAuthors         = 128
	maxLedgerKeyBytes        = 200
	maxBoardSeq              = 1 << 30
	maxSwarmAliasBytes       = 256
	maxSwarmHandleBytes      = 256
	maxSwarmTaskIDBytes      = 128
	maxMemberNameBytes       = 48
	maxLineageDepth          = 8
	maxWorkstreamOwners      = 16
	maxActivityEntryBytes    = 64 << 10
	maxItemIDBytes           = 36
	// swarmScanBytes is how much of one untrusted string is read at all,
	// before it is reduced to a line and capped in runes.
	swarmScanBytes = 4096
	// swarmRecent is how long a conversation stays on the roster after its
	// last use or task, when nothing of it is running (Adam's
	// expiredHistorical window).
	swarmRecent = time.Hour
)

// SwarmConversation is one console conversation the client holds. Every
// field is client-supplied. Parent is the parent conversation's Alias, a name
// only: one not in the input, or one that forms a cycle, is ignored.
// AGYConversationID is the client's own id for the subagent conversation
// behind it, echoed onto the session member's conversation_id so the client
// can link the two; one that is not a safe id (see safeConversationID) is
// dropped.
type SwarmConversation struct {
	Alias             string
	Token             string
	Description       string
	Parent            string
	AGYConversationID string
	LastUsed          time.Time
}

// SwarmTask is one task on TASKS, as the gateway found it.
type SwarmTask struct {
	Addressee, TaskID       string    // where it was submitted
	Conversation            string    // "console:<token>" from the submission's authority audience
	ViaAddressee, ViaTaskID string    // set when delegated (authority.via)
	Submitted               time.Time // when it was submitted
	Prompt                  string    // the submission's text; untrusted
	Task                    *lib.Task // lib.FoldTask result, may be non-final or nil
	// Question is the message of the task's latest input-required
	// status-update: the question it is asking (spec-a2a-payloads.md). The
	// fold keeps only the final status message (lib.Task.FinalMessage), so
	// the caller reads this off the event. Untrusted; nil when it has none.
	Question *lib.Message
	// QuestionSeq is the TASKS stream sequence of that input-required
	// status-update, 0 when the caller does not have it. It keys the
	// input-required post, so each new ask posts once.
	QuestionSeq uint64
}

// SwarmInput is everything BuildSwarmState reads. Now is the only clock.
type SwarmInput struct {
	Title         string
	Conversations []SwarmConversation
	Tasks         []SwarmTask
	Cards         map[string]AgentCard
	Now           time.Time
}

// The wire types below are copied from the workstation bridge
// (#3689: a2a/mcpbridge/swarm_sink.go). Field names and json tags must stay
// byte-compatible with it. SwarmView omits the bridge's KubeAgents field
// (omitempty, deployment metadata this projection is not given).

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

// SwarmBoardMessage represents a single line in .system_generated/swarm/board.jsonl.
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
	SchemaVersion int             `json:"schemaVersion,omitempty"`
	NextSeq       int             `json:"nextSeq"`
	EventSeqs     map[string]int  `json:"eventSeqs"`
	Authors       map[string]bool `json:"authors,omitempty"`
}

// SwarmSnapshot is the complete projected state. ViewHTML is never set here:
// the bridge renders the view.
type SwarmSnapshot struct {
	Title       string                 `json:"title"`
	Members     map[string]SwarmMember `json:"members"`
	NewMessages []SwarmBoardMessage    `json:"newMessages,omitempty"`
	View        SwarmView              `json:"view"`
	ViewHTML    string                 `json:"viewHtml,omitempty"`
}

// swarmConv is one conversation after validation, with what the projection
// derived for it.
type swarmConv struct {
	alias, token, desc string // raw alias and token (matching), sanitised desc
	aliasLabel         string // sanitised alias for display
	parent             string // raw alias of a valid parent, or ""
	root               string // raw alias of the lineage root (itself when parent is "")
	lastUsed           time.Time
	lastActivity       time.Time
	firstTask          time.Time
	working            bool
	step               string
	member             string // roster name, "" when not on the roster
	agyConversationID  string // validated client conversation id, or ""
}

// swarmTaskView is one task after validation.
type swarmTaskView struct {
	id, addr, addrLabel string // id is validated; addr raw; addrLabel sanitised
	token               string // console token, "" when not a console task
	viaTaskID           string
	viaAddr             string // the delegating parent's addressee, raw; "" unless viaTaskID is set
	submitted           time.Time
	state               lib.TaskState
	terminal            bool
	result, reason      string // sanitised first lines
	progress, step      string
	prompt              string // the submission's text, sanitised first line
	question            string // the input-required question, sanitised first line
	questionSeq         uint64 // stream sequence of the input-required status-update, or 0
	asks                int    // input-required entries in the status history
}

// activeStep is what a running task is doing: the question it is waiting on,
// when it asked one, else its newest tool call, its progress line, or its
// prompt (Adam's latestActiveStepTitle fallbacks).
func (t *swarmTaskView) activeStep() string {
	return swarmText(firstNonEmptyString(t.question, t.step, t.progress, t.prompt), maxSwarmStepChars)
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
	render      func(citeSeq int) string
}

// BuildSwarmState is a pure, deterministic projection from in and the
// previous BoardLedger to a SwarmSnapshot and the updated BoardLedger. It
// reads no clock (in.Now is the only time), and its output does not depend
// on the order of in's slices or on map iteration order.
func BuildSwarmState(in SwarmInput, prev BoardLedger) (SwarmSnapshot, BoardLedger) {
	now := in.Now.UTC()
	ledger := sanitizeLedger(prev)

	title := swarmText(in.Title, maxSwarmTitleChars)
	if title == "" {
		title = swarmDefaultTitle
	}

	tasks := normalizeTasks(in.Tasks, now)
	liveTokens := make(map[string]bool)
	for _, t := range tasks {
		if t.token != "" && !t.terminal {
			liveTokens[t.token] = true
		}
	}
	convs := normalizeConversations(in.Conversations, liveTokens, now)
	resolveLineage(convs)
	byToken := make(map[string]*swarmConv, len(convs))
	byAlias := make(map[string]*swarmConv, len(convs))
	for _, c := range convs {
		byAlias[c.alias] = c
		if c.token != "" {
			if _, dup := byToken[c.token]; !dup {
				byToken[c.token] = c
			}
		}
	}

	// Working state and recency per conversation.
	for _, t := range tasks {
		c := byToken[t.token]
		if c == nil {
			continue
		}
		if c.firstTask.IsZero() {
			c.firstTask = t.submitted
		}
		if t.submitted.After(c.lastActivity) {
			c.lastActivity = t.submitted
		}
		if !t.terminal {
			c.working = true
			if step := t.activeStep(); step != "" {
				c.step = step
			}
		}
	}

	// Roster: coordinator, narrator, platform, then the conversations that are
	// running something or were used within swarmRecent, most active first.
	nowISO := now.Format(time.RFC3339)
	members := map[string]SwarmMember{
		swarmMemberCoordinator: {
			Name: swarmMemberCoordinator, Role: swarmRoleCoordinator,
			Model:    "kube-agents · " + title,
			WakeMode: swarmWakeMentions, JoinedAt: nowISO, ManagedBy: swarmManagedByTag,
		},
		swarmMemberNarrator: {
			Name: swarmMemberNarrator, Role: swarmRoleNarrator,
			Model:    "a2a-jetstream/0.4 · tasks",
			WakeMode: swarmWakeMentions, JoinedAt: nowISO, ManagedBy: swarmManagedByTag,
		},
	}
	var active []*swarmConv
	for _, c := range convs {
		last := c.lastActivity
		if c.lastUsed.After(last) {
			last = c.lastUsed
		}
		c.lastActivity = last
		if c.working || (!last.IsZero() && now.Sub(last) <= swarmRecent) {
			active = append(active, c)
		}
	}
	sort.SliceStable(active, func(i, j int) bool {
		a, b := active[i], active[j]
		if a.working != b.working {
			return a.working
		}
		if !a.lastActivity.Equal(b.lastActivity) {
			return a.lastActivity.After(b.lastActivity)
		}
		return a.alias < b.alias
	})
	if room := maxSwarmMembers - 3; len(active) > room {
		active = active[:room]
	}
	// Names are assigned in alias order so the selection order above never
	// decides which of two colliding aliases gets the suffix.
	sort.Slice(active, func(i, j int) bool { return active[i].alias < active[j].alias })
	usedNames := map[string]bool{swarmMemberCoordinator: true, swarmMemberNarrator: true, swarmMemberPlatform: true}
	var rostered []*swarmConv
	for _, c := range active {
		parentSlug := ""
		if p := byAlias[c.parent]; p != nil {
			parentSlug = sanitizeAliasSlug(p.alias)
		}
		name := uniqueMemberName(deriveSessionMemberName(c.alias, parentSlug, c.token), usedNames)
		if name == "" {
			continue
		}
		c.member = name
		rostered = append(rostered, c)
		model := "console"
		if c.token != "" {
			model = swarmConsolePrefix + swarmText(c.token, 14)
		}
		wake := swarmWakeMentions
		if c.working {
			wake = swarmWakeWorking
			if c.step != "" {
				model += " · ⚡ " + c.step
			}
		}
		joined := c.firstTask
		if joined.IsZero() || (!c.lastUsed.IsZero() && c.lastUsed.Before(joined)) {
			joined = c.lastUsed
		}
		if joined.IsZero() {
			joined = now
		}
		members[name] = SwarmMember{
			Name: name, Role: swarmRoleWorker, Model: model,
			WakeMode: wake, JoinedAt: joined.UTC().Format(time.RFC3339), ManagedBy: swarmManagedByTag,
			ConversationID: c.agyConversationID,
		}
	}

	// Adam's skippedTokens: a console task whose conversation is not on the
	// roster (expired by the activity rule, unknown to this client, or past
	// the member cap) is not projected, nor is anything delegated from a
	// skipped task. Tasks off the console (no token) are kept, as Adam kept
	// them.
	// The delegation half runs to a fixed point over the whole set, so a
	// child that sorts before its parent is skipped all the same. Each pass
	// that changes anything skips at least one more task, so it ends within
	// len(tasks) passes (and tasks is capped at maxSwarmInputTasks).
	skipped := make(map[string]bool)
	for _, t := range tasks {
		if c := byToken[t.token]; t.token != "" && (c == nil || c.member == "") {
			skipped[t.id] = true
		}
	}
	for changed := true; changed; {
		changed = false
		for _, t := range tasks {
			if !skipped[t.id] && t.viaTaskID != "" && skipped[t.viaTaskID] {
				skipped[t.id] = true
				changed = true
			}
		}
	}
	kept := tasks[:0:0]
	for _, t := range tasks {
		if !skipped[t.id] {
			kept = append(kept, t)
		}
	}
	tasks = kept

	platformWorking, platformStep := false, ""
	for _, t := range tasks {
		if !t.terminal && (t.addr == "" || t.addr == swarmMemberPlatform) {
			platformWorking = true
			if step := t.activeStep(); step != "" {
				platformStep = step
			}
		}
	}
	platformModel := "hermes · " + firstNonEmptyString(swarmText(in.Cards[swarmMemberPlatform].Name, maxSwarmDescriptionChars), swarmMemberPlatform)
	platformWake := swarmWakeMentions
	if platformWorking {
		platformWake = swarmWakeWorking
		if platformStep != "" {
			platformModel += " · ⚡ " + platformStep
		}
	}
	members[swarmMemberPlatform] = SwarmMember{
		Name: swarmMemberPlatform, Role: swarmRoleWorker, Model: platformModel,
		WakeMode: platformWake, JoinedAt: nowISO, ManagedBy: swarmManagedByTag,
	}

	// Which roster member owns a task (Adam's resolveTaskMember, on the
	// topology this port has): a console conversation's own top-level turn
	// to anything but the platform or gateway is its session worker's, so
	// the conversation's; an addressee that names a worker on the roster is
	// that worker's; everything else is the platform's.
	resolveTaskMember := func(t *swarmTaskView) string {
		if t == nil {
			return swarmMemberPlatform
		}
		if c := byToken[t.token]; c != nil && c.member != "" && isSessionWorkerTask(t) {
			return c.member
		}
		if cand := sanitizeMemberName(t.addr); cand != "" && cand != swarmMemberCoordinator && cand != swarmMemberNarrator {
			if _, ok := members[cand]; ok {
				return cand
			}
		}
		return swarmMemberPlatform
	}
	// The owner of a delegating parent that is not in the input (past the
	// task cap or TASKS retention), from the child's ViaAddressee: the
	// addressee mapping resolveTaskMember applies, or "" when it names no
	// member, so the caller keeps its own fallback.
	resolveViaMember := func(t *swarmTaskView) string {
		if cand := sanitizeMemberName(t.viaAddr); cand != "" && cand != swarmMemberCoordinator && cand != swarmMemberNarrator {
			if _, ok := members[cand]; ok {
				return cand
			}
		}
		return ""
	}
	taskByID := make(map[string]*swarmTaskView, len(tasks))
	for _, t := range tasks {
		taskByID[t.id] = t
	}

	var candidates []candidateBoardEvent
	addCandidate := func(ev candidateBoardEvent) {
		if ev.ts.IsZero() {
			ev.ts = now
		}
		ev.orderIdx = len(candidates)
		candidates = append(candidates, ev)
	}

	addCandidate(candidateBoardEvent{
		key:         "deploy:" + sanitizeItemID(title),
		ts:          now,
		pinnedFirst: true,
		from:        swarmMemberCoordinator,
		render: func(int) string {
			return fmt.Sprintf("Connected @%s to kube-agents swarm %s.", swarmMemberPlatform, swarmCode(title))
		},
	})

	for _, c := range rostered {
		member, label := c.member, c.aliasLabel
		descSuffix := ""
		if c.desc != "" {
			descSuffix = " — " + swarmCode(c.desc)
		}
		addCandidate(candidateBoardEvent{
			key:      "session-start:" + member,
			ts:       c.firstTask,
			priority: 1,
			from:     swarmMemberCoordinator,
			render: func(int) string {
				return fmt.Sprintf("@%s joined from console conversation %s%s.", member, swarmCode(label), descSuffix)
			},
		})
	}

	for _, t := range tasks {
		target := resolveTaskMember(t)
		inKey := "task-in:" + t.id
		taskID, addrLabel := t.id, swarmAddressee(t.addrLabel, in.Cards[t.addr])
		prompt := t.prompt

		if t.viaTaskID != "" {
			// Adam's delegation: posted by the parent task's owner; when the
			// parent is not in the input, the owner its ViaAddressee names,
			// else the conversation's member.
			from := swarmMemberCoordinator
			if parent, ok := taskByID[t.viaTaskID]; ok {
				from = resolveTaskMember(parent)
			} else if m := resolveViaMember(t); m != "" {
				from = m
			} else if c := byToken[t.token]; c != nil && c.member != "" {
				from = c.member
			}
			viaID := t.viaTaskID
			addCandidate(candidateBoardEvent{
				key:      inKey,
				ts:       t.submitted,
				priority: 3,
				from:     from,
				citeKey:  "task-in:" + viaID,
				taskID:   taskID,
				render: func(citeSeq int) string {
					if prompt == "" {
						return fmt.Sprintf("@%s delegated task `%s`%s to %s.", target, taskID, swarmCite(citeSeq), addrLabel)
					}
					return fmt.Sprintf("@%s delegated task `%s`%s to %s: %s", target, taskID, swarmCite(citeSeq), addrLabel, swarmCode(prompt))
				},
			})
		} else {
			addCandidate(candidateBoardEvent{
				key:      inKey,
				ts:       t.submitted,
				priority: 2,
				from:     swarmMemberCoordinator,
				taskID:   taskID,
				render: func(int) string {
					if prompt == "" {
						return fmt.Sprintf("@%s [task `%s`] submitted to %s.", target, taskID, addrLabel)
					}
					return fmt.Sprintf("@%s [task `%s`] %s", target, taskID, swarmCode(prompt))
				},
			})
		}

		// Adam's addressing: input-required goes to the coordinator; a
		// completion to the coordinator, or to the delegating parent's owner.
		if t.state == lib.StateInputRequired && !t.terminal {
			ask := firstNonEmptyString(t.question, t.progress, t.reason, t.result, "awaiting user input")
			addCandidate(candidateBoardEvent{
				key: inputKey(t),
				// lib.Task keeps no update time, so an observed state is
				// stamped when this projection first saw it.
				ts:       now,
				priority: 5,
				from:     target,
				citeKey:  inKey,
				taskID:   taskID,
				render: func(citeSeq int) string {
					return fmt.Sprintf("@%s input required%s (`%s`): %s", swarmMemberCoordinator, swarmOn(citeSeq), taskID, swarmCode(ask))
				},
			})
		}

		if t.terminal {
			recipient := swarmMemberCoordinator
			if parent, ok := taskByID[t.viaTaskID]; ok && t.viaTaskID != "" {
				recipient = resolveTaskMember(parent)
			} else if t.viaTaskID != "" {
				recipient = firstNonEmptyString(resolveViaMember(t), recipient)
			}
			state := string(t.state)
			summary := firstNonEmptyString(t.result, t.reason, t.progress, prompt, state)
			addCandidate(candidateBoardEvent{
				key:      fmt.Sprintf("task-done:%s:%s", t.id, state),
				ts:       now,
				priority: 5,
				from:     target,
				citeKey:  inKey,
				taskID:   taskID,
				render: func(citeSeq int) string {
					return fmt.Sprintf("@%s `%s`%s (`%s`): %s", recipient, state, swarmOn(citeSeq), taskID, swarmCode(summary))
				},
			})
		}
	}

	// Causal clamping: every dependent event sorts after the event it cites.
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
	sort.SliceStable(candidates, func(i, j int) bool {
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

	// Emit what the ledger has not seen, at most maxNewBoardMessages per
	// call; the rest stays out of the ledger and is emitted next call.
	var newMessages []SwarmBoardMessage
	for _, c := range candidates {
		if _, seen := ledger.EventSeqs[c.key]; seen {
			continue
		}
		if len(newMessages) >= maxNewBoardMessages || ledger.NextSeq >= maxBoardSeq {
			break
		}
		from := c.from
		if _, ok := members[from]; !ok {
			from = swarmMemberPlatform
		}
		citeSeq := 0
		if c.citeKey != "" {
			citeSeq = ledger.EventSeqs[c.citeKey]
		}
		seq := ledger.NextSeq
		ledger.NextSeq++
		ledger.EventSeqs[c.key] = seq
		ledger.Authors[from] = true
		newMessages = append(newMessages, SwarmBoardMessage{
			Seq:   seq,
			Agent: from,
			Text:  strings.TrimSpace(c.render(citeSeq)),
			Time:  c.ts.UTC().Format(time.RFC3339),
		})
	}

	taskInSeq := make(map[string]int, len(tasks))
	taskLastSeq := make(map[string]int, len(tasks))
	liveKeys := make(map[string]bool, len(candidates))
	for _, c := range candidates {
		liveKeys[c.key] = true
		seq, ok := ledger.EventSeqs[c.key]
		if !ok || c.taskID == "" {
			continue
		}
		if strings.HasPrefix(c.key, "task-in:") {
			taskInSeq[c.taskID] = seq
		}
		if seq > taskLastSeq[c.taskID] {
			taskLastSeq[c.taskID] = seq
		}
	}
	trimLedger(&ledger, liveKeys, members)

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

	view := buildSwarmOverview(title, rostered, byAlias, tasks, taskInSeq, taskLastSeq, head, now)
	return SwarmSnapshot{
		Title:       title,
		Members:     members,
		NewMessages: newMessages,
		View:        view,
	}, ledger
}

func buildSwarmOverview(
	title string,
	rostered []*swarmConv,
	byAlias map[string]*swarmConv,
	tasks []*swarmTaskView,
	taskInSeq, taskLastSeq map[string]int,
	head int,
	now time.Time,
) SwarmView {
	activeTasks, completedTasks := 0, 0
	for _, t := range tasks {
		if t.terminal {
			completedTasks++
		} else {
			activeTasks++
		}
	}

	phaseLabel, phaseIdx, phaseTotal := "Monitoring Bus", 1, 2
	if activeTasks > 0 {
		phaseLabel = fmt.Sprintf("Executing %d Task(s)", activeTasks)
		phaseIdx = 2
	} else if completedTasks > 0 {
		phaseLabel = "Ready · Idle"
		phaseIdx = 2
	}
	headline := fmt.Sprintf("Connected to %s · %d conversation(s) · %d task(s) on TASKS", title, len(rostered), len(tasks))

	usedItemIDs := make(map[string]bool)
	allocID := func(prefix, raw string) string {
		slug := sanitizeItemID(prefix + "-" + raw)
		if !usedItemIDs[slug] {
			usedItemIDs[slug] = true
			return slug
		}
		// sanitizeItemID caps an id at maxItemIDBytes, so the base is cut to
		// leave room for the suffix; re-sanitising slug+suffix would cut the
		// suffix off and every retry would collide.
		for i := 2; ; i++ {
			suffix := fmt.Sprintf("-%d", i)
			base := slug
			if len(base)+len(suffix) > maxItemIDBytes {
				base = strings.TrimRight(base[:maxItemIDBytes-len(suffix)], "-")
			}
			if cand := base + suffix; !usedItemIDs[cand] {
				usedItemIDs[cand] = true
				return cand
			}
		}
	}
	seqOf := func(t *swarmTaskView) int {
		return firstPositiveInt(taskLastSeq[t.id], taskInSeq[t.id], head)
	}
	jump := func(seq int, label string) []SwarmAction {
		return []SwarmAction{{Type: "board_post", Target: seq, Label: fmt.Sprintf("%s #%d", label, seq)}}
	}

	// 1. Attention: input-required and failed tasks, newest first.
	attention := []SwarmAttentionItem{}
	for i := len(tasks) - 1; i >= 0 && len(attention) < maxAttentionItemsInView; i-- {
		t := tasks[i]
		seq := seqOf(t)
		switch {
		case t.state == lib.StateInputRequired && !t.terminal:
			attention = append(attention, SwarmAttentionItem{
				ID:       allocID("input", t.id),
				Kind:     "decision",
				Severity: "high",
				Title:    swarmText(fmt.Sprintf("Input required on task %s (%s)", t.id, t.addrLabel), maxViewTitleChars),
				Why:      swarmText(firstNonEmptyString(t.question, t.progress, t.reason, t.prompt), maxViewWhyChars),
				Refs:     []SwarmRef{{Seq: seq}},
				Actions:  jump(seq, "Jump to"),
			})
		case t.state == lib.StateFailed || t.state == lib.StateRejected:
			attention = append(attention, SwarmAttentionItem{
				ID:       allocID("fail", t.id),
				Kind:     "failure",
				Severity: "med",
				Title:    swarmText(fmt.Sprintf("Task %s (%s) %s", t.id, t.addrLabel, t.state), maxViewTitleChars),
				Why:      swarmText(firstNonEmptyString(t.reason, t.result, t.prompt), maxViewWhyChars),
				Refs:     []SwarmRef{{Seq: seq}},
				Actions:  jump(seq, "Jump to"),
			})
		}
	}

	// 2. Workstreams: one per lineage root that has a member on the roster,
	// owned by the root and its rostered descendants, then the platform's.
	groups := make(map[string][]*swarmConv)
	var roots []string
	for _, c := range rostered {
		if _, ok := groups[c.root]; !ok {
			roots = append(roots, c.root)
		}
		groups[c.root] = append(groups[c.root], c)
	}
	sort.Strings(roots)
	workstreams := []SwarmWorkstream{}
	for _, root := range roots {
		if len(workstreams) >= maxWorkstreamsInView-1 {
			break
		}
		group := groups[root]
		sort.SliceStable(group, func(i, j int) bool {
			if (group[i].alias == root) != (group[j].alias == root) {
				return group[i].alias == root
			}
			return group[i].member < group[j].member
		})
		owners := []string{}
		refs := []SwarmRef{}
		working, step := false, ""
		for _, c := range group {
			if len(owners) < maxWorkstreamOwners {
				owners = append(owners, c.member)
				refs = append(refs, SwarmRef{Member: c.member})
			}
			if c.working {
				working = true
				if step == "" {
					step = c.step
				}
			}
		}
		rc := byAlias[root]
		note := firstNonEmptyString(rc.desc, "Console conversation")
		if n := len(group) - 1; n > 0 {
			note = fmt.Sprintf("%s · %d sub-conversation(s)", note, n)
		}
		if working && step != "" {
			note = "⚡ " + step + " — " + note
		}
		workstreams = append(workstreams, SwarmWorkstream{
			ID:     allocID("ws-conv", firstNonEmptyString(rc.member, sanitizeAliasSlug(rc.alias))),
			Title:  swarmText("Conversation: "+rc.aliasLabel, maxViewTitleChars),
			Owners: owners,
			State:  "active",
			Note:   swarmText(note, maxViewTextItemChars),
			Refs:   refs,
		})
	}

	platformNote := "Hermes orchestrator (" + swarmMemberPlatform + ")"
	latestPlatformSeq := head
	for i := len(tasks) - 1; i >= 0; i-- {
		t := tasks[i]
		if t.addr == swarmMemberPlatform {
			latestPlatformSeq = seqOf(t)
			platformNote = fmt.Sprintf("Latest task %s (%s): %s", t.id, t.state, firstNonEmptyString(t.result, t.reason, t.progress, t.prompt, string(t.state)))
			break
		}
	}
	workstreams = append(workstreams, SwarmWorkstream{
		ID:      allocID("ws", "platform-fleet"),
		Title:   swarmText(fmt.Sprintf("Platform Agent (%s)", title), maxViewTitleChars),
		Owners:  []string{swarmMemberPlatform},
		State:   "active",
		Note:    swarmText(platformNote, maxViewTextItemChars),
		Refs:    []SwarmRef{{Member: swarmMemberPlatform}, {Seq: latestPlatformSeq}},
		Actions: jump(latestPlatformSeq, "Latest Post"),
	})

	// 3. Milestones: recent finished tasks, newest first.
	milestones := []SwarmTextItem{}
	for i := len(tasks) - 1; i >= 0 && len(milestones) < maxMilestonesInView; i-- {
		t := tasks[i]
		if !t.terminal {
			continue
		}
		seq := seqOf(t)
		milestones = append(milestones, SwarmTextItem{
			ID:      allocID("task", t.id),
			Text:    swarmText(fmt.Sprintf("Task %s (%s -> %s): %s", t.id, t.addrLabel, t.state, firstNonEmptyString(t.result, t.reason, t.progress, t.prompt, string(t.state))), maxViewTextItemChars),
			Refs:    []SwarmRef{{Seq: seq}},
			Actions: jump(seq, "Post"),
		})
	}
	if len(milestones) == 0 {
		milestones = append(milestones, SwarmTextItem{
			ID:   allocID("init", title),
			Text: swarmText(fmt.Sprintf("Connected to %s A2A v2 JetStream bus", title), maxViewTextItemChars),
			Refs: []SwarmRef{{Seq: head}},
		})
	}

	return SwarmView{
		Version:     1,
		AsOfSeq:     head,
		UpdatedAt:   now.UTC().Format(time.RFC3339),
		Headline:    swarmText(headline, maxViewHeadlineChars),
		Phase:       &SwarmPhase{Label: phaseLabel, Index: phaseIdx, Total: phaseTotal},
		Attention:   attention,
		Workstreams: workstreams,
		Milestones:  milestones,
		Risks:       []SwarmTextItem{},
	}
}

// sanitizeLedger validates a client-supplied ledger: malformed keys,
// non-positive or out-of-range seqs and non-member author names are dropped,
// and the maps are bounded. Which entries survive the bound depends only on
// the entries (highest seqs, then keys), never on map order.
func sanitizeLedger(prev BoardLedger) BoardLedger {
	out := BoardLedger{
		SchemaVersion: swarmLedgerSchema,
		NextSeq:       prev.NextSeq,
		EventSeqs:     make(map[string]int, min(len(prev.EventSeqs), maxLedgerEvents)),
		Authors:       make(map[string]bool, min(len(prev.Authors), maxLedgerAuthors)),
	}
	// Clamp, never reset: a reset would hand out seqs the board already
	// has. At the cap nothing more is emitted (2^30 posts; a documented
	// limit rather than a repeat).
	if out.NextSeq <= 0 {
		out.NextSeq = 1
	}
	if out.NextSeq > maxBoardSeq {
		out.NextSeq = maxBoardSeq
	}
	type kv struct {
		k string
		v int
	}
	events := make([]kv, 0, min(len(prev.EventSeqs), maxLedgerEvents*2))
	for k, v := range prev.EventSeqs {
		if v <= 0 || v >= maxBoardSeq || !validLedgerKey(k) {
			continue
		}
		events = append(events, kv{k, v})
	}
	sort.Slice(events, func(i, j int) bool {
		if events[i].v != events[j].v {
			return events[i].v > events[j].v
		}
		return events[i].k < events[j].k
	})
	if len(events) > maxLedgerEvents {
		events = events[:maxLedgerEvents]
	}
	for _, e := range events {
		out.EventSeqs[e.k] = e.v
		if e.v >= out.NextSeq {
			out.NextSeq = e.v + 1
		}
	}
	authors := make([]string, 0, min(len(prev.Authors), maxLedgerAuthors*2))
	for name, ok := range prev.Authors {
		if ok && validMemberName(name) {
			authors = append(authors, name)
		}
	}
	sort.Strings(authors)
	if len(authors) > maxLedgerAuthors {
		authors = authors[:maxLedgerAuthors]
	}
	for _, name := range authors {
		out.Authors[name] = true
	}
	return out
}

// trimLedger bounds the ledger on the way out. Keys this call's candidates
// can produce are kept first, so a live event is never forgotten and
// re-emitted; the rest are kept by highest seq. The input caps keep the live
// keys well under maxLedgerEvents. Authors on the roster are kept first.
func trimLedger(l *BoardLedger, live map[string]bool, members map[string]SwarmMember) {
	if len(l.EventSeqs) > maxLedgerEvents {
		keys := make([]string, 0, len(l.EventSeqs))
		for k := range l.EventSeqs {
			keys = append(keys, k)
		}
		sort.Slice(keys, func(i, j int) bool {
			a, b := keys[i], keys[j]
			if live[a] != live[b] {
				return live[a]
			}
			if l.EventSeqs[a] != l.EventSeqs[b] {
				return l.EventSeqs[a] > l.EventSeqs[b]
			}
			return a < b
		})
		for _, k := range keys[maxLedgerEvents:] {
			delete(l.EventSeqs, k)
		}
	}
	if len(l.Authors) > maxLedgerAuthors {
		names := make([]string, 0, len(l.Authors))
		for n := range l.Authors {
			names = append(names, n)
		}
		sort.Slice(names, func(i, j int) bool {
			_, ai := members[names[i]]
			_, aj := members[names[j]]
			if ai != aj {
				return ai
			}
			return names[i] < names[j]
		})
		for _, n := range names[maxLedgerAuthors:] {
			delete(l.Authors, n)
		}
	}
}

// validLedgerKey accepts the keys this projection and the bridge write:
// short, printable ASCII without spaces.
func validLedgerKey(k string) bool {
	if k == "" || len(k) > maxLedgerKeyBytes {
		return false
	}
	for i := 0; i < len(k); i++ {
		if c := k[i]; c <= ' ' || c > '~' {
			return false
		}
	}
	return true
}

func validMemberName(n string) bool {
	return n != "" && len(n) <= maxMemberNameBytes && sanitizeMemberName(n) == n
}

// validTaskID accepts the characters A2A task ids use here. A task id lands
// in ledger keys and in code spans on the board, so anything else is refused
// rather than rewritten.
func validTaskID(id string) bool {
	if id == "" || len(id) > maxSwarmTaskIDBytes {
		return false
	}
	for _, r := range id {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_' || r == '.' || r == ':') {
			return false
		}
	}
	return true
}

// normalizeConversations validates, dedupes (by alias) and bounds the
// conversations, returning them sorted by alias. At the cap, conversations
// whose token owns a non-terminal task (liveTokens) are kept first, then the
// most recently used, so live work is never cut for idle conversations.
func normalizeConversations(in []SwarmConversation, liveTokens map[string]bool, now time.Time) []*swarmConv {
	list := make([]SwarmConversation, 0, len(in))
	for _, c := range in {
		c.Alias = strings.TrimSpace(c.Alias)
		if c.Alias == "" || len(c.Alias) > maxSwarmAliasBytes {
			continue
		}
		if len(c.Token) > maxSwarmHandleBytes {
			c.Token = ""
		}
		if len(c.Parent) > maxSwarmAliasBytes {
			c.Parent = ""
		}
		list = append(list, c)
	}
	sort.Slice(list, func(i, j int) bool {
		a, b := list[i], list[j]
		if a.Alias != b.Alias {
			return a.Alias < b.Alias
		}
		if !a.LastUsed.Equal(b.LastUsed) {
			return a.LastUsed.After(b.LastUsed)
		}
		if a.Token != b.Token {
			return a.Token < b.Token
		}
		if a.Parent != b.Parent {
			return a.Parent < b.Parent
		}
		if a.AGYConversationID != b.AGYConversationID {
			return a.AGYConversationID < b.AGYConversationID
		}
		return a.Description < b.Description
	})
	out := make([]*swarmConv, 0, len(list))
	for i, c := range list {
		if i > 0 && list[i-1].Alias == c.Alias {
			continue
		}
		out = append(out, &swarmConv{
			alias:      c.Alias,
			token:      strings.TrimSpace(c.Token),
			desc:       swarmLine(c.Description, maxSwarmDescriptionChars),
			aliasLabel: swarmText(c.Alias, maxViewTitleChars),
			parent:     strings.TrimSpace(c.Parent),
			lastUsed:   clampNotAfter(c.LastUsed.UTC(), now),

			agyConversationID: safeConversationID(c.AGYConversationID),
		})
	}
	if len(out) > maxSwarmInputConvs {
		sort.SliceStable(out, func(i, j int) bool {
			li, lj := out[i].token != "" && liveTokens[out[i].token], out[j].token != "" && liveTokens[out[j].token]
			if li != lj {
				return li
			}
			return out[i].lastUsed.After(out[j].lastUsed)
		})
		out = out[:maxSwarmInputConvs]
		sort.Slice(out, func(i, j int) bool { return out[i].alias < out[j].alias })
	}
	return out
}

// maxConversationIDBytes bounds a client conversation id echoed onto a member.
const maxConversationIDBytes = 128

// safeConversationID returns id (trimmed) when it is a safe opaque
// identifier, since the client joins it into paths (Antigravity's
// per-conversation directories): at most maxConversationIDBytes bytes of
// [A-Za-z0-9._-] only, not starting with '.' (so never "." or ".."), no
// "..", and not a Windows reserved device name (CON, PRN, AUX, NUL, COM0-9,
// LPT0-9, in any case and with any extension), which opens a device rather
// than a file there. Anything else is "", so the member simply carries no
// link.
func safeConversationID(id string) string {
	id = strings.TrimSpace(id)
	if id == "" || len(id) > maxConversationIDBytes || id[0] == '.' || strings.Contains(id, "..") {
		return ""
	}
	for i := 0; i < len(id); i++ {
		c := id[i]
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '.' || c == '_' || c == '-') {
			return ""
		}
	}
	base, _, _ := strings.Cut(strings.ToUpper(id), ".")
	switch {
	case base == "CON" || base == "PRN" || base == "AUX" || base == "NUL":
		return ""
	case len(base) == 4 && (strings.HasPrefix(base, "COM") || strings.HasPrefix(base, "LPT")) && base[3] >= '0' && base[3] <= '9':
		return ""
	}
	return id
}

// resolveLineage sets each conversation's parent and root. Parent is only a
// name: one that is missing, the conversation itself, part of a cycle, or
// more than maxLineageDepth hops from a root is ignored, and the
// conversation is its own root.
func resolveLineage(convs []*swarmConv) {
	byAlias := make(map[string]*swarmConv, len(convs))
	for _, c := range convs {
		byAlias[c.alias] = c
	}
	parentOf := func(alias string) string {
		c := byAlias[alias]
		if c == nil || c.parent == alias || byAlias[c.parent] == nil {
			return ""
		}
		return c.parent
	}
	for _, c := range convs {
		c.root = c.alias
		cur := c.alias
		seen := map[string]bool{cur: true}
		ok := true
		for depth := 0; ; depth++ {
			p := parentOf(cur)
			if p == "" {
				break
			}
			if seen[p] || depth >= maxLineageDepth {
				ok = false
				break
			}
			seen[p] = true
			cur = p
		}
		if ok {
			c.root = cur
		}
	}
	for _, c := range convs {
		if c.root == c.alias {
			c.parent = ""
		}
	}
}

// normalizeTasks validates, dedupes (by task id) and bounds the tasks,
// returning them in submission order.
func normalizeTasks(in []SwarmTask, now time.Time) []*swarmTaskView {
	list := make([]*swarmTaskView, 0, min(len(in), maxSwarmInputTasks*2))
	for i := range in {
		st := &in[i]
		id := strings.TrimSpace(st.TaskID)
		if !validTaskID(id) {
			continue
		}
		sub := st.Submitted.UTC()
		if sub.IsZero() || sub.After(now) {
			sub = now
		}
		addr := strings.TrimSpace(st.Addressee)
		if len(addr) > maxSwarmHandleBytes {
			addr = ""
		}
		token := ""
		if conv := strings.TrimSpace(st.Conversation); strings.HasPrefix(conv, swarmConsolePrefix) && len(conv) <= maxSwarmHandleBytes {
			token = strings.TrimPrefix(conv, swarmConsolePrefix)
		}
		via := strings.TrimSpace(st.ViaTaskID)
		viaAddr := ""
		if !validTaskID(via) || via == id {
			via = ""
		} else if va := strings.TrimSpace(st.ViaAddressee); len(va) <= maxSwarmHandleBytes {
			viaAddr = va
		}
		tv := &swarmTaskView{
			id: id, addr: addr, addrLabel: firstNonEmptyString(swarmText(addr, 64), swarmMemberPlatform),
			token: token, viaTaskID: via, viaAddr: viaAddr, submitted: sub, state: lib.StateSubmitted,
			prompt: swarmLine(st.Prompt, maxBoardEventTextChars),
		}
		if t := st.Task; t != nil {
			tv.state = knownTaskState(t.State)
			// A final event with a non-terminal state is a protocol
			// violation; the state, not the flag, decides.
			tv.terminal = tv.state.Terminal()
			if art := t.Artifact(lib.ArtifactResult); art != nil {
				tv.result = swarmLine(boundedText(art.Parts, false), maxBoardEventTextChars)
			}
			if t.FinalMessage != nil {
				tv.reason = swarmLine(boundedText(t.FinalMessage.Parts, false), maxBoardEventTextChars)
			}
			if art := t.Artifact(lib.ArtifactProgress); art != nil {
				tv.progress = swarmLine(boundedText(art.Parts, true), maxBoardEventTextChars)
			}
			tv.step = swarmText(lastActivityTool(t), maxSwarmStepChars)
			for _, s := range t.StatusHistory {
				if s == lib.StateInputRequired {
					tv.asks++
				}
			}
		}
		if tv.state == lib.StateInputRequired {
			if st.Question != nil {
				tv.question = swarmLine(boundedText(st.Question.Parts, false), maxBoardEventTextChars)
			}
			tv.questionSeq = st.QuestionSeq
		}
		list = append(list, tv)
	}
	// A total order over every field the projection reads, so duplicates
	// and ties resolve the same way whatever the input order.
	sort.Slice(list, func(i, j int) bool { return taskLess(list[i], list[j]) })
	seen := make(map[string]bool, len(list))
	out := list[:0]
	for _, t := range list {
		if seen[t.id] {
			continue
		}
		seen[t.id] = true
		out = append(out, t)
	}
	if len(out) > maxSwarmInputTasks {
		// Keep pending input-required tasks first (they need a person),
		// then the most recent; then restore submission order.
		sort.SliceStable(out, func(i, j int) bool {
			pi, pj := out[i].pendingInput(), out[j].pendingInput()
			if pi != pj {
				return pi
			}
			return taskLess(out[j], out[i])
		})
		out = out[:maxSwarmInputTasks]
		sort.Slice(out, func(i, j int) bool { return taskLess(out[i], out[j]) })
	}
	return out
}

// inputKey is the ledger key of t's current input request. A task resumed
// after input-required can ask again on the same id, so each ask needs its
// own key: an unchanged ask keeps its key and stays deduped, a new one posts.
// The key is the stream sequence of the input-required status-update
// (task-input:<id>@<seq>). Only when the caller has no sequence (0) does it
// fall back to counting input-required entries in the status history
// (task-input:<id>, then task-input:<id>#<n> from the second); that count
// can fall when TASKS drops a task's oldest events, so a later ask can then
// reuse a key already posted and be suppressed. '@' and '#' are not task id
// characters, so the keys of two tasks never collide.
func inputKey(t *swarmTaskView) string {
	if t.questionSeq > 0 {
		return fmt.Sprintf("task-input:%s@%d", t.id, t.questionSeq)
	}
	if t.asks > 1 {
		return fmt.Sprintf("task-input:%s#%d", t.id, t.asks)
	}
	return "task-input:" + t.id
}

func (t *swarmTaskView) pendingInput() bool {
	return t.state == lib.StateInputRequired && !t.terminal
}

func taskLess(a, b *swarmTaskView) bool {
	if !a.submitted.Equal(b.submitted) {
		return a.submitted.Before(b.submitted)
	}
	for _, p := range [][2]string{
		{a.id, b.id}, {a.addr, b.addr}, {a.token, b.token}, {a.viaTaskID, b.viaTaskID}, {a.viaAddr, b.viaAddr},
		{string(a.state), string(b.state)}, {a.prompt, b.prompt}, {a.result, b.result},
		{a.reason, b.reason}, {a.progress, b.progress}, {a.step, b.step}, {a.question, b.question},
	} {
		if p[0] != p[1] {
			return p[0] < p[1]
		}
	}
	if a.questionSeq != b.questionSeq {
		return a.questionSeq < b.questionSeq
	}
	if a.asks != b.asks {
		return a.asks < b.asks
	}
	return !a.terminal && b.terminal
}

func knownTaskState(s lib.TaskState) lib.TaskState {
	switch s {
	case lib.StateSubmitted, lib.StateWorking, lib.StateInputRequired, lib.StateCompleted,
		lib.StateFailed, lib.StateCanceled, lib.StateRejected, lib.StateAuthRequired:
		return s
	case "":
		return lib.StateSubmitted
	}
	return "unknown"
}

// boundedText joins the text parts, reading at most swarmScanBytes. With
// last set it returns only the last text part (a progress line replaces the
// one before it).
func boundedText(parts []lib.Part, last bool) string {
	if last {
		for i := len(parts) - 1; i >= 0; i-- {
			if parts[i].Kind == "text" && strings.TrimSpace(parts[i].Text) != "" {
				return capBytes(parts[i].Text, swarmScanBytes)
			}
		}
		return ""
	}
	var b strings.Builder
	for _, p := range parts {
		if p.Kind != "text" {
			continue
		}
		b.WriteString(capBytes(p.Text, swarmScanBytes-b.Len()))
		if b.Len() >= swarmScanBytes {
			break
		}
	}
	return b.String()
}

// lastActivityTool is the tool of the newest activity entry, Adam's
// latestActiveStepTitle on what the activity artifact carries.
func lastActivityTool(t *lib.Task) string {
	art := t.Artifact(lib.ArtifactActivity)
	if art == nil {
		return ""
	}
	for i := len(art.Parts) - 1; i >= 0; i-- {
		p := art.Parts[i]
		if p.Kind != "data" || len(p.Data) == 0 || len(p.Data) > maxActivityEntryBytes {
			continue
		}
		var e struct {
			Tool string `json:"tool"`
		}
		if json.Unmarshal(p.Data, &e) == nil && strings.TrimSpace(e.Tool) != "" {
			return e.Tool
		}
	}
	return ""
}

// isSessionWorkerTask reports whether t is a console conversation's own
// top-level turn run by something other than the platform agent or the
// gateway: a session worker (Adam's topological rule, without card tasks).
func isSessionWorkerTask(t *swarmTaskView) bool {
	return t.token != "" && t.viaTaskID == "" &&
		t.addr != "" && t.addr != swarmMemberPlatform && t.addr != swarmMemberGateway
}

// swarmText applies cell()'s rules without the code span: control
// characters become spaces, format characters (bidi overrides, zero-width)
// are removed, whitespace runs collapse, backticks become quotes, and the
// result is capped in runes. "@" becomes a fullwidth at sign so untrusted
// text never mentions (and so wakes) a roster member, angle brackets become
// guillemets so no HTML survives, and "](" is broken so no markdown link or
// image does: view text is not code-wrapped.
func swarmText(s string, maxRunes int) string {
	s = capBytes(s, swarmScanBytes)
	s = strings.Map(func(r rune) rune {
		switch {
		case r == '`':
			return '\''
		case r == '@':
			return '＠'
		case r == '<':
			return '‹'
		case r == '>':
			return '›'
		case unicode.IsControl(r):
			return ' '
		case unicode.Is(unicode.Cf, r):
			return -1
		}
		return r
	}, s)
	s = strings.Join(strings.Fields(s), " ")
	// Break markdown link and image syntax: "[x](y)" no longer parses as a
	// link once the bracket and the parenthesis are apart.
	s = strings.ReplaceAll(s, "](", "] (")
	if maxRunes > 1 && utf8.RuneCountInString(s) > maxRunes {
		s = string([]rune(s)[:maxRunes-1]) + swarmEllipsis
	}
	return s
}

// swarmLine is swarmText on the first non-empty line, the summary Adam's
// firstLine took.
func swarmLine(s string, maxRunes int) string {
	return swarmText(firstLine(capBytes(s, swarmScanBytes)), maxRunes)
}

// swarmCode renders already-sanitised text as an inline code span, so the
// board renders it as inert data. swarmText has removed every backtick.
func swarmCode(s string) string {
	if s == "" {
		return "``"
	}
	return "`" + s + "`"
}

func swarmAddressee(label string, card AgentCard) string {
	out := swarmCode(label)
	if name := swarmText(card.Name, maxSwarmDescriptionChars); name != "" && name != label {
		out += " (" + swarmCode(name) + ")"
	}
	return out
}

func swarmCite(seq int) string {
	if seq > 0 {
		return fmt.Sprintf(" (#%d)", seq)
	}
	return ""
}

func swarmOn(seq int) string {
	if seq > 0 {
		return fmt.Sprintf(" on #%d", seq)
	}
	return ""
}

func clampNotAfter(t, now time.Time) time.Time {
	if t.After(now) {
		return now
	}
	return t
}

// capBytes cuts s to at most n bytes without splitting a rune.
func capBytes(s string, n int) string {
	if n <= 0 {
		return ""
	}
	if len(s) <= n {
		return s
	}
	return strings.ToValidUTF8(s[:n], "")
}

func firstNonEmptyString(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}

func firstPositiveInt(vals ...int) int {
	for _, v := range vals {
		if v > 0 {
			return v
		}
	}
	return 0
}

// The helpers below are Adam's, nearly verbatim.

// deriveSessionMemberName names a conversation's roster member from its
// alias; an alias with no usable slug falls back to its parent's slug, then
// to a short form of its handle.
func deriveSessionMemberName(alias, parentSlug, handle string) string {
	if slug := sanitizeAliasSlug(alias); slug != "" && slug != "default" {
		if strings.HasPrefix(slug, "session-") || strings.HasPrefix(slug, "s-") {
			return sanitizeMemberName(slug)
		}
		return sanitizeMemberName("session-" + slug)
	}
	if parentSlug != "" && parentSlug != "default" {
		return sanitizeMemberName("session-" + parentSlug + "-sub")
	}
	if short := sanitizeAliasSlug(capBytes(handle, 8)); short != "" {
		return sanitizeMemberName("session-" + short)
	}
	return "session-worker"
}

func uniqueMemberName(base string, used map[string]bool) string {
	clean := sanitizeMemberName(base)
	if len(clean) > maxMemberNameBytes-4 {
		clean = strings.Trim(clean[:maxMemberNameBytes-4], "-")
	}
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
	return ""
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
		if b.Len() > maxSwarmHandleBytes {
			break
		}
	}
	return strings.Trim(b.String(), "-")
}

func sanitizeAliasSlug(raw string) string {
	lower := strings.ToLower(strings.TrimSpace(capBytes(raw, maxSwarmAliasBytes)))
	if lower == "" {
		return ""
	}
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
	}
	res := strings.Trim(b.String(), "-")
	for _, trimWord := range []string{"kube-agents-", "kube-session-", "kube-", "-session-worker", "-worker", "-subagent"} {
		res = strings.TrimPrefix(res, trimWord)
		res = strings.TrimSuffix(res, trimWord)
	}
	res = strings.Trim(res, "-")
	if len(res) > 28 {
		res = strings.Trim(res[:28], "-")
	}
	return res
}

func sanitizeItemID(raw string) string {
	lower := strings.ToLower(strings.TrimSpace(capBytes(raw, swarmScanBytes)))
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
		if b.Len() >= maxItemIDBytes {
			break
		}
	}
	res := strings.Trim(b.String(), "-")
	if res == "" {
		return "item"
	}
	return res
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
