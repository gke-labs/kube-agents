package gateway

import (
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// gchatMixedCard is one message with a choice, a link and two rows that
// carry Retry and Drop: every rewrite the adapter makes can land on it.
func gchatMixedCard() *chatmsg.Message {
	return &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Two tasks are stuck. Which cluster?",
		Choices: []chatmsg.Choice{{Text: "Look at the first one", Reply: "Look at the first one: prod-eu", Primary: true}, {Text: "Skip"}},
		Links:   []chatmsg.Link{{Text: "board", URL: "https://x.example/b"}},
		Rows: []chatmsg.Row{
			{Text: "**Triage x**", Detail: "Stopped.", Tasks: []string{"t_1"}, Actions: []string{"retry", "drop"}},
			{Text: "**Open PR for y**", Detail: "Waiting.", Tasks: []string{"t_3"}, Actions: []string{"retry", "drop"}},
		}}
}

const (
	gchatMixedCardText = "*Two tasks are stuck. Which cluster?*\n\n" +
		"1. *Triage x* · Stopped.\n" +
		"2. *Open PR for y* · Waiting."
	gchatMixedRow1Done  = "1. *Triage x* · Stopped. — ✓ Retrying. Test Sender, 15:04 UTC\n"
	gchatMixedAnswered  = "\n\n✓ Test Sender: Look at the first one"
	gchatMixedRow1Tasks = "t_1"
)

// newMixedGchatAdapter is the board adapter with the recorder wired too,
// and the mixed card posted rated in a DM: the create is relay call 0.
func newMixedGchatAdapter(t *testing.T) (*GoogleChatAdapter, *fakeChatRelay, gchatChatLayout) {
	t.Helper()
	a, f, _ := newBoardGchatAdapter(t)
	a.SetFeedbackRecorder(&fakeRecorder{admit: map[string]bool{"sender@example.com": true}})
	if _, err := a.PostMessage("gchat:dm/spaces/_c2fixtureAA", OutMessage{Text: "fallback", Chat: gchatMixedCard(), Feedback: "t-root"}); err != nil {
		t.Fatal(err)
	}
	layout, err := renderGchatChat(gchatMixedCard())
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(buttonTexts(t, callBody(t, f.call(0))), ","); got != "Look at the first one,Skip,board,Retry 1,Drop 1,Retry 2,Drop 2,👍,👎" {
		t.Fatalf("buttons as created = %s", got)
	}
	return a, f, layout
}

// answerMixed answers the card's first choice through a verified click.
func answerMixed(t *testing.T, a *GoogleChatAdapter) {
	t.Helper()
	msg, reason := a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatalf("choice click: %q", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
}

// tapMixedRow1 taps Retry on the card's first row, with the line its button
// carries.
func tapMixedRow1(t *testing.T, a *GoogleChatAdapter, layout gchatChatLayout) {
	t.Helper()
	tap := gchatBoardTapAt(t, "retry", 1, gchatMixedRow1Tasks, gchatButtonLine(t, layout, "retry", 1))
	if _, reason := a.classify(tap); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("board tap: %q", reason)
	}
}

// checkMixedBody checks one patched body's text and its buttons' texts.
func checkMixedBody(t *testing.T, body map[string]any, wantText, wantButtons string) {
	t.Helper()
	if body["text"] != wantText {
		t.Errorf("text =\n%s\nwant\n%s", body["text"], wantText)
	}
	if got := strings.Join(buttonTexts(t, body), ","); got != wantButtons {
		t.Errorf("buttons = %s, want %s", got, wantButtons)
	}
}

// Answering the choice, then tapping a row: the answer keeps the link, the
// row buttons and the rating buttons and drops the choices; the tap then
// settles its row's line under the answered line, drops that row's two
// buttons, and keeps everything else the answer left.
func TestGchatAnswerThenBoardTapKeepOneLayout(t *testing.T) {
	a, f, layout := newMixedGchatAdapter(t)
	answerMixed(t, a)
	checkMixedBody(t, checkPatch(t, f, 1, gchatBoardMessage), gchatMixedCardText+gchatMixedAnswered, "board,Retry 1,Drop 1,Retry 2,Drop 2,👍,👎")
	tapMixedRow1(t, a, layout)
	body := checkPatch(t, f, 2, gchatBoardMessage)
	want := strings.Replace(gchatMixedCardText, "1. *Triage x* · Stopped.\n", gchatMixedRow1Done, 1) + gchatMixedAnswered
	checkMixedBody(t, body, want, "board,Retry 2,Drop 2,👍,👎")
	if n := relayCallCount(f); n != 3 {
		t.Errorf("%d relay calls, want the create and two patches", n)
	}
}

// Tapping a row, then answering the choice: the tap keeps the choices, the
// link and the other row's buttons; the answer then keeps the settled row's
// line, the link, the other row's buttons and the rating buttons, and
// drops the choices alone.
func TestGchatBoardTapThenAnswerKeepOneLayout(t *testing.T) {
	a, f, layout := newMixedGchatAdapter(t)
	tapMixedRow1(t, a, layout)
	settled := strings.Replace(gchatMixedCardText, "1. *Triage x* · Stopped.\n", gchatMixedRow1Done, 1)
	checkMixedBody(t, checkPatch(t, f, 1, gchatBoardMessage), settled, "Look at the first one,Skip,board,Retry 2,Drop 2,👍,👎")
	answerMixed(t, a)
	checkMixedBody(t, checkPatch(t, f, 2, gchatBoardMessage), settled+gchatMixedAnswered, "board,Retry 2,Drop 2,👍,👎")
	if n := relayCallCount(f); n != 3 {
		t.Errorf("%d relay calls, want the create and two patches", n)
	}
}

// A rating tap after a board tap keeps the settled row's line and the
// remaining row buttons, fills the tapped rating, and, the question still
// open, keeps the choices; the answer after it keeps the filled rating.
func TestGchatRatingTapAfterBoardTapKeepsOneLayout(t *testing.T) {
	a, f, layout := newMixedGchatAdapter(t)
	tapMixedRow1(t, a, layout)
	if _, reason := a.classify(gchatRatingEvent(t, "up")); !strings.HasPrefix(reason, gchatRatingTaken) {
		t.Fatalf("rating tap: %q", reason)
	}
	settled := strings.Replace(gchatMixedCardText, "1. *Triage x* · Stopped.\n", gchatMixedRow1Done, 1)
	body := checkPatch(t, f, 2, gchatBoardMessage)
	checkMixedBody(t, body, settled, "Look at the first one,Skip,board,Retry 2,Drop 2,👍,👎")
	checkFilled(t, bodyButtons(t, body), "up")
	answerMixed(t, a)
	body = checkPatch(t, f, 3, gchatBoardMessage)
	checkMixedBody(t, body, settled+gchatMixedAnswered, "board,Retry 2,Drop 2,👍,👎")
	checkFilled(t, bodyButtons(t, body), "up")
	if n := relayCallCount(f); n != 4 {
		t.Errorf("%d relay calls, want the create and three patches", n)
	}
}

// An answered message the adapter has no record of (posted before a
// restart) is rewritten from the click's copy: its choice buttons go, its
// link and row buttons stay, and the rating buttons it showed come last.
func TestGchatAnswerWithoutARecordKeepsTheEventsButtons(t *testing.T) {
	a, f, layout := newMixedGchatAdapter(t)
	a.forgetLayout(gchatBoardMessage)
	a.mu.Lock()
	delete(a.uxLocked().questions, gchatBoardMessage)
	a.mu.Unlock()
	rated := layout
	rated.Widgets = gchatWithRating(layout.Widgets, gchatRating{task: "t-root"})
	ev := gchatClickEvent(t)
	ev.Message.Text = rated.Text
	ev.Message.AccessoryWidgets = gchatShownWidgets(t, rated)
	msg, reason := a.classify(ev)
	if reason != "" {
		t.Fatalf("choice click: %q", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	checkMixedBody(t, checkPatch(t, f, 1, gchatBoardMessage), gchatMixedCardText+gchatMixedAnswered, "board,Retry 1,Drop 1,Retry 2,Drop 2,👍,👎")
	if _, remembered, _ := a.layoutOf(gchatBoardMessage); !remembered {
		t.Error("the rewrite from the event's copy left no record")
	}
}

// A message laid out again without rows that act, while its record still
// drew a row's buttons, keeps the new layout as its record and is marked as
// moved; a tap naming a row of the old layout is stale.
func TestGchatMovedFollowsTheRecordsButtons(t *testing.T) {
	a, f, layout := newMixedGchatAdapter(t)
	tapMixedRow1(t, a, layout)
	if err := a.EditMessage("gchat:dm/spaces/_c2fixtureAA", gchatBoardMessage, OutMessage{Text: "settled"}); err != nil {
		t.Fatal(err)
	}
	if record, remembered, moved := a.layoutOf(gchatBoardMessage); !remembered || !moved || record.Text != "settled" {
		t.Errorf("record = %+v, remembered %v, moved %v; want the settled text, marked as moved", record, remembered, moved)
	}
	if n := relayCallCount(f); n != 3 {
		t.Fatalf("%d relay calls, want the create, the tap's patch and the edit", n)
	}
	stale := gchatBoardTapAt(t, "drop", 2, "t_3", gchatButtonLine(t, layout, "drop", 2))
	if _, reason := a.classify(stale); reason != gchatBoardStale {
		t.Errorf("a tap after the settle: %q", reason)
	}
}
