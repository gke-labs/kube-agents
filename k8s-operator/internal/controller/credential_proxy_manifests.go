package controller

import (
	"fmt"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The credential proxy: the pod that holds every credential the agent is not
// allowed to see.
//
// It is a Deployment of its own, always. No setting places it in the agent's
// gateway Pod or in the shell sandbox's StatefulSet, because the pod is the
// smallest unit that has an IP and an IP is what GKE resolves Workload Identity
// by. A credential runtime sharing a pod with model-authored code lets that code
// curl 169.254.169.254 and mint the runtime's own GSA token — every credential
// the proxy holds, with the policy layer bypassed entirely. Neither gVisor nor
// NetworkPolicy nor automountServiceAccountToken:false closes that: gVisor's
// boundary is the host kernel rather than the network, runtimeClassName and
// NetworkPolicy are both pod-scoped, and Workload Identity does not read the
// projected token file.
//
// Callers reach it over a ClusterIP on credentialProxyPort and are authenticated
// by TokenReview against CREDENTIAL_PROXY_ALLOWED_CALLERS, which names the
// ServiceAccounts it will serve. buildCredentialProxyNetworkPolicy narrows who
// can open the connection at all, wherever the CNI enforces one.
//
// The boundary costs the proxy a shared working tree. A separate pod cannot
// mount the sandbox's ReadWriteOnce claim, so `git` here operates on the proxy's
// own state volume rather than on the shell's checkout, and a wrapped command
// that names a host path has to pass the content rather than the filename. The
// version-control abstraction is what settles this rather than working around
// it: local git stays in the sandbox against a remoteless checkout, and only the
// remote operations cross to this pod.
//
// spec.security.workloadIdentityFederation is optional hardening on top. It
// moves this pod's cloud identity off the metadata server and onto a projected
// token file: the pod's ServiceAccount carries no iam.gke.io/gcp-service-account
// annotation, so the metadata server answers with an unbound
// <project>.svc.id.goog principal that IAM grants nothing, and the provider's
// attribute conditions bound what the exchange can mint. It narrows what this
// pod may mint; it does not decide where the pod runs.
//
// The split is by role rather than by copy: the same image runs here and in the
// gateway Pod's agent-api-auth container, with CREDENTIAL_PROXY_ROLE selecting
// which of its three services start. See deploy/shared/start-services.sh, and
// the design in docs/designs/agent-shell-sandboxing.md.

const (
	// Where the federated token and the ADC config derived from it live. Both
	// are inside the proxy container's mount namespace and nowhere else — that
	// containment is the control, so a mount added to the shell container at
	// either path silently undoes this whole design.
	credentialProxyWIFTokenVolume = "credential-proxy-wif-token"      // #nosec G101 -- Volume name, not a credential
	credentialProxyWIFTokenPath   = "/var/run/secrets/kubeagents/wif" // #nosec G101 -- Mount path, not a credential
	credentialProxyWIFTokenFile   = credentialProxyWIFTokenPath + "/token"

	// On credential-proxy-runtime, which is a memory-backed emptyDir: the file
	// names the token path and the impersonation target and is regenerated at
	// every container start, so nothing is gained by letting it reach a disk.
	credentialProxyWIFCredentialFile = "/var/run/credential-proxy/wif-credentials.json" // #nosec G101 -- File path, not a credential
)

// credentialProxyFederation returns the federation config when it is complete.
//
// Both fields or neither: a pool with no service account to impersonate, or an
// impersonation target with no pool to reach it through, cannot produce a token.
// Treating a half-filled block as absent means the broker falls back to the
// metadata server, which works from a pod the model cannot enter — rather than
// half-configuring an exchange and failing every credentialed command.
//
// Optional, not a precondition. The broker always has a pod to itself, so the
// metadata identity it holds is already out of the shell's network namespace;
// federation narrows what that pod can mint, it does not decide where it runs.
func credentialProxyFederation(agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.WorkloadIdentityFederationSpec {
	if agent == nil || agent.Spec.Security == nil {
		return nil
	}
	wif := agent.Spec.Security.WorkloadIdentityFederation
	if wif == nil || wif.Audience == "" || wif.ServiceAccountEmail == "" {
		return nil
	}
	return wif
}

// credentialProxyName is the Deployment, Service and pod-selector name.
func credentialProxyName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-credential-proxy"
}

// credentialProxySelector reproduces the labels the pre-#368 standalone proxy
// carried, down to the component label nothing reads any more. A Deployment's
// spec.selector is immutable, so an install old enough to still have that
// Deployment — one that has not reconciled since #368's cleanup removed it —
// would otherwise fail the apply and wedge the whole reconcile rather than
// adopting the object.
func credentialProxySelector(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return map[string]string{
		"app":                           credentialProxyName(agent),
		"kubeagents.x-k8s.io/component": "credential-proxy",
	}
}

// credentialProxyURL is the routable address: what the gateway's Google Chat and
// Slack relay clients dial, from a pod that is never the proxy's own. Fully
// qualified so it resolves the same from a pod with a different search path.
func credentialProxyURL(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("http://%s.%s.svc.cluster.local:%d",
		credentialProxyName(agent), agent.Namespace, credentialProxyPort)
}

// credentialProxySandboxURL is what the sandbox's wrapped CLIs post to.
//
// The same Service address the gateway uses, because the broker is never in the
// sandbox's pod: a broker sharing the shell's network namespace is reachable by
// the model on loopback, and so is anything that namespace can reach, which is
// the property the separate pod exists to deny.
//
// One consequence to keep in view. The two pods share no filesystem, so a path
// is not a value that can cross this boundary — a `cwd`, a `kubeconfig` or a
// `--body-file` argument names something the broker cannot open. Content moves
// as content, over the workspace API, or it does not move.
func credentialProxySandboxURL(agent *agentv1alpha1.PlatformAgent) string {
	return credentialProxyURL(agent)
}

func buildCredentialProxyService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	svc := &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: credentialProxyName(agent), Namespace: agent.Namespace},
		Spec: corev1.ServiceSpec{
			Selector: credentialProxySelector(agent),
			Ports: []corev1.ServicePort{{
				Name:       "cred-proxy",
				Port:       credentialProxyPort,
				TargetPort: intstr.FromString("cred-proxy"),
			}},
		},
	}
	withCommonLabels(svc, agent)
	return svc
}

// buildCredentialProxyDeployment renders the standalone proxy pod.
//
// Recreate, not RollingUpdate. The Google Chat relay pulls from a Pub/Sub
// subscription and buffers what it pulled until the gateway fetches it over this
// Service; two pods pulling the same subscription during a rollout means
// messages land in the buffer of the pod that is going away, and the Service
// then load-balances the gateway's fetch to the other one. A few seconds of
// unavailability is the cheaper failure — the gateway retries its long poll,
// while a dropped chat message is silent.
func buildCredentialProxyDeployment(agent *agentv1alpha1.PlatformAgent, policyHash string) *appsv1.Deployment {
	saName := agent.Name
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		saName = agent.Spec.Security.ServiceAccountName
	}
	podLabels := commonLabels(agent)
	for k, v := range credentialProxySelector(agent) {
		podLabels[k] = v
	}
	// What github-token-minter's NetworkPolicy admits on 8080. It follows the
	// credential runtime rather than staying on the gateway: the runtime is what
	// calls TOKEN_BROKER_URL, and the gateway pod no longer has a reason to.
	podLabels["kubeagents.x-k8s.io/has-credential-proxy"] = "true"

	var affinity *corev1.Affinity
	var nodeSelector map[string]string
	var tolerations []corev1.Toleration
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.Availability != nil {
		affinity = agent.Spec.Deployment.Availability.Affinity
		nodeSelector = agent.Spec.Deployment.Availability.NodeSelector
		tolerations = agent.Spec.Deployment.Availability.Tolerations
	}

	dep := &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: credentialProxyName(agent), Namespace: agent.Namespace},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr.To(int32(1)),
			Strategy: appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType},
			Selector: &metav1.LabelSelector{MatchLabels: credentialProxySelector(agent)},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      podLabels,
					Annotations: map[string]string{"kubeagents.x-k8s.io/proxy-policy-hash": policyHash},
				},
				Spec: corev1.PodSpec{
					ServiceAccountName:           saName,
					AutomountServiceAccountToken: ptr.To(false),
					// The same UID and group the agent image ships, because the
					// credential runtime and the agent are built from it. What
					// used to separate this process from the sandbox's was a
					// second UID inside one Pod; the Pod boundary does it now.
					SecurityContext: &corev1.PodSecurityContext{
						FSGroup:        ptr.To(agentFSGroup),
						RunAsUser:      ptr.To(sandboxUID),
						RunAsNonRoot:   ptr.To(true),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					// The same pull identity as the gateway pod. This used to come
					// for free: the credential runtime was a sidecar in that pod,
					// so it pulled under the pod's secrets. A pod of its own has
					// to be told, and an install pulling from an authenticated
					// registry has no other way to say it — both
					// spec.deployment.imagePullSecrets and the operator-wide
					// IMAGE_PULL_SECRETS resolve here.
					ImagePullSecrets: resolveImagePullSecrets(agent.Spec.Deployment),
					Affinity:         affinity,
					NodeSelector:     nodeSelector,
					Tolerations:      tolerations,
					Containers:       []corev1.Container{buildCredentialProxyContainer(agent)},
					Volumes:          buildCredentialProxyRuntimeVolumes(agent),
				},
			},
		},
	}
	withCommonLabels(dep, agent)
	dep.Labels["app"] = credentialProxyName(agent)
	return dep
}

// buildCredentialProxyContainer is the credential half of the old sidecar: Envoy
// and the credential runtime, with the chat relays the runtime hosts. The event
// watcher and the agent API authenticator stay in the gateway pod, because both
// talk to processes on that pod's loopback — see buildAgentAPIAuthSidecar.
//
// One variant, because the broker has one placement: a Deployment of its own.
// It shares no pod with the agent, which holds the credentials it mints from,
// and no pod with the sandbox, which runs model-authored code.
func buildCredentialProxyContainer(agent *agentv1alpha1.PlatformAgent) corev1.Container {
	pullPolicy := corev1.PullAlways
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.ImagePullPolicy != nil {
		pullPolicy = *agent.Spec.Deployment.ImagePullPolicy
	}
	// The role, the listen address and the caller authentication come from
	// buildCredentialProxyEnv. 0.0.0.0 because both callers arrive over the
	// Service — the sandbox's wrapped CLIs and the gateway's chat relays, which
	// are hosted in this same process, and one listener serves both.
	envVars := buildCredentialProxyEnv(agent)
	volumeMounts := buildCredentialProxyVolumeMounts(agent)
	securityContext := &corev1.SecurityContext{
		AllowPrivilegeEscalation: ptr.To(false), ReadOnlyRootFilesystem: ptr.To(true), Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
	}
	// Federation is optional hardening on this pod rather than a precondition
	// for it. Set, the broker's Google clients read a projected token instead of
	// 169.254.169.254; unset, they use the metadata server, which is already out
	// of the shell's reach because the shell is in another pod. The mount is
	// gated on the same nil check as the volume in
	// buildCredentialProxyFederationVolume: a mount naming a volume that is not
	// there does not start.
	if credentialProxyFederation(agent) != nil {
		envVars = append(envVars, buildCredentialProxyFederationEnv(agent)...)
		volumeMounts = append(volumeMounts,
			corev1.VolumeMount{Name: credentialProxyWIFTokenVolume, MountPath: credentialProxyWIFTokenPath, ReadOnly: true},
		)
	}
	// CREDENTIAL_PROXY_WORKSPACE_ROOT is deliberately not set. It would name the
	// sandbox's data volume, which a separate pod cannot mount — that claim is
	// ReadWriteOnce. Unset, credential_proxy.py falls back to
	// <state-dir>/workspace inside this pod's own emptyDir, which is where a
	// `git clone` through the proxy lands and where nothing else can read it.
	return corev1.Container{
		Name:            "envoy-credential-proxy",
		Image:           resolveCredentialProxyImage(agent.Spec.Deployment),
		ImagePullPolicy: pullPolicy,
		Command:         []string{"/usr/local/bin/start-services"},
		Env:             envVars,
		Ports:           []corev1.ContainerPort{{Name: "cred-proxy", ContainerPort: credentialProxyPort}},
		ReadinessProbe: &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
				Path: "/healthz", Port: intstr.FromString("cred-proxy"),
			}},
			InitialDelaySeconds: 5,
			PeriodSeconds:       10,
			TimeoutSeconds:      5,
			FailureThreshold:    3,
		},
		Resources: corev1.ResourceRequirements{
			// Lower than the sidecar's, which sized for the event watcher's
			// informer caches. Nothing here holds cluster state; the memory goes
			// on Envoy and one Python process per in-flight command.
			Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse("256Mi")},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("1Gi"), corev1.ResourceEphemeralStorage: resource.MustParse("2Gi"),
			},
		},
		VolumeMounts:    volumeMounts,
		SecurityContext: securityContext,
	}
}

// buildCredentialProxyVolumeMounts is what the broker container mounts.
//
// Every entry holds something the shell must never read — the proxy's
// kubeconfig, its gcloud config directory, its projected tokens. None of them
// is mounted anywhere but this pod, and the sandbox is a different pod, so a
// mount here is not reachable from the shell at all.
//
// Two of them exist only because buildCredentialProxyEnv names the paths.
// GITOPS_STATE_PATH points into gitopsStateDir and
// CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE into scopedSAPoolMountPath; without the
// mounts the broker reads a variable naming a file that is not there, which is
// a runtime failure in the GitOps and scoped-identity paths rather than a
// startup one. Keep the two lists in step.
func buildCredentialProxyVolumeMounts(agent *agentv1alpha1.PlatformAgent) []corev1.VolumeMount {
	mounts := []corev1.VolumeMount{
		{Name: "credential-proxy-policy", MountPath: "/etc/credential-proxy/policy.json", SubPath: "policy.json", ReadOnly: true},
		{Name: "credential-proxy-tmp", MountPath: "/tmp"},
		{Name: "credential-proxy-state", MountPath: "/var/lib/credential-proxy"},
		{Name: "credential-proxy-runtime", MountPath: "/var/run/credential-proxy"},
		// Named for the watcher it was introduced for, but what it holds is
		// $KUBECONFIG — the file CREDENTIAL_PROXY_BOOTSTRAP_COMMAND writes with
		// `gcloud container clusters get-credentials`. The watcher moved; the
		// kubeconfig did not.
		{Name: "event-watcher-kubeconfig", MountPath: "/var/run/event-watcher"},
		{Name: "credential-proxy-ksa-token", MountPath: "/var/run/secrets/kubeagents/serviceaccount", ReadOnly: true},
		// The default-audience token and the cluster CA, which is what the
		// TokenReview call needs — a broker off the agent's Pod authenticates
		// its callers, and it authenticates itself to the API server with this.
		// A different token from the one above: that one is audience-bound to
		// the broker and is what the proxy *presents*, and an audience-bound
		// token is not accepted as a client credential by the API server.
		// AutomountServiceAccountToken is false on both Pods, so nothing
		// projects this bundle unless it is asked for.
		{Name: "event-watcher-ksa-token", MountPath: kubeAPIAccessMountPath, ReadOnly: true},
		{Name: gitopsStateVolumeName, MountPath: gitopsStateDir, ReadOnly: true},
	}
	// Conditional because it is a SubPath mount: naming a key the ConfigMap does
	// not carry leaves the container unable to start, so the mount and the key
	// have to appear and disappear together.
	if scopedSAPoolEnabled(agent) {
		mounts = append(mounts, corev1.VolumeMount{
			Name:      "credential-proxy-policy",
			MountPath: scopedSAPoolMountPath,
			SubPath:   scopedSAPoolKey,
			ReadOnly:  true,
		})
	}
	return mounts
}

// buildCredentialProxyFederationEnv points the proxy's Google clients at a token
// file instead of 169.254.169.254.
//
// Three variables do the work. CREDENTIAL_PROXY_WIF_* are read by
// scripts/wif_credentials.py, which start-services.sh runs before anything else
// and which writes the external_account document. The other two are what make
// that document authoritative: GOOGLE_APPLICATION_CREDENTIALS for the client
// libraries and gke-gcloud-auth-plugin, CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE
// for gcloud itself, which keeps its own credential store and would otherwise
// ignore ADC and fall through to the metadata server. Both names are already on
// credential_proxy.py's forwarding allowlist, so they reach the executed command
// as well as the bootstrap.
func buildCredentialProxyFederationEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	wif := credentialProxyFederation(agent)
	if wif == nil {
		return nil
	}
	return []corev1.EnvVar{
		{Name: "CREDENTIAL_PROXY_WIF_AUDIENCE", Value: wif.Audience},
		{Name: "CREDENTIAL_PROXY_WIF_SERVICE_ACCOUNT", Value: wif.ServiceAccountEmail},
		{Name: "CREDENTIAL_PROXY_WIF_TOKEN_FILE", Value: credentialProxyWIFTokenFile},
		{Name: "CREDENTIAL_PROXY_WIF_CREDENTIAL_FILE", Value: credentialProxyWIFCredentialFile},
		{Name: "GOOGLE_APPLICATION_CREDENTIALS", Value: credentialProxyWIFCredentialFile},
		{Name: "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", Value: credentialProxyWIFCredentialFile},
	}
}

// buildCredentialProxyFederationVolume is the projected token STS validates.
//
// A second projection rather than a re-audienced credential-proxy-ksa-token: that
// one is presented to github-token-minter, which checks for its own audience, and
// STS checks for the provider's. One token cannot satisfy both, and widening
// either audience to cover the other would let a token minted for one verifier be
// replayed at the other.
//
// Gated on the field alone, and matched by the mount in
// buildCredentialProxyContainer. Federation is hardening the operator applies
// wherever it is configured: it narrows what this pod can mint to what the
// provider's attribute conditions allow, which is worth having whether or not
// the metadata identity would also have worked.
func buildCredentialProxyFederationVolume(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	wif := credentialProxyFederation(agent)
	if wif == nil {
		return nil
	}
	return []corev1.Volume{{
		Name: credentialProxyWIFTokenVolume,
		VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: ptr.To(int32(0400)),
			Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
				Audience: wif.Audience,
				// The floor kubelet accepts. Short because the exchange is
				// re-run on demand by the auth library from the file, so a
				// rotation costs nothing, and because this token is the one
				// thing in the pod that is worth stealing.
				ExpirationSeconds: ptr.To(int64(3600)),
				Path:              "token",
			}}},
		}},
	}}
}

// buildCredentialProxyNetworkPolicy narrows who may reach the endpoint down to
// the two callers that have a reason to: the sandbox, whose wrapped CLIs are the
// proxy's purpose, and the gateway, which pulls chat events from the relay
// hosted here. TokenReview already rejects a caller this pod does not serve;
// this is the layer that keeps such a caller from opening the connection.
//
// Ingress only. Egress is left open because this pod is the one that talks to
// the world — GKE control planes, the Google Chat and Slack APIs, the token
// broker. buildAgentEgressNetworkPolicy enumerates the agent Pod's egress and
// deliberately leaves this one alone.
//
// Inert on a cluster whose CNI does not implement NetworkPolicy. It is a control
// where it is enforced and a statement of intent where it is not.
func buildCredentialProxyNetworkPolicy(agent *agentv1alpha1.PlatformAgent) *networkingv1.NetworkPolicy {
	tcp := corev1.ProtocolTCP
	np := &networkingv1.NetworkPolicy{
		TypeMeta:   metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{Name: credentialProxyName(agent), Namespace: agent.Namespace},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: credentialProxySelector(agent)},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
			Ingress: []networkingv1.NetworkPolicyIngressRule{{
				From: []networkingv1.NetworkPolicyPeer{
					{PodSelector: &metav1.LabelSelector{MatchLabels: shellSandboxSelector(agent)}},
					{PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": agent.Name + "-gateway"}}},
				},
				Ports: []networkingv1.NetworkPolicyPort{{
					Protocol: &tcp,
					Port:     ptr.To(intstr.FromInt32(credentialProxyPort)),
				}},
			}},
		},
	}
	withCommonLabels(np, agent)
	return np
}

// buildCredentialProxyRuntimeVolumes and buildAgentAPIAuthVolumes split
// buildCredentialProxyVolumes between the two pods the sidecar became. Both
// filter the same source list rather than restating it, so a volume added there
// has to be assigned to a side here and cannot be silently dropped from both.
var (
	// The watcher's default-audience token and the agent's data volume went with
	// the watcher; everything else is the credential runtime's.
	agentAPIAuthVolumeNames = map[string]bool{
		"credential-proxy-tmp":     true,
		"event-watcher-kubeconfig": true,
		"event-watcher-ksa-token":  true,
	}
	// Volumes the agent container must never mount, whatever the CR says.
	// `credential-proxy-state` is the sharp one: it holds $HOME/.gitconfig, the
	// regenerated kubeconfigs the agent is specifically not supposed to hold,
	// and the content workspaces. Three separate controls in credential_proxy.py
	// rest on the agent not seeing that directory, and each of their comments
	// says the protection is deployment geometry rather than a check — this is
	// the check. The CR is authored by an operator rather than by the agent, so
	// this is a configuration hazard and not an escape; it is guarded because
	// nothing else would notice.
	agentForbiddenVolumeNames = map[string]bool{
		"credential-proxy-state":     true,
		"credential-proxy-policy":    true,
		"credential-proxy-runtime":   true,
		"credential-proxy-ksa-token": true,
	}
	credentialProxyRuntimeVolumeNames = map[string]bool{
		"credential-proxy-policy":    true,
		"credential-proxy-tmp":       true,
		"credential-proxy-state":     true,
		"credential-proxy-runtime":   true,
		"event-watcher-kubeconfig":   true,
		"credential-proxy-ksa-token": true,
		// On both sides, and deliberately: it is a projection rather than a
		// shared object, so each Pod gets its own. The watcher authenticates to
		// the API server with it in the agent Pod, and the broker makes its
		// TokenReview call with it here.
		"event-watcher-ksa-token": true,
	}
)

// buildCredentialProxyRuntimeVolumes is the credential runtime's own set: the
// broker Deployment's volume list. The 0400 mode the projections carry is
// readable because that pod sets an fsGroup, which is what makes kubelet apply
// group ownership to a projected file.
func buildCredentialProxyRuntimeVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	volumes := filterVolumes(buildCredentialProxyVolumes(agent), credentialProxyRuntimeVolumeNames)
	volumes = append(volumes, buildGitopsStateVolume(agent))
	return append(volumes, buildCredentialProxyFederationVolume(agent)...)
}

// buildAgentAPIAuthVolumes is the gateway pod's remaining share. The data volume
// the watcher reads is not here: the gateway pod already declares it.
func buildAgentAPIAuthVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	return filterVolumes(buildCredentialProxyVolumes(agent), agentAPIAuthVolumeNames)
}

func filterVolumes(volumes []corev1.Volume, keep map[string]bool) []corev1.Volume {
	var out []corev1.Volume
	for _, vol := range volumes {
		if keep[vol.Name] {
			out = append(out, vol)
		}
	}
	return out
}

// validateExtraVolumeMounts refuses a CR that would mount a broker-owned volume
// into the agent container, returning the message for a degraded condition or
// "" when the CR is acceptable.
//
// It reports rather than silently dropping the mount. A dropped mount is a CR
// whose author believes it took effect, and the failure this guards against is
// one nobody looks for: the manifest applies cleanly and the agent quietly gains
// read access to the credentials the proxy exists to keep from it.
func validateExtraVolumeMounts(agent *agentv1alpha1.PlatformAgent) string {
	if agent.Spec.Deployment == nil {
		return ""
	}
	var forbidden []string
	for _, mount := range agent.Spec.Deployment.ExtraVolumeMounts {
		if agentForbiddenVolumeNames[mount.Name] {
			forbidden = append(forbidden, fmt.Sprintf("%s (at %s)", mount.Name, mount.MountPath))
		}
	}
	if len(forbidden) == 0 {
		return ""
	}
	return fmt.Sprintf(
		"spec.deployment.extraVolumeMounts names volumes owned by the credential proxy: %s. "+
			"Those hold the broker's home directory, its generated kubeconfigs and its git "+
			"workspaces, and mounting them into the agent container defeats the credential "+
			"isolation the proxy provides. Remove them, or use a volume of your own.",
		strings.Join(forbidden, ", "),
	)
}
