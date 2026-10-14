package gateway

import (
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/slack-go/slack"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// liveSpecExample names specExample in the live thread.
const liveSpecExample = "spec-example"

// specExample is spec-a2a-payloads.md's example object, mark and plan aside:
// every layout field, a fold table included.
var specExample = chatmsg.Message{
	Schema:   chatmsg.Schema,
	Headline: "3 of 12 clusters are behind the GKE stable channel",
	Note:     "checked 12 of 12",
	Detail:   "Auto-upgrade is off on all three.",
	Tone:     chatmsg.ToneAttention,
	Rows:     []chatmsg.Row{{Text: "`prod-eu` is on 1.30, stable is 1.31", Severity: "major", Detail: "since 9/12"}},
	After:    []string{"2 more are in the ledger issue."},
	Fold: &chatmsg.Fold{Title: "why", Text: "Each cluster's channel and version, as `gcloud` reported them.",
		Table: &chatmsg.Table{Columns: []string{"cluster", "channel", "version"}, Rows: [][]string{{"prod-eu", "none", "1.30.4"}}}},
	Choices: []chatmsg.Choice{{Text: "Look at the first one", Reply: "Look at the first one: prod-eu", Primary: true}},
	Links:   []chatmsg.Link{{Text: "Ledger issue #231", URL: "https://github.com/o/r/issues/231"}},
}

// TestLiveSlackChatCases posts every chat message the shared cases lay out
// (the golden files' inputs) into one thread of a real channel through the
// Slack adapter's PostMessage, so a person can read Slack's own rendering of
// each and Slack's API is the judge of whether it takes the blocks: a case
// it refuses fails the test with Slack's error. It uses the Web API alone and
// opens no Socket Mode connection, so it runs beside a deployed gateway on
// the same app.
//
// Skipped unless the env is set:
//
//	SLACK_LIVE_BOT_TOKEN=xoxb-… SLACK_LIVE_CHANNEL=C… \
//	go test ./gateway -run TestLiveSlackChatCases -v -count=1
//
// SLACK_LIVE_CASES, a comma-separated list of case names, posts only those;
// SLACK_LIVE_EDIT set also edits the spec example back to text with Edit,
// which sends the blocks and side bar cleared.
func TestLiveSlackChatCases(t *testing.T) {
	botToken := os.Getenv("SLACK_LIVE_BOT_TOKEN")
	channel := os.Getenv("SLACK_LIVE_CHANNEL")
	if botToken == "" || channel == "" {
		t.Skip("live Slack env not set; see comment")
	}
	only := map[string]bool{}
	for _, name := range strings.Split(os.Getenv("SLACK_LIVE_CASES"), ",") {
		if name = strings.TrimSpace(name); name != "" {
			only[name] = true
		}
	}
	a := newSlackAdapter(botToken, slackAppTokenPrefix+"unused", slog.New(slog.NewTextHandler(os.Stderr, nil)))
	_, root, err := a.api.PostMessage(channel, slack.MsgOptionText(
		"kube-agents.chat/v1 Slack renderer, every shared case "+time.Now().UTC().Format(time.RFC3339), false))
	if err != nil {
		t.Fatalf("root post: %v", err)
	}
	conv := slackConversationID("channel", channel, root)
	t.Logf("thread root ts %s", root)

	// No shared case carries a fold table, so the payload spec's own example
	// object, which has every layout field, goes first.
	if len(only) == 0 || only[liveSpecExample] {
		if ts, err := a.PostMessage(conv, OutMessage{Text: liveSpecExample + " (the text fallback)", Chat: &specExample}); err != nil {
			t.Errorf("%s: Slack refused the layout: %v", liveSpecExample, err)
		} else {
			t.Logf("%s posted as %s", liveSpecExample, ts)
			// A text Edit of a laid-out message, as the rolling line's after
			// a gateway restart: Slack must take the cleared blocks.
			if os.Getenv("SLACK_LIVE_EDIT") != "" {
				if err := a.Edit(conv, ts, liveSpecExample+" edited back to text"); err != nil {
					t.Errorf("%s: Slack refused the text edit that clears the layout: %v", liveSpecExample, err)
				}
			}
		}
	}
	paths, err := filepath.Glob(filepath.Join(chatCasesDir, "*.json"))
	if err != nil || len(paths) == 0 {
		t.Fatalf("no shared cases under %s: %v", chatCasesDir, err)
	}
	for _, path := range paths {
		name := strings.TrimSuffix(filepath.Base(path), ".json")
		if len(only) > 0 && !only[name] {
			continue
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var c goldenCase
		if err := json.Unmarshal(raw, &c); err != nil {
			t.Fatal(err)
		}
		for i, out := range c.Expected {
			if out == nil {
				continue
			}
			m, err := chatmsg.Parse(out.Chat)
			if err != nil || out.Carrier == chatmsg.Terminal || m.MarkOnly() {
				continue
			}
			if !out.Carrier.ReadsPlan() {
				m.Plan = nil
			}
			text := name + " (expected[" + strconv.Itoa(i) + "], the text fallback)"
			ts, err := a.PostMessage(conv, OutMessage{Text: text, Chat: &m})
			if err != nil {
				t.Errorf("%s expected[%d]: Slack refused the layout: %v", name, i, err)
				continue
			}
			t.Logf("%s expected[%d] posted as %s", name, i, ts)
		}
	}
}
