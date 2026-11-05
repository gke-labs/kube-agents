package gateway

import (
	"strings"
	"testing"

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
