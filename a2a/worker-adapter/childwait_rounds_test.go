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
