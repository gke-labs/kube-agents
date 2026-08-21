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
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// AnnotationDNSClusterIP overrides the Cluster DNS Service ClusterIP used for NetworkPolicy DNS egress.
	AnnotationDNSClusterIP = "kubeagents.x-k8s.io/dns-cluster-ip"

	// AnnotationMetadataDaemonIP overrides the Workload Identity metadata daemon IP used for NetworkPolicy egress (rule 3).
	AnnotationMetadataDaemonIP = "kubeagents.x-k8s.io/metadata-daemon-ip"

	// defaultDNSClusterIP is the standard fallback DNS VIP when kube-dns cannot be discovered.
	defaultDNSClusterIP = "10.96.0.10"
)

// netpolProfile holds resolved network policy cluster targets.
type netpolProfile struct {
	DNSClusterIP     string
	MetadataDaemonIP string
}

// resolveNetpolProfile mirrors resolveOTLPEndpoint's ladder: per-agent
// annotation, operator flag/env field, kube-dns discovery, documented default.
// The operator override sits above discovery deliberately, matching telemetry.go:
// an explicit operator value is authoritative even when kube-dns is discoverable.
func (r *PlatformAgentReconciler) resolveNetpolProfile(ctx context.Context, agent *agentv1alpha1.PlatformAgent) netpolProfile {
	log := logf.FromContext(ctx).WithName("netpol-profile")
	var p netpolProfile

	// --- DNS cluster IP ---
	if ip := trimmedAnnotation(agent, AnnotationDNSClusterIP); ip != "" {
		if net.ParseIP(ip) != nil {
			p.DNSClusterIP = ip
		} else {
			log.Info("Ignoring invalid annotation IP", "annotation", AnnotationDNSClusterIP, "value", ip)
		}
	}
	if p.DNSClusterIP == "" && r.DNSClusterIPOverride != "" {
		if net.ParseIP(r.DNSClusterIPOverride) != nil {
			p.DNSClusterIP = r.DNSClusterIPOverride
		} else {
			log.Info("Ignoring invalid operator override IP", "flag", "kubernetes-dns-cluster-ip", "value", r.DNSClusterIPOverride)
		}
	}
	if p.DNSClusterIP == "" {
		// Preserves the exact cached Get reconcileNetworkPolicy uses today.
		var svc corev1.Service
		if err := r.Get(ctx, types.NamespacedName{Namespace: "kube-system", Name: "kube-dns"}, &svc); err == nil {
			if ip := strings.TrimSpace(svc.Spec.ClusterIP); ip != "" && ip != "None" && net.ParseIP(ip) != nil {
				p.DNSClusterIP = ip
			}
		} else if !apierrors.IsNotFound(err) {
			log.Info("Failed to discover kube-dns ClusterIP; falling back to default", "error", err)
		}
	}
	if p.DNSClusterIP == "" {
		p.DNSClusterIP = defaultDNSClusterIP
	}

	// --- Metadata daemon IP (always present; overridable IP, never omitted in B2) ---
	if ip := trimmedAnnotation(agent, AnnotationMetadataDaemonIP); ip != "" {
		if net.ParseIP(ip) != nil {
			p.MetadataDaemonIP = ip
		} else {
			log.Info("Ignoring invalid annotation IP", "annotation", AnnotationMetadataDaemonIP, "value", ip)
		}
	}
	if p.MetadataDaemonIP == "" && r.MetadataDaemonIPOverride != "" {
		if net.ParseIP(r.MetadataDaemonIPOverride) != nil {
			p.MetadataDaemonIP = r.MetadataDaemonIPOverride
		} else {
			log.Info("Ignoring invalid operator override IP", "flag", "kubernetes-metadata-daemon-ip", "value", r.MetadataDaemonIPOverride)
		}
	}
	if p.MetadataDaemonIP == "" {
		p.MetadataDaemonIP = metadataDaemonIP // existing const, 169.254.169.252
	}

	return p
}

func trimmedAnnotation(agent *agentv1alpha1.PlatformAgent, key string) string {
	if agent == nil || agent.Annotations == nil {
		return ""
	}
	return strings.TrimSpace(agent.Annotations[key])
}
