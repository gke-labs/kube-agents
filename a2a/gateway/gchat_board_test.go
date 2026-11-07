package gateway

import (
	"context"
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

// gchatShownWidgets is a layout's widgets as a click event carries them
// back. Decoded into a fresh slice: a decode into the fixture's own buttons
// would keep the fields the JSON does not set.
func gchatShownWidgets(t *testing.T, layout gchatChatLayout) []gchatAccessoryWidget {
	t.Helper()
	raw, _ := json.Marshal(layout.Widgets)
	var shown []gchatAccessoryWidget
	if err := json.Unmarshal(raw, &shown); err != nil {
		t.Fatal(err)
	}
	return shown
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
	ev.Message.AccessoryWidgets = gchatShownWidgets(t, layout)
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
	a, f, actor := newBoardGchatAdapter(t)
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
	if _, remembered, _ := a.boardLayoutOf(gchatBoardMessage); remembered {
		t.Error("a message laid out without rows that act is still remembered")
	}
	// A client still showing the old card: the tap carries the old text and
	// buttons, and is dropped before it acts.
	old, err := renderGchatChat(gchatBoardCard())
	if err != nil {
		t.Fatal(err)
	}
	gone := gchatBoardTap(t, "retry", 2, "t_3")
	gone.Message.Text = old.Text
	gone.Message.AccessoryWidgets = gchatShownWidgets(t, old)
	if _, reason := a.classify(gone); reason != gchatBoardStale {
		t.Fatalf("tap on a settled message: %q", reason)
	}
	if n := relayCallCount(f); n != 4 {
		t.Errorf("a settled message was edited: %d relay calls", n)
	}
	if n := len(actor.taken()); n != 1 {
		t.Errorf("a stale tap acted: %d board actions", n)
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
		{"1. *a — Dropped. from the queue*", "1. *a — Dropped. from the queue*"},
		{"1. *a* — Dropped. from the queue", "1. *a* — Dropped. from the queue"},
	} {
		if got := gchatBoardRowText(tc.line); got != tc.want {
			t.Errorf("row text of %q = %q, want %q", tc.line, got, tc.want)
		}
	}
	if _, err := gchatBoardRowLayout(gchatChatLayout{Text: "10. x"}, 1, "l", true); !errors.Is(err, errGchatBoardRowGone) {
		t.Errorf("row 1 found in %q: %v", "10. x", err)
	}
}

// A message laid out again through the notify route has new rows, so the
// claim a settled tap kept on the old row 1 does not hold the new row 1's
// buttons dead: a tap on it acts again.
func TestGchatBoardReLayoutFreesTheRowsClaims(t *testing.T) {
	a, f, actor := newBoardGchatAdapter(t)
	postBoardCard(t, a)
	if _, reason := a.classify(gchatBoardTap(t, "retry", 1, "t_1,t_2")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("first tap: %q", reason)
	}
	if _, reason := a.classify(gchatBoardTap(t, "retry", 1, "t_1,t_2")); reason != gchatBoardBusy {
		t.Fatalf("a second tap before the re-layout: %q", reason)
	}
	again := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "1 task is stuck.",
		Rows: []chatmsg.Row{{Text: "**Only z**", Tasks: []string{"t_9"}, Actions: []string{"retry", "drop"}}}}
	if err := a.EditNotifyMessage("spaces/_c2fixtureAA", "", gchatBoardMessage, OutMessage{Text: "again", Chat: again}); err != nil {
		t.Fatal(err)
	}
	if _, reason := a.classify(gchatBoardTap(t, "drop", 1, "t_9")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap on the new row 1: %q", reason)
	}
	calls := actor.taken()
	if len(calls) != 2 || calls[1].Action != "drop" || strings.Join(calls[1].Tasks, ",") != "t_9" {
		t.Fatalf("board actions = %+v, want the new row's drop as the second", calls)
	}
	body := checkPatch(t, f, 3, gchatBoardMessage)
	if want := "*1 task is stuck.*\n\n1. *Only z* — Dropped. Test Sender, 15:04 UTC"; body["text"] != want {
		t.Errorf("text =\n%s\nwant\n%s", body["text"], want)
	}
}

// The row is found in the rows block, not at the first line that starts
// with its number: a detail with a numbered list of its own is left alone.
func TestGchatBoardTapEditsTheRowNotADetailLine(t *testing.T) {
	a, f, _ := newBoardGchatAdapter(t)
	card := gchatBoardCard()
	card.Detail = "1. not a row\n2. nor this"
	if _, _, err := a.PostNotifyMessage("spaces/_c2fixtureAA", "", OutMessage{Text: "fallback", Chat: card}); err != nil {
		t.Fatal(err)
	}
	if _, reason := a.classify(gchatBoardTap(t, "retry", 1, "t_1,t_2")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap: %q", reason)
	}
	body := checkPatch(t, f, 1, gchatBoardMessage)
	text := body["text"].(string)
	if !strings.Contains(text, "\n\n1. not a row\n2. nor this\n\n1. *Triage x* · Stopped 1 day ago before finishing. — ✓ Retrying. Test Sender, 15:04 UTC\n2. ") {
		t.Errorf("text =\n%s", text)
	}
	for _, tc := range []struct {
		lines        []string
		start, count int
	}{
		{[]string{"h", "", "1. a", "2. b", "", "x"}, 2, 2},
		{[]string{"1. d", "", "1. a", "2. b"}, 2, 2},
		{[]string{"1. a", "2. b", "", "1. f"}, 0, 2},
		{[]string{"1. d", "", "1. a"}, 2, 1},
		{[]string{"h", "10. x"}, 0, 0},
	} {
		if start, count := gchatBoardRowsBlock(tc.lines); start != tc.start || count != tc.count {
			t.Errorf("rows block of %q = %d, %d; want %d, %d", tc.lines, start, count, tc.start, tc.count)
		}
	}
}

// A progress message draws its plan in the rows' place, so rows that carry
// buttons draw none there: a button for a row not shown would name nothing.
func TestGchatBoardButtonsAreDrawnOnlyWithTheRows(t *testing.T) {
	m := gchatBoardCard()
	m.Plan = &chatmsg.Plan{Items: []chatmsg.PlanItem{{ID: "c1", Title: "Triage x", Status: chatmsg.StatusInProgress}}}
	layout, err := renderGchatChat(m)
	if err != nil {
		t.Fatal(err)
	}
	if layout.Widgets != nil || strings.Contains(layout.Text, "1. ") {
		t.Errorf("plan layout = %+v, want no row buttons and no numbered rows", layout)
	}
}

// The gateway hands the Chat adapter the board route and the admission
// rule on New: an allowlisted presser's tap acts, another's does not. The
// mux and the side door pass the rule through to the adapter behind them.
func TestGchatBoardAdmitsIsWiredThroughNewMuxAndSideDoor(t *testing.T) {
	s := startServer(t)
	provision(t, s.ClientURL())
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("gateway-test"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/patch"] = map[string]any{"name": gchatBoardMessage}
	a := newTestGchatAdapterWithRelay(t, f)
	a.syncRewrites = true
	cfg := &Config{NATSURL: s.ClientURL(), DefaultAddressee: "platform", IdleTTL: 30 * time.Minute,
		AttributionSalt: []byte("test-salt"), GchatAllowedUsers: []string{"Sender@example.com"}}
	if _, err := New(Options{Client: client, Adapter: a, Config: cfg, Backend: "gchat"}); err != nil {
		t.Fatal(err)
	}
	a.mu.Lock()
	actor, admits := a.boardActor, a.boardAdmits
	a.mu.Unlock()
	if actor == nil || admits == nil {
		t.Fatal("New wired no board route or rule into the Chat adapter")
	}
	if !admits("gchat", "sender@example.com") || admits("gchat", "other@example.com") {
		t.Error("the rule is not the gateway's allowlist")
	}
	// With no bridge on the bus the tap fails and the row says so; a tap
	// from an unlisted presser is dropped before any call.
	other := gchatBoardTap(t, "retry", 1, "t_1")
	other.click.User.Email = "other@example.com"
	if _, reason := a.classify(other); reason != gchatBoardUnverified {
		t.Errorf("unlisted presser: %q", reason)
	}
	// As after a restart: the event carries the message, text and buttons.
	shown, err := renderGchatChat(&chatmsg.Message{Schema: chatmsg.Schema,
		Rows: []chatmsg.Row{{Text: "x", Tasks: []string{"t_1"}, Actions: []string{"retry", "drop"}}}})
	if err != nil {
		t.Fatal(err)
	}
	ev := gchatBoardTap(t, "retry", 1, "t_1")
	ev.Message.Text = shown.Text
	ev.Message.AccessoryWidgets = gchatShownWidgets(t, shown)
	if _, reason := a.classify(ev); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("listed presser: %q", reason)
	}
	if body := checkPatch(t, f, 0, gchatBoardMessage); body["text"] != "1. x — Couldn't retry it just now. Try again." {
		t.Errorf("text after a tap with no bridge = %q", body["text"])
	}

	behindMux := newTestGchatAdapterWithRelay(t, newFakeChatRelay(t))
	m, err := NewMultiAdapter("gchat", "console", map[string]Adapter{"gchat": behindMux, "console": newFakeAdapter()}, nil)
	if err != nil {
		t.Fatal(err)
	}
	m.SetBoardAdmits(func(string, string) bool { return true })
	behindDoor := newTestGchatAdapterWithRelay(t, newFakeChatRelay(t))
	door := WithSideDoors(behindDoor, nil, nil).(*sideDoorAdapter)
	door.SetBoardAdmits(func(string, string) bool { return true })
	for name, b := range map[string]*GoogleChatAdapter{"mux": behindMux, "side door": behindDoor} {
		b.mu.Lock()
		set := b.boardAdmits != nil
		b.mu.Unlock()
		if !set {
			t.Errorf("the %s did not pass SetBoardAdmits through", name)
		}
	}
}

// A message laid out again while a tap's round trip is in flight has new
// rows: the tap's outcome is not drawn on the new row n, the new row keeps
// its buttons, and a tap on it acts. A tap whose client still shows the old
// buttons is dropped before it acts.
func TestGchatBoardReLayoutDuringTheRoundTripLeavesTheNewRow(t *testing.T) {
	a, f, _ := newBoardGchatAdapter(t)
	postBoardCard(t, a)
	again := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "1 task is stuck.",
		Rows: []chatmsg.Row{{Text: "**Only z**", Tasks: []string{"t_9"}, Actions: []string{"retry", "drop"}}}}
	var acted []lib.BoardAction
	a.SetBoardActor(func(_ context.Context, act lib.BoardAction) (*lib.BoardActionResult, error) {
		acted = append(acted, act)
		if len(acted) == 1 {
			// The re-layout lands while session-kv is still answering.
			if err := a.EditNotifyMessage("spaces/_c2fixtureAA", "", gchatBoardMessage, OutMessage{Text: "again", Chat: again}); err != nil {
				t.Fatal(err)
			}
		}
		res := &lib.BoardActionResult{Action: act.Action}
		for _, id := range act.Tasks {
			res.Results = append(res.Results, lib.BoardActionOutcome{ID: id, Outcome: lib.BoardOutcomeDone})
		}
		return res, nil
	})
	if _, reason := a.classify(gchatBoardTap(t, "retry", 1, "t_1,t_2")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap: %q", reason)
	}
	// Create, the re-layout's patch, and nothing from the tap.
	if n := relayCallCount(f); n != 2 {
		t.Fatalf("%d relay calls, want the create and the re-layout's patch only", n)
	}
	if body := callBody(t, f.call(1)); body["text"] != "*1 task is stuck.*\n\n1. *Only z*" {
		t.Errorf("the re-laid message = %q", body["text"])
	}
	// The old row's buttons, still on a client: dropped before acting.
	if _, reason := a.classify(gchatBoardTap(t, "drop", 1, "t_1,t_2")); reason != gchatBoardStale {
		t.Errorf("a tap on the old row: %q", reason)
	}
	// The new row 1 is free to act.
	if _, reason := a.classify(gchatBoardTap(t, "drop", 1, "t_9")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap on the new row: %q", reason)
	}
	if len(acted) != 2 || strings.Join(acted[1].Tasks, ",") != "t_9" {
		t.Fatalf("board actions = %+v", acted)
	}
	body := checkPatch(t, f, 2, gchatBoardMessage)
	if want := "*1 task is stuck.*\n\n1. *Only z* — Dropped. Test Sender, 15:04 UTC"; body["text"] != want {
		t.Errorf("text =\n%s\nwant\n%s", body["text"], want)
	}
}

// A row that draws nothing (a rule, no severity) takes no number and no
// buttons, so the numbers in the text and on the buttons name the same
// rows, and a tap on "Retry 1" edits the first drawn row.
func TestGchatBoardSkippedRowTakesNoNumber(t *testing.T) {
	a, f, actor := newBoardGchatAdapter(t)
	card := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "2 tasks are stuck.",
		Rows: []chatmsg.Row{
			{Text: "---", Tasks: []string{"t_0"}, Actions: []string{"retry", "drop"}},
			{Text: "**Triage x**", Tasks: []string{"t_1"}, Actions: []string{"retry", "drop"}},
			{Text: "**Open GitOps PR for y**", Tasks: []string{"t_3"}, Actions: []string{"retry", "drop"}},
		}}
	layout, err := renderGchatChat(card)
	if err != nil {
		t.Fatal(err)
	}
	if want := "*2 tasks are stuck.*\n\n1. *Triage x*\n2. *Open GitOps PR for y*"; layout.Text != want {
		t.Errorf("text =\n%s\nwant\n%s", layout.Text, want)
	}
	buttons := layout.Widgets[0]["buttonList"].(map[string]any)["buttons"].([]map[string]any)
	var got []string
	for _, b := range buttons {
		got = append(got, b["text"].(string)+"="+gchatButtonParam(b, "tasks"))
	}
	if strings.Join(got, ",") != "Retry 1=t_1,Drop 1=t_1,Retry 2=t_3,Drop 2=t_3" {
		t.Errorf("buttons = %v", got)
	}
	if _, _, err := a.PostNotifyMessage("spaces/_c2fixtureAA", "", OutMessage{Text: "fallback", Chat: card}); err != nil {
		t.Fatal(err)
	}
	if _, reason := a.classify(gchatBoardTap(t, "retry", 1, "t_1")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap: %q", reason)
	}
	if calls := actor.taken(); len(calls) != 1 || strings.Join(calls[0].Tasks, ",") != "t_1" {
		t.Fatalf("board actions = %+v", calls)
	}
	body := checkPatch(t, f, 1, gchatBoardMessage)
	if want := "*2 tasks are stuck.*\n\n1. *Triage x* — ✓ Retrying. Test Sender, 15:04 UTC\n2. *Open GitOps PR for y*"; body["text"] != want {
		t.Errorf("text =\n%s\nwant\n%s", body["text"], want)
	}
	if texts := buttonTexts(t, body); strings.Join(texts, ",") != "Retry 2,Drop 2" {
		t.Errorf("buttons = %v", texts)
	}
}

// A keyed edit that settles the message to text during a tap's round trip
// (or lays it out without rows that act) leaves nothing for the tap to draw
// on: the settle stands, the tap patches nothing, and a tap from a client
// still showing the old buttons is dropped. The actor fake runs the settle
// from inside the round trip, which is the window.
func TestGchatBoardSettleDuringTheRoundTripStands(t *testing.T) {
	a, f, _ := newBoardGchatAdapter(t)
	postBoardCard(t, a)
	a.SetBoardActor(func(_ context.Context, act lib.BoardAction) (*lib.BoardActionResult, error) {
		if err := a.EditNotifyMessage("spaces/_c2fixtureAA", "", gchatBoardMessage, OutMessage{Text: "settled: all done"}); err != nil {
			t.Fatal(err)
		}
		return &lib.BoardActionResult{Action: act.Action, Results: []lib.BoardActionOutcome{{ID: act.Tasks[0], Outcome: lib.BoardOutcomeDone}}}, nil
	})
	if _, reason := a.classify(gchatBoardTap(t, "retry", 1, "t_1,t_2")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap: %q", reason)
	}
	if n := relayCallCount(f); n != 2 {
		t.Fatalf("%d relay calls, want the create and the settle only", n)
	}
	if body := callBody(t, f.call(1)); body["text"] != "settled: all done" {
		t.Errorf("the settled message = %q", body["text"])
	}
	if _, remembered, moved := a.boardLayoutOf(gchatBoardMessage); remembered || !moved {
		t.Errorf("record after the settle: remembered %v, moved %v", remembered, moved)
	}
	old, err := renderGchatChat(gchatBoardCard())
	if err != nil {
		t.Fatal(err)
	}
	stale := gchatBoardTap(t, "drop", 2, "t_3")
	stale.Message.Text = old.Text
	stale.Message.AccessoryWidgets = gchatShownWidgets(t, old)
	if _, reason := a.classify(stale); reason != gchatBoardStale {
		t.Errorf("a tap on the old buttons: %q", reason)
	}
	if n := relayCallCount(f); n != 2 {
		t.Errorf("a stale tap edited the settled message: %d relay calls", n)
	}
}

// The row is edited where the renderer put the rows, so a numbered list in
// the fold (which renders after the rows) is left alone; a layout read back
// from a click's copy still uses the finder.
func TestGchatBoardTapEditsTheRowNotAFoldList(t *testing.T) {
	a, f, _ := newBoardGchatAdapter(t)
	card := gchatBoardCard()
	card.After = nil
	card.Fold = &chatmsg.Fold{Title: "why", Text: "1. x\n2. y"}
	layout, err := renderGchatChat(card)
	if err != nil {
		t.Fatal(err)
	}
	if layout.RowsStart != 2 || layout.RowsCount != 3 {
		t.Fatalf("rows at %d for %d lines, want 2 for 3:\n%s", layout.RowsStart, layout.RowsCount, layout.Text)
	}
	if _, _, err := a.PostNotifyMessage("spaces/_c2fixtureAA", "", OutMessage{Text: "fallback", Chat: card}); err != nil {
		t.Fatal(err)
	}
	if _, reason := a.classify(gchatBoardTap(t, "retry", 1, "t_1,t_2")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap: %q", reason)
	}
	body := checkPatch(t, f, 1, gchatBoardMessage)
	want := "*2 tasks are stuck and need someone to decide.*\n\n" +
		"1. *Triage x* · Stopped 1 day ago before finishing. — ✓ Retrying. Test Sender, 15:04 UTC\n" +
		"2. *Open GitOps PR for y* · Waiting on you.\n3. *Too many to act on*\n\n*why*\n1. x\n2. y"
	if body["text"] != want {
		t.Errorf("text =\n%s\nwant\n%s", body["text"], want)
	}
	if plain, err := renderGchatChat(&chatmsg.Message{Schema: chatmsg.Schema, Rows: []chatmsg.Row{{Text: "a"}}}); err != nil || plain.RowsCount != 0 {
		t.Errorf("rows without buttons recorded a block: %+v, %v", plain, err)
	}
}

// A tap that finds its row moved frees only its own claim: a newer tap that
// claimed the same row key in the meantime keeps its hold. The actor fake
// runs the re-layout and the newer tap from inside the first tap's round
// trip, which is the window.
func TestGchatBoardMovedTapFreesOnlyItsOwnClaim(t *testing.T) {
	a, f, _ := newBoardGchatAdapter(t)
	postBoardCard(t, a)
	again := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "1 task is stuck.",
		Rows: []chatmsg.Row{{Text: "**Only z**", Tasks: []string{"t_9"}, Actions: []string{"retry", "drop"}}}}
	calls := 0
	a.SetBoardActor(func(_ context.Context, act lib.BoardAction) (*lib.BoardActionResult, error) {
		calls++
		if calls == 1 {
			if err := a.EditNotifyMessage("spaces/_c2fixtureAA", "", gchatBoardMessage, OutMessage{Text: "again", Chat: again}); err != nil {
				t.Fatal(err)
			}
			// The newer tap, on the new row 1, runs to its end here.
			if _, reason := a.classify(gchatBoardTap(t, "drop", 1, "t_9")); !strings.HasPrefix(reason, gchatBoardTaken) {
				t.Fatalf("newer tap: %q", reason)
			}
		}
		return &lib.BoardActionResult{Action: act.Action, Results: []lib.BoardActionOutcome{{ID: act.Tasks[0], Outcome: lib.BoardOutcomeDone}}}, nil
	})
	if _, reason := a.classify(gchatBoardTap(t, "retry", 1, "t_1,t_2")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("first tap: %q", reason)
	}
	if calls != 2 || relayCallCount(f) != 3 {
		t.Fatalf("%d board actions, %d relay calls; want 2 and 3 (create, re-layout, the newer tap's edit)", calls, relayCallCount(f))
	}
	if body := checkPatch(t, f, 2, gchatBoardMessage); body["text"] != "*1 task is stuck.*\n\n1. *Only z* — Dropped. Test Sender, 15:04 UTC" {
		t.Errorf("the newer tap's edit = %q", body["text"])
	}
	// The newer tap settled its row and keeps the claim: the first tap's
	// release did not free it.
	if _, reason := a.classify(gchatBoardTap(t, "retry", 1, "t_9")); reason != gchatBoardBusy {
		t.Errorf("a tap on the settled new row: %q", reason)
	}
}

// A row whose own text holds a note's words after a separator is kept
// whole: a tap cuts only a whole line a tap wrote.
func TestGchatBoardRowTextWithANoteInItIsKept(t *testing.T) {
	a, f, actor := newBoardGchatAdapter(t)
	card := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "1 task is stuck.",
		Rows: []chatmsg.Row{{Text: "**Triage x — Dropped. from the queue**", Tasks: []string{"t_1"}, Actions: []string{"retry", "drop"}}}}
	if _, _, err := a.PostNotifyMessage("spaces/_c2fixtureAA", "", OutMessage{Text: "fallback", Chat: card}); err != nil {
		t.Fatal(err)
	}
	actor.err = errors.New("no bridge")
	if _, reason := a.classify(gchatBoardTap(t, "retry", 1, "t_1")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("tap: %q", reason)
	}
	body := checkPatch(t, f, 1, gchatBoardMessage)
	if want := "*1 task is stuck.*\n\n1. *Triage x — Dropped. from the queue* — Couldn't retry it just now. Try again."; body["text"] != want {
		t.Errorf("after the failed tap =\n%s\nwant\n%s", body["text"], want)
	}
	actor.err = nil
	if _, reason := a.classify(gchatBoardTap(t, "drop", 1, "t_1")); !strings.HasPrefix(reason, gchatBoardTaken) {
		t.Fatalf("second tap: %q", reason)
	}
	body = checkPatch(t, f, 2, gchatBoardMessage)
	if want := "*1 task is stuck.*\n\n1. *Triage x — Dropped. from the queue* — Dropped. Test Sender, 15:04 UTC"; body["text"] != want {
		t.Errorf("after the drop =\n%s\nwant\n%s", body["text"], want)
	}
}
