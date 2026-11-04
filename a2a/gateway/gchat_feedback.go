package gateway

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// Answer feedback on Google Chat (feedback.go): two rating buttons, 👍 then
// 👎, at the end of the final answer's one button list, after its choice and
// link buttons. They are drawn only once the gateway has handed over its
// recorder, and only on an answer in a conversation, never on a home post.
// A layout Chat refuses with them is posted once more without them. The
// buttons each rated message carries are remembered in this process; after
// a restart a click reads them from the event's copy of the message.
const (
	// gchatFeedbackFunction is the onClick action a rating button sends,
	// with the answer's task id and the rating as its parameters.
	gchatFeedbackFunction = "kage.feedback"
	// gchatFeedbackNoteFunction is the action of the note dialog a 👎 opens.
	gchatFeedbackNoteFunction = "kage.feedback-note"
	gchatFeedbackParamTask    = "task"
	gchatFeedbackParamRating  = "rating"
	// gchatFeedbackParamCancel marks the note dialog's Cancel, with
	// gchatFeedbackCancelValue as its value.
	gchatFeedbackParamCancel = "cancel"
	gchatFeedbackCancelValue = "1"
	gchatFeedbackUpText      = "👍"
	gchatFeedbackDownText    = "👎"
	// gchatFeedbackSeenPrefix keys a rating tap in the dedupe ring, apart
	// from a choice click on the same message at the same time.
	gchatFeedbackSeenPrefix = "feedback:"
	// The relay call that patches a rated message, and the widget keys of
	// its one button list.
	gchatSpacesResource   = "spaces"
	gchatMessagesResource = "messages"
	gchatPatchMethod      = "patch"
	gchatNameArg          = "name"
	gchatUpdateMaskArg    = "updateMask"
	gchatBodyArg          = "body"
	gchatButtonListKey    = "buttonList"
	gchatButtonsKey       = "buttons"

	// The reasons a rating tap is not a turn, for the pull loop's log.
	gchatRatingOff        = "a rating tap with answer feedback off"
	gchatRatingNoTask     = "a rating tap with no task"
	gchatRatingUnknown    = "a rating tap with an unknown rating "
	gchatRatingNoEmail    = "rater has no email"
	gchatRatingNoMessage  = "rating tap names no space or no message"
	gchatRatingDuplicate  = "duplicate delivery of rating tap "
	gchatRatingUnverified = "a rating tap from an unverified sender"
	gchatRatingTaken      = "a rating tap, recorded off the pull loop: "
)

// gchatButtonKind is what a button on a message does, told apart by its
// function or link (classifyGchatButton).
type gchatButtonKind string

const (
	gchatButtonOther  gchatButtonKind = ""
	gchatButtonChoice gchatButtonKind = "choice"
	gchatButtonLink   gchatButtonKind = "link"
	gchatButtonRating gchatButtonKind = "rating"
	gchatButtonNote   gchatButtonKind = "note"
)

// gchatRating is the rating buttons a message carries: task is the rated
// answer's task id, empty for no buttons, and chosen the rating drawn
// filled (FeedbackUp, FeedbackDown, or empty for none).
type gchatRating struct{ task, chosen string }

var _ FeedbackSink = (*GoogleChatAdapter)(nil)

// SetFeedbackRecorder turns the rating buttons on (FeedbackSink).
func (a *GoogleChatAdapter) SetFeedbackRecorder(r FeedbackRecorder) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.feedback = r
}

func (a *GoogleChatAdapter) feedbackRecorder() FeedbackRecorder {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.feedback
}

// rates reports whether a message is drawn with rating buttons.
func (a *GoogleChatAdapter) rates(m OutMessage) bool {
	return m.Feedback != "" && a.feedbackRecorder() != nil
}

// gchatRatingButtons is the two rating buttons for r, 👍 then 👎; the chosen
// one is drawn filled.
func gchatRatingButtons(r gchatRating) []map[string]any {
	button := func(text, alt, rating string) map[string]any {
		b := map[string]any{"text": text, "altText": alt, "onClick": map[string]any{"action": map[string]any{
			"function": gchatFeedbackFunction,
			"parameters": []map[string]any{
				{"key": gchatFeedbackParamTask, "value": r.task},
				{"key": gchatFeedbackParamRating, "value": rating},
			},
		}}}
		if r.chosen == rating {
			b["type"] = gchatChatButtonFilled
		}
		return b
	}
	return []map[string]any{
		button(gchatFeedbackUpText, feedbackGoodLabel, FeedbackUp),
		button(gchatFeedbackDownText, feedbackBadLabel, FeedbackDown),
	}
}

// gchatWithRating is widgets with r's rating buttons at the end of their one
// button list, which it adds when there is none. It returns new slices and
// maps and leaves widgets as they are, since a remembered question keeps
// its layout; with no task it returns widgets unchanged.
func gchatWithRating(widgets []map[string]any, r gchatRating) []map[string]any {
	if r.task == "" {
		return widgets
	}
	out := make([]map[string]any, 0, len(widgets)+1)
	added := false
	for _, w := range widgets {
		list, ok := w["buttonList"].(map[string]any)
		if !ok || added {
			out = append(out, w)
			continue
		}
		existing, _ := list["buttons"].([]map[string]any)
		buttons := append(append([]map[string]any{}, existing...), gchatRatingButtons(r)...)
		newList := make(map[string]any, len(list))
		for k, v := range list {
			newList[k] = v
		}
		newList["buttons"] = buttons
		newWidget := make(map[string]any, len(w))
		for k, v := range w {
			newWidget[k] = v
		}
		newWidget["buttonList"] = newList
		out = append(out, newWidget)
		added = true
	}
	if !added {
		out = append(out, map[string]any{"buttonList": map[string]any{"buttons": gchatRatingButtons(r)}})
	}
	return out
}

// classifyGchatButton tells what a decoded button does by its function, or
// by its link.
func classifyGchatButton(b gchatButton) gchatButtonKind {
	if b.OnClick.OpenLink.URL != "" {
		return gchatButtonLink
	}
	switch b.OnClick.Action.Function {
	case gchatChatFunction:
		return gchatButtonChoice
	case gchatFeedbackFunction:
		return gchatButtonRating
	case gchatFeedbackNoteFunction:
		return gchatButtonNote
	}
	return gchatButtonOther
}

// gchatButtonWidget re-encodes a decoded button as the renderer draws one:
// its text, type and alt text when set, and its link or its action.
func gchatButtonWidget(b gchatButton) map[string]any {
	w := map[string]any{"text": b.Text}
	if b.Type != "" {
		w["type"] = b.Type
	}
	if b.AltText != "" {
		w["altText"] = b.AltText
	}
	switch {
	case b.OnClick.OpenLink.URL != "":
		w["onClick"] = map[string]any{"openLink": map[string]any{"url": b.OnClick.OpenLink.URL}}
	case b.OnClick.Action.Function != "":
		action := map[string]any{"function": b.OnClick.Action.Function}
		if len(b.OnClick.Action.Parameters) > 0 {
			params := make([]map[string]any, len(b.OnClick.Action.Parameters))
			for i, p := range b.OnClick.Action.Parameters {
				params[i] = map[string]any{"key": p.Key, "value": p.Value}
			}
			action["parameters"] = params
		}
		w["onClick"] = map[string]any{"action": action}
	}
	return w
}

// ratingOf is the rating buttons message was posted with, remembered in
// this process; no task when it is not remembered.
func (a *GoogleChatAdapter) ratingOf(message string) gchatRating {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.uxLocked().rated[message]
}

// rememberRating records the rating buttons message carries.
func (a *GoogleChatAdapter) rememberRating(message string, r gchatRating) {
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	ux.rated[message] = r
	remember(&ux.ratedOrder, message, func(k string) { delete(ux.rated, k) })
}

// postRatedText posts a rated answer that has no chat message: its text with
// the rating buttons. When Chat refuses the buttons the text is posted alone.
func (a *GoogleChatAdapter) postRatedText(conversation string, m OutMessage) (string, error) {
	space, thread, ok := gchatSpaceThread(conversation)
	if !ok {
		return "", fmt.Errorf("gchat: not a gchat conversation: %q", conversation)
	}
	rating := gchatRating{task: m.Feedback}
	body := map[string]any{"text": toGchatText(m.Text), "accessoryWidgets": gchatWithRating(nil, rating)}
	message, _, err := a.create(space, thread, body)
	if err != nil {
		if errors.Is(gchatLayoutError(err, false), ErrLayoutRefused) {
			return a.Post(conversation, m.Text)
		}
		return "", err
	}
	a.rememberRating(message, rating)
	return message, nil
}

// feedbackClick takes a tap on a rating button. It is never a turn: it
// always returns the reason the pull loop logs. A tap the gateway cannot
// read, a redelivery, or one from a sender the recorder does not admit
// changes nothing. Otherwise the rating is recorded off the pull loop
// (Rate carries its own deadline, the relay calls theirs), and then the
// message is patched with the tapped button filled. A failed write logs and
// leaves the message as it was. The tap claims no question: a choice click
// on the message is still a turn.
func (a *GoogleChatAdapter) feedbackClick(ev *gchatEvent) string {
	click := ev.click
	recorder := a.feedbackRecorder()
	task := strings.TrimSpace(click.Parameters[gchatFeedbackParamTask])
	value := click.Parameters[gchatFeedbackParamRating]
	switch {
	case recorder == nil:
		return gchatRatingOff
	case task == "":
		return gchatRatingNoTask
	case value != FeedbackUp && value != FeedbackDown:
		return gchatRatingUnknown + strconv.Quote(value)
	case click.User.Email == "":
		return gchatRatingNoEmail
	case ev.Space.Name == "" || ev.Message.Name == "":
		return gchatRatingNoMessage
	}
	message := ev.Message.Name
	id := gchatClickID(message, click.EventTime)
	a.mu.Lock()
	// As a choice click: a tap with no event time has no id of its own.
	if click.EventTime != "" && a.seenLocked(gchatFeedbackSeenPrefix+id) {
		a.mu.Unlock()
		return gchatRatingDuplicate + id
	}
	conversation := a.clickConversationLocked(ev)
	a.mu.Unlock()
	if !recorder.Admits(gchatBackend, click.User.Email) {
		return gchatRatingUnverified
	}
	rating := FeedbackRating{Platform: platformGoogleChat, Backend: gchatBackend, Rater: click.User.Email,
		Conversation: conversation, MessageID: message, TaskID: task, Rating: value, Answer: ev.Message.Text}
	chosen := gchatRating{task: task, chosen: value}
	a.goRewrite(func() {
		if _, err := recorder.Rate(rating); err != nil {
			a.log.Warn("gchat: recording a rating failed", "message", message, "taskId", task, "err", err)
			return
		}
		a.rememberRating(message, chosen)
		if err := a.patchRating(message, a.ratedLayout(ev, chosen)); err != nil {
			a.log.Warn("gchat: filling the tapped rating button failed; the rating is kept", "message", message, "err", err)
		}
		// Task 4: a 👎 asks for a note here.
	})
	return gchatRatingTaken + value
}

// ratedLayout is the layout a rating tap patches message ev to, with r's
// buttons at the end: (a) a remembered question not yet answered keeps its
// own layout; (b) an answered one takes the event's text and its link
// buttons only, so a tap never restores the choices the answer removed; (c)
// any other message, one from before a restart, takes the event's text and
// its choice and link buttons, in the event's order.
func (a *GoogleChatAdapter) ratedLayout(ev *gchatEvent, r gchatRating) gchatChatLayout {
	a.mu.Lock()
	ux := a.uxLocked()
	answered := ux.answered[ev.Message.Name]
	q, remembered := ux.questions[ev.Message.Name]
	a.mu.Unlock()
	if remembered && !answered {
		return gchatChatLayout{Text: q.layout.Text, Widgets: gchatWithRating(q.layout.Widgets, r)}
	}
	var buttons []map[string]any
	for _, w := range ev.Message.AccessoryWidgets {
		for _, b := range w.ButtonList.Buttons {
			kind := classifyGchatButton(b)
			if kind == gchatButtonLink || (kind == gchatButtonChoice && !answered) {
				buttons = append(buttons, gchatButtonWidget(b))
			}
		}
	}
	var widgets []map[string]any
	if len(buttons) > 0 {
		widgets = []map[string]any{{gchatButtonListKey: map[string]any{gchatButtonsKey: buttons}}}
	}
	return gchatChatLayout{Text: ev.Message.Text, Widgets: gchatWithRating(widgets, r)}
}

// patchRating patches message to layout.
func (a *GoogleChatAdapter) patchRating(message string, layout gchatChatLayout) error {
	arguments := map[string]any{
		gchatNameArg:       message,
		gchatUpdateMaskArg: gchatLayoutUpdateMask,
		gchatBodyArg:       gchatLayoutBody(layout),
	}
	return a.apiCall([]string{gchatSpacesResource, gchatMessagesResource}, gchatPatchMethod, arguments, nil)
}
