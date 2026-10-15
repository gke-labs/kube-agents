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
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// initContainerMemoryLimit returns the agent-api-auth sidecar's memory limit
// from the gateway workload's pod template, whether it is a Deployment or a
// StatefulSet.
func initContainerMemoryLimit(t *testing.T, tmpl corev1.PodTemplateSpec) resource.Quantity {
	t.Helper()
	for _, c := range tmpl.Spec.InitContainers {
		if c.Name == agentAPIAuthContainerName {
			return c.Resources.Limits[corev1.ResourceMemory]
		}
	}
	t.Fatalf("no %s init container in the gateway pod template", agentAPIAuthContainerName)
	return resource.Quantity{}
}

// TestReconcileInvalidAgentAPIAuthResources pins the webhook-off reconciler
// path for a refused spec.deployment.agentAPIAuth.resources: the sidecar is
// rendered at the operator's defaults, the CR reports Degraded with the reason,
// Ready is untouched, and a Warning event names the field — on both the
// Deployment and the StatefulSet gateway, because the strip sits above the
// fork. A request above its limit is the refusal the API server would also
// reject, so the test proves the operator ignores it rather than writing it.
func TestReconcileInvalidAgentAPIAuthResources(t *testing.T) {
	refused := &corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("5Gi")},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
	}
	rwo := "rwo-ssd"
	two := int32(2)
	cases := []struct {
		name        string
		statefulSet bool
		deployment  *agentv1alpha1.DeploymentSpec
	}{
		{"deployment gateway", false, &agentv1alpha1.DeploymentSpec{
			AgentAPIAuth: &agentv1alpha1.AgentAPIAuthSpec{Resources: refused},
		}},
		{"statefulset gateway", true, &agentv1alpha1.DeploymentSpec{
			AgentAPIAuth: &agentv1alpha1.AgentAPIAuthSpec{Resources: refused},
			Availability: &agentv1alpha1.AvailabilitySpec{Replicas: &two},
			Storages: []agentv1alpha1.StorageSpec{{
				Name: "data", StorageClassName: &rwo, StorageSize: "5Gi", MountPath: "/data",
				AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			}},
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			agent := &agentv1alpha1.PlatformAgent{
				ObjectMeta: metav1.ObjectMeta{Name: "test-agent-apiauth", Namespace: "test-ns"},
				Spec: agentv1alpha1.PlatformAgentSpec{
					AgentSpec: agentv1alpha1.AgentSpec{Deployment: tc.deployment},
					Harness:   &agentv1alpha1.HarnessSpec{ProjectID: "test-project", Location: "us-central1", ClusterName: "test-cluster"},
				},
			}
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(agent, shellSandboxKeysSecret(agent)).
				WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
				WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
				Build()
			recorder := record.NewFakeRecorder(64)
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: recorder}
			req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
			ctx := context.Background()
			for i := range 2 {
				if _, err := r.Reconcile(ctx, req); err != nil {
					t.Fatalf("Reconcile %d failed: %v", i+1, err)
				}
			}

			updated := &agentv1alpha1.PlatformAgent{}
			if err := cl.Get(ctx, req.NamespacedName, updated); err != nil {
				t.Fatalf("get agent: %v", err)
			}
			degraded := meta.FindStatusCondition(updated.Status.Conditions, "Degraded")
			if degraded == nil || degraded.Status != metav1.ConditionTrue || degraded.Reason != conditionReasonInvalidAgentAPIAuthResources {
				t.Fatalf("Degraded = %v, want True/%s", degraded, conditionReasonInvalidAgentAPIAuthResources)
			}
			if ready := meta.FindStatusCondition(updated.Status.Conditions, "Ready"); ready != nil && ready.Reason == conditionReasonInvalidAgentAPIAuthResources {
				t.Errorf("Ready reason = %s; the refusal is reported on Degraded only", ready.Reason)
			}
			if updated.Status.Phase == "Degraded" {
				t.Errorf("Status.Phase = Degraded; the sidecar runs at defaults, so the refusal is reported on the Degraded condition only")
			}

			// The gateway workload carries the sidecar at the 2Gi default, not the
			// refused 4Gi override — on whichever workload kind this path renders.
			gwKey := client.ObjectKey{Name: agent.Name + "-gateway", Namespace: agent.Namespace}
			var tmpl corev1.PodTemplateSpec
			if tc.statefulSet {
				sts := &appsv1.StatefulSet{}
				if err := cl.Get(ctx, gwKey, sts); err != nil {
					t.Fatalf("get gateway StatefulSet: %v", err)
				}
				tmpl = sts.Spec.Template
			} else {
				dep := &appsv1.Deployment{}
				if err := cl.Get(ctx, gwKey, dep); err != nil {
					t.Fatalf("get gateway Deployment: %v", err)
				}
				tmpl = dep.Spec.Template
			}
			if got := initContainerMemoryLimit(t, tmpl); got.Cmp(resource.MustParse("2Gi")) != 0 {
				t.Errorf("sidecar memory limit = %s, want the 2Gi default; a refused override must be ignored", got.String())
			}

			// With every workload ready the agent reaches Ready while Degraded still
			// carries the refusal: a refused override must not keep the agent out of
			// Ready, which is what the field's doc and values.yaml promise. The
			// reconciles above leave the fake workloads not-yet-ready, so this drives
			// the status writer directly after marking them ready, as the credential
			// proxy's sibling test does.
			deps := &appsv1.DeploymentList{}
			if err := cl.List(ctx, deps, client.InNamespace(agent.Namespace)); err != nil {
				t.Fatalf("list deployments: %v", err)
			}
			for i := range deps.Items {
				d := &deps.Items[i]
				want := int32(1)
				if d.Spec.Replicas != nil {
					want = *d.Spec.Replicas
				}
				d.Status.Replicas = want
				d.Status.ReadyReplicas = want
				if err := cl.Status().Update(ctx, d); err != nil {
					t.Fatalf("mark %s ready: %v", d.Name, err)
				}
			}
			stses := &appsv1.StatefulSetList{}
			if err := cl.List(ctx, stses, client.InNamespace(agent.Namespace)); err != nil {
				t.Fatalf("list statefulsets: %v", err)
			}
			for i := range stses.Items {
				st := &stses.Items[i]
				want := int32(1)
				if st.Spec.Replicas != nil {
					want = *st.Spec.Replicas
				}
				st.Status.Replicas = want
				st.Status.ReadyReplicas = want
				if err := cl.Status().Update(ctx, st); err != nil {
					t.Fatalf("mark %s ready: %v", st.Name, err)
				}
			}
			r.APIReader = cl
			phase, err := r.updateStatusReady(ctx, updated, "", otlpSourceNone, r.resolveNetpolProfile(ctx, updated), a2aStateFrom(t, ctx, r, updated))
			if err != nil {
				t.Fatalf("updateStatusReady failed: %v", err)
			}
			if phase != "Ready" {
				t.Errorf("phase with every workload ready = %q, want Ready; a refused override must not hold the agent out of Ready", phase)
			}
			if ready := meta.FindStatusCondition(updated.Status.Conditions, "Ready"); ready == nil || ready.Status != metav1.ConditionTrue {
				t.Errorf("Ready with every workload ready = %v, want True", ready)
			}
			if degraded := meta.FindStatusCondition(updated.Status.Conditions, "Degraded"); degraded == nil || degraded.Status != metav1.ConditionTrue || degraded.Reason != conditionReasonInvalidAgentAPIAuthResources {
				t.Errorf("Degraded with every workload ready = %v, want True/%s", degraded, conditionReasonInvalidAgentAPIAuthResources)
			}

			close(recorder.Events)
			warned := false
			for event := range recorder.Events {
				if strings.HasPrefix(event, corev1.EventTypeWarning+" "+conditionReasonInvalidAgentAPIAuthResources) {
					warned = true
				}
			}
			if !warned {
				t.Errorf("no Warning %s event", conditionReasonInvalidAgentAPIAuthResources)
			}
		})
	}
}
