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
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// SensitiveEnvVars defines environment variables that are sensitive and cannot be
// overridden by user Deployment specs or injected into the credential proxy.
var SensitiveEnvVars = map[string]struct{}{
	"API_SERVER_KEY": {},
	"HERMES_HOME":    {},
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

	// ApiServerSecretRef securely references a Secret containing the API_SERVER_KEY.
	// +optional
	ApiServerSecretRef *corev1.SecretKeySelector `json:"apiServerSecretRef,omitempty"`

	// SessionKVApiKeySecretRef references the Secret key holding the bearer
	// token for the pod-local Session KV server on port 8699. Distinct from
	// ApiServerSecretRef: that path uses the non-secret loopback sentinel
	// `cluster-internal-trusted`, which would authenticate nothing here.
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
	// omitted entirely, the operator's build-injected default version applies
	// instead, so no "latest" default is persisted on the CR.
	// +optional
	Tag *string `json:"tag,omitempty"`

	// ImagePullPolicy specifies if the image should be pulled.
	// +kubebuilder:default=IfNotPresent
	// +kubebuilder:validation:Enum=Always;Never;IfNotPresent
	// +optional
	ImagePullPolicy *corev1.PullPolicy `json:"imagePullPolicy,omitempty"`

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

	// SplitCredentialBrokerPod moves the credential broker out of the agent Pod
	// into a Deployment and Service of its own, so that a compromised agent no
	// longer shares a network namespace with the process holding the cloud
	// credentials.
	//
	// REQUIRES ReadWriteMany storage for the agent data volume, and defaults to
	// false for that reason. The broker runs proxied commands with a working
	// directory the agent created on the shared data PVC, so both Pods must
	// mount that PVC read-write at the same path and see the same files there.
	// The default GKE persistent disk is ReadWriteOnce, which cannot do that
	// across two Pods on different nodes; the cluster needs Filestore or GCS
	// Fuse (storage class "standard-rwx" or equivalent) before this is enabled.
	//
	// Without it the failure is a scheduling one, not a policy one. The broker
	// Pod cannot attach the volume, stays Pending with a Multi-Attach error,
	// and never becomes a Service endpoint — so every proxied command reports
	// "credential proxy unavailable: [Errno 111] Connection refused", the same
	// symptom an unhealthy sidecar produces. If the scheduler happens to place
	// both Pods on one node it will instead appear to work, which makes the
	// misconfiguration intermittent and worth ruling out first.
	// directory the agent created on the shared data PVC and rejects any
	// working directory outside it, so both Pods must mount that PVC read-write
	// at the same path. The default GKE persistent disk is ReadWriteOnce, which
	// cannot do that across two Pods; the cluster needs Filestore or GCS Fuse
	// (storage class "standard-rwx" or equivalent) before this is enabled.
	// Turning it on without ReadWriteMany storage does not degrade the agent —
	// every proxied command fails.
	//
	// Two further caveats. The agent Pod and the broker Pod share one
	// ServiceAccount, because the Workload Identity IAM binding names it, so
	// the identity the broker verifies is per-ServiceAccount rather than
	// per-Pod. And the bearer token the agent presents crosses the cluster
	// network in cleartext.
	// +optional
	SplitCredentialBrokerPod *bool `json:"splitCredentialBrokerPod,omitempty"`

	// EgressPolicy selects the NetworkPolicy the operator renders for the agent
	// Pod. "None" (the default) renders nothing.
	//
	// "Allowlist" renders a default-deny egress policy that permits only the
	// destinations the agent legitimately needs, which denies the link-local
	// metadata server — 169.254.169.254, where anything that can make an HTTP
	// request can mint the node or Workload Identity service account's tokens —
	// by simply not listing it.
	//
	// REQUIRES splitCredentialBrokerPod: true. This is not a stylistic pairing,
	// it is the whole reason the split exists. Containers in one Pod share a
	// network namespace, and the credential broker reaches the metadata server
	// on purpose: minting the cloud token is its job. A Pod-level NetworkPolicy
	// cannot deny the metadata server to the agent container while allowing it
	// to the broker container beside it. With the broker still a sidecar, this
	// policy would take the broker's credentials away and every proxied
	// command would fail. The operator therefore refuses to render it in that
	// configuration and reports Degraded rather than breaking the agent — so
	// the default install, which has the split off, is NOT protected from the
	// metadata server. See docs/site reference/credential-isolation.md.
	//
	// Three further conditions the operator cannot check for you.
	//
	//   - The policy does nothing at all on a cluster whose CNI does not
	//     enforce NetworkPolicy (GKE Standard without network policy enabled);
	//     Autopilot and GKE Dataplane V2 always enforce.
	//   - NetworkPolicies are additive, so any other policy in the namespace
	//     that selects this Pod and permits wider egress re-opens what this
	//     one closes.
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
	// WHAT IT BREAKS, and this is not a short list. The allowlist covers DNS,
	// the credential broker, LiteLLM, the managed OpenTelemetry collector, and
	// whatever egressAllowlist adds. Everything else the agent container
	// reaches on its own goes away:
	//
	//   - the "web" toolset (DuckDuckGo) and the "browser" toolset (headless
	//     Chromium), both of which the platform and cluster-* profiles enable;
	//   - the MCP servers that call container.googleapis.com and
	//     developerknowledge.googleapis.com;
	//   - github.com reached directly from the sandbox;
	//   - the GKE metadata lookups in cluster_agent_reconcile.py, which fail
	//     soft — set RECONCILE_PROJECT and RECONCILE_EXCLUDE to restore what
	//     they were for.
	//
	// Those are not accidental casualties. A headless browser with
	// unrestricted egress is the exfiltration path, so the capabilities this
	// removes are the same ones that make the control worth having. Restore
	// individual destinations with egressAllowlist.extraRules — noting that
	// NetworkPolicy matches addresses, never DNS names, so restoring a hosted
	// service means naming its address ranges.
	//
	// Credentialed gcloud, kubectl, gh and git are unaffected: they are shims
	// that call the broker, and the broker is on the allowlist.
	//
	// TURNING THIS OFF DOES NOT DELETE THE POLICY, and reverting both flags
	// together will break the agent. An egress policy is a guardrail, and the
	// operator will not remove a guardrail it may not have created, so setting
	// this back to "None" leaves <name>-sandbox-metadata-deny in place. That
	// is fail-closed and harmless on its own — but it is not harmless
	// alongside splitCredentialBrokerPod: false.
	//
	// The trap is that reverting the split alone is refused (see above), so
	// the natural way out is to turn both off in one edit. Do that and the
	// broker becomes a sidecar again, inside the very Pod the leftover policy
	// selects, and the policy does not list the metadata server.
	//
	// What you see is a crashlooping agent, not a quietly broken one. The Pod
	// template changed, and the deployment strategy is Recreate at the default
	// single replica, so the old Pod goes away first. The new sidecar runs
	// CREDENTIAL_PROXY_BOOTSTRAP_COMMAND before it serves anything — a gcloud
	// container clusters get-credentials, rendered whenever spec.harness names
	// a project, location and cluster, which the Helm chart requires — and
	// that command needs the metadata server and container.googleapis.com,
	// both of which the leftover policy denies. The bootstrap raises, the
	// runtime exits, the sidecar entrypoint's `wait -n` takes the container
	// with it, and the Pod enters CrashLoopBackOff. ReadyReplicas stays 0, so
	// the agent reports phase Provisioning with Ready=False and "Waiting for
	// deployment replicas to be ready".
	//
	// That status is accurate but says nothing about a NetworkPolicy. The
	// sidecar log is where the cause is: gcloud failing to reach the metadata
	// server. Check for a leftover <name>-sandbox-metadata-deny before
	// diagnosing anything else.
	//
	// (An agent whose spec.harness omits those cluster fields has no bootstrap
	// command, so its sidecar starts cleanly and the Pod does report Ready
	// while every proxied command fails at the network. That configuration is
	// reachable by hand, not through the chart.)
	//
	// Revert in three steps instead, which never leaves a broker inside a
	// policy that denies it:
	//
	//   1. set egressPolicy: None, leaving splitCredentialBrokerPod: true;
	//   2. kubectl -n NS delete networkpolicy NAME-sandbox-metadata-deny
	//      (safe now — with the field off the operator will not re-apply it,
	//      whereas deleting it while the field is still "Allowlist" only
	//      earns it back on the next reconcile);
	//   3. set splitCredentialBrokerPod: false.
	// +kubebuilder:validation:Enum=None;Allowlist
	// +optional
	EgressPolicy string `json:"egressPolicy,omitempty"`

	// EgressAllowlist tunes the destinations egressPolicy: Allowlist permits.
	// Ignored for any other egressPolicy value.
	// +optional
	EgressAllowlist *EgressAllowlistSpec `json:"egressAllowlist,omitempty"`
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
	// evaluated. Leaving this empty is allowed and is the stricter choice, but
	// it costs the event-watcher sidecar its API-server connection, so cluster
	// events stop reaching the agent. Find the range with
	// `gcloud container clusters describe CLUSTER --format='value(privateClusterConfig.masterIpv4CidrBlock,endpoint)'`.
	// +optional
	ControlPlaneCIDRs []string `json:"controlPlaneCIDRs,omitempty"`

	// ExtraRules are appended verbatim to the rendered policy, for
	// destinations a plugin or a custom sidecar needs.
	//
	// A rule that would re-permit the metadata server is not rendered — an
	// escape hatch that can reopen the escape is not one. It is also not
	// silently skipped: the agent goes Degraded with reason
	// EgressAllowlistRefused, naming the rule and why, and is not reconciled
	// until the spec is fixed. A dropped rule that left the agent Ready would
	// mean an unreachable destination with nothing in kubectl describe to
	// explain it.
	// +optional
	ExtraRules []networkingv1.NetworkPolicyEgressRule `json:"extraRules,omitempty"`
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
	// OTLPEndpoint is the collector endpoint written into the agent pod.
	// +optional
	OTLPEndpoint string `json:"otlpEndpoint,omitempty"`

	// OTLPEndpointSource is how the endpoint was chosen: DeploymentEnv, Spec,
	// OperatorEnv, Discovered, or Default.
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
