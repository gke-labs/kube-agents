package controller

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/busmigrate"
)

// Every phase the Reshape leg can reach is one the operator that first held
// unknown legs (#6479) knows, so an operator rolled back to it holds a
// reshape in any phase, with an identity in its frozen map for every pod.
// Walks the step table from every phase in the set, both directions, both
// outcomes, and the run's first phase.
func TestTheReshapeLegUsesOnlyPhasesTheHoldKnows(t *testing.T) {
	if !slices.Contains(busReshapePhases, busMigrationFreezing) {
		t.Fatal("a reshape starts in Freezing, which is not in the set")
	}
	for _, phase := range busReshapePhases {
		for _, abandoning := range []bool{false, true} {
			for _, outcome := range []busMigrationOutcome{busMigrationSucceeded, busMigrationErred, busMigrationPending} {
				next := nextBusMigrationPhase(busMigrationReshape, phase, abandoning, outcome).next
				if !slices.Contains(busReshapePhases, next) {
					t.Errorf("from %s (abandoning %v, outcome %v) the Reshape leg goes to %s, which #6479's operator does not know", phase, abandoning, outcome, next)
				}
			}
		}
	}
	for _, p := range []busMigrationPhase{busMigrationConverting, busMigrationPinning, busMigrationShrinking, busMigrationUnpinning} {
		if slices.Contains(busReshapePhases, p) {
			t.Errorf("%s is in the reshape's phases; a reshape never converts or pins", p)
		}
	}
}

// The forward walk skips the conversion: Clearing goes straight to
// Restoring. A failed restore turns back through the clear and the rollback
// restore, as one to three does.
func TestTheReshapeStepTable(t *testing.T) {
	step := func(phase busMigrationPhase, abandoning, ok bool) busMigrationStep {
		outcome := busMigrationErred
		if ok {
			outcome = busMigrationSucceeded
		}
		return nextBusMigrationPhase(busMigrationReshape, phase, abandoning, outcome)
	}
	for _, c := range []struct {
		phase          busMigrationPhase
		abandoning, ok bool
		next           busMigrationPhase
		nextAbandoning bool
	}{
		{busMigrationFreezing, false, true, busMigrationSnapshot, false},
		{busMigrationSnapshot, false, true, busMigrationHashing, false},
		{busMigrationHashing, false, true, busMigrationClearing, false},
		{busMigrationClearing, false, true, busMigrationRestoring, false},
		{busMigrationRestoring, false, true, busMigrationReleasing, false},
		{busMigrationReleasing, false, true, busMigrationDone, false},
		{busMigrationFreezing, false, false, busMigrationReleasing, true},
		{busMigrationHashing, false, false, busMigrationReleasing, true},
		{busMigrationClearing, false, false, busMigrationFailed, false},
		{busMigrationRestoring, false, false, busMigrationClearing, true},
		{busMigrationClearing, true, true, busMigrationRollingBack, true},
		{busMigrationClearing, true, false, busMigrationFailed, true},
		{busMigrationRollingBack, true, true, busMigrationRollbackRestoring, true},
		{busMigrationRollbackRestoring, true, true, busMigrationReleasing, true},
		{busMigrationRollbackRestoring, true, false, busMigrationFailed, true},
		{busMigrationReleasing, true, true, busMigrationAbandoned, true},
	} {
		got := step(c.phase, c.abandoning, c.ok)
		if got.next != c.next || got.abandoning != c.nextAbandoning {
			t.Errorf("%s (abandoning %v, ok %v) -> %s (abandoning %v), want %s (abandoning %v)", c.phase, c.abandoning, c.ok, got.next, got.abandoning, c.next, c.nextAbandoning)
		}
	}
}

// tasksDigests is a hashing pod's report for a reshape: TASKS alone.
func tasksDigests() string {
	b, _ := json.Marshal(map[string]busmigrate.StagedDigest{"TASKS": {Data: fmt.Sprintf("%064x", 1), Meta: fmt.Sprintf("%064x", 100)}})
	return string(b)
}

// reshapeRig is a migration rig for a reshape at running servers, with the
// CR asking for that count (a reshape never changes it).
func reshapeRig(t *testing.T, phase busMigrationPhase, running int32, deadline time.Time) *migrationRig {
	t.Helper()
	m := newMigrationRig(t, busMigrationReshape, phase, running, deadline)
	m.agent.Spec.Harness.Tuning.BusReplicas = ptr.To(int(running))
	return m
}

// A reshape walks the freeze, the snapshot, the hash, the clear and the
// restore of TASKS alone, at the count the bus runs, one server or three,
// and never renders another count. Its restore pod restores forward at that
// count, named.
func TestAReshapeRunsAtTheCountTheBusRuns(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	for _, n := range []int32{1, 3} {
		m := reshapeRig(t, busMigrationFreezing, n, time.Now().Add(time.Hour))
		m.calloutPods(2, true, true)
		m.pass(busMigrationFreezing, n)
		m.rollOut(n)
		m.pass(busMigrationSnapshot, n)
		if spec, _, ok := m.workload("snapshot"); !ok || !slices.Contains(spec.Containers[0].Command, "TASKS") || slices.Contains(spec.Containers[0].Command, strings.Join(a2aBusMigrationStreams(), ",")) {
			t.Fatalf("at %d: snapshot pod %v, want --streams TASKS", n, spec.Containers)
		}
		m.finishPod("snapshot", corev1.PodSucceeded, "", "")
		m.pass(busMigrationHashing, n)
		m.finishPod("hash", corev1.PodSucceeded, tasksDigests(), "")
		m.pass(busMigrationClearing, n)
		m.snapshotKept()
		b, _ := json.Marshal(busmigrate.Report{Cleared: []string{"TASKS"}})
		m.finishPod(busMigrationRunWork(m.agent.Status.BusMigration).name, corev1.PodSucceeded, string(b), "")
		m.pass(busMigrationRestoring, n)
		spec, _, ok := m.workload("restore")
		if !ok {
			t.Fatalf("at %d: no restore pod", n)
		}
		cmd := strings.Join(spec.Containers[0].Command, " ")
		if !strings.Contains(cmd, "--direction forward") || !strings.Contains(cmd, fmt.Sprintf("--replicas %d", n)) || !strings.Contains(cmd, "--streams TASKS") {
			t.Errorf("at %d: restore command %q, want TASKS forward at %d replicas", n, cmd, n)
		}
		m.finishPod("restore", corev1.PodSucceeded, `{}`, "")
		m.pass(busMigrationReleasing, n)
		// The provision Job that refused the narrow TASKS, failed: its name
		// is the same for the same render, so only its deletion runs it again.
		job := buildA2AProvisionJob(m.agent)
		m.own(job)
		if err := m.cl.Create(context.Background(), job); err != nil {
			t.Fatal(err)
		}
		m.rollOut(n)
		m.pass(busMigrationDone, n)
		if err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(job), &batchv1.Job{}); !apierrors.IsNotFound(err) {
			t.Errorf("at %d: the provision Job after the reshape: %v, want it deleted so a fresh one runs", n, err)
		}
	}
}

// The way back restores TASKS as it was, at the count the bus runs, named
// back.
func TestAReshapesRollbackRestoresBackAtTheSameCount(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	for _, n := range []int32{1, 3} {
		m := reshapeRig(t, busMigrationRollbackRestoring, n, time.Now().Add(time.Hour))
		m.agent.Status.BusMigration.Abandoning = true
		m.agent.Status.BusMigration.Cleared = []string{"TASKS"}
		m.snapshotKept()
		m.pass(busMigrationRollbackRestoring, n)
		spec, _, ok := m.workload("rollback")
		if !ok {
			t.Fatalf("at %d: no rollback pod", n)
		}
		cmd := strings.Join(spec.Containers[0].Command, " ")
		if !strings.Contains(cmd, "--direction back") || !strings.Contains(cmd, fmt.Sprintf("--replicas %d", n)) {
			t.Errorf("at %d: rollback command %q, want back at %d replicas", n, cmd, n)
		}
	}
}

// A hashing report for a reshape names TASKS alone; one that names every
// stream, or misses TASKS, is refused.
func TestAReshapesDigestsAreTasksAlone(t *testing.T) {
	streams := a2aBusMigrationStreamsFor(busMigrationReshape)
	if !slices.Equal(streams, []string{"TASKS"}) {
		t.Fatalf("reshape streams %v, want [TASKS]", streams)
	}
	if _, bad := parseBusDigests(tasksDigests(), streams); bad != "" {
		t.Errorf("a TASKS-only report refused: %s", bad)
	}
	if _, bad := parseBusDigests(validDigests(), streams); bad == "" {
		t.Error("a report naming every stream was accepted for a reshape")
	}
	if _, bad := parseBusDigests(tasksDigests(), a2aBusMigrationStreams()); bad == "" {
		t.Error("a TASKS-only report was accepted for one to three")
	}
}

// reshapeTrigger is a settled, provisioned one-server bus whose live TASKS
// holds width; the CR renders a2aTasksMaxConsumers.
func reshapeTrigger(t *testing.T, width int) *migrationRig {
	t.Helper()
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	m := newMigrationRig(t, busMigrationGrow, busMigrationDone, 1, time.Now().Add(time.Hour))
	m.agent.Spec.Harness.Tuning.BusReplicas = ptr.To(1)
	m.agent.Status.BusMigration = nil
	meta.SetStatusCondition(&m.agent.Status.Conditions, metav1.Condition{Type: busProvisionedConditionType, Status: metav1.ConditionTrue, Reason: "ProvisionJobComplete"})
	m.r.busStreamState = func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
		return map[string]a2aBusStream{"TASKS": {MaxConsumers: width}}, nil
	}
	return m
}

func (m *migrationRig) reshapeEvents() []string {
	var out []string
	for _, e := range drainEvents(m.r.Recorder.(*record.FakeRecorder)) {
		if strings.Contains(e, reasonA2ABusReshapeNotStarted) {
			out = append(out, e)
		}
	}
	return out
}

// The annotation starts a reshape on a provisioned bus whose TASKS is
// narrower than the render, at the count the bus runs, with the request
// recorded in the run's ID, and the run is persisted before the pass goes
// on.
func TestTheReshapeAnnotationStartsAReshape(t *testing.T) {
	m := reshapeTrigger(t, a2aTasksMaxConsumers(a2aTestAgent())-20)
	m.agent.Annotations = map[string]string{a2aBusReshapeAnnotation: "first"}
	m.pass(busMigrationFreezing, 1)
	run := m.agent.Status.BusMigration
	if run.Leg != string(busMigrationReshape) || !strings.HasSuffix(run.RunID, busReshapeRunSuffix("first")) {
		t.Fatalf("run %+v, want a Reshape whose ID ends %s", run, busReshapeRunSuffix("first"))
	}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(m.agent), stored); err != nil {
		t.Fatal(err)
	}
	if stored.Status.BusMigration == nil || stored.Status.BusMigration.RunID != run.RunID {
		t.Errorf("stored run %+v, want the started run persisted", stored.Status.BusMigration)
	}
	// The next pass, with the same value still set, reports nothing: the
	// run under way answers it.
	m.pass(busMigrationFreezing, 1)
	if ev := m.reshapeEvents(); len(ev) != 0 {
		t.Errorf("events %q on the pass after the start, want none", ev)
	}
}

// Each guard refuses with a Normal Event naming why, once per request and
// reason, and starts nothing.
func TestTheReshapeGuards(t *testing.T) {
	rendered := a2aTasksMaxConsumers(a2aTestAgent())
	for _, c := range []struct {
		name string
		set  func(t *testing.T, m *migrationRig)
		want string
	}{
		{"wide enough", func(t *testing.T, m *migrationRig) {
			m.r.busStreamState = func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
				return map[string]a2aBusStream{"TASKS": {MaxConsumers: rendered}}, nil
			}
		}, "already holds max_consumers"},
		{"no limit", func(t *testing.T, m *migrationRig) {
			m.r.busStreamState = func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
				return map[string]a2aBusStream{"TASKS": {MaxConsumers: -1}}, nil
			}
		}, "already holds max_consumers -1"},
		{"not provisioned", func(t *testing.T, m *migrationRig) {
			meta.RemoveStatusCondition(&m.agent.Status.Conditions, busProvisionedConditionType)
		}, "not provisioned"},
		{"unreadable", func(t *testing.T, m *migrationRig) {
			m.r.busStreamState = func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
				return nil, errors.New("monitor down")
			}
		}, "could not be read (monitor down)"},
		{"no TASKS", func(t *testing.T, m *migrationRig) {
			m.r.busStreamState = func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
				return map[string]a2aBusStream{"DIRECTORY": {}}, nil
			}
		}, "no TASKS stream"},
		{"over cap", func(t *testing.T, m *migrationRig) {
			m.r.busStreamState = func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
				return map[string]a2aBusStream{"TASKS": {MaxConsumers: rendered - 20, Bytes: a2aTasksMaxBytes + 1}}, nil
			}
		}, "TASKS holds"},
		{"failed run waits", func(t *testing.T, m *migrationRig) {
			m.agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationGrow), RunID: "2-1", Phase: string(busMigrationFailed), FailedIn: string(busMigrationRollbackRestoring)}
		}, "waits for a person"},
		{"no operator image", func(t *testing.T, m *migrationRig) {
			t.Setenv(operatorImageEnvVar, "")
		}, "without OPERATOR_IMAGE"},
		{"a run under way", func(t *testing.T, m *migrationRig) {
			m.agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationGrow), RunID: "2-1", Phase: string(busMigrationRestoring), Deadline: metav1.NewTime(time.Now().Add(time.Hour))}
		}, "under way"},
		{"width unreported", func(t *testing.T, m *migrationRig) {
			m.r.busStreamState = func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
				return map[string]a2aBusStream{"TASKS": {}}, nil
			}
		}, "did not report"},
		{"CRD drops the run", func(t *testing.T, m *migrationRig) {
			m.r.prunedBusMigrationStatus.Store(client.ObjectKeyFromObject(m.agent), true)
		}, "drops status.busMigration"},
	} {
		t.Run(c.name, func(t *testing.T) {
			m := reshapeTrigger(t, rendered-20)
			m.agent.Annotations = map[string]string{a2aBusReshapeAnnotation: "n1"}
			c.set(t, m)
			before := m.agent.Status.BusMigration
			for i := 0; i < 2; i++ {
				if err := m.r.resolveA2ABusReplicas(context.Background(), m.agent); err != nil {
					t.Fatal(err)
				}
			}
			if run := m.agent.Status.BusMigration; run != nil && run.Leg == string(busMigrationReshape) {
				t.Errorf("a reshape started: %+v", run)
			}
			if before != nil && (m.agent.Status.BusMigration == nil || m.agent.Status.BusMigration.RunID != before.RunID) {
				t.Errorf("the run %s was replaced: %+v", before.RunID, m.agent.Status.BusMigration)
			}
			events := m.reshapeEvents()
			if len(events) != 1 || !strings.Contains(events[0], c.want) || !strings.Contains(events[0], "Normal") {
				t.Errorf("events %q, want one Normal naming %q", events, c.want)
			}
		})
	}
}

// A request already answered starts nothing, by its record in the run's ID;
// and one left in place after that record is gone (a later run replaced
// it) starts nothing either, because TASKS is at the width: the width guard
// is the primary one.
func TestAReshapeRequestLeftInPlaceStartsNoSecondReshape(t *testing.T) {
	rendered := a2aTasksMaxConsumers(a2aTestAgent())

	// Answered, TASKS still narrow (the run was abandoned): the record in
	// the run's ID stops a second start on the same nonce.
	m := reshapeTrigger(t, rendered-20)
	m.agent.Annotations = map[string]string{a2aBusReshapeAnnotation: "n1"}
	m.agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationReshape), RunID: "3" + busReshapeRunSuffix("n1"), Phase: string(busMigrationAbandoned)}
	m.pass(busMigrationAbandoned, 1)
	if len(m.reshapeEvents()) != 0 {
		t.Error("an answered request was reported as refused")
	}
	// A new nonce on the same narrow TASKS starts one.
	m.agent.Annotations[a2aBusReshapeAnnotation] = "n2"
	m.pass(busMigrationFreezing, 1)

	// Done, then a later run replaced the record: the stale nonce finds
	// TASKS at the width, and starts nothing.
	m = reshapeTrigger(t, rendered)
	m.agent.Annotations = map[string]string{a2aBusReshapeAnnotation: "n1"}
	m.agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationShrink), RunID: "9-1", Phase: string(busMigrationDone)}
	m.pass(busMigrationDone, 1)
	if ev := m.reshapeEvents(); len(ev) != 1 || !strings.Contains(ev[0], "already holds") {
		t.Errorf("events %q, want the width guard's", ev)
	}
}

// A reshape's server count is the bus's: a terminal reshape gates no later
// count change, and its condition names TASKS, not a server count.
func TestAReshapeHasNoTargetCount(t *testing.T) {
	agent := a2aTestAgent()
	agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationReshape), RunID: "3-rabc", Phase: string(busMigrationDone)}
	for _, want := range []int{1, 3} {
		if !busMigrationWouldStart(agent, want) {
			t.Errorf("after a reshape, a change to %d starts nothing", want)
		}
	}
	agent.Status.BusMigration.Phase = string(busMigrationRestoring)
	cond, ok := busMigratingCondition(agent)
	if !ok || cond.Status != metav1.ConditionTrue || !strings.Contains(cond.Message, "TASKS") || strings.Contains(cond.Message, "NATS servers to") {
		t.Errorf("condition %+v (%v), want True naming TASKS", cond, ok)
	}
}

// A reshape's bus principal is granted TASKS alone, in every phase that has
// one; one to three's is granted every provisioned stream, as before.
func TestAReshapesPrincipalReachesTasksAlone(t *testing.T) {
	for _, phase := range []busMigrationPhase{busMigrationSnapshot, busMigrationClearing, busMigrationRestoring, busMigrationRollbackRestoring} {
		agent := a2aTestAgent()
		agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationReshape), RunID: "3-rabc", Phase: string(phase)}
		ids := busMigrationIdentities(agent)
		if len(ids) == 0 {
			t.Fatalf("%s: no migration identity", phase)
		}
		for _, id := range ids {
			for _, subject := range id.publish {
				if strings.Contains(subject, "STREAM.") && !strings.Contains(subject, ".TASKS") {
					t.Errorf("%s: the reshape's principal may publish %s", phase, subject)
				}
			}
		}
		agent.Status.BusMigration.Leg = string(busMigrationGrow)
		var all string
		for _, id := range busMigrationIdentities(agent) {
			all += strings.Join(id.publish, " ")
		}
		if !strings.Contains(all, ".DIRECTORY") {
			t.Errorf("%s: one to three's principal lost DIRECTORY: %s", phase, all)
		}
	}
}

// A bus whose monitor fails differently on each pass is one reason, so one
// Event, not one per error text.
func TestAnUnreadableBusIsOneReshapeEvent(t *testing.T) {
	m := reshapeTrigger(t, 1)
	m.agent.Annotations = map[string]string{a2aBusReshapeAnnotation: "n1"}
	pass := 0
	m.r.busStreamState = func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
		pass++
		return nil, fmt.Errorf("dial tcp: i/o timeout after %d ms", 1000+pass)
	}
	for i := 0; i < 3; i++ {
		if err := m.r.resolveA2ABusReplicas(context.Background(), m.agent); err != nil {
			t.Fatal(err)
		}
	}
	if ev := m.reshapeEvents(); len(ev) != 1 {
		t.Errorf("%d Events over three passes with varying errors, want 1: %q", len(ev), ev)
	}
}

// stsReshapeRecord reads the bus StatefulSet's consumed-request record.
func (m *migrationRig) stsReshapeRecord() string {
	m.t.Helper()
	sts := &appsv1.StatefulSet{}
	if err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(m.sts), sts); err != nil {
		m.t.Fatal(err)
	}
	return sts.Annotations[a2aBusReshapeConsumedAnnotation]
}

// Starting a reshape records its request on the bus StatefulSet, and a pass
// that finds a reshape run without that record writes it.
func TestAReshapeRecordsItsRequestOnTheStatefulSet(t *testing.T) {
	m := reshapeTrigger(t, a2aTasksMaxConsumers(a2aTestAgent())-20)
	m.agent.Annotations = map[string]string{a2aBusReshapeAnnotation: "first"}
	m.pass(busMigrationFreezing, 1)
	if got := m.stsReshapeRecord(); got != busReshapeNonceHash("first") {
		t.Fatalf("StatefulSet record %q after the start, want %q", got, busReshapeNonceHash("first"))
	}
	// Lost (a failed write after the start): the next pass writes it.
	sts := &appsv1.StatefulSet{}
	if err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(m.sts), sts); err != nil {
		t.Fatal(err)
	}
	delete(sts.Annotations, a2aBusReshapeConsumedAnnotation)
	if err := m.cl.Update(context.Background(), sts); err != nil {
		t.Fatal(err)
	}
	m.pass(busMigrationFreezing, 1)
	if got := m.stsReshapeRecord(); got != busReshapeNonceHash("first") {
		t.Errorf("StatefulSet record %q after a pass over the run, want it written back", got)
	}
}

// The case the width guard alone misses: a reshape answered a request, a
// later run that left TASKS as it was replaced its record in status, and
// the render then grew past TASKS. The value still on the CR starts
// nothing: the StatefulSet's record names it. A new value starts one.
func TestAStaleReshapeRequestAfterALaterRunAndAWiderRenderStartsNothing(t *testing.T) {
	rendered := a2aTasksMaxConsumers(a2aTestAgent())
	m := reshapeTrigger(t, rendered-20) // narrower than the render again
	m.agent.Annotations = map[string]string{a2aBusReshapeAnnotation: "old"}
	sts := &appsv1.StatefulSet{}
	if err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(m.sts), sts); err != nil {
		t.Fatal(err)
	}
	if sts.Annotations == nil {
		sts.Annotations = map[string]string{}
	}
	sts.Annotations[a2aBusReshapeConsumedAnnotation] = busReshapeNonceHash("old")
	if err := m.cl.Update(context.Background(), sts); err != nil {
		t.Fatal(err)
	}
	m.agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationShrink), RunID: "9-1", Phase: string(busMigrationDone)}
	for i := 0; i < 2; i++ {
		m.pass(busMigrationDone, 1)
	}
	if ev := m.reshapeEvents(); len(ev) != 0 {
		t.Errorf("events %q for a request already started, want none", ev)
	}
	m.agent.Annotations[a2aBusReshapeAnnotation] = "new"
	m.pass(busMigrationFreezing, 1)
	if run := m.agent.Status.BusMigration; run.Leg != string(busMigrationReshape) {
		t.Errorf("a new value started %+v, want a reshape", run)
	}
}

// A deleted CR's reshape and hold records go with it, so a CR recreated
// under the same name gets its Events afresh.
func TestTheReshapeRecordsAreForgottenWithTheCR(t *testing.T) {
	r := &PlatformAgentReconciler{}
	agent := a2aTestAgent()
	key := client.ObjectKeyFromObject(agent)
	r.busReshapeRefused.Store(key, "n1/wide")
	r.heldBusMigrationWarned.Store(key, "3-1/Freezing")
	r.forgetUsageStatus(agent)
	if _, ok := r.busReshapeRefused.Load(key); ok {
		t.Error("the reshape refusal record outlived the CR")
	}
	if _, ok := r.heldBusMigrationWarned.Load(key); ok {
		t.Error("the hold Warning record outlived the CR")
	}
}

// The reshape counts a provision Job failed by its podFailurePolicy (exit 2)
// as provisioned, because exit 2 is only the closing max_consumers gate
// (busProvisionRefusedOnlyForWidth). The rendered script must keep it so:
// exactly one `exit 2`, inside the block that compares the live TASKS width
// with the required one, and no exit taking its status from a variable.
func TestTheProvisionScriptExitsTwoOnlyAtTheConsumerGate(t *testing.T) {
	script := buildA2AProvisionJob(a2aTestAgent()).Spec.Template.Spec.Containers[0].Command[2]
	lines := strings.Split(script, "\n")
	var exits []int
	for i, l := range lines {
		f := strings.Fields(l)
		for j := 0; j+1 < len(f); j++ {
			if f[j] == "exit" && (f[j+1] == "2" || strings.HasPrefix(f[j+1], "$") || strings.HasPrefix(f[j+1], `"$`)) {
				exits = append(exits, i)
			}
		}
	}
	if len(exits) != 1 {
		t.Fatalf("%d exits with status 2 or a variable status in the provision script, want exactly 1 (the max_consumers gate): lines %v", len(exits), exits)
	}
	// The innermost if still open at the exit is the consumer gate.
	depth, gate := 0, ""
	for i := exits[0]; i >= 0; i-- {
		l := strings.TrimSpace(lines[i])
		switch {
		case l == "fi":
			depth++
		case strings.HasPrefix(l, "if "):
			if depth == 0 {
				gate = l
				i = -1
			} else {
				depth--
			}
		}
	}
	if !strings.Contains(gate, "live_consumers") || !strings.Contains(gate, "required_consumers") {
		t.Errorf("the provision script's exit 2 sits under %q, want the live-vs-required max_consumers gate", gate)
	}
}

// A reshape given up before its clear drops BusProvisioned, and the
// provision Job that runs next refuses the narrow TASKS at its consumer
// gate (failed by its podFailurePolicy). That bus counts as provisioned for
// a reshape, so a new request starts one; a Job failed any other way (its
// backoff spent) still refuses as not provisioned (#6598).
func TestAReshapeStartsOnABusRefusedOnlyForWidth(t *testing.T) {
	for _, c := range []struct {
		reason string
		starts bool
	}{{batchv1.JobReasonPodFailurePolicy, true}, {batchv1.JobReasonBackoffLimitExceeded, false}} {
		m := reshapeTrigger(t, a2aTasksMaxConsumers(a2aTestAgent())-20)
		meta.RemoveStatusCondition(&m.agent.Status.Conditions, busProvisionedConditionType)
		m.agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationReshape), RunID: "3" + busReshapeRunSuffix("first"), Phase: string(busMigrationFailed), FailedIn: string(busMigrationHashing), GivenUp: true}
		job := buildA2AProvisionJob(m.agent)
		m.own(job)
		if err := m.cl.Create(context.Background(), job); err != nil {
			t.Fatal(err)
		}
		job.Status.Conditions = []batchv1.JobCondition{{Type: batchv1.JobFailed, Status: corev1.ConditionTrue, Reason: c.reason}}
		if err := m.cl.Status().Update(context.Background(), job); err != nil {
			t.Fatal(err)
		}
		m.agent.Annotations = map[string]string{a2aBusReshapeAnnotation: "second"}
		if err := m.r.resolveA2ABusReplicas(context.Background(), m.agent); err != nil {
			t.Fatal(err)
		}
		run := m.agent.Status.BusMigration
		started := run.Leg == string(busMigrationReshape) && strings.HasSuffix(run.RunID, busReshapeRunSuffix("second"))
		if started != c.starts {
			t.Errorf("provision Job failed with %s: reshape started %v, want %v (run %+v, events %q)", c.reason, started, c.starts, run, m.reshapeEvents())
		}
	}
}
