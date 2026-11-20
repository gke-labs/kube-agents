/*
Copyright 2026 Google LLC

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

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func TestControllerReportsZeroSlotKanbanAdmissionDegraded(t *testing.T) {
	t.Parallel()
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent-kanban-admission", Namespace: "test-ns", Generation: 1},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{Deployment: &agentv1alpha1.DeploymentSpec{
				Resources: &corev1.ResourceRequirements{
					Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("512Mi")},
				},
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
	recorder := record.NewFakeRecorder(32)
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
		t.Fatalf("failed to get agent: %v", err)
	}
	if updated.Status.Phase == degradedPhase {
		t.Errorf("Status.Phase = %s; InvalidKanbanAdmission should leave Ready/Phase to workloads", updated.Status.Phase)
	}
	degraded := meta.FindStatusCondition(updated.Status.Conditions, degradedConditionType)
	if degraded == nil || degraded.Status != metav1.ConditionTrue || degraded.Reason != conditionReasonInvalidKanbanAdmission {
		t.Fatalf("Degraded condition = %+v, want True/%s", degraded, conditionReasonInvalidKanbanAdmission)
	}
	if !strings.Contains(degraded.Message, "spec.deployment.resources.requests.memory") {
		t.Errorf("Degraded message %q does not name spec.deployment.resources.requests.memory", degraded.Message)
	}
	if ready := meta.FindStatusCondition(updated.Status.Conditions, readyConditionType); ready == nil || ready.Reason == conditionReasonInvalidKanbanAdmission {
		t.Errorf("Ready condition = %+v; InvalidKanbanAdmission should be reported on Degraded", ready)
	}

	// Clearing the sub-cap override restores normal status and removes Degraded.
	updated.Spec.Deployment.Resources = nil
	updated.Generation = 2
	if err := cl.Update(ctx, updated); err != nil {
		t.Fatalf("failed to clear spec.deployment.resources: %v", err)
	}
	for i := range 2 {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile after fix %d failed: %v", i+1, err)
		}
	}
	afterFix := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, afterFix); err != nil {
		t.Fatalf("failed to get agent after fix: %v", err)
	}
	if d := meta.FindStatusCondition(afterFix.Status.Conditions, degradedConditionType); d != nil {
		t.Errorf("Degraded condition remained after fixing sub-cap override: %+v", d)
	}

	close(recorder.Events)
	var warned bool
	for event := range recorder.Events {
		if strings.HasPrefix(event, corev1.EventTypeWarning+" "+conditionReasonInvalidKanbanAdmission) &&
			strings.Contains(event, "spec.deployment.resources.requests.memory") {
			warned = true
		}
	}
	if !warned {
		t.Errorf("no Warning %s event naming spec.deployment.resources.requests.memory", conditionReasonInvalidKanbanAdmission)
	}
}

func TestSpecResourceDegradedIncludesInvalidKanbanAdmission(t *testing.T) {
	t.Parallel()
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID:   "test-project",
		Location:    "us-central1",
		ClusterName: "test-cluster",
		Tuning: &agentv1alpha1.TuningSpec{
			Admission: &agentv1alpha1.AdmissionSpec{
				MemoryBudgetMiB: ptr.To[int32](1024),
			},
		},
	}
	reason, msg := specResourceDegraded(agent)
	if reason != conditionReasonInvalidKanbanAdmission {
		t.Fatalf("specResourceDegraded reason = %q, want %s", reason, conditionReasonInvalidKanbanAdmission)
	}
	if !strings.Contains(msg, "1024 MiB") || !strings.Contains(msg, "1536 MiB") {
		t.Errorf("specResourceDegraded message %q lacks budget/cap figures", msg)
	}

	// failClosed=false does not waive the budget >= cap invariant.
	agent.Spec.Harness.Tuning.Admission.FailClosed = ptr.To(false)
	reasonOpen, msgOpen := specResourceDegraded(agent)
	if reasonOpen != conditionReasonInvalidKanbanAdmission || !strings.Contains(msgOpen, "1024 MiB") {
		t.Errorf("failClosed=false should still flag InvalidKanbanAdmission, got %q %q", reasonOpen, msgOpen)
	}

	// Healthy >=4Gi deployment resources paired with an oversized cap blame workerRssCapMiB.
	capOversized := a2aTestAgent()
	capOversized.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Resources: &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
		},
	}
	capOversized.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID:   "test-project",
		Location:    "us-central1",
		ClusterName: "test-cluster",
		Tuning: &agentv1alpha1.TuningSpec{
			Admission: &agentv1alpha1.AdmissionSpec{
				WorkerRssCapMiB: ptr.To[int32](5000),
			},
		},
	}
	errs := ValidateKanbanAdmission(capOversized)
	if len(errs) != 1 || errs[0].Field != "spec.harness.tuning.admission.workerRssCapMiB" {
		t.Errorf("oversized cap with 4Gi request blamed %v, want spec.harness.tuning.admission.workerRssCapMiB", errs)
	}

	// Explicit memoryBudgetMiB >= 1536 overridden by larger workerRssCapMiB must not suggest editing spec.deployment.resources.
	explicitCapOversized := a2aTestAgent()
	explicitCapOversized.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Resources: &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("8Gi")},
		},
	}
	explicitCapOversized.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID:   "test-project",
		Location:    "us-central1",
		ClusterName: "test-cluster",
		Tuning: &agentv1alpha1.TuningSpec{
			Admission: &agentv1alpha1.AdmissionSpec{
				MemoryBudgetMiB: ptr.To[int32](2048),
				WorkerRssCapMiB: ptr.To[int32](4096),
			},
		},
	}
	explicitErrs := ValidateKanbanAdmission(explicitCapOversized)
	if len(explicitErrs) != 1 || explicitErrs[0].Field != "spec.harness.tuning.admission.workerRssCapMiB" {
		t.Fatalf("oversized cap with explicit 2048MiB budget blamed %v, want spec.harness.tuning.admission.workerRssCapMiB", explicitErrs)
	}
	if strings.Contains(explicitErrs[0].Detail, "spec.deployment.resources") {
		t.Errorf("explicit memoryBudgetMiB mismatch erroneously suggested spec.deployment.resources: %q", explicitErrs[0].Detail)
	}
}
