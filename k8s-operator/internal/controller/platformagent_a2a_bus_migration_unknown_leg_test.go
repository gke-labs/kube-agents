package controller

import (
	"context"
	"strings"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Every phase a run can be in that is not over.
var busMigrationLivePhases = []busMigrationPhase{
	busMigrationPinning, busMigrationShrinking, busMigrationUnpinning,
	busMigrationFreezing, busMigrationSnapshot, busMigrationHashing, busMigrationClearing,
	busMigrationConverting, busMigrationRestoring, busMigrationRollingBack,
	busMigrationRollbackRestoring, busMigrationReleasing,
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
			for _, phase := range busMigrationLivePhases {
				m := newMigrationRig(t, leg, phase, running, time.Now().Add(-time.Hour))
				m.agent.Status.BusMigration.FreezeConfirmed = phase != busMigrationFreezing
				before := *m.agent.Status.BusMigration.DeepCopy()
				for i := 0; i < 3; i++ {
					m.pass(phase, running)
				}
				run := m.agent.Status.BusMigration
				if run.Phase != before.Phase || run.FailedIn != "" || run.Cause != "" || run.Abandoning || !run.Deadline.Equal(&before.Deadline) {
					t.Errorf("%s %s at %d: run moved: %+v", leg, phase, running, run)
				}
				if n := m.workloadCount(); n != 0 {
					t.Errorf("%s %s at %d: %d pods or Jobs made", leg, phase, running, n)
				}
				if !busMigrationHoldsProvision(m.agent) {
					t.Errorf("%s %s at %d: the provision Job is not held", leg, phase, running)
				}
				if wantFrozen := phase != busMigrationReleasing; busMigrationFreezeActive(m.agent) != wantFrozen {
					t.Errorf("%s %s at %d: freeze active %v, want %v", leg, phase, running, busMigrationFreezeActive(m.agent), wantFrozen)
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
	if cond.Reason != "Failed" || strings.Contains(cond.Message, "To retry") || !strings.Contains(cond.Message, "recreated empty") {
		t.Errorf("condition %+v, want Failed, no retry, streams recreated", cond)
	}
}

// A Failed run of an unknown leg offers and takes no retry.
func TestAFailedUnknownLegTakesNoRetry(t *testing.T) {
	m := newMigrationRig(t, "Reshape", busMigrationFailed, 3, time.Now().Add(time.Hour))
	run := m.agent.Status.BusMigration
	run.FailedIn = string(busMigrationRestoring)
	m.agent.Annotations = map[string]string{a2aBusMigrationRetryAnnotation: busMigrationRetryValue(run)}
	if busMigrationActOnFailed(m.agent, metav1.Now()) || run.Phase != string(busMigrationFailed) || len(run.Retries) != 0 {
		t.Errorf("phase %s, retries %d: the retry ran", run.Phase, len(run.Retries))
	}
	if strings.Contains(busMigrationFailedActions(m.agent), "To retry") {
		t.Error("the condition offers a retry")
	}
}
