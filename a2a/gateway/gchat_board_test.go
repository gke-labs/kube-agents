package gateway

import (
	"encoding/json"
	"errors"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

const gchatBoardMessage = "spaces/_c2fixtureAA/messages/BOT1.BOT1"

// gchatBoardCard is the stuck-task report as the relay sends it: two rows
// with Retry and Drop, and one with none.
func gchatBoardCard() *chatmsg.Message {
	return &chatmsg.Message{Schema: chatmsg.Schema, Headline: "2 tasks are stuck and need someone to decide.",
		Rows: []chatmsg.Row{
			{Text: "**Triage x**", Detail: "Stopped 1 day ago before finishing.", Tasks: []string{"t_1", "t_2"}, Actions: []string{"retry", "drop"}},
			{Text: "**Open GitOps PR for y**", Detail: "Waiting on you.", Tasks: []string{"t_3"}, Actions: []string{"retry", "drop"}},
			{Text: "**Too many to act on**"},
		},
		After: []string{"And 3 more."}}
}

const gchatBoardCardText = "*2 tasks are stuck and need someone to decide.*\n\n" +
	"1. *Triage x* · Stopped 1 day ago before finishing.\n" +
	"2. *Open GitOps PR for y* · Waiting on you.\n" +
	"3. *Too many to act on*\n\n" +
	"And 3 more."

// taken is the actions a fakeBoardActor (slack_board_test.go) was sent.
func (f *fakeBoardActor) taken() []lib.BoardAction {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]lib.BoardAction{}, f.got...)
}

// newBoardGchatAdapter is an adapter with the board route and the rule
// wired, admitting the fixture's sender, over a relay whose create and
// patch answer with one bot message, at a fixed clock.
func newBoardGchatAdapter(t *testing.T) (*GoogleChatAdapter, *fakeChatRelay, *fakeBoardActor) {
	t.Helper()
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": gchatBoardMessage,
		"thread": map[string]any{"name": "spaces/_c2fixtureAA/threads/BOT1"}}
	f.responses["spaces.messages/patch"] = map[string]any{"name": gchatBoardMessage}
	a := newTestGchatAdapterWithRelay(t, f)
	a.syncRewrites = true
	a.now = func() time.Time { return time.Date(2026, 11, 6, 15, 4, 0, 0, time.UTC) }
	actor := &fakeBoardActor{outcome: lib.BoardOutcomeDone}
	a.SetBoardActor(actor.act)
	a.SetBoardAdmits(func(backend, author string) bool { return backend == "gchat" && author == "sender@example.com" })
	return a, f, actor
}

// gchatBoardTap is the fixture's click as a tap on row's action.
func gchatBoardTap(t *testing.T, action string, row int, tasks string) *gchatEvent {
	t.Helper()
	ev := gchatClickEvent(t)
	ev.click.Function = gchatBoardFunction
	ev.click.Parameters = map[string]string{"action": action, "row": strconv.Itoa(row), "tasks": tasks}
	return ev
}

// buttonTexts is the texts of a patched body's buttons, none for an empty
// widget list.
func buttonTexts(t *testing.T, body map[string]any) []string {
	t.Helper()
	widgets, _ := body["accessoryWidgets"].([]any)
	if len(widgets) == 0 {
		return nil
	}
	var texts []string
	for _, b := range bodyButtons(t, body) {
		texts = append(texts, b.Text)
	}
	return texts
}

func postBoardCard(t *testing.T, a *GoogleChatAdapter) {
	t.Helper()
	if _, _, err := a.PostNotifyMessage("spaces/_c2fixtureAA", "", OutMessage{Text: "fallback", Chat: gchatBoardCard()}); err != nil {
		t.Fatal(err)
	}
}

// A message whose rows carry buttons numbers every row in the bullet's
// place and draws each row's Retry and Drop, labelled with its number, in
// the one button list, their parameters the action, the row and its ids. A
// message whose rows carry none keeps its bullets.
func TestRenderGchatChatNumbersRowsWithButtons(t *testing.T) {
	layout, err := renderGchatChat(gchatBoardCard())
	if err != nil {
		t.Fatal(err)
	}
	if layout.Text != gchatBoardCardText {
		t.Errorf("text =\n%s\nwant\n%s", layout.Text, gchatBoardCardText)
	}
	if len(layout.Widgets) != 1 {
		t.Fatalf("widgets = %v", layout.Widgets)
	}
	buttons := layout.Widgets[0]["buttonList"].(map[string]any)["buttons"].([]map[string]any)
	var labels []string
	for _, b := range buttons {
		labels = append(labels, b["text"].(string))
		if _, filled := b["type"]; filled {
			t.Errorf("a board button takes the default type: %v", b)
		}
		if gchatButtonFunction(b) != gchatBoardFunction {
			t.Errorf("button function = %q", gchatButtonFunction(b))
		}
	}
	if strings.Join(labels, ",") != "Retry 1,Drop 1,Retry 2,Drop 2" {
		t.Errorf("labels = %v", labels)
	}
	first, last := buttons[0], buttons[3]
	if gchatButtonParam(first, "action") != "retry" || gchatButtonParam(first, "row") != "1" || gchatButtonParam(first, "tasks") != "t_1,t_2" {
		t.Errorf("first button's parameters = %v", first)
	}
	if gchatButtonParam(last, "action") != "drop" || gchatButtonParam(last, "row") != "2" || gchatButtonParam(last, "tasks") != "t_3" {
		t.Errorf("last button's parameters = %v", last)
	}
	plain := &chatmsg.Message{Schema: chatmsg.Schema, Rows: []chatmsg.Row{{Text: "a"}, {Text: "b"}}}
	if layout, err = renderGchatChat(plain); err != nil || layout.Text != "- a\n- b" || layout.Widgets != nil {
		t.Errorf("rows without buttons = %+v, %v", layout, err)
	}
	// A home post marks the board buttons like its choice buttons, which the
	// click does not read.
	home := gchatHomeButtons(layout)
	if home.Widgets != nil {
		t.Errorf("home layout of a plain message = %+v", home)
	}
}

// A tap is no turn: it acts on the row's cards over the board route as the
// presser, and the row is edited in place, its line after the text and its
// buttons gone, from the layout the adapter sent. A second tap on the row
// is dropped; a tap on another row edits from the first edit's layout.
func TestGchatBoardTapActsAndEditsTheRow(t *testing.T) {
	a, f, actor := newBoardGchatAdapter(t)
	postBoardCard(t, a)
	msg, reason := a.classify(gchatBoardTap(t, "retry", 1, "t_1,t_2"))
	if !strings.HasPrefix(reason, gchatBoardTaken) || msg.Text != "" {
		t.Fatalf("tap = %+v, %q", msg, reason)
	}
	calls := actor.taken()
	if len(calls) != 1 || calls[0].Action != "retry" || strings.Join(calls[0].Tasks, ",") != "t_1,t_2" || calls[0].By != "Test Sender" {
		t.Fatalf("board actions = %+v", calls)
	}
	body := checkPatch(t, f, 1, gchatBoardMessage)
	want := strings.Replace(gchatBoardCardText, "before finishing.\n", "before finishing. — ✓ Retrying. Test Sender, 15:04 UTC\n", 1)
	if body["text"] != want {
		t.Errorf("edited text =\n%s\nwant\n%s", body["text"], want)
	}
	if texts := buttonTexts(t, body); strings.Join(texts, ",") != "Retry 2,Drop 2" {
		t.Errorf("buttons after the tap = %v", texts)
	}
	if _, reason := a.classify(gchatBoardTap(t, "drop", 1, "t_1,t_2")); reason != gchatBoardBusy {
		t.Errorf("a second tap on a settled row: %q", reason)
	}
	actor.outcome = lib.BoardOutcomeMoved
	if _, reason := a.classify(gchatBoardTap(t, "drop", 2, "t_3")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("second row's tap: %q", reason)
	}
	body = checkPatch(t, f, 2, gchatBoardMessage)
	want = strings.Replace(want, "Waiting on you.\n", "Waiting on you. — No longer stuck. Test Sender, 15:04 UTC\n", 1)
	if body["text"] != want {
		t.Errorf("second edit =\n%s\nwant\n%s", body["text"], want)
	}
	if texts := buttonTexts(t, body); texts != nil {
		t.Errorf("buttons after both taps = %v", texts)
	}
	if len(actor.taken()) != 2 {
		t.Errorf("board actions = %+v", actor.taken())
	}
}

// A tap that did not land keeps the row's buttons under a line saying so,
// and the row can be tapped again; the next tap's line replaces it.
func TestGchatBoardFailedTapKeepsTheButtons(t *testing.T) {
	a, f, actor := newBoardGchatAdapter(t)
	postBoardCard(t, a)
	actor.err = errors.New("nothing answers the board route")
	if _, reason := a.classify(gchatBoardTap(t, "retry", 1, "t_1,t_2")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap: %q", reason)
	}
	body := checkPatch(t, f, 1, gchatBoardMessage)
	if text := body["text"].(string); !strings.Contains(text, "before finishing. — Couldn't retry it just now. Try again.\n2. ") {
		t.Errorf("failed tap's text =\n%s", text)
	}
	if texts := buttonTexts(t, body); strings.Join(texts, ",") != "Retry 1,Drop 1,Retry 2,Drop 2" {
		t.Errorf("buttons after a failed tap = %v", texts)
	}
	actor.err = nil
	if _, reason := a.classify(gchatBoardTap(t, "drop", 1, "t_1,t_2")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap after a failure: %q", reason)
	}
	body = checkPatch(t, f, 2, gchatBoardMessage)
	want := strings.Replace(gchatBoardCardText, "before finishing.\n", "before finishing. — Dropped. Test Sender, 15:04 UTC\n", 1)
	if body["text"] != want {
		t.Errorf("text after the retry =\n%s\nwant\n%s", body["text"], want)
	}
	if texts := buttonTexts(t, body); strings.Join(texts, ",") != "Retry 2,Drop 2" {
		t.Errorf("buttons after the drop = %v", texts)
	}
}

// A tap the adapter cannot act on changes nothing: no route or rule wired,
// a presser the gateway would not answer, no row, action, card or email,
// and a redelivery.
func TestGchatBoardTapsTheGatewayCannotTakeAreDropped(t *testing.T) {
	bare := newTestGchatAdapterWithRelay(t, newFakeChatRelay(t))
	if _, reason := bare.classify(gchatBoardTap(t, "retry", 1, "t_1")); reason != gchatBoardUnwired {
		t.Errorf("unwired: %q", reason)
	}
	a, f, actor := newBoardGchatAdapter(t)
	postBoardCard(t, a)
	other := gchatBoardTap(t, "retry", 1, "t_1")
	other.click.User.Email = "other@example.com"
	if _, reason := a.classify(other); reason != gchatBoardUnverified {
		t.Errorf("unadmitted: %q", reason)
	}
	for name, params := range map[string]map[string]string{
		"no row":     {"action": "retry", "tasks": "t_1"},
		"bad row":    {"action": "retry", "row": "0", "tasks": "t_1"},
		"bad action": {"action": "archive", "row": "1", "tasks": "t_1"},
		"no cards":   {"action": "drop", "row": "1", "tasks": ","},
	} {
		ev := gchatBoardTap(t, "retry", 1, "t_1")
		ev.click.Parameters = params
		if _, reason := a.classify(ev); reason != gchatBoardNoRow {
			t.Errorf("%s: %q", name, reason)
		}
	}
	nameless := gchatBoardTap(t, "retry", 1, "t_1")
	nameless.click.User.Email = ""
	if _, reason := a.classify(nameless); reason != gchatBoardNoEmail {
		t.Errorf("no email: %q", reason)
	}
	if len(actor.taken()) != 0 || relayCallCount(f) != 1 {
		t.Fatalf("a dropped tap acted: %+v, %d relay calls", actor.taken(), relayCallCount(f))
	}
	ev := gchatBoardTap(t, "retry", 2, "t_3")
	if _, reason := a.classify(ev); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap: %q", reason)
	}
	if _, reason := a.classify(ev); !strings.HasPrefix(reason, gchatBoardDuplicate) {
		t.Errorf("redelivery: %q", reason)
	}
	if len(actor.taken()) != 1 {
		t.Errorf("a redelivered tap acted again: %+v", actor.taken())
	}
}

// A tap on a message posted before a restart edits from the event's copy of
// it: its text, and its buttons as Chat shows them.
func TestGchatBoardTapAfterARestartEditsFromTheEventsCopy(t *testing.T) {
	a, f, _ := newBoardGchatAdapter(t)
	layout, err := renderGchatChat(gchatBoardCard())
	if err != nil {
		t.Fatal(err)
	}
	ev := gchatBoardTap(t, "retry", 2, "t_3")
	ev.Message.Text = layout.Text
	// Into a fresh slice: a decode into the fixture's own buttons would keep
	// the fields the JSON does not set.
	raw, _ := json.Marshal(layout.Widgets)
	var shown []gchatAccessoryWidget
	if err := json.Unmarshal(raw, &shown); err != nil {
		t.Fatal(err)
	}
	ev.Message.AccessoryWidgets = shown
	if _, reason := a.classify(ev); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap: %q", reason)
	}
	body := checkPatch(t, f, 0, gchatBoardMessage)
	want := strings.Replace(gchatBoardCardText, "Waiting on you.\n", "Waiting on you. — ✓ Retrying. Test Sender, 15:04 UTC\n", 1)
	if body["text"] != want {
		t.Errorf("text =\n%s\nwant\n%s", body["text"], want)
	}
	if texts := buttonTexts(t, body); strings.Join(texts, ",") != "Retry 1,Drop 1" {
		t.Errorf("buttons = %v", texts)
	}
}

// An edit of the message through the notify route refreshes what a tap
// edits from; one that lays no such rows out forgets it, and a tap then
// reads the event's copy. A row the text no longer holds is not edited, and
// the row is released for another tap.
func TestGchatBoardEditRefreshesTheRecordAndAGoneRowIsReleased(t *testing.T) {
	a, f, _ := newBoardGchatAdapter(t)
	postBoardCard(t, a)
	again := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "1 task is stuck.",
		Rows: []chatmsg.Row{{Text: "**Only z**", Tasks: []string{"t_9"}, Actions: []string{"retry", "drop"}}}}
	if err := a.EditNotifyMessage("spaces/_c2fixtureAA", "", gchatBoardMessage, OutMessage{Text: "again", Chat: again}); err != nil {
		t.Fatal(err)
	}
	if _, reason := a.classify(gchatBoardTap(t, "retry", 1, "t_9")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap: %q", reason)
	}
	body := checkPatch(t, f, 2, gchatBoardMessage)
	if want := "*1 task is stuck.*\n\n1. *Only z* — ✓ Retrying. Test Sender, 15:04 UTC"; body["text"] != want {
		t.Errorf("text after the edit =\n%s\nwant\n%s", body["text"], want)
	}
	if texts := buttonTexts(t, body); texts != nil {
		t.Errorf("buttons = %v", texts)
	}
	if err := a.EditNotifyMessage("spaces/_c2fixtureAA", "", gchatBoardMessage, OutMessage{Text: "settled"}); err != nil {
		t.Fatal(err)
	}
	if _, ok := a.boardLayoutOf(gchatBoardMessage); ok {
		t.Error("a message laid out without rows that act is still remembered")
	}
	gone := gchatBoardTap(t, "retry", 2, "t_3")
	gone.Message.Text = "settled"
	if _, reason := a.classify(gone); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap on a gone row: %q", reason)
	}
	if n := relayCallCount(f); n != 4 {
		t.Errorf("a row the text no longer holds was edited: %d relay calls", n)
	}
	if _, reason := a.classify(gchatBoardTap(t, "retry", 2, "t_3")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Errorf("the gone row was not released: %q", reason)
	}
}

// The row's line: who and when for a settled tap, UTC; an ask to try again
// for one that did not land; and a line an earlier tap left is replaced,
// while a separator in the row's own text is kept.
func TestGchatBoardLineAndRowText(t *testing.T) {
	now := time.Date(2026, 11, 6, 15, 4, 0, 0, time.FixedZone("x", 3600))
	for _, tc := range []struct{ verb, outcome, want string }{
		{"retry", lib.BoardOutcomeDone, "✓ Retrying. Ada, 14:04 UTC"},
		{"drop", lib.BoardOutcomeDone, "Dropped. Ada, 14:04 UTC"},
		{"retry", lib.BoardOutcomeMoved, "No longer stuck. Ada, 14:04 UTC"},
		{"retry", slackBoardFailed, "Couldn't retry it just now. Try again."},
		{"drop", slackBoardFailed, "Couldn't drop it just now. Try again."},
	} {
		if got := gchatBoardLine(tc.verb, tc.outcome, "Ada", now); got != tc.want {
			t.Errorf("%s/%s = %q, want %q", tc.verb, tc.outcome, got, tc.want)
		}
	}
	// A name that would ping is defanged as every added string is.
	if got := gchatBoardLine("retry", lib.BoardOutcomeDone, "<users/1>", now); got != "✓ Retrying. < users/1>, 14:04 UTC" {
		t.Errorf("a mention in the name = %q", got)
	}
	for _, tc := range []struct{ line, want string }{
		{"1. *a* — ✓ Retrying. Ada, 14:04 UTC", "1. *a*"},
		{"1. *a* — Couldn't drop it just now. Try again.", "1. *a*"},
		{"1. *a — b* · c", "1. *a — b* · c"},
	} {
		if got := gchatBoardRowText(tc.line); got != tc.want {
			t.Errorf("row text of %q = %q, want %q", tc.line, got, tc.want)
		}
	}
	if _, err := gchatBoardRowLayout(gchatChatLayout{Text: "10. x"}, 1, "l", true); !errors.Is(err, errGchatBoardRowGone) {
		t.Errorf("row 1 found in %q: %v", "10. x", err)
	}
}
