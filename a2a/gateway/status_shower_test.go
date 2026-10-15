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

func (statusShowingAdapter) ShowsTaskStatus(string) bool { return true }

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

// Behind the mux, as a real install runs it (Slack beside the console), the
// answer is the owning backend's: a Slack conversation shows no line, a
// console one keeps it, and an unknown prefix keeps it.
func TestTheMuxAnswersStatusShowingForTheOwningBackend(t *testing.T) {
	mux, err := NewMultiAdapter("slack", "console", map[string]Adapter{
		"slack":   statusShowingAdapter{newFakeAdapter()},
		"console": newFakeAdapter(),
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !mux.ShowsTaskStatus("slack:C1/1.0") {
		t.Error("a Slack conversation: want the status shown, no rolling line")
	}
	if mux.ShowsTaskStatus("console:abc") || mux.ShowsTaskStatus("discord:g/1") {
		t.Error("a console or unknown conversation: want the rolling line kept")
	}
	var _ StatusShower = mux
}

// The Slack adapter shows the status once Agent Sessions is wired, and keeps
// the line without it.
func TestSlackShowsTaskStatusWithAgentSessions(t *testing.T) {
	if !(&SlackAdapter{sessionsAPI: slackSessionsClient{}}).ShowsTaskStatus("slack:C1") {
		t.Error("with Agent Sessions: want the status shown, no rolling line")
	}
	if (&SlackAdapter{}).ShowsTaskStatus("slack:C1") {
		t.Error("without Agent Sessions: want the rolling line kept")
	}
}
