package mcp

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

// StreamingStepContinuePrefix is the prefix placed at the start of a tool result
// when a task is still running and more steps remain to be streamed.
const StreamingStepContinuePrefix = "STREAMING_STEP_CONTINUE: "

var (
	inlineWhitespaceRe = regexp.MustCompile(`\s+`)
	quotedFlagArgRe    = regexp.MustCompile(`^([A-Za-z0-9_.-]+=)"([^"]*)"$`)
)

// ActivityItem is one structured tool activity entry from a task's activity artifact.
type ActivityItem struct {
	Type          string          `json:"type,omitempty"`
	Tool          string          `json:"tool"`
	Input         json.RawMessage `json:"input,omitempty"`
	CallID        string          `json:"callId,omitempty"`
	Status        string          `json:"status,omitempty"`
	ErrorType     string          `json:"errorType,omitempty"`
	ErrorMessage  string          `json:"errorMessage,omitempty"`
	ResultPreview string          `json:"resultPreview,omitempty"`
	SessionID     string          `json:"sessionId,omitempty"`
	Worker        string          `json:"worker,omitempty"`
	Title         string          `json:"title,omitempty"`
	CardID        string          `json:"cardId,omitempty"`
	DurationMs    int64           `json:"durationMs,omitempty"`
	At            string          `json:"at,omitempty"`
	Summary       string          `json:"summary,omitempty"`
	Detail        string          `json:"detail,omitempty"`
	Output        string          `json:"output,omitempty"`
	IsError       bool            `json:"isError,omitempty"`
	Agent         string          `json:"agent,omitempty"`
	StepID        string          `json:"stepId,omitempty"`
}

// StepDetail holds the enriched view of a single agent step, pairing its
// stepmsg.Step metadata with its thought and associated tool activities.
type StepDetail struct {
	Step       stepmsg.Step
	Agent      string
	Thought    string
	Activities []ActivityItem
}

// KanbanCardSummary summarizes a Kanban card subtask step.
type KanbanCardSummary struct {
	ID      string
	Title   string
	Status  string
	Verdict string
	Summary string
	Settled int
	Total   int
}

// TurnStepOutcome is the snapshot of a turn returned by WaitForTurnStep.
type TurnStepOutcome struct {
	Outcome           TurnOutcome
	Token             string
	Alias             string
	ByToken           bool
	Notices           []string
	StepDetails       []StepDetail
	TotalActivities   int
	PrevReportedSteps int
	LastReportedSteps int
	PrevReportedActs  int
	LastReportedActs  int
	Elapsed           time.Duration
	StillRunning      bool
	WaitStepTitle     string
}

type delegateRecord struct {
	Target string
	TaskID string
	Prompt string
}

// CustomToolResultEnvelope is the JSON payload returned to MCP hosts that
// render custom step titles and expandable markdown bodies.
type CustomToolResultEnvelope struct {
	CustomToolResult bool   `json:"_custom_tool_result"`
	Title            string `json:"title"`
	Markdown         string `json:"markdown"`
	Result           string `json:"result"`
}

// MarshalCustomToolResult encodes a CustomToolResultEnvelope as a JSON string.
func MarshalCustomToolResult(title, markdown, result string) string {
	b, err := json.Marshal(CustomToolResultEnvelope{
		CustomToolResult: true,
		Title:            title,
		Markdown:         markdown,
		Result:           result,
	})
	if err != nil {
		return result
	}
	return string(b)
}

func taskStepsFromTask(t *lib.Task) []stepmsg.Step {
	if t == nil {
		return nil
	}
	art := t.Artifact(stepmsg.ArtifactName)
	if art == nil {
		return nil
	}
	var out []stepmsg.Step
	for _, p := range art.Parts {
		if s, ok := stepmsg.Decode(p); ok {
			out = append(out, s)
		}
	}
	return out
}

func taskActivitiesFromTask(t *lib.Task) []ActivityItem {
	if t == nil {
		return nil
	}
	art := t.Artifact(lib.ArtifactActivity)
	if art == nil {
		return nil
	}
	var out []ActivityItem
	for _, p := range art.Parts {
		if len(p.Data) == 0 {
			continue
		}
		var item ActivityItem
		if json.Unmarshal(p.Data, &item) == nil && (item.Tool != "" || item.Title != "" || item.Summary != "") {
			out = append(out, item)
		}
	}
	return out
}

func taskThinkingFromTask(t *lib.Task) []string {
	if t == nil {
		return nil
	}
	art := t.Artifact(lib.ArtifactThinking)
	if art == nil {
		return nil
	}
	var out []string
	for _, p := range art.Parts {
		if txt := strings.TrimSpace(p.Text); txt != "" {
			out = append(out, txt)
		}
	}
	return out
}

func taskDelegatesFromTask(t *lib.Task) []delegateRecord {
	if t == nil {
		return nil
	}
	art := t.Artifact(lib.ArtifactDelegate)
	if art == nil {
		return nil
	}
	var out []delegateRecord
	for _, p := range art.Parts {
		if len(p.Data) == 0 {
			continue
		}
		var m map[string]any
		if json.Unmarshal(p.Data, &m) != nil {
			continue
		}
		target, _ := m["target"].(string)
		taskID, _ := m["taskId"].(string)
		prompt, _ := m["prompt"].(string)
		if target != "" || taskID != "" {
			out = append(out, delegateRecord{
				Target: target,
				TaskID: taskID,
				Prompt: prompt,
			})
		}
	}
	return out
}

func taskResultText(t *lib.Task) string {
	if t == nil {
		return ""
	}
	if answer := artifactText(t, lib.ArtifactResult); answer != "" {
		return answer
	}
	if t.Final && t.FinalMessage != nil {
		if msg := partsText(t.FinalMessage.Parts); msg != "" {
			return msg
		}
	}
	if turn := artifactText(t, lib.ArtifactTurn); turn == "[SILENT]" {
		return "[SILENT]"
	}
	return ""
}

// CollectTaskStepDetails pairs a single task's steps with its thinking and
// tool activity artifacts.
func CollectTaskStepDetails(ref TaskRef, t *lib.Task) ([]StepDetail, int) {
	if t == nil {
		return nil, 0
	}
	steps := taskStepsFromTask(t)
	activities := taskActivitiesFromTask(t)
	thinking := taskThinkingFromTask(t)
	agentDefault := strings.TrimSpace(ref.Addressee)
	if agentDefault == "" {
		agentDefault = "platform"
	}

	details := make([]StepDetail, len(steps))
	stepIdxByCallID := make(map[string]int, len(steps))
	var planStepIndices []int

	for i, s := range steps {
		details[i] = StepDetail{
			Step:    s,
			Agent:   agentDefault,
			Thought: strings.TrimSpace(s.Thought),
		}
		for _, cid := range s.CallIDs {
			if cid != "" {
				stepIdxByCallID[cid] = i
			}
		}
		if s.Kind == stepmsg.KindPlan || s.Kind == "" {
			planStepIndices = append(planStepIndices, i)
		}
	}

	// Pair activities to steps by CallID/StepID first, then by order among plan steps.
	unmatchedActs := make([]ActivityItem, 0, len(activities))
	for _, act := range activities {
		matched := false
		for _, id := range []string{act.CallID, act.StepID} {
			if id == "" {
				continue
			}
			if idx, ok := stepIdxByCallID[id]; ok {
				details[idx].Activities = append(details[idx].Activities, act)
				if w := strings.TrimSpace(act.Worker); w != "" {
					details[idx].Agent = w
				} else if a := strings.TrimSpace(act.Agent); a != "" {
					details[idx].Agent = a
				}
				matched = true
				break
			}
		}
		if !matched {
			unmatchedActs = append(unmatchedActs, act)
		}
	}

	if len(unmatchedActs) > 0 && len(planStepIndices) > 0 {
		actCursor := 0
		for _, stepIdx := range planStepIndices {
			if actCursor >= len(unmatchedActs) {
				break
			}
			if len(details[stepIdx].Activities) == 0 {
				act := unmatchedActs[actCursor]
				details[stepIdx].Activities = append(details[stepIdx].Activities, act)
				if w := strings.TrimSpace(act.Worker); w != "" {
					details[stepIdx].Agent = w
				} else if a := strings.TrimSpace(act.Agent); a != "" {
					details[stepIdx].Agent = a
				}
				actCursor++
			}
		}
		for actCursor < len(unmatchedActs) {
			lastPlanStep := planStepIndices[len(planStepIndices)-1]
			act := unmatchedActs[actCursor]
			details[lastPlanStep].Activities = append(details[lastPlanStep].Activities, act)
			actCursor++
		}
	}

	// Backfill thoughts from task thinking chunks if any steps lack an inline Thought.
	if len(thinking) > 0 {
		thinkCursor := 0
		for i := range details {
			if details[i].Thought != "" {
				continue
			}
			if details[i].Step.Kind == stepmsg.KindSubtask {
				continue
			}
			if thinkCursor < len(thinking) {
				details[i].Thought = thinking[thinkCursor]
				thinkCursor++
			}
		}
	}

	// Synthesize steps from raw activities/thinking if the agent emitted no step artifacts.
	if len(details) == 0 && (len(activities) > 0 || len(thinking) > 0) {
		if len(activities) > 0 {
			for i, act := range activities {
				thought := ""
				if i < len(thinking) {
					thought = thinking[i]
				}
				title := strings.TrimSpace(act.Title)
				if title == "" {
					title = strings.TrimSpace(act.Summary)
				}
				if title == "" {
					title = strings.TrimSpace(act.Tool)
				}
				agent := strings.TrimSpace(act.Worker)
				if agent == "" {
					agent = strings.TrimSpace(act.Agent)
				}
				if agent == "" {
					agent = agentDefault
				}
				var callIDs []string
				if act.CallID != "" {
					callIDs = []string{act.CallID}
				}
				details = append(details, StepDetail{
					Step: stepmsg.Step{
						Schema:  stepmsg.Schema,
						Kind:    stepmsg.KindPlan,
						Title:   title,
						Thought: thought,
						CallIDs: callIDs,
					},
					Agent:      agent,
					Thought:    thought,
					Activities: []ActivityItem{act},
				})
			}
		} else {
			for _, th := range thinking {
				details = append(details, StepDetail{
					Step: stepmsg.Step{
						Schema:  stepmsg.Schema,
						Kind:    stepmsg.KindPlan,
						Title:   summarizeThoughtTitle(th),
						Thought: th,
					},
					Agent:   agentDefault,
					Thought: th,
				})
			}
		}
	}

	return details, len(activities)
}

// CollectTurnStepDetails aggregates StepDetails across the root task and all
// followed delegated tasks in order.
func CollectTurnStepDetails(out TurnOutcome) ([]StepDetail, int) {
	var all []StepDetail
	totalActs := 0
	if out.Task != nil {
		d, acts := CollectTaskStepDetails(out.Root, out.Task)
		all = append(all, d...)
		totalActs += acts
	}
	for _, f := range out.Followed {
		if f.Task == nil {
			continue
		}
		d, acts := CollectTaskStepDetails(f.Ref, f.Task)
		all = append(all, d...)
		totalActs += acts
	}
	return all, totalActs
}

func extractKanbanCards(details []StepDetail) []KanbanCardSummary {
	byID := make(map[string]int)
	var cards []KanbanCardSummary
	for _, d := range details {
		if d.Step.Kind != stepmsg.KindSubtask || d.Step.Subtask == nil {
			continue
		}
		sub := d.Step.Subtask
		id := strings.TrimSpace(sub.ID)
		if id == "" {
			id = strings.TrimSpace(d.Step.Title)
		}
		card := KanbanCardSummary{
			ID:      id,
			Title:   strings.TrimSpace(sub.Title),
			Status:  strings.TrimSpace(sub.Status),
			Verdict: strings.TrimSpace(sub.Verdict),
			Summary: strings.TrimSpace(sub.Summary),
			Settled: sub.Settled,
			Total:   sub.Total,
		}
		if card.Title == "" {
			card.Title = strings.TrimSpace(d.Step.Title)
		}
		if idx, ok := byID[id]; ok {
			cards[idx] = card
		} else {
			byID[id] = len(cards)
			cards = append(cards, card)
		}
	}
	return cards
}

const (
	maxThoughtSentenceLen = 90
	maxThoughtTitleLen    = 80
	maxCardTitleLen       = 72
	maxStepTitleLen       = 96
	maxActivityCmdLen     = 180
	maxActivityDetailLen  = 200
	maxActivityErrLen     = 140
	maxAnswerPreviewLen   = 400
	maxWaitProgressLen    = 88
	maxTableCardTitleLen  = 60
)

func summarizeThoughtTitle(thought string) string {
	clean := collapseInlineWhitespace(thought)
	if clean == "" {
		return "Thinking"
	}
	if idx := strings.IndexAny(clean, ".!?"); idx > 0 && idx < maxThoughtSentenceLen {
		return clean[:idx+1]
	}
	return truncateInlineText(clean, maxThoughtTitleLen)
}

func collapseInlineWhitespace(s string) string {
	return strings.TrimSpace(inlineWhitespaceRe.ReplaceAllString(s, " "))
}

func sanitizeInlineBackticks(s string) string {
	return strings.ReplaceAll(collapseInlineWhitespace(s), "`", "'")
}

func sanitizeTableCell(s string) string {
	clean := sanitizeInlineBackticks(s)
	return strings.ReplaceAll(clean, "|", `\|`)
}

func sanitizeInlineItalic(s string) string {
	clean := collapseInlineWhitespace(s)
	clean = strings.ReplaceAll(clean, "*", `\*`)
	clean = strings.ReplaceAll(clean, "_", `\_`)
	return clean
}

func indentBlockquote(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	for i, line := range lines {
		lines[i] = "> " + strings.TrimRight(line, "\r")
	}
	return strings.Join(lines, "\n")
}

func wrapInlineCode(s string) string {
	clean := collapseInlineWhitespace(s)
	if clean == "" {
		return ""
	}
	if !strings.Contains(clean, "`") {
		return "`" + clean + "`"
	}
	return "`` " + strings.ReplaceAll(clean, "``", "''") + " ``"
}

func normalizeUnquotedInlineCode(s string) string {
	clean := collapseInlineWhitespace(s)
	if m := quotedFlagArgRe.FindStringSubmatch(clean); len(m) == 3 {
		if !strings.ContainsAny(m[2], " \t") {
			return m[1] + m[2]
		}
	}
	return clean
}

func truncateInlineText(s string, maxLen int) string {
	if maxLen <= 0 || len(s) <= maxLen {
		return s
	}
	if maxLen <= 3 {
		return s[:maxLen]
	}
	return strings.TrimSpace(s[:maxLen-3]) + "..."
}

func formatAgentBadge(agent string) string {
	a := strings.TrimSpace(strings.TrimPrefix(agent, "@"))
	if a == "" {
		a = "platform"
	}
	return "`@" + sanitizeInlineBackticks(a) + "`"
}

func humanizeStepTitle(d StepDetail) string {
	s := d.Step
	title := collapseInlineWhitespace(s.Title)
	if s.Kind == stepmsg.KindSubtask && s.Subtask != nil {
		cardID := strings.TrimSpace(s.Subtask.ID)
		cardTitle := collapseInlineWhitespace(s.Subtask.Title)
		if cardTitle == "" {
			cardTitle = title
		}
		status := strings.ToLower(strings.TrimSpace(s.Subtask.Status))
		verdict := strings.ToUpper(strings.TrimSpace(s.Subtask.Verdict))
		prefix := "Kanban Card"
		if cardID != "" {
			prefix = fmt.Sprintf("Kanban Card #%s", cardID)
		}
		switch status {
		case "done", "completed":
			if verdict != "" {
				return fmt.Sprintf("%s (%s): %s", prefix, verdict, truncateInlineText(cardTitle, maxCardTitleLen))
			}
			return fmt.Sprintf("%s Completed: %s", prefix, truncateInlineText(cardTitle, maxCardTitleLen))
		case "in_progress", "running", "claimed":
			return fmt.Sprintf("%s In Progress: %s", prefix, truncateInlineText(cardTitle, maxCardTitleLen))
		default:
			return fmt.Sprintf("%s Queued: %s", prefix, truncateInlineText(cardTitle, maxCardTitleLen))
		}
	}
	if title != "" && !isBareToolName(title) {
		return truncateInlineText(title, maxStepTitleLen)
	}
	if len(d.Activities) > 0 {
		act := d.Activities[0]
		actTitle := collapseInlineWhitespace(act.Title)
		if actTitle == "" {
			actTitle = collapseInlineWhitespace(act.Summary)
		}
		if actTitle != "" && !isBareToolName(actTitle) {
			return truncateInlineText(actTitle, maxStepTitleLen)
		}
		if tool := collapseInlineWhitespace(act.Tool); tool != "" {
			return fmt.Sprintf("Running %s", tool)
		}
	}
	if d.Thought != "" {
		return summarizeThoughtTitle(d.Thought)
	}
	if title != "" {
		return fmt.Sprintf("Running %s", title)
	}
	return "Working on cluster task"
}

func isBareToolName(s string) bool {
	if s == "" || strings.Contains(s, " ") {
		return false
	}
	switch strings.ToLower(s) {
	case "kubectl", "terminal", "bash", "exec", "read_file", "write_file", "search_files", "grep", "curl", "jq", "python", "python3", "sh":
		return true
	}
	return !strings.ContainsAny(s, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
}

func describeActivityForMarkdown(act ActivityItem) string {
	tool := sanitizeInlineBackticks(act.Tool)
	summary := normalizeUnquotedInlineCode(act.Title)
	if summary == "" {
		summary = normalizeUnquotedInlineCode(act.Summary)
	}
	detail := collapseInlineWhitespace(act.Detail)
	if detail == "" && act.ResultPreview != "" {
		detail = collapseInlineWhitespace(act.ResultPreview)
	}

	var cmd string
	if len(act.Input) > 0 {
		var inputMap map[string]any
		if json.Unmarshal(act.Input, &inputMap) == nil {
			for _, k := range []string{"command", "cmd", "args", "query", "path", "url"} {
				if v, ok := inputMap[k].(string); ok && strings.TrimSpace(v) != "" && !strings.HasPrefix(strings.TrimSpace(v), "<string,") {
					cmd = normalizeUnquotedInlineCode(v)
					break
				}
			}
		}
	}

	var parts []string
	switch {
	case cmd != "":
		if tool != "" && !strings.HasPrefix(strings.ToLower(cmd), strings.ToLower(tool)) {
			parts = append(parts, fmt.Sprintf("`%s`: %s", tool, wrapInlineCode(truncateInlineText(cmd, maxActivityCmdLen))))
		} else {
			parts = append(parts, wrapInlineCode(truncateInlineText(cmd, maxActivityCmdLen)))
		}
	case summary != "":
		if tool != "" && !strings.HasPrefix(strings.ToLower(summary), strings.ToLower(tool)) && !strings.Contains(strings.ToLower(summary), strings.ToLower(tool)) {
			parts = append(parts, fmt.Sprintf("`%s`: %s", tool, wrapInlineCode(truncateInlineText(summary, maxActivityCmdLen))))
		} else {
			parts = append(parts, wrapInlineCode(truncateInlineText(summary, maxActivityCmdLen)))
		}
	case tool != "":
		parts = append(parts, fmt.Sprintf("`%s`", tool))
	}

	if detail != "" && detail != summary && detail != cmd {
		parts = append(parts, truncateInlineText(detail, maxActivityDetailLen))
	}
	if act.DurationMs > 0 {
		parts = append(parts, fmt.Sprintf("*(%dms)*", act.DurationMs))
	}
	isErr := act.IsError || act.Status == "error"
	if isErr {
		parts = append(parts, "**(error)**")
	}
	errOut := collapseInlineWhitespace(act.ErrorMessage)
	if errOut == "" {
		errOut = collapseInlineWhitespace(act.Output)
	}
	if errOut != "" && isErr {
		parts = append(parts, fmt.Sprintf("— %s", wrapInlineCode(truncateInlineText(errOut, maxActivityErrLen))))
	}
	return strings.Join(parts, " ")
}

// FormatStepDetailMarkdown renders a single StepDetail's expandable markdown body.
func FormatStepDetailMarkdown(idx, total int, d StepDetail) string {
	return FormatStepDetailMarkdownWithOptions(idx, total, d, true)
}

// FormatStepDetailMarkdownWithOptions renders a single StepDetail's markdown
// body, optionally including or omitting the top heading line.
func FormatStepDetailMarkdownWithOptions(idx, total int, d StepDetail, includeHeader bool) string {
	var sb strings.Builder
	title := humanizeStepTitle(d)
	if includeHeader {
		if total > 0 {
			fmt.Fprintf(&sb, "**Step %d/%d — %s**\n\n", idx, total, title)
		} else {
			fmt.Fprintf(&sb, "**Step %d — %s**\n\n", idx, title)
		}
	}

	fmt.Fprintf(&sb, "- **Agent**: %s\n", formatAgentBadge(d.Agent))

	if d.Step.Kind == stepmsg.KindSubtask && d.Step.Subtask != nil {
		sub := d.Step.Subtask
		if sub.ID != "" {
			fmt.Fprintf(&sb, "- **Card**: `#%s` — %s\n", sanitizeInlineBackticks(sub.ID), collapseInlineWhitespace(sub.Title))
		}
		if sub.Status != "" {
			statusLine := fmt.Sprintf("`%s`", sanitizeInlineBackticks(sub.Status))
			if sub.Verdict != "" {
				statusLine += fmt.Sprintf(" (verdict: **%s**)", sanitizeInlineBackticks(sub.Verdict))
			}
			if sub.Total > 0 {
				statusLine += fmt.Sprintf(" [%d/%d settled]", sub.Settled, sub.Total)
			}
			fmt.Fprintf(&sb, "- **Card Status**: %s\n", statusLine)
		}
		if d.Thought != "" {
			fmt.Fprintf(&sb, "- **Thought**: *%s*\n", sanitizeInlineItalic(d.Thought))
		}
		if summary := strings.TrimSpace(sub.Summary); summary != "" {
			fmt.Fprintf(&sb, "- **Findings**:\n\n%s\n", indentBlockquote(summary))
		}
		return strings.TrimSpace(sb.String())
	}

	if d.Thought != "" {
		fmt.Fprintf(&sb, "- **Thought**: *%s*\n", sanitizeInlineItalic(d.Thought))
	}

	if len(d.Activities) == 1 {
		desc := describeActivityForMarkdown(d.Activities[0])
		if desc != "" {
			fmt.Fprintf(&sb, "- **Tool Activity**: %s\n", desc)
		}
	} else if len(d.Activities) > 1 {
		sb.WriteString("- **Tool Activity**:\n")
		for _, act := range d.Activities {
			if desc := describeActivityForMarkdown(act); desc != "" {
				fmt.Fprintf(&sb, "  - %s\n", desc)
			}
		}
	}

	return strings.TrimSpace(sb.String())
}

// FormatTurnCustomToolEnvelope builds the title, expandable markdown, and
// model-facing result text for a TurnStepOutcome.
func FormatTurnCustomToolEnvelope(stepOut TurnStepOutcome) (title, markdown, result string) {
	out := stepOut.Outcome
	details := stepOut.StepDetails
	totalSteps := len(details)
	prevSteps := stepOut.PrevReportedSteps
	lastSteps := stepOut.LastReportedSteps
	if lastSteps > totalSteps {
		lastSteps = totalSteps
	}
	if prevSteps > lastSteps {
		prevSteps = lastSteps
	}

	var newDetails []StepDetail
	if lastSteps > prevSteps {
		newDetails = details[prevSteps:lastSteps]
	}

	activeRef := out.Root
	activeTask := out.Task
	if out.AnswerFrom != nil && out.Answer != nil {
		activeRef = *out.AnswerFrom
		activeTask = out.Answer
	} else if len(out.Followed) > 0 {
		for i := len(out.Followed) - 1; i >= 0; i-- {
			if out.Followed[i].Task != nil {
				activeRef = out.Followed[i].Ref
				activeTask = out.Followed[i].Task
				break
			}
		}
	}

	answerText := taskResultText(activeTask)
	if answerText == "" && out.Task != nil {
		answerText = taskResultText(out.Task)
	}
	progressText := ""
	if activeTask != nil {
		progressText = artifactText(activeTask, lib.ArtifactProgress)
	}
	if progressText == "" && out.Task != nil {
		progressText = artifactText(out.Task, lib.ArtifactProgress)
	}

	cards := extractKanbanCards(details)

	// 1. Build Title & Expandable Markdown for the step card.
	switch {
	case len(newDetails) == 1:
		idx := prevSteps + 1
		d := newDetails[0]
		title = fmt.Sprintf("Step %d: %s", idx, humanizeStepTitle(d))
		markdown = FormatStepDetailMarkdownWithOptions(idx, totalSteps, d, false)
	case len(newDetails) > 1:
		firstIdx := prevSteps + 1
		lastIdx := lastSteps
		lastD := newDetails[len(newDetails)-1]
		title = fmt.Sprintf("Steps %d–%d: %s", firstIdx, lastIdx, humanizeStepTitle(lastD))
		var mdParts []string
		for i, d := range newDetails {
			stepNum := prevSteps + i + 1
			mdParts = append(mdParts, FormatStepDetailMarkdownWithOptions(stepNum, totalSteps, d, true))
		}
		markdown = strings.Join(mdParts, "\n\n---\n\n")
	case !stepOut.StillRunning:
		if totalSteps > 0 {
			lastD := details[totalSteps-1]
			title = fmt.Sprintf("Completed (%d steps): %s", totalSteps, humanizeStepTitle(lastD))
			markdown = FormatStepDetailMarkdownWithOptions(totalSteps, totalSteps, lastD, false)
		} else {
			state := "completed"
			if activeTask != nil && activeTask.State != "" {
				state = string(activeTask.State)
			}
			title = fmt.Sprintf("Task %s (%s)", out.Root, state)
			if answerText != "" {
				markdown = truncateInlineText(collapseInlineWhitespace(answerText), maxAnswerPreviewLen)
			} else {
				markdown = fmt.Sprintf("- **Agent**: %s\n- **Status**: `%s`", formatAgentBadge(activeRef.Addressee), state)
			}
		}
	default:
		waitTitle := strings.TrimSpace(stepOut.WaitStepTitle)
		if waitTitle == "" {
			if progressText != "" {
				waitTitle = truncateInlineText(collapseInlineWhitespace(lastLine(progressText)), maxWaitProgressLen)
			} else if totalSteps > 0 {
				waitTitle = humanizeStepTitle(details[totalSteps-1])
			} else {
				waitTitle = fmt.Sprintf("Waiting for @%s response", activeRef.Addressee)
			}
		}
		title = waitTitle
		var md strings.Builder
		fmt.Fprintf(&md, "- **Agent**: %s\n", formatAgentBadge(activeRef.Addressee))
		if progressText != "" {
			fmt.Fprintf(&md, "- **Progress**: *%s*\n", sanitizeInlineItalic(lastLine(progressText)))
		} else {
			fmt.Fprintf(&md, "- **Status**: Waiting for next step from `%s` (elapsed %s)\n", activeRef, stepOut.Elapsed.Round(time.Second))
		}
		markdown = strings.TrimSpace(md.String())
	}

	// If completed and there are Kanban cards, append a concise summary table to markdown.
	if !stepOut.StillRunning && len(cards) > 0 {
		var kb strings.Builder
		kb.WriteString("\n\n**Kanban Specialist Cards**:\n\n")
		kb.WriteString("| Card | Title | Status | Verdict |\n| :--- | :--- | :--- | :--- |\n")
		for _, c := range cards {
			verdict := c.Verdict
			if verdict == "" {
				verdict = "—"
			}
			fmt.Fprintf(&kb, "| `#%s` | %s | `%s` | %s |\n",
				sanitizeTableCell(c.ID),
				sanitizeTableCell(truncateInlineText(c.Title, maxTableCardTitleLen)),
				sanitizeTableCell(c.Status),
				sanitizeTableCell(verdict))
		}
		markdown = strings.TrimSpace(markdown) + kb.String()
	}

	// 2. Build the model-facing Result string.
	var res strings.Builder
	if stepOut.StillRunning {
		fmt.Fprintf(&res, "%sTask `%s` is actively running (`working`, elapsed %s).\n\n",
			StreamingStepContinuePrefix, out.Root, stepOut.Elapsed.Round(time.Second))
		writeConversation(&res, stepOut.Alias, stepOut.Token, stepOut.ByToken)
		writeNotices(&res, stepOut.Notices)
		if len(newDetails) > 0 {
			for i, d := range newDetails {
				stepNum := prevSteps + i + 1
				res.WriteString(FormatStepDetailMarkdownWithOptions(stepNum, totalSteps, d, true))
				res.WriteString("\n\n")
			}
		} else if markdown != "" {
			res.WriteString(markdown)
			res.WriteString("\n\n")
		}
		fmt.Fprintf(&res, "**NEXT ACTION REQUIRED**: Call `task_control` with `action: \"get\"`, `task: \"%s\"`, `task_id: \"%s\"`, `wait_timeout_seconds: 60`, and `toolSummary: %q` to stream the next step until the task reaches a terminal state. Do NOT respond to the user until the task finishes.\n",
			out.Root, out.Root, title)
		return title, markdown, strings.TrimSpace(res.String()) + "\n"
	}

	// Terminal result: include standard conversation, notices, task/outcome, step summary, and card reports.
	writeConversation(&res, stepOut.Alias, stepOut.Token, stepOut.ByToken)
	writeNotices(&res, stepOut.Notices)
	if len(newDetails) > 0 {
		for i, d := range newDetails {
			stepNum := prevSteps + i + 1
			res.WriteString(FormatStepDetailMarkdownWithOptions(stepNum, totalSteps, d, true))
			res.WriteString("\n\n")
		}
	}
	writeOutcome(&res, out)
	writeReports(&res, out, stepOut.Token)
	return title, markdown, res.String()
}
