package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"testing"
)

// fakeViewer answers with fn and records what it was asked.
type fakeViewer struct {
	fn   func(ProposalViewRequest) (*ProposalView, error)
	reqs []ProposalViewRequest
}

func (f *fakeViewer) ProposalView(_ context.Context, req ProposalViewRequest) (*ProposalView, error) {
	f.reqs = append(f.reqs, req)
	return f.fn(req)
}

func viewOf(t *testing.T, raw string) *ProposalView {
	t.Helper()
	var v ProposalView
	if err := json.Unmarshal([]byte(raw), &v); err != nil {
		t.Fatal(err)
	}
	return &v
}

func callInspect(t *testing.T, viewer ProposalViewer, args string) (string, error) {
	t.Helper()
	return InspectPRTool(viewer).Call(context.Background(), json.RawMessage(args))
}

// End to end through the real client and a stand-in broker.
func TestInspectPRRendersAView(t *testing.T) {
	for _, tc := range []struct {
		name, body string
		want       []string
	}{
		{"github", githubViewJSON, []string{
			"## Pull request `acme/widgets`#12: `Fix the thing`",
			"| **State** | `merged` |", "| **Author** | `octocat` |", "| **Branches** | `fix-thing` → `main` |",
			"| **URL** | `https://github.com/acme/widgets/pull/12` |", "| **Labels** | `bug` |",
			"| **Changes** | 1 file(s), +1 −1 |",
			"```text\nFixes the thing.\n```",
			"### Comments (2, oldest first)", "`kube-agents-bot` (bot) · `review_comment` on `main.go` line 7",
			"| `main.go` | 1 | 1 |",
			"```diff\ndiff --git a/main.go b/main.go\n",
		}},
		{"gitlab", gitlabViewJSON, []string{
			"## Merge request `group/sub/proj`!7: `Add a widget`",
			"| **State** | `open` (draft) |", "| **Forge** | `gitlab` |", "_No description._",
			"`maintainer` · `review_comment` on `widget.py` line 3",
			"The forge holds more comments than the broker read",
			"| `widget.py` | 2 | 0 |",
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv, seen := fakeBroker(t, answer(http.StatusOK, tc.body))
			c := newTestClient(t, srv.URL, writeToken(t, testToken), nil)
			out, err := callInspect(t, c, `{"repository":"acme/widgets","number":12}`)
			if err != nil {
				t.Fatal(err)
			}
			for _, w := range tc.want {
				if !strings.Contains(out, w) {
					t.Errorf("output lacks %q:\n%s", w, out)
				}
			}
			if r := seen(); len(r) != 1 || r[0].body != testViewRequest {
				t.Errorf("requests = %+v", r)
			}
		})
	}
}

func TestInspectPRArguments(t *testing.T) {
	ok := func(ProposalViewRequest) (*ProposalView, error) { return &ProposalView{}, nil }
	for _, tc := range []struct {
		name, args string
		repo       string
		number     int
	}{
		{"owner/name", `{"repository":"acme/widgets","number":12}`, "acme/widgets", 12},
		{"host/path", `{"repository":"gitlab.example.com/group/sub/proj","number":7}`, "gitlab.example.com/group/sub/proj", 7},
		{"a number as a string", `{"repository":"acme/widgets","number":"#12"}`, "acme/widgets", 12},
		{"a GitHub PR URL", `{"repository":"https://github.com/acme/widgets/pull/12/files"}`, "github.com/acme/widgets", 12},
		{"a GitLab MR URL", `{"repository":"https://gitlab.example.com/group/sub/proj/-/merge_requests/7"}`, "gitlab.example.com/group/sub/proj", 7},
		{"a URL and its own number", `{"repository":"https://github.com/acme/widgets/pull/12","number":12}`, "github.com/acme/widgets", 12},
		{"extra fields are ignored", `{"repository":"acme/widgets","number":3,"output_dir":"/tmp"}`, "acme/widgets", 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeViewer{fn: ok}
			if _, err := callInspect(t, f, tc.args); err != nil {
				t.Fatal(err)
			}
			if len(f.reqs) != 1 || f.reqs[0].Repository != tc.repo || f.reqs[0].Number != tc.number ||
				!f.reqs[0].Diff || !f.reqs[0].Comments {
				t.Errorf("requests = %+v, want %s #%d with diff and comments", f.reqs, tc.repo, tc.number)
			}
		})
	}
	for name, args := range map[string]string{
		"nothing":                   ``,
		"not an object":             `[1]`,
		"no repository":             `{"number":12}`,
		"no number":                 `{"repository":"acme/widgets"}`,
		"a zero number":             `{"repository":"acme/widgets","number":0}`,
		"a negative number":         `{"repository":"acme/widgets","number":-3}`,
		"a fractional number":       `{"repository":"acme/widgets","number":1.5}`,
		"a word for a number":       `{"repository":"acme/widgets","number":"twelve"}`,
		"a boolean number":          `{"repository":"acme/widgets","number":true}`,
		"whitespace in repository":  `{"repository":"acme/wid gets","number":1}`,
		"a newline in repository":   `{"repository":"acme/\nwidgets","number":1}`,
		"a bidi mark in repository": `{"repository":"acme/\u202ewidgets","number":1}`,
		"a huge repository":         `{"repository":"` + strings.Repeat("a", maxRepositoryLen+1) + `","number":1}`,
		"a URL that disagrees":      `{"repository":"https://github.com/acme/widgets/pull/12","number":13}`,
	} {
		t.Run(name, func(t *testing.T) {
			f := &fakeViewer{fn: ok}
			if _, err := callInspect(t, f, args); err == nil {
				t.Errorf("accepted %s", args)
			}
			if len(f.reqs) != 0 {
				t.Errorf("asked the broker for %+v", f.reqs)
			}
		})
	}
}

// Text the forge returns is written by whoever opened or commented on the
// proposal and read by a model in the caller's editor. None of it may close
// its fence, become markdown, carry an escape sequence to a terminal, or hide
// behind a bidi override or a zero-width mark.
func TestInspectPRRendersHostileTextInert(t *testing.T) {
	hostile := "ok\x1b[31mRED\x1b[0m \u202eevil\u202c zero\u200bwidth line\u2028sep \u00a0\r\nCR"
	v := &ProposalView{
		Forge: "github",
		Repo:  "acme/widgets",
		Proposal: Proposal{
			Number: 12,
			Title:  "a `title` | with [a link](https://evil) \n## and a heading \u202e",
			Author: "mallory`|",
			State:  "open",
			Source: "br`anch", Target: "main\n|x|",
			URL:    "javascript:alert(1)`",
			Labels: []string{"`l`|"},
			Body:   "before\n```\n## escaped heading\n```````\n" + hostile + "\n````",
		},
		Comments: []ProposalComment{{Kind: "issue`", Author: "eve\u200b", Path: "p`|ath", Body: "````\n![img](https://evil/x.png)\n" + hostile}},
		Diff: "diff --git a/we`ird|name.go b/we`ird|name.go\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n" +
			"-```````````\n+" + hostile + "\n",
	}
	out := FormatProposalViewMarkdown("acme/widgets", 12, v, "")
	for _, bad := range []string{"\x1b", "\u202e", "\u202c", "\u200b", "\u2028", "\r", "\n## escaped heading\n```\n"} {
		if strings.Contains(out, bad) {
			t.Errorf("output carries %q:\n%s", bad, out)
		}
	}
	for _, want := range []string{"<U+001B>[31mRED", "<U+202E>evil<U+202C>", "zero<U+200B>width", "line<U+2028>sep"} {
		if !strings.Contains(out, want) {
			t.Errorf("output does not show %q", want)
		}
	}
	// Every one-line field is a single code span on its own line.
	if first := strings.SplitN(out, "\n", 2)[0]; first != "## Pull request `acme/widgets`#12: `a 'title' ¦ with [a link](https://evil) ## and a heading`" {
		t.Errorf("heading line %q", first)
	}
	for _, want := range []string{"| **Author** | `mallory'¦` |", "| **Branches** | `br'anch` → `main ¦x¦` |", "| **URL** | `javascript:alert(1)'` |", "| `we'ird¦name.go` |"} {
		if !strings.Contains(out, want) {
			t.Errorf("output lacks %q", want)
		}
	}
	for _, line := range strings.Split(out, "\n") {
		if strings.HasPrefix(line, "## and") || strings.HasPrefix(line, "|x|") {
			t.Errorf("a field broke onto its own line: %q", line)
		}
	}
	assertFencesHold(t, out)
}

// assertFencesHold walks the fenced blocks in out and checks that each is
// closed by its own fence and that nothing inside could have closed it early.
func assertFencesHold(t *testing.T, out string) {
	t.Helper()
	lines := strings.Split(out, "\n")
	blocks := 0
	for i := 0; i < len(lines); i++ {
		n := longestRun(lines[i], '`')
		if n < minFenceLen || !strings.HasPrefix(lines[i], strings.Repeat("`", n)) {
			continue
		}
		fence := strings.Repeat("`", n)
		closed := false
		for j := i + 1; j < len(lines); j++ {
			if lines[j] == fence {
				i, closed = j, true
				break
			}
			if longestRun(lines[j], '`') >= n {
				t.Errorf("line %q inside a %d-backtick fence could close it", lines[j], n)
			}
		}
		if !closed {
			t.Errorf("fence at line %d never closes", i)
		}
		blocks++
	}
	if blocks == 0 {
		t.Error("no fenced blocks found")
	}
}

func TestInspectPRTruncatesAHugeDiffOnLineBoundaries(t *testing.T) {
	var sb strings.Builder
	sb.WriteString("diff --git a/big.txt b/big.txt\n--- a/big.txt\n+++ b/big.txt\n@@ -0,0 +1,20000 @@\n")
	for i := range 20000 {
		fmt.Fprintf(&sb, "+line %05d of the big file\n", i)
	}
	diff := sb.String()
	out := FormatProposalViewMarkdown("acme/widgets", 1, &ProposalView{Diff: diff}, "")
	if len(out) > maxDiffBytes+16<<10 {
		t.Errorf("output is %d bytes", len(out))
	}
	block := out[strings.Index(out, "```diff\n")+len("```diff\n"):]
	block = block[:strings.Index(block, "\n```\n")]
	kept := strings.Split(block, "\n")
	if len(kept) > maxDiffLines {
		t.Errorf("%d lines kept, cap %d", len(kept), maxDiffLines)
	}
	last := kept[len(kept)-1]
	if !strings.HasSuffix(last, " of the big file") {
		t.Errorf("the kept diff ends mid-line: %q", last)
	}
	total := strings.Count(diff, "\n")
	want := fmt.Sprintf("_Truncated: %d more line(s), %d more byte(s) not shown._", total-len(kept), len(diff)-len(block)-1)
	if !strings.Contains(out, want) {
		t.Errorf("output lacks %q", want)
	}
	if !strings.Contains(out, "| **Changes** | 1 file(s), +20000 −0 |") {
		t.Error("the change count is not of the whole diff")
	}
}

func TestTruncateLines(t *testing.T) {
	for _, tc := range []struct {
		name, in         string
		maxBytes, maxLns int
		kept             string
		lines, bytes     int
	}{
		{"fits", "a\nb\n", 10, 0, "a\nb\n", 0, 0},
		{"cut at the last whole line under the byte cap", "aaa\nbbb\nccc\n", 9, 0, "aaa\nbbb\n", 1, 4},
		{"never inside a line", "aaa\nbbbbbbbb\n", 6, 0, "aaa\n", 1, 9},
		{"the line cap", "a\nb\nc\nd", 100, 2, "a\nb\n", 2, 3},
		{"one line longer than the cap is cut at a rune boundary", "ééééé", 5, 0, "éé", 1, 6},
	} {
		t.Run(tc.name, func(t *testing.T) {
			kept, lines, bytes := truncateLines(tc.in, tc.maxBytes, tc.maxLns)
			if kept != tc.kept || lines != tc.lines || bytes != tc.bytes {
				t.Errorf("= %q, %d, %d; want %q, %d, %d", kept, lines, bytes, tc.kept, tc.lines, tc.bytes)
			}
		})
	}
}

func TestLongLinesAndBodiesAreCapped(t *testing.T) {
	v := &ProposalView{
		Proposal: Proposal{Body: strings.Repeat("word ", 10000)},
		Comments: make([]ProposalComment, maxComments+5),
		Diff:     "diff --git a/min.js b/min.js\n@@ -1 +1 @@\n+" + strings.Repeat("x", 50000) + "\n",
	}
	for i := range v.Comments {
		v.Comments[i] = ProposalComment{Author: "a", Kind: "issue", Body: strings.Repeat("c", 10000)}
	}
	out := FormatProposalViewMarkdown("acme/widgets", 1, v, "")
	for _, want := range []string{
		fmt.Sprintf("… [%d bytes cut]", 50001-maxDiffLineBytes),
		fmt.Sprintf("### Comments (%d of %d, oldest first)", maxComments, maxComments+5),
		"_5 more comment(s) not shown._",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("output lacks %q", want)
		}
	}
	if strings.Contains(out, strings.Repeat("c", maxProseLineBytes+1)) || strings.Contains(out, strings.Repeat("x", maxDiffLineBytes+1)) {
		t.Error("a line past its cap was shown whole")
	}
	if len(out) > maxDescriptionBytes+maxComments*(maxCommentBytes+200)+maxDiffBytes {
		t.Errorf("output is %d bytes", len(out))
	}
}

// Carried from #3689: a diff refused for its size costs the caller the diff,
// with the reason, and not the rest of the view.
func TestInspectPRAsksAgainWithoutATooLargeDiff(t *testing.T) {
	for name, refusal := range map[string]error{
		"the broker's cap":  &BrokerError{Status: http.StatusBadGateway, Code: brokerCodeTooLarge},
		"GitHub's 406":      &BrokerError{Status: http.StatusBadGateway, Code: brokerCodeFailed},
		"this client's cap": fmt.Errorf("%w (16 bytes)", ErrBrokerResponseTooLarge),
	} {
		t.Run(name, func(t *testing.T) {
			f := &fakeViewer{fn: func(req ProposalViewRequest) (*ProposalView, error) {
				if req.Diff {
					return nil, refusal
				}
				return viewOf(t, githubViewJSON), nil
			}}
			out, err := callInspect(t, f, `{"repository":"acme/widgets","number":12}`)
			if err != nil {
				t.Fatal(err)
			}
			if len(f.reqs) != 2 || f.reqs[1].Diff || !f.reqs[1].Comments {
				t.Errorf("requests = %+v", f.reqs)
			}
			for _, want := range []string{"`Fix the thing`", "_Diff not available: the diff is too large", "Looks good."} {
				if !strings.Contains(out, want) {
					t.Errorf("output lacks %q", want)
				}
			}
		})
	}
}

func TestInspectPRDoesNotRetryOtherRefusals(t *testing.T) {
	for name, refusal := range map[string]error{
		"not managed": &BrokerError{Status: http.StatusForbidden, Code: "REPOSITORY_NOT_MANAGED"},
		"not found":   &BrokerError{Status: http.StatusNotFound, Code: "FORGE_NOT_FOUND"},
		"down":        errors.New("the credential broker did not answer: dial tcp: refused"),
	} {
		t.Run(name, func(t *testing.T) {
			f := &fakeViewer{fn: func(ProposalViewRequest) (*ProposalView, error) { return nil, refusal }}
			if _, err := callInspect(t, f, `{"repository":"acme/widgets","number":12}`); !errors.Is(err, refusal) {
				t.Errorf("err = %v, want %v", err, refusal)
			}
			if len(f.reqs) != 1 {
				t.Errorf("%d requests, want 1", len(f.reqs))
			}
		})
	}
	// A retry that fails too reports the first failure, which is the one
	// that says what happened to the whole view.
	first := &BrokerError{Status: http.StatusBadGateway, Code: brokerCodeFailed}
	f := &fakeViewer{fn: func(req ProposalViewRequest) (*ProposalView, error) {
		if req.Diff {
			return nil, first
		}
		return nil, &BrokerError{Status: http.StatusBadGateway}
	}}
	if _, err := callInspect(t, f, `{"repository":"acme/widgets","number":12}`); !errors.Is(err, first) || len(f.reqs) != 2 {
		t.Errorf("err = %v after %d requests", err, len(f.reqs))
	}
}

// A refusal reaches the caller as an isError result naming the tool.
func TestInspectPRErrorsAreToolErrors(t *testing.T) {
	srv, _ := fakeBroker(t, answer(http.StatusForbidden, `{"error":"x","code":"REPOSITORY_NOT_MANAGED"}`))
	s, err := NewServer(InspectPRTool(newTestClient(t, srv.URL, writeToken(t, testToken), nil)))
	if err != nil {
		t.Fatal(err)
	}
	res := s.CallTool(context.Background(), ToolInspectPR, json.RawMessage(`{"repository":"acme/widgets","number":1}`))
	if !res.IsError || !strings.Contains(res.Content[0].Text, "**Error in `inspect_pr`**") ||
		!strings.Contains(res.Content[0].Text, "does not manage that repository") {
		t.Errorf("result = %+v", res)
	}
}

// The split is anchored to line starts (#3689): a "diff --git " inside a hunk
// is content, and a quoted path is unquoted.
func TestDiffFilesSplitsOnLineStartsOnly(t *testing.T) {
	diff := "diff --git a/fixture.patch b/fixture.patch\n--- a/fixture.patch\n+++ b/fixture.patch\n@@ -0,0 +1,3 @@\n" +
		"+diff --git a/inner.go b/inner.go\n+-x\n+ text diff --git a/no b/no\n" +
		"diff --git \"a/with space.go\" \"b/with space.go\"\n--- \"a/with space.go\"\n+++ \"b/with space.go\"\n@@ -1 +1 @@\n-a\n+b\n"
	got := diffFiles(diff)
	want := []diffFile{{"fixture.patch", 3, 0}, {"with space.go", 1, 1}}
	if len(got) != len(want) || got[0] != want[0] || got[1] != want[1] {
		t.Errorf("diffFiles = %+v, want %+v", got, want)
	}
}
