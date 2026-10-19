package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
	"github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"
)

// fakeSessionsAPI records agents.sessions calls as "status <conv-ish>" and
// "rename <title>".
type fakeSessionsAPI struct {
	mu        sync.Mutex
	calls     []string
	renameErr error
}

func (f *fakeSessionsAPI) SetSessionStatus(_ context.Context, channel, thread, status string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "status "+channel+"/"+thread+" "+status)
	return nil
}

func (f *fakeSessionsAPI) RenameSession(_ context.Context, channel, thread, title string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "rename "+channel+"/"+thread+" "+title)
	return f.renameErr
}

func (f *fakeSessionsAPI) got() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...)
}

// formCapture is a chat.update stub that hands back the form a real
// slack-go client sent, for sentUpdateForm.
var (
	formCaptureOnce sync.Once
	formCaptureURL  string
	formCaptureMu   sync.Mutex
	formCaptureLast url.Values
)

// sentUpdateForm is the form slack-go would send for chat.update with these
// options: blocks and attachments are marshalled only on the wire.
func sentUpdateForm(channel, ts string, options ...slack.MsgOption) url.Values {
	formCaptureOnce.Do(func() {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_ = r.ParseForm()
			formCaptureLast = r.PostForm
			_, _ = w.Write([]byte(`{"ok":true,"channel":"C","ts":"1.0"}`))
		}))
		formCaptureURL = srv.URL + "/"
	})
	formCaptureMu.Lock()
	defer formCaptureMu.Unlock()
	client := slack.New("xoxb-capture", slack.OptionAPIURL(formCaptureURL))
	if _, _, _, err := client.UpdateMessage(channel, ts, options...); err != nil {
		return url.Values{}
	}
	return formCaptureLast
}

func newUXSlackAdapter(api *fakeSlackAPI) (*SlackAdapter, *fakeSessionsAPI) {
	a := newTestSlackAdapter(api)
	a.teamID = "T1"
	sessions := &fakeSessionsAPI{}
	a.sessionsAPI = sessions
	a.syncSessions = true
	return a, sessions
}

// A question as the renderer draws it: headline, then one actions block with
// two choices and a link.
const uxQuestionBlocks = `[
 {"type":"section","block_id":"h","text":{"type":"mrkdwn","text":"*Which one?*"}},
 {"type":"actions","block_id":"a","elements":[
  {"type":"button","action_id":"kage.choice.0","text":{"type":"plain_text","text":"Option A"},"value":"Option A: restart"},
  {"type":"button","action_id":"kage.choice.1","text":{"type":"plain_text","text":"Option B"},"value":"Option B"},
  {"type":"button","action_id":"kage.link.0","text":{"type":"plain_text","text":"Docs"},"url":"https://example.com/d"}]}]`

func clickPayload(t *testing.T, user, team, channel, thread, actionID, label, value, actionTS string) json.RawMessage {
	t.Helper()
	p := map[string]any{
		"type":    "block_actions",
		"user":    map[string]any{"id": user, "team_id": team},
		"team":    map[string]any{"id": team},
		"channel": map[string]any{"id": channel},
		"message": map[string]any{"ts": "50.0", "thread_ts": thread, "text": "Which one?",
			"blocks": json.RawMessage(uxQuestionBlocks)},
		"actions":   []any{map[string]any{"action_id": actionID, "value": value, "text": map[string]any{"text": label}}},
		"action_ts": actionTS,
	}
	raw, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestSlackClickIsATurnFromTheClicker(t *testing.T) {
	api := &fakeSlackAPI{}
	a, _ := newUXSlackAdapter(api)
	msg, ok := a.interactive(clickPayload(t, "U1", "T1", "C1", "40.0", "kage.choice.0", "Option A", "Option A: restart", "60.1"))
	if !ok {
		t.Fatal("a choice click must be a turn")
	}
	want := InboundMessage{Conversation: "slack:C1/40.0", Kind: "group", AuthorID: "U1", MessageID: "60.1", Text: "Option A: restart", Click: true}
	if msg != want {
		t.Errorf("turn = %+v, want %+v", msg, want)
	}
	if len(api.updated) != 0 {
		t.Error("nothing is rewritten before the gateway has verified the clicker")
	}
}

func TestSlackClickWithoutReplySendsItsLabelAndDMKeys(t *testing.T) {
	a, _ := newUXSlackAdapter(&fakeSlackAPI{})
	msg, ok := a.interactive(clickPayload(t, "U1", "T1", "D9", "", "kage.choice.1", "Option B", "", "61.0"))
	if !ok || msg.Text != "Option B" || msg.Kind != "dm" || msg.Conversation != "slack:dm/D9/50.0" {
		t.Errorf("turn = %+v ok=%v; want the label as text in the question's own DM thread", msg, ok)
	}
}

func TestSlackClickThatIsNotOursIsNoTurn(t *testing.T) {
	a, _ := newUXSlackAdapter(&fakeSlackAPI{})
	if _, ok := a.interactive(clickPayload(t, "U1", "T1", "C1", "40.0", "kage.link.0", "Docs", "", "62.0")); ok {
		t.Error("a link button is acked and nothing else")
	}
	if _, ok := a.interactive(clickPayload(t, "U1", "T2", "C1", "40.0", "kage.choice.0", "Option A", "x", "63.0")); ok {
		t.Error("another workspace's member's click must drop")
	}
	if _, ok := a.interactive(json.RawMessage(`{"type":"view_submission"}`)); ok {
		t.Error("a view submission is not a click")
	}
}

func TestSlackVerifiedClickRewritesTheQuestion(t *testing.T) {
	api := &fakeSlackAPI{users: map[string]*slack.User{"U1": {Name: "jay", Profile: slack.UserProfile{DisplayName: "Jayanti"}}}}
	a, _ := newUXSlackAdapter(api)
	msg, _ := a.interactive(clickPayload(t, "U1", "T1", "C1", "40.0", "kage.choice.0", "Option A", "Option A: restart", "60.1"))
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	if len(api.updated) != 1 || api.updated[0].ts != "50.0" || api.updated[0].channel != "C1" {
		t.Fatalf("updates = %+v, want one rewrite of the clicked message", api.updated)
	}
	blocks := api.updatedBlocks[0]
	for _, gone := range []string{"kage.choice.0", "kage.choice.1"} {
		if strings.Contains(blocks, gone) {
			t.Errorf("rewrite still carries %q: %s", gone, blocks)
		}
	}
	for _, kept := range []string{"kage.link.0", "Which one?", "✓ Jayanti: Option A"} {
		if !strings.Contains(blocks, kept) {
			t.Errorf("rewrite lost %q: %s", kept, blocks)
		}
	}
	// A second click on the same question, by anyone, is dropped.
	if _, ok := a.interactive(clickPayload(t, "U2", "T1", "C1", "40.0", "kage.choice.1", "Option B", "Option B", "60.2")); ok {
		t.Error("a question answered once takes no second click")
	}
}

func TestSlackUnverifiedClickChangesNothingAndFreesTheQuestion(t *testing.T) {
	api := &fakeSlackAPI{}
	a, _ := newUXSlackAdapter(api)
	msg, _ := a.interactive(clickPayload(t, "U9", "T1", "C1", "40.0", "kage.choice.0", "Option A", "Option A: restart", "70.0"))
	if _, ok := a.interactive(clickPayload(t, "U1", "T1", "C1", "40.0", "kage.choice.0", "Option A", "Option A: restart", "70.1")); ok {
		t.Error("a second click while the first waits on verification must drop")
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, false)
	if len(api.updated) != 0 || api.userInfoCalls != 0 {
		t.Errorf("a dropped clicker rewrote the question (%d updates, %d users.info)", len(api.updated), api.userInfoCalls)
	}
	if _, ok := a.interactive(clickPayload(t, "U1", "T1", "C1", "40.0", "kage.choice.0", "Option A", "Option A: restart", "70.2")); !ok {
		t.Error("after a dropped click the question must take the next one")
	}
}

func TestSlackRefusedRewritePostsTheLineInTheThread(t *testing.T) {
	api := &fakeSlackAPI{}
	a, _ := newUXSlackAdapter(api)
	q := slackQuestion{channel: "C1", ts: "50.0", text: "q", blocks: json.RawMessage(`not json`)}
	a.rewriteAnswered(q, "U404", "Option <A>")
	if len(api.posted) != 1 || api.posted[0].thread != "50.0" || api.posted[0].text != "✓ Someone: Option &lt;A&gt;" {
		t.Errorf("posted = %+v, want the escaped answered line in the question's thread, naming no id", api.posted)
	}
}

func TestSlackAnsweredLayoutKeepsTheSideBar(t *testing.T) {
	atts := `[{"color":"#ECB22E","blocks":[{"type":"section","text":{"type":"mrkdwn","text":"q"}},
	 {"type":"actions","elements":[{"type":"button","action_id":"kage.choice.0","text":{"type":"plain_text","text":"Yes"},"value":"Yes"}]}]}]`
	blocks, attachments, err := slackAnsweredLayout(nil, json.RawMessage(atts), "✓ A: Yes")
	if err != nil {
		t.Fatal(err)
	}
	if len(blocks.BlockSet) != 0 || len(attachments) != 1 || attachments[0].Color != "#ECB22E" {
		t.Fatalf("blocks %d, attachments %+v; want the note beside the colour", len(blocks.BlockSet), attachments)
	}
	raw, _ := json.Marshal(attachments[0].Blocks)
	if strings.Contains(string(raw), "kage.choice") || !strings.Contains(string(raw), "✓ A: Yes") {
		t.Errorf("attachment blocks = %s", raw)
	}
}

func TestSlackTypedReplyAnswersTheOpenQuestion(t *testing.T) {
	api := &fakeSlackAPI{users: map[string]*slack.User{"U1": {RealName: "Jay P"}}}
	a, _ := newUXSlackAdapter(api)
	a.noteQuestion("slack:C1/40.0", slackQuestion{channel: "C1", ts: "50.0", text: "Which one?", blocks: json.RawMessage(uxQuestionBlocks)})
	stop := InboundMessage{Conversation: "slack:C1/40.0", AuthorID: "U1", MessageID: "80.0", Text: "stop"}
	a.notePending(stop)
	a.InboundVerified(stop.Conversation, stop.MessageID, true)
	if len(api.updated) != 0 {
		t.Fatal("a stop is a command, not an answer")
	}
	a.mu.Lock()
	if st := a.sessionStates["slack:C1/40.0"]; st != nil && st.askText != "" {
		t.Errorf("a command was kept as the thread's ask: %q", st.askText)
	}
	a.mu.Unlock()
	unmapped := InboundMessage{Conversation: "slack:C1/40.0", AuthorID: "U9", MessageID: "80.5", Text: "B obviously"}
	a.notePending(unmapped)
	a.InboundVerified(unmapped.Conversation, unmapped.MessageID, false)
	if len(api.updated) != 0 {
		t.Fatal("a dropped sender's reply answers nothing")
	}
	reply := InboundMessage{Conversation: "slack:C1/40.0", AuthorID: "U1", MessageID: "81.0",
		Text: "go with   option B, " + strings.Repeat("because ", 20)}
	a.notePending(reply)
	a.InboundVerified(reply.Conversation, reply.MessageID, true)
	if len(api.updated) != 1 {
		t.Fatalf("updates = %d, want the typed answer's rewrite", len(api.updated))
	}
	blocks := api.updatedBlocks[0]
	if strings.Contains(blocks, "kage.choice") || !strings.Contains(blocks, "✓ Jay P: go with option B, because") || !strings.Contains(blocks, "…") {
		t.Errorf("typed rewrite = %s", blocks)
	}
	again := InboundMessage{Conversation: "slack:C1/40.0", AuthorID: "U1", MessageID: "82.0", Text: "and another thing"}
	a.notePending(again)
	a.InboundVerified(again.Conversation, again.MessageID, true)
	if len(api.updated) != 1 {
		t.Error("one reply answers one question")
	}
}

// The post path records a laid-out question so a typed reply can answer it,
// and the card's later settle edit (no choices) keeps the answered line.
func TestSlackPostedQuestionIsAnsweredByATypedReplyAndKeepsItsLine(t *testing.T) {
	api := &fakeSlackAPI{users: map[string]*slack.User{"U1": {Name: "jay"}}}
	a, _ := newUXSlackAdapter(api)
	conv := "slack:C1/40.0"
	question := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Which cluster first?",
		Choices: []chatmsg.Choice{{Text: "prod-eu", Reply: "prod-eu first, for card t-1"}, {Text: "prod-us"}}}
	ts, err := a.PostMessage(conv, OutMessage{Text: "Which cluster first?", Chat: question})
	if err != nil {
		t.Fatal(err)
	}
	reply := InboundMessage{Conversation: conv, AuthorID: "U1", MessageID: "41.0", Text: "prod-us please"}
	a.notePending(reply)
	a.InboundVerified(conv, reply.MessageID, true)
	if len(api.updated) != 1 || api.updated[0].ts != ts {
		t.Fatalf("updates = %+v, want the posted question rewritten", api.updated)
	}
	if b := api.updatedBlocks[0]; strings.Contains(b, "kage.choice") || !strings.Contains(b, "✓ jay: prod-us please") || !strings.Contains(b, "Which cluster first?") {
		t.Errorf("rewrite = %s", b)
	}
	settled := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Which cluster first?"}
	if err := a.EditMessage(conv, ts, OutMessage{Text: "Which cluster first?", Chat: settled}); err != nil {
		t.Fatal(err)
	}
	if b := api.updatedBlocks[1]; !strings.Contains(b, "✓ jay: prod-us please") {
		t.Errorf("the settle edit lost the answered line: %s", b)
	}
}

// An incident option's click sends its reply as the emitter wrote it (G2:
// "<title> — apply Option B", led by the label it shows), the answered line
// names the label, and a typed "apply Option B" answers the same question.
func TestSlackIncidentOptionReplyAndTypedApply(t *testing.T) {
	const label, reply = "Raise the memory limit", "Raise the memory limit — apply Option B"
	api := &fakeSlackAPI{users: map[string]*slack.User{"U1": {Name: "jay"}}}
	a, _ := newUXSlackAdapter(api)
	msg, ok := a.interactive(clickPayload(t, "U1", "T1", "C1", "40.0", "kage.choice.1", label, reply, "61.0"))
	if !ok || msg.Text != reply {
		t.Fatalf("turn = %+v ok=%v, want the reply verbatim", msg, ok)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	if b := api.updatedBlocks[0]; !strings.Contains(b, "✓ jay: "+label) || strings.Contains(b, "apply Option B") {
		t.Errorf("answered line should name the label it showed: %s", b)
	}
	conv := "slack:C1/41.0"
	a.noteQuestion(conv, slackQuestion{channel: "C1", ts: "70.0", text: "triage", blocks: json.RawMessage(uxQuestionBlocks)})
	typed := InboundMessage{Conversation: conv, AuthorID: "U1", MessageID: "71.0", Text: "apply Option B"}
	a.notePending(typed)
	a.InboundVerified(conv, typed.MessageID, true)
	if b := api.updatedBlocks[len(api.updatedBlocks)-1]; !strings.Contains(b, "✓ jay: apply Option B") || strings.Contains(b, "kage.choice") {
		t.Errorf("typed apply rewrite = %s", b)
	}
}

// A typed message answers the open question only while one is open and
// unanswered, and only when it is not a gateway command.
func TestSlackAnswersOpenQuestion(t *testing.T) {
	a, _ := newUXSlackAdapter(&fakeSlackAPI{})
	conv := "slack:C1/40.0"
	reply := InboundMessage{Conversation: conv, Text: "prod-eu"}
	if a.answersOpenQuestion(reply) {
		t.Fatal("no question is open")
	}
	a.noteQuestion(conv, slackQuestion{channel: "C1", ts: "50.0"})
	if !a.answersOpenQuestion(reply) {
		t.Error("an open question is answered by a typed reply")
	}
	if a.answersOpenQuestion(InboundMessage{Conversation: conv, Text: "stop"}) {
		t.Error("a stop answers nothing")
	}
	a.mu.Lock()
	a.answerLocked("C1/50.0")
	a.mu.Unlock()
	if a.answersOpenQuestion(reply) {
		t.Error("an answered question takes no second answer")
	}
}

// An answer whose only choice is its next step asks nothing: a typed reply
// does not answer it, and posting it leaves a question already open in
// place. A message with any other choice is still the open question.
func TestSlackNextStepIsNoOpenQuestion(t *testing.T) {
	a, _ := newUXSlackAdapter(&fakeSlackAPI{})
	conv := "slack:C1/40.0"
	reply := InboundMessage{Conversation: conv, Text: "are the node pools autoscaling?"}
	step := OutMessage{Text: "seeded-c is behind.", Chat: &chatmsg.Message{Schema: chatmsg.Schema,
		Headline: "seeded-c is behind.", Choices: []chatmsg.Choice{{Text: "upgrade plan for seeded-c", Next: true}}}}
	a.trackQuestion(conv, "60.0", step)
	if a.answersOpenQuestion(reply) {
		t.Fatal("an answer's next step is no question a typed reply answers")
	}
	a.noteQuestion(conv, slackQuestion{channel: "C1", ts: "50.0"})
	a.trackQuestion(conv, "61.0", step)
	a.mu.Lock()
	open := a.uxLocked().questions[conv].ts
	a.mu.Unlock()
	if open != "50.0" {
		t.Errorf("open question = %q, want the card's question 50.0 kept", open)
	}
	if asksQuestion(step.Chat.Choices) || !asksQuestion([]chatmsg.Choice{{Text: "A"}, {Text: "B", Next: true}}) {
		t.Error("asksQuestion: only choices that are all next steps ask nothing")
	}
}

// A card asking again on the same message (its update key) is a fresh
// question: the earlier answer does not block its buttons.
func TestSlackReaskedQuestionTakesAClick(t *testing.T) {
	api := &fakeSlackAPI{}
	a, _ := newUXSlackAdapter(api)
	conv := "slack:C1/40.0"
	question := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Which one?", Choices: []chatmsg.Choice{{Text: "Option A"}, {Text: "Option B"}}}
	msg, _ := a.interactive(clickPayload(t, "U1", "T1", "C1", "40.0", "kage.choice.0", "Option A", "Option A: restart", "60.1"))
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	if err := a.EditMessage(conv, "50.0", OutMessage{Text: "Which one now?", Chat: question}); err != nil {
		t.Fatal(err)
	}
	if _, ok := a.interactive(clickPayload(t, "U1", "T1", "C1", "40.0", "kage.choice.1", "Option B", "Option B", "60.2")); !ok {
		t.Error("a question asked again on the same message must take a click")
	}
}

// A typed reply verified before a click that was claimed after it answers
// the question; the click's turn still goes, but nothing is rewritten twice.
func TestSlackClickThatLostToATypedReplyRewritesNothing(t *testing.T) {
	api := &fakeSlackAPI{}
	a, _ := newUXSlackAdapter(api)
	conv := "slack:C1/40.0"
	a.noteQuestion(conv, slackQuestion{channel: "C1", ts: "50.0", text: "Which one?", blocks: json.RawMessage(uxQuestionBlocks)})
	reply := InboundMessage{Conversation: conv, AuthorID: "U1", MessageID: "55.0", Text: "B"}
	a.notePending(reply)
	click, ok := a.interactive(clickPayload(t, "U2", "T1", "C1", "40.0", "kage.choice.0", "Option A", "Option A: restart", "56.0"))
	if !ok {
		t.Fatal("the click was claimed before the reply was verified, so it is a turn")
	}
	a.InboundVerified(conv, reply.MessageID, true)
	a.InboundVerified(click.Conversation, click.MessageID, true)
	if len(api.updated) != 1 {
		t.Fatalf("updates = %d, want the typed answer's rewrite alone", len(api.updated))
	}
}

// A text-only settle of an answered question keeps the answered line.
func TestSlackTextSettleKeepsTheAnsweredLine(t *testing.T) {
	api := &fakeSlackAPI{users: map[string]*slack.User{"U1": {Name: "jay"}}}
	a, _ := newUXSlackAdapter(api)
	msg, _ := a.interactive(clickPayload(t, "U1", "T1", "C1", "40.0", "kage.choice.0", "Option A", "Option A: restart", "60.1"))
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	if err := a.EditMessage("slack:C1/40.0", "50.0", OutMessage{Text: "Which one? (answered)"}); err != nil {
		t.Fatal(err)
	}
	if b := api.updatedBlocks[len(api.updatedBlocks)-1]; !strings.Contains(b, "✓ jay: Option A") || !strings.Contains(b, "Which one? (answered)") {
		t.Errorf("text settle blocks = %s", b)
	}
}

// A rewrite Slack refuses leaves no line behind, so the card's settle does
// not show the answer a second time beside the one posted in the thread.
func TestSlackRefusedRewriteRecordsNoLine(t *testing.T) {
	api := &fakeSlackAPI{}
	a, _ := newUXSlackAdapter(api)
	a.mu.Lock()
	a.uxLocked().answered["C1/50.0"] = true
	a.mu.Unlock()
	a.rewriteAnswered(slackQuestion{channel: "C1", thread: "40.0", ts: "50.0", blocks: json.RawMessage(`not json`)}, "U1", "A")
	if line := a.answeredLine("C1", "50.0"); line != "" {
		t.Errorf("line = %q after a refused rewrite", line)
	}
	if len(api.posted) != 1 || api.posted[0].thread != "40.0" {
		t.Errorf("posted = %+v, want the line in the question's thread root", api.posted)
	}
}

// Blocks slack-go does not model travel as written: a fold's container keeps
// its children through the rewrite.
func TestSlackAnsweredLayoutKeepsBlocksSlackGoDoesNotModel(t *testing.T) {
	blocks := `[{"type":"container","block_id":"f","children":[{"type":"section","text":{"type":"mrkdwn","text":"why"}}]},
	 {"type":"actions","elements":[{"type":"button","action_id":"kage.choice.0","text":{"type":"plain_text","text":"Yes"},"value":"Yes"}]}]`
	got, _, err := slackAnsweredLayout(json.RawMessage(blocks), nil, "✓ A: Yes")
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(got)
	if !strings.Contains(string(raw), `"children"`) || !strings.Contains(string(raw), "why") {
		t.Errorf("rewrite dropped the container's children: %s", raw)
	}
}

func TestSlackReactionsMapTokensToEmoji(t *testing.T) {
	api := &fakeSlackAPI{}
	a, _ := newUXSlackAdapter(api)
	for _, call := range []func() error{
		func() error { return a.React("slack:C1/40.0", "40.0", "upgrade") },
		func() error { return a.React("slack:dm/D1/41.0", "41.0", "blocked") },
		func() error { return a.RemoveReaction("slack:C1/40.0", "40.0", "upgrade") },
		func() error { return a.React("slack:C1/40.0", "40.0", "done") },
		func() error { return a.React("slack:C1/40.0", "40.0", "no-such-kind") },
		func() error { return a.React("slack:C1/40.0", "", "failed") },
	} {
		if err := call(); err != nil {
			t.Fatal(err)
		}
	}
	want := []string{"+arrow_up C1/40.0", "+double_vertical_bar D1/41.0", "-arrow_up C1/40.0"}
	if !slices.Equal(api.reactions, want) {
		t.Errorf("reactions = %v, want %v (done and an unknown kind draw nothing)", api.reactions, want)
	}
	api.reactErr = errors.New("already_reacted")
	if err := a.React("slack:C1/40.0", "40.0", "x"); err != nil {
		t.Errorf("unknown token: %v", err)
	}
	if err := a.React("slack:C1/40.0", "40.0", "failed"); err != nil {
		t.Errorf("a reaction already there is no error: %v", err)
	}
	api.reactErr = errors.New("missing_scope")
	if err := a.RemoveReaction("slack:C1/40.0", "40.0", "failed"); err == nil {
		t.Error("a real failure is returned for the caller to log")
	}
}

func TestSlackSessionFollowsTheChainAndItsCardWork(t *testing.T) {
	api := &fakeSlackAPI{}
	a, sessions := newUXSlackAdapter(api)
	conv := "slack:C1/40.0"
	ask := InboundMessage{Conversation: conv, AuthorID: "U1", MessageID: "40.0", Text: "<@UBOT> which clusters: are behind?"}
	a.notePending(ask)
	a.InboundVerified(conv, ask.MessageID, true)
	a.TaskStarted(conv, "task-1")
	a.TaskAccepted(conv, "task-1")
	a.MarkChanged(conv, "upgrade", "")
	a.TaskTerminal(conv, "task-1", lib.StateCompleted, TerminalFromExecutor, "")
	a.MarkChanged(conv, "upgrade", "blocked")
	a.MarkChanged(conv, "upgrade", "")
	a.MarkChanged(conv, "", "")
	want := []string{
		"status C1/40.0 processing",
		"rename C1/40.0 which clusters, are behind?",
		"status C1/40.0 suspended",
		"status C1/40.0 processing",
		"status C1/40.0 closed",
	}
	if got := sessions.got(); !slices.Equal(got, want) {
		t.Errorf("sessions calls =\n%v\nwant\n%v", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
}

func TestSlackSessionDirectAnswerClosesAndARestartedThreadIsClosedToo(t *testing.T) {
	a, sessions := newUXSlackAdapter(&fakeSlackAPI{})
	a.TaskStarted("slack:dm/D1", "t")
	if got := sessions.got(); len(got) != 0 {
		t.Errorf("calls = %v; a thread-less DM has no session", got)
	}
	// A chain this process never saw start (a restart mid-chain) still
	// closes: the previous process's Working… is on the thread.
	a.TaskTerminal("slack:C1/9.0", "t", lib.StateFailed, TerminalFromExecutor, "")
	a.TaskStarted("slack:dm/D1/5.0", "t")
	a.TaskTerminal("slack:dm/D1/5.0", "t", lib.StateCompleted, TerminalFromExecutor, "")
	want := []string{"status C1/9.0 closed", "status D1/5.0 processing", "status D1/5.0 closed"}
	if got := sessions.got(); !slices.Equal(got, want) {
		t.Errorf("calls = %v, want %v", got, want)
	}
}

func TestSlackSessionRenameRetriesUntilItLands(t *testing.T) {
	a, sessions := newUXSlackAdapter(&fakeSlackAPI{})
	clock := newFakeClock(a)
	conv := "slack:C1/40.0"
	a.noteAsk(conv, "check prod")
	sessions.renameErr = errors.New("invalid_name")
	a.TaskStarted(conv, "t")
	sessions.renameErr = nil
	clock.advance(slackSessionRefresh)
	a.MarkChanged(conv, "question", "")
	a.MarkChanged(conv, "fleet", "")
	want := []string{"status C1/40.0 processing", "rename C1/40.0 check prod", "status C1/40.0 processing", "rename C1/40.0 check prod"}
	if got := sessions.got(); !slices.Equal(got, want) {
		t.Errorf("calls = %v, want %v (one refresh, the rename retried once and then kept)", got, want)
	}
}

// A typed answer to an open question is not the thread's ask: after a
// restart, or when the first ask sanitised to nothing, it must not become
// the title.
func TestSlackTypedAnswerDoesNotTitleTheThread(t *testing.T) {
	api := &fakeSlackAPI{users: map[string]*slack.User{"U1": {RealName: "Jay P"}}}
	a, _ := newUXSlackAdapter(api)
	conv := "slack:C1/40.0"
	a.noteQuestion(conv, slackQuestion{channel: "C1", ts: "50.0", text: "Which one?", blocks: json.RawMessage(uxQuestionBlocks)})
	reply := InboundMessage{Conversation: conv, AuthorID: "U1", MessageID: "81.0", Text: "option B"}
	a.notePending(reply)
	a.InboundVerified(reply.Conversation, reply.MessageID, true)
	a.mu.Lock()
	defer a.mu.Unlock()
	if st := a.sessionStates[conv]; st != nil && st.askText != "" {
		t.Errorf("the typed answer was kept as the thread's ask: %q", st.askText)
	}
}

// A typed answer's line shows a link by its label, a bare link by its url,
// a channel by its name, and no raw mention.
func TestSlackTypedWordsReduceSlackEntities(t *testing.T) {
	got := slackTypedWords("<@U123> go with <https://x.io/a|option B>, see <https://x.io/b> in <#C1|ops>")
	if want := "go with option B, see https://x.io/b in ops"; got != want {
		t.Errorf("slackTypedWords = %q, want %q", got, want)
	}
}

// A first ask that sanitises to nothing gives way to the next one.
func TestSlackSessionUntitleableAskGivesWay(t *testing.T) {
	a, sessions := newUXSlackAdapter(&fakeSlackAPI{})
	conv := "slack:C1/40.0"
	a.noteAsk(conv, "<https://example.com/x>")
	a.TaskStarted(conv, "t1")
	a.noteAsk(conv, "check prod")
	a.TaskTerminal(conv, "t1", lib.StateCompleted, TerminalFromExecutor, "")
	a.TaskStarted(conv, "t2")
	want := []string{"status C1/40.0 processing", "status C1/40.0 closed", "status C1/40.0 processing", "rename C1/40.0 check prod"}
	if got := sessions.got(); !slices.Equal(got, want) {
		t.Errorf("calls = %v, want %v", got, want)
	}
}

// A verified message gives up its pending slot, so a click still waiting
// survives any number of messages verified after it.
func TestSlackWaitingClickOutlivesVerifiedMessages(t *testing.T) {
	a, _ := newUXSlackAdapter(&fakeSlackAPI{})
	click, ok := a.interactive(clickPayload(t, "U1", "T1", "C1", "40.0", "kage.choice.0", "Option A", "Option A: restart", "60.1"))
	if !ok {
		t.Fatal("click is a turn")
	}
	for i := 0; i < slackUXCap+10; i++ {
		m := InboundMessage{Conversation: "slack:C2/1.0", AuthorID: "U1", MessageID: fmt.Sprintf("m-%d", i), Text: "hi"}
		a.notePending(m)
		a.InboundVerified(m.Conversation, m.MessageID, true)
	}
	a.mu.Lock()
	_, waiting := a.uxLocked().pending[slackPendingKey(click.Conversation, click.MessageID)]
	claimed := a.uxLocked().claims["C1/50.0"]
	a.mu.Unlock()
	if !waiting || !claimed {
		t.Errorf("waiting=%v claimed=%v; the queued click lost its record", waiting, claimed)
	}
}

// A settle edit over an answered question that the renderer refuses is a
// refused layout, not a failed call.
func TestSlackAnsweredOptionsWrapsRefusals(t *testing.T) {
	big := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "q"}
	_, err := slackAnsweredOptions(OutMessage{Text: strings.Repeat("x", slackChatTextMax+1), Chat: big}, "✓ A: B")
	if !errors.Is(err, ErrLayoutRefused) {
		t.Errorf("err = %v, want ErrLayoutRefused", err)
	}
}

func TestSlackSuggestedPrompts(t *testing.T) {
	api := &fakeSlackAPI{}
	a, _ := newUXSlackAdapter(api)
	a.suggestPrompts(&slackevents.AssistantThreadStartedEvent{AssistantThread: slackevents.AssistantThread{ChannelID: "D1", ThreadTimeStamp: "1.0"}})
	a.suggestPrompts(&slackevents.AppHomeOpenedEvent{Channel: "D2", Tab: "messages"})
	a.suggestPrompts(&slackevents.AppHomeOpenedEvent{Channel: "D3", Tab: "home"})
	if len(api.prompts) != 2 {
		t.Fatalf("prompts set %d times, want 2 (not on the Home tab)", len(api.prompts))
	}
	if api.prompts[0].ChannelID != "D1" || api.prompts[0].ThreadTS != "1.0" || api.prompts[1].ChannelID != "D2" || api.prompts[1].ThreadTS != "" {
		t.Errorf("prompts = %+v", api.prompts)
	}
	var got []string
	for _, p := range api.prompts[1].Prompts {
		if p.Title != p.Message {
			t.Errorf("prompt %+v: a tap sends what it shows", p)
		}
		got = append(got, p.Message)
	}
	if !slices.Equal(got, slackSuggestedPrompts) {
		t.Errorf("prompts = %v", got)
	}
}

func TestSlackSessionsClientCallsTheJSONAPI(t *testing.T) {
	var seen []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		seen = append(seen, r.URL.Path+" "+r.Header.Get("Authorization")+" "+r.Header.Get("Content-Type")+" "+string(body))
		if strings.HasSuffix(r.URL.Path, "rename") {
			_, _ = w.Write([]byte(`{"ok":false,"error":"invalid_name"}`))
			return
		}
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()
	c := slackSessionsClient{token: "xoxb-t", apiURL: srv.URL + "/", http: srv.Client()}
	if err := c.SetSessionStatus(context.Background(), "C1", "1.0", "processing"); err != nil {
		t.Fatal(err)
	}
	if err := c.RenameSession(context.Background(), "C1", "1.0", "x"); err == nil || !strings.Contains(err.Error(), "invalid_name") {
		t.Errorf("rename err = %v, want Slack's refusal", err)
	}
	want := `/agents.sessions.setStatus Bearer xoxb-t application/json; charset=utf-8 {"channel_id":"C1","status":"processing","thread_ts":"1.0"}`
	if len(seen) != 2 || seen[0] != want {
		t.Errorf("requests = %q, want first %q", seen, want)
	}
}
