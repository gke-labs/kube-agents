package gateway

import (
	"errors"
	"fmt"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// Chat messages on Teams (spec-chatops-gateway.md, "The Teams adapter"):
// a kube-agents.chat/v1 message is laid out as the Adaptive Card the
// adapter already draws for text, with the message's buttons under it.
//
// The renderer composes nothing. It writes the message's blocks in the
// schema's order (headline and note, detail, the plan or the rows, after,
// the fold) as the markdown teamsMarkdownToCard turns into a card's text
// blocks and tables, so a laid-out message and a text one look alike, and
// it draws a choice as an Action.Submit whose data carries the reply the
// press stands for (teams_clicks.go) and a link as an Action.OpenUrl. The
// fold is drawn open: a card's own show-and-hide is one more control, and
// Google Chat draws it open too. A row's Retry and Drop are not drawn; the
// board card is not served on Teams.
//
// A card that would not fit one activity is refused (ErrLayoutRefused), so
// the gateway posts the text: the fitting Post falls back to would send it
// as plain text and drop the buttons without saying so.
const (
	teamsActionOpenURL = "Action.OpenUrl"
	// teamsActionStyleKey and teamsActionPositive draw a primary button
	// filled.
	teamsActionStyleKey = "style"
	teamsActionPositive = "positive"
	// teamsPressActionChoice is the press of a choice; its data carries
	// the reply, the choice's index among the message's choices and its
	// label, which names the choice when the adapter no longer remembers
	// the message.
	teamsPressActionChoice = "choice"
	teamsPressReplyKey     = "reply"
	teamsPressChoiceKey    = "choice"
	teamsPressLabelKey     = "label"
	// teamsPressNextKey marks a next step's button: its press answers no
	// question, so nothing is posted for one the adapter cannot draw
	// again (teams_clicks.go).
	teamsPressNextKey = "next"

	teamsChatBold         = "**"
	teamsChatRowBullet    = "- "
	teamsChatRowDetailSep = " · "
	teamsChatSectionGap   = "\n\n"
	// teamsChatHeading opens a markdown heading line, which the card
	// builder would draw larger than the bold headline; the renderer
	// draws it as a bold line, as Google Chat does.
	teamsChatHeading = "#"
	// teamsChatIndent sets a plan's steps in from its title: braille
	// blanks, which draw as space and are not the Unicode white space the
	// card builder trims from each line.
	teamsChatIndent   = "\u2800\u2800"
	teamsChatStepDone = "✓"
	teamsChatStepOpen = "◌"
	// teamsChatCellPipe stands in for a pipe inside a table cell, which
	// would otherwise split the cell.
	teamsChatCellPipe = "｜"
	teamsChatTableSep = "---"
)

// teamsChatStatusGlyph is the mark in front of a plan item, by its status.
var teamsChatStatusGlyph = map[string]string{
	chatmsg.StatusPending:    "⏳",
	chatmsg.StatusInProgress: "⚙️",
	chatmsg.StatusComplete:   "✅",
	chatmsg.StatusError:      "❌",
}

var (
	errTeamsChatTooLarge = errors.New("the chat message is more than one Teams activity holds")
	errTeamsChatEmpty    = errors.New("the chat message lays nothing out")
)

// teamsChatLayout is a rendered chat message: the card's text, and its
// choice and link buttons in the schema's order.
type teamsChatLayout struct {
	Text    string
	Choices []map[string]any
	Links   []map[string]any
}

// renderTeamsChat lays m out, or says it lays nothing out.
func renderTeamsChat(m *chatmsg.Message) (teamsChatLayout, error) {
	var sections []string
	add := func(s string) {
		if strings.TrimSpace(s) != "" {
			sections = append(sections, strings.TrimRight(s, "\n"))
		}
	}
	add(teamsChatHead(m))
	add(teamsChatFlat(m.Detail))
	if m.Plan != nil && len(m.Plan.Items) > 0 {
		add(teamsChatPlan(m.Plan))
	} else {
		add(teamsChatRows(m.Rows))
	}
	add(teamsChatAfter(m.After))
	if m.Fold != nil {
		add(teamsChatFold(m.Fold))
	}
	layout := teamsChatLayout{
		Text:    strings.Join(sections, teamsChatSectionGap),
		Choices: teamsChoiceActions(m.Choices),
		Links:   teamsLinkActions(m.Links),
	}
	if layout.Text == "" && len(layout.Choices) == 0 && len(layout.Links) == 0 {
		return teamsChatLayout{}, errTeamsChatEmpty
	}
	return layout, nil
}

// teamsChatHead is the headline in bold with the note after it.
func teamsChatHead(m *chatmsg.Message) string {
	var head []string
	if headline := gchatOneLine(m.Headline); headline != "" {
		head = append(head, teamsBoldLine(headline))
	}
	if note := gchatOneLine(m.Note); note != "" {
		head = append(head, note)
	}
	return strings.Join(head, " ")
}

// teamsBoldLine is text in bold, left as it is when it carries the mark
// itself, which would otherwise close the bold early.
func teamsBoldLine(text string) string {
	if strings.Contains(text, teamsChatBold) {
		return text
	}
	return teamsChatBold + text + teamsChatBold
}

// teamsChatFlat is text with each markdown heading line outside a code
// fence drawn as a bold line, and a fence the text leaves open closed at
// its end, so the code block stops with the section that opened it.
func teamsChatFlat(text string) string {
	lines := strings.Split(text, "\n")
	fence := 0
	for i, line := range lines {
		stripped := strings.TrimSpace(line)
		if fence > 0 {
			if teamsClosesFence(stripped, fence) {
				fence = 0
			}
			continue
		}
		if n := teamsOpensFence(stripped); n > 0 {
			fence = n
			continue
		}
		title := strings.TrimLeft(stripped, teamsChatHeading)
		if title == stripped || !strings.HasPrefix(title, " ") || strings.TrimSpace(title) == "" {
			continue
		}
		lines[i] = teamsBoldLine(strings.TrimSpace(title))
	}
	if fence > 0 {
		lines = append(lines, strings.Repeat(teamsCodeTick, fence))
	}
	return strings.Join(lines, "\n")
}

// teamsChatRows is one bullet per row: its severity in bold (a card's text
// has no inline code), its text, and its detail after a separator.
func teamsChatRows(rows []chatmsg.Row) string {
	var lines []string
	for _, r := range rows {
		text := strings.TrimPrefix(gchatOneLine(r.Text), teamsChatRowBullet)
		if text == "" && r.Severity == "" {
			continue
		}
		line := teamsChatRowBullet
		if r.Severity != "" {
			line += teamsBoldLine(r.Severity) + " "
		}
		line += text
		if detail := gchatOneLine(r.Detail); detail != "" {
			line += teamsChatRowDetailSep + detail
		}
		lines = append(lines, line)
	}
	return strings.Join(lines, "\n")
}

// teamsChatAfter is the lines that follow the rows.
func teamsChatAfter(after []string) string {
	var lines []string
	for _, line := range after {
		if strings.TrimSpace(line) != "" {
			lines = append(lines, teamsChatFlat(line))
		}
	}
	return strings.Join(lines, "\n")
}

// teamsChatFold is the fold drawn open: its title in bold, its text, and
// its table as the card's own table.
func teamsChatFold(f *chatmsg.Fold) string {
	hasTable := f.Table != nil && len(f.Table.Columns) > 0
	if strings.TrimSpace(f.Text) == "" && !hasTable {
		return ""
	}
	title := gchatOneLine(f.Title)
	if title == "" {
		title = chatmsg.DefaultFoldTitle
	}
	parts := []string{teamsBoldLine(title)}
	if strings.TrimSpace(f.Text) != "" {
		parts = append(parts, strings.TrimRight(teamsChatFlat(f.Text), "\n"))
	}
	if hasTable {
		separator := make([]string, len(f.Table.Columns))
		for i := range separator {
			separator[i] = teamsChatTableSep
		}
		lines := []string{teamsChatTableLine(f.Table.Columns), teamsChatTableLine(separator)}
		for _, row := range f.Table.Rows {
			lines = append(lines, teamsChatTableLine(row))
		}
		// A blank line ends the text before the table, so the table's
		// first line is read as its header.
		parts = append(parts, "", strings.Join(lines, "\n"))
	}
	return strings.Join(parts, "\n")
}

// teamsChatTableLine is one table row as the markdown the card builder
// reads: plain one-line cells between pipes.
func teamsChatTableLine(cells []string) string {
	out := make([]string, len(cells))
	for i, c := range cells {
		out[i] = strings.ReplaceAll(gchatOneLine(c), teamsPipe, teamsChatCellPipe)
	}
	return teamsPipe + " " + strings.Join(out, " "+teamsPipe+" ") + " " + teamsPipe
}

// teamsChatPlan is the plan's items, each with its status mark and its
// steps under it: done steps ticked, the open one of a running item marked
// open. A step the item's title already shows is not drawn twice
// (gchatStepIsTheNote).
func teamsChatPlan(p *chatmsg.Plan) string {
	var lines []string
	for _, item := range p.Items {
		title := gchatOneLine(item.Title)
		if glyph := teamsChatStatusGlyph[item.Status]; glyph != "" {
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
			mark := teamsChatStepDone
			if open && i == len(steps)-1 {
				mark = teamsChatStepOpen
			}
			lines = append(lines, teamsChatIndent+mark+" "+gchatOneLine(step))
		}
	}
	return strings.Join(lines, "\n")
}

// teamsChoiceActions is one Action.Submit per choice, whose press arrives
// as the reply it carries (teams_clicks.go).
func teamsChoiceActions(choices []chatmsg.Choice) []map[string]any {
	var actions []map[string]any
	for i, c := range choices {
		reply := c.Reply
		if reply == "" {
			reply = c.Text
		}
		action := map[string]any{
			"type":  teamsActionSubmit,
			"title": c.Text,
			"data": map[string]any{
				teamsPressActionKey: teamsPressActionChoice,
				teamsPressReplyKey:  reply,
				teamsPressChoiceKey: i,
				teamsPressLabelKey:  c.Text,
			},
		}
		if c.Primary {
			action[teamsActionStyleKey] = teamsActionPositive
		}
		if c.Next {
			action["data"].(map[string]any)[teamsPressNextKey] = true
		}
		actions = append(actions, action)
	}
	return actions
}

// teamsLinkActions is one Action.OpenUrl per link.
func teamsLinkActions(links []chatmsg.Link) []map[string]any {
	var actions []map[string]any
	for _, l := range links {
		action := map[string]any{"type": teamsActionOpenURL, "title": l.Text, "url": l.URL}
		if l.Primary {
			action[teamsActionStyleKey] = teamsActionPositive
		}
		actions = append(actions, action)
	}
	return actions
}

// teamsChatCard is what one laid-out message shows: its text, its choice
// buttons, and the buttons a rewrite keeps (links, Stop, the rating
// controls).
type teamsChatCard struct {
	text    string
	choices []map[string]any
	rest    []map[string]any
	// closed marks a message that was a card and is text now: nothing is
	// drawn on it again.
	closed bool
}

// actions is the card's buttons in order: choices, then the rest.
func (c teamsChatCard) actions() []map[string]any {
	return append(append([]map[string]any{}, c.choices...), c.rest...)
}

// chatCard lays m's chat message out as the card messageID shows next
// (empty for a new message), with the Stop button of a running task's line
// and the rating controls of an answer marked for feedback, and refuses a
// layout that draws nothing or does not fit one activity. An answered
// question keeps its line through an edit that lays out no choices.
func (a *TeamsAdapter) chatCard(messageID string, m OutMessage) (teamsChatCard, error) {
	layout, err := renderTeamsChat(m.Chat)
	if err != nil {
		return teamsChatCard{}, fmt.Errorf("%w: %w", ErrLayoutRefused, err)
	}
	card := teamsChatCard{text: layout.Text, choices: layout.Choices, rest: layout.Links}
	if len(layout.Choices) == 0 {
		card.text = a.withAnsweredLine(messageID, card.text)
	}
	if m.Stop != "" {
		card.rest = append(card.rest, teamsStopActions(m.Stop)...)
	}
	if a.ratesTask(m.Feedback) {
		card.rest = append(card.rest, teamsRatingActions(m.Feedback, a.feedbackRecorder().KeptDays())...)
	}
	if !teamsFitsCard(card.text, card.actions()) {
		return teamsChatCard{}, fmt.Errorf("%w: %w", ErrLayoutRefused, errTeamsChatTooLarge)
	}
	return card, nil
}

// postChat posts m's chat message as a card and remembers what it shows.
func (a *TeamsAdapter) postChat(conversation string, m OutMessage) (string, error) {
	card, err := a.chatCard("", m)
	if err != nil {
		return "", err
	}
	id, err := a.post(conversation, card.text, card.actions())
	if err != nil {
		return "", err
	}
	a.rememberChat(id, conversation, m, card)
	return id, nil
}

// editChat edits messageID to m's chat message as a card, under the lock
// that orders a message's edits (EditMessage).
func (a *TeamsAdapter) editChat(conversation, messageID string, m OutMessage) error {
	card, err := a.chatCard(messageID, m)
	if err != nil {
		return err
	}
	if err := a.edit(conversation, messageID, card.text, card.actions()); err != nil {
		return err
	}
	a.rememberChat(messageID, conversation, m, card)
	return nil
}

// rememberChat records what a laid-out message shows, for the rewrites
// that follow it: a press of one of its choices, and a rating on it.
func (a *TeamsAdapter) rememberChat(id, conversation string, m OutMessage, card teamsChatCard) {
	if id == "" {
		return
	}
	a.trackQuestion(id, conversation, m.Chat, &card)
	if a.ratesTask(m.Feedback) {
		a.rememberRated(id, m.Feedback, card.text)
	}
}
