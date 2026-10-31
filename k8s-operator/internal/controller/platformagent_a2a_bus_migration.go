package controller

import (
	"context"
	"fmt"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The A2A bus migration (#4225): converting an existing bus between one NATS
// server and three without emptying it. This file holds the run's phases and
// the one function that moves between them; the reconcile that observes each
// phase's work and renders what a phase needs builds on it.

// busMigrationLeg is the direction of a run.
type busMigrationLeg string

const (
	// busMigrationShrink is three servers to one, in place: every stream is
	// led by server 0 and scaled to R1 while the bus is still a cluster,
	// then the bus renders one server. Writes keep flowing.
	busMigrationShrink busMigrationLeg = "ThreeToOne"
	// busMigrationGrow is one server to three, by snapshot and restore,
	// behind a write freeze.
	busMigrationGrow busMigrationLeg = "OneToThree"
)

// busMigrationPhase is where a run is.
type busMigrationPhase string

const (
	busMigrationPinning           busMigrationPhase = "Pinning"           // shrink: the pin Job runs
	busMigrationShrinking         busMigrationPhase = "Shrinking"         // shrink: one server rendered, waiting for it
	busMigrationUnpinning         busMigrationPhase = "Unpinning"         // shrink failed before the shrink: streams back to R3
	busMigrationFreezing          busMigrationPhase = "Freezing"          // grow: only the migration's principals admitted
	busMigrationSnapshot          busMigrationPhase = "Snapshotting"      // grow: the snapshot Job stages every stream
	busMigrationHashing           busMigrationPhase = "Hashing"           // grow: the operator's hashing pod digests the stage
	busMigrationConverting        busMigrationPhase = "Converting"        // grow: three servers rendered, waiting for them
	busMigrationRestoring         busMigrationPhase = "Restoring"         // grow: the restore pod restores and raises to R3
	busMigrationRollingBack       busMigrationPhase = "RollingBack"       // grow failed after the conversion: one server rendered again
	busMigrationRollbackRestoring busMigrationPhase = "RollbackRestoring" // grow: the same snapshot restored into one server
	busMigrationReleasing         busMigrationPhase = "Releasing"         // grow: the freeze lifted
	busMigrationDone              busMigrationPhase = "Done"              // converted, data kept
	busMigrationAbandoned         busMigrationPhase = "Abandoned"         // not converted; the bus runs as before, data kept
	busMigrationFailed            busMigrationPhase = "Failed"            // stopped where a person has to act
)

// busMigrationOutcome is what a pass observed of the current phase's work.
type busMigrationOutcome int

const (
	busMigrationPending   busMigrationOutcome = iota // still running
	busMigrationSucceeded                            // the phase's work completed
	busMigrationErred                                // the phase's work failed, or the run passed its deadline
)

// busMigrationStep is one transition: the next phase, whether the run is
// converting (Releasing after a success) or abandoning (Releasing after a
// rollback), and for Failed the manual step the condition names.
type busMigrationStep struct {
	next       busMigrationPhase
	abandoning bool
	manual     string
}

// nextBusMigrationPhase moves a run on. Failure is bounded: nothing leaves an
// install write-frozen unless the automatic way back has itself failed.
//   - Three to one, failing before the shrink: the pinned streams go back to
//     R3 and the bus stays three servers (Abandoned).
//   - One to three, failing before the conversion: the freeze is lifted and
//     the bus stays one server (Abandoned); nothing has changed.
//   - One to three, failing after the conversion: one server is rendered
//     again and the same snapshot restored into it, then the freeze is
//     lifted (Abandoned).
//   - Only a failed undo or rollback stops at Failed, frozen for one to
//     three, with the step a person has to take.
//
// abandoning carries whether the run is already on its way back, which
// Releasing needs to tell Done from Abandoned.
func nextBusMigrationPhase(leg busMigrationLeg, phase busMigrationPhase, abandoning bool, outcome busMigrationOutcome) busMigrationStep {
	stay := busMigrationStep{next: phase, abandoning: abandoning}
	if outcome == busMigrationPending {
		return stay
	}
	ok := outcome == busMigrationSucceeded
	switch leg {
	case busMigrationShrink:
		switch phase {
		case busMigrationPinning:
			if ok {
				return busMigrationStep{next: busMigrationShrinking}
			}
			return busMigrationStep{next: busMigrationUnpinning, abandoning: true}
		case busMigrationShrinking:
			if ok {
				return busMigrationStep{next: busMigrationDone}
			}
			return busMigrationStep{next: busMigrationFailed, manual: "server 0 did not come up alone after the shrink; its store holds every stream at R1: check the a2a-nats-0 pod, and set busReplicas back to 3 only once it serves"}
		case busMigrationUnpinning:
			if ok {
				return busMigrationStep{next: busMigrationAbandoned, abandoning: true}
			}
			return busMigrationStep{next: busMigrationFailed, abandoning: true, manual: "streams pinned to R1 on server 0 could not be raised back to R3; the bus is three servers with some single-copy streams: raise them with the bus-migrate unpin leg"}
		}
	case busMigrationGrow:
		switch phase {
		case busMigrationFreezing, busMigrationSnapshot, busMigrationHashing:
			if ok {
				return busMigrationStep{next: map[busMigrationPhase]busMigrationPhase{
					busMigrationFreezing: busMigrationSnapshot,
					busMigrationSnapshot: busMigrationHashing,
					busMigrationHashing:  busMigrationConverting,
				}[phase]}
			}
			// Nothing has changed yet: lift the freeze, stay one server.
			return busMigrationStep{next: busMigrationReleasing, abandoning: true}
		case busMigrationConverting, busMigrationRestoring:
			if ok {
				if phase == busMigrationConverting {
					return busMigrationStep{next: busMigrationRestoring}
				}
				return busMigrationStep{next: busMigrationReleasing}
			}
			return busMigrationStep{next: busMigrationRollingBack, abandoning: true}
		case busMigrationRollingBack:
			if ok {
				return busMigrationStep{next: busMigrationRollbackRestoring, abandoning: true}
			}
			return busMigrationStep{next: busMigrationFailed, abandoning: true, manual: "the bus did not come back up as one server for the rollback; it is write-frozen and its snapshot is kept on the staging volume: check the a2a-nats-0 pod (docs: bus migration runbook)"}
		case busMigrationRollbackRestoring:
			if ok {
				return busMigrationStep{next: busMigrationReleasing, abandoning: true}
			}
			return busMigrationStep{next: busMigrationFailed, abandoning: true, manual: "restoring the snapshot into one server failed; the bus is write-frozen and the snapshot is kept on the staging volume: see the bus migration runbook"}
		case busMigrationReleasing:
			if ok {
				if abandoning {
					return busMigrationStep{next: busMigrationAbandoned, abandoning: true}
				}
				return busMigrationStep{next: busMigrationDone}
			}
			return busMigrationStep{next: busMigrationFailed, abandoning: abandoning, manual: "the servers did not come back with the static logins after the freeze; the bus serves the migration's principals only: check the a2a-nats pods (docs: bus migration runbook)"}
		}
	}
	return stay
}

// busMigrationFrozen reports whether a phase holds the write freeze: only
// the migration's principals reach the bus. Three to one never freezes.
func busMigrationFrozen(leg busMigrationLeg, phase busMigrationPhase) bool {
	if leg != busMigrationGrow {
		return false
	}
	switch phase {
	case busMigrationDone, busMigrationAbandoned:
		return false
	}
	return true
}

// busMigrationTerminal reports a phase no pass moves on from.
func busMigrationTerminal(phase busMigrationPhase) bool {
	return phase == busMigrationDone || phase == busMigrationAbandoned || phase == busMigrationFailed
}

// busMigrationReplicas is the server count the bus renders in a phase. Three
// to one keeps three until it shrinks; one to three keeps one until it
// converts, and a rollback renders one again. A run that ended keeps the
// count it ended on: the new one when Done, the old one when Abandoned or
// when it failed on its way back.
func busMigrationReplicas(leg busMigrationLeg, phase busMigrationPhase, abandoning bool) int {
	from, to := 3, 1
	if leg == busMigrationGrow {
		from, to = 1, 3
	}
	switch phase {
	case busMigrationShrinking, busMigrationConverting, busMigrationRestoring, busMigrationDone:
		return to
	case busMigrationReleasing:
		if abandoning {
			return from
		}
		return to
	case busMigrationFailed:
		if abandoning || leg == busMigrationGrow {
			return from
		}
		// Three to one failed after the shrink: server 0 alone holds the
		// streams at R1.
		return to
	}
	return from
}

// jobOutcome reads a migration Job's outcome from its conditions.
func jobOutcome(job *batchv1.Job) busMigrationOutcome {
	for _, c := range job.Status.Conditions {
		if c.Status != corev1.ConditionTrue {
			continue
		}
		switch c.Type {
		case batchv1.JobComplete:
			return busMigrationSucceeded
		case batchv1.JobFailed:
			return busMigrationErred
		}
	}
	return busMigrationPending
}

// withDeadline turns a pending outcome into a failure once the run is past
// its deadline.
func withDeadline(outcome busMigrationOutcome, deadline, now time.Time) busMigrationOutcome {
	if outcome == busMigrationPending && !deadline.IsZero() && now.After(deadline) {
		return busMigrationErred
	}
	return outcome
}

// busMigrationStep names the migration's own workloads: the pin, unpin and
// snapshot Jobs and the hashing pod, each named for the run.
func busMigrationWorkloadName(agent *agentv1alpha1.PlatformAgent, step, runID string) string {
	return fmt.Sprintf("%s-bus-%s-%s", agent.Name, step, runID)
}

// busNATSRolledOutAt reports the bus StatefulSet rolled out at replicas: the
// spec asks for that count and every server is updated and Ready, as of the
// generation the controller observed.
func busNATSRolledOutAt(sts *appsv1.StatefulSet, replicas int32) bool {
	return sts.Spec.Replicas != nil && *sts.Spec.Replicas == replicas &&
		sts.Status.ObservedGeneration >= sts.Generation &&
		sts.Status.ReadyReplicas == replicas && sts.Status.UpdatedReplicas == replicas
}

// observeBusMigration reads the current phase's outcome from what runs:
// the phase's Job, the hashing pod, or the StatefulSet's rollout at the
// count the phase renders. The restore pod's phases stay pending until it
// exists.
func (r *PlatformAgentReconciler) observeBusMigration(ctx context.Context, agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus) (busMigrationOutcome, error) {
	leg, phase := busMigrationLeg(run.Leg), busMigrationPhase(run.Phase)
	job := func(step string) (busMigrationOutcome, error) {
		j := &batchv1.Job{}
		switch err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: busMigrationWorkloadName(agent, step, run.RunID)}, j); {
		case errors.IsNotFound(err):
			return busMigrationPending, nil
		case err != nil:
			return busMigrationPending, err
		}
		return jobOutcome(j), nil
	}
	switch phase {
	case busMigrationPinning:
		return job("pin")
	case busMigrationUnpinning:
		return job("unpin")
	case busMigrationSnapshot:
		return job("snapshot")
	case busMigrationHashing:
		pod := &corev1.Pod{}
		switch err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: busMigrationWorkloadName(agent, "hash", run.RunID)}, pod); {
		case errors.IsNotFound(err):
			return busMigrationPending, nil
		case err != nil:
			return busMigrationPending, err
		}
		switch pod.Status.Phase {
		case corev1.PodSucceeded:
			return busMigrationSucceeded, nil
		case corev1.PodFailed:
			return busMigrationErred, nil
		}
		return busMigrationPending, nil
	case busMigrationFreezing, busMigrationShrinking, busMigrationConverting, busMigrationRollingBack, busMigrationReleasing:
		sts := &appsv1.StatefulSet{}
		switch err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, sts); {
		case errors.IsNotFound(err):
			return busMigrationPending, nil
		case err != nil:
			return busMigrationPending, err
		}
		if busNATSRolledOutAt(sts, int32(busMigrationReplicas(leg, phase, run.Abandoning))) { // #nosec G115 -- 1 or 3
			return busMigrationSucceeded, nil
		}
		return busMigrationPending, nil
	}
	return busMigrationPending, nil
}

// advanceBusMigration moves a running migration on by what this pass
// observes, the deadline included, and returns the server count the bus
// renders in the phase it is in after that.
func (r *PlatformAgentReconciler) advanceBusMigration(ctx context.Context, agent *agentv1alpha1.PlatformAgent, now time.Time) (int, error) {
	run := agent.Status.BusMigration
	leg := busMigrationLeg(run.Leg)
	if !busMigrationTerminal(busMigrationPhase(run.Phase)) {
		outcome, err := r.observeBusMigration(ctx, agent, run)
		if err != nil {
			return 0, err
		}
		step := nextBusMigrationPhase(leg, busMigrationPhase(run.Phase), run.Abandoning, withDeadline(outcome, run.Deadline.Time, now))
		run.Phase, run.Abandoning = string(step.next), step.abandoning
		if step.manual != "" {
			run.Message = step.manual
		}
	}
	return busMigrationReplicas(leg, busMigrationPhase(run.Phase), run.Abandoning), nil
}

// busMigrationHoldsProvision reports a migration under way, during which the
// provision Job is not created: on one to three it would create the streams
// empty on the new cluster ahead of the restore, which then finds every name
// taken, and on three to one there is nothing for it to do until the bus is
// one server. Once the run ends, a Job for the bus's new shape runs as usual
// and finds every stream there.
func busMigrationHoldsProvision(agent *agentv1alpha1.PlatformAgent) bool {
	run := agent.Status.BusMigration
	return run != nil && !busMigrationTerminal(busMigrationPhase(run.Phase))
}

// a2aBusMigratingConditionType reports the bus's migration: True while one
// runs, False when the last one ended without converting (Abandoned) or
// stopped for a person (Failed), and absent once one converted the bus or
// none has run.
const a2aBusMigratingConditionType = "A2ABusMigrating"

// busMigratingCondition is the condition this pass's run calls for; ok is
// false when it is to be absent.
func busMigratingCondition(agent *agentv1alpha1.PlatformAgent) (cond metav1.Condition, ok bool) {
	run := agent.Status.BusMigration
	if run == nil || !a2aStackRendering(agent) {
		return metav1.Condition{}, false
	}
	leg, phase := busMigrationLeg(run.Leg), busMigrationPhase(run.Phase)
	from, to := 3, 1
	if leg == busMigrationGrow {
		from, to = 1, 3
	}
	switch phase {
	case busMigrationDone:
		return metav1.Condition{}, false
	case busMigrationAbandoned:
		return metav1.Condition{Type: a2aBusMigratingConditionType, Status: metav1.ConditionFalse, Reason: "Abandoned",
			Message: fmt.Sprintf("the migration from %d NATS servers to %d did not complete and was undone; the bus runs %d servers with its data. Change the spec to try again.", from, to, from)}, true
	case busMigrationFailed:
		return metav1.Condition{Type: a2aBusMigratingConditionType, Status: metav1.ConditionFalse, Reason: "Failed",
			Message: fmt.Sprintf("the migration from %d NATS servers to %d stopped: %s", from, to, run.Message)}, true
	}
	msg := fmt.Sprintf("migrating the A2A bus from %d NATS servers to %d (phase %s)", from, to, phase)
	if run.Abandoning {
		msg = fmt.Sprintf("undoing the A2A bus migration from %d NATS servers to %d (phase %s)", from, to, phase)
	}
	if busMigrationFrozen(leg, phase) {
		msg += "; writes are frozen: only the migration reaches the bus, and the console and web logins are off until it ends"
	}
	return metav1.Condition{Type: a2aBusMigratingConditionType, Status: metav1.ConditionTrue, Reason: "InProgress", Message: msg}, true
}

// busMigratingConditionCurrent reports whether the CR already carries the
// condition this pass calls for.
func busMigratingConditionCurrent(agent *agentv1alpha1.PlatformAgent) bool {
	want, ok := busMigratingCondition(agent)
	existing := meta.FindStatusCondition(agent.Status.Conditions, a2aBusMigratingConditionType)
	if !ok {
		return existing == nil
	}
	return existing != nil && existing.Status == want.Status && existing.Reason == want.Reason && existing.Message == want.Message
}

// setBusMigratingCondition writes the condition this pass calls for, or
// removes it. The caller does the API write.
func setBusMigratingCondition(agent *agentv1alpha1.PlatformAgent, now metav1.Time) {
	want, ok := busMigratingCondition(agent)
	if !ok {
		meta.RemoveStatusCondition(&agent.Status.Conditions, a2aBusMigratingConditionType)
		return
	}
	want.ObservedGeneration, want.LastTransitionTime = agent.Generation, now
	meta.SetStatusCondition(&agent.Status.Conditions, want)
}
