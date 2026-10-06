package gateway

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A delegation chain is one task to the adapter's observers: the human (or
// door) turn that delegated is the chain's root, and the child and the wake
// are never named to a TaskObserver or a DeliverableObserver. The root's end
// is announced once, at the chain's end, with the wake's result as the
// deliverable.

// observed is one call the gateway made on an observer.
type observed struct {
	kind   string // started, accepted, delivered, terminal, cancel
	task   string
	text   string // the deliverable, or the terminal's reason
	state  lib.TaskState
	source TerminalSource
}

// recordingObserver is the rig's fake adapter as a TaskObserver and a
// DeliverableObserver, recording every call in order.
type recordingObserver struct {
	*fakeAdapter
	mu  sync.Mutex
	got []observed
}

func (o *recordingObserver) add(e observed) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.got = append(o.got, e)
}

func (o *recordingObserver) TaskStarted(_, taskID string) {
	o.add(observed{kind: "started", task: taskID})
}

func (o *recordingObserver) TaskAccepted(_, taskID string) {
	o.add(observed{kind: "accepted", task: taskID})
}

func (o *recordingObserver) TaskTerminal(_, taskID string, state lib.TaskState, source TerminalSource, reason string) {
	o.add(observed{kind: "terminal", task: taskID, state: state, source: source, text: reason})
}

func (o *recordingObserver) CancelPublished(_, taskID string) {
	o.add(observed{kind: "cancel", task: taskID})
}

func (o *recordingObserver) TaskDelivered(_, taskID, result string) {
	o.add(observed{kind: "delivered", task: taskID, text: result})
}

func (o *recordingObserver) events() []observed {
	o.mu.Lock()
	defer o.mu.Unlock()
	return append([]observed(nil), o.got...)
}

// kinds is the observer's calls as "kind:task" strings, for one readable
// failure message.
func (o *recordingObserver) kinds() []string {
	var out []string
	for _, e := range o.events() {
		out = append(out, e.kind+":"+e.task)
	}
	return out
}

func (o *recordingObserver) terminalFor(taskID string) (observed, bool) {
	for _, e := range o.events() {
		if e.kind == "terminal" && e.task == taskID {
			return e, true
		}
	}
	return observed{}, false
}

// startObservedRig is startRigWithSpawnerCap with the fake wrapped in a
// recordingObserver.
func startObservedRig(t *testing.T, tweak func(*Config)) (*rig, *fakeSpawner, *recordingObserver) {
	t.Helper()
	var obs *recordingObserver
	r, spawn := startRigWithSpawnerAdapter(t, "platform", 0, tweak, func(a *fakeAdapter) Adapter {
		obs = &recordingObserver{fakeAdapter: a}
		return obs
	})
	return r, spawn, obs
}

// armInjectMap arms the inject door's principal map on the fake rig, so a
// turn stamped Backend inject from author 1001 verifies.
func armInjectMap(t *testing.T, c *Config) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "inject-map")
	if err := os.WriteFile(path, []byte(injectPrincipalPrefix+"1001 eval:bnaylor\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	c.InjectListen, c.InjectToken, c.InjectPrincipalMapPath = "127.0.0.1:0", "unused", path
}

// doorDelegation arms the door's map and a door list naming 1001, so a door
// turn may delegate.
func doorDelegation(t *testing.T) func(*Config) {
	return func(c *Config) {
		armDoorMap(t, c)
		c.TargetAllowedUsers = map[string]map[string][]string{targetPlatform: {a2aBackend: {"1001"}}}
	}
}

// delegatedVia is delegated for a turn stamped with backend.
func delegatedVia(t *testing.T, r *rig, spawn *fakeSpawner, conv, backend string) (*lib.Envelope, string, *lib.Envelope) {
	t.Helper()
	exec, origin, session := sessionTurnVia(t, r, spawn, conv, backend, "how is the fleet?")
	if err := exec.PublishArtifact(context.Background(), delegateArtifact(t, "platform", "report fleet health")); err != nil {
		t.Fatal(err)
	}
	child := awaitSubmission(t, r, targetPlatform, 0)
	waitFor(t, "chain on the record", func() bool {
		rec, _ := r.g.reg.Get(context.Background(), conv)
		pref, _ := rec.TaskRefFor(origin.TaskID)
		return len(pref.Children) == 1
	})
	completeTask(t, exec, "delegated to platform")
	waitFor(t, "parent terminal relayed", postedContaining(r, "delegated to platform"))
	return origin, session, child
}

// TestADelegatingTurnIsOneTaskToTheObserver: through the A2A door, the inject
// door and a chat backend alike, a turn that delegates and is woken is told
// to the observer as its own task alone: one start, one deliverable (the
// wake's result, not the parent's "delegated to platform" nor the child's
// raw result), then one completed terminal, in that order, and nothing at
// all under the child's or the wake's id.
func TestADelegatingTurnIsOneTaskToTheObserver(t *testing.T) {
	for _, tc := range []struct {
		name, backend, conv string
		tweak               func(t *testing.T) func(*Config)
	}{
		{"the A2A door", a2aBackend, "a2a:agent-1001/ctx-one", doorDelegation},
		{"the inject door", injectBackend, injectKeyPrefix + "case-one", func(t *testing.T) func(*Config) {
			return func(c *Config) { armInjectMap(t, c) }
		}},
		{"a chat backend", "", "discord:g1/t-one", func(*testing.T) func(*Config) { return nil }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, spawn, obs := startObservedRig(t, tc.tweak(t))
			origin, _, child := delegatedVia(t, r, spawn, tc.conv, tc.backend)
			if _, ended := obs.terminalFor(origin.TaskID); ended {
				t.Fatalf("the delegating turn's own end reached the observer: %v", obs.kinds())
			}
			completeTask(t, r.execFor(t, child, targetPlatform), "fleet is green")
			waitFor(t, "wake spawn", func() bool { return len(spawn.calls()) == 2 })
			wakeSession := spawn.calls()[1].Session
			wake := r.awaitTask(t, wakeSession)
			wexec := r.execFor(t, wake, wakeSession)
			_ = wexec.PublishStatus(context.Background(), lib.StateWorking, false)
			completeTask(t, wexec, "the fleet is healthy")
			waitFor(t, "the root's terminal", func() bool { _, ok := obs.terminalFor(origin.TaskID); return ok })

			want := []string{"started:" + origin.TaskID, "accepted:" + origin.TaskID,
				"delivered:" + origin.TaskID, "terminal:" + origin.TaskID}
			if got := obs.kinds(); strings.Join(got, " ") != strings.Join(want, " ") {
				t.Fatalf("observer calls = %v, want %v (child %s, wake %s)", got, want, child.TaskID, wake.TaskID)
			}
			ev := obs.events()
			if ev[2].text != "the fleet is healthy" {
				t.Fatalf("the root's deliverable = %q, want the wake's result", ev[2].text)
			}
			if ev[3].state != lib.StateCompleted || ev[3].source != TerminalFromExecutor {
				t.Fatalf("the root's terminal = %+v, want the wake's completed from the executor", ev[3])
			}
		})
	}
}

// TestARefusedDelegationLeavesTheParentsOwnEnd: no child was minted (a door
// turn with no list), so nothing is withheld: the turn's own deliverable and
// terminal reach the observer as any turn's do.
func TestARefusedDelegationLeavesTheParentsOwnEnd(t *testing.T) {
	r, spawn, obs := startObservedRig(t, func(c *Config) { armDoorMap(t, c) })
	exec, origin, _ := sessionTurnVia(t, r, spawn, "a2a:agent-1001/ctx-refused", a2aBackend, "do a thing")
	if err := exec.PublishArtifact(context.Background(), delegateArtifact(t, "platform", "x")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "refusal line", loggedContaining(r, "delegation refused", "rule="+ruleDelegationDoorUnlisted))
	completeTask(t, exec, "delegated to platform")
	waitFor(t, "the turn's terminal", func() bool { _, ok := obs.terminalFor(origin.TaskID); return ok })
	want := []string{"started:" + origin.TaskID, "accepted:" + origin.TaskID,
		"delivered:" + origin.TaskID, "terminal:" + origin.TaskID}
	if got := obs.kinds(); strings.Join(got, " ") != strings.Join(want, " ") {
		t.Fatalf("observer calls = %v, want %v", got, want)
	}
	if ev := obs.events(); ev[2].text != "delegated to platform" || ev[3].state != lib.StateCompleted {
		t.Fatalf("deliverable %q, terminal %+v", ev[2].text, ev[3])
	}
}

// TestAChainThatEndsWithoutAWakeEndsTheRoot: when the child's end starts no
// wake, the root's one terminal is announced from it. A human stop is the
// root canceled; a wake that cannot run (here, the requester aged out) is
// the root failed, with a reason token naming it. Neither delivers.
func TestAChainThatEndsWithoutAWakeEndsTheRoot(t *testing.T) {
	t.Run("a human stop", func(t *testing.T) {
		r, spawn, obs := startObservedRig(t, doorDelegation(t))
		conv := "a2a:agent-1001/ctx-stop"
		origin, _, child := delegatedVia(t, r, spawn, conv, a2aBackend)
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001",
			MessageID: "stop-1", Text: "stop", Backend: a2aBackend}
		waitFor(t, "cancel sent", postedContaining(r, "cancel sent"))
		_ = r.execFor(t, child, targetPlatform).PublishStatus(context.Background(), lib.StateCanceled, true)
		waitFor(t, "the root's terminal", func() bool { _, ok := obs.terminalFor(origin.TaskID); return ok })
		end, _ := obs.terminalFor(origin.TaskID)
		if end.state != lib.StateCanceled {
			t.Fatalf("root terminal = %+v, want canceled", end)
		}
		assertOnlyRoot(t, obs, origin.TaskID)
		assertNoDelivery(t, obs)
	})
	t.Run("a wake that cannot run", func(t *testing.T) {
		r, spawn, obs := startObservedRig(t, doorDelegation(t))
		conv := "a2a:agent-1001/ctx-gone"
		origin, _, child := delegatedVia(t, r, spawn, conv, a2aBackend)
		putRecord(t, r, conv, func(rec *SessionRecord) {
			for i := range rec.Tasks {
				if rec.Tasks[i].ID == origin.TaskID {
					rec.Tasks[i].Requester, rec.Tasks[i].Attribution = nil, nil
				}
			}
		})
		completeTask(t, r.execFor(t, child, targetPlatform), "fleet is green")
		waitFor(t, "the root's terminal", func() bool { _, ok := obs.terminalFor(origin.TaskID); return ok })
		end, _ := obs.terminalFor(origin.TaskID)
		if end.state != lib.StateFailed || !strings.HasPrefix(end.text, "reason: "+reasonWakeNotStarted+" - ") {
			t.Fatalf("root terminal = %+v, want failed with reason %s", end, reasonWakeNotStarted)
		}
		assertOnlyRoot(t, obs, origin.TaskID)
		assertNoDelivery(t, obs)
	})
}

func assertOnlyRoot(t *testing.T, obs *recordingObserver, root string) {
	t.Helper()
	terminals := 0
	for _, e := range obs.events() {
		if e.task != root {
			t.Fatalf("the observer was told about %s, not the root %s: %v", e.task, root, obs.kinds())
		}
		if e.kind == "terminal" {
			terminals++
		}
	}
	if terminals != 1 {
		t.Fatalf("root terminals = %d, want 1: %v", terminals, obs.kinds())
	}
}

func assertNoDelivery(t *testing.T, obs *recordingObserver) {
	t.Helper()
	for _, e := range obs.events() {
		if e.kind == "delivered" {
			t.Fatalf("a chain with no wake delivered %q", e.text)
		}
	}
}

// TestAHealedChildsResultIsNotTheRootsDeliverable: the heal that finds a
// child's terminal on the stream (the relay never delivered it) hands the
// observer nothing for the child; the wake it starts delivers under the
// root, as the relay's wake does.
func TestAHealedChildsResultIsNotTheRootsDeliverable(t *testing.T) {
	r, spawn, _ := startObservedRig(t, doorDelegation(t))
	conv := "a2a:agent-1001/ctx-heal"
	origin, _, child := delegatedVia(t, r, spawn, conv, a2aBackend)
	cexec := r.execFor(t, child, targetPlatform)
	var obs *recordingObserver
	r2, spawn2 := restartRigWrapped(t, r, func(a *fakeAdapter) Adapter {
		obs = &recordingObserver{fakeAdapter: a}
		return obs
	}, func() {
		completeTask(t, cexec, "fleet is green")
		drainRelayDurable(t, r.url)
	})
	r2.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001",
		MessageID: "h-heal", Text: "status", Backend: a2aBackend}
	waitFor(t, "the heal's status card", postedContaining(r2, "🔎 task `"+child.TaskID+"` is **completed**"))
	waitFor(t, "wake spawn", func() bool { return len(spawn2.calls()) == 1 })
	wakeSession := spawn2.calls()[0].Session
	wake := r2.awaitTask(t, wakeSession)
	for _, e := range obs.events() {
		if e.task != origin.TaskID || e.kind == "delivered" || e.kind == "terminal" {
			t.Fatalf("the heal told the observer %v before the wake ended", obs.kinds())
		}
	}
	wexec := r2.execFor(t, wake, wakeSession)
	_ = wexec.PublishStatus(context.Background(), lib.StateWorking, false)
	completeTask(t, wexec, "the fleet is healthy")
	waitFor(t, "the root's terminal", func() bool { _, ok := obs.terminalFor(origin.TaskID); return ok })
	var delivered []string
	for _, e := range obs.events() {
		if e.task != origin.TaskID {
			t.Fatalf("the observer was told about %s: %v", e.task, obs.kinds())
		}
		if e.kind == "delivered" {
			delivered = append(delivered, e.text)
		}
	}
	if len(delivered) != 1 || delivered[0] != "the fleet is healthy" {
		t.Fatalf("root deliverables = %q, want the wake's result once", delivered)
	}
}

// TestACancelNamingTheRootStopsTheActiveChild: a door caller holds only the
// root's id, so tasks/cancel names it while the child runs. The cancel goes
// to the child, the task that is running, and is announced under the root.
func TestACancelNamingTheRootStopsTheActiveChild(t *testing.T) {
	r, spawn, obs := startObservedRig(t, doorDelegation(t))
	conv := "a2a:agent-1001/ctx-cancel"
	origin, _, child := delegatedVia(t, r, spawn, conv, a2aBackend)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001",
		MessageID: "c-1", Text: "stop", Backend: a2aBackend, Intent: IntentCancel, TaskID: origin.TaskID}
	waitFor(t, "a cancel on the child's subject", func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, targetPlatform) {
			if e.Kind == lib.KindCancel && e.TaskID == child.TaskID {
				return true
			}
		}
		return false
	})
	waitFor(t, "the cancel announced", func() bool {
		for _, e := range obs.events() {
			if e.kind == "cancel" {
				return true
			}
		}
		return false
	})
	for _, e := range obs.events() {
		if e.kind == "cancel" && e.task != origin.TaskID {
			t.Fatalf("the cancel was announced under %s, want the root %s", e.task, origin.TaskID)
		}
	}
}

// TestTheProbeReadsTheRootAsTheChainsActiveTask: a program grading the root
// reads the chain's running task through the probe, not the parent's own
// finished stream, until the chain ends.
func TestTheProbeReadsTheRootAsTheChainsActiveTask(t *testing.T) {
	r, spawn, _ := startObservedRig(t, doorDelegation(t))
	conv := "a2a:agent-1001/ctx-probe"
	origin, _, child := delegatedVia(t, r, spawn, conv, a2aBackend)
	if err := r.execFor(t, child, targetPlatform).PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	var st ConversationState
	waitFor(t, "the probe sees the child working", func() bool {
		var err error
		st, err = r.g.probeConversation(context.Background(), conv, origin.TaskID)
		return err == nil && st.ExecutorState == lib.StateWorking
	})
	if !st.Active || st.Final || st.TaskID != origin.TaskID {
		t.Fatalf("probe of the root = %+v, want the active chain under the root's id", st)
	}
}

// TestTheA2ADoorShowsADelegatingTurnAsOneTask: end to end through the real
// door. A caller's message/send starts a session turn that delegates; the
// child runs and wakes the session; the wake answers. The caller's one task
// stays live through the chain and ends completed with the wake's result as
// its artifact, and the door never learns the child's or the wake's id.
func TestTheA2ADoorShowsADelegatingTurnAsOneTask(t *testing.T) {
	spawn := &fakeSpawner{}
	r := startA2ARigOpts(t, func(d *A2ADoor) Adapter { return d }, spawn, func(c *Config) {
		c.TargetAllowedUsers = map[string]map[string][]string{targetPlatform: {a2aBackend: {a2aTestCaller}}}
	})
	ctx := context.Background()
	root := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("/session how is the fleet?", "m-chain-1", "", false)))
	waitFor(t, "spawn", func() bool { return len(spawn.calls()) == 1 })
	session := spawn.calls()[0].Session
	origin := r.awaitTask(t, session)
	if origin.TaskID != root.ID {
		t.Fatalf("the door's task %s is not the turn's %s", root.ID, origin.TaskID)
	}
	exec := r.execFor(t, origin, session)
	_ = exec.PublishStatus(ctx, lib.StateWorking, false)
	if err := exec.PublishArtifact(ctx, delegateArtifact(t, "platform", "report fleet health")); err != nil {
		t.Fatal(err)
	}
	child := r.awaitTask(t, targetPlatform)
	completeTask(t, exec, "delegated to platform")
	// The parent's own end is withheld: the caller's task is still live.
	time.Sleep(300 * time.Millisecond)
	if got := taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodGet, map[string]any{"id": root.ID})); got.Status.State == lib.StateCompleted {
		t.Fatalf("the caller's task completed on the delegating turn's own end: %+v", got.Status)
	}
	cexec := r.execFor(t, child, targetPlatform)
	_ = cexec.PublishStatus(ctx, lib.StateWorking, false)
	completeTask(t, cexec, "fleet is green")
	waitFor(t, "wake spawn", func() bool { return len(spawn.calls()) == 2 })
	wakeSession := spawn.calls()[1].Session
	wake := r.awaitTask(t, wakeSession)
	wexec := r.execFor(t, wake, wakeSession)
	_ = wexec.PublishStatus(ctx, lib.StateWorking, false)
	completeTask(t, wexec, "the fleet is healthy")

	got := r.getUntil(t, a2aTestCaller, root.ID, "the caller's task completed", func(o a2aTaskObject) bool {
		return o.Status.State == lib.StateCompleted
	})
	if len(got.Artifacts) != 1 || joinTextParts(got.Artifacts[0].Parts) != "the fleet is healthy" {
		t.Fatalf("artifacts = %+v, want the wake's result", got.Artifacts)
	}
	for _, id := range []string{child.TaskID, wake.TaskID} {
		if resp := r.rpc(t, a2aTestCaller, a2aMethodGet, map[string]any{"id": id}); resp.Error == nil {
			t.Fatalf("the door knows the chain's inner task %s", id)
		}
	}
}
