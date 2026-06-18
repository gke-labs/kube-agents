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
