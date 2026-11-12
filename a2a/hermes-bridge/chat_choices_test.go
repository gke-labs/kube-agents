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

// The cut prefers the last space outside a bracket, falls inside one when
// nothing else fits, and never leaves the headline on a connector or a bare
// number: slack_presenter's word_cut tests, case for case.
func TestWordCutPrefersOutsideABracketAndStepsBackFromAConnector(t *testing.T) {
	head, rest, ok := wordCut("platform is currently running the live cluster count and will post the result here when it finishes " +
		"(its last completed count 14 hours ago found 209 running GKE clusters across all 42 GCP projects in scope).")
	if !ok || head != "platform is currently running the live cluster count and will post the result here when it finishes…" ||
		!strings.HasPrefix(rest, "…(its last completed count") {
		t.Errorf("outside the bracket: %q, %q, %v", head, rest, ok)
	}
	if head, _, ok := wordCut("(" + strings.Repeat("word ", 60) + "end)"); !ok || !strings.HasPrefix(head, "(word") || !strings.HasSuffix(head, "word…") {
		t.Errorf("inside the only bracket: %q, %v", head, ok)
	}
	head, rest, ok = wordCut("Three pods restarted (checkout, payments, inventory, search, cart, and the ledger worker that feeds them " +
		"all every hour) on seeded-a overnight and none of them has recovered since then at all.")
	if !ok || !strings.HasSuffix(head, "on seeded-a overnight…") || !strings.HasPrefix(rest, "…and none") {
		t.Errorf("the connector: %q, %q, %v", head, rest, ok)
	}
	head, rest, ok = wordCut(strings.Repeat("x", 132) + " found 209 running GKE clusters across all projects in scope today.")
	if !ok || head != strings.Repeat("x", 132)+" found…" || rest != "…209 running GKE clusters across all projects in scope today." {
		t.Errorf("the number: %q, %q, %v", head, rest, ok)
	}
	link := "(" + strings.Repeat("word ", 14) + "then [the Logs Explorer query for the last completed count that found 209 " +
		"running GKE clusters](https://console.cloud.google.com/logs/query;q=x) across all projects in scope)"
	if head, rest, ok := wordCut(link); !ok || head != "("+strings.Repeat("word ", 14)+"then…" || !strings.HasPrefix(rest, "…[the Logs") {
		t.Errorf("the link: %q, %q, %v", head, rest, ok)
	}
	mrkdwn := strings.Repeat("word ", 20) + "then <https://example.com/a/very/long/path|a label with spaces in it that reaches the cap> and on."
	if head, _, ok := wordCut(mrkdwn); !ok || head != strings.Repeat("word ", 20)+"then…" {
		t.Errorf("the mrkdwn link: %q, %v", head, ok)
	}
	if head, rest, ok := wordCut(strings.Repeat("x", 136) + " found 209… running GKE clusters across all projects in scope today."); !ok ||
		!strings.HasSuffix(head, "209…") || strings.HasSuffix(head, "……") || !strings.HasPrefix(rest, "…running") {
		t.Errorf("a second ellipsis: %q, %q, %v", head, rest, ok)
	}
}

// An in-word marker (DB__HOST, 2**20) is text to both cuts, and a bold wrap
// whose code span holds "**" is still one bold run: the presenter's
// test_an_in_word_marker_is_text_to_both_cuts.
func TestAnInWordMarkerIsTextToBothCuts(t *testing.T) {
	prose := "DB__HOST on seeded-c points to the retired replica (the one the migration retired last week after " +
		"the failover test that moved every writer to the new pool in us-central1)."
	if head, rest, ok := wordCut(prose); !ok || head != "DB__HOST on seeded-c points to the retired replica…" || !strings.HasPrefix(rest, "…(the one") {
		t.Errorf("in-word __: %q, %q, %v", head, rest, ok)
	}
	bold := "**The `2**20` byte limit on the queue was reached before sunrise on seeded-c (the producer kept " +
		"writing while every consumer waited on the retired replica in us-central1).**"
	if head, rest, ok := wordCut(bold); !ok || head != "**The `2**20` byte limit on the queue was reached before sunrise on seeded-c…**" ||
		!strings.HasPrefix(rest, "**…(the producer") || !strings.HasSuffix(rest, "**") {
		t.Errorf("bold wrap over a code span with **: %q, %q, %v", head, rest, ok)
	}
	clause := "DB__HOST on seeded-c still points to the retired replica because the failover test moved every " +
		"writer to the new pool in us-central1 and nobody moved the variable after."
	if head, _, ok := clauseCut(clause); !ok || head != "DB__HOST on seeded-c still points to the retired replica." {
		t.Errorf("clauseCut with an in-word __: %q, %v", head, ok)
	}
}

// A closed bold run before an in-word marker stays closed, the ellipsis test
// reads through closing emphasis, a comparison's "<" is no mrkdwn link, and a
// bracketed connector is held: the presenter's tests, case for case.
func TestWordCutRoundTwo(t *testing.T) {
	prose := "The __bold__ setting and DB__HOST on seeded-c point to the retired replica (the one the migration " +
		"retired last week after the failover test that moved every writer to the new pool in us-central1)."
	if head, _, ok := wordCut(prose); !ok || head != "The __bold__ setting and DB__HOST on seeded-c point to the retired replica…" {
		t.Errorf("closed run then in-word __: %q, %v", head, ok)
	}
	stars := "The **bold** setting and 2**20 byte limit on the queue were reached before sunrise on seeded-c (the " +
		"producer kept writing while every consumer waited on the retired replica in us-central1)."
	if head, _, ok := wordCut(stars); !ok || !strings.HasSuffix(head, "before sunrise on seeded-c…") {
		t.Errorf("closed run then in-word **: %q, %v", head, ok)
	}
	if head, rest, ok := wordCut(strings.Repeat("x", 132) + " **found 209…** running GKE clusters across all projects in scope today."); !ok ||
		head != strings.Repeat("x", 132)+" **found 209…**" || rest != "…running GKE clusters across all projects in scope today." {
		t.Errorf("ellipsis inside closing emphasis: %q, %q, %v", head, rest, ok)
	}
	if head, rest, ok := wordCut(strings.Repeat("x", 136) + " found **…209 running** GKE clusters across all projects in scope today."); !ok ||
		head != strings.Repeat("x", 136)+" found…" || rest != "**…209 running** GKE clusters across all projects in scope today." {
		t.Errorf("ellipsis inside opening emphasis: %q, %q, %v", head, rest, ok)
	}
	if _, _, ok := wordCut(strings.Repeat("x", 60) + " request is < 100Mi and memory limit is > 200Mi for all pods running in the production " +
		"namespace of seeded-c (every one of them since the rollout at noon)."); !ok {
		t.Error("a comparison read as a link")
	}
	if head, _, ok := wordCut(strings.Repeat("x", 130) + " (and the same again for the pods of the other namespace on seeded-c and seeded-b)."); !ok ||
		head != strings.Repeat("x", 130)+"…" {
		t.Errorf("a bracketed connector: %q, %v", head, ok)
	}
}
