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
	"errors"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// haGateway sets the A2A gateway Deployment to want replicas with ready of
// them ready; the fake client runs no Deployment controller.
func haGateway(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent, want, ready int32) {
	t.Helper()
	ctx := context.Background()
	gw := &appsv1.Deployment{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aGatewayName(agent)}, gw); err != nil {
		t.Fatal(err)
	}
	gw.Spec.Replicas = ptr.To(want)
	if err := r.Update(ctx, gw); err != nil {
		t.Fatal(err)
	}
	gw.Status.ReadyReplicas = ready
	if err := r.Status().Update(ctx, gw); err != nil {
		t.Fatal(err)
	}
}

// agentGatewayReady reports the agent's own gateway workload ready, so the
// Provisioning message lists the workloads still waiting.
func agentGatewayReady(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	ctx := context.Background()
	key := client.ObjectKey{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}
	dep := &appsv1.Deployment{}
	if err := r.Get(ctx, key, dep); err == nil {
		dep.Status.ReadyReplicas = 1
		if err := r.Status().Update(ctx, dep); err != nil {
			t.Fatal(err)
		}
		return
	}
	sts := &appsv1.StatefulSet{}
	if err := r.Get(ctx, key, sts); err != nil {
		t.Fatalf("the agent's gateway workload: %v", err)
	}
	sts.Status.ReadyReplicas = 1
	if err := r.Status().Update(ctx, sts); err != nil {
		t.Fatal(err)
	}
}

// renewLease sets the gateway Lease's renew time and duration, as the
// gateway's elector writes them on every renewal.
func renewLease(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent, at time.Time, duration time.Duration) {
	t.Helper()
	ctx := context.Background()
	lease := &coordinationv1.Lease{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aGatewayLeaseName(agent)}, lease); err != nil {
		t.Fatal(err)
	}
	lease.Spec.RenewTime = &metav1.MicroTime{Time: at}
	lease.Spec.LeaseDurationSeconds = ptr.To(int32(duration / time.Second))
	if err := r.Update(ctx, lease); err != nil {
		t.Fatal(err)
	}
}

// standbyGateway creates a Ready standby gateway pod beside the leader.
func standbyGateway(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent) *corev1.Pod {
	t.Helper()
	started := time.Now().Add(-time.Hour)
	pod := a2aGatewayPod(agent, a2aGatewayName(agent)+"-standby", &started, true)
	if err := r.Create(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	return pod
}

// gatewayWorkload is the A2A gateway's entry in readSplitWorkloads.
func gatewayWorkload(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent, state a2aProvisionState) splitWorkloadStatus {
	t.Helper()
	workloads, _, err := r.readSplitWorkloads(context.Background(), agent, state)
	if err != nil {
		t.Fatal(err)
	}
	for _, w := range workloads {
		if w.name == a2aGatewayName(agent) {
			return w
		}
	}
	t.Fatalf("no A2A gateway in %+v", workloads)
	return splitWorkloadStatus{}
}

// The shape seen live: two replicas, the elected leader not ready past the
// handover grace, the standby Ready. The Deployment reads 1/2 ready; Ready
// must not, and its message names the leader.
func TestReadyFollowsANotReadyLeaderPastTheGrace(t *testing.T) {
	r, stored, state := settledA2AInstall(t)
	ctx := context.Background()
	haGateway(t, r, stored, 2, 1)
	agentGatewayReady(t, r, stored)
	leader := waitingLeader(t, r, stored, 10*time.Minute)
	standby := standbyGateway(t, r, stored)

	if w := gatewayWorkload(t, r, stored, state); w.ready != 0 {
		t.Fatalf("gateway counted %d ready with its leader not ready", w.ready)
	}
	_, requeue, err := r.updateStatusReady(ctx, stored, "", otlpSourceNone, r.resolveNetpolProfile(ctx, stored), state)
	if err != nil {
		t.Fatal(err)
	}
	// Lease updates send no event, so a handover to the Ready standby is
	// seen only by a scheduled pass.
	if requeue <= 0 || requeue > a2aGatewayLeaderRecheck {
		t.Errorf("requeue = %s, want within %s while following a not-ready leader", requeue, a2aGatewayLeaderRecheck)
	}
	ready := meta.FindStatusCondition(stored.Status.Conditions, "Ready")
	if ready == nil || ready.Status != metav1.ConditionFalse || ready.Reason != "Provisioning" {
		t.Fatalf("Ready = %+v, want False/Provisioning", ready)
	}
	if !strings.Contains(ready.Message, a2aGatewayName(stored)) || !strings.Contains(ready.Message, leader.Name) {
		t.Errorf("Ready message does not name the gateway and its leader %s: %s", leader.Name, ready.Message)
	}
	if strings.Contains(ready.Message, standby.Name) {
		t.Errorf("Ready message names the standby: %s", ready.Message)
	}
}

// A leader elected moments ago gets the grace to bind before Ready follows
// it: a normal failover does not flip Ready. The pass asks to come back when
// the grace ends.
func TestANewlyElectedLeaderGetsTheGrace(t *testing.T) {
	r, stored, state := settledA2AInstall(t)
	ctx := context.Background()
	haGateway(t, r, stored, 2, 1)
	standbyGateway(t, r, stored)
	waitingLeader(t, r, stored, 10*time.Minute)
	holdLease(t, r, stored, a2aGatewayName(stored)+"-abc", time.Now().Add(-5*time.Second))

	if w := gatewayWorkload(t, r, stored, state); w.ready != 1 {
		t.Fatalf("gateway counted %d ready inside the grace, want the Deployment's 1", w.ready)
	}
	_, requeue, err := r.updateStatusReady(ctx, stored, "", otlpSourceNone, r.resolveNetpolProfile(ctx, stored), state)
	if err != nil {
		t.Fatal(err)
	}
	if requeue <= 0 || requeue > a2aGatewayLeaderReadyGrace {
		t.Errorf("requeue = %s, want within the %s grace", requeue, a2aGatewayLeaderReadyGrace)
	}
}

// A Ready leader is the Deployment's count, whatever a standby reads.
func TestAReadyLeaderKeepsTheDeploymentsCount(t *testing.T) {
	r, stored, state := settledA2AInstall(t)
	haGateway(t, r, stored, 2, 1)
	standby := standbyGateway(t, r, stored)
	waitingLeader(t, r, stored, 10*time.Minute)
	holdLease(t, r, stored, standby.Name, time.Now().Add(-time.Hour))

	if w := gatewayWorkload(t, r, stored, state); w.ready != 1 {
		t.Errorf("gateway counted %d ready with a Ready leader, want 1", w.ready)
	}
}

// With nothing current to follow, Ready reads the Deployment as it always
// has: no Lease holder, a holder whose pod is gone, or a holder that stopped
// renewing.
func TestWithNoCurrentLeaderReadyFallsBackToTheDeployment(t *testing.T) {
	cases := map[string]func(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent){
		"no holder": func(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent) {
			holdLease(t, r, agent, "", time.Now().Add(-time.Hour))
		},
		"holder pod gone": func(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent) {
			holdLease(t, r, agent, a2aGatewayName(agent)+"-gone", time.Now().Add(-time.Hour))
		},
		"stale holder": func(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent) {
			renewLease(t, r, agent, time.Now().Add(-time.Minute), 15*time.Second)
		},
	}
	for name, setup := range cases {
		t.Run(name, func(t *testing.T) {
			r, stored, state := settledA2AInstall(t)
			haGateway(t, r, stored, 2, 1)
			standbyGateway(t, r, stored)
			waitingLeader(t, r, stored, 10*time.Minute)
			setup(t, r, stored)
			if w := gatewayWorkload(t, r, stored, state); w.ready != 1 {
				t.Errorf("gateway counted %d ready, want the Deployment's 1", w.ready)
			}
		})
	}
}

// A holder that renewed within its duration is current, and followed.
func TestARenewingLeaderIsFollowed(t *testing.T) {
	r, stored, state := settledA2AInstall(t)
	haGateway(t, r, stored, 2, 1)
	standbyGateway(t, r, stored)
	waitingLeader(t, r, stored, 10*time.Minute)
	renewLease(t, r, stored, time.Now().Add(-2*time.Second), 15*time.Second)
	if w := gatewayWorkload(t, r, stored, state); w.ready != 0 {
		t.Errorf("gateway counted %d ready with a renewing, not-ready leader", w.ready)
	}
}

// With the election off there is no leader, and every replica counts.
func TestWithoutElectionReadyCountsTheDeployment(t *testing.T) {
	t.Setenv(a2aGatewayElectionEnvVar, "false")
	r, stored, state := settledA2AInstall(t)
	haGateway(t, r, stored, 2, 1)
	standbyGateway(t, r, stored)
	waitingLeader(t, r, stored, 10*time.Minute)
	if w := gatewayWorkload(t, r, stored, state); w.ready != 1 {
		t.Errorf("gateway counted %d ready with the election off, want 1", w.ready)
	}
}

// A gateway whose replicas are all ready, or none, costs no pod read: the
// leader can only change the answer between the two.
func TestTheLeaderIsReadOnlyWhenTheCountIsSplit(t *testing.T) {
	for _, tc := range []struct {
		name        string
		want, ready int32
	}{{"all ready", 2, 2}, {"none ready", 2, 0}, {"one of one", 1, 1}} {
		t.Run(tc.name, func(t *testing.T) {
			r, stored, state := settledA2AInstall(t)
			haGateway(t, r, stored, tc.want, tc.ready)
			waitingLeader(t, r, stored, 10*time.Minute)
			reads := 0
			counting := interceptor.NewClient(r.Client.(client.WithWatch), interceptor.Funcs{
				Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
					if _, ok := obj.(*corev1.Pod); ok {
						reads++
					}
					return c.Get(ctx, key, obj, opts...)
				},
			})
			counted := &PlatformAgentReconciler{Client: counting, APIReader: counting, Scheme: r.Scheme}
			if w := gatewayWorkload(t, counted, stored, state); w.ready != tc.ready {
				t.Errorf("gateway counted %d ready, want %d", w.ready, tc.ready)
			}
			if reads != 0 {
				t.Errorf("%d pod reads, want none", reads)
			}
		})
	}
}

// A leader read that fails is no reason to report the gateway down, nor to
// fail the pass: Ready counts the Deployment, as before.
func TestAFailedLeaderReadFallsBackToTheDeployment(t *testing.T) {
	r, stored, state := settledA2AInstall(t)
	haGateway(t, r, stored, 2, 1)
	standbyGateway(t, r, stored)
	waitingLeader(t, r, stored, 10*time.Minute)
	failing := interceptor.NewClient(r.Client.(client.WithWatch), interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, ok := obj.(*corev1.Pod); ok {
				return errors.New("etcdserver: request timed out")
			}
			return c.Get(ctx, key, obj, opts...)
		},
	})
	broken := &PlatformAgentReconciler{Client: failing, APIReader: failing, Scheme: r.Scheme}
	if w := gatewayWorkload(t, broken, stored, state); w.ready != 1 {
		t.Errorf("gateway counted %d ready on a failed leader read, want the Deployment's 1", w.ready)
	}
}
