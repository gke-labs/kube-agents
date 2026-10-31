package workeradapter

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The opening-prompt tests need a write that cannot complete into the pipe
// buffer and return before the stub exits. The write then blocks until the
// stub's exit closes the read end and fails with EPIPE, every run. A prompt
// that fits the buffer would let the write succeed and send the task down
// supervise's failure arm instead, which is not the path under test.
//
// The buffer is not a fixed size. Linux gives a fresh pipe 16 pages
// (PIPE_DEF_BUFFERS in fs/pipe.c): 64 KiB on 4 KiB pages, but 1 MiB on a
// 64 KiB-page arm64 kernel. macOS pipes grow to 64 KiB at most. So the size is
// derived from the page size rather than assumed.
const (
	// minOpeningPromptBytes is four times the 64 KiB buffer of a 4 KiB-page
	// Linux kernel or a macOS one.
	minOpeningPromptBytes = 256 * 1024
	// pipeDefBuffers is Linux's PIPE_DEF_BUFFERS: a fresh pipe's capacity in
	// pages.
	pipeDefBuffers = 16
)

// openingPromptBytesFor is the prompt size that overruns a fresh pipe on a
// kernel with the given page size: twice Linux's default capacity, and never
// less than minOpeningPromptBytes.
func openingPromptBytesFor(pageSize int) int {
	return max(minOpeningPromptBytes, 2*pipeDefBuffers*pageSize)
}

// openingPromptBytes is openingPromptBytesFor on this machine.
func openingPromptBytes() int { return openingPromptBytesFor(os.Getpagesize()) }

// TestOpeningPromptBytes_OverrunsAFreshPipe: the size carries the premise on
// every page size, including the 64 KiB pages where a fixed 256 KiB would fit
// a fresh Linux pipe four times over.
func TestOpeningPromptBytes_OverrunsAFreshPipe(t *testing.T) {
	for _, page := range []int{4096, 16384, 65536} {
		linuxPipe := pipeDefBuffers * page
		if got := openingPromptBytesFor(page); got <= linuxPipe || got <= 64*1024 {
			t.Errorf("page size %d: prompt of %d bytes does not overrun a %d-byte Linux pipe or a 64 KiB macOS one", page, got, linuxPipe)
		}
	}
}

// TestLifecycle_OpeningPromptWriteFailureKeepsEvidence: a harness that dies
// before reading its prompt fails the opening-prompt write, and the terminal
// reason still carries its exit status and stderr tail, as every other
// harness failure does (TestLifecycle_FailedWithEvidence). The token stays
// spawn-failed: the eval harness classifies on it.
func TestLifecycle_OpeningPromptWriteFailureKeepsEvidence(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	// The prompt rides one bus message, so it must fit the server's payload
	// cap with room for the envelope. Where the page size makes the prompt
	// too large for that, the direct startHarness test below, which has no
	// cap, still covers the write failure.
	size := openingPromptBytes()
	const envelopeRoom = 4096
	if limit := c.Conn().MaxPayload(); int64(size+envelopeRoom) > limit {
		t.Skipf("a %d-byte opening prompt (enough to overrun a fresh pipe on %d-byte pages) does not fit the test server's %d-byte payload cap; TestStartHarness_OpeningPromptReapIsBounded covers the write failure without the bus", size, os.Getpagesize(), limit)
	}
	const session, taskID = "chat-okapi-c5d6", "task-openfail-1"
	submit(t, c, session, taskID, strings.Repeat("x", size))

	// Never reads stdin: bash reads the script from its path, not stdin.
	harness := stub(t, `
echo "stub died before reading its prompt" >&2
exit 7
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.res.State != lib.StateFailed {
		t.Fatalf("state %q err %v", out.res.State, out.err)
	}
	events := replayEvents(t, url, session, taskID)
	last := statusOf(t, events[len(events)-1])
	if !last.Final || last.Status.State != lib.StateFailed {
		t.Fatalf("last event %+v", last)
	}
	text := last.Status.Message.Parts[0].Text
	for _, want := range []string{
		"reason: spawn-failed - write opening prompt: ",
		" - exit status 7",
		"\nstderr tail:\nstub died before reading its prompt",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("terminal reason missing %q:\n%s", want, text)
		}
	}
	if !strings.HasPrefix(text, "reason: spawn-failed ") {
		t.Errorf("terminal reason does not lead with the spawn-failed token:\n%s", text)
	}
	// Nothing held stderr and the stub exited 7, so the reason must not
	// blame a held stderr. It may still say the reap ran the full bound: on a
	// loaded runner the reap of a dead bash can take KillGrace, and the line
	// is true when it appears, so the test does not forbid it.
	if strings.Contains(text, "kept stderr open") {
		t.Errorf("terminal reason claims a held stderr the stub never left:\n%s", text)
	}
}

// reapBoundForTest is the bound the direct startHarness tests pass. The
// assertions name it as Go prints it.
const reapBoundForTest = 500 * time.Millisecond

// escapingChildStub is a harness that leaves a child holding its stderr. It
// runs prelude first (empty for a stub that never reads stdin), then
// backgrounds a sleep under job control (its own process group, out of the
// group kill's reach) that inherits stderr, writes one stderr line, and exits
// with exitCode. The sleep outlives any test's patience, so an unbounded reap
// fails on time rather than finishing late. It returns the argv and the file
// the sleep's pid lands in; the sleep is killed at cleanup.
func escapingChildStub(t *testing.T, prelude string, exitCode int, stderrLine string) ([]string, string) {
	t.Helper()
	const sleepSeconds = 120
	pidFile := filepath.Join(t.TempDir(), "escaped.pid")
	harness := stub(t, fmt.Sprintf(`
%s
set -m
sleep %d </dev/null >/dev/null &
echo $! > %q
echo %q >&2
exit %d
`, prelude, sleepSeconds, pidFile, stderrLine, exitCode))
	t.Cleanup(func() {
		raw, err := os.ReadFile(pidFile)
		if err != nil {
			return
		}
		if pid, err := strconv.Atoi(strings.TrimSpace(string(raw))); err == nil {
			_ = syscall.Kill(pid, syscall.SIGKILL)
		}
	})
	return harness, pidFile
}

// startHarnessWithin runs startHarness with the opening prompt against
// harness and returns its error, failing the test if it has not returned
// within returnWithin (an unbounded reap) or if it succeeded.
func startHarnessWithin(t *testing.T, harness []string) error {
	t.Helper()
	const returnWithin = 30 * time.Second
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	done := make(chan error, 1)
	start := time.Now()
	go func() {
		_, err := startHarness(harness, os.Environ(), strings.Repeat("x", openingPromptBytes()), reapBoundForTest, log)
		done <- err
	}()
	var err error
	select {
	case err = <-done:
	case <-time.After(returnWithin):
		t.Fatalf("startHarness still reaping after %s: the reap is unbounded", returnWithin)
	}
	if err == nil {
		t.Fatal("startHarness succeeded against a harness that never read its prompt")
	}
	t.Logf("startHarness returned after %s: %s", time.Since(start).Round(time.Millisecond), err)
	return err
}

// requireEscaped is the premise of the bounded-reap tests: the sleep led its
// own process group, so the group kill could not reach it and only the bound
// ended the reap. Without this, a shell whose background jobs stayed in the
// stub's group would let the tests pass with the bound removed.
func requireEscaped(t *testing.T, pidFile string) {
	t.Helper()
	raw, rerr := os.ReadFile(pidFile)
	if rerr != nil {
		t.Fatalf("escaped child pid: %v", rerr)
	}
	pid, perr := strconv.Atoi(strings.TrimSpace(string(raw)))
	if perr != nil {
		t.Fatalf("escaped child pid %q: %v", raw, perr)
	}
	if pgid, gerr := syscall.Getpgid(pid); gerr != nil || pgid != pid {
		t.Fatalf("background sleep %d is not its own process group leader (pgid %d, err %v); the test proves nothing", pid, pgid, gerr)
	}
}

// TestStartHarness_OpeningPromptReapIsBounded: the reap after a failed
// opening-prompt write cannot be held open by a descendant the process-group
// kill does not reach. Unbounded, Wait would read stderr until the escaped
// sleep exits; bounded, startHarness returns about one reapBound after the
// stub's exit, still carrying the exit status and the stderr tail, and says
// the reap ran the bound. After a failed exit Wait cannot say whether the
// bound fired, so the line must not blame a held stderr.
func TestStartHarness_OpeningPromptReapIsBounded(t *testing.T) {
	harness, pidFile := escapingChildStub(t, "", 7, "stub left a child holding stderr")
	msg := startHarnessWithin(t, harness).Error()
	requireEscaped(t, pidFile)
	for _, want := range []string{
		"write opening prompt: ",
		" - exit status 7 - the reap ran the full 500ms; the stderr tail may stop there",
		"\nstderr tail:\nstub left a child holding stderr",
	} {
		if !strings.Contains(msg, want) {
			t.Errorf("error missing %q:\n%s", want, msg)
		}
	}
	if strings.Contains(msg, "kept stderr open") {
		t.Errorf("error blames a held stderr after a failed exit, which Wait cannot tell from a slow reap:\n%s", msg)
	}
}

// TestReapEvidence_SlowReapIsNotAHeldStderr: a failed exit whose reap ran
// past the bound, with nothing holding stderr. A SIGKILLed harness in
// uninterruptible sleep is the shape: its own Wait blocks past the bound, and
// the WaitDelay timer, which starts only after that Wait, never fires. The
// reason must say what is known, the reap's length, and not that something
// held stderr. The clean-exit arm keeps its claim: exec.ErrWaitDelay is
// returned only after the harness was reaped and the copy still ran.
func TestReapEvidence_SlowReapIsNotAHeldStderr(t *testing.T) {
	killed := errors.New("signal: killed")
	for _, tc := range []struct {
		name    string
		waitErr error
		took    time.Duration
		bound   time.Duration
		want    string
	}{
		{"slow failed reap", killed, 12 * time.Second, 10 * time.Second,
			" - signal: killed - the reap ran the full 10s; the stderr tail may stop there"},
		{"fast failed reap", killed, time.Millisecond, 10 * time.Second, " - signal: killed"},
		{"unbounded failed reap", killed, time.Minute, 0, " - signal: killed"},
		{"clean exit, bound fired", exec.ErrWaitDelay, 10 * time.Second, 10 * time.Second,
			" - harness exited 0; a process the harness started kept stderr open past 10s; the stderr tail stops there"},
		{"clean exit, no bound", nil, time.Millisecond, 10 * time.Second, ""},
	} {
		if got := reapEvidence(tc.waitErr, tc.took, tc.bound); got != tc.want {
			t.Errorf("%s: reapEvidence = %q, want %q", tc.name, got, tc.want)
		}
	}
}

// TestStartHarness_OpeningPromptCleanExitHeldStderr: a harness that exits 0
// without reading its prompt (a --version or --help misconfiguration is the
// realistic shape) and leaves a child holding stderr. Wait then reports Go's
// own exec.ErrWaitDelay, not an exit status. The error must say the harness
// exited cleanly and that something it started held stderr past the bound,
// not relay "exec: WaitDelay expired before I/O complete".
func TestStartHarness_OpeningPromptCleanExitHeldStderr(t *testing.T) {
	harness, pidFile := escapingChildStub(t, "", 0, "usage: harness [--version]")
	msg := startHarnessWithin(t, harness).Error()
	requireEscaped(t, pidFile)
	for _, want := range []string{
		"write opening prompt: ",
		" - harness exited 0; a process the harness started kept stderr open past 500ms; the stderr tail stops there",
		"\nstderr tail:\nusage: harness [--version]",
	} {
		if !strings.Contains(msg, want) {
			t.Errorf("error missing %q:\n%s", want, msg)
		}
	}
	if strings.Contains(msg, "WaitDelay") {
		t.Errorf("error relays Go's WaitDelay message instead of saying what happened:\n%s", msg)
	}
}
