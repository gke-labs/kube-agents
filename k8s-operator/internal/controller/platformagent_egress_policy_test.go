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
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// egressPolicyAgent is an agent with the split broker and the allowlist both on
// — the only configuration in which the policy renders.
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
				ProjectID:   "proj",
				Location:    "us-central1",
				ClusterName: "cluster",
			},
			AgentSpec: agentv1alpha1.AgentSpec{
				Security: &agentv1alpha1.SecuritySpec{
					SplitCredentialBrokerPod: ptr.To(true),
					EgressPolicy:             egressPolicyAllowlist,
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
			if peer.PodSelector == nil || peer.NamespaceSelector == nil {
				continue
			}
			nsSelector, err := metav1.LabelSelectorAsSelector(peer.NamespaceSelector)
			if err != nil || !nsSelector.Matches(labelSet(map[string]string{"kubernetes.io/metadata.name": namespace})) {
				continue
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
func TestTheRenderedPolicyDeniesEveryMetadataAddress(t *testing.T) {
	policy, _ := buildAgentEgressNetworkPolicy(egressPolicyAgent())

	for _, address := range metadataServerAddresses {
		if permits(policy, address) {
			t.Errorf("the rendered egress policy permits the metadata server at %s; "+
				"anything that can make an HTTP request there can mint the Workload Identity token "+
				"and bypass the credential broker entirely (invariant C1)", address)
		}
	}
}

// TestTheRenderedPolicyIsDefaultDeny pins the two spec-level properties that
// make the deny work at all. Without Egress in policyTypes the object selects
// the Pod and restricts nothing; with a rule that has no peers, everything is
// permitted regardless of the other rules.
func TestTheRenderedPolicyIsDefaultDeny(t *testing.T) {
	agent := egressPolicyAgent()
	policy, _ := buildAgentEgressNetworkPolicy(agent)

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
	policy, _ := buildAgentEgressNetworkPolicy(agent)

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
	policy, _ := buildAgentEgressNetworkPolicy(agent)

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
			name: "litellm", ns: agent.Namespace, labels: map[string]string{"app": "litellm"}, port: 4000,
			why: "buildAgentConfig pins model base_url to http://litellm.<ns>.svc.cluster.local/v1 unconditionally",
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
// inventing a range. Omitting it costs the event-watcher its connection; that
// cost is documented on the CRD field and must not be quietly paid off with a
// guess.
func TestTheControlPlaneRuleIsAbsentUntilAskedFor(t *testing.T) {
	policy, _ := buildAgentEgressNetworkPolicy(egressPolicyAgent())
	if permits(policy, "172.16.0.2") {
		t.Error("a control-plane range was rendered without egressAllowlist.controlPlaneCIDRs asking for one")
	}

	configured, _ := buildAgentEgressNetworkPolicy(egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Security.EgressAllowlist = &agentv1alpha1.EgressAllowlistSpec{
			ControlPlaneCIDRs: []string{"172.16.0.0/28"},
		}
	}))
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
			policy, dropped := buildAgentEgressNetworkPolicy(agent)
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

// TestTheSplitBrokerRefusalStillRendersNothing is the other side of that
// distinction, and the reason it cannot be "always render anyway". There the
// objection is to the policy existing at all: it would govern the credential
// broker sharing the Pod and take away the metadata server it mints the cloud
// token from. Rendering it would be the outage the refusal exists to prevent.
func TestTheSplitBrokerRefusalStillRendersNothing(t *testing.T) {
	if refusalStillRendersTheGuardrail(reasonEgressPolicyRequiresSplitBroker) {
		t.Error("the split-broker refusal must not render the policy: it would deny the credential " +
			"broker in the same Pod the metadata server it mints the cloud token from")
	}
	if !refusalStillRendersTheGuardrail(reasonEgressAllowlistRefused) {
		t.Error("a refused allowlist value must still leave the guardrail rendered")
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
			policy, dropped := buildAgentEgressNetworkPolicy(agent)

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
			for _, address := range metadataServerAddresses {
				if permits(policy, address) {
					t.Errorf("extraRules re-permitted the metadata server at %s", address)
				}
			}
			if permits(policy, "8.8.8.8") && tc.rule.To != nil {
				t.Error("the refused rule still widened the policy")
			}
		})
	}
}

// TestTheEgressPolicyIsRefusedWithoutTheSplitBroker makes the conditionality
// visible at the unit level. The paired Reconcile test below proves the
// refusal is wired up rather than merely available.
func TestTheEgressPolicyIsRefusedWithoutTheSplitBroker(t *testing.T) {
	sidecar := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Security.SplitCredentialBrokerPod = ptr.To(false)
	})
	reason, message := validateEgressPolicy(sidecar)
	if reason != "EgressPolicyRequiresSplitBroker" {
		t.Fatalf("asking for the egress policy in the sidecar layout must be refused, got reason %q", reason)
	}
	if message == "" {
		t.Error("the refusal must say what to do about it")
	}

	if reason, _ := validateEgressPolicy(egressPolicyAgent()); reason != "" {
		t.Errorf("the split layout must be accepted, got %q", reason)
	}
	off := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Security.EgressPolicy = ""
		a.Spec.Security.SplitCredentialBrokerPod = ptr.To(false)
	})
	if reason, _ := validateEgressPolicy(off); reason != "" {
		t.Errorf("an agent that asked for no egress policy must reconcile normally, got %q", reason)
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
		if permits(rendered, address) {
			t.Errorf("the policy Reconcile wrote to the cluster permits the metadata server at %s", address)
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
		if permits(policy, address) {
			t.Errorf("the %s policy permits the metadata server at %s", stage, address)
		}
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

// TestReconcileRefusesTheEgressPolicyInTheSidecarLayout is the conditionality
// assertion, end to end. With the split gate off — which is the default — the
// operator must render no policy, must say why, and must not proceed to a
// running agent that silently lacks the control the spec asked for.
func TestReconcileRefusesTheEgressPolicyInTheSidecarLayout(t *testing.T) {
	scheme := setupScheme()
	agent := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Security.SplitCredentialBrokerPod = nil
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

	policy := &networkingv1.NetworkPolicy{}
	err := cl.Get(ctx, types.NamespacedName{Name: agentEgressPolicyName(agent), Namespace: agent.Namespace}, policy)
	if err == nil {
		t.Error("a NetworkPolicy was rendered in the sidecar layout. It would deny the credential broker " +
			"the metadata server it mints the cloud token from, because a policy selects Pods and not containers")
	}

	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatalf("failed to re-read the agent: %v", err)
	}
	if stored.Status.Phase != "Degraded" {
		t.Errorf("the refusal must be visible in status, got phase %q", stored.Status.Phase)
	}
	var reason string
	for _, condition := range stored.Status.Conditions {
		if condition.Type == "Ready" {
			reason = condition.Reason
		}
	}
	if reason != "EgressPolicyRequiresSplitBroker" {
		t.Errorf("the Ready condition must name why the policy was refused, got reason %q", reason)
	}

	deployment := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name + "-gateway", Namespace: agent.Namespace}, deployment); err == nil {
		t.Error("the agent workload was reconciled anyway. An operator who asked for the metadata server " +
			"to be denied must not get a running agent that silently can still reach it")
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
