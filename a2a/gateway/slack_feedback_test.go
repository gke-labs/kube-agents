package gateway

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"testing"

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
	noteErr error
}

func (f *fakeRecorder) Admits(_, rater string) bool { return f.admit[rater] }
func (f *fakeRecorder) KeptDays() int               { return 30 }

func (f *fakeRecorder) Rate(r FeedbackRating) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ratings = append(f.ratings, r)
	return nil
}

func (f *fakeRecorder) Note(r FeedbackRating, note string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.notes = append(f.notes, r.Rater+" "+r.TaskID+" "+r.MessageID+" "+r.Conversation+" "+note)
	return f.noteErr
}

// fakeFeedbackAPI records the modal, the ephemeral line, and answers the
// thread read with replies.
type fakeFeedbackAPI struct {
	mu         sync.Mutex
	views      []slack.ModalViewRequest
	triggers   []string
	ephemerals []string
	replies    []slack.Message
	history    []slack.Message
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

func (f *fakeFeedbackAPI) GetConversationRepliesContext(_ context.Context, _ *slack.GetConversationRepliesParameters) ([]slack.Message, bool, string, error) {
	return f.replies, false, "", nil
}

func (f *fakeFeedbackAPI) GetConversationHistoryContext(_ context.Context, _ *slack.GetConversationHistoryParameters) (*slack.GetConversationHistoryResponse, error) {
	return &slack.GetConversationHistoryResponse{Messages: f.history}, nil
}

func threadMsg(ts, user, bot, text string) slack.Message {
	return slack.Message{Msg: slack.Msg{Timestamp: ts, User: user, BotID: bot, Text: text}}
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
	if _, err := slackRatedOptions(OutMessage{Text: strings.Repeat("x", slackChatSectionMax+1), Feedback: "t"}); err == nil {
		t.Error("a chunk past a section's cap must be refused, so the gateway posts it as text")
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
		rec.ratings[0].Conversation != "slack:C1/40.0" || rec.ratings[0].Ask != "" {
		t.Fatalf("ratings = %+v, want one bare 👍 on t-root", rec.ratings)
	}
	if len(api.views) != 0 || len(api.ephemerals) != 0 {
		t.Error("a 👍 opens nothing and posts nothing")
	}
}

func TestSlackDownRatingOpensTheNoteAndKeepsTheExchange(t *testing.T) {
	a, rec, api := newFeedbackSlackAdapter()
	api.replies = []slack.Message{
		threadMsg("40.0", "U1", "", "<@UBOT> why is payments-api crashing?"),
		threadMsg("45.0", "UBOT", "B1", "checking seeded-a."),
		threadMsg("50.0", "UBOT", "B1", "*It is OOMKilled.*"),
	}
	if _, ok := a.interactive(ratingPayload(t, "U1", FeedbackDown)); ok {
		t.Fatal("a rating is never a turn")
	}
	if len(api.triggers) != 1 || api.triggers[0] != "trig-1" || api.views[0].CallbackID != slackFeedbackCallback {
		t.Fatalf("views = %+v, want the note modal opened on the tap's trigger", api.views)
	}
	view, _ := json.Marshal(api.views[0])
	for _, want := range []string{slackFeedbackTitle, slackFeedbackLabel, `"optional":true`, "kept on your install for 30 days", slackFeedbackSubmit, slackFeedbackClose} {
		if !strings.Contains(string(view), want) {
			t.Errorf("modal lacks %q: %s", want, view)
		}
	}
	if len(rec.ratings) != 1 {
		t.Fatalf("ratings = %+v", rec.ratings)
	}
	r := rec.ratings[0]
	if r.Rating != FeedbackDown || r.Ask != "why is payments-api crashing?" || r.Answer != "*It is OOMKilled.*" || len(r.Transcript) != 3 ||
		r.Transcript[0].From != FeedbackFromUser || r.Transcript[2].From != FeedbackFromAgent {
		t.Errorf("rating = %+v, want the ask without the mention, the answer, and the thread", r)
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
	if len(api.ephemerals) != 1 || api.ephemerals[0] != "C1 U1 40.0 "+slackFeedbackThanksLine {
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

func TestSlackExchangeKeepsOnlyTheAgentTheAskerAndTheRater(t *testing.T) {
	a, rec, api := newFeedbackSlackAdapter()
	api.replies = []slack.Message{
		threadMsg("40.0", "U3", "", "<@UBOT> is checkout healthy?"),
		threadMsg("41.0", "U4", "", "a colleague's aside"),
		threadMsg("45.0", "UBOT", "B1", "It is."),
		threadMsg("46.0", "U1", "", "are you sure?"),
		threadMsg("50.0", "UBOT", "B1", "*It is OOMKilled.*"),
	}
	a.interactive(ratingPayload(t, "U1", FeedbackDown))
	r := rec.ratings[0]
	if r.Ask != "are you sure?" {
		t.Errorf("ask = %q, want the last message a person wrote before the answer", r.Ask)
	}
	var texts []string
	for _, turn := range r.Transcript {
		texts = append(texts, turn.Text)
	}
	if got := strings.Join(texts, "|"); strings.Contains(got, "aside") || strings.Contains(got, "checkout") {
		t.Errorf("transcript = %q; a third party's messages are not kept", got)
	}
}

func TestSlackModalSaysOneDay(t *testing.T) {
	view, _ := json.Marshal(slackFeedbackModal(slackFeedbackMeta{Task: "t"}, 1))
	if !strings.Contains(string(view), "for 1 day so") {
		t.Errorf("modal = %s", view)
	}
}
