package gateway

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// statusShowingAdapter is the fake as a backend that shows a task's status
// itself (StatusShower), as the Slack adapter does with Agent Sessions.
type statusShowingAdapter struct{ *fakeAdapter }

func (statusShowingAdapter) ShowsTaskStatus(string) bool { return true }

// A backend that shows a task's status itself gets no rolling line: no
// "⏳ submitted…" placeholder, no progress edit and no "✅ completed" edit,
// and the answer is the task's one post.
func TestAStatusShowingBackendGetsNoRollingLine(t *testing.T) {
	t.Parallel()
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
	t.Parallel()
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

// statusShowingProfileRig is a spawner rig on a status-showing backend
// whose session can delegate to a pinned cluster profile.
func statusShowingProfileRig(t *testing.T) (*rig, *fakeSpawner) {
	t.Helper()
	r, spawn := startRigWithSpawnerAdapter(t, "platform", 0, nil, func(a *fakeAdapter) Adapter {
		return statusShowingAdapter{a}
	})
	scope := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scope, []byte(`{"present":true,"projects":["p1"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	r.g.cfg.ScopeFile = scope
	r.g.profiles.put("prod-east", &lib.AgentCard{Name: "prod-east", Parameters: clusterParams,
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "prod"}})
	return r, spawn
}

// A task delegated to a profile keeps its rolling line on a backend that
// shows task status itself (#6532): that status never carries a profile
// worker's progress, so without the line the room sees nothing from the
// child until its answer. The line moves with the child's events.
func TestAProfileChildKeepsItsLineOnAStatusShowingBackend(t *testing.T) {
	t.Parallel()
	r, spawn := statusShowingProfileRig(t)
	ctx := context.Background()
	exec, _, _ := sessionTurn(t, r, spawn, "discord:g1/t-profile-line", "why is prod unhappy?")
	if err := exec.PublishArtifact(ctx, delegateArtifactWithParameters(t, "prod-east", "diagnose prod", targetParams("p1", "us-east1", "prod"))); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the child's line", postedContaining(r, "(delegated to prod-east)"))
	var child *lib.Envelope
	waitFor(t, "the child's submission", func() bool {
		if got := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"prod-east.>"); len(got) == 1 {
			child = got[0].env
			return true
		}
		return false
	})
	cexec := r.execFor(t, child, "prod-east")
	if err := cexec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the child's line at working", func() bool {
		for _, e := range r.adapter.editTexts() {
			if strings.Contains(e, "working") && strings.Contains(e, "(delegated to prod-east)") {
				return true
			}
		}
		return false
	})
	// Done, the line closes to the bare mark, not a "completed" header left
	// above the answer for good.
	if err := cexec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "prod is fine"}}}); err != nil {
		t.Fatal(err)
	}
	if err := cexec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	closed := withLineNote(lineDoneMark, "(delegated to prod-east)")
	waitFor(t, "the child's line closed to the bare mark", func() bool {
		return slices.Contains(r.adapter.editTexts(), closed)
	})
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "completed") {
			t.Fatalf("edits = %q, want the line closed to %q", r.adapter.editTexts(), closed)
		}
	}
}

// A profile child's progress carries the ask's kind as a mark-only chat
// message (the worker adapter's shape, #6536): its line still shows the
// progress text with the delegation note. (TestMarkOnlyProgressLaysNothingOut
// pins that the message lays nothing out.)
func TestAProfileChildsMarkOnlyProgressKeepsItsLineText(t *testing.T) {
	t.Parallel()
	r, spawn := statusShowingProfileRig(t)
	ctx := context.Background()
	exec, _, _ := sessionTurn(t, r, spawn, "discord:g1/t-profile-mark", "why is prod unhappy?")
	if err := exec.PublishArtifact(ctx, delegateArtifactWithParameters(t, "prod-east", "diagnose prod", targetParams("p1", "us-east1", "prod"))); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the child's line", postedContaining(r, "(delegated to prod-east)"))
	var child *lib.Envelope
	waitFor(t, "the child's submission", func() bool {
		if got := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"prod-east.>"); len(got) == 1 {
			child = got[0].env
			return true
		}
		return false
	})
	cexec := r.execFor(t, child, "prod-east")
	if err := cexec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	markOnly := chatmsg.Message{Schema: chatmsg.Schema, Mark: &chatmsg.Mark{Ask: "investigate"}}
	if err := cexec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "checking the pods"}, chatPart(t, markOnly)}}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the child's line showing the progress text", func() bool {
		for _, e := range r.adapter.editTexts() {
			if strings.Contains(e, "checking the pods") && strings.Contains(e, "(delegated to prod-east)") {
				return true
			}
		}
		return false
	})
}

// Only a single child a session turn delegates to a profile keeps a line
// where the backend shows task status itself: a child of a profile task
// would put a second live line beside its parent's.
func TestOnlyASessionTurnsProfileChildKeepsALine(t *testing.T) {
	t.Parallel()
	rec := &SessionRecord{Tasks: []TaskRef{{ID: "t-session"}, {ID: "t-profile", Tier: "read"}}}
	route := &profileRoute{Profile: "prod-east"}
	for _, tc := range []struct {
		name string
		ts   taskStart
		want bool
	}{
		{"a session turn's profile child", taskStart{Role: taskRoleChild, ParentTaskID: "t-session", Route: route}, true},
		{"a profile task's profile child", taskStart{Role: taskRoleChild, ParentTaskID: "t-profile", Route: route}, false},
		{"a child to platform", taskStart{Role: taskRoleChild, ParentTaskID: "t-session"}, false},
		{"a batch child", taskStart{Role: taskRoleChild, ParentTaskID: "t-session", Route: route, KeepActive: true}, false},
		{"a human turn", taskStart{Route: route}, false},
		{"a wake", taskStart{Role: taskRoleWake, ParentTaskID: "t-session"}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := childKeepsLine(rec, tc.ts); got != tc.want {
				t.Fatalf("childKeepsLine = %v, want %v", got, tc.want)
			}
		})
	}
}

// A child delegated to platform keeps the backend's own status, as a
// platform task does: no line of its own.
func TestAPlatformChildGetsNoLineOnAStatusShowingBackend(t *testing.T) {
	t.Parallel()
	r, spawn := statusShowingProfileRig(t)
	ctx := context.Background()
	exec, _, _ := sessionTurn(t, r, spawn, "discord:g1/t-platform-line", "how is the fleet?")
	if err := exec.PublishArtifact(ctx, delegateArtifact(t, "platform", "check the fleet")); err != nil {
		t.Fatal(err)
	}
	child := r.awaitTask(t, "platform")
	// The delegating turn ends, as a session's does once it has delegated.
	if err := exec.PublishArtifact(ctx, lib.Artifact{ArtifactID: "a-r", Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "delegated to platform"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the delegating turn's answer", postedContaining(r, "delegated to platform"))
	cexec := r.execFor(t, child, "platform")
	if err := cexec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := cexec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "the fleet is fine"}}}); err != nil {
		t.Fatal(err)
	}
	if err := cexec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	// The child's end wakes its session; the relay takes a task's events in
	// order, so by the wake its working event has been through the relay.
	waitFor(t, "the session woken by the child's end", func() bool {
		rec, err := r.g.reg.Get(ctx, "discord:g1/t-platform-line")
		if err != nil || rec == nil || rec.ActiveTask == nil {
			return false
		}
		ref, ok := rec.TaskRefFor(rec.ActiveTask.TaskID)
		return ok && ref.Role == taskRoleWake
	})
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "submitted") {
			t.Fatalf("posts = %q, want no line for a platform child", r.adapter.postTexts())
		}
	}
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "working") {
			t.Fatalf("edits = %q, want no line for a platform child", r.adapter.editTexts())
		}
	}
}
