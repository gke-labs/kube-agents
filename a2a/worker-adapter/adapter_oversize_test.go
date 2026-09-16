package workeradapter

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestLifecycle_OversizeResultLineFailsWithTheCeilingNamed pins what a result
// line past scannerMaxBytes does. It does not truncate, and it must not stall.
//
// It used to stall. The scanner stopped with bufio.ErrTooLong and nothing
// drained stdout, so the harness blocked forever writing into a full 64KB pipe,
// cmd.Wait never returned, and the task ran to TaskDeadline -- 1800s in
// production -- reporting `deadline-exceeded`. That reason is worse than no
// reason: it sends the next debugger after a slow model when the answer is a
// full pipe.
//
// Now the scan goroutine drains and discards after the error, so the child
// exits, cmd.Wait returns, and the terminal reason names the ceiling and its
// value. The deliverable is still refused rather than truncated, which is the
// deliberate half: a silently shortened answer is worse than a loud failure,
// and a line this size means a model dumped a file into its result.
//
// The deadline here is injected and generous on purpose. It has to be injected
// so a regression fails in a minute instead of hanging for the production
// 1800s, and generous because a deadline-based test that waits on a stall is
// exactly the shape that flakes -- this PR already fixed one that failed CI at
// 15.12s against a 15s bound.
func TestLifecycle_OversizeResultLineFailsWithTheCeilingNamed(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-tapir-oversize", "task-oversize-1"
	submit(t, c, session, taskID, "write something enormous")

	// One result line comfortably past scannerMaxBytes (8 MiB), emitted the
	// way a real deliverable would be: valid JSON, exit 0, nothing wrong with
	// the harness at all.
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-oversize"}'
printf '{"type":"result","subtype":"success","result":"'
head -c 9000000 /dev/zero | tr '\0' 'x'
printf '"}\n'
exit 0
`)
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.TaskDeadline = 90 * time.Second
	started := time.Now()
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 120*time.Second)
	if out.res.State != lib.StateFailed {
		t.Fatalf("state %q, want failed: an oversize deliverable must not read as success", out.res.State)
	}
	task := foldTask(t, c, session, taskID)
	if task.State != lib.StateFailed || !task.Final {
		t.Fatalf("folded %+v, want a final failed task", task)
	}
	events := replayEvents(t, url, session, taskID)
	text := statusOf(t, events[len(events)-1]).Status.Message.Parts[0].Text
	if strings.Contains(text, "deadline-exceeded") {
		t.Fatalf("the oversize line stalled to the task deadline again — the "+
			"drain after the scan error has regressed: %q", text)
	}
	if !strings.Contains(text, "stream-ended-without-result") {
		t.Errorf("reason does not say the stream ended without a result: %q", text)
	}
	if !strings.Contains(text, "8388608-byte limit") || !strings.Contains(text, "scannerMaxBytes") {
		t.Errorf("reason does not name the ceiling and its value, so it is accurate "+
			"but not actionable: %q", text)
	}
	// The point of the drain is that this no longer waits out the clock. A
	// wide margin: the assertion is "promptly, not at the deadline", and the
	// deadline is 90s.
	if elapsed := time.Since(started); elapsed > 60*time.Second {
		t.Errorf("took %s to fail; the drain should let the harness exit "+
			"promptly rather than running to the deadline", elapsed)
	}
}

// TestLifecycle_OversizeLineWithAHarnessThatWaitsOnStdin is the version of the
// test above with a stub that behaves like the real harness: after writing, it
// keeps reading stdin rather than exiting.
//
// That difference is the whole finding. The real harness is an agent loop whose
// turn ends when the adapter closes stdin; the `result` arm does that, and the
// scan-error path did not. So the drain blocked on a pipe that never closed,
// the goroutine never returned, `close(events)` never ran, `cmd.Wait` was never
// reached, and the task parked until TaskDeadline -- publishing
// `deadline-exceeded` and never reaching the ceiling diagnostic. A stub that
// exits on its own hides all of it.
func TestLifecycle_OversizeLineWithAHarnessThatWaitsOnStdin(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-tapir-oversize2", "task-oversize-2"
	submit(t, c, session, taskID, "write something enormous")

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-oversize2"}'
printf '{"type":"result","subtype":"success","result":"'
head -c 9000000 /dev/zero | tr '\0' 'x'
printf '"}\n'
# The real harness does not exit here: it waits for the next turn on stdin,
# and exits when the adapter closes it.
while read -r _line; do :; done
exit 0
`)
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.TaskDeadline = 90 * time.Second
	started := time.Now()
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 120*time.Second)
	if out.res.State != lib.StateFailed {
		t.Fatalf("state %q, want failed", out.res.State)
	}
	text := statusOf(t, replayEvents(t, url, session, taskID)[len(replayEvents(t, url, session, taskID))-1]).Status.Message.Parts[0].Text
	if strings.Contains(text, "deadline-exceeded") {
		t.Fatalf("stalled to the deadline with a harness that waits on stdin — "+
			"the scan-error path must end the input stream, not just drain: %q", text)
	}
	if !strings.Contains(text, "scannerMaxBytes") {
		t.Errorf("reason does not name the ceiling: %q", text)
	}
	if elapsed := time.Since(started); elapsed > 60*time.Second {
		t.Errorf("took %s; the harness was not released promptly", elapsed)
	}
}
