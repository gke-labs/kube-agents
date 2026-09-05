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

package v1alpha1

import (
	"fmt"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// SensitiveEnvVars defines environment variables that are sensitive and cannot be
// overridden by user Deployment specs or injected into the credential proxy.
//
// Membership does two things, and both are needed: the validating webhook
// rejects a spec.deployment.env entry with one of these names, and
// mergeCredentialProxyEnv drops it. The webhook alone is not enough because
// the chart's default failurePolicy is Ignore, so an unreachable webhook
// admits the object with validation skipped; the drop is what actually holds,
// and the rejection is what tells the operator why.
var SensitiveEnvVars = map[string]struct{}{
	"API_SERVER_KEY": {},
	// Not a secret, unlike its neighbours: this is the read-only gate, and
	// setting it to "false" disables every refusal the credential proxy makes
	// for every command, agent and cluster in the Pod. It was already dropped
	// silently on the way to the sidecar, which left an operator patching the
	// CR, seeing it accepted, and getting no behaviour change and no
	// explanation. Naming it here turns that into a field.Forbidden on
	// spec.deployment.env[i].name.
	"CREDENTIAL_PROXY_ENFORCE_READ_ONLY": {},
	"HERMES_HOME":                        {},
}

type HermesSpec struct {
	// DashboardEnabled toggles the AGENT_DASHBOARD environment variable.
	// +kubebuilder:default=true
	// +optional
	DashboardEnabled *bool `json:"dashboardEnabled,omitempty"`

	// PluginsDebug toggles the AGENT_PLUGINS_DEBUG environment variable.
	// +kubebuilder:default=false
	// +optional
	PluginsDebug *bool `json:"pluginsDebug,omitempty"`

	// AgentHome is the path to the AGENT_HOME directory.
	// +kubebuilder:default="/opt/data"
	// +optional
	AgentHome string `json:"agentHome,omitempty"`

	// ApiServerSecretRef references the Secret key holding API_SERVER_EXTERNAL_KEY,
	// the credential outside callers present to the credential-proxy sidecar. It
	// does not set API_SERVER_KEY: the value the Hermes API server itself validates
	// is the non-secret loopback sentinel `cluster-internal-trusted`, a compile-time
	// constant the sidecar swaps in once it has authenticated the caller.
	// +optional
	ApiServerSecretRef *corev1.SecretKeySelector `json:"apiServerSecretRef,omitempty"`

	// SessionKVApiKeySecretRef references the Secret key holding the bearer
	// token for the pod-local Session KV server on port 8699. Distinct from
	// API_SERVER_KEY, which is that loopback sentinel and would authenticate
	// nothing here.
	// +optional
	SessionKVApiKeySecretRef *corev1.SecretKeySelector `json:"sessionKVApiKeySecretRef,omitempty"`

	// SessionKVSaltSecretRef references the Secret key holding the HMAC salt
	// used to pseudonymise chat identities before they reach session metadata,
	// audit logs, or OTel spans. When absent the agent generates a per-pod salt
	// and logs a warning: hashes then stop correlating across restarts.
	// +optional
	SessionKVSaltSecretRef *corev1.SecretKeySelector `json:"sessionKVSaltSecretRef,omitempty"`
}

// HarnessSpec configures the core execution environment and framework-level settings for the agent.
// This extracts environmental context that doesn't belong in infrastructure blocks.
type HarnessSpec struct {
	// ClusterName is the logical name of the cluster (either where the agent is running or the target cluster).
	// +required
	ClusterName string `json:"clusterName,omitempty"`

	// Location is the geographical location or cloud region.
	// +required
	Location string `json:"location,omitempty"`

	// ProjectID is the GCP Project ID of the cluster.
	// Required alongside ClusterName and Location: the credential proxy only
	// renders its bootstrap (the `gcloud container clusters get-credentials`
	// that gives the agent a usable kubectl context) when all three are set.
	// Omitting it leaves every kubectl call in the sidecar pointed at
	// localhost:8080. See buildCredentialProxyEnv.
	// +required
	ProjectID string `json:"projectId,omitempty"`

	// Hermes configures the internal event-routing or agent framework.
	// +optional
	Hermes *HermesSpec `json:"hermes,omitempty"`

	// Memory configures agent memory settings.
	// +optional
	Memory *MemorySpec `json:"memory,omitempty"`

	// EventWatcher configures cluster event ingestion — the k8s-event-watcher that
	// turns cluster warnings into autonomous triage sessions. Its `enabled: false`
	// is the emergency stop for an event storm.
	// +optional
	EventWatcher *EventWatcherSpec `json:"eventWatcher,omitempty"`

	// Tuning sets per-persona execution limits. Unset values keep the defaults
	// baked into the agent image.
	// +optional
	Tuning *TuningSpec `json:"tuning,omitempty"`

	// Experimental holds opt-in behaviour that is not supported and may change
	// or disappear in any release.
	// +optional
	Experimental *ExperimentalSpec `json:"experimental,omitempty"`
}

// ExperimentalSpec gathers the unsupported switches. Nothing here carries a
// compatibility promise: a field may change meaning, change default, or be
// removed outright between releases, and an install that depends on one is
// expected to be re-checked at every upgrade. Fields belong here while the
// question they answer is still open — once the answer is settled the switch
// either graduates into a supported spec block or goes away.
type ExperimentalSpec struct {
	// PlatformFrontDoor makes the Platform Agent the profile the Hermes gateway
	// runs as, so chat messages are handled by it directly instead of arriving
	// at the Chat Agent, which delegates through the router and the kanban board.
	//
	// The trade is the Chat Agent's whole reason for existing: its lockdown (a
	// router with three toolsets) is what keeps an inbound message from reaching
	// the full Platform Agent tool surface before a card and a worker turn have
	// framed it. With this on, an inbound message reaches that surface directly.
	//
	// One gateway means one profile, so this is not additive: while it is on, the
	// Chat Agent persona sees no chat at all.
	// +kubebuilder:default=false
	// +optional
	PlatformFrontDoor *bool `json:"platformFrontDoor,omitempty"`

	// ShellSandbox tunes the agent's shell sandbox — the separate pod it reaches
	// over SSH, where every command it runs executes. The sandbox itself is not
	// optional; this block sets its image and its container runtime. See
	// docs/designs/agent-shell-sandboxing.md.
	// +optional
	ShellSandbox *ShellSandboxSpec `json:"shellSandbox,omitempty"`
}

// ShellSandboxSpec configures the per-agent shell sandbox: a StatefulSet running
// sshd, which Hermes' `ssh` terminal backend points at. Every tool that reaches a
// shell — terminal, the four file tools, and execute_code — follows the backend,
// so the agent's whole execution surface is in that pod.
//
// Experimental because the shape of the spec is still open — whether the sandbox
// belongs to this operator at all is listed as unresolved in the design.
type ShellSandboxSpec struct {
	// Enabled is retained so that an install carrying `enabled: true` still
	// applies, and so that `enabled: false` is refused rather than silently
	// ignored. The sandbox is not optional: the agent image ships no kubectl,
	// gcloud, gh or git, so an agent without a sandbox has no shell tools at
	// all, and running them back in the gateway pod is the arrangement this
	// design exists to end. Setting it false parks the agent Degraded with
	// reason ShellSandboxCannotBeDisabled and changes nothing about the running
	// workload.
	//
	// The sandbox needs a keypair in the agent's credential Secret
	// (SANDBOX_SSH_PRIVATE_KEY) and its public half in
	// <agent>-shell-authorized-keys. Every install surface mints both; an
	// install that predates them gets them from upgrade.sh. With no key, the
	// sandbox runs and the agent cannot log into it.
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// Image overrides the sandbox image. Empty takes the operator's own default,
	// which the install surfaces set from the same tag inventory as every other
	// image (AGENT_SANDBOX_IMAGE).
	// +optional
	Image string `json:"image,omitempty"`

	// RuntimeClassName runs the sandbox pod under a sandboxed container runtime,
	// `gvisor` being the one GKE offers. This is a second boundary and not the
	// one the sandbox is built on: unbinding the ServiceAccount is what takes the
	// cloud credential away, and a shell running as a different pod is what takes
	// the agent's filesystem away. What this adds is protection of the node from
	// the code the model runs, by putting a user-space kernel between that code
	// and the host's syscall surface.
	//
	// Separate from deployment.availability.runtimeClassName, which governs the
	// agent pod, because the two pods do not want the same answer. The agent pod
	// holds WAL-mode SQLite, which gVisor corrupts on the gofer-backed mount
	// (#610); the sandbox pod holds none. Splitting the field is what lets an
	// install sandbox the untrusted pod without sandboxing the trusted one.
	//
	// On GKE Standard this needs a node pool created with `--sandbox
	// type=gvisor`; the operator reports Degraded rather than leaving the pod
	// Pending if the named RuntimeClass does not exist. GKE adds the node pool's
	// taint toleration itself, so nothing else has to be set here.
	// +optional
	RuntimeClassName *string `json:"runtimeClassName,omitempty"`
}

// EventWatcherSpec configures the k8s-event-watcher, which runs as a peer service
// inside the credential-proxy sidecar alongside Envoy and the credential runtime.
// It streams warning events from every watched cluster, deduplicates them, and posts
// each surviving incident to the pod-local Session KV server, which starts an
// autonomous triage session for it.
type EventWatcherSpec struct {
	// Enabled controls whether the watcher is started at all. Absent means started:
	// the watcher is how a fleet notices its own incidents, so only an explicit
	// false turns it off.
	//
	// This is an emergency stop, not a tuning knob. It exists for the case where
	// events arrive faster than the agent can triage them — a fleet-wide rollout
	// gone wrong, a node pool flapping — and the cheapest way to get the agent back
	// is to cut the inflow rather than to chase the cards it has already been given.
	// It is all-or-nothing across every watched cluster: the watcher's reason and
	// namespace filters are fixed by the sidecar's entrypoint and not exposed here,
	// so there is no way to silence one noisy namespace through this field.
	//
	// Three consequences to know before pressing it:
	//
	//   - It rolls the pod. The value reaches the sidecar as an environment variable,
	//     so changing it rewrites the pod template. During a storm that restart is
	//     usually wanted anyway — it is also what ends the sessions already running.
	//   - It stops the inflow only. Kanban cards and sessions created from events
	//     already delivered keep running and still have to be dealt with on the board.
	//   - Nothing turns it back on. An install left with the watcher off has no
	//     incident detection at all while the container stays Ready, which is why the
	//     operator reports the off state as an `EventWatcher` condition on the CR
	//     instead of letting it sit unremarked in the spec.
	// +kubebuilder:default=true
	// +optional
	Enabled *bool `json:"enabled,omitempty"`
}

// TuningSpec carries execution limits per agent persona.
//
// Keys are personas, not profile names, because the profiles they map to are not all
// known when the CR is written: cluster profiles are scaffolded at runtime, one per
// managed cluster, with generated names like `cluster-<project>-<cluster>-<region>`.
// `Cluster` therefore applies to every `cluster-*` profile rather than to one of them.
type TuningSpec struct {
	// Default applies to the `default` profile — the Chat Agent front door. Delivered
	// as a config overlay merged into that profile at pod startup, like the others.
	// +optional
	Default *AgentLimits `json:"default,omitempty"`

	// Platform applies to the `platform` profile (the Platform Agent). Delivered as a
	// config overlay merged into that profile at pod startup.
	// +optional
	Platform *AgentLimits `json:"platform,omitempty"`

	// Cluster applies to every `cluster-*` profile (the Cluster Agents). Delivered as a
	// single class overlay, merged into each existing cluster profile at pod startup and
	// into a new one when it is scaffolded — onboarding a cluster does not roll the pod,
	// so a profile created between two starts has to pick the overlay up itself.
	// +optional
	Cluster *AgentLimits `json:"cluster,omitempty"`

	// MaxInProgress caps how many kanban workers run concurrently across the whole
	// board. It is board-wide rather than per-persona: there is one dispatcher, and
	// every worker it spawns — platform and cluster alike — draws on the same model
	// quota. Setting it to 1 serialises all delegated work.
	//
	// Unset means 2, the operator's default — not Hermes' own behaviour, which does not
	// cap concurrency at all. The default exists because a worker is a full agent process
	// holding a few hundred MiB for the length of the task: unbounded dispatch lets a
	// burst of queued cards spawn workers until the cgroup OOM killer takes them, and
	// that kills a child process rather than the container, so it produces no Kubernetes
	// event and no restart while the dispatcher strands the card instead of retrying it.
	//
	// The cap is bought at a real price, so raise it deliberately rather than leaving it
	// alone by default. A slot is held for a worker's entire run, so capping serialises
	// minutes of model work: measured against real fan-outs on a live cluster, capping at
	// 2 roughly doubled the time for a batch to finish. Do NOT reach for a lower value as
	// a latency fix — an uncapped fan-out does spawn every sandboxed worker at once and
	// they contend during startup, but a cap trades minutes of model work for seconds of
	// boot. What the workers contend for is not established either — CPU limit, memory
	// ceiling and gVisor I/O all fit the evidence, and gVisor hides the cgroup throttle
	// counters that would settle it — so raising resources is not a guaranteed fix;
	// measure it.
	//
	// Set it higher once a deployment has measured its own worker footprint and model
	// quota — a fleet with headroom is throttled by 2. Set it to 1 to serialise all
	// delegated work. When quota rather than memory binds, note the related failure mode:
	// workers that exhaust their retry budget exit without calling a terminal kanban
	// tool, and the dispatcher reports that as a "protocol violation" rather than as the
	// quota exhaustion it actually is.
	// +kubebuilder:validation:Minimum=1
	// +optional
	MaxInProgress *int `json:"maxInProgress,omitempty"`
}

// AgentLimits bounds a single agent run. Both limits exist because they fail the same
// way — the run stops mid-task without calling a terminal kanban tool, which the
// dispatcher then records as a "protocol violation" regardless of the real cause.
type AgentLimits struct {
	// APIMaxRetries is how many times a failed model call is retried before the run
	// gives up. Hermes defaults to 3, which suits an interactive session where a human
	// can retry; a background worker has nobody to retry it, so a transient burst of
	// upstream 429s or 503s ends the run.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=100
	// +optional
	APIMaxRetries *int `json:"apiMaxRetries,omitempty"`

	// MaxTurns is how many iterations (model calls) a single turn may take. Hermes
	// defaults to 90. A long multi-step task can exhaust it while still mid-flight, and
	// a run that does cannot even produce a closing summary. Repository exploration is
	// the main consumer, so size this against how much the agent has to read, not
	// against how complex the request is.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=1000
	// +optional
	MaxTurns *int `json:"maxTurns,omitempty"`
}

// MemorySpec configures memory and user profile settings for the agent framework.
type MemorySpec struct {
	// MemoryEnabled toggles framework memory persistence.
	// +kubebuilder:default=false
	// +optional
	MemoryEnabled *bool `json:"memoryEnabled,omitempty"`

	// Provider selects the memory provider plugin. Two ship in the agent image:
	// "multiuser_memory" — the default, for small or personal deployments — keeps a
	// per-user Markdown file inside the pod and needs nothing else running, at the
	// price of loading the whole store into the model's context on every turn, and
	// "kube_agents_memory" — for enterprise deployments — gives ranked recall backed
	// by the in-cluster Hindsight service and its Postgres database. Any other
	// plugin Hermes ships may be named here too.
	//
	// The file store is the default because it is what this API shipped before
	// "kube_agents_memory" existed. A CR written against the older schema omits this
	// field, and taking the default must leave that agent with the store it already
	// has rather than pointing it at a Hindsight service nobody deployed.
	//
	// Use "none" for no external provider at all. That is not the same as leaving
	// this field empty: an absent field takes the default below, so "none" is the
	// only way to express the choice. The operator translates it to the empty
	// string Hermes itself uses.
	//
	// Only a Hindsight-backed provider reaches the specialist profiles, and only
	// read-only; see memoryOverlay in the controller for why.
	// +kubebuilder:default="multiuser_memory"
	// +optional
	Provider string `json:"provider,omitempty"`

	// UserProfileEnabled toggles per-user memory profiling.
	// +kubebuilder:default=false
	// +optional
	UserProfileEnabled *bool `json:"userProfileEnabled,omitempty"`
}

// DeploymentSpec abstracts the Kubernetes Pod/Deployment configuration,
// completely decoupling the compute payload from the agent's application logic.
type DeploymentSpec struct {
	// Image specifies the container image repository.
	// +optional
	Image string `json:"image,omitempty"`

	// Tag specifies the container image tag. It applies only when Image is set
	// without a tag or digest, and falls back to "latest" there. When Image is
	// omitted entirely, the operator's default platform-agent version applies
	// instead, so no "latest" default is persisted on the CR.
	// +optional
	Tag *string `json:"tag,omitempty"`

	// ImagePullPolicy specifies if the image should be pulled.
	// +kubebuilder:default=IfNotPresent
	// +kubebuilder:validation:Enum=Always;Never;IfNotPresent
	// +optional
	ImagePullPolicy *corev1.PullPolicy `json:"imagePullPolicy,omitempty"`

	// Note, deliberately not a doc comment — the blank line below keeps it out of
	// the CRD description that `kubectl explain` prints. listType is atomic rather
	// than the map Env and Sidecars use below: a list-map key has to be a required
	// field, and corev1.LocalObjectReference's Name is optional, so a map marker
	// here yields a CRD the API server rejects. That same optionality is why the
	// webhook checks each name is non-empty and distinct, and why the controller
	// normalizes the list before building the pod: nothing below either layer
	// does. An empty name reaches the kubelet, which pulls anonymously; a repeat
	// makes every apply of the generated Deployment fail, PodSpec's own
	// imagePullSecrets being a server-side-apply list-map keyed on name.

	// ImagePullSecrets references Secrets in the agent's namespace holding
	// registry credentials, for installs whose mirror needs authenticating to
	// (Harbor, Artifactory) rather than being readable with the nodes' own
	// credentials. The Secrets are referenced, not created: each must already
	// exist in the agent's namespace when the pod is scheduled.
	//
	// One pod means one pull identity — Kubernetes has no per-container split —
	// so this covers every image in the pod: the agent, the credential-proxy and
	// fluent-bit sidecars, any initContainers or sidecars set alongside, and the
	// OCI image volumes AgentPlugins mount.
	//
	// Setting this REPLACES the operator's IMAGE_PULL_SECRETS default rather than
	// adding to it, on the same terms as Image against PLATFORM_AGENT_IMAGE. A CR
	// that names its own registry identity is stating it completely, and a
	// silently merged fleet default would hand the kubelet credentials this agent
	// never asked for.
	// +listType=atomic
	// +optional
	ImagePullSecrets []corev1.LocalObjectReference `json:"imagePullSecrets,omitempty"`

	// BrowserArgs specifies custom command-line arguments to pass to the agent's browser (e.g. --no-sandbox).
	// +optional
	BrowserArgs []string `json:"browserArgs,omitempty"`

	// Env is a list of environment variables to set in the container
	// +listType=map
	// +listMapKey=name
	// +optional
	Env []corev1.EnvVar `json:"env,omitempty"`

	// InitContainers specifies standard Kubernetes initContainers to run before the agent starts.
	// +listType=map
	// +listMapKey=name
	// +optional
	InitContainers []corev1.Container `json:"initContainers,omitempty"`

	// Sidecars specifies standard Kubernetes sidecar/application containers to run alongside the agent.
	// +listType=map
	// +listMapKey=name
	// +optional
	Sidecars []corev1.Container `json:"sidecars,omitempty"`

	// SidecarVolumes specifies custom volumes to mount for the sidecar containers.
	// +listType=map
	// +listMapKey=name
	// +optional
	SidecarVolumes []corev1.Volume `json:"sidecarVolumes,omitempty"`

	// ExtraVolumes specifies custom volumes to mount for the main container.
	// +listType=map
	// +listMapKey=name
	// +optional
	ExtraVolumes []corev1.Volume `json:"extraVolumes,omitempty"`

	// ExtraVolumeMounts specifies custom volume mounts for the main container.
	// +listType=map
	// +listMapKey=name
	// +optional
	ExtraVolumeMounts []corev1.VolumeMount `json:"extraVolumeMounts,omitempty"`

	// PodAnnotations specifies custom annotations to apply to the generated Pod template.
	// +optional
	PodAnnotations map[string]string `json:"podAnnotations,omitempty"`

	// ScaleToZero scales the deployment replicas to 0 when true (useful for saving costs during idle periods).
	// +optional
	ScaleToZero *bool `json:"scaleToZero,omitempty"`

	// Availability configures high availability and scheduling settings for the agent pod.
	// +optional
	Availability *AvailabilitySpec `json:"availability,omitempty"`

	// Resources specifies resource requests and limits for the main container.
	// +optional
	Resources *corev1.ResourceRequirements `json:"resources,omitempty"`

	// DefaultStorageClassName specifies the default storage class to use for the system and data PVCs.
	// +optional
	DefaultStorageClassName *string `json:"defaultStorageClassName,omitempty"`

	// Storages specifies extra custom PersistentVolumeClaims to provision and mount for the agent pod.
	// +listType=map
	// +listMapKey=name
	// +optional
	Storages []StorageSpec `json:"storages,omitempty"`
}

// StorageSpec defines custom PersistentVolumeClaim and volume mount configuration.
type StorageSpec struct {
	// Name specifies the PersistentVolumeClaim name.
	// +required
	Name string `json:"name"`

	// StorageClassName specifies the storage class name for this volume claim.
	// +optional
	StorageClassName *string `json:"storageClassName,omitempty"`

	// AccessModes specifies the requested access modes (e.g. ReadWriteOnce, ReadWriteMany).
	// +optional
	AccessModes []corev1.PersistentVolumeAccessMode `json:"accessModes,omitempty"`

	// StorageSize specifies the requested storage capacity (e.g. 5Gi, 20Gi).
	// +kubebuilder:default="5Gi"
	// +optional
	StorageSize string `json:"storageSize,omitempty"`

	// MountPath specifies the container mount directory path for this volume claim.
	// +optional
	MountPath string `json:"mountPath,omitempty"`

	// SubPath specifies a sub-path within the volume to mount.
	// +optional
	SubPath string `json:"subPath,omitempty"`

	// ReadOnly specifies if the volume should be mounted as read-only.
	// +optional
	ReadOnly bool `json:"readOnly,omitempty"`
}

// AvailabilitySpec defines high availability and scheduling settings.
type AvailabilitySpec struct {
	// Replicas specifies the desired number of pod replicas. If omitted, defaults to 1.
	// +optional
	// +kubebuilder:validation:Minimum=0
	Replicas *int32 `json:"replicas,omitempty"`

	// NodeSelector is a selector which must match a node's labels for the pod to be scheduled
	// +optional
	NodeSelector map[string]string `json:"nodeSelector,omitempty"`

	// Tolerations are tolerations for pod scheduling
	// +optional
	Tolerations []corev1.Toleration `json:"tolerations,omitempty"`

	// Affinity specifies affinity scheduling rules
	// +optional
	Affinity *corev1.Affinity `json:"affinity,omitempty"`

	// RuntimeClassName refers to a RuntimeClass object in the cluster.
	// +optional
	RuntimeClassName *string `json:"runtimeClassName,omitempty"`
}

// SecuritySpec manages Kubernetes RBAC, Pod Security, and Cloud Workload Identity,
// decoupling the operator from being strictly tied to GCP.
type SecuritySpec struct {
	// ServiceAccountName is the Kubernetes Service Account bound to the Deployment.
	// +optional
	ServiceAccountName string `json:"serviceAccountName,omitempty"`

	// ServiceAccountAnnotations specifies custom annotations to apply to the generated ServiceAccount.
	// +optional
	ServiceAccountAnnotations map[string]string `json:"serviceAccountAnnotations,omitempty"`

	// ScopedServiceAccounts maps each GKE cluster the agent may read to the
	// Google service account that reads it. The credential broker mints a
	// short-lived token for the account a request's cluster maps to, instead of
	// using the agent's own identity — which, holding a project-level
	// roles/container.viewer, can read objects in every cluster in the project.
	//
	// Each account is provisioned by Terraform, never by this operator. A
	// controller must not grant authority beyond its requester's, and minting
	// cloud principals inside the loop that is supposed to bound the agent
	// would put the grant on the wrong side of that boundary.
	//
	// As of 2026-08-12 the accounts hold no IAM grant. They were scoped by an
	// IAM Condition on the cluster's resource.name, and that was measured to
	// grant nothing for Kubernetes object operations; removing the condition
	// without removing the grant would have given every account project-wide
	// container.viewer. Authority arrives with per-cluster RBAC, and until it
	// does the pool is off by default.
	//
	// A cluster absent from this list is REFUSED, not served by a wider
	// credential. That is the point of the field, and it is also the first thing
	// an operator will hit: adding a cluster to the fleet without adding it here
	// produces a refusal naming the missing scope.
	//
	// Leaving the list empty keeps the previous behaviour — one identity for
	// every cluster — and renders CREDENTIAL_PROXY_SCOPED_SA_POOL=0 so that the
	// mode a deployment is in can be read off the Deployment rather than
	// inferred from what is absent.
	//
	// Keyed on the cluster tuple by the API server, so a repeated cluster is
	// rejected at admission. Without that a copy-pasted entry whose clusterName
	// was never changed is admitted, reconciles, changes the ConfigMap hash and
	// rolls the broker — which then refuses to start, because the broker will
	// not resolve one cluster to two accounts by taking whichever came last.
	// The failure is a crashloop with the cause several layers away, so it is
	// worth catching in `kubectl apply`. Terraform's scoped_clusters already
	// validates the same thing on its own path.
	// +kubebuilder:validation:MaxItems=100
	// +listType=map
	// +listMapKey=projectId
	// +listMapKey=location
	// +listMapKey=clusterName
	// +optional
	ScopedServiceAccounts []ScopedServiceAccount `json:"scopedServiceAccounts,omitempty"`

	// WorkloadIdentityFederation gives the credential proxy a GCP identity that
	// does not come from the metadata server.
	//
	// Optional hardening rather than a requirement. GKE resolves Workload
	// Identity by pod IP, so the broker's pod holds one cloud identity for
	// whatever runs in it, and what it can mint is whatever that identity may
	// mint. Federation replaces the metadata server with a projected
	// service-account token exchanged for a GCP access token over STS, which
	// narrows the pod to what the provider's attribute conditions allow.
	//
	// Absent means the broker keeps using the metadata server. That is a
	// supported arrangement: the broker has a pod of its own, so the identity is
	// already out of reach of the shell that runs model-authored code.
	// +optional
	WorkloadIdentityFederation *WorkloadIdentityFederationSpec `json:"workloadIdentityFederation,omitempty"`

	// EgressPolicy selects the NetworkPolicy the operator renders for the agent
	// Pod. "None" (the default) renders nothing.
	//
	// "Allowlist" renders a default-deny egress policy that permits only the
	// destinations the agent legitimately needs. Because NetworkPolicy has no
	// deny rule, a destination is denied by not appearing on the list, and the
	// credential API of the link-local metadata server — 169.254.169.254 on TCP
	// 80, where anything that can make an HTTP request can mint the node or
	// Workload Identity service account's tokens — is one of the destinations
	// left off. That address does appear on the list once, on port 53 only:
	// under Cloud DNS for GKE it is the Pod's DNS resolver, and withholding it
	// leaves the agent unable to resolve any of the names the rest of the
	// allowlist is written in. Port 53 reaches no token.
	//
	// READ THIS BEFORE YOU BELIEVE THE NAME. THIS FIELD BLOCKS NOTHING TODAY.
	// Not the metadata server, not anything else. Setting it to Allowlist can
	// only widen what the agent Pod may send, never narrow it.
	//
	// That is not a bug in the rules below; it is what NetworkPolicy does.
	// Policies selecting one Pod are unioned — the Pod may send whatever any of
	// them permits — and the API has no deny rule, so an added policy is a
	// monotone operation. It cannot subtract. The agent Pod is already selected
	// for egress by <name>-gateway-netpol, which the operator renders on every
	// reconcile whether this field is set or not — unless
	// spec.networkPolicy.enabled is false, which withholds the gateway policy.
	// On a Helm install that makes this the Pod's only policy: the one shape
	// where this field enforces for real on an enforcing CNI, denying
	// everything off its list. A Kustomize install still carries the static
	// platform-agent-core-egress set over the same Pod, so the union resumes
	// there. Everywhere else, turning this on leaves the
	// Pod's permitted egress a strict superset of what it was. In the default
	// shape the only destination it adds is the credential broker on TCP 8765
	// — plus, when the agent is not exporting telemetry, the collector
	// namespace on 4317/4318, because the gateway policy omits its own OTel
	// rule in that case and this one is rendered unconditionally. Anything
	// egressAllowlist names is added on top of that.
	//
	// What the gateway policy already permits, and therefore what this cannot
	// take away:
	//
	//   - 169.254.169.254/32 on TCP 80, plus the discovered metadata-daemon port (988 by default) to both link-local
	//     metadata addresses — the pre- and post-DNAT forms of a metadata
	//     request (the 988 rule is suppressed when the resolved metadata
	//     daemon IP is empty). So the metadata path stays open. The same
	//     address is also permitted on port 53, where it is the Cloud DNS for
	//     GKE resolver rather than a credential path.
	//   - TCP 443 to 0.0.0.0/0 minus the private ranges, unless the
	//     FQDNNetworkPolicy annotation is set. So every HTTPS destination on
	//     the public internet stays open, and with it the exfiltration half of
	//     what this control is meant to be.
	//
	// A Kustomize install additionally applies platform-agent-core-egress
	// (deploy/kustomize/platform/networkpolicy-core-egress.yaml), which selects
	// the agent Pod by app.kubernetes.io/name and permits the same metadata
	// path. A Helm install does not carry it, and it changes nothing either
	// way: the gateway policy alone is enough to make the point above.
	//
	// The overlap is deliberate rather than an oversight. Workload Identity
	// needs the metadata path, and <name>-gateway-netpol still permits it to
	// the agent Pod. Narrowing that allowance to the broker Pod, which is the
	// only one that mints a cloud token, is the work that turns this field into
	// a control.
	//
	// So what is this for today? Two things, and they are worth having, but
	// neither is enforcement. It renders an auditable statement of the
	// destinations the agent is supposed to need, in an object an operator can
	// diff and a reviewer can read. And it establishes the field, the refusal
	// rules and the reconcile behaviour, so that narrowing the gateway policy
	// later is a change to one policy rather than a new feature.
	//
	// Three conditions the operator cannot check for you.
	//
	//   - The policy does nothing at all on a cluster whose CNI does not
	//     enforce NetworkPolicy (GKE Standard without network policy enabled);
	//     Autopilot and GKE Dataplane V2 always enforce. An unenforced policy
	//     is stored and returned by kubectl exactly like an enforced one, so
	//     there is nothing for the operator to read.
	//   - Any other policy in the namespace that selects this Pod and permits
	//     wider egress re-opens what this one closes, as the two above do.
	//   - NodeLocal DNSCache, if the cluster runs it, may lose DNS entirely.
	//     It runs hostNetwork, so on Cilium and Dataplane V2 its traffic
	//     carries a host or remote-node identity, which neither the
	//     k8s-app: node-local-dns Pod selector nor the 169.254.20.10/32 CIDR
	//     peer in the rendered DNS rule is guaranteed to match. Both work on
	//     an iptables dataplane, which is why both are rendered. This is the
	//     only one of the three that can take the agent down rather than
	//     quietly weaken it — every allowlisted destination is reached by
	//     name, so no DNS means no egress at all. Check
	//     `kubectl -n kube-system get ds node-local-dns` and confirm
	//     resolution from the agent container after enabling.
	//
	// WHAT IT WILL COST, once the gateway policy is narrowed and this field
	// starts blocking things. None of the following happens today, for the
	// reason above: every destination on this list is one <name>-gateway-netpol
	// still permits to the same Pod. Read it as the bill that falls due, not as
	// the current behaviour — and do not schedule a capability review for a
	// change that will not alter anything yet.
	//
	// The allowlist covers DNS, the credential broker, LiteLLM, the managed
	// OpenTelemetry collector, and whatever egressAllowlist adds. Everything
	// else the agent container reaches on its own would go away:
	//
	//   - DuckDuckGo web search, which the shared default config turns on for
	//     every profile, and the "browser" toolset, which only the Chat Agent
	//     disables;
	//   - the gke and developer_knowledge MCP servers, which proxy
	//     container.googleapis.com and developerknowledge.googleapis.com;
	//   - github.com reached directly from the sandbox, though not the gh and
	//     git wrappers, which go through the broker;
	//   - the metadata lookup in cluster_agent_reconcile.py, which finds that
	//     script's project id. It fails soft after a five-second timeout and
	//     falls back to a broker gcloud call; set RECONCILE_PROJECT to skip it.
	//
	// Those would not be accidental casualties. A headless browser with
	// unrestricted egress is the exfiltration path, so the capabilities this
	// would remove are the same ones that make the control worth having. Restore
	// individual destinations with egressAllowlist.extraRules — noting that
	// NetworkPolicy matches addresses, never DNS names, so restoring a hosted
	// service means naming its address ranges.
	//
	// Credentialed gcloud, kubectl, gh and git are unaffected: they are shims
	// that call the broker, and the broker is on the allowlist.
	//
	// TURNING THIS OFF DOES NOT DELETE THE POLICY. An egress policy is a
	// guardrail, and the operator will not remove a guardrail it may not have
	// created, so setting this back to "None" leaves
	// <name>-sandbox-metadata-deny in place, still denying what it denied. To
	// undo it, set egressPolicy: None first and then
	// `kubectl -n NS delete networkpolicy NAME-sandbox-metadata-deny`. Deleting
	// it while the field still reads "Allowlist" only earns it back on the next
	// reconcile.
	// +kubebuilder:validation:Enum=None;Allowlist
	// +optional
	EgressPolicy string `json:"egressPolicy,omitempty"`

	// EgressAllowlist tunes the destinations egressPolicy: Allowlist permits.
	// Ignored for any other egressPolicy value.
	// +optional
	EgressAllowlist *EgressAllowlistSpec `json:"egressAllowlist,omitempty"`
}

// WorkloadIdentityFederationSpec names the pool the proxy federates through and
// the service account it impersonates once it gets there.
//
// The Helm chart sets both fields from
// platformAgent.security.workloadIdentityFederation, and refuses a half-filled
// block rather than relying on the fail-safe below. What no install surface
// does is create the pool itself: the runnable pool, provider and
// roles/iam.workloadIdentityUser commands are in
// docs/designs/agent-shell-sandboxing.md.
type WorkloadIdentityFederationSpec struct {
	// Audience is the provider's full resource name, in the form STS expects as
	// the `aud` claim:
	//
	//	//iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>
	//
	// The projected token's audience is set from this verbatim. A mismatch is
	// rejected by STS at exchange time with `invalid_target`, not at admission,
	// so it surfaces as a proxy that starts and then fails every command.
	// +kubebuilder:validation:MaxLength=512
	// +kubebuilder:validation:Pattern=`^//iam\.googleapis\.com/projects/[0-9]+/locations/global/workloadIdentityPools/[^/]+/providers/[^/]+$`
	// +optional
	Audience string `json:"audience,omitempty"`

	// ServiceAccountEmail is the GSA the federated principal impersonates.
	//
	// Impersonation rather than direct grants: the agent's roles are already
	// attached to this service account by every install surface, and rebuilding
	// that grant set against a federated principal would mean maintaining it
	// twice. The federated principal therefore needs exactly one permission —
	// roles/iam.workloadIdentityUser on this account — and nothing else moves.
	// +kubebuilder:validation:MaxLength=256
	// +optional
	ServiceAccountEmail string `json:"serviceAccountEmail,omitempty"`
}

// EgressAllowlistSpec supplies the parts of the agent Pod's egress allowlist
// that the operator cannot derive from the PlatformAgent itself.
type EgressAllowlistSpec struct {
	// ControlPlaneCIDRs are the address ranges of the Kubernetes API server,
	// permitted on port 443.
	//
	// Refused, with the same Degraded report extraRules gets, if a range
	// contains a metadata server address or is broader than /16 (/32 for
	// IPv6). A GKE control plane is a /28 or a single address, so a wider
	// range is an internet rule in a field named for the control plane — and
	// this policy is an exfiltration control as well as a metadata one.
	//
	// The operator cannot derive this and NetworkPolicy has no selector for it:
	// on GKE the control plane is outside the cluster, at a private /28 you
	// chose at creation time or at a public address, and the in-cluster
	// "kubernetes" Service is translated to that address before policy is
	// evaluated. Leaving this empty is allowed and is the stricter choice. It
	// costs the agent container its API-server connection, which matters at
	// spec.deployment.replicas above 1, where the container runs
	// leader_elect.py and holds a Lease, and to any sidecar or plugin you
	// added that talks to the API. Find the range with
	// `gcloud container clusters describe CLUSTER --format='value(privateClusterConfig.masterIpv4CidrBlock,endpoint)'`.
	// On a cluster with a public endpoint that command emits a bare address;
	// paste it as-is and it is widened to a single-host prefix.
	// +optional
	ControlPlaneCIDRs []string `json:"controlPlaneCIDRs,omitempty"`

	// ExtraRules are appended verbatim to the rendered policy, for
	// destinations a plugin or a custom sidecar needs.
	//
	// A rule that would re-permit the metadata server is not rendered — an
	// escape hatch that can reopen the escape is not one. It is also not
	// silently skipped: the agent goes Degraded with reason
	// EgressAllowlistRefused, naming the rule and why, while the policy
	// without that rule is still rendered and still maintained. A dropped rule
	// that left the agent Ready would mean an unreachable destination with
	// nothing in kubectl describe to explain it.
	// +optional
	ExtraRules []networkingv1.NetworkPolicyEgressRule `json:"extraRules,omitempty"`
}

// ScopedServiceAccount binds one GKE cluster to the Google service account
// permitted to read it.
//
// The three cluster fields are a tuple rather than a name because they compose
// into the GKE resource name — projects/P/locations/L/clusters/C — which is the
// key the credential broker looks the account up by, and the key Terraform
// files the account under. Keying on the cluster name alone would let a second
// project reusing a name be served by the first project's account.
//
// The patterns are the broker's own component regexes, which is the property
// that matters: they are narrower than GKE's naming rules in places, and being
// identical to what the broker will accept is what stops the API server
// admitting an entry the broker then refuses. They are enforced here as well as
// there because a separator or a quote in one of them would produce a key that
// silently matches nothing.
type ScopedServiceAccount struct {
	// ProjectID is the project the cluster lives in, which need not be the
	// project the agent runs in.
	// +kubebuilder:validation:Pattern=`^[a-z0-9][a-z0-9-]*$`
	// +kubebuilder:validation:MaxLength=63
	ProjectID string `json:"projectId"`

	// Location is the cluster's region or zone.
	// +kubebuilder:validation:Pattern=`^[a-z0-9][a-z0-9-]*$`
	// +kubebuilder:validation:MaxLength=63
	Location string `json:"location"`

	// ClusterName is the GKE cluster's name.
	// +kubebuilder:validation:Pattern=`^[a-z0-9][a-z0-9-]*$`
	// +kubebuilder:validation:MaxLength=63
	ClusterName string `json:"clusterName"`

	// ServiceAccountEmail is the account scoped to this cluster. Terraform's
	// `scoped_service_accounts` output is the source of these values.
	// +kubebuilder:validation:Pattern=`^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9-]{6,30}\.iam\.gserviceaccount\.com$`
	ServiceAccountEmail string `json:"serviceAccountEmail"`
}

// IntegrationSpec isolates common platform-specific external connections.
type IntegrationSpec struct {
	// GitHub configures the GitHub integration.
	// +optional
	GitHub *GitHubSpec `json:"github,omitempty"`
}

// GitHubSpec contains the configuration for the GitHub integration.
type GitHubSpec struct {
	// Org is the target GitHub organization or user account for the agent environment.
	// If omitted and GitRepo is provided, the organization is inferred from the repository owner.
	// +kubebuilder:validation:MaxLength=39
	// +kubebuilder:validation:Pattern=`^$|^[a-zA-Z0-9]([a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$`
	// +optional
	Org string `json:"org,omitempty"`

	// GitRepo is the optional target GitOps repository URL or owner/repo shorthand for the agent environment.
	// When omitted or empty, no repository is initially configured, and repositories can be registered
	// in the gitops-state ConfigMap by a cluster administrator.
	// +kubebuilder:validation:MaxLength=2048
	// +optional
	GitRepo string `json:"gitRepo,omitempty"`
}

// TelemetrySpec configures where the agent's OpenTelemetry signals are sent.
type TelemetrySpec struct {
	// OTLPEndpoint is the base URL of an OTLP/HTTP collector, for example
	// "http://otel-collector.otel-collector.svc.cluster.local:4318". Give the base URL
	// only — the per-signal path ("/v1/traces") is appended by the exporter.
	//
	// Setting it pins the endpoint and disables in-cluster collector discovery. Leave it
	// empty to let the operator discover a collector and fall back to the GKE Managed
	// OpenTelemetry collector. The empty alternative in the pattern is required because
	// the API server validates an explicitly-set "", which omitempty does not suppress.
	// +kubebuilder:validation:MaxLength=2048
	// +kubebuilder:validation:Pattern=`^$|^https?://[^\s]+$`
	// +optional
	OTLPEndpoint string `json:"otlpEndpoint,omitempty"`
}

// NetworkPolicySpec configures the operator-generated egress NetworkPolicy.
// Tier-2 typed equivalent of the kubeagents.x-k8s.io/{dns-cluster-ip,metadata-daemon-ip}
// annotations; the annotations remain as the escape hatch and win over this field.
type NetworkPolicySpec struct {
	// Enabled turns operator-managed NetworkPolicy generation off entirely, for
	// installs that manage network policy through their own tooling. Unset means on.
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// DNSClusterIPs pins the cluster DNS Service ClusterIPs. Setting it disables
	// discovery, like spec.telemetry.otlpEndpoint. Each entry is a bare IP with no
	// prefix; the operator writes it into rule 1 as a /32 or /128.
	//
	// The per-item pattern is here rather than left to the resolver because an entry
	// the resolver cannot parse is dropped and the pin silently reverts to discovery.
	// It bounds the IPv4 octets and rejects the leading-zero form net.ParseIP refuses
	// (010.96.0.10), so the usual typos are apply-time errors -- but it is a shape
	// check, not net.ParseIP: a malformed IPv6 literal the hextet alternation admits
	// still reaches the resolver, which logs it and falls back to discovery.
	// EgressPeer.CIDR and MetadataDaemonSpec.Endpoint below carry the same bound for
	// the same reason.
	// +kubebuilder:validation:MaxItems=8
	// +kubebuilder:validation:items:MaxLength=45
	// +kubebuilder:validation:items:Pattern=`^((((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9]))|(([0-9a-fA-F]{0,4}:){1,7}[0-9a-fA-F]{0,4}))$`
	// +optional
	DNSClusterIPs []string `json:"dnsClusterIPs,omitempty"`

	// MetadataDaemon describes the node-local cloud metadata daemon. Leave nil to let
	// the operator discover the container port from the kube-system/gke-metadata-server
	// DaemonSet (falling back to port 988 and 169.254.169.252). Overriding the endpoint
	// via annotation, spec, or operator flag opts out of discovery and uses port 988.
	// Present with Endpoint "" emits no post-NAT rule at all, for datapaths that evaluate
	// pre-NAT or clouds without one.
	// +optional
	MetadataDaemon *MetadataDaemonSpec `json:"metadataDaemon,omitempty"`

	// AdditionalEgress appends CIDR-and-port egress rules to the generated policy.
	// Entries are not passed through untouched: every peer CIDR is canonicalised,
	// and three things the schema below cannot express are dropped by the operator
	// instead -- an IPv4-mapped IPv6 peer, which clears the IPv6 prefix floor and
	// then fails the IPv4 one once collapsed to the block it means; an except that
	// is not a strict subset of its peer, which the API server would reject the
	// whole policy for; and a rule left with no usable peer, which would otherwise
	// permit egress to every destination. Each drop is logged and costs only the
	// entry it names. Everything else is rejected at admission -- except an entry
	// with no ports, which is admitted and opens every port to its peers. See
	// EgressRule.ports.
	// +kubebuilder:validation:MaxItems=32
	// +optional
	AdditionalEgress []EgressRule `json:"additionalEgress,omitempty"`
}

// MetadataDaemonSpec pins the post-NAT metadata-daemon egress target (rule 3).
type MetadataDaemonSpec struct {
	// Endpoint is the daemon IP. "" (explicitly set) suppresses rule 3 entirely;
	// the empty alternative in the pattern is required because the API server
	// validates an explicitly-set "", which omitempty does not suppress.
	// +kubebuilder:validation:Pattern=`^($|(((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9]))|(([0-9a-fA-F]{0,4}:){1,7}[0-9a-fA-F]{0,4}))$`
	// +kubebuilder:validation:MaxLength=45
	Endpoint string `json:"endpoint"`
}

// EgressRule is a deliberately narrow projection of networkingv1.NetworkPolicyEgressRule:
// CIDR + port list only. It keeps the CRD OpenAPI small and forbids the selector-based
// peers that would let a CR reference pods/namespaces the operator does not vet.
type EgressRule struct {
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:MaxItems=16
	To []EgressPeer `json:"to"`

	// Ports restricts the rule to these destination ports. Omitting it emits a rule
	// with peers and no ports, which in NetworkPolicy semantics permits EVERY port
	// to those peers -- the mirror of the case the operator refuses to emit, a rule
	// with ports and no surviving peer. That is standard NetworkPolicy behaviour and
	// a legitimate thing to ask for, so it is admitted rather than blocked and
	// nothing is logged; list the ports if you did not mean it.
	// +kubebuilder:validation:MaxItems=16
	// +optional
	Ports []EgressPort `json:"ports,omitempty"`
}

// EgressPeer defines a CIDR block and optional exclusions.
type EgressPeer struct {
	// CIDR is an IPv4/IPv6 block or host IP, e.g. 10.0.0.0/24 or 10.0.0.1.
	//
	// The prefix length is bounded by the pattern rather than left to the resolver:
	// 12-32 for IPv4 and 48-128 for IPv6, the same floors toEgressRules enforces.
	// Stating them at admission turns "the rule silently never took effect" into an
	// apply-time error.
	//
	// One case the pattern cannot express and the resolver handles instead: an
	// IPv4-mapped IPv6 block such as ::ffff:0:0/96 is a 128-bit prefix by every
	// textual measure, so it clears the IPv6 floor here, and is then collapsed to its
	// IPv4 equivalent and re-measured against the IPv4 floor by normalizeCIDRTarget --
	// which is what stops it emitting as 0.0.0.0/0. Excluding the mapped form by
	// regex would mean enumerating every zero-compression spelling of the first five
	// hextets; the resolver decides it in one comparison.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=49
	// +kubebuilder:validation:Pattern=`^((((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(/(1[2-9]|2[0-9]|3[0-2]))?)|(([0-9a-fA-F]{0,4}:){1,7}[0-9a-fA-F]{0,4}(/(4[89]|[5-9][0-9]|1[01][0-9]|12[0-8]))?))$`
	CIDR string `json:"cidr"`

	// Except carves ranges out of CIDR. Each entry must be a strict subset of CIDR --
	// contained by it and narrower than it -- because ValidateIPBlock rejects the
	// whole NetworkPolicy otherwise, which would freeze every other egress rule at
	// its previous revision. The resolver applies the same test and drops an except
	// that fails it rather than forwarding it.
	//
	// An entry may be a bare host address as well as a block, the same as CIDR above
	// -- a bare address means a /32 or /128. The prefix is optional here for that
	// symmetry alone: writing 10.0.1.5 next to a cidr that accepts 10.0.1.5 should
	// not be an apply-time rejection quoting a 200-character regex. Unlike CIDR
	// there is no prefix floor, because an except is bounded by having to be a
	// strict subset of its peer.
	// +kubebuilder:validation:MaxItems=16
	// +kubebuilder:validation:items:MaxLength=49
	// +kubebuilder:validation:items:Pattern=`^((((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(/([0-9]|[12][0-9]|3[0-2]))?)|(([0-9a-fA-F]{0,4}:){1,7}[0-9a-fA-F]{0,4}(/([0-9]|[1-9][0-9]|1[01][0-9]|12[0-8]))?))$`
	// +optional
	Except []string `json:"except,omitempty"`
}

// EgressPort defines a port and transport protocol.
type EgressPort struct {
	// +kubebuilder:validation:Enum=TCP;UDP;SCTP
	Protocol string `json:"protocol"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	Port int32 `json:"port"`
}

// AgentSpec defines the common infrastructure configuration shared across all agent types.
type AgentSpec struct {
	// Deployment abstracts the Kubernetes Pod/Deployment configuration.
	// +optional
	Deployment *DeploymentSpec `json:"deployment,omitempty"`

	// Security configures RBAC, Pod Security, and Workload Identity.
	// +optional
	Security *SecuritySpec `json:"security,omitempty"`

	// Telemetry configures OpenTelemetry export for this agent.
	// +optional
	Telemetry *TelemetrySpec `json:"telemetry,omitempty"`

	// NetworkPolicy configures the operator-generated egress NetworkPolicy.
	// +optional
	NetworkPolicy *NetworkPolicySpec `json:"networkPolicy,omitempty"`
}

type DeploymentStatus struct {
	// Name is the exact name of the underlying Kubernetes Deployment.
	// +optional
	Name string `json:"name,omitempty"`

	// ReadyReplicas indicates how many replicas are fully ready.
	// +optional
	ReadyReplicas int32 `json:"readyReplicas,omitempty"`
}

type ServiceStatus struct {
	// Endpoint is the primary URL or IP (including protocol and port) to reach the agent.
	// +optional
	Endpoint string `json:"endpoint,omitempty"`
}

type StorageStatus struct {
	// Bound indicates if the primary PVC has been successfully provisioned.
	// +optional
	Bound bool `json:"bound,omitempty"`
}

// TelemetryStatus reports the telemetry wiring the operator resolved for this agent.
//
// The endpoint alone cannot distinguish "we discovered the managed collector" from "we
// found nothing and fell back to it", so the source is reported alongside it — that
// distinction is the whole diagnostic question when spans do not arrive.
type TelemetryStatus struct {
	// OTLPEndpoint is the collector endpoint written into the agent pod. Empty when the
	// source is None, which is the one case where the pod is given no endpoint at all.
	// +optional
	OTLPEndpoint string `json:"otlpEndpoint,omitempty"`

	// OTLPEndpointSource is how the endpoint was chosen: DeploymentEnv, Spec,
	// OperatorEnv, Discovered, Default, or None. None means discovery completed and
	// this cluster has no collector, so the agent runs with OTEL_SDK_DISABLED=true and
	// exports nothing; Default still means the GKE managed collector, and is what an
	// install gets when discovery is switched off or could not complete.
	// +optional
	OTLPEndpointSource string `json:"otlpEndpointSource,omitempty"`
}

// NetworkPolicyStatus reports the network wiring the operator resolved, and its source —
// the same diagnostic split as TelemetryStatus: the value alone cannot say whether a DNS
// IP was discovered or pinned.
type NetworkPolicyStatus struct {
	// Note, deliberately not a doc comment — the blank line below keeps it out of the
	// CRD description that `kubectl explain` prints. No omitempty, deliberately:
	// encoding/json omits a false bool under omitempty, so a disabled agent would
	// serialise as `networkPolicy: {}` and the one state this field exists to report
	// would be the one it could not express. The key is therefore always present,
	// including before anything has resolved it — which is what the doc comment below
	// has to scope, and why this field is not a *bool: a pointer would put the key
	// back to absent for exactly the CR an operator is most likely to be inspecting.

	// Generated reports whether the operator is managing a NetworkPolicy for this
	// agent: true once a reconcile has generated one, false when
	// spec.networkPolicy.enabled is false. It is written only by the Ready status
	// update, so read it alongside the Ready condition — a CR that went Degraded
	// before its first successful reconcile reports false because nothing has
	// resolved the field yet, not because generation is off.
	// +optional
	Generated bool `json:"generated"`

	// DNSClusterIPs are the ClusterIPs written into rule 1.
	// +optional
	DNSClusterIPs []string `json:"dnsClusterIPs,omitempty"`

	// DNSClusterIPsSource reports which rung answered the DNS ClusterIP (Annotation, Spec, OperatorEnv, Discovered, or Default).
	// +optional
	DNSClusterIPsSource string `json:"dnsClusterIPsSource,omitempty"`

	// MetadataDaemonIP is the post-NAT daemon IP in rule 3, empty when suppressed.
	// +optional
	MetadataDaemonIP string `json:"metadataDaemonIP,omitempty"`

	// MetadataDaemonPort is the post-NAT daemon port in rule 3, resolved from the live
	// DaemonSet when metadataDaemonIPSource is Discovered, else the documented default (988).
	// +optional
	MetadataDaemonPort int32 `json:"metadataDaemonPort,omitempty"`

	// MetadataDaemonIPSource reports which rung answered the metadata daemon IP (Annotation, Spec, OperatorEnv, Discovered, Default, or Suppressed).
	// +optional
	MetadataDaemonIPSource string `json:"metadataDaemonIPSource,omitempty"`
}

// AgentStatus defines the observed state of an agent.
type AgentStatus struct {
	// Phase is the overall state (Pending, Provisioning, Ready, Failed).
	// +optional
	Phase string `json:"phase,omitempty"`

	// Address is the fully qualified domain name (FQDN) of the agent service.
	// +optional
	Address string `json:"address,omitempty"`

	// LastReconcileTime is the timestamp when the operator last updated this status.
	// +optional
	LastReconcileTime *metav1.Time `json:"lastReconcileTime,omitempty"`

	// Conditions represent the latest available observations of the instance's state.
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`

	// DeploymentStatus tracks the state of the underlying compute.
	// +optional
	DeploymentStatus DeploymentStatus `json:"deploymentStatus,omitempty"`

	// ServiceStatus holds internal/external endpoints.
	// +optional
	ServiceStatus ServiceStatus `json:"serviceStatus,omitempty"`

	// StorageStatus tracks PVC binding state.
	// +optional
	StorageStatus StorageStatus `json:"storageStatus,omitempty"`

	// Note, deliberately not a doc comment — the blank line below keeps it out of the
	// CRD description that `kubectl explain` prints. As on the three status structs
	// above, omitempty does nothing here: encoding/json has no notion of an empty
	// struct, so this key is always serialised, as `{}` before the first reconcile. It
	// is kept for consistency with its neighbours — read the field, not the key's
	// absence, to tell whether telemetry has been resolved.

	// Telemetry reports the resolved OpenTelemetry export configuration.
	// +optional
	Telemetry TelemetryStatus `json:"telemetry,omitempty"`

	// Note, deliberately not a doc comment — the blank line below keeps it out of the
	// CRD description that `kubectl explain` prints. As on the three status structs
	// above, omitempty does nothing here: encoding/json has no notion of an empty
	// struct, so this key is always serialised, as `{}` before the first reconcile. It
	// is kept for consistency with its neighbours — read the field, not the key's
	// absence, to tell whether network policy has been resolved.

	// NetworkPolicy reports the resolved egress NetworkPolicy configuration.
	// +optional
	NetworkPolicy NetworkPolicyStatus `json:"networkPolicy,omitempty"`
}

const (
	// MaxGitHubOrgLength defines the maximum character length for GitHub org/user names.
	MaxGitHubOrgLength = 39
	// MaxGitRepoURLLength defines the maximum character length for Git repository URLs.
	MaxGitRepoURLLength = 2048
)

// githubOrgRegex validates GitHub organization or username format
// (alphanumeric and hyphens, not starting or ending with hyphen, max 39 chars).
var githubOrgRegex = regexp.MustCompile(`^[a-zA-Z0-9]([a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$`)

// CleanRepoSlug cleans up git URLs, HTTPS/SSH endpoints, or bare shorthands into "owner/repo" format.
func CleanRepoSlug(rawURL string) (string, error) {
	return CleanRepoSlugWithOrg(rawURL, "")
}

// CleanRepoSlugWithOrg cleans up git URLs, HTTPS/SSH endpoints, or bare shorthands into "owner/repo" format,
// using the provided org if a bare repository name (without a slash) is given.
func CleanRepoSlugWithOrg(rawURL, org string) (string, error) {
	cleaned := strings.TrimSpace(rawURL)
	cleaned = strings.TrimSuffix(cleaned, ".git")

	// Validate URL scheme if a scheme is present
	if idx := strings.Index(cleaned, "://"); idx != -1 {
		scheme := strings.ToLower(cleaned[:idx])
		if scheme != "http" && scheme != "https" && scheme != "git" && scheme != "ssh" {
			return "", fmt.Errorf("unsupported URL scheme %q; must be http, https, git, or ssh", scheme)
		}
	}

	// Strip known URL schemes
	for _, scheme := range []string{"ssh://", "git://", "https://", "http://"} {
		cleaned = strings.TrimPrefix(cleaned, scheme)
	}

	// Handle user@host prefix (e.g. git@github.com:owner/repo or git@github.com/owner/repo)
	if idx := strings.Index(cleaned, "@"); idx != -1 {
		cleaned = cleaned[idx+1:]
	}

	// Handle SCP-style host:path syntax (e.g. github.com:owner/repo)
	if strings.Contains(cleaned, ":") {
		parts := strings.SplitN(cleaned, ":", 2)
		if len(parts) == 2 {
			cleaned = parts[1]
		}
	}

	// Strip common domain prefixes
	cleaned = strings.TrimPrefix(cleaned, "github.com/")
	cleaned = strings.TrimPrefix(cleaned, "www.github.com/")
	cleaned = strings.Trim(cleaned, "/")

	if cleaned == "" {
		return "", fmt.Errorf("empty repository")
	}

	// If no slash is present and an org was supplied, prefix with org
	if !strings.Contains(cleaned, "/") && strings.TrimSpace(org) != "" {
		cleaned = strings.TrimSpace(org) + "/" + cleaned
	}

	// Basic verification of owner/repo structure
	if strings.Count(cleaned, "/") != 1 {
		return "", fmt.Errorf("invalid repository format")
	}
	return cleaned, nil
}

// CleanRepoURLWithOrg cleans up git URLs, SSH endpoints, or shorthands into a full HTTPS URL format (e.g. "https://github.com/owner/repo").
func CleanRepoURLWithOrg(rawURL, org string) (string, error) {
	trimmed := strings.TrimSpace(rawURL)
	if trimmed == "" || trimmed == "None" {
		return "", fmt.Errorf("empty repository")
	}
	if strings.HasPrefix(trimmed, "https://") || strings.HasPrefix(trimmed, "http://") {
		u := strings.TrimSuffix(trimmed, ".git")
		u = strings.TrimSuffix(u, "/")
		return u, nil
	}
	cleanedSlug, err := CleanRepoSlugWithOrg(rawURL, org)
	if err != nil {
		return "", err
	}
	return "https://github.com/" + cleanedSlug, nil
}

// ValidateGitRepoURL verifies that a Git repository URL or shorthand is structurally valid
// and contains no whitespace or non-graphic character injections.
func ValidateGitRepoURL(gitRepo string) error {
	return ValidateGitRepoURLWithOrg(gitRepo, "")
}

// ValidateGitRepoURLWithOrg verifies that a Git repository URL or shorthand (with optional org context)
// is structurally valid and contains no whitespace or non-graphic character injections.
func ValidateGitRepoURLWithOrg(gitRepo, org string) error {
	trimmed := strings.TrimSpace(gitRepo)
	if trimmed == "" || trimmed == "None" {
		return nil
	}

	if utf8.RuneCountInString(trimmed) > MaxGitRepoURLLength {
		return fmt.Errorf("git repo URL exceeds maximum length of %d characters", MaxGitRepoURLLength)
	}

	for _, r := range trimmed {
		if unicode.IsSpace(r) || !unicode.IsGraphic(r) {
			return fmt.Errorf("git repo URL contains whitespace or non-graphic characters")
		}
	}

	if _, err := CleanRepoSlugWithOrg(trimmed, org); err != nil {
		return fmt.Errorf("invalid git repository format %q: expected owner/repo or valid git URL", trimmed)
	}

	return nil
}

// ValidateGitHubOrg verifies that a GitHub Org string is a valid organization or user name
// and contains no control characters, slashes, or newline injections (PI-004).
func ValidateGitHubOrg(org string) error {
	trimmed := strings.TrimSpace(org)
	if trimmed == "" {
		return nil
	}

	if utf8.RuneCountInString(trimmed) > MaxGitHubOrgLength {
		return fmt.Errorf("github org exceeds maximum length of %d characters", MaxGitHubOrgLength)
	}

	// Disallow whitespace and any non-graphic characters
	for _, r := range trimmed {
		if unicode.IsSpace(r) || !unicode.IsGraphic(r) {
			return fmt.Errorf("github org contains whitespace or non-graphic characters")
		}
	}

	if !githubOrgRegex.MatchString(trimmed) {
		return fmt.Errorf("invalid github org %q: must contain only alphanumeric characters and hyphens, and cannot begin or end with a hyphen", trimmed)
	}

	return nil
}

// ManagedRepoEntry represents a single managed repository in the gitops-state ConfigMap.
type ManagedRepoEntry struct {
	Type string `json:"type"`
	URL  string `json:"url"`
}
