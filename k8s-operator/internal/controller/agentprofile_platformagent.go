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

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"

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
	var bound []agentv1alpha1.AgentProfile
	for i := range profiles.Items {
		foreign, err := agentProfileServiceAccountIsForeign(ctx, c, &profiles.Items[i])
		if err != nil {
			return nil, err
		}
		if foreign == "" {
			bound = append(bound, profiles.Items[i])
		}
	}
	return bound, nil
}

// agentProfileServiceAccountIsForeign reports whether a profile with no
// spec.identity would land on a ServiceAccount, in the profile namespace, that
// already exists and is not the profile's: one someone created by hand, with
// whatever RoleBindings they gave it. The operator neither adopts it nor
// renders a bus identity for it. It returns the first such name, or "". A
// ServiceAccount spec.identity names is foreign on purpose and not checked.
func agentProfileServiceAccountIsForeign(ctx context.Context, c client.Reader, p *agentv1alpha1.AgentProfile) (string, error) {
	profilesNS := profileNamespaceOf(p)
	if p.Spec.Identity.ServiceAccountName != "" {
		return "", nil
	}
	for _, tier := range p.AllowedTierList() {
		name := agentProfileServiceAccountName(p, tier)
		var sa corev1.ServiceAccount
		err := c.Get(ctx, types.NamespacedName{Namespace: profilesNS, Name: name}, &sa)
		if apierrors.IsNotFound(err) {
			continue
		}
		if err != nil {
			return "", err
		}
		if !agentProfileOwnsServiceAccount(&sa, p) {
			return name, nil
		}
	}
	return "", nil
}

// agentProfileOwnsServiceAccount reports whether a ServiceAccount in the
// profile namespace is this profile's. It can't carry an owner reference (the
// profile is in the agent's namespace, and owner references don't cross
// namespaces), so the operator marks the ones it creates with the profile's
// name label and UID annotation. Whoever can create ServiceAccounts in the
// profile namespace could copy them, which is the same trust the namespace
// already rests on (agentprofile_namespace.go).
func agentProfileOwnsServiceAccount(sa *corev1.ServiceAccount, p *agentv1alpha1.AgentProfile) bool {
	return sa.Labels[agentProfileNameLabel] == p.Name && p.UID != "" && sa.Annotations[agentProfileUIDAnnotation] == string(p.UID)
}
