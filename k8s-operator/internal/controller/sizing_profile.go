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
	"context"
	"fmt"
	"slices"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// sizingTier is one of the three preset rows spec.deployment.sizingProfile
// selects for the credential-proxy container, the agent-api-auth sidecar and
// the shell container. Ordered: a higher tier compares greater, which is what
// the upward-only latch under the auto profile compares.
type sizingTier int

const (
	sizingTierSmall sizingTier = iota
	sizingTierMedium
	sizingTierFleet
)

const (
	// The spec.deployment.sizingProfile values. small is also what an absent
	// field means, so an install that predates the field renders as it did.
	sizingProfileSmall  = "small"
	sizingProfileMedium = "medium"
	sizingProfileFleet  = "fleet"
	sizingProfileAuto   = "auto"

	// The cluster counts at which the auto profile earns a tier, read off
	// the larger of status.usage.clustersRegistered and clustersMonitored.
	// A fleet of ten clusters outgrows the small broker's two-command memory
	// budget, and at twenty-five the event watcher's informers outgrow a 4Gi
	// sidecar. Below the first the tier stays small.
	sizingMediumClusterThreshold int64 = 10
	sizingFleetClusterThreshold  int64 = 25

	// The medium row. The broker gets 1 CPU and 4Gi of requests, 2 CPU and
	// 4Gi of limits; the Autopilot CPU request stays at the small row's 2
	// CPU, because Autopilot without bursting runs the pod at its requests
	// and a step up that lowered the broker's CPU there would be a step down.
	// The memory limit sizes the broker's child memory budget
	// (credentialProxyAdmittedRequests): 4Gi admits the eight-command slot
	// cap with listings of a few hundred Mi each. The sidecar's memory limit
	// doubles to 4Gi for the event watcher's informers; its requests, CPU
	// limit and ephemeral-storage limit keep the small row's figures, and
	// the shell keeps the small row entirely.
	credentialProxyMediumCPURequest          = "1"
	credentialProxyMediumMemoryRequest       = "4Gi"
	credentialProxyMediumAutopilotCPURequest = "2"
	credentialProxyMediumCPULimit            = "2"
	credentialProxyMediumMemoryLimit         = "4Gi"
	agentAPIAuthMediumMemoryLimit            = "4Gi"

	// The fleet row. The broker gets 4 CPU and 8Gi of requests on both
	// cluster types (2 GiB per vCPU, inside the band Autopilot admits
	// unchanged), 8 CPU and 8Gi of limits, and keeps the 8Gi
	// ephemeral-storage limit, which already holds its two emptyDir caps and
	// is the most Autopilot admits per pod with headroom. The sidecar's memory
	// limit goes to 8Gi and the shell's to 4Gi, for the fan-out of a fleet
	// sweep; every other figure keeps the small row's.
	credentialProxyFleetCPURequest            = "4"
	credentialProxyFleetMemoryRequest         = "8Gi"
	credentialProxyFleetCPULimit              = "8"
	credentialProxyFleetMemoryLimit           = "8Gi"
	credentialProxyFleetEphemeralStorageLimit = "8Gi"
	agentAPIAuthFleetMemoryLimit              = "8Gi"
	shellSandboxFleetMemoryLimit              = "4Gi"

	// The condition reporting that the auto profile earned a higher tier
	// than the namespace's ResourceQuota has room for. Written only in that
	// state, on the ReplicasLimited pattern, by resolveSizingTier alone; the
	// Ready and Degraded writers carry it through as they read it. The
	// message names the quota and the dimension, because the person reading
	// `kubectl describe` decides between raising the quota and naming a tier.
	// It carries no quantities: the status write and the Warning event are
	// gated on the message, and the quota's free figure moves with every pod
	// the namespace admits, so a figure in the message would write status and
	// record a Warning on every session pod a busy install runs. The figures
	// go in the event and the log line, recorded when the message changes.
	sizingCappedByQuotaConditionType = "SizingCappedByQuota"
	sizingCappedByQuotaReason        = "ResourceQuotaShort"
	// sizingCappedByQuotaMessageFormat takes the tier earned, the quota's
	// name, the quota key, the running tier, and the running tier again.
	sizingCappedByQuotaMessageFormat = "spec.deployment.sizingProfile is auto and the cluster count earns the %s tier, " +
		"but ResourceQuota %s has no room on %s for the step up from %s; the %s tier stays until the quota is raised " +
		"or sizingProfile names the tier, which the operator applies without this check"
	// sizingCappedByQuotaEventFormat is the Warning event's text: the
	// condition message, then the figures as they stood when it was written.
	// It takes the message, the cluster count, the free quantity, the quota
	// key and the quantity the step adds.
	sizingCappedByQuotaEventFormat = "%s (at %d clusters: %s of %s free, the step adds %s)"
	// sizingTierChangedReason is the Normal event a tier change records, and
	// sizingCappedByQuotaEventReason the Warning the first capped pass records.
	sizingTierChangedReason        = "SizingTierChanged"
	sizingCappedByQuotaEventReason = "SizingCappedByQuota"
	sizingTierChangedMessageFormat = "sizing tier %s -> %s (spec.deployment.sizingProfile %s)"

	// The three sized workloads the step-up check reads back: the gateway
	// workload (a Deployment, or a StatefulSet under useStatefulSet) named
	// with this suffix carries the agent-api-auth sidecar, the credential
	// proxy Deployment (credentialProxyName) the broker, and the shell
	// StatefulSet (shellSandboxName) the shell container, which
	// shell_sandbox_manifests.go renders under this name.
	gatewayWorkloadNameSuffix = "-gateway"
	shellSandboxContainerName = "shell"
)

// sizingSettledRequests and sizingSettledLimits are the resource names the
// settled check (sizedContainerCarries) compares on a sized container's
// template: the figures the tier rows move. The proxy's ephemeral-storage is
// a limit alone, so it is read on that side only; an ephemeral-storage
// request is what GKE Autopilot adds to a template and no row sets.
var (
	sizingSettledRequests = []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory}
	sizingSettledLimits   = []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory, corev1.ResourceEphemeralStorage}
)

// sizingShortfall is one ResourceQuota dimension without room for a step up:
// the quota, the key, what was free and what the step adds. The condition
// message is built from the quota and the key alone (see
// sizingCappedByQuotaMessageFormat); the figures reach the event and the log.
type sizingShortfall struct {
	quota string
	key   corev1.ResourceName
	free  resource.Quantity
	need  resource.Quantity
}

// sizingQuotaDimensions are the ResourceQuota hard keys the step-up check
// reads, each with the container resource it counts and the side of the step
// it is compared with. The bare names count requests, as the API server reads
// them. Any other hard key (pods, count/*, storage) is not moved by a tier
// change and is not read.
var sizingQuotaDimensions = []struct {
	key      corev1.ResourceName
	resource corev1.ResourceName
	limits   bool
}{
	{corev1.ResourceCPU, corev1.ResourceCPU, false},
	{corev1.ResourceRequestsCPU, corev1.ResourceCPU, false},
	{corev1.ResourceLimitsCPU, corev1.ResourceCPU, true},
	{corev1.ResourceMemory, corev1.ResourceMemory, false},
	{corev1.ResourceRequestsMemory, corev1.ResourceMemory, false},
	{corev1.ResourceLimitsMemory, corev1.ResourceMemory, true},
	{corev1.ResourceEphemeralStorage, corev1.ResourceEphemeralStorage, false},
	{corev1.ResourceRequestsEphemeralStorage, corev1.ResourceEphemeralStorage, false},
	{corev1.ResourceLimitsEphemeralStorage, corev1.ResourceEphemeralStorage, true},
}

// String is the tier's name as spec.deployment.sizingProfile and
// status.effectiveSizingTier spell it.
func (t sizingTier) String() string {
	switch t {
	case sizingTierMedium:
		return sizingProfileMedium
	case sizingTierFleet:
		return sizingProfileFleet
	default:
		return sizingProfileSmall
	}
}

// parseSizingTier reads a tier name; false for anything that is not one of
// the three, auto and "" included.
func parseSizingTier(name string) (sizingTier, bool) {
	switch name {
	case sizingProfileSmall:
		return sizingTierSmall, true
	case sizingProfileMedium:
		return sizingTierMedium, true
	case sizingProfileFleet:
		return sizingTierFleet, true
	default:
		return sizingTierSmall, false
	}
}

// sizingProfileOf is spec.deployment.sizingProfile, with an absent block or
// field read as small.
func sizingProfileOf(deployment *agentv1alpha1.DeploymentSpec) string {
	if deployment == nil || deployment.SizingProfile == "" {
		return sizingProfileSmall
	}
	return deployment.SizingProfile
}

// specSizingTier is the tier the spec alone fixes: an explicit profile's
// tier, and small under auto, where the tier is whatever the latch has
// reached. Admission validates an override against it, so under auto the
// webhook checks the small row; the reconciler validates against the rendered
// tier, and a higher row never refuses an override a lower one admitted (its
// limits are at or above the lower row's and its requests are either the same
// or lowered to a limit set alone), so the webhook is never looser than the
// reconciler.
func specSizingTier(deployment *agentv1alpha1.DeploymentSpec) sizingTier {
	tier, _ := parseSizingTier(sizingProfileOf(deployment))
	return tier
}

// renderedSizingTier is the tier the builders render: an explicit profile's,
// or under auto the latched status.effectiveSizingTier, small before the
// first pass wrote one. resolveSizingTier writes the status before any builder
// reads this, so the render and the status agree within a pass; a golden
// fixture carries the status it wants rendered.
func renderedSizingTier(agent *agentv1alpha1.PlatformAgent) sizingTier {
	if agent == nil {
		return sizingTierSmall
	}
	if sizingProfileOf(agent.Spec.Deployment) != sizingProfileAuto {
		return specSizingTier(agent.Spec.Deployment)
	}
	tier, _ := parseSizingTier(agent.Status.EffectiveSizingTier)
	return tier
}

// sizingTierForClusterCount is the tier the auto profile earns at a cluster
// count.
func sizingTierForClusterCount(count int64) sizingTier {
	switch {
	case count >= sizingFleetClusterThreshold:
		return sizingTierFleet
	case count >= sizingMediumClusterThreshold:
		return sizingTierMedium
	default:
		return sizingTierSmall
	}
}

// observedClusterCount is the larger of status.usage.clustersRegistered and
// clustersMonitored, false when the operator has no current reading of either
// (before the first poll, while the event watcher is disabled, after two
// polls that read no gateway replica). Monitored cannot exceed registered on
// one poll, but the two are written from one reading each and the larger is
// the one a fleet is sized for.
func observedClusterCount(agent *agentv1alpha1.PlatformAgent) (int64, bool) {
	usage := agent.Status.Usage
	if usage.ClustersRegistered == nil && usage.ClustersMonitored == nil {
		return 0, false
	}
	var count int64
	if usage.ClustersRegistered != nil {
		count = *usage.ClustersRegistered
	}
	if usage.ClustersMonitored != nil {
		count = max(count, *usage.ClustersMonitored)
	}
	return count, true
}

// credentialProxyTierResources is the credential-proxy container's row before
// the CR's override: the small row is the operator's defaults, with the
// Autopilot requests when autopilot is true. Fresh maps on every call, because
// the resolver writes the override into them.
func credentialProxyTierResources(tier sizingTier, autopilot bool) corev1.ResourceRequirements {
	cpuRequest, memoryRequest := credentialProxyCPURequest, credentialProxyMemoryRequest
	cpuLimit, memoryLimit, ephemeralLimit := credentialProxyCPULimit, credentialProxyMemoryLimit, credentialProxyEphemeralStorageLimit
	switch tier {
	case sizingTierMedium:
		cpuRequest, memoryRequest = credentialProxyMediumCPURequest, credentialProxyMediumMemoryRequest
		cpuLimit, memoryLimit = credentialProxyMediumCPULimit, credentialProxyMediumMemoryLimit
		if autopilot {
			cpuRequest = credentialProxyMediumAutopilotCPURequest
		}
	case sizingTierFleet:
		cpuRequest, memoryRequest = credentialProxyFleetCPURequest, credentialProxyFleetMemoryRequest
		cpuLimit, memoryLimit, ephemeralLimit = credentialProxyFleetCPULimit, credentialProxyFleetMemoryLimit, credentialProxyFleetEphemeralStorageLimit
	default:
		if autopilot {
			cpuRequest, memoryRequest = credentialProxyAutopilotCPURequest, credentialProxyAutopilotMemoryRequest
		}
	}
	return corev1.ResourceRequirements{
		Requests: corev1.ResourceList{
			corev1.ResourceCPU:    resource.MustParse(cpuRequest),
			corev1.ResourceMemory: resource.MustParse(memoryRequest),
		},
		Limits: corev1.ResourceList{
			corev1.ResourceCPU:              resource.MustParse(cpuLimit),
			corev1.ResourceMemory:           resource.MustParse(memoryLimit),
			corev1.ResourceEphemeralStorage: resource.MustParse(ephemeralLimit),
		},
	}
}

// agentAPIAuthTierResources is the agent-api-auth sidecar's row before the
// CR's override. The rows differ in the memory limit alone.
func agentAPIAuthTierResources(tier sizingTier) corev1.ResourceRequirements {
	memoryLimit := agentAPIAuthMemoryLimit
	switch tier {
	case sizingTierMedium:
		memoryLimit = agentAPIAuthMediumMemoryLimit
	case sizingTierFleet:
		memoryLimit = agentAPIAuthFleetMemoryLimit
	}
	return corev1.ResourceRequirements{
		Requests: corev1.ResourceList{
			corev1.ResourceCPU:    resource.MustParse(agentAPIAuthCPURequest),
			corev1.ResourceMemory: resource.MustParse(agentAPIAuthMemoryRequest),
		},
		Limits: corev1.ResourceList{
			corev1.ResourceCPU:              resource.MustParse(agentAPIAuthCPULimit),
			corev1.ResourceMemory:           resource.MustParse(memoryLimit),
			corev1.ResourceEphemeralStorage: resource.MustParse(agentAPIAuthEphemeralStorageLimit),
		},
	}
}

// shellSandboxTierResources is the shell container's row before the CR's
// override. Only the fleet row differs, in the memory limit.
func shellSandboxTierResources(tier sizingTier) corev1.ResourceRequirements {
	memoryLimit := shellSandboxMemoryLimit
	if tier == sizingTierFleet {
		memoryLimit = shellSandboxFleetMemoryLimit
	}
	return corev1.ResourceRequirements{
		Requests: corev1.ResourceList{
			corev1.ResourceCPU:    resource.MustParse(shellSandboxCPURequest),
			corev1.ResourceMemory: resource.MustParse(shellSandboxMemoryRequest),
		},
		Limits: corev1.ResourceList{
			corev1.ResourceCPU:    resource.MustParse(shellSandboxCPULimit),
			corev1.ResourceMemory: resource.MustParse(memoryLimit),
		},
	}
}

// sizingStep is what a step from one tier to another adds, per resource name,
// on the requests side and on the limits side: the differences across the
// three containers a profile sizes, each side as the reconciler renders that
// tier (the CR's override where the tier admits it, the row where it does
// not). Each of
// the three pods is replaced rather than surged on a template change (the
// proxy's and the gateway's Recreate Deployments, the sandbox StatefulSet's
// ordered roll), so what the quota has to admit is the difference, not a
// second pod; the RollingUpdate shape replicasAndStrategyFor keeps for the
// multi-replica redesign is reached by no caller.
type sizingStep struct {
	requests corev1.ResourceList
	limits   corev1.ResourceList
}

// renderedCredentialProxyResourcesAt is the credential-proxy container as
// reconcileCredentialProxy renders it at tier: the CR's override merged over
// the row when the tier's validator admits it, and the bare row when it
// refuses it, because the reconciler strips a refused override and renders
// the row (credentialProxyResourcesRefusal, read at the rendered tier). The
// step between two tiers reads each side through this rather than through the
// resolver alone: an override the lower row refuses and the higher row admits
// (a 3 CPU request against the small row's 2 CPU limit and the fleet row's 8)
// moves the pod from the row to the override, and a step that merged it on
// both sides would count that move as zero and consult no quota for it. The
// validator is the one the reconciler reads, so the two agree on every
// refusal; the stripped render is the resolver on a nil override, the row.
func renderedCredentialProxyResourcesAt(deployment *agentv1alpha1.DeploymentSpec, autopilot bool, tier sizingTier) corev1.ResourceRequirements {
	if errs, _ := validateCredentialProxyResourcesAt(deployment, tier, credentialProxyResourcesPath); len(errs) > 0 {
		return credentialProxyTierResources(tier, autopilot)
	}
	return resolveCredentialProxyResourcesFor(deployment, autopilot, tier)
}

// renderedAgentAPIAuthResourcesAt is the agent-api-auth sidecar as
// reconcileWorkload renders it at tier, on renderedCredentialProxyResourcesAt's
// rule.
func renderedAgentAPIAuthResourcesAt(deployment *agentv1alpha1.DeploymentSpec, tier sizingTier) corev1.ResourceRequirements {
	if errs, _ := validateAgentAPIAuthResourcesAt(deployment, tier, agentAPIAuthResourcesPath); len(errs) > 0 {
		return agentAPIAuthTierResources(tier)
	}
	return resolveAgentAPIAuthResources(deployment, tier)
}

// renderedShellSandboxResourcesAt is the shell container as
// reconcileShellSandbox renders it at tier, on
// renderedCredentialProxyResourcesAt's rule.
func renderedShellSandboxResourcesAt(agent *agentv1alpha1.PlatformAgent, tier sizingTier) corev1.ResourceRequirements {
	if errs, _ := validateShellSandboxResourcesAt(agent, tier, shellSandboxResourcesPath); len(errs) > 0 {
		return shellSandboxTierResources(tier)
	}
	return resolveShellSandboxResources(agent, tier)
}

// sizingStepBetween is the sizingStep from tier from to tier to for agent,
// each side as the reconciler renders that tier.
func sizingStepBetween(agent *agentv1alpha1.PlatformAgent, autopilot bool, from, to sizingTier) sizingStep {
	deployment := agent.Spec.Deployment
	pairs := [][2]corev1.ResourceRequirements{
		{renderedCredentialProxyResourcesAt(deployment, autopilot, from), renderedCredentialProxyResourcesAt(deployment, autopilot, to)},
		{renderedAgentAPIAuthResourcesAt(deployment, from), renderedAgentAPIAuthResourcesAt(deployment, to)},
		{renderedShellSandboxResourcesAt(agent, from), renderedShellSandboxResourcesAt(agent, to)},
	}
	step := sizingStep{requests: corev1.ResourceList{}, limits: corev1.ResourceList{}}
	for _, pair := range pairs {
		addResourceDelta(step.requests, effectiveRequests(pair[0]), effectiveRequests(pair[1]))
		addResourceDelta(step.limits, pair[0].Limits, pair[1].Limits)
	}
	return step
}

// effectiveRequests is a container's requests as the API server accounts
// them against a quota: a name with a limit and no request takes the limit,
// the proxy's ephemeral-storage among them.
func effectiveRequests(rr corev1.ResourceRequirements) corev1.ResourceList {
	requests := corev1.ResourceList{}
	for name, quantity := range rr.Requests {
		requests[name] = quantity.DeepCopy()
	}
	for name, quantity := range rr.Limits {
		if _, set := requests[name]; !set {
			requests[name] = quantity.DeepCopy()
		}
	}
	return requests
}

// addResourceDelta adds to - from, per name, into total. A name present on
// one side only counts as zero on the other.
func addResourceDelta(total, from, to corev1.ResourceList) {
	for _, name := range sortedResourceNames(to) {
		delta := to[name].DeepCopy()
		if before, ok := from[name]; ok {
			delta.Sub(before)
		}
		sum := total[name]
		sum.Add(delta)
		total[name] = sum
	}
	for _, name := range sortedResourceNames(from) {
		if _, ok := to[name]; ok {
			continue
		}
		sum := total[name]
		sum.Sub(from[name])
		total[name] = sum
	}
}

// sizingScopeSelectsThePods reports whether one scope requirement of a
// ResourceQuota selects the three sized pods, as the quota admission plugin
// evaluates it against a pod. Every scope but PriorityClass has a fixed
// answer, because the three pods have one shape: a request on every sized
// container, so never BestEffort; no activeDeadlineSeconds, so never
// Terminating; no cross-namespace affinity term; and VolumeAttributesClass
// tracks claims, never a pod's cpu or memory. PriorityClass is read against
// classes, the priorityClassName values the live sized pods carry
// (sizedPodPriorityClasses): the controller sets none, but the API server's
// Priority admission fills in the cluster's globalDefault PriorityClass when
// one exists, and the quota plugin, which runs after it, matches the scope
// against the filled-in name, so a fixed answer would skip a quota scoped
// `In` that default, which binds the pods, and read a `DoesNotExist` one,
// which does not. The requirement selects the pods when it selects any class
// they carry; with no pod to read (classes empty) it is read as selecting
// them, as is a scope this operator does not know: the side that holds the
// tier rather than promotes past a quota that binds.
func sizingScopeSelectsThePods(req corev1.ScopedResourceSelectorRequirement, classes []string) bool {
	switch req.ScopeName {
	case corev1.ResourceQuotaScopeNotTerminating, corev1.ResourceQuotaScopeNotBestEffort:
		return true
	case corev1.ResourceQuotaScopeTerminating, corev1.ResourceQuotaScopeBestEffort,
		corev1.ResourceQuotaScopeCrossNamespacePodAffinity, corev1.ResourceQuotaScopeVolumeAttributesClass:
		return false
	case corev1.ResourceQuotaScopePriorityClass:
		if len(classes) == 0 {
			return true
		}
		return slices.ContainsFunc(classes, func(class string) bool { return priorityClassRequirementSelects(req, class) })
	default:
		return true
	}
}

// priorityClassRequirementSelects evaluates a PriorityClass scope requirement
// against one pod's priorityClassName the way the quota admission plugin
// does: a pod with no class has no PriorityClass label to match, so In and
// Exists miss it and NotIn and DoesNotExist match it; a pod with one matches
// In and NotIn on the value and Exists outright. An operator this operator
// does not know selects, the holding side.
func priorityClassRequirementSelects(req corev1.ScopedResourceSelectorRequirement, class string) bool {
	switch req.Operator {
	case corev1.ScopeSelectorOpIn:
		return class != "" && slices.Contains(req.Values, class)
	case corev1.ScopeSelectorOpNotIn:
		return !slices.Contains(req.Values, class)
	case corev1.ScopeSelectorOpExists:
		return class != ""
	case corev1.ScopeSelectorOpDoesNotExist:
		return class == ""
	default:
		return true
	}
}

// sizingQuotaScopesPriorityClass reports whether a ResourceQuota has a
// PriorityClass scope in spec.scopes or spec.scopeSelector, the one scope
// whose answer needs the live pods read.
func sizingQuotaScopesPriorityClass(quota *corev1.ResourceQuota) bool {
	if slices.Contains(quota.Spec.Scopes, corev1.ResourceQuotaScopePriorityClass) {
		return true
	}
	if quota.Spec.ScopeSelector == nil {
		return false
	}
	return slices.ContainsFunc(quota.Spec.ScopeSelector.MatchExpressions, func(req corev1.ScopedResourceSelectorRequirement) bool {
		return req.ScopeName == corev1.ResourceQuotaScopePriorityClass
	})
}

// sizingQuotaBindsThePods reports whether a ResourceQuota counts the three
// sized pods at all: an unscoped quota does, and a scoped one does when every
// entry of spec.scopes (each an Exists requirement, as the API server reads
// them) and every requirement of spec.scopeSelector selects them, a
// PriorityClass requirement read against classes (sizingScopeSelectsThePods).
// A quota that cannot count these pods cannot refuse them either, so the
// step-up check does not read it; the chart's quota preflight skips every
// scoped quota for the same reason, and this is the same call made per scope
// so a NotTerminating or NotBestEffort quota, which does bind them, still
// holds the tier.
func sizingQuotaBindsThePods(quota *corev1.ResourceQuota, classes []string) bool {
	for _, scope := range quota.Spec.Scopes {
		if !sizingScopeSelectsThePods(corev1.ScopedResourceSelectorRequirement{ScopeName: scope, Operator: corev1.ScopeSelectorOpExists}, classes) {
			return false
		}
	}
	if quota.Spec.ScopeSelector == nil {
		return true
	}
	for _, req := range quota.Spec.ScopeSelector.MatchExpressions {
		if !sizingScopeSelectsThePods(req, classes) {
			return false
		}
	}
	return true
}

// namespacePodPriorityClasses is the set of spec.priorityClassName values the
// live pods in the agent's namespace carry, "" for a pod with none, sorted;
// nil when the namespace has no pod. The pods read are the ones this operator
// created (commonLabels), admitted the way the three sized pods are; with
// none of those yet, any pod in the namespace, since a globalDefault
// PriorityClass is filled in on every pod created without one; with no pod
// at all there is nothing to read, and the caller takes a PriorityClass
// requirement as selecting the pods. Pods are not a cached kind here
// (manager_cache.go), so the lists go through the API reader, as the
// ResourceQuota list does, and only when a quota has a PriorityClass scope.
func (r *PlatformAgentReconciler) namespacePodPriorityClasses(ctx context.Context, agent *agentv1alpha1.PlatformAgent) ([]string, error) {
	for _, opts := range [][]client.ListOption{
		{client.InNamespace(agent.Namespace), client.MatchingLabels(commonLabels(agent))},
		{client.InNamespace(agent.Namespace)},
	} {
		pods := &corev1.PodList{}
		if err := r.a2aReader().List(ctx, pods, opts...); err != nil {
			return nil, fmt.Errorf("listing the namespace's pods for the quota scopes: %w", err)
		}
		if len(pods.Items) == 0 {
			continue
		}
		var classes []string
		for _, pod := range pods.Items {
			if !slices.Contains(classes, pod.Spec.PriorityClassName) {
				classes = append(classes, pod.Spec.PriorityClassName)
			}
		}
		slices.Sort(classes)
		return classes, nil
	}
	return nil, nil
}

// sizingDeploymentSettled and sizingStatefulSetSettled report whether one
// sized workload has settled at rendered: its roll is complete by the two-term
// test the status writers use (deploymentRollIncomplete, its StatefulSet
// twin), every replica the spec asks for is available (ready, for a
// StatefulSet), and the template carries rendered's figures for the sized
// container (sizedContainerCarries). The available term is what reads the
// proxy's Recreate window, where the old pod is gone and the new one not yet
// created: the generation is observed, no replica is old, and none is there.
func sizingDeploymentSettled(dep *appsv1.Deployment, container string, rendered corev1.ResourceRequirements) bool {
	return !bool(deploymentRollIncomplete(dep)) &&
		dep.Status.AvailableReplicas >= ptr.Deref(dep.Spec.Replicas, 1) &&
		sizedContainerCarries(dep.Spec.Template, container, rendered)
}

func sizingStatefulSetSettled(sts *appsv1.StatefulSet, container string, rendered corev1.ResourceRequirements) bool {
	return !bool(statefulSetRollIncomplete(sts)) &&
		sts.Status.ReadyReplicas >= ptr.Deref(sts.Spec.Replicas, 1) &&
		sizedContainerCarries(sts.Spec.Template, container, rendered)
}

// sizedContainerCarries reports whether the container named name in a pod
// template (its containers or, for the native sidecar, its init containers)
// carries at least rendered's figures on the names sizingSettledRequests and
// sizingSettledLimits list, or is not in the template at all, in which case
// nothing of it is charged. At least rather than equal, because GKE
// Autopilot's admission rewrites a workload's template (an ephemeral-storage
// request added, a request raised to a minimum) and only ever upward, so an
// equal test would never settle there. A template rendered at a lower tier
// falls short on a figure the step raises; one whose override has since been
// lowered reads as settled with the quota charged more than the step assumes,
// the safe side.
func sizedContainerCarries(template corev1.PodTemplateSpec, name string, rendered corev1.ResourceRequirements) bool {
	var found *corev1.Container
	for _, list := range [][]corev1.Container{template.Spec.Containers, template.Spec.InitContainers} {
		for i := range list {
			if list[i].Name == name {
				found = &list[i]
			}
		}
	}
	if found == nil {
		return true
	}
	carries := func(have, want corev1.ResourceList, names []corev1.ResourceName) bool {
		for _, resourceName := range names {
			wanted, moved := want[resourceName]
			if !moved {
				continue
			}
			got, set := have[resourceName]
			if !set || got.Cmp(wanted) < 0 {
				return false
			}
		}
		return true
	}
	return carries(found.Resources.Requests, rendered.Requests, sizingSettledRequests) &&
		carries(found.Resources.Limits, rendered.Limits, sizingSettledLimits)
}

// sizingTierSettled reports whether the three sized workloads have settled at
// tier: each carries tier's render, its roll is complete and every replica is
// available, so that every ResourceQuota's used total includes the pods the
// tier runs and nothing older. The step-up check measures each candidate step
// from the running tier against those totals, and the latch runs ahead of the
// pods: resolveSizingTier writes status.effectiveSizingTier before the
// workloads are re-rendered, the write re-enqueues the CR at once, and the
// proxy's Recreate rollout admits its new pod only after the old one has
// gone. A pass in that window that measured the next step from the latch
// would count the admitted step as already charged and could promote past
// the quota; it measures nothing until the pods have settled instead, and the
// workloads' status updates re-enqueue the CR when they have. The reads are
// cache hits (both kinds are Owns()'d). A workload that cannot be read has
// not settled; one that is not there yet has nothing charged, which at small
// is the tier settled (the first pass of a fresh install, before the steps
// after this one create the workloads) and above small is a latch the
// workloads have not caught up with (a pass that failed between the status
// write and the renders, or a controller restarted between them).
func (r *PlatformAgentReconciler) sizingTierSettled(ctx context.Context, agent *agentv1alpha1.PlatformAgent, tier sizingTier) bool {
	autopilot := r.clusterAutopilot()
	absent := func(err error) bool { return apierrors.IsNotFound(err) && tier == sizingTierSmall }
	proxy := &appsv1.Deployment{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: credentialProxyName(agent)}, proxy); err != nil {
		if !absent(err) {
			return false
		}
	} else if !sizingDeploymentSettled(proxy, credentialProxyContainerName, renderedCredentialProxyResourcesAt(agent.Spec.Deployment, autopilot, tier)) {
		return false
	}
	shell := &appsv1.StatefulSet{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: shellSandboxName(agent)}, shell); err != nil {
		if !absent(err) {
			return false
		}
	} else if !sizingStatefulSetSettled(shell, shellSandboxContainerName, renderedShellSandboxResourcesAt(agent, tier)) {
		return false
	}
	gatewayKey := client.ObjectKey{Namespace: agent.Namespace, Name: agent.Name + gatewayWorkloadNameSuffix}
	sidecar := renderedAgentAPIAuthResourcesAt(agent.Spec.Deployment, tier)
	if useStatefulSet(agent) {
		gateway := &appsv1.StatefulSet{}
		if err := r.Get(ctx, gatewayKey, gateway); err != nil {
			return absent(err)
		}
		return sizingStatefulSetSettled(gateway, agentAPIAuthContainerName, sidecar)
	}
	gateway := &appsv1.Deployment{}
	if err := r.Get(ctx, gatewayKey, gateway); err != nil {
		return absent(err)
	}
	return sizingDeploymentSettled(gateway, agentAPIAuthContainerName, sidecar)
}

// sizingQuotas lists the ResourceQuotas in the agent's namespace in name
// order, so the condition message is stable across passes. ResourceQuota is
// not a watched kind, so the list goes through the API reader, as the A2A
// session quota's reads do.
func (r *PlatformAgentReconciler) sizingQuotas(ctx context.Context, agent *agentv1alpha1.PlatformAgent) ([]corev1.ResourceQuota, error) {
	quotas := &corev1.ResourceQuotaList{}
	if err := r.a2aReader().List(ctx, quotas, client.InNamespace(agent.Namespace)); err != nil {
		return nil, fmt.Errorf("listing ResourceQuotas for the sizing step: %w", err)
	}
	slices.SortFunc(quotas.Items, func(a, b corev1.ResourceQuota) int { return strings.Compare(a.Name, b.Name) })
	return quotas.Items, nil
}

// sizingQuotaPriorityClasses is namespacePodPriorityClasses when a quota in
// quotas has a PriorityClass scope, the one scope whose answer needs the live
// pods read, and nil without a list otherwise.
func (r *PlatformAgentReconciler) sizingQuotaPriorityClasses(ctx context.Context, agent *agentv1alpha1.PlatformAgent, quotas []corev1.ResourceQuota) ([]string, error) {
	for i := range quotas {
		if sizingQuotaScopesPriorityClass(&quotas[i]) {
			return r.namespacePodPriorityClasses(ctx, agent)
		}
	}
	return nil, nil
}

// sizingQuotaShortfall is nil when every quota in quotas (the namespace's
// ResourceQuotas, in name order) that counts the sized pods has room for the
// step from tier from to tier to, or the first quota and dimension that is
// short, dimensions in sizingQuotaDimensions order so the condition message
// is stable across passes. The hard figure is the spec's, the used figure the
// quota controller's last accounting (zero on a quota it has not accounted
// yet, which admission then settles). A quota whose scopes cannot select the
// three pods (sizingQuotaBindsThePods, a PriorityClass scope read against
// classes) is not read: admission would never evaluate them against it, so
// it can neither refuse the step nor be raised to admit it.
func sizingQuotaShortfall(agent *agentv1alpha1.PlatformAgent, autopilot bool, quotas []corev1.ResourceQuota, classes []string, from, to sizingTier) *sizingShortfall {
	if len(quotas) == 0 {
		return nil
	}
	step := sizingStepBetween(agent, autopilot, from, to)
	for i := range quotas {
		quota := &quotas[i]
		if !sizingQuotaBindsThePods(quota, classes) {
			continue
		}
		for _, dim := range sizingQuotaDimensions {
			hard, constrained := quota.Spec.Hard[dim.key]
			if !constrained {
				continue
			}
			side := step.requests
			if dim.limits {
				side = step.limits
			}
			need, moved := side[dim.resource]
			if !moved || need.Sign() <= 0 {
				continue
			}
			free := hard.DeepCopy()
			if used, ok := quota.Status.Used[dim.key]; ok {
				free.Sub(used)
			}
			if free.Cmp(need) < 0 {
				return &sizingShortfall{quota: quota.Name, key: dim.key, free: free, need: need}
			}
		}
	}
	return nil
}

// sizingCappedConditionCurrent reports whether the SizingCappedByQuota
// condition on the CR already says msg, where "" means absent.
func sizingCappedConditionCurrent(agent *agentv1alpha1.PlatformAgent, msg string) bool {
	existing := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
	if msg == "" {
		return existing == nil
	}
	return existing != nil && existing.Status == metav1.ConditionTrue &&
		existing.Reason == sizingCappedByQuotaReason && existing.Message == msg
}

// setSizingCappedByQuotaCondition writes the SizingCappedByQuota condition,
// or removes it when msg is "". The caller does the API write.
func setSizingCappedByQuotaCondition(agent *agentv1alpha1.PlatformAgent, msg string, now metav1.Time) {
	if msg == "" {
		meta.RemoveStatusCondition(&agent.Status.Conditions, sizingCappedByQuotaConditionType)
		return
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type:               sizingCappedByQuotaConditionType,
		Status:             metav1.ConditionTrue,
		Reason:             sizingCappedByQuotaReason,
		Message:            msg,
		ObservedGeneration: agent.Generation,
		LastTransitionTime: now,
	})
}

// sizingCappedConditionMessage is the message the SizingCappedByQuota
// condition on the CR carries, "" when it is absent or not the condition this
// operator writes.
func sizingCappedConditionMessage(agent *agentv1alpha1.PlatformAgent) string {
	existing := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
	if existing == nil || existing.Status != metav1.ConditionTrue || existing.Reason != sizingCappedByQuotaReason {
		return ""
	}
	return existing.Message
}

// resolveSizingTier decides the tier this pass renders and writes it to
// status.effectiveSizingTier, with the SizingCappedByQuota condition, before
// any builder reads renderedSizingTier, so the status is the record of what
// the pods run even when a later step of the pass fails. An explicit profile
// is its tier, applied as written. Under auto the tier is the latched status
// tier, whatever profile wrote it (an explicit fleet switched to auto
// continues at fleet; small first, then auto, restarts the latch), raised to
// the highest tier, at or under what the cluster count earns, whose step from
// the running tier every ResourceQuota that counts the pods has room for; a
// quota short on every step keeps the running tier, and any refused step sets
// the condition. With a quota in the namespace, a step is measured only once
// the running tier's workloads have settled (sizingTierSettled), since the
// used totals it is measured against do not charge a step whose pods
// admission has not yet created; until then the latch and the condition stay
// where they are, with no write and no event. A count that falls leaves the latch where it is and clears the
// condition, since it no longer earns the step; a pass with no reading (the
// gauges absent before the first poll, or cleared after two polls that read
// no gateway) leaves the latch and the condition both where they are, so a
// gateway outage neither clears a cap the quota still imposes nor records it
// again when the reading returns. The one no-reading state that is not an
// outage, the event watcher switched off by spec, clears the condition
// instead, since the gauges stay absent for as long as it is off and nothing
// could lift the cap. The write is gated on a change, as the
// other status writers' are, because the PlatformAgent watch has no predicate
// and every write re-enqueues the object; under a served CRD that prunes
// status.effectiveSizingTier the field reads "" after every write, so the
// gate treats "" as settled while the echo record (prunedSizingTier) is fresh,
// as the Ready writer does for status.usage, rather than writing and logging
// on every pass.
func (r *PlatformAgentReconciler) resolveSizingTier(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	profile := sizingProfileOf(agent.Spec.Deployment)
	latched, _ := parseSizingTier(agent.Status.EffectiveSizingTier)
	tier := specSizingTier(agent.Spec.Deployment)
	capped := ""
	var short *sizingShortfall
	var count int64
	if profile == sizingProfileAuto {
		tier = latched
		if observed, ok := observedClusterCount(agent); ok {
			count = observed
			if earned := sizingTierForClusterCount(count); earned > tier {
				quotas, err := r.sizingQuotas(ctx, agent)
				if err != nil {
					return err
				}
				if len(quotas) > 0 && !r.sizingTierSettled(ctx, agent, tier) {
					// The running tier's pods have not all been admitted yet
					// (the pass after a promotion, while the proxy recreates),
					// so the quotas' used totals do not yet charge the step
					// just taken and a step measured from the latch would
					// count it as free. Nothing is measured: the cap stands
					// as written, and the workloads' status updates
					// re-enqueue the CR when the pods have settled. With no
					// quota there is nothing to measure against, and the
					// earned tier is taken as before.
					capped = sizingCappedConditionMessage(agent)
				} else {
					classes, err := r.sizingQuotaPriorityClasses(ctx, agent, quotas)
					if err != nil {
						return err
					}
					// The earned tier first, then each tier between it and
					// the running one, every step measured from the running
					// tier against the same used totals: the highest step the
					// quotas admit is taken, so a count that earns fleet with
					// room for medium alone reaches medium rather than
					// waiting at small for the whole fleet step. The cap
					// reports the lowest step refused, the one raising the
					// quota by the least relieves.
					running := tier
					for candidate := earned; candidate > running; candidate-- {
						shortfall := sizingQuotaShortfall(agent, r.clusterAutopilot(), quotas, classes, running, candidate)
						if shortfall == nil {
							tier = candidate
							break
						}
						short = shortfall
					}
					if short != nil {
						capped = fmt.Sprintf(sizingCappedByQuotaMessageFormat, earned, short.quota, short.key, tier, tier)
					}
				}
			}
		} else if eventWatcherEnabled(agent) {
			// No reading with the watcher on is an outage or the time before
			// the first poll: the cap stands as written. With the watcher off
			// by spec the poller clears the gauges for as long as it stays
			// off, and a cap carried through that would outlive the quota
			// being raised; auto cannot promote without a reading, so there
			// is nothing to cap, and the first reading after the watcher is
			// switched back on checks the quota again.
			capped = sizingCappedConditionMessage(agent)
		}
	}
	tierSettled := agent.Status.EffectiveSizingTier == tier.String() ||
		(agent.Status.EffectiveSizingTier == "" && r.sizingTierPruned(agent))
	if tierSettled && sizingCappedConditionCurrent(agent, capped) {
		return nil
	}
	log := logf.FromContext(ctx)
	if previous := agent.Status.EffectiveSizingTier; previous != tier.String() {
		log.Info("sizing tier resolved", "name", agent.Name, "namespace", agent.Namespace, "from", previous, "to", tier.String(), "profile", profile)
		// The first write, on every CR after the upgrade that brought the
		// field, is not a change worth an event.
		if previous != "" {
			r.recordEvent(agent, corev1.EventTypeNormal, sizingTierChangedReason, fmt.Sprintf(sizingTierChangedMessageFormat, previous, tier, profile))
		}
	}
	if short != nil && !sizingCappedConditionCurrent(agent, capped) {
		log.Info("sizing tier capped by ResourceQuota", "name", agent.Name, "namespace", agent.Namespace,
			"quota", short.quota, "key", short.key, "free", short.free.String(), "adds", short.need.String(), "clusters", count)
		r.recordEvent(agent, corev1.EventTypeWarning, sizingCappedByQuotaEventReason,
			fmt.Sprintf(sizingCappedByQuotaEventFormat, capped, count, short.free.String(), short.key, short.need.String()))
	}
	agent.Status.EffectiveSizingTier = tier.String()
	setSizingCappedByQuotaCondition(agent, capped, metav1.Now())
	if err := r.Status().Update(ctx, agent); err != nil {
		return err
	}
	r.noteSizingTierEcho(ctx, agent)
	return nil
}

// sizingTierPruned reports whether the served CRD has been seen to drop
// status.effectiveSizingTier for this CR within the last
// usageStatusReprobeInterval. A stale record reads false, which is what makes
// the next pass probe the field again.
func (r *PlatformAgentReconciler) sizingTierPruned(agent *agentv1alpha1.PlatformAgent) bool {
	recorded, pruned := r.prunedSizingTier.Load(client.ObjectKeyFromObject(agent))
	if !pruned {
		return false
	}
	return time.Since(recorded.(time.Time)) < usageStatusReprobeInterval
}

// noteSizingTierEcho reads the server's copy of status.effectiveSizingTier
// back after resolveSizingTier's write, which always carries a tier.
// controller-runtime decodes the response into agent through a decoder that
// zeroes the target first, so a field the served CRD does not know comes back
// "" although a tier was just written. An echo without the field is recorded
// with the time, and said once per record; an echo with it clears the record.
func (r *PlatformAgentReconciler) noteSizingTierEcho(ctx context.Context, agent *agentv1alpha1.PlatformAgent) {
	key := client.ObjectKeyFromObject(agent)
	if agent.Status.EffectiveSizingTier != "" {
		r.prunedSizingTier.Delete(key)
		return
	}
	fresh := r.sizingTierPruned(agent)
	r.prunedSizingTier.Store(key, time.Now())
	if !fresh {
		logf.FromContext(ctx).Info("the served CRD has no status.effectiveSizingTier; apply this release's CRD to get it, which is probed again after the interval",
			"platformagent", key.String(), "reprobeAfter", usageStatusReprobeInterval.String())
	}
}
