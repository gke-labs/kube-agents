package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/busmigrate"
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
			return busMigrationStep{next: busMigrationFailed, abandoning: true, manual: "streams pinned to R1 on server 0 could not be raised back to R3; the bus is three servers with some single-copy streams"}
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
		// Where it stopped: a run that failed on its way back is at the old
		// count; one that failed going forward is at the new one, which is
		// where its streams are (three to one after the shrink: server 0
		// alone, at R1; one to three in Releasing: a converted cluster at R3).
		if abandoning {
			return from
		}
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
// jobDeadlineExceeded reports a Job the API server stopped at its
// activeDeadlineSeconds, which the run's deadline sets.
func jobDeadlineExceeded(job *batchv1.Job) bool {
	for _, c := range job.Status.Conditions {
		if c.Type == batchv1.JobFailed && c.Status == corev1.ConditionTrue && c.Reason == batchv1.JobReasonDeadlineExceeded {
			return true
		}
	}
	return false
}

func withDeadline(outcome busMigrationOutcome, deadline, now time.Time) busMigrationOutcome {
	if outcome == busMigrationPending && !deadline.IsZero() && now.After(deadline) {
		return busMigrationErred
	}
	return outcome
}

// busMigrationStep names the migration's own workloads: the pin, unpin and
// snapshot Jobs and the hashing pod, each named for the run.
// busMigrationWorkName names a run's Job or pod for its current attempt: the
// run's own name until it is retried, then suffixed with the retry, so a
// retry runs fresh work and is never read off the attempt that failed. Its
// ServiceAccounts, staging claim and run Secret keep the run's name, which a
// retry reuses.
func busMigrationWorkName(agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus, name string) string {
	id := run.RunID
	if n := len(run.Retries); n > 0 {
		id = fmt.Sprintf("%s-r%d", id, n)
	}
	return busMigrationWorkloadName(agent, name, id)
}

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
// the phase's Job or pod, or the StatefulSet's rollout at the count the
// phase renders. late reports a Job or pod stopped at its deadline.
func (r *PlatformAgentReconciler) observeBusMigration(ctx context.Context, agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus) (busMigrationOutcome, bool, error) {
	leg, phase := busMigrationLeg(run.Leg), busMigrationPhase(run.Phase)
	job := func(step string) (busMigrationOutcome, bool, error) {
		j := &batchv1.Job{}
		switch err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: busMigrationWorkName(agent, run, step)}, j); {
		case errors.IsNotFound(err):
			return busMigrationPending, false, nil
		case err != nil:
			return busMigrationPending, false, err
		}
		return jobOutcome(j), jobDeadlineExceeded(j), nil
	}
	if work := busMigrationPhaseWorkload(phase); work.name != "" {
		if !work.pod {
			return job(work.name)
		}
		pod, err := r.busMigrationPod(ctx, agent, run, work.name)
		if pod == nil || err != nil {
			return busMigrationPending, false, err
		}
		switch pod.Status.Phase {
		case corev1.PodSucceeded:
			return busMigrationSucceeded, false, nil
		case corev1.PodFailed:
			return busMigrationErred, pod.Status.Reason == "DeadlineExceeded", nil
		}
		return busMigrationPending, false, nil
	}
	switch phase {
	case busMigrationFreezing, busMigrationShrinking, busMigrationConverting, busMigrationRollingBack, busMigrationReleasing:
		sts := &appsv1.StatefulSet{}
		switch err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, sts); {
		case errors.IsNotFound(err):
			return busMigrationPending, false, nil
		case err != nil:
			return busMigrationPending, false, err
		}
		if !busNATSRolledOutAt(sts, int32(busMigrationReplicas(leg, phase, run.Abandoning))) { // #nosec G115 -- 1 or 3
			return busMigrationPending, false, nil
		}
		// On the one to three leg the rollout must also be the right side
		// of the freeze: Freezing completes only on a restart into the
		// freeze, rendered after the callout confirmed it (a restart before
		// that, a drain say, is not one), the phases inside it keep it, and
		// Releasing completes only on a restart out of it.
		if leg == busMigrationGrow {
			want := run.RunID
			if phase == busMigrationReleasing {
				want = ""
			}
			if busNATSFrozenFor(sts) != want || (phase == busMigrationFreezing && !run.FreezeConfirmed) {
				return busMigrationPending, false, nil
			}
		}
		return busMigrationSucceeded, false, nil
	}
	return busMigrationPending, false, nil
}

// busMigrationPod reads one of the run's pods, nil when it is not there.
func (r *PlatformAgentReconciler) busMigrationPod(ctx context.Context, agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus, name string) (*corev1.Pod, error) {
	pod := &corev1.Pod{}
	switch err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: busMigrationWorkName(agent, run, name)}, pod); {
	case errors.IsNotFound(err):
		return nil, nil
	case err != nil:
		return nil, err
	}
	return pod, nil
}

// terminationMessage is a pod's first container's termination message.
func terminationMessage(pod *corev1.Pod) string {
	for _, cs := range pod.Status.ContainerStatuses {
		if cs.State.Terminated != nil {
			return cs.State.Terminated.Message
		}
	}
	return ""
}

// advanceBusMigration moves a running migration on by what this pass
// observes, the deadline included, and returns the server count the bus
// renders in the phase it is in after that.
func (r *PlatformAgentReconciler) advanceBusMigration(ctx context.Context, agent *agentv1alpha1.PlatformAgent, now time.Time) (int, error) {
	run := agent.Status.BusMigration
	leg := busMigrationLeg(run.Leg)
	if !busMigrationTerminal(busMigrationPhase(run.Phase)) {
		observed, late, err := r.observeBusMigration(ctx, agent, run)
		if err != nil {
			return 0, err
		}
		outcome := withDeadline(observed, run.Deadline.Time, now)
		if busMigrationPhase(run.Phase) == busMigrationFreezing && !run.FreezeConfirmed && outcome == busMigrationPending {
			// The handshake: the bus restarts into the freeze only once
			// every callout replica serves the frozen map.
			served, err := r.busMigrationCalloutServesTheFreeze(ctx, agent, run)
			if err != nil {
				return 0, err
			}
			run.FreezeConfirmed = served
		}
		switch phase := busMigrationPhase(run.Phase); {
		case phase == busMigrationHashing && outcome == busMigrationSucceeded:
			// The digests go from the hashing pod's termination message into
			// the operator's own Secret, beside its policy; the restore pods
			// read them from there and never from the staging volume.
			refused, err := r.ensureBusRunSecret(ctx, agent, run)
			if err != nil {
				return 0, err
			}
			if refused != "" {
				outcome, run.Cause = busMigrationErred, "the hashing pod's report was refused: "+refused
			}
		case (phase == busMigrationRestoring || phase == busMigrationRollbackRestoring) && observed != busMigrationPending:
			if failure, err := r.recordRestoreReport(ctx, agent, run); err != nil {
				return 0, err
			} else if failure != "" && outcome == busMigrationErred && run.Cause == "" && !late {
				// A pod stopped at its deadline is named for the deadline
				// below, whatever it managed to report.
				run.Cause = fmt.Sprintf("the %s phase failed: %s", run.Phase, failure)
			}
		}
		if outcome == busMigrationErred && run.Cause == "" {
			run.Cause = busMigrationCause(run, late || observed == busMigrationPending)
		}
		step := nextBusMigrationPhase(leg, busMigrationPhase(run.Phase), run.Abandoning, outcome)
		if step.next == busMigrationFailed && busMigrationPhase(run.Phase) != busMigrationFailed {
			run.FailedIn = run.Phase
		}
		if step.abandoning && !run.Abandoning && run.ForwardDeadline == nil {
			// Turning back: the way back gets its own deadline, once per
			// run, so an undo that starts at the forward deadline has the
			// time to run, with credentials that last it.
			run.ForwardDeadline = run.Deadline.DeepCopy()
			run.Deadline = metav1.NewTime(now.Add(busMigrationRunDeadline))
		}
		run.Phase, run.Abandoning = string(step.next), step.abandoning
		if step.manual != "" {
			run.Message = step.manual
		}
		if err := r.ensureBusMigrationWorkload(ctx, agent, run, now); err != nil {
			return 0, err
		}
		if busMigrationTerminal(busMigrationPhase(run.Phase)) {
			if err := r.cleanupBusMigration(ctx, agent, run); err != nil {
				return 0, err
			}
		}
	}
	return busMigrationReplicas(leg, busMigrationPhase(run.Phase), run.Abandoning), nil
}

// busMigrationCause says why a run turned back: the phase that failed, and
// the deadline when that is what stopped it, so a slow run is not read as a
// broken one.
func busMigrationCause(run *agentv1alpha1.BusMigrationStatus, late bool) string {
	if late {
		return fmt.Sprintf("the %s phase was still running at the run's deadline, %s", run.Phase, run.Deadline.UTC().Format(time.RFC3339))
	}
	return fmt.Sprintf("the %s phase failed", run.Phase)
}

// busMigrationHoldsProvision reports a migration under way, during which the
// provision Job is not created: on one to three it would create the streams
// empty on the new cluster ahead of the restore, which then finds every name
// taken, and on three to one there is nothing for it to do until the bus is
// one server. Once the run ends, a Job for the bus's new shape runs as usual
// and finds every stream there.
func busMigrationHoldsProvision(agent *agentv1alpha1.PlatformAgent) bool {
	run := agent.Status.BusMigration
	// A Failed run holding its freeze holds the Job too: streams it would
	// create empty would take the names a retried restore needs.
	return run != nil && (!busMigrationTerminal(busMigrationPhase(run.Phase)) || busMigrationFreezeActive(agent))
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
			Message: fmt.Sprintf("the migration from %d NATS servers to %d did not complete and was undone (%s); the bus runs %d servers with its data. To try again, annotate the PlatformAgent %s=%s.", from, to, run.Cause, from, a2aBusMigrationStartAnnotation, run.RunID)}, true
	case busMigrationFailed:
		return metav1.Condition{Type: a2aBusMigratingConditionType, Status: metav1.ConditionFalse, Reason: "Failed",
			Message: fmt.Sprintf("the migration from %d NATS servers to %d stopped (%s): %s.%s", from, to, run.Cause, run.Message, busMigrationFailedActions(agent))}, true
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

// busMigrationIdentities are the bus principals the migration run holds in
// the phase it is in, and none outside it: the callout map names a run's
// principal only while its Job or pod runs, so a token left behind
// authenticates nothing once the phase has moved on. Each is its own
// ServiceAccount, named for the run, and its grants are the JetStream API
// verbs its work sends, on the provisioned streams by name and nothing else;
// it subscribes to its own inbox only.
//
// Two kinds. The three to one leg's pin Job (and its undo, unpin) moves
// stream leaders and replica counts. The one to three leg's stream identity
// is the operator's own pods, which take the snapshot, restore it and roll
// it back: the only principal holding SNAPSHOT, RESTORE or DELETE, each per
// stream name, its chunk subjects per name too (a restore's data travels on
// the chunk subject, which the server derives from the request's stream
// name, so a request can only deliver data to a stream whose chunk subject
// it is granted).
// The hashing pod between them has no bus identity at all.
func busMigrationIdentities(agent *agentv1alpha1.PlatformAgent) []a2aIdentity {
	run := agent.Status.BusMigration
	if run == nil || busMigrationTerminal(busMigrationPhase(run.Phase)) {
		return nil
	}
	var step string
	var verbs []string
	var extra func(stream string) []string
	switch busMigrationPhase(run.Phase) {
	case busMigrationPinning:
		step, verbs = "pin", []string{"INFO", "LEADER.STEPDOWN", "UPDATE"}
	case busMigrationUnpinning:
		step, verbs = "unpin", []string{"INFO", "UPDATE"}
	case busMigrationSnapshot, busMigrationRestoring, busMigrationRollbackRestoring:
		step, verbs = busMigrationStreamIdentity, []string{"INFO", "SNAPSHOT", "RESTORE", "UPDATE", "DELETE"}
		// A snapshot's chunks arrive on the identity's inbox and are acked
		// on the first; a restore's are sent on the second.
		extra = func(stream string) []string {
			return []string{"$JS.SNAPSHOT.ACK." + stream + ".>", "$JS.SNAPSHOT.RESTORE." + stream + ".>"}
		}
	default:
		return nil
	}
	user := busMigrationPrincipal(step, run.RunID)
	var publish []string
	for _, stream := range a2aProvisionedStreams {
		for _, verb := range verbs {
			publish = append(publish, "$JS.API.STREAM."+verb+"."+stream)
		}
		if extra != nil {
			publish = append(publish, extra(stream)...)
		}
	}
	return []a2aIdentity{{
		user:           user,
		account:        a2aAccountApp,
		comment:        "the bus migration's " + step + " identity for run " + run.RunID + "; present only while its work runs",
		auth:           a2aAuthCallout,
		serviceAccount: a2aServiceAccountName(agent.Namespace, busMigrationWorkloadName(agent, step, run.RunID)),
		publish:        publish,
		subscribe:      []string{"_INBOX." + user + ".>"},
		// The callout refuses this principal from the run's deadline on and
		// mints it no JWT that outlives it, so nothing valid outlasts the
		// run. The step's Job or pod stops busMigrationJobMargin earlier, so
		// a slow step fails on its own time limit, which Cause names, before
		// its credential does.
		notAfter: ptr.To(run.Deadline.UTC()),
	}}
}

// busMigrationPrincipal is a migration step's bus user, which is also its
// inbox prefix.
func busMigrationPrincipal(step, runID string) string {
	return "migrate-" + step + "-" + runID
}

const (
	// busMigrationJobMargin is how long before the run's deadline a step's
	// Job is stopped, so it fails on its own time limit while its credential
	// still works, never on an authorization error mid-stream.
	busMigrationJobMargin = 5 * time.Minute
	// busMigrationStageMount is where the staging volume is mounted in the
	// snapshot Job and the hashing pod.
	busMigrationStageMount = "/stage"
	// busMigrationStageVolume names that volume in their pod specs.
	busMigrationStageVolume = "stage"
	// busMigrationBinary is the operator image's entrypoint, which runs the
	// migration's subcommands (cmd/main.go).
	busMigrationBinary = "/manager"
)

// busMigrationStageClaim is the run's staging volume claim.
func busMigrationStageClaim(agent *agentv1alpha1.PlatformAgent, runID string) string {
	return busMigrationWorkloadName(agent, "stage", runID)
}

// busMigrationImage is the image the migration's pods run: the operator's
// own (OPERATOR_IMAGE), so their version is the operator's. "" when the
// manager was deployed without it, and then no run starts its pods.
func busMigrationImage() string {
	return os.Getenv(operatorImageEnvVar)
}

// busMigrationPodSpec is what every migration pod shares: its own
// ServiceAccount with no token mounted (the bus token is projected
// separately and only where a pod connects), the hardened pod and container
// security, and no restarts, so a failure is the phase's failure.
func busMigrationPodSpec(agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus, step, identity string, command []string, volumes []corev1.Volume, mounts []corev1.VolumeMount) corev1.PodSpec {
	return corev1.PodSpec{
		ServiceAccountName:           busMigrationWorkloadName(agent, identity, run.RunID),
		AutomountServiceAccountToken: ptr.To(false),
		RestartPolicy:                corev1.RestartPolicyNever,
		SecurityContext: &corev1.PodSecurityContext{
			SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
			RunAsNonRoot:   ptr.To(true),
			RunAsUser:      ptr.To(int64(65532)),
			FSGroup:        ptr.To(int64(65532)),
		},
		Volumes: volumes,
		Containers: []corev1.Container{{
			Name:                     "bus-" + step,
			Image:                    busMigrationImage(),
			Command:                  command,
			SecurityContext:          hardenedSecurityContext(),
			Resources:                a2aProvisionResources(),
			VolumeMounts:             mounts,
			TerminationMessagePolicy: corev1.TerminationMessageReadFile,
		}},
	}
}

// buildBusMigrationJob is a run's pin or unpin Job: bus-migrate
// connecting as the step's own principal with its projected bus token, over
// TLS verified against the bus CA, bounded by the run's deadline.
func buildBusMigrationJob(agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus, step string, now time.Time) *batchv1.Job {
	command := []string{busMigrationBinary, "bus-migrate", step,
		"--server", a2aNATSTLSScheme + a2aNATSAddress(agent),
		"--ca-file", a2aNATSCAFile(),
		"--user", busMigrationPrincipal(step, run.RunID),
		"--token-file", a2aBusTokenPath + "/" + a2aBusTokenFile,
		"--streams", strings.Join(a2aProvisionedStreams, ","),
	}
	volumes := []corev1.Volume{a2aBusTokenVolumeSource(), a2aNATSCABundleVolume(agent)}
	mounts := []corev1.VolumeMount{a2aBusTokenVolumeMount(), a2aNATSCABundleMount()}
	switch step {
	case "pin":
		command = append(command, "--target", a2aNATSName(agent)+"-0")
	case "unpin":
		command = append(command, "--replicas", "3")
	}
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      busMigrationWorkName(agent, run, step),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "bus-migration"),
		},
		Spec: batchv1.JobSpec{
			BackoffLimit:            ptr.To(int32(2)),
			ActiveDeadlineSeconds:   ptr.To(busMigrationActiveDeadline(run, now)),
			TTLSecondsAfterFinished: ptr.To(int32(86400)),
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: a2aLabels(agent, "bus-migration")},
				Spec:       busMigrationPodSpec(agent, run, step, step, command, volumes, mounts),
			},
		},
	}
}

// buildBusHashPod is a run's hashing pod: bus-hash over the staging volume,
// mounted read-only, writing the digests to its termination message for the
// operator to read. It holds no bus credential at all.
func buildBusHashPod(agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus) *corev1.Pod {
	command := []string{busMigrationBinary, "bus-hash", "--dir", busMigrationStageMount, "--streams", strings.Join(a2aProvisionedStreams, ",")}
	volumes := []corev1.Volume{{Name: busMigrationStageVolume, VolumeSource: corev1.VolumeSource{
		PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: busMigrationStageClaim(agent, run.RunID), ReadOnly: true},
	}}}
	mounts := []corev1.VolumeMount{{Name: busMigrationStageVolume, MountPath: busMigrationStageMount, ReadOnly: true}}
	spec := busMigrationPodSpec(agent, run, "hash", "hash", command, volumes, mounts)
	// Bounded like the rest, so a hung hash never outlives the run holding
	// the one-pod staging claim.
	spec.ActiveDeadlineSeconds = ptr.To(busMigrationActiveDeadline(run, time.Now()))
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      busMigrationWorkName(agent, run, "hash"),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "bus-migration"),
		},
		Spec: spec,
	}
}

// busMigrationActiveDeadline is a Job's or pod's activeDeadlineSeconds: the
// time left on the run less busMigrationJobMargin, so the work stops while
// its credential still works.
func busMigrationActiveDeadline(run *agentv1alpha1.BusMigrationStatus, now time.Time) int64 {
	return max(int64(run.Deadline.Add(-busMigrationJobMargin).Sub(now).Seconds()), 1)
}

// busMigrationStreamIdentity names the one to three leg's bus identity:
// its ServiceAccount, its principal, and the pods that hold it.
const busMigrationStreamIdentity = "stream"

const (
	// busMigrationRunMount is where the restore pods read the run Secret.
	busMigrationRunMount = "/run/bus-migration"
	// busMigrationRunKey is the run Secret's one key (busmigrate.RunFile).
	busMigrationRunKey = "run.json"
)

// busMigrationRunSecret is the run's Secret: the hashing pod's digests and
// the operator's stream policies, which the restore pods read.
func busMigrationRunSecret(agent *agentv1alpha1.PlatformAgent, runID string) string {
	return busMigrationWorkloadName(agent, "run", runID)
}

// buildBusStreamPod is one of the one to three leg's pods, all holding the
// stream identity: "snapshot" stages every stream to the staging volume,
// mounted read-write; "restore" restores them into three servers and
// raises each to R3; "rollback" restores the same snapshot into one server.
// The two restores mount the staging volume read-only and take the digests
// and policies from the run Secret. Each is bounded by the run's deadline
// less the margin.
func buildBusStreamPod(agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus, kind string, now time.Time) *corev1.Pod {
	leg := map[string]string{"snapshot": "snapshot", "restore": "restore", "rollback": "restore"}[kind]
	command := []string{busMigrationBinary, "bus-migrate", leg,
		"--server", a2aNATSTLSScheme + a2aNATSAddress(agent),
		"--ca-file", a2aNATSCAFile(),
		"--user", busMigrationPrincipal(busMigrationStreamIdentity, run.RunID),
		"--token-file", a2aBusTokenPath + "/" + a2aBusTokenFile,
		"--streams", strings.Join(a2aProvisionedStreams, ","),
		"--dir", busMigrationStageMount,
	}
	readOnly := kind != "snapshot"
	volumes := []corev1.Volume{a2aBusTokenVolumeSource(), a2aNATSCABundleVolume(agent), {Name: busMigrationStageVolume, VolumeSource: corev1.VolumeSource{
		PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: busMigrationStageClaim(agent, run.RunID), ReadOnly: readOnly},
	}}}
	mounts := []corev1.VolumeMount{a2aBusTokenVolumeMount(), a2aNATSCABundleMount(), {Name: busMigrationStageVolume, MountPath: busMigrationStageMount, ReadOnly: readOnly}}
	if leg == "restore" {
		replicas := "3"
		if kind == "rollback" {
			replicas = "1"
		}
		command = append(command, "--run-file", busMigrationRunMount+"/"+busMigrationRunKey, "--replicas", replicas)
		volumes = append(volumes, corev1.Volume{Name: "run", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{
			SecretName: busMigrationRunSecret(agent, run.RunID), DefaultMode: ptr.To(int32(0o440)),
		}}})
		mounts = append(mounts, corev1.VolumeMount{Name: "run", MountPath: busMigrationRunMount, ReadOnly: true})
	}
	spec := busMigrationPodSpec(agent, run, kind, busMigrationStreamIdentity, command, volumes, mounts)
	spec.ActiveDeadlineSeconds = ptr.To(busMigrationActiveDeadline(run, now))
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      busMigrationWorkName(agent, run, kind),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "bus-migration"),
		},
		Spec: spec,
	}
}

// busMigrationWork is what a phase runs: the Job or pod's name in the run,
// the identity (ServiceAccount) it holds, and whether it is a bare pod.
type busMigrationWork struct {
	name, identity string
	pod            bool
}

// busMigrationPhaseWorkload is the work a phase runs, zero for the phases
// that run none of their own (rollouts).
func busMigrationPhaseWorkload(phase busMigrationPhase) busMigrationWork {
	switch phase {
	case busMigrationPinning:
		return busMigrationWork{name: "pin", identity: "pin"}
	case busMigrationUnpinning:
		return busMigrationWork{name: "unpin", identity: "unpin"}
	case busMigrationSnapshot:
		return busMigrationWork{name: "snapshot", identity: busMigrationStreamIdentity, pod: true}
	case busMigrationHashing:
		return busMigrationWork{name: "hash", identity: "hash", pod: true}
	case busMigrationRestoring:
		return busMigrationWork{name: "restore", identity: busMigrationStreamIdentity, pod: true}
	case busMigrationRollbackRestoring:
		return busMigrationWork{name: "rollback", identity: busMigrationStreamIdentity, pod: true}
	}
	return busMigrationWork{}
}

// ensureBusMigrationWorkload creates the current phase's ServiceAccount and
// then its Job or pod, both owned by the CR, if they are not there yet. The
// ServiceAccount is the identity the callout resolves the pod's token to, so
// it comes first; it holds no RBAC. Nothing is created without
// OPERATOR_IMAGE, and the phase then waits out the run's deadline.
func (r *PlatformAgentReconciler) ensureBusMigrationWorkload(ctx context.Context, agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus, now time.Time) error {
	return r.ensureBusMigrationWorkloadFor(ctx, agent, run, busMigrationPhaseWorkload(busMigrationPhase(run.Phase)), now)
}

// ensureBusMigrationWorkloadFor is ensureBusMigrationWorkload for one work.
func (r *PlatformAgentReconciler) ensureBusMigrationWorkloadFor(ctx context.Context, agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus, work busMigrationWork, now time.Time) error {
	if work.name == "" || busMigrationImage() == "" {
		return nil
	}
	if work.identity == busMigrationSentinelIdentity {
		// Never: the sentinel's ServiceAccount must not exist, or its
		// entry would be an identity something could hold.
		return fmt.Errorf("refusing to create the freeze sentinel's ServiceAccount")
	}
	if work.name == "snapshot" {
		pvc := buildBusStagePVC(agent, run)
		if err := ctrl.SetControllerReference(agent, pvc, r.Scheme); err != nil {
			return err
		}
		withCommonLabels(pvc, agent)
		if err := r.Create(ctx, pvc); err != nil && !errors.IsAlreadyExists(err) {
			return err
		}
	}
	if err := ReconcileServiceAccount(ctx, r.Client, r.Scheme, agent, busMigrationWorkloadName(agent, work.identity, run.RunID), agent.Namespace, nil, commonLabels(agent), fieldOwner); err != nil {
		return err
	}
	var obj client.Object
	switch {
	case work.name == "hash":
		obj = buildBusHashPod(agent, run)
	case work.pod:
		obj = buildBusStreamPod(agent, run, work.name, now)
	default:
		obj = buildBusMigrationJob(agent, run, work.name, now)
	}
	if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
		return err
	}
	withCommonLabels(obj, agent)
	if err := r.Create(ctx, obj); err != nil && !errors.IsAlreadyExists(err) {
		return err
	}
	return nil
}

// a2aStreamPolicies is the provision script's streams on the fields a
// restore checks (busmigrate.StreamPolicy), at replicas. Literal for literal
// with the script's `stream add` and `kv add` lines, which
// TestTheStreamPoliciesAreTheProvisionScripts holds it to; a `kv add`
// implies the KV fields (its subjects, rollup, deny delete, direct, discard
// new), which TestTheStreamPoliciesMatchAFreshInstall holds to a fresh
// install. The restore pod reads it from the operator's run Secret.
func a2aStreamPolicies(replicas int) []busmigrate.StreamPolicy {
	const day = 24 * time.Hour
	stream := func(name string, subjects []string, maxAge time.Duration, maxBytes, perSubject int64) busmigrate.StreamPolicy {
		return busmigrate.StreamPolicy{Name: name, Subjects: subjects, Retention: nats.LimitsPolicy, Storage: nats.FileStorage,
			MaxBytes: maxBytes, MaxAge: maxAge, Discard: nats.DiscardOld, MaxMsgsPerSubject: perSubject, AllowDirect: true, Replicas: replicas}
	}
	kv := func(bucket string, maxBytes int64, ttl time.Duration) busmigrate.StreamPolicy {
		return busmigrate.StreamPolicy{Name: "KV_" + bucket, Subjects: []string{"$KV." + bucket + ".>"}, Retention: nats.LimitsPolicy, Storage: nats.FileStorage,
			MaxBytes: maxBytes, MaxAge: ttl, Discard: nats.DiscardNew, MaxMsgsPerSubject: 1, AllowRollup: true, DenyDelete: true, AllowDirect: true, Replicas: replicas}
	}
	return []busmigrate.StreamPolicy{
		stream("TASKS", []string{"a2a.tasks.>"}, 3*day, 21474836480, 4096),
		stream("DIRECTORY", []string{"a2a.agents.>"}, 0, 1073741824, 1),
		stream(a2aTopicsStateStream, []string{"a2a.topics.agent.platform.upgrade-readiness", a2aTopologySkewSubject, "a2a.topics.shared.blueprint", "a2a.topics.shared.probe"}, 0, 1073741824, 8),
		stream("TOPICS-JOURNAL", []string{"a2a.topics.shared.annotations"}, 30*day, 5368709120, -1),
		kv("runtime-state", 268435456, 0),
		kv("session-state", 268435456, 0),
		kv("cap", 268435456, 0),
		kv("feedback", 67108864, 30*day),
		kv("PROFILE_LEASES", 67108864, 0),
		stream("PROFILE_SUBMITS", []string{"a2a.submit.>"}, 3*day, 1073741824, 1),
	}
}

// ensureBusRunSecret writes the run Secret once the hashing pod has
// succeeded: its digests, read from its termination message, beside the
// operator's stream policies at one replica (each stream is restored at R1,
// then raised). A report that is not exactly one well-formed digest pair
// per provisioned stream is refused, and refused says why; the run then
// turns back without the Secret.
func (r *PlatformAgentReconciler) ensureBusRunSecret(ctx context.Context, agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus) (refused string, err error) {
	key := client.ObjectKey{Namespace: agent.Namespace, Name: busMigrationRunSecret(agent, run.RunID)}
	// Read uncached: the operator may not list or watch Secrets.
	if err := r.a2aReader().Get(ctx, key, &corev1.Secret{}); err == nil {
		return "", nil
	} else if !errors.IsNotFound(err) {
		return "", err
	}
	pod, err := r.busMigrationPod(ctx, agent, run, "hash")
	if err != nil {
		return "", err
	}
	if pod == nil {
		return "the hashing pod is gone", nil
	}
	digests, refused := parseBusDigests(terminationMessage(pod))
	if refused != "" {
		return refused, nil
	}
	body, err := json.Marshal(busmigrate.RunFile{Digests: digests, Policies: a2aStreamPolicies(1)})
	if err != nil {
		return "", err
	}
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace, Labels: a2aLabels(agent, "bus-migration")},
		Data:       map[string][]byte{busMigrationRunKey: body},
	}
	if err := ctrl.SetControllerReference(agent, secret, r.Scheme); err != nil {
		return "", err
	}
	withCommonLabels(secret, agent)
	if err := r.Create(ctx, secret); err != nil && !errors.IsAlreadyExists(err) {
		return "", err
	}
	return "", nil
}

// parseBusDigests reads the hashing pod's report: exactly the provisioned
// streams, each with a data and a metadata digest of 64 lowercase hex.
func parseBusDigests(msg string) (map[string]busmigrate.StagedDigest, string) {
	var digests map[string]busmigrate.StagedDigest
	if err := json.Unmarshal([]byte(msg), &digests); err != nil {
		return nil, "it is not a digest report"
	}
	hex64 := func(s string) bool {
		return len(s) == 64 && strings.Trim(s, "0123456789abcdef") == ""
	}
	if len(digests) != len(a2aProvisionedStreams) {
		return nil, fmt.Sprintf("it names %d streams, not the %d provisioned", len(digests), len(a2aProvisionedStreams))
	}
	for _, name := range a2aProvisionedStreams {
		d, ok := digests[name]
		if !ok {
			return nil, "it has no digest for " + name
		}
		if !hex64(d.Data) || !hex64(d.Meta) {
			return nil, "its digests for " + name + " are not SHA-256 hex"
		}
	}
	return digests, ""
}

// busMigrationEventLimit bounds the warnings Event's message.
const busMigrationEventLimit = 1024

// recordRestoreReport reads a finished restore pod's report. Its warnings
// go onto the run's status, once, with one Warning Event, so nothing the
// restore accepted outside its policy goes unreported; its error, when it
// failed, is returned for the run's cause.
func (r *PlatformAgentReconciler) recordRestoreReport(ctx context.Context, agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus) (failure string, err error) {
	pod, err := r.busMigrationPod(ctx, agent, run, busMigrationPhaseWorkload(busMigrationPhase(run.Phase)).name)
	if err != nil || pod == nil {
		return "", err
	}
	msg := terminationMessage(pod)
	if msg == "" {
		// Killed before it wrote one: at its deadline, or evicted.
		return "", nil
	}
	var report busmigrate.Report
	if err := json.Unmarshal([]byte(msg), &report); err != nil {
		return "its report could not be read", nil
	}
	if len(report.Warnings) > 0 && run.Warnings == nil {
		run.Warnings = report.Warnings
		if r.Recorder != nil {
			msg := fmt.Sprintf("the A2A bus migration restored streams that differ from a fresh install (%d): %s",
				len(report.Warnings), strings.Join(report.Warnings, "; "))
			if len(msg) > busMigrationEventLimit {
				msg = msg[:busMigrationEventLimit-3] + "..."
			}
			r.Recorder.Event(agent, corev1.EventTypeWarning, "BusMigrationDrift", msg)
		}
	}
	return report.Error, nil
}

// busMigrationStageHeadroom is the staging volume's margin over the streams'
// caps, for the snapshots' metadata and the filesystem's own overhead.
const busMigrationStageHeadroom = 1.1

// busMigrationStageSize is the staging volume's request: every provisioned
// stream full to its max_bytes cap, with headroom, in whole GiB. A snapshot
// is at most its stream's size, so the stage never runs out before the bus
// it copies would.
func busMigrationStageSize() resource.Quantity {
	var total int64
	for _, p := range a2aStreamPolicies(1) {
		total += p.MaxBytes
	}
	const gib = 1 << 30
	n := int64(math.Ceil(float64(total) * busMigrationStageHeadroom / gib))
	return *resource.NewQuantity(n*gib, resource.BinarySI)
}

// buildBusStagePVC is the run's staging volume, ReadWriteOncePod so only
// one pod at a time holds it: the snapshot pod writes it, then the hashing
// pod reads it, then a restore pod does, and no second pod can open it in
// between. The cluster's default StorageClass, as the bus's own claims use.
func buildBusStagePVC(agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus) *corev1.PersistentVolumeClaim {
	return &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{
			Name:      busMigrationStageClaim(agent, run.RunID),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "bus-migration"),
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOncePod},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceStorage: busMigrationStageSize()},
			},
		},
	}
}

// cleanupBusMigration removes what a run made once it has ended: every
// per-run ServiceAccount, and, unless the run Failed and was not given up,
// the staging volume and the run Secret. A Failed run keeps those two for a
// retry, which needs the snapshot and its digests; its
// ServiceAccounts go regardless, since the map no longer names them. Jobs
// and pods stay, owned by the CR, for their logs; they hold no credential
// once their ServiceAccount is gone.
func (r *PlatformAgentReconciler) cleanupBusMigration(ctx context.Context, agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus) error {
	var objs []client.Object
	for _, identity := range []string{"pin", "unpin", "hash", busMigrationStreamIdentity} {
		objs = append(objs, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: busMigrationWorkloadName(agent, identity, run.RunID), Namespace: agent.Namespace}})
	}
	if busMigrationPhase(run.Phase) != busMigrationFailed || run.GivenUp {
		objs = append(objs,
			&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: busMigrationRunSecret(agent, run.RunID), Namespace: agent.Namespace}},
			&corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: busMigrationStageClaim(agent, run.RunID), Namespace: agent.Namespace}})
	}
	for _, obj := range objs {
		if err := r.Delete(ctx, obj); err != nil && !errors.IsNotFound(err) {
			return err
		}
	}
	return nil
}

// busMigrationFreezeActive reports the one to three leg's write freeze in
// force: from Freezing until Releasing begins. While it is, the identity map
// holds only busMigrationFrozenIdentities, and, once the callout serves that
// map (FreezeConfirmed), the bus renders without its APP account's static
// users.
func busMigrationFreezeActive(agent *agentv1alpha1.PlatformAgent) bool {
	run := agent.Status.BusMigration
	if run == nil {
		return false
	}
	phase := busMigrationPhase(run.Phase)
	if phase == busMigrationFailed {
		// A run that failed inside the freeze keeps it, over a bus whose
		// streams were not all put back and the snapshot that can, until a
		// person retries or gives it up (busMigrationActOnFailed). An
		// unrelated spec change does not lift it.
		if run.GivenUp || run.FailedIn == "" {
			return false
		}
		phase = busMigrationPhase(run.FailedIn)
	}
	return busMigrationFrozen(busMigrationLeg(run.Leg), phase) && phase != busMigrationReleasing && !busMigrationTerminal(phase)
}

// busMigrationSentinelIdentity names the freeze's sentinel: an identity-map
// entry that keeps the frozen map non-empty in the phases where no migration
// principal is present (the callout refuses an empty map and goes on serving
// the previous one, which would end the freeze unannounced). Its
// ServiceAccount is one the operator never creates
// (ensureBusMigrationWorkload refuses it), and it grants its own inbox only.
const busMigrationSentinelIdentity = "frozen"

// busMigrationSentinel is the run's sentinel entry.
func busMigrationSentinel(agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus) a2aIdentity {
	user := busMigrationPrincipal(busMigrationSentinelIdentity, run.RunID)
	return a2aIdentity{
		user:           user,
		account:        a2aAccountApp,
		comment:        "the bus migration's freeze sentinel for run " + run.RunID + "; keeps the frozen map non-empty, bound to a ServiceAccount nothing creates",
		auth:           a2aAuthCallout,
		serviceAccount: a2aServiceAccountName(agent.Namespace, busMigrationWorkloadName(agent, busMigrationSentinelIdentity, run.RunID)),
		publish:        []string{"_INBOX." + user + ".>"},
		subscribe:      []string{"_INBOX." + user + ".>"},
		notAfter:       ptr.To(run.Deadline.UTC()),
	}
}

// busMigrationFrozenIdentities is the whole callout identity map during the
// freeze: the migration principal of the phase, if any, and the sentinel.
func busMigrationFrozenIdentities(agent *agentv1alpha1.PlatformAgent) []a2aIdentity {
	return append(busMigrationIdentities(agent), busMigrationSentinel(agent, agent.Status.BusMigration))
}

// a2aBusFreezeAnnotation marks a pod template rendered for a run's write
// freeze, with the run's ID: on the callout, the template whose replicas
// must all be serving the frozen map before the bus restarts into the
// freeze; on the bus, the template that restart runs.
const a2aBusFreezeAnnotation = "kubeagents.x-k8s.io/bus-freeze"

// busMigrationFrozenMapVersion is the version of the freeze's identity map.
// It takes no profiles (the frozen map has none), so the callout's builder
// can render it. A render that fails yields a version no map has, so no
// callout replica becomes ready on it and the freeze fails at the deadline
// rather than going ahead unconfirmed.
func busMigrationFrozenMapVersion(agent *agentv1alpha1.PlatformAgent) string {
	doc, err := renderA2AAuthMap(agent, nil)
	if err != nil {
		return "unrenderable"
	}
	return doc.Version
}

// busMigrationFreezeCallout renders the callout for the write freeze: its
// pod template carries the run's ID, so every replica is replaced, and the
// frozen map's version as the one its readiness requires
// (A2A_EXPECT_MAP_VERSION), so a ready replica is one serving the frozen
// map itself. Outside the freeze it does nothing.
func busMigrationFreezeCallout(agent *agentv1alpha1.PlatformAgent, dep *appsv1.Deployment) {
	if !busMigrationFreezeActive(agent) {
		return
	}
	tmpl := &dep.Spec.Template
	if tmpl.Annotations == nil {
		tmpl.Annotations = map[string]string{}
	}
	tmpl.Annotations[a2aBusFreezeAnnotation] = agent.Status.BusMigration.RunID
	for i := range tmpl.Spec.Containers {
		tmpl.Spec.Containers[i].Env = append(tmpl.Spec.Containers[i].Env,
			corev1.EnvVar{Name: a2aCalloutExpectMapVersionEnv, Value: busMigrationFrozenMapVersion(agent)})
	}
}

// a2aCalloutExpectMapVersionEnv mirrors the callout's EnvExpectMapVersion
// (a2a/authcallout/status.go); the modules cannot import each other.
const a2aCalloutExpectMapVersionEnv = "A2A_EXPECT_MAP_VERSION"

// busMigrationCalloutServesTheFreeze reports every callout replica serving
// the freeze's map: at least the Deployment's desired count of pods carry the
// run's annotation and are Ready (which, with A2A_EXPECT_MAP_VERSION, means
// serving the frozen map's exact version), and no pod without it is left,
// terminating or not, since a pod on the old template may still answer an
// authorization request from the old map.
func (r *PlatformAgentReconciler) busMigrationCalloutServesTheFreeze(ctx context.Context, agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus) (bool, error) {
	dep := &appsv1.Deployment{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aCalloutName(agent)}, dep); err != nil {
		return false, client.IgnoreNotFound(err)
	}
	desired := ptr.Deref(dep.Spec.Replicas, 1)
	pods := &corev1.PodList{}
	if err := r.List(ctx, pods, client.InNamespace(agent.Namespace), client.MatchingLabels(a2aCalloutPodSelector(agent))); err != nil {
		return false, err
	}
	var serving int32
	for i := range pods.Items {
		pod := &pods.Items[i]
		if pod.Annotations[a2aBusFreezeAnnotation] != run.RunID {
			return false, nil
		}
		if pod.DeletionTimestamp == nil && podReady(pod) {
			serving++
		}
	}
	return serving >= desired, nil
}

// podReady reports a pod's Ready condition true.
func podReady(pod *corev1.Pod) bool {
	for _, c := range pod.Status.Conditions {
		if c.Type == corev1.PodReady {
			return c.Status == corev1.ConditionTrue
		}
	}
	return false
}

// busMigrationFreezeConfigured reports the bus rendered for the freeze:
// the freeze is in force and the callout confirmed serving its map. Only
// then does nats.conf drop the APP account's static users, and the bus's
// pod template carry the run's ID, so the restart into the freeze begins
// after every replica refuses everyone but the migration.
func busMigrationFreezeConfigured(agent *agentv1alpha1.PlatformAgent) bool {
	return busMigrationFreezeActive(agent) && agent.Status.BusMigration.FreezeConfirmed
}

// busNATSFrozenFor reports the bus StatefulSet's template rendered for the
// run's freeze ("" for one rendered without it).
func busNATSFrozenFor(sts *appsv1.StatefulSet) string {
	return sts.Spec.Template.Annotations[a2aBusFreezeAnnotation]
}

const (
	// a2aBusMigrationRetryAnnotation, set on the PlatformAgent to
	// "<run>-<attempt>", retries a Failed run: the way back it failed on
	// (RollingBack, from the start of it, or Unpinning) runs again with a
	// fresh deadline, with the same identity, snapshot and digests. The value
	// names the attempt, so each retry is its own deliberate act and an
	// annotation left in place never retries twice.
	a2aBusMigrationRetryAnnotation = "kubeagents.x-k8s.io/bus-migration-retry"
	// a2aBusMigrationAbandonAnnotation, set to the run's ID, gives a Failed
	// run up: its freeze lifts, and streams it did not put back are
	// recreated empty by the provision Job.
	a2aBusMigrationAbandonAnnotation = "kubeagents.x-k8s.io/bus-migration-abandon"
	// busMigrationMaxRetries bounds the retries of one run.
	busMigrationMaxRetries = 3
)

// busMigrationRetryPhase is where a retry of a run that failed in failedIn
// starts, "" when that failure has no retry: the two ways back, from their
// start.
func busMigrationRetryPhase(failedIn busMigrationPhase) busMigrationPhase {
	switch failedIn {
	case busMigrationRollingBack, busMigrationRollbackRestoring:
		return busMigrationRollingBack
	case busMigrationUnpinning:
		return busMigrationUnpinning
	}
	return ""
}

// busMigrationRetryValue is the retry annotation's value for a Failed run's
// next attempt.
func busMigrationRetryValue(run *agentv1alpha1.BusMigrationStatus) string {
	return fmt.Sprintf("%s-%d", run.RunID, len(run.Retries)+1)
}

// busMigrationActOnFailed applies a person's decision on a Failed run, read
// from the PlatformAgent's annotations: a retry naming its next attempt, while
// attempts remain, puts it back on its way back with a fresh deadline; an
// abandon naming the run gives it up. Anything else, an annotation naming
// another run or attempt included, is ignored. gaveUp reports a run given up
// on this call, whose stage and run Secret are then no longer kept.
func busMigrationActOnFailed(agent *agentv1alpha1.PlatformAgent, now metav1.Time) (gaveUp bool) {
	run := agent.Status.BusMigration
	if run == nil || busMigrationPhase(run.Phase) != busMigrationFailed || run.GivenUp {
		return false
	}
	if agent.Annotations[a2aBusMigrationAbandonAnnotation] == run.RunID {
		run.GivenUp = true
		return true
	}
	retry := busMigrationRetryPhase(busMigrationPhase(run.FailedIn))
	if retry == "" || len(run.Retries) >= busMigrationMaxRetries || agent.Annotations[a2aBusMigrationRetryAnnotation] != busMigrationRetryValue(run) {
		return false
	}
	run.Retries = append(run.Retries, agentv1alpha1.BusMigrationRetry{At: now, FailedIn: run.FailedIn, Cause: run.Cause})
	run.Phase, run.FailedIn, run.Cause, run.Message = string(retry), "", "", ""
	run.Deadline = metav1.NewTime(now.Add(busMigrationRunDeadline))
	return false
}

// busMigrationFailedActions is what the Failed condition tells a person
// they can do, with the exact annotations.
func busMigrationFailedActions(agent *agentv1alpha1.PlatformAgent) string {
	run := agent.Status.BusMigration
	if run.GivenUp {
		return " It was given up: the freeze is lifted, and streams it did not put back are recreated empty."
	}
	var b strings.Builder
	if busMigrationRetryPhase(busMigrationPhase(run.FailedIn)) != "" {
		if left := busMigrationMaxRetries - len(run.Retries); left > 0 {
			fmt.Fprintf(&b, " To retry (%d of %d left): annotate the PlatformAgent %s=%s.", left, busMigrationMaxRetries, a2aBusMigrationRetryAnnotation, busMigrationRetryValue(run))
		} else {
			b.WriteString(" No retries are left.")
		}
	}
	fmt.Fprintf(&b, " To give it up: annotate the PlatformAgent %s=%s", a2aBusMigrationAbandonAnnotation, run.RunID)
	if busMigrationFreezeActive(agent) {
		b.WriteString(", which lifts the write freeze and recreates every stream the run did not put back, empty")
	}
	b.WriteString(". Until then no other migration starts, and spec changes leave the bus as it is.")
	return b.String()
}
