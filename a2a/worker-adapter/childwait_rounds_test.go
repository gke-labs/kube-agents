package workeradapter

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// roundsParentStub is a toy-driver parent that delegates once per round:
// after its opening turn and after each delivery but the last it ends a turn
// once dir/asked.<n> exists, writes each delivery's closing line to
// dir/close.<n>, and answers "the report" once dir/go exists.
func roundsParentStub(t *testing.T, dir string, rounds int) []string {
	t.Helper()
	return stub(t, fmt.Sprintf(`
dir=%q
rounds=%d
waitfor() { i=0; while [ ! -f "$1" ] && [ "$i" -lt 1200 ]; do sleep 0.05; i=$((i+1)); done; }
echo 'S toy-parent'
read -r first || exit 1
touch "$dir/armed"
n=1
while [ "$n" -le "$rounds" ]; do
  waitfor "$dir/asked.$n"
  echo "OK handed out round $n"
  while read -r -t 60 l; do
    case "$l" in "That is everything"*) printf '%%s' "$l" > "$dir/close.$n"; break;; esac
  done
  n=$((n+1))
done
waitfor "$dir/go"
echo 'OK the report'
cat > /dev/null
`, dir, rounds))
}

func touchFile(t *testing.T, path string) {
	t.Helper()
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
}

func readClose(t *testing.T, dir string, n int) string {
	t.Helper()
	path := filepath.Join(dir, fmt.Sprintf("close.%d", n))
	waitPath(t, path, fmt.Sprintf("delivery %d was never written", n))
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func delegateArtifacts(t *testing.T, url, addressee, taskID string) int {
	t.Helper()
	n := 0
	for _, ev := range replayEvents(t, url, addressee, taskID) {
		var u lib.ArtifactUpdate
		if ev.Kind == lib.KindArtifactUpdate && json.Unmarshal(ev.Payload, &u) == nil && u.Artifact.Name == lib.ArtifactDelegate {
			n++
		}
	}
	return n
}

// With two rounds advertised, the first delivery offers the round left, and
// a delegate call after it starts round two: its own wait, which a late
// duplicate of round one's end doesn't settle. Round two's delivery offers
// nothing more, a third call is told the results are delivered, and the
// task's deliverable is the answer after the last delivery.
func TestAProfileTaskWithTwoRoundsDelegatesAgainAfterTheDelivery(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	addressee, taskID := "auditor-rounds2", "task-rounds2"
	submitWithParts(t, c, addressee, taskID, "audit prod", roundsPart(t, 2))
	dir, err := os.MkdirTemp("", "r")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	cfg := adapterConfig(url, taskID, addressee, roundsParentStub(t, dir, 2))
	cfg.DelegateSocket = filepath.Join(dir, "d.sock")
	cfg.TaskDeadline = 2 * time.Minute
	cfg.Driver = &toyDriver{}
	done := runDispatched(context.Background(), cfg)
	waitPath(t, filepath.Join(dir, "armed"), "the parent stub never started")

	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "audit prod"}); !reply.OK {
		t.Fatalf("round one's delegate reply = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.1"))
	one := lib.ChildResult{ChildTaskID: "child-1", Addressee: "cluster", State: lib.StateCompleted, Text: "prod has 3 nodes.", Round: 1}
	sendChildEnd(t, c, addressee, taskID, childPart(t, one))
	if got, want := readClose(t, dir, 1), fmt.Sprintf(childDeliveryCloseRoundsLeft, 1, "round"); got != want {
		t.Fatalf("round one's delivery closes %q, want %q", got, want)
	}

	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "which node is unhealthy?"}); !reply.OK {
		t.Fatalf("round two's delegate reply = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.2"))
	// The gateway can send an end twice; round one's again is no end of
	// round two's.
	sendChildEnd(t, c, addressee, taskID, childPart(t, one))
	time.Sleep(time.Second)
	if _, err := os.Stat(filepath.Join(dir, "close.2")); err == nil {
		t.Fatal("round one's duplicate end settled round two's wait")
	}
	sendChildEnd(t, c, addressee, taskID, childPart(t, lib.ChildResult{ChildTaskID: "child-2", Addressee: "cluster", State: lib.StateCompleted, Text: "node-2 is NotReady.", Round: 2}))
	if got := readClose(t, dir, 2); got != childDeliveryClose {
		t.Fatalf("round two's delivery closes %q, want %q", got, childDeliveryClose)
	}

	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "a third"}); reply.OK || reply.Message != refuseAfterDelivery {
		t.Fatalf("a third call = %+v, want refused with %q", reply, refuseAfterDelivery)
	}
	touchFile(t, filepath.Join(dir, "go"))
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	if r := artifactText(foldTask(t, c, addressee, taskID), lib.ArtifactResult); r != "the report" {
		t.Fatalf("result %q, want the answer after the last delivery", r)
	}
	if n := delegateArtifacts(t, url, addressee, taskID); n != 2 {
		t.Fatalf("%d delegate artifacts, want one per round", n)
	}
}

// A round's children that the deadline reserve delivered without leave no
// time for another round: the delivery offers none, and a call with rounds
// still advertised is told time is up rather than starting a round whose
// children couldn't report.
func TestNoNewRoundOnceTheReserveHasDelivered(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	addressee, taskID := "auditor-rounds-late", "task-rounds-late"
	submitWithParts(t, c, addressee, taskID, "audit prod", roundsPart(t, 3))
	dir, err := os.MkdirTemp("", "r")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	cfg := adapterConfig(url, taskID, addressee, roundsParentStub(t, dir, 1))
	cfg.DelegateSocket = filepath.Join(dir, "d.sock")
	// The reserve is half of a short budget: it delivers 4s in.
	cfg.TaskDeadline = 8 * time.Second
	cfg.Driver = &toyDriver{}
	done := runDispatched(context.Background(), cfg)
	waitPath(t, filepath.Join(dir, "armed"), "the parent stub never started")

	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "audit prod"}); !reply.OK {
		t.Fatalf("delegate reply = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.1"))
	if got := readClose(t, dir, 1); got != childDeliveryClose {
		t.Fatalf("the reserve's delivery closes %q, want %q", got, childDeliveryClose)
	}
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "again"}); reply.OK || reply.Message != refuseNoTimeForRound {
		t.Fatalf("a call after the reserve = %+v, want refused with %q", reply, refuseNoTimeForRound)
	}
	touchFile(t, filepath.Join(dir, "go"))
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	if n := delegateArtifacts(t, url, addressee, taskID); n != 1 {
		t.Fatalf("%d delegate artifacts, want the first round's only", n)
	}
}

// The delivery's close offers the rounds left, naming them, and says to
// answer when there are none.
func TestTheDeliveryCloseOffersTheRoundsLeft(t *testing.T) {
	w := newChildWait(lib.DelegateRequest{Addressee: "cluster", Text: "audit"}, 1)
	w.add(lib.ChildResult{ChildTaskID: "child-a", Addressee: "cluster", State: lib.StateCompleted, Text: "ok"})
	for left, want := range map[int]string{
		0: childDeliveryClose,
		1: fmt.Sprintf(childDeliveryCloseRoundsLeft, 1, "round"),
		2: fmt.Sprintf(childDeliveryCloseRoundsLeft, 2, "rounds"),
	} {
		if got := w.childDelivery(func(s string) string { return s }, left); !strings.HasSuffix(got, "\n\n"+want) {
			t.Errorf("%d left: delivery ends %q, want %q", left, got[max(len(got)-len(want)-2, 0):], want)
		}
	}
}

// Under a driver that holds steers, a round's ends that settle mid-turn are
// held for the turn's result, so the model hasn't seen them: a call in that
// turn is still waiting on them, not the start of the next round.
func TestACallBeforeAHeldDeliveryReachesTheModelStartsNoRound(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	addressee, taskID := "auditor-rounds-held", "task-rounds-held"
	submitWithParts(t, c, addressee, taskID, "audit prod", roundsPart(t, 2))
	dir, err := os.MkdirTemp("", "r")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	cfg := adapterConfig(url, taskID, addressee, roundsParentStub(t, dir, 1))
	cfg.DelegateSocket = filepath.Join(dir, "d.sock")
	cfg.TaskDeadline = 2 * time.Minute
	cfg.Driver = &toyDriver{hold: true}
	done := runDispatched(context.Background(), cfg)
	waitPath(t, filepath.Join(dir, "armed"), "the parent stub never started")

	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "audit prod"}); !reply.OK {
		t.Fatalf("delegate reply = %+v", reply)
	}
	sendChildEnd(t, c, addressee, taskID, childPart(t, lib.ChildResult{ChildTaskID: "child-1", Addressee: "cluster", State: lib.StateCompleted, Text: "prod has 3 nodes.", Round: 1}))
	time.Sleep(childWaitPause)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "again"}); reply.OK || reply.Message != refuseWhileWaiting {
		t.Fatalf("a call while the delivery is held = %+v, want refused with %q", reply, refuseWhileWaiting)
	}
	touchFile(t, filepath.Join(dir, "asked.1"))
	if got, want := readClose(t, dir, 1), fmt.Sprintf(childDeliveryCloseRoundsLeft, 1, "round"); got != want {
		t.Fatalf("the held delivery closes %q, want %q", got, want)
	}
	touchFile(t, filepath.Join(dir, "go"))
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	if n := delegateArtifacts(t, url, addressee, taskID); n != 1 {
		t.Fatalf("%d delegate artifacts, want the first round's only", n)
	}
}

// roundsRig starts a dispatched run of a parent stub that delegates once per
// round for stubRounds rounds, with advertised rounds, the given driver and
// deadline, and waits until the stub has its opening turn.
func roundsRig(t *testing.T, name string, advertised, stubRounds int, drv Driver, deadline time.Duration) (cfg Config, dir, url, addressee, taskID string, done <-chan runOutcome) {
	t.Helper()
	url = startServer(t)
	c := testClient(t, url)
	addressee, taskID = "auditor-"+name, "task-"+name
	submitWithParts(t, c, addressee, taskID, "audit prod", roundsPart(t, advertised))
	dir, err := os.MkdirTemp("", "r")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	cfg = adapterConfig(url, taskID, addressee, roundsParentStub(t, dir, stubRounds))
	cfg.DelegateSocket = filepath.Join(dir, "d.sock")
	cfg.TaskDeadline = deadline
	cfg.Driver = drv
	done = runDispatched(context.Background(), cfg)
	waitPath(t, filepath.Join(dir, "armed"), "the parent stub never started")
	return cfg, dir, url, addressee, taskID, done
}

// Under a driver that doesn't hold steers, a round's ends that settle
// mid-turn are written onto stdin at once, but the harness reads them only
// at the turn's end: until then the model hasn't seen them, so a call in that
// turn is still waiting on them, not the start of the next round. Once the
// turn ends and the delivery is read, a call starts round two.
func TestACallBeforeAQueuedDeliveryReachesTheModelStartsNoRound(t *testing.T) {
	cfg, dir, url, addressee, taskID, done := roundsRig(t, "rounds-queued", 2, 2, &toyDriver{}, 2*time.Minute)
	c := testClient(t, url)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "audit prod"}); !reply.OK {
		t.Fatalf("round one's delegate reply = %+v", reply)
	}
	sendChildEnd(t, c, addressee, taskID, childPart(t, lib.ChildResult{ChildTaskID: "child-1", Addressee: "cluster", State: lib.StateCompleted, Text: "prod has 3 nodes.", Round: 1}))
	time.Sleep(childWaitPause)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "again"}); reply.OK || reply.Message != refuseWhileWaiting {
		t.Fatalf("a call while the delivery waits on stdin = %+v, want refused with %q", reply, refuseWhileWaiting)
	}
	touchFile(t, filepath.Join(dir, "asked.1"))
	if got, want := readClose(t, dir, 1), fmt.Sprintf(childDeliveryCloseRoundsLeft, 1, "round"); got != want {
		t.Fatalf("round one's delivery closes %q, want %q", got, want)
	}
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "which node is unhealthy?"}); !reply.OK {
		t.Fatalf("round two's delegate reply, once the delivery was read = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.2"))
	sendChildEnd(t, c, addressee, taskID, childPart(t, lib.ChildResult{ChildTaskID: "child-2", Addressee: "cluster", State: lib.StateCompleted, Text: "node-2 is NotReady.", Round: 2}))
	if got := readClose(t, dir, 2); got != childDeliveryClose {
		t.Fatalf("round two's delivery closes %q, want %q", got, childDeliveryClose)
	}
	touchFile(t, filepath.Join(dir, "go"))
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	if n := delegateArtifacts(t, url, addressee, taskID); n != 2 {
		t.Fatalf("%d delegate artifacts, want one per round", n)
	}
}

// Each round arms its own reserve: a round two whose children never report
// is delivered by the reserve ahead of the deadline, as round one would be,
// rather than left open until the deadline ends the task.
func TestASecondRoundsWaitIsDeliveredByTheReserve(t *testing.T) {
	// The reserve is half of a short budget: it delivers 4s in. Round one
	// settles well before it, so round two starts with time left.
	cfg, dir, url, addressee, taskID, done := roundsRig(t, "rounds-reserve2", 3, 2, &toyDriver{}, 8*time.Second)
	c := testClient(t, url)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "audit prod"}); !reply.OK {
		t.Fatalf("round one's delegate reply = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.1"))
	sendChildEnd(t, c, addressee, taskID, childPart(t, lib.ChildResult{ChildTaskID: "child-1", Addressee: "cluster", State: lib.StateCompleted, Text: "prod has 3 nodes.", Round: 1}))
	readClose(t, dir, 1)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "which node is unhealthy?"}); !reply.OK {
		t.Fatalf("round two's delegate reply = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.2"))
	if got := readClose(t, dir, 2); got != childDeliveryClose {
		t.Fatalf("the reserve's delivery of round two closes %q, want %q", got, childDeliveryClose)
	}
	touchFile(t, filepath.Join(dir, "go"))
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
}

// With its rounds used and the last round's delivery held for the turn's
// result, a call is refused as having delegated, with its results still to
// come: the model hasn't seen them, so it isn't told they were delivered.
func TestARoundsUsedRefusalBeforeTheDeliveryIsReadSaysTheResultsAreComing(t *testing.T) {
	cfg, dir, url, addressee, taskID, done := roundsRig(t, "rounds-used-held", 1, 1, &toyDriver{hold: true}, 2*time.Minute)
	c := testClient(t, url)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "audit prod"}); !reply.OK {
		t.Fatalf("delegate reply = %+v", reply)
	}
	sendChildEnd(t, c, addressee, taskID, childPart(t, lib.ChildResult{ChildTaskID: "child-1", Addressee: "cluster", State: lib.StateCompleted, Text: "prod has 3 nodes.", Round: 1}))
	time.Sleep(childWaitPause)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "again"}); reply.OK || reply.Message != refuseSecondDelegation {
		t.Fatalf("a call while the delivery is held = %+v, want refused with %q", reply, refuseSecondDelegation)
	}
	touchFile(t, filepath.Join(dir, "asked.1"))
	readClose(t, dir, 1)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "and again"}); reply.OK || reply.Message != refuseAfterDelivery {
		t.Fatalf("a call after the delivery was read = %+v, want refused with %q", reply, refuseAfterDelivery)
	}
	touchFile(t, filepath.Join(dir, "go"))
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
}

// A held delivery's close is written for when the model reads it: settled
// with a round left but written after the reserve's time, it offers none,
// since a call then would be refused.
func TestAHeldDeliveryWrittenAfterTheReserveOffersNoRound(t *testing.T) {
	// The reserve's time is half of a short budget: 4s in.
	cfg, dir, url, addressee, taskID, done := roundsRig(t, "rounds-held-late", 2, 1, &toyDriver{hold: true}, 8*time.Second)
	start := time.Now()
	c := testClient(t, url)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "audit prod"}); !reply.OK {
		t.Fatalf("delegate reply = %+v", reply)
	}
	sendChildEnd(t, c, addressee, taskID, childPart(t, lib.ChildResult{ChildTaskID: "child-1", Addressee: "cluster", State: lib.StateCompleted, Text: "prod has 3 nodes.", Round: 1}))
	// Settled at once, held; the turn ends after the reserve's time.
	time.Sleep(time.Until(start.Add(4*time.Second + childWaitPause)))
	touchFile(t, filepath.Join(dir, "asked.1"))
	if got := readClose(t, dir, 1); got != childDeliveryClose {
		t.Fatalf("the held delivery, written after the reserve's time, closes %q, want %q", got, childDeliveryClose)
	}
	touchFile(t, filepath.Join(dir, "go"))
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
}

// A round-two request the gateway refuses because its round one hasn't
// settled (#7547) comes back as a busy delegationRefused tagged round 2: it
// ends round two's wait at once, and since the gateway didn't count it as a
// round, neither does the worker. The delivery offers the round again, a
// call after it is round two once more (as the gateway numbers it), and that
// round's ends settle it. A round-one end that arrives late is dropped, since
// round one's delivery is already the model's.
func TestABusyRefusalEndsItsRoundsWaitAndUsesNoRound(t *testing.T) {
	// A long budget puts the reserve minutes out, so only the refusal can
	// deliver round two within readClose's wait.
	cfg, dir, url, addressee, taskID, done := roundsRig(t, "rounds-busy-refused", 2, 3, &toyDriver{}, 20*time.Minute)
	c := testClient(t, url)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "audit prod"}); !reply.OK {
		t.Fatalf("round one's delegate reply = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.1"))
	sendChildEnd(t, c, addressee, taskID, childPart(t, lib.ChildResult{ChildTaskID: "child-1", Addressee: "cluster", State: lib.StateFailed, Text: "withdrawn"}))
	readClose(t, dir, 1)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "again"}); !reply.OK {
		t.Fatalf("round two's delegate reply = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.2"))
	busy, err := lib.DelegationRefusalPart(lib.DelegationRefusal{Text: "⚠️ delegation refused: this task's last round of delegated tasks hasn't finished on the gateway, so another round can't start yet", Round: 2, Busy: true})
	if err != nil {
		t.Fatal(err)
	}
	sendChildEnd(t, c, addressee, taskID, busy)
	if got, want := readClose(t, dir, 2), fmt.Sprintf(childDeliveryCloseRoundsLeft, 1, "round"); got != want {
		t.Fatalf("the busy refusal's delivery closes %q, want %q (the round wasn't used)", got, want)
	}
	// Round one's child, still running when it was withdrawn, reports late.
	sendChildEnd(t, c, addressee, taskID, childPart(t, lib.ChildResult{ChildTaskID: "child-1", Addressee: "cluster", State: lib.StateCompleted, Text: "prod has 3 nodes."}))
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "again, now it has settled"}); !reply.OK {
		t.Fatalf("the retried round two's delegate reply = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.3"))
	sendChildEnd(t, c, addressee, taskID, childPart(t, lib.ChildResult{ChildTaskID: "child-2", Addressee: "cluster", State: lib.StateCompleted, Text: "node-2 is NotReady.", Round: 2}))
	if got := readClose(t, dir, 3); got != childDeliveryClose {
		t.Fatalf("the retried round two's delivery closes %q, want %q", got, childDeliveryClose)
	}
	touchFile(t, filepath.Join(dir, "go"))
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	if r := artifactText(foldTask(t, c, addressee, taskID), lib.ArtifactResult); r != "the report" {
		t.Fatalf("result %q, want the answer after the last delivery", r)
	}
}

// A refusal for the round the wait is on that isn't busy (the request was
// refused on its merits) used its round, as the gateway counts it: the
// delivery offers no more than the rounds left after it.
func TestANonBusyRefusalOfALaterRoundUsesItsRound(t *testing.T) {
	cfg, dir, url, addressee, taskID, done := roundsRig(t, "rounds-refused2", 2, 2, &toyDriver{}, 20*time.Minute)
	c := testClient(t, url)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "audit prod"}); !reply.OK {
		t.Fatalf("round one's delegate reply = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.1"))
	sendChildEnd(t, c, addressee, taskID, childPart(t, lib.ChildResult{ChildTaskID: "child-1", Addressee: "cluster", State: lib.StateCompleted, Text: "prod has 3 nodes."}))
	readClose(t, dir, 1)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "audit staging"}); !reply.OK {
		t.Fatalf("round two's delegate reply = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.2"))
	refused, err := lib.DelegationRefusalPart(lib.DelegationRefusal{Text: "⚠️ delegation refused: cluster staging is outside this install's scope", Round: 2})
	if err != nil {
		t.Fatal(err)
	}
	sendChildEnd(t, c, addressee, taskID, refused)
	if got := readClose(t, dir, 2); got != childDeliveryClose {
		t.Fatalf("a refused round two's delivery closes %q, want %q", got, childDeliveryClose)
	}
	touchFile(t, filepath.Join(dir, "go"))
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
}

// The gateway's side of #7547 numbers the request after a busy refusal as
// the same round: one refused on its merits then comes back tagged round 2,
// which ends the worker's retried round-two wait rather than leaving it to
// wait on a round the gateway never started.
func TestAfterABusyRefusalANormalRefusalOfTheSameRoundEndsTheWait(t *testing.T) {
	cfg, dir, url, addressee, taskID, done := roundsRig(t, "rounds-busy-then-refused", 3, 3, &toyDriver{}, 20*time.Minute)
	c := testClient(t, url)
	refusal := func(text string, round int, busy bool) lib.Part {
		t.Helper()
		p, err := lib.DelegationRefusalPart(lib.DelegationRefusal{Text: text, Round: round, Busy: busy})
		if err != nil {
			t.Fatal(err)
		}
		return p
	}
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "audit prod"}); !reply.OK {
		t.Fatalf("round one's delegate reply = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.1"))
	sendChildEnd(t, c, addressee, taskID, childPart(t, lib.ChildResult{ChildTaskID: "child-1", Addressee: "cluster", State: lib.StateCompleted, Text: "prod has 3 nodes."}))
	readClose(t, dir, 1)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "again"}); !reply.OK {
		t.Fatalf("round two's delegate reply = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.2"))
	sendChildEnd(t, c, addressee, taskID, refusal("⚠️ delegation refused: this task's last round of delegated tasks hasn't finished on the gateway, so another round can't start yet", 2, true))
	readClose(t, dir, 2)
	if reply := askDelegate(t, cfg.DelegateSocket, lib.DelegateRequest{Addressee: "cluster", Text: "two clusters, too wide"}); !reply.OK {
		t.Fatalf("the retried round two's delegate reply = %+v", reply)
	}
	touchFile(t, filepath.Join(dir, "asked.3"))
	sendChildEnd(t, c, addressee, taskID, refusal("⚠️ delegation refused: too many tasks in one request", 2, false))
	if got, want := readClose(t, dir, 3), fmt.Sprintf(childDeliveryCloseRoundsLeft, 1, "round"); got != want {
		t.Fatalf("the refused retried round two's delivery closes %q, want %q", got, want)
	}
	touchFile(t, filepath.Join(dir, "go"))
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
}
