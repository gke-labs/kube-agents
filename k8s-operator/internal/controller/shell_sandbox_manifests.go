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

// The shell sandbox: the pod the agent's terminal, file and code-execution tools
// run in once Hermes' `ssh` terminal backend is turned on. Design and rationale
// live in docs/designs/agent-shell-sandboxing.md; the image is deploy/sandbox/.
//
// Reconciled on every install. spec.harness.experimental.shellSandbox is a block of
// overrides now, not a switch: `enabled: false` is refused with
// Degraded/ShellSandboxCannotBeDisabled (validateShellSandbox below), because the
// agent image ships no kubectl, gcloud, gh or git and this pod is the only place a
// command can run. The credential proxy it reaches for those credentials has an
// address of its own — see the credentialProxyURL parameter below and
// credential_proxy_manifests.go.
//
// On the name: "sandbox" already means something else here. The agent's own
// container is the credential-isolation sandbox — see buildSandboxCredentialCleanup
// and safeSandboxEnvOverrides — and that usage predates this file and is load-bearing
// in docs/credential-isolation-design.md. Everything in here is therefore the *shell*
// sandbox, and its objects are named <agent>-shell so no one has to hold both
// meanings at once while reading a `kubectl get`.
//
// On the workload kind: this was going to be a `Sandbox` custom resource from
// kubernetes-sigs/agent-sandbox. It is a StatefulSet because three of that project's
// four CRDs do not exist in the version that ships, and what does ship maps field for
// field onto this file. The design doc records the evidence, and the interface is
// drawn so that swapping back is this file and nothing else.
package controller

import (
	"fmt"
	"os"
	"path"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// The port sshd listens on in the sandbox image, matching
	// deploy/sandbox/sshd_config. Above 1024 so the daemon does not need
	// CAP_NET_BIND_SERVICE, and not 22 so nothing in the cluster mistakes it for
	// a node.
	shellSandboxPort = 2222

	// Operator-level override for installs mirroring images into a private
	// registry, matching the "override" field of the agent-sandbox entry in
	// images.json. Set on the controller-manager Deployment.
	shellSandboxImageEnvVar = "AGENT_SANDBOX_IMAGE"

	// The repository half of the sandbox image reference, as it is named in
	// images.json and pushed by the release workflow. Substituted into the
	// operator's own image reference by resolveShellSandboxImage.
	shellSandboxRepositoryName = "agent-sandbox"

	// The login the agent ssh's in as, created by deploy/sandbox/Dockerfile as uid
	// 1000, with an ephemeral home and a durable /opt/data. Not root, and not the
	// agent pod's own uid 10000 — the two pods share nothing but a public key.
	shellSandboxUser = "agent"

	// shellSandboxUser's uid, from the same useradd. Named here because the
	// entrypoint chowns the data volume to it and the StatefulSet's
	// securityContext has to agree with the image.
	shellSandboxUID = 1000

	shellSandboxDataVolume     = "data"
	shellSandboxSshdVolume     = "sshd"
	shellSandboxKeysVolume     = "authorized-keys"
	shellSandboxSettingsVolume = "settings"

	// The token the shell presents to the credential runtime. It mounts at
	// credentialProxyTokenMountPath, the same path the agent pod uses, because a
	// script that reads CREDENTIAL_PROXY_TOKEN_FILE should not have to care which
	// pod it is running in.
	shellSandboxCredentialProxyTokenVolume = "credential-proxy-token" // #nosec G101 -- Volume name, not a credential

	// Where deploy/sandbox/entrypoint.sh expects each of them. Changing either
	// side alone starts a pod that exits with a pointed message rather than one
	// that half works, which is the intended failure mode.
	//
	// The data path is the agent pod's Hermes home path, on purpose and on a
	// different volume: the SOPs, skills and model-written scripts that hardcode
	// /opt/data then resolve wherever they run, instead of failing on a directory
	// that exists in only one of the two pods. Nothing is copied across and
	// nothing can read across — see the marker file entrypoint.sh writes, and the
	// design doc's note that no handoff may assume write-here-read-there.
	shellSandboxDataPath = "/opt/data"
	shellSandboxKeysPath = "/etc/ssh-authorized"

	// sshd's host keys, on a volume the model has no access to. They cannot live
	// on the data volume: uid 1000 owns that mount point, so it can rename any
	// directory inside it and take over whatever the entrypoint writes there
	// next. Both clients pin the host key with StrictHostKeyChecking=accept-new,
	// which is worth nothing if the sandboxed account holds the private half.
	shellSandboxSshdPath = "/var/lib/sandbox-sshd"

	// shellSandboxUser's home, from the useradd in deploy/sandbox/Dockerfile. It
	// is writable alongside the data volume — see HERMES_WRITE_SAFE_ROOT in
	// buildPodTemplateSpec — but it is on the container filesystem and does not
	// survive a restart. That is deliberate: the model owns ~/.bashrc, bash
	// sources it for a non-interactive `ssh host cmd`, and a hijack planted there
	// should not outlive the pod. Durable work goes to the data volume, which is
	// what TERMINAL_CWD points at.
	shellSandboxHomePath = "/home/" + shellSandboxUser

	// Hermes' ssh backend keeps a file sync over ~/.hermes: it pushes at connect
	// and, in FileSyncManager.sync_back, copies anything new or changed back onto
	// the agent pod's Hermes home. Skills live in that tree, so a file written
	// here would land in the gateway's skills/ as instructions the next session
	// loads. deploy/sandbox/Dockerfile makes the directory root-owned and 0555 to
	// stop that, and on its own that is not enough: /home/agent is owned by uid
	// 1000 on a writable container filesystem, and removing a directory needs
	// write on the parent rather than on the directory. `rmdir ~/.hermes && mkdir
	// ~/.hermes` hands the model a writable one back.
	//
	// An empty read-only volume over the path closes it from the other side. The
	// mount cannot be removed — rmdir on a mount point is EBUSY — and cannot be
	// written whatever it is replaced by, and undoing it needs CAP_SYS_ADMIN,
	// which this container does not have. Leaving /home/agent itself
	// agent-writable keeps ~/.bashrc and the rest of the home working the way the
	// comment above describes.
	shellSandboxHermesHomeVolume = "hermes-sync-block"
	shellSandboxHermesHomePath   = shellSandboxHomePath + "/.hermes"

	// The agent pod's side of the same keypair. Two volumes rather than one for
	// a reason spelled out at buildShellSandboxClientKeyInitContainer: the
	// Secret cannot be handed to `ssh -i` directly.
	shellSandboxClientKeySecretVolume = "sandbox-ssh-secret" // #nosec G101 -- Volume name, not a credential
	shellSandboxClientKeyVolume       = "sandbox-ssh"
	shellSandboxClientKeySecretPath   = "/etc/sandbox-ssh-secret" // #nosec G101 -- Mount path, not a credential
	shellSandboxClientKeyPath         = "/etc/sandbox-ssh"
	shellSandboxClientKeyFile         = "id_ed25519"

	// The key in platform-agent-secrets holding the private half. The public
	// half is beside it as SANDBOX_SSH_PUBLIC_KEY, but the agent pod has no use
	// for it — it is there so a re-running install surface can recover the pair
	// from one place, and so the chart can render the sandbox's Secret from it.
	shellSandboxPrivateKeySecretKey = "SANDBOX_SSH_PRIVATE_KEY" // #nosec G101 -- Secret key name, not a credential
)

// shellSandboxAuthorizedKeysSecretName is the Secret the sandbox mounts. It holds
// one entry, `authorized_keys`, and nothing else.
//
// Deliberately not platform-agent-secrets with an `items:` selector, which would
// work — kubelet projects only the listed items — and is still wrong: that object
// holds every model API key, and naming it in the sandbox's volume list puts the
// whole thing one careless edit away from being readable inside the pod this
// design exists to keep credential-free. The duplication of the public half
// across two Secrets is the price, and a public key is the cheapest thing in the
// system to duplicate.
func shellSandboxAuthorizedKeysSecretName(agent *agentv1alpha1.PlatformAgent) string {
	return shellSandboxName(agent) + "-authorized-keys"
}

// shellSandboxServiceAccountName is the identity the sandbox pod runs as.
//
// Its own, not the agent's, and the difference is the point. The agent's
// ServiceAccount carries iam.gke.io/gcp-service-account, and GKE resolves
// Workload Identity by pod IP — so running this pod under it would hand the
// shell container a full GSA token from 169.254.169.254 whether or not anything
// mounts a Kubernetes token, and whether or not the credential proxy is even
// there. This one is deliberately unannotated: the metadata server answers both
// containers with the unbound <project>.svc.id.goog principal, which IAM grants
// nothing.
//
// The proxy's cloud identity comes from spec.security.workloadIdentityFederation
// instead — a projected token this ServiceAccount can mint, mounted into the
// proxy container alone. The KSA is still the subject IAM authorizes; what
// changes is that the authorization runs against a token file rather than a pod
// IP, and a file is per-container where an IP is not.
func shellSandboxServiceAccountName(agent *agentv1alpha1.PlatformAgent) string {
	return shellSandboxName(agent)
}

// buildShellSandboxServiceAccount renders it. No annotations at all, so there is
// no place for iam.gke.io/gcp-service-account to arrive by accident: the CR's
// spec.security.serviceAccountAnnotations is deliberately not plumbed through
// here, because the one annotation an operator would reach for is the one that
// undoes the isolation.
func buildShellSandboxServiceAccount(agent *agentv1alpha1.PlatformAgent) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      shellSandboxServiceAccountName(agent),
			Namespace: agent.Namespace,
			Labels:    shellSandboxSelector(agent),
		},
		// Kubelet would otherwise create a legacy token Secret for it on older
		// clusters. Nothing reads one; the projected volume is the only token
		// path this design has.
		AutomountServiceAccountToken: ptr.To(false),
	}
}

// shellSandboxClientKeyFilePath is the path the agent's Hermes config points
// `terminal.ssh.key_path` at once this is wired up.
func shellSandboxClientKeyFilePath() string {
	return shellSandboxClientKeyPath + "/" + shellSandboxClientKeyFile
}

// fallbackShellSandboxImage derives its tag from DefaultPlatformAgentVersion at
// call time, exactly as fallbackPlatformAgentImage does, so a release build
// defaults the sandbox and the agent to the same version. They are built from the
// same commit by the same workflow and a skew between them is a bug, not a
// configuration.
func fallbackShellSandboxImage() string {
	return "ghcr.io/gke-labs/kube-agents/" + shellSandboxRepositoryName + ":" + DefaultPlatformAgentVersion
}

// resolveShellSandboxImage returns the sandbox image: the CR's own override if it
// carries one, else AGENT_SANDBOX_IMAGE from the controller, else the operator's
// own image with the repository swapped, else the public ghcr.io default.
//
// That third rung is the same one defaultPlatformAgentImage has, and it is here
// for the same reason: an install that mirrored these images into a private
// registry configures the operator's image and nothing else, because the
// operator is the only image its Deployment names. Without the derivation the
// sandbox is the one pod in the install that reaches ghcr.io anyway, on a
// cluster whose whole point may be that it cannot — and it fails at ImagePull
// on a Deployment nobody edited, which reads as a broken release rather than an
// unset variable. main.go fills OPERATOR_IMAGE from the operator's own pod spec
// when the manifests do not, so this rung is reached on any install, not only
// the chart's.
//
// Deliberately not derived from the resolved *agent* image the way
// resolveCredentialProxyImage is. That derivation exists because the proxy is a
// second stage of the same Dockerfile and must not drift from the agent it sits
// beside in one pod; the sandbox is a separate artifact in a separate pod, and
// inferring its registry from a CR's spec.deployment.image would mean a user who
// points one agent at their own mirror silently gets a sandbox image from a
// repository they never populated. OPERATOR_IMAGE is not that: it is set once
// per install by whoever installed the operator, not per CR by whoever wrote it.
func resolveShellSandboxImage(agent *agentv1alpha1.PlatformAgent) string {
	if spec := shellSandboxSpec(agent); spec != nil && spec.Image != "" {
		return spec.Image
	}
	if override := os.Getenv(shellSandboxImageEnvVar); override != "" {
		return override
	}
	if operatorImage := os.Getenv(operatorImageEnvVar); operatorImage != "" {
		return deriveImageFromOperator(operatorImage, shellSandboxRepositoryName)
	}
	return fallbackShellSandboxImage()
}

// shellSandboxSpec returns the CR's sandbox block, or nil. Every access to it goes
// through here because the path is four optional levels deep and a nil check missed
// anywhere in it is a panic in the reconcile loop.
func shellSandboxSpec(agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.ShellSandboxSpec {
	if agent == nil || agent.Spec.Harness == nil || agent.Spec.Harness.Experimental == nil {
		return nil
	}
	return agent.Spec.Harness.Experimental.ShellSandbox
}

// reasonShellSandboxCannotBeDisabled refuses spec.harness.experimental.shellSandbox.enabled: false.
const reasonShellSandboxCannotBeDisabled = "ShellSandboxCannotBeDisabled"

// reasonShellSandboxKeysMissing names an install whose authorized-keys Secret was
// never created.
const reasonShellSandboxKeysMissing = "ShellSandboxKeysMissing"

// shellSandboxKeysMissingMessage explains an absent authorized-keys Secret and
// names the fix for each install surface.
//
// Worth a condition of its own because the symptom is unreadable: the volume is
// not optional, so kubelet leaves the pod in ContainerCreating indefinitely and
// says why only in a FailedMount event on the pod — not on the StatefulSet, and
// not anywhere in the PlatformAgent an operator is looking at. Every install
// surface generates the pair (see docs/designs/agent-shell-sandboxing.md#key-management),
// so reaching this means a bare `helm install` that supplied none.
func shellSandboxKeysMissingMessage(secretName string) string {
	return fmt.Sprintf("Secret '%s' does not exist, so the shell sandbox pod cannot start and no command "+
		"the agent runs will execute. The chart renders it from the SANDBOX_SSH_PUBLIC_KEY entry in "+
		"platform-agent-secrets; a bare `helm install` that supplies no keypair renders nothing. Run "+
		"`upgrade.sh`, which generates the pair into that Secret, or pass both halves as "+
		"platformAgent.credentials.data.SANDBOX_SSH_PRIVATE_KEY and .SANDBOX_SSH_PUBLIC_KEY.", secretName)
}

// validateShellSandbox refuses a CR that asks for the sandbox to be off,
// returning a Degraded reason and message, or "" when the CR is acceptable.
//
// There is nothing to render for that request. The agent image carries no
// kubectl, gcloud, gh or git — #737 removed them — so an agent with a local
// shell has no shell tools, and the only way to give it any is to put
// model-authored code back in the pod holding the credentials. Answering the
// field by ignoring it would leave an operator reading `enabled: false` off a
// running CR that does the opposite.
func validateShellSandbox(agent *agentv1alpha1.PlatformAgent) (string, string) {
	spec := shellSandboxSpec(agent)
	if spec == nil || spec.Enabled == nil || *spec.Enabled {
		return "", ""
	}
	return reasonShellSandboxCannotBeDisabled, "spec.harness.experimental.shellSandbox.enabled: false " +
		"is not a supported configuration. Every command the agent runs executes in the sandbox pod, and " +
		"the agent image ships no kubectl, gcloud, gh or git of its own — so with the sandbox off the agent " +
		"has no shell tools at all, and restoring them would mean running model-authored code in the pod " +
		"that holds the credentials. Remove the field or set it to true. To stop the agent instead, scale " +
		"its Deployment to zero or delete the PlatformAgent."
}

// shellSandboxRuntimeClassName is the sandbox pod's runtime, or nil for the
// node's default.
//
// An empty string is treated as unset rather than passed through. Kubernetes
// reads `runtimeClassName: ""` as the default runtime, so the two mean the same
// thing to the API server — but only nil leaves the field out of the manifest,
// and a rendered `runtimeClassName: ""` on every install that never asked for
// one is noise in every diff of the object.
func shellSandboxRuntimeClassName(agent *agentv1alpha1.PlatformAgent) *string {
	spec := shellSandboxSpec(agent)
	if spec == nil || spec.RuntimeClassName == nil || *spec.RuntimeClassName == "" {
		return nil
	}
	return ptr.To(*spec.RuntimeClassName)
}

// shellSandboxName is the name of every object in this file: the StatefulSet, its
// governing Service, and the NetworkPolicy. One name, because they are one thing,
// and because the DNS record the agent dials is built from it.
func shellSandboxName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-shell"
}

// shellSandboxDataClaimName is the claim the StatefulSet controller derives from
// the data volumeClaimTemplate: <template>-<statefulset>-<ordinal>. One replica,
// so one ordinal, and it is spelled out here because the operator has to reach
// the claim directly to widen it — the template sizes only claims it creates.
func shellSandboxDataClaimName(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("%s-%s-0", shellSandboxDataVolume, shellSandboxName(agent))
}

// shellSandboxSelector is the pod label the Service, the StatefulSet and both
// halves of the NetworkPolicy agree on. `app` rather than a kubeagents.x-k8s.io/
// key to match the gateway's existing selector, which the ingress rule below has
// to name anyway.
func shellSandboxSelector(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return map[string]string{"app": shellSandboxName(agent)}
}

// shellSandboxHost is the address Hermes' ssh backend connects to: the stable
// per-pod DNS name a StatefulSet gives its replica through its governing Service.
// It is what buildConfigMapData will render into the agent's terminal.ssh settings
// when this is wired up.
//
// Not the Service name. A headless Service resolves to the pod's address either
// way at one replica, but the pod name is the record that stays correct if this
// ever grows a second replica, and it is what makes the identity in
// "long-running singleton with a stable identity" observable from the client side.
func shellSandboxHost(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("%s-0.%s.%s.svc.cluster.local", shellSandboxName(agent), shellSandboxName(agent), agent.Namespace)
}

// buildShellSandboxService is the StatefulSet's governing Service: headless, so it
// publishes the per-pod DNS record above rather than load-balancing to it.
func buildShellSandboxService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	name := shellSandboxName(agent)
	return &corev1.Service{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: agent.Namespace,
			Labels:    shellSandboxSelector(agent),
		},
		Spec: corev1.ServiceSpec{
			ClusterIP: corev1.ClusterIPNone,
			Selector:  shellSandboxSelector(agent),
			Ports: []corev1.ServicePort{{
				Name:       "ssh",
				Port:       shellSandboxPort,
				TargetPort: intstr.FromInt32(shellSandboxPort),
				Protocol:   corev1.ProtocolTCP,
			}},
			// The pod is addressable while sshd is still generating host keys on a
			// first start. Without this the DNS record does not exist until the
			// readiness probe passes, and a StatefulSet's first pod can wait on its
			// own name.
			PublishNotReadyAddresses: true,
		},
	}
}

// buildShellSandboxStatefulSet is the sandbox itself.
//
// authorizedKeysSecret holds the public half of the keypair the agent pod connects
// with, under the key "authorized_keys". credentialProxyURL is what the sandbox's
// kubectl/gcloud/gh/git wrappers post to — always the broker's Service, which is
// always another pod. Empty is a supported state: the entrypoint logs that the
// wrappers are unconfigured and starts anyway, so file and code-execution tools
// work while the credentialed ones report a clear error instead of a stack trace.
//
// settingsConfigHash goes on the pod template for the same reason the agent's
// Deployment carries it: SETTINGS.md is mounted with a subPath, and a subPath mount
// is resolved once at pod start and never refreshed. Without the annotation, editing
// the CR's scope rolls the agent pod onto the new file and leaves the sandbox holding
// the old one — and the sandbox is where the shell reads it, so the skills that read
// SETTINGS.md by path would be the ones getting the stale answer.
func buildShellSandboxStatefulSet(agent *agentv1alpha1.PlatformAgent, authorizedKeysSecret, credentialProxyURL, settingsConfigHash string) *appsv1.StatefulSet {
	name := shellSandboxName(agent)
	labels := shellSandboxSelector(agent)

	env := []corev1.EnvVar{}
	if credentialProxyURL != "" {
		env = append(env, corev1.EnvVar{Name: "CREDENTIAL_PROXY_URL", Value: credentialProxyURL})
		// The shell is a caller of the credential runtime, and the runtime
		// authenticates its callers whenever it is off the agent's Pod — which
		// the sandbox being on already guarantees, at either placement. Without
		// this the listener is reachable and answers 401 to everything, so every
		// command the model runs fails at the point it needs a credential.
		//
		// Audience-bound, so what the shell holds is a credential for the broker
		// and not for the Kubernetes API: the API server rejects a token minted
		// for another audience, which is why mounting one here does not undo
		// AutomountServiceAccountToken: false above.
		env = append(env, corev1.EnvVar{
			Name:  "CREDENTIAL_PROXY_TOKEN_FILE",
			Value: credentialProxyTokenMountPath + "/token",
		})
	}

	containers := buildShellSandboxContainers(agent, env)
	volumes := buildShellSandboxVolumes(agent, authorizedKeysSecret)

	annotations := map[string]string{
		"kubeagents.x-k8s.io/settings-config-hash": settingsConfigHash,
	}
	// commonLabels is in here explicitly, and that is not belt-and-braces.
	// `labels` is one map shared by ObjectMeta.Labels and Selector.MatchLabels
	// below, and withCommonLabels merges into the object's map in place on the
	// way out — so by the time the StatefulSet reaches the API server its
	// selector carries the four recommended labels too. A template built from
	// `labels` alone would then be narrower than the selector the server has
	// stored, which it rejects with `selector` does not match template `labels`.
	podLabels := commonLabels(agent)
	for k, v := range labels {
		podLabels[k] = v
	}
	return &appsv1.StatefulSet{
		TypeMeta: metav1.TypeMeta{APIVersion: "apps/v1", Kind: "StatefulSet"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: agent.Namespace,
			Labels:    labels,
		},
		Spec: appsv1.StatefulSetSpec{
			Replicas:    ptr.To(int32(1)),
			ServiceName: name,
			Selector:    &metav1.LabelSelector{MatchLabels: labels},
			// Retain on both transitions, for both claims. The sshd volume holds
			// the host keys, and Hermes connects with
			// StrictHostKeyChecking=accept-new: a regenerated host key is not a
			// prompt, it is every command from then on failing until known_hosts
			// is edited by hand. The data volume holds whatever the agent has
			// been working on. Deleting the StatefulSet must therefore leave both
			// claims, at the cost of PVCs that outlive their workload.
			PersistentVolumeClaimRetentionPolicy: &appsv1.StatefulSetPersistentVolumeClaimRetentionPolicy{
				WhenDeleted: appsv1.RetainPersistentVolumeClaimRetentionPolicyType,
				WhenScaled:  appsv1.RetainPersistentVolumeClaimRetentionPolicyType,
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      podLabels,
					Annotations: annotations,
				},
				Spec: corev1.PodSpec{
					// Unannotated, so the metadata server has no GSA to hand
					// either container. See shellSandboxServiceAccountName.
					ServiceAccountName: shellSandboxServiceAccountName(agent),
					// The whole point. With a token mounted, the sandbox holds a
					// Kubernetes credential and the boundary this workload exists
					// to draw is decorative.
					//
					// Note what this does *not* do, since it is the obvious thing
					// to reach for and it is not a Workload Identity control:
					// Workload Identity never reads the projected token file, so
					// turning the automount off leaves 169.254.169.254 answering
					// exactly as before. Unbinding the ServiceAccount above is
					// what closes that; this closes the Kubernetes API.
					AutomountServiceAccountToken: ptr.To(false),
					// Explicitly false, never merely unset. With the credential
					// proxy in this pod, a shared PID namespace would put its
					// environment — Slack tokens, API_SERVER_EXTERNAL_KEY — and
					// its whole filesystem behind /proc/<pid>/{environ,root},
					// readable from the shell, which runs as the same uid on
					// purpose. That is the exact finding #720 reproduced on the
					// gateway pod, which did set this. Pinning it rather than
					// leaving it nil is so that a future edit has to argue with a
					// value instead of adding one to a blank.
					ShareProcessNamespace: ptr.To(false),
					// Kubelet otherwise injects a docker-link-style env var for
					// every Service in the namespace. None of them are secrets,
					// but they hand the sandbox a map of the namespace it has no
					// use for: a live pod came up knowing the cluster IP and port
					// of another workload's Service. The sandbox reaches the
					// credential proxy by an explicit URL, so it needs no
					// service discovery at all.
					EnableServiceLinks: ptr.To(false),
					// nil unless the CR names one, so the default install is
					// byte-identical to what it rendered before the field
					// existed. See ShellSandboxSpec.RuntimeClassName for why
					// this is not the agent's field.
					RuntimeClassName: shellSandboxRuntimeClassName(agent),
					// No pod-level securityContext, and that is a decision rather
					// than an omission. sshd's privilege separation forks as uid 0
					// and drops to the unprivileged `agent` user for the session,
					// and the entrypoint chowns the freshly-mounted data volume
					// before it — so runAsNonRoot cannot be set, and a capability
					// drop has to keep at least CHOWN, SETUID, SETGID, SYS_CHROOT
					// and DAC_OVERRIDE. Which of those is genuinely required is a
					// question deploy/sandbox/smoke-test.sh can answer and nobody
					// has asked it yet; guessing here would produce a pod that
					// fails at login, which reads as a key problem.
					//
					// What does not depend on that answer is set on the container
					// instead — see buildShellSandboxContainer for the seccomp
					// profile and the NET_RAW drop.
					// The sandbox image is a fourth image, pulled by a pod that did
					// not exist before this design. It needs the install's pull
					// identity for the same reason the gateway does, and there is
					// no separate field for it: spec.deployment.imagePullSecrets
					// and IMAGE_PULL_SECRETS cover every pod the operator renders.
					ImagePullSecrets: resolveImagePullSecrets(agent.Spec.Deployment),
					Containers:       containers,
					Volumes:          volumes,
				},
			},
			// Two claims, because one of them must be unreachable from the account
			// that can write the other. See shellSandboxSshdPath.
			//
			// VolumeClaimTemplates is immutable, so an install that already has a
			// sandbox needs its StatefulSet deleted (--cascade=orphan keeps the
			// pod up meanwhile) before the operator can lay this down. The feature
			// is experimental and off by default, which is what makes that
			// acceptable rather than a migration.
			VolumeClaimTemplates: []corev1.PersistentVolumeClaim{
				{
					// Sized from the agent's own /opt/data claim, not independently.
					// sandbox_mirror.py copies a subset of that volume into this one
					// on upgrade, so matching them makes the migration fit by
					// construction — see agentDataStorageSize.
					ObjectMeta: metav1.ObjectMeta{Name: shellSandboxDataVolume},
					Spec: corev1.PersistentVolumeClaimSpec{
						AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
						Resources: corev1.VolumeResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceStorage: resource.MustParse(agentDataStorageSize),
							},
						},
					},
				},
				{
					// Two host keys and nothing else, so this is a minimum-size
					// request rather than a sized one; the CSI driver rounds it up
					// to whatever the storage class's disk type allows.
					ObjectMeta: metav1.ObjectMeta{Name: shellSandboxSshdVolume},
					Spec: corev1.PersistentVolumeClaimSpec{
						AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
						Resources: corev1.VolumeResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceStorage: resource.MustParse("1Gi"),
							},
						},
					},
				},
			},
		},
	}
}

// buildShellSandboxContainers is the shell, and only the shell.
//
// One container, because the credential runtime is never here. A broker in this
// pod is a broker on the model's loopback, and everything that pod can reach is
// reachable from the shell along with it — see credentialProxySandboxURL.
func buildShellSandboxContainers(agent *agentv1alpha1.PlatformAgent, env []corev1.EnvVar) []corev1.Container {
	return []corev1.Container{buildShellSandboxContainer(agent, env)}
}

// buildShellSandboxVolumes is the shell's own set.
//
// Nothing credential-bearing is in it but the audience-bound token the shell
// presents to the broker, which buys the caller nothing except the right to ask.
// The broker's kubeconfig, gcloud configuration and federated token are in
// another pod.
func buildShellSandboxVolumes(agent *agentv1alpha1.PlatformAgent, authorizedKeysSecret string) []corev1.Volume {
	volumes := []corev1.Volume{{
		Name: shellSandboxKeysVolume,
		VolumeSource: corev1.VolumeSource{
			Secret: &corev1.SecretVolumeSource{
				SecretName: authorizedKeysSecret,
				// Only this key. The Secret is the agent's, and the
				// sandbox has no business seeing the private half
				// if it ever ends up stored alongside.
				Items: []corev1.KeyToPath{{Key: "authorized_keys", Path: "authorized_keys"}},
			},
		},
	}, {
		Name: shellSandboxSettingsVolume,
		VolumeSource: corev1.VolumeSource{
			ConfigMap: &corev1.ConfigMapVolumeSource{
				LocalObjectReference: corev1.LocalObjectReference{
					Name: settingsConfigMapName(agent),
				},
				// Optional, unlike the agent container's copy. The
				// reconciler writes this ConfigMap before it builds
				// the StatefulSet, but the two are separate objects
				// and a sandbox that cannot start because one of them
				// is briefly missing takes the agent's whole shell
				// with it. A skill reading an absent SETTINGS.md
				// fails on its own terms.
				Optional: ptr.To(true),
			},
		},
	}, {
		// See shellSandboxHermesHomePath. Empty and read-only: the point is the
		// mount, not what is behind it.
		Name:         shellSandboxHermesHomeVolume,
		VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
	}, buildShellSandboxCredentialProxyTokenVolume()}
	return volumes
}

// buildShellSandboxCredentialProxyTokenVolume projects the token the shell
// presents to the credential runtime.
//
// The agent pod's equivalent is buildAgentCredentialProxyTokenVolume, and the two
// differ in one thing: the mode. That one projects 0400 into a container running
// as the uid kubelet writes the file as; here the file is read by uid 1000, the
// login the model's commands run under, so 0400 would leave it unreadable by the
// only process that needs it. 0444 gives away nothing this container is not
// already holding — every process in it belongs to the model, and the credential
// exists to be spent on the model's behalf. What keeps it from being a general
// Kubernetes credential is the audience, not the mode.
func buildShellSandboxCredentialProxyTokenVolume() corev1.Volume {
	return corev1.Volume{
		Name: shellSandboxCredentialProxyTokenVolume,
		VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: ptr.To(int32(0444)),
			Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
				Audience: credentialProxyAudience, ExpirationSeconds: ptr.To(int64(3600)), Path: "token",
			}}},
		}},
	}
}

func buildShellSandboxContainer(agent *agentv1alpha1.PlatformAgent, env []corev1.EnvVar) corev1.Container {
	return corev1.Container{
		Name:  "shell",
		Image: resolveShellSandboxImage(agent),
		// No command or args: the image's entrypoint does the
		// volume-dependent setup and execs sshd. An earlier prototype
		// carried all of it as a heredoc in the pod spec, where no
		// linter or test could reach it.
		Ports: []corev1.ContainerPort{{
			Name:          "ssh",
			ContainerPort: shellSandboxPort,
		}},
		// The two hardening settings that hold with sshd's privilege
		// separation. The pod-level comment in buildShellSandboxStatefulSet
		// says why runAsNonRoot and a full capability drop are not here;
		// these two are not part of that question.
		//
		// RuntimeDefault is the container runtime's own seccomp filter. It
		// is what an unconfined pod would get if anyone had set it, and it
		// leaves fork, setuid, setgid and chroot — everything privilege
		// separation and the entrypoint's chown need — while removing the
		// syscalls a container has no business making. This is the pod in
		// the install where every model-authored command runs, so an
		// unconfined seccomp profile here is the one that matters most.
		//
		// NET_RAW goes because nothing in the sandbox uses it: sshd does
		// not, and neither do the wrapped CLIs, which speak TCP to the
		// broker. What it buys is that a command running in here cannot
		// open a raw socket, so it cannot forge or sniff packets on the
		// pod network — the capability behind ARP and DNS spoofing, and
		// the one Kubernetes' own baseline profile singles out.
		SecurityContext: &corev1.SecurityContext{
			SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
			Capabilities:   &corev1.Capabilities{Drop: []corev1.Capability{"NET_RAW"}},
		},
		Env: env,
		ReadinessProbe: &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{
				TCPSocket: &corev1.TCPSocketAction{Port: intstr.FromInt32(shellSandboxPort)},
			},
			InitialDelaySeconds: 5,
			PeriodSeconds:       5,
		},
		// Requests and limits on every container, always: the
		// platform-baseline-quota in kubeagents-system rejects a pod
		// that omits them, and the rejection surfaces as a StatefulSet
		// that never creates a pod.
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("200m"),
				corev1.ResourceMemory: resource.MustParse("512Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("2"),
				corev1.ResourceMemory: resource.MustParse("2Gi"),
			},
		},
		VolumeMounts: []corev1.VolumeMount{
			{Name: shellSandboxKeysVolume, MountPath: shellSandboxKeysPath, ReadOnly: true},
			{Name: shellSandboxDataVolume, MountPath: shellSandboxDataPath},
			{Name: shellSandboxSshdVolume, MountPath: shellSandboxSshdPath},
			{
				Name:      shellSandboxHermesHomeVolume,
				MountPath: shellSandboxHermesHomePath,
				ReadOnly:  true,
			},
			{
				Name:      shellSandboxCredentialProxyTokenVolume,
				MountPath: credentialProxyTokenMountPath,
				ReadOnly:  true,
			},
			{
				// The one file in the delivery set the image cannot
				// carry: SETTINGS.md is per-install, rendered by the
				// operator from the CR, and six skills read it by
				// path. The image stages skills, SOPs and shared
				// scripts at /opt/defaults for the entrypoint to sync
				// (deploy/sandbox/Dockerfile); this arrives the way
				// the agent container gets the same file, as a subPath
				// mount over its own data volume.
				//
				// subPath, so the ConfigMap lands as a single file
				// rather than replacing the directory. The cost is
				// that it does not track ConfigMap updates — a
				// subPath mount is resolved once at container start —
				// which matches the agent container's behaviour, where
				// a settings change already means a restart.
				Name:      shellSandboxSettingsVolume,
				MountPath: path.Join(shellSandboxDataPath, settingsFileName),
				SubPath:   settingsFileName,
				ReadOnly:  true,
			},
		},
	}
}

// buildShellSandboxNetworkPolicy is deny-by-default in both directions, with
// three holes: ssh in from the gateway, DNS out, and the broker's Service out.
//
// Agent Sandbox ships an equivalent as its GKE default; not taking the CRD means
// writing it, and this is the one part of that reversal that is real work rather
// than a rename. It is inert on any cluster without a NetworkPolicy
// implementation, so it is a control where one is enforced and documentation
// everywhere else.
//
// The narrow egress is what the broker's separate pod buys. A NetworkPolicy
// selects pods, so a broker sharing this pod would make every address it needs —
// a GKE control plane per registered cluster, googleapis.com, chat.googleapis.com,
// slack.com, github.com, the token minter — an address the *shell* may reach.
// Off-pod, the shell's whole outbound world is DNS and one ClusterIP.
// dnsIPs is the resolved Cluster DNS VIP list, the same one the gateway policy is
// built from; an empty slice falls back to the standard VIP inside clusterDNSPeers.
func buildShellSandboxNetworkPolicy(agent *agentv1alpha1.PlatformAgent, dnsIPs []string) *networkingv1.NetworkPolicy {
	tcp := corev1.ProtocolTCP
	udp := corev1.ProtocolUDP
	gateway := map[string]string{"app": agent.Name + "-gateway"}

	ingress := []networkingv1.NetworkPolicyIngressRule{{
		// Only the agent pod may open a shell, and only on sshd's port.
		From: []networkingv1.NetworkPolicyPeer{{
			PodSelector: &metav1.LabelSelector{MatchLabels: gateway},
		}},
		Ports: []networkingv1.NetworkPolicyPort{{
			Protocol: &tcp,
			Port:     ptr.To(intstr.FromInt32(shellSandboxPort)),
		}},
	}}

	egress := []networkingv1.NetworkPolicyEgressRule{{
		// Cluster DNS. Without it the sandbox cannot resolve the credential
		// proxy, and every wrapper fails with a name error that looks like the
		// proxy being down — which is exactly what a live install did when this
		// rule named the kube-dns podSelector alone and the cluster ran NodeLocal
		// DNSCache. clusterDNSPeers is the gateway policy's peer list, so the two
		// policies cannot drift apart into that failure again.
		To: clusterDNSPeers(dnsIPs),
		Ports: []networkingv1.NetworkPolicyPort{
			{Protocol: &udp, Port: ptr.To(intstr.FromInt32(53))},
			{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(53))},
		},
	}}

	egress = append(egress, networkingv1.NetworkPolicyEgressRule{
		// The credential proxy in a pod of its own. This is the connection
		// every wrapped CLI in the sandbox makes.
		To: []networkingv1.NetworkPolicyPeer{{
			PodSelector: &metav1.LabelSelector{MatchLabels: credentialProxySelector(agent)},
		}},
		Ports: []networkingv1.NetworkPolicyPort{{
			Protocol: &tcp,
			Port:     ptr.To(intstr.FromInt32(credentialProxyPort)),
		}},
	})

	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      shellSandboxName(agent),
			Namespace: agent.Namespace,
			Labels:    shellSandboxSelector(agent),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: shellSandboxSelector(agent)},
			// Both types listed even though each has rules below: naming a type
			// with no rule is what makes it deny-all, and a later edit that
			// removes the last egress rule must not silently open egress.
			PolicyTypes: []networkingv1.PolicyType{
				networkingv1.PolicyTypeIngress,
				networkingv1.PolicyTypeEgress,
			},
			Ingress: ingress,
			Egress:  egress,
		},
	}
}

// buildShellSandboxClientKeyVolumes returns the agent pod's half of the keypair:
// the Secret holding the private key, and an emptyDir the init container below
// copies it into.
//
// Two volumes because one does not work, and the reason is worth stating rather
// than rediscovering. `ssh -i` refuses a private key with any group or other
// permission bit set, and a Secret volume's files are owned by root — the agent
// pod runs as uid 10000 under runAsNonRoot. That leaves no mode that satisfies
// both: 0400 is unreadable by the agent, and 0440 is refused by ssh. Every
// combination fails at connection time with a message about permissions, which
// reads like a bad key and sends the reader to the sandbox.
//
// So the Secret is mounted world-readable *within this pod* — which changes
// nothing, since the pod is the key's legitimate holder — and copied to an
// emptyDir where the copy is owned by the uid that made it.
func buildShellSandboxClientKeyVolumes() []corev1.Volume {
	return []corev1.Volume{
		{
			Name: shellSandboxClientKeySecretVolume,
			VolumeSource: corev1.VolumeSource{
				Secret: &corev1.SecretVolumeSource{
					SecretName: defaultPlatformAgentSecrets,
					Items: []corev1.KeyToPath{{
						Key:  shellSandboxPrivateKeySecretKey,
						Path: shellSandboxClientKeyFile,
					}},
					DefaultMode: ptr.To(int32(0444)),
					// Optional so that an install predating the keypair keeps
					// starting: it gets an empty directory and the init container
					// says so, which leaves an agent that cannot reach its shell
					// but can be read and fixed. A required mount would hold the
					// whole pod in CreateContainerConfigError instead, where the
					// missing Secret key is not in any log the operator reads.
					// upgrade.sh mints the pair on an install that lacks it.
					Optional: ptr.To(true),
				},
			},
		},
		{
			Name:         shellSandboxClientKeyVolume,
			VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
		},
	}
}

// buildShellSandboxClientKeyInitContainer copies the private key into place with
// the ownership and mode ssh insists on. See buildShellSandboxClientKeyVolumes
// for why a plain Secret mount cannot do this.
//
// It runs as the pod's uid, so `install` produces a file owned by the account
// that will read it. Missing key is not an error: the container logs and exits 0,
// leaving an empty directory behind, because the sandbox is opt-in and an install
// that has not provisioned a keypair is not broken.
func buildShellSandboxClientKeyInitContainer(image string) corev1.Container {
	return corev1.Container{
		Name:            "sandbox-ssh-key",
		Image:           image,
		ImagePullPolicy: corev1.PullIfNotPresent,
		Command:         []string{"/bin/sh", "-c"},
		Args: []string{fmt.Sprintf(
			`set -eu
if [ -r %[1]s/%[3]s ]; then
  install -m 0600 %[1]s/%[3]s %[2]s/%[3]s
  echo "sandbox ssh key staged at %[2]s/%[3]s"
else
  echo "no %[4]s in the agent credentials Secret; the shell sandbox will be unreachable"
fi`,
			shellSandboxClientKeySecretPath,
			shellSandboxClientKeyPath,
			shellSandboxClientKeyFile,
			shellSandboxPrivateKeySecretKey,
		)},
		VolumeMounts: []corev1.VolumeMount{
			{Name: shellSandboxClientKeySecretVolume, MountPath: shellSandboxClientKeySecretPath, ReadOnly: true},
			{Name: shellSandboxClientKeyVolume, MountPath: shellSandboxClientKeyPath},
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("10m"),
				corev1.ResourceMemory: resource.MustParse("16Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("100m"),
				corev1.ResourceMemory: resource.MustParse("64Mi"),
			},
		},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false),
			ReadOnlyRootFilesystem:   ptr.To(true),
			Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
	}
}

// buildShellSandboxClientKeyMount is the read-only view of the staged key that
// the agent container gets. Only the emptyDir: the container that talks to the
// sandbox has no reason to see the Secret mount the init container read.
func buildShellSandboxClientKeyMount() corev1.VolumeMount {
	return corev1.VolumeMount{
		Name:      shellSandboxClientKeyVolume,
		MountPath: shellSandboxClientKeyPath,
		ReadOnly:  true,
	}
}
