package gateway

import (
	"errors"
	"fmt"
	"strconv"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// The Google Chat renderer for kube-agents.chat/v1 (spec-a2a-payloads.md,
// "Chat messages"): one validated chat message laid out as a Chat text
// message, in the spec's fixed order -- headline and note, detail, rows,
// after, fold, then the buttons, choices before links, then the Retry and
// Drop of any row that carries them (gchat_board.go) -- with the buttons as
// accessory widgets under the message; in a progress message the plan
// follows the headline in the rows' place. It is text rather than a card
// because Chat's card text has no monospace and no code, which a table, a
// command or a resource name in an answer needs, while a text message
// carries bold, lists, code spans and monospace blocks, and accessory
// widgets draw the same buttons under it (Chat API, Message.accessoryWidgets,
// app authentication). Every field of markdown goes through toGchatText, so
// a mention or a <url|text> in it is defanged as in any post. It lays out
// and never composes: every word comes from the message, and the only things
// it adds are its backend's own -- the glyph on a plan item, the step
// markers, the bullet on a row, the Stop button the gateway asks for under
// a running task's line (gchat_stop.go), and, where a row carries Retry and
// Drop, the row's number in the bullet's place and the "Retry n" and "Drop
// n" labels on its buttons (gchat_board.go). The one thing it leaves out is
// a running or failed plan item's last step when the item's title already
// holds that step whole, since Chat draws the steps in the open
// (gchatChatPlan); a step the title clipped is kept.
const (
	// gchatChatTextMax bounds the laid-out text: the adapter's one bound on
	// a posted text (gchatTextMax). A message past it posts as its
	// fallback, chunked.
	gchatChatTextMax = gchatTextMax
	// gchatChatFunction is the onClick action a choice button sends, and
	// gchatChatParamReply and gchatChatParamChoice its parameters: the
	// clicker's message, and the choice's index in the message.
	gchatChatFunction    = "kage.choice"
	gchatChatParamReply  = "reply"
	gchatChatParamChoice = "choice"
	// gchatChatParamHome marks a home post's choice buttons. The click's
	// binding does not read it; new cards keep carrying it so an older
	// gateway still binds them.
	gchatChatParamHome      = "home"
	gchatChatParamHomeValue = "1"
	// gchatChatButtonFilled draws the one primary button filled, Chat's
	// emphasised button; the rest take Chat's default.
	gchatChatButtonFilled = "FILLED"
	// gchatChatRowBullet leads each row: Chat draws a bulleted list from it,
	// which keeps rows apart where Slack's blocks do it by layout.
	gchatChatRowBullet = "- "
	// gchatChatIndent sets a plan item's steps under its line.
	gchatChatIndent = "  "
	// gchatChatRowDetailSep sets a row's detail after its text on the same
	// line: Chat draws a bulleted list from the marker but keeps no indent
	// on a continuation line, so a detail on its own line fell out of the
	// list (seen live).
	gchatChatRowDetailSep = " · "
	// gchatChatStepDone and gchatChatStepOpen mark a plan item's steps, as
	// the Slack renderer does: the last step of a running item is the open
	// one, unless the item's title already holds it and it is not drawn
	// (gchatChatPlan).
	gchatChatStepDone = "✓"
	gchatChatStepOpen = "◌"
	// gchatChatSectionGap separates the layout's sections.
	gchatChatSectionGap = "\n\n"
	// gchatChatBold wraps a headline and a fold's title: Chat's bold mark.
	gchatChatBold = "*"
	// gchatChatCode wraps a row's severity as inline code.
	gchatChatCode = "`"
)

// gchatChatStatusGlyph leads a plan item by its status: Chat has no task
// card, so the glyph carries what Slack's card icon does.
var gchatChatStatusGlyph = map[string]string{
	chatmsg.StatusPending:    "⏳",
	chatmsg.StatusInProgress: "⚙️",
	chatmsg.StatusComplete:   "✅",
	chatmsg.StatusError:      "❌",
}

// errGchatChatTooLarge is a valid chat message that is more than one Chat
// message holds. The caller posts the fallback.
var errGchatChatTooLarge = errors.New("the chat message is more than one Google Chat message holds")

// errGchatChatEmpty is a chat message that lays nothing out: no text and no
// button. The caller posts the fallback.
var errGchatChatEmpty = errors.New("the chat message lays nothing out")

// gchatChatLayout is one rendered message: its text, and the accessory
// widgets under it (empty when the message has no button). A row's place
// in Text is not kept here: the row's buttons carry its line
// (gchatBoardButtons), so a tap edits by it whether or not the layout was
// remembered.
type gchatChatLayout struct {
	Text    string
	Widgets []map[string]any
}

// renderGchatChat lays m out as a Chat text message with accessory buttons.
// An error is a message the renderer will not send -- too large for Chat, or
// empty -- and the caller posts the fallback instead.
func renderGchatChat(m *chatmsg.Message) (gchatChatLayout, error) {
	var sections []string
	add := func(s string) {
		if strings.TrimSpace(s) != "" {
			sections = append(sections, strings.TrimRight(s, "\n"))
		}
	}
	add(gchatChatHead(m))
	if strings.TrimSpace(m.Detail) != "" {
		add(toGchatText(m.Detail))
	}
	// The rows' buttons are drawn only where the rows are: a progress
	// message's plan takes their place, and a button for a row not shown
	// would name nothing. Each button carries its row's line in the text,
	// so the rows' first line is settled before they are drawn: the rows
	// begin after the sections so far (their last line is the count of
	// breaks in them) and the gap's blank line.
	var rowButtons []map[string]any
	if m.Plan != nil && len(m.Plan.Items) > 0 {
		add(gchatChatPlan(m.Plan))
	} else {
		rowsStart := 0
		if before := strings.Join(sections, gchatChatSectionGap); before != "" {
			rowsStart = strings.Count(before, "\n") + strings.Count(gchatChatSectionGap, "\n")
		}
		rowButtons = gchatBoardButtons(m.Rows, rowsStart)
		add(strings.TrimRight(gchatChatRows(m.Rows), "\n"))
	}
	add(gchatChatAfter(m.After))
	if m.Fold != nil {
		add(gchatChatFold(m.Fold))
	}
	layout := gchatChatLayout{
		Text:    strings.Join(sections, gchatChatSectionGap),
		Widgets: gchatButtonList(append(gchatChatButtons(m.Choices, m.Links), rowButtons...)),
	}
	if layout.Text == "" && len(layout.Widgets) == 0 {
		return gchatChatLayout{}, errGchatChatEmpty
	}
	if len(layout.Text) > gchatChatTextMax {
		return gchatChatLayout{}, errGchatChatTooLarge
	}
	return layout, nil
}

// gchatChatHead is the headline in bold with the note after it, as the Slack
// renderer sets them. Both are plain by the spec, so they take the defang and
// nothing else (a mention in a headline must not ping).
func gchatChatHead(m *chatmsg.Message) string {
	var head []string
	if m.Headline != "" {
		head = append(head, gchatBoldLine(defangGchatControls(m.Headline)))
	}
	if m.Note != "" {
		head = append(head, defangGchatControls(m.Note))
	}
	return strings.Join(head, " ")
}

// gchatBoldLine wraps a plain line (a headline, a fold's title) in Chat's
// bold mark. A line that holds a star of its own (`kube-*`, `2**20`,
// `*.yaml`) is left as written, since the added marks would pair with it and
// Chat would show a plain text as part bold with a stray star, as
// slackBoldLine leaves such a heading plain in the markdown pass.
func gchatBoldLine(text string) string {
	if strings.Contains(text, gchatChatBold) {
		return text
	}
	return gchatChatBold + text + gchatChatBold
}

// gchatChatRows is the rows as a bulleted list: the severity as inline code
// ahead of the text, the detail after it on the same line. When a row
// carries buttons, every row is numbered in the bullet's place, since the
// buttons sit under the whole message and name their row by its number
// (gchat_board.go).
func gchatChatRows(rows []chatmsg.Row) string {
	var lines []string
	numbered := slackChatRowsAct(rows)
	drawn := 0
	for _, r := range rows {
		// A row that was a rule is nothing after the pass, and draws no
		// empty bullet and takes no number (gchatChatRowText).
		text, ok := gchatChatRowText(r)
		if !ok {
			continue
		}
		drawn++
		line := gchatChatRowBullet
		if numbered {
			line = fmt.Sprintf(gchatBoardRowMarker, drawn)
		}
		if r.Severity != "" {
			line += gchatChatCode + r.Severity + gchatChatCode + " "
		}
		line += text
		// Measured after the pass, as the text is: a detail that was a rule
		// is nothing, and draws no separator.
		if detail := strings.TrimSpace(toGchatText(r.Detail)); detail != "" {
			line += gchatChatRowDetailSep + detail
		}
		lines = append(lines, line)
	}
	return strings.Join(lines, "\n")
}

// gchatChatRowText is a row's text as drawn, and whether the row is drawn
// at all: a row the model wrote as a list item keeps its marker through
// the line pass (Chat's bullet is the renderer's), so the marker is shed
// before the renderer's goes on; a row that was a rule is nothing after the
// pass and, with no severity, is not drawn. The row buttons count drawn
// rows the same way (gchatBoardButtons), so a number names the same row in
// the text and on its buttons.
func gchatChatRowText(r chatmsg.Row) (text string, drawn bool) {
	text = strings.TrimPrefix(strings.TrimSpace(toGchatText(r.Text)), gchatChatRowBullet)
	return text, text != "" || r.Severity != ""
}

// gchatChatAfter is the lines after the rows, one each.
func gchatChatAfter(after []string) string {
	var lines []string
	for _, line := range after {
		if strings.TrimSpace(line) != "" {
			lines = append(lines, toGchatText(line))
		}
	}
	return strings.Join(lines, "\n")
}

// gchatChatFold is the fold, open: Chat has no collapsible section in a text
// message, so its title leads in bold, then its text, then its table as a
// monospace block with its columns lined up (renderTableCells, the padder a
// pipe table in prose ends in), the column names as its first row.
func gchatChatFold(f *chatmsg.Fold) string {
	if strings.TrimSpace(f.Text) == "" && (f.Table == nil || len(f.Table.Columns) == 0) {
		return ""
	}
	title := f.Title
	if title == "" {
		title = chatmsg.DefaultFoldTitle
	}
	parts := []string{gchatBoldLine(defangGchatControls(gchatOneLine(title)))}
	if strings.TrimSpace(f.Text) != "" {
		parts = append(parts, toGchatText(f.Text))
	}
	if f.Table != nil && len(f.Table.Columns) > 0 {
		rows := [][]string{gchatChatTableCells(f.Table.Columns)}
		for _, r := range f.Table.Rows {
			rows = append(rows, gchatChatTableCells(r))
		}
		parts = append(parts, strings.Join(renderTableCells(rows), "\n"))
	}
	return strings.Join(parts, "\n")
}

// gchatChatTableCells is one table row as the block draws it: each cell as
// written, flattened to one line, the defang keeping a mention in one inert.
// Cells are plain by the spec, so no markdown reader touches them.
func gchatChatTableCells(cells []string) []string {
	out := make([]string, len(cells))
	for i, c := range cells {
		out[i] = defangGchatControls(gchatOneLine(c))
	}
	return out
}

// gchatChatPlan is the plan, one line per item led by its status glyph, its
// steps under it marked done but the last of a running item, which is the
// open one. A running or failed item's title already shows its latest note
// (the emitter folds the item's note into its title), and that note is also
// its last step. Slack keeps the steps behind the task card's arrow; Chat
// draws them in the open, so a last step the title holds whole
// (gchatStepIsTheNote) is left out and the title alone says where the card
// is, with the steps before it as history; a step the title clipped stays,
// so the note's tail is not lost. A complete item is drawn whole: its title
// is its result, and its steps are history. The plan's title is the fallback
// text's, and draws nothing, as on Slack.
func gchatChatPlan(p *chatmsg.Plan) string {
	var lines []string
	for _, item := range p.Items {
		glyph := gchatChatStatusGlyph[item.Status]
		title := defangGchatControls(gchatOneLine(item.Title))
		if glyph != "" {
			title = glyph + " " + title
		}
		lines = append(lines, strings.TrimSpace(title))
		steps := item.Steps
		open := item.Status == chatmsg.StatusInProgress
		if (open || item.Status == chatmsg.StatusError) && gchatStepIsTheNote(item) {
			steps = steps[:len(steps)-1]
			open = false
		}
		for i, step := range steps {
			mark := gchatChatStepDone
			if open && i == len(steps)-1 {
				mark = gchatChatStepOpen
			}
			lines = append(lines, gchatChatIndent+mark+" "+defangGchatControls(step))
		}
	}
	return strings.Join(lines, "\n")
}

// gchatStepIsTheNote reports whether an item's last step is the note its
// title shows whole: the step equals the item's note as the emitter sent it
// beside the title (or the title itself, from an emitter that sent no note),
// and the title contains the step. The emitter clips a title shorter than a
// step (256, less the card's name in a plan of several), so a long note's
// title is a prefix of it; the step is then the only whole copy and stays.
// A note past the step's own clip (300) is cut by two different helpers for
// the step and for the note, which can differ by a trailing comma or space;
// the two then compare unequal and both lines draw, which the title check
// would decide the same way, so the emitter is left alone. All compared as
// sent, before the layout's defang.
func gchatStepIsTheNote(item chatmsg.PlanItem) bool {
	if len(item.Steps) == 0 {
		return false
	}
	last := item.Steps[len(item.Steps)-1]
	// The title as drawn: the emitter folds the note into the title with its
	// raw whitespace (a newline, a run of spaces) while it collapses the step
	// and the note to one line, so the comparison reads the one-line title the
	// member sees, not the raw field.
	title := gchatOneLine(item.Title)
	shown := title
	if item.Note != "" {
		shown = item.Note
	}
	return last == shown && strings.Contains(title, last)
}

// gchatChatWidgets is the buttons as one accessory button list, choices
// before links (gchatChatButtons).
func gchatChatWidgets(choices []chatmsg.Choice, links []chatmsg.Link) []map[string]any {
	return gchatButtonList(gchatChatButtons(choices, links))
}

// gchatButtonList is buttons as one accessory button list, nil for none.
func gchatButtonList(buttons []map[string]any) []map[string]any {
	if len(buttons) == 0 {
		return nil
	}
	return []map[string]any{{gchatButtonListKey: map[string]any{gchatButtonsKey: buttons}}}
}

// gchatChatButtons is the choice and link buttons, choices before links. A
// choice's click sends gchatChatFunction with the choice's reply (its text
// when it has none) and index, which the adapter turns into the clicker's
// message; a link button opens its url. The one primary entry is drawn
// filled.
func gchatChatButtons(choices []chatmsg.Choice, links []chatmsg.Link) []map[string]any {
	var buttons []map[string]any
	for i, c := range choices {
		reply := c.Reply
		if reply == "" {
			reply = c.Text
		}
		b := gchatChatButton(c.Text, c.Primary)
		b["onClick"] = map[string]any{"action": map[string]any{
			"function": gchatChatFunction,
			"parameters": []map[string]any{
				{"key": gchatChatParamReply, "value": reply},
				{"key": gchatChatParamChoice, "value": strconv.Itoa(i)},
			},
		}}
		buttons = append(buttons, b)
	}
	for _, l := range links {
		b := gchatChatButton(l.Text, l.Primary)
		b["onClick"] = map[string]any{"openLink": map[string]any{"url": l.URL}}
		buttons = append(buttons, b)
	}
	return buttons
}

// gchatHomeButtons marks each choice button of a home post's layout with
// the home parameter (gchatChatParamHome).
func gchatHomeButtons(layout gchatChatLayout) gchatChatLayout {
	for _, w := range layout.Widgets {
		list, _ := w["buttonList"].(map[string]any)
		buttons, _ := list["buttons"].([]map[string]any)
		for _, b := range buttons {
			click, _ := b["onClick"].(map[string]any)
			action, _ := click["action"].(map[string]any)
			params, ok := action["parameters"].([]map[string]any)
			if !ok {
				continue
			}
			action["parameters"] = append(params, map[string]any{"key": gchatChatParamHome, "value": gchatChatParamHomeValue})
		}
	}
	return layout
}

// gchatOneLine flattens a plain field the schema caps but does not hold to
// one line (a table cell, a column name, a plan item's title, a fold's
// title) onto the one line the layout draws it on: a line break inside a
// cell would break the row, and inside a title the bold marks.
func gchatOneLine(s string) string {
	return strings.Join(strings.Fields(s), " ")
}

func gchatChatButton(label string, primary bool) map[string]any {
	b := map[string]any{"text": label}
	if primary {
		b["type"] = gchatChatButtonFilled
	}
	return b
}
