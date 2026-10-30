package mcp

import "strings"

// WriteInertBlock writes untrusted multi-line text the way inspect_pr does:
// one fenced block with info as its info string, a fence longer than any
// backtick run inside, control and format characters shown as <U+XXXX>,
// capped at maxBytes and maxLines (0 for none) on line boundaries with each
// line capped at lineBytes, and a note of what the caps left out. It is
// exported for the workstation bridge (a2a/mcpbridge), which renders pull
// requests it fetches itself and must not present their text as its own.
func WriteInertBlock(sb *strings.Builder, s, info string, maxBytes, maxLines, lineBytes int) {
	writeBlock(sb, s, info, maxBytes, maxLines, lineBytes)
}

// InertField renders a one-line untrusted string as inspect_pr renders a
// title, author or path: a clipped code span that cannot close itself, end a
// table cell, or carry control or format characters.
func InertField(s string) string {
	return field(s)
}
