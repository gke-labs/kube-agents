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
	"net"
	"reflect"
	"testing"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
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
