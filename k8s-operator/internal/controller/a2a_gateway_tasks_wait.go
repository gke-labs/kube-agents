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

	appsv1 "k8s.io/api/apps/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
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
// /readyz is the relay bind and nothing else). The gateway is leader-elected
// and only the leader binds, so with two replicas the standby reads Ready
// while the leader waits, and the CR reads Ready over a gateway relaying
// nothing (whether Ready should follow the leader is #5593). Once
// BusProvisioned is True the Job has created the stream, so a leader still
// waiting is not seeing a stream that exists, or one that was lost since:
// its NATS identity lands in another account, its credentials are wrong, or
// a JetStream domain does not match. Past a threshold the CR reads Degraded
// with this reason, naming the leader pod.
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

	// a2aGatewayTasksWaitPoll is how soon the status pass looks again while
	// the gateway is short of ready replicas on a provisioned bus but not
	// waiting yet, or its read failed (a2aGatewayTasksWaitMessage). The
	// A2A stack's own pending requeue: the gate that leads here is cached and
	// false on a settled install, so a settled install never polls.
	a2aGatewayTasksWaitPoll = a2aPendingRequeue

	// a2aGatewayTasksWaitMsgFmt takes the gateway Deployment's name, the pod
	// that binds the relay (the elected leader, or with election off the
	// gateway's pod), and the threshold. No elapsed time: the Degraded write is gated on the
	// message, so a figure that moved would write status on every pass.
	a2aGatewayTasksWaitMsgFmt = "A2A gateway Deployment %s: pod %s, the one that binds the event relay, has not turned ready for more than %s " +
		"since the bus was recorded provisioned (BusProvisioned=True), with the NATS server up. Its readiness is its event relay binding the TASKS stream, " +
		"which the provision Job created, so the gateway most likely cannot see that stream: its NATS identity lands in a " +
		"different account than the streams, its credentials are wrong, or a JetStream domain or prefix does not match. " +
		"Read the gateway's /readyz body on its metrics port, which names what it waits on, and its log"
	// a2aGatewayTasksWaitStreamsSuffixFmt is added when BusStreamsPresent
	// is on the CR and not True, naming its reason: that condition then
	// says whether the stream is there at all.
	a2aGatewayTasksWaitStreamsSuffixFmt = "; BusStreamsPresent reads %s (%s), which says whether the stream is there at all"

	// a2aBusStreamsPresentConditionType is #5474's condition, read by its
	// type string so this file stands before #5474 lands. Whichever of the
	// two lands second replaces it with that change's constant.
	a2aBusStreamsPresentConditionType = "BusStreamsPresent"
)

// a2aGatewayLeaderRead is the A2A gateway's elected leader as the operator
// reads it: the pod named by the gateway Lease's holderIdentity, whether its
// gateway container is ready, and when the Lease was acquired. Shared by the
// Degraded reason here and by anything else that has to know whether the
// gateway that does the work is serving (Ready following the leader, #5593):
// with leader election on, a standby reads Ready while the leader may not,
// so the Deployment's ready count says nothing about the relay.
type a2aGatewayLeaderRead struct {
	Pod        *corev1.Pod
	Ready      bool
	AcquiredAt time.Time
}

// a2aGatewayLeader reads the A2A gateway's elected leader. It returns nil
// with no error when there is none to read: no Lease, no holder, a holder
// whose pod is gone or terminating, or a pod that is not this gateway's.
// The Lease comes from the cache (the operator watches it), the pod from
// the live reader, since its readiness is the question. A read that failed
// for another reason is returned as the error.
func (r *PlatformAgentReconciler) a2aGatewayLeader(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (*a2aGatewayLeaderRead, error) {
	lease := &coordinationv1.Lease{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aGatewayLeaseName(agent)}, lease); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, nil
		}
		return nil, err
	}
	if lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity == "" {
		return nil, nil
	}
	pod := &corev1.Pod{}
	if err := r.podReader().Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: *lease.Spec.HolderIdentity}, pod); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, nil
		}
		return nil, err
	}
	if pod.Labels["app"] != a2aGatewayName(agent) || !pod.DeletionTimestamp.IsZero() {
		return nil, nil
	}
	read := &a2aGatewayLeaderRead{Pod: pod}
	if lease.Spec.AcquireTime != nil {
		read.AcquiredAt = lease.Spec.AcquireTime.Time
	}
	if gateway := a2aGatewayContainerStatus(pod); gateway != nil {
		read.Ready = gateway.Ready
	}
	return read, nil
}

// a2aGatewayContainerStatus is the pod's gateway container status, or nil.
func a2aGatewayContainerStatus(pod *corev1.Pod) *corev1.ContainerStatus {
	for i := range pod.Status.ContainerStatuses {
		if pod.Status.ContainerStatuses[i].Name == a2aGatewayContainerName {
			return &pod.Status.ContainerStatuses[i]
		}
	}
	return nil
}

// a2aGatewayLeaderTasksWait is a2aGatewayTasksWait for an elected gateway:
// the leader alone binds the relay, so it alone is read, and a standby that
// reads Ready changes nothing. The clock starts at the latest of
// BusProvisioned's transition, the leader container's start and the Lease's
// acquisition, so a standby promoted a moment ago is given the threshold to
// bind from its promotion. A leader whose container is not running is the
// pod fault scan's.
func a2aGatewayLeaderTasksWait(agent *agentv1alpha1.PlatformAgent, leader *a2aGatewayLeaderRead) (time.Time, bool) {
	since, ok := a2aGatewayProvisionedAt(agent)
	if !ok || leader == nil || leader.Ready || leader.Pod.Status.Phase != corev1.PodRunning {
		return time.Time{}, false
	}
	gateway := a2aGatewayContainerStatus(leader.Pod)
	if gateway == nil || gateway.State.Running == nil {
		return time.Time{}, false
	}
	for _, at := range []time.Time{gateway.State.Running.StartedAt.Time, leader.AcquiredAt} {
		if at.After(since) {
			since = at
		}
	}
	return since, true
}

// a2aGatewayProvisionedAt is BusProvisioned's transition to True on a
// rendering stack, false when the stack does not render or the bus is not
// recorded provisioned.
func a2aGatewayProvisionedAt(agent *agentv1alpha1.PlatformAgent) (time.Time, bool) {
	if !a2aStackRendering(agent) {
		return time.Time{}, false
	}
	provisioned := meta.FindStatusCondition(agent.Status.Conditions, busProvisionedConditionType)
	if provisioned == nil || provisioned.Status != metav1.ConditionTrue {
		return time.Time{}, false
	}
	return provisioned.LastTransitionTime.Time, true
}

// a2aGatewayTasksWait reports whether the A2A gateway is waiting for TASKS
// on a provisioned bus, and since when: the later of BusProvisioned's
// transition to True and the youngest gateway container's start. Waiting
// means every live gateway pod is Running with its gateway container
// running and not ready. A container that is not running (waiting to start,
// pulling, crash looping, terminated) is the pod fault scan's to name, so
// such a pod makes this false, as does any ready pod, or none at all.
func a2aGatewayTasksWait(agent *agentv1alpha1.PlatformAgent, pods []corev1.Pod) (time.Time, bool) {
	since, ok := a2aGatewayProvisionedAt(agent)
	if !ok {
		return time.Time{}, false
	}
	live := 0
	for _, pod := range pods {
		if !pod.DeletionTimestamp.IsZero() {
			continue
		}
		live++
		if pod.Status.Phase != corev1.PodRunning {
			return time.Time{}, false
		}
		gateway := a2aGatewayContainerStatus(&pod)
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

// a2aGatewayTasksWaitApplies is the cached precondition for reading the
// gateway's pods at all: the stack renders, BusProvisioned is True, the NATS
// StatefulSet has a ready replica (a bus that is down is a different fault,
// and the gateway would wait on its connection, not on TASKS), and the A2A
// gateway Deployment exists with fewer ready replicas than it asks for: a
// standby can read Ready while the leader does not, so zero ready is not
// the test, but every replica ready includes the leader. Read from the
// cache, so a settled install pays no live read for this on any pass.
func (r *PlatformAgentReconciler) a2aGatewayTasksWaitApplies(ctx context.Context, agent *agentv1alpha1.PlatformAgent) bool {
	if !a2aStackRendering(agent) || !busProvisioned(agent) {
		return false
	}
	nats := &appsv1.StatefulSet{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, nats); err != nil || nats.Status.ReadyReplicas == 0 {
		return false
	}
	gateway := &appsv1.Deployment{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aGatewayName(agent)}, gateway); err != nil {
		return false
	}
	want := int32(1)
	if gateway.Spec.Replicas != nil {
		want = *gateway.Spec.Replicas
	}
	return gateway.Status.ReadyReplicas < want
}

// a2aGatewayTasksWaitRead is whether the gateway is waiting for TASKS, since
// when, and the pod the message names. With leader election on (the
// default), only the elected leader is read (a2aGatewayLeaderTasksWait);
// with it off, every gateway pod (a2aGatewayTasksWait). Call it only past
// a2aGatewayTasksWaitApplies.
func (r *PlatformAgentReconciler) a2aGatewayTasksWaitRead(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, time.Time, bool, error) {
	if a2aGatewayElectionEnabled() {
		leader, err := r.a2aGatewayLeader(ctx, agent)
		if err != nil {
			return "", time.Time{}, false, err
		}
		since, waiting := a2aGatewayLeaderTasksWait(agent, leader)
		if !waiting {
			return "", time.Time{}, false, nil
		}
		return leader.Pod.Name, since, true, nil
	}
	pods, err := r.a2aGatewayTasksWaitPods(ctx, agent)
	if err != nil {
		return "", time.Time{}, false, err
	}
	since, waiting := a2aGatewayTasksWait(agent, pods)
	if !waiting {
		return "", time.Time{}, false, nil
	}
	name := ""
	for _, pod := range pods {
		if pod.DeletionTimestamp.IsZero() {
			name = pod.Name
			break
		}
	}
	return name, since, true, nil
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

// a2aGatewayTasksWaitMessage is the status pass's one read of the wait: the
// Degraded message for a gateway past the threshold ("" when it is not), and
// the requeue the wait needs (0 when none). Pods and Lease updates do not
// wake Reconcile, so the pass that writes the reason has to be scheduled:
//   - past a2aGatewayTasksWaitApplies, with the gateway waiting short of the
//     threshold, the time left;
//   - past it, with the gateway not waiting yet (its leader's pod still
//     starting, no holder yet) or a read that failed, a poll at
//     a2aGatewayTasksWaitPoll, since the wait can start without any watched
//     object changing;
//   - due (the message is returned) or not applying (a settled install, the
//     bus down, the stack not rendering), none.
//
// A due wait schedules nothing whatever reason the Degraded chain picks: one
// that ranks above this writes nothing new when it wins, and its clearing is
// a status write that re-enqueues the CR. A failed read keeps the reason the
// CR already carries, as the pod fault scan keeps a fault it wrote: finding
// nothing is not evidence of nothing.
func (r *PlatformAgentReconciler) a2aGatewayTasksWaitMessage(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, time.Duration) {
	if !r.a2aGatewayTasksWaitApplies(ctx, agent) {
		return "", 0
	}
	pod, since, waiting, err := r.a2aGatewayTasksWaitRead(ctx, agent)
	if err != nil {
		logf.FromContext(ctx).Error(err, "could not read the A2A gateway's pods for the TASKS wait; the Degraded reason is left as the CR last reported it")
		if held := meta.FindStatusCondition(agent.Status.Conditions, degradedConditionType); held != nil &&
			held.Status == metav1.ConditionTrue && held.Reason == conditionReasonA2AGatewayWaitingForTasks {
			return held.Message, 0
		}
		return "", a2aGatewayTasksWaitPoll
	}
	if !waiting {
		return "", a2aGatewayTasksWaitPoll
	}
	if due, left := a2aGatewayTasksWaitDue(since, time.Now()); !due {
		return "", left
	}
	msg := fmt.Sprintf(a2aGatewayTasksWaitMsgFmt, a2aGatewayName(agent), pod, a2aGatewayTasksWaitThreshold)
	if streams := meta.FindStatusCondition(agent.Status.Conditions, a2aBusStreamsPresentConditionType); streams != nil && streams.Status != metav1.ConditionTrue {
		msg += fmt.Sprintf(a2aGatewayTasksWaitStreamsSuffixFmt, streams.Status, streams.Reason)
	}
	return msg, 0
}
