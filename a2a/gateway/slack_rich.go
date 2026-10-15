package gateway

import (
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

// A chat message's detail and fold text are markdown, and Slack's rich_text
// draws markdown's block forms natively: a fenced block as
// rich_text_preformatted, a list as rich_text_list (nesting by its indent),
// a quote as rich_text_quote. slackRichDocument reads those line by line and
// lays the rest out as sections, a heading as a bold line, paragraphs apart
// by a blank line, and every inline span through slackRichMarkdown (code,
// bold, italic, strike, links). rich_text text is literal: Slack parses no
// mrkdwn and no mention in it, so nothing here escapes. A table becomes a
// preformatted block with its columns lined up (renderSlackTable), and a
// rule goes, as on the text path (slack_blocks.go).
var (
	// slackRichFenceRE opens a fenced block: its indent, then a run of three
	// or more backticks or tildes. A backtick run with a backtick after it on
	// the line (```cmd```) is a code span, as CommonMark reads it.
	slackRichFenceRE = regexp.MustCompile("^( *)(`{3,}|~{3,})")
	// slackRichItemRE is a list item: its indent, its marker (a bullet, or a
	// number and its "." or ")"), whitespace, the item's text.
	slackRichItemRE = regexp.MustCompile(`^( *)([-*+•]|(\d{1,9})[.)])[ \t]+(.*)$`)
	// slackRichQuoteRE is a quote line and its text.
	slackRichQuoteRE = regexp.MustCompile(`^ {0,3}> ?(.*)$`)
)

const (
	// slackRichBullet and slackRichOrdered are rich_text_list's styles.
	slackRichBullet  = "bullet"
	slackRichOrdered = "ordered"
	// slackRichIndentMax caps a nested list's indent at Slack's deepest.
	slackRichIndentMax = 8
	// slackRichLineBreak ends a line inside a section; two end a paragraph.
	slackRichLineBreak = "\n"
)

// slackRichDoc is slackRichDocument's state while it reads the lines.
type slackRichDoc struct {
	out     []map[string]any
	section []map[string]any
	// blank is whether a blank line or a rule came since the section's
	// last line, so the next one starts a paragraph.
	blank bool
	// indents is the open list's indent per level; list is the list element
	// being filled, at listLevel in listStyle, and item the section of its
	// last item, which an indented line continues (nil once a fenced block
	// inside the item has ended the list element it sat in).
	indents   []int
	list      map[string]any
	listLevel int
	listStyle string
	item      map[string]any
	// proseFrom is where the section's last line starts when that line is
	// prose a setext underline can make a heading, else -1.
	proseFrom int
	// lastLineFrom is where line put the section's last line.
	lastLineFrom int
}

// slackRichDocument is md as the elements of one rich_text block.
func slackRichDocument(md string) []map[string]any {
	lines := strings.Split(strings.ReplaceAll(md, "\r\n", "\n"), "\n")
	noCode := make([]bool, len(lines))
	d := &slackRichDoc{proseFrom: -1}
	for i := 0; i < len(lines); i++ {
		line := lines[i]
		if m := slackRichFenceRE.FindStringSubmatch(line); m != nil &&
			!(m[2][0] == '`' && strings.Contains(line[len(m[0]):], "`")) {
			i = d.fence(lines, i, len(m[1]), m[2])
			continue
		}
		if strings.TrimSpace(line) == "" {
			d.blank = true
			d.proseFrom = -1
			continue
		}
		// A setext underline bolds the prose line above it, as the text path
		// does; a rule ("---", "* * *") is read before a list item, which
		// "* * *" would otherwise also match.
		if d.proseFrom >= 0 && slackSetextRE.MatchString(line) {
			slackRichBold(d.section[d.proseFrom:])
			d.proseFrom = -1
			continue
		}
		if slackRuleRE.MatchString(line) {
			d.endList()
			d.blank = true
			d.proseFrom = -1
			continue
		}
		if m := slackRichItemRE.FindStringSubmatch(line); m != nil {
			d.listItem(len(m[1]), m[3], m[4])
			continue
		}
		if d.indents != nil && line[0] == ' ' {
			d.continueItem(strings.TrimSpace(line))
			continue
		}
		d.endList()
		if end := tableEnd(lines, noCode, i); end > i {
			table := renderSlackTable(lines[i], lines[i+2:end])
			d.block(slackRichPre(strings.Join(table[1:len(table)-1], slackRichLineBreak)))
			i = end - 1
			continue
		}
		if m := slackHeadingRE.FindStringSubmatch(line); m != nil {
			if heading := strings.TrimSpace(m[1]); heading != "" {
				d.line(slackRichBold(slackRichMarkdown(shedBoldPairs(heading))))
			}
			continue
		}
		if slackRichQuoteRE.MatchString(line) {
			var quoted []string
			for ; i < len(lines) && slackRichQuoteRE.MatchString(lines[i]); i++ {
				quoted = append(quoted, slackRichQuoteRE.FindStringSubmatch(lines[i])[1])
			}
			i--
			d.block(map[string]any{"type": "rich_text_quote", "elements": slackRichMarkdown(strings.Join(quoted, slackRichLineBreak))})
			continue
		}
		d.line(slackRichMarkdown(strings.TrimSpace(line)))
		if !slackNotParagraphRE.MatchString(line) {
			d.proseFrom = d.lastLineFrom
		}
	}
	d.endList()
	d.flush()
	return d.out
}

// line adds one line of prose to the section, a paragraph's break before it
// when a blank line came between.
func (d *slackRichDoc) line(elements []map[string]any) {
	if len(d.section) > 0 {
		sep := slackRichLineBreak
		if d.blank {
			sep += slackRichLineBreak
		}
		d.section = append(d.section, slackRichText(sep, nil))
	}
	d.lastLineFrom = len(d.section)
	d.section = append(d.section, elements...)
	d.blank = false
	d.proseFrom = -1
}

// flush ends the section.
func (d *slackRichDoc) flush() {
	if len(d.section) > 0 {
		d.out = append(d.out, slackRichSection(d.section))
	}
	d.section, d.blank, d.proseFrom = nil, false, -1
}

// block ends the section and the list element and adds a block element.
func (d *slackRichDoc) block(element map[string]any) {
	d.flush()
	d.closeList()
	d.out = append(d.out, element)
}

// closeList adds the list element being filled; the list stays open, so an
// item after a fenced block inside it continues the list.
func (d *slackRichDoc) closeList() {
	if d.list != nil {
		d.out = append(d.out, d.list)
		d.list = nil
	}
}

// endList closes the list element and the list.
func (d *slackRichDoc) endList() {
	d.closeList()
	d.indents, d.item = nil, nil
}

// listItem adds one item at indent: a level deeper than the item above when
// indented past it, back at an open level when indented to it.
func (d *slackRichDoc) listItem(indent int, number, text string) {
	d.flush()
	for len(d.indents) > 0 && d.indents[len(d.indents)-1] > indent {
		d.indents = d.indents[:len(d.indents)-1]
	}
	if len(d.indents) == 0 || d.indents[len(d.indents)-1] < indent {
		d.indents = append(d.indents, indent)
	}
	level := min(len(d.indents)-1, slackRichIndentMax)
	style := slackRichBullet
	if number != "" {
		style = slackRichOrdered
	}
	if d.list == nil || d.listLevel != level || d.listStyle != style {
		d.closeList()
		d.list = map[string]any{"type": "rich_text_list", "style": style, "indent": level, "elements": []map[string]any{}}
		if n, err := strconv.Atoi(number); err == nil && n > 1 {
			d.list["offset"] = n - 1
		}
		d.listLevel, d.listStyle = level, style
	}
	d.item = slackRichSection(slackRichMarkdown(text))
	d.list["elements"] = append(d.list["elements"].([]map[string]any), d.item)
	d.blank = false
}

// continueItem adds an indented line to the last item, or as prose when the
// item's list element has already been added.
func (d *slackRichDoc) continueItem(text string) {
	if d.item == nil || d.list == nil {
		d.line(slackRichMarkdown(text))
		return
	}
	elements := d.item["elements"].([]map[string]any)
	elements = append(elements, slackRichText(slackRichLineBreak, nil))
	d.item["elements"] = append(elements, slackRichMarkdown(text)...)
	d.blank = false
}

// fence reads the fenced block opening at lines[start] and returns the index
// of its last line: its closing fence, or the text's end when it has none.
// Its lines lose the opener's indent, as an item's fenced block is indented
// under the item.
func (d *slackRichDoc) fence(lines []string, start, indent int, marker string) int {
	if d.indents != nil && indent == 0 {
		d.endList()
	}
	var code []string
	end := start + 1
	for ; end < len(lines); end++ {
		trimmed := strings.TrimSpace(lines[end])
		if strings.HasPrefix(trimmed, marker[:1]) && strings.Trim(trimmed, marker[:1]) == "" && len(trimmed) >= len(marker) {
			break
		}
		line := lines[end]
		cut := 0
		for cut < indent && cut < len(line) && line[cut] == ' ' {
			cut++
		}
		code = append(code, line[cut:])
	}
	if text := strings.Join(code, slackRichLineBreak); strings.TrimSpace(text) != "" {
		d.block(slackRichPre(text))
	}
	d.item = nil
	return min(end, len(lines)-1)
}

func slackRichPre(text string) map[string]any {
	return map[string]any{"type": "rich_text_preformatted", "elements": []map[string]any{slackRichText(text, nil)}}
}

// slackRichBold sets bold on every element of a heading's line.
func slackRichBold(elements []map[string]any) []map[string]any {
	for _, e := range elements {
		style := map[string]any{"bold": true}
		if old, ok := e["style"].(map[string]any); ok {
			for k, v := range old {
				style[k] = v
			}
		}
		e["style"] = style
	}
	return elements
}

// slackRichLen is the runes of text an element holds, its children's
// included.
func slackRichLen(v any) int {
	n := 0
	switch node := v.(type) {
	case map[string]any:
		if text, ok := node["text"].(string); ok {
			n += utf8.RuneCountInString(text)
		}
		if children, ok := node["elements"].([]map[string]any); ok {
			for _, c := range children {
				n += slackRichLen(c)
			}
		}
	}
	return n
}

// slackRichPreLines cuts a preformatted block's text into chunks of at most
// budget runes between its lines, a line past budget cut by runes. The text
// is code already, so unlike chatChunks nothing in it is read as markdown:
// a ``` line or a table row inside it is content, not a fence to balance or
// a header to repeat.
func slackRichPreLines(text string, budget int) []string {
	var out []string
	var chunk strings.Builder
	size, open := 0, false
	flush := func() {
		out = append(out, chunk.String())
		chunk.Reset()
		size, open = 0, false
	}
	for _, line := range strings.Split(text, slackRichLineBreak) {
		rs := []rune(line)
		for len(rs) > budget {
			if open {
				flush()
			}
			chunk.WriteString(string(rs[:budget]))
			flush()
			rs = rs[budget:]
		}
		n := len(rs)
		if open && size+len(slackRichLineBreak)+n > budget {
			flush()
		}
		if open {
			chunk.WriteString(slackRichLineBreak)
			size += len(slackRichLineBreak)
		}
		chunk.WriteString(string(rs))
		size += n
		open = true
	}
	if open {
		flush()
	}
	return out
}

// slackRichPreChunks cuts an element past budget runes into several: a
// preformatted one between its lines (slackRichPreLines), a section or a quote
// between its spans, a text span past budget cut by runes, a list between
// its items (a numbered list's numbering kept by offset). A single item
// past budget stands alone.
func slackRichPreChunks(e map[string]any, budget int) []map[string]any {
	if slackRichLen(e) <= budget {
		return []map[string]any{e}
	}
	var out []map[string]any
	switch e["type"] {
	case "rich_text_preformatted":
		text, _ := e["elements"].([]map[string]any)[0]["text"].(string)
		for _, chunk := range slackRichPreLines(text, budget) {
			out = append(out, slackRichPre(chunk))
		}
	case "rich_text_list":
		return slackRichListChunks(e, budget)
	case "rich_text_section", "rich_text_quote":
		var spans, group []map[string]any
		for _, span := range e["elements"].([]map[string]any) {
			spans = append(spans, slackRichSpanChunks(span, budget)...)
		}
		size := 0
		for _, span := range spans {
			n := slackRichLen(span)
			if len(group) > 0 && size+n > budget {
				out = append(out, map[string]any{"type": e["type"], "elements": group})
				group, size = nil, 0
			}
			group = append(group, span)
			size += n
		}
		out = append(out, map[string]any{"type": e["type"], "elements": group})
	default:
		out = append(out, e)
	}
	return out
}

// slackRichListChunks cuts a list between its items into lists of at most
// budget runes, each keeping the style and indent, a numbered one its
// numbering through offset.
func slackRichListChunks(e map[string]any, budget int) []map[string]any {
	items, _ := e["elements"].([]map[string]any)
	offset, _ := e["offset"].(int)
	var out []map[string]any
	start, size := 0, 0
	emit := func(end int) {
		part := map[string]any{"type": e["type"], "style": e["style"], "indent": e["indent"], "elements": items[start:end]}
		if n := offset + start; n > 0 && e["style"] == slackRichOrdered {
			part["offset"] = n
		}
		out = append(out, part)
	}
	for i, item := range items {
		n := slackRichLen(item)
		if i > start && size+n > budget {
			emit(i)
			start, size = i, 0
		}
		size += n
	}
	emit(len(items))
	return out
}

// slackRichSpanChunks cuts a text span past budget runes into spans of at
// most budget runes, each keeping its style.
func slackRichSpanChunks(span map[string]any, budget int) []map[string]any {
	text, ok := span["text"].(string)
	if !ok || span["type"] != "text" || utf8.RuneCountInString(text) <= budget {
		return []map[string]any{span}
	}
	style, _ := span["style"].(map[string]any)
	rs := []rune(text)
	var out []map[string]any
	for start := 0; start < len(rs); start += budget {
		out = append(out, slackRichText(string(rs[start:min(start+budget, len(rs))]), style))
	}
	return out
}

// slackRichBlocks is md as rich_text blocks of at most budget runes each,
// cut between elements, an element past budget cut as slackRichPreChunks
// cuts it; only a single list item past budget is a block alone.
func slackRichBlocks(md string, budget int) []map[string]any {
	var blocks []map[string]any
	var group []map[string]any
	size := 0
	var elements []map[string]any
	for _, e := range slackRichDocument(md) {
		elements = append(elements, slackRichPreChunks(e, budget)...)
	}
	for _, e := range elements {
		n := slackRichLen(e)
		if len(group) > 0 && size+n > budget {
			blocks = append(blocks, slackRichBlock(group))
			group, size = nil, 0
		}
		group = append(group, e)
		size += n
	}
	if len(group) > 0 {
		blocks = append(blocks, slackRichBlock(group))
	}
	return blocks
}
