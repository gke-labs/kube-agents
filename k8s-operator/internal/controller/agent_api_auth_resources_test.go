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
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/validation/field"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func apiAuthAgentWithResources(override *corev1.ResourceRequirements) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec:       agentv1alpha1.PlatformAgentSpec{AgentSpec: agentv1alpha1.AgentSpec{Deployment: &agentv1alpha1.DeploymentSpec{}}},
	}
	if override != nil {
		agent.Spec.Deployment.AgentAPIAuth = &agentv1alpha1.AgentAPIAuthSpec{Resources: override}
	}
	return agent
}

// TestAgentAPIAuthResourcesDefaultToTheOperatorsValues: a nil override renders
// exactly the literals the sidecar carried before the field existed, which the
// goldens and the chart's quota preflight all carry.
func TestAgentAPIAuthResourcesDefaultToTheOperatorsValues(t *testing.T) {
	for _, deployment := range []*agentv1alpha1.DeploymentSpec{nil, {}, {AgentAPIAuth: &agentv1alpha1.AgentAPIAuthSpec{}}} {
		got := resolveAgentAPIAuthResources(deployment)
		assertQuantity(t, got.Requests, corev1.ResourceCPU, "150m")
		assertQuantity(t, got.Requests, corev1.ResourceMemory, "384Mi")
		assertQuantity(t, got.Limits, corev1.ResourceCPU, "1")
		assertQuantity(t, got.Limits, corev1.ResourceMemory, "2Gi")
		assertQuantity(t, got.Limits, corev1.ResourceEphemeralStorage, "2Gi")
		if len(got.Requests) != 2 || len(got.Limits) != 3 {
			t.Errorf("default render carries %d requests and %d limits, want 2 and 3", len(got.Requests), len(got.Limits))
		}
	}
	container := buildAgentAPIAuthSidecar(apiAuthAgentWithResources(nil), "/home/hermes")
	if container.Resources.Limits.Memory().Cmp(resource.MustParse("2Gi")) != 0 {
		t.Errorf("container memory limit = %s, want the 2Gi default", container.Resources.Limits.Memory())
	}
}

// TestAgentAPIAuthMemoryLimitOverrideKeepsTheOtherDefaults is the case the field
// exists for (#2648): a CR raises limits.memory and nothing else, and the CPU
// request, the CPU limit and the ephemeral-storage limit all survive.
func TestAgentAPIAuthMemoryLimitOverrideKeepsTheOtherDefaults(t *testing.T) {
	container := buildAgentAPIAuthSidecar(apiAuthAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
	}), "/home/hermes")
	got := container.Resources
	assertQuantity(t, got.Limits, corev1.ResourceMemory, "4Gi")
	assertQuantity(t, got.Requests, corev1.ResourceCPU, "150m")
	assertQuantity(t, got.Requests, corev1.ResourceMemory, "384Mi")
	assertQuantity(t, got.Limits, corev1.ResourceCPU, "1")
	assertQuantity(t, got.Limits, corev1.ResourceEphemeralStorage, "2Gi")
}

// TestAgentAPIAuthMemoryLimitDownwardAPIFollowsTheOverride: the watcher reads
// its GOMEMLIMIT from EVENT_WATCHER_MEMORY_LIMIT_BYTES, a Downward API ref on
// the container's own limits.memory, so a raised limit raises the soft limit
// with no other change.
func TestAgentAPIAuthMemoryLimitDownwardAPIFollowsTheOverride(t *testing.T) {
	container := buildAgentAPIAuthSidecar(apiAuthAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
	}), "/home/hermes")
	var env *corev1.EnvVar
	for i := range container.Env {
		if container.Env[i].Name == "EVENT_WATCHER_MEMORY_LIMIT_BYTES" {
			env = &container.Env[i]
		}
	}
	if env == nil {
		t.Fatal("EVENT_WATCHER_MEMORY_LIMIT_BYTES is missing from the sidecar env")
	}
	if env.ValueFrom == nil || env.ValueFrom.ResourceFieldRef == nil {
		t.Fatalf("EVENT_WATCHER_MEMORY_LIMIT_BYTES is not a Downward API resource ref: %+v", env)
	}
	if env.ValueFrom.ResourceFieldRef.Resource != "limits.memory" {
		t.Errorf("EVENT_WATCHER_MEMORY_LIMIT_BYTES reads %q, want limits.memory so it follows the override",
			env.ValueFrom.ResourceFieldRef.Resource)
	}
}

// TestAgentAPIAuthFullOverrideReplacesEveryKey: a CR that states every key gets
// every key, with nothing of the operator's left underneath.
func TestAgentAPIAuthFullOverrideReplacesEveryKey(t *testing.T) {
	container := buildAgentAPIAuthSidecar(apiAuthAgentWithResources(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("500m"), corev1.ResourceMemory: resource.MustParse("1Gi")},
		Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("2"), corev1.ResourceMemory: resource.MustParse("6Gi"), corev1.ResourceEphemeralStorage: resource.MustParse("4Gi")},
	}), "/home/hermes")
	got := container.Resources
	assertQuantity(t, got.Requests, corev1.ResourceCPU, "500m")
	assertQuantity(t, got.Requests, corev1.ResourceMemory, "1Gi")
	assertQuantity(t, got.Limits, corev1.ResourceCPU, "2")
	assertQuantity(t, got.Limits, corev1.ResourceMemory, "6Gi")
	assertQuantity(t, got.Limits, corev1.ResourceEphemeralStorage, "4Gi")
}

// TestAgentAPIAuthOverrideDoesNotAliasTheCR: the resolver deep-copies, so a
// later writer of the rendered list cannot mutate the CR's quantities.
func TestAgentAPIAuthOverrideDoesNotAliasTheCR(t *testing.T) {
	override := &corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")}}
	agent := apiAuthAgentWithResources(override)
	resolved := resolveAgentAPIAuthResources(agent.Spec.Deployment)
	m := resolved.Limits[corev1.ResourceMemory]
	m.Add(resource.MustParse("1Gi"))
	resolved.Limits[corev1.ResourceMemory] = m
	crLimit := override.Limits[corev1.ResourceMemory]
	if crLimit.Cmp(resource.MustParse("4Gi")) != 0 {
		t.Errorf("the CR's limit changed to %s; the resolver aliased it", crLimit.String())
	}
}

func TestValidateAgentAPIAuthResources(t *testing.T) {
	path := field.NewPath("spec", "deployment", "agentAPIAuth", "resources")
	cases := []struct {
		name       string
		override   *corev1.ResourceRequirements
		wantErr    bool
		wantWarn   bool
	}{
		{"nil override", nil, false, false},
		// Raising limits.memory alone warns that Autopilot without bursting clamps
		// the limit to the request; it is accepted, like the proxy's same case.
		{"raise memory limit only", &corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")}}, false, true},
		// A balanced requests pair (4 GiB per vCPU, inside the Autopilot band) with
		// matching limits is the clean case: no error, no warning.
		{"raise cpu and memory in band", &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("4Gi")},
			Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("4Gi")},
		}, false, false},
		{"unknown name", &corev1.ResourceRequirements{Limits: corev1.ResourceList{"nvidia.com/gpu": resource.MustParse("1")}}, true, false},
		{"claims", &corev1.ResourceRequirements{Claims: []corev1.ResourceClaim{{Name: "c"}}}, true, false},
		{"negative", &corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("-1Gi")}}, true, false},
		{"zero limit", &corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("0")}}, true, false},
		{"request above limit", &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("5Gi")},
			Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
		}, true, false},
		{"requests leave the autopilot band", &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse("4Gi")},
			Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("4Gi")},
		}, false, true},
		{"limit without its request", &corev1.ResourceRequirements{
			Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("2")},
		}, false, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			errs, warnings := ValidateAgentAPIAuthResources(apiAuthAgentWithResources(tc.override).Spec.Deployment, path)
			if tc.wantErr != (len(errs) > 0) {
				t.Errorf("errs = %v; want error: %v", errs, tc.wantErr)
			}
			if tc.wantWarn != (len(warnings) > 0) {
				t.Errorf("warnings = %v; want warning: %v", warnings, tc.wantWarn)
			}
		})
	}
}

// TestAgentAPIAuthHasNoChildBudgetFloor: unlike the credential proxy, the
// watcher has no admission count tied to its limit, so a small but positive
// memory limit at or above the request is accepted rather than floored.
func TestAgentAPIAuthHasNoChildBudgetFloor(t *testing.T) {
	path := field.NewPath("spec", "deployment", "agentAPIAuth", "resources")
	errs, _ := ValidateAgentAPIAuthResources(apiAuthAgentWithResources(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("256Mi")},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("400Mi")},
	}).Spec.Deployment, path)
	if len(errs) != 0 {
		t.Errorf("a 400Mi limit was refused (%v); the watcher has no child-budget floor", errs)
	}
}
