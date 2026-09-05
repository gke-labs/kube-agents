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

// The agent Pod's half of the credential boundary.
//
// The broker itself is rendered by credential_proxy_manifests.go — one pod, one
// builder, unconditionally. What is left here is what stays behind on the agent
// Pod once it moves: the audience-bound token the agent presents, the container
// that presents it, the ServiceAccount both Pods share, and the TokenReview
// grant the broker needs to check the token it is handed.
//
// The boundary itself is that the agent no longer shares a network namespace
// with the process holding the cloud credentials, so "reachable on 127.0.0.1"
// stops being an access-control mechanism. What it costs is that the broker
// call is a network call, which is why it cannot stand without the
// authentication in credential_proxy.py.

import (
	"fmt"
	"strings"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// credentialProxyAudience is the audience the shell sandbox's projected token
	// is minted for. A token for any other audience — including the Kubernetes
	// API's own — is refused, and this one is useless anywhere but the broker.
	credentialProxyAudience = "kubeagents-credential-proxy" // #nosec G101 -- Token audience name, not a credential

	// credentialProxyChatAudience is the same for the gateway Pod, and the two
	// being different is what lets the broker tell its two callers apart.
	//
	// It cannot do so any other way. Both Pods run as ServiceAccounts named on
	// CREDENTIAL_PROXY_ALLOWED_CALLERS, so the TokenReview username says only
	// that the caller was one of the two entitled to call — not which. The
	// audience is chosen here, per Pod, and the API server will not validate a
	// token against an audience it was not minted for, so it is a claim the
	// caller cannot restate.
	//
	// What it buys: the sandbox, where every model-authored command runs, cannot
	// reach /v1/chat/** at all, and the gateway cannot reach /v1/exec,
	// /v1/github/** or /v1/workspace/**. Neither needed the other's routes —
	// CREDENTIAL_PROXY_URL is empty on the gateway and the relay URLs are absent
	// from the sandbox — so this enforces a separation the deployment already
	// had and nothing checked. credential_proxy.py's ROUTE_ROLES is the table.
	credentialProxyChatAudience = "kubeagents-credential-proxy-chat" // #nosec G101 -- Token audience name, not a credential

	// credentialProxyTokenMountPath is where the agent container finds the
	// token it presents to the broker.
	credentialProxyTokenMountPath = "/var/run/secrets/kubeagents/credential-proxy" // #nosec G101 -- Mount path, not a credential

	// kubeAPIAccessMountPath is the conventional location of a Pod's
	// default-audience token and cluster CA. The broker reads both to make the
	// TokenReview call that verifies the agent's token.
	kubeAPIAccessMountPath = "/var/run/secrets/kubernetes.io/serviceaccount"

	agentCredentialProxyTokenVolume = "agent-credential-proxy-token" // #nosec G101 -- Volume name, not a credential
)

// credentialBrokerName is the name of the broker's Deployment and Service.
func credentialBrokerName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-credential-proxy"
}

// agentServiceAccountName is the ServiceAccount both Pods run as.
//
// They share one, and this is the weakest joint in the split: the identity the
// broker verifies is "a Pod running as this ServiceAccount", not "the agent
// Pod". Good enough to exclude everything else in the cluster, not good enough
// to tell the two halves of this agent apart.
//
// It shares one because both Pods are rendered from this one name and nothing
// here mints a second. Not because splitting them is impossible: the Workload
// Identity IAM binding names this ServiceAccount, so the way to split it is to
// leave the bound name with the *broker* — which is what actually needs the
// cloud credential once it is split out — and give the agent a new one of its
// own. That buys per-Pod attribution and takes the cloud credential away from
// the sandbox's identity at the same time. It also moves which identity holds
// the cloud credential, which is a bigger decision than a manifest change and
// belongs with the per-cluster service-account work rather than here.
func agentServiceAccountName(agent *agentv1alpha1.PlatformAgent) string {
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		return agent.Spec.Security.ServiceAccountName
	}
	return agent.Name
}

// credentialProxyBaseURL is where the agent Pod reaches the broker.
//
// Never loopback: the broker has a pod of its own. credentialProxyName and
// credentialBrokerName render the same string, so the gateway's relay clients
// and the sandbox's wrapped CLIs dial one Service between them.
func credentialProxyBaseURL(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("http://%s.%s.svc.cluster.local:%d",
		credentialBrokerName(agent), agent.Namespace, credentialProxyPort)
}

// allowedBrokerCallers is the value of CREDENTIAL_PROXY_ALLOWED_CALLERS: the
// TokenReview usernames the broker will serve.
//
// Two of them. The sandbox's is the caller that matters — the shell is where
// every credentialed command runs, so a broker that served only the agent's
// identity would answer 401 to all of them. The agent's is there because the
// gateway's chat relays go through the same listener from the agent Pod.
//
// Two identities rather than one is not a widening of who may call: both Pods
// belong to this agent, and neither could reach the broker without a token this
// namespace mints. What it does not give is a way to tell them apart — the
// sandbox runs as its own ServiceAccount, but the gateway shares the agent's
// with the broker, so a username alone cannot say which Pod called. That is
// what credentialProxyChatAudience is for.
func allowedBrokerCallers(agent *agentv1alpha1.PlatformAgent) string {
	return strings.Join([]string{
		fmt.Sprintf("system:serviceaccount:%s:%s", agent.Namespace, agentServiceAccountName(agent)),
		fmt.Sprintf("system:serviceaccount:%s:%s", agent.Namespace, shellSandboxServiceAccountName(agent)),
	}, ",")
}

// buildAgentCredentialProxyTokenVolume projects the token the agent presents to
// the broker. Audience-bound and one hour long, so a copy that escapes the Pod
// is worth an hour of broker access and nothing else.
//
// credentialProxyChatAudience, not credentialProxyAudience: the chat relays are
// the only thing in this Pod that calls the broker, and minting for the chat
// audience is what stops this token opening the shell's routes if it does
// escape. The sandbox's equivalent is in buildShellSandboxTokenVolume.
func buildAgentCredentialProxyTokenVolume() corev1.Volume {
	return corev1.Volume{
		Name: agentCredentialProxyTokenVolume,
		VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: ptr.To(int32(0400)),
			Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
				Audience: credentialProxyChatAudience, ExpirationSeconds: ptr.To(int64(3600)), Path: "token",
			}}},
		}},
	}
}

// buildCredentialBrokerTokenReviewRole lets the broker verify the tokens its
// callers present.
//
// One verb on one virtual resource. Creating a TokenReview grants no read
// access to anything and cannot be used to mint a token — it only answers
// "is this token valid, and for whom", which is the whole of what the broker
// needs. Deliberately not system:auth-delegator, which also carries
// SubjectAccessReview.
//
// The cost, named rather than hidden: the binding names the ServiceAccount the
// agent Pod also runs as, so wherever this is applied the agent can validate any
// bearer token it gets hold of. It has no use for that. Narrowing it means
// giving the Pods separate ServiceAccounts — see agentServiceAccountName —
// which is why this is a cost of moving the broker rather than something the
// move can fix on its own. reconcileCredentialBrokerTokenReviewRBAC applies it
// on every install, because the broker is always off the agent's Pod.
func buildCredentialBrokerTokenReviewRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.ClusterRole {
	return &rbacv1.ClusterRole{
		TypeMeta: metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "ClusterRole"},
		ObjectMeta: metav1.ObjectMeta{
			Name: fmt.Sprintf("kubeagents:tokenreview:%s:%s", agent.Namespace, agent.Name),
		},
		Rules: []rbacv1.PolicyRule{{
			APIGroups: []string{"authentication.k8s.io"},
			Resources: []string{"tokenreviews"},
			Verbs:     []string{"create"},
		}},
	}
}
