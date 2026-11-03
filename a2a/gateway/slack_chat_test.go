package gateway

import (
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/slack-go/slack"

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
	// The fold is rich text too, so a token there is literal text and a
	// bare broadcast word is inert: neither is mrkdwn.
	for _, text := range []string{"write <!channel> to ping", "hey @here, look"} {
		fold := chatmsg.Message{Schema: chatmsg.Schema, Fold: &chatmsg.Fold{Text: text}}
		layout, err := renderSlackChat(&fold, "x")
		if err != nil {
			t.Fatal(err)
		}
		child := layout.Blocks[0]["child_blocks"].([]map[string]any)[0]
		span := child["elements"].([]map[string]any)[0]["elements"].([]map[string]any)[0]
		if child["type"] != "rich_text" || span["text"] != text {
			t.Fatalf("fold child = %v, want %q as rich text, as written", child, text)
		}
	}
}

// slackFoldRunes is the runes of text in each of a fold's blocks.
func slackFoldRunes(t *testing.T, layout slackChatLayout) []int {
	t.Helper()
	var sizes []int
	for _, c := range layout.Blocks[0]["child_blocks"].([]map[string]any) {
		sizes = append(sizes, slackRichLen(map[string]any{"elements": c["elements"]}))
	}
	return sizes
}

// A fold at the spec's cap is cut into blocks under a section's share, each
// a block of rich text, so text dense with characters mrkdwn would escape
// fits whole.
func TestSlackChatFoldAtTheCapIsCut(t *testing.T) {
	m := chatmsg.Message{Schema: chatmsg.Schema, Fold: &chatmsg.Fold{Text: strings.Repeat("&", 12000)}}
	layout, err := renderSlackChat(&m, "x")
	if err != nil {
		t.Fatal(err)
	}
	total := 0
	for _, n := range slackFoldRunes(t, layout) {
		if n > slackChatFoldChunk {
			t.Fatalf("a block of %d runes, over the share", n)
		}
		total += n
	}
	if total != 12000 {
		t.Fatalf("the fold holds %d runes, want all 12000", total)
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

// A fold with neither text nor a table folds nothing and is omitted; the
// rest of the message still lays out.
func TestSlackChatEmptyFoldIsOmitted(t *testing.T) {
	m := chatmsg.Message{Schema: chatmsg.Schema, Headline: "h", Fold: &chatmsg.Fold{Title: "why"}}
	layout, err := renderSlackChat(&m, "t")
	if err != nil || len(layout.Blocks) != 1 {
		t.Fatalf("layout = %+v err = %v, want the headline alone", layout, err)
	}
}

// A fenced block longer than a block's share is cut between its lines into
// several preformatted blocks, none over the share.
func TestSlackChatLongFenceInTheFoldIsCut(t *testing.T) {
	code := strings.Repeat("kubectl get pods -A -o wide\n", 400)
	m := chatmsg.Message{Schema: chatmsg.Schema, Fold: &chatmsg.Fold{Text: "Ran:\n```\n" + code + "```"}}
	layout, err := renderSlackChat(&m, "t")
	if err != nil {
		t.Fatalf("a long fence in the fold was refused: %v", err)
	}
	for _, n := range slackFoldRunes(t, layout) {
		if n > slackChatFoldChunk {
			t.Fatalf("a block of %d runes, over the share", n)
		}
	}
}

// Slack's Edit replaces the whole message, blocks and side bar cleared, so a
// live message laid out before a gateway restart loses its stale layout.
func TestSlackEditClearsAnyLayout(t *testing.T) {
	adapter, stub := startSlackStubAdapter(t)
	if err := adapter.Edit("slack:C1/1.0", "2.0", "✅ **completed**"); err != nil {
		t.Fatal(err)
	}
	form := stub.forms[0]
	if form.Get("text") != "✅ *completed*" || form.Get("blocks") != "[]" || form.Get("attachments") != "[]" {
		t.Fatalf("form = %v", form)
	}
}

// Slack's refusal of a layout is ErrLayoutRefused; any other API error is
// not, and is left to be retried.
func TestSlackLayoutErrorClassifies(t *testing.T) {
	if err := slackLayoutError(slack.SlackErrorResponse{Err: "invalid_blocks"}); !errors.Is(err, ErrLayoutRefused) {
		t.Errorf("invalid_blocks: %v, want ErrLayoutRefused", err)
	}
	if err := slackLayoutError(slack.SlackErrorResponse{Err: "ratelimited"}); errors.Is(err, ErrLayoutRefused) {
		t.Errorf("ratelimited: %v, want no refusal", err)
	}
	if slackLayoutError(nil) != nil {
		t.Error("nil error wrapped")
	}
}

// A fold of multi-byte text at the spec's 12,000-character cap fits: the
// chunk budget is scaled to characters, so CJK text is not cut into three
// times the sections ASCII is.
func TestSlackChatMultibyteFoldAtTheCapFits(t *testing.T) {
	m := chatmsg.Message{Schema: chatmsg.Schema, Fold: &chatmsg.Fold{Text: strings.Repeat("集", 12000)}}
	if layout, err := chatmsg.Validate(m, chatmsg.Result); layout != nil || err != nil {
		t.Fatalf("the fixture is not a valid message: %v %v", layout, err)
	}
	layout, err := renderSlackChat(&m, "t")
	if err != nil {
		t.Fatalf("a valid multi-byte fold at the cap was refused: %v", err)
	}
	for _, n := range slackFoldRunes(t, layout) {
		if n > slackChatSectionMax {
			t.Fatalf("a block of %d characters, over Slack's cap", n)
		}
	}
}

// Slack's answer that a message cannot be edited is ErrMessageGone.
func TestSlackLayoutErrorClassifiesAGoneMessage(t *testing.T) {
	if err := slackLayoutError(slack.SlackErrorResponse{Err: "message_not_found"}); !errors.Is(err, ErrMessageGone) || errors.Is(err, ErrLayoutRefused) {
		t.Fatalf("message_not_found: %v, want ErrMessageGone alone", err)
	}
}

// A waiting plan item draws in_progress, since Slack's task_card refuses
// pending with invalid_blocks, and its steps stay all done: the open mark
// follows the item's own status, not the drawn one.
func TestSlackChatPlanDrawsAWaitingItemInProgressWithItsStepsDone(t *testing.T) {
	tasks := slackChatPlan(&chatmsg.Plan{Items: []chatmsg.PlanItem{
		{ID: "t_a", Title: "waiting on you", Status: chatmsg.StatusPending, Steps: []string{"found two", "asked which"}},
	}})
	if len(tasks) != 1 || tasks[0]["status"] != chatmsg.StatusInProgress {
		t.Fatalf("tasks = %v, want one in_progress card", tasks)
	}
	raw, err := json.Marshal(tasks[0]["details"])
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{slackChatStepDone + " found two", slackChatStepDone + " asked which"} {
		if !strings.Contains(string(raw), want) {
			t.Errorf("details %s lack %q", raw, want)
		}
	}
	if strings.Contains(string(raw), slackChatStepOpen) {
		t.Errorf("details %s mark a step open on a waiting item", raw)
	}
}
