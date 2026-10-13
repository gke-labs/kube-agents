package chatmsg

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// casesDir holds the shared cases, from this package's directory: each file
// is an input lifted from a today presenter test and the chat messages the
// emitter owes for it (tests/conformance/chat, read by the Python presenters'
// reader too).
const casesDir = "../../../tests/conformance/chat"

// sharedCase is the part of a case file this test reads.
type sharedCase struct {
	Name     string `json:"name"`
	Expected []*struct {
		Carrier Carrier         `json:"carrier"`
		Chat    json.RawMessage `json:"chat"`
	} `json:"expected"`
}

// TestEverySharedCaseIsValid loads every expected chat message in the
// shared cases and checks it passes Validate for its carrier, mark
// included: the emitter must never produce what a renderer would drop.
func TestEverySharedCaseIsValid(t *testing.T) {
	paths, err := filepath.Glob(filepath.Join(casesDir, "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(paths) == 0 {
		t.Fatalf("no shared cases under %s: a glob that matches nothing checks nothing", casesDir)
	}
	for _, path := range paths {
		t.Run(filepath.Base(path), func(t *testing.T) {
			raw, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			var c sharedCase
			if err := json.Unmarshal(raw, &c); err != nil {
				t.Fatal(err)
			}
			outputs := 0
			for i, out := range c.Expected {
				if out == nil {
					continue // the event emits no chat message
				}
				outputs++
				m, err := Parse(out.Chat)
				if err != nil {
					t.Fatalf("expected[%d]: %v", i, err)
				}
				if layout, mark := Validate(m, out.Carrier); layout != nil || mark != nil {
					t.Errorf("expected[%d] on %s: layout %v, mark %v", i, out.Carrier, layout, mark)
				}
			}
			if outputs == 0 {
				t.Errorf("%s expects no chat message at all", c.Name)
			}
		})
	}
}
