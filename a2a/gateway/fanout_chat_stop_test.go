package gateway

import (
	"context"
	"testing"
)

// startChatFanOutRig is a Google Chat gateway with sessions armed, so a
// session's batch delegation runs as it does on a chat backend, and a Stop
// press is the button's (isChatStopPress).
func startChatFanOutRig(t *testing.T) (*rig, *fakeSpawner) {
	t.Helper()
	spawn := &fakeSpawner{}
	r := startGchatRigWith(t, spawn, nil, true)
	return r, spawn
}

func chatStopPressOn(r *rig, conv, id, taskID string) {
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001",
		MessageID: id, Intent: IntentCancel, TaskID: taskID, Click: true}
}

// TestAChatStopPressOnAFanOutChildsLineStopsTheTree: once the delegating
// turn's line has closed, the children's lines carry the only Stop buttons.
// A press on a child still running is a stop of the whole tree: every live
// child is sent its cancel and marked canceled, and it isn't answered as a
// stale button.
func TestAChatStopPressOnAFanOutChildsLineStopsTheTree(t *testing.T) {
	t.Parallel()
	r, spawn := startChatFanOutRig(t)
	ctx := context.Background()
	conv := "gchat:spaces/S1/threads/T-fanout-press"
	f := fanOut(t, r, spawn, conv, platformTask("one"), platformTask("two"))

	chatStopPressOn(r, conv, "spaces/S1/messages/P1", f.kids[0].ID)
	for _, k := range f.kids {
		waitFor(t, "the cancel for "+k.ID, func() bool { return cancelSentTo(t, r, targetPlatform, k.ID) })
	}
	waitFor(t, "every child canceled on the record", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		for _, k := range f.kids {
			if ref, _ := rec.TaskRefFor(k.ID); !ref.Canceled {
				return false
			}
		}
		return true
	})
	if loggedContaining(r, "chat stop press on a task that has ended")() {
		t.Fatalf("the press on a running child was answered as a stale button:\n%s", r.logs.String())
	}
}

// TestAChatStopPressOnAnEndedFanOutChildCancelsNothing: a press on the line
// of a child that has already ended stays a stale button, answered on its own
// line, while its sibling keeps running.
func TestAChatStopPressOnAnEndedFanOutChildCancelsNothing(t *testing.T) {
	t.Parallel()
	r, spawn := startChatFanOutRig(t)
	ctx := context.Background()
	conv := "gchat:spaces/S1/threads/T-fanout-press-ended"
	f := fanOut(t, r, spawn, conv, platformTask("one"), platformTask("two"))
	completeTask(t, r.execFor(t, f.envs[0], targetPlatform), "one is done")
	childEndedLogged(t, r, f.kids[0].ID)

	chatStopPressOn(r, conv, "spaces/S1/messages/P2", f.kids[0].ID)
	waitFor(t, "the stale press answered", loggedContaining(r, "chat stop press on a task that has ended", f.kids[0].ID))
	if cancelSentTo(t, r, targetPlatform, f.kids[1].ID) {
		t.Fatal("a press on an ended child's line canceled its running sibling")
	}
	rec, _ := r.g.reg.Get(ctx, conv)
	if ref, _ := rec.TaskRefFor(f.kids[1].ID); ref.Canceled {
		t.Fatal("the running sibling is marked canceled")
	}
}
