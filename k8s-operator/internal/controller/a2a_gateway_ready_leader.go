// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package controller

import (
	"context"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// a2aGatewayLeaderReadyGrace is how long a newly elected A2A gateway
	// leader may read not ready before Ready follows it. A normal failover's
	// new leader binds the relay within seconds, so inside the grace the
	// gateway counts as the Deployment does and Ready does not flip; a
	// leader still not ready past it (waiting on a bucket or on TASKS)
	// takes Ready to Provisioning. Ready can therefore lag a stuck leader by
	// up to this long.
	a2aGatewayLeaderReadyGrace = 30 * time.Second
	// a2aGatewayLeaseSkew is added to the Lease's duration before its holder
	// counts as stale: renewTime is the gateway pod's clock, compared here
	// with the operator's.
	a2aGatewayLeaseSkew = 2 * time.Second
	// a2aGatewayLeaderRecheck is how soon a pass that followed a not-ready
	// leader comes back when the Lease says nothing sooner. Lease updates
	// reconcile nothing (a2aGatewayLeaseEvents), so a handover to a Ready
	// standby is seen only by a scheduled pass.
	a2aGatewayLeaderRecheck = 15 * time.Second
)

// a2aGatewayReadyByLeader is the A2A gateway's ready count for Ready on an
// install whose gateway is leader-elected. Only the elected leader binds the
// relay; a standby reads Ready while the leader may not, so the Deployment's
// count overstates what serves. The count is 0 when the current leader is not
// ready and was elected at least a2aGatewayLeaderReadyGrace ago; otherwise it
// is the Deployment's. The second result names the leader pod when it was
// followed to 0, and the third is when to look again (0 for no need).
//
// The leader is read only when the count is split (some replicas ready, not
// all): with every replica ready the leader is, and with none the count is
// already 0. Anything that leaves no current leader to follow falls back to
// the Deployment's count, as Ready read before: no Lease or holder, a holder
// whose pod is gone, terminating or not this gateway's, a holder that stopped
// renewing, or a read that failed (logged; a status pass does not fail over
// a Lease or pod read).
func (r *PlatformAgentReconciler) a2aGatewayReadyByLeader(ctx context.Context, agent *agentv1alpha1.PlatformAgent, gateway *appsv1.Deployment, now time.Time) (int32, string, time.Duration) {
	ready := gateway.Status.ReadyReplicas
	want := int32(1)
	if gateway.Spec.Replicas != nil {
		want = *gateway.Spec.Replicas
	}
	if !a2aGatewayElectionEnabled() || ready == 0 || ready >= want {
		return ready, "", 0
	}
	leader, err := r.a2aGatewayLeader(ctx, agent)
	if err != nil {
		logf.FromContext(ctx).V(1).Info("the A2A gateway's leader could not be read; Ready counts its Deployment", "err", err.Error())
		return ready, "", 0
	}
	if leader == nil || leader.Ready || a2aGatewayLeaderStale(leader, now) {
		return ready, "", 0
	}
	if left := leader.AcquiredAt.Add(a2aGatewayLeaderReadyGrace).Sub(now); left > 0 {
		return ready, "", left
	}
	recheck := a2aGatewayLeaderRecheck
	if !leader.RenewedAt.IsZero() && leader.LeaseDuration > 0 {
		if staleIn := leader.RenewedAt.Add(leader.LeaseDuration + a2aGatewayLeaseSkew).Sub(now); staleIn > 0 {
			recheck = min(recheck, staleIn)
		}
	}
	return 0, leader.Pod.Name, recheck
}

// a2aGatewayLeaderStale reports whether the Lease's holder stopped renewing:
// renewTime plus the duration and a skew margin is past. A Lease without
// either field can't be judged and is not stale.
func a2aGatewayLeaderStale(leader *a2aGatewayLeaderRead, now time.Time) bool {
	if leader.RenewedAt.IsZero() || leader.LeaseDuration <= 0 {
		return false
	}
	return now.After(leader.RenewedAt.Add(leader.LeaseDuration + a2aGatewayLeaseSkew))
}
