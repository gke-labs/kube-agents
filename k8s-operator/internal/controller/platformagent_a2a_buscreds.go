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

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// BusCredentialsReady: whether the bus can authenticate the identities the
// operator has rendered.
//
// The race it exists to remove is the deployment spec's: an identity's entry
// lands, a workload that needs it is spawned seconds later, and the callout has
// not caught up — so a legitimate client holding a perfectly good token is
// refused, and the refusal is indistinguishable from a bad credential. The
// answer is not to hope the propagation wins; it is to have a condition that
// says whether it has, and for the thing that dispatches work to wait on it.
//
// **What this asserts, exactly.** The callout Deployment is Available with all
// replicas ready. Since the callout's readiness probe answers 503 until it is
// serving a map, that means every replica is serving one. Combined with the
// map being rendered before the callout in the same reconcile, this is "the
// bus can authenticate what the operator has rendered".
//
// **What it does not assert**, so nobody reads more into it than it carries: it
// does not confirm that a named replica has observed a named map version. The
// gap is the sub-second window after a re-render in which a ready replica may
// still be serving the previous map. That is acceptable while the identity set
// changes only when the operator re-renders it — which is A1's situation, where
// the identities are fixed at install. It stops being acceptable when profiles
// arrive at runtime and identities become dynamic: at that point this has to
// become a per-replica check of the served version against the rendered one,
// and the callout's status endpoint already exposes exactly what such a check
// would read.
//
// Rolling the callout pods on every map change would close the gap and was
// rejected: the callout is on the connection path, so a rolling restart is a
// window in which new connections fail, which is the thing the informer exists
// to avoid. A stale condition for a moment is cheaper than a refused connection.
const busCredentialsReadyCondition = "BusCredentialsReady"

const (
	busCredsReasonServing     = "CalloutServing"
	busCredsReasonUnavailable = "CalloutUnavailable"
	busCredsReasonAbsent      = "CalloutAbsent"
)

// syncBusCredentialsReady brings the condition into line with what is on the
// cluster, and is called on the way out of Reconcile rather than at a point in
// its sequence.
//
// The sequence has no point that works. Four refusals of today's stack return
// above the bus step -- ForbiddenVolumeMount, ShellSandboxCannotBeDisabled,
// RuntimeClassNotFound, EgressAllowlistRefused -- and the bus step cannot move
// above them, because it renders on top of what they withhold. Written after
// them, the condition is skipped on those paths; and skipped is not absent,
// because updateStatusDegraded writes Ready alone and preserves every other
// condition. The last value stands unchallenged for as long as the refusal
// does, so a CR goes on reporting a callout serving a named map version
// through a Deployment that has since lost every replica. Absent is a gap
// somebody notices. Stale reads as healthy.
//
// Which is why the answer is not a better position in the sequence. Whatever
// the reconcile decided, the callout Deployment is on the cluster or it is
// not, and this reads it and says so. Deferred, it also lands after
// updateStatusReady and updateStatusDegraded, so it is not racing either of
// them for the object's resourceVersion.
//
// wantNext decides only the absent case: a next install with no callout says
// so, a today install carries no condition at all. It deliberately does not
// decide the present case. A CR flipped to today whose refusal also withheld
// cleanupA2A still has a callout running, and removing the condition there
// would report a component gone while it is still authenticating the bus.
//
// mapVersion is what this reconcile rendered, or empty on a pass that returned
// before rendering -- in which case the version is read back off the ConfigMap
// the callout watches, since that is the map actually being served.
func (r *PlatformAgentReconciler) syncBusCredentialsReady(ctx context.Context, agent *agentv1alpha1.PlatformAgent, wantNext bool, mapVersion string) error {
	dep := &appsv1.Deployment{}
	err := r.Get(ctx, types.NamespacedName{Name: a2aCalloutName(agent), Namespace: agent.Namespace}, dep)
	if client.IgnoreNotFound(err) != nil {
		return err
	}
	if err != nil && !wantNext {
		if r.clearBusCredentialsReady(agent) {
			return r.Status().Update(ctx, agent)
		}
		return nil
	}
	if mapVersion == "" {
		mapVersion = r.renderedAuthMapVersion(ctx, agent)
	}
	return r.setBusCredentialsReady(ctx, agent, dep, err != nil, mapVersion)
}

// renderedAuthMapVersion reads the version off the ConfigMap the callout
// watches. Best effort: the caller only needs it for the condition's message,
// and a message naming no version is better than a reconcile that fails
// because it could not read one.
func (r *PlatformAgentReconciler) renderedAuthMapVersion(ctx context.Context, agent *agentv1alpha1.PlatformAgent) string {
	cm := &corev1.ConfigMap{}
	if err := r.Get(ctx, types.NamespacedName{Name: a2aAuthMapName(agent), Namespace: agent.Namespace}, cm); err != nil {
		return a2aAuthMapVersionUnknown
	}
	if v := cm.Annotations[a2aAuthMapVersionAnnotation]; v != "" {
		return v
	}
	return a2aAuthMapVersionUnknown
}

// setBusCredentialsReady writes the condition from the callout Deployment's
// state. mapVersion is the map being served, carried into the message so an
// operator can compare it against what the callout reports.
func (r *PlatformAgentReconciler) setBusCredentialsReady(ctx context.Context, agent *agentv1alpha1.PlatformAgent, dep *appsv1.Deployment, absent bool, mapVersion string) error {
	// The count the operator asked for, not the pods that happen to exist.
	// Status.Replicas counts non-terminated pods matching the selector, which
	// is a different number from the desired one on both sides: the rendered
	// Deployment rolls at MaxSurge 1 over MaxUnavailable 0, so a routine
	// image or config-hash roll spends its whole duration at three pods with
	// two ready, and a callout coming up or missing a reaped pod spends its
	// window at one pod that is ready. Comparing ready against it therefore
	// reports an outage through every healthy rollout -- the surge strategy
	// being precisely what keeps the ready count from dropping -- and reports
	// serving on half a callout. Spec.Replicas is the question being asked;
	// nil is one, the API's default.
	desired := int32(1)
	if dep.Spec.Replicas != nil {
		desired = *dep.Spec.Replicas
	}
	// Status describes whichever spec the Deployment controller has acted on.
	// While ObservedGeneration lags Generation the counts below are true of a
	// Deployment that no longer exists, so a spec change that will take pods
	// down still reads as fully ready for as long as the controller takes to
	// notice it. That is the stale-reads-as-healthy failure the rest of this
	// file is written against, arriving through the Deployment instead of
	// through the reconcile.
	observed := dep.Status.ObservedGeneration >= dep.Generation

	condition := metav1.Condition{Type: busCredentialsReadyCondition, LastTransitionTime: metav1.Now()}
	switch {
	case absent:
		condition.Status = metav1.ConditionFalse
		condition.Reason = busCredsReasonAbsent
		condition.Message = "the auth callout is not deployed; nothing can authenticate to the bus"
	case observed && dep.Status.ReadyReplicas > 0 && dep.Status.ReadyReplicas >= desired:
		condition.Status = metav1.ConditionTrue
		condition.Reason = busCredsReasonServing
		condition.Message = "the auth callout is serving identity map " + mapVersion
	case !observed:
		condition.Status = metav1.ConditionFalse
		condition.Reason = busCredsReasonUnavailable
		condition.Message = fmt.Sprintf(
			"the auth callout is rolling: generation %d is not yet observed, and the last reported "+
				"%d of %d replicas ready describes the spec before it; new connections to the bus "+
				"may be refused",
			dep.Generation, dep.Status.ReadyReplicas, desired)
	default:
		condition.Status = metav1.ConditionFalse
		condition.Reason = busCredsReasonUnavailable
		// With the counts, because "not ready" on a component that gates
		// every new connection is the first thing someone will want to size:
		// one replica of two is a degraded rollout, zero of two is the bus
		// accepting no new client at all.
		condition.Message = fmt.Sprintf(
			"the auth callout has %d of %d replicas ready; new connections to the bus may be refused",
			dep.Status.ReadyReplicas, desired)
	}

	// Only when something changed. This runs on every exit from every
	// reconcile, and an unconditional Status().Update would be a write per
	// pass on a resource the operator already resyncs on a timer.
	if !meta.SetStatusCondition(&agent.Status.Conditions, condition) {
		return nil
	}
	return r.Status().Update(ctx, agent)
}

// clearBusCredentialsReady removes the condition. A today install must not
// carry a condition that describes a component it does not have — the darkness
// property reaches status, not just objects.
func (r *PlatformAgentReconciler) clearBusCredentialsReady(agent *agentv1alpha1.PlatformAgent) bool {
	return meta.RemoveStatusCondition(&agent.Status.Conditions, busCredentialsReadyCondition)
}
