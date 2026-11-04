package gateway

import (
	"errors"
	"fmt"
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
