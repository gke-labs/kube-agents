package gateway

import (
	"encoding/json"
	"errors"
	"testing"

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
