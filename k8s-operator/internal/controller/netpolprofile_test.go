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
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func TestResolveNetpolProfile(t *testing.T) {
	t.Parallel()

	scheme := runtime.NewScheme()
	_ = corev1.AddToScheme(scheme)
	_ = agentv1alpha1.AddToScheme(scheme)

	t.Run("DefaultValues", func(t *testing.T) {
		t.Parallel()
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

	t.Run("OperatorOverrideWinsOverDiscovery", func(t *testing.T) {
		t.Parallel()
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
								Ports: []agentv1alpha1.EgressPort{
									{Protocol: "TCP", Port: 5432},
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
	})

	t.Run("AdditionalEgress_NoPeers_NeverEmitsAllowAll", func(t *testing.T) {
		t.Parallel()
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
