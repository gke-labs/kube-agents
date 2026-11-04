package gateway

import (
	"context"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The Google Chat adapter keys every DM message by its thread and marks a
// top-level one (gchat.go), so a Google Chat DM gets the same top-level
// notices a Slack DM gets: the gateway keys them on the mark, not on the
// backend.

const (
	gchatDMUser         = "u1@example.com"
	gchatDMQuestion     = "gchat:dm/spaces/D1/threads/T1"
	gchatDMSecondThread = "gchat:dm/spaces/D1/threads/T2"
	gchatDMThirdThread  = "gchat:dm/spaces/D1/threads/T3"
	gchatDMCancelNotice = "cancel sent"
	// gchatDMWholeSpace is the thread-less DM key a record from before the
	// thread-per-ask rule, or a gateway-opened DM, carries.
	gchatDMWholeSpace     = "gchat:dm/spaces/D1"
	gchatDMSessionThread  = "reply in this thread"
	gchatDMSessionNextMsg = "your next message opens a session pod"
)

// gchatDM sends one Google Chat DM message on conv.
func gchatDM(r *rig, conv, id, text string, topLevel bool) {
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: sessionKindDM, AuthorID: gchatDMUser,
		MessageID: id, Text: text, TopLevel: topLevel,
	}
}

// awaitPostIn waits for a post into conv whose text contains want.
func awaitPostIn(t *testing.T, r *rig, what, conv, want string) {
	t.Helper()
	waitFor(t, what, func() bool {
		return slices.ContainsFunc(r.adapter.postTextsFor(conv), func(p string) bool {
			return strings.Contains(p, want)
		})
	})
}

// TestGchatDMTopLevelStopPointsAtTheThread: a "stop" typed at the top of a
// Google Chat DM is a conversation of its own, so it cannot reach a
// question's task; it says where the stop goes and cancels nothing. A stop
// typed inside the question's thread reaches the question's task.
func TestGchatDMTopLevelStopPointsAtTheThread(t *testing.T) {
	r := startGchatRig(t, []string{gchatDMUser}, false)
	ctx := context.Background()
	gchatDM(r, gchatDMQuestion, "spaces/D1/messages/M1", "how is the fleet", true)
	first := awaitSubmission(t, r, "platform", 0)
	exec := r.execFor(t, first, "platform")
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}

	gchatDM(r, gchatDMSecondThread, "spaces/D1/messages/M2", "stop", true)
	awaitPostIn(t, r, "the notice in the stop's own thread", gchatDMSecondThread, topLevelNothingRunningNotice)
	for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
		if e.TaskID == first.TaskID && e.Kind != lib.KindMessage {
			t.Errorf("a top-level stop sent %s to the question's task", e.Kind)
		}
	}
	if n := messagesOn(t, r, "platform"); n != 1 {
		t.Fatalf("%d messages on platform, want the question alone: a top-level stop started a task", n)
	}

	gchatDM(r, gchatDMQuestion, "spaces/D1/messages/M3", "stop", false)
	awaitPostIn(t, r, "the cancel in the question's thread", gchatDMQuestion, gchatDMCancelNotice)
	waitFor(t, "the cancel on the question's task", func() bool {
		return slices.ContainsFunc(inSubjectEnvelopes(t, r.url, "platform"), func(e *lib.Envelope) bool {
			return e.TaskID == first.TaskID && e.Kind == lib.KindCancel
		})
	})
	if slices.Contains(r.adapter.postTextsFor(gchatDMQuestion), topLevelNothingRunningNotice) {
		t.Errorf("a stop in the question's own thread was redirected to it")
	}
}

// TestGchatDMTopLevelStatusPointsAtTheThread: a status question typed at the
// top of a Google Chat DM has nothing running in its conversation. It gets
// the redirect a top-level stop gets and starts no task. A top-level real
// question shaped like the wide status match still starts a task.
func TestGchatDMTopLevelStatusPointsAtTheThread(t *testing.T) {
	r := startGchatRig(t, []string{gchatDMUser}, false)
	ctx := context.Background()
	gchatDM(r, gchatDMQuestion, "spaces/D1/messages/M1", "how is the fleet", true)
	first := awaitSubmission(t, r, "platform", 0)
	if err := r.execFor(t, first, "platform").PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}

	gchatDM(r, gchatDMSecondThread, "spaces/D1/messages/M2", "any update?", true)
	awaitPostIn(t, r, "the redirect in the status question's own thread", gchatDMSecondThread, topLevelNothingRunningNotice)
	if n := messagesOn(t, r, "platform"); n != 1 {
		t.Fatalf("a top-level status question started a task: %d messages on platform, want 1", n)
	}

	gchatDM(r, gchatDMThirdThread, "spaces/D1/messages/M3", "how is the deploy doing", true)
	awaitSubmission(t, r, "platform", 1)
	if slices.Contains(r.adapter.postTextsFor(gchatDMThirdThread), topLevelNothingRunningNotice) {
		t.Errorf("a top-level question was redirected as a status poke")
	}
}

// TestGchatDMSessionOnAckNamesTheThreadOnlyWhenTheRecordHasOne: a bare
// `/session` in a Google Chat DM whose key names a thread points the user at
// that thread; on a thread-less DM key it says the next message opens the
// pod and names no thread.
func TestGchatDMSessionOnAckNamesTheThreadOnlyWhenTheRecordHasOne(t *testing.T) {
	r := startGchatRigWith(t, &fakeSpawner{}, []string{gchatDMUser}, false)

	gchatDM(r, gchatDMQuestion, "spaces/D1/messages/M1", "/session", true)
	awaitPostIn(t, r, "the ack in the command's thread", gchatDMQuestion, gchatDMSessionThread)

	gchatDM(r, gchatDMWholeSpace, "spaces/D1/messages/M2", "/session", false)
	awaitPostIn(t, r, "the ack on the thread-less DM", gchatDMWholeSpace, gchatDMSessionNextMsg)
	if slices.ContainsFunc(r.adapter.postTextsFor(gchatDMWholeSpace), func(p string) bool {
		return strings.Contains(p, gchatDMSessionThread)
	}) {
		t.Errorf("the ack on a thread-less DM key names a thread it does not have")
	}
}
