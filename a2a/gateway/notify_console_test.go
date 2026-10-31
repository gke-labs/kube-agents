package gateway

import (
	"context"
	"strings"
	"sync"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// fakeFrames records what the console notifier posts (consoleFramePoster).
type fakeFrames struct {
	mu     sync.Mutex
	frames []fakeNotifyFrame
	next   int
}

type fakeNotifyFrame struct {
	conversation, messageID, text, replyTo string
	edit                                   bool
}

func (f *fakeFrames) PostNotifyFrame(conversation, messageID, text, replyTo string, edit bool) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if messageID == "" {
		f.next++
		messageID = "n-test-" + string(rune('0'+f.next))
	}
	f.frames = append(f.frames, fakeNotifyFrame{conversation, messageID, text, replyTo, edit})
	return messageID, nil
}

const (
	testConsoleConversation = "console:tab-1"
	testConsoleContext      = "ctx-console-1"
)

func newTestConsoleNotifier(t *testing.T) (*Notifier, *fakeFrames, *fakeConversations) {
	t.Helper()
	frames := &fakeFrames{}
	conv := &fakeConversations{contexts: map[string]string{testConsoleConversation: testConsoleContext}}
	n := NewConsoleNotifier(frames, nil)
	n.SetConversations(conv)
	return n, frames, conv
}

// A card's report into a console conversation is one notify frame, whole
// (no chunking at a chat platform's message size), naming the turn the card
// was filed from.
func TestTheConsoleNotifierPostsOneFrameNamingTheTurn(t *testing.T) {
	n, frames, conv := newTestConsoleNotifier(t)
	report := strings.Repeat("the fleet is green. ", 400)
	got := serveJSON(t, n, lib.NotifyRequest{Text: report, Conversation: testConsoleConversation,
		ContextID: testConsoleContext, ReplyTo: "task-7"})
	if got.Error != "" || got.MessageID == "" || got.ThreadID != testConsoleConversation {
		t.Fatalf("reply = %+v, want a post into the conversation", got)
	}
	want := fakeNotifyFrame{testConsoleConversation, got.MessageID, report, "task-7", false}
	if len(frames.frames) != 1 || frames.frames[0] != want {
		t.Fatalf("frames = %+v, want exactly one whole frame naming task-7", frames.frames)
	}
	if len(conv.posts) != 0 {
		t.Fatalf("the report also went out as chat posts: %+v", conv.posts)
	}
}

// A keyed report (Update) edits the frame its key posted, as it edits a chat
// message elsewhere.
func TestAKeyedConsoleReportEditsItsFrame(t *testing.T) {
	n, frames, _ := newTestConsoleNotifier(t)
	first := serveJSON(t, n, lib.NotifyRequest{Text: "1 of 3", Conversation: testConsoleConversation,
		ContextID: testConsoleContext, Update: "plan"})
	second := serveJSON(t, n, lib.NotifyRequest{Text: "3 of 3", Conversation: testConsoleConversation,
		ContextID: testConsoleContext, Update: "plan"})
	if first.MessageID == "" || second.MessageID != first.MessageID {
		t.Fatalf("replies = %+v then %+v, want the same message", first, second)
	}
	if len(frames.frames) != 2 || frames.frames[0].edit || !frames.frames[1].edit || frames.frames[1].text != "3 of 3" {
		t.Fatalf("frames = %+v, want a post then an edit of it", frames.frames)
	}
}

// A reply_to that is no task id is dropped, and the report still posts:
// the frame's replyTo is an id a client acts on, and a report is worth more
// than the join.
func TestAMalformedReplyToIsDroppedAndTheReportStillPosts(t *testing.T) {
	n, frames, _ := newTestConsoleNotifier(t)
	got := serveJSON(t, n, lib.NotifyRequest{Text: "report", Conversation: testConsoleConversation,
		ContextID: testConsoleContext, ReplyTo: "task 7; rm -rf"})
	if got.Error != "" || len(frames.frames) != 1 || frames.frames[0].replyTo != "" {
		t.Fatalf("reply = %+v, frames = %+v, want one frame with no replyTo", got, frames.frames)
	}
}

// The console route serves its own conversations only: no home post, no
// other backend's conversation, and no conversation whose record does not
// carry the request's context.
func TestTheConsoleNotifierRefusesWhatIsNotItsConversation(t *testing.T) {
	for _, tc := range []struct {
		name string
		req  lib.NotifyRequest
		want string
	}{
		{"a home post", lib.NotifyRequest{Text: "alert"}, notifyNoHome},
		{"a chat conversation", lib.NotifyRequest{Text: "x", Conversation: testConversation, ContextID: testContext},
			"not on this route's backend"},
		{"another context", lib.NotifyRequest{Text: "x", Conversation: testConsoleConversation, ContextID: "ctx-other"},
			notifyConversationRefused},
	} {
		t.Run(tc.name, func(t *testing.T) {
			n, frames, _ := newTestConsoleNotifier(t)
			got := serveJSON(t, n, tc.req)
			if !strings.Contains(got.Error, tc.want) || len(frames.frames) != 0 {
				t.Fatalf("reply = %+v, frames = %+v, want a refusal containing %q and no frame", got, frames.frames, tc.want)
			}
		})
	}
}

// rootingConversations answers ReplyToRoot with a fixed root.
type rootingConversations struct {
	*fakeConversations
	root string
}

func (r rootingConversations) ReplyToRoot(context.Context, string, string) string { return r.root }

// The console notifier asks the conversations for the root a client knows
// the reply_to task by, and the frame carries that.
func TestTheConsoleNotifierRepliesToTheChainsRoot(t *testing.T) {
	frames := &fakeFrames{}
	n := NewConsoleNotifier(frames, nil)
	n.SetConversations(rootingConversations{
		fakeConversations: &fakeConversations{contexts: map[string]string{testConsoleConversation: testConsoleContext}},
		root:              "task-root",
	})
	got := serveJSON(t, n, lib.NotifyRequest{Text: "report", Conversation: testConsoleConversation,
		ContextID: testConsoleContext, ReplyTo: "task-child"})
	if got.Error != "" || len(frames.frames) != 1 || frames.frames[0].replyTo != "task-root" {
		t.Fatalf("reply = %+v, frames = %+v, want replyTo task-root", got, frames.frames)
	}
}
