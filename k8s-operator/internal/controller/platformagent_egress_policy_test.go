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
	"net/netip"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// dnsPort is the one port on which the rendered policy is allowed to name a
// metadata address; see permitsBeyondDNS.
const dnsPort = 53

// egressPolicyAgent is an agent with the allowlist on.
func egressPolicyAgent(mutate ...func(*agentv1alpha1.PlatformAgent)) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "test-agent",
			Namespace:  "test-ns",
			UID:        types.UID("agent-uid"),
			Finalizers: []string{platformAgentFinalizer},
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:    "proj",
				Location:     "us-central1",
				ClusterName:  "cluster",
				EventWatcher: &agentv1alpha1.EventWatcherSpec{Enabled: ptr.To(false)},
			},
			AgentSpec: agentv1alpha1.AgentSpec{
				Security: &agentv1alpha1.SecuritySpec{
					EgressPolicy: egressPolicyAllowlist,
				},
			},
		},
	}
	for _, m := range mutate {
		m(agent)
	}
	return agent
}

// permits reports whether any rule in the policy would let a packet reach addr.
//
// It models what the dataplane does rather than what the rule text looks like:
// an egress rule with no peers permits everything, and an ipBlock permits every
// address its CIDR contains. It deliberately does NOT honour an "except"
// clause, because the whole point of the default-deny shape is that we are not
// relying on one — see the package comment on
// platformagent_egress_policy.go and kubernetes/kubernetes#68078.
func permits(policy *networkingv1.NetworkPolicy, address string) bool {
	target := netip.MustParseAddr(address)
	for _, rule := range policy.Spec.Egress {
		if len(rule.To) == 0 {
			return true
		}
		for _, peer := range rule.To {
			if peer.IPBlock == nil {
				continue
			}
			prefix, err := netip.ParsePrefix(peer.IPBlock.CIDR)
			if err != nil {
				continue
			}
			// Model the enforcer, not netip. net.ParseCIDR — which the API
			// server's ipBlock validation and the CNI both sit on — normalises
			// ::ffff:0.0.0.0/96 to 0.0.0.0/0, while netip.Prefix.Contains
			// refuses to compare across families and would call it inert. A
			// helper that believed netip here would report "denied" for a rule
			// that permits the whole internet in the cluster.
			if prefix.Overlaps(netip.MustParsePrefix("::ffff:0.0.0.0/96")) {
				return true
			}
			if prefix.Contains(target) {
				return true
			}
		}
	}
	return false
}

// permitsBeyondDNS is permits, restricted to the rules that can carry a
// credential request: every rule except one whose port list is nothing but 53.
//
// This is the shape of the invariant after the DNS rule started naming the
// metadata address. It stays a property rather than an enumeration of the
// credential ports on purpose — a future rule permitting the metadata server on
// some port nobody has thought of yet is caught by this and would not be caught
// by a list of 80, 987, 988 and 8080.
//
// The exemption is granted to one address, not to the DNS rule. metadataLinkLocalIP
// is the resolver under Cloud DNS for GKE and is deliberately on that rule;
// metadataDaemonIP and the IPv6 endpoint answer the token API and no DNS at all,
// so there is no reading of "DNS-only" that should let them onto it. Exempting
// the rule itself would drop them from every assertion in this file the moment
// someone added one to the peer list — port 53 included — and the package comment
// on metadataServerAddresses promises the opposite.
func permitsBeyondDNS(policy *networkingv1.NetworkPolicy, address string) bool {
	exemptDNSRule := address == metadataLinkLocalIP

	beyondDNS := &networkingv1.NetworkPolicy{Spec: networkingv1.NetworkPolicySpec{}}
	for _, rule := range policy.Spec.Egress {
		if exemptDNSRule && ruleIsDNSOnly(rule) {
			continue
		}
		beyondDNS.Spec.Egress = append(beyondDNS.Spec.Egress, rule)
	}
	return permits(beyondDNS, address)
}

// ruleIsDNSOnly reports whether every port the rule names is 53. A rule with no
// ports permits all of them and is never DNS-only.
//
// An EndPort disqualifies the rule outright. A NetworkPolicyPort carrying one is
// a range, and Port is only its lower bound: {Port: 53, EndPort: 988} permits
// every port from 53 to 988 inclusive — the post-NAT token port among them —
// while IntValue() still reads 53. Testing the lower bound alone would call that
// rule DNS-only and drop it before permits ever saw it, which is precisely the
// exemption this helper must not grant.
func ruleIsDNSOnly(rule networkingv1.NetworkPolicyEgressRule) bool {
	if len(rule.Ports) == 0 {
		return false
	}
	for _, candidate := range rule.Ports {
		if candidate.EndPort != nil {
			return false
		}
		if candidate.Port == nil || candidate.Port.IntValue() != dnsPort {
			return false
		}
	}
	return true
}

// allowsPeerOnPort reports whether the policy has a rule that both selects a
// Pod carrying podLabels in the named namespace and names port.
//
// It evaluates the selectors rather than comparing structs, so a rule
// expressed differently but equivalently still counts — the test is about
// reachability, not about the shape of the manifest.
func allowsPeerOnPort(policy *networkingv1.NetworkPolicy, namespace string, podLabels map[string]string, port int32) bool {
	for _, rule := range policy.Spec.Egress {
		if !ruleNamesPort(rule, port) {
			continue
		}
		for _, peer := range rule.To {
			if peer.PodSelector == nil {
				continue
			}
			// A peer with no namespaceSelector selects the policy's own
			// namespace, which is how the Hindsight peer (and the gateway
			// policy's) is written.
			if peer.NamespaceSelector == nil {
				if namespace != policy.Namespace {
					continue
				}
			} else {
				nsSelector, err := metav1.LabelSelectorAsSelector(peer.NamespaceSelector)
				if err != nil || !nsSelector.Matches(labelSet(map[string]string{"kubernetes.io/metadata.name": namespace})) {
					continue
				}
			}
			podSelector, err := metav1.LabelSelectorAsSelector(peer.PodSelector)
			if err != nil || !podSelector.Matches(labelSet(podLabels)) {
				continue
			}
			return true
		}
	}
	return false
}

// ruleNamesPort reports whether the rule permits port. A rule with no ports
// permits every port.
func ruleNamesPort(rule networkingv1.NetworkPolicyEgressRule, port int32) bool {
	if len(rule.Ports) == 0 {
		return true
	}
	for _, candidate := range rule.Ports {
		if candidate.Port != nil && candidate.Port.IntValue() == int(port) {
			return true
		}
	}
	return false
}

func labelSet(from map[string]string) labels.Set {
	return labels.Set(from)
}

// TestTheRenderedPolicyDeniesEveryMetadataAddress is the assertion this whole
// task exists for, and it is written as a property over the rendered object
// rather than as a check that a particular rule is absent: a future rule added
// for a good reason has to keep satisfying it.
//
// All three addresses matter. 169.254.169.254 is what a Pod's own code
// connects to; 169.254.169.252 is where an iptables dataplane has already
// DNATed that request by the time policy is evaluated; fd20:ce::254 is the
// documented IPv6 metadata address, which a dual-stack Pod reaches without
// touching either IPv4 one.
//
// It asks permitsBeyondDNS rather than permits because the DNS rule names
// 169.254.169.254 on port 53 deliberately — that is the resolver under Cloud
// DNS for GKE. The property being defended is that no rule permits a metadata
// address on a port a token can be minted over, which is every port but that
// one, and for the other two addresses it is every port without exception.
// TestTheDNSRuleReachesTheCloudDNSResolver holds the other side of it.
func TestTheRenderedPolicyDeniesEveryMetadataAddress(t *testing.T) {
	policy, _ := buildAgentEgressNetworkPolicy(egressPolicyAgent(), nil, managedOTelCollectorNamespace)

	for _, address := range metadataServerAddresses {
		if permitsBeyondDNS(policy, address) {
			scope := "on any port"
			if address == metadataLinkLocalIP {
				scope = "on a port other than 53"
			}
			t.Errorf("the rendered egress policy permits the metadata server at %s %s; anything that "+
				"can make an HTTP request there can mint the Workload Identity token and bypass the "+
				"credential broker entirely", address, scope)
		}
	}
}

// TestTheDNSRuleReachesTheCloudDNSResolver is the Cloud DNS for GKE half of the
// invariant above, and it is a separate test because the two fail for opposite
// reasons: that one catches the metadata server being reopened, this one
// catches it being closed so thoroughly that the Pod cannot resolve a name.
//
// Under Cloud DNS the node answers DNS at 169.254.169.254:53 and every Pod's
// resolv.conf names it. With this peer missing, the allowlist below it is
// unreachable in full — the broker, LiteLLM and the control plane are all
// addressed by name — so the symptom is a total outage that reads like a
// credential bug, which is how it was first reported.
func TestTheDNSRuleReachesTheCloudDNSResolver(t *testing.T) {
	policy, _ := buildAgentEgressNetworkPolicy(egressPolicyAgent(), nil, managedOTelCollectorNamespace)

	if !permits(policy, metadataLinkLocalIP) {
		t.Errorf("the DNS rule does not name %s, so a Cloud DNS for GKE cluster has no resolver and "+
			"every destination in the allowlist becomes unreachable by name", metadataLinkLocalIP)
	}

	// That it is granted on 53 alone is TestTheRenderedPolicyDeniesEveryMetadataAddress's
	// half, and it genuinely covers the case: a single rule naming both 53 and 80
	// is not DNS-only, so permitsBeyondDNS keeps it and that test fails on it.
	// Re-checking it here would only restate the same property more narrowly.
}

// TestRuleIsDNSOnlyRefusesAnythingButBareFiftyThree tests the helper rather than
// the policy, because every metadata assertion in this file is only as strong as
// this predicate: a rule it wrongly calls DNS-only is a rule permitsBeyondDNS
// drops before permits can object to it. The exemption has to be impossible to
// widen by accident, so the cases below are the ways a rule can name 53 and
// still reach further.
func TestRuleIsDNSOnlyRefusesAnythingButBareFiftyThree(t *testing.T) {
	port := func(number int32) *intstr.IntOrString {
		value := intstr.FromInt32(number)
		return &value
	}
	endPort := func(number int32) *int32 { return &number }
	namedPort := func(name string) *intstr.IntOrString {
		value := intstr.FromString(name)
		return &value
	}

	for _, testCase := range []struct {
		name  string
		ports []networkingv1.NetworkPolicyPort
		want  bool
	}{
		{"bare 53 is the exemption", []networkingv1.NetworkPolicyPort{{Port: port(dnsPort)}}, true},
		{"UDP and TCP 53 together are still DNS", []networkingv1.NetworkPolicyPort{{Port: port(dnsPort)}, {Port: port(dnsPort)}}, true},
		{"no ports permits everything", nil, false},
		{"53 alongside the pre-NAT token port", []networkingv1.NetworkPolicyPort{{Port: port(dnsPort)}, {Port: port(80)}}, false},
		{"a range starting at 53 reaches the post-NAT token port", []networkingv1.NetworkPolicyPort{{Port: port(dnsPort), EndPort: endPort(988)}}, false},
		{"an entry with no port at all permits everything on its protocol", []networkingv1.NetworkPolicyPort{{Port: nil}}, false},
		{"a named port is not 53, whatever it resolves to", []networkingv1.NetworkPolicyPort{{Port: namedPort("dns")}}, false},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			got := ruleIsDNSOnly(networkingv1.NetworkPolicyEgressRule{Ports: testCase.ports})
			if got != testCase.want {
				t.Errorf("ruleIsDNSOnly(%v) = %v, want %v; a wrong answer here silently exempts the "+
					"rule from every metadata assertion in this file", testCase.ports, got, testCase.want)
			}
		})
	}
}

// TestTheRenderedPolicyIsDefaultDeny pins the two spec-level properties that
// make the deny work at all. Without Egress in policyTypes the object selects
// the Pod and restricts nothing; with a rule that has no peers, everything is
// permitted regardless of the other rules.
func TestTheRenderedPolicyIsDefaultDeny(t *testing.T) {
	agent := egressPolicyAgent()
	policy, _ := buildAgentEgressNetworkPolicy(agent, nil, managedOTelCollectorNamespace)

	found := false
	for _, policyType := range policy.Spec.PolicyTypes {
		if policyType == networkingv1.PolicyTypeEgress {
			found = true
		}
		if policyType == networkingv1.PolicyTypeIngress {
			t.Error("the policy must not declare Ingress: doing so default-denies inbound as a side " +
				"effect, cutting off the agent's own API Service and the session-KV listener on 8699")
		}
	}
	if !found {
		t.Fatal("without PolicyTypeEgress the object selects the Pod and restricts nothing")
	}

	if got := policy.Spec.PodSelector.MatchLabels["app"]; got != agent.Name+"-gateway" {
		t.Errorf("the policy must select the agent Pod, got app=%q", got)
	}
	if permits(policy, "8.8.8.8") {
		t.Error("the policy permits an arbitrary internet address; it is not default-deny")
	}
}

// TestTheBrokerPodIsNotSelectedByTheEgressPolicy is the other half of the same
// property, and the reason this task depended on the Pod split. The broker
// reaches the metadata server on purpose. If the policy ever selected it, the
// agent would lose its credentials rather than its escape route.
func TestTheBrokerPodIsNotSelectedByTheEgressPolicy(t *testing.T) {
	agent := egressPolicyAgent()
	policy, _ := buildAgentEgressNetworkPolicy(agent, nil, managedOTelCollectorNamespace)

	brokerLabels := map[string]string{"app": credentialBrokerName(agent)}
	selector, err := metav1.LabelSelectorAsSelector(&policy.Spec.PodSelector)
	if err != nil {
		t.Fatalf("the rendered pod selector does not parse: %v", err)
	}
	if selector.Matches(labelSet(brokerLabels)) {
		t.Error("the egress policy selects the credential broker Pod; it mints the cloud token from " +
			"the metadata server, so denying it there breaks every proxied command")
	}
}

// TestTheAllowlistCoversWhatTheAgentCannotRunWithout is the under-allow guard.
// Each destination here is derived from a fixed value in this repository's own
// source, cited in the failure message, so a reviewer can check the claim
// rather than trust it.
func TestTheAllowlistCoversWhatTheAgentCannotRunWithout(t *testing.T) {
	agent := egressPolicyAgent()
	policy, _ := buildAgentEgressNetworkPolicy(agent, nil, managedOTelCollectorNamespace)

	cases := []struct {
		name   string
		labels map[string]string
		ns     string
		port   int32
		why    string
	}{
		{
			name: "kube-dns", ns: "kube-system", labels: map[string]string{"k8s-app": "kube-dns"}, port: 53,
			why: "every other destination is reached by name; without DNS the allowlist is a total block",
		},
		{
			name: "the credential broker", ns: agent.Namespace,
			labels: map[string]string{"app": credentialBrokerName(agent)}, port: credentialProxyPort,
			why: "CREDENTIAL_PROXY_URL, GOOGLE_CHAT_RELAY_URL and SLACK_RELAY_URL all address it (credentialProxyBaseURL)",
		},
		{
			name: "the shell sandbox's sshd", ns: agent.Namespace,
			labels: shellSandboxSelector(agent), port: shellSandboxPort,
			why: "every command the model runs executes in the sandbox Pod, and this policy can be the " +
				"only one rendered — networkPolicy.enabled: false with egressPolicy: Allowlist deletes " +
				"the gateway policy that carries the matching rule, leaving an agent that reads Ready " +
				"and cannot run anything",
		},
		{
			name: "litellm on the port this repository's deployments listen on", ns: agent.Namespace,
			labels: map[string]string{"app": "litellm"}, port: 8080,
			why: "buildAgentConfig pins model base_url to http://litellm.<ns>.svc.cluster.local/v1 unconditionally, " +
				"and the chart, kustomize and example deployments all start LiteLLM with --port 8080 — a " +
				"Pod-selector peer matches after the ClusterIP translation, so 8080 is the port that carries " +
				"every model call",
		},
		{
			name: "litellm on the upstream default port", ns: agent.Namespace,
			labels: map[string]string{"app": "litellm"}, port: 4000,
			why: "the gateway policy names it for deployments these manifests did not render, and the two " +
				"policies must not disagree about the model gateway",
		},
		{
			name: "the Hindsight memory API", ns: agent.Namespace,
			labels: map[string]string{"app.kubernetes.io/name": "hindsight", "app.kubernetes.io/component": "api"},
			port:   8888,
			why: "buildPodEnv sets HINDSIGHT_API_URL on every agent container, and the install this rule " +
				"saves is the one that enforces the policy — the same lesson buildNetworkPolicy's rule 10 " +
				"records",
		},
	}

	for _, tc := range cases {
		if !allowsPeerOnPort(policy, tc.ns, tc.labels, tc.port) {
			t.Errorf("the allowlist does not reach %s on port %d — %s", tc.name, tc.port, tc.why)
		}
	}
}

// TestTheControlPlaneRuleIsAbsentUntilAskedFor pins the deliberate under-allow.
// NetworkPolicy has no peer for "the Kubernetes API server" and the operator
// cannot derive its address, so the choice was between omitting the rule and
// inventing a range. Omitting it costs the agent container its API-server
// connection, which matters above one replica where it runs leader_elect.py;
// that cost is documented on the CRD field and must not be quietly paid off
// with a guess. (Not the event watcher — the split this policy requires
// already refuses to render while the watcher is enabled.)
func TestTheControlPlaneRuleIsAbsentUntilAskedFor(t *testing.T) {
	policy, _ := buildAgentEgressNetworkPolicy(egressPolicyAgent(), nil, managedOTelCollectorNamespace)
	if permits(policy, "172.16.0.2") {
		t.Error("a control-plane range was rendered without egressAllowlist.controlPlaneCIDRs asking for one")
	}

	configured, _ := buildAgentEgressNetworkPolicy(egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Security.EgressAllowlist = &agentv1alpha1.EgressAllowlistSpec{
			ControlPlaneCIDRs: []string{"172.16.0.0/28"},
		}
	}), nil, managedOTelCollectorNamespace)
	if !permits(configured, "172.16.0.2") {
		t.Error("egressAllowlist.controlPlaneCIDRs was supplied but the API server is still unreachable")
	}
	// And supplying it must not have opened anything else. Checking only the
	// metadata addresses here is not enough — the metadata server does not
	// serve 443, so a control-plane rule of 0.0.0.0/0 would satisfy that check
	// while handing the sandbox the whole internet over HTTPS.
	assertClosed(t, configured, egressPolicyAgent(), "control-plane-configured")
}

// TestAControlPlaneCIDRCannotBeTheWholeInternet closes the gap that
// controlPlaneCIDRs was one function away from being: a field named for a /28
// that would render allow-TCP/443-to-anywhere if handed 0.0.0.0/0. This policy
// is sold as an exfiltration control as well as a metadata one, and a hole in
// a field named for the control plane is the last place anyone would look.
func TestAControlPlaneCIDRCannotBeTheWholeInternet(t *testing.T) {
	cases := []struct {
		name    string
		cidr    string
		refused bool
	}{
		{name: "a private cluster's /28", cidr: "172.16.0.0/28"},
		{name: "a public endpoint as a single address", cidr: "34.28.1.5/32"},
		{name: "the generous end of the bound", cidr: "10.1.0.0/16"},

		{name: "the whole IPv4 internet", cidr: "0.0.0.0/0", refused: true},
		{name: "a whole /8", cidr: "10.0.0.0/8", refused: true},
		{name: "the link-local range", cidr: "169.254.0.0/16", refused: true},
		{name: "the whole IPv6 internet", cidr: "::/0", refused: true},
		{name: "a /24 of IPv6", cidr: "2600::/24", refused: true},
		{name: "an unparseable range", cidr: "controlplane.example.com", refused: true},

		// The IPv4-mapped forms. netip reads these as inert 128-bit IPv6
		// prefixes — the width bound compares 96 or 128 against the IPv4
		// threshold of 16 and passes, and the metadata loop cannot match an
		// IPv4 address inside them because Contains is false across families.
		// net.ParseCIDR, which the API server and the CNI sit on, normalises
		// them to 0.0.0.0/0 and 169.254.169.254/32 respectively.
		{name: "the whole internet in IPv4-mapped form", cidr: "::ffff:0.0.0.0/96", refused: true},
		{name: "the metadata server in IPv4-mapped form", cidr: "::ffff:169.254.169.254/128", refused: true},
		{name: "an otherwise-fine range in IPv4-mapped form", cidr: "::ffff:140.82.112.0/116", refused: true},
		// Not written in mapped form, but wide enough to cover it, and not
		// caught by the metadata loop because fd20:ce::254 is in the other half.
		{name: "an IPv6 prefix wide enough to reach the mapped range", cidr: "::/1", refused: true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			agent := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
				a.Spec.Security.EgressAllowlist = &agentv1alpha1.EgressAllowlistSpec{
					ControlPlaneCIDRs: []string{tc.cidr},
				}
			})
			policy, dropped := buildAgentEgressNetworkPolicy(agent, nil, managedOTelCollectorNamespace)
			reason, _ := validateEgressPolicy(agent)

			if !tc.refused {
				if len(dropped) != 0 {
					t.Fatalf("a legitimate control-plane range was dropped: %v", dropped)
				}
				if reason != "" {
					t.Fatalf("a legitimate control-plane range was refused: %s", reason)
				}
				return
			}
			if len(dropped) != 1 {
				t.Errorf("the range was rendered; the builder must drop it, dropped=%v", dropped)
			}
			if reason != "EgressAllowlistRefused" {
				t.Errorf("the range must also make the agent Degraded, got reason %q", reason)
			}
			assertClosed(t, policy, agent, "control-plane-refused")
		})
	}
}

// TestARefusedAllowlistEntryIsReportedNotJustLogged is IMPORTANT 1 from review.
// The CRD promised a Degraded report for a dropped rule and the code only
// logged one, so the failure an operator would actually hit was: add a rule to
// restore GitHub, rule silently dropped, agent Ready, GitHub unreachable,
// nothing in kubectl describe connecting the two.
func TestARefusedAllowlistEntryIsReportedNotJustLogged(t *testing.T) {
	scheme := setupScheme()
	agent := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Security.EgressAllowlist = &agentv1alpha1.EgressAllowlistSpec{
			ExtraRules: []networkingv1.NetworkPolicyEgressRule{{
				To: []networkingv1.NetworkPolicyPeer{{IPBlock: &networkingv1.IPBlock{
					CIDR: "0.0.0.0/0", Except: []string{"169.254.169.254/32"},
				}}},
			}},
		}
	})
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(ssaApplyInterceptor()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile failed: %v", err)
	}

	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatalf("failed to re-read the agent: %v", err)
	}
	if stored.Status.Phase != "Degraded" {
		t.Errorf("a refused allowlist entry must not leave the agent Ready, got phase %q", stored.Status.Phase)
	}
	var reason, message string
	for _, condition := range stored.Status.Conditions {
		if condition.Type == "Ready" {
			reason, message = condition.Reason, condition.Message
		}
	}
	if reason != "EgressAllowlistRefused" {
		t.Errorf("the Ready condition must name the refusal, got %q", reason)
	}
	if !strings.Contains(message, "extraRules[0]") {
		t.Errorf("the message must name which entry was refused so it can be found and fixed, got %q", message)
	}
}

// TestAnExtraRuleTheAPIServerWouldRejectIsRefusedNotApplied covers the shapes
// the CRD stores and networking.k8s.io/v1 refuses. ExtraRules is the raw
// upstream type, so without this screen the rejection arrives as an apply
// error at step 11b and returns from Reconcile above the Service, the gateway
// guardrail and the status update — a wedged reconcile whose reason lives in a
// log line. The property under test is that the reconcile parks Degraded with
// the rule named instead of erroring, and that a valid except survives the
// screen: the guard must refuse what the API server refuses and nothing more.
func TestAnExtraRuleTheAPIServerWouldRejectIsRefusedNotApplied(t *testing.T) {
	cases := []struct {
		name    string
		rule    networkingv1.NetworkPolicyEgressRule
		refused bool
	}{
		{
			name:    "a peer naming nothing",
			rule:    networkingv1.NetworkPolicyEgressRule{To: []networkingv1.NetworkPolicyPeer{{}}},
			refused: true,
		},
		{
			name: "an except outside its cidr",
			rule: networkingv1.NetworkPolicyEgressRule{To: []networkingv1.NetworkPolicyPeer{{
				IPBlock: &networkingv1.IPBlock{CIDR: "140.82.112.0/20", Except: []string{"10.0.0.0/8"}},
			}}},
			refused: true,
		},
		{
			name: "an unparseable except",
			rule: networkingv1.NetworkPolicyEgressRule{To: []networkingv1.NetworkPolicyPeer{{
				IPBlock: &networkingv1.IPBlock{CIDR: "140.82.112.0/20", Except: []string{"not-a-cidr"}},
			}}},
			refused: true,
		},
		{
			// Upstream ValidateIPBlock rejects an except of equal mask length
			// too — "strict subset" — the same reading toEgressRules applies
			// to additionalEgress one file over.
			name: "an except equal to its cidr",
			rule: networkingv1.NetworkPolicyEgressRule{To: []networkingv1.NetworkPolicyPeer{{
				IPBlock: &networkingv1.IPBlock{CIDR: "140.82.112.0/20", Except: []string{"140.82.112.0/20"}},
			}}},
			refused: true,
		},
		{
			name: "a peer carrying both an ipBlock and a selector",
			rule: networkingv1.NetworkPolicyEgressRule{To: []networkingv1.NetworkPolicyPeer{{
				IPBlock:     &networkingv1.IPBlock{CIDR: "140.82.112.0/20"},
				PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "x"}},
			}}},
			refused: true,
		},
		{
			name: "a lowercase protocol",
			rule: networkingv1.NetworkPolicyEgressRule{
				Ports: []networkingv1.NetworkPolicyPort{{Protocol: ptr.To(corev1.Protocol("tcp")), Port: ptr.To(intstr.FromInt32(443))}},
				To: []networkingv1.NetworkPolicyPeer{{
					IPBlock: &networkingv1.IPBlock{CIDR: "140.82.112.0/20"},
				}},
			},
			refused: true,
		},
		{
			name: "an endPort below its port",
			rule: networkingv1.NetworkPolicyEgressRule{
				Ports: []networkingv1.NetworkPolicyPort{{Port: ptr.To(intstr.FromInt32(443)), EndPort: ptr.To(int32(80))}},
				To: []networkingv1.NetworkPolicyPeer{{
					IPBlock: &networkingv1.IPBlock{CIDR: "140.82.112.0/20"},
				}},
			},
			refused: true,
		},
		{
			name: "an endPort with no port",
			rule: networkingv1.NetworkPolicyEgressRule{
				Ports: []networkingv1.NetworkPolicyPort{{EndPort: ptr.To(int32(443))}},
				To: []networkingv1.NetworkPolicyPeer{{
					IPBlock: &networkingv1.IPBlock{CIDR: "140.82.112.0/20"},
				}},
			},
			refused: true,
		},
		{
			name: "an except inside its cidr",
			rule: networkingv1.NetworkPolicyEgressRule{To: []networkingv1.NetworkPolicyPeer{{
				IPBlock: &networkingv1.IPBlock{CIDR: "140.82.112.0/20", Except: []string{"140.82.113.0/24"}},
			}}},
			refused: false,
		},
		{
			name: "a zero port",
			rule: networkingv1.NetworkPolicyEgressRule{
				Ports: []networkingv1.NetworkPolicyPort{{Protocol: ptr.To(corev1.ProtocolTCP), Port: ptr.To(intstr.FromInt32(0))}},
				To: []networkingv1.NetworkPolicyPeer{{
					IPBlock: &networkingv1.IPBlock{CIDR: "140.82.112.0/20"},
				}},
			},
			refused: true,
		},
		{
			name: "an endPort past 65535",
			rule: networkingv1.NetworkPolicyEgressRule{
				Ports: []networkingv1.NetworkPolicyPort{{Port: ptr.To(intstr.FromInt32(443)), EndPort: ptr.To(int32(70000))}},
				To: []networkingv1.NetworkPolicyPeer{{
					IPBlock: &networkingv1.IPBlock{CIDR: "140.82.112.0/20"},
				}},
			},
			refused: true,
		},
		{
			name: "an invalid port name",
			rule: networkingv1.NetworkPolicyEgressRule{
				Ports: []networkingv1.NetworkPolicyPort{{Port: ptr.To(intstr.FromString("Not_A_Port!"))}},
				To: []networkingv1.NetworkPolicyPeer{{
					IPBlock: &networkingv1.IPBlock{CIDR: "140.82.112.0/20"},
				}},
			},
			refused: true,
		},
		{
			name: "an In expression with no values",
			rule: networkingv1.NetworkPolicyEgressRule{To: []networkingv1.NetworkPolicyPeer{{
				PodSelector: &metav1.LabelSelector{MatchExpressions: []metav1.LabelSelectorRequirement{{
					Key: "app", Operator: metav1.LabelSelectorOpIn, Values: nil,
				}}},
			}}},
			refused: true,
		},
		{
			name: "a matchLabels key with a space",
			rule: networkingv1.NetworkPolicyEgressRule{To: []networkingv1.NetworkPolicyPeer{{
				NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"my app": "x"}},
			}}},
			refused: true,
		},
		{
			name: "a valid selector-only peer",
			rule: networkingv1.NetworkPolicyEgressRule{To: []networkingv1.NetworkPolicyPeer{{
				PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "example"}},
			}}},
			refused: false,
		},
		{
			name: "a valid named port",
			rule: networkingv1.NetworkPolicyEgressRule{
				Ports: []networkingv1.NetworkPolicyPort{{Port: ptr.To(intstr.FromString("https"))}},
				To: []networkingv1.NetworkPolicyPeer{{
					IPBlock: &networkingv1.IPBlock{CIDR: "140.82.112.0/20"},
				}},
			},
			refused: false,
		},
		{
			name: "a well-formed port range",
			rule: networkingv1.NetworkPolicyEgressRule{
				Ports: []networkingv1.NetworkPolicyPort{{Protocol: ptr.To(corev1.ProtocolTCP), Port: ptr.To(intstr.FromInt32(443)), EndPort: ptr.To(int32(8443))}},
				To: []networkingv1.NetworkPolicyPeer{{
					IPBlock: &networkingv1.IPBlock{CIDR: "140.82.112.0/20"},
				}},
			},
			refused: false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			agent := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
				a.Spec.Security.EgressAllowlist = &agentv1alpha1.EgressAllowlistSpec{
					ExtraRules: []networkingv1.NetworkPolicyEgressRule{tc.rule},
				}
			})
			refusals := egressAllowlistRefusals(agent)
			policy, dropped := buildAgentEgressNetworkPolicy(agent, nil, managedOTelCollectorNamespace)
			rendered := false
			for _, rule := range policy.Spec.Egress {
				for _, peer := range rule.To {
					if peer.IPBlock != nil && peer.IPBlock.CIDR == "140.82.112.0/20" {
						rendered = true
					}
					if peer.PodSelector != nil && peer.PodSelector.MatchLabels["app"] == "example" {
						rendered = true
					}
				}
			}
			if tc.refused {
				if len(refusals) == 0 {
					t.Error("a rule the API server would reject must be refused at validation, not passed to the apply")
				}
				if len(dropped) == 0 {
					t.Error("the builder must drop the rule too; validation and the builder are separate layers on purpose")
				}
				if rendered {
					t.Error("the refused rule was rendered anyway")
				}
				return
			}
			if len(refusals) != 0 {
				t.Errorf("a valid except was refused; the screen must refuse only what the API server refuses: %v", refusals)
			}
			if !rendered {
				t.Error("the valid rule was not rendered")
			}
		})
	}

	// The whole point is that the reconcile parks rather than wedges: run one
	// rejected shape through a full Reconcile and require no error and a
	// Degraded status naming the rule.
	scheme := setupScheme()
	agent := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Security.EgressAllowlist = &agentv1alpha1.EgressAllowlistSpec{
			ExtraRules: []networkingv1.NetworkPolicyEgressRule{{
				To: []networkingv1.NetworkPolicyPeer{{
					IPBlock: &networkingv1.IPBlock{CIDR: "140.82.112.0/20", Except: []string{"10.0.0.0/8"}},
				}},
			}},
		}
	})
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(ssaApplyInterceptor()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("a rejectable extraRules entry must park the CR Degraded, not error the reconcile: %v", err)
	}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatalf("failed to re-read the agent: %v", err)
	}
	if stored.Status.Phase != "Degraded" {
		t.Errorf("expected Degraded, got %q", stored.Status.Phase)
	}
	var reason, message string
	for _, condition := range stored.Status.Conditions {
		if condition.Type == "Ready" {
			reason, message = condition.Reason, condition.Message
		}
	}
	if reason != "EgressAllowlistRefused" || !strings.Contains(message, "extraRules[0]") {
		t.Errorf("the refusal must name the entry: reason=%q message=%q", reason, message)
	}
}

// TestARefusalDoesNotSuspendTheGuardrail is the regression test for the hole
// the previous round's fix opened. Refusing the spec returns before the step
// that reconciles the NetworkPolicy, so a bad extraRules entry on an
// already-running agent would leave the guardrail unmaintained: delete it and
// nothing puts it back, while the Degraded status reads like the control is
// merely misconfigured rather than gone.
//
// Refusing a value and withholding the whole control are different things. The
// builder has already dropped the offending destination, so there is a good
// policy to render.
func TestARefusalDoesNotSuspendTheGuardrail(t *testing.T) {
	scheme := setupScheme()
	agent := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Security.EgressAllowlist = &agentv1alpha1.EgressAllowlistSpec{
			ExtraRules: []networkingv1.NetworkPolicyEgressRule{{
				To: []networkingv1.NetworkPolicyPeer{{IPBlock: &networkingv1.IPBlock{CIDR: "0.0.0.0/0"}}},
			}},
		}
	})
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(ssaApplyInterceptor()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile failed: %v", err)
	}
	key := types.NamespacedName{Name: agentEgressPolicyName(agent), Namespace: agent.Namespace}
	rendered := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, key, rendered); err != nil {
		t.Fatalf("a refused allowlist entry withheld the whole guardrail; the refusal is about one "+
			"destination, and the policy without it is still the control: %v", err)
	}
	assertClosed(t, rendered, agent, "rendered-under-refusal")

	// And it must keep being maintained, not merely have been written once.
	if err := cl.Delete(ctx, rendered); err != nil {
		t.Fatalf("failed to delete the policy for the restore check: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("second Reconcile failed: %v", err)
	}
	restored := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, key, restored); err != nil {
		t.Fatalf("while the spec was refused the guardrail stopped being reconciled, so deleting it "+
			"stuck; the agent would run unprotected behind a Degraded status: %v", err)
	}
	assertClosed(t, restored, agent, "restored-under-refusal")

	// The refusal itself must survive all of that.
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatalf("failed to re-read the agent: %v", err)
	}
	if stored.Status.Phase != "Degraded" {
		t.Errorf("rendering the policy anyway must not clear the refusal, got phase %q", stored.Status.Phase)
	}
}

// TestExtraRulesCannotReopenTheMetadataServer is the escape hatch's own guard.
// The allowlist under-allows by design, so operators will reach for extraRules;
// the hatch is only acceptable if it cannot be widened onto the thing the
// policy exists to close.
func TestExtraRulesCannotReopenTheMetadataServer(t *testing.T) {
	cidrPeer := func(cidr string, except ...string) networkingv1.NetworkPolicyEgressRule {
		return networkingv1.NetworkPolicyEgressRule{
			To: []networkingv1.NetworkPolicyPeer{{IPBlock: &networkingv1.IPBlock{CIDR: cidr, Except: except}}},
		}
	}

	cases := []struct {
		name string
		rule networkingv1.NetworkPolicyEgressRule
		kept bool
	}{
		{name: "no peers at all permits every destination", rule: networkingv1.NetworkPolicyEgressRule{}},
		{name: "the whole IPv4 internet", rule: cidrPeer("0.0.0.0/0")},
		{
			// The form fb99cd1 used. It is refused rather than accepted
			// because NAT PREROUTING rewrites the destination before an
			// iptables dataplane evaluates the rule, so the excepted address
			// is not the address that gets matched.
			name: "an except clause naming the metadata server does not rescue it",
			rule: cidrPeer("0.0.0.0/0", "169.254.169.254/32"),
		},
		{name: "the link-local range", rule: cidrPeer("169.254.0.0/16")},
		{name: "the DNAT target alone", rule: cidrPeer("169.254.169.252/32")},
		{name: "the whole IPv6 internet", rule: cidrPeer("::/0")},
		{name: "the IPv6 metadata prefix", rule: cidrPeer("fd20:ce::/64")},
		{name: "an unparseable CIDR fails closed", rule: cidrPeer("not-a-cidr")},

		// The parser differential. netip sees inert IPv6 here; net.ParseCIDR,
		// which the API server and the CNI use, sees 0.0.0.0/0 and
		// 169.254.169.254/32. The second is the metadata server itself, coming
		// back in through the escape hatch built to keep it out.
		{name: "the whole internet in IPv4-mapped form", rule: cidrPeer("::ffff:0.0.0.0/96")},
		{name: "the metadata server in IPv4-mapped form", rule: cidrPeer("::ffff:169.254.169.254/128")},
		{name: "the DNAT target in IPv4-mapped form", rule: cidrPeer("::ffff:169.254.169.252/128")},
		{name: "an IPv6 prefix wide enough to reach the mapped range", rule: cidrPeer("::/1")},

		{name: "a specific external range is kept", rule: cidrPeer("140.82.112.0/20"), kept: true},
		{
			name: "a Pod selector cannot reach the metadata server and is kept",
			rule: networkingv1.NetworkPolicyEgressRule{
				To: []networkingv1.NetworkPolicyPeer{namespacedPodPeer("other-ns", map[string]string{"app": "thing"})},
			},
			kept: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			agent := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
				a.Spec.Security.EgressAllowlist = &agentv1alpha1.EgressAllowlistSpec{
					ExtraRules: []networkingv1.NetworkPolicyEgressRule{tc.rule},
				}
			})
			policy, dropped := buildAgentEgressNetworkPolicy(agent, nil, managedOTelCollectorNamespace)

			if tc.kept {
				if len(dropped) != 0 {
					t.Fatalf("a legitimate rule was dropped: %v", dropped)
				}
				return
			}
			if len(dropped) != 1 {
				t.Fatalf("expected the rule to be refused, dropped=%v", dropped)
			}
			// Dropped, not narrowed: the rendered policy must not carry it.
			// permitsBeyondDNS, because the DNS rule names the resolver at
			// 169.254.169.254:53 on purpose and a port-blind check here would
			// report every case as a re-opening.
			for _, address := range metadataServerAddresses {
				if permitsBeyondDNS(policy, address) {
					t.Errorf("extraRules re-permitted the metadata server at %s", address)
				}
			}
			if permits(policy, "8.8.8.8") && tc.rule.To != nil {
				t.Error("the refused rule still widened the policy")
			}
		})
	}
}

// TestReconcileRendersAndRestoresTheEgressPolicy is the continuous check the
// plan asked for. A NetworkPolicy that was verified once is how this control
// got deleted in the first place, so the assertion is not "it was created" but
// "deleting it does not stick".
func TestReconcileRendersAndRestoresTheEgressPolicy(t *testing.T) {
	scheme := setupScheme()
	agent := egressPolicyAgent()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(ssaApplyInterceptor()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile failed: %v", err)
	}
	key := types.NamespacedName{Name: agentEgressPolicyName(agent), Namespace: agent.Namespace}
	rendered := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, key, rendered); err != nil {
		t.Fatalf("Reconcile did not render the agent egress policy: %v", err)
	}
	for _, address := range metadataServerAddresses {
		if permitsBeyondDNS(rendered, address) {
			t.Errorf("the policy Reconcile wrote to the cluster permits the metadata server at %s "+
				"on a port beyond 53", address)
		}
	}

	// An operator, or a compromised agent with RBAC on NetworkPolicies, deletes
	// the guardrail. The next reconcile must put it back.
	if err := cl.Delete(ctx, rendered); err != nil {
		t.Fatalf("failed to delete the policy for the restore check: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("second Reconcile failed: %v", err)
	}
	restored := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, key, restored); err != nil {
		t.Fatalf("Reconcile did not restore the deleted egress policy; the control is one-time, not continuous: %v", err)
	}
	// Existence is not the property. A policy that came back permissive would
	// satisfy a Get and protect nothing, so the restored object is checked
	// against the same assertions the freshly rendered one gets.
	assertClosed(t, restored, agent, "restored")
}

// TestReconcileRevertsAPermissiveEditToTheEgressPolicy is the mutation half of
// "continuous". Deletion is the loud attack; the quiet one is patching the live
// object to add a peer, which leaves a policy of the right name in place for
// anyone who only checks that it exists.
//
// applyManaged server-side-applies with ForceOwnership, and the egress list is
// atomic, so the operator owns the whole list and rewrites it. The fake
// client's apply interceptor models that as a full replacement, which is the
// same outcome for this property but not the same mechanism — a real cluster
// would resolve it through field ownership. Worth knowing when reading a
// failure here.
func TestReconcileRevertsAPermissiveEditToTheEgressPolicy(t *testing.T) {
	scheme := setupScheme()
	agent := egressPolicyAgent()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(ssaApplyInterceptor()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile failed: %v", err)
	}
	key := types.NamespacedName{Name: agentEgressPolicyName(agent), Namespace: agent.Namespace}
	live := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, key, live); err != nil {
		t.Fatalf("Reconcile did not render the agent egress policy: %v", err)
	}

	// A third party widens the live object: one extra rule, everything else
	// untouched, the name and labels intact.
	live.Spec.Egress = append(live.Spec.Egress, networkingv1.NetworkPolicyEgressRule{
		To: []networkingv1.NetworkPolicyPeer{{IPBlock: &networkingv1.IPBlock{CIDR: "0.0.0.0/0"}}},
	})
	if err := cl.Update(ctx, live); err != nil {
		t.Fatalf("failed to patch the policy for the revert check: %v", err)
	}
	if !permits(live, "169.254.169.254") {
		t.Fatal("the test's own mutation did not open the policy; the check below would prove nothing")
	}

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("second Reconcile failed: %v", err)
	}
	reverted := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, key, reverted); err != nil {
		t.Fatalf("the policy vanished after the revert reconcile: %v", err)
	}
	assertClosed(t, reverted, agent, "reverted")
}

// assertClosed re-runs the metadata and default-deny properties over a policy
// read back from the cluster, so "the controller put something there" is never
// mistaken for "the controller put the right thing there".
func assertClosed(t *testing.T, policy *networkingv1.NetworkPolicy, agent *agentv1alpha1.PlatformAgent, stage string) {
	t.Helper()
	for _, address := range metadataServerAddresses {
		if permitsBeyondDNS(policy, address) {
			t.Errorf("the %s policy permits the metadata server at %s on a port beyond 53", stage, address)
		}
	}
	if !permits(policy, metadataLinkLocalIP) {
		t.Errorf("the %s policy lost the Cloud DNS resolver at %s, which is a total outage on a "+
			"cluster that does not run kube-dns", stage, metadataLinkLocalIP)
	}
	if permits(policy, "8.8.8.8") {
		t.Errorf("the %s policy permits an arbitrary internet address; it is not default-deny", stage)
	}
	if !allowsPeerOnPort(policy, "kube-system", map[string]string{"k8s-app": "kube-dns"}, 53) {
		t.Errorf("the %s policy lost DNS, which makes it a total egress block rather than an allowlist", stage)
	}
	broker := map[string]string{"app": credentialBrokerName(agent)}
	if !allowsPeerOnPort(policy, agent.Namespace, broker, credentialProxyPort) {
		t.Errorf("the %s policy lost the credential broker, which is every credentialed command", stage)
	}
}

// TestReconcileRendersNoPolicyWhenNotAskedFor guards the default. The two
// existing golden fixtures cover the rendered manifests; this covers the
// cluster-side effect, which they do not see.
func TestReconcileRendersNoPolicyWhenNotAskedFor(t *testing.T) {
	scheme := setupScheme()
	agent := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Security.EgressPolicy = ""
	})
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(ssaApplyInterceptor()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile failed: %v", err)
	}
	err := cl.Get(ctx, types.NamespacedName{Name: agentEgressPolicyName(agent), Namespace: agent.Namespace},
		&networkingv1.NetworkPolicy{})
	if err == nil {
		t.Error("a policy was rendered for an agent that did not ask for one")
	}
}

// TestARefusalDoesNotSuspendTheGatewayNetworkPolicy covers the sibling of the
// hazard refusalStillRendersTheGuardrail was written for.
//
// The agent Pod has two NetworkPolicies: <name>-sandbox-metadata-deny, which
// this change adds, and <name>-gateway-netpol, which the operator has always
// rendered. Refusing the egress spec returns before the step that reconciles
// the second one, so an operator triaging the refusal could delete both and
// get neither back. That is not a Pod left restricted — with nothing selecting
// it, NetworkPolicy permits all egress — so the failure is wide-open egress
// behind a Degraded status that names one bad CIDR.
//
// Checked under both refusal reasons. The gateway policy is not what either
// refusal objects to, so both must keep it maintained.
func TestARefusalDoesNotSuspendTheGatewayNetworkPolicy(t *testing.T) {
	for _, tc := range []struct {
		name    string
		mutate  func(*agentv1alpha1.PlatformAgent)
		reason  string
		guarded bool // whether the egress guardrail is rendered too
	}{
		{
			name:    "EgressAllowlistRefused",
			reason:  reasonEgressAllowlistRefused,
			guarded: true,
			mutate: func(a *agentv1alpha1.PlatformAgent) {
				a.Spec.Security.EgressAllowlist = &agentv1alpha1.EgressAllowlistSpec{
					ControlPlaneCIDRs: []string{"0.0.0.0/0"},
				}
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			agent := egressPolicyAgent(tc.mutate)
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(agent).
				WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
				WithInterceptorFuncs(ssaApplyInterceptor()).
				Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
			req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
			ctx := context.Background()

			if _, err := r.Reconcile(ctx, req); err != nil {
				t.Fatalf("Reconcile failed: %v", err)
			}

			// The refusal is what makes this test mean anything: without it the
			// gateway policy would be reconciled by the ordinary path below.
			stored := &agentv1alpha1.PlatformAgent{}
			if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
				t.Fatalf("failed to re-read the agent: %v", err)
			}
			var gotReason string
			for _, condition := range stored.Status.Conditions {
				if condition.Type == "Ready" {
					gotReason = condition.Reason
				}
			}
			if gotReason != tc.reason {
				t.Fatalf("the spec was not refused, so this test proves nothing; got reason %q", gotReason)
			}

			gateway := types.NamespacedName{Name: agent.Name + "-gateway-netpol", Namespace: agent.Namespace}
			if err := cl.Get(ctx, gateway, &networkingv1.NetworkPolicy{}); err != nil {
				t.Fatalf("the refusal withheld the agent Pod's gateway NetworkPolicy: %v", err)
			}

			// And it must keep being maintained, not merely have been written
			// once before the spec went bad.
			if err := cl.Delete(ctx, &networkingv1.NetworkPolicy{
				ObjectMeta: metav1.ObjectMeta{Name: gateway.Name, Namespace: gateway.Namespace},
			}); err != nil {
				t.Fatalf("failed to delete the gateway policy for the restore check: %v", err)
			}
			if _, err := r.Reconcile(ctx, req); err != nil {
				t.Fatalf("second Reconcile failed: %v", err)
			}
			if err := cl.Get(ctx, gateway, &networkingv1.NetworkPolicy{}); err != nil {
				t.Fatalf("while the spec was refused the gateway NetworkPolicy stopped being reconciled, so "+
					"deleting it stuck; with nothing selecting the agent Pod its egress is unrestricted: %v", err)
			}

			egress := types.NamespacedName{Name: agentEgressPolicyName(agent), Namespace: agent.Namespace}
			err := cl.Get(ctx, egress, &networkingv1.NetworkPolicy{})
			if tc.guarded && err != nil {
				t.Errorf("the egress guardrail was withheld by a refusal about one destination: %v", err)
			}
			if !tc.guarded && err == nil {
				t.Error("the split-broker refusal rendered the egress policy, which is the outage it exists to prevent")
			}
		})
	}
}

// TestTheFlagAddedToARunningAgentRendersTheGuardrail covers the case the
// reference page describes in prose and no other test reaches: the field is
// set on an agent that is already running, rather than being present when the
// agent is first created. The guardrail has to appear against a workload that
// already exists.
func TestTheFlagAddedToARunningAgentRendersTheGuardrail(t *testing.T) {
	scheme := setupScheme()
	// The agent starts without the field, which is how every existing install
	// starts.
	agent := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Security.EgressPolicy = ""
	})
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(ssaApplyInterceptor()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("first Reconcile failed: %v", err)
	}
	workload := types.NamespacedName{Name: agent.Name + "-gateway", Namespace: agent.Namespace}
	if err := cl.Get(ctx, workload, &appsv1.Deployment{}); err != nil {
		t.Fatalf("the agent was not running before the field was set, so this test proves nothing: %v", err)
	}
	egress := types.NamespacedName{Name: agentEgressPolicyName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, egress, &networkingv1.NetworkPolicy{}); err == nil {
		t.Fatal("a policy existed before the field was set")
	}

	// Now an operator sets the field on the running agent.
	live := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), live); err != nil {
		t.Fatalf("failed to re-read the agent: %v", err)
	}
	live.Spec.Security.EgressPolicy = egressPolicyAllowlist
	if err := cl.Update(ctx, live); err != nil {
		t.Fatalf("failed to set the field: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("second Reconcile failed: %v", err)
	}

	rendered := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, egress, rendered); err != nil {
		t.Fatalf("setting the field on a running agent did not render the guardrail: %v", err)
	}
	assertClosed(t, rendered, live, "added-to-running-agent")
}

// TestTheDNSRuleCarriesTheResolvedClusterIP pins the peer the review found
// missing. On a dataplane that matches the Service VIP rather than the
// backing Pods, the two selector peers never fire, so a rendered policy
// without the resolved ClusterIP is a total egress block in the one shape
// where this policy stands alone — and an operator's dnsClusterIPs override
// applied to only one of the Pod's two policies is silently ignored by the
// other.
func TestTheDNSRuleCarriesTheResolvedClusterIP(t *testing.T) {
	resolved, _ := buildAgentEgressNetworkPolicy(egressPolicyAgent(), []string{"34.118.224.10"}, managedOTelCollectorNamespace)
	if !permits(resolved, "34.118.224.10") {
		t.Error("the resolved DNS ClusterIP is not on the rendered policy; on a VIP-matching dataplane " +
			"every named destination becomes unreachable with it absent")
	}

	fallback, _ := buildAgentEgressNetworkPolicy(egressPolicyAgent(), nil, managedOTelCollectorNamespace)
	if !permits(fallback, defaultDNSClusterIP) {
		t.Errorf("with no resolved IPs the rule must fall back to the documented default %s, as the "+
			"gateway policy does", defaultDNSClusterIP)
	}

	// The annotation rung of the resolution ladder is operator input, so a
	// metadata address arriving as a "DNS ClusterIP" must not buy any reach the
	// rule does not already grant. 169.254.169.254:53 it does grant, so naming
	// it is a no-op rather than an escalation; what must still not appear on a
	// credential port is any of the three. The fallback then applies, because
	// the filter leaves the resolved set empty: a policy whose DNS rule names no
	// address at all is the total block this rule exists to prevent.
	poisoned, _ := buildAgentEgressNetworkPolicy(egressPolicyAgent(), []string{"169.254.169.254"}, managedOTelCollectorNamespace)
	for _, address := range metadataServerAddresses {
		if permitsBeyondDNS(poisoned, address) {
			t.Errorf("a metadata address supplied as a DNS ClusterIP reached a credential port: %s", address)
		}
	}
	if !permits(poisoned, defaultDNSClusterIP) {
		t.Error("dropping a poisoned DNS IP must fall back to the default, not render a rule with no address")
	}

	// The credential-only metadata listeners are not resolvers and must not be
	// rendered as DNS peers even though 169.254.169.254 now is. The daemon
	// address answers the token API on 988 and no DNS at all, so a rule naming
	// it on 53 grants reach for nothing.
	for _, address := range []string{metadataDaemonIP, "fd20:ce::254"} {
		daemonAsDNS, _ := buildAgentEgressNetworkPolicy(egressPolicyAgent(), []string{address}, managedOTelCollectorNamespace)
		if permits(daemonAsDNS, address) {
			t.Errorf("%s was rendered as a DNS peer; only the resolver address belongs on the DNS rule", address)
		}
	}

	// The whole path: a spec-level dnsClusterIPs override must reach the
	// rendered egress policy through a real Reconcile, not only the gateway
	// policy.
	scheme := setupScheme()
	agent := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.NetworkPolicy = &agentv1alpha1.NetworkPolicySpec{DNSClusterIPs: []string{"34.118.230.7"}}
	})
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(ssaApplyInterceptor()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile failed: %v", err)
	}
	rendered := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, types.NamespacedName{Name: agentEgressPolicyName(agent), Namespace: agent.Namespace}, rendered); err != nil {
		t.Fatalf("Reconcile did not render the egress policy: %v", err)
	}
	if !permits(rendered, "34.118.230.7") {
		t.Error("spec.networkPolicy.dnsClusterIPs reached the gateway policy but not the egress policy; " +
			"the override must apply to both policies over the same Pod")
	}
}

// TestTheDNSRuleDoesNotRepeatAPeerItAlreadyCarries guards the same duplication
// the gateway policy's DNS rule guards against, on the builder next door. The
// fixed peers here include nodeLocalDNSCacheIP, and a cluster running NodeLocal
// DNSCache puts that address in kubelet's --cluster-dns, so an operator naming
// the value their nodes use arrives at a peer the rule already has.
func TestTheDNSRuleDoesNotRepeatAPeerItAlreadyCarries(t *testing.T) {
	bareNodeLocalIP := strings.TrimSuffix(nodeLocalDNSCacheIP, "/32")
	policy, _ := buildAgentEgressNetworkPolicy(egressPolicyAgent(), []string{bareNodeLocalIP}, managedOTelCollectorNamespace)

	occurrences := 0
	for _, rule := range policy.Spec.Egress {
		if !ruleIsDNSOnly(rule) {
			continue
		}
		for _, peer := range rule.To {
			if peer.IPBlock != nil && peer.IPBlock.CIDR == nodeLocalDNSCacheIP {
				occurrences++
			}
		}
	}
	if occurrences != 1 {
		t.Errorf("the DNS rule names %s %d times, want 1; a duplicate ipBlock is no wider but leaves a "+
			"reader of an auditable policy guessing which peer is doing the work", nodeLocalDNSCacheIP, occurrences)
	}
}

// TestABareControlPlaneAddressIsWidenedNotRefused covers the paste the field
// description itself produces: the documented gcloud command emits a bare
// address for a cluster with a public endpoint, and refusing it parked the CR
// Degraded for following the docs. The widening must not weaken the guards —
// a bare metadata address is still refused.
func TestABareControlPlaneAddressIsWidenedNotRefused(t *testing.T) {
	agent := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Security.EgressAllowlist = &agentv1alpha1.EgressAllowlistSpec{
			ControlPlaneCIDRs: []string{"34.1.2.3"},
		}
	})
	if refusals := egressAllowlistRefusals(agent); len(refusals) != 0 {
		t.Fatalf("a bare control-plane address must be widened to /32, not refused: %v", refusals)
	}
	policy, dropped := buildAgentEgressNetworkPolicy(agent, nil, managedOTelCollectorNamespace)
	if len(dropped) != 0 {
		t.Fatalf("the builder dropped the widened address: %v", dropped)
	}
	found := false
	for _, rule := range policy.Spec.Egress {
		for _, peer := range rule.To {
			if peer.IPBlock != nil && peer.IPBlock.CIDR == "34.1.2.3/32" {
				found = true
			}
		}
	}
	if !found {
		t.Error("the bare address must render as its /32 — the API server rejects a bare address in an ipBlock")
	}

	poisoned := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Security.EgressAllowlist = &agentv1alpha1.EgressAllowlistSpec{
			ControlPlaneCIDRs: []string{"169.254.169.254"},
		}
	})
	if refusals := egressAllowlistRefusals(poisoned); len(refusals) == 0 {
		t.Error("a bare metadata address must still be refused after widening")
	}
}

// TestTheDNSLadderRunsEvenWithTheGatewayPolicyDisabled is the re-review's
// sharpest point turned into a test. spec.networkPolicy.enabled: false makes
// resolveNetpolProfile return before the DNS ladder runs, and that flag
// creates the only shape where this policy stands alone and enforces — so a
// nil ladder result there meant the hard-coded default VIP on exactly the
// install where a wrong VIP is a total egress block, and the documented
// dnsClusterIPs override was unreachable. The egress policy's DNS resolution
// must not be gated by the flag that withholds the other policy.
func TestTheDNSLadderRunsEvenWithTheGatewayPolicyDisabled(t *testing.T) {
	scheme := setupScheme()
	agent := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.NetworkPolicy = &agentv1alpha1.NetworkPolicySpec{
			Enabled:       ptr.To(false),
			DNSClusterIPs: []string{"34.118.230.7"},
		}
	})
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(ssaApplyInterceptor()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile failed: %v", err)
	}

	// The shape is real: no gateway policy to union with.
	gateway := types.NamespacedName{Name: agent.Name + "-gateway-netpol", Namespace: agent.Namespace}
	if err := cl.Get(ctx, gateway, &networkingv1.NetworkPolicy{}); err == nil {
		t.Fatal("networkPolicy.enabled: false must withhold the gateway policy, or this test proves nothing")
	}

	rendered := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, types.NamespacedName{Name: agentEgressPolicyName(agent), Namespace: agent.Namespace}, rendered); err != nil {
		t.Fatalf("the egress policy must still render in this shape: %v", err)
	}
	if !permits(rendered, "34.118.230.7") {
		t.Error("the dnsClusterIPs override did not reach the egress policy's DNS rule in the one shape " +
			"where that rule is the Pod's only route to DNS")
	}
}

// TestTheOTelRuleNamesTheResolvedCollectorNamespace covers #1080. The gateway
// policy reads the collector namespace off the resolved OTLP endpoint; this
// policy used to name gke-managed-otel whatever the endpoint was, so on an
// install with a discovered or configured collector elsewhere it permitted a
// namespace the agent never sent to and, once enforcing, blocked the export
// it did. The two policies select the same Pod and must not disagree.
func TestTheOTelRuleNamesTheResolvedCollectorNamespace(t *testing.T) {
	for _, tc := range []struct {
		name      string
		namespace string // what the caller read off the endpoint
		want      string // namespace the rule must name; "" means no rule
	}{
		{name: "managed collector", namespace: managedOTelCollectorNamespace, want: managedOTelCollectorNamespace},
		{name: "collector elsewhere", namespace: "observability", want: "observability"},
		{name: "no in-cluster namespace", namespace: "", want: ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			policy, _ := buildAgentEgressNetworkPolicy(egressPolicyAgent(), nil, tc.namespace)
			if tc.want == "" {
				for _, rule := range policy.Spec.Egress {
					for _, peer := range rule.To {
						if peer.NamespaceSelector != nil && rulePermitsOTLPPort(rule) {
							t.Errorf("an endpoint with no in-cluster namespace still rendered an OTel rule, to %v", peer.NamespaceSelector.MatchLabels)
						}
					}
				}
				return
			}
			if !hasCollectorEgress(policy, tc.want) {
				t.Fatalf("the OTel rule does not name %q, so the export to the resolved collector is blocked once the policy enforces", tc.want)
			}
			if tc.want != managedOTelCollectorNamespace && hasCollectorEgress(policy, managedOTelCollectorNamespace) {
				t.Errorf("the OTel rule still names %q alongside the resolved %q, which permits traffic the agent never sends", managedOTelCollectorNamespace, tc.want)
			}
		})
	}
}

// rulePermitsOTLPPort reports whether rule names either OTLP receiver port.
func rulePermitsOTLPPort(rule networkingv1.NetworkPolicyEgressRule) bool {
	for _, port := range rule.Ports {
		if port.Port != nil && (port.Port.IntValue() == 4317 || port.Port.IntValue() == 4318) {
			return true
		}
	}
	return false
}

// TestTheReconciledOTelRuleFollowsTheResolvedEndpoint is the reconcile-level
// half: the namespace has to arrive from resolveOTLPEndpoint, on the normal
// path and on the refusal path that reconcileAgentNetworkGuardrails keeps
// maintained, or the builder test above proves only that the plumbing could
// carry it.
func TestTheReconciledOTelRuleFollowsTheResolvedEndpoint(t *testing.T) {
	const endpoint = "http://otel-collector.observability.svc.cluster.local:4318"
	pointAtObservability := func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Telemetry = &agentv1alpha1.TelemetrySpec{OTLPEndpoint: endpoint}
	}
	for _, tc := range []struct {
		name   string
		mutate func(*agentv1alpha1.PlatformAgent)
		reason string // "" for the normal path
	}{
		{name: "normal path", mutate: pointAtObservability},
		{
			name:   "refusal path",
			reason: reasonEgressAllowlistRefused,
			mutate: func(a *agentv1alpha1.PlatformAgent) {
				pointAtObservability(a)
				a.Spec.Security.EgressAllowlist = &agentv1alpha1.EgressAllowlistSpec{
					ControlPlaneCIDRs: []string{"0.0.0.0/0"},
				}
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			agent := egressPolicyAgent(tc.mutate)
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(agent).
				WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
				WithInterceptorFuncs(ssaApplyInterceptor()).
				Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
			req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
			ctx := context.Background()

			if _, err := r.Reconcile(ctx, req); err != nil {
				t.Fatalf("Reconcile failed: %v", err)
			}

			if tc.reason != "" {
				stored := &agentv1alpha1.PlatformAgent{}
				if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
					t.Fatalf("failed to re-read the agent: %v", err)
				}
				var gotReason string
				for _, condition := range stored.Status.Conditions {
					if condition.Type == "Ready" {
						gotReason = condition.Reason
					}
				}
				if gotReason != tc.reason {
					t.Fatalf("the spec was not refused, so this case proves nothing about the refusal path; got reason %q", gotReason)
				}
			}

			egress := &networkingv1.NetworkPolicy{}
			if err := cl.Get(ctx, types.NamespacedName{Name: agentEgressPolicyName(agent), Namespace: agent.Namespace}, egress); err != nil {
				t.Fatalf("the egress policy was not rendered: %v", err)
			}
			if !hasCollectorEgress(egress, "observability") {
				t.Errorf("spec.telemetry.otlpEndpoint names a collector in observability but the egress policy's OTel rule does not, so the export is blocked once the policy enforces")
			}
			if hasCollectorEgress(egress, managedOTelCollectorNamespace) {
				t.Errorf("the egress policy still permits %s while the agent exports to observability", managedOTelCollectorNamespace)
			}

			// The gateway policy is the reference: whatever it names, this one must too.
			gateway := &networkingv1.NetworkPolicy{}
			if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name + "-gateway-netpol", Namespace: agent.Namespace}, gateway); err != nil {
				t.Fatalf("the gateway policy was not rendered: %v", err)
			}
			if hasCollectorEgress(gateway, "observability") != hasCollectorEgress(egress, "observability") {
				t.Errorf("the two policies selecting the agent Pod disagree about the collector namespace")
			}
		})
	}
}
