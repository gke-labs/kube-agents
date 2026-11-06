package gateway

import (
	"strings"
	"testing"
)

func TestFeedbackWordsAreOneCopyForEveryBackend(t *testing.T) {
	for got, want := range map[string]string{
		feedbackGoodLabel: "Good answer", feedbackBadLabel: "Bad answer",
		feedbackNoteTitle: "What went wrong?", feedbackNoteLabel: "Anything else?",
		feedbackNoteSend: "Send", feedbackNoteCancel: "Cancel",
		feedbackThanksLine: "Thanks. I kept this answer for your team to review.",
		feedbackFormURL:    "https://gke-labs.github.io/kube-agents/feedback",
	} {
		if got != want {
			t.Errorf("%q, want %q", got, want)
		}
	}
	if got := feedbackKeptText(30, true); got != "Your question and this answer are kept on your install for 30 days so an operator can turn them into a test." {
		t.Error(got)
	}
	if got := feedbackKeptText(1, true); !strings.Contains(got, "for 1 day so") {
		t.Error(got)
	}
	if got := feedbackKeptText(30, false); got != "This answer is kept on your install for 30 days so an operator can turn it into a test." {
		t.Error(got)
	}
	// Slack's thanks line is byte-for-byte what it was.
	if got := slackFeedbackThanks(); got != "Thanks. I kept this answer for your team to review. To tell the kube-agents maintainers as well, use <https://gke-labs.github.io/kube-agents/feedback>. It opens a public issue, so leave out cluster names and other details from your install." {
		t.Error(got)
	}
}
