package workeradapter

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// agyStopStub is an agy stand-in for the stop tests. Each run, n counting
// from 1, records itself in $dir/runs, reads its one user line, says init,
// marks turn one started, then acts on $dir/mode:
//   - error: a failed result after 1.5s;
//   - sleep: no result for 20s (cancel or the deadline ends it);
//   - refusal: a clean result after 1.5s, then a write refusal record and a
//     tool step's result after it;
//   - anything else: a clean result after 1.5s.
//
// It then reads stdin to its end and exits with $dir/exitcode (0 if none).
func agyStopStub(t *testing.T, dir, taskID string) []string {
	t.Helper()
	record := refusalRecord(t, true, kubernetesReadOnlyRule, "scale", taskID)
	return stub(t, fmt.Sprintf(`
dir=%q
n=$(( $(cat "$dir/runs" 2>/dev/null || echo 0) + 1 )); echo $n > "$dir/runs"
mode=$(cat "$dir/mode" 2>/dev/null)
read first || exit 1
echo '{"event":"init","conversation_id":"conv-s","init":{"model":"m"}}'
echo 1 > "$dir/turn1"
case "$mode" in
error) sleep 1.5; echo '{"event":"result","result":{"conversation_id":"conv-s","status":"ERROR","error":"boom"}}' ;;
sleep) sleep 20 ;;
refusal)
  sleep 1.5
  echo '{"event":"result","result":{"conversation_id":"conv-s","status":"SUCCESS","response":"turn '$n'"}}'
  sleep 0.3
  printf '%%s\n' %q >> "$%s"
  echo '{"event":"step_update","step_update":{"conversation_id":"conv-s","step_index":3,"state":"DONE","step_type":"tool","tool_name":"run_command"}}' ;;
*) sleep 1.5; echo '{"event":"result","result":{"conversation_id":"conv-s","status":"SUCCESS","response":"turn '$n'"}}' ;;
esac
while read -r more; do :; done
exit $(cat "$dir/exitcode" 2>/dev/null || echo 0)
`, dir, record, EnvWriteRefusalFile))
}

// stopRun runs one agy task in mode with a steer held during turn one,
// calling trigger once the steer is sent, and returns the outcome and how
// many agy processes ran.
func stopRun(t *testing.T, mode string, exitCode int, setup func(*Config), trigger func(c *lib.Client, origin *lib.Envelope, session, taskID string)) (runOutcome, string) {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "mode"), []byte(mode), 0o644); err != nil {
		t.Fatal(err)
	}
	if exitCode != 0 {
		if err := os.WriteFile(filepath.Join(dir, "exitcode"), []byte(fmt.Sprint(exitCode)), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	// Task and session ids are subject tokens: short lowercase DNS labels.
	label := strings.ToLower(mode) + "-" + fmt.Sprint(exitCode) + "-" + fmt.Sprint(time.Now().UnixNano()%1_000_000)
	session := "chat-agy-stop-" + label
	taskID := "task-agy-stop-" + label
	url := startServer(t)
	c := testClient(t, url)
	origin := submit(t, c, session, taskID, "start")
	cfg := adapterConfig(url, taskID, session, agyStopStub(t, dir, taskID))
	useAgyTestDriver(t, &cfg)
	if setup != nil {
		setup(&cfg)
	}
	done := runAdapter(context.Background(), cfg)
	waitFile(t, filepath.Join(dir, "turn1"), 10*time.Second)
	publishSteerFor(t, c, origin, session, "a steer during turn one")
	if trigger != nil {
		trigger(c, origin, session, taskID)
	}
	out := waitOutcome(t, done, 45*time.Second)
	b, _ := os.ReadFile(filepath.Join(dir, "runs"))
	return out, strings.TrimSpace(string(b))
}

func publishCancelFor(t *testing.T, c *lib.Client, origin *lib.Envelope, session, taskID string) {
	t.Helper()
	time.Sleep(300 * time.Millisecond) // the steer is held first
	env, err := lib.NewCancelEnvelope(gatewayParty, taskID, origin.ContextID, origin.CorrelationID, lib.WithTo(lib.Party{Session: session}))
	if err != nil {
		t.Fatalf("cancel envelope: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := c.Publish(ctx, lib.TaskInSubject(session, taskID), env); err != nil {
		t.Fatalf("publish cancel: %v", err)
	}
}

// TestNoSupervisorStopStartsAnotherAgyProcess walks every supervisorStop: a
// task stopped any way the supervisor knows, with a steer held, runs no
// further agy process. A stop agy cannot reach says why. A stop added to
// supervisorStops without a case here fails the test.
func TestNoSupervisorStopStartsAnotherAgyProcess(t *testing.T) {
	type stopCase struct {
		run         func(t *testing.T) (runOutcome, string)
		want        lib.TaskState
		unreachable string
	}
	cases := map[supervisorStop]stopCase{
		stopDecided: {
			run:  func(t *testing.T) (runOutcome, string) { return stopRun(t, "error", 0, nil, nil) },
			want: lib.StateFailed,
		},
		stopCanceled: {
			run: func(t *testing.T) (runOutcome, string) {
				return stopRun(t, "sleep", 0, nil, func(c *lib.Client, o *lib.Envelope, s, id string) { publishCancelFor(t, c, o, s, id) })
			},
			want: lib.StateCanceled,
		},
		stopDeadline: {
			run: func(t *testing.T) (runOutcome, string) {
				return stopRun(t, "sleep", 0, func(cfg *Config) { cfg.TaskDeadline = 3 * time.Second }, nil)
			},
			want: lib.StateFailed,
		},
		stopWriteRefused: {
			run: func(t *testing.T) (runOutcome, string) {
				return stopRun(t, "refusal", 0, func(cfg *Config) {
					f := filepath.Join(t.TempDir(), "refusals.jsonl")
					cfg.StopOnWriteRefusal, cfg.WriteRefusalFile = true, f
					cfg.HarnessEnv = append(cfg.HarnessEnv, EnvWriteRefusalFile+"="+f, EnvWriteRefusalTask+"="+cfg.TaskID)
				}, nil)
			},
			want: lib.StateFailed,
		},
		stopAbandoned: {unreachable: "an abandon reaches only a dispatched profile task's adapter (profile_test drives that arm); the respawn checks a.abandoned() itself, and the abandon arm kills the process, which the clean-exit check refuses"},
		stopDelegated: {unreachable: "agy configures no delegate tool until #6712; a delegation also records stopDecided-equivalent sawResult, and its kill leaves an exit error"},
		stopEvicted:   {unreachable: "supervise returns from the SIGTERM arm at once, so no respawn code runs after it"},
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
			out, runs := c.run(t)
			if runs != "1" {
				t.Fatalf("%s agy processes ran; a %s stop must start no further process", runs, s)
			}
			if out.res.State != c.want {
				t.Errorf("state=%q err=%v; want %q", out.res.State, out.err, c.want)
			}
		})
	}
	if len(cases) != len(supervisorStops) {
		t.Errorf("%d cases for %d supervisorStops: a case names a stop that no longer exists", len(cases), len(supervisorStops))
	}
}

// TestAnAgyProcessThatExitsUncleanlyIsNotRespawned: the held steer's
// process starts only after the last one exited cleanly once its stdin
// closed. One that exits with an error, no stop recorded, is the case a
// stop that forgot to record itself would leave.
func TestAnAgyProcessThatExitsUncleanlyIsNotRespawned(t *testing.T) {
	out, runs := stopRun(t, "clean", 3, nil, nil)
	if runs != "1" {
		t.Fatalf("%s agy processes ran after an unclean exit; want 1", runs)
	}
	if out.res.State == lib.StateCompleted {
		t.Errorf("state=%q; the held steer never ran, so the task did not complete its turns", out.res.State)
	}
}
