package gateway

import (
	"context"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// cancelDoorTaskWith runs one door turn to working, cancels it through the
// door, and has the executor end it canceled after publishing a result made
// of parts (none when parts is nil). It returns the task as tasks/get shows
// it once canceled.
func cancelDoorTaskWith(t *testing.T, parts []lib.Part) a2aTaskObject {
	t.Helper()
	r := startA2ARig(t)
	ctx := context.Background()
	task := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("scale checkout-gateway to 4", "m-1", "", false)))
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	r.getUntil(t, a2aTestCaller, task.ID, "the working event to reach the door", func(task a2aTaskObject) bool {
		return task.Status.State == lib.StateWorking
	})
	if resp := r.rpc(t, a2aTestCaller, a2aMethodCancel, map[string]any{"id": task.ID}); resp.Error != nil {
		t.Fatalf("the cancel was refused: %d %s", resp.Error.Code, resp.Error.Message)
	}
	if parts != nil {
		if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: parts}); err != nil {
			t.Fatal(err)
		}
	}
	if err := exec.PublishStatus(ctx, lib.StateCanceled, true); err != nil {
		t.Fatal(err)
	}
	return r.getUntil(t, a2aTestCaller, task.ID, "the canceled terminal to reach the door", func(task a2aTaskObject) bool {
		return task.Status.State == lib.StateCanceled
	})
}

func cardStopResultPartFor(t *testing.T, ids ...string) lib.Part {
	t.Helper()
	res := &lib.CardStopResult{}
	for _, id := range ids {
		res.Stopped = append(res.Stopped, lib.CardStopCard{ID: id, Title: "scale checkout-gateway"})
	}
	part, err := lib.CardStopPart(lib.CardStop{CardStopResult: res})
	if err != nil {
		t.Fatal(err)
	}
	return part
}

// TestA2ACanceledTaskShowsItsCardStopResult: a cancel that stopped the
// turn's cards ends with the executor's card-stop result, and the door's
// caller reads it on the canceled task: the text, and the cardStop part the
// card-stop control returns, so one schema serves both (#5528).
func TestA2ACanceledTaskShowsItsCardStopResult(t *testing.T) {
	got := cancelDoorTaskWith(t, []lib.Part{
		{Kind: "text", Text: "canceled; stopped 1 card"},
		cardStopResultPartFor(t, "t_1"),
	})
	if len(got.Artifacts) != 1 {
		t.Fatalf("the canceled task has %d artifacts, want its card-stop result", len(got.Artifacts))
	}
	art := got.Artifacts[0]
	if text := joinTextParts(art.Parts); !strings.HasPrefix(text, "canceled; ") || !strings.Contains(text, "stopped 1 card") {
		t.Errorf("the artifact's text = %q, want the gateway's card-stop text", text)
	}
	res, ok := cardStopResultOf(art.Parts)
	if !ok {
		t.Fatalf("the artifact carries no cardStop result part: %+v", art.Parts)
	}
	if len(res.Stopped) != 1 || res.Stopped[0].ID != "t_1" {
		t.Errorf("the cardStop part's stopped cards = %+v, want t_1", res.Stopped)
	}
}

// TestA2ACanceledCardStopCarriesNoPartialAnswer: a canceled turn that had
// published part of its answer into the result before the card-stop chunk
// shows only the card-stop result. The gateway writes the text itself, so
// the partial answer chunked into the same artifact never reaches the door.
func TestA2ACanceledCardStopCarriesNoPartialAnswer(t *testing.T) {
	got := cancelDoorTaskWith(t, []lib.Part{
		{Kind: "text", Text: "Scaling checkout-gateway now; the first replica"},
		{Kind: "text", Text: "canceled; stopped 1 card"},
		cardStopResultPartFor(t, "t_1"),
	})
	if len(got.Artifacts) != 1 {
		t.Fatalf("the canceled task has %d artifacts, want its card-stop result", len(got.Artifacts))
	}
	if text := joinTextParts(got.Artifacts[0].Parts); strings.Contains(text, "first replica") || strings.Contains(text, "Scaling") {
		t.Errorf("the partial answer reached the door: %q", text)
	}
	if n := len(got.Artifacts[0].Parts); n != 2 {
		t.Errorf("%d parts, want the text and the cardStop part", n)
	}
}

// TestA2ACanceledTaskShowsNoOtherResult: a canceled turn's other output,
// a partial answer, or a cardStop part that is a request rather than a
// result, stays away from the door's caller, as it did before card-stop
// results were shown. Only a completed task delivers a result.
func TestA2ACanceledTaskShowsNoOtherResult(t *testing.T) {
	request, err := lib.CardStopPart(lib.CardStop{})
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name  string
		parts []lib.Part
	}{
		{"a partial answer", []lib.Part{{Kind: "text", Text: "Scaling checkout-gateway now; the first replica"}}},
		{"a card-stop request", []lib.Part{{Kind: "text", Text: "stop the cards"}, request}},
		{"other data", []lib.Part{{Kind: "text", Text: "partial"}, {Kind: "data", Data: []byte(`{"chat":{"v":1}}`)}}},
		{"no result", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := cancelDoorTaskWith(t, tc.parts); len(got.Artifacts) != 0 {
				t.Errorf("the canceled task shows %d artifacts (%+v), want none", len(got.Artifacts), got.Artifacts)
			}
		})
	}
}

// TestA2ACardStopResultIsBounded: a card-stop result past the deliverable
// bound keeps its text, cut, and drops the part rather than handing over
// JSON cut mid-way.
func TestA2ACardStopResultIsBounded(t *testing.T) {
	d := bareDoor(t)
	key := a2aConversationKey(a2aTestCaller, "big-stop")
	d.TaskStarted(key, "task-stop")
	big := make([]byte, a2aMaxResultBytes+64)
	for i := range big {
		big[i] = 'x'
	}
	d.TaskCardStop(key, "task-stop", string(big), cardStopResultPartFor(t, "t_1"))
	d.TaskTerminal(key, "task-stop", lib.StateCanceled, TerminalFromExecutor, "reason: canceled-by-request")
	obj := d.taskObject("task-stop")
	if len(obj.Artifacts) != 1 {
		t.Fatalf("%d artifacts, want the cut card-stop text", len(obj.Artifacts))
	}
	if _, ok := cardStopResultOf(obj.Artifacts[0].Parts); ok {
		t.Error("an over-bound card-stop result kept its part")
	}
	if n := len(joinTextParts(obj.Artifacts[0].Parts)); n > a2aMaxResultBytes+len("…") || n >= len(big) {
		t.Errorf("the card-stop text is %d bytes, not cut to the bound", n)
	}
	if _, cut := obj.Metadata["resultTruncatedFrom"]; !cut {
		t.Error("a cut card-stop result is not marked resultTruncatedFrom")
	}
}

// TestA2ACardStopNotShownUnlessCanceled: a card-stop result recorded for a
// task that then ends some other way renders nothing of it.
func TestA2ACardStopNotShownUnlessCanceled(t *testing.T) {
	d := bareDoor(t)
	key := a2aConversationKey(a2aTestCaller, "stop-failed")
	d.TaskStarted(key, "task-f")
	d.TaskCardStop(key, "task-f", "canceled; stopped 1 card", cardStopResultPartFor(t, "t_1"))
	d.TaskTerminal(key, "task-f", lib.StateFailed, TerminalFromExecutor, "reason: x")
	if obj := d.taskObject("task-f"); len(obj.Artifacts) != 0 {
		t.Errorf("a failed task shows %d artifacts from a card-stop result", len(obj.Artifacts))
	}
}
