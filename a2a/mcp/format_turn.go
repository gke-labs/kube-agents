package mcp

import (
	"fmt"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

const (
	maxDebugThoughts      = 15
	headThoughts          = 6
	tailThoughts          = 9
	maxDebugTableRows     = 24
	headDebugRows         = 8
	tailDebugRows         = 16
	maxDebugThinkingLines = 16
	headThinkingLines     = 6
	tailThinkingLines     = 10
)

// FormatTurnOutcomeMarkdown renders a turn outcome using the default clean, collapsed-thinking Antigravity UX.
func FormatTurnOutcomeMarkdown(outcome *TurnOutcome) string {
	return FormatTurnOutcomeMarkdownWithOptions(outcome, false)
}

// FormatTurnOutcomeMarkdownWithOptions renders a turn outcome.
func FormatTurnOutcomeMarkdownWithOptions(outcome *TurnOutcome, debug bool) string {
	if outcome == nil {
		return ""
	}
	if !debug {
		return formatCleanTurnOutcomeMarkdown(outcome)
	}
	var sb strings.Builder
	fmt.Fprintf(&sb, "## 💬 Kube-Agents Turn (`%s` / `console:%s`)\n\n", outcome.ConversationAlias, outcome.ConversationToken)

	if len(outcome.ConsoleNotices) > 0 {
		sb.WriteString("### 🔔 Gateway Notices\n")
		for _, n := range outcome.ConsoleNotices {
			fmt.Fprintf(&sb, "- %s\n", n.Text)
		}
		sb.WriteString("\n")
	}

	if len(outcome.ChildTasks) > 0 && outcome.PrimaryTask != nil {
		sb.WriteString("### 🔀 Delegation Flow\n```mermaid\nflowchart LR\n")
		fmt.Fprintf(&sb, "  P[\"%s (%s)\"]\n", outcome.PrimaryTask.Addressee, outcome.PrimaryTask.TaskID)
		for i, child := range outcome.ChildTasks {
			nodeID := fmt.Sprintf("C%d", i)
			fmt.Fprintf(&sb, "  P -->|delegate| %s[\"%s (%s)\"]\n", nodeID, child.Addressee, child.TaskID)
			if outcome.WakeTask != nil {
				fmt.Fprintf(&sb, "  %s -->|wake| W[\"%s (%s)\"]\n", nodeID, outcome.WakeTask.Addressee, outcome.WakeTask.TaskID)
			}
		}
		sb.WriteString("```\n\n")
	}

	if outcome.PrimaryTask != nil {
		sb.WriteString(FormatTaskRecordMarkdownWithOptions(outcome.PrimaryTask, "Primary Task", true))
	}

	for i, child := range outcome.ChildTasks {
		sb.WriteString("\n---\n")
		sb.WriteString(FormatTaskRecordMarkdownWithOptions(child, fmt.Sprintf("Delegated Child Task #%d", i+1), true))
	}

	if outcome.WakeTask != nil {
		sb.WriteString("\n---\n")
		sb.WriteString(FormatTaskRecordMarkdownWithOptions(outcome.WakeTask, "Session Wake Synthesis Task", true))
	}

	allPRs := collectOutcomePRs(outcome)
	if len(allPRs) > 0 {
		sb.WriteString("\n> [!TIP]\n")
		fmt.Fprintf(&sb, "> **Pull Request(s) Detected**: %s\n", strings.Join(allPRs, ", "))
		sb.WriteString("> Call `inspect_pr` to fetch the unified diff and open files natively in Antigravity's diff viewer!\n")
	}

	if outcome.TimedOut && outcome.PrimaryTask != nil {
		sb.WriteString("\n> [!NOTE]\n")
		fmt.Fprintf(&sb, "> Task `%s` is still in flight (`%s`). You can call `task_control` (`action: \"get\"` or `\"steer\"` or `\"stop\"`) with `task_id: %q`, or wait for the automatic background push wakeup when it finishes.\n",
			outcome.PrimaryTask.TaskID, outcome.PrimaryTask.State, outcome.PrimaryTask.TaskID)
	}

	return sb.String()
}

func formatCompactDebugLLMResult(outcome *TurnOutcome, answerOnlyMD string) string {
	if outcome == nil || outcome.PrimaryTask == nil {
		return answerOnlyMD
	}
	rec := outcome.PrimaryTask
	sec := computeOutcomeElapsedSeconds(rec, outcome.ChildTasks, outcome.WakeTask)
	cards, doneCards, runningCards, totalCards, _ := extractKanbanCardsFromTask(rec)
	if totalCards == 0 {
		for _, child := range outcome.ChildTasks {
			cCards, cDone, cRunning, cTotal, _ := extractKanbanCardsFromTask(child)
			if cTotal > 0 {
				cards = append(cards, cCards...)
				doneCards += cDone
				runningCards += cRunning
				totalCards += cTotal
			}
		}
	}
	var sb strings.Builder
	fmt.Fprintf(&sb, "### Debug Telemetry Summary (`%s`)\n", rec.TaskID)
	fmt.Fprintf(&sb, "- **Task / Context**: `%s` / `%s` (`%s`, `%s`, `%ds` elapsed)\n",
		rec.TaskID, firstNonEmptyStr(rec.ContextID, "—"), firstNonEmptyStr(rec.Addressee, "platform"), rec.State, sec)
	fmt.Fprintf(&sb, "- **Envelopes & Tool Calls**: `%d` envelopes (%s), `%d` tool calls\n",
		rec.EnvelopeCount, firstNonEmptyStr(formatEnvelopeKindBreakdown(rec.EnvelopeCountsByKind), "—"), len(rec.Activities))
	if len(outcome.ChildTasks) > 0 || len(rec.Delegates) > 0 {
		var hops []string
		hops = append(hops, fmt.Sprintf("`%s` (`%s`)", firstNonEmptyStr(rec.Addressee, "session"), rec.TaskID))
		if len(outcome.ChildTasks) > 0 {
			for _, child := range outcome.ChildTasks {
				if child == nil {
					continue
				}
				hops = append(hops, fmt.Sprintf("`%s` (`%s`, `%d` envelopes, `%d` tool calls)",
					firstNonEmptyStr(child.Addressee, "platform"), child.TaskID, child.EnvelopeCount, len(child.Activities)))
			}
		} else {
			for _, d := range rec.Delegates {
				if d.ChildTaskID != "" {
					hops = append(hops, fmt.Sprintf("`%s` (`%s`)", d.Addressee, d.ChildTaskID))
				} else {
					hops = append(hops, fmt.Sprintf("`%s`", d.Addressee))
				}
			}
		}
		if outcome.WakeTask != nil {
			hops = append(hops, fmt.Sprintf("`%s` wake (`%s`, `%d` envelopes)",
				firstNonEmptyStr(outcome.WakeTask.Addressee, rec.Addressee, "session"), outcome.WakeTask.TaskID, outcome.WakeTask.EnvelopeCount))
		}
		fmt.Fprintf(&sb, "- **Delegation Chain**: %s\n", strings.Join(hops, " → "))
	}
	if len(rec.SteerTimestamps) > 0 {
		var steerTimes []string
		for _, ts := range rec.SteerTimestamps {
			steerTimes = append(steerTimes, ts.UTC().Format("15:04:05 UTC"))
		}
		fmt.Fprintf(&sb, "- **Steer / Follow-Up Timeline**: `%d` steer(s) at `%s`\n", len(rec.SteerTimestamps), strings.Join(steerTimes, "`, `"))
	}
	if totalCards > 0 {
		fmt.Fprintf(&sb, "- **Specialist Kanban Cards**: `%d/%d` completed (`%d` running)\n", doneCards, totalCards, runningCards)
		for _, c := range cards {
			fmt.Fprintf(&sb, "  - `%s` (%s): status=`%s`, verdict=`%s` — %s\n",
				c.ID, firstNonEmptyStr(c.Cluster, c.Title, "—"), c.Status, firstNonEmptyStr(c.Verdict, c.Status), truncateText(c.Summary, 140))
		}
	}
	sb.WriteString("\n### Final Answer\n\n")
	sb.WriteString(answerOnlyMD)
	return strings.TrimSpace(sb.String()) + "\n"
}

// FormatTranscriptTurnMarkdown renders a single root user turn for a conversation transcript.
func FormatTranscriptTurnMarkdown(outcome *TurnOutcome, maxAnswerRunes int, debug bool) string {
	if outcome == nil || outcome.PrimaryTask == nil {
		return ""
	}
	t := outcome.PrimaryTask
	var sb strings.Builder
	fmt.Fprintf(&sb, "### Prompt: %s (`%s`)\n", firstNonEmptyStr(t.PromptText, "(no prompt text)"), t.TaskID)
	answerMD := strings.TrimSpace(formatCleanTurnAnswerOnlyMarkdown(outcome))
	if maxAnswerRunes > 0 && len([]rune(answerMD)) > maxAnswerRunes {
		answerMD = truncateText(answerMD, maxAnswerRunes)
	}
	if answerMD != "" {
		answerMD += "\n"
	}
	if debug {
		sb.WriteString(formatCompactDebugLLMResult(outcome, answerMD))
	} else {
		thoughts := CollectOutcomeSteps(outcome)
		summaryLabel := formatThoughtSummaryLabel(t, outcome.ChildTasks, outcome.WakeTask, len(thoughts))
		fmt.Fprintf(&sb, "_%s_\n\n", summaryLabel)
		if len(outcome.ChildTasks) > 0 || len(t.Delegates) > 0 {
			var hops []string
			hops = append(hops, fmt.Sprintf("`%s` (`%s`)", firstNonEmptyStr(t.Addressee, "session"), t.TaskID))
			for _, child := range outcome.ChildTasks {
				if child != nil {
					hops = append(hops, fmt.Sprintf("`%s` (`%s`)", firstNonEmptyStr(child.Addressee, "platform"), child.TaskID))
				}
			}
			if outcome.WakeTask != nil {
				hops = append(hops, fmt.Sprintf("`%s` wake (`%s`)", firstNonEmptyStr(outcome.WakeTask.Addressee, t.Addressee, "session"), outcome.WakeTask.TaskID))
			}
			fmt.Fprintf(&sb, "- **Delegation Chain**: %s\n\n", strings.Join(hops, " → "))
		}
		sb.WriteString(answerMD)
	}
	sb.WriteString("\n---\n")
	return sb.String()
}

func formatCleanTurnOutcomeMarkdown(outcome *TurnOutcome) string {
	var sb strings.Builder

	if outcome.PrimaryTask == nil {
		for _, n := range outcome.ConsoleNotices {
			if txt := strings.TrimSpace(n.Text); txt != "" {
				fmt.Fprintf(&sb, "%s\n", txt)
			}
		}
		return strings.TrimSpace(sb.String())
	}

	thoughts := CollectOutcomeSteps(outcome)
	summaryLabel := formatThoughtSummaryLabel(outcome.PrimaryTask, outcome.ChildTasks, outcome.WakeTask, len(thoughts))
	if len(thoughts) > 0 {
		sb.WriteString("<details>\n")
		fmt.Fprintf(&sb, "<summary>%s</summary>\n\n", summaryLabel)
		for _, line := range thoughts {
			fmt.Fprintf(&sb, "- ✅ %s\n", line)
		}
		sb.WriteString("\n</details>\n\n")
	}

	sb.WriteString(formatCleanTurnAnswerOnlyMarkdown(outcome))
	return strings.TrimSpace(sb.String()) + "\n"
}

func formatCleanTurnAnswerOnlyMarkdown(outcome *TurnOutcome) string {
	if outcome == nil {
		return ""
	}
	var sb strings.Builder
	if outcome.PrimaryTask == nil {
		for _, n := range outcome.ConsoleNotices {
			if txt := strings.TrimSpace(n.Text); txt != "" {
				fmt.Fprintf(&sb, "%s\n", txt)
			}
		}
		return strings.TrimSpace(sb.String())
	}

	resultBody := renderTaskAnswerBody(outcome.WakeTask)
	if resultBody == "" && len(outcome.ChildTasks) > 0 {
		var parts []string
		if pBody := renderTaskAnswerBody(outcome.PrimaryTask); pBody != "" {
			parts = append(parts, pBody)
		}
		for _, child := range outcome.ChildTasks {
			if cBody := renderTaskAnswerBody(child); cBody != "" {
				parts = append(parts, cBody)
			}
		}
		resultBody = strings.Join(parts, "\n\n")
	}
	if resultBody == "" {
		resultBody = renderTaskAnswerBody(outcome.PrimaryTask)
	}
	if resultBody != "" {
		sb.WriteString(resultBody)
		if !strings.HasSuffix(resultBody, "\n") {
			sb.WriteString("\n")
		}
	}

	allPRs := collectOutcomePRs(outcome)
	var missingPRs []string
	for _, pr := range allPRs {
		if !strings.Contains(resultBody, pr) {
			missingPRs = append(missingPRs, pr)
		}
	}
	if len(missingPRs) > 0 {
		sb.WriteString("\n**Pull Request**: " + strings.Join(missingPRs, ", ") + "\n")
	}

	if outcome.TimedOut && outcome.PrimaryTask != nil {
		sb.WriteString("\n> [!NOTE]\n")
		fmt.Fprintf(&sb, "> Task `%s` is still in flight (`%s`). Wait for the automatic background push wakeup or call `task_control` (`action: \"get\"`, `task_id: %q`).\n",
			outcome.PrimaryTask.TaskID, outcome.PrimaryTask.State, outcome.PrimaryTask.TaskID)
	}

	return strings.TrimSpace(sb.String()) + "\n"
}

func isSilentResultText(s string) bool {
	t := strings.Trim(strings.TrimSpace(s), "`*_ ")
	return strings.EqualFold(t, "[SILENT]")
}

func hasStructuredChatContent(cm *chatmsg.Message) bool {
	if cm == nil {
		return false
	}
	return strings.TrimSpace(cm.Detail) != "" ||
		len(cm.Rows) > 0 ||
		cm.Plan != nil ||
		cm.Fold != nil ||
		len(cm.Choices) > 0 ||
		len(cm.Links) > 0 ||
		(cm.Tone != "" && cm.Tone != "neutral")
}

func renderTaskAnswerBody(rec *TaskRecord) string {
	if rec == nil {
		return ""
	}
	var sb strings.Builder
	plain := strings.TrimSpace(rec.ResultText)
	if isSilentResultText(plain) {
		plain = ""
		for i := len(rec.IntermediateTurns) - 1; i >= 0; i-- {
			if t := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(rec.IntermediateTurns[i]), cardResultsRawHeader)); t != "" && !isSilentResultText(t) {
				plain = t
				break
			}
		}
		if plain == "" {
			plain = silentResultFallback
		}
	}
	if rec.ResultChat != nil || plain != "" {
		if rec.ResultChat != nil && (hasStructuredChatContent(rec.ResultChat) || plain == "") {
			sb.WriteString(FormatChatMessageMarkdown(rec.ResultChat))
			if plain != "" && rec.ResultChat.Detail == "" && len(rec.ResultChat.Rows) == 0 {
				fmt.Fprintf(&sb, "\n%s\n", plain)
			}
		} else {
			fmt.Fprintf(&sb, "%s\n", plain)
		}
	} else if !rec.Terminal && (rec.ProgressChat != nil || strings.TrimSpace(rec.ProgressText) != "") {
		if rec.ProgressChat != nil {
			sb.WriteString(FormatChatMessageMarkdown(rec.ProgressChat))
		} else {
			fmt.Fprintf(&sb, "%s\n", strings.TrimSpace(rec.ProgressText))
		}
	} else if rec.StatusText != "" {
		fmt.Fprintf(&sb, "%s\n", strings.TrimSpace(rec.StatusText))
	}
	return strings.TrimSpace(sb.String())
}

func computeOutcomeElapsedSeconds(primary *TaskRecord, children []*TaskRecord, wake *TaskRecord) int {
	if primary == nil {
		return 0
	}
	start := primary.CreatedAt
	end := primary.UpdatedAt
	anyRunning := !primary.Terminal
	for _, c := range children {
		if c != nil {
			if c.UpdatedAt.After(end) {
				end = c.UpdatedAt
			}
			if !c.Terminal {
				anyRunning = true
			}
		}
	}
	if wake != nil {
		if wake.UpdatedAt.After(end) {
			end = wake.UpdatedAt
		}
		if !wake.Terminal {
			anyRunning = true
		}
	}
	if anyRunning {
		now := time.Now().UTC()
		if now.After(end) {
			end = now
		}
	}
	if !start.IsZero() && end.After(start) {
		sec := int(end.Sub(start).Round(time.Second).Seconds())
		if sec >= 1 {
			return sec
		}
	}
	var totalMs int64
	for _, a := range primary.Activities {
		totalMs += a.DurationMs
	}
	for _, c := range children {
		if c != nil {
			for _, a := range c.Activities {
				totalMs += a.DurationMs
			}
		}
	}
	if totalMs >= 1000 {
		return int((time.Duration(totalMs) * time.Millisecond).Round(time.Second).Seconds())
	}
	return 0
}

func formatThoughtSummaryLabel(primary *TaskRecord, children []*TaskRecord, wake *TaskRecord, stepCount int) string {
	sec := computeOutcomeElapsedSeconds(primary, children, wake)
	if sec >= 1 && stepCount > 0 {
		return fmt.Sprintf("Thought for %ds (%d %s)", sec, stepCount, pluralizeStep(stepCount))
	}
	if sec >= 1 {
		return fmt.Sprintf("Thought for %ds", sec)
	}
	if stepCount > 0 {
		return fmt.Sprintf("Thought (%d %s)", stepCount, pluralizeStep(stepCount))
	}
	return "Thought"
}

// FormatTaskRecordMarkdown renders a single TaskRecord using default clean formatting.
func FormatTaskRecordMarkdown(rec *TaskRecord, titlePrefix string) string {
	return FormatTaskRecordMarkdownWithOptions(rec, titlePrefix, false)
}

// FormatTaskRecordMarkdownWithOptions renders a single TaskRecord.
func FormatTaskRecordMarkdownWithOptions(rec *TaskRecord, titlePrefix string, debug bool) string {
	if rec == nil {
		return ""
	}
	var sb strings.Builder
	if debug {
		if titlePrefix == "" {
			titlePrefix = "Task"
		}
		fmt.Fprintf(&sb, "### %s: `%s` → `%s` (%s)\n\n",
			titlePrefix, rec.TaskID, rec.Addressee, formatTaskStateBadge(rec.State))

		fmt.Fprintf(&sb, "- **Context ID**: `%s`", rec.ContextID)
		if rec.Conversation != "" {
			fmt.Fprintf(&sb, " | **Conversation**: `%s`", rec.Conversation)
		}
		if rec.DelegatedFromTaskID != "" {
			fmt.Fprintf(&sb, " | **Delegated From**: `%s`", rec.DelegatedFromTaskID)
		}
		if rec.WakeFromChildTaskID != "" {
			fmt.Fprintf(&sb, " | **Wake From Child**: `%s`", rec.WakeFromChildTaskID)
		}
		sb.WriteString("\n\n")
	}

	thoughts := CollectOutcomeSteps(&TurnOutcome{PrimaryTask: rec})
	if len(thoughts) > 0 {
		label := formatThoughtSummaryLabel(rec, nil, nil, len(thoughts))
		sb.WriteString("<details>\n")
		fmt.Fprintf(&sb, "<summary>%s</summary>\n\n", label)
		for i, line := range thoughts {
			if debug && len(thoughts) > maxDebugThoughts && i >= headThoughts && i < len(thoughts)-tailThoughts {
				if i == headThoughts {
					fmt.Fprintf(&sb, "- *… (%d intermediate steps omitted — see telemetry panels below)*\n", len(thoughts)-headThoughts-tailThoughts)
				}
				continue
			}
			fmt.Fprintf(&sb, "- ✅ %s\n", line)
		}
		sb.WriteString("\n</details>\n\n")
	}

	if debug {
		sec := computeOutcomeElapsedSeconds(rec, nil, nil)
		if sec < 1 {
			sec = 1
		}
		addressee := firstNonEmptyStr(rec.Addressee, "platform-agent")

		fmt.Fprintf(&sb, "<details open>\n<summary>📡 <strong>Bus Envelope &amp; Task Telemetry</strong> — <code>%s</code> → <code>%s</code> (<code>%d envelopes</code>, <code>%ds</code>)</summary>\n\n",
			rec.TaskID, addressee, rec.EnvelopeCount, sec)
		sb.WriteString("| Field | Value |\n| :--- | :--- |\n")
		fmt.Fprintf(&sb, "| **Task & Context ID** | `%s` / `%s` (conversation `%s`) |\n",
			rec.TaskID, firstNonEmptyStr(rec.ContextID, "—"), firstNonEmptyStr(rec.Conversation, "default"))
		fmt.Fprintf(&sb, "| **Subjects** | `a2a.tasks.%s.%s.in` → `a2a.tasks.%s.%s.out` |\n",
			addressee, rec.TaskID, addressee, rec.TaskID)
		breakdown := formatEnvelopeKindBreakdown(rec.EnvelopeCountsByKind)
		if breakdown != "" {
			fmt.Fprintf(&sb, "| **Envelopes Folded** | `%d` total (%s) |\n", rec.EnvelopeCount, breakdown)
		} else {
			fmt.Fprintf(&sb, "| **Envelopes Folded** | `%d` total |\n", rec.EnvelopeCount)
		}
		startStr := "—"
		if !rec.CreatedAt.IsZero() {
			startStr = rec.CreatedAt.UTC().Format("15:04:05 UTC")
		}
		endStr := "—"
		if !rec.UpdatedAt.IsZero() {
			endStr = rec.UpdatedAt.UTC().Format("15:04:05 UTC")
		}
		fmt.Fprintf(&sb, "| **Timing** | `%s` → `%s` (`%ds` elapsed) |\n", startStr, endStr, sec)
		if rec.DelegatedFromTaskID != "" || rec.WakeFromChildTaskID != "" {
			fmt.Fprintf(&sb, "| **Delegation Lineage** | DelegatedFrom: `%s`, WakeFromChild: `%s` |\n",
				firstNonEmptyStr(rec.DelegatedFromTaskID, "—"), firstNonEmptyStr(rec.WakeFromChildTaskID, "—"))
		}
		if len(rec.SteerTimestamps) > 0 {
			var steerTimes []string
			for _, ts := range rec.SteerTimestamps {
				steerTimes = append(steerTimes, fmt.Sprintf("`%s`", ts.UTC().Format("15:04:05 UTC")))
			}
			fmt.Fprintf(&sb, "| **Steer / Follow-Up Timeline** | `%d` steer(s) at %s |\n",
				len(rec.SteerTimestamps), strings.Join(steerTimes, ", "))
		}
		sb.WriteString("\n</details>\n\n")

		cards, doneCards, runningCards, totalCards, _ := extractKanbanCardsFromTask(rec)
		if totalCards > 0 {
			fmt.Fprintf(&sb, "<details open>\n<summary>🗂️ <strong>Kanban Specialist Cards</strong> — <code>%d/%d completed</code>", doneCards, totalCards)
			if runningCards > 0 {
				fmt.Fprintf(&sb, ", <code>%d running</code>", runningCards)
			}
			sb.WriteString("</summary>\n\n")
			sb.WriteString("| Card ID | Cluster / Title | Worker / Session | Status | Verdict & Summary |\n")
			sb.WriteString("| :--- | :--- | :--- | :--- | :--- |\n")
			for _, c := range cards {
				titleCell := firstNonEmptyStr(c.Cluster, c.Title, "—")
				if c.Cluster != "" && c.Title != "" && !strings.EqualFold(c.Cluster, c.Title) {
					titleCell = fmt.Sprintf("**`%s`** — %s", c.Cluster, truncateText(c.Title, 64))
				} else {
					titleCell = fmt.Sprintf("**`%s`**", titleCell)
				}
				workerCell := firstNonEmptyStr(c.Worker, "specialist")
				if c.SessionID != "" {
					workerCell = fmt.Sprintf("`%s` (`%s`)", workerCell, c.SessionID)
				} else {
					workerCell = fmt.Sprintf("`%s`", workerCell)
				}
				statusBadge := "⏳ `pending`"
				switch c.Status {
				case "done":
					statusBadge = "✅ `done`"
				case "failed":
					statusBadge = "❌ `failed`"
				case "running":
					statusBadge = "🔄 `running`"
				}
				verdictSummary := firstNonEmptyStr(c.Summary, "—")
				if c.Verdict != "" {
					verdictSummary = fmt.Sprintf("`%s` — %s", c.Verdict, firstNonEmptyStr(c.Summary, "completed"))
				}
				verdictSummary = strings.ReplaceAll(strings.ReplaceAll(verdictSummary, "\n", " "), "|", "\\|")
				fmt.Fprintf(&sb, "| `%s` | %s | %s | %s | %s |\n",
					c.ID,
					strings.ReplaceAll(titleCell, "|", "\\|"),
					strings.ReplaceAll(workerCell, "|", "\\|"),
					statusBadge,
					truncateText(verdictSummary, 180))
			}
			sb.WriteString("\n</details>\n\n")
		}
	}

	if debug && len(rec.Activities) > 0 {
		fmt.Fprintf(&sb, "<details open>\n<summary>🛠️ <strong>Full Tool Activity Stream</strong> — <code>%d tool calls</code></summary>\n\n", len(rec.Activities))
		sb.WriteString("#### 🛠️ Tool Activity Stream\n\n")
		sb.WriteString("| # | Time | Worker / Card | Tool | Status | Duration | Arguments | Result / Error Preview |\n")
		sb.WriteString("| ---: | :--- | :--- | :--- | :--- | ---: | :--- | :--- |\n")
		maxInputChars := 240
		maxPreviewChars := 180
		if len(rec.Activities) > maxDebugTableRows {
			maxInputChars = 75
			maxPreviewChars = 65
		}
		for i, act := range rec.Activities {
			if len(rec.Activities) > maxDebugTableRows && i >= headDebugRows && i < len(rec.Activities)-tailDebugRows {
				if i == headDebugRows {
					omitted := len(rec.Activities) - headDebugRows - tailDebugRows
					fmt.Fprintf(&sb, "| … | — | — | *(%d intermediate tool calls)* | — | — | *See per-step expandable cards above* | — |\n", omitted)
				}
				continue
			}
			idx := act.Index
			if idx <= 0 {
				idx = i + 1
			}
			timeStr := "—"
			if tStr := formatActivityAtTime(act.At); tStr != "" {
				timeStr = fmt.Sprintf("`%s`", tStr)
			}
			workerStr := firstNonEmptyStr(act.Worker, "platform")
			if act.SessionID != "" && !strings.Contains(workerStr, act.SessionID) {
				workerStr = fmt.Sprintf("`%s` (`%s`)", workerStr, act.SessionID)
			} else {
				workerStr = fmt.Sprintf("`%s`", workerStr)
			}
			statusStr := act.Status
			if statusStr == "" {
				statusStr = "called"
			}
			durStr := "—"
			if act.DurationMs > 0 {
				if act.DurationMs >= 1000 {
					durStr = fmt.Sprintf("%dms (%.1fs)", act.DurationMs, float64(act.DurationMs)/1000.0)
				} else {
					durStr = fmt.Sprintf("%dms", act.DurationMs)
				}
			}
			inputClean := strings.ReplaceAll(strings.TrimSpace(act.Input), "\n", " ")
			inputClean = strings.ReplaceAll(inputClean, "|", "\\|")
			inputClean = truncateText(inputClean, maxInputChars)

			previewClean := "—"
			if act.ErrorMessage != "" {
				errClean := strings.ReplaceAll(strings.TrimSpace(act.ErrorMessage), "\n", " ")
				errClean = strings.ReplaceAll(errClean, "|", "\\|")
				previewClean = fmt.Sprintf("❌ `%s`", truncateText(errClean, maxPreviewChars))
			} else if act.ResultPreview != "" {
				resClean := strings.ReplaceAll(strings.TrimSpace(act.ResultPreview), "\n", " ")
				resClean = strings.ReplaceAll(resClean, "|", "\\|")
				previewClean = fmt.Sprintf("`%s`", truncateText(resClean, maxPreviewChars))
			}
			fmt.Fprintf(&sb, "| %d | %s | %s | `%s` | `%s` | %s | `%s` | %s |\n",
				idx, timeStr, strings.ReplaceAll(workerStr, "|", "\\|"), act.Tool, statusStr, durStr, firstNonEmptyStr(inputClean, "—"), previewClean)
		}
		sb.WriteString("\n</details>\n\n")
	}

	var thinkingLines []string
	for _, raw := range strings.Split(rec.Thinking, "\n") {
		if strings.TrimSpace(raw) != "" {
			thinkingLines = append(thinkingLines, strings.TrimSpace(raw))
		}
	}
	if debug && (len(thinkingLines) > 0 || len(rec.IntermediateTurns) > 0) {
		fmt.Fprintf(&sb, "<details>\n<summary>🧠 <strong>Raw LLM API Turn Log</strong> — <code>%d thinking frames</code></summary>\n\n", len(thinkingLines))
		for i, raw := range thinkingLines {
			if len(thinkingLines) > maxDebugThinkingLines && i >= headThinkingLines && i < len(thinkingLines)-tailThinkingLines {
				if i == headThinkingLines {
					omitted := len(thinkingLines) - headThinkingLines - tailThinkingLines
					fmt.Fprintf(&sb, "- *… (%d intermediate thinking frames omitted — see consolidated steps above)*\n", omitted)
				}
				continue
			}
			clean := truncateText(strings.Join(strings.Fields(raw), " "), 150)
			fmt.Fprintf(&sb, "%d. %s\n", i+1, clean)
		}
		if len(rec.IntermediateTurns) > 0 {
			sb.WriteString("\n#### 🔄 Intermediate Turns\n")
			for i, t := range rec.IntermediateTurns {
				if i >= 2 {
					fmt.Fprintf(&sb, "*… (%d additional intermediate turns omitted)*\n\n", len(rec.IntermediateTurns)-2)
					break
				}
				fmt.Fprintf(&sb, "> %s\n\n", truncateText(strings.TrimSpace(t), 450))
			}
		}
		sb.WriteString("\n</details>\n\n")
	}

	if debug && len(rec.Delegates) > 0 {
		sb.WriteString("#### 🤝 Delegations Requested\n")
		for _, d := range rec.Delegates {
			childInfo := ""
			if d.ChildTaskID != "" {
				childInfo = fmt.Sprintf(" → child task `%s`", d.ChildTaskID)
			}
			fmt.Fprintf(&sb, "- **To `%s`**%s: %s\n", d.Addressee, childInfo, d.Text)
		}
		sb.WriteString("\n")
	}

	if debug && len(rec.Notices) > 0 {
		sb.WriteString("#### 📌 Task Notices\n")
		for _, n := range rec.Notices {
			fmt.Fprintf(&sb, "- %s\n", n)
		}
		sb.WriteString("\n")
	}

	body := renderTaskAnswerBody(rec)
	if body != "" {
		if debug {
			sb.WriteString("#### 🎯 Result\n\n")
		}
		sb.WriteString(body + "\n")
	}

	return sb.String()
}

// FormatLiveTaskProgress renders a concise single-line live status message suitable for
// MCP notifications/progress frames when debug mode is enabled.
func FormatLiveTaskProgress(primary *TaskRecord, children []*TaskRecord, wake *TaskRecord, elapsed time.Duration) string {
	if primary == nil {
		return "Waiting for kube-agents task..."
	}
	rounded := elapsed.Round(time.Second)
	if rounded < time.Second {
		rounded = time.Second
	}
	msg := fmt.Sprintf("Task %s → %s (%s, %s elapsed, %d tool calls)",
		primary.TaskID, primary.Addressee, primary.State, rounded, len(primary.Activities))
	if len(children) > 0 {
		lastChild := children[len(children)-1]
		msg += fmt.Sprintf(" • Delegated to %s (%s)", lastChild.Addressee, lastChild.State)
	}
	if wake != nil {
		msg += fmt.Sprintf(" • Wake synthesis (%s)", wake.State)
	}
	return msg
}

func collectOutcomePRs(outcome *TurnOutcome) []string {
	var out []string
	add := func(links []string) {
		for _, l := range links {
			if !containsString(out, l) {
				out = append(out, l)
			}
		}
	}
	if outcome.PrimaryTask != nil {
		add(outcome.PrimaryTask.PRLinks)
	}
	for _, c := range outcome.ChildTasks {
		if c != nil {
			add(c.PRLinks)
		}
	}
	for _, ct := range outcome.CardTasks {
		if ct != nil {
			add(ct.PRLinks)
		}
	}
	if outcome.WakeTask != nil {
		add(outcome.WakeTask.PRLinks)
	}
	return out
}
