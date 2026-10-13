package gateway

import (
	"bytes"
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// The Slack renderer's golden files: every chat message the shared cases
// (tests/conformance/chat, lifted from the today presenters' tests) expect
// an emitter to send, laid out as Block Kit, against a stored file per case
// under testdata/slack-chat. `go test ./gateway -run SlackChatGolden
// -update-slack-chat` rewrites them; a diff in review is a change to what
// Slack shows.
const (
	chatCasesDir    = "../../tests/conformance/chat"
	slackGoldenDir  = "testdata/slack-chat"
	slackGoldenText = "(the text fallback)"
)

var updateSlackGolden = flag.Bool("update-slack-chat", false, "rewrite the Slack renderer's golden files")

type goldenCase struct {
	Name     string `json:"name"`
	Expected []*struct {
		Carrier chatmsg.Carrier `json:"carrier"`
		Chat    json.RawMessage `json:"chat"`
		Update  string          `json:"update,omitempty"`
	} `json:"expected"`
}

// goldenLayout is one rendered message in a golden file.
type goldenLayout struct {
	Expected    int              `json:"expected"`
	Carrier     chatmsg.Carrier  `json:"carrier"`
	Blocks      []map[string]any `json:"blocks"`
	Attachments []map[string]any `json:"attachments"`
}

func TestSlackChatGolden(t *testing.T) {
	paths, err := filepath.Glob(filepath.Join(chatCasesDir, "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(paths) == 0 {
		t.Fatalf("no shared cases under %s: a glob that matches nothing checks nothing", chatCasesDir)
	}
	seen := map[string]bool{}
	for _, path := range paths {
		name := strings.TrimSuffix(filepath.Base(path), ".json")
		t.Run(name, func(t *testing.T) {
			raw, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			var c goldenCase
			if err := json.Unmarshal(raw, &c); err != nil {
				t.Fatal(err)
			}
			var layouts []goldenLayout
			for i, out := range c.Expected {
				if out == nil {
					continue
				}
				m, err := chatmsg.Parse(out.Chat)
				if err != nil {
					t.Fatalf("expected[%d]: %v", i, err)
				}
				if layout, _ := chatmsg.Validate(m, out.Carrier); layout != nil {
					t.Fatalf("expected[%d]: %v", i, layout)
				}
				if out.Carrier == chatmsg.Terminal || m.MarkOnly() {
					continue // a mark lays nothing out
				}
				if !out.Carrier.ReadsPlan() {
					m.Plan = nil
				}
				layout, err := renderSlackChat(&m, slackGoldenText)
				if err != nil {
					t.Fatalf("expected[%d]: %v", i, err)
				}
				layouts = append(layouts, goldenLayout{Expected: i, Carrier: out.Carrier, Blocks: layout.Blocks, Attachments: layout.Attachments})
			}
			if len(layouts) == 0 {
				return
			}
			seen[name+".json"] = true
			var got bytes.Buffer
			enc := json.NewEncoder(&got)
			enc.SetEscapeHTML(false)
			enc.SetIndent("", "  ")
			if err := enc.Encode(layouts); err != nil {
				t.Fatal(err)
			}
			golden := filepath.Join(slackGoldenDir, name+".json")
			if *updateSlackGolden {
				if err := os.MkdirAll(slackGoldenDir, 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(golden, got.Bytes(), 0o644); err != nil {
					t.Fatal(err)
				}
				return
			}
			want, err := os.ReadFile(golden)
			if err != nil {
				t.Fatalf("%v (run with -update-slack-chat to write it)", err)
			}
			if !bytes.Equal(got.Bytes(), want) {
				t.Errorf("the layout differs from %s; run with -update-slack-chat and review the diff\n got: %s", golden, got.String())
			}
		})
	}
	// A golden file whose case is gone, or no longer lays anything out, is
	// stale, and a reviewer reading it would read a layout nothing makes.
	stored, _ := filepath.Glob(filepath.Join(slackGoldenDir, "*.json"))
	for _, path := range stored {
		if !seen[filepath.Base(path)] && !*updateSlackGolden {
			t.Errorf("%s has no shared case that lays it out; delete it", path)
		}
	}
}
