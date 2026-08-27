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

// The credential broker in its own Pod.
//
// Everything here is dead code unless spec.security.splitCredentialBrokerPod is
// true, and it stays off by default until the workspace stops being shared.
//
// The reason is the working directory. The broker runs proxied commands in a
// directory the *agent* created, and refuses any working directory outside that
// root (credential_proxy.py, CommandExecutor._execute). Two Pods therefore have
// to see the same files, which is a property of the current directory-sharing
// design rather than of the split. It is also the security problem: a tree the
// agent owns is a tree the agent can write .git/config into, which is the class
// argument-level hardening cannot close by enumeration.
//
// The decided answer is content-passing: the broker owns the workspace on a
// volume of its own, ordinary ReadWriteOnce, and the agent hands it
// {path, content} pairs and a commit message instead of a directory. That
// removes the coupling and the .git class together. It is being built
// separately, and this flag stays off until it lands.
//
// A ReadWriteMany claim is one way to make two Pods see the same files today,
// and an operator may choose it. It is not what the product asks for, and it
// must not become what the product asks for: the managed options bill on
// provisioned capacity with a floor far above what an agent workspace needs,
// which is an adoption tax on installs that run in someone else's environment.
// Co-scheduling the two Pods on one node against a ReadWriteOnce claim is not
// an answer either — a rolling update deadlocks on the volume, node affinity is
// only honoured at scheduling time, and the two Pods become one failure domain.
//
// What the split buys is that the agent no longer shares a network namespace
// with the process holding the cloud credentials, so "reachable on 127.0.0.1"
// stops being an access-control mechanism. What it costs is that the broker
// call becomes a network call, which is why it cannot land without the
// authentication in credential_proxy.py.

import (
	"fmt"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// credentialProxyAudience is the audience the agent's projected token is
	// minted for and the only audience the broker accepts. A token for any
	// other audience — including the Kubernetes API's own — is refused, and
	// this one is useless anywhere but the broker.
	credentialProxyAudience = "kubeagents-credential-proxy" // #nosec G101 -- Token audience name, not a credential

	// credentialProxyTokenMountPath is where the agent container finds the
	// token it presents to the broker.
	credentialProxyTokenMountPath = "/var/run/secrets/kubeagents/credential-proxy" // #nosec G101 -- Mount path, not a credential

	// kubeAPIAccessMountPath is the conventional location of a Pod's
	// default-audience token and cluster CA. The broker reads both to make the
	// TokenReview call that verifies the agent's token.
	kubeAPIAccessMountPath = "/var/run/secrets/kubernetes.io/serviceaccount"

	agentCredentialProxyTokenVolume = "agent-credential-proxy-token" // #nosec G101 -- Volume name, not a credential
)

// reasonSplitBrokerStrandsEventWatcher refuses the layout: the k8s-event-watcher
// is hosted inside the credential container, so splitting the broker takes the
// watcher with it, away from the loopback it posts on.
const reasonSplitBrokerStrandsEventWatcher = "SplitBrokerStrandsEventWatcher"

// validateCredentialBrokerSplit returns a Degraded reason and message when
// spec.security.splitCredentialBrokerPod asks for something the operator cannot
// honestly render, or "" when it can.
//
// There is one such case, and it is the event watcher. start-services.sh runs
// the watcher inside the credential container, gated by EVENT_WATCHER_ENABLED,
// and the watcher posts what it sees to the Session KV server the sandbox binds
// on 127.0.0.1:8699. Both of those are properties of sharing a Pod. Split the
// broker and the watcher goes with it: no loopback to reach the sandbox on, and
// no SESSION_KV_API_KEY, which the operator sets only in the sidecar branch
// because there is no shared loopback for it to authenticate across. Measured on
// a cluster, the result is the watcher exiting and being retried forever while
// the container stays Ready and no cluster event reaches the agent.
//
// So the two are refused together rather than rendered. Note that
// eventWatcherEnabled defaults to true, so this fires for anyone who enables the
// split without also turning the watcher off — which is the intent. The choice
// between the split and fleet event delivery is the operator's to make with the
// facts in front of them, not one to discover from a log line weeks later.
//
// The alternative was to force EVENT_WATCHER_ENABLED=false under the split. A
// quietly disabled observability component is worse than a refusal: nothing in
// the CR or in kubectl describe would say the events had stopped. Giving the
// watcher a home the split does not strand — a Service in front of the Session
// KV server, or the watcher moved into the agent Pod — is the real fix and is
// its own change.
func validateCredentialBrokerSplit(agent *agentv1alpha1.PlatformAgent) (string, string) {
	if !credentialBrokerIsSplit(agent) {
		return "", ""
	}
	if !eventWatcherEnabled(agent) {
		return "", ""
	}
	return reasonSplitBrokerStrandsEventWatcher, "spec.security.splitCredentialBrokerPod: true requires " +
		"spec.harness.eventWatcher.enabled: false. The k8s-event-watcher runs inside the credential " +
		"container and posts to the Session KV server on the agent Pod's loopback, so moving the broker " +
		"into its own Pod takes the watcher away from the only address it can deliver to — it would exit " +
		"and be retried for the life of the Pod while the container stayed Ready and no cluster event " +
		"reached the agent. Set spec.harness.eventWatcher.enabled: false to accept losing cluster event " +
		"delivery and keep the split, or leave splitCredentialBrokerPod off. Giving the watcher a home " +
		"that survives the split is follow-up work."
}

// credentialBrokerIsSplit reports whether the broker runs in its own Pod.
func credentialBrokerIsSplit(agent *agentv1alpha1.PlatformAgent) bool {
	return agent.Spec.Security != nil &&
		agent.Spec.Security.SplitCredentialBrokerPod != nil &&
		*agent.Spec.Security.SplitCredentialBrokerPod
}

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
func credentialProxyBaseURL(agent *agentv1alpha1.PlatformAgent) string {
	if credentialBrokerIsSplit(agent) {
		return fmt.Sprintf("http://%s.%s.svc.cluster.local:%d",
			credentialBrokerName(agent), agent.Namespace, credentialProxyPort)
	}
	return fmt.Sprintf("http://127.0.0.1:%d", credentialProxyPort)
}

// allowedBrokerCallers is the value of CREDENTIAL_PROXY_ALLOWED_CALLERS: the
// TokenReview usernames the broker will serve.
func allowedBrokerCallers(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("system:serviceaccount:%s:%s", agent.Namespace, agentServiceAccountName(agent))
}

// buildAgentCredentialProxyTokenVolume projects the token the agent presents to
// the broker. Audience-bound and one hour long, so a copy that escapes the Pod
// is worth an hour of broker access and nothing else.
func buildAgentCredentialProxyTokenVolume() corev1.Volume {
	return corev1.Volume{
		Name: agentCredentialProxyTokenVolume,
		VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: ptr.To(int32(0400)),
			Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
				Audience: credentialProxyAudience, ExpirationSeconds: ptr.To(int64(3600)), Path: "token",
			}}},
		}},
	}
}

// buildAgentAPIProxyContainer is the authenticated front door for the agent's
// own API, kept in the agent Pod.
//
// It does not follow the broker across the Pod boundary, and that is not an
// oversight. It terminates an external caller's bearer key and forwards to
// 127.0.0.1:8642 with a fixed non-secret sentinel — the agent's API server
// trusts that sentinel precisely because only the Pod's own loopback can
// present it. Moving this into the broker Pod would mean publishing 8642 on
// the cluster network behind a shared fixed string, which is a credential
// nobody may present from outside the Pod. So the process's two jobs split
// along the Pod boundary:
// broker there, front door here. It holds the external API key and no cloud
// credential at all.
func buildAgentAPIProxyContainer(agent *agentv1alpha1.PlatformAgent) corev1.Container {
	image := resolveCredentialProxyImage(agent.Spec.Deployment)
	pullPolicy := corev1.PullAlways
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.ImagePullPolicy != nil {
		pullPolicy = *agent.Spec.Deployment.ImagePullPolicy
	}

	apiServerSecretRef := defaultSecretRef(nil, defaultPlatformAgentSecrets, "API_SERVER_KEY")
	if harness := agent.Spec.Harness; harness != nil && harness.Hermes != nil && harness.Hermes.ApiServerSecretRef != nil {
		apiServerSecretRef = harness.Hermes.ApiServerSecretRef
	}

	return corev1.Container{
		Name:            "agent-api-proxy",
		Image:           image,
		ImagePullPolicy: pullPolicy,
		Command:         []string{"/opt/hermes/.venv/bin/python3", "/opt/defaults/scripts/credential_proxy.py"},
		Env: []corev1.EnvVar{
			{Name: "CREDENTIAL_PROXY_ROLE", Value: "api-proxy"},
			{Name: "AGENT_API_PROXY_PORT", Value: "8643"},
			{Name: "AGENT_API_UPSTREAM_KEY", Value: loopbackAgentAPIKey},
			{Name: "PYTHONPATH", Value: "/opt/defaults/scripts"},
			{Name: "HOME", Value: "/tmp"},
			{
				Name:      "API_SERVER_EXTERNAL_KEY",
				ValueFrom: &corev1.EnvVarSource{SecretKeyRef: apiServerSecretRef},
			},
		},
		Ports: []corev1.ContainerPort{{Name: "proxy-api", ContainerPort: 8643}},
		// Pod readiness used to be the broker sidecar's readiness, which is
		// what kept the Service from routing to a half-started agent. The
		// broker is not in this Pod any more, so the front door takes that
		// job: it is the container the Service's targetPort names.
		ReadinessProbe: &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{
				TCPSocket: &corev1.TCPSocketAction{Port: intstr.FromString("proxy-api")},
			},
			InitialDelaySeconds: 5,
			PeriodSeconds:       10,
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("50m"),
				corev1.ResourceMemory: resource.MustParse("64Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("200m"),
				corev1.ResourceMemory: resource.MustParse("128Mi"),
			},
		},
		SecurityContext: &corev1.SecurityContext{
			// A user of its own, as the sidecar had. It holds the external API
			// key, so the sandbox must not be able to read its memory or files.
			RunAsUser:                ptr.To(credentialProxyUID),
			RunAsGroup:               ptr.To(agentFSGroup),
			AllowPrivilegeEscalation: ptr.To(false),
			ReadOnlyRootFilesystem:   ptr.To(true),
			Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
	}
}

// buildCredentialBrokerDeployment renders the broker's own Pod.
//
// Deliberately not a copy of the agent's workload builder: this Pod runs one
// container, mounts no plugin volumes, takes no user-supplied sidecars, and
// runs entirely as credentialProxyUID. Replicas is fixed at one and the
// strategy is Recreate — the broker owns a lock file and a kubeconfig cache on
// its own emptyDir, and two of them would race over the shared workspace.
func buildCredentialBrokerDeployment(agent *agentv1alpha1.PlatformAgent, policyHash, homeDir string) *appsv1.Deployment {
	labels := commonLabels(agent)
	labels["app"] = credentialBrokerName(agent)
	labels["kubeagents.x-k8s.io/component"] = "credential-broker"

	var runtimeClassName *string
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.Availability != nil {
		runtimeClassName = agent.Spec.Deployment.Availability.RuntimeClassName
	}

	volumes := buildCredentialProxyVolumes(agent)
	volumes = append(volumes, corev1.Volume{
		Name: "platform-agent-data-vol",
		VolumeSource: corev1.VolumeSource{
			PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
				ClaimName: agent.Name + "-data",
			},
		},
	})

	return &appsv1.Deployment{
		TypeMeta: metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      credentialBrokerName(agent),
			Namespace: agent.Namespace,
			Labels:    labels,
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr.To(int32(1)),
			Strategy: appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType},
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": credentialBrokerName(agent)}},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      labels,
					Annotations: map[string]string{"kubeagents.x-k8s.io/proxy-policy-hash": policyHash},
				},
				Spec: corev1.PodSpec{
					RuntimeClassName:   runtimeClassName,
					ServiceAccountName: agentServiceAccountName(agent),
					// The token this Pod uses for its TokenReview call is
					// projected explicitly below, so the automatic mount stays
					// off exactly as it is on the agent Pod.
					AutomountServiceAccountToken: ptr.To(false),
					SecurityContext: &corev1.PodSecurityContext{
						FSGroup:        ptr.To(agentFSGroup),
						RunAsUser:      ptr.To(credentialProxyUID),
						RunAsGroup:     ptr.To(agentFSGroup),
						RunAsNonRoot:   ptr.To(true),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: []corev1.Container{buildCredentialBrokerContainer(agent, homeDir)},
					Volumes:    volumes,
				},
			},
		},
	}
}

// buildCredentialBrokerContainer is the sidecar container, adjusted for a Pod
// of its own: it drops the agent-API front door, gains caller authentication,
// and tells Envoy to listen on the Pod IP instead of loopback.
// The TokenReview call needs the Pod's default-audience token and the cluster
// CA at kubeAPIAccessMountPath. The sidecar builder already mounts
// event-watcher-ksa-token there, so this adds no mount of its own — a second
// volumeMount at the same mountPath is rejected by the API server outright.
func buildCredentialBrokerContainer(agent *agentv1alpha1.PlatformAgent, homeDir string) corev1.Container {
	container := buildCredentialProxySidecar(agent, homeDir)
	container.Name = "credential-broker"
	container.Ports = []corev1.ContainerPort{{Name: "cred-proxy", ContainerPort: credentialProxyPort}}
	return container
}

// buildCredentialBrokerService publishes the broker to the agent Pod.
func buildCredentialBrokerService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	return &corev1.Service{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      credentialBrokerName(agent),
			Namespace: agent.Namespace,
		},
		Spec: corev1.ServiceSpec{
			Selector: map[string]string{"app": credentialBrokerName(agent)},
			Ports: []corev1.ServicePort{{
				Name:       "cred-proxy",
				Port:       credentialProxyPort,
				TargetPort: intstr.FromString("cred-proxy"),
			}},
		},
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
// agent Pod also runs as, so with the split on the sandbox can validate any
// bearer token it gets hold of. It has no use for that. Narrowing it means
// giving the two Pods separate ServiceAccounts — see agentServiceAccountName —
// which is why this is a cost of enabling the flag rather than something the
// flag can fix on its own. It is applied only under the split and deleted when
// the split goes off (reconcileCredentialBroker), so an install with the flag
// off never has it.
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
