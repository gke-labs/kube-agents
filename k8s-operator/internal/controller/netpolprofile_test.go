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
	"reflect"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func TestResolveNetpolProfile(t *testing.T) {
	t.Parallel()

	t.Run("DefaultValues", func(t *testing.T) {
		t.Parallel()
		// Per-subtest, not hoisted into the parent: a Scheme shared across
		// parallel fake clients is a data race. See internal/testing/golden_test.go.
		scheme := setupScheme()
		client := fake.NewClientBuilder().WithScheme(scheme).Build()
		r := &PlatformAgentReconciler{Client: client, Scheme: scheme}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		if !profile.Generated {
			t.Errorf("got Generated=false, want true")
		}
		if !reflect.DeepEqual(profile.DNSClusterIPs, []string{defaultDNSClusterIP}) {
			t.Errorf("got DNSClusterIPs %v, want [%s]", profile.DNSClusterIPs, defaultDNSClusterIP)
		}
		if profile.DNSSource != netpolSourceDefault {
			t.Errorf("got DNSSource %q, want %q", profile.DNSSource, netpolSourceDefault)
		}
		if profile.MetadataDaemonIP != metadataDaemonIP {
			t.Errorf("got MetadataDaemonIP %q, want default %q", profile.MetadataDaemonIP, metadataDaemonIP)
		}
		if profile.MetadataDaemonSource != netpolSourceDefault {
			t.Errorf("got MetadataDaemonSource %q, want %q", profile.MetadataDaemonSource, netpolSourceDefault)
		}
	})

	t.Run("DiscoveryKubeDNS", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		kubeDNSSvc := &corev1.Service{
			ObjectMeta: metav1.ObjectMeta{Namespace: "kube-system", Name: "kube-dns"},
			Spec:       corev1.ServiceSpec{ClusterIP: "34.118.224.10"},
		}
		client := fake.NewClientBuilder().WithScheme(scheme).WithObjects(kubeDNSSvc).Build()
		r := &PlatformAgentReconciler{Client: client, Scheme: scheme}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		if !reflect.DeepEqual(profile.DNSClusterIPs, []string{"34.118.224.10"}) {
			t.Errorf("got DNSClusterIPs %v, want [34.118.224.10]", profile.DNSClusterIPs)
		}
		if profile.DNSSource != netpolSourceDiscovered {
			t.Errorf("got DNSSource %q, want %q", profile.DNSSource, netpolSourceDiscovered)
		}
		if profile.MetadataDaemonIP != metadataDaemonIP {
			t.Errorf("got MetadataDaemonIP %q, want %q", profile.MetadataDaemonIP, metadataDaemonIP)
		}
		if profile.MetadataDaemonSource != netpolSourceDefault {
			t.Errorf("got MetadataDaemonSource %q, want %q", profile.MetadataDaemonSource, netpolSourceDefault)
		}
	})

	t.Run("DiscoveryKubeDNS_DualStack", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		kubeDNSSvc := &corev1.Service{
			ObjectMeta: metav1.ObjectMeta{Namespace: "kube-system", Name: "kube-dns"},
			Spec: corev1.ServiceSpec{
				ClusterIPs: []string{"10.96.0.10", "2001:db8::10"},
			},
		}
		client := fake.NewClientBuilder().WithScheme(scheme).WithObjects(kubeDNSSvc).Build()
		r := &PlatformAgentReconciler{Client: client, Scheme: scheme}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		wantIPs := []string{"10.96.0.10", "2001:db8::10"}
		if !reflect.DeepEqual(profile.DNSClusterIPs, wantIPs) {
			t.Errorf("got DNSClusterIPs %v, want %v", profile.DNSClusterIPs, wantIPs)
		}
		if profile.DNSSource != netpolSourceDiscovered {
			t.Errorf("got DNSSource %q, want %q", profile.DNSSource, netpolSourceDiscovered)
		}
	})

	// The three subtests below hold the anti-flap arm of the discovery rung: a Get that
	// fails with anything other than NotFound must not drop the agent back to
	// defaultDNSClusterIP. On a cluster whose kube-dns sits outside the classic Service
	// CIDR -- 34.118.224.10 on GKE -- that fallback rewrites rule 1 with the wrong peer
	// and takes DNS out from under the agent until the next successful reconcile, which
	// is the outage the branch exists to prevent.
	t.Run("DiscoveryTransientError_PreservesDiscoveredStatus", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		client := fake.NewClientBuilder().WithScheme(scheme).
			WithInterceptorFuncs(kubeDNSGetFails()).Build()
		r := &PlatformAgentReconciler{Client: client, Scheme: scheme}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
			Status: agentv1alpha1.AgentStatus{
				NetworkPolicy: agentv1alpha1.NetworkPolicyStatus{
					DNSClusterIPs:       []string{"34.118.224.10", "2001:db8::10"},
					DNSClusterIPsSource: netpolSourceDiscovered,
				},
			},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		wantIPs := []string{"2001:db8::10", "34.118.224.10"} // deduplicateAndSortIPs sorts
		if !reflect.DeepEqual(profile.DNSClusterIPs, wantIPs) {
			t.Errorf("got DNSClusterIPs %v, want the previously discovered %v", profile.DNSClusterIPs, wantIPs)
		}
		if profile.DNSSource != netpolSourceDiscovered {
			t.Errorf("got DNSSource %q, want %q", profile.DNSSource, netpolSourceDiscovered)
		}
	})

	t.Run("DiscoveryTransientError_NoPriorStatusFallsBackToDefault", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		client := fake.NewClientBuilder().WithScheme(scheme).
			WithInterceptorFuncs(kubeDNSGetFails()).Build()
		r := &PlatformAgentReconciler{Client: client, Scheme: scheme}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		if !reflect.DeepEqual(profile.DNSClusterIPs, []string{defaultDNSClusterIP}) {
			t.Errorf("got DNSClusterIPs %v, want [%s]", profile.DNSClusterIPs, defaultDNSClusterIP)
		}
		if profile.DNSSource != netpolSourceDefault {
			t.Errorf("got DNSSource %q, want %q", profile.DNSSource, netpolSourceDefault)
		}
	})

	// The preserved status has to have come from discovery. A status left over from a
	// Spec pin that has since been removed says nothing about what kube-dns is, and
	// carrying it forward would report Discovered for an IP nothing discovered.
	t.Run("DiscoveryTransientError_IgnoresNonDiscoveredStatus", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		client := fake.NewClientBuilder().WithScheme(scheme).
			WithInterceptorFuncs(kubeDNSGetFails()).Build()
		r := &PlatformAgentReconciler{Client: client, Scheme: scheme}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
			Status: agentv1alpha1.AgentStatus{
				NetworkPolicy: agentv1alpha1.NetworkPolicyStatus{
					DNSClusterIPs:       []string{"10.100.0.10"},
					DNSClusterIPsSource: netpolSourceSpec,
				},
			},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		if !reflect.DeepEqual(profile.DNSClusterIPs, []string{defaultDNSClusterIP}) {
			t.Errorf("got DNSClusterIPs %v, want [%s]", profile.DNSClusterIPs, defaultDNSClusterIP)
		}
		if profile.DNSSource != netpolSourceDefault {
			t.Errorf("got DNSSource %q, want %q", profile.DNSSource, netpolSourceDefault)
		}
	})

	t.Run("OperatorOverrideWinsOverDiscovery", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		kubeDNSSvc := &corev1.Service{
			ObjectMeta: metav1.ObjectMeta{Namespace: "kube-system", Name: "kube-dns"},
			Spec:       corev1.ServiceSpec{ClusterIP: "34.118.224.10"},
		}
		client := fake.NewClientBuilder().WithScheme(scheme).WithObjects(kubeDNSSvc).Build()
		r := &PlatformAgentReconciler{
			Client:                   client,
			Scheme:                   scheme,
			DNSClusterIPOverride:     "10.0.0.53",
			MetadataDaemonIPOverride: "169.254.169.250",
		}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		if !reflect.DeepEqual(profile.DNSClusterIPs, []string{"10.0.0.53"}) {
			t.Errorf("got DNSClusterIPs %v, want [10.0.0.53]", profile.DNSClusterIPs)
		}
		if profile.DNSSource != netpolSourceOperatorEnv {
			t.Errorf("got DNSSource %q, want %q", profile.DNSSource, netpolSourceOperatorEnv)
		}
		if profile.MetadataDaemonIP != "169.254.169.250" {
			t.Errorf("got MetadataDaemonIP %q, want %q", profile.MetadataDaemonIP, "169.254.169.250")
		}
		if profile.MetadataDaemonSource != netpolSourceOperatorEnv {
			t.Errorf("got MetadataDaemonSource %q, want %q", profile.MetadataDaemonSource, netpolSourceOperatorEnv)
		}
	})

	t.Run("SpecWinsOverOperatorOverride", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		kubeDNSSvc := &corev1.Service{
			ObjectMeta: metav1.ObjectMeta{Namespace: "kube-system", Name: "kube-dns"},
			Spec:       corev1.ServiceSpec{ClusterIP: "34.118.224.10"},
		}
		client := fake.NewClientBuilder().WithScheme(scheme).WithObjects(kubeDNSSvc).Build()
		r := &PlatformAgentReconciler{
			Client:                   client,
			Scheme:                   scheme,
			DNSClusterIPOverride:     "10.0.0.53",
			MetadataDaemonIPOverride: "169.254.169.250",
		}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
			Spec: agentv1alpha1.PlatformAgentSpec{
				AgentSpec: agentv1alpha1.AgentSpec{
					NetworkPolicy: &agentv1alpha1.NetworkPolicySpec{
						DNSClusterIPs: []string{"10.100.0.10"},
						MetadataDaemon: &agentv1alpha1.MetadataDaemonSpec{
							Endpoint: "169.254.169.245",
						},
					},
				},
			},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		if !reflect.DeepEqual(profile.DNSClusterIPs, []string{"10.100.0.10"}) {
			t.Errorf("got DNSClusterIPs %v, want [10.100.0.10]", profile.DNSClusterIPs)
		}
		if profile.DNSSource != netpolSourceSpec {
			t.Errorf("got DNSSource %q, want %q", profile.DNSSource, netpolSourceSpec)
		}
		if profile.MetadataDaemonIP != "169.254.169.245" {
			t.Errorf("got MetadataDaemonIP %q, want %q", profile.MetadataDaemonIP, "169.254.169.245")
		}
		if profile.MetadataDaemonSource != netpolSourceSpec {
			t.Errorf("got MetadataDaemonSource %q, want %q", profile.MetadataDaemonSource, netpolSourceSpec)
		}
	})

	t.Run("AnnotationWinsOverSpec", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		kubeDNSSvc := &corev1.Service{
			ObjectMeta: metav1.ObjectMeta{Namespace: "kube-system", Name: "kube-dns"},
			Spec:       corev1.ServiceSpec{ClusterIP: "34.118.224.10"},
		}
		client := fake.NewClientBuilder().WithScheme(scheme).WithObjects(kubeDNSSvc).Build()
		r := &PlatformAgentReconciler{
			Client:                   client,
			Scheme:                   scheme,
			DNSClusterIPOverride:     "10.0.0.53",
			MetadataDaemonIPOverride: "169.254.169.250",
		}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
				Annotations: map[string]string{
					AnnotationDNSClusterIP:     "172.16.0.10",
					AnnotationMetadataDaemonIP: "169.254.169.240",
				},
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				AgentSpec: agentv1alpha1.AgentSpec{
					NetworkPolicy: &agentv1alpha1.NetworkPolicySpec{
						DNSClusterIPs: []string{"10.100.0.10"},
						MetadataDaemon: &agentv1alpha1.MetadataDaemonSpec{
							Endpoint: "169.254.169.245",
						},
					},
				},
			},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		if !reflect.DeepEqual(profile.DNSClusterIPs, []string{"172.16.0.10"}) {
			t.Errorf("got DNSClusterIPs %v, want [172.16.0.10]", profile.DNSClusterIPs)
		}
		if profile.DNSSource != netpolSourceAnnotation {
			t.Errorf("got DNSSource %q, want %q", profile.DNSSource, netpolSourceAnnotation)
		}
		if profile.MetadataDaemonIP != "169.254.169.240" {
			t.Errorf("got MetadataDaemonIP %q, want [169.254.169.240]", profile.MetadataDaemonIP)
		}
		if profile.MetadataDaemonSource != netpolSourceAnnotation {
			t.Errorf("got MetadataDaemonSource %q, want %q", profile.MetadataDaemonSource, netpolSourceAnnotation)
		}
	})

	t.Run("MetadataDaemonSuppression", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		client := fake.NewClientBuilder().WithScheme(scheme).Build()
		r := &PlatformAgentReconciler{Client: client, Scheme: scheme}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
			Spec: agentv1alpha1.PlatformAgentSpec{
				AgentSpec: agentv1alpha1.AgentSpec{
					NetworkPolicy: &agentv1alpha1.NetworkPolicySpec{
						MetadataDaemon: &agentv1alpha1.MetadataDaemonSpec{
							Endpoint: "",
						},
					},
				},
			},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		if profile.MetadataDaemonIP != "" {
			t.Errorf("got MetadataDaemonIP %q, want empty (suppressed)", profile.MetadataDaemonIP)
		}
		if profile.MetadataDaemonSource != netpolSourceSuppressed {
			t.Errorf("got MetadataDaemonSource %q, want %q", profile.MetadataDaemonSource, netpolSourceSuppressed)
		}
	})

	t.Run("NetworkPolicyDisabled", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		client := fake.NewClientBuilder().WithScheme(scheme).Build()
		r := &PlatformAgentReconciler{Client: client, Scheme: scheme}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
			Spec: agentv1alpha1.PlatformAgentSpec{
				AgentSpec: agentv1alpha1.AgentSpec{
					NetworkPolicy: &agentv1alpha1.NetworkPolicySpec{
						Enabled: ptr.To(false),
					},
				},
			},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		if profile.Generated {
			t.Errorf("got Generated=true, want false")
		}
	})

	t.Run("AdditionalEgress", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		client := fake.NewClientBuilder().WithScheme(scheme).Build()
		r := &PlatformAgentReconciler{Client: client, Scheme: scheme}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
			Spec: agentv1alpha1.PlatformAgentSpec{
				AgentSpec: agentv1alpha1.AgentSpec{
					NetworkPolicy: &agentv1alpha1.NetworkPolicySpec{
						AdditionalEgress: []agentv1alpha1.EgressRule{
							{
								To: []agentv1alpha1.EgressPeer{
									{CIDR: "10.0.0.0/16", Except: []string{"10.0.1.0/24"}},
									{CIDR: "192.168.1.5"}, // bare IP -> /32
								},
								// One entry per arm of toEgressRules' protocol switch. The
								// lower-case "sctp" also holds the ToUpper: losing it sends
								// the entry to the default arm, which drops the port rather
								// than mistyping it.
								Ports: []agentv1alpha1.EgressPort{
									{Protocol: "TCP", Port: 5432},
									{Protocol: "UDP", Port: 8125},
									{Protocol: "sctp", Port: 9899},
								},
							},
						},
					},
				},
			},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		if len(profile.AdditionalEgress) != 1 {
			t.Fatalf("got %d additional egress rules, want 1", len(profile.AdditionalEgress))
		}
		rule := profile.AdditionalEgress[0]
		if len(rule.To) != 2 {
			t.Errorf("got %d peers, want 2", len(rule.To))
		}
		if rule.To[0].IPBlock.CIDR != "10.0.0.0/16" {
			t.Errorf("got CIDR %q, want 10.0.0.0/16", rule.To[0].IPBlock.CIDR)
		}
		if len(rule.To[0].IPBlock.Except) != 1 || rule.To[0].IPBlock.Except[0] != "10.0.1.0/24" {
			t.Errorf("got Except %v, want [10.0.1.0/24]", rule.To[0].IPBlock.Except)
		}
		if rule.To[1].IPBlock.CIDR != "192.168.1.5/32" {
			t.Errorf("got CIDR %q, want 192.168.1.5/32", rule.To[1].IPBlock.CIDR)
		}

		// Ports are the half of toEgressRules that widens egress by doing nothing:
		// a rule that keeps its peers and loses its ports permits EVERY port to
		// them, the mirror of the len(peers) == 0 guard. Assert the emitted list
		// exactly -- length, order, protocol, value -- so dropping the append,
		// reordering the switch or losing the ToUpper fails here instead of
		// shipping an all-ports rule with a green suite.
		wantPorts := []struct {
			protocol corev1.Protocol
			port     int
		}{
			{corev1.ProtocolTCP, 5432},
			{corev1.ProtocolUDP, 8125},
			{corev1.ProtocolSCTP, 9899},
		}
		if len(rule.Ports) != len(wantPorts) {
			t.Fatalf("got %d ports (%+v), want %d", len(rule.Ports), rule.Ports, len(wantPorts))
		}
		for i, want := range wantPorts {
			got := rule.Ports[i]
			switch {
			case got.Protocol == nil:
				t.Errorf("port %d: got nil Protocol, want %s", i, want.protocol)
			case *got.Protocol != want.protocol:
				t.Errorf("port %d: got protocol %s, want %s", i, *got.Protocol, want.protocol)
			}
			switch {
			case got.Port == nil:
				t.Errorf("port %d: got nil Port, want %d", i, want.port)
			case got.Port.IntValue() != want.port:
				t.Errorf("port %d: got port %s, want %d", i, got.Port.String(), want.port)
			}
		}
	})

	// The regression for the IPv4-mapped IPv6 collapse. Before normalizeCIDRTarget
	// took the address family from the address, net.ParseCIDR reported these as
	// 128-bit prefixes -- clearing the /48 IPv6 floor -- and net.IPNet.String() then
	// re-read the address, found To4() != nil, truncated the mask to its low four
	// bytes and printed an IPv4 CIDR far broader than the /12 floor. "::ffff:0:0/96"
	// emitted the literal string "0.0.0.0/0": allow-all egress on every port from a
	// pod holding the agent's Workload Identity credentials.
	t.Run("AdditionalEgress_IPv4MappedIPv6_NeverWidens", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		client := fake.NewClientBuilder().WithScheme(scheme).Build()
		r := &PlatformAgentReconciler{Client: client, Scheme: scheme}

		for _, tc := range []struct {
			name string
			cidr string
			want string // "" means the peer must be dropped entirely
		}{
			{name: "AllZeroMappedPrefix", cidr: "::ffff:0:0/96", want: ""},
			{name: "MappedTenSlashEight", cidr: "::ffff:a00:0/104", want: ""},
			// 108 - 96 = an IPv4 /12, exactly the floor, so this one is
			// legitimate -- and it must emit as the IPv4 block it means.
			{name: "MappedAtTheIPv4Floor", cidr: "::ffff:a00:0/108", want: "10.0.0.0/12"},
			{name: "MappedNarrowEnough", cidr: "::ffff:a00:0/120", want: "10.0.0.0/24"},
			{name: "MappedBareHost", cidr: "::ffff:102:304", want: "1.2.3.4/32"},
		} {
			agent := &agentv1alpha1.PlatformAgent{
				ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
				Spec: agentv1alpha1.PlatformAgentSpec{
					AgentSpec: agentv1alpha1.AgentSpec{
						NetworkPolicy: &agentv1alpha1.NetworkPolicySpec{
							AdditionalEgress: []agentv1alpha1.EgressRule{{
								To:    []agentv1alpha1.EgressPeer{{CIDR: tc.cidr}},
								Ports: []agentv1alpha1.EgressPort{{Protocol: "TCP", Port: 443}},
							}},
						},
					},
				},
			}

			profile := r.resolveNetpolProfile(context.Background(), agent)
			if tc.want == "" {
				if len(profile.AdditionalEgress) != 0 {
					t.Errorf("%s: %q produced %d rules, want 0 (emitted %q)",
						tc.name, tc.cidr, len(profile.AdditionalEgress),
						profile.AdditionalEgress[0].To[0].IPBlock.CIDR)
				}
				continue
			}
			if len(profile.AdditionalEgress) != 1 || len(profile.AdditionalEgress[0].To) != 1 {
				t.Fatalf("%s: %q produced %d rules, want 1", tc.name, tc.cidr, len(profile.AdditionalEgress))
			}
			if got := profile.AdditionalEgress[0].To[0].IPBlock.CIDR; got != tc.want {
				t.Errorf("%s: %q emitted %q, want %q", tc.name, tc.cidr, got, tc.want)
			}
		}
	})

	// An except outside its peer's CIDR is rejected by the API server for the WHOLE
	// NetworkPolicy, which would freeze every other egress rule -- including the DNS
	// rediscovery -- at its previous revision. Contain it to its own peer instead.
	t.Run("AdditionalEgress_ExceptOutsidePeerIsDropped", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		client := fake.NewClientBuilder().WithScheme(scheme).Build()
		r := &PlatformAgentReconciler{Client: client, Scheme: scheme}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
			Spec: agentv1alpha1.PlatformAgentSpec{
				AgentSpec: agentv1alpha1.AgentSpec{
					NetworkPolicy: &agentv1alpha1.NetworkPolicySpec{
						AdditionalEgress: []agentv1alpha1.EgressRule{{
							To: []agentv1alpha1.EgressPeer{{
								CIDR: "10.0.0.0/12",
								Except: []string{
									"192.168.0.0/16", // outside the peer
									"fd00::/64",      // wrong family
									"10.1.0.0/16",    // strictly inside, kept
									"10.0.0.0/8",     // broader than the peer
									"10.0.0.0/12",    // equal to the peer: ValidateIPBlock
									//                   requires a STRICT subset
									"not-a-cidr",
								},
							}},
							Ports: []agentv1alpha1.EgressPort{{Protocol: "TCP", Port: 5432}},
						}},
					},
				},
			},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		if len(profile.AdditionalEgress) != 1 {
			t.Fatalf("got %d rules, want 1", len(profile.AdditionalEgress))
		}
		got := profile.AdditionalEgress[0].To[0].IPBlock.Except
		if !reflect.DeepEqual(got, []string{"10.1.0.0/16"}) {
			t.Errorf("got Except %v, want [10.1.0.0/16]", got)
		}
	})

	t.Run("AdditionalEgress_NoPeers_NeverEmitsAllowAll", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		client := fake.NewClientBuilder().WithScheme(scheme).Build()
		r := &PlatformAgentReconciler{Client: client, Scheme: scheme}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
			Spec: agentv1alpha1.PlatformAgentSpec{
				AgentSpec: agentv1alpha1.AgentSpec{
					NetworkPolicy: &agentv1alpha1.NetworkPolicySpec{
						AdditionalEgress: []agentv1alpha1.EgressRule{
							{
								// Ports with no peers or invalid CIDRs must NOT produce an allow-all egress rule
								To: []agentv1alpha1.EgressPeer{
									{CIDR: "invalid-cidr"},
									{CIDR: "0.0.0.0/0"}, // rejected by /12 min prefix check
								},
								Ports: []agentv1alpha1.EgressPort{
									{Protocol: "TCP", Port: 443},
								},
							},
						},
					},
				},
			},
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		if len(profile.AdditionalEgress) != 0 {
			t.Fatalf("expected 0 additional egress rules for invalid/dropped peers, got %d: %+v", len(profile.AdditionalEgress), profile.AdditionalEgress)
		}
	})

	t.Run("InvalidInputsIgnored", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		kubeDNSSvc := &corev1.Service{
			ObjectMeta: metav1.ObjectMeta{Namespace: "kube-system", Name: "kube-dns"},
			Spec:       corev1.ServiceSpec{ClusterIP: "34.118.224.10"},
		}
		client := fake.NewClientBuilder().WithScheme(scheme).WithObjects(kubeDNSSvc).Build()
		r := &PlatformAgentReconciler{
			Client:                   client,
			Scheme:                   scheme,
			DNSClusterIPOverride:     "not-an-ip",
			MetadataDaemonIPOverride: "bad-ip",
		}
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-agent",
				Namespace: "default",
				Annotations: map[string]string{
					AnnotationDNSClusterIP:     "invalid-dns",
					AnnotationMetadataDaemonIP: "invalid-daemon",
				},
			},
		}

		// Invalid annotations and overrides are ignored: DNS falls back to discovered, daemon falls back to default.
		profile := r.resolveNetpolProfile(context.Background(), agent)
		if !reflect.DeepEqual(profile.DNSClusterIPs, []string{"34.118.224.10"}) {
			t.Errorf("got DNSClusterIPs %v, want fallback to discovered [34.118.224.10]", profile.DNSClusterIPs)
		}
		if profile.MetadataDaemonIP != metadataDaemonIP {
			t.Errorf("got MetadataDaemonIP %q, want fallback to default %q", profile.MetadataDaemonIP, metadataDaemonIP)
		}
	})
}

// kubeDNSGetFails makes the kube-system/kube-dns read return ServiceUnavailable -- a
// transient API error rather than a NotFound, which is the distinction the discovery
// rung branches on. Every other Get is passed through so the helper stays usable if
// resolveNetpolProfile grows a second read.
func kubeDNSGetFails() interceptor.Funcs {
	return interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if key.Namespace == "kube-system" && key.Name == "kube-dns" {
				return apierrors.NewServiceUnavailable("apiserver is shutting down")
			}
			return c.Get(ctx, key, obj, opts...)
		},
	}
}
