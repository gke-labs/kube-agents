package gateway

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"strings"
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
	a.classify(gchatRatingEvent(t, "up"))
	buttons := bodyButtons(t, checkPatch(t, f, 2, name))
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

func TestGchatReRatingReplacesTheFilledButton(t *testing.T) {
	a, f, rec := newFeedbackGchatAdapter(t)
	a.classify(gchatRatingEvent(t, "down"))
	checkFilled(t, bodyButtons(t, checkPatch(t, f, 0, gchatRatedMessage)), "down")
	a.classify(gchatRatingEvent(t, "up"))
	checkFilled(t, bodyButtons(t, checkPatch(t, f, 1, gchatRatedMessage)), "up")
	if r := a.ratingOf(gchatRatedMessage); r.task != "t-root" || r.chosen != FeedbackUp {
		t.Errorf("ratingOf = %+v, want t-root up", r)
	}
	if got := recordedRatings(rec); len(got) != 2 {
		t.Errorf("ratings = %+v, want two", got)
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
	checkFilled(t, bodyButtons(t, checkPatch(t, f, relayCallCount(f)-1, "spaces/AAA/messages/M1")), "down")
}
