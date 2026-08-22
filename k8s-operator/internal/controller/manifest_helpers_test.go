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
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/validation"
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
	// 1. Default resources — sized for kanban fan-out (see resolveResources).
	defaults := resolveResources(nil)
	cpuReq := defaults.Requests[corev1.ResourceCPU]
	memReq := defaults.Requests[corev1.ResourceMemory]
	if cpuReq.Cmp(resource.MustParse("1")) != 0 || memReq.Cmp(resource.MustParse("2Gi")) != 0 {
		t.Errorf("unexpected default requests: %v", defaults.Requests)
	}
	cpuLim := defaults.Limits[corev1.ResourceCPU]
	memLim := defaults.Limits[corev1.ResourceMemory]
	if cpuLim.Cmp(resource.MustParse("3")) != 0 || memLim.Cmp(resource.MustParse("8Gi")) != 0 {
		t.Errorf("unexpected default limits: %v", defaults.Limits)
	}

	// A CPU limit above what a node can actually hand out is not headroom, it
	// is a number that reads like headroom. The reference gVisor pool
	// (e2-standard-4) advertises 3920m allocatable, so anything at or above 4
	// cores is unreachable in the deployment this default is tuned for.
	if cpuLim.MilliValue() > 3920 {
		t.Errorf("default CPU limit %s exceeds the 3920m allocatable on the reference node pool", cpuLim.String())
	}

	// 2. Custom resources override. Deliberately not the defaults, so this
	// still fails if the override path is dropped and defaults are returned.
	customDep := &agentv1alpha1.DeploymentSpec{
		Resources: &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("250m"),
				corev1.ResourceMemory: resource.MustParse("3Gi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("1500m"),
				corev1.ResourceMemory: resource.MustParse("6Gi"),
			},
		},
	}
	res := resolveResources(customDep)
	resCpuReq := res.Requests[corev1.ResourceCPU]
	resCpuLim := res.Limits[corev1.ResourceCPU]
	if resCpuReq.String() != "250m" || resCpuLim.String() != "1500m" {
		t.Errorf("expected custom resources 250m request / 1500m limit, got %v", res)
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

func TestInstanceLabel(t *testing.T) {
	longName := strings.Repeat("a", 80)

	tests := []struct {
		name      string
		namespace string
		agentName string
		expected  string
	}{
		{
			name:      "short values are joined verbatim",
			namespace: "kubeagents-system",
			agentName: "platform-agent",
			expected:  "kubeagents-system-platform-agent",
		},
		{
			name:      "same name in a different namespace stays distinct",
			namespace: "team-b",
			agentName: "platform-agent",
			expected:  "team-b-platform-agent",
		},
		{
			name:      "over-long value is truncated to the label limit",
			namespace: "ns",
			agentName: longName,
			expected:  "ns-" + strings.Repeat("a", maxLabelValueLength-3),
		},
		{
			name:      "truncation does not leave a trailing separator",
			namespace: strings.Repeat("n", 62),
			agentName: "agent",
			expected:  strings.Repeat("n", 62),
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := instanceLabel(tt.namespace, tt.agentName)
			if got != tt.expected {
				t.Errorf("expected %q, got %q", tt.expected, got)
			}
			if len(got) > maxLabelValueLength {
				t.Errorf("value %q exceeds the %d character label limit", got, maxLabelValueLength)
			}
			if errs := validation.IsValidLabelValue(got); len(errs) > 0 {
				t.Errorf("value %q is not a valid label value: %v", got, errs)
			}
		})
	}
}

func TestWithCommonLabels(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "kubeagents-system"},
	}

	t.Run("sets the recommended labels on an unlabelled object", func(t *testing.T) {
		cm := &corev1.ConfigMap{}
		withCommonLabels(cm, agent)

		expected := map[string]string{
			labelName:      "platform-agent",
			labelInstance:  "kubeagents-system-my-agent",
			labelPartOf:    "kube-agents",
			labelManagedBy: fieldOwner,
		}
		if !reflect.DeepEqual(cm.Labels, expected) {
			t.Errorf("expected %v, got %v", expected, cm.Labels)
		}
	})

	t.Run("preserves selector-bearing labels the builders already set", func(t *testing.T) {
		dep := &appsv1.Deployment{
			ObjectMeta: metav1.ObjectMeta{
				Labels: map[string]string{
					"app":     "my-agent-gateway",
					labelName: "do-not-clobber",
				},
			},
		}
		withCommonLabels(dep, agent)

		if dep.Labels["app"] != "my-agent-gateway" {
			t.Errorf("expected the app label to survive, got %q", dep.Labels["app"])
		}
		if dep.Labels[labelName] != "do-not-clobber" {
			t.Errorf("expected an existing recommended label to be left alone, got %q", dep.Labels[labelName])
		}
		if dep.Labels[labelPartOf] != "kube-agents" {
			t.Errorf("expected the missing labels to be filled in, got %q", dep.Labels[labelPartOf])
		}
	})
}

// TestVersionedDefaultImage pins the wiring that lets a release build change the
// default agent image tag via -ldflags "-X ...DefaultPlatformAgentVersion=X.Y.Z":
// overriding the variable must flow through defaultPlatformAgentImage() and the
// credential-proxy sidecar derivation. Overriding (rather than asserting against
// the current value) is what makes the test non-tautological — a hardcoded
// ":latest" fallback would fail it.
func TestVersionedDefaultImage(t *testing.T) {
	t.Setenv(platformAgentImageEnvVar, "")
	t.Setenv(credentialProxyImageEnvVar, "")

	orig := DefaultPlatformAgentVersion
	DefaultPlatformAgentVersion = "9.9.9-test"
	t.Cleanup(func() { DefaultPlatformAgentVersion = orig })

	want := "ghcr.io/gke-labs/kube-agents/platform-agent:9.9.9-test"
	if got := defaultPlatformAgentImage(); got != want {
		t.Errorf("defaultPlatformAgentImage() = %q, want %q — the injected version must flow through at call time", got, want)
	}

	// The credential-proxy sidecar must carry the same tag as the defaulted
	// agent image, so a versioned release rolls both together.
	wantSidecar := "ghcr.io/gke-labs/kube-agents/credential-proxy:9.9.9-test"
	if got := resolveCredentialProxyImage(nil); got != wantSidecar {
		t.Errorf("resolveCredentialProxyImage(nil) = %q, want %q", got, wantSidecar)
	}
}

// TestDefaultImagePullSecrets covers the parsing of IMAGE_PULL_SECRETS. The
// value is hand-written into a Deployment or joined by a Helm template, so the
// malformed cases here are the realistic ones rather than pathological — and
// each has a consequence a layer away from the typo: an empty name makes the
// kubelet pull anonymously, and a repeat fails every apply of the Deployment,
// PodSpec.imagePullSecrets being a server-side-apply list-map keyed on name.
func TestDefaultImagePullSecrets(t *testing.T) {
	tests := []struct {
		name string
		env  string
		want []corev1.LocalObjectReference
	}{
		{name: "unset", env: "", want: nil},
		{name: "single", env: "regcred", want: []corev1.LocalObjectReference{{Name: "regcred"}}},
		{
			name: "several with padding",
			env:  " regcred , harbor-pull ",
			want: []corev1.LocalObjectReference{{Name: "regcred"}, {Name: "harbor-pull"}},
		},
		{
			name: "duplicates collapse",
			env:  "regcred,regcred,harbor-pull",
			want: []corev1.LocalObjectReference{{Name: "regcred"}, {Name: "harbor-pull"}},
		},
		{
			name: "empty entries dropped",
			env:  "regcred,,harbor-pull,",
			want: []corev1.LocalObjectReference{{Name: "regcred"}, {Name: "harbor-pull"}},
		},
		{name: "nothing but separators", env: ", ,", want: nil},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv(imagePullSecretsEnvVar, tt.env)
			if got := defaultImagePullSecrets(); !reflect.DeepEqual(got, tt.want) {
				t.Errorf("defaultImagePullSecrets() = %v, want %v", got, tt.want)
			}
		})
	}
}

// TestResolveImagePullSecrets pins the precedence: a CR that names its own pull
// identity REPLACES the operator-wide default rather than merging with it, the
// same way spec.deployment.image beats PLATFORM_AGENT_IMAGE. Merging would hand
// the kubelet credentials the CR never asked for.
func TestResolveImagePullSecrets(t *testing.T) {
	t.Run("falls back to the operator default", func(t *testing.T) {
		t.Setenv(imagePullSecretsEnvVar, "fleet-pull")

		want := []corev1.LocalObjectReference{{Name: "fleet-pull"}}
		for _, dep := range []*agentv1alpha1.DeploymentSpec{nil, {}, {ImagePullSecrets: nil}} {
			if got := resolveImagePullSecrets(dep); !reflect.DeepEqual(got, want) {
				t.Errorf("resolveImagePullSecrets(%v) = %v, want %v", dep, got, want)
			}
		}
	})

	t.Run("the CR replaces the default outright", func(t *testing.T) {
		t.Setenv(imagePullSecretsEnvVar, "fleet-pull")

		dep := &agentv1alpha1.DeploymentSpec{
			ImagePullSecrets: []corev1.LocalObjectReference{{Name: "team-pull"}},
		}
		want := []corev1.LocalObjectReference{{Name: "team-pull"}}
		if got := resolveImagePullSecrets(dep); !reflect.DeepEqual(got, want) {
			t.Errorf("resolveImagePullSecrets() = %v, want %v — the fleet default must not be merged in", got, want)
		}
	})

	t.Run("nil when neither is set", func(t *testing.T) {
		t.Setenv(imagePullSecretsEnvVar, "")

		if got := resolveImagePullSecrets(&agentv1alpha1.DeploymentSpec{}); got != nil {
			t.Errorf("resolveImagePullSecrets() = %v, want nil — a default install must render no imagePullSecrets at all", got)
		}
	})

	// The CR path gets the same normalization as the env var, because the
	// webhook that would have rejected these is off by default in the chart.
	// A repeat is the one that matters: PodSpec.imagePullSecrets is an SSA
	// list-map keyed on name, so a duplicate passed through here fails every
	// apply of the Deployment with "duplicate entries for key".
	t.Run("the CR list is normalized", func(t *testing.T) {
		t.Setenv(imagePullSecretsEnvVar, "")

		dep := &agentv1alpha1.DeploymentSpec{
			ImagePullSecrets: []corev1.LocalObjectReference{
				{Name: " regcred "},
				{Name: "regcred"},
				{Name: ""},
				{Name: "harbor-pull"},
			},
		}
		want := []corev1.LocalObjectReference{{Name: "regcred"}, {Name: "harbor-pull"}}
		if got := resolveImagePullSecrets(dep); !reflect.DeepEqual(got, want) {
			t.Errorf("resolveImagePullSecrets() = %v, want %v", got, want)
		}
	})

	t.Run("a CR list that normalizes away counts as unset", func(t *testing.T) {
		t.Setenv(imagePullSecretsEnvVar, "fleet-pull")

		dep := &agentv1alpha1.DeploymentSpec{
			ImagePullSecrets: []corev1.LocalObjectReference{{Name: ""}, {Name: "  "}},
		}
		want := []corev1.LocalObjectReference{{Name: "fleet-pull"}}
		if got := resolveImagePullSecrets(dep); !reflect.DeepEqual(got, want) {
			t.Errorf("resolveImagePullSecrets() = %v, want %v — an all-blank list is a typo, not a pull identity", got, want)
		}
	})
}

func TestDeriveAgentImageFromOperator(t *testing.T) {
	tests := []struct {
		name          string
		operatorImage string
		expected      string
	}{
		{
			name:          "standard ghcr SemVer release",
			operatorImage: "ghcr.io/gke-labs/kube-agents/k8s-operator:0.2.0",
			expected:      "ghcr.io/gke-labs/kube-agents/platform-agent:0.2.0",
		},
		{
			name:          "standard ghcr RC release candidate tag",
			operatorImage: "ghcr.io/gke-labs/kube-agents/k8s-operator:rc_2608201147_1c06e1a",
			expected:      "ghcr.io/gke-labs/kube-agents/platform-agent:rc_2608201147_1c06e1a",
		},
		{
			name:          "standard ghcr full commit SHA",
			operatorImage: "ghcr.io/gke-labs/kube-agents/k8s-operator:1c06e1ab71fdeea55e6100e61c0394206188a5ba",
			expected:      "ghcr.io/gke-labs/kube-agents/platform-agent:1c06e1ab71fdeea55e6100e61c0394206188a5ba",
		},
		{
			name:          "standard ghcr short commit SHA",
			operatorImage: "ghcr.io/gke-labs/kube-agents/k8s-operator:1c06e1a",
			expected:      "ghcr.io/gke-labs/kube-agents/platform-agent:1c06e1a",
		},
		{
			name:          "custom mirror registry with pure SemVer",
			operatorImage: "mirror.corp.internal/kube-agents/k8s-operator:0.2.0",
			expected:      "mirror.corp.internal/kube-agents/platform-agent:0.2.0",
		},
		{
			name:          "digest pinned image falls back to latest",
			operatorImage: "ghcr.io/gke-labs/kube-agents/k8s-operator@sha256:1111111111111111111111111111111111111111111111111111111111111111",
			expected:      "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
		},
		{
			name:          "tag and digest pinned image preserves tag",
			operatorImage: "ghcr.io/gke-labs/kube-agents/k8s-operator:0.2.0@sha256:1111111111111111111111111111111111111111111111111111111111111111",
			expected:      "ghcr.io/gke-labs/kube-agents/platform-agent:0.2.0",
		},
		{
			name:          "latest tag",
			operatorImage: "ghcr.io/gke-labs/kube-agents/k8s-operator:latest",
			expected:      "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
		},
		{
			name:          "registry host with port and SemVer tag",
			operatorImage: "registry.example.com:5000/kube-agents/k8s-operator:0.2.0",
			expected:      "registry.example.com:5000/kube-agents/platform-agent:0.2.0",
		},
		{
			name:          "localhost with port and latest tag",
			operatorImage: "localhost:5000/k8s-operator:latest",
			expected:      "localhost:5000/platform-agent:latest",
		},
		{
			name:          "registry host with port and digest falls back to latest",
			operatorImage: "registry.example.com:5000/kube-agents/k8s-operator@sha256:1111111111111111111111111111111111111111111111111111111111111111",
			expected:      "registry.example.com:5000/kube-agents/platform-agent:latest",
		},
		{
			name:          "registry host with port, tag and digest preserves tag",
			operatorImage: "registry.example.com:5000/kube-agents/k8s-operator:0.2.0@sha256:1111111111111111111111111111111111111111111111111111111111111111",
			expected:      "registry.example.com:5000/kube-agents/platform-agent:0.2.0",
		},
		{
			name:          "registry host with port and no tag",
			operatorImage: "registry.example.com:5000/kube-agents/k8s-operator",
			expected:      "registry.example.com:5000/kube-agents/platform-agent:latest",
		},
		{
			name:          "unqualified image with tag",
			operatorImage: "k8s-operator:0.2.0",
			expected:      "platform-agent:0.2.0",
		},
		{
			name:          "unqualified image without tag",
			operatorImage: "k8s-operator",
			expected:      "platform-agent:latest",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := deriveAgentImageFromOperator(tt.operatorImage)
			if got != tt.expected {
				t.Errorf("deriveAgentImageFromOperator(%q) = %q, want %q", tt.operatorImage, got, tt.expected)
			}
		})
	}
}

func TestDefaultPlatformAgentImagePrecedence(t *testing.T) {
	t.Run("explicit PLATFORM_AGENT_IMAGE overrides OPERATOR_IMAGE and fallback", func(t *testing.T) {
		t.Setenv(platformAgentImageEnvVar, "custom-registry/agent:explicit")
		t.Setenv(operatorImageEnvVar, "ghcr.io/gke-labs/kube-agents/k8s-operator:0.2.0")

		want := "custom-registry/agent:explicit"
		if got := defaultPlatformAgentImage(); got != want {
			t.Errorf("defaultPlatformAgentImage() = %q, want %q", got, want)
		}
	})

	t.Run("OPERATOR_IMAGE derives platform-agent and credential-proxy when PLATFORM_AGENT_IMAGE unset", func(t *testing.T) {
		t.Setenv(platformAgentImageEnvVar, "")
		t.Setenv(credentialProxyImageEnvVar, "")
		t.Setenv(operatorImageEnvVar, "ghcr.io/gke-labs/kube-agents/k8s-operator:0.2.0")

		wantAgent := "ghcr.io/gke-labs/kube-agents/platform-agent:0.2.0"
		if got := defaultPlatformAgentImage(); got != wantAgent {
			t.Errorf("defaultPlatformAgentImage() = %q, want %q", got, wantAgent)
		}

		wantSidecar := "ghcr.io/gke-labs/kube-agents/credential-proxy:0.2.0"
		if got := resolveCredentialProxyImage(nil); got != wantSidecar {
			t.Errorf("resolveCredentialProxyImage(nil) = %q, want %q", got, wantSidecar)
		}
	})
}
