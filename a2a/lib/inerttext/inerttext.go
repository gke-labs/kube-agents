// Package inerttext renders text that came from outside (a forge, an agent
// card, a caller) so that a model reading the answer takes it as data: a
// one-line string as a clipped code span, multi-line text as one fenced block
// that cannot close itself. The in-cluster MCP server (a2a/mcp) and the
// workstation bridge (a2a/mcpbridge) both render with it, so the bridge
// carries one copy of the rules without linking the server and its bus
// client.
//
// DiffHeaderBSidePath renders nothing. It is here because both callers read
// a file's path from a `diff --git` header with it before they show the
// path, and the bridge keys its per-file patches by the result.
package inerttext

import (
	"fmt"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

const (
	// Dash is what Cell and Field show for text that is empty once made inert.
	Dash = "—"
	// tablePipe stands in for "|" in card text: a table cell ends at a pipe
	// even inside a code span, and an escape the text could undo is no
	// escape at all.
	tablePipe = "¦"

	// maxCellRunes caps one card field in the table. Card text is written by
	// whoever publishes the card and read by a model in the caller's editor,
	// so it is shown as short, inert data, never as markdown it controls.
	maxCellRunes = 200
	// Ellipsis ends text that was cut short.
	Ellipsis = "…"

	// maxFieldBytes clips a one-line field before Cell sees it. Cell shows
	// 200 runes at most, but it maps and splits the whole string first,
	// and a title is only bounded by the broker answer's size.
	maxFieldBytes = 1 << 10

	// MinFenceLen is the shortest fence a block is written with.
	MinFenceLen = 3

	diffBSidePrefix   = " b/"
	diffQuotedBPrefix = ` "b/`
)

// Cell renders untrusted text as one inert table cell: a code span, which
// markdown renders verbatim, so links, images, HTML and emphasis are shown as
// text. Backticks become quotes so the text cannot close the span, pipes
// become tablePipe so it cannot end the cell, control and format characters
// (line breaks, bidi overrides, zero-width marks) go, and the result is capped
// at maxCellRunes.
func Cell(s string) string {
	s = strings.Map(func(r rune) rune {
		switch {
		case r == '`':
			return '\''
		case unicode.IsControl(r):
			return ' '
		case unicode.Is(unicode.Cf, r):
			return -1
		}
		return r
	}, s)
	s = strings.ReplaceAll(strings.Join(strings.Fields(s), " "), "|", tablePipe)
	if r := []rune(s); len(r) > maxCellRunes {
		s = string(r[:maxCellRunes]) + Ellipsis
	}
	if s == "" {
		return Dash
	}
	return "`" + s + "`"
}

// WriteBlock writes untrusted text as one fenced block, capped at maxBytes
// and maxLines (0 for none) on line boundaries, each line capped at
// lineBytes, and says what the caps left out.
func WriteBlock(sb *strings.Builder, s, info string, maxBytes, maxLines, lineBytes int) {
	kept, lines, bytes := inertPrefix(s, maxBytes, maxLines, lineBytes)
	fence := strings.Repeat("`", max(MinFenceLen, longestRun(kept, '`')+1))
	sb.WriteString(fence + info + "\n" + kept)
	if !strings.HasSuffix(kept, "\n") {
		sb.WriteString("\n")
	}
	sb.WriteString(fence + "\n")
	if lines > 0 {
		fmt.Fprintf(sb, "\n_Truncated: %d more line(s), %d more byte(s) not shown._\n", lines, bytes)
	}
}

// inertPrefix makes the head of untrusted multi-line text inert inside a
// fence and returns it with how many lines and bytes of s it left out. A line
// ends at \n alone (a \r just before it is part of the ending) and tabs stay;
// every other control or format character (ANSI escapes, bidi overrides,
// zero-width marks, line separators, and any other \r) is shown as <U+XXXX>
// rather than dropped, so a reviewer sees that it is there, and a \r inside a
// diff line cannot draw a line the diff does not have. Each line is cut at lineBytes, at a rune boundary, saying
// how much went; whole lines are kept while they fit in maxBytes and
// maxLines (0 for no line cap). It never cuts inside a line, the
// line-anchored counterpart of #3689's line-anchored diff split, so a
// truncated diff ends on a whole line rather than mid-hunk-header; the one
// exception is a first line longer than the whole cap, cut at a rune
// boundary rather than show nothing.
//
// It walks s by index and stops at the caps: no copy of s is made, the
// output is at most maxBytes, and what it leaves out is only counted. s may
// be as large as the broker's answer; this costs O(maxBytes) memory.
func inertPrefix(s string, maxBytes, maxLines, lineBytes int) (string, int, int) {
	out := make([]byte, 0, min(len(s), maxBytes)+utf8.UTFMax)
	pos, n := 0, 0
	for pos < len(s) {
		if maxLines > 0 && n >= maxLines {
			break
		}
		end, next := lineBounds(s, pos)
		mark := len(out)
		out, _ = appendInert(out, s[pos:end], lineBytes, true)
		if next > end {
			out = append(out, '\n')
		}
		if len(out) > maxBytes {
			out = out[:mark]
			if n == 0 {
				var stop int
				out, stop = appendInert(out, s[pos:end], maxBytes, false)
				pos += stop
			}
			break
		}
		pos, n = next, n+1
	}
	if pos >= len(s) {
		return string(out), 0, 0
	}
	rest := s[pos:]
	return string(out), strings.Count(strings.TrimSuffix(rest, "\n"), "\n") + 1, len(rest)
}

// lineBounds is the line of s starting at pos: end is where its text ends
// and next where the following line starts, past a \n. A \r just before the
// \n is left out of the text; any other \r is text, for appendInert to show.
func lineBounds(s string, pos int) (end, next int) {
	i := strings.IndexByte(s[pos:], '\n')
	if i < 0 {
		return len(s), len(s)
	}
	end, next = pos+i, pos+i+1
	if end > pos && s[end-1] == '\r' {
		end--
	}
	return end, next
}

// appendInert appends the inert form of one line (no line ending) to dst,
// at most limit bytes of it. With marker, a line cut short ends in a note of
// how many of its bytes went and the whole line counts as used; without, it
// stops at the cap and reports how many bytes of line it used.
func appendInert(dst []byte, line string, limit int, marker bool) ([]byte, int) {
	start := len(dst)
	var esc [16]byte
	for i, r := range line {
		var piece []byte
		switch {
		case r == utf8.RuneError:
			piece = append(esc[:0], "\uFFFD"...)
		case r == '\t':
			piece = append(esc[:0], '\t')
		case unicode.IsControl(r), unicode.Is(unicode.Cf, r), unicode.In(r, unicode.Zl, unicode.Zp):
			piece = appendCodePoint(esc[:0], r)
		default:
			piece = utf8.AppendRune(esc[:0], r)
		}
		if len(dst)-start+len(piece) > limit {
			if !marker {
				return dst, i
			}
			return fmt.Appendf(dst, "… [%d bytes cut]", len(line)-i), len(line)
		}
		dst = append(dst, piece...)
	}
	return dst, len(line)
}

// appendCodePoint appends r as <U+XXXX> (at least four upper-case hex
// digits), without fmt, which would move the caller's scratch to the heap.
func appendCodePoint(dst []byte, r rune) []byte {
	const hex = "0123456789ABCDEF"
	dst = append(dst, "<U+"...)
	if r < 0 || r > utf8.MaxRune {
		r = utf8.RuneError
	}
	u := uint32(r) // #nosec G115 -- clamped to [0, utf8.MaxRune] just above
	digits := 4
	for v := u >> 16; v > 0; v >>= 4 {
		digits++
	}
	for i := digits - 1; i >= 0; i-- {
		dst = append(dst, hex[(u>>(4*uint(i)))&0xF])
	}
	return append(dst, '>')
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

// Field is a one-line forge string as a table cell, clipped first so Cell
// never maps or splits more than maxFieldBytes of it.
func Field(s string) string {
	return Cell(s[:runeFloor(s, maxFieldBytes)])
}

// DiffHeaderBSidePath is the post-image path of the remainder of a `diff --git
// a/<p> b/<p>` header line, the text after `diff --git `. It tolerates git's
// C-style quoting (`"a/p q" "b/p q"`) for paths with spaces or special
// characters, and an unquoted path that itself contains " b/" is split where
// the a-side matches the b-side. Adam Parco's, from #3689.
func DiffHeaderBSidePath(header string) string {
	line := strings.TrimSpace(header)
	if idx := strings.LastIndex(line, diffQuotedBPrefix); idx >= 0 {
		if unquoted, err := strconv.Unquote(line[idx+1:]); err == nil {
			return strings.TrimPrefix(unquoted, "b/")
		}
	}
	// Git does not quote a path for a space alone, so " b/" may be inside
	// the path too (`a/docs b/x b/docs b/x`). Outside a rename both sides
	// are the same path, so the split whose a-side matches its b-side is
	// the one; a rename has no such split and takes the last " b/".
	last := -1
	for from := 0; ; {
		i := strings.Index(line[from:], diffBSidePrefix)
		if i < 0 {
			break
		}
		last = from + i
		if b := line[last+len(diffBSidePrefix):]; line[:last] == "a/"+b {
			return strings.TrimSpace(b)
		}
		from = last + 1
	}
	if last >= 0 {
		return strings.TrimSpace(line[last+len(diffBSidePrefix):])
	}
	return ""
}
