package gateway

import (
	"context"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The requester gate on Google Chat's Stop button (stopgate.go): the rule
// Slack's button has, on the button the Chat adapter draws. Only the people
// who asked for work in the thread may press it; anyone else's press cancels
// nothing and posts nothing, and a typed `stop` keeps its rule.

func TestMayStopOnGoogleChatOnlyTheRequesters(t *testing.T) {
	alice := TaskRequester{Backend: gchatBackend, Subject: "h-alice"}
	bob := TaskRequester{Backend: gchatBackend, Subject: "h-bob"}
	carol := TaskRequester{Backend: gchatBackend, Subject: "h-carol"}
	dave := TaskRequester{Backend: gchatBackend, Subject: "h-dave"}
	rec := &SessionRecord{
		Tasks:          []TaskRef{{ID: "task-1", Requester: &alice, SteerAuthors: []TaskRequester{bob}}},
		SessionAuthors: []TaskRequester{carol},
	}
	for _, who := range []TaskRequester{alice, bob, carol} {
		if !mayStop(rec, gchatBackend, who) {
			t.Errorf("%s asked for work here and was refused", who.Subject)
		}
	}
	if mayStop(rec, gchatBackend, dave) {
		t.Error("someone the allowlist admits, who asked for nothing here, may stop it")
	}
	if mayStop(rec, gchatBackend, TaskRequester{Backend: gchatBackend}) {
		t.Error("an author with no subject may stop it")
	}
	// The same subject on another backend is another person.
	if mayStop(rec, gchatBackend, TaskRequester{Backend: slackBackend, Subject: "h-alice"}) {
		t.Error("a requester's subject on another backend was taken for theirs")
	}
}

// When the record cannot tell who asked, anyone the gateway answers may press
// Stop, on Google Chat as on Slack.
func TestMayStopOnGoogleChatFailsOpenWhenTheRecordCannotTellWhoAsked(t *testing.T) {
	bob := TaskRequester{Backend: gchatBackend, Subject: "h-bob"}
	alice := TaskRequester{Backend: gchatBackend, Subject: "h-alice"}
	full := make([]TaskRef, taskHistoryCap)
	for i := range full {
		full[i] = TaskRef{ID: "task", Requester: &bob}
	}
	for name, rec := range map[string]*SessionRecord{
		"ran nothing":              {},
		"every requester aged out": {Tasks: []TaskRef{{ID: "task-1"}, {ID: "task-2"}}},
		"one requester pruned":     {Tasks: []TaskRef{{ID: "task-1"}, {ID: "task-2", Requester: &bob}}},
		"steer authors overflowed": {Tasks: []TaskRef{{ID: "task-1", Requester: &bob, SteerAuthorsOverflow: true}}},
		"session authors unknown":  {Tasks: []TaskRef{{ID: "task-1", Requester: &bob}}, SessionAuthorsUnknown: true},
		"history at its cap":       {Tasks: full},
	} {
		if !mayStop(rec, gchatBackend, alice) {
			t.Errorf("%s: the requester whose copy may be gone was refused", name)
		}
	}
	if mayStop(&SessionRecord{Tasks: []TaskRef{{ID: "task-1", Requester: &bob}}}, gchatBackend, alice) {
		t.Error("a complete record let a non-requester stop the thread")
	}
}

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
func gateChatRig(t *testing.T, conv string) (*rig, *lib.Envelope, string) {
	t.Helper()
	r := startGchatRig(t, []string{gateRequester, gateOther}, false)
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

// Another admitted member's press, on the running task or on a stale one,
// cancels nothing, edits nothing and posts nothing. Their follow-up after it
// is the sync point, and the requester's press on the same record then
// cancels, so it was the presser that was refused and not the press.
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
	// The steer's own acknowledgement is the one post since the ask; the
	// press itself neither edits a line nor posts.
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "cancel") || strings.Contains(e, staleStopNotice) {
			t.Fatalf("another member's press edited %q", e)
		}
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
