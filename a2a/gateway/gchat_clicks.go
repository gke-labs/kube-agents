package gateway

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
	"github.com/gke-labs/kube-agents/a2a/lib/chattext"
)

// Clicks on Google Chat. A click on a choice button the renderer drew
// (gchat_chat.go) arrives as a CARD_CLICKED interaction on the same Pub/Sub
// subscription as a message, in either wire layout: the add-on one, where
// chat.buttonClickedPayload names the message the button is on and its
// space, chat.user the clicker, and commonEventObject the function and
// parameters the button carried; or the legacy one, with message, user,
// space, action and common at the top level, which is how the one install
// measured so far delivers clicks while its messages arrive in the add-on
// layout (decodeGchatLegacyClick). Both are read the same. The adapter turns
// the click into a text turn from
// the clicker, as the Slack adapter does (slack_interactive.go): its text is
// the choice's reply, handed to the gateway like a typed message, so it
// passes the allowlist like one and carries no intent of its own. Once the
// gateway says the clicker is verified (VerifiedObserver), the clicked
// message is rewritten: its choice buttons go, its link buttons stay, and
// "✓ <name>: <label>" is added under its text, the clicker named by their
// Chat display name, never by a mention or an address. A click from a
// sender the gateway drops changes nothing.
//
// A typed reply in a conversation whose question is still open answers it
// the same way, as on Slack: the question loses its choice buttons and gains
// "✓ <name>: <the reply's first words>"; a command (stop, a status question,
// /session) is not a reply, and a finished answer's next step (choices
// marked next) takes no typed answer. A question is answered once,
// whichever way comes first; a second click on it is dropped, until the
// message is laid out again with choices (a card asking again on its update
// key), which opens it anew. A message laid out again without choices, or
// replaced by text, closes it. What the adapter remembers lives in this
// process: the layout of each message with choices it posted, so the
// rewrite keeps the message's text and links, and which questions are
// answered. The click event carries the clicked message itself, text and
// buttons, so a question posted before a restart is rewritten from that:
// its text, the link buttons it shows, and the clicked button's own label.
const (
	// gchatEventTypeClick is the normalized type of a button click
	// (decodeGchatEvent), Chat's own name for the interaction.
	gchatEventTypeClick = "CARD_CLICKED"
	// gchatAddonClickKey is the chat.* payload key that marks a button
	// click in the add-on event object.
	gchatAddonClickKey = "buttonClickedPayload"
	// gchatClickIDSep joins a clicked message's name and the event time
	// into a click id (gchatClickID).
	gchatClickIDSep = "#"
	// gchatChoiceUnknown is the choice index of a click whose parameter
	// is missing or malformed: no button is named by it.
	gchatChoiceUnknown = -1
)

// gchatCommonEvent is commonEventObject of the add-on event object (and
// `common` of the legacy one): the function a clicked button named and its
// parameters.
type gchatCommonEvent struct {
	InvokedFunction string            `json:"invokedFunction"`
	Parameters      map[string]string `json:"parameters"`
}

// gchatLegacyAction is `action` of the legacy CARD_CLICKED event: the
// button's function under its legacy name, and its parameters as a list.
type gchatLegacyAction struct {
	ActionMethodName string `json:"actionMethodName"`
	Parameters       []struct {
		Key   string `json:"key"`
		Value string `json:"value"`
	} `json:"parameters"`
}

// gchatAccessoryWidget and gchatButton are the accessory widgets as a
// clicked message carries them on the wire: the button list the renderer
// drew (gchatChatWidgets), read back to tell a link button from a choice
// and to name the clicked one when the question is not remembered.
type gchatAccessoryWidget struct {
	ButtonList struct {
		Buttons []gchatButton `json:"buttons"`
	} `json:"buttonList"`
}

type gchatButton struct {
	Text    string `json:"text"`
	OnClick struct {
		OpenLink struct {
			URL string `json:"url"`
		} `json:"openLink"`
		Action struct {
			Function   string `json:"function"`
			Parameters []struct {
				Key   string `json:"key"`
				Value string `json:"value"`
			} `json:"parameters"`
		} `json:"action"`
	} `json:"onClick"`
}

// gchatClick is what a CARD_CLICKED event carries beside the message and
// space: the clicker, the button's function and parameters, and the event's
// time, which tells one click on a message from the next.
type gchatClick struct {
	User       gchatSender
	Function   string
	Parameters map[string]string
	EventTime  string
}

// gchatQuestion is a posted message with choice buttons, remembered so its
// answer can rewrite it.
type gchatQuestion struct {
	conversation string
	asks         bool
	layout       gchatChatLayout
	choices      []string
	links        []chatmsg.Link
}

// gchatPendingClick is a click handed to the gateway and not yet verified:
// the message it is on, and what the rewrite needs, taken from the question
// when it is remembered and from the event's message otherwise.
type gchatPendingClick struct {
	message string
	clicker string
	label   string
	text    string
	links   []chatmsg.Link
	// click is set for a click, which claims the question while it waits
	// (waiting); a typed reply claims nothing and releases nothing.
	click bool
}

// gchatUX is the click state, held under the adapter's lock. Each ring holds
// at most uxCap keys, evicted oldest first; a question evicted keeps
// its buttons, and its answer is still a turn.
type gchatUX struct {
	questions      map[string]gchatQuestion
	questionsOrder []string
	// open is the conversation's newest question that takes a typed
	// answer, by conversation key.
	open map[string]string
	// pending is by click id (gchatClickID) or typed message name, and
	// waiting by the message a pending click is on (a typed reply claims
	// nothing); every one the gateway is handed is verified or dropped
	// (InboundVerified), so neither needs a ring.
	pending map[string]gchatPendingClick
	waiting map[string]bool
	// answered marks a question whose answer has been taken, and lines
	// holds the line an answered message shows, which a later edit keeps.
	answered     map[string]bool
	lines        map[string]string
	answeredRing []string
}

func (a *GoogleChatAdapter) uxLocked() *gchatUX {
	if a.ux == nil {
		a.ux = &gchatUX{questions: map[string]gchatQuestion{}, open: map[string]string{}, pending: map[string]gchatPendingClick{},
			waiting: map[string]bool{}, answered: map[string]bool{}, lines: map[string]string{}}
	}
	return a.ux
}

// trackQuestion remembers a laid-out message with choices, keyed by its
// name and bound to conversation, so a click or a typed reply can rewrite
// it. A home post has no conversation here (its space's kind is not known
// without a read), so its click is bound by the event and a typed reply in
// its thread does not answer it. Laying
// choices out on a message opens it, even one answered before: a card
// asking again on its update key is a fresh question, so the earlier answer
// and its line are forgotten. Laying a message out again without choices
// closes it, so a late click on buttons a client still shows does not write
// the old question back.
func (a *GoogleChatAdapter) trackQuestion(name, conversation string, m *chatmsg.Message, layout gchatChatLayout) {
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	if m == nil || len(m.Choices) == 0 {
		// A click waiting on the message, remembered or not (one from
		// before a restart is not), would patch its stale snapshot of the
		// text over this layout once verified.
		ux.forgetClicksLocked(name)
		if _, ok := ux.questions[name]; ok {
			ux.dropQuestionLocked(name)
			forget(&ux.questionsOrder, name)
		}
		return
	}
	labels := make([]string, len(m.Choices))
	for i, c := range m.Choices {
		labels[i] = c.Text
	}
	q := gchatQuestion{conversation: conversation, asks: asksQuestion(m.Choices), layout: layout, choices: labels, links: m.Links}
	ux.questions[name] = q
	remember(&ux.questionsOrder, name, func(k string) { ux.dropQuestionLocked(k) })
	switch {
	case q.asks && conversation != "":
		ux.open[conversation] = name
	case ux.open[conversation] == name:
		// Re-laid with a next step alone: it asks nothing now.
		delete(ux.open, conversation)
	}
	ux.forgetClicksLocked(name)
	ux.forgetAnswerLocked(name)
}

// forgetAnswer forgets a message's answer and its line: it asks anew.
func (a *GoogleChatAdapter) forgetAnswer(name string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.uxLocked().forgetAnswerLocked(name)
}

// forgetAnswerLocked is forgetAnswer under the adapter's lock.
func (ux *gchatUX) forgetAnswerLocked(name string) {
	delete(ux.answered, name)
	delete(ux.lines, name)
	forget(&ux.answeredRing, name)
}

// dropQuestionLocked forgets a question, and the open slot that names it.
func (ux *gchatUX) dropQuestionLocked(name string) {
	if q, ok := ux.questions[name]; ok && ux.open[q.conversation] == name {
		delete(ux.open, q.conversation)
	}
	delete(ux.questions, name)
}

// forgetClicksLocked drops the clicks and typed answers waiting on a message
// that was just laid out again: their snapshot of its text is stale, and
// the gateway's verdict on them would otherwise patch the old question over
// the new layout. The turn they started is the gateway's as before.
func (ux *gchatUX) forgetClicksLocked(name string) {
	for id, p := range ux.pending {
		if p.message == name {
			delete(ux.pending, id)
		}
	}
	delete(ux.waiting, name)
}

// noteTypedAnswer reads a typed message against the conversation's open
// question: when one is open and the text is not a command, the message is
// a pending answer to it, keyed by the message's own id, settled when the
// gateway says whether the sender is verified (InboundVerified). It has no
// effect on the turn itself, which the gateway handles as typed.
func (a *GoogleChatAdapter) noteTypedAnswer(msg InboundMessage, sender gchatSender) {
	if chattext.IsCommand(msg.Text) {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	name := ux.open[msg.Conversation]
	q, ok := ux.questions[name]
	if name == "" || !ok {
		return
	}
	clicker := sender.DisplayName
	if clicker == "" {
		clicker = namelessClicker
	}
	// A typed reply claims nothing: a click on the question while the
	// typer awaits verification is still a turn, and whichever the gateway
	// verifies first answers (InboundVerified).
	ux.pending[msg.MessageID] = gchatPendingClick{message: name, clicker: clicker, label: slackTypedWords(msg.Text), text: q.layout.Text, links: q.links}
}

// classifyClick normalizes one button click to an InboundMessage, or names
// why it is not a turn: a button this renderer did not draw, a clicker Chat
// did not name, a question already answered or already waiting on a click.
func (a *GoogleChatAdapter) classifyClick(ev *gchatEvent) (InboundMessage, string) {
	click := ev.click
	if click == nil {
		return InboundMessage{}, "a click event with no click"
	}
	if click.Function != gchatChatFunction {
		return InboundMessage{}, "a click on a button this adapter did not draw (" + strconv.Quote(click.Function) + ")"
	}
	reply := strings.TrimSpace(click.Parameters[gchatChatParamReply])
	if reply == "" {
		return InboundMessage{}, "a click with no reply"
	}
	if click.User.Email == "" {
		return InboundMessage{}, "clicker has no email"
	}
	if ev.Space.Name == "" || ev.Message.Name == "" {
		return InboundMessage{}, "click names no space or no message"
	}
	kind, thread := gchatSurface(ev)
	index, err := strconv.Atoi(click.Parameters[gchatChatParamChoice])
	if err != nil {
		index = gchatChoiceUnknown
	}
	id := gchatClickID(ev.Message.Name, click.EventTime)
	a.mu.Lock()
	ux := a.uxLocked()
	// A click with no event time has no id of its own, so it is not run
	// through the dedupe ring: the message name alone would make every
	// later click on the message a duplicate.
	if click.EventTime != "" && a.seenLocked(id) {
		a.mu.Unlock()
		return InboundMessage{}, "duplicate delivery of click " + id
	}
	if ux.answered[ev.Message.Name] {
		a.mu.Unlock()
		return InboundMessage{}, "the question is already answered (" + ev.Message.Name + ")"
	}
	if ux.waiting[ev.Message.Name] {
		a.mu.Unlock()
		return InboundMessage{}, "a click on the question is already waiting (" + ev.Message.Name + ")"
	}
	p := gchatPendingClick{message: ev.Message.Name, clicker: click.User.DisplayName, label: reply, click: true}
	if p.clicker == "" {
		p.clicker = namelessClicker
	}
	bound := ""
	if q, ok := ux.questions[ev.Message.Name]; ok {
		// A remembered question binds its click to the conversation it was
		// posted in; the event's surface is read only for one it does not
		// remember (a home post, or one from before a restart).
		bound = q.conversation
		p.text, p.links = q.layout.Text, q.links
		if index >= 0 && index < len(q.choices) {
			p.label = q.choices[index]
		}
	} else {
		// Posted before a restart: the message the event carries is what
		// Chat shows, text and buttons.
		p.text = ev.Message.Text
		p.links, p.label = gchatMessageButtons(ev.Message, index, p.label)
	}
	ux.pending[id] = p
	ux.waiting[ev.Message.Name] = true
	// The clicker spoke, in the roster's sense: their id maps to their
	// email from here, as after a typed message.
	a.rememberUserLocked(click.User)
	a.mu.Unlock()
	if bound == "" {
		bound = gchatConversationID(ev.Space.Name, thread, kind)
	}
	return InboundMessage{
		Conversation: bound,
		Kind:         kind,
		AuthorID:     click.User.Email,
		MessageID:    id,
		Text:         reply,
		Click:        true,
	}, ""
}

// gchatClickID names one click for the audit chain and the pending map: the
// clicked message and the event's time, so two clicks on one message (a
// question asked again on its update key) are two ids, as Slack's clicks
// are by their own ts. An event with no time names the message alone, and
// is not deduplicated by it (classifyClick).
func gchatClickID(message, eventTime string) string {
	if eventTime == "" {
		return message
	}
	return message + gchatClickIDSep + eventTime
}

// gchatMessageButtons reads a clicked message's own buttons: the link
// buttons it shows, as links, and the label of the choice at index, or
// fallback when no button carries that index (or the index is unknown).
func gchatMessageButtons(m gchatMessage, index int, fallback string) (links []chatmsg.Link, label string) {
	label = fallback
	for _, w := range m.AccessoryWidgets {
		for _, b := range w.ButtonList.Buttons {
			if b.OnClick.OpenLink.URL != "" {
				links = append(links, chatmsg.Link{Text: b.Text, URL: b.OnClick.OpenLink.URL})
				continue
			}
			if b.OnClick.Action.Function != gchatChatFunction || index == gchatChoiceUnknown {
				continue
			}
			for _, param := range b.OnClick.Action.Parameters {
				if param.Key == gchatChatParamChoice && param.Value == strconv.Itoa(index) && b.Text != "" {
					label = b.Text
				}
			}
		}
	}
	return links, label
}

// InboundVerified learns whether a click, or a typed reply to an open
// question, passed sender verification (VerifiedObserver). A verified one
// answers its question: the message is rewritten once, and later clicks on
// it are dropped. A dropped one changes nothing, and the question stays
// open. The relay call runs off the
// inbox worker, so a slow relay does not hold the clicked turn.
func (a *GoogleChatAdapter) InboundVerified(conversation, messageID string, verified bool) {
	a.mu.Lock()
	ux := a.uxLocked()
	p, ok := ux.pending[messageID]
	delete(ux.pending, messageID)
	if !ok {
		a.mu.Unlock()
		return
	}
	if p.click {
		delete(ux.waiting, p.message)
	}
	if !verified || ux.answered[p.message] {
		// Dropped, or answered since by the other way (a typed reply and
		// a click both pending): the first verified answer stands.
		a.mu.Unlock()
		return
	}
	ux.answered[p.message] = true
	line := defangGchatControls(fmt.Sprintf(answeredLine, p.clicker, p.label))
	remember(&ux.answeredRing, p.message, func(k string) { delete(ux.answered, k); delete(ux.lines, k) })
	ux.dropQuestionLocked(p.message)
	forget(&ux.questionsOrder, p.message)
	a.mu.Unlock()
	layout := gchatAnsweredLayout(gchatChatLayout{Text: p.text}, p.links, line)
	a.goRewrite(func() { a.rewriteAnswered(conversation, p.message, layout, line) })
}

// rewriteAnswered patches an answered question to layout, and, when the
// message cannot be patched, posts the line in its conversation instead so
// the answer is on record where the question is.
func (a *GoogleChatAdapter) rewriteAnswered(conversation, messageID string, layout gchatChatLayout, line string) {
	arguments := map[string]any{
		"name":       messageID,
		"updateMask": gchatLayoutUpdateMask,
		"body":       gchatLayoutBody(layout),
	}
	err := a.apiCall([]string{"spaces", "messages"}, "patch", arguments, nil)
	if err == nil {
		// Only once it is on the message: a later edit keeps the line it
		// shows (answeredLayout), and one posted in the conversation
		// instead, below, must not show twice.
		a.mu.Lock()
		if ux := a.uxLocked(); ux.answered[messageID] {
			ux.lines[messageID] = line
		}
		a.mu.Unlock()
		return
	}
	a.log.Warn("gchat: rewriting an answered question failed; posting the answer in its conversation", "message", messageID, "err", err)
	if _, err := a.Post(conversation, line); err != nil {
		a.log.Warn("gchat: posting an answered line failed", "conversation", conversation, "err", err)
	}
}

// goRewrite runs a rewrite off the inbox worker, or inline under test.
func (a *GoogleChatAdapter) goRewrite(f func()) {
	if a.syncRewrites {
		f()
		return
	}
	go f()
}

// gchatAnsweredLayout is a question's layout with its choice buttons gone,
// its links kept, and line under its text.
func gchatAnsweredLayout(layout gchatChatLayout, links []chatmsg.Link, line string) gchatChatLayout {
	text := strings.TrimRight(layout.Text, "\n")
	if text != "" {
		text += gchatChatSectionGap
	}
	return gchatChatLayout{Text: text + line, Widgets: gchatChatWidgets(nil, links)}
}

// answeredLayout keeps an answered question's line through a later edit of
// the message that lays out no choices (the card settling it on the notify
// route, laid out or as text): the line stays under the new text. An edit
// that lays choices out asks anew and is left as laid out (trackQuestion
// then forgets the answer).
func (a *GoogleChatAdapter) answeredLayout(messageID string, m OutMessage, layout gchatChatLayout) gchatChatLayout {
	a.mu.Lock()
	line := a.uxLocked().lines[messageID]
	a.mu.Unlock()
	if line == "" || (m.Chat != nil && len(m.Chat.Choices) > 0) {
		return layout
	}
	var links []chatmsg.Link
	if m.Chat != nil {
		links = m.Chat.Links
	}
	return gchatAnsweredLayout(layout, links, line)
}

// gchatSurface binds an event to its conversation's kind and thread: a DM
// only on a positive signal, since a space misread as a DM would bind every
// thread in it to one session; no thread in a space whose threading state
// does not support replies or in a group chat, where every message has its
// own thread resource and binding those would fragment the chat into one
// session per ask; and in a DM, which Chat threads too, only a message
// typed inside an existing thread (threadReply) has one, so top-level DMs
// share the space's one conversation and are answered top-level, the
// default-mode Hermes Google Chat rule read from Chat's own flag
// (spec-chatops-gateway.md, "The Google Chat adapter").
func gchatSurface(ev *gchatEvent) (kind, thread string) {
	kind = sessionKindGroup
	if ev.Space.SpaceType == gchatSpaceTypeDM || ev.Space.Type == gchatLegacySpaceTypeDM {
		kind = sessionKindDM
	}
	thread = ev.Message.Thread.Name
	if ev.Space.SpaceThreadingState == gchatThreadingUnthreaded || ev.Space.SpaceType == gchatSpaceTypeGroupChat {
		thread = ""
	}
	if kind == sessionKindDM && !ev.Message.ThreadReply {
		thread = ""
	}
	return kind, thread
}

// decodeGchatClick reads a buttonClickedPayload add-on event into a click
// event: the clicked message and its space from the payload, the clicker
// from chat.user, the event time from chat.eventTime, the button's function
// and parameters from commonEventObject.
func decodeGchatClick(payload, user, eventTime json.RawMessage, common gchatCommonEvent) (*gchatEvent, error) {
	var mp gchatAddonMessagePayload
	if err := json.Unmarshal(payload, &mp); err != nil {
		return nil, fmt.Errorf("chat.%s: %w", gchatAddonClickKey, err)
	}
	click := &gchatClick{Function: common.InvokedFunction, Parameters: common.Parameters}
	if len(user) > 0 {
		_ = json.Unmarshal(user, &click.User)
	}
	if len(eventTime) > 0 {
		_ = json.Unmarshal(eventTime, &click.EventTime)
	}
	return &gchatEvent{Type: gchatEventTypeClick, Space: mp.Space, Message: mp.Message, shape: gchatShapeAddon, click: click}, nil
}

// decodeGchatLegacyClick reads a CARD_CLICKED event in the legacy layout
// ({"type":"CARD_CLICKED","message":…,"user":…,"space":…,"action":…,
// "common":…}): the clicked message and its space at the top level, the
// clicker under user, the button's function and parameters under action,
// or under common when the event carries it. A Chat app whose events
// arrive in this layout (measured live 2026-10-30 on gkedemos, where
// messages arrive in the add-on layout and clicks in this one) is read the
// same as one on the add-on layout.
func decodeGchatLegacyClick(wire *gchatWireEvent) *gchatEvent {
	click := &gchatClick{User: wire.User, EventTime: wire.EventTime,
		Function: wire.LegacyCommon.InvokedFunction, Parameters: wire.LegacyCommon.Parameters}
	if click.Function == "" {
		click.Function = wire.Action.ActionMethodName
	}
	if len(click.Parameters) == 0 && len(wire.Action.Parameters) > 0 {
		click.Parameters = make(map[string]string, len(wire.Action.Parameters))
		for _, p := range wire.Action.Parameters {
			click.Parameters[p.Key] = p.Value
		}
	}
	return &gchatEvent{Type: gchatEventTypeClick, Space: wire.Space, Message: wire.Message, shape: gchatShapeLegacy, click: click}
}

var _ VerifiedObserver = (*GoogleChatAdapter)(nil)
