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

// A home-channel chat.notify request carrying a chat message: the cron
// relay's scheduled report card under next. Slack lays it out as one
// message; an invalid chat message, a refused layout or a backend without a
// renderer posts the text, as the request did before.

const testHomeChat = `{"schema":"kube-agents.chat/v1","headline":"Security & RBAC audit: 2 critical findings",` +
	`"rows":[{"text":"seeded-b admits privileged pods","severity":"critical"}],` +
	`"after":["2 more (1 major, 1 minor) are in the ledger issue."],` +
	`"choices":[{"text":"Look at the first one","primary":true}],` +
	`"links":[{"text":"Ledger issue #231 ↗","url":"https://github.com/o/r/issues/231"}]}`

const testHomeChatText = "**Security & RBAC audit: 2 critical findings**\n`critical` seeded-b admits privileged pods"

// stubSlackRefusesBlocks answers chat.postMessage with invalid_blocks when
// the call carries blocks, and ok otherwise, recording each form.
type stubSlackRefusesBlocks struct {
	mu    sync.Mutex
	forms []url.Values
}

func (s *stubSlackRefusesBlocks) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	_ = r.ParseForm()
	s.mu.Lock()
	s.forms = append(s.forms, r.Form)
	s.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	if blocks := r.Form.Get("blocks"); blocks != "" && blocks != "null" && blocks != "[]" {
		_, _ = w.Write([]byte(`{"ok":false,"error":"invalid_blocks"}`))
		return
	}
	_, _ = w.Write([]byte(`{"ok":true,"channel":"C0HOME","ts":"1700000000.000300"}`))
}

func TestSlackNotifyLaysOutAHomeChatMessage(t *testing.T) {
	adapter, stub := startSlackStubAdapter(t)
	n := newTestSlackNotifier(t, adapter)
	got := serveJSON(t, n, lib.NotifyRequest{Text: testHomeChatText, Chat: json.RawMessage(testHomeChat)})
	if got.Error != "" || got.MessageID != "1700000000.000200" || got.ThreadID != "1700000000.000200" {
		t.Fatalf("answer = %+v", got)
	}
	if len(stub.forms) != 1 || stub.forms[0].Get("channel") != testSlackHome {
		t.Fatalf("posts = %v, want one message in the home channel", stub.forms)
	}
	blocks := stub.forms[0].Get("blocks") + stub.forms[0].Get("attachments")
	for _, want := range []string{"RBAC audit: 2 critical findings", "seeded-b admits privileged pods",
		"in the ledger issue", "Look at the first one", "https://github.com/o/r/issues/231"} {
		if !strings.Contains(blocks, want) {
			t.Errorf("layout on the wire lacks %q: %s", want, blocks)
		}
	}
	if stub.forms[0].Get("unfurl_links") != "false" || stub.forms[0].Get("unfurl_media") != "false" {
		t.Errorf("a notify card unfurls: unfurl_links=%q unfurl_media=%q", stub.forms[0].Get("unfurl_links"), stub.forms[0].Get("unfurl_media"))
	}
	// The card carries a choice, so it is its thread's open question, as a
	// conversation's card is: a typed answer settles it.
	adapter.mu.Lock()
	q, open := adapter.uxLocked().questions["slack:"+testSlackHome+"/1700000000.000200"]
	adapter.mu.Unlock()
	if !open || q.ts != "1700000000.000200" {
		t.Fatalf("open question = %+v, %v; want the home card", q, open)
	}
}

func TestSlackNotifyRepliesWithAHomeChatMessageOnAThread(t *testing.T) {
	adapter, stub := startSlackStubAdapter(t)
	n := newTestSlackNotifier(t, adapter)
	thread := testSlackHome + "/1700000000.000100"
	got := serveJSON(t, n, lib.NotifyRequest{Text: testHomeChatText, Chat: json.RawMessage(testHomeChat), Thread: thread})
	if got.Error != "" || got.ThreadID != "1700000000.000100" {
		t.Fatalf("answer = %+v, want it on the thread", got)
	}
	if len(stub.forms) != 1 || stub.forms[0].Get("thread_ts") != "1700000000.000100" {
		t.Fatalf("posts = %v, want one reply on the thread", stub.forms)
	}
}

func TestSlackNotifyPostsTheTextOfAnInvalidHomeChatMessage(t *testing.T) {
	adapter, stub := startSlackStubAdapter(t)
	n := newTestSlackNotifier(t, adapter)
	bad := `{"schema":"kube-agents.chat/v9","headline":"x"}`
	got := serveJSON(t, n, lib.NotifyRequest{Text: "plain report", Chat: json.RawMessage(bad)})
	if got.Error != "" {
		t.Fatalf("answer = %+v", got)
	}
	if len(stub.forms) != 1 || stub.forms[0].Get("text") != "plain report" {
		t.Fatalf("posts = %v, want the text alone", stub.forms)
	}
	if blocks := stub.forms[0].Get("blocks"); blocks != "" {
		t.Errorf("an invalid chat message was laid out: %s", blocks)
	}
}

func TestSlackNotifyPostsTheTextWhenSlackRefusesTheHomeLayout(t *testing.T) {
	stub := &stubSlackRefusesBlocks{}
	srv := httptest.NewServer(stub)
	t.Cleanup(srv.Close)
	adapter := newSlackAdapter("xoxb-stub", "xapp-stub", slog.Default(), slack.OptionAPIURL(srv.URL+"/"))
	n := newTestSlackNotifier(t, adapter)
	got := serveJSON(t, n, lib.NotifyRequest{Text: testHomeChatText, Chat: json.RawMessage(testHomeChat)})
	if got.Error != "" || got.MessageID != "1700000000.000300" {
		t.Fatalf("answer = %+v, want the text post's", got)
	}
	if len(stub.forms) != 2 {
		t.Fatalf("posts = %d, want the refused layout then the text", len(stub.forms))
	}
	if !strings.Contains(stub.forms[1].Get("text"), "RBAC audit: 2 critical") {
		t.Errorf("text post = %q", stub.forms[1].Get("text"))
	}
}

func TestNotifyPostsTheTextOfAHomeChatMessageOnABackendWithoutLayout(t *testing.T) {
	p := &fakeNotifyPoster{landsIn: testHome + "/threads/T1"}
	got := serveJSON(t, newTestNotifier(t, p), lib.NotifyRequest{Text: "plain report", Chat: json.RawMessage(testHomeChat)})
	if got.Error != "" {
		t.Fatalf("answer = %+v", got)
	}
	if posts := p.all(); len(posts) != 1 || posts[0].text != "plain report" {
		t.Fatalf("posts = %+v, want the text alone", posts)
	}
}
