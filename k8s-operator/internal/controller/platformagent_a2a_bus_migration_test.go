package controller

import (
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
)

// walkBusMigration runs a migration from its first phase, failing the phase
// named failAt (and, when failAgain is set, the first phase of the way back
// too), and returns every phase it passed through and the last step.
func walkBusMigration(leg busMigrationLeg, failAt busMigrationPhase, failAgain bool) ([]busMigrationPhase, busMigrationStep) {
	phase := busMigrationPinning
	if leg == busMigrationGrow {
		phase = busMigrationFreezing
	}
	step := busMigrationStep{next: phase}
	seen := []busMigrationPhase{phase}
	failedOnce := false
	for i := 0; i < 20 && !busMigrationTerminal(step.next); i++ {
		outcome := busMigrationSucceeded
		if step.next == failAt && !failedOnce {
			outcome, failedOnce = busMigrationErred, true
		} else if failedOnce && failAgain && step.abandoning {
			outcome, failAgain = busMigrationErred, false
		}
		step = nextBusMigrationPhase(leg, step.next, step.abandoning, outcome)
		seen = append(seen, step.next)
	}
	return seen, step
}

// Every failure point ends bounded: back where it started with the data
// (Abandoned, not frozen), or, only when the way back itself fails, at Failed
// with the manual step named; frozen then only for one to three, whose
// rollback is what failed.
func TestEveryBusMigrationFailureIsBounded(t *testing.T) {
	for _, tc := range []struct {
		leg        busMigrationLeg
		failAt     busMigrationPhase
		failAgain  bool
		want       busMigrationPhase
		wantFrozen bool
	}{
		{busMigrationShrink, "", false, busMigrationDone, false},
		{busMigrationShrink, busMigrationPinning, false, busMigrationAbandoned, false},
		{busMigrationShrink, busMigrationPinning, true, busMigrationFailed, false},
		{busMigrationShrink, busMigrationShrinking, false, busMigrationFailed, false},
		{busMigrationGrow, "", false, busMigrationDone, false},
		{busMigrationGrow, busMigrationFreezing, false, busMigrationAbandoned, false},
		{busMigrationGrow, busMigrationSnapshot, false, busMigrationAbandoned, false},
		{busMigrationGrow, busMigrationHashing, false, busMigrationAbandoned, false},
		{busMigrationGrow, busMigrationConverting, false, busMigrationAbandoned, false},
		{busMigrationGrow, busMigrationRestoring, false, busMigrationAbandoned, false},
		{busMigrationGrow, busMigrationRestoring, true, busMigrationFailed, true},
		{busMigrationGrow, busMigrationConverting, true, busMigrationFailed, true},
	} {
		seen, last := walkBusMigration(tc.leg, tc.failAt, tc.failAgain)
		if last.next != tc.want {
			t.Errorf("%s failing at %q (again: %v): ended %s via %v, want %s", tc.leg, tc.failAt, tc.failAgain, last.next, seen, tc.want)
			continue
		}
		if got := busMigrationFrozen(tc.leg, last.next); got != tc.wantFrozen {
			t.Errorf("%s failing at %q (again: %v): left frozen = %v, want %v", tc.leg, tc.failAt, tc.failAgain, got, tc.wantFrozen)
		}
		if (last.next == busMigrationFailed) != (last.manual != "") {
			t.Errorf("%s failing at %q: Failed must name a manual step and nothing else may (manual %q)", tc.leg, tc.failAt, last.manual)
		}
	}
}

// One to three failing after the conversion rolls back through one server
// and the same snapshot, and only then lifts the freeze.
func TestAOneToThreeFailureAfterTheConversionRollsBack(t *testing.T) {
	seen, last := walkBusMigration(busMigrationGrow, busMigrationRestoring, false)
	want := []busMigrationPhase{busMigrationFreezing, busMigrationSnapshot, busMigrationHashing, busMigrationConverting,
		busMigrationRestoring, busMigrationRollingBack, busMigrationRollbackRestoring, busMigrationReleasing, busMigrationAbandoned}
	if len(seen) != len(want) {
		t.Fatalf("phases %v, want %v", seen, want)
	}
	for i := range want {
		if seen[i] != want[i] {
			t.Fatalf("phases %v, want %v", seen, want)
		}
	}
	if !last.abandoning {
		t.Error("a rolled-back run does not read as abandoned")
	}
}

// A pending pass changes nothing, and three to one never freezes.
func TestABusMigrationWaitsAndThreeToOneNeverFreezes(t *testing.T) {
	for _, phase := range []busMigrationPhase{busMigrationPinning, busMigrationShrinking, busMigrationUnpinning} {
		if busMigrationFrozen(busMigrationShrink, phase) {
			t.Errorf("three to one frozen in %s", phase)
		}
	}
	got := nextBusMigrationPhase(busMigrationGrow, busMigrationHashing, false, busMigrationPending)
	if got.next != busMigrationHashing || got.abandoning {
		t.Errorf("a pending Hashing pass moved to %+v", got)
	}
	for _, phase := range []busMigrationPhase{busMigrationSnapshot, busMigrationHashing, busMigrationConverting, busMigrationRestoring, busMigrationRollingBack, busMigrationReleasing} {
		if !busMigrationFrozen(busMigrationGrow, phase) {
			t.Errorf("one to three not frozen in %s", phase)
		}
	}
}

// The bus renders the old count until the phase that converts it, the new
// one from there, and the old one again on a rollback.
func TestTheBusRendersTheCountItsPhaseCalls(t *testing.T) {
	for _, tc := range []struct {
		leg        busMigrationLeg
		phase      busMigrationPhase
		abandoning bool
		want       int
	}{
		{busMigrationShrink, busMigrationPinning, false, 3},
		{busMigrationShrink, busMigrationUnpinning, true, 3},
		{busMigrationShrink, busMigrationShrinking, false, 1},
		{busMigrationShrink, busMigrationDone, false, 1},
		{busMigrationShrink, busMigrationAbandoned, true, 3},
		{busMigrationShrink, busMigrationFailed, false, 1},
		{busMigrationShrink, busMigrationFailed, true, 3},
		{busMigrationGrow, busMigrationFreezing, false, 1},
		{busMigrationGrow, busMigrationSnapshot, false, 1},
		{busMigrationGrow, busMigrationHashing, false, 1},
		{busMigrationGrow, busMigrationConverting, false, 3},
		{busMigrationGrow, busMigrationRestoring, false, 3},
		{busMigrationGrow, busMigrationRollingBack, true, 1},
		{busMigrationGrow, busMigrationRollbackRestoring, true, 1},
		{busMigrationGrow, busMigrationReleasing, false, 3},
		{busMigrationGrow, busMigrationReleasing, true, 1},
		{busMigrationGrow, busMigrationDone, false, 3},
		{busMigrationGrow, busMigrationAbandoned, true, 1},
		{busMigrationGrow, busMigrationFailed, true, 1},
	} {
		if got := busMigrationReplicas(tc.leg, tc.phase, tc.abandoning); got != tc.want {
			t.Errorf("%s %s (abandoning %v) renders %d, want %d", tc.leg, tc.phase, tc.abandoning, got, tc.want)
		}
	}
}

// A Job's outcome is its Complete or Failed condition; still pending past
// the run's deadline counts as failed.
func TestAMigrationJobsOutcomeAndTheDeadline(t *testing.T) {
	job := func(cond batchv1.JobConditionType, status corev1.ConditionStatus) *batchv1.Job {
		j := &batchv1.Job{}
		if cond != "" {
			j.Status.Conditions = []batchv1.JobCondition{{Type: cond, Status: status}}
		}
		return j
	}
	for _, tc := range []struct {
		job  *batchv1.Job
		want busMigrationOutcome
	}{
		{job("", ""), busMigrationPending},
		{job(batchv1.JobComplete, corev1.ConditionTrue), busMigrationSucceeded},
		{job(batchv1.JobFailed, corev1.ConditionTrue), busMigrationErred},
		{job(batchv1.JobComplete, corev1.ConditionFalse), busMigrationPending},
	} {
		if got := jobOutcome(tc.job); got != tc.want {
			t.Errorf("jobOutcome(%+v) = %v, want %v", tc.job.Status.Conditions, got, tc.want)
		}
	}
	now := time.Now()
	if withDeadline(busMigrationPending, now.Add(-time.Second), now) != busMigrationErred {
		t.Error("a pending phase past the deadline is not failed")
	}
	if withDeadline(busMigrationPending, now.Add(time.Hour), now) != busMigrationPending {
		t.Error("a pending phase before the deadline was failed")
	}
	if withDeadline(busMigrationSucceeded, now.Add(-time.Second), now) != busMigrationSucceeded {
		t.Error("a finished phase was failed by the deadline")
	}
}
