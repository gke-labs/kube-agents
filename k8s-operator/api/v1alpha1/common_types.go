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
	"net/url"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"

	corev1 "k8s.io/api/core/v1"
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
}

// IntegrationSpec isolates common platform-specific external connections.
type IntegrationSpec struct {
	// GitHub configures the GitHub integration.
	// +optional
	GitHub *GitHubSpec `json:"github,omitempty"`
}

// GitHubSpec contains the configuration for the GitHub integration.
type GitHubSpec struct {
	// GitRepo is the target GitOps repository URL for the agent environment.
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
}

const (
	// MaxGitRepoURLLength defines the maximum character length for GitRepo URLs,
	// matching the +kubebuilder:validation:MaxLength marker on GitHubSpec.GitRepo.
	MaxGitRepoURLLength = 2048
)

// scpRegex validates SCP-style SSH Git URLs (e.g., git@github.com:owner/repo.git).
// Compiled at package level to avoid re-compilation overhead on every validation invocation.
var scpRegex = regexp.MustCompile(`^git@[a-zA-Z0-9.-]+:[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+(\.git)?$`)

// ownerRepoRegex validates bare "owner/repo" shorthand (e.g. "gke-labs/kube-agents").
var ownerRepoRegex = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)

// ValidateGitRepoURL verifies that a GitRepo string is a valid Git repository URL
// and contains no control characters or newline injections (PI-004).
func ValidateGitRepoURL(rawURL string) error {
	trimmed := strings.TrimSpace(rawURL)
	if trimmed == "" {
		return nil
	}

	if utf8.RuneCountInString(trimmed) > MaxGitRepoURLLength {
		return fmt.Errorf("gitRepo URL exceeds maximum length of %d characters", MaxGitRepoURLLength)
	}

	// Disallow whitespace (ASCII and Unicode) and any non-graphic characters (control chars, zero-width chars, etc.)
	for _, r := range trimmed {
		if unicode.IsSpace(r) || !unicode.IsGraphic(r) {
			return fmt.Errorf("gitRepo URL contains whitespace or non-graphic characters")
		}
	}

	// Check SCP-style SSH format: git@host:owner/repo.git
	if scpRegex.MatchString(trimmed) {
		return nil
	}

	// Check bare owner/repo shorthand (e.g., gke-labs/kube-agents)
	if ownerRepoRegex.MatchString(trimmed) {
		return nil
	}

	// Parse standard URIs
	u, err := url.ParseRequestURI(trimmed)
	if err != nil {
		return fmt.Errorf("invalid URL structure: %w", err)
	}

	scheme := strings.ToLower(u.Scheme)
	if scheme != "http" && scheme != "https" && scheme != "git" && scheme != "ssh" {
		return fmt.Errorf("unsupported URL scheme %q; must be http, https, git, or ssh", u.Scheme)
	}

	if u.Host == "" {
		return fmt.Errorf("gitRepo URL missing host")
	}

	return nil
}
