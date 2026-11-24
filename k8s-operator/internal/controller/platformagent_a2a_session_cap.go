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
// All of this runs only where spec.harness.tuning.defaultRoute says
// `session`. On `platform`, which an unset field resolves to, nothing here
// runs: no read of the bus, no provision Job wait, and the session cap is
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
// pass. Only a provisioned bus with no stored cap whose width can't be read
// (the monitoring endpoint doesn't answer, or a migration is under way),
// an existing install's first pass after it opts in, waits: it renders the
// session route at the configured cap, holds the provision Job, and says so
// in the SessionCapFitted condition; the reconcile's requeue for the held
// Job brings the pass that reads it. A bus that answers without TASKS, a
// stream deleted for recreation or one a restarted server hasn't recovered
// yet, keeps a stored cap. With nothing stored it is a reading of no
// stream, as on a bus never provisioned: the Job runs and creates TASKS at
// maxSessions' width, and its own check refuses a stream that turns out to
// be there and narrower.
//
// A stored cap's reading is reused for sessionCapReadingTTL, so a settled
// install does not make a blocking GET of the bus on every pass. A served CRD
// that prunes status.effectiveSessionCap (one older than the operator) can't
// store the cap the session route renders from, so the install falls back to
// platform with SessionCapFitted saying why (sessionCapPrunedReason). The
// condition is the record: it survives a restart, and the route reads it.
// Every sessionCapPrunedReprobe a pass writes the fitted cap again, keeping
// platform for that pass's render; only an echo that keeps the cap engages
// the session route. A pass that can't read the bus doesn't probe, so it
// can't widen anything.
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
	// the provision Job waits and the session route renders at maxSessions.
	sessionCapWaitingReason = "WaitingOnBusWidth"
	// sessionCapPrunedReason: the served PlatformAgent CRD drops
	// status.effectiveSessionCap, so the session route can't keep its cap,
	// and plain conversations stay with platform until the CRD is updated.
	sessionCapPrunedReason = "StatusFieldPruned"

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
	// rendered one, history kept (gke-labs/kube-agents#6462); on any bus a
	// reshape (a2aBusReshapeAnnotation) recreates it at the rendered width.
	sessionCapRemedy = "the cap stays fitted until TASKS is widened: on a one-server bus, setting " +
		"spec.harness.tuning.busReplicas to 3 widens it and keeps its history; on any bus, a new value on the " +
		"PlatformAgent's " + a2aBusReshapeAnnotation + " annotation recreates it at the rendered width, keeping its messages"
	sessionCapNarrowMessage = "the TASKS stream has room for fewer sessions than spec.harness.tuning.maxSessions on the session route, " +
		"and a stream's max_consumers cannot change once it exists, so the session cap is fitted to it (status.effectiveSessionCap); " +
		sessionCapRemedy
	sessionCapNoRoomMessage = "the TASKS stream has no room for one session's consumers on the session route, " +
		"so plain conversations go to platform (status.effectiveSessionCap is 0); " + sessionCapRemedy
	sessionCapPrunedMessage = "the served PlatformAgent CRD drops status.effectiveSessionCap, so the session route can't keep its fitted cap " +
		"and plain conversations stay with platform; apply the chart's crds/ (upgrade.sh does) and the next probe engages the session route"
	sessionCapWaitingMessage = "the session cap waits on the A2A bus's TASKS width, which could not be read yet; " +
		"until it is, plain conversations go to session pods at spec.harness.tuning.maxSessions and the provision Job waits"

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
// migration is reshaping the bus, or the report carried no config for TASKS.
// present is false on a known reading of a bus that answers without TASKS,
// provisioned or not; what that means on a provisioned bus is
// fitA2ASessionCap's to decide. With reuse set (a cap is stored), a known
// reading younger than sessionCapReadingTTL answers without reading the bus.
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
	// A bus that answers without TASKS, provisioned or not and with no
	// migration under way (busMigrationHoldsProvision, above), is a reading
	// of no stream: the provision Job creates TASKS at maxSessions' width
	// (a2aTasksMaxConsumers), so nothing waits on it. Waiting would hold the
	// one Job that makes the stream.
	case ok && tasks.MaxConsumers == 0:
		// Unreported: the server sent no config for TASKS. A live stream
		// reports -1 for no limit, never 0.
		return 0, false, false
	case ok:
		width, present = tasks.MaxConsumers, true
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
// Under a served CRD that prunes the cap the install is held on platform
// (holdSessionCapPruned), probed every sessionCapPrunedReprobe.
func (r *PlatformAgentReconciler) resolveA2ASessionCap(ctx context.Context, agent, instance *agentv1alpha1.PlatformAgent) {
	key := client.ObjectKeyFromObject(agent)
	if a2aSpecDefaultRoute(agent) != agentv1alpha1.DefaultRouteSession {
		// The platform route, the default: no fit applies, and nothing is
		// read off the bus. This reconciler's memory of the route is
		// dropped, so an install that opts back in reads the bus afresh.
		r.sessionCapWarned.Delete(key)
		r.sessionCapPruned.Delete(key)
		r.sessionCapReadings.Delete(key)
		r.clearA2ASessionCap(ctx, agent, instance)
		return
	}
	if r.sessionCapStatusPruned(agent) {
		r.holdSessionCapPruned(ctx, agent, instance)
		return
	}
	// A pruned write past its reprobe, or a condition saying so from before
	// a restart: this pass probes the CRD, and renders platform either way.
	_, recorded := r.sessionCapPruned.Load(key)
	probing := recorded || a2aSessionCapPrunedHeld(agent)
	sessionCap, cond, event := r.fitA2ASessionCap(ctx, agent)
	if event == "" {
		r.sessionCapWarned.Delete(key)
	} else if warned, ok := r.sessionCapWarned.Load(key); !ok || warned.(string) != event {
		r.recordEvent(agent, corev1.EventTypeWarning, sessionCapFittedConditionType, event)
		r.sessionCapWarned.Store(key, event)
	}
	if probing {
		if sessionCap == nil || !r.probeSessionCapStored(ctx, agent, instance, *sessionCap) {
			// Nothing to probe with (the bus unreadable), or the CRD
			// still drops the field: platform, and the next probe after
			// the reprobe.
			r.sessionCapPruned.Store(key, time.Now())
			r.holdSessionCapPruned(ctx, agent, instance)
			return
		}
		r.sessionCapPruned.Delete(key)
	}
	if sessionCapCurrent(agent, sessionCap, cond) {
		return
	}
	original := agent.DeepCopy()
	patch := client.MergeFromWithOptions(original, client.MergeFromWithOptimisticLock{})
	written := original.DeepCopy()
	setSessionCap(written, sessionCap, cond)
	if err := r.Status().Patch(ctx, written, patch); err != nil {
		// The render follows this pass's reading; the next pass writes it.
		setSessionCap(agent, sessionCap, cond)
		logf.FromContext(ctx).Error(err, "could not record the A2A session cap; the next pass records it")
		return
	}
	if sessionCap != nil && written.Status.EffectiveSessionCap == nil {
		// The echo dropped the cap: the served CRD prunes it. Held on
		// platform from this pass, so the session route never renders a
		// cap the store can't keep.
		logf.FromContext(ctx).Info("the served PlatformAgent CRD drops status.effectiveSessionCap; plain conversations stay with platform until the CRD is updated")
		r.sessionCapPruned.Store(key, time.Now())
		// Only the version is carried: the render copy holds status the
		// store doesn't (resolvedUniverseDomain), and holdSessionCapPruned
		// sets the cap and the condition.
		agent.ResourceVersion = written.ResourceVersion
		if instance != nil {
			instance.ResourceVersion = written.ResourceVersion
		}
		r.holdSessionCapPruned(ctx, agent, instance)
		return
	}
	setSessionCap(agent, sessionCap, cond)
	agent.ResourceVersion = written.ResourceVersion
	if instance != nil {
		setSessionCap(instance, sessionCap, cond)
		instance.ResourceVersion = written.ResourceVersion
	}
}

// holdSessionCapPruned renders platform under a CRD that prunes
// status.effectiveSessionCap: no cap, and the SessionCapFitted condition
// saying why (sessionCapPrunedReason), which resolveA2ADefaultRoute reads.
// The condition is written only when it changes.
func (r *PlatformAgentReconciler) holdSessionCapPruned(ctx context.Context, agent, instance *agentv1alpha1.PlatformAgent) {
	cond := sessionCapCondition(agent, metav1.ConditionTrue, sessionCapPrunedReason, sessionCapPrunedMessage)
	if !sessionCapCurrent(agent, nil, cond) {
		patch := client.MergeFromWithOptions(agent.DeepCopy(), client.MergeFromWithOptimisticLock{})
		written := agent.DeepCopy()
		setSessionCap(written, nil, cond)
		if err := r.Status().Patch(ctx, written, patch); err != nil {
			logf.FromContext(ctx).Error(err, "could not record that the session route is held on platform; the next pass records it")
		} else {
			agent.ResourceVersion = written.ResourceVersion
			if instance != nil {
				instance.ResourceVersion = written.ResourceVersion
			}
		}
	}
	setSessionCap(agent, nil, cond)
	if instance != nil {
		setSessionCap(instance, nil, cond)
	}
}

// probeSessionCapStored writes sessionCap with the held condition kept, and
// reports whether the echo kept the cap: the CRD has been updated. The
// render copy is not given the cap here; the caller's write that follows
// sets it and the fitted condition.
func (r *PlatformAgentReconciler) probeSessionCapStored(ctx context.Context, agent, instance *agentv1alpha1.PlatformAgent, sessionCap int) bool {
	patch := client.MergeFromWithOptions(agent.DeepCopy(), client.MergeFromWithOptimisticLock{})
	written := agent.DeepCopy()
	written.Status.EffectiveSessionCap = &sessionCap
	if err := r.Status().Patch(ctx, written, patch); err != nil {
		logf.FromContext(ctx).Error(err, "could not probe whether the PlatformAgent CRD keeps status.effectiveSessionCap; the next probe retries")
		return false
	}
	agent.ResourceVersion = written.ResourceVersion
	agent.Status.EffectiveSessionCap = written.Status.EffectiveSessionCap
	if instance != nil {
		instance.ResourceVersion = written.ResourceVersion
		instance.Status.EffectiveSessionCap = written.Status.EffectiveSessionCap
	}
	return written.Status.EffectiveSessionCap != nil
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

// sessionCapStatusPruned reports a recent write whose echo dropped the cap
// (resolveA2ASessionCap), reprobed every sessionCapPrunedReprobe.
func (r *PlatformAgentReconciler) sessionCapStatusPruned(agent *agentv1alpha1.PlatformAgent) bool {
	at, ok := r.sessionCapPruned.Load(client.ObjectKeyFromObject(agent))
	return ok && time.Since(at.(time.Time)) < sessionCapPrunedReprobe
}

// fitA2ASessionCap is resolveA2ASessionCap's decision: the cap to store, the
// condition to hold (nil for none; keep, from an unreadable pass with a cap,
// is the stored one), and the Warning's text ("" for none). Only on the
// session route: resolveA2ASessionCap does not call it on platform.
func (r *PlatformAgentReconciler) fitA2ASessionCap(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (*int, *metav1.Condition, string) {
	stored := agent.Status.EffectiveSessionCap
	configured := resolveA2AMaxSessions(agent)
	width, present, known := r.a2aTasksLiveWidth(ctx, agent, stored != nil)
	// A provisioned bus that answers without TASKS keeps a stored cap: the
	// server may not have recovered a stream that is there and narrower.
	// With nothing stored it waits on nothing: the provision Job creates
	// TASKS at maxSessions' width, and holding it would hold the one Job
	// that makes the stream.
	absentOnExisting := known && !present && a2aBusExisted(agent)
	switch {
	case (!known || absentOnExisting) && stored != nil:
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
// and on the generation it observed, so a spec edit refreshes it.
func sessionCapCurrent(agent *agentv1alpha1.PlatformAgent, want *int, cond *metav1.Condition) bool {
	have := agent.Status.EffectiveSessionCap
	if (have == nil) != (want == nil) || (have != nil && *have != *want) {
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

// a2aSessionCapPrunedHeld reports an install held on platform because the
// served CRD prunes status.effectiveSessionCap (sessionCapPrunedReason).
// Only on the session route, as a2aSessionCapWaiting.
func a2aSessionCapPrunedHeld(agent *agentv1alpha1.PlatformAgent) bool {
	if a2aSpecDefaultRoute(agent) != agentv1alpha1.DefaultRouteSession {
		return false
	}
	c := meta.FindStatusCondition(agent.Status.Conditions, sessionCapFittedConditionType)
	return c != nil && c.Reason == sessionCapPrunedReason
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
