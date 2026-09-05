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
// So there is one policy, it is default-deny, and the metadata server's
// credential API is denied because it does not appear on the list.
//
// "Credential API" and not "the metadata address": the DNS rule names
// 169.254.169.254 on port 53, because that is the resolver on a Cloud DNS for
// GKE cluster and withholding it is a total outage rather than a control. The
// ports the token is minted on — 80 pre-NAT, 988 post-NAT — appear nowhere.
// buildAgentEgressNetworkPolicy's dnsPeers comment argues that split in full.
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
//   - It does nothing whatsoever on a cluster whose CNI does not enforce
//     NetworkPolicy. The operator cannot detect that: an unenforced policy is
//     accepted by the API server, stored, and returned by kubectl get exactly
//     like an enforced one. There is no field, condition or event to read.
//   - It can be undone from outside, and today it is. Policies selecting one
//     Pod are additive, and two others select this one and permit the metadata
//     path: <name>-gateway-netpol, which this same operator renders on every
//     reconcile (buildNetworkPolicy — 169.254.169.254/32 on TCP 80, the
//     discovered metadata-daemon port (988 by default) to both link-local
//     metadata addresses, and TCP 443 to 0.0.0.0/0 minus
//     the private ranges), and platform-agent-core-egress from
//     deploy/kustomize/platform, on installs that apply it. So the policy
//     below denies the metadata server, and the Pod it selects can still
//     reach it. Narrowing the gateway policy is not done here: the metadata
//     path is still what Workload Identity uses, and the broker Pod is the one
//     that needs it. Scoping the allowance to that Pod alone is the follow-up.

import (
	"fmt"
	"net/netip"
	"strings"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	metav1validation "k8s.io/apimachinery/pkg/apis/meta/v1/validation"
	"k8s.io/apimachinery/pkg/util/intstr"
	utilvalidation "k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// egressPolicyAllowlist is the one spec.security.egressPolicy value that
	// renders anything.
	egressPolicyAllowlist = "Allowlist"

	// agentEgressPolicyNameSuffix keeps the name the previous two-pod layout
	// used, and that deleteLegacyCredentialIsolationResources still names.
	// Rendering under a new name would leave any hand-applied copy
	// of the old one in place beside this one, and since policies are additive
	// the more permissive of the two would win.
	agentEgressPolicyNameSuffix = "-sandbox-metadata-deny"

	// nodeLocalDNSCacheIP is where NodeLocal DNSCache listens when it is
	// deployed. Link-local, but not the metadata server: allowing it grants
	// name resolution and nothing else.
	//
	// KNOWN WEAKNESS, and it is the same construct this file argues against
	// elsewhere. NodeLocal DNSCache runs with hostNetwork, so on Cilium and GKE
	// Dataplane V2 its traffic carries a host or remote-node identity rather
	// than a Pod one — and neither the podSelector beside this CIDR nor the
	// CIDR itself is guaranteed to match that, because CIDR peers do not select
	// node identities unless policy-cidr-match-mode includes "nodes", which is
	// off by default. Both peers do work on an iptables dataplane, which is why
	// they are here.
	//
	// The failure mode if neither matches is DNS being blocked outright, which
	// is fail-closed but is a total agent outage rather than a subtle
	// degradation. So it is a pre-enable check rather than something the
	// operator can fix in a manifest: on a cluster running NodeLocal DNSCache,
	// confirm the agent can still resolve before trusting this. Documented
	// alongside the CNI-enforcement caveat in the credential-isolation
	// reference page.
	nodeLocalDNSCacheIP = "169.254.20.10/32"

	// metadataResolverCIDR is the metadata address in the one role this policy
	// grants it: the DNS resolver a Cloud DNS for GKE cluster puts in every
	// Pod's resolv.conf. It is the same host as metadataLinkLocalIP, spelled as
	// a CIDR because that is what an ipBlock takes, and kept separate from that
	// constant so a reader of the DNS rule sees which role is meant. The
	// credential ports on this host are permitted nowhere in this file; the
	// dnsPeers comment in buildAgentEgressNetworkPolicy says why 53 is safe.
	metadataResolverCIDR = metadataLinkLocalIP + "/32"
)

// metadataServerAddresses are every address a request for cloud credentials
// can arrive at, and none of them may be permitted on a credential port by a
// rendered egress rule.
//
// "On a credential port" and not "at all": the DNS rule in
// buildAgentEgressNetworkPolicy names 169.254.169.254 on port 53, because that
// is the resolver under Cloud DNS for GKE. Its comment argues why 53 reaches no
// token, and permitsBeyondDNS in the tests is what holds the invariant at that
// scope. The addresses below still may not appear anywhere else, and
// egressRuleReachesMetadata refuses them in extraRules on every port.
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

// isMetadataAddress reports whether ip parses to one of the metadata server
// addresses, in canonical or IPv4-mapped form.
func isMetadataAddress(ip string) bool {
	addr, err := netip.ParseAddr(ip)
	if err != nil {
		return false
	}
	addr = addr.Unmap()
	for _, m := range metadataServerAddresses {
		if metadataAddr, mErr := netip.ParseAddr(m); mErr == nil && addr == metadataAddr {
			return true
		}
	}
	return false
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
//
// dnsClusterIPs is the resolved cluster DNS VIP list from
// resolveNetpolProfile — the same four-rung ladder (annotation, spec,
// operator flag, discovery) whose result buildNetworkPolicy renders, because
// on a dataplane that matches the Service VIP rather than the backing Pods
// the selector peers below never fire and the VIP peer is what keeps DNS
// alive. Empty falls back to the documented default, exactly as the gateway
// policy does.
func buildAgentEgressNetworkPolicy(agent *agentv1alpha1.PlatformAgent, dnsClusterIPs []string) (*networkingv1.NetworkPolicy, []string) {
	labels := commonLabels(agent)
	labels["kubeagents.x-k8s.io/component"] = "agent-egress"

	var rules []networkingv1.NetworkPolicyEgressRule

	// DNS. Everything else in this list is reached by name, so without this
	// rule the allowlist is equivalent to a total egress block. kube-dns is the
	// CoreDNS Service in kube-system; node-local-dns is the NodeLocal DNSCache
	// DaemonSet, which also answers on a link-local address of its own. The
	// same two peers appear in charts/kube-agents/templates/litellm.yaml and in
	// deploy/kustomize/platform/networkpolicy-core-egress.yaml, and the
	// resolved ClusterIP peers join them for the VIP-matching dataplanes.
	//
	// The metadata address is on this rule, and on port 53 only. Under Cloud DNS
	// for GKE the node answers DNS at 169.254.169.254:53 and a Pod's resolv.conf
	// names it, so barring it there is not a narrower credential path — it is no
	// name resolution, and this allowlist reaches every one of its destinations
	// by name. Port 53 is not a way in: a TCP connection to :53 reaches the
	// resolver, and the credential API this policy exists to withhold is on :80
	// pre-NAT and :988 post-NAT. No rule below permits this address on either —
	// the only rule naming 80 is LiteLLM's, whose peer is a Pod selector that no
	// link-local address matches, and extraRules refuses a metadata address
	// outright on every port. Nor is it a new exfiltration channel wherever
	// CoreDNS forwards externally, as the stock configuration does: the kube-dns
	// peers above already resolve arbitrary external names. The exception is a
	// cluster that has removed that forwarding, where this peer does hand the
	// sandbox a recursive resolver CoreDNS was withholding.
	//
	// So the invariant this policy holds is that no rule permits a metadata
	// address on a *credential* port, and it is machine-checked at that scope by
	// TestTheRenderedPolicyDeniesEveryMetadataAddress.
	//
	// A resolved IP that is a metadata address is still dropped below rather
	// than rendered. That filter is no longer load-bearing for 169.254.169.254,
	// which is granted here unconditionally, but 169.254.169.252 and fd20:ce::254
	// are credential listeners that answer no DNS, and the annotation rung of the
	// ladder is operator input that can name them.
	dnsPeers := []networkingv1.NetworkPolicyPeer{
		namespacedPodPeer("kube-system", map[string]string{"k8s-app": "kube-dns"}),
		namespacedPodPeer("kube-system", map[string]string{"k8s-app": "node-local-dns"}),
		{IPBlock: &networkingv1.IPBlock{CIDR: nodeLocalDNSCacheIP}},
		{IPBlock: &networkingv1.IPBlock{CIDR: metadataResolverCIDR}},
	}
	safeDNSIPs := make([]string, 0, len(dnsClusterIPs))
	for _, ip := range dnsClusterIPs {
		if isMetadataAddress(ip) {
			continue
		}
		safeDNSIPs = append(safeDNSIPs, ip)
	}
	dnsIPPeers := formatCIDRPeers(safeDNSIPs, false)
	if len(dnsIPPeers) == 0 {
		dnsIPPeers = formatCIDRPeers([]string{defaultDNSClusterIP}, false)
	}
	// formatCIDRPeers dedupes within one call, not against the fixed peers above.
	// The metadata address cannot collide, having been filtered out already, but
	// nodeLocalDNSCacheIP can: a cluster running NodeLocal DNSCache puts
	// 169.254.20.10 in kubelet's --cluster-dns, so an operator naming the value
	// their nodes use lands on the peer this rule already carries.
	rules = append(rules, networkingv1.NetworkPolicyEgressRule{
		Ports: []networkingv1.NetworkPolicyPort{udpPort(53), tcpPort(53)},
		To:    append(dnsPeers, peersNotAlreadyPresent(dnsPeers, dnsIPPeers)...),
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

	// The shell sandbox's sshd. Every command the model runs executes there, so
	// this rule is the difference between an agent that can act and one whose
	// every tool call times out. It duplicates rule 11 of buildNetworkPolicy
	// because the two policies are rendered independently and either can be the
	// only one present: spec.networkPolicy.enabled: false with
	// spec.security.egressPolicy: Allowlist deletes the gateway policy and leaves
	// this one standing alone. That combination is also the one that fails
	// silently — the CR reads Ready, the sandbox Pod is Running, and the ssh dial
	// just never completes.
	rules = append(rules, networkingv1.NetworkPolicyEgressRule{
		Ports: []networkingv1.NetworkPolicyPort{tcpPort(shellSandboxPort)},
		To: []networkingv1.NetworkPolicyPeer{
			namespacedPodPeer(agent.Namespace, shellSandboxSelector(agent)),
		},
	})

	// The model gateway. buildAgentConfig pins the agent's model base_url to
	// http://litellm.<namespace>.svc.cluster.local/v1 unconditionally, so the
	// agent cannot think for a living without this rule. The port set matches
	// buildNetworkPolicy's LiteLLM rule exactly, so the two policies cannot
	// disagree about the model gateway: 8080 is what this repository's chart
	// and kustomize deployments actually listen on (Service 80 -> targetPort
	// 8080), and it is the port a Pod-selector peer sees after the ClusterIP
	// translation, which is the only point such a peer matches; 4000 is
	// LiteLLM's upstream default, kept because the gateway rule keeps it for
	// deployments this repository's manifests did not render; 80 covers a
	// dataplane evaluating before the translation. Extra ports on a peer that matches only LiteLLM permit
	// nothing beyond LiteLLM.
	rules = append(rules, networkingv1.NetworkPolicyEgressRule{
		Ports: []networkingv1.NetworkPolicyPort{tcpPort(80), tcpPort(4000), tcpPort(8080)},
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

	// The Hindsight memory API. buildPodEnv sets HINDSIGHT_API_URL to
	// http://hindsight-api.<ns>.svc.cluster.local:8888 on every agent
	// container, so on an install that chose a Hindsight-backed provider,
	// every memory_retain and memory_recall goes here. Unconditional for the
	// same reason buildNetworkPolicy's rule 10 is: the selector matches
	// nothing on an install without Hindsight, and the install this rule saves
	// is the one where the policy enforces. The component label keeps it off
	// the Postgres Pod, which carries the same name label.
	//
	// Deliberately absent, and why: github-token-minter, because gh and git run
	// in the broker Pod and the minter call is the broker's egress, not the
	// agent's; gemma-server and standalone-replay, because buildAgentConfig pins
	// the agent's model base_url to LiteLLM unconditionally, so those backends
	// are LiteLLM's egress. The gateway policy names all three anyway; narrowing
	// it is the work that turns this field into a control.
	rules = append(rules, networkingv1.NetworkPolicyEgressRule{
		Ports: []networkingv1.NetworkPolicyPort{tcpPort(8888)},
		To: []networkingv1.NetworkPolicyPeer{{
			PodSelector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"app.kubernetes.io/name":      "hindsight",
					"app.kubernetes.io/component": "api",
				},
			},
		}},
	})

	// The Kubernetes API server, if and only if the operator was told where it
	// is. There is no NetworkPolicy peer for "the API server": on GKE the
	// control plane is not a Pod and not in the cluster, and the in-cluster
	// Service address is translated to it before policy is evaluated. So it
	// has to be a CIDR the cluster's owner supplies. Left empty, this rule is
	// absent and nothing in the agent Pod can reach the API — which matters
	// above one replica, where the container runs leader_elect.py and holds a
	// Lease, and to any sidecar or plugin the operator added. That is a
	// deliberate under-allow: inventing a range here would mean permitting a
	// guess.
	//
	// The supplied ranges go through the same refusal check extraRules gets.
	// The field is named for the control plane, but nothing stops it being
	// handed 0.0.0.0/0 — and "allow TCP/443 to anywhere" through a field named
	// controlPlaneCIDRs would defeat the exfiltration half of this control one
	// function away from the guard built to prevent exactly that.
	var dropped []string
	if cidrs := controlPlaneCIDRs(agent); len(cidrs) > 0 {
		peers := make([]networkingv1.NetworkPolicyPeer, 0, len(cidrs))
		for index, cidr := range cidrs {
			if reason := controlPlaneCIDRRefusal(cidr); reason != "" {
				dropped = append(dropped, fmt.Sprintf("controlPlaneCIDRs[%d]: %s", index, reason))
				continue
			}
			peers = append(peers, networkingv1.NetworkPolicyPeer{IPBlock: &networkingv1.IPBlock{CIDR: normalizeControlPlaneCIDR(cidr)}})
		}
		if len(peers) > 0 {
			rules = append(rules, networkingv1.NetworkPolicyEgressRule{
				Ports: []networkingv1.NetworkPolicyPort{tcpPort(443)},
				To:    peers,
			})
		}
	}

	extra, extraDropped := admissibleExtraEgressRules(agent)
	rules = append(rules, extra...)
	dropped = append(dropped, extraDropped...)

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

const (
	// narrowestControlPlanePrefixIPv4 and ...IPv6 bound how much of the
	// internet a "control plane" may be.
	//
	// A GKE control plane is a /28 that you chose at cluster creation, or a
	// single public address. /16 is already four thousand times more generous
	// than the first and is not a number anyone will hit by accident; anything
	// broader is not a control plane, it is an internet rule wearing the
	// field's name. The IPv6 bound is the same argument at a scale where /32
	// is still an enormous allocation.
	narrowestControlPlanePrefixIPv4 = 16
	narrowestControlPlanePrefixIPv6 = 32
)

// ipv4MappedSpace is ::ffff:0.0.0.0/96, the IPv4-mapped IPv6 range.
//
// Every CIDR check in this file has to reject anything touching it, because the
// library that validates the value and the library that enforces it disagree
// about what it means. netip — used here — treats an address family as a hard
// boundary: netip.Prefix.Contains returns false across families, so
// "0.0.0.0/0".Contains(a mapped address) is false and, worse,
// "::ffff:169.254.169.254/128".Contains(169.254.169.254) is also false. Go's
// net.ParseCIDR — which k8s.io/utils/net.ParseCIDRSloppy, the API server's own
// ipBlock validation and Calico all sit on — instead normalises the mapped form
// away: "::ffff:0.0.0.0/96" becomes 0.0.0.0/0, and
// "::ffff:169.254.169.254/128" becomes 169.254.169.254/32.
//
// So a mapped CIDR reads as an inert IPv6 range to a netip-based check and as
// the whole internet, or as the metadata server itself, to everything that acts
// on it. That is a parser differential, and parser differentials are how guards
// like this one get walked past.
var ipv4MappedSpace = netip.MustParsePrefix("::ffff:0.0.0.0/96")

// ipv4MappedRefusal refuses any prefix that touches the IPv4-mapped range.
//
// Refusing outright rather than unmapping-then-checking is deliberate. Unmapping
// means reimplementing another library's normalisation and hoping the two agree
// at every edge — a /64 written in mapped form, for instance, has no obvious
// IPv4 equivalent. Refusing costs the operator one edit to dotted-quad form and
// removes the whole class.
//
// The check is an overlap rather than an Is4In6 test on the prefix address,
// because a short IPv6 prefix reaches the mapped range without being written in
// mapped form: "::/1" covers ::ffff:169.254.169.254 and is not caught by the
// metadata loop, since the IPv6 metadata address fd20:ce::254 sits in the other
// half of the space.
func ipv4MappedRefusal(prefix netip.Prefix, cidr string) string {
	if !prefix.Overlaps(ipv4MappedSpace) {
		return ""
	}
	return fmt.Sprintf("%q covers IPv4-mapped IPv6 addresses (%s). Those normalise to plain IPv4 "+
		"in the libraries that enforce the policy but not in the one that validates it, so a range "+
		"like ::ffff:0.0.0.0/96 would read as inert here and mean 0.0.0.0/0 in the cluster. "+
		"Write IPv4 ranges in dotted-quad form", cidr, ipv4MappedSpace)
}

// normalizeControlPlaneCIDR widens a bare address to a single-host prefix and
// leaves everything else alone.
//
// The gcloud command the field description gives —
// `--format='value(privateClusterConfig.masterIpv4CidrBlock,endpoint)'` —
// emits a bare address for a cluster with a public endpoint, so pasting its
// output is the documented workflow, and refusing the paste over a missing
// "/32" would park the CR Degraded for a value the field's own docs produced.
// formatCIDRPeers does the same widening for the gateway policy's
// operator-resolved IPs.
func normalizeControlPlaneCIDR(cidr string) string {
	addr, err := netip.ParseAddr(cidr)
	if err != nil {
		return cidr
	}
	return netip.PrefixFrom(addr, addr.BitLen()).String()
}

// controlPlaneCIDRRefusal returns why a control-plane range may not be
// rendered, or "" if it may.
//
// Three checks. The IPv4-mapped one first, because the other two cannot be
// trusted on a prefix that means something different to the enforcer than it
// does here. Then the metadata guard extraRules gets, because 0.0.0.0/0
// contains the metadata addresses and this field must not be the way round it.
// Then a width bound, because the metadata guard alone would still admit, say,
// 1.0.0.0/8 on port 443 — which does not reopen the metadata escape, since the
// metadata server does not serve 443, but does hand the sandbox a large slice
// of the internet over HTTPS. This control is sold as an exfiltration control
// as well as a metadata one, and a field named for the control plane is the
// last place a reviewer would look for a hole in it.
func controlPlaneCIDRRefusal(cidr string) string {
	prefix, err := netip.ParsePrefix(normalizeControlPlaneCIDR(cidr))
	if err != nil {
		return fmt.Sprintf("%q is not a valid CIDR", cidr)
	}
	if reason := ipv4MappedRefusal(prefix, cidr); reason != "" {
		return reason
	}
	for _, address := range metadataServerAddresses {
		addr, addrErr := netip.ParseAddr(address)
		if addrErr == nil && prefix.Contains(addr) {
			return fmt.Sprintf("%q contains the metadata server address %s", cidr, address)
		}
	}
	narrowest := narrowestControlPlanePrefixIPv4
	if prefix.Addr().Is6() {
		narrowest = narrowestControlPlanePrefixIPv6
	}
	if prefix.Bits() < narrowest {
		return fmt.Sprintf("%q is broader than /%d; a GKE control plane is a /28 or a single address, "+
			"so a range this wide is an internet rule in a field named for the control plane. "+
			"Use egressAllowlist.extraRules if that is really what you mean", cidr, narrowest)
	}
	return ""
}

// egressAllowlistRefusals returns every operator-supplied destination that may
// not be rendered, in the order they appear in the spec.
//
// Shared by the builder, which drops them, and by validateEgressPolicy, which
// refuses the whole reconcile over them. Two layers on purpose: the validator
// is the one an operator sees, and the builder's drop is what keeps the
// rendered object safe if a future change reorders the reconcile or calls the
// builder from somewhere new.
func egressAllowlistRefusals(agent *agentv1alpha1.PlatformAgent) []string {
	if agent.Spec.Security == nil || agent.Spec.Security.EgressAllowlist == nil {
		return nil
	}
	var refusals []string
	for index, cidr := range agent.Spec.Security.EgressAllowlist.ControlPlaneCIDRs {
		if reason := controlPlaneCIDRRefusal(cidr); reason != "" {
			refusals = append(refusals, fmt.Sprintf("controlPlaneCIDRs[%d]: %s", index, reason))
		}
	}
	for index, rule := range agent.Spec.Security.EgressAllowlist.ExtraRules {
		if reason := egressRuleReachesMetadata(rule); reason != "" {
			refusals = append(refusals, fmt.Sprintf("extraRules[%d]: %s", index, reason))
			continue
		}
		if reason := egressRuleAPIServerRejection(rule); reason != "" {
			refusals = append(refusals, fmt.Sprintf("extraRules[%d]: %s", index, reason))
		}
	}
	return refusals
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
		if reason := egressRuleAPIServerRejection(rule); reason != "" {
			dropped = append(dropped, fmt.Sprintf("extraRules[%d]: %s", index, reason))
			continue
		}
		kept = append(kept, *rule.DeepCopy())
	}
	return kept, dropped
}

// egressRuleAPIServerRejection returns why the API server would reject the
// whole NetworkPolicy over this rule, or "" if it would store it.
//
// ExtraRules is the raw upstream type, so the CRD schema stores shapes
// networking.k8s.io/v1 refuses at apply time. Left unscreened, the rejection
// arrives as an applyManaged error at reconcile step 11b, which returns from
// Reconcile above the Service, the PodDisruptionBudget, the gateway guardrail
// and the status update — the CR keeps its last phase while nothing behind it
// is being maintained, and the reason sits in a log line rather than on the
// resource. That is the same wedge toEgressRules defends
// spec.networkPolicy.additionalEgress against by canonicalising its own
// narrowed type; this field keeps the raw type, so the two shapes the API
// server refuses are turned into an EgressAllowlistRefused here, where the
// operator parks Degraded with the rule named.
func egressRuleAPIServerRejection(rule networkingv1.NetworkPolicyEgressRule) string {
	for _, port := range rule.Ports {
		if port.Protocol != nil {
			switch *port.Protocol {
			case corev1.ProtocolTCP, corev1.ProtocolUDP, corev1.ProtocolSCTP:
			default:
				return fmt.Sprintf("port protocol %q is rejected by the API server; it must be TCP, UDP or "+
					"SCTP, case-sensitively", string(*port.Protocol))
			}
		}
		if port.Port != nil {
			if port.Port.Type == intstr.Int {
				if msgs := utilvalidation.IsValidPortNum(int(port.Port.IntVal)); len(msgs) > 0 {
					return fmt.Sprintf("port %d is rejected by the API server: %s",
						port.Port.IntVal, strings.Join(msgs, "; "))
				}
			} else if msgs := utilvalidation.IsValidPortName(port.Port.StrVal); len(msgs) > 0 {
				return fmt.Sprintf("port name %q is rejected by the API server: %s",
					port.Port.StrVal, strings.Join(msgs, "; "))
			}
		}
		if port.EndPort != nil {
			if port.Port == nil {
				return "endPort without port is rejected by the API server"
			}
			if port.Port.Type != intstr.Int {
				return "endPort alongside a named port is rejected by the API server"
			}
			if msgs := utilvalidation.IsValidPortNum(int(*port.EndPort)); len(msgs) > 0 {
				return fmt.Sprintf("endPort %d is rejected by the API server: %s",
					*port.EndPort, strings.Join(msgs, "; "))
			}
			if *port.EndPort < port.Port.IntVal {
				return fmt.Sprintf("endPort %d below port %d is rejected by the API server",
					*port.EndPort, port.Port.IntVal)
			}
		}
	}
	for _, peer := range rule.To {
		if peer.IPBlock == nil && peer.PodSelector == nil && peer.NamespaceSelector == nil {
			return "a \"to\" peer with no ipBlock, podSelector or namespaceSelector is rejected by the " +
				"API server (\"must specify a peer\"), and the rejection would freeze this agent's whole " +
				"reconcile at the apply"
		}
		// The same validator the API server runs, because the CRD schema
		// constrains neither: matchExpressions[].operator is a bare string
		// and matchLabels keys are unvalidated map keys.
		for _, selector := range []*metav1.LabelSelector{peer.PodSelector, peer.NamespaceSelector} {
			if selector == nil {
				continue
			}
			if errs := metav1validation.ValidateLabelSelector(selector,
				metav1validation.LabelSelectorValidationOptions{}, field.NewPath("selector")); len(errs) > 0 {
				return fmt.Sprintf("a \"to\" peer's label selector is rejected by the API server: %s",
					errs.ToAggregate().Error())
			}
		}
		if peer.IPBlock == nil {
			continue
		}
		if peer.PodSelector != nil || peer.NamespaceSelector != nil {
			return "a \"to\" peer carrying both an ipBlock and a selector is rejected by the API server " +
				"(\"may not specify both ipBlock and another peer\")"
		}
		prefix, err := netip.ParsePrefix(peer.IPBlock.CIDR)
		if err != nil {
			// Already refused by egressRuleReachesMetadata; nothing to add.
			continue
		}
		for _, except := range peer.IPBlock.Except {
			exceptPrefix, exceptErr := netip.ParsePrefix(except)
			if exceptErr != nil {
				return fmt.Sprintf("ipBlock except %q is not a valid CIDR", except)
			}
			// Strict subset, as upstream ValidateIPBlock enforces it: an
			// except of equal mask length is rejected too, the same reading
			// toEgressRules already applies to additionalEgress.
			if exceptPrefix.Bits() <= prefix.Bits() || !prefix.Contains(exceptPrefix.Masked().Addr()) {
				return fmt.Sprintf("ipBlock except %q is not a strict subset of cidr %q; the API server "+
					"rejects the whole policy over it", except, peer.IPBlock.CIDR)
			}
		}
	}
	return ""
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
		// Before the containment loop, not after: the loop below cannot see
		// into an IPv4-mapped prefix at all, so ::ffff:169.254.169.254/128
		// would pass every check here and normalise to the metadata server in
		// the cluster. This is the escape hatch whose whole purpose is that it
		// cannot reopen the escape.
		if reason := ipv4MappedRefusal(prefix, peer.IPBlock.CIDR); reason != "" {
			return "ipBlock " + reason
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
