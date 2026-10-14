package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// chatPart encodes a chat message object as the DataPart that carries it.
func chatPart(t *testing.T, m chatmsg.Message) lib.Part {
	t.Helper()
	data, err := chatmsg.Encode(m)
	if err != nil {
		t.Fatal(err)
	}
	return lib.Part{Kind: "data", Data: data}
}

// publishArtifactUpdate publishes one artifact-update chunk from the executor.
func publishArtifactUpdate(t *testing.T, r *rig, origin *lib.Envelope, name string, parts []lib.Part, appendChunk, last bool) {
	t.Helper()
	payload, _ := json.Marshal(lib.ArtifactUpdate{TaskID: origin.TaskID, ContextID: origin.ContextID,
		Artifact: lib.Artifact{ArtifactID: "artifact-" + origin.TaskID + "-" + name, Name: name, Parts: parts},
		Append:   appendChunk, LastChunk: last})
	env, err := lib.NewArtifactUpdateEnvelope(lib.Party{Session: "platform", AgentType: "test-executor"},
		origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(context.Background(), lib.TaskEventsSubject("platform", origin.TaskID), env); err != nil {
		t.Fatal(err)
	}
}

// publishStatusMessage publishes a status-update whose message is parts.
func publishStatusMessage(t *testing.T, r *rig, origin *lib.Envelope, state lib.TaskState, final bool, parts []lib.Part) {
	t.Helper()
	payload, _ := json.Marshal(lib.StatusUpdate{TaskID: origin.TaskID, ContextID: origin.ContextID,
		Status: lib.TaskStatus{State: state, Message: &lib.Message{Role: "agent", MessageID: "msg-" + string(state), Parts: parts}},
		Final:  final})
	env, err := lib.NewStatusUpdateEnvelope(lib.Party{Session: "platform", AgentType: "test-executor"},
		origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(context.Background(), lib.TaskEventsSubject("platform", origin.TaskID), env); err != nil {
		t.Fatal(err)
	}
}

// startChatTask starts a task in conv and returns its origin, with the fake
// laying chat messages out when renders is set.
func startChatTask(t *testing.T, r *rig, conv string, renders bool) *lib.Envelope {
	t.Helper()
	r.adapter.mu.Lock()
	r.adapter.rendersChat = renders
	r.adapter.mu.Unlock()
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-1", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	if err := r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	return origin
}

func (a *fakeAdapter) chatPostsCopy() []*chatmsg.Message {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]*chatmsg.Message(nil), a.chatPosts...)
}

func (a *fakeAdapter) chatEditsCopy() []*chatmsg.Message {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]*chatmsg.Message(nil), a.chatEdits...)
}

func hasPost(a *fakeAdapter, text string) bool {
	for _, p := range a.postTexts() {
		if p == text {
			return true
		}
	}
	return false
}

func chatCount(t *testing.T, r *rig, outcome string) float64 {
	t.Helper()
	return counterValue(t, r.g.metrics.chatMessages.WithLabelValues(outcome))
}

var testReport = chatmsg.Message{Schema: chatmsg.Schema, Headline: "3 of 12 clusters are behind", Tone: chatmsg.ToneAttention,
	Rows: []chatmsg.Row{{Text: "`prod-eu` is on 1.30", Severity: "major"}}}

const testReportText = "3 of 12 clusters are behind\n- prod-eu is on 1.30"

// A result carrying a chat message on its last chunk reaches a backend that
// lays chat messages out as one message value, its text beside it, and is
// counted rendered; the text is not posted again in chunks.
func TestChatResultIsLaidOutByABackendThatRendersIt(t *testing.T) {
	r := startRig(t)
	origin := startChatTask(t, r, "discord:g1/chat-result", true)
	publishArtifactUpdate(t, r, origin, lib.ArtifactResult, []lib.Part{{Kind: "text", Text: testReportText}, chatPart(t, testReport)}, false, true)
	publishFinal(t, r, origin, "platform", lib.StateCompleted, "")
	waitFor(t, "the laid-out result", func() bool { return len(r.adapter.chatPostsCopy()) == 1 })
	got := r.adapter.chatPostsCopy()[0]
	if got.Headline != testReport.Headline || len(got.Rows) != 1 {
		t.Fatalf("chat message = %+v, want the report", got)
	}
	n := 0
	for _, p := range r.adapter.postTexts() {
		if p == testReportText {
			n++
		}
	}
	if n != 1 {
		t.Fatalf("the result's text was posted %d times, want once, as the laid-out message's fallback: %q", n, r.adapter.postTexts())
	}
	if got := chatCount(t, r, chatRendered); got != 1 {
		t.Fatalf("rendered = %v, want 1", got)
	}
}

// Every backend but Slack answers ErrTextOnly, and the relay posts the text:
// the same post a result with no chat message makes.
func TestChatResultOnATextOnlyBackendPostsItsText(t *testing.T) {
	r := startRig(t)
	origin := startChatTask(t, r, "discord:g1/chat-text", false)
	publishArtifactUpdate(t, r, origin, lib.ArtifactResult, []lib.Part{{Kind: "text", Text: testReportText}, chatPart(t, testReport)}, false, true)
	publishFinal(t, r, origin, "platform", lib.StateCompleted, "")
	waitFor(t, "the result's text", func() bool { return hasPost(r.adapter, testReportText) })
	if len(r.adapter.chatPostsCopy()) != 0 {
		t.Fatal("a text-only backend recorded a laid-out message")
	}
	if got := chatCount(t, r, chatTextOnly); got != 1 {
		t.Fatalf("text_only = %v, want 1", got)
	}
}

// A chat message that breaks a rule is dropped whole, counted invalid, and
// its text posted; the backend is never handed it.
func TestInvalidChatMessagePostsItsFallback(t *testing.T) {
	r := startRig(t)
	origin := startChatTask(t, r, "discord:g1/chat-invalid", true)
	bad := testReport
	bad.Headline = strings.Repeat("x", 151)
	publishArtifactUpdate(t, r, origin, lib.ArtifactResult, []lib.Part{{Kind: "text", Text: testReportText}, chatPart(t, bad)}, false, true)
	publishFinal(t, r, origin, "platform", lib.StateCompleted, "")
	waitFor(t, "the fallback", func() bool { return hasPost(r.adapter, testReportText) })
	if len(r.adapter.chatPostsCopy()) != 0 {
		t.Fatal("an invalid chat message reached the backend")
	}
	if got := chatCount(t, r, chatInvalid); got != 1 {
		t.Fatalf("invalid = %v, want 1", got)
	}
}

// A chat message the backend refuses (Slack's invalid_blocks, a message too
// large for it) is retried once as its text.
func TestRefusedChatMessageIsRetriedAsText(t *testing.T) {
	r := startRig(t)
	r.adapter.mu.Lock()
	r.adapter.refuseChat = true
	r.adapter.mu.Unlock()
	origin := startChatTask(t, r, "discord:g1/chat-refused", true)
	publishArtifactUpdate(t, r, origin, lib.ArtifactResult, []lib.Part{{Kind: "text", Text: testReportText}, chatPart(t, testReport)}, false, true)
	publishFinal(t, r, origin, "platform", lib.StateCompleted, "")
	waitFor(t, "the fallback", func() bool { return hasPost(r.adapter, testReportText) })
	if got := chatCount(t, r, chatRefused); got != 1 {
		t.Fatalf("refused = %v, want 1", got)
	}
}

// A chat message on a result chunk that is not the last breaks assertion 27
// and drops; the folded text posts.
func TestChatMessageOnAnEarlierResultChunkDrops(t *testing.T) {
	r := startRig(t)
	origin := startChatTask(t, r, "discord:g1/chat-chunk", true)
	publishArtifactUpdate(t, r, origin, lib.ArtifactResult, []lib.Part{{Kind: "text", Text: "3 of 12 "}, chatPart(t, testReport)}, false, false)
	publishArtifactUpdate(t, r, origin, lib.ArtifactResult, []lib.Part{{Kind: "text", Text: "clusters are behind"}}, true, true)
	publishFinal(t, r, origin, "platform", lib.StateCompleted, "")
	waitFor(t, "the folded text", func() bool { return hasPost(r.adapter, "3 of 12 clusters are behind") })
	if len(r.adapter.chatPostsCopy()) != 0 {
		t.Fatal("a chat message off the last chunk reached the backend")
	}
}

// A finished turn's answer carries its chat message like a result does.
func TestChatTurnAnswerIsLaidOut(t *testing.T) {
	r := startRig(t)
	origin := startChatTask(t, r, "discord:g1/chat-turn", true)
	publishArtifactUpdate(t, r, origin, lib.ArtifactTurn, []lib.Part{{Kind: "text", Text: testReportText}, chatPart(t, testReport)}, false, true)
	waitFor(t, "the laid-out turn", func() bool { return len(r.adapter.chatPostsCopy()) == 1 })
}

// A notice posts once, laid out, beside the live message.
func TestChatNoticePostsOnce(t *testing.T) {
	r := startRig(t)
	origin := startChatTask(t, r, "discord:g1/chat-notice", true)
	pr := chatmsg.Message{Schema: chatmsg.Schema, Headline: "I opened PR #12 in o/r", Tone: chatmsg.ToneSuccess,
		Links: []chatmsg.Link{{Text: "Open PR ↗", URL: "https://github.com/o/r/pull/12", Primary: true}}}
	publishArtifactUpdate(t, r, origin, lib.ArtifactNotice, []lib.Part{{Kind: "text", Text: "I opened PR #12 in o/r"}, chatPart(t, pr)}, false, true)
	waitFor(t, "the laid-out notice", func() bool { return len(r.adapter.chatPostsCopy()) == 1 })
	if !hasPost(r.adapter, "I opened PR #12 in o/r") {
		t.Fatalf("posts = %q, want the notice's text beside its layout", r.adapter.postTexts())
	}
}

// A notice on a text-only backend posts its text, where a gateway before
// chat messages dropped the artifact.
func TestTextNoticePostsOnATextOnlyBackend(t *testing.T) {
	r := startRig(t)
	origin := startChatTask(t, r, "discord:g1/text-notice", false)
	publishArtifactUpdate(t, r, origin, lib.ArtifactNotice, []lib.Part{{Kind: "text", Text: "I opened PR #12 in o/r"}}, false, true)
	waitFor(t, "the notice", func() bool { return hasPost(r.adapter, "I opened PR #12 in o/r") })
}

// An input-required question carries its choices to the backend, with the
// question line the relay always posted as its text.
func TestChatQuestionCarriesItsChoices(t *testing.T) {
	r := startRig(t)
	origin := startChatTask(t, r, "discord:g1/chat-question", true)
	q := chatmsg.Message{Schema: chatmsg.Schema, Headline: "Which cluster first?",
		Choices: []chatmsg.Choice{{Text: "prod-eu", Reply: "start with prod-eu"}, {Text: "prod-us"}}}
	publishStatusMessage(t, r, origin, lib.StateInputRequired, false, []lib.Part{{Kind: "text", Text: "Which cluster first?"}, chatPart(t, q)})
	waitFor(t, "the laid-out question", func() bool { return len(r.adapter.chatPostsCopy()) == 1 })
	if got := r.adapter.chatPostsCopy()[0]; len(got.Choices) != 2 {
		t.Fatalf("choices = %+v", got.Choices)
	}
	if !hasPost(r.adapter, "❓ Which cluster first?") {
		t.Fatalf("posts = %q, want the question line as the text", r.adapter.postTexts())
	}
}

var testPlan = chatmsg.Message{Schema: chatmsg.Schema, Plan: &chatmsg.Plan{Title: "Upgrade readiness", Items: []chatmsg.PlanItem{
	{ID: "t-1", Title: "Read cluster versions", Status: chatmsg.StatusInProgress, Steps: []string{"listed 12 clusters"}, StepCount: 1}}}}

// A progress chat message lays out the live message in place of the text
// line, and the terminal edit keeps the layout with the terminal line as its
// text. A progress artifact without one returns the line to text.
func TestChatProgressLaysOutTheLiveMessage(t *testing.T) {
	r := startRig(t)
	origin := startChatTask(t, r, "discord:g1/chat-progress", true)
	publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "reading versions"}, chatPart(t, testPlan)}, false, false)
	waitFor(t, "the laid-out line", func() bool {
		edits := r.adapter.chatEditsCopy()
		return len(edits) > 0 && edits[len(edits)-1] != nil && edits[len(edits)-1].Plan != nil
	})
	publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "plain again"}}, false, false)
	waitFor(t, "the line back to text", func() bool {
		edits := r.adapter.chatEditsCopy()
		return len(edits) > 1 && edits[len(edits)-1] == nil
	})
	publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "reading versions"}, chatPart(t, testPlan)}, false, false)
	waitFor(t, "the line laid out again", func() bool {
		edits := r.adapter.chatEditsCopy()
		return len(edits) > 2 && edits[len(edits)-1] != nil
	})
	completeTask(t, r.execFor(t, origin, "platform"), "done")
	waitFor(t, "the terminal edit", func() bool {
		edits, texts := r.adapter.chatEditsCopy(), r.adapter.editTexts()
		return len(texts) > 0 && strings.Contains(texts[len(texts)-1], "completed") &&
			len(edits) > 3 && edits[len(edits)-1] != nil && edits[len(edits)-1].Plan != nil
	})
}

// On a text-only backend a progress chat message changes nothing: the line
// is the text edit it always was.
func TestChatProgressOnATextOnlyBackendEditsText(t *testing.T) {
	r := startRig(t)
	origin := startChatTask(t, r, "discord:g1/text-progress", false)
	publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "reading versions"}, chatPart(t, testPlan)}, false, false)
	completeTask(t, r.execFor(t, origin, "platform"), "done")
	waitFor(t, "the terminal edit", func() bool {
		texts := r.adapter.editTexts()
		return len(texts) > 0 && strings.Contains(texts[len(texts)-1], "completed")
	})
	if len(r.adapter.chatEditsCopy()) != 0 {
		t.Fatal("a text-only backend recorded a laid-out edit")
	}
}

// On a text-only backend a progress chat message that changes while the
// line's text does not costs no edit: the backend shows the text alone.
func TestChatProgressChangeAloneEditsNothingOnATextOnlyBackend(t *testing.T) {
	r := startRigWith(t, func(c *Config) { c.DisplayMode = displayModeDefault })
	origin := startChatTask(t, r, "discord:g1/text-plan-change", false)
	publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "a"}, chatPart(t, testPlan)}, false, false)
	waitFor(t, "the working line", func() bool {
		texts := r.adapter.editTexts()
		return len(texts) > 0 && strings.Contains(texts[len(texts)-1], "working")
	})
	time.Sleep(200 * time.Millisecond)
	before := len(r.adapter.editTexts())
	changed := testPlan
	changed.Plan = &chatmsg.Plan{Title: "Upgrade readiness", Items: []chatmsg.PlanItem{{ID: "t-1", Title: "Read cluster versions", Status: chatmsg.StatusComplete}}}
	publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "b"}, chatPart(t, changed)}, false, false)
	time.Sleep(500 * time.Millisecond)
	if after := len(r.adapter.editTexts()); after != before {
		t.Fatalf("edits went from %d to %d for a line whose text did not change", before, after)
	}
}

// A laid-out line whose next chat message the backend refuses is replaced by
// its text, the layout cleared (EditMessage with no chat message).
func TestChatProgressRefusedAfterLayoutClearsIt(t *testing.T) {
	r := startRig(t)
	origin := startChatTask(t, r, "discord:g1/chat-progress-refused", true)
	publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "a"}, chatPart(t, testPlan)}, false, false)
	waitFor(t, "the laid-out line", func() bool {
		edits := r.adapter.chatEditsCopy()
		return len(edits) > 0 && edits[len(edits)-1] != nil
	})
	r.adapter.mu.Lock()
	r.adapter.refuseChat = true
	r.adapter.mu.Unlock()
	publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "b"}, chatPart(t, testPlan)}, false, false)
	waitFor(t, "the layout cleared", func() bool {
		edits := r.adapter.chatEditsCopy()
		return len(edits) > 1 && edits[len(edits)-1] == nil
	})
}

// A terminal's mark-only chat message draws its mark on the asking message,
// posts nothing of its own, and is kept on the chain's root entry, so the
// ask stays marked past the turn's end (card work still open).
func TestTerminalMarkIsDrawnOnTheAsk(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/chat-mark"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-1", Text: "upgrade prod"}
	origin := r.awaitTask(t, "platform")
	mark, err := chatmsg.MarkPart(chatmsg.Mark{Ask: "upgrade"})
	if err != nil {
		t.Fatal(err)
	}
	publishArtifactUpdate(t, r, origin, lib.ArtifactResult, []lib.Part{{Kind: "text", Text: "on it"}}, false, true)
	publishStatusMessage(t, r, origin, lib.StateCompleted, true, []lib.Part{mark})
	want := []string{conv + " +upgrade@d-1"}
	waitFor(t, "the mark", func() bool { return len(r.adapter.reactionCalls()) == len(want) })
	if got := r.adapter.reactionCalls(); fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("reactions = %v, want %v", got, want)
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "upgrade") {
			t.Fatalf("the mark posted something: %q", p)
		}
	}
	waitFor(t, "the mark on the record", func() bool {
		rec, err := r.g.reg.Get(context.Background(), conv)
		return err == nil && rec != nil && len(rec.Tasks) > 0 && rec.Tasks[0].Mark != nil && rec.Tasks[0].Mark.Ask == "upgrade"
	})
}

// A delegated child's end is not the chain's: its terminal default waits for
// the wake, so a failed child leaves no ❌ over a wake that then answers, and
// the ask's reaction stays on until the wake's answer.
func TestAChildsEndLeavesTheMarkToTheWake(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	ctx := context.Background()
	conv := "discord:g1/t-wake-mark"
	exec, origin, _ := sessionTurnVia(t, r, spawn, conv, "", "how is the fleet?")
	ask := conv + "-0"
	mark := chatPart(t, chatmsg.Message{Schema: chatmsg.Schema, Headline: "looking", Mark: &chatmsg.Mark{Ask: "fleet"}})
	if err := exec.PublishArtifact(ctx, lib.Artifact{ArtifactID: "artifact-p", Name: lib.ArtifactProgress,
		Parts: []lib.Part{{Kind: "text", Text: "looking"}, mark}}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the ask's mark", func() bool { return len(r.adapter.reactionCalls()) == 1 })
	if err := exec.PublishArtifact(ctx, delegateArtifact(t, "platform", "report fleet health")); err != nil {
		t.Fatal(err)
	}
	child := awaitSubmission(t, r, targetPlatform, 0)
	waitFor(t, "chain on the record", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		pref, _ := rec.TaskRefFor(origin.TaskID)
		return len(pref.Children) == 1
	})
	completeTask(t, exec, "delegated to platform")
	waitFor(t, "parent terminal relayed", postedContaining(r, "delegated to platform"))
	publishFinal(t, r, child, targetPlatform, lib.StateFailed, "reason: quota - exceeded")
	waitFor(t, "wake spawn", func() bool { return len(spawn.calls()) == 2 })
	wakeSession := spawn.calls()[1].Session
	wake := r.awaitTask(t, wakeSession)
	if got := r.adapter.reactionCalls(); fmt.Sprint(got) != fmt.Sprint([]string{conv + " +fleet@" + ask}) {
		t.Fatalf("reactions before the wake answered = %v; the child's end must not settle the chain", got)
	}
	completeTask(t, r.execFor(t, wake, wakeSession), "the quota ran out; the fleet itself is fine")
	want := []string{conv + " +fleet@" + ask, conv + " -fleet@" + ask}
	waitFor(t, "the wake's settle", func() bool { return len(r.adapter.reactionCalls()) == len(want) })
	if got := r.adapter.reactionCalls(); fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("reactions = %v, want %v (no ❌ over a wake that answered)", got, want)
	}
}

// A click, or a typed answer to an open question, that starts a chain marks
// the ask whose question it answered: a click is no message, and an answer
// continues that ask's work, so the card's settle must land on the ask.
func TestAClickChainMarksThePreviousAsk(t *testing.T) {
	for _, tc := range []struct {
		name string
		msg  InboundMessage
	}{
		{"click", InboundMessage{MessageID: "click-ts", Text: "prod-eu", Click: true}},
		{"typed answer", InboundMessage{MessageID: "typed-ts", Text: "prod-eu please", Answer: true}},
	} {
		t.Run(tc.name, func(t *testing.T) { testAnswerChainMarksThePreviousAsk(t, tc.msg) })
	}
}

func testAnswerChainMarksThePreviousAsk(t *testing.T, answer InboundMessage) {
	r := startRig(t)
	conv := "discord:g1/click-chain"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-5", Text: "which cluster first?"}
	origin := r.awaitTask(t, "platform")
	question, err := chatmsg.MarkPart(chatmsg.Mark{Ask: "question", Settle: "blocked"})
	if err != nil {
		t.Fatal(err)
	}
	publishArtifactUpdate(t, r, origin, lib.ArtifactResult, []lib.Part{{Kind: "text", Text: "prod-eu or prod-us?"}}, false, true)
	publishStatusMessage(t, r, origin, lib.StateCompleted, true, []lib.Part{question})
	waitFor(t, "the question's mark", func() bool { return len(r.adapter.reactionCalls()) == 2 })
	waitFor(t, "the chain to end", func() bool {
		rec, err := r.g.reg.Get(context.Background(), conv)
		return err == nil && rec != nil && rec.ActiveTask == nil
	})
	answer.Conversation, answer.Kind, answer.AuthorID = conv, "group", "1001"
	r.adapter.inbox <- answer
	second := awaitSubmission(t, r, "platform", 1)
	completeTask(t, r.execFor(t, second, "platform"), "prod-eu is done")
	want := []string{conv + " +question@d-5", conv + " +blocked@d-5", conv + " -question@d-5", conv + " -blocked@d-5"}
	waitFor(t, "the click chain's settle", func() bool { return len(r.adapter.reactionCalls()) == len(want) })
	if got := r.adapter.reactionCalls(); fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("reactions = %v, want %v", got, want)
	}
}

// A conversation notify's mark lands on the latest chain's ask once its task
// has ended, and is written to the record; while a task runs it is dropped.
func TestConversationNotifyMarkWaitsForTheChainToEnd(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/notify-mark"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-3", Text: "restart the fleet"}
	origin := r.awaitTask(t, "platform")
	marker := r.g.Conversations(r.adapter).(notifyMarker)
	marker.MarkConversation(conv, MarkState{Ask: "fleet", Settle: "blocked"})
	if got := r.adapter.reactionCalls(); len(got) != 0 {
		t.Fatalf("a notify mark drew while the chain ran: %v", got)
	}
	mark, err := chatmsg.MarkPart(chatmsg.Mark{Ask: "fleet"})
	if err != nil {
		t.Fatal(err)
	}
	publishArtifactUpdate(t, r, origin, lib.ArtifactResult, []lib.Part{{Kind: "text", Text: "on it"}}, false, true)
	publishStatusMessage(t, r, origin, lib.StateCompleted, true, []lib.Part{mark})
	waitFor(t, "the turn's mark", func() bool { return len(r.adapter.reactionCalls()) == 1 })
	waitFor(t, "the chain to end", func() bool {
		rec, err := r.g.reg.Get(context.Background(), conv)
		return err == nil && rec != nil && rec.ActiveTask == nil
	})
	marker.MarkConversation(conv, MarkState{Ask: "fleet", Settle: "blocked"})
	marker.MarkConversation(conv, MarkState{})
	want := []string{conv + " +fleet@d-3", conv + " +blocked@d-3", conv + " -fleet@d-3", conv + " -blocked@d-3"}
	if got := r.adapter.reactionCalls(); fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("reactions = %v, want %v", got, want)
	}
	rec, err := r.g.reg.Get(context.Background(), conv)
	if err != nil || rec == nil || rec.Tasks[0].Mark != nil {
		t.Fatalf("record mark = %+v (err %v), want cleared", rec.Tasks[0].Mark, err)
	}
}

// A mark a progress message carried is drawn as it arrives; the terminal
// with none takes the ask off once the answer is posted, and a failed one
// leaves ❌ and lifts ⏸️.
func TestTerminalWithoutAMarkSettlesTheAsk(t *testing.T) {
	cases := []struct {
		name  string
		mark  chatmsg.Mark
		state lib.TaskState
		want  []string
	}{
		{"answered", chatmsg.Mark{Ask: "question"}, lib.StateCompleted, []string{"+question", "-question"}},
		{"failed while blocked", chatmsg.Mark{Ask: "fleet", Settle: "blocked"}, lib.StateFailed,
			[]string{"+fleet", "+blocked", "-fleet", "-blocked", "+failed"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := startRig(t)
			conv := "discord:g1/settle-" + strings.ReplaceAll(c.name, " ", "-")
			r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-2", Text: "how is the fleet?"}
			origin := r.awaitTask(t, "platform")
			mark := c.mark
			progress := chatPart(t, chatmsg.Message{Schema: chatmsg.Schema, Headline: "looking", Mark: &mark})
			publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "looking"}, progress}, false, true)
			drawn := len(MarkState{Ask: mark.Ask, Settle: mark.Settle}.tokens())
			waitFor(t, "the progress mark", func() bool { return len(r.adapter.reactionCalls()) == drawn })
			if c.state == lib.StateCompleted {
				completeTask(t, r.execFor(t, origin, "platform"), "all fine")
			} else {
				publishStatusMessage(t, r, origin, c.state, true, []lib.Part{{Kind: "text", Text: "reason: boom"}})
			}
			waitFor(t, "the settle", func() bool { return len(r.adapter.reactionCalls()) == len(c.want) })
			var got []string
			for _, call := range r.adapter.reactionCalls() {
				got = append(got, strings.TrimSuffix(strings.TrimPrefix(call, conv+" "), "@d-2"))
			}
			if fmt.Sprint(got) != fmt.Sprint(c.want) {
				t.Fatalf("reactions = %v, want %v", got, c.want)
			}
		})
	}
}
