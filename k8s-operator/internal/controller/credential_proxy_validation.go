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
	"math"
	"slices"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// GKE Autopilot's general-purpose compute class admits a container unchanged
// only while its requested memory sits between 1 GiB and 6.5 GiB per requested
// vCPU, and raises the smaller request of the pair to reach the band
// otherwise. The band applies to requests alone; limits Autopilot either sets
// equal to the requests (no bursting) or keeps as declared (bursting),
// whatever their ratio. The operator declares the proxy's default requests at
// the lower edge of the band (500m and 512Mi) so the manifest it writes is the
// pod Autopilot admits; a requests override that leaves the band is admitted
// at figures the CR does not show, and the chart's quota preflight, which sums
// the CR's figures, is short by the difference. A warning rather than a
// refusal: the pod still runs, and on Standard nothing is resized at all.
const (
	autopilotMinMemoryBytesPerVCPU int64 = 1 << 30
	autopilotMaxMemoryBytesPerVCPU int64 = 6656 << 20 // 6.5 GiB
	bytesPerMiB                    int64 = 1 << 20
	bytesPerGiB                    int64 = 1 << 30
)

const (
	credentialProxyRequestsField = "requests"
	credentialProxyLimitsField   = "limits"
	credentialProxyClaimsField   = "claims"
)

// The refusals ValidateCredentialProxyResources makes that carry no figures
// of their own.
const (
	credentialProxyResourceNameRefusal    = "the credential-proxy container accepts cpu, memory and ephemeral-storage only and declares no other resource; any other name is one the API server refuses as written (an extended resource without its limit, hugepages whose request and limit differ) or the pod has no use for"                                            // #nosec G101 -- Error message, not a credential
	credentialProxyClaimsRefusal          = "the credential-proxy pod declares no resourceClaims, so a claim named here cannot take effect"                                                                                                                                                                                                                                 // #nosec G101 -- Error message, not a credential
	credentialProxyNegativeRefusal        = "must not be negative; the API server refuses a container that declares one"                                                                                                                                                                                                                                                    // #nosec G101 -- Error message, not a credential
	credentialProxyZeroLimitRefusal       = "a limit of zero leaves the container nothing of this resource; omit the key to keep the operator's default"                                                                                                                                                                                                                    // #nosec G101 -- Error message, not a credential
	credentialProxyUnrepresentableFmt     = "is not a representable byte count: it exceeds the %d bytes an int64 holds, which is what the Downward API hands the broker"                                                                                                                                                                                                    // #nosec G101 -- Error message, not a credential
	credentialProxyUnrepresentableCPUFmt  = "is not a CPU count the scheduler can represent in millicores: its millicore value exceeds the %d an int64 holds, so the scheduler and kubelet would read it as a wrapped figure, zero or negative"                                                                                                                             // #nosec G101 -- Error message, not a credential
	credentialProxyFloorRefusalFmt        = "a %s memory limit is under the %dMi floor at which the budget admits %d commands; below it the broker turns the budget off and admits by the slot cap alone, which is the exposure the budget exists to remove"                                                                                                                // #nosec G101 -- Error message, not a credential
	credentialProxyCrossedBesideFmt       = "exceeds the %s %s limit set beside it"                                                                                                                                                                                                                                                                                         // #nosec G101 -- Error message, not a credential
	credentialProxyCrossedDefLimitFmt     = "exceeds the operator's default %s %s limit, which this override does not raise; set limits.%s as well"                                                                                                                                                                                                                         // #nosec G101 -- Error message, not a credential
	credentialProxyCrossedDefRequestFmt   = "is below the operator's default %s %s request, which this override does not lower; set requests.%s as well"                                                                                                                                                                                                                    // #nosec G101 -- Error message, not a credential
	credentialProxyRequestsBandWarningFmt = "%s: %s of memory per %s of CPU is %.2f GiB per vCPU, outside the %d to %.1f GiB per vCPU that GKE Autopilot admits unchanged; Autopilot raises the smaller side into that band, so the pod it admits is larger than this CR declares and the chart's quota preflight, which sums the CR's figures, is short by the difference" // #nosec G101 -- Error message, not a credential
	credentialProxyLimitWithoutRequestFmt = "%s is set without %s; GKE Autopilot without bursting sets a container's limits equal to its requests, so there the proxy runs at the %s request and this limit has no effect — set %s to the same value, or enable bursting"                                                                                                   // #nosec G101 -- Warning text, not a credential
)

// credentialProxyKeySeparator joins a side and a resource name: "requests.memory".
const credentialProxyKeySeparator = "."

// credentialProxyRefusalMoreFmt counts the refusals past the first, which the
// condition and the event leave out: the override's keys are the author's and
// unbounded in number, while a condition message is capped.
const credentialProxyRefusalMoreFmt = " (and %d more)" // #nosec G101 -- Message suffix, not a credential

// credentialProxyRefusalMessageBudget bounds the refusal the condition and the
// event carry, and credentialProxyRefusalEllipsis marks a refusal cut to fit
// it. A refusal quotes the field path, and a resource name under limits or
// requests is the author's and unbounded in length, while the CRD caps a
// condition message at 32768 characters: an undeclared name longer than that
// would fail every status write, Ready and every other condition with it, for
// as long as it stays in the spec. The budget matches
// hostPathDroppedEntryBudget's, far under the cap, because the message is read
// in `kubectl describe`.
const (
	credentialProxyRefusalMessageBudget = 4096
	credentialProxyRefusalEllipsis      = "..."
)

// credentialProxyResourceNameBudget bounds a resource name where it enters a
// field path. A field.Error renders as "<path>: <reason>", so a name cut only
// with the whole message would keep the author's key and drop the reason that
// says what is wrong with it. Cut here, the reason survives and the message
// budget above is the backstop.
const credentialProxyResourceNameBudget = 128

// credentialProxyResourcesPath is where the override sits on the CR.
var credentialProxyResourcesPath = field.NewPath("spec", "deployment", "credentialProxy", "resources")

// acceptedContainerResourceNames are the only names an override may carry, for
// the credential proxy and the agent-api-auth sidecar alike: the quantities a
// container declares. Anything else (an extended resource, hugepages, a
// misspelt name) reaches the API server unchecked here, which refuses several
// such shapes as Invalid, and the reconciler would read that as an
// immutable-field change and recreate the workload.
var acceptedContainerResourceNames = []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory, corev1.ResourceEphemeralStorage}

// byteCountResources are the names whose quantity is a count of bytes, and so
// has to fit the int64 the Downward API and the kubelet carry it in.
var byteCountResources = []corev1.ResourceName{corev1.ResourceMemory, corev1.ResourceEphemeralStorage}

// boundedResourceFieldPath is path.side.name with name cut to
// credentialProxyResourceNameBudget, marked with credentialProxyRefusalEllipsis.
func boundedResourceFieldPath(path *field.Path, side string, name corev1.ResourceName) *field.Path {
	key := string(name)
	if len(key) > credentialProxyResourceNameBudget {
		key = truncateToValidUTF8(key, credentialProxyResourceNameBudget-len(credentialProxyRefusalEllipsis)) + credentialProxyRefusalEllipsis
	}
	return path.Child(side, key)
}

// maxByteCount is the largest byte count an int64 carries, as a quantity.
var maxByteCount = *resource.NewQuantity(math.MaxInt64, resource.BinarySI)

// maxCPUMilli is the largest CPU quantity whose millicore value an int64
// carries. The scheduler and kubelet read a CPU quantity through MilliValue,
// which past this wraps (10E reads as 0, 9223372036854775808m as negative).
var maxCPUMilli = resource.NewMilliQuantity(math.MaxInt64, resource.DecimalSI)

// containerResourceMessages carries the container-specific text the generic
// validateContainerResources puts on each refusal and warning, so the
// credential proxy and the agent-api-auth sidecar share one validator and
// differ only in wording.
type containerResourceMessages struct {
	resourceName           string
	claims                 string
	negative               string
	zeroLimit              string
	unrepresentableFmt     string
	unrepresentableCPUFmt  string
	crossedBesideFmt       string
	crossedDefLimitFmt     string
	crossedDefRequestFmt   string
	requestsBandFmt        string
	limitWithoutRequestFmt string
}

// memoryLimitFloor is an optional lower bound on the merged memory limit, with
// the message for a limit below it. The credential proxy sets one, because its
// child memory budget needs room to admit two commands; the agent-api-auth
// sidecar passes nil, because its event watcher derives no admission count from
// the limit, only a Go soft limit at half of it.
type memoryLimitFloor struct {
	bytes   int64
	message func(limit resource.Quantity) string
}

// validateContainerResources checks a container's merged requests and limits,
// the operator's defaults with the CR's keys over them, against the shape the
// API server, the kubelet and GKE Autopilot accept. The caller passes the
// accepted resource names, the per-container message set, and an optional
// memory floor. The refusals: claims (the pods declare none); any name outside
// accepted; a negative quantity, a zero limit, a byte count beyond an int64,
// or a CPU quantity whose millicore value is beyond one; a merged memory limit
// under the floor, when a floor is set; and a request above its limit on any
// name. The warnings, both for GKE Autopilot: a requests pair outside the
// memory-per-vCPU band it admits unchanged, and a cpu or memory limit set
// without the same key under requests, which Autopilot without bursting clamps
// to the request. A quantity already refused draws no further comparison or
// warning that would only restate it. The caller returns early when the CR
// carries no override, so the defaults never reach here.
func validateContainerResources(override, merged corev1.ResourceRequirements, path *field.Path, accepted []corev1.ResourceName, msgs containerResourceMessages, floor *memoryLimitFloor) (field.ErrorList, admission.Warnings) {
	var errs field.ErrorList
	var warnings admission.Warnings

	if len(override.Claims) > 0 {
		errs = append(errs, field.Forbidden(path.Child(credentialProxyClaimsField), msgs.claims))
	}

	sides := []struct {
		name string
		list corev1.ResourceList
	}{{credentialProxyRequestsField, merged.Requests}, {credentialProxyLimitsField, merged.Limits}}

	// A quantity refused on its own is left out of the comparisons below,
	// which would only restate it.
	refused := map[string]bool{}
	for _, side := range sides {
		for _, name := range sortedResourceNames(side.list) {
			quantity := side.list[name]
			at := boundedResourceFieldPath(path, side.name, name)
			if !slices.Contains(accepted, name) {
				errs = append(errs, field.Forbidden(at, msgs.resourceName))
				refused[at.String()] = true
				continue
			}
			var msg string
			switch {
			case quantity.Sign() < 0:
				msg = msgs.negative
			case quantity.IsZero() && side.name == credentialProxyLimitsField:
				msg = msgs.zeroLimit
			case slices.Contains(byteCountResources, name) && quantity.Cmp(maxByteCount) > 0:
				msg = fmt.Sprintf(msgs.unrepresentableFmt, int64(math.MaxInt64))
			case name == corev1.ResourceCPU && quantity.Cmp(*maxCPUMilli) > 0:
				msg = fmt.Sprintf(msgs.unrepresentableCPUFmt, int64(math.MaxInt64))
			default:
				continue
			}
			errs = append(errs, field.Invalid(at, quantity.String(), msg))
			refused[at.String()] = true
		}
	}

	if floor != nil {
		limitPath := boundedResourceFieldPath(path, credentialProxyLimitsField, corev1.ResourceMemory)
		limit := merged.Limits[corev1.ResourceMemory]
		if !refused[limitPath.String()] && limit.CmpInt64(floor.bytes) < 0 {
			errs = append(errs, field.Invalid(limitPath, limit.String(), floor.message(limit)))
			refused[limitPath.String()] = true
		}
	}

	for _, name := range sortedResourceNames(merged.Requests) {
		request := merged.Requests[name]
		limit, hasLimit := merged.Limits[name]
		requestPath := boundedResourceFieldPath(path, credentialProxyRequestsField, name)
		limitPath := boundedResourceFieldPath(path, credentialProxyLimitsField, name)
		if !hasLimit || refused[requestPath.String()] || refused[limitPath.String()] || request.Cmp(limit) <= 0 {
			continue
		}
		// The defaults are consistent with each other, so at least one side of
		// a crossed pair is the override's; the error goes on that side, and
		// says when the other is the operator's.
		_, overrodeRequest := override.Requests[name]
		_, overrodeLimit := override.Limits[name]
		refused[requestPath.String()] = true
		refused[limitPath.String()] = true
		switch {
		case overrodeRequest && overrodeLimit:
			errs = append(errs, field.Invalid(requestPath, request.String(),
				fmt.Sprintf(msgs.crossedBesideFmt, limit.String(), name)))
		case overrodeRequest:
			errs = append(errs, field.Invalid(requestPath, request.String(),
				fmt.Sprintf(msgs.crossedDefLimitFmt, limit.String(), name, name)))
		default:
			errs = append(errs, field.Invalid(limitPath, limit.String(),
				fmt.Sprintf(msgs.crossedDefRequestFmt, request.String(), name, name)))
		}
	}

	// The band applies to the requests pair only.
	cpu, hasCPU := merged.Requests[corev1.ResourceCPU]
	memory, hasMemory := merged.Requests[corev1.ResourceMemory]
	cpuPath := boundedResourceFieldPath(path, credentialProxyRequestsField, corev1.ResourceCPU)
	memoryPath := boundedResourceFieldPath(path, credentialProxyRequestsField, corev1.ResourceMemory)
	if hasCPU && hasMemory && cpu.Sign() > 0 && memory.Sign() > 0 &&
		!refused[cpuPath.String()] && !refused[memoryPath.String()] {
		low, high := cpu.DeepCopy(), cpu.DeepCopy()
		low.Mul(autopilotMinMemoryBytesPerVCPU)
		high.Mul(autopilotMaxMemoryBytesPerVCPU)
		if memory.Cmp(low) < 0 || memory.Cmp(high) > 0 {
			gibPerVCPU := memory.AsApproximateFloat64() / cpu.AsApproximateFloat64() / float64(bytesPerGiB)
			warnings = append(warnings, fmt.Sprintf(msgs.requestsBandFmt,
				path.Child(credentialProxyRequestsField), memory.String(), cpu.String(), gibPerVCPU,
				autopilotMinMemoryBytesPerVCPU/bytesPerGiB, float64(autopilotMaxMemoryBytesPerVCPU)/float64(bytesPerGiB)))
		}
	}

	// A limit the override sets without its request: Autopilot without
	// bursting replaces it with the request.
	for _, name := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
		limit, hasLimit := override.Limits[name]
		if _, hasRequest := override.Requests[name]; !hasLimit || hasRequest {
			continue
		}
		limitPath := boundedResourceFieldPath(path, credentialProxyLimitsField, name)
		request := merged.Requests[name]
		if refused[limitPath.String()] || limit.Cmp(request) == 0 {
			continue
		}
		requestKey := credentialProxyRequestsField + credentialProxyKeySeparator + string(name)
		warnings = append(warnings, fmt.Sprintf(msgs.limitWithoutRequestFmt,
			limitPath, requestKey, request.String(), requestKey))
	}
	return errs, warnings
}

// credentialProxyMessages is the credential-proxy container's wording for
// validateContainerResources.
var credentialProxyMessages = containerResourceMessages{
	resourceName:           credentialProxyResourceNameRefusal,
	claims:                 credentialProxyClaimsRefusal,
	negative:               credentialProxyNegativeRefusal,
	zeroLimit:              credentialProxyZeroLimitRefusal,
	unrepresentableFmt:     credentialProxyUnrepresentableFmt,
	unrepresentableCPUFmt:  credentialProxyUnrepresentableCPUFmt,
	crossedBesideFmt:       credentialProxyCrossedBesideFmt,
	crossedDefLimitFmt:     credentialProxyCrossedDefLimitFmt,
	crossedDefRequestFmt:   credentialProxyCrossedDefRequestFmt,
	requestsBandFmt:        credentialProxyRequestsBandWarningFmt,
	limitWithoutRequestFmt: credentialProxyLimitWithoutRequestFmt,
}

// ValidateCredentialProxyResources checks spec.deployment.credentialProxy.resources
// on the result the operator renders (resolveCredentialProxyResources). The
// admission webhook calls it at apply, and the reconciler calls it before
// writing the proxy Deployment, so an install without the webhook refuses the
// same override rather than rendering it. It adds one refusal to the shared
// validateContainerResources: a memory limit under the floor at which the
// broker's child memory budget admits two commands.
func ValidateCredentialProxyResources(deployment *agentv1alpha1.DeploymentSpec, path *field.Path) (field.ErrorList, admission.Warnings) {
	if deployment == nil || deployment.CredentialProxy == nil || deployment.CredentialProxy.Resources == nil {
		return nil, nil
	}
	floorBytes := credentialProxyMinimumMemoryLimitBytes(credentialProxyOutputCapBytes)
	floor := &memoryLimitFloor{
		bytes: floorBytes,
		message: func(limit resource.Quantity) string {
			return fmt.Sprintf(credentialProxyFloorRefusalFmt, limit.String(), floorBytes/bytesPerMiB, credentialProxyMinimumAdmittedRequests)
		},
	}
	return validateContainerResources(*deployment.CredentialProxy.Resources, resolveCredentialProxyResources(deployment),
		path, acceptedContainerResourceNames, credentialProxyMessages, floor)
}

// sortedResourceNames is list's keys in order, so the errors a CR gets back
// read the same on every apply.
func sortedResourceNames(list corev1.ResourceList) []corev1.ResourceName {
	names := make([]corev1.ResourceName, 0, len(list))
	for name := range list {
		names = append(names, name)
	}
	slices.Sort(names)
	return names
}

// credentialProxyResourcesRefusal is the reconciler's reading of
// ValidateCredentialProxyResources: the first refusal with the count of the
// rest, within credentialProxyRefusalMessageBudget, or "" when the override is
// valid. Warnings are not refusals; the
// caller logs them.
func credentialProxyResourcesRefusal(agent *agentv1alpha1.PlatformAgent) (string, admission.Warnings) {
	errs, warnings := ValidateCredentialProxyResources(agent.Spec.Deployment, credentialProxyResourcesPath)
	return boundCredentialProxyRefusal(errs), warnings
}

// boundCredentialProxyRefusal is errs' first refusal with the count of the
// rest, cut to credentialProxyRefusalMessageBudget, or "" for none. The cut is
// a backstop: boundedResourceFieldPath already bounds the one part of a
// refusal the author controls the length of.
func boundCredentialProxyRefusal(errs field.ErrorList) string {
	if len(errs) == 0 {
		return ""
	}
	refusal := errs[0].Error()
	more := ""
	if len(errs) > 1 {
		more = fmt.Sprintf(credentialProxyRefusalMoreFmt, len(errs)-1)
	}
	if room := credentialProxyRefusalMessageBudget - len(more); len(refusal) > room {
		refusal = truncateToValidUTF8(refusal, room-len(credentialProxyRefusalEllipsis)) + credentialProxyRefusalEllipsis
	}
	return refusal + more
}
