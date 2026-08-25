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
	networkingv1 "k8s.io/api/networking/v1"
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
	envs := otelTelemetryEnvVars("platform", "my-agent", "my-ns", "", false)
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

// TestOTelTelemetryEnvVarsCustomEndpoint pins the endpoint verbatim: no path is
// appended and no scheme is rewritten, because the exporter owns the per-signal path.
func TestOTelTelemetryEnvVarsCustomEndpoint(t *testing.T) {
	const endpoint = "http://otel-collector.otel-collector.svc.cluster.local:4318"
	m := envMapOf(otelTelemetryEnvVars("cluster", "my-agent", "my-ns", endpoint, false))

	if got := m["OTEL_EXPORTER_OTLP_ENDPOINT"].Value; got != endpoint {
		t.Errorf("expected endpoint %q, got %q", endpoint, got)
	}
}

// TestOTelTelemetryEnvVarsDisabled covers the otlpSourceNone rendering. Both halves
// matter: no endpoint, because there is no collector to name, and OTEL_SDK_DISABLED,
// because an absent OTEL_EXPORTER_OTLP_ENDPOINT makes the SDK fall back to
// http://localhost:4318 and keep exporting at the same interval into a refused connection.
func TestOTelTelemetryEnvVarsDisabled(t *testing.T) {
	m := envMapOf(otelTelemetryEnvVars("platform", "my-agent", "my-ns", "", true))

	if _, ok := m["OTEL_EXPORTER_OTLP_ENDPOINT"]; ok {
		t.Errorf("expected no OTEL_EXPORTER_OTLP_ENDPOINT, got %q", m["OTEL_EXPORTER_OTLP_ENDPOINT"].Value)
	}
	if _, ok := m["OTEL_EXPORTER_OTLP_PROTOCOL"]; ok {
		t.Error("expected no OTEL_EXPORTER_OTLP_PROTOCOL when nothing is exported")
	}
	if got := m["OTEL_SDK_DISABLED"].Value; got != "true" {
		t.Errorf("expected OTEL_SDK_DISABLED=true, got %q", got)
	}
	// Identity survives: docker-entrypoint passes OTEL_SERVICE_NAME to otel_config.py.
	if got := m["OTEL_SERVICE_NAME"].Value; got != "my-agent-gateway" {
		t.Errorf("expected OTEL_SERVICE_NAME my-agent-gateway, got %q", got)
	}
	if m["OTEL_RESOURCE_ATTRIBUTES"].Value == "" {
		t.Error("expected OTEL_RESOURCE_ATTRIBUTES to be set")
	}
}

// TestBuildDeploymentDisabledTelemetryIsOverridable keeps the escape hatch open: an
// operator who wants an exporter on a cluster the probe found nothing in can still say so,
// because mergeEnvVars applies spec.deployment.env after the operator's own values.
func TestBuildDeploymentDisabledTelemetryIsOverridable(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "my-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Env: []corev1.EnvVar{
						{Name: "OTEL_SDK_DISABLED", Value: "false"},
						{Name: "OTEL_EXPORTER_OTLP_ENDPOINT", Value: "http://insisted-on:4318"},
					},
				},
			},
		},
	}

	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil,
		renderOptions{imageVolumeSupported: true, otlpDisabled: true})
	m := envMapOf(dep.Spec.Template.Spec.Containers[0].Env)

	if got := m["OTEL_SDK_DISABLED"].Value; got != "false" {
		t.Errorf("expected the operator's OTEL_SDK_DISABLED to be overridable, got %q", got)
	}
	if got := m["OTEL_EXPORTER_OTLP_ENDPOINT"].Value; got != "http://insisted-on:4318" {
		t.Errorf("expected the pinned endpoint, got %q", got)
	}
}

// TestBuildDeploymentDisabledTelemetry is the manifest-level statement of #831 item 5: the
// pod that used to be handed a collector that does not exist is now handed nothing.
func TestBuildDeploymentDisabledTelemetry(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "my-ns"},
	}

	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil,
		renderOptions{imageVolumeSupported: true, otlpDisabled: true})
	m := envMapOf(dep.Spec.Template.Spec.Containers[0].Env)

	if _, ok := m["OTEL_EXPORTER_OTLP_ENDPOINT"]; ok {
		t.Errorf("expected no endpoint on a cluster with no collector, got %q", m["OTEL_EXPORTER_OTLP_ENDPOINT"].Value)
	}
	if got := m["OTEL_SDK_DISABLED"].Value; got != "true" {
		t.Errorf("expected OTEL_SDK_DISABLED=true, got %q", got)
	}
}

// TestBuildNetworkPolicyOmitsCollectorEgressWhenDisabled: with nothing exporting, the
// egress rule would open a path to a namespace that does not exist on this cluster.
func TestBuildNetworkPolicyOmitsCollectorEgressWhenDisabled(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "my-ns"},
	}

	enabled := buildNetworkPolicy(agent, nil, defaultTestNetpolProfile(), false, managedOTelEndpoint, false)
	if !hasCollectorEgress(enabled, "gke-managed-otel") {
		t.Fatal("expected the collector egress rule when telemetry is on")
	}

	disabled := buildNetworkPolicy(agent, nil, defaultTestNetpolProfile(), false, "", true)
	if hasCollectorEgress(disabled, "gke-managed-otel") {
		t.Error("expected no collector egress rule when telemetry is disabled")
	}
}

// hasCollectorEgress reports whether np allows egress to ns on an OTLP receiver port.
func hasCollectorEgress(np *networkingv1.NetworkPolicy, ns string) bool {
	for _, rule := range np.Spec.Egress {
		for _, peer := range rule.To {
			if peer.NamespaceSelector == nil {
				continue
			}
			if peer.NamespaceSelector.MatchLabels["kubernetes.io/metadata.name"] == ns {
				return true
			}
		}
	}
	return false
}

// TestBuildDeploymentHasOTelEnv verifies the agent container is wired to the managed
// collector and still carries its service name, without duplicate env entries.
func TestBuildDeploymentHasOTelEnv(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "my-ns"},
	}

	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
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

	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
	m := envMapOf(dep.Spec.Template.Spec.Containers[0].Env)

	if got := m["OTEL_EXPORTER_OTLP_ENDPOINT"].Value; got != "http://custom-collector:4318" {
		t.Errorf("expected custom OTLP endpoint, got %q", got)
	}
	if got := m["OTEL_RESOURCE_ATTRIBUTES"].Value; got != "deployment.environment=testing" {
		t.Errorf("expected custom resource attributes, got %q", got)
	}
}

func TestBuildDeploymentUsesResolvedOTLPEndpoint(t *testing.T) {
	const resolved = "http://otel-collector.otel-collector.svc.cluster.local:4318"
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "my-ns"},
	}

	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil,
		renderOptions{imageVolumeSupported: true, otlpEndpoint: resolved})
	m := envMapOf(dep.Spec.Template.Spec.Containers[0].Env)

	if got := m["OTEL_EXPORTER_OTLP_ENDPOINT"].Value; got != resolved {
		t.Errorf("expected resolved endpoint %q, got %q", resolved, got)
	}
}

// TestBuildDeploymentEnvOverrideBeatsResolvedEndpoint pins the top of the resolution
// ladder: spec.deployment.env is a pre-existing escape hatch and must keep winning over
// anything the operator resolves, or upgrading silently redirects existing installs.
func TestBuildDeploymentEnvOverrideBeatsResolvedEndpoint(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "my-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Env: []corev1.EnvVar{
						{Name: "OTEL_EXPORTER_OTLP_ENDPOINT", Value: "http://pinned-by-env:4318"},
					},
				},
			},
		},
	}

	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil,
		renderOptions{imageVolumeSupported: true, otlpEndpoint: "http://discovered:4318"})
	m := envMapOf(dep.Spec.Template.Spec.Containers[0].Env)

	if got := m["OTEL_EXPORTER_OTLP_ENDPOINT"].Value; got != "http://pinned-by-env:4318" {
		t.Errorf("expected deployment env to win, got %q", got)
	}
}
