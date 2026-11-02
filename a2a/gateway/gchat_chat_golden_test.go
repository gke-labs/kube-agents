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

// The Google Chat renderer's golden files: every chat message the shared cases
// (tests/conformance/chat, lifted from the today presenters' tests) expect
// an emitter to send, laid out as Chat text with accessory buttons, against
// a stored file per case under testdata/gchat-chat. `go test ./gateway -run GchatChatGolden
// -update-gchat-chat` rewrites them, and deletes one whose case no longer
// lays anything out, so run it over the whole test, not one subtest; a diff
// in review is a change to what Google Chat shows.
const gchatGoldenDir = "testdata/gchat-chat"

var updateGchatGolden = flag.Bool("update-gchat-chat", false, "rewrite the Google Chat renderer's golden files")

// gchatGoldenLayout is one rendered message in a golden file.
type gchatGoldenLayout struct {
	Expected int              `json:"expected"`
	Carrier  chatmsg.Carrier  `json:"carrier"`
	Text     string           `json:"text"`
	Widgets  []map[string]any `json:"widgets"`
}

func TestGchatChatGolden(t *testing.T) {
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
			var layouts []gchatGoldenLayout
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
				layout, err := renderGchatChat(&m)
				if err != nil {
					t.Fatalf("expected[%d]: %v", i, err)
				}
				layouts = append(layouts, gchatGoldenLayout{Expected: i, Carrier: out.Carrier, Text: layout.Text, Widgets: layout.Widgets})
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
			golden := filepath.Join(gchatGoldenDir, name+".json")
			if *updateGchatGolden {
				if err := os.MkdirAll(gchatGoldenDir, 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(golden, got.Bytes(), 0o644); err != nil {
					t.Fatal(err)
				}
				return
			}
			want, err := os.ReadFile(golden)
			if err != nil {
				t.Fatalf("%v (run with -update-gchat-chat to write it)", err)
			}
			if !bytes.Equal(got.Bytes(), want) {
				t.Errorf("the layout differs from %s; run with -update-gchat-chat and review the diff\n got: %s", golden, got.String())
			}
		})
	}
	// A golden file whose case is gone, or no longer lays anything out, is
	// stale, and a reviewer reading it would read a layout nothing makes.
	stored, _ := filepath.Glob(filepath.Join(gchatGoldenDir, "*.json"))
	for _, path := range stored {
		switch {
		case seen[filepath.Base(path)]:
		case *updateGchatGolden:
			if err := os.Remove(path); err != nil {
				t.Fatal(err)
			}
		default:
			t.Errorf("%s has no shared case that lays it out; delete it, or run with -update-gchat-chat", path)
		}
	}
}
