package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
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
	scope := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scope, []byte(`{"present":true,"projects":["p1"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	r := startRigWith(t, func(c *Config) {
		c.Namespace = "kubeagents-system"
		c.ScopeFile = scope
		if tweak != nil {
			tweak(c)
		}
	})
	r.g.profiles.put("fleet-audit", &lib.AgentCard{Name: "fleet-audit", Tiers: []string{"read", "unattended"}})
	r.g.profiles.put("readonly", &lib.AgentCard{Name: "readonly", Tiers: []string{"read"}})
	r.g.profiles.put("cluster", &lib.AgentCard{Name: "cluster", Tiers: []string{"read", "unattended"}, Parameters: clusterParams})
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
		_, err := tr.request("schedule", []byte("{"), time.Second)
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
	msg, err := tr.request(source, data, 5*time.Second)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	var reply lib.TriggerSubmitReply
	if err := json.Unmarshal(msg.Data, &reply); err != nil {
		t.Fatalf("reply %q: %v", msg.Data, err)
	}
	return reply
}

// request submits data for source with the reply on the source's own reply
// namespace, as the operator will, and waits for the answer.
func (tr *triggerRig) request(source string, data []byte, wait time.Duration) (*nats.Msg, error) {
	reply := lib.TriggerReplySubject(source, nats.NewInbox()[len("_INBOX."):])
	sub, err := tr.nc.SubscribeSync(reply)
	if err != nil {
		return nil, err
	}
	defer func() { _ = sub.Unsubscribe() }()
	if err := tr.nc.PublishRequest(lib.TriggerSubmitSubject(source), reply, data); err != nil {
		return nil, err
	}
	return sub.NextMsg(wait)
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
	if reply.TaskID != triggerTaskID(s.IdempotencyKey, 0) {
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
	if len(rec.TriggerRuns) != 1 || rec.TriggerRuns[0].Key != s.IdempotencyKey || rec.TriggerRuns[0].Task != reply.TaskID ||
		!rec.TriggerRuns[0].Published || rec.TriggerRuns[0].Capability == nil || *rec.TriggerRuns[0].Capability != ref {
		t.Errorf("recorded runs = %+v", rec.TriggerRuns)
	}
	if rec.LastActivity.IsZero() {
		t.Error("the trigger record's LastActivity was not stamped")
	}
	if entry.Scope != capability.NamespaceScope("kubeagents-system") {
		t.Errorf("grant scope = %q, want the install namespace's", entry.Scope)
	}
}

// A submission naming a profile script carries it into both the grant and
// the authority the envelope claims, which is what the dispatcher checks
// against the profile's allowlist and the worker against its credential
// (#5622).
func TestATriggerSubmissionsScriptIsMintedIntoTheGrantAndTheAuthority(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	s.Trigger.Script = "compliance-audit"
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil || reply.TaskID == "" {
		t.Fatalf("reply = %+v", reply)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	var env lib.Envelope
	if err := json.Unmarshal(tr.submissions()[0].Data, &env); err != nil {
		t.Fatal(err)
	}
	want := capability.Trigger{Name: s.Trigger.Name, Origin: "builtin", Stream: "compliance-audit", Script: "compliance-audit"}
	claimed, err := capability.TriggerFromAuthority(env.Authority)
	if err != nil || claimed == nil || *claimed != want {
		t.Fatalf("authority trigger = %+v, %v; want %+v", claimed, err, want)
	}
	ref, present, err := capability.RefFromAuthority(env.Authority)
	if err != nil || !present {
		t.Fatalf("authority grant: %+v, %v, %v", ref, present, err)
	}
	if entry := readCapEntry(t, tr.url, ref); entry.Trigger == nil || *entry.Trigger != want {
		t.Errorf("grant trigger = %+v, want %+v", entry.Trigger, want)
	}
}

// A pending run published again carries the trigger its grant was minted
// with, not the retry's: a retry naming another script (spec.script edited,
// or an operator rolled back) would otherwise claim a trigger the grant
// doesn't bind, and the verifier would refuse the task the gateway answered.
func TestAPendingTriggerRunIsPublishedWithItsMintedTrigger(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	s.Trigger.Script = "compliance-audit"
	first := tr.submit(t, "schedule", s)
	if first.Refused != nil {
		t.Fatalf("refused: %+v", first.Refused)
	}
	ctx := context.Background()
	key := "trigger:schedule/kubeagents-system/compliance-audit"
	rec, err := tr.g.reg.Get(ctx, key)
	if err != nil || rec == nil {
		t.Fatalf("record: %v", err)
	}
	rec.TriggerRuns[0].Published = false
	if err := tr.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	retry := s
	retry.Trigger.Script = ""
	again := tr.submit(t, "schedule", retry)
	if again.Refused != nil || again.TaskID != first.TaskID {
		t.Fatalf("the pending run's repeat = %+v, want task %s", again, first.TaskID)
	}
	waitFor(t, "the run published again", func() bool { return len(tr.submissions()) == 2 })
	var env lib.Envelope
	if err := json.Unmarshal(tr.submissions()[1].Data, &env); err != nil {
		t.Fatal(err)
	}
	claimed, err := capability.TriggerFromAuthority(env.Authority)
	if err != nil || claimed == nil || claimed.Script != "compliance-audit" {
		t.Fatalf("the republished authority trigger = %+v, %v; want the minted script", claimed, err)
	}
	ref, _, err := capability.RefFromAuthority(env.Authority)
	if err != nil {
		t.Fatal(err)
	}
	if entry := readCapEntry(t, tr.url, ref); entry.Trigger == nil || *entry.Trigger != *claimed {
		t.Errorf("grant trigger %+v, authority %+v: they must be equal", entry.Trigger, *claimed)
	}
}

// A script that is a path, not a name, is refused before anything is minted.
func TestATriggerSubmissionWithAScriptPathIsRefused(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	s.Trigger.Script = "/opt/kube-agents/scripts/compliance_audit.py"
	reply := tr.submit(t, "schedule", s)
	if reply.Refused == nil || reply.Refused.Reason != lib.TriggerRefusedInvalid || reply.TaskID != "" {
		t.Fatalf("reply = %+v, want refused %s", reply, lib.TriggerRefusedInvalid)
	}
	if n := len(tr.submissions()); n != 0 {
		t.Errorf("%d submission(s) published for a refused trigger", n)
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
	// The first key, retried after the second, is still the first run.
	if again := tr.submit(t, "schedule", s); again.TaskID != first.TaskID || again.Refused != nil {
		t.Fatalf("the first key after the second = %+v, want task %s", again, first.TaskID)
	}
	time.Sleep(200 * time.Millisecond)
	if n := len(tr.submissions()); n != 2 {
		t.Fatalf("%d submissions published, want 2", n)
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
		{"a target the scope refuses", "schedule", func() lib.TriggerSubmit {
			s := goodScheduleSubmit()
			s.Profile = "cluster"
			s.Parameters = map[string]string{"project": "p-refused", "location": "us-central1", "cluster": "c1"}
			return s
		}(), lib.TriggerRefusedInvalid},
		{"data after the object", "schedule", func() []byte {
			b, _ := json.Marshal(goodScheduleSubmit())
			return append(b, []byte(` {"x":1}`)...)
		}(), lib.TriggerRefusedInvalid},
		{"a control character in the run token", "schedule", func() lib.TriggerSubmit {
			s := goodScheduleSubmit()
			s.IdempotencyKey = "schedule/kubeagents-system/compliance-audit/17\x01"
			return s
		}(), lib.TriggerRefusedInvalid},
		{"another namespace's schedule", "schedule", func() lib.TriggerSubmit {
			s := goodScheduleSubmit()
			s.Schedule.Namespace = "team-a"
			s.IdempotencyKey = "schedule/team-a/compliance-audit/1763100000"
			return s
		}(), lib.TriggerRefusedInvalid},
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

// TestATriggerIngressWithNoNamespaceRefusesEverySubmission: a gateway that
// doesn't know its own namespace can't tell a built-in schedule from
// another namespace's, so it starts nothing.
func TestATriggerIngressWithNoNamespaceRefusesEverySubmission(t *testing.T) {
	r := startRig(t)
	r.g.profiles.put("fleet-audit", &lib.AgentCard{Name: "fleet-audit", Tiers: []string{"unattended"}})
	data, err := json.Marshal(goodScheduleSubmit())
	if err != nil {
		t.Fatal(err)
	}
	reply := r.g.handleTriggerSubmit(context.Background(), lib.TriggerSubmitSubject("schedule"), data)
	if reply.Refused == nil || reply.Refused.Reason != lib.TriggerRefusedInvalid {
		t.Fatalf("reply = %+v, want invalid", reply)
	}
}

// TestATriggerTasksEndPostsNothingToChat: a trigger's task that completes
// is relayed, but nothing is posted to its conversation, which no chat
// backend owns (the result's delivery is Track 3's).
func TestATriggerTasksEndPostsNothingToChat(t *testing.T) {
	tr := startTriggerRig(t, nil)
	reply := tr.submit(t, "schedule", goodScheduleSubmit())
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	var origin lib.Envelope
	if err := json.Unmarshal(tr.submissions()[0].Data, &origin); err != nil {
		t.Fatal(err)
	}
	exec := tr.execFor(t, &origin, "fleet-audit")
	completeTask(t, exec, "No findings.")
	waitFor(t, "the trigger task's terminal", func() bool {
		rec, err := tr.g.reg.Get(context.Background(), "trigger:schedule/kubeagents-system/compliance-audit")
		return err == nil && rec != nil && rec.ActiveTask == nil
	})
	tr.adapter.mu.Lock()
	defer tr.adapter.mu.Unlock()
	for _, p := range tr.adapter.posts {
		if strings.HasPrefix(p.Conversation, triggerConversationPrefix) {
			t.Fatalf("posted to a trigger's conversation: %+v", p)
		}
	}
}

// TestATriggerSubmissionWithAReplyOutsideItsNamespaceStartsNothing: a
// submission whose reply subject isn't a2a.trigger.reply.<source>.<id>
// (here, a plain inbox) is dropped unanswered, and no task starts.
func TestATriggerSubmissionWithAReplyOutsideItsNamespaceStartsNothing(t *testing.T) {
	tr := startTriggerRig(t, nil)
	data, err := json.Marshal(goodScheduleSubmit())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tr.nc.Request(lib.TriggerSubmitSubject("schedule"), data, 500*time.Millisecond); err == nil {
		t.Fatal("a submission with an inbox reply was answered")
	}
	time.Sleep(200 * time.Millisecond)
	if n := len(tr.submissions()); n != 0 {
		t.Fatalf("%d submissions published", n)
	}
}

// streamCount is how many messages PROFILE_SUBMITS holds on subject.
func streamCount(t *testing.T, url, subject string) uint64 {
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
	stream, err := js.Stream(ctx, lib.ProfileSubmitStream)
	if err != nil {
		t.Fatal(err)
	}
	info, err := stream.Info(ctx, jetstream.WithSubjectFilter(subject))
	if err != nil {
		t.Fatal(err)
	}
	return info.State.Subjects[subject]
}

// TestAPendingTriggerRunIsPublishedAgainNotMintedAgain: a run recorded but
// not marked published (its publish failed, or the gateway stopped) is
// published again under the same task and grant when the key repeats; the
// stream keeps one copy, and no second grant is minted.
func TestAPendingTriggerRunIsPublishedAgainNotMintedAgain(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	first := tr.submit(t, "schedule", s)
	if first.Refused != nil {
		t.Fatalf("refused: %+v", first.Refused)
	}
	ctx := context.Background()
	key := "trigger:schedule/kubeagents-system/compliance-audit"
	rec, err := tr.g.reg.Get(ctx, key)
	if err != nil || rec == nil {
		t.Fatalf("record: %v", err)
	}
	rec.TriggerRuns[0].Published = false
	if err := tr.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	again := tr.submit(t, "schedule", s)
	if again.Refused != nil || again.TaskID != first.TaskID {
		t.Fatalf("the pending run's repeat = %+v, want task %s", again, first.TaskID)
	}
	subject := lib.TaskSubmitSubject("fleet-audit", "unattended", lib.SubmitKindTop, first.TaskID)
	if n := streamCount(t, tr.url, subject); n != 1 {
		t.Fatalf("PROFILE_SUBMITS holds %d copies of the run, want 1", n)
	}
	if rec, _ = tr.g.reg.Get(ctx, key); rec == nil || !rec.TriggerRuns[0].Published {
		t.Fatalf("the run is not marked published: %+v", rec)
	}
}

// TestATriggerKeyAnUnrecordedAttemptMintedTakesTheNextTask: a grant left by
// an attempt that failed before it was recorded (so never published) takes
// the run's first task id; the cap bucket refuses a second write to it, and
// the run takes the next attempt's id instead of being refused for good.
func TestATriggerKeyAnUnrecordedAttemptMintedTakesTheNextTask(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	if _, err := capability.NewMinter(tr.g.client.JetStream()).Mint(context.Background(), triggerTaskID(s.IdempotencyKey, 0), capability.Entry{
		Tier: capability.TierUnattended, Scope: capability.NamespaceScope("kubeagents-system"), Profile: "fleet-audit", Delegate: profileGrantDelegate,
		Trigger: &capability.Trigger{Name: s.Trigger.Name, Origin: "builtin", Stream: s.Trigger.Stream},
	}); err != nil {
		t.Fatal(err)
	}
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	if reply.TaskID != triggerTaskID(s.IdempotencyKey, 1) {
		t.Fatalf("task = %s, want the second attempt's", reply.TaskID)
	}
}

// status asks after taskID for the schedule s names, as the operator will.
func (tr *triggerRig) status(t *testing.T, s lib.TriggerSubmit, taskID string) lib.TriggerStatusReply {
	t.Helper()
	data, err := json.Marshal(lib.TriggerStatus{Schedule: s.Schedule, TaskID: taskID})
	if err != nil {
		t.Fatal(err)
	}
	reply := lib.TriggerReplySubject("schedule", nats.NewInbox()[len("_INBOX."):])
	sub, err := tr.nc.SubscribeSync(reply)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	if err := tr.nc.PublishRequest(lib.TriggerStatusSubject("schedule"), reply, data); err != nil {
		t.Fatal(err)
	}
	msg, err := sub.NextMsg(5 * time.Second)
	if err != nil {
		t.Fatalf("status reply: %v", err)
	}
	var out lib.TriggerStatusReply
	if err := json.Unmarshal(msg.Data, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

// TestATriggersStatusIsAnsweredForItsOwnTasksOnly: the gateway answers what
// state a schedule's run is in, from its fold of TASKS, through to the
// terminal; a task it never started for that schedule is not found, so a
// source reads only its own runs.
func TestATriggersStatusIsAnsweredForItsOwnTasksOnly(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	if got := tr.status(t, s, reply.TaskID); !got.Found || got.Terminal {
		t.Fatalf("a running task's status = %+v", got)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	var origin lib.Envelope
	if err := json.Unmarshal(tr.submissions()[0].Data, &origin); err != nil {
		t.Fatal(err)
	}
	completeTask(t, tr.execFor(t, &origin, "fleet-audit"), "No findings.")
	var got lib.TriggerStatusReply
	waitFor(t, "the terminal in the status", func() bool {
		got = tr.status(t, s, reply.TaskID)
		return got.Terminal
	})
	if !got.Found || got.State != lib.StateCompleted {
		t.Fatalf("the ended task's status = %+v", got)
	}
	if got := tr.status(t, s, "task-not-this-schedules"); got.Found || got.Refused != nil {
		t.Fatalf("an unknown task = %+v, want not found", got)
	}
	other := s
	other.Schedule.Name = "stockout-prevention"
	if got := tr.status(t, other, reply.TaskID); got.Found {
		t.Fatalf("another schedule read this run: %+v", got)
	}
}

// The ingress resubscribes on the server's refusal of its own subjects only:
// the error text nats.go hands for a refused SUB names the subject, and
// nothing else (another subscription's refusal, a publish refusal, a
// disconnect) restarts it.
func TestTheIngressResubscribesOnlyOnItsOwnSubscriptionsRefusal(t *testing.T) {
	for text, want := range map[string]bool{
		`nats: permissions violation: Permissions Violation for Subscription to "a2a.trigger.*.submit"`:    true,
		`nats: permissions violation: Permissions Violation for Subscription to "a2a.trigger.*.status"`:    true,
		`nats: permissions violation: Permissions Violation for Subscription to "chat.console.*.in"`:       false,
		`nats: permissions violation: Permissions Violation for Publish to "a2a.trigger.reply.schedule.x"`: false,
		`nats: connection closed`: false,
	} {
		if got := isTriggerSubscriptionRefusal(errors.New(text)); got != want {
			t.Errorf("%q: %v, want %v", text, got, want)
		}
	}
	if isTriggerSubscriptionRefusal(nil) {
		t.Error("nil is a refusal")
	}
}

// A status query whose reply names anything but the source's own reply
// namespace is dropped, as a submission's is.
func TestATriggerStatusWithAReplyOutsideItsNamespaceIsNotAnswered(t *testing.T) {
	tr := startTriggerRig(t, nil)
	data, err := json.Marshal(lib.TriggerStatus{Schedule: lib.ScheduleRef{Namespace: "kubeagents-system", Name: "compliance-audit"}, TaskID: "t"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tr.nc.Request(lib.TriggerStatusSubject("schedule"), data, 500*time.Millisecond); err == nil {
		t.Fatal("a status query with an inbox reply was answered")
	}
}

// Every trigger run the record keeps still has its task in the record's
// Tasks, where a status query reads its addressee.
func TestATriggersKeptRunsNeverOutliveTheirTasks(t *testing.T) {
	if triggerRunsKept > taskHistoryCap {
		t.Fatalf("triggerRunsKept %d > taskHistoryCap %d: a kept run could lose its task and answer submitted forever", triggerRunsKept, taskHistoryCap)
	}
}

// A published run's retry is answered with its task even when the profile has
// left the directory since (a new leader before its first DIRECTORY read, or a
// deleted profile): it needs nothing from the directory. A new key for the
// same missing profile is still refused.
func TestAPublishedRunsRetryNeedsNoDirectory(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	first := tr.submit(t, "schedule", s)
	if first.Refused != nil || first.TaskID == "" {
		t.Fatalf("first = %+v", first)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	tr.g.profiles.put(s.Profile, nil)
	if again := tr.submit(t, "schedule", s); again.TaskID != first.TaskID || again.Refused != nil {
		t.Fatalf("retry with the profile gone = %+v, want task %s", again, first.TaskID)
	}
	next := s
	next.IdempotencyKey = "schedule/kubeagents-system/compliance-audit/1763100300"
	if r := tr.submit(t, "schedule", next); r.Refused == nil || r.Refused.Reason != lib.TriggerRefusedProfileUnknown {
		t.Fatalf("a new key with the profile gone = %+v, want profile_unknown", r)
	}
}

// failingMint makes the gateway's trigger mints fail with err while fail is
// above zero, counting each down, and mint for real after.
func failingMint(tr *triggerRig, fail *int, err error) {
	tr.g.triggerMint = func(ctx context.Context, taskID string, e capability.Entry) (capability.Ref, error) {
		if *fail > 0 {
			*fail--
			return capability.Ref{}, err
		}
		return capability.NewMinter(tr.g.client.JetStream()).Mint(ctx, taskID, e)
	}
}

// A mint that fails hands its rate token back (#5923): more failed retries
// of one slot than the hourly cap admits are each refused unavailable, never
// rate_limited, and the slot then runs.
func TestAFailedMintSpendsNoRateToken(t *testing.T) {
	tr := startTriggerRig(t, func(c *Config) { c.TriggerRatePerHour = 3 })
	s := goodScheduleSubmit()
	fail := 5
	failingMint(tr, &fail, errors.New("cap bucket unavailable"))
	for i := 0; i < 5; i++ {
		if r := tr.submit(t, "schedule", s); r.Refused == nil || r.Refused.Reason != lib.TriggerRefusedUnavailable {
			t.Fatalf("failed attempt %d = %+v, want unavailable", i+1, r)
		}
	}
	r := tr.submit(t, "schedule", s)
	if r.Refused != nil || r.TaskID != triggerTaskID(s.IdempotencyKey, 0) {
		t.Fatalf("after the failures = %+v, want the first attempt's task", r)
	}
}

// A record write that fails after the mint costs exactly one attempt, and
// the attempt survives a restart (a fresh rate limiter, the same record): the
// retry resumes at the next task id instead of walking from the first, and
// still runs after more failures than the old cap of four allowed.
func TestARecordWriteFailureCostsOneAttemptAcrossARestart(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	failRuns := 6
	tr.g.reg.beforePut = func(rec *SessionRecord) error {
		if failRuns > 0 && rec.triggerRun(s.IdempotencyKey) != nil && !rec.TriggerRuns[len(rec.TriggerRuns)-1].Published {
			failRuns--
			return errors.New("kv write failed")
		}
		return nil
	}
	mints := 0
	tr.g.triggerMint = func(ctx context.Context, taskID string, e capability.Entry) (capability.Ref, error) {
		mints++
		return capability.NewMinter(tr.g.client.JetStream()).Mint(ctx, taskID, e)
	}
	for i := 0; i < 6; i++ {
		if r := tr.submit(t, "schedule", s); r.Refused == nil || r.Refused.Reason != lib.TriggerRefusedUnavailable {
			t.Fatalf("failed attempt %d = %+v, want unavailable", i+1, r)
		}
		// A restart between retries: the limiter is new, the record isn't.
		tr.g.triggerLimits = newTriggerRateLimiter(DefaultTriggerRatePerHour)
	}
	r := tr.submit(t, "schedule", s)
	if r.Refused != nil || r.TaskID != triggerTaskID(s.IdempotencyKey, 6) {
		t.Fatalf("after six failed record writes = %+v, want attempt 6's task %s", r, triggerTaskID(s.IdempotencyKey, 6))
	}
	// Each retry mints the recorded attempt (refused: minted already) and
	// then the next one; none walks from the first again.
	if want := 1 + 2*6; mints != want {
		t.Fatalf("%d mints, want %d: a retry walked from an earlier attempt", mints, want)
	}
	rec, err := tr.g.reg.Get(context.Background(), "trigger:schedule/kubeagents-system/compliance-audit")
	if err != nil || rec == nil || len(rec.TriggerPending) != 0 {
		t.Fatalf("the pending attempt outlived the recorded run: %+v, %v", rec, err)
	}
}

// A pending attempt whose grant an earlier try minted moves to the next
// attempt from where it was, never from the first.
func TestAPendingAttemptMintedAlreadyMovesToTheNextFromWhereItWas(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	ctx := context.Background()
	key := "trigger:schedule/kubeagents-system/compliance-audit"
	rec := &SessionRecord{Key: key, ContextID: "ctx-pending", Kind: triggerAudienceKind, LastActivity: time.Now().UTC(),
		TriggerPending: map[string]TriggerPendingRun{s.IdempotencyKey: {Attempt: 9, Task: triggerTaskID(s.IdempotencyKey, 9)}}}
	if err := tr.g.reg.Create(ctx, rec); err != nil {
		t.Fatal(err)
	}
	if _, err := capability.NewMinter(tr.g.client.JetStream()).Mint(ctx, triggerTaskID(s.IdempotencyKey, 9), capability.Entry{
		Tier: capability.TierUnattended, Scope: capability.NamespaceScope("kubeagents-system"), Profile: "fleet-audit", Delegate: profileGrantDelegate,
		Trigger: &capability.Trigger{Name: s.Trigger.Name, Origin: "builtin", Stream: s.Trigger.Stream},
	}); err != nil {
		t.Fatal(err)
	}
	if r := tr.submit(t, "schedule", s); r.Refused != nil || r.TaskID != triggerTaskID(s.IdempotencyKey, 10) {
		t.Fatalf("reply = %+v, want attempt 10's task", r)
	}
}

// Every attempt counts toward the cap, refunded or not: with the first
// sixteen task ids already minted, the run is refused unavailable without a
// seventeenth mint.
func TestARunIsRefusedOnceItsSixteenAttemptsAreSpent(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	ctx := context.Background()
	for n := 0; n < triggerMintAttempts; n++ {
		if _, err := capability.NewMinter(tr.g.client.JetStream()).Mint(ctx, triggerTaskID(s.IdempotencyKey, n), capability.Entry{
			Tier: capability.TierUnattended, Scope: capability.NamespaceScope("kubeagents-system"), Profile: "fleet-audit", Delegate: profileGrantDelegate,
			Trigger: &capability.Trigger{Name: s.Trigger.Name, Origin: "builtin", Stream: s.Trigger.Stream},
		}); err != nil {
			t.Fatal(err)
		}
	}
	var mints int
	tr.g.triggerMint = func(ctx context.Context, taskID string, e capability.Entry) (capability.Ref, error) {
		mints++
		return capability.NewMinter(tr.g.client.JetStream()).Mint(ctx, taskID, e)
	}
	if r := tr.submit(t, "schedule", s); r.Refused == nil || r.Refused.Reason != lib.TriggerRefusedUnavailable {
		t.Fatalf("reply = %+v, want unavailable", r)
	}
	if mints != triggerMintAttempts {
		t.Fatalf("%d mints, want %d and none past the cap", mints, triggerMintAttempts)
	}
	if r := tr.submit(t, "schedule", s); r.Refused == nil || mints != triggerMintAttempts {
		t.Fatalf("a retry past the cap = %+v after %d mints, want refused without minting", r, mints)
	}
}

// A key whose run has left TriggerRuns is answered with its task from the
// 24-hour key record; past 24 hours it is forgotten.
func TestAKeyOlderThanTheKeptRunsIsStillItsTask(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	ctx := context.Background()
	key := "trigger:schedule/kubeagents-system/compliance-audit"
	rec := &SessionRecord{Key: key, ContextID: "ctx-keys", Kind: triggerAudienceKind, LastActivity: time.Now().UTC(),
		TriggerKeys: map[string]TriggerKeyRecord{s.IdempotencyKey: {Task: "task-from-yesterday", At: time.Now().Add(-23 * time.Hour)}}}
	if err := tr.g.reg.Create(ctx, rec); err != nil {
		t.Fatal(err)
	}
	if r := tr.submit(t, "schedule", s); r.Refused != nil || r.TaskID != "task-from-yesterday" {
		t.Fatalf("reply = %+v, want the recorded task", r)
	}
	if len(tr.submissions()) != 0 {
		t.Fatalf("the repeat started a task: %d submissions", len(tr.submissions()))
	}
	rec.TriggerKeys = map[string]TriggerKeyRecord{"old": {Task: "t-old", At: time.Now().Add(-25 * time.Hour)}}
	rec.recordTriggerKey("new", "t-new", time.Now(), triggerKeysKeptMin)
	if _, ok := rec.TriggerKeys["old"]; ok {
		t.Error("a key past 24 hours wasn't pruned")
	}
	if _, ok := rec.triggerKeyTask("old", time.Now()); ok {
		t.Error("a key past 24 hours was answered")
	}
}

// A refund never fills a bucket past its size.
func TestARefundNeverExceedsTheBucket(t *testing.T) {
	l := newTriggerRateLimiter(2)
	now := time.Now()
	if !l.allow("t", now) {
		t.Fatal("a full bucket refused")
	}
	l.refund("t", now)
	l.refund("t", now)
	l.refund("t", now)
	for i := 0; i < 2; i++ {
		if !l.allow("t", now) {
			t.Fatalf("token %d refused", i+1)
		}
	}
	if l.allow("t", now) {
		t.Fatal("refunds filled the bucket past its size")
	}
}

// End to end: a real run's key is recorded when the run is, even when the
// write marking it published fails, and after more newer runs than
// TriggerRuns keeps have pushed it out, a repeat of the key is still answered
// with its task and starts nothing.
func TestAPublishedKeyOutlivesTheKeptRuns(t *testing.T) {
	tr := startTriggerRig(t, func(c *Config) { c.TriggerRatePerHour = 1000 })
	first := goodScheduleSubmit()
	// The first run's publish lands but the write marking it published
	// fails, so only the key record written with the run can answer later.
	failMark := 1
	tr.g.reg.beforePut = func(rec *SessionRecord) error {
		if run := rec.triggerRun(first.IdempotencyKey); failMark > 0 && run != nil && run.Published {
			failMark--
			return errors.New("kv write failed")
		}
		return nil
	}
	r := tr.submit(t, "schedule", first)
	if r.Refused != nil {
		t.Fatalf("refused: %+v", r.Refused)
	}
	for i := 1; i <= triggerRunsKept; i++ {
		s := goodScheduleSubmit()
		s.IdempotencyKey = fmt.Sprintf("schedule/kubeagents-system/compliance-audit/%d", 1763100000+i*3600)
		if rr := tr.submit(t, "schedule", s); rr.Refused != nil {
			t.Fatalf("run %d refused: %+v", i, rr.Refused)
		}
	}
	rec, err := tr.g.reg.Get(context.Background(), "trigger:schedule/kubeagents-system/compliance-audit")
	if err != nil || rec == nil || rec.triggerRun(first.IdempotencyKey) != nil {
		t.Fatalf("the first run is still kept (%v); the test needs it pushed out", err)
	}
	before := len(tr.submissions())
	again := tr.submit(t, "schedule", first)
	if again.Refused != nil || again.TaskID != r.TaskID {
		t.Fatalf("the repeat = %+v, want task %s", again, r.TaskID)
	}
	time.Sleep(200 * time.Millisecond)
	if n := len(tr.submissions()); n != before {
		t.Fatalf("the repeat started a task: %d submissions, was %d", n, before)
	}
}

// Pending attempts a reconciler gave up on are dropped a day later, and the
// map never grows past triggerPendingKept; key records are capped too, with
// the cap sized from the configured rate.
func TestTriggerRecordMapsAreBounded(t *testing.T) {
	rec := &SessionRecord{}
	now := time.Now()
	rec.setTriggerPending("stale", TriggerPendingRun{Attempt: 3, At: now.Add(-25 * time.Hour)})
	rec.setTriggerPending("fresh", TriggerPendingRun{Attempt: 1, At: now})
	if _, ok := rec.TriggerPending["stale"]; ok {
		t.Error("a day-old pending attempt wasn't dropped")
	}
	for i := 0; i < triggerPendingKept+10; i++ {
		rec.setTriggerPending(fmt.Sprintf("k%d", i), TriggerPendingRun{At: now.Add(time.Duration(i) * time.Second)})
	}
	if len(rec.TriggerPending) != triggerPendingKept {
		t.Errorf("TriggerPending holds %d, want %d", len(rec.TriggerPending), triggerPendingKept)
	}
	for i := 0; i < 40; i++ {
		rec.recordTriggerKey(fmt.Sprintf("key%d", i), "t", now.Add(time.Duration(i)*time.Second), 30)
	}
	if len(rec.TriggerKeys) != 30 {
		t.Errorf("TriggerKeys holds %d, want 30", len(rec.TriggerKeys))
	}
	if _, ok := rec.TriggerKeys["key0"]; ok {
		t.Error("the oldest key wasn't the one evicted")
	}
	g := &Gateway{cfg: &Config{TriggerRatePerHour: 60}}
	if got := g.triggerKeysKept(); got < 60*24 {
		t.Errorf("at 60 an hour the key cap is %d, under a day's runs", got)
	}
}

// A scheduled run may fan out (#6744): its child is minted at the read tier,
// as a fresh root on the parent's scope, with no trigger, so it can read for
// its parent and never write under the schedule's trigger. The child's result
// reaches the parent on its .in, and nothing is posted to chat, since a
// trigger's conversation has no backend.
func TestAScheduledRunsChildIsAReadRootWithNoTrigger(t *testing.T) {
	tr := startTriggerRig(t, nil)
	ctx := context.Background()
	s := goodScheduleSubmit()
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	var origin lib.Envelope
	if err := json.Unmarshal(tr.submissions()[0].Data, &origin); err != nil {
		t.Fatal(err)
	}
	parent := tr.execFor(t, &origin, "fleet-audit")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	postsBefore := len(tr.adapter.posts) + len(tr.adapter.stopPostsCopy())

	if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, "cluster", "list this cluster's nodes",
		targetParams("p1", "us-east1", "prod"))); err != nil {
		t.Fatal(err)
	}
	var childMsg *nats.Msg
	waitFor(t, "the child's submission", func() bool {
		for _, m := range tr.submissions() {
			if _, _, kind, _, ok := lib.ParseSubmitSubject(m.Subject); ok && kind == lib.SubmitKindChild {
				childMsg = m
				return true
			}
		}
		return false
	})
	profile, tier, _, _, _ := lib.ParseSubmitSubject(childMsg.Subject)
	if profile != "cluster" || tier != string(capability.TierRead) {
		t.Fatalf("child submitted on %s, want a read child of cluster", childMsg.Subject)
	}
	var childEnv lib.Envelope
	if err := json.Unmarshal(childMsg.Data, &childEnv); err != nil {
		t.Fatal(err)
	}
	if trig, err := capability.TriggerFromAuthority(childEnv.Authority); err != nil || trig != nil {
		t.Fatalf("the child's authority claims trigger %+v (%v); a child carries none", trig, err)
	}
	ref, present, err := capability.RefFromAuthority(childEnv.Authority)
	if err != nil || !present {
		t.Fatalf("the child pins no grant: %v", err)
	}
	entry := readCapEntry(t, tr.url, ref)
	if entry.Tier != capability.TierRead || entry.Trigger != nil || entry.Profile != "cluster" {
		t.Fatalf("the child's grant = %+v, want read tier on cluster with no trigger", entry)
	}

	child := tr.execFor(t, &childEnv, "cluster")
	if err := child.PublishArtifact(ctx, lib.Artifact{ArtifactID: "a-r", Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "three nodes, all ready"}}}); err != nil {
		t.Fatal(err)
	}
	if err := child.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the child result on the parent's .in", func() bool {
		for _, e := range streamEnvelopes(t, tr.url, lib.TaskInSubject("fleet-audit", origin.TaskID)) {
			if c, ok := lib.ChildResultOf(messageParts(t, e.env)); ok && c.ChildTaskID == childEnv.TaskID && c.Text == "three nodes, all ready" {
				return true
			}
		}
		return false
	})
	if n := len(tr.adapter.posts) + len(tr.adapter.stopPostsCopy()); n != postsBefore {
		t.Fatalf("the fan-out posted to chat: %d posts, was %d", n, postsBefore)
	}
}

// A scheduled run whose delegation is refused hands off to a child that is
// never minted: its own `completed` is only the hand-off line, so its status
// reads failed, never completed (which the reconciler would record as
// Succeeded).
func TestARefusedFanOutReadsFailedNotCompleted(t *testing.T) {
	tr := startTriggerRig(t, nil)
	ctx := context.Background()
	s := goodScheduleSubmit()
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	var origin lib.Envelope
	if err := json.Unmarshal(tr.submissions()[0].Data, &origin); err != nil {
		t.Fatal(err)
	}
	parent := tr.execFor(t, &origin, "fleet-audit")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	// Outside the install's scope (project p9): refused, nothing minted.
	if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, "cluster", "look", targetParams("p9", "us-east1", "prod"))); err != nil {
		t.Fatal(err)
	}
	key := "trigger:schedule/kubeagents-system/compliance-audit"
	waitFor(t, "the refusal on the run's entry", func() bool {
		rec, _ := tr.g.reg.Get(ctx, key)
		return rec != nil && rec.delegationEndFor(origin.TaskID) != ""
	})
	// The worker ends its turn on the hand-off line.
	if err := parent.PublishArtifact(ctx, lib.Artifact{ArtifactID: "r", Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "delegated to cluster"}}}); err != nil {
		t.Fatal(err)
	}
	if err := parent.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	var got lib.TriggerStatusReply
	waitFor(t, "the run's terminal in its status", func() bool {
		got = tr.status(t, s, reply.TaskID)
		return got.Terminal
	})
	if got.State != lib.StateFailed {
		t.Fatalf("a refused fan-out's status = %+v, want failed", got)
	}
}

// TestAScheduledRunHandsNoTaskToAProfileThatTakesNoScheduledWork: the
// child runs at read, but for a schedule, so its profile's card must list
// unattended, as the trigger's own profile's must (#6744).
func TestAScheduledRunHandsNoTaskToAProfileThatTakesNoScheduledWork(t *testing.T) {
	tr := startTriggerRig(t, nil)
	ctx := context.Background()
	s := goodScheduleSubmit()
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	var origin lib.Envelope
	if err := json.Unmarshal(tr.submissions()[0].Data, &origin); err != nil {
		t.Fatal(err)
	}
	parent := tr.execFor(t, &origin, "fleet-audit")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := parent.PublishArtifact(ctx, delegateArtifact(t, "readonly", "look")); err != nil {
		t.Fatal(err)
	}
	key := "trigger:schedule/kubeagents-system/compliance-audit"
	waitFor(t, "the refusal on the run's entry", func() bool {
		rec, _ := tr.g.reg.Get(ctx, key)
		return rec != nil && strings.Contains(rec.delegationEndFor(origin.TaskID), fmt.Sprintf(noticeUnattendedChild, "readonly"))
	})
	for _, m := range tr.submissions() {
		if _, _, kind, _, ok := lib.ParseSubmitSubject(m.Subject); ok && kind == lib.SubmitKindChild {
			t.Fatalf("a child was minted on %s", m.Subject)
		}
	}
}

// TestAScheduledRunThatMintedAChildReadsItsOwnEnd: a refused request
// followed by one that mints a child is not a bare hand-off, so the run's
// own completed stands.
func TestAScheduledRunThatMintedAChildReadsItsOwnEnd(t *testing.T) {
	tr := startTriggerRig(t, nil)
	ctx := context.Background()
	s := goodScheduleSubmit()
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	var origin lib.Envelope
	if err := json.Unmarshal(tr.submissions()[0].Data, &origin); err != nil {
		t.Fatal(err)
	}
	parent := tr.execFor(t, &origin, "fleet-audit")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	key := "trigger:schedule/kubeagents-system/compliance-audit"
	if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, "cluster", "look", targetParams("p9", "us-east1", "prod"))); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the refusal on the run's entry", func() bool {
		rec, _ := tr.g.reg.Get(ctx, key)
		if rec == nil {
			return false
		}
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && ref.DelegationEnd != ""
	})
	if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, "cluster", "look", targetParams("p1", "us-east1", "prod"))); err != nil {
		t.Fatal(err)
	}
	var childMsg *nats.Msg
	waitFor(t, "the child's submission", func() bool {
		for _, m := range tr.submissions() {
			if _, _, kind, _, ok := lib.ParseSubmitSubject(m.Subject); ok && kind == lib.SubmitKindChild {
				childMsg = m
				return true
			}
		}
		return false
	})
	var childEnv lib.Envelope
	if err := json.Unmarshal(childMsg.Data, &childEnv); err != nil {
		t.Fatal(err)
	}
	child := tr.execFor(t, &childEnv, "cluster")
	if err := child.PublishArtifact(ctx, lib.Artifact{ArtifactID: "a-r", Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "fine"}}}); err != nil {
		t.Fatal(err)
	}
	if err := child.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	if err := parent.PublishArtifact(ctx, lib.Artifact{ArtifactID: "r", Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "one cluster checked, all fine"}}}); err != nil {
		t.Fatal(err)
	}
	if err := parent.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	var got lib.TriggerStatusReply
	waitFor(t, "the run's terminal in its status", func() bool {
		got = tr.status(t, s, reply.TaskID)
		return got.Terminal
	})
	if got.State != lib.StateCompleted {
		t.Fatalf("a run that minted a child reads %+v, want completed", got)
	}
}

// TestAScheduledRunsChildCarriesTheScheduledMark: the child runs at read,
// so the card check can't key on its tier when it delegates in turn; the
// mark it carries (TaskRef.Scheduled) holds its own children to the same
// cards. A child can't delegate while it is the conversation's live task
// today (delegation.busy), so this checks the mark, not a grandchild.
func TestAScheduledRunsChildCarriesTheScheduledMark(t *testing.T) {
	tr := startTriggerRig(t, nil)
	ctx := context.Background()
	s := goodScheduleSubmit()
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	var origin lib.Envelope
	if err := json.Unmarshal(tr.submissions()[0].Data, &origin); err != nil {
		t.Fatal(err)
	}
	parent := tr.execFor(t, &origin, "fleet-audit")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, "cluster", "look", targetParams("p1", "us-east1", "prod"))); err != nil {
		t.Fatal(err)
	}
	key := "trigger:schedule/kubeagents-system/compliance-audit"
	var child TaskRef
	waitFor(t, "the child's entry", func() bool {
		rec, _ := tr.g.reg.Get(ctx, key)
		if rec == nil {
			return false
		}
		p, ok := rec.TaskRefFor(origin.TaskID)
		if !ok || len(p.Children) == 0 {
			return false
		}
		child, ok = rec.TaskRefFor(p.Children[0])
		return ok
	})
	if child.Tier != string(capability.TierRead) || !child.Scheduled || !child.belowSchedule() {
		t.Fatalf("the child's entry = tier %q scheduled %v, want read and marked", child.Tier, child.Scheduled)
	}
	if (TaskRef{Tier: string(capability.TierRead)}).belowSchedule() {
		t.Fatal("an unmarked read task reads as below a schedule")
	}
}

// TestAScheduledRunHandsNoTaskWhereAPinnedProfileTakesNoScheduledWork: a
// trigger submitted straight to a profile on a cluster whose pinned
// profile doesn't list unattended is refused, so a scheduled run's child
// sent there is refused too, though it runs at read.
func TestAScheduledRunHandsNoTaskWhereAPinnedProfileTakesNoScheduledWork(t *testing.T) {
	tr := startTriggerRig(t, nil)
	tr.g.profiles.put("prod-east", &lib.AgentCard{Name: "prod-east", Tiers: []string{"read"},
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "prod"}})
	ctx := context.Background()
	s := goodScheduleSubmit()
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	var origin lib.Envelope
	if err := json.Unmarshal(tr.submissions()[0].Data, &origin); err != nil {
		t.Fatal(err)
	}
	parent := tr.execFor(t, &origin, "fleet-audit")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, "cluster", "look", targetParams("p1", "us-east1", "prod"))); err != nil {
		t.Fatal(err)
	}
	key := "trigger:schedule/kubeagents-system/compliance-audit"
	waitFor(t, "the refusal on the run's entry", func() bool {
		rec, _ := tr.g.reg.Get(ctx, key)
		return rec != nil && strings.Contains(rec.delegationEndFor(origin.TaskID), "prod-east")
	})
	for _, m := range tr.submissions() {
		if _, _, kind, _, ok := lib.ParseSubmitSubject(m.Subject); ok && kind == lib.SubmitKindChild {
			t.Fatalf("a child was minted on %s", m.Subject)
		}
	}
}
