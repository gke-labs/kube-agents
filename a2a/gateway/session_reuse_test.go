package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sfake "k8s.io/client-go/kubernetes/fake"
	"k8s.io/utils/ptr"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func reuseOn(c *Config) { c.SessionReuse = true }

// startReuseRig is the post-flip gateway (every conversation a session) with
// pod reuse on.
func startReuseRig(t *testing.T) (*rig, *fakeSpawner) {
	t.Helper()
	return startRigWithSpawnerCap(t, RouteSession, 0, reuseOn)
}

func say(r *rig, conv, id, text string) {
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: id, Text: text}
}

func waitIdle(t *testing.T, r *rig, conv string) {
	t.Helper()
	waitFor(t, "the turn's terminal relayed", func() bool {
		rec, err := r.g.reg.Get(context.Background(), conv)
		return err == nil && rec != nil && rec.ActiveTask == nil
	})
}

// The second turn of a conversation goes to the pod the first one started:
// no spawn, no delete, the same bus session, and a capability minted for that
// same session as its delegate. Then a stop reaches the task that is running
// in it, not the one before.
func TestASecondTurnGoesToTheLivePod(t *testing.T) {
	r, spawn := startReuseRig(t)
	ctx := context.Background()
	conv := "discord:g1/t-reuse-1"
	say(r, conv, "m1", "first question")
	waitFor(t, "first spawn", func() bool { return len(spawn.calls()) == 1 })
	session := spawn.calls()[0].Session
	spawn.setReusable(session)
	first := r.awaitTask(t, session)
	completeTask(t, r.execFor(t, first, session), "first answer")
	waitIdle(t, r, conv)

	rec, _ := r.g.reg.Get(ctx, conv)
	if !rec.PodReuse || rec.PodName != session {
		t.Fatalf("after the first turn: PodReuse=%v PodName=%q, want a reusable pod %q", rec.PodReuse, rec.PodName, session)
	}

	say(r, conv, "m2", "second question")
	second := awaitSubmission(t, r, session, 1)
	if got := envText(t, second); !strings.Contains(got, "second question") {
		t.Fatalf("the second submission on %s reads %q", session, got)
	}
	if n := len(spawn.calls()); n != 1 {
		t.Fatalf("%d spawns, want 1: the live pod should have taken the second turn", n)
	}
	if d := spawn.deleted(); len(d) != 0 {
		t.Fatalf("pods deleted %v; the live pod must stay", d)
	}
	rec, _ = r.g.reg.Get(ctx, conv)
	if rec.BusSession != session || rec.Addressee != session || rec.PodName != session {
		t.Fatalf("record busSession=%s addressee=%s pod=%s, want all %s", rec.BusSession, rec.Addressee, rec.PodName, session)
	}
	var auth Authority
	if err := json.Unmarshal(second.Authority, &auth); err != nil {
		t.Fatal(err)
	}
	assertRootCapability(t, r, auth, second.TaskID, session)

	say(r, conv, "m3", "stop")
	waitFor(t, "cancel on the running task", func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, session) {
			if e.Kind == lib.KindCancel {
				return e.TaskID == second.TaskID
			}
		}
		return false
	})
	for _, e := range inSubjectEnvelopes(t, r.url, session) {
		if e.Kind == lib.KindCancel && e.TaskID != second.TaskID {
			t.Fatalf("a cancel reached task %s, not the running %s", e.TaskID, second.TaskID)
		}
	}
}

// Every way the live pod cannot take the turn falls back to today's path:
// retire it, mint a new incarnation, spawn cold.
func TestATurnStartsAFreshPodWhenTheLiveOneCannotTakeIt(t *testing.T) {
	const pod = "chat-otter-r9"
	for _, tc := range []struct {
		name        string
		reuseOff    bool
		podReuse    bool
		reusable    bool
		reusableErr error
		detached    bool
	}{
		{name: "the pod is not live", podReuse: true},
		{name: "the liveness read fails", podReuse: true, reusable: true, reusableErr: errors.New("api down")},
		{name: "an older gateway spawned it", podReuse: false, reusable: true},
		{name: "a stopped task is still finishing on it", podReuse: true, reusable: true, detached: true},
		{name: "reuse is switched off", reuseOff: true, podReuse: true, reusable: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tweak := reuseOn
			if tc.reuseOff {
				tweak = func(*Config) {}
			}
			r, spawn := startRigWithSpawnerCap(t, RouteSession, 0, tweak)
			conv := "discord:g1/t-fresh"
			rec := &SessionRecord{
				Key: conv, ContextID: "ctx-fresh", Kind: "group",
				Addressee: pod, BusSession: pod, PodName: pod, PodReuse: tc.podReuse,
				SessionRouted: true, Profile: "chat", LastActivity: time.Now().UTC(),
			}
			if tc.detached {
				rec.ActiveTask = &ActiveTask{TaskID: "task-old", CorrelationID: "corr-old", Detached: true}
				rec.Tasks = []TaskRef{{ID: "task-old", Addressee: pod, Canceled: true}}
			}
			if err := r.g.reg.Put(context.Background(), rec); err != nil {
				t.Fatal(err)
			}
			if tc.reusable {
				spawn.setReusable(pod)
			}
			spawn.reusableErr = tc.reusableErr
			say(r, conv, "m1", "next question")
			waitFor(t, "a fresh spawn", func() bool { return len(spawn.calls()) == 1 })
			if s := spawn.calls()[0].Session; s == pod {
				t.Fatalf("the fresh spawn reused the name %s", s)
			}
			if d := spawn.deleted(); !slices.Contains(d, pod) {
				t.Fatalf("deleted %v, want the old pod retired", d)
			}
			if tc.detached {
				if s := terminalFor(t, r.url, pod, "task-old"); s == nil || s.Status.State != lib.StateCanceled {
					t.Fatalf("the stopped task's terminal = %+v, want canceled before the retirement", s)
				}
			}
		})
	}
}

// On a conversation marked with a bare /session before the flip, a
// "/session <text>" turn is an ordinary turn and goes to the live pod the same
// way.
func TestASessionCommandTurnGoesToTheLivePod(t *testing.T) {
	r, spawn := startRigWithSpawnerCap(t, "platform", 0, reuseOn)
	ctx := context.Background()
	conv := "discord:g1/t-reuse-cmd"
	_, first, session := sessionTurnVia(t, r, spawn, conv, "", "first question")
	spawn.setReusable(session)
	completeTask(t, r.execFor(t, first, session), "first answer")
	waitIdle(t, r, conv)

	say(r, conv, "m2", "/session second question")
	second := awaitSubmission(t, r, session, 1)
	if got := envText(t, second); !strings.Contains(got, "second question") {
		t.Fatalf("the second submission on %s reads %q", session, got)
	}
	if n := len(spawn.calls()); n != 1 {
		t.Fatalf("%d spawns, want 1: the live pod should have taken the /session turn", n)
	}
	if rec, _ := r.g.reg.Get(ctx, conv); rec.BusSession != session {
		t.Fatalf("busSession = %s, want %s", rec.BusSession, session)
	}
}

// A wake into a live pod goes to that pod: the same incarnation, so its
// author set is the delegating turn's already and nothing is re-seeded, and
// the wake still carries via, which is what turns its delegate tool off.
func TestAWakeGoesToTheLivePod(t *testing.T) {
	r, spawn := startRigWithSpawnerCap(t, "platform", 0, reuseOn)
	ctx := context.Background()
	conv := "discord:g1/t-wake-reuse"
	_, session, child := delegated(t, r, spawn, conv, "")
	spawn.setReusable(session)
	before, _ := r.g.reg.Get(ctx, conv)
	authorsBefore := append([]TaskRequester(nil), before.SessionAuthors...)

	completeTask(t, r.execFor(t, child, targetPlatform), "fleet is green")
	wake := awaitSubmission(t, r, session, 1)
	if n := len(spawn.calls()); n != 1 {
		t.Fatalf("%d spawns, want 1: the wake belongs in the live pod", n)
	}
	var auth Authority
	if err := json.Unmarshal(wake.Authority, &auth); err != nil {
		t.Fatal(err)
	}
	if auth.Via == nil || auth.Via.Session != session || auth.Via.TaskID != child.TaskID {
		t.Fatalf("wake via = %+v, want task %s on %s", auth.Via, child.TaskID, session)
	}
	assertRootCapability(t, r, auth, wake.TaskID, session)
	waitFor(t, "the wake on the record", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		return rec.ActiveTask != nil && rec.ActiveTask.TaskID == wake.TaskID
	})
	rec, _ := r.g.reg.Get(ctx, conv)
	if rec.BusSession != session || rec.Addressee != session || rec.SessionAuthorsFor != session {
		t.Fatalf("record busSession=%s addressee=%s authorsFor=%s, want all %s", rec.BusSession, rec.Addressee, rec.SessionAuthorsFor, session)
	}
	for _, a := range authorsBefore {
		if !slices.Contains(rec.SessionAuthors, a) {
			t.Fatalf("the wake dropped author %+v from the live incarnation's set %+v", a, rec.SessionAuthors)
		}
	}
	if d := spawn.deleted(); len(d) != 0 {
		t.Fatalf("pods deleted %v; the live pod must stay", d)
	}
}

// The idle clock is the last user message. An answer that landed a minute ago
// does not keep a pod whose conversation nobody has written in past the TTL,
// and a message inside the TTL keeps it whatever LastActivity says.
func TestTheReapCountsFromTheLastUserMessage(t *testing.T) {
	r, spawn := startReuseRig(t)
	ctx := context.Background()
	now := time.Now().UTC()
	quiet := &SessionRecord{
		Key: "discord:g1/t-reap-quiet", ContextID: "ctx-q", Kind: "group",
		Addressee: "chat-vole-q", BusSession: "chat-vole-q", PodName: "chat-vole-q", PodReuse: true,
		SessionRouted: true, LastUserMessage: now.Add(-40 * time.Minute), LastActivity: now.Add(-time.Minute),
	}
	spoken := &SessionRecord{
		Key: "discord:g1/t-reap-spoken", ContextID: "ctx-s", Kind: "group",
		Addressee: "chat-vole-s", BusSession: "chat-vole-s", PodName: "chat-vole-s", PodReuse: true,
		SessionRouted: true, LastUserMessage: now.Add(-5 * time.Minute), LastActivity: now.Add(-40 * time.Minute),
	}
	for _, rec := range []*SessionRecord{quiet, spoken} {
		if err := r.g.reg.Put(ctx, rec); err != nil {
			t.Fatal(err)
		}
		r.g.reapSession(ctx, rec)
	}
	if d := spawn.deleted(); len(d) != 1 || d[0] != "chat-vole-q" {
		t.Fatalf("reap deleted %v, want only the pod nobody has written to in 40m", d)
	}
}

// A verified turn moves the idle clock; nothing else does. The negatives, for
// the relay's and the heal's executor-terminal stamps, are in
// TestTaskEndCountsAsActivity and TestHealedExecutorTerminalCountsAsActivity.
func TestATurnStampsTheLastUserMessage(t *testing.T) {
	r, spawn := startReuseRig(t)
	conv := "discord:g1/t-stamp"
	say(r, conv, "m1", "hello")
	waitFor(t, "spawn", func() bool { return len(spawn.calls()) == 1 })
	rec, _ := r.g.reg.Get(context.Background(), conv)
	if rec.LastUserMessage.IsZero() || time.Since(rec.LastUserMessage) > time.Minute {
		t.Fatalf("LastUserMessage = %v after a turn", rec.LastUserMessage)
	}
}

// A reused pod's running task past the task deadline plus the pod grace is
// closed by its supervisor and its pod retired; a younger task, a one-task
// pod's (which has its own deadline), and a task already final on the stream
// are left alone.
func TestAnOverdueTaskOnAReusedPodIsClosedAndThePodRetired(t *testing.T) {
	for _, tc := range []struct {
		name     string
		age      time.Duration
		podReuse bool
		final    bool
		retire   bool
	}{
		{name: "overdue", age: defaultTaskDeadline + podDeadlineGrace + time.Minute, podReuse: true, retire: true},
		{name: "inside its bound", age: defaultTaskDeadline, podReuse: true},
		{name: "a one-task pod", age: defaultTaskDeadline + podDeadlineGrace + time.Minute},
		{name: "already final", age: defaultTaskDeadline + podDeadlineGrace + time.Minute, podReuse: true, final: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, spawn := startReuseRig(t)
			ctx := context.Background()
			pod := "chat-lynx-o1"
			rec := &SessionRecord{
				Key: "discord:g1/t-overdue", ContextID: "ctx-o", Kind: "group",
				Addressee: pod, BusSession: pod, PodName: pod, PodReuse: tc.podReuse, SessionRouted: true,
				LastUserMessage: time.Now().UTC(),
				ActiveTask:      &ActiveTask{TaskID: "task-o1", CorrelationID: "corr-o1", SubmittedAt: time.Now().Add(-tc.age)},
				Tasks:           []TaskRef{{ID: "task-o1", Addressee: pod}},
			}
			if err := r.g.reg.Put(ctx, rec); err != nil {
				t.Fatal(err)
			}
			if tc.final {
				origin := &lib.Envelope{Kind: lib.KindMessage, TaskID: "task-o1", ContextID: "ctx-o", CorrelationID: "corr-o1"}
				exec, err := r.bus.NewTaskExecution(origin, lib.Party{Session: pod, AgentType: "test-executor"}, pod)
				if err != nil {
					t.Fatal(err)
				}
				if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
					t.Fatal(err)
				}
			}
			r.g.reapSession(ctx, rec)
			sup := supervisorTerminal(t, r, pod, "task-o1")
			fresh, _ := r.g.reg.Get(ctx, rec.Key)
			if tc.retire {
				if sup == nil || sup.Status.State != lib.StateFailed {
					t.Fatalf("supervisor terminal = %+v, want failed", sup)
				}
				if d := spawn.deleted(); len(d) != 1 || d[0] != pod {
					t.Fatalf("deleted %v, want %s", d, pod)
				}
				if fresh.PodName != "" {
					t.Fatalf("PodName = %q after the retirement", fresh.PodName)
				}
				return
			}
			if sup != nil {
				t.Fatalf("a supervisor terminal was published: %+v", sup)
			}
			if d := spawn.deleted(); len(d) != 0 {
				t.Fatalf("deleted %v, want nothing", d)
			}
		})
	}
}

// overdueRecord is a conversation whose reused pod has held its task past the
// task deadline plus the pod grace.
func overdueRecord(key, pod, taskID string) *SessionRecord {
	return &SessionRecord{
		Key: key, ContextID: "ctx-" + taskID, Kind: "group", Profile: "chat",
		Addressee: pod, BusSession: pod, PodName: pod, PodReuse: true, SessionRouted: true,
		LastUserMessage: time.Now().UTC(),
		ActiveTask: &ActiveTask{TaskID: taskID, CorrelationID: "corr-" + taskID,
			SubmittedAt: time.Now().Add(-(defaultTaskDeadline + podDeadlineGrace + time.Minute))},
		Tasks: []TaskRef{{ID: taskID, Addressee: pod}},
	}
}

// A reused pod's first task counts its bound from the pod's start when the
// pod started after the submission, the clock a one-task pod's
// activeDeadlineSeconds used: a slow schedule or image pull does not come out
// of the grace that was sized for it. The same task is retired once the bound
// from the start has passed, and a pod with no start time on record leaves
// the submission's clock in charge.
func TestASlowPodStartDoesNotSpendTheOverdueGrace(t *testing.T) {
	r, spawn := startReuseRig(t)
	ctx := context.Background()
	bound := defaultTaskDeadline + podDeadlineGrace

	late := overdueRecord("discord:g1/t-overdue-late", "chat-lynx-late", "task-late")
	spawn.setStarted("chat-lynx-late", time.Now().Add(-(bound - 5*time.Minute)))
	gone := overdueRecord("discord:g1/t-overdue-gone", "chat-lynx-gone", "task-gone")
	for _, rec := range []*SessionRecord{late, gone} {
		if err := r.g.reg.Put(ctx, rec); err != nil {
			t.Fatal(err)
		}
	}
	r.g.retireOverdue(ctx, late.Key)
	if sup := supervisorTerminal(t, r, "chat-lynx-late", "task-late"); sup != nil {
		t.Fatalf("a task whose pod started %v ago was failed as overdue: %+v", bound-5*time.Minute, sup)
	}
	if d := spawn.deleted(); len(d) != 0 {
		t.Fatalf("deleted %v, want nothing while the pod's own bound runs", d)
	}

	spawn.setStarted("chat-lynx-late", time.Now().Add(-(bound + time.Minute)))
	r.g.retireOverdue(ctx, late.Key)
	if sup := supervisorTerminal(t, r, "chat-lynx-late", "task-late"); sup == nil || sup.Status.State != lib.StateFailed {
		t.Fatalf("past the bound from the pod's start: supervisor terminal = %+v, want failed", sup)
	}

	r.g.retireOverdue(ctx, gone.Key)
	if sup := supervisorTerminal(t, r, "chat-lynx-gone", "task-gone"); sup == nil || sup.Status.State != lib.StateFailed {
		t.Fatalf("no start time: supervisor terminal = %+v, want failed on the submission's clock", sup)
	}
}

// A pod read that fails holds the overdue check for the next pass rather
// than guessing which clock applies.
func TestAnOverdueCheckWaitsOutAFailedPodRead(t *testing.T) {
	r, spawn := startReuseRig(t)
	ctx := context.Background()
	rec := overdueRecord("discord:g1/t-overdue-read", "chat-lynx-read", "task-read")
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	spawn.mu.Lock()
	spawn.startedErr = errors.New("apiserver unavailable")
	spawn.mu.Unlock()
	r.g.retireOverdue(ctx, rec.Key)
	if sup := supervisorTerminal(t, r, "chat-lynx-read", "task-read"); sup != nil {
		t.Fatalf("a supervisor terminal was published on a failed pod read: %+v", sup)
	}
	if d := spawn.deleted(); len(d) != 0 {
		t.Fatalf("deleted %v, want nothing", d)
	}
}

// A gateway with no spawner (A2A_SPAWN_SESSIONS dropped after an armed run
// spawned the pod) leaves an overdue task alone: it cannot delete the pod, so
// it does not write the terminal the deletion rule pairs with the delete, and
// the reap does not dereference the missing spawner.
func TestAnOverdueTaskIsLeftAloneWithoutASpawner(t *testing.T) {
	r := startRigWith(t, reuseOn)
	ctx := context.Background()
	pod := "chat-lynx-nil"
	rec := overdueRecord("discord:g1/t-overdue-nil", pod, "task-nil")
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	if r.g.taskOverdue(rec, time.Now()) {
		t.Fatal("taskOverdue is true with no spawner")
	}
	r.g.reapSession(ctx, rec)
	r.g.retireOverdue(ctx, rec.Key)
	if sup := supervisorTerminal(t, r, pod, "task-nil"); sup != nil {
		t.Fatalf("a supervisor terminal was published with nothing to delete the pod: %+v", sup)
	}
	fresh, err := r.g.reg.Get(ctx, rec.Key)
	if err != nil || fresh == nil {
		t.Fatalf("record: %+v, %v", fresh, err)
	}
	if fresh.PodName != pod || !fresh.PodReuse || fresh.ActiveTask == nil {
		t.Fatalf("record changed with no spawner: pod %q reuse %v active %+v", fresh.PodName, fresh.PodReuse, fresh.ActiveTask)
	}
}

// A failed delete after the overdue terminal leaves the pod on the record for
// the reap, but unmarked: once the relay releases the task, the next turn
// retires the wedged pod and spawns fresh rather than being routed to it.
func TestAFailedOverdueDeleteUnmarksThePod(t *testing.T) {
	r, spawn := startReuseRig(t)
	ctx := context.Background()
	pod := "chat-lynx-del"
	rec := overdueRecord("discord:g1/t-overdue-del", pod, "task-del")
	// The task is not indexed, so the relay never routes its terminal and
	// cannot be what unmarks the pod: this is retireOverdue's own write.
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	spawn.setDeleteErr(errors.New("apiserver unavailable"))
	r.g.reapSession(ctx, rec)
	if sup := supervisorTerminal(t, r, pod, "task-del"); sup == nil || sup.Status.State != lib.StateFailed {
		t.Fatalf("supervisor terminal = %+v, want failed", sup)
	}
	if d := spawn.deleted(); len(d) != 1 || d[0] != pod {
		t.Fatalf("delete attempts %v, want one for %s", d, pod)
	}
	fresh, _ := r.g.reg.Get(ctx, rec.Key)
	if fresh.PodName != pod || fresh.PodReuse {
		t.Fatalf("after the failed delete: pod %q reuse %v, want the pod kept and unmarked", fresh.PodName, fresh.PodReuse)
	}
	// The release the relay would make on that terminal.
	fresh.ActiveTask = nil
	if err := r.g.reg.Put(ctx, fresh); err != nil {
		t.Fatal(err)
	}

	spawn.setDeleteErr(nil)
	spawn.setReusable(pod)
	say(r, rec.Key, "m-del-1", "are you there?")
	waitFor(t, "a fresh spawn", func() bool { return len(spawn.calls()) == 1 })
	if s := spawn.calls()[0].Session; s == pod {
		t.Fatal("the next turn went to the wedged pod")
	}
	if d := spawn.deleted(); len(d) != 2 || d[1] != pod {
		t.Fatalf("deleted %v, want the wedged pod retired by the next turn", d)
	}
}

// A gateway that stops between the overdue terminal and its record write
// leaves the pod marked. The relay that later releases the task on that
// supervisor's terminal unmarks it in the same write, so the next turn never
// sees the pod free and still reusable. An executor's own terminal leaves the
// mark alone.
func TestASupervisorTerminalUnmarksAReusedPod(t *testing.T) {
	for _, tc := range []struct {
		name       string
		supervisor bool
	}{
		{name: "supervisor", supervisor: true},
		{name: "executor"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, _ := startReuseRig(t)
			ctx := context.Background()
			pod := "chat-lynx-rs"
			rec := overdueRecord("discord:g1/t-restart-"+tc.name, pod, "task-rs-"+tc.name)
			taskID := rec.ActiveTask.TaskID
			if err := r.g.reg.Put(ctx, rec); err != nil {
				t.Fatal(err)
			}
			if err := r.g.reg.IndexTask(ctx, taskID, rec.Key); err != nil {
				t.Fatal(err)
			}
			if tc.supervisor {
				if err := r.g.publishSupervisorTerminal(ctx, pod, taskID, rec.ContextID, rec.ActiveTask.CorrelationID,
					lib.StateFailed, noteTaskOverdue); err != nil {
					t.Fatal(err)
				}
			} else {
				origin := &lib.Envelope{Kind: lib.KindMessage, TaskID: taskID, ContextID: rec.ContextID, CorrelationID: rec.ActiveTask.CorrelationID}
				exec, err := r.bus.NewTaskExecution(origin, lib.Party{Session: pod, AgentType: "test-executor"}, pod)
				if err != nil {
					t.Fatal(err)
				}
				if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
					t.Fatal(err)
				}
			}
			waitIdle(t, r, rec.Key)
			fresh, _ := r.g.reg.Get(ctx, rec.Key)
			if fresh.PodReuse == tc.supervisor {
				t.Fatalf("PodReuse = %v after the %s terminal", fresh.PodReuse, tc.name)
			}
		})
	}
}

// The heal's half of the same rule: a gateway that stopped between the
// overdue terminal and its record write, whose relay never delivers that
// terminal (here the task is not indexed), leaves the record holding the task
// and the pod marked. The next turn's heal finds the supervisor's terminal on
// the stream and releases the task, and unmarks the pod in the same write,
// so that turn retires the pod and spawns fresh instead of going to it.
func TestTheHealUnmarksAReusedPodOnASupervisorTerminal(t *testing.T) {
	r, spawn := startReuseRig(t)
	ctx := context.Background()
	pod := "chat-lynx-heal"
	rec := overdueRecord("discord:g1/t-heal-unmark", pod, "task-heal")
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	if err := r.g.publishSupervisorTerminal(ctx, pod, "task-heal", rec.ContextID, rec.ActiveTask.CorrelationID,
		lib.StateFailed, noteTaskOverdue); err != nil {
		t.Fatal(err)
	}
	spawn.setReusable(pod)
	say(r, rec.Key, "m-heal-1", "are you there?")
	waitFor(t, "a fresh spawn", func() bool { return len(spawn.calls()) == 1 })
	if s := spawn.calls()[0].Session; s == pod {
		t.Fatal("the next turn went to the pod whose task the supervisor failed")
	}
	if d := spawn.deleted(); !slices.Contains(d, pod) {
		t.Fatalf("deleted %v, want the unmarked pod retired by the next turn", d)
	}
}

// supervisorTerminal is the final the gateway wrote for a task as its
// supervisor, or nil.
func supervisorTerminal(t *testing.T, r *rig, addressee, taskID string) *lib.StatusUpdate {
	t.Helper()
	envs, err := eventsEnvelopes(r.url, addressee)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range envs {
		var s lib.StatusUpdate
		if e.TaskID == taskID && e.Kind == lib.KindStatusUpdate && e.From.Session == gatewayParty.Session &&
			json.Unmarshal(e.Payload, &s) == nil && s.Final {
			return &s
		}
	}
	return nil
}

// postTextsFor is every post the gateway made into one conversation.
func (a *fakeAdapter) postTextsFor(conversation string) []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	var out []string
	for _, p := range a.posts {
		if p.Conversation == conversation {
			out = append(out, p.Text)
		}
	}
	return out
}

// A reused pod's annotation names its first task only. When it dies on a
// later turn, the sweep has to close that turn, which the record holds as
// active, and not report a clean exit because the first task is final.
func TestTheSweepClosesTheTaskAReusedPodDiedOn(t *testing.T) {
	r, spawn := startReuseRig(t)
	ctx := context.Background()
	pod := "chat-heron-sw"
	rec := &SessionRecord{
		Key: "discord:g1/t-sweep-reuse", ContextID: "ctx-sw", Kind: "group",
		Addressee: pod, BusSession: pod, PodName: pod, PodReuse: true, SessionRouted: true,
		ActiveTask: &ActiveTask{TaskID: "task-sw-3", CorrelationID: "corr-sw-3", SubmittedAt: time.Now()},
		Tasks: []TaskRef{
			{ID: "task-sw-1", Addressee: pod},
			{ID: "task-sw-2", Addressee: pod},
			{ID: "task-sw-3", Addressee: pod},
		},
	}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	origin := &lib.Envelope{Kind: lib.KindMessage, TaskID: "task-sw-1", ContextID: "ctx-sw", CorrelationID: "corr-sw-1"}
	exec, err := r.bus.NewTaskExecution(origin, lib.Party{Session: pod, AgentType: "test-executor"}, pod)
	if err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	spawn.setOrphans([]orphanPod{{PodName: pod, SessionKey: rec.Key, Addressee: pod,
		TaskID: "task-sw-1", ContextID: "ctx-sw", CorrelationID: "corr-sw-1"}})

	r.g.sweepOnce(ctx)

	s := terminalFor(t, r.url, pod, "task-sw-3")
	if s == nil || s.Status.State != lib.StateFailed {
		t.Fatalf("the turn the pod died on has terminal %+v, want failed by its supervisor", s)
	}
	if sup := supervisorTerminal(t, r, pod, "task-sw-1"); sup != nil {
		t.Fatalf("the sweep wrote a second final for the first task: %+v", sup)
	}
	if d := spawn.deleted(); len(d) != 1 || d[0] != pod {
		t.Fatalf("deleted %v, want %s", d, pod)
	}

	// A pod whose conversation has moved on to another pod closes nothing
	// of the other pod's: the active task there is not this pod's to end.
	moved := &SessionRecord{
		Key: "discord:g1/t-sweep-moved", ContextID: "ctx-mv", Kind: "group",
		Addressee: "chat-heron-new", BusSession: "chat-heron-new", PodName: "chat-heron-new", PodReuse: true,
		ActiveTask: &ActiveTask{TaskID: "task-mv-2", CorrelationID: "corr-mv-2", SubmittedAt: time.Now()},
		Tasks:      []TaskRef{{ID: "task-mv-1", Addressee: "chat-heron-old"}, {ID: "task-mv-2", Addressee: "chat-heron-new"}},
	}
	if err := r.g.reg.Put(ctx, moved); err != nil {
		t.Fatal(err)
	}
	old := &lib.Envelope{Kind: lib.KindMessage, TaskID: "task-mv-1", ContextID: "ctx-mv", CorrelationID: "corr-mv-1"}
	oexec, err := r.bus.NewTaskExecution(old, lib.Party{Session: "chat-heron-old", AgentType: "test-executor"}, "chat-heron-old")
	if err != nil {
		t.Fatal(err)
	}
	if err := oexec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	spawn.setOrphans([]orphanPod{{PodName: "chat-heron-old", SessionKey: moved.Key, Addressee: "chat-heron-old",
		TaskID: "task-mv-1", ContextID: "ctx-mv", CorrelationID: "corr-mv-1"}})
	r.g.sweepOnce(ctx)
	for _, addr := range []string{"chat-heron-old", "chat-heron-new"} {
		if s := supervisorTerminal(t, r, addr, "task-mv-2"); s != nil {
			t.Fatalf("the old pod's sweep closed the new pod's task on %s: %+v", addr, s)
		}
	}
}

// At the cap, a new conversation's pod is made room for by evicting the pod
// that has been idle longest, and that conversation is told. A pod whose
// conversation is running anything is never evicted, and with only those at
// the cap the turn is refused as before.
func TestTheCapEvictsTheLongestIdlePod(t *testing.T) {
	now := time.Now().UTC()
	idle := func(key, pod string, quiet time.Duration) *SessionRecord {
		return &SessionRecord{Key: key, ContextID: "ctx-" + pod, Kind: "group", Addressee: pod, BusSession: pod,
			PodName: pod, PodReuse: true, SessionRouted: true, LastUserMessage: now.Add(-quiet)}
	}
	t.Run("an idle pod makes room", func(t *testing.T) {
		r, spawn := startRigWithSpawnerCap(t, RouteSession, 2, reuseOn)
		ctx := context.Background()
		older := idle("discord:g1/t-old", "chat-vole-old", 20*time.Minute)
		newer := idle("discord:g1/t-new", "chat-vole-new", 5*time.Minute)
		for _, rec := range []*SessionRecord{older, newer} {
			if err := r.g.reg.Put(ctx, rec); err != nil {
				t.Fatal(err)
			}
		}
		spawn.live = 2
		spawn.setSessionPods(sessionPod{PodName: "chat-vole-new", SessionKey: newer.Key, Reuse: true}, sessionPod{PodName: "chat-vole-old", SessionKey: older.Key, Reuse: true})
		say(r, "discord:g1/t-third", "m1", "a third conversation")
		waitFor(t, "the third conversation's spawn", func() bool { return len(spawn.calls()) == 1 })
		if d := spawn.deleted(); len(d) != 1 || d[0] != "chat-vole-old" {
			t.Fatalf("evicted %v, want only the longest-idle pod", d)
		}
		rec, _ := r.g.reg.Get(ctx, older.Key)
		if rec.PodName != "" {
			t.Fatalf("the evicted conversation still names its pod %q", rec.PodName)
		}
		if !slices.Contains(r.adapter.postTextsFor(older.Key), noticeSessionPaused) {
			t.Fatalf("the evicted conversation was not told; its posts: %v", r.adapter.postTextsFor(older.Key))
		}
	})
	t.Run("an untracked pod goes first, without a notice", func(t *testing.T) {
		r, spawn := startRigWithSpawnerCap(t, RouteSession, 2, reuseOn)
		ctx := context.Background()
		older := idle("discord:g1/t-old2", "chat-vole-old2", time.Hour)
		moved := idle("discord:g1/t-moved", "chat-vole-now", time.Minute)
		for _, rec := range []*SessionRecord{older, moved} {
			if err := r.g.reg.Put(ctx, rec); err != nil {
				t.Fatal(err)
			}
		}
		spawn.live = 2
		// The moved conversation's record names a later pod; its old one
		// lingers after a failed delete.
		spawn.setSessionPods(sessionPod{PodName: "chat-vole-old2", SessionKey: older.Key, Reuse: true}, sessionPod{PodName: "chat-vole-gone", SessionKey: moved.Key, Reuse: true})
		say(r, "discord:g1/t-third2", "m1", "a third conversation")
		waitFor(t, "the third conversation's spawn", func() bool { return len(spawn.calls()) == 1 })
		if d := spawn.deleted(); len(d) != 1 || d[0] != "chat-vole-gone" {
			t.Fatalf("evicted %v, want the untracked pod", d)
		}
		if posts := r.adapter.postTextsFor(moved.Key); slices.Contains(posts, noticeSessionPaused) {
			t.Fatalf("a conversation whose pod was untracked was told it was paused: %v", posts)
		}
	})
	t.Run("a conversation whose lock is held is busy", func(t *testing.T) {
		r, spawn := startRigWithSpawnerCap(t, RouteSession, 1, reuseOn)
		ctx := context.Background()
		held := idle("discord:g1/t-held", "chat-vole-held", time.Hour)
		if err := r.g.reg.Put(ctx, held); err != nil {
			t.Fatal(err)
		}
		// A turn in flight there holds this lock while it decides; an
		// eviction must not wait on it, or decide around it.
		l := r.g.lockSession(held.Key)
		l.Lock()
		defer l.Unlock()
		spawn.live = 1
		spawn.setSessionPods(sessionPod{PodName: "chat-vole-held", SessionKey: held.Key, Reuse: true})
		say(r, "discord:g1/t-blocked", "m1", "anyone home?")
		waitFor(t, "the cap refusal", postedContaining(r, "not started"))
		if d := spawn.deleted(); len(d) != 0 {
			t.Fatalf("evicted %v from a conversation whose lock was held", d)
		}
	})
	t.Run("a busy pod is never evicted", func(t *testing.T) {
		r, spawn := startRigWithSpawnerCap(t, RouteSession, 1, reuseOn)
		ctx := context.Background()
		busy := idle("discord:g1/t-busy", "chat-vole-busy", time.Hour)
		busy.ActiveTask = &ActiveTask{TaskID: "task-busy", CorrelationID: "corr-busy", SubmittedAt: now}
		if err := r.g.reg.Put(ctx, busy); err != nil {
			t.Fatal(err)
		}
		spawn.live = 1
		spawn.setSessionPods(sessionPod{PodName: "chat-vole-busy", SessionKey: busy.Key, Reuse: true})
		say(r, "discord:g1/t-refused", "m1", "anyone home?")
		waitFor(t, "the cap refusal", postedContaining(r, "not started"))
		if d := spawn.deleted(); len(d) != 0 {
			t.Fatalf("evicted %v; a pod with a running task must stay", d)
		}
		if n := len(spawn.calls()); n != 0 {
			t.Fatalf("%d spawns past a full cap of busy pods", n)
		}
	})
	t.Run("a pod that turned busy after the candidate scan stays", func(t *testing.T) {
		// evictIdleSession read this conversation idle; a turn then took and
		// released its lock, leaving a task running. evictSession is what
		// sees the record now, under the lock, so it is called directly:
		// nothing between the two reads can be hooked.
		r, spawn := startRigWithSpawnerCap(t, RouteSession, 1, reuseOn)
		ctx := context.Background()
		turned := idle("discord:g1/t-turned", "chat-vole-turned", time.Hour)
		turned.ActiveTask = &ActiveTask{TaskID: "task-turned", CorrelationID: "corr-turned", SubmittedAt: now}
		if err := r.g.reg.Put(ctx, turned); err != nil {
			t.Fatal(err)
		}
		if r.g.evictSession(ctx, sessionPod{PodName: "chat-vole-turned", SessionKey: turned.Key, Reuse: true}) {
			t.Fatal("evictSession evicted a pod whose fresh record has a task running")
		}
		if d := spawn.deleted(); len(d) != 0 {
			t.Fatalf("evicted %v; a pod with a running task must stay", d)
		}
		if posts := r.adapter.postTextsFor(turned.Key); slices.Contains(posts, noticeSessionPaused) {
			t.Fatalf("a conversation with a running task was told it was paused: %v", posts)
		}
	})
}

// A reused pod carries the flag its adapter reads and the annotation the
// gateway checks before routing to it, the session's maximum lifetime as its
// deadline, and the delegate tool on whatever its first task is: the adapter
// turns it off per task, for a wake.
func TestAReusedPodIsSpawnedWithTheFlagTheLifetimeAndThePerTaskTool(t *testing.T) {
	for _, tc := range []struct {
		name       string
		reuse      bool
		fixedRoute bool
		lifetime   time.Duration
		deadline   time.Duration
		tool       string
	}{
		{name: "reuse on", reuse: true, lifetime: 4 * time.Hour, deadline: 4 * time.Hour, tool: "on"},
		{name: "reuse on, lifetime unset", reuse: true, deadline: defaultSessionMaxLifetime, tool: "on"},
		{name: "reuse off", deadline: 15*time.Minute + podDeadlineGrace, tool: "off"},
		{name: "a one-shot Delegate off the session route", reuse: true, fixedRoute: true, deadline: 15*time.Minute + podDeadlineGrace, tool: "off"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cs := k8sfake.NewSimpleClientset()
			cfg := &Config{Namespace: "test-ns", WorkerImage: "img", SessionServiceAccount: "agent-a2a-session",
				TaskDeadline: 15 * time.Minute, SessionReuse: tc.reuse, SessionMaxLifetime: tc.lifetime}
			s := &podSpawner{cfg: cfg, client: cs, log: slog.Default()}
			rec := &SessionRecord{Key: "discord:g1/t", ContextID: "ctx-1", BusSession: "chat-otter-wake", Addressee: "chat-otter-wake",
				SessionRouted: !tc.fixedRoute, Tasks: []TaskRef{{ID: "task-w", Role: taskRoleWake}}}
			if _, err := s.Spawn(context.Background(), rec, "task-w", "", 1); err != nil {
				t.Fatal(err)
			}
			pod, err := cs.CoreV1().Pods("test-ns").Get(context.Background(), "chat-otter-wake", metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if got := time.Duration(*pod.Spec.ActiveDeadlineSeconds) * time.Second; got != tc.deadline {
				t.Errorf("activeDeadlineSeconds = %v, want %v", got, tc.deadline)
			}
			env := map[string]string{}
			for _, e := range pod.Spec.Containers[0].Env {
				env[e.Name] = e.Value
			}
			if got := env[lib.EnvDelegateTool]; got != tc.tool {
				t.Errorf("%s = %q, want %q", lib.EnvDelegateTool, got, tc.tool)
			}
			want := tc.reuse && !tc.fixedRoute
			flag, anno := env[lib.EnvSessionReuse] == "true", pod.Annotations[annoReuse] == "true"
			if flag != want || anno != want {
				t.Errorf("reuse env=%v annotation=%v, want both %v", flag, anno, want)
			}
		})
	}
}

// Reusable vouches only for a pod that can run a whole task: spawned for
// reuse, running with its worker up, not being deleted, and with the task's
// bound left in its lifetime.
func TestPodReusable(t *testing.T) {
	now := time.Now()
	const deadline = 30 * time.Minute
	base := func() *corev1.Pod {
		return &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: "p", Annotations: map[string]string{annoReuse: reuseValue}},
			Spec:       corev1.PodSpec{ActiveDeadlineSeconds: ptr.To(int64((4 * time.Hour) / time.Second))},
			Status: corev1.PodStatus{
				Phase:     corev1.PodRunning,
				StartTime: &metav1.Time{Time: now.Add(-time.Hour)},
				ContainerStatuses: []corev1.ContainerStatus{{Name: workerContainer,
					State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}}}},
			},
		}
	}
	for _, tc := range []struct {
		name   string
		mutate func(*corev1.Pod)
		want   bool
	}{
		{"a live reused pod", func(*corev1.Pod) {}, true},
		{"no reuse annotation", func(p *corev1.Pod) { delete(p.Annotations, annoReuse) }, false},
		{"pending", func(p *corev1.Pod) { p.Status.Phase = corev1.PodPending }, false},
		{"succeeded", func(p *corev1.Pod) { p.Status.Phase = corev1.PodSucceeded }, false},
		{"being deleted", func(p *corev1.Pod) { p.DeletionTimestamp = &metav1.Time{Time: now} }, false},
		{"worker exited", func(p *corev1.Pod) {
			p.Status.ContainerStatuses[0].State = corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{}}
		}, false},
		{"no time left for a task", func(p *corev1.Pod) {
			p.Status.StartTime = &metav1.Time{Time: now.Add(-4*time.Hour + deadline + podDeadlineGrace)}
		}, false},
		{"just enough time left", func(p *corev1.Pod) {
			p.Status.StartTime = &metav1.Time{Time: now.Add(-4*time.Hour + deadline + 2*podDeadlineGrace + time.Minute)}
		}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := base()
			tc.mutate(p)
			if got := podReusable(p, deadline, now); got != tc.want {
				t.Errorf("podReusable = %v, want %v", got, tc.want)
			}
		})
	}
}

// SessionPods lists live session pods with their conversation, skipping the
// terminal and the ones already going.
func TestSessionPodsListsTheLiveOnes(t *testing.T) {
	mk := func(name string, phase corev1.PodPhase, deleting bool) *corev1.Pod {
		p := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "test-ns",
				Labels:      map[string]string{labelPartOf: partOfValue, labelRole: sessionRole},
				Annotations: map[string]string{annoConvo: "conv-" + name}},
			Status: corev1.PodStatus{Phase: phase},
		}
		if deleting {
			p.DeletionTimestamp = &metav1.Time{Time: time.Now()}
			p.Finalizers = []string{"test"}
		}
		return p
	}
	cs := k8sfake.NewSimpleClientset(
		mk("live", corev1.PodRunning, false),
		mk("pending", corev1.PodPending, false),
		mk("done", corev1.PodSucceeded, false),
		mk("going", corev1.PodRunning, true),
	)
	s := &podSpawner{cfg: &Config{Namespace: "test-ns"}, client: cs, log: slog.Default()}
	pods, err := s.SessionPods(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, p := range pods {
		got = append(got, fmt.Sprintf("%s=%s", p.PodName, p.SessionKey))
	}
	slices.Sort(got)
	if want := []string{"live=conv-live", "pending=conv-pending"}; !slices.Equal(got, want) {
		t.Fatalf("SessionPods = %v, want %v", got, want)
	}
	// The cap counts exactly the pods its eviction can choose from.
	n, err := s.LiveSessions(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if n != len(pods) {
		t.Fatalf("LiveSessions = %d over %d evictable pods; the count and the candidates disagree", n, len(pods))
	}
}

// At the cap with one pod already being deleted, its slot is the one that
// frees: the turn goes ahead and no other conversation's idle pod is evicted
// for it.
func TestTheCapDoesNotEvictForAPodAlreadyGoing(t *testing.T) {
	r, _ := startReuseRig(t)
	ctx := context.Background()
	mk := func(name, convo string, deleting bool) *corev1.Pod {
		p := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "test-ns",
				Labels:      map[string]string{labelPartOf: partOfValue, labelRole: sessionRole},
				Annotations: map[string]string{annoConvo: convo, annoReuse: reuseValue}},
			Status: corev1.PodStatus{Phase: corev1.PodRunning},
		}
		if deleting {
			p.DeletionTimestamp = &metav1.Time{Time: time.Now()}
			p.Finalizers = []string{"test"}
		}
		return p
	}
	idleKey := "discord:g1/t-cap-idle"
	idleRec := &SessionRecord{Key: idleKey, ContextID: "ctx-ci", Kind: "group", Profile: "chat",
		Addressee: "chat-vole-idle", BusSession: "chat-vole-idle", PodName: "chat-vole-idle", PodReuse: true,
		SessionRouted: true, LastUserMessage: time.Now().Add(-time.Hour).UTC()}
	if err := r.g.reg.Put(ctx, idleRec); err != nil {
		t.Fatal(err)
	}
	cs := k8sfake.NewSimpleClientset(
		mk("chat-vole-going", "discord:g1/t-cap-going", true),
		mk("chat-vole-idle", idleKey, false),
	)
	const capOfTwo = 2
	g := &Gateway{
		cfg:          &Config{MaxSessions: capOfTwo},
		spawner:      &podSpawner{cfg: &Config{Namespace: "test-ns"}, client: cs, log: slog.Default()},
		reg:          r.g.reg,
		adapter:      r.adapter,
		log:          slog.Default(),
		sessionLocks: map[string]*sessionLockEntry{},
	}
	requester := &SessionRecord{Key: "discord:g1/t-cap-new"}
	if g.refuseAtSessionCap(ctx, requester, false) {
		t.Fatal("refused at the cap though one of the two pods is already going")
	}
	if _, err := cs.CoreV1().Pods("test-ns").Get(ctx, "chat-vole-idle", metav1.GetOptions{}); err != nil {
		t.Fatalf("the idle pod was evicted for a slot already freeing: %v", err)
	}
	if fresh, _ := r.g.reg.Get(ctx, idleKey); fresh == nil || fresh.PodName != "chat-vole-idle" {
		t.Fatalf("the idle conversation lost its pod: %+v", fresh)
	}
}

// A reused pod that its conversation no longer names (a retirement whose
// delete failed) never exits on its own; the sweep deletes it. A pod its
// record still names, and a one-task pod, are left alone.
func TestTheSweepDeletesAnUntrackedReusedPod(t *testing.T) {
	r, spawn := startReuseRig(t)
	ctx := context.Background()
	tracked := &SessionRecord{Key: "discord:g1/t-tracked", ContextID: "ctx-t", Kind: "group",
		Addressee: "chat-otter-now", BusSession: "chat-otter-now", PodName: "chat-otter-now", PodReuse: true, SessionRouted: true}
	if err := r.g.reg.Put(ctx, tracked); err != nil {
		t.Fatal(err)
	}
	// A record whose last write lost PodName still names the pod as its
	// session, with a task running there: that pod is in use.
	lost := &SessionRecord{Key: "discord:g1/t-lost", ContextID: "ctx-l", Kind: "group",
		Addressee: "chat-otter-busy", BusSession: "chat-otter-busy", SessionRouted: true,
		ActiveTask: &ActiveTask{TaskID: "task-l", CorrelationID: "corr-l", SubmittedAt: time.Now()},
		Tasks:      []TaskRef{{ID: "task-l", Addressee: "chat-otter-busy"}}}
	// The same shape once that task has ended: nothing can route to the
	// pod again, so it is untracked.
	finished := &SessionRecord{Key: "discord:g1/t-finished", ContextID: "ctx-f", Kind: "group",
		Addressee: "chat-otter-done", BusSession: "chat-otter-done", SessionRouted: true,
		Tasks: []TaskRef{{ID: "task-f", Addressee: "chat-otter-done"}}}
	held := &SessionRecord{Key: "discord:g1/t-held-sweep", ContextID: "ctx-h", Kind: "group",
		Addressee: "chat-otter-new", BusSession: "chat-otter-new", PodName: "chat-otter-new", PodReuse: true, SessionRouted: true}
	for _, rec := range []*SessionRecord{lost, finished, held} {
		if err := r.g.reg.Put(ctx, rec); err != nil {
			t.Fatal(err)
		}
	}
	spawn.setSessionPods(
		sessionPod{PodName: "chat-otter-now", SessionKey: tracked.Key, Reuse: true},
		sessionPod{PodName: "chat-otter-was", SessionKey: tracked.Key, Reuse: true},
		sessionPod{PodName: "chat-otter-once", SessionKey: tracked.Key},
		sessionPod{PodName: "chat-otter-busy", SessionKey: lost.Key, Reuse: true},
		sessionPod{PodName: "chat-otter-done", SessionKey: finished.Key, Reuse: true},
		sessionPod{PodName: "chat-otter-old", SessionKey: held.Key, Reuse: true},
	)
	// A conversation whose lock is held is mid-turn; its pods wait for the
	// next pass.
	l := r.g.lockSession(held.Key)
	l.Lock()
	r.g.sweepOnce(ctx)
	if d := spawn.deleted(); len(d) != 2 || !slices.Contains(d, "chat-otter-was") || !slices.Contains(d, "chat-otter-done") {
		t.Fatalf("sweep deleted %v, want only the two untracked reused pods", d)
	}
	l.Unlock()
	r.g.sweepOnce(ctx)
	d := spawn.deleted()
	if !slices.Contains(d, "chat-otter-old") {
		t.Fatalf("sweep deleted %v, want the held conversation's untracked pod once the lock is free", d)
	}
	for _, kept := range []string{"chat-otter-now", "chat-otter-once", "chat-otter-busy"} {
		if slices.Contains(d, kept) {
			t.Fatalf("sweep deleted %s: %v", kept, d)
		}
	}
}

// A one-shot Delegate from a fixed-route conversation spawns a pod that serves
// that task only: the next plain ask goes back to the fixed addressee, and a
// pod waiting for a turn that never comes would idle.
func TestADelegateOffTheSessionRouteIsNotReused(t *testing.T) {
	r, spawn := startRigWithSpawnerCap(t, "platform", 0, reuseOn)
	conv := "discord:g1/t-delegate-once"
	say(r, conv, "m1", "Delegate: check the fleet")
	waitFor(t, "the delegate spawn", func() bool { return len(spawn.calls()) == 1 })
	waitFor(t, "the pod on the record", func() bool {
		rec, _ := r.g.reg.Get(context.Background(), conv)
		return rec != nil && rec.PodName != ""
	})
	if rec, _ := r.g.reg.Get(context.Background(), conv); rec.PodReuse {
		t.Fatal("a fixed-route conversation's Delegate pod was marked for reuse")
	}
}

// A pod that never started its task is not handed the next one: the heal
// unmarks it, and the next turn retires it and spawns fresh.
func TestANeverStartedTaskUnmarksItsPod(t *testing.T) {
	r, spawn := startRigWithSpawnerCap(t, RouteSession, 0, func(c *Config) {
		c.SessionReuse = true
		c.FirstEventGrace = time.Second
	})
	ctx := context.Background()
	pod := "chat-puffin-ns"
	rec := &SessionRecord{Key: "discord:g1/t-never", ContextID: "ctx-ns", Kind: "group",
		Addressee: pod, BusSession: pod, PodName: pod, PodReuse: true, SessionRouted: true, Profile: "chat",
		ActiveTask: &ActiveTask{TaskID: "task-ns", CorrelationID: "corr-ns", SubmittedAt: time.Now().Add(-time.Minute)},
		Tasks:      []TaskRef{{ID: "task-ns", Addressee: pod}}}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	spawn.setReusable(pod)
	say(r, rec.Key, "m1", "hello?")
	waitFor(t, "a fresh spawn", func() bool { return len(spawn.calls()) == 1 })
	if s := spawn.calls()[0].Session; s == pod {
		t.Fatalf("the next turn went to the pod that never started its task")
	}
	if d := spawn.deleted(); !slices.Contains(d, pod) {
		t.Fatalf("deleted %v, want the never-started pod retired", d)
	}
}

// The untracked pass lists pods for itself, so a failed orphan list does not
// hold it up.
func TestTheUntrackedSweepRunsWhenTheOrphanListFails(t *testing.T) {
	r, spawn := startReuseRig(t)
	ctx := context.Background()
	rec := &SessionRecord{Key: "discord:g1/t-orphan-err", ContextID: "ctx-oe", Kind: "group",
		Addressee: "chat-otter-cur", BusSession: "chat-otter-cur", PodName: "chat-otter-cur", PodReuse: true, SessionRouted: true}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	spawn.orphansErr = errors.New("api down")
	spawn.setSessionPods(sessionPod{PodName: "chat-otter-stale", SessionKey: rec.Key, Reuse: true})
	r.g.sweepOnce(ctx)
	if d := spawn.deleted(); len(d) != 1 || d[0] != "chat-otter-stale" {
		t.Fatalf("deleted %v, want the untracked pod despite the failed orphan list", d)
	}
}
