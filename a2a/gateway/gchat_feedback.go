package gateway

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
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
	// The keys a drawn button names its action's function under, which
	// gchatWithoutRating reads to find the rating buttons.
	gchatOnClickKey  = "onClick"
	gchatActionKey   = "action"
	gchatFunctionKey = "function"

	// The reasons a rating tap is not a turn, for the pull loop's log.
	gchatRatingOff        = "a rating tap with answer feedback off"
	gchatRatingNoTask     = "a rating tap with no task"
	gchatRatingUnknown    = "a rating tap with an unknown rating "
	gchatRatingNoEmail    = "rater has no email"
	gchatRatingNoMessage  = "rating tap names no space or no message"
	gchatRatingDuplicate  = "duplicate delivery of rating tap "
	gchatRatingUnverified = "a rating tap from an unverified sender"
	gchatRatingTaken      = "a rating tap, recorded off the pull loop: "
	// gchatRatingSuperseded is logged for a tap dropped because the same
	// rater tapped the same message later.
	gchatRatingSuperseded = "gchat: a rating tap was superseded by a later tap from the same rater; dropped"
	// gchatTapKeySep joins a message name and a rater's email in a
	// gchatTapKey; neither contains it.
	gchatTapKeySep = "\x00"

	// The note card a 👎 posts: a cardsV2 message with one text box, in
	// the rater's thread, private to the rater outside a one-to-one DM.
	gchatNoteCardID          = "kage.feedback-note"
	gchatNoteInputName       = "note"
	gchatNoteInputSingleLine = "SINGLE_LINE"
	gchatNoteHint            = "Up to %d characters."
	// gchatNoteCardsMask is the patch mask that replaces a message's cards.
	gchatNoteCardsMask = "cards_v2"
	gchatNoteLost      = "The note did not reach me; your rating stands as it was."
	gchatNoteReplaced  = "This answer's rating changed, so the note was not kept."
	// gchatCardLink is a link in a card's textParagraph, which takes HTML.
	gchatCardLink = `<a href="%s">%s</a>`
	// gchatFeedbackNoteSeenPrefix keys a note card click in the dedupe
	// ring, apart from a rating tap.
	gchatFeedbackNoteSeenPrefix = "feedback-note:"
	gchatPrivateViewerKey       = "privateMessageViewer"
	// The card JSON keys the note card is drawn with.
	gchatCardsV2Key       = "cardsV2"
	gchatCardIDKey        = "cardId"
	gchatCardKey          = "card"
	gchatSectionsKey      = "sections"
	gchatWidgetsKey       = "widgets"
	gchatTextParagraphKey = "textParagraph"
	gchatTextInputKey     = "textInput"

	// The reasons a note card click is not a turn, for the pull loop's log.
	gchatNoteOff        = "a note click with answer feedback off"
	gchatNoteNoTask     = "a note click with no task"
	gchatNoteNotDown    = "a note click on a rating other than 👎: "
	gchatNoteNoEmail    = "note writer has no email"
	gchatNoteNoMessage  = "note click names no space or no message"
	gchatNoteDuplicate  = "duplicate delivery of note click "
	gchatNoteUnverified = "a note click from an unverified sender"
	gchatNoteTaken      = "a note click, handled off the pull loop"
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
// answer's task id, empty for no buttons, chosen the rating drawn filled
// (FeedbackUp, FeedbackDown, or empty for none), and at the event time of
// the tap that filled it, empty when none had one.
type gchatRating struct{ task, chosen, at string }

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
		list, ok := w[gchatButtonListKey].(map[string]any)
		if !ok || added {
			out = append(out, w)
			continue
		}
		existing, _ := list[gchatButtonsKey].([]map[string]any)
		buttons := append(append([]map[string]any{}, existing...), gchatRatingButtons(r)...)
		newList := make(map[string]any, len(list))
		for k, v := range list {
			newList[k] = v
		}
		newList[gchatButtonsKey] = buttons
		newWidget := make(map[string]any, len(w))
		for k, v := range w {
			newWidget[k] = v
		}
		newWidget[gchatButtonListKey] = newList
		out = append(out, newWidget)
		added = true
	}
	if !added {
		out = append(out, map[string]any{gchatButtonListKey: map[string]any{gchatButtonsKey: gchatRatingButtons(r)}})
	}
	return out
}

// gchatWithoutRating is layout with its rating buttons removed, and a button
// list left empty by that removed with them, so gchatWithRating draws one
// pair on it again. It returns new slices and maps and leaves layout as it
// is.
func gchatWithoutRating(layout gchatChatLayout) gchatChatLayout {
	var widgets []map[string]any
	for _, w := range layout.Widgets {
		list, ok := w[gchatButtonListKey].(map[string]any)
		if !ok {
			widgets = append(widgets, w)
			continue
		}
		existing, _ := list[gchatButtonsKey].([]map[string]any)
		var buttons []map[string]any
		for _, b := range existing {
			click, _ := b[gchatOnClickKey].(map[string]any)
			action, _ := click[gchatActionKey].(map[string]any)
			if action[gchatFunctionKey] != gchatFeedbackFunction {
				buttons = append(buttons, b)
			}
		}
		if len(buttons) == len(existing) {
			widgets = append(widgets, w)
			continue
		}
		if len(buttons) == 0 {
			continue
		}
		newList := make(map[string]any, len(list))
		for k, v := range list {
			newList[k] = v
		}
		newList[gchatButtonsKey] = buttons
		newWidget := make(map[string]any, len(w))
		for k, v := range w {
			newWidget[k] = v
		}
		newWidget[gchatButtonListKey] = newList
		widgets = append(widgets, newWidget)
	}
	return gchatChatLayout{Text: layout.Text, Widgets: widgets}
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

// gchatTapTimes is a rater's newest tap time on a message, and the time it
// displaced, which a failed write of the newest restores (forgetTap).
type gchatTapTimes struct{ at, displaced string }

// gchatTapKey keys a rater's taps on a message in ux.tapped.
func gchatTapKey(message, rater string) string {
	return message + gchatTapKeySep + rater
}

// gchatTapOlder reports whether event time at is before recorded. A missing
// or unreadable time is never older.
func gchatTapOlder(at, recorded string) bool {
	if at == "" || recorded == "" {
		return false
	}
	t, terr := time.Parse(time.RFC3339Nano, at)
	r, rerr := time.Parse(time.RFC3339Nano, recorded)
	return terr == nil && rerr == nil && t.Before(r)
}

// takeTap records at as rater's newest tap on message, keeping the time it
// displaces, unless a newer one is on record. A tap with no event time
// records nothing.
func (a *GoogleChatAdapter) takeTap(message, rater, at string) {
	if at == "" {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	key := gchatTapKey(message, rater)
	recorded, ok := ux.tapped[key]
	if ok && !gchatTapOlder(recorded.at, at) {
		return
	}
	ux.tapped[key] = gchatTapTimes{at: at, displaced: recorded.at}
	remember(&ux.tappedOrder, key, func(k string) { delete(ux.tapped, k) })
}

// forgetTap undoes takeTap for at, when it is still rater's newest tap on
// message: the time it displaced is newest again, or none is.
func (a *GoogleChatAdapter) forgetTap(message, rater, at string) {
	if at == "" {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	key := gchatTapKey(message, rater)
	recorded := ux.tapped[key]
	switch {
	case recorded.at != at:
	case recorded.displaced != "":
		ux.tapped[key] = gchatTapTimes{at: recorded.displaced}
	default:
		delete(ux.tapped, key)
		forget(&ux.tappedOrder, key)
	}
}

// tapSuperseded reports whether rater has tapped message after event time
// at.
func (a *GoogleChatAdapter) tapSuperseded(message, rater, at string) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return gchatTapOlder(at, a.uxLocked().tapped[gchatTapKey(message, rater)].at)
}

// rememberTap records r, a recorded tap, as the rating buttons message
// carries, and reports whether it did: a tap older than the one that filled
// the buttons on record, another rater's, leaves them. A tap with no event
// time keeps the time on record.
func (a *GoogleChatAdapter) rememberTap(message string, r gchatRating) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	current := ux.rated[message]
	if gchatTapOlder(r.at, current.at) {
		return false
	}
	if r.at == "" {
		r.at = current.at
	}
	ux.rated[message] = r
	remember(&ux.ratedOrder, message, func(k string) { delete(ux.rated, k) })
	return true
}

// rememberRatingIfAbsent is the rating buttons on record for message, or r,
// recorded in the same lock section, when none is and r has a task.
func (a *GoogleChatAdapter) rememberRatingIfAbsent(message string, r gchatRating) gchatRating {
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	if current := ux.rated[message]; current.task != "" || r.task == "" {
		return current
	}
	ux.rated[message] = r
	remember(&ux.ratedOrder, message, func(k string) { delete(ux.rated, k) })
	return r
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
// leaves the message as it was. Rate, the layout and the patch run under the
// message's rewrite lock (lockRewrite), so taps on one message take effect
// in turn. A tap older than one the same rater has made on the same message
// is superseded and dropped before Rate. An older tap whose Rate was already
// running when the newer one came in is recorded and patched, the newer one
// repaints after it, and its 👎 asks for no note while the newer one stands.
// A failed Rate restores the tap time this tap displaced. A tap with no
// event time is never superseded. A kept 👎 then asks for a note (askForNote). The
// tap claims no question: a choice click on the message is still a turn.
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
	rater, at := click.User.Email, click.EventTime
	a.takeTap(message, rater, at)
	chosen := gchatRating{task: task, chosen: value, at: at}
	// The rewrite runs after this returns: it reads its own copy of the
	// message, its space and the rater, never the event the caller holds.
	snapshot := &gchatEvent{Space: ev.Space, Message: gchatMessage{Name: message, Text: ev.Message.Text,
		Thread: ev.Message.Thread, AccessoryWidgets: cloneGchatWidgets(ev.Message.AccessoryWidgets)},
		click: &gchatClick{User: click.User}}
	a.goRewrite(func() {
		// Under the message's rewrite lock from Rate to the patch, so taps
		// on one message are recorded in turn, the layout reads what the
		// previous patch left, and this patch never lands over a later one.
		unlock := a.lockRewrite(message)
		if a.tapSuperseded(message, rater, at) {
			unlock()
			a.log.Info(gchatRatingSuperseded, "message", message, "taskId", task, "rating", value)
			return
		}
		askKept, err := recorder.Rate(rating)
		if err != nil {
			// The bucket never took this tap, so the record does not claim it.
			a.forgetTap(message, rater, at)
			unlock()
			a.log.Warn("gchat: recording a rating failed", "message", message, "taskId", task, "err", err)
			return
		}
		// A later tap taken while Rate ran patches after this one, under
		// the same lock, so this patch never outlives it.
		if a.rememberTap(message, chosen) {
			if err := a.patchRating(message, a.ratedLayout(snapshot, chosen)); err != nil {
				a.log.Warn("gchat: filling the tapped rating button failed; the rating is kept", "message", message, "err", err)
			}
		}
		unlock()
		// The 👎 is kept whether or not its button filled: ask for a note,
		// unless a later tap from the rater is on its way to replace it.
		if value == FeedbackDown && !a.tapSuperseded(message, rater, at) {
			a.askForNote(snapshot, task, askKept)
		}
	})
	return gchatRatingTaken + value
}

// ratedLayout is the layout a rating tap patches message ev to, with r's
// buttons at the end. The adapter's record wins when it has one: (a) a
// remembered question not yet answered keeps its own layout; (b) an
// answered one keeps the layout its answer put on it, the answered line
// included (ux.shown), and, while that rewrite has not landed, takes the
// event's text and its link buttons only, so a tap never restores the
// choices the answer removed; (c) any other message, one from before a
// restart, takes the event's text and its choice and link buttons, in the
// event's order.
func (a *GoogleChatAdapter) ratedLayout(ev *gchatEvent, r gchatRating) gchatChatLayout {
	a.mu.Lock()
	ux := a.uxLocked()
	answered := ux.answered[ev.Message.Name]
	shown, recorded := ux.shown[ev.Message.Name]
	q, remembered := ux.questions[ev.Message.Name]
	a.mu.Unlock()
	switch {
	case answered && recorded:
		return gchatChatLayout{Text: shown.Text, Widgets: gchatWithRating(shown.Widgets, r)}
	case remembered && !answered:
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

// cloneGchatWidgets is a deep copy of decoded accessory widgets: no slice
// in it shares a backing array with widgets.
func cloneGchatWidgets(widgets []gchatAccessoryWidget) []gchatAccessoryWidget {
	if widgets == nil {
		return nil
	}
	out := make([]gchatAccessoryWidget, len(widgets))
	for i, w := range widgets {
		buttons := make([]gchatButton, len(w.ButtonList.Buttons))
		for j, b := range w.ButtonList.Buttons {
			params := b.OnClick.Action.Parameters
			b.OnClick.Action.Parameters = append(params[:0:0], params...)
			buttons[j] = b
		}
		out[i].ButtonList.Buttons = buttons
	}
	return out
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

// gchatNoteText is a typed note as kept: trimmed, and cut to the text box's
// limit in code points, with no ellipsis.
func gchatNoteText(raw string) string {
	note := strings.TrimSpace(raw)
	if runes := []rune(note); len(runes) > feedbackNoteChars {
		note = string(runes[:feedbackNoteChars])
	}
	return note
}

// gchatNoteAction is the note card's onClick action for task, with params
// added.
func gchatNoteAction(task string, params ...map[string]any) map[string]any {
	parameters := append([]map[string]any{
		{"key": gchatFeedbackParamTask, "value": task},
		{"key": gchatFeedbackParamRating, "value": FeedbackDown},
	}, params...)
	return map[string]any{"action": map[string]any{"function": gchatFeedbackNoteFunction, "parameters": parameters}}
}

// gchatNoteCardOf is the note card holding widgets.
func gchatNoteCardOf(widgets ...map[string]any) map[string]any {
	return map[string]any{
		gchatCardIDKey: gchatNoteCardID,
		gchatCardKey:   map[string]any{gchatSectionsKey: []map[string]any{{gchatWidgetsKey: widgets}}},
	}
}

// gchatNoteCard is the note a 👎 on task asks for: the title, the text box,
// the kept line (naming the question only when askKept), and Send and
// Cancel.
func gchatNoteCard(task string, days int, askKept bool) map[string]any {
	return gchatNoteCardOf(
		map[string]any{gchatTextParagraphKey: map[string]any{"text": feedbackNoteTitle}},
		map[string]any{gchatTextInputKey: map[string]any{
			"name":       gchatNoteInputName,
			"label":      feedbackNoteLabel,
			"type":       gchatNoteInputSingleLine,
			"hintText":   fmt.Sprintf(gchatNoteHint, feedbackNoteChars),
			"validation": map[string]any{"characterLimit": feedbackNoteChars},
		}},
		map[string]any{gchatTextParagraphKey: map[string]any{"text": feedbackKeptText(days, askKept)}},
		map[string]any{gchatButtonListKey: map[string]any{gchatButtonsKey: []map[string]any{
			{"text": feedbackNoteSend, "onClick": gchatNoteAction(task)},
			{"text": feedbackNoteCancel, "onClick": gchatNoteAction(task,
				map[string]any{"key": gchatFeedbackParamCancel, "value": gchatFeedbackCancelValue})},
		}}},
	)
}

// gchatCollapsedCard is the note card collapsed to one line.
func gchatCollapsedCard(text string) map[string]any {
	return gchatNoteCardOf(map[string]any{gchatTextParagraphKey: map[string]any{"text": text}})
}

// gchatNoteThanks is the line a kept note collapses its card to.
func gchatNoteThanks() string {
	return feedbackThanks(fmt.Sprintf(gchatCardLink, feedbackFormURL, feedbackFormURL))
}

// createCard posts card in space, on thread when set, private to viewer
// when set.
func (a *GoogleChatAdapter) createCard(space, thread, viewer string, card map[string]any) (string, error) {
	body := map[string]any{gchatCardsV2Key: []map[string]any{card}}
	if viewer != "" {
		body[gchatPrivateViewerKey] = map[string]any{gchatNameArg: viewer}
	}
	message, _, err := a.create(space, thread, body)
	return message, err
}

// noteViewer is who the note card on ev is shown to and the thread it goes
// in: everyone in a one-to-one DM (viewer empty), the clicker alone
// anywhere else. ok is false in a space whose clicker Chat did not name as
// a user, where the card would be public.
func noteViewer(ev *gchatEvent) (viewer, thread string, ok bool) {
	kind, thread := gchatSurface(ev)
	if kind == sessionKindDM {
		return "", thread, true
	}
	viewer = ev.click.User.Name
	return viewer, thread, strings.HasPrefix(viewer, gchatUsersToken)
}

// askForNote posts the note card under the 👎 the rater on ev just gave
// task. A threaded private card Chat refuses is posted once more at the top
// level, still private; a second refusal, or any other failure, logs and
// posts nothing more.
func (a *GoogleChatAdapter) askForNote(ev *gchatEvent, task string, askKept bool) {
	recorder := a.feedbackRecorder()
	if recorder == nil {
		return
	}
	viewer, thread, ok := noteViewer(ev)
	if !ok {
		a.log.Warn("gchat: the rater has no user name; no note card is posted", "space", ev.Space.Name, "taskId", task)
		return
	}
	card := gchatNoteCard(task, recorder.KeptDays(), askKept)
	_, err := a.createCard(ev.Space.Name, thread, viewer, card)
	if errors.Is(gchatLayoutError(err, false), ErrLayoutRefused) && viewer != "" && thread != "" {
		a.log.Info("gchat: a private note card in the thread was refused; posting it at the top level", "space", ev.Space.Name, "err", err)
		_, err = a.createCard(ev.Space.Name, "", viewer, card)
	}
	if err != nil {
		a.log.Warn("gchat: posting the note card failed; the 👎 is kept without a note", "space", ev.Space.Name, "taskId", task, "err", err)
	}
}

// feedbackNoteClick takes a click on the note card's Send or Cancel. It is
// never a turn: it always returns the reason the pull loop logs. A click the
// gateway cannot read, a redelivery, or one from a sender the recorder does
// not admit changes nothing. Otherwise, off the pull loop, Cancel collapses
// the card to the thanks; Send joins the note to the kept 👎 and collapses
// the card to the thanks, or to gchatNoteReplaced when no 👎 is kept any
// more. A Send that carries no inputs collapses to gchatNoteLost and keeps
// no note. Any other failed write leaves the card for another Send.
func (a *GoogleChatAdapter) feedbackNoteClick(ev *gchatEvent) string {
	click := ev.click
	recorder := a.feedbackRecorder()
	task := strings.TrimSpace(click.Parameters[gchatFeedbackParamTask])
	value := click.Parameters[gchatFeedbackParamRating]
	switch {
	case recorder == nil:
		return gchatNoteOff
	case task == "":
		return gchatNoteNoTask
	case value != FeedbackDown:
		return gchatNoteNotDown + strconv.Quote(value)
	case click.User.Email == "":
		return gchatNoteNoEmail
	case ev.Space.Name == "" || ev.Message.Name == "":
		return gchatNoteNoMessage
	}
	id := gchatClickID(ev.Message.Name, click.EventTime)
	a.mu.Lock()
	if click.EventTime != "" && a.seenLocked(gchatFeedbackNoteSeenPrefix+id) {
		a.mu.Unlock()
		return gchatNoteDuplicate + id
	}
	a.mu.Unlock()
	if !recorder.Admits(gchatBackend, click.User.Email) {
		return gchatNoteUnverified
	}
	kind, thread := gchatSurface(ev)
	rating := FeedbackRating{Platform: platformGoogleChat, Backend: gchatBackend, Rater: click.User.Email,
		Conversation: gchatConversationID(ev.Space.Name, thread, kind), TaskID: task, Rating: FeedbackDown}
	cancel := click.Parameters[gchatFeedbackParamCancel] == gchatFeedbackCancelValue
	note, present := click.formValue(gchatNoteInputName)
	snapshot := &gchatEvent{Space: ev.Space, Message: gchatMessage{Name: ev.Message.Name, Thread: ev.Message.Thread},
		click: &gchatClick{User: click.User}}
	a.goRewrite(func() {
		switch {
		case cancel:
			a.collapseNoteCard(snapshot, gchatNoteThanks())
			return
		case !present:
			a.log.Warn("gchat: a note Send carried no inputs; the 👎 is kept without a note", "message", snapshot.Message.Name, "taskId", task)
			a.collapseNoteCard(snapshot, gchatNoteLost)
			return
		}
		err := recorder.Note(rating, gchatNoteText(note))
		switch {
		case err == nil:
			a.collapseNoteCard(snapshot, gchatNoteThanks())
		case errors.Is(err, errFeedbackNotKept):
			a.collapseNoteCard(snapshot, gchatNoteReplaced)
		default:
			a.log.Warn("gchat: keeping a feedback note failed; the card stays", "message", snapshot.Message.Name, "taskId", task, "err", err)
		}
	})
	return gchatNoteTaken
}

// collapseNoteCard replaces the note card on ev's message with text. When
// Chat refuses the patch, text is posted as a new card to the same viewer;
// any other failure logs and posts nothing.
func (a *GoogleChatAdapter) collapseNoteCard(ev *gchatEvent, text string) {
	arguments := map[string]any{
		gchatNameArg:       ev.Message.Name,
		gchatUpdateMaskArg: gchatNoteCardsMask,
		gchatBodyArg:       map[string]any{gchatCardsV2Key: []map[string]any{gchatCollapsedCard(text)}},
	}
	err := a.apiCall([]string{gchatSpacesResource, gchatMessagesResource}, gchatPatchMethod, arguments, nil)
	if err == nil {
		return
	}
	if !errors.Is(gchatLayoutError(err, true), ErrLayoutRefused) {
		a.log.Warn("gchat: collapsing the note card failed", "message", ev.Message.Name, "err", err)
		return
	}
	viewer, thread, ok := noteViewer(ev)
	if !ok {
		a.log.Warn("gchat: collapsing the note card failed and the writer has no user name", "message", ev.Message.Name, "err", err)
		return
	}
	if _, cerr := a.createCard(ev.Space.Name, thread, viewer, gchatCollapsedCard(text)); cerr != nil {
		a.log.Warn("gchat: collapsing the note card failed", "message", ev.Message.Name, "patchErr", err, "err", cerr)
	}
}
