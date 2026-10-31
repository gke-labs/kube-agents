package hermesbridge

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// heldStdoutTaskDeadline ends the deadline test's stub, which would
	// otherwise sleep 60s. The clean-exit test's stub exits at once.
	heldStdoutTaskDeadline = time.Second
	// heldStdoutKillGrace is the SIGTERM-to-SIGKILL grace and so, through
	// cmd.WaitDelay, the bound on the reap.
	heldStdoutKillGrace = 500 * time.Millisecond
	// heldStdoutTerminalWithin is how long the task may take to finalize
	// before the reap counts as unbounded. The escaped sleep lasts 120s; a
	// bounded run ends about heldStdoutTaskDeadline plus one
	// heldStdoutKillGrace after it starts.
	heldStdoutTerminalWithin = 15 * time.Second
	// heldStdoutLongDeadline keeps the deadline out of the cancel and
	// shutdown tests, whose stub sleeps 60s.
	heldStdoutLongDeadline = time.Hour
	// reapNote is the detail a killed run's reason carries when its reap
	// ran the full bound.
	reapNote = "the reap after the kill ran the full 500ms, so a process hermes started may still hold its output"
)

// reasonToken is the bench's parse_reason (bench/kube_agents_bench/
// inject_transport.py): the word after "reason: ", up to the next space.
func reasonToken(reason string) string {
	rest, ok := strings.CutPrefix(strings.TrimSpace(reason), "reason: ")
	if !ok {
		return ""
	}
	token, _, _ := strings.Cut(strings.TrimSpace(rest), " ")
	return token
}

// TestLifecycle_DeadlineReapIsBoundedWithAHeldStdout: a hermes run that
// leaves a child in another process group holding stdout, then outlives the
// task deadline. The deadline's group kill ends the run but not the escaped
// sleep, and Wait copies stdout to EOF, so unbounded it waits for the sleep,
// with no terminal event in the meantime. Bounded by KillGrace, the task
// fails with deadline-exceeded about one grace after the kill, and the
// reason says the reap ran the full bound.
func TestLifecycle_DeadlineReapIsBoundedWithAHeldStdout(t *testing.T) {
	task, _ := runHeldStdoutTask(t, "task-held-stdout-deadline", "exec sleep 60", heldStdoutTaskDeadline, nil)
	if task.State != lib.StateFailed {
		t.Fatalf("state = %s, want failed", task.State)
	}
	want := fmt.Sprintf("reason: deadline-exceeded - killed after %s; %s", heldStdoutTaskDeadline, reapNote)
	got := terminalReason(t, task)
	if got != want {
		t.Fatalf("reason:\n got %q\nwant %q", got, want)
	}
	if tok := reasonToken(got); tok != "deadline-exceeded" {
		t.Fatalf("reason token = %q, want deadline-exceeded", tok)
	}
}

// TestLifecycle_CancelReapIsBoundedWithAHeldStdout: the same escaped child,
// and the task is canceled mid-run. The group kill ends the run, the reap
// is bounded by KillGrace, and the reason carries the reap note as detail
// after " - ", so the token a reader takes is still canceled-by-request.
func TestLifecycle_CancelReapIsBoundedWithAHeldStdout(t *testing.T) {
	task, _ := runHeldStdoutTask(t, "task-held-stdout-cancel", "exec sleep 60", heldStdoutLongDeadline,
		func(h heldStdoutRun) { publishCancel(t, h.client, h.origin) })
	if task.State != lib.StateCanceled {
		t.Fatalf("state = %s msg = %v, want canceled", task.State, task.FinalMessage)
	}
	got := terminalReason(t, task)
	if want := "reason: canceled-by-request - " + reapNote; got != want {
		t.Fatalf("reason:\n got %q\nwant %q", got, want)
	}
	if tok := reasonToken(got); tok != "canceled-by-request" {
		t.Fatalf("reason token = %q, want canceled-by-request", tok)
	}
}

// TestLifecycle_ShutdownReapIsBoundedWithAHeldStdout: the same escaped
// child, and the bridge shuts down mid-run. shutdownTasks finalizes the
// task with the plain bridge-shutdown reason right after its kill, so no
// reap note can reach it, and the bounded reap then lets Run's worker join
// end about one grace later instead of waiting on the escaped sleep.
func TestLifecycle_ShutdownReapIsBoundedWithAHeldStdout(t *testing.T) {
	task, joined := runHeldStdoutTask(t, "task-held-stdout-shutdown", "exec sleep 60", heldStdoutLongDeadline,
		func(h heldStdoutRun) {
			h.stop()
			done := make(chan struct{})
			go func() { h.bridge.wg.Wait(); close(done) }()
			select {
			case <-done:
			case <-time.After(heldStdoutTerminalWithin):
				t.Errorf("the bridge's workers did not finish within %s of shutdown: the reap waited on the escaped sleep", heldStdoutTerminalWithin)
			}
		})
	if !joined {
		t.Fatal("the shutdown hook did not run")
	}
	if task.State != lib.StateFailed {
		t.Fatalf("state = %s msg = %v, want failed", task.State, task.FinalMessage)
	}
	got := terminalReason(t, task)
	if got != shutdownReason {
		t.Fatalf("reason:\n got %q\nwant %q", got, shutdownReason)
	}
	if tok := reasonToken(got); tok != "bridge-shutdown" {
		t.Fatalf("reason token = %q, want bridge-shutdown", tok)
	}
}

// TestWithDetail_KeepsTheTokenIntact: the reason grammar on every shape a
// killed arm builds, read back the way the bench reads it.
func TestWithDetail_KeepsTheTokenIntact(t *testing.T) {
	for _, tc := range []struct{ reason, detail, want, token string }{
		{"reason: canceled-by-request", "", "reason: canceled-by-request", "canceled-by-request"},
		{"reason: canceled-by-request", reapNote, "reason: canceled-by-request - " + reapNote, "canceled-by-request"},
		{"reason: deadline-exceeded - killed after 1s", reapNote, "reason: deadline-exceeded - killed after 1s; " + reapNote, "deadline-exceeded"},
		{shutdownReason, reapNote, shutdownReason + "; " + reapNote, "bridge-shutdown"},
	} {
		got := withDetail(tc.reason, tc.detail)
		if got != tc.want {
			t.Errorf("withDetail(%q, %q):\n got %q\nwant %q", tc.reason, tc.detail, got, tc.want)
		}
		if tok := reasonToken(got); tok != tc.token {
			t.Errorf("token of %q = %q, want %q", got, tok, tc.token)
		}
	}
}

// TestLifecycle_CleanExitWithAHeldStdoutCompletes: the same escaped child,
// but hermes prints its answer and exits 0. Unbounded, Wait waits for the
// sleep. Bounded, Wait returns exec.ErrWaitDelay one grace after the exit.
// Everything hermes wrote was copied during that grace, so the task
// completes with the answer rather than failing over a cut that only lost
// the escaped child's output.
func TestLifecycle_CleanExitWithAHeldStdoutCompletes(t *testing.T) {
	task, _ := runHeldStdoutTask(t, "task-held-stdout-clean", "echo the whole answer\nexit 0", heldStdoutTaskDeadline, nil)
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s msg = %v, want completed", task.State, task.FinalMessage)
	}
	if got := task.Artifact(lib.ArtifactResult).Parts[0].Text; got != "the whole answer\n" {
		t.Fatalf("result = %q, want the stub's whole answer", got)
	}
}

// heldStdoutRun is what a mid-run hook of runHeldStdoutTask acts on.
type heldStdoutRun struct {
	client *lib.Client
	origin *lib.Envelope
	bridge *Bridge
	stop   func()
}

// runHeldStdoutTask runs one task against a stub that backgrounds a sleep
// holding stdout in its own process group, out of the group kill's reach,
// and then runs rest. With midRun set, it waits for the task to be working
// and the sleep to be started, then calls midRun (a cancel, a shutdown). It
// fails the test if the task does not finalize within
// heldStdoutTerminalWithin (an unbounded reap) or if the sleep did not
// escape the group (the premise), and returns the task and whether midRun
// ran.
func runHeldStdoutTask(t *testing.T, taskID, rest string, deadline time.Duration, midRun func(heldStdoutRun)) (*lib.Task, bool) {
	t.Helper()
	_, url := startServer(t)
	pidFile := filepath.Join(t.TempDir(), "escaped.pid")
	path := filepath.Join(t.TempDir(), "hermes-stub")
	// set -m gives the background sleep its own process group; it keeps
	// stdout and drops stderr. exec in rest keeps a foreground command in
	// the stub's own group, so the group kill reaches it.
	body := fmt.Sprintf("#!/bin/bash\nset -m\nsleep 120 </dev/null 2>/dev/null &\necho $! > %q\n%s\n", pidFile, rest)
	if err := os.WriteFile(path, []byte(body), 0o755); err != nil {
		t.Fatalf("write stub: %v", err)
	}
	b, stop := startBridgeConfig(t, Config{
		NATSURL:      url,
		Command:      []string{"/bin/bash", path},
		TaskDeadline: deadline,
		KillGrace:    heldStdoutKillGrace,
		Scope:        capability.NamespaceScope(""),
	}, nil)
	// Registered after the bridge so it runs first: an unbounded reap holds
	// the bridge's shutdown until this sleep is gone.
	t.Cleanup(func() {
		raw, err := os.ReadFile(pidFile)
		if err != nil {
			return
		}
		if pid, err := strconv.Atoi(strings.TrimSpace(string(raw))); err == nil {
			_ = syscall.Kill(pid, syscall.SIGKILL)
		}
	})
	c := gatewayClient(t, url)

	started := time.Now()
	origin := submit(t, c, taskID, "leave a child holding stdout")
	ran := false
	if midRun != nil {
		waitFor(t, heldStdoutTerminalWithin, "the stub's escaped sleep on "+taskID, func() bool {
			raw, err := os.ReadFile(pidFile)
			return err == nil && strings.TrimSpace(string(raw)) != ""
		})
		midRun(heldStdoutRun{client: c, origin: origin, bridge: b, stop: stop})
		ran = true
	}
	var task *lib.Task
	waitFor(t, heldStdoutTerminalWithin, "terminal event on "+taskID+" (an unbounded reap never sends one)", func() bool {
		got, err := c.TasksGet(testCtx(t), "platform", taskID)
		if err != nil {
			return false
		}
		task = got
		return task.Final
	})
	t.Logf("task finalized after %s", time.Since(started).Round(time.Millisecond))

	// The premise: the sleep led its own group, so only the bound ended the
	// reap. Without it the test would pass with the bound removed.
	raw, err := os.ReadFile(pidFile)
	if err != nil {
		t.Fatalf("escaped child pid: %v", err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil {
		t.Fatalf("escaped child pid %q: %v", raw, err)
	}
	if pgid, err := syscall.Getpgid(pid); err != nil || pgid != pid {
		t.Fatalf("background sleep %d is not its own process group leader (pgid %d, err %v); the test proves nothing", pid, pgid, err)
	}
	return task, ran
}
