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

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// a2aBusSingleServerReason is the Normal Event a one-server A2A bus gets
	// once per episode (noteA2ABusSingleServer): it names the outage a node
	// drain or upgrade causes. It no longer advises converting to three
	// servers while that conversion is refused (#6298). Not a condition: a
	// single server is a supported shape, so Ready and Degraded stay as they
	// are.
	a2aBusSingleServerReason  = "A2ABusSingleServer"
	a2aBusSingleServerMessage = "the A2A bus runs one NATS server, so a node drain or upgrade takes it down, typically for about 2 minutes, and longer after a node scale-down; converting it to three servers is temporarily disabled: https://github.com/gke-labs/kube-agents/issues/6298"
)

// noteA2ABusSingleServer writes the A2ABusSingleServer Event once per
// episode on a one-server bus. An episode is each bus pod first seen Running
// under a new UID, which is exactly when a drain, an upgrade or a scale-down
// has just replaced it, and never a steady pass.
//
// The last UID warned about is held in memory per agent
// (singleServerBusWarned), with no CR field and no status write. A fresh map
// after an operator restart or a leader failover writes the Event once more
// for the pod already running; that repeat is accepted.
//
// It changes nothing it reads and fails nothing: a read error leaves the
// Event for a later pass.
func (r *PlatformAgentReconciler) noteA2ABusSingleServer(ctx context.Context, agent *agentv1alpha1.PlatformAgent) {
	key := client.ObjectKeyFromObject(agent)
	// Three servers, or a migration in flight (it is the conversion this
	// Event points to): nothing to say, and a later return to one server
	// starts a new episode. A run that ended (Done, Abandoned, Failed) stays
	// recorded on the status, so only one not yet terminal counts.
	run := agent.Status.BusMigration
	if a2aNATSReplicas(agent) != 1 || (run != nil && !busMigrationTerminal(busMigrationPhase(run.Phase))) {
		r.singleServerBusWarned.Delete(key)
		return
	}
	pod := &corev1.Pod{}
	err := r.podReader().Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSName(agent) + "-0"}, pod)
	if err != nil {
		if !apierrors.IsNotFound(err) {
			logf.FromContext(ctx).V(1).Info("reading the A2A bus pod for the single-server notice", "error", err.Error())
		}
		return
	}
	if pod.Status.Phase != corev1.PodRunning || pod.DeletionTimestamp != nil || pod.UID == "" {
		return
	}
	if last, ok := r.singleServerBusWarned.Load(key); ok && last == pod.UID {
		return
	}
	r.recordEvent(agent, corev1.EventTypeNormal, a2aBusSingleServerReason, a2aBusSingleServerMessage)
	r.singleServerBusWarned.Store(key, pod.UID)
}
