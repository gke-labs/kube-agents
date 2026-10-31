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
	"reflect"
	"slices"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// A multi-replica gateway does not start (#2995): above one replica the
// platform-agent container runs leader_elect.py, which has no in-cluster token
// to load, and the data volumes are ReadWriteOnce. The operator caps
// spec.deployment.availability.replicas at supportedGatewayReplicas and says
// so on the CR. These tests pin both halves.

// TestAMultiReplicaSpecRendersTheSingleReplicaGateway renders every object the
// replica count used to change at 2 and 3 replicas and requires each to equal
// the one-replica render exactly, then names the HA pieces that must be absent
// so a failure reads as what came back rather than as a diff.
func TestAMultiReplicaSpecRendersTheSingleReplicaGateway(t *testing.T) {
	render := func(replicas int32) map[string]any {
		agent := haAgent("cap-agent", replicas)
		return map[string]any{
			"Deployment":       buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true}),
			"Service":          buildPlatformService(agent),
			"leader Role":      buildPlatformLeaderRole(agent),
			"data PVC":         buildPVC(agent),
			"system PVC":       buildSystemPVC(agent),
			"config ConfigMap": buildConfigMap(agent, nil),
			"PDB":              buildPlatformPDB(agent),
			"useStatefulSet":   useStatefulSet(agent),
		}
	}
	single := render(1)

	for _, replicas := range []int32{2, 3} {
		t.Run(fmt.Sprintf("replicas=%d", replicas), func(t *testing.T) {
			capped := render(replicas)
			for name, want := range single {
				if !reflect.DeepEqual(capped[name], want) {
					t.Errorf("%s at replicas %d differs from the single-replica render", name, replicas)
				}
			}

			dep := capped["Deployment"].(*appsv1.Deployment)
			if dep.Spec.Replicas == nil || *dep.Spec.Replicas != 1 {
				t.Errorf("Deployment replicas = %d, want 1", ptr.Deref(dep.Spec.Replicas, -1))
			}
			if dep.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType {
				t.Errorf("Deployment strategy = %s, want Recreate", dep.Spec.Strategy.Type)
			}
			gateway := containerNamed(t, dep, appNamePlatformAgent)
			for _, arg := range gateway.Args {
				if strings.Contains(arg, "leader_elect.py") {
					t.Errorf("gateway args run leader_elect.py: %v", gateway.Args)
				}
			}
			for _, name := range []string{"ENABLE_LEADER_ELECTION", "LEADER_ELECTION_LEASE_NAME", "LEADER_ELECTION_NAMESPACE"} {
				if value, found := envValue(gateway, name); found {
					t.Errorf("gateway carries %s=%q", name, value)
				}
			}
			if _, found := buildPlatformService(haAgent("cap-agent", replicas)).Spec.Selector["kubeagents.io/is-leader"]; found {
				t.Errorf("Service selector narrows to kubeagents.io/is-leader")
			}
			for _, rule := range buildPlatformLeaderRole(haAgent("cap-agent", replicas)).Rules {
				if slices.Contains(rule.Resources, "pods") {
					t.Errorf("leader Role grants %v on pods", rule.Verbs)
				}
			}
		})
	}
}

// TestReplicasLimitedConditionFollowsTheSpec drives the reconciler through a
// capped spec and back: replicas 2 sets ReplicasLimited with its reason and the
// requested count, a pass over unchanged state writes nothing, and replicas 1
// or the field removed clears it.
func TestReplicasLimitedConditionFollowsTheSpec(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "cap-agent", Namespace: "test-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{Deployment: &agentv1alpha1.DeploymentSpec{
				Availability: &agentv1alpha1.AvailabilitySpec{Replicas: ptr.To(int32(2))},
			}},
			Harness: &agentv1alpha1.HarnessSpec{ProjectID: "test-project", Location: "us-central1", ClusterName: "test-cluster"},
		},
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(64)}
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	ctx := context.Background()
	reconcile := func(passes int) *agentv1alpha1.PlatformAgent {
		t.Helper()
		for i := range passes {
			if _, err := r.Reconcile(ctx, req); err != nil {
				t.Fatalf("Reconcile %d failed: %v", i+1, err)
			}
		}
		got := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
			t.Fatalf("get agent: %v", err)
		}
		return got
	}

	got := reconcile(2)
	cond := meta.FindStatusCondition(got.Status.Conditions, replicasLimitedConditionType)
	if cond == nil {
		t.Fatalf("no %s condition at replicas 2; conditions: %+v", replicasLimitedConditionType, got.Status.Conditions)
	}
	if cond.Status != metav1.ConditionTrue || cond.Reason != replicasLimitedReason {
		t.Errorf("%s = %s/%s, want True/%s", replicasLimitedConditionType, cond.Status, cond.Reason, replicasLimitedReason)
	}
	for _, want := range []string{"replicas is 2", "one replica at most", "leader_elect.py", "ReadWriteOnce", "issues/3016"} {
		if !strings.Contains(cond.Message, want) {
			t.Errorf("%s message does not mention %q: %s", replicasLimitedConditionType, want, cond.Message)
		}
	}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}, dep); err != nil {
		t.Fatalf("get gateway Deployment: %v", err)
	}
	if dep.Spec.Replicas == nil || *dep.Spec.Replicas != 1 {
		t.Errorf("gateway Deployment replicas = %d, want the capped 1", ptr.Deref(dep.Spec.Replicas, -1))
	}

	// The condition is inside the unchanged comparison as well as the write:
	// a status pass over the same state must not write again (#1392).
	settled := got.ResourceVersion
	if _, err := r.updateStatusReady(ctx, got, "", otlpSourceNone, r.resolveNetpolProfile(ctx, got), a2aProvisionState{}); err != nil {
		t.Fatalf("updateStatusReady (settled) failed: %v", err)
	}
	again := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, again); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	if again.ResourceVersion != settled {
		t.Errorf("a status pass over unchanged state wrote the CR (resourceVersion %s -> %s)", settled, again.ResourceVersion)
	}

	for _, tc := range []struct {
		name     string
		replicas *int32
	}{
		{"replicas 1 clears it", ptr.To(int32(1))},
		{"replicas 3 sets it again", ptr.To(int32(3))},
		{"replicas removed clears it", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			current := &agentv1alpha1.PlatformAgent{}
			if err := cl.Get(ctx, req.NamespacedName, current); err != nil {
				t.Fatalf("get agent: %v", err)
			}
			current.Spec.Deployment.Availability.Replicas = tc.replicas
			if err := cl.Update(ctx, current); err != nil {
				t.Fatalf("update agent: %v", err)
			}
			got := reconcile(1)
			cond := meta.FindStatusCondition(got.Status.Conditions, replicasLimitedConditionType)
			wantCapped := tc.replicas != nil && *tc.replicas > 1
			if wantCapped {
				if cond == nil || !strings.Contains(cond.Message, fmt.Sprintf("replicas is %d", *tc.replicas)) {
					t.Errorf("%s = %+v, want it set naming %d replicas", replicasLimitedConditionType, cond, *tc.replicas)
				}
			} else if cond != nil {
				t.Errorf("%s still present: %+v", replicasLimitedConditionType, cond)
			}
		})
	}
}

// TestTheDegradedPathCarriesReplicasLimitedOnlyAfterARender pins the second
// status writer: a CR parked Degraded after a pass that rendered reports the
// cap, and one parked before the render neither writes nor clears it, as with
// VolumesDropped.
func TestTheDegradedPathCarriesReplicasLimitedOnlyAfterARender(t *testing.T) {
	for _, tc := range []struct {
		name     string
		rendered workloadRenderState
	}{
		{"after a render", workloadRendered},
		{"before a render", workloadNotRendered},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := haAgent("cap-degraded", 3)
			scheme := setupScheme()
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(agent).
				WithStatusSubresource(agent).
				Build()
			r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
			ctx := context.Background()

			if err := r.updateStatusDegraded(ctx, agent, reasonShellSandboxKeysMissing, "the sandbox keypair Secret is missing", tc.rendered); err != nil {
				t.Fatalf("updateStatusDegraded failed: %v", err)
			}
			cond := meta.FindStatusCondition(agent.Status.Conditions, replicasLimitedConditionType)
			if bool(tc.rendered) != (cond != nil) {
				t.Fatalf("%s present = %v after a pass with rendered=%v", replicasLimitedConditionType, cond != nil, tc.rendered)
			}
			if cond != nil && cond.Reason != replicasLimitedReason {
				t.Errorf("%s reason = %s, want %s", replicasLimitedConditionType, cond.Reason, replicasLimitedReason)
			}

			stored := &agentv1alpha1.PlatformAgent{}
			if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
				t.Fatalf("reading the PlatformAgent back: %v", err)
			}
			settled := stored.ResourceVersion
			if err := r.updateStatusDegraded(ctx, agent, reasonShellSandboxKeysMissing, "the sandbox keypair Secret is missing", tc.rendered); err != nil {
				t.Fatalf("updateStatusDegraded (settled) failed: %v", err)
			}
			if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
				t.Fatalf("reading the PlatformAgent back: %v", err)
			}
			if stored.ResourceVersion != settled {
				t.Errorf("a second parked pass wrote the CR (resourceVersion %s -> %s)", settled, stored.ResourceVersion)
			}
		})
	}
}

// TestAPreCapHAGatewayIsCappedInPlaceEnvtest is the upgrade against a real
// kube-apiserver: a gateway Deployment the previous release applied at three
// replicas (RollingUpdate, leader_elect.py args), then a reconcile by this
// one. The apply has to be accepted, in place, with the rollingUpdate block
// gone: a Recreate strategy that kept it would be refused as Invalid and the
// install would stop reconciling. The status write has to pass the CRD's
// condition schema too, which only a real API server checks. Clearing the
// field then removes the condition.
func TestAPreCapHAGatewayIsCappedInPlaceEnvtest(t *testing.T) {
	cl, scheme := startEnvtest(t)
	ctx := context.Background()
	const namespace = "replicas-cap"
	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: envtestAgentName, Namespace: namespace},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{Deployment: &agentv1alpha1.DeploymentSpec{
				Availability: &agentv1alpha1.AvailabilitySpec{Replicas: ptr.To(int32(3))},
			}},
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   envtestHarnessProject,
				Location:    envtestHarnessLocation,
				ClusterName: envtestHarnessCluster,
			},
		},
	}
	if err := cl.Create(ctx, agent); err != nil {
		t.Fatalf("creating PlatformAgent: %v", err)
	}
	if err := cl.Create(ctx, shellSandboxKeysSecret(agent)); err != nil {
		t.Fatalf("creating sandbox keys Secret: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}

	// The previous release's shape, applied under the operator's own field
	// manager so it owns the rollingUpdate block it set.
	preCap := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{})
	_, haStrategy := replicasAndStrategyFor(3, false)
	preCap.Spec.Replicas = ptr.To(int32(3))
	preCap.Spec.Strategy = haStrategy
	for i := range preCap.Spec.Template.Spec.Containers {
		if preCap.Spec.Template.Spec.Containers[i].Name == appNamePlatformAgent {
			preCap.Spec.Template.Spec.Containers[i].Args = []string{"/opt/hermes/.venv/bin/python3", "/opt/data/leader_elect.py"}
		}
	}
	if err := r.applyManaged(ctx, agent, preCap); err != nil {
		t.Fatalf("applying the pre-cap HA gateway: %v", err)
	}
	gatewayKey := client.ObjectKey{Namespace: namespace, Name: agent.Name + "-gateway"}
	before := &appsv1.Deployment{}
	if err := cl.Get(ctx, gatewayKey, before); err != nil {
		t.Fatalf("reading the pre-cap gateway: %v", err)
	}
	if before.Spec.Strategy.RollingUpdate == nil {
		t.Fatal("test setup: the pre-cap gateway carries no rollingUpdate block, so the transition under test does not exist")
	}

	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	for _, pass := range []string{"finalizer", "first full pass"} {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile (%s) failed: %v", pass, err)
		}
	}

	after := &appsv1.Deployment{}
	if err := cl.Get(ctx, gatewayKey, after); err != nil {
		t.Fatalf("reading the capped gateway: %v", err)
	}
	if after.UID != before.UID {
		t.Errorf("gateway UID changed from %s to %s; the cap must apply in place", before.UID, after.UID)
	}
	if after.Spec.Replicas == nil || *after.Spec.Replicas != 1 {
		t.Errorf("gateway replicas = %d, want 1", ptr.Deref(after.Spec.Replicas, -1))
	}
	if after.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType || after.Spec.Strategy.RollingUpdate != nil {
		t.Errorf("gateway strategy = %+v, want Recreate with no rollingUpdate block", after.Spec.Strategy)
	}
	for _, c := range after.Spec.Template.Spec.Containers {
		if c.Name == appNamePlatformAgent && len(c.Args) != 0 {
			t.Errorf("gateway args = %v, want the image CMD", c.Args)
		}
	}

	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	cond := meta.FindStatusCondition(got.Status.Conditions, replicasLimitedConditionType)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != replicasLimitedReason ||
		!strings.Contains(cond.Message, "replicas is 3") {
		t.Fatalf("%s = %+v, want True/%s naming 3 replicas", replicasLimitedConditionType, cond, replicasLimitedReason)
	}

	got.Spec.Deployment.Availability.Replicas = nil
	if err := cl.Update(ctx, got); err != nil {
		t.Fatalf("clearing availability.replicas: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile (cleared) failed: %v", err)
	}
	cleared := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, cleared); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	if cond := meta.FindStatusCondition(cleared.Status.Conditions, replicasLimitedConditionType); cond != nil {
		t.Errorf("%s still present after availability.replicas was cleared: %+v", replicasLimitedConditionType, cond)
	}
}
