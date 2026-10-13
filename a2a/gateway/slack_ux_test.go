package gateway

import (
	"context"
	"encoding/json"
	"errors"
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
	want := InboundMessage{Conversation: "slack:C1/40.0", Kind: "group", AuthorID: "U1", MessageID: "60.1", Text: "Option A: restart"}
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

func TestSlackSessionDirectAnswerClosesAndANeverOpenedThreadIsLeftAlone(t *testing.T) {
	a, sessions := newUXSlackAdapter(&fakeSlackAPI{})
	a.MarkChanged("slack:C1/9.0", "", "")
	a.TaskTerminal("slack:C1/9.0", "t", lib.StateFailed, TerminalFromGateway, "")
	a.TaskStarted("slack:dm/D1", "t")
	if got := sessions.got(); len(got) != 0 {
		t.Errorf("calls = %v; a thread never opened is not closed, and a thread-less DM has no session", got)
	}
	a.TaskStarted("slack:dm/D1/5.0", "t")
	a.TaskTerminal("slack:dm/D1/5.0", "t", lib.StateCompleted, TerminalFromExecutor, "")
	want := []string{"status D1/5.0 processing", "status D1/5.0 closed"}
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
