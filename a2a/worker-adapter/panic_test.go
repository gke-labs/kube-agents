package workeradapter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/taskguard"
)

// A panic while the adapter handles one task ends that task failed closed,
// with no answer on the bus, and the process goes on serving (panic.go).

// panicMarker in a prompt, a tool name or a stdout line makes a test's seam
// panic.
const panicMarker = "PANIC"

// wantFailedClosed checks a task a recovered panic ended: failed, its
// reason exactly taskguard's, no result artifact, exactly one final event.
func wantFailedClosed(t *testing.T, c *lib.Client, url, session, taskID string) {
	t.Helper()
	if n := finals(t, url, session, taskID); n != 1 {
		t.Fatalf("%s has %d final events, want exactly 1", taskID, n)
	}
	task := foldTask(t, c, session, taskID)
	if task.State != lib.StateFailed {
		t.Fatalf("%s state = %s, want failed", taskID, task.State)
	}
	if got := terminalText(t, url, session, taskID); got != taskguard.Reason {
		t.Fatalf("%s reason = %q, want exactly %q", taskID, got, taskguard.Reason)
	}
	if a := task.Artifact(lib.ArtifactResult); a != nil {
		t.Fatalf("%s has a result artifact %q, want none", taskID, artifactText(task, lib.ArtifactResult))
	}
}

// wantOnePanic checks the process recovered exactly one panic since before.
func wantOnePanic(t *testing.T, before int64) {
	t.Helper()
	if got := taskguard.Count() - before; got != 1 {
		t.Fatalf("recovered panics = %d, want 1", got)
	}
}

// wantGone checks the process whose pid is in path no longer exists: killed
// and reaped.
func wantGone(t *testing.T, path string) {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("the harness left no pid: %v", err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil {
		t.Fatalf("pid %q: %v", b, err)
	}
	if err := syscall.Kill(pid, 0); !errors.Is(err, syscall.ESRCH) {
		t.Fatalf("the panicked task's harness (pid %d) is still there (kill 0: %v)", pid, err)
	}
}

// panickyStub is a harness that, when its prompt carries panicMarker,
// writes its pid to dir/pid, starts a process outside its group that holds
// its stdout for holdSeconds (pid in dir/holder), so the reap waits for
// that process, reports a tool call named panicMarker, floods
// stdout with more progress lines than the event channel holds, touches
// dir/flooded, and then waits long enough that only a kill ends it;
// otherwise it answers "answer to <prompt>". It reports session sess-<n>
// at init.
func panickyStub(t *testing.T, dir string, holdSeconds int) []string {
	t.Helper()
	return stub(t, fmt.Sprintf(`
dir=%q
n=$(( $(cat "$dir/n" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$dir/n"
echo '{"type":"system","subtype":"init","session_id":"sess-'"$n"'"}'
read first || exit 1
case "$first" in
*%s*)
  echo $$ > "$dir/pid"
  perl -MPOSIX -e 'POSIX::setsid(); open(my $f, ">", $ARGV[0]); print $f $$; close $f; sleep %d' "$dir/holder" &
  while [ ! -s "$dir/holder" ]; do sleep 0.05; done
  echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"%s","input":{}}]}}'
  i=0
  while [ $i -lt 300 ]; do echo '{"type":"assistant","message":{"content":[{"type":"text","text":"after the panic"}]}}'; i=$((i+1)); done
  : > "$dir/flooded"
  sleep 60
  echo '{"type":"result","subtype":"success","result":"the late answer"}'
  ;;
*)
  echo '{"type":"result","subtype":"success","result":"answer '"$n"'"}'
  ;;
esac
`, dir, panicMarker, holdSeconds, panicMarker))
}

// panicAfterFlood is panicOn for the activity seam, panicking only once
// panickyStub has flooded stdout, so the scanner is parked on a full event
// channel when the recovery runs and only the recovery's drain frees it.
func panicAfterFlood(t *testing.T, dir string) func(string, string) {
	return func(p, detail string) {
		if p != seamActivity || !strings.Contains(detail, panicMarker) {
			return
		}
		deadline := time.Now().Add(10 * time.Second)
		for time.Now().Before(deadline) {
			if _, err := os.Stat(filepath.Join(dir, "flooded")); err == nil {
				break
			}
			time.Sleep(10 * time.Millisecond)
		}
		time.Sleep(100 * time.Millisecond)
		panic("injected panic at " + p)
	}
}

// panicOn is a panic seam that panics at point when the detail carries
// panicMarker or equals want.
func panicOn(point, want string) func(string, string) {
	return func(p, detail string) {
		if p == point && (strings.Contains(detail, panicMarker) || (want != "" && detail == want)) {
			panic("injected panic at " + p)
		}
	}
}

// A panic in supervise's event handling, in a reused session pod: the task
// ends failed closed, its harness is killed and reaped before the terminal,
// the panic is logged without its text, and the next task in the same
// process completes.
func TestPanic_SuperviseFailsTheTaskClosedAndTheSessionServesTheNext(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-panic-s1"
	dir := t.TempDir()
	submit(t, c, session, "task-ps-1", panicMarker+" question")

	logs := &lockedLog{}
	cfg := reuseConfig(url, "task-ps-1", session, panickyStub(t, dir, 1))
	cfg.TaskDeadline = 120 * time.Second
	cfg.Logger = slog.New(slog.NewJSONHandler(logs, nil))
	cfg.panicSeam = panicAfterFlood(t, dir)
	before := taskguard.Count()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, cfg)

	waitState(t, c, session, "task-ps-1", lib.StateFailed)
	// The reap, which waits for stdout to close, finished before the
	// terminal: the harness and the process holding its stdout are gone.
	wantGone(t, filepath.Join(dir, "pid"))
	wantGone(t, filepath.Join(dir, "holder"))
	wantFailedClosed(t, c, url, session, "task-ps-1")
	wantOnePanic(t, before)
	if got := artifactText(foldTask(t, c, session, "task-ps-1"), lib.ArtifactProgress); got != "" {
		t.Fatalf("progress after the panic was published: %q", got)
	}
	out := logs.String()
	if !strings.Contains(out, `"msg":"`+taskguard.LogMessage+`"`) || !strings.Contains(out, `"task":"task-ps-1"`) {
		t.Fatalf("no Error record for the recovered panic:\n%s", out)
	}
	if strings.Contains(out, "injected panic") {
		t.Fatalf("the panic's text reached the log:\n%s", out)
	}

	submit(t, c, session, "task-ps-2", "the next question")
	waitState(t, c, session, "task-ps-2", lib.StateCompleted)
	if got := artifactText(foldTask(t, c, session, "task-ps-2"), lib.ArtifactResult); got != "answer 2" {
		t.Fatalf("next task's result = %q, want answer 2", got)
	}
	select {
	case o := <-done:
		t.Fatalf("the adapter exited after the panicked task: %+v", o)
	default:
	}
	cancel()
	waitOutcome(t, done, 30*time.Second)
}

// The same panic in a profile worker's task: the task fails closed and the
// worker takes and completes the next one.
func TestPanic_SuperviseFailsTheTaskClosedAndTheProfileWorkerServesTheNext(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	d := newFakeDispatcher(t, url)
	dir := t.TempDir()
	cfg := profileWorkerConfig(t, url, panickyStub(t, dir, 1))
	cfg.panicSeam = panicOn(seamActivity, "")
	before := taskguard.Count()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	startProfileWorker(t, ctx, cfg, false)

	run := func(task, text string) {
		t.Helper()
		d.offer(task, profileSubmission(t, task, text, nil), 5)
		if a := d.nextAccept(5 * time.Second); a.TaskID != task || !a.Accepted {
			t.Fatalf("answer = %+v, want an accept of %s", a, task)
		}
		d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
		if _, final := drainLeases(d, task, 30*time.Second); !final {
			t.Fatalf("no final renewal for %s", task)
		}
	}
	run("task-pp-1", panicMarker+" question")
	wantGone(t, filepath.Join(dir, "pid"))
	wantFailedClosed(t, c, url, pwProfile, "task-pp-1")
	wantOnePanic(t, before)

	run("task-pp-2", "the next question")
	next := foldTask(t, c, pwProfile, "task-pp-2")
	if next.State != lib.StateCompleted || artifactText(next, lib.ArtifactResult) != "answer 2" {
		t.Fatalf("next task: state %s result %q, want completed with answer 2", next.State, artifactText(next, lib.ArtifactResult))
	}
}

// A panic in the result publish publishes no result: the task ends failed
// closed.
func TestPanic_ResultPublishPublishesNoResult(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-panic-r1", "task-pr-1"
	submit(t, c, session, taskID, "a question")
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-pr"}'
read first || exit 1
echo '{"type":"result","subtype":"success","result":"the secret answer"}'
`)
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.panicSeam = panicOn(seamResult, taskID)
	before := taskguard.Count()
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateFailed {
		t.Fatalf("run: state=%q err=%v, want failed and no error", out.res.State, out.err)
	}
	wantFailedClosed(t, c, url, session, taskID)
	wantOnePanic(t, before)
}

// A panic inside finalize after its claim, before its publish: the
// fallback writes the failed internal-panic terminal, exactly once, with
// none of the harness's partial output.
func TestPanic_InsideFinalizeAfterTheClaimWritesTheFallbackOnce(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-panic-f1", "task-pf-1"
	submit(t, c, session, taskID, "a question")
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-pf"}'
read first || exit 1
echo '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"secret partial output"}'
`)
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.panicSeam = panicOn(seamFinalize, taskID)
	before := taskguard.Count()
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateFailed {
		t.Fatalf("run: state=%q err=%v, want failed and no error", out.res.State, out.err)
	}
	wantFailedClosed(t, c, url, session, taskID)
	wantOnePanic(t, before)
}

// The fallback terminal is bare whatever the claim had gathered: a panic
// inside finalize after the claim, with the run's partial output and stderr
// tail already in hand as evidence, leaves none of either anywhere on the
// task's TASKS subject, in any event, only the failed internal-panic
// terminal.
func TestPanic_FallbackTerminalCarriesNothingTheClaimGathered(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-panic-f2", "task-pf-2"
	submit(t, c, session, taskID, "a question")
	const partial, tail = "evidence-partial-7d1f", "evidence-stderr-9c2e"
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-pf2"}'
read first || exit 1
echo '`+tail+`' >&2
echo '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"`+partial+`"}'
`)
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.panicSeam = panicOn(seamFinalize, taskID)
	before := taskguard.Count()
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateFailed {
		t.Fatalf("run: state=%q err=%v, want failed and no error", out.res.State, out.err)
	}
	// The scan comes first: it is what this test adds.
	for _, env := range replayEvents(t, url, session, taskID) {
		raw, err := json.Marshal(env)
		if err != nil {
			t.Fatalf("marshal %s event: %v", env.Kind, err)
		}
		for _, s := range []string{partial, tail} {
			if strings.Contains(string(raw), s) {
				t.Fatalf("the claim's evidence %q reached TASKS in a %s event: %s", s, env.Kind, raw)
			}
		}
	}
	wantFailedClosed(t, c, url, session, taskID)
	wantOnePanic(t, before)
}

// panicDecoderDriver is the toy driver with a decoder that panics on a line
// starting with panicMarker.
type panicDecoderDriver struct{ *toyDriver }

func (panicDecoderDriver) NewDecoder() Decoder { return panicDecoder{} }

type panicDecoder struct{}

func (panicDecoder) Decode(line []byte) ([]Event, error) {
	if strings.HasPrefix(string(line), panicMarker) {
		panic("injected panic decoding " + string(line))
	}
	return toyDecoder{}.Decode(line)
}

// A panic while a stdout line is decoded, on the scanner goroutine: the
// task fails closed even though a result line came first, with no partial
// output, the line is not logged, nothing after it is published, the rest
// of stdout is drained (far more than the channel and the pipe hold), and
// the harness, which would run on past its closed stdin, is killed: all
// well inside the short bound.
func TestPanic_DecodeFailsTheTaskClosedAndDrainsTheHarness(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-panic-d1", "task-pd-1"
	submit(t, c, session, taskID, "a question")
	dir := t.TempDir()
	harness := stub(t, `
echo $$ > "`+dir+`/pid"
echo 'S toy-pd'
read first || exit 1
echo 'P before the panic'
echo 'OK the secret answer'
echo '`+panicMarker+` secret line'
i=0
while [ $i -lt 5000 ]; do echo "P after the panic, line $i, padding padding padding padding"; i=$((i+1)); done
# A harness mid-turn does not stop at a closed stdin: only a kill ends it.
sleep 60
`)
	logs := &lockedLog{}
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Driver = panicDecoderDriver{&toyDriver{}}
	cfg.TaskDeadline = 120 * time.Second
	cfg.Logger = slog.New(slog.NewJSONHandler(logs, nil))
	before := taskguard.Count()
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 15*time.Second)
	if out.err != nil || out.res.State != lib.StateFailed {
		t.Fatalf("run: state=%q err=%v, want failed and no error", out.res.State, out.err)
	}
	wantFailedClosed(t, c, url, session, taskID)
	wantOnePanic(t, before)
	wantGone(t, filepath.Join(dir, "pid"))
	if got := artifactText(foldTask(t, c, session, taskID), lib.ArtifactProgress); strings.Contains(got, "after the panic") {
		t.Fatalf("progress = %q, published a line after the panic", got)
	}
	if l := logs.String(); strings.Contains(l, "secret") || !strings.Contains(l, taskguard.LogMessage) {
		t.Fatalf("log carries the line or lacks the panic record:\n%s", l)
	}
}

// A panic before the task's execution exists publishes nothing, returns
// the panic as the error, and does not take the process down.
func TestPanic_BeforeTheExecutionPublishesNothing(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-panic-e1", "task-pe-1"
	submit(t, c, session, taskID, "a question")
	cfg := adapterConfig(url, taskID, session, stub(t, "exit 0"))
	cfg.panicSeam = panicOn(seamBeforeExec, taskID)
	before := taskguard.Count()
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	var pe *taskguard.PanicError
	if !errors.As(out.err, &pe) {
		t.Fatalf("err = %v, want a *taskguard.PanicError", out.err)
	}
	if out.res != (Result{}) {
		t.Fatalf("result = %+v, want zero", out.res)
	}
	if evs := replayEvents(t, url, session, taskID); len(evs) != 0 {
		t.Fatalf("%d events published, want none", len(evs))
	}
	wantOnePanic(t, before)
}

// waitLog waits, up to waitDeadline, for the log to carry text.
func waitLog(t *testing.T, logs *lockedLog, text string) {
	t.Helper()
	deadline := time.Now().Add(waitDeadline)
	for !strings.Contains(logs.String(), text) {
		if time.Now().After(deadline) {
			t.Fatalf("the log never carried %q:\n%s", text, logs.String())
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// Events the scanner queued before a decode panic and supervise handles
// after it are dropped: the task fails closed from the scanner's mark, not
// from the end of the drain. The activity seam holds supervise on the
// first event until the panic is recovered.
func TestPanic_DecodeDropsWhatWasQueuedBeforeIt(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-panic-q1", "task-pq-1"
	submit(t, c, session, taskID, "a question")
	harness := stub(t, `
echo 'S toy-pq'
read first || exit 1
echo 'A Hold'
echo 'P queued progress'
echo 'OK the secret answer'
echo '`+panicMarker+` line'
`)
	logs := &lockedLog{}
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Driver = panicDecoderDriver{&toyDriver{}}
	cfg.Logger = slog.New(slog.NewJSONHandler(logs, nil))
	cfg.panicSeam = func(point, detail string) {
		if point != seamActivity || detail != "Hold" {
			return
		}
		deadline := time.Now().Add(10 * time.Second)
		for !strings.Contains(logs.String(), taskguard.LogMessage) && time.Now().Before(deadline) {
			time.Sleep(10 * time.Millisecond)
		}
	}
	before := taskguard.Count()
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateFailed {
		t.Fatalf("run: state=%q err=%v, want failed and no error", out.res.State, out.err)
	}
	wantFailedClosed(t, c, url, session, taskID)
	wantOnePanic(t, before)
	task := foldTask(t, c, session, taskID)
	for _, name := range []string{lib.ArtifactActivity, lib.ArtifactProgress} {
		if a := task.Artifact(name); a != nil {
			t.Errorf("%s artifact published after the decode panic: %+v", name, a.Parts)
		}
	}
}

// A SIGTERM while the scanner drains after a decode panic (a process
// outside the group holds stdout, so the drain outlasts the kill): the
// eviction's terminal is the failed internal-panic one, with none of the
// earlier result line's text as evidence, and no result.
func TestPanic_EvictionDuringTheDecodeDrainFailsClosed(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-panic-v1", "task-pv-1"
	submit(t, c, session, taskID, "a question")
	dir := t.TempDir()
	harness := stub(t, `
echo 'S toy-pv'
read first || exit 1
echo 'OK the secret answer'
perl -MPOSIX -e 'POSIX::setsid(); open(my $f, ">", $ARGV[0]); print $f $$; close $f; sleep 4' "`+dir+`/holder" &
while [ ! -s "`+dir+`/holder" ]; do sleep 0.05; done
echo '`+panicMarker+` line'
sleep 60
`)
	logs := &lockedLog{}
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Driver = panicDecoderDriver{&toyDriver{}}
	cfg.Logger = slog.New(slog.NewJSONHandler(logs, nil))
	before := taskguard.Count()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, cfg)
	// The decode-panic mark is set before the recovery's log record.
	waitLog(t, logs, taskguard.LogMessage)
	cancel()
	out := waitOutcome(t, done, 15*time.Second)
	if out.res.State != lib.StateFailed {
		t.Fatalf("run: state=%q err=%v, want failed", out.res.State, out.err)
	}
	wantFailedClosed(t, c, url, session, taskID)
	wantOnePanic(t, before)
}

// panicEncodeDriver is the toy driver with an encoder that panics, once
// the harness has written its pid to dir/pid.
type panicEncodeDriver struct {
	*toyDriver
	dir string
}

func (d panicEncodeDriver) EncodeUser(string) ([]byte, error) {
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if b, err := os.ReadFile(filepath.Join(d.dir, "pid")); err == nil && len(b) > 0 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	panic("injected panic encoding the prompt")
}

// A panic writing the opening prompt, after the harness started and before
// supervise holds it: the harness is killed and reaped all the same, and
// the task fails closed.
func TestPanic_OpeningPromptEncodeKillsTheStartedHarness(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-panic-o1", "task-po-1"
	submit(t, c, session, taskID, "a question")
	dir := t.TempDir()
	harness := stub(t, `
echo $$ > "`+dir+`/pid"
sleep 60
`)
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Driver = panicEncodeDriver{&toyDriver{}, dir}
	before := taskguard.Count()
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateFailed {
		t.Fatalf("run: state=%q err=%v, want failed and no error", out.res.State, out.err)
	}
	wantGone(t, filepath.Join(dir, "pid"))
	wantFailedClosed(t, c, url, session, taskID)
	wantOnePanic(t, before)
}

// A panicked task whose harness can't be reaped in time (a process outside
// its group holds its stdout far past the bound): the terminal goes out
// anyway, and a reused pod ends instead of taking another task beside it.
func TestPanic_AnUnreapedHarnessEndsTheReusedPod(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-panic-u1"
	dir := t.TempDir()
	submit(t, c, session, "task-pu-1", panicMarker+" question")
	cfg := reuseConfig(url, "task-pu-1", session, panickyStub(t, dir, 20))
	cfg.TaskDeadline = 120 * time.Second
	cfg.panicSeam = panicAfterFlood(t, dir)
	t.Cleanup(func() {
		if b, err := os.ReadFile(filepath.Join(dir, "holder")); err == nil {
			if pid, err := strconv.Atoi(strings.TrimSpace(string(b))); err == nil {
				_ = syscall.Kill(pid, syscall.SIGKILL)
			}
		}
	})
	before := taskguard.Count()
	// Well before the holder lets go (20s): the bound is KillGrace plus
	// leftoverReapBound.
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 12*time.Second)
	if out.res.State != lib.StateFailed {
		t.Fatalf("run: state=%q err=%v, want failed", out.res.State, out.err)
	}
	wantFailedClosed(t, c, url, session, "task-pu-1")
	wantOnePanic(t, before)
}

// A one-turn-per-process driver swaps the task's harness between turns
// (respawn). A decode panic in the process being replaced keeps the task
// failing closed after the swap: the mark moves onto the adapter.
func TestPanic_ADecodePanicSurvivesTheNextProcess(t *testing.T) {
	a := &adapter{}
	first := &harnessProc{}
	a.setHarness(first)
	first.decodePanicked.Store(true)
	if !a.failClosed() {
		t.Fatal("a decode panic in the running process doesn't fail the task closed")
	}
	a.setHarness(&harnessProc{})
	if !a.failClosed() {
		t.Fatal("after the next process replaced the one whose decode panicked, the task no longer fails closed")
	}
}
