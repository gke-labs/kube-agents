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
// line up. Lines inside a fenced code block are left as written. It runs on
// the raw text, before escaping, so the table's column widths are measured on
// what Slack will display (Slack decodes the escaped &, < and > back).
var (
	// slackHeadingRE is an ATX heading: up to three spaces, one to six #,
	// whitespace, the text, and an optional closing run of #.
	slackHeadingRE = regexp.MustCompile(`^ {0,3}#{1,6}[ \t]+(.*?)[ \t]*(?:#+[ \t]*)?$`)
	// slackRuleRE is a thematic break: three or more of one of - * _, with
	// optional spaces between them.
	slackRuleRE = regexp.MustCompile(`^ {0,3}([-*_])(?:[ \t]*([-*_]))+[ \t]*$`)
	// slackBulletRE is an unordered list item: indentation, one of - * +,
	// whitespace, the item.
	slackBulletRE = regexp.MustCompile(`^([ \t]*)[-*+][ \t]+(.*)$`)
	// slackTableSepRE is a table's delimiter row: cells of dashes with
	// optional alignment colons, separated by pipes, outer pipes optional.
	slackTableSepRE = regexp.MustCompile(`^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$`)
	// slackCellMarksRE is the inline markup a table cell may carry that a
	// monospace block would show raw: bold and italic marks, code backticks.
	slackCellMarksRE = regexp.MustCompile("\\*\\*|__|`")
	// slackGluedHeadingRE is a heading run onto the end of a sentence with
	// no line break ("…its health status.# Cluster Report"), as a model's
	// streamed answer can arrive: sentence punctuation, then one to six #
	// and a space. The heading is moved to a line of its own so the pass
	// above reads it as one.
	slackGluedHeadingRE = regexp.MustCompile(`([.!?:])(#{1,6} )`)
)

const (
	// slackBullet replaces a list item's marker.
	slackBullet = "• "
	// slackBoldOpen and slackBoldClose wrap a heading's text as markdown
	// bold, which toMrkdwn's bold pass then turns into Slack's.
	slackBoldOpen  = "**"
	slackBoldClose = "**"
	// slackTableColSep separates a rendered table's columns, and
	// slackTableRuleChar draws the line under its header.
	slackTableColSep   = " | "
	slackTableRuleChar = "-"
	slackTableRuleSep  = "-+-"
	// slackTablePipe is the cell separator in the markdown being read, and
	// slackEscapedPipe the escaped one a cell may carry, held in
	// slackPipePlaceholder while the row is split.
	slackTablePipe       = "|"
	slackEscapedPipe     = `\|`
	slackPipePlaceholder = "\x00"
	// slackTableDash is what a delimiter row must contain beyond pipes.
	slackTableDash = "-"
	// mdBoldMark and mdBoldMarkAlt are markdown's two bold spellings, which
	// a heading's text sheds before it is wrapped as one bold line.
	mdBoldMark    = "**"
	mdBoldMarkAlt = "__"
)

// rewriteSlackBlocks applies the line-level rewrites above to text, leaving
// fenced code blocks as written.
func rewriteSlackBlocks(text string) string {
	lines := strings.Split(text, "\n")
	lines = splitGluedHeadings(lines)
	out := make([]string, 0, len(lines))
	inFence := false
	for i := 0; i < len(lines); i++ {
		line := lines[i]
		if strings.HasPrefix(strings.TrimLeft(line, " \t"), mdFence) {
			inFence = !inFence
			out = append(out, line)
			continue
		}
		if inFence {
			out = append(out, line)
			continue
		}
		if i+1 < len(lines) && strings.Contains(line, slackTablePipe) && slackTableSepRE.MatchString(lines[i+1]) &&
			strings.Contains(lines[i+1], slackTableDash) {
			end := i + 2
			for end < len(lines) && strings.Contains(lines[end], slackTablePipe) && strings.TrimSpace(lines[end]) != "" {
				end++
			}
			out = append(out, renderSlackTable(lines[i], lines[i+2:end])...)
			i = end - 1
			continue
		}
		if m := slackRuleRE.FindStringSubmatch(line); m != nil && sameRuleMarks(line) {
			continue
		}
		if m := slackHeadingRE.FindStringSubmatch(line); m != nil {
			heading := strings.TrimSpace(strings.ReplaceAll(strings.ReplaceAll(m[1], mdBoldMark, ""), mdBoldMarkAlt, ""))
			if heading != "" {
				out = append(out, slackBoldOpen+heading+slackBoldClose)
			}
			continue
		}
		if m := slackBulletRE.FindStringSubmatch(line); m != nil {
			out = append(out, m[1]+slackBullet+m[2])
			continue
		}
		out = append(out, line)
	}
	return strings.Join(out, "\n")
}

// splitGluedHeadings moves a heading run onto the end of a sentence to its
// own paragraph, outside fenced code.
func splitGluedHeadings(lines []string) []string {
	out := make([]string, 0, len(lines))
	inFence := false
	for _, line := range lines {
		if strings.HasPrefix(strings.TrimLeft(line, " \t"), mdFence) {
			inFence = !inFence
		}
		if inFence || !slackGluedHeadingRE.MatchString(line) || mdCodeSpanRE.MatchString(line) {
			out = append(out, line)
			continue
		}
		out = append(out, strings.Split(slackGluedHeadingRE.ReplaceAllString(line, "$1\n\n$2"), "\n")...)
	}
	return out
}

// sameRuleMarks reports whether every mark in a thematic break is the same
// character, as CommonMark requires ("- * -" is not a rule).
func sameRuleMarks(line string) bool {
	marks := strings.Map(func(r rune) rune {
		if r == ' ' || r == '\t' {
			return -1
		}
		return r
	}, line)
	return strings.Count(marks, marks[:1]) == len(marks)
}

// renderSlackTable renders a markdown table (its header line and body rows;
// the delimiter row is dropped) as a fenced monospace block with its columns
// padded to line up.
func renderSlackTable(header string, body []string) []string {
	rows := [][]string{tableCells(header)}
	for _, line := range body {
		rows = append(rows, tableCells(line))
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
			cells[c] = cell + strings.Repeat(" ", widths[c]-utf8.RuneCountInString(cell))
		}
		return strings.TrimRight(strings.Join(cells, slackTableColSep), " ")
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

// tableCells splits one table row into its trimmed cells, dropping the outer
// pipes and the inline marks a monospace block would show raw. An escaped
// pipe (\|) stays in its cell.
func tableCells(line string) []string {
	line = strings.TrimSpace(line)
	line = strings.TrimPrefix(line, slackTablePipe)
	line = strings.TrimSuffix(line, slackTablePipe)
	line = strings.ReplaceAll(line, slackEscapedPipe, slackPipePlaceholder)
	parts := strings.Split(line, slackTablePipe)
	cells := make([]string, len(parts))
	for i, p := range parts {
		p = strings.ReplaceAll(p, slackPipePlaceholder, slackTablePipe)
		cells[i] = strings.TrimSpace(slackCellMarksRE.ReplaceAllString(p, ""))
	}
	return cells
}
