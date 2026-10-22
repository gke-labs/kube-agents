package mcp

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// KanbanCardSummary represents a specialist Kanban card observed via tool calls or thinking updates.
type KanbanCardSummary struct {
	ID        string
	Title     string
	Cluster   string
	Worker    string
	SessionID string
	Status    string
	Verdict   string
	Summary   string
}

func collectOutcomeActivities(outcome *TurnOutcome) []ActivityItem {
	if outcome == nil {
		return nil
	}
	var out []ActivityItem
	if outcome.PrimaryTask != nil {
		out = append(out, outcome.PrimaryTask.Activities...)
	}
	for _, c := range outcome.ChildTasks {
		if c != nil {
			out = append(out, c.Activities...)
		}
	}
	for _, c := range outcome.CardTasks {
		if c != nil {
			out = append(out, c.Activities...)
		}
	}
	if outcome.WakeTask != nil {
		out = append(out, outcome.WakeTask.Activities...)
	}
	return out
}

func collectDeltaActivities(outcome *TurnOutcome) []ActivityItem {
	all := collectOutcomeActivities(outcome)
	if len(all) == 0 {
		return nil
	}
	reported := outcome.ReportedActivityCount
	if reported <= 0 || reported > len(all) {
		reported = len(all)
	}
	prev := outcome.PrevActivityCount
	if prev < 0 {
		prev = 0
	}
	if prev > reported {
		prev = reported
	}
	return all[prev:reported]
}

func parseActivityArgMap(raw string) map[string]any {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" || !strings.HasPrefix(trimmed, "{") {
		return nil
	}
	var m map[string]any
	if json.Unmarshal([]byte(trimmed), &m) != nil {
		return nil
	}
	return m
}

func firstNonEmptyArgString(m map[string]any, keys ...string) string {
	if len(m) == 0 {
		return ""
	}
	for _, k := range keys {
		if v, ok := m[k]; ok {
			if s, ok := v.(string); ok {
				s = strings.TrimSpace(s)
				if s != "" && !strings.HasPrefix(s, "<string,") && s != "[redacted]" {
					return s
				}
			}
		}
	}
	return ""
}

func summarizeKanbanMetadata(args map[string]any) string {
	meta, ok := args["metadata"].(map[string]any)
	if !ok || len(meta) == 0 {
		return ""
	}
	return formatArgMapSummaryWithLimit(meta, 80)
}

func formatActivityAtTime(at string) string {
	at = strings.TrimSpace(at)
	if at == "" {
		return ""
	}
	if t, err := time.Parse(time.RFC3339Nano, at); err == nil {
		return t.UTC().Format("15:04:05")
	}
	if t, err := time.Parse(time.RFC3339, at); err == nil {
		return t.UTC().Format("15:04:05")
	}
	return at
}

// kanbanCardArgID is the card a kanban_* call names in its arguments.
func kanbanCardArgID(args map[string]any) string {
	return firstNonEmptyArgString(args, "task_id", "card_id", "id")
}

func isSettledCardStatus(status, verdict string) (settled bool, failed bool) {
	switch strings.ToLower(strings.TrimSpace(firstNonEmptyStr(status, verdict))) {
	case "failed", "gave_up", "canceled", "cancelled", "error":
		return true, true
	case "done", "archived", "completed", "blocked", "healthy", "ok", "success", "warning", "degraded", "unhealthy":
		return true, false
	}
	return status != "" || verdict != "", false
}

// extractKanbanCardsFromTask reads the specialist cards a task fanned out, from the facts the
// executor states: a kanban_create call names the card it made (ActivityItem.CardID, with the
// title and assignee from its arguments), a worker's calls carry the card they execute, a
// kanban_done/kanban_fail call settles the card it names, and the executor's card step
// records (stepmsg.KindCard) carry the verdict, summary, and the board's settled/total count.
func extractKanbanCardsFromTask(rec *TaskRecord) (cards []KanbanCardSummary, doneCount int, runningCount int, totalCount int, latestNote string) {
	if rec == nil {
		return nil, 0, 0, 0, ""
	}
	byID := make(map[string]*KanbanCardSummary)
	var order []string
	ensureCard := func(id string) *KanbanCardSummary {
		id = strings.TrimSpace(id)
		if id == "" {
			return nil
		}
		if c, ok := byID[id]; ok {
			return c
		}
		c := &KanbanCardSummary{ID: id, Status: "pending"}
		byID[id] = c
		order = append(order, id)
		return c
	}
	markRunning := func(c *KanbanCardSummary, act ActivityItem) {
		if c.Status == "pending" {
			c.Status = "running"
		}
		if act.SessionID != "" {
			c.SessionID = act.SessionID
		}
	}

	var anonCreateIdx int
	for _, act := range rec.Activities {
		args := parseActivityArgMap(act.Input)
		switch act.Tool {
		case "kanban_create":
			cardID := firstNonEmptyStr(act.CardID, kanbanCardArgID(args))
			if cardID == "" {
				anonCreateIdx++
				cardID = fmt.Sprintf("card_%d", anonCreateIdx)
			}
			if c := ensureCard(cardID); c != nil {
				if t := firstNonEmptyArgString(args, "title"); t != "" {
					c.Title = t
				}
				if as := firstNonEmptyArgString(args, "assignee"); as != "" && c.Worker == "" {
					c.Worker = as
				}
				if cl := firstNonEmptyArgString(args, "cluster", "cluster_name"); cl != "" {
					c.Cluster = cl
				}
			}
		case "kanban_claim":
			if c := ensureCard(firstNonEmptyStr(kanbanCardArgID(args), act.CardID)); c != nil {
				markRunning(c, act)
				if act.Worker != "" {
					c.Worker = act.Worker
				}
			}
		case "kanban_heartbeat":
			cardID := firstNonEmptyStr(kanbanCardArgID(args), act.CardID)
			note := firstNonEmptyArgString(args, "note", "status", "message")
			if note != "" {
				latestNote = note
				if cardID != "" {
					latestNote = fmt.Sprintf("Card %s: %s", cardID, note)
				}
			}
			if c := ensureCard(cardID); c != nil {
				markRunning(c, act)
				if note != "" && c.Summary == "" {
					c.Summary = note
				}
			}
		case "kanban_done", "kanban_complete":
			cardID := firstNonEmptyStr(kanbanCardArgID(args), act.CardID)
			var c *KanbanCardSummary
			if cardID != "" {
				c = ensureCard(cardID)
			} else {
				// An unnamed completion settles the calling worker's card, else the oldest open one.
				for _, id := range order {
					cand := byID[id]
					if cand.Status == "done" || cand.Status == "failed" {
						continue
					}
					if act.SessionID != "" && cand.SessionID == act.SessionID {
						c = cand
						break
					}
					if c == nil {
						c = cand
					}
				}
			}
			if c != nil {
				c.Status = "done"
				if act.SessionID != "" {
					c.SessionID = act.SessionID
				}
				if v := firstNonEmptyArgString(args, "verdict", "status"); v != "" {
					c.Verdict = v
				}
				if sum := firstNonEmptyArgString(args, "summary", "result", "note"); sum != "" {
					c.Summary = sum
				} else if c.Summary == "" {
					c.Summary = summarizeKanbanMetadata(args)
				}
			}
		case "kanban_fail":
			if c := ensureCard(firstNonEmptyStr(kanbanCardArgID(args), act.CardID)); c != nil {
				c.Status, c.Verdict = "failed", "failed"
				if act.SessionID != "" {
					c.SessionID = act.SessionID
				}
				if r := firstNonEmptyArgString(args, "reason", "error", "summary"); r != "" {
					c.Summary = r
				}
			}
		default:
			if act.CardID != "" && isWorkerSessionID(act.SessionID) {
				if c := ensureCard(act.CardID); c != nil {
					markRunning(c, act)
				}
			}
		}
	}

	// The executor's own word on each settled card, and the board's count at that moment.
	var boardSettled, boardTotal int
	for _, sr := range rec.Steps {
		if sr.Card == nil || sr.Card.ID == "" {
			continue
		}
		c := ensureCard(sr.Card.ID)
		if c.Title == "" {
			c.Title = sr.Card.Title
		}
		if c.Worker == "" {
			c.Worker = sr.Card.Assignee
		}
		if sr.Card.Verdict != "" {
			c.Verdict = sr.Card.Verdict
		}
		if sr.Card.Summary != "" {
			c.Summary = sr.Card.Summary
		} else if sr.Thought != "" {
			c.Summary = sr.Thought
		}
		if settled, failed := isSettledCardStatus(sr.Card.Status, sr.Card.Verdict); settled {
			c.Status = "done"
			if failed {
				c.Status = "failed"
			}
		}
		if sr.Card.Total > boardTotal || (sr.Card.Total == boardTotal && sr.Card.Settled > boardSettled) {
			boardSettled, boardTotal = sr.Card.Settled, sr.Card.Total
		}
	}

	// A kanban_create whose card id was not observed stands for the card the executor later
	// named with the same title, else for the next unnamed card in creation order.
	var realIDs, anonIDs []string
	for _, id := range order {
		if strings.HasPrefix(id, "card_") {
			anonIDs = append(anonIDs, id)
		} else {
			realIDs = append(realIDs, id)
		}
	}
	if len(realIDs) > 0 && len(anonIDs) > 0 {
		usedAnon := make(map[string]bool)
		claim := func(rc, ac *KanbanCardSummary, anonID string) {
			usedAnon[anonID] = true
			if rc.Title == "" {
				rc.Title = ac.Title
			}
			if rc.Cluster == "" {
				rc.Cluster = ac.Cluster
			}
			if rc.Worker == "" {
				rc.Worker = ac.Worker
			}
		}
		for _, realID := range realIDs {
			rc := byID[realID]
			for _, anonID := range anonIDs {
				if ac := byID[anonID]; !usedAnon[anonID] && ac.Title != "" && strings.EqualFold(ac.Title, rc.Title) {
					claim(rc, ac, anonID)
					break
				}
			}
		}
		next := 0
		for _, realID := range realIDs {
			rc := byID[realID]
			if rc.Title != "" {
				continue
			}
			for next < len(anonIDs) && usedAnon[anonIDs[next]] {
				next++
			}
			if next < len(anonIDs) {
				claim(rc, byID[anonIDs[next]], anonIDs[next])
			}
		}
		var kept []string
		for _, id := range order {
			if !usedAnon[id] {
				kept = append(kept, id)
			}
		}
		order = kept
	}

	for _, id := range order {
		c := byID[id]
		if rec.Terminal && rec.State == lib.StateCompleted && c.Status != "done" && c.Status != "failed" {
			c.Status = "done"
		}
		totalCount++
		if c.Status == "done" || c.Status == "failed" {
			doneCount++
		} else {
			runningCount++
		}
		cards = append(cards, *c)
	}
	if boardTotal > totalCount || (boardTotal == totalCount && boardSettled > doneCount) {
		doneCount, totalCount = boardSettled, boardTotal
		runningCount = totalCount - doneCount
	}
	return cards, doneCount, runningCount, totalCount, latestNote
}

func summarizeOutcomeKanbanState(outcome *TurnOutcome) (cards []KanbanCardSummary, doneCount int, runningCount int, totalCount int, latestNote string) {
	if outcome == nil {
		return nil, 0, 0, 0, ""
	}
	if outcome.PrimaryTask != nil {
		cards, doneCount, runningCount, totalCount, latestNote = extractKanbanCardsFromTask(outcome.PrimaryTask)
	}
	if d, t := countCardProgress(outcome.CardTasks); t > totalCount {
		doneCount = d
		totalCount = t
		runningCount = t - d
	}
	return cards, doneCount, runningCount, totalCount, latestNote
}

// cardLabels maps each card id to the short name a step title qualifies with: the cluster the
// card was created for, else its title.
func cardLabels(cards []KanbanCardSummary) map[string]string {
	if len(cards) == 0 {
		return nil
	}
	labels := make(map[string]string, len(cards))
	for _, c := range cards {
		if name := firstNonEmptyStr(c.Cluster, c.Title); name != "" {
			labels[c.ID] = truncateAtWordBoundary(name, 64)
		}
	}
	return labels
}

func shortActivityPath(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	if idx := strings.Index(raw, "/skills/"); idx >= 0 {
		return "skill " + truncateText(strings.TrimPrefix(raw[idx+len("/skills/"):], "/"), 48)
	}
	parts := strings.Split(strings.Trim(raw, "/"), "/")
	if len(parts) > 2 {
		parts = parts[len(parts)-2:]
	}
	return truncateText(strings.Join(parts, "/"), 48)
}

func qualifyWithCluster(base, prep, cluster string) string {
	if cluster == "" || strings.Contains(strings.ToLower(base), strings.ToLower(cluster)) {
		return base
	}
	return fmt.Sprintf("%s %s %s", base, prep, cluster)
}

// describeActivityWaitStep titles the step a still-running turn is in, from its latest tool
// call: the title the executor planned the call under, qualified with the cluster the call
// names or, failing that, the card it ran for. When nothing specific can be derived it falls
// back to a generic waiting line; callers that only want a specific title use
// specificActivityWaitStep so they never have to recognise that fallback by its wording.
func describeActivityWaitStep(act ActivityItem, activeAgent string, labels map[string]string) string {
	if title, ok := specificActivityWaitStep(act, labels); ok {
		return title
	}
	return fmt.Sprintf("Waiting for %s to complete active step", activeAgent)
}

// specificActivityWaitStep returns the step title derivable from the activity's own facts
// (planned title, note, path, tool name) and false when there is none.
func specificActivityWaitStep(act ActivityItem, labels map[string]string) (string, bool) {
	args := parseActivityArgMap(act.Input)
	qualifier, prep := firstNonEmptyArgString(args, "cluster", "cluster_name"), "on"
	if qualifier == "" {
		if label := labels[act.CardID]; label != "" {
			qualifier, prep = label, "for"
		}
	}
	toolName := act.Tool
	if toolName == "tool_call" {
		if names := expandActivityToolNames(act); len(names) > 0 {
			toolName = names[0]
		}
	}
	if toolName == "kanban_heartbeat" {
		if note := firstNonEmptyArgString(args, "note", "status", "message"); note != "" {
			return truncateAtWordBoundary(trimStepPeriod(note), 64), true
		}
		return qualifyWithCluster("Updating specialist progress", prep, qualifier), true
	}
	if t := trimStepPeriod(act.Title); t != "" {
		if strings.HasSuffix(t, ")") {
			// The executor already qualified the title (a cluster, a path).
			return t, true
		}
		return qualifyWithCluster(t, prep, qualifier), true
	}
	if p := shortActivityPath(firstNonEmptyArgString(args, "path", "file_path", "filepath")); p != "" {
		switch toolName {
		case "read_file":
			return qualifyWithCluster("Reading "+p, prep, qualifier), true
		case "patch", "edit_file", "replace_in_file":
			return qualifyWithCluster("Editing "+p, "for", qualifier), true
		case "write_file", "create_file":
			return qualifyWithCluster("Writing "+p, "for", qualifier), true
		}
	}
	if title := toolNameToStepTitle(toolName); title != "" {
		return qualifyWithCluster(trimStepPeriod(title), prep, qualifier), true
	}
	return "", false
}

func formatEnvelopeKindBreakdown(counts map[string]int) string {
	if len(counts) == 0 {
		return ""
	}
	keys := make([]string, 0, len(counts))
	for k, v := range counts {
		if v > 0 {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	var parts []string
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("`%s=%d`", k, counts[k]))
	}
	return strings.Join(parts, ", ")
}

func formatStepActivitiesListWithOptions(activities []ActivityItem, stepTitleClean string, debug bool) []string {
	var lines []string
	maxShown := maxStepActivitiesShown
	if debug {
		maxShown = maxDebugStepActivitiesShown
	}
	limit := len(activities)
	if limit > maxShown {
		limit = maxShown - 1
	}
	for i := 0; i < limit; i++ {
		lines = append(lines, formatSingleActivityLineWithOptions(activities[i], stepTitleClean, debug))
	}
	if len(activities) > limit {
		remaining := len(activities) - limit
		var remMs int64
		for i := limit; i < len(activities); i++ {
			remMs += activities[i].DurationMs
		}
		if remMs > 0 {
			lines = append(lines, fmt.Sprintf("*(+ %d more tool %s, %s total)*", remaining, pluralizeWord(remaining, "call", "calls"), formatDurationMs(remMs)))
		} else {
			lines = append(lines, fmt.Sprintf("*(+ %d more tool %s)*", remaining, pluralizeWord(remaining, "call", "calls")))
		}
	}
	return lines
}

func formatSingleActivityLineWithOptions(act ActivityItem, stepTitleClean string, debug bool) string {
	maxValChars := 64
	if debug {
		maxValChars = 240
	}
	toolDisplay := fmt.Sprintf("**`%s`**", act.Tool)
	argSummary := summarizeActivityInputWithLimit(act.Tool, act.Input, maxValChars)

	if act.Tool == "tool_call" {
		nestedDisplay, nestedArgs := summarizeBatchWrapperCallWithLimit(act.Input, maxValChars)
		if nestedDisplay != "" {
			toolDisplay = nestedDisplay
			argSummary = nestedArgs
		}
	}

	statusIcon := "✅"
	statusWord := firstNonEmptyStr(act.Status, "completed")
	switch statusWord {
	case "ok", "completed":
		statusIcon = "✅"
		statusWord = "completed"
	case "error", "failed":
		statusIcon = "❌"
		if act.ErrorType != "" && act.ErrorType != statusWord {
			statusWord = fmt.Sprintf("error: %s", act.ErrorType)
		}
	case "interrupted":
		statusIcon = "⚠️"
	default:
		statusIcon = "🔄"
	}

	timingPart := fmt.Sprintf("%s %s", statusIcon, statusWord)
	if act.DurationMs > 0 {
		timingPart = fmt.Sprintf("%s %s in %s", statusIcon, statusWord, formatDurationMs(act.DurationMs))
	}

	prefixBadge := "🛠️"
	if debug {
		var metaParts []string
		if act.Index > 0 {
			metaParts = append(metaParts, fmt.Sprintf("`#%d`", act.Index))
		}
		if tStr := formatActivityAtTime(act.At); tStr != "" {
			metaParts = append(metaParts, fmt.Sprintf("`%s`", tStr))
		}
		worker := strings.TrimSpace(act.Worker)
		if worker == "" {
			worker = "platform"
		}
		metaParts = append(metaParts, fmt.Sprintf("`[%s]`", worker))
		prefixBadge = "🛠️ " + strings.Join(metaParts, " ")
	} else if act.Worker != "" && act.Worker != "platform" {
		prefixBadge = fmt.Sprintf("🛠️ `[%s]`", act.Worker)
	}

	resultSuffix := ""
	if debug {
		if act.ErrorMessage != "" {
			resultSuffix = fmt.Sprintf(" → ❌ `%s`", truncateText(strings.ReplaceAll(act.ErrorMessage, "\n", " "), 180))
		} else if act.ResultPreview != "" {
			resultSuffix = fmt.Sprintf(" → `%s`", truncateText(strings.ReplaceAll(act.ResultPreview, "\n", " "), 180))
		}
	}

	desc := trimStepPeriod(activityStepTitle(act))
	if argSummary != "" {
		return fmt.Sprintf("%s %s — %s *(%s)*%s", prefixBadge, toolDisplay, argSummary, timingPart, resultSuffix)
	}
	if desc != "" && !strings.EqualFold(desc, "Running "+act.Tool) {
		return fmt.Sprintf("%s %s — %s *(%s)*%s", prefixBadge, toolDisplay, desc, timingPart, resultSuffix)
	}
	if stepTitleClean != "" {
		return fmt.Sprintf("%s %s — %s *(%s)*%s", prefixBadge, toolDisplay, stepTitleClean, timingPart, resultSuffix)
	}
	return fmt.Sprintf("%s %s *(%s)*%s", prefixBadge, toolDisplay, timingPart, resultSuffix)
}

func summarizeBatchWrapperCallWithLimit(rawInput string, maxValChars int) (toolDisplay string, argSummary string) {
	var wrapper struct {
		Calls []struct {
			Name      string         `json:"name"`
			Arguments map[string]any `json:"arguments"`
		} `json:"calls"`
	}
	if json.Unmarshal([]byte(rawInput), &wrapper) != nil || len(wrapper.Calls) == 0 {
		return "", ""
	}
	counts := make(map[string]int)
	var order []string
	var firstArgs map[string]any
	for _, c := range wrapper.Calls {
		name := strings.TrimSpace(c.Name)
		if name == "" {
			continue
		}
		if counts[name] == 0 {
			order = append(order, name)
		}
		counts[name]++
		if firstArgs == nil && len(c.Arguments) > 0 {
			firstArgs = c.Arguments
		}
	}
	if len(order) == 0 {
		return "", ""
	}
	var parts []string
	for _, name := range order {
		if counts[name] > 1 {
			parts = append(parts, fmt.Sprintf("**`%s`** (×%d)", name, counts[name]))
		} else {
			parts = append(parts, fmt.Sprintf("**`%s`**", name))
		}
	}
	toolDisplay = strings.Join(parts, ", ")
	if firstArgs != nil {
		argSummary = formatArgMapSummaryWithLimit(firstArgs, maxValChars)
	}
	return toolDisplay, argSummary
}

func summarizeActivityInputWithLimit(_ string, rawInput string, maxValChars int) string {
	trimmed := strings.TrimSpace(rawInput)
	if trimmed == "" || trimmed == "{}" || trimmed == "null" {
		return ""
	}
	var m map[string]any
	if json.Unmarshal([]byte(trimmed), &m) == nil {
		if len(m) == 0 {
			return ""
		}
		return formatArgMapSummaryWithLimit(m, maxValChars)
	}
	if m := shapedStringPlaceholderRe.FindStringSubmatch(trimmed); len(m) == 2 {
		return fmt.Sprintf("input (%s chars)", m[1])
	}
	return fmt.Sprintf("`%s`", truncateText(strings.ReplaceAll(trimmed, "\n", " "), maxValChars))
}

func formatArgMapSummaryWithLimit(m map[string]any, maxValChars int) string {
	keys := make([]string, 0, len(m))
	for k := range m {
		if strings.HasPrefix(k, "<key ") {
			continue
		}
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var parts []string
	for _, k := range keys {
		val := m[k]
		switch v := val.(type) {
		case string:
			if sm := shapedStringPlaceholderRe.FindStringSubmatch(v); len(sm) == 2 {
				parts = append(parts, fmt.Sprintf("`%s` (%s chars)", k, sm[1]))
			} else if v == "[redacted]" {
				parts = append(parts, fmt.Sprintf("`%s`: `[redacted]`", k))
			} else {
				parts = append(parts, fmt.Sprintf("`%s`: `%s`", k, truncateText(strings.ReplaceAll(v, "\n", " "), maxValChars)))
			}
		case float64, int, int64, json.Number, bool:
			parts = append(parts, fmt.Sprintf("`%s`: `%v`", k, v))
		default:
			if b, err := json.Marshal(v); err == nil && len(b) > 0 && string(b) != "null" {
				parts = append(parts, fmt.Sprintf("`%s`: `%s`", k, truncateText(string(b), maxValChars)))
			} else {
				parts = append(parts, fmt.Sprintf("`%s`", k))
			}
		}
	}
	return strings.Join(parts, ", ")
}
