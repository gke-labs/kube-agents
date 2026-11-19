package workeradapter

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// reusedPodRunBound bounds how long the reused pod takes to exit once it
	// is told to stop.
	reusedPodRunBound = 30 * time.Second
	// forgedTurnKeySuffix makes a key next to the turn's own that nothing
	// minted, and forgedTurnRevision a plausible revision for it.
	forgedTurnKeySuffix = "-forged"
	forgedTurnRevision  = 1
	// capRefusalTerminalPrefix opens the terminal reason of every capability
	// refusal (capability.go); the rule that fired follows it.
	capRefusalTerminalPrefix = "reason: capability-refused - "
)

// A reused session pod checks the capability on every turn, not just the
// first one's: the turns after the first arrive through the task watcher rather
// than the pod's start, and a forged capability on one of them is refused
// before that turn's harness starts. The refusal is the turn's, not the
// pod's: the next turn, on a real capability, runs on the same pod and
// resumes the session the first turn left.
//
// recordingStub counts its runs (dir/n) before it does anything else, so the
// count is the proof that a harness did or did not start.
func TestAReusedSessionPodRefusesAForgedTurnAndRunsTheNextOne(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-stoat-c1p2"
	const first, forgedTurn, third = "task-rc-1", "task-rc-2", "task-rc-3"
	dir := t.TempDir()
	submit(t, c, session, first, "first question")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, reuseConfig(url, first, session, recordingStub(t, dir, "")))
	waitState(t, c, session, first, lib.StateCompleted)
	if n := runCount(dir); n != 1 {
		t.Fatalf("after the first turn the harness ran %d times, want 1", n)
	}

	// The gateway's own envelope shape, with the grants pointing at a key
	// nothing minted.
	forged := capability.Ref{Key: "root." + forgedTurn + forgedTurnKeySuffix, Revision: forgedTurnRevision}
	submitWithAuthority(t, c, session, forgedTurn, "second question", authorityFor(t, forged))
	waitState(t, c, session, forgedTurn, lib.StateRejected)
	task := foldTask(t, c, session, forgedTurn)
	if !task.Final {
		t.Fatalf("the forged turn's refusal is not terminal: %+v", task)
	}
	for _, s := range task.StatusHistory {
		if s == lib.StateWorking {
			t.Fatalf("the forged turn reached working: %v", task.StatusHistory)
		}
	}
	// Exactly the chain-walk refusal, not just some capability refusal: an
	// unreachable verifier also ends in capability-refused.
	if text, want := terminalText(t, url, session, forgedTurn), capRefusalTerminalPrefix+capability.WalkRefused; text != want {
		t.Fatalf("the forged turn's terminal reason = %q, want %q", text, want)
	}
	if n := runCount(dir); n != 1 {
		t.Fatalf("the harness ran %d times after the forged turn, want 1: it started for a refused turn", n)
	}

	submit(t, c, session, third, "third question")
	waitState(t, c, session, third, lib.StateCompleted)
	select {
	case out := <-done:
		t.Fatalf("the pod exited instead of staying for its conversation: %+v err=%v", out.res, out.err)
	default:
	}
	if n := runCount(dir); n != 2 {
		t.Fatalf("the harness ran %d times after the third turn, want 2", n)
	}
	if p := readRun(t, dir, "prompt", 2); !strings.Contains(p, "third question") || strings.Contains(p, "second question") {
		t.Errorf("the second harness run read %q; want the third turn's prompt and nothing of the refused one", p)
	}
	if a := readRun(t, dir, "argv", 2); !strings.HasSuffix(strings.TrimSpace(a), harnessResumeFlag+" sess-1") {
		t.Errorf("the third turn's harness argv = %q; want it to resume the first turn's session on the same pod", a)
	}
	if got := artifactText(foldTask(t, c, session, third), lib.ArtifactResult); got != "answer 2" {
		t.Errorf("the third turn's result = %q, want the second harness run's", got)
	}

	cancel()
	if out := waitOutcome(t, done, reusedPodRunBound); out.err != nil || out.res != (Result{}) {
		t.Fatalf("SIGTERM while idle: %+v err=%v, want a zero Result", out.res, out.err)
	}
}
