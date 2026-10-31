package workeradapter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// recordingStub is a harness that writes, per run, its argv and the opening
// stdin line into dir (argv.<n>, prompt.<n>), reports session id
// sess-<n> at init unless the file noinit.<n> exists, and answers
// "answer <n>". extra runs between the init and the read, for a stub that
// has to check something or wait.
func recordingStub(t *testing.T, dir, extra string) []string {
	t.Helper()
	return stub(t, fmt.Sprintf(`
dir=%q
n=$(( $(cat "$dir/n" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$dir/n"
printf '%%s\n' "$*" > "$dir/argv.$n"
if [ ! -e "$dir/noinit.$n" ]; then
  echo '{"type":"system","subtype":"init","session_id":"sess-'"$n"'"}'
fi
%s
read first || exit 1
printf '%%s' "$first" > "$dir/prompt.$n"
echo '{"type":"result","subtype":"success","result":"answer '"$n"'"}'
`, dir, extra))
}

func readRun(t *testing.T, dir, what string, n int) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(dir, fmt.Sprintf("%s.%d", what, n)))
	if err != nil {
		t.Fatalf("harness run %d left no %s: %v", n, what, err)
	}
	return string(b)
}

func runCount(dir string) int {
	b, err := os.ReadFile(filepath.Join(dir, "n"))
	if err != nil {
		return 0
	}
	var n int
	fmt.Sscanf(strings.TrimSpace(string(b)), "%d", &n)
	return n
}

func reuseConfig(url, taskID, session string, harness []string) Config {
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.SessionReuse = true
	return cfg
}

// publishIn puts a gateway envelope on a task's in subject: a steer when
// text is set, a cancel otherwise.
func publishIn(t *testing.T, c *lib.Client, session, taskID, text string) {
	t.Helper()
	var env *lib.Envelope
	var err error
	if text == "" {
		env, err = lib.NewCancelEnvelope(gatewayParty, taskID, "ctx-"+taskID, "corr-"+taskID,
			lib.WithTo(lib.Party{Session: session}))
	} else {
		payload, merr := json.Marshal(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: text}},
			MessageID: "msg-steer-" + taskID, TaskID: taskID, ContextID: "ctx-" + taskID})
		if merr != nil {
			t.Fatal(merr)
		}
		env, err = lib.NewMessageEnvelope(gatewayParty, taskID, "ctx-"+taskID, "corr-"+taskID, payload,
			lib.WithTo(lib.Party{Session: session}))
	}
	if err != nil {
		t.Fatalf("envelope: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := c.Publish(ctx, lib.TaskInSubject(session, taskID), env); err != nil {
		t.Fatalf("publish: %v", err)
	}
}

func finals(t *testing.T, url, session, taskID string) int {
	t.Helper()
	n := 0
	for _, env := range replayEvents(t, url, session, taskID) {
		if env.Kind == lib.KindStatusUpdate && statusOf(t, env).Final {
			n++
		}
	}
	return n
}

// The pod stays: after the first task's terminal, the next task submitted on
// the session's subjects runs in the same process, resuming the harness
// session the first run reported, without the primer, which only the first
// run reads. A SIGTERM while idle then ends the process with nothing
// published and a zero Result (exit 0).
func TestAReusedSessionPodRunsTheNextTurnInTheSameHarnessSession(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-otter-r1u2"
	dir := t.TempDir()
	submit(t, c, session, "task-r-1", "first question")

	cfg := reuseConfig(url, "task-r-1", session, recordingStub(t, dir, ""))
	cfg.Primer = "EARLIER-TURNS"
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, cfg)

	waitState(t, c, session, "task-r-1", lib.StateCompleted)
	submit(t, c, session, "task-r-2", "second question")
	waitState(t, c, session, "task-r-2", lib.StateCompleted)
	submit(t, c, session, "task-r-3", "third question")
	waitState(t, c, session, "task-r-3", lib.StateCompleted)

	if got := artifactText(foldTask(t, c, session, "task-r-2"), lib.ArtifactResult); got != "answer 2" {
		t.Errorf("second task's result = %q, want the second harness run's", got)
	}
	if a := readRun(t, dir, "argv", 1); strings.Contains(a, harnessResumeFlag) {
		t.Errorf("the first run resumed something: %q", a)
	}
	if p := readRun(t, dir, "prompt", 1); !strings.Contains(p, "EARLIER-TURNS") || !strings.Contains(p, "first question") {
		t.Errorf("the first run's prompt lacks the primer or the question: %q", p)
	}
	for n, want := range map[int]string{2: "sess-1", 3: "sess-2"} {
		if a := readRun(t, dir, "argv", n); !strings.HasSuffix(strings.TrimSpace(a), harnessResumeFlag+" "+want) {
			t.Errorf("run %d argv = %q, want it to resume %s", n, a, want)
		}
		if p := readRun(t, dir, "prompt", n); strings.Contains(p, "EARLIER-TURNS") {
			t.Errorf("run %d read the primer again: %q", n, p)
		}
	}

	select {
	case out := <-done:
		t.Fatalf("the adapter exited while its conversation was live: %+v", out)
	case <-time.After(500 * time.Millisecond):
	}
	cancel()
	out := waitOutcome(t, done, 30*time.Second)
	if out.err != nil || out.res != (Result{}) {
		t.Fatalf("SIGTERM while idle: %+v err=%v, want a zero Result and no error", out.res, out.err)
	}
	for _, task := range []string{"task-r-1", "task-r-2", "task-r-3"} {
		if n := finals(t, url, session, task); n != 1 {
			t.Errorf("%s has %d finals, want exactly 1", task, n)
		}
	}
}

// Everything else on the session's subjects is passed over: a steer or a
// cancel for a task that has ended is not a new task, and a cancel for the
// ended task does not reach the running one. A cancel for the running task
// still stops it, and the pod then takes the next task.
func TestAReusedPodRoutesSteerAndStopToTheirOwnTask(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-heron-s7t8"
	dir := t.TempDir()
	submit(t, c, session, "task-s-1", "first")

	// Run 2 waits for its cancel; the others answer at once.
	harness := recordingStub(t, dir, `
if [ "$n" = 2 ]; then echo "$$" > "$dir/pid.2"; exec sleep 60; fi`)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var logs lockedLog
	cfg := reuseConfig(url, "task-s-1", session, harness)
	cfg.Logger = slog.New(slog.NewTextHandler(&logs, nil))
	done := runAdapter(ctx, cfg)
	waitState(t, c, session, "task-s-1", lib.StateCompleted)

	publishIn(t, c, session, "task-s-1", "a late steer for the first task")
	publishIn(t, c, session, "task-s-1", "")
	submit(t, c, session, "task-s-2", "second")
	waitState(t, c, session, "task-s-2", lib.StateWorking)
	// Run 2 has counted itself once its argv record exists; a cancel
	// landing earlier would leave task-s-3 to run as the stub's second.
	waitRun(t, dir, "argv", 2)

	// A stop aimed at the ended task leaves this one running.
	publishIn(t, c, session, "task-s-1", "")
	time.Sleep(time.Second)
	if st := foldTask(t, c, session, "task-s-2").State; st != lib.StateWorking {
		t.Fatalf("task-s-2 is %s after a cancel for task-s-1, want still working", st)
	}
	publishIn(t, c, session, "task-s-2", "")
	waitState(t, c, session, "task-s-2", lib.StateCanceled)

	submit(t, c, session, "task-s-3", "third")
	waitState(t, c, session, "task-s-3", lib.StateCompleted)
	if n := runCount(dir); n != 3 {
		t.Errorf("harness ran %d times, want 3: the late steer and cancels must not start a task", n)
	}
	if n := finals(t, url, session, "task-s-1"); n != 1 {
		t.Errorf("task-s-1 has %d finals, want 1", n)
	}
	cancel()
	waitOutcome(t, done, 30*time.Second)
	// Passed over by the watcher itself, not caught one step later by the
	// respawn check: a steer taken as a task would run it as the request
	// if the ended task's terminal had not reached the stream.
	for _, line := range strings.Split(logs.String(), "\n") {
		if strings.Contains(line, "next task taken") && strings.Contains(line, "task=task-s-1 ") {
			t.Fatalf("the watcher took a late steer or cancel for an ended task as a new task: %s", line)
		}
	}
}

// lockedLog is a log sink the adapter's goroutines and the test can share.
type lockedLog struct {
	mu  sync.Mutex
	buf strings.Builder
}

func (l *lockedLog) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buf.Write(p)
}

func (l *lockedLog) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buf.String()
}

// SIGTERM in the middle of a later task is that task's eviction, exactly as
// in a one-task pod.
func TestASIGTERMMidTaskInAReusedPodIsAnEviction(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-lynx-e1v2"
	dir := t.TempDir()
	submit(t, c, session, "task-e-1", "first")
	harness := recordingStub(t, dir, `if [ "$n" = 2 ]; then exec sleep 60; fi`)
	ctx, cancel := context.WithCancel(context.Background())
	done := runAdapter(ctx, reuseConfig(url, "task-e-1", session, harness))
	waitState(t, c, session, "task-e-1", lib.StateCompleted)
	submit(t, c, session, "task-e-2", "second")
	waitState(t, c, session, "task-e-2", lib.StateWorking)
	cancel()
	out := waitOutcome(t, done, 30*time.Second)
	if !out.res.Evicted || out.res.State != lib.StateFailed {
		t.Fatalf("run: %+v err=%v, want the eviction contract", out.res, out.err)
	}
	task := foldTask(t, c, session, "task-e-2")
	if !task.Final || task.State != lib.StateFailed {
		t.Fatalf("task-e-2: final=%v state=%s", task.Final, task.State)
	}
}

// A first harness run that reports no session leaves nothing to resume, and
// a next turn with neither the session nor the primer would know nothing of
// the conversation; a resumed run that ends without one, other than by a
// stop, may have failed on the session it was given. The process exits after
// that task's terminal, so the next turn starts cold, with the primer.
func TestAReusedPodExitsWhenTheHarnessReportsNoSession(t *testing.T) {
	for _, tc := range []struct {
		name   string
		noInit int
		tasks  int
	}{
		{"on the first turn", 1, 1},
		{"on a resumed turn that was not stopped", 2, 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			url := startServer(t)
			c := testClient(t, url)
			session := fmt.Sprintf("chat-vole-n%d", tc.noInit)
			dir := t.TempDir()
			if err := os.WriteFile(filepath.Join(dir, fmt.Sprintf("noinit.%d", tc.noInit)), nil, 0o600); err != nil {
				t.Fatal(err)
			}
			submit(t, c, session, "task-n-1", "first")
			done := runAdapter(context.Background(), reuseConfig(url, "task-n-1", session, recordingStub(t, dir, "")))
			for i := 2; i <= tc.tasks; i++ {
				waitState(t, c, session, fmt.Sprintf("task-n-%d", i-1), lib.StateCompleted)
				submit(t, c, session, fmt.Sprintf("task-n-%d", i), "next")
			}
			out := waitOutcome(t, done, 30*time.Second)
			if out.err != nil || out.res.State != lib.StateCompleted {
				t.Fatalf("run: %+v err=%v, want the last task's completed terminal", out.res, out.err)
			}
		})
	}
}

// The delegate tool follows the task, not the pod: a wake (the gateway's
// task on the session's behalf, marked by authority.via) runs with the tool
// off, and the human turns either side of it in the same pod keep it. Off
// means what A2A_DELEGATE_TOOL=off means for a whole pod: the no-delegate
// argv, and no listener on the socket.
func TestTheDelegateToolIsDecidedPerTaskInAReusedPod(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-stoat-d3w4"
	dir := t.TempDir()
	sock := delegateSock(t)
	// A reused pod's turn listens on a path of its own, which it is told in
	// A2A_DELEGATE_SOCKET; a turn with the tool off is told nothing.
	check := `if [ -n "$A2A_DELEGATE_SOCKET" ] && [ -S "$A2A_DELEGATE_SOCKET" ]; then echo listening > "$dir/sock.$n"; else echo absent > "$dir/sock.$n"; fi`
	with := recordingStub(t, dir, "echo WITH > \"$dir/tool.$n\"\n"+check)
	without := recordingStub(t, dir, "echo WITHOUT > \"$dir/tool.$n\"\n"+check)

	submit(t, c, session, "task-d-1", "human turn")
	cfg := reuseConfig(url, "task-d-1", session, with)
	cfg.HarnessCommandNoDelegate = without
	cfg.DelegateSocket = sock
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, cfg)
	waitState(t, c, session, "task-d-1", lib.StateCompleted)

	wake := authorityWithVia(t, c, "task-d-2", session)
	submitWithAuthority(t, c, session, "task-d-2", "the delegation came back", wake)
	waitState(t, c, session, "task-d-2", lib.StateCompleted)
	submit(t, c, session, "task-d-3", "human again")
	waitState(t, c, session, "task-d-3", lib.StateCompleted)

	for n, want := range map[int][2]string{1: {"WITH", "listening"}, 2: {"WITHOUT", "absent"}, 3: {"WITH", "listening"}} {
		if got := strings.TrimSpace(readRun(t, dir, "tool", n)); got != want[0] {
			t.Errorf("run %d used the %s argv, want %s", n, got, want[0])
		}
		if got := strings.TrimSpace(readRun(t, dir, "sock", n)); got != want[1] {
			t.Errorf("run %d saw the delegate socket %s, want %s", n, got, want[1])
		}
	}
	cancel()
	waitOutcome(t, done, 30*time.Second)
}

// authorityWithVia is the authority block wakeSession publishes: the task's
// capability, and via naming the delegated task and the session it ran on.
func authorityWithVia(t *testing.T, c *lib.Client, taskID, session string) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(map[string]any{
		"grants": map[string]any{"capability": mintFor(t, c, taskID, session)},
		"via":    map[string]string{"taskId": "task-child-1", "session": session},
	})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestDelegateOffForTaskReadsTheWakeMark(t *testing.T) {
	for _, tc := range []struct {
		name      string
		authority string
		off       bool
	}{
		{"no authority block", "", false},
		{"a human turn", `{"requester":{"principal":"p"},"grants":null}`, false},
		{"via null", `{"via":null,"grants":null}`, false},
		{"a wake", `{"via":{"taskId":"t","session":"s"},"grants":null}`, true},
		{"an unreadable block", `{"via":`, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			env := &lib.Envelope{Authority: json.RawMessage(tc.authority)}
			if got := delegateOffForTask(env); got != tc.off {
				t.Errorf("delegateOffForTask(%s) = %v, want %v", tc.authority, got, tc.off)
			}
		})
	}
}

// The watcher's consumer is a named ephemeral like the others, so a
// nats-server restart or a long disconnect drops it while the pod is idle.
// The next task must still arrive: the watcher rebuilds it from where it
// left off.
func TestAReusedPodSurvivesItsWatcherConsumerBeingDropped(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-marten-w5x6"
	dir := t.TempDir()
	submit(t, c, session, "task-w-1", "first")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var logs lockedLog
	cfg := reuseConfig(url, "task-w-1", session, recordingStub(t, dir, ""))
	cfg.Logger = slog.New(slog.NewTextHandler(&logs, nil))
	done := runAdapter(ctx, cfg)
	waitState(t, c, session, "task-w-1", lib.StateCompleted)

	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	name := lib.SessionConsumerName(session, lib.SessionConsumerOrigin)
	dctx, dcancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer dcancel()
	deadline := time.Now().Add(waitDeadline)
	for {
		if err := js.DeleteConsumer(dctx, lib.TasksStream, name); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the watcher's consumer %s never appeared", name)
		}
		time.Sleep(50 * time.Millisecond)
	}
	submit(t, c, session, "task-w-2", "second")
	waitState(t, c, session, "task-w-2", lib.StateCompleted)
	cancel()
	waitOutcome(t, done, 30*time.Second)

	// The recovery is logged, after the loss, so a live run shows the
	// watcher came back and not only that it went.
	lost, back := -1, -1
	lines := strings.Split(logs.String(), "\n")
	for n, line := range lines {
		if lost < 0 && strings.Contains(line, "task watcher lost its consumer; recreating") {
			lost = n
		}
		if lost >= 0 && strings.Contains(line, `msg="task watcher recreated its consumer"`) {
			back = n
			break
		}
	}
	if lost < 0 {
		t.Fatalf("no loss line logged; the consumer drop was never seen:\n%s", logs.String())
	}
	if back < 0 {
		t.Fatalf("no recovery line logged after the loss line:\n%s", logs.String())
	}
	line := lines[back]
	for _, want := range []string{"level=INFO", "filter=a2a.tasks.", "resumeSeq="} {
		if !strings.Contains(line, want) {
			t.Errorf("recovery line lacks %q: %s", want, line)
		}
	}
}

// A profile pod's addressee is shared by every pod of its profile, so a
// watcher there would take its siblings' tasks.
func TestSessionReuseIsRefusedWithoutASessionOfItsOwn(t *testing.T) {
	cfg := Config{SessionReuse: true, ProfileExecutor: true, Profile: "p", PodName: "p-1"}
	if err := cfg.validate(); err == nil {
		t.Fatal("SessionReuse on a profile pod validated")
	}
	cfg = Config{SessionReuse: true, Profile: "p"}
	if err := cfg.validate(); err == nil {
		t.Fatal("SessionReuse with no session validated")
	}
}

// A reused pod's watcher refuses a new task it cannot show it has the
// submission for. When the per-subject cap has evicted the submission, the
// oldest surviving steer comes first, and running it as the request is what
// the refusal stops. A submission with a person's few follow-ups already
// behind it (a wake still queued behind the turn before it) is taken.
func TestTheWatcherRefusesANewTaskItCannotShowIsASubmission(t *testing.T) {
	const session, taskID = "sess", "t1"
	// renderCap is the render's per-subject cap; evictingCap is the smallest
	// on which the check holds, so its cap plus one messages evict the
	// submission and leave exactly the bound behind the first survivor.
	const renderCap, evictingCap = 4096, maxPendingBehindSubmission + 1
	steers := func(n int) []string {
		out := []string{"the request"}
		for i := 1; i <= n; i++ {
			out = append(out, fmt.Sprintf("steer %d", i))
		}
		return out
	}
	for _, tc := range []struct {
		name     string
		cap      int64
		messages []string
		take     string // the text taken as the submission; "" for a refusal
	}{
		{name: "the submission alone", cap: renderCap, messages: steers(0), take: "the request"},
		{name: "follow-ups already behind the submission", cap: renderCap, messages: steers(3), take: "the request"},
		{name: "the submission evicted", cap: evictingCap, messages: steers(evictingCap)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			js, cleanup := capTasksStream(t, tc.cap)
			defer cleanup()
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			for _, text := range tc.messages {
				publishMessage(ctx, t, js, lib.TaskInSubject(session, taskID), text)
			}
			w := &taskWatcher{js: js, log: slog.Default(), stem: session, addr: session, next: 1, handled: map[string]bool{}}
			env, _, err := w.nextTask(ctx)
			if tc.take == "" {
				if !errors.Is(err, errSubmissionUnproven) {
					t.Fatalf("nextTask = %v, %v; want a refusal", env, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("nextTask: %v", err)
			}
			if !strings.Contains(string(env.Payload), tc.take) {
				t.Fatalf("took %s, want %q", env.Payload, tc.take)
			}
		})
	}
}

// A background process the model leaves behind in a clean turn does not live
// into the next one: once the harness has exited and been reaped, what is left
// of its process group is killed (the code does it before the turn's outcome
// is handed back; this test shows only that it dies).
func TestAReusedPodKillsWhatATurnLeftBehind(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-ferret-k1l2"
	dir := t.TempDir()
	// The leftover lets go of the harness's stdout, as a background job
	// started through a tool does, so the harness's own exit ends the turn.
	harness := recordingStub(t, dir, `sleep 300 >/dev/null 2>&1 &
echo $! > "$dir/bg.$n"`)
	submit(t, c, session, "task-k-1", "start something")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, reuseConfig(url, "task-k-1", session, harness))
	waitState(t, c, session, "task-k-1", lib.StateCompleted)

	var pid int
	if _, err := fmt.Sscanf(strings.TrimSpace(readRun(t, dir, "bg", 1)), "%d", &pid); err != nil || pid <= 0 {
		t.Fatalf("no background pid recorded: %v", err)
	}
	t.Cleanup(func() { _ = syscall.Kill(pid, syscall.SIGKILL) })
	deadline := time.Now().Add(waitDeadline)
	for syscall.Kill(pid, 0) == nil {
		if time.Now().After(deadline) {
			t.Fatalf("the background process %d from the first turn is still alive after the turn ended", pid)
		}
		time.Sleep(50 * time.Millisecond)
	}
	cancel()
	waitOutcome(t, done, 30*time.Second)
}

// The real harness's Bash tool spawns each command detached, in a session and
// process group of its own, so the group kill above never reaches it. As the
// worker container's pid 1 the adapter also kills every other process in the
// namespace after each turn, and reaps them. A test cannot be pid 1, and
// kill(-1) from here would reach every process the user owns, so the
// namespace-wide kill is stood in for by one that kills the leftover the stub
// recorded: what this shows is that a leftover outside the group survives the
// group kill, and that the namespace-wide kill and the reap run, after the
// harness is reaped, only when the adapter is pid 1.
func TestAReusedPodAsInitKillsWhatLeftTheGroup(t *testing.T) {
	if _, err := exec.LookPath("perl"); err != nil {
		t.Skip("perl is needed to start a process in a session of its own")
	}
	for _, asInit := range []bool{false, true} {
		t.Run(fmt.Sprintf("pid1=%v", asInit), func(t *testing.T) {
			url := startServer(t)
			c := testClient(t, url)
			const session = "chat-ferret-s3d4"
			dir := t.TempDir()
			// setsid first, as the Bash tool's detached spawn does, then
			// let the stub record the pid once the setsid has landed.
			harness := recordingStub(t, dir, `/usr/bin/env perl -MPOSIX -e 'POSIX::setsid(); exec "sleep", "300"' >/dev/null 2>&1 &
echo $! > "$dir/bg.$n"
sleep 0.5`)
			var killed, reapedAll atomic.Int32
			pidOf := func() int {
				var pid int
				fmt.Sscanf(strings.TrimSpace(readRun(t, dir, "bg", 1)), "%d", &pid)
				return pid
			}
			prevPID, prevKill, prevReap := adapterPID, killEveryoneButMe, reapKilledChildren
			t.Cleanup(func() { adapterPID, killEveryoneButMe, reapKilledChildren = prevPID, prevKill, prevReap })
			if asInit {
				adapterPID = func() int { return containerInitPID }
			}
			killEveryoneButMe = func() {
				killed.Add(1)
				if pid := pidOf(); pid > 0 {
					_ = syscall.Kill(pid, syscall.SIGKILL)
				}
			}
			reapKilledChildren = func(time.Duration) { reapedAll.Add(1) }

			submit(t, c, session, "task-s-1", "start something detached")
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := runAdapter(ctx, reuseConfig(url, "task-s-1", session, harness))
			waitState(t, c, session, "task-s-1", lib.StateCompleted)
			pid := pidOf()
			if pid <= 0 {
				t.Fatal("no background pid recorded")
			}
			t.Cleanup(func() { _ = syscall.Kill(pid, syscall.SIGKILL) })

			alive := func() bool { return syscall.Kill(pid, 0) == nil }
			if !asInit {
				time.Sleep(500 * time.Millisecond)
				if !alive() {
					t.Fatal("a leftover in its own session died without the namespace-wide kill; this test no longer shows what the group kill misses")
				}
				if killed.Load() != 0 || reapedAll.Load() != 0 {
					t.Fatalf("not pid 1, but the namespace-wide kill ran %d time(s) and the reap %d", killed.Load(), reapedAll.Load())
				}
			} else {
				deadline := time.Now().Add(waitDeadline)
				for alive() {
					if time.Now().After(deadline) {
						t.Fatalf("the detached leftover %d is still alive after the turn ended", pid)
					}
					time.Sleep(50 * time.Millisecond)
				}
				if killed.Load() != 1 || reapedAll.Load() != 1 {
					t.Fatalf("as pid 1: namespace-wide kill ran %d time(s), reap %d, want 1 each", killed.Load(), reapedAll.Load())
				}
			}
			cancel()
			waitOutcome(t, done, 30*time.Second)
		})
	}
}

// reapChildren takes every exited child and returns once there are none,
// without waiting out its bound.
func TestReapChildrenTakesExitedChildren(t *testing.T) {
	cmd := exec.Command("/bin/sh", "-c", "exit 0")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	reapChildren(5 * time.Second)
	if took := time.Since(start); took > 4*time.Second {
		t.Fatalf("reapChildren waited %v with nothing left to reap", took)
	}
	if err := cmd.Wait(); err == nil {
		t.Fatal("the child was still there for os/exec to wait on; reapChildren did not take it")
	}
}

// Each turn of a reused pod has its own delegate socket, and the path goes
// with the turn: whatever kept the first turn's path, or knows the configured
// one, cannot reach the second turn's listener.
func TestEachReusedTurnHasItsOwnDelegateSocket(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-ferret-s3o4"
	dir := t.TempDir()
	sock := delegateSock(t)
	harness := recordingStub(t, dir, `echo "$A2A_DELEGATE_SOCKET" > "$dir/sock.$n"
if [ "$n" = 2 ]; then while [ ! -e "$dir/go" ]; do sleep 0.1; done; fi`)
	submit(t, c, session, "task-s-1", "first")
	cfg := reuseConfig(url, "task-s-1", session, harness)
	cfg.DelegateSocket = sock
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, cfg)
	waitState(t, c, session, "task-s-1", lib.StateCompleted)

	first := strings.TrimSpace(readRun(t, dir, "sock", 1))
	if first == "" || first == sock || filepath.Dir(first) != filepath.Dir(sock) {
		t.Fatalf("first turn's socket = %q, want a fresh name beside %s", first, sock)
	}
	if _, err := os.Stat(first); !os.IsNotExist(err) {
		t.Fatalf("the first turn's socket %s outlived its turn (stat err=%v)", first, err)
	}

	submit(t, c, session, "task-s-2", "second")
	second := waitRun(t, dir, "sock", 2)
	if second == first || second == sock {
		t.Fatalf("second turn's socket = %q, the same as an earlier or the configured path", second)
	}
	if fi, err := os.Stat(second); err != nil || fi.Mode()&os.ModeSocket == 0 {
		t.Fatalf("the second turn is not listening at %s: %v", second, err)
	}
	for _, old := range []string{first, sock} {
		if conn, err := net.Dial("unix", old); err == nil {
			conn.Close()
			t.Fatalf("dialing %s reached a listener during the second turn", old)
		}
	}
	if err := os.WriteFile(filepath.Join(dir, "go"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	waitState(t, c, session, "task-s-2", lib.StateCompleted)
	cancel()
	waitOutcome(t, done, 30*time.Second)
}

// readRunIfAny is readRun for a file that may not be written yet.
func readRunIfAny(dir, what string, n int) string {
	b, _ := os.ReadFile(filepath.Join(dir, fmt.Sprintf("%s.%d", what, n)))
	return strings.TrimSpace(string(b))
}

// waitRun is readRun for a record the stub may not have written yet: it
// polls until what.<n> holds something, bounded by waitDeadline, and fails
// naming the record that never came. A record's presence also means the
// stub has counted the run, since it writes n before any record.
func waitRun(t *testing.T, dir, what string, n int) string {
	t.Helper()
	deadline := time.Now().Add(waitDeadline)
	for {
		if v := readRunIfAny(dir, what, n); v != "" {
			return v
		}
		if time.Now().After(deadline) {
			t.Fatalf("harness run %d never wrote %s", n, what)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// withDelegateSocket rewrites the delegate server's socket inside the
// --mcp-config argument the pod builds, adds it to the environment, and leaves
// its inputs alone.
func TestWithDelegateSocketRewritesTheMCPConfig(t *testing.T) {
	cfg, err := json.Marshal(map[string]any{MCPServersKey: map[string]any{
		DelegateMCPServer: map[string]any{"command": "/bin/adapter", "args": []string{"mcp"},
			MCPEnvKey: map[string]string{EnvDelegateSocket: DefaultDelegateSocket}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	argv := []string{"claude", "--print", MCPConfigFlag, string(cfg), "--strict-mcp-config"}
	env := []string{"PATH=/bin"}
	const turnSocket = "/scratch/a2a-delegate-0123456789abcdef.sock"
	gotArgv, gotEnv, rewrote := withDelegateSocket(argv, env, turnSocket)
	if !rewrote {
		t.Fatal("the delegate server's entry was not found")
	}
	if argv[3] != string(cfg) || len(env) != 1 {
		t.Fatal("withDelegateSocket modified its inputs")
	}
	var parsed map[string]map[string]struct {
		Command string            `json:"command"`
		Env     map[string]string `json:"env"`
	}
	if err := json.Unmarshal([]byte(gotArgv[3]), &parsed); err != nil {
		t.Fatal(err)
	}
	srv := parsed[MCPServersKey][DelegateMCPServer]
	if srv.Env[EnvDelegateSocket] != turnSocket || srv.Command != "/bin/adapter" {
		t.Fatalf("delegate server after the rewrite = %+v", srv)
	}
	if !slices.Contains(gotEnv, EnvDelegateSocket+"="+turnSocket) {
		t.Fatalf("env = %v, want the turn's socket in it", gotEnv)
	}
}

// Every turn shape in a reused pod -- the first turn, a follow-up that
// resumes, and a wake with the delegate tool off -- reaches the harness with
// the flags that keep it off on-disk config (--setting-sources user and
// --strict-mcp-config, built by cmd/worker-adapter), and only a turn with the
// tool on carries --mcp-config. Each run plants a user settings file, as a
// turn's model with Write could; the next run must start without it.
func TestEveryReusedTurnRunsWithoutConfigAnEarlierTurnLeft(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-marten-c5x6"
	dir := t.TempDir()
	settings := filepath.Join(dir, "config", "settings.json")
	if err := os.MkdirAll(filepath.Dir(settings), 0o755); err != nil {
		t.Fatal(err)
	}
	plant := fmt.Sprintf(`if [ -e %[1]q ]; then echo present > "$dir/settings.$n"; else echo absent > "$dir/settings.$n"; fi
echo '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"touch /tmp/pwned"}]}]}}' > %[1]q`, settings)
	harness := recordingStub(t, dir, plant)
	offDisk := []string{"--setting-sources", "user", "--strict-mcp-config"}
	mcpCfg := `{"mcpServers":{"` + DelegateMCPServer + `":{"command":"worker-adapter","args":["mcp"],"env":{}}}}`
	with := append(append(slices.Clone(harness), offDisk...), MCPConfigFlag, mcpCfg)
	without := append(slices.Clone(harness), offDisk...)

	submit(t, c, session, "task-c-1", "human turn")
	cfg := reuseConfig(url, "task-c-1", session, with)
	cfg.HarnessCommandNoDelegate = without
	cfg.DelegateSocket = delegateSock(t)
	cfg.HarnessUserSettings = settings
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, cfg)
	waitState(t, c, session, "task-c-1", lib.StateCompleted)
	submit(t, c, session, "task-c-2", "follow-up")
	waitState(t, c, session, "task-c-2", lib.StateCompleted)
	submitWithAuthority(t, c, session, "task-c-3", "the delegation came back", authorityWithVia(t, c, "task-c-3", session))
	waitState(t, c, session, "task-c-3", lib.StateCompleted)

	for n, wantMCP := range map[int]bool{1: true, 2: true, 3: false} {
		argv := strings.Fields(readRun(t, dir, "argv", n))
		joined := strings.Join(argv, " ")
		if !strings.Contains(joined, "--setting-sources user") || !slices.Contains(argv, "--strict-mcp-config") {
			t.Errorf("run %d argv = %q, want --setting-sources user and --strict-mcp-config", n, joined)
		}
		if got := slices.Contains(argv, MCPConfigFlag); got != wantMCP {
			t.Errorf("run %d carries --mcp-config = %v, want %v", n, got, wantMCP)
		}
		if resumes := slices.Contains(argv, harnessResumeFlag); resumes != (n > 1) {
			t.Errorf("run %d resumes = %v, want %v", n, resumes, n > 1)
		}
		if got := strings.TrimSpace(readRun(t, dir, "settings", n)); got != "absent" {
			t.Errorf("run %d started with the user settings file %s, want it removed", n, got)
		}
	}
	cancel()
	waitOutcome(t, done, 30*time.Second)
}

// A settings path the adapter cannot remove (a directory planted there) is
// not run over: the turn fails before the harness starts.
func TestARemovalFailureRefusesTheTurn(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "settings.json")
	if err := removeUserSettings(path); err != nil {
		t.Fatalf("a missing file: %v, want nil", err)
	}
	if err := os.MkdirAll(filepath.Join(path, "x"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := removeUserSettings(path); err == nil {
		t.Fatal("a non-empty directory at the path was accepted")
	}
	if err := removeUserSettings(""); err != nil {
		t.Fatalf("an empty path: %v, want nil", err)
	}
}

// A resumed turn stopped before the harness's init line has reported no
// session, but the one it was resuming is still there: the pod keeps it and
// the next turn resumes it, rather than paying a cold start. A resumed run
// that ends without a session any other way may have failed on the session
// itself, and the pod exits, as on a first run.
func TestAReusedPodKeepsItsSessionWhenAResumedTurnIsStoppedBeforeInit(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-shrew-k1p2"
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "noinit.2"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	harness := recordingStub(t, dir, `if [ "$n" = 2 ]; then exec sleep 60; fi`)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	submit(t, c, session, "task-k-1", "first")
	done := runAdapter(ctx, reuseConfig(url, "task-k-1", session, harness))
	waitState(t, c, session, "task-k-1", lib.StateCompleted)
	submit(t, c, session, "task-k-2", "second")
	waitState(t, c, session, "task-k-2", lib.StateWorking)
	// working is published before the harness is launched, so the cancel
	// waits for run 2's record: a SIGTERM that lands before the stub has
	// counted itself leaves no run 2, and task-k-3 would then become it.
	// The turn is still stopped before any init line, since noinit.2 is
	// planted.
	waitRun(t, dir, "argv", 2)
	publishIn(t, c, session, "task-k-2", "")
	waitState(t, c, session, "task-k-2", lib.StateCanceled)

	submit(t, c, session, "task-k-3", "third")
	waitState(t, c, session, "task-k-3", lib.StateCompleted)
	for _, n := range []int{2, 3} {
		if a := readRun(t, dir, "argv", n); !strings.HasSuffix(strings.TrimSpace(a), harnessResumeFlag+" sess-1") {
			t.Errorf("run %d argv = %q, want it to resume sess-1", n, a)
		}
	}
	select {
	case out := <-done:
		t.Fatalf("the adapter exited after a resumed turn stopped before init: %+v err=%v", out.res, out.err)
	default:
	}
	cancel()
	waitOutcome(t, done, 30*time.Second)
}

// A turn refused before its harness runs, for a reason in the pod's own state,
// ends the pod after that turn's terminal: a non-empty directory planted at
// the user settings path would refuse every later turn the same way, so the
// next one has to start on a fresh pod.
func TestAReusedPodEndsWhenATurnIsRefusedBeforeItsHarness(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-mink-b3r4"
	dir := t.TempDir()
	settings := filepath.Join(dir, "config", "settings.json")
	submit(t, c, session, "task-b-1", "first")
	cfg := reuseConfig(url, "task-b-1", session, recordingStub(t, dir, ""))
	cfg.HarnessUserSettings = settings
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, cfg)
	waitState(t, c, session, "task-b-1", lib.StateCompleted)

	if err := os.MkdirAll(filepath.Join(settings, "planted"), 0o755); err != nil {
		t.Fatal(err)
	}
	submit(t, c, session, "task-b-2", "second")
	out := waitOutcome(t, done, 30*time.Second)
	if out.res.State != lib.StateFailed || out.res.Evicted {
		t.Fatalf("run: %+v err=%v, want the refused turn's failed terminal", out.res, out.err)
	}
	if task := foldTask(t, c, session, "task-b-2"); !task.Final || task.State != lib.StateFailed {
		t.Fatalf("task-b-2: final=%v state=%s, want failed", task.Final, task.State)
	}
	if n := runCount(dir); n != 1 {
		t.Errorf("harness ran %d times, want 1", n)
	}
}

// A message the watcher cannot read, on a task it has not run, may be that
// task's submission, so the steer after it must not be taken as one: the
// task is refused, as when the submission may have been evicted. On a task
// it has run, the same message is a late steer, and is passed over.
func TestTheWatcherRefusesATaskWhoseFirstMessageItCannotRead(t *testing.T) {
	const session = "sess"
	t.Run("an unparseable envelope", func(t *testing.T) {
		for _, tc := range []struct {
			name    string
			handled bool
		}{{"on a new task", false}, {"on a task already run", true}} {
			t.Run(tc.name, func(t *testing.T) {
				js, cleanup := capTasksStream(t, maxPendingBehindSubmission*2)
				defer cleanup()
				ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
				defer cancel()
				if _, err := js.Publish(ctx, lib.TaskInSubject(session, "t1"), []byte("not an envelope")); err != nil {
					t.Fatal(err)
				}
				publishMessage(ctx, t, js, lib.TaskInSubject(session, "t1"), "a steer")
				w := &taskWatcher{js: js, log: slog.Default(), stem: session, addr: session, next: 1,
					handled: map[string]bool{"t1": tc.handled}}
				if !tc.handled {
					if env, _, err := w.nextTask(ctx); !errors.Is(err, errSubmissionUnproven) {
						t.Fatalf("nextTask = %v, %v; want a refusal", env, err)
					}
					return
				}
				wctx, wcancel := context.WithTimeout(ctx, 2*time.Second)
				defer wcancel()
				if env, _, err := w.nextTask(wctx); env != nil || !errors.Is(err, context.DeadlineExceeded) {
					t.Fatalf("nextTask = %v, %v; want nothing taken on a task already run", env, err)
				}
			})
		}
	})
	t.Run("a message with no metadata", func(t *testing.T) {
		for _, tc := range []struct {
			name    string
			handled bool
		}{{"on a new task", false}, {"on a task already run", true}} {
			t.Run(tc.name, func(t *testing.T) {
				w := &taskWatcher{log: slog.Default(), addr: session, handled: map[string]bool{"t1": tc.handled}}
				_, _, isTask, err := w.take(noMetadataMsg{subject: lib.TaskInSubject(session, "t1")})
				if isTask {
					t.Fatal("took a message with no metadata as a task")
				}
				if got := errors.Is(err, errSubmissionUnproven); got != !tc.handled {
					t.Fatalf("take err = %v, want a refusal = %v", err, !tc.handled)
				}
				if !w.handled["t1"] {
					t.Fatal("t1 is not marked handled, so its next message would be admitted")
				}
			})
		}
	})
}

// noMetadataMsg is a delivery with no JetStream metadata.
type noMetadataMsg struct {
	jetstream.Msg
	subject string
}

func (m noMetadataMsg) Subject() string { return m.subject }
func (m noMetadataMsg) Metadata() (*jetstream.MsgMetadata, error) {
	return nil, errors.New("not a JetStream message")
}
