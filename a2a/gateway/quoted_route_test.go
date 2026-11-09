package gateway

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A Google Chat quote-reply reaches the gateway as the member's words on
// Text and the composed ask beside them on Quoted (gchat.go, quotedAsk).
// routeTurn's matchers read the words, and the quoted ask is what reaches an
// agent (gateway.go, agentText and wordsAfter): a command the gateway
// answers without an agent is answered on the words alone, and whatever
// reaches an agent carries the quote. These drive the turn the real adapter
// composes, against a snapshot, through the gateway rig, so the frames are
// the adapter's own.

const (
	quotedRouteUser  = "u1@example.com"
	quotedRouteSpace = "gchat:spaces/S1/threads/T1"
	quotedRouteDM    = "gchat:dm/spaces/D1/threads/T1"
	quotedRouteQuote = "> Posted by Alertmanager:\n> " + gchatQuotedAlert
)

// quotedOntoAlert is the turn the Chat adapter hands over for words typed in
// a quote-reply to an Alertmanager post: in a space, mentioned, or at the
// top of a DM. Empty words are a bare quote.
func quotedOntoAlert(t *testing.T, words, id string, dm bool) InboundMessage {
	t.Helper()
	a := newTestGchatAdapterWithRelay(t, newFakeChatRelay(t))
	var ev *gchatEvent
	if dm {
		ev = gchatQuotedMsg("spaces/D1", "DIRECT_MESSAGE", "spaces/D1/threads/T1", id, words, "", gchatQuotedName)
	} else {
		ev = gchatQuotedMsg("spaces/S1", "SPACE", "spaces/S1/threads/T1", id, "@bot "+words, words, gchatQuotedName)
		if words == "" {
			empty := ""
			ev.Message.ArgumentText = &empty
		}
	}
	msg, reason := quotedTurn(a, withSnapshot(ev, "REPLY", "Alertmanager", gchatQuotedAlert))
	if reason != "" {
		t.Fatalf("the adapter dropped %q: %s", words, reason)
	}
	return msg
}

// quotedAskFor is the ask an agent reads for words quoted onto the alert:
// the words first, or the bare opening when there are none.
func quotedAskFor(words string) string {
	if words == "" {
		return fmt.Sprintf(gchatQuotedAsk, quotedRouteQuote)
	}
	return fmt.Sprintf(gchatQuotedAskWithWords, words, quotedRouteQuote)
}

// startQuotedRouteTask starts a task in conv on the gchat rig and reports it
// working, so the next message there meets a running task.
func startQuotedRouteTask(t *testing.T, r *rig, conv string) *lib.Envelope {
	t.Helper()
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: quotedRouteUser, MessageID: "spaces/S1/messages/M0", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	if err := r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	return origin
}

// A quote-reply "stop" on a running task is the stop: the task is cancelled
// and nothing of the quote reaches it.
func TestQuotedStopOnARunningTaskStopsIt(t *testing.T) {
	r := startGchatRig(t, []string{quotedRouteUser}, false)
	origin := startQuotedRouteTask(t, r, quotedRouteSpace)
	r.adapter.inbox <- quotedOntoAlert(t, "stop", "spaces/S1/messages/M1", false)
	waitFor(t, "the cancel on the running task", func() bool {
		return slices.ContainsFunc(inSubjectEnvelopes(t, r.url, "platform"), func(e *lib.Envelope) bool {
			return e.TaskID == origin.TaskID && e.Kind == lib.KindCancel
		})
	})
	if n := messagesOn(t, r, "platform"); n != 1 {
		t.Fatalf("%d messages on platform, want the task alone: a quoted stop reached the agent", n)
	}
}

// A quote-reply status phrase on a running task is answered as a status ask,
// from the stream, and reaches no agent.
func TestQuotedStatusOnARunningTaskIsAnsweredAsStatus(t *testing.T) {
	r := startGchatRig(t, []string{quotedRouteUser}, false)
	startQuotedRouteTask(t, r, quotedRouteSpace)
	r.adapter.inbox <- quotedOntoAlert(t, "status", "spaces/S1/messages/M1", false)
	awaitPostIn(t, r, "the replayed status card", quotedRouteSpace, "🎯 on: “check the fleet”")
	if n := messagesOn(t, r, "platform"); n != 1 {
		t.Fatalf("%d messages on platform, want the task alone: a quoted status ask reached the agent", n)
	}
}

// The same phrase in a space with nothing running is no status ask routeTurn
// answers, so it reaches the agent as a task, and the task carries the quote
// rather than reading "status".
func TestQuotedStatusInASpaceWithNothingRunningKeepsItsQuote(t *testing.T) {
	for i, words := range []string{"status", "any update?"} {
		t.Run(words, func(t *testing.T) {
			r := startGchatRig(t, []string{quotedRouteUser}, false)
			r.adapter.inbox <- quotedOntoAlert(t, words, fmt.Sprintf("spaces/S1/messages/M%d", i), false)
			if got, want := envText(t, awaitSubmission(t, r, "platform", 0)), quotedAskFor(words); got != want {
				t.Errorf("the task reads %q\nwant %q", got, want)
			}
		})
	}
}

// At the top of a DM with nothing running the phrase is answered as a status
// ask, with the redirect, and starts no task.
func TestQuotedStatusAtADMTopLevelIsAnsweredAsStatus(t *testing.T) {
	r := startGchatRig(t, []string{quotedRouteUser}, false)
	msg := quotedOntoAlert(t, "status", "spaces/D1/messages/M1", true)
	if !msg.TopLevel || msg.Conversation != quotedRouteDM {
		t.Fatalf("turn = %+v, want a top-level DM turn in %s", msg, quotedRouteDM)
	}
	r.adapter.inbox <- msg
	awaitPostIn(t, r, "the redirect in the status question's own thread", quotedRouteDM, topLevelNothingRunningNotice)
	if n := messagesOn(t, r, "platform"); n != 0 {
		t.Fatalf("%d messages on platform, want none: a top-level quoted status ask started a task", n)
	}
}

// A bare quote has no words for any matcher to read, and is dropped nowhere
// on the way: with nothing running it is a task that reads the quoted ask,
// and on a running task a steer that does.
func TestBareQuoteReachesTheAgentWhole(t *testing.T) {
	r := startGchatRig(t, []string{quotedRouteUser}, false)
	r.adapter.inbox <- quotedOntoAlert(t, "", "spaces/S1/messages/M1", false)
	origin := awaitSubmission(t, r, "platform", 0)
	if got, want := envText(t, origin), quotedAskFor(""); got != want {
		t.Errorf("the task reads %q\nwant %q", got, want)
	}
	if err := r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- quotedOntoAlert(t, "", "spaces/S1/messages/M2", false)
	steer := awaitSubmission(t, r, "platform", 1)
	if steer.TaskID != origin.TaskID {
		t.Fatalf("the bare quote started task %s instead of steering %s", steer.TaskID, origin.TaskID)
	}
	if got, want := envText(t, steer), quotedAskFor(""); got != want {
		t.Errorf("the steer reads %q\nwant %q", got, want)
	}
}

// Text after "/session" or "/feedback" is the agent's turn, and carries the
// quote: the session's first turn reads the text with the quote, the command
// word off its head; the feedback text goes over as typed, quote and all.
func TestQuotedSessionAndFeedbackTextReachTheAgentWithTheQuote(t *testing.T) {
	spawn := &fakeSpawner{}
	r := startGchatRigWith(t, spawn, []string{quotedRouteUser}, false)
	r.adapter.inbox <- quotedOntoAlert(t, "/session what is this alert?", "spaces/S1/messages/M1", false)
	waitFor(t, "the session spawn", func() bool { return len(spawn.calls()) == 1 })
	session := spawn.calls()[0].Session
	if got, want := envText(t, r.awaitTask(t, session)), quotedAskFor("what is this alert?"); got != want {
		t.Errorf("the session's first turn reads %q\nwant %q", got, want)
	}

	feedback := "gchat:spaces/S1/threads/T2"
	msg := quotedOntoAlert(t, "/feedback the alert text is wrong", "spaces/S1/messages/M2", false)
	msg.Conversation = feedback
	r.adapter.inbox <- msg
	if got, want := envText(t, awaitSubmission(t, r, "platform", 0)), quotedAskFor("/feedback the alert text is wrong"); got != want {
		t.Errorf("the feedback turn reads %q\nwant %q", got, want)
	}
	if slices.Contains(r.adapter.postTextsFor(feedback), feedbackCommandNotice) {
		t.Error("feedback text beside a quote was answered as the bare /feedback")
	}
}

// "delegate <task>" spawns a worker whose task is <task> with the quote, the
// word off its head. A bare "delegate" is no delegation (chattext.IsDelegate
// refuses it) and is a task like any other words, so it keeps its quote
// rather than reaching the agent as the one word.
func TestQuotedDelegateKeepsItsTaskAndABareDelegateItsQuote(t *testing.T) {
	spawn := &fakeSpawner{}
	r := startGchatRigWith(t, spawn, []string{quotedRouteUser}, false)
	r.adapter.inbox <- quotedOntoAlert(t, "delegate check node-3", "spaces/S1/messages/M1", false)
	waitFor(t, "the worker spawn", func() bool { return len(spawn.calls()) == 1 })
	worker := spawn.calls()[0].Session
	if got, want := envText(t, r.awaitTask(t, worker)), quotedAskFor("check node-3"); got != want {
		t.Errorf("the worker's task reads %q\nwant %q", got, want)
	}

	bare := quotedOntoAlert(t, "delegate", "spaces/S1/messages/M2", false)
	bare.Conversation = "gchat:spaces/S1/threads/T2"
	r.adapter.inbox <- bare
	if got, want := envText(t, awaitSubmission(t, r, "platform", 0)), quotedAskFor("delegate"); got != want || strings.TrimSpace(got) == "delegate" {
		t.Errorf("the bare delegate's task reads %q\nwant %q", got, want)
	}
	if got := len(spawn.calls()); got != 1 {
		t.Errorf("a bare delegate spawned a worker: %d spawns", got)
	}
}
