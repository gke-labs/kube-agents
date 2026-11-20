package gateway

import (
	"context"
	"encoding/json"
	"errors"
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

// stop asks the gateway to stop taskID of s's schedule, on the source's own
// reply namespace, as the operator will. The stop subscription runs on its
// own loop, so the first ask waits for it.
func (tr *triggerRig) stop(t *testing.T, s lib.TriggerSubmit, taskID string) lib.TriggerStopReply {
	t.Helper()
	data, err := json.Marshal(lib.TriggerStop{Schedule: s.Schedule, TaskID: taskID})
	if err != nil {
		t.Fatal(err)
	}
	var out lib.TriggerStopReply
	waitFor(t, "the stop's answer", func() bool {
		reply := lib.TriggerReplySubject("schedule", nats.NewInbox()[len("_INBOX."):])
		sub, err := tr.nc.SubscribeSync(reply)
		if err != nil {
			return false
		}
		defer func() { _ = sub.Unsubscribe() }()
		if err := tr.nc.PublishRequest(lib.TriggerStopSubject("schedule"), reply, data); err != nil {
			return false
		}
		msg, err := sub.NextMsg(time.Second)
		if err != nil {
			return false
		}
		return json.Unmarshal(msg.Data, &out) == nil
	})
	return out
}

// A schedule's stop ends its own run and no other (#6052): the gateway writes
// the run's cancel on its .in subject, under the run's own trigger authority,
// and its dispatcher stop record, so a run still starting has its Job removed.
// A second stop writes nothing again. A task the schedule's record doesn't
// hold, or another schedule's run, is an unknown run, and nothing is written.
func TestATriggerStopEndsItsOwnRunOnly(t *testing.T) {
	tr := startTriggerRig(t, nil)
	var mu sync.Mutex
	var cancels, stops []*nats.Msg
	if _, err := tr.nc.Subscribe("a2a.tasks.fleet-audit.*.in", func(m *nats.Msg) {
		mu.Lock()
		cancels = append(cancels, m)
		mu.Unlock()
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := tr.nc.Subscribe(lib.DispatchStopPrefix+">", func(m *nats.Msg) {
		mu.Lock()
		stops = append(stops, m)
		mu.Unlock()
	}); err != nil {
		t.Fatal(err)
	}
	count := func() (int, int) {
		mu.Lock()
		defer mu.Unlock()
		return len(cancels), len(stops)
	}
	s := goodScheduleSubmit()
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })

	// Another schedule with a run of its own still can't stop this one.
	other := s
	other.Schedule.Name = "stockout-prevention"
	other.Trigger = lib.TriggerClaim{Name: "schedule/stockout-prevention", Origin: lib.TriggerOriginBuiltin, Stream: "stockout-prevention"}
	other.IdempotencyKey = "schedule/kubeagents-system/stockout-prevention/1763100000"
	other.Delivery.JobKey = "stockout-prevention"
	if r := tr.submit(t, "schedule", other); r.Refused != nil {
		t.Fatalf("the other schedule's run was refused: %+v", r.Refused)
	}
	waitFor(t, "both submissions", func() bool { return len(tr.submissions()) == 2 })
	if got := tr.stop(t, other, reply.TaskID); got.Result != lib.TriggerStopUnknownRun {
		t.Fatalf("another schedule's stop of this run = %+v, want unknown_run", got)
	}
	if got := tr.stop(t, s, "task-not-this-schedules"); got.Result != lib.TriggerStopUnknownRun {
		t.Fatalf("a task the record doesn't hold = %+v, want unknown_run", got)
	}
	if c, st := count(); c != 0 || st != 0 {
		t.Fatalf("an unknown run's stop wrote %d cancels and %d stop records", c, st)
	}

	if got := tr.stop(t, s, reply.TaskID); got.Result != lib.TriggerStopStopped {
		t.Fatalf("the schedule's own stop = %+v, want stopped", got)
	}
	waitFor(t, "the cancel and the stop record", func() bool { c, st := count(); return c == 1 && st == 1 })
	mu.Lock()
	cancel, stopRec := cancels[0], stops[0]
	mu.Unlock()
	if want := lib.TaskInSubject("fleet-audit", reply.TaskID); cancel.Subject != want {
		t.Errorf("cancel on %s, want %s", cancel.Subject, want)
	}
	if want := lib.DispatchStopSubject("fleet-audit", reply.TaskID); stopRec.Subject != want {
		t.Errorf("stop record on %s, want %s", stopRec.Subject, want)
	}
	var env lib.Envelope
	if err := json.Unmarshal(cancel.Data, &env); err != nil {
		t.Fatal(err)
	}
	claimed, err := capability.TriggerFromAuthority(env.Authority)
	if err != nil || claimed == nil || claimed.Name != s.Trigger.Name {
		t.Errorf("the cancel's authority trigger = %+v, %v; want the run's", claimed, err)
	}
	// The run's own authority: the trigger as requester, its conversation as
	// the audience, and the run's grant.
	auth := string(env.Authority)
	for _, want := range []string{triggerRequesterBackend, triggerConversationKey("schedule", s.Schedule), s.Trigger.Name} {
		if !strings.Contains(auth, want) {
			t.Errorf("the cancel's authority lacks %q: %s", want, auth)
		}
	}
	if ref, present, err := capability.RefFromAuthority(env.Authority); err != nil || !present || ref.Key == "" {
		t.Errorf("the cancel's authority carries no grant: %+v, %v, %v", ref, present, err)
	}

	if got := tr.stop(t, s, reply.TaskID); got.Result != lib.TriggerStopStopped {
		t.Fatalf("a second stop = %+v, want stopped", got)
	}
	time.Sleep(200 * time.Millisecond)
	if c, st := count(); c != 1 || st != 1 {
		t.Fatalf("a second stop wrote again: %d cancels, %d stop records", c, st)
	}
}

// A stop for a run that has already ended writes nothing and says so.
func TestATriggerStopOfAnEndedRunWritesNothing(t *testing.T) {
	tr := startTriggerRig(t, nil)
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
	completeTask(t, tr.execFor(t, &origin, "fleet-audit"), "No findings.")
	waitFor(t, "the terminal", func() bool { return tr.status(t, s, reply.TaskID).Terminal })
	if got := tr.stop(t, s, reply.TaskID); got.Result != lib.TriggerStopAlreadyFinal {
		t.Fatalf("an ended run's stop = %+v, want already_final", got)
	}
}

// A stop whose reply names anything but the source's own reply namespace is
// dropped, as a submission's is.
func TestATriggerStopWithAReplyOutsideItsNamespaceIsNotAnswered(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	_ = tr.stop(t, s, "t") // the stop subscription is up
	data, err := json.Marshal(lib.TriggerStop{Schedule: s.Schedule, TaskID: "t"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tr.nc.Request(lib.TriggerStopSubject("schedule"), data, 500*time.Millisecond); err == nil {
		t.Fatal("a stop with an inbox reply was answered")
	}
}

// The stop subscription's refusal restarts the stop loop alone, and the
// ingress's own refusals don't count the stop's: a bus that hasn't loaded the
// newer stop grant leaves submissions and status queries running.
func TestTheStopSubscriptionsRefusalIsItsOwn(t *testing.T) {
	stop := `nats: permissions violation: Permissions Violation for Subscription to "a2a.trigger.*.stop"`
	submit := `nats: permissions violation: Permissions Violation for Subscription to "a2a.trigger.*.submit"`
	if isTriggerSubscriptionRefusal(errors.New(stop)) {
		t.Error("the stop's refusal restarts the ingress")
	}
	if !isTriggerStopSubscriptionRefusal(errors.New(stop)) {
		t.Error("the stop's refusal doesn't restart the stop loop")
	}
	if isTriggerStopSubscriptionRefusal(errors.New(submit)) || isTriggerStopSubscriptionRefusal(nil) {
		t.Error("another refusal restarts the stop loop")
	}
}

// A stop whose stop record can't be published is refused unavailable and
// marks nothing, so the operator asks again and the retry sends the record:
// the record is what removes a run still starting, and a stop answered
// stopped without it could never be sent again.
func TestATriggerStopWhoseRecordFailsIsRefusedAndSentOnRetry(t *testing.T) {
	tr := startTriggerRig(t, nil)
	s := goodScheduleSubmit()
	reply := tr.submit(t, "schedule", s)
	if reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	ctx := context.Background()
	js, err := jetstream.New(tr.nc)
	if err != nil {
		t.Fatal(err)
	}
	if err := js.DeleteStream(ctx, lib.DispatchStopStream); err != nil {
		t.Fatal(err)
	}
	if got := tr.stop(t, s, reply.TaskID); got.Refused == nil || got.Refused.Reason != lib.TriggerRefusedUnavailable {
		t.Fatalf("a stop with no stop record = %+v, want unavailable", got)
	}
	rec, err := tr.g.reg.Get(ctx, "trigger:schedule/kubeagents-system/compliance-audit")
	if err != nil || rec == nil {
		t.Fatal(err)
	}
	if ref, _ := rec.TaskRefFor(reply.TaskID); ref.Canceled {
		t.Fatal("the task was marked canceled though its stop record never went out")
	}
	if _, err := js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name: lib.DispatchStopStream, Subjects: []string{lib.DispatchStopPrefix + ">"},
		Retention: jetstream.LimitsPolicy, MaxMsgsPerSubject: 1, AllowDirect: true,
	}); err != nil {
		t.Fatal(err)
	}
	if got := tr.stop(t, s, reply.TaskID); got.Result != lib.TriggerStopStopped {
		t.Fatalf("the retry = %+v, want stopped", got)
	}
	if _, err := js.Stream(ctx, lib.DispatchStopStream); err != nil {
		t.Fatal(err)
	}
	stream, _ := js.Stream(ctx, lib.DispatchStopStream)
	if _, err := stream.GetLastMsgForSubject(ctx, lib.DispatchStopSubject("fleet-audit", reply.TaskID)); err != nil {
		t.Fatalf("the retry wrote no stop record: %v", err)
	}
}
