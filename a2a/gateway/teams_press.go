package gateway

import (
	"encoding/json"
	"strings"
)

// The buttons the Teams adapter draws on its cards, and their presses.
//
// A button is an Adaptive Card Action.Submit. Teams delivers its press as a
// message activity whose value is the button's data and whose replyToId is
// the message the card is on, with no text; it asks the bot's endpoint for
// nothing in return, which is what lets a press travel the broker's queue
// like any other activity. Action.Execute would not: Teams sends it as an
// invoke and shows the presser an error unless the endpoint answers it at
// once, and the receiver in front of the queue answers every activity
// "accepted" before the gateway has read it.
//
// The Stop button (spec-chatops-gateway.md, "Who may press Stop") is drawn
// on a running task's line, names its task, and reaches the gateway as the
// press Google Chat's does: IntentCancel, Click, the task, and the pressed
// message in the id (gchatClickID), so the same gate and the same answer on
// the line apply (stopgate.go, chat_stop_press.go). It rides the card, so a
// line posted as plain text (adaptiveCards off, or a line too large for a
// card) has none, and a typed stop is the way there.
const (
	teamsCardActionsKey  = "actions"
	teamsActionSubmit    = "Action.Submit"
	teamsPressActionKey  = "kageAction"
	teamsPressTaskKey    = "task"
	teamsPressActionStop = "stop"
	teamsStopTitle       = "Stop"
)

// teamsPress is a pressed button's data: which button, and the task it
// names.
type teamsPress struct {
	Action string `json:"kageAction"`
	Task   string `json:"task"`
}

// teamsCardPress reads a message activity's value as a press of one of the
// adapter's buttons; the zero press for a value that is absent, not an
// object, or another sender's shape.
func teamsCardPress(raw json.RawMessage) teamsPress {
	var press teamsPress
	if len(raw) == 0 || json.Unmarshal(raw, &press) != nil {
		return teamsPress{}
	}
	press.Action, press.Task = strings.TrimSpace(press.Action), strings.TrimSpace(press.Task)
	return press
}

// teamsStopActions is the Stop button for a line whose running task is
// task.
func teamsStopActions(task string) []map[string]any {
	return []map[string]any{{
		"type":  teamsActionSubmit,
		"title": teamsStopTitle,
		"data":  map[string]any{teamsPressActionKey: teamsPressActionStop, teamsPressTaskKey: task},
	}}
}

// classifyPress turns a press into the turn the gateway reads, or names why
// it is not one. The caller (classify) has checked the conversation, the
// service URL, the tenant and that the sender has an id.
func (a *TeamsAdapter) classifyPress(act *teamsActivity, press teamsPress, conversation, kind string) (InboundMessage, string) {
	if press.Action != teamsPressActionStop {
		return InboundMessage{}, "press of a button this adapter does not draw (" + press.Action + ")"
	}
	if press.Task == "" {
		return InboundMessage{}, "stop press names no task"
	}
	pressed := strings.TrimSpace(act.ReplyToID)
	if pressed == "" {
		return InboundMessage{}, "stop press names no message"
	}
	a.mu.Lock()
	if act.ServiceURL != "" {
		a.rememberServiceURLLocked(conversation, act.ServiceURL)
	}
	author := a.authorForLocked(act.From.AadObjectID, act.From.ID)
	dup := act.ID != "" && a.seenLocked(act.ID)
	a.mu.Unlock()
	if dup {
		return InboundMessage{}, "duplicate delivery of " + act.ID
	}
	return InboundMessage{
		Conversation: teamsConversationID(conversation),
		Kind:         kind,
		AuthorID:     author,
		MessageID:    gchatClickID(pressed, act.ID),
		Intent:       IntentCancel,
		TaskID:       press.Task,
		Click:        true,
	}, ""
}
