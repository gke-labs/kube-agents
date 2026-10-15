package gateway

import (
	"context"
	"errors"
	"strconv"
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

// With no line to put it on, a submission that never reached the bus is
// posted, so the ask does not go unanswered.
func TestAStatusShowingBackendGetsTheBusFailurePosted(t *testing.T) {
	r, _ := startRigWithSpawnerAdapter(t, "platform", 0, nil, func(a *fakeAdapter) Adapter {
		return statusShowingAdapter{a}
	})
	deleteTasksStream(t, r.url)
	r.adapter.inbox <- InboundMessage{Conversation: "discord:g1/no-bus", Kind: "group", AuthorID: "1001", MessageID: "d-1", Text: "count me"}
	waitFor(t, "the bus failure posted", postedContaining(r, noticeBusUnreachable))
	if edits := r.adapter.editTexts(); len(edits) != 0 {
		t.Fatalf("edits = %q, want none: there is no line", edits)
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

// stubDoor is a door that is never called: ShowsTaskStatus answers for a
// door's conversation without asking it.
type stubDoor struct{ SideDoor }

// The side door answers for the primary's conversations and keeps the line
// for its own: an A2A or inject caller reads the line's edits as data.
func TestTheSideDoorKeepsTheLineForItsDoors(t *testing.T) {
	s := &sideDoorAdapter{
		primary: statusShowingAdapter{newFakeAdapter()},
		doors:   []DoorSpec{{Name: "inject", Prefix: "inject:", Door: stubDoor{}}},
	}
	if !s.ShowsTaskStatus("slack:C1/1.0") {
		t.Error("the primary's conversation: want the status shown, no rolling line")
	}
	if s.ShowsTaskStatus("inject:case-1") {
		t.Error("a door's conversation: want the rolling line kept")
	}
	if (&sideDoorAdapter{primary: newFakeAdapter()}).ShowsTaskStatus("discord:g/1") {
		t.Error("a primary that shows no status: want the rolling line kept")
	}
}

// A setStatus error is said at Warn once per text, so a missing scope is one
// line rather than one per event.
func TestASessionErrorIsWarnedOnce(t *testing.T) {
	s := &SlackAdapter{}
	scope, other := errors.New("missing_scope"), errors.New("ratelimited")
	if !s.firstSessionError(scope) || s.firstSessionError(scope) {
		t.Error("missing_scope: want the first warned and the second not")
	}
	if !s.firstSessionError(other) {
		t.Error("a different error: want it warned too")
	}
	for i := range slackSessionsCap {
		s.firstSessionError(errors.New("transient " + strconv.Itoa(i)))
	}
	if !s.firstSessionError(errors.New("a new one past the bound")) {
		t.Error("past the bound: want a new error still warned")
	}
}

// The Slack adapter shows the status once Agent Sessions is wired, and keeps
// the line without it.
func TestSlackShowsTaskStatusWithAgentSessions(t *testing.T) {
	if (&SlackAdapter{sessionsAPI: slackSessionsClient{}}).ShowsTaskStatus("slack:dm/D1") {
		t.Error("a conversation with no thread gets no Agent Session: want the rolling line kept")
	}
	if !(&SlackAdapter{sessionsAPI: slackSessionsClient{}}).ShowsTaskStatus("slack:C1/1700000000.000100") {
		t.Error("with Agent Sessions: want the status shown, no rolling line")
	}
	if (&SlackAdapter{}).ShowsTaskStatus("slack:C1/1700000000.000100") {
		t.Error("without Agent Sessions: want the rolling line kept")
	}
}
