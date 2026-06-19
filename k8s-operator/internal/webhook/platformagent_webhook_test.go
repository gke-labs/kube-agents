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

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
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
