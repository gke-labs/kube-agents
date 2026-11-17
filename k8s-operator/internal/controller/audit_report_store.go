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

// The audit-report store's broker-only listener (#5619).
//
// A profile worker runs in a pod with no PVC, so it reaches the fleet-audit
// report store (docs/designs/fleet-audit-report-store.md) through the
// credential broker, and the broker reaches the store over the network: an
// HTTP listener (agents/platform/scripts/audit_report_store.py) on port 8700
// of the gateway pod, over the tree audit_report.py writes on the agent PVC.
//
// What this file renders, with the A2A stack on every install:
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
//     which it presents to the listener, the listener's address, and the
//     profiles whose tasks may use the store (auditReportProfilesEnv). The
//     broker's /v1/audit-reports routes read all three;
//   - the Service <agent>-report-store;
//   - in <agent>-gateway-netpol, one more ingress rule: the broker's pods to
//     the port (auditReportStoreIngressRule). A rule in the gateway policy
//     rather than a policy of its own, so an operator rollback, whose render
//     of that policy lacks the rule, drops it with no orphan left isolating
//     the pod, and spec.networkPolicy.enabled false takes it with the policy.
//
// There is no mode gate: spec.mode is retired and the A2A stack renders on
// every install (mode_migration.go), so the store does too. A rollback to an
// operator before this change leaves the Service behind: it is inert, since
// nothing listens on 8700 without this change. The data stays on the PVC.

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// auditReportStorePort is the listener's port, the number
	// audit_report_store.py binds (its LISTEN_PORT); Session KV keeps 8699 on
	// loopback.
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
	// And the name prefix of the broker Deployment's pods, `<agent>-credential-proxy-`:
	// the broker's ServiceAccount is shared with the exec tier and kube-proxy,
	// so the subject alone is not the broker. The listener
	// (audit_report_store.is_broker_pod_name) accepts the token's
	// kubernetes.io.pod.name claim only if it is (prefix + hash + "-")[:58]
	// plus 5 random characters, the API server's generateName cut, so long
	// agent names still pass; and it refuses a name whose remainder after the
	// prefix begins with "exec", the exec tier's pods, at every length where
	// the tier can exist (credentialProxyExecTierMaxAgentName).
	auditReportStoreAllowedPodPrefixEnv = "AUDIT_REPORT_STORE_ALLOWED_POD_PREFIX"
	// On the broker: where the listener is, and the token it presents.
	auditReportStoreURLEnv       = "AUDIT_REPORT_STORE_URL"
	auditReportStoreTokenFileEnv = "AUDIT_REPORT_STORE_TOKEN_FILE" // #nosec G101 -- Environment variable name, not a credential
	// And the AgentProfiles whose tasks may use the store through the broker
	// (credential_proxy.REPORT_PROFILES_ENV): a task's profile must be named
	// here to read, and to write with its schedule's trigger. Unset means no
	// profile task reaches the store.
	auditReportProfilesEnv = "CREDENTIAL_PROXY_REPORT_PROFILES"
	// The fleet-audit profile the builtin audits run on, and the platform
	// specialist (docs/designs/scheduling-on-profiles.md §1 item 4).
	auditReportProfiles = "fleet-audit,platform"
)

// auditReportStoreEnvNames are reserved in mergeCredentialProxyEnv: a CR
// could otherwise override them, start a listener with a subject of its
// choosing, or point the broker's token at a listener of its choosing.
//
// The last three are names the listener no longer reads, reserved anyway: an
// image or a hand edit that read them would let a CR move the store's root,
// or point the JWKS fetch at a server of its choosing with the pod's own token
// and have it vouch for any token it signs.
var auditReportStoreEnvNames = []string{
	auditReportStoreEnabledEnv,
	auditReportStoreAllowedSubjectEnv,
	auditReportStoreAllowedPodPrefixEnv,
	auditReportStoreURLEnv,
	auditReportStoreTokenFileEnv,
	auditReportProfilesEnv,
	"FLEET_AUDIT_REPORTS_DIR",
	"AUDIT_REPORT_STORE_API_SERVER",
	"AUDIT_REPORT_STORE_SA_DIR",
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
// agent-api-auth.
func auditReportStoreSidecarEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: auditReportStoreEnabledEnv, Value: "1"},
		{Name: auditReportStoreAllowedSubjectEnv, Value: auditReportStoreAllowedSubject(agent)},
		{Name: auditReportStoreAllowedPodPrefixEnv, Value: credentialProxyName(agent) + "-"},
	}
}

// auditReportStoreSidecarPorts is the listener's port on agent-api-auth.
func auditReportStoreSidecarPorts(agent *agentv1alpha1.PlatformAgent) []corev1.ContainerPort {
	return []corev1.ContainerPort{{Name: auditReportStorePortName, ContainerPort: auditReportStorePort}}
}

// auditReportStoreBrokerVolumes is the broker's projected token for the
// store: audience-bound, so the API server does not accept it as a client
// credential and the store accepts nothing else. The exec tier drops it
// (credential_proxy_exec_tier.go): an exec pod runs brokered commands and
// never calls the store.
func auditReportStoreBrokerVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	return []corev1.Volume{{Name: auditReportStoreTokenVolume, VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
		DefaultMode: ptr.To(int32(0400)),
		Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
			Audience: auditReportStoreAudience, ExpirationSeconds: ptr.To(int64(3600)), Path: "token",
		}}},
	}}}}
}

func auditReportStoreBrokerMounts(agent *agentv1alpha1.PlatformAgent) []corev1.VolumeMount {
	return []corev1.VolumeMount{{Name: auditReportStoreTokenVolume, MountPath: auditReportStoreTokenMountPath, ReadOnly: true}}
}

func auditReportStoreBrokerEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: auditReportStoreURLEnv, Value: auditReportStoreURL(agent)},
		{Name: auditReportStoreTokenFileEnv, Value: auditReportStoreTokenMountPath + "/token"},
		{Name: auditReportProfilesEnv, Value: auditReportProfiles},
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

// auditReportStoreIngressRule is the rule <agent>-gateway-netpol carries: the
// broker's pods, by the selector its Deployment's pods carry
// (credentialProxySelector), to the listener's port, and nothing else to it.
// The exec tier's pods carry another app label and are not admitted. The
// listener verifies the broker's token as well (audience, issuer, subject and
// the pod's name), which is the whole control where policies are off or not
// enforced.
func auditReportStoreIngressRule(agent *agentv1alpha1.PlatformAgent) networkingv1.NetworkPolicyIngressRule {
	return networkingv1.NetworkPolicyIngressRule{
		From: []networkingv1.NetworkPolicyPeer{{
			PodSelector: &metav1.LabelSelector{MatchLabels: credentialProxySelector(agent)},
		}},
		Ports: []networkingv1.NetworkPolicyPort{tcpPort(auditReportStorePort)},
	}
}

// reconcileAuditReportStore applies the Service. It also deletes an owned
// NetworkPolicy <agent>-report-store: an earlier build of this change rendered
// the store's rule as a policy of its own, which an install that ran it still
// has.
func (r *PlatformAgentReconciler) reconcileAuditReportStore(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	if err := r.deleteAuditReportStoreObject(ctx, agent, &networkingv1.NetworkPolicy{}); err != nil {
		return err
	}
	svc := buildAuditReportStoreService(agent)
	if err := ctrl.SetControllerReference(agent, svc, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on audit-report store Service %s/%s: %w", svc.Namespace, svc.Name, err)
	}
	if err := r.applyManaged(ctx, agent, svc); err != nil {
		return fmt.Errorf("failed to apply audit-report store Service %s/%s: %w", svc.Namespace, svc.Name, err)
	}
	return nil
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
