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

package webhook

import (
	"context"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func admissionTestAgent(resources *corev1.ResourceRequirements, admission *agentv1alpha1.AdmissionSpec) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
		Spec:       agentv1alpha1.PlatformAgentSpec{},
	}
	if resources != nil {
		agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Resources: resources}
	}
	if admission != nil {
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
			Tuning: &agentv1alpha1.TuningSpec{Admission: admission},
		}
	}
	return agent
}

func TestValidateKanbanAdmissionRejectsZeroSlotConfigurations(t *testing.T) {
	t.Parallel()
	val := &PlatformAgentCustomValidator{}
	ctx := context.Background()

	rejected := []struct {
		name      string
		agent     *agentv1alpha1.PlatformAgent
		wantField string
	}{
		{
			name: "deployment requests memory 512Mi below default 1536MiB worker cap",
			agent: admissionTestAgent(&corev1.ResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("512Mi")},
			}, nil),
			wantField: "spec.deployment.resources.requests.memory",
		},
		{
			name: "deployment requests memory 256Mi clamps to 512MiB floor below default 1536MiB cap",
			agent: admissionTestAgent(&corev1.ResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("256Mi")},
			}, nil),
			wantField: "spec.deployment.resources.requests.memory",
		},
		{
			name: "deployment limits-only 1Gi below default 1536MiB worker cap",
			agent: admissionTestAgent(&corev1.ResourceRequirements{
				Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")},
			}, nil),
			wantField: "spec.deployment.resources.limits.memory",
		},
		{
			name: "explicit memoryBudgetMiB below default workerRssCapMiB",
			agent: admissionTestAgent(nil, &agentv1alpha1.AdmissionSpec{
				MemoryBudgetMiB: ptr.To[int32](1024),
			}),
			wantField: "spec.harness.tuning.admission.memoryBudgetMiB",
		},
		{
			name: "explicit workerRssCapMiB above default 4096MiB budget",
			agent: admissionTestAgent(nil, &agentv1alpha1.AdmissionSpec{
				WorkerRssCapMiB: ptr.To[int32](5000),
			}),
			wantField: "spec.harness.tuning.admission.workerRssCapMiB",
		},
		{
			name: "explicit workerRssCapMiB above explicit 2048MiB budget",
			agent: admissionTestAgent(&corev1.ResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("8Gi")},
			}, &agentv1alpha1.AdmissionSpec{
				MemoryBudgetMiB: ptr.To[int32](2048),
				WorkerRssCapMiB: ptr.To[int32](4096),
			}),
			wantField: "spec.harness.tuning.admission.workerRssCapMiB",
		},
		{
			name: "sub-cap budget with failClosed=false is still rejected",
			agent: admissionTestAgent(&corev1.ResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("512Mi")},
			}, &agentv1alpha1.AdmissionSpec{
				FailClosed: ptr.To(false),
			}),
			wantField: "spec.deployment.resources.requests.memory",
		},
		{
			name: "healthy 6Gi deployment request with workerRssCapMiB=8192 blames workerRssCapMiB",
			agent: admissionTestAgent(&corev1.ResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("6Gi")},
			}, &agentv1alpha1.AdmissionSpec{
				WorkerRssCapMiB: ptr.To[int32](8192),
			}),
			wantField: "spec.harness.tuning.admission.workerRssCapMiB",
		},
	}

	for _, tc := range rejected {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, err := val.ValidateCreate(ctx, tc.agent)
			msg := fieldErrorMessage(t, err, tc.wantField)
			if !strings.Contains(msg, "exceeds the pod's admission budget") {
				t.Errorf("error message %q does not explain budget < cap mismatch", msg)
			}
			if strings.Contains(msg, "admission.memoryBudgetMiB to at least 5000Mi") ||
				strings.Contains(msg, "admission.memoryBudgetMiB to at least 8192Mi") {
				t.Errorf("error message %q attaches Mi suffix to integer memoryBudgetMiB field", msg)
			}
			if strings.Contains(msg, "failClosed: false") {
				t.Errorf("error message %q should not advise failClosed: false", msg)
			}
			if tc.agent.Spec.Harness != nil && tc.agent.Spec.Harness.Tuning != nil &&
				tc.agent.Spec.Harness.Tuning.Admission != nil && tc.agent.Spec.Harness.Tuning.Admission.MemoryBudgetMiB != nil &&
				strings.Contains(msg, "spec.deployment.resources") {
				t.Errorf("error message %q should not suggest spec.deployment.resources when memoryBudgetMiB is explicitly set", msg)
			}
		})
	}

	allowed := []struct {
		name  string
		agent *agentv1alpha1.PlatformAgent
	}{
		{
			name:  "defaults (4Gi budget >= 1536MiB cap)",
			agent: admissionTestAgent(nil, nil),
		},
		{
			name: "exact boundary requests memory 1536Mi == 1536MiB",
			agent: admissionTestAgent(&corev1.ResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1536Mi")},
			}, nil),
		},
		{
			name: "small deployment request with explicit memoryBudgetMiB >= cap",
			agent: admissionTestAgent(&corev1.ResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("512Mi")},
			}, &agentv1alpha1.AdmissionSpec{
				MemoryBudgetMiB: ptr.To[int32](2048),
			}),
		},
		{
			name: "512Mi request with lowered workerRssCapMiB=512",
			agent: admissionTestAgent(&corev1.ResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("512Mi")},
			}, &agentv1alpha1.AdmissionSpec{
				WorkerRssCapMiB: ptr.To[int32](512),
			}),
		},
	}

	for _, tc := range allowed {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			warnings, err := val.ValidateCreate(ctx, tc.agent)
			if err != nil {
				t.Fatalf("unexpected ValidateCreate error: %v", err)
			}
			if len(warnings) != 0 {
				t.Errorf("unexpected ValidateCreate warnings: %v", warnings)
			}
		})
	}
}

func TestValidateKanbanAdmissionUpdateSafety(t *testing.T) {
	t.Parallel()
	val := &PlatformAgentCustomValidator{}
	ctx := context.Background()

	storedZeroSlot := admissionTestAgent(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("512Mi")},
	}, nil)

	t.Run("unrelated update on pre-existing sub-cap CR passes with warning", func(t *testing.T) {
		t.Parallel()
		updated := storedZeroSlot.DeepCopy()
		updated.Spec.Deployment.Env = []corev1.EnvVar{{Name: "FOO", Value: "bar"}}
		warnings, err := val.ValidateUpdate(ctx, storedZeroSlot, updated)
		if err != nil {
			t.Fatalf("unexpected error on unrelated update: %v", err)
		}
		if len(warnings) == 0 || !strings.Contains(strings.Join(warnings, "\n"), "spec.deployment.resources.requests.memory") {
			t.Errorf("expected warning naming spec.deployment.resources.requests.memory, got %v", warnings)
		}
	})

	t.Run("editing sub-cap CR resources to another sub-cap value is rejected", func(t *testing.T) {
		t.Parallel()
		updated := storedZeroSlot.DeepCopy()
		updated.Spec.Deployment.Resources.Requests[corev1.ResourceMemory] = resource.MustParse("1024Mi")
		_, err := val.ValidateUpdate(ctx, storedZeroSlot, updated)
		assertFieldError(t, err, "spec.deployment.resources.requests.memory")
	})

	t.Run("lowering a valid CR into sub-cap resources is rejected", func(t *testing.T) {
		t.Parallel()
		validCR := admissionTestAgent(nil, nil)
		_, err := val.ValidateUpdate(ctx, validCR, storedZeroSlot)
		assertFieldError(t, err, "spec.deployment.resources.requests.memory")
	})

	t.Run("fixing a pre-existing sub-cap CR succeeds with no warnings", func(t *testing.T) {
		t.Parallel()
		fixed := storedZeroSlot.DeepCopy()
		fixed.Spec.Deployment.Resources = nil
		warnings, err := val.ValidateUpdate(ctx, storedZeroSlot, fixed)
		if err != nil {
			t.Fatalf("unexpected error fixing sub-cap CR: %v", err)
		}
		if len(warnings) != 0 {
			t.Errorf("unexpected warnings after fixing CR: %v", warnings)
		}
	})
}
