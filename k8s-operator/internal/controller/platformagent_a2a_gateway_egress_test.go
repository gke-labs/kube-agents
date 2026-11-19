package controller

import (
	"context"
	"net/netip"
	"reflect"
	"testing"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// egressDestination is one connection the gateway pod might open: either an
// address, matched against ipBlock peers, or a pod in a namespace, matched
// against selector peers. Never both: a pod's own address is in the cluster's
// private range, which the address cases cover on their own.
type egressDestination struct {
	name      string
	address   string
	namespace string
	podLabels map[string]string
	port      int32
	protocol  corev1.Protocol
}

// egressAdmits reports whether any of the policy's egress rules admits dest,
// evaluating ports (protocol included, TCP when unset, and an EndPort range),
// ipBlock CIDRs with their Except ranges, and namespace and pod selectors, the
// way an enforcing CNI reads them. A rule with no ports admits every port; a
// rule with no peers admits every destination.
func egressAdmits(t *testing.T, policy *networkingv1.NetworkPolicy, dest egressDestination) bool {
	t.Helper()
	protocol := dest.protocol
	if protocol == "" {
		protocol = corev1.ProtocolTCP
	}
	for _, rule := range policy.Spec.Egress {
		if !egressRuleAdmitsPort(rule, dest.port, protocol) {
			continue
		}
		if len(rule.To) == 0 {
			return true
		}
		for _, peer := range rule.To {
			if egressPeerAdmits(t, policy.Namespace, peer, dest) {
				return true
			}
		}
	}
	return false
}

func egressRuleAdmitsPort(rule networkingv1.NetworkPolicyEgressRule, port int32, protocol corev1.Protocol) bool {
	if len(rule.Ports) == 0 {
		return true
	}
	for _, candidate := range rule.Ports {
		ruleProtocol := corev1.ProtocolTCP
		if candidate.Protocol != nil {
			ruleProtocol = *candidate.Protocol
		}
		if ruleProtocol != protocol {
			continue
		}
		if candidate.Port == nil {
			return true
		}
		low := int32(candidate.Port.IntValue())
		high := low
		if candidate.EndPort != nil {
			high = *candidate.EndPort
		}
		if port >= low && port <= high {
			return true
		}
	}
	return false
}

func egressPeerAdmits(t *testing.T, policyNamespace string, peer networkingv1.NetworkPolicyPeer, dest egressDestination) bool {
	t.Helper()
	if peer.IPBlock != nil {
		if dest.address == "" {
			return false
		}
		target := netip.MustParseAddr(dest.address)
		block, err := netip.ParsePrefix(peer.IPBlock.CIDR)
		if err != nil {
			t.Fatalf("an ipBlock the API server would refuse: %q", peer.IPBlock.CIDR)
		}
		if !block.Contains(target) {
			return false
		}
		for _, except := range peer.IPBlock.Except {
			hole, err := netip.ParsePrefix(except)
			if err != nil {
				t.Fatalf("an except the API server would refuse: %q", except)
			}
			if hole.Contains(target) {
				return false
			}
		}
		return true
	}
	if dest.podLabels == nil {
		return false
	}
	if peer.NamespaceSelector == nil {
		if dest.namespace != policyNamespace {
			return false
		}
	} else {
		nsSelector, err := metav1.LabelSelectorAsSelector(peer.NamespaceSelector)
		if err != nil {
			t.Fatal(err)
		}
		if !nsSelector.Matches(labels.Set{"kubernetes.io/metadata.name": dest.namespace}) {
			return false
		}
	}
	if peer.PodSelector == nil {
		return true
	}
	podSelector, err := metav1.LabelSelectorAsSelector(peer.PodSelector)
	if err != nil {
		t.Fatal(err)
	}
	return podSelector.Matches(labels.Set(dest.podLabels))
}

// gatewayEgressAdmitted is what the gateway calls: each must stay reachable,
// or a chat backend, the bus, the broker or the spawner stops working.
func gatewayEgressAdmitted(agent *agentv1alpha1.PlatformAgent, apiServer string) []egressDestination {
	nats := map[string]string{labelPartOf: a2aPartOf, a2aComponentLabel: "nats"}
	return []egressDestination{
		{name: "kube-dns over UDP", namespace: "kube-system", podLabels: map[string]string{"k8s-app": "kube-dns"}, port: 53, protocol: corev1.ProtocolUDP},
		{name: "kube-dns over TCP", namespace: "kube-system", podLabels: map[string]string{"k8s-app": "kube-dns"}, port: 53},
		{name: "the NATS bus", namespace: agent.Namespace, podLabels: nats, port: a2aNATSClientPort},
		{name: "the credential broker", namespace: agent.Namespace, podLabels: credentialProxySelector(agent), port: credentialProxyPort},
		{name: "the API server on 443", address: apiServer, port: apiServerHTTPSPort},
		{name: "the API server on 6443", address: apiServer, port: apiServerAltPort},
		{name: "a public IPv4 address on 443", address: "203.0.113.10", port: 443},
		{name: "a public IPv6 address on 443", address: "2001:db8::10", port: 443},
	}
}

// gatewayEgressDenied is a sample of everything else: other ports on public
// addresses, every excluded range on 443, and in-cluster pods and ports the
// gateway does not call.
func gatewayEgressDenied(agent *agentv1alpha1.PlatformAgent) []egressDestination {
	nats := map[string]string{labelPartOf: a2aPartOf, a2aComponentLabel: "nats"}
	return []egressDestination{
		{name: "a public address on 80", address: "203.0.113.10", port: 80},
		{name: "a public address on 22", address: "203.0.113.10", port: 22},
		{name: "a public address on 443 over UDP", address: "203.0.113.10", port: 443, protocol: corev1.ProtocolUDP},
		{name: "10/8 on 443", address: "10.1.2.3", port: 443},
		{name: "172.16/12 on 443", address: "172.16.0.1", port: 443},
		{name: "192.168/16 on 443", address: "192.168.1.1", port: 443},
		{name: "100.64/10 on 443", address: "100.64.0.1", port: 443},
		{name: "IPv4 link-local on 443", address: "169.254.10.10", port: 443},
		{name: "IPv4 link-local on 80", address: "169.254.10.10", port: 80},
		{name: "IPv6 unique-local on 443", address: "fd00::1", port: 443},
		{name: "IPv6 link-local on 443", address: "fe80::1", port: 443},
		{name: "IPv6 multicast on 443", address: "ff02::1", port: 443},
		{name: "the API server's address on 80", address: defaultAPIServerClusterIP, port: 80},
		{name: "a DNS address on 80", address: defaultDNSClusterIP, port: 80},
		{name: "the NATS bus on another port", namespace: agent.Namespace, podLabels: nats, port: 8222},
		{name: "the credential broker on another port", namespace: agent.Namespace, podLabels: credentialProxySelector(agent), port: 80},
		{name: "LiteLLM", namespace: agent.Namespace, podLabels: map[string]string{"app": "litellm"}, port: a2aLiteLLMContainerPort},
		{name: "the agent gateway pod", namespace: agent.Namespace, podLabels: map[string]string{"app": agent.Name + "-gateway"}, port: 8642},
		{name: "a pod in another namespace on the bus port", namespace: "elsewhere", podLabels: nats, port: a2aNATSClientPort},
	}
}

// assertA2AGatewayEgressAdmitsOnlyWhatItCalls is the policy's property, read
// off whatever object is under test: egress-only, on the gateway pod, every
// call the gateway makes admitted and the sample of everything else denied.
func assertA2AGatewayEgressAdmitsOnlyWhatItCalls(t *testing.T, np *networkingv1.NetworkPolicy, agent *agentv1alpha1.PlatformAgent, apiServer string) {
	t.Helper()
	if !reflect.DeepEqual(np.Spec.PodSelector.MatchLabels, map[string]string{"app": a2aGatewayName(agent)}) {
		t.Errorf("%s selects %+v, want the A2A gateway pod", np.Name, np.Spec.PodSelector)
	}
	if !reflect.DeepEqual(np.Spec.PolicyTypes, []networkingv1.PolicyType{networkingv1.PolicyTypeEgress}) {
		t.Errorf("%s governs %v, want Egress alone: ingress is the fences'", np.Name, np.Spec.PolicyTypes)
	}
	for _, dest := range gatewayEgressAdmitted(agent, apiServer) {
		if !egressAdmits(t, np, dest) {
			t.Errorf("%s denies %s, which the gateway calls", np.Name, dest.name)
		}
	}
	for _, dest := range gatewayEgressDenied(agent) {
		if egressAdmits(t, np, dest) {
			t.Errorf("%s admits %s", np.Name, dest.name)
		}
	}
}

// assertA2AGatewayEgress: the rendered gateway has its egress policy, with
// the default API server address the fake cluster resolves to.
func assertA2AGatewayEgress(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	np := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, a2aGatewayEgressKey(agent), np); err != nil {
		t.Fatalf("the gateway renders and no egress policy %s selects it: %v", a2aGatewayEgressKey(agent).Name, err)
	}
	assertA2AGatewayEgressAdmitsOnlyWhatItCalls(t, np, agent, defaultAPIServerClusterIP)
}

func a2aGatewayEgressKey(agent *agentv1alpha1.PlatformAgent) types.NamespacedName {
	return types.NamespacedName{Name: a2aGatewayEgressName(agent), Namespace: agent.Namespace}
}

// TestTheA2AGatewayEgressPolicyAdmitsOnlyWhatTheGatewayCalls is the policy's
// property with the defaults, and with addresses the reconcile resolved.
func TestTheA2AGatewayEgressPolicyAdmitsOnlyWhatTheGatewayCalls(t *testing.T) {
	agent := a2aTestAgent()
	t.Run("defaults", func(t *testing.T) {
		np := buildA2AGatewayEgressPolicy(agent, nil, nil)
		if np.Name != agent.Name+"-a2a-gateway-egress" || np.Namespace != agent.Namespace {
			t.Errorf("rendered as %s/%s", np.Namespace, np.Name)
		}
		assertA2AGatewayEgressAdmitsOnlyWhatItCalls(t, np, agent, defaultAPIServerClusterIP)
	})
	t.Run("resolved addresses", func(t *testing.T) {
		// A private control plane and a custom DNS VIP: both admitted on their
		// own ports and nothing else, though both sit in excluded ranges.
		np := buildA2AGatewayEgressPolicy(agent, []string{"172.16.0.2/32"}, []string{"10.20.0.10"})
		assertA2AGatewayEgressAdmitsOnlyWhatItCalls(t, np, agent, "172.16.0.2")
		for _, dest := range []egressDestination{
			{name: "the resolved DNS VIP over UDP", address: "10.20.0.10", port: 53, protocol: corev1.ProtocolUDP},
			{name: "the resolved API server on 8443", address: "172.16.0.2", port: apiServerProxyPort},
		} {
			if !egressAdmits(t, np, dest) {
				t.Errorf("denies %s", dest.name)
			}
		}
		for _, dest := range []egressDestination{
			{name: "the resolved DNS VIP on 443", address: "10.20.0.10", port: 443},
			{name: "the resolved API server on 80", address: "172.16.0.2", port: 80},
			{name: "the default API server VIP once resolved past it", address: defaultAPIServerClusterIP, port: 443},
		} {
			if egressAdmits(t, np, dest) {
				t.Errorf("admits %s", dest.name)
			}
		}
	})
}

// TestTheEgressEvaluatorReadsExceptAndPorts keeps the helper honest: a
// helper that ignored Except or ports would pass every denial above for the
// wrong reason, or fail none.
func TestTheEgressEvaluatorReadsExceptAndPorts(t *testing.T) {
	open := &networkingv1.NetworkPolicy{Spec: networkingv1.NetworkPolicySpec{Egress: []networkingv1.NetworkPolicyEgressRule{{}}}}
	if !egressAdmits(t, open, egressDestination{address: "169.254.10.10", port: 80}) {
		t.Error("an empty rule admits everything, and the helper said no")
	}
	rule := publicHTTPSEgressRule()
	https := &networkingv1.NetworkPolicy{Spec: networkingv1.NetworkPolicySpec{Egress: []networkingv1.NetworkPolicyEgressRule{rule}}}
	if egressAdmits(t, https, egressDestination{address: "10.0.0.1", port: 443}) {
		t.Error("the helper ignored an Except range")
	}
	if egressAdmits(t, https, egressDestination{address: "203.0.113.10", port: 444}) {
		t.Error("the helper ignored the port")
	}
	ranged := &networkingv1.NetworkPolicy{Spec: networkingv1.NetworkPolicySpec{Egress: []networkingv1.NetworkPolicyEgressRule{{
		Ports: []networkingv1.NetworkPolicyPort{{Port: ptrIntOrString(53), EndPort: func() *int32 { v := int32(988); return &v }()}},
	}}}}
	if !egressAdmits(t, ranged, egressDestination{address: "203.0.113.10", port: 80}) {
		t.Error("the helper read an EndPort range as its lower bound alone")
	}
}

// TestBothGatewayPoliciesShareThePublicHTTPSRule: the agent gateway's policy
// carries the same external HTTPS rule, so a change to one reaches both.
func TestBothGatewayPoliciesShareThePublicHTTPSRule(t *testing.T) {
	agent := a2aTestAgent()
	want := publicHTTPSEgressRule()
	for name, np := range map[string]*networkingv1.NetworkPolicy{
		"agent gateway": buildNetworkPolicy(agent, nil, netpolProfile{}, false, "", true),
		"A2A gateway":   buildA2AGatewayEgressPolicy(agent, nil, nil),
	} {
		found := false
		for _, rule := range np.Spec.Egress {
			if reflect.DeepEqual(rule, want) {
				found = true
			}
		}
		if !found {
			t.Errorf("the %s policy does not carry publicHTTPSEgressRule", name)
		}
	}
	if np := buildNetworkPolicy(agent, nil, netpolProfile{}, true, "", true); func() bool {
		for _, rule := range np.Spec.Egress {
			if reflect.DeepEqual(rule, want) {
				return true
			}
		}
		return false
	}() {
		t.Error("the agent gateway policy keeps the HTTPS rule with an FQDN policy in its place")
	}
}
