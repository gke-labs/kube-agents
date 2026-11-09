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

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// a2aNATSStuckPodGrace is how long the bus pod may stay not Ready on a
	// revision the StatefulSet no longer renders before the operator replaces
	// it. The StatefulSet controller rolls a Ready pod itself; one that never
	// became Ready blocks an OrderedReady rolling update for good (the
	// documented forced-rollback limit), which a rollback across the bus's
	// account move leaves behind. Long enough for a pod that is merely
	// starting, which is on the current revision anyway.
	a2aNATSStuckPodGrace = 2 * time.Minute
	// a2aNATSStuckPodReplacedReason is the Warning event's reason when the
	// operator replaces such a pod.
	a2aNATSStuckPodReplacedReason = "A2ABusPodReplaced"
	// a2aNATSPodOrdinalSuffix names the bus's one pod: the StatefulSet has a
	// single replica, ordinal 0.
	a2aNATSPodOrdinalSuffix = "-0"
)

// replaceStuckA2ANATSPod deletes the bus pod when it sits not Ready on a
// revision the StatefulSet no longer renders, so the StatefulSet recreates it
// on the current one. It returns how long until a pod still inside
// a2aNATSStuckPodGrace is due, zero otherwise. The caller runs it only on a
// pass that applied the StatefulSet, never while the bus is held for the
// auth callout (a2aBusWaitsForCallout), whose earlier template a replaced
// pod would come back on.
//
// sts is the StatefulSet as the apply returned it, so its generation and
// status are the server's at that moment. It acts only when the StatefulSet
// controller has observed that generation, so status.updateRevision names the
// template just applied (the status update that follows a template change
// starts another pass, as the StatefulSet is owned); some replica is not on
// that revision; the pod's controller-revision-hash differs from it; the pod
// is not terminating; and the pod has not been Ready for
// a2aNATSStuckPodGrace, timed from its Ready condition's last transition,
// else from its creation. The delete names the pod's UID, so it cannot remove
// a pod the StatefulSet created since the read. A healthy bus, every replica
// updated, costs no read; otherwise the pod is read live (podReader), as pods
// are outside the manager's cache. Only a pod the StatefulSet controls is
// ever deleted.
func (r *PlatformAgentReconciler) replaceStuckA2ANATSPod(ctx context.Context, agent *agentv1alpha1.PlatformAgent, sts *appsv1.StatefulSet, now time.Time) (time.Duration, error) {
	updateRevision := sts.Status.UpdateRevision
	if sts.Status.ObservedGeneration < sts.Generation || updateRevision == "" {
		return 0, nil
	}
	if sts.Spec.Replicas != nil && sts.Status.UpdatedReplicas >= *sts.Spec.Replicas {
		return 0, nil
	}
	pod := &corev1.Pod{}
	if err := r.podReader().Get(ctx, types.NamespacedName{Name: sts.Name + a2aNATSPodOrdinalSuffix, Namespace: sts.Namespace}, pod); err != nil {
		return 0, client.IgnoreNotFound(err)
	}
	// A pod of that name the StatefulSet did not create is not the bus's to
	// replace, whatever its labels say.
	if owner := metav1.GetControllerOf(pod); owner == nil || owner.UID != sts.UID {
		return 0, nil
	}
	if pod.DeletionTimestamp != nil || pod.Labels[appsv1.ControllerRevisionHashLabelKey] == updateRevision {
		return 0, nil
	}
	since := pod.CreationTimestamp.Time
	for _, cond := range pod.Status.Conditions {
		if cond.Type != corev1.PodReady {
			continue
		}
		if cond.Status == corev1.ConditionTrue {
			return 0, nil
		}
		if !cond.LastTransitionTime.IsZero() {
			since = cond.LastTransitionTime.Time
		}
	}
	if left := a2aNATSStuckPodGrace - now.Sub(since); left > 0 {
		return left, nil
	}
	revision := pod.Labels[appsv1.ControllerRevisionHashLabelKey]
	uid := pod.UID
	if err := r.Delete(ctx, pod, client.Preconditions{UID: &uid}); err != nil {
		if errors.IsNotFound(err) || errors.IsConflict(err) {
			return 0, nil
		}
		return 0, fmt.Errorf("failed to replace A2A bus pod %s: %w", pod.Name, err)
	}
	logf.FromContext(ctx).Info("replaced the A2A bus pod: it was not Ready on a revision the StatefulSet no longer renders",
		"pod", pod.Name, "revision", revision, "updateRevision", updateRevision)
	r.recordEvent(agent, corev1.EventTypeWarning, a2aNATSStuckPodReplacedReason,
		fmt.Sprintf("Replaced A2A bus pod %s: not Ready for %s on revision %s, while the StatefulSet renders %s.",
			pod.Name, now.Sub(since).Truncate(time.Second), revision, updateRevision))
	return 0, nil
}
