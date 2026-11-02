package gateway

import (
	"errors"
	"strconv"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// The Google Chat renderer for kube-agents.chat/v1 (spec-a2a-payloads.md,
// "Chat messages"): one validated chat message laid out as a Chat text
// message, in the spec's fixed order -- headline and note, detail, rows,
// after, fold, then the buttons, choices before links -- with the buttons as
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
// markers, the bullet on a row.
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
	// one.
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
// widgets under it (empty when the message has no button).
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
	if m.Plan != nil && len(m.Plan.Items) > 0 {
		add(gchatChatPlan(m.Plan))
	} else {
		add(gchatChatRows(m.Rows))
	}
	add(gchatChatAfter(m.After))
	if m.Fold != nil {
		add(gchatChatFold(m.Fold))
	}
	layout := gchatChatLayout{
		Text:    strings.Join(sections, gchatChatSectionGap),
		Widgets: gchatChatWidgets(m.Choices, m.Links),
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
// ahead of the text, the detail after it on the same line.
func gchatChatRows(rows []chatmsg.Row) string {
	var lines []string
	for _, r := range rows {
		line := gchatChatRowBullet
		if r.Severity != "" {
			line += gchatChatCode + r.Severity + gchatChatCode + " "
		}
		// A row the model wrote as a list item keeps its marker through
		// the line pass (Chat's bullet is the renderer's), so the marker is
		// shed before the renderer's goes on; a row that was a rule is
		// nothing after the pass, and draws no empty bullet.
		text := strings.TrimPrefix(strings.TrimSpace(toGchatText(r.Text)), gchatChatRowBullet)
		line += text
		// Measured after the pass, as the text is: a detail that was a rule
		// is nothing, and draws no separator.
		if detail := strings.TrimSpace(toGchatText(r.Detail)); detail != "" {
			line += gchatChatRowDetailSep + detail
		}
		if text != "" || r.Severity != "" {
			lines = append(lines, line)
		}
	}
	return strings.Join(lines, "\n")
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
// open one. The plan's title is the fallback text's, and draws nothing, as
// on Slack.
func gchatChatPlan(p *chatmsg.Plan) string {
	var lines []string
	for _, item := range p.Items {
		glyph := gchatChatStatusGlyph[item.Status]
		title := defangGchatControls(gchatOneLine(item.Title))
		if glyph != "" {
			title = glyph + " " + title
		}
		lines = append(lines, strings.TrimSpace(title))
		for i, step := range item.Steps {
			mark := gchatChatStepDone
			if item.Status == chatmsg.StatusInProgress && i == len(item.Steps)-1 {
				mark = gchatChatStepOpen
			}
			lines = append(lines, gchatChatIndent+mark+" "+defangGchatControls(step))
		}
	}
	return strings.Join(lines, "\n")
}

// gchatChatWidgets is the buttons as one accessory button list, choices
// before links. A choice's click sends gchatChatFunction with the choice's
// reply (its text when it has none) and index, which the adapter turns into
// the clicker's message; a link button opens its url. The one primary entry
// is drawn filled.
func gchatChatWidgets(choices []chatmsg.Choice, links []chatmsg.Link) []map[string]any {
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
	if len(buttons) == 0 {
		return nil
	}
	return []map[string]any{{"buttonList": map[string]any{"buttons": buttons}}}
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
