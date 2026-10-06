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
	"reflect"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	"k8s.io/apimachinery/pkg/api/errors"
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

// The verifier's grants are keyed on a ServiceAccount, and grants keyed on an
// account nothing presents a token for authorize nobody.
//
// TestEveryCalloutPrincipalHasAClientThatCanPresentAToken asserts the list of
// callout principals and says in a comment that each has a workload behind it.
// This is the verifier's half of that promise, asserted rather than described:
// the Deployment runs as the account the identity names, projects a bus-audience
// token for it, and leaves NATS_USER unset so the binary takes the token path
// instead of the static-password one. Any of the three going wrong produces the
// same symptom on a cluster — a verifier that connects as nobody, or not at all,
// and therefore every task refused.
func TestTheVerifierDeploymentPresentsTheTokenItsGrantsAreKeyedOn(t *testing.T) {
	agent := identityTestAgent()

	var identity *a2aIdentity
	for _, id := range a2aIdentities(agent) {
		if id.user == a2aVerifierUser {
			cp := id
			identity = &cp
		}
	}
	if identity == nil {
		t.Fatal("no verifier principal is rendered; the verifier Deployment would have no identity to present")
	}

	dep := buildA2AVerifierDeployment(agent)
	sa := buildA2AVerifierServiceAccount(agent)
	pod := dep.Spec.Template.Spec

	if sa.Name != pod.ServiceAccountName {
		t.Errorf("the Deployment runs as %q but the operator creates %q", pod.ServiceAccountName, sa.Name)
	}
	if want := a2aServiceAccountName(agent.Namespace, sa.Name); identity.serviceAccount != want {
		t.Errorf("the verifier identity is keyed on %q; the Deployment presents a token for %q.\n"+
			"The callout resolves a connection by the ServiceAccount its token names, so these two disagreeing "+
			"means the verifier authenticates as an unknown principal and is refused at connect.",
			identity.serviceAccount, want)
	}
	if identity.auth != a2aAuthCallout {
		t.Error("the verifier does not authenticate by callout; the only alternative is a shared secret for the one principal that can read every capability in flight")
	}
	if identity.credsKey != "" {
		t.Errorf("the verifier carries creds key %q; a Secret read would be a capability read", identity.credsKey)
	}

	// The projected token, and the mount the binary reads it from. Both, not
	// either: a volume with no mount is invisible to the process, and a
	// mount with no volume fails the pod at admission rather than at connect.
	var projected *corev1.Volume
	for i, v := range pod.Volumes {
		if v.Name == a2aBusTokenVolume {
			projected = &pod.Volumes[i]
		}
	}
	if projected == nil {
		t.Fatalf("the verifier pod projects no %q volume; it has no bus credential at all", a2aBusTokenVolume)
	}
	if projected.Projected == nil || len(projected.Projected.Sources) != 1 ||
		projected.Projected.Sources[0].ServiceAccountToken == nil {
		t.Fatal("the verifier's bus token volume is not a single ServiceAccountToken projection")
	}
	if aud := projected.Projected.Sources[0].ServiceAccountToken.Audience; aud != a2aBusTokenAudience {
		t.Errorf("the verifier's token is minted for audience %q, want %q; the callout's validator refuses any other", aud, a2aBusTokenAudience)
	}

	container := verifierContainer(t, dep.Spec.Template.Spec.Containers)
	var mounted bool
	for _, m := range container.VolumeMounts {
		if m.Name == a2aBusTokenVolume {
			mounted = true
			if !m.ReadOnly {
				t.Error("the verifier's bus token is mounted writable")
			}
		}
	}
	if !mounted {
		t.Error("the verifier container does not mount its bus token")
	}

	// NATS_USER unset is not an omission, it is the switch. Set, the binary
	// connects with a static password and says so in a warning; unset, it
	// reads the projected token. A future edit adding it "for parity" with
	// the gateway would silently move this pod onto a shared credential.
	for _, e := range container.Env {
		if e.Name == "NATS_USER" {
			t.Errorf("the verifier Deployment sets NATS_USER=%q; that switches the binary off the projected-token path onto a static password", e.Value)
		}
	}
	if !hasEnv(container.Env, "NATS_URL") {
		t.Error("the verifier Deployment sets no NATS_URL; the binary requires it and would exit at boot")
	}

	// No second credential. The account holds no RBAC, so a default-audience
	// token on this pod would be a credential with no purpose living on the
	// one pod whose reads are the design's secret.
	if pod.AutomountServiceAccountToken == nil || *pod.AutomountServiceAccountToken {
		t.Error("the verifier pod automounts a default-audience ServiceAccount token")
	}
}

// The fence, and specifically what is missing from it. The verifier never talks
// to Kubernetes — its account holds no RBAC and its credential arrives from the
// kubelet through a volume — so egress is DNS and the bus, and the absence of a
// 443 rule is the control rather than an oversight. A test that only asserted
// the two present rules would pass after somebody added a third.
func TestTheVerifierFenceAllowsOnlyDNSAndTheBus(t *testing.T) {
	agent := identityTestAgent()
	np := buildA2AVerifierNetworkPolicy(agent, []string{"10.0.0.10"})

	if got := np.Spec.PodSelector.MatchLabels["app"]; got != a2aVerifierName(agent) {
		t.Errorf("the fence selects %q, not the verifier", got)
	}
	if len(np.Spec.Ingress) != 0 {
		t.Errorf("the verifier fence carries %d ingress rules; nothing dials the verifier, so a reachable listener here is an accident", len(np.Spec.Ingress))
	}
	var sawIngressType, sawEgressType bool
	for _, pt := range np.Spec.PolicyTypes {
		switch pt {
		case networkingv1.PolicyTypeIngress:
			sawIngressType = true
		case networkingv1.PolicyTypeEgress:
			sawEgressType = true
		}
	}
	if !sawIngressType || !sawEgressType {
		t.Errorf("policy types = %v, want both Ingress and Egress; an empty Ingress rule list only denies when the type is declared", np.Spec.PolicyTypes)
	}

	if len(np.Spec.Egress) != 2 {
		t.Fatalf("the verifier fence has %d egress rules, want exactly 2 (DNS, bus).\n"+
			"A third destination is a new way for a compromised verifier to carry what it read out of the cluster.", len(np.Spec.Egress))
	}
	// Ports and peers, together. A rule is a port set AND a peer set, and
	// neither half bounds the other: an empty To on a NetworkPolicyEgressRule
	// is not "no destinations", it is every destination, so a rule checked on
	// its ports alone is checked on half of what it grants. The count above
	// does not close that either -- dropping To from the bus rule leaves two
	// rules on the same two ports and opens TCP 4222 to every address the
	// cluster can route, in-cluster and out.
	//
	// By position, because the two rules are not interchangeable. The peer set
	// that is right for DNS (kube-system resolvers plus two host routes) is
	// wrong for the bus, so a loop asserting "each rule has some peer" would
	// pass on a bus rule pointed at kube-dns.
	for i, rule := range np.Spec.Egress {
		for _, p := range rule.Ports {
			if p.Port == nil {
				t.Errorf("egress rule %d allows every port", i)
				continue
			}
			switch int32(p.Port.IntValue()) {
			case a2aDNSPort, a2aNATSClientPort:
			default:
				t.Errorf("the verifier may egress to port %d; only DNS and the bus belong here", p.Port.IntValue())
			}
		}
		if len(rule.To) == 0 {
			t.Errorf("egress rule %d has no peer: its ports are open to every destination the cluster can route", i)
		}
	}

	// Rule 1, DNS. Port 53 to an unbounded destination is a tunnel rather than
	// name resolution, so every peer here has to be a named resolver or a host
	// route -- a /32 or /128, never a range, and never widened by an except
	// block. clusterDNSPeers is shared with the session fence, which is why
	// this reads its output rather than restating it: what is asserted is the
	// shape a peer list has to keep, not the list.
	dns := np.Spec.Egress[0]
	// Both ports named, not merely counted. The loop above accepts either
	// port on either rule, so a DNS rule carrying UDP 53 and TCP 4222 would
	// have two ports, both in the accepted set, and the right peers -- and
	// would be NATS client traffic to the cluster resolvers.
	if len(dns.Ports) != 2 {
		t.Fatalf("DNS rule ports = %+v, want udp+tcp %d", dns.Ports, a2aDNSPort)
	}
	for i, want := range []corev1.Protocol{corev1.ProtocolUDP, corev1.ProtocolTCP} {
		p := dns.Ports[i]
		if p.Protocol == nil || *p.Protocol != want || p.Port == nil || int32(p.Port.IntValue()) != a2aDNSPort {
			t.Errorf("DNS rule port %d = %+v, want %s %d", i, p, want, a2aDNSPort)
		}
	}
	if !reflect.DeepEqual(dns.To, clusterDNSPeers([]string{"10.0.0.10"})) {
		t.Errorf("the DNS rule no longer carries the cluster resolver peers: %+v", dns.To)
	}
	for _, peer := range dns.To {
		if peer.IPBlock == nil {
			if peer.PodSelector == nil || peer.NamespaceSelector == nil {
				t.Errorf("DNS peer %+v selects pods without bounding the namespace, or the other way round", peer)
			}
			continue
		}
		if _, network, err := net.ParseCIDR(peer.IPBlock.CIDR); err != nil {
			t.Errorf("DNS peer %q is not a CIDR", peer.IPBlock.CIDR)
		} else if ones, bits := network.Mask.Size(); ones != bits {
			t.Errorf("DNS peer %q is a range, not a host: port 53 to a range is a tunnel", peer.IPBlock.CIDR)
		}
		if len(peer.IPBlock.Except) != 0 {
			t.Errorf("DNS peer %q carries an except block, which only ever widens a host route", peer.IPBlock.CIDR)
		}
	}

	// Rule 2, the bus. One peer, selected by label in this agent's own
	// namespace -- not an IPBlock, because a pod IP does not survive a restart
	// and a fence pinned to one stops matching without failing. The
	// NamespaceSelector is load-bearing rather than decorative: a bare
	// PodSelector would match `nats` pods in EVERY namespace, which on a
	// multi-tenant cluster is egress to another tenant's bus.
	bus := np.Spec.Egress[1]
	if len(bus.Ports) != 1 || bus.Ports[0].Port.IntValue() != int(a2aNATSClientPort) ||
		bus.Ports[0].Protocol == nil || *bus.Ports[0].Protocol != corev1.ProtocolTCP {
		t.Errorf("bus rule is not exactly TCP %d: %+v", a2aNATSClientPort, bus.Ports)
	}
	wantBus := []networkingv1.NetworkPolicyPeer{namespacedPodPeer(agent.Namespace, map[string]string{
		labelPartOf:       a2aPartOf,
		a2aComponentLabel: "nats",
	})}
	if !reflect.DeepEqual(bus.To, wantBus) {
		t.Errorf("bus peer = %+v, want the nats pods in %s only", bus.To, agent.Namespace)
	}
}

func verifierContainer(t *testing.T, containers []corev1.Container) corev1.Container {
	t.Helper()
	if len(containers) != 1 {
		t.Fatalf("the verifier pod has %d containers, want 1", len(containers))
	}
	return containers[0]
}

func hasEnv(env []corev1.EnvVar, name string) bool {
	for _, e := range env {
		if e.Name == name {
			return true
		}
	}
	return false
}

// The budget, and what it has to select. obtainability_audit_sop.md §3.3 flags
// a replicas >= 2 workload with no PDB "whose spec.selector matches
// spec.template.metadata.labels", and a budget that exists but selects the
// wrong pods satisfies a test for existence while leaving the finding open.
// So the assertion is equality with the Deployment's own selector, and that
// the selector is the verifier's alone: a2aLabels is shared by every workload
// of the next stack, and a budget keyed on it would count the callout's and
// the gateway's pods toward the verifier's allowance, letting a drain take
// both verifiers while the budget reads as satisfied.
func TestTheVerifierBudgetSelectsTheDeploymentItBudgets(t *testing.T) {
	agent := identityTestAgent()
	dep := buildA2AVerifierDeployment(agent)
	pdb := buildA2AVerifierPDB(agent)

	if pdb.Name != dep.Name || pdb.Namespace != dep.Namespace {
		t.Errorf("the budget is %s/%s, the Deployment %s/%s; the teardown deletes the budget by the Deployment's name",
			pdb.Namespace, pdb.Name, dep.Namespace, dep.Name)
	}
	// maxUnavailable, never minAvailable (SOP §3.3/§3.4): minAvailable: 1
	// over a count that has since dropped to one is the budget that blocks
	// every drain on the cluster.
	if pdb.Spec.MinAvailable != nil {
		t.Errorf("the verifier budget sets minAvailable %v; that is the drain-deadlocking shape", pdb.Spec.MinAvailable)
	}
	if pdb.Spec.MaxUnavailable == nil || pdb.Spec.MaxUnavailable.IntValue() != 1 {
		t.Errorf("maxUnavailable = %v, want 1", pdb.Spec.MaxUnavailable)
	}

	if pdb.Spec.Selector == nil || len(pdb.Spec.Selector.MatchLabels) == 0 {
		t.Fatal("the verifier budget has an empty selector; in policy/v1 that budgets every pod in the namespace")
	}
	if !reflect.DeepEqual(pdb.Spec.Selector, dep.Spec.Selector) {
		t.Errorf("budget selector %v != Deployment selector %v; the eviction API would consult a budget that does not cover these pods",
			pdb.Spec.Selector.MatchLabels, dep.Spec.Selector.MatchLabels)
	}
	selector, err := metav1.LabelSelectorAsSelector(pdb.Spec.Selector)
	if err != nil {
		t.Fatalf("budget selector does not parse: %v", err)
	}
	if !selector.Matches(labels.Set(dep.Spec.Template.Labels)) {
		t.Errorf("budget selector %v does not match the verifier pod labels %v", pdb.Spec.Selector.MatchLabels, dep.Spec.Template.Labels)
	}
	// The other next-stack workloads carry the same a2aLabels and must fall
	// outside this budget — and the verifier's pods must fall outside
	// theirs. Neither the callout nor the a2a gateway carries a budget of
	// its own today, so the converse is asserted against the selectors a
	// budget for either would be built from (a budget copies its
	// Deployment's selector, as this one does), plus the one other budget
	// the operator does render in the namespace, the platform PDB on
	// `app: <agent>-gateway`. A selector of theirs that matched these pods
	// would count a verifier toward some other workload's allowance.
	verifierPods := labels.Set(dep.Spec.Template.Labels)
	for name, other := range map[string]*appsv1.Deployment{
		"callout": buildA2ACalloutDeployment(agent),
		"gateway": buildA2AGatewayDeployment(agent),
	} {
		if selector.Matches(labels.Set(other.Spec.Template.Labels)) {
			t.Errorf("the verifier budget also selects the %s pods %v; the budget would be satisfied by the wrong workload's replicas",
				name, other.Spec.Template.Labels)
		}
		theirs, err := metav1.LabelSelectorAsSelector(other.Spec.Selector)
		if err != nil {
			t.Fatalf("%s selector does not parse: %v", name, err)
		}
		if theirs.Matches(verifierPods) {
			t.Errorf("the %s selector %v also matches the verifier pods %v; a budget built from it would count a verifier toward the %s allowance",
				name, other.Spec.Selector.MatchLabels, verifierPods, name)
		}
	}
	platform, err := metav1.LabelSelectorAsSelector(buildPlatformPDB(agent).Spec.Selector)
	if err != nil {
		t.Fatalf("platform budget selector does not parse: %v", err)
	}
	if platform.Matches(verifierPods) {
		t.Errorf("the platform budget selects the verifier pods %v; a drain would charge a verifier eviction to the gateway's allowance", verifierPods)
	}

	// Labelled as part of the next stack, which is how the residue sweep and
	// the flip to today find it. A budget stamped only with the default
	// part-of would be invisible to both and survive the flip.
	if got := pdb.Labels[labelPartOf]; got != a2aPartOf {
		t.Errorf("budget part-of label = %q, want %q; the teardown sweep lists by that label", got, a2aPartOf)
	}
	if got := pdb.Labels[a2aComponentLabel]; got != "verifier" {
		t.Errorf("budget component label = %q, want verifier", got)
	}
}

// The spread. One hostname constraint, advisory, selecting the same pods the
// budget does. ScheduleAnyway is asserted rather than left to the reader: a
// DoNotSchedule here would pend the surge pod of a MaxUnavailable 0 /
// MaxSurge 1 rollout on any cluster with as many nodes as replicas, and the
// second replica of a single-node dev install forever.
func TestTheVerifierSpreadsAcrossNodesWithoutRefusingToSchedule(t *testing.T) {
	agent := identityTestAgent()
	dep := buildA2AVerifierDeployment(agent)
	pod := dep.Spec.Template.Spec

	if len(pod.TopologySpreadConstraints) != 1 {
		t.Fatalf("the verifier pod carries %d topology spread constraints, want 1 on the hostname", len(pod.TopologySpreadConstraints))
	}
	spread := pod.TopologySpreadConstraints[0]
	if spread.TopologyKey != "kubernetes.io/hostname" {
		t.Errorf("spread topologyKey = %q, want kubernetes.io/hostname; a zone key is satisfied by two pods on one node", spread.TopologyKey)
	}
	if spread.MaxSkew != 1 {
		t.Errorf("spread maxSkew = %d, want 1", spread.MaxSkew)
	}
	if spread.WhenUnsatisfiable != corev1.ScheduleAnyway {
		t.Errorf("spread whenUnsatisfiable = %q, want ScheduleAnyway; DoNotSchedule pends the rollout's surge pod and a single-node install's second replica",
			spread.WhenUnsatisfiable)
	}
	if !reflect.DeepEqual(spread.LabelSelector, dep.Spec.Selector) {
		t.Errorf("spread selector %v != Deployment selector %v; the scheduler would balance a different pod set", spread.LabelSelector, dep.Spec.Selector)
	}
	// Scoped to one ReplicaSet, as the chart's helper is: counted across old
	// and new pods together, a MaxSurge 1 rollout over two nodes can end
	// with both new replicas on one node (the argument is on the constant).
	if !reflect.DeepEqual(spread.MatchLabelKeys, []string{"pod-template-hash"}) {
		t.Errorf("spread matchLabelKeys = %v, want [pod-template-hash]; without it a rollout can re-co-locate the replicas", spread.MatchLabelKeys)
	}
	// Spread is the mechanism, not anti-affinity beside it: two mechanisms on
	// one key is a second thing to keep in step with the selector.
	if pod.Affinity != nil && pod.Affinity.PodAntiAffinity != nil {
		t.Error("the verifier pod carries pod anti-affinity as well as a topology spread; one mechanism, keyed on one selector")
	}
}

// reconcileA2AVerifier applies the budget, owned by the CR, selecting the
// Deployment it applied in the same pass. Through the reconcile rather than
// the builder so that dropping the object from the owned list — the shape the
// render had before #2058 — fails.
func TestReconcileA2AVerifierRendersAnEvictableBudget(t *testing.T) {
	ctx := context.Background()
	scheme := setupScheme()
	agent := a2aTestAgent()

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.reconcileA2AVerifier(ctx, agent); err != nil {
		t.Fatalf("reconcileA2AVerifier: %v", err)
	}

	key := types.NamespacedName{Name: a2aVerifierName(agent), Namespace: agent.Namespace}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, key, dep); err != nil {
		t.Fatalf("the verifier Deployment was not applied: %v", err)
	}
	pdb := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, key, pdb); err != nil {
		t.Fatalf("no PodDisruptionBudget %s after reconcileA2AVerifier: %v", key, err)
	}
	if pdb.Spec.MaxUnavailable == nil || pdb.Spec.MaxUnavailable.IntValue() != 1 || pdb.Spec.MinAvailable != nil {
		t.Errorf("budget = maxUnavailable %v / minAvailable %v, want 1 / unset", pdb.Spec.MaxUnavailable, pdb.Spec.MinAvailable)
	}
	if !reflect.DeepEqual(pdb.Spec.Selector, dep.Spec.Selector) {
		t.Errorf("live budget selector %v != live Deployment selector %v", pdb.Spec.Selector, dep.Spec.Selector)
	}
	if !metav1.IsControlledBy(pdb, agent) {
		t.Errorf("the budget is not controlled by the PlatformAgent: %v", pdb.OwnerReferences)
	}
}

// The unhealthy-pod policy, on the rendered object and on the one the
// reconcile applies. The verifier's readiness is the bus connection, and the
// bus is one replica: with it down, or its PVC Pending, or the provision Job
// failed, both verifiers are Running and NotReady, and under the default
// IfHealthyBudget policy the budget then refuses to evict either of them —
// currentHealthy 0, disruptionsAllowed 0, 429 from the eviction API — for a
// workload that is already fully down. AlwaysAllow (KEP-3017) is what lets
// the drain proceed while leaving maxUnavailable: 1 over the ready pods. A
// nil here is not "unset", it is the default, so the pin is on the value.
func TestTheVerifierBudgetLetsAnUnreadyPodBeEvicted(t *testing.T) {
	ctx := context.Background()
	scheme := setupScheme()
	agent := a2aTestAgent()

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if err := r.reconcileA2AVerifier(ctx, agent); err != nil {
		t.Fatalf("reconcileA2AVerifier: %v", err)
	}
	applied := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aVerifierName(agent), Namespace: agent.Namespace}, applied); err != nil {
		t.Fatalf("no PodDisruptionBudget after reconcileA2AVerifier: %v", err)
	}

	for name, pdb := range map[string]*policyv1.PodDisruptionBudget{
		"rendered": buildA2AVerifierPDB(agent),
		"applied":  applied,
	} {
		got := pdb.Spec.UnhealthyPodEvictionPolicy
		if got == nil || *got != policyv1.AlwaysAllow {
			t.Errorf("%s budget unhealthyPodEvictionPolicy = %v, want %s; with the bus down both verifiers are Running/NotReady and the default budget blocks the drain of a workload that is already fully down",
				name, ptr.Deref(got, "<nil, i.e. IfHealthyBudget>"), policyv1.AlwaysAllow)
		}
	}
}

// The wedge reconcilePodDisruptionBudget already guards against, on the
// verifier's budget: a hand-set minAvailable that a forced apply cannot
// remove, so every apply merges to both fields and is refused — and because
// this step sits ahead of the fences and the provision Job in reconcileA2A,
// the whole next render fails from then on. Through reconcileA2AVerifier, so
// that deleting the clearForeignPDBBudgetField call rather than gutting the
// helper is what reds.
func TestReconcileA2AVerifierRecoversFromAForeignBudgetField(t *testing.T) {
	ctx := context.Background()
	scheme := setupScheme()
	agent := a2aTestAgent()
	live := &policyv1.PodDisruptionBudget{
		ObjectMeta: metav1.ObjectMeta{Name: a2aVerifierName(agent), Namespace: agent.Namespace},
		Spec: policyv1.PodDisruptionBudgetSpec{
			MinAvailable: ptr.To(intstr.FromInt32(1)),
			Selector:     &metav1.LabelSelector{MatchLabels: a2aVerifierPodSelector(agent)},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, live).
		WithInterceptorFuncs(pdbSSAInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.reconcileA2AVerifier(ctx, agent); err != nil {
		t.Fatalf("reconcileA2AVerifier did not recover from a foreign budget field: %v", err)
	}
	pdb := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(live), pdb); err != nil {
		t.Fatalf("get budget: %v", err)
	}
	if pdb.Spec.MinAvailable != nil {
		t.Errorf("minAvailable survived the reconcile: %v", pdb.Spec.MinAvailable)
	}
	if pdb.Spec.MaxUnavailable == nil || pdb.Spec.MaxUnavailable.IntValue() != 1 {
		t.Errorf("maxUnavailable = %v, want 1", pdb.Spec.MaxUnavailable)
	}
	// The hand-made budget carried no policy; the apply that cleared the
	// foreign field has to land the operator's as well.
	if got := pdb.Spec.UnhealthyPodEvictionPolicy; got == nil || *got != policyv1.AlwaysAllow {
		t.Errorf("unhealthyPodEvictionPolicy after recovery = %v, want %s", ptr.Deref(got, "<nil>"), policyv1.AlwaysAllow)
	}
}

// The darkness property for the budget by name. The label sweep in
// TestNothingA2ALabelledSurvivesAFlipToToday lists PodDisruptionBudgets too
// and is what catches a budget nobody added to the teardown; this is the
// verifier-specific statement a reader of #2058 looks for — rendered under
// next, gone under today — through the full Reconcile rather than the step.
func TestTheVerifierBudgetComesAndGoesWithTheMode(t *testing.T) {
	ctx := context.Background()
	scheme := setupScheme()
	agent := a2aTestAgent()

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	key := types.NamespacedName{Name: a2aVerifierName(agent), Namespace: agent.Namespace}

	// Two passes: the first adds the finalizer, the second renders.
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d under next: %v", i+1, err)
		}
	}
	pdb := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, key, pdb); err != nil {
		t.Fatalf("no verifier budget under mode next: %v", err)
	}

	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	fresh.Spec.Mode = nil
	if err := cl.Update(ctx, fresh); err != nil {
		t.Fatalf("flip to today: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after the flip: %v", err)
	}
	err := cl.Get(ctx, key, &policyv1.PodDisruptionBudget{})
	if !errors.IsNotFound(err) {
		t.Errorf("the verifier budget survives a flip to today (get: %v); a budget over no pods is inert residue until the next verifier is scheduled", err)
	}
}
