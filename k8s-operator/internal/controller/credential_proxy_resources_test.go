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
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/validation/field"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func proxyAgentWithResources(override *corev1.ResourceRequirements) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec:       agentv1alpha1.PlatformAgentSpec{AgentSpec: agentv1alpha1.AgentSpec{Deployment: &agentv1alpha1.DeploymentSpec{}}},
	}
	if override != nil {
		agent.Spec.Deployment.CredentialProxy = &agentv1alpha1.CredentialProxySpec{Resources: override}
	}
	return agent
}

func assertQuantity(t *testing.T, list corev1.ResourceList, name corev1.ResourceName, want string) {
	t.Helper()
	got, ok := list[name]
	if !ok {
		t.Fatalf("%s is missing from %v", name, list)
	}
	if got.Cmp(resource.MustParse(want)) != 0 {
		t.Errorf("%s = %s, want %s", name, got.String(), want)
	}
}

// TestCredentialProxyResourcesDefaultToTheOperatorsValues pins the values the
// goldens, footprint.yaml and the chart's quota preflight all carry: a nil CR
// override renders exactly what the literals rendered before the field existed.
func TestCredentialProxyResourcesDefaultToTheOperatorsValues(t *testing.T) {
	for _, deployment := range []*agentv1alpha1.DeploymentSpec{nil, {}, {CredentialProxy: &agentv1alpha1.CredentialProxySpec{}}} {
		got := resolveCredentialProxyResources(deployment)
		assertQuantity(t, got.Requests, corev1.ResourceCPU, "500m")
		assertQuantity(t, got.Requests, corev1.ResourceMemory, "512Mi")
		assertQuantity(t, got.Limits, corev1.ResourceCPU, "1")
		assertQuantity(t, got.Limits, corev1.ResourceMemory, "2Gi")
		assertQuantity(t, got.Limits, corev1.ResourceEphemeralStorage, "2Gi")
		if len(got.Requests) != 2 || len(got.Limits) != 3 {
			t.Errorf("default render carries %d requests and %d limits, want 2 and 3", len(got.Requests), len(got.Limits))
		}
	}
	container := buildCredentialProxyContainer(proxyAgentWithResources(nil))
	if container.Resources.Limits.Memory().Cmp(resource.MustParse("2Gi")) != 0 {
		t.Errorf("container memory limit = %s, want the 2Gi default", container.Resources.Limits.Memory())
	}
}

// TestCredentialProxyMemoryLimitOverrideKeepsTheOtherDefaults is the case the
// field exists for (#2324): a CR raises limits.memory and nothing else, and the
// CPU request Autopilot sizes the pod by, the CPU limit and the
// ephemeral-storage limit that bounds the content workspace all survive.
func TestCredentialProxyMemoryLimitOverrideKeepsTheOtherDefaults(t *testing.T) {
	container := buildCredentialProxyContainer(proxyAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("3Gi")},
	}))
	got := container.Resources
	assertQuantity(t, got.Limits, corev1.ResourceMemory, "3Gi")
	assertQuantity(t, got.Requests, corev1.ResourceCPU, "500m")
	assertQuantity(t, got.Requests, corev1.ResourceMemory, "512Mi")
	assertQuantity(t, got.Limits, corev1.ResourceCPU, "1")
	assertQuantity(t, got.Limits, corev1.ResourceEphemeralStorage, "2Gi")
}

// TestCredentialProxyFullOverrideReplacesEveryKey: a CR that states every key
// gets every key, with nothing of the operator's left underneath.
func TestCredentialProxyFullOverrideReplacesEveryKey(t *testing.T) {
	container := buildCredentialProxyContainer(proxyAgentWithResources(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{
			corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("1Gi"),
		},
		Limits: corev1.ResourceList{
			corev1.ResourceCPU: resource.MustParse("2"), corev1.ResourceMemory: resource.MustParse("4Gi"), corev1.ResourceEphemeralStorage: resource.MustParse("8Gi"),
		},
	}))
	got := container.Resources
	assertQuantity(t, got.Requests, corev1.ResourceCPU, "1")
	assertQuantity(t, got.Requests, corev1.ResourceMemory, "1Gi")
	assertQuantity(t, got.Limits, corev1.ResourceCPU, "2")
	assertQuantity(t, got.Limits, corev1.ResourceMemory, "4Gi")
	assertQuantity(t, got.Limits, corev1.ResourceEphemeralStorage, "8Gi")
}

func emptyDirSizeLimits(volumes []corev1.Volume) map[string]string {
	sizes := map[string]string{}
	for _, vol := range volumes {
		if vol.EmptyDir != nil && vol.EmptyDir.SizeLimit != nil {
			sizes[vol.Name] = vol.EmptyDir.SizeLimit.String()
		}
	}
	return sizes
}

// TestCredentialProxyEmptyDirsFollowARaisedEphemeralStorageLimit: the kubelet
// evicts the pod when an emptyDir passes its sizeLimit, so a limit raised past
// the /tmp (2Gi) and state (5Gi) defaults widens both, a limit under them moves
// neither, and the gateway pod's /tmp, which shares the volume's definition but
// not the broker's container, keeps its default.
func TestCredentialProxyEmptyDirsFollowARaisedEphemeralStorageLimit(t *testing.T) {
	cases := []struct {
		limit, tmp, state string
	}{
		{"", "2Gi", "5Gi"},
		{"1Gi", "2Gi", "5Gi"},
		{"3Gi", "3Gi", "5Gi"},
		{"8Gi", "8Gi", "8Gi"},
	}
	for _, tc := range cases {
		var override *corev1.ResourceRequirements
		if tc.limit != "" {
			override = &corev1.ResourceRequirements{
				Limits: corev1.ResourceList{corev1.ResourceEphemeralStorage: resource.MustParse(tc.limit)},
			}
		}
		agent := proxyAgentWithResources(override)
		got := emptyDirSizeLimits(buildCredentialProxyRuntimeVolumes(agent))
		if got["credential-proxy-tmp"] != tc.tmp || got["credential-proxy-state"] != tc.state {
			t.Errorf("limit %q: tmp %s, state %s; want %s and %s", tc.limit, got["credential-proxy-tmp"], got["credential-proxy-state"], tc.tmp, tc.state)
		}
		if got["credential-proxy-runtime"] != "16Mi" {
			t.Errorf("limit %q: runtime %s, want the 16Mi default", tc.limit, got["credential-proxy-runtime"])
		}
		if gateway := emptyDirSizeLimits(buildAgentAPIAuthVolumes(agent))["credential-proxy-tmp"]; gateway != "2Gi" {
			t.Errorf("limit %q: gateway /tmp %s, want the 2Gi default", tc.limit, gateway)
		}
	}
}

// TestCredentialProxyOverrideDoesNotAliasTheCR: the render builds maps of its
// own rather than handing back the CR's, and copies each quantity, so writing
// to the rendered Requests or Limits, or to a rendered quantity in place,
// cannot reach back into the object the reconciler was handed. The in-place
// case needs a quantity held in decimal form, whose value sits behind a
// pointer that a struct copy shares and only DeepCopy separates.
func TestCredentialProxyOverrideDoesNotAliasTheCR(t *testing.T) {
	decimalLimit := resource.MustParse("2Gi")
	decimalLimit.ToDec()
	override := &corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: decimalLimit},
	}
	got := resolveCredentialProxyResources(&agentv1alpha1.DeploymentSpec{CredentialProxy: &agentv1alpha1.CredentialProxySpec{Resources: override}})
	rendered := got.Limits[corev1.ResourceMemory]
	rendered.AsDec().SetScale(rendered.AsDec().Scale() - 1)
	if override.Limits.Memory().Cmp(resource.MustParse("2Gi")) != 0 {
		t.Fatalf("scaling the rendered limit in place changed the CR's override to %d bytes", override.Limits.Memory().Value())
	}
	got.Requests[corev1.ResourceMemory] = resource.MustParse("3Gi")
	got.Limits[corev1.ResourceMemory] = resource.MustParse("4Gi")
	got.Requests["hugepages-2Mi"] = resource.MustParse("2Mi")
	delete(got.Limits, corev1.ResourceMemory)
	if len(override.Requests) != 1 || override.Requests.Memory().Cmp(resource.MustParse("1Gi")) != 0 {
		t.Errorf("writing the rendered requests changed the CR's override to %v", override.Requests)
	}
	if len(override.Limits) != 1 || override.Limits.Memory().Cmp(resource.MustParse("2Gi")) != 0 {
		t.Errorf("writing the rendered limits changed the CR's override to %v", override.Limits)
	}
}

// TestCredentialProxyBudgetArithmeticAtTheDefaults pins the numbers the design
// quotes (docs/designs/credential-proxy-child-memory-budget.md §2.2 and §2.5):
// a request costs 176 MiB at the 8 MiB cap, the 2Gi default admits nine
// ((2048 - 192 - 128) / 176 = 9.8), the slot cap holds that to eight, and the
// floor that admits two is 672 MiB.
func TestCredentialProxyBudgetArithmeticAtTheDefaults(t *testing.T) {
	const mib = 1 << 20
	if credentialProxyOutputCapBytes != 8*mib {
		t.Fatalf("output cap parsed as %d bytes, want 8 MiB", credentialProxyOutputCapBytes)
	}
	if got := credentialProxyRequestCostBytes(credentialProxyOutputCapBytes); got != 176*mib {
		t.Errorf("request cost = %d MiB, want 176", got/mib)
	}
	defaultLimit := resource.MustParse(credentialProxyMemoryLimit)
	if got := credentialProxyAdmittedRequests(defaultLimit.Value(), credentialProxyOutputCapBytes); got != 9 {
		t.Errorf("the default limit admits %d requests, want 9", got)
	}
	// The kanban default runs six workers, each of which may hold a brokered
	// command; the default limit has to admit at least that many (#2678).
	if got := credentialProxyAdmittedRequests(defaultLimit.Value(), credentialProxyOutputCapBytes); got < defaultKanbanMaxInProgress {
		t.Errorf("the default limit admits %d requests, fewer than the %d kanban workers the default cap runs", got, defaultKanbanMaxInProgress)
	}
	if got := credentialProxyMinimumMemoryLimitBytes(credentialProxyOutputCapBytes); got != 672*mib {
		t.Errorf("floor = %d MiB, want 672", got/mib)
	}
	if got := credentialProxyMinimumMemoryLimitBytes(credentialProxyOutputCapBytes); got != credentialProxyMemoryFloorBytesAtDefaultCap {
		t.Errorf("floor = %d bytes, but credentialProxyMemoryFloorBytesAtDefaultCap declares %d; the declared copy the chart and the broker are compared with has drifted from the function", got, credentialProxyMemoryFloorBytesAtDefaultCap)
	}
	if got := credentialProxyAdmittedRequests(credentialProxyMinimumMemoryLimitBytes(credentialProxyOutputCapBytes), credentialProxyOutputCapBytes); got != credentialProxyMinimumAdmittedRequests {
		t.Errorf("the floor admits %d requests, want %d", got, credentialProxyMinimumAdmittedRequests)
	}
	if got := credentialProxyAdmittedRequests(credentialProxyResidentReserveBytes, credentialProxyOutputCapBytes); got != 0 {
		t.Errorf("a limit below the fixed reserves admits %d requests, want 0", got)
	}
}

// A long resource name is cut where it enters the field path, so the refusal
// keeps the reason that follows the path, and the count of further refusals.
func TestCredentialProxyRefusalOfALongNameKeepsTheReason(t *testing.T) {
	long := corev1.ResourceName("example.com/" + strings.Repeat("y", 2*credentialProxyRefusalMessageBudget))
	agent := proxyAgentWithResources(&corev1.ResourceRequirements{
		Limits:   corev1.ResourceList{long: resource.MustParse("1")},
		Requests: corev1.ResourceList{long: resource.MustParse("1")},
	})
	refusal, _ := credentialProxyResourcesRefusal(agent)
	if len(refusal) > credentialProxyRefusalMessageBudget {
		t.Errorf("refusal is %d characters, want at most %d", len(refusal), credentialProxyRefusalMessageBudget)
	}
	if !strings.Contains(refusal, credentialProxyRefusalEllipsis+": ") {
		t.Errorf("refusal does not mark the cut name: %q", refusal)
	}
	if !strings.Contains(refusal, credentialProxyResourceNameRefusal) {
		t.Errorf("refusal dropped the reason: %q", refusal)
	}
	if !strings.HasSuffix(refusal, " (and 1 more)") {
		t.Errorf("refusal %q does not end with the count", refusal)
	}
}

// The whole-message budget is the backstop for a refusal that is long for any
// other reason: the first refusal gives way to the count of the rest, and the
// whole stays within the budget.
func TestCredentialProxyRefusalKeepsTheCountWithinTheBudget(t *testing.T) {
	path := field.NewPath("spec")
	errs := field.ErrorList{
		field.Invalid(path, "x", strings.Repeat("z", 2*credentialProxyRefusalMessageBudget)),
		field.Invalid(path, "x", "second"),
	}
	refusal := boundCredentialProxyRefusal(errs)
	if len(refusal) > credentialProxyRefusalMessageBudget {
		t.Errorf("refusal is %d characters, want at most %d", len(refusal), credentialProxyRefusalMessageBudget)
	}
	if !strings.HasSuffix(refusal, credentialProxyRefusalEllipsis+" (and 1 more)") {
		t.Errorf("refusal ends %q, want the ellipsis and the count", refusal[len(refusal)-40:])
	}
}
