package chatmsg

import (
	"fmt"
	"regexp"
	"strings"
)

// The chat UX rules code can check (.agents/rules/chat_ux.md), by the id a
// Finding carries: a card id or a raw command is "No plumbing in user
// text", a heading or a fence breaks "The answer comes first, as one bold
// line", and an apology "A failure is one plain line". The headline's
// length is not among them: Validate already refuses one past headlineMax,
// and the emitters cut a long first sentence at a clause to stay under it.
// Nor is "At most 5 buttons share a row", which the renderer keeps by
// wrapping (slackChatButtonsPerRow).
const (
	LintHeadlineCardID   = "headline-card-id"
	LintHeadlineHeading  = "headline-heading"
	LintHeadlineFence    = "headline-code-fence"
	LintHeadlineApology  = "headline-apology"
	LintChoiceRawCommand = "choice-raw-command"
)

// cardIDRE is a board card id as the kanban mints one, t_ and eight hex
// digits, which means nothing to the person reading the headline.
var cardIDRE = regexp.MustCompile(`\bt_[0-9a-f]{8}\b`)

// headingRE is a markdown heading's opening: one to six #, then a space,
// a tab or the end, the classes spelled out so Python's \s cannot differ.
// "#412" is a pull request, not a heading.
var headingRE = regexp.MustCompile(`^[ \t]*#{1,6}([ \t]|$)`)

// apologyRE is an opening that apologises before saying what happened:
// "Sorry", "I'm sorry", "We are so sorry", "My apologies", "I apologize",
// "Unfortunately".
var apologyRE = regexp.MustCompile(`(?i)^\W*(((i|we)(['’](m|re)|[ \t]+(am|are))[ \t]+)?((so|very|truly)[ \t]+)?sorry|(my[ \t]+)?apologies|(i|we)[ \t]+apologi[sz]e|unfortunately)\b`)

// codeFences are the markers that open a fenced code block.
var codeFences = []string{"```", "~~~"}

// slashMark opens a slash command, and codeMark a code span.
const (
	slashMark = "/"
	codeMark  = "`"
)

// cliWords are the command-line tools whose name, typed in lower case and
// followed by an argument, makes a choice's label a command to paste, not a
// decision to click. "Git history" and "Docker logs" are labels.
var cliWords = map[string]bool{
	"kubectl": true, "gcloud": true, "helm": true, "git": true, "gh": true,
	"terraform": true, "tofu": true, "docker": true,
}

// Finding is one chat UX rule a message breaks: the rule's id, the field,
// and what is wrong.
type Finding struct {
	Rule  string
	Field string
	What  string
}

func (f Finding) String() string {
	return f.Rule + " " + f.Field + ": " + f.What
}

// Lint reports every chat UX rule m breaks. Nothing in production calls it:
// a renderer refusing a message for its wording would drop the user's
// answer, so the emitters' tests and the shared cases hold them to it
// instead, and Validate keeps to what Slack cannot draw.
func Lint(m Message) []Finding {
	var out []Finding
	add := func(rule, field, format string, args ...any) {
		out = append(out, Finding{Rule: rule, Field: field, What: fmt.Sprintf(format, args...)})
	}
	if id := cardIDRE.FindString(m.Headline); id != "" {
		add(LintHeadlineCardID, "headline", "names card %s", id)
	}
	if headingRE.MatchString(m.Headline) {
		add(LintHeadlineHeading, "headline", "opens with a markdown heading")
	}
	for _, fence := range codeFences {
		if strings.Contains(m.Headline, fence) {
			add(LintHeadlineFence, "headline", "carries a code fence %s", fence)
			break
		}
	}
	if apologyRE.MatchString(m.Headline) {
		add(LintHeadlineApology, "headline", "opens with an apology")
	}
	for i, ch := range m.Choices {
		if why := rawCommand(ch.Text); why != "" {
			add(LintChoiceRawCommand, fmt.Sprintf("choices[%d].text", i), "%q %s", ch.Text, why)
		}
	}
	return out
}

// rawCommand is why label reads as a command rather than a choice, or "".
func rawCommand(label string) string {
	trimmed := strings.TrimSpace(label)
	switch {
	case strings.HasPrefix(trimmed, slashMark):
		return "is a slash command"
	case strings.Contains(trimmed, codeMark):
		return "carries a code span"
	}
	if fields := strings.Fields(trimmed); len(fields) > 1 && cliWords[fields[0]] {
		return "is a command line"
	}
	return ""
}
