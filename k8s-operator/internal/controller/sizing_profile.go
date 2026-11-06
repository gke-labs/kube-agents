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
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
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
	// than the namespace's admission has room for: a ResourceQuota short of
	// the step (sizingCappedByQuotaReason), or a LimitRange whose bound the
	// earned row crosses (sizingCappedByLimitRangeReason). Written only in that state, on the
	// ReplicasLimited pattern, by resolveSizingTier alone; the Ready and
	// Degraded writers carry it through as they read it. The message names
	// the quota and the dimension, or the LimitRange, the bound and the
	// container, because the person reading `kubectl describe` decides
	// between raising the object and naming a tier. It carries no
	// quantities: the status write and the Warning event are gated on the
	// message, and the quota's free figure moves with every pod the
	// namespace admits, so a figure in the message would write status and
	// record a Warning on every session pod a busy install runs. The figures
	// go in the event and the log line, recorded when the message changes.
	sizingCappedByQuotaConditionType = "SizingCappedByQuota"
	sizingCappedByQuotaReason        = "ResourceQuotaShort"
	sizingCappedByLimitRangeReason   = "LimitRangeShort"
	// sizingCappedByQuotaMessageFormat takes the tier earned, the quota's
	// name, the quota key, the running tier the refused step was measured
	// from, and the tier that stays: the running tier again when every step
	// was refused, the admitted candidate when a lower step was taken, so
	// the figures the event and the log carry beside it (the refused step's
	// free and need) belong to the step the message names.
	sizingCappedByQuotaMessageFormat = "spec.deployment.sizingProfile is auto and the cluster count earns the %s tier, " +
		"but ResourceQuota %s has no room on %s for the step up from %s; the %s tier stays until the quota is raised " +
		"or sizingProfile names the tier, which the operator applies without this check"
	// sizingCappedByLimitRangeMessageFormat takes the tier earned, the
	// LimitRange's name, the item's scope (sizingLimitRangeScopeContainer or
	// Pod), the bound (sizingLimitRangeMax, Min or Ratio), the resource
	// name, what crosses it (sizingLimitRangeContainerSubjectFormat or
	// PodSubjectFormat on the sized container), the running tier the
	// refused step was measured from, and the tier that stays, as the quota
	// format does.
	sizingCappedByLimitRangeMessageFormat = "spec.deployment.sizingProfile is auto and the cluster count earns the %s tier, " +
		"but LimitRange %s sets a per-%s %s on %s that the %s crosses at the step up from %s; " +
		"the %s tier stays until the LimitRange admits the row or sizingProfile names the tier, " +
		"which the operator applies without this check"
	// The two item scopes the step-up check reads, as the LimitRange spec's
	// type field names them in lower case, and what each is read against:
	// a Container item against the sized container, a Pod item against the
	// pod the sized container runs in.
	sizingLimitRangeScopeContainer         = "container"
	sizingLimitRangeScopePod               = "pod"
	sizingLimitRangeContainerSubjectFormat = "%s container"
	sizingLimitRangePodSubjectFormat       = "%s container's pod"
	// sizingLimitRangeRatioFigureFormat prints the limit-to-request ratio a
	// render carries, for the event and the log.
	sizingLimitRangeRatioFigureFormat = "%.3g"
	// sizingStepRefusedMessageFormat is the SizingCappedByQuota message the
	// back-out writes (sizingRefusedStep): a pod of the latched tier that
	// admission refused on a ResourceQuota for a key the step up to that
	// tier moved, whether the quota check admitted the step and the
	// namespace admitted another pod between the measurement and the
	// Recreate, or an explicit profile set the tier before auto did. It
	// takes the workload whose pod was refused, the tier stepped back from,
	// the quota admission named, admission's own message, and the tier that
	// stays. Admission's message is part of the condition because it is one
	// of the two things that tell one refusal from the next (the other is
	// the figure it asked for, sizingRefusalIsTheSteps): the pod name it
	// carries is the ReplicaSet's, so the same refusal read again on a
	// later pass is not stepped back from twice, and a refusal of the lower
	// row's pod, a new ReplicaSet's, is.
	sizingStepRefusedMessageFormat = "spec.deployment.sizingProfile is auto and admission refused the pod %s recreates at the %s tier, " +
		"ResourceQuota %s having no room for it (%s); " +
		"the %s tier stays until the quota is raised or sizingProfile names the tier, which the operator applies without this check"
	// resourceQuotaExceededPhrase opens the quota admission plugin's refusal
	// ("exceeded quota: <name>, requested: <key>=<quantity>,..., used: ...,
	// limited: ..."), the name running to the next
	// resourceQuotaExceededNameEnd; the requested list, the keys the pod
	// crossed with the quantities the pod asked for on them, runs from
	// resourceQuotaRequestedPhrase to resourceQuotaUsedPhrase, its pairs
	// separated by resourceQuotaListSeparator and each key from its quantity
	// by resourceQuotaPairSeparator.
	resourceQuotaExceededPhrase  = "exceeded quota: "
	resourceQuotaExceededNameEnd = ","
	resourceQuotaRequestedPhrase = "requested: "
	resourceQuotaUsedPhrase      = ", used: "
	resourceQuotaListSeparator   = ","
	resourceQuotaPairSeparator   = "="
	// sizingStepBackHoldInterval is how long after a step back
	// (sizingRefusedStep) the gate measures no step up, so that a namespace
	// whose slack sits near a step's size, with pods landing in the
	// broker's drain window, cycles between two rows at most once per
	// interval rather than on every pass the lower row settles. The record
	// is the operator's own (steppedBackAt), kept the way the pruned-field
	// echo is: a restart forgets it and measures on the next settled pass,
	// at worst one cycle early.
	sizingStepBackHoldInterval = 10 * time.Minute
	// sizingCappedByQuotaEventFormat is the Warning event's text: the
	// condition message, then the figures as they stood when it was written.
	// It takes the message, the cluster count, the free quantity, the quota
	// key and the quantity the step adds.
	sizingCappedByQuotaEventFormat = "%s (at %d clusters: %s of %s free, the step adds %s)"
	// sizingCappedByLimitRangeEventFormat is the LimitRange cap's Warning
	// event: the message, the cluster count, the bound, its quantity and the
	// container's figure at the refused row.
	sizingCappedByLimitRangeEventFormat = "%s (at %d clusters: the %s is %s, the row's figure %s)"
	// sizingLimitRangeMax, sizingLimitRangeMin and sizingLimitRangeRatio
	// name the three bounds of a LimitRange item the step-up check reads, as
	// the LimitRange spec spells them.
	sizingLimitRangeMax   = "max"
	sizingLimitRangeMin   = "min"
	sizingLimitRangeRatio = "maxLimitRequestRatio"
	// sizingTierChangedReason is the Normal event a tier change records, and
	// sizingCappedByQuotaEventReason and sizingCappedByLimitRangeEventReason
	// the Warning the first capped pass records.
	sizingTierChangedReason             = "SizingTierChanged"
	sizingCappedByQuotaEventReason      = "SizingCappedByQuota"
	sizingCappedByLimitRangeEventReason = "SizingCappedByLimitRange"
	sizingTierChangedMessageFormat      = "sizing tier %s -> %s (spec.deployment.sizingProfile %s)"

	// The three sized workloads the step-up check reads back: the gateway
	// workload (a Deployment, or a StatefulSet under useStatefulSet) named
	// with this suffix carries the agent-api-auth sidecar, the credential
	// proxy Deployment (credentialProxyName) the broker, and the shell
	// StatefulSet (shellSandboxName) the shell container
	// (shellSandboxContainerName).
	gatewayWorkloadNameSuffix = "-gateway"

	// How the hold names the workload it waits on, in the log line the pass
	// that measures no step writes (sizingTierUnsettledWorkload).
	sizingUnsettledDeploymentFormat  = "Deployment/%s"
	sizingUnsettledStatefulSetFormat = "StatefulSet/%s"

	// The generateName suffix of the Pod sizingAdmittedPriorityClass asks
	// admission about, after the proxy Deployment's name; the Pod is a dry
	// run and never exists, the name is what an audit log shows.
	sizingDryRunPodSuffix = "-sizing-dry-run-"

	// The annotation GKE Autopilot's admission writes on a pod template whose
	// resources it changed: a JSON record with the containers' requests and
	// limits as they came in (input) and as it left them (output). It is the
	// one record of what the operator wrote before the rewrite, and the clamp
	// readings below (autopilotClampedLimits) take the clamp from it and from
	// nothing else; a template without it is read as written.
	autopilotResourceAdjustmentAnnotation = "autopilot.gke.io/resource-adjustment"
)

// autopilotAdjustment is the autopilotResourceAdjustmentAnnotation record:
// each side lists the pod template's containers and init containers by name
// with the requests and limits they carried on that side, a key absent where
// nothing was set. Keys this operator does not read (modified, and any the
// record grows) are ignored.
type autopilotAdjustment struct {
	Input  autopilotAdjustmentSide `json:"input"`
	Output autopilotAdjustmentSide `json:"output"`
}

type autopilotAdjustmentSide struct {
	Containers     []autopilotAdjustedContainer `json:"containers"`
	InitContainers []autopilotAdjustedContainer `json:"initContainers"`
}

type autopilotAdjustedContainer struct {
	Name     string              `json:"name"`
	Requests corev1.ResourceList `json:"requests"`
	Limits   corev1.ResourceList `json:"limits"`
}

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

// sizingBound is one LimitRange bound a candidate tier's render crosses: the
// LimitRange, the item's scope (sizingLimitRangeScopeContainer or Pod), the
// bound (sizingLimitRangeMax, Min or Ratio), the resource name, the sized
// container, the bound's quantity and the figure the render carries against
// it (a quantity, or a ratio printed with sizingLimitRangeRatioFigureFormat).
// As with sizingShortfall, the condition message is built from the names
// alone; the quantities reach the event and the log.
type sizingBound struct {
	limitRange string
	scope      string
	bound      string
	key        corev1.ResourceName
	container  string
	quantity   resource.Quantity
	figure     string
}

// subject is what the condition message says crosses the bound: the sized
// container for a Container item, its pod for a Pod item.
func (b *sizingBound) subject() string {
	if b.scope == sizingLimitRangeScopePod {
		return fmt.Sprintf(sizingLimitRangePodSubjectFormat, b.container)
	}
	return fmt.Sprintf(sizingLimitRangeContainerSubjectFormat, b.container)
}

// sizingRefusal is a sized Deployment whose pod admission refused on a
// ResourceQuota, read off its ReplicaFailure condition
// (sizingRefusedStep): the workload (kind and name), the quota the message
// names, and the message as the condition carries it.
type sizingRefusal struct {
	workload string
	quota    string
	message  string
}

// sizingCap is the SizingCappedByQuota condition's content: the reason
// (sizingCappedByQuotaReason or sizingCappedByLimitRangeReason) and the
// message. The zero value is the condition absent.
type sizingCap struct {
	reason  string
	message string
}

// sizedRender is one of the three sized containers with its resources as the
// reconciler renders them at a tier (renderedSizedContainersAt).
type sizedRender struct {
	container string
	resources corev1.ResourceRequirements
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

// The step's limits side is read through a clamped flag: whether the
// cluster's admission lowers every container's limit to its request, as GKE
// Autopilot's does without Pod bursting. It is a property of the cluster, not
// of a container or a name: the pods a step recreates are all clamped the
// same way, so a limits.* quota is charged the request on every name, and the
// step's limits side is the requests side's movement rather than the rows'
// limits movement (the sidecar's 2Gi to 8Gi memory limit over a 384Mi request
// moves the quota by nothing; the broker's 2 to 8 CPU limit over a 2 to 4 CPU
// request moves it by 2, though its medium row's equal 2/2 pair would not
// show the clamp on its own). It is read once per measuring pass off
// Autopilot's own record on the running templates (sizingRunningLimitsClamped),
// and false, the rows' limits as written, wherever that record shows no
// clamp: a Standard cluster, and Autopilot with bursting, where limits
// survive.

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
// each side as the reconciler renders that tier, the limits side read through
// clamped (sizingRunningLimitsClamped).
func sizingStepBetween(agent *agentv1alpha1.PlatformAgent, autopilot bool, clamped bool, from, to sizingTier) sizingStep {
	before, after := renderedSizedContainersAt(agent, autopilot, from), renderedSizedContainersAt(agent, autopilot, to)
	step := sizingStep{requests: corev1.ResourceList{}, limits: corev1.ResourceList{}}
	for i := range before {
		addRenderStep(&step, before[i].resources, after[i].resources, clamped)
	}
	return step
}

// sizedContainerStepBetween is sizingStepBetween for the one sized container
// named name (renderedSizedContainersAt's names): what the step from from to
// to moves on the pod that container runs in, since the other containers of
// that pod keep their figures. The zero step for a name that is not sized.
func sizedContainerStepBetween(agent *agentv1alpha1.PlatformAgent, autopilot bool, clamped bool, name string, from, to sizingTier) sizingStep {
	before, after := renderedSizedContainersAt(agent, autopilot, from), renderedSizedContainersAt(agent, autopilot, to)
	step := sizingStep{requests: corev1.ResourceList{}, limits: corev1.ResourceList{}}
	for i := range before {
		if before[i].container == name {
			addRenderStep(&step, before[i].resources, after[i].resources, clamped)
		}
	}
	return step
}

// addRenderStep adds to step what one container's render moves from before
// to after: the requests as admission accounts them (effectiveRequests) and
// the limits through clamped.
func addRenderStep(step *sizingStep, before, after corev1.ResourceRequirements, clamped bool) {
	fromRequests, toRequests := effectiveRequests(before), effectiveRequests(after)
	addResourceDelta(step.requests, fromRequests, toRequests)
	addResourceDelta(step.limits,
		clampedLimits(before.Limits, fromRequests, clamped),
		clampedLimits(after.Limits, toRequests, clamped))
}

// renderedSizedContainersAt is the three sized containers as the reconciler
// renders them at tier, in one order: the broker, the sidecar, the shell.
func renderedSizedContainersAt(agent *agentv1alpha1.PlatformAgent, autopilot bool, tier sizingTier) []sizedRender {
	deployment := agent.Spec.Deployment
	return []sizedRender{
		{credentialProxyContainerName, renderedCredentialProxyResourcesAt(deployment, autopilot, tier)},
		{agentAPIAuthContainerName, renderedAgentAPIAuthResourcesAt(deployment, tier)},
		{shellSandboxContainerName, renderedShellSandboxResourcesAt(agent, tier)},
	}
}

// clampedLimits is limits with every name replaced by its figure in requests
// when clamped, the limit Autopilot's admission sets on each name without
// bursting; limits as given otherwise.
func clampedLimits(limits, requests corev1.ResourceList, clamped bool) corev1.ResourceList {
	if !clamped {
		return limits
	}
	out := corev1.ResourceList{}
	for name, quantity := range limits {
		if request, ok := requests[name]; ok {
			quantity = request
		}
		out[name] = quantity.DeepCopy()
	}
	return out
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
// classes, the priorityClassName admission gives the pod a step recreates
// (sizingAdmittedPriorityClass): the controller sets none, but the API
// server's Priority admission fills in the cluster's globalDefault
// PriorityClass when one exists, and the quota plugin, which runs after it,
// matches the scope against the filled-in name, so a fixed answer would skip
// a quota scoped `In` that default, which binds the pods, and read a
// `DoesNotExist` one, which does not. The requirement selects the pods when
// it selects any class in the set. resolveSizingTier measures no step while
// admission has given no answer (sizingQuotaPriorityClasses), so an empty set
// reaches this only from a caller that did not ask; it is read as selecting
// the pods, as is a scope this operator does not know: the side that holds
// the tier rather than promotes past a quota that binds.
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

// sizingAdmittedPriorityClass is the spec.priorityClassName the pod a step
// recreates will carry, asked of admission itself: a server-side dry-run
// create (client.DryRunAll) of a Pod built from the live credential-proxy
// Deployment's template, which the API server runs through its admission
// chain and returns as mutated without persisting. The Priority plugin in
// that chain fills in the cluster's globalDefault PriorityClass when one
// exists, so the answer is the name the quota plugin will match the real
// replacement's scopes against: "" on a cluster with no default, the
// default's name on one that has it, whenever it was created. Reading the
// classes off the running pods could not see a default created after them,
// since admission fills a class in at creation and never rewrites a running
// pod, and the pod a Recreate rollout makes is a new one. The template is the
// proxy's own, so the Pod carries its labels, service account and requests:
// what the namespace's webhooks and quotas would see. The second value is
// false when admission gave no answer: no proxy Deployment (not reached from
// resolveSizingTier, which holds on a missing workload before asking), or a
// dry run refused. A refusal says nothing about the class, and it need not
// come from a quota the step would trip: the dry run adds a whole pod at the
// running tier where the step adds one pod's difference, so a pods count
// quota at its cap, or a requests.cpu quota with less than the broker's
// request free, refuses the dry run and would admit the step; a webhook that
// declines dry runs or a verb the ClusterRole lacks refuses it too. The
// caller therefore measures no step on a refusal (resolveSizingTier holds, as
// it does for a workload that has not settled) rather than reading a
// PriorityClass requirement as binding, which would cap the tier on a quota
// that may not count the pods and name it as the one to raise. The refusal,
// with the quota or webhook it names, is in the log line here on every pass
// it holds. The Pod goes through the writing client, as the writes do;
// nothing is cached or created.
func (r *PlatformAgentReconciler) sizingAdmittedPriorityClass(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, bool) {
	log := logf.FromContext(ctx)
	proxy := &appsv1.Deployment{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: credentialProxyName(agent)}, proxy); err != nil {
		log.Info("no credential-proxy Deployment to ask admission about the sizing pods' priority class; the sizing step is held",
			"name", agent.Name, "namespace", agent.Namespace, "error", err.Error())
		return "", false
	}
	template := proxy.Spec.Template
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:    agent.Namespace,
			GenerateName: credentialProxyName(agent) + sizingDryRunPodSuffix,
			Labels:       template.Labels,
			Annotations:  template.Annotations,
		},
		Spec: *template.Spec.DeepCopy(),
	}
	if err := r.Create(ctx, pod, client.DryRunAll); err != nil {
		log.Info("admission refused the dry-run Pod that asks the sizing pods' priority class; the sizing step is held until it answers",
			"name", agent.Name, "namespace", agent.Namespace, "error", err.Error())
		return "", false
	}
	return pod.Spec.PriorityClassName, true
}

// sizingDeploymentSettled and sizingStatefulSetSettled report whether one
// sized workload has settled at rendered: its roll is complete by the two-term
// test the status writers use (deploymentRollIncomplete, its StatefulSet
// twin), every replica is available (ready, for a StatefulSet), both the
// count the spec asks for and the count the operator renders for it
// (replicas), and the template carries rendered's figures for the sized
// container (sizedContainerCarries). The available term is what reads the
// proxy's Recreate window, where the old pod is gone and the new one not yet
// created: the generation is observed, no replica is old, and none is there.
// The rendered count is what reads a workload scaled to zero by hand: its
// spec asks for nothing and its roll is complete, but the quota's used total
// charges no pod of it while the step is measured from its row, the same
// under-count as a workload that is not there, and the operator's next apply
// puts the replica back, so it has not settled until that replica is
// available. The gateway under spec.deployment.scaleToZero renders zero and
// settles at zero.
func sizingDeploymentSettled(dep *appsv1.Deployment, container string, rendered corev1.ResourceRequirements, replicas int32) bool {
	return !bool(deploymentRollIncomplete(dep)) &&
		dep.Status.AvailableReplicas >= max(ptr.Deref(dep.Spec.Replicas, 1), replicas) &&
		sizedContainerCarries(dep.Spec.Template, container, rendered)
}

func sizingStatefulSetSettled(sts *appsv1.StatefulSet, container string, rendered corev1.ResourceRequirements, replicas int32) bool {
	return !bool(statefulSetRollIncomplete(sts)) &&
		sts.Status.ReadyReplicas >= max(ptr.Deref(sts.Spec.Replicas, 1), replicas) &&
		sizedContainerCarries(sts.Spec.Template, container, rendered)
}

// sizedContainerCarries reports whether the container named name in a pod
// template (its containers or, for the native sidecar, its init containers)
// carries at least rendered's figures on the names sizingSettledRequests and
// sizingSettledLimits list, or is not in the template at all, in which case
// nothing of it is charged. At least rather than equal, because GKE
// Autopilot's admission rewrites a workload's template (an ephemeral-storage
// request added, a request raised to a minimum), and on the requests side
// only ever upward, so an equal test would never settle there.
//
// The limits side is different on Autopilot: without Pod bursting its
// admission lowers every container's limit to its request, so a limit a row
// sets above its request (the broker's 2 CPU over 1 on Standard, the
// sidecar's 2Gi over 384Mi) never reaches the template, and a check that
// wanted the row's limit there would hold the tier for good. The clamp is
// taken from Autopilot's own record of the rewrite, the
// autopilotResourceAdjustmentAnnotation on the template
// (autopilotClampedLimits): on a name the record shows lowered to its request,
// and which the row also requests, the requests side, already compared,
// stands for the limit. The quota is charged the one figure on both sides
// there, a template whose requests carry the tier's runs what the tier runs,
// and a lower tier's template, whose requests fall short on a figure the step
// raises, still does not settle; two rows that differ in a limit alone (the
// sidecar's small and medium) clamp to one template and charge the same, so
// the lower one settles the higher tier, which is what the quota's used total
// holds. Every other limit is compared as written: on a Standard cluster and
// under bursting, where no record is written and the limits survive; a name
// the record shows unchanged, among them an equal pair the operator wrote
// itself, from a row (the broker's 2 CPU request and limit at medium) or from
// a CR override pinned to the lower row's limit (a 2Gi memory request on the
// sidecar), which the template's figures alone could not tell from a clamp;
// and a name the row sets a limit for alone (the proxy's ephemeral-storage),
// which Autopilot copies into the request and leaves where it was.
//
// A template rendered at a lower tier falls short on a figure the step raises;
// one whose override has since been lowered reads as settled with the quota
// charged more than the step assumes, the safe side.
func sizedContainerCarries(template corev1.PodTemplateSpec, name string, rendered corev1.ResourceRequirements) bool {
	found := sizedContainer(template, name)
	if found == nil {
		return true
	}
	for _, resourceName := range sizingSettledRequests {
		wanted, moved := rendered.Requests[resourceName]
		if !moved {
			continue
		}
		if got, set := found.Resources.Requests[resourceName]; !set || got.Cmp(wanted) < 0 {
			return false
		}
	}
	clamped, _ := autopilotClampedLimits(template, name)
	for _, resourceName := range sizingSettledLimits {
		wanted, moved := rendered.Limits[resourceName]
		if !moved {
			continue
		}
		got, set := found.Resources.Limits[resourceName]
		if !set {
			return false
		}
		if _, rowRequests := rendered.Requests[resourceName]; rowRequests && clamped[resourceName] {
			continue
		}
		if got.Cmp(wanted) < 0 {
			return false
		}
	}
	return true
}

// sizedContainer is the container named name in a pod template (its
// containers or, for the native sidecar, its init containers), nil when the
// template does not carry it.
func sizedContainer(template corev1.PodTemplateSpec, name string) *corev1.Container {
	var found *corev1.Container
	for _, list := range [][]corev1.Container{template.Spec.Containers, template.Spec.InitContainers} {
		for i := range list {
			if list[i].Name == name {
				found = &list[i]
			}
		}
	}
	return found
}

// autopilotAdjustmentOf is the autopilotResourceAdjustmentAnnotation record
// on a pod template: nil with no error when the template carries none (a
// Standard cluster, or Autopilot where admission changed nothing), an error
// when it carries one this operator cannot read, which the callers treat as
// absent and sizingRunningLimitsClamped logs.
func autopilotAdjustmentOf(template corev1.PodTemplateSpec) (*autopilotAdjustment, error) {
	raw, present := template.Annotations[autopilotResourceAdjustmentAnnotation]
	if !present {
		return nil, nil
	}
	record := &autopilotAdjustment{}
	if err := json.Unmarshal([]byte(raw), record); err != nil {
		return nil, fmt.Errorf("reading the %s annotation: %w", autopilotResourceAdjustmentAnnotation, err)
	}
	return record, nil
}

// container is the record's entry for the container named name on one side,
// among the containers and the init containers.
func (s autopilotAdjustmentSide) container(name string) (autopilotAdjustedContainer, bool) {
	for _, list := range [][]autopilotAdjustedContainer{s.Containers, s.InitContainers} {
		for _, entry := range list {
			if entry.Name == name {
				return entry, true
			}
		}
	}
	return autopilotAdjustedContainer{}, false
}

// autopilotClampedLimits is the set of resource names on which Autopilot's
// record on a pod template shows it lowered the container named name's limit
// to its request: the output carries a limit equal to its request there, and
// the input carried a higher one. Nil, nothing clamped, with no record, with
// a record the container is not in, and on every name the record shows
// unchanged, an equal pair the operator wrote itself included. The error is
// autopilotAdjustmentOf's.
func autopilotClampedLimits(template corev1.PodTemplateSpec, name string) (map[corev1.ResourceName]bool, error) {
	record, err := autopilotAdjustmentOf(template)
	if record == nil || err != nil {
		return nil, err
	}
	input, inputHas := record.Input.container(name)
	output, outputHas := record.Output.container(name)
	if !inputHas || !outputHas {
		return nil, nil
	}
	clamped := map[corev1.ResourceName]bool{}
	for resourceName, limit := range output.Limits {
		request, requested := output.Requests[resourceName]
		before, wasLimited := input.Limits[resourceName]
		if requested && wasLimited && limit.Cmp(request) == 0 && before.Cmp(limit) > 0 {
			clamped[resourceName] = true
		}
	}
	return clamped, nil
}

// sizingRunningLimitsClamped reports whether the cluster's admission lowers
// limits to requests, read off Autopilot's record on the three running
// templates (autopilotClampedLimits): true when any sized container's record
// shows a limit lowered to its request. The reads are the cache hits
// sizingTierUnsettledWorkload makes, and the caller measures only once that
// check has passed, so the templates read here carry the running tier. False
// where no record shows a clamp: a Standard cluster, Autopilot with bursting,
// and a clamping cluster whose three templates happen to carry no pair the
// clamp could lower (every row's limit at its request), where the step counts
// the rows' limits as written, the side that holds. A record this operator
// cannot read is logged and read as absent. A workload that cannot be read
// has no record.
func (r *PlatformAgentReconciler) sizingRunningLimitsClamped(ctx context.Context, agent *agentv1alpha1.PlatformAgent) bool {
	clamped := false
	note := func(template corev1.PodTemplateSpec, workload, container string) {
		names, err := autopilotClampedLimits(template, container)
		if err != nil {
			logf.FromContext(ctx).Info("the sizing step cannot read Autopilot's resource-adjustment record on a sized workload and counts its limits as written",
				"name", agent.Name, "namespace", agent.Namespace, "workload", workload, "error", err.Error())
			return
		}
		if len(names) > 0 {
			clamped = true
		}
	}
	proxyKey := client.ObjectKey{Namespace: agent.Namespace, Name: credentialProxyName(agent)}
	proxy := &appsv1.Deployment{}
	if err := r.Get(ctx, proxyKey, proxy); err == nil {
		note(proxy.Spec.Template, fmt.Sprintf(sizingUnsettledDeploymentFormat, proxyKey.Name), credentialProxyContainerName)
	}
	shellKey := client.ObjectKey{Namespace: agent.Namespace, Name: shellSandboxName(agent)}
	shell := &appsv1.StatefulSet{}
	if err := r.Get(ctx, shellKey, shell); err == nil {
		note(shell.Spec.Template, fmt.Sprintf(sizingUnsettledStatefulSetFormat, shellKey.Name), shellSandboxContainerName)
	}
	gatewayKey := client.ObjectKey{Namespace: agent.Namespace, Name: agent.Name + gatewayWorkloadNameSuffix}
	if useStatefulSet(agent) {
		gateway := &appsv1.StatefulSet{}
		if err := r.Get(ctx, gatewayKey, gateway); err == nil {
			note(gateway.Spec.Template, fmt.Sprintf(sizingUnsettledStatefulSetFormat, gatewayKey.Name), agentAPIAuthContainerName)
		}
		return clamped
	}
	gateway := &appsv1.Deployment{}
	if err := r.Get(ctx, gatewayKey, gateway); err == nil {
		note(gateway.Spec.Template, fmt.Sprintf(sizingUnsettledDeploymentFormat, gatewayKey.Name), agentAPIAuthContainerName)
	}
	return clamped
}

// sizingTierUnsettledWorkload is "" when the three sized workloads have
// settled at tier, or names the first that has not (its kind and name, for the
// log line the hold writes): settled means each carries tier's render, its
// roll is complete and every replica is available, so that every
// ResourceQuota's used total includes the pods the tier runs and nothing
// older. The step-up check measures each candidate step from the running tier
// against those totals, and the latch runs ahead of the pods:
// resolveSizingTier writes status.effectiveSizingTier before the workloads are
// re-rendered, the write re-enqueues the CR at once, and the proxy's Recreate
// rollout admits its new pod only after the old one has gone. A pass in that
// window that measured the next step from the latch would count the admitted
// step as already charged and could promote past the quota; it measures
// nothing until the pods have settled instead, and the workloads' status
// updates re-enqueue the CR when they have. The reads are cache hits (both
// kinds are Owns()'d). A workload that cannot be read has not settled, and
// one that is not there is among them, at small as above it: the step is
// measured as the difference between the running tier's row and the earned
// one's, and a used total that charges no pod of the workload does not charge
// the row the step is measured from, so a step measured without it would come
// up short by the whole small footprint when the workload is recreated at the
// earned row (the recreate path, when a template change the API server
// refuses as an immutable-field update is answered by deleting the workload
// and the pass ends before it is recreated; or a workload deleted by hand). The
// hold lasts the one pass that recreates it: the steps after this one create
// the missing workload at the running tier, its status re-enqueues the CR, and
// the next pass measures. The first pass of a fresh install never reaches this
// check, since it has no cluster reading to earn a step with.
func (r *PlatformAgentReconciler) sizingTierUnsettledWorkload(ctx context.Context, agent *agentv1alpha1.PlatformAgent, tier sizingTier) string {
	autopilot := r.clusterAutopilot()
	// The proxy Deployment and the shell StatefulSet are rendered with one
	// replica (credential_proxy_manifests.go, shell_sandbox_manifests.go);
	// the gateway's count is the spec's, zero under scaleToZero.
	const singleReplica int32 = 1
	gatewayReplicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	proxy := &appsv1.Deployment{}
	proxyKey := client.ObjectKey{Namespace: agent.Namespace, Name: credentialProxyName(agent)}
	if err := r.Get(ctx, proxyKey, proxy); err != nil ||
		!sizingDeploymentSettled(proxy, credentialProxyContainerName, renderedCredentialProxyResourcesAt(agent.Spec.Deployment, autopilot, tier), singleReplica) {
		return fmt.Sprintf(sizingUnsettledDeploymentFormat, proxyKey.Name)
	}
	shell := &appsv1.StatefulSet{}
	shellKey := client.ObjectKey{Namespace: agent.Namespace, Name: shellSandboxName(agent)}
	if err := r.Get(ctx, shellKey, shell); err != nil ||
		!sizingStatefulSetSettled(shell, shellSandboxContainerName, renderedShellSandboxResourcesAt(agent, tier), singleReplica) {
		return fmt.Sprintf(sizingUnsettledStatefulSetFormat, shellKey.Name)
	}
	gatewayKey := client.ObjectKey{Namespace: agent.Namespace, Name: agent.Name + gatewayWorkloadNameSuffix}
	sidecar := renderedAgentAPIAuthResourcesAt(agent.Spec.Deployment, tier)
	if useStatefulSet(agent) {
		gateway := &appsv1.StatefulSet{}
		if err := r.Get(ctx, gatewayKey, gateway); err != nil ||
			!sizingStatefulSetSettled(gateway, agentAPIAuthContainerName, sidecar, gatewayReplicas) {
			return fmt.Sprintf(sizingUnsettledStatefulSetFormat, gatewayKey.Name)
		}
		return ""
	}
	gateway := &appsv1.Deployment{}
	if err := r.Get(ctx, gatewayKey, gateway); err != nil ||
		!sizingDeploymentSettled(gateway, agentAPIAuthContainerName, sidecar, gatewayReplicas) {
		return fmt.Sprintf(sizingUnsettledDeploymentFormat, gatewayKey.Name)
	}
	return ""
}

// sizingTierSettled is sizingTierUnsettledWorkload with no workload to name.
func (r *PlatformAgentReconciler) sizingTierSettled(ctx context.Context, agent *agentv1alpha1.PlatformAgent, tier sizingTier) bool {
	return r.sizingTierUnsettledWorkload(ctx, agent, tier) == ""
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

// sizingQuotaPriorityClasses is the one-element set of the class admission
// gives the sized pods (sizingAdmittedPriorityClass) when a quota in quotas
// has a PriorityClass scope, the one scope whose answer needs admission asked,
// and nil without asking otherwise. The second value is false when admission
// was asked and gave no answer; the caller then measures no step.
func (r *PlatformAgentReconciler) sizingQuotaPriorityClasses(ctx context.Context, agent *agentv1alpha1.PlatformAgent, quotas []corev1.ResourceQuota) ([]string, bool) {
	for i := range quotas {
		if sizingQuotaScopesPriorityClass(&quotas[i]) {
			class, answered := r.sizingAdmittedPriorityClass(ctx, agent)
			if !answered {
				return nil, false
			}
			return []string{class}, true
		}
	}
	return nil, true
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
// it can neither refuse the step nor be raised to admit it. The step's limits
// side is read through clamped (sizingRunningLimitsClamped).
func sizingQuotaShortfall(agent *agentv1alpha1.PlatformAgent, autopilot bool, clamped bool, quotas []corev1.ResourceQuota, classes []string, from, to sizingTier) *sizingShortfall {
	if len(quotas) == 0 {
		return nil
	}
	step := sizingStepBetween(agent, autopilot, clamped, from, to)
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

// sizingLimitRanges lists the LimitRanges in the agent's namespace in name
// order, so the condition message is stable across passes. LimitRange is not
// a watched kind, so the list goes through the API reader, as sizingQuotas
// does.
func (r *PlatformAgentReconciler) sizingLimitRanges(ctx context.Context, agent *agentv1alpha1.PlatformAgent) ([]corev1.LimitRange, error) {
	ranges := &corev1.LimitRangeList{}
	if err := r.a2aReader().List(ctx, ranges, client.InNamespace(agent.Namespace)); err != nil {
		return nil, fmt.Errorf("listing LimitRanges for the sizing step: %w", err)
	}
	slices.SortFunc(ranges.Items, func(a, b corev1.LimitRange) int { return strings.Compare(a.Name, b.Name) })
	return ranges.Items, nil
}

// sizedPodTemplates is the pod template each sized container runs in, keyed
// by the container's name (renderedSizedContainersAt's names), read off the
// three sized workloads; a workload that cannot be read has no entry. The
// reads are the cache hits sizingTierUnsettledWorkload makes.
func (r *PlatformAgentReconciler) sizedPodTemplates(ctx context.Context, agent *agentv1alpha1.PlatformAgent) map[string]corev1.PodTemplateSpec {
	templates := map[string]corev1.PodTemplateSpec{}
	proxy := &appsv1.Deployment{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: credentialProxyName(agent)}, proxy); err == nil {
		templates[credentialProxyContainerName] = proxy.Spec.Template
	}
	shell := &appsv1.StatefulSet{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: shellSandboxName(agent)}, shell); err == nil {
		templates[shellSandboxContainerName] = shell.Spec.Template
	}
	gatewayKey := client.ObjectKey{Namespace: agent.Namespace, Name: agent.Name + gatewayWorkloadNameSuffix}
	if useStatefulSet(agent) {
		gateway := &appsv1.StatefulSet{}
		if err := r.Get(ctx, gatewayKey, gateway); err == nil {
			templates[agentAPIAuthContainerName] = gateway.Spec.Template
		}
		return templates
	}
	gateway := &appsv1.Deployment{}
	if err := r.Get(ctx, gatewayKey, gateway); err == nil {
		templates[agentAPIAuthContainerName] = gateway.Spec.Template
	}
	return templates
}

// sizingPodFigures is the pod-level requests and limits the LimitRanger
// plugin checks a Pod-type item against, for the pod template the sized
// container named name runs in, with that container's resources replaced by
// rendered (the candidate's render) and every container read as admission
// reads it (effectiveRequests, the limits through clamped). The sum is the
// scheduler's: the containers and the restartable init containers added up,
// and each other init container, with the restartable ones before it, taken
// as a floor on each name. A template that is not there is read as the sized
// container alone.
func sizingPodFigures(template corev1.PodTemplateSpec, name string, rendered corev1.ResourceRequirements, clamped bool) (corev1.ResourceList, corev1.ResourceList) {
	figures := func(container corev1.Container) (corev1.ResourceList, corev1.ResourceList) {
		rr := container.Resources
		if container.Name == name {
			rr = rendered
		}
		requests := effectiveRequests(rr)
		return requests, clampedLimits(rr.Limits, requests, clamped)
	}
	add := func(total, more corev1.ResourceList) {
		for key, quantity := range more {
			sum := total[key]
			sum.Add(quantity)
			total[key] = sum
		}
	}
	floor := func(total, more corev1.ResourceList) {
		for key, quantity := range more {
			if current, ok := total[key]; !ok || current.Cmp(quantity) < 0 {
				total[key] = quantity.DeepCopy()
			}
		}
	}
	requests, limits := corev1.ResourceList{}, corev1.ResourceList{}
	if sizedContainer(template, name) == nil {
		return figures(corev1.Container{Name: name})
	}
	for _, container := range template.Spec.Containers {
		req, lim := figures(container)
		add(requests, req)
		add(limits, lim)
	}
	restartableRequests, restartableLimits := corev1.ResourceList{}, corev1.ResourceList{}
	initRequests, initLimits := corev1.ResourceList{}, corev1.ResourceList{}
	for _, container := range template.Spec.InitContainers {
		req, lim := figures(container)
		if container.RestartPolicy != nil && *container.RestartPolicy == corev1.ContainerRestartPolicyAlways {
			add(requests, req)
			add(limits, lim)
			add(restartableRequests, req)
			add(restartableLimits, lim)
			floor(initRequests, restartableRequests)
			floor(initLimits, restartableLimits)
			continue
		}
		add(req, restartableRequests)
		add(lim, restartableLimits)
		floor(initRequests, req)
		floor(initLimits, lim)
	}
	floor(requests, initRequests)
	floor(limits, initLimits)
	return requests, limits
}

// sizingLimitRangeBound is nil when every item of every LimitRange in ranges
// (the namespace's, in name order) admits the three sized containers, and
// the pods they run in, as tier to renders them, or the first bound one of
// them crosses: ranges, items, containers (renderedSizedContainersAt's
// order) and resource names each in a fixed order, so the condition message
// is stable across passes. It reads the checks the LimitRanger admission
// plugin makes against the recreated pod, after the pod's own defaulting has
// copied a limit set alone into the request (effectiveRequests): on a
// Container-type item, the sized container's limit or request above max, its
// request or limit under min, and its limit-to-request ratio above
// maxLimitRequestRatio; on a Pod-type item, the same three on the pod's
// sums (sizingPodFigures), the sized container swapped for the candidate's
// render in the running template read off templates (sizedPodTemplates),
// since the pod's sum moves by exactly the sized container's step. A name
// the container, or the pod, does not set is not read: the plugin fills a
// container's from the item's default, which the API server sets to max
// where none is given, so it admits, and a min, a ratio or a Pod-type bound
// with no figure to read refuses the running pods' recreation the same
// whatever the tier. The limits side is read through clamped
// (sizingRunningLimitsClamped), since the plugin's validation runs after
// Autopilot's rewrite and sees the clamped limit, and a clamped ratio is one.
// The ratio is compared as the plugin compares it, limit over request in
// milli-units against the item's figure.
func sizingLimitRangeBound(agent *agentv1alpha1.PlatformAgent, autopilot bool, clamped bool, ranges []corev1.LimitRange, templates map[string]corev1.PodTemplateSpec, to sizingTier) *sizingBound {
	if len(ranges) == 0 {
		return nil
	}
	containers := renderedSizedContainersAt(agent, autopilot, to)
	for i := range ranges {
		for _, item := range ranges[i].Spec.Limits {
			var scope string
			switch item.Type {
			case corev1.LimitTypeContainer:
				scope = sizingLimitRangeScopeContainer
			case corev1.LimitTypePod:
				scope = sizingLimitRangeScopePod
			default:
				continue
			}
			for _, rendered := range containers {
				var requests, limits corev1.ResourceList
				if scope == sizingLimitRangeScopePod {
					requests, limits = sizingPodFigures(templates[rendered.container], rendered.container, rendered.resources, clamped)
				} else {
					requests = effectiveRequests(rendered.resources)
					limits = clampedLimits(rendered.resources.Limits, requests, clamped)
				}
				crossed := func(bound string, key corev1.ResourceName, quantity resource.Quantity, figure string) *sizingBound {
					return &sizingBound{limitRange: ranges[i].Name, scope: scope, bound: bound, key: key, container: rendered.container, quantity: quantity, figure: figure}
				}
				for _, key := range sortedResourceNames(item.Max) {
					if limit, set := limits[key]; set && limit.Cmp(item.Max[key]) > 0 {
						return crossed(sizingLimitRangeMax, key, item.Max[key], limit.String())
					}
					if request, set := requests[key]; set && request.Cmp(item.Max[key]) > 0 {
						return crossed(sizingLimitRangeMax, key, item.Max[key], request.String())
					}
				}
				for _, key := range sortedResourceNames(item.Min) {
					if request, set := requests[key]; set && request.Cmp(item.Min[key]) < 0 {
						return crossed(sizingLimitRangeMin, key, item.Min[key], request.String())
					}
					if limit, set := limits[key]; set && limit.Cmp(item.Min[key]) < 0 {
						return crossed(sizingLimitRangeMin, key, item.Min[key], limit.String())
					}
				}
				for _, key := range sortedResourceNames(item.MaxLimitRequestRatio) {
					request, requested := requests[key]
					limit, limited := limits[key]
					if !requested || !limited || request.Sign() <= 0 {
						continue
					}
					enforced := item.MaxLimitRequestRatio[key]
					ratio := float64(limit.MilliValue()) / float64(request.MilliValue())
					if ratio > enforced.AsApproximateFloat64() {
						return crossed(sizingLimitRangeRatio, key, enforced, fmt.Sprintf(sizingLimitRangeRatioFigureFormat, ratio))
					}
				}
			}
		}
	}
	return nil
}

// sizingRefusedStep is the ResourceQuota refusal the latched tier's pods
// met at admission, or nil. The gate measures a step against the quotas'
// used totals once and writes the latch before the proxy Deployment, a
// Recreate, has drained its old pod and asked admission for the new one; a
// pod the namespace admits in that window (a session pod, an A2A job, a
// sibling agent's step) takes the room the gate found, the ReplicaSet's
// create is refused with "exceeded quota", and the Deployment carries
// ReplicaFailure True with admission's message. Nothing in the gate reads
// that: the latch names the tier, the proxy never settles, and the broker
// is down until a person names a lower tier. This reads it. The proxy
// Deployment and the gateway's, when it is a Deployment, are read for a
// ReplicaFailure condition whose message names a quota
// (resourceQuotaNamedIn) and is the latched tier's step being refused
// (sizingRefusalIsTheSteps: a key the step up to the tier moved on that
// pod, asked for at that tier's figure and not the row above's); the first
// found is returned, and the caller steps the latch back one tier and
// writes the cap with admission's message. A refusal on a key no row moves
// (a pods count at its cap, a requests.memory quota refusing the gateway's
// pod, whose sidecar's request every row keeps) is not the step's and is
// left to the settled hold, which logs the workload that cannot settle;
// stepping back would recreate the broker at a lower row the refusal does
// not care about. The StatefulSets (the shell, the gateway under
// useStatefulSet) carry no such condition and are not read: their refused
// pod shows as the hold the settled wait logs. Nil at small, where there is
// no tier to step back to, and for a refusal the condition on the CR
// already records: the ReplicaSet keeps its condition until it is scaled
// away, and the Deployment copies it for a pass or two after the step back,
// so the same message is read as already answered, as is a message asking
// for the row above's figure, whichever Deployment carries it and whatever
// the condition says; the lower row's pod, refused by a new ReplicaSet,
// comes with a new message at the lower figure and is stepped back from in
// turn.
func (r *PlatformAgentReconciler) sizingRefusedStep(ctx context.Context, agent *agentv1alpha1.PlatformAgent, tier sizingTier) *sizingRefusal {
	if tier == sizingTierSmall {
		return nil
	}
	sized := []struct {
		key       client.ObjectKey
		container string
	}{{client.ObjectKey{Namespace: agent.Namespace, Name: credentialProxyName(agent)}, credentialProxyContainerName}}
	if !useStatefulSet(agent) {
		sized = append(sized, struct {
			key       client.ObjectKey
			container string
		}{client.ObjectKey{Namespace: agent.Namespace, Name: agent.Name + gatewayWorkloadNameSuffix}, agentAPIAuthContainerName})
	}
	recorded := sizingCappedCondition(agent).message
	autopilot, clamped, clampRead := r.clusterAutopilot(), false, false
	for _, workload := range sized {
		dep := &appsv1.Deployment{}
		if err := r.Get(ctx, workload.key, dep); err != nil {
			continue
		}
		for _, cond := range dep.Status.Conditions {
			if cond.Type != appsv1.DeploymentReplicaFailure || cond.Status != corev1.ConditionTrue {
				continue
			}
			quota, named := resourceQuotaNamedIn(cond.Message)
			if !named || strings.Contains(recorded, cond.Message) {
				continue
			}
			if !clampRead {
				clamped, clampRead = r.sizingRunningLimitsClamped(ctx, agent), true
			}
			if !sizingRefusalIsTheSteps(agent, autopilot, clamped, dep.Spec.Template, workload.container, tier, quotaRefusalRequested(cond.Message)) {
				continue
			}
			return &sizingRefusal{workload: fmt.Sprintf(sizingUnsettledDeploymentFormat, workload.key.Name), quota: quota, message: cond.Message}
		}
	}
	return nil
}

// sizingRefusalIsTheSteps reports whether a quota refusal of the pod the
// sized container named name runs in, read with the latch at tier, is a
// refusal of that tier's pod on a key the step up to it moved. requested is
// the refusal's list (quotaRefusalRequested): the quota keys the pod crossed
// and the pod's figure on each. One key is enough when it is a key the
// step-up check reads (sizingQuotaDimensions), the container's own figure
// on it rose from the tier below (sizedContainerStepBetween, the limits
// through clamped), and the pod's figure the refusal names is under what the
// pod would ask at the row above tier (sizingPodFigures on template with the
// container at that row's render). The last term tells a refusal of the row
// above, lingering on the Deployment after a step back from it, from the
// latched row's own: the one asks for the row above's figure or more, the
// other for less. At the top row nothing lingers from above.
func sizingRefusalIsTheSteps(agent *agentv1alpha1.PlatformAgent, autopilot bool, clamped bool, template corev1.PodTemplateSpec, name string, tier sizingTier, requested corev1.ResourceList) bool {
	if tier == sizingTierSmall || len(requested) == 0 {
		return false
	}
	step := sizedContainerStepBetween(agent, autopilot, clamped, name, tier-1, tier)
	var aboveRequests, aboveLimits corev1.ResourceList
	if tier < sizingTierFleet {
		for _, rendered := range renderedSizedContainersAt(agent, autopilot, tier+1) {
			if rendered.container == name {
				aboveRequests, aboveLimits = sizingPodFigures(template, name, rendered.resources, clamped)
			}
		}
	}
	for _, dim := range sizingQuotaDimensions {
		asked, named := requested[dim.key]
		if !named {
			continue
		}
		side, above := step.requests, aboveRequests
		if dim.limits {
			side, above = step.limits, aboveLimits
		}
		if moved, ok := side[dim.resource]; !ok || moved.Sign() <= 0 {
			continue
		}
		if bound, ok := above[dim.resource]; ok && asked.Cmp(bound) >= 0 {
			continue
		}
		return true
	}
	return false
}

// resourceQuotaNamedIn is the ResourceQuota an admission refusal names, as
// the quota plugin words it (resourceQuotaExceededPhrase), and whether the
// message carries one.
func resourceQuotaNamedIn(message string) (string, bool) {
	_, after, found := strings.Cut(message, resourceQuotaExceededPhrase)
	if !found {
		return "", false
	}
	name, _, _ := strings.Cut(after, resourceQuotaExceededNameEnd)
	name = strings.TrimSpace(name)
	return name, name != ""
}

// quotaRefusalRequested is the requested list of an admission refusal, as
// the quota plugin words it (resourceQuotaRequestedPhrase): the quota keys
// the pod crossed, each with the figure the pod asked for on it. Empty when
// the message carries none; a pair that does not parse is skipped.
func quotaRefusalRequested(message string) corev1.ResourceList {
	requested := corev1.ResourceList{}
	_, after, found := strings.Cut(message, resourceQuotaRequestedPhrase)
	if !found {
		return requested
	}
	list, _, _ := strings.Cut(after, resourceQuotaUsedPhrase)
	for _, pair := range strings.Split(list, resourceQuotaListSeparator) {
		key, value, ok := strings.Cut(strings.TrimSpace(pair), resourceQuotaPairSeparator)
		if !ok {
			continue
		}
		quantity, err := resource.ParseQuantity(strings.TrimSpace(value))
		if err != nil {
			continue
		}
		requested[corev1.ResourceName(strings.TrimSpace(key))] = quantity
	}
	return requested
}

// sizingCappedConditionCurrent reports whether the SizingCappedByQuota
// condition on the CR already says capped, where the zero value means
// absent.
func sizingCappedConditionCurrent(agent *agentv1alpha1.PlatformAgent, capped sizingCap) bool {
	existing := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
	if capped.message == "" {
		return existing == nil
	}
	return existing != nil && existing.Status == metav1.ConditionTrue &&
		existing.Reason == capped.reason && existing.Message == capped.message
}

// setSizingCappedCondition writes the SizingCappedByQuota condition, or
// removes it when capped is the zero value. The caller does the API write.
func setSizingCappedCondition(agent *agentv1alpha1.PlatformAgent, capped sizingCap, now metav1.Time) {
	if capped.message == "" {
		meta.RemoveStatusCondition(&agent.Status.Conditions, sizingCappedByQuotaConditionType)
		return
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type:               sizingCappedByQuotaConditionType,
		Status:             metav1.ConditionTrue,
		Reason:             capped.reason,
		Message:            capped.message,
		ObservedGeneration: agent.Generation,
		LastTransitionTime: now,
	})
}

// sizingCappedCondition is the reason and message the SizingCappedByQuota
// condition on the CR carries, the zero value when it is absent or not a
// condition this operator writes.
func sizingCappedCondition(agent *agentv1alpha1.PlatformAgent) sizingCap {
	existing := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
	if existing == nil || existing.Status != metav1.ConditionTrue ||
		(existing.Reason != sizingCappedByQuotaReason && existing.Reason != sizingCappedByLimitRangeReason) {
		return sizingCap{}
	}
	return sizingCap{reason: existing.Reason, message: existing.Message}
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
// the condition. A step the gate admitted whose recreated pod admission then
// refused on a quota (sizingRefusedStep) is stepped back: the latch moves
// down one tier and the condition names the quota and admission's message,
// so the latch records what the pods run and the broker comes back at the
// lower row. A step is also refused when a LimitRange in the namespace
// bounds a figure the candidate's render, or the pod's sum with it, crosses
// (sizingLimitRangeBound), since that admission plugin refuses the recreated
// pod the way a short quota does; the LimitRange is read before the quota,
// the order admission evaluates them in, so the condition names the LimitRange
// when both refuse. With a quota or a LimitRange in the namespace, a step is
// measured only once the running tier's workloads have settled
// (sizingTierSettled): the used totals a quota is measured against do not
// charge a step whose pods admission has not yet created, and a Pod-type
// LimitRange item is measured against the running templates, which a
// workload that is absent or still rolling does not supply; until then the
// latch and the condition stay where they are, with no write and no event.
// For sizingStepBackHoldInterval after a step back no step is measured
// either (sizingStepBackHeld), so a namespace whose slack sits near a step's
// size cycles at most once per interval. A count that falls leaves the latch where it is and clears the
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
	capped := sizingCap{}
	var short *sizingShortfall
	var bound *sizingBound
	var refused *sizingRefusal
	var count int64
	if profile == sizingProfileAuto {
		tier = latched
		if refused = r.sizingRefusedStep(ctx, agent, tier); refused != nil {
			// The latched tier's recreated pod was refused on a quota the
			// gate had found room on (sizingRefusedStep): the latch steps
			// back one tier, so that it again records what the pods run,
			// and the cap names the quota and what admission said. The
			// next pass renders the lower row; when its pod settles, the
			// gate measures the step up again against the used totals as
			// they stand, and when it too is refused this reads that
			// refusal and steps back once more. The count is not read
			// here: a refused pod is backed out of whatever the count
			// earns, and the pass after decides from the count.
			from := tier
			tier--
			capped = sizingCap{reason: sizingCappedByQuotaReason, message: fmt.Sprintf(sizingStepRefusedMessageFormat,
				refused.workload, from, refused.quota, refused.message, tier)}
		} else if observed, ok := observedClusterCount(agent); ok {
			count = observed
			if earned := sizingTierForClusterCount(count); earned > tier {
				quotas, err := r.sizingQuotas(ctx, agent)
				if err != nil {
					return err
				}
				ranges, err := r.sizingLimitRanges(ctx, agent)
				if err != nil {
					return err
				}
				waitingOn := ""
				if len(quotas) > 0 || len(ranges) > 0 {
					waitingOn = r.sizingTierUnsettledWorkload(ctx, agent, tier)
				}
				heldFor, heldBack := time.Duration(0), false
				if waitingOn == "" {
					heldFor, heldBack = r.sizingStepBackHeld(agent)
				}
				classes, answered := []string(nil), true
				if waitingOn == "" && !heldBack {
					classes, answered = r.sizingQuotaPriorityClasses(ctx, agent, quotas)
				}
				switch {
				case waitingOn != "":
					// The running tier's pods have not all been admitted yet
					// (the pass after a promotion, while the proxy recreates),
					// so the quotas' used totals do not yet charge the step
					// just taken and a step measured from the latch would
					// count it as free, and the running templates a Pod-type
					// LimitRange item is read against are not all there or
					// not yet at the tier's render, so a pod's sum read off
					// them would miss a container or carry the wrong row.
					// Nothing is measured: the cap stands as written, and the
					// workloads' status updates re-enqueue the CR when the
					// pods have settled. With no quota and no LimitRange
					// there is nothing to measure against, and the earned
					// tier is taken as before. The hold is logged on every
					// pass it takes, so a wait that never ends (a workload
					// that cannot settle at the tier's render) is told apart
					// from a quiet pass.
					capped = sizingCappedCondition(agent)
					logf.FromContext(ctx).Info("sizing step held until the running tier's workloads settle",
						"name", agent.Name, "namespace", agent.Namespace, "tier", tier.String(), "earned", earned.String(),
						"waitingOn", waitingOn, "clusters", count)
				case heldBack:
					// A step back (sizingRefusedStep) within the last
					// sizingStepBackHoldInterval: the room the refused step
					// lost may be back already, and a pass that measured it
					// would take the step again, into the next pod the
					// namespace admits during the drain. Nothing is measured
					// until the interval has passed; the cap stands as the
					// step back wrote it, and the hold is logged on every pass
					// it takes, as the settled hold is.
					capped = sizingCappedCondition(agent)
					logf.FromContext(ctx).Info("sizing step held after a step back until the hold interval passes",
						"name", agent.Name, "namespace", agent.Namespace, "tier", tier.String(), "earned", earned.String(),
						"heldFor", heldFor.Round(time.Second).String(), "holdInterval", sizingStepBackHoldInterval.String(), "clusters", count)
				case !answered:
					// A quota scopes the pods by PriorityClass and admission
					// refused the dry run that asks which class they get
					// (sizingAdmittedPriorityClass, whose log line names the
					// refusal). Which quotas bind the pods is unknown, so
					// nothing is measured, as above: the latch and the
					// condition stand, and the pass that finds admission
					// answering measures. Reading the scoped quotas as
					// binding instead would cap the tier on a quota that may
					// not count these pods and tell the reader to raise it.
					capped = sizingCappedCondition(agent)
					logf.FromContext(ctx).Info("sizing step held until admission answers the sizing pods' priority class",
						"name", agent.Name, "namespace", agent.Namespace, "tier", tier.String(), "earned", earned.String(),
						"clusters", count)
				default:
					autopilot := r.clusterAutopilot()
					clamped := r.sizingRunningLimitsClamped(ctx, agent)
					templates := map[string]corev1.PodTemplateSpec{}
					if len(ranges) > 0 {
						templates = r.sizedPodTemplates(ctx, agent)
					}
					// The earned tier first, then each tier between it and
					// the running one, every step measured from the running
					// tier against the same used totals: the highest step the
					// LimitRanges and the quotas admit is taken, so a count
					// that earns fleet with room for medium alone reaches
					// medium rather than waiting at small for the whole fleet
					// step. The cap reports the lowest step refused, the one
					// raising the quota or the bound by the least relieves,
					// and the check that refused it.
					running := tier
					for candidate := earned; candidate > running; candidate-- {
						if crossed := sizingLimitRangeBound(agent, autopilot, clamped, ranges, templates, candidate); crossed != nil {
							bound, short = crossed, nil
							continue
						}
						if shortfall := sizingQuotaShortfall(agent, autopilot, clamped, quotas, classes, running, candidate); shortfall != nil {
							short, bound = shortfall, nil
							continue
						}
						tier = candidate
						break
					}
					// The message names the step the figures were measured
					// from (running) and the tier that stays (tier, the
					// admitted candidate when one was taken); the next
					// settled pass measures from that tier and the message
					// changes once, recording that step's figures with it.
					switch {
					case bound != nil:
						capped = sizingCap{reason: sizingCappedByLimitRangeReason, message: fmt.Sprintf(sizingCappedByLimitRangeMessageFormat,
							earned, bound.limitRange, bound.scope, bound.bound, bound.key, bound.subject(), running, tier)}
					case short != nil:
						capped = sizingCap{reason: sizingCappedByQuotaReason, message: fmt.Sprintf(sizingCappedByQuotaMessageFormat,
							earned, short.quota, short.key, running, tier)}
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
			capped = sizingCappedCondition(agent)
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
	if refused != nil {
		log.Info("sizing tier stepped back: admission refused the recreated pod on a ResourceQuota", "name", agent.Name, "namespace", agent.Namespace,
			"workload", refused.workload, "quota", refused.quota, "from", agent.Status.EffectiveSizingTier, "to", tier.String(), "admission", refused.message)
		r.recordEvent(agent, corev1.EventTypeWarning, sizingCappedByQuotaEventReason, capped.message)
	}
	if short != nil && !sizingCappedConditionCurrent(agent, capped) {
		log.Info("sizing tier capped by ResourceQuota", "name", agent.Name, "namespace", agent.Namespace,
			"quota", short.quota, "key", short.key, "free", short.free.String(), "adds", short.need.String(), "clusters", count)
		r.recordEvent(agent, corev1.EventTypeWarning, sizingCappedByQuotaEventReason,
			fmt.Sprintf(sizingCappedByQuotaEventFormat, capped.message, count, short.free.String(), short.key, short.need.String()))
	}
	if bound != nil && !sizingCappedConditionCurrent(agent, capped) {
		log.Info("sizing tier capped by LimitRange", "name", agent.Name, "namespace", agent.Namespace,
			"limitRange", bound.limitRange, "scope", bound.scope, "bound", bound.bound, "key", bound.key, "container", bound.container,
			"quantity", bound.quantity.String(), "figure", bound.figure, "clusters", count)
		r.recordEvent(agent, corev1.EventTypeWarning, sizingCappedByLimitRangeEventReason,
			fmt.Sprintf(sizingCappedByLimitRangeEventFormat, capped.message, count, bound.bound, bound.quantity.String(), bound.figure))
	}
	agent.Status.EffectiveSizingTier = tier.String()
	setSizingCappedCondition(agent, capped, metav1.Now())
	if err := r.Status().Update(ctx, agent); err != nil {
		return err
	}
	if refused != nil {
		r.steppedBackAt.Store(client.ObjectKeyFromObject(agent), time.Now())
	}
	r.noteSizingTierEcho(ctx, agent)
	return nil
}

// sizingStepBackHeld reports whether this CR's latch was stepped back
// (sizingRefusedStep) within the last sizingStepBackHoldInterval, and for
// how long the hold has stood. The record is in memory (steppedBackAt), as
// the pruned-field echo's is: an operator restart reads no hold.
func (r *PlatformAgentReconciler) sizingStepBackHeld(agent *agentv1alpha1.PlatformAgent) (time.Duration, bool) {
	recorded, stepped := r.steppedBackAt.Load(client.ObjectKeyFromObject(agent))
	if !stepped {
		return 0, false
	}
	held := time.Since(recorded.(time.Time))
	return held, held < sizingStepBackHoldInterval
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
