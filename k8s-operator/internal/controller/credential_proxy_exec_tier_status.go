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
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// The condition reporting the exec tier's Ready replicas against
	// spec.deployment.credentialProxy.exec.replicas. Written only while the
	// tier is on, and removed at 0, on the EventWatcher pattern. It leaves
	// Ready and the phase alone: a tier short of its count still serves,
	// because a caller that cannot reach it falls back to the stateful broker
	// (docs/designs/credential-broker-exec-tier.md §3.2), so the agent works
	// while the tier is short.
	credentialProxyExecReadyConditionType = "CredentialProxyExecReady"
	credentialProxyExecReadyReason        = "AllReplicasReady"
	credentialProxyExecUnavailableReason  = "ReplicasUnavailable"
	// A tier pod the scheduler could not place: the most common cause on a
	// small node pool, where each replica needs 2 free CPU (#4536).
	credentialProxyExecUnschedulableReason = "Unschedulable"
	// credentialProxyExecReadyMessageFormat takes the Ready and the desired count.
	credentialProxyExecReadyMessageFormat = "%d of %d exec-tier replicas are Ready."
	// credentialProxyExecUnschedulableMessageFormat takes the Ready count, the
	// desired count, the pod's name and the scheduler's message.
	credentialProxyExecUnschedulableMessageFormat = "%d of %d exec-tier replicas are Ready. Pod %s cannot be " +
		"scheduled: %s Each replica requests 2 CPU, so the tier needs nodes with 2 free CPU each, plus room for " +
		"the shell sandbox, which restarts when the tier turns on. Callers fall back to the stateful broker meanwhile."
	// How soon the next pass reads the tier again while it is short. The
	// scheduler's verdict lands on the pod, which is not watched, so without
	// this the condition would wait for the steady-state requeue.
	credentialProxyExecShortRequeue = 30 * time.Second
)

// credentialProxyExecReadyRead is what one pass found for
// CredentialProxyExecReady: the condition to write, nil to remove it, and
// known false when a read failed, so the condition stays as it was. A stale
// count is better than one a transient error made up.
type credentialProxyExecReadyRead struct {
	condition *metav1.Condition
	known     bool
}

// readCredentialProxyExecReady reads the tier's Deployment and pods. It
// writes nothing, so updateStatusReady can tell whether the status changed.
func (r *PlatformAgentReconciler) readCredentialProxyExecReady(ctx context.Context, agent *agentv1alpha1.PlatformAgent) credentialProxyExecReadyRead {
	desired := credentialProxyExecTierReplicas(agent)
	if desired <= 0 || len(ValidateCredentialProxyExec(agent, nil)) > 0 {
		// Off, or refused before render (Degraded says why): no tier to report.
		return credentialProxyExecReadyRead{known: true}
	}
	log := logf.FromContext(ctx)
	deployment := &appsv1.Deployment{}
	ready := int32(0)
	err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: credentialProxyExecTierName(agent)}, deployment)
	switch {
	case err == nil:
		ready = deployment.Status.ReadyReplicas
	case apierrors.IsNotFound(err):
		// Not rendered yet on this pass: none Ready.
	default:
		log.Info("exec tier status not read; CredentialProxyExecReady unchanged", "error", err.Error())
		return credentialProxyExecReadyRead{}
	}
	condition := &metav1.Condition{
		Type:               credentialProxyExecReadyConditionType,
		ObservedGeneration: agent.Generation,
	}
	switch {
	case ready >= desired:
		condition.Status = metav1.ConditionTrue
		condition.Reason = credentialProxyExecReadyReason
		condition.Message = fmt.Sprintf(credentialProxyExecReadyMessageFormat, ready, desired)
	default:
		condition.Status = metav1.ConditionFalse
		condition.Reason = credentialProxyExecUnavailableReason
		condition.Message = fmt.Sprintf(credentialProxyExecReadyMessageFormat, ready, desired)
		name, why, readErr := r.credentialProxyExecUnschedulablePod(ctx, agent)
		if readErr != nil {
			log.Info("exec tier pods not read; CredentialProxyExecReady gives no scheduling reason", "error", readErr.Error())
		} else if name != "" {
			condition.Reason = credentialProxyExecUnschedulableReason
			condition.Message = fmt.Sprintf(credentialProxyExecUnschedulableMessageFormat, ready, desired, name, why)
		}
	}
	return credentialProxyExecReadyRead{condition: condition, known: true}
}

// credentialProxyExecReadyCurrent reports whether the stored condition
// already says what this pass read, so the status write can be skipped.
func credentialProxyExecReadyCurrent(agent *agentv1alpha1.PlatformAgent, read credentialProxyExecReadyRead) bool {
	if !read.known {
		return true
	}
	existing := meta.FindStatusCondition(agent.Status.Conditions, credentialProxyExecReadyConditionType)
	if read.condition == nil {
		return existing == nil
	}
	return existing != nil && existing.Status == read.condition.Status && existing.Reason == read.condition.Reason &&
		existing.Message == read.condition.Message && existing.ObservedGeneration == read.condition.ObservedGeneration
}

// setCredentialProxyExecReadyCondition writes what the pass read. The caller
// does the API write.
func setCredentialProxyExecReadyCondition(agent *agentv1alpha1.PlatformAgent, read credentialProxyExecReadyRead, now metav1.Time) {
	switch {
	case !read.known:
	case read.condition == nil:
		meta.RemoveStatusCondition(&agent.Status.Conditions, credentialProxyExecReadyConditionType)
	default:
		condition := *read.condition
		condition.LastTransitionTime = now
		meta.SetStatusCondition(&agent.Status.Conditions, condition)
	}
}

// credentialProxyExecUnschedulablePod names the first tier pod whose
// PodScheduled condition is False with the scheduler's Unschedulable reason,
// and the scheduler's message, or "" when every pod is placed.
func (r *PlatformAgentReconciler) credentialProxyExecUnschedulablePod(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, string, error) {
	pods := &corev1.PodList{}
	if err := r.podReader().List(ctx, pods, client.InNamespace(agent.Namespace), client.MatchingLabels(credentialProxyExecTierSelector(agent))); err != nil {
		return "", "", err
	}
	for _, pod := range pods.Items {
		if pod.DeletionTimestamp != nil {
			continue
		}
		for _, c := range pod.Status.Conditions {
			if c.Type == corev1.PodScheduled && c.Status == corev1.ConditionFalse && c.Reason == corev1.PodReasonUnschedulable {
				return pod.Name, c.Message, nil
			}
		}
	}
	return "", "", nil
}

// credentialProxyExecShort reports whether the last status write found the
// tier short of its count, which asks for the sooner requeue.
func credentialProxyExecShort(agent *agentv1alpha1.PlatformAgent) bool {
	return meta.IsStatusConditionFalse(agent.Status.Conditions, credentialProxyExecReadyConditionType)
}
