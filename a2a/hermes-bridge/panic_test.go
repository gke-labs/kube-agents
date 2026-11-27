package hermesbridge

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
	"github.com/gke-labs/kube-agents/a2a/lib/taskguard"
)

// A panic while one task is handled ends that task failed closed, with no
// answer on the bus, and the bridge goes on serving (panic.go).

// panicMarker in an answer or a tool name makes a test's seam panic.
const panicMarker = "PANIC"

// wantFailedClosed checks a task a recovered panic ended: failed, the
// reason taskguard's token, and no answer of it on the bus, neither the
// result nor a turn artifact.
func wantFailedClosed(t *testing.T, url string, task *lib.Task) {
	t.Helper()
	if task.State != lib.StateFailed {
		t.Fatalf("state = %s (%q), want failed", task.State, terminalReason(t, task))
	}
	if got := terminalReason(t, task); got != taskguard.Reason {
		t.Fatalf("reason = %q, want %q", got, taskguard.Reason)
	}
	if got := artifactsNamed(t, url, task.ID, lib.ArtifactResult); len(got) != 0 {
		t.Fatalf("result artifact %q published for a task a panic ended", got)
	}
	if got := artifactsNamed(t, url, task.ID, lib.ArtifactTurn); len(got) != 0 {
		t.Fatalf("turn artifact %q published for a task a panic ended", got)
	}
	finals := 0
	for _, env := range replayEvents(t, url, task.ID) {
		if env.Kind == lib.KindStatusUpdate {
			if _, final := statusState(t, env); final {
				finals++
			}
		}
	}
	if finals != 1 {
		t.Fatalf("%d final events, want exactly 1", finals)
	}
}

// wantAnswered checks that a task after the panicked one completed with
// its answer: the worker and the bridge survived.
func wantAnswered(t *testing.T, url string, task *lib.Task, answer string) {
	t.Helper()
	if task.State != lib.StateCompleted {
		t.Fatalf("next task state = %s (%q), want completed", task.State, terminalReason(t, task))
	}
	if got := artifactsNamed(t, url, task.ID, lib.ArtifactResult); len(got) != 1 || !strings.Contains(got[0], answer) {
		t.Fatalf("next task result = %q, want %q", got, answer)
	}
}

// The chat fold runs inside finalize with run.mu held and stateDone set: a
// panic there leaves no terminal written, and before the lock was released
// by defer the recovery's terminal waited on it forever. The task ends
// failed internal-panic with no result, the panic is logged and counted,
// and the one worker takes the next task.
func TestPanic_ChatFoldFailsTheTaskClosedAndTheWorkerServesTheNext(t *testing.T) {
	_, url := startServer(t)
	logs := &lockedBuffer{}
	startBridgeCfgHooked(t, url, script(t, `echo "answer for $1"`), func(c *Config) {
		c.Concurrency = 1
		c.Logger = slog.New(slog.NewJSONHandler(logs, nil))
	}, func(b *Bridge) {
		fold := b.chatPart
		b.chatPart = func(answer string) (lib.Part, bool) {
			if strings.Contains(answer, panicMarker) {
				panic("injected panic in the chat fold")
			}
			return fold(answer)
		}
	})
	c := gatewayClient(t, url)
	before := taskguard.Count()

	submit(t, c, "task-panic-fold", panicMarker+" please")
	wantFailedClosed(t, url, waitTerminal(t, c, "task-panic-fold"))
	if got := taskguard.Count() - before; got != 1 {
		t.Fatalf("recovered panics = %d, want 1", got)
	}
	out := logs.String()
	if !strings.Contains(out, `"level":"ERROR","msg":"`+taskguard.LogMessage+`"`) ||
		!strings.Contains(out, `"panic":"panic value of type string"`) || !strings.Contains(out, `"task":"task-panic-fold"`) {
		t.Fatalf("no Error record for the recovered panic in the log:\n%s", out)
	}
	// The panic's own text never reaches the log (it could have been built
	// from the task's text).
	if strings.Contains(out, "injected panic in the chat fold") {
		t.Fatalf("the panic's text reached the log:\n%s", out)
	}

	submit(t, c, "task-panic-fold-next", "the next question")
	wantAnswered(t, url, waitTerminal(t, c, "task-panic-fold-next"), "answer for the next question")
}

// A panic while a turn's answer is published as a turn artifact, with
// run.noticeMu held, ends the task failed internal-panic with neither the
// turn artifact nor a result, and a next task in the same session (whose
// turn the panicked one held) is answered.
func TestPanic_TurnPublishFailsTheTaskClosedAndTheSessionServesTheNext(t *testing.T) {
	_, url := startServer(t)
	stub, in, release := holdingStub(t)
	startAPIBridgeHooked(t, url, stub, nil, func(b *Bridge) {
		b.publishTurn = func(context.Context, *taskRun, string, string) error {
			panic("injected panic in the turn publish")
		}
	}, func(net.Addr) bool { return true })
	c := gatewayClient(t, url)
	before := taskguard.Count()

	origin := submitIn(t, c, "task-panic-turn", "ctx-panic-turn", "long question")
	<-in
	sendSteer(t, c, origin, "and the west")
	waitFor(t, 10*time.Second, "queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	release()
	wantFailedClosed(t, url, waitTerminal(t, c, origin.TaskID))
	if got := taskguard.Count() - before; got != 1 {
		t.Fatalf("recovered panics = %d, want 1", got)
	}

	submitIn(t, c, "task-panic-turn-next", "ctx-panic-turn", "next question")
	wantAnswered(t, url, waitTerminal(t, c, "task-panic-turn-next"), "answer to next question")
}

// A panic while the activity door observes a delivery, with run.mu held,
// drops that delivery, still answers the hook 204 (a 5xx would only make
// hermes retry it), and marks the run: the answer
// the child prints afterwards is not published, and the task ends failed
// internal-panic. The door and the bridge go on serving the next task.
func TestPanic_ActivityDeliveryFailsItsTaskClosed(t *testing.T) {
	_, url := startServer(t)
	status := filepath.Join(t.TempDir(), "status")
	t.Setenv(stubMarkerEnv, status)
	startBridgeCfgHooked(t, url, hermesStub(t, `
import urllib.error
if "`+panicMarker+`" in sys.argv[-1]:
    try:
        code = post("pre_tool_call", "`+panicMarker+`", {}, {"tool_call_id": "c-boom", "task_id": "", "session_id": "s1"})
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        code = type(e).__name__
    open(os.environ["`+stubMarkerEnv+`"], "w").write(str(code))
else:
    call("kubectl", {"cmd": "get pods"}, "c-ok")
print("the answer to " + sys.argv[-1])
`), func(c *Config) { c.Concurrency = 1 }, func(b *Bridge) {
		b.observe = func(a *activityState, d hookDelivery) (ActivityEntry, bool, []stepmsg.Step) {
			if d.ToolName == panicMarker {
				panic("injected panic in the activity observation")
			}
			return a.observe(d)
		}
	})
	c := gatewayClient(t, url)
	before := taskguard.Count()

	submit(t, c, "task-panic-door", panicMarker+" question")
	wantFailedClosed(t, url, waitTerminal(t, c, "task-panic-door"))
	if got, err := os.ReadFile(status); err != nil || string(got) != "204" {
		t.Fatalf("hook answer = %q (%v), want 204", got, err)
	}
	if got := taskguard.Count() - before; got != 1 {
		t.Fatalf("recovered panics = %d, want 1", got)
	}

	submit(t, c, "task-panic-door-next", "next question")
	next := waitTerminal(t, c, "task-panic-door-next")
	wantAnswered(t, url, next, "the answer to next question")
	if entries := activityEntries(t, next); len(entries) != 1 || entries[0].Tool != "kubectl" {
		t.Fatalf("next task's trace = %+v, want its kubectl call", entries)
	}
}

// A heartbeat tick that panics, with run.mu held, is skipped: the
// heartbeat goes on, the lock is free for the ticks after it and for
// finalize, and the task completes with its answer.
func TestPanic_HeartbeatTickIsSkippedAndTheHeartbeatGoesOn(t *testing.T) {
	_, url := startServer(t)
	var ticks atomic.Int32
	startBridgeCfgHooked(t, url, script(t, `sleep 1.2; echo "slow answer"`),
		func(c *Config) { c.ProgressInterval = 100 * time.Millisecond },
		func(b *Bridge) {
			b.progressLine = func(a *activityState, now time.Time) string {
				if ticks.Add(1) == 1 {
					panic("injected panic in the heartbeat")
				}
				return a.progressLine(now)
			}
		})
	c := gatewayClient(t, url)
	before := taskguard.Count()

	submit(t, c, "task-panic-beat", "take your time")
	task := waitTerminal(t, c, "task-panic-beat")
	wantAnswered(t, url, task, "slow answer")
	if got := taskguard.Count() - before; got != 1 {
		t.Fatalf("recovered panics = %d, want 1", got)
	}
	if texts := progressTexts(task.Artifact(lib.ArtifactProgress)); len(texts) == 0 ||
		!slices.ContainsFunc(texts, func(s string) bool { return strings.HasPrefix(s, "running ") }) {
		t.Fatalf("progress = %q, want heartbeats after the panicked tick", texts)
	}
}

// A card stop runs on its own goroutine, not a worker: a panic while its
// result is published, inside finalize with run.mu held, ends that control
// failed internal-panic with no result, and the next card stop completes.
func TestPanic_CardStopFailsClosedAndTheNextStopCompletes(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, stopAnswer)
	var publishes atomic.Int32
	startAPIBridgeHooked(t, url, newAPIStub(t, nil), withStore(store), func(b *Bridge) {
		publish := b.resultPublish
		b.resultPublish = func(ctx context.Context, run *taskRun, output string, chat []byte) error {
			if publishes.Add(1) == 1 {
				panic("injected panic in the card stop's result publish")
			}
			return publish(ctx, run, output, chat)
		}
	}, func(net.Addr) bool { return true })
	c := gatewayClient(t, url)
	before := taskguard.Count()

	submitCardStop(t, c, "task-panic-stop", "ctx-panic-stop", "slack:dm/D123", nil)
	wantFailedClosed(t, url, waitTerminal(t, c, "task-panic-stop"))
	if got := taskguard.Count() - before; got != 1 {
		t.Fatalf("recovered panics = %d, want 1", got)
	}

	submitCardStop(t, c, "task-panic-stop-next", "ctx-panic-stop", "slack:dm/D123", nil)
	wantAnswered(t, url, waitTerminal(t, c, "task-panic-stop-next"), "stopped 2 cards")
}

// A panic while the door collects a tool result's secret values, before
// run.mu and outside the observation, still marks the run: a value the
// scrub never saw cannot reach the answer, because no answer is published.
// The door answers 204, and the next task's delivery is collected and
// answered as usual.
func TestPanic_ToolResultCollectionFailsItsTaskClosed(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfgHooked(t, url, hermesStub(t, `
if "`+panicMarker+`" in sys.argv[-1]:
    post("pre_tool_call", "kubectl", {}, {"tool_call_id": "c-sec", "task_id": "", "session_id": "s1"})
    post("post_tool_call", "kubectl", {}, {"tool_call_id": "c-sec", "task_id": "", "session_id": "s1",
                                            "duration_ms": 3, "status": "ok", "result": "`+panicMarker+` s3cr3t-value"})
    print("the answer quotes s3cr3t-value")
else:
    call("kubectl", {"cmd": "get pods"}, "c-ok")
    print("the answer to " + sys.argv[-1])
`), func(c *Config) { c.Concurrency = 1 }, func(b *Bridge) {
		b.collectResult = func(a *activityState, result json.RawMessage) {
			if strings.Contains(string(result), panicMarker) {
				panic("injected panic in the tool result collection")
			}
			a.secrets.CollectToolResult(result)
		}
	})
	c := gatewayClient(t, url)
	before := taskguard.Count()

	submit(t, c, "task-panic-collect", panicMarker+" question")
	wantFailedClosed(t, url, waitTerminal(t, c, "task-panic-collect"))
	if got := taskguard.Count() - before; got != 1 {
		t.Fatalf("recovered panics = %d, want 1", got)
	}

	submit(t, c, "task-panic-collect-next", "next question")
	wantAnswered(t, url, waitTerminal(t, c, "task-panic-collect-next"), "the answer to next question")
}

// usagePanicsOnce makes the first usage part a bridge builds panic, with
// run.mu held inside publishUsage.
func usagePanicsOnce(b *Bridge) {
	var calls atomic.Int32
	b.usagePart = func(u lib.Usage) (lib.Part, error) {
		if calls.Add(1) == 1 {
			panic("injected panic in the usage publish")
		}
		return lib.UsagePart(u)
	}
}

// A panic while a turn's usage is published, with run.mu held, ends the
// task failed internal-panic with no answer on either executor, and the
// next task completes. On the API executor the turn's deferred cleanup takes
// run.mu again while the panic unwinds, so a lock the panic left held
// deadlocks there before any recovery runs.
func TestPanic_UsagePublishFailsTheTaskClosedAndTheNextCompletes(t *testing.T) {
	t.Run("api", func(t *testing.T) {
		_, url := startServer(t)
		// The first turn fails 5xx, whose wall-time usage goes out through
		// publishTurnUsage; an answered turn's usage rides the answer's own
		// critical section, which already released by defer.
		var calls atomic.Int32
		stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
			if calls.Add(1) == 1 {
				http.Error(w, "server fault", http.StatusInternalServerError)
				return
			}
			writeCompletion(w, c.sessionID, "answer to "+c.prompt)
		})
		startAPIBridgeHooked(t, url, stub, nil, usagePanicsOnce, func(net.Addr) bool { return true })
		c := gatewayClient(t, url)
		before := taskguard.Count()

		submitIn(t, c, "task-panic-usage-api", "ctx-panic-usage", "first question")
		wantFailedClosed(t, url, waitTerminal(t, c, "task-panic-usage-api"))
		if got := taskguard.Count() - before; got != 1 {
			t.Fatalf("recovered panics = %d, want 1", got)
		}
		submitIn(t, c, "task-panic-usage-api-next", "ctx-panic-usage", "next question")
		wantAnswered(t, url, waitTerminal(t, c, "task-panic-usage-api-next"), "next question")
	})
	t.Run("cli", func(t *testing.T) {
		_, url := startServer(t)
		startBridgeCfgHooked(t, url, script(t, `echo "answer for $1"`),
			func(c *Config) { c.Concurrency = 1 }, usagePanicsOnce)
		c := gatewayClient(t, url)
		before := taskguard.Count()

		submit(t, c, "task-panic-usage-cli", "first question")
		wantFailedClosed(t, url, waitTerminal(t, c, "task-panic-usage-cli"))
		if got := taskguard.Count() - before; got != 1 {
			t.Fatalf("recovered panics = %d, want 1", got)
		}
		submit(t, c, "task-panic-usage-cli-next", "next question")
		wantAnswered(t, url, waitTerminal(t, c, "task-panic-usage-cli-next"), "answer for next question")
	})
}

// A door panic during turn 1 of an API task with a follow-up queued marks
// the run, and at the turn boundary no turn artifact goes out and the
// follow-up's turn is never sent: the task ends failed internal-panic.
func TestPanic_MarkedRunPublishesNoTurnArtifactAtTheBoundary(t *testing.T) {
	_, url := startServer(t)
	const shared = "pod-wide-activity-key"
	var door atomic.Value
	inCh, rel := make(chan struct{}), make(chan struct{})
	var first atomic.Bool
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		if first.CompareAndSwap(false, true) {
			close(inCh)
			select {
			case <-rel:
			case <-r.Context().Done():
				return
			}
			postDelivery(t, door.Load().(string), shared, c.sessionID, hookPreToolCall, panicMarker, "c-boom")
		}
		writeCompletion(w, c.sessionID, "answer to "+c.prompt)
	})
	var released atomic.Bool
	release := func() {
		if released.CompareAndSwap(false, true) {
			close(rel)
		}
	}
	t.Cleanup(release)
	b, _ := startAPIBridgeHooked(t, url, stub, func(cfg *Config) {
		cfg.ActivityListen = "127.0.0.1:0"
		cfg.ActivitySecret = shared
	}, func(b *Bridge) {
		b.observe = func(a *activityState, d hookDelivery) (ActivityEntry, bool, []stepmsg.Step) {
			if d.ToolName == panicMarker {
				panic("injected panic in the activity observation")
			}
			return a.observe(d)
		}
	}, func(net.Addr) bool { return true })
	door.Store(b.ActivityURL())
	c := gatewayClient(t, url)

	origin := submitIn(t, c, "task-panic-boundary", "ctx-panic-boundary", "long question")
	<-inCh
	sendSteer(t, c, origin, "and the west")
	waitFor(t, 10*time.Second, "queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	release()
	wantFailedClosed(t, url, waitTerminal(t, c, origin.TaskID))
	if n := len(stub.seen()); n != 1 {
		t.Fatalf("%d requests, want the opening one only: the follow-up's turn ran", n)
	}
}

// A panic collecting a card worker's tool result (a delivery that matched
// no trace, read into the set of the task that filed the card) marks that
// task's run, so its answer fails closed, and the recovery's Error record
// names the task.
func TestPanic_CardWorkerCollectionMarksTheRunAndNamesItsTask(t *testing.T) {
	logs := &lockedBuffer{}
	b := &Bridge{cfg: Config{Logger: slog.New(slog.NewJSONHandler(logs, nil))}, tasks: make(map[string]*taskRun)}
	b.collectResult = func(*activityState, json.RawMessage) { panic("injected panic collecting a card worker's result") }
	a := newActivityState(false)
	a.sessionID = "a2a-coord-cardpanic"
	a.inTurn.Store(true)
	a.SyncKanbanCard("t_card91", "Check prod-c", "")
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-card-panic"}, state: stateRunning}
	run.act.Store(a)
	d := &hookDelivery{Event: hookPostToolCall, SessionID: "20990101_120003_worker", Cwd: "/var/lib/hermes/kanban/workspaces/t_card91/repo"}
	before := taskguard.Count()

	b.collectCardWorkerSecrets([]*taskRun{run}, d)

	if !run.panicked.Load() {
		t.Fatal("the run whose card worker's result panicked is not marked: its answer would go out")
	}
	if got := taskguard.Count() - before; got != 1 {
		t.Fatalf("recovered panics = %d, want 1", got)
	}
	out := logs.String()
	if !strings.Contains(out, `"msg":"`+taskguard.LogMessage+`"`) || !strings.Contains(out, `"task":"task-card-panic"`) {
		t.Fatalf("the recovery's Error record doesn't name the task:\n%s", out)
	}
}

// The door's observation runs under run.mu, and a panic in it marks the run
// before the lock is released (observeDelivery's own guard): a finalize
// waiting on run.mu then sees the mark and publishes no answer. Without
// that inner guard the deferred unlock runs first and the outer guard
// marks the run only after, so a finalize that takes the lock in between
// publishes the answer. Here a goroutine waits on run.mu in finalize's
// place and reads the mark the moment it gets the lock.
func TestPanic_ObservationMarksTheRunBeforeReleasingItsLock(t *testing.T) {
	for i := range 20 {
		b := &Bridge{cfg: Config{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}, tasks: make(map[string]*taskRun)}
		run := &taskRun{origin: &lib.Envelope{TaskID: "task-observe-window"}, state: stateRunning}
		run.act.Store(newActivityState(false))
		saw := make(chan bool, 1)
		b.observe = func(*activityState, hookDelivery) (ActivityEntry, bool, []stepmsg.Step) {
			waiting := make(chan struct{})
			go func() {
				close(waiting)
				run.mu.Lock()
				defer run.mu.Unlock()
				saw <- run.panicked.Load()
			}()
			<-waiting
			time.Sleep(10 * time.Millisecond) // the goroutine is now blocked on run.mu
			panic("injected panic in the door's observation")
		}
		b.takeDelivery(run, nil, &hookDelivery{Event: hookPreToolCall, SessionID: "a2a-observe-window"})
		if !<-saw {
			t.Fatalf("iteration %d: run.mu was released before the run was marked panicked; a finalize waiting on it would publish the answer", i)
		}
	}
}
