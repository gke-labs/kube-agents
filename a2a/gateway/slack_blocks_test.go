package gateway

import (
	"strings"
	"testing"
)

// The line-level markdown Slack cannot show is rewritten into what it can: a
// heading into a bold line, a rule dropped, a bullet into "•", and a table
// into an aligned monospace block. Fenced code is left as written.
func TestRewriteSlackBlocks(t *testing.T) {
	for _, tc := range []struct {
		name, in, want string
	}{
		{"heading", "### Pods", "**Pods**"},
		{"heading with a closing run", "## Nodes ##", "**Nodes**"},
		{"heading already bold", "# **Summary**", "**Summary**"},
		{"not a heading", "#hashtag and #2 issues", "#hashtag and #2 issues"},
		{"dash rule", "above\n---\nbelow", "above\nbelow"},
		{"star rule", "***", ""},
		{"spaced rule", "- - -", ""},
		{"mixed marks are not a rule", "- * -", "• * -"},
		{"bullets", "* one\n- two\n+ three", "• one\n• two\n• three"},
		{"nested bullet keeps its indent", "- top\n  - sub", "• top\n  • sub"},
		{"bold at line start is not a bullet", "**Done** today", "**Done** today"},
		{"numbered list unchanged", "1. first\n2. second", "1. first\n2. second"},
		{"fenced code untouched", "```\n# not a heading\n- not a bullet\n---\n```", "```\n# not a heading\n- not a bullet\n---\n```"},
		{
			"table",
			"| Name | Status |\n|:---|---:|\n| web-7 | **Running** |\n| db | `CrashLoop` |",
			"```\nName  | Status\n------+----------\nweb-7 | Running\ndb    | CrashLoop\n```",
		},
		{
			"table without outer pipes, then prose",
			"pod | ready\n--- | ---\na | 1/1\nafter the table",
			"```\npod | ready\n----+------\na   | 1/1\n```\nafter the table",
		},
		{
			"escaped pipe stays in its cell",
			"| expr |\n|---|\n| a \\| b |",
			"```\nexpr\n-----\na | b\n```",
		},
		{"a line with a pipe and no delimiter row is prose", "use a | b in the shell", "use a | b in the shell"},
		{"a heading glued to a sentence gets its own line", "summarizing its health status.# GKE Cluster Health Report", "summarizing its health status.\n\n**GKE Cluster Health Report**"},
		{"a hash after a word is not a heading", "see issue #2 and C# code", "see issue #2 and C# code"},
		{"inline code keeps its hashes", "run `echo done.# not` now", "run `echo done.# not` now"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := rewriteSlackBlocks(tc.in); got != tc.want {
				t.Errorf("rewriteSlackBlocks(%q) =\n%s\nwant\n%s", tc.in, got, tc.want)
			}
		})
	}
}

// Through toMrkdwn: the heading ends as Slack bold, the table as a code block
// whose cells are escaped like everything else (Slack decodes them back), and
// the report vamp-49's run showed raw renders.
func TestToMrkdwnRendersAMarkdownReport(t *testing.T) {
	in := "# GKE Cluster Health Report\n\n---\n\n## Nodes\n\n* 3 nodes, all **Ready**\n\n| Node | CPU |\n|:---|---:|\n| a<b | 12% |"
	got := toMrkdwn(in)
	for _, want := range []string{"*GKE Cluster Health Report*", "*Nodes*", "• 3 nodes, all *Ready*", "```\nNode | CPU\n-----+----\na&lt;b  | 12%\n```"} {
		if !strings.Contains(got, want) {
			t.Errorf("toMrkdwn output lacks %q:\n%s", want, got)
		}
	}
	for _, raw := range []string{"# ", "\n---\n", "|:---|", "* 3"} {
		if strings.Contains(got, raw) {
			t.Errorf("toMrkdwn output still carries raw %q:\n%s", raw, got)
		}
	}
}
