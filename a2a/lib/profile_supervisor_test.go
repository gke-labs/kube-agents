package lib

import (
	"slices"
	"testing"

	"github.com/nats-io/nats.go/jetstream"
)

// The profile supervisor form parses to its profile and task, is a supervisor
// subject for that task, and can't be confused with a five-token subject,
// whatever the addressee is named (gke-labs/kube-agents#5301).
func TestTheProfileSupervisorForm(t *testing.T) {
	subject := ProfileSupervisorSubject("fleet-audit", "task-1")
	if subject != "a2a.tasks.p.fleet-audit.task-1.supervisor" {
		t.Fatalf("ProfileSupervisorSubject = %q", subject)
	}
	addressee, taskID, class, ok := ParseTaskSubject(subject)
	if !ok || addressee != "fleet-audit" || taskID != "task-1" || class != TaskClassSupervisor {
		t.Fatalf("ParseTaskSubject(%q) = %q %q %q %v", subject, addressee, taskID, class, ok)
	}
	if !IsTaskSupervisorSubject(subject, "fleet-audit", "task-1") || !IsTaskSupervisorSubject(TaskSupervisorSubject("fleet-audit", "task-1"), "fleet-audit", "task-1") {
		t.Fatal("either form is the task's supervisor subject")
	}
	if IsTaskSupervisorSubject(subject, "fleet-audit", "task-2") || IsTaskSupervisorSubject(TaskEventsSubject("fleet-audit", "task-1"), "fleet-audit", "task-1") {
		t.Fatal("another task's, or the events subject, is not")
	}
	// A five-token subject whose addressee is "p" stays five-token.
	if a, id, c, ok := ParseTaskSubject("a2a.tasks.p.task-1.events"); !ok || a != "p" || id != "task-1" || c != TaskClassEvents {
		t.Fatalf("a five-token subject for addressee p = %q %q %q %v", a, id, c, ok)
	}
	for _, bad := range []string{"a2a.tasks.p.fleet-audit.task-1.events", "a2a.tasks.p..task-1.supervisor", "a2a.tasks.q.fleet-audit.task-1.supervisor"} {
		if _, _, _, ok := ParseTaskSubject(bad); ok {
			t.Errorf("ParseTaskSubject(%q) accepted it", bad)
		}
	}
	if got := TaskReplaySubjects("fleet-audit", "task-1"); !slices.Contains(got, subject) || len(got) != 3 {
		t.Fatalf("TaskReplaySubjects = %v, want the events and both supervisor forms", got)
	}
}

// A pre-created durable binds with its own filter set or an accepted one,
// and nothing else.
func TestAPrecreatedDurableBindsAnAcceptedFilterSet(t *testing.T) {
	cfg := SubscribeConfig{
		Durable: "relay", Stream: TasksStream,
		Subjects:         []string{"a2a.tasks.*.*.events", "a2a.tasks.*.*.supervisor"},
		AcceptFilterSets: [][]string{{"a2a.tasks.*.*.events", "a2a.tasks.*.*.supervisor", "a2a.tasks.p.*.*.supervisor"}},
	}
	durable := func(filters ...string) jetstream.ConsumerConfig {
		return jetstream.ConsumerConfig{FilterSubjects: filters, AckPolicy: jetstream.AckExplicitPolicy,
			DeliverPolicy: jetstream.DeliverAllPolicy, ReplayPolicy: jetstream.ReplayInstantPolicy}
	}
	for _, ok := range [][]string{
		{"a2a.tasks.*.*.supervisor", "a2a.tasks.*.*.events"},
		{"a2a.tasks.p.*.*.supervisor", "a2a.tasks.*.*.events", "a2a.tasks.*.*.supervisor"},
	} {
		if err := cfg.checkPrecreated(durable(ok...)); err != nil {
			t.Errorf("filters %v refused: %v", ok, err)
		}
	}
	if err := cfg.checkPrecreated(durable("a2a.tasks.*.*.events")); err == nil {
		t.Error("a durable with a filter set neither named nor accepted was bound")
	}
}
