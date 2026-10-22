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

// shapedStringPlaceholderRe matches the executor's stand-in for a redacted string argument
// ("<string, 12 chars>"), the one shape in a tool input this package reads rather than shows.
var shapedStringPlaceholderRe = regexp.MustCompile(`^<string,\s*(\d+)\s*chars>$`)

const (
	cardResultsRawHeader        = "Specialist card results (raw):"
	silentResultFallback        = "_The agent completed this turn without a textual reply._"
	synthFinalStepTitle         = "Synthesizing findings into final response."
	streamingStepContinuePrefix = "STREAMING_STEP_CONTINUE:"
	maxStepActivitiesShown      = 6
	maxDebugStepActivitiesShown = 12
	cardHeadlineMaxChars        = 64
	cardThoughtMaxChars         = 400
	mergedThoughtMaxChars       = 420
	stepTitleSentenceCap        = 72
	// maxRowsPerStepTitle bounds how many rows one title gets: the first repeat of a title stays
	// its own row (the turn really did come back to it), later repeats fold into the latest row
	// of that title, so a 16-cluster fan-out does not list the same diagnostic sixteen times. The
	// rule is decided per record as it arrives and never revisits earlier rows, so a step list
	// streamed to a client only ever grows.
	maxRowsPerStepTitle = 2
)

// StepDetail holds the clean high-level action title and the expandable details
// (agent/task provenance, reasoning thought, delegation details, and tool call activities)
// for a single step in a kube-agents turn.
type StepDetail struct {
	Kind           stepmsg.Kind   `json:"kind,omitempty"`
	Title          string         `json:"title"`
	Thought        string         `json:"thought,omitempty"`
	Agent          string         `json:"agent,omitempty"`
	TaskID         string         `json:"taskId,omitempty"`
	DelegatedTo    string         `json:"delegatedTo,omitempty"`
	ChildTaskID    string         `json:"childTaskId,omitempty"`
	DelegatePrompt string         `json:"delegatePrompt,omitempty"`
	Activities     []ActivityItem `json:"activities,omitempty"`
	at             time.Time
}

// isSynthesis reports whether the step is a synthesis pass: by kind when the executor said,
// by the title this package gives one it synthesizes itself.
func (d StepDetail) isSynthesis() bool {
	return d.Kind == stepmsg.KindSynthesis || sameStepTitle(d.Title, synthFinalStepTitle)
}

// CollectOutcomeSteps extracts a clean, deduplicated chronological list of human-readable
// step titles taken across the primary task, delegated child tasks, and wake synthesis task.
func CollectOutcomeSteps(outcome *TurnOutcome) []string {
	details := CollectOutcomeStepDetails(outcome)
	if len(details) == 0 {
		return nil
	}
	steps := make([]string, 0, len(details))
	for _, d := range details {
		steps = append(steps, d.Title)
	}
	return steps
}

// CollectOutcomeStepDetails extracts a clean, deduplicated chronological list of StepDetail items
// across the primary task, delegated child tasks, and wake synthesis task.
func CollectOutcomeStepDetails(outcome *TurnOutcome) []StepDetail {
	if outcome == nil {
		return nil
	}
	var rawDetails []StepDetail
	appendDetail := func(sd StepDetail) {
		sd.Title = strings.TrimSpace(sd.Title)
		if sd.Title == "" {
			return
		}
		if sd.isSynthesis() {
			for i := 0; i < len(rawDetails); i++ {
				if sameStepTitle(rawDetails[i].Title, sd.Title) {
					rawDetails = append(rawDetails[:i], rawDetails[i+1:]...)
					break
				}
			}
		}
		if len(rawDetails) > 0 {
			last := &rawDetails[len(rawDetails)-1]
			if sameStepTitle(last.Title, sd.Title) {
				if last.Thought == "" && sd.Thought != "" {
					last.Thought = sd.Thought
				} else if sd.Thought != "" {
					last.Thought = mergeThoughts(last.Thought, sd.Thought)
				}
				if last.Agent == "" && sd.Agent != "" {
					last.Agent = sd.Agent
				}
				if last.TaskID == "" && sd.TaskID != "" {
					last.TaskID = sd.TaskID
				}
				if last.DelegatedTo == "" && sd.DelegatedTo != "" {
					last.DelegatedTo = sd.DelegatedTo
				}
				if last.ChildTaskID == "" && sd.ChildTaskID != "" {
					last.ChildTaskID = sd.ChildTaskID
				}
				if last.DelegatePrompt == "" && sd.DelegatePrompt != "" {
					last.DelegatePrompt = sd.DelegatePrompt
				}
				last.Activities = mergeActivityItems(last.Activities, sd.Activities)
				return
			}
		}
		rawDetails = append(rawDetails, sd)
	}

	for _, sd := range collectRecStepDetails(outcome.PrimaryTask) {
		appendDetail(sd)
	}
	primaryID := ""
	if outcome.PrimaryTask != nil {
		primaryID = outcome.PrimaryTask.TaskID
	}
	for _, sd := range collectCardStepDetails(outcome.CardTasks, primaryID) {
		appendDetail(sd)
	}
	for idx, d := range outcome.PrimaryTaskDelegates() {
		if d.Addressee != "" {
			childID := d.ChildTaskID
			if childID == "" && idx < len(outcome.ChildTasks) && outcome.ChildTasks[idx] != nil {
				childID = outcome.ChildTasks[idx].TaskID
			}
			var agent, taskID string
			if outcome.PrimaryTask != nil {
				agent = outcome.PrimaryTask.Addressee
				taskID = outcome.PrimaryTask.TaskID
			}
			appendDetail(StepDetail{
				Title:          fmt.Sprintf("Delegating task to %s.", d.Addressee),
				Agent:          agent,
				TaskID:         taskID,
				DelegatedTo:    d.Addressee,
				ChildTaskID:    childID,
				DelegatePrompt: strings.TrimSpace(d.Text),
			})
		}
	}
	for _, child := range outcome.ChildTasks {
		for _, sd := range collectRecStepDetails(child) {
			appendDetail(sd)
		}
		for _, sd := range collectCardStepDetails(outcome.CardTasks, child.TaskID) {
			appendDetail(sd)
		}
	}
	for _, sd := range collectRecStepDetails(outcome.WakeTask) {
		appendDetail(sd)
	}

	if len(rawDetails) <= 1 {
		return rawDetails
	}
	var filtered []StepDetail
	for i, sd := range rawDetails {
		if sd.isSynthesis() {
			isLast := i == len(rawDetails)-1
			if !isLast || outcome.TimedOut {
				continue
			}
		}
		filtered = append(filtered, sd)
	}
	return filtered
}

func collectCardStepDetails(cards []*TaskRecord, parentID string) []StepDetail {
	if parentID == "" {
		return nil
	}
	var out []StepDetail
	for _, card := range cards {
		if card == nil || card.CardParentTaskID != parentID {
			continue
		}
		headline := cardHeadline(card)
		var title, thought string
		switch {
		case card.Terminal && (card.State == lib.StateFailed || card.State == lib.StateRejected || card.State == lib.StateCanceled):
			title = fmt.Sprintf("Specialist check %s: %s.", card.State, headline)
			thought = firstNonEmptyStr(card.StatusText, card.ResultText, card.ProgressText)
		case card.Terminal:
			title = fmt.Sprintf("Specialist check completed: %s.", headline)
			thought = firstNonEmptyStr(card.ResultText, card.ProgressText, card.StatusText)
		default:
			title = fmt.Sprintf("Specialist check running: %s.", headline)
			thought = firstNonEmptyStr(card.ProgressText, card.PromptText)
		}
		thought = strings.ReplaceAll(thought, "**", "")
		thought = truncateText(strings.Join(strings.Fields(thought), " "), cardThoughtMaxChars)
		out = append(out, StepDetail{
			Title:      title,
			Thought:    thought,
			Agent:      card.Addressee,
			TaskID:     card.TaskID,
			Activities: append([]ActivityItem(nil), card.Activities...),
		})
	}
	return out
}

func cardHeadline(card *TaskRecord) string {
	if card.ResultChat != nil && strings.TrimSpace(card.ResultChat.Headline) != "" {
		return cleanCardHeadline(card.ResultChat.Headline)
	}
	if card.ProgressChat != nil && strings.TrimSpace(card.ProgressChat.Headline) != "" {
		return cleanCardHeadline(card.ProgressChat.Headline)
	}
	for _, cand := range []string{card.ResultText, card.PromptText, card.ProgressText} {
		for _, line := range strings.Split(cand, "\n") {
			line = strings.TrimSpace(line)
			if line != "" {
				return cleanCardHeadline(line)
			}
		}
	}
	return card.TaskID
}

func cleanCardHeadline(s string) string {
	s = strings.TrimSpace(s)
	s = strings.Trim(s, "*_#> ")
	s = strings.ReplaceAll(s, "**", "")
	for _, sep := range []string{" — ", " - ", ": ", "; "} {
		if idx := strings.Index(s, sep); idx > 12 {
			s = s[:idx]
			break
		}
	}
	s = trimStepPeriod(s)
	return truncateAtWordBoundary(s, cardHeadlineMaxChars)
}

func trimStepPeriod(s string) string {
	s = strings.TrimSpace(s)
	if strings.HasSuffix(s, "..") {
		return s
	}
	return strings.TrimSuffix(s, ".")
}

func truncateAtWordBoundary(s string, maxLen int) string {
	s = strings.TrimSpace(s)
	if len(s) <= maxLen {
		return s
	}
	cut := s[:maxLen]
	if lastSpace := strings.LastIndexByte(cut, ' '); lastSpace >= maxLen-14 && lastSpace > 12 {
		cut = cut[:lastSpace]
	}
	cut = strings.TrimRight(cut, " .,;:-")
	return cut + ellipsisSuffix
}

func collectFullCardTitles(rec *TaskRecord) []string {
	if rec == nil {
		return nil
	}
	var out []string
	add := func(t string) {
		t = strings.TrimSpace(t)
		if t != "" && !containsString(out, t) {
			out = append(out, t)
		}
	}
	for _, sr := range rec.Steps {
		if sr.Card != nil {
			add(sr.Card.Title)
		}
	}
	for _, act := range rec.Activities {
		if act.Tool == "kanban_create" {
			args := parseActivityArgMap(act.Input)
			add(firstNonEmptyArgString(args, "title"))
		} else if act.Tool == "tool_call" {
			var wrapper struct {
				Calls []struct {
					Name      string         `json:"name"`
					Arguments map[string]any `json:"arguments"`
				} `json:"calls"`
			}
			if json.Unmarshal([]byte(act.Input), &wrapper) == nil {
				for _, c := range wrapper.Calls {
					if strings.TrimSpace(c.Name) == "kanban_create" {
						add(firstNonEmptyArgString(c.Arguments, "title"))
					}
				}
			}
		}
	}
	return out
}

func expandTruncatedCardStepTitle(title string, card *stepmsg.Card, fullTitles []string) string {
	title = strings.TrimSpace(title)
	colonIdx := strings.Index(title, ": ")
	if colonIdx < 0 {
		return title
	}
	prefix := title[:colonIdx+2]
	body := trimStepPeriod(title[colonIdx+2:])
	if len(body) < 44 {
		return title
	}
	if card != nil && strings.TrimSpace(card.Title) != "" {
		full := strings.TrimSpace(card.Title)
		if len(full) > len(body) && strings.HasPrefix(strings.ToLower(full), strings.ToLower(body)) {
			return prefix + trimStepPeriod(truncateAtWordBoundary(full, 72)) + "."
		}
	}
	for _, full := range fullTitles {
		if len(full) > len(body) && strings.HasPrefix(strings.ToLower(full), strings.ToLower(body)) {
			return prefix + trimStepPeriod(truncateAtWordBoundary(full, 72)) + "."
		}
	}
	return title
}

func mergeThoughts(existing, added string) string {
	added = strings.TrimSpace(added)
	if added == "" {
		return existing
	}
	if existing == "" {
		return added
	}
	if strings.Contains(existing, added) {
		return existing
	}
	merged := existing + " " + added
	if len(merged) <= mergedThoughtMaxChars {
		return merged
	}
	tail := merged[len(merged)-mergedThoughtMaxChars:]
	if idx := strings.Index(tail, ". "); idx >= 0 && idx < mergedThoughtMaxChars/2 {
		tail = tail[idx+2:]
	}
	return "… " + tail
}

// collectRecStepDetails lists one task's steps. An executor that publishes step records
// (stepmsg) is read structurally: each record is a row, and each activity entry is filed
// under the step that planned its call id, else under the step current at its time. A task
// without step records (a Claude worker, an older executor) gets one row per run of
// same-titled tool calls, with any reasoning text the executor published attached in time
// order.
func collectRecStepDetails(rec *TaskRecord) []StepDetail {
	if rec == nil {
		return nil
	}
	var steps []StepDetail
	if len(rec.Steps) > 0 {
		steps = stepDetailsFromRecords(rec)
	} else {
		steps = synthesizeStepDetailsFromActivities(rec)
	}
	for i := range steps {
		steps[i].Agent = rec.Addressee
		steps[i].TaskID = rec.TaskID
	}
	return steps
}

func stepDetailsFromRecords(rec *TaskRecord) []StepDetail {
	var out []StepDetail
	byCallID := make(map[string]int)
	rowsByTitle := make(map[string][]int)
	fullCardTitles := collectFullCardTitles(rec)
	for _, sr := range rec.Steps {
		title := cardStepTitleWithLookup(sr, fullCardTitles)
		if title == "" {
			continue
		}
		key := stepTitleKey(title)
		rows := rowsByTitle[key]
		idx := -1
		switch {
		case len(out) > 0 && sameStepTitle(out[len(out)-1].Title, title):
			idx = len(out) - 1
		case sr.IsSynthesis() && len(rows) > 0:
			idx = rows[len(rows)-1]
		case len(rows) >= maxRowsPerStepTitle:
			idx = rows[len(rows)-1]
		}
		if idx >= 0 {
			if sr.IsSynthesis() && idx != len(out)-1 {
				// A synthesis pass belongs at the end: the model went back to work after it.
				merged := out[idx]
				merged.Thought = mergeThoughts(merged.Thought, sr.Thought)
				out = append(out[:idx], out[idx+1:]...)
				out = append(out, merged)
				rowsByTitle = reindexRows(out)
				byCallID = reindexCallIDs(rec.Steps, out, fullCardTitles)
				idx = len(out) - 1
			} else if sr.Thought != "" {
				out[idx].Thought = mergeThoughts(out[idx].Thought, sr.Thought)
			}
		} else {
			out = append(out, StepDetail{Kind: sr.Kind, Title: title, Thought: sr.Thought, at: sr.At})
			idx = len(out) - 1
			rowsByTitle[key] = append(rowsByTitle[key], idx)
		}
		if out[idx].at.IsZero() || (!sr.At.IsZero() && sr.At.Before(out[idx].at)) {
			out[idx].at = sr.At
		}
		for _, id := range sr.CallIDs {
			if id != "" {
				byCallID[id] = idx
			}
		}
	}
	if len(out) == 0 || len(rec.Activities) == 0 {
		return out
	}
	for _, act := range rec.Activities {
		idx, ok := byCallID[act.CallID]
		if !ok {
			idx = stepIndexAtTime(out, act.At)
		}
		out[idx].Activities = mergeActivityItems(out[idx].Activities, []ActivityItem{act})
	}
	return out
}

func stepTitleKey(title string) string {
	return strings.ToLower(trimStepPeriod(title))
}

// reindexRows rebuilds the title -> row indexes after a row moved.
func reindexRows(out []StepDetail) map[string][]int {
	rows := make(map[string][]int, len(out))
	for i, d := range out {
		key := stepTitleKey(d.Title)
		rows[key] = append(rows[key], i)
	}
	return rows
}

// reindexCallIDs rebuilds the call id -> row index map after a row moved: each call id maps
// to the last row whose title its record was folded into.
func reindexCallIDs(records []StepRecord, out []StepDetail, fullCardTitles []string) map[string]int {
	byCallID := make(map[string]int)
	last := make(map[string]int, len(out))
	for i, d := range out {
		last[stepTitleKey(d.Title)] = i
	}
	for _, sr := range records {
		idx, ok := last[stepTitleKey(cardStepTitleWithLookup(sr, fullCardTitles))]
		if !ok {
			continue
		}
		for _, id := range sr.CallIDs {
			if id != "" {
				byCallID[id] = idx
			}
		}
	}
	return byCallID
}

// cardStepTitle is the row title of a step record: a card step's title carries the card's
// verdict and the board's settled/total count, which the executor states as fields.
func cardStepTitle(sr StepRecord) string {
	return cardStepTitleWithLookup(sr, nil)
}

func cardStepTitleWithLookup(sr StepRecord, fullCardTitles []string) string {
	title := expandTruncatedCardStepTitle(strings.TrimSpace(sr.Title), sr.Card, fullCardTitles)
	if title == "" || sr.Kind != stepmsg.KindCard || sr.Card == nil {
		return title
	}
	var meta []string
	if v := strings.TrimSpace(sr.Card.Verdict); v != "" {
		meta = append(meta, v)
	}
	if sr.Card.Total > 0 {
		meta = append(meta, fmt.Sprintf("%d/%d cards done", sr.Card.Settled, sr.Card.Total))
	}
	if len(meta) == 0 {
		return title
	}
	return fmt.Sprintf("%s (%s).", trimStepPeriod(title), strings.Join(meta, " — "))
}

// stepIndexAtTime is the last step that had started when the activity at `at` ran, else the
// last step.
func stepIndexAtTime(steps []StepDetail, at string) int {
	idx := len(steps) - 1
	ts, ok := parseActivityTimestamp(at)
	if !ok {
		return idx
	}
	for i := len(steps) - 1; i >= 0; i-- {
		if steps[i].at.IsZero() || !steps[i].at.After(ts) {
			return i
		}
	}
	return 0
}

func sameStepTitle(a, b string) bool {
	return strings.EqualFold(trimStepPeriod(a), trimStepPeriod(b))
}

// synthesizeStepDetailsFromActivities builds the step list of a task that published no step
// records. When the executor published reasoning text, each chunk is a row in the model's own
// words (titled by its first sentence), and each tool call files under the row current when it
// ran; without reasoning, consecutive calls with the same title share a row.
func synthesizeStepDetailsFromActivities(rec *TaskRecord) []StepDetail {
	var out []StepDetail
	for _, chunk := range rec.thinkingChunks {
		thought := strings.Join(strings.Fields(chunk.text), " ")
		if thought == "" {
			continue
		}
		title := leadingSentence(thought, stepTitleSentenceCap)
		if sameStepTitle(title, thought) {
			thought = ""
		}
		if n := len(out); n > 0 && sameStepTitle(out[n-1].Title, title) {
			out[n-1].Thought = mergeThoughts(out[n-1].Thought, thought)
			continue
		}
		out = append(out, StepDetail{Kind: stepmsg.KindPlan, Title: title, Thought: truncateText(thought, cardThoughtMaxChars), at: chunk.ts})
	}
	if len(out) == 0 {
		for _, act := range rec.Activities {
			title := activityStepTitle(act)
			if n := len(out); n > 0 && sameStepTitle(out[n-1].Title, title) {
				out[n-1].Activities = mergeActivityItems(out[n-1].Activities, []ActivityItem{act})
				continue
			}
			at, _ := parseActivityTimestamp(act.At)
			out = append(out, StepDetail{Kind: stepmsg.KindPlan, Title: title, Activities: []ActivityItem{act}, at: at})
		}
		return out
	}
	for _, act := range rec.Activities {
		idx := stepIndexAtTime(out, act.At)
		out[idx].Activities = mergeActivityItems(out[idx].Activities, []ActivityItem{act})
	}
	return out
}

// leadingSentence is the first sentence of s when it ends within limit characters, else s
// cut at limit: the title of a step an executor described only in prose.
func leadingSentence(s string, limit int) string {
	s = strings.TrimSpace(s)
	if idx := strings.Index(s, ". "); idx > 0 && idx+1 <= limit {
		return s[:idx+1]
	}
	return truncateText(s, limit)
}

// activityStepTitle is the row title one activity entry stands for: the title the executor
// planned it under when it said, else a generic title for its tool name.
func activityStepTitle(act ActivityItem) string {
	if t := strings.TrimSpace(act.Title); t != "" {
		return t
	}
	names := expandActivityToolNames(act)
	if len(names) == 0 {
		return toolNameToStepTitle(act.Tool)
	}
	return toolNameToStepTitle(names[0])
}

func mergeActivityItems(existing []ActivityItem, added []ActivityItem) []ActivityItem {
	for _, item := range added {
		dup := false
		for _, cur := range existing {
			if item.CallID != "" && cur.CallID == item.CallID {
				dup = true
				break
			}
			if item.CallID == "" && cur.Tool == item.Tool && cur.Input == item.Input && cur.DurationMs == item.DurationMs && cur.At == item.At {
				dup = true
				break
			}
		}
		if !dup {
			existing = append(existing, item)
		}
	}
	return existing
}

func expandActivityToolNames(act ActivityItem) []string {
	if act.Tool == "tool_call" && strings.TrimSpace(act.Input) != "" {
		var wrapper struct {
			Calls []struct {
				Name string `json:"name"`
			} `json:"calls"`
		}
		if json.Unmarshal([]byte(act.Input), &wrapper) == nil && len(wrapper.Calls) > 0 {
			var names []string
			for _, c := range wrapper.Calls {
				if n := strings.TrimSpace(c.Name); n != "" {
					names = append(names, n)
				}
			}
			if len(names) > 0 {
				return names
			}
		}
	}
	if act.Tool != "" {
		return []string{act.Tool}
	}
	return nil
}

// staticToolStepTitles and prefixToolStepTitles title an activity whose executor sent no
// title of its own (a Claude worker's trace, an older bridge). An executor that titles its
// calls (ActivityItem.Title) is always preferred.
var staticToolStepTitles = map[string]string{
	"mcp__gke__list_clusters":                      "Listing GKE clusters in the fleet.",
	"mcp__gke__get_cluster":                        "Inspecting GKE cluster configuration.",
	"mcp__platform_control__list_cluster_profiles": "Listing GKE cluster profiles in the fleet.",
	"read_file":          "Reading file in the workspace.",
	"search_files":       "Inspecting files in the workspace.",
	"find_by_name":       "Inspecting files in the workspace.",
	"list_dir":           "Inspecting files in the workspace.",
	"glob":               "Inspecting files in the workspace.",
	"patch":              "Editing a file in the agent workspace.",
	"edit_file":          "Editing a file in the agent workspace.",
	"replace_in_file":    "Editing a file in the agent workspace.",
	"write_file":         "Writing a file in the agent workspace.",
	"create_file":        "Writing a file in the agent workspace.",
	"terminal":           "Running cluster and workspace diagnostics.",
	"kubectl":            "Running cluster and workspace diagnostics.",
	"run_kubectl":        "Running cluster and workspace diagnostics.",
	"bash":               "Running cluster and workspace diagnostics.",
	"kanban_create":      "Delegating task to specialist agent.",
	"tool_search":        "Searching available tools.",
	"tool_describe":      "Inspecting tool schema.",
	"execute_code":       "Running analysis script.",
	"skill_view":         "Loading skill instructions.",
	"mcp__a2a__delegate": "Delegating task to platform.",
	"web_search":         "Searching the web.",
	"web_extract":        "Reading web page content.",
	"web_fetch":          "Reading web page content.",
	"read_url_content":   "Reading web page content.",
}

var prefixToolStepTitles = []struct {
	prefix string
	title  string
}{
	{"mcp__gke__", "Querying GKE cluster state."},
	{"mcp__platform_control__", "Querying platform control state."},
	{"kanban_", "Updating task status on the Kanban board."},
	{"mcp__a2a__", "Communicating over the A2A bus."},
	{"mcp__developer_knowledge__", "Searching documentation in Developer Knowledge."},
}

func toolNameToStepTitle(toolName string) string {
	if title, ok := staticToolStepTitles[toolName]; ok {
		return title
	}
	for _, p := range prefixToolStepTitles {
		if strings.HasPrefix(toolName, p.prefix) {
			return p.title
		}
	}
	if strings.HasPrefix(toolName, "mcp__") {
		parts := strings.SplitN(strings.TrimPrefix(toolName, "mcp__"), "__", 2)
		if len(parts) == 2 && parts[0] != "" && parts[1] != "" {
			return fmt.Sprintf("Calling %s (%s).", strings.ReplaceAll(parts[1], "_", " "), parts[0])
		}
	}
	return fmt.Sprintf("Running %s.", toolName)
}

// PrimaryTaskDelegates returns the delegates slice on PrimaryTask if non-nil.
func (o *TurnOutcome) PrimaryTaskDelegates() []DelegateItem {
	if o == nil || o.PrimaryTask == nil {
		return nil
	}
	return o.PrimaryTask.Delegates
}

func countCardProgress(cards []*TaskRecord) (done int, total int) {
	for _, c := range cards {
		if c == nil {
			continue
		}
		total++
		if c.Terminal {
			done++
		}
	}
	return done, total
}

// FormatTurnCustomToolEnvelope builds the Antigravity _custom_tool_result fields (title, markdown, result)
// for a turn outcome.
func FormatTurnCustomToolEnvelope(outcome *TurnOutcome, debug bool) (title string, markdown string, result string) {
	if outcome == nil {
		return "", "", ""
	}
	fullMD := FormatTurnOutcomeMarkdownWithOptions(outcome, debug)
	if outcome.PrimaryTask == nil {
		return "Executed control command", fullMD, fullMD
	}

	allDetails := CollectOutcomeStepDetails(outcome)
	sec := computeOutcomeElapsedSeconds(outcome.PrimaryTask, outcome.ChildTasks, outcome.WakeTask)
	if sec < 1 {
		sec = 1
	}

	reported := outcome.ReportedStepCount
	if reported <= 0 || reported > len(allDetails) {
		reported = len(allDetails)
	}
	prev := outcome.PrevStepCount
	if prev < 0 {
		prev = 0
	}
	if prev > reported {
		prev = reported
	}
	visibleDetails := allDetails[:reported]
	deltaDetails := visibleDetails[prev:]

	activeAgent := firstNonEmptyStr(outcome.PrimaryTask.Addressee, "platform-agent")
	activeTaskID := outcome.PrimaryTask.TaskID
	for _, c := range outcome.ChildTasks {
		if c != nil && !c.Terminal {
			activeAgent = firstNonEmptyStr(c.Addressee, activeAgent)
			activeTaskID = c.TaskID
		}
	}
	if outcome.WakeTask != nil && !outcome.WakeTask.Terminal {
		activeAgent = firstNonEmptyStr(outcome.WakeTask.Addressee, activeAgent)
		activeTaskID = outcome.WakeTask.TaskID
	}

	if outcome.TimedOut {
		var currentStep string
		var stepMD string
		deltaActs := collectDeltaActivities(outcome)
		cards, doneCards, runningCards, totalCards, latestNote := summarizeOutcomeKanbanState(outcome)

		switch {
		case len(deltaDetails) >= 1:
			lastDetail := deltaDetails[len(deltaDetails)-1]
			currentStep = trimStepPeriod(lastDetail.Title)
			title = fmt.Sprintf("%s (%ds)", currentStep, sec)
			stepMD = FormatStepDetailMarkdownWithOptions(lastDetail, sec, true, debug, outcome)
		case len(visibleDetails) > 0:
			lastDetail := visibleDetails[len(visibleDetails)-1]
			var thought string
			var lastDiagAct *ActivityItem
			for i := len(deltaActs) - 1; i >= 0; i-- {
				t := deltaActs[i].Tool
				if t == "kanban_heartbeat" {
					args := parseActivityArgMap(deltaActs[i].Input)
					if note := firstNonEmptyArgString(args, "note", "status", "message"); note != "" && !strings.HasPrefix(note, "<string,") {
						lastDiagAct = &deltaActs[i]
						break
					}
				}
				if !strings.HasPrefix(t, "kanban_") && t != "tool_search" && t != "tool_describe" {
					lastDiagAct = &deltaActs[i]
					break
				}
			}
			if lastDiagAct == nil && len(deltaActs) > 0 && totalCards == 0 {
				lastDiagAct = &deltaActs[len(deltaActs)-1]
			}
			switch {
			case lastDiagAct != nil:
				actStep := trimStepPeriod(describeActivityWaitStep(*lastDiagAct, activeAgent, cardLabels(cards)))
				if totalCards > 0 && doneCards < totalCards {
					currentStep = fmt.Sprintf("%s (%d/%d cards done)", actStep, doneCards, totalCards)
				} else {
					currentStep = actStep
				}
				if latestNote != "" {
					thought = latestNote
				} else if totalCards > 0 && doneCards < totalCards {
					thought = fmt.Sprintf("Executed %d new tool %s across %d running specialist card(s) (%d/%d completed).", len(deltaActs), pluralizeWord(len(deltaActs), "call", "calls"), runningCards, doneCards, totalCards)
				} else {
					thought = fmt.Sprintf("Executed %d new tool %s during active step.", len(deltaActs), pluralizeWord(len(deltaActs), "call", "calls"))
				}
			case totalCards > 0 && doneCards < totalCards:
				var activeClusters []string
				for _, c := range cards {
					if (c.Status == "running" || c.Status == "pending") && c.Cluster != "" && len(activeClusters) < 3 {
						activeClusters = append(activeClusters, c.Cluster)
					}
				}
				if len(activeClusters) > 0 {
					currentStep = fmt.Sprintf("Waiting for specialist check on %s (%d/%d cards done)", strings.Join(activeClusters, ", "), doneCards, totalCards)
				} else {
					currentStep = fmt.Sprintf("Specialist checks in progress (%d/%d completed, %d running)", doneCards, totalCards, runningCards)
				}
				if latestNote != "" {
					thought = latestNote
				} else {
					thought = fmt.Sprintf("Waiting for %d specialist card(s) to finish diagnostics (%d/%d completed).", runningCards, doneCards, totalCards)
				}
			default:
				currentStep = fmt.Sprintf("Waiting for %s to complete active step", activeAgent)
				if latestNote != "" {
					thought = latestNote
				} else {
					thought = fmt.Sprintf("Still executing: %s", trimStepPeriod(lastDetail.Title))
				}
			}
			title = fmt.Sprintf("%s (%ds)", currentStep, sec)
			waitDetail := StepDetail{
				Title:      currentStep,
				Agent:      activeAgent,
				TaskID:     activeTaskID,
				Thought:    thought,
				Activities: deltaActs,
			}
			stepMD = FormatStepDetailMarkdownWithOptions(waitDetail, sec, false, debug, outcome)
		default:
			currentStep = fmt.Sprintf("Starting task on %s", activeAgent)
			title = fmt.Sprintf("%s (%ds)", currentStep, sec)
			stepMD = FormatStepDetailMarkdownWithOptions(StepDetail{
				Title:      currentStep,
				Agent:      activeAgent,
				TaskID:     activeTaskID,
				Activities: deltaActs,
			}, sec, false, debug, outcome)
		}

		nextSummaryHint := currentStep
		if len(allDetails) > reported {
			nextSummaryHint = trimStepPeriod(allDetails[reported].Title)
		}

		var llm strings.Builder
		fmt.Fprintf(&llm, "%s Task `%s` is actively running on `%s` (%ds elapsed, %d %s recorded).\n",
			streamingStepContinuePrefix, outcome.PrimaryTask.TaskID, activeAgent, sec, len(visibleDetails), pluralizeStep(len(visibleDetails)))
		if len(deltaDetails) > 0 {
			fmt.Fprintf(&llm, "Latest step: %s\n", deltaDetails[len(deltaDetails)-1].Title)
		} else if len(deltaActs) > 0 {
			fmt.Fprintf(&llm, "Latest step: %s (%d new tool %s recorded)\n", currentStep, len(deltaActs), pluralizeWord(len(deltaActs), "call", "calls"))
		} else {
			llm.WriteString("No new step since the last poll: the remote agent is still inside a long-running step (remote tool calls, specialist fan-outs, and web lookups routinely take several minutes). This is normal and is NOT a stall.\n")
		}
		if done, total := countCardProgress(outcome.CardTasks); total > 0 {
			fmt.Fprintf(&llm, "Specialist cards: %d/%d completed.\n", done, total)
		} else if totalCards > 0 {
			fmt.Fprintf(&llm, "Specialist cards: %d/%d completed.\n", doneCards, totalCards)
		}
		fmt.Fprintf(&llm, "\nNEXT ACTION REQUIRED: Immediately call `mcp_kube-agents_task_control` with `action: \"get\"`, `task_id: %q`, and `toolSummary: %q` to stream the next step. Do NOT output any assistant text before calling `task_control` so that all progress steps stay in a single collapsible list. Do NOT call `list`, `steer`, `stop`, `poll_events`, `manage_session`, or read any files while this loop is active unless the user explicitly asks; just keep polling `get` until a final answer (no STREAMING_STEP_CONTINUE prefix) is returned.\n",
			outcome.PrimaryTask.TaskID, truncateText(nextSummaryHint, 48))

		return title, stepMD, strings.TrimSpace(llm.String())
	}

	answerOnlyMD := formatCleanTurnAnswerOnlyMarkdown(outcome)
	llmOut := answerOnlyMD
	if debug {
		llmOut = formatCompactDebugLLMResult(outcome, answerOnlyMD)
	}
	if prev > 0 {
		if len(deltaDetails) >= 1 {
			lastDetail := deltaDetails[len(deltaDetails)-1]
			lastStep := trimStepPeriod(lastDetail.Title)
			title = fmt.Sprintf("%s (%ds)", lastStep, sec)
			userMD := FormatStepDetailsBatchMarkdownWithOptions(deltaDetails, sec, true, debug, outcome)
			if debug {
				userMD = fullMD
			}
			return title, userMD, llmOut
		}
		title = fmt.Sprintf("Completed in %ds (%d %s)", sec, len(allDetails), pluralizeStep(len(allDetails)))
		userMD := fmt.Sprintf("- **Agent**: `%s` (task `%s`)\n- **Status**: ✅ Completed in %ds (%d %s)",
			activeAgent, activeTaskID, sec, len(allDetails), pluralizeStep(len(allDetails)))
		if debug {
			userMD = fullMD
		}
		return title, userMD, llmOut
	}

	if len(allDetails) >= 1 {
		lastDetail := allDetails[len(allDetails)-1]
		lastStep := trimStepPeriod(lastDetail.Title)
		title = fmt.Sprintf("%s (%ds)", lastStep, sec)
		userMD := FormatStepDetailsBatchMarkdownWithOptions(allDetails, sec, true, debug, outcome)
		if debug {
			if len(allDetails) > 1 {
				title = fmt.Sprintf("Completed in %ds (%d %s)", sec, len(allDetails), pluralizeStep(len(allDetails)))
			}
			userMD = fullMD
		}
		return title, userMD, llmOut
	}
	title = fmt.Sprintf("Completed in %ds", sec)
	userMD := fmt.Sprintf("- **Agent**: `%s` (task `%s`)\n- **Status**: ✅ Task completed in %ds", activeAgent, activeTaskID, sec)
	if debug {
		userMD = fullMD
	}
	return title, userMD, llmOut
}

// FormatStepDetailsBatchMarkdown renders the expandable markdown body for one or more StepDetails.
func FormatStepDetailsBatchMarkdown(details []StepDetail, elapsedSec int, completed bool) string {
	return FormatStepDetailsBatchMarkdownWithOptions(details, elapsedSec, completed, false, nil)
}

// FormatStepDetailsBatchMarkdownWithOptions renders the expandable markdown body for one or more StepDetails.
func FormatStepDetailsBatchMarkdownWithOptions(details []StepDetail, elapsedSec int, completed bool, debug bool, outcome *TurnOutcome) string {
	if len(details) == 0 {
		return ""
	}
	if len(details) == 1 {
		return FormatStepDetailMarkdownWithOptions(details[0], elapsedSec, completed, debug, outcome)
	}
	var parts []string
	for _, d := range details {
		header := fmt.Sprintf("**%s**", trimStepPeriod(d.Title))
		parts = append(parts, header+"\n"+FormatStepDetailMarkdownWithOptions(d, elapsedSec, completed, debug, outcome))
	}
	return strings.Join(parts, "\n\n")
}

// FormatStepDetailMarkdown renders the rich expandable markdown body for a single step row.
func FormatStepDetailMarkdown(detail StepDetail, elapsedSec int, completed bool) string {
	return FormatStepDetailMarkdownWithOptions(detail, elapsedSec, completed, false, nil)
}

// FormatStepDetailMarkdownWithOptions renders the expandable markdown body for a single step row,
// adding bus envelope & worker/session telemetry, unshaped arguments, and tool result previews when debug is true.
func FormatStepDetailMarkdownWithOptions(detail StepDetail, elapsedSec int, completed bool, debug bool, outcome *TurnOutcome) string {
	var sb strings.Builder
	stepTitleClean := trimStepPeriod(detail.Title)

	if detail.Agent != "" && detail.TaskID != "" {
		if debug && outcome != nil && outcome.PrimaryTask != nil && outcome.PrimaryTask.ContextID != "" {
			sessionID := ""
			for _, a := range detail.Activities {
				if a.SessionID != "" {
					sessionID = a.SessionID
					break
				}
			}
			if sessionID != "" {
				fmt.Fprintf(&sb, "- **Agent**: `%s` (task `%s`, context `%s`, session `%s`)\n",
					detail.Agent, detail.TaskID, outcome.PrimaryTask.ContextID, sessionID)
			} else {
				fmt.Fprintf(&sb, "- **Agent**: `%s` (task `%s`, context `%s`)\n",
					detail.Agent, detail.TaskID, outcome.PrimaryTask.ContextID)
			}
		} else {
			fmt.Fprintf(&sb, "- **Agent**: `%s` (task `%s`)\n", detail.Agent, detail.TaskID)
		}
	} else if detail.Agent != "" {
		fmt.Fprintf(&sb, "- **Agent**: `%s`\n", detail.Agent)
	}

	if debug && outcome != nil && outcome.PrimaryTask != nil {
		rec := outcome.PrimaryTask
		if rec.EnvelopeCount > 0 {
			breakdown := formatEnvelopeKindBreakdown(rec.EnvelopeCountsByKind)
			deltaActs := len(collectDeltaActivities(outcome))
			if breakdown != "" {
				fmt.Fprintf(&sb, "- **Bus Envelopes**: `%d` folded (%s) | `+%d` new tool %s\n",
					rec.EnvelopeCount, breakdown, deltaActs, pluralizeWord(deltaActs, "call", "calls"))
			} else {
				fmt.Fprintf(&sb, "- **Bus Envelopes**: `%d` folded | `+%d` new tool %s\n",
					rec.EnvelopeCount, deltaActs, pluralizeWord(deltaActs, "call", "calls"))
			}
		}
	}

	if detail.DelegatedTo != "" {
		if detail.ChildTaskID != "" {
			fmt.Fprintf(&sb, "- **Delegated To**: `%s` (child task `%s`)\n", detail.DelegatedTo, detail.ChildTaskID)
		} else {
			fmt.Fprintf(&sb, "- **Delegated To**: `%s`\n", detail.DelegatedTo)
		}
	}

	if detail.Thought != "" {
		thoughtSingle := strings.Join(strings.Fields(detail.Thought), " ")
		fmt.Fprintf(&sb, "- **Thought**: *%s*\n", thoughtSingle)
	}
	if detail.DelegatePrompt != "" {
		promptSingle := strings.Join(strings.Fields(detail.DelegatePrompt), " ")
		fmt.Fprintf(&sb, "- **Delegation Request**: *%s*\n", truncateText(promptSingle, 280))
	}

	if len(detail.Activities) > 0 {
		sb.WriteString("- **Tool Activity**:\n")
		for _, line := range formatStepActivitiesListWithOptions(detail.Activities, stepTitleClean, debug) {
			fmt.Fprintf(&sb, "  - %s\n", line)
		}
	} else if detail.DelegatedTo == "" && detail.Thought == "" {
		if strings.EqualFold(stepTitleClean, strings.TrimSuffix(synthFinalStepTitle, ".")) {
			fmt.Fprintf(&sb, "- **Action**: *Synthesizing tool outputs and cluster telemetry into final response* _(%ds elapsed)_\n", elapsedSec)
		} else if completed {
			fmt.Fprintf(&sb, "- **Action**: *%s* _(%ds elapsed)_\n", stepTitleClean, elapsedSec)
		} else {
			fmt.Fprintf(&sb, "- **Action**: *%s...* _(%ds elapsed)_\n", stepTitleClean, elapsedSec)
		}
	}

	return strings.TrimSpace(sb.String())
}
