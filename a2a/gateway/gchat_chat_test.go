package gateway

import (
	"errors"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// The Google Chat renderer lays a chat message out as Chat text in the
// spec's order, with the buttons as accessory widgets under it.
func TestRenderGchatChat(t *testing.T) {
	report := &chatmsg.Message{
		Schema:   chatmsg.Schema,
		Headline: "3 of 12 clusters are behind",
		Note:     "checked 12 of 12",
		Detail:   "Auto-upgrade is **off** on all three.\n\n## Why it matters\n- support ends soon",
		Tone:     chatmsg.ToneAttention,
		Rows: []chatmsg.Row{
			{Text: "`prod-eu` is on 1.30, stable is 1.31", Severity: "major", Detail: "since 9/12"},
			{Text: "`dev` is on 1.29"},
		},
		After: []string{"2 more are in the [ledger](https://github.com/o/r/issues/231)."},
		Fold: &chatmsg.Fold{
			Text:  "Each cluster's channel, as `gcloud` reported it.",
			Table: &chatmsg.Table{Columns: []string{"cluster", "channel"}, Rows: [][]string{{"prod-eu", "none"}, {"dev", "a|b"}}},
		},
		Choices: []chatmsg.Choice{{Text: "Look at the first one", Reply: "Look at the first one: prod-eu", Primary: true}, {Text: "Skip"}},
		Links:   []chatmsg.Link{{Text: "Ledger issue #231", URL: "https://github.com/o/r/issues/231"}},
	}
	layout, err := renderGchatChat(report)
	if err != nil {
		t.Fatal(err)
	}
	wantText := strings.Join([]string{
		"*3 of 12 clusters are behind* checked 12 of 12",
		"",
		"Auto-upgrade is *off* on all three.\n\n*Why it matters*\n- support ends soon",
		"",
		"- `major` `prod-eu` is on 1.30, stable is 1.31 · since 9/12\n- `dev` is on 1.29",
		"",
		"2 more are in the <https://github.com/o/r/issues/231|ledger>.",
		"",
		"*why*\nEach cluster's channel, as `gcloud` reported it.\n```\ncluster | channel\n--------+--------\nprod-eu | none\ndev     | a|b\n```",
	}, "\n")
	if layout.Text != wantText {
		t.Errorf("text =\n%s\nwant\n%s", layout.Text, wantText)
	}
	if len(layout.Widgets) != 1 {
		t.Fatalf("widgets = %v", layout.Widgets)
	}
	buttons := layout.Widgets[0]["buttonList"].(map[string]any)["buttons"].([]map[string]any)
	if len(buttons) != 3 {
		t.Fatalf("buttons = %v", buttons)
	}
	first := buttons[0]
	if first["text"] != "Look at the first one" || first["type"] != gchatChatButtonFilled {
		t.Errorf("primary choice = %v", first)
	}
	action := first["onClick"].(map[string]any)["action"].(map[string]any)
	params := action["parameters"].([]map[string]any)
	if action["function"] != gchatChatFunction || params[0]["value"] != "Look at the first one: prod-eu" || params[1]["value"] != "0" {
		t.Errorf("choice action = %v", action)
	}
	if _, filled := buttons[1]["type"]; filled {
		t.Errorf("a non-primary choice takes the default button type: %v", buttons[1])
	}
	skip := buttons[1]["onClick"].(map[string]any)["action"].(map[string]any)["parameters"].([]map[string]any)
	if skip[0]["value"] != "Skip" {
		t.Errorf("a choice with no reply sends its text: %v", skip)
	}
	link := buttons[2]["onClick"].(map[string]any)["openLink"].(map[string]any)
	if buttons[2]["text"] != "Ledger issue #231" || link["url"] != "https://github.com/o/r/issues/231" {
		t.Errorf("link button = %v", buttons[2])
	}
}

func TestRenderGchatChatPlanAndEdges(t *testing.T) {
	plan := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Upgrade readiness", Plan: &chatmsg.Plan{
		Title: "drawn nowhere",
		Items: []chatmsg.PlanItem{
			{ID: "t-1", Title: "Read cluster versions", Status: chatmsg.StatusComplete, Steps: []string{"listed 12 clusters"}},
			{ID: "t-2", Title: "Check channels", Status: chatmsg.StatusInProgress, Steps: []string{"read prod", "reading dev"}},
			{ID: "t-3", Title: "waiting on you", Status: chatmsg.StatusPending},
			{ID: "t-4", Title: "Broke", Status: chatmsg.StatusError},
		},
	}, Rows: []chatmsg.Row{{Text: "a row the plan displaces"}}}
	layout, err := renderGchatChat(plan)
	if err != nil {
		t.Fatal(err)
	}
	want := "*Upgrade readiness*\n\n✅ Read cluster versions\n  ✓ listed 12 clusters\n⚙️ Check channels\n  ✓ read prod\n  ◌ reading dev\n⏳ waiting on you\n❌ Broke"
	if layout.Text != want || layout.Widgets != nil {
		t.Errorf("plan =\n%s\nwidgets %v\nwant\n%s", layout.Text, layout.Widgets, want)
	}

	// Plain fields take the defang and no markdown: a mention in a headline
	// cannot ping, and a link in one is not made.
	m := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "<users/all> see [x](https://x.example)", Fold: &chatmsg.Fold{Title: "t", Table: &chatmsg.Table{Columns: []string{"<users/all>"}}}}
	layout, err = renderGchatChat(m)
	if err != nil {
		t.Fatal(err)
	}
	if want := "*< users/all> see [x](https://x.example)*\n\n*t*\n```\n< users/all>\n------------\n```"; layout.Text != want {
		t.Errorf("defanged head =\n%s\nwant\n%s", layout.Text, want)
	}

	// A row written as a list item or a rule, and plain fields the schema
	// caps but does not hold to one line.
	rows := &chatmsg.Message{Schema: chatmsg.Schema, Rows: []chatmsg.Row{{Text: "- restart the deployment", Detail: "---"}, {Text: "---"}, {Text: "* also this", Severity: "minor"}},
		Fold: &chatmsg.Fold{Title: "why\nnot", Table: &chatmsg.Table{Columns: []string{"state\nnow"}, Rows: [][]string{{"OOMKilled\nexit 137"}}}},
		Plan: nil}
	layout, err = renderGchatChat(rows)
	if err != nil {
		t.Fatal(err)
	}
	if want := "- restart the deployment\n- `minor` also this\n\n*why not*\n```\nstate now\n------------------\nOOMKilled exit 137\n```"; layout.Text != want {
		t.Errorf("rows =\n%s\nwant\n%s", layout.Text, want)
	}
	// A cell is plain: markup in one is shown as written, not read.
	plainCells := &chatmsg.Message{Schema: chatmsg.Schema, Fold: &chatmsg.Fold{Table: &chatmsg.Table{Columns: []string{"sev", "cmd", "where"},
		Rows: [][]string{{"**critical**", "`kubectl get pods`", "[ledger](https://x.example/l)"}, {"a|b", "<users/all>", "x"}}}}}
	layout, err = renderGchatChat(plainCells)
	if err != nil {
		t.Fatal(err)
	}
	if want := "*why*\n```\nsev          | cmd                | where\n-------------+--------------------+------------------------------\n**critical** | `kubectl get pods` | [ledger](https://x.example/l)\na|b          | < users/all>       | x\n```"; layout.Text != want {
		t.Errorf("plain cells =\n%s\nwant\n%s", layout.Text, want)
	}
	planTitle := &chatmsg.Message{Schema: chatmsg.Schema, Plan: &chatmsg.Plan{Items: []chatmsg.PlanItem{{ID: "a", Title: "two\nlines", Status: chatmsg.StatusComplete}}}}
	if layout, _ := renderGchatChat(planTitle); layout.Text != "✅ two lines" {
		t.Errorf("plan title = %q", layout.Text)
	}

	// Buttons alone are a layout; nothing at all is not.
	only := &chatmsg.Message{Schema: chatmsg.Schema, Links: []chatmsg.Link{{Text: "PR", URL: "https://github.com/o/r/pull/1"}}}
	if layout, err := renderGchatChat(only); err != nil || layout.Text != "" || len(layout.Widgets) != 1 {
		t.Errorf("links alone: %v %v", layout, err)
	}
	if _, err := renderGchatChat(&chatmsg.Message{Schema: chatmsg.Schema, Fold: &chatmsg.Fold{Title: "empty"}}); !errors.Is(err, errGchatChatEmpty) {
		t.Errorf("an empty message: %v", err)
	}
	big := &chatmsg.Message{Schema: chatmsg.Schema, Fold: &chatmsg.Fold{Text: strings.Repeat("x", gchatChatTextMax+1)}}
	if _, err := renderGchatChat(big); !errors.Is(err, errGchatChatTooLarge) {
		t.Errorf("a message past Chat's cap: %v", err)
	}
}

// Through the adapter: a chat message posts as one message with its text and
// accessory widgets, on the conversation's thread; the API's 400 is a refused
// layout and anything else is the call's own failure.
func TestGchatPostMessageLaysOut(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/S1/messages/M1"}
	a := newTestGchatAdapterWithRelay(t, f)
	m := OutMessage{Text: "fallback", Chat: &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Which cluster?",
		Choices: []chatmsg.Choice{{Text: "prod"}, {Text: "dev"}}}}
	id, err := a.PostMessage("gchat:spaces/S1/threads/T9", m)
	if err != nil || id != "spaces/S1/messages/M1" {
		t.Fatalf("post: %q %v", id, err)
	}
	c := f.call(0)
	body := c.arguments["body"].(map[string]any)
	if c.method != "create" || c.arguments["parent"] != "spaces/S1" || c.arguments["messageReplyOption"] != gchatReplyOption ||
		body["thread"].(map[string]any)["name"] != "spaces/S1/threads/T9" {
		t.Errorf("create = %+v", c)
	}
	if body["text"] != "*Which cluster?*" {
		t.Errorf("text = %q", body["text"])
	}
	widgets := body["accessoryWidgets"].([]any)
	if len(widgets) != 1 {
		t.Fatalf("widgets = %v", widgets)
	}
	buttons := widgets[0].(map[string]any)["buttonList"].(map[string]any)["buttons"].([]any)
	if len(buttons) != 2 || buttons[1].(map[string]any)["text"] != "dev" {
		t.Errorf("buttons = %v", buttons)
	}

	// No chat message: Post, text alone, no widgets field.
	if _, err := a.PostMessage("gchat:spaces/S1/threads/T9", OutMessage{Text: "**plain**"}); err != nil {
		t.Fatal(err)
	}
	if body := f.call(1).arguments["body"].(map[string]any); body["text"] != "*plain*" || body["accessoryWidgets"] != nil {
		t.Errorf("text post = %v", body)
	}

	f.responses["spaces.messages/create"] = fakeChatAPIError{status: 400, reason: "accessoryWidgets: invalid"}
	if _, err := a.PostMessage("gchat:spaces/S1/threads/T9", m); !errors.Is(err, ErrLayoutRefused) {
		t.Errorf("a 400 is a refused layout: %v", err)
	}
	f.responses["spaces.messages/create"] = fakeChatAPIError{status: 429, reason: "quota"}
	if _, err := a.PostMessage("gchat:spaces/S1/threads/T9", m); err == nil || errors.Is(err, ErrLayoutRefused) {
		t.Errorf("a 429 is the call's own failure: %v", err)
	}
	if _, err := a.PostMessage("gchat:spaces/S1/threads/T9", OutMessage{Text: "x", Chat: &chatmsg.Message{Schema: chatmsg.Schema}}); !errors.Is(err, ErrLayoutRefused) {
		t.Errorf("a message that lays nothing out is refused before any call: %v", err)
	}
}

func TestGchatEditMessageLaysOutOrClears(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/patch"] = map[string]any{"name": "spaces/S1/messages/M77"}
	a := newTestGchatAdapterWithRelay(t, f)
	chat := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Still working", Plan: &chatmsg.Plan{Items: []chatmsg.PlanItem{{ID: "a", Title: "step", Status: chatmsg.StatusInProgress}}},
		Links: []chatmsg.Link{{Text: "board", URL: "https://x.example/b"}}}
	if err := a.EditMessage("gchat:spaces/S1/threads/T9", "spaces/S1/messages/M77", OutMessage{Text: "fallback", Chat: chat}); err != nil {
		t.Fatal(err)
	}
	c := f.call(0)
	body := c.arguments["body"].(map[string]any)
	if c.method != "patch" || c.arguments["updateMask"] != gchatLayoutUpdateMask || body["text"] != "*Still working*\n\n⚙️ step" ||
		len(body["accessoryWidgets"].([]any)) != 1 {
		t.Errorf("laid-out patch = %+v", c)
	}
	// Without a chat message the text replaces the layout, widgets cleared.
	if err := a.EditMessage("gchat:spaces/S1/threads/T9", "spaces/S1/messages/M77", OutMessage{Text: "✅ **completed**"}); err != nil {
		t.Fatal(err)
	}
	body = f.call(1).arguments["body"].(map[string]any)
	if f.call(1).arguments["updateMask"] != gchatLayoutUpdateMask || body["text"] != "✅ *completed*" || len(body["accessoryWidgets"].([]any)) != 0 {
		t.Errorf("text patch = %+v", f.call(1))
	}
	f.responses["spaces.messages/patch"] = fakeChatAPIError{status: 404, reason: "not found"}
	if err := a.EditMessage("gchat:spaces/S1/threads/T9", "spaces/S1/messages/M77", OutMessage{Text: "x"}); !errors.Is(err, ErrMessageGone) {
		t.Errorf("a 404 is a gone message: %v", err)
	}
}

// A plain headline or fold title that holds a star of its own is left as
// written: wrapping it in Chat's bold mark would pair the added marks with
// its star and show it part bold.
func TestRenderGchatChatLeavesAStarredHeadlinePlain(t *testing.T) {
	for _, headline := range []string{"Memory is 2**20 bytes.", "kube-* clusters are behind", "*.yaml manifests missing"} {
		m := &chatmsg.Message{Schema: chatmsg.Schema, Headline: headline, Fold: &chatmsg.Fold{Title: headline, Text: "x"}}
		layout, err := renderGchatChat(m)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Count(layout.Text, headline) != 2 || strings.Contains(layout.Text, gchatChatBold+headline) {
			t.Errorf("headline %q renders as %q; want it twice, unwrapped", headline, layout.Text)
		}
	}
	plain, err := renderGchatChat(&chatmsg.Message{Schema: chatmsg.Schema, Headline: "Plain"})
	if err != nil || plain.Text != "*Plain*" {
		t.Errorf("a plain headline = %q, %v; want it bold", plain.Text, err)
	}
}
