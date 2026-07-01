//go:build ignore

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

func TestDevTeamAgentValidation(t *testing.T) {
	ctx := context.Background()

	t.Run("allows creation of devteam agent when none exists in the namespace", func(t *testing.T) {
		scheme := runtime.NewScheme()
		_ = agentv1alpha1.AddToScheme(scheme)
		fakeClient := fake.NewClientBuilder().WithScheme(scheme).Build()

		val := &DevTeamAgentCustomValidator{
			Client: fakeClient,
		}

		newAgent := &agentv1alpha1.DevTeamAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "new-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.DevTeamAgentSpec{},
		}

		_, err := val.ValidateCreate(ctx, newAgent)
		if err != nil {
			t.Errorf("unexpected error: %v", err)
		}
	})

	t.Run("fails if another devteam agent already exists in the same namespace", func(t *testing.T) {
		existingAgent := &agentv1alpha1.DevTeamAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "existing-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.DevTeamAgentSpec{},
		}

		scheme := runtime.NewScheme()
		_ = agentv1alpha1.AddToScheme(scheme)
		fakeClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(existingAgent).Build()

		val := &DevTeamAgentCustomValidator{
			Client: fakeClient,
		}

		newAgent := &agentv1alpha1.DevTeamAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "new-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.DevTeamAgentSpec{},
		}

		_, err := val.ValidateCreate(ctx, newAgent)
		if err == nil {
			t.Error("expected validation to fail when another DevTeamAgent already exists in the same namespace")
		}
	})

	t.Run("allows creation of devteam agent if another exists in a different namespace", func(t *testing.T) {
		existingAgent := &agentv1alpha1.DevTeamAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "existing-agent",
				Namespace: "other-namespace",
			},
			Spec: agentv1alpha1.DevTeamAgentSpec{},
		}

		scheme := runtime.NewScheme()
		_ = agentv1alpha1.AddToScheme(scheme)
		fakeClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(existingAgent).Build()

		val := &DevTeamAgentCustomValidator{
			Client: fakeClient,
		}

		newAgent := &agentv1alpha1.DevTeamAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "new-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.DevTeamAgentSpec{},
		}

		_, err := val.ValidateCreate(ctx, newAgent)
		if err != nil {
			t.Errorf("unexpected error: %v", err)
		}
	})

	t.Run("allows creation when existing devteam agent is terminating", func(t *testing.T) {
		now := metav1.Now()
		existingAgent := &agentv1alpha1.DevTeamAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:              "existing-agent",
				Namespace:         "default",
				DeletionTimestamp: &now,
				Finalizers:        []string{"kubeagents.x-k8s.io/devteamagent-webhook-lock"},
			},
			Spec: agentv1alpha1.DevTeamAgentSpec{},
		}

		scheme := runtime.NewScheme()
		_ = agentv1alpha1.AddToScheme(scheme)
		fakeClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(existingAgent).Build()

		val := &DevTeamAgentCustomValidator{
			Client: fakeClient,
		}

		newAgent := &agentv1alpha1.DevTeamAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "new-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.DevTeamAgentSpec{},
		}

		_, err := val.ValidateCreate(ctx, newAgent)
		if err != nil {
			t.Errorf("unexpected validation failure: %v", err)
		}
	})

	t.Run("allows update to the same existing devteam agent", func(t *testing.T) {
		existingAgent := &agentv1alpha1.DevTeamAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "existing-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.DevTeamAgentSpec{},
		}

		scheme := runtime.NewScheme()
		_ = agentv1alpha1.AddToScheme(scheme)
		fakeClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(existingAgent).Build()

		val := &DevTeamAgentCustomValidator{
			Client: fakeClient,
		}

		_, err := val.ValidateUpdate(ctx, existingAgent, existingAgent)
		if err != nil {
			t.Errorf("unexpected error when updating the same existing DevTeamAgent: %v", err)
		}
	})

	t.Run("allows update when the agent under validation is terminating to prevent deadlocks", func(t *testing.T) {
		now := metav1.Now()
		// Another active agent exists in the same namespace
		existingAgent := &agentv1alpha1.DevTeamAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "existing-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.DevTeamAgentSpec{},
		}

		// The agent being updated is terminating
		terminatingAgent := &agentv1alpha1.DevTeamAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:              "my-agent",
				Namespace:         "default",
				DeletionTimestamp: &now,
			},
			Spec: agentv1alpha1.DevTeamAgentSpec{},
		}

		scheme := runtime.NewScheme()
		_ = agentv1alpha1.AddToScheme(scheme)
		fakeClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(existingAgent).Build()

		val := &DevTeamAgentCustomValidator{
			Client: fakeClient,
		}

		// Update should be permitted without triggering cardinality errors
		_, err := val.ValidateUpdate(ctx, terminatingAgent, terminatingAgent)
		if err != nil {
			t.Errorf("unexpected error: terminating agent update should bypass validation: %v", err)
		}
	})

	t.Run("fails when Client is nil (fail-closed)", func(t *testing.T) {
		val := &DevTeamAgentCustomValidator{
			Client: nil,
		}

		agent := &agentv1alpha1.DevTeamAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "new-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.DevTeamAgentSpec{},
		}

		_, err := val.ValidateCreate(ctx, agent)
		if err == nil {
			t.Error("expected validation to fail when Client is nil")
		}
	})

	t.Run("fails if namespace is empty", func(t *testing.T) {
		val := &DevTeamAgentCustomValidator{}

		agent := &agentv1alpha1.DevTeamAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "new-agent",
				Namespace: "",
			},
			Spec: agentv1alpha1.DevTeamAgentSpec{},
		}

		_, err := val.ValidateCreate(ctx, agent)
		if err == nil {
			t.Error("expected validation to fail when namespace is empty")
		}
	})
}
