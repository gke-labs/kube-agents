package gateway

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// The Stop button on Google Chat. A task's live message, the status line the
// gateway posts as "⏳ submitted…" and edits as the task moves (relay.go),
// arrives with OutMessage.Stop while the task runs, and this adapter draws a
// Stop button under it: after every button the message carries when it is
// laid out (a progress message's links), alone under the text otherwise. The
// terminal edit arrives without Stop and takes the button off, as Edit does.
// A click on it is the typed `stop` in every way: it lands on the gateway as
// IntentCancel with no text, bound to the conversation the button carries,
// and the gateway's stop path answers it as it answers the word, in the
// conversation, for anyone the allowlist admits. Slack's Stop is native to
// its Agent Sessions (slack_stop.go); this one is drawn, since Chat has no
// such control, and a text line Chat refuses with it is posted or patched
// once more without it, so the line itself never fails for the button (a
// laid-out line's refusal is the layout's, and the relay falls back to the
// text, which then takes the same path).
const (
	// gchatStopFunction is the onClick action the Stop button sends, with
	// the conversation its line belongs to as its one parameter: the
	// click's event names the surface the line is on, which is not the
	// conversation key a DM's thread-less line was posted under.
	gchatStopFunction          = "kage.stop"
	gchatStopParamConversation = "conversation"
	gchatStopText              = "Stop"
	// gchatStopSeenPrefix keys a stop click in the dedupe ring, apart from a
	// choice click on the same message at the same time.
	gchatStopSeenPrefix = "stop:"

	// The reasons a stop click is not a turn, for the pull loop's log.
	gchatStopNoEmail        = "stop clicker has no email"
	gchatStopNoMessage      = "stop click names no space or no message"
	gchatStopNoConversation = "a stop click naming no conversation"
	gchatStopOtherSpace     = "a stop click naming a conversation outside its space"
	gchatStopDuplicate      = "duplicate delivery of stop click "
)

// gchatStopButtons appends the Stop button, bound to conversation, after
// every button layout carries: in its one button list, or a list of its own
// under a text line.
func gchatStopButtons(layout gchatChatLayout, conversation string) gchatChatLayout {
	button := gchatChatButton(gchatStopText, false)
	button["onClick"] = map[string]any{"action": map[string]any{
		"function":   gchatStopFunction,
		"parameters": []map[string]any{{"key": gchatStopParamConversation, "value": conversation}},
	}}
	for _, w := range layout.Widgets {
		if list, ok := w["buttonList"].(map[string]any); ok {
			buttons, _ := list["buttons"].([]map[string]any)
			list["buttons"] = append(buttons, button)
			return layout
		}
	}
	layout.Widgets = append(layout.Widgets, map[string]any{"buttonList": map[string]any{"buttons": []map[string]any{button}}})
	return layout
}

// postStopLine posts a text line with the Stop button under it, and the
// line alone when Chat refuses the button.
func (a *GoogleChatAdapter) postStopLine(conversation, text string) (string, error) {
	space, thread, ok := gchatSpaceThread(conversation)
	if !ok {
		return "", fmt.Errorf("gchat: not a gchat conversation: %q", conversation)
	}
	layout := gchatStopButtons(gchatChatLayout{Text: toGchatText(text)}, conversation)
	message, _, err := a.create(space, thread, gchatLayoutBody(layout))
	if err == nil {
		return message, nil
	}
	if !errors.Is(gchatLayoutError(err, false), ErrLayoutRefused) {
		return "", err
	}
	a.log.Warn("gchat: the Stop button was refused; posting the line without it", "conversation", conversation, "err", err)
	return a.Post(conversation, text)
}

// classifyStop reads a click on the Stop button as the conversation's
// cancel: IntentCancel with no text, from the clicker, bound to the
// conversation the button carries, which must be in the space the click
// came from. A redelivered click is dropped by its id, as a choice click
// is.
func (a *GoogleChatAdapter) classifyStop(ev *gchatEvent) (InboundMessage, string) {
	click := ev.click
	if click.User.Email == "" {
		return InboundMessage{}, gchatStopNoEmail
	}
	if ev.Space.Name == "" || ev.Message.Name == "" {
		return InboundMessage{}, gchatStopNoMessage
	}
	conversation := strings.TrimSpace(click.Parameters[gchatStopParamConversation])
	space, _, ok := gchatSpaceThread(conversation)
	if !ok {
		return InboundMessage{}, gchatStopNoConversation + " (" + strconv.Quote(conversation) + ")"
	}
	if space != ev.Space.Name {
		return InboundMessage{}, gchatStopOtherSpace + " (" + conversation + ")"
	}
	id := gchatClickID(ev.Message.Name, click.EventTime)
	a.mu.Lock()
	if click.EventTime != "" && a.seenLocked(gchatStopSeenPrefix+id) {
		a.mu.Unlock()
		return InboundMessage{}, gchatStopDuplicate + id
	}
	a.rememberUserLocked(click.User)
	a.mu.Unlock()
	kind := sessionKindGroup
	if strings.HasPrefix(conversation, gchatDMKeyPrefix) {
		kind = sessionKindDM
	}
	return InboundMessage{
		Conversation: conversation,
		Kind:         kind,
		AuthorID:     click.User.Email,
		MessageID:    id,
		Intent:       IntentCancel,
	}, ""
}
