package gateway

import (
	"fmt"

	"github.com/gke-labs/kube-agents/a2a/lib/chattext"
)

// The words answer feedback shows on every chat backend (the approved mock,
// phase2/feedback). A backend supplies only its own markup: the link around
// feedbackFormURL, and how a control or a dialog is drawn.
const (
	feedbackGoodLabel  = "Good answer"
	feedbackBadLabel   = "Bad answer"
	feedbackNoteTitle  = "What went wrong?"
	feedbackNoteLabel  = "Anything else?"
	feedbackNoteSend   = "Send"
	feedbackNoteCancel = "Cancel"
	feedbackKeptLine   = "Your question and this answer are kept on your install for %s so an operator can turn them into a test."
	// feedbackKeptNoAsk is the kept line when the ask is no longer on
	// record (AskTTL, a reaped session), so only the answer is kept.
	feedbackKeptNoAsk  = "This answer is kept on your install for %s so an operator can turn it into a test."
	feedbackKeptDays   = "%d days"
	feedbackKeptOneDay = "1 day"
	feedbackThanksLine = "Thanks. I kept this answer for your team to review."
	// feedbackFormLine points the rater at the project's feedback form,
	// with nothing pre-filled: the install never files anything itself. Its
	// %s is the backend's own link markup around feedbackFormURL.
	feedbackFormLine = "To tell the kube-agents maintainers as well, use %s. It opens a public issue, so leave out cluster names and other details from your install."
	// feedbackFormURL is the form the chat commands name too.
	feedbackFormURL = chattext.FeedbackFormURL

	// The same words for a post the agent made on its own (an alert, a
	// report), which answers no question: it is rated useful or not, and
	// only the post is kept.
	feedbackPostGoodLabel  = "Useful"
	feedbackPostBadLabel   = "Not useful"
	feedbackPostNoteTitle  = "What made it not useful?"
	feedbackPostKeptLine   = "This post is kept on your install for %s so an operator can turn it into a test."
	feedbackPostThanksLine = "Thanks. I kept this post for your team to review."
)

// feedbackLabels are the two rating controls' labels for the message rated
// under task: a post's when task names one (postKindOf), an answer's
// otherwise.
func feedbackLabels(task string) (good, bad string) {
	if postKindOf(task) != "" {
		return feedbackPostGoodLabel, feedbackPostBadLabel
	}
	return feedbackGoodLabel, feedbackBadLabel
}

// feedbackNoteTitleFor is the title of the note a 👎 on the message rated
// under task asks for: a post's own when task names one.
func feedbackNoteTitleFor(task string) string {
	if postKindOf(task) != "" {
		return feedbackPostNoteTitle
	}
	return feedbackNoteTitle
}

// feedbackKeptTextFor is feedbackKeptText for the message rated under task:
// a post's own line when task names one.
func feedbackKeptTextFor(task string, days int, askKept bool) string {
	if postKindOf(task) == "" {
		return feedbackKeptText(days, askKept)
	}
	return fmt.Sprintf(feedbackPostKeptLine, feedbackKeptFor(days))
}

// feedbackKeptFor is how long a rating is kept, in words.
func feedbackKeptFor(days int) string {
	if days == 1 {
		return feedbackKeptOneDay
	}
	return fmt.Sprintf(feedbackKeptDays, days)
}

// feedbackThanksFor is feedbackThanks for the message rated under task: a
// post's own line when task names one.
func feedbackThanksFor(task, link string) string {
	if postKindOf(task) == "" {
		return feedbackThanks(link)
	}
	return feedbackPostThanksLine + " " + fmt.Sprintf(feedbackFormLine, link)
}

// feedbackKeptText is the line under the note a 👎 asks for: how long the
// install keeps the rated answer, naming the question only when askKept.
func feedbackKeptText(days int, askKept bool) string {
	line := feedbackKeptLine
	if !askKept {
		line = feedbackKeptNoAsk
	}
	return fmt.Sprintf(line, feedbackKeptFor(days))
}

// feedbackThanks is the line a kept note is thanked with; link is the
// backend's markup around feedbackFormURL.
func feedbackThanks(link string) string {
	return feedbackThanksLine + " " + fmt.Sprintf(feedbackFormLine, link)
}
