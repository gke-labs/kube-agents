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
)

// feedbackKeptText is the line under the note a 👎 asks for: how long the
// install keeps the rated answer, naming the question only when askKept.
func feedbackKeptText(days int, askKept bool) string {
	kept := fmt.Sprintf(feedbackKeptDays, days)
	if days == 1 {
		kept = feedbackKeptOneDay
	}
	line := feedbackKeptLine
	if !askKept {
		line = feedbackKeptNoAsk
	}
	return fmt.Sprintf(line, kept)
}

// feedbackThanks is the line a kept note is thanked with; link is the
// backend's markup around feedbackFormURL.
func feedbackThanks(link string) string {
	return feedbackThanksLine + " " + fmt.Sprintf(feedbackFormLine, link)
}
