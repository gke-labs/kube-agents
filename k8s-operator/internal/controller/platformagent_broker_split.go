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
// that presents it, the gateway's own ServiceAccount (the broker keeps the
// annotated one), and the TokenReview grant the broker needs to check the token
// it is handed.
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

	// credentialProxyA2AChatAudience is the third audience: the A2A gateway's
	// relay token, minted for it when the operator arms the gateway's Google
	// Chat backend under mode: next. Its own audience because the broker
	// confers roles by audience and the a2a-chat role must not be reachable
	// from the legacy chat caller: that caller is the LLM-driven Hermes pod,
	// and a shared role would let a prompt-injected agent pull and ack the
	// A2A gateway's events (spec-chatops-gateway.md, "The Google Chat
	// adapter"). credential_proxy.py refuses to confer the role when this
	// equals either audience above.
	credentialProxyA2AChatAudience = "kubeagents-credential-proxy-a2a-chat" // #nosec G101 -- Token audience name, not a credential
	// credentialProxySessionAudience is the session pod's, under
	// a2aSessionClusterViewEnvVar. A third audience for the same reason as
	// the second: the session ServiceAccount is on the callers list, so only
	// the audience can tell a session token from the shell's, and the
	// broker's `session` role (exec only, kubectl and gcloud only) hangs off
	// it. Spelled identically in credential_proxy.py and a2a/gateway/spawn.go.
	credentialProxySessionAudience = "kubeagents-credential-proxy-session" // #nosec G101 -- Token audience name, not a credential

	// credentialProxyTokenMountPath is where the agent container finds the
	// token it presents to the broker.
	credentialProxyTokenMountPath = "/var/run/secrets/kubeagents/credential-proxy" // #nosec G101 -- Mount path, not a credential

	// kubeAPIAccessMountPath is the conventional location of a Pod's
	// default-audience token and cluster CA. The broker reads both to make the
	// TokenReview call that verifies the agent's token.
	kubeAPIAccessMountPath = "/var/run/secrets/kubernetes.io/serviceaccount"

	agentCredentialProxyTokenVolume = "agent-credential-proxy-token" // #nosec G101 -- Volume name, not a credential

	// credentialProxyWatcherAudience is the gateway's watchers' audience: the
	// k8s-event-watcher and the drift-detector in agent-api-auth make their
	// Google calls through the broker under the `watcher` role. Its own
	// audience because the role is conferred by audience, and its token is
	// projected into agent-api-auth only, so the Hermes container beside it
	// holds the chat token and cannot reach these routes. Spelled identically
	// in credential_proxy.py.
	credentialProxyWatcherAudience = "kubeagents-credential-proxy-watcher" // #nosec G101 -- Token audience name, not a credential
	// watcherTokenVolume and watcherTokenMountPath carry that token.
	watcherTokenVolume    = "watcher-credential-proxy-token"            // #nosec G101 -- Volume name, not a credential
	watcherTokenMountPath = "/var/run/secrets/kubeagents/watcher-proxy" // #nosec G101 -- Mount path, not a credential
	// watcherBrokerURLEnvVar and watcherBrokerTokenFileEnvVar tell the two
	// watchers where the broker is and which token to present; the Go side
	// reads them in internal/clusterprofiles (BrokerFromEnv).
	watcherBrokerURLEnvVar       = "WATCHER_BROKER_URL"
	watcherBrokerTokenFileEnvVar = "WATCHER_BROKER_TOKEN_FILE" // #nosec G101 -- Env var name, not a credential
	// watcherSubscriptionEnvVar is the broker's copy of the drift detector's
	// subscription, fully qualified: the one Pub/Sub target its watcher routes
	// reach.
	watcherSubscriptionEnvVar = "WATCHER_PUBSUB_SUBSCRIPTION"
	// driftDetectorDefaultSubscription is the detector's own default, the
	// subscription the drift-pubsub module creates (cmd/drift-detector/main.go
	// defaultSubscriptionName). tests/conformance holds the two equal.
	driftDetectorDefaultSubscription = "platform-agent-drift-audit-sub"

	// mcpRelayURLEnvVar names the broker's remote MCP relay for the agent
	// container. The agent configs interpolate it into each mcp-remote
	// server's URL (`${KUBE_AGENTS_MCP_RELAY_URL}/gke`), so every
	// `mcp__gke__*` call reaches Google through the broker's tool allowlist
	// rather than on the gateway's own credential.
	mcpRelayURLEnvVar = "KUBE_AGENTS_MCP_RELAY_URL"
	// mcpRelayPath is the relay's route on the broker, without the trailing
	// slash the server name follows. credential_proxy.py takes the prefix from
	// mcp_policy.MCP_RELAY_PREFIX; tests/conformance holds the two equal.
	mcpRelayPath = "/v1/mcp"
)

// mcpRelayURL is the value of mcpRelayURLEnvVar.
func mcpRelayURL(agent *agentv1alpha1.PlatformAgent) string {
	return credentialProxyBaseURL(agent) + mcpRelayPath
}

// credentialBrokerName is the name of the broker's Deployment and Service.
func credentialBrokerName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-credential-proxy"
}

// agentServiceAccountName is the credential broker's ServiceAccount: the name
// the Workload Identity IAM binding (and spec.security.serviceAccountName,
// and its annotations) refers to.
//
// The gateway Pod used to run as it too, which was the weakest joint in the
// split: the identity the broker verified was "a Pod running as this
// ServiceAccount", and the model's Pod resolved to the install's Google service
// account. The split this comment used to propose is done: the bound name stays
// with the broker, the one Pod that needs the cloud credential, and the gateway
// runs as gatewayServiceAccountName, minted with no annotation.
func agentServiceAccountName(agent *agentv1alpha1.PlatformAgent) string {
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		return agent.Spec.Security.ServiceAccountName
	}
	return agent.Name
}

// gatewayServiceAccountSuffix makes the gateway Pod's own ServiceAccount name.
// It ends in `-agent` because the kube-agents-agent-binding-scope admission
// policy selects the bindings it governs by that suffix
// (config/admission/agent-rbac-policy.yaml, binds-agent-sa).
const gatewayServiceAccountSuffix = "-gateway-agent"

// gatewayServiceAccountName is the ServiceAccount the gateway Pod runs as.
//
// The split agentServiceAccountName's comment describes, done the way it
// describes: the Workload Identity-bound name stays with the broker, the one
// Pod that needs a Google credential, and the gateway gets a ServiceAccount of
// its own that the operator mints with no annotation. GKE resolves Workload
// Identity by the Pod's ServiceAccount, so the gateway's metadata server then
// has no Google service account to answer for, and its NetworkPolicy no longer
// opens the metadata path at all.
//
// Always operator-owned, whatever spec.security.serviceAccountName says: that
// field names the identity that carries the cloud binding, which is now the
// broker's alone.
func gatewayServiceAccountName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + gatewayServiceAccountSuffix
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
// Two of them on every install; a third when the next stack takes Chat, and a
// fourth under the cluster-view flag. The sandbox's is the caller that matters —
// the shell is where every credentialed command runs, so a broker that served
// only the gateway's identity would answer 401 to all of them. The gateway's
// is there because its chat relays, its Pub/Sub platform adapter and its
// watchers go through the same listener.
//
// Two identities rather than one is not a widening of who may call: both Pods
// belong to this agent, and neither could reach the broker without a token this
// namespace mints. Each runs as a ServiceAccount of its own, so a username
// says which Pod called; the audience (credentialProxyChatAudience and its
// siblings) says which role the call is for. The A2A gateway is told apart
// the same way.
func allowedBrokerCallers(agent *agentv1alpha1.PlatformAgent, mcpIdentityOwned bool) string {
	callers := []string{
		// The gateway Pod, which presents the chat and watcher tokens. The
		// broker's own ServiceAccount is not a caller: the broker serves these
		// routes, it does not call them.
		fmt.Sprintf("system:serviceaccount:%s:%s", agent.Namespace, gatewayServiceAccountName(agent)),
		fmt.Sprintf("system:serviceaccount:%s:%s", agent.Namespace, shellSandboxServiceAccountName(agent)),
	}
	// The A2A gateway, only while it is a caller: its Google Chat adapter
	// pulls the relay hosted here, and a next install that gives it no Chat
	// backend has no reason to widen the broker's callers by one.
	if a2aChatArmed(agent) {
		callers = append(callers, fmt.Sprintf("system:serviceaccount:%s:%s", agent.Namespace, a2aGatewayName(agent)))
	}
	// The third caller exists only under the cluster-view flag. It is
	// held to the session role by CREDENTIAL_PROXY_SESSION_CALLERS, which
	// buildCredentialProxyEnv renders beside it: the broker refuses this
	// ServiceAccount every audience but the session one, and the session
	// audience to every other caller.
	if a2aSessionClusterViewEnabled(agent) {
		callers = append(callers, a2aSessionBrokerCaller(agent))
	}
	// The MCP server's own ServiceAccount, under its flag
	// (a2aMCPBrokerEnabled) and only while this PlatformAgent owns the
	// account (a2aMCPIdentityOwned), never the namespace default it ran as
	// before. Held to the mcp role by CREDENTIAL_PROXY_MCP_CALLERS, which
	// a2aMCPBrokerEnv renders beside it, the same way the session caller is.
	if a2aMCPBrokerEnabled(agent) && mcpIdentityOwned {
		callers = append(callers, a2aMCPBrokerCaller(agent))
	}
	return strings.Join(callers, ",")
}

// a2aSessionBrokerCaller is the TokenReview username of the session pods'
// ServiceAccount, as the broker sees it.
func a2aSessionBrokerCaller(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("system:serviceaccount:%s:%s", agent.Namespace, a2aSessionServiceAccountName(agent))
}

// buildAgentCredentialProxyTokenVolume projects the token the agent presents to
// the broker. Audience-bound and one hour long, so a copy that escapes the Pod
// is worth an hour of broker access and nothing else.
//
// credentialProxyChatAudience, not credentialProxyAudience: the chat relays,
// the Pub/Sub platform adapter's relay and the remote MCP servers' mcp-remote
// children are the only things in this Pod that call the broker, and minting for the chat audience is what stops this
// token opening the shell's routes if it does escape. The sandbox's equivalent is in buildShellSandboxTokenVolume.
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

// buildWatcherTokenVolume projects the token the gateway's watchers present to
// the broker, for the watcher audience. Mounted into agent-api-auth only.
func buildWatcherTokenVolume() corev1.Volume {
	return corev1.Volume{
		Name: watcherTokenVolume,
		VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: ptr.To(int32(0400)),
			Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
				Audience: credentialProxyWatcherAudience, ExpirationSeconds: ptr.To(int64(3600)), Path: "token",
			}}},
		}},
	}
}

// watcherPubsubSubscription is the drift detector's subscription as the broker
// pins it: `projects/<p>/subscriptions/<s>`. The detector takes a bare name or a
// qualified one, and defaults the name when the CR gives none
// (cmd/drift-detector/main.go defaultSubscriptionName); this resolves all three
// the same way the detector does.
func watcherPubsubSubscription(agent *agentv1alpha1.PlatformAgent) string {
	name := driftDetectorSubscription(agent)
	if strings.HasPrefix(name, "projects/") {
		return name
	}
	if name == "" {
		name = driftDetectorDefaultSubscription
	}
	project, _, _ := driftDetectorHarness(agent)
	return fmt.Sprintf("projects/%s/subscriptions/%s", project, name)
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
// The binding names the broker's ServiceAccount, which the gateway Pod no longer
// runs as (gatewayServiceAccountName), so the agent cannot use it to validate a
// bearer token it gets hold of. reconcileCredentialBrokerTokenReviewRBAC applies
// it on every install, because the broker is always off the agent's Pod.
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

// gatewaySubject is the gateway Pod's ServiceAccount as an RBAC subject.
func gatewaySubject(agent *agentv1alpha1.PlatformAgent) rbacv1.Subject {
	return rbacv1.Subject{Kind: "ServiceAccount", Name: gatewayServiceAccountName(agent), Namespace: agent.Namespace}
}

// watcherTokenSourceNames is the set of user-authored volume names that would
// hand a container the watchers' broker token: the reserved name itself, and
// any volume on either of the CR's volume lists that projects a
// serviceAccountToken for the watcher audience under another name. The broker
// confers the `watcher` role on the audience alone -- every fleet cluster's
// events and named objects, and the drift subscription -- and the operator
// projects that token into agent-api-auth and nowhere else, so a CR container
// mounting either kind is a second holder of the role. buildPodTemplateSpec
// strips those volumes, and every mount naming them, from all five
// user-authored surfaces: sidecars, initContainers, sidecarVolumes,
// extraVolumes and (in buildBaseContainers) extraVolumeMounts.
//
// The same two halves, name and source, as the A2A bus token's reservation
// (a2aBusCredentialVolumeNames), and on the same terms: a guard against a
// misconfiguration by the CR's author, who is the platform operator, not a
// boundary against a hostile sidecar, since a pod's projected tokens are minted
// for its ServiceAccount whichever container asks. Unlike that one it is not
// gated on mode: the watcher token is in every gateway pod.
func watcherTokenSourceNames(agent *agentv1alpha1.PlatformAgent) map[string]bool {
	names := map[string]bool{watcherTokenVolume: true}
	if agent.Spec.Deployment == nil {
		return names
	}
	for _, list := range [][]corev1.Volume{agent.Spec.Deployment.SidecarVolumes, agent.Spec.Deployment.ExtraVolumes} {
		for _, v := range list {
			if projectsWatcherToken(v) {
				names[v.Name] = true
			}
		}
	}
	return names
}

// projectsWatcherToken reports whether a volume delivers a token for the
// watcher audience.
func projectsWatcherToken(v corev1.Volume) bool {
	if v.Projected == nil {
		return false
	}
	for _, source := range v.Projected.Sources {
		if source.ServiceAccountToken != nil && source.ServiceAccountToken.Audience == credentialProxyWatcherAudience {
			return true
		}
	}
	return false
}
