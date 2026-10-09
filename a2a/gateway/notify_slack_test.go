package gateway

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"

	"github.com/slack-go/slack"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The chat.notify route on Slack: the same Notifier as Chat's, built with
// Slack's home check and thread rule, and SlackAdapter.PostNotify as the
// poster.

const testSlackHome = "C0HOME"

func newTestSlackNotifier(t *testing.T, p notifyPoster) *Notifier {
	t.Helper()
	n, err := NewSlackNotifier(p, testSlackHome, nil)
	if err != nil {
		t.Fatalf("NewSlackNotifier: %v", err)
	}
	return n
}

func TestSlackNotifyRefusesAHomeThatIsNotAChannel(t *testing.T) {
	for _, home := range []string{"D0DM", "general", "#ops", "c0lower", "C", "spaces/AAA", "C0HOME/1.2"} {
		if _, err := NewSlackNotifier(&fakeNotifyPoster{}, home, nil); err == nil {
			t.Errorf("home %q was accepted", home)
		}
	}
	for _, home := range []string{"C0HOME", "G0PRIVATE", ""} {
		if _, err := NewSlackNotifier(&fakeNotifyPoster{}, home, nil); err != nil {
			t.Errorf("home %q was refused: %v", home, err)
		}
	}
}

func TestSlackNotifyUsesItsOwnSubject(t *testing.T) {
	n := newTestSlackNotifier(t, &fakeNotifyPoster{})
	if n.subject != lib.NotifySubjectSlack {
		t.Errorf("subject = %q, want %q", n.subject, lib.NotifySubjectSlack)
	}
	if lib.NotifySubjects[lib.NotifyPlatformSlack] != lib.NotifySubjectSlack {
		t.Errorf("the CLI cannot find Slack's subject: %v", lib.NotifySubjects)
	}
}

// TestSlackNotifyPostsOnlyIntoTheHomeChannel: a new thread and a reply both
// land in the home channel, whatever the request names, because a Slack
// thread ts names no channel. The answer carries the thread root's ts.
func TestSlackNotifyPostsOnlyIntoTheHomeChannel(t *testing.T) {
	p := &fakeNotifyPoster{landsIn: "1700000000.000100"}
	n := newTestSlackNotifier(t, p)
	first := serveJSON(t, n, lib.NotifyRequest{Text: "drift on prod-eu"})
	if first.Error != "" || first.ThreadID != "1700000000.000100" {
		t.Fatalf("new thread: %+v", first)
	}
	reply := serveJSON(t, n, lib.NotifyRequest{Text: "and on prod-us", Thread: first.ThreadID})
	if reply.Error != "" || reply.ThreadID != first.ThreadID {
		t.Fatalf("reply: %+v", reply)
	}
	for _, post := range p.all() {
		if post.space != testSlackHome {
			t.Errorf("posted into %q, want the home channel %q", post.space, testSlackHome)
		}
	}
}

func TestSlackNotifyRefusesAThreadThatIsNotATS(t *testing.T) {
	p := &fakeNotifyPoster{}
	n := newTestSlackNotifier(t, p)
	for _, thread := range []string{"C0OTHER/1.2", "spaces/AAA/threads/B", "1700000000", ".5", "1.", "abc.def", "1.2.3", " 1.2"} {
		got := serveJSON(t, n, lib.NotifyRequest{Text: "x", Thread: thread})
		if got.Error == "" {
			t.Errorf("thread %q was accepted", thread)
		}
	}
	if len(p.all()) != 0 {
		t.Errorf("a refused request posted: %v", p.all())
	}
}

// TestSlackAdapterPostNotify: the adapter posts into the channel it is
// given, threads on a ts when given one, and reports the new message's ts as
// the thread when it started one.
func TestSlackAdapterPostNotify(t *testing.T) {
	api := &fakeSlackAPI{}
	a := newTestSlackAdapter(api)
	msg, thread, err := a.PostNotify(testSlackHome, "", "**alert**: drift")
	if err != nil || msg != "999.001" || thread != "999.001" {
		t.Fatalf("top-level: msg=%q thread=%q err=%v, want the new ts as both", msg, thread, err)
	}
	msg, thread, err = a.PostNotify(testSlackHome, "1700000000.000100", "follow-up")
	if err != nil || thread != "1700000000.000100" {
		t.Fatalf("reply: msg=%q thread=%q err=%v", msg, thread, err)
	}
	if len(api.posted) != 2 || api.posted[0].channel != testSlackHome || api.posted[0].thread != "" ||
		api.posted[1].thread != "1700000000.000100" {
		t.Fatalf("posted = %+v", api.posted)
	}
	if !strings.Contains(api.posted[0].text, "*alert*") {
		t.Errorf("text %q was not translated to mrkdwn", api.posted[0].text)
	}
	if _, _, err := a.PostNotify("D0DM", "", "x"); err == nil {
		t.Error("PostNotify accepted a DM channel")
	}
}

// testBlocks is a minimal Block Kit array: one section.
const testBlocks = `[{"type":"section","text":{"type":"mrkdwn","text":"*3 findings*"}}]`

// stubSlackPostMessage is a Slack Web API stub that records each
// chat.postMessage's form and answers as Slack does: ok with a ts, or the
// error it was told to give.
type stubSlackPostMessage struct {
	mu    sync.Mutex
	forms []url.Values
	fail  string
}

func (s *stubSlackPostMessage) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	_ = r.ParseForm()
	s.mu.Lock()
	s.forms = append(s.forms, r.Form)
	fail := s.fail
	s.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	if fail != "" {
		_, _ = w.Write([]byte(`{"ok":false,"error":"` + fail + `"}`))
		return
	}
	_, _ = w.Write([]byte(`{"ok":true,"channel":"C0HOME","ts":"1700000000.000200"}`))
}

func startSlackStubAdapter(t *testing.T) (*SlackAdapter, *stubSlackPostMessage) {
	t.Helper()
	stub := &stubSlackPostMessage{}
	srv := httptest.NewServer(stub)
	t.Cleanup(srv.Close)
	return newSlackAdapter("xoxb-stub", "xapp-stub", slog.Default(), slack.OptionAPIURL(srv.URL+"/")), stub
}

// TestSlackNotifyPostsBlocksAsOneMessage: a request with blocks posts once,
// on the wire as Block Kit, with the text as its fallback, into home.
func TestSlackNotifyPostsBlocksAsOneMessage(t *testing.T) {
	adapter, stub := startSlackStubAdapter(t)
	n := newTestSlackNotifier(t, adapter)
	got := serveJSON(t, n, lib.NotifyRequest{Text: strings.Repeat("fallback ", 1000), Blocks: json.RawMessage(testBlocks)})
	if got.Error != "" || got.MessageID != "1700000000.000200" || got.ThreadID != "1700000000.000200" {
		t.Fatalf("answer = %+v", got)
	}
	if len(stub.forms) != 1 || stub.forms[0].Get("channel") != testSlackHome {
		t.Fatalf("posts = %v, want one message in the home channel", stub.forms)
	}
	if blocks := stub.forms[0].Get("blocks"); !strings.Contains(blocks, "3 findings") {
		t.Errorf("blocks on the wire = %q", blocks)
	}
	if text := stub.forms[0].Get("text"); len([]rune(text)) > discordChunk+1 {
		t.Errorf("fallback text is %d runes, over one chunk", len([]rune(text)))
	}
}

// TestSlackNotifyReportsSlacksRefusalOfTheBlocks: blocks Slack will not
// render come back as the answer's error, so the caller falls back to text.
func TestSlackNotifyReportsSlacksRefusalOfTheBlocks(t *testing.T) {
	adapter, stub := startSlackStubAdapter(t)
	stub.fail = "invalid_blocks"
	n := newTestSlackNotifier(t, adapter)
	got := serveJSON(t, n, lib.NotifyRequest{Text: "x", Blocks: json.RawMessage(`[{"type":"no-such-block"}]`)})
	if !strings.Contains(got.Error, "invalid_blocks") {
		t.Errorf("answer = %+v, want Slack's invalid_blocks", got)
	}
}

// TestNotifyRefusesBlocksABackendCannotRender: Chat's notifier has no Block
// Kit half, so blocks are refused there rather than dropped; and blocks that
// are not a non-empty array are refused on Slack.
func TestNotifyRefusesBlocksABackendCannotRender(t *testing.T) {
	p := &fakeNotifyPoster{}
	chat := newTestNotifier(t, p)
	if got := serveJSON(t, chat, lib.NotifyRequest{Text: "x", Blocks: json.RawMessage(testBlocks)}); got.Error == "" {
		t.Error("Chat's notifier accepted blocks")
	}
	slackN := newTestSlackNotifier(t, newTestSlackAdapter(&fakeSlackAPI{}))
	for _, bad := range []string{`{}`, `[]`, `"section"`, `[1`} {
		body := []byte(`{"text":"x","blocks":` + bad + `}`)
		if got := serveBytes(t, slackN, body); got.Error == "" {
			t.Errorf("blocks %s were accepted", bad)
		}
	}
	if len(p.all()) != 0 {
		t.Errorf("a refused request posted: %v", p.all())
	}
}

// TestSlackNotifyRefusesMentionsInBlocks: the text path escapes every
// mention; blocks cannot be escaped, so a block carrying one is refused and
// the caller posts the escaped text instead. A link is not a mention.
func TestSlackNotifyRefusesMentionsInBlocks(t *testing.T) {
	adapter, stub := startSlackStubAdapter(t)
	n := newTestSlackNotifier(t, adapter)
	for name, blocks := range map[string]string{
		"channel":       `[{"type":"section","text":{"type":"mrkdwn","text":"<!channel> drift"}}]`,
		"here":          `[{"type":"section","text":{"type":"mrkdwn","text":"see <!here>"}}]`,
		"subteam":       `[{"type":"context","elements":[{"type":"mrkdwn","text":"<!subteam^S1> look"}]}]`,
		"user":          `[{"type":"section","fields":[{"type":"mrkdwn","text":"<@U123>"}]}]`,
		"broadcast":     `[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"broadcast","range":"channel"}]}]}]`,
		"rich user":     `[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"user","user_id":"U1"}]}]}]`,
		"bare here":     `[{"type":"section","text":{"type":"mrkdwn","text":"heads up @here"}}]`,
		"bare everyone": `[{"type":"context","elements":[{"type":"mrkdwn","text":"@everyone look"}]}]`,
		"mixed case":    `[{"type":"section","text":{"type":"mrkdwn","text":"see @Channel now"}}]`,
	} {
		if got := serveJSON(t, n, lib.NotifyRequest{Text: "x", Blocks: json.RawMessage(blocks)}); got.Error == "" {
			t.Errorf("%s: blocks with a mention were accepted", name)
		}
	}
	if len(stub.forms) != 0 {
		t.Errorf("a refused request posted: %d posts", len(stub.forms))
	}
	link := `[{"type":"section","text":{"type":"mrkdwn","text":"<https://github.com/o/r/issues/1|ledger #1>"}}]`
	if got := serveJSON(t, n, lib.NotifyRequest{Text: "x", Blocks: json.RawMessage(link)}); got.Error != "" {
		t.Errorf("a link was refused as a mention: %+v", got)
	}
}

const testSlackConversation = "slack:C0OTHER/1700000000.000300"

// TestSlackNotifyWithNoHomeServesConversationsOnly: with no home channel the
// Slack route refuses a home post but posts a card's report into a live
// conversation, Slack's own, through the gateway's adapter.
func TestSlackNotifyWithNoHomeServesConversationsOnly(t *testing.T) {
	home := &fakeNotifyPoster{}
	n, err := NewSlackNotifier(home, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	conv := &fakeConversations{contexts: map[string]string{testSlackConversation: testContext}}
	n.SetConversations(conv)
	if got := serveJSON(t, n, lib.NotifyRequest{Text: "drift"}); got.Error != notifyNoHome {
		t.Errorf("home post with no home = %+v, want %q", got, notifyNoHome)
	}
	got := serveJSON(t, n, lib.NotifyRequest{Text: "3 nodes", Conversation: testSlackConversation, ContextID: testContext})
	if got.Error != "" || got.ThreadID != testSlackConversation {
		t.Fatalf("conversation post = %+v", got)
	}
	if len(conv.posts) != 1 || len(home.all()) != 0 {
		t.Fatalf("conversation posts %v, home posts %v", conv.posts, home.all())
	}
	// Chat's conversation is not Slack's to post into.
	conv.contexts[testConversation] = testContext
	if got := serveJSON(t, n, lib.NotifyRequest{Text: "x", Conversation: testConversation, ContextID: testContext}); !strings.Contains(got.Error, "not on this route's backend") {
		t.Errorf("a Chat conversation on Slack's route = %+v", got)
	}
}

// TestNotifyRefusesBlocksOnAConversation: raw Block Kit is for home posts; a
// conversation post carries its layout as chat, so blocks there are refused
// rather than dropped.
func TestNotifyRefusesBlocksOnAConversation(t *testing.T) {
	n := newTestSlackNotifier(t, newTestSlackAdapter(&fakeSlackAPI{}))
	conv := &fakeConversations{contexts: map[string]string{testSlackConversation: testContext}}
	n.SetConversations(conv)
	got := serveJSON(t, n, lib.NotifyRequest{Text: "x", Conversation: testSlackConversation, ContextID: testContext, Blocks: json.RawMessage(testBlocks)})
	if !strings.Contains(got.Error, "carries no blocks") || len(conv.posts) != 0 {
		t.Fatalf("reply = %+v, posts = %v", got, conv.posts)
	}
}

// TestSlackNotifyRefusesInteractiveBlocks: the gateway acks no click, so a
// button, a select or an actions block is refused at the gateway, whatever
// the sender stripped; a link in text is not interactive.
func TestSlackNotifyRefusesInteractiveBlocks(t *testing.T) {
	adapter, stub := startSlackStubAdapter(t)
	n := newTestSlackNotifier(t, adapter)
	for name, blocks := range map[string]string{
		"actions block": `[{"type":"actions","elements":[{"type":"button","text":{"type":"plain_text","text":"Look"},"action_id":"a"}]}]`,
		"accessory":     `[{"type":"section","text":{"type":"mrkdwn","text":"x"},"accessory":{"type":"button","text":{"type":"plain_text","text":"Go"},"action_id":"b"}}]`,
		"select":        `[{"type":"section","text":{"type":"mrkdwn","text":"x"},"accessory":{"type":"static_select","action_id":"c","options":[]}}]`,
		"input":         `[{"type":"input","label":{"type":"plain_text","text":"y"},"element":{"type":"plain_text_input","action_id":"d"}}]`,
	} {
		if got := serveJSON(t, n, lib.NotifyRequest{Text: "x", Blocks: json.RawMessage(blocks)}); !strings.Contains(got.Error, "interactive") {
			t.Errorf("%s: reply = %+v, want an interactive refusal", name, got)
		}
	}
	if len(stub.forms) != 0 {
		t.Errorf("a refused request posted: %d posts", len(stub.forms))
	}
	if got := serveJSON(t, n, lib.NotifyRequest{Text: "x", Blocks: json.RawMessage(testBlocks)}); got.Error != "" {
		t.Errorf("a plain section was refused: %+v", got)
	}
}

// TestSlackNotifyChecksAQualifiedThreadsChannel: a thread qualified by its
// channel ("<channel>/<ts>", as the kanban stand-in sends it) is admitted
// only for the home channel and posted on its bare ts; a DM's or another
// channel's thread is refused, so a card filed there never reports into home.
func TestSlackNotifyChecksAQualifiedThreadsChannel(t *testing.T) {
	p := &fakeNotifyPoster{}
	n := newTestSlackNotifier(t, p)
	got := serveJSON(t, n, lib.NotifyRequest{Text: "report", Thread: testSlackHome + "/1700000000.000100"})
	if got.Error != "" {
		t.Fatalf("a home thread was refused: %+v", got)
	}
	if posts := p.all(); len(posts) != 1 || posts[0].space != testSlackHome || posts[0].thread != "1700000000.000100" {
		t.Fatalf("posts = %+v, want one on the bare ts in home", posts)
	}
	for _, thread := range []string{"D0DM/1700000000.000100", "C0OTHER/1700000000.000100", testSlackHome + "/not-a-ts", "/1700000000.000100"} {
		if got := serveJSON(t, n, lib.NotifyRequest{Text: "report", Thread: thread}); !strings.Contains(got.Error, "not a thread of the home channel") {
			t.Errorf("thread %q: reply = %+v, want a refusal", thread, got)
		}
	}
	if len(p.all()) != 1 {
		t.Errorf("a refused thread posted: %+v", p.all())
	}
}

var _ notifyPoster = (*SlackAdapter)(nil)
var _ notifyBlocksPoster = (*SlackAdapter)(nil)
