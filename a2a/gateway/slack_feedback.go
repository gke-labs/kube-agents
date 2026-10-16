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
// fills the icon; nothing posts. A 👎 is kept with the rated answer first,
// then opens a modal asking what was wrong, optionally, while the adapter
// reads the ask (the last message a person wrote before the answer) and adds
// it; Send adds the note and thanks the rater with an ephemeral line in the
// thread, and Cancel leaves the 👎 as it is. Nothing else of the thread is
// kept. The controls are drawn only once the gateway has handed over its
// recorder.
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
	slackFeedbackGood       = "Good answer"
	slackFeedbackBad        = "Bad answer"
	slackFeedbackTitle      = "What went wrong?"
	slackFeedbackLabel      = "What was wrong?"
	slackFeedbackSubmit     = "Send"
	slackFeedbackClose      = "Cancel"
	slackFeedbackKeptLine   = "Your question and this answer are kept on your install for %s so an operator can turn them into a test."
	slackFeedbackDays       = "%d days"
	slackFeedbackOneDay     = "1 day"
	slackFeedbackThanksLine = "Thanks. I kept this answer for your team to review."

	// slackFeedbackDeadline bounds the Slack calls one tap makes.
	slackFeedbackDeadline = 15 * time.Second
	// slackFeedbackRepliesPage and slackFeedbackRepliesPages bound the read
	// of the thread a 👎's ask is found in; slackFeedbackHistory is how far
	// back a top-level answer (a DM's thread-less conversation) looks.
	slackFeedbackRepliesPage  = 200
	slackFeedbackRepliesPages = 5
	slackFeedbackHistory      = 10
)

// slackFeedbackAPI is the part of the Web API answer feedback calls,
// beyond slackAPI; nil in a test that drives none of it.
type slackFeedbackAPI interface {
	OpenViewContext(ctx context.Context, triggerID string, view slack.ModalViewRequest) (*slack.ViewResponse, error)
	PostEphemeralContext(ctx context.Context, channelID, userID string, options ...slack.MsgOption) (string, error)
	GetConversationRepliesContext(ctx context.Context, params *slack.GetConversationRepliesParameters) ([]slack.Message, bool, string, error)
	GetConversationHistoryContext(ctx context.Context, params *slack.GetConversationHistoryParameters) (*slack.GetConversationHistoryResponse, error)
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
// controls: the text as one section, the controls under it.
func slackRatedTextOptions(m OutMessage) ([]slack.MsgOption, error) {
	text := toMrkdwn(m.Text)
	if utf8.RuneCountInString(text) > slackChatSectionMax {
		return nil, fmt.Errorf("%w: %w", ErrLayoutRefused, errSlackChatTooLarge)
	}
	set, err := slackRawBlocks([]map[string]any{
		{"type": slackSectionBlock, "text": map[string]any{"type": slackMrkdwnText, "text": text}},
		slackFeedbackBlock(m.Feedback)})
	if err != nil {
		return nil, err
	}
	return []slack.MsgOption{slack.MsgOptionText(text, false), slack.MsgOptionBlocks(set...)}, nil
}

// feedbackClick handles a tap on the rating controls: never a turn.
func (s *SlackAdapter) feedbackClick(p slackClickPayload, blockID, value string) {
	recorder := s.feedbackRecorder()
	taskID, ok := strings.CutPrefix(blockID, slackFeedbackBlockPrefix)
	if recorder == nil || !ok || taskID == "" || (value != FeedbackUp && value != FeedbackDown) {
		return
	}
	if p.User.ID == "" || p.Channel.ID == "" || p.Message.TS == "" || p.ActionTS == "" {
		return
	}
	if s.otherWorkspace(p.User.TeamID) || s.otherWorkspace(p.Team.ID) {
		s.log.Info("slack: ignoring a rating from another workspace's member",
			"userTeam", p.User.TeamID, "team", s.teamID, "channel", p.Channel.ID)
		return
	}
	if s.alreadySeen("feedback:" + p.Channel.ID + "/" + p.ActionTS) {
		return
	}
	if !recorder.Admits(slackBackend, p.User.ID) {
		s.log.Info("slack: dropping a rating from an unverified sender", "author", p.User.ID, "channel", p.Channel.ID)
		return
	}
	conversation, thread, _ := slackClickConversation(p)
	rating := FeedbackRating{Platform: platformSlack, Backend: slackBackend, Rater: p.User.ID,
		Conversation: conversation, MessageID: p.Message.TS, TaskID: taskID, Rating: value}
	if value == FeedbackDown {
		rating.Answer = p.Message.Text
	}
	s.goSession(func() {
		// Kept first, so the note the modal sends and a 👍 that follows find
		// the 👎 already there; one KV write, inside the three seconds the
		// modal's trigger lasts.
		if err := recorder.Rate(rating); err != nil {
			s.log.Warn("slack: recording a rating failed", "channel", p.Channel.ID, "taskId", taskID, "err", err)
		}
		if value != FeedbackDown {
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), slackFeedbackDeadline)
		defer cancel()
		meta := slackFeedbackMeta{Conversation: conversation, Task: taskID, Channel: p.Channel.ID, Thread: thread, Message: p.Message.TS}
		if _, err := s.feedbackAPI.OpenViewContext(ctx, p.TriggerID, slackFeedbackModal(meta, recorder.KeptDays())); err != nil {
			s.log.Warn("slack: opening the feedback note failed; the 👎 is kept without one", "channel", p.Channel.ID, "err", err)
		}
		if ask := s.feedbackAsk(ctx, p.Channel.ID, thread, p.Message.TS); ask != "" {
			if err := recorder.AddAsk(rating, ask); err != nil {
				s.log.Warn("slack: adding the ask to a rating failed", "channel", p.Channel.ID, "taskId", taskID, "err", err)
			}
		}
	})
}

// slackFeedbackModal is the note a 👎 asks for.
func slackFeedbackModal(meta slackFeedbackMeta, days int) slack.ModalViewRequest {
	raw, _ := json.Marshal(meta)
	kept := fmt.Sprintf(slackFeedbackDays, days)
	if days == 1 {
		kept = slackFeedbackOneDay
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
			slack.NewContextBlock("", slack.NewTextBlockObject(slackPlainText, fmt.Sprintf(slackFeedbackKeptLine, kept), false, false)),
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
		opts := []slack.MsgOption{slack.MsgOptionText(slackFeedbackThanksLine, false)}
		if meta.Thread != "" {
			opts = append(opts, slack.MsgOptionTS(meta.Thread))
		}
		if _, err := s.feedbackAPI.PostEphemeralContext(ctx, meta.Channel, p.User.ID, opts...); err != nil {
			s.log.Warn("slack: thanking a rater failed", "channel", meta.Channel, "err", err)
		}
	})
}

// feedbackAsk reads the conversation up to the rated message for its ask:
// the last message a person wrote before it, as an eval prompt reads it (the
// bot's mention gone, spaces collapsed). Slack timestamps compare as
// strings: whole seconds of a fixed width, then the fraction. A read that
// fails finds what it can; the rating is kept either way.
func (s *SlackAdapter) feedbackAsk(ctx context.Context, channel, thread, ratedTS string) string {
	var msgs []slack.Message
	if thread != "" && thread != ratedTS {
		params := &slack.GetConversationRepliesParameters{ChannelID: channel, Timestamp: thread,
			Latest: ratedTS, Inclusive: true, Limit: slackFeedbackRepliesPage}
		for range slackFeedbackRepliesPages {
			page, more, cursor, err := s.feedbackAPI.GetConversationRepliesContext(ctx, params)
			if err != nil {
				s.log.Warn("slack: reading a rated thread failed", "channel", channel, "err", err)
				break
			}
			msgs = append(msgs, page...)
			if !more || cursor == "" {
				break
			}
			params.Cursor = cursor
		}
	} else {
		// A top-level answer: the channel's last few messages up to it,
		// which Slack returns newest first.
		resp, err := s.feedbackAPI.GetConversationHistoryContext(ctx, &slack.GetConversationHistoryParameters{
			ChannelID: channel, Latest: ratedTS, Inclusive: true, Limit: slackFeedbackHistory})
		if err != nil {
			s.log.Warn("slack: reading a rated conversation failed", "channel", channel, "err", err)
		} else {
			for i := len(resp.Messages) - 1; i >= 0; i-- {
				msgs = append(msgs, resp.Messages[i])
			}
		}
	}
	ask := ""
	for _, m := range msgs {
		agent := m.BotID != "" || (s.botUserID != "" && m.User == s.botUserID)
		if m.Timestamp < ratedTS && !agent {
			ask = m.Text
		}
	}
	return strings.Join(strings.Fields(slackEntitiesReduced(ask)), " ")
}
