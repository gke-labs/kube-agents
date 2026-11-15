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

// The audit-report store's broker-only listener (#5619), under mode: next.
//
// A profile worker runs in a pod with no PVC, so it reaches the fleet-audit
// report store (docs/designs/fleet-audit-report-store.md) through the
// credential broker, and the broker reaches the store over the network: an
// HTTP listener (agents/platform/scripts/audit_report_store.py) on port 8700
// of the gateway pod, over the tree audit_report.py writes on the agent PVC.
//
// What this file renders, all of it next mode only:
//
//   - on the gateway pod's agent-api-auth container, the switch that starts
//     the listener (start-services.sh), the subject it accepts, and the port.
//     That container, not platform-agent: it already mounts the PVC and the
//     pod's API-audience token (event-watcher-ksa-token), which the listener
//     reads the cluster JWKS with, and platform-agent is held to audience-bound
//     tokens (docs/security-requirements.md). No new RBAC: the default
//     system:service-account-issuer-discovery binding lets every
//     ServiceAccount read the JWKS;
//   - on the broker, a projected token bound to auditReportStoreAudience,
//     which it presents to the listener, and the listener's address. The
//     broker reads neither yet; its routes are the next change;
//   - the Service <agent>-report-store, and the NetworkPolicy of the same
//     name that admits only the broker's pods on the port.
//
// Today mode renders none of it, and a flip back to today deletes the two
// objects (owned, so an older operator's garbage collection takes them with
// the CR too). The data stays on the PVC either way.

import (
	"context"
	"fmt"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// auditReportStorePort is the listener's port. start-services.sh binds the
	// same number (AUDIT_REPORT_STORE_PORT); Session KV keeps 8699 on loopback.
	auditReportStorePort     = int32(8700)
	auditReportStorePortName = "report-store"
	auditReportStoreSuffix   = "-report-store"
	// auditReportStoreAudience is the one audience the listener accepts
	// (audit_report_store.AUDIENCE). Its own, so the broker's token for the
	// store passes nowhere else, and no other token passes at the store.
	auditReportStoreAudience = "kubeagents-audit-report-store"
	// The broker's projected token for the store, and where it is mounted.
	// Named without credential words, which gosec's G101 reads as a
	// hardcoded credential.
	auditReportStoreTokenVolume    = "audit-report-store-ksa"
	auditReportStoreTokenMountPath = "/var/run/secrets/kubeagents/audit-report-store"

	// On agent-api-auth: the switch start-services.sh reads, and the one
	// subject the listener accepts.
	auditReportStoreEnabledEnv        = "AUDIT_REPORT_STORE_ENABLED"
	auditReportStoreAllowedSubjectEnv = "AUDIT_REPORT_STORE_ALLOWED_SUBJECT"
	// On the broker: where the listener is, and the token it presents.
	auditReportStoreURLEnv       = "AUDIT_REPORT_STORE_URL"
	auditReportStoreTokenFileEnv = "AUDIT_REPORT_STORE_TOKEN_FILE" // #nosec G101 -- Environment variable name, not a credential
)

// auditReportStoreEnvNames are reserved in mergeCredentialProxyEnv in every
// mode: rendered only under next, a CR could otherwise set them on a today
// install and start a listener nobody declared, or point the broker's token at
// a listener of its choosing.
var auditReportStoreEnvNames = []string{
	auditReportStoreEnabledEnv,
	auditReportStoreAllowedSubjectEnv,
	auditReportStoreURLEnv,
	auditReportStoreTokenFileEnv,
}

// auditReportStoreRendered is whether the listener and its objects render:
// mode next. Fail-closed on version skew (renderMode), where the pod drops the
// listener and the reconcile leaves the two objects as they are.
func auditReportStoreRendered(agent *agentv1alpha1.PlatformAgent) bool {
	return renderMode(agent, "audit-report-store") == ModeNext
}

func auditReportStoreName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + auditReportStoreSuffix
}

// auditReportStoreURL is the address the broker dials.
func auditReportStoreURL(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("http://%s.%s.svc:%d", auditReportStoreName(agent), agent.Namespace, auditReportStorePort)
}

// auditReportStoreAllowedSubject is the broker's ServiceAccount as a token
// subject: the only caller the listener accepts.
func auditReportStoreAllowedSubject(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("system:serviceaccount:%s:%s", agent.Namespace, agentServiceAccountName(agent))
}

// auditReportStoreSidecarEnv is the listener's switch and subject on
// agent-api-auth, or nothing outside next mode.
func auditReportStoreSidecarEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	if !auditReportStoreRendered(agent) {
		return nil
	}
	return []corev1.EnvVar{
		{Name: auditReportStoreEnabledEnv, Value: "1"},
		{Name: auditReportStoreAllowedSubjectEnv, Value: auditReportStoreAllowedSubject(agent)},
	}
}

// auditReportStoreSidecarPorts is the listener's port on agent-api-auth.
func auditReportStoreSidecarPorts(agent *agentv1alpha1.PlatformAgent) []corev1.ContainerPort {
	if !auditReportStoreRendered(agent) {
		return nil
	}
	return []corev1.ContainerPort{{Name: auditReportStorePortName, ContainerPort: auditReportStorePort}}
}

// auditReportStoreBrokerVolumes is the broker's projected token for the
// store: audience-bound, so the API server does not accept it as a client
// credential and the store accepts nothing else. The exec tier drops it
// (credential_proxy_exec_tier.go): an exec pod runs brokered commands and
// never calls the store.
func auditReportStoreBrokerVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	if !auditReportStoreRendered(agent) {
		return nil
	}
	return []corev1.Volume{{Name: auditReportStoreTokenVolume, VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
		DefaultMode: ptr.To(int32(0400)),
		Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
			Audience: auditReportStoreAudience, ExpirationSeconds: ptr.To(int64(3600)), Path: "token",
		}}},
	}}}}
}

func auditReportStoreBrokerMounts(agent *agentv1alpha1.PlatformAgent) []corev1.VolumeMount {
	if !auditReportStoreRendered(agent) {
		return nil
	}
	return []corev1.VolumeMount{{Name: auditReportStoreTokenVolume, MountPath: auditReportStoreTokenMountPath, ReadOnly: true}}
}

func auditReportStoreBrokerEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	if !auditReportStoreRendered(agent) {
		return nil
	}
	return []corev1.EnvVar{
		{Name: auditReportStoreURLEnv, Value: auditReportStoreURL(agent)},
		{Name: auditReportStoreTokenFileEnv, Value: auditReportStoreTokenMountPath + "/token"},
	}
}

// buildAuditReportStoreService fronts the listener on the gateway pod.
func buildAuditReportStoreService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	svc := &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: auditReportStoreName(agent), Namespace: agent.Namespace},
		Spec: corev1.ServiceSpec{
			Type:     corev1.ServiceTypeClusterIP,
			Selector: map[string]string{"app": agent.Name + "-gateway"},
			Ports: []corev1.ServicePort{{
				Name:       auditReportStorePortName,
				Protocol:   corev1.ProtocolTCP,
				Port:       auditReportStorePort,
				TargetPort: intstr.FromString(auditReportStorePortName),
			}},
		},
	}
	withCommonLabels(svc, agent)
	return svc
}

// buildAuditReportStoreNetworkPolicy admits the broker's pods, and only them,
// to the listener's port on the gateway pod, and changes nothing else about
// what reaches that pod.
//
// NetworkPolicies selecting one pod are additive: the pod accepts the union
// of what they allow, and is isolated for ingress as soon as any one of them
// lists Ingress. There is no deny rule; denying is not allowing. So what this
// policy must say depends on whether another policy already isolates the pod
// (gatewayPodIngressIsolated, which reads every policy in the namespace: the
// operator's own and anybody else's):
//
//   - isolated (with spec.networkPolicy on, the default, <agent>-gateway-netpol
//     always is one; a policy a customer applied can be another): then this
//     policy is rule (a) alone, the one path added to their isolation. A rule
//     from anywhere here would be added to their allowlists and reopen every
//     port they closed, to every source.
//   - not isolated: nothing selects the pod for ingress, and the egress-only
//     policy beside it (buildAgentEgressNetworkPolicy) says why it lists no
//     Ingress: doing so cuts the agent's API Service and everything else
//     nobody enumerated. Rule (a) alone would do exactly that. So rule (b)
//     admits every source on every port but 8700 (TCP 1-8699 and 8701-65535,
//     and all of UDP), which keeps the pod as open as it was, with 8700 held
//     to the broker.
//
// Either way the listener also verifies the broker's token (sub, aud, iss), so
// a cluster whose CNI does not enforce NetworkPolicy still admits nobody else.
// Egress is not listed, so the gateway's egress policies are untouched.
func buildAuditReportStoreNetworkPolicy(agent *agentv1alpha1.PlatformAgent, gatewayIngressIsolated bool) *networkingv1.NetworkPolicy {
	rules := []networkingv1.NetworkPolicyIngressRule{
		// (a) The broker's pods, by the selector its Deployment's pods carry
		// (credentialProxySelector), on the listener's port. The exec tier's
		// pods carry another app label and are not admitted.
		{
			From: []networkingv1.NetworkPolicyPeer{{
				PodSelector: &metav1.LabelSelector{MatchLabels: credentialProxySelector(agent)},
			}},
			Ports: []networkingv1.NetworkPolicyPort{tcpPort(auditReportStorePort)},
		},
	}
	if !gatewayIngressIsolated {
		// (b) Everything the pod accepted before this policy existed, with
		// 8700 cut out of the TCP range. No From: every source.
		rules = append(rules, networkingv1.NetworkPolicyIngressRule{
			Ports: []networkingv1.NetworkPolicyPort{
				portRange(corev1.ProtocolTCP, 1, auditReportStorePort-1),
				portRange(corev1.ProtocolTCP, auditReportStorePort+1, 65535),
				portRange(corev1.ProtocolUDP, 1, 65535),
			},
		})
	}
	np := &networkingv1.NetworkPolicy{
		TypeMeta:   metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{Name: auditReportStoreName(agent), Namespace: agent.Namespace},
		Spec: networkingv1.NetworkPolicySpec{
			// The gateway pod, the selector the gateway policies use.
			PodSelector: metav1.LabelSelector{MatchLabels: map[string]string{"app": agent.Name + "-gateway"}},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
			Ingress:     rules,
		},
	}
	withCommonLabels(np, agent)
	return np
}

func portRange(protocol corev1.Protocol, from, to int32) networkingv1.NetworkPolicyPort {
	return networkingv1.NetworkPolicyPort{Protocol: ptr.To(protocol), Port: ptr.To(intstr.FromInt32(from)), EndPort: ptr.To(to)}
}

// reconcileAuditReportStore applies the Service and the NetworkPolicy under
// next, and deletes them (owned only) under today. On version skew it does
// neither, as the A2A stack's mode gate does: modeOK is false there.
//
// The policy's shape is recomputed on every pass from the policies in the
// namespace, so one that starts isolating the gateway pod drops rule (b) on
// the next reconcile. The NetworkPolicy watch in SetupWithManager enqueues
// the namespace's agents when any ingress-isolating policy changes, which
// makes that the pass right after it. The known limit is the window between
// the foreign policy landing and that pass applying: for those seconds rule
// (b) is in the union with it.
func (r *PlatformAgentReconciler) reconcileAuditReportStore(ctx context.Context, agent *agentv1alpha1.PlatformAgent, modeOK bool) error {
	if !modeOK {
		return nil
	}
	if !auditReportStoreRendered(agent) {
		if err := r.deleteAuditReportStoreObject(ctx, agent, &corev1.Service{}); err != nil {
			return err
		}
		return r.deleteAuditReportStoreObject(ctx, agent, &networkingv1.NetworkPolicy{})
	}
	gatewayIngressIsolated, err := r.gatewayPodIngressIsolated(ctx, agent)
	if err != nil {
		return err
	}
	for _, obj := range []client.Object{buildAuditReportStoreService(agent), buildAuditReportStoreNetworkPolicy(agent, gatewayIngressIsolated)} {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return fmt.Errorf("failed to set controller reference on audit-report store %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply audit-report store %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
	}
	return nil
}

// policyIsolatesIngress is whether a NetworkPolicy isolates the pods it
// selects for ingress. An empty policyTypes is read as the API server defaults
// it: Ingress always, whatever the policy lists.
func policyIsolatesIngress(np *networkingv1.NetworkPolicy) bool {
	if len(np.Spec.PolicyTypes) == 0 {
		return true
	}
	for _, t := range np.Spec.PolicyTypes {
		if t == networkingv1.PolicyTypeIngress {
			return true
		}
	}
	return false
}

// gatewayPodIngressIsolated is whether any NetworkPolicy in the namespace but
// the store's own selects the gateway pod and isolates it for ingress.
//
// The pod's labels are the live gateway Deployment's template labels, or the
// operator's own if the Deployment is not there yet, plus the leader label,
// which leader_elect.py stamps on one pod above one replica. Counting a
// policy that selects only the leader as isolating errs toward rule (a)
// alone: a non-leader pod could then lose ingress it had, where the other way
// round every port on the leader would open.
func (r *PlatformAgentReconciler) gatewayPodIngressIsolated(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (bool, error) {
	podLabels := commonLabels(agent)
	podLabels["app"] = agent.Name + "-gateway"
	var gateway appsv1.Deployment
	if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}, &gateway); err == nil {
		for k, v := range gateway.Spec.Template.Labels {
			podLabels[k] = v
		}
	} else if !apierrors.IsNotFound(err) {
		return false, fmt.Errorf("failed to read the gateway Deployment for the audit-report store policy: %w", err)
	}
	podLabels[gatewayLeaderLabel] = gatewayLeaderLabelValue
	var policies networkingv1.NetworkPolicyList
	if err := r.List(ctx, &policies, client.InNamespace(agent.Namespace)); err != nil {
		return false, fmt.Errorf("failed to list NetworkPolicies for the audit-report store policy: %w", err)
	}
	for i := range policies.Items {
		np := &policies.Items[i]
		if np.Name == auditReportStoreName(agent) || !policyIsolatesIngress(np) {
			continue
		}
		selector, err := metav1.LabelSelectorAsSelector(&np.Spec.PodSelector)
		if err != nil {
			// The API server refuses a selector this cannot parse, so this
			// is a policy that selects nothing.
			continue
		}
		if selector.Matches(labels.Set(podLabels)) {
			return true, nil
		}
	}
	return false, nil
}

// deleteAuditReportStoreObject deletes the store's object of obj's kind, read first and
// deleted only when this agent controls it: a same-named object someone else
// created is not the operator's to remove.
func (r *PlatformAgentReconciler) deleteAuditReportStoreObject(ctx context.Context, agent *agentv1alpha1.PlatformAgent, obj client.Object) error {
	key := types.NamespacedName{Namespace: agent.Namespace, Name: auditReportStoreName(agent)}
	if err := r.Get(ctx, key, obj); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		return fmt.Errorf("failed to get audit-report store %T %s: %w", obj, key, err)
	}
	if !metav1.IsControlledBy(obj, agent) {
		return nil
	}
	if err := r.Delete(ctx, obj); err != nil && !apierrors.IsNotFound(err) {
		return fmt.Errorf("failed to delete audit-report store %T %s: %w", obj, key, err)
	}
	return nil
}
