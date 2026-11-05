package gateway

import (
	"context"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A task's live message carries OutMessage.Stop from the placeholder through
// every live edit, text or laid out, and the terminal edit arrives without
// it: a backend that draws a Stop control draws it while the task runs and
// takes it off when the task ends.
func TestTheLiveLineCarriesStopUntilTheTerminal(t *testing.T) {
	r := startRig(t)
	origin := startChatTask(t, r, "discord:g1/stop-line", true)
	waitFor(t, "the working line with Stop", func() bool {
		lines := r.adapter.stopLinesCopy()
		return len(lines) >= 2 && strings.Contains(lines[0].Text, "submitted") && strings.Contains(lines[len(lines)-1].Text, "working")
	})
	publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "reading versions"}, chatPart(t, testPlan)}, false, false)
	waitFor(t, "the laid-out line with Stop", func() bool {
		edits := r.adapter.chatEditsCopy()
		return len(edits) > 0 && edits[len(edits)-1] != nil
	})
	laidOut := len(r.adapter.stopLinesCopy())
	completeTask(t, r.execFor(t, origin, "platform"), "done")
	waitFor(t, "the terminal edit", func() bool {
		texts := r.adapter.editTexts()
		return len(texts) > 0 && strings.Contains(texts[len(texts)-1], "completed")
	})
	lines := r.adapter.stopLinesCopy()
	if len(lines) < 3 || laidOut < 3 {
		t.Fatalf("stop lines = %+v (laid out at %d)", lines, laidOut)
	}
	for _, l := range lines {
		if l.Conversation != "discord:g1/stop-line" || strings.Contains(l.Text, "completed") {
			t.Errorf("the terminal edit, or another conversation's line, carried Stop: %+v", l)
		}
	}
}

// A task the heal ends (here one that never started inside the grace) ends
// its live message too: the terminal line, with no Stop on it, so a line
// that read working with a Stop control does not outlive its task.
func TestTheHealEndsTheLiveLineWithoutStop(t *testing.T) {
	r, spawn := startRigWithSpawnerCap(t, RouteSession, 0, func(c *Config) {
		c.SessionReuse = true
		c.FirstEventGrace = time.Second
	})
	ctx := context.Background()
	pod := "chat-heron-line"
	rec := &SessionRecord{Key: "discord:g1/t-heal-line", ContextID: "ctx-hl", Kind: "group",
		Addressee: pod, BusSession: pod, PodName: pod, PodReuse: true, SessionRouted: true, Profile: "chat",
		ActiveTask: &ActiveTask{TaskID: "task-hl", CorrelationID: "corr-hl", StatusMsgID: "m-stale", LineNote: "(note)", SubmittedAt: time.Now().Add(-time.Minute)},
		Tasks:      []TaskRef{{ID: "task-hl", Addressee: pod}}}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	spawn.setReusable(pod)
	say(r, rec.Key, "m1", "hello?")
	waitFor(t, "the stale line ended", func() bool {
		r.adapter.mu.Lock()
		edits := append([]fakePost(nil), r.adapter.edits...)
		r.adapter.mu.Unlock()
		for _, e := range edits {
			if e.MessageID == "m-stale" && strings.Contains(e.Text, "failed") && strings.HasSuffix(e.Text, "(note)") {
				return true
			}
		}
		return false
	})
	for _, l := range r.adapter.stopLinesCopy() {
		if l.MessageID == "m-stale" {
			t.Fatalf("the heal's terminal edit carried Stop: %+v", l)
		}
	}
}

// A stopped task whose terminal has not arrived when the next ask starts
// keeps its line otherwise, with its Stop control on it: the new turn
// closes it as canceled, without Stop, and the new task's line carries the
// control instead.
func TestANewTurnClosesASupersededStoppedLine(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/superseded-line"
	startChatTask(t, r, conv, false)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-stop", Text: "stop"}
	waitFor(t, "the cancel notice", func() bool {
		return slices.ContainsFunc(r.adapter.postTexts(), func(s string) bool { return strings.Contains(s, "cancel sent") })
	})
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-2", Text: "and now the nodes"}
	r.awaitTask(t, "platform")
	first := r.adapter.stopLinesCopy()[0]
	waitFor(t, "the old line closed", func() bool {
		r.adapter.mu.Lock()
		defer r.adapter.mu.Unlock()
		for _, e := range r.adapter.edits {
			if e.MessageID == first.MessageID && strings.Contains(e.Text, "canceled") {
				return true
			}
		}
		return false
	})
	lines := r.adapter.stopLinesCopy()
	last := lines[len(lines)-1]
	if last.MessageID == first.MessageID || !strings.Contains(last.Text, "submitted") {
		t.Fatalf("stop lines = %+v; want the new task's placeholder last", lines)
	}
	for _, l := range lines {
		if l.MessageID == first.MessageID && strings.Contains(l.Text, "canceled") {
			t.Fatalf("the closing edit carried Stop: %+v", l)
		}
	}
}
