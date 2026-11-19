package gateway

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// goodEventSubmit is an event source's submission (#6572): a trigger named
// <source>/<kind>, no schedule, a key <source>/<kind>/<token>, and a
// cluster target the rig's scope admits.
func goodEventSubmit(kind, token string) lib.TriggerSubmit {
	return lib.TriggerSubmit{
		Trigger:        lib.TriggerClaim{Name: "event-watcher/" + kind, Origin: lib.TriggerOriginBuiltin},
		IdempotencyKey: "event-watcher/" + kind + "/" + token,
		Profile:        "cluster",
		Prompt:         "Triage the event.",
		Parameters:     map[string]string{"project": "p1", "location": "us-central1", "cluster": "c1"},
		Delivery:       lib.ScheduleDelivery{Mode: lib.ScheduleDeliveryChat, JobKey: kind},
	}
}

// An install that admits no event kind, the default, refuses every event
// submission and starts nothing: the widening ships dark.
func TestAnEventSourceTheGatewayDoesNotAdmitIsRefused(t *testing.T) {
	tr := startTriggerRig(t, nil)
	reply := tr.submit(t, "event-watcher", goodEventSubmit("k8s-event", "k8s-evt-1"))
	if reply.Refused == nil || reply.Refused.Reason != lib.TriggerRefusedInvalid || reply.TaskID != "" {
		t.Fatalf("reply = %+v, want invalid", reply)
	}
	time.Sleep(200 * time.Millisecond)
	if n := len(tr.submissions()); n != 0 {
		t.Fatalf("%d submissions published", n)
	}
}

// An admitted event kind's submission starts an unattended task whose grant
// and authority bind <source>/<kind>, recorded on the run's own
// conversation; no schedule or install namespace is asked for.
func TestAnAdmittedEventSourcesSubmissionStartsATaskOnItsKindsConversation(t *testing.T) {
	tr := startTriggerRig(t, func(c *Config) {
		c.TriggerEventKinds = []string{"event-watcher/k8s-event", "event-watcher/gitops-drift"}
	})
	s := goodEventSubmit("k8s-event", "k8s-evt-1")
	reply := tr.submit(t, "event-watcher", s)
	if reply.Refused != nil || reply.TaskID != triggerTaskID(s.IdempotencyKey, 0) {
		t.Fatalf("reply = %+v", reply)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	m := tr.submissions()[0]
	if want := lib.TaskSubmitSubject("cluster", "unattended", lib.SubmitKindTop, reply.TaskID); m.Subject != want {
		t.Fatalf("published on %s, want %s", m.Subject, want)
	}
	var env lib.Envelope
	if err := json.Unmarshal(m.Data, &env); err != nil {
		t.Fatal(err)
	}
	want := capability.Trigger{Name: "event-watcher/k8s-event", Origin: "builtin"}
	if claimed, err := capability.TriggerFromAuthority(env.Authority); err != nil || claimed == nil || *claimed != want {
		t.Fatalf("authority trigger = %+v, %v", claimed, err)
	}
	ref, present, err := capability.RefFromAuthority(env.Authority)
	if err != nil || !present {
		t.Fatalf("authority grant: %v, %v", present, err)
	}
	if entry := readCapEntry(t, tr.url, ref); entry.Tier != capability.TierUnattended || entry.Profile != "cluster" || entry.Trigger == nil || *entry.Trigger != want {
		t.Errorf("grant = %+v", entry)
	}
	rec, err := tr.g.reg.Get(context.Background(), "trigger:"+s.IdempotencyKey)
	if err != nil || rec == nil || len(rec.TriggerRuns) != 1 || rec.TriggerRuns[0].Key != s.IdempotencyKey {
		t.Fatalf("record = %+v, %v", rec, err)
	}
	// A repeat of the key is the same run.
	if again := tr.submit(t, "event-watcher", s); again.TaskID != reply.TaskID {
		t.Errorf("repeat = %+v, want %s", again, reply.TaskID)
	}
}

// Each event kind has its own bucket at the event rate, apart from the
// schedules' rate.
func TestEachEventKindHasItsOwnRateCap(t *testing.T) {
	tr := startTriggerRig(t, func(c *Config) {
		c.TriggerEventKinds = []string{"event-watcher/k8s-event", "event-watcher/gitops-drift"}
		c.TriggerEventRatePerHour = 2
		c.TriggerRatePerHour = 1
	})
	for i, token := range []string{"a", "b", "c"} {
		reply := tr.submit(t, "event-watcher", goodEventSubmit("k8s-event", token))
		if i < 2 && reply.Refused != nil {
			t.Fatalf("event %d refused: %+v", i, reply.Refused)
		}
		if i == 2 && (reply.Refused == nil || reply.Refused.Reason != lib.TriggerRefusedRateLimited) {
			t.Fatalf("event %d = %+v, want rate_limited", i, reply)
		}
	}
	if reply := tr.submit(t, "event-watcher", goodEventSubmit("gitops-drift", "a")); reply.Refused != nil {
		t.Fatalf("another kind refused: %+v", reply.Refused)
	}
	if reply := tr.submit(t, "schedule", goodScheduleSubmit()); reply.Refused != nil {
		t.Fatalf("a schedule refused: %+v", reply.Refused)
	}
}

// The schedule source keeps its own shape and namespace check when event
// sources are admitted.
func TestAdmittingAnEventSourceLeavesTheScheduleShapeAsItIs(t *testing.T) {
	tr := startTriggerRig(t, func(c *Config) {
		c.TriggerEventKinds = []string{"event-watcher/k8s-event", "event-watcher/gitops-drift"}
	})
	s := goodScheduleSubmit()
	s.Schedule.Namespace = "team-a"
	s.IdempotencyKey = "schedule/team-a/compliance-audit/1763100000"
	if reply := tr.submit(t, "schedule", s); reply.Refused == nil || reply.Refused.Reason != lib.TriggerRefusedInvalid {
		t.Fatalf("another namespace's schedule = %+v, want invalid", reply)
	}
	if reply := tr.submit(t, "event-watcher", goodScheduleSubmit()); reply.Refused == nil || reply.Refused.Reason != lib.TriggerRefusedInvalid {
		t.Fatalf("a schedule-shaped event submission = %+v, want invalid", reply)
	}
}

// A kind the install doesn't list is refused even from a source it lists
// another kind for, so a source can't open a fresh rate bucket by naming a
// new kind.
func TestAnEventKindTheGatewayDoesNotAdmitIsRefused(t *testing.T) {
	tr := startTriggerRig(t, func(c *Config) {
		c.TriggerEventKinds = []string{"event-watcher/k8s-event"}
		c.TriggerEventRatePerHour = 1
	})
	if reply := tr.submit(t, "event-watcher", goodEventSubmit("k8s-event", "a")); reply.Refused != nil {
		t.Fatalf("the admitted kind refused: %+v", reply.Refused)
	}
	for _, kind := range []string{"a1", "a2", "gitops-drift"} {
		if reply := tr.submit(t, "event-watcher", goodEventSubmit(kind, "b")); reply.Refused == nil || reply.Refused.Reason != lib.TriggerRefusedInvalid {
			t.Fatalf("kind %s = %+v, want invalid", kind, reply)
		}
	}
	if reply := tr.submit(t, "event-watcher", goodEventSubmit("k8s-event", "c")); reply.Refused == nil || reply.Refused.Reason != lib.TriggerRefusedRateLimited {
		t.Fatalf("the admitted kind past its cap = %+v, want rate_limited", reply)
	}
}

// Two runs of one kind keep their own records, each with its own delivery,
// so one run's result is never read against another's.
func TestTwoRunsOfOneEventKindKeepTheirOwnDelivery(t *testing.T) {
	tr := startTriggerRig(t, func(c *Config) { c.TriggerEventKinds = []string{"event-watcher/k8s-event"} })
	a, b := goodEventSubmit("k8s-event", "a"), goodEventSubmit("k8s-event", "b")
	b.Delivery = lib.ScheduleDelivery{Mode: lib.ScheduleDeliveryLocal, JobKey: "other"}
	for _, s := range []lib.TriggerSubmit{a, b} {
		if reply := tr.submit(t, "event-watcher", s); reply.Refused != nil {
			t.Fatalf("refused: %+v", reply.Refused)
		}
	}
	for _, s := range []lib.TriggerSubmit{a, b} {
		rec, err := tr.g.reg.Get(context.Background(), "trigger:"+s.IdempotencyKey)
		if err != nil || rec == nil || rec.Delivery == nil || *rec.Delivery != s.Delivery || len(rec.TriggerRuns) != 1 {
			t.Errorf("record for %s = %+v, %v", s.IdempotencyKey, rec, err)
		}
	}
}
