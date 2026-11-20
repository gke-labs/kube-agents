/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The session cap on the session route, fitted to the TASKS stream's live
// width.
//
// All of this runs only on the session route, which an unset
// spec.harness.tuning.defaultRoute resolves to. On `platform`, the opt-out,
// nothing here runs: no read of the bus, no provision Job wait, and the session cap is
// maxSessions. The one write there is clearing a cap and condition an earlier
// opt-in left (clearA2ASessionCap); an install that never opted in has
// neither and is not written.
//
// On the session route every conversation's task is a session pod, and each
// one needs a2aTasksConsumersPerSessionOnSessionRoute consumers on TASKS. A
// stream holds the max_consumers it was created with: nats-server refuses to
// change it on a stream that exists. An install whose TASKS an earlier
// operator created for the platform route has room for fewer sessions than
// maxSessions asks for, and a gateway allowed maxSessions would spawn pods
// past the point where their consumers can be created. So before anything is
// rendered, resolveA2ASessionCap reads the stream's max_consumers off the
// bus's monitoring endpoint (a2aBusStreamState) and writes
// status.effectiveSessionCap: maxSessions, or the sessions the stream fits
// (a2aSessionsFit) when that is fewer. The gateway's cap, the session
// ResourceQuota and the provision Job's consumer check render from it
// (resolveA2AEffectiveSessions); a cap below 1 sends plain conversations to
// platform (resolveA2ADefaultRoute).
//
// What a pass that cannot read the width does depends on what it has. With a
// cap in status it keeps that cap. On a bus never provisioned (neither a
// BusProvisioned nor a migration record, a2aBusExisted) it takes
// maxSessions: the provision Job will create TASKS at the width maxSessions
// needs (a2aTasksMaxConsumers), so there is
// nothing to fit, and a fresh install renders the spec's route from its first
// pass. Only a provisioned bus with no stored cap, an existing install's
// first pass after the upgrade or after it opts in, waits: it renders what
// the operator before sessions became the default rendered (platform routing
// where the field is unset, at the configured cap), holds the provision Job,
// and says so in the
// SessionCapFitted condition; the reconcile's requeue for the held Job brings
// the pass that reads it. "No TASKS stream" counts as a reading only on a bus
// never provisioned. On a provisioned bus a missing TASKS is a server that has
// not caught up, or a stream deleted for recreation, and the pass treats it as
// unreadable.
//
// A stored cap's reading is reused for a2aSessionCapReadingTTL, so a settled
// install does not make a blocking GET of the bus on every pass. Under a
// served CRD that prunes status.effectiveSessionCap the stored cap is always
// absent; the echo of the write says so (noteSessionCapEcho), and for
// a2aSessionCapPrunedReprobe the pass keeps the cap it last rendered rather
// than falling back, and does not write it.
const (
	// sessionCapFittedConditionType is written only while the cap is below
	// maxSessions, the install fell back to platform, or the cap is waiting
	// on its first reading; absent otherwise. Like SizingCappedByQuota its
	// message carries no quantities, so the condition is written when the
	// state changes, not when a figure does: the figures are in
	// status.effectiveSessionCap and in the Warning beside it.
	sessionCapFittedConditionType = "SessionCapFitted"
	// sessionCapNarrowReason: the cap is fitted below maxSessions.
	sessionCapNarrowReason = "TasksStreamNarrow"
	// sessionCapNoRoomReason: TASKS has no room for one session, so plain
	// conversations go to platform.
	sessionCapNoRoomReason = "NoRoomForASession"
	// sessionCapWaitingReason: the bus's width has not been read yet, so
	// the provision Job waits and routing stays as it was.
	sessionCapWaitingReason = "WaitingOnBusWidth"
	// sessionCapClusterViewReason: the operator's A2A_SESSION_CLUSTER_VIEW is
	// on, so the session default does not engage and plain turns stay with
	// the platform agent (resolveA2ADefaultRoute).
	sessionCapClusterViewReason = "SessionClusterViewOn"

	// sessionCapReadingTTL is how long a reading of the bus's TASKS width is
	// reused while a cap is stored: the bus stream poller's period. A stream
	// is widened rarely and by hand, so a pass that sees it a poll late costs
	// nothing; a GET on every pass costs a blocking round trip each time.
	sessionCapReadingTTL = busStreamsPollInterval
	// sessionCapPrunedReprobe is how long a write the served CRD pruned keeps
	// the cap unwritten before the next attempt, the status.usage cadence.
	sessionCapPrunedReprobe = usageStatusReprobeInterval

	// sessionCapRemedy is what lifts the fit: a wider TASKS. A live stream's
	// max_consumers can't change in place. On a one-server bus, moving to
	// busReplicas: 3 restores TASKS at the larger of its width and the
	// rendered one, history kept (gke-labs/kube-agents#6462); on any bus the
	// planned opt-in reshape will.
	sessionCapRemedy = "the cap stays fitted until TASKS is widened: on a one-server bus, setting " +
		"spec.harness.tuning.busReplicas to 3 widens it and keeps its history, and the planned opt-in TASKS " +
		"reshape (gke-labs/kube-agents#6466) will widen it on any bus"
	sessionCapClusterViewMessage = "the operator's " + a2aSessionClusterViewEnvVar + " is on, so the session pods' temporary cluster view is still rendered " +
		"and the session default is not engaged: plain conversations stay with platform, whatever spec.harness.tuning.defaultRoute says, " +
		"so the default never widens what a plain turn reaches; the view's retirement is tracked in gke-labs/kube-agents#6483"
	sessionCapNarrowMessage = "the TASKS stream has room for fewer sessions than spec.harness.tuning.maxSessions on the session route, " +
		"and a stream's max_consumers cannot change once it exists, so the session cap is fitted to it (status.effectiveSessionCap); " +
		sessionCapRemedy
	sessionCapNoRoomMessage = "the TASKS stream has no room for one session's consumers on the session route, " +
		"so plain conversations go to platform (status.effectiveSessionCap is 0); " + sessionCapRemedy
	sessionCapWaitingMessage = "the session cap waits on the A2A bus's TASKS width, which could not be read yet; " +
		"routing and the session cap stay as they were and the provision Job waits until it is read"

	// sessionCapEventFormat is the Warning recorded when the fit changes:
	// the live width, what maxSessions needs on the session route, the
	// per-session share and reserve behind it, and the fitted cap.
	sessionCapEventFormat = "TASKS holds max_consumers=%d; spec.harness.tuning.maxSessions=%d needs %d on the session route " +
		"(%d per session, %d reserved); the session cap is fitted to %d. "
	sessionCapNoRoomEventSuffix = "Not one session fits, so plain conversations go to platform. "
)

// a2aBusStreamStateDefault, when set, answers a2aBusStreamState for a
// reconciler whose busStreamState is unset. Nil in the operator; the
// package's TestMain sets it to a bus with an unlimited TASKS, so the
// hundreds of reconcilers built without a stream seam render the configured
// cap rather than dialing a monitoring endpoint no test runs.
var a2aBusStreamStateDefault func(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error)

// a2aTasksWidthReading is one reading of TASKS' width (a2aTasksLiveWidth),
// kept for reuse (sessionCapReadings).
type a2aTasksWidthReading struct {
	width   int
	present bool
	at      time.Time
}

// a2aTasksLiveWidth is TASKS' max_consumers as the bus reports it. known is
// false when the width can't be trusted from this pass: the read failed, a
// migration is reshaping the bus, the report carried no config for TASKS,
// or TASKS is missing from a bus that has been provisioned. present is
// false on a known reading of a bus with no TASKS yet. With reuse set (a cap
// is stored), a known reading younger than sessionCapReadingTTL answers
// without reading the bus.
func (r *PlatformAgentReconciler) a2aTasksLiveWidth(ctx context.Context, agent *agentv1alpha1.PlatformAgent, reuse bool) (width int, present, known bool) {
	if busMigrationHoldsProvision(agent) {
		return 0, false, false
	}
	key := client.ObjectKeyFromObject(agent)
	if v, ok := r.sessionCapReadings.Load(key); ok && reuse {
		if reading := v.(a2aTasksWidthReading); time.Since(reading.at) < sessionCapReadingTTL {
			return reading.width, reading.present, true
		}
	}
	streams, err := r.a2aBusStreamState(ctx, agent)
	if err != nil {
		logf.FromContext(ctx).Info("could not read the A2A bus's TASKS width for the session cap", "error", err.Error())
		return 0, false, false
	}
	tasks, ok := streams[a2aTasksStream]
	switch {
	case !ok && a2aBusExisted(agent):
		return 0, false, false
	case ok && tasks.MaxConsumers == nil:
		return 0, false, false
	case ok:
		width, present = *tasks.MaxConsumers, true
	}
	r.sessionCapReadings.Store(key, a2aTasksWidthReading{width: width, present: present, at: time.Now()})
	return width, present, true
}

// resolveA2ASessionCap writes status.effectiveSessionCap and the
// SessionCapFitted condition on agent, the render copy, before anything is
// rendered from it (see the block comment above). A change is patched to the
// stored status at once, as recordProfileSubmitMaxAge patches its field:
// nothing later in the pass writes a status field the Ready writer does not
// track. The patch's version and both fields are carried to instance too, so
// the pass's later status writes neither drop them nor conflict on them.
// Under a served CRD that prunes the field (sessionCapStatusPruned), the cap
// is set in memory for the render and not written.
func (r *PlatformAgentReconciler) resolveA2ASessionCap(ctx context.Context, agent, instance *agentv1alpha1.PlatformAgent) {
	if a2aSpecDefaultRoute(agent) != agentv1alpha1.DefaultRouteSession {
		// The platform route, the opt-out: no fit applies, and nothing is
		// read off the bus. This reconciler's memory of the route is
		// dropped, so an install that opts back in reads the bus afresh.
		key := client.ObjectKeyFromObject(agent)
		r.sessionCapWarned.Delete(key)
		r.sessionCapLast.Delete(key)
		r.sessionCapReadings.Delete(key)
		r.clearA2ASessionCap(ctx, agent, instance)
		return
	}
	pruned := r.sessionCapStatusPruned(agent)
	sessionCap, cond, event := r.fitA2ASessionCap(ctx, agent, pruned)
	key := client.ObjectKeyFromObject(agent)
	if event == "" {
		r.sessionCapWarned.Delete(key)
	} else if warned, ok := r.sessionCapWarned.Load(key); !ok || warned.(string) != event {
		r.recordEvent(agent, corev1.EventTypeWarning, sessionCapFittedConditionType, event)
		r.sessionCapWarned.Store(key, event)
	}
	if sessionCap != nil {
		r.sessionCapLast.Store(key, *sessionCap)
	} else {
		r.sessionCapLast.Delete(key)
	}
	if sessionCapCurrent(agent, sessionCap, cond, pruned) {
		if pruned {
			setSessionCap(agent, sessionCap, cond)
			if instance != nil {
				setSessionCap(instance, sessionCap, cond)
			}
		}
		return
	}
	original := agent.DeepCopy()
	patch := client.MergeFromWithOptions(original, client.MergeFromWithOptimisticLock{})
	setSessionCap(agent, sessionCap, cond)
	written := agent.DeepCopy()
	if pruned {
		// Not written while the served CRD drops it: the field stays as the
		// store has it, and only the condition goes out.
		written.Status.EffectiveSessionCap = original.Status.EffectiveSessionCap
	}
	if err := r.Status().Patch(ctx, written, patch); err != nil {
		// The render still follows this pass's reading; the next pass
		// writes it.
		logf.FromContext(ctx).Error(err, "could not record the A2A session cap; the next pass records it")
		return
	}
	if !pruned {
		r.noteSessionCapEcho(ctx, agent, sessionCap, written.Status.EffectiveSessionCap)
	}
	agent.ResourceVersion = written.ResourceVersion
	if instance != nil {
		setSessionCap(instance, sessionCap, cond)
		instance.ResourceVersion = written.ResourceVersion
	}
}

// clearA2ASessionCap removes, on the platform route, the cap and the
// SessionCapFitted condition an earlier opt-in to the session route left, in
// one status patch, carried to instance as resolveA2ASessionCap carries its
// writes. A status holding neither, which is every install that never opted
// in, is not written. Nothing on the platform route reads either; they are
// cleared so the status does not describe a route the install has left.
func (r *PlatformAgentReconciler) clearA2ASessionCap(ctx context.Context, agent, instance *agentv1alpha1.PlatformAgent) {
	if agent.Status.EffectiveSessionCap == nil && meta.FindStatusCondition(agent.Status.Conditions, sessionCapFittedConditionType) == nil {
		return
	}
	patch := client.MergeFromWithOptions(agent.DeepCopy(), client.MergeFromWithOptimisticLock{})
	setSessionCap(agent, nil, nil)
	written := agent.DeepCopy()
	if err := r.Status().Patch(ctx, written, patch); err != nil {
		logf.FromContext(ctx).Error(err, "could not clear the A2A session cap left from the session route; the next pass clears it")
		return
	}
	agent.ResourceVersion = written.ResourceVersion
	if instance != nil {
		setSessionCap(instance, nil, nil)
		instance.ResourceVersion = written.ResourceVersion
	}
}

// noteSessionCapEcho reads status.effectiveSessionCap back after a write, as
// noteBusReplicasEcho does status.busReplicas: a cap written and absent from
// the answer is the served CRD pruning it.
func (r *PlatformAgentReconciler) noteSessionCapEcho(ctx context.Context, agent *agentv1alpha1.PlatformAgent, wrote, echo *int) {
	key := client.ObjectKeyFromObject(agent)
	if wrote == nil || echo != nil {
		r.sessionCapPruned.Delete(key)
		return
	}
	if _, seen := r.sessionCapPruned.Load(key); !seen {
		logf.FromContext(ctx).Info("the served PlatformAgent CRD drops status.effectiveSessionCap; the session cap is kept in memory and not written until the CRD is updated")
	}
	r.sessionCapPruned.Store(key, time.Now())
}

// sessionCapStatusPruned reports a recent echo without the cap
// (noteSessionCapEcho), reprobed every sessionCapPrunedReprobe.
func (r *PlatformAgentReconciler) sessionCapStatusPruned(agent *agentv1alpha1.PlatformAgent) bool {
	at, ok := r.sessionCapPruned.Load(client.ObjectKeyFromObject(agent))
	return ok && time.Since(at.(time.Time)) < sessionCapPrunedReprobe
}

// fitA2ASessionCap is resolveA2ASessionCap's decision: the cap to store, the
// condition to hold (nil for none; keep, from an unreadable pass with a cap,
// is the stored one), and the Warning's text ("" for none). Only on the
// session route: resolveA2ASessionCap does not call it on platform. Under a pruning
// CRD the cap this reconciler last rendered stands in for the stored one.
func (r *PlatformAgentReconciler) fitA2ASessionCap(ctx context.Context, agent *agentv1alpha1.PlatformAgent, pruned bool) (*int, *metav1.Condition, string) {
	if a2aSessionClusterViewEnabled(agent) {
		// The cluster view holds the session default off (resolveA2ADefaultRoute):
		// platform routing at maxSessions, and no fit.
		return nil, sessionCapCondition(agent, metav1.ConditionTrue, sessionCapClusterViewReason, sessionCapClusterViewMessage), ""
	}
	stored := agent.Status.EffectiveSessionCap
	if stored == nil && pruned {
		if last, ok := r.sessionCapLast.Load(client.ObjectKeyFromObject(agent)); ok {
			v := last.(int)
			stored = &v
		}
	}
	configured := resolveA2AMaxSessions(agent)
	width, present, known := r.a2aTasksLiveWidth(ctx, agent, stored != nil)
	switch {
	case !known && stored != nil:
		kept := *stored
		var cond *metav1.Condition
		if existing := meta.FindStatusCondition(agent.Status.Conditions, sessionCapFittedConditionType); existing != nil {
			c := *existing
			c.ObservedGeneration = agent.Generation
			cond = &c
		}
		return &kept, cond, ""
	case !known && !a2aBusExisted(agent):
		// A bus never provisioned: TASKS will be created at maxSessions' width.
		return &configured, nil, ""
	case !known:
		return nil, sessionCapCondition(agent, metav1.ConditionUnknown, sessionCapWaitingReason, sessionCapWaitingMessage), ""
	case !present:
		return &configured, nil, ""
	}
	reserve := a2aTasksReserve(agent)
	fit := a2aSessionsFit(width, reserve, a2aTasksConsumersPerSessionOnSessionRoute)
	effective := max(min(configured, fit), 0)
	if effective >= configured {
		return &effective, nil, ""
	}
	event := fmt.Sprintf(sessionCapEventFormat, width, configured,
		configured*a2aTasksConsumersPerSessionOnSessionRoute+reserve, a2aTasksConsumersPerSessionOnSessionRoute, reserve, effective)
	if effective < 1 {
		return &effective, sessionCapCondition(agent, metav1.ConditionTrue, sessionCapNoRoomReason, sessionCapNoRoomMessage),
			event + sessionCapNoRoomEventSuffix + sessionCapRemedy
	}
	return &effective, sessionCapCondition(agent, metav1.ConditionTrue, sessionCapNarrowReason, sessionCapNarrowMessage),
		event + sessionCapRemedy
}

func sessionCapCondition(agent *agentv1alpha1.PlatformAgent, status metav1.ConditionStatus, reason, message string) *metav1.Condition {
	return &metav1.Condition{
		Type:               sessionCapFittedConditionType,
		Status:             status,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: agent.Generation,
		LastTransitionTime: metav1.Now(),
	}
}

// sessionCapCurrent reports whether agent already holds cap and cond, the
// condition compared on what the writers key on (status, reason, message)
// and on the generation it observed, so a spec edit refreshes it. ignoreCap
// leaves the cap out, for a CRD that prunes it.
func sessionCapCurrent(agent *agentv1alpha1.PlatformAgent, want *int, cond *metav1.Condition, ignoreCap bool) bool {
	have := agent.Status.EffectiveSessionCap
	if !ignoreCap && ((have == nil) != (want == nil) || (have != nil && *have != *want)) {
		return false
	}
	existing := meta.FindStatusCondition(agent.Status.Conditions, sessionCapFittedConditionType)
	if cond == nil || existing == nil {
		return cond == nil && existing == nil
	}
	return existing.Status == cond.Status && existing.Reason == cond.Reason && existing.Message == cond.Message &&
		existing.ObservedGeneration == cond.ObservedGeneration
}

func setSessionCap(agent *agentv1alpha1.PlatformAgent, sessionCap *int, cond *metav1.Condition) {
	if sessionCap == nil {
		agent.Status.EffectiveSessionCap = nil
	} else {
		v := *sessionCap
		agent.Status.EffectiveSessionCap = &v
	}
	if cond == nil {
		meta.RemoveStatusCondition(&agent.Status.Conditions, sessionCapFittedConditionType)
		return
	}
	meta.SetStatusCondition(&agent.Status.Conditions, *cond)
}

// a2aBusExisted reports a bus some operator has provisioned: the
// BusProvisioned record, or a migration's run record (a migration drops the
// BusProvisioned record until the Job after it completes, and runs only on a
// bus that exists). Without either, the bus is a fresh install's, and its
// TASKS will be created at maxSessions' width.
func a2aBusExisted(agent *agentv1alpha1.PlatformAgent) bool {
	return busProvisioned(agent) || agent.Status.BusMigration != nil
}

// a2aSessionCapWaiting reports a pass whose session cap waits on the bus's
// width (sessionCapWaitingReason): the provision Job is not created on it.
// Only on the session route, so a platform install's Job is never held, even
// on the pass before clearA2ASessionCap removes a condition an opt-in left.
func a2aSessionCapWaiting(agent *agentv1alpha1.PlatformAgent) bool {
	if a2aSpecDefaultRoute(agent) != agentv1alpha1.DefaultRouteSession {
		return false
	}
	c := meta.FindStatusCondition(agent.Status.Conditions, sessionCapFittedConditionType)
	return c != nil && c.Reason == sessionCapWaitingReason
}

// UseA2ABusWithoutTasksForTesting answers every reconciler's bus stream read
// with a bus that has no TASKS stream yet, a fresh install's, so the session
// cap is maxSessions. For tests outside this package (the golden renders),
// which cannot set a reconciler's stream seam and run no bus; the operator
// never calls it.
func UseA2ABusWithoutTasksForTesting() {
	a2aBusStreamStateDefault = func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
		return map[string]a2aBusStream{}, nil
	}
}
