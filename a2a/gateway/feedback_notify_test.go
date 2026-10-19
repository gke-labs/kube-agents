package gateway

import (
	"context"
	"encoding/json"
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

func (f *feedbackConversations) AnswerFeedbackTarget(string) string { return f.target }

func (f *feedbackConversations) PostMessage(conversation string, m OutMessage) (string, error) {
	f.feedbacks = append(f.feedbacks, m.Feedback)
	return f.fakeConversations.PostMessage(conversation, m)
}

const testAnswerChat = `{"schema":"kube-agents.chat/v1","headline":"Both pods are up."}`

// The card report the notifier flags as the ask's answer is marked for
// rating under the conversation's latest ask; any other report, and a keyed
// post, is not.
func TestNotifyMarksOnlyTheFlaggedAnswer(t *testing.T) {
	conv := &feedbackConversations{
		fakeConversations: &fakeConversations{contexts: map[string]string{testConversation: testContext}, rendersChat: true},
		target:            "task-root",
	}
	n := newTestNotifier(t, &fakeNotifyPoster{})
	n.SetConversations(conv)
	for _, req := range []lib.NotifyRequest{
		{Text: "Both pods are up.", Chat: json.RawMessage(testAnswerChat), Answer: true},
		{Text: "Both pods are up.", Chat: json.RawMessage(testAnswerChat)},
		{Text: "Both pods are up.", Chat: json.RawMessage(testAnswerChat), Answer: true, Update: "plan"},
	} {
		req.Conversation, req.ContextID = testConversation, testContext
		if got := serveJSON(t, n, req); got.Error != "" {
			t.Fatalf("reply = %+v", got)
		}
	}
	if want := []string{"task-root", "", ""}; len(conv.feedbacks) != len(want) ||
		conv.feedbacks[0] != want[0] || conv.feedbacks[1] != want[1] || conv.feedbacks[2] != want[2] {
		t.Fatalf("feedback marks = %q, want %q", conv.feedbacks, want)
	}
}

// The target is the root of the conversation's latest ask, and none while
// feedback is off.
func TestAnswerFeedbackTargetIsTheLatestAsk(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	origin := completeTurn(t, r, "discord:g1/fb12", "d-1", "the fleet is fine")
	c := gatewayConversations{g: r.g}
	if got := c.AnswerFeedbackTarget("discord:g1/fb12"); got != origin.TaskID {
		t.Errorf("target = %q, want the ask's root %q", got, origin.TaskID)
	}
	if got := c.AnswerFeedbackTarget("discord:g1/none"); got != "" {
		t.Errorf("a conversation with no session has target %q, want none", got)
	}
	r.g.feedback.Store(nil)
	if got := c.AnswerFeedbackTarget("discord:g1/fb12"); got != "" {
		t.Errorf("feedback off: target = %q, want none", got)
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
