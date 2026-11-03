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

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
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
	// The figures are the fleet-size bands the sizing guide uses: a fleet of
	// ten clusters outgrows the small broker's two-command memory budget, and
	// twenty-five is where the event watcher's informers outgrow a 4Gi
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
	sizingCappedByQuotaConditionType = "SizingCappedByQuota"
	sizingCappedByQuotaReason        = "ResourceQuotaShort"
	// sizingCappedByQuotaMessageFormat takes the cluster count, the tier
	// earned, the quota's name, the free quantity, the quota key, the running
	// tier, the quantity the step adds, and the running tier again.
	sizingCappedByQuotaMessageFormat = "spec.deployment.sizingProfile is auto and the %d clusters the operator counts earn the %s tier, " +
		"but ResourceQuota %s has %s of %s free and the step up from %s adds %s; the %s tier stays until the quota is raised " +
		"or sizingProfile names the tier, which the operator applies without this check"
	// sizingTierChangedReason is the Normal event a tier change records, and
	// sizingCappedByQuotaEventReason the Warning the first capped pass records.
	sizingTierChangedReason        = "SizingTierChanged"
	sizingCappedByQuotaEventReason = "SizingCappedByQuota"
	sizingTierChangedMessageFormat = "sizing tier %s -> %s (spec.deployment.sizingProfile %s)"
)

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
// three containers a profile sizes, as the CR's overrides leave them. Each of
// the three pods is replaced rather than surged on a template change (the
// proxy's and the gateway's Recreate Deployments, the sandbox StatefulSet's
// ordered roll), so what the quota has to admit is the difference, not a
// second pod; the RollingUpdate shape replicasAndStrategyFor keeps for the
// multi-replica redesign is reached by no caller.
type sizingStep struct {
	requests corev1.ResourceList
	limits   corev1.ResourceList
}

// sizingStepBetween is the sizingStep from tier from to tier to for agent.
func sizingStepBetween(agent *agentv1alpha1.PlatformAgent, autopilot bool, from, to sizingTier) sizingStep {
	deployment := agent.Spec.Deployment
	pairs := [][2]corev1.ResourceRequirements{
		{resolveCredentialProxyResourcesFor(deployment, autopilot, from), resolveCredentialProxyResourcesFor(deployment, autopilot, to)},
		{resolveAgentAPIAuthResources(deployment, from), resolveAgentAPIAuthResources(deployment, to)},
		{resolveShellSandboxResources(agent, from), resolveShellSandboxResources(agent, to)},
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

// sizingQuotaShortfall is "" when every ResourceQuota in the agent's
// namespace has room for the step from tier from to tier to, or the
// SizingCappedByQuota message naming the first quota and dimension that is
// short, quotas in name order so the message is stable across passes. The
// hard figure is the spec's, the used figure the quota controller's last
// accounting (zero on a quota it has not accounted yet, which admission then
// settles). A quota with scopes is read as if it applied: the operator does
// not evaluate scope selectors, so a scoped quota that is short holds the
// tier, and the message names it for the person who knows whether it binds.
// ResourceQuota is not a watched kind, so the list goes through the API
// reader, as the A2A session quota's reads do.
func (r *PlatformAgentReconciler) sizingQuotaShortfall(ctx context.Context, agent *agentv1alpha1.PlatformAgent, count int64, from, to sizingTier) (string, error) {
	quotas := &corev1.ResourceQuotaList{}
	if err := r.a2aReader().List(ctx, quotas, client.InNamespace(agent.Namespace)); err != nil {
		return "", fmt.Errorf("listing ResourceQuotas for the sizing step to %s: %w", to, err)
	}
	if len(quotas.Items) == 0 {
		return "", nil
	}
	step := sizingStepBetween(agent, r.clusterAutopilot(), from, to)
	slices.SortFunc(quotas.Items, func(a, b corev1.ResourceQuota) int { return strings.Compare(a.Name, b.Name) })
	for _, quota := range quotas.Items {
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
				return fmt.Sprintf(sizingCappedByQuotaMessageFormat, count, to, quota.Name, free.String(), dim.key, from, need.String(), from), nil
			}
		}
	}
	return "", nil
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

// resolveSizingTier decides the tier this pass renders and writes it to
// status.effectiveSizingTier, with the SizingCappedByQuota condition, before
// any builder reads renderedSizingTier, so the status is the record of what
// the pods run even when a later step of the pass fails. An explicit profile
// is its tier, applied as written. Under auto the tier is the latched status
// tier, raised to what the cluster count earns when every ResourceQuota in
// the namespace has room for the step; a count that falls, or that the
// operator has no reading of, leaves the latch where it is, and a short quota
// keeps the running tier and sets the condition. The write is gated on a
// change, as the other status writers' are, because the PlatformAgent watch
// has no predicate and every write re-enqueues the object.
func (r *PlatformAgentReconciler) resolveSizingTier(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	profile := sizingProfileOf(agent.Spec.Deployment)
	latched, _ := parseSizingTier(agent.Status.EffectiveSizingTier)
	tier := specSizingTier(agent.Spec.Deployment)
	capped := ""
	if profile == sizingProfileAuto {
		tier = latched
		if count, ok := observedClusterCount(agent); ok {
			if earned := sizingTierForClusterCount(count); earned > tier {
				shortfall, err := r.sizingQuotaShortfall(ctx, agent, count, tier, earned)
				if err != nil {
					return err
				}
				if shortfall == "" {
					tier = earned
				} else {
					capped = shortfall
				}
			}
		}
	}
	if agent.Status.EffectiveSizingTier == tier.String() && sizingCappedConditionCurrent(agent, capped) {
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
	if capped != "" && !sizingCappedConditionCurrent(agent, capped) {
		log.Info("sizing tier capped by ResourceQuota", "name", agent.Name, "namespace", agent.Namespace, "message", capped)
		r.recordEvent(agent, corev1.EventTypeWarning, sizingCappedByQuotaEventReason, capped)
	}
	agent.Status.EffectiveSizingTier = tier.String()
	setSizingCappedByQuotaCondition(agent, capped, metav1.Now())
	return r.Status().Update(ctx, agent)
}
