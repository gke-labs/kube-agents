// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package controller

import (
	"testing"

	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const policyTestOperatorNamespace = "operator-ns"

// policyTestMalformedNamespace is not a valid label value: the API server would
// reject a NetworkPolicy that carried it in a selector.
const policyTestMalformedNamespace = "Not A Valid NS!"

// policyTestNonDNS1123Namespace is a valid label value but not a DNS-1123 label
// (uppercase). The selector would carry it, but the API server only ever sets
// kubernetes.io/metadata.name to a namespace's own DNS-1123 name, so it matches
// no namespace on any cluster.
const policyTestNonDNS1123Namespace = "Kubeagents-System"

// operatorPeerRules is every ingress rule of np whose peer is the operator's
// pods, keyed by the ports it opens.
func operatorPeerRules(np *networkingv1.NetworkPolicy) map[int32]networkingv1.NetworkPolicyPeer {
	found := map[int32]networkingv1.NetworkPolicyPeer{}
	for _, rule := range np.Spec.Ingress {
		for _, peer := range rule.From {
			if peer.PodSelector != nil && peer.PodSelector.MatchLabels[operatorPodNameLabel] == operatorPodNameValue {
				for _, port := range rule.Ports {
					found[port.Port.IntVal] = peer
				}
			}
		}
	}
	return found
}

// operatorRules is every ingress rule of np that names the operator -- a peer
// carrying the operator's pod label, a peer whose namespace selector names the
// operator's namespace, or an empty From that admits every source -- returning
// the whole rule, so a test can see what else the rule opens beside that peer (a
// second peer, an empty or wider Ports list) and whether a second rule reaches
// the operator at all, both of which operatorPeerRules drops. The namespace
// selector is evaluated the way a CNI evaluates it (selectorMatches), not read
// off MatchLabels: an empty selector that matches every namespace or a
// MatchExpressions clause naming the operator's is caught as surely as a
// MatchLabels entry, and an empty From -- which has no peer to read at all --
// is caught before the peer loop. Reading MatchLabels alone saw none of the
// three. Keying on the namespace selector as well as the pod label is what
// catches a sibling rule that widens the operator's reach through a
// namespace-only peer, which the pod label alone would miss.
func operatorRules(np *networkingv1.NetworkPolicy, operatorNamespace string) []networkingv1.NetworkPolicyIngressRule {
	operatorNS := labels.Set{labelMetadataName: operatorNamespace}
	var rules []networkingv1.NetworkPolicyIngressRule
	for _, rule := range np.Spec.Ingress {
		if len(rule.From) == 0 {
			rules = append(rules, rule)
			continue
		}
		for _, peer := range rule.From {
			byPod := peer.PodSelector != nil && peer.PodSelector.MatchLabels[operatorPodNameLabel] == operatorPodNameValue
			byNamespace := peer.NamespaceSelector != nil && selectorMatches(peer.NamespaceSelector, operatorNS)
			if byPod || byNamespace {
				rules = append(rules, rule)
				break
			}
		}
	}
	return rules
}

// selectorMatches reports whether sel, evaluated the way a CNI evaluates a
// metav1.LabelSelector -- an empty selector matches everything, and
// MatchExpressions as well as MatchLabels -- selects set. A selector the API
// server would reject matches nothing.
func selectorMatches(sel *metav1.LabelSelector, set labels.Set) bool {
	s, err := metav1.LabelSelectorAsSelector(sel)
	if err != nil {
		return false
	}
	return s.Matches(set)
}

// With the operator's namespace known, the gateway policy gains a rule
// admitting the operator's pods, selected by namespace and pod label, on each
// of its two metrics ports (the watcher's and the chat plugin's) and the broker
// policy one on its metrics port, and no other; without it, neither does.
func TestThePoliciesAdmitTheOperatorOnTheMetricsPortsOnly(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "agent", Namespace: "agents"}}
	profile := defaultTestNetpolProfile()
	profile.OperatorNamespace = policyTestOperatorNamespace

	gateway := operatorPeerRules(buildNetworkPolicy(agent, nil, profile, false, "", false))
	broker := operatorPeerRules(credentialProxyNetworkPolicyWithOperatorPeer(agent, policyTestOperatorNamespace))
	if len(gateway) != 2 || len(broker) != 1 {
		t.Fatalf("operator peer rules: gateway on ports %v, broker on ports %v; want the gateway's two listeners and the broker's one", gateway, broker)
	}
	for name, want := range map[string]struct {
		got   map[int32]networkingv1.NetworkPolicyPeer
		ports []int32
	}{
		"gateway": {gateway, []int32{eventWatcherMetricsPort, chatMetricsPort}},
		"broker":  {broker, []int32{credentialProxyMetricsPort}},
	} {
		for _, port := range want.ports {
			peer, ok := want.got[port]
			if !ok {
				t.Fatalf("%s policy: the operator rule is not on the metrics port %d: %v", name, port, want.got)
			}
			if peer.NamespaceSelector == nil || peer.NamespaceSelector.MatchLabels[labelMetadataName] != policyTestOperatorNamespace {
				t.Errorf("%s policy: the operator peer is not narrowed to the operator's namespace: %+v", name, peer)
			}
		}
	}

	// A namespace the operator cannot be admitted under -- unset, malformed (a
	// selector carrying it would have the API server reject the whole policy), or
	// a valid label value that is not a DNS-1123 label (it would match no
	// namespace) -- renders no operator rule of any shape. Diff the whole ingress
	// against the namespace-unset rendering rather than counting labelled peers,
	// which a rule carrying the pod label but no Ports, or a namespace-only peer,
	// slips past.
	gatewayBaseline := buildNetworkPolicy(agent, nil, defaultTestNetpolProfile(), false, "", false)
	brokerBaseline := buildCredentialProxyNetworkPolicy(agent)
	for _, ns := range []string{"", policyTestMalformedNamespace, policyTestNonDNS1123Namespace} {
		gwProfile := defaultTestNetpolProfile()
		gwProfile.OperatorNamespace = ns
		if gw := buildNetworkPolicy(agent, nil, gwProfile, false, "", false); !equality.Semantic.DeepEqual(gw.Spec.Ingress, gatewayBaseline.Spec.Ingress) {
			t.Errorf("the gateway policy's ingress changes under namespace %q, want no operator rule:\n got %+v\nwant %+v", ns, gw.Spec.Ingress, gatewayBaseline.Spec.Ingress)
		}
		if br := credentialProxyNetworkPolicyWithOperatorPeer(agent, ns); !equality.Semantic.DeepEqual(br.Spec.Ingress, brokerBaseline.Spec.Ingress) {
			t.Errorf("the broker policy's ingress changes under namespace %q, want no operator rule:\n got %+v\nwant %+v", ns, br.Spec.Ingress, brokerBaseline.Spec.Ingress)
		}
	}
	// The operator rule admits nothing besides the operator peer on the metrics
	// port: exactly one rule, and that rule's shape pinned against literals -- one
	// peer, one TCP port equal to the metrics port, the peer narrowed by both the
	// operator namespace label and the operator pod label, and no IPBlock. Pin the
	// shape directly rather than comparing against operatorMetricsIngressRule's
	// output, which both policies append verbatim: widening that builder would
	// widen the comparison with it, so a self-compare passes for the very edit
	// this guards against.
	for name, np := range map[string]*networkingv1.NetworkPolicy{
		"gateway": buildNetworkPolicy(agent, nil, profile, false, "", false),
		"broker":  credentialProxyNetworkPolicyWithOperatorPeer(agent, policyTestOperatorNamespace),
	} {
		// One rule per metrics listener, each opening its one port: the gateway
		// carries the watcher's and the chat plugin's, the broker its own.
		ports := []int32{eventWatcherMetricsPort, chatMetricsPort}
		if name == "broker" {
			ports = []int32{credentialProxyMetricsPort}
		}
		rules := operatorRules(np, policyTestOperatorNamespace)
		if len(rules) != len(ports) {
			t.Fatalf("%s policy: %d rules admit the operator peer, want %d: %+v", name, len(rules), len(ports), rules)
		}
		for i, rule := range rules {
			port := ports[i]
			if len(rule.Ports) != 1 {
				t.Errorf("%s policy: the operator rule opens %d ports, want exactly 1: %+v", name, len(rule.Ports), rule.Ports)
			} else if !equality.Semantic.DeepEqual(rule.Ports[0], tcpPort(port)) {
				t.Errorf("%s policy: the operator rule's port is not TCP %d: %+v", name, port, rule.Ports[0])
			}
			if len(rule.From) != 1 {
				t.Errorf("%s policy: the operator rule has %d peers, want exactly 1: %+v", name, len(rule.From), rule.From)
				continue
			}
			peer := rule.From[0]
			if peer.IPBlock != nil {
				t.Errorf("%s policy: the operator rule carries an IPBlock peer: %+v", name, peer.IPBlock)
			}
			if peer.NamespaceSelector == nil || peer.NamespaceSelector.MatchLabels[labelMetadataName] != policyTestOperatorNamespace {
				t.Errorf("%s policy: the operator peer is not narrowed to the operator namespace: %+v", name, peer.NamespaceSelector)
			}
			if peer.PodSelector == nil || peer.PodSelector.MatchLabels[operatorPodNameLabel] != operatorPodNameValue {
				t.Errorf("%s policy: the operator peer is not narrowed to the operator pod: %+v", name, peer.PodSelector)
			}
		}
	}
	// The builder itself is unchanged: no operator rule, whatever the caller knows.
	if n := len(operatorPeerRules(buildCredentialProxyNetworkPolicy(agent))); n != 0 {
		t.Errorf("buildCredentialProxyNetworkPolicy renders %d operator rule(s), want 0", n)
	}
	// What the reconcile applies is the builder's policy plus exactly that one
	// rule, so the tests that guard the broker's boundary through the builder
	// still describe the applied policy up to the operator peer.
	built := buildCredentialProxyNetworkPolicy(agent)
	applied := credentialProxyNetworkPolicyWithOperatorPeer(agent, policyTestOperatorNamespace)
	if len(applied.Spec.Ingress) != len(built.Spec.Ingress)+1 {
		t.Fatalf("the applied broker policy has %d ingress rules, the builder's %d; want exactly one more", len(applied.Spec.Ingress), len(built.Spec.Ingress))
	}
	for i := range built.Spec.Ingress {
		if !equality.Semantic.DeepEqual(built.Spec.Ingress[i], applied.Spec.Ingress[i]) {
			t.Errorf("ingress rule %d differs between the builder's policy and the applied one", i)
		}
	}
	if !equality.Semantic.DeepEqual(built.Spec.PodSelector, applied.Spec.PodSelector) || !equality.Semantic.DeepEqual(built.Spec.PolicyTypes, applied.Spec.PolicyTypes) {
		t.Error("the applied broker policy differs from the builder's beyond the appended rule")
	}
	// The same whole-rule-set diff for the gateway, which adds its operator rules
	// inline in the builder when the namespace is known rather than through a
	// separate wrapper, each beside the collector rule for the same listener:
	// render it with the namespace unset and set -- the only field that differs
	// between the two profiles -- and the set rendering is the unset one with
	// exactly operatorMetricsIngressRule's output for each listener's port
	// inserted, in listener order, and nothing else added or changed. The
	// operatorRules count check above already pins the operator rule count for
	// the gateway, but it projects a rule onto the operator by its pod label or
	// namespace selector and so cannot see a rule whose only peer is an IPBlock
	// -- a CIDR covering the operator's pod IP names the operator to a CNI yet
	// carries neither selector. Only a diff of the full rule set catches an
	// extra rule of any peer shape.
	gatewayPorts := []int32{eventWatcherMetricsPort, chatMetricsPort}
	gatewayWant := make([]networkingv1.NetworkPolicyIngressRule, 0, len(gatewayPorts))
	for _, port := range gatewayPorts {
		rule, ok := operatorMetricsIngressRule(policyTestOperatorNamespace, port)
		if !ok {
			t.Fatalf("operatorMetricsIngressRule returned no rule for a valid namespace on port %d", port)
		}
		gatewayWant = append(gatewayWant, rule)
	}
	builtGateway := buildNetworkPolicy(agent, nil, defaultTestNetpolProfile(), false, "", false)
	appliedGateway := buildNetworkPolicy(agent, nil, profile, false, "", false)
	if len(appliedGateway.Spec.Ingress) != len(builtGateway.Spec.Ingress)+len(gatewayWant) {
		t.Fatalf("the gateway policy with the operator namespace known has %d ingress rules, without it %d; want exactly %d more", len(appliedGateway.Spec.Ingress), len(builtGateway.Spec.Ingress), len(gatewayWant))
	}
	// Strip the expected operator rules, in order, from the applied set; what
	// remains must be the unset rendering rule for rule.
	remaining := make([]networkingv1.NetworkPolicyIngressRule, 0, len(builtGateway.Spec.Ingress))
	next := 0
	for _, rule := range appliedGateway.Spec.Ingress {
		if next < len(gatewayWant) && equality.Semantic.DeepEqual(rule, gatewayWant[next]) {
			next++
			continue
		}
		remaining = append(remaining, rule)
	}
	if next != len(gatewayWant) {
		t.Errorf("the gateway policy carries %d of the %d operator metrics rules in listener order:\n got %+v\nwant %+v", next, len(gatewayWant), appliedGateway.Spec.Ingress, gatewayWant)
	}
	for i := range builtGateway.Spec.Ingress {
		if i >= len(remaining) || !equality.Semantic.DeepEqual(builtGateway.Spec.Ingress[i], remaining[i]) {
			t.Errorf("gateway ingress rule %d changed when the operator namespace became known", i)
		}
	}
	if !equality.Semantic.DeepEqual(builtGateway.Spec.PodSelector, appliedGateway.Spec.PodSelector) || !equality.Semantic.DeepEqual(builtGateway.Spec.PolicyTypes, appliedGateway.Spec.PolicyTypes) || !equality.Semantic.DeepEqual(builtGateway.Spec.Egress, appliedGateway.Spec.Egress) {
		t.Error("the gateway policy changed beyond the appended operator rule when the operator namespace became known")
	}
}

// A rule that reaches the operator through a namespace-only peer -- the operator
// namespace on any port, no pod label -- is a sibling operatorPeerRules never
// records, because it keeps only peers carrying the pod label. operatorRules now
// keys on the namespace selector too, so the "exactly one rule" check in the
// test above catches such a sibling; this pins that the helper sees it.
func TestOperatorRulesSeeASiblingNamespaceOnlyRule(t *testing.T) {
	metrics, ok := operatorMetricsIngressRule(policyTestOperatorNamespace, eventWatcherMetricsPort)
	if !ok {
		t.Fatalf("operatorMetricsIngressRule returned no rule for a valid namespace")
	}
	// A namespace-only peer on all ports: the worst-case widening the per-peer
	// projection missed on the gateway.
	sibling := networkingv1.NetworkPolicyIngressRule{
		From: []networkingv1.NetworkPolicyPeer{{
			NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{labelMetadataName: policyTestOperatorNamespace}},
		}},
	}
	np := &networkingv1.NetworkPolicy{
		Spec: networkingv1.NetworkPolicySpec{Ingress: []networkingv1.NetworkPolicyIngressRule{metrics, sibling}},
	}
	if n := len(operatorRules(np, policyTestOperatorNamespace)); n != 2 {
		t.Errorf("operatorRules saw %d rules naming the operator, want 2 (the metrics rule and the namespace-only sibling)", n)
	}
	// operatorPeerRules, keyed on the pod label, still sees only the metrics rule:
	// this is the gap operatorRules closes.
	if n := len(operatorPeerRules(np)); n != 1 {
		t.Errorf("operatorPeerRules saw %d operator peers, want 1 (the sibling has no pod label)", n)
	}
}

// operatorRules must count every ingress rule that names the operator, whatever
// selector shape names it: a CNI admits a source through an empty selector that
// matches every namespace, a MatchExpressions clause, or an empty From that
// admits everything, and a helper that reads MatchLabels off the selectors sees
// none of the three. Each "names" case is a single rule operatorRules must
// count; each "does not" case is one it must not, or the "exactly one rule"
// guard in TestThePoliciesAdmitTheOperatorOnTheMetricsPortsOnly would fire on a
// correctly scoped policy.
func TestOperatorRulesCountEveryShapeNamingTheOperator(t *testing.T) {
	ns := policyTestOperatorNamespace
	names := map[string]networkingv1.NetworkPolicyIngressRule{
		"the operator namespace by MatchLabels": {From: []networkingv1.NetworkPolicyPeer{{
			NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{labelMetadataName: ns}},
		}}},
		"the operator pod label": {From: []networkingv1.NetworkPolicyPeer{{
			PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{operatorPodNameLabel: operatorPodNameValue}},
		}}},
		"the operator namespace by MatchExpressions": {From: []networkingv1.NetworkPolicyPeer{{
			NamespaceSelector: &metav1.LabelSelector{MatchExpressions: []metav1.LabelSelectorRequirement{{
				Key: labelMetadataName, Operator: metav1.LabelSelectorOpIn, Values: []string{ns},
			}}},
		}}},
		"an empty namespace selector, which matches every namespace": {From: []networkingv1.NetworkPolicyPeer{{
			NamespaceSelector: &metav1.LabelSelector{},
		}}},
		"an empty From, which admits every source": {},
	}
	for name, rule := range names {
		np := &networkingv1.NetworkPolicy{Spec: networkingv1.NetworkPolicySpec{Ingress: []networkingv1.NetworkPolicyIngressRule{rule}}}
		if n := len(operatorRules(np, ns)); n != 1 {
			t.Errorf("operatorRules saw %d rules naming the operator through %s, want 1", n, name)
		}
	}

	notNames := map[string]networkingv1.NetworkPolicyIngressRule{
		"a different namespace": {From: []networkingv1.NetworkPolicyPeer{{
			NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{labelMetadataName: gmpNamespace}},
		}}},
		"a different pod label": {From: []networkingv1.NetworkPolicyPeer{{
			PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "someone-else"}},
		}}},
		// The baseline in-namespace rule the gateway policy carries: an empty pod
		// selector, no namespace selector, which a CNI scopes to the policy's own
		// namespace rather than the operator's.
		"the baseline in-namespace rule": {From: []networkingv1.NetworkPolicyPeer{{
			PodSelector: &metav1.LabelSelector{},
		}}},
	}
	for name, rule := range notNames {
		np := &networkingv1.NetworkPolicy{Spec: networkingv1.NetworkPolicySpec{Ingress: []networkingv1.NetworkPolicyIngressRule{rule}}}
		if n := len(operatorRules(np, ns)); n != 0 {
			t.Errorf("operatorRules saw %d rules for %s, want 0", n, name)
		}
	}
}
