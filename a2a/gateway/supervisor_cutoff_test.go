package gateway

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The gate-2 pair (#5301): a profile named like a session token, and a
// session task addressed to a name that is a live profile's. Profile-ness is
// the record's (TaskRef.Tier), never the name's shape.
const (
	cutoffProfileAddressee = "cto-otter-1a2b" // session-shaped, a profile's name
	cutoffSessionAddressee = "fleet-audit"    // profile-shaped, with a live card, a session task's addressee
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
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)
	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("1001 test:bnaylor\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test"), lib.WithAgreementPolicy(SupervisorAgreement(nil)),
		lib.WithProfileSupervisorCutoff(cutoff))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	bus, err := lib.Connect(ctx, url, lib.WithName("executor-test"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(bus.Close)
	logs, text := &syncBuffer{}, &lockedBuffer{}
	obs := &recordingObserver{fakeAdapter: newFakeAdapter()}
	cfg := &Config{NATSURL: url, PrincipalMapPath: mapFile, DefaultAddressee: "platform", IdleTTL: 30 * time.Minute,
		AttributionSalt: []byte("test-salt"), FirstEventGrace: time.Hour}
	g, err := New(Options{Client: client, Adapter: obs, Config: cfg, Backend: "discord",
		Logger: slog.New(slog.NewJSONHandler(io.MultiWriter(logs, text), nil))})
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = g.Run(ctx) }()
	r := &cutoffRig{rig: &rig{g: g, adapter: obs.fakeAdapter, client: client, bus: bus, url: url, logs: text}, obs: obs, logs: logs, cutoff: cutoff}
	waitRunning(t, r.rig)
	// The session task's addressee is a live profile's name: a name-based
	// rule would take its task for a profile's.
	g.profiles.put(cutoffSessionAddressee, &lib.AgentCard{Name: cutoffSessionAddressee})
	return r
}

// cutoffRecord is a conversation holding one task as its active task:
// a profile task (Tier set) or a session task.
func cutoffRecord(conv, addressee, taskID string, profile bool) *SessionRecord {
	ref := TaskRef{ID: taskID, Addressee: addressee, CorrelationID: "corr-" + taskID, StartedAt: time.Now()}
	if profile {
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
// terminal (#5301, gate 2). The profile's name is session-shaped and the
// session's addressee is a live profile's, so only the record can tell them
// apart.
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
	if r.g.healBatchChild(ctx, rec, profKid) {
		t.Error("the profile child healed on a five-token write after the cutoff")
	}
	if !r.g.healBatchChild(ctx, rec, sessKid) {
		t.Error("the session child did not heal on its supervisor's terminal")
	}
	if got := r.terminalsFor(cutoffProfileTask); len(got) != 0 {
		t.Errorf("the profile child's five-token write was booked as its end: %+v", got)
	}
}

// taskIsProfile answers from the record, not the name: a TaskRef with a Tier
// is a profile task whatever its addressee looks like, one without is not
// even when its addressee has a live card, the card-stop control is never
// one, and only a task the record doesn't hold falls back to the directory.
func TestTaskIsProfileReadsTheRecordNotTheName(t *testing.T) {
	g := &Gateway{profiles: newProfileDirectory()}
	g.profiles.put(cutoffSessionAddressee, &lib.AgentCard{Name: cutoffSessionAddressee})
	rec := &SessionRecord{Tasks: []TaskRef{
		{ID: cutoffProfileTask, Addressee: cutoffProfileAddressee, Tier: "read"},
		{ID: cutoffSessionTask, Addressee: cutoffSessionAddressee},
	}, CardStop: &CardStopRun{TaskID: "task-card-stop", Addressee: cutoffSessionAddressee}}
	for _, c := range []struct {
		taskID, addressee string
		want              bool
	}{
		{cutoffProfileTask, cutoffProfileAddressee, true},
		{cutoffSessionTask, cutoffSessionAddressee, false},
		{"task-card-stop", cutoffSessionAddressee, false},
		{"task-unknown", cutoffSessionAddressee, true},
		{"task-unknown", cutoffProfileAddressee, false},
	} {
		if got := g.taskIsProfile(rec, c.taskID, c.addressee); got != c.want {
			t.Errorf("taskIsProfile(%s under %s) = %v, want %v", c.taskID, c.addressee, got, c.want)
		}
	}
}
