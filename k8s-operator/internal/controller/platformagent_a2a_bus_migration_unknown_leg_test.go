package controller

import (
	"context"
	"reflect"
	"strings"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Every phase a run can be in that is not over.
var busMigrationLivePhases = []busMigrationPhase{
	busMigrationPinning, busMigrationShrinking, busMigrationUnpinning,
	busMigrationFreezing, busMigrationSnapshot, busMigrationHashing, busMigrationClearing,
	busMigrationConverting, busMigrationRestoring, busMigrationRollingBack,
	busMigrationRollbackRestoring, busMigrationReleasing,
}

// seedWorkloads puts a pod and a Job of the run's on the rig, as a newer
// operator would have left them, and returns how many objects that is.
func (m *migrationRig) seedWorkloads() int {
	m.t.Helper()
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: busMigrationWorkloadName(m.agent, "reshape", "3-1"), Namespace: m.agent.Namespace}}
	job := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: busMigrationWorkloadName(m.agent, "reshape-job", "3-1"), Namespace: m.agent.Namespace}}
	for _, obj := range []client.Object{pod, job} {
		m.own(obj)
		if err := m.cl.Create(context.Background(), obj); err != nil {
			m.t.Fatal(err)
		}
	}
	return 2
}

// workloadCount is how many pods and Jobs the run has on the rig.
func (m *migrationRig) workloadCount() int {
	m.t.Helper()
	pods, jobs := &corev1.PodList{}, &batchv1.JobList{}
	if err := m.cl.List(context.Background(), pods, client.InNamespace(m.agent.Namespace)); err != nil {
		m.t.Fatal(err)
	}
	if err := m.cl.List(context.Background(), jobs, client.InNamespace(m.agent.Namespace)); err != nil {
		m.t.Fatal(err)
	}
	return len(pods.Items) + len(jobs.Items)
}

// A run whose leg this operator does not run (a newer release's, found
// after a rollback) is held in every phase, the deadline past or not: the
// phase does not move, no pod or Job is made, the bus keeps the count it
// runs (one or three), the provision Job is held, the freeze holds in every
// phase that has one, and one Warning names the leg and both ways out.
func TestAnUnknownLegIsHeldInEveryPhase(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	for _, leg := range []busMigrationLeg{"Reshape", "Sideways"} {
		for _, running := range []int32{1, 3} {
			for _, phase := range append(append([]busMigrationPhase{}, busMigrationLivePhases...), "Reshaping") {
				m := newMigrationRig(t, leg, phase, running, time.Now().Add(-time.Hour))
				m.agent.Status.BusMigration.FreezeConfirmed = phase != busMigrationFreezing
				seeded := m.seedWorkloads()
				before := m.agent.Status.BusMigration.DeepCopy()
				for i := 0; i < 3; i++ {
					m.pass(phase, running)
				}
				if run := m.agent.Status.BusMigration; !reflect.DeepEqual(run, before) {
					t.Errorf("%s %s at %d: the hold wrote to the run:\n %+v\nwas\n %+v", leg, phase, running, run, before)
				}
				if n := m.workloadCount(); n != seeded {
					t.Errorf("%s %s at %d: %d pods and Jobs, want the %d already there", leg, phase, running, n, seeded)
				}
				if !busMigrationHoldsProvision(m.agent) {
					t.Errorf("%s %s at %d: the provision Job is not held", leg, phase, running)
				}
				if wantFrozen := phase != busMigrationReleasing; busMigrationFreezeActive(m.agent) != wantFrozen {
					t.Errorf("%s %s at %d: freeze active %v, want %v", leg, phase, running, busMigrationFreezeActive(m.agent), wantFrozen)
				}
				// The freeze as rendered: the bus's template keeps the run's
				// freeze annotation and the callout map holds only the
				// migration's identities.
				if wantFrozen := phase != busMigrationReleasing && phase != busMigrationFreezing; (buildA2ANATSStatefulSet(m.agent, "h").Spec.Template.Annotations[a2aBusFreezeAnnotation] == "3-1") != wantFrozen {
					t.Errorf("%s %s at %d: bus template freeze annotation %q, want frozen %v", leg, phase, running, buildA2ANATSStatefulSet(m.agent, "h").Spec.Template.Annotations[a2aBusFreezeAnnotation], wantFrozen)
				}
				events := drainEvents(m.r.Recorder.(*record.FakeRecorder))
				if countEvents(events, reasonA2ABusMigrationLegUnknown) != 1 {
					t.Errorf("%s %s at %d: events %v, want one %s", leg, phase, running, events, reasonA2ABusMigrationLegUnknown)
				}
				msg := strings.Join(events, "\n")
				for _, want := range []string{"unrecognised migration leg " + string(leg), "holding", "Upgrade the operator", a2aBusMigrationAbandonAnnotation + "=3-1"} {
					if !strings.Contains(msg, want) {
						t.Errorf("%s %s at %d: Warning %q does not say %q", leg, phase, running, msg, want)
					}
				}
				cond, ok := busMigratingCondition(m.agent)
				if !ok || cond.Status != metav1.ConditionTrue || cond.Reason != "LegUnknown" {
					t.Errorf("%s %s at %d: condition %+v (%v), want True LegUnknown", leg, phase, running, cond, ok)
				}
			}
		}
	}
}

// The known legs read as they always did: three to one never freezes, one
// to three freezes in every phase but Done and Abandoned, and a run of
// either is not held.
func TestTheKnownLegsAreNotHeld(t *testing.T) {
	all := append(append([]busMigrationPhase{}, busMigrationLivePhases...), busMigrationDone, busMigrationAbandoned, busMigrationFailed)
	for _, phase := range all {
		if busMigrationFrozen(busMigrationShrink, phase) {
			t.Errorf("ThreeToOne frozen in %s", phase)
		}
		if want := phase != busMigrationDone && phase != busMigrationAbandoned; busMigrationFrozen(busMigrationGrow, phase) != want {
			t.Errorf("OneToThree frozen in %s: %v, want %v", phase, !want, want)
		}
	}
	if !busMigrationLegKnown(busMigrationGrow) || !busMigrationLegKnown(busMigrationShrink) || busMigrationLegKnown("Reshape") || busMigrationLegKnown("") {
		t.Error("busMigrationLegKnown misreads a leg")
	}
	// A known leg's pass is the run's own, not the hold: one to three in
	// Freezing with the callout confirmed moves on, and no hold Warning.
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	m := newMigrationRig(t, busMigrationGrow, busMigrationHashing, 1, time.Now().Add(time.Hour))
	m.pass(busMigrationHashing, 1)
	if n := countEvents(drainEvents(m.r.Recorder.(*record.FakeRecorder)), reasonA2ABusMigrationLegUnknown); n != 0 {
		t.Errorf("%d hold Warnings on a known leg", n)
	}
	if m.workloadCount() == 0 {
		t.Error("the known leg's Hashing made no hashing pod: its pass did not run")
	}
}

// The abandon annotation is the way out of a held run: the next pass moves
// it to Failed, the one after gives it up, which lifts the freeze, drops the
// provisioned record and the provision Job so missing streams are recreated,
// and offers no retry: only the leg's own operator knows its way back.
func TestAHeldUnknownLegIsGivenUpByTheAbandonAnnotation(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	m := newMigrationRig(t, "Reshape", busMigrationRestoring, 3, time.Now().Add(time.Hour))
	m.agent.Status.BusMigration.Cleared = []string{"TASKS"}
	meta.SetStatusCondition(&m.agent.Status.Conditions, metav1.Condition{Type: busProvisionedConditionType, Status: metav1.ConditionTrue, Reason: "ProvisionJobComplete"})
	m.pass(busMigrationRestoring, 3)

	// A retry annotation does nothing to a held run, nor to it once Failed.
	m.agent.Annotations = map[string]string{a2aBusMigrationRetryAnnotation: "3-1-1"}
	m.pass(busMigrationRestoring, 3)

	m.agent.Annotations[a2aBusMigrationAbandonAnnotation] = "3-1"
	m.pass(busMigrationFailed, 3)
	run := m.agent.Status.BusMigration
	if run.FailedIn != string(busMigrationRestoring) || run.GivenUp {
		t.Fatalf("after the annotation: failed in %q, given up %v; want Restoring, not yet", run.FailedIn, run.GivenUp)
	}
	m.pass(busMigrationFailed, 3)
	if !run.GivenUp || len(run.Retries) != 0 {
		t.Fatalf("given up %v, retries %d; want given up, none", run.GivenUp, len(run.Retries))
	}
	if busMigrationFreezeActive(m.agent) || busMigrationHoldsProvision(m.agent) {
		t.Error("a given-up run still holds the freeze or the provision Job")
	}
	if meta.FindStatusCondition(m.agent.Status.Conditions, busProvisionedConditionType) != nil {
		t.Error("the provisioned record stands, so the provision Job would not recreate what the run did not put back")
	}
	cond, _ := busMigratingCondition(m.agent)
	if cond.Reason != "Failed" || strings.Contains(cond.Message, "To retry") || !strings.Contains(cond.Message, "recreated empty") || !strings.Contains(cond.Message, busMigrationStageClaim(m.agent, "3-1")) {
		t.Errorf("condition %+v, want Failed, no retry, streams recreated, and the staging claim kept as their only copy", cond)
	}
}

// A Failed run of an unknown leg offers and takes no retry, even from a
// phase a known leg retries from; the same run on the known leg does, so
// the test can tell.
func TestAFailedUnknownLegTakesNoRetry(t *testing.T) {
	for _, failedIn := range []busMigrationPhase{busMigrationRollingBack, busMigrationRollbackRestoring, busMigrationClearing, busMigrationUnpinning} {
		for _, leg := range []busMigrationLeg{busMigrationGrow, "Reshape"} {
			m := newMigrationRig(t, leg, busMigrationFailed, 3, time.Now().Add(time.Hour))
			run := m.agent.Status.BusMigration
			run.FailedIn = string(failedIn)
			m.agent.Annotations = map[string]string{a2aBusMigrationRetryAnnotation: busMigrationRetryValue(run)}
			offered := strings.Contains(busMigrationFailedActions(m.agent), "To retry")
			busMigrationActOnFailed(m.agent, metav1.Now())
			retried := run.Phase != string(busMigrationFailed)
			if want := leg == busMigrationGrow; offered != want || retried != want {
				t.Errorf("%s failed in %s: retry offered %v, ran %v; want %v", leg, failedIn, offered, retried, want)
			}
		}
	}
}

// A held run given up before it cleared anything says its streams are
// recreated empty where missing, as one to three does, not three to one's
// "keep their data".
func TestAnUnknownLegGivenUpEarlySaysStreamsAreRecreated(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	m := newMigrationRig(t, "Reshape", busMigrationHashing, 1, time.Now().Add(time.Hour))
	m.agent.Annotations = map[string]string{a2aBusMigrationAbandonAnnotation: "3-1"}
	m.pass(busMigrationFailed, 1)
	m.pass(busMigrationFailed, 1)
	if got := busMigrationFailedActions(m.agent); !strings.Contains(got, "recreated empty") || strings.Contains(got, "keep their data") {
		t.Errorf("given-up actions %q, want streams recreated empty", got)
	}
}

// A new phase of the held run is a new Warning; the same phase on later
// passes is not.
func TestTheHoldWarnsOncePerPhase(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	m := newMigrationRig(t, "Reshape", busMigrationSnapshot, 1, time.Now().Add(time.Hour))
	m.pass(busMigrationSnapshot, 1)
	m.pass(busMigrationSnapshot, 1)
	m.agent.Status.BusMigration.Phase = string(busMigrationHashing) // the newer operator moved it on, then was rolled back again
	m.pass(busMigrationHashing, 1)
	m.pass(busMigrationHashing, 1)
	if n := countEvents(drainEvents(m.r.Recorder.(*record.FakeRecorder)), reasonA2ABusMigrationLegUnknown); n != 2 {
		t.Errorf("%d hold Warnings over two phases, want 2", n)
	}
}

// With no StatefulSet to read the count from, the hold keeps the count the
// last pass recorded.
func TestTheHoldKeepsTheRecordedCountWithNoStatefulSet(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	m := newMigrationRig(t, "Reshape", busMigrationRestoring, 3, time.Now().Add(time.Hour))
	if err := m.cl.Delete(context.Background(), m.sts); err != nil {
		t.Fatal(err)
	}
	m.pass(busMigrationRestoring, 3)
}

// After a run of an unknown leg ends, any count asked for starts a run: the
// operator can't read that run's target, and a shrink read into it would
// ignore a request for one server.
func TestAnEndedUnknownLegDoesNotGateTheNextRun(t *testing.T) {
	for _, phase := range []busMigrationPhase{busMigrationDone, busMigrationAbandoned} {
		for _, want := range []int{1, 3} {
			agent := a2aTestAgent()
			agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: "Reshape", RunID: "3-1", Phase: string(phase)}
			if !busMigrationWouldStart(agent, want) {
				t.Errorf("after a %s Reshape run, a change to %d starts nothing", phase, want)
			}
		}
	}
	// A known leg still gates as before: one to three Done, asked for
	// three again, starts nothing.
	agent := a2aTestAgent()
	agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationGrow), RunID: "3-1", Phase: string(busMigrationDone)}
	if busMigrationWouldStart(agent, 3) {
		t.Error("a Done one to three run, asked for three again, starts another")
	}
}

// An unknown leg that ended Abandoned says so, as a known leg's does.
func TestAnAbandonedUnknownLegHasACondition(t *testing.T) {
	agent := a2aTestAgent()
	agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: "Reshape", RunID: "3-1", Phase: string(busMigrationAbandoned), Cause: "the Restoring phase failed"}
	cond, ok := busMigratingCondition(agent)
	if !ok || cond.Status != metav1.ConditionFalse || cond.Reason != "Abandoned" || !strings.Contains(cond.Message, "Reshape") {
		t.Errorf("condition %+v (%v), want False Abandoned naming the leg", cond, ok)
	}
	agent.Status.BusMigration.Phase = string(busMigrationDone)
	if _, ok := busMigratingCondition(agent); ok {
		t.Error("a Done unknown-leg run keeps a condition; a Done known leg has none")
	}
}
