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

	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The PlatformAgent reconciler's half of AgentProfile ("profile" here is the
// A2A AgentProfile resource, not a Hermes profile directory): it reads the
// profiles bound to the agent and renders their entries into the one identity
// map the callout watches. The AgentProfile reconciler owns the rest (the
// ServiceAccount, the card, the finalizer, the status). The map stays here
// because it is one object built from one agent's whole principal set; a
// second writer would be two controllers racing on one ConfigMap.

// boundAgentProfiles lists the AgentProfiles this agent renders identities for:
// every profile in its namespace, provided the agent is the only PlatformAgent
// there. The spec's field table has no agentRef, so the binding is the
// namespace; with two agents in one namespace (possible only with the
// singleton webhook off) a profile cannot say which it belongs to, and
// neither renders it. A cluster without the AgentProfile CRD has no profiles.
func boundAgentProfiles(ctx context.Context, c client.Reader, agent *agentv1alpha1.PlatformAgent) ([]agentv1alpha1.AgentProfile, error) {
	var agents agentv1alpha1.PlatformAgentList
	if err := c.List(ctx, &agents, client.InNamespace(agent.Namespace)); err != nil {
		return nil, err
	}
	if len(agents.Items) != 1 {
		return nil, nil
	}
	var profiles agentv1alpha1.AgentProfileList
	if err := c.List(ctx, &profiles, client.InNamespace(agent.Namespace)); err != nil {
		if isCRDNotInstalledError(err) {
			logf.FromContext(ctx).V(1).Info("AgentProfile CRD is not installed on cluster; rendering no profile identities", "namespace", agent.Namespace)
			return nil, nil
		}
		return nil, err
	}
	return profiles.Items, nil
}
