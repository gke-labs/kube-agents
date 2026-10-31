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
	"strconv"
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

func agentAPIAuthAgentWithResources(override *corev1.ResourceRequirements) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec:       agentv1alpha1.PlatformAgentSpec{AgentSpec: agentv1alpha1.AgentSpec{Deployment: &agentv1alpha1.DeploymentSpec{}}},
	}
	if override != nil {
		agent.Spec.Deployment.AgentAPIAuth = &agentv1alpha1.AgentAPIAuthSpec{Resources: override}
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
		assertQuantity(t, got.Requests, corev1.ResourceMemory, "1Gi")
		assertQuantity(t, got.Limits, corev1.ResourceCPU, "1")
		assertQuantity(t, got.Limits, corev1.ResourceMemory, "2Gi")
		assertQuantity(t, got.Limits, corev1.ResourceEphemeralStorage, "8Gi")
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
	assertQuantity(t, got.Requests, corev1.ResourceMemory, "1Gi")
	assertQuantity(t, got.Limits, corev1.ResourceCPU, "1")
	assertQuantity(t, got.Limits, corev1.ResourceEphemeralStorage, "8Gi")
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

// TestCredentialProxyEphemeralStorageLimitCoversItsEmptyDirs: the kubelet
// sums the pod's disk-backed emptyDirs, writable layer and logs against the
// container's ephemeral-storage limit, so a limit under the state and /tmp
// sizeLimits together evicts the pod before either volume reaches its own cap
// (three evictions on a hyperscale fleet at the old 2Gi, #4314). The limit
// holds the two caps plus the declared headroom for the writable layer and
// the container's logs, and stays inside the 10Gi GKE Autopilot admits per
// pod, because Autopilot sets the ephemeral-storage limit equal to the request
// and the operator declares no request, so the API server defaults the request
// to this limit.
func TestCredentialProxyEphemeralStorageLimitCoversItsEmptyDirs(t *testing.T) {
	limit := resource.MustParse(credentialProxyEphemeralStorageLimit)
	needed := resource.MustParse(credentialProxyStateSizeLimit)
	needed.Add(resource.MustParse(credentialProxyTmpSizeLimit))
	needed.Add(resource.MustParse(credentialProxyEphemeralStorageHeadroom))
	if limit.Cmp(needed) < 0 {
		t.Errorf("ephemeral-storage limit %s is under the state and /tmp sizeLimits plus the %s headroom (%s): the kubelet evicts the pod before the state volume can fill, or the writable layer and logs have no room", limit.String(), credentialProxyEphemeralStorageHeadroom, needed.String())
	}
	if autopilotCeiling := resource.MustParse("10Gi"); limit.Cmp(autopilotCeiling) > 0 {
		t.Errorf("ephemeral-storage limit %s is above the %s Autopilot admits per pod, and the request defaults to the limit", limit.String(), autopilotCeiling.String())
	}
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
// the container's 8Gi default gives the excess to the state volume (5Gi) and
// leaves /tmp at its 2Gi cap, so the two caps and the 1Gi headroom still add
// up to the limit; a limit at or under the default moves neither, the default
// render included, because the kubelet sums the two volumes against the
// container's limit and each cap keeps a runaway in one volume out of the
// other's share; and the gateway pod's /tmp, which shares the volume's
// definition but not the broker's container, keeps its default.
func TestCredentialProxyEmptyDirsFollowARaisedEphemeralStorageLimit(t *testing.T) {
	cases := []struct {
		limit, tmp, state string
	}{
		{"", "2Gi", "5Gi"},
		{"1Gi", "2Gi", "5Gi"},
		{"6Gi", "2Gi", "5Gi"},
		{"8Gi", "2Gi", "5Gi"},
		{"10Gi", "2Gi", "7Gi"},
		{"16Gi", "2Gi", "13Gi"},
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

// TestAgentAPIAuthEmptyDirFollowsARaisedEphemeralStorageLimit: the sidecar's /tmp
// is the credential-proxy-tmp emptyDir, and the kubelet evicts the gateway pod
// when it passes its sizeLimit. So a raised agentAPIAuth ephemeral limit must
// widen that volume, a limit at or under the 2Gi default moves it not at all, and
// the sidecar's own override drives it, independently of the broker's.
func TestAgentAPIAuthEmptyDirFollowsARaisedEphemeralStorageLimit(t *testing.T) {
	cases := []struct {
		limit, tmp string
	}{
		{"", "2Gi"},
		{"1Gi", "2Gi"},
		{"3Gi", "3Gi"},
		{"10Gi", "10Gi"},
	}
	for _, tc := range cases {
		var override *corev1.ResourceRequirements
		if tc.limit != "" {
			override = &corev1.ResourceRequirements{
				Limits: corev1.ResourceList{corev1.ResourceEphemeralStorage: resource.MustParse(tc.limit)},
			}
		}
		agent := agentAPIAuthAgentWithResources(override)
		if got := emptyDirSizeLimits(buildAgentAPIAuthVolumes(agent))["credential-proxy-tmp"]; got != tc.tmp {
			t.Errorf("limit %q: gateway /tmp %s, want %s", tc.limit, got, tc.tmp)
		}
	}

	// The sidecar's override widens the gateway pod's volume, not the broker's.
	agent := agentAPIAuthAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceEphemeralStorage: resource.MustParse("10Gi")},
	})
	if got := emptyDirSizeLimits(buildCredentialProxyRuntimeVolumes(agent))["credential-proxy-tmp"]; got != "2Gi" {
		t.Errorf("broker /tmp %s, want the 2Gi default; the sidecar's override must not widen the broker's volume", got)
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
	// command. What the proxy admits at once is the smaller of its memory
	// budget and its slot cap, so both have to cover that many.
	slotCap, err := strconv.ParseInt(credentialProxyMaxConcurrentCommands, 10, 64)
	if err != nil {
		t.Fatalf("credentialProxyMaxConcurrentCommands = %q is not an integer: %v", credentialProxyMaxConcurrentCommands, err)
	}
	admitted := min(credentialProxyAdmittedRequests(defaultLimit.Value(), credentialProxyOutputCapBytes), slotCap)
	if admitted < defaultKanbanMaxInProgress {
		t.Errorf("the proxy admits %d commands at once at its defaults (memory budget or slot cap of %d, whichever is smaller), fewer than the %d kanban workers the default cap runs", admitted, slotCap, defaultKanbanMaxInProgress)
	}
	if got := credentialProxyMinimumMemoryLimitBytes(credentialProxyOutputCapBytes); got != 672*mib {
		t.Errorf("floor = %d MiB, want 672", got/mib)
	}
	// GKE Autopilot without bursting sets a container's limits equal to its
	// requests, so there the broker reads the request as its limit. The
	// request therefore has to clear the floor on its own, or the budget is
	// off exactly where a default install runs and the broker admits by slot
	// alone, with the out-of-memory exposure the budget exists to remove.
	defaultRequest := resource.MustParse(credentialProxyMemoryRequest)
	if floor := credentialProxyMinimumMemoryLimitBytes(credentialProxyOutputCapBytes); defaultRequest.Value() < floor {
		t.Errorf("the default memory request %s is under the budget's %d MiB floor, so on Autopilot without bursting the budget is off", defaultRequest.String(), floor/mib)
	}
	if got := credentialProxyAdmittedRequests(defaultRequest.Value(), credentialProxyOutputCapBytes); got < credentialProxyMinimumAdmittedRequests {
		t.Errorf("at its request the proxy admits %d requests, want at least %d: that is the figure on Autopilot without bursting", got, credentialProxyMinimumAdmittedRequests)
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
