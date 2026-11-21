package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats.go"
)

// replayRecorder records the task ids the gateway fully replays on the fan-out
// paths (fanOutReplayHook), so a test can tell a direct-get read from one that
// opened a consumer on TASKS (#6646).
type replayRecorder struct {
	mu  sync.Mutex
	ids []string
}

func (rr *replayRecorder) hook(taskID string) {
	rr.mu.Lock()
	rr.ids = append(rr.ids, taskID)
	rr.mu.Unlock()
}

func (rr *replayRecorder) replayed() []string {
	rr.mu.Lock()
	defer rr.mu.Unlock()
	return slices.Clone(rr.ids)
}

// consumerCreates counts the consumer-create requests made on the TASKS
// stream from now on: each is a replay (or other consumer) the gateway opens.
func consumerCreates(t *testing.T, r *rig) func() int {
	t.Helper()
	nc, err := connectTest(r.url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	var mu sync.Mutex
	n := 0
	for _, subj := range []string{"$JS.API.CONSUMER.CREATE." + lib.TasksStream + ".>", "$JS.API.CONSUMER.CREATE." + lib.TasksStream} {
		if _, err := nc.Subscribe(subj, func(*nats.Msg) { mu.Lock(); n++; mu.Unlock() }); err != nil {
			t.Fatal(err)
		}
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	return func() int { mu.Lock(); defer mu.Unlock(); return n }
}

// A status question during a fan-out reads each live child's state with a
// direct get. The turn opens at most one TASKS consumer, the heal's one spot
// check, however wide the fan-out; before #6646 it opened about two per
// child (a heal replay and a status replay each).
func TestAStatusTurnDuringAFanOutOpensAtMostOneConsumer(t *testing.T) {
	t.Parallel()
	r, spawn, _ := startFanOutRig(t, 3)
	ctx := context.Background()
	conv := "discord:g1/t-fanout-status-probe"
	f := fanOut(t, r, spawn, conv, clusterTask("web-1", "a"), clusterTask("web-2", "b"), clusterTask("web-3", "c"))
	for i := 0; i < 2; i++ {
		if err := r.execFor(t, f.envs[i], "cluster").PublishStatus(ctx, lib.StateWorking, false); err != nil {
			t.Fatal(err)
		}
	}
	waitFor(t, "two children working", func() bool {
		for i := 0; i < 2; i++ {
			if task, err := r.bus.TasksGet(ctx, "cluster", f.kids[i].ID); err != nil || task.State != lib.StateWorking {
				return false
			}
		}
		return true
	})
	creates := consumerCreates(t, r)
	sessionRigTurn(r, conv, "status-1", "status")
	want := fmt.Sprintf(noticeFanOutStatusHead, 3, 3)
	waitFor(t, "the fan-out status", postedContaining(r, want))
	var status string
	for _, p := range r.adapter.postTexts() {
		if strings.HasPrefix(p, want) {
			status = p
		}
	}
	for _, line := range []string{
		"\n• " + clusterLabel("web-1") + ": working",
		"\n• " + clusterLabel("web-2") + ": working",
		"\n• " + clusterLabel("web-3") + ": waiting to start",
	} {
		if !strings.Contains(status, line) {
			t.Fatalf("status lacks %q:\n%s", line, status)
		}
	}
	if n := creates(); n > 1 {
		t.Fatalf("the status turn opened %d TASKS consumers; want at most the heal's one spot check", n)
	}
}

// The heal replays only a child whose terminal is on the stream and wasn't
// relayed: a child still running, and one with nothing on its stream yet,
// are read with direct gets alone (#6646).
func TestAHealReplaysOnlyAChildThatEnded(t *testing.T) {
	t.Parallel()
	r, spawn := startBatchRig(t, 3)
	ctx := context.Background()
	conv := "discord:g1/t-fanout-heal-probe"
	f := fanOut(t, r, spawn, conv, platformTask("one"), platformTask("two"), platformTask("three"))
	e0, e1 := r.execFor(t, f.envs[0], targetPlatform), r.execFor(t, f.envs[1], targetPlatform)
	var rr replayRecorder
	r2, _ := restartRig(t, r, func() {
		completeTask(t, e0, "one done")
		if err := e1.PublishStatus(ctx, lib.StateWorking, false); err != nil {
			t.Fatal(err)
		}
		drainRelayDurable(t, r.url)
	})
	r2.g.mu.Lock()
	r2.g.fanOutReplayHook = rr.hook
	r2.g.mu.Unlock()
	sessionRigTurn(r2, conv, "h-heal-probe", "status")
	waitFor(t, "the first child healed", func() bool {
		rec, err := r2.g.reg.Get(ctx, conv)
		if err != nil || rec == nil {
			return false
		}
		kid, ok := rec.TaskRefFor(f.kids[0].ID)
		return ok && kid.BatchEnd != nil
	})
	got := rr.replayed()
	if !slices.Contains(got, f.kids[0].ID) {
		t.Fatalf("the heal never replayed the ended child %s (replayed %v)", f.kids[0].ID, got)
	}
	others := 0
	for _, id := range got {
		if id != f.kids[0].ID {
			others++
		}
	}
	if others > 1 {
		t.Fatalf("the heal replayed %d running children (%v); want at most its one spot check", others, got)
	}
}

// A child whose terminal is followed by a later write on its events subject
// (a dying executor's flush) reads as running to the probe. When the relay
// missed that terminal, the heal's spot check still finds it within as many
// passes as there are running children, and ends the child as the relay
// would have (#6646).
func TestAHealFindsATerminalAStragglerHides(t *testing.T) {
	t.Parallel()
	r, spawn := startBatchRig(t, 2)
	ctx := context.Background()
	conv := "discord:g1/t-fanout-straggler"
	f := fanOut(t, r, spawn, conv, platformTask("one"), platformTask("two"))
	e0, e1 := r.execFor(t, f.envs[0], targetPlatform), r.execFor(t, f.envs[1], targetPlatform)
	r2, _ := restartRig(t, r, func() {
		completeTask(t, e0, "one done")
		// The straggler: a write after the terminal on the same subject.
		if err := e0.PublishStatus(ctx, lib.StateWorking, false); err != nil {
			t.Fatal(err)
		}
		if err := e1.PublishStatus(ctx, lib.StateWorking, false); err != nil {
			t.Fatal(err)
		}
		drainRelayDurable(t, r.url)
	})
	if p, err := r2.g.probeTask(ctx, targetPlatform, f.kids[0].ID); err != nil || p.final {
		t.Fatalf("probe of the straggled child = %+v, %v; the test needs it to read as running", p, err)
	}
	healed := func() bool {
		rec, err := r2.g.reg.Get(ctx, conv)
		if err != nil || rec == nil {
			return false
		}
		kid, ok := rec.TaskRefFor(f.kids[0].ID)
		return ok && kid.BatchEnd != nil
	}
	for i := 0; i < 2 && !healed(); i++ {
		sessionRigTurn(r2, conv, fmt.Sprintf("h-straggler-%d", i), "status")
		waitFor(t, "the status answer", postedContaining(r2, fmt.Sprintf("%d", i+1)))
	}
	waitFor(t, "the straggled child healed by a spot check", healed)
}

// probeStatus publishes a status update for taskID on subject.
func probeStatus(t *testing.T, r *rig, subject, addressee, taskID string, state lib.TaskState, final bool) {
	t.Helper()
	payload, err := json.Marshal(lib.StatusUpdate{TaskID: taskID, ContextID: "ctx-probe", Status: lib.TaskStatus{State: state}, Final: final})
	if err != nil {
		t.Fatal(err)
	}
	from := lib.Party{Session: addressee, AgentType: "test-executor"}
	if subject != lib.TaskEventsSubject(addressee, taskID) {
		from = gatewayParty // a supervisor subject carries the gateway's terminal
	}
	env, err := lib.NewStatusUpdateEnvelope(from, taskID, "ctx-probe", "corr-probe", payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(context.Background(), subject, env); err != nil {
		t.Fatal(err)
	}
}

// The probe agrees with the full replay on whether a task ended and in what
// state, for a terminal on each of the replay subjects, and on a task with
// nothing on its stream (#6646).
func TestTheProbeAgreesWithTheReplay(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	const addr = "tse"
	cases := []struct {
		name  string
		steps []func(id string)
	}{
		{"working", []func(string){
			func(id string) { probeStatus(t, r, lib.TaskEventsSubject(addr, id), addr, id, lib.StateWorking, false) },
		}},
		{"a question", []func(string){
			func(id string) { probeStatus(t, r, lib.TaskEventsSubject(addr, id), addr, id, lib.StateWorking, false) },
			func(id string) {
				probeStatus(t, r, lib.TaskEventsSubject(addr, id), addr, id, lib.StateInputRequired, false)
			},
		}},
		{"completed by the executor", []func(string){
			func(id string) { probeStatus(t, r, lib.TaskEventsSubject(addr, id), addr, id, lib.StateWorking, false) },
			func(id string) {
				probeStatus(t, r, lib.TaskEventsSubject(addr, id), addr, id, lib.StateCompleted, true)
			},
		}},
		{"canceled by the task supervisor", []func(string){
			func(id string) { probeStatus(t, r, lib.TaskEventsSubject(addr, id), addr, id, lib.StateWorking, false) },
			func(id string) {
				probeStatus(t, r, lib.TaskSupervisorSubject(addr, id), addr, id, lib.StateCanceled, true)
			},
		}},
		{"an executor's terminal, then a supervisor's that raced it", []func(string){
			func(id string) {
				probeStatus(t, r, lib.TaskEventsSubject(addr, id), addr, id, lib.StateCompleted, true)
			},
			func(id string) {
				probeStatus(t, r, lib.TaskSupervisorSubject(addr, id), addr, id, lib.StateFailed, true)
			},
		}},
		{"failed by the profile supervisor", []func(string){
			func(id string) { probeStatus(t, r, lib.TaskEventsSubject(addr, id), addr, id, lib.StateWorking, false) },
			func(id string) {
				probeStatus(t, r, lib.ProfileSupervisorSubject(addr, id), addr, id, lib.StateFailed, true)
			},
		}},
	}
	for i, c := range cases {
		id := fmt.Sprintf("task-probe-%d", i)
		for _, step := range c.steps {
			step(id)
		}
		p, err := r.g.probeTask(ctx, addr, id)
		if err != nil {
			t.Fatalf("%s: probe: %v", c.name, err)
		}
		task, _, _, err := r.bus.TasksGetTerminal(ctx, addr, id)
		if err != nil {
			t.Fatalf("%s: replay: %v", c.name, err)
		}
		if p.empty || p.final != task.Final || p.state != task.State {
			t.Errorf("%s: probe = %+v, replay = state %s final %v", c.name, p, task.State, task.Final)
		}
	}
	if p, err := r.g.probeTask(ctx, addr, "task-probe-none"); err != nil || !p.empty || p.final {
		t.Errorf("a task with nothing on its stream: probe = %+v, %v; want empty", p, err)
	}
}
