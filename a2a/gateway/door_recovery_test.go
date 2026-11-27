package gateway

import (
	"context"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// handOver stops r's gateway and starts a fresh one on the same bus, as a
// failover or a rollout hands the door to the other replica: the bus, the
// session records and the task streams survive; the door's memory does not.
func handOver(t *testing.T, r *a2aRig) *a2aRig {
	t.Helper()
	r.stop()
	select {
	case <-r.ran:
	case <-time.After(10 * time.Second):
		t.Fatal("the first gateway did not stop")
	}
	return startA2ARigTuned(t, func(door *A2ADoor) Adapter {
		return WithSideDoors(newFakeAdapter(), []DoorSpec{A2ADoorSpec(door)}, nil)
	}, a2aRigTuning{url: r.url})
}

// startA2ADoorRig is the door under the composite the shipped gateway builds.
func startA2ADoorRig(t *testing.T) *a2aRig {
	t.Helper()
	return startA2ARigWith(t, func(door *A2ADoor) Adapter {
		return WithSideDoors(newFakeAdapter(), []DoorSpec{A2ADoorSpec(door)}, nil)
	})
}

// A task that finished before the handover: its route was retired at the
// terminal, and the new gateway still answers tasks/get from the record and
// the stream, with the terminal and the deliverable (#7454).
func TestADoorTaskFinishedBeforeAHandoverIsReadByTheNextGateway(t *testing.T) {
	t.Parallel()
	before := startA2ADoorRig(t)
	task := taskOf(t, before.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("finish first", "m-before", "", false)))
	before.complete(t, before.awaitTask(t, "platform"), "the answer")
	before.getUntil(t, a2aTestCaller, task.ID, "the first gateway to see the terminal", func(o a2aTaskObject) bool {
		return o.Status.State == lib.StateCompleted
	})

	after := handOver(t, before)
	got := taskOf(t, after.rpc(t, a2aTestCaller, a2aMethodGet, map[string]any{"id": task.ID}))
	if got.Status.State != lib.StateCompleted {
		t.Fatalf("state = %q, want completed", got.Status.State)
	}
	if got.ContextID != task.ContextID {
		t.Errorf("contextId = %q, want %q", got.ContextID, task.ContextID)
	}
	if len(got.Artifacts) != 1 || joinTextParts(got.Artifacts[0].Parts) != "the answer" {
		t.Errorf("artifacts = %+v, want the one result", got.Artifacts)
	}
	if got.Metadata["recovered"] != true {
		t.Errorf("metadata = %v, want recovered: true", got.Metadata)
	}
}

// A task that finished after the handover, rendered by the new gateway's
// relay: the door learns its end from the relay but not its caller, and the
// read fills the caller in from the record.
func TestADoorTaskFinishedAfterAHandoverIsReadByTheNextGateway(t *testing.T) {
	t.Parallel()
	before := startA2ADoorRig(t)
	task := taskOf(t, before.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("finish later", "m-after", "", false)))
	origin := before.awaitTask(t, "platform")

	after := handOver(t, before)
	after.complete(t, origin, "late answer")
	got := after.getUntil(t, a2aTestCaller, task.ID, "the next gateway to answer with the terminal", func(o a2aTaskObject) bool {
		return o.Status.State == lib.StateCompleted
	})
	if len(got.Artifacts) != 1 || joinTextParts(got.Artifacts[0].Parts) != "late answer" {
		t.Errorf("artifacts = %+v, want the one result", got.Artifacts)
	}
}

// A task still running at the handover reads as working on the next gateway,
// and can be cancelled there: the record has what the cancel needs.
func TestADoorTaskRunningAtAHandoverIsWorkingAndCancellableOnTheNextGateway(t *testing.T) {
	t.Parallel()
	before := startA2ADoorRig(t)
	task := taskOf(t, before.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("still running", "m-run", "", false)))
	origin := before.awaitTask(t, "platform")
	if err := before.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}

	after := handOver(t, before)
	got := taskOf(t, after.rpc(t, a2aTestCaller, a2aMethodGet, map[string]any{"id": task.ID}))
	if got.Status.State != lib.StateWorking {
		t.Fatalf("state = %q, want working", got.Status.State)
	}
	resp := after.rpc(t, a2aTestCaller, a2aMethodCancel, map[string]any{"id": task.ID})
	if resp.Error != nil {
		t.Fatalf("cancel: %d %s", resp.Error.Code, resp.Error.Message)
	}
	waitFor(t, "the cancel on the bus", func() bool {
		for _, env := range inSubjectEnvelopes(t, after.url, "platform") {
			if env.Kind == lib.KindCancel && env.TaskID == task.ID {
				return true
			}
		}
		return false
	})
}

// Deny: another caller asking after a recovered task, cancelling it or
// following up on it gets exactly the answer an id no door minted gets. The
// record lookup is a new way in, so it confirms nothing the in-memory one
// doesn't.
func TestAnotherCallerCannotReachARecoveredTask(t *testing.T) {
	t.Parallel()
	before := startA2ADoorRig(t)
	task := taskOf(t, before.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("mine alone", "m-deny", "", false)))
	before.complete(t, before.awaitTask(t, "platform"), "private answer")
	before.getUntil(t, a2aTestCaller, task.ID, "the terminal", func(o a2aTaskObject) bool { return o.Status.State == lib.StateCompleted })

	after := handOver(t, before)
	const unknownID = "task-0000000000000000"
	follow := func(id string) map[string]any {
		p := sendParams("let me in", "m-intruder-"+id, "", false)
		p["message"].(map[string]any)["taskId"] = id
		return p
	}
	for _, c := range []struct {
		method string
		params func(id string) map[string]any
	}{
		{a2aMethodGet, func(id string) map[string]any { return map[string]any{"id": id} }},
		{a2aMethodCancel, func(id string) map[string]any { return map[string]any{"id": id} }},
		{a2aMethodSend, follow},
	} {
		other := notHeldData(t, after.rpc(t, a2aTestOtherCaller, c.method, c.params(task.ID)))
		unknown := notHeldData(t, after.rpc(t, a2aTestOtherCaller, c.method, c.params(unknownID)))
		if other != unknown || !other.MayHaveRun {
			t.Errorf("%s: another caller's recovered task reads %+v, an unknown id %+v: want them alike", c.method, other, unknown)
		}
	}
	// And the owner still can: the denial was the caller, not the read.
	if got := taskOf(t, after.rpc(t, a2aTestCaller, a2aMethodGet, map[string]any{"id": task.ID})); got.Status.State != lib.StateCompleted {
		t.Errorf("the owner reads %q, want completed", got.Status.State)
	}
}

// A task the record still lists, with no terminal on its stream and no
// longer the record's active task, has an outcome the gateway doesn't know:
// mayHaveRun, never working (a recovered task must not read as running
// forever).
func TestARecoveredTaskWithNoTerminalAndNotActiveIsMayHaveRun(t *testing.T) {
	t.Parallel()
	r := startA2ADoorRig(t)
	ctx := context.Background()
	const taskID = "task-00000000000000aa"
	key := a2aConversationKey(a2aTestCaller, "ctx-00000000000000aa")
	if err := r.g.reg.Put(ctx, &SessionRecord{Key: key, ContextID: "ctx-00000000000000aa", Addressee: "platform", Kind: a2aConversationKind,
		Tasks: []TaskRef{{ID: taskID, Addressee: "platform"}}}); err != nil {
		t.Fatal(err)
	}
	if err := r.g.reg.IndexDoorTask(ctx, taskID, key); err != nil {
		t.Fatal(err)
	}
	if data := notHeldData(t, r.rpc(t, a2aTestCaller, a2aMethodGet, map[string]any{"id": taskID})); !data.MayHaveRun {
		t.Errorf("data = %+v, want mayHaveRun", data)
	}
}

// An id that isn't a task id never reaches the registry: no KV key or
// subject is built from it.
func TestARecoveryReadRefusesAnIDThatIsNotATaskID(t *testing.T) {
	t.Parallel()
	r := startA2ADoorRig(t)
	var asked []string
	r.door.mu.Lock()
	inner := r.door.locate
	r.door.locate = func(ctx context.Context, taskID string) (string, error) {
		asked = append(asked, taskID)
		return inner(ctx, taskID)
	}
	r.door.mu.Unlock()
	for _, id := range []string{"tasks.>", "a.b", "*", "task-ABC"} {
		notHeldData(t, r.rpc(t, a2aTestCaller, a2aMethodGet, map[string]any{"id": id}))
	}
	if len(asked) != 0 {
		t.Errorf("the locator was asked about %q", asked)
	}
}

// A caller signed in with a token is named with its provider's prefix
// (":google:<email>", ":entra:<oid>"), which carries colons by design so no
// eval caller can spell it. Recovery serves those callers too, and the
// prefix still keeps every class to its own conversations.
func TestATokenSignedCallerRecoversItsOwnTaskAndNoOneElses(t *testing.T) {
	t.Parallel()
	const taskID = "task-00000000000000bb"
	for _, c := range []struct {
		caller, owner string
		want          bool
	}{
		{a2aGoogleCallerPrefix + "alice@example.com", a2aGoogleCallerPrefix + "alice@example.com", true},
		{a2aEntraCallerPrefix + "0f8fad5b-d9cb-469f-a165-70867728950e", a2aEntraCallerPrefix + "0f8fad5b-d9cb-469f-a165-70867728950e", true},
		// An eval caller named like the email does not reach the token
		// caller's conversation, nor the other way round.
		{"alice@example.com", a2aGoogleCallerPrefix + "alice@example.com", false},
		{a2aGoogleCallerPrefix + "alice@example.com", "alice@example.com", false},
		{a2aGoogleCallerPrefix + "bob@example.com", a2aGoogleCallerPrefix + "alice@example.com", false},
	} {
		d, err := NewA2ADoor("127.0.0.1:0", a2aTestToken, A2ADoorOptions{PublicURL: a2aTestPublicURL, DefaultAddressee: "platform"})
		if err != nil {
			t.Fatal(err)
		}
		key := a2aConversationKey(c.owner, "ctx-00000000000000bb")
		d.SetTaskLocator(func(context.Context, string) (string, error) { return key, nil })
		d.SetProbe(func(context.Context, string, string) (ConversationState, error) {
			return ConversationState{ContextID: "ctx-bus", TaskID: taskID, Final: true, ExecutorState: lib.StateCompleted, Result: "r"}, nil
		})
		if got := d.recoverTask(context.Background(), taskID, c.caller); got != c.want {
			t.Errorf("caller %q on %q's conversation: recovered = %v, want %v", c.caller, c.owner, got, c.want)
		}
	}
}
