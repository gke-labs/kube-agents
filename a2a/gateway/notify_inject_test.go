package gateway

import (
	"context"
	"encoding/json"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	testInjectConversation = injectKeyPrefix + "eval-case-1"
	testInjectContext      = "ctx-inject-1"
)

func newTestInjectNotifier(t *testing.T) (*Notifier, *InjectAdapter, *fakeConversations) {
	t.Helper()
	door, err := NewInjectAdapter("127.0.0.1:0", injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	conv := &fakeConversations{contexts: map[string]string{testInjectConversation: testInjectContext}}
	n := NewInjectNotifier(door, nil)
	n.SetConversations(conv)
	return n, door, conv
}

// The inject route is its own subject, on the inject door's conversations:
// the console route's code path, not the console's subject or prefix.
func TestTheInjectNotifierServesItsOwnSubjectAndPrefix(t *testing.T) {
	n, _, _ := newTestInjectNotifier(t)
	if n.subject != "chat.notify.inject" || lib.NotifySubjectInject != "chat.notify.inject" {
		t.Fatalf("subject = %q, want chat.notify.inject", n.subject)
	}
	if n.convPrefix != injectKeyPrefix {
		t.Fatalf("conversation prefix = %q, want %q", n.convPrefix, injectKeyPrefix)
	}
	if n.frames == nil {
		t.Fatal("the inject notifier does not take the console route's frame path")
	}
}

// A card's report into an inject conversation lands on the door's
// transcript as one whole post carrying the task it reports on and its
// report mark, which is what the eval harness reads back through the read
// route; and the report is kept by that task's root, as on the console.
func TestTheInjectNotifierPostsTheReportOnTheTranscriptAndKeepsIt(t *testing.T) {
	n, door, conv := newTestInjectNotifier(t)
	report := strings.Repeat("the fleet is green. ", 40)
	got := serveJSON(t, n, lib.NotifyRequest{Text: report, Conversation: testInjectConversation,
		ContextID: testInjectContext, ReplyTo: "task-7", Report: true})
	if got.Error != "" || got.MessageID == "" || got.ThreadID != testInjectConversation {
		t.Fatalf("reply = %+v, want a post into the conversation", got)
	}
	entries, _, _ := door.snapshot(testInjectConversation, 0, "")
	if len(entries) != 1 {
		t.Fatalf("entries = %+v, want exactly one", entries)
	}
	e := entries[0]
	if e.Kind != InjectEntryPost || e.Text != report || e.MessageID != got.MessageID || e.ReplyTo != "task-7" || !e.Report {
		t.Fatalf("entry = %+v, want one whole post naming task-7, marked a report", e)
	}
	if want := []string{testInjectConversation + " task-7 " + report}; !reflect.DeepEqual(conv.kept, want) {
		t.Fatalf("kept = %q, want %q", conv.kept, want)
	}
	if len(conv.posts) != 0 {
		t.Fatalf("the report also went out as chat posts: %+v", conv.posts)
	}
}

// A progress note is on the transcript too, but only the report is kept;
// a keyed post edits the entry its key posted.
func TestTheInjectNotifierKeepsReportsOnlyAndEditsByKey(t *testing.T) {
	n, door, conv := newTestInjectNotifier(t)
	serveJSON(t, n, lib.NotifyRequest{Text: "⏳ reading pod state", Conversation: testInjectConversation,
		ContextID: testInjectContext, ReplyTo: "task-7"})
	first := serveJSON(t, n, lib.NotifyRequest{Text: "1 of 3", Conversation: testInjectConversation,
		ContextID: testInjectContext, ReplyTo: "task-7", Update: "plan"})
	second := serveJSON(t, n, lib.NotifyRequest{Text: "3 of 3", Conversation: testInjectConversation,
		ContextID: testInjectContext, ReplyTo: "task-7", Update: "plan"})
	if first.MessageID == "" || second.MessageID != first.MessageID {
		t.Fatalf("replies = %+v then %+v, want the same message", first, second)
	}
	entries, _, _ := door.snapshot(testInjectConversation, 0, "")
	var kinds []string
	for _, e := range entries {
		kinds = append(kinds, e.Kind+"|"+e.Text+"|"+e.ReplyTo)
	}
	want := []string{"post|⏳ reading pod state|task-7", "post|1 of 3|task-7", "edit|3 of 3|task-7"}
	if !reflect.DeepEqual(kinds, want) {
		t.Fatalf("entries (kind|text|replyTo) = %q, want %q", kinds, want)
	}
	if entries[2].MessageID != first.MessageID {
		t.Fatalf("the edit names %q, want the keyed post %q", entries[2].MessageID, first.MessageID)
	}
	if len(conv.kept) != 0 {
		t.Fatalf("kept = %q, want nothing: no post was a report", conv.kept)
	}
}

// The inject route serves the inject door's conversations only: no home
// post, no console or chat conversation, and no conversation whose record
// does not carry the request's context.
func TestTheInjectNotifierRefusesWhatIsNotItsConversation(t *testing.T) {
	for _, tc := range []struct {
		name string
		req  lib.NotifyRequest
		want string
	}{
		{"a home post", lib.NotifyRequest{Text: "alert"}, notifyNoHome},
		{"a console conversation", lib.NotifyRequest{Text: "x", Conversation: testConsoleConversation, ContextID: testConsoleContext},
			"not on this route's backend"},
		{"a chat conversation", lib.NotifyRequest{Text: "x", Conversation: testConversation, ContextID: testContext},
			"not on this route's backend"},
		{"another context", lib.NotifyRequest{Text: "x", Conversation: testInjectConversation, ContextID: "ctx-other"},
			notifyConversationRefused},
	} {
		t.Run(tc.name, func(t *testing.T) {
			n, door, _ := newTestInjectNotifier(t)
			got := serveJSON(t, n, tc.req)
			if !strings.Contains(got.Error, tc.want) {
				t.Fatalf("reply = %+v, want a refusal containing %q", got, tc.want)
			}
			if entries, _, _ := door.snapshot(testInjectConversation, 0, ""); len(entries) != 0 {
				t.Fatalf("entries = %+v, want none", entries)
			}
		})
	}
}

// The console route does not serve an inject conversation either: each
// door's reports go out on its own subject.
func TestTheConsoleNotifierRefusesAnInjectConversation(t *testing.T) {
	n, frames, _ := newTestConsoleNotifier(t)
	got := serveJSON(t, n, lib.NotifyRequest{Text: "x", Conversation: testInjectConversation, ContextID: testInjectContext})
	if !strings.Contains(got.Error, "not on this route's backend") || len(frames.frames) != 0 {
		t.Fatalf("reply = %+v, frames = %+v, want a refusal and no frame", got, frames.frames)
	}
}

// The whole gateway half over the bus, through the real gateway's
// conversations: a card's report published on chat.notify.inject, as
// `a2a notify --platform inject --conversation ... --reply-to ... --report`
// sends it, is answered on the notify reply namespace, lands on the inject
// door's transcript as a report entry naming the turn, and is kept by the
// gateway under that turn's root for the reply under it.
func TestAnInjectReportOverTheBusLandsOnTheTranscriptAndIsKeptByRoot(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	const conv = injectKeyPrefix + "eval-chain"
	if err := r.g.reg.Create(ctx, &SessionRecord{Key: conv, ContextID: "ctx-e2e", Addressee: "platform", Kind: "dm", LastActivity: time.Now()}); err != nil {
		t.Fatalf("session record: %v", err)
	}
	door, err := NewInjectAdapter("127.0.0.1:0", injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	n := NewInjectNotifier(door, nil)
	n.SetConversations(r.g.Conversations(door))
	sub, err := n.Start(r.client)
	if err != nil {
		t.Fatalf("inject route: %v", err)
	}
	t.Cleanup(sub.Stop)

	nc, err := connectTest(r.url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	reply := lib.NotifyReplyPrefix + "e2e-1"
	in, err := nc.SubscribeSync(reply)
	if err != nil {
		t.Fatal(err)
	}
	const report = "card report: np-quartz-7 is at 92%"
	body, _ := json.Marshal(lib.NotifyRequest{Text: report, Conversation: conv, ContextID: "ctx-e2e", ReplyTo: "task-7", Report: true})
	if err := nc.PublishRequest(lib.NotifySubjectInject, reply, body); err != nil {
		t.Fatal(err)
	}
	msg, err := in.NextMsg(10 * time.Second)
	if err != nil {
		t.Fatalf("no answer on %s: %v", reply, err)
	}
	var answer lib.NotifyReply
	if err := json.Unmarshal(msg.Data, &answer); err != nil || answer.Error != "" || answer.MessageID == "" {
		t.Fatalf("answer = %s (%v), want a post", msg.Data, err)
	}
	entries, _, _ := door.snapshot(conv, 0, "")
	if len(entries) != 1 || entries[0].Kind != InjectEntryPost || entries[0].Text != report ||
		entries[0].ReplyTo != "task-7" || !entries[0].Report || entries[0].MessageID != answer.MessageID {
		t.Fatalf("transcript = %+v, want one report entry naming task-7", entries)
	}
	if got, ok := r.g.reports.byRoot(conv, "task-7"); !ok || got != report {
		t.Fatalf("kept report under task-7 = %q (%v), want the report", got, ok)
	}
}
