package gateway

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats.go/jetstream"
)

// The gate-2 pair (#5301): a profile named like a session token, and a
// session task addressed to a profile-shaped name with a live card. Profile-ness
// is the operator's profile names (lib.WithProfileNames), never the name's
// shape, the directory or anything the gateway writes: the rig's records say
// the opposite of the truth (cutoffRecord), and the reads follow the names.
const (
	cutoffProfileAddressee = "cto-otter-1a2b" // session-shaped, in the operator's profile names
	cutoffSessionAddressee = "fleet-audit"    // profile-shaped, with a live card, not in the names
	cutoffProfileTask      = "task-cut-profile"
	cutoffSessionTask      = "task-cut-session"
)

// cutoffRig is a running gateway whose client has the profile supervisor
// cutoff, observed, with its log captured as JSON.
type cutoffRig struct {
	*rig
	obs    *recordingObserver
	logs   *syncBuffer
	cutoff time.Time
}

func startCutoffRig(t *testing.T, cutoff time.Time) *cutoffRig {
	t.Helper()
	return startCutoffRigNames(t, cutoff, lib.NewProfileNames(cutoffProfileAddressee))
}

// startCutoffRigNames is startCutoffRig with the profile names given.
func startCutoffRigNames(t *testing.T, cutoff time.Time, names *lib.ProfileNames) *cutoffRig {
	t.Helper()
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)
	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("1001 test:bnaylor\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	client, err := connectClientTest(ctx, url, lib.WithName("gateway-test"), lib.WithAgreementPolicy(SupervisorAgreement(nil)),
		lib.WithProfileSupervisorCutoff(cutoff), lib.WithProfileNames(names))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	bus, err := connectClientTest(ctx, url, lib.WithName("executor-test"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(bus.Close)
	logs, text := &syncBuffer{}, &lockedBuffer{}
	obs := &recordingObserver{fakeAdapter: newFakeAdapter()}
	cfg := &Config{NATSURL: url, PrincipalMapPath: mapFile, DefaultAddressee: "platform", IdleTTL: 30 * time.Minute,
		AttributionSalt: []byte("test-salt"), FirstEventGrace: time.Hour, Namespace: "kubeagents-system"}
	g, err := New(Options{Client: client, Adapter: obs, Config: cfg, Backend: "discord",
		Logger: slog.New(slog.NewJSONHandler(io.MultiWriter(logs, text), nil))})
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = g.Run(ctx) }()
	r := &cutoffRig{rig: &rig{g: g, adapter: obs.fakeAdapter, client: client, bus: bus, url: url, logs: text}, obs: obs, logs: logs, cutoff: cutoff}
	waitRunning(t, r.rig)
	// The session task's addressee has a live card: a rule reading the
	// directory would take its task for a profile's.
	g.profiles.put(cutoffSessionAddressee, &lib.AgentCard{Name: cutoffSessionAddressee})
	return r
}

// cutoffRecord is a conversation holding one task as its active task. The
// record is the gateway's own write, so it is made to lie: the task under the
// profile's name has no Tier (the record says session), and one under any
// other name has one (the record says profile). A read that took
// profile-ness from the record would read each pair backwards.
func cutoffRecord(conv, addressee, taskID string, profile bool) *SessionRecord {
	ref := TaskRef{ID: taskID, Addressee: addressee, CorrelationID: "corr-" + taskID, StartedAt: time.Now()}
	if !profile {
		ref.Tier = "read"
	}
	return &SessionRecord{Key: conv, Addressee: addressee, ContextID: "ctx-" + taskID, LastActivity: time.Now(),
		Tasks:      []TaskRef{ref},
		ActiveTask: &ActiveTask{TaskID: taskID, CorrelationID: "corr-" + taskID, SubmittedAt: time.Now()}}
}

// supervisorTerminalEnvelope is the gateway's own failed terminal for taskID,
// what a five-token supervisor write looks like whoever made it.
func supervisorTerminalEnvelope(t *testing.T, taskID string) *lib.Envelope {
	t.Helper()
	payload, err := json.Marshal(lib.StatusUpdate{TaskID: taskID, ContextID: "ctx-" + taskID,
		Status: lib.TaskStatus{State: lib.StateFailed, Message: &lib.Message{Role: "agent", MessageID: "m-" + taskID,
			Parts: []lib.Part{{Kind: "text", Text: "reason: executor-lost"}}}}, Final: true})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(gatewayParty, taskID, "ctx-"+taskID, "corr-"+taskID, payload)
	if err != nil {
		t.Fatal(err)
	}
	return env
}

// stampedDelivery is fakeDelivery with the stream's stored time.
type stampedDelivery struct {
	fakeDelivery
	at time.Time
}

func (d *stampedDelivery) Received() time.Time { return d.at }

func (r *cutoffRig) terminalsFor(taskID string) []observed {
	r.obs.mu.Lock()
	defer r.obs.mu.Unlock()
	var out []observed
	for _, o := range r.obs.got {
		if o.kind == "terminal" && o.task == taskID {
			out = append(out, o)
		}
	}
	return out
}

func (r *cutoffRig) putRecord(t *testing.T, rec *SessionRecord) {
	t.Helper()
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	// The task index the relay routes by, as startTaskWith writes it.
	for _, ref := range rec.Tasks {
		if err := r.g.reg.IndexTask(context.Background(), ref.ID, rec.Key); err != nil {
			t.Fatal(err)
		}
	}
}

// On a latched gateway the relay settles a five-token supervisor event on a
// profile task stored after the cutoff without rendering it, counts it and
// logs it; the same event on a session task renders as the supervisor's
// terminal (#5301, gate 2). The profile's name is session-shaped, the
// session's addressee has a live card, and each record says the opposite of
// the truth: only the operator's profile names tell them apart.
func TestTheRelayDropsAFiveTokenTerminalOnAProfileTaskAfterTheCutoff(t *testing.T) {
	t.Parallel()
	r := startCutoffRig(t, time.Now().Add(-time.Minute))
	after := r.cutoff.Add(30 * time.Second)

	profConv, sessConv := "discord:cut/profile", "discord:cut/session"
	r.putRecord(t, cutoffRecord(profConv, cutoffProfileAddressee, cutoffProfileTask, true))
	r.putRecord(t, cutoffRecord(sessConv, cutoffSessionAddressee, cutoffSessionTask, false))

	profSubject := lib.TaskSupervisorSubject(cutoffProfileAddressee, cutoffProfileTask)
	profEnv := supervisorTerminalEnvelope(t, cutoffProfileTask)
	profD := &stampedDelivery{fakeDelivery: fakeDelivery{env: profEnv, subj: profSubject, seq: 501}, at: after}
	r.g.relayBatch(profConv, []relayItem{{env: profEnv, subject: profSubject, d: profD}})

	sessSubject := lib.TaskSupervisorSubject(cutoffSessionAddressee, cutoffSessionTask)
	sessEnv := supervisorTerminalEnvelope(t, cutoffSessionTask)
	sessD := &stampedDelivery{fakeDelivery: fakeDelivery{env: sessEnv, subj: sessSubject, seq: 502}, at: after}
	r.g.relayBatch(sessConv, []relayItem{{env: sessEnv, subject: sessSubject, d: sessD}})

	if profD.acks != 1 {
		t.Errorf("the dropped profile event was acked %d times, want settled once", profD.acks)
	}
	if got := r.terminalsFor(cutoffProfileTask); len(got) != 0 {
		t.Errorf("the profile task's five-token write was rendered as its terminal: %+v", got)
	}
	rec, err := r.g.reg.Get(context.Background(), profConv)
	if err != nil {
		t.Fatal(err)
	}
	if ref, _ := rec.TaskRefFor(cutoffProfileTask); slices.Contains(ref.RelayedSeqs, 501) {
		t.Error("the dropped event is on the record as rendered")
	}
	if rec.ActiveTask == nil || rec.ActiveTask.TaskID != cutoffProfileTask {
		t.Errorf("the dropped event released the profile task: active = %+v", rec.ActiveTask)
	}
	drops := logRecords(t, r.logs, "relay: five-token supervisor event on a profile task, stored after the cutoff, dropped")
	if len(drops) != 1 || drops[0]["taskId"] != cutoffProfileTask || drops[0]["level"] != "WARN" {
		t.Errorf("drop log = %+v, want one Warn naming the profile task", drops)
	}
	if r.client.ProfileSupervisorDrops() < 1 {
		t.Error("the drop was not counted")
	}

	waitFor(t, "the session task's terminal", func() bool { return len(r.terminalsFor(cutoffSessionTask)) == 1 })
	if got := r.terminalsFor(cutoffSessionTask)[0]; got.source != TerminalFromSupervisor || got.state != lib.StateFailed {
		t.Errorf("the session task's terminal = %+v, want the supervisor's failed terminal", got)
	}
}

// A five-token supervisor event stored before the cutoff is still a profile
// task's terminal on the relay: the dispatcher wrote it before it switched.
func TestTheRelayKeepsAFiveTokenTerminalStoredBeforeTheCutoff(t *testing.T) {
	t.Parallel()
	r := startCutoffRig(t, time.Now().Add(time.Hour))
	conv := "discord:cut/before"
	r.putRecord(t, cutoffRecord(conv, cutoffProfileAddressee, cutoffProfileTask, true))
	subject := lib.TaskSupervisorSubject(cutoffProfileAddressee, cutoffProfileTask)
	env := supervisorTerminalEnvelope(t, cutoffProfileTask)
	d := &stampedDelivery{fakeDelivery: fakeDelivery{env: env, subj: subject, seq: 601}, at: time.Now()}
	r.g.relayBatch(conv, []relayItem{{env: env, subject: subject, d: d}})
	waitFor(t, "the profile task's pre-cutoff terminal", func() bool { return len(r.terminalsFor(cutoffProfileTask)) == 1 })
	if r.client.ProfileSupervisorDrops() != 0 {
		t.Errorf("a pre-cutoff terminal was counted as dropped (%d)", r.client.ProfileSupervisorDrops())
	}
}

// The heal reads the task as the relay does: on a latched gateway, a
// five-token supervisor terminal stored after the cutoff doesn't heal a
// profile task, and does heal a session task (healActiveTask).
func TestTheHealIgnoresAFiveTokenTerminalOnAProfileTaskAfterTheCutoff(t *testing.T) {
	t.Parallel()
	r := startCutoffRig(t, time.Now().Add(-time.Minute))
	ctx := context.Background()
	for _, profile := range []bool{true, false} {
		addressee, taskID, conv := cutoffSessionAddressee, cutoffSessionTask, "discord:heal/session"
		if profile {
			addressee, taskID, conv = cutoffProfileAddressee, cutoffProfileTask, "discord:heal/profile"
		}
		if err := r.bus.Publish(ctx, lib.TaskSupervisorSubject(addressee, taskID), supervisorTerminalEnvelope(t, taskID)); err != nil {
			t.Fatal(err)
		}
		rec := cutoffRecord(conv, addressee, taskID, profile)
		r.putRecord(t, rec)
		r.g.healActiveTask(ctx, rec)
		got := r.terminalsFor(taskID)
		if profile {
			if len(got) != 0 || rec.ActiveTask == nil {
				t.Errorf("the profile task healed on a five-token write after the cutoff: terminals %+v, active %+v", got, rec.ActiveTask)
			}
			continue
		}
		if len(got) != 1 || got[0].source != TerminalFromSupervisor || rec.ActiveTask != nil {
			t.Errorf("the session task did not heal on its supervisor's terminal: terminals %+v, active %+v", got, rec.ActiveTask)
		}
	}
}

// The fan-out heal does the same for a batch child (healBatchChild).
func TestTheFanOutHealIgnoresAFiveTokenTerminalOnAProfileChildAfterTheCutoff(t *testing.T) {
	t.Parallel()
	r := startCutoffRig(t, time.Now().Add(-time.Minute))
	ctx := context.Background()
	parent := TaskRef{ID: "task-cut-parent", Addressee: "platform", CorrelationID: "corr-parent", BatchSize: 2, StartedAt: time.Now()}
	kid := func(id, addressee string, profile bool, index int) TaskRef {
		k := TaskRef{ID: id, Addressee: addressee, Role: taskRoleChild, ParentTaskID: parent.ID, BatchIndex: index,
			CorrelationID: "corr-" + id, StartedAt: time.Now()}
		if profile {
			k.Tier = "read"
		}
		return k
	}
	profKid, sessKid := kid(cutoffProfileTask, cutoffProfileAddressee, true, 0), kid(cutoffSessionTask, cutoffSessionAddressee, false, 1)
	parent.Children = []string{profKid.ID, sessKid.ID}
	rec := &SessionRecord{Key: "discord:heal/fanout", Addressee: "platform", ContextID: "ctx-fanout", LastActivity: time.Now(),
		Tasks:      []TaskRef{parent, profKid, sessKid},
		ActiveTask: &ActiveTask{TaskID: parent.ID, CorrelationID: parent.CorrelationID, SubmittedAt: time.Now()}}
	r.putRecord(t, rec)
	for _, k := range []TaskRef{profKid, sessKid} {
		if err := r.bus.Publish(ctx, lib.TaskSupervisorSubject(k.Addressee, k.ID), supervisorTerminalEnvelope(t, k.ID)); err != nil {
			t.Fatal(err)
		}
	}
	stream, err := r.g.tasksStream(ctx)
	if err != nil {
		t.Fatal(err)
	}
	// Both of the heal's reads (#6646): the probe, which drives it, and the
	// full replay the pass's spot check makes for one running child.
	probe := func(k TaskRef) taskProbe {
		p, err := r.g.probeTask(ctx, stream, k.Addressee, k.ID)
		if err != nil {
			t.Fatal(err)
		}
		return p
	}
	if p := probe(profKid); p.final {
		t.Errorf("the probe read the profile child's five-token write after the cutoff as its terminal: %+v", p)
	} else if r.g.healBatchChild(ctx, rec, profKid, p, false) {
		t.Error("the profile child healed on a five-token write after the cutoff")
	}
	if r.g.healBatchChild(ctx, rec, profKid, taskProbe{}, true) {
		t.Error("the spot check's replay healed the profile child on a five-token write after the cutoff")
	}
	if p := probe(sessKid); !p.final {
		t.Errorf("the probe missed the session child's supervisor terminal: %+v", p)
	} else if !r.g.healBatchChild(ctx, rec, sessKid, p, false) {
		t.Error("the session child did not heal on its supervisor's terminal")
	}
	if got := r.terminalsFor(cutoffProfileTask); len(got) != 0 {
		t.Errorf("the profile child's five-token write was booked as its end: %+v", got)
	}
}

// cutoffWorking publishes the executor's working status for taskID, so the
// task has a first event whatever its supervisor terminal does.
func (r *cutoffRig) cutoffWorking(t *testing.T, addressee, taskID string) {
	t.Helper()
	payload, err := json.Marshal(lib.StatusUpdate{TaskID: taskID, ContextID: "ctx-" + taskID, Status: lib.TaskStatus{State: lib.StateWorking}})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(lib.Party{Session: addressee, AgentType: "test-executor"}, taskID, "ctx-"+taskID, "corr-"+taskID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(context.Background(), lib.TaskEventsSubject(addressee, taskID), env); err != nil {
		t.Fatal(err)
	}
}

// Every other gateway read reads the profile/session pair as the relay and
// the heals do: under a cutoff, a five-token supervisor terminal stored after
// it is not a profile task's terminal and is a session task's. Each row's
// read would take the profile task's terminal if the gate were not applied
// or took the record's word, and the drop count is checked beside what the
// read shows. Records are written without the task index, so the relay never
// routes these events itself.
func TestEveryGatewayReadTakesTheCutoffForAProfileTask(t *testing.T) {
	r := startCutoffRig(t, time.Now().Add(-time.Minute))
	ctx := context.Background()
	schedule := lib.ScheduleRef{Namespace: "kubeagents-system", Name: "compliance-audit"}
	triggerQuery := func(taskID string) []byte {
		data, err := json.Marshal(lib.TriggerStatus{Schedule: schedule, TaskID: taskID})
		if err != nil {
			t.Fatal(err)
		}
		return data
	}
	triggerRecord := func(rec *SessionRecord, taskID string) *SessionRecord {
		rec.Key = triggerConversationKey(lib.TriggerSourceSchedule, schedule)
		rec.TriggerRuns = []TriggerRun{{Key: "k-" + taskID, Task: taskID, Published: true}}
		return rec
	}
	for _, c := range []struct {
		name string
		// noWorking leaves the task with its supervisor terminal alone.
		noWorking bool
		// shape adjusts the record before it is stored.
		shape func(rec *SessionRecord, taskID string) *SessionRecord
		// read calls the entry point and reports whether it took the
		// terminal, or "" when only the drop count tells.
		read func(rec *SessionRecord, taskID string) (took bool, observable bool)
	}{
		{name: "probeConversation", read: func(rec *SessionRecord, taskID string) (bool, bool) {
			st, err := r.g.probeConversation(ctx, rec.Key, taskID)
			if err != nil {
				t.Fatal(err)
			}
			return st.Final, true
		}},
		{name: "answerStatusByReplay", read: func(rec *SessionRecord, taskID string) (bool, bool) {
			r.g.answerStatusByReplay(ctx, rec)
			r.adapter.mu.Lock()
			defer r.adapter.mu.Unlock()
			for i := len(r.adapter.posts) - 1; i >= 0; i-- {
				if p := r.adapter.posts[i]; p.Conversation == rec.Key {
					return strings.Contains(p.Text, "**"+string(lib.StateFailed)+"**"), true
				}
			}
			t.Fatalf("%s: no status posted", rec.Key)
			return false, false
		}},
		{name: "healStoppedTasks", shape: func(rec *SessionRecord, taskID string) *SessionRecord {
			rec.Tasks[0].StopPending = true
			return rec
		}, read: func(rec *SessionRecord, taskID string) (bool, bool) {
			return r.g.healStoppedTasks(ctx, rec), true
		}},
		{name: "trigger status", shape: triggerRecord, read: func(rec *SessionRecord, taskID string) (bool, bool) {
			reply := r.g.handleTriggerStatus(ctx, lib.TriggerStatusSubject(lib.TriggerSourceSchedule), triggerQuery(taskID))
			if !reply.Found || reply.Refused != nil {
				t.Fatalf("%s: status reply %+v", taskID, reply)
			}
			return reply.Terminal, true
		}},
		{name: "trigger stop", shape: func(rec *SessionRecord, taskID string) *SessionRecord {
			// Canceled already: a task read as running answers stopped
			// and writes nothing; one read as ended answers already final.
			rec.Tasks[0].Canceled = true
			return triggerRecord(rec, taskID)
		}, read: func(rec *SessionRecord, taskID string) (bool, bool) {
			reply := r.g.handleTriggerStop(ctx, lib.TriggerStopSubject(lib.TriggerSourceSchedule), triggerQuery(taskID))
			if reply.Refused != nil {
				t.Fatalf("%s: stop refused %+v", taskID, reply.Refused)
			}
			return reply.Result == lib.TriggerStopAlreadyFinal, true
		}},
		{name: "busy readTask", read: func(rec *SessionRecord, taskID string) (bool, bool) {
			active := &ActiveTask{TaskID: taskID, SubmittedAt: time.Now().Add(-2 * r.g.cfg.FirstEventGrace)}
			return r.g.busyTaskLeftOut(ctx, &busyTasksStream{g: r.g}, active, rec.Addressee, time.Now()), true
		}},
		{name: "taskStreamEmpty", noWorking: true, read: func(rec *SessionRecord, taskID string) (bool, bool) {
			empty, err := r.g.taskStreamEmpty(ctx, rec.AddresseeFor(taskID), taskID)
			if err != nil {
				t.Fatal(err)
			}
			return !empty, true
		}},
		{name: "chainUsage", read: func(rec *SessionRecord, taskID string) (bool, bool) {
			r.g.chainUsage(ctx, rec, taskID, "", nil)
			return false, false
		}},
		{name: "rehydration primer", shape: func(rec *SessionRecord, taskID string) *SessionRecord {
			rec.Tasks[0].Requester = &TaskRequester{Backend: "discord", Subject: "s"}
			return rec
		}, read: func(rec *SessionRecord, taskID string) (bool, bool) {
			primer, _, _, _ := r.g.buildRehydrationPrimer(ctx, rec, "")
			return strings.Contains(primer, "executor-lost"), true
		}},
	} {
		for _, profile := range []bool{true, false} {
			addressee, kind := cutoffSessionAddressee, "session"
			if profile {
				addressee, kind = cutoffProfileAddressee, "profile"
			}
			taskID := "task-" + strings.ToLower(strings.ReplaceAll(c.name, " ", "-")) + "-" + kind
			if !c.noWorking {
				r.cutoffWorking(t, addressee, taskID)
			}
			if err := r.bus.Publish(ctx, lib.TaskSupervisorSubject(addressee, taskID), supervisorTerminalEnvelope(t, taskID)); err != nil {
				t.Fatal(err)
			}
			rec := cutoffRecord("discord:every/"+taskID, addressee, taskID, profile)
			if c.shape != nil {
				rec = c.shape(rec, taskID)
			}
			if err := r.g.reg.Put(ctx, rec); err != nil {
				t.Fatal(err)
			}
			before := r.client.ProfileSupervisorDrops()
			took, observable := c.read(rec, taskID)
			dropped := r.client.ProfileSupervisorDrops() - before
			if profile {
				if observable && took {
					t.Errorf("%s: the profile task's five-token terminal after the cutoff was taken", c.name)
				}
				if dropped == 0 {
					t.Errorf("%s: no drop counted for the profile task", c.name)
				}
				continue
			}
			if observable && !took {
				t.Errorf("%s: the session task's supervisor terminal was not taken", c.name)
			}
			if dropped != 0 {
				t.Errorf("%s: %d drops counted for the session task", c.name, dropped)
			}
		}
	}
}

// cutoffPair is the profile and session variants of one row: the addressee,
// a task id unique to the row, and whether the operator's names call it a
// profile.
func cutoffPair(row string) [2]struct {
	addressee, taskID string
	profile           bool
} {
	return [2]struct {
		addressee, taskID string
		profile           bool
	}{
		{cutoffProfileAddressee, "task-" + row + "-profile", true},
		{cutoffSessionAddressee, "task-" + row + "-session", false},
	}
}

// publishGatewayFiveToken is a five-token supervisor terminal on taskID, as
// the gateway's grant can write it on any addressee.
func (r *cutoffRig) publishGatewayFiveToken(t *testing.T, addressee, taskID string) {
	t.Helper()
	if err := r.bus.Publish(context.Background(), lib.TaskSupervisorSubject(addressee, taskID), supervisorTerminalEnvelope(t, taskID)); err != nil {
		t.Fatal(err)
	}
}

// The fan-out heal's read of the parent (healFanOut): a parent whose line is
// open is closed when its terminal is on the stream. A five-token terminal
// after the cutoff closes a session parent's line and not a profile parent's.
func TestTheFanOutParentReadTakesTheCutoff(t *testing.T) {
	t.Parallel()
	r := startCutoffRig(t, time.Now().Add(-time.Minute))
	ctx := context.Background()
	for _, v := range cutoffPair("fanout-parent") {
		r.cutoffWorking(t, v.addressee, v.taskID)
		r.publishGatewayFiveToken(t, v.addressee, v.taskID)
		rec := cutoffRecord("discord:fanout-parent/"+v.taskID, v.addressee, v.taskID, v.profile)
		rec.Tasks[0].BatchSize = 1
		rec.ActiveTask.StatusMsgID = "line-" + v.taskID
		r.putRecord(t, rec)
		before := r.client.ProfileSupervisorDrops()
		r.g.healFanOut(ctx, rec, rec.ActiveTask, rec.Tasks[0])
		closed := rec.ActiveTask.StatusMsgID == ""
		dropped := r.client.ProfileSupervisorDrops() - before
		if v.profile && (closed || dropped == 0) {
			t.Errorf("profile parent: line closed %v, drops %d; want the five-token terminal refused", closed, dropped)
		}
		if !v.profile && (!closed || dropped != 0) {
			t.Errorf("session parent: line closed %v, drops %d; want its supervisor's terminal read", closed, dropped)
		}
	}
}

// The fan-out status's read of each running child (answerFanOutStatus): a
// session child with a five-token terminal after the cutoff reads failed; a
// profile child reads as its executor left it, working.
func TestTheFanOutStatusReadTakesTheCutoff(t *testing.T) {
	t.Parallel()
	r := startCutoffRig(t, time.Now().Add(-time.Minute))
	ctx := context.Background()
	parent := TaskRef{ID: "task-status-parent", Addressee: "platform", CorrelationID: "corr-status-parent", BatchSize: 2, StartedAt: time.Now()}
	rec := &SessionRecord{Key: "discord:fanout/status", Addressee: "platform", ContextID: "ctx-status", LastActivity: time.Now(),
		ActiveTask: &ActiveTask{TaskID: parent.ID, CorrelationID: parent.CorrelationID, SubmittedAt: time.Now()}}
	pair := cutoffPair("fanout-status")
	for i, v := range pair {
		r.cutoffWorking(t, v.addressee, v.taskID)
		r.publishGatewayFiveToken(t, v.addressee, v.taskID)
		kid := TaskRef{ID: v.taskID, Addressee: v.addressee, Role: taskRoleChild, ParentTaskID: parent.ID, BatchIndex: i + 1,
			CorrelationID: "corr-" + v.taskID, StartedAt: time.Now()}
		if !v.profile {
			kid.Tier = "read" // the record lies, as cutoffRecord's do
		}
		parent.Children = append(parent.Children, kid.ID)
		rec.Tasks = append(rec.Tasks, kid)
	}
	rec.Tasks = append([]TaskRef{parent}, rec.Tasks...)
	r.putRecord(t, rec)
	r.g.answerFanOutStatus(ctx, rec, parent)
	var status string
	r.adapter.mu.Lock()
	for _, p := range r.adapter.posts {
		if p.Conversation == rec.Key {
			status = p.Text
		}
	}
	r.adapter.mu.Unlock()
	for _, v := range pair {
		want := string(lib.StateFailed)
		if v.profile {
			want = string(lib.StateWorking)
		}
		if line := fmt.Sprintf(noticeFanOutStatusLine, v.addressee, want); !strings.Contains(status, line) {
			t.Errorf("status %q: want the line %q", status, line)
		}
	}
	if r.client.ProfileSupervisorDrops() == 0 {
		t.Error("no drop counted for the profile child")
	}
}

// The relay's terminal replay (relayTerminal): a completed terminal whose
// result the render state lost reads the result back from the stream. Ahead
// of the result on the stream is a five-token failed terminal stored after
// the cutoff. On a profile task the replay leaves it out and reads the
// result; on a session task it is the task's end, and the result behind it
// is the post-final drop.
func TestTheRelaysTerminalReplayTakesTheCutoff(t *testing.T) {
	t.Parallel()
	r := startCutoffRig(t, time.Now().Add(-time.Minute))
	ctx := context.Background()
	for _, v := range cutoffPair("relay-replay") {
		r.cutoffWorking(t, v.addressee, v.taskID)
		r.publishGatewayFiveToken(t, v.addressee, v.taskID)
		from := lib.Party{Session: v.addressee, AgentType: "test-executor"}
		art, err := json.Marshal(lib.ArtifactUpdate{TaskID: v.taskID, ContextID: "ctx-" + v.taskID,
			Artifact:  lib.Artifact{ArtifactID: "a-" + v.taskID, Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "the answer"}}},
			LastChunk: true})
		if err != nil {
			t.Fatal(err)
		}
		env, err := lib.NewArtifactUpdateEnvelope(from, v.taskID, "ctx-"+v.taskID, "corr-"+v.taskID, art)
		if err != nil {
			t.Fatal(err)
		}
		if err := r.bus.Publish(ctx, lib.TaskEventsSubject(v.addressee, v.taskID), env); err != nil {
			t.Fatal(err)
		}
		rec := cutoffRecord("discord:relay-replay/"+v.taskID, v.addressee, v.taskID, v.profile)
		r.putRecord(t, rec)
		rs := &relayState{state: lib.StateWorking}
		r.g.mu.Lock()
		r.g.relays[v.taskID] = rs
		r.g.mu.Unlock()
		replays := 0
		r.g.terminalReplayHook = func(string) error { replays++; return nil }
		status := lib.StatusUpdate{TaskID: v.taskID, ContextID: "ctx-" + v.taskID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}
		before := r.client.ProfileSupervisorDrops()
		r.g.relayTerminal(ctx, rec, rs, v.taskID, status, TerminalFromExecutor)
		r.g.terminalReplayHook = nil
		if replays != 1 {
			t.Fatalf("%s: the terminal replay ran %d times, want once", v.taskID, replays)
		}
		delivered := ""
		for _, o := range r.obs.events() {
			if o.kind == "delivered" && o.task == v.taskID {
				delivered = o.text
			}
		}
		dropped := r.client.ProfileSupervisorDrops() - before
		if v.profile && (delivered != "the answer" || dropped == 0) {
			t.Errorf("profile task: delivered %q, drops %d; want the result read past the refused terminal", delivered, dropped)
		}
		if !v.profile && (delivered == "the answer" || dropped != 0) {
			t.Errorf("session task: delivered %q, drops %d; want its supervisor's terminal to end the fold first", delivered, dropped)
		}
	}
}

// The gate a gateway runs with shows on its metrics listener, not only in a
// log line: the cutoff as a timestamp by its source, whether the profile
// names are loaded and when they were last read, and the drops. A malformed
// value shows as the malformed source at the unix epoch; an unset one as 0
// under unset. Names never loaded show a load time of 0.
func TestTheProfileSupervisorGateShowsOnTheMetricsListener(t *testing.T) {
	for _, c := range []struct {
		value, source, cutoff string
		names                 *lib.ProfileNames
		loaded                string
	}{
		{"", lib.CutoffSourceUnset, "0", nil, "0"},
		{"2026-11-23T12:10:00Z", lib.CutoffSourceEnv, unixText(time.Date(2026, 11, 23, 12, 10, 0, 0, time.UTC)), lib.NewProfileNames("fleet-audit"), "1"},
		{"not a time", lib.CutoffSourceMalformed, "0", &lib.ProfileNames{}, "0"},
	} {
		setting := lib.ProfileSupervisorCutoffFromEnv(func(string) string { return c.value }, slog.New(slog.NewTextHandler(io.Discard, nil)))
		m := NewMetrics()
		m.SetProfileSupervisorGate(lib.ProfileSupervisorGate{ProfileSupervisorCutoffSetting: setting, Names: c.names}, func() int64 { return 7 })
		_, body := scrape(t, http.MethodGet, startMetricsServer(t, m)+MetricsPath)
		for _, want := range []string{
			fmt.Sprintf(`kubeagents_a2a_gateway_profile_supervisor_cutoff_timestamp_seconds{source=%q} %s`, c.source, c.cutoff),
			"kubeagents_a2a_gateway_profile_names_loaded " + c.loaded,
			"kubeagents_a2a_gateway_profile_names_last_load_timestamp_seconds " + loadedAtText(c.names),
			"kubeagents_a2a_gateway_profile_supervisor_drops_total 7",
		} {
			if !strings.Contains(body, want) {
				t.Errorf("%q: metrics lack %q:\n%s", c.value, want, body)
			}
		}
	}
}

// loadedAtText is the names' last load time as the metrics listener writes
// it, 0 when never loaded.
func loadedAtText(names *lib.ProfileNames) string {
	if at := names.LoadedAt(); !at.IsZero() {
		return unixText(at)
	}
	return "0"
}

// unixText is at in seconds as the metrics listener writes a gauge.
func unixText(at time.Time) string {
	return strconv.FormatFloat(float64(at.UnixNano())/float64(time.Second), 'g', -1, 64)
}

// allowDirect turns on allow_direct on the rig's TASKS stream, as the
// provision script creates it, so the gateway's reads are DIRECT.GET.
func (r *cutoffRig) allowDirect(t *testing.T) {
	t.Helper()
	nc, err := connectTest(r.url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	st, err := js.Stream(ctx, lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	cfg := st.CachedInfo().Config
	cfg.AllowDirect = true
	if _, err := js.UpdateStream(ctx, cfg); err != nil {
		t.Fatal(err)
	}
}

// publishBlankLineForgery publishes data on subject over the raw protocol
// with a header block a blank line ends early: the headers a direct get
// needs and a Nats-Time-Stamp of the publisher's own set to at, then the
// blank line. The server appends its own headers after the block's last
// line, where the client's header parse never reaches. It returns once the
// stream holds it.
func publishBlankLineForgery(t *testing.T, url, subject string, at time.Time, data []byte) {
	t.Helper()
	hdr := "NATS/1.0\r\nNats-Stream: " + lib.TasksStream + "\r\nNats-Subject: " + subject + "\r\nNats-Sequence: 1\r\n" +
		"Nats-Time-Stamp: " + at.UTC().Format(time.RFC3339Nano) + "\r\n\r\nX-Pad: 1\r\n\r\n"
	conn, err := net.DialTimeout("tcp", strings.TrimPrefix(url, "nats://"), 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	rd := bufio.NewReader(conn)
	if line, err := rd.ReadString('\n'); err != nil || !strings.HasPrefix(line, "INFO ") {
		t.Fatalf("server greeting %q: %v", line, err)
	}
	msg := fmt.Sprintf("CONNECT {\"verbose\":false,\"pedantic\":false,\"headers\":true,\"protocol\":1}\r\nHPUB %s %d %d\r\n%s%s\r\nPING\r\n",
		subject, len(hdr), len(hdr)+len(data), hdr, data)
	if _, err := conn.Write([]byte(msg)); err != nil {
		t.Fatal(err)
	}
	for {
		line, err := rd.ReadString('\n')
		if err != nil {
			t.Fatalf("waiting for PONG: %v", err)
		}
		if strings.HasPrefix(line, "-ERR") {
			t.Fatalf("server refused the publish: %s", line)
		}
		if strings.HasPrefix(line, "PONG") {
			break
		}
	}
	nc, err := connectTest(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the forgery stored", func() bool {
		st, err := js.Stream(context.Background(), lib.TasksStream)
		if err != nil {
			return false
		}
		info, err := st.Info(context.Background(), jetstream.WithSubjectFilter(subject))
		return err == nil && info.State.Subjects[subject] > 0
	})
}

// H1 on #6885: on a stream with allow_direct, a five-token terminal on a
// profile task, published after the cutoff with a blank line inside its own
// header block and a stamp before the cutoff ahead of it, is not the task's
// terminal on the busy count and is not a first event for the no-first-event
// notice. The direct get shows the client one stamp, the forged one; the
// reads take the server's time from a consumer instead.
func TestABlankLineForgeryPassesNeitherTheBusyCountNorTheEmptyRead(t *testing.T) {
	r := startCutoffRig(t, time.Now().Add(-time.Minute))
	r.allowDirect(t)
	ctx := context.Background()
	forged := r.cutoff.Add(-time.Hour)
	env := func(taskID string) []byte {
		data, err := json.Marshal(supervisorTerminalEnvelope(t, taskID))
		if err != nil {
			t.Fatal(err)
		}
		return data
	}

	// The busy count: a running task with a forged terminal is still running.
	const running = "task-blank-running"
	r.cutoffWorking(t, cutoffProfileAddressee, running)
	publishBlankLineForgery(t, r.url, lib.TaskSupervisorSubject(cutoffProfileAddressee, running), forged, env(running))
	empty, final, err := (&busyTasksStream{g: r.g}).readTask(ctx, cutoffProfileAddressee, running)
	if err != nil || empty || final {
		t.Errorf("busy readTask: empty %v, final %v, %v; want running", empty, final, err)
	}
	active := &ActiveTask{TaskID: running, SubmittedAt: time.Now().Add(-2 * r.g.cfg.FirstEventGrace)}
	if r.g.busyTaskLeftOut(ctx, &busyTasksStream{g: r.g}, active, cutoffProfileAddressee, time.Now()) {
		t.Error("the busy count left the running task out on the forged terminal")
	}
	// The fan-out's probe (probeTask): the same direct gets, the same answer.
	stream, err := r.g.tasksStream(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if p, err := r.g.probeTask(ctx, stream, cutoffProfileAddressee, running); err != nil || p.final || p.empty {
		t.Errorf("probeTask: %+v, %v; want running", p, err)
	}

	// The empty read: a forged terminal alone is not a first event.
	const silent = "task-blank-silent"
	publishBlankLineForgery(t, r.url, lib.TaskSupervisorSubject(cutoffProfileAddressee, silent), forged, env(silent))
	if empty, err := r.g.taskStreamEmpty(ctx, cutoffProfileAddressee, silent); err != nil || !empty {
		t.Errorf("taskStreamEmpty: %v, %v; want empty", empty, err)
	}
	if empty, err := r.g.client.TaskReplayEmpty(ctx, nil, cutoffProfileAddressee, silent); err != nil || !empty {
		t.Errorf("TaskReplayEmpty: %v, %v; want empty", empty, err)
	}
	if heads, err := r.g.client.TaskReplayHeads(ctx, nil, cutoffProfileAddressee, running); err != nil || len(heads) != 1 {
		t.Errorf("TaskReplayHeads: %d heads, %v; want the working event alone", len(heads), err)
	}
	if r.client.ProfileSupervisorDrops() < 3 {
		t.Errorf("drops = %d, want one per read", r.client.ProfileSupervisorDrops())
	}
}

// With allow_direct, a five-token terminal the dispatcher wrote before the
// cutoff still ends a profile task on the busy count and is still a first
// event: the direct get gives no time, and the consumer read gives the
// server's, before the cutoff.
func TestAPreCutoffTerminalStillEndsAProfileTaskOnTheDirectReads(t *testing.T) {
	r := startCutoffRig(t, time.Now().Add(time.Hour))
	r.allowDirect(t)
	ctx := context.Background()
	const taskID = "task-pre-cutoff"
	r.cutoffWorking(t, cutoffProfileAddressee, taskID)
	r.publishGatewayFiveToken(t, cutoffProfileAddressee, taskID)
	empty, final, err := (&busyTasksStream{g: r.g}).readTask(ctx, cutoffProfileAddressee, taskID)
	if err != nil || empty || !final {
		t.Errorf("busy readTask: empty %v, final %v, %v; want final", empty, final, err)
	}
	stream, err := r.g.tasksStream(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if p, err := r.g.probeTask(ctx, stream, cutoffProfileAddressee, taskID); err != nil || !p.final {
		t.Errorf("probeTask: %+v, %v; want final", p, err)
	}
	const alone = "task-pre-cutoff-alone"
	r.publishGatewayFiveToken(t, cutoffProfileAddressee, alone)
	if empty, err := r.g.taskStreamEmpty(ctx, cutoffProfileAddressee, alone); err != nil || empty {
		t.Errorf("taskStreamEmpty: %v, %v; want the terminal found", empty, err)
	}
	if r.client.ProfileSupervisorDrops() != 0 {
		t.Errorf("a pre-cutoff terminal counted %d drops", r.client.ProfileSupervisorDrops())
	}
}

// nakDelivery is stampedDelivery counting give-backs.
type nakDelivery struct {
	stampedDelivery
	naks  int
	delay time.Duration
}

func (d *nakDelivery) Nak(delay time.Duration) error { d.naks++; d.delay = delay; return nil }

// While the profile names are not loaded every addressee reads as a profile,
// so the relay's refusal may be a session task's terminal the names would
// take. It is given back to the stream, not acked, and not rendered; once
// the names are loaded the same refusal is settled.
func TestTheRelayHoldsARefusalMadeBeforeTheNamesAreLoaded(t *testing.T) {
	t.Parallel()
	r := startCutoffRigNames(t, time.Now().Add(-time.Minute), &lib.ProfileNames{})
	after := r.cutoff.Add(30 * time.Second)
	conv := "discord:cut/unloaded"
	r.putRecord(t, cutoffRecord(conv, cutoffSessionAddressee, cutoffSessionTask, false))
	subject := lib.TaskSupervisorSubject(cutoffSessionAddressee, cutoffSessionTask)
	env := supervisorTerminalEnvelope(t, cutoffSessionTask)
	d := &nakDelivery{stampedDelivery: stampedDelivery{fakeDelivery: fakeDelivery{env: env, subj: subject, seq: 701}, at: after}}
	r.g.relayBatch(conv, []relayItem{{env: env, subject: subject, d: d}})
	if d.acks != 0 || d.naks != 1 || d.delay != profileNamesHoldDelay {
		t.Errorf("acks %d, naks %d after %v; want given back once after %v", d.acks, d.naks, d.delay, profileNamesHoldDelay)
	}
	if got := r.terminalsFor(cutoffSessionTask); len(got) != 0 {
		t.Errorf("a held event was rendered: %+v", got)
	}
	if held := logRecords(t, r.logs, "relay: five-token supervisor event held; the profile names are not loaded, so every task reads as a profile's"); len(held) != 1 {
		t.Errorf("hold log = %+v, want one line", held)
	}

	loaded := startCutoffRig(t, time.Now().Add(-time.Minute))
	profConv := "discord:cut/loaded"
	loaded.putRecord(t, cutoffRecord(profConv, cutoffProfileAddressee, cutoffProfileTask, true))
	profSubject := lib.TaskSupervisorSubject(cutoffProfileAddressee, cutoffProfileTask)
	profEnv := supervisorTerminalEnvelope(t, cutoffProfileTask)
	pd := &nakDelivery{stampedDelivery: stampedDelivery{fakeDelivery: fakeDelivery{env: profEnv, subj: profSubject, seq: 702}, at: loaded.cutoff.Add(30 * time.Second)}}
	loaded.g.relayBatch(profConv, []relayItem{{env: profEnv, subject: profSubject, d: pd}})
	if pd.acks != 1 || pd.naks != 0 {
		t.Errorf("with the names loaded: acks %d, naks %d; want settled once", pd.acks, pd.naks)
	}
}

// redeliveredDelivery is nakDelivery on its nth delivery.
type redeliveredDelivery struct {
	nakDelivery
	n uint64
}

func (d *redeliveredDelivery) NumDelivered() uint64 { return d.n }

// The relay durable has no MaxDeliver, so with a cutoff set and no names
// file a held refusal would come back every profileNamesHoldDelay for good,
// counting a drop each time. It is held profileNamesHoldLimit times, then
// settled with an Error, and the drop is counted once, at the settle.
func TestTheRelayStopsHoldingARefusalAfterTheLimit(t *testing.T) {
	t.Parallel()
	r := startCutoffRigNames(t, time.Now().Add(-time.Minute), &lib.ProfileNames{})
	conv := "discord:cut/never-loaded"
	r.putRecord(t, cutoffRecord(conv, cutoffSessionAddressee, cutoffSessionTask, false))
	subject := lib.TaskSupervisorSubject(cutoffSessionAddressee, cutoffSessionTask)
	env := supervisorTerminalEnvelope(t, cutoffSessionTask)
	deliver := func(n uint64) *redeliveredDelivery {
		d := &redeliveredDelivery{nakDelivery: nakDelivery{stampedDelivery: stampedDelivery{
			fakeDelivery: fakeDelivery{env: env, subj: subject, seq: 703}, at: r.cutoff.Add(30 * time.Second)}}, n: n}
		r.g.relayBatch(conv, []relayItem{{env: env, subject: subject, d: d}})
		return d
	}
	for n := uint64(1); n <= profileNamesHoldLimit; n++ {
		if d := deliver(n); d.acks != 0 || d.naks != 1 {
			t.Fatalf("delivery %d: acks %d, naks %d; want given back", n, d.acks, d.naks)
		}
	}
	if drops := r.client.ProfileSupervisorDrops(); drops != 0 {
		t.Errorf("%d holds counted %d drops, want none until the settle", profileNamesHoldLimit, drops)
	}
	if d := deliver(profileNamesHoldLimit + 1); d.acks != 1 || d.naks != 0 {
		t.Errorf("delivery %d: acks %d, naks %d; want settled", profileNamesHoldLimit+1, d.acks, d.naks)
	}
	if drops := r.client.ProfileSupervisorDrops(); drops != 1 {
		t.Errorf("drops = %d after the settle, want 1", drops)
	}
	dropped := logRecords(t, r.logs, "relay: five-token supervisor event dropped; the profile names never loaded while it was held")
	if len(dropped) != 1 || dropped[0]["level"] != "ERROR" {
		t.Errorf("drop log = %+v, want one Error line", dropped)
	}
	if got := r.terminalsFor(cutoffSessionTask); len(got) != 0 {
		t.Errorf("a refused event was rendered: %+v", got)
	}
}

// The hold budget counts holds for the names and nothing else. A delivery
// the durable already handed out many times for other reasons (a failed KV
// write's give-back, a stop's, an AckWait that lapsed) is still held, and
// the event is settled only after profileNamesHoldLimit holds of its own.
func TestKVRequeuesDoNotSpendTheRelaysNamesHolds(t *testing.T) {
	t.Parallel()
	r := startCutoffRigNames(t, time.Now().Add(-time.Minute), &lib.ProfileNames{})
	conv := "discord:cut/kv-requeues"
	r.putRecord(t, cutoffRecord(conv, cutoffSessionAddressee, cutoffSessionTask, false))
	subject := lib.TaskSupervisorSubject(cutoffSessionAddressee, cutoffSessionTask)
	env := supervisorTerminalEnvelope(t, cutoffSessionTask)
	deliver := func(n uint64) *redeliveredDelivery {
		d := &redeliveredDelivery{nakDelivery: nakDelivery{stampedDelivery: stampedDelivery{
			fakeDelivery: fakeDelivery{env: env, subj: subject, seq: 704}, at: r.cutoff.Add(30 * time.Second)}}, n: n}
		r.g.relayBatch(conv, []relayItem{{env: env, subject: subject, d: d}})
		return d
	}
	// Forty earlier deliveries given back for other reasons, then three more
	// between every hold: NumDelivered is past the limit from the start.
	n := uint64(41)
	for hold := 1; hold <= profileNamesHoldLimit; hold++ {
		if d := deliver(n); d.acks != 0 || d.naks != 1 {
			t.Fatalf("hold %d (delivery %d): acks %d, naks %d; want given back", hold, n, d.acks, d.naks)
		}
		n += 3
	}
	if d := deliver(n); d.acks != 1 || d.naks != 0 {
		t.Errorf("after %d holds (delivery %d): acks %d, naks %d; want settled", profileNamesHoldLimit, n, d.acks, d.naks)
	}
	if drops := r.client.ProfileSupervisorDrops(); drops != 1 {
		t.Errorf("drops = %d after the settle, want 1", drops)
	}
}

// A session task's five-token terminal held while the names are unloaded
// renders once the names load and it comes back: exactly once, however many
// times it is delivered after that.
func TestAHeldSessionTerminalRendersOnceTheNamesLoad(t *testing.T) {
	t.Parallel()
	names := &lib.ProfileNames{}
	r := startCutoffRigNames(t, time.Now().Add(-time.Minute), names)
	conv := "discord:cut/held-then-loaded"
	r.putRecord(t, cutoffRecord(conv, cutoffSessionAddressee, cutoffSessionTask, false))
	subject := lib.TaskSupervisorSubject(cutoffSessionAddressee, cutoffSessionTask)
	env := supervisorTerminalEnvelope(t, cutoffSessionTask)
	deliver := func(n uint64) *redeliveredDelivery {
		d := &redeliveredDelivery{nakDelivery: nakDelivery{stampedDelivery: stampedDelivery{
			fakeDelivery: fakeDelivery{env: env, subj: subject, seq: 705}, at: r.cutoff.Add(30 * time.Second)}}, n: n}
		r.g.relayBatch(conv, []relayItem{{env: env, subject: subject, d: d}})
		return d
	}

	if d := deliver(1); d.acks != 0 || d.naks != 1 {
		t.Fatalf("names unloaded: acks %d, naks %d; want held", d.acks, d.naks)
	}
	if got := r.terminalsFor(cutoffSessionTask); len(got) != 0 {
		t.Fatalf("a held event was rendered: %+v", got)
	}

	path := filepath.Join(t.TempDir(), "profile-names.json")
	if err := os.WriteFile(path, []byte(`{"profiles":["`+cutoffProfileAddressee+`"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := names.LoadFile(path); err != nil || !r.client.ProfileNamesLoaded() {
		t.Fatalf("loading the names: %v, loaded %v", err, r.client.ProfileNamesLoaded())
	}

	if d := deliver(2); d.acks != 1 || d.naks != 0 {
		t.Errorf("names loaded: acks %d, naks %d; want rendered and settled", d.acks, d.naks)
	}
	waitFor(t, "the held terminal rendered", func() bool { return len(r.terminalsFor(cutoffSessionTask)) >= 1 })
	// The same message once more, as an ack the server lost would bring it.
	if d := deliver(3); d.naks != 0 {
		t.Errorf("third delivery: naks %d; want no hold", d.naks)
	}
	time.Sleep(200 * time.Millisecond)
	got := r.terminalsFor(cutoffSessionTask)
	if len(got) != 1 {
		t.Fatalf("the held terminal rendered %d times, want once: %+v", len(got), got)
	}
	if got[0].source != TerminalFromSupervisor || got[0].state != lib.StateFailed {
		t.Errorf("rendered %+v, want the supervisor's failed terminal", got[0])
	}
	if drops := r.client.ProfileSupervisorDrops(); drops != 0 {
		t.Errorf("a session task's terminal counted %d drops", drops)
	}
}
