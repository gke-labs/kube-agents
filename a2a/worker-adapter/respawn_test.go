package workeradapter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// respawnScope stands the adapter in as the container's init, with the
// namespace-wide kill and the process listing scoped to the pids it tracks,
// so a test never signals anything it did not start.
type respawnScope struct {
	mu   sync.Mutex
	pids []int
}

func (s *respawnScope) track(pid int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pids = append(s.pids, pid)
}

func (s *respawnScope) alive() []int {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []int
	for _, pid := range s.pids {
		if syscall.Kill(pid, 0) == nil && !zombieProc(pid) {
			out = append(out, pid)
		}
	}
	return out
}

// useToyRespawn points cfg at the toy driver taking one turn per process,
// with a wall-clock budget and the boundary check as its PrepareTurn, and
// scopes the kill and the listing to what the test tracks.
func useToyRespawn(t *testing.T, cfg *Config) (*respawnScope, *toyDriver) {
	t.Helper()
	d := &toyDriver{hold: true, restart: true, budget: true, prepare: func(int, TurnDirs) error { return checkTurnBoundary() }}
	cfg.Driver = d
	return useRespawnScope(t), d
}

// useRespawnScope stands the adapter in as the container's init, with the
// kill and the process listing scoped to the pids the returned scope tracks.
func useRespawnScope(t *testing.T) *respawnScope {
	t.Helper()
	s := &respawnScope{}
	prevPID, prevKill, prevReap, prevOthers, prevSettle := adapterPID, killEveryoneButMe, reapKilledChildren, otherProcesses, sweepSettle
	t.Cleanup(func() {
		adapterPID, killEveryoneButMe, reapKilledChildren, otherProcesses, sweepSettle = prevPID, prevKill, prevReap, prevOthers, prevSettle
		for _, pid := range s.alive() {
			_ = syscall.Kill(pid, syscall.SIGKILL)
		}
	})
	adapterPID = func() int { return containerInitPID }
	killEveryoneButMe = func() {
		for _, pid := range s.alive() {
			_ = syscall.Kill(pid, syscall.SIGKILL)
		}
	}
	reapKilledChildren = func(bound time.Duration) {
		for deadline := time.Now().Add(bound); len(s.alive()) > 0 && time.Now().Before(deadline); {
			time.Sleep(10 * time.Millisecond)
		}
	}
	otherProcesses = func() ([]int, error) { return s.alive(), nil }
	sweepSettle = 300 * time.Millisecond
	return s
}

// toyRespawnStub is a toy-dialect harness for one turn per process. Each
// run, n counting from 1, records its argv in $dir/argv.<n>, reads its one
// user line, says "S conv-t", marks turn one started, waits for
// $dir/steer-sent, then acts on $dir/mode:
//   - error: a failed result;
//   - linger: a clean result, then it stays up until SIGTERM, on which it
//     exits 0, so a stop after its result leaves a clean exit;
//   - refusal: a clean result, then a write refusal record and a tool
//     step's result after it;
//   - writer: a clean result after leaving a process in its own session
//     (pid in $dir/writer) that the boundary must end;
//   - double: a clean result, then a second one once its stdin closes, so
//     the second decides the task while a steer waits for the next process;
//   - delegate: no result; it waits mid-turn (the test delegates on the
//     adapter's socket) and exits 0 on the delegation's SIGTERM;
//   - anything else: a clean result.
//
// It then reads stdin to its end and exits with $dir/exitcode (0 if none).
func toyRespawnStub(t *testing.T, dir, taskID string) []string {
	t.Helper()
	record := refusalRecord(t, true, kubernetesReadOnlyRule, "scale", taskID)
	return stub(t, fmt.Sprintf(`
dir=%q
n=$(( $(cat "$dir/runs" 2>/dev/null || echo 0) + 1 )); echo $n > "$dir/runs"
echo "$*" > "$dir/argv.$n"
mode=$(cat "$dir/mode" 2>/dev/null)
read first || exit 1
echo 'S conv-t'
echo 1 > "$dir/turn1.$n"
i=0; while [ ! -e "$dir/steer-sent" ] && [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done
case "$mode" in
error) echo 'ERR boom it failed' ;;
linger) echo "OK turn $n"; echo 1 > "$dir/result"; trap 'exit 0' TERM; while :; do sleep 0.05; done ;;
refusal) echo "OK turn $n"; sleep 0.3; printf '%%s\n' %q >> "$%s"; echo 'R' ;;
writer)
  if [ $n = 1 ]; then /usr/bin/perl -MPOSIX -e 'if (fork) { exit 0 } POSIX::setsid(); open(STDIN,"<","/dev/null"); open(STDOUT,">","/dev/null"); open(STDERR,">","/dev/null"); open(my $f, ">", $ARGV[0]) or die; print $f "$$\n"; close($f); sleep 60' "$dir/writer"; fi
  echo "OK turn $n" ;;
double) echo "OK turn $n"; while read -r more; do :; done; echo "OK again $n" ;;
delegate) echo 1 > "$dir/mid-turn"; trap 'exit 0' TERM; while :; do sleep 0.05; done ;;
*) echo "OK turn $n" ;;
esac
while read -r more; do :; done
exit $(cat "$dir/exitcode" 2>/dev/null || echo 0)
`, dir, record, EnvWriteRefusalFile))
}

const respawnSteer = "a steer during turn one"

// respawnResult is one respawnRun: the outcome, the dir with the stub's
// records, how many processes ran, and where to replay the task's events.
type respawnResult struct {
	out              runOutcome
	dir              string
	runs             int
	url, session, id string
}

// refused reports whether a steer with text was refused on the task's
// stream.
func (r respawnResult) refused(t *testing.T, text string) bool {
	t.Helper()
	for _, env := range replayEvents(t, r.url, r.session, r.id) {
		if p := string(env.Payload); strings.Contains(p, "steer refused") && strings.Contains(p, text) {
			return true
		}
	}
	return false
}

// respawnRun runs one toy task in mode with a steer held during turn one,
// calling trigger once the steer is sent.
func respawnRun(t *testing.T, mode string, exitCode int, setup func(*Config, *respawnScope), trigger func(c *lib.Client, origin *lib.Envelope, session, taskID, dir string)) respawnResult {
	t.Helper()
	dir := t.TempDir()
	must := func(err error) {
		if err != nil {
			t.Fatal(err)
		}
	}
	must(os.WriteFile(filepath.Join(dir, "mode"), []byte(mode), 0o644))
	if exitCode != 0 {
		must(os.WriteFile(filepath.Join(dir, "exitcode"), []byte(strconv.Itoa(exitCode)), 0o644))
	}
	label := strings.ToLower(mode) + "-" + strconv.Itoa(exitCode) + "-" + strconv.FormatInt(time.Now().UnixNano()%1_000_000, 10)
	session, taskID := "chat-respawn-"+label, "task-respawn-"+label
	url := startServer(t)
	c := testClient(t, url)
	origin := submit(t, c, session, taskID, "start")
	cfg := adapterConfig(url, taskID, session, toyRespawnStub(t, dir, taskID))
	s, _ := useToyRespawn(t, &cfg)
	if setup != nil {
		setup(&cfg, s)
	}
	done := runAdapter(context.Background(), cfg)
	waitFileFor(t, filepath.Join(dir, "turn1.1"), 10*time.Second)
	publishSteerFor(t, c, origin, session, respawnSteer)
	time.Sleep(300 * time.Millisecond) // the steer reaches the adapter and is held
	must(os.WriteFile(filepath.Join(dir, "steer-sent"), []byte("1"), 0o644))
	if trigger != nil {
		trigger(c, origin, session, taskID, dir)
	}
	out := waitOutcome(t, done, 45*time.Second)
	b, _ := os.ReadFile(filepath.Join(dir, "runs"))
	runs, _ := strconv.Atoi(strings.TrimSpace(string(b)))
	return respawnResult{out: out, dir: dir, runs: runs, url: url, session: session, id: taskID}
}

func waitFileFor(t *testing.T, path string, bound time.Duration) string {
	t.Helper()
	for deadline := time.Now().Add(bound); time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		if b, err := os.ReadFile(path); err == nil && len(strings.TrimSpace(string(b))) > 0 {
			return strings.TrimSpace(string(b))
		}
	}
	t.Fatalf("%s never appeared", path)
	return ""
}

// TestAHeldSteerStartsAFreshProcessOnTheSameConversation: a steer held
// during turn one is not written to that process. It ends at its result,
// and a fresh process resumes the conversation with the steer, with less
// time left than the first; no process takes a second turn.
func TestAHeldSteerStartsAFreshProcessOnTheSameConversation(t *testing.T) {
	r := respawnRun(t, "clean", 0, nil, nil)
	if r.out.err != nil || r.out.res.State != lib.StateCompleted {
		t.Fatalf("state=%q err=%v", r.out.res.State, r.out.err)
	}
	if r.runs != 2 {
		t.Fatalf("%d processes; want 2 (one per user turn)", r.runs)
	}
	argv2 := readRunIfAny(r.dir, "argv", 2)
	if !strings.Contains(argv2, "--again conv-t") {
		t.Errorf("run 2 argv %q does not resume conv-t", argv2)
	}
	// Each process carries exactly one budget, and the second's is the
	// time left, less than the first's.
	budget := func(argv string) time.Duration {
		f := strings.Fields(argv)
		var got []time.Duration
		for i := range f {
			if f[i] == "--left" && i+1 < len(f) {
				d, err := time.ParseDuration(f[i+1])
				if err != nil {
					t.Fatalf("argv %q: %v", argv, err)
				}
				got = append(got, d)
			}
		}
		if len(got) != 1 {
			t.Fatalf("argv %q carries %d budgets; want 1", argv, len(got))
		}
		return got[0]
	}
	if b1, b2 := budget(readRunIfAny(r.dir, "argv", 1)), budget(argv2); b2 >= b1 {
		t.Errorf("run 2's budget %v is not less than run 1's %v", b2, b1)
	}
}

// TestALeftoverFromOneProcessIsGoneBeforeTheNext: the process a turn left
// in its own session is ended (killLeftovers after the exit) and the
// boundary check passes before the next process starts.
func TestALeftoverFromOneProcessIsGoneBeforeTheNext(t *testing.T) {
	if _, err := os.Stat("/usr/bin/perl"); err != nil {
		t.Skip("needs perl for setsid")
	}
	var scope *respawnScope
	r := respawnRun(t, "writer", 0, func(_ *Config, s *respawnScope) { scope = s }, func(_ *lib.Client, _ *lib.Envelope, _, _, dir string) {
		pid, _ := strconv.Atoi(waitFileFor(t, filepath.Join(dir, "writer"), 10*time.Second))
		scope.track(pid)
	})
	if r.out.err != nil || r.out.res.State != lib.StateCompleted || r.runs != 2 {
		t.Fatalf("state=%q err=%v runs=%d", r.out.res.State, r.out.err, r.runs)
	}
	if alive := scope.alive(); len(alive) > 0 {
		t.Fatalf("the leftover %v outlived its process", alive)
	}
}

// TestNoSupervisorStopStartsAnotherProcess walks every supervisorStop: a
// task stopped any way the supervisor knows, with a steer held, runs no
// further process, and the steer is refused visibly. A stop the toy
// cannot reach says why; a stop added without a case fails.
func TestNoSupervisorStopStartsAnotherProcess(t *testing.T) {
	type stopCase struct {
		run         func(t *testing.T) respawnResult
		want        lib.TaskState
		unreachable string
	}
	cases := map[supervisorStop]stopCase{
		// The steer is taken for the next process at the first result; the
		// second result, after stdin closes, decides the task.
		stopDecided: {run: func(t *testing.T) respawnResult { return respawnRun(t, "double", 0, nil, nil) }, want: lib.StateCompleted},
		// Canceled and deadline land after the result, while the process
		// lingers, and it exits 0 on their SIGTERM: the clean-exit check
		// passes, so only the stop registry refuses the steer.
		stopCanceled: {run: func(t *testing.T) respawnResult {
			return respawnRun(t, "linger", 0, nil, func(c *lib.Client, o *lib.Envelope, s, id, dir string) {
				waitFileFor(t, filepath.Join(dir, "result"), 10*time.Second)
				env, err := lib.NewCancelEnvelope(gatewayParty, id, o.ContextID, o.CorrelationID, lib.WithTo(lib.Party{Session: s}))
				if err != nil {
					t.Fatal(err)
				}
				ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				if err := c.Publish(ctx, lib.TaskInSubject(s, id), env); err != nil {
					t.Fatal(err)
				}
			})
		}, want: lib.StateCanceled},
		stopDeadline: {run: func(t *testing.T) respawnResult {
			return respawnRun(t, "linger", 0, func(cfg *Config, _ *respawnScope) { cfg.TaskDeadline = 4 * time.Second }, nil)
		}, want: lib.StateFailed},
		stopWriteRefused: {run: func(t *testing.T) respawnResult {
			return respawnRun(t, "refusal", 0, func(cfg *Config, _ *respawnScope) {
				f := filepath.Join(t.TempDir(), "refusals.jsonl")
				cfg.StopOnWriteRefusal, cfg.WriteRefusalFile = true, f
				cfg.HarnessEnv = append(cfg.HarnessEnv, EnvWriteRefusalFile+"="+f, EnvWriteRefusalTask+"="+cfg.TaskID)
			}, nil)
		}, want: lib.StateFailed},
		stopAbandoned: {unreachable: "an abandon reaches only a dispatched profile task's adapter (profile tests drive that arm); the respawn checks a.abandoned() itself, and the abandon arm kills the process, which the clean-exit check refuses"},
		// A delegation mid-turn ends the turn: the loop ignores any later
		// harness result and, when the process exits (0 here, on the
		// delegation's SIGTERM), completes the task with the delegation line
		// and refuses the held steer, before the respawn decision. So this
		// case shows the behaviour (one process, steer refused, delegation
		// line) through that path; the halt(stopDelegated) record is
		// defence for a path this can't reach, and removing it doesn't fail
		// this case.
		stopDelegated: {run: func(t *testing.T) respawnResult {
			sock := delegateSock(t)
			r := respawnRun(t, "delegate", 0, func(cfg *Config, _ *respawnScope) {
				cfg.DelegateSocket = sock
				cfg.KillGrace = 5 * time.Second
			}, func(_ *lib.Client, _ *lib.Envelope, _, _, dir string) {
				waitFileFor(t, filepath.Join(dir, "mid-turn"), 10*time.Second)
				if reply := askDelegate(t, sock, lib.DelegateRequest{Addressee: "platform", Text: "how is the fleet?"}); !reply.OK {
					t.Fatalf("delegation refused: %+v", reply)
				}
			})
			c := testClient(t, r.url)
			if got := artifactText(foldTask(t, c, r.session, r.id), lib.ArtifactResult); got != "delegated to platform" {
				t.Errorf("result %q, want the delegation line", got)
			}
			return r
		}, want: lib.StateCompleted},
		stopEvicted: {unreachable: "the loop's SIGTERM arm returns at once; pendingStops records an eviction only in the gap between processes, which the toy can't hit deterministically, and the final switch ends the task as the arm does"},
	}
	for _, s := range supervisorStops {
		c, ok := cases[s]
		if !ok {
			t.Errorf("supervisorStop %q has no case here: add one that stops a task that way and asserts no further process", s)
			continue
		}
		t.Run(string(s), func(t *testing.T) {
			if c.unreachable != "" {
				t.Skip(c.unreachable)
			}
			r := c.run(t)
			if r.runs != 1 {
				t.Fatalf("%d processes ran; a %s stop must start no further process", r.runs, s)
			}
			if r.out.res.State != c.want {
				t.Errorf("state=%q err=%v; want %q", r.out.res.State, r.out.err, c.want)
			}
			if !r.refused(t, respawnSteer) {
				t.Errorf("the held steer was not refused on the task's stream")
			}
		})
	}
	if len(cases) != len(supervisorStops) {
		t.Errorf("%d cases for %d supervisorStops: a case names a stop that no longer exists", len(cases), len(supervisorStops))
	}
}

// TestAProcessThatExitsUncleanlyIsNotRespawned: the held steer's process
// starts only after the last one exited cleanly once its stdin closed.
func TestAProcessThatExitsUncleanlyIsNotRespawned(t *testing.T) {
	r := respawnRun(t, "clean", 3, nil, nil)
	if r.runs != 1 {
		t.Fatalf("%d processes ran after an unclean exit; want 1", r.runs)
	}
	if !r.refused(t, respawnSteer) {
		t.Errorf("the held steer was not refused on the task's stream")
	}
}

// TestAStopBetweenProcessesRefusesEverySteer: when the next turn can't be
// prepared, the held steer and one that arrived while it was being prepared
// are both refused, and no further process runs.
func TestAStopBetweenProcessesRefusesEverySteer(t *testing.T) {
	const late = "a steer while the next turn is prepared"
	preparing, sent := make(chan struct{}), make(chan struct{})
	r := respawnRun(t, "clean", 0, func(cfg *Config, _ *respawnScope) {
		d := cfg.Driver.(*toyDriver)
		first := d.prepare
		d.prepare = func(n int, dirs TurnDirs) error {
			if err := first(n, dirs); err != nil || n == 1 {
				return err
			}
			close(preparing)
			select {
			case <-sent:
			case <-time.After(10 * time.Second):
			}
			return errors.New("the toy refuses the second turn")
		}
	}, func(c *lib.Client, origin *lib.Envelope, session, _, _ string) {
		select {
		case <-preparing:
		case <-time.After(10 * time.Second):
			t.Fatal("the next turn was never prepared")
		}
		publishSteerFor(t, c, origin, session, late)
		time.Sleep(300 * time.Millisecond) // it reaches the adapter's steer channel
		close(sent)
	})
	if r.runs != 1 {
		t.Fatalf("%d processes ran; a turn that can't be prepared starts none", r.runs)
	}
	if r.out.res.State != lib.StateFailed {
		t.Errorf("state=%q err=%v; want failed", r.out.res.State, r.out.err)
	}
	for _, text := range []string{respawnSteer, late} {
		if !r.refused(t, text) {
			t.Errorf("the steer %q was not refused on the task's stream", text)
		}
	}
}

// TestACoreAgentSteerHeldAtTheResultRunsOnAFreshProcess drives the real
// core-agent driver with a stub speaking its wire format: a steer that
// arrives during turn one is held, not written to that process; turn one's
// process ends at its result, and a second process gets an envelope with
// the steer as its message and turn one's conversation events as its
// history.
func TestACoreAgentSteerHeldAtTheResultRunsOnAFreshProcess(t *testing.T) {
	dir := t.TempDir()
	harness := stub(t, fmt.Sprintf(`
dir=%q
n=$(( $(cat "$dir/runs" 2>/dev/null || echo 0) + 1 )); echo $n > "$dir/runs"
read -r envelope || exit 1
printf '%%s\n' "$envelope" > "$dir/envelope.$n"
echo '{"type":"init","session":"ca-1","model":"m"}'
echo "{\"type\":\"event\",\"event\":{\"turn\":$n}}"
echo 1 > "$dir/turn.$n"
i=0; while [ ! -e "$dir/steer-sent" ] && [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done
echo "{\"type\":\"result\",\"text\":\"done $n\"}"
while read -r more; do echo "$more" >> "$dir/late.$n"; done
exit 0
`, dir))
	url := startServer(t)
	c := testClient(t, url)
	session, taskID := "chat-ca-respawn", "task-ca-respawn"
	origin := submit(t, c, session, taskID, "start")
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Driver = newCoreAgentDriver()
	useRespawnScope(t)
	done := runAdapter(context.Background(), cfg)
	waitFileFor(t, filepath.Join(dir, "turn.1"), 10*time.Second)
	publishSteerFor(t, c, origin, session, respawnSteer)
	time.Sleep(300 * time.Millisecond)
	if err := os.WriteFile(filepath.Join(dir, "steer-sent"), []byte("1"), 0o644); err != nil {
		t.Fatal(err)
	}
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("state=%q err=%v", out.res.State, out.err)
	}
	if b, _ := os.ReadFile(filepath.Join(dir, "runs")); strings.TrimSpace(string(b)) != "2" {
		t.Fatalf("%s processes ran; want 2", strings.TrimSpace(string(b)))
	}
	// The envelope ends in a newline the stub's read leaves behind; anything
	// more is a second write.
	if late, err := os.ReadFile(filepath.Join(dir, "late.1")); err == nil && len(strings.TrimSpace(string(late))) > 0 {
		t.Errorf("turn one's process was written to after its envelope: %q", late)
	}
	var env2 struct {
		Message string            `json:"message"`
		Session string            `json:"session"`
		History []json.RawMessage `json:"history"`
	}
	b, _ := os.ReadFile(filepath.Join(dir, "envelope.2"))
	if err := json.Unmarshal(b, &env2); err != nil {
		t.Fatalf("envelope 2 %q: %v", b, err)
	}
	if env2.Message != respawnSteer {
		t.Errorf("process 2's message is %q; want the held steer", env2.Message)
	}
	if env2.Session != "ca-1" || len(env2.History) != 1 || !strings.Contains(string(env2.History[0]), `"turn":1`) {
		t.Errorf("process 2 resumes session %q with history %s; want ca-1 with turn one's event", env2.Session, env2.History)
	}
}
