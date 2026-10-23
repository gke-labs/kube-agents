package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/slack-go/slack"
)

// Answer feedback on Slack (feedback.go): Slack's own feedback buttons, a
// context_actions block under the final answer of an ask, the last block of
// the message (inside its side bar when it has one, where Slack draws it as
// well). A tap is never a turn. A 👍 is handed to the gateway and Slack
// fills the icon; nothing posts. A 👎 is kept with the rated answer (the
// gateway adds the ask that started the chain), and only once it is kept
// opens a modal asking what went wrong, optionally; Send adds the note and
// thanks the rater with an ephemeral line in the thread, and Cancel leaves
// the 👎 as it is. The controls are drawn only once the gateway has handed
// over its recorder.
const (
	// slackFeedbackAction is the feedback buttons' action_id; the answer's
	// task id rides in the block's id after slackFeedbackBlockPrefix.
	slackFeedbackAction      = slackChatActionPrefix + ".feedback"
	slackFeedbackBlockPrefix = slackChatActionPrefix + ".feedback."
	// slackFeedbackCallback is the modal's callback_id, and the input's
	// block and action ids.
	slackFeedbackCallback = slackChatActionPrefix + ".feedback-note"
	slackFeedbackNoteID   = "note"

	slackContextActionsBlock = "context_actions"
	slackFeedbackButtons     = "feedback_buttons"
	slackViewSubmission      = "view_submission"

	// The words the controls and the modal show (the approved mock,
	// phase2/feedback).
	slackFeedbackGood     = "Good answer"
	slackFeedbackBad      = "Bad answer"
	slackFeedbackTitle    = "What went wrong?"
	slackFeedbackLabel    = "Anything else?"
	slackFeedbackSubmit   = "Send"
	slackFeedbackClose    = "Cancel"
	slackFeedbackKeptLine = "Your question and this answer are kept on your install for %s so an operator can turn them into a test."
	// slackFeedbackKeptNoAsk is the kept line when the ask is no longer on
	// record (AskTTL, a reaped session), so only the answer is kept.
	slackFeedbackKeptNoAsk  = "This answer is kept on your install for %s so an operator can turn it into a test."
	slackFeedbackDays       = "%d days"
	slackFeedbackOneDay     = "1 day"
	slackFeedbackThanksLine = "Thanks. I kept this answer for your team to review."
	// slackFeedbackFormLine points the rater at the project's feedback form,
	// with nothing pre-filled: the install never files anything itself.
	slackFeedbackFormLine = "To tell the kube-agents maintainers as well, use <%s>. It opens a public issue, so leave out cluster names and other details from your install."
	slackFeedbackFormURL  = "https://gke-labs.github.io/kube-agents/feedback"

	// slackFeedbackDeadline bounds the Slack calls one tap makes.
	slackFeedbackDeadline = 15 * time.Second
	// slackFeedbackSeenPrefix keys a tap in the at-least-once dedupe ring.
	slackFeedbackSeenPrefix = "feedback:"
)

// slackFeedbackAPI is the part of the Web API answer feedback calls,
// beyond slackAPI; nil in a test that drives none of it.
type slackFeedbackAPI interface {
	OpenViewContext(ctx context.Context, triggerID string, view slack.ModalViewRequest) (*slack.ViewResponse, error)
	PostEphemeralContext(ctx context.Context, channelID, userID string, options ...slack.MsgOption) (string, error)
}

// slackFeedbackMeta is what the modal carries for its submission.
type slackFeedbackMeta struct {
	Conversation string `json:"c"`
	Task         string `json:"t"`
	Channel      string `json:"ch"`
	Thread       string `json:"th,omitempty"`
	Message      string `json:"m,omitempty"`
}

// slackViewPayload is the part of a view_submission payload the adapter
// reads.
type slackViewPayload struct {
	Type string `json:"type"`
	User struct {
		ID     string `json:"id"`
		TeamID string `json:"team_id"`
	} `json:"user"`
	View struct {
		// ID and Hash name one submission of one view, the same across a
		// Socket Mode redelivery.
		ID              string `json:"id"`
		Hash            string `json:"hash"`
		CallbackID      string `json:"callback_id"`
		PrivateMetadata string `json:"private_metadata"`
		State           struct {
			Values map[string]map[string]struct {
				Value string `json:"value"`
			} `json:"values"`
		} `json:"state"`
	} `json:"view"`
}

// SetFeedbackRecorder turns the rating controls on (FeedbackSink).
func (s *SlackAdapter) SetFeedbackRecorder(r FeedbackRecorder) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.feedback = r
}

func (s *SlackAdapter) feedbackRecorder() FeedbackRecorder {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.feedback
}

// rates reports whether a message is drawn with rating controls.
func (s *SlackAdapter) rates(m OutMessage) bool {
	return m.Feedback != "" && s.feedbackRecorder() != nil && s.feedbackAPI != nil
}

// slackFeedbackBlock is the rating controls for the answer marked taskID.
func slackFeedbackBlock(taskID string) map[string]any {
	button := func(label, value string) map[string]any {
		return map[string]any{"text": map[string]any{"type": slackPlainText, "text": label},
			"value": value, "accessibility_label": label}
	}
	return map[string]any{
		"type":     slackContextActionsBlock,
		"block_id": slackFeedbackBlockPrefix + taskID,
		"elements": []any{map[string]any{
			"type":            slackFeedbackButtons,
			"action_id":       slackFeedbackAction,
			"positive_button": button(slackFeedbackGood, FeedbackUp),
			"negative_button": button(slackFeedbackBad, FeedbackDown),
		}},
	}
}

// withFeedback adds the rating controls for taskID as the layout's last
// block: in its side bar when it has one, since an attachment draws after
// every top-level block. A layout already at Slack's block cap is left as it
// is: the answer posts laid out, without the controls.
func withFeedback(layout slackChatLayout, taskID string) slackChatLayout {
	block := slackFeedbackBlock(taskID)
	if n := len(layout.Attachments); n > 0 {
		last := layout.Attachments[n-1]
		inner, _ := last["blocks"].([]map[string]any)
		if len(inner) < slackChatBlocksMax {
			last["blocks"] = append(inner, block)
		}
		return layout
	}
	if len(layout.Blocks) < slackChatBlocksMax {
		layout.Blocks = append(layout.Blocks, block)
	}
	return layout
}

// slackRatedOptions is slackMessageOptions for an answer drawn with rating
// controls, refusals wrapped the same way.
func slackRatedOptions(m OutMessage) ([]slack.MsgOption, error) {
	if m.Chat == nil {
		return slackRatedTextOptions(m)
	}
	text := toMrkdwn(m.Text)
	if utf8.RuneCountInString(text) > slackChatTextMax {
		return nil, fmt.Errorf("%w: %w", ErrLayoutRefused, errSlackChatTooLarge)
	}
	layout, err := renderSlackChat(m.Chat, text)
	if err != nil {
		return nil, fmt.Errorf("%w: %w", ErrLayoutRefused, err)
	}
	return slackChatOptions(withFeedback(layout, m.Feedback), text)
}

// slackRatedTextOptions is a text-only answer's last chunk with the rating
// controls: the text as sections, each under a section's cap (escaping can
// take a chunk past one), and the controls under them.
func slackRatedTextOptions(m OutMessage) ([]slack.MsgOption, error) {
	text := toMrkdwn(m.Text)
	var blocks []map[string]any
	for _, part := range slackSectionParts(text) {
		blocks = append(blocks, map[string]any{"type": slackSectionBlock, "text": map[string]any{"type": slackMrkdwnText, "text": part}})
	}
	if len(blocks) >= slackChatBlocksMax {
		return nil, fmt.Errorf("%w: %w", ErrLayoutRefused, errSlackChatTooLarge)
	}
	set, err := slackRawBlocks(append(blocks, slackFeedbackBlock(m.Feedback)))
	if err != nil {
		return nil, err
	}
	return []slack.MsgOption{slack.MsgOptionText(text, false), slack.MsgOptionBlocks(set...)}, nil
}

// slackSectionParts cuts text into pieces of at most slackChatSectionMax
// code points, at a line break where one falls in the second half of a
// piece.
func slackSectionParts(text string) []string {
	var parts []string
	r := []rune(text)
	for len(r) > slackChatSectionMax {
		cut := slackChatSectionMax
		for i := slackChatSectionMax - 1; i > slackChatSectionMax/2; i-- {
			if r[i] == '\n' {
				cut = i + 1
				break
			}
		}
		parts = append(parts, string(r[:cut]))
		r = r[cut:]
	}
	return append(parts, string(r))
}

// feedbackClick handles a tap on the rating controls: never a turn.
func (s *SlackAdapter) feedbackClick(p slackClickPayload, actionType, blockID, value string) {
	recorder := s.feedbackRecorder()
	taskID, ok := strings.CutPrefix(blockID, slackFeedbackBlockPrefix)
	if recorder == nil || !ok || taskID == "" || (value != FeedbackUp && value != FeedbackDown) {
		// Ids and the rating's value only, never text: a tap the gateway
		// cannot read is otherwise indistinguishable from no tap at all.
		s.log.Info("slack: dropping a rating tap the gateway cannot read",
			"actionType", actionType, "actionId", slackFeedbackAction, "blockId", blockID, "value", value,
			"feedbackOn", recorder != nil, "channel", p.Channel.ID)
		return
	}
	if p.User.ID == "" || p.Channel.ID == "" || p.Message.TS == "" || p.ActionTS == "" {
		s.log.Info("slack: dropping a rating tap with ids missing",
			"blockId", blockID, "user", p.User.ID != "", "channel", p.Channel.ID != "",
			"message", p.Message.TS != "", "actionTs", p.ActionTS != "")
		return
	}
	if s.otherWorkspace(p.User.TeamID) || s.otherWorkspace(p.Team.ID) {
		s.log.Info("slack: ignoring a rating from another workspace's member",
			"userTeam", p.User.TeamID, "team", s.teamID, "channel", p.Channel.ID)
		return
	}
	if s.alreadySeen(slackFeedbackSeenPrefix + p.Channel.ID + "/" + p.ActionTS) {
		return
	}
	if !recorder.Admits(slackBackend, p.User.ID) {
		s.log.Info("slack: dropping a rating from an unverified sender", "author", p.User.ID, "channel", p.Channel.ID)
		return
	}
	conversation, thread, _ := slackClickConversation(p)
	// The message's text is the mrkdwn fallback the adapter posted. Its
	// &, < and > entities are undone; the rest of the mrkdwn rewrite
	// (headings, bold, links) stays as Slack showed it.
	rating := FeedbackRating{Platform: platformSlack, Backend: slackBackend, Rater: p.User.ID,
		Conversation: conversation, MessageID: p.Message.TS, TaskID: taskID, Rating: value,
		Answer: slackUnescaper.Replace(p.Message.Text)}
	s.goSession(func() {
		// Kept first, so the note the modal sends and a 👍 that follows find
		// the 👎 already there: a session read and a merge (a read and a
		// write, again on a race), which must finish inside the three seconds the
		// modal's trigger lasts.
		askKept, err := recorder.Rate(rating)
		if err != nil {
			// The modal says the answer is kept; with nothing kept it
			// does not open.
			s.log.Warn("slack: recording a rating failed", "channel", p.Channel.ID, "taskId", taskID, "err", err)
			return
		}
		if value != FeedbackDown {
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), slackFeedbackDeadline)
		defer cancel()
		meta := slackFeedbackMeta{Conversation: conversation, Task: taskID, Channel: p.Channel.ID, Thread: thread, Message: p.Message.TS}
		if _, err := s.feedbackAPI.OpenViewContext(ctx, p.TriggerID, slackFeedbackModal(meta, recorder.KeptDays(), askKept)); err != nil {
			s.log.Warn("slack: opening the feedback note failed; the 👎 is kept without one", "channel", p.Channel.ID, "err", err)
		}
	})
}

// slackFeedbackThanks is the line a kept note is thanked with.
func slackFeedbackThanks() string {
	return slackFeedbackThanksLine + " " + fmt.Sprintf(slackFeedbackFormLine, slackFeedbackFormURL)
}

// slackFeedbackModal is the note a 👎 asks for; its kept line names the
// question only when askKept.
func slackFeedbackModal(meta slackFeedbackMeta, days int, askKept bool) slack.ModalViewRequest {
	raw, _ := json.Marshal(meta)
	kept := fmt.Sprintf(slackFeedbackDays, days)
	if days == 1 {
		kept = slackFeedbackOneDay
	}
	line := slackFeedbackKeptLine
	if !askKept {
		line = slackFeedbackKeptNoAsk
	}
	input := slack.NewInputBlock(slackFeedbackNoteID, slack.NewTextBlockObject(slackPlainText, slackFeedbackLabel, false, false), nil,
		slack.NewPlainTextInputBlockElement(nil, slackFeedbackNoteID).WithMaxLength(feedbackNoteChars))
	input.Optional = true
	return slack.ModalViewRequest{
		Type:            slack.VTModal,
		CallbackID:      slackFeedbackCallback,
		Title:           slack.NewTextBlockObject(slackPlainText, slackFeedbackTitle, false, false),
		Submit:          slack.NewTextBlockObject(slackPlainText, slackFeedbackSubmit, false, false),
		Close:           slack.NewTextBlockObject(slackPlainText, slackFeedbackClose, false, false),
		PrivateMetadata: string(raw),
		Blocks: slack.Blocks{BlockSet: []slack.Block{
			input,
			slack.NewContextBlock("", slack.NewTextBlockObject(slackPlainText, fmt.Sprintf(line, kept), false, false)),
		}},
	}
}

// feedbackNote handles the modal's Send: the note joins the 👎, and the
// rater is thanked in the thread, to them alone.
func (s *SlackAdapter) feedbackNote(raw json.RawMessage) {
	var p slackViewPayload
	if err := json.Unmarshal(raw, &p); err != nil || p.View.CallbackID != slackFeedbackCallback || p.User.ID == "" {
		return
	}
	recorder := s.feedbackRecorder()
	var meta slackFeedbackMeta
	if recorder == nil || json.Unmarshal([]byte(p.View.PrivateMetadata), &meta) != nil || meta.Task == "" || meta.Channel == "" {
		return
	}
	if s.otherWorkspace(p.User.TeamID) {
		return
	}
	// At-least-once: a redelivered Send must not thank the rater twice.
	if p.View.ID != "" && s.alreadySeen(slackFeedbackSeenPrefix+p.View.ID+"/"+p.View.Hash) {
		return
	}
	note := strings.TrimSpace(p.View.State.Values[slackFeedbackNoteID][slackFeedbackNoteID].Value)
	rating := FeedbackRating{Platform: platformSlack, Backend: slackBackend, Rater: p.User.ID,
		Conversation: meta.Conversation, MessageID: meta.Message, TaskID: meta.Task, Rating: FeedbackDown}
	s.goSession(func() {
		// An empty note is still written: the thanks says the answer was
		// kept, so it posts only once it is.
		if err := recorder.Note(rating, note); err != nil {
			s.log.Warn("slack: keeping a feedback note failed", "channel", meta.Channel, "taskId", meta.Task, "err", err)
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), slackFeedbackDeadline)
		defer cancel()
		opts := []slack.MsgOption{slack.MsgOptionText(slackFeedbackThanks(), false)}
		if meta.Thread != "" {
			opts = append(opts, slack.MsgOptionTS(meta.Thread))
		}
		if _, err := s.feedbackAPI.PostEphemeralContext(ctx, meta.Channel, p.User.ID, opts...); err != nil {
			s.log.Warn("slack: thanking a rater failed", "channel", meta.Channel, "err", err)
		}
	})
}
