package gateway

import (
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// decodeBlocks reads a form field holding a JSON array of blocks.
func decodeBlocks(t *testing.T, raw string) []map[string]any {
	t.Helper()
	var blocks []map[string]any
	if err := json.Unmarshal([]byte(raw), &blocks); err != nil {
		t.Fatalf("blocks %q: %v", raw, err)
	}
	return blocks
}

// PostMessage puts the layout on the wire as Slack takes it: the headline
// above, the rest in the side bar coloured by tone, the text as mrkdwn
// beside it, into the thread, with previews off.
func TestSlackPostMessageSendsTheLayout(t *testing.T) {
	adapter, stub := startSlackStubAdapter(t)
	report := testReport
	report.Links = []chatmsg.Link{{Text: "Ledger issue #231", URL: "https://github.com/o/r/issues/231"}}
	ts, err := adapter.PostMessage("slack:C1/1.0", OutMessage{Text: "**3 of 12** <!here>", Chat: &report})
	if err != nil || ts == "" {
		t.Fatalf("PostMessage: ts=%q err=%v", ts, err)
	}
	form := stub.forms[0]
	if form.Get("thread_ts") != "1.0" || form.Get("unfurl_links") != "false" || form.Get("unfurl_media") != "false" {
		t.Fatalf("form = %v", form)
	}
	if text := form.Get("text"); text != "*3 of 12* &lt;!here&gt;" {
		t.Fatalf("text = %q, want the fallback as escaped mrkdwn", text)
	}
	blocks := decodeBlocks(t, form.Get("blocks"))
	if len(blocks) != 1 || blocks[0]["type"] != "rich_text" {
		t.Fatalf("top-level blocks = %v, want the headline alone", blocks)
	}
	var attachments []map[string]any
	if err := json.Unmarshal([]byte(form.Get("attachments")), &attachments); err != nil || len(attachments) != 1 {
		t.Fatalf("attachments = %q (%v)", form.Get("attachments"), err)
	}
	if attachments[0]["color"] != "#ECB22E" {
		t.Errorf("side bar = %v, want attention yellow", attachments[0]["color"])
	}
	barred, _ := json.Marshal(attachments[0]["blocks"])
	for _, want := range []string{`"divider"`, `"major"`, `"actions"`, `"https://github.com/o/r/issues/231"`} {
		if !strings.Contains(string(barred), want) {
			t.Errorf("side bar blocks lack %s: %s", want, barred)
		}
	}
}

// Without a chat message PostMessage is Post: no blocks, no side bar.
func TestSlackPostMessageWithoutChatIsPost(t *testing.T) {
	adapter, stub := startSlackStubAdapter(t)
	if _, err := adapter.PostMessage("slack:C1/1.0", OutMessage{Text: "plain"}); err != nil {
		t.Fatal(err)
	}
	if _, err := adapter.Post("slack:C1/1.0", "plain"); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(stub.forms[0], stub.forms[1]) {
		t.Fatalf("PostMessage without a chat message sent %v, Post sent %v", stub.forms[0], stub.forms[1])
	}
}

// EditMessage without a chat message replaces a laid-out message with its
// text: the blocks and the side bar go empty, since chat.update keeps what
// it is not sent.
func TestSlackEditMessageWithoutChatClearsTheLayout(t *testing.T) {
	adapter, stub := startSlackStubAdapter(t)
	if err := adapter.EditMessage("slack:C1/1.0", "2.0", OutMessage{Text: "✅ **completed**"}); err != nil {
		t.Fatal(err)
	}
	form := stub.forms[0]
	if form.Get("ts") != "2.0" || form.Get("text") != "✅ *completed*" || form.Get("blocks") != "[]" || form.Get("attachments") != "[]" {
		t.Fatalf("form = %v", form)
	}
}

// Slack's own refusal comes back as the error, for the gateway to post the
// text instead.
func TestSlackPostMessageReturnsSlacksRefusal(t *testing.T) {
	adapter, stub := startSlackStubAdapter(t)
	stub.fail = "invalid_blocks"
	if _, err := adapter.PostMessage("slack:C1/1.0", OutMessage{Text: "x", Chat: &testReport}); err == nil || !strings.Contains(err.Error(), "invalid_blocks") {
		t.Fatalf("err = %v, want Slack's invalid_blocks", err)
	}
}

// A fallback past the text cap is a message Slack cannot hold, refused
// before any call.
func TestSlackPostMessageRefusesAnOversizedFallback(t *testing.T) {
	adapter, stub := startSlackStubAdapter(t)
	_, err := adapter.PostMessage("slack:C1/1.0", OutMessage{Text: strings.Repeat("x", slackChatTextMax+1), Chat: &testReport})
	if !errors.Is(err, errSlackChatTooLarge) || len(stub.forms) != 0 {
		t.Fatalf("err = %v, posts = %d; want errSlackChatTooLarge and nothing sent", err, len(stub.forms))
	}
}

func TestSlackRichMarkdownSpans(t *testing.T) {
	for _, tc := range []struct {
		md   string
		want string
	}{
		{"plain", `[{"text":"plain","type":"text"}]`},
		{"`kubectl get` it", `[{"style":{"code":true},"text":"kubectl get","type":"text"},{"text":" it","type":"text"}]`},
		{"a **bold** b", `[{"text":"a ","type":"text"},{"style":{"bold":true},"text":"bold","type":"text"},{"text":" b","type":"text"}]`},
		{"_it_ and ~gone~", `[{"style":{"italic":true},"text":"it","type":"text"},{"text":" and ","type":"text"},{"style":{"strike":true},"text":"gone","type":"text"}]`},
		{"see [the issue](https://github.com/o/r/issues/1)", `[{"text":"see ","type":"text"},{"text":"the issue","type":"link","url":"https://github.com/o/r/issues/1"}]`},
		// A label naming another host shows as its label alone.
		{"[https://good.example](https://evil.example/x)", `[{"text":"https://good.example","type":"text"}]`},
		// In-word markers are text.
		{"2*3*4 and DB_HOST_NAME", `[{"text":"2*3*4 and DB_HOST_NAME","type":"text"}]`},
		// rich_text is literal, so a mention token is plain text here, and
		// Slack shows it as written.
		{"ping <!here>", `[{"text":"ping \u003c!here\u003e","type":"text"}]`},
	} {
		got, _ := json.Marshal(slackRichMarkdown(tc.md))
		if string(got) != tc.want {
			t.Errorf("slackRichMarkdown(%q)\n got %s\nwant %s", tc.md, got, tc.want)
		}
	}
}

// Nothing renders as a mention. A token in a literal field (rich_text text,
// a button label, a plan row) is shown as written; one in the fold's mrkdwn
// is escaped and sent verbatim. The one thing escaping cannot neutralise, a
// bare broadcast word in mrkdwn, makes the message one the renderer will
// not send (blocksMention), and the fallback posts.
func TestSlackChatRendersNoMention(t *testing.T) {
	for _, m := range []chatmsg.Message{
		{Schema: chatmsg.Schema, Headline: "ping <!channel>"},
		{Schema: chatmsg.Schema, Rows: []chatmsg.Row{{Text: "cc <@U0123ABC>"}}},
		{Schema: chatmsg.Schema, Choices: []chatmsg.Choice{{Text: "<!here>"}}},
		{Schema: chatmsg.Schema, Fold: &chatmsg.Fold{Text: "write <!channel> to ping"}},
	} {
		layout, err := renderSlackChat(&m, "x")
		if err != nil {
			t.Errorf("renderSlackChat(%+v): %v", m, err)
			continue
		}
		raw, _ := json.Marshal(layout)
		var decoded any
		if err := json.Unmarshal(raw, &decoded); err != nil {
			t.Fatal(err)
		}
		for _, text := range slackMrkdwnTexts(decoded, nil) {
			if s, _ := text.(string); strings.Contains(s, "<!") || strings.Contains(s, "<@") {
				t.Errorf("a token reached mrkdwn unescaped: %q", s)
			}
		}
	}
	fold := chatmsg.Message{Schema: chatmsg.Schema, Fold: &chatmsg.Fold{Text: "write <!channel> to ping"}}
	layout, err := renderSlackChat(&fold, "x")
	if err != nil {
		t.Fatal(err)
	}
	section := layout.Blocks[0]["child_blocks"].([]map[string]any)[0]["text"].(map[string]any)
	if section["text"] != "write &lt;!channel&gt; to ping" || section["verbatim"] != true {
		t.Fatalf("fold section = %v, want the token escaped and sent verbatim", section)
	}
	bare := chatmsg.Message{Schema: chatmsg.Schema, Fold: &chatmsg.Fold{Text: "hey @here, look"}}
	if _, err := renderSlackChat(&bare, "x"); err == nil || !strings.Contains(err.Error(), "mention") {
		t.Fatalf("err = %v, want the bare broadcast word in mrkdwn refused", err)
	}
}

// A fold's text is cut into sections under Slack's cap; a chunk that escaping
// pushes past the section cap is too large, and posts as text.
func TestSlackChatFoldTooLarge(t *testing.T) {
	m := chatmsg.Message{Schema: chatmsg.Schema, Fold: &chatmsg.Fold{Text: strings.Repeat("&", 12000)}}
	if _, err := renderSlackChat(&m, "x"); !errors.Is(err, errSlackChatTooLarge) {
		t.Fatalf("err = %v, want errSlackChatTooLarge (each & escapes to five characters)", err)
	}
}

// Slack refuses a container in an attachment, so with a fold the bar starts
// after it: the fold and what is above it stay top-level.
func TestSlackChatSideBarStartsAfterTheFold(t *testing.T) {
	m := chatmsg.Message{Schema: chatmsg.Schema, Headline: "h", Detail: "d", Tone: chatmsg.ToneCritical,
		Fold:    &chatmsg.Fold{Text: "why"},
		Choices: []chatmsg.Choice{{Text: "go"}}}
	layout, err := renderSlackChat(&m, "t")
	if err != nil {
		t.Fatal(err)
	}
	if len(layout.Blocks) != 3 || layout.Blocks[2]["type"] != "container" {
		t.Fatalf("top-level blocks = %v, want headline, detail, fold", layout.Blocks)
	}
	if len(layout.Attachments) != 1 || layout.Attachments[0]["color"] != "#E01E5A" {
		t.Fatalf("attachments = %v, want the buttons in a red bar", layout.Attachments)
	}
	noButtons := m
	noButtons.Choices = nil
	if layout, err := renderSlackChat(&noButtons, "t"); err != nil || len(layout.Attachments) != 0 {
		t.Fatalf("layout = %+v err = %v, want no bar when nothing follows the fold", layout, err)
	}
}

// The side bar needs something under the headline; a lone block keeps no
// colour, as with_side_bar keeps none.
func TestSlackChatSideBarOnlyWithBlocksBelowTheHeadline(t *testing.T) {
	m := chatmsg.Message{Schema: chatmsg.Schema, Headline: "done", Tone: chatmsg.ToneSuccess}
	layout, err := renderSlackChat(&m, "done")
	if err != nil || len(layout.Blocks) != 1 || len(layout.Attachments) != 0 {
		t.Fatalf("layout = %+v err = %v", layout, err)
	}
}
