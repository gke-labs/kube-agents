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
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

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
		if profile.DNSClusterIP != defaultDNSClusterIP {
			t.Errorf("got DNSClusterIP %q, want default %q", profile.DNSClusterIP, defaultDNSClusterIP)
		}
		if profile.MetadataDaemonIP != metadataDaemonIP {
			t.Errorf("got MetadataDaemonIP %q, want default %q", profile.MetadataDaemonIP, metadataDaemonIP)
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
		if profile.DNSClusterIP != "34.118.224.10" {
			t.Errorf("got DNSClusterIP %q, want discovered %q", profile.DNSClusterIP, "34.118.224.10")
		}
		if profile.MetadataDaemonIP != metadataDaemonIP {
			t.Errorf("got MetadataDaemonIP %q, want %q", profile.MetadataDaemonIP, metadataDaemonIP)
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
		if profile.DNSClusterIP != "10.0.0.53" {
			t.Errorf("got DNSClusterIP %q, want operator override %q", profile.DNSClusterIP, "10.0.0.53")
		}
		if profile.MetadataDaemonIP != "169.254.169.250" {
			t.Errorf("got MetadataDaemonIP %q, want operator override %q", profile.MetadataDaemonIP, "169.254.169.250")
		}
	})

	t.Run("AnnotationWinsOverOperatorOverride", func(t *testing.T) {
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
		}

		profile := r.resolveNetpolProfile(context.Background(), agent)
		if profile.DNSClusterIP != "172.16.0.10" {
			t.Errorf("got DNSClusterIP %q, want annotation %q", profile.DNSClusterIP, "172.16.0.10")
		}
		if profile.MetadataDaemonIP != "169.254.169.240" {
			t.Errorf("got MetadataDaemonIP %q, want annotation %q", profile.MetadataDaemonIP, "169.254.169.240")
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
		if profile.DNSClusterIP != "34.118.224.10" {
			t.Errorf("got DNSClusterIP %q, want fallback to discovered %q", profile.DNSClusterIP, "34.118.224.10")
		}
		if profile.MetadataDaemonIP != metadataDaemonIP {
			t.Errorf("got MetadataDaemonIP %q, want fallback to default %q", profile.MetadataDaemonIP, metadataDaemonIP)
		}
	})
}
