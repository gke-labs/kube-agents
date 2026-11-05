package gateway

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

const gchatRatedMessage = "spaces/_c2fixtureAA/messages/BOT1.BOT1"

// newFeedbackGchatAdapter is an adapter holding the recorder, over a relay
// whose create and patch answer with one bot message.
func newFeedbackGchatAdapter(t *testing.T) (*GoogleChatAdapter, *fakeChatRelay, *fakeRecorder) {
	t.Helper()
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": gchatRatedMessage,
		"thread": map[string]any{"name": "spaces/_c2fixtureAA/threads/BOT1"}}
	f.responses["spaces.messages/patch"] = map[string]any{"name": gchatRatedMessage}
	a := newTestGchatAdapterWithRelay(t, f)
	a.syncRewrites = true
	rec := &fakeRecorder{admit: map[string]bool{"sender@example.com": true}}
	a.SetFeedbackRecorder(rec)
	return a, f, rec
}

// gchatFeedbackQuestion is a question with two choices and one link.
func gchatFeedbackQuestion() *chatmsg.Message {
	return &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Which cluster?",
		Choices: []chatmsg.Choice{{Text: "Look at the first one", Reply: "Look at the first one: prod-eu", Primary: true}, {Text: "Skip"}},
		Links:   []chatmsg.Link{{Text: "board", URL: "https://x.example/b"}}}
}

// callBody is the body a relay call sent.
func callBody(t *testing.T, c relayCall) map[string]any {
	t.Helper()
	body, ok := c.arguments["body"].(map[string]any)
	if !ok {
		t.Fatalf("call %+v has no body", c)
	}
	return body
}

// bodyButtons decodes a call body's accessory widgets, which must be one
// button list, into its buttons.
func bodyButtons(t *testing.T, body map[string]any) []gchatButton {
	t.Helper()
	raw, err := json.Marshal(body["accessoryWidgets"])
	if err != nil {
		t.Fatal(err)
	}
	var widgets []gchatAccessoryWidget
	if err := json.Unmarshal(raw, &widgets); err != nil {
		t.Fatal(err)
	}
	if len(widgets) != 1 {
		t.Fatalf("accessoryWidgets = %s, want one button list", raw)
	}
	return widgets[0].ButtonList.Buttons
}

func buttonParams(b gchatButton) map[string]string {
	out := map[string]string{}
	for _, p := range b.OnClick.Action.Parameters {
		out[p.Key] = p.Value
	}
	return out
}

func checkRatingButton(t *testing.T, b gchatButton, text, alt, rating string) {
	t.Helper()
	params := buttonParams(b)
	if b.Text != text || b.AltText != alt || b.Type != "" || b.OnClick.Action.Function != "kage.feedback" ||
		params["task"] != "t-root" || params["rating"] != rating || len(params) != 2 {
		t.Errorf("rating button = %+v params %v, want %s/%s/%s", b, params, text, alt, rating)
	}
}

func TestGchatRatedAnswerEndsWithFeedbackButtons(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	name, err := a.PostMessage("gchat:dm/spaces/_c2fixtureAA", OutMessage{Text: "fallback", Chat: gchatFeedbackQuestion(), Feedback: "t-root"})
	if err != nil || name != gchatRatedMessage {
		t.Fatalf("post: %q %v", name, err)
	}
	buttons := bodyButtons(t, callBody(t, f.call(0)))
	if len(buttons) != 5 {
		t.Fatalf("buttons = %+v, want the 3 drawn and 2 rating", buttons)
	}
	checkRatingButton(t, buttons[3], "👍", "Good answer", "up")
	checkRatingButton(t, buttons[4], "👎", "Bad answer", "down")
	want, _ := renderGchatChat(gchatFeedbackQuestion())
	raw, _ := json.Marshal(want.Widgets)
	var wantWidgets []gchatAccessoryWidget
	if err := json.Unmarshal(raw, &wantWidgets); err != nil {
		t.Fatal(err)
	}
	got, _ := json.Marshal(buttons[:3])
	drawn, _ := json.Marshal(wantWidgets[0].ButtonList.Buttons)
	if string(got) != string(drawn) {
		t.Errorf("the drawn buttons changed:\n got %s\nwant %s", got, drawn)
	}
	if r := a.ratingOf(name); r.task != "t-root" || r.chosen != "" {
		t.Errorf("ratingOf = %+v, want t-root unchosen", r)
	}
	a.mu.Lock()
	kept := a.uxLocked().questions[name].layout
	a.mu.Unlock()
	if keptJSON, _ := json.Marshal(kept.Widgets); string(keptJSON) != string(raw) {
		t.Errorf("the remembered question carries\n %s\nwant the layout without rating buttons\n %s", keptJSON, raw)
	}
}

func TestGchatRatedTextAnswerIsTextAndTheButtons(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	name, err := a.PostMessage("gchat:dm/spaces/_c2fixtureAA", OutMessage{Text: "**It is OOMKilled.**", Feedback: "t-root"})
	if err != nil || name != gchatRatedMessage {
		t.Fatalf("post: %q %v", name, err)
	}
	body := callBody(t, f.call(0))
	if body["text"] != "*It is OOMKilled.*" {
		t.Errorf("text = %q", body["text"])
	}
	buttons := bodyButtons(t, body)
	if len(buttons) != 2 {
		t.Fatalf("buttons = %+v, want the two rating buttons", buttons)
	}
	checkRatingButton(t, buttons[0], "👍", "Good answer", "up")
	checkRatingButton(t, buttons[1], "👎", "Bad answer", "down")
	if r := a.ratingOf(name); r.task != "t-root" {
		t.Errorf("ratingOf = %+v", r)
	}
}

func TestGchatDrawsNoButtonsWithoutTheRecorder(t *testing.T) {
	a := newTestGchatAdapter(t)
	if a.rates(OutMessage{Text: "x", Feedback: "t"}) {
		t.Error("no recorder handed over: no rating controls")
	}
	a.SetFeedbackRecorder(&fakeRecorder{})
	if a.rates(OutMessage{Text: "x"}) {
		t.Error("an unmarked message carries no rating controls")
	}
	if !a.rates(OutMessage{Text: "x", Feedback: "t"}) {
		t.Error("a marked answer with the recorder handed over is rated")
	}
}

func TestGchatRefusedRatedAnswerPostsWithoutTheButtons(t *testing.T) {
	const create = "spaces.messages/create"
	ok := map[string]any{"name": gchatRatedMessage}
	refused := fakeChatAPIError{status: 400, reason: "invalid accessory widget"}
	conversation := "gchat:dm/spaces/_c2fixtureAA"

	a, f, _ := newFeedbackGchatAdapter(t)
	f.queue[create] = []any{refused, ok}
	name, err := a.PostMessage(conversation, OutMessage{Text: "fallback", Chat: gchatFeedbackQuestion(), Feedback: "t-root"})
	if err != nil || name != gchatRatedMessage {
		t.Fatalf("laid-out post: %q %v, want the name of the post without the rating", name, err)
	}
	buttons := bodyButtons(t, callBody(t, f.call(1)))
	if len(buttons) != 3 {
		t.Fatalf("second body buttons = %+v, want 3", buttons)
	}
	for _, b := range buttons {
		if classifyGchatButton(b) == gchatButtonRating {
			t.Errorf("the second post carries a rating button: %+v", b)
		}
	}
	if r := a.ratingOf(name); r.task != "" {
		t.Errorf("a post without the buttons is remembered as rated: %+v", r)
	}

	a, f, _ = newFeedbackGchatAdapter(t)
	f.queue[create] = []any{refused, ok}
	name, err = a.PostMessage(conversation, OutMessage{Text: "**It is OOMKilled.**", Feedback: "t-root"})
	if err != nil || name != gchatRatedMessage {
		t.Fatalf("text post: %q %v", name, err)
	}
	body := callBody(t, f.call(1))
	if _, has := body["accessoryWidgets"]; has || body["text"] != "*It is OOMKilled.*" {
		t.Errorf("second text body = %+v, want the text alone", body)
	}
	if r := a.ratingOf(name); r.task != "" {
		t.Errorf("a text post without the buttons is remembered as rated: %+v", r)
	}

	a, f, _ = newFeedbackGchatAdapter(t)
	f.queue[create] = []any{refused, refused}
	if _, err := a.PostMessage(conversation, OutMessage{Text: "fallback", Chat: gchatFeedbackQuestion(), Feedback: "t-root"}); !errors.Is(err, ErrLayoutRefused) {
		t.Errorf("both laid-out posts refused: err = %v, want ErrLayoutRefused", err)
	}

	a, f, _ = newFeedbackGchatAdapter(t)
	f.queue[create] = []any{refused, refused}
	if _, err := a.PostMessage(conversation, OutMessage{Text: "x", Feedback: "t-root"}); err == nil {
		t.Error("both text posts refused: want an error")
	}
}

func TestGchatHomePostDrawsNoRatingButtons(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	name, _, err := a.PostNotifyMessage("spaces/S1", "", OutMessage{Text: "x", Chat: gchatFeedbackQuestion(), Feedback: "t-root"})
	if err != nil {
		t.Fatal(err)
	}
	for _, b := range bodyButtons(t, callBody(t, f.call(0))) {
		if classifyGchatButton(b) == gchatButtonRating {
			t.Errorf("a home post carries a rating button: %+v", b)
		}
	}
	if r := a.ratingOf(name); r.task != "" {
		t.Errorf("ratingOf(home post) = %+v", r)
	}
}

func TestGchatButtonKindsAreToldApartByFunction(t *testing.T) {
	ev := gchatFixtureEvent(t, "addon-dm-button-click.json", nil)
	buttons := append([]gchatButton{}, ev.Message.AccessoryWidgets[0].ButtonList.Buttons...)
	raw, _ := json.Marshal(gchatRatingButtons(gchatRating{task: "t-root"}))
	var rating []gchatButton
	if err := json.Unmarshal(raw, &rating); err != nil {
		t.Fatal(err)
	}
	buttons = append(buttons, rating...)
	var note, other gchatButton
	note.OnClick.Action.Function = "kage.feedback-note"
	other.OnClick.Action.Function = "kage.other"
	buttons = append(buttons, note)

	want := []gchatButtonKind{gchatButtonChoice, gchatButtonChoice, gchatButtonLink, gchatButtonRating, gchatButtonRating, gchatButtonNote}
	if len(buttons) != len(want) {
		t.Fatalf("buttons = %+v", buttons)
	}
	if buttons[0].Type != gchatChatButtonFilled {
		t.Fatalf("fixture's first button is %+v, want the FILLED primary", buttons[0])
	}
	for i, b := range buttons {
		if got := classifyGchatButton(b); got != want[i] {
			t.Errorf("button %d (%+v) = %q, want %q", i, b, got, want[i])
		}
	}
	if got := classifyGchatButton(other); got != gchatButtonOther {
		t.Errorf("kage.other = %q, want other", got)
	}
}

func TestGchatRatingButtonsRoundTrip(t *testing.T) {
	original := gchatRatingButtons(gchatRating{task: "t-root", chosen: FeedbackUp})
	raw, err := json.Marshal(original)
	if err != nil {
		t.Fatal(err)
	}
	var decoded []gchatButton
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatal(err)
	}
	if len(decoded) != 2 || decoded[0].Text != "👍" || decoded[0].Type != "FILLED" || decoded[0].AltText != "Good answer" ||
		decoded[1].Text != "👎" || decoded[1].Type != "" || decoded[1].AltText != "Bad answer" {
		t.Fatalf("decoded = %+v", decoded)
	}
	for i := range decoded {
		got, _ := json.Marshal(gchatButtonWidget(decoded[i]))
		want, _ := json.Marshal(original[i])
		if string(got) != string(want) {
			t.Errorf("button %d re-encodes as\n %s\nwant %s", i, got, want)
		}
	}
}

// gchatRatingEvent is the add-on click fixture as a tap on rating, with the
// rating buttons at the end of the message's button list, as Chat shows a
// rated answer.
func gchatRatingEvent(t *testing.T, rating string) *gchatEvent {
	t.Helper()
	ev := gchatClickEvent(t)
	ev.click.Function = gchatFeedbackFunction
	ev.click.Parameters = map[string]string{"task": "t-root", "rating": rating}
	raw, _ := json.Marshal(gchatRatingButtons(gchatRating{task: "t-root"}))
	var buttons []gchatButton
	if err := json.Unmarshal(raw, &buttons); err != nil {
		t.Fatal(err)
	}
	list := &ev.Message.AccessoryWidgets[0].ButtonList
	list.Buttons = append(list.Buttons, buttons...)
	return ev
}

// gchatSpaceEvent moves ev into a threaded space.
func gchatSpaceEvent(ev *gchatEvent) *gchatEvent {
	ev.Space = gchatSpace{Name: "spaces/AAA", SpaceType: "SPACE", SpaceThreadingState: "THREADED_MESSAGES"}
	ev.Message.Name = "spaces/AAA/messages/M1"
	ev.Message.Thread.Name = "spaces/AAA/threads/T1"
	return ev
}

func relayCallCount(f *fakeChatRelay) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.calls)
}

func recordedRatings(rec *fakeRecorder) []FeedbackRating {
	rec.mu.Lock()
	defer rec.mu.Unlock()
	return append([]FeedbackRating{}, rec.ratings...)
}

// checkPatch checks that call i patches message's layout, and returns its
// body.
func checkPatch(t *testing.T, f *fakeChatRelay, i int, message string) map[string]any {
	t.Helper()
	c := f.call(i)
	if c.method != "patch" || c.arguments["name"] != message || c.arguments["updateMask"] != gchatLayoutUpdateMask {
		t.Fatalf("call %d = %+v, want a patch of %s's layout", i, c, message)
	}
	return callBody(t, c)
}

// checkFilled checks the two rating buttons at the end of buttons: the
// chosen one FILLED, the other with no type.
func checkFilled(t *testing.T, buttons []gchatButton, chosen string) {
	t.Helper()
	if len(buttons) < 2 {
		t.Fatalf("buttons = %+v, want the two rating buttons last", buttons)
	}
	up, down := buttons[len(buttons)-2], buttons[len(buttons)-1]
	wantUp, wantDown := "", ""
	if chosen == "up" {
		wantUp = "FILLED"
	} else {
		wantDown = "FILLED"
	}
	if up.Text != "👍" || up.Type != wantUp || buttonParams(up)["rating"] != "up" || buttonParams(up)["task"] != "t-root" {
		t.Errorf("👍 = %+v, want type %q", up, wantUp)
	}
	if down.Text != "👎" || down.Type != wantDown || buttonParams(down)["rating"] != "down" || buttonParams(down)["task"] != "t-root" {
		t.Errorf("👎 = %+v, want type %q", down, wantDown)
	}
}

func TestGchatRatingInBothEventLayoutsIsRecorded(t *testing.T) {
	a, _, rec := newFeedbackGchatAdapter(t)
	if _, reason := a.classify(gchatRatingEvent(t, "down")); reason == "" {
		t.Fatal("an add-on rating tap is a turn")
	}
	raw, err := os.ReadFile("testdata/gchat/legacy-dm-button-click.json")
	if err != nil {
		t.Fatal(err)
	}
	var wire map[string]any
	if err := json.Unmarshal(raw, &wire); err != nil {
		t.Fatal(err)
	}
	wire["common"] = map[string]any{"invokedFunction": "kage.feedback", "parameters": map[string]any{"task": "t-root", "rating": "down"}}
	legacy, _ := json.Marshal(wire)
	ev, err := decodeGchatEvent(base64.StdEncoding.EncodeToString(legacy))
	if err != nil {
		t.Fatal(err)
	}
	if _, reason := a.classify(ev); reason == "" {
		t.Fatal("a legacy rating tap is a turn")
	}
	got := recordedRatings(rec)
	if len(got) != 2 {
		t.Fatalf("ratings = %+v, want one per layout", got)
	}
	for i, r := range got {
		if r.TaskID != "t-root" || r.Rating != "down" || r.Rater != "sender@example.com" {
			t.Errorf("rating %d = %+v", i, r)
		}
	}
}

func TestGchatRatingTapsTheGatewayCannotReadAreDropped(t *testing.T) {
	cases := map[string]func(ev *gchatEvent){
		"no task":    func(ev *gchatEvent) { delete(ev.click.Parameters, "task") },
		"sideways":   func(ev *gchatEvent) { ev.click.Parameters["rating"] = "sideways" },
		"no email":   func(ev *gchatEvent) { ev.click.User.Email = "" },
		"no message": func(ev *gchatEvent) { ev.Message.Name = "" },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			a, f, rec := newFeedbackGchatAdapter(t)
			ev := gchatRatingEvent(t, "up")
			mutate(ev)
			msg, reason := a.classify(ev)
			if reason == "" || msg != (InboundMessage{}) {
				t.Errorf("classify = %+v %q, want a reason and no turn", msg, reason)
			}
			if got := recordedRatings(rec); len(got) != 0 {
				t.Errorf("ratings = %+v, want none", got)
			}
			if n := relayCallCount(f); n != 0 {
				t.Errorf("relay calls = %d, want none", n)
			}
		})
	}
}

func TestGchatUpRatingIsRecordedAndIsNoTurn(t *testing.T) {
	a, _, rec := newFeedbackGchatAdapter(t)
	msg, reason := a.classify(gchatRatingEvent(t, "up"))
	if msg != (InboundMessage{}) || reason == "" {
		t.Fatalf("classify = %+v %q, want no turn and a reason", msg, reason)
	}
	got := recordedRatings(rec)
	want := FeedbackRating{Platform: "googlechat", Backend: "gchat", Rater: "sender@example.com",
		Conversation: "gchat:dm/spaces/_c2fixtureAA/threads/BOT1", MessageID: "spaces/_c2fixtureAA/messages/BOT1.BOT1",
		TaskID: "t-root", Rating: "up", Answer: "*Which cluster?*\n\nTwo clusters run one."}
	if len(got) != 1 || got[0] != want {
		t.Errorf("ratings = %+v\nwant [%+v]", got, want)
	}
}

func TestGchatRatingAfterARestartPatchesFromTheEventsCopy(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	ev := gchatRatingEvent(t, "up")
	a.classify(ev)
	if n := relayCallCount(f); n != 1 {
		t.Fatalf("relay calls = %d, want one patch", n)
	}
	body := checkPatch(t, f, 0, gchatRatedMessage)
	if body["text"] != ev.Message.Text {
		t.Errorf("text = %q, want the event's %q", body["text"], ev.Message.Text)
	}
	buttons := bodyButtons(t, body)
	if len(buttons) != 5 {
		t.Fatalf("buttons = %+v, want two choices, the link and two rating", buttons)
	}
	if buttons[0].Text != "Look at the first one" || buttons[0].Type != "FILLED" || classifyGchatButton(buttons[0]) != gchatButtonChoice ||
		buttonParams(buttons[0])["reply"] != "Look at the first one: prod-eu" {
		t.Errorf("first choice = %+v", buttons[0])
	}
	if buttons[1].Text != "Skip" || buttons[1].Type != "" || classifyGchatButton(buttons[1]) != gchatButtonChoice {
		t.Errorf("second choice = %+v", buttons[1])
	}
	if buttons[2].Text != "board" || buttons[2].OnClick.OpenLink.URL != "https://x.example/b" {
		t.Errorf("link = %+v", buttons[2])
	}
	checkFilled(t, buttons, "up")
}

func TestGchatRatingOnARememberedAnswerPatchesFromItsLayout(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	name, err := a.PostMessage("gchat:dm/spaces/_c2fixtureAA", OutMessage{Text: "fallback", Chat: gchatFeedbackQuestion(), Feedback: "t-root"})
	if err != nil {
		t.Fatal(err)
	}
	posted := callBody(t, f.call(0))
	ev := gchatRatingEvent(t, "down")
	ev.Message.Text = "a stale copy"
	a.classify(ev)
	body := checkPatch(t, f, 1, name)
	if body["text"] != posted["text"] {
		t.Errorf("text = %q, want the posted %q", body["text"], posted["text"])
	}
	buttons := bodyButtons(t, body)
	if len(buttons) != 5 {
		t.Fatalf("buttons = %+v", buttons)
	}
	kinds := []gchatButtonKind{gchatButtonChoice, gchatButtonChoice, gchatButtonLink, gchatButtonRating, gchatButtonRating}
	for i, b := range buttons {
		if classifyGchatButton(b) != kinds[i] {
			t.Errorf("button %d = %+v, want %q", i, b, kinds[i])
		}
	}
	checkFilled(t, buttons, "down")
	// The tap claimed nothing: a choice click is still a turn.
	if _, reason := a.classify(gchatClickEvent(t)); reason != "" {
		t.Errorf("a choice click after the tap is dropped: %q", reason)
	}
}

func TestGchatRatingNeverRestoresAnsweredChoices(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	name, err := a.PostMessage("gchat:dm/spaces/_c2fixtureAA", OutMessage{Text: "fallback", Chat: gchatFeedbackQuestion(), Feedback: "t-root"})
	if err != nil {
		t.Fatal(err)
	}
	msg, reason := a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatalf("choice click: %q", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	// The event's copy predates the answered rewrite: no line, choices.
	a.classify(gchatRatingEvent(t, "up"))
	body := checkPatch(t, f, 2, name)
	answered := callBody(t, f.call(1))["text"].(string)
	if text, _ := body["text"].(string); text != answered || !strings.HasSuffix(text, "✓ Test Sender: Look at the first one") {
		t.Errorf("text = %q, want the answered rewrite's %q, ending with its line", text, answered)
	}
	buttons := bodyButtons(t, body)
	for _, b := range buttons {
		if classifyGchatButton(b) == gchatButtonChoice {
			t.Errorf("the tap restored a choice: %+v", b)
		}
	}
	if len(buttons) != 3 || classifyGchatButton(buttons[0]) != gchatButtonLink || buttons[0].OnClick.OpenLink.URL != "https://x.example/b" {
		t.Fatalf("buttons = %+v, want the link and two rating", buttons)
	}
	checkFilled(t, buttons, "up")
}

// An edit of an answered question that lays out no choices keeps the line;
// a later tap redraws that edit, not the layout the answer first put on.
func TestGchatRatingAfterAnEditOfTheAnswerRedrawsTheEdit(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	conversation := "gchat:dm/spaces/_c2fixtureAA"
	name, err := a.PostMessage(conversation, OutMessage{Text: "fallback", Chat: gchatFeedbackQuestion(), Feedback: "t-root"})
	if err != nil {
		t.Fatal(err)
	}
	msg, reason := a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatalf("choice click: %q", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	if err := a.EditMessage(conversation, name, OutMessage{Text: "Settled: prod-eu."}); err != nil {
		t.Fatal(err)
	}
	edited := callBody(t, f.call(2))["text"].(string)
	if !strings.HasPrefix(edited, "Settled: prod-eu.") || !strings.HasSuffix(edited, "✓ Test Sender: Look at the first one") {
		t.Fatalf("edit text = %q, want the new text and the line", edited)
	}
	a.classify(gchatRatingEvent(t, "down"))
	body := checkPatch(t, f, 3, name)
	if body["text"] != edited {
		t.Errorf("text = %q, want the edit's %q", body["text"], edited)
	}
	buttons := bodyButtons(t, body)
	if len(buttons) != 2 {
		t.Fatalf("buttons = %+v, want the two rating buttons alone", buttons)
	}
	checkFilled(t, buttons, "down")
}

// The rewrite runs after classify returns, so it reads its own copy of the
// message: a caller that changes the event afterwards changes nothing.
func TestGchatRatingPatchReadsItsOwnCopyOfTheMessage(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	ev := gchatRatingEvent(t, "up")
	text := ev.Message.Text
	rec.onRate = func() {
		ev.Message.Text = "changed"
		buttons := ev.Message.AccessoryWidgets[0].ButtonList.Buttons
		buttons[2].Text = "changed"
		buttons[0].OnClick.Action.Parameters[0].Value = "changed"
	}
	a.classify(ev)
	body := checkPatch(t, f, 0, gchatRatedMessage)
	if body["text"] != text {
		t.Errorf("text = %q, want %q", body["text"], text)
	}
	buttons := bodyButtons(t, body)
	if len(buttons) != 5 || buttons[2].Text != "board" || buttonParams(buttons[0])["reply"] != "Look at the first one: prod-eu" {
		t.Errorf("buttons = %+v, want the event's as classified", buttons)
	}
}

func TestGchatReRatingReplacesTheFilledButton(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	a.classify(gchatRatingEvent(t, "down"))
	checkFilled(t, bodyButtons(t, checkPatch(t, f, 0, gchatRatedMessage)), "down")
	a.classify(gchatRatingEvent(t, "up"))
	// Call 1 is the note card the 👎 asked with.
	checkFilled(t, bodyButtons(t, checkPatch(t, f, 2, gchatRatedMessage)), "up")
	if r := a.ratingOf(gchatRatedMessage); r.task != "t-root" || r.chosen != FeedbackUp {
		t.Errorf("ratingOf = %+v, want t-root up", r)
	}
	if got := recordedRatings(rec); len(got) != 2 {
		t.Errorf("ratings = %+v, want two", got)
	}
}

// rapidTaps classifies two taps by one rater on one message back to back,
// first at an older event time than second, with the first Rate held until
// both are taken, then waits for every rewrite to finish. With inRate, the
// second is taken only once the first tap's Rate has started. It returns
// the last patch's buttons and the number of note cards created.
func rapidTaps(t *testing.T, a *GoogleChatAdapter, f *fakeChatRelay, rec *fakeRecorder, first, second string, inRate bool) ([]gchatButton, int) {
	t.Helper()
	a.syncRewrites = false
	entered, taken := make(chan struct{}), make(chan struct{})
	var rates atomic.Int32
	rec.onRate = func() {
		if rates.Add(1) == 1 {
			close(entered)
			<-taken
		}
	}
	older := gchatRatingEvent(t, first)
	older.click.EventTime = "2026-10-29T20:03:00.100000Z"
	newer := gchatRatingEvent(t, second)
	newer.click.EventTime = "2026-10-29T20:03:00.2Z"
	a.classify(older)
	if inRate {
		<-entered
	}
	a.classify(newer)
	close(taken)
	a.rewrites.Wait()
	last, cards := -1, 0
	for i := range relayCallCount(f) {
		switch f.call(i).method {
		case "patch":
			last = i
		case "create":
			cards++
		}
	}
	if last < 0 {
		t.Fatal("no patch")
	}
	return bodyButtons(t, checkPatch(t, f, last, gchatRatedMessage)), cards
}

// Two taps by one rater on one message end with the later tap's rating in
// the bucket, on record and filled, whichever goroutine runs first; a 👎
// the later tap replaced posts no note card.
func TestGchatRapidTapsOnOneMessageKeepTheLastOne(t *testing.T) {
	for _, tc := range []struct {
		name          string
		first, second string
		cards         int
	}{
		{"down then up", "down", "up", 0},
		{"up then down", "up", "down", 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			a, f, rec := newFeedbackGchatAdapter(t)
			buttons, cards := rapidTaps(t, a, f, rec, tc.first, tc.second, false)
			got := recordedRatings(rec)
			if len(got) == 0 || got[len(got)-1].Rating != tc.second {
				t.Errorf("ratings = %+v, want %s last", got, tc.second)
			}
			if r := a.ratingOf(gchatRatedMessage); r.task != "t-root" || r.chosen != tc.second {
				t.Errorf("ratingOf = %+v, want t-root %s", r, tc.second)
			}
			checkFilled(t, buttons, tc.second)
			if cards != tc.cards {
				t.Errorf("note cards = %d, want %d", cards, tc.cards)
			}
		})
	}
}

// When the later tap, taken while the older one's Rate ran, fails its own
// write, the older tap stays in the bucket and drawn. Its note card posts
// only when the newer tap's failure lands before the 👎 asks for one.
func TestGchatRapidTapsWhoseNewerWriteFailsKeepTheOlderOneDrawn(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	rec.failRating = "up"
	buttons, cards := rapidTaps(t, a, f, rec, "down", "up", true)
	if got := recordedRatings(rec); len(got) != 1 || got[0].Rating != "down" {
		t.Errorf("ratings = %+v, want the 👎 alone", got)
	}
	if r := a.ratingOf(gchatRatedMessage); r.chosen != "down" {
		t.Errorf("ratingOf = %+v, want down", r)
	}
	checkFilled(t, buttons, "down")
	if cards > 1 {
		t.Errorf("note cards = %d, want at most one", cards)
	}
}

// An older 👎 whose Rate is running when a newer 👍 comes in patches, and
// the 👍 repaints after it; the 👎 asks for no note.
func TestGchatATapTakenDuringAnotherWritePatchesAndIsRepainted(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	buttons, cards := rapidTaps(t, a, f, rec, "down", "up", true)
	if got := recordedRatings(rec); len(got) != 2 || got[0].Rating != "down" || got[1].Rating != "up" {
		t.Errorf("ratings = %+v, want down then up", got)
	}
	patches := 0
	for i := range relayCallCount(f) {
		if f.call(i).method == "patch" {
			patches++
		}
	}
	if patches != 2 {
		t.Errorf("patches = %d, want the 👎's and the 👍's", patches)
	}
	checkFilled(t, buttons, "up")
	if cards != 0 {
		t.Errorf("note cards = %d, want none", cards)
	}
}

// Taps C (oldest), A, B (newest) by one rater: B is taken while A's Rate
// runs and its own write fails, so A's time is the newest again, and C,
// arriving last, is superseded by it.
func TestGchatAFailedTapWriteRestoresTheTimeItDisplaced(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	rec.failRating = "down"
	rapidTaps(t, a, f, rec, "up", "down", true)
	rec.mu.Lock()
	rec.failRating = ""
	rec.mu.Unlock()
	late := gchatRatingEvent(t, "down")
	late.click.EventTime = "2026-10-29T20:03:00Z"
	a.classify(late)
	a.rewrites.Wait()
	if got := recordedRatings(rec); len(got) != 1 || got[0].Rating != "up" {
		t.Errorf("ratings = %+v, want A's 👍 alone", got)
	}
	if r := a.ratingOf(gchatRatedMessage); r.chosen != "up" {
		t.Errorf("ratingOf = %+v, want up", r)
	}
	if n := relayCallCount(f); n != 1 {
		t.Fatalf("relay calls = %d, want A's patch alone", n)
	}
	checkFilled(t, bodyButtons(t, checkPatch(t, f, 0, gchatRatedMessage)), "up")
}

// A tap with no event time is never superseded, and never supersedes: a
// timed tap after a timeless one is recorded and drawn.
func TestGchatTapWithoutEventTimeIsNeverSuperseded(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	a.syncRewrites = false
	timeless := gchatRatingEvent(t, "up")
	timeless.click.EventTime = ""
	a.classify(timeless)
	a.rewrites.Wait()
	timed := gchatRatingEvent(t, "down")
	timed.click.EventTime = "2026-10-29T20:04:00Z"
	a.classify(timed)
	a.rewrites.Wait()
	if got := recordedRatings(rec); len(got) != 2 || got[1].Rating != "down" {
		t.Fatalf("ratings = %+v, want up then down", got)
	}
	checkFilled(t, bodyButtons(t, checkPatch(t, f, 1, gchatRatedMessage)), "down")
	if r := a.ratingOf(gchatRatedMessage); r.chosen != "down" {
		t.Errorf("ratingOf = %+v, want down", r)
	}
}

func TestGchatRatingFromAnUnverifiedSenderChangesNothing(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	rec.admit = map[string]bool{}
	if _, reason := a.classify(gchatRatingEvent(t, "up")); reason == "" {
		t.Error("an unverified rating tap is a turn")
	}
	if got := recordedRatings(rec); len(got) != 0 {
		t.Errorf("ratings = %+v", got)
	}
	if n := relayCallCount(f); n != 0 {
		t.Errorf("relay calls = %d", n)
	}
	if r := a.ratingOf(gchatRatedMessage); r.chosen != "" {
		t.Errorf("ratingOf = %+v", r)
	}
}

func TestGchatRedeliveredRatingIsHandledOnce(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	ev := gchatRatingEvent(t, "up")
	a.classify(ev)
	_, reason := a.classify(ev)
	if !strings.Contains(reason, "duplicate") {
		t.Errorf("second reason = %q, want a duplicate", reason)
	}
	if got := recordedRatings(rec); len(got) != 1 {
		t.Errorf("ratings = %+v, want one", got)
	}
	if n := relayCallCount(f); n != 1 {
		t.Errorf("relay calls = %d, want one patch", n)
	}
}

func TestGchatFailedRatingWriteLeavesTheMessage(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	rec.rateErr = errFeedbackContended
	a.classify(gchatRatingEvent(t, "down"))
	if n := relayCallCount(f); n != 0 {
		t.Errorf("relay calls = %d, want none", n)
	}
	if r := a.ratingOf(gchatRatedMessage); r.task != "" {
		t.Errorf("ratingOf = %+v, want nothing remembered", r)
	}
}

func TestGchatRatingWithFeedbackOffChangesNothing(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/patch"] = map[string]any{"name": gchatRatedMessage}
	a := newTestGchatAdapterWithRelay(t, f)
	a.syncRewrites = true
	if _, reason := a.classify(gchatRatingEvent(t, "up")); reason == "" {
		t.Error("a rating tap with feedback off is a turn")
	}
	if n := relayCallCount(f); n != 0 {
		t.Errorf("relay calls = %d, want none", n)
	}
	if r := a.ratingOf(gchatRatedMessage); r.task != "" {
		t.Errorf("ratingOf = %+v", r)
	}
}

func TestGchatTwoRatersInASpaceKeepTheirOwnRatings(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	rec.admit = map[string]bool{"a@example.com": true, "b@example.com": true}
	first := gchatSpaceEvent(gchatRatingEvent(t, "up"))
	first.click.User.Email = "a@example.com"
	second := gchatSpaceEvent(gchatRatingEvent(t, "down"))
	second.click.User.Email = "b@example.com"
	a.classify(first)
	a.classify(second)
	got := recordedRatings(rec)
	if len(got) != 2 || got[0].Rater != "a@example.com" || got[1].Rater != "b@example.com" {
		t.Fatalf("ratings = %+v, want one per rater", got)
	}
	for i, r := range got {
		if r.Conversation != "gchat:spaces/AAA/threads/T1" || r.MessageID != "spaces/AAA/messages/M1" {
			t.Errorf("rating %d = %+v", i, r)
		}
	}
	// The last call is the second rater's note card; the one before it fills
	// their 👎.
	checkFilled(t, bodyButtons(t, checkPatch(t, f, relayCallCount(f)-2, "spaces/AAA/messages/M1")), "down")
}

const gchatNoteCardMessage = "spaces/_c2fixtureAA/messages/CARD1"

// gchatNoteEvent is the add-on click fixture as a click on the note card's
// Send (or Cancel), on the card's message, with formInputs when non-nil.
func gchatNoteEvent(t *testing.T, formInputs map[string]any, cancel bool) *gchatEvent {
	t.Helper()
	raw, _ := realGchatFixture(t, "addon-dm-button-click.json")
	var wire map[string]any
	if err := json.Unmarshal(raw, &wire); err != nil {
		t.Fatal(err)
	}
	params := map[string]any{"task": "t-root", "rating": "down"}
	if cancel {
		params["cancel"] = "1"
	}
	common := map[string]any{"invokedFunction": "kage.feedback-note", "parameters": params}
	if formInputs != nil {
		common["formInputs"] = formInputs
	}
	wire["commonEventObject"] = common
	chat := wire["chat"].(map[string]any)
	chat["eventTime"] = fmt.Sprintf("2026-10-29T20:02:%02d.000000Z", clickEvents.Add(1))
	payload := chat["buttonClickedPayload"].(map[string]any)
	message := payload["message"].(map[string]any)
	message["name"] = gchatNoteCardMessage
	delete(message, "accessoryWidgets")
	out, _ := json.Marshal(wire)
	ev, err := decodeGchatEvent(base64.StdEncoding.EncodeToString(out))
	if err != nil {
		t.Fatal(err)
	}
	return ev
}

// noteInputs is a formInputs object holding the note box's values.
func noteInputs(values ...string) map[string]any {
	return map[string]any{"note": map[string]any{"stringInputs": map[string]any{"value": values}}}
}

// cardOf is the one card a call body carries under cardsV2, and its one
// section's widgets.
func cardOf(t *testing.T, body map[string]any) (map[string]any, []map[string]any) {
	t.Helper()
	cards, ok := body["cardsV2"].([]any)
	if !ok || len(cards) != 1 {
		t.Fatalf("body = %+v, want one card under cardsV2", body)
	}
	card := cards[0].(map[string]any)
	sections := card["card"].(map[string]any)["sections"].([]any)
	if len(sections) != 1 {
		t.Fatalf("card = %+v, want one section", card)
	}
	var widgets []map[string]any
	for _, w := range sections[0].(map[string]any)["widgets"].([]any) {
		widgets = append(widgets, w.(map[string]any))
	}
	return card, widgets
}

// collapsedText checks that body is the collapsed note card and returns its
// one line.
func collapsedText(t *testing.T, body map[string]any) string {
	t.Helper()
	card, widgets := cardOf(t, body)
	if card["cardId"] != "kage.feedback-note" || len(widgets) != 1 {
		t.Fatalf("card = %+v, want the note card with one widget", card)
	}
	text, _ := widgets[0]["textParagraph"].(map[string]any)["text"].(string)
	return text
}

// checkNotePatch checks that call i patches the note card on message to
// one line, and returns the line.
func checkNotePatch(t *testing.T, f *fakeChatRelay, i int, message string) string {
	t.Helper()
	c := f.call(i)
	if c.method != "patch" || c.arguments["name"] != message || c.arguments["updateMask"] != "cards_v2" {
		t.Fatalf("call %d = %+v, want a cards_v2 patch of %s", i, c, message)
	}
	return collapsedText(t, callBody(t, c))
}

func recordedNotes(rec *fakeRecorder) []string {
	rec.mu.Lock()
	defer rec.mu.Unlock()
	return append([]string{}, rec.notes...)
}

func TestDecodeGchatFormInputsInBothLayouts(t *testing.T) {
	ev := gchatNoteEvent(t, noteInputs("x"), false)
	if v, ok := ev.click.formValue("note"); v != "x" || !ok {
		t.Errorf("add-on formValue = %q %v, want x true", v, ok)
	}
	raw, err := os.ReadFile("testdata/gchat/legacy-dm-button-click.json")
	if err != nil {
		t.Fatal(err)
	}
	var wire map[string]any
	if err := json.Unmarshal(raw, &wire); err != nil {
		t.Fatal(err)
	}
	wire["common"] = map[string]any{"invokedFunction": "kage.feedback-note",
		"parameters": map[string]any{"task": "t-root", "rating": "down"}, "formInputs": noteInputs("x")}
	legacy, _ := json.Marshal(wire)
	lev, err := decodeGchatEvent(base64.StdEncoding.EncodeToString(legacy))
	if err != nil {
		t.Fatal(err)
	}
	if v, ok := lev.click.formValue("note"); v != "x" || !ok {
		t.Errorf("legacy formValue = %q %v, want x true", v, ok)
	}
	if v, ok := gchatNoteEvent(t, nil, false).click.formValue("note"); v != "" || ok {
		t.Errorf("absent formValue = %q %v, want \"\" false", v, ok)
	}
	if v, ok := gchatNoteEvent(t, map[string]any{}, false).click.formValue("note"); v != "" || !ok {
		t.Errorf("empty formValue = %q %v, want \"\" true", v, ok)
	}
}

func TestGchatDownRatingPostsTheNoteCardInTheThread(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	callsAtRate := -1
	rec.onRate = func() { callsAtRate = relayCallCount(f) }
	a.classify(gchatRatingEvent(t, "down"))
	if callsAtRate != 0 {
		t.Errorf("relay calls at Rate = %d, want 0", callsAtRate)
	}
	if n := relayCallCount(f); n != 2 {
		t.Fatalf("relay calls = %d, want the answer patch and the card", n)
	}
	checkPatch(t, f, 0, gchatRatedMessage)
	c := f.call(1)
	body := callBody(t, c)
	if c.method != "create" || c.arguments["parent"] != "spaces/_c2fixtureAA" {
		t.Fatalf("call 1 = %+v, want a create in the DM", c)
	}
	if th, _ := body["thread"].(map[string]any); th["name"] != "spaces/_c2fixtureAA/threads/BOT1" {
		t.Errorf("thread = %+v", body["thread"])
	}
	if _, has := body["privateMessageViewer"]; has {
		t.Errorf("a DM card is private: %+v", body["privateMessageViewer"])
	}
	card, widgets := cardOf(t, body)
	if card["cardId"] != "kage.feedback-note" || len(widgets) != 4 {
		t.Fatalf("card = %+v, want the note card with four widgets", card)
	}
	if text := widgets[0]["textParagraph"].(map[string]any)["text"]; text != "What went wrong?" {
		t.Errorf("title = %q", text)
	}
	input := widgets[1]["textInput"].(map[string]any)
	if input["name"] != "note" || input["label"] != "Anything else?" || input["type"] != "SINGLE_LINE" ||
		input["hintText"] != "Up to 500 characters." || input["validation"].(map[string]any)["characterLimit"] != float64(500) {
		t.Errorf("textInput = %+v", input)
	}
	if text := widgets[2]["textParagraph"].(map[string]any)["text"]; text != "Your question and this answer are kept on your install for 30 days so an operator can turn them into a test." {
		t.Errorf("kept line = %q", text)
	}
	raw, _ := json.Marshal(widgets[3]["buttonList"].(map[string]any)["buttons"])
	var buttons []gchatButton
	if err := json.Unmarshal(raw, &buttons); err != nil {
		t.Fatal(err)
	}
	if len(buttons) != 2 {
		t.Fatalf("buttons = %s", raw)
	}
	send, cancel := buttons[0], buttons[1]
	if p := buttonParams(send); send.Text != "Send" || send.OnClick.Action.Function != "kage.feedback-note" ||
		p["task"] != "t-root" || p["rating"] != "down" || len(p) != 2 {
		t.Errorf("Send = %+v %v", send, p)
	}
	if p := buttonParams(cancel); cancel.Text != "Cancel" || cancel.OnClick.Action.Function != "kage.feedback-note" ||
		p["task"] != "t-root" || p["rating"] != "down" || p["cancel"] != "1" || len(p) != 3 {
		t.Errorf("Cancel = %+v %v", cancel, p)
	}
}

func TestGchatDownRatingWithNoAskOnRecordSaysOnlyTheAnswerIsKept(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	rec.noAsk = true
	a.classify(gchatRatingEvent(t, "down"))
	_, widgets := cardOf(t, callBody(t, f.call(1)))
	text, _ := widgets[2]["textParagraph"].(map[string]any)["text"].(string)
	if text != "This answer is kept on your install for 30 days so an operator can turn it into a test." || strings.Contains(text, "Your question") {
		t.Errorf("kept line = %q", text)
	}
}

func TestGchatNoteCardInASpaceIsPrivateToTheRater(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	a.classify(gchatSpaceEvent(gchatRatingEvent(t, "down")))
	c := f.call(1)
	body := callBody(t, c)
	if c.method != "create" || c.arguments["parent"] != "spaces/AAA" {
		t.Fatalf("call 1 = %+v", c)
	}
	if v, _ := body["privateMessageViewer"].(map[string]any); v["name"] != "users/100000000000000000042" {
		t.Errorf("privateMessageViewer = %+v", body["privateMessageViewer"])
	}
	if th, _ := body["thread"].(map[string]any); th["name"] != "spaces/AAA/threads/T1" {
		t.Errorf("thread = %+v", body["thread"])
	}
	if card, _ := cardOf(t, body); card["cardId"] != "kage.feedback-note" {
		t.Errorf("card = %+v", card)
	}
}

func TestGchatRefusedPrivateThreadReplyRetriesAtTheTopLevel(t *testing.T) {
	const create = "spaces.messages/create"
	refused := fakeChatAPIError{status: 400, reason: "private message in thread"}

	a, f, _ := newFeedbackGchatAdapter(t)
	f.queue[create] = []any{refused, map[string]any{"name": "spaces/AAA/messages/CARD1"}}
	a.classify(gchatSpaceEvent(gchatRatingEvent(t, "down")))
	if n := relayCallCount(f); n != 3 {
		t.Fatalf("relay calls = %d, want the patch and two creates", n)
	}
	c := f.call(2)
	body := callBody(t, c)
	if c.method != "create" {
		t.Fatalf("call 2 = %+v", c)
	}
	if _, has := body["thread"]; has {
		t.Errorf("retry carries a thread: %+v", body["thread"])
	}
	if v, _ := body["privateMessageViewer"].(map[string]any); v["name"] != "users/100000000000000000042" {
		t.Errorf("retry privateMessageViewer = %+v", body["privateMessageViewer"])
	}

	a, f, rec := newFeedbackGchatAdapter(t)
	f.queue[create] = []any{refused, refused}
	a.classify(gchatSpaceEvent(gchatRatingEvent(t, "down")))
	if n := relayCallCount(f); n != 3 {
		t.Errorf("relay calls = %d, want no third create", n)
	}
	if got := recordedRatings(rec); len(got) != 1 || got[0].Rating != "down" {
		t.Errorf("ratings = %+v, want the 👎 kept", got)
	}

	// Only a refusal retries: an outage posts no second card.
	a, f, rec = newFeedbackGchatAdapter(t)
	f.queue[create] = []any{fakeChatAPIError{status: 502, reason: "backend error"}}
	a.classify(gchatSpaceEvent(gchatRatingEvent(t, "down")))
	if n := relayCallCount(f); n != 2 {
		t.Errorf("relay calls = %d, want the patch and one create, no retry after a 502", n)
	}
	if got := recordedRatings(rec); len(got) != 1 || got[0].Rating != "down" {
		t.Errorf("ratings = %+v, want the 👎 kept", got)
	}
}

func TestGchatUpRatingPostsNoCard(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	a.classify(gchatRatingEvent(t, "up"))
	for i := 0; i < relayCallCount(f); i++ {
		if f.call(i).method == "create" {
			t.Errorf("call %d is a create: %+v", i, f.call(i))
		}
	}
}

func TestGchatFailedDownWritePostsNoCard(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	rec.rateErr = errFeedbackContended
	a.classify(gchatRatingEvent(t, "down"))
	for i := 0; i < relayCallCount(f); i++ {
		if f.call(i).method == "create" {
			t.Errorf("call %d is a create: %+v", i, f.call(i))
		}
	}
}

func TestGchatNoteJoinsTheRatingAndCollapsesTheCard(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	msg, reason := a.classify(gchatNoteEvent(t, noteInputs(" the limit is 256Mi "), false))
	if msg != (InboundMessage{}) || reason == "" {
		t.Fatalf("classify = %+v %q, want no turn", msg, reason)
	}
	want := "sender@example.com t-root  gchat:dm/spaces/_c2fixtureAA/threads/BOT1 the limit is 256Mi"
	if got := recordedNotes(rec); len(got) != 1 || got[0] != want {
		t.Errorf("notes = %q\nwant [%q]", got, want)
	}
	text := checkNotePatch(t, f, 0, gchatNoteCardMessage)
	if text != gchatNoteThanks() || !strings.Contains(text, `<a href="https://gke-labs.github.io/kube-agents/feedback">`) ||
		!strings.Contains(text, "public issue") {
		t.Errorf("collapsed line = %q", text)
	}
}

func TestGchatEmptyNoteIsStillKeptAndThanked(t *testing.T) {
	for name, inputs := range map[string]map[string]any{"empty value": noteInputs(""), "empty formInputs": {}} {
		t.Run(name, func(t *testing.T) {
			a, f, rec := newFeedbackGchatAdapter(t)
			a.classify(gchatNoteEvent(t, inputs, false))
			got := recordedNotes(rec)
			if len(got) != 1 || got[0] != "sender@example.com t-root  gchat:dm/spaces/_c2fixtureAA/threads/BOT1 " {
				t.Errorf("notes = %q, want one empty note", got)
			}
			if text := checkNotePatch(t, f, 0, gchatNoteCardMessage); text != gchatNoteThanks() {
				t.Errorf("collapsed line = %q", text)
			}
		})
	}
}

func TestGchatSendWithoutFormInputsSaysTheNoteDidNotArrive(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	a.classify(gchatNoteEvent(t, nil, false))
	if got := recordedNotes(rec); len(got) != 0 {
		t.Errorf("notes = %q, want none", got)
	}
	if text := checkNotePatch(t, f, 0, gchatNoteCardMessage); text != "The note did not reach me; your rating stands as it was." {
		t.Errorf("collapsed line = %q, want the note-lost line", text)
	}
}

func TestGchatCancelCollapsesTheCardWithoutANote(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	a.classify(gchatNoteEvent(t, noteInputs("typed then cancelled"), true))
	if got := recordedNotes(rec); len(got) != 0 {
		t.Errorf("notes = %q, want none", got)
	}
	if text := checkNotePatch(t, f, 0, gchatNoteCardMessage); text != gchatNoteThanks() {
		t.Errorf("collapsed line = %q", text)
	}
}

func TestGchatNoteThatWasNotKeptSaysTheRatingChanged(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	rec.noteErr = errFeedbackNotKept
	a.classify(gchatNoteEvent(t, noteInputs("x"), false))
	if text := checkNotePatch(t, f, 0, gchatNoteCardMessage); text != gchatNoteReplaced {
		t.Errorf("collapsed line = %q, want %q", text, gchatNoteReplaced)
	}

	a, f, rec = newFeedbackGchatAdapter(t)
	rec.noteErr = errFeedbackContended
	a.classify(gchatNoteEvent(t, noteInputs("x"), false))
	if got := recordedNotes(rec); len(got) != 1 {
		t.Errorf("notes = %q, want the one attempt", got)
	}
	if n := relayCallCount(f); n != 0 {
		t.Errorf("relay calls = %d, want none: the card stays for another Send", n)
	}
}

func TestGchatRefusedCardPatchPostsTheLineAsANewPrivateMessage(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	f.queue["spaces.messages/patch"] = []any{fakeChatAPIError{status: 400, reason: "cannot update"}}
	a.classify(gchatSpaceEvent(gchatNoteEvent(t, noteInputs("x"), false)))
	if n := relayCallCount(f); n != 2 {
		t.Fatalf("relay calls = %d, want the patch and a create", n)
	}
	checkNotePatch(t, f, 0, "spaces/AAA/messages/M1")
	c := f.call(1)
	body := callBody(t, c)
	if c.method != "create" || c.arguments["parent"] != "spaces/AAA" {
		t.Fatalf("call 1 = %+v", c)
	}
	if v, _ := body["privateMessageViewer"].(map[string]any); v["name"] != "users/100000000000000000042" {
		t.Errorf("privateMessageViewer = %+v", body["privateMessageViewer"])
	}
	if th, _ := body["thread"].(map[string]any); th["name"] != "spaces/AAA/threads/T1" {
		t.Errorf("thread = %+v", body["thread"])
	}
	if text := collapsedText(t, body); text != gchatNoteThanks() {
		t.Errorf("line = %q", text)
	}

	// Only a refusal falls back: a failed patch posts no second card.
	a, f, _ = newFeedbackGchatAdapter(t)
	f.queue["spaces.messages/patch"] = []any{fakeChatAPIError{status: 502, reason: "backend error"}}
	a.classify(gchatSpaceEvent(gchatNoteEvent(t, noteInputs("x"), false)))
	if n := relayCallCount(f); n != 1 {
		t.Errorf("relay calls = %d, want the patch alone after a 502", n)
	}
}

// In a space the note card is private to the rater's users/ name; with no
// name it would be public, so neither the card nor the collapse fallback is
// posted.
func TestGchatNoteCardIsNotPostedInASpaceWithoutAUserName(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	ev := gchatSpaceEvent(gchatRatingEvent(t, "down"))
	ev.click.User.Name = ""
	a.classify(ev)
	if got := recordedRatings(rec); len(got) != 1 || got[0].Rating != "down" {
		t.Errorf("ratings = %+v, want the 👎 kept", got)
	}
	if n := relayCallCount(f); n != 1 {
		t.Fatalf("relay calls = %d, want the answer patch alone", n)
	}
	checkFilled(t, bodyButtons(t, checkPatch(t, f, 0, "spaces/AAA/messages/M1")), "down")

	a, f, _ = newFeedbackGchatAdapter(t)
	f.queue["spaces.messages/patch"] = []any{fakeChatAPIError{status: 400, reason: "cannot update"}}
	ev = gchatSpaceEvent(gchatNoteEvent(t, noteInputs("x"), false))
	ev.click.User.Name = ""
	a.classify(ev)
	if n := relayCallCount(f); n != 1 {
		t.Errorf("relay calls = %d, want the refused patch alone", n)
	}
	checkNotePatch(t, f, 0, "spaces/AAA/messages/M1")
}

// A tap's patch and the answered rewrite's patch of one message are ordered:
// a tap whose layout still had the choices, held back at the relay, lands
// before the rewrite, never over it.
func TestGchatRatingPatchNeverLandsOverTheAnsweredRewrite(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	name, err := a.PostMessage("gchat:dm/spaces/_c2fixtureAA", OutMessage{Text: "fallback", Chat: gchatFeedbackQuestion(), Feedback: "t-root"})
	if err != nil {
		t.Fatal(err)
	}
	a.syncRewrites = false
	tapPatching := make(chan struct{})
	// Not a sync.Once: its Do would hold the rewrite's patch back too.
	var patches atomic.Int32
	f.mu.Lock()
	f.beforeCall = func(key string) {
		if key == "spaces.messages/patch" && patches.Add(1) == 1 {
			close(tapPatching)
			time.Sleep(100 * time.Millisecond)
		}
	}
	f.mu.Unlock()

	if _, reason := a.classify(gchatRatingEvent(t, "up")); reason == "" {
		t.Fatal("a rating tap is a turn")
	}
	select {
	case <-tapPatching:
	case <-time.After(5 * time.Second):
		t.Fatal("the tap never patched")
	}
	msg, reason := a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatalf("choice click: %q", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)

	deadline := time.Now().Add(5 * time.Second)
	for {
		a.mu.Lock()
		_, shown := a.uxLocked().shown[name]
		a.mu.Unlock()
		if relayCallCount(f) >= 3 && shown {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("relay calls = %d, shown = %v; want the create, two patches and the rewrite on record", relayCallCount(f), shown)
		}
		time.Sleep(time.Millisecond)
	}
	if n := relayCallCount(f); n != 3 {
		t.Fatalf("relay calls = %d, want the create and two patches", n)
	}
	body := checkPatch(t, f, 2, name)
	if text, _ := body["text"].(string); !strings.HasSuffix(text, "✓ Test Sender: Look at the first one") {
		t.Errorf("final text = %q, want the answered line", text)
	}
	buttons := bodyButtons(t, body)
	for _, b := range buttons {
		if classifyGchatButton(b) == gchatButtonChoice {
			t.Errorf("the final patch shows a choice: %+v", b)
		}
	}
	checkFilled(t, buttons, "up")
}

func TestGchatRedeliveredSendIsHandledOnce(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	ev := gchatNoteEvent(t, noteInputs("x"), false)
	a.classify(ev)
	if _, reason := a.classify(ev); !strings.Contains(reason, "duplicate") {
		t.Errorf("second reason = %q, want a duplicate", reason)
	}
	if got := recordedNotes(rec); len(got) != 1 {
		t.Errorf("notes = %q, want one", got)
	}
	if n := relayCallCount(f); n != 1 {
		t.Errorf("relay calls = %d, want one patch", n)
	}
}

func TestGchatNoteFromAnUnverifiedSenderChangesNothing(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	rec.admit = map[string]bool{}
	if _, reason := a.classify(gchatNoteEvent(t, noteInputs("x"), false)); reason == "" {
		t.Error("an unverified note is a turn")
	}
	if got := recordedNotes(rec); len(got) != 0 {
		t.Errorf("notes = %q", got)
	}
	if n := relayCallCount(f); n != 0 {
		t.Errorf("relay calls = %d", n)
	}
}

func TestGchatNoteIsCappedAtTheBoxsLimit(t *testing.T) {
	a, _, rec := newFeedbackGchatAdapter(t)
	a.classify(gchatNoteEvent(t, noteInputs(strings.Repeat("é", 600)), false))
	got := recordedNotes(rec)
	if len(got) != 1 {
		t.Fatalf("notes = %q", got)
	}
	note := strings.TrimPrefix(got[0], "sender@example.com t-root  gchat:dm/spaces/_c2fixtureAA/threads/BOT1 ")
	if n := utf8.RuneCountInString(note); n != 500 || strings.Contains(note, "…") {
		t.Errorf("note has %d runes (ellipsis %v), want 500 and none", n, strings.Contains(note, "…"))
	}
}

func TestGchatDownWhoseAnswerPatchFailsStillAsksForTheNote(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	f.queue["spaces.messages/patch"] = []any{fakeChatAPIError{status: 404, reason: "not found"}}
	a.classify(gchatRatingEvent(t, "down"))
	if got := recordedRatings(rec); len(got) != 1 || got[0].Rating != "down" {
		t.Errorf("ratings = %+v", got)
	}
	if n := relayCallCount(f); n != 2 || f.call(1).method != "create" {
		t.Fatalf("relay calls = %d, want the failed patch and the card create", n)
	}
	if card, _ := cardOf(t, callBody(t, f.call(1))); card["cardId"] != "kage.feedback-note" {
		t.Errorf("card = %+v", card)
	}
}

// gchatRatedClickEvent is the add-on choice click fixture with r's rating
// buttons at the end of the message's button list.
func gchatRatedClickEvent(t *testing.T, r gchatRating) *gchatEvent {
	t.Helper()
	ev := gchatClickEvent(t)
	raw, _ := json.Marshal(gchatRatingButtons(r))
	var buttons []gchatButton
	if err := json.Unmarshal(raw, &buttons); err != nil {
		t.Fatal(err)
	}
	list := &ev.Message.AccessoryWidgets[0].ButtonList
	list.Buttons = append(list.Buttons, buttons...)
	return ev
}

// checkLinkAndRating checks buttons are the board link, then the two rating
// buttons with chosen filled ("" for neither).
func checkLinkAndRating(t *testing.T, buttons []gchatButton, chosen string) {
	t.Helper()
	if len(buttons) != 3 {
		t.Fatalf("buttons = %+v, want the link and two rating", buttons)
	}
	if classifyGchatButton(buttons[0]) != gchatButtonLink || buttons[0].Text != "board" || buttons[0].OnClick.OpenLink.URL != "https://x.example/b" {
		t.Errorf("link = %+v", buttons[0])
	}
	if chosen == "" {
		checkRatingButton(t, buttons[1], "👍", "Good answer", "up")
		checkRatingButton(t, buttons[2], "👎", "Bad answer", "down")
		return
	}
	checkFilled(t, buttons, chosen)
}

func TestGchatAnsweredRewriteKeepsTheRatingButtons(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	name, err := a.PostMessage("gchat:dm/spaces/_c2fixtureAA", OutMessage{Text: "fallback", Chat: gchatFeedbackQuestion(), Feedback: "t-root"})
	if err != nil {
		t.Fatal(err)
	}
	a.classify(gchatRatingEvent(t, "up"))
	msg, reason := a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatalf("choice click: %q", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	if n := relayCallCount(f); n != 3 {
		t.Fatalf("relay calls = %d, want the post, the rating patch and the rewrite", n)
	}
	body := checkPatch(t, f, 2, name)
	if text, _ := body["text"].(string); !strings.HasSuffix(text, "✓ Test Sender: Look at the first one") {
		t.Errorf("text = %q, want the answered line last", text)
	}
	checkLinkAndRating(t, bodyButtons(t, body), "up")
}

func TestGchatTypedAnswerRewriteKeepsTheRatingUnfilled(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	conversation := "gchat:dm/spaces/_c2fixtureAA/threads/jhqZwM-35cA"
	name, err := a.PostMessage(conversation, OutMessage{Text: "fallback", Chat: gchatFeedbackQuestion(), Feedback: "t-root"})
	if err != nil {
		t.Fatal(err)
	}
	msg, reason := a.classify(gchatFixtureEvent(t, "addon-dm-thread-reply.json", nil))
	if reason != "" || msg.Conversation != conversation {
		t.Fatalf("typed reply = %+v (%q), want a turn in %s", msg, reason, conversation)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	if n := relayCallCount(f); n != 2 {
		t.Fatalf("relay calls = %d, want the post and the rewrite", n)
	}
	body := checkPatch(t, f, 1, name)
	if text, _ := body["text"].(string); !strings.Contains(text, "✓ Test Sender: ") {
		t.Errorf("text = %q, want the answered line", text)
	}
	checkLinkAndRating(t, bodyButtons(t, body), "")
}

func TestGchatClickAfterARestartKeepsTheRatingButtonsAsShown(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	ev := gchatRatedClickEvent(t, gchatRating{task: "t-root", chosen: "down"})
	msg, reason := a.classify(ev)
	if reason != "" {
		t.Fatalf("choice click: %q", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	if n := relayCallCount(f); n != 1 {
		t.Fatalf("relay calls = %d, want the rewrite", n)
	}
	body := checkPatch(t, f, 0, ev.Message.Name)
	if text, _ := body["text"].(string); !strings.HasSuffix(text, "✓ Test Sender: Look at the first one") {
		t.Errorf("text = %q, want the answered line last", text)
	}
	checkLinkAndRating(t, bodyButtons(t, body), "down")
}

func TestGchatMessageButtonsTellTheKindsApartByFunction(t *testing.T) {
	ev := gchatClickEvent(t)
	original := ev.Message.AccessoryWidgets[0].ButtonList.Buttons
	if len(original) != 3 || original[0].Type != "FILLED" || classifyGchatButton(original[2]) != gchatButtonLink {
		t.Fatalf("fixture buttons = %+v, want a FILLED primary choice, a choice and a link", original)
	}
	withRating := func(r gchatRating) gchatMessage {
		raw, _ := json.Marshal(gchatRatingButtons(r))
		var rating []gchatButton
		if err := json.Unmarshal(raw, &rating); err != nil {
			t.Fatal(err)
		}
		m := ev.Message
		// The rating buttons sit before the link: kinds come from the
		// function, not the position.
		buttons := append(append(append([]gchatButton{}, original[:2]...), rating...), original[2])
		m.AccessoryWidgets = []gchatAccessoryWidget{{}}
		m.AccessoryWidgets[0].ButtonList.Buttons = buttons
		return m
	}
	links, label, rating := gchatMessageButtons(withRating(gchatRating{task: "t-root"}), 1, "fallback")
	if len(links) != 1 || links[0].Text != "board" || links[0].URL != "https://x.example/b" {
		t.Errorf("links = %+v, want the link alone", links)
	}
	if label != "Skip" {
		t.Errorf("label = %q, want the choice at index 1", label)
	}
	if rating != (gchatRating{task: "t-root"}) {
		t.Errorf("rating = %+v, want t-root unchosen", rating)
	}
	if _, _, rating := gchatMessageButtons(withRating(gchatRating{task: "t-root", chosen: "down"}), 1, "fallback"); rating != (gchatRating{task: "t-root", chosen: "down"}) {
		t.Errorf("rating with 👎 FILLED = %+v, want chosen down", rating)
	}
}

func TestGchatLaterEditOfAnAnsweredRatedMessageKeepsItsButtons(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	conversation := "gchat:dm/spaces/_c2fixtureAA"
	name, err := a.PostMessage(conversation, OutMessage{Text: "fallback", Chat: gchatFeedbackQuestion(), Feedback: "t-root"})
	if err != nil {
		t.Fatal(err)
	}
	msg, reason := a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatalf("choice click: %q", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	if err := a.EditMessage(conversation, name, OutMessage{Text: "settled"}); err != nil {
		t.Fatal(err)
	}
	body := checkPatch(t, f, 2, name)
	if text, _ := body["text"].(string); !strings.HasPrefix(text, "settled") || !strings.HasSuffix(text, "✓ Test Sender: Look at the first one") {
		t.Errorf("edit text = %q, want the new text and the answered line", text)
	}
	buttons := bodyButtons(t, body)
	if len(buttons) != 2 {
		t.Fatalf("buttons = %+v, want the two rating buttons", buttons)
	}
	checkRatingButton(t, buttons[0], "👍", "Good answer", "up")
	checkRatingButton(t, buttons[1], "👎", "Bad answer", "down")
}

// The layout the answered rewrite (and a later edit) records carries no
// rating buttons, so a tap afterwards draws one pair, not two.
func TestGchatRatingAfterARatedAnsweredRewriteDrawsOnePair(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	conversation := "gchat:dm/spaces/_c2fixtureAA"
	name, err := a.PostMessage(conversation, OutMessage{Text: "fallback", Chat: gchatFeedbackQuestion(), Feedback: "t-root"})
	if err != nil {
		t.Fatal(err)
	}
	a.classify(gchatRatingEvent(t, "up"))
	msg, reason := a.classify(gchatClickEvent(t))
	if reason != "" {
		t.Fatalf("choice click: %q", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	a.classify(gchatRatingEvent(t, "down"))
	if n := relayCallCount(f); n < 4 {
		t.Fatalf("relay calls = %d, want the 👎 patch fourth", n)
	}
	body := checkPatch(t, f, 3, name)
	if text, _ := body["text"].(string); !strings.HasSuffix(text, "✓ Test Sender: Look at the first one") {
		t.Errorf("text = %q, want the answered line last", text)
	}
	checkLinkAndRating(t, bodyButtons(t, body), "down")

	if err := a.EditMessage(conversation, name, OutMessage{Text: "settled"}); err != nil {
		t.Fatal(err)
	}
	a.classify(gchatRatingEvent(t, "up"))
	n := relayCallCount(f)
	edited := bodyButtons(t, checkPatch(t, f, n-2, name))
	if len(edited) != 2 {
		t.Fatalf("buttons on the edit = %+v, want one rating pair", edited)
	}
	checkFilled(t, edited, "down")
	buttons := bodyButtons(t, checkPatch(t, f, n-1, name))
	if len(buttons) != 2 {
		t.Fatalf("buttons after the edit and a tap = %+v, want one rating pair", buttons)
	}
	checkFilled(t, buttons, "up")
}

// A rewrite after a restart puts the rating it drew from the event's copy on
// record, so a later edit keeps the buttons and the filled one.
func TestGchatEditAfterARestartRewriteKeepsTheRatingAsShown(t *testing.T) {
	a, f, _ := newFeedbackGchatAdapter(t)
	ev := gchatRatedClickEvent(t, gchatRating{task: "t-root", chosen: "down"})
	msg, reason := a.classify(ev)
	if reason != "" {
		t.Fatalf("choice click: %q", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	if err := a.EditMessage(msg.Conversation, ev.Message.Name, OutMessage{Text: "settled"}); err != nil {
		t.Fatal(err)
	}
	if n := relayCallCount(f); n != 2 {
		t.Fatalf("relay calls = %d, want the rewrite and the edit", n)
	}
	body := checkPatch(t, f, 1, ev.Message.Name)
	if text, _ := body["text"].(string); !strings.HasPrefix(text, "settled") || !strings.HasSuffix(text, "✓ Test Sender: Look at the first one") {
		t.Errorf("edit text = %q, want the new text and the answered line", text)
	}
	// The edit names no links, so the rating pair is all it carries.
	buttons := bodyButtons(t, body)
	if len(buttons) != 2 {
		t.Fatalf("buttons = %+v, want the two rating buttons", buttons)
	}
	checkFilled(t, buttons, "down")
}
