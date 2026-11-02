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
	"fmt"
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
// goldens, footprint.yaml and the chart's quota preflight all carry: 500m and
// 1Gi requests on Standard, 2 CPU and 2Gi on Autopilot, and a 2 CPU limit on
// both (#4106).
func TestCredentialProxyResourcesDefaultToTheOperatorsValues(t *testing.T) {
	for _, deployment := range []*agentv1alpha1.DeploymentSpec{nil, {}, {CredentialProxy: &agentv1alpha1.CredentialProxySpec{}}} {
		autopilot := resolveCredentialProxyResourcesFor(deployment, true)
		assertQuantity(t, autopilot.Requests, corev1.ResourceCPU, "2")
		assertQuantity(t, autopilot.Requests, corev1.ResourceMemory, "2Gi")
		assertQuantity(t, autopilot.Limits, corev1.ResourceCPU, "2")
		assertQuantity(t, autopilot.Limits, corev1.ResourceMemory, "2Gi")
		got := resolveCredentialProxyResources(deployment)
		assertQuantity(t, got.Requests, corev1.ResourceCPU, "500m")
		assertQuantity(t, got.Requests, corev1.ResourceMemory, "1Gi")
		assertQuantity(t, got.Limits, corev1.ResourceCPU, "2")
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
	assertQuantity(t, got.Limits, corev1.ResourceCPU, "2")
	assertQuantity(t, got.Limits, corev1.ResourceEphemeralStorage, "8Gi")
}

// TestCredentialProxyLimitSetAloneBelowTheDefaultRequestLowersTheRequest: a CR
// that sets only a limit below the default request (limits.cpu 1 or
// limits.memory 1536Mi against Autopilot's 2 CPU and 2Gi, limits.cpu 200m
// against Standard's 500m) renders that limit as its request rather than a
// crossed pair the API server and the validation refuse. A limit above the
// default request, or a request the CR sets itself, is left as written.
func TestCredentialProxyLimitSetAloneBelowTheDefaultRequestLowersTheRequest(t *testing.T) {
	cpuAlone := proxyAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1")},
	}).Spec.Deployment
	got := resolveCredentialProxyResourcesFor(cpuAlone, true)
	assertQuantity(t, got.Requests, corev1.ResourceCPU, "1")
	assertQuantity(t, got.Limits, corev1.ResourceCPU, "1")
	assertQuantity(t, got.Requests, corev1.ResourceMemory, "2Gi")

	standard := resolveCredentialProxyResources(proxyAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("200m")},
	}).Spec.Deployment)
	assertQuantity(t, standard.Requests, corev1.ResourceCPU, "200m")
	assertQuantity(t, standard.Requests, corev1.ResourceMemory, "1Gi")

	raised := resolveCredentialProxyResourcesFor(proxyAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("4")},
	}).Spec.Deployment, true)
	assertQuantity(t, raised.Requests, corev1.ResourceCPU, "2")
	assertQuantity(t, raised.Limits, corev1.ResourceCPU, "4")

	explicit := resolveCredentialProxyResources(proxyAgentWithResources(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("3")},
		Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1")},
	}).Spec.Deployment)
	assertQuantity(t, explicit.Requests, corev1.ResourceCPU, "3")
	if errs, _ := ValidateCredentialProxyResources(proxyAgentWithResources(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("3")},
		Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1")},
	}).Spec.Deployment, credentialProxyResourcesPath); len(errs) == 0 {
		t.Error("an explicit cpu request above its limit was admitted; want refused")
	}
	if errs, _ := ValidateCredentialProxyResources(proxyAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1536Mi")},
	}).Spec.Deployment, credentialProxyResourcesPath); len(errs) != 0 {
		t.Errorf("a memory limit set alone under the default request was refused: %v", errs)
	}
}

// TestCredentialProxyAutopilotCPURequestStaysInTheBand: on Autopilot a memory
// request lowered below 2Gi (here by a limit set alone) takes a CPU request the
// CR did not set down to one CPU per GiB, so Autopilot's band does not raise
// the memory back over the CR's limit. A CPU request the CR sets is kept, and
// Standard is not touched.
func TestCredentialProxyAutopilotCPURequestStaysInTheBand(t *testing.T) {
	memoryAlone := proxyAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1536Mi")},
	}).Spec.Deployment
	got := resolveCredentialProxyResourcesFor(memoryAlone, true)
	assertQuantity(t, got.Requests, corev1.ResourceMemory, "1536Mi")
	assertQuantity(t, got.Requests, corev1.ResourceCPU, "1500m")
	assertQuantity(t, got.Limits, corev1.ResourceCPU, "2")

	explicit := resolveCredentialProxyResourcesFor(proxyAgentWithResources(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("2")},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1536Mi")},
	}).Spec.Deployment, true)
	assertQuantity(t, explicit.Requests, corev1.ResourceCPU, "2")

	standard := resolveCredentialProxyResources(memoryAlone)
	assertQuantity(t, standard.Requests, corev1.ResourceCPU, "500m")
	assertQuantity(t, standard.Requests, corev1.ResourceMemory, "1Gi")
	// 256Mi would cap the CPU request at 250m on Autopilot; Standard keeps 500m.
	smallMemory := proxyAgentWithResources(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("256Mi")},
	}).Spec.Deployment
	assertQuantity(t, resolveCredentialProxyResources(smallMemory).Requests, corev1.ResourceCPU, "500m")
	assertQuantity(t, resolveCredentialProxyResourcesFor(smallMemory, true).Requests, corev1.ResourceCPU, "250m")

	// A byte count just under 2Gi rounds the CPU request down to the 250m
	// step below it, so the pair stays in the band and draws no band warning.
	// 672Mi gives 656m before the step, which Autopilot would round up to
	// 750m and then raise the memory to 768Mi, over the CR's limit.
	justUnder := proxyAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("2147483647")},
	}).Spec.Deployment
	assertQuantity(t, resolveCredentialProxyResourcesFor(justUnder, true).Requests, corev1.ResourceCPU, "1750m")
	atFloor := proxyAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("672Mi")},
	}).Spec.Deployment
	assertQuantity(t, resolveCredentialProxyResourcesFor(atFloor, true).Requests, corev1.ResourceCPU, "500m")
	// Under 256Mi the step still leaves 250m, Autopilot's smallest request.
	tiny := proxyAgentWithResources(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("128Mi")},
	}).Spec.Deployment
	assertQuantity(t, resolveCredentialProxyResourcesFor(tiny, true).Requests, corev1.ResourceCPU, "250m")
	_, warnings := ValidateCredentialProxyResources(justUnder, field.NewPath("spec"))
	for _, w := range warnings {
		if strings.Contains(w, "GiB per vCPU") {
			t.Errorf("a band-held pair drew the band warning: %q", w)
		}
	}
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
	// requests, so there the broker reads the request as its limit: the
	// Autopilot request, or the Standard one on a cluster discovery could not
	// classify. Each request therefore has to clear the floor on its own, or
	// the budget is off and the broker admits by slot alone, with the
	// out-of-memory exposure the budget exists to remove.
	autopilotRequest := resource.MustParse(credentialProxyAutopilotMemoryRequest)
	if got := credentialProxyAdmittedRequests(autopilotRequest.Value(), credentialProxyOutputCapBytes); got != 9 {
		t.Errorf("at the Autopilot memory request the proxy admits %d requests, want 9: that is the figure on Autopilot without bursting", got)
	}
	defaultRequest := resource.MustParse(credentialProxyMemoryRequest)
	if floor := credentialProxyMinimumMemoryLimitBytes(credentialProxyOutputCapBytes); defaultRequest.Value() < floor {
		t.Errorf("the Standard memory request %s is under the budget's %d MiB floor, so on an unclassified Autopilot cluster the budget is off", defaultRequest.String(), floor/mib)
	}
	if got := credentialProxyAdmittedRequests(defaultRequest.Value(), credentialProxyOutputCapBytes); got < credentialProxyMinimumAdmittedRequests {
		t.Errorf("at the Standard request the proxy admits %d requests, want at least %d: that is the figure on an unclassified Autopilot cluster", got, credentialProxyMinimumAdmittedRequests)
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

// TestClusterAutopilotCachesOnlyAnAuthoritativeAnswer: the reconciler renders
// the Autopilot requests on an Autopilot cluster, the Standard ones while
// discovery cannot say, and asks again until it can.
func TestClusterAutopilotCachesOnlyAnAuthoritativeAnswer(t *testing.T) {
	autopilotGroups := &metav1.APIGroupList{Groups: []metav1.APIGroup{{
		Name:     "auto.gke.io",
		Versions: []metav1.GroupVersionForDiscovery{{GroupVersion: "auto.gke.io/v1", Version: "v1"}},
	}}}
	dc := &mockAutopilotDiscovery{
		serverGroupsList: autopilotGroups,
		resourcesErr:     map[string]error{"auto.gke.io/v1": fmt.Errorf("503 Service Unavailable")},
	}
	r := &PlatformAgentReconciler{DiscoveryClient: dc}
	if r.clusterAutopilot() || r.autopilotResolved {
		t.Fatal("an undetermined probe was read as Autopilot or cached")
	}
	dc.resourcesErr = nil
	dc.resourcesList = map[string]*metav1.APIResourceList{"auto.gke.io/v1": {
		GroupVersion: "auto.gke.io/v1", APIResources: []metav1.APIResource{{Name: "allowlistedworkloads"}},
	}}
	if !r.clusterAutopilot() || !r.autopilotResolved {
		t.Fatal("an Autopilot cluster was not recognised once discovery answered")
	}
	container := buildCredentialProxyDeploymentForPlugins(proxyAgentWithResources(nil), "hash", nil, true, credentialProxyProfileConfig{}, r.clusterAutopilot()).Spec.Template.Spec.Containers[0]
	assertQuantity(t, container.Resources.Requests, corev1.ResourceCPU, "2")
	assertQuantity(t, container.Resources.Requests, corev1.ResourceMemory, "2Gi")
}
