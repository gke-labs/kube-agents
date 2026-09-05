package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// eventsEnvelopes replays everything on an addressee's events subjects —
// the supervisor tests read the stream, not the gateway's memory, because
// the property under test is what replay sees. Error-returning (no
// *testing.T) so the fake spawner's onDelete hook can call it from the
// gateway's own goroutine.
func eventsEnvelopes(url, addressee string) ([]*lib.Envelope, error) {
	nc, err := nats.Connect(url)
	if err != nil {
		return nil, err
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cons, err := js.OrderedConsumer(ctx, lib.TasksStream, jetstream.OrderedConsumerConfig{
		FilterSubjects: []string{fmt.Sprintf("a2a.tasks.%s.*.events", addressee)},
	})
	if err != nil {
		return nil, err
	}
	var out []*lib.Envelope
	it, err := cons.Messages()
	if err != nil {
		return nil, err
	}
	defer it.Stop()
	for {
		it2, cancel2 := context.WithTimeout(ctx, 300*time.Millisecond)
		msg, err := fetchNext(it2, it)
		cancel2()
		if err != nil {
			break
		}
		env, err := lib.ParseEnvelope(msg.Data())
		if err == nil {
			out = append(out, env)
		}
	}
	return out, nil
}

// finalsFor returns every final status-update for a task on an addressee's
// events — the payload spec allows exactly one, so a slice longer than that
// is itself a finding.
func finalsFor(url, addressee, taskID string) ([]lib.StatusUpdate, error) {
	envs, err := eventsEnvelopes(url, addressee)
	if err != nil {
		return nil, err
	}
	var out []lib.StatusUpdate
	for _, env := range envs {
		if env.Kind != lib.KindStatusUpdate || env.TaskID != taskID {
			continue
		}
		var s lib.StatusUpdate
		if json.Unmarshal(env.Payload, &s) != nil {
			continue
		}
		if s.Final {
			out = append(out, s)
		}
	}
	return out, nil
}

// hasTerminal is finalsFor as a hook-safe boolean: errors read as "not
// there", which can only fail an assertion, never pass one.
func hasTerminal(url, addressee, taskID string) bool {
	finals, err := finalsFor(url, addressee, taskID)
	return err == nil && len(finals) > 0
}

// terminalFor returns the final status-update for a task on an addressee's
// events, or nil.
func terminalFor(t *testing.T, url, addressee, taskID string) *lib.StatusUpdate {
	t.Helper()
	finals, err := finalsFor(url, addressee, taskID)
	if err != nil {
		t.Fatal(err)
	}
	if len(finals) == 0 {
		return nil
	}
	return &finals[0]
}

// TestDelegateOverDetachedPublishesCanceledBeforeDelete: the one rule for
// every pod the gateway deletes itself, on the Delegate path. A delegated
// task is stopped (detached — the cancel is on the stream, the terminal
// never arrives), then a second Delegate retires the incarnation: the
// gateway is that task's supervisor, so its terminal must be on the stream
// — state `canceled`, because the supervisor is finishing the requester's
// cancel, not reporting an error (assertion 13's enumeration).
func TestDelegateOverDetachedPublishesCanceledBeforeDelete(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-sup1"

	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "s-100", Text: "Delegate: first task",
	}
	waitFor(t, "first delegate spawn", func() bool { return len(spawn.calls()) == 1 })
	first := spawn.calls()[0]

	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "s-101", Text: "stop",
	}
	waitFor(t, "cancel acknowledged", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "cancel sent") {
				return true
			}
		}
		return false
	})

	// The ordering is the rule's whole content: at the moment of deletion
	// the terminal must already be on the stream — "terminal exists and
	// pod deleted" would also pass a delete-then-publish regression.
	atDelete := make(chan bool, 4)
	spawn.onDelete = func(string) { atDelete <- hasTerminal(r.url, first.Session, first.TaskID) }

	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "s-102", Text: "Delegate: second task",
	}
	waitFor(t, "second delegate spawn", func() bool { return len(spawn.calls()) == 2 })

	if deleted := spawn.deleted(); len(deleted) != 1 || deleted[0] != first.Session {
		t.Fatalf("previous incarnation not retired: %v", deleted)
	}
	if !<-atDelete {
		t.Fatal("pod deleted before its task's terminal was on the stream")
	}
	s := terminalFor(t, r.url, first.Session, first.TaskID)
	if s == nil {
		t.Fatalf("detached task %s has no terminal on the stream after its pod was deleted", first.TaskID)
	}
	if s.Status.State != lib.StateCanceled {
		t.Fatalf("supervisor terminal state = %s, want canceled (finishing the requester's cancel)", s.Status.State)
	}
}

// TestReapDetachedPublishesCanceledBeforeDelete: a detached task does not
// exempt its session from the idle TTL, so reap may delete a pod whose
// harness is still working — the supervisor rule is what keeps that from
// being a silent stop.
func TestReapDetachedPublishesCanceledBeforeDelete(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-sup2"
	rec := &SessionRecord{
		Key: conv, ContextID: "ctx-sup2", Kind: "group",
		Addressee: "chat-heron-r1", BusSession: "chat-heron-r1", PodName: "chat-heron-r1",
		SessionRouted: true, Profile: "chat",
		LastActivity: time.Now().UTC().Add(-2 * time.Hour),
		ActiveTask:   &ActiveTask{TaskID: "task-sup2", CorrelationID: "corr-sup2", Detached: true},
		Tasks:        []TaskRef{{ID: "task-sup2", Addressee: "chat-heron-r1", Canceled: true}},
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	atDelete := make(chan bool, 4)
	spawn.onDelete = func(string) { atDelete <- hasTerminal(r.url, "chat-heron-r1", "task-sup2") }

	r.g.reapOnce(context.Background())

	if deleted := spawn.deleted(); len(deleted) != 1 || deleted[0] != "chat-heron-r1" {
		t.Fatalf("idle detached pod not reaped: %v", deleted)
	}
	if !<-atDelete {
		t.Fatal("reap deleted the pod before its task's terminal was on the stream")
	}
	s := terminalFor(t, r.url, "chat-heron-r1", "task-sup2")
	if s == nil {
		t.Fatal("reaped detached task has no terminal on the stream")
	}
	if s.Status.State != lib.StateCanceled {
		t.Fatalf("reap terminal state = %s, want canceled", s.Status.State)
	}
	fresh, err := r.g.reg.Get(context.Background(), conv)
	if err != nil || fresh == nil || fresh.PodName != "" {
		t.Fatalf("record not released after reap: %+v (err=%v)", fresh, err)
	}
}

// TestReapIdlePodWithoutTaskPublishesNothing: reap of a plainly idle
// incarnation owes the stream nothing — only a detached task has a
// supervisor debt.
func TestReapIdlePodWithoutTaskPublishesNothing(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-sup3"
	rec := &SessionRecord{
		Key: conv, ContextID: "ctx-sup3", Kind: "group",
		Addressee: "chat-vole-r2", BusSession: "chat-vole-r2", PodName: "chat-vole-r2",
		SessionRouted: true, Profile: "chat",
		LastActivity: time.Now().UTC().Add(-2 * time.Hour),
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}

	r.g.reapOnce(context.Background())

	if deleted := spawn.deleted(); len(deleted) != 1 || deleted[0] != "chat-vole-r2" {
		t.Fatalf("idle pod not reaped: %v", deleted)
	}
	if envs, err := eventsEnvelopes(r.url, "chat-vole-r2"); err != nil || len(envs) != 0 {
		t.Fatalf("idle reap published %d events (err=%v); want none", len(envs), err)
	}
}

// TestSweepStateFollowsTheSupervisorRule: Sweep reaches detached tasks
// routinely (a worker that exits or wedges after a stop), and an
// unconditional `failed` would report broken for every task a user stopped.
// The durable evidence is the task history's Canceled mark — ActiveTask may
// long since have moved on.
func TestSweepStateFollowsTheSupervisorRule(t *testing.T) {
	r, spawn := startRigWithSpawner(t)

	// Two orphaned pods in terminal phase: one whose task the requester
	// stopped (cancel on the stream), one whose executor just died.
	stopped := &SessionRecord{
		Key: "discord:g1/thread-sw-c", ContextID: "ctx-sw-c", Kind: "group",
		Addressee: "chat-lynx-c", BusSession: "chat-lynx-c", PodName: "chat-lynx-c",
		Tasks: []TaskRef{{ID: "task-sw-c", Addressee: "chat-lynx-c", Canceled: true}},
	}
	died := &SessionRecord{
		Key: "discord:g1/thread-sw-f", ContextID: "ctx-sw-f", Kind: "group",
		Addressee: "chat-stoat-f", BusSession: "chat-stoat-f", PodName: "chat-stoat-f",
		Tasks: []TaskRef{{ID: "task-sw-f", Addressee: "chat-stoat-f"}},
	}
	for _, rec := range []*SessionRecord{stopped, died} {
		if err := r.g.reg.Put(context.Background(), rec); err != nil {
			t.Fatal(err)
		}
	}
	spawn.setOrphans([]orphanPod{
		{PodName: "chat-lynx-c", SessionKey: stopped.Key, Addressee: "chat-lynx-c",
			TaskID: "task-sw-c", ContextID: "ctx-sw-c", CorrelationID: "corr-sw-c"},
		{PodName: "chat-stoat-f", SessionKey: died.Key, Addressee: "chat-stoat-f",
			TaskID: "task-sw-f", ContextID: "ctx-sw-f", CorrelationID: "corr-sw-f"},
	})

	r.g.sweepOnce(context.Background())

	c := terminalFor(t, r.url, "chat-lynx-c", "task-sw-c")
	if c == nil || c.Status.State != lib.StateCanceled {
		t.Fatalf("stopped task's sweep terminal = %+v, want canceled", c)
	}
	f := terminalFor(t, r.url, "chat-stoat-f", "task-sw-f")
	if f == nil || f.Status.State != lib.StateFailed {
		t.Fatalf("dead executor's sweep terminal = %+v, want failed", f)
	}
	deleted := spawn.deleted()
	if len(deleted) != 2 {
		t.Fatalf("sweep deleted %v, want both orphans", deleted)
	}
}

// TestStopMarksTaskHistoryCanceled: the cancel is recorded on the task's
// history entry, not just ActiveTask — the mark Sweep reads after
// ActiveTask has moved on. Set only after the publish succeeded, so a true
// mark means the cancel is on the stream.
func TestStopMarksTaskHistoryCanceled(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-sup4"
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "s-140", Text: "check the fleet",
	}
	waitFor(t, "task starts", func() bool {
		rec, err := r.g.reg.Get(context.Background(), conv)
		return err == nil && rec != nil && rec.ActiveTask != nil
	})
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "s-141", Text: "stop",
	}
	waitFor(t, "cancel recorded on the history entry", func() bool {
		rec, err := r.g.reg.Get(context.Background(), conv)
		if err != nil || rec == nil || rec.ActiveTask == nil || !rec.ActiveTask.Detached {
			return false
		}
		return rec.TaskCanceled(rec.ActiveTask.TaskID)
	})
}

// TestMintAdoptsTheRaceWinner: contextId minting is create-only (the
// spec's MUST) — a mint that loses the first-contact race reads and adopts
// the winner's identity instead of forking the conversation.
func TestMintAdoptsTheRaceWinner(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-mint"
	winner := &SessionRecord{
		Key: conv, ContextID: "ctx-winner", Addressee: "platform", Kind: "group",
	}
	if err := r.g.reg.Create(context.Background(), winner); err != nil {
		t.Fatal(err)
	}
	if err := r.g.reg.Create(context.Background(), winner); !errors.Is(err, ErrSessionExists) {
		t.Fatalf("second Create = %v, want ErrSessionExists", err)
	}

	rec, err := r.g.mintSession(context.Background(), InboundMessage{Conversation: conv, Kind: "group"})
	if err != nil {
		t.Fatal(err)
	}
	if rec.ContextID != "ctx-winner" {
		t.Fatalf("loser minted its own identity: %q, want the winner's ctx-winner", rec.ContextID)
	}
}

// TestNoSecondFinalWhenWorkerAlreadyConfirmed: Detached means the terminal
// has not been RELAYED, not that it does not exist. A worker that confirmed
// the cancel before the relay clears the flag already put the one final on
// the stream; the retirement path must find it there (sweep's guard) and
// publish nothing, or the gateway itself authors the protocol error
// assertion 10 makes every consumer surface.
func TestNoSecondFinalWhenWorkerAlreadyConfirmed(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-sup5"
	// A detached task whose executor's own final canceled is already on
	// the stream, with the relay's clear not yet applied to the record —
	// the relay-lag shape.
	rec := &SessionRecord{
		Key: conv, ContextID: "ctx-sup5", Kind: "group",
		Addressee: "chat-marten-x1", BusSession: "chat-marten-x1", PodName: "chat-marten-x1",
		SessionRouted: true, Profile: "chat",
		LastActivity: time.Now().UTC(),
		ActiveTask:   &ActiveTask{TaskID: "task-sup5", CorrelationID: "corr-sup5", Detached: true},
		Tasks:        []TaskRef{{ID: "task-sup5", Addressee: "chat-marten-x1", Canceled: true}},
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID: "task-sup5", ContextID: "ctx-sup5",
		Status: lib.TaskStatus{State: lib.StateCanceled}, Final: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(lib.Party{Session: "chat-marten-x1", AgentType: "test-worker"},
		"task-sup5", "ctx-sup5", "corr-sup5", payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(context.Background(), lib.TaskEventsSubject("chat-marten-x1", "task-sup5"), env); err != nil {
		t.Fatal(err)
	}

	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "s-150", Text: "Delegate: next thing",
	}
	waitFor(t, "successor spawn", func() bool { return len(spawn.calls()) == 1 })
	if deleted := spawn.deleted(); len(deleted) != 1 || deleted[0] != "chat-marten-x1" {
		t.Fatalf("previous incarnation not retired: %v", deleted)
	}
	finals, err := finalsFor(r.url, "chat-marten-x1", "task-sup5")
	if err != nil {
		t.Fatal(err)
	}
	if len(finals) != 1 {
		t.Fatalf("stream carries %d finals for the task, want exactly the worker's own", len(finals))
	}
}

// TestSessionRoutePlainAskRetiresDeadIncarnation: the worker adapter is one
// task per process, so on the session route a lingering PodName names an
// executor that can never serve a NEW task — a plain ask must retire it the
// way Delegate does (terminal for a detached task first) and mint the
// successor, not publish toward the dead addressee and wedge the
// conversation (S9 review finding 4).
func TestSessionRoutePlainAskRetiresDeadIncarnation(t *testing.T) {
	r, spawn := startRigWithSpawnerRoute(t, RouteSession)
	conv := "discord:g1/thread-sup6"
	rec := &SessionRecord{
		Key: conv, ContextID: "ctx-sup6", Kind: "group",
		Addressee: "chat-puffin-d1", BusSession: "chat-puffin-d1", PodName: "chat-puffin-d1",
		SessionRouted: true, Profile: "chat",
		LastActivity: time.Now().UTC(),
		ActiveTask:   &ActiveTask{TaskID: "task-sup6", CorrelationID: "corr-sup6", Detached: true},
		Tasks:        []TaskRef{{ID: "task-sup6", Addressee: "chat-puffin-d1", Canceled: true}},
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}

	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "s-160", Text: "hello again",
	}
	waitFor(t, "fresh incarnation for the plain ask", func() bool { return len(spawn.calls()) == 1 })
	call := spawn.calls()[0]
	if call.Session == "chat-puffin-d1" {
		t.Fatalf("plain ask reused the dead incarnation %q", call.Session)
	}
	if deleted := spawn.deleted(); len(deleted) != 1 || deleted[0] != "chat-puffin-d1" {
		t.Fatalf("dead incarnation not retired: %v", deleted)
	}
	s := terminalFor(t, r.url, "chat-puffin-d1", "task-sup6")
	if s == nil || s.Status.State != lib.StateCanceled {
		t.Fatalf("detached task's terminal = %+v, want canceled before the retirement", s)
	}
	// And the completed-pod shape: ActiveTask already cleared, pod still
	// bound. Nothing is owed to the stream, but the successor must not
	// publish toward the dead addressee.
	conv2 := "discord:g1/thread-sup7"
	rec2 := &SessionRecord{
		Key: conv2, ContextID: "ctx-sup7", Kind: "group",
		Addressee: "chat-vole-d2", BusSession: "chat-vole-d2", PodName: "chat-vole-d2",
		SessionRouted: true, Profile: "chat",
		LastActivity: time.Now().UTC(),
	}
	if err := r.g.reg.Put(context.Background(), rec2); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- InboundMessage{
		Conversation: conv2, Kind: "group",
		AuthorID: "1001", MessageID: "s-161", Text: "one more",
	}
	waitFor(t, "fresh incarnation after a completed pod", func() bool { return len(spawn.calls()) == 2 })
	if call := spawn.calls()[1]; call.Session == "chat-vole-d2" {
		t.Fatalf("plain ask reused the completed incarnation %q", call.Session)
	}
	if envs, err := eventsEnvelopes(r.url, "chat-vole-d2"); err != nil || len(envs) != 0 {
		t.Fatalf("retiring an idle pod published %d events (err=%v); want none", len(envs), err)
	}
}

// TestAskBoundClearsOnlyTheCopy: the independent bound on the KV ask copy —
// an ask past AskTTL is cleared by the reap scan while the task record, its
// serialization, and its detach state stay untouched; a younger ask stays.
func TestAskBoundClearsOnlyTheCopy(t *testing.T) {
	r := startRig(t)
	old := &SessionRecord{
		Key: "discord:g1/thread-ask-old", ContextID: "ctx-ask-old", Kind: "group",
		Addressee:    "platform",
		LastActivity: time.Now().UTC(),
		ActiveTask: &ActiveTask{TaskID: "task-ask-old", CorrelationID: "corr-a",
			Ask: "the secret ask", SubmittedAt: time.Now().Add(-25 * time.Hour), Detached: true},
	}
	young := &SessionRecord{
		Key: "discord:g1/thread-ask-new", ContextID: "ctx-ask-new", Kind: "group",
		Addressee:    "platform",
		LastActivity: time.Now().UTC(),
		ActiveTask: &ActiveTask{TaskID: "task-ask-new", CorrelationID: "corr-b",
			Ask: "the fresh ask", SubmittedAt: time.Now().Add(-time.Hour)},
	}
	for _, rec := range []*SessionRecord{old, young} {
		if err := r.g.reg.Put(context.Background(), rec); err != nil {
			t.Fatal(err)
		}
	}

	r.g.reapOnce(context.Background())

	got, err := r.g.reg.Get(context.Background(), old.Key)
	if err != nil || got == nil || got.ActiveTask == nil {
		t.Fatalf("task record must survive the ask bound: %+v (err=%v)", got, err)
	}
	if got.ActiveTask.Ask != "" {
		t.Fatalf("expired ask survived: %q", got.ActiveTask.Ask)
	}
	if got.ActiveTask.TaskID != "task-ask-old" || !got.ActiveTask.Detached {
		t.Fatalf("ask bound touched more than the copy: %+v", got.ActiveTask)
	}
	fresh, err := r.g.reg.Get(context.Background(), young.Key)
	if err != nil || fresh == nil || fresh.ActiveTask == nil || fresh.ActiveTask.Ask != "the fresh ask" {
		t.Fatalf("young ask must stay: %+v (err=%v)", fresh, err)
	}
}
