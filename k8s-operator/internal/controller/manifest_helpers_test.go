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
	"reflect"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func TestResolveAgentImage(t *testing.T) {
	tests := []struct {
		name         string
		deployment   *agentv1alpha1.DeploymentSpec
		defaultImage string
		expected     string
	}{
		{
			name:         "nil deployment",
			deployment:   nil,
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
		},
		{
			name: "empty image in deployment",
			deployment: &agentv1alpha1.DeploymentSpec{
				Image: "",
			},
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
		},
		{
			name: "custom image without tag or digest",
			deployment: &agentv1alpha1.DeploymentSpec{
				Image: "my-custom-image",
			},
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "my-custom-image:latest",
		},
		{
			name: "custom image with tag in image field",
			deployment: &agentv1alpha1.DeploymentSpec{
				Image: "my-custom-image:v1.0.0",
			},
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "my-custom-image:v1.0.0",
		},
		{
			name: "custom image with digest in image field",
			deployment: &agentv1alpha1.DeploymentSpec{
				Image: "my-custom-image@sha256:568c460a8a65c92c892837fcf4b46c6a461e7127e4e04052cfdf10a56f2e2124",
			},
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "my-custom-image@sha256:568c460a8a65c92c892837fcf4b46c6a461e7127e4e04052cfdf10a56f2e2124",
		},
		{
			name: "custom image with explicit tag field",
			deployment: &agentv1alpha1.DeploymentSpec{
				Image: "my-custom-image",
				Tag:   ptr.To("v2.0.0"),
			},
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "my-custom-image:v2.0.0",
		},
		{
			name: "custom image with empty tag field fallback to latest",
			deployment: &agentv1alpha1.DeploymentSpec{
				Image: "my-custom-image",
				Tag:   ptr.To(""),
			},
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "my-custom-image:latest",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := resolveAgentImage(tt.deployment, tt.defaultImage)
			if result != tt.expected {
				t.Errorf("resolveAgentImage() = %q, expected %q", result, tt.expected)
			}
		})
	}
}

func TestMergeEnvVars(t *testing.T) {
	tests := []struct {
		name     string
		defaults []corev1.EnvVar
		custom   []corev1.EnvVar
		expected []corev1.EnvVar
	}{
		{
			name:     "empty custom returns defaults",
			defaults: []corev1.EnvVar{{Name: "A", Value: "1"}},
			custom:   nil,
			expected: []corev1.EnvVar{{Name: "A", Value: "1"}},
		},
		{
			name:     "empty defaults returns custom",
			defaults: nil,
			custom:   []corev1.EnvVar{{Name: "B", Value: "2"}},
			expected: []corev1.EnvVar{{Name: "B", Value: "2"}},
		},
		{
			name:     "no overlap, appends custom",
			defaults: []corev1.EnvVar{{Name: "A", Value: "1"}},
			custom:   []corev1.EnvVar{{Name: "B", Value: "2"}},
			expected: []corev1.EnvVar{{Name: "A", Value: "1"}, {Name: "B", Value: "2"}},
		},
		{
			name:     "overlap, custom overrides default",
			defaults: []corev1.EnvVar{{Name: "A", Value: "1"}, {Name: "B", Value: "2"}},
			custom:   []corev1.EnvVar{{Name: "B", Value: "3"}},
			expected: []corev1.EnvVar{{Name: "A", Value: "1"}, {Name: "B", Value: "3"}},
		},
		{
			name:     "duplicate custom, last one wins",
			defaults: []corev1.EnvVar{{Name: "A", Value: "1"}},
			custom:   []corev1.EnvVar{{Name: "B", Value: "2"}, {Name: "B", Value: "3"}},
			expected: []corev1.EnvVar{{Name: "A", Value: "1"}, {Name: "B", Value: "3"}},
		},
		{
			name:     "duplicate custom overrides default, last one wins",
			defaults: []corev1.EnvVar{{Name: "A", Value: "1"}, {Name: "B", Value: "2"}},
			custom:   []corev1.EnvVar{{Name: "B", Value: "3"}, {Name: "B", Value: "4"}},
			expected: []corev1.EnvVar{{Name: "A", Value: "1"}, {Name: "B", Value: "4"}},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := mergeEnvVars(tt.defaults, tt.custom)
			if !reflect.DeepEqual(result, tt.expected) {
				t.Errorf("mergeEnvVars() = %v, expected %v", result, tt.expected)
			}
		})
	}
}

func TestResolveDeploymentReplicasAndStrategy(t *testing.T) {
	tests := []struct {
		name             string
		deployment       *agentv1alpha1.DeploymentSpec
		expectedReplicas int32
		expectedStrategy appsv1.DeploymentStrategyType
	}{
		{
			name:             "nil deployment returns defaults",
			deployment:       nil,
			expectedReplicas: 1,
			expectedStrategy: appsv1.RecreateDeploymentStrategyType,
		},
		{
			name: "high availability enabled",
			deployment: &agentv1alpha1.DeploymentSpec{
				Availability: &agentv1alpha1.AvailabilitySpec{
					Replicas: ptr.To(int32(2)),
				},
			},
			expectedReplicas: 2,
			expectedStrategy: appsv1.RollingUpdateDeploymentStrategyType,
		},
		{
			name: "scale to zero enabled",
			deployment: &agentv1alpha1.DeploymentSpec{
				ScaleToZero: ptr.To(true),
			},
			expectedReplicas: 0,
			expectedStrategy: appsv1.RecreateDeploymentStrategyType,
		},
		{
			name: "high availability and scale to zero both enabled",
			deployment: &agentv1alpha1.DeploymentSpec{
				Availability: &agentv1alpha1.AvailabilitySpec{
					Replicas: ptr.To(int32(2)),
				},
				ScaleToZero: ptr.To(true),
			},
			expectedReplicas: 0,
			expectedStrategy: appsv1.RollingUpdateDeploymentStrategyType,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			replicas, strategy := resolveDeploymentReplicasAndStrategy(tt.deployment)
			if replicas != tt.expectedReplicas {
				t.Errorf("expected replicas %d, got %d", tt.expectedReplicas, replicas)
			}
			if strategy.Type != tt.expectedStrategy {
				t.Errorf("expected strategy %s, got %s", tt.expectedStrategy, strategy.Type)
			}
		})
	}
}

func TestResolveResources(t *testing.T) {
	// 1. Default resources
	defaults := resolveResources(nil)
	cpuLim := defaults.Limits[corev1.ResourceCPU]
	memLim := defaults.Limits[corev1.ResourceMemory]
	if cpuLim.Cmp(resource.MustParse("2")) != 0 || memLim.Cmp(resource.MustParse("4Gi")) != 0 {
		t.Errorf("unexpected default resources: %v", defaults)
	}

	// 2. Custom resources override
	customDep := &agentv1alpha1.DeploymentSpec{
		Resources: &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("1"),
				corev1.ResourceMemory: resource.MustParse("3Gi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("4"),
				corev1.ResourceMemory: resource.MustParse("8Gi"),
			},
		},
	}
	res := resolveResources(customDep)
	resCpuReq := res.Requests[corev1.ResourceCPU]
	resCpuLim := res.Limits[corev1.ResourceCPU]
	if resCpuReq.String() != "1" || resCpuLim.String() != "4" {
		t.Errorf("expected custom resources 1 CPU / 4 CPU limit, got %v", res)
	}
}

func TestMergeAnnotations(t *testing.T) {
	defaults := map[string]string{"a": "1", "b": "2"}
	custom := map[string]string{"b": "override", "c": "3"}
	result := mergeAnnotations(defaults, custom)
	expected := map[string]string{"a": "1", "b": "override", "c": "3"}
	if !reflect.DeepEqual(result, expected) {
		t.Errorf("expected %v, got %v", expected, result)
	}

	// Test immutability when custom is empty
	emptyCustomResult := mergeAnnotations(defaults, nil)
	if !reflect.DeepEqual(emptyCustomResult, defaults) {
		t.Errorf("expected %v, got %v", defaults, emptyCustomResult)
	}
	emptyCustomResult["a"] = "mutated"
	if defaults["a"] == "mutated" {
		t.Errorf("expected defaults map not to be mutated when result map is changed")
	}

	// Test nil when both empty
	if nilResult := mergeAnnotations(nil, nil); nilResult != nil {
		t.Errorf("expected nil when both defaults and custom are nil, got %v", nilResult)
	}
}
