package gateway

import (
	"encoding/json"
	"strconv"
	"strings"
	"testing"
)

// slackRichKinds is each element's type, a list's as "list:<style>:<indent>".
func slackRichKinds(elements []map[string]any) []string {
	var kinds []string
	for _, e := range elements {
		kind, _ := e["type"].(string)
		if kind == "rich_text_list" {
			kind += ":" + e["style"].(string) + ":" + strings.Repeat(">", e["indent"].(int))
		}
		kinds = append(kinds, kind)
	}
	return kinds
}

// The block forms a report uses are drawn as rich text: a heading as a bold
// line, a fence as preformatted (an item's fence losing the item's indent),
// a list as a list with its nesting, an ordered list keeping its numbers,
// and paragraphs a blank line apart.
func TestSlackRichDocumentDrawsTheBlockForms(t *testing.T) {
	md := "## Root Cause\nThe container runs:\n```yaml\n- tail /dev/zero\n```\nRunning it fills memory.\n\n" +
		"## Evidence\n- The pod is in `CrashLoopBackOff`.\n- The last state:\n  ```json\n  {\"exitCode\":137}\n  ```\n" +
		"- The limit is `64Mi`.\n  - set in the manifest\n\n2. **Raise it**\n3. Fix it\n\n> quoted\n\nDone."
	got := slackRichDocument(md)
	want := []string{
		"rich_text_section", "rich_text_preformatted", "rich_text_section",
		"rich_text_list:bullet:", "rich_text_preformatted", "rich_text_list:bullet:", "rich_text_list:bullet:>",
		"rich_text_list:ordered:", "rich_text_quote", "rich_text_section",
	}
	if kinds := slackRichKinds(got); strings.Join(kinds, " ") != strings.Join(want, " ") {
		t.Fatalf("kinds = %v\nwant    %v", kinds, want)
	}
	heading := got[0]["elements"].([]map[string]any)[0]
	if heading["text"] != "Root Cause" || heading["style"].(map[string]any)["bold"] != true {
		t.Errorf("heading = %v, want a bold line", heading)
	}
	if code := got[1]["elements"].([]map[string]any)[0]["text"]; code != "- tail /dev/zero" {
		t.Errorf("fence = %q", code)
	}
	if code := got[4]["elements"].([]map[string]any)[0]["text"]; code != "{\"exitCode\":137}" {
		t.Errorf("an item's fence = %q, want it without the item's indent", code)
	}
	if got[7]["offset"] != 1 {
		t.Errorf("ordered list = %v, want it to start at 2 (offset 1)", got[7])
	}
	raw, _ := json.Marshal(got[2])
	if !strings.Contains(string(raw), `Running it fills memory.`) || !strings.Contains(string(raw), `\n\n`) ||
		!strings.Contains(string(raw), `"Evidence"`) {
		t.Errorf("section = %s, want the paragraph, a blank line, then the bold heading", raw)
	}
}

// A bold run's own code span draws as code, kept bold, rather than showing
// its backticks: a clause cut leaves the rest of a bold first sentence bold.
func TestSlackRichMarkdownCodeInsideBold(t *testing.T) {
	got, _ := json.Marshal(slackRichMarkdown("**Because it runs `tail /dev/zero`.** Done."))
	want := `[{"style":{"bold":true},"text":"Because it runs ","type":"text"},` +
		`{"style":{"bold":true,"code":true},"text":"tail /dev/zero","type":"text"},` +
		`{"style":{"bold":true},"text":".","type":"text"},{"text":" Done.","type":"text"}]`
	if string(got) != want {
		t.Fatalf("got  %s\nwant %s", got, want)
	}
}

// A table is a preformatted block with its columns lined up, and a rule goes.
func TestSlackRichDocumentTableAndRule(t *testing.T) {
	got := slackRichDocument("| a | bb |\n|---|---|\n| ccc | d |\n\n---\n\nend")
	if kinds := slackRichKinds(got); strings.Join(kinds, " ") != "rich_text_preformatted rich_text_section" {
		t.Fatalf("kinds = %v", kinds)
	}
	if code := got[0]["elements"].([]map[string]any)[0]["text"]; code != "a   | bb\n----+---\nccc | d" {
		t.Errorf("table = %q", code)
	}
}

// A spaced rule goes rather than reading as a list item, and a setext
// underline bolds the line above it, as on the text path.
func TestSlackRichDocumentSpacedRuleAndSetext(t *testing.T) {
	for _, rule := range []string{"* * *", "- - -", "___"} {
		got := slackRichDocument("one\n\n" + rule + "\n\ntwo")
		if kinds := slackRichKinds(got); strings.Join(kinds, " ") != "rich_text_section" {
			t.Errorf("%q: kinds = %v, want the rule dropped", rule, kinds)
		}
	}
	for _, underline := range []string{"---", "==="} {
		got := slackRichDocument("Root cause\n" + underline + "\nIt ran out of memory.")
		first := got[0]["elements"].([]map[string]any)[0]
		if first["text"] != "Root cause" || first["style"] == nil || first["style"].(map[string]any)["bold"] != true {
			t.Errorf("%q: first = %v, want the line above bold", underline, first)
		}
		raw, _ := json.Marshal(got)
		if strings.Contains(string(raw), underline) {
			t.Errorf("%q: the underline is drawn: %s", underline, raw)
		}
	}
}

// A fold that is one long numbered list is cut between its items, each part
// under the share and numbered on from the last.
func TestSlackRichLongListIsCut(t *testing.T) {
	var md strings.Builder
	for i := 1; i <= 15; i++ {
		md.WriteString(strconv.Itoa(i) + ". " + strings.Repeat("finding text ", 20) + "\n")
	}
	blocks := slackRichBlocks(md.String(), slackChatFoldChunk)
	if len(blocks) < 2 {
		t.Fatalf("blocks = %d, want the list cut", len(blocks))
	}
	next := 0
	for _, b := range blocks {
		if n := slackRichLen(b); n > slackChatFoldChunk {
			t.Errorf("a block of %d runes, over the share", n)
		}
		for _, list := range b["elements"].([]map[string]any) {
			offset, _ := list["offset"].(int)
			if offset != next {
				t.Errorf("a part starts at offset %d, want %d", offset, next)
			}
			next += len(list["elements"].([]map[string]any))
		}
	}
	if next != 15 {
		t.Fatalf("the parts hold %d items, want 15", next)
	}
}

// A fence that never closes runs to the end, as CommonMark reads it.
func TestSlackRichDocumentUnclosedFence(t *testing.T) {
	got := slackRichDocument("look:\n```\nkubectl get pods")
	if kinds := slackRichKinds(got); strings.Join(kinds, " ") != "rich_text_section rich_text_preformatted" {
		t.Fatalf("kinds = %v", kinds)
	}
}
