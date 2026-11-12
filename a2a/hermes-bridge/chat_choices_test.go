package hermesbridge

import (
	"strings"
	"testing"
)

// A first sentence longer than the cap with no clause break outside its
// bracket is cut at a word, the headline ending on the ellipsis the rest opens
// with, and no word is dropped; a sentence bold end to end keeps both halves
// bold; one unbroken word is declined, as a clause cut is.
func TestWordCutKeepsTheTail(t *testing.T) {
	sentence := "platform is currently running the live cluster count and will post the result here when it finishes " +
		"(its last completed count 14 hours ago found 209 running GKE clusters across all 42 GCP projects in scope)."
	head, rest, ok := wordCut(sentence)
	if !ok || !strings.HasSuffix(head, chatEllipsis) || !strings.HasPrefix(rest, chatEllipsis) {
		t.Fatalf("wordCut = %q, %q, %v", head, rest, ok)
	}
	if runeLen(plainLine(head)) > chatHeadlineMax {
		t.Errorf("headline over the cap: %d", runeLen(plainLine(head)))
	}
	if rejoined := strings.TrimSuffix(head, chatEllipsis) + " " + strings.TrimPrefix(rest, chatEllipsis); rejoined != sentence {
		t.Errorf("a word was dropped: %q", rejoined)
	}
	bold := "**" + sentence + "**"
	if head, rest, ok := wordCut(bold); !ok || !strings.HasPrefix(head, "**") || !strings.HasSuffix(head, chatEllipsis+"**") || !strings.HasPrefix(rest, "**"+chatEllipsis) {
		t.Errorf("bold wordCut = %q, %q, %v", head, rest, ok)
	}
	if _, _, ok := wordCut(strings.Repeat("x", chatHeadlineMax+40)); ok {
		t.Error("one unbroken word was cut")
	}
	headline, body := splitAnswer(sentence)
	if !strings.HasSuffix(headline, chatEllipsis) || len(body) != 1 || !strings.HasPrefix(body[0], chatEllipsis) {
		t.Errorf("splitAnswer = %q, %q", headline, body)
	}
}
