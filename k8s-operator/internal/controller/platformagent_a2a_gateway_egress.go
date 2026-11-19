package controller

import (
	"context"
	"fmt"

	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The A2A gateway's egress policy (#6553).
//
// Every other policy selecting the gateway pod is ingress-only (the gateway's
// own fence and the doors' copies, buildA2AGatewayNetworkPolicy), so until
// this one its outbound traffic was whatever the pod network allowed. This
// policy is default-deny egress with an allowlist of what the gateway calls:
//
//   - cluster DNS (clusterDNSPeers, port 53);
//   - the NATS bus's pods, on the client port;
//   - the credential broker's pods, on its port: the Google Chat adapter's
//     relay and the report reader both go through it;
//   - the Kubernetes API server, for the session spawner, the replica lease
//     and the ConfigMaps the gateway reads and records into, together with the
//     install's custom egress CIDRs (AnnotationCustomEgressCIDRs), on the same
//     ports, as the agent gateway's policy takes them: the documented place
//     for a private endpoint (a Private Service Connect address, say) that an
//     install's pods reach in place of a public one;
//   - TCP 443 outside the private, link-local and shared ranges, for the chat
//     platforms the gateway dials directly (Slack, Discord) and the A2A door's
//     token check. A NetworkPolicy cannot name a host, so the platforms are an
//     address range, the same one the agent gateway's policy uses
//     (publicHTTPSEgressRule).
//
// Egress only. The ingress fences stay as they are, and a policy with Egress
// alone in its PolicyTypes leaves ingress to them.
//
// It renders and goes with the gateway's own fence: applied where that fence
// is (reconcileA2ANetworkFences, and reconcileA2A on the first render), so
// the Deployment never runs without it, and removed with it when the gateway
// is held for the callout (removeA2AGatewayFence). Like that fence, it does
// not follow spec.networkPolicy.enabled: the flag withholds the agent
// gateway's policy, not the A2A stack's.
//
// Its addresses are the reconcile's: the DNS ladder ungated, as the session
// fence resolves it (a2aSessionDNSClusterIPs), and the API server's targets
// (apiServerEgressTargets, with the custom egress CIDRs), with the same
// defaults the scope publisher's policy falls back to.

// a2aGatewayEgressComponent labels the policy among the A2A objects.
const a2aGatewayEgressComponent = "gateway-egress"

// a2aGatewayEgressName is the gateway egress policy's name.
func a2aGatewayEgressName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-a2a-gateway-egress"
}

// publicHTTPSEgressRule is TCP 443 to every address outside the private,
// link-local and shared ranges, IPv4 and IPv6: the external HTTPS rule of the
// agent gateway's policy (buildNetworkPolicy) and of the A2A gateway's
// (buildA2AGatewayEgressPolicy). One copy, so the two cannot drift apart.
func publicHTTPSEgressRule() networkingv1.NetworkPolicyEgressRule {
	return networkingv1.NetworkPolicyEgressRule{
		Ports: []networkingv1.NetworkPolicyPort{tcpPort(apiServerHTTPSPort)},
		To: []networkingv1.NetworkPolicyPeer{
			{
				IPBlock: &networkingv1.IPBlock{
					CIDR:   "0.0.0.0/0",
					Except: []string{"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"},
				},
			},
			{
				IPBlock: &networkingv1.IPBlock{
					CIDR:   "::/0",
					Except: []string{"fc00::/7", "fe80::/10", "ff00::/8"},
				},
			},
		},
	}
}

// buildA2AGatewayEgressPolicy renders the gateway's egress policy for the API
// server targets and cluster DNS addresses the reconcile resolved; with none,
// the defaults the other policies fall back to.
func buildA2AGatewayEgressPolicy(agent *agentv1alpha1.PlatformAgent, apiCIDRs, dnsIPs []string) *networkingv1.NetworkPolicy {
	if len(dnsIPs) == 0 {
		dnsIPs = []string{defaultDNSClusterIP}
	}
	apiPeers := formatCIDRPeers(apiCIDRs, true)
	if len(apiPeers) == 0 {
		apiPeers = formatCIDRPeers([]string{defaultAPIServerClusterIP}, true)
	}
	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aGatewayEgressName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, a2aGatewayEgressComponent),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: map[string]string{"app": a2aGatewayName(agent)}},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeEgress},
			Egress: []networkingv1.NetworkPolicyEgressRule{
				{
					Ports: []networkingv1.NetworkPolicyPort{udpPort(a2aDNSPort), tcpPort(a2aDNSPort)},
					To:    clusterDNSPeers(dnsIPs),
				},
				{
					Ports: []networkingv1.NetworkPolicyPort{tcpPort(a2aNATSClientPort)},
					To: []networkingv1.NetworkPolicyPeer{
						namespacedPodPeer(agent.Namespace, map[string]string{
							labelPartOf:       a2aPartOf,
							a2aComponentLabel: "nats",
						}),
					},
				},
				{
					Ports: []networkingv1.NetworkPolicyPort{tcpPort(credentialProxyPort)},
					To:    []networkingv1.NetworkPolicyPeer{namespacedPodPeer(agent.Namespace, credentialProxySelector(agent))},
				},
				{
					Ports: []networkingv1.NetworkPolicyPort{
						tcpPort(apiServerHTTPSPort), tcpPort(apiServerAltPort), tcpPort(apiServerProxyPort),
					},
					To: apiPeers,
				},
				publicHTTPSEgressRule(),
			},
		},
	}
}

// a2aGatewayEgressPolicy is the gateway's egress policy with this reconcile's
// addresses resolved.
func (r *PlatformAgentReconciler) a2aGatewayEgressPolicy(ctx context.Context, agent *agentv1alpha1.PlatformAgent, dnsIPs []string) *networkingv1.NetworkPolicy {
	return buildA2AGatewayEgressPolicy(agent, r.apiServerEgressTargets(ctx, agent, true), dnsIPs)
}

// applyA2AGatewayEgress applies the gateway's egress policy, owned by agent.
func (r *PlatformAgentReconciler) applyA2AGatewayEgress(ctx context.Context, agent *agentv1alpha1.PlatformAgent, dnsIPs []string) error {
	np := r.a2aGatewayEgressPolicy(ctx, agent, dnsIPs)
	if err := ctrl.SetControllerReference(agent, np, r.Scheme); err != nil {
		return err
	}
	if err := r.applyManaged(ctx, agent, np); err != nil {
		return fmt.Errorf("failed to apply A2A NetworkPolicy %s: %w", np.Name, err)
	}
	return nil
}
