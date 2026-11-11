package gateway

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The requester gate on Google Chat's Stop button (stopgate.go): the rule
// Slack's button has, on the button the Chat adapter draws. Only the people
// who asked for work in the thread may press it; anyone else's press cancels
// nothing and posts nothing, and a typed `stop` keeps its rule. The gate's
// own rule is tested once per gated backend in slack_stop_test.go; this file
// holds what is Chat's: the wire shape of a press, and the press through the
// gateway.

// isStopPress is the button and only the button: Slack's native Stop, and a
// click on the Chat adapter's. A program's cancel on Chat (IntentCancel with
// no click), a typed stop, and a cancel from an ungated backend are not.
func TestIsStopPressIsTheButtonOnAGatedBackend(t *testing.T) {
	for name, tc := range map[string]struct {
		msg     InboundMessage
		backend string
		want    bool
	}{
		"slack's button":       {InboundMessage{Intent: IntentCancel}, slackBackend, true},
		"chat's button":        {InboundMessage{Intent: IntentCancel, TaskID: "task-1", Click: true}, gchatBackend, true},
		"a program's cancel":   {InboundMessage{Intent: IntentCancel, TaskID: "task-1"}, gchatBackend, false},
		"a typed stop on chat": {InboundMessage{Text: "stop"}, gchatBackend, false},
		"a cancel on discord":  {InboundMessage{Intent: IntentCancel, Click: true}, discordBackend, false},
	} {
		if got := isStopPress(tc.msg, tc.backend); got != tc.want {
			t.Errorf("%s: isStopPress = %v, want %v", name, got, tc.want)
		}
	}
}

const (
	gateRequester = "user@example.com"
	gateOther     = "other@example.com"
)

// gateChatRig is a Chat rig with two members admitted, the requester and
// another, and a task of the requester's running with its working line.
func gateChatRig(t *testing.T, conv string, opts ...func(*Config)) (*rig, *lib.Envelope, string) {
	t.Helper()
	r := startGchatRig(t, []string{gateRequester, gateOther}, false, opts...)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: gateRequester, MessageID: "spaces/S1/messages/M1", Text: "explain node upgrades"}
	origin := r.awaitTask(t, "platform")
	if err := r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the working line", func() bool {
		lines := r.adapter.stopLinesCopy()
		return len(lines) > 0 && strings.Contains(lines[len(lines)-1].Text, "working")
	})
	return r, origin, r.adapter.stopLinesCopy()[0].MessageID
}

func pressChatStop(r *rig, conv, who, message, task string) {
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: who, MessageID: message, Intent: IntentCancel, TaskID: task, Click: true}
}

func lineClosedOnCancelSent(r *rig) bool {
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "cancel sent") {
			return true
		}
	}
	return false
}

// steerAndWait sends a follow-up from who, not the requester, while the task
// runs, and waits for its author on the record (the requester's own steer
// records no author): the inbox is ordered per conversation, so every press
// sent before it has been handled by then. A task a press cancelled takes no
// steer, so the wait ends on the cancel too and the caller's checks say so.
func steerAndWait(t *testing.T, r *rig, conv, who, id string, origin *lib.Envelope) {
	t.Helper()
	before := 0
	if rec, err := r.g.reg.Get(context.Background(), conv); err == nil && rec != nil {
		if ref, ok := rec.TaskRefFor(origin.TaskID); ok {
			before = len(ref.SteerAuthors)
		}
	}
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: who, MessageID: id, Text: "and the node pools too"}
	waitForRecord(t, r, conv, "the steer author recorded", func(rec *SessionRecord) bool {
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && (len(ref.SteerAuthors) > before || ref.Canceled)
	})
}

// Another admitted member's press on the running task cancels nothing,
// edits nothing and posts nothing; their press on a stale line heals it, as
// anyone's does, since it cancels nothing. Their follow-up after it is the
// sync point, and the requester's press on the same record then cancels, so
// it was the presser that was refused and not the press.
func TestAChatStopPressFromAnotherMemberCancelsNothing(t *testing.T) {
	conv := "gchat:spaces/S1/threads/T-gate-other"
	r, origin, line := gateChatRig(t, conv)

	pressChatStop(r, conv, gateOther, line+"#t1", origin.TaskID)
	pressChatStop(r, conv, gateOther, "spaces/S1/messages/OLD-LINE#t2", "task-gone")
	steerAndWait(t, r, conv, gateOther, "spaces/S1/messages/M2", origin)

	rec := waitForRecord(t, r, conv, "the record", func(rec *SessionRecord) bool { return rec.ActiveTask != nil })
	if rec.ActiveTask.Detached || rec.ActiveTask.LineClosed {
		t.Fatalf("another member's press cancelled the task: %+v", rec.ActiveTask)
	}
	// The stale press's heal is the one edit, on the pressed line; the
	// steer's own acknowledgement is the one post since the ask.
	stale := false
	r.adapter.mu.Lock()
	for _, e := range r.adapter.edits {
		if e.MessageID == "spaces/S1/messages/OLD-LINE" && e.Text == staleStopNotice {
			stale = true
		} else if strings.Contains(e.Text, "cancel") {
			t.Errorf("another member's press edited %q", e.Text)
		}
	}
	r.adapter.mu.Unlock()
	if !stale {
		t.Error("another member's stale press did not heal the dead line")
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "🛑") || strings.Contains(p, staleStopNotice) {
			t.Fatalf("another member's press posted %q", p)
		}
	}

	pressChatStop(r, conv, gateRequester, line+"#t3", origin.TaskID)
	waitFor(t, "the requester's press to close the line", func() bool { return lineClosedOnCancelSent(r) })
}

// A steer author's press cancels: they asked for work in the thread too.
func TestAChatStopPressFromASteerAuthorCancels(t *testing.T) {
	conv := "gchat:spaces/S1/threads/T-gate-steer"
	r, origin, line := gateChatRig(t, conv)
	steerAndWait(t, r, conv, gateOther, "spaces/S1/messages/M2", origin)

	pressChatStop(r, conv, gateOther, line+"#t1", origin.TaskID)
	waitFor(t, "the steer author's press to close the line", func() bool { return lineClosedOnCancelSent(r) })
	rec := waitForRecord(t, r, conv, "the cancel", func(rec *SessionRecord) bool { return rec.ActiveTask != nil && rec.ActiveTask.Detached })
	if !rec.ActiveTask.LineClosed {
		t.Fatalf("the line was not closed: %+v", rec.ActiveTask)
	}
}

// A typed `stop` from another admitted member keeps its rule: it is not the
// button, so the gate does not read it, and the line answers as it does for
// any typed stop on Chat.
func TestATypedStopFromAnotherMemberOnChatKeepsItsRule(t *testing.T) {
	conv := "gchat:spaces/S1/threads/T-gate-typed"
	r, _, _ := gateChatRig(t, conv)

	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: gateOther, MessageID: "spaces/S1/messages/M2", Text: "stop"}
	waitFor(t, "the typed stop to close the line", func() bool { return lineClosedOnCancelSent(r) })
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "🛑") {
			t.Errorf("a typed stop with a line posted %q; the line is the reply", p)
		}
	}
}

// cardStopControlsOn is how many card-stop controls addressee's in subject
// holds.
func cardStopControlsOn(t *testing.T, r *rig, addressee string) int {
	t.Helper()
	n := 0
	for _, e := range inSubjectEnvelopes(t, r.url, addressee) {
		if e.Kind != lib.KindMessage {
			continue
		}
		var m lib.Message
		if err := json.Unmarshal(e.Payload, &m); err == nil && lib.CardStopRequested(m.Parts) {
			n++
		}
	}
	return n
}

// With the card stop armed (A2A_CARD_STOP=true) a press on the running task
// is a typed stop, cards and all; the gate reads it before that. Another
// member's press stops no cards and posts nothing; the requester's press
// cancels the turn and sends the card-stop control.
func TestAChatStopPressFromAnotherMemberStopsNoCards(t *testing.T) {
	conv := "gchat:spaces/S1/threads/T-gate-cards"
	r, origin, line := gateChatRig(t, conv, func(c *Config) { c.CardStop = true })

	pressChatStop(r, conv, gateOther, line+"#t1", origin.TaskID)
	steerAndWait(t, r, conv, gateOther, "spaces/S1/messages/M2", origin)

	rec := waitForRecord(t, r, conv, "the record", func(rec *SessionRecord) bool { return rec.ActiveTask != nil })
	if rec.ActiveTask.Detached || rec.CardStop != nil {
		t.Fatalf("another member's press stopped the work: %+v", rec.ActiveTask)
	}
	if n := cardStopControlsOn(t, r, "platform"); n != 0 {
		t.Fatalf("another member's press sent %d card-stop control(s)", n)
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "🛑") {
			t.Fatalf("another member's press posted %q", p)
		}
	}

	pressChatStop(r, conv, gateRequester, line+"#t2", origin.TaskID)
	waitFor(t, "the requester's press to send the card-stop control", func() bool { return cardStopControlsOn(t, r, "platform") == 1 })
	waitForRecord(t, r, conv, "the turn cancelled", func(rec *SessionRecord) bool { return rec.ActiveTask == nil || rec.ActiveTask.Detached })
}
