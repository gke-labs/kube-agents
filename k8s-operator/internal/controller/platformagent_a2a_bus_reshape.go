package controller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"

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

// reasonA2ABusReshapeNotStarted is the Event for a reshape request that
// started nothing, and why.
const reasonA2ABusReshapeNotStarted = "A2ABusReshapeNotStarted"

// busReshapeRunSuffix is the part of a reshape run's ID that records the
// request it answered: "-r" and the first 8 hex of the nonce's SHA-256. The
// run ID is the one place a record survives an operator rolled back to one
// that does not know the field (#6479's hold keeps the run as it is, but an
// older operator's status writes drop fields it does not know).
func busReshapeRunSuffix(nonce string) string {
	sum := sha256.Sum256([]byte(nonce))
	return "-r" + hex.EncodeToString(sum[:])[:8]
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
//     (busReshapeRunSuffix), whether that run is still under way or not;
//   - a run under way, or a Failed run not given up;
//   - the bus must be provisioned, and the operator must have its own image
//     to run the work;
//   - the served CRD must keep status.busMigration, or a run started here
//     would be gone by the next pass;
//   - the live TASKS must be narrower than the CR renders. This is the
//     primary guard, the run ID's record the secondary one: once a reshape
//     (or a one to three migration) has widened TASKS, a value left in place
//     finds it at the width and does nothing, even after a later run has
//     replaced the record of the one that answered it. It is not a
//     complete guard: if that later run left TASKS as it was and the render
//     then grew (maxSessions raised), the old value would start a reshape
//     no one asked for again. So the docs say to remove the annotation once
//     the reshape is done. A TASKS with no limit is never narrower;
//   - TASKS must fit the limits the restore puts on it, as one to three
//     checks every stream.
func (r *PlatformAgentReconciler) busReshapeRefusal(ctx context.Context, agent *agentv1alpha1.PlatformAgent, nonce string) (refusal, kind string, answered bool) {
	if run := agent.Status.BusMigration; run != nil {
		if busMigrationLeg(run.Leg) == busMigrationReshape && strings.HasSuffix(run.RunID, busReshapeRunSuffix(nonce)) {
			return "", "", true
		}
		phase := busMigrationPhase(run.Phase)
		if !busMigrationTerminal(phase) {
			return "a bus migration is under way", "running", false
		}
		if phase == busMigrationFailed && !run.GivenUp {
			return "the last bus migration failed and waits for a person (retry or abandon it first)", "failed", false
		}
	}
	if !busProvisioned(agent) {
		return "the bus is not provisioned yet", "unprovisioned", false
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
	if over := r.busMigrationStreamsOverCapIn(ctx, agent, []string{"TASKS"}); over != "" {
		return over, "over-cap", false
	}
	return "", "", false
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
	return r.persistBusMigrationStart(ctx, agent, func() { startBusReshape(agent, running, nonce, metav1.Now()) })
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
