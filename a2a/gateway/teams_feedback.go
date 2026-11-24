package gateway

import (
	"strings"
)

// Answer feedback on Teams (spec-chatops-gateway.md, "Answer feedback"): the
// two rating controls on the card of an answer the relay marked
// (OutMessage.Feedback), drawn once the gateway has handed the adapter its
// recorder, as on Google Chat and Slack.
//
// 👍 is an Action.Submit, a press like the Stop button's (teams_press.go).
// 👎 is an Action.ShowCard: it unfolds a small form inside the answer's
// card, on the presser's screen alone, with a box for what went wrong and
// a Send button, and Send is the press, carrying the note as an input
// beside the rating. Teams has neither Slack's modal (opening one needs an
// answer to the press that the queue cannot give) nor Google Chat's private
// note card, and the unfolded form asks the same question without either.
// So a 👎 is recorded when it is sent, with the note or without one, and
// not at the first tap.
//
// A rating never reaches the gateway as a turn. The adapter checks the
// presser as the gateway checks a sender (FeedbackRecorder.Admits), hands
// the rating to the recorder off the pull loop, and redraws the card with a
// line saying a rating was recorded: the card is the conversation's, so the
// line says that and not who rated or how. The recorder keeps one record
// per rated answer and rater, so a second rating by the same person
// replaces the first, and the count moves only when the rating changes.
const (
	teamsActionShowCard  = "Action.ShowCard"
	teamsInputText       = "Input.Text"
	teamsPressActionRate = "rate"
	teamsPressRatingKey  = "rating"
	teamsPressNoteKey    = "note"
	teamsRatingUpTitle   = "👍"
	teamsRatingDownTitle = "👎"
	// teamsNoteOptional marks the note's question as one the rater may
	// leave unanswered: Send with an empty box is a 👎 alone.
	teamsNoteOptional = " (optional)"
	// teamsRatingRecordedLine is what the card says once a rating on it
	// was recorded.
	teamsRatingRecordedLine = "_Feedback recorded. Thank you._"
	// teamsRatedCap bounds the rated-message memory; the oldest goes
	// first, and a press on a message no longer held is still recorded,
	// without the answer's text and without the redraw.
	teamsRatedCap = 512
)

// teamsRated is a message posted with rating controls: the task it is
// rated under, its text, and whether a rating on it was recorded.
type teamsRated struct {
	task     string
	text     string
	recorded bool
}

var (
	_ FeedbackSink = (*TeamsAdapter)(nil)
)

// SetFeedbackRecorder hands the adapter the gateway's feedback desk
// (FeedbackSink).
func (a *TeamsAdapter) SetFeedbackRecorder(r FeedbackRecorder) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.recorder = r
}

func (a *TeamsAdapter) feedbackRecorder() FeedbackRecorder {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.recorder
}

// rates reports whether m is posted with rating controls: an answer the
// relay marked, as text, on an adapter that holds the recorder. A mark that
// names a post the agent made on its own (an alert its triage edits, a
// report) draws none: Teams rates answers only.
func (a *TeamsAdapter) rates(m OutMessage) bool {
	return m.Chat == nil && m.Feedback != "" && postKindOf(m.Feedback) == "" && a.feedbackRecorder() != nil
}

// teamsRatingActions is the rating controls for the message rated under
// task, kept for days: 👍, and 👎 with the note form it unfolds.
func teamsRatingActions(task string, days int) []map[string]any {
	good, bad := feedbackLabels(task)
	rate := func(rating string) map[string]any {
		return map[string]any{teamsPressActionKey: teamsPressActionRate, teamsPressTaskKey: task, teamsPressRatingKey: rating}
	}
	return []map[string]any{
		{"type": teamsActionSubmit, "title": teamsRatingUpTitle, "tooltip": good, "data": rate(FeedbackUp)},
		{
			"type": teamsActionShowCard, "title": teamsRatingDownTitle, "tooltip": bad,
			"card": map[string]any{
				"type": teamsCardType,
				"body": []map[string]any{
					{
						"type": teamsInputText, "id": teamsPressNoteKey, "isMultiline": true,
						"maxLength": feedbackNoteChars, "placeholder": feedbackNoteTitleFor(task) + teamsNoteOptional,
					},
					{"type": "TextBlock", "wrap": true, "isSubtle": true, "size": "Small", "text": feedbackKeptTextFor(task, days, true)},
				},
				teamsCardActionsKey: []map[string]any{
					{"type": teamsActionSubmit, "title": feedbackNoteSend, "data": rate(FeedbackDown)},
				},
			},
		},
	}
}

// postRated posts an answer with its rating controls and remembers it.
func (a *TeamsAdapter) postRated(conversation string, m OutMessage) (string, error) {
	id, err := a.post(conversation, m.Text, teamsRatingActions(m.Feedback, a.feedbackRecorder().KeptDays()))
	if err == nil {
		a.rememberRated(id, m.Feedback, m.Text)
	}
	return id, err
}

// editRated edits an answer onto messageID with its rating controls (the
// whole answer taken on the task's line) and remembers it.
func (a *TeamsAdapter) editRated(conversation, messageID string, m OutMessage) error {
	err := a.edit(conversation, messageID, m.Text, teamsRatingActions(m.Feedback, a.feedbackRecorder().KeptDays()))
	if err == nil {
		a.rememberRated(messageID, m.Feedback, m.Text)
	}
	return err
}

// rememberRated holds a rated message for its presses; the oldest is
// evicted at teamsRatedCap.
func (a *TeamsAdapter) rememberRated(id, task, text string) {
	if id == "" {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if _, held := a.rated[id]; !held {
		a.ratedOrder = append(a.ratedOrder, id)
		if len(a.ratedOrder) > teamsRatedCap {
			delete(a.rated, a.ratedOrder[0])
			a.ratedOrder = a.ratedOrder[1:]
		}
	}
	a.rated[id] = teamsRated{task: task, text: text}
}

// feedbackPress checks a rating press and hands it on, and answers why the
// press is not a turn. The caller (classify) has checked the conversation,
// the service URL, the tenant and that the sender has an id.
func (a *TeamsAdapter) feedbackPress(act *teamsActivity, press teamsPress, conversation string) string {
	recorder := a.feedbackRecorder()
	if recorder == nil {
		return "rating press while answer feedback is off"
	}
	if press.Task == "" {
		return "rating press names no task"
	}
	if press.Rating != FeedbackUp && press.Rating != FeedbackDown {
		return "rating press carries no rating this adapter draws"
	}
	message := strings.TrimSpace(act.ReplyToID)
	if message == "" {
		return "rating press names no message"
	}
	rater, dup := a.admit(act, conversation)
	if dup {
		return "duplicate delivery of " + act.ID
	}
	if !recorder.Admits(teamsBackend, rater) {
		return "rating press from a sender the install does not serve"
	}
	rating := FeedbackRating{
		Platform: platformTeams, Backend: teamsBackend, Rater: rater,
		Conversation: teamsConversationID(conversation), MessageID: message,
		TaskID: press.Task, Rating: press.Rating,
	}
	a.rating.Add(1)
	go func() {
		defer a.rating.Done()
		a.rate(recorder, rating, press.Note)
	}()
	return "rating press, recorded off the pull loop"
}

// rate records one rating, adds a 👎's note when one was typed, and redraws
// the rated card the first time a rating on it is recorded. A press on a
// message the adapter no longer holds is recorded without the answer's
// text, and the card is left as it is.
func (a *TeamsAdapter) rate(recorder FeedbackRecorder, r FeedbackRating, note string) {
	a.rateMu.Lock()
	defer a.rateMu.Unlock()
	a.mu.Lock()
	rated, held := a.rated[r.MessageID]
	a.mu.Unlock()
	if held && rated.task == r.TaskID {
		r.Answer = rated.text
	}
	if _, err := recorder.Rate(r); err != nil {
		a.log.Warn("teams answer rating was not kept", "conversation", r.Conversation, "taskId", r.TaskID, "err", err)
		return
	}
	if r.Rating == FeedbackDown && strings.TrimSpace(note) != "" {
		if err := recorder.Note(r, note); err != nil {
			a.log.Warn("teams answer rating note was not kept", "conversation", r.Conversation, "taskId", r.TaskID, "err", err)
		}
	}
	if !held || rated.task != r.TaskID || rated.recorded {
		return
	}
	text := rated.text + "\n\n" + teamsRatingRecordedLine
	if err := a.edit(r.Conversation, r.MessageID, text, teamsRatingActions(rated.task, recorder.KeptDays())); err != nil {
		a.log.Warn("teams rated card was not redrawn", "conversation", r.Conversation, "err", err)
		return
	}
	a.mu.Lock()
	if now, still := a.rated[r.MessageID]; still && now.task == rated.task {
		now.recorded = true
		a.rated[r.MessageID] = now
	}
	a.mu.Unlock()
}
