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
	"sort"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// AgentProfile workers as credential-broker callers (#3889). A profile worker's
// kubectl and gcloud are the credential-proxy shim, so its credentialed calls
// reach the shared broker over the ClusterIP, like a session pod's, with a
// projected token for an audience of its own. The broker confers the `profile`
// role by that audience and holds every call to the cluster in the task
// credential the worker presents (credential_proxy.py, task_confinement.py).
//
// What the broker needs from the operator, all rendered here and all reserved
// against spec.deployment.env (mergeCredentialProxyEnv):
//
//   - the profile audience;
//   - the identity map, ServiceAccount to (profile, tier), from the same
//     source the callout's map gets its profile entries
//     (agentProfileMapEntries), because a ServiceAccount name can be user-set
//     and can't be parsed for a profile;
//   - the task-signing public key, the public half of
//     ensureA2ATaskSigningKeySecret's seed, so the broker trusts the key the
//     dispatcher signs with and no other;
//   - the identity map's ServiceAccounts on CREDENTIAL_PROXY_ALLOWED_CALLERS;
//   - an ingress peer for the profile namespace on the broker's fence, and an
//     egress rule to the broker on the profile namespace's.
//
// Nothing renders without a profile identity, so an install with no
// AgentProfile confers no profile role and admits no profile namespace.
const (
	// credentialProxyProfileAudience is the profile workers' audience.
	// Spelled identically in credential_proxy.py, whose default it is.
	credentialProxyProfileAudience = "kubeagents-credential-proxy-profile" // #nosec G101 -- Token audience name, not a credential

	credentialProxyProfileAudienceEnvVar   = "CREDENTIAL_PROXY_PROFILE_AUDIENCE"   // #nosec G101 -- Environment variable name, not a credential
	credentialProxyProfileIdentitiesEnvVar = "CREDENTIAL_PROXY_PROFILE_IDENTITIES" // #nosec G101 -- Environment variable name, not a credential

	// credentialProxyTaskSigningKeysEnvVar is the callout's variable, read
	// the same way on both (authcallout.ParseTaskSigningKeys,
	// task_credential.parse_trusted_keys).
	credentialProxyTaskSigningKeysEnvVar = a2aCalloutTaskSigningKeysEnvVar

	// credentialProxyTaskCredentialMaxLifetimeEnvVar is the lifetime ceiling
	// the callout and the broker share. Not rendered yet: #4121 adds the
	// PlatformAgent field it comes from (the install's maxTaskDeadlineSeconds)
	// and renders it for both. Until then both default to 6h. Reserved now,
	// so a CR can't set a ceiling the operator didn't choose.
	credentialProxyTaskCredentialMaxLifetimeEnvVar = "A2A_TASK_CREDENTIAL_MAX_LIFETIME" // #nosec G101 -- Environment variable name, not hardcoded credentials
)

// credentialProxyProfileIdentity is one profile worker ServiceAccount, as the
// broker's identity map names it.
type credentialProxyProfileIdentity struct {
	// ServiceAccount is the TokenReview username: system:serviceaccount:ns:sa.
	ServiceAccount string
	Profile        string
	Tier           string
}

// credentialProxyProfileConfig is what the broker's profile role is rendered
// from. The zero value renders nothing.
type credentialProxyProfileConfig struct {
	// TaskSigningPublic is the task-signing public key (C...).
	TaskSigningPublic string
	// Identities are the profile worker ServiceAccounts, in map order.
	Identities []credentialProxyProfileIdentity
}

// enabled reports whether there is a profile caller to confer the role on.
func (c credentialProxyProfileConfig) enabled() bool {
	return len(c.Identities) > 0
}

// brokerProfileIdentities is the broker's identity map, from the callout map's
// profile entries: one per (profile, tier) that resolved, refused profiles
// left out exactly as they are there.
func brokerProfileIdentities(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) []credentialProxyProfileIdentity {
	var out []credentialProxyProfileIdentity
	for _, entry := range agentProfileMapEntries(agent, profiles) {
		out = append(out, credentialProxyProfileIdentity{ServiceAccount: entry.ServiceAccount, Profile: entry.Profile, Tier: entry.Tier})
	}
	return out
}

// identitiesJSON is CREDENTIAL_PROXY_PROFILE_IDENTITIES: an object keyed by
// ServiceAccount user, each value {"profile", "tier"}. encoding/json writes map
// keys sorted, so the pod template only changes when an identity does.
func (c credentialProxyProfileConfig) identitiesJSON() string {
	type entry struct {
		Profile string `json:"profile"`
		Tier    string `json:"tier"`
	}
	m := make(map[string]entry, len(c.Identities))
	for _, id := range c.Identities {
		m[id.ServiceAccount] = entry{Profile: id.Profile, Tier: id.Tier}
	}
	// A map of string pairs, which json.Marshal cannot fail on.
	document, _ := json.Marshal(m)
	return string(document)
}

// callers is the identity map's ServiceAccounts, sorted, for
// CREDENTIAL_PROXY_ALLOWED_CALLERS.
func (c credentialProxyProfileConfig) callers() []string {
	seen := map[string]bool{}
	var out []string
	for _, id := range c.Identities {
		if !seen[id.ServiceAccount] {
			seen[id.ServiceAccount] = true
			out = append(out, id.ServiceAccount)
		}
	}
	sort.Strings(out)
	return out
}

// credentialProxyProfileEnv is the broker's profile-role environment, or
// nothing when there is no profile caller. The signing key is a plain value:
// it is the public half, and a reference to its Secret would be a reference to
// the dispatcher's seed.
func credentialProxyProfileEnv(c credentialProxyProfileConfig) []corev1.EnvVar {
	if !c.enabled() {
		return nil
	}
	env := []corev1.EnvVar{
		{Name: credentialProxyProfileAudienceEnvVar, Value: credentialProxyProfileAudience},
		{Name: credentialProxyProfileIdentitiesEnvVar, Value: c.identitiesJSON()},
	}
	// Defensive: credentialProxyProfileConfigFor ensures the key or errors,
	// so an enabled config carries it. Were it empty, the broker would refuse
	// every task credential and its profile callers would run local-only
	// commands.
	if c.TaskSigningPublic != "" {
		env = append(env, corev1.EnvVar{Name: credentialProxyTaskSigningKeysEnvVar, Value: c.TaskSigningPublic})
	}
	// credentialProxyTaskCredentialMaxLifetimeEnvVar: see its declaration.
	return env
}

// credentialProxyProfilePeer admits the profile namespace's pods to the
// broker's credentialed port. Namespace-wide, as the bus and LiteLLM admit
// it (profileNamespacePeer): the profile namespace runs worker pods only, and
// its own fence keeps them to its egress list. TokenReview, the profile
// audience and the identity map are what keep a peer to the profile role.
func credentialProxyProfilePeer(agent *agentv1alpha1.PlatformAgent) networkingv1.NetworkPolicyPeer {
	return profileNamespacePeer(agent)
}

// withCredentialProxyProfilePeer adds the profile namespace to the broker's
// ingress fence when there is a profile caller.
func withCredentialProxyProfilePeer(np *networkingv1.NetworkPolicy, agent *agentv1alpha1.PlatformAgent, c credentialProxyProfileConfig) *networkingv1.NetworkPolicy {
	if c.enabled() && len(np.Spec.Ingress) > 0 {
		np.Spec.Ingress[0].From = append(np.Spec.Ingress[0].From, credentialProxyProfilePeer(agent))
	}
	return np
}

// credentialProxyProfileConfigFor reads what the broker's profile role is
// rendered from: the bound AgentProfiles' identities and the task-signing
// public key. Nothing on an install that doesn't render the A2A stack, whose
// AgentProfiles are unreadable, that has none, or whose profile namespace is
// absent: the same gates reconcileA2A applies to the callout's map, so the two
// maps name the same ServiceAccounts.
//
// The broker renders before reconcileA2A, so this ensures the signing key
// itself; ensureA2ATaskSigningKeySecret creates it once and is a read after.
func (r *PlatformAgentReconciler) credentialProxyProfileConfigFor(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (credentialProxyProfileConfig, error) {
	if !a2aStackRendering(agent) || r.agentProfilesUnreadable {
		return credentialProxyProfileConfig{}, nil
	}
	profiles, err := boundAgentProfiles(ctx, r.Client, agent)
	if err != nil {
		return credentialProxyProfileConfig{}, fmt.Errorf("failed to list AgentProfiles for the credential proxy: %w", err)
	}
	if len(profiles) == 0 {
		return credentialProxyProfileConfig{}, nil
	}
	ready, err := r.profileNamespaceExists(ctx, agent)
	if err != nil {
		return credentialProxyProfileConfig{}, fmt.Errorf("failed to read the profile namespace for the credential proxy: %w", err)
	}
	if !ready {
		return credentialProxyProfileConfig{}, nil
	}
	identities := brokerProfileIdentities(agent, profiles)
	if len(identities) == 0 {
		return credentialProxyProfileConfig{}, nil
	}
	key, err := r.ensureA2ATaskSigningKeySecret(ctx, agent)
	if err != nil {
		return credentialProxyProfileConfig{}, fmt.Errorf("failed to ensure the A2A task-signing key for the credential proxy: %w", err)
	}
	return credentialProxyProfileConfig{TaskSigningPublic: key.Public, Identities: identities}, nil
}
