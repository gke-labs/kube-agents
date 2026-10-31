package controller

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
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
func (r *PlatformAgentReconciler) observeBusMigration(ctx context.Context, agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus) (busMigrationOutcome, bool, error) {
	leg, phase := busMigrationLeg(run.Leg), busMigrationPhase(run.Phase)
	job := func(step string) (busMigrationOutcome, bool, error) {
		j := &batchv1.Job{}
		switch err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: busMigrationWorkloadName(agent, step, run.RunID)}, j); {
		case errors.IsNotFound(err):
			return busMigrationPending, false, nil
		case err != nil:
			return busMigrationPending, false, err
		}
		return jobOutcome(j), jobDeadlineExceeded(j), nil
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
			return busMigrationPending, false, nil
		case err != nil:
			return busMigrationPending, false, err
		}
		switch pod.Status.Phase {
		case corev1.PodSucceeded:
			return busMigrationSucceeded, false, nil
		case corev1.PodFailed:
			return busMigrationErred, false, nil
		}
		return busMigrationPending, false, nil
	case busMigrationFreezing, busMigrationShrinking, busMigrationConverting, busMigrationRollingBack, busMigrationReleasing:
		sts := &appsv1.StatefulSet{}
		switch err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, sts); {
		case errors.IsNotFound(err):
			return busMigrationPending, false, nil
		case err != nil:
			return busMigrationPending, false, err
		}
		if busNATSRolledOutAt(sts, int32(busMigrationReplicas(leg, phase, run.Abandoning))) { // #nosec G115 -- 1 or 3
			return busMigrationSucceeded, false, nil
		}
		return busMigrationPending, false, nil
	}
	return busMigrationPending, false, nil
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
		if outcome == busMigrationErred && run.Cause == "" {
			run.Cause = busMigrationCause(run, late || observed == busMigrationPending)
		}
		step := nextBusMigrationPhase(leg, busMigrationPhase(run.Phase), run.Abandoning, outcome)
		run.Phase, run.Abandoning = string(step.next), step.abandoning
		if step.manual != "" {
			run.Message = step.manual
		}
		if err := r.ensureBusMigrationWorkload(ctx, agent, run, now); err != nil {
			return 0, err
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
			Message: fmt.Sprintf("the migration from %d NATS servers to %d did not complete and was undone (%s); the bus runs %d servers with its data. Change the spec to try again.", from, to, run.Cause, from)}, true
	case busMigrationFailed:
		return metav1.Condition{Type: a2aBusMigratingConditionType, Status: metav1.ConditionFalse, Reason: "Failed",
			Message: fmt.Sprintf("the migration from %d NATS servers to %d stopped (%s): %s", from, to, run.Cause, run.Message)}, true
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
// principal only while its Job runs, so a token left behind authenticates
// nothing once the phase has moved on. Each is its own ServiceAccount, named
// for the run, and its grants are the JetStream API verbs its leg sends, on
// the provisioned streams by name and nothing else; it subscribes to its own
// inbox only. The restore pod's principal is not here yet.
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
	case busMigrationSnapshot:
		step, verbs = "snapshot", []string{"INFO", "SNAPSHOT"}
		// A snapshot's chunks arrive on the run's inbox and are acked here.
		extra = func(stream string) []string { return []string{"$JS.SNAPSHOT.ACK." + stream + ".>"} }
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
		comment:        "the bus migration's " + step + " step for run " + run.RunID + "; present only while that step runs",
		auth:           a2aAuthCallout,
		serviceAccount: a2aServiceAccountName(agent.Namespace, busMigrationWorkloadName(agent, step, run.RunID)),
		publish:        publish,
		subscribe:      []string{"_INBOX." + user + ".>"},
		// The callout refuses this principal a grace period after the run's
		// deadline and mints it no JWT that outlives that. The grace keeps
		// the credential from being what stops a slow run: the Job's own
		// deadline and the operator's fire first, and say so in Cause.
		notAfter: ptr.To(run.Deadline.Add(busMigrationCredentialGrace).UTC()),
	}}
}

// busMigrationPrincipal is a migration step's bus user, which is also its
// inbox prefix.
func busMigrationPrincipal(step, runID string) string {
	return "migrate-" + step + "-" + runID
}

const (
	// busMigrationCredentialGrace is how long a run's principals outlive its
	// deadline.
	busMigrationCredentialGrace = 10 * time.Minute
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
func busMigrationPodSpec(agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus, step string, command []string, volumes []corev1.Volume, mounts []corev1.VolumeMount) corev1.PodSpec {
	return corev1.PodSpec{
		ServiceAccountName:           busMigrationWorkloadName(agent, step, run.RunID),
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

// buildBusMigrationJob is a run's pin, unpin or snapshot Job: bus-migrate
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
	case "snapshot":
		command = append(command, "--dir", busMigrationStageMount)
		volumes = append(volumes, corev1.Volume{Name: busMigrationStageVolume, VolumeSource: corev1.VolumeSource{
			PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: busMigrationStageClaim(agent, run.RunID)},
		}})
		mounts = append(mounts, corev1.VolumeMount{Name: busMigrationStageVolume, MountPath: busMigrationStageMount})
	}
	deadline := int64(run.Deadline.Sub(now).Seconds())
	if deadline < 1 {
		deadline = 1
	}
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      busMigrationWorkloadName(agent, step, run.RunID),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "bus-migration"),
		},
		Spec: batchv1.JobSpec{
			BackoffLimit:            ptr.To(int32(2)),
			ActiveDeadlineSeconds:   ptr.To(deadline),
			TTLSecondsAfterFinished: ptr.To(int32(86400)),
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: a2aLabels(agent, "bus-migration")},
				Spec:       busMigrationPodSpec(agent, run, step, command, volumes, mounts),
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
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      busMigrationWorkloadName(agent, "hash", run.RunID),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "bus-migration"),
		},
		Spec: busMigrationPodSpec(agent, run, "hash", command, volumes, mounts),
	}
}

// busMigrationPhaseWorkload names the workload a phase runs, "" for the
// phases that run none of their own (rollouts, and the restore pod's, which
// is on hold).
func busMigrationPhaseWorkload(phase busMigrationPhase) string {
	switch phase {
	case busMigrationPinning:
		return "pin"
	case busMigrationUnpinning:
		return "unpin"
	case busMigrationSnapshot:
		return "snapshot"
	case busMigrationHashing:
		return "hash"
	}
	return ""
}

// ensureBusMigrationWorkload creates the current phase's ServiceAccount and
// then its Job or pod, both owned by the CR, if they are not there yet. The
// ServiceAccount is the identity the callout resolves the pod's token to, so
// it comes first; it holds no RBAC. Nothing is created without
// OPERATOR_IMAGE, and the phase then waits out the run's deadline.
func (r *PlatformAgentReconciler) ensureBusMigrationWorkload(ctx context.Context, agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus, now time.Time) error {
	step := busMigrationPhaseWorkload(busMigrationPhase(run.Phase))
	if step == "" || busMigrationImage() == "" {
		return nil
	}
	name := busMigrationWorkloadName(agent, step, run.RunID)
	if err := ReconcileServiceAccount(ctx, r.Client, r.Scheme, agent, name, agent.Namespace, nil, commonLabels(agent), fieldOwner); err != nil {
		return err
	}
	var obj client.Object
	if step == "hash" {
		obj = buildBusHashPod(agent, run)
	} else {
		obj = buildBusMigrationJob(agent, run, step, now)
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
