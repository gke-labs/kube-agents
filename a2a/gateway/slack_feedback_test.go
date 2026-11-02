package gateway

import (
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/slack-go/slack"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// fakeRecorder is the gateway's FeedbackRecorder as the Slack adapter sees
// it: admits the listed raters and records every call.
type fakeRecorder struct {
	mu      sync.Mutex
	admit   map[string]bool
	ratings []FeedbackRating
	notes   []string
	rateErr error
	noteErr error
	// noAsk makes Rate report that the kept record holds no ask.
	noAsk bool
	// up, down and tallyErr are what Tally answers; tallies counts its calls.
	up, down int
	tallyErr error
	tallies  int
	// onRate runs inside Rate, for a test that checks what had happened
	// by then.
	onRate func()
}

func (f *fakeRecorder) Admits(_, rater string) bool { return f.admit[rater] }
func (f *fakeRecorder) KeptDays() int               { return 30 }

func (f *fakeRecorder) Rate(r FeedbackRating) (bool, error) {
	if f.onRate != nil {
		f.onRate()
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ratings = append(f.ratings, r)
	return !f.noAsk, f.rateErr
}

func (f *fakeRecorder) Tally(context.Context, time.Time) (int, int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.tallies++
	return f.up, f.down, f.tallyErr
}

func (f *fakeRecorder) Note(r FeedbackRating, note string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.notes = append(f.notes, r.Rater+" "+r.TaskID+" "+r.MessageID+" "+r.Conversation+" "+note)
	return f.noteErr
}

// fakeFeedbackAPI records the modal and the ephemeral line.
type fakeFeedbackAPI struct {
	mu         sync.Mutex
	views      []slack.ModalViewRequest
	triggers   []string
	ephemerals []string
}

func (f *fakeFeedbackAPI) OpenViewContext(_ context.Context, triggerID string, view slack.ModalViewRequest) (*slack.ViewResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.triggers = append(f.triggers, triggerID)
	f.views = append(f.views, view)
	return &slack.ViewResponse{}, nil
}

func (f *fakeFeedbackAPI) PostEphemeralContext(_ context.Context, channelID, userID string, options ...slack.MsgOption) (string, error) {
	_, values, err := slack.UnsafeApplyMsgOptions("tok", channelID, "https://slack.example/api/", options...)
	if err != nil {
		return "", err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ephemerals = append(f.ephemerals, channelID+" "+userID+" "+values.Get("thread_ts")+" "+values.Get("text"))
	return "1.0", nil
}

func newFeedbackSlackAdapter() (*SlackAdapter, *fakeRecorder, *fakeFeedbackAPI) {
	a, _ := newUXSlackAdapter(&fakeSlackAPI{})
	rec := &fakeRecorder{admit: map[string]bool{"U1": true}}
	api := &fakeFeedbackAPI{}
	a.feedbackAPI = api
	a.SetFeedbackRecorder(rec)
	return a, rec, api
}

func ratingPayload(t *testing.T, user, value string) json.RawMessage {
	t.Helper()
	p := map[string]any{
		"type":       "block_actions",
		"user":       map[string]any{"id": user, "team_id": "T1"},
		"team":       map[string]any{"id": "T1"},
		"channel":    map[string]any{"id": "C1"},
		"trigger_id": "trig-1",
		"message":    map[string]any{"ts": "50.0", "thread_ts": "40.0", "text": "*It is OOMKilled.*"},
		"actions": []any{map[string]any{"action_id": slackFeedbackAction, "block_id": slackFeedbackBlockPrefix + "t-root",
			"type": slackFeedbackButtons, "value": value}},
		"action_ts": "60." + value,
	}
	raw, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// documentedRatingPayload is a 👍/👎 tap in the shape Slack documents for
// block_actions (docs.slack.dev, "block_actions payload"): action_ts inside
// the action, none at the top.
func documentedRatingPayload(t *testing.T, actionID, value string) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(map[string]any{
		"type":       "block_actions",
		"user":       map[string]any{"id": "U1", "team_id": "T1"},
		"team":       map[string]any{"id": "T1"},
		"channel":    map[string]any{"id": "C1"},
		"container":  map[string]any{"type": "message", "message_ts": "50.0", "channel_id": "C1"},
		"trigger_id": "trig-1",
		"message":    map[string]any{"ts": "50.0", "thread_ts": "40.0", "text": "*It is OOMKilled.*"},
		"actions": []any{map[string]any{"type": slackFeedbackButtons, "action_id": actionID,
			"block_id": slackFeedbackBlockPrefix + "t-root", "value": value, "action_ts": "61.0"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestSlackRatingInTheDocumentedPayloadShapeIsRecorded(t *testing.T) {
	a, rec, api := newFeedbackSlackAdapter()
	a.interactive(documentedRatingPayload(t, slackFeedbackAction, FeedbackDown))
	if len(rec.ratings) != 1 || rec.ratings[0].Rating != FeedbackDown || rec.ratings[0].TaskID != "t-root" {
		t.Fatalf("ratings = %+v, want the 👎 recorded from a payload whose action_ts is inside the action", rec.ratings)
	}
	if len(api.views) != 1 {
		t.Errorf("views = %d, want the note box opened", len(api.views))
	}
}

func TestSlackRatingTapsTheGatewayCannotReadAreLogged(t *testing.T) {
	a, rec, _ := newFeedbackSlackAdapter()
	logs := &recordingHandler{}
	a.log = slog.New(logs)
	a.interactive(documentedRatingPayload(t, slackFeedbackAction, "positive"))
	a.interactive(documentedRatingPayload(t, "another.app", FeedbackUp))
	if len(rec.ratings) != 0 {
		t.Fatalf("ratings = %+v, want none", rec.ratings)
	}
	if !logs.has("value", "positive") || !logs.has("actionId", "another.app") {
		t.Error("a rating tap the gateway drops must be logged with its ids and value")
	}
}

// sentBlocks is the blocks and attachments options would send.
func sentBlocks(t *testing.T, opts []slack.MsgOption) (string, string) {
	t.Helper()
	form := sentUpdateForm("C1", "1.0", opts...)
	return form.Get("blocks"), form.Get("attachments")
}

func TestSlackRatedAnswerEndsWithFeedbackButtons(t *testing.T) {
	m := OutMessage{Text: "*It is OOMKilled.*", Chat: &chatmsg.Message{Schema: chatmsg.Schema, Headline: "It is OOMKilled.",
		Choices: []chatmsg.Choice{{Text: "raise the memory limit", Primary: true}}}, Feedback: "t-root"}
	opts, err := slackRatedOptions(m)
	if err != nil {
		t.Fatal(err)
	}
	blocks, _ := sentBlocks(t, opts)
	var got []map[string]any
	if err := json.Unmarshal([]byte(blocks), &got); err != nil {
		t.Fatalf("blocks %q: %v", blocks, err)
	}
	last := got[len(got)-1]
	if last["type"] != slackContextActionsBlock || last["block_id"] != slackFeedbackBlockPrefix+"t-root" {
		t.Fatalf("last block = %v, want the feedback buttons under the answer's own buttons", last)
	}
	if !strings.Contains(blocks, `"positive_button":{"accessibility_label":"Good answer"`) || !strings.Contains(blocks, `"value":"down"`) {
		t.Errorf("feedback buttons = %s", blocks)
	}
	if got[len(got)-2]["type"] != slackBlockActionsT {
		t.Errorf("the answer's choices must stay above the feedback buttons: %s", blocks)
	}
}

func TestSlackRatedAnswerWithASideBarCarriesTheButtonsInIt(t *testing.T) {
	m := OutMessage{Text: "*Done.*", Chat: &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Done.", Tone: chatmsg.ToneSuccess,
		Rows: []chatmsg.Row{{Text: "seeded-a"}}}, Feedback: "t-root"}
	opts, err := slackRatedOptions(m)
	if err != nil {
		t.Fatal(err)
	}
	_, attachments := sentBlocks(t, opts)
	if !strings.Contains(attachments, slackContextActionsBlock) {
		t.Errorf("attachments = %s, want the feedback buttons inside the side bar, where they draw last", attachments)
	}
}

func TestSlackRatedTextAnswerIsASectionAndTheButtons(t *testing.T) {
	opts, err := slackRatedOptions(OutMessage{Text: "plain **answer**", Feedback: "t-root"})
	if err != nil {
		t.Fatal(err)
	}
	blocks, _ := sentBlocks(t, opts)
	if !strings.Contains(blocks, `"type":"section"`) || !strings.Contains(blocks, slackContextActionsBlock) {
		t.Errorf("blocks = %s", blocks)
	}
	// Escaping can take a chunk past one section's cap; it posts as more
	// sections in the same message, the controls still last.
	long, err := slackRatedOptions(OutMessage{Text: strings.Repeat("<a> & ", 400), Feedback: "t"})
	if err != nil {
		t.Fatalf("an escaped chunk past one section must still carry the controls: %v", err)
	}
	blocks, _ = sentBlocks(t, long)
	var got []map[string]any
	if err := json.Unmarshal([]byte(blocks), &got); err != nil {
		t.Fatal(err)
	}
	if len(got) < 3 || got[len(got)-1]["type"] != slackContextActionsBlock {
		t.Errorf("blocks = %d, want two or more sections then the controls", len(got))
	}
}

func TestSlackRedeliveredNoteThanksOnce(t *testing.T) {
	a, rec, api := newFeedbackSlackAdapter()
	var p map[string]any
	if err := json.Unmarshal(notePayload(t, "stale"), &p); err != nil {
		t.Fatal(err)
	}
	view := p["view"].(map[string]any)
	view["id"], view["hash"] = "V1", "h1"
	raw, _ := json.Marshal(p)
	a.interactive(raw)
	a.interactive(raw)
	if len(rec.notes) != 1 || len(api.ephemerals) != 1 {
		t.Errorf("notes = %d ephemerals = %d; a redelivered Send is handled once", len(rec.notes), len(api.ephemerals))
	}
}

func TestSlackDrawsNoButtonsWithoutTheRecorder(t *testing.T) {
	a, _ := newUXSlackAdapter(&fakeSlackAPI{})
	a.feedbackAPI = &fakeFeedbackAPI{}
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

func TestSlackUpRatingIsRecordedAndIsNoTurn(t *testing.T) {
	a, rec, api := newFeedbackSlackAdapter()
	if _, ok := a.interactive(ratingPayload(t, "U1", FeedbackUp)); ok {
		t.Fatal("a rating is never a turn")
	}
	if len(rec.ratings) != 1 || rec.ratings[0].Rating != FeedbackUp || rec.ratings[0].TaskID != "t-root" ||
		rec.ratings[0].Conversation != "slack:C1/40.0" || rec.ratings[0].Answer != "*It is OOMKilled.*" {
		t.Fatalf("ratings = %+v, want one 👍 on t-root with the answer", rec.ratings)
	}
	if len(api.views) != 0 || len(api.ephemerals) != 0 {
		t.Error("a 👍 opens nothing and posts nothing")
	}
}

// A 👎 whose ask is no longer on record tells the rater only the answer is kept.
func TestSlackDownRatingWithNoAskOnRecordSaysOnlyTheAnswerIsKept(t *testing.T) {
	a, rec, api := newFeedbackSlackAdapter()
	rec.noAsk = true
	if _, ok := a.interactive(ratingPayload(t, "U1", FeedbackDown)); ok {
		t.Fatal("a rating is never a turn")
	}
	if len(api.views) != 1 {
		t.Fatalf("views = %d, want the note modal", len(api.views))
	}
	view, _ := json.Marshal(api.views[0])
	if !strings.Contains(string(view), "This answer is kept on your install for 30 days so an operator can turn it into a test.") ||
		strings.Contains(string(view), "Your question") {
		t.Errorf("modal's kept line must name the answer alone: %s", view)
	}
}

func TestSlackDownRatingOpensTheNoteAndKeepsTheExchange(t *testing.T) {
	a, rec, api := newFeedbackSlackAdapter()
	viewsAtRate := -1
	rec.onRate = func() { viewsAtRate = len(api.views) }
	if _, ok := a.interactive(ratingPayload(t, "U1", FeedbackDown)); ok {
		t.Fatal("a rating is never a turn")
	}
	if viewsAtRate != 0 {
		t.Errorf("the 👎 was kept after the modal opened (%d views); it must be kept first", viewsAtRate)
	}
	if len(api.triggers) != 1 || api.triggers[0] != "trig-1" || api.views[0].CallbackID != slackFeedbackCallback {
		t.Fatalf("views = %+v, want the note modal opened on the tap's trigger", api.views)
	}
	view, _ := json.Marshal(api.views[0])
	for _, want := range []string{slackFeedbackTitle, slackFeedbackLabel, `"optional":true`, "Your question and this answer are kept on your install for 30 days", slackFeedbackSubmit, slackFeedbackClose} {
		if !strings.Contains(string(view), want) {
			t.Errorf("modal lacks %q: %s", want, view)
		}
	}
	if len(rec.ratings) != 1 {
		t.Fatalf("ratings = %+v", rec.ratings)
	}
	r := rec.ratings[0]
	if r.Rating != FeedbackDown || r.Answer != "*It is OOMKilled.*" {
		t.Errorf("rating = %+v, want the 👎 with the rated answer", r)
	}

}

func TestSlackRatingFromAnUnverifiedSenderChangesNothing(t *testing.T) {
	a, rec, api := newFeedbackSlackAdapter()
	a.interactive(ratingPayload(t, "U2", FeedbackDown))
	if len(rec.ratings) != 0 || len(api.views) != 0 {
		t.Error("an unverified sender's rating opens nothing and records nothing")
	}
}

func notePayload(t *testing.T, note string) json.RawMessage {
	t.Helper()
	meta, _ := json.Marshal(slackFeedbackMeta{Conversation: "slack:C1/40.0", Task: "t-root", Channel: "C1", Thread: "40.0", Message: "50.0"})
	p := map[string]any{
		"type": slackViewSubmission,
		"user": map[string]any{"id": "U1", "team_id": "T1"},
		"view": map[string]any{"callback_id": slackFeedbackCallback, "private_metadata": string(meta),
			"state": map[string]any{"values": map[string]any{slackFeedbackNoteID: map[string]any{slackFeedbackNoteID: map[string]any{"value": note}}}}},
	}
	raw, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestSlackNoteJoinsTheRatingAndThanksTheRaterAlone(t *testing.T) {
	a, rec, api := newFeedbackSlackAdapter()
	if _, ok := a.interactive(notePayload(t, " the limit is 256Mi ")); ok {
		t.Fatal("a note is never a turn")
	}
	if len(rec.notes) != 1 || rec.notes[0] != "U1 t-root 50.0 slack:C1/40.0 the limit is 256Mi" {
		t.Errorf("notes = %v", rec.notes)
	}
	if len(api.ephemerals) != 1 || api.ephemerals[0] != "C1 U1 40.0 "+slackFeedbackThanks() ||
		!strings.Contains(api.ephemerals[0], "<https://gke-labs.github.io/kube-agents/feedback>") ||
		!strings.Contains(api.ephemerals[0], "public issue") {
		t.Errorf("ephemerals = %v, want the thanks in the thread, to the rater", api.ephemerals)
	}
}

func TestSlackEmptyNoteIsStillKeptAndThanked(t *testing.T) {
	a, rec, api := newFeedbackSlackAdapter()
	a.interactive(notePayload(t, ""))
	if len(rec.notes) != 1 || !strings.HasSuffix(rec.notes[0], "slack:C1/40.0 ") || len(api.ephemerals) != 1 {
		t.Errorf("notes = %q ephemerals = %v; want the 👎 written without a note, then the thanks", rec.notes, api.ephemerals)
	}
}

func TestSlackNoteThatWasNotKeptThanksNobody(t *testing.T) {
	a, rec, api := newFeedbackSlackAdapter()
	rec.noteErr = errFeedbackContended
	a.interactive(notePayload(t, "late"))
	if len(api.ephemerals) != 0 {
		t.Error("the thanks says the answer was kept; it must not post when it was not")
	}
}

func TestSlackFailedDownWriteOpensNoModal(t *testing.T) {
	a, rec, api := newFeedbackSlackAdapter()
	rec.rateErr = errFeedbackContended
	a.interactive(ratingPayload(t, "U1", FeedbackDown))
	if len(api.views) != 0 {
		t.Error("the modal says the answer is kept; with the write failed it must not open")
	}
}

func TestSlackLayoutAtTheBlockCapPostsWithoutTheButtons(t *testing.T) {
	full := slackChatLayout{Blocks: make([]map[string]any, slackChatBlocksMax)}
	if got := withFeedback(full, "t"); len(got.Blocks) != slackChatBlocksMax {
		t.Errorf("blocks = %d, want the answer left as it is at the cap", len(got.Blocks))
	}
}

func TestSlackAnsweredLineStaysAboveTheFeedbackButtons(t *testing.T) {
	note := map[string]any{"type": slackContextBlock}
	got := slackWithNote([]map[string]any{{"type": slackSectionBlock}, slackFeedbackBlock("t")}, note)
	if len(got) != 3 || got[1]["type"] != slackContextBlock || got[2]["type"] != slackContextActionsBlock {
		t.Errorf("blocks = %v, want the answered line above the rating controls", got)
	}
}

func TestSlackModalSaysOneDay(t *testing.T) {
	view, _ := json.Marshal(slackFeedbackModal(slackFeedbackMeta{Task: "t"}, 1, true))
	if !strings.Contains(string(view), "for 1 day so") {
		t.Errorf("modal = %s", view)
	}
}
