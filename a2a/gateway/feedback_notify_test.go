package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// feedbackConversations is fakeConversations naming an answer's feedback
// target, and recording each post's mark.
type feedbackConversations struct {
	*fakeConversations
	target    string
	feedbacks []string
}

func (f *feedbackConversations) AnswerFeedbackTarget(_, answerTo string) string {
	if answerTo != "plan-1" {
		return ""
	}
	return f.target
}

func (f *feedbackConversations) PostMessage(conversation string, m OutMessage) (string, error) {
	f.feedbacks = append(f.feedbacks, m.Feedback)
	return f.fakeConversations.PostMessage(conversation, m)
}

const testAnswerChat = `{"schema":"kube-agents.chat/v1","headline":"Both pods are up."}`

// The card report the notifier flags as an ask's answer is marked for rating
// under the ask its plan key names, laid out or as text; any other report, a
// key no ask holds, and a keyed post are not.
func TestNotifyMarksOnlyTheFlaggedAnswer(t *testing.T) {
	conv := &feedbackConversations{
		fakeConversations: &fakeConversations{contexts: map[string]string{testConversation: testContext}, rendersChat: true},
		target:            "task-root",
	}
	n := newTestNotifier(t, &fakeNotifyPoster{})
	n.SetConversations(conv)
	for _, req := range []lib.NotifyRequest{
		{Text: "Both pods are up.", Chat: json.RawMessage(testAnswerChat), AnswerTo: "plan-1"},
		{Text: "Both pods are up.", Chat: json.RawMessage(testAnswerChat)},
		{Text: "Both pods are up.", Chat: json.RawMessage(testAnswerChat), AnswerTo: "plan-gone"},
		{Text: "Both pods are up.", Chat: json.RawMessage(testAnswerChat), AnswerTo: "plan-1", Update: "plan"},
		{Text: "What to do: reply apply.", AnswerTo: "plan-1"},
	} {
		req.Conversation, req.ContextID = testConversation, testContext
		if got := serveJSON(t, n, req); got.Error != "" {
			t.Fatalf("reply = %+v", got)
		}
	}
	// The keyed post is an edit-or-post through PostMessage with no mark;
	// the text answer's last chunk carries it.
	want := []string{"task-root", "", "", "", "task-root"}
	if len(conv.feedbacks) != len(want) {
		t.Fatalf("feedback marks = %q, want %q", conv.feedbacks, want)
	}
	for i := range want {
		if conv.feedbacks[i] != want[i] {
			t.Fatalf("feedback marks = %q, want %q", conv.feedbacks, want)
		}
	}
}

// A flagged card answer whose layout the backend refuses keeps its rating:
// it posts as text with the mark on its last chunk.
func TestNotifyRefusedLayoutKeepsTheRatingOnText(t *testing.T) {
	conv := &feedbackConversations{
		fakeConversations: &fakeConversations{contexts: map[string]string{testConversation: testContext}},
		target:            "task-root",
	}
	n := newTestNotifier(t, &fakeNotifyPoster{})
	n.SetConversations(conv)
	req := lib.NotifyRequest{Text: "Both pods are up.", Chat: json.RawMessage(testAnswerChat), AnswerTo: "plan-1",
		Conversation: testConversation, ContextID: testContext}
	if got := serveJSON(t, n, req); got.Error != "" || got.MessageID == "" {
		t.Fatalf("reply = %+v", got)
	}
	// The refused layout, then the text post that carries the controls.
	if len(conv.feedbacks) != 2 || conv.feedbacks[1] != "task-root" {
		t.Fatalf("feedback marks = %q, want the rated text post last", conv.feedbacks)
	}
	if len(conv.chats) != 1 || conv.chats[0] != nil {
		t.Errorf("posted chats = %v, want one text post", conv.chats)
	}
}

// The target is the root of the ask the plan key is bound to, none for a key
// no ask holds, and none while feedback is off.
func TestAnswerFeedbackTargetIsTheAskItsPlanNames(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	origin := completeTurn(t, r, "discord:g1/fb12", "d-1", "the fleet is fine")
	c := gatewayConversations{g: r.g}
	// The plan's first mark binds its key to the ask.
	c.MarkConversation("discord:g1/fb12", "plan-1", MarkState{Ask: "diagnose"})
	if got := c.AnswerFeedbackTarget("discord:g1/fb12", "plan-1"); got != "" {
		t.Errorf("a chain that answered itself has card target %q, want none", got)
	}
	// The chain handed its ask to cards, as the relay records it.
	ctx := context.Background()
	rec, err := r.g.reg.Get(ctx, "discord:g1/fb12")
	if err != nil || rec == nil {
		t.Fatalf("session = %v, %v", rec, err)
	}
	rec.markCardsAnswer(origin.TaskID)
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	if got := c.AnswerFeedbackTarget("discord:g1/fb12", "plan-1"); got != origin.TaskID {
		t.Errorf("target = %q, want the ask's root %q", got, origin.TaskID)
	}
	// A second plan of the same ask (its follow-up cards) is bound to it,
	// and rates nothing more.
	c.MarkConversation("discord:g1/fb12", "plan-2", MarkState{Ask: "diagnose"})
	if rec, _ := r.g.reg.Get(ctx, "discord:g1/fb12"); rec == nil || rec.notifyChainIndex("plan-2") < 0 {
		t.Fatal("the second plan's key must be bound to the ask")
	}
	if got := c.AnswerFeedbackTarget("discord:g1/fb12", "plan-2"); got != "" {
		t.Errorf("a second card answer for one ask has target %q, want none", got)
	}
	if got := c.AnswerFeedbackTarget("discord:g1/fb12", "plan-never-marked"); got != "" {
		t.Errorf("a key no ask holds has target %q, want none", got)
	}
	if got := c.AnswerFeedbackTarget("discord:g1/none", "plan-1"); got != "" {
		t.Errorf("a conversation with no session has target %q, want none", got)
	}
	r.g.feedback.Store(nil)
	if got := c.AnswerFeedbackTarget("discord:g1/fb12", "plan-1"); got != "" {
		t.Errorf("feedback off: target = %q, want none", got)
	}
}

// One card answer per ask: the second claim is refused, and the oldest is
// forgotten past the cap.
func TestClaimCardAnswerOncePerAskWithinItsCap(t *testing.T) {
	d := &feedbackDesk{}
	if !d.claimCardAnswer("r-0") || d.claimCardAnswer("r-0") {
		t.Fatal("the first claim must succeed and the second fail")
	}
	for i := 1; i <= feedbackCardAnswersMax; i++ {
		d.claimCardAnswer(fmt.Sprintf("r-%d", i))
	}
	if !d.claimCardAnswer("r-0") {
		t.Error("the oldest claim must be forgotten past the cap")
	}
	if d.claimCardAnswer(fmt.Sprintf("r-%d", feedbackCardAnswersMax)) {
		t.Error("a recent claim must still be held")
	}
}

// A turn that leaves cards running is not the answer: its result goes out
// unmarked, and the card's report is marked instead.
func TestATurnThatLeavesCardsRunningIsNotMarked(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	r.adapter.inbox <- InboundMessage{Conversation: "discord:g1/fb13", Kind: "group", AuthorID: "1001", MessageID: "d-1", Text: "why?"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "checking seeded-a."}}}); err != nil {
		t.Fatal(err)
	}
	// The executor's terminal mark keeps the ask's kind with no settle:
	// the turn filed cards that are still running (cardsPending).
	markPart, err := chatmsg.MarkPart(chatmsg.Mark{Ask: "diagnose"})
	if err != nil {
		t.Fatal(err)
	}
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID:    origin.TaskID,
		ContextID: origin.ContextID,
		Status: lib.TaskStatus{
			State:   lib.StateCompleted,
			Message: &lib.Message{Role: "agent", MessageID: "msg-final-" + origin.TaskID, Parts: []lib.Part{markPart}},
		},
		Final: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(lib.Party{Session: "platform", AgentType: "test-executor"},
		origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(ctx, lib.TaskEventsSubject("platform", origin.TaskID), env); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "ack post", func() bool {
		for _, p := range r.adapter.postTexts() {
			if p == "checking seeded-a." {
				return true
			}
		}
		return false
	})
	if got := r.adapter.feedbackMarks(); len(got) != 0 {
		t.Fatalf("feedback marks = %v, want none on a hand-off acknowledgement", got)
	}
}

// A chain that handed the ask to cards anywhere, a delegated child's terminal
// included, rates none of its relay answers: the wake's reply is not the
// answer, the card's report is.
func TestAChainHandedToCardsRatesNoRelayAnswer(t *testing.T) {
	g := &Gateway{}
	g.feedback.Store(&feedbackDesk{g: g, ttl: time.Hour})
	rec := &SessionRecord{Tasks: []TaskRef{
		{ID: "root", Children: []string{"child"}},
		{ID: "child", Role: taskRoleChild, RootTaskID: "root"},
		{ID: "wake", Role: taskRoleWake, RootTaskID: "root"},
		{ID: "other"},
	}}
	if got := g.feedbackTarget(rec, "wake"); got != "root" {
		t.Fatalf("before any card: wake target = %q, want root", got)
	}
	rec.markCardsAnswer("child")
	if got := g.feedbackTarget(rec, "wake"); got != "" {
		t.Errorf("the child handed off to cards: wake target = %q, want none", got)
	}
	if got := g.feedbackTarget(rec, "other"); got != "other" {
		t.Errorf("another ask's own answer target = %q, want other", got)
	}
}
