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
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func TestPlatformAgentDefaulting(t *testing.T) {
	ctx := context.Background()
	defaulter := &PlatformAgentCustomDefaulter{}

	t.Run("defaults empty spec fields correctly", func(t *testing.T) {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{},
		}

		err := defaulter.Default(ctx, agent)
		if err != nil {
			t.Fatalf("unexpected error during defaulting: %v", err)
		}

		if agent.Spec.Harness == nil {
			t.Fatal("expected Harness to be defaulted")
		}
		if agent.Spec.Harness.Hermes == nil {
			t.Fatal("expected Hermes to be defaulted")
		}
		if agent.Spec.Harness.Hermes.DashboardEnabled == nil || !*agent.Spec.Harness.Hermes.DashboardEnabled {
			t.Error("expected DashboardEnabled to default to true")
		}
		if agent.Spec.Harness.Hermes.PluginsDebug == nil || *agent.Spec.Harness.Hermes.PluginsDebug {
			t.Error("expected PluginsDebug to default to false")
		}
		if agent.Spec.Harness.Hermes.AgentHome != "/opt/data" {
			t.Errorf("expected AgentHome to default to '/opt/data', got %q", agent.Spec.Harness.Hermes.AgentHome)
		}
	})

	t.Run("defaults deployment fields when deployment is present", func(t *testing.T) {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Image: "gcr.io/my-project/agent-image",
				},
			},
		}

		err := defaulter.Default(ctx, agent)
		if err != nil {
			t.Fatalf("unexpected error during defaulting: %v", err)
		}

		if agent.Spec.Deployment.Tag == nil || *agent.Spec.Deployment.Tag != "latest" {
			t.Errorf("expected Tag to default to 'latest'")
		}
		if agent.Spec.Deployment.ImagePullPolicy == nil || *agent.Spec.Deployment.ImagePullPolicy != corev1.PullIfNotPresent {
			t.Errorf("expected ImagePullPolicy to default to 'IfNotPresent'")
		}
	})

	t.Run("defaults google chat enabled field when google chat is present", func(t *testing.T) {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Integration: &agentv1alpha1.IntegrationSpec{
					GoogleChat: &agentv1alpha1.GoogleChatSpec{
						ProjectID: "my-project",
					},
				},
			},
		}

		err := defaulter.Default(ctx, agent)
		if err != nil {
			t.Fatalf("unexpected error during defaulting: %v", err)
		}

		if agent.Spec.Integration.GoogleChat.Enabled == nil || *agent.Spec.Integration.GoogleChat.Enabled {
			t.Error("expected GoogleChat.Enabled to default to false")
		}
	})
}

func TestPlatformAgentValidation(t *testing.T) {
	ctx := context.Background()
	validator := &PlatformAgentCustomValidator{}

	enabledChat := true

	t.Run("allows valid platform agent spec", func(t *testing.T) {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Image: "my-image",
				},
			},
		}

		_, err := validator.ValidateCreate(ctx, agent)
		if err != nil {
			t.Errorf("unexpected validation error: %v", err)
		}
	})

	t.Run("fails when deployment image is missing", func(t *testing.T) {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Image: "",
				},
			},
		}

		_, err := validator.ValidateCreate(ctx, agent)
		if err == nil {
			t.Error("expected validation to fail when image is empty")
		}
	})

	t.Run("fails when image pull policy is invalid", func(t *testing.T) {
		invalidPolicy := corev1.PullPolicy("InvalidPolicy")
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Image:           "my-image",
					ImagePullPolicy: &invalidPolicy,
				},
			},
		}

		_, err := validator.ValidateCreate(ctx, agent)
		if err == nil {
			t.Error("expected validation to fail when imagePullPolicy is invalid")
		}
	})

	t.Run("fails when GCP workload identity is configured but missing gsaName", func(t *testing.T) {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Security: &agentv1alpha1.SecuritySpec{
					WorkloadIdentity: &agentv1alpha1.WorkloadIdentitySpec{
						Gcp: &agentv1alpha1.GcpWorkloadIdentitySpec{
							GSAName:   "",
							ProjectID: "my-project",
						},
					},
				},
			},
		}

		_, err := validator.ValidateCreate(ctx, agent)
		if err == nil {
			t.Error("expected validation to fail when workloadIdentity gsaName is empty")
		}
	})

	t.Run("fails when GCP workload identity is configured but missing projectId", func(t *testing.T) {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Security: &agentv1alpha1.SecuritySpec{
					WorkloadIdentity: &agentv1alpha1.WorkloadIdentitySpec{
						Gcp: &agentv1alpha1.GcpWorkloadIdentitySpec{
							GSAName:   "my-gsa",
							ProjectID: "",
						},
					},
				},
			},
		}

		_, err := validator.ValidateCreate(ctx, agent)
		if err == nil {
			t.Error("expected validation to fail when workloadIdentity projectId is empty")
		}
	})

	t.Run("fails when Google Chat integration is enabled but missing fields", func(t *testing.T) {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Integration: &agentv1alpha1.IntegrationSpec{
					GoogleChat: &agentv1alpha1.GoogleChatSpec{
						Enabled:          &enabledChat,
						ProjectID:        "my-project",
						TopicName:        "",
						SubscriptionName: "sub",
					},
				},
			},
		}

		_, err := validator.ValidateCreate(ctx, agent)
		if err == nil {
			t.Error("expected validation to fail when Google Chat is enabled but topicName is empty")
		}
	})

	t.Run("fails when ApiServerSecretRef is configured but missing name", func(t *testing.T) {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Harness: &agentv1alpha1.PlatformAgentHarnessSpec{
					Hermes: &agentv1alpha1.HermesSpec{
						ApiServerSecretRef: &corev1.SecretKeySelector{
							LocalObjectReference: corev1.LocalObjectReference{Name: ""},
							Key:                  "my-key",
						},
					},
				},
			},
		}

		_, err := validator.ValidateCreate(ctx, agent)
		if err == nil {
			t.Error("expected validation to fail when ApiServerSecretRef name is empty")
		}
	})

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

	t.Run("allows update to the same existing platform agent", func(t *testing.T) {
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

		_, err := val.ValidateUpdate(ctx, nil, existingAgent)
		if err != nil {
			t.Errorf("unexpected error when updating the same existing PlatformAgent: %v", err)
		}
	})

	t.Run("allows creation when global GCS lock does not exist", func(t *testing.T) {
		val := &PlatformAgentCustomValidator{
			GCSClient: &fakeGCSClient{lock: nil},
		}

		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent"},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Integration: &agentv1alpha1.IntegrationSpec{
					GoogleChat: &agentv1alpha1.GoogleChatSpec{ProjectID: "my-project"},
				},
				Harness: &agentv1alpha1.PlatformAgentHarnessSpec{ClusterName: "cluster-a"},
			},
		}

		_, err := val.ValidateCreate(ctx, agent)
		if err != nil {
			t.Errorf("unexpected validation failure: %v", err)
		}
	})

	t.Run("fails when global GCS lock is held by a different GKE cluster", func(t *testing.T) {
		val := &PlatformAgentCustomValidator{
			GCSClient: &fakeGCSClient{
				lock: &PlatformAgentLock{ClusterName: "different-cluster"},
			},
		}

		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent"},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Integration: &agentv1alpha1.IntegrationSpec{
					GoogleChat: &agentv1alpha1.GoogleChatSpec{ProjectID: "my-project"},
				},
				Harness: &agentv1alpha1.PlatformAgentHarnessSpec{ClusterName: "my-cluster"},
			},
		}

		_, err := val.ValidateCreate(ctx, agent)
		if err == nil {
			t.Error("expected validation to fail since lock is held by another cluster")
		}
	})

	t.Run("allows creation when global GCS lock belongs to the same cluster name", func(t *testing.T) {
		val := &PlatformAgentCustomValidator{
			GCSClient: &fakeGCSClient{
				lock: &PlatformAgentLock{ClusterName: "my-cluster"},
			},
		}

		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent"},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Integration: &agentv1alpha1.IntegrationSpec{
					GoogleChat: &agentv1alpha1.GoogleChatSpec{ProjectID: "my-project"},
				},
				Harness: &agentv1alpha1.PlatformAgentHarnessSpec{ClusterName: "my-cluster"},
			},
		}

		_, err := val.ValidateCreate(ctx, agent)
		if err != nil {
			t.Errorf("unexpected validation failure: %v", err)
		}
	})
}

type fakeGCSClient struct {
	lock *PlatformAgentLock
	err  error
}

func (c *fakeGCSClient) GetLock(ctx context.Context, projectID string) (*PlatformAgentLock, error) {
	return c.lock, c.err
}
