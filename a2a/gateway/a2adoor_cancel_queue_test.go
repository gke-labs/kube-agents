package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// rpcAsync is rawRPC for calls made side by side: its own request id, no
// shared counter, and an error rather than t.Fatal off the test goroutine.
func (r *a2aRig) rpcAsync(id int, caller, method string, params any) (rpcResponse, error) {
	body, err := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params})
	if err != nil {
		return rpcResponse{}, err
	}
	req, err := http.NewRequest(http.MethodPost, r.base+a2aRPCPath, bytes.NewReader(body))
	if err != nil {
		return rpcResponse{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(authorizationHeader, "Bearer "+a2aTestToken)
	req.Header.Set(a2aCallerHeader, caller)
	res, err := a2aTestClient.Do(req)
	if err != nil {
		return rpcResponse{}, err
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(res.Body)
	if res.StatusCode != http.StatusOK {
		return rpcResponse{}, fmt.Errorf("%s: HTTP %d", method, res.StatusCode)
	}
	var out rpcResponse
	return out, json.Unmarshal(raw, &out)
}

// cancelQueueRig is a running task whose conversation's turn slot is held,
// as a send turn still running holds it, so calls queue behind it.
func cancelQueueRig(t *testing.T) (r *a2aRig, task a2aTaskObject, exec *lib.TaskExecution, key string) {
	t.Helper()
	r = startA2ARig(t)
	task = taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("long job", "m-1", "", false)))
	origin := r.awaitTask(t, "platform")
	exec = r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	r.getUntil(t, a2aTestCaller, task.ID, "the working event to reach the door", func(task a2aTaskObject) bool {
		return task.Status.State == lib.StateWorking
	})
	key = a2aConversationKey(a2aTestCaller, task.ContextID)
	held := lib.Message{Role: a2aRoleUser, Parts: textParts("a running turn"), MessageID: "m-held", ContextID: task.ContextID}
	if _, ok := r.door.claimTurn(context.Background(), key, a2aTestCaller, task.ContextID, held, time.Now().Add(a2aSubmitWait)); !ok {
		t.Fatal("could not hold the turn slot")
	}
	return r, task, exec, key
}

// waitQueued waits until n calls are asleep in claimTurnIf waiting for the
// slot, so a test's later steps happen while they queue and not before they
// arrive (a late call would meet the pre-checks instead).
func waitQueued(t *testing.T, r *a2aRig, n int) {
	t.Helper()
	waitFor(t, fmt.Sprintf("%d call(s) queued for the turn slot", n), func() bool {
		r.door.mu.Lock()
		defer r.door.mu.Unlock()
		return r.door.claimWaiting >= n
	})
}

func cancelEnvelopes(t *testing.T, r *a2aRig) int {
	t.Helper()
	n := 0
	for _, env := range inSubjectEnvelopes(t, r.url, "platform") {
		if env.Kind == lib.KindCancel {
			n++
		}
	}
	return n
}

// TestA2ACancelRetryQueuedBehindTheFirstIsAnsweredWithTheTask (#5777): a
// client's cancel times out behind a running turn and is retried, so two
// cancels for the task and a follow-up send queue for the slot. When it
// frees, whichever cancel claims it puts the one cancel on the bus; the
// other sees that in the claim's critical section and is answered with the
// task, claiming nothing, so no second stop goes to the gateway; and the
// send, woken with them, still gets its turn.
func TestA2ACancelRetryQueuedBehindTheFirstIsAnsweredWithTheTask(t *testing.T) {
	r, task, _, key := cancelQueueRig(t)
	type answer struct {
		what string
		resp rpcResponse
		err  error
		took time.Duration
	}
	answers := make(chan answer, 3)
	var wg sync.WaitGroup
	call := func(id int, what, method string, params any) {
		defer wg.Done()
		start := time.Now()
		resp, err := r.rpcAsync(id, a2aTestCaller, method, params)
		answers <- answer{what, resp, err, time.Since(start)}
	}
	wg.Add(3)
	go call(9001, "cancel", a2aMethodCancel, map[string]any{"id": task.ID})
	go call(9002, "cancel retry", a2aMethodCancel, map[string]any{"id": task.ID})
	go call(9003, "send", a2aMethodSend, sendParams("and one more thing", "m-3", task.ContextID, false))
	waitQueued(t, r, 3)
	select {
	case a := <-answers:
		t.Fatalf("%s answered while the slot was held: %+v", a.what, a.resp)
	default:
	}
	r.door.TurnFinished(key)
	wg.Wait()
	close(answers)

	for a := range answers {
		if a.err != nil {
			t.Fatalf("%s: %v", a.what, a.err)
		}
		switch a.what {
		case "send":
			// Not stranded: answered as a turn, never the claim's refusal.
			if a.resp.Error != nil && strings.Contains(a.resp.Error.Message, earlierTurnNote()) {
				t.Errorf("the queued send was stranded: %d %s", a.resp.Error.Code, a.resp.Error.Message)
			}
			if a.took >= a2aSubmitWait {
				t.Errorf("the queued send took %s, the whole claim bound", a.took)
			}
		default:
			if a.resp.Error != nil {
				t.Errorf("%s refused: %d %s", a.what, a.resp.Error.Code, a.resp.Error.Message)
			} else if got := taskOf(t, a.resp); got.ID != task.ID {
				t.Errorf("%s answered with task %q", a.what, got.ID)
			}
		}
	}
	waitFor(t, "the cancel on the bus", func() bool { return cancelEnvelopes(t, r) >= 1 })
	time.Sleep(300 * time.Millisecond)
	if n := cancelEnvelopes(t, r); n != 1 {
		t.Errorf("%d cancels on the bus, want the one", n)
	}
	r.door.mu.Lock()
	busy := r.door.conversationLocked(key).busy
	r.door.mu.Unlock()
	if busy {
		t.Error("the slot is still held after every queued call answered")
	}
}

// TestA2ACancelQueuedBehindATaskThatEndsIsRefusedWithoutAClaim: a cancel
// queued behind a running turn for a task that then ends on its own, with no
// cancel published, is refused as not cancelable when the slot frees, and
// takes no turn: nothing is handed to the gateway and the slot stays free.
func TestA2ACancelQueuedBehindATaskThatEndsIsRefusedWithoutAClaim(t *testing.T) {
	r, task, exec, key := cancelQueueRig(t)
	got := make(chan rpcResponse, 1)
	errs := make(chan error, 1)
	go func() {
		resp, err := r.rpcAsync(9101, a2aTestCaller, a2aMethodCancel, map[string]any{"id": task.ID})
		errs <- err
		got <- resp
	}()
	waitQueued(t, r, 1)
	if err := exec.PublishStatus(context.Background(), lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	r.getUntil(t, a2aTestCaller, task.ID, "the terminal to reach the door", func(task a2aTaskObject) bool {
		return task.Status.State == lib.StateCompleted
	})
	r.door.TurnFinished(key)
	if err := <-errs; err != nil {
		t.Fatal(err)
	}
	resp := <-got
	if resp.Error == nil || resp.Error.Code != a2aErrTaskNotCancelable {
		t.Fatalf("answer %+v, want not cancelable", resp)
	}
	time.Sleep(300 * time.Millisecond)
	if n := cancelEnvelopes(t, r); n != 0 {
		t.Errorf("%d cancels on the bus for a task that ended on its own", n)
	}
	r.door.mu.Lock()
	conv := r.door.conversationLocked(key)
	busy, turns := conv.busy, conv.turns
	r.door.mu.Unlock()
	if busy || turns != 2 {
		t.Errorf("busy %v after %d turns; want free after the send's and the held turn only", busy, turns)
	}
}

// TestA2ACancelRetryAnswersOnceTheCancelIsOnTheBus: a retry queued while
// the first cancel's turn still holds the slot answers as soon as that
// cancel reaches the bus, without waiting for the turn to end, so a turn
// that runs long can't push the retry past its claim window. With the
// executor's canceled terminal in too, it is still answered with the task:
// the cancel took.
func TestA2ACancelRetryAnswersOnceTheCancelIsOnTheBus(t *testing.T) {
	for _, terminalToo := range []bool{false, true} {
		t.Run(fmt.Sprintf("terminal=%v", terminalToo), func(t *testing.T) {
			r, task, exec, key := cancelQueueRig(t)
			got := make(chan rpcResponse, 1)
			errs := make(chan error, 1)
			go func() {
				resp, err := r.rpcAsync(9201, a2aTestCaller, a2aMethodCancel, map[string]any{"id": task.ID})
				errs <- err
				got <- resp
			}()
			waitQueued(t, r, 1)
			// The held turn is the first cancel's: its cancel reaches the bus
			// while it still holds the slot.
			r.door.CancelPublished(key, task.ID)
			if terminalToo {
				if err := exec.PublishStatus(context.Background(), lib.StateCanceled, true); err != nil {
					t.Fatal(err)
				}
				r.getUntil(t, a2aTestCaller, task.ID, "the terminal to reach the door", func(task a2aTaskObject) bool {
					return task.Status.State == lib.StateCanceled
				})
			}
			select {
			case err := <-errs:
				if err != nil {
					t.Fatal(err)
				}
				resp := <-got
				if resp.Error != nil {
					t.Fatalf("the retry was refused: %d %s", resp.Error.Code, resp.Error.Message)
				}
				if taskOf(t, resp).ID != task.ID {
					t.Errorf("the retry answered with another task")
				}
			case <-time.After(10 * time.Second):
				t.Fatal("the retry waited on the held slot after the cancel was on the bus")
			}
			r.door.TurnFinished(key)
		})
	}
}
