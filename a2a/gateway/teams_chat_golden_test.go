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

const teamsGoldenDir = "testdata/teams-chat"

var updateTeamsGolden = flag.Bool("update-teams-chat", false, "rewrite the Teams renderer's golden files")

// teamsGoldenLayout is one expected chat message as the Teams renderer
// lays it out: the card's text and its buttons.
type teamsGoldenLayout struct {
	Expected int              `json:"expected"`
	Carrier  chatmsg.Carrier  `json:"carrier"`
	Text     string           `json:"text"`
	Actions  []map[string]any `json:"actions"`
}

// Every shared chat case (tests/conformance/chat) lays out on Teams as its
// golden file says, and the layout fits one activity as a card.
func TestTeamsChatGolden(t *testing.T) {
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
			var layouts []teamsGoldenLayout
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
				layout, err := renderTeamsChat(&m)
				if err != nil {
					t.Fatalf("expected[%d]: %v", i, err)
				}
				actions := append(append([]map[string]any{}, layout.Choices...), layout.Links...)
				if !teamsFitsCard(layout.Text, actions) {
					t.Fatalf("expected[%d]: the layout does not fit one activity", i)
				}
				layouts = append(layouts, teamsGoldenLayout{Expected: i, Carrier: out.Carrier, Text: layout.Text, Actions: actions})
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
			golden := filepath.Join(teamsGoldenDir, name+".json")
			if *updateTeamsGolden {
				if err := os.MkdirAll(teamsGoldenDir, 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(golden, got.Bytes(), 0o644); err != nil {
					t.Fatal(err)
				}
				return
			}
			want, err := os.ReadFile(golden)
			if err != nil {
				t.Fatalf("%v (run with -update-teams-chat to write it)", err)
			}
			if !bytes.Equal(got.Bytes(), want) {
				t.Errorf("the layout differs from %s; run with -update-teams-chat and review the diff\n got: %s", golden, got.String())
			}
		})
	}
	stored, _ := filepath.Glob(filepath.Join(teamsGoldenDir, "*.json"))
	for _, path := range stored {
		switch {
		case seen[filepath.Base(path)]:
		case *updateTeamsGolden:
			if err := os.Remove(path); err != nil {
				t.Fatal(err)
			}
		default:
			t.Errorf("%s has no shared case that lays it out; delete it, or run with -update-teams-chat", path)
		}
	}
}
