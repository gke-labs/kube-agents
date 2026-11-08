package gateway

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"
)

// A Google Chat message that quotes another in reply is Chat's "ask about
// this": the quoted message is read through the relay and put to the agent
// quoted, under the rule that it is something to check, not an instruction
// (gchat.go, quotedAsk). These tests drive classify and quotedAsk as Run
// does, against the fake relay's spaces.messages.get.

const (
	gchatQuotedName  = "spaces/S1/messages/ALERT1.ALERT1"
	gchatQuotedAlert = "CrashLoopBackOff: payments-api in ns checkout, 14 restarts"
)

// gchatQuotedMsg is gchatMsg with a quote on the message.
func gchatQuotedMsg(spaceName, spaceType, threadName, msgName, text, argumentText, quoted string) *gchatEvent {
	ev := gchatMsg(spaceName, spaceType, "THREADED_MESSAGES", threadName, msgName, text, argumentText, "u1@example.com", "HUMAN")
	ev.Message.Sender.Name = "users/1"
	ev.Message.QuotedMessageMetadata.Name = quoted
	return ev
}

// gchatBareQuotedMsg is a space message that is a mention and a quote and
// nothing else: argumentText present and empty, Google's documented shape.
func gchatBareQuotedMsg(quoted string) *gchatEvent {
	ev := gchatQuotedMsg("spaces/S1", "SPACE", "spaces/S1/threads/T1", "spaces/S1/messages/M1", "@bot", "", quoted)
	empty := ""
	ev.Message.ArgumentText = &empty
	return ev
}

// quotedTurn runs one event the way Run does: classify on the pump, then
// the quote read and composition off it.
func quotedTurn(a *GoogleChatAdapter, ev *gchatEvent) (InboundMessage, string) {
	msg, reason := a.classify(ev)
	if reason != "" {
		return msg, reason
	}
	return a.quotedAsk(context.Background(), ev, msg)
}

func quotedResponse(sender map[string]any, text string) map[string]any {
	return map[string]any{"name": gchatQuotedName, "text": text, "sender": sender}
}

func humanSender(name string) map[string]any {
	return map[string]any{"name": "users/42", "displayName": name, "type": "HUMAN"}
}

// A DM message that quotes another and says nothing of its own is the
// quoted ask: the shared opening, the quote with who posted it, and the rule.
func TestGchatQuotedDMWithNoWordsIsTheQuotedAsk(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/get"] = quotedResponse(humanSender("Alex Doe"), gchatQuotedAlert)
	a := newTestGchatAdapterWithRelay(t, f)

	ev := gchatQuotedMsg("spaces/D1", "DIRECT_MESSAGE", "spaces/D1/threads/T1", "spaces/D1/messages/M1", "", "", gchatQuotedName)
	msg, reason := quotedTurn(a, ev)
	if reason != "" {
		t.Fatalf("dropped: %s", reason)
	}
	want := fmt.Sprintf(gchatQuotedAsk, "> Posted by Alex Doe:\n> "+gchatQuotedAlert)
	if msg.Text != want {
		t.Errorf("text = %q\nwant %q", msg.Text, want)
	}
	if !strings.HasPrefix(msg.Text, shortcutAskOpen) || !strings.HasSuffix(msg.Text, shortcutAskRule) {
		t.Errorf("the ask must open as Slack's shortcut does and close with the rule: %q", msg.Text)
	}
	c := f.call(0)
	if strings.Join(c.resource, ".") != "spaces.messages" || c.method != "get" || c.arguments["name"] != gchatQuotedName {
		t.Errorf("read = %+v, want spaces.messages.get of the quoted message", c)
	}
	if msg.Shortcut {
		t.Error("a quoted ask is the member's own message; it must not carry Shortcut")
	}
	if msg.Conversation != "gchat:dm/spaces/D1/threads/T1" || msg.Kind != "dm" || msg.MessageID != "spaces/D1/messages/M1" || !msg.TopLevel {
		t.Errorf("binding = %+v, want the message's own, as for a plain DM", msg)
	}
}

// Words typed beside the quote come first, then the quote, then the rule;
// everything but the text is what the same message gets without the quote.
func TestGchatQuotedSpaceMessageWithWordsPutsTheWordsFirst(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/get"] = quotedResponse(humanSender("Alex Doe"), gchatQuotedAlert)
	a := newTestGchatAdapterWithRelay(t, f)

	plain := gchatQuotedMsg("spaces/S1", "SPACE", "spaces/S1/threads/T1", "spaces/S1/messages/M1", "@bot is this one ours?", "is this one ours?", "")
	plainMsg, reason := a.classify(plain)
	if reason != "" {
		t.Fatalf("plain dropped: %s", reason)
	}
	ev := gchatQuotedMsg("spaces/S1", "SPACE", "spaces/S1/threads/T1", "spaces/S1/messages/M2", "@bot is this one ours?", "is this one ours?", gchatQuotedName)
	msg, reason := quotedTurn(a, ev)
	if reason != "" {
		t.Fatalf("dropped: %s", reason)
	}
	want := fmt.Sprintf(gchatQuotedAskWithWords, "is this one ours?", "> Posted by Alex Doe:\n> "+gchatQuotedAlert)
	if msg.Text != want {
		t.Errorf("text = %q\nwant %q", msg.Text, want)
	}
	if strings.Contains(msg.Text, shortcutAskOpen) {
		t.Errorf("a member who typed words does not get the bare opening: %q", msg.Text)
	}
	msg.Text, msg.MessageID = plainMsg.Text, plainMsg.MessageID
	if msg != plainMsg {
		t.Errorf("a quoted ask = %+v differs from the plain message %+v beyond its text", msg, plainMsg)
	}
}

// The quoted message is clipped to the shared cap, the ellipsis marking the
// cut, and the clip is of the quote: the frame and the rule stay whole.
func TestGchatQuotedTextIsClippedAtTheCap(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/get"] = quotedResponse(humanSender("Alex"), strings.Repeat("a", shortcutAskQuoteMax+50))
	a := newTestGchatAdapterWithRelay(t, f)

	msg, reason := quotedTurn(a, gchatBareQuotedMsg(gchatQuotedName))
	if reason != "" {
		t.Fatalf("dropped: %s", reason)
	}
	lines := strings.Split(msg.Text, "\n")
	quoted := lines[3]
	if n := len([]rune(quoted)); n != len("> ")+shortcutAskQuoteMax || !strings.HasSuffix(quoted, slackClipEllipsis) {
		t.Errorf("quoted line is %d code points ending %q; want %d plus the prefix, ending in the ellipsis", n, quoted[len(quoted)-3:], shortcutAskQuoteMax)
	}
	if !strings.HasSuffix(msg.Text, shortcutAskRule) {
		t.Error("the rule must survive the clip")
	}
}

// A quoted app message is posted under the app's name; one with no name is
// "An app", and a member Chat gives no name to is named by the email the
// adapter learned for them, else "Someone".
func TestGchatQuotedAppMessageNamesTheApp(t *testing.T) {
	cases := []struct {
		name   string
		sender map[string]any
		learn  bool
		posted string
	}{
		{"an app by its name", map[string]any{"name": "users/9", "displayName": "Alertmanager", "type": "BOT"}, false, "Alertmanager"},
		{"an app with no name", map[string]any{"name": "users/9", "type": "BOT"}, false, shortcutAskSomeApp},
		{"a member with no name, learned", map[string]any{"name": "users/1", "type": "HUMAN"}, true, "u1@example.com"},
		{"a member with no name, unknown", map[string]any{"name": "users/77", "type": "HUMAN"}, true, namelessClicker},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := newFakeChatRelay(t)
			f.responses["spaces.messages/get"] = quotedResponse(c.sender, "disk 97% on node-3")
			a := newTestGchatAdapterWithRelay(t, f)
			msg, reason := quotedTurn(a, gchatBareQuotedMsg(gchatQuotedName))
			if reason != "" {
				t.Fatalf("dropped: %s", reason)
			}
			if want := "> Posted by " + c.posted + ":\n> disk 97% on node-3"; !strings.Contains(msg.Text, want) {
				t.Errorf("text = %q, want it to carry %q", msg.Text, want)
			}
		})
	}
}

// A quoted message's files are named, and a message with markup only reads
// its formattedText.
func TestGchatQuotedContentNamesFilesAndFallsBackToFormattedText(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/get"] = map[string]any{
		"name": gchatQuotedName, "text": "", "formattedText": "*pods pending*", "sender": humanSender("Alex"),
		"attachment": []map[string]any{{"name": gchatQuotedName + "/attachments/A1", "contentName": "describe.txt", "contentType": "text/plain"}},
	}
	a := newTestGchatAdapterWithRelay(t, f)
	msg, reason := quotedTurn(a, gchatBareQuotedMsg(gchatQuotedName))
	if reason != "" {
		t.Fatalf("dropped: %s", reason)
	}
	if want := "> Posted by Alex:\n> *pods pending*\n> [attached: describe.txt]"; !strings.Contains(msg.Text, want) {
		t.Errorf("text = %q, want it to carry %q", msg.Text, want)
	}
}

// A read the relay refuses loses the quote, not the member's words: the turn
// goes ahead with what they typed, a warning names the quoted message, and
// nothing is posted about it.
func TestGchatQuotedReadFailureFallsBackToTheTypedWords(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/get"] = fakeChatAPIError{status: 404, reason: "NOT_FOUND"}
	var logs bytes.Buffer
	a := newTestGchatAdapterWithRelay(t, f)
	a.log = slog.New(slog.NewTextHandler(&logs, nil))

	ev := gchatQuotedMsg("spaces/S1", "SPACE", "spaces/S1/threads/T1", "spaces/S1/messages/M1", "@bot is this one ours?", "is this one ours?", gchatQuotedName)
	msg, reason := quotedTurn(a, ev)
	if reason != "" {
		t.Fatalf("dropped: %s", reason)
	}
	if msg.Text != "is this one ours?" || msg.Shortcut {
		t.Errorf("turn = %+v, want the typed words alone", msg)
	}
	if !strings.Contains(logs.String(), "level=WARN") || !strings.Contains(logs.String(), gchatQuotedName) || !strings.Contains(logs.String(), "chat 404") {
		t.Errorf("log = %q, want a warning naming the quoted message and the error", logs.String())
	}
	if len(f.calls) != 1 {
		t.Errorf("relay calls = %+v, want the read alone and no post", f.calls)
	}
}

// A bare quote whose read fails is no turn, and the reason says why.
func TestGchatBareQuoteWhoseReadFailsIsDroppedWithTheReason(t *testing.T) {
	f := newFakeChatRelay(t)
	a := newTestGchatAdapterWithRelay(t, f)
	_, reason := quotedTurn(a, gchatBareQuotedMsg(gchatQuotedName))
	if !strings.Contains(reason, gchatQuotedName) || !strings.Contains(reason, "no words of its own") {
		t.Errorf("reason = %q, want it to name the quoted message and that nothing was typed", reason)
	}
}

// A read that outruns its budget is a failed read: the member's words go
// ahead without the quote.
func TestGchatQuotedReadTimesOutToTheTypedWords(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/get"] = quotedResponse(humanSender("Alex"), gchatQuotedAlert)
	a := newTestGchatAdapterWithRelay(t, f)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	ev := gchatQuotedMsg("spaces/S1", "SPACE", "spaces/S1/threads/T1", "spaces/S1/messages/M1", "@bot is this one ours?", "is this one ours?", gchatQuotedName)
	msg, reason := a.classify(ev)
	if reason != "" {
		t.Fatalf("dropped: %s", reason)
	}
	msg, reason = a.quotedAsk(ctx, ev, msg)
	if reason != "" || msg.Text != "is this one ours?" {
		t.Errorf("turn = %+v (%q), want the typed words alone", msg, reason)
	}
}

// A redelivery of a quoted message is still dropped on the pump, before any
// read.
func TestGchatQuotedRedeliveryIsDroppedBeforeTheRead(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/get"] = quotedResponse(humanSender("Alex"), gchatQuotedAlert)
	a := newTestGchatAdapterWithRelay(t, f)
	if _, reason := quotedTurn(a, gchatBareQuotedMsg(gchatQuotedName)); reason != "" {
		t.Fatalf("first delivery dropped: %s", reason)
	}
	if _, reason := a.classify(gchatBareQuotedMsg(gchatQuotedName)); !strings.Contains(reason, "duplicate delivery") {
		t.Errorf("reason = %q, want the duplicate dropped", reason)
	}
	if len(f.calls) != 1 {
		t.Errorf("relay calls = %d, want one read for the first delivery alone", len(f.calls))
	}
}

// The composed fixture: a real add-on DM payload with the quoted-message
// metadata the Chat API documents on it. Decoded, it names the quoted
// message; through Run, the turn is handed over composed, off the pump, and
// the event is acked.
func TestGchatQuotedReplyFixtureThroughRun(t *testing.T) {
	const quoted = "spaces/_c2fixtureAA/messages/AlertB0tMsg.AlertB0tMsg"
	ev := gchatFixtureEvent(t, "addon-dm-quoted-reply.json", nil)
	if ev.Message.QuotedMessageMetadata.Name != quoted {
		t.Fatalf("quotedMessageMetadata.name = %q, want %q", ev.Message.QuotedMessageMetadata.Name, quoted)
	}

	f := newFakeChatRelay(t)
	f.responses["spaces.messages/get"] = map[string]any{"name": quoted, "text": gchatQuotedAlert,
		"sender": map[string]any{"name": "users/900", "displayName": "Alertmanager", "type": "BOT"}}
	_, b64 := realGchatFixture(t, "addon-dm-quoted-reply.json")
	acked, _ := f.serveEvents([]map[string]any{{"receipt": "r1", "data": b64, "messageId": "1"}})
	a := newTestGchatAdapterWithRelay(t, f)

	var mu sync.Mutex
	var got []InboundMessage
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- a.Run(ctx, func(m InboundMessage) {
			mu.Lock()
			got = append(got, m)
			mu.Unlock()
		})
	}()
	deadline := time.After(5 * time.Second)
	for {
		mu.Lock()
		n := len(got)
		mu.Unlock()
		if n == 1 {
			break
		}
		select {
		case <-deadline:
			t.Fatal("no turn handed over after 5s")
		case <-time.After(10 * time.Millisecond):
		}
	}
	cancel()
	if err := <-done; err != nil && !errors.Is(err, context.Canceled) {
		t.Fatalf("Run returned %v", err)
	}
	f.mu.Lock()
	settled := append([]string(nil), *acked...)
	f.mu.Unlock()
	if len(settled) != 1 || settled[0] != "r1" {
		t.Errorf("acked = %v, want the event acked once", settled)
	}
	mu.Lock()
	defer mu.Unlock()
	msg := got[0]
	want := fmt.Sprintf(gchatQuotedAskWithWords, "is this one ours?", "> Posted by Alertmanager:\n> "+gchatQuotedAlert)
	if msg.Text != want {
		t.Errorf("text = %q\nwant %q", msg.Text, want)
	}
	if msg.Shortcut || msg.Conversation != "gchat:dm/spaces/_c2fixtureAA/threads/Qu0teDr3ply" ||
		msg.MessageID != "spaces/_c2fixtureAA/messages/Qu0teDr3ply.Qu0teDr3ply" || msg.AuthorID != "sender@example.com" || !msg.TopLevel {
		t.Errorf("turn = %+v", msg)
	}
	if f.call(0).method != "get" {
		t.Errorf("first relay call = %+v, want the quoted message read", f.call(0))
	}
}
