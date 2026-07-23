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

package webhook

import (
	"context"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func TestPlatformAgentValidation(t *testing.T) {
	ctx := context.Background()

	t.Run("fails if another platform agent already exists in the project", func(t *testing.T) {
		existingAgent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "existing-agent",
				Namespace: "kubeagents-system",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{},
		}

		scheme := runtime.NewScheme()
		_ = agentv1alpha1.AddToScheme(scheme)
		fakeClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(existingAgent).Build()

		val := &PlatformAgentCustomValidator{
			Client: fakeClient,
		}

		newAgent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "new-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{},
		}

		_, err := val.ValidateCreate(ctx, newAgent)
		if err == nil {
			t.Error("expected validation to fail when another PlatformAgent already exists in the cluster")
		}
	})

	t.Run("allows creation when existing platform agent is terminating", func(t *testing.T) {
		now := metav1.Now()
		existingAgent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:              "existing-agent",
				Namespace:         "kubeagents-system",
				DeletionTimestamp: &now,
				Finalizers:        []string{"kubeagents.x-k8s.io/platformagent-webhook-lock"},
			},
			Spec: agentv1alpha1.PlatformAgentSpec{},
		}

		scheme := runtime.NewScheme()
		_ = agentv1alpha1.AddToScheme(scheme)
		fakeClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(existingAgent).Build()

		val := &PlatformAgentCustomValidator{
			Client: fakeClient,
		}

		newAgent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "new-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{},
		}

		_, err := val.ValidateCreate(ctx, newAgent)
		if err != nil {
			t.Errorf("unexpected validation failure: %v", err)
		}
	})

	t.Run("fails if sensitive environment variables are overridden", func(t *testing.T) {
		val := &PlatformAgentCustomValidator{}

		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				AgentSpec: agentv1alpha1.AgentSpec{
					Deployment: &agentv1alpha1.DeploymentSpec{
						Env: []corev1.EnvVar{
							{Name: "API_SERVER_KEY", Value: "malicious-key"},
						},
					},
				},
			},
		}

		_, err := val.ValidateCreate(ctx, agent)
		if err == nil {
			t.Error("expected validation to fail when overriding API_SERVER_KEY")
		}
	})

	t.Run("fails if privileged containers are specified", func(t *testing.T) {
		val := &PlatformAgentCustomValidator{}

		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				AgentSpec: agentv1alpha1.AgentSpec{
					Deployment: &agentv1alpha1.DeploymentSpec{
						Sidecars: []corev1.Container{
							{
								Name: "malicious-sidecar",
								SecurityContext: &corev1.SecurityContext{
									Privileged: ptr.To(true),
								},
							},
						},
					},
				},
			},
		}

		_, err := val.ValidateCreate(ctx, agent)
		if err == nil {
			t.Error("expected validation to fail for privileged sidecar")
		}
	})

	t.Run("fails if hostPath volumes are specified", func(t *testing.T) {
		val := &PlatformAgentCustomValidator{}

		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				AgentSpec: agentv1alpha1.AgentSpec{
					Deployment: &agentv1alpha1.DeploymentSpec{
						ExtraVolumes: []corev1.Volume{
							{
								Name: "host-root",
								VolumeSource: corev1.VolumeSource{
									HostPath: &corev1.HostPathVolumeSource{
										Path: "/",
									},
								},
							},
						},
					},
				},
			},
		}

		_, err := val.ValidateCreate(ctx, agent)
		if err == nil {
			t.Error("expected validation to fail for hostPath volume")
		}
	})

	t.Run("fails if privileged service account is specified", func(t *testing.T) {
		val := &PlatformAgentCustomValidator{}

		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				AgentSpec: agentv1alpha1.AgentSpec{
					Security: &agentv1alpha1.SecuritySpec{
						ServiceAccountName: "cluster-admin",
					},
				},
			},
		}

		_, err := val.ValidateCreate(ctx, agent)
		if err == nil {
			t.Error("expected validation to fail for cluster-admin service account")
		}
	})
}

func TestPlatformAgentDefaulter(t *testing.T) {
	ctx := context.Background()
	defaulter := &PlatformAgentCustomDefaulter{}

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name: "test-agent",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				Memory: &agentv1alpha1.MemorySpec{},
			},
		},
	}

	err := defaulter.Default(ctx, agent)
	if err != nil {
		t.Fatalf("unexpected defaulting error: %v", err)
	}

	if agent.Spec.Deployment == nil {
		t.Fatal("expected DeploymentSpec to be initialized")
	}
	if agent.Spec.Deployment.Tag == nil || *agent.Spec.Deployment.Tag != "latest" {
		t.Errorf("expected Tag 'latest', got %v", agent.Spec.Deployment.Tag)
	}
	if agent.Spec.Deployment.ImagePullPolicy == nil || *agent.Spec.Deployment.ImagePullPolicy != corev1.PullIfNotPresent {
		t.Errorf("expected ImagePullPolicy IfNotPresent, got %v", agent.Spec.Deployment.ImagePullPolicy)
	}
	if agent.Spec.Harness.Memory.UserProfileEnabled == nil || *agent.Spec.Harness.Memory.UserProfileEnabled != false {
		t.Errorf("expected UserProfileEnabled false, got %v", agent.Spec.Harness.Memory.UserProfileEnabled)
	}
}

func TestPlatformAgentValidateDelete(t *testing.T) {
	ctx := context.Background()
	val := &PlatformAgentCustomValidator{}

	t.Run("blocks deletion if prevent-deletion annotation is true", func(t *testing.T) {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name: "protected-agent",
				Annotations: map[string]string{
					PreventDeletionAnnotation: "true",
				},
			},
		}

		_, err := val.ValidateDelete(ctx, agent)
		if err == nil {
			t.Error("expected ValidateDelete to fail when prevent-deletion annotation is present")
		}
	})

	t.Run("allows deletion when no protection annotation is present", func(t *testing.T) {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name: "unprotected-agent",
			},
		}

		_, err := val.ValidateDelete(ctx, agent)
		if err != nil {
			t.Errorf("unexpected error on deletion: %v", err)
		}
	})
}
