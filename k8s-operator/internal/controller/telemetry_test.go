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
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func envMapOf(envs []corev1.EnvVar) map[string]corev1.EnvVar {
	m := make(map[string]corev1.EnvVar, len(envs))
	for _, e := range envs {
		m[e.Name] = e
	}
	return m
}

func TestOTelTelemetryEnvVars(t *testing.T) {
	envs := otelTelemetryEnvVars("platform", "my-agent", "my-ns")
	m := envMapOf(envs)

	if m["OTEL_SERVICE_NAME"].Value != "my-agent-gateway" {
		t.Errorf("expected OTEL_SERVICE_NAME my-agent-gateway, got %s", m["OTEL_SERVICE_NAME"].Value)
	}
	if m["OTEL_EXPORTER_OTLP_ENDPOINT"].Value != managedOTelEndpoint {
		t.Errorf("expected OTLP endpoint %s, got %s", managedOTelEndpoint, m["OTEL_EXPORTER_OTLP_ENDPOINT"].Value)
	}
	if m["OTEL_EXPORTER_OTLP_PROTOCOL"].Value != "http/protobuf" {
		t.Errorf("expected protocol http/protobuf, got %s", m["OTEL_EXPORTER_OTLP_PROTOCOL"].Value)
	}
	want := "service.namespace=my-ns,k8s.namespace.name=my-ns,kubeagents.agent_type=platform,kubeagents.agent_name=my-agent"
	if m["OTEL_RESOURCE_ATTRIBUTES"].Value != want {
		t.Errorf("expected resource attributes %q, got %q", want, m["OTEL_RESOURCE_ATTRIBUTES"].Value)
	}
}

// TestBuildDeploymentHasOTelEnv verifies the agent container is wired to the managed
// collector and still carries its service name, without duplicate env entries.
func TestBuildDeploymentHasOTelEnv(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "my-ns"},
	}

	dep := buildDeployment(agent, "h1", "h2", "h3")
	container := dep.Spec.Template.Spec.Containers[0]

	seen := make(map[string]bool)
	for _, e := range container.Env {
		if seen[e.Name] {
			t.Errorf("duplicate env var: %s", e.Name)
		}
		seen[e.Name] = true
	}
	m := envMapOf(container.Env)

	if m["OTEL_EXPORTER_OTLP_ENDPOINT"].Value != managedOTelEndpoint {
		t.Errorf("expected agent wired to managed collector, got %q", m["OTEL_EXPORTER_OTLP_ENDPOINT"].Value)
	}
	if m["OTEL_SERVICE_NAME"].Value != "my-agent-gateway" {
		t.Errorf("expected OTEL_SERVICE_NAME my-agent-gateway, got %q", m["OTEL_SERVICE_NAME"].Value)
	}
	if m["OTEL_RESOURCE_ATTRIBUTES"].Value == "" {
		t.Errorf("expected OTEL_RESOURCE_ATTRIBUTES to be set")
	}
}

func TestBuildDeploymentAllowsOTelEnvOverrides(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "my-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Env: []corev1.EnvVar{
						{Name: "OTEL_EXPORTER_OTLP_ENDPOINT", Value: "http://custom-collector:4318"},
						{Name: "OTEL_RESOURCE_ATTRIBUTES", Value: "deployment.environment=testing"},
					},
				},
			},
		},
	}

	dep := buildDeployment(agent, "h1", "h2", "h3")
	m := envMapOf(dep.Spec.Template.Spec.Containers[0].Env)

	if got := m["OTEL_EXPORTER_OTLP_ENDPOINT"].Value; got != "http://custom-collector:4318" {
		t.Errorf("expected custom OTLP endpoint, got %q", got)
	}
	if got := m["OTEL_RESOURCE_ATTRIBUTES"].Value; got != "deployment.environment=testing" {
		t.Errorf("expected custom resource attributes, got %q", got)
	}
}
