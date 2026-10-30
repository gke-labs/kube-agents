package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The turn tools: send a turn as a console user, and read or stop its task.
const (
	ToolSendMessage = "send_message"
	ToolTaskControl = "task_control"
	ToolPollEvents  = "poll_events"

	actionGet  = "get"
	actionStop = "stop"
	// stopText is the console door's cancel: a turn whose whole text is a
	// stop word cancels the conversation's running task (chattext.IsStop).
	stopText = "stop"

	defaultWaitSeconds = 60
	maxWaitSeconds     = 300
	// minAnswerWait is the least a sent turn's answer is waited on, however
	// much of the wait went on the conversation's running turn.
	minAnswerWait = 2 * time.Second

	// maxAnswerBytes caps the answer shown in one result; the full answer
	// stays on the task.
	maxAnswerBytes = 64 * 1024

	// maxPollSeconds bounds poll_events' wait for a new frame.
	maxPollSeconds = 60
)

// MaxTurnWait is the longest a turn tool holds a request: the wait for the
// lock and the gateway's submission, plus the longest answer wait a caller
// may ask for. The noticeSettle window after the submission is spent inside
// it: noticeGrace is slack on that path. Every turn call is held to it,
// whatever the client allows.
const MaxTurnWait = submissionWait + noticeGrace + maxWaitSeconds*time.Second

// TurnBus is what the turn tools need from the bus.
type TurnBus interface {
	SendTurn(ctx context.Context, convs *Conversations, token, text string, wait time.Duration) (TurnResult, *turnWatch, error)
	WaitForTurn(ctx context.Context, w *turnWatch, root TaskRef, wait time.Duration) (TurnOutcome, error)
	SendStop(ctx context.Context, convs *Conversations, token string) (TurnResult, error)
	ReadTask(ctx context.Context, ref TaskRef) (*lib.Task, error)
	WaitForTask(ctx context.Context, ref TaskRef, wait time.Duration) (*lib.Task, error)
	ResolveAddressee(ctx context.Context, taskID string) (string, error)
}

type sendMessageArgs struct {
	Text              string `json:"text"`
	Conversation      string `json:"conversation"`
	ConversationToken string `json:"conversation_token"`
	WaitSeconds       int    `json:"wait_seconds"`
}

// SendMessageTool sends one turn to the install's agents and waits a while
// for its answer.
func SendMessageTool(bus TurnBus, convs *Conversations) Tool {
	return Tool{
		Definition: ToolDefinition{
			Name: ToolSendMessage,
			Description: "Send a message to this kube-agents install, as a console user, and wait for the answer. " +
				"A turn still running when the wait ends returns its task; read it later with task_control. " +
				"Once the turn's tasks end, the reports of any card it filed are gathered too, until the wait ends or " +
				"no new report has come for a while; a card's report can take minutes, so for card work pass a longer " +
				"wait_seconds, or read later reports with poll_events. " +
				"A message on a conversation whose turn is still running waits for that turn to end first, within the same wait.",
			InputSchema: map[string]any{
				"type":     "object",
				"required": []string{"text"},
				"properties": map[string]any{
					"text":               map[string]any{"type": "string", "description": "The message."},
					"conversation":       map[string]any{"type": "string", "description": "A name for the conversation; turns with the same name continue it. Default: " + DefaultConversation + "."},
					"conversation_token": map[string]any{"type": "string", "description": "Continue a conversation by the token a previous answer reported, instead of by name."},
					"wait_seconds":       map[string]any{"type": "integer", "description": fmt.Sprintf("How long to wait for the answer, %d at most. Default %d.", maxWaitSeconds, defaultWaitSeconds)},
				},
			},
		},
		Call: func(ctx context.Context, raw json.RawMessage) (string, error) {
			ctx, cancel := context.WithTimeout(ctx, MaxTurnWait)
			defer cancel()
			var a sendMessageArgs
			if err := json.Unmarshal(orEmptyObject(raw), &a); err != nil {
				return "", fmt.Errorf("arguments: %w", err)
			}
			return sendAndWait(ctx, bus, convs, a.Conversation, a.ConversationToken, a.Text, waitFor(a.WaitSeconds))
		},
	}
}

// sendAndWait sends text as one turn of the conversation named by alias or
// token and renders what came of it: the gateway's notices, and the turn's
// task as it stands after wait.
func sendAndWait(ctx context.Context, bus TurnBus, convs *Conversations, alias, token, text string, wait time.Duration) (string, error) {
	token, byToken, err := convs.ResolveCall(alias, token)
	if err != nil {
		return "", err
	}
	// The wait covers any wait for the conversation's running turn too, so
	// a second message waits rather than being steered into it.
	start := time.Now()
	res, w, err := bus.SendTurn(ctx, convs, token, text, wait)
	if err != nil {
		return "", err
	}
	var sb strings.Builder
	writeConversation(&sb, alias, token, byToken)
	writeNotices(&sb, res.Notices)
	if res.Task == nil {
		if len(res.Notices) == 0 {
			sb.WriteString("The gateway answered with no task and no notice.\n")
		}
		return sb.String(), nil
	}
	out, err := bus.WaitForTurn(ctx, w, *res.Task, max(wait-time.Since(start), minAnswerWait))
	if err != nil {
		fmt.Fprintf(&sb, "Task `%s` carries the turn, but reading it failed: %v\n", res.Task, err)
		return sb.String(), nil
	}
	convs.setRunning(token, out.Unfinished())
	writeOutcome(&sb, out)
	writeReports(&sb, out, token)
	return sb.String(), nil
}

// writeReports renders the cards' reports on a turn, in the order they
// arrived, and why gathering them stopped. A report is an agent's text, so
// it is shown inert: a card's post must not read as this server's words.
func writeReports(sb *strings.Builder, out TurnOutcome, token string) {
	if out.ReportsEnd == reportsNotGathered {
		return
	}
	if len(out.Reports) > 0 {
		fmt.Fprintf(sb, "\nCard reports on this turn (%d), in the order they arrived:\n", len(out.Reports))
		for _, f := range out.Reports {
			sb.WriteString("\n")
			writeEventHead(sb, "Report from a card", f)
			sb.WriteString(inertBlock(f.Text))
		}
	}
	later := fmt.Sprintf("later reports can be read with poll_events (conversation_token `%s`, since %d)", token, out.ReportCursor)
	switch out.ReportsEnd {
	case reportsRunning:
		fmt.Fprintf(sb, "\nThe turn's tasks were still running when the wait ended, so the card reports shown are only those that "+
			"had arrived by then (%d); %s.\n", len(out.Reports), later)
	case reportsNoCards:
		fmt.Fprintf(sb, "\nThe turn's tasks ended, and the gateway's result for task %s said no work was handed to cards, "+
			"so gathering stopped at once; %s.\n", cell(out.Root.TaskID), later)
	case reportsQuiet:
		if len(out.Reports) == 0 {
			fmt.Fprintf(sb, "\n%s No card report came within %s of the end; a card's report can take minutes: %s.\n",
				cardsLine(out), reportQuiet, later)
		} else {
			fmt.Fprintf(sb, "\n%s Stopped gathering after %s with no new report; %s.\n", cardsLine(out), reportQuiet, later)
		}
	case reportsWaitOver:
		fmt.Fprintf(sb, "\n%s The wait ended while reports were still being gathered, so the reports shown are those that "+
			"had arrived by then; %s, or wait longer with a larger wait_seconds.\n", cardsLine(out), later)
	}
	if out.ReportsDropped {
		fmt.Fprintf(sb, "\nThis server's buffer for the conversation dropped frames posted since this turn was sent "+
			"(it keeps at most %d frames or %d KiB a conversation, and %d MiB in all, dropping the oldest), "+
			"so a report on this turn may be among them and is lost.\n",
			maxEventsPerConversation, maxEventBytesPerConversation>>10, maxEventBytesTotal>>20)
	}
}

// cardsLine says the turn's tasks ended, and what the gateway's result said
// about cards, which is why gathering went on.
func cardsLine(out TurnOutcome) string {
	if out.Cards == cardsPending {
		return "The turn's tasks ended, and the gateway's result said work was handed to cards."
	}
	return "The turn's tasks ended; no result from the gateway said whether work was handed to cards, so gathering waited for a quiet period."
}

// writeEventHead is a buffered frame's heading line: label, then its message
// id, the task it reports on, and its state, each an inert cell.
func writeEventHead(sb *strings.Builder, label string, f EventFrame) {
	fmt.Fprintf(sb, "%s (message %s", label, cell(f.MessageID))
	if f.ReplyTo != "" {
		fmt.Fprintf(sb, ", on task %s", cell(f.ReplyTo))
	}
	if f.TaskID != "" {
		task := f.TaskID
		if f.Addressee != "" {
			task = f.Addressee + taskRefSep + task
		}
		fmt.Fprintf(sb, ", task %s", cell(task))
	}
	if f.State != "" {
		fmt.Fprintf(sb, ", %s", cell(f.State))
	}
	if f.Edited {
		sb.WriteString(", updated")
	}
	sb.WriteString("):\n\n")
}

type pollEventsArgs struct {
	Conversation      string `json:"conversation"`
	ConversationToken string `json:"conversation_token"`
	Since             uint64 `json:"since"`
	WaitSeconds       int    `json:"wait_seconds"`
}

// PollEventsTool reads the cards' reports a conversation was posted since a
// cursor, from what this server heard.
func PollEventsTool(events *Events, convs *Conversations) Tool {
	return Tool{
		Definition: ToolDefinition{
			Name: ToolPollEvents,
			Description: "Read the cards' reports a conversation was posted outside a turn's answer, as this server heard " +
				"them, newer than a cursor. An update to a report comes back under a new cursor. A name must be one this " +
				"server knows; a token is listened to from the first call that names it. Live only: posts made before this " +
				"server first heard of the conversation, or before it restarted, are not kept.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"conversation":       map[string]any{"type": "string", "description": "The conversation's name. Default: " + DefaultConversation + "."},
					"conversation_token": map[string]any{"type": "string", "description": "The conversation, by token, instead of by name."},
					"since":              map[string]any{"type": "integer", "description": "Return frames newer than this cursor, as an earlier answer reported it. Default 0: everything kept."},
					"wait_seconds":       map[string]any{"type": "integer", "description": fmt.Sprintf("When nothing is newer, wait up to this long for a frame, %d at most. Default 0.", maxPollSeconds)},
				},
			},
		},
		Call: func(ctx context.Context, raw json.RawMessage) (string, error) {
			var a pollEventsArgs
			if err := json.Unmarshal(orEmptyObject(raw), &a); err != nil {
				return "", fmt.Errorf("arguments: %w", err)
			}
			if a.WaitSeconds < 0 || a.WaitSeconds > maxPollSeconds {
				return "", fmt.Errorf("wait_seconds is %d; it takes 0 to %d", a.WaitSeconds, maxPollSeconds)
			}
			// Looked up, not resolved: a reader names a conversation that
			// exists, and a mistyped name must not mint a new one. Watched
			// here so that the answer can say whether the server had been
			// listening before this call.
			token, named, err := convs.lookup(a.Conversation, a.ConversationToken)
			if err != nil {
				return "", err
			}
			heard, err := events.Watch(token, named)
			if err != nil {
				return "", fmt.Errorf("listen to the conversation's posts: %w", err)
			}
			// A poll that waits holds the conversation's listener for the
			// wait, so a delete or an eviction meanwhile does not drop it.
			if a.WaitSeconds > 0 {
				unpin, err := events.Pin(token)
				if err != nil {
					return "", fmt.Errorf("listen to the conversation's posts: %w", err)
				}
				defer unpin()
			}
			var sb strings.Builder
			// Named by token exactly when no name in the table holds it.
			writeConversation(&sb, a.Conversation, token, !named)
			since := a.Since
			if cur := events.Cursor(); since > cur {
				fmt.Fprintf(&sb, "since %d is past this conversation's cursor %d (this server may have restarted); reading from 0.\n\n", since, cur)
				since = 0
			}
			page := events.Since(ctx, token, since, time.Duration(a.WaitSeconds)*time.Second)
			writeEventPage(&sb, page, heard, since)
			return sb.String(), nil
		},
	}
}

// writeEventPage renders poll_events' frames in cursor order, each frame's
// text inert, and the cursor to read on from.
func writeEventPage(sb *strings.Builder, page EventPage, heard bool, since uint64) {
	if !heard && len(page.Frames) == 0 {
		sb.WriteString("This server had not heard this conversation before; it listens from now on. No events have been heard yet, " +
			"and anything posted before now is lost.\n\n")
	} else if len(page.Frames) == 0 {
		sb.WriteString("No new events.\n\n")
	}
	for _, f := range page.Frames {
		fmt.Fprintf(sb, "Cursor %d. ", f.Cursor)
		writeEventHead(sb, "Report from a card", f)
		sb.WriteString(inertBlock(f.Text))
		sb.WriteString("\n")
	}
	if page.Dropped > 0 {
		fmt.Fprintf(sb, "%d events of this conversation were dropped: this server keeps %d events or %d bytes per conversation, "+
			"and %d bytes across all of them, dropping the oldest.\n", page.Dropped,
			maxEventsPerConversation, maxEventBytesPerConversation, maxEventBytesTotal)
		if page.DroppedNewest > since {
			fmt.Fprintf(sb, "Some of the dropped events were newer than since %d: what is shown is not everything posted since then.\n", since)
		}
	}
	if page.Rejected > 0 {
		fmt.Fprintf(sb, "%d malformed frames were ignored.\n", page.Rejected)
	}
	fmt.Fprintf(sb, "Cursor: %d (pass it as since to read only what comes next).\n", page.Cursor)
}

type taskControlArgs struct {
	Action            string `json:"action"`
	Task              string `json:"task"`
	Conversation      string `json:"conversation"`
	ConversationToken string `json:"conversation_token"`
	WaitSeconds       int    `json:"wait_seconds"`
}

// TaskControlTool reads a task, or stops a conversation's running task.
func TaskControlTool(bus TurnBus, convs *Conversations) Tool {
	return Tool{
		Definition: ToolDefinition{
			Name: ToolTaskControl,
			Description: "Read a task's state and answer (action get, optionally waiting for it to finish), " +
				"or stop the running task of a conversation (action stop).",
			InputSchema: map[string]any{
				"type":     "object",
				"required": []string{"action"},
				"properties": map[string]any{
					"action":             map[string]any{"type": "string", "enum": []string{actionGet, actionStop}},
					"task":               map[string]any{"type": "string", "description": "The task to read (get): <addressee>/<task-id> as send_message reports it, or a task id alone."},
					"conversation":       map[string]any{"type": "string", "description": "The conversation whose task to stop (stop). Default: " + DefaultConversation + "."},
					"conversation_token": map[string]any{"type": "string", "description": "The conversation, by token (stop)."},
					"wait_seconds":       map[string]any{"type": "integer", "description": fmt.Sprintf("For get: wait up to this long for the task to finish, %d at most. Default 0.", maxWaitSeconds)},
				},
			},
		},
		Call: func(ctx context.Context, raw json.RawMessage) (string, error) {
			ctx, cancel := context.WithTimeout(ctx, MaxTurnWait)
			defer cancel()
			var a taskControlArgs
			if err := json.Unmarshal(orEmptyObject(raw), &a); err != nil {
				return "", fmt.Errorf("arguments: %w", err)
			}
			switch a.Action {
			case actionGet:
				ref, err := resolveTask(ctx, bus, a.Task)
				if err != nil {
					return "", err
				}
				var task *lib.Task
				if a.WaitSeconds > 0 {
					task, err = bus.WaitForTask(ctx, ref, waitFor(a.WaitSeconds))
				} else {
					task, err = bus.ReadTask(ctx, ref)
				}
				if err != nil {
					return "", err
				}
				var sb strings.Builder
				writeTask(&sb, ref, task)
				return sb.String(), nil
			case actionStop:
				token, byToken, err := convs.ResolveCall(a.Conversation, a.ConversationToken)
				if err != nil {
					return "", err
				}
				res, err := bus.SendStop(ctx, convs, token)
				if err != nil {
					return "", err
				}
				var sb strings.Builder
				writeConversation(&sb, a.Conversation, token, byToken)
				sb.WriteString("Sent stop.\n")
				writeNotices(&sb, res.Notices)
				return sb.String(), nil
			default:
				return "", fmt.Errorf("unknown action %q; use %s or %s", a.Action, actionGet, actionStop)
			}
		},
	}
}

// resolveTask reads a task reference, resolving a bare id's addressee from its
// submission.
func resolveTask(ctx context.Context, bus TurnBus, task string) (TaskRef, error) {
	if task == "" {
		return TaskRef{}, errors.New("get needs a task")
	}
	if strings.Contains(task, taskRefSep) {
		return ParseTaskRef(task, "")
	}
	if !lib.ValidSubjectToken(task) {
		return TaskRef{}, fmt.Errorf("%q is not a task id", task)
	}
	addressee, err := bus.ResolveAddressee(ctx, task)
	if err != nil {
		return TaskRef{}, err
	}
	return ParseTaskRef(task, addressee)
}

func orEmptyObject(raw json.RawMessage) json.RawMessage {
	if len(raw) == 0 || string(raw) == "null" {
		return json.RawMessage("{}")
	}
	return raw
}

func waitFor(seconds int) time.Duration {
	switch {
	case seconds <= 0:
		seconds = defaultWaitSeconds
	case seconds > maxWaitSeconds:
		seconds = maxWaitSeconds
	}
	return time.Duration(seconds) * time.Second
}

// writeConversation names the conversation a call used. A call that named a
// token used that conversation whatever alias came with it, so it is named
// by the token alone; otherwise the alias is the caller's, so it is shown as
// an inert cell.
func writeConversation(sb *strings.Builder, alias, token string, byToken bool) {
	if byToken {
		fmt.Fprintf(sb, "Conversation by token `%s`.\n\n", token)
		return
	}
	if alias == "" {
		alias = DefaultConversation
	}
	fmt.Fprintf(sb, "Conversation %s (token `%s`).\n\n", cell(alias), token)
}

// writeNotices quotes each notice whole, line breaks and all: for a settled
// turn the notice is the answer, and there is no task to read it from later.
func writeNotices(sb *strings.Builder, notices []string) {
	if len(notices) == 0 {
		return
	}
	sb.WriteString("Gateway notices:\n\n")
	for _, n := range notices {
		n = cutText(n, "\n…(notice shortened)")
		sb.WriteString("> " + strings.ReplaceAll(n, "\n", "\n> ") + "\n\n")
	}
}

// writeTask renders a task's state and, once it has one, its answer. The
// answer is the agents' own markdown, shown as written: it is what the
// caller asked for. Progress shows while the task runs.
func writeTask(sb *strings.Builder, ref TaskRef, t *lib.Task) {
	if t == nil {
		fmt.Fprintf(sb, "Task `%s`: no events yet.\n", ref)
		return
	}
	fmt.Fprintf(sb, "Task `%s`: **%s**", ref, t.State)
	if !t.Final {
		sb.WriteString(" (still running; read it again with task_control get)")
	}
	sb.WriteString("\n\n")
	if answer := artifactText(t, lib.ArtifactResult); answer != "" {
		sb.WriteString(capText(answer))
		sb.WriteString("\n")
	} else if t.Final && t.FinalMessage != nil {
		if msg := partsText(t.FinalMessage.Parts); msg != "" {
			sb.WriteString(capText(msg))
			sb.WriteString("\n")
		}
	} else if p := artifactText(t, lib.ArtifactProgress); p != "" && !t.Final {
		fmt.Fprintf(sb, "Progress: %s\n", cell(lastLine(p)))
	}
}

// writeOutcome renders a turn: its task, the tasks delegated from it, and the
// answer from whichever of them holds it.
func writeOutcome(sb *strings.Builder, out TurnOutcome) {
	if len(out.Followed) == 0 || out.AnswerFrom == nil || *out.AnswerFrom == out.Root {
		writeTask(sb, out.Root, out.Task)
		writeFollowed(sb, out.Followed)
		return
	}
	state := "no events yet"
	if out.Task != nil {
		state = string(out.Task.State)
	}
	fmt.Fprintf(sb, "Task `%s`: **%s**; the answer came from a delegated task.\n\n", out.Root, state)
	writeTask(sb, *out.AnswerFrom, out.Answer)
	writeFollowed(sb, out.Followed)
}

func writeFollowed(sb *strings.Builder, followed []FollowedTask) {
	if len(followed) == 0 {
		return
	}
	sb.WriteString("\nDelegated tasks:\n")
	for _, f := range followed {
		switch {
		case f.Err != nil:
			fmt.Fprintf(sb, "- `%s`: not read (%s)\n", f.Ref, cell(f.Err.Error()))
		case f.Task == nil:
			fmt.Fprintf(sb, "- `%s`: no events yet\n", f.Ref)
		default:
			fmt.Fprintf(sb, "- `%s`: %s\n", f.Ref, f.Task.State)
		}
	}
}

func artifactText(t *lib.Task, name string) string {
	a := t.Artifact(name)
	if a == nil {
		return ""
	}
	return partsText(a.Parts)
}

func partsText(parts []lib.Part) string {
	var sb strings.Builder
	for _, p := range parts {
		if p.Text != "" {
			sb.WriteString(p.Text)
		}
	}
	return strings.TrimSpace(sb.String())
}

func lastLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.LastIndex(s, "\n"); i >= 0 {
		return s[i+1:]
	}
	return s
}

func capText(s string) string {
	return cutText(s, "\n\n…(answer shortened; the full answer is on the task)")
}

// cutText cuts s to maxAnswerBytes on a rune boundary, marking the cut with
// suffix.
func cutText(s, suffix string) string {
	if len(s) <= maxAnswerBytes {
		return s
	}
	cut := maxAnswerBytes
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut] + suffix
}
