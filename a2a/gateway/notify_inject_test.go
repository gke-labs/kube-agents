package gateway

import (
	"context"
	"encoding/json"
	"net/http"
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

// Every entry the notify route relays onto an inject conversation is marked,
// a card's progress line as well as its settled report, and none counts as
// the turn's own entry: a reader sets them apart, and a turn that posted
// nothing of its own is not read as answered.
func TestEveryRelayedEntryIsMarkedAndNotATurnEntry(t *testing.T) {
	door, err := NewInjectAdapter("127.0.0.1:0", injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	const conv = "inject:relayed-1"
	door.mu.Lock()
	before := countsOf(door.conversationLocked(conv))
	door.mu.Unlock()
	if _, err := door.PostNotifyFrame(conv, "", "card started", "task-7", false, false); err != nil {
		t.Fatal(err)
	}
	if _, err := door.PostNotifyFrame(conv, "", "card report", "task-7", true, false); err != nil {
		t.Fatal(err)
	}
	door.mu.Lock()
	c := door.conversationLocked(conv)
	after := countsOf(c)
	entries := append([]InjectEntry(nil), c.entries...)
	door.mu.Unlock()
	if after.entries != before.entries {
		t.Fatalf("relayed entries counted as the turn's: %d -> %d", before.entries, after.entries)
	}
	if len(entries) != 2 {
		t.Fatalf("%d entries", len(entries))
	}
	for _, e := range entries {
		if !e.Report {
			t.Errorf("relayed entry %q not marked", e.Text)
		}
	}
	if entries[0].Kept || !entries[1].Kept {
		t.Errorf("kept marks %v %v, want only the settled report kept", entries[0].Kept, entries[1].Kept)
	}
	// A turn's own post still counts.
	if _, err := door.PostMessage(conv, OutMessage{Text: "the reply"}); err != nil {
		t.Fatal(err)
	}
	door.mu.Lock()
	own := countsOf(door.conversationLocked(conv))
	door.mu.Unlock()
	if own.entries != after.entries+1 {
		t.Fatalf("the turn's own post not counted: %d -> %d", after.entries, own.entries)
	}
}

// injectTurnParts POSTs one inject turn and returns the parts of the
// submission it started on platform.
func injectTurnParts(t *testing.T, r *injectRig, req injectRequest) []lib.Part {
	t.Helper()
	status, out := r.postInject(t, req)
	if status != http.StatusOK || !out.Accepted || out.TaskID == "" {
		t.Fatalf("POST = %d %+v, want an accepted turn", status, out)
	}
	origin := r.awaitTask(t, "platform")
	if origin.TaskID != out.TaskID {
		t.Fatalf("submission task %q, POST answered %q", origin.TaskID, out.TaskID)
	}
	return messageParts(t, origin)
}

// An inject turn whose replyTo names the root a report was kept under, on
// its own conversation, carries that report and the replyTo part, as a
// console reply does: an inject key is not thread-keyed, so replyTo is the
// only way a turn on this door is under a report.
func TestAnInjectReplyNamingTheRootCarriesTheKeptReport(t *testing.T) {
	t.Parallel()
	r := startInjectRig(t)
	const report = "card report: np-quartz-7 is at 92%"
	r.g.reports.put(injectKeyPrefix+"eval-reply-1", "task-root", report)
	parts := injectTurnParts(t, r, injectRequest{Conversation: "eval-reply-1", Author: injectTestAuthor,
		Text: "and the other pool?", ReplyTo: "task-root"})
	got, found, err := lib.PushedReportOf(parts)
	if err != nil || !found || got != (lib.PushedReport{TaskID: "task-root", Text: report}) {
		t.Fatalf("pushedReport part = %+v found=%v err=%v, want the kept report", got, found, err)
	}
	if rt, found, _ := lib.ReplyToOf(parts); !found || rt.TaskID != "task-root" {
		t.Fatalf("replyTo part = %+v found=%v, want task-root", rt, found)
	}
}

// Without replyTo, an inject turn on a conversation holding a kept report
// carries neither part.
func TestAnInjectTurnWithoutReplyToCarriesNoKeptReport(t *testing.T) {
	t.Parallel()
	r := startInjectRig(t)
	r.g.reports.put(injectKeyPrefix+"eval-reply-2", "task-root", "card report")
	parts := injectTurnParts(t, r, injectRequest{Conversation: "eval-reply-2", Author: injectTestAuthor, Text: "and the other pool?"})
	if _, found, _ := lib.PushedReportOf(parts); found {
		t.Fatal("a turn naming no task carried the kept report")
	}
	if _, found, _ := lib.ReplyToOf(parts); found {
		t.Fatal("a turn naming no task carried a replyTo part")
	}
}

// A replyTo that is not a task id's shape is refused at the door with a
// 400, as a malformed profile is, and nothing is submitted.
func TestTheInjectDoorRefusesAMalformedReplyTo(t *testing.T) {
	t.Parallel()
	r := startInjectRig(t)
	r.g.reports.put(injectKeyPrefix+"eval-reply-3", "task-root", "card report")
	for _, bad := range []string{"task.>", "Task-Root", "task root", strings.Repeat("t", 64)} {
		status, _ := r.postInject(t, injectRequest{Conversation: "eval-reply-3", Author: injectTestAuthor, Text: "x", ReplyTo: bad})
		if status != http.StatusBadRequest {
			t.Errorf("replyTo %q: status %d, want 400", bad, status)
		}
	}
	if envs := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+">"); len(envs) != 0 {
		t.Fatalf("%d submission(s) after refused POSTs", len(envs))
	}
}

// A turn on conversation A naming a root whose report was kept under
// conversation B carries nothing of B's: kept reports are looked up under
// the posting turn's own key (relayedReport passes rec.Key to byRoot), so
// holding the door's token is not a way to read another conversation's
// report by guessing its root.
func TestAnInjectReplyCannotReachAnotherConversationsKeptReport(t *testing.T) {
	t.Parallel()
	r := startInjectRig(t)
	const secret = "conversation B's report: the token is in vault-7"
	r.g.reports.put(injectKeyPrefix+"eval-conv-b", "task-b", secret)
	parts := injectTurnParts(t, r, injectRequest{Conversation: "eval-conv-a", Author: injectTestAuthor,
		Text: "what did the card say?", ReplyTo: "task-b"})
	if got, found, _ := lib.PushedReportOf(parts); found {
		t.Fatalf("A's turn carried a report kept under B: %+v", got)
	}
	for _, p := range parts {
		if strings.Contains(p.Text, secret) || strings.Contains(string(p.Data), secret) {
			t.Fatalf("A's submission holds B's report text in part %+v", p)
		}
	}
	// B's report is still kept for B.
	if got, ok := r.g.reports.byRoot(injectKeyPrefix+"eval-conv-b", "task-b"); !ok || got != secret {
		t.Fatalf("B's kept report = %q (%v)", got, ok)
	}
}
