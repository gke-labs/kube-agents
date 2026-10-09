package gateway

import (
	"regexp"
	"strings"
	"unicode/utf8"
)

// Slack's mrkdwn has no headings, rules, list markers or tables, and an
// executor's markdown answer uses all four, so in a Slack thread they showed
// raw: "### Pods", "---", "* item", and a table's pipes and ":---" row. This
// pass rewrites those line-level forms before toMrkdwn escapes the text and
// translates bold and links: a heading becomes a bold line, a rule goes, a
// bullet becomes "•", and a table becomes a monospace block whose columns
// line up. A line any part of which sits in a fenced block (mdCodeSpanRE,
// so a fence opened mid-line counts) is left as written. It runs on the raw
// text, before escaping, so a table's column widths are measured on what
// Slack will display (Slack decodes the escaped &, < and > back). Widths are
// counted in runes, so a cell holding East Asian wide characters or emoji
// pads short. The relay chunks a long answer before this runs and each
// chunk is rewritten alone, so a table the chunker cuts renders as a block
// up to the cut and raw rows after it.
var (
	// slackHeadingRE is an ATX heading: up to three spaces, one to six #,
	// whitespace, the text, and an optional closing run of # set off by
	// whitespace (so "## Using C#" keeps its #).
	slackHeadingRE = regexp.MustCompile(`^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$`)
	// slackNotParagraphRE is a line that falls through the rewrites but is
	// not a paragraph a setext underline could make a heading: indented (a
	// list item's continuation), an ordered-list item, or a blockquote line.
	slackNotParagraphRE = regexp.MustCompile(`^(?:[ \t]|\d+[.)][ \t]|>)`)
	// slackTildeFenceRE opens or closes a tilde-fenced code block, which
	// mdCodeSpanRE (backticks only) does not see.
	slackTildeFenceRE = regexp.MustCompile(`^ {0,3}~~~`)
	// slackSetextRE is a setext heading's underline: a run of = or of -
	// with no spaces inside it, under a paragraph line.
	slackSetextRE = regexp.MustCompile(`^ {0,3}(?:=+|-+)[ \t]*$`)
	// slackRuleRE is a thematic break: three or more of one of - * _, with
	// optional spaces between them, so "--" or a stray "**" line stays.
	slackRuleRE = regexp.MustCompile(`^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$`)
	// slackBulletRE is an unordered list item: indentation, one of - * +,
	// whitespace, the item.
	slackBulletRE = regexp.MustCompile(`^([ \t]*)[-*+][ \t]+(.*)$`)
	// slackTableSepRE is a table's delimiter row: cells of dashes with
	// optional alignment colons, separated by pipes, outer pipes optional.
	// The row must also carry a pipe (slackTablePipe), as GFM requires, so a
	// bare "---" under a line that happens to hold a pipe stays a rule.
	slackTableSepRE = regexp.MustCompile(`^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$`)
	// slackGluedHeadingRE is a heading run onto the end of a sentence with
	// no line break ("…its health status.# Cluster Report"), as a model's
	// answer can arrive: a sentence's closing . or !, one to six #, a space,
	// and a letter or digit. ":" and "?" are left out because a shell prompt
	// ("root@node:# cmd") and a URL fragment ("faq?# x") carry them.
	slackGluedHeadingRE = regexp.MustCompile(`([.!])(#{1,6} )([\p{L}\p{N}])`)
)

const (
	// slackBullet replaces a list item's marker.
	slackBullet = "• "
	// mdBoldMark wraps a heading's text as markdown bold, which toMrkdwn's
	// bold pass then turns into Slack's, and is the bold mark a table cell
	// sheds outside its code spans.
	mdBoldMark = "**"
	// mdEmphasisStar is the single star a heading must not start or end on
	// to be wrapped as bold.
	mdEmphasisStar = "*"
	// mdCodeTick is the backtick that delimits a code span, which a table
	// cell sheds (its content stays) since the whole table is a code block.
	mdCodeTick = "`"
	// slackGluedHeadingSplit puts a glued heading on its own paragraph.
	slackGluedHeadingSplit = "$1\n\n$2$3"
	// slackLineCR is the carriage return a CRLF answer ends each line with,
	// dropped so the line patterns' $ anchors see the end of the line.
	slackLineCR = "\r"
	// slackTableColSep separates a rendered table's columns, slackTablePad
	// pads a cell to its column's width, and slackTableRuleChar draws the
	// line under its header, with slackTableRuleSep where columns meet.
	slackTableColSep   = " | "
	slackTablePad      = " "
	slackTableRuleChar = "-"
	slackTableRuleSep  = "-+-"
	// slackTablePipe is the cell separator in the markdown being read, and
	// slackEscapedPipe the escaped one a cell may carry, held in
	// slackPipePlaceholder while the row is split.
	slackTablePipe       = "|"
	slackEscapedPipe     = `\|`
	slackPipePlaceholder = "\x00"
	// slackCellLinkForm sets a link's URL after its label in a table cell
	// (slackLinkRE's groups): inside the code block a table becomes, Slack
	// renders no link, so the destination is shown rather than lost.
	slackCellLinkForm = "$1 ($2)"
	// slackBoldPairText keeps a bold pair's content (mdBoldRE's second group)
	// and drops its marks.
	slackBoldPairText = "${2}"
)

// rewriteSlackBlocks applies the line-level rewrites above to text, leaving
// fenced code blocks as written.
func rewriteSlackBlocks(text string) string {
	lines := strings.Split(text, "\n")
	for i, line := range lines {
		lines[i] = strings.TrimSuffix(line, slackLineCR)
	}
	lines = strings.Split(rewriteOutsideCode(strings.Join(lines, "\n"), func(s string) string {
		return slackGluedHeadingRE.ReplaceAllString(s, slackGluedHeadingSplit)
	}), "\n")
	code := fencedLines(lines)
	out := make([]string, 0, len(lines))
	// paragraph is whether the last line out is a prose line as written,
	// which a setext underline turns into a heading.
	paragraph := false
	for i := 0; i < len(lines); i++ {
		line := lines[i]
		wasParagraph := paragraph
		paragraph = false
		if code[i] {
			out = append(out, line)
			continue
		}
		if wasParagraph && slackSetextRE.MatchString(line) {
			out[len(out)-1] = slackBoldLine(out[len(out)-1])
			continue
		}
		if end := tableEnd(lines, code, i); end > i {
			out = append(out, renderSlackTable(lines[i], lines[i+2:end])...)
			i = end - 1
			continue
		}
		if slackRuleRE.MatchString(line) {
			continue
		}
		if m := slackHeadingRE.FindStringSubmatch(line); m != nil {
			if heading := strings.TrimSpace(m[1]); heading != "" {
				out = append(out, slackBoldLine(heading))
			}
			continue
		}
		if m := slackBulletRE.FindStringSubmatch(line); m != nil {
			out = append(out, m[1]+slackBullet+m[2])
			continue
		}
		out = append(out, line)
		paragraph = strings.TrimSpace(line) != "" && !slackNotParagraphRE.MatchString(line)
	}
	return strings.Join(out, "\n")
}

// slackBoldLine renders a heading's text as one bold line: the bold pairs
// inside it are shed first, and a text that still holds a `**` of its own
// (`**/*.yaml`, `**kwargs`) or starts or ends on a star (`kube-*`, `*.yaml`,
// edge emphasis) is left plain, since wrapping it would run the added marks
// into its own.
func slackBoldLine(text string) string {
	text = strings.TrimSpace(shedBoldPairs(text))
	if strings.Contains(mdCodeSpanRE.ReplaceAllString(text, ""), mdBoldMark) ||
		strings.HasPrefix(text, mdEmphasisStar) || strings.HasSuffix(text, mdEmphasisStar) {
		return text
	}
	return mdBoldMark + text + mdBoldMark
}

// fencedLines reports, per line, whether any part of it sits in a fenced
// block: an mdCodeSpanRE match that spans a line break, or a ~~~ fence. A
// single-line code span does not count, so "- run `ls`" is still a bullet.
// An indented code block is not detected: telling it from an indented list
// item needs the list context this pass does not track.
func fencedLines(lines []string) []bool {
	text := strings.Join(lines, "\n")
	var fences [][]int
	for _, r := range mdCodeSpanRE.FindAllStringIndex(text, -1) {
		if strings.Contains(text[r[0]:r[1]], "\n") {
			fences = append(fences, r)
		}
	}
	code := make([]bool, len(lines))
	tilde := false
	start := 0
	for i, line := range lines {
		end := start + len(line)
		if slackTildeFenceRE.MatchString(line) {
			tilde = !tilde
			code[i] = true
		} else if tilde {
			code[i] = true
		}
		for _, r := range fences {
			if r[0] <= end && r[1] > start {
				code[i] = true
				break
			}
		}
		start = end + 1
	}
	return code
}

// tableEnd returns the index one past the last row of a table whose header
// is lines[i], or i when lines[i] does not start one: the header carries a
// pipe, the next line is a delimiter row with a pipe and as many cells as
// the header, and the body runs to the first line without a pipe, blank or
// fenced.
func tableEnd(lines []string, code []bool, i int) int {
	if i+1 >= len(lines) || code[i+1] || !strings.Contains(lines[i], slackTablePipe) || slackHeadingRE.MatchString(lines[i]) ||
		!strings.Contains(lines[i+1], slackTablePipe) || !slackTableSepRE.MatchString(lines[i+1]) ||
		len(tableCells(lines[i])) != len(tableCells(lines[i+1])) {
		return i
	}
	end := i + 2
	for end < len(lines) && !code[end] && strings.Contains(lines[end], slackTablePipe) &&
		strings.TrimSpace(lines[end]) != "" {
		end++
	}
	return end
}

// renderSlackTable renders a markdown table (its header line and body rows;
// the delimiter row is dropped) as a fenced monospace block with its columns
// padded to line up.
func renderSlackTable(header string, body []string) []string {
	rows := [][]string{cellTexts(header)}
	for _, line := range body {
		rows = append(rows, cellTexts(line))
	}
	cols := 0
	for _, r := range rows {
		cols = max(cols, len(r))
	}
	widths := make([]int, cols)
	for _, r := range rows {
		for c, cell := range r {
			widths[c] = max(widths[c], utf8.RuneCountInString(cell))
		}
	}
	render := func(r []string) string {
		cells := make([]string, cols)
		for c := range cells {
			cell := ""
			if c < len(r) {
				cell = r[c]
			}
			cells[c] = cell + strings.Repeat(slackTablePad, widths[c]-utf8.RuneCountInString(cell))
		}
		return strings.TrimRight(strings.Join(cells, slackTableColSep), slackTablePad)
	}
	rules := make([]string, cols)
	for c, w := range widths {
		rules[c] = strings.Repeat(slackTableRuleChar, max(w, 1))
	}
	out := []string{mdFence, render(rows[0]), strings.Join(rules, slackTableRuleSep)}
	for _, r := range rows[1:] {
		out = append(out, render(r))
	}
	return append(out, mdFence)
}

// tableCells splits one table row into its trimmed cells as written,
// dropping the outer pipes. An escaped pipe (\|) stays in its cell.
func tableCells(line string) []string {
	line = strings.TrimSpace(line)
	line = strings.TrimPrefix(line, slackTablePipe)
	line = strings.TrimSuffix(line, slackTablePipe)
	line = strings.ReplaceAll(line, slackEscapedPipe, slackPipePlaceholder)
	parts := strings.Split(line, slackTablePipe)
	cells := make([]string, len(parts))
	for i, p := range parts {
		cells[i] = strings.TrimSpace(strings.ReplaceAll(p, slackPipePlaceholder, slackTablePipe))
	}
	return cells
}

// cellTexts is tableCells with each cell read as it displays in a code
// block: a code span keeps its content and loses its backticks, a link
// becomes its label and URL, and bold marks outside code spans go.
func cellTexts(line string) []string {
	cells := tableCells(line)
	for i, cell := range cells {
		var b strings.Builder
		end := 0
		for _, r := range mdCodeSpanRE.FindAllStringIndex(cell, -1) {
			b.WriteString(cellProse(cell[end:r[0]]))
			b.WriteString(strings.Trim(cell[r[0]:r[1]], mdCodeTick))
			end = r[1]
		}
		b.WriteString(cellProse(cell[end:]))
		cells[i] = b.String()
	}
	return cells
}

// cellProse renders the prose between a cell's code spans: links as
// "label (url)", bold marks dropped.
func cellProse(s string) string {
	return shedBoldPairs(slackLinkRE.ReplaceAllString(s, slackCellLinkForm))
}

// shedBoldPairs removes the marks of every closed bold pair (mdBoldRE) outside
// code spans, keeping the text between them: a heading is about to be bolded
// whole, and a table cell is about to sit in a code block. A `**` that is not
// half of a pair, as in `**kwargs` or `**/*.yaml`, stays.
func shedBoldPairs(s string) string {
	return rewriteOutsideCode(s, func(t string) string { return mdBoldRE.ReplaceAllString(t, slackBoldPairText) })
}
