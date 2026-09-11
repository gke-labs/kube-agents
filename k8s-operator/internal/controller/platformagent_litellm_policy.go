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
	"fmt"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	litellmNetworkPolicyName = "litellm-policy"
	litellmDeploymentName    = "litellm"
	litellmAppName           = "litellm"
	podLabelApp              = "app"

	litellmPort  int32 = 8080
	dnsPort            = 53
	httpsPort    int32 = 443
	httpPort     int32 = 80
	otlpGRPCPort int32 = 4317

	gmpNamespace         = "gke-gmp-system"
	kubeSystemNamespace  = "kube-system"
	kubeDNSAppLabel      = "kube-dns"
	nodeLocalDNSAppLabel = "node-local-dns"
	labelK8sApp          = "k8s-app"
	labelMetadataName    = "kubernetes.io/metadata.name"

	dnsNodeLocalLinkLocalCIDR = "169.254.20.10/32"
	metadataLinkLocalCIDR     = metadataLinkLocalIP + "/32"
	internetAnywhereCIDR      = "0.0.0.0/0"
	internetAnywhereIPv6CIDR  = "::/0"
	rfc1918Block10            = "10.0.0.0/8"
	rfc1918Block172           = "172.16.0.0/12"
	rfc1918Block192           = "192.168.0.0/16"
	rfc6598CGNAT              = "100.64.0.0/10"
	linkLocalIPv4Block        = "169.254.0.0/16"
	ipv6ULA                   = "fc00::/7"
	ipv6LinkLocal             = "fe80::/10"
	ipv6Multicast             = "ff00::/8"

	netpolAPIVersion = "networking.k8s.io/v1"
	netpolKind       = "NetworkPolicy"

	// AnnotationEnableLiteLLMNetworkPolicy toggles operator management of litellm-policy.
	// When set to "false", the operator stops generating litellm-policy and deletes any managed copy.
	AnnotationEnableLiteLLMNetworkPolicy = "kubeagents.x-k8s.io/enable-litellm-network-policy"
	// AnnotationOTLPCollectorNamespace specifies the namespace of the OpenTelemetry collector
	// when it cannot be derived from the endpoint host.
	AnnotationOTLPCollectorNamespace = "kubeagents.x-k8s.io/otlp-collector-namespace"
	disabledValue                    = "false"

	managedByHelm      = "Helm"
	managedByKustomize = "kustomize"
)

// buildLiteLLMNetworkPolicy builds the operator-managed NetworkPolicy for the LiteLLM gateway.
// It allows:
// Ingress:
//   - Port 8080 from pods in the same namespace (the agent and sidecars)
//   - Port 8080 from gke-gmp-system namespace (Prometheus scraping)
//
// Egress:
//   - Port 53 (UDP/TCP) to kube-dns, node-local-dns, 169.254.20.10/32, 169.254.169.254/32,
//     and discovered cluster DNS VIPs (profile.DNSClusterIPs)
//   - Port 443 (TCP) to 0.0.0.0/0 and ::/0 except private/link-local/multicast space (outbound model provider APIs)
//   - Port 80 (TCP) to 169.254.169.254/32 (GCP metadata server pre-NAT / eBPF)
//   - Port 988 (or profile.MetadataDaemonPort) (TCP) to 169.254.169.254/32 and profile.MetadataDaemonIP/32
//     (GKE Workload Identity host-network daemon post-NAT / iptables, if profile.MetadataDaemonIP != "")
//   - Ports 4317, 4318 (TCP) to litellmOTLPCollectorNamespace(agent) (if non-empty)
func buildLiteLLMNetworkPolicy(agent *agentv1alpha1.PlatformAgent, profile netpolProfile) *networkingv1.NetworkPolicy {
	ingressRules := []networkingv1.NetworkPolicyIngressRule{
		{
			Ports: []networkingv1.NetworkPolicyPort{
				tcpPort(litellmPort),
			},
			From: []networkingv1.NetworkPolicyPeer{
				{
					PodSelector: &metav1.LabelSelector{},
				},
			},
		},
		{
			Ports: []networkingv1.NetworkPolicyPort{
				tcpPort(litellmPort),
			},
			From: []networkingv1.NetworkPolicyPeer{
				{
					NamespaceSelector: &metav1.LabelSelector{
						MatchLabels: map[string]string{
							labelMetadataName: gmpNamespace,
						},
					},
				},
			},
		},
	}

	dnsPeers := []networkingv1.NetworkPolicyPeer{
		namespacedPodPeer(kubeSystemNamespace, map[string]string{labelK8sApp: kubeDNSAppLabel}),
		namespacedPodPeer(kubeSystemNamespace, map[string]string{labelK8sApp: nodeLocalDNSAppLabel}),
		{
			IPBlock: &networkingv1.IPBlock{
				CIDR: dnsNodeLocalLinkLocalCIDR,
			},
		},
		{
			IPBlock: &networkingv1.IPBlock{
				CIDR: metadataLinkLocalCIDR,
			},
		},
	}

	dnsIPs := profile.DNSClusterIPs
	if len(dnsIPs) == 0 {
		// Deliberately narrower than the static copy: it relies on the discovered VIP
		// instead of a 0.0.0.0/0-except-RFC1918 fallback, which is the residual gap Part C closes.
		dnsIPs = []string{defaultDNSClusterIP}
	}
	dnsIPPeers := formatCIDRPeers(dnsIPs, false)
	if len(dnsIPPeers) == 0 {
		dnsIPPeers = formatCIDRPeers([]string{defaultDNSClusterIP}, false)
	}
	dnsPeers = append(dnsPeers, peersNotAlreadyPresent(dnsPeers, dnsIPPeers)...)

	egressRules := []networkingv1.NetworkPolicyEgressRule{
		// 1. Cluster DNS
		{
			Ports: []networkingv1.NetworkPolicyPort{
				udpPort(dnsPort),
				tcpPort(dnsPort),
			},
			To: dnsPeers,
		},
		// 2. HTTPS to external model APIs (OpenAI, Anthropic, Vertex, etc.), excluding RFC 1918 private space, CGNAT, and link-local
		{
			Ports: []networkingv1.NetworkPolicyPort{
				tcpPort(httpsPort),
			},
			To: []networkingv1.NetworkPolicyPeer{
				{
					IPBlock: &networkingv1.IPBlock{
						CIDR: internetAnywhereCIDR,
						Except: []string{
							rfc1918Block10,
							rfc1918Block172,
							rfc1918Block192,
							rfc6598CGNAT,
							linkLocalIPv4Block,
						},
					},
				},
				{
					IPBlock: &networkingv1.IPBlock{
						CIDR: internetAnywhereIPv6CIDR,
						Except: []string{
							ipv6ULA,
							ipv6LinkLocal,
							ipv6Multicast,
						},
					},
				},
			},
		},
		// 3. GCP Metadata Server (pre-NAT link-local address, port 80)
		{
			Ports: []networkingv1.NetworkPolicyPort{
				tcpPort(httpPort),
			},
			To: []networkingv1.NetworkPolicyPeer{
				{
					IPBlock: &networkingv1.IPBlock{
						CIDR: metadataLinkLocalCIDR,
					},
				},
			},
		},
	}

	// 4. GKE Workload Identity host-network daemon (conditional, port 988 or profile.MetadataDaemonPort)
	if profile.MetadataDaemonIP != "" {
		metadataDaemonPeers := formatCIDRPeers([]string{metadataLinkLocalIP, profile.MetadataDaemonIP}, true)
		port := profile.MetadataDaemonPort
		if port == 0 {
			port = metadataDaemonDefaultPort
		}
		egressRules = append(egressRules, networkingv1.NetworkPolicyEgressRule{
			Ports: []networkingv1.NetworkPolicyPort{
				tcpPort(port),
			},
			To: metadataDaemonPeers,
		})
	}

	// 5. OpenTelemetry Collector (conditional)
	if collectorNs := litellmOTLPCollectorNamespace(agent); collectorNs != "" {
		egressRules = append(egressRules, networkingv1.NetworkPolicyEgressRule{
			Ports: []networkingv1.NetworkPolicyPort{
				tcpPort(otlpGRPCPort),
				tcpPort(otlpHTTPPort),
			},
			To: []networkingv1.NetworkPolicyPeer{
				{
					NamespaceSelector: &metav1.LabelSelector{
						MatchLabels: map[string]string{
							labelMetadataName: collectorNs,
						},
					},
				},
			},
		})
	}

	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{
			APIVersion: netpolAPIVersion,
			Kind:       netpolKind,
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      litellmNetworkPolicyName,
			Namespace: agent.Namespace,
			Labels: map[string]string{
				labelName: litellmAppName,
			},
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{
				MatchLabels: map[string]string{
					podLabelApp: litellmAppName,
				},
			},
			PolicyTypes: []networkingv1.PolicyType{
				networkingv1.PolicyTypeIngress,
				networkingv1.PolicyTypeEgress,
			},
			Ingress: ingressRules,
			Egress:  egressRules,
		},
	}
}

// litellmOTLPCollectorNamespace resolves the collector namespace for the LiteLLM NetworkPolicy.
// LiteLLM's OTLP exporter is statically configured by Helm (defaulting to gke-managed-otel)
// and does not participate in the agent's dynamic runtime discovery.
// The ladder resolves the same inputs the same way as Helm's
// kube-agents.otlpCollectorNamespace, except for an endpoint that names no
// in-cluster Service while the LiteLLM exporter is off, where it deliberately differs:
//  1. AnnotationOTLPCollectorNamespace on the PlatformAgent CR if set and valid.
//  2. The namespace extracted from agent.Spec.Telemetry.OTLPEndpoint if specified.
//     An endpoint that names no in-cluster Service yields "", and the caller emits
//     no OTLP rule. The static Helm render does the same with litellm.otel on, and
//     keeps gke-managed-otel there with it off.
//  3. With no endpoint at all, managedOTelCollectorNamespace ("gke-managed-otel").
func litellmOTLPCollectorNamespace(agent *agentv1alpha1.PlatformAgent) string {
	if ns := trimmedAnnotation(agent, AnnotationOTLPCollectorNamespace); ns != "" {
		if errs := validation.IsValidLabelValue(ns); len(errs) == 0 {
			return ns
		}
		logf.Log.Info("Ignoring invalid annotation value: must be a valid label value", "annotation", AnnotationOTLPCollectorNamespace, "value", ns)
	}
	if agent != nil && agent.Spec.Telemetry != nil && agent.Spec.Telemetry.OTLPEndpoint != "" {
		return otlpCollectorNamespace(agent.Spec.Telemetry.OTLPEndpoint)
	}
	return managedOTelCollectorNamespace
}

// deleteManagedLiteLLMPolicy deletes litellm-policy in agent.Namespace only if it is
// managed by the operator (has app.kubernetes.io/managed-by: platformagent-controller).
func (r *PlatformAgentReconciler) deleteManagedLiteLLMPolicy(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	var netpol networkingv1.NetworkPolicy
	err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: litellmNetworkPolicyName}, &netpol)
	if err != nil {
		if errors.IsNotFound(err) {
			return nil
		}
		return fmt.Errorf("failed to get NetworkPolicy %s/%s: %w", agent.Namespace, litellmNetworkPolicyName, err)
	}

	// Never delete a policy that wasn't created by this controller.
	if netpol.Labels != nil && netpol.Labels[labelManagedBy] == fieldOwner {
		if err := r.Delete(ctx, &netpol); err != nil {
			if !errors.IsNotFound(err) {
				return fmt.Errorf("failed to delete managed NetworkPolicy %s/%s: %w", netpol.Namespace, netpol.Name, err)
			}
		} else {
			logf.FromContext(ctx).Info("Deleted managed LiteLLM NetworkPolicy", "namespace", netpol.Namespace, "name", netpol.Name)
		}
	}
	return nil
}

// canAdoptLiteLLMPolicy reports whether existingNetpol can be adopted and managed by the operator.
// It allows adopting policies previously managed by the operator or legacy static policies
// deployed as part of kube-agents (via Helm or Kustomize). Externally-managed policies are preserved.
func canAdoptLiteLLMPolicy(netpol *networkingv1.NetworkPolicy) bool {
	if netpol.Labels == nil {
		return false
	}
	managedBy := netpol.Labels[labelManagedBy]
	if managedBy == fieldOwner {
		return true
	}
	// An empty managed-by label alongside part-of: kube-agents is treated as project-owned
	// (legacy static manifests without explicit managed-by tool attribution).
	if netpol.Labels[labelPartOf] == partOfKubeAgents && (strings.EqualFold(managedBy, managedByHelm) || strings.EqualFold(managedBy, managedByKustomize) || managedBy == "") {
		return true
	}
	return false
}

// reconcileLiteLLMNetworkPolicy manages the litellm-policy NetworkPolicy when LiteLLM is
// present in the agent namespace and networkPolicy generation is enabled.
//
// Lifecycle design: litellm-policy is owned by Deployment/litellm via an OwnerReference (and has
// NO OwnerReference to PlatformAgent), decoupling its runtime object identity from the agent CR.
// When PlatformAgent is deleted while LiteLLM is still running, litellm-policy is preserved to prevent
// fail-open credential exposure. When Deployment/litellm is deleted (e.g. during helm uninstall),
// Kubernetes garbage collection cleans up litellm-policy automatically.
//
// Safe deletion: If network policy generation is disabled (spec.networkPolicy.enabled == false or
// kubeagents.x-k8s.io/enable-litellm-network-policy == "false") or the LiteLLM deployment is not
// found in the namespace, deleteManagedLiteLLMPolicy is called, which deletes the policy only if
// it bears app.kubernetes.io/managed-by: platformagent-controller. Hand-authored
// or unmanaged policies are never deleted.
func (r *PlatformAgentReconciler) reconcileLiteLLMNetworkPolicy(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profile netpolProfile) error {
	if !profile.Generated || strings.EqualFold(trimmedAnnotation(agent, AnnotationEnableLiteLLMNetworkPolicy), disabledValue) {
		return r.deleteManagedLiteLLMPolicy(ctx, agent)
	}

	var litellmDep appsv1.Deployment
	if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: litellmDeploymentName}, &litellmDep); err != nil {
		if errors.IsNotFound(err) {
			return r.deleteManagedLiteLLMPolicy(ctx, agent)
		}
		return fmt.Errorf("failed to get LiteLLM deployment in namespace %s: %w", agent.Namespace, err)
	}
	if litellmDep.DeletionTimestamp != nil {
		return r.deleteManagedLiteLLMPolicy(ctx, agent)
	}

	// Safe adoption: if litellm-policy already exists and is managed by an external entity
	// (e.g. hand-authored or external tool without part-of: kube-agents), do not overwrite it.
	var existingNetpol networkingv1.NetworkPolicy
	if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: litellmNetworkPolicyName}, &existingNetpol); err == nil {
		if !canAdoptLiteLLMPolicy(&existingNetpol) {
			managedBy := ""
			if existingNetpol.Labels != nil {
				managedBy = existingNetpol.Labels[labelManagedBy]
			}
			logf.FromContext(ctx).Info("Skipping LiteLLM NetworkPolicy reconciliation: existing policy is not managed by kube-agents",
				"namespace", agent.Namespace, "name", litellmNetworkPolicyName, "managedBy", managedBy)
			return nil
		}
	} else if !errors.IsNotFound(err) {
		return fmt.Errorf("failed to check existing NetworkPolicy %s/%s: %w", agent.Namespace, litellmNetworkPolicyName, err)
	}

	netpol := buildLiteLLMNetworkPolicy(agent, profile)
	if agent.Spec.Telemetry != nil && agent.Spec.Telemetry.OTLPEndpoint != "" && otlpCollectorNamespace(agent.Spec.Telemetry.OTLPEndpoint) == "" {
		if litellmOTLPCollectorNamespace(agent) == "" {
			// Not a warning: an external endpoint on port 443 is a supported shape, and the
			// exporter leaves over the policy's 443 rule. A collector namespace is the remedy
			// only when the collector is in fact in-cluster behind a host that hides it.
			logf.FromContext(ctx).Info("LiteLLM OTLP endpoint names no in-cluster Service; emitting no OTLP egress rule, the exporter uses the port-443 rule (set the otlp-collector-namespace annotation if the collector is in-cluster)",
				"namespace", agent.Namespace, "endpoint", agent.Spec.Telemetry.OTLPEndpoint)
		}
	}
	if err := controllerutil.SetOwnerReference(&litellmDep, netpol, r.Scheme); err != nil {
		return fmt.Errorf("failed to set owner reference on LiteLLM NetworkPolicy: %w", err)
	}
	if err := r.applyManaged(ctx, agent, netpol); err != nil {
		return fmt.Errorf("failed to apply NetworkPolicy %s/%s: %w", netpol.Namespace, netpol.Name, err)
	}
	return nil
}
