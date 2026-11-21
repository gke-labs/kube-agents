package controller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// a2aBusReshapeAnnotation, set on the PlatformAgent to any value (a
// nonce), asks for TASKS to be recreated at the max_consumers the CR
// renders: JetStream never changes a stream's max_consumers after it is
// created, so the only way to widen it, keeping its history, is a snapshot
// and a restore with the wider config, behind a write freeze. Never
// automatic: the freeze is a person's to schedule.
const a2aBusReshapeAnnotation = "kubeagents.x-k8s.io/bus-reshape"

// a2aBusReshapeConsumedAnnotation, on the bus StatefulSet, records the
// last bus-reshape request a run was started for (busReshapeNonceHash),
// like #5708's restart record: on the StatefulSet because a later run of
// any leg replaces status.busMigration, and with it the run ID's record,
// while the StatefulSet's annotations stay. A request it names never
// starts a second reshape, whatever ran since: without it, a value left on
// the CR after a later run that left TASKS as it was, and a render that
// then grew, would start a reshape, and a write freeze, no one asked for.
const a2aBusReshapeConsumedAnnotation = "kubeagents.x-k8s.io/a2a-reshape-consumed"

// reasonA2ABusReshapeNotStarted is the Event for a reshape request that
// started nothing, and why.
const reasonA2ABusReshapeNotStarted = "A2ABusReshapeNotStarted"

// busReshapeRunSuffix is the part of a reshape run's ID that records the
// request it answered: "-r" and the first 8 hex of the nonce's SHA-256. The
// run ID is the one place a record survives an operator rolled back to one
// that does not know the field (#6479's hold keeps the run as it is, but an
// older operator's status writes drop fields it does not know).
func busReshapeRunSuffix(nonce string) string {
	return "-r" + busReshapeNonceHash(nonce)
}

// busReshapeNonceHash is the first 8 hex of the request's SHA-256, the form
// both records keep it in.
func busReshapeNonceHash(nonce string) string {
	sum := sha256.Sum256([]byte(nonce))
	return hex.EncodeToString(sum[:])[:busReshapeNonceHashLength]
}

// busReshapeNonceHashLength is how many hex digits of the request's hash
// the records keep: enough that two values an admin sets don't collide, and
// short enough for the run ID, which names the run's objects.
const busReshapeNonceHashLength = 8

// busReshapeConsumed reports whether the bus StatefulSet records nonce as
// already started (a2aBusReshapeConsumedAnnotation). A StatefulSet that
// can't be read records nothing.
func (r *PlatformAgentReconciler) busReshapeConsumed(ctx context.Context, agent *agentv1alpha1.PlatformAgent, nonce string) bool {
	sts := &appsv1.StatefulSet{}
	if err := r.a2aReader().Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, sts); err != nil {
		return false
	}
	return sts.Annotations[a2aBusReshapeConsumedAnnotation] == busReshapeNonceHash(nonce)
}

// recordBusReshapeConsumed writes nonce's hash to the bus StatefulSet's
// record, if it isn't there already.
func (r *PlatformAgentReconciler) recordBusReshapeConsumed(ctx context.Context, agent *agentv1alpha1.PlatformAgent, nonce string) error {
	sts := &appsv1.StatefulSet{}
	if err := r.a2aReader().Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, sts); err != nil {
		return client.IgnoreNotFound(err)
	}
	want := busReshapeNonceHash(nonce)
	if sts.Annotations[a2aBusReshapeConsumedAnnotation] == want {
		return nil
	}
	patch := client.MergeFrom(sts.DeepCopy())
	if sts.Annotations == nil {
		sts.Annotations = map[string]string{}
	}
	sts.Annotations[a2aBusReshapeConsumedAnnotation] = want
	return r.Patch(ctx, sts, patch)
}

// busReshapeServers is the count a reshape runs at: the count the bus runs,
// as this pass resolved it. A reshape never changes it.
func busReshapeServers(agent *agentv1alpha1.PlatformAgent) int {
	if n := int(agent.Status.BusReplicas); n == 1 || n == 3 {
		return n
	}
	return a2aNATSReplicas(agent)
}

// busReshapeRefusal says why a reshape asked for with nonce can't start, or
// "" when it can, and kind, a short name for the reason that keys its Event.
// In order:
//   - the request was answered already, which the last run's ID records
//     (busReshapeRunSuffix), whether that run is still under way or not,
//     and the bus StatefulSet records after any later run has replaced
//     that one (a2aBusReshapeConsumedAnnotation);
//   - a run under way, or a Failed run not given up;
//   - the bus must be provisioned, and the operator must have its own image
//     to run the work;
//   - the served CRD must keep status.busMigration, or a run started here
//     would be gone by the next pass;
//   - the live TASKS must be narrower than the CR renders: once a reshape
//     (or a one to three migration) has widened TASKS, a value left in place
//     finds it at the width and does nothing. The records above cover the
//     case this guard alone does not (a later run that left TASKS as it
//     was, then a render that grew). A TASKS with no limit is never
//     narrower;
//   - TASKS must fit the limits the restore puts on it, as one to three
//     checks every stream.
func (r *PlatformAgentReconciler) busReshapeRefusal(ctx context.Context, agent *agentv1alpha1.PlatformAgent, nonce string) (refusal, kind string, answered bool) {
	if run := agent.Status.BusMigration; run != nil {
		if busMigrationLeg(run.Leg) == busMigrationReshape && strings.HasSuffix(run.RunID, busReshapeRunSuffix(nonce)) {
			return "", "", true
		}
	}
	if r.busReshapeConsumed(ctx, agent, nonce) {
		return "", "", true
	}
	if run := agent.Status.BusMigration; run != nil {
		phase := busMigrationPhase(run.Phase)
		if !busMigrationTerminal(phase) {
			return "a bus migration is under way", "running", false
		}
		if phase == busMigrationFailed && !run.GivenUp {
			return "the last bus migration failed and waits for a person (retry or abandon it first)", "failed", false
		}
	}
	refusedForWidth := false
	if !busProvisioned(agent) {
		if refusedForWidth = r.busProvisionRefusedOnlyForWidth(ctx, agent); !refusedForWidth {
			return "the bus is not provisioned yet", "unprovisioned", false
		}
	}
	if busMigrationImage() == "" {
		return "this operator was deployed without OPERATOR_IMAGE, which runs the reshape's work", "image", false
	}
	if r.busMigrationStatusPruned(agent) {
		return "the served PlatformAgent CRD drops status.busMigration, so a run started here would be lost; apply this release's CRD, then restart the operator", "pruned", false
	}
	streams, err := r.a2aBusStreamState(ctx, agent)
	if err != nil {
		return "TASKS's width could not be read (" + err.Error() + ")", "unreadable", false
	}
	if refusedForWidth {
		// The Job's exit 2 says the script reached its last gate; the bus
		// itself is the proof: every stream and bucket the Job creates is
		// there. A shell error can exit 2 too, and then something is
		// missing.
		for _, name := range a2aProvisionedStreams {
			if _, ok := streams[name]; !ok {
				return "the bus is not provisioned yet (" + name + " is missing)", "unprovisioned", false
			}
		}
	}
	tasks, ok := streams["TASKS"]
	if !ok {
		return "the bus has no TASKS stream", "no-tasks", false
	}
	rendered := a2aTasksMaxConsumers(agent)
	switch {
	case tasks.MaxConsumers == 0:
		return "the bus did not report TASKS's max_consumers", "unreported", false
	case tasks.MaxConsumers < 0 || tasks.MaxConsumers >= rendered:
		return fmt.Sprintf("TASKS already holds max_consumers %d, at least the %d this PlatformAgent renders", tasks.MaxConsumers, rendered), "wide", false
	}
	if over := busMigrationStreamsOverCapFrom(agent, streams, []string{"TASKS"}); over != "" {
		return over, "over-cap", false
	}
	return "", "", false
}

// busProvisionRefusedOnlyForWidth reports a bus whose current provision Job
// (this render's name, this CR's, not being deleted) failed by its
// podFailurePolicy: the script's exit 2, which by its own convention is
// only the closing max_consumers gate, reached once every stream and bucket
// exists and the one limit is short. busReshapeRefusal also checks the bus
// holds every stream, since a shell error can exit 2 as well. That is the state a reshape
// fixes, so it counts as provisioned for one, though BusProvisioned is not
// set: a reshape given up before its clear (or whose rollback put a narrow
// TASKS back) drops the condition, and the provision Job that runs next
// refuses the narrow TASKS, so the condition would never come back and no
// reshape could start (#6598). A Job failed any other way (its backoff
// spent: the bus down, a stream missing) is not this, and refuses as before.
func (r *PlatformAgentReconciler) busProvisionRefusedOnlyForWidth(ctx context.Context, agent *agentv1alpha1.PlatformAgent) bool {
	var jobs batchv1.JobList
	if err := r.a2aReader().List(ctx, &jobs, client.InNamespace(agent.Namespace), client.MatchingLabels{
		a2aComponentLabel: a2aProvisionComponent,
		labelInstance:     instanceLabel(agent.Namespace, agent.Name),
	}); err != nil {
		return false
	}
	current := buildA2AProvisionJob(agent).Name
	for i := range jobs.Items {
		job := &jobs.Items[i]
		if job.Name != current || !metav1.IsControlledBy(job, agent) || job.DeletionTimestamp != nil {
			continue
		}
		for _, c := range job.Status.Conditions {
			if c.Type == batchv1.JobFailed && c.Status == corev1.ConditionTrue && c.Reason == batchv1.JobReasonPodFailurePolicy {
				return true
			}
		}
	}
	return false
}

// maybeStartBusReshape starts a reshape when the annotation asks for one
// that busReshapeRefusal allows, persisted before the pass goes on as any
// run's start is. A request it refuses gets one Normal Event per nonce and
// kind of reason (an unreadable bus whose error text varies is one), from
// this operator process (a restart may repeat it once).
func (r *PlatformAgentReconciler) maybeStartBusReshape(ctx context.Context, agent *agentv1alpha1.PlatformAgent, running int) error {
	nonce := agent.Annotations[a2aBusReshapeAnnotation]
	if nonce == "" {
		return nil
	}
	refusal, kind, answered := r.busReshapeRefusal(ctx, agent, nonce)
	if answered {
		// The run that answered it writes the StatefulSet's record; a pass
		// that started it and failed to write it writes it now.
		if run := agent.Status.BusMigration; run != nil && busMigrationLeg(run.Leg) == busMigrationReshape && strings.HasSuffix(run.RunID, busReshapeRunSuffix(nonce)) {
			return r.recordBusReshapeConsumed(ctx, agent, nonce)
		}
		return nil
	}
	if refusal != "" {
		key, seen := client.ObjectKeyFromObject(agent), nonce+"/"+kind
		if prev, ok := r.busReshapeRefused.Load(key); !ok || prev != seen {
			r.busReshapeRefused.Store(key, seen)
			r.recordEvent(agent, corev1.EventTypeNormal, reasonA2ABusReshapeNotStarted,
				fmt.Sprintf("%s=%s starts no TASKS reshape: %s", a2aBusReshapeAnnotation, nonce, refusal))
		}
		return nil
	}
	if err := r.persistBusMigrationStart(ctx, agent, func() { startBusReshape(agent, running, nonce, metav1.Now()) }); err != nil {
		return err
	}
	// Recorded only for a run the API server kept: on a served CRD that
	// drops status.busMigration the write came back without it, the run is
	// gone (persistBusMigrationStart put the old record back), and a record
	// here would read the request as answered with nothing to show for it.
	// The next pass refuses it instead, naming the CRD (#6600).
	if run := agent.Status.BusMigration; run == nil || busMigrationLeg(run.Leg) != busMigrationReshape || !strings.HasSuffix(run.RunID, busReshapeRunSuffix(nonce)) {
		return nil
	}
	return r.recordBusReshapeConsumed(ctx, agent, nonce)
}

// startBusReshape records a reshape run at the count the bus runs, its ID
// carrying the request it answers.
func startBusReshape(agent *agentv1alpha1.PlatformAgent, running int, nonce string, now metav1.Time) {
	agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{
		Leg:                string(busMigrationReshape),
		RunID:              fmt.Sprintf("%d%s", agent.Generation, busReshapeRunSuffix(nonce)),
		Phase:              string(busMigrationFreezing),
		StartedAt:          now,
		Deadline:           metav1.NewTime(now.Add(busMigrationRunDeadline)),
		ObservedGeneration: agent.Generation,
	}
	agent.Status.BusReplicas = int32(running) // #nosec G115 -- 1 or 3
}

// reshapeCondition is A2ABusMigrating for a reshape.
func reshapeCondition(agent *agentv1alpha1.PlatformAgent, run *agentv1alpha1.BusMigrationStatus) metav1.Condition {
	phase := busMigrationPhase(run.Phase)
	switch phase {
	case busMigrationAbandoned:
		return metav1.Condition{Type: a2aBusMigratingConditionType, Status: metav1.ConditionFalse, Reason: "Abandoned",
			Message: fmt.Sprintf("the TASKS reshape %s did not complete and was undone (%s); TASKS is as it was, with its data. To try again, set %s to a new value.", run.RunID, run.Cause, a2aBusReshapeAnnotation)}
	case busMigrationFailed:
		return metav1.Condition{Type: a2aBusMigratingConditionType, Status: metav1.ConditionFalse, Reason: "Failed",
			Message: fmt.Sprintf("the TASKS reshape %s stopped (%s): %s.%s", run.RunID, run.Cause, run.Message, busMigrationFailedActions(agent))}
	}
	msg := fmt.Sprintf("recreating TASKS at the width this PlatformAgent renders (phase %s); writes are frozen: only the reshape reaches the bus, and the console and web logins are off until it ends", phase)
	if run.Abandoning {
		msg = fmt.Sprintf("undoing the TASKS reshape (phase %s); writes are frozen until it ends", phase)
	}
	return metav1.Condition{Type: a2aBusMigratingConditionType, Status: metav1.ConditionTrue, Reason: "InProgress", Message: msg}
}
