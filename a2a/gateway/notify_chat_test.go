package gateway

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// notifyChat encodes m as a request's chat field.
func notifyChat(t *testing.T, m chatmsg.Message) json.RawMessage {
	t.Helper()
	data, err := chatmsg.Encode(m)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func newChatNotifier(t *testing.T, renders bool) (*Notifier, *fakeConversations) {
	t.Helper()
	conv := &fakeConversations{contexts: map[string]string{testConversation: testContext}, rendersChat: renders}
	n := newTestNotifier(t, &fakeNotifyPoster{})
	n.SetConversations(conv)
	return n, conv
}

// A conversation request's chat message is laid out by a backend that
// renders it, as one message with the text beside it.
func TestNotifyConversationChatIsLaidOut(t *testing.T) {
	n, conv := newChatNotifier(t, true)
	got := serveJSON(t, n, lib.NotifyRequest{Text: testReportText, Chat: notifyChat(t, testReport),
		Conversation: testConversation, ContextID: testContext})
	if got.Error != "" || got.ThreadID != testConversation {
		t.Fatalf("reply = %+v", got)
	}
	if len(conv.chats) != 1 || conv.chats[0] == nil || conv.chats[0].Headline != testReport.Headline {
		t.Fatalf("chats = %+v, want the report laid out", conv.chats)
	}
	if len(conv.posts) != 1 || conv.posts[0].text != testReportText {
		t.Fatalf("posts = %+v, want one message, the text beside the layout", conv.posts)
	}
}

// A conversation request's mark is drawn after its post, and a bad mark is
// dropped alone with the layout still posted.
func TestNotifyConversationMarkIsDrawnAfterThePost(t *testing.T) {
	n, conv := newChatNotifier(t, true)
	settled := testReport
	settled.Mark = &chatmsg.Mark{Settle: chatmsg.SettleDone}
	got := serveJSON(t, n, lib.NotifyRequest{Text: testReportText, Chat: notifyChat(t, settled),
		Conversation: testConversation, ContextID: testContext, Update: "plan"})
	if got.Error != "" {
		t.Fatalf("reply = %+v", got)
	}
	if fmt.Sprint(conv.marks) != fmt.Sprint([]string{testConversation + " /done"}) || conv.postsAtMark[0] != 1 {
		t.Fatalf("marks = %v at posts %v, want the settle drawn after the one post", conv.marks, conv.postsAtMark)
	}
	bad := testReport
	bad.Mark = &chatmsg.Mark{Ask: "Not A Token"}
	serveJSON(t, n, lib.NotifyRequest{Text: testReportText, Chat: notifyChat(t, bad),
		Conversation: testConversation, ContextID: testContext})
	if len(conv.marks) != 1 || len(conv.chats) != 2 || conv.chats[1] == nil {
		t.Fatalf("marks %v, chats %d: a bad mark is dropped alone", conv.marks, len(conv.chats))
	}
}

// On a backend that renders none, or for a chat message that breaks a rule,
// the request posts its text, chunked, as it always has.
func TestNotifyConversationChatFallsBackToText(t *testing.T) {
	bad := testReport
	bad.Tone = "loud"
	for name, tc := range map[string]struct {
		renders bool
		chat    chatmsg.Message
	}{
		"text-only backend": {false, testReport},
		"invalid message":   {true, bad},
	} {
		t.Run(name, func(t *testing.T) {
			n, conv := newChatNotifier(t, tc.renders)
			long := strings.Repeat("x", discordChunk+10)
			got := serveJSON(t, n, lib.NotifyRequest{Text: long, Chat: notifyChat(t, tc.chat),
				Conversation: testConversation, ContextID: testContext})
			if got.Error != "" {
				t.Fatalf("reply = %+v", got)
			}
			if len(conv.chats) != 0 {
				t.Fatalf("chats = %+v, want none laid out", conv.chats)
			}
			if len(conv.posts) != 2 {
				t.Fatalf("posts = %d, want the text in two chunks", len(conv.posts))
			}
		})
	}
}

// An update key posts once and edits that message after: the plan stays one
// message per conversation, laid out each time.
func TestNotifyUpdateKeyEditsTheMessageItPosted(t *testing.T) {
	n, conv := newChatNotifier(t, true)
	first := serveJSON(t, n, lib.NotifyRequest{Text: "plan", Chat: notifyChat(t, testPlan), Update: "plan",
		Conversation: testConversation, ContextID: testContext})
	second := serveJSON(t, n, lib.NotifyRequest{Text: "plan, later", Chat: notifyChat(t, testPlan), Update: "plan",
		Conversation: testConversation, ContextID: testContext})
	if first.Error != "" || second.Error != "" || second.MessageID != first.MessageID {
		t.Fatalf("replies = %+v, %+v; want the second to name the first's message", first, second)
	}
	if len(conv.posts) != 1 || len(conv.edits) != 1 || conv.edits[0].thread != first.MessageID || conv.edits[0].text != "plan, later" {
		t.Fatalf("posts = %+v, edits = %+v; want one post and one edit of it", conv.posts, conv.edits)
	}
	if len(conv.chats) != 2 || conv.chats[1] == nil || conv.chats[1].Plan == nil {
		t.Fatalf("chats = %+v, want the plan on both", conv.chats)
	}
}

// A keyed request on a text-only backend edits the message as text, so a
// card's question settles where it was asked on Google Chat too.
func TestNotifyUpdateKeyEditsAsTextOnATextOnlyBackend(t *testing.T) {
	n, conv := newChatNotifier(t, false)
	first := serveJSON(t, n, lib.NotifyRequest{Text: "Which cluster?", Chat: notifyChat(t, testReport), Update: "q-1",
		Conversation: testConversation, ContextID: testContext})
	serveJSON(t, n, lib.NotifyRequest{Text: "Answered: prod-eu", Update: "q-1", Conversation: testConversation, ContextID: testContext})
	if len(conv.posts) != 1 || len(conv.edits) != 1 || conv.edits[0].thread != first.MessageID || conv.edits[0].text != "Answered: prod-eu" {
		t.Fatalf("posts = %+v, edits = %+v", conv.posts, conv.edits)
	}
}

// An edit that fails posts a new message, and the key moves to it, so the
// next request edits the message that is there rather than the lost one.
func TestNotifyUpdateKeyPostsAnewWhenTheEditFails(t *testing.T) {
	n, conv := newChatNotifier(t, false)
	first := serveJSON(t, n, lib.NotifyRequest{Text: "a", Update: "plan", Conversation: testConversation, ContextID: testContext})
	conv.failEdits = true
	second := serveJSON(t, n, lib.NotifyRequest{Text: "b", Update: "plan", Conversation: testConversation, ContextID: testContext})
	if second.Error != "" || second.MessageID == first.MessageID || len(conv.posts) != 2 {
		t.Fatalf("replies %+v, %+v, posts %d; want a second post", first, second, len(conv.posts))
	}
	conv.failEdits = false
	serveJSON(t, n, lib.NotifyRequest{Text: "c", Update: "plan", Conversation: testConversation, ContextID: testContext})
	if len(conv.edits) != 1 || conv.edits[0].thread != second.MessageID {
		t.Fatalf("edits = %+v, want the key on the second post", conv.edits)
	}
}

// A conversation keeps at most notifyUpdateKeysMax keys; the oldest is
// forgotten, and a request with it posts anew.
func TestNotifyUpdateKeysAreBounded(t *testing.T) {
	n, conv := newChatNotifier(t, false)
	for i := 0; i <= notifyUpdateKeysMax; i++ {
		serveJSON(t, n, lib.NotifyRequest{Text: "x", Update: fmt.Sprintf("k%d", i), Conversation: testConversation, ContextID: testContext})
	}
	serveJSON(t, n, lib.NotifyRequest{Text: "again", Update: "k0", Conversation: testConversation, ContextID: testContext})
	if len(conv.edits) != 0 || len(conv.posts) != notifyUpdateKeysMax+2 {
		t.Fatalf("posts = %d, edits = %d; want the evicted key to post anew", len(conv.posts), len(conv.edits))
	}
	serveJSON(t, n, lib.NotifyRequest{Text: "edit", Update: fmt.Sprintf("k%d", notifyUpdateKeysMax), Conversation: testConversation, ContextID: testContext})
	if len(conv.edits) != 1 {
		t.Fatalf("edits = %d, want the newest key still edited", len(conv.edits))
	}
}

// An update key edits only what it posted in the same conversation.
func TestNotifyUpdateKeyIsPerConversation(t *testing.T) {
	const other = "gchat:spaces/OTHER/threads/T8"
	n, conv := newChatNotifier(t, false)
	conv.contexts[other] = testContext
	serveJSON(t, n, lib.NotifyRequest{Text: "a", Update: "plan", Conversation: testConversation, ContextID: testContext})
	serveJSON(t, n, lib.NotifyRequest{Text: "b", Update: "plan", Conversation: other, ContextID: testContext})
	if len(conv.posts) != 2 || len(conv.edits) != 0 {
		t.Fatalf("posts = %d, edits = %d; want a post in each conversation", len(conv.posts), len(conv.edits))
	}
}

func TestNotifyRefusesAnOverlongUpdateKey(t *testing.T) {
	n, conv := newChatNotifier(t, false)
	got := serveJSON(t, n, lib.NotifyRequest{Text: "x", Update: strings.Repeat("k", notifyUpdateKeyMax+1),
		Conversation: testConversation, ContextID: testContext})
	if got.Error == "" || len(conv.posts) != 0 {
		t.Fatalf("reply = %+v, posts = %d; want a refusal", got, len(conv.posts))
	}
}
