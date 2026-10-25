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
	"fmt"
	"path"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The MCP server as a credential-broker caller. Under its flag the MCP pod
// runs as a ServiceAccount of its own and presents a projected token minted
// for an audience of its own, and the broker confers the `mcp` role by that
// audience. Every piece here hangs off a2aMCPBrokerEnabled, so an install
// without the MCP server renders none of it: no audience on the broker, no
// extra caller, no extra ingress peer, and an mcp-audience token from anywhere
// is "another audience", 401.
//
// The pattern is the session pod's (credentialProxySessionAudience) and the
// A2A gateway's (credentialProxyA2AChatAudience): a projected token, an
// audience the broker names in its env, the caller's ServiceAccount on
// CREDENTIAL_PROXY_ALLOWED_CALLERS, and the caller's pod on the broker's
// ingress fence. Like the session pod's, the ServiceAccount is also bound to
// the audience both ways (CREDENTIAL_PROXY_MCP_CALLERS).
const (
	// credentialProxyMCPAudience is the MCP pod's audience. Spelled
	// identically in credential_proxy.py, whose default it is.
	credentialProxyMCPAudience = "kubeagents-credential-proxy-mcp" // #nosec G101 -- Token audience name, not a credential
	// credentialProxyMCPAudienceEnvVar names it to the broker.
	credentialProxyMCPAudienceEnvVar = "CREDENTIAL_PROXY_MCP_AUDIENCE" // #nosec G101 -- Environment variable name, not a credential
	// credentialProxyMCPCallersEnvVar binds the mcp role to the MCP pod's
	// ServiceAccount in both directions, as CREDENTIAL_PROXY_SESSION_CALLERS
	// does the session role: the broker refuses that account every audience
	// but the mcp one, and the mcp audience to every other caller. Unlike the
	// session binding, unset does not leave the audience alone deciding: the
	// broker confers the mcp role to nobody without it.
	credentialProxyMCPCallersEnvVar = "CREDENTIAL_PROXY_MCP_CALLERS" // #nosec G101 -- Environment variable name, not a credential

	// a2aMCPBrokerURLEnvVar and a2aMCPBrokerTokenFileEnvVar tell the MCP
	// binary (a2a/cmd/mcp) where the broker is and which token to present.
	// The watchers' pair (WATCHER_BROKER_URL, WATCHER_BROKER_TOKEN_FILE) is the
	// same shape under another prefix.
	a2aMCPBrokerURLEnvVar       = "A2A_MCP_BROKER_URL"
	a2aMCPBrokerTokenFileEnvVar = "A2A_MCP_BROKER_TOKEN_FILE" // #nosec G101 -- Env var name, not a credential

	a2aMCPBrokerTokenVolume   = "mcp-broker-token"                // #nosec G101 -- Volume name, not a credential
	a2aMCPBrokerTokenMountDir = "/var/run/secrets/a2a-mcp-broker" // #nosec G101 -- Mount path, not a credential
	a2aMCPBrokerTokenKey      = "token"                           // #nosec G101 -- Projected file name, not a credential
	// One hour, like every broker token.
	a2aMCPBrokerTokenTTLSeconds = 3600
)

// a2aMCPBrokerEnabled is the one predicate every MCP broker render asks: the
// MCP server's flag, on a mode-next install. The MCP Deployment itself renders
// only from reconcileA2A, which runs only under next; the broker renders in
// both modes, so it needs the mode spelled out or a today install carrying the
// flag would confer a role nobody can hold.
func a2aMCPBrokerEnabled(agent *agentv1alpha1.PlatformAgent) bool {
	return renderMode(agent, "a2a-mcp") == ModeNext && a2aMCPDoorEnabled()
}

// a2aMCPServiceAccountName is the MCP pod's own ServiceAccount: the identity
// the broker's TokenReview names, so that the namespace's default account,
// which any pod without one runs as, is never a broker caller.
func a2aMCPServiceAccountName(agent *agentv1alpha1.PlatformAgent) string {
	return a2aMCPName(agent)
}

// a2aMCPBrokerCaller is that ServiceAccount's TokenReview username.
func a2aMCPBrokerCaller(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("system:serviceaccount:%s:%s", agent.Namespace, a2aMCPServiceAccountName(agent))
}

// a2aMCPBrokerAccess is what the MCP container needs to call the broker: the
// projected token's volume, its mount, and the two variables naming the
// broker and the token. All three empty when a2aMCPBrokerEnabled is false.
func a2aMCPBrokerAccess(agent *agentv1alpha1.PlatformAgent) ([]corev1.Volume, []corev1.VolumeMount, []corev1.EnvVar) {
	if !a2aMCPBrokerEnabled(agent) {
		return nil, nil, nil
	}
	volumes := []corev1.Volume{{
		Name: a2aMCPBrokerTokenVolume,
		VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: ptr.To(int32(0400)),
			Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
				Audience:          credentialProxyMCPAudience,
				ExpirationSeconds: ptr.To(int64(a2aMCPBrokerTokenTTLSeconds)),
				Path:              a2aMCPBrokerTokenKey,
			}}},
		}},
	}}
	mounts := []corev1.VolumeMount{{Name: a2aMCPBrokerTokenVolume, MountPath: a2aMCPBrokerTokenMountDir, ReadOnly: true}}
	env := []corev1.EnvVar{
		{Name: a2aMCPBrokerURLEnvVar, Value: credentialProxyBaseURL(agent)},
		{Name: a2aMCPBrokerTokenFileEnvVar, Value: path.Join(a2aMCPBrokerTokenMountDir, a2aMCPBrokerTokenKey)},
	}
	return volumes, mounts, env
}

// withA2AMCPBrokerAccess applies a2aMCPBrokerAccess to the MCP pod: the
// ServiceAccount, the volume, and the mount and env on the MCP container. It
// takes and returns the PodSpec by value so buildA2AMCPDeployment can wrap its
// literal in one call. Disabled, the spec comes back unchanged.
//
// AutomountServiceAccountToken stays false: the projected volume is the only
// token in the pod, and it opens the broker's mcp routes and nothing else; the
// pod still never talks to the API server.
func withA2AMCPBrokerAccess(agent *agentv1alpha1.PlatformAgent, spec corev1.PodSpec) corev1.PodSpec {
	volumes, mounts, env := a2aMCPBrokerAccess(agent)
	if volumes == nil {
		return spec
	}
	spec.ServiceAccountName = a2aMCPServiceAccountName(agent)
	spec.Volumes = append(spec.Volumes, volumes...)
	containers := append([]corev1.Container{}, spec.Containers...)
	for i := range containers {
		if containers[i].Name != a2aMCPContainer {
			continue
		}
		containers[i].VolumeMounts = append(containers[i].VolumeMounts, mounts...)
		containers[i].Env = append(containers[i].Env, env...)
	}
	spec.Containers = containers
	return spec
}

// a2aMCPBrokerPeers is the MCP pod as a peer of the broker's ingress fence
// (buildCredentialProxyNetworkPolicy), on the credentialed port. Nothing when
// disabled, so the fence admits exactly what it did.
//
// There is no egress half: no NetworkPolicy selects the MCP pod for egress
// (its fence, buildA2AMCPNetworkPolicy, is ingress-only), so its egress is
// already open, as the A2A gateway's is for the same broker. An egress
// policy naming only the broker would default-deny the pod's DNS and bus
// traffic.
func a2aMCPBrokerPeers(agent *agentv1alpha1.PlatformAgent) []networkingv1.NetworkPolicyPeer {
	if !a2aMCPBrokerEnabled(agent) {
		return nil
	}
	return []networkingv1.NetworkPolicyPeer{{PodSelector: &metav1.LabelSelector{
		MatchLabels: map[string]string{"app": a2aMCPName(agent)},
	}}}
}

// a2aMCPBrokerEnv is the broker's side: the audience it confers the mcp role
// by, and the ServiceAccount bound to it both ways, only when enabled. The
// role comes from the audience and a pod picks the audience it projects, so
// without the binding a token minted for the MCP account with the shell
// audience would get the shell role.
func a2aMCPBrokerEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	if !a2aMCPBrokerEnabled(agent) {
		return nil
	}
	return []corev1.EnvVar{
		{Name: credentialProxyMCPAudienceEnvVar, Value: credentialProxyMCPAudience},
		{Name: credentialProxyMCPCallersEnvVar, Value: a2aMCPBrokerCaller(agent)},
	}
}

// newA2AMCPServiceAccount is the MCP pod's identity. It holds no RBAC: its
// token exists to be presented to the broker.
func newA2AMCPServiceAccount(agent *agentv1alpha1.PlatformAgent) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aMCPServiceAccountName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, a2aMCPComponent),
		},
	}
}

// reconcileA2AMCPBrokerIdentity applies the MCP pod's ServiceAccount when
// enabled and removes it when not. reconcileA2A runs it just ahead of
// reconcileA2AMCP, so the account exists before the pod that names it; on the
// way down it goes in the same pass as the Deployment, and a pod still
// terminating keeps the token it already holds for at most its hour. The flip
// to today sweeps it with the rest of the MCP server (cleanupA2A's list).
//
// An account under the reserved name that this PlatformAgent does not control
// is treated as removeA2AMCP and ensureA2AMCPToken treat the rest of the MCP
// objects. With the flag off it is skipped: it is not ours to remove, and this
// runs on every next reconcile, so refusing would stop the rest of it. With
// the flag on it is refused rather than adopted: server-side apply would keep
// every field another manager set, a Workload Identity annotation included,
// and the MCP pod would run as that identity.
func (r *PlatformAgentReconciler) reconcileA2AMCPBrokerIdentity(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	sa := newA2AMCPServiceAccount(agent)
	existing := &corev1.ServiceAccount{}
	err := r.Get(ctx, client.ObjectKeyFromObject(sa), existing)
	if err != nil && !apierrors.IsNotFound(err) {
		return fmt.Errorf("failed to read the MCP server's ServiceAccount: %w", err)
	}
	unowned := err == nil && !metav1.IsControlledBy(existing, agent)
	if !a2aMCPBrokerEnabled(agent) {
		if unowned {
			return nil
		}
		if err := r.deleteOwnedA2AObject(ctx, agent, sa, r.Client); err != nil {
			return fmt.Errorf("failed to remove the MCP server's ServiceAccount: %w", err)
		}
		return nil
	}
	if unowned {
		return fmt.Errorf("refusing to adopt unowned ServiceAccount %s/%s as the MCP server's identity; delete it, or give it a controller reference to this PlatformAgent", sa.Namespace, sa.Name)
	}
	if err := ctrl.SetControllerReference(agent, sa, r.Scheme); err != nil {
		return err
	}
	if err := r.applyManaged(ctx, agent, sa); err != nil {
		return fmt.Errorf("failed to apply the MCP server's ServiceAccount: %w", err)
	}
	return nil
}
