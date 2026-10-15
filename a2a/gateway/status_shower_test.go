package gateway

import (
	"context"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// statusShowingAdapter is the fake as a backend that shows a task's status
// itself (StatusShower), as the Slack adapter does with Agent Sessions.
type statusShowingAdapter struct{ *fakeAdapter }

func (statusShowingAdapter) ShowsTaskStatus() bool { return true }

// A backend that shows a task's status itself gets no rolling line: no
// "⏳ submitted…" placeholder, no progress edit and no "✅ completed" edit,
// and the answer is the task's one post.
func TestAStatusShowingBackendGetsNoRollingLine(t *testing.T) {
	r, _ := startRigWithSpawnerAdapter(t, "platform", 0, nil, func(a *fakeAdapter) Adapter {
		return statusShowingAdapter{a}
	})
	conv := "discord:g1/thread-lineless"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-1", Text: "do the thing"}

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	for _, state := range []lib.TaskState{lib.StateSubmitted, lib.StateWorking} {
		if err := exec.PublishStatus(ctx, state, false); err != nil {
			t.Fatal(err)
		}
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "reading the fleet"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "the fleet is fine"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "result post", postedContaining(r, "the fleet is fine"))
	if posts := r.adapter.postTexts(); len(posts) != 1 {
		t.Fatalf("posts = %q, want the answer alone", posts)
	}
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "completed") || strings.Contains(e, "working") {
			t.Fatalf("edits = %q, want no rolling line", r.adapter.editTexts())
		}
	}
}

// The Slack adapter shows the status once Agent Sessions is wired, and keeps
// the line without it.
func TestSlackShowsTaskStatusWithAgentSessions(t *testing.T) {
	if !(&SlackAdapter{sessionsAPI: slackSessionsClient{}}).ShowsTaskStatus() {
		t.Error("with Agent Sessions: want the status shown, no rolling line")
	}
	if (&SlackAdapter{}).ShowsTaskStatus() {
		t.Error("without Agent Sessions: want the rolling line kept")
	}
}
