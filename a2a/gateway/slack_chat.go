package gateway

import (
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/slack-go/slack"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
	"github.com/gke-labs/kube-agents/a2a/lib/chattext"
)

// The Slack renderer for kube-agents.chat/v1 (spec-a2a-payloads.md, "Chat
// messages"): one validated chat message laid out as Block Kit, in the
// spec's fixed order -- headline and note, detail, rows, after, fold, then
// the buttons, choices before links; in a progress message the plan follows
// the headline in the rows' place. The layout is the today presenter's
// (agents/platform/scripts/slack_presenter.py blocks_report and with_side_bar,
// slack_status.py plan_blocks), so a message posted under next reads as the
// one the Hermes presenter posts under today. It lays out and never composes:
// every word comes from the message, and the only things it adds are its
// backend's own -- the step markers on a plan row, the side bar's colour.
const (
	// slackChatActionPrefix, slackChatChoiceAction and slackChatLinkAction
	// build a button's action id, <prefix>.choice.<n> or <prefix>.link.<n>,
	// the presenter's own scheme (CHOICE_ACTION_ID_PATTERN and
	// LINK_ACTION_ID_PATTERN), so a click handler matches either path's
	// buttons alike.
	slackChatActionPrefix = "kage"
	slackChatChoiceAction = "choice"
	slackChatLinkAction   = "link"
	// slackChatButtonsPerRow is the presenter's BUTTONS_PER_ROW: Slack allows
	// 25 per actions block, but a row past 5 wraps badly on mobile.
	slackChatButtonsPerRow = 5
	// slackChatPrimaryStyle draws a button filled in Slack's accent colour.
	slackChatPrimaryStyle = "primary"
	// slackChatSideBarFrom is the presenter's SIDE_BAR_FROM: how many
	// leading blocks stay above the side bar, so the headline is the
	// message's own and the bar holds the rest.
	slackChatSideBarFrom = 1
	// slackChatSectionMax is Slack's cap on a section's text; a fold's text
	// is cut into sections under it, and a fold that cannot be is a message
	// Slack cannot hold, which posts as its fallback.
	slackChatSectionMax = 3000
	// slackChatFoldChunk is the raw-text share of one fold section, under
	// slackChatSectionMax with room for the escaping toMrkdwn adds.
	slackChatFoldChunk = 2500
	// slackChatContainerChildMax is Slack's cap on a container's children.
	slackChatContainerChildMax = 10
	// slackChatBlocksMax is Slack's cap on the blocks in one message, the
	// side bar's included.
	slackChatBlocksMax = 50
	// slackChatTextMax bounds the message text sent beside the blocks (the
	// notification, and what a read-back of the thread sees). Slack refuses
	// a text past 40,000 characters; a fallback longer than this posts as
	// chunked text instead.
	slackChatTextMax = 39000
	// slackChatStepDone and slackChatStepOpen mark a plan row's steps, as
	// slack_status.py's STEP_DONE and STEP_OPEN do: the last step of a
	// running row is the open one.
	slackChatStepDone = "✓"
	slackChatStepOpen = "◌"
	// slackChatPlanCountTitle is slack_status.py's CARDS_TITLE, the plan's
	// title when the message names none and it has more than one item.
	slackChatPlanCountTitle = "%d cards"
	// slackChatRawTextCell is the Slack table cell type that shows its text
	// as written.
	slackChatRawTextCell = "raw_text"
	// slackChatMrkdwn is the text object type Slack parses as mrkdwn; the
	// renderer sends it verbatim, so Slack links and mentions nothing the
	// text does not spell out as <url|label>.
	slackChatMrkdwn = "mrkdwn"
)

// slackChatColors are the side-bar colours by tone: the presenter's
// SIDE_BAR_GREEN and SIDE_BAR_YELLOW, and the spec's critical red.
var slackChatColors = map[string]string{
	chatmsg.ToneSuccess:   "#2EB67D",
	chatmsg.ToneAttention: "#ECB22E",
	chatmsg.ToneCritical:  "#E01E5A",
}

// errSlackChatTooLarge is a valid chat message that is more than one Slack
// message holds: the fold's text past its sections, the blocks past Slack's
// count, the fallback past the text cap. The caller posts the fallback.
var errSlackChatTooLarge = errors.New("the chat message is more than one Slack message holds")

// slackChatLayout is one rendered message: the top-level blocks, and the
// side bar's attachment when the message has a tone and something below the
// headline. Attachments is always set, empty when there is no bar, because
// chat.update keeps a message's attachments unless it is sent some.
type slackChatLayout struct {
	Blocks      []map[string]any `json:"blocks"`
	Attachments []map[string]any `json:"attachments"`
}

// renderSlackChat lays m out as Block Kit, with text (the fallback, already
// mrkdwn) as the side bar's fallback. An error is a message the renderer
// will not send -- too large for Slack, or a mention or an interactive
// element where the renderer put none (refuseSlackChat) -- and the caller
// posts the fallback instead.
func renderSlackChat(m *chatmsg.Message, text string) (slackChatLayout, error) {
	var blocks []map[string]any
	if head := slackChatHead(m); head != nil {
		blocks = append(blocks, head)
	}
	if detail := slackChatDetail(m.Detail); detail != nil {
		blocks = append(blocks, detail)
	}
	if m.Plan != nil && len(m.Plan.Items) > 0 {
		blocks = append(blocks, slackChatPlan(m.Plan))
	} else if rows := slackChatRows(m.Rows); rows != nil {
		blocks = append(blocks, slackChatDivider(), rows, slackChatDivider())
	}
	if after := slackChatAfter(m.After); after != nil {
		blocks = append(blocks, after)
	}
	if m.Fold != nil {
		fold, err := slackChatFold(m.Fold)
		if err != nil {
			return slackChatLayout{}, err
		}
		blocks = append(blocks, fold)
	}
	blocks = append(blocks, slackChatActions(m.Choices, m.Links)...)
	if len(blocks) > slackChatBlocksMax {
		return slackChatLayout{}, errSlackChatTooLarge
	}
	if err := refuseSlackChat(blocks); err != nil {
		return slackChatLayout{}, err
	}
	layout := slackChatLayout{Blocks: blocks, Attachments: []map[string]any{}}
	if color, ok := slackChatColors[m.Tone]; ok && len(blocks) > slackChatSideBarFrom {
		layout.Blocks = blocks[:slackChatSideBarFrom]
		layout.Attachments = []map[string]any{{
			"color":    color,
			"fallback": text,
			"blocks":   blocks[slackChatSideBarFrom:],
		}}
	}
	return layout, nil
}

// refuseSlackChat is the last check before a rendered message is sent. The
// only text Slack parses for mentions is a mrkdwn text object (rich_text
// text, plain_text and a task card's title are shown as written, so a field
// carrying <!here> there is inert text), and the renderer writes mrkdwn only
// for a fold's text, escaped (toMrkdwn) and verbatim. blocksMention, the
// notify route's own check, reads every mrkdwn text object, so the two Block
// Kit paths hold one bound; and blocksInteractive finds no interactive
// element outside the actions blocks the renderer wrote itself from the
// message's choices and links. Either firing means a field reached Slack in
// a form it would act on, and the fallback is the safe answer.
func refuseSlackChat(blocks []map[string]any) error {
	// The checks walk decoded JSON (map[string]any, []any, string), so the
	// layout is read back the way the notify route reads its request.
	raw, err := json.Marshal(blocks)
	if err != nil {
		return err
	}
	var all []any
	if err := json.Unmarshal(raw, &all); err != nil {
		return err
	}
	var content []any
	for _, b := range all {
		if block, _ := b.(map[string]any); block["type"] != "actions" {
			content = append(content, b)
		}
	}
	if why := blocksMention(slackMrkdwnTexts(all, nil)); why != "" {
		return fmt.Errorf("the rendered blocks carry a mention (%s)", why)
	}
	if kind := blocksInteractive(content); kind != "" {
		return fmt.Errorf("the rendered blocks carry an interactive element outside the buttons (type %s)", kind)
	}
	return nil
}

// slackMrkdwnTexts appends the text of every mrkdwn text object in v.
func slackMrkdwnTexts(v any, out []any) []any {
	switch node := v.(type) {
	case []any:
		for _, item := range node {
			out = slackMrkdwnTexts(item, out)
		}
	case map[string]any:
		if node["type"] == slackChatMrkdwn {
			out = append(out, node["text"])
		}
		for _, item := range node {
			out = slackMrkdwnTexts(item, out)
		}
	}
	return out
}

// slackChatHead is the headline, bold, with the note after it on the same
// line (blocks_report's head). It is the one block above the side bar.
func slackChatHead(m *chatmsg.Message) map[string]any {
	var head []map[string]any
	if m.Headline != "" {
		head = append(head, slackRichText(m.Headline, map[string]any{"bold": true}))
	}
	if m.Note != "" {
		head = append(head, slackRichText(" "+m.Note, nil))
	}
	if len(head) == 0 {
		return nil
	}
	return slackRichBlock([]map[string]any{slackRichSection(head)})
}

// slackChatDetail is the detail, a block of its own under the head, so a
// message with a tone carries it inside the side bar, as the presenter's
// subline under a question or an opened PR is.
func slackChatDetail(detail string) map[string]any {
	if strings.TrimSpace(detail) == "" {
		return nil
	}
	return slackRichBlock([]map[string]any{slackRichSection(slackRichMarkdown(detail))})
}

// slackChatRows is the rows, one section each: the severity as inline code
// ahead of the text, the detail on the line under it (blocks_report's
// _rich_row, which adds no bullet).
func slackChatRows(rows []chatmsg.Row) map[string]any {
	var sections []map[string]any
	for _, r := range rows {
		var elements []map[string]any
		if r.Severity != "" {
			elements = append(elements, slackRichText(r.Severity, map[string]any{"code": true}), slackRichText(" ", nil))
		}
		elements = append(elements, slackRichMarkdown(r.Text)...)
		if r.Detail != "" {
			elements = append(elements, slackRichText("\n", nil))
			elements = append(elements, slackRichMarkdown(r.Detail)...)
		}
		if len(elements) > 0 {
			sections = append(sections, slackRichSection(elements))
		}
	}
	if len(sections) == 0 {
		return nil
	}
	return slackRichBlock(sections)
}

// slackChatAfter is the lines after the rows, one section each.
func slackChatAfter(after []string) map[string]any {
	var sections []map[string]any
	for _, line := range after {
		if strings.TrimSpace(line) != "" {
			sections = append(sections, slackRichSection(slackRichMarkdown(line)))
		}
	}
	if len(sections) == 0 {
		return nil
	}
	return slackRichBlock(sections)
}

// slackChatFold is the fold as Slack's collapsible container, collapsed:
// its text as mrkdwn sections (toMrkdwn, the text path's escaping and
// translation), then its table as a native table block whose first row is
// the column names.
func slackChatFold(f *chatmsg.Fold) (map[string]any, error) {
	var children []map[string]any
	if strings.TrimSpace(f.Text) != "" {
		for _, chunk := range chatChunks(f.Text, slackChatFoldChunk) {
			text := toMrkdwn(chunk)
			if utf8.RuneCountInString(text) > slackChatSectionMax {
				return nil, errSlackChatTooLarge
			}
			children = append(children, map[string]any{
				"type": "section",
				"text": map[string]any{"type": slackChatMrkdwn, "text": text, "verbatim": true},
			})
		}
	}
	if f.Table != nil && len(f.Table.Columns) > 0 {
		rows := [][]map[string]any{slackChatTableRow(f.Table.Columns)}
		for _, r := range f.Table.Rows {
			rows = append(rows, slackChatTableRow(r))
		}
		children = append(children, map[string]any{"type": "table", "rows": rows})
	}
	if len(children) == 0 || len(children) > slackChatContainerChildMax {
		return nil, errSlackChatTooLarge
	}
	title := f.Title
	if title == "" {
		title = chatmsg.DefaultFoldTitle
	}
	return map[string]any{
		"type":              "container",
		"title":             map[string]any{"type": "plain_text", "text": title},
		"is_collapsible":    true,
		"default_collapsed": true,
		"child_blocks":      children,
	}, nil
}

func slackChatTableRow(cells []string) []map[string]any {
	row := make([]map[string]any, 0, len(cells))
	for _, c := range cells {
		row = append(row, map[string]any{"type": slackChatRawTextCell, "text": c})
	}
	return row
}

// slackChatPlan is the plan as Slack's plan block of task cards, as
// slack_status.plan_blocks lays it out: a row's title arrives composed, and
// its steps are listed under it, each marked done but the last of a running
// row, which is the open one.
func slackChatPlan(p *chatmsg.Plan) map[string]any {
	tasks := make([]map[string]any, 0, len(p.Items))
	for _, item := range p.Items {
		card := map[string]any{
			"type":    "task_card",
			"task_id": item.ID,
			"title":   item.Title,
			"status":  item.Status,
		}
		if len(item.Steps) > 0 {
			steps := make([]map[string]any, 0, len(item.Steps))
			for i, step := range item.Steps {
				mark := slackChatStepDone
				if item.Status == chatmsg.StatusInProgress && i == len(item.Steps)-1 {
					mark = slackChatStepOpen
				}
				steps = append(steps, slackRichSection([]map[string]any{slackRichText(mark+" "+step, nil)}))
			}
			card["details"] = slackRichBlock([]map[string]any{{
				"type":     "rich_text_list",
				"style":    "bullet",
				"elements": steps,
			}})
		}
		tasks = append(tasks, card)
	}
	title := p.Title
	switch {
	case title != "":
	case len(p.Items) == 1:
		title = p.Items[0].Title
	default:
		title = fmt.Sprintf(slackChatPlanCountTitle, len(p.Items))
	}
	return map[string]any{"type": "plan", "title": title, "tasks": tasks}
}

// slackChatActions is the buttons, choices before links, five to a row. A
// choice's value is what a click sends (its reply, else its text; PR 4
// answers the click); a link button opens its url. The one primary entry is
// drawn as Slack's primary button.
func slackChatActions(choices []chatmsg.Choice, links []chatmsg.Link) []map[string]any {
	var buttons []map[string]any
	for i, c := range choices {
		value := c.Reply
		if value == "" {
			value = c.Text
		}
		b := slackChatButton(c.Text, slackChatChoiceAction, i, c.Primary)
		b["value"] = value
		buttons = append(buttons, b)
	}
	for i, l := range links {
		b := slackChatButton(l.Text, slackChatLinkAction, i, l.Primary)
		b["url"] = l.URL
		buttons = append(buttons, b)
	}
	var blocks []map[string]any
	for start := 0; start < len(buttons); start += slackChatButtonsPerRow {
		end := min(start+slackChatButtonsPerRow, len(buttons))
		blocks = append(blocks, map[string]any{"type": "actions", "elements": buttons[start:end]})
	}
	return blocks
}

func slackChatButton(label, action string, i int, primary bool) map[string]any {
	b := map[string]any{
		"type":      "button",
		"text":      map[string]any{"type": "plain_text", "text": label, "emoji": true},
		"action_id": slackChatActionPrefix + "." + action + "." + strconv.Itoa(i),
	}
	if primary {
		b["style"] = slackChatPrimaryStyle
	}
	return b
}

func slackChatDivider() map[string]any { return map[string]any{"type": "divider"} }

func slackRichBlock(sections []map[string]any) map[string]any {
	return map[string]any{"type": "rich_text", "elements": sections}
}

func slackRichSection(elements []map[string]any) map[string]any {
	return map[string]any{"type": "rich_text_section", "elements": elements}
}

func slackRichText(text string, style map[string]any) map[string]any {
	e := map[string]any{"type": "text", "text": text}
	if style != nil {
		e["style"] = style
	}
	return e
}

// slackRichMarkdown is one field of markdown as rich_text elements, the
// presenter's _rich_elements: code spans, bold, italic, strike and links
// kept, the rest text. rich_text text is literal -- Slack parses no mrkdwn
// and no mention in it -- so nothing here escapes. A link renders as one
// only when its url passes the links rule, the label check included
// (chatmsg.SafeURL, chattext.LinkLabelMisnamesHost); any other shows its
// label.
func slackRichMarkdown(md string) []map[string]any {
	var out []map[string]any
	var text strings.Builder
	flush := func() {
		if text.Len() > 0 {
			out = append(out, slackRichText(text.String(), nil))
			text.Reset()
		}
	}
	for i := 0; i < len(md); {
		if span, n, ok := slackRichSpan(md, i); ok {
			flush()
			out = append(out, span)
			i += n
			continue
		}
		_, size := utf8.DecodeRuneInString(md[i:])
		text.WriteString(md[i : i+size])
		i += size
	}
	flush()
	return out
}

// slackRichSpan reads one span opening at md[i]: a code span, a bold pair,
// a link, an italic or a strike, in that order. ok is false when none opens
// there. The delimiter rules are the presenter's RICH_SPAN: an emphasis
// opener has no word character or marker of its kind before it and no space
// after it, a closer no space before it and no word character after it.
func slackRichSpan(md string, i int) (span map[string]any, n int, ok bool) {
	rest := md[i:]
	switch {
	case strings.HasPrefix(rest, "`"):
		if end := strings.IndexByte(rest[1:], '`'); end > 0 && !strings.Contains(rest[1:1+end], "\n") {
			return slackRichText(rest[1:1+end], map[string]any{"code": true}), end + 2, true
		}
	case strings.HasPrefix(rest, "**"):
		if m := mdBoldRE.FindStringSubmatchIndex(rest); m != nil && m[0] == 0 && m[2] == m[3] && m[6] == m[7] &&
			!slackWordBefore(md, i) {
			return slackRichText(rest[m[4]:m[5]], map[string]any{"bold": true}), m[1], true
		}
	case strings.HasPrefix(rest, "["):
		if m := slackLinkRE.FindStringSubmatchIndex(rest); m != nil && m[0] == 0 {
			label, dest := rest[m[2]:m[3]], rest[m[4]:m[5]]
			if chatmsg.SafeURL(dest) == nil && !chattext.LinkLabelMisnamesHost(label, dest) {
				return map[string]any{"type": "link", "url": dest, "text": label}, m[1], true
			}
			return slackRichText(label, nil), m[1], true
		}
	}
	for _, mark := range []struct {
		delim string
		style string
	}{{"*", "italic"}, {"_", "italic"}, {"~", "strike"}} {
		if !strings.HasPrefix(rest, mark.delim) {
			continue
		}
		if content, n, ok := slackEmphasis(md, i, mark.delim); ok {
			return slackRichText(content, map[string]any{mark.style: true}), n, true
		}
	}
	return nil, 0, false
}

// slackEmphasis reads a one-character emphasis span opening at md[i].
func slackEmphasis(md string, i int, delim string) (string, int, bool) {
	if slackWordBefore(md, i) || (i > 0 && md[i-1] == delim[0]) {
		return "", 0, false
	}
	body := md[i+1:]
	if body == "" || body[0] == ' ' || body[0] == '.' || body[0] == delim[0] {
		return "", 0, false
	}
	for j := 1; j < len(body); j++ {
		switch body[j] {
		case '\n':
			return "", 0, false
		case delim[0]:
			if body[j-1] == ' ' {
				continue
			}
			after := i + 1 + j + 1
			if after < len(md) && (md[after] == delim[0] || slackWordAt(md, after)) {
				continue
			}
			return body[:j], j + 2, true
		}
	}
	return "", 0, false
}

func slackWordBefore(s string, i int) bool {
	if i == 0 {
		return false
	}
	r, _ := utf8.DecodeLastRuneInString(s[:i])
	return unicode.IsLetter(r) || unicode.IsDigit(r) || r == '_'
}

func slackWordAt(s string, i int) bool {
	r, _ := utf8.DecodeRuneInString(s[i:])
	return unicode.IsLetter(r) || unicode.IsDigit(r) || r == '_'
}

// slackRawBlock sends a rendered block exactly as the renderer wrote it.
// slack-go's typed blocks would drop a field they do not model (the plan's
// task cards, the container's children), so the layout goes on the wire as
// the JSON the golden files hold.
type slackRawBlock struct {
	kind string
	raw  json.RawMessage
}

func (b slackRawBlock) BlockType() slack.MessageBlockType { return slack.MessageBlockType(b.kind) }
func (b slackRawBlock) ID() string                        { return "" }
func (b slackRawBlock) MarshalJSON() ([]byte, error)      { return b.raw, nil }

// slackChatOptions turns a layout into the message options PostMessage and
// UpdateMessage take: the text, the blocks, and the side bar (an empty list
// when there is none, which clears one a previous version of the message
// had).
func slackChatOptions(layout slackChatLayout, text string) ([]slack.MsgOption, error) {
	blocks, err := slackRawBlocks(layout.Blocks)
	if err != nil {
		return nil, err
	}
	attachments := make([]slack.Attachment, 0, len(layout.Attachments))
	for _, a := range layout.Attachments {
		inner, err := slackRawBlocks(a["blocks"].([]map[string]any))
		if err != nil {
			return nil, err
		}
		color, _ := a["color"].(string)
		fallback, _ := a["fallback"].(string)
		attachments = append(attachments, slack.Attachment{Color: color, Fallback: fallback, Blocks: slack.Blocks{BlockSet: inner}})
	}
	return []slack.MsgOption{
		slack.MsgOptionText(text, false),
		slack.MsgOptionBlocks(blocks...),
		slack.MsgOptionAttachments(attachments...),
	}, nil
}

func slackRawBlocks(blocks []map[string]any) ([]slack.Block, error) {
	out := make([]slack.Block, 0, len(blocks))
	for _, b := range blocks {
		raw, err := json.Marshal(b)
		if err != nil {
			return nil, err
		}
		kind, _ := b["type"].(string)
		out = append(out, slackRawBlock{kind: kind, raw: raw})
	}
	return out, nil
}
