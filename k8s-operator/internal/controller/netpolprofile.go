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
	"net"
	"sort"
	"strings"

	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// AnnotationDNSClusterIP overrides the Cluster DNS Service ClusterIP used for NetworkPolicy DNS egress.
	AnnotationDNSClusterIP = "kubeagents.x-k8s.io/dns-cluster-ip"

	// AnnotationMetadataDaemonIP overrides the Workload Identity metadata daemon IP used for NetworkPolicy egress (rule 3).
	AnnotationMetadataDaemonIP = "kubeagents.x-k8s.io/metadata-daemon-ip"

	// defaultDNSClusterIP is the standard fallback DNS VIP when kube-dns cannot be discovered.
	defaultDNSClusterIP = "10.96.0.10"

	// Source constants reporting how the network policy values were chosen.
	netpolSourceSpec        = "Spec"
	netpolSourceAnnotation  = "Annotation"
	netpolSourceOperatorEnv = "OperatorEnv"
	netpolSourceDiscovered  = "Discovered"
	netpolSourceDefault     = "Default"
	netpolSourceSuppressed  = "Suppressed"
)

// netpolProfile holds resolved network policy cluster targets and provenance.
type netpolProfile struct {
	Generated            bool
	DNSClusterIPs        []string
	DNSSource            string
	MetadataDaemonIP     string // "" == suppress rule 3
	MetadataDaemonSource string
	AdditionalEgress     []networkingv1.NetworkPolicyEgressRule
}

// resolveNetpolProfile mirrors resolveOTLPEndpoint's ladder: per-agent
// annotation, typed CR spec, operator flag/env field, kube-dns discovery, documented default.
// The operator override sits above discovery deliberately, matching telemetry.go:
// an explicit operator value is authoritative even when kube-dns is discoverable.
func (r *PlatformAgentReconciler) resolveNetpolProfile(ctx context.Context, agent *agentv1alpha1.PlatformAgent) netpolProfile {
	log := logf.FromContext(ctx).WithName("netpol-profile")
	p := netpolProfile{
		Generated: true,
	}

	// If NetworkPolicy is explicitly disabled in spec, skip generation and discovery.
	if agent != nil && agent.Spec.NetworkPolicy != nil && agent.Spec.NetworkPolicy.Enabled != nil && !*agent.Spec.NetworkPolicy.Enabled {
		p.Generated = false
		return p
	}

	// --- DNS cluster IPs ---
	// 1. Annotation escape hatch (highest precedence)
	if ip := trimmedAnnotation(agent, AnnotationDNSClusterIP); ip != "" {
		if net.ParseIP(ip) != nil {
			p.DNSClusterIPs = []string{ip}
			p.DNSSource = netpolSourceAnnotation
		} else {
			log.Info("Ignoring invalid annotation IP", "annotation", AnnotationDNSClusterIP, "value", ip)
		}
	}

	// 2. Typed spec in CRD
	if len(p.DNSClusterIPs) == 0 && agent != nil && agent.Spec.NetworkPolicy != nil && len(agent.Spec.NetworkPolicy.DNSClusterIPs) > 0 {
		var validIPs []string
		for _, rawIP := range agent.Spec.NetworkPolicy.DNSClusterIPs {
			trimmed := strings.TrimSpace(rawIP)
			if net.ParseIP(trimmed) != nil {
				validIPs = append(validIPs, trimmed)
			} else {
				log.Info("Ignoring invalid IP in spec.networkPolicy.dnsClusterIPs", "value", rawIP)
			}
		}
		if len(validIPs) > 0 {
			p.DNSClusterIPs = validIPs
			p.DNSSource = netpolSourceSpec
		}
	}

	// 3. Operator flag / env override
	if len(p.DNSClusterIPs) == 0 && r.DNSClusterIPOverride != "" {
		if net.ParseIP(r.DNSClusterIPOverride) != nil {
			p.DNSClusterIPs = []string{r.DNSClusterIPOverride}
			p.DNSSource = netpolSourceOperatorEnv
		} else {
			log.Info("Ignoring invalid operator override IP", "flag", "kubernetes-dns-cluster-ip", "value", r.DNSClusterIPOverride)
		}
	}

	// 4. In-cluster discovery from kube-system/kube-dns Service
	if len(p.DNSClusterIPs) == 0 {
		var svc corev1.Service
		if err := r.Get(ctx, types.NamespacedName{Namespace: "kube-system", Name: "kube-dns"}, &svc); err == nil {
			var discovered []string
			if len(svc.Spec.ClusterIPs) > 0 {
				for _, ip := range svc.Spec.ClusterIPs {
					trimmed := strings.TrimSpace(ip)
					if trimmed != "" && trimmed != "None" && net.ParseIP(trimmed) != nil {
						discovered = append(discovered, trimmed)
					}
				}
			} else if ip := strings.TrimSpace(svc.Spec.ClusterIP); ip != "" && ip != "None" && net.ParseIP(ip) != nil {
				discovered = append(discovered, ip)
			}

			if len(discovered) > 0 {
				p.DNSClusterIPs = discovered
				p.DNSSource = netpolSourceDiscovered
			}
		} else if !apierrors.IsNotFound(err) {
			log.Info("Failed to discover kube-dns ClusterIP", "error", err)
			// Anti-flap: on transient error, preserve previously discovered status if present
			if agent != nil && agent.Status.NetworkPolicy.DNSClusterIPsSource == netpolSourceDiscovered && len(agent.Status.NetworkPolicy.DNSClusterIPs) > 0 {
				p.DNSClusterIPs = append([]string(nil), agent.Status.NetworkPolicy.DNSClusterIPs...)
				p.DNSSource = netpolSourceDiscovered
			}
		}
	}

	// 5. Documented fallback default
	if len(p.DNSClusterIPs) == 0 {
		p.DNSClusterIPs = []string{defaultDNSClusterIP}
		p.DNSSource = netpolSourceDefault
	}

	// Sort and deduplicate DNSClusterIPs for deterministic spec/status comparisons
	p.DNSClusterIPs = deduplicateAndSortIPs(p.DNSClusterIPs)

	// --- Metadata daemon IP ---
	// 1. Annotation escape hatch
	if ip := trimmedAnnotation(agent, AnnotationMetadataDaemonIP); ip != "" {
		if net.ParseIP(ip) != nil {
			p.MetadataDaemonIP = ip
			p.MetadataDaemonSource = netpolSourceAnnotation
		} else {
			log.Info("Ignoring invalid annotation IP", "annotation", AnnotationMetadataDaemonIP, "value", ip)
		}
	}

	// 2. Typed spec in CRD
	if p.MetadataDaemonSource == "" && agent != nil && agent.Spec.NetworkPolicy != nil && agent.Spec.NetworkPolicy.MetadataDaemon != nil {
		ep := strings.TrimSpace(agent.Spec.NetworkPolicy.MetadataDaemon.Endpoint)
		if ep == "" {
			// Explicit empty string suppresses rule 3 entirely
			p.MetadataDaemonIP = ""
			p.MetadataDaemonSource = netpolSourceSuppressed
		} else if net.ParseIP(ep) != nil {
			p.MetadataDaemonIP = ep
			p.MetadataDaemonSource = netpolSourceSpec
		} else {
			log.Info("Ignoring invalid IP in spec.networkPolicy.metadataDaemon.endpoint", "value", ep)
		}
	}

	// 3. Operator flag / env override
	if p.MetadataDaemonSource == "" && r.MetadataDaemonIPOverride != "" {
		if net.ParseIP(r.MetadataDaemonIPOverride) != nil {
			p.MetadataDaemonIP = r.MetadataDaemonIPOverride
			p.MetadataDaemonSource = netpolSourceOperatorEnv
		} else {
			log.Info("Ignoring invalid operator override IP", "flag", "kubernetes-metadata-daemon-ip", "value", r.MetadataDaemonIPOverride)
		}
	}

	// 4. Default fallback
	if p.MetadataDaemonSource == "" {
		p.MetadataDaemonIP = metadataDaemonIP // 169.254.169.252
		p.MetadataDaemonSource = netpolSourceDefault
	}

	// --- Additional egress rules ---
	if agent != nil && agent.Spec.NetworkPolicy != nil && len(agent.Spec.NetworkPolicy.AdditionalEgress) > 0 {
		p.AdditionalEgress = toEgressRules(log, agent.Spec.NetworkPolicy.AdditionalEgress)
	}

	return p
}

func deduplicateAndSortIPs(raw []string) []string {
	seen := make(map[string]bool, len(raw))
	var out []string
	for _, ip := range raw {
		trimmed := strings.TrimSpace(ip)
		if trimmed != "" && !seen[trimmed] {
			seen[trimmed] = true
			out = append(out, trimmed)
		}
	}
	sort.Strings(out)
	return out
}

// normalizeCIDRTarget canonicalises one CIDR or bare-IP string into the *net.IPNet a
// NetworkPolicy ipBlock is built from, and reports whether it is usable at all. Three
// callers share it -- toEgressRules, formatCIDRPeers, and the API-server annotation
// parser in reconcileNetworkPolicy -- so the address-family rule below is stated once
// instead of three times.
//
// enforceMinPrefix applies the /12 (IPv4) and /48 (IPv6) floors that stop a
// caller-supplied range from being widened into an unrestricted egress bypass. Pass
// false only where the input cannot come from outside the operator.
//
// The address family is taken from the ADDRESS, never from the parsed mask width. For
// an IPv4-mapped IPv6 block the two disagree, and the disagreement is exploitable:
// net.ParseCIDR("::ffff:0:0/96") returns a 16-byte address with a 128-bit mask, so
// Mask.Size() reports ones=96/bits=128 and the value clears the IPv6 floor -- but
// net.IPNet.String() re-reads the address, finds To4() != nil, truncates the mask to its
// low four bytes (all zero at /96) and prints "0.0.0.0/0". A spec.networkPolicy
// .additionalEgress peer of "::ffff:0:0/96" therefore passed every guard this package
// has and emitted allow-all IPv4 egress from a pod holding the agent's Workload Identity
// credentials. Collapsing to the 4-byte form here measures the floor against the same
// value String() will print.
func normalizeCIDRTarget(entry string, enforceMinPrefix bool) (*net.IPNet, bool) {
	entry = strings.TrimSpace(entry)
	if entry == "" {
		return nil, false
	}

	var ipNet *net.IPNet
	if strings.Contains(entry, "/") {
		_, parsed, err := net.ParseCIDR(entry)
		if err != nil {
			return nil, false
		}
		ipNet = parsed
	} else {
		ip := net.ParseIP(strings.Trim(entry, "[]"))
		if ip == nil {
			return nil, false
		}
		if v4 := ip.To4(); v4 != nil {
			ipNet = &net.IPNet{IP: v4, Mask: net.CIDRMask(32, 32)}
		} else {
			ipNet = &net.IPNet{IP: ip, Mask: net.CIDRMask(128, 128)}
		}
	}

	if v4 := ipNet.IP.To4(); v4 != nil && len(ipNet.Mask) == net.IPv6len {
		ipNet = &net.IPNet{IP: v4, Mask: ipNet.Mask[12:]}
	}

	ones, bits := ipNet.Mask.Size()
	if bits == 0 {
		// A non-contiguous mask. net.IPNet.String() cannot render one either, so
		// there is no string to put in an ipBlock.
		return nil, false
	}
	if enforceMinPrefix && ((bits == 32 && ones < minIPv4CIDRPrefix) || (bits == 128 && ones < minIPv6CIDRPrefix)) {
		return nil, false
	}
	return ipNet, true
}

// toEgressRules projects spec.networkPolicy.additionalEgress onto the NetworkPolicy API.
//
// Everything it drops is logged. The CRD admits a rule that this function then discards
// -- a peer past the prefix floor, an except outside its peer -- and without a log line
// the CR reads exactly as it did when the rule was working, so the first symptom is a
// connection timeout inside the agent with no thread leading back to the spec.
func toEgressRules(log logr.Logger, rules []agentv1alpha1.EgressRule) []networkingv1.NetworkPolicyEgressRule {
	if len(rules) == 0 {
		return nil
	}
	out := make([]networkingv1.NetworkPolicyEgressRule, 0, len(rules))
	for i, r := range rules {
		var peers []networkingv1.NetworkPolicyPeer
		for _, p := range r.To {
			ipNet, ok := normalizeCIDRTarget(p.CIDR, true)
			if !ok {
				log.Info("Dropping peer in spec.networkPolicy.additionalEgress: not a usable CIDR, or broader than the /12 (IPv4) or /48 (IPv6) floor",
					"rule", i, "cidr", p.CIDR)
				continue
			}
			peerOnes, peerBits := ipNet.Mask.Size()

			var validExcept []string
			for _, ex := range p.Except {
				// The API server rejects the whole NetworkPolicy when an except
				// is not a STRICT subset of its peer's CIDR -- ValidateIPBlock in
				// pkg/apis/networking/validation fails on
				// `cidrMaskLen >= exceptMaskLen` as well as on non-containment, so
				// an except equal to its peer is rejected too. That rejection
				// freezes every other egress rule at its previous revision,
				// including the DNS ClusterIP rediscovery, so the same condition is
				// applied here and one bad except costs its own peer and nothing
				// else.
				exNet, exOK := normalizeCIDRTarget(ex, false)
				if !exOK {
					log.Info("Dropping unparseable except block in spec.networkPolicy.additionalEgress",
						"rule", i, "cidr", ipNet.String(), "except", ex)
					continue
				}
				exOnes, exBits := exNet.Mask.Size()
				if exBits != peerBits || exOnes <= peerOnes || !ipNet.Contains(exNet.IP) {
					log.Info("Dropping except block outside its peer CIDR in spec.networkPolicy.additionalEgress",
						"rule", i, "cidr", ipNet.String(), "except", ex)
					continue
				}
				validExcept = append(validExcept, exNet.String())
			}

			peers = append(peers, networkingv1.NetworkPolicyPeer{
				IPBlock: &networkingv1.IPBlock{
					CIDR:   ipNet.String(),
					Except: validExcept,
				},
			})
		}

		var ports []networkingv1.NetworkPolicyPort
		for _, port := range r.Ports {
			if port.Port < 1 || port.Port > 65535 {
				log.Info("Dropping out-of-range port in spec.networkPolicy.additionalEgress", "rule", i, "port", port.Port)
				continue
			}
			protocol := corev1.ProtocolTCP
			switch strings.ToUpper(port.Protocol) {
			case "UDP":
				protocol = corev1.ProtocolUDP
			case "SCTP":
				protocol = corev1.ProtocolSCTP
			case "TCP":
				protocol = corev1.ProtocolTCP
			default:
				log.Info("Dropping unknown protocol in spec.networkPolicy.additionalEgress", "rule", i, "protocol", port.Protocol)
				continue
			}
			portVal := intstr.FromInt32(port.Port)
			ports = append(ports, networkingv1.NetworkPolicyPort{
				Protocol: &protocol,
				Port:     &portVal,
			})
		}

		// In NetworkPolicy semantics, an egress rule with Ports but To: nil allows egress
		// to ALL destinations (0.0.0.0/0). To prevent unintentional egress widening,
		// require at least one valid peer before emitting an additional egress rule.
		if len(peers) == 0 {
			log.Info("Dropping egress rule in spec.networkPolicy.additionalEgress: no peer survived validation, and a rule with ports but no peer would permit egress to every destination",
				"rule", i)
			continue
		}
		out = append(out, networkingv1.NetworkPolicyEgressRule{
			Ports: ports,
			To:    peers,
		})
	}
	return out
}

func trimmedAnnotation(agent *agentv1alpha1.PlatformAgent, key string) string {
	if agent == nil || agent.Annotations == nil {
		return ""
	}
	return strings.TrimSpace(agent.Annotations[key])
}
