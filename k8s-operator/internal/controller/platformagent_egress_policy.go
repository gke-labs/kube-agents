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

// Default-deny egress for the agent Pod, and with it the metadata-server deny.
//
// # Why this is one object and not two
//
// The obvious shape is two policies: one that denies the metadata server, one
// that restricts everything else. NetworkPolicy does not work that way.
// Policies selecting the same Pod are additive — the Pod may send whatever the
// union of their rules permits — so a policy that says "all destinations except
// the metadata server" does not subtract anything from a second policy's
// allowlist, it adds the entire internet to it. There is no deny rule in
// NetworkPolicy at all. Denying is what you get by not allowing.
//
// So there is one policy, it is default-deny, and the metadata server is denied
// because it does not appear on the list.
//
// # Why not the "0.0.0.0/0 except 169.254.169.254/32" form
//
// That is what this repository shipped in fb99cd1 and it is unsound twice over
// on GKE, which is the platform this product targets:
//
//   - GKE Dataplane V2 — the default dataplane, and mandatory on Autopilot —
//     documents that "Pod traffic is never covered by an ipBlock rule". A
//     policy whose only peer is 0.0.0.0/0 therefore permits no Pod-to-Pod
//     traffic at all: not kube-dns, not LiteLLM, not the broker. It reads as
//     permissive and behaves as a near-total outage.
//   - The DNAT ordering defeats the except clause on Calico. A Pod's request to
//     169.254.169.254:80 is translated to the node-local gke-metadata-server at
//     169.254.169.252:988 in NAT PREROUTING, before iptables-based policy is
//     evaluated, so the address the policy names is not the address the policy
//     sees. kubernetes/kubernetes#68078 is that bug, reported in 2018 and still
//     the reason "put it in except" is bad advice.
//
// A default-deny allowlist is immune to both: it names Pods with selectors
// rather than CIDRs, and it does not have to predict which address the
// metadata request will have been rewritten to, because neither address is
// permitted.
//
// # What this control does not do
//
// It is a real control in exactly one configuration and the operator says so
// out loud rather than rendering something that looks protective:
//
//   - It requires spec.security.splitCredentialBrokerPod. The broker mints the
//     cloud token from the metadata server — that is its function — and a
//     Pod-level policy cannot tell two containers in one network namespace
//     apart. reconcileAgentEgressPolicy refuses to render in the sidecar
//     layout and reports Degraded.
//   - It does nothing whatsoever on a cluster whose CNI does not enforce
//     NetworkPolicy. The operator cannot detect that: an unenforced policy is
//     accepted by the API server, stored, and returned by kubectl get exactly
//     like an enforced one. There is no field, condition or event to read.
//   - It can be undone from outside. Any other NetworkPolicy in the namespace
//     that selects this Pod and permits wider egress unions with this one.

import (
	"fmt"
	"net/netip"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// egressPolicyAllowlist is the one spec.security.egressPolicy value that
	// renders anything.
	egressPolicyAllowlist = "Allowlist"

	// agentEgressPolicyNameSuffix keeps the name the previous two-pod layout
	// used, and that invariant C5 and deleteLegacyCredentialIsolationResources
	// both name. Rendering under a new name would leave any hand-applied copy
	// of the old one in place beside this one, and since policies are additive
	// the more permissive of the two would win.
	agentEgressPolicyNameSuffix = "-sandbox-metadata-deny"

	// nodeLocalDNSCacheIP is where NodeLocal DNSCache listens when it is
	// deployed. Link-local, but not the metadata server: allowing it grants
	// name resolution and nothing else.
	nodeLocalDNSCacheIP = "169.254.20.10/32"
)

// metadataServerAddresses are every address a request for cloud credentials
// can arrive at, and none of them may appear in a rendered egress rule.
//
//   - 169.254.169.254 is the documented GCE metadata address, and the one a
//     Pod's own code connects to.
//   - 169.254.169.252 is the node-local gke-metadata-server's own listener,
//     which is what an iptables dataplane sees after NAT PREROUTING has
//     rewritten the request above. Blocking only the first address is the
//     mistake in kubernetes/kubernetes#68078.
//   - fd20:ce::254 is the IPv6 metadata address, documented alongside the IPv4
//     one. Dual-stack clusters reach it without touching either IPv4 address.
var metadataServerAddresses = []string{
	"169.254.169.254",
	"169.254.169.252",
	"fd20:ce::254",
}

// agentEgressPolicyEnabled reports whether the agent Pod gets a rendered
// egress policy.
func agentEgressPolicyEnabled(agent *agentv1alpha1.PlatformAgent) bool {
	return agent.Spec.Security != nil && agent.Spec.Security.EgressPolicy == egressPolicyAllowlist
}

// agentEgressPolicyName is the NetworkPolicy the controller renders for the
// agent Pod.
func agentEgressPolicyName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + agentEgressPolicyNameSuffix
}

// namespacedPodPeer selects Pods by label within one namespace. NetworkPolicy
// peers are namespace-local unless a namespaceSelector says otherwise, and the
// namespaceSelector has to match on the kubernetes.io/metadata.name label the
// API server maintains on every Namespace.
func namespacedPodPeer(namespace string, podLabels map[string]string) networkingv1.NetworkPolicyPeer {
	return networkingv1.NetworkPolicyPeer{
		NamespaceSelector: &metav1.LabelSelector{
			MatchLabels: map[string]string{"kubernetes.io/metadata.name": namespace},
		},
		PodSelector: &metav1.LabelSelector{MatchLabels: podLabels},
	}
}

func tcpPort(port int32) networkingv1.NetworkPolicyPort {
	return networkingv1.NetworkPolicyPort{Protocol: ptr.To(corev1.ProtocolTCP), Port: ptrIntOrString(port)}
}

func udpPort(port int32) networkingv1.NetworkPolicyPort {
	return networkingv1.NetworkPolicyPort{Protocol: ptr.To(corev1.ProtocolUDP), Port: ptrIntOrString(port)}
}

func ptrIntOrString(port int32) *intstr.IntOrString {
	value := intstr.FromInt32(port)
	return &value
}

// buildAgentEgressNetworkPolicy renders the agent Pod's default-deny egress
// policy.
//
// Every rule below is derived from a destination in this repository's own
// source, not from a guess about what an agent might want. Under-allowing
// breaks the product and over-allowing makes the control meaningless, so the
// comment on each rule names where the destination comes from.
//
// The second return value carries the extraRules that were dropped for
// re-permitting the metadata server, so the caller can report them.
func buildAgentEgressNetworkPolicy(agent *agentv1alpha1.PlatformAgent) (*networkingv1.NetworkPolicy, []string) {
	labels := commonLabels(agent)
	labels["kubeagents.x-k8s.io/component"] = "agent-egress"

	var rules []networkingv1.NetworkPolicyEgressRule

	// DNS. Everything else in this list is reached by name, so without this
	// rule the allowlist is equivalent to a total egress block. kube-dns is the
	// CoreDNS Service in kube-system; node-local-dns is the NodeLocal DNSCache
	// DaemonSet, which also answers on a link-local address of its own. Mirrors
	// charts/kube-agents/templates/litellm.yaml, the repository's only other
	// worked NetworkPolicy.
	rules = append(rules, networkingv1.NetworkPolicyEgressRule{
		Ports: []networkingv1.NetworkPolicyPort{udpPort(53), tcpPort(53)},
		To: []networkingv1.NetworkPolicyPeer{
			namespacedPodPeer("kube-system", map[string]string{"k8s-app": "kube-dns"}),
			namespacedPodPeer("kube-system", map[string]string{"k8s-app": "node-local-dns"}),
			{IPBlock: &networkingv1.IPBlock{CIDR: nodeLocalDNSCacheIP}},
		},
	})

	// The credential broker. This is the agent's route to every credentialed
	// command it is allowed to run, so denying it denies the product:
	// CREDENTIAL_PROXY_URL, GOOGLE_CHAT_RELAY_URL and SLACK_RELAY_URL all
	// address it (credentialProxyBaseURL). The Service publishes
	// credentialProxyPort onto a target port of the same number, and the
	// selector here is the broker Deployment's own "app" label — Pod selectors,
	// not the Service, because that is what NetworkPolicy matches after the
	// ClusterIP has been translated away.
	rules = append(rules, networkingv1.NetworkPolicyEgressRule{
		Ports: []networkingv1.NetworkPolicyPort{tcpPort(credentialProxyPort)},
		To: []networkingv1.NetworkPolicyPeer{
			namespacedPodPeer(agent.Namespace, map[string]string{"app": credentialBrokerName(agent)}),
		},
	})

	// The model gateway. buildAgentConfig pins the agent's model base_url to
	// http://litellm.<namespace>.svc.cluster.local/v1 unconditionally, so the
	// agent cannot think for a living without this rule. Both ports are named
	// deliberately: the Service is 80 -> targetPort 4000
	// (charts/kube-agents/templates/litellm.yaml), and whether a dataplane
	// evaluates policy before or after that translation is not something the
	// operator can know. The peer is a Pod selector that matches only LiteLLM,
	// which listens on 4000 alone, so naming 80 as well permits nothing extra.
	rules = append(rules, networkingv1.NetworkPolicyEgressRule{
		Ports: []networkingv1.NetworkPolicyPort{tcpPort(80), tcpPort(4000)},
		To: []networkingv1.NetworkPolicyPeer{
			namespacedPodPeer(agent.Namespace, map[string]string{"app": "litellm"}),
		},
	})

	// Traces and metrics. OTEL_EXPORTER_OTLP_ENDPOINT is set on every agent
	// container by otelTelemetryEnvVars and addresses the GKE Managed
	// OpenTelemetry collector. Blocking it loses telemetry rather than
	// function, but a silently trace-less agent is its own kind of security
	// problem. 4317 accompanies 4318 because the endpoint's protocol is
	// configurable through spec.deployment.env.
	rules = append(rules, networkingv1.NetworkPolicyEgressRule{
		Ports: []networkingv1.NetworkPolicyPort{tcpPort(4317), tcpPort(4318)},
		To: []networkingv1.NetworkPolicyPeer{{
			NamespaceSelector: &metav1.LabelSelector{
				MatchLabels: map[string]string{"kubernetes.io/metadata.name": "gke-managed-otel"},
			},
		}},
	})

	// The Kubernetes API server, if and only if the operator was told where it
	// is. The event-watcher sidecar reaches it with in-cluster config, and
	// leader election needs it when replicas > 1. There is no NetworkPolicy
	// peer for "the API server": on GKE the control plane is not a Pod and not
	// in the cluster, and the in-cluster Service address is translated to it
	// before policy is evaluated. So it has to be a CIDR the cluster's owner
	// supplies. Left empty, this rule is absent and the event-watcher loses its
	// connection — that is a deliberate under-allow, since inventing a range
	// here would mean permitting a guess.
	if cidrs := controlPlaneCIDRs(agent); len(cidrs) > 0 {
		peers := make([]networkingv1.NetworkPolicyPeer, 0, len(cidrs))
		for _, cidr := range cidrs {
			peers = append(peers, networkingv1.NetworkPolicyPeer{IPBlock: &networkingv1.IPBlock{CIDR: cidr}})
		}
		rules = append(rules, networkingv1.NetworkPolicyEgressRule{
			Ports: []networkingv1.NetworkPolicyPort{tcpPort(443)},
			To:    peers,
		})
	}

	extra, dropped := admissibleExtraEgressRules(agent)
	rules = append(rules, extra...)

	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agentEgressPolicyName(agent),
			Namespace: agent.Namespace,
			Labels:    labels,
		},
		Spec: networkingv1.NetworkPolicySpec{
			// The agent Pod, and only the agent Pod. The broker Pod carries
			// app: <name>-credential-proxy and is deliberately left
			// unrestricted — it is the container that is supposed to reach the
			// metadata server.
			PodSelector: metav1.LabelSelector{MatchLabels: map[string]string{"app": agent.Name + "-gateway"}},
			// Egress only. Adding Ingress here would default-deny inbound as a
			// side effect and cut off the agent's own API Service and the
			// session-KV listener on 8699, neither of which this change has
			// enumerated.
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeEgress},
			Egress:      rules,
		},
	}, dropped
}

func controlPlaneCIDRs(agent *agentv1alpha1.PlatformAgent) []string {
	if agent.Spec.Security == nil || agent.Spec.Security.EgressAllowlist == nil {
		return nil
	}
	return agent.Spec.Security.EgressAllowlist.ControlPlaneCIDRs
}

// admissibleExtraEgressRules returns the operator-supplied rules that may be
// rendered, and a description of each one that may not.
//
// A rule is refused if any of its peers could carry a packet to the metadata
// server. An escape hatch that can reopen the escape is not an escape hatch,
// and the failure has to be a dropped rule rather than a narrowed one: the
// obvious narrowing is to append the metadata addresses to the ipBlock's
// except list, which is the very construct kubernetes/kubernetes#68078 says
// does not hold.
func admissibleExtraEgressRules(agent *agentv1alpha1.PlatformAgent) ([]networkingv1.NetworkPolicyEgressRule, []string) {
	if agent.Spec.Security == nil || agent.Spec.Security.EgressAllowlist == nil {
		return nil, nil
	}

	var kept []networkingv1.NetworkPolicyEgressRule
	var dropped []string
	for index, rule := range agent.Spec.Security.EgressAllowlist.ExtraRules {
		if reason := egressRuleReachesMetadata(rule); reason != "" {
			dropped = append(dropped, fmt.Sprintf("extraRules[%d]: %s", index, reason))
			continue
		}
		kept = append(kept, *rule.DeepCopy())
	}
	return kept, dropped
}

// egressRuleReachesMetadata returns why a rule could reach the metadata server,
// or "" if it cannot.
func egressRuleReachesMetadata(rule networkingv1.NetworkPolicyEgressRule) string {
	// An egress rule with no peers permits every destination, which is the
	// broadest way to reopen this and the easiest to write by accident.
	if len(rule.To) == 0 {
		return "an egress rule with no \"to\" peers permits every destination, including the metadata server"
	}
	for _, peer := range rule.To {
		// Selector peers resolve to Pods and Namespaces. The metadata server is
		// neither, so they cannot reach it however broad they are.
		if peer.IPBlock == nil {
			continue
		}
		prefix, err := netip.ParsePrefix(peer.IPBlock.CIDR)
		if err != nil {
			// Unparseable is refused rather than passed through: the API server
			// would reject it later anyway, and guessing at intent here is how
			// a fail-open creeps in.
			return fmt.Sprintf("ipBlock cidr %q is not a valid CIDR", peer.IPBlock.CIDR)
		}
		for _, address := range metadataServerAddresses {
			addr, addrErr := netip.ParseAddr(address)
			if addrErr != nil {
				continue
			}
			if prefix.Contains(addr) {
				// Note that an "except" clause naming the address does not
				// rescue the rule, and that is not an oversight — see the
				// package comment. Carve the range up instead.
				return fmt.Sprintf("ipBlock cidr %q contains the metadata server address %s", peer.IPBlock.CIDR, address)
			}
		}
	}
	return ""
}
