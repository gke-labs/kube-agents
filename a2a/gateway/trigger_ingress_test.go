package gateway

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

func goodScheduleSubmit() lib.TriggerSubmit {
	return lib.TriggerSubmit{
		Trigger:        lib.TriggerClaim{Name: "schedule/compliance-audit", Origin: lib.TriggerOriginBuiltin, Stream: "compliance-audit"},
		IdempotencyKey: "schedule/kubeagents-system/compliance-audit/1763100000",
		Schedule:       lib.ScheduleRef{Namespace: "kubeagents-system", Name: "compliance-audit"},
		Profile:        "fleet-audit",
		Prompt:         "Run the compliance audit.",
		Skills:         []string{"fleet-audit"},
		Delivery:       lib.ScheduleDelivery{Mode: lib.ScheduleDeliveryChat, SilentWhenClean: true, JobKey: "compliance-audit"},
	}
}

// triggerRig is a gateway whose directory knows fleet-audit at read and
// unattended and readonly at read only, a raw connection playing the
// operator, and a core subscription catching every profile submission.
type triggerRig struct {
	*rig
	nc *nats.Conn

	mu   sync.Mutex
	subs []*nats.Msg
}

func startTriggerRig(t *testing.T, tweak func(*Config)) *triggerRig {
	t.Helper()
	r := startRigWith(t, tweak)
	r.g.profiles.put("fleet-audit", &lib.AgentCard{Name: "fleet-audit", Tiers: []string{"read", "unattended"}})
	r.g.profiles.put("readonly", &lib.AgentCard{Name: "readonly", Tiers: []string{"read"}})
	nc, err := connectTest(r.url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	tr := &triggerRig{rig: r, nc: nc}
	if _, err := nc.Subscribe(lib.SubmitSubjectPrefix+">", func(m *nats.Msg) {
		tr.mu.Lock()
		tr.subs = append(tr.subs, m)
		tr.mu.Unlock()
	}); err != nil {
		t.Fatal(err)
	}
	// The ingress subscribes as Run starts; wait until it answers.
	waitFor(t, "the trigger ingress", func() bool {
		_, err := nc.Request(lib.TriggerSubmitSubject("schedule"), []byte("{"), time.Second)
		return err == nil
	})
	return tr
}

func (tr *triggerRig) submit(t *testing.T, source string, body any) lib.TriggerSubmitReply {
	t.Helper()
	data, ok := body.([]byte)
	if !ok {
		var err error
		if data, err = json.Marshal(body); err != nil {
			t.Fatal(err)
		}
	}
	msg, err := tr.nc.Request(lib.TriggerSubmitSubject(source), data, 5*time.Second)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	var reply lib.TriggerSubmitReply
	if err := json.Unmarshal(msg.Data, &reply); err != nil {
		t.Fatalf("reply %q: %v", msg.Data, err)
	}
	return reply
}

func (tr *triggerRig) submissions() []*nats.Msg {
	tr.mu.Lock()
	defer tr.mu.Unlock()
	return append([]*nats.Msg(nil), tr.subs...)
}

// TestATriggerSubmissionStartsATaskWithTheTriggerBound: the elected gateway
// answers a schedule's submission with a task on the profile's unattended
// intake, whose grant binds the trigger, whose authority block claims the
// same trigger (read as the dispatcher reads it), and whose run and
// delivery are recorded on the trigger's own conversation.
func TestATriggerSubmissionStartsATaskWithTheTriggerBound(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil || reply.TaskID == "" {
		t.Fatalf("reply = %+v", reply)
	}
	if reply.TaskID != triggerTaskID(s.IdempotencyKey) {
		t.Errorf("task %s is not the key's", reply.TaskID)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	m := tr.submissions()[0]
	if want := lib.TaskSubmitSubject("fleet-audit", "unattended", lib.SubmitKindTop, reply.TaskID); m.Subject != want {
		t.Fatalf("published on %s, want %s", m.Subject, want)
	}
	var env lib.Envelope
	if err := json.Unmarshal(m.Data, &env); err != nil {
		t.Fatal(err)
	}
	claimed, err := capability.TriggerFromAuthority(env.Authority)
	if err != nil || claimed == nil {
		t.Fatalf("authority trigger = %+v, %v", claimed, err)
	}
	want := capability.Trigger{Name: s.Trigger.Name, Origin: "builtin", Stream: "compliance-audit"}
	if *claimed != want {
		t.Errorf("claimed trigger = %+v, want %+v", *claimed, want)
	}
	if !strings.Contains(string(env.Payload), "Use these skills: fleet-audit.") {
		t.Errorf("the task's text does not name its skills: %s", env.Payload)
	}

	// The grant the envelope pins binds the same trigger, at the unattended
	// tier on the profile, for the dispatcher.
	ref, present, err := capability.RefFromAuthority(env.Authority)
	if err != nil || !present {
		t.Fatalf("authority grant: %+v, %v, %v", ref, present, err)
	}
	entry := readCapEntry(t, tr.url, ref)
	if entry.Tier != capability.TierUnattended || entry.Profile != "fleet-audit" || entry.Delegate != profileGrantDelegate {
		t.Errorf("grant = %+v", entry)
	}
	if entry.Trigger == nil || *entry.Trigger != want {
		t.Errorf("grant trigger = %+v, want %+v", entry.Trigger, want)
	}

	rec, err := tr.g.reg.Get(context.Background(), "trigger:schedule/kubeagents-system/compliance-audit")
	if err != nil || rec == nil {
		t.Fatalf("record: %+v, %v", rec, err)
	}
	if rec.Delivery == nil || *rec.Delivery != s.Delivery {
		t.Errorf("recorded delivery = %+v", rec.Delivery)
	}
	if rec.TriggerKey != s.IdempotencyKey || rec.TriggerTask != reply.TaskID {
		t.Errorf("recorded run = %q %q", rec.TriggerKey, rec.TriggerTask)
	}
}

// TestARepeatedTriggerSubmissionIsTheSameRun: the same idempotency key
// answers with the same task and starts nothing, and spends no rate budget;
// a new key is a new run.
func TestARepeatedTriggerSubmissionIsTheSameRun(t *testing.T) {
	tr := startTriggerRig(t, func(c *Config) { c.TriggerRatePerHour = 2 })
	s := goodScheduleSubmit()
	first := tr.submit(t, "schedule", s)
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	for range 3 {
		again := tr.submit(t, "schedule", s)
		if again.TaskID != first.TaskID || again.Refused != nil {
			t.Fatalf("repeat = %+v, want task %s", again, first.TaskID)
		}
	}
	time.Sleep(200 * time.Millisecond)
	if n := len(tr.submissions()); n != 1 {
		t.Fatalf("%d submissions published, want 1", n)
	}
	next := s
	next.IdempotencyKey = "schedule/kubeagents-system/compliance-audit/1763100300"
	second := tr.submit(t, "schedule", next)
	if second.Refused != nil || second.TaskID == first.TaskID {
		t.Fatalf("a new slot = %+v", second)
	}
}

// TestATriggerOverItsRateCapIsRefused: the third run inside the hour on a
// cap of two is refused rate_limited.
func TestATriggerOverItsRateCapIsRefused(t *testing.T) {
	tr := startTriggerRig(t, func(c *Config) { c.TriggerRatePerHour = 2 })
	s := goodScheduleSubmit()
	for i, slot := range []string{"1", "2", "3"} {
		s.IdempotencyKey = "schedule/kubeagents-system/compliance-audit/" + slot
		reply := tr.submit(t, "schedule", s)
		if i < 2 && reply.Refused != nil {
			t.Fatalf("run %d refused: %+v", i, reply.Refused)
		}
		if i == 2 && (reply.Refused == nil || reply.Refused.Reason != lib.TriggerRefusedRateLimited) {
			t.Fatalf("run %d = %+v, want rate_limited", i, reply)
		}
	}
}

// TestATriggerSubmissionIsRefusedWithAReason: each refusal starts nothing.
func TestATriggerSubmissionIsRefusedWithAReason(t *testing.T) {
	tr := startTriggerRig(t, nil)
	for _, tc := range []struct {
		name, source string
		body         any
		reason       string
	}{
		{"an unknown profile", "schedule", func() lib.TriggerSubmit { s := goodScheduleSubmit(); s.Profile = "nope"; return s }(), lib.TriggerRefusedProfileUnknown},
		{"a profile without unattended", "schedule", func() lib.TriggerSubmit { s := goodScheduleSubmit(); s.Profile = "readonly"; return s }(), lib.TriggerRefusedTierNotAllowed},
		{"another source", "event-watcher", goodScheduleSubmit(), lib.TriggerRefusedInvalid},
		{"malformed JSON", "schedule", []byte("{"), lib.TriggerRefusedInvalid},
		{"an unknown field", "schedule", []byte(`{"trigger":{"name":"schedule/x","origin":"builtin","stream":"x"},"extra":1}`), lib.TriggerRefusedInvalid},
		{"a user origin", "schedule", func() lib.TriggerSubmit { s := goodScheduleSubmit(); s.Trigger.Origin = "user"; return s }(), lib.TriggerRefusedInvalid},
		{"a bad delivery", "schedule", func() lib.TriggerSubmit { s := goodScheduleSubmit(); s.Delivery.JobKey = "has.dot"; return s }(), lib.TriggerRefusedInvalid},
	} {
		t.Run(tc.name, func(t *testing.T) {
			reply := tr.submit(t, tc.source, tc.body)
			if reply.Refused == nil || reply.Refused.Reason != tc.reason || reply.TaskID != "" {
				t.Fatalf("reply = %+v, want %s", reply, tc.reason)
			}
		})
	}
	time.Sleep(200 * time.Millisecond)
	if n := len(tr.submissions()); n != 0 {
		t.Fatalf("%d submissions published by refusals", n)
	}
}

func TestTheTriggerRateLimiterRefillsOverTheHour(t *testing.T) {
	l := newTriggerRateLimiter(2)
	t0 := time.Unix(1_763_100_000, 0)
	if !l.allow("a", t0) || !l.allow("a", t0) || l.allow("a", t0) {
		t.Fatal("a fresh bucket of 2 did not admit exactly 2")
	}
	if !l.allow("b", t0) {
		t.Fatal("another trigger shares the bucket")
	}
	if !l.allow("a", t0.Add(31*time.Minute)) {
		t.Fatal("half an hour did not refill one token of 2/h")
	}
	if l.allow("a", t0.Add(32*time.Minute)) {
		t.Fatal("a token came back early")
	}
}

// readCapEntry reads the grant at ref's pinned revision from the cap bucket.
func readCapEntry(t *testing.T, url string, ref capability.Ref) capability.Entry {
	t.Helper()
	nc, err := connectTest(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	kv, err := js.KeyValue(ctx, capability.Bucket)
	if err != nil {
		t.Fatal(err)
	}
	e, err := kv.GetRevision(ctx, ref.Key, ref.Revision)
	if err != nil {
		t.Fatal(err)
	}
	var entry capability.Entry
	if err := json.Unmarshal(e.Value(), &entry); err != nil {
		t.Fatal(err)
	}
	return entry
}
