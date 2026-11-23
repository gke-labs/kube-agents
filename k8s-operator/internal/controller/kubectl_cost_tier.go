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
	"regexp"
	"strings"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The kubectl admission's sizing tier (deploy/sandbox/kubectl-admit.py), read
// once from the CR and forwarded to every pod that runs the admission.
const (
	// envKubectlCostTier names the sizing tier whose column of
	// deploy/sandbox/kubectl-costs.json the kubectl admission charges
	// ("default" when unset). An operator sets it in spec.deployment.env. The
	// shell sandbox gets it under this name, and deploy/sandbox/entrypoint.sh
	// writes it to a root-owned file the admission reads, so a session cannot
	// pick a lighter tier.
	envKubectlCostTier = "KUBEAGENTS_KUBECTL_COST_TIER"
	// a2aKubectlCostTierEnv carries the same tier to the A2A gateway, which
	// sets it on the session pods it spawns under the cluster view.
	a2aKubectlCostTierEnv = "A2A_KUBECTL_COST_TIER"
)

// kubectlCostTierPattern is a tier name the entrypoint will write: a short
// lowercase word, as the cost table's tier keys are.
var kubectlCostTierPattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}$`)

// kubectlCostTier is the agent's tier, normalised: spec.deployment.env's first
// literal KUBEAGENTS_KUBECTL_COST_TIER entry (a valueFrom is never read),
// trimmed and lowercased, and only if it is then a plain tier name. ok is
// false when there is none, and the admission charges its default tier.
func kubectlCostTier(agent *agentv1alpha1.PlatformAgent) (tier string, ok bool) {
	if agent.Spec.Deployment == nil {
		return "", false
	}
	for _, v := range agent.Spec.Deployment.Env {
		if v.Name == envKubectlCostTier && v.ValueFrom == nil {
			tier = strings.ToLower(strings.TrimSpace(v.Value))
			return tier, kubectlCostTierPattern.MatchString(tier)
		}
	}
	return "", false
}
