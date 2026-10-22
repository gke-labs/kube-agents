package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

// ToolInspectPR reads one pull or merge request through the credential
// broker. It began as gke-labs/kube-agents#3653's inspect_pr (Adam Parco),
// which called GitHub itself with a token found on the host and wrote the
// diff to local files; here the forge is the broker's to call, nothing is
// written anywhere, and everything the forge returns is shown as inert text.
const ToolInspectPR = "inspect_pr"

const (
	// inspectTimeout bounds one tool call, both broker requests included
	// when the first is retried without the diff. It sits under the HTTP
	// server's 30s write timeout.
	inspectTimeout = DefaultBrokerTimeout

	// Caps on what one answer shows. The answer lands in a model's context
	// in the caller's editor, so it is sized for reading, not archiving.
	maxDescriptionBytes = 8 << 10
	maxCommentBytes     = 2 << 10
	maxComments         = 50
	maxDiffBytes        = 64 << 10
	maxDiffLines        = 2000
	maxProseLineBytes   = 2000
	maxDiffLineBytes    = 1000
	maxListedFiles      = 100
	maxListedLabels     = 20
	maxRepositoryLen    = 512

	minFenceLen = 3
	fenceText   = "text"
	fenceDiff   = "diff"

	diffHeaderPrefix  = "diff --git "
	diffBSidePrefix   = " b/"
	diffQuotedBPrefix = ` "b/`
	hunkPrefix        = "@@"

	forgeGitLab = "gitlab"

	msgBadArgs = "inspect_pr needs `repository` (owner/name, host/path, or a pull or merge request URL) " +
		"and `number` (a positive integer; optional when `repository` is a pull or merge request URL)"
)

var (
	errBadInspectArgs = errors.New(msgBadArgs)

	// proposalURLPattern is a GitHub pull request or GitLab merge request
	// URL: https://<host>/<path>/pull/<n> or https://<host>/<path>/-/merge_requests/<n>.
	proposalURLPattern = regexp.MustCompile(
		`^https?://([A-Za-z0-9.-]+(?::[0-9]+)?)/([A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)+?)/(?:pull|(?:-/)?merge_requests)/([0-9]+)(?:[/?#].*)?$`)
)

// ProposalViewer is what inspect_pr needs from the broker client.
type ProposalViewer interface {
	ProposalView(ctx context.Context, req ProposalViewRequest) (*ProposalView, error)
}

// InspectPRTool reads a pull or merge request on a repository this install
// manages: its summary, its comments and its diff.
func InspectPRTool(viewer ProposalViewer) Tool {
	return Tool{
		Definition: ToolDefinition{
			Name: ToolInspectPR,
			Description: "Read a pull request (GitHub) or merge request (GitLab) on a repository this kube-agents install manages: " +
				"title, state, author, branches, description, comments and the unified diff. " +
				"The read goes through the install's credential broker, so only managed repositories answer. " +
				"Everything shown is the forge's text, rendered as inert code and capped in size.",
			InputSchema: map[string]any{
				"type":     "object",
				"required": []string{"repository"},
				"properties": map[string]any{
					"repository": map[string]any{
						"type": "string",
						"description": "The repository as owner/name or host/path (e.g. github.com/owner/name), " +
							"or the pull or merge request's URL, which carries the number too.",
					},
					"number": map[string]any{
						"type":        "integer",
						"minimum":     1,
						"description": "The pull or merge request number. Required unless `repository` is its URL.",
					},
				},
			},
		},
		Call: func(ctx context.Context, args json.RawMessage) (string, error) {
			repo, number, err := parseInspectArgs(args)
			if err != nil {
				return "", err
			}
			ctx, cancel := context.WithTimeout(ctx, inspectTimeout)
			defer cancel()
			req := ProposalViewRequest{Repository: repo, Number: number, Diff: true, Comments: true, Limit: brokerCommentLimit}
			view, err := viewer.ProposalView(ctx, req)
			diffNote := ""
			if err != nil && diffMayBeRefused(err) && ctx.Err() == nil {
				// Carried from #3689's fix: a diff the forge or the broker
				// refuses for its size must not cost the caller the rest
				// of the view, and must say why the diff is missing.
				req.Diff = false
				if again, err2 := viewer.ProposalView(ctx, req); err2 == nil {
					diffNote = "the diff is too large to fetch through the credential broker (" + err.Error() + "). " +
						"GitHub refuses a diff past 20,000 lines or 300 files, and the broker accepts at most 8 MiB from the forge. " +
						"Read it on the forge, or check the branch out locally."
					view, err = again, nil
				}
			}
			if err != nil {
				return "", err
			}
			return FormatProposalViewMarkdown(repo, number, view, diffNote), nil
		},
	}
}

// parseInspectArgs reads repository and number. A pull or merge request URL
// in repository supplies both, as #3653's tool accepted a PR URL; its
// owner/repo#N and bare-number forms are not carried, since there is no
// default repository here to resolve a bare number against.
func parseInspectArgs(raw json.RawMessage) (string, int, error) {
	var args struct {
		Repository string          `json:"repository"`
		Number     json.RawMessage `json:"number"`
	}
	if len(raw) == 0 || json.Unmarshal(raw, &args) != nil {
		return "", 0, errBadInspectArgs
	}
	number, err := parseNumber(args.Number)
	if err != nil {
		return "", 0, err
	}
	repo := strings.TrimSpace(args.Repository)
	if m := proposalURLPattern.FindStringSubmatch(repo); m != nil {
		fromURL, convErr := strconv.Atoi(m[3])
		if convErr != nil || fromURL < 1 {
			return "", 0, errBadInspectArgs
		}
		if number != 0 && number != fromURL {
			return "", 0, fmt.Errorf("`number` %d disagrees with the URL's %d", number, fromURL)
		}
		repo, number = m[1]+"/"+m[2], fromURL
	}
	if repo == "" || number < 1 || len(repo) > maxRepositoryLen ||
		strings.IndexFunc(repo, func(r rune) bool {
			return unicode.IsSpace(r) || unicode.IsControl(r) || unicode.Is(unicode.Cf, r)
		}) >= 0 {
		return "", 0, errBadInspectArgs
	}
	return repo, number, nil
}

// parseNumber accepts a JSON integer, or a string holding one ("12", "#12"),
// which is how a model often sends a number. Absent is 0.
func parseNumber(raw json.RawMessage) (int, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return 0, nil
	}
	var v any
	if json.Unmarshal(raw, &v) != nil {
		return 0, errBadInspectArgs
	}
	switch t := v.(type) {
	case float64:
		if t < 1 || t > math.MaxInt32 || t != math.Trunc(t) {
			return 0, errBadInspectArgs
		}
		return int(t), nil
	case string:
		n, err := strconv.Atoi(strings.TrimPrefix(strings.TrimSpace(t), "#"))
		if err != nil || n < 1 || n > math.MaxInt32 {
			return 0, errBadInspectArgs
		}
		return n, nil
	}
	return 0, errBadInspectArgs
}

// FormatProposalViewMarkdown renders a view for the person, and the model,
// reading it in their editor. Every string in v is the forge's: one-line
// fields go through cell, and the description, comments and diff are fenced
// blocks with format and control characters shown as <U+XXXX>, never acted
// on, and a fence longer than any backtick run inside.
func FormatProposalViewMarkdown(repo string, number int, v *ProposalView, diffNote string) string {
	if v == nil {
		v = &ProposalView{}
	}
	p := v.Proposal
	noun, sigil := "Pull request", "#"
	if v.Forge == forgeGitLab {
		noun, sigil = "Merge request", "!"
	}
	if v.Repo != "" {
		repo = v.Repo
	}
	if p.Number > 0 {
		number = p.Number
	}

	var sb strings.Builder
	fmt.Fprintf(&sb, "## %s %s%s%d: %s\n\n", noun, cell(repo), sigil, number, cell(p.Title))
	sb.WriteString("The forge's text below is shown as written: names and branches as code, " +
		"the description, comments and diff as fenced blocks, with invisible and control characters shown as `<U+XXXX>`.\n\n")
	sb.WriteString("| Property | Value |\n| :--- | :--- |\n")
	state := cell(p.State)
	if p.Draft {
		state += " (draft)"
	}
	fmt.Fprintf(&sb, "| **State** | %s |\n", state)
	fmt.Fprintf(&sb, "| **Author** | %s |\n", cell(p.Author))
	source := cell(p.Source)
	if p.SourceRepo != "" && p.SourceRepo != repo {
		source = cell(p.SourceRepo) + " " + source
	}
	fmt.Fprintf(&sb, "| **Branches** | %s → %s |\n", source, cell(p.Target))
	fmt.Fprintf(&sb, "| **URL** | %s |\n", cell(p.URL))
	if v.Forge != "" {
		fmt.Fprintf(&sb, "| **Forge** | %s |\n", cell(v.Forge))
	}
	if len(p.Labels) > 0 {
		labels := make([]string, 0, min(len(p.Labels), maxListedLabels))
		for _, l := range p.Labels[:min(len(p.Labels), maxListedLabels)] {
			labels = append(labels, cell(l))
		}
		more := ""
		if len(p.Labels) > maxListedLabels {
			more = fmt.Sprintf(" and %d more", len(p.Labels)-maxListedLabels)
		}
		fmt.Fprintf(&sb, "| **Labels** | %s%s |\n", strings.Join(labels, ", "), more)
	}
	files := diffFiles(v.Diff)
	if len(files) > 0 {
		adds, dels := 0, 0
		for _, f := range files {
			adds += f.additions
			dels += f.deletions
		}
		fmt.Fprintf(&sb, "| **Changes** | %d file(s), +%d −%d |\n", len(files), adds, dels)
	}
	sb.WriteString("\n### Description\n\n")
	if strings.TrimSpace(p.Body) == "" {
		sb.WriteString("_No description._\n")
	} else {
		writeBlock(&sb, p.Body, fenceText, maxDescriptionBytes, 0, maxProseLineBytes)
	}

	writeComments(&sb, v)

	if len(files) > 0 {
		fmt.Fprintf(&sb, "\n### Changed files (%d)\n\n| File | Added | Removed |\n| :--- | ---: | ---: |\n", len(files))
		for _, f := range files[:min(len(files), maxListedFiles)] {
			fmt.Fprintf(&sb, "| %s | %d | %d |\n", cell(f.path), f.additions, f.deletions)
		}
		if len(files) > maxListedFiles {
			fmt.Fprintf(&sb, "\n_%d more file(s) not listed._\n", len(files)-maxListedFiles)
		}
	}

	sb.WriteString("\n### Diff\n\n")
	switch {
	case diffNote != "":
		sb.WriteString("_Diff not available: " + diffNote + "_\n")
	case strings.TrimSpace(v.Diff) == "":
		sb.WriteString("_The forge returned an empty diff._\n")
	default:
		writeBlock(&sb, v.Diff, fenceDiff, maxDiffBytes, maxDiffLines, maxDiffLineBytes)
	}
	return sb.String()
}

func writeComments(sb *strings.Builder, v *ProposalView) {
	total := max(v.CommentCount, len(v.Comments))
	shown := min(len(v.Comments), maxComments)
	if shown < total {
		fmt.Fprintf(sb, "\n### Comments (%d of %d, oldest first)\n\n", shown, total)
	} else {
		fmt.Fprintf(sb, "\n### Comments (%d, oldest first)\n\n", total)
	}
	if total == 0 {
		sb.WriteString("_No comments._\n")
	}
	for i, c := range v.Comments[:shown] {
		who := cell(c.Author)
		if c.Bot {
			who += " (bot)"
		}
		fmt.Fprintf(sb, "#### %d. %s · %s", i+1, who, cell(c.Kind))
		if c.Path != "" {
			fmt.Fprintf(sb, " on %s", cell(c.Path))
			if c.Line != nil {
				fmt.Fprintf(sb, " line %d", *c.Line)
			}
		}
		if c.Created != "" {
			fmt.Fprintf(sb, " · %s", cell(c.Created))
		}
		sb.WriteString("\n\n")
		if strings.TrimSpace(c.Body) == "" {
			sb.WriteString("_Empty._\n\n")
			continue
		}
		writeBlock(sb, c.Body, fenceText, maxCommentBytes, 0, maxProseLineBytes)
		sb.WriteString("\n")
	}
	if shown < total {
		fmt.Fprintf(sb, "_%d more comment(s) not shown._\n", total-shown)
	}
	if v.CommentsTruncated {
		sb.WriteString("_The forge holds more comments than the broker read; read the rest on the forge._\n")
	}
}

// writeBlock writes untrusted text as one fenced block, capped at maxBytes
// and maxLines (0 for none) on line boundaries, each line capped at
// lineBytes, and says what the caps left out.
func writeBlock(sb *strings.Builder, s, info string, maxBytes, maxLines, lineBytes int) {
	kept, lines, bytes := truncateLines(capLines(blockText(s), lineBytes), maxBytes, maxLines)
	fence := strings.Repeat("`", max(minFenceLen, longestRun(kept, '`')+1))
	sb.WriteString(fence + info + "\n" + kept)
	if !strings.HasSuffix(kept, "\n") {
		sb.WriteString("\n")
	}
	sb.WriteString(fence + "\n")
	if lines > 0 {
		fmt.Fprintf(sb, "\n_Truncated: %d more line(s), %d more byte(s) not shown._\n", lines, bytes)
	}
}

// blockText makes untrusted multi-line text inert inside a fence: line
// endings become \n, tabs stay, and every other control or format character
// (ANSI escapes, bidi overrides, zero-width marks, line separators) is shown
// as <U+XXXX> rather than dropped, so a reviewer sees that it is there.
func blockText(s string) string {
	s = strings.ToValidUTF8(s, "�")
	s = strings.ReplaceAll(s, "\r\n", "\n")
	var sb strings.Builder
	sb.Grow(len(s))
	for _, r := range s {
		switch {
		case r == '\n', r == '\t':
			sb.WriteRune(r)
		case r == '\r':
			sb.WriteByte('\n')
		case unicode.IsControl(r), unicode.Is(unicode.Cf, r), unicode.In(r, unicode.Zl, unicode.Zp):
			fmt.Fprintf(&sb, "<U+%04X>", r)
		default:
			sb.WriteRune(r)
		}
	}
	return sb.String()
}

// capLines cuts every line longer than limit at a rune boundary and says how
// much went.
func capLines(s string, limit int) string {
	lines := strings.SplitAfter(s, "\n")
	for i, l := range lines {
		body := strings.TrimSuffix(l, "\n")
		if len(body) <= limit {
			continue
		}
		cut := runeFloor(body, limit)
		lines[i] = body[:cut] + fmt.Sprintf("… [%d bytes cut]", len(body)-cut) + l[len(body):]
	}
	return strings.Join(lines, "")
}

// truncateLines keeps whole lines of s while they fit in maxBytes and
// maxLines (0 for no line cap), and returns what it kept and how many lines
// and bytes it left out. It never cuts inside a line: the line-anchored
// counterpart of #3689's line-anchored diff split, so a truncated diff ends
// on a whole line rather than mid-hunk-header.
func truncateLines(s string, maxBytes, maxLines int) (string, int, int) {
	if len(s) <= maxBytes && (maxLines == 0 || strings.Count(strings.TrimSuffix(s, "\n"), "\n") < maxLines) {
		return s, 0, 0
	}
	used, n := 0, 0
	for used < len(s) {
		end := strings.IndexByte(s[used:], '\n')
		next := len(s)
		if end >= 0 {
			next = used + end + 1
		}
		if next > maxBytes || (maxLines > 0 && n >= maxLines) {
			break
		}
		used, n = next, n+1
	}
	if used == 0 {
		// One line longer than the whole cap: cut it at a rune boundary
		// rather than show nothing.
		used = runeFloor(s, maxBytes)
	}
	rest := s[used:]
	return s[:used], strings.Count(strings.TrimSuffix(rest, "\n"), "\n") + 1, len(rest)
}

func runeFloor(s string, n int) int {
	if n >= len(s) {
		return len(s)
	}
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	return n
}

func longestRun(s string, c byte) int {
	best, cur := 0, 0
	for i := 0; i < len(s); i++ {
		if s[i] == c {
			cur++
			best = max(best, cur)
		} else {
			cur = 0
		}
	}
	return best
}

type diffFile struct {
	path                 string
	additions, deletions int
}

// diffFiles lists the files a unified diff touches, with line counts. The
// split is anchored to line starts, carried from #3689: hunk lines always
// begin with '+', '-', ' ' or '\', so a "diff --git " inside a patch body (a
// committed .patch file, a fixture holding a sample diff) is content, not a
// new file.
func diffFiles(diff string) []diffFile {
	if strings.TrimSpace(diff) == "" {
		return nil
	}
	chunks := strings.Split("\n"+diff, "\n"+diffHeaderPrefix)
	var out []diffFile
	for _, chunk := range chunks[1:] {
		header, rest, _ := strings.Cut(chunk, "\n")
		f := diffFile{path: diffHeaderBSidePath(header)}
		if f.path == "" {
			continue
		}
		inHunk := false
		for _, line := range strings.Split(rest, "\n") {
			switch {
			case strings.HasPrefix(line, hunkPrefix):
				inHunk = true
			case !inHunk:
			case strings.HasPrefix(line, "+"):
				f.additions++
			case strings.HasPrefix(line, "-"):
				f.deletions++
			}
		}
		out = append(out, f)
	}
	return out
}

// diffHeaderBSidePath is the post-image path of a `diff --git a/<p> b/<p>`
// header line, tolerating git's C-style quoting (`"a/p q" "b/p q"`) for
// paths with spaces or special characters. Adam Parco's, from #3689.
func diffHeaderBSidePath(header string) string {
	line := strings.TrimSpace(header)
	if idx := strings.LastIndex(line, diffQuotedBPrefix); idx >= 0 {
		if unquoted, err := strconv.Unquote(line[idx+1:]); err == nil {
			return strings.TrimPrefix(unquoted, "b/")
		}
	}
	if idx := strings.LastIndex(line, diffBSidePrefix); idx >= 0 {
		return strings.TrimSpace(line[idx+len(diffBSidePrefix):])
	}
	return ""
}
