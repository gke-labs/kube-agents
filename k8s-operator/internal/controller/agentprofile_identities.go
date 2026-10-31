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
	"os"
	"regexp"
	"sort"
	"strings"

	"k8s.io/apimachinery/pkg/util/validation"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The bus identities AgentProfiles add to the map, and the operator's own.
//
// "Profile" in this file is the AgentProfile resource (the A2A side's profile),
// never a Hermes profile directory.
//
// A profile's entry is not a static grant set. Every pod of a profile publishes
// as the profile, but each needs its own consumers and inbox, so the entry
// narrows on "profile": it carries the profile name and the profile's topics,
// and the callout derives the subjects from those plus the attested pod name
// (a2a/authcallout/profile_narrowing.go). The map therefore holds no subject a
// profile pod can reach, exactly as for the session entry.
//
// The operator's entry IS static, and it is the narrowest principal in the map:
// publish on the directory, read the directory, its own inbox. It exists to
// publish profile cards and their tombstones, which the spec gives to the
// operator because a card is presence derived from desired state, and the
// operator is the one component that holds the desired state.

const (
	// a2aNarrowingProfile is the callout's NarrowingProfile, duplicated here
	// because the modules cannot import each other; the fixture contract test
	// holds the two spellings equal.
	a2aNarrowingProfile = "profile"

	// agentProfileServiceAccountPrefix names the ServiceAccount the operator
	// creates for a profile with no spec.identity. The prefix keeps the name
	// out of the space the PlatformAgent's own ServiceAccounts use
	// (<agent>-…), so a profile name cannot collide with one by construction.
	agentProfileServiceAccountPrefix = "agentprofile-"

	// agentProfileMapUserPrefix names a profile's entry in the map. The user
	// a pod is minted as is its pod name; this is only the entry's label, and
	// it must be unique in the map.
	agentProfileMapUserPrefix = "profile-"

	// agentProfileNameMax keeps a profile's map user at the default tier,
	// agentProfileMapUserPrefix plus the name plus "-read", within one
	// 63-character DNS-1123 label. A profile that allows unattended has six
	// characters fewer; the CRD's length rule says both numbers.
	agentProfileNameMax = 63 - len(agentProfileMapUserPrefix) - len("-"+agentv1alpha1.AgentProfileTierRead)

	// operatorNamespaceEnvVar and operatorServiceAccountEnvVar are the
	// manager's own namespace and ServiceAccount, by the downward API. Both
	// are needed to key the operator's map entry; with either unset the
	// operator renders no entry for itself and publishes no cards, and the
	// AgentProfile's CardPublished condition says so.
	operatorNamespaceEnvVar      = OperatorNamespaceEnv
	operatorServiceAccountEnvVar = "OPERATOR_SERVICE_ACCOUNT"

	// a2aOperatorBusClientLabel marks the manager's pod as a bus client, so
	// the NATS fence can admit it from the operator's namespace. The chart
	// and the kustomize manager both stamp it.
	a2aOperatorBusClientLabel      = "kubeagents.x-k8s.io/a2a-bus-client"
	a2aOperatorBusClientLabelValue = "operator"

	// optionalHermesProfileTSE is the built-in optional GKE Support Agent
	// Hermes specialist template baked into the platform-agent image (/opt/tse-template).
	optionalHermesProfileTSE = "tse"
)

// agentProfileTopicRE is the CRD's topic-grant pattern, rechecked when the map
// is rendered: a CRD older than the operator, or one edited on the cluster, is
// not where the map's safety should rest.
var agentProfileTopicRE = regexp.MustCompile(agentv1alpha1.AgentProfileTopicPattern)

// isDNS1123LabelToken is the subject-token rule: a DNS-1123 label, which is
// dot-free by definition.
func isDNS1123LabelToken(s string) bool {
	return len(validation.IsDNS1123Label(s)) == 0
}

// operatorBusPrincipal is the manager's own ServiceAccount, as the downward API
// reports it. ok is false when the manager was deployed without the two
// variables, which is an install older than profiles, and when either is not
// the shape the downward API yields (a DNS-1123 label namespace and a
// DNS-1123 subdomain ServiceAccount). The pair keys a map entry and a fence
// selector shared by every PlatformAgent, so a hand-edited value that is
// neither renders no operator principal instead of failing every render.
func operatorBusPrincipal() (namespace, serviceAccount string, ok bool) {
	namespace = os.Getenv(operatorNamespaceEnvVar)
	serviceAccount = os.Getenv(operatorServiceAccountEnvVar)
	ok = isDNS1123LabelToken(namespace) && len(validation.IsDNS1123Subdomain(serviceAccount)) == 0
	return namespace, serviceAccount, ok
}

// agentProfileServiceAccountName is the ServiceAccount a profile's pods run as
// at one tier, in the profile namespace (#3655): the one spec.identity names,
// which the CRD allows only for a profile with one tier, or the one the
// operator creates per tier.
func agentProfileServiceAccountName(p *agentv1alpha1.AgentProfile, tier string) string {
	if p.Spec.Identity.ServiceAccountName != "" {
		return p.Spec.Identity.ServiceAccountName
	}
	return agentProfileServiceAccountPrefix + p.Name + "-" + tier
}

// agentProfileMapUser is a (profile, tier)'s map entry user: its label in
// the map, which must be unique and one DNS-1123 label.
func agentProfileMapUser(p *agentv1alpha1.AgentProfile, tier string) string {
	return agentProfileMapUserPrefix + p.Name + "-" + tier
}

// agentProfileTierIdentity is one (profile, tier)'s ServiceAccount.
type agentProfileTierIdentity struct {
	tier           string
	serviceAccount string
}

// reservedProfileServiceAccounts are the ServiceAccounts a profile may not run
// as. Each is an identity the operator already renders for something else, and
// two failure modes make naming one a refusal rather than a choice:
//
//   - The map is keyed on the ServiceAccount, and a duplicate key makes the
//     callout refuse the whole map (and keep serving the previous one), so
//     one profile naming the session SA would freeze every identity change
//     behind it.
//   - The ones not in the map carry real authority: the agent's own
//     ServiceAccount, the callout's (TokenReview), the gateway's (pod create).
//     A profile is not the route to borrowing them.
//
// Each is reserved whether or not this agent renders it now (the session
// account only on next, the MCP account only under A2A_MCP_DOOR): a flip or a
// flag turned on later must not find a profile or an override already holding
// the name.
//
// `default` is reserved because every pod in the namespace that names no
// ServiceAccount runs as it, so a profile on `default` would hand its bus
// identity to all of them.
func reservedProfileServiceAccounts(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return map[string]string{
		"default":                             "the namespace's default ServiceAccount, which every pod naming none runs as",
		agentServiceAccountName(agent):        "the credential broker's ServiceAccount, which carries the cloud binding",
		gatewayServiceAccountName(agent):      "the platform agent gateway's own ServiceAccount",
		a2aSessionServiceAccountName(agent):   "the gateway's session pods' ServiceAccount",
		a2aProvisionServiceAccountName(agent): "the bus provisioner's ServiceAccount",
		a2aGatewayName(agent):                 "the A2A gateway's ServiceAccount",
		a2aVerifierName(agent):                "the capability verifier's ServiceAccount",
		a2aCalloutName(agent):                 "the auth callout's ServiceAccount",
		shellSandboxServiceAccountName(agent): "the shell sandbox's ServiceAccount",
		a2aMCPServiceAccountName(agent):       "the A2A MCP server's ServiceAccount, the broker's mcp caller",
	}
}

// agentProfileResolution is what the operator decided about one profile's
// identity: the ServiceAccount it runs as, or why it renders nothing.
type agentProfileResolution struct {
	// identities are the profile's (tier, ServiceAccount) pairs, one per
	// allowed tier, in allowedTiers order.
	identities []agentProfileTierIdentity
	refused    error
	// reason is the IdentityReady condition reason for a refusal: the
	// profile itself is malformed, or its ServiceAccount is not usable.
	reason string
}

// resolveAgentProfileIdentities decides every profile's ServiceAccount at once,
// because two of the refusals are relative: two profiles naming one
// ServiceAccount would be two map entries under one key, so the second by name
// is refused and the first keeps it. Profiles are taken oldest first (name
// breaks ties), so an incumbent keeps its claim and the answer does not depend
// on list order. A terminating profile still holds its
// identity: its pods may still be running, and the finalizer removes the entry
// only after its card is tombstoned.
func resolveAgentProfileIdentities(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) map[string]agentProfileResolution {
	reserved := reservedProfileServiceAccounts(agent)
	// Every key the PlatformAgent's own principals already hold in the map,
	// the operator's among them when it runs in the agent's namespace. A
	// profile on one of these would be a duplicate key, which fails the
	// whole map, so the check is against the rendered set rather than a
	// list someone has to remember to extend.
	mapKeys := map[string]string{}
	for _, id := range calloutIdentities(agent) {
		mapKeys[id.serviceAccount] = id.user
	}
	// Oldest first, so an incumbent keeps a contested ServiceAccount and a
	// newcomer naming it is the one refused; name breaks ties, so the answer
	// never depends on list order.
	sorted := append([]agentv1alpha1.AgentProfile(nil), profiles...)
	sort.Slice(sorted, func(i, j int) bool {
		ti, tj := sorted[i].CreationTimestamp, sorted[j].CreationTimestamp
		if !ti.Equal(&tj) {
			return ti.Before(&tj)
		}
		return sorted[i].Name < sorted[j].Name
	})

	out := make(map[string]agentProfileResolution, len(sorted))
	claimedBy := map[string]string{}
	profilesNS := profileNamespaceName(agent)
	for i := range sorted {
		p := &sorted[i]
		out[p.Name] = resolveOneAgentProfile(p, agent, reserved, mapKeys, claimedBy, profilesNS)
	}
	return out
}

// resolveOneAgentProfile decides one profile's identities, recording each
// ServiceAccount it claims in claimedBy, so a later profile naming one is
// refused. A profile is refused whole: one tier it can't render leaves the
// profile without identities rather than half-rendered.
func resolveOneAgentProfile(p *agentv1alpha1.AgentProfile, agent *agentv1alpha1.PlatformAgent, reserved, mapKeys, claimedBy map[string]string, profilesNS string) agentProfileResolution {
	named := p.Spec.Identity.ServiceAccountName
	tiers := p.AllowedTierList()
	switch {
	case !isDNS1123LabelToken(p.Name):
		// The CRD refuses these too. Refused here as well so that a
		// profile admitted by an older or edited CRD drops out of the map
		// instead of failing the whole render.
		return agentProfileResolution{refused: fmt.Errorf("profile name %q is not a dot-free DNS-1123 label", p.Name), reason: reasonAgentProfileInvalid}
	case malformedTopicGrant(p) != "":
		return agentProfileResolution{refused: fmt.Errorf("topic grant %q is not shared.{topic} or agent.{agent}.{topic}", malformedTopicGrant(p)), reason: reasonAgentProfileInvalid}
	case foreignPublishTopic(p) != "":
		return agentProfileResolution{refused: fmt.Errorf("publish topic %q is another agent's: an agent-scoped topic has one writer, the agent it names, so a profile publishes only agent.%s.<topic>", foreignPublishTopic(p), p.Name), reason: reasonAgentProfileInvalid}
	case p.Name == a2aBridgeAddressee:
		// The CRD refuses this name too; the operator does not rely on
		// admission alone, because CRD validation can be bypassed by an
		// older CRD left on the cluster.
		return agentProfileResolution{refused: fmt.Errorf("profile name %q is the Hermes bridge's addressee", p.Name), reason: reasonAgentProfileInvalid}
	case named != "" && len(tiers) > 1:
		// The CRD refuses this too: one named ServiceAccount can't be the
		// identity of more than one tier.
		return agentProfileResolution{refused: fmt.Errorf("spec.identity.serviceAccountName names one ServiceAccount, so it can't serve the %d tiers allowedTiers lists; leave it unset or allow one tier", len(tiers)), reason: reasonAgentProfileRefused}
	case named != "" && len(validation.IsDNS1123Subdomain(named)) > 0:
		// The CRD's pattern refuses this too; rechecked for the same reason
		// as the name, since a malformed name makes a map key the whole
		// render refuses.
		return agentProfileResolution{refused: fmt.Errorf("serviceAccountName %q is not a DNS-1123 subdomain", named), reason: reasonAgentProfileRefused}
	case named != "" && strings.HasPrefix(named, agentProfileServiceAccountPrefix):
		// The operator-created ServiceAccounts belong to the profile they
		// are named for. Without this a profile that sorts first could name
		// another's and take its map entry over.
		return agentProfileResolution{refused: fmt.Errorf("serviceAccountName %q is an operator-created AgentProfile ServiceAccount", named), reason: reasonAgentProfileRefused}
	}
	var ids []agentProfileTierIdentity
	for _, tier := range tiers {
		sa := agentProfileServiceAccountName(p, tier)
		key := a2aServiceAccountName(profilesNS, sa)
		switch {
		case !isDNS1123LabelToken(agentProfileMapUser(p, tier)):
			// The map entry's user is the prefix, the name and the tier,
			// and the callout refuses a user that is not one DNS-1123
			// label, so a name too long for one of its tiers would fail
			// the whole map.
			return agentProfileResolution{refused: fmt.Errorf("profile name %q is too long for tier %s: its bus identity %q would not be a DNS-1123 label (at most 63 characters)", p.Name, tier, agentProfileMapUser(p, tier)), reason: reasonAgentProfileInvalid}
		case reserved[sa] != "":
			return agentProfileResolution{refused: fmt.Errorf("serviceAccountName %q is %s", sa, reserved[sa]), reason: reasonAgentProfileRefused}
		case mapKeys[key] != "":
			return agentProfileResolution{refused: fmt.Errorf("serviceAccountName %q is the bus principal %q's", sa, mapKeys[key]), reason: reasonAgentProfileRefused}
		case claimedBy[sa] != "":
			return agentProfileResolution{refused: fmt.Errorf("serviceAccountName %q is already AgentProfile %q's", sa, claimedBy[sa]), reason: reasonAgentProfileRefused}
		}
		ids = append(ids, agentProfileTierIdentity{tier: tier, serviceAccount: sa})
	}
	for _, id := range ids {
		claimedBy[id.serviceAccount] = p.Name
	}
	return agentProfileResolution{identities: ids}
}

// serviceAccountList is a resolution's ServiceAccounts, for status and
// messages: "a, b" in tier order.
func (r agentProfileResolution) serviceAccountList() string {
	names := make([]string, 0, len(r.identities))
	for _, id := range r.identities {
		names = append(names, id.serviceAccount)
	}
	return strings.Join(names, ", ")
}

// malformedTopicGrant returns the first topic grant the CRD's pattern refuses,
// or "".
func malformedTopicGrant(p *agentv1alpha1.AgentProfile) string {
	for _, t := range append(append([]string(nil), p.Spec.Bus.PublishTopics...), p.Spec.Bus.SubscribeTopics...) {
		if !agentProfileTopicRE.MatchString(t) {
			return t
		}
	}
	return ""
}

// foreignPublishTopic returns the first agent-scoped publish topic that is not
// the profile's own, or "".
func foreignPublishTopic(p *agentv1alpha1.AgentProfile) string {
	for _, t := range p.Spec.Bus.PublishTopics {
		if !ownsAgentTopic(p.Name, t) {
			return t
		}
	}
	return ""
}

// ownsAgentTopic reports whether a publish topic is the profile's to write: a
// shared topic, or agent.<profile>.<topic>. The callout applies the same rule
// at parse and at mint. The CRD does not: a root-level CEL rule comparing each
// topic with metadata.name exceeds the API server's rule cost budget, so the
// CRD would not install. An offending profile is admitted and then refused
// here, with a condition.
func ownsAgentTopic(profile, topic string) bool {
	return !strings.HasPrefix(topic, "agent.") || strings.HasPrefix(topic, "agent."+profile+".")
}

// agentProfileMapEntries renders one narrowed map entry per (profile, tier)
// that resolved, in name then tier order, keyed on the ServiceAccount in the
// profile namespace. Refused profiles are left out, never rendered with a
// fault: one bad profile must not fail the map for every other principal.
func agentProfileMapEntries(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) []a2aAuthMapIdentity {
	resolved := resolveAgentProfileIdentities(agent, profiles)
	sorted := append([]agentv1alpha1.AgentProfile(nil), profiles...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Name < sorted[j].Name })

	var out []a2aAuthMapIdentity
	for i := range sorted {
		p := &sorted[i]
		r := resolved[p.Name]
		if r.refused != nil {
			continue
		}
		for _, id := range r.identities {
			entry := a2aAuthMapIdentity{
				ServiceAccount: a2aServiceAccountName(profileNamespaceName(agent), id.serviceAccount),
				User:           agentProfileMapUser(p, id.tier),
				Account:        a2aAccountApp,
				Narrowing:      a2aNarrowingProfile,
				Profile:        p.Name,
				Tier:           id.tier,
			}
			if len(p.Spec.Bus.PublishTopics) > 0 || len(p.Spec.Bus.SubscribeTopics) > 0 {
				entry.Topics = &a2aAuthMapTopics{
					Publish:   append([]string(nil), p.Spec.Bus.PublishTopics...),
					Subscribe: append([]string(nil), p.Spec.Bus.SubscribeTopics...),
				}
			}
			out = append(out, entry)
		}
	}
	return out
}

// isOptionalHermesProfileName reports whether name matches a built-in optional
// Hermes specialist template baked into the platform-agent image (/opt/<name>-template).
// Ordinary AgentProfiles (such as `chat` or custom A2A bus profiles like `auditor`)
// describe A2A bus identities only and do not scaffold a Hermes profile on the
// gateway pod, so creating or deleting them does not mutate KUBE_AGENTS_ENABLED_PROFILES
// or restart the gateway Deployment.
func isOptionalHermesProfileName(name string) bool {
	return name == optionalHermesProfileTSE
}

// enabledAgentProfileNames returns the sorted names of bound, non-terminating
// AgentProfiles whose identity resolution succeeded and whose name matches a
// built-in optional Hermes specialist template (currently `tse`). These are
// passed to the gateway pod via KUBE_AGENTS_ENABLED_PROFILES in `mode: next`
// so the entrypoint scaffolds and syncs their Hermes profile directories.
func enabledAgentProfileNames(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) []string {
	if len(profiles) == 0 {
		return nil
	}
	resolutions := resolveAgentProfileIdentities(agent, profiles)
	var names []string
	for i := range profiles {
		p := &profiles[i]
		if !p.DeletionTimestamp.IsZero() || !isOptionalHermesProfileName(p.Name) {
			continue
		}
		if res, ok := resolutions[p.Name]; ok && res.refused == nil {
			names = append(names, p.Name)
		}
	}
	sort.Strings(names)
	return names
}

// ReservedServiceAccountOverride returns a refusal message when
// spec.security.serviceAccountName names a ServiceAccount the operator creates
// for another of this agent's workloads, or "" when it does not. The broker
// runs as that name with its Workload Identity binding, so a collision would
// hand the other workload (the gateway above all) the broker's cloud identity,
// and two reconcilers would fight over one ServiceAccount. The webhook refuses
// it at admission; the controller refuses it too, before any ServiceAccount is
// written, because the chart ships the webhook off and it fails open.
func ReservedServiceAccountOverride(agent *agentv1alpha1.PlatformAgent) string {
	if agent.Spec.Security == nil || agent.Spec.Security.ServiceAccountName == "" {
		return ""
	}
	sa := agent.Spec.Security.ServiceAccountName
	for _, minted := range operatorServiceAccountNames(agent) {
		if sa == minted {
			return fmt.Sprintf("%q is a ServiceAccount the operator creates for another of this agent's workloads; the broker's identity needs a name of its own", sa)
		}
	}
	return ""
}

// operatorServiceAccountNames are the ServiceAccounts the operator mints for its
// own workloads, other than the credential broker's. spec.security.serviceAccountName
// names the broker's identity, and naming one of these instead would put the
// broker (and its cloud binding) on another workload's ServiceAccount;
// ReservedServiceAccountOverride refuses it.
func operatorServiceAccountNames(agent *agentv1alpha1.PlatformAgent) []string {
	// Computed with the override cleared: the broker's own name is the one the
	// caller is checking, and leaving it set would take it out of the list.
	plain := agent.DeepCopy()
	if plain.Spec.Security != nil {
		plain.Spec.Security.ServiceAccountName = ""
	}
	names := []string{}
	for name := range reservedProfileServiceAccounts(plain) {
		if name != agentServiceAccountName(plain) && name != "default" {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names
}
