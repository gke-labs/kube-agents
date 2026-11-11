/*
Copyright 2026 Google LLC.

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

// The A2A gateway's wait for TASKS once the bus is provisioned (#4625).
//
// Since #4624 a gateway whose event relay finds no TASKS stream waits for it
// rather than exiting: its pod reads not ready (the readinessProbe on
// /readyz is the relay bind and nothing else), and the CR reads
// Provisioning, naming the gateway Deployment. That is right while the
// provision Job has yet to run. Once BusProvisioned is True the Job has
// created the stream, so a gateway still waiting is not seeing a stream
// that exists, or one that was lost since: its NATS identity lands in
// another account, its credentials are wrong, or a JetStream domain does not
// match. Provisioning then points the reader at the one thing that is done,
// so past a threshold the CR reads Degraded with this reason as well.
//
// A lost stream is also what BusStreamsPresent reports, once confirmed
// (A2ABusStreamsMissing, which ranks above this in the Degraded chain). The
// two differ in what they see: that one reads the streams through the
// exporter, so it reads AllPresent when the stream exists but the gateway
// cannot reach it, and it takes three polls to confirm a loss. This one
// reads the gateway's own readiness.
const (
	conditionReasonA2AGatewayWaitingForTasks = "A2AGatewayWaitingForTasks"

	// a2aGatewayTasksWaitThreshold is how long a gateway may wait for TASKS
	// after the later of its start and BusProvisioned turning True before the
	// CR reads Degraded. A gateway that started with the bus up binds in
	// seconds; the gateway itself warns at ten minutes (#4624), and its
	// Deployment's progress deadline is thirty. Fixed rather than a field:
	// a gateway that has not bound five minutes into a provisioned bus is
	// not going to without someone looking.
	a2aGatewayTasksWaitThreshold = 5 * time.Minute

	// a2aGatewayTasksWaitMsgFmt takes the gateway Deployment's name and the
	// threshold. No elapsed time: the Degraded write is gated on the
	// message, so a figure that moved would write status on every pass.
	a2aGatewayTasksWaitMsgFmt = "A2A gateway Deployment %s has not bound its event relay on the TASKS stream for more than %s " +
		"since the bus was recorded provisioned (BusProvisioned=True), so the stream the provision Job created is not visible to it. " +
		"Likely causes: the gateway's NATS identity lands in a different account than the streams, its credentials are wrong, " +
		"or a JetStream domain or prefix does not match. Read the gateway's /readyz body on its metrics port and its log"
	// a2aGatewayTasksWaitStreamsSuffixFmt is added when BusStreamsPresent
	// is on the CR and not True, naming its reason: that condition then
	// says whether the stream is there at all.
	a2aGatewayTasksWaitStreamsSuffixFmt = "; BusStreamsPresent reads %s (%s), which says whether the stream is there at all"

	// a2aBusStreamsPresentConditionType is #5474's condition, read by its
	// type string so this file stands before #5474 lands. Whichever of the
	// two lands second replaces it with that change's constant.
	a2aBusStreamsPresentConditionType = "BusStreamsPresent"
)

// a2aGatewayTasksWait reports whether the A2A gateway is waiting for TASKS
// on a provisioned bus, and since when: the later of BusProvisioned's
// transition to True and the youngest gateway container's start. Waiting
// means every live gateway pod is Running with its gateway container
// running and not ready. A container that is not running (waiting to start,
// pulling, crash looping, terminated) is the pod fault scan's to name, so
// such a pod makes this false, as does any ready pod, or none at all.
func a2aGatewayTasksWait(agent *agentv1alpha1.PlatformAgent, pods []corev1.Pod) (time.Time, bool) {
	if !a2aStackRendering(agent) {
		return time.Time{}, false
	}
	provisioned := meta.FindStatusCondition(agent.Status.Conditions, busProvisionedConditionType)
	if provisioned == nil || provisioned.Status != metav1.ConditionTrue {
		return time.Time{}, false
	}
	since := provisioned.LastTransitionTime.Time
	live := 0
	for _, pod := range pods {
		if !pod.DeletionTimestamp.IsZero() {
			continue
		}
		live++
		if pod.Status.Phase != corev1.PodRunning {
			return time.Time{}, false
		}
		var gateway *corev1.ContainerStatus
		for i := range pod.Status.ContainerStatuses {
			if pod.Status.ContainerStatuses[i].Name == a2aGatewayContainerName {
				gateway = &pod.Status.ContainerStatuses[i]
			}
		}
		if gateway == nil || gateway.Ready || gateway.State.Running == nil {
			return time.Time{}, false
		}
		if started := gateway.State.Running.StartedAt.Time; started.After(since) {
			since = started
		}
	}
	return since, live > 0
}

// a2aGatewayTasksWaitDue reports whether a wait since since has reached the
// threshold at now, and if not, how long until it does.
func a2aGatewayTasksWaitDue(since, now time.Time) (bool, time.Duration) {
	left := a2aGatewayTasksWaitThreshold - now.Sub(since)
	if left <= 0 {
		return true, 0
	}
	return false, left
}

// a2aGatewayTasksWaitPods lists the A2A gateway's pods from the live reader
// the pod fault scan uses, or reports the read failed.
func (r *PlatformAgentReconciler) a2aGatewayTasksWaitPods(ctx context.Context, agent *agentv1alpha1.PlatformAgent) ([]corev1.Pod, error) {
	podList := &corev1.PodList{}
	if err := r.podReader().List(ctx, podList, client.InNamespace(agent.Namespace), client.MatchingLabels{"app": a2aGatewayName(agent)}); err != nil {
		return nil, err
	}
	return podList.Items, nil
}

// a2aGatewayTasksWaitMessage is the Degraded message for a gateway past the
// threshold, or "" when it is not. Read only when the status pass already
// found the A2A gateway Deployment with no ready replica, so a settled
// install lists no pods for it. A failed pod read keeps the reason the CR
// already carries, as the pod fault scan keeps a fault it wrote: finding
// nothing is not evidence of nothing, and a cleared reason re-asserted on
// the next pass would write status twice for an apiserver blip.
func (r *PlatformAgentReconciler) a2aGatewayTasksWaitMessage(ctx context.Context, agent *agentv1alpha1.PlatformAgent) string {
	if !a2aStackRendering(agent) || !busProvisioned(agent) {
		return ""
	}
	pods, err := r.a2aGatewayTasksWaitPods(ctx, agent)
	if err != nil {
		logf.FromContext(ctx).Error(err, "could not list the A2A gateway's pods for the TASKS wait; the Degraded reason is left as the CR last reported it")
		if held := meta.FindStatusCondition(agent.Status.Conditions, degradedConditionType); held != nil &&
			held.Status == metav1.ConditionTrue && held.Reason == conditionReasonA2AGatewayWaitingForTasks {
			return held.Message
		}
		return ""
	}
	since, waiting := a2aGatewayTasksWait(agent, pods)
	if !waiting {
		return ""
	}
	if due, _ := a2aGatewayTasksWaitDue(since, time.Now()); !due {
		return ""
	}
	msg := fmt.Sprintf(a2aGatewayTasksWaitMsgFmt, a2aGatewayName(agent), a2aGatewayTasksWaitThreshold)
	if streams := meta.FindStatusCondition(agent.Status.Conditions, a2aBusStreamsPresentConditionType); streams != nil && streams.Status != metav1.ConditionTrue {
		msg += fmt.Sprintf(a2aGatewayTasksWaitStreamsSuffixFmt, streams.Status, streams.Reason)
	}
	return msg
}

// a2aGatewayTasksWaitRequeue is when the pass that turns a waiting gateway
// Degraded is due: pods are not watched, so nothing else wakes it at the
// threshold. A day (past any requeue it is min'd with) when no gateway is
// waiting short of it, including once the reason is already written.
func (r *PlatformAgentReconciler) a2aGatewayTasksWaitRequeue(ctx context.Context, agent *agentv1alpha1.PlatformAgent) time.Duration {
	const none = 24 * time.Hour
	if !a2aStackRendering(agent) || !busProvisioned(agent) {
		return none
	}
	if held := meta.FindStatusCondition(agent.Status.Conditions, degradedConditionType); held != nil &&
		held.Status == metav1.ConditionTrue && held.Reason == conditionReasonA2AGatewayWaitingForTasks {
		return none
	}
	pods, err := r.a2aGatewayTasksWaitPods(ctx, agent)
	if err != nil {
		return none
	}
	since, waiting := a2aGatewayTasksWait(agent, pods)
	if !waiting {
		return none
	}
	if due, left := a2aGatewayTasksWaitDue(since, time.Now()); !due {
		return left
	}
	// Due already and not yet written: the next pass writes it.
	return time.Second
}
