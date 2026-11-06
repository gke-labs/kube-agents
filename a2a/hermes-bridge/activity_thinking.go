package hermesbridge

import (
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

var (
	thinkTagPattern      = regexp.MustCompile(`(?is)<think>(.*?)</think>`)
	scratchpadTagPattern = regexp.MustCompile(`(?is)<REASONING_SCRATCHPAD>(.*?)</REASONING_SCRATCHPAD>`)
	pyReprReasoningRe    = regexp.MustCompile(`reasoning(?:_content)?=['"]([^'"]+)['"]`)
	cmdClusterFlagRe     = regexp.MustCompile(`(?:--cluster|--context)[=\s]+['"]?([A-Za-z0-9_.:-]+)['"]?`)
	kanbanCardIDPattern  = regexp.MustCompile(`\b(t_[A-Za-z0-9_-]{2,32})\b`)
	kanbanCardIDExact    = regexp.MustCompile(`^t_[A-Za-z0-9_-]{2,32}$`)

	inlineTokenValuePattern = regexp.MustCompile(`(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{16,}|ya29\.[A-Za-z0-9._-]{16,}|1//[0-9A-Za-z_-]{20,}|AIza[A-Za-z0-9_-]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[A-Z0-9]{12,}|glpat-[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{16,}|r8_[A-Za-z0-9]{20,}|npm_[A-Za-z0-9]{20,}|pypi-[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})`)
	// A private-key armour header ends in "PRIVATE KEY-----" (PEM, OpenSSH,
	// PKCS#8, EC, RSA, DSA) or "PRIVATE KEY BLOCK-----" (PGP).
	pemPrivateKeyBlockPattern     = regexp.MustCompile(`(?is)-----BEGIN [A-Z0-9 _-]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z0-9 _-]*PRIVATE KEY(?: BLOCK)?-----`)
	pemPrivateKeyOpenPattern      = regexp.MustCompile(`(?is)-----BEGIN [A-Z0-9 _-]*PRIVATE KEY(?: BLOCK)?-----.*`)
	inlineAuthHeaderPattern       = regexp.MustCompile(`(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}`)
	inlineAuthSchemePrefixPattern = regexp.MustCompile(`(?i)^(Bearer|Basic|Token|ApiKey|Api-Key|Digest|Negotiate|AWS4-HMAC-SHA256|Mutual|OAuth)([ \t]+)`)
	// The password runs to the userinfo's closing @ and may hold a path
	// character, as base64-style passwords do; a URL with an @ in its path
	// after a port is over-redacted, the safe side.
	inlineURLCredPattern     = regexp.MustCompile(`([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]*:)[^@\s'"]+(@)`)
	cliUserPassPattern       = regexp.MustCompile(`(?i)(\b(?:curl|wget|mysql|mysqldump|mariadb|mysqladmin|(?:docker|helm)\s+login)\b[^\r\n]*?\s(?:-u|--user)(?:=|[ \t]+))(["']?)([^"'\s:]+):([^"'\s]+)(["']?)`)
	cliAttachedPassPattern   = regexp.MustCompile(`(?i)(\b(?:mysql|mysqldump|mariadb|mysqladmin|sshpass|mongosh|mongo)\b[^\r\n]*?\s-p)([^\s"';|&]+)`)
	cliSpacedPassPattern     = regexp.MustCompile(`(?i)(\b(?:mysql|mysqldump|mariadb|mysqladmin|sshpass|mongosh|mongo|redis-cli|az\s+login|(?:docker|helm)\s+login)\b[^\r\n]*?\s-[pa][ \t]+)("[^"\r\n]*"|'[^'\r\n]*'|[^\s"';|&]+)`)
	cliLongSecretFlagPattern = regexp.MustCompile(`(--([A-Za-z0-9_.-]+)(=|[ \t]+))("[^"\r\n]*"|'[^'\r\n]*'|[^\s"';|&]+)`)
	// Prose that states a secret: "the password is hunter2", "password for
	// admin is hunter2", "the token was abc", "the password is now hunter2",
	// "the secret is set to abc". The verb may be followed by a qualifier
	// (now, currently, still, just, simply, set to, equal to) or a colon,
	// none of which is the value.
	proseSecretPattern          = regexp.MustCompile(`(?i)\b((?:the\s+)?(?:[A-Za-z0-9_.-]+\s+)?(?:password|passwd|passphrase|secret|token|api[ _-]?key|credential)\s+(?:for\s+[^\s,.;:!?)'"\]]+\s+)?(?:is|was|=|will\s+be|should\s+be|has\s+been\s+set\s+to)\s+(?:(?:now|currently|still|just|simply|also|again)\s+)*(?:(?:set|equal|changed|updated|reset)\s+to\s+)?(?::\s*)?)("[^"\r\n]*"|'[^'\r\n]*'|[^\s,.;:!?)'"\]]+)`)
	inlineKVSecretPattern       = regexp.MustCompile(`((?:\\?["'])?)([A-Za-z0-9_.-]+)((?:\\?["'])?)([ \t]*([=:])[ \t]*)`)
	inlineExtraSecretKeyPattern = regexp.MustCompile(`(?i)(?:^|[_.-])(?:pass|pwd|auth|apikey|access[_-]?key|secret[_-]?key|privatekey|id_rsa|id_ed25519|id_ecdsa|dockercfg|dockerconfigjson|client[_-]?certificate[_-]?data)(?:$|[_.-])|\.(?:key|pem|crt|p12|pfx|dockercfg|dockerconfigjson)$`)
	// An all-caps env-style key whose last component says it holds a key,
	// credentials, a DSN or private material (ENCRYPTION_KEY, STRIPE_KEY,
	// AWS_CREDS, DB_DSN, RSA_PRIVATE): the spelling is the credential's,
	// whatever the first component names. Case-sensitive on purpose, so a
	// prose "primary key" or a JSON "sort_key" is not blanked.
	allCapsSecretSuffixKeyPattern = regexp.MustCompile(`^[A-Z][A-Z0-9_]*_(?:KEY|KEYS|CREDS|DSN|PRIVATE)$`)
	leadingEqAssignmentPattern    = regexp.MustCompile(`^([A-Za-z_][A-Za-z0-9_.-]*)=[^\s=]`)
	allCapsEnvKeyPattern          = regexp.MustCompile(`^[A-Z_][A-Z0-9_]+$`)
	base64PaddingTokenPattern     = regexp.MustCompile(`^[A-Za-z0-9+/_-]+={1,2}(?:[\s,;)\]}>]|\\?["'](?:\s|$)|$)`)
	yamlSecretBlockHeaderPattern  = regexp.MustCompile(`^([ \t]*)(?:data|stringData)[ \t]*:[ \t]*(?:#.*)?$`)
	yamlBlockEntryPattern         = regexp.MustCompile(`^([ \t]+[-A-Za-z0-9_./]+[ \t]*:[ \t]*)([^\r\n#]+?)([ \t]*(?:#.*)?)$`)

	// envFlagsWithValue are flags to `env` that consume the following
	// argument before the command name.
	envFlagsWithValue = map[string]bool{
		"-u": true, "--unset": true,
		"-C": true, "--chdir": true,
		"-S": true, "--split-string": true,
	}
	// shellFlagsWithValue are CLI flags that take a separate value argument
	// before the subcommand token (e.g. `kubectl -n kube-system get pods`).
	shellFlagsWithValue = map[string]bool{
		"-n": true, "--namespace": true,
		"-c": true, "--context": true,
		"--cluster": true, "--kubeconfig": true,
		"--server": true, "-s": true,
		"--user": true, "--token": true,
		"-C": true,
		"-f": true, "--filename": true,
		"-k": true, "--kustomize": true,
		"-l": true, "--selector": true, "--field-selector": true,
		"-o": true, "--output": true, "--template": true,
		"-v": true, "--v": true,
	}
)

const (
	// apiSynthesisStepTitle is the step the bridge publishes on rich API
	// turns when the model stops calling tools and writes its answer.
	apiSynthesisStepTitle = "Synthesizing findings into final response."
	// reasoningStepTitle is the step a request that reasoned without
	// planning a call and without stopping stands for. It is a fixed title:
	// the reasoning's text does not go on the bus (see the executor note in
	// activity.go).
	reasoningStepTitle = "Reasoning about the next step."
	// maxPlannedTitles bounds the call-id -> title map one task keeps so
	// activity entries can carry the title their step was published under.
	maxPlannedTitles = 4096
	// kanbanWorkspacesMarker is the path segment under which Kanban workers
	// run their per-card workspaces.
	kanbanWorkspacesMarker     = "/kanban/workspaces/"
	workerLabelSessionCap      = 24
	entryTitleFallbackCap      = activityWordCap * 4
	shortClusterRefCap         = 48
	shortFilePathCap           = 56
	shortCardTitleCap          = 64
	shortCardWordBreakLookback = 14
	shortCardWordBreakMin      = 12
	shortCardSuffixMinPrefix   = 8
	toolSearchQueryCap         = 36
	leadingSubcommandCap       = 24
	sessionTimestampPrefixLen  = 15
	sessionTimestampClockSkew  = 2 * time.Second
	maxStepTitleBytes          = 250
	maxThinkingBytes           = 4000
)

// sessionTrace is the API-executor side of activityState: what the door
// learns from post_api_request deliveries (the model's plan, published as
// step records whose titles name the tools and the identifiers they act on)
// and how worker sessions map to the Kanban cards they execute. The model's
// reasoning text is read only to tell a request that reasoned from one that
// did not; none of it is published.
type sessionTrace struct {
	apiCalls             int
	turnCardsFiled       bool
	prevBatchKey         string
	plannedTitles        map[string]string // tool call id -> the step title it was planned under
	sessionCards         map[string]string // worker sessionID -> cardID
	cardTitles           map[string]string // cardID -> short cluster/card title
	claimedSessions      map[string]bool   // worker sessionID claimed by this task
	reportedSettledCards map[string]bool   // cardID -> KindSubtask step already emitted for this task
}

type hookAssistantMessage struct {
	Content                string          `json:"content"`
	ReasoningContent       string          `json:"reasoning_content"`
	Reasoning              string          `json:"reasoning"`
	ReasoningDetails       json.RawMessage `json:"reasoning_details"`
	AnthropicContentBlocks json.RawMessage `json:"anthropic_content_blocks"`
	ToolCalls              []struct {
		ID       string `json:"id"`
		Name     string `json:"name"`
		Function struct {
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
		} `json:"function"`
		Arguments json.RawMessage `json:"arguments"`
	} `json:"tool_calls"`
}

func capBytes(s string, maxBytes int) string {
	if len(s) <= maxBytes {
		return s
	}
	for maxBytes > 0 && !utf8.RuneStart(s[maxBytes]) {
		maxBytes--
	}
	return s[:maxBytes]
}

func (a *activityState) initSessionTraceLocked() {
	if a.trace.sessionCards == nil {
		a.trace.sessionCards = make(map[string]string)
		a.trace.cardTitles = make(map[string]string)
		a.trace.claimedSessions = make(map[string]bool)
		a.trace.plannedTitles = make(map[string]string)
		a.trace.reportedSettledCards = make(map[string]bool)
	}
}

// markCardSettledReported records cardID as having emitted its settled KindSubtask step for
// this task and returns true if this is the first time it was recorded across all turns.
func (a *activityState) markCardSettledReported(cardID string) bool {
	if a == nil || cardID == "" {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.initSessionTraceLocked()
	if a.trace.reportedSettledCards[cardID] {
		return false
	}
	a.trace.reportedSettledCards[cardID] = true
	return true
}

// SyncKanbanCard records what the board says about a card - its title and
// the worker session executing it - so the door attributes that session's
// calls to the card.
func (a *activityState) SyncKanbanCard(cardID, title, workerSessionID string) {
	if a == nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.initSessionTraceLocked()
	// Capped as the argument path caps a worker's card id.
	cardID = capBytes(strings.TrimSpace(cardID), maxSubtaskIDBytes)
	title = strings.TrimSpace(title)
	workerSessionID = strings.TrimSpace(workerSessionID)
	if cardID != "" && title != "" {
		// kanban.db's title is model-written: scrubbed, as at kanban_create.
		a.trace.cardTitles[cardID] = shortCardClusterOrTitle(scrubInlineSecrets(title))
	}
	if workerSessionID != "" && cardID != "" {
		a.trace.sessionCards[workerSessionID] = cardID
		a.trace.claimedSessions[workerSessionID] = true
	}
}

func extractKanbanCardIDFromCwd(cwd string) string {
	idx := strings.Index(cwd, kanbanWorkspacesMarker)
	if idx < 0 {
		return ""
	}
	rest := cwd[idx+len(kanbanWorkspacesMarker):]
	if slash := strings.IndexByte(rest, '/'); slash >= 0 {
		rest = rest[:slash]
	}
	// The segment is hermes's card id; anything else (an overlong or odd
	// path a tool changed into) names no card.
	if rest = strings.TrimSpace(rest); !kanbanCardIDExact.MatchString(rest) {
		return ""
	}
	return rest
}

func unwrapHermesToolCall(toolName string, rawInput json.RawMessage) (string, map[string]any) {
	parsed := decodeToolArgsMap(rawInput)
	if toolName == hermesToolCallWrapper && parsed != nil {
		if calls, ok := parsed[wrapperCallsKey].([]any); ok {
			for _, item := range calls {
				cm, ok := item.(map[string]any)
				if !ok {
					continue
				}
				subName := strings.TrimSpace(fmt.Sprint(cm[wrapperCallNameKey]))
				subArgs, _ := cm[wrapperCallArgsKey].(map[string]any)
				if strings.HasPrefix(subName, "kanban_") || len(calls) == 1 {
					return subName, subArgs
				}
			}
		}
	}
	return toolName, parsed
}

func (a *activityState) observeWorkerContextLocked(d hookDelivery, rawInput json.RawMessage, createdCardID string) {
	a.initSessionTraceLocked()
	sessID := strings.TrimSpace(d.SessionID)
	if cid := extractKanbanCardIDFromCwd(d.Cwd); cid != "" && sessID != "" && sessID != a.sessionID {
		a.trace.sessionCards[sessID] = cid
		a.trace.claimedSessions[sessID] = true
	}
	effectiveTool, parsed := unwrapHermesToolCall(d.ToolName, rawInput)
	if effectiveTool == kanbanCreateTool && createdCardID != "" {
		if short := shortCardClusterOrTitle(scrubInlineSecrets(extractArgStr(parsed, "title"))); short != "" {
			a.trace.cardTitles[createdCardID] = short
		}
	}
	if strings.HasPrefix(effectiveTool, "kanban_") && sessID != "" && sessID != a.sessionID {
		// A card id or nothing: the worker model chose this string.
		if cid := strings.TrimSpace(extractArgStr(parsed, "card_id", "task_id", "id")); kanbanCardIDExact.MatchString(cid) {
			a.trace.sessionCards[sessID] = cid
			a.trace.claimedSessions[sessID] = true
		}
	}
}

func (a *activityState) workerLabelLocked(sessionID string) string {
	sessionID = strings.TrimSpace(sessionID)
	if sessionID == "" || sessionID == a.sessionID || strings.HasPrefix(sessionID, apiSessionIDPrefix) {
		return "platform"
	}
	if cid := a.trace.sessionCards[sessionID]; cid != "" {
		if title := a.trace.cardTitles[cid]; title != "" {
			return fmt.Sprintf("card:%s (%s)", cid, title)
		}
		return "card:" + cid
	}
	if len(a.trace.cardTitles) == 1 {
		for cid, title := range a.trace.cardTitles {
			if title != "" && !strings.HasPrefix(strings.ToLower(title), "fleet-wide") {
				return fmt.Sprintf("card:%s (%s)", cid, title)
			}
			return "card:" + cid
		}
	}
	return "worker:" + capRunes(sessionID, workerLabelSessionCap)
}

func (a *activityState) observePostAPIRequestLocked(d hookDelivery) []stepmsg.Step {
	if a.sessionID != "" && (d.SessionID == "" || (d.SessionID != a.sessionID && !a.waitingCard.Load())) {
		return nil
	}
	a.trace.apiCalls++
	a.initSessionTraceLocked()
	isWorker := a.sessionID != "" && d.SessionID != "" && d.SessionID != a.sessionID
	if isWorker {
		a.observeWorkerContextLocked(d, nil, "")
	}
	steps := buildAPIRequestSteps(d, a.trace.apiCalls, a.turnCalls, a.sessionID != "", len(a.trace.plannedTitles) > 0)
	if len(steps) == 1 && steps[0].IsSynthesis() {
		if isWorker || (a.trace.turnCardsFiled && a.waitInTurn.Load()) {
			steps = nil
		}
	}
	key := stepBatchKey(steps)
	if key == "" || key == a.trace.prevBatchKey {
		return nil
	}
	a.trace.prevBatchKey = key
	for _, st := range steps {
		for _, id := range st.CallIDs {
			if len(a.trace.plannedTitles) >= maxPlannedTitles {
				clear(a.trace.plannedTitles)
			}
			a.trace.plannedTitles[id] = st.Title
		}
	}
	return steps
}

// stepBatchKey identifies one API request's step batch, so a model that
// restates the same plan is published once.
func stepBatchKey(steps []stepmsg.Step) string {
	if len(steps) == 0 {
		return ""
	}
	var sb strings.Builder
	for _, st := range steps {
		sb.WriteString(string(st.Kind))
		sb.WriteByte(':')
		sb.WriteString(st.Title)
		sb.WriteByte(':')
		sb.WriteString(strings.Join(st.CallIDs, ","))
		sb.WriteByte('\n')
	}
	return sb.String()
}

// enrichPreToolCallLocked adds the api executor's attribution to a call's
// opening entry: the step title it was planned under, the session and the
// worker or card it ran for. The entry's input is the shape observe already
// set, the same as under the cli executor; the raw arguments are read here
// only to name the title's identifier and map worker sessions to cards.
func (a *activityState) enrichPreToolCallLocked(d hookDelivery, entry *ActivityEntry) {
	if a.sessionID == "" {
		return
	}
	a.initSessionTraceLocked()
	entry.Title = a.entryTitleLocked(d.Extra.ToolCallID, d.ToolName, d.ToolInput)
	a.observeWorkerContextLocked(d, d.ToolInput, "")
	entry.SessionID = capRunes(d.SessionID, activityCallIDCap)
	entry.Worker = a.workerLabelLocked(d.SessionID)
	entry.CardID = a.trace.sessionCards[strings.TrimSpace(d.SessionID)]
}

// entryTitleLocked is the step title the call was planned under when its
// post_api_request was seen, else the title its name and arguments describe,
// so an activity entry names what it did in the same words as its step.
func (a *activityState) entryTitleLocked(callID, toolName string, rawInput json.RawMessage) string {
	if t := a.trace.plannedTitles[strings.TrimSpace(callID)]; t != "" {
		return t
	}
	titles := expandPlannedToolCall(toolName, rawInput, true, 0)
	if len(titles) == 0 {
		return ""
	}
	return capRunes(strings.Join(titles, " "), entryTitleFallbackCap)
}

// popOpenCallLocked closes the open call a post_tool_call ends and returns
// its opening entry and the raw arguments the pre_tool_call carried (kept
// in memory only, never published), or zero values when no start was seen.
func (a *activityState) popOpenCallLocked(id string, d hookDelivery) (ActivityEntry, json.RawMessage) {
	take := func(key string) (ActivityEntry, json.RawMessage) {
		oe := a.open[key]
		raw := a.openRaw[key]
		delete(a.open, key)
		delete(a.openRaw, key)
		a.openOrder = slices.DeleteFunc(a.openOrder, func(v string) bool { return v == key })
		return oe, raw
	}
	if _, seen := a.open[id]; seen {
		return take(id)
	}
	if a.sessionID != "" && d.SessionID != "" {
		fallbackTool := capRunes(d.ToolName, activityToolNameCap)
		for _, openID := range a.openOrder {
			oe := a.open[openID]
			if oe.SessionID == d.SessionID && oe.Tool == fallbackTool {
				return take(openID)
			}
		}
	}
	return ActivityEntry{}, nil
}

// enrichPostToolCallLocked adds the api executor's attribution to a call's
// closing entry: its step title, session, worker or card, and, on a
// kanban_create that made a card, the card's id. The entry's input is the
// shape observe set (the opening entry's when the end carried no input), and
// nothing of the tool's result or error text goes on it: the raw arguments
// and result are read here only for the tool name inside hermes's wrapper,
// the card id a kanban_create returned, and the worker-to-card mapping.
func (a *activityState) enrichPostToolCallLocked(d hookDelivery, openEntry ActivityEntry, openRaw json.RawMessage, status string, e *ActivityEntry) {
	if a.sessionID == "" {
		return
	}
	rawToolInput := d.ToolInput
	if len(rawToolInput) == 0 {
		rawToolInput = openRaw
	}
	effectiveTool, _ := unwrapHermesToolCall(d.ToolName, rawToolInput)
	if (d.SessionID == "" || d.SessionID == a.sessionID) && status != ActivityStatusError && (effectiveTool == kanbanCreateTool || countKanbanCreateCalls(d.ToolName, rawToolInput) > 0) {
		a.cardsFiled.Store(true)
		a.trace.turnCardsFiled = true
	}
	a.initSessionTraceLocked()
	e.Title = openEntry.Title
	if e.Title == "" {
		e.Title = a.entryTitleLocked(d.Extra.ToolCallID, d.ToolName, rawToolInput)
	}
	if len(e.Input) == 0 && len(openEntry.Input) > 0 {
		e.Input = openEntry.Input
	}
	// A failed create made no card: its result's text may name another one.
	var createdCardID string
	if status != ActivityStatusError {
		createdCardID = createdKanbanCardID(effectiveTool, d.Extra.Result)
	}
	a.observeWorkerContextLocked(d, rawToolInput, createdCardID)
	e.SessionID = capRunes(d.SessionID, activityCallIDCap)
	e.Worker = a.workerLabelLocked(d.SessionID)
	// The card an entry concerns: the one a kanban_create made, else the
	// one the calling worker session executes.
	e.CardID = createdCardID
	if e.CardID == "" {
		e.CardID = a.trace.sessionCards[strings.TrimSpace(d.SessionID)]
	}
	// The one result detail that goes on the bus: the id of the card a
	// successful kanban_create made, an identifier the task's own subtask
	// steps name again. Any other tool's output stays in the pod, and so does
	// every error message unless the maintainer's BRIDGE_DEBUG_ERROR_TEXT
	// switch is on (debugErrorText, activity.go).
	if status != ActivityStatusError && effectiveTool == kanbanCreateTool && createdCardID != "" {
		e.ResultPreview = "card_id=" + createdCardID
	}
}

func (a *activityState) hasWorkerSession(sessionID string) bool {
	if a == nil || sessionID == "" {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.trace.claimedSessions[sessionID] || a.trace.sessionCards[sessionID] != ""
}

func (a *activityState) hasCardID(cardID string) bool {
	if a == nil || cardID == "" {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.trace.cardTitles[cardID] != ""
}

// buildAPIRequestSteps reads one post_api_request delivery into the step
// records it stands for: the planned tool calls become plan steps, a stop
// after work becomes the synthesis step, and reasoning without a tool call
// becomes a step with a fixed title. The model's reasoning is read only to
// tell whether the request reasoned at all; none of its text is kept.
func buildAPIRequestSteps(d hookDelivery, fallbackStep, completedTools int, detailed bool, hadPlannedTitles bool) []stepmsg.Step {
	step := fallbackStep
	if n := durationMillis(d.Extra.APICallCount); n > 0 {
		step = int(n)
	}
	var msg hookAssistantMessage
	if len(d.Extra.AssistantMessage) > 0 && json.Unmarshal(d.Extra.AssistantMessage, &msg) != nil {
		var pyRepr string
		if json.Unmarshal(d.Extra.AssistantMessage, &pyRepr) == nil && pyRepr != "" {
			if m := pyReprReasoningRe.FindStringSubmatch(pyRepr); len(m) > 1 {
				msg.ReasoningContent = m[1]
			}
		}
	}
	if len(d.Extra.Response) > 0 {
		var resp struct {
			FinishReason     string               `json:"finish_reason"`
			AssistantMessage hookAssistantMessage `json:"assistant_message"`
		}
		if json.Unmarshal(d.Extra.Response, &resp) == nil {
			if d.Extra.FinishReason == "" {
				d.Extra.FinishReason = resp.FinishReason
			}
			if msg.Content == "" {
				msg.Content = resp.AssistantMessage.Content
			}
			if msg.ReasoningContent == "" {
				msg.ReasoningContent = resp.AssistantMessage.ReasoningContent
			}
			if msg.Reasoning == "" {
				msg.Reasoning = resp.AssistantMessage.Reasoning
			}
			if len(msg.ReasoningDetails) == 0 {
				msg.ReasoningDetails = resp.AssistantMessage.ReasoningDetails
			}
			if len(msg.AnthropicContentBlocks) == 0 {
				msg.AnthropicContentBlocks = resp.AssistantMessage.AnthropicContentBlocks
			}
			if len(msg.ToolCalls) == 0 {
				msg.ToolCalls = resp.AssistantMessage.ToolCalls
			}
		}
	}

	// hadReasoning says whether the request stated any reasoning, in any of
	// the shapes the providers use. The text itself is dropped here: a
	// model that has read a secret may restate it, and no scrubber is a
	// contract (the executor note in activity.go).
	hadReasoning := false
	noteReasoning := func(s string) {
		if strings.TrimSpace(s) != "" {
			hadReasoning = true
		}
	}

	noteReasoning(msg.ReasoningContent)
	noteReasoning(msg.Reasoning)

	if !hadReasoning && len(msg.ReasoningDetails) > 0 {
		var details []map[string]any
		if json.Unmarshal(msg.ReasoningDetails, &details) == nil {
			for _, item := range details {
				if txt, ok := item["text"].(string); ok {
					noteReasoning(txt)
				}
				if sum, ok := item["summary"].(string); ok {
					noteReasoning(sum)
				}
			}
		}
	}
	if !hadReasoning && len(msg.AnthropicContentBlocks) > 0 {
		var blocks []struct {
			Type     string `json:"type"`
			Thinking string `json:"thinking"`
		}
		if json.Unmarshal(msg.AnthropicContentBlocks, &blocks) == nil {
			for _, blk := range blocks {
				if blk.Type == "thinking" {
					noteReasoning(blk.Thinking)
				}
			}
		}
	}

	content := msg.Content
	for _, re := range []*regexp.Regexp{thinkTagPattern, scratchpadTagPattern} {
		for _, m := range re.FindAllStringSubmatch(content, -1) {
			if len(m) > 1 {
				noteReasoning(m[1])
			}
		}
		content = re.ReplaceAllString(content, "")
	}
	content = strings.TrimSpace(content)
	if content != "" && (d.Extra.FinishReason == "tool_calls" || len(msg.ToolCalls) > 0) {
		noteReasoning(content)
	}

	kanbanCreateCount := 0
	if detailed {
		for _, tc := range msg.ToolCalls {
			name := firstNonEmptyToolName(tc.Function.Name, tc.Name)
			argsRaw := tc.Function.Arguments
			if len(argsRaw) == 0 {
				argsRaw = tc.Arguments
			}
			kanbanCreateCount += countKanbanCreateCalls(name, argsRaw)
		}
	}
	// One plan step per distinct title, in plan order, each carrying the
	// ids of the tool calls it was titled from.
	var planned []stepmsg.Step
	for _, tc := range msg.ToolCalls {
		name := firstNonEmptyToolName(tc.Function.Name, tc.Name)
		argsRaw := tc.Function.Arguments
		if len(argsRaw) == 0 {
			argsRaw = tc.Arguments
		}
		callID := capBytes(strings.TrimSpace(tc.ID), activityCallIDCap)
		for _, title := range expandPlannedToolCall(name, argsRaw, detailed, kanbanCreateCount) {
			title = capBytes(strings.TrimSpace(title), maxStepTitleBytes)
			if title == "" {
				continue
			}
			idx := slices.IndexFunc(planned, func(s stepmsg.Step) bool { return s.Title == title })
			if idx < 0 {
				planned = append(planned, stepmsg.Step{Kind: stepmsg.KindPlan, Title: title})
				idx = len(planned) - 1
			}
			if callID != "" && len(planned[idx].CallIDs) < stepCallIDsCap && !slices.Contains(planned[idx].CallIDs, callID) {
				planned[idx].CallIDs = append(planned[idx].CallIDs, callID)
			}
		}
	}

	synthTitle := synthesisStepTitle
	if hadPlannedTitles || hadReasoning {
		synthTitle = apiSynthesisStepTitle
	}
	switch {
	case len(planned) > 0:
		return planned
	case hadReasoning:
		// Reasoning with no tool call: when the model stopped on turn 1 with
		// no prior tool work, nothing is published; after prior work, the
		// synthesis step; and a request that reasoned without stopping is a
		// plan step under the fixed title.
		if d.Extra.FinishReason == finishReasonStop {
			if step > 1 || completedTools > 0 {
				return []stepmsg.Step{{Kind: stepmsg.KindSynthesis, Title: synthTitle}}
			}
			return nil
		}
		return []stepmsg.Step{{Kind: stepmsg.KindPlan, Title: reasoningStepTitle}}
	case d.Extra.FinishReason == finishReasonStop && (step > 1 || completedTools > 0):
		return []stepmsg.Step{{Kind: stepmsg.KindSynthesis, Title: synthTitle}}
	default:
		return nil
	}
}

func firstNonEmptyToolName(a, b string) string {
	if s := strings.TrimSpace(a); s != "" {
		return s
	}
	return strings.TrimSpace(b)
}

func countKanbanCreateCalls(toolName string, argsRaw json.RawMessage) int {
	if toolName == kanbanCreateTool {
		return 1
	}
	if toolName == hermesToolCallWrapper {
		if calls, ok := decodeToolArgsMap(argsRaw)[wrapperCallsKey].([]any); ok {
			n := 0
			for _, item := range calls {
				if cm, ok := item.(map[string]any); ok && strings.TrimSpace(fmt.Sprint(cm[wrapperCallNameKey])) == kanbanCreateTool {
					n++
				}
			}
			return n
		}
	}
	return 0
}

func expandPlannedToolCall(toolName string, argsRaw json.RawMessage, detailed bool, kanbanCreateCount int) []string {
	toolName = capRunes(strings.TrimSpace(toolName), activityToolNameCap)
	if toolName == "" {
		return nil
	}
	describe := func(tn string, pa map[string]any) string {
		if !detailed {
			pa = nil
		} else if tn == kanbanCreateTool && kanbanCreateCount > 1 {
			return fmt.Sprintf("Dispatching %d specialist tasks on the Kanban board.", kanbanCreateCount)
		}
		return describePlannedTool(tn, pa)
	}
	parsed := decodeToolArgsMap(argsRaw)
	if toolName == hermesToolCallWrapper && parsed != nil {
		if calls, ok := parsed[wrapperCallsKey].([]any); ok && len(calls) > 0 {
			var out []string
			for _, item := range calls {
				cm, ok := item.(map[string]any)
				if !ok {
					continue
				}
				subName := capRunes(strings.TrimSpace(fmt.Sprint(cm[wrapperCallNameKey])), activityToolNameCap)
				if subName == "" || subName == "<nil>" || subName == redactedValue {
					continue
				}
				subArgs, _ := cm[wrapperCallArgsKey].(map[string]any)
				if desc := describe(subName, subArgs); desc != "" {
					out = append(out, desc)
				}
			}
			if len(out) > 0 {
				return out
			}
		}
	}
	if desc := describe(toolName, parsed); desc != "" {
		return []string{desc}
	}
	return nil
}

func decodeToolArgsMap(raw json.RawMessage) map[string]any {
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" || trimmed == "null" {
		return nil
	}
	var asString string
	if json.Unmarshal(raw, &asString) == nil {
		trimmed = strings.TrimSpace(asString)
	}
	var m map[string]any
	if json.Unmarshal([]byte(trimmed), &m) != nil {
		return nil
	}
	return m
}

func extractArgStr(m map[string]any, keys ...string) string {
	for _, k := range keys {
		if v, ok := m[k].(string); ok {
			v = strings.TrimSpace(v)
			if v != "" && v != redactedValue && !strings.HasPrefix(v, "<string,") {
				return scrubInlineSecrets(v)
			}
		}
	}
	return ""
}

func shortClusterRef(raw string) string {
	raw = strings.TrimSpace(raw)
	if idx := strings.LastIndex(raw, "/clusters/"); idx >= 0 {
		raw = raw[idx+len("/clusters/"):]
	} else if strings.HasPrefix(raw, "gke_") {
		if parts := strings.Split(raw, "_"); len(parts) >= 4 {
			raw = strings.Join(parts[3:], "_")
		}
	}
	return capRunes(raw, shortClusterRefCap)
}

func shortFilePath(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	if idx := strings.Index(raw, "/skills/"); idx >= 0 {
		return "skill " + capRunes(strings.TrimPrefix(raw[idx+len("/skills/"):], "/"), shortFilePathCap)
	}
	parts := strings.Split(strings.Trim(raw, "/"), "/")
	if len(parts) > 2 {
		parts = parts[len(parts)-2:]
	}
	return capRunes(strings.Join(parts, "/"), shortFilePathCap)
}

func shortCardClusterOrTitle(title string) string {
	title = strings.TrimSpace(title)
	lower := strings.ToLower(title)
	for _, sep := range []string{" for ", ": "} {
		if idx := strings.LastIndex(lower, sep); idx >= 0 && idx+len(sep) < len(title) {
			if cand := strings.TrimSpace(title[idx+len(sep):]); cand != "" && !strings.Contains(cand, " ") {
				return capRunes(cand, shortClusterRefCap)
			}
		}
	}
	capped := capRunes(title, shortCardTitleCap)
	if utf8.RuneCountInString(title) > shortCardTitleCap {
		if lastSpace := strings.LastIndexByte(capped, ' '); lastSpace >= len(capped)-shortCardWordBreakLookback && lastSpace > shortCardWordBreakMin {
			capped = capped[:lastSpace]
		}
	}
	for _, w := range []string{" for", " on", " in", " to", " with", " from", " by", " of", " and", " or", " the"} {
		if strings.HasSuffix(strings.ToLower(capped), w) && len(capped) > len(w)+shortCardSuffixMinPrefix {
			capped = strings.TrimRight(capped[:len(capped)-len(w)], " ,;:-")
			break
		}
	}
	return strings.TrimRight(capped, " .,;:-")
}

var staticPlannedToolTitles = map[string]string{
	"mcp__gke__list_clusters":                      "Listing GKE clusters in the fleet.",
	"mcp__platform_control__list_cluster_profiles": "Listing GKE cluster profiles in the fleet.",
	"execute_code":                                 "Running analysis script.",
}

func describePlannedTool(toolName string, parsedArgs map[string]any) string {
	if s, ok := staticPlannedToolTitles[toolName]; ok {
		return s
	}
	switch {
	case toolName == "mcp__gke__get_cluster":
		if c := shortClusterRef(extractArgStr(parsedArgs, "name", "cluster", "cluster_name")); c != "" {
			return "Inspecting GKE cluster " + c + "."
		}
		return "Inspecting GKE cluster configuration."
	case strings.HasPrefix(toolName, "mcp__gke__"):
		if c := shortClusterRef(extractArgStr(parsedArgs, "name", "cluster", "cluster_name")); c != "" {
			return "Querying GKE cluster state (" + c + ")."
		}
		return "Querying GKE cluster state."
	case strings.HasPrefix(toolName, "mcp__platform_control__"):
		if c := shortClusterRef(extractArgStr(parsedArgs, "cluster_name", "name", "cluster")); c != "" {
			return "Querying platform control state (" + c + ")."
		}
		return "Querying platform control state."
	case toolName == "terminal" || toolName == "kubectl" || toolName == "run_kubectl" || toolName == "bash":
		return describeShellTool(toolName, parsedArgs)
	case toolName == kanbanCreateTool:
		if t := shortCardClusterOrTitle(extractArgStr(parsedArgs, "title")); t != "" {
			return "Delegating specialist task: " + t + "."
		}
		return "Delegating task to specialist agent."
	case strings.HasPrefix(toolName, "kanban_"):
		return "Updating task status on the Kanban board."
	case toolName == "read_file":
		if p := shortFilePath(extractArgStr(parsedArgs, "path", "file_path", "filepath")); p != "" {
			return "Reading " + p + "."
		}
		return "Reading file in the workspace."
	case toolName == "search_files" || toolName == "find_by_name" || toolName == "list_dir" || toolName == "glob":
		if p := shortFilePath(extractArgStr(parsedArgs, "path", "pattern", "query")); p != "" {
			return "Inspecting workspace files (" + p + ")."
		}
		return "Inspecting files in the workspace."
	case toolName == "patch" || toolName == "edit_file" || toolName == "replace_in_file":
		if p := shortFilePath(extractArgStr(parsedArgs, "path", "file_path", "filepath")); p != "" {
			return "Editing " + p + "."
		}
		return "Editing a file in the agent workspace."
	case toolName == "write_file" || toolName == "create_file":
		if p := shortFilePath(extractArgStr(parsedArgs, "path", "file_path", "filepath")); p != "" {
			return "Writing " + p + "."
		}
		return "Writing a file in the agent workspace."
	case toolName == "tool_search":
		if q := extractArgStr(parsedArgs, "query", "pattern"); q != "" {
			return "Searching available tools (" + capRunes(q, toolSearchQueryCap) + ")."
		}
		return "Searching available tools."
	case toolName == "tool_describe":
		if n := extractArgStr(parsedArgs, "name", "tool_name"); n != "" {
			return "Inspecting tool schema (" + capRunes(n, toolSearchQueryCap) + ")."
		}
		return "Inspecting tool schema."
	case toolName == "skill_view":
		if s := extractArgStr(parsedArgs, "name", "skill", "skill_name"); s != "" {
			return "Loading skill " + capRunes(s, shortClusterRefCap) + "."
		}
		return "Loading skill instructions."
	default:
		return "Running " + toolName + "."
	}
}

func describeShellTool(toolName string, parsedArgs map[string]any) string {
	if parsedArgs == nil {
		if toolName == "kubectl" || toolName == "run_kubectl" {
			return "Querying Kubernetes cluster resources."
		}
		return "Running cluster and workspace diagnostics."
	}
	cmd := extractArgStr(parsedArgs, "command", "cmd")
	cluster := shortClusterRef(extractArgStr(parsedArgs, "cluster", "cluster_name", "context"))
	if cluster == "" {
		if m := cmdClusterFlagRe.FindStringSubmatch(cmd); len(m) > 1 {
			cluster = shortClusterRef(m[1])
		}
	}
	base := "Running cluster and workspace command"
	if toolName == "kubectl" || toolName == "run_kubectl" {
		base = "Querying Kubernetes cluster resources"
	} else if bin, sub := extractLeadingCommand(cmd); bin != "" {
		switch bin {
		case "kubectl", "git", "gh", "helm", "gcloud":
			if sub != "" {
				base = "Running " + bin + " " + sub
			} else {
				base = "Running " + bin + " command"
			}
		}
	}
	if cluster != "" {
		return base + " (" + cluster + ")."
	}
	return base + "."
}

func extractLeadingCommand(cmd string) (bin, sub string) {
	for _, seg := range strings.FieldsFunc(cmd, func(r rune) bool { return r == '\n' || r == ';' || r == '&' || r == '|' }) {
		fields := strings.Fields(strings.TrimSpace(seg))
		for len(fields) > 0 {
			f := fields[0]
			if strings.Contains(f, "=") || f == "sudo" || f == "timeout" {
				fields = fields[1:]
				continue
			}
			if f == "env" {
				fields = fields[1:]
				for len(fields) > 0 && strings.HasPrefix(fields[0], "-") {
					flag := fields[0]
					fields = fields[1:]
					if !strings.Contains(flag, "=") && envFlagsWithValue[flag] && len(fields) > 0 {
						fields = fields[1:]
					}
				}
				continue
			}
			break
		}
		if len(fields) == 0 {
			continue
		}
		b := strings.ToLower(fields[0])
		if slash := strings.LastIndexByte(b, '/'); slash >= 0 {
			b = b[slash+1:]
		}
		if b == "cd" || b == "export" || b == "set" || b == "echo" || b == "true" {
			continue
		}
		for i := 1; i < len(fields); i++ {
			arg := fields[i]
			if strings.HasPrefix(arg, "-") {
				if !strings.Contains(arg, "=") && shellFlagsWithValue[arg] {
					i++
				}
				continue
			}
			if s := strings.ToLower(strings.Trim(arg, "'\"")); len(s) <= leadingSubcommandCap {
				return b, s
			}
			break
		}
		return b, ""
	}
	return "", ""
}

// inlineSecretKey says whether a key seen inside text (a key=value pair, a
// --flag, a YAML entry) names a credential: the bus-wide secretLookingKey
// rule, the extra spellings text uses (pass, pwd, auth, a key file's
// extension), and the all-caps env-style suffixes.
func inlineSecretKey(k string) bool {
	return secretLookingKey(k) || inlineExtraSecretKeyPattern.MatchString(k) || allCapsSecretSuffixKeyPattern.MatchString(k)
}

// scrubInlineSecrets is the defence in depth on the little text the api
// executor does publish: the identifier a step title names (a cluster, a
// file's tail, a search query, a card's title) and a settled card's report.
// Tool arguments, results, error messages and model reasoning never reach
// it, because they are not published at all (the executor note in
// activity.go); a scrubber is a net, not a contract.
func scrubInlineSecrets(s string) string {
	if s == "" || s == redactedValue {
		return s
	}
	s = pemPrivateKeyBlockPattern.ReplaceAllString(s, redactedValue)
	s = pemPrivateKeyOpenPattern.ReplaceAllString(s, redactedValue)
	s = scrubYAMLSecretBlocks(s)
	s = inlineTokenValuePattern.ReplaceAllString(s, redactedValue)
	s = inlineAuthHeaderPattern.ReplaceAllString(s, "$1 "+redactedValue)
	s = inlineURLCredPattern.ReplaceAllString(s, "${1}"+redactedValue+"${2}")
	s = scrubCLISecretFlags(s)
	s = proseSecretPattern.ReplaceAllString(s, "${1}"+redactedValue)
	return scrubInlineKVSecrets(s)
}

func scrubCLISecretFlags(s string) string {
	s = cliUserPassPattern.ReplaceAllString(s, "${1}${2}${3}:"+redactedValue+"${5}")
	s = cliSpacedPassPattern.ReplaceAllStringFunc(s, func(m string) string {
		sub := cliSpacedPassPattern.FindStringSubmatch(m)
		if len(sub) < 3 || strings.HasPrefix(sub[2], "-") {
			return m
		}
		return sub[1] + redactedValue
	})
	s = cliAttachedPassPattern.ReplaceAllString(s, "${1}"+redactedValue)
	s = cliLongSecretFlagPattern.ReplaceAllStringFunc(s, func(m string) string {
		sub := cliLongSecretFlagPattern.FindStringSubmatch(m)
		if len(sub) < 5 || !inlineSecretKey(sub[2]) {
			return m
		}
		if sub[3] != "=" && strings.HasPrefix(sub[4], "-") {
			return m
		}
		return sub[1] + redactedValue
	})
	return s
}

func scrubYAMLSecretBlocks(s string) string {
	if !strings.Contains(s, "data:") && !strings.Contains(s, "stringData:") {
		return s
	}
	lines := strings.Split(s, "\n")
	inSecretBlock := false
	blockIndent := 0
	for i, line := range lines {
		trimmedLine := strings.TrimRight(line, "\r")
		if m := yamlSecretBlockHeaderPattern.FindStringSubmatch(trimmedLine); m != nil {
			inSecretBlock = true
			blockIndent = len(m[1])
			continue
		}
		if !inSecretBlock {
			continue
		}
		if strings.TrimSpace(trimmedLine) == "" || strings.HasPrefix(strings.TrimSpace(trimmedLine), "#") {
			continue
		}
		indent := len(trimmedLine) - len(strings.TrimLeft(trimmedLine, " \t"))
		if indent <= blockIndent {
			inSecretBlock = false
			if m := yamlSecretBlockHeaderPattern.FindStringSubmatch(trimmedLine); m != nil {
				inSecretBlock = true
				blockIndent = len(m[1])
			}
			continue
		}
		if em := yamlBlockEntryPattern.FindStringSubmatch(trimmedLine); em != nil {
			val := strings.TrimSpace(em[2])
			if val != "" && val != "{}" && val != "[]" && val != "|" && val != ">" && val != "|-" && val != ">-" {
				cr := ""
				if strings.HasSuffix(line, "\r") {
					cr = "\r"
				}
				lines[i] = em[1] + redactedValue + em[3] + cr
			}
		}
	}
	return strings.Join(lines, "\n")
}

func scrubInlineKVSecrets(s string) string {
	var sb strings.Builder
	pos := 0
	for pos < len(s) {
		loc := inlineKVSecretPattern.FindStringSubmatchIndex(s[pos:])
		if loc == nil {
			sb.WriteString(s[pos:])
			break
		}
		matchStart := pos + loc[0]
		matchEnd := pos + loc[1]
		openQuote := s[pos+loc[2] : pos+loc[3]]
		key := s[pos+loc[4] : pos+loc[5]]
		closeQuote := s[pos+loc[6] : pos+loc[7]]
		sep := s[pos+loc[10]]
		hadPreSepSpace := loc[10] > loc[8]
		hadPostSepSpace := loc[9] > loc[11]
		if !inlineSecretKey(key) {
			sb.WriteString(s[pos:matchEnd])
			pos = matchEnd
			continue
		}
		sb.WriteString(s[pos:matchEnd])
		rest := s[matchEnd:]
		if rest == "" || rest[0] == '\r' || rest[0] == '\n' {
			pos = matchEnd
			continue
		}
		if sep == '=' && !hadPreSepSpace && hadPostSepSpace && isFollowingKVAssignment(rest) {
			pos = matchEnd
			continue
		}
		if sloc := inlineAuthSchemePrefixPattern.FindStringSubmatchIndex(rest); sloc != nil {
			sb.WriteString(rest[:sloc[1]])
			matchEnd += sloc[1]
			rest = rest[sloc[1]:]
		}
		if openQuote != "" && closeQuote == "" && sep == ':' && !strings.HasPrefix(rest, `"`) && !strings.HasPrefix(rest, `'`) && !strings.HasPrefix(rest, `\"`) && !strings.HasPrefix(rest, `\'`) {
			var closeIdx int
			if len(openQuote) == 2 {
				closeIdx = findUnescapedEscapedQuote(rest, openQuote[1])
			} else {
				closeIdx = findUnescapedQuote(rest, openQuote[0])
			}
			if closeIdx >= 0 {
				sb.WriteString(redactedValue)
				pos = matchEnd + closeIdx
				continue
			}
		}
		switch {
		case strings.HasPrefix(rest, `\"`), strings.HasPrefix(rest, `\'`):
			q := rest[:2]
			afterOpen := rest[2:]
			if closeIdx := findUnescapedEscapedQuote(afterOpen, q[1]); closeIdx >= 0 {
				sb.WriteString(q + redactedValue + q)
				pos = matchEnd + 2 + closeIdx + 2
			} else {
				lineEnd := len(afterOpen)
				if nl := strings.IndexAny(afterOpen, "\r\n"); nl >= 0 {
					lineEnd = nl
				}
				sb.WriteString(q + redactedValue)
				pos = matchEnd + 2 + lineEnd
			}
		case strings.HasPrefix(rest, `"`), strings.HasPrefix(rest, `'`):
			q := rest[0]
			afterOpen := rest[1:]
			if closeIdx := findUnescapedQuote(afterOpen, q); closeIdx >= 0 {
				inner := afterOpen[:closeIdx]
				sb.WriteByte(q)
				if sloc := inlineAuthSchemePrefixPattern.FindStringSubmatchIndex(inner); sloc != nil {
					sb.WriteString(inner[:sloc[1]])
				}
				sb.WriteString(redactedValue)
				sb.WriteByte(q)
				pos = matchEnd + 1 + closeIdx + 1
			} else {
				lineEnd := len(afterOpen)
				if nl := strings.IndexAny(afterOpen, "\r\n"); nl >= 0 {
					lineEnd = nl
				}
				sb.WriteByte(q)
				sb.WriteString(redactedValue)
				pos = matchEnd + 1 + lineEnd
			}
		case strings.HasPrefix(rest, redactedValue):
			sb.WriteString(redactedValue)
			pos = matchEnd + len(redactedValue)
		default:
			if sep == ':' && openQuote == "" && closeQuote == "" && isLineLeadingKVKey(s, matchStart) {
				lineEnd := len(rest)
				if nl := strings.IndexAny(rest, "\r\n"); nl >= 0 {
					lineEnd = nl
				}
				lineRest := strings.TrimRight(rest[:lineEnd], " \t")
				if lineRest != "" && !strings.ContainsAny(lineRest, "{};") && !hasLaterInlineKVAssignment(lineRest) {
					sb.WriteString(redactedValue)
					pos = matchEnd + len(lineRest)
					continue
				}
			}
			valLen := 0
			for valLen < len(rest) {
				if strings.HasPrefix(rest[valLen:], `\"`) || strings.HasPrefix(rest[valLen:], `\'`) {
					break
				}
				c := rest[valLen]
				if c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '"' || c == '\'' || c == ',' || c == ')' || c == '}' || c == ']' || c == '>' {
					break
				}
				if c == ';' && (sep == '=' || valLen+1 >= len(rest) || rest[valLen+1] == ' ' || rest[valLen+1] == '\t') {
					break
				}
				valLen++
			}
			if valLen == 0 {
				pos = matchEnd
				continue
			}
			sb.WriteString(redactedValue)
			pos = matchEnd + valLen
		}
	}
	return sb.String()
}

func isLineLeadingKVKey(s string, matchStart int) bool {
	lineStart := 0
	if nl := strings.LastIndexByte(s[:matchStart], '\n'); nl >= 0 {
		lineStart = nl + 1
	}
	prefix := strings.TrimLeft(s[lineStart:matchStart], " \t")
	return prefix == "" || prefix == "-"
}

func hasLaterInlineKVAssignment(lineRest string) bool {
	ws := strings.IndexAny(lineRest, " \t")
	if ws < 0 {
		return false
	}
	afterFirstToken := strings.TrimLeft(lineRest[ws:], " \t")
	if loc := inlineKVSecretPattern.FindStringSubmatchIndex(afterFirstToken); loc != nil {
		return inlineSecretKey(afterFirstToken[loc[4]:loc[5]])
	}
	return false
}

func isFollowingKVAssignment(rest string) bool {
	if base64PaddingTokenPattern.MatchString(rest) {
		return false
	}
	m := leadingEqAssignmentPattern.FindStringSubmatch(rest)
	if len(m) < 2 {
		return false
	}
	candKey := m[1]
	return inlineSecretKey(candKey) || allCapsEnvKeyPattern.MatchString(candKey)
}

func findUnescapedQuote(s string, q byte) int {
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' {
			i++
			continue
		}
		if s[i] == q {
			return i
		}
	}
	return -1
}

func findUnescapedEscapedQuote(s string, q byte) int {
	for i := 1; i < len(s); i++ {
		if s[i] != q || s[i-1] != '\\' {
			continue
		}
		bs := 0
		for j := i - 1; j >= 0 && s[j] == '\\'; j-- {
			bs++
		}
		if bs%4 == 1 {
			return i - 1
		}
	}
	return -1
}

// createdKanbanCardID is the id of the card a kanban_create's result names
// (its id, card_id or task_id field, else the first card token in the raw
// result), or "" when the call made no card or was not a kanban_create. It
// is the one thing read out of a tool result: an identifier with the card
// token's shape, which the task's own subtask steps name again.
func createdKanbanCardID(toolName string, rawResult json.RawMessage) string {
	trimmed := strings.TrimSpace(string(rawResult))
	if trimmed == "" || trimmed == "null" {
		return ""
	}
	isCardID := func(s string) bool {
		s = strings.TrimSpace(s)
		return kanbanCardIDExact.MatchString(s)
	}
	var decoded any
	dec := json.NewDecoder(strings.NewReader(trimmed))
	dec.UseNumber()
	if dec.Decode(&decoded) == nil {
		if s, ok := decoded.(string); ok {
			if st := strings.TrimSpace(s); strings.HasPrefix(st, "{") {
				var inner any
				if json.Unmarshal([]byte(st), &inner) == nil {
					decoded = inner
				}
			}
		}
	}
	if m, ok := decoded.(map[string]any); ok {
		for _, k := range []string{"id", "card_id", "task_id"} {
			if v, ok := m[k].(string); ok && isCardID(v) {
				return strings.TrimSpace(v)
			}
		}
	}
	if toolName == kanbanCreateTool {
		if m := kanbanCardIDPattern.FindStringSubmatch(trimmed); len(m) > 1 {
			return m[1]
		}
	}
	return ""
}

func oneLinePreview(s string, maxBytes int) string {
	for _, line := range strings.Split(strings.TrimSpace(s), "\n") {
		if line = strings.TrimSpace(line); line != "" {
			return capRunes(strings.Join(strings.Fields(line), " "), maxBytes)
		}
	}
	return ""
}

func matchWorkerDeliveryRun(runs []*taskRun, d hookDelivery) *taskRun {
	if strings.HasPrefix(d.SessionID, apiSessionIDPrefix) {
		return nil
	}
	var waiting []*taskRun
	for _, r := range runs {
		a := r.act.Load()
		if a == nil || a.sessionID == "" {
			continue
		}
		if a.waitingCard.Load() {
			if !a.hasWorkerSession(d.SessionID) && workerSessionStartedBefore(d.SessionID, a.startedAt) {
				continue
			}
			waiting = append(waiting, r)
		}
	}
	if len(waiting) == 0 {
		return nil
	}
	slices.SortFunc(waiting, func(x, y *taskRun) int {
		ax, ay := x.act.Load(), y.act.Load()
		if ax != nil && ay != nil && !ax.startedAt.Equal(ay.startedAt) {
			if ax.startedAt.After(ay.startedAt) {
				return -1
			}
			return 1
		}
		return 0
	})
	for _, r := range waiting {
		if a := r.act.Load(); a != nil && a.hasWorkerSession(d.SessionID) {
			return r
		}
	}
	cid := extractKanbanCardIDFromCwd(d.Cwd)
	if cid == "" {
		effectiveTool, parsed := unwrapHermesToolCall(d.ToolName, d.ToolInput)
		if strings.HasPrefix(effectiveTool, "kanban_") {
			cid = extractArgStr(parsed, "card_id", "task_id", "id")
		}
	}
	if cid != "" {
		for _, r := range waiting {
			if a := r.act.Load(); a != nil && a.hasCardID(cid) {
				a.SyncKanbanCard(cid, "", d.SessionID)
				return r
			}
		}
	}
	return nil
}

func workerSessionStartedBefore(sessionID string, startedAt time.Time) bool {
	if len(sessionID) < sessionTimestampPrefixLen || sessionID[8] != '_' || startedAt.IsZero() {
		return false
	}
	ts, err := time.Parse("20060102_150405", sessionID[:sessionTimestampPrefixLen])
	if err != nil {
		return false
	}
	return ts.Before(startedAt.UTC().Add(-sessionTimestampClockSkew))
}
