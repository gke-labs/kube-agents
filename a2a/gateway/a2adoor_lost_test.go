package gateway

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// notHeldData reads a not-found error's data back into its type.
func notHeldData(t *testing.T, resp rpcResponse) a2aNotHeldData {
	t.Helper()
	if resp.Error == nil || resp.Error.Code != a2aErrTaskNotFound {
		t.Fatalf("want the protocol's task-not-found, got %+v", resp)
	}
	raw, err := json.Marshal(resp.Error.Data)
	if err != nil {
		t.Fatal(err)
	}
	var data a2aNotHeldData
	if err := json.Unmarshal(raw, &data); err != nil {
		t.Fatalf("error data %s: %v", raw, err)
	}
	return data
}

// A task accepted by one gateway and asked after on another, as after a
// restart or a failover to the other replica: the new gateway holds no
// record of it, and the error says so in data a client can act on, with
// when this gateway started serving the door and that the task may have
// run (its outcome is unknown here, so a resubmit may duplicate it). It keeps the protocol's task-not-found code, so a client
// that reads only the code behaves as before.
func TestATaskAskedAfterOnAFreshGatewayIsReportedUnknownHere(t *testing.T) {
	before := startA2ARig(t)
	task := taskOf(t, before.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("survive this", "m-restart", "", false)))

	start := time.Now()
	after := startA2ARig(t)
	for _, method := range []string{a2aMethodGet, a2aMethodCancel} {
		data := notHeldData(t, after.rpc(t, a2aTestCaller, method, map[string]any{"id": task.ID}))
		if data.Reason != a2aNotHeldSinceServing || !data.MayHaveRun {
			t.Errorf("%s: data = %+v, want reason %q and mayHaveRun", method, data, a2aNotHeldSinceServing)
		}
		since, err := time.Parse(time.RFC3339Nano, data.ServingSince)
		if err != nil || since.Before(start) {
			t.Errorf("%s: servingSince %q is not when the fresh gateway started serving (after %s)", method, data.ServingSince, start)
		}
	}
	// A follow-up onto the lost task reads the same.
	follow := sendParams("and this", "m-restart-2", "", false)
	follow["message"].(map[string]any)["taskId"] = task.ID
	if data := notHeldData(t, after.rpc(t, a2aTestCaller, a2aMethodSend, follow)); data.Reason != a2aNotHeldSinceServing {
		t.Errorf("follow-up: data = %+v", data)
	}
}

// Another caller's live task, and an id no door minted, read exactly like a
// task lost to a restart: the answer confirms no id the caller can't read.
func TestANotHeldAnswerConfirmsNothingAboutAnotherCallersTask(t *testing.T) {
	r := startA2ARig(t)
	task := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("mine", "m-1", "", false)))
	const unknownID = "task-0000000000000000"
	otherResp := r.rpc(t, a2aTestOtherCaller, a2aMethodGet, map[string]any{"id": task.ID})
	unknownResp := r.rpc(t, a2aTestOtherCaller, a2aMethodGet, map[string]any{"id": unknownID})
	other, unknown := notHeldData(t, otherResp), notHeldData(t, unknownResp)
	if other.Reason != a2aNotHeldSinceServing || other != unknown {
		t.Errorf("another caller's task reads %+v, an unknown id %+v: want both %q and alike", other, unknown, a2aNotHeldSinceServing)
	}
	if strings.ReplaceAll(otherResp.Error.Message, task.ID, "<id>") != strings.ReplaceAll(unknownResp.Error.Message, unknownID, "<id>") {
		t.Errorf("the messages differ beyond the id:\n%s\n%s", otherResp.Error.Message, unknownResp.Error.Message)
	}
}

// The caller's own task, evicted to keep the door inside its bounds, is
// reported evicted; anyone else asking after that id gets the same answer
// as for an id the door never held.
func TestAnEvictedTaskIsReportedEvictedToItsCallerAlone(t *testing.T) {
	r := startA2ARig(t)
	task := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("short-lived", "m-evict", "", false)))
	r.door.mu.Lock()
	for len(r.door.taskOrder) > 0 {
		r.door.evictOldestTaskLocked(nil)
	}
	r.door.mu.Unlock()

	own := notHeldData(t, r.rpc(t, a2aTestCaller, a2aMethodGet, map[string]any{"id": task.ID}))
	if own.Reason != a2aNotHeldEvicted || !own.MayHaveRun {
		t.Errorf("the owner's evicted task: data = %+v, want reason %q and mayHaveRun", own, a2aNotHeldEvicted)
	}
	other := notHeldData(t, r.rpc(t, a2aTestOtherCaller, a2aMethodGet, map[string]any{"id": task.ID}))
	if other.Reason != a2aNotHeldSinceServing {
		t.Errorf("another caller learns the id was evicted: data = %+v", other)
	}
}

// The record of evicted ids is bounded like the tasks themselves.
func TestTheEvictedRecordIsBounded(t *testing.T) {
	d := &A2ADoor{}
	for i := range a2aMaxEvictedIDs + 10 {
		d.rememberEvictedLocked(randHex(taskIDHexWidth)+string(rune('a'+i%26)), "c")
	}
	if len(d.evicted) != a2aMaxEvictedIDs || len(d.evictedOrder) != a2aMaxEvictedIDs {
		t.Errorf("evicted record holds %d ids (%d in order), want %d", len(d.evicted), len(d.evictedOrder), a2aMaxEvictedIDs)
	}
}
