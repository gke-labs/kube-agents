package chatmsg

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// lintCases is the chat UX lint's shared cases, from this package's
// directory; agents/platform/scripts/test_chat_cases.py reads them too.
const lintCases = "../../../tests/conformance/chat-ux/lint.json"

// TestLintSharedCases checks every shared lint case: the message passes
// Validate, so the lint is the only thing that can object to it, and Lint
// reports exactly the rules the case lists, in order.
func TestLintSharedCases(t *testing.T) {
	raw, err := os.ReadFile(lintCases)
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Cases []struct {
			Name     string          `json:"name"`
			Chat     json.RawMessage `json:"chat"`
			Findings []string        `json:"findings"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	if len(file.Cases) == 0 {
		t.Fatalf("no cases in %s", lintCases)
	}
	for _, c := range file.Cases {
		t.Run(c.Name, func(t *testing.T) {
			m, err := Parse(c.Chat)
			if err != nil {
				t.Fatal(err)
			}
			if layout, mark := Validate(m, Result); layout != nil || mark != nil {
				t.Fatalf("not a valid message: layout %v, mark %v", layout, mark)
			}
			got := []string{}
			for _, f := range Lint(m) {
				got = append(got, f.Rule)
			}
			want := c.Findings
			if want == nil {
				want = []string{}
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("Lint = %v, want %v", got, want)
			}
		})
	}
}

func TestLintNamesTheField(t *testing.T) {
	m := Message{Schema: Schema, Headline: "Done.", Choices: []Choice{{Text: "Roll back"}, {Text: "/approve"}}}
	got := Lint(m)
	if len(got) != 1 || got[0].Field != "choices[1].text" || got[0].Rule != LintChoiceRawCommand {
		t.Fatalf("Lint = %v, want one %s on choices[1].text", got, LintChoiceRawCommand)
	}
}
