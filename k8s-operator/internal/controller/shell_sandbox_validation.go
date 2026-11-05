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
	"k8s.io/apimachinery/pkg/util/validation/field"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The shell container's wording for the refusals and warnings that name it;
// the container-agnostic ones reuse the credential-proxy constants.
const (
	shellSandboxResourceNameRefusal    = "the shell container accepts cpu, memory and ephemeral-storage only and declares no other resource; any other name is one the API server refuses as written (an extended resource without its limit, hugepages whose request and limit differ) or the pod has no use for" // #nosec G101 -- Error message, not a credential
	shellSandboxClaimsRefusal          = "the shell sandbox pod declares no resourceClaims, so a claim named here cannot take effect"                                                                                                                                                                              // #nosec G101 -- Error message, not a credential
	shellSandboxUnrepresentableFmt     = "is not a representable byte count: it exceeds the %d bytes an int64 holds, which is what the kubelet enforces the shell's memory limit in"                                                                                                                               // #nosec G101 -- Error message, not a credential
	shellSandboxLimitWithoutRequestFmt = "%s is set without %s; GKE Autopilot without bursting sets a container's limits equal to its requests, so there the shell runs at the %s request and this limit has no effect — set %s to the same value, or enable bursting"                                             // #nosec G101 -- Warning text, not a credential
)

// shellSandboxMessages is the shell container's wording for
// validateContainerResources. The container-agnostic refusals (negative, zero
// limit, unrepresentable CPU, the crossed request/limit pair) and the Autopilot
// band warning reuse the credential-proxy constants rather than restate them.
var shellSandboxMessages = containerResourceMessages{
	resourceName:           shellSandboxResourceNameRefusal,
	claims:                 shellSandboxClaimsRefusal,
	negative:               credentialProxyNegativeRefusal,
	zeroLimit:              credentialProxyZeroLimitRefusal,
	unrepresentableFmt:     shellSandboxUnrepresentableFmt,
	unrepresentableCPUFmt:  credentialProxyUnrepresentableCPUFmt,
	crossedBesideFmt:       credentialProxyCrossedBesideFmt,
	crossedDefLimitFmt:     credentialProxyCrossedDefLimitFmt,
	crossedDefRequestFmt:   credentialProxyCrossedDefRequestFmt,
	requestsBandFmt:        credentialProxyRequestsBandWarningFmt,
	limitWithoutRequestFmt: shellSandboxLimitWithoutRequestFmt,
}

// shellSandboxResourcesPath is where the override sits on the CR.
var shellSandboxResourcesPath = field.NewPath("spec", "harness", "experimental", "shellSandbox", "resources")

// ValidateShellSandboxResources checks
// spec.harness.experimental.shellSandbox.resources on the merged result the
// operator renders (resolveShellSandboxResources). It runs the shared
// validateContainerResources with no memory floor: nothing in the shell derives
// an admission count from its limit, so any positive limit at or above the
// request is accepted. The webhook calls it at apply and the reconciler before
// writing the StatefulSet, so an install without the webhook refuses the same
// override rather than rendering it.
func ValidateShellSandboxResources(agent *agentv1alpha1.PlatformAgent, path *field.Path) (field.ErrorList, admission.Warnings) {
	spec := shellSandboxSpec(agent)
	if spec == nil || spec.Resources == nil {
		return nil, nil
	}
	return validateContainerResources(*spec.Resources, resolveShellSandboxResources(agent),
		path, acceptedContainerResourceNames, shellSandboxMessages, nil)
}

// shellSandboxResourcesRefusal is the reconciler's reading of
// ValidateShellSandboxResources: the first refusal with the count of the rest
// within the shared budget, or "" when the override is valid. Warnings are not
// refusals; the caller logs them.
func shellSandboxResourcesRefusal(agent *agentv1alpha1.PlatformAgent) (string, admission.Warnings) {
	errs, warnings := ValidateShellSandboxResources(agent, shellSandboxResourcesPath)
	return boundCredentialProxyRefusal(errs), warnings
}
