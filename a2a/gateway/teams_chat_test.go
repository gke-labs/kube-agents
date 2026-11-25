package gateway

import (
	"errors"
	"net/http"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

const testTeamsPosted = "1794603936403-reply"

// testTeamsQuestion is a chat message that asks with two choices and
// carries one link.
func testTeamsQuestion() *chatmsg.Message {
	return &chatmsg.Message{
		Schema:   chatmsg.Schema,
		Headline: "Restart checkout-gateway?",
		Note:     "It is crash-looping.",
		Rows:     []chatmsg.Row{{Text: "3 of 3 pods restarting", Severity: "high"}},
		Choices:  []chatmsg.Choice{{Text: "Yes", Reply: "yes, restart it", Primary: true}, {Text: "No"}},
		Links:    []chatmsg.Link{{Text: "Open the workload", URL: "https://console.example/workload"}},
	}
}

// teamsCallActions is the buttons of the card api call n carried.
func teamsCallActions(t *testing.T, relay *fakeTeamsRelay, n int) []map[string]any {
	t.Helper()
	var actions []map[string]any
	for _, raw := range teamsLineActions(t, relay, n) {
		action, _ := raw.(map[string]any)
		actions = append(actions, action)
	}
	return actions
}

// teamsActionTitles is the buttons' titles, in order.
func teamsActionTitles(actions []map[string]any) string {
	var titles []string
	for _, action := range actions {
		title, _ := action["title"].(string)
		titles = append(titles, title)
	}
	return strings.Join(titles, ",")
}

func teamsChoice(reply string, index int, label string) map[string]any {
	return map[string]any{teamsPressActionKey: teamsPressActionChoice, teamsPressReplyKey: reply, teamsPressChoiceKey: index, teamsPressLabelKey: label}
}

// A chat message is posted as one card: its blocks as the card's text, a
// button per choice whose data carries the reply, a link that opens its
// url, and the running task's Stop button after them.
func TestTeamsPostMessageLaysOutAChatMessage(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	id, err := a.PostMessage("teams:"+testTeamsChannel, OutMessage{Text: "the fallback", Chat: testTeamsQuestion(), Stop: "task-1"})
	if err != nil || id != testTeamsPosted {
		t.Fatalf("post: %v %q", err, id)
	}
	text := teamsCallText(t, relay, 0)
	for _, want := range []string{"**Restart checkout-gateway?** It is crash-looping.", "• `high` 3 of 3 pods restarting"} {
		if !strings.Contains(text, want) {
			t.Errorf("the card's text lacks %q:\n%s", want, text)
		}
	}
	if strings.Contains(text, "the fallback") {
		t.Errorf("the card shows the text fallback:\n%s", text)
	}
	actions := teamsCallActions(t, relay, 0)
	if got := teamsActionTitles(actions); got != "Yes,No,Open the workload,Stop" {
		t.Fatalf("buttons %q, want the choices, the link, then Stop", got)
	}
	data, _ := actions[0]["data"].(map[string]any)
	if actions[0]["type"] != teamsActionSubmit || actions[0][teamsActionStyleKey] != teamsActionPositive ||
		data[teamsPressActionKey] != teamsPressActionChoice || data[teamsPressReplyKey] != "yes, restart it" || data[teamsPressLabelKey] != "Yes" {
		t.Errorf("the primary choice's button: %v", actions[0])
	}
	if data, _ := actions[1]["data"].(map[string]any); data[teamsPressReplyKey] != "No" {
		t.Errorf("a choice with no reply does not send its text: %v", actions[1])
	}
	if actions[2]["type"] != teamsActionOpenURL || actions[2]["url"] != "https://console.example/workload" {
		t.Errorf("the link's button: %v", actions[2])
	}
}

// A layout that draws nothing or does not fit one activity is refused, so
// the gateway posts the text, and nothing is sent; with cards off a chat
// message is text only.
func TestTeamsRefusesAChatMessageItCannotDraw(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	big := testTeamsQuestion()
	big.Detail = strings.Repeat("a long line of evidence\n", 2000)
	for name, m := range map[string]*chatmsg.Message{
		"too large":   big,
		"nothing out": {Schema: chatmsg.Schema, Tone: "good"},
	} {
		if _, err := a.PostMessage("teams:"+testTeamsChannel, OutMessage{Text: "fallback", Chat: m}); !errors.Is(err, ErrLayoutRefused) {
			t.Errorf("%s: post error %v, want a refused layout", name, err)
		}
		if err := a.EditMessage("teams:"+testTeamsChannel, testTeamsPosted, OutMessage{Text: "fallback", Chat: m}); !errors.Is(err, ErrLayoutRefused) {
			t.Errorf("%s: edit error %v, want a refused layout", name, err)
		}
	}
	if n := teamsCallCount(relay); n != 0 {
		t.Fatalf("%d calls for refused layouts, want none", n)
	}
	a.adaptiveCards = false
	if _, err := a.PostMessage("teams:"+testTeamsChannel, OutMessage{Text: "fallback", Chat: testTeamsQuestion()}); !errors.Is(err, ErrTextOnly) {
		t.Fatalf("with cards off: %v, want text only", err)
	}
}

// askedTeamsAdapter is an adapter that has posted the test question.
func askedTeamsAdapter(t *testing.T) (*TeamsAdapter, *fakeTeamsRelay) {
	t.Helper()
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	if _, err := a.PostMessage("teams:"+testTeamsChannel, OutMessage{Text: "fallback", Chat: testTeamsQuestion()}); err != nil {
		t.Fatal(err)
	}
	return a, relay
}

// A press of a choice is the turn its reply stands for, once: a second
// press while the first waits on the gateway is no turn, the verified
// answer draws the card again without its choices and with the answered
// line, and a press after that, or a redelivery, is no turn.
func TestTeamsChoicePressIsATurnOnce(t *testing.T) {
	a, relay := askedTeamsAdapter(t)
	press := func(id string) (InboundMessage, string) {
		return a.classify(decodeTeamsActivity(t, testTeamsPress(id, testTeamsPosted, teamsChoice("yes, restart it", 0, "Yes"))))
	}
	msg, reason := press("f:30")
	if reason != "" {
		t.Fatalf("a choice press was not a turn: %s", reason)
	}
	if msg.Text != "yes, restart it" || !msg.Click || msg.Intent != "" || msg.AuthorID != testTeamsUser ||
		msg.Conversation != "teams:"+testTeamsChannel || gchatClickMessage(msg.MessageID) != testTeamsPosted {
		t.Fatalf("press: %+v", msg)
	}
	if _, reason := press("f:31"); !strings.Contains(reason, "already waiting") {
		t.Fatalf("a second press while one waits: %q", reason)
	}
	if _, reason := press("f:30"); !strings.Contains(reason, "duplicate") {
		t.Fatalf("a redelivered press: %q", reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	a.rewrites.Wait()
	if n := teamsCallCount(relay); n != 2 {
		t.Fatalf("%d calls, want the post and one edit", n)
	}
	relay.mu.Lock()
	method := relay.apiCall[1]["method"]
	relay.mu.Unlock()
	if method != http.MethodPut {
		t.Fatalf("the answered question was sent by %v, want an edit", method)
	}
	if text := teamsCallText(t, relay, 1); !strings.HasSuffix(text, "✓ hangdng: Yes") || !strings.Contains(text, "Restart checkout-gateway?") {
		t.Errorf("the answered card's text:\n%s", text)
	}
	if got := teamsActionTitles(teamsCallActions(t, relay, 1)); got != "Open the workload" {
		t.Errorf("the answered card's buttons %q, want the link alone", got)
	}
	if _, reason := press("f:32"); !strings.Contains(reason, "already answered") {
		t.Fatalf("a press on an answered question: %q", reason)
	}
}

// A press the gateway does not verify answers nothing: the card stays as it
// is, and the next press is a turn.
func TestTeamsUnverifiedPressLeavesTheQuestionOpen(t *testing.T) {
	a, relay := askedTeamsAdapter(t)
	msg, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:30", testTeamsPosted, teamsChoice("No", 1, "No"))))
	if reason != "" {
		t.Fatal(reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, false)
	a.rewrites.Wait()
	if n := teamsCallCount(relay); n != 1 {
		t.Fatalf("%d calls after an unverified press, want the post alone", n)
	}
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:31", testTeamsPosted, teamsChoice("No", 1, "No")))); reason != "" {
		t.Fatalf("the next press was not a turn: %s", reason)
	}
}

// A typed reply in the conversation of an open question answers it as a
// press does, and a command typed there does not.
func TestTeamsTypedReplyAnswersTheOpenQuestion(t *testing.T) {
	a, relay := askedTeamsAdapter(t)
	command, reason := a.classify(decodeTeamsActivity(t, testTeamsActivity("f:40", "<at>Kage</at> /status")))
	if reason != "" {
		t.Fatal(reason)
	}
	a.InboundVerified(command.Conversation, command.MessageID, true)
	a.rewrites.Wait()
	if n := teamsCallCount(relay); n != 1 {
		t.Fatalf("a typed command answered the question: %d calls", n)
	}
	typed, reason := a.classify(decodeTeamsActivity(t, testTeamsActivity("f:41", "<at>Kage</at> go ahead")))
	if reason != "" || typed.Click {
		t.Fatalf("typed reply: %+v %s", typed, reason)
	}
	a.InboundVerified(typed.Conversation, typed.MessageID, true)
	a.rewrites.Wait()
	if text := teamsCallText(t, relay, 1); !strings.HasSuffix(text, "✓ hangdng: go ahead") {
		t.Errorf("the answered card's text:\n%s", text)
	}
	if got := teamsActionTitles(teamsCallActions(t, relay, 1)); got != "Open the workload" {
		t.Errorf("the answered card's buttons %q", got)
	}
}

// A press on a question the adapter does not remember (posted before a
// restart) is still its turn; the card cannot be drawn again, so the
// answered line is posted in the conversation, naming the choice by the
// label the button carried.
func TestTeamsPressOnAnUnrememberedQuestionPostsItsLine(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	msg, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:30", "1794603000000", teamsChoice("yes, restart it", 0, "Yes"))))
	if reason != "" || msg.Text != "yes, restart it" {
		t.Fatalf("press: %+v %s", msg, reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	a.rewrites.Wait()
	if n := teamsCallCount(relay); n != 1 {
		t.Fatalf("%d calls, want the one post", n)
	}
	relay.mu.Lock()
	method := relay.apiCall[0]["method"]
	relay.mu.Unlock()
	if text := teamsCallText(t, relay, 0); method != http.MethodPost || text != "✓ hangdng: Yes" {
		t.Fatalf("the line was sent by %v as %q", method, text)
	}
}

// A press that carries no reply or names no message is no turn.
func TestTeamsRefusesAMalformedChoicePress(t *testing.T) {
	a, _ := askedTeamsAdapter(t)
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:30", testTeamsPosted, teamsChoice(" ", 0, "Yes")))); !strings.Contains(reason, "no reply") {
		t.Errorf("a press with no reply: %q", reason)
	}
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:31", "", teamsChoice("yes", 0, "Yes")))); !strings.Contains(reason, "no message") {
		t.Errorf("a press with no message: %q", reason)
	}
}

// An answered question keeps its line through a later edit that lays out
// no choices, laid out or as text; an edit that lays choices out asks anew,
// without the old line, and takes a press again.
func TestTeamsAnsweredLineSurvivesEditsUntilAReAsk(t *testing.T) {
	a, relay := askedTeamsAdapter(t)
	conversation := "teams:" + testTeamsChannel
	msg, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:30", testTeamsPosted, teamsChoice("yes, restart it", 0, "Yes"))))
	if reason != "" {
		t.Fatal(reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, true)
	a.rewrites.Wait()
	settled := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Restarted checkout-gateway."}
	if err := a.EditMessage(conversation, testTeamsPosted, OutMessage{Text: "fallback", Chat: settled}); err != nil {
		t.Fatal(err)
	}
	if text := teamsCallText(t, relay, 2); !strings.HasPrefix(text, "**Restarted checkout-gateway.**") || !strings.HasSuffix(text, "✓ hangdng: Yes") {
		t.Errorf("a laid-out edit of an answered question:\n%s", text)
	}
	if err := a.EditMessage(conversation, testTeamsPosted, OutMessage{Text: "restarted, as text"}); err != nil {
		t.Fatal(err)
	}
	if text := teamsCallText(t, relay, 3); !strings.HasPrefix(text, "restarted, as text") || !strings.HasSuffix(text, "✓ hangdng: Yes") {
		t.Errorf("a text edit of an answered question:\n%s", text)
	}
	if err := a.EditMessage(conversation, testTeamsPosted, OutMessage{Text: "fallback", Chat: testTeamsQuestion()}); err != nil {
		t.Fatal(err)
	}
	if text := teamsCallText(t, relay, 4); strings.Contains(text, "✓") {
		t.Errorf("a re-ask kept the old answer:\n%s", text)
	}
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:31", testTeamsPosted, teamsChoice("No", 1, "No")))); reason != "" {
		t.Fatalf("a press on the re-asked question was not a turn: %s", reason)
	}
}

// An edit that lays the message out without choices, or as text, closes
// the question: a late press on buttons a client still shows is a turn the
// gateway handles, and writes nothing back on the card.
func TestTeamsEditWithoutChoicesClosesTheQuestion(t *testing.T) {
	a, relay := askedTeamsAdapter(t)
	conversation := "teams:" + testTeamsChannel
	if err := a.EditMessage(conversation, testTeamsPosted, OutMessage{Text: "settled as text"}); err != nil {
		t.Fatal(err)
	}
	typed, reason := a.classify(decodeTeamsActivity(t, testTeamsActivity("f:40", "<at>Kage</at> thanks")))
	if reason != "" {
		t.Fatal(reason)
	}
	a.InboundVerified(typed.Conversation, typed.MessageID, true)
	a.rewrites.Wait()
	if n := teamsCallCount(relay); n != 2 {
		t.Fatalf("a typed message after the question closed wrote on it: %d calls", n)
	}
}

// A laid-out answer marked for feedback carries the rating controls after
// its own buttons, and the redraw a rating causes keeps them all.
func TestTeamsRatedChatAnswerKeepsItsButtons(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	recorder := &fakeRecorder{admit: map[string]bool{testTeamsUser: true}}
	a.SetFeedbackRecorder(recorder)
	answer := &chatmsg.Message{
		Schema: chatmsg.Schema, Headline: "The fleet is fine.",
		Links: []chatmsg.Link{{Text: "Open the dashboard", URL: "https://console.example/fleet"}},
	}
	id, err := a.PostMessage("teams:"+testTeamsChannel, OutMessage{Text: "fallback", Chat: answer, Feedback: "task-1"})
	if err != nil {
		t.Fatal(err)
	}
	if got := teamsActionTitles(teamsCallActions(t, relay, 0)); got != "Open the dashboard,"+teamsRatingUpTitle+","+teamsRatingDownTitle {
		t.Fatalf("buttons %q, want the link, then the rating controls", got)
	}
	up := map[string]any{teamsPressActionKey: teamsPressActionRate, teamsPressTaskKey: "task-1", teamsPressRatingKey: FeedbackUp}
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:50", id, up))); !strings.Contains(reason, "rating press") {
		t.Fatalf("rating press: %q", reason)
	}
	a.rating.Wait()
	if text := teamsCallText(t, relay, 1); !strings.HasPrefix(text, "**The fleet is fine.**") || !strings.Contains(text, "Feedback recorded") {
		t.Errorf("the rated card's text:\n%s", text)
	}
	if got := teamsActionTitles(teamsCallActions(t, relay, 1)); got != "Open the dashboard,"+teamsRatingUpTitle+","+teamsRatingDownTitle {
		t.Errorf("the rated card's buttons %q", got)
	}
}
