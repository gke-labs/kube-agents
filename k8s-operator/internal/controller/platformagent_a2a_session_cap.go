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
// cap in status it keeps that cap. With none (an upgrade's first pass on an
// unreadable bus, a fresh install before its bus answers) it renders what the
// operator before the default's flip rendered, platform routing where the
// field is unset at the configured cap, holds the provision Job, and says so
// in the SessionCapFitted condition; the reconcile's requeue for the held Job
// brings the pass that reads it. "No TASKS stream" counts as a reading only
// from a successful read of a bus never provisioned: the provision Job will
// create TASKS at the width maxSessions needs (a2aTasksMaxConsumers). On a
// provisioned bus a missing TASKS is a server that has not caught up, or a
// stream deleted for recreation, and the pass treats it as unreadable.
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

	// sessionCapRemedy is how TASKS gets wider, which lifts the fit. A one
	// to three bus migration does not: its restore sends each snapshot's
	// own config (k8s-operator/internal/busmigrate), max_consumers with it.
	sessionCapRemedy = "TASKS gets wider through the opt-in TASKS reshape (gke-labs/kube-agents#6466), " +
		"or by deleting the stream so the provision Job recreates it at the width maxSessions needs, " +
		"which discards the task history it holds"
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

// a2aTasksLiveWidth is TASKS' max_consumers as the bus reports it. known is
// false when the width can't be trusted from this pass: the read failed, a
// migration is reshaping the bus, the report carried no config for TASKS,
// or TASKS is missing from a bus that has been provisioned. present is
// false on a known reading of a bus with no TASKS yet.
func (r *PlatformAgentReconciler) a2aTasksLiveWidth(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (width int, present, known bool) {
	if busMigrationHoldsProvision(agent) {
		return 0, false, false
	}
	streams, err := r.a2aBusStreamState(ctx, agent)
	if err != nil {
		logf.FromContext(ctx).Info("could not read the A2A bus's TASKS width for the session cap", "error", err.Error())
		return 0, false, false
	}
	tasks, ok := streams[a2aTasksStream]
	if !ok {
		return 0, false, !busProvisioned(agent)
	}
	if tasks.MaxConsumers == nil {
		return 0, false, false
	}
	return *tasks.MaxConsumers, true, true
}

// resolveA2ASessionCap writes status.effectiveSessionCap and the
// SessionCapFitted condition on agent, the render copy, before anything is
// rendered from it (see the block comment above). A change is patched to the
// stored status at once, as recordProfileSubmitMaxAge patches its field:
// nothing later in the pass writes a status field the Ready writer does not
// track. The patch's version and both fields are carried to instance too, so
// the pass's later status writes neither drop them nor conflict on them.
func (r *PlatformAgentReconciler) resolveA2ASessionCap(ctx context.Context, agent, instance *agentv1alpha1.PlatformAgent) {
	sessionCap, cond, event := r.fitA2ASessionCap(ctx, agent)
	key := client.ObjectKeyFromObject(agent)
	if event == "" {
		r.sessionCapWarned.Delete(key)
	} else if warned, ok := r.sessionCapWarned.Load(key); !ok || warned.(string) != event {
		r.recordEvent(agent, corev1.EventTypeWarning, sessionCapFittedConditionType, event)
		r.sessionCapWarned.Store(key, event)
	}
	if sessionCapCurrent(agent, sessionCap, cond) {
		return
	}
	patch := client.MergeFromWithOptions(agent.DeepCopy(), client.MergeFromWithOptimisticLock{})
	setSessionCap(agent, sessionCap, cond)
	written := agent.DeepCopy()
	if err := r.Status().Patch(ctx, written, patch); err != nil {
		// The render still follows this pass's reading; the next pass
		// writes it.
		logf.FromContext(ctx).Error(err, "could not record the A2A session cap; the next pass records it")
		return
	}
	agent.ResourceVersion = written.ResourceVersion
	if instance != nil {
		setSessionCap(instance, sessionCap, cond)
		instance.ResourceVersion = written.ResourceVersion
	}
}

// fitA2ASessionCap is resolveA2ASessionCap's decision: the cap to store, the
// condition to hold (nil for none; keep, from an unreadable pass with a cap,
// is the stored one), and the Warning's text ("" for none).
func (r *PlatformAgentReconciler) fitA2ASessionCap(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (*int, *metav1.Condition, string) {
	if a2aSpecDefaultRoute(agent) != agentv1alpha1.DefaultRouteSession {
		// The opt-out: no fit applies on platform, and the cap is maxSessions.
		return nil, nil, ""
	}
	stored := agent.Status.EffectiveSessionCap
	configured := resolveA2AMaxSessions(agent)
	width, present, known := r.a2aTasksLiveWidth(ctx, agent)
	switch {
	case !known && stored == nil:
		return nil, sessionCapCondition(agent, metav1.ConditionUnknown, sessionCapWaitingReason, sessionCapWaitingMessage), ""
	case !known:
		kept := *stored
		return &kept, meta.FindStatusCondition(agent.Status.Conditions, sessionCapFittedConditionType), ""
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
// condition compared on what the writers key on: status, reason, message.
func sessionCapCurrent(agent *agentv1alpha1.PlatformAgent, want *int, cond *metav1.Condition) bool {
	have := agent.Status.EffectiveSessionCap
	if (have == nil) != (want == nil) || (have != nil && *have != *want) {
		return false
	}
	existing := meta.FindStatusCondition(agent.Status.Conditions, sessionCapFittedConditionType)
	if cond == nil || existing == nil {
		return cond == nil && existing == nil
	}
	return existing.Status == cond.Status && existing.Reason == cond.Reason && existing.Message == cond.Message
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

// a2aSessionCapWaiting reports a pass whose session cap waits on the bus's
// width (sessionCapWaitingReason): the provision Job is not created on it.
func a2aSessionCapWaiting(agent *agentv1alpha1.PlatformAgent) bool {
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
