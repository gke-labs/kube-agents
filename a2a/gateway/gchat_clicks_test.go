package gateway

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// gchatClickEvent is the add-on click fixture as a distinct click each time:
// Pub/Sub redelivers an event with its time unchanged, so a test that means
// a redelivery classifies one event twice, and one that means another click
// calls this again.
func gchatClickEvent(t *testing.T) *gchatEvent {
	t.Helper()
	ev := gchatFixtureEvent(t, "addon-dm-button-click.json", nil)
	ev.click.EventTime = fmt.Sprintf("2026-10-29T20:01:%02d.000000Z", clickEvents.Add(1))
	return ev
}

var clickEvents atomic.Int32

// A button click in the add-on shape decodes to a click event: the clicked
// message and space from the payload, the clicker from chat.user, the
// function and parameters from commonEventObject.
func TestDecodeGchatClick(t *testing.T) {
	ev := gchatClickEvent(t)
	if ev.Type != gchatEventTypeClick || ev.click == nil {
		t.Fatalf("event = %+v", ev)
	}
	if ev.click.Function != gchatChatFunction || ev.click.Parameters[gchatChatParamReply] != "Look at the first one: prod-eu" ||
		ev.click.User.Email != "sender@example.com" || ev.click.User.DisplayName != "Test Sender" {
		t.Errorf("click = %+v", ev.click)
	}
	if ev.Message.Name != "spaces/_c2fixtureAA/messages/BOT1.BOT1" || ev.Space.SpaceType != gchatSpaceTypeDM {
		t.Errorf("message = %+v space = %+v", ev.Message, ev.Space)
	}
}

// A click in the legacy event layout, as the gkedemos app delivered one
// live on 2026-10-30 (testdata/gchat/legacy-dm-button-click.json, the
// clicker redacted), decodes to the same click event: the function and
// parameters from common, or from action when common is not carried, the
// clicker from user, the time from eventTime, and the clicked message with
// its buttons at the top level.
func TestDecodeGchatLegacyClick(t *testing.T) {
	raw, err := os.ReadFile("testdata/gchat/legacy-dm-button-click.json")
	if err != nil {
		t.Fatal(err)
	}
	check := func(name string, ev *gchatEvent) {
		t.Helper()
		if ev.Type != gchatEventTypeClick || ev.click == nil || ev.click.Function != gchatChatFunction ||
			ev.click.Parameters[gchatChatParamReply] != "platform-agent-host: which GCP project is it in? Answer in one line." ||
			ev.click.Parameters[gchatChatParamChoice] != "1" || ev.click.User.Email != "sender@example.com" ||
			ev.click.User.DisplayName != "Test Sender" || ev.click.EventTime != "2026-10-30T01:46:08.760777Z" ||
			ev.Message.Name != "spaces/rj0PRyAAAAE/messages/SiNPVaUGVPc.SiNPVaUGVPc" || ev.Space.SpaceType != gchatSpaceTypeDM ||
			ev.Message.ThreadReply || len(ev.Message.AccessoryWidgets) != 1 {
			t.Errorf("%s: event = %+v click = %+v", name, ev, ev.click)
		}
	}
	ev, err := decodeGchatEvent(base64.StdEncoding.EncodeToString(raw))
	if err != nil {
		t.Fatal(err)
	}
	check("as captured", ev)
	// The clicked message's own buttons name the clicked one, and its link.
	links, label := gchatMessageButtons(ev.Message, 1, "fallback")
	if label != "platform-agent-host" || len(links) != 1 || links[0].Text != "Cloud Console" {
		t.Errorf("buttons read back: label %q links %v", label, links)
	}
	// Without common, action alone carries the function and parameters.
	var wire map[string]any
	if err := json.Unmarshal(raw, &wire); err != nil {
		t.Fatal(err)
	}
	delete(wire, "common")
	actionOnly, _ := json.Marshal(wire)
	ev, err = decodeGchatEvent(base64.StdEncoding.EncodeToString(actionOnly))
	if err != nil {
		t.Fatal(err)
	}
	check("action only", ev)
}

// A click is a text turn from the clicker: the choice's reply, in the
// conversation the clicked message sits in, marked as a click. A second
// click while the first waits, or after the question is answered, is
// dropped; a click on a button this renderer did not draw is not a turn.
func TestGchatClickIsATurnOnce(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	f.responses["spaces.messages/patch"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	a := newTestGchatAdapterWithRelay(t, f)
	a.syncRewrites = true
	question := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Which cluster?", Detail: "Two clusters run one.",
		Choices: []chatmsg.Choice{{Text: "Look at the first one", Reply: "Look at the first one: prod-eu"}, {Text: "Skip"}},
		Links:   []chatmsg.Link{{Text: "board", URL: "https://x.example/b"}}}
	if _, err := a.PostMessage("gchat:dm/spaces/_c2fixtureAA", OutMessage{Text: "fallback", Chat: question}); err != nil {
		t.Fatal(err)
	}

	msg, reason := a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatalf("click dropped: %s", reason)
	}
	// The bot's top-level DM post is not a thread reply, so the click
	// lands in the DM's one conversation, as a typed reply there would.
	// The click's id is the message and the event time, so two clicks on
	// one message are two ids in the ingress log.
	if msg.Conversation != "gchat:dm/spaces/_c2fixtureAA" || msg.Kind != "dm" || msg.AuthorID != "sender@example.com" ||
		!strings.HasPrefix(msg.MessageID, "spaces/_c2fixtureAA/messages/BOT1.BOT1#2026-10-29T20:01:") || msg.Text != "Look at the first one: prod-eu" || !msg.Click {
		t.Errorf("click turn = %+v", msg)
	}
	if _, reason := a.classify(gchatClickEvent(t)); !strings.Contains(reason, "already waiting") {
		t.Errorf("a second click while the first waits: %q", reason)
	}

	// Verified: the question is rewritten once, choices gone, links kept,
	// the clicker named by display name and the choice by its label.
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	c := f.call(1)
	body := c.arguments["body"].(map[string]any)
	if c.method != "patch" || c.arguments["name"] != "spaces/_c2fixtureAA/messages/BOT1.BOT1" || c.arguments["updateMask"] != gchatLayoutUpdateMask {
		t.Fatalf("rewrite = %+v", c)
	}
	if want := "*Which cluster?*\n\nTwo clusters run one.\n\n✓ Test Sender: Look at the first one"; body["text"] != want {
		t.Errorf("rewritten text = %q, want %q", body["text"], want)
	}
	buttons := body["accessoryWidgets"].([]any)[0].(map[string]any)["buttonList"].(map[string]any)["buttons"].([]any)
	if len(buttons) != 1 || buttons[0].(map[string]any)["text"] != "board" {
		t.Errorf("rewritten buttons = %v", buttons)
	}
	if _, reason := a.classify(gchatClickEvent(t)); !strings.Contains(reason, "already answered") {
		t.Errorf("a click after the answer: %q", reason)
	}

	// A later edit of the answered message that no longer asks keeps the
	// line under its new text, and draws no choice buttons.
	question1 := "spaces/_c2fixtureAA/messages/BOT1.BOT1"
	settled := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "prod-eu is on 1.30", Links: question.Links}
	if err := a.EditMessage(msg.Conversation, question1, OutMessage{Text: "fallback", Chat: settled}); err != nil {
		t.Fatal(err)
	}
	body = f.call(2).arguments["body"].(map[string]any)
	if want := "*prod-eu is on 1.30*\n\n✓ Test Sender: Look at the first one"; body["text"] != want {
		t.Errorf("settled text = %q, want %q", body["text"], want)
	}

	// Laid out again with choices (a card asking again on its update key),
	// the message is a fresh question: the earlier answer and its line are
	// forgotten, and a click answers it anew.
	if err := a.EditMessage(msg.Conversation, question1, OutMessage{Text: "fallback", Chat: question}); err != nil {
		t.Fatal(err)
	}
	if body := f.call(3).arguments["body"].(map[string]any); strings.Contains(body["text"].(string), "✓") {
		t.Errorf("a re-asked question keeps the old answered line: %q", body["text"])
	}
	msg, reason = a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatalf("a re-asked question refused its click: %s", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	if body := f.call(4).arguments["body"].(map[string]any); !strings.HasSuffix(body["text"].(string), "✓ Test Sender: Look at the first one") {
		t.Errorf("re-asked rewrite = %q", body["text"])
	}

	// Not drawn by this adapter, no reply, no clicker, no message: not a
	// turn, each named.
	// A click in a threaded space binds to the message's thread, as a
	// typed reply there would.
	space := gchatClickEvent(t)
	space.Space.SpaceType = "SPACE"
	space.Space.Type = "ROOM"
	space.Space.SpaceThreadingState = "THREADED_MESSAGES"
	space.Message.ThreadReply = true
	space.Message.Name = "spaces/_c2fixtureAA/messages/BOT2.BOT2"
	if msg, reason := a.classify(space); reason != "" || msg.Conversation != "gchat:spaces/_c2fixtureAA/threads/BOT1" || msg.Kind != sessionKindGroup {
		t.Errorf("space click = %+v (%s)", msg, reason)
	}

	for _, tc := range []struct {
		name, want string
		edit       func(ev *gchatEvent)
	}{
		{"foreign button", "did not draw", func(ev *gchatEvent) { ev.click.Function = "someone.else" }},
		{"no reply", "no reply", func(ev *gchatEvent) { delete(ev.click.Parameters, gchatChatParamReply) }},
		{"no email", "no email", func(ev *gchatEvent) { ev.click.User.Email = "" }},
		{"no message", "no space or no message", func(ev *gchatEvent) { ev.Message.Name = "" }},
		{"no click", "no click", func(ev *gchatEvent) { ev.click = nil }},
	} {
		ev := gchatClickEvent(t)
		tc.edit(ev)
		if _, reason := a.classify(ev); !strings.Contains(reason, tc.want) {
			t.Errorf("%s: %q", tc.name, reason)
		}
	}
	if _, err := decodeGchatEvent(base64.StdEncoding.EncodeToString([]byte(`{"chat":{"buttonClickedPayload":"nonsense"}}`))); err == nil {
		t.Error("an unreadable click payload must error")
	}
}

// A choice's label or a clicker's name is model- or user-authored text that
// is patched into the message: the answered line is defanged like every
// other string, so a mention in one cannot ping. A clicker Chat names
// nowhere is "Someone", never their address. A rewrite the API refuses posts
// the line in the conversation instead.
func TestGchatClickLineIsDefangedAndFallsBack(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	f.responses["spaces.messages/patch"] = fakeChatAPIError{status: 500, reason: "backend"}
	a := newTestGchatAdapterWithRelay(t, f)
	a.syncRewrites = true
	question := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Ping?", Choices: []chatmsg.Choice{{Text: "<users/all> now", Reply: "Look at the first one: prod-eu"}}}
	if _, err := a.PostMessage("gchat:dm/spaces/_c2fixtureAA", OutMessage{Text: "fallback", Chat: question}); err != nil {
		t.Fatal(err)
	}
	ev := gchatClickEvent(t)
	ev.click.User.DisplayName = ""
	msg, reason := a.classify(ev)
	if reason != "" {
		t.Fatal(reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	patch := f.call(1)
	if patch.method != "patch" || !strings.HasSuffix(patch.arguments["body"].(map[string]any)["text"].(string), "✓ Someone: < users/all> now") {
		t.Errorf("rewrite = %+v", patch)
	}
	// The patch failed: the line posts in the conversation, top-level in
	// the DM, defanged.
	post := f.call(2)
	body := post.arguments["body"].(map[string]any)
	if post.method != "create" || post.arguments["parent"] != "spaces/_c2fixtureAA" || body["thread"] != nil || body["text"] != "✓ Someone: < users/all> now" {
		t.Errorf("fallback post = %+v", post)
	}
	// The line is on record in the conversation, not on the message, so a
	// later edit of the message without choices does not show it again.
	f.responses["spaces.messages/patch"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	if err := a.EditMessage(msg.Conversation, "spaces/_c2fixtureAA/messages/BOT1.BOT1", OutMessage{Text: "settled"}); err != nil {
		t.Fatal(err)
	}
	if body := f.call(3).arguments["body"].(map[string]any); strings.Contains(body["text"].(string), "✓") {
		t.Errorf("the answered line shows twice: %q", body["text"])
	}
}

// The chat.notify route's home posts lay out too (notifyChatPoster): the
// same message into the space, the thread it landed in back, and the
// question remembered for its click.
func TestGchatPostNotifyMessageLaysOut(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/S1/messages/M1", "thread": map[string]any{"name": "spaces/S1/threads/M1"}}
	a := newTestGchatAdapterWithRelay(t, f)
	m := OutMessage{Text: "fallback", Chat: &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Audit: 2 findings", Choices: []chatmsg.Choice{{Text: "Fix the first"}}}}
	id, landed, err := a.PostNotifyMessage("spaces/S1", "", m)
	if err != nil || id != "spaces/S1/messages/M1" || landed != "spaces/S1/threads/M1" {
		t.Fatalf("notify post: %q %q %v", id, landed, err)
	}
	body := f.call(0).arguments["body"].(map[string]any)
	if body["text"] != "*Audit: 2 findings*" || len(body["accessoryWidgets"].([]any)) != 1 || body["thread"] != nil {
		t.Errorf("home post = %v", body)
	}
	a.mu.Lock()
	_, tracked := a.uxLocked().questions["spaces/S1/messages/M1"]
	a.mu.Unlock()
	if !tracked {
		t.Error("a home question is not remembered for its click")
	}
	if _, _, err := a.PostNotifyMessage("spaces/S1", "spaces/S1/threads/T2", OutMessage{Text: "**text** only"}); err != nil {
		t.Fatal(err)
	}
	if body := f.call(1).arguments["body"].(map[string]any); body["text"] != "*text* only" || body["thread"].(map[string]any)["name"] != "spaces/S1/threads/T2" {
		t.Errorf("text notify = %v", body)
	}
	if _, _, err := a.PostNotifyMessage("not-a-space", "", m); err == nil {
		t.Error("a non-space must error")
	}
}

// A click the gateway drops changes nothing: no rewrite, and the question
// stays open for the next click. A click on a question posted before a
// restart is still a turn, and its rewrite reads the message the event
// carries: its text, the clicked button's label, its link buttons kept and
// its choice buttons gone.
func TestGchatClickUnverifiedAndUnknown(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/patch"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	a := newTestGchatAdapterWithRelay(t, f)
	a.syncRewrites = true

	msg, reason := a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatalf("click dropped: %s", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, false)
	if len(f.calls) != 0 {
		t.Errorf("an unverified click made a call: %+v", f.calls)
	}
	msg, reason = a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatalf("the question is still open after a dropped click: %s", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	body := f.call(0).arguments["body"].(map[string]any)
	// The event carries the message: its text, the clicked button's label
	// and its link buttons, which the rewrite keeps.
	if want := "*Which cluster?*\n\nTwo clusters run one.\n\n✓ Test Sender: Look at the first one"; body["text"] != want {
		t.Errorf("rewritten text = %q, want %q", body["text"], want)
	}
	buttons := body["accessoryWidgets"].([]any)[0].(map[string]any)["buttonList"].(map[string]any)["buttons"].([]any)
	if len(buttons) != 1 || buttons[0].(map[string]any)["text"] != "board" {
		t.Errorf("widgets after a restart rewrite = %v", buttons)
	}
}

// A typed reply in a conversation whose question is open answers it: the
// question loses its choice buttons and gains the reply's first words under
// the clicker's name. A command is no reply, a finished answer's next step
// takes none, and an edit that lays the message out without choices closes
// the question. A question remembered from its post binds its click to the
// conversation it was posted in, whatever surface the event names.
func TestGchatTypedReplyAnswersAndEditsClose(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	f.responses["spaces.messages/patch"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	a := newTestGchatAdapterWithRelay(t, f)
	a.syncRewrites = true
	side := "gchat:dm/spaces/_c2fixtureAA/threads/SIDE"
	question := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Which cluster?",
		Choices: []chatmsg.Choice{{Text: "prod-eu"}, {Text: "dev"}}, Links: []chatmsg.Link{{Text: "board", URL: "https://x.example/b"}}}
	if _, err := a.PostMessage(side, OutMessage{Text: "fallback", Chat: question}); err != nil {
		t.Fatal(err)
	}

	// A command typed there is no answer.
	typed := func(text, name string) InboundMessage {
		msg := InboundMessage{Conversation: side, Kind: sessionKindDM, AuthorID: "sender@example.com", MessageID: name, Text: text}
		a.noteTypedAnswer(msg, gchatSender{DisplayName: "Test Sender", Email: "sender@example.com"})
		return msg
	}
	stop := typed("stop", "spaces/_c2fixtureAA/messages/U1")
	a.InboundVerified(stop.Conversation, stop.MessageID, true)
	if len(f.calls) != 1 {
		t.Fatalf("a command answered the question: %+v", f.calls[1:])
	}
	// A typed reply does, once verified, with its first words.
	reply := typed("the prod-eu one, please", "spaces/_c2fixtureAA/messages/U2")
	a.InboundVerified(reply.Conversation, reply.MessageID, true)
	body := f.call(1).arguments["body"].(map[string]any)
	if want := "*Which cluster?*\n\n✓ Test Sender: the prod-eu one, please"; body["text"] != want {
		t.Errorf("typed answer rewrite = %q, want %q", body["text"], want)
	}
	if buttons := body["accessoryWidgets"].([]any)[0].(map[string]any)["buttonList"].(map[string]any)["buttons"].([]any); len(buttons) != 1 {
		t.Errorf("links after a typed answer = %v", buttons)
	}
	if _, reason := a.classify(gchatClickEvent(t)); !strings.Contains(reason, "already answered") {
		t.Errorf("a click after a typed answer: %q", reason)
	}

	// Asked again on the side thread: a click's event names the top-level
	// DM (the bot's message is no thread reply to Chat), but the question
	// binds its click to the conversation it was posted in.
	if err := a.EditMessage(side, "spaces/_c2fixtureAA/messages/BOT1.BOT1", OutMessage{Text: "fallback", Chat: question}); err != nil {
		t.Fatal(err)
	}
	msg, reason := a.classify(gchatClickEvent(t))
	if reason != "" || msg.Conversation != side {
		t.Errorf("click on a side-thread question = %+v (%s)", msg, reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)

	// A next step takes no typed answer, even on a message that asked
	// before: re-laid with a next step alone, the question is closed to
	// typed replies.
	if err := a.EditMessage(side, "spaces/_c2fixtureAA/messages/BOT1.BOT1", OutMessage{Text: "fallback", Chat: question}); err != nil {
		t.Fatal(err)
	}
	next := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Done.", Choices: []chatmsg.Choice{{Text: "Check the other one", Next: true}}}
	if err := a.EditMessage(side, "spaces/_c2fixtureAA/messages/BOT1.BOT1", OutMessage{Text: "fallback", Chat: next}); err != nil {
		t.Fatal(err)
	}
	calls := len(f.calls)
	later := typed("thanks", "spaces/_c2fixtureAA/messages/U3")
	a.InboundVerified(later.Conversation, later.MessageID, true)
	if len(f.calls) != calls {
		t.Errorf("a typed reply answered a next step: %+v", f.calls[calls:])
	}

	// A click waiting on verification when its message is laid out again
	// is forgotten: the verdict then patches nothing stale over the new
	// layout. The turn it started is the gateway's as before.
	if err := a.EditMessage(side, "spaces/_c2fixtureAA/messages/BOT1.BOT1", OutMessage{Text: "fallback", Chat: question}); err != nil {
		t.Fatal(err)
	}
	stale, reason := a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatal(reason)
	}
	if err := a.EditMessage(side, "spaces/_c2fixtureAA/messages/BOT1.BOT1", OutMessage{Text: "settled early", Chat: &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Settled early."}}); err != nil {
		t.Fatal(err)
	}
	calls = len(f.calls)
	a.InboundVerified(stale.Conversation, stale.MessageID, true)
	if len(f.calls) != calls {
		t.Errorf("a stale click rewrote a re-laid message: %+v", f.calls[calls:])
	}

	// Laid out again without choices, the question is closed: a late click
	// is still a turn, rewritten from the event's own message, never from
	// the old layout.
	if err := a.EditMessage(side, "spaces/_c2fixtureAA/messages/BOT1.BOT1", OutMessage{Text: "settled", Chat: &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Settled."}}); err != nil {
		t.Fatal(err)
	}
	calls = len(f.calls)
	msg, reason = a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatal(reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	if body := f.call(calls).arguments["body"].(map[string]any); !strings.HasPrefix(body["text"].(string), "*Which cluster?*\n\nTwo clusters run one.") {
		t.Errorf("late click rewrote from the old layout: %q", body["text"])
	}
}

// Pub/Sub is at-least-once: a redelivered click is a duplicate whatever the
// question's state since, even after the question was asked again on the
// same message, where the answered and waiting marks were cleared.
func TestGchatRedeliveredClickIsADuplicate(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	f.responses["spaces.messages/patch"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	a := newTestGchatAdapterWithRelay(t, f)
	a.syncRewrites = true
	question := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Which?", Choices: []chatmsg.Choice{{Text: "Look at the first one"}, {Text: "Skip"}}}
	if _, err := a.PostMessage("gchat:dm/spaces/_c2fixtureAA", OutMessage{Text: "fallback", Chat: question}); err != nil {
		t.Fatal(err)
	}
	ev := gchatClickEvent(t)
	msg, reason := a.classify(ev)
	if reason != "" {
		t.Fatal(reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	if err := a.EditMessage("gchat:dm/spaces/_c2fixtureAA", "spaces/_c2fixtureAA/messages/BOT1.BOT1", OutMessage{Text: "fallback", Chat: question}); err != nil {
		t.Fatal(err)
	}
	if _, reason := a.classify(ev); !strings.Contains(reason, "duplicate delivery") {
		t.Errorf("a redelivered click after a re-ask: %q", reason)
	}
	// A new click (another event time) on the re-asked question is a turn.
	if _, reason := a.classify(gchatClickEvent(t)); reason != "" {
		t.Errorf("a fresh click on the re-asked question: %q", reason)
	}
}

// A typed reply awaiting verification does not block a click on the same
// question: both are turns, and whichever the gateway verifies first
// answers; the other changes nothing.
func TestGchatTypedReplyDoesNotBlockAClick(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	f.responses["spaces.messages/patch"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	a := newTestGchatAdapterWithRelay(t, f)
	a.syncRewrites = true
	conv := "gchat:dm/spaces/_c2fixtureAA"
	question := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Which?", Choices: []chatmsg.Choice{{Text: "Look at the first one"}, {Text: "Skip"}}}
	if _, err := a.PostMessage(conv, OutMessage{Text: "fallback", Chat: question}); err != nil {
		t.Fatal(err)
	}
	typed := InboundMessage{Conversation: conv, Kind: sessionKindDM, AuthorID: "other@example.com", MessageID: "spaces/_c2fixtureAA/messages/U9", Text: "one sec"}
	a.noteTypedAnswer(typed, gchatSender{DisplayName: "Other", Email: "other@example.com"})
	click, reason := a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatalf("a click while a typed reply waits: %s", reason)
	}
	// The click is verified first: it answers. The typer, verified later,
	// changes nothing.
	a.InboundVerified(click.Conversation, click.MessageID, true)
	a.InboundVerified(typed.Conversation, typed.MessageID, true)
	if len(f.calls) != 2 {
		t.Fatalf("calls = %d, want the post and one rewrite", len(f.calls))
	}
	if body := f.call(1).arguments["body"].(map[string]any); !strings.HasSuffix(body["text"].(string), "✓ Test Sender: Look at the first one") {
		t.Errorf("rewrite = %q", body["text"])
	}
}

// A click whose event carries no time has no id of its own: it is still a
// turn, and a later click on the same message is not a duplicate of it.
func TestGchatClickWithoutEventTimeIsNotDeduplicated(t *testing.T) {
	a := newTestGchatAdapterWithRelay(t, newFakeChatRelay(t))
	first := gchatClickEvent(t)
	first.click.EventTime = ""
	msg, reason := a.classify(first)
	if reason != "" || msg.MessageID != "spaces/_c2fixtureAA/messages/BOT1.BOT1" {
		t.Fatalf("click without a time: %+v (%s)", msg, reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, false)
	second := gchatClickEvent(t)
	second.click.EventTime = ""
	if _, reason := a.classify(second); reason != "" {
		t.Errorf("a later click on the message was refused: %q", reason)
	}
}

// A typed reply the gateway drops releases nothing: a click still waiting
// on verification keeps its claim, so a second click is still refused.
func TestGchatDroppedTypedReplyKeepsAClicksClaim(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	a := newTestGchatAdapterWithRelay(t, f)
	conv := "gchat:dm/spaces/_c2fixtureAA"
	question := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Which?", Choices: []chatmsg.Choice{{Text: "Look at the first one"}, {Text: "Skip"}}}
	if _, err := a.PostMessage(conv, OutMessage{Text: "fallback", Chat: question}); err != nil {
		t.Fatal(err)
	}
	typed := InboundMessage{Conversation: conv, Kind: sessionKindDM, AuthorID: "other@example.com", MessageID: "spaces/_c2fixtureAA/messages/U9", Text: "one sec"}
	a.noteTypedAnswer(typed, gchatSender{DisplayName: "Other", Email: "other@example.com"})
	if _, reason := a.classify(gchatClickEvent(t)); reason != "" {
		t.Fatal(reason)
	}
	a.InboundVerified(typed.Conversation, typed.MessageID, false)
	if _, reason := a.classify(gchatClickEvent(t)); !strings.Contains(reason, "already waiting") {
		t.Errorf("a second click after a dropped typed reply: %q", reason)
	}
}

// A click records the clicker's id against their email as a typed message
// does, so the roster maps them and a DM can be opened for them before they
// ever type.
func TestGchatClickRemembersTheClicker(t *testing.T) {
	a := newTestGchatAdapterWithRelay(t, newFakeChatRelay(t))
	if _, reason := a.classify(gchatClickEvent(t)); reason != "" {
		t.Fatal(reason)
	}
	a.mu.Lock()
	id, email := a.userIDs["users/100000000000000000042"], a.userEmails["sender@example.com"]
	a.mu.Unlock()
	if id != "sender@example.com" || email != "users/100000000000000000042" {
		t.Errorf("clicker not remembered: %q %q", id, email)
	}
}

// A question asked again on a message is open anew even when the re-ask's
// layout is refused and the gateway edits the text instead: the text carries
// no earlier answer line.
func TestGchatRefusedReAskCarriesNoStaleAnswer(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	f.responses["spaces.messages/patch"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/BOT1.BOT1"}
	a := newTestGchatAdapterWithRelay(t, f)
	a.syncRewrites = true
	conv := "gchat:dm/spaces/_c2fixtureAA"
	question := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Which?", Choices: []chatmsg.Choice{{Text: "Look at the first one"}, {Text: "Skip"}}}
	if _, err := a.PostMessage(conv, OutMessage{Text: "fallback", Chat: question}); err != nil {
		t.Fatal(err)
	}
	msg, reason := a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatal(reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	huge := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Again?", Choices: question.Choices, Fold: &chatmsg.Fold{Text: strings.Repeat("x", gchatChatTextMax+1)}}
	if err := a.EditMessage(conv, "spaces/_c2fixtureAA/messages/BOT1.BOT1", OutMessage{Text: "Again? fallback", Chat: huge}); !errors.Is(err, ErrLayoutRefused) {
		t.Fatalf("a layout past the cap must be refused: %v", err)
	}
	if err := a.EditMessage(conv, "spaces/_c2fixtureAA/messages/BOT1.BOT1", OutMessage{Text: "Again? fallback"}); err != nil {
		t.Fatal(err)
	}
	if body := f.call(len(f.calls) - 1).arguments["body"].(map[string]any); strings.Contains(body["text"].(string), "✓") {
		t.Errorf("the text fallback of a re-ask carries the old answer: %q", body["text"])
	}
}

// The buttons the renderer writes are the buttons a click event reads back:
// one round trip keeps the two shapes in step.
func TestGchatWidgetsRoundTrip(t *testing.T) {
	widgets := gchatChatWidgets([]chatmsg.Choice{{Text: "A", Reply: "do A"}, {Text: "B"}}, []chatmsg.Link{{Text: "docs", URL: "https://x.example/d"}})
	raw, err := json.Marshal(widgets)
	if err != nil {
		t.Fatal(err)
	}
	var m gchatMessage
	if err := json.Unmarshal([]byte(`{"accessoryWidgets":`+string(raw)+`}`), &m); err != nil {
		t.Fatal(err)
	}
	links, label := gchatMessageButtons(m, 1, "fallback")
	if label != "B" || len(links) != 1 || links[0].URL != "https://x.example/d" || links[0].Text != "docs" {
		t.Errorf("round trip: label %q links %v", label, links)
	}
	if _, label := gchatMessageButtons(m, gchatChoiceUnknown, "fallback"); label != "fallback" {
		t.Errorf("an unknown index names no button: %q", label)
	}
}

// A click waiting on a message the adapter does not remember (posted before
// a restart) is forgotten when the message is laid out again without
// choices, so its verification does not patch the old question back over
// the settled text.
func TestGchatSettlingAnUnrememberedQuestionForgetsItsPendingClick(t *testing.T) {
	a := newTestGchatAdapter(t)
	ev := gchatClickEvent(t)
	if _, reason := a.classifyClick(ev); reason != "" {
		t.Fatalf("click refused: %s", reason)
	}
	a.mu.Lock()
	pending, waiting := len(a.uxLocked().pending), a.uxLocked().waiting[ev.Message.Name]
	a.mu.Unlock()
	if pending != 1 || !waiting {
		t.Fatalf("pending = %d, waiting = %v; want the click held", pending, waiting)
	}
	a.trackQuestion(ev.Message.Name, "", &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Settled"}, gchatChatLayout{Text: "*Settled*"})
	a.mu.Lock()
	pending, waiting = len(a.uxLocked().pending), a.uxLocked().waiting[ev.Message.Name]
	a.mu.Unlock()
	if pending != 0 || waiting {
		t.Errorf("pending = %d, waiting = %v after the settle; want the click forgotten", pending, waiting)
	}
}
