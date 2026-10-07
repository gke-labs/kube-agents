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
	for _, home := range []string{"", "D0DM", "general", "#ops", "c0lower", "C", "spaces/AAA", "C0HOME/1.2"} {
		if _, err := NewSlackNotifier(&fakeNotifyPoster{}, home, nil); err == nil {
			t.Errorf("home %q was accepted", home)
		}
	}
	for _, home := range []string{"C0HOME", "G0PRIVATE"} {
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

var _ notifyPoster = (*SlackAdapter)(nil)
var _ notifyBlocksPoster = (*SlackAdapter)(nil)
