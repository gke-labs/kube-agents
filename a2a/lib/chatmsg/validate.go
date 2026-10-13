package chatmsg

import (
	"fmt"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib/chattext"
)

// Caps, in characters: Unicode code points of the raw value, before any
// escaping. The headline, row, button, url and plan caps are the today
// presenter's (slack_presenter.py, slack_status.py); the fold text cap is
// the Slack answer fold's; the rest sit inside Slack's own limits.
const (
	headlineMax    = 150
	noteMax        = 300
	detailMax      = 2000
	rowsMax        = 20
	rowTextMax     = 300
	afterMax       = 5
	afterLineMax   = 300
	foldTitleMax   = 40
	foldTextMax    = 12000
	columnsMax     = 10
	columnMax      = 40
	tableRowsMax   = 50
	cellMax        = 200
	choicesMax     = 10
	buttonTextMax  = 75
	replyMax       = 400
	linksMax       = 10
	urlMax         = 3000
	planTitleMax   = 256
	planItemsMax   = 20
	itemIDMax      = 64
	itemTitleMax   = 256
	stepsMax       = 6
	stepMax        = 300
	itemNoteMax    = 300
	itemResultMax  = 256
	urlSchemeHTTP  = "http"
	urlSchemeHTTPS = "https"
	authorityStart = "://"
	authorityEnds  = "/?#"
	backslash      = `\`
	userinfoMark   = "@"
	portMark       = ":"
	ipv6Open       = "["
	ipv6Close      = "]"
	lineBreaks     = "\r\n"
)

// tokenRE is a severity or a mark's ask: 1-16 characters of [a-z0-9-].
var tokenRE = regexp.MustCompile(`^[a-z0-9-]{1,16}$`)

var (
	tones    = map[string]bool{ToneSuccess: true, ToneAttention: true, ToneCritical: true}
	statuses = map[string]bool{StatusPending: true, StatusInProgress: true, StatusComplete: true, StatusError: true}
	settles  = map[string]bool{SettleDone: true, SettleBlocked: true, SettleFailed: true}
)

// RuleError is one broken rule: the field it is about, as a path into the
// message (rows[2].text), and what is wrong with it.
type RuleError struct {
	Field string
	Rule  string
}

func (e *RuleError) Error() string {
	return e.Field + ": " + e.Rule
}

func broke(field, format string, args ...any) error {
	return &RuleError{Field: field, Rule: fmt.Sprintf(format, args...)}
}

// Validate checks m against every rule in the Chat messages section for
// carrier c. layout is the first broken rule outside mark, and a renderer
// given one drops the whole message and posts the fallback; mark is a
// broken mark, and drops the mark alone. On a terminal status-update only
// the schema and the mark are read, so only they are checked.
func Validate(m Message, c Carrier) (layout, mark error) {
	mark = m.markErr
	if mark == nil && m.Mark != nil {
		mark = validateMark(*m.Mark)
	}
	if m.Schema != Schema {
		return broke(schemaKey, "%q is not %q", m.Schema, Schema), mark
	}
	if !c.valid() {
		return broke("carrier", "%q is not a carrier", c), mark
	}
	if c == Terminal {
		return nil, mark
	}
	return validateLayout(m), mark
}

func validateLayout(m Message) error {
	if err := oneLine("headline", m.Headline, headlineMax); err != nil {
		return err
	}
	if err := oneLine("note", m.Note, noteMax); err != nil {
		return err
	}
	if err := capped("detail", m.Detail, detailMax); err != nil {
		return err
	}
	if m.Tone != "" && !tones[m.Tone] {
		return broke("tone", "%q is not success, attention or critical", m.Tone)
	}
	if err := validateRows(m.Rows); err != nil {
		return err
	}
	if len(m.After) > afterMax {
		return broke("after", "%d lines, at most %d", len(m.After), afterMax)
	}
	for i, line := range m.After {
		if err := oneLine(fmt.Sprintf("after[%d]", i), line, afterLineMax); err != nil {
			return err
		}
	}
	if m.Fold != nil {
		if err := validateFold(*m.Fold); err != nil {
			return err
		}
	}
	if err := validateChoices(m.Choices); err != nil {
		return err
	}
	if err := validateLinks(m.Links); err != nil {
		return err
	}
	if primaries(m) > 1 {
		return broke("primary", "on more than one entry across choices and links")
	}
	if m.Plan != nil {
		return validatePlan(*m.Plan)
	}
	return nil
}

func validateRows(rows []Row) error {
	if len(rows) > rowsMax {
		return broke("rows", "%d rows, at most %d", len(rows), rowsMax)
	}
	for i, row := range rows {
		field := fmt.Sprintf("rows[%d]", i)
		if row.Text == "" {
			return broke(field+".text", "empty")
		}
		if err := oneLine(field+".text", row.Text, rowTextMax); err != nil {
			return err
		}
		if row.Severity != "" && !tokenRE.MatchString(row.Severity) {
			return broke(field+".severity", "%q is not 1-16 characters of [a-z0-9-]", row.Severity)
		}
		if err := oneLine(field+".detail", row.Detail, rowTextMax); err != nil {
			return err
		}
	}
	return nil
}

func validateFold(f Fold) error {
	if err := capped("fold.title", f.Title, foldTitleMax); err != nil {
		return err
	}
	if err := capped("fold.text", f.Text, foldTextMax); err != nil {
		return err
	}
	if f.Table == nil {
		return nil
	}
	cols := f.Table.Columns
	if len(cols) == 0 || len(cols) > columnsMax {
		return broke("fold.table.columns", "%d columns, want 1-%d", len(cols), columnsMax)
	}
	for i, col := range cols {
		if err := between(fmt.Sprintf("fold.table.columns[%d]", i), col, columnMax); err != nil {
			return err
		}
	}
	if len(f.Table.Rows) > tableRowsMax {
		return broke("fold.table.rows", "%d rows, at most %d", len(f.Table.Rows), tableRowsMax)
	}
	for i, row := range f.Table.Rows {
		if len(row) != len(cols) {
			return broke(fmt.Sprintf("fold.table.rows[%d]", i), "%d cells, the table has %d columns", len(row), len(cols))
		}
		for j, cell := range row {
			if err := capped(fmt.Sprintf("fold.table.rows[%d][%d]", i, j), cell, cellMax); err != nil {
				return err
			}
		}
	}
	return nil
}

// validateChoices holds the choice rules, the interactive-command one
// included: a click posts the choice's reply (or text) as the clicker's own
// message, so one the gateway would act on as a command, on any route and
// in any task state, is refused.
func validateChoices(choices []Choice) error {
	if len(choices) > choicesMax {
		return broke("choices", "%d choices, at most %d", len(choices), choicesMax)
	}
	for i, ch := range choices {
		field := fmt.Sprintf("choices[%d]", i)
		if err := between(field+".text", ch.Text, buttonTextMax); err != nil {
			return err
		}
		if chattext.IsCommand(ch.Text) {
			return broke(field+".text", "%q is a gateway command", ch.Text)
		}
		if ch.Reply == "" {
			continue
		}
		if err := capped(field+".reply", ch.Reply, replyMax); err != nil {
			return err
		}
		if !strings.Contains(ch.Reply, ch.Text) {
			return broke(field+".reply", "does not contain the choice's text %q", ch.Text)
		}
		if chattext.IsCommand(ch.Reply) {
			return broke(field+".reply", "%q is a gateway command", ch.Reply)
		}
	}
	return nil
}

func validateLinks(links []Link) error {
	if len(links) > linksMax {
		return broke("links", "%d links, at most %d", len(links), linksMax)
	}
	for i, link := range links {
		field := fmt.Sprintf("links[%d]", i)
		if err := between(field+".text", link.Text, buttonTextMax); err != nil {
			return err
		}
		if err := SafeURL(link.URL); err != nil {
			return broke(field+".url", "%v", err)
		}
		if chattext.LinkLabelMisnamesHost(link.Text, link.URL) {
			return broke(field+".text", "names a host other than the url's")
		}
	}
	return nil
}

func primaries(m Message) int {
	n := 0
	for _, ch := range m.Choices {
		if ch.Primary {
			n++
		}
	}
	for _, link := range m.Links {
		if link.Primary {
			n++
		}
	}
	return n
}

func validatePlan(p Plan) error {
	if err := capped("plan.title", p.Title, planTitleMax); err != nil {
		return err
	}
	if len(p.Items) > planItemsMax {
		return broke("plan.items", "%d items, at most %d", len(p.Items), planItemsMax)
	}
	seen := make(map[string]bool, len(p.Items))
	for i, item := range p.Items {
		field := fmt.Sprintf("plan.items[%d]", i)
		if err := between(field+".id", item.ID, itemIDMax); err != nil {
			return err
		}
		if seen[item.ID] {
			return broke(field+".id", "%q is not unique in the plan", item.ID)
		}
		seen[item.ID] = true
		if err := capped(field+".title", item.Title, itemTitleMax); err != nil {
			return err
		}
		if !statuses[item.Status] {
			return broke(field+".status", "%q is not pending, in_progress, complete or error", item.Status)
		}
		if len(item.Steps) > stepsMax {
			return broke(field+".steps", "%d steps, at most %d", len(item.Steps), stepsMax)
		}
		for j, step := range item.Steps {
			if err := oneLine(fmt.Sprintf("%s.steps[%d]", field, j), step, stepMax); err != nil {
				return err
			}
		}
		if item.StepCount < 0 {
			return broke(field+".stepCount", "negative")
		}
		if err := capped(field+".note", item.Note, itemNoteMax); err != nil {
			return err
		}
		if err := capped(field+".result", item.Result, itemResultMax); err != nil {
			return err
		}
	}
	return nil
}

func validateMark(m Mark) error {
	if m.Ask != "" && !tokenRE.MatchString(m.Ask) {
		return broke("mark.ask", "%q is not 1-16 characters of [a-z0-9-]", m.Ask)
	}
	if m.Settle != "" && !settles[m.Settle] {
		return broke("mark.settle", "%q is not done, blocked or failed", m.Settle)
	}
	return nil
}

// SafeURL reports why raw may not back a link, or nil: http or https with a
// host, no userinfo, no whitespace or unprintable character, no backslash
// in the host part (a browser reads one as a slash), at most urlMax
// characters. It reads the url as the today presenter's _safe_link_url does
// (Python's urlsplit), splitting rather than parsing it, so a path escape
// or a port that does not parse refuses nothing the presenter accepts.
func SafeURL(raw string) error {
	if n := utf8.RuneCountInString(raw); n > urlMax {
		return fmt.Errorf("%d characters, at most %d", n, urlMax)
	}
	for _, r := range raw {
		if unicode.IsSpace(r) || !unicode.IsPrint(r) {
			return fmt.Errorf("carries whitespace or an unprintable character %U", r)
		}
	}
	scheme, rest, ok := strings.Cut(raw, authorityStart)
	if !ok {
		return fmt.Errorf("no %s after a scheme", authorityStart)
	}
	if scheme = strings.ToLower(scheme); scheme != urlSchemeHTTP && scheme != urlSchemeHTTPS {
		return fmt.Errorf("scheme %q is not http or https", scheme)
	}
	authority := rest
	if end := strings.IndexAny(authority, authorityEnds); end >= 0 {
		authority = authority[:end]
	}
	if strings.Contains(authority, backslash) {
		return fmt.Errorf("a backslash in the host part")
	}
	if strings.Contains(authority, userinfoMark) {
		return fmt.Errorf("carries userinfo")
	}
	if host(authority) == "" {
		return fmt.Errorf("has no host")
	}
	return nil
}

// host is authority without its port: a bracketed IPv6 literal's inside,
// else everything before the last colon.
func host(authority string) string {
	if strings.HasPrefix(authority, ipv6Open) {
		if end := strings.Index(authority, ipv6Close); end >= 0 {
			return authority[len(ipv6Open):end]
		}
		return ""
	}
	if colon := strings.LastIndex(authority, portMark); colon >= 0 {
		return authority[:colon]
	}
	return authority
}

// capped checks a field of any number of lines against its cap.
func capped(field, s string, limit int) error {
	if n := utf8.RuneCountInString(s); n > limit {
		return broke(field, "%d characters, at most %d", n, limit)
	}
	return nil
}

// oneLine checks a one-line field: capped, and no line break.
func oneLine(field, s string, limit int) error {
	if err := capped(field, s, limit); err != nil {
		return err
	}
	if strings.ContainsAny(s, lineBreaks) {
		return broke(field, "more than one line")
	}
	return nil
}

// between checks a field that must be present: 1 to limit characters.
func between(field, s string, limit int) error {
	if s == "" {
		return broke(field, "empty")
	}
	return capped(field, s, limit)
}
