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
	"encoding/json"
	goerrors "errors"
	"fmt"
	"net"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/go-logr/logr"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	nodev1 "k8s.io/api/node/v1"
	policyv1 "k8s.io/api/policy/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/discovery"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// PlatformAgentControllerName is the name this controller records Events
// under (main.go hands it to the manager's recorder): the same name it
// manages fields under, held to that by definition rather than by a second
// copy of the string.
const PlatformAgentControllerName = fieldOwner

const (
	platformAgentFinalizer = "kubeagents.x-k8s.io/finalizer"
	// usageStatusReprobeInterval is how long a CR stays marked as served by a
	// CRD without status.usage before the Ready writer probes again (see
	// prunedUsageStatus). The RBAC self-check's cadence, for the same reason:
	// the condition changes when someone applies something, not on a schedule,
	// and one status write per interval is a cost nobody notices.
	usageStatusReprobeInterval = 5 * time.Minute
	minIPv4CIDRPrefix          = 12
	minIPv6CIDRPrefix          = 48
	maxCIDRsPerAnnotation      = 50

	// The two keys of the <agent>-gitops-state ConfigMap the minter policy is
	// synced from: managed_repos renders write policies, context_repos read-only
	// ones. gitops_workspace.py names the same two keys on the agent side.
	gitopsStateManagedReposKey = "managed_repos"
	gitopsStateContextReposKey = "context_repos"
	// minterConfigMapName is the minty rule ConfigMap the chart and the kustomize
	// template render; minterBaseTemplateKey is the policy every rendered key is
	// derived from.
	minterConfigMapName   = "github-token-minter-config"
	minterBaseTemplateKey = "default.yaml"
	// minterPolicyKeySuffix turns a bare repository name into its policy key. A
	// repository whose key would be minterBaseTemplateKey is skipped: rendering
	// it would overwrite the template every other policy is derived from.
	minterPolicyKeySuffix = ".yaml"
	// minterReadScope is the scope a context repository's policy carries and
	// nothing else: contents: read, as the chart's default.yaml declares it. A
	// default.yaml without it predates the read grant and renders no context
	// policies. minterScopeField and minterRepositoriesField are the minty v2
	// rule fields the read-only rendering rewrites.
	minterReadScope         = "platform-agent-read-scope"
	minterScopeField        = "scope"
	minterRepositoriesField = "repositories"

	// metadataLinkLocalIP is the address a workload dials for GCP metadata and Workload
	// Identity tokens. It is only ever the pre-DNAT destination.
	metadataLinkLocalIP = "169.254.169.254"
	// metadataDaemonIP is where GKE's node-local metadata daemon actually listens, on
	// TCP 988. On the iptables datapath (Dataplane V1) the node DNATs
	// 169.254.169.254:80 to 169.254.169.252:988 in nat PREROUTING — before NetworkPolicy
	// is evaluated — so a policy that permits only the link-local address drops every
	// token fetch. Dataplane V2 (eBPF) evaluates policy pre-NAT at the socket layer,
	// so the 169.254.169.254/32 on port 80 rule satisfies it directly.
	//
	// Ref:
	// - https://cloud.google.com/kubernetes-engine/docs/how-to/network-policy
	// - https://docs.cilium.io/en/stable/security/policy/layer3/
	// - https://github.com/cilium/cilium/issues/12277 (CIDR rules don't match node IPs without --policy-cidr-match-mode=nodes)
	metadataDaemonIP = "169.254.169.252"

	// How long applyShellSandboxStatefulSet waits for an orphan-propagation
	// delete to finish before giving the reconcile back. Orphan collection is
	// a finalizer removal on one object, so it lands in milliseconds; the
	// budget is for an overloaded garbage collector, not for the normal case,
	// and expiry requeues rather than fails the recreation.
	shellSandboxDeleteTimeout = 5 * time.Second
	// The gap between reads while that wait runs.
	shellSandboxDeletePollInterval = 100 * time.Millisecond

	// How long applyCredentialProxyDeployment waits for its foreground delete.
	// Longer than the sandbox's budget because this one waits on a pod to
	// terminate and not only on a finalizer: foreground propagation holds the
	// Deployment until the ReplicaSet and its pod are gone, and the broker has a
	// termination grace period to serve out. Expiry requeues, so overshooting
	// costs a reconcile rather than the recreation.
	credentialProxyDeleteTimeout = 60 * time.Second

	AnnotationAPIServerCIDR           = "kubeagents.x-k8s.io/apiserver-cidr"
	AnnotationCustomEgressCIDRs       = "kubeagents.x-k8s.io/custom-egress-cidrs"
	AnnotationEnableFQDNNetworkPolicy = "kubeagents.x-k8s.io/enable-fqdn-network-policy"
	AnnotationManagedMinterKeys       = "kubeagents.x-k8s.io/managed-minter-keys"

	// GKE Autopilot API groups and resources used to detect Autopilot clusters where Warden restricts Image volumes.
	gkeAutopilotAPIGroup                     = "auto.gke.io"
	gkeAutopilotAllowlistedWorkloadsResource = "allowlistedworkloads"
	gkeAutopilotDefaultGroupVersion          = "auto.gke.io/v1"

	pluginFailureReasonImagePull = "ImagePullFailed"
	pluginFailureReasonStaging   = "StagingFailed"
	exitCodeCommandNotFound      = int32(127)
	pluginStagingContainerPrefix = "stage-"

	reasonContainerCreating = "ContainerCreating"
	reasonPodInitializing   = "PodInitializing"
	reasonContainerError    = "Error"
	reasonCrashLoopBackOff  = "CrashLoopBackOff"

	// a2aVerifierContainerName is the verifier Deployment's only container
	// (buildA2AVerifierDeployment). The pod scan names it to tell the one
	// crash loop that is the a2a stack still coming up from a real fault;
	// see getDeploymentStatusDetails.
	a2aVerifierContainerName = "verifier"

	// The condition reporting that cluster event ingestion has been switched off
	// on the spec. It is written only in that state — see updateStatusReady.
	eventWatcherConditionType  = "EventWatcher"
	eventWatcherDisabledReason = "DisabledBySpec"
	// Long, because the reader of `kubectl describe` is the person who has to
	// decide whether this is still wanted. It has to say what stopped, that
	// nothing will turn it back on, and how to turn it back on.
	eventWatcherDisabledMessage = "Cluster event ingestion is disabled by spec.harness.eventWatcher.enabled=false. " +
		"The k8s-event-watcher is not started, so no cluster warning reaches the agent and no autonomous triage " +
		"session is created from one; the pod stays Ready regardless. Nothing restores this automatically — set " +
		"spec.harness.eventWatcher.enabled=true (or remove the field) to start watching again."

	// The condition reporting that the render left a hostPath volume out of
	// the agent Pod. Written only while the spec carries one, on the
	// EventWatcher pattern above, and not a Degraded state: the Pod runs, the
	// author's other volumes are in it, and the CR says what was left out and
	// why. See hostPathVolumes for the drop itself.
	//
	// Both status writers carry it, updateStatusReady and
	// updateStatusDegraded, because the strip happens at render and the render
	// is above both of them: three refusals park the CR on Degraded after the
	// Pod has already been written (ModeNotRecognized, A2AProvisionFailed,
	// ShellSandboxKeysMissing), and ShellSandboxKeysMissing is where a bare
	// `helm install` sits indefinitely -- the chart renders the sandbox's
	// authorized-keys Secret only when a public key is supplied. That is the
	// same chart-default install the webhook is off on, so the one park most
	// likely to hold a dropped hostPath was the one saying nothing about it.
	//
	// The Degraded writer carries it on those three only. The other four
	// refusals return before the render (ForbiddenVolumeMount,
	// ShellSandboxCannotBeDisabled, RuntimeClassNotFound,
	// EgressAllowlistRefused), so the Pod left running on such a pass is
	// whatever the previous pass rendered -- and on an operator rolled out
	// over a CR whose Deployment a pre-fix render gave real hostPath mounts,
	// writing the condition there would report the mounts gone while they are
	// still in the Pod, on every requeue tick for as long as the refusal
	// stands. A condition asserting a security property must not be able to
	// say True where the property does not hold. See workloadRenderState.
	hostPathDroppedConditionType = "VolumesDropped"
	hostPathDroppedReason        = "HostPathVolumeDropped"
	// hostPathDroppedEntryFormat renders one dropped entry as the author would
	// find it in the spec: field, index, name, and the host path it asked for.
	hostPathDroppedEntryFormat = "%s[%d] %q (hostPath %s)"
	// hostPathDroppedMessageFormat takes the joined entries and then the
	// rollout clause below, which is "" on most passes. It says what is left
	// out, that the mounts go with it, what keeping them would have cost, why
	// admission did not stop it, and how to clear the condition, because
	// `kubectl describe` is where the author of a CR the webhook never saw
	// finds out.
	//
	// It speaks about the Pod template the operator renders, not about the Pod
	// that is running, because the template is the whole of what this
	// controller observes. reconcileWorkload server-side-applies it and
	// returns once the API server has accepted it; nothing waits on the
	// rollout. With spec.deployment.availability.replicas > 1 the strategy is
	// RollingUpdate at maxUnavailable 1, so Pods from the revision before it --
	// on an operator rolled out over a CR a pre-fix render gave real hostPath
	// mounts, Pods that really do mount them -- keep running until the roll
	// finishes, and indefinitely if it stalls. "Left out of the agent Pod"
	// asserted a property of those Pods too, and Ready does not offset it:
	// updateStatusReady decides the phase from ReadyReplicas, which counts
	// ready Pods across every ReplicaSet the Deployment owns.
	hostPathDroppedMessageFormat = "hostPath volumes are forbidden and are left out of the Pod template the operator " +
		"renders, along with every volumeMount naming them: %s. Keeping them would give the agent container, which is " +
		"where model output runs, access to the node's filesystem at those paths.%s The admission webhook refuses " +
		"these when it runs, and this CR was admitted without it (the Helm chart ships operator.webhooks.enabled=false, " +
		"and one enabled through the chart fails open at its default failurePolicy: Ignore). Remove the entries from " +
		"the spec to clear this condition."
	// hostPathDroppedRollingClause fills the second slot on a pass that can see
	// the roll of that template is not finished. A separate sentence rather
	// than a hedge on the first one, so the reader who does not get it reads an
	// unqualified statement, and leading with a space because the slot sits
	// directly after a full stop.
	hostPathDroppedRollingClause = " That template is still rolling out, so Pods from an earlier revision may still be " +
		"running with these volumes mounted."
	hostPathDroppedEntrySeparator = ", "
	// hostPathDroppedEntryBudget bounds the joined entries, and
	// hostPathDroppedOverflowFormat counts whatever did not fit. Volume names
	// and host paths are the author's, and neither the CRD nor this controller
	// bounds their length or their number, while the CRD schema caps a
	// condition message at 32768 characters -- so an unbounded list is one
	// spec away from failing the whole status write, Ready and the phase and
	// every other condition with it, on every pass, for as long as the entries
	// stay in the spec. The budget is far under the cap because the message is
	// read in `kubectl describe`, where the first few entries are what anyone
	// acts on.
	hostPathDroppedEntryBudget    = 4096
	hostPathDroppedOverflowFormat = ", and %d more"
	// hostPathDroppedEntryEllipsis marks a single entry cut to fit the budget,
	// which takes one name or one path longer than the whole list is allowed
	// to be.
	hostPathDroppedEntryEllipsis = "..."

	conditionReasonInvalidGitRepoURL   = "InvalidGitRepoURL"
	conditionReasonCorruptManagedRepos = "CorruptManagedRepos"
	// conditionReasonInvalidCredentialProxyResources: the CR's
	// spec.deployment.credentialProxy.resources fails
	// ValidateCredentialProxyResources, so the override is ignored and the
	// proxy Deployment is rendered at the operator's default resources.
	conditionReasonInvalidCredentialProxyResources = "InvalidCredentialProxyResources"
	invalidCredentialProxyResourcesMsgFmt          = "Invalid spec.deployment.credentialProxy.resources (%s); the override is ignored and the credential-proxy Deployment runs at the operator's default resources until it is corrected" // #nosec G101 -- Condition message, not a credential
	// conditionReasonMinterPruningHeld: a GitHub repository entry the minter
	// sync cannot read holds every tracked policy, so a repository removed
	// from the lists keeps its write policy until the entry is fixed.
	conditionReasonMinterPruningHeld = "MinterPruningHeld"
	// agentRepoRefMaxLength is repo_ref.py's MAX_REPO_LENGTH: the agent
	// refuses a managed_repos value longer than this before parsing it.
	agentRepoRefMaxLength = 256
	// minterHeldEntriesShown caps how many held entries the condition names.
	minterHeldEntriesShown     = 3
	gitopsStateConfigMapSuffix = "-gitops-state"
	managedReposConfigMapKey   = "managed_repos"

	reasonRuntimeClassNotFound = "RuntimeClassNotFound"
	reasonForbiddenVolumeMount = "ForbiddenVolumeMount"
)

var (
	missingShellMessageMarkers = []string{
		"/bin/sh",
		"no such file or directory",
		"executable file not found",
	}

	platformAgentGroupResource = agentv1alpha1.GroupVersion.WithResource("platformagents").GroupResource()
)

// PlatformAgentReconciler reconciles a PlatformAgent object
type PlatformAgentReconciler struct {
	client.Client
	Scheme          *runtime.Scheme
	DiscoveryClient discovery.DiscoveryInterface

	// prunedUsageStatus records, per CR, when the served CRD was last seen to
	// drop status.usage on a write: the operator is running ahead of its CRD.
	// While the record is fresh the Ready writer stops gating on
	// status.usage.activeInterfaces, because a field the schema prunes reads
	// back absent on every pass and would otherwise cost a status write per
	// reconcile — the loop the observedGeneration witness was moved for. After
	// usageStatusReprobeInterval the record expires and the next pass writes
	// once: that lands the field if the CRD has been applied since, and records
	// the pruning again if not. So an applied CRD shows up within one interval
	// on a quiet install, at once when the Ready writer next writes for any
	// other reason (the other status writers carry the field through as read,
	// so a Degraded pass lands nothing new), and a skewed install costs one
	// status write and one log line per interval. The steady-state
	// requeue is capped at the interval while a record is held
	// (usageStatusRequeue), so the probe is scheduled rather than left to the
	// next event. Keyed by ObjectKey, value time.Time; cleared by any write
	// whose echo carries the field, and when the CR is deleted.
	prunedUsageStatus sync.Map

	// credentialProxyWarningsLogged records, per CR, the spec generation whose
	// credential-proxy resources warnings reconcileCredentialProxy last logged,
	// so each generation logs them once. Keyed by ObjectKey, value int64;
	// cleared when the CR is deleted.
	credentialProxyWarningsLogged sync.Map

	// APIReader reads straight from the API server, bypassing the manager's cache.
	// Collector discovery looks at Services in namespaces this operator otherwise never
	// touches, and a cached read there would have the manager start — and keep — an
	// informer watching every Service in the cluster, to serve a handful of reads an
	// hour. Nil falls back to the cached client, which is what tests supply.
	APIReader client.Reader

	// RBAC reports the permissions this controller's RBAC markers declare and
	// the API server denies it — an image deployed ahead of its ClusterRole
	// (#1009). Nil never probes, which is what tests and the golden harness
	// supply; see rbac_selfcheck.go.
	RBAC *RBACChecker

	// Recorder writes Events on the PlatformAgent. Nil records nothing, which
	// is what tests and the golden harness supply (recordEvent).
	//
	// The line between a condition and an Event, drawn once here so the two
	// do not drift. A condition is for a state this reconciler converges on
	// and re-derives on every pass from what it renders and reads back
	// (VolumesDropped, BusProvisioned, A2AGateway, BusCredentialsReady): the
	// pass that owns it writes it, keeps it current and removes it. An Event
	// is for a fact about the live install that something this reconciler
	// ran discovered and no later pass can re-derive without running it
	// again: what the provision Job found on the live TASKS stream
	// (reportA2AProvisionFindings). VolumesDropped went to a condition when
	// there was no recorder, and it stays one, because it is also a state the
	// render re-derives on every pass. An Event costs create;patch on events
	// in the ClusterRole, which the RBAC self-check names when an image runs
	// ahead of its role.
	Recorder record.EventRecorder

	// clusterImageVolumes caches the cluster-wide ImageVolume capability. Server
	// version cannot change without an API server restart, so resolving it once
	// avoids a discovery round-trip on every reconcile of every agent. Only an
	// authoritative probe sets imageVolumeResolved; a failed probe is retried.
	imageVolumeMu       sync.Mutex
	imageVolumeResolved bool
	clusterImageVolumes bool

	// APIServerIP configures the Kubernetes API server control-plane egress CIDR
	// for generated NetworkPolicy manifests.
	APIServerIP string

	// APIServerCIDROverride configures static CIDR overrides for the Kubernetes API server
	// (e.g. from KUBERNETES_API_SERVER_CIDR).
	APIServerCIDROverride string

	// DNSClusterIPOverride configures static override for the Cluster DNS Service ClusterIP
	// (e.g. from KUBERNETES_DNS_CLUSTER_IP or --kubernetes-dns-cluster-ip).
	DNSClusterIPOverride string

	// MetadataDaemonIPOverride configures static override for the Workload Identity metadata daemon IP
	// (e.g. from KUBERNETES_METADATA_DAEMON_IP or --kubernetes-metadata-daemon-ip).
	MetadataDaemonIPOverride string

	// OperatorNamespace is the namespace this operator's pods run in, from
	// POD_NAMESPACE (OperatorNamespaceEnv), which both install paths set on
	// the manager container from the Downward API. The gateway and broker
	// NetworkPolicies admit the operator's pods in it on the metrics ports,
	// for the usage counters poller (usage_counters_poller.go); empty, as
	// under `make run` off the cluster, renders no such rule.
	OperatorNamespace string

	// otelEndpoint caches the discovered OpenTelemetry collector, cluster-wide — there
	// is one collector per cluster, not one per agent. Unlike the ImageVolume
	// capability this expires (otelDiscoveryTTL): a Service can appear or move at any
	// time. See discoveredOTLPEndpoint for the "" / not-determined distinction.
	// otelProbedAt is when a probe was last attempted, successful or not. It exists
	// only to rate-limit retries: an inconclusive probe caches nothing, so without a
	// floor an API outage has every reconcile of every agent re-run the whole sweep.
	otelMu         sync.Mutex
	otelResolved   bool
	otelEndpoint   string
	otelResolvedAt time.Time
	otelProbedAt   time.Time
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=platformagents,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=platformagents/status,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=platformagents/finalizers,verbs=update
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentplugins,verbs=get;list;watch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentplugins/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=apps,resources=deployments;statefulsets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=apps,resources=daemonsets;replicasets,verbs=get;list;watch
// apps/daemonsets is also read by resolveNetpolProfile to discover the gke-metadata-server
// DaemonSet port (issue #747 B4) — a second consumer of a grant that already existed for
// buildMinimalPlatformRole's escalation-prevention requirement.
// +kubebuilder:rbac:groups="",resources=serviceaccounts;persistentvolumeclaims;configmaps;services;pods,verbs=get;list;watch;create;update;patch;delete
// `secrets` and full `jobs` verbs exist for the mode-next A2A stack: the
// generated NATS credentials/config Secrets and the provisioning Job.
// No list/watch: the A2A code does one Get, Create, Update, apply-Patch and
// Delete by name, and a2aReader() exists so those never go through the cache.
// Without the enumeration verbs a cached Secret read cannot be added by accident.
// +kubebuilder:rbac:groups="",resources=secrets,verbs=get;create;update;patch;delete
// +kubebuilder:rbac:groups=batch,resources=jobs,verbs=get;list;watch;create;update;patch;delete
// `nodes` is still required: buildMinimalPlatformRole grants it to the agent audit
// ClusterRole, and RBAC escalation-prevention needs the operator to hold it to apply that.
// +kubebuilder:rbac:groups="",resources=namespaces;nodes;events;persistentvolumes;limitranges;endpoints;pods/log,verbs=get;list;watch
// events create;patch: the Recorder's writes (see the field); patch is what
// the recorder uses to bump the count on a repeat of the same Event.
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch
// Full `resourcequotas` verbs exist for the mode-next session-pod quota (the
// enforcement half of the session cap); everything else only reads quotas.
// +kubebuilder:rbac:groups="",resources=resourcequotas,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=metrics.k8s.io,resources=nodes;pods,verbs=get;list;watch
// +kubebuilder:rbac:groups=autoscaling,resources=horizontalpodautoscalers,verbs=get;list;watch
// +kubebuilder:rbac:groups=batch,resources=cronjobs;jobs,verbs=get;list;watch
// +kubebuilder:rbac:groups=coordination.k8s.io,resources=leases,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=node.k8s.io,resources=runtimeclasses,verbs=get;list;watch
// +kubebuilder:rbac:groups=networking.k8s.io,resources=networkpolicies,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=networking.k8s.io,resources=ingresses,verbs=get;list;watch
// +kubebuilder:rbac:groups=networking.gke.io,resources=fqdnnetworkpolicies,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=policy,resources=poddisruptionbudgets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=rbac.authorization.k8s.io,resources=clusterroles;clusterrolebindings;roles;rolebindings,verbs=get;list;watch;create;update;patch;delete
// The split credential broker verifies its callers with a TokenReview. The operator has to
// hold that permission in order to grant it; it confers no read access and cannot mint a token.
// +kubebuilder:rbac:groups=authentication.k8s.io,resources=tokenreviews,verbs=create
//
// The A2A auth callout also verifies callers with a TokenReview, and under mode next the
// operator binds it to the built-in system:auth-delegator ClusterRole. RBAC escalation
// prevention refuses that binding unless the operator either holds every permission in the
// role it is granting or holds `bind` on that role by name. It holds tokenreviews/create
// above but not subjectaccessreviews/create, which system:auth-delegator also carries — so
// without this line the binding is refused at runtime and the callout never gets TokenReview,
// on every install that turns next on. Measured against a real API server, not inferred:
// without it, "attempting to grant RBAC permissions not currently held"; with it, allowed.
// `bind` scoped by resourceNames is the narrow form — it permits granting this one role and
// confers none of its permissions on the operator itself.
// +kubebuilder:rbac:groups=rbac.authorization.k8s.io,resources=clusterroles,resourceNames="system:auth-delegator",verbs=bind
// `get` and nothing else on secrets, for two callers. checkShellSandboxKeys asks whether
// the sandbox's authorized-keys Secret exists so the status can say so; it never reads a
// value out of one, and the operator creates that Secret nowhere. stampSecretEnvHash does
// read values: it digests the Secret keys a pod consumes as environment onto that pod's
// template, so rotating one rolls the pod (platformagent_secret_hash.go). Both read by
// name. Both go through r.APIReader rather than the cached client on purpose: a cached Get
// of a type the manager does not already watch starts a cluster-wide Secret informer, which
// would both hold every Secret in the cluster in the operator's memory and, on any cluster
// where this grant is trimmed, block WaitForCacheSync forever behind a forbidden LIST.
// +kubebuilder:rbac:groups="",resources=secrets,verbs=get
// +kubebuilder:rbac:groups=apiextensions.k8s.io,resources=customresourcedefinitions,verbs=get;list;watch

// isPlatformAgentConflict reports whether err is an optimistic concurrency conflict
// (409 Conflict) specifically on a PlatformAgent custom resource (Group: kubeagents.x-k8s.io,
// Resource/Kind: platformagents). Conflicts on owned objects (ConfigMaps, Secrets, Deployments,
// NetworkPolicies) return false so they propagate as reconciler errors and surface in
// controller_runtime_reconcile_errors_total (#2281).
func isPlatformAgentConflict(err error) bool {
	if !errors.IsConflict(err) {
		return false
	}
	var statusErr *errors.StatusError
	if goerrors.As(err, &statusErr) && statusErr.ErrStatus.Details != nil {
		d := statusErr.ErrStatus.Details
		return d.Group == platformAgentGroupResource.Group && d.Kind == platformAgentGroupResource.Resource
	}
	return false
}

func (r *PlatformAgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (result ctrl.Result, retErr error) {
	log := logf.FromContext(ctx)

	// Optimistic concurrency conflicts (409 Conflict) on PlatformAgent updates
	// (spec, finalizer, or status updates) occur when owned-object watch events
	// race ahead of the status watch stream or when concurrent reconcile passes
	// update the CR. Instead of letting controller-runtime log an unhandled
	// Reconciler error and increment reconcile_errors_total, requeue cleanly
	// via the workqueue rate limiter so the informer cache catches up and the
	// next pass reconciles against the fresh ResourceVersion (#2281).
	// Owned-object conflicts (e.g. ConfigMaps, Secrets, Deployments) are not
	// caught here and propagate as errors to preserve telemetry and diagnostics.
	defer func() {
		if isPlatformAgentConflict(retErr) {
			log.Info("PlatformAgent update conflict; requeuing cleanly",
				"name", req.Name,
				"namespace", req.Namespace,
				"error", retErr,
			)
			result = ctrl.Result{Requeue: true}
			retErr = nil
		}
	}()

	instance := &agentv1alpha1.PlatformAgent{}
	if err := r.Get(ctx, req.NamespacedName, instance); err != nil {
		if errors.IsNotFound(err) {
			// The AgentPlugin watch enqueues spec.agentRef, so this also fires for
			// plugins pointing at an agent that does not exist. Tell them so, rather
			// than leaving a mistyped agentRef silently statusless forever.
			r.markOrphanedPlugins(ctx, req.Namespace, req.Name)
		}
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	log.Info("Reconciling PlatformAgent", "name", instance.Name, "namespace", instance.Namespace)

	// projectId became required, but CRs stored before that change still
	// reconcile. Without the full triple the credential proxy bootstrap is
	// skipped and kubectl silently resolves to localhost:8080, so say so
	// loudly rather than letting the agent discover it at runtime.
	if h := instance.Spec.Harness; h == nil || h.ProjectID == "" || h.Location == "" || h.ClusterName == "" {
		log.Info("WARNING: spec.harness needs projectId, location, and clusterName; "+
			"without all three the credential proxy skips its kubeconfig bootstrap and kubectl will not reach any cluster",
			"name", instance.Name, "namespace", instance.Namespace)
	}

	// The forge declaration is checked against its provider's rules at
	// admission. CRs stored before a rule tightened still reconcile, but
	// subsequent updates will be rejected by the validating webhook until
	// corrected. Warn loudly so an administrator discovers un-updatable CRs
	// immediately upon operator upgrade. Both spellings go through ValidateGit,
	// the same call admission makes, so the warning cannot pass a declaration
	// the webhook refuses. It names the refused fields, not their values: a
	// clone URL can carry a token, and this is logged on every reconcile.
	if instance.Spec.Integration != nil {
		if err := instance.Spec.Integration.ValidateGit(); err != nil {
			log.Info("WARNING: spec.integration.forges/repositories (or the deprecated spec.integration.github) is invalid; "+
				"updates to this PlatformAgent will be rejected by the admission webhook until corrected",
				"name", instance.Name, "namespace", instance.Namespace, "fields", gitProblemFields(&instance.Spec.Integration.IntegrationSpec))
		}
		r.recordIntegrationWarnings(instance)
	}

	// 1. Intercept Deletion
	if !instance.ObjectMeta.DeletionTimestamp.IsZero() {
		return r.handleDeletion(ctx, instance)
	}

	// 2. Add Finalizer if not present
	if !controllerutil.ContainsFinalizer(instance, platformAgentFinalizer) {
		controllerutil.AddFinalizer(instance, platformAgentFinalizer)
		if err := r.Update(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
		// Return immediately after update to fetch the fresh ResourceVersion, preventing OptimisticLockErrors
		return ctrl.Result{}, nil
	}

	// 2c. Say on the status when the ClusterRole is behind this image, before
	// any step that could fail on it: a reconcile that errors below never
	// reaches updateStatusReady, and without this the only trace of the skew
	// is the `Reconciler error` loop itself (#1009). Writes status and carries
	// on; nothing here withholds a step.
	rbacDegraded, err := r.reportRBACSkew(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// 2b. Validate the mode gate once at the top; everything downstream asks
	// renderMode, which fails closed. An error here is version skew — a newer
	// CRD's mode value this binary does not know (see mode.go). Today's stack
	// still renders below, so the cluster keeps running what it ran; status
	// reports Degraded/ModeNotRecognized at the end instead of Ready.
	_, modeErr := resolveMode(instance)
	if modeErr != nil {
		log.Info("Unrecognized spec.mode; rendering today's stack and reporting Degraded", "error", modeErr.Error())
	}

	// 2d. BusCredentialsReady, written on the way out rather than at a point in
	// the sequence below.
	//
	// It was at the bottom, under everything, which meant a reconcile that
	// parked Degraded above it neither wrote it nor cleared it. Moving it up to
	// just after the bus step fixed two of those parks and left four: the
	// refusals of today's stack at 9b, 9c, 10 and 11e all return above the bus
	// step, and the bus step cannot move above them because it renders on top
	// of what they withhold. There is no position in the sequence that works,
	// so this is not a position in the sequence.
	//
	// Skipping the write is worse than it sounds, and the reason is
	// updateStatusDegraded: it writes Ready alone and preserves every other
	// condition, so the last BusCredentialsReady stands unchallenged for as
	// long as the refusal does. The CR reports a callout serving a named map
	// version through a Deployment that may since have lost every replica.
	//
	// Version skew is the one case with nothing to say. renderMode fails closed
	// to today while cleanupA2A is deliberately not run (see the mode gate
	// below), so the bus a newer CRD rendered is still standing; clearing the
	// condition would report it gone, and rewriting it would claim this binary
	// knows what it describes. Both are worse than leaving it.
	busCredsMapVersion := ""
	if modeErr == nil {
		wantNext := renderMode(instance, "nats") == ModeNext
		defer func() {
			if err := r.syncBusCredentialsReady(ctx, instance, wantNext, busCredsMapVersion); err != nil {
				if retErr == nil {
					retErr = err
					return
				}
				// The reconcile is already failing and will requeue. If the
				// failure is already a PlatformAgent conflict being requeued cleanly,
				// a subsequent conflict writing BusCredentialsReady is the same race
				// and should not log an Error with a stack trace.
				if isPlatformAgentConflict(retErr) && errors.IsConflict(err) {
					log.Info("Conflict writing BusCredentialsReady; pass already requeuing on conflict", "error", err)
				} else {
					log.Error(err, "could not write BusCredentialsReady")
				}
			}
		}()
	}

	// 3. Reconcile Service Account (with Workload Identity annotation)
	if err := r.reconcileServiceAccount(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}
	// 3b. Reconcile RBAC (ClusterRole and ClusterRoleBindings)
	if err := r.reconcileRBAC(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 4. Reconcile PVC for agent persistent data
	if err := r.reconcilePVC(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 5. Resolve agent plugins
	agentPlugins, err := r.resolveAgentPlugins(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// 6. Reconcile ConfigMap (config.yaml content)
	configMapHash, err := r.reconcileConfigMap(ctx, instance, agentPlugins)
	if err != nil {
		return ctrl.Result{}, err
	}

	// 7. Reconcile Fluent Bit ConfigMap
	fluentBitHash, err := r.reconcileFluentBitConfigMap(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// 8. Reconcile Settings ConfigMap
	settingsHash, err := r.reconcileSettingsConfigMap(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// Reconcile Gitops State ConfigMap (create-only to avoid overwriting agent updates)
	if err := r.reconcileGitopsStateConfigMap(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 9. Reconcile Credential Proxy Policy ConfigMap
	proxyPolicyHash, err := r.reconcileCredentialProxyPolicyConfigMap(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}
	// The gateway's annotation leaves out what only the broker reads, so a
	// forge configuration change rolls the broker and not the gateway.
	gatewayPolicyHash, err := getConfigMapHash(gatewayPolicyView(buildCredentialProxyPolicyConfigMap(instance)))
	if err != nil {
		return ctrl.Result{}, err
	}

	// 9b. Refuse a CR that mounts the broker's own volumes into the agent container.
	//
	// The guardrail reconcile before the refusal is the rule step 11e states at
	// length: a refusal withholds the workload, and it must not also withhold a
	// NetworkPolicy, because a policy that stops being reconciled is one an
	// operator can delete permanently — and with nothing selecting the agent Pod,
	// NetworkPolicy permits all egress. Read 11e for why; both refusals here are
	// the same hazard and take the same rescue.
	if msg := validateExtraVolumeMounts(instance); msg != "" {
		log.Info(msg)
		guardrailErr := r.reconcileAgentNetworkGuardrails(ctx, instance)
		if statusErr := r.updateStatusDegraded(ctx, instance, reasonForbiddenVolumeMount, msg, workloadNotRendered); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		if guardrailErr != nil {
			return ctrl.Result{}, guardrailErr
		}
		return ctrl.Result{}, nil
	}

	// 9c. Refuse a CR that asks for the shell sandbox to be switched off.
	//
	// A refusal rather than a silent override: the request cannot be honoured —
	// see validateShellSandbox — and answering it by rendering the opposite
	// leaves an operator reading a field off the running CR that describes
	// nothing. Returning here withholds every later step, so the agent keeps
	// whatever it is already running rather than being half-reconfigured.
	if reason, msg := validateShellSandbox(instance); reason != "" {
		log.Info(msg)
		guardrailErr := r.reconcileAgentNetworkGuardrails(ctx, instance)
		if statusErr := r.updateStatusDegraded(ctx, instance, reason, msg, workloadNotRendered); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		if guardrailErr != nil {
			return ctrl.Result{}, guardrailErr
		}
		return ctrl.Result{}, nil
	}

	// 10. Validate RuntimeClass if specified
	if rcName, err := r.validateRuntimeClass(ctx, instance); err != nil {
		if errors.IsNotFound(err) {
			// The name comes back from the check rather than being read off
			// spec.deployment here: the sandbox has a RuntimeClass field of its
			// own, and dereferencing the agent's would panic on a CR that names
			// only the sandbox one.
			msg := fmt.Sprintf("RuntimeClass '%s' is not configured in this cluster. For GKE Standard, enable GKE Sandbox by provisioning a gVisor node pool first. In GKE Autopilot, gVisor is supported automatically.", rcName)
			log.Info(msg)
			guardrailErr := r.reconcileAgentNetworkGuardrails(ctx, instance)
			if statusErr := r.updateStatusDegraded(ctx, instance, reasonRuntimeClassNotFound, msg, workloadNotRendered); statusErr != nil {
				return ctrl.Result{}, statusErr
			}
			if guardrailErr != nil {
				return ctrl.Result{}, guardrailErr
			}
			return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
		}
		return ctrl.Result{}, fmt.Errorf("failed to validate RuntimeClass: %w", err)
	}

	// 10b. Reconcile the shell sandbox before the credential proxy, and both before
	// the workload that connects to them. Neither client blocks on the proxy — the
	// wrapped CLIs report it unavailable and the chat relay retries its poll — but
	// on a first install this order means the sandbox's ServiceAccount exists
	// before the broker starts authenticating callers against it.
	if err := r.reconcileShellSandbox(ctx, instance, settingsHash); err != nil {
		return ctrl.Result{}, err
	}

	// 10c. Grant the broker the one verb it needs to authenticate its callers,
	// before anything that runs it.
	if err := r.reconcileCredentialBrokerTokenReviewRBAC(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 11. Reconcile the credential proxy: its own Deployment, its Service and the
	// NetworkPolicy narrowing who may reach it.
	if err := r.reconcileCredentialProxy(ctx, instance, proxyPolicyHash); err != nil {
		return ctrl.Result{}, err
	}

	// 11e. Refuse an allowlist destination the policy will not render.
	//
	// Immediately before the workload, deliberately: an operator who asked for
	// the agent Pod to be denied the metadata server must not get a running
	// agent that silently is not. Below the broker reconcile, also
	// deliberately: this refusal is about one destination, and it should not
	// stop the broker being reconciled the way the layout refusal at 10c must.
	if reason, msg := validateEgressAllowlist(instance); reason != "" {
		log.Info(msg)
		// Returning here withholds the workload, the Service, the
		// PodDisruptionBudget, the legacy cleanup and updateStatusReady. What
		// it must not withhold is a guardrail, and the agent Pod has two:
		// <name>-gateway-netpol, which is reconciled below in the normal path
		// and so is reconciled here as well, and <name>-sandbox-metadata-deny,
		// which step 12b renders.
		//
		// Both have to survive a refusal for the same reason. An operator
		// triaging an EgressAllowlistRefused who deletes them gets neither back
		// until the spec is fixed, and with nothing selecting the agent Pod
		// NetworkPolicy permits all egress — so the outcome is wide-open egress
		// behind a Degraded status that names only the allowlist. The gateway
		// policy is unconditional because it has nothing to do with either
		// refusal; it is the Pod's baseline and it predates this field.
		// Steps 9b, 9c, 10, and this one take the same rescue: reconcile network
		// guardrails via reconcileAgentNetworkGuardrails, recording Degraded status
		// before returning any guardrail error so neither the agent gateway policy
		// nor the litellm policy is stranded when reconciliation pauses at Degraded.
		guardrailErr := r.reconcileAgentNetworkGuardrails(ctx, instance)
		if statusErr := r.updateStatusDegraded(ctx, instance, reason, msg, workloadNotRendered); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		if guardrailErr != nil {
			return ctrl.Result{}, guardrailErr
		}
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}

	// 12. Reconcile the Agent Sandbox Pod with its Envoy credential sidecar.
	otlpEndpoint, otlpSource := r.resolveOTLPEndpoint(ctx, instance)
	otlpDisabled := otlpSource == otlpSourceNone
	netpolProf := r.resolveNetpolProfile(ctx, instance)
	if err := r.reconcileWorkload(ctx, instance, configMapHash, fluentBitHash, settingsHash, gatewayPolicyHash, agentPlugins, otlpEndpoint, otlpDisabled); err != nil {
		return ctrl.Result{}, err
	}

	// 12b. Reconcile the agent Pod's default-deny egress policy, if it has one.
	if err := r.reconcileAgentEgressPolicy(ctx, instance, r.agentEgressDNSClusterIPs(ctx, instance, netpolProf), otlpEndpoint); err != nil {
		return ctrl.Result{}, err
	}

	// Reconcile Service
	if err := r.reconcileService(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}
	// Reconcile PodDisruptionBudget
	if err := r.reconcilePodDisruptionBudget(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}
	// Reconcile NetworkPolicy
	if err := r.reconcileNetworkPolicy(ctx, instance, netpolProf, otlpEndpoint, otlpDisabled); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.reconcileLiteLLMNetworkPolicy(ctx, instance, netpolProf); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.deleteLegacyCredentialIsolationResources(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// The mode gate: `next` additionally renders the A2A stack -- NATS, the
	// auth callout, the gateway, the capability verifier, the provisioning
	// Job; `today` keeps the
	// dark stack dark — including tearing it
	// back down after a flip, so `mode` absent renders exactly today's stack
	// rather than today's stack plus leftovers. Version skew touches NEITHER
	// branch: renderMode fails closed to today, and letting that reach
	// cleanupA2A would have a one-version operator rollback tear down a live
	// bus that a newer CRD's mode legitimately rendered. Skew is a status
	// problem (below), not a rendering instruction.
	var a2aState a2aProvisionState
	a2aNext := a2aStackRendering(instance)
	if a2aNext {
		if a2aState, err = r.reconcileA2A(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
	} else if modeErr == nil {
		if err := r.cleanupA2A(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
	}

	// The version this pass rendered, for the deferred write at 2d. Empty on
	// every path that did not get here, where the write reads it back off the
	// ConfigMap the callout watches instead.
	if a2aNext {
		busCredsMapVersion = a2aState.AuthMapVersion
	}

	// 9. Update status phase. While the mode is unrecognized the phase is
	// Degraded with a named reason — silently rendering today at that point
	// would leave nothing in `kubectl describe` saying the cluster runs
	// something other than what the spec asks. Requeue: the skew resolves by
	// an operator upgrade or a spec correction, neither of which is an event
	// on this object's watches.
	if modeErr != nil {
		msg := modeErr.Error() + " (version skew); rendering today's stack until the operator is upgraded or spec.mode is corrected"
		if statusErr := r.updateStatusDegraded(ctx, instance, "ModeNotRecognized", msg, workloadRendered); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}
	if a2aState.failed {
		// The render-derived conditions first (syncA2AConditions): this
		// pass withheld or lit the gateway and may have seen an earlier
		// Job complete, and the Degraded writer below knows none of it.
		if err := r.syncA2AConditions(ctx, instance, a2aState); err != nil {
			return ctrl.Result{}, err
		}
		if statusErr := r.updateStatusDegraded(ctx, instance, "A2AProvisionFailed", a2aState.message, workloadRendered); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}

	// 13. Report an install whose sandbox keypair was never generated.
	//
	// Last, below every reconcile step, because unlike the refusals above it this
	// one withholds nothing: everything is already applied, and the StatefulSet is
	// wanted in place so the pod starts on its own the moment the Secret appears.
	// What is withheld is the Ready status, which would otherwise be the only
	// thing an operator sees while no command the agent runs can execute.
	//
	// Requeued rather than watched. Secrets are not in this controller's watch
	// set, and adding them for one check would wake every reconcile on every
	// Secret write in the namespace.
	if reason, msg := r.checkShellSandboxKeys(ctx, instance); reason != "" {
		log.Info(msg)
		// Same as the failed-Job path: a next install can sit here for
		// days, and its render-derived conditions have to follow the
		// renders it keeps doing. Under today it is the pass that removes
		// the next-mode conditions a flip left behind; on a today install
		// that never had them it writes nothing.
		if err := r.syncA2AConditions(ctx, instance, a2aState); err != nil {
			return ctrl.Result{}, err
		}
		if statusErr := r.updateStatusDegraded(ctx, instance, reason, msg, workloadRendered); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}

	phase, err := r.updateStatusReady(ctx, instance, otlpEndpoint, otlpSource, netpolProf, a2aState)
	if err != nil {
		return ctrl.Result{}, err
	}

	// A plugin image that cannot be pulled only surfaces on the pod seconds after the
	// workload is written, and Pods are not watched here. Requeue while the picture is
	// still incomplete so both the failure and the later recovery reach plugin status.
	if pluginStatusNeedsRecheck(agentPlugins, phase == "Ready") {
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}

	// A2A provisioning still running — Jobs are not watched (see a2aReader),
	// so completion, failure, and the TTL removing a finished Job are all
	// invisible without a requeue. A provision Job held for the callout
	// (a2aProvisionState.jobHeld) is a pass with done=false, so it rides
	// this term too.
	//
	// gatewayHeld shares the requeue rather than getting its own: the gateway
	// is waiting on a callout replica that is both ready and on the current
	// spec (a2aCalloutCanServeANewGateway), and the pass that finally sees one
	// has to be a pass that happens. The callout Deployment is owned and its
	// status changes do trigger one, but a gate that only converges because
	// something else is watched is a gate with a hidden dependency -- and the
	// predicate's false negatives, a terminated pod still counted and an
	// informer copy older than the pass's own apply, clear on a Deployment
	// event the requeue does not need to wait for.
	// gatewayDark shares it too: the discord-bot Secret is not watched, so
	// its creation is invisible without a requeue, and the pass that renders
	// the gateway once it exists has to be a pass that happens.
	if a2aNext && (!a2aState.done || a2aState.gatewayHeld || a2aState.gatewayDark) {
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}

	// An out-of-date ClusterRole is fixed by someone re-applying the manifests,
	// which triggers no reconcile of its own, so poll while the condition
	// stands rather than leave it until an unrelated event (rbac_selfcheck.go).
	if rbacDegraded {
		return ctrl.Result{RequeueAfter: rbacReprobeInterval}, nil
	}

	// Secret material reaches the gateway as environment, and a container's
	// environment is fixed for the life of the pod. Secrets are deliberately
	// outside this controller's watch set (see the grant above), so nothing
	// wakes a reconcile when one changes: the pass that re-reads them is the
	// only thing that notices a rotated key, and it has to be asked for.
	// stampSecretEnvHash does the reading; this is what guarantees it happens
	// while nothing else about the agent is changing.
	requeueAfter := secretEnvReprobeInterval

	// Default and None are the telemetry outcomes that can improve without anything else
	// changing — someone installs a collector and nothing about this agent is touched.
	// Reconciles are event-driven and can be quiet for hours, so nudge the probe rather
	// than wait for an unrelated event. Every other source is explicit or already found
	// something, and needs no polling. None especially: it is the outcome that leaves the
	// agent exporting nowhere, so it is the one an operator most wants picked up promptly
	// once they install a collector. Taken as a deadline rather than the answer, because
	// the secret re-read above has one of its own and the sooner of the two wins. The two
	// intervals are equal today, so this min picks neither — it is here so that shortening
	// one of them later does not silently lengthen the other.
	if otlpSource == otlpSourceDefault || otlpSource == otlpSourceNone {
		requeueAfter = min(requeueAfter, otelRediscoverAfter)
	}
	// A served CRD without status.usage is fixed by someone applying the CRD,
	// which triggers no reconcile of its own, so while the pruning record is
	// held the probe is scheduled at its interval rather than left to the next
	// event — the same pairing rbacDegraded has above. Without this the record
	// would expire and sit until the fifteen-minute requeue.
	requeueAfter = min(requeueAfter, r.usageStatusRequeue(instance))
	return ctrl.Result{RequeueAfter: requeueAfter}, nil
}

// usageStatusRequeue is how soon the steady-state requeue has to fire for the
// pruned-status probe: the interval while a record is held for this CR. With no
// record it returns the secret re-read's interval, the ceiling the caller
// already applies, so the min it feeds leaves the caller's value alone.
func (r *PlatformAgentReconciler) usageStatusRequeue(agent *agentv1alpha1.PlatformAgent) time.Duration {
	if r.usageStatusPruned(agent) {
		return usageStatusReprobeInterval
	}
	return secretEnvReprobeInterval
}

// pluginStatusNeedsRecheck reports whether plugin status is still provisional.
//
// While the agent has not reached Ready its pod may yet fail to pull a plugin image, so
// a plugin currently marked Ready cannot be trusted as final. Once a plugin is in
// ImagePullFailed we keep looking so that fixing the image clears the condition. Both
// conditions settle, so this terminates rather than requeueing forever.
func pluginStatusNeedsRecheck(plugins []*agentv1alpha1.AgentPlugin, agentReady bool) bool {
	if len(plugins) == 0 {
		return false
	}
	if !agentReady {
		return true
	}
	for _, plugin := range plugins {
		cond := meta.FindStatusCondition(plugin.Status.Conditions, "Ready")
		if cond == nil || cond.Reason == "ImagePullFailed" {
			return true
		}
	}
	return false
}

func (r *PlatformAgentReconciler) handleDeletion(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (ctrl.Result, error) {
	if controllerutil.ContainsFinalizer(agent, platformAgentFinalizer) {
		// Delete the credential broker's TokenReview grant, if the split ever
		// created one. cleanupAgentRBAC's label-driven pass also reaps it under
		// deleteAll, but only when the grant carries the instance labels — one
		// applied before applyManaged stamped them would be orphaned
		// cluster-scoped RBAC. Named explicitly for that reason.
		tokenReviewName := fmt.Sprintf("kubeagents:tokenreview:%s:%s", agent.Namespace, agent.Name)
		crbTokenReview := &rbacv1.ClusterRoleBinding{ObjectMeta: metav1.ObjectMeta{Name: tokenReviewName}}
		if err := client.IgnoreNotFound(r.Delete(ctx, crbTokenReview)); err != nil {
			return ctrl.Result{}, err
		}
		crTokenReview := &rbacv1.ClusterRole{ObjectMeta: metav1.ObjectMeta{Name: tokenReviewName}}
		if err := client.IgnoreNotFound(r.Delete(ctx, crTokenReview)); err != nil {
			return ctrl.Result{}, err
		}
		if err := r.cleanupAgentRBAC(ctx, agent, true); err != nil {
			return ctrl.Result{}, err
		}

		// The auth callout's ClusterRoleBinding, which nothing else reclaims.
		//
		// It is cluster-scoped, so it carries no owner reference — the garbage
		// collector treats a cluster-scoped object owned by a namespaced one as
		// an orphan and deletes it immediately, which is worse than leaking it.
		// cleanupAgentRBAC's two label sweeps do not reach it either: the first
		// selects agent-name/agent-namespace labels that commonLabels does not
		// set, the second selects part-of=kube-agents which a2aLabels overrides
		// to a2a-next, and both then require a kubeagents-prefixed name.
		//
		// Left behind, it is a standing grant of tokenreviews/create and
		// subjectaccessreviews/create to a ServiceAccount name in a namespace,
		// surviving the workload it was minted for — so anyone who can later
		// create a ServiceAccount of that name inherits it. cleanupA2A reaps it
		// on a mode flip; this is the other way the stack can go away.
		if err := r.deleteA2ACalloutClusterRoleBinding(ctx, agent); err != nil {
			return ctrl.Result{}, err
		}

		// Delete managed litellm-policy during finalizer teardown only if Deployment/litellm
		// is absent or terminating. litellm-policy is owned by Deployment/litellm (via OwnerReference)
		// so that deleting PlatformAgent does not strip NetworkPolicy protection while LiteLLM
		// is still running. When Deployment/litellm is deleted (e.g. helm uninstall), Kubernetes
		// garbage collection cleans up litellm-policy automatically.
		var litellmDep appsv1.Deployment
		depErr := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: litellmDeploymentName}, &litellmDep)
		if depErr != nil && !errors.IsNotFound(depErr) {
			return ctrl.Result{}, fmt.Errorf("failed to get LiteLLM deployment during deletion cleanup: %w", depErr)
		}
		if errors.IsNotFound(depErr) || (depErr == nil && litellmDep.DeletionTimestamp != nil) {
			if err := r.deleteManagedLiteLLMPolicy(ctx, agent); err != nil {
				return ctrl.Result{}, err
			}
		}

		// The NATS StatefulSet's volumeClaimTemplate PVC has no owner
		// reference (nothing from a template does), so without this a
		// deleted next-mode agent leaks its 40Gi JetStream volume. Guarded by
		// the instance label the claim template stamps, because a name is not
		// ownership: a PVC squatting this exact name that this render did not
		// create is left alone rather than destroyed.
		a2aPVC := &corev1.PersistentVolumeClaim{}
		pvcKey := client.ObjectKey{Name: a2aNATSDataClaim + "-" + a2aNATSName(agent) + "-0", Namespace: agent.Namespace}
		switch err := r.Client.Get(ctx, pvcKey, a2aPVC); {
		case err == nil:
			if a2aPVC.Labels[labelInstance] == instanceLabel(agent.Namespace, agent.Name) {
				if err := client.IgnoreNotFound(r.Delete(ctx, a2aPVC)); err != nil {
					return ctrl.Result{}, err
				}
			}
		case client.IgnoreNotFound(err) != nil:
			return ctrl.Result{}, err
		}

		// Resource is deleted. Safe to remove finalizer and update.
		r.forgetUsageStatus(agent)
		r.credentialProxyWarningsLogged.Delete(client.ObjectKeyFromObject(agent))
		controllerutil.RemoveFinalizer(agent, platformAgentFinalizer)
		if err := r.Update(ctx, agent); err != nil {
			return ctrl.Result{}, err
		}
	}
	return ctrl.Result{}, nil
}

// applyManaged stamps the recommended labels onto obj and applies it.
//
// Every object this controller writes goes through here, so a newly added
// resource cannot reach the cluster unlabelled. Owner references are still set
// by the caller: the cluster-scoped RBAC objects deliberately have none,
// because a namespaced owner cannot own a cluster-scoped resource.
func (r *PlatformAgentReconciler) applyManaged(ctx context.Context, agent *agentv1alpha1.PlatformAgent, obj client.Object) error {
	withCommonLabels(obj, agent)
	return r.Patch(ctx, obj, client.Apply, client.ForceOwnership, client.FieldOwner(fieldOwner))
}

func (r *PlatformAgentReconciler) reconcileServiceAccount(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" && len(agent.Spec.Security.ServiceAccountAnnotations) == 0 {
		return nil
	}

	saName := agent.Name
	var annotations map[string]string
	if agent.Spec.Security != nil {
		if agent.Spec.Security.ServiceAccountName != "" {
			saName = agent.Spec.Security.ServiceAccountName
		}
		annotations = agent.Spec.Security.ServiceAccountAnnotations
	}

	return ReconcileServiceAccount(ctx, r.Client, r.Scheme, agent, saName, agent.Namespace, annotations, commonLabels(agent), fieldOwner)
}

func (r *PlatformAgentReconciler) reconcilePVC(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	pvcs := []*corev1.PersistentVolumeClaim{
		buildPVC(agent),
		buildSystemPVC(agent),
	}
	customPVCs, err := buildCustomPVCs(agent)
	if err != nil {
		return fmt.Errorf("failed to build custom PVCs: %w", err)
	}
	pvcs = append(pvcs, customPVCs...)
	for _, pvc := range pvcs {
		if err := r.reconcilePersistentVolumeClaim(ctx, agent, pvc); err != nil {
			return err
		}
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcilePersistentVolumeClaim(ctx context.Context, agent *agentv1alpha1.PlatformAgent, pvc *corev1.PersistentVolumeClaim) error {
	if err := ctrl.SetControllerReference(agent, pvc, r.Scheme); err != nil {
		return err
	}
	// PVCs are created once and never updated, so this labels new claims only;
	// claims from before this change stay unlabelled until they are recreated.
	withCommonLabels(pvc, agent)

	found := &corev1.PersistentVolumeClaim{}
	err := r.Get(ctx, client.ObjectKey{Name: pvc.Name, Namespace: pvc.Namespace}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			return r.Create(ctx, pvc)
		}
		return err
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcileConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent, agentPlugins []*agentv1alpha1.AgentPlugin) (string, error) {
	cm := buildConfigMap(agent, agentPlugins)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.applyManaged(ctx, agent, cm)
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

func (r *PlatformAgentReconciler) reconcileFluentBitConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, error) {
	cm := buildFluentBitConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.applyManaged(ctx, agent, cm)
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

func (r *PlatformAgentReconciler) reconcileSettingsConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, error) {
	cm := buildSettingsConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.applyManaged(ctx, agent, cm)
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

func parseManagedRepoEntries(raw string) ([]agentv1alpha1.ManagedRepoEntry, error) {
	entries, _, err := parseManagedRepoEntriesAt(raw)
	return entries, err
}

// parseManagedRepoEntriesAt is parseManagedRepoEntries with each entry's index
// in the JSON array, which a blank entry it drops would otherwise shift.
func parseManagedRepoEntriesAt(raw string) ([]agentv1alpha1.ManagedRepoEntry, []int, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil, nil
	}
	if !strings.HasPrefix(raw, "[") {
		return nil, nil, fmt.Errorf("managed_repos JSON must be an array starting with '['")
	}
	var entries []agentv1alpha1.ManagedRepoEntry
	if err := json.Unmarshal([]byte(raw), &entries); err != nil {
		return nil, nil, fmt.Errorf("failed to unmarshal managed_repos JSON: %w", err)
	}
	var res []agentv1alpha1.ManagedRepoEntry
	var positions []int
	for i, e := range entries {
		u := strings.TrimSpace(e.URL)
		t := strings.TrimSpace(e.Type)
		if u != "" && t != "" {
			res = append(res, agentv1alpha1.ManagedRepoEntry{Type: t, URL: u})
			positions = append(positions, i)
		}
	}
	return res, positions, nil
}

func parseManagedRepos(raw string) ([]string, error) {
	entries, err := parseManagedRepoEntries(raw)
	if err != nil {
		return nil, err
	}
	var res []string
	for _, e := range entries {
		res = append(res, e.URL)
	}
	return res, nil
}

// reconcileGitopsStateConfigMap ensures the <agent-name>-gitops-state ConfigMap exists to track
// the agent's repositories. The repositories declared in spec.integration.repositories (or the
// deprecated spec.integration.github.gitRepo) are seeded into it — the GitOps and managed ones
// into managed_repos, the context ones into context_repos — and kept present on subsequent
// reconciles without removing any additional repositories added to the ConfigMap.
//
// Repository lifecycle and removal:
// The reconciler adds each declared repository to its list if it is not already present in
// the ConfigMap, preserving all existing entries. A missing GitOps repository goes to the front
// of managed_repos, because agent-side consumers read the first entry as the GitOps repository;
// the rest are appended. An entry already present is never moved. Repository removal/unregistration is
// administrator-driven via the ConfigMap: to unregister a repository, remove its entry directly
// from the list in the <agent-name>-gitops-state ConfigMap. If the repository to be removed is
// declared on the CR, remove it there as well so the reconciler does not re-append it on
// subsequent passes.
func (r *PlatformAgentReconciler) reconcileGitopsStateConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	logger := logf.FromContext(ctx)
	cm := buildGitopsStateConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return err
	}

	found := &corev1.ConfigMap{}
	err := r.Get(ctx, client.ObjectKey{Name: cm.Name, Namespace: cm.Namespace}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			withCommonLabels(cm, agent)
			if err := r.Create(ctx, cm); err != nil {
				return err
			}
			return r.syncGithubTokenMinterConfigMap(ctx, agent, cm.Data[gitopsStateManagedReposKey], cm.Data[gitopsStateContextReposKey])
		}
		return err
	}

	// For each list the CR seeds, ensure its repositories are recorded without
	// overwriting other dynamically added repositories. A list that cannot be
	// parsed, on either side, is left as it is: the ConfigMap is
	// administrator-writable, and rewriting a value the operator does not
	// understand would lose it.
	updated := false
	gitops := seededGitOpsEntry(agent)
	for _, key := range []string{gitopsStateManagedReposKey, gitopsStateContextReposKey} {
		seeded := cm.Data[key]
		if seeded == "" {
			continue
		}
		if found.Data == nil {
			found.Data = map[string]string{}
		}
		existing := strings.TrimSpace(found.Data[key])
		if existing == "" {
			found.Data[key] = seeded
			updated = true
			continue
		}
		specEntries, err := parseManagedRepoEntries(seeded)
		if err != nil {
			logger.Error(err, "skipping gitops state reconcile due to unparseable spec repository JSON", "list", key)
			continue
		}
		existingEntries, err := parseManagedRepoEntries(existing)
		if err != nil {
			logger.Error(err, "skipping gitops state reconcile due to unparseable existing list in ConfigMap",
				"configMap", found.Name, "list", key)
			continue
		}
		// A GitOps repository the list lacks goes first rather than last. The
		// entries carry no role, and the agent's token refresh mints for the
		// first managed entry when no repository is named, so appending would
		// leave whichever repository was first before -- the previous GitOps
		// repository, typically -- the one the token is scoped to. An entry
		// already in the list is never moved.
		var front, missing []agentv1alpha1.ManagedRepoEntry
		for _, se := range specEntries {
			present := false
			for _, ee := range existingEntries {
				if sameManagedRepo(ee, se) {
					present = true
					break
				}
			}
			if !present {
				if key == gitopsStateManagedReposKey && gitops != nil && sameManagedRepo(se, *gitops) {
					front = append(front, se)
				} else {
					missing = append(missing, se)
				}
				existingEntries = append(existingEntries, se)
			}
		}
		if len(front)+len(missing) > 0 {
			merged, err := mergeRepoEntries(existing, front, missing)
			if err != nil {
				logger.Error(err, "skipping gitops state reconcile; could not append to the list", "list", key)
				continue
			}
			found.Data[key] = merged
			updated = true
		}
	}
	if updated {
		if err := r.Update(ctx, found); err != nil {
			return err
		}
	}

	return r.syncGithubTokenMinterConfigMap(ctx, agent, found.Data[gitopsStateManagedReposKey], found.Data[gitopsStateContextReposKey])
}

// mergeRepoEntries puts front before a repository list's JSON and back after
// it, keeping every existing element's content as written; encoding/json only
// compacts its whitespace. Round-tripping the list through ManagedRepoEntry
// would drop what that type does not model — a context_repos entry's `ref`,
// or any field an administrator or a later agent version adds — and the
// entries parseManagedRepoEntries skips as incomplete.
func mergeRepoEntries(existing string, front, back []agentv1alpha1.ManagedRepoEntry) (string, error) {
	var raw []json.RawMessage
	if err := json.Unmarshal([]byte(existing), &raw); err != nil {
		return "", err
	}
	encode := func(entries []agentv1alpha1.ManagedRepoEntry) ([]json.RawMessage, error) {
		var out []json.RawMessage
		for _, e := range entries {
			b, err := json.Marshal(e)
			if err != nil {
				return nil, err
			}
			out = append(out, b)
		}
		return out, nil
	}
	head, err := encode(front)
	if err != nil {
		return "", err
	}
	tail, err := encode(back)
	if err != nil {
		return "", err
	}
	raw = append(append(head, raw...), tail...)
	out, err := json.Marshal(raw)
	if err != nil {
		return "", err
	}
	return string(out), nil
}

// sameManagedRepo reports whether two managed_repos URLs name one repository.
//
// A string comparison is not enough across an upgrade. An entry written by
// hand, or by an older operator, can spell the repository the operator now
// seeds canonically as "https://github.com/o/r" with a ".git" suffix or as a
// remote. Comparing the strings would append a second entry for the same
// repository: the agent would sweep it twice, and the old spelling would
// stay, since removal is administrator-driven by design. An entry in another
// case is another spelling to the agent, which compares managed slugs exactly,
// so it is seeded beside it, as a string comparison always did.
//
// Only an entry the agent reads counts as present, since treating one it
// skips as the seeded repository would leave the agent with none. The agent
// matches the type exactly, so "GitHub" is not "github", and it reads a GitHub
// entry only on github.com or as the bare owner/name shorthand, so
// "https://www.github.com/o/r" and a schemeless "www.github.com/o/r" are
// skipped. An unregistered type, and values the provider cannot resolve, are
// not treated as equal either: this ConfigMap is administrator-writable, so a
// value the operator does not understand is one it must leave alone.
func sameManagedRepo(existing, seeded agentv1alpha1.ManagedRepoEntry) bool {
	if existing.Type != seeded.Type {
		return false
	}
	if existing.URL == seeded.URL {
		return true
	}
	// repo_ref.py reads a host after a remote's first `@` where git and this
	// parser read it after the last, so the agent skips an entry with two.
	if strings.Count(existing.URL, "@") > 1 {
		return false
	}
	// The agent refuses a value over its bound before it reads the syntax;
	// this parser's bound is MaxGitRepoURLLength, so userinfo can push an
	// entry past the agent's and still resolve here.
	if utf8.RuneCountInString(strings.TrimSpace(existing.URL)) > agentRepoRefMaxLength {
		return false
	}
	provider, err := agentv1alpha1.LookupGitProvider(seeded.Type)
	if err != nil {
		return false
	}
	// The provider lifts every spelling of its host out of a schemeless path,
	// dropping a `user@` before it; the agent lifts only the canonical one,
	// and only bare, so `git@github.com/o/r` (an scp remote with its colon
	// mistyped) is one it skips, like any other host.
	if url := strings.TrimLeft(strings.TrimSpace(existing.URL), "/"); !strings.Contains(url, "://") {
		if first, _, _ := strings.Cut(url, "/"); strings.Contains(first, "@") && !strings.Contains(first, ":") {
			return false
		}
	}
	parsed, err := provider.ParseRepoRef(existing.URL)
	if err != nil {
		return false
	}
	if parsed.Host != "" && parsed.Host != provider.DefaultHost {
		return false
	}
	existingRef, err := provider.Resolve("", existing.URL, "")
	if err != nil {
		return false
	}
	seededRef, err := provider.Resolve("", seeded.URL, "")
	if err != nil {
		return false
	}
	// The path compares exactly. GitHub folds case, but the agent's readers of
	// managed_repos do not: the `--repo` allowlists and the token scope match
	// the spelling, so an entry in another case is not the declared one to
	// them, and counting it present would refuse `--repo` spelt as declared.
	return existingRef.Host == seededRef.Host && existingRef.Path == seededRef.Path
}

func parseManagedKeysAnnotation(ann string) map[string]struct{} {
	keys := make(map[string]struct{})
	if strings.TrimSpace(ann) == "" {
		return keys
	}
	for _, k := range strings.Split(ann, ",") {
		k = strings.TrimSpace(k)
		if k != "" {
			keys[k] = struct{}{}
		}
	}
	return keys
}

func serializeManagedKeysAnnotation(keys map[string]struct{}) string {
	var list []string
	for k := range keys {
		list = append(list, k)
	}
	sort.Strings(list)
	return strings.Join(list, ",")
}

var minterRepoRegex = regexp.MustCompile(`(?m)^(\s*repositories:\s*\n)(?:\s*-\s*.*?\n)+`)

func renderRepoPolicy(baseTemplate string, repos []string) string {
	return minterRepoRegex.ReplaceAllStringFunc(baseTemplate, func(match string) string {
		lines := strings.Split(match, "\n")
		prefix := lines[0]
		indent := ""
		for _, ch := range prefix {
			if ch == ' ' || ch == '\t' {
				indent += string(ch)
			} else {
				break
			}
		}
		itemIndent := indent + "  "
		var sb strings.Builder
		sb.WriteString(prefix)
		for _, r := range repos {
			sb.WriteString("\n")
			sb.WriteString(itemIndent)
			sb.WriteString("- '")
			sb.WriteString(r)
			sb.WriteString("'")
		}
		sb.WriteString("\n")
		return sb.String()
	})
}

// renderReadOnlyPolicy renders the policy a context repository gets: baseTemplate
// with its scope map reduced to minterReadScope alone and that scope's
// repositories replaced by repos. Everything else at the top level (version,
// rule) is carried over as parsed.
//
// Parsed rather than edited by regex like renderRepoPolicy, because dropping
// the write scope is a structural edit: a text substitution that removes one
// mapping from a block it did not author is how a policy ends up carrying a
// scope nobody meant it to. sigs.k8s.io/yaml marshals through JSON, so keys come
// out sorted and the rendering is stable across reconciles.
//
// The second return is false when baseTemplate does not parse or carries no
// minterReadScope; the caller then renders no context policies rather than
// inventing a scope the minter was never told about.
func renderReadOnlyPolicy(baseTemplate string, repos []string) (string, bool) {
	var doc map[string]interface{}
	if err := yaml.Unmarshal([]byte(baseTemplate), &doc); err != nil || doc == nil {
		return "", false
	}
	scopes, ok := doc[minterScopeField].(map[string]interface{})
	if !ok {
		return "", false
	}
	readScope, ok := scopes[minterReadScope].(map[string]interface{})
	if !ok {
		return "", false
	}
	repoList := make([]interface{}, 0, len(repos))
	for _, repo := range repos {
		repoList = append(repoList, repo)
	}
	readScope[minterRepositoriesField] = repoList
	doc[minterScopeField] = map[string]interface{}{minterReadScope: readScope}
	rendered, err := yaml.Marshal(doc)
	if err != nil {
		return "", false
	}
	return string(rendered), true
}

// minterBareRepos returns the bare repository names in reposStr (a managed_repos
// or context_repos JSON list) that belong to primaryOrg, deduplicated and
// sorted. An empty primaryOrg accepts every organisation, as the managed sync
// always has. listName is for the log lines only. A list that is not JSON is
// an error, never an empty result: the caller skips the whole sync on it,
// because an empty result would read as "no repositories" and prune every
// policy the operator tracks. An entry that does not resolve is returned in
// unreadable, so the caller can tell a repository that left the list from one
// it could not read. It is named by list and index, as "managed_repos[2]",
// never by value: a hand-written clone URL can carry a credential, and the
// names reach the operator's log and the CR's status. The parser's errors
// quote the value too, so they are not logged either.
func minterBareRepos(logger logr.Logger, reposStr, primaryOrg, listName string) (bare, unreadable []string, err error) {
	reposStr = strings.TrimSpace(reposStr)
	if reposStr == "" {
		return nil, nil, nil
	}
	entries, positions, err := parseManagedRepoEntriesAt(reposStr)
	if err != nil {
		return nil, nil, fmt.Errorf("unparseable %s in ConfigMap: %w", listName, err)
	}
	seen := make(map[string]struct{}, len(entries))
	for i, entry := range entries {
		// Another forge's entry is not the minter's: it never had a policy, and
		// the agent skips it by type too. Read as a GitHub URL it would be
		// unreadable, and hold every tracked policy for as long as it is listed.
		if entry.Type != agentv1alpha1.GitProviderGitHub {
			logger.V(1).Info("skipping a non-GitHub repository entry in minter policy sync", "list", listName, "index", positions[i], "type", entry.Type)
			continue
		}
		fullRepo := entry.URL
		slug, err := agentv1alpha1.CleanRepoSlugWithOrg(fullRepo, primaryOrg)
		if agentSpelling, ok := agentURLSpelling(fullRepo); err != nil && ok {
			slug, err = agentv1alpha1.CleanRepoSlugWithOrg(agentSpelling, primaryOrg)
		}
		if err != nil {
			entryName := fmt.Sprintf("%s[%d]", listName, positions[i])
			logger.Info("skipping a repository entry the minter policy sync cannot read", "entry", entryName)
			unreadable = append(unreadable, entryName)
			continue
		}
		parts := strings.SplitN(slug, "/", 2)
		if len(parts) != 2 {
			continue
		}
		repoOrg, bareRepo := parts[0], parts[1]
		if primaryOrg != "" && !strings.EqualFold(repoOrg, primaryOrg) {
			logger.Info("skipping cross-org repository in minter policy sync; minter is scoped to primary org",
				"list", listName, "repo", slug, "repoOrg", repoOrg, "primaryOrg", primaryOrg)
			continue
		}
		if bareRepo+minterPolicyKeySuffix == minterBaseTemplateKey {
			// The base template is never claimed: a policy rendered under its
			// key becomes the template the next reconcile derives every policy
			// from, and a read-only rendering there strips the write scope from
			// every managed repository.
			logger.Info("skipping repository whose minter policy key would be the base template",
				"list", listName, "repo", slug, "key", minterBaseTemplateKey)
			continue
		}
		if _, exists := seen[bareRepo]; exists {
			continue
		}
		seen[bareRepo] = struct{}{}
		bare = append(bare, bareRepo)
	}
	sort.Strings(bare)
	return bare, unreadable, nil
}

// agentURLSchemeRegex is the scheme urlsplit recognises before `://`.
var agentURLSchemeRegex = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9+.-]*$`)

// agentURLSpelling reads a URL as the agent does, and returns it as the
// `https://host/path` that names the same repository, for the parser to read
// or refuse. The agent takes any URL through urlsplit, whose scheme is not
// allowlisted (`git+ssh://`, `file://github.com/...`), whose hostname is the
// text after the last `@` and before the first colon, never validating a port,
// and whose path ends at a query or fragment; tabs and line breaks anywhere in
// the value are deleted before any of that. This parser refuses all of
// those, but the agent uses such an entry, so the minter must count it rather
// than hold every policy for an entry that is not broken. What the agent
// refuses stays refused: a value over its length bound, an empty host, and a
// path that is one segment once the port slot is gone
// (`https://github.com:owner/repo`).
func agentURLSpelling(value string) (string, bool) {
	// The bound is checked as the agent does, in code points and before
	// urlsplit deletes every tab, carriage return and newline in the value.
	text := strings.TrimSpace(value)
	if utf8.RuneCountInString(text) > agentRepoRefMaxLength {
		return "", false
	}
	text = strings.NewReplacer("\t", "", "\r", "", "\n", "").Replace(text)
	scheme, rest, ok := strings.Cut(text, "://")
	if !ok || !agentURLSchemeRegex.MatchString(scheme) {
		return "", false
	}
	if cut := strings.IndexAny(rest, "?#"); cut != -1 {
		rest = rest[:cut]
	}
	authority, path, _ := strings.Cut(rest, "/")
	// urlsplit refuses a bracket without its pair anywhere in the netloc.
	if strings.Contains(authority, "[") != strings.Contains(authority, "]") {
		return "", false
	}
	if at := strings.LastIndex(authority, "@"); at != -1 {
		// With any bracket in the userinfo, urlsplit requires the host to be
		// an address literal, so it refuses every GitHub spelling.
		if strings.ContainsAny(authority[:at], "[]") {
			return "", false
		}
		authority = authority[at+1:]
	}
	if strings.HasPrefix(authority, "[") {
		return "", false
	}
	host, _, _ := strings.Cut(authority, ":")
	if host == "" {
		return "", false
	}
	return "https://" + host + "/" + path, true
}

// minterPrimaryOrg is the organisation the minter policies are scoped to, or,
// in skip, why the sync must leave them as they were.
func minterPrimaryOrg(agent *agentv1alpha1.PlatformAgent) (primaryOrg, skip string) {
	if agent.Spec.Integration == nil {
		return "", ""
	}
	resolved, err := agent.Spec.Integration.ResolveGit()
	if err != nil {
		// Both spellings set. Nothing can say which forge the organisation
		// is read from, and an empty primaryOrg would accept every
		// organisation, so leave the policies as they were. The reconcile
		// status already reports the declaration.
		return "", "git integration does not resolve: " + err.Error()
	}
	// An empty primaryOrg accepts every organisation, and a primary
	// organisation chosen from what validation left standing can be
	// another forge's or another repository's. Where validation refused
	// something the organisation is read from, syncing would widen or
	// move the policies rather than leave them as they were. Skip the
	// sync until the declaration is fixed; the reconcile status already
	// reports it.
	if resolved.ScopeRefused(agentv1alpha1.GitProviderGitHub) {
		return "", "validation refuses something the github organisation is read from"
	}
	return resolved.PrimaryNamespace(agentv1alpha1.GitProviderGitHub), ""
}

// minterHeldEntries returns the GitHub repository entries that stop
// syncGithubTokenMinterConfigMap from pruning, for the Degraded condition: a
// revocation that silently does not happen is what a condition reports. It is
// nil where the sync would not run, or would have nothing to prune: no minter
// ConfigMap, no base template, or no policy the operator tracks.
func (r *PlatformAgentReconciler) minterHeldEntries(ctx context.Context, agent *agentv1alpha1.PlatformAgent, managedReposStr, contextReposStr string) []string {
	minterCM := &corev1.ConfigMap{}
	if err := r.Get(ctx, client.ObjectKey{Name: minterConfigMapName, Namespace: agent.Namespace}, minterCM); err != nil {
		return nil
	}
	if strings.TrimSpace(minterCM.Data[minterBaseTemplateKey]) == "" ||
		len(parseManagedKeysAnnotation(minterCM.Annotations[AnnotationManagedMinterKeys])) == 0 {
		return nil
	}
	primaryOrg, skip := minterPrimaryOrg(agent)
	if skip != "" {
		return nil
	}
	_, managed, errManaged := minterBareRepos(logr.Discard(), managedReposStr, primaryOrg, gitopsStateManagedReposKey)
	_, contextHeld, errContext := minterBareRepos(logr.Discard(), contextReposStr, primaryOrg, gitopsStateContextReposKey)
	if errManaged != nil || errContext != nil {
		return nil
	}
	return slices.Concat(managed, contextHeld)
}

// minterHeldMessage names at most minterHeldEntriesShown held entries, by the
// list-and-index names minterBareRepos gives them: the values are
// administrator-written URLs that can carry a credential, and the Degraded
// message is readable by anyone who can read the PlatformAgent.
func minterHeldMessage(cmName string, held []string) string {
	shown := strings.Join(held[:min(len(held), minterHeldEntriesShown)], hostPathDroppedEntrySeparator)
	if extra := len(held) - minterHeldEntriesShown; extra > 0 {
		shown += fmt.Sprintf(" and %d more", extra)
	}
	return fmt.Sprintf("GitHub repository entries in ConfigMap %s cannot be read (%s); no minter policy is pruned, so a repository removed from the lists keeps its write policy, until they are corrected or removed", cmName, shown)
}

// syncGithubTokenMinterConfigMap ensures that for every repository in managed_repos that belongs
// to the primary GitHub organization (minterPrimaryOrg, from the declaration), a corresponding <repo>.yaml
// entry exists in github-token-minter-config ConfigMap, and that every same-organization
// repository in context_repos has a <repo>.yaml carrying the read-only scope alone.
// Repositories belonging to a different organization are skipped because the minter instance is
// bound to the primary organization directory (/etc/minty/<primary-org>/).
//
// A managed repository's policy is default.yaml with the repository list replaced by every
// same-org managed repository (renderRepoPolicy). A context repository's policy is default.yaml
// reduced to minterReadScope, listing every same-org context repository (renderReadOnlyPolicy):
// the broker mints from it for its own clone and nothing else, so a private context repository
// is readable without a write token ever covering it. A repository in both lists is managed and
// keeps the write rendering. A default.yaml without the read scope renders no context policies.
//
// Key ownership contract:
// The operator owns every <repo>.yaml key for an active managed or context repository (including
// adopting pre-rendered chart or template keys). Hand-editing <repo>.yaml keys for active
// repositories is unsupported: custom edits will be overwritten with policy rendered from
// default.yaml on reconcile, and the key will be pruned when the repository is unregistered from
// both lists — unless a GitHub entry in either list cannot be read, which holds all pruning
// (Degraded/MinterPruningHeld) until it is fixed. Keys for repositories present in neither list
// (and default.yaml itself) are never claimed or pruned.
func (r *PlatformAgentReconciler) syncGithubTokenMinterConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent, managedReposStr, contextReposStr string) error {
	logger := logf.FromContext(ctx)
	minterCM := &corev1.ConfigMap{}
	err := r.Get(ctx, client.ObjectKey{Name: minterConfigMapName, Namespace: agent.Namespace}, minterCM)
	if err != nil {
		if errors.IsNotFound(err) {
			return nil
		}
		return err
	}

	if minterCM.Data == nil {
		return nil
	}

	baseTemplate, ok := minterCM.Data[minterBaseTemplateKey]
	if !ok || strings.TrimSpace(baseTemplate) == "" {
		return nil
	}

	managedReposStr = strings.TrimSpace(managedReposStr)
	contextReposStr = strings.TrimSpace(contextReposStr)

	// Read operator-managed keys from annotation
	existingAnn := ""
	if minterCM.Annotations != nil {
		existingAnn = minterCM.Annotations[AnnotationManagedMinterKeys]
	}
	operatorManagedKeys := parseManagedKeysAnnotation(existingAnn)

	// If both lists are empty and no keys are tracked as operator-managed, no-op to avoid touching unmanaged keys.
	if managedReposStr == "" && contextReposStr == "" && len(operatorManagedKeys) == 0 {
		return nil
	}

	primaryOrg, skip := minterPrimaryOrg(agent)
	if skip != "" {
		logger.Info("skipping minter policy sync: " + skip)
		return nil
	}

	// Both lists are parsed before anything is computed from either: an
	// unparseable one skips the sync and leaves the ConfigMap as it is, as the
	// managed-only sync always did. Treating it as empty would prune every
	// tracked policy and break every write until the JSON was repaired.
	allBareRepos, unreadableManaged, err := minterBareRepos(logger, managedReposStr, primaryOrg, gitopsStateManagedReposKey)
	if err != nil {
		logger.Error(err, "skipping minter policy sync due to unparseable repository list in ConfigMap", "list", gitopsStateManagedReposKey)
		return nil
	}
	contextCandidates, unreadableContext, err := minterBareRepos(logger, contextReposStr, primaryOrg, gitopsStateContextReposKey)
	if err != nil {
		logger.Error(err, "skipping minter policy sync due to unparseable repository list in ConfigMap", "list", gitopsStateContextReposKey)
		return nil
	}
	unreadable := slices.Concat(unreadableManaged, unreadableContext)
	// Managed wins: a repository registered in both lists is written to, so its
	// policy is the write one, and it is left out of the read-only list too.
	var contextBareRepos []string
	for _, bareRepo := range contextCandidates {
		if !slices.Contains(allBareRepos, bareRepo) {
			contextBareRepos = append(contextBareRepos, bareRepo)
		}
	}

	// key -> the content it must hold. No capacity hint: both lengths come from
	// ConfigMap JSON, and CodeQL (go/allocation-size-overflow) flags their sum
	// as an allocation size that untrusted input could overflow.
	expected := make(map[string]string)
	writeContent := renderRepoPolicy(baseTemplate, allBareRepos)
	for _, bareRepo := range allBareRepos {
		expected[bareRepo+minterPolicyKeySuffix] = writeContent
	}
	if len(contextBareRepos) > 0 {
		readContent, ok := renderReadOnlyPolicy(baseTemplate, contextBareRepos)
		if !ok {
			logger.Info("skipping context_repos in minter policy sync; default.yaml has no read-only scope",
				"scope", minterReadScope, "repos", contextBareRepos)
		} else {
			for _, bareRepo := range contextBareRepos {
				expected[bareRepo+minterPolicyKeySuffix] = readContent
			}
		}
	}

	updated := false

	// Ensure every active repository has its policy entry. The operator claims and owns every
	// <repo>.yaml key for an active repository: if unmanaged (!managed), it adopts the key and
	// overwrites it with rendered policy derived from default.yaml. Hand-editing <repo>.yaml for an
	// active repository is unsupported; when the repository is later unregistered, the key is pruned.
	for key, expectedContent := range expected {
		currentVal, exists := minterCM.Data[key]
		_, managed := operatorManagedKeys[key]
		if !exists || !managed || currentVal != expectedContent {
			minterCM.Data[key] = expectedContent
			operatorManagedKeys[key] = struct{}{}
			updated = true
		}
	}

	// Prune policy entries ONLY for repositories that were previously managed by the operator but are no longer active.
	// An entry this release cannot read may be the repository a tracked policy is for -- a spelling
	// an earlier release accepted, hand-added to the ConfigMap -- and nothing says which. Pruning
	// then would revoke a policy that was working, so the sync only adds until every entry reads.
	if len(unreadable) > 0 {
		logger.Info("keeping every tracked minter policy: some repository entries cannot be read", "entries", unreadable)
	} else {
		for key := range operatorManagedKeys {
			if key == minterBaseTemplateKey {
				continue
			}
			if _, active := expected[key]; !active {
				delete(minterCM.Data, key)
				delete(operatorManagedKeys, key)
				updated = true
			}
		}
	}

	if updated {
		if minterCM.Annotations == nil {
			minterCM.Annotations = make(map[string]string)
		}
		minterCM.Annotations[AnnotationManagedMinterKeys] = serializeManagedKeysAnnotation(operatorManagedKeys)
		return r.Update(ctx, minterCM)
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcileCredentialProxyPolicyConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, error) {
	cm := buildCredentialProxyPolicyConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}
	if err := r.applyManaged(ctx, agent, cm); err != nil {
		return "", err
	}
	return getConfigMapHash(cm)
}

func (r *PlatformAgentReconciler) reconcileWorkload(ctx context.Context, agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsHash, policyHash string, agentPlugins []*agentv1alpha1.AgentPlugin, otlpEndpoint string, otlpDisabled bool) error {
	imageVolumeSupported := r.imageVolumeSupported(agent)
	r.updatePluginStatuses(ctx, agent, agentPlugins, imageVolumeSupported)

	opts := renderOptions{imageVolumeSupported: imageVolumeSupported, otlpEndpoint: otlpEndpoint, otlpDisabled: otlpDisabled}
	held, err := r.heldGitHubOrg(ctx, agent)
	if err != nil {
		return err
	}
	opts.heldGitHubOrg = held

	// Note: Switching between Deployment and StatefulSet causes a full delete+recreate of the workload.
	// This will incur downtime and potentially stuck pods if RWO volumes take time to unbind.
	// This is an acceptable tradeoff since switching replicas/storage requires an explicit CRD update.
	if useStatefulSet(agent) {
		dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-gateway", Namespace: agent.Namespace}}
		if err := client.IgnoreNotFound(r.Delete(ctx, dep)); err != nil {
			return fmt.Errorf("failed to cleanup legacy Deployment: %w", err)
		}

		sts := buildStatefulSet(agent, configHash, fluentBitHash, settingsHash, policyHash, agentPlugins, opts)
		if err := r.stampSecretEnvHash(ctx, agent, sts, &sts.Spec.Template); err != nil {
			return err
		}
		if err := ctrl.SetControllerReference(agent, sts, r.Scheme); err != nil {
			return err
		}
		return r.applyManaged(ctx, agent, sts)
	}

	sts := &appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-gateway", Namespace: agent.Namespace}}
	if err := client.IgnoreNotFound(r.Delete(ctx, sts)); err != nil {
		return fmt.Errorf("failed to cleanup legacy StatefulSet: %w", err)
	}

	dep := buildDeployment(agent, configHash, fluentBitHash, settingsHash, policyHash, agentPlugins, opts)
	if err := r.stampSecretEnvHash(ctx, agent, dep, &dep.Spec.Template); err != nil {
		return err
	}
	if err := ctrl.SetControllerReference(agent, dep, r.Scheme); err != nil {
		return err
	}
	return r.applyManaged(ctx, agent, dep)
}

// heldGitHubOrg returns the GITHUB_ORG the live gateway carries, read only
// while the declaration cannot name the organisation: it does not resolve, or
// validation refuses something the organisation is read from. The minter sync
// leaves its policies as they were in that state, and the pod does the same.
// Rendering the variable unset would roll the gateway and switch off the
// agent's cross-organisation guard for as long as the refusal lasts, on an
// upgrade from a release that accepted the spelling now refused.
//
// The hold lasts only as long as each pass re-reads it, so a read that fails
// is returned and the pass retried: rendering the variable unset would apply
// the loss, and every later pass would read it back. The workload kind the CR
// selects is read first, then the other one, because a storage switch renders
// the new kind while the old one still carries the value. Neither existing
// holds nothing, as on a first install.
func (r *PlatformAgentReconciler) heldGitHubOrg(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, error) {
	if agent.Spec.Integration == nil || r.Client == nil {
		return "", nil
	}
	resolved, err := agent.Spec.Integration.ResolveGit()
	if err == nil && !resolved.ScopeRefused(agentv1alpha1.GitProviderGitHub) {
		return "", nil
	}
	kinds := []client.Object{&appsv1.Deployment{}, &appsv1.StatefulSet{}}
	if useStatefulSet(agent) {
		kinds[0], kinds[1] = kinds[1], kinds[0]
	}
	key := client.ObjectKey{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}
	for _, live := range kinds {
		if err := r.Get(ctx, key, live); err != nil {
			if errors.IsNotFound(err) {
				continue
			}
			return "", fmt.Errorf("reading the live gateway's GITHUB_ORG to hold it while the git declaration is refused: %w", err)
		}
		for _, container := range podTemplateOf(live).Spec.Containers {
			if container.Name != appNamePlatformAgent {
				continue
			}
			for _, env := range container.Env {
				if env.Name == "GITHUB_ORG" && env.ValueFrom == nil {
					return env.Value, nil
				}
			}
		}
		return "", nil
	}
	return "", nil
}

// deleteLegacyCredentialIsolationResources removes the workload objects left
// behind by the two-pod layout that shipped in fb99cd1 and was collapsed back
// into a sidecar in 9b2b7e8. Nothing recreates these names, so leaving them
// running would leave a second, unreconciled copy of the agent alive.
//
// The <name>-credential-proxy Deployment and Service used to be on this list.
// They are not legacy any more — they carry the same names again, and
// reconcileCredentialProxy applies them on every pass. Leaving them here
// deleted the object the reconcile had just applied, every pass.
// credentialProxySelector reproduces the pre-#368 labels so those objects are
// adopted rather than orphaned.
//
// It also deliberately does NOT touch the <name>-sandbox-metadata-deny
// NetworkPolicy. That object is a guardrail, not a workload: it denies the
// sandbox egress to the link-local metadata server. Deleting it removed a
// control this controller no longer creates, and the rule this controller keeps
// is that it does not delete, weaken, or stop reconciling a guardrail it did
// not create. A cluster operator who applies that policy by
// hand, or a future release that renders it again, has to be able to rely on
// it surviving a reconcile. A stale NetworkPolicy fails closed; a stale
// Deployment does not, which is why the two are treated differently here.
//
// Leaving it on the list was also a live bug, not only a doctrinal one. The
// operator stopped creating the policy, so nothing in the wild owns it, and a
// hand-applied copy hit the IsControlledBy guard below and failed the whole
// reconcile with "refusing to delete unowned legacy *v1.NetworkPolicy" on
// every pass. This step runs after RBAC, the ConfigMaps, the workload, the
// Service and the NetworkPolicy, so what the failure blocked was
// updateStatusReady: the CR's status silently stopped tracking reality while
// an admin followed the documented deletion path straight onto the error path.
func (r *PlatformAgentReconciler) deleteLegacyCredentialIsolationResources(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	resources := []client.Object{
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-sandbox", Namespace: agent.Namespace}},
		&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-sandbox", Namespace: agent.Namespace}},
	}
	for _, resource := range resources {
		if err := r.Get(ctx, client.ObjectKeyFromObject(resource), resource); err != nil {
			if client.IgnoreNotFound(err) != nil {
				return err
			}
			continue
		}
		if !metav1.IsControlledBy(resource, agent) {
			return fmt.Errorf("refusing to delete unowned legacy %T %s/%s", resource, resource.GetNamespace(), resource.GetName())
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, resource)); err != nil {
			return err
		}
	}
	return nil
}

// reconcileShellSandbox creates or removes the agent's shell sandbox — the pod its
// terminal, file and code-execution tools run in when the ssh backend is on. The
// manifests and the reasoning behind them are in shell_sandbox_manifests.go.
//
// There is no off path. Every agent gets a sandbox, because every command the
// agent runs executes there — see validateShellSandbox for why the CR cannot ask
// for anything else.
//
// The credential proxy is never a container of this StatefulSet, so what the
// sandbox is handed is the broker's Service URL. credentialProxySandboxURL is
// the one place that decides, and credential_proxy_manifests.go carries the
// reasoning.
func (r *PlatformAgentReconciler) reconcileShellSandbox(ctx context.Context, agent *agentv1alpha1.PlatformAgent, settingsHash string) error {
	// Before the StatefulSet, because an install that predates agentDataStorageSize
	// has a claim the template can no longer resize.
	r.growShellSandboxDataClaim(ctx, agent)

	sts := buildShellSandboxStatefulSet(agent, shellSandboxAuthorizedKeysSecretName(agent), credentialProxySandboxURL(agent), settingsHash)
	objs := []client.Object{
		buildShellSandboxServiceAccount(agent),
		buildShellSandboxService(agent),
		sts,
		buildShellSandboxNetworkPolicy(agent, r.shellSandboxDNSClusterIPs(ctx, agent)),
	}
	for _, obj := range objs {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return fmt.Errorf("failed to set controller reference on shell sandbox %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
		apply := r.applyManaged
		if obj == sts {
			apply = r.applyShellSandboxStatefulSet
		}
		if err := apply(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply shell sandbox %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
	}
	return nil
}

// shellSandboxDNSClusterIPs is the resolved cluster DNS VIP list for the sandbox
// policy's DNS rule.
//
// Always ungated, unlike agentEgressDNSClusterIPs, which reads the profile first.
// The sandbox policy renders on every reconcile — spec.networkPolicy.enabled
// withholds the gateway policy and nothing else — so a profile that returned early
// because that flag is false would hand this rule an empty list and pin it to the
// fallback VIP, silently discarding the documented dnsClusterIPs override on the
// one policy that is still enforcing. The flag gates the gateway policy, not DNS
// resolution.
//
// The nil check below is not shared with agentEgressDNSClusterIPs: that one reaches
// its copy only on a path where spec.networkPolicy is provably set, while this runs
// on every reconcile, including the common CR that omits the block entirely.
func (r *PlatformAgentReconciler) shellSandboxDNSClusterIPs(ctx context.Context, agent *agentv1alpha1.PlatformAgent) []string {
	return r.ungatedDNSClusterIPs(ctx, agent)
}

// ungatedDNSClusterIPs runs the DNS resolution ladder with
// spec.networkPolicy.enabled lifted, for the policies that render whatever
// that flag says. Shared by the sandbox policy and the A2A session fence:
// both are policies the flag does not withhold, so both need the documented
// dnsClusterIPs override to survive it, and one copy of that rule is one
// place to correct it.
func (r *PlatformAgentReconciler) ungatedDNSClusterIPs(ctx context.Context, agent *agentv1alpha1.PlatformAgent) []string {
	if agent.Spec.NetworkPolicy == nil || agent.Spec.NetworkPolicy.Enabled == nil {
		return r.resolveNetpolProfile(ctx, agent).DNSClusterIPs
	}
	ungated := agent.DeepCopy()
	ungated.Spec.NetworkPolicy.Enabled = nil
	return r.resolveNetpolProfile(ctx, ungated).DNSClusterIPs
}

// growShellSandboxDataClaim widens the sandbox's data claim to match the agent's.
//
// A StatefulSet's volumeClaimTemplate sizes only the claims it creates, so an
// install from before agentDataStorageSize keeps the 5Gi it was given however the
// template changes — and that claim is the destination sandbox_mirror.py copies
// the agent's working directories into. Expansion is online; the volume stays
// mounted and the shell keeps running.
//
// Best-effort and logged rather than returned. A StorageClass without
// allowVolumeExpansion is how an administrator configured the cluster, and
// failing the reconcile over it would take the whole agent down to fix a volume
// that is merely smaller than we would like. The mirror already refuses to fill
// the volume it is given, so the consequence is a bounded migration, not a broken
// one.
func (r *PlatformAgentReconciler) growShellSandboxDataClaim(ctx context.Context, agent *agentv1alpha1.PlatformAgent) {
	log := logf.FromContext(ctx)
	want := resource.MustParse(agentDataStorageSize)

	name := shellSandboxDataClaimName(agent)
	pvc := &corev1.PersistentVolumeClaim{}
	if err := r.Get(ctx, client.ObjectKey{Name: name, Namespace: agent.Namespace}, pvc); err != nil {
		// Not created yet on a first install: the template sizes it correctly.
		if client.IgnoreNotFound(err) != nil {
			log.Error(err, "could not read the sandbox data claim", "claim", name)
		}
		return
	}

	have := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
	if have.Cmp(want) >= 0 {
		return
	}

	patched := pvc.DeepCopy()
	patched.Spec.Resources.Requests[corev1.ResourceStorage] = want
	if err := r.Patch(ctx, patched, client.MergeFrom(pvc)); err != nil {
		log.Error(err, "could not grow the sandbox data claim; migration into it stays bounded by the space that is there",
			"claim", name, "have", have.String(), "want", want.String())
		return
	}
	log.Info("grew the sandbox data claim to match the agent's",
		"claim", name, "from", have.String(), "to", want.String())
}

// applyShellSandboxStatefulSet applies the StatefulSet, recreating it when the
// API server refuses the update.
//
// Only replicas, ordinals, template, updateStrategy,
// persistentVolumeClaimRetentionPolicy and minReadySeconds are mutable on a
// StatefulSet. Any change to volumeClaimTemplates therefore comes back 422
// Invalid, which without this would error-loop the reconcile on every install
// that already has a sandbox — and take the rest of the agent's reconcile with
// it. Deleting with Orphan propagation leaves the pod and its claims running and
// the replacement adopts the pod by selector, so the shell stays up across the
// swap and the sandbox's disk is never at risk. awaitStatefulSetGone is what
// makes the re-apply a creation rather than another update of the object that
// is on its way out.
func (r *PlatformAgentReconciler) applyShellSandboxStatefulSet(ctx context.Context, agent *agentv1alpha1.PlatformAgent, obj client.Object) error {
	err := r.applyManaged(ctx, agent, obj)
	if !errors.IsInvalid(err) {
		return err
	}

	log := logf.FromContext(ctx)
	log.Info("the sandbox StatefulSet needs an immutable field changed; recreating it with the pod left running",
		"statefulset", obj.GetName(), "reason", err.Error())

	orphan := metav1.DeletePropagationOrphan
	existing := &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{Name: obj.GetName(), Namespace: obj.GetNamespace()},
	}
	if delErr := r.Delete(ctx, existing, &client.DeleteOptions{PropagationPolicy: &orphan}); client.IgnoreNotFound(delErr) != nil {
		return fmt.Errorf("failed to delete the sandbox StatefulSet for recreation: %w", delErr)
	}
	if err := r.awaitStatefulSetGone(ctx, client.ObjectKeyFromObject(obj)); err != nil {
		return err
	}
	return r.applyManaged(ctx, agent, obj)
}

// awaitStatefulSetGone blocks until a deleted StatefulSet has left the API
// server, or the budget above runs out.
//
// Delete returns once the object is marked, not once it is gone: orphan
// propagation puts the `orphan` finalizer on it and the garbage collector
// clears the ownerReferences off the pod and the claims before removing that
// finalizer. Applying the replacement inside that window addresses the object
// that is still terminating, so it is validated against the immutable fields
// the recreation exists to change and comes back Invalid a second time — and on
// the runs where it does not, the collector deletes what the apply just wrote
// and the agent has no sandbox until some later reconcile happens to find the
// name free.
//
// Running out of budget is not a failure of the recreation, only of doing it in
// this pass: the error requeues, the delete has already been accepted, and the
// next reconcile finds the name free and applies. Say that in the message, so
// the log line does not read as an agent stuck without a shell.
func (r *PlatformAgentReconciler) awaitStatefulSetGone(ctx context.Context, key client.ObjectKey) error {
	deadline := time.Now().Add(shellSandboxDeleteTimeout)
	for {
		err := r.Get(ctx, key, &appsv1.StatefulSet{})
		if errors.IsNotFound(err) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("failed to read the sandbox StatefulSet %s/%s while waiting for its deletion: %w", key.Namespace, key.Name, err)
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("the sandbox StatefulSet %s/%s is still terminating %s after it was deleted for recreation; retrying on the next reconcile", key.Namespace, key.Name, shellSandboxDeleteTimeout)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(shellSandboxDeletePollInterval):
		}
	}
}

// reconcileCredentialProxy creates the broker's own pod: the Deployment that runs
// it, the Service its callers reach it through, and the NetworkPolicy that
// narrows who may connect.
//
// One placement, so there is nothing to swing. The gateway's chat relay clients
// and the sandbox's wrapped CLIs both dial the Service, and neither has any
// business knowing where the relays run. credential_proxy_manifests.go carries
// the reasoning for why the pod is its own.
func (r *PlatformAgentReconciler) reconcileCredentialProxy(ctx context.Context, agent *agentv1alpha1.PlatformAgent, policyHash string) error {
	objs := []client.Object{buildCredentialProxyService(agent)}
	// The resources override is checked here as well as at admission, because
	// the chart installs with the webhook off. Rendered, an override under the
	// floor runs the broker with its budget off, and a request above its limit
	// is refused by the API server as Invalid, which
	// applyCredentialProxyDeployment reads as an immutable-field change and
	// answers by deleting the running proxy. Refused, the override is ignored
	// rather than the Deployment withheld: the Deployment is rendered at the
	// operator's default resources, so the image, the policy hash that rolls
	// the pod for a pool-mapping change, the Secret env hash and the caller and
	// egress env keep flowing, and updateStatusReady reports the refusal as
	// Degraded.
	refusal, warnings := credentialProxyResourcesRefusal(agent)
	// The warnings are logged once per spec generation: every pass that follows
	// reads the same spec and would repeat them indefinitely, with no way to
	// acknowledge one, and the webhook, where it is on, has already said each
	// once at apply. The gate is held in memory, keyed on the generation this
	// step last logged, rather than on status.observedGeneration: the
	// forbidden-mount, shell-sandbox and RuntimeClass refusals run before this
	// step and record the generation as observed, so a generation that first
	// went Degraded there would reach this step already observed and never log.
	// The residue is that an operator restart logs each CR's warnings once more.
	key := client.ObjectKeyFromObject(agent)
	if logged, ok := r.credentialProxyWarningsLogged.Load(key); !ok || logged.(int64) != agent.Generation {
		for _, warning := range warnings {
			logf.FromContext(ctx).Info("WARNING: "+warning, "name", agent.Name, "namespace", agent.Namespace)
		}
		r.credentialProxyWarningsLogged.Store(key, agent.Generation)
	}
	rendered := agent
	if refusal != "" {
		logf.FromContext(ctx).Info("refusing spec.deployment.credentialProxy.resources; the credential-proxy Deployment is rendered at the operator's default resources",
			"name", agent.Name, "namespace", agent.Namespace, "refusal", refusal)
		r.recordEvent(agent, corev1.EventTypeWarning, conditionReasonInvalidCredentialProxyResources, refusal)
		rendered = agent.DeepCopy()
		rendered.Spec.Deployment.CredentialProxy = nil
	}
	// This pod, not the gateway, is where the Slack and Teams tokens and the
	// model-provider keys are read out of a Secret as environment, so it needs
	// the same digest — see platformagent_secret_hash.go. Stamping only the
	// gateway would have left the credentials most likely to be rotated
	// reaching a container that never restarts. (On a Slack-armed next install
	// the Slack pair is read by the A2A gateway instead, which reconcileA2A
	// stamps.)
	proxy := buildCredentialProxyDeployment(rendered, policyHash)
	if err := r.stampSecretEnvHash(ctx, agent, proxy, &proxy.Spec.Template); err != nil {
		return err
	}
	objs = append(objs, proxy)
	objs = append(objs, credentialProxyNetworkPolicyWithOperatorPeer(agent, r.OperatorNamespace))
	for _, obj := range objs {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return fmt.Errorf("failed to set controller reference on credential proxy %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
		apply := r.applyManaged
		if _, isDeployment := obj.(*appsv1.Deployment); isDeployment {
			apply = r.applyCredentialProxyDeployment
		}
		if err := apply(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply credential proxy %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
	}
	return nil
}

// applyCredentialProxyDeployment applies the broker's Deployment, recreating it
// when the API server refuses the update.
//
// spec.selector is immutable on a Deployment, and this Deployment's selector
// changed. An install that ran the broker in its own pod before this PR — the
// old splitCredentialBrokerPod field — matched on `app` alone; the selector now
// also carries kubeagents.x-k8s.io/component=credential-proxy. Without this the
// apply comes back 422 Invalid on every reconcile, forever, and takes the rest
// of the agent's reconcile with it: the Service has already been applied by
// then, so its endpoints are empty, every credentialed command fails, and the CR
// still reads Ready because the status update is never reached.
//
// Foreground propagation rather than the Orphan the StatefulSet uses. Orphaning
// works there because the replacement adopts the running pod by selector; here
// the selector is the thing that changed and the new labels are not a superset,
// so nothing would ever adopt the old pod. It would sit in the namespace
// unowned, unreferenced by the Service, and still mounting the broker's
// credentials. Deleting it costs the outage that the label change makes
// unavoidable, and the outage is bounded by one pod start.
func (r *PlatformAgentReconciler) applyCredentialProxyDeployment(ctx context.Context, agent *agentv1alpha1.PlatformAgent, obj client.Object) error {
	err := r.applyManaged(ctx, agent, obj)
	if !errors.IsInvalid(err) {
		return err
	}

	log := logf.FromContext(ctx)
	log.Info("the credential broker Deployment needs an immutable field changed; recreating it",
		"deployment", obj.GetName(), "reason", err.Error())

	foreground := metav1.DeletePropagationForeground
	existing := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: obj.GetName(), Namespace: obj.GetNamespace()},
	}
	if delErr := r.Delete(ctx, existing, &client.DeleteOptions{PropagationPolicy: &foreground}); client.IgnoreNotFound(delErr) != nil {
		return fmt.Errorf("failed to delete the credential broker Deployment for recreation: %w", delErr)
	}
	if err := r.awaitCredentialProxyDeploymentGone(ctx, client.ObjectKeyFromObject(obj)); err != nil {
		return err
	}
	return r.applyManaged(ctx, agent, obj)
}

// awaitCredentialProxyDeploymentGone blocks until the deleted Deployment has left
// the API server, or the budget runs out.
//
// Same reason as awaitStatefulSetGone: Delete returns once the object is marked,
// and an apply issued inside that window addresses the object that is still
// terminating, so it is validated against the immutable field the recreation
// exists to change and comes back Invalid a second time. Running out of budget
// requeues — the delete has been accepted, and the next reconcile finds the name
// free.
func (r *PlatformAgentReconciler) awaitCredentialProxyDeploymentGone(ctx context.Context, key client.ObjectKey) error {
	deadline := time.Now().Add(credentialProxyDeleteTimeout)
	for {
		err := r.Get(ctx, key, &appsv1.Deployment{})
		if errors.IsNotFound(err) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("failed to read the credential broker Deployment %s/%s while waiting for its deletion: %w", key.Namespace, key.Name, err)
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("the credential broker Deployment %s/%s is still terminating %s after it was deleted for recreation; retrying on the next reconcile", key.Namespace, key.Name, credentialProxyDeleteTimeout)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(shellSandboxDeletePollInterval):
		}
	}
}

// reconcileCredentialBrokerTokenReviewRBAC applies, or removes, the one verb the
// broker needs to authenticate the callers it can no longer take on trust.
//
// Unconditional, because the broker is always off the agent's Pod: it stops
// treating loopback as the control and reviews every bearer token it is handed.
// This shipped once gated on a field an install could leave unset, and an
// install that left it unset got a runtime asking the API server a question it
// had no permission to ask. The TokenReview came back 403, which the
// authenticator correctly treats as a rejection rather than an allow, and every
// credentialed command in the sandbox failed with a 401 about the caller
// instead of a message about the missing rule.
func (r *PlatformAgentReconciler) reconcileCredentialBrokerTokenReviewRBAC(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	tokenReviewName := fmt.Sprintf("kubeagents:tokenreview:%s:%s", agent.Namespace, agent.Name)

	// One verb on one virtual resource, which grants no read access to anything.
	role := buildCredentialBrokerTokenReviewRole(agent)
	if err := r.applyManaged(ctx, agent, role); err != nil {
		return fmt.Errorf("failed to reconcile credential broker TokenReview ClusterRole: %w", err)
	}
	binding := buildClusterRoleBinding(agent, tokenReviewName, role.Name)
	if err := r.applyManaged(ctx, agent, binding); err != nil {
		return fmt.Errorf("failed to reconcile credential broker TokenReview ClusterRoleBinding: %w", err)
	}
	return nil
}

// deleteIfOwned removes a namespaced object this controller created, refusing
// to touch one it does not own.
func (r *PlatformAgentReconciler) deleteIfOwned(ctx context.Context, agent *agentv1alpha1.PlatformAgent, object client.Object) error {
	if err := r.Get(ctx, client.ObjectKeyFromObject(object), object); err != nil {
		return client.IgnoreNotFound(err)
	}
	if !metav1.IsControlledBy(object, agent) {
		return fmt.Errorf("refusing to delete unowned %T %s/%s", object, object.GetNamespace(), object.GetName())
	}
	return client.IgnoreNotFound(r.Delete(ctx, object))
}

// deleteIfManaged removes a cluster-scoped object this controller created.
// Cluster-scoped objects cannot carry an owner reference to a namespaced agent,
// so the managed-by label is the only evidence of provenance there is.
func (r *PlatformAgentReconciler) deleteIfManaged(ctx context.Context, object client.Object) error {
	if err := r.Get(ctx, client.ObjectKeyFromObject(object), object); err != nil {
		return client.IgnoreNotFound(err)
	}
	if object.GetLabels()[labelManagedBy] != fieldOwner {
		return fmt.Errorf("refusing to delete unmanaged %T %s", object, object.GetName())
	}
	return client.IgnoreNotFound(r.Delete(ctx, object))
}

// reasonEgressAllowlistRefused refuses the contents of an egress policy: the
// policy is fine and still gets rendered, minus the destinations that were
// refused.
const reasonEgressAllowlistRefused = "EgressAllowlistRefused"

// validateEgressPolicy returns a Degraded reason and message when
// spec.security.egressPolicy asks for something the operator cannot honestly
// render, or "" when it can.
//
// One case: an operator-supplied destination the policy refuses to render. The
// builder drops those rather than narrowing them, and a silently dropped rule
// is its own failure — an operator who added a rule to restore GitHub would get
// a Ready agent, an unreachable github.com, and nothing in kubectl describe to
// connect the two. So the refusal is surfaced here rather than left in a log
// line the operator has no reason to read.
//
// There used to be a second, and it is worth knowing why it is gone: the policy
// denies the agent Pod the link-local metadata server, a NetworkPolicy selects
// Pods rather than containers, and a broker sharing the Pod would have lost the
// metadata server with it. The broker is now always in a Pod of its own, so the
// combination the refusal named cannot be expressed.
func validateEgressPolicy(agent *agentv1alpha1.PlatformAgent) (string, string) {
	return validateEgressAllowlist(agent)
}
func validateEgressAllowlist(agent *agentv1alpha1.PlatformAgent) (string, string) {
	if !agentEgressPolicyEnabled(agent) {
		return "", ""
	}
	if refusals := egressAllowlistRefusals(agent); len(refusals) > 0 {
		return reasonEgressAllowlistRefused, "spec.security.egressAllowlist names destinations the operator " +
			"will not render, so the agent is not being reconciled rather than being given a policy that " +
			"quietly omits them: " + strings.Join(refusals, "; ") +
			". Note that an ipBlock \"except\" clause does not rescue a range containing a metadata " +
			"address — NAT rewrites the destination before the policy is evaluated " +
			"(kubernetes/kubernetes#68078). Split the range around it instead."
	}
	return "", ""
}

// reconcileAgentNetworkGuardrails keeps the agent Pod's NetworkPolicies
// maintained on a reconcile that is about to bail out over its egress spec.
//
// A refusal withholds the workload. It must not also withhold a guardrail,
// because a guardrail that stops being reconciled is a guardrail an operator
// can delete permanently — and deleting every policy that selects the agent
// Pod does not leave it restricted, it leaves NetworkPolicy permitting all
// egress. That the CR reads Degraded at the time makes it worse rather than
// better: the status names one bad CIDR while the Pod's egress is wide open.
//
// All of the policies are reconciled whatever the refusal was (steps 9b, 9c, 10,
// 11e).
// <name>-gateway-netpol is the Pod's baseline, it predates spec.security.egressPolicy,
// and no refusal is an objection to it; <name>-sandbox-metadata-deny is the refused policy
// itself, and the builder has already dropped the offending destination, so
// what is left to render is a good policy minus one rule. Under spec.mode: next
// the A2A fences join them, for the reason reconcileA2ANetworkFences states: they
// are applied from reconcileA2A, which every path here returns before reaching,
// and the session fence is the whole of what confines a session pod. litellm-policy
// rides along too, after the agent's own: it selects a different Pod, so a failure
// on its side (a transient Get on Deployment/litellm, say) must not cost the
// agent's guardrails a requeue cycle. Every step runs even when an earlier one
// fails, and the errors are joined so none of them is hidden.
func (r *PlatformAgentReconciler) reconcileAgentNetworkGuardrails(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	otlpEndpoint, otlpSource := r.resolveOTLPEndpoint(ctx, agent)
	netpolProf := r.resolveNetpolProfile(ctx, agent)
	var errs []error
	if err := r.reconcileNetworkPolicy(ctx, agent, netpolProf, otlpEndpoint, otlpSource == otlpSourceNone); err != nil {
		errs = append(errs, err)
	}
	if err := r.reconcileAgentEgressPolicy(ctx, agent, r.agentEgressDNSClusterIPs(ctx, agent, netpolProf), otlpEndpoint); err != nil {
		errs = append(errs, err)
	}
	if a2aStackRendering(agent) {
		if _, err := r.reconcileA2ANetworkFences(ctx, agent); err != nil {
			errs = append(errs, err)
		}
	}
	if err := r.reconcileLiteLLMNetworkPolicy(ctx, agent, netpolProf); err != nil {
		errs = append(errs, err)
	}
	return goerrors.Join(errs...)
}

// a2aStackRendering is the gate reconcileA2A sits behind, as a predicate rather
// than an expression at its one call site, because the refusal paths above have
// to ask the same question and two spellings of it would be two things to keep
// true. resolveMode and renderMode are both pure functions of the CR, so asking
// twice in one reconcile costs nothing and cannot disagree.
//
// modeErr is part of the gate, not noise beside it: an unrecognised spec.mode is
// the version skew described at the call site, and skew deliberately renders
// neither branch. A refusal during a freeze must not start asserting fences the
// unfrozen path would not have touched.
func a2aStackRendering(agent *agentv1alpha1.PlatformAgent) bool {
	_, modeErr := resolveMode(agent)
	return modeErr == nil && renderMode(agent, "nats") == ModeNext
}

// agentEgressDNSClusterIPs is the resolved cluster DNS VIP list for the agent
// egress policy's DNS rule.
//
// In the ordinary shape it is the profile's own answer. When
// spec.networkPolicy.enabled is false, resolveNetpolProfile returns before the
// DNS ladder runs — correct for the gateway policy, which that flag withholds,
// and exactly wrong for this one: that flag creates the only shape where the
// egress policy stands alone and enforces, so it is where a hard-coded
// fallback VIP is a total egress block on a VIP-matching dataplane and where
// the documented dnsClusterIPs override must still work. Re-run the ladder
// with the gate lifted; the flag gates the gateway policy, not DNS
// resolution.
func (r *PlatformAgentReconciler) agentEgressDNSClusterIPs(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profile netpolProfile) []string {
	if profile.Generated {
		return profile.DNSClusterIPs
	}
	if !agentEgressPolicyEnabled(agent) {
		// Nothing will render, so skip the discovery round-trip.
		return nil
	}
	ungated := agent.DeepCopy()
	ungated.Spec.NetworkPolicy.Enabled = nil
	return r.resolveNetpolProfile(ctx, ungated).DNSClusterIPs
}

// reconcileAgentEgressPolicy renders the agent Pod's default-deny egress policy.
//
// It applies the policy when spec.security.egressPolicy asks for it, and
// otherwise does nothing at all — note that "nothing at all" includes not
// deleting. An egress policy is a guardrail, and this controller does not
// remove one it did not create, which is the mistake that left
// <name>-sandbox-metadata-deny deleted on every reconcile; see
// deleteLegacyCredentialIsolationResources. A cluster operator who applies
// their own policy under this name, or who turns the field off after the
// operator rendered one, keeps a closed door rather than silently getting an
// open one.
//
// The cost is a stale policy after an opt-out: the door stays shut for anything
// the agent Pod later needs to reach. The egressPolicy CRD field description
// carries that warning, so it reaches kubectl explain.
//
// otlpEndpoint is the endpoint resolveOTLPEndpoint returned for this
// reconcile; the policy's OTel rule names the namespace it reads off it, so
// the two policies selecting the agent Pod cannot disagree about where the
// collector is (#1080). An empty endpoint keeps the managed namespace — see
// the rule's comment in buildAgentEgressNetworkPolicy for why that differs
// from the gateway policy.
func (r *PlatformAgentReconciler) reconcileAgentEgressPolicy(ctx context.Context, agent *agentv1alpha1.PlatformAgent, dnsClusterIPs []string, otlpEndpoint string) error {
	if !agentEgressPolicyEnabled(agent) {
		return nil
	}
	log := logf.FromContext(ctx)

	// validateEgressPolicy has already refused the reconcile if any of these
	// fired, so reaching the loop below means something calls this builder on a
	// path that skipped validation. Log it rather than assume: the drop is what
	// keeps the rendered object safe, and a silent drop is the failure mode
	// this guard exists for.
	policy, dropped := buildAgentEgressNetworkPolicy(agent, dnsClusterIPs, otlpCollectorNamespace(otlpEndpoint))
	for _, reason := range dropped {
		log.Info("WARNING: dropped an egressAllowlist destination that would widen the policy onto the "+
			"metadata server or the open internet. It was dropped, not narrowed: an ipBlock \"except\" "+
			"clause does not reliably block the metadata server (kubernetes/kubernetes#68078).",
			"agent", agent.Name, "namespace", agent.Namespace, "destination", reason)
	}
	if err := ctrl.SetControllerReference(agent, policy, r.Scheme); err != nil {
		return err
	}
	if err := r.applyManaged(ctx, agent, policy); err != nil {
		return fmt.Errorf("failed to reconcile agent egress NetworkPolicy: %w", err)
	}
	log.Info("agent Pod egress is default-deny with an allowlist; the metadata server is not on it. "+
		"This does nothing unless the cluster CNI enforces NetworkPolicy, which the operator cannot "+
		"detect, and it is unioned with every other policy selecting this Pod — including the "+
		"gateway policy this operator renders, which does permit the metadata server.",
		"policy", policy.Name, "rules", len(policy.Spec.Egress))
	return nil
}

func (r *PlatformAgentReconciler) reconcileService(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	svc := buildPlatformService(agent)
	if err := ctrl.SetControllerReference(agent, svc, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on Service %s/%s: %w", svc.Namespace, svc.Name, err)
	}
	if err := r.applyManaged(ctx, agent, svc); err != nil {
		return fmt.Errorf("failed to apply Service %s/%s: %w", svc.Namespace, svc.Name, err)
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcilePodDisruptionBudget(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	pdb := buildPlatformPDB(agent)
	if err := ctrl.SetControllerReference(agent, pdb, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on PodDisruptionBudget %s/%s: %w", pdb.Namespace, pdb.Name, err)
	}
	if err := r.clearForeignPDBBudgetField(ctx, pdb); err != nil {
		return err
	}
	if err := r.applyManaged(ctx, agent, pdb); err != nil {
		return fmt.Errorf("failed to apply PodDisruptionBudget %s/%s: %w", pdb.Namespace, pdb.Name, err)
	}
	return nil
}

// clearForeignPDBBudgetField removes whichever of minAvailable/maxUnavailable
// the desired budget does not use, when the live object carries it anyway.
//
// Every other object this controller reconciles recovers from hand-edits on its
// own, because a server-side apply with ForceOwnership takes back any field it
// sets. A PodDisruptionBudget does not, and the failure is permanent rather than
// cosmetic. The two budget fields are mutually exclusive, so the apply cannot
// simply overwrite the foreign one: SSA does not remove fields it never owned,
// leaving the merged object with both set, which the API server rejects with
// "minAvailable and maxUnavailable cannot be both set". That error fails the
// whole Reconcile, so every step after this one — the NetworkPolicy included —
// stops running until someone deletes the stray field by hand. An administrator
// tightening the singleton default to minAvailable is all it takes; observed
// while drain-testing this budget.
//
// Nulling the field through a merge patch deletes it from the object, and with
// it the other manager's claim in managedFields, so the apply that follows is
// unambiguous. This runs on the way to a normal apply, not just after damage:
// when the live object already agrees, the switch falls through and nothing is
// patched.
func (r *PlatformAgentReconciler) clearForeignPDBBudgetField(ctx context.Context, desired *policyv1.PodDisruptionBudget) error {
	var live policyv1.PodDisruptionBudget
	if err := r.Get(ctx, client.ObjectKeyFromObject(desired), &live); err != nil {
		if errors.IsNotFound(err) {
			return nil
		}
		return fmt.Errorf("failed to get PodDisruptionBudget %s/%s: %w", desired.Namespace, desired.Name, err)
	}

	var foreign string
	switch {
	case desired.Spec.MaxUnavailable != nil && live.Spec.MinAvailable != nil:
		foreign = "minAvailable"
	case desired.Spec.MinAvailable != nil && live.Spec.MaxUnavailable != nil:
		foreign = "maxUnavailable"
	default:
		return nil
	}

	patch := client.RawPatch(types.MergePatchType, fmt.Appendf(nil, `{"spec":{%q:null}}`, foreign))
	if err := r.Patch(ctx, &live, patch); err != nil {
		return fmt.Errorf("failed to clear %s on PodDisruptionBudget %s/%s: %w", foreign, desired.Namespace, desired.Name, err)
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcileNetworkPolicy(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profile netpolProfile, otlpEndpoint string, otlpDisabled bool) error {
	if !profile.Generated {
		var existingNetpol networkingv1.NetworkPolicy
		if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-gateway-netpol"}, &existingNetpol); err == nil {
			if metav1.IsControlledBy(&existingNetpol, agent) {
				if err := r.Delete(ctx, &existingNetpol); err != nil && !errors.IsNotFound(err) {
					return fmt.Errorf("failed to delete disabled NetworkPolicy %s/%s: %w", existingNetpol.Namespace, existingNetpol.Name, err)
				}
				logf.FromContext(ctx).Info("Deleted owner-referenced NetworkPolicy because spec.networkPolicy.enabled is false", "namespace", existingNetpol.Namespace, "name", existingNetpol.Name)
			}
		} else if !errors.IsNotFound(err) {
			return fmt.Errorf("failed to get NetworkPolicy %s/%s: %w", agent.Namespace, agent.Name+"-gateway-netpol", err)
		}

		// Read before deleting, and check ownership, exactly as the NetworkPolicy
		// above does. The name is agent-prefixed and namespaced, so a collision is
		// unlikely -- but "enabled: false" is a request to stop managing policy, not
		// a licence to delete a policy somebody else created under that name.
		//
		// The FQDN cleanup on the ENABLED path below (fqdnEnabled == false) deletes
		// the same name unguarded, and deliberately still does: an operator old
		// enough to have created that policy without an owner reference would leave
		// it behind here, and FQDN filtering the user just switched off would keep
		// applying. That risk is not worth taking on this path, where the whole
		// point is to stop managing policy at all.
		fqdnNetpol := &unstructured.Unstructured{}
		fqdnNetpol.SetGroupVersionKind(schema.GroupVersionKind{
			Group:   "networking.gke.io",
			Version: "v1alpha1",
			Kind:    "FQDNNetworkPolicy",
		})
		fqdnName := agent.Name + "-fqdn-netpol"
		if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: fqdnName}, fqdnNetpol); err == nil {
			if metav1.IsControlledBy(fqdnNetpol, agent) {
				if err := r.Delete(ctx, fqdnNetpol); err != nil && !isCRDNotInstalledError(err) {
					return fmt.Errorf("failed to clean up disabled FQDNNetworkPolicy %s/%s: %w", agent.Namespace, fqdnName, err)
				}
				logf.FromContext(ctx).Info("Deleted owner-referenced FQDNNetworkPolicy because spec.networkPolicy.enabled is false", "namespace", agent.Namespace, "name", fqdnName)
			}
		} else if !isCRDNotInstalledError(err) {
			return fmt.Errorf("failed to get FQDNNetworkPolicy %s/%s: %w", agent.Namespace, fqdnName, err)
		}
		return nil
	}

	var apiTargets []string
	if r.APIServerIP != "" {
		apiTargets = append(apiTargets, r.APIServerIP)
	}

	var k8sSvc corev1.Service
	if err := r.Get(ctx, types.NamespacedName{Namespace: "default", Name: "kubernetes"}, &k8sSvc); err == nil {
		if ip := strings.TrimSpace(k8sSvc.Spec.ClusterIP); ip != "" && ip != "None" && net.ParseIP(ip) != nil {
			apiTargets = append(apiTargets, ip)
		}
	} else if !errors.IsNotFound(err) {
		logf.FromContext(ctx).Info("Failed to discover default/kubernetes Service ClusterIP", "error", err)
	}

	// Use APIReader (live non-cached reader) for default/kubernetes Endpoints to avoid
	// starting an unconstrained cluster-wide Endpoints informer / watch cache.
	endpointsReader := client.Reader(r.Client)
	if r.APIReader != nil {
		endpointsReader = r.APIReader
	}

	var k8sEndpoints corev1.Endpoints
	if err := endpointsReader.Get(ctx, types.NamespacedName{Namespace: "default", Name: "kubernetes"}, &k8sEndpoints); err == nil {
		for _, subset := range k8sEndpoints.Subsets {
			for _, addr := range subset.Addresses {
				if addr.IP != "" {
					apiTargets = append(apiTargets, addr.IP)
				}
			}
		}
	} else if !errors.IsNotFound(err) {
		logf.FromContext(ctx).Info("Failed to discover default/kubernetes Endpoints", "error", err)
	}

	parseCIDRTarget := func(annotationName, raw string) {
		raw = strings.TrimSpace(raw)
		if raw == "" {
			return
		}
		// normalizeCIDRTarget, not a local parse: it takes the address family from
		// the address rather than the mask width, so an IPv4-mapped IPv6 block is
		// measured against the IPv4 floor it will actually print as.
		// ::ffff:a00:0/104 used to clear the /48 IPv6 floor here and land in the
		// list as 10.0.0.0/8; it is now rejected, while ::ffff:a00:0/108 still
		// passes because /108 is the IPv4 /12 that is exactly the floor.
		ipNet, ok := normalizeCIDRTarget(raw, true)
		if !ok {
			logf.FromContext(ctx).Info("Ignoring CIDR in annotation: unparseable, or broader than the /12 (IPv4) or /48 (IPv6) floor", "annotation", annotationName, "cidr", raw)
			return
		}
		apiTargets = append(apiTargets, ipNet.String())
	}

	appendCIDRs := func(sourceName, rawList string) {
		if rawList == "" {
			return
		}
		cidrs := strings.Split(rawList, ",")
		if len(cidrs) > maxCIDRsPerAnnotation {
			logf.FromContext(ctx).Info("Truncating CIDR list to max allowed CIDRs", "source", sourceName, "max", maxCIDRsPerAnnotation, "total", len(cidrs))
			cidrs = cidrs[:maxCIDRsPerAnnotation]
		}
		for _, cidr := range cidrs {
			parseCIDRTarget(sourceName, cidr)
		}
	}

	if agent.Annotations != nil {
		appendCIDRs(AnnotationAPIServerCIDR, agent.Annotations[AnnotationAPIServerCIDR])
		appendCIDRs(AnnotationCustomEgressCIDRs, agent.Annotations[AnnotationCustomEgressCIDRs])
	}
	appendCIDRs("KUBERNETES_API_SERVER_CIDR", r.APIServerCIDROverride)

	// 1. Reconcile or clean up companion FQDNNetworkPolicy (networking.gke.io/v1alpha1) on GKE Dataplane V2 clusters
	fqdnEnabled := isFQDNNetworkPolicyEnabled(agent)
	if fqdnEnabled {
		fqdnNetpol := buildFQDNNetworkPolicy(agent)
		if err := ctrl.SetControllerReference(agent, fqdnNetpol, r.Scheme); err != nil {
			return fmt.Errorf("failed to set controller reference on FQDNNetworkPolicy %s/%s: %w", fqdnNetpol.GetNamespace(), fqdnNetpol.GetName(), err)
		}
		if err := r.applyManaged(ctx, agent, fqdnNetpol); err != nil {
			if isCRDNotInstalledError(err) {
				logf.FromContext(ctx).Info("FQDNNetworkPolicy CRD (networking.gke.io/v1alpha1) not present in cluster; keeping blanket external egress rule", "error", err)
				fqdnEnabled = false
			} else {
				return fmt.Errorf("failed to apply FQDNNetworkPolicy %s/%s: %w", fqdnNetpol.GetNamespace(), fqdnNetpol.GetName(), err)
			}
		}
	} else {
		fqdnNetpol := &unstructured.Unstructured{}
		fqdnNetpol.SetGroupVersionKind(schema.GroupVersionKind{
			Group:   "networking.gke.io",
			Version: "v1alpha1",
			Kind:    "FQDNNetworkPolicy",
		})
		fqdnNetpol.SetName(agent.Name + "-fqdn-netpol")
		fqdnNetpol.SetNamespace(agent.Namespace)
		if err := r.Delete(ctx, fqdnNetpol); err != nil && !isCRDNotInstalledError(err) {
			return fmt.Errorf("failed to clean up disabled FQDNNetworkPolicy %s/%s: %w", fqdnNetpol.GetNamespace(), fqdnNetpol.GetName(), err)
		}
	}

	// 2. Build and reconcile standard NetworkPolicy (omits blanket external HTTPS egress only if replacement FQDN policy is active)
	netpol := buildNetworkPolicy(agent, apiTargets, profile, fqdnEnabled, otlpEndpoint, otlpDisabled)
	if err := ctrl.SetControllerReference(agent, netpol, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on NetworkPolicy %s/%s: %w", netpol.Namespace, netpol.Name, err)
	}
	if err := r.applyManaged(ctx, agent, netpol); err != nil {
		return fmt.Errorf("failed to apply NetworkPolicy %s/%s: %w", netpol.Namespace, netpol.Name, err)
	}

	return nil
}

// cleanupAgentRBAC dynamically purges un-wanted or all RBAC resources for a PlatformAgent.
// When deleteAll is true (called during finalization), all RBAC resources are deleted.
// When deleteAll is false (called during reconcile), active canonical bindings (minimal, local, leader) are preserved.
func (r *PlatformAgentReconciler) cleanupAgentRBAC(ctx context.Context, agent *agentv1alpha1.PlatformAgent, deleteAll bool) error {
	saName := agent.Name
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		saName = agent.Spec.Security.ServiceAccountName
	}
	minimalBindingName := fmt.Sprintf("kubeagents:minimal:%s:%s", agent.Namespace, agent.Name)
	localBindingName := fmt.Sprintf("kubeagents:local:%s:%s", agent.Namespace, agent.Name)
	leaderBindingName := fmt.Sprintf("kubeagents:leader:%s:%s", agent.Namespace, agent.Name)
	// The credential broker's TokenReview grant is applied by
	// reconcileCredentialBrokerTokenReviewRBAC on every reconcile, through applyManaged,
	// which stamps the same instance labels this cleanup selects on. Reaping
	// it here would delete what the same pass just applied — the reconcile
	// would never stabilize. Spared like the minimal binding; deleteAll
	// (the finalizer path) still removes it.
	tokenReviewName := fmt.Sprintf("kubeagents:tokenreview:%s:%s", agent.Namespace, agent.Name)

	// 1. Fast, dynamic cleanup of ClusterRoleBindings using targeted label selectors (current and legacy instance labels)
	var labeledClusterRoleBindings rbacv1.ClusterRoleBindingList
	if err := r.List(ctx, &labeledClusterRoleBindings, client.MatchingLabels{
		"kubeagents.x-k8s.io/agent-name":      agent.Name,
		"kubeagents.x-k8s.io/agent-namespace": agent.Namespace,
	}); err != nil {
		return fmt.Errorf("failed to list labeled ClusterRoleBindings: %w", err)
	}
	for i := range labeledClusterRoleBindings.Items {
		crb := &labeledClusterRoleBindings.Items[i]
		if !deleteAll && (crb.Name == minimalBindingName || crb.Name == tokenReviewName) {
			continue
		}
		if (strings.HasPrefix(crb.Name, "kubeagents:") || strings.HasPrefix(crb.Name, "kubeagents-")) && crb.DeletionTimestamp.IsZero() {
			if err := client.IgnoreNotFound(r.Delete(ctx, crb)); err != nil {
				return fmt.Errorf("failed to clean up legacy ClusterRoleBinding %s: %w", crb.Name, err)
			}
		}
	}

	instLabel := instanceLabel(agent.Namespace, agent.Name)
	var legacyLabeledCRBs rbacv1.ClusterRoleBindingList
	if err := r.List(ctx, &legacyLabeledCRBs, client.MatchingLabels{
		"app.kubernetes.io/instance": instLabel,
		"app.kubernetes.io/part-of":  "kube-agents",
	}); err != nil {
		return fmt.Errorf("failed to list legacy labeled ClusterRoleBindings: %w", err)
	}
	for i := range legacyLabeledCRBs.Items {
		crb := &legacyLabeledCRBs.Items[i]
		if !deleteAll && (crb.Name == minimalBindingName || crb.Name == tokenReviewName) {
			continue
		}
		if (strings.HasPrefix(crb.Name, "kubeagents:") || strings.HasPrefix(crb.Name, "kubeagents-")) && crb.DeletionTimestamp.IsZero() {
			if err := client.IgnoreNotFound(r.Delete(ctx, crb)); err != nil {
				return fmt.Errorf("failed to clean up legacy ClusterRoleBinding %s: %w", crb.Name, err)
			}
		}
	}

	// 2. Dynamic cleanup of ClusterRoles using label selector
	var legacyClusterRoles rbacv1.ClusterRoleList
	if err := r.List(ctx, &legacyClusterRoles, client.MatchingLabels{
		"app.kubernetes.io/instance": instLabel,
		"app.kubernetes.io/part-of":  "kube-agents",
	}); err != nil {
		return fmt.Errorf("failed to list legacy ClusterRoles: %w", err)
	}
	for i := range legacyClusterRoles.Items {
		cr := &legacyClusterRoles.Items[i]
		if !deleteAll && (cr.Name == fmt.Sprintf("kubeagents:minimal:%s:%s", agent.Namespace, agent.Name) || cr.Name == tokenReviewName) {
			continue
		}
		if (strings.HasPrefix(cr.Name, "kubeagents:") || strings.HasPrefix(cr.Name, "kubeagents-")) && cr.DeletionTimestamp.IsZero() {
			if err := client.IgnoreNotFound(r.Delete(ctx, cr)); err != nil {
				return fmt.Errorf("failed to delete legacy ClusterRole %s: %w", cr.Name, err)
			}
		}
	}

	// 4. Dynamically clean up RoleBindings in the agent's namespace (with SA swap protection)
	var existingRoleBindings rbacv1.RoleBindingList
	if err := r.List(ctx, &existingRoleBindings, client.InNamespace(agent.Namespace)); err != nil {
		return fmt.Errorf("failed to list RoleBindings in namespace %s: %w", agent.Namespace, err)
	}
	for i := range existingRoleBindings.Items {
		rb := &existingRoleBindings.Items[i]
		// Preserve local and leader bindings during reconciliation
		if !deleteAll && (rb.Name == localBindingName || rb.Name == leaderBindingName) {
			continue
		}
		isTargetSA := false
		for _, subj := range rb.Subjects {
			if subj.Kind == "ServiceAccount" &&
				(subj.Namespace == "" || subj.Namespace == agent.Namespace) &&
				(subj.Name == saName || subj.Name == agent.Name) {
				isTargetSA = true
				break
			}
		}
		if isTargetSA && (strings.HasPrefix(rb.Name, "kubeagents:") || strings.HasPrefix(rb.Name, "kubeagents-")) && rb.DeletionTimestamp.IsZero() {
			if err := client.IgnoreNotFound(r.Delete(ctx, rb)); err != nil {
				return fmt.Errorf("failed to clean up legacy RoleBinding %s: %w", rb.Name, err)
			}
		}
	}

	// 5. Clean up local and leader Role/RoleBindings if deleteAll is requested
	if deleteAll {
		rLeader := &rbacv1.Role{ObjectMeta: metav1.ObjectMeta{Name: leaderBindingName, Namespace: agent.Namespace}}
		if err := client.IgnoreNotFound(r.Delete(ctx, rLeader)); err != nil {
			return fmt.Errorf("failed to delete leader Role %s: %w", leaderBindingName, err)
		}

		rbLeader := &rbacv1.RoleBinding{ObjectMeta: metav1.ObjectMeta{Name: leaderBindingName, Namespace: agent.Namespace}}
		if err := client.IgnoreNotFound(r.Delete(ctx, rbLeader)); err != nil {
			return fmt.Errorf("failed to delete leader RoleBinding %s: %w", leaderBindingName, err)
		}

		rLocal := &rbacv1.Role{ObjectMeta: metav1.ObjectMeta{Name: localBindingName, Namespace: agent.Namespace}}
		if err := client.IgnoreNotFound(r.Delete(ctx, rLocal)); err != nil {
			return fmt.Errorf("failed to delete local Role %s: %w", localBindingName, err)
		}

		rbLocal := &rbacv1.RoleBinding{ObjectMeta: metav1.ObjectMeta{Name: localBindingName, Namespace: agent.Namespace}}
		if err := client.IgnoreNotFound(r.Delete(ctx, rbLocal)); err != nil {
			return fmt.Errorf("failed to delete local RoleBinding %s: %w", localBindingName, err)
		}
	}

	return nil
}

func (r *PlatformAgentReconciler) reconcileRBAC(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	minimalBindingName := fmt.Sprintf("kubeagents:minimal:%s:%s", agent.Namespace, agent.Name)
	localBindingName := fmt.Sprintf("kubeagents:local:%s:%s", agent.Namespace, agent.Name)
	leaderBindingName := fmt.Sprintf("kubeagents:leader:%s:%s", agent.Namespace, agent.Name)

	// Reconcile minimal read-only audit ClusterRole and ClusterRoleBinding
	minimalRole := buildMinimalPlatformRole(agent)
	if err := r.applyManaged(ctx, agent, minimalRole); err != nil {
		return fmt.Errorf("failed to reconcile minimal ClusterRole: %w", err)
	}

	crbMinimal := buildClusterRoleBinding(agent, minimalBindingName, minimalRole.Name)
	if err := r.applyManaged(ctx, agent, crbMinimal); err != nil {
		return fmt.Errorf("failed to reconcile minimal ClusterRoleBinding: %w", err)
	}

	// Reconcile namespace-scoped Role and RoleBinding for inspecting PlatformAgent CRs
	localRole := buildPlatformLocalRole(agent)
	if err := ctrl.SetControllerReference(agent, localRole, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on local Role: %w", err)
	}
	if err := r.applyManaged(ctx, agent, localRole); err != nil {
		return fmt.Errorf("failed to reconcile local Role: %w", err)
	}

	localBinding := buildRoleBinding(agent, localBindingName, localRole.Name)
	if err := ctrl.SetControllerReference(agent, localBinding, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on local RoleBinding: %w", err)
	}
	if err := r.applyManaged(ctx, agent, localBinding); err != nil {
		return fmt.Errorf("failed to reconcile local RoleBinding: %w", err)
	}

	// Reconcile leader election Role and RoleBinding
	leaderRole := buildPlatformLeaderRole(agent)
	if err := ctrl.SetControllerReference(agent, leaderRole, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on leader Role: %w", err)
	}
	if err := r.applyManaged(ctx, agent, leaderRole); err != nil {
		return fmt.Errorf("failed to reconcile leader Role: %w", err)
	}

	rbLeader := buildLeaderRoleBinding(agent, leaderBindingName, leaderRole.Name)
	if err := ctrl.SetControllerReference(agent, rbLeader, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on leader RoleBinding: %w", err)
	}
	if err := r.applyManaged(ctx, agent, rbLeader); err != nil {
		return fmt.Errorf("failed to reconcile leader RoleBinding: %w", err)
	}

	// Clean up legacy or un-canonical RBAC definitions after new roles are applied (Zero-Downtime Upgrade)
	if err := r.cleanupAgentRBAC(ctx, agent, false); err != nil {
		return err
	}

	return nil
}

// splitWorkloadStatus is one workload the gateway's readiness does not cover,
// read back so Ready can depend on it: the two the credential-broker split made
// mandatory, and on a next install the A2A gateway as well.
type splitWorkloadStatus struct {
	// name is the object's name, and what the Provisioning message reports.
	name string
	// kind is "StatefulSet" or "Deployment", so the message says which to describe.
	kind string
	// ready is the workload's ReadyReplicas; zero when the object is absent.
	ready int32
}

// readSplitWorkloads reads the shell sandbox StatefulSet and the credential broker
// Deployment, and on an install that renders the A2A stack, the A2A gateway
// Deployment.
//
// Ready has to depend on all of them. Before the split the credential runtime was a native
// sidecar of the gateway pod, so a broker that could not start held the gateway out of
// readiness and the existing pod scan reported why. Splitting it into its own pod took
// that away: the gateway now becomes Ready on its own while the model cannot run a single
// command, because the shell it runs them in does not exist. sandbox_mirror returns
// EXIT_OK when its wait times out, so nothing else in the gateway notices either.
//
// A read error other than NotFound is returned to the caller, which fails the reconcile
// rather than reporting a readiness it could not check. NotFound is not an error here: it
// is the ordinary state between applying the objects and the API server serving them back,
// and it reads as not-ready, which is what it is.
// The second result is non-empty when a next install's A2A gateway is
// withheld for want of a chat backend (a2aGatewayBackend): the remedy text
// the status writer publishes as the A2AGateway condition. A withheld gateway
// is left out of the list rather than counted as not ready, because it is
// absent on purpose and Ready would otherwise never be true on such an
// install (#1660, option 1).
func (r *PlatformAgentReconciler) readSplitWorkloads(ctx context.Context, agent *agentv1alpha1.PlatformAgent, a2a a2aProvisionState) ([]splitWorkloadStatus, string, error) {
	shell := &appsv1.StatefulSet{}
	shellName := shellSandboxName(agent)
	if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: shellName}, shell); err != nil {
		if !errors.IsNotFound(err) {
			return nil, "", fmt.Errorf("failed to get shell sandbox StatefulSet for status update: %w", err)
		}
		shell.Status.ReadyReplicas = 0
	}

	broker := &appsv1.Deployment{}
	brokerName := credentialBrokerName(agent)
	if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: brokerName}, broker); err != nil {
		if !errors.IsNotFound(err) {
			return nil, "", fmt.Errorf("failed to get credential broker Deployment for status update: %w", err)
		}
		broker.Status.ReadyReplicas = 0
	}

	workloads := []splitWorkloadStatus{
		{name: shellName, kind: "StatefulSet", ready: shell.Status.ReadyReplicas},
		{name: brokerName, kind: "Deployment", ready: broker.Status.ReadyReplicas},
	}

	// The A2A gateway stands in the same relation to Ready as those two: a next
	// install without one cannot serve an A2A request at all, and nothing about the
	// agent gateway's own readiness says so. It is also the one workload here that
	// the operator withholds ON PURPOSE -- a2aGatewayWaitsForCallout holds the first
	// creation until one auth callout replica is ready on the current spec -- and
	// until that hold is counted, the CR reports Ready: True with no dispatcher in
	// the namespace. The hold stays; it stops being silent. BusCredentialsReady is
	// not the signal for it: that condition asks for every replica, so it reads
	// False through holds the gate has already released.
	//
	// a2aStackRendering, not a2aAgentSurface: this has to be the same predicate as
	// whatever creates the Deployment. On version skew the A2A objects are frozen
	// rather than reconciled, and that CR is already Degraded for the skew itself --
	// a second reason to hold Ready there would report the freeze as a fault.
	gatewayDark := ""
	if a2aStackRendering(agent) {
		// The rest of the stack the mode renders counts too (#1701): a next
		// install whose NATS is not up, whose callout serves nothing, or
		// whose bus was never provisioned cannot serve an A2A request either,
		// and until these were counted the CR read Ready over every one of
		// those states (quota refusals, image pulls, a Job burning its
		// backoff). NATS and the callout are read here; the Job's answer
		// arrives in a2a from the pass's own read of it (below).
		nats := &appsv1.StatefulSet{}
		natsName := a2aNATSName(agent)
		if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: natsName}, nats); err != nil {
			if !errors.IsNotFound(err) {
				return nil, "", fmt.Errorf("failed to get A2A NATS StatefulSet for status update: %w", err)
			}
			nats.Status.ReadyReplicas = 0
		}
		callout := &appsv1.Deployment{}
		calloutName := a2aCalloutName(agent)
		if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: calloutName}, callout); err != nil {
			if !errors.IsNotFound(err) {
				return nil, "", fmt.Errorf("failed to get A2A auth callout Deployment for status update: %w", err)
			}
			callout.Status.ReadyReplicas = 0
		}
		// The Job counts until the bus has been provisioned once. After that a
		// Job that is absent or running again is the TTL re-run (the finished
		// Job is removed a day after completion and create-if-absent builds
		// it again; a2aProvisionJobName's comment) or a digest change, both
		// of which re-run an idempotent script against a bus that is already
		// there, and neither should flip a Ready install to Provisioning for
		// the minute it takes. "Provisioned once" is the BusProvisioned
		// condition, which only a pass that saw the Job complete writes
		// (updateStatusReady, below); this pass's own sighting counts too, so
		// the first completion is Ready on the pass that sees it. Not the
		// Ready condition: an install upgraded from an operator that never
		// counted the Job carries a Ready=True that says nothing about the
		// bus, and a latch seeded from it would never count the Job at all.
		// The Job itself is not read again here; reconcileA2A read it this
		// pass and a2a carries the answer. A Failed Job is
		// A2AProvisionFailed's, before this runs.
		jobName := a2a.jobName
		if jobName == "" {
			jobName = strings.TrimSuffix(agent.Name+a2aProvisionJobNameInfix, "-")
		}
		var provisioned int32
		if a2a.done || busProvisioned(agent) {
			provisioned = 1
		}
		workloads = append(workloads,
			splitWorkloadStatus{name: natsName, kind: "StatefulSet", ready: nats.Status.ReadyReplicas},
			splitWorkloadStatus{name: calloutName, kind: "Deployment", ready: callout.Status.ReadyReplicas},
			splitWorkloadStatus{name: jobName, kind: "Job", ready: provisioned},
		)

		gateway := &appsv1.Deployment{}
		gatewayName := a2aGatewayName(agent)
		if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: gatewayName}, gateway); err != nil {
			if !errors.IsNotFound(err) {
				return nil, "", fmt.Errorf("failed to get A2A gateway Deployment for status update: %w", err)
			}
			// Absent. Withheld on purpose when the install configures no
			// chat backend, in which case it is not a workload to wait on
			// but a condition to publish; otherwise the callout gate is
			// holding it, and it counts as not ready. The pass's own
			// decision, off the state reconcileA2A filled in: asking the
			// backend question again here would be a second Secret read on
			// every dark or held pass, and two reads can disagree inside one
			// pass when the Secret lands between them.
			if a2a.gatewayDark {
				gatewayDark = a2a.gatewayDarkReason
			} else {
				workloads = append(workloads, splitWorkloadStatus{name: gatewayName, kind: "Deployment", ready: 0})
			}
		} else {
			workloads = append(workloads, splitWorkloadStatus{
				name: gatewayName, kind: "Deployment", ready: gateway.Status.ReadyReplicas,
			})
		}
	}

	return workloads, gatewayDark, nil
}

// busProvisioned reports whether this CR has recorded the bus provisioned
// once (the BusProvisioned condition, True).
func busProvisioned(agent *agentv1alpha1.PlatformAgent) bool {
	return meta.IsStatusConditionTrue(agent.Status.Conditions, busProvisionedConditionType)
}

// The two conditions the A2A render derives, shared by both status writers
// on the hostPathDroppedConditionCurrent / setHostPathDroppedCondition
// pattern: a pass that rendered the stack keeps them true of that render
// whichever phase it ends on, and each writer gates its write on the
// *Current answer so a quiet pass stays quiet (#1392).
//
// a2aGatewayConditionCurrent reports whether the CR's A2AGateway condition
// already says dark, where "" means the condition is to be absent.
func a2aGatewayConditionCurrent(agent *agentv1alpha1.PlatformAgent, dark string) bool {
	existing := meta.FindStatusCondition(agent.Status.Conditions, a2aGatewayConditionType)
	if dark == "" {
		return existing == nil
	}
	return existing != nil && existing.Status == metav1.ConditionFalse &&
		existing.Reason == a2aGatewayDarkReason && existing.Message == dark
}

// setA2AGatewayCondition writes the withheld-gateway condition on the
// EventWatcher pattern: present while the state holds, removed the pass it
// stops holding. Not Degraded: the install configured no chat backend and
// the rest of the stack is up; the message says what would render the
// gateway.
func setA2AGatewayCondition(agent *agentv1alpha1.PlatformAgent, dark string, now metav1.Time) {
	if dark == "" {
		meta.RemoveStatusCondition(&agent.Status.Conditions, a2aGatewayConditionType)
		return
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type:               a2aGatewayConditionType,
		Status:             metav1.ConditionFalse,
		Reason:             a2aGatewayDarkReason,
		Message:            dark,
		ObservedGeneration: agent.Generation,
		LastTransitionTime: now,
	})
}

// a2aVerifierNotReady reports whether the CR should carry the A2AVerifier
// condition: the stack is rendered, the verifier Deployment exists, and no
// replica of it is ready.
//
// Existence is part of the test on purpose. An absent Deployment is the
// ordinary shape of a pass that has not rendered it yet, and reconcileA2A
// creates it every pass, so "absent" is not a state an install sits in --
// whereas reporting it would put the condition on every install for the
// first seconds of its life. What this is for is the durable case: the
// Deployment is there and its pods cannot run.
//
// The second result is whether this pass has an answer at all, and it is the
// reason "not ready" is not a plain bool. A read error leaves the condition
// alone rather than asserting either way: the API server being unreachable is
// not a fact about the verifier, it is already the reconcile's problem, and
// the next pass re-reads. Returning false for it would not be neutral --
// setA2AVerifierCondition REMOVES the condition on false -- so a single
// transient Get error on a settled install would clear a correct "the verifier
// has no ready replica" off the CR and report the stack healthy until the next
// pass put it back.
//
// An absent Deployment is a real answer (not ready is false; see above) and a
// read error is not, so NotFound is not folded in with the rest.
func (r *PlatformAgentReconciler) a2aVerifierNotReady(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (notReady, known bool) {
	if !a2aStackRendering(agent) {
		return false, true
	}
	verifier := &appsv1.Deployment{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aVerifierName(agent)}, verifier); err != nil {
		if errors.IsNotFound(err) {
			return false, true
		}
		return false, false
	}
	return verifier.Status.ReadyReplicas == 0, true
}

// a2aVerifierConditionCurrent reports whether the CR's A2AVerifier condition
// already says what this pass would write.
func a2aVerifierConditionCurrent(agent *agentv1alpha1.PlatformAgent, notReady, known bool) bool {
	// Unknown contributes no change: this pass would not touch the
	// condition, so it cannot be the reason to write status.
	if !known {
		return true
	}
	existing := meta.FindStatusCondition(agent.Status.Conditions, a2aVerifierConditionType)
	if !notReady {
		return existing == nil
	}
	return existing != nil && existing.Status == metav1.ConditionFalse &&
		existing.Reason == a2aVerifierNotReadyReason
}

// setA2AVerifierCondition writes the verifier condition on the same
// present-while-it-holds pattern as A2AGateway. Not Degraded, and not a Ready
// row: see a2aVerifierConditionType for why the CR stays Ready through this.
func setA2AVerifierCondition(agent *agentv1alpha1.PlatformAgent, notReady, known bool, now metav1.Time) {
	// No answer this pass, so whatever the CR already says stands.
	if !known {
		return
	}
	if !notReady {
		meta.RemoveStatusCondition(&agent.Status.Conditions, a2aVerifierConditionType)
		return
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type:               a2aVerifierConditionType,
		Status:             metav1.ConditionFalse,
		Reason:             a2aVerifierNotReadyReason,
		Message:            a2aVerifierNotReadyMessage,
		ObservedGeneration: agent.Generation,
		LastTransitionTime: now,
	})
}

// wantBusProvisioned is the provisioned-once record's desired presence:
// sticky under next once this pass or an earlier one saw the Job complete,
// absent under today, where the flip's teardown took the bus with it.
func wantBusProvisioned(agent *agentv1alpha1.PlatformAgent, a2a a2aProvisionState) bool {
	return a2aStackRendering(agent) && (a2a.done || busProvisioned(agent))
}

// busProvisionedConditionCurrent reports whether the CR already carries the
// record as wanted. Present is compared on status alone: the message keeps
// naming the Job that first provisioned the bus, so a re-run under a new
// digest is not a write.
func busProvisionedConditionCurrent(agent *agentv1alpha1.PlatformAgent, want bool) bool {
	existing := meta.FindStatusCondition(agent.Status.Conditions, busProvisionedConditionType)
	if !want {
		return existing == nil
	}
	return existing != nil && existing.Status == metav1.ConditionTrue
}

func setBusProvisionedCondition(agent *agentv1alpha1.PlatformAgent, want bool, jobName string, now metav1.Time) {
	switch {
	case !want:
		meta.RemoveStatusCondition(&agent.Status.Conditions, busProvisionedConditionType)
	case !busProvisioned(agent):
		meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
			Type:   busProvisionedConditionType,
			Status: metav1.ConditionTrue,
			Reason: busProvisionedReason,
			Message: fmt.Sprintf("provisioning Job %s completed; the Job's later re-runs (the 24h TTL, a digest change) do not hold Ready",
				jobName),
			ObservedGeneration: agent.Generation,
			LastTransitionTime: now,
		})
	}
}

// syncA2AConditions keeps the two render-derived conditions true of this
// pass on the paths that rendered the stack and then park Degraded
// (A2AProvisionFailed, ShellSandboxKeysMissing), where updateStatusDegraded
// is handed the CR and the refusal and nothing else. Without it a dark
// install whose Secret arrived alongside a failing Job re-run would keep a
// NoChatBackend condition with the Secret present and the gateway running,
// and an install parked on missing sandbox keys for a day would never record
// the completion its Job reached, so the TTL re-run would count the Job
// again. Its own status write, gated on both *Current answers, so a parked
// pass whose conditions already match writes nothing.
func (r *PlatformAgentReconciler) syncA2AConditions(ctx context.Context, agent *agentv1alpha1.PlatformAgent, a2a a2aProvisionState) error {
	dark := ""
	if a2a.gatewayDark {
		dark = a2a.gatewayDarkReason
	}
	want := wantBusProvisioned(agent, a2a)
	verifierNotReady, verifierKnown := r.a2aVerifierNotReady(ctx, agent)
	if a2aGatewayConditionCurrent(agent, dark) && busProvisionedConditionCurrent(agent, want) &&
		a2aVerifierConditionCurrent(agent, verifierNotReady, verifierKnown) {
		return nil
	}
	if r.liveAgentSatisfies(ctx, agent, func(live *agentv1alpha1.PlatformAgent) bool {
		wantLive := wantBusProvisioned(live, a2a)
		return a2aGatewayConditionCurrent(live, dark) && busProvisionedConditionCurrent(live, wantLive)
	}) {
		return nil
	}
	now := metav1.Now()
	setA2AGatewayCondition(agent, dark, now)
	setBusProvisionedCondition(agent, want, a2a.jobName, now)
	setA2AVerifierCondition(agent, verifierNotReady, verifierKnown, now)
	return r.Status().Update(ctx, agent)
}

// liveAgentSatisfies checks whether the uncached live object already satisfies pred.
// If it does, live status and ResourceVersion are adopted into agent (so subsequent
// deferred writers operate on the fresh version), and returns true. If the live object
// is from a different generation, adoption is refused.
func (r *PlatformAgentReconciler) liveAgentSatisfies(ctx context.Context, agent *agentv1alpha1.PlatformAgent, pred func(*agentv1alpha1.PlatformAgent) bool) bool {
	if r.APIReader == nil {
		return false
	}
	live := &agentv1alpha1.PlatformAgent{}
	if err := r.APIReader.Get(ctx, client.ObjectKeyFromObject(agent), live); err != nil {
		return false
	}
	if live.Generation == agent.Generation && pred(live) {
		agent.Status = *live.Status.DeepCopy()
		agent.ResourceVersion = live.ResourceVersion
		return true
	}
	return false
}

// updateStatusReady writes the agent's status and returns the phase it settled on, so
// the caller can decide whether the agent is still converging. otlpEndpoint, otlpSource,
// and netpolProfile are the resolved telemetry and network policy wiring; they are reported
// rather than derived because discovery is otherwise invisible to anyone reading the CR.
// a2a is what reconcileA2A found this pass (zero on a today install): whether the
// provisioning Job is complete and what it is called, so the status write neither
// re-reads the Job nor re-renders it to learn its name.
func (r *PlatformAgentReconciler) updateStatusReady(ctx context.Context, agent *agentv1alpha1.PlatformAgent, otlpEndpoint, otlpSource string, netpolProfile netpolProfile, a2a a2aProvisionState) (string, error) {
	newDeploymentStatusName := ""
	newDeploymentStatusReadyReplicas := int32(0)
	var errWorkload error

	// Whether the gateway workload may still be running Pods from a revision
	// older than the template reconcileWorkload just applied. It qualifies the
	// VolumesDropped message below, which otherwise asserts a security
	// property of Pods the apply has not reached yet. Read off the object this
	// function already fetches, so it costs no extra API call.
	//
	// Two terms, because neither is sound alone. UpdatedReplicas < Replicas is
	// the direct reading -- Replicas counts every non-terminated Pod the
	// selector matches, UpdatedReplicas only those from the current revision --
	// but both are as of Status.ObservedGeneration, so immediately after an
	// apply they describe the previous template and can read equal while every
	// Pod is old. ObservedGeneration < Generation catches exactly that window.
	//
	// It over-reports on a workload that has only just been created: no Pods
	// exist, the status is zeroes, and the generation term holds until the
	// workload controller writes back. That is the safe direction, because the
	// clause weakens the condition's claim -- a spurious one under-claims the
	// drop rather than over-claiming it -- and it clears on the next pass.
	workloadRollIncomplete := rolloutNotKnownIncomplete

	if useStatefulSet(agent) {
		sts := &appsv1.StatefulSet{}
		errWorkload = r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}, sts)
		if errWorkload != nil && !errors.IsNotFound(errWorkload) {
			return "", fmt.Errorf("failed to get StatefulSet for status update: %w", errWorkload)
		}
		if errWorkload == nil {
			newDeploymentStatusName = sts.Name
			newDeploymentStatusReadyReplicas = sts.Status.ReadyReplicas
			// The ordered roll has the same window: replicas > 1 over RWO
			// storage takes this path, and the StatefulSet controller replaces
			// Pods one at a time.
			workloadRollIncomplete = statefulSetRollIncomplete(sts)
		}
	} else {
		dep := &appsv1.Deployment{}
		errWorkload = r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}, dep)
		if errWorkload != nil && !errors.IsNotFound(errWorkload) {
			return "", fmt.Errorf("failed to get Deployment for status update: %w", errWorkload)
		}
		if errWorkload == nil {
			newDeploymentStatusName = dep.Name
			newDeploymentStatusReadyReplicas = dep.Status.ReadyReplicas
			workloadRollIncomplete = deploymentRollIncomplete(dep)
		}
	}

	// Fetch actual PVC
	pvc := &corev1.PersistentVolumeClaim{}
	errPVC := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-data"}, pvc)
	if errPVC != nil && !errors.IsNotFound(errPVC) {
		return "", fmt.Errorf("failed to get PVC for status update: %w", errPVC)
	}
	newStorageStatusBound := false
	if errPVC == nil {
		newStorageStatusBound = (pvc.Status.Phase == corev1.ClaimBound)
	}

	// Fetch actual Service
	svc := &corev1.Service{}
	errSvc := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name}, svc)
	if errSvc != nil && !errors.IsNotFound(errSvc) {
		return "", fmt.Errorf("failed to get Service for status update: %w", errSvc)
	}
	newServiceStatusEndpoint := ""
	newAddress := ""
	if errSvc == nil {
		newServiceStatusEndpoint = fmt.Sprintf("http://%s.%s.svc.cluster.local:8642", svc.Name, svc.Namespace)
		newAddress = fmt.Sprintf("%s.%s.svc.cluster.local", svc.Name, svc.Namespace)
	}

	// The workloads the gateway's own readiness does not cover. Read before the phase
	// is decided, because Ready is a claim about every one of them and not about the
	// gateway alone.
	splitWorkloads, a2aGatewayDark, errSplit := r.readSplitWorkloads(ctx, agent, a2a)
	if errSplit != nil {
		return "", errSplit
	}
	notReady := make([]string, 0, len(splitWorkloads))
	for _, w := range splitWorkloads {
		if w.ready == 0 {
			notReady = append(notReady, fmt.Sprintf("%s %s", w.kind, w.name))
		}
	}

	// Determine Phase and Condition
	newPhase := "Provisioning"
	condStatus := metav1.ConditionFalse
	condReason := "Provisioning"
	condMsg := "Waiting for deployment replicas to be ready"
	switch {
	case errWorkload == nil && newDeploymentStatusReadyReplicas > 0 && len(notReady) == 0:
		newPhase = "Ready"
		condStatus = metav1.ConditionTrue
		condReason = "Reconciled"
		condMsg = "Gateway, shell sandbox and credential broker are all ready"
		if a2aStackRendering(agent) {
			condMsg = "Gateway, shell sandbox, credential broker, NATS, auth callout, bus provisioning and A2A gateway are all ready"
			if a2aGatewayDark != "" {
				condMsg = "Gateway, shell sandbox, credential broker, NATS, auth callout and bus provisioning are all ready; " +
					"the A2A gateway is not rendered (no chat backend, see the A2AGateway condition)"
			}
		}
	case errWorkload == nil:
		// The bucket question the scan asks is "has this install ever
		// provisioned the bus", not "did this pass watch the Job finish".
		// a2a.done is only the latter: the Job carries a 24h
		// TTLSecondsAfterFinished and a spec-digested name, so it is reaped
		// daily and re-rendered on any operator upgrade that changes the
		// digest, and done is false for the whole re-run while the bucket
		// has existed the entire time. busProvisioned is the sticky record
		// of the first completion, and the disjunction is the same one
		// readSplitWorkloads and wantBusProvisioned already use.
		if phaseOverride, reasonOverride, msgOverride := r.getDeploymentStatusDetails(ctx, agent, a2a.done || busProvisioned(agent)); reasonOverride != "Provisioning" {
			newPhase = phaseOverride
			condReason = reasonOverride
			condMsg = msgOverride
		} else if newDeploymentStatusReadyReplicas > 0 && len(notReady) > 0 {
			// The gateway is up and the pod scan found no fault to name, so the
			// generic "waiting for replicas" message would point at the one
			// workload that is fine. Say which of the other two is missing.
			condMsg = fmt.Sprintf("Waiting for %s to become ready", strings.Join(notReady, " and "))
		}
	}

	gitRepoErr := error(nil)
	if agent.Spec.Integration != nil {
		gitRepoErr = agent.Spec.Integration.ValidateGit()
	}

	managedReposErr := error(nil)
	var minterHeld []string
	if gitRepoErr == nil {
		cmName := agent.Name + gitopsStateConfigMapSuffix
		cm := &corev1.ConfigMap{}
		if err := r.Get(ctx, client.ObjectKey{Name: cmName, Namespace: agent.Namespace}, cm); err == nil {
			if raw, ok := cm.Data[managedReposConfigMapKey]; ok && strings.TrimSpace(raw) != "" {
				if _, err := parseManagedRepos(raw); err != nil {
					managedReposErr = err
				}
			}
			if managedReposErr == nil {
				minterHeld = r.minterHeldEntries(ctx, agent, cm.Data[gitopsStateManagedReposKey], cm.Data[gitopsStateContextReposKey])
			}
		}
	}

	proxyResourcesRefusal, _ := credentialProxyResourcesRefusal(agent)

	degradedStatus := metav1.ConditionFalse
	degradedReason := ""
	// The Degraded message is the Ready one unless a branch says otherwise.
	degradedMsg := ""
	if gitRepoErr != nil {
		newPhase = "Degraded"
		condStatus = metav1.ConditionFalse
		// The reason string stays InvalidGitRepoURL: it is on the status of
		// running resources and in tests, and renaming it would be a second,
		// unrelated break. The message names the declaration rather than the
		// two fields only the deprecated alias has.
		condReason = conditionReasonInvalidGitRepoURL
		degradedReason = conditionReasonInvalidGitRepoURL
		// Not "GitOps disabled": with the lists, every entry validation
		// accepts is still seeded, the gitops repository included. Two kinds
		// of withheld entry are not in the problem list, so the message names
		// them: the repositories on a refused forge, and the managed ones
		// while the gitops repository is refused.
		withheld := ""
		if resolved, err := agent.Spec.Integration.ResolveGit(); err == nil {
			if n := len(resolved.OnRefusedForge()); n == 1 {
				withheld += ", nor is the repository on a refused forge"
			} else if n > 1 {
				withheld += fmt.Sprintf(", nor are the %d repositories on a refused forge", n)
			}
			if gitopsRefusalWithholdsManaged(resolved) {
				withheld += ", and no managed repository is seeded while the gitops repository is refused, since the agent reads the first managed_repos entry as its GitOps repository"
			}
		}
		condMsg = fmt.Sprintf("Invalid git integration (%s); the refused entries are not seeded%s. Admission webhook will reject updates to this resource until corrected", gitProblemList(gitRepoErr), withheld)
		degradedStatus = metav1.ConditionTrue
	} else if managedReposErr != nil {
		newPhase = "Degraded"
		condStatus = metav1.ConditionFalse
		condReason = conditionReasonCorruptManagedRepos
		degradedReason = conditionReasonCorruptManagedRepos
		condMsg = fmt.Sprintf("Corrupt %s in ConfigMap %s%s (%s); GitOps disabled", managedReposConfigMapKey, agent.Name, gitopsStateConfigMapSuffix, managedReposErr.Error())
		degradedStatus = metav1.ConditionTrue
	} else if len(minterHeld) > 0 {
		// Degraded only: the agent runs and every readable repository still
		// gets its policy, so Ready and the phase keep what the workload says.
		degradedStatus = metav1.ConditionTrue
		degradedReason = conditionReasonMinterPruningHeld
		degradedMsg = minterHeldMessage(agent.Name+gitopsStateConfigMapSuffix, minterHeld)
	} else if proxyResourcesRefusal != "" {
		// Same shape as the git refusal above: the spec is read here, on
		// every pass, rather than carried from reconcileCredentialProxy, so
		// the condition clears on the pass the override is corrected.
		// Degraded only, as the held minter entry above: the proxy runs at
		// the operator's defaults, so Ready and the phase keep what the
		// workload says. Last, because the reasons above report lost
		// function and must not be masked by it.
		degradedStatus = metav1.ConditionTrue
		degradedReason = conditionReasonInvalidCredentialProxyResources
		degradedMsg = fmt.Sprintf(invalidCredentialProxyResourcesMsgFmt, proxyResourcesRefusal)
	}
	if degradedMsg == "" {
		degradedMsg = condMsg
	}

	// Cluster event ingestion, reported only while it is switched off. A
	// permanently-present condition would have to read True on every healthy
	// install, and True here could only ever mean "the operator asked for a
	// watcher" — it is not a liveness check, and a watcher that dies leaves the
	// pod Ready with nothing to show for it. Claiming otherwise on every CR is
	// worse than saying nothing, so the condition exists only in the state that
	// is genuinely worth reporting: somebody pressed the emergency stop.
	eventWatcherOn := eventWatcherEnabled(agent)
	existingWatcherCond := meta.FindStatusCondition(agent.Status.Conditions, eventWatcherConditionType)
	// Message is compared alongside Status and Reason, as the Ready and Degraded
	// terms below do. Reason is a constant here, so the only way the text can
	// differ is a release that rewords eventWatcherDisabledMessage — and that
	// message is the recovery instruction a reader gets from `kubectl describe`.
	// Leaving it out would freeze the previous release's wording on every
	// install still holding the stop, since nothing else about them changes.
	eventWatcherUnchanged := (eventWatcherOn && existingWatcherCond == nil) ||
		(!eventWatcherOn && existingWatcherCond != nil && existingWatcherCond.Status == metav1.ConditionFalse &&
			existingWatcherCond.Reason == eventWatcherDisabledReason && existingWatcherCond.Message == eventWatcherDisabledMessage)

	// A hostPath volume the render left out of the Pod template, reported
	// while the spec still carries one and absent otherwise. Message is
	// compared for the same reason EventWatcher's is: it names the entries,
	// and an edit that swaps one hostPath for another has to change what the
	// CR says. workloadRollIncomplete rides in the same string, so the
	// comparison covers it without a term of its own -- a roll finishing is
	// one status write, not a write per pass (#1392).
	hostPathDroppedMsg := hostPathDroppedMessage(agent, workloadRollIncomplete)
	hostPathDroppedUnchanged := hostPathDroppedConditionCurrent(agent, hostPathDroppedMsg)

	// The withheld-gateway condition, same class as EventWatcher and for the
	// same reason it needs its own term: the flip from dark to rendered can
	// leave the Ready phase and message exactly as they were (another
	// workload holding Provisioning both times), and without a term here the
	// early return below would keep a NoChatBackend condition on a CR whose
	// gateway is running.
	a2aGatewayUnchanged := a2aGatewayConditionCurrent(agent, a2aGatewayDark)

	// The provisioned-once record, same shape (wantBusProvisioned).
	busProvisionedWanted := wantBusProvisioned(agent, a2a)
	busProvisionedUnchanged := busProvisionedConditionCurrent(agent, busProvisionedWanted)
	// The verifier term has to be computed here, with the others, rather than
	// read at the write below: the early return a few lines down is what
	// decides whether there is a write at all. Left out of the check, the
	// condition is only ever written on a pass that some *other* status change
	// already dirtied -- so on a settled Ready install, the one state it exists
	// to report (verifier loses its last replica, nothing else moves) never
	// reaches status, and the recovery never clears it. That is the shape the
	// A2AGateway and BusProvisioned terms above are in, and for the same
	// reason.
	verifierNotReady, verifierKnown := r.a2aVerifierNotReady(ctx, agent)
	a2aVerifierUnchanged := a2aVerifierConditionCurrent(agent, verifierNotReady, verifierKnown)

	existingCond := meta.FindStatusCondition(agent.Status.Conditions, "Ready")
	existingDegradedCond := meta.FindStatusCondition(agent.Status.Conditions, "Degraded")
	// A Degraded/RBACIncomplete condition is reportRBACSkew's, and this function
	// leaves it in place below; it must count as unchanged here too, or every
	// pass under an out-of-date ClusterRole writes status, re-enqueues itself
	// through the unfiltered PlatformAgent watch, and reconciles continuously.
	rbacDegradedPreserved := degradedStatus == metav1.ConditionFalse && existingDegradedCond != nil &&
		existingDegradedCond.Reason == reasonRBACIncomplete
	degradedUnchanged := (degradedStatus == metav1.ConditionFalse && existingDegradedCond == nil) || rbacDegradedPreserved ||
		(degradedStatus == metav1.ConditionTrue && existingDegradedCond != nil && existingDegradedCond.Status == metav1.ConditionTrue && existingDegradedCond.Reason == degradedReason && existingDegradedCond.Message == degradedMsg)

	// From the spec alone, so it is resolved here rather than passed in like the
	// telemetry and policy results, which take a discovery to produce.
	newActiveInterfaces := resolveActiveInterfaces(agent)

	// Check if anything actually changed. The generation is in the list so that
	// a spec edit which changes nothing derived here still gets one write:
	// without it the status would keep describing the previous generation and
	// a reader could not tell that the operator had seen the new one (#534).
	// The witness is the Ready condition's observedGeneration rather than the
	// top-level field, deliberately: the two are written together, but a CRD
	// that predates status.observedGeneration prunes the top-level copy on
	// every write while the condition's has always been in the schema. Keyed
	// on the pruned copy, an operator rolled ahead of its CRD would write on
	// every pass, and each write wakes the next through the unfiltered watch.
	if agent.Status.Phase == newPhase &&
		agent.Status.DeploymentStatus.Name == newDeploymentStatusName &&
		agent.Status.DeploymentStatus.ReadyReplicas == newDeploymentStatusReadyReplicas &&
		agent.Status.StorageStatus.Bound == newStorageStatusBound &&
		agent.Status.ServiceStatus.Endpoint == newServiceStatusEndpoint &&
		agent.Status.Address == newAddress &&
		agent.Status.Telemetry.OTLPEndpoint == otlpEndpoint &&
		agent.Status.Telemetry.OTLPEndpointSource == otlpSource &&
		networkPolicyStatusUnchanged(agent.Status.NetworkPolicy, netpolProfile) &&
		(r.usageStatusPruned(agent) || slices.Equal(agent.Status.Usage.ActiveInterfaces, newActiveInterfaces)) &&
		degradedUnchanged &&
		eventWatcherUnchanged &&
		hostPathDroppedUnchanged &&
		a2aGatewayUnchanged &&
		busProvisionedUnchanged &&
		a2aVerifierUnchanged &&
		existingCond != nil && existingCond.Status == condStatus && existingCond.Reason == condReason && existingCond.Message == condMsg &&
		existingCond.ObservedGeneration == agent.Generation {
		return newPhase, nil
	}

	// Apply updates
	agent.Status.Phase = newPhase
	agent.Status.ObservedGeneration = agent.Generation
	agent.Status.DeploymentStatus.Name = newDeploymentStatusName
	agent.Status.DeploymentStatus.ReadyReplicas = newDeploymentStatusReadyReplicas
	agent.Status.StorageStatus.Bound = newStorageStatusBound
	agent.Status.ServiceStatus.Endpoint = newServiceStatusEndpoint
	agent.Status.Address = newAddress
	agent.Status.Telemetry.OTLPEndpoint = otlpEndpoint
	agent.Status.Telemetry.OTLPEndpointSource = otlpSource
	agent.Status.NetworkPolicy.Generated = netpolProfile.Generated
	agent.Status.NetworkPolicy.DNSClusterIPs = append([]string(nil), netpolProfile.DNSClusterIPs...)
	agent.Status.NetworkPolicy.DNSClusterIPsSource = netpolProfile.DNSSource
	agent.Status.NetworkPolicy.MetadataDaemonIP = netpolProfile.MetadataDaemonIP
	agent.Status.NetworkPolicy.MetadataDaemonPort = netpolProfile.MetadataDaemonPort
	agent.Status.NetworkPolicy.MetadataDaemonIPSource = netpolProfile.MetadataDaemonSource
	// The one usage field this writer owns. The counters beside it are carried
	// through as read: Update sends the whole status, so leaving them alone
	// here is what keeps them intact once something does write them.
	agent.Status.Usage.ActiveInterfaces = newActiveInterfaces

	now := metav1.Now()
	agent.Status.LastReconcileTime = &now

	condition := metav1.Condition{
		Type:               "Ready",
		Status:             condStatus,
		Reason:             condReason,
		Message:            condMsg,
		ObservedGeneration: agent.Generation,
		LastTransitionTime: now,
	}
	meta.SetStatusCondition(&agent.Status.Conditions, condition)

	if degradedStatus == metav1.ConditionTrue {
		degradedCond := metav1.Condition{
			Type:               "Degraded",
			Status:             metav1.ConditionTrue,
			Reason:             degradedReason,
			Message:            degradedMsg,
			ObservedGeneration: agent.Generation,
			LastTransitionTime: now,
		}
		meta.SetStatusCondition(&agent.Status.Conditions, degradedCond)
	} else if degraded := meta.FindStatusCondition(agent.Status.Conditions, degradedConditionType); degraded != nil && degraded.Reason != reasonRBACIncomplete {
		// A Degraded/RBACIncomplete condition is reportRBACSkew's to clear, on
		// the pass where the probe comes back clean; a Ready workload does not
		// mean the ClusterRole caught up with the image.
		meta.RemoveStatusCondition(&agent.Status.Conditions, degradedConditionType)
	}

	if eventWatcherOn {
		meta.RemoveStatusCondition(&agent.Status.Conditions, eventWatcherConditionType)
	} else {
		meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
			Type:               eventWatcherConditionType,
			Status:             metav1.ConditionFalse,
			Reason:             eventWatcherDisabledReason,
			Message:            eventWatcherDisabledMessage,
			ObservedGeneration: agent.Generation,
			LastTransitionTime: now,
		})
	}

	setHostPathDroppedCondition(agent, hostPathDroppedMsg, now)

	setA2AGatewayCondition(agent, a2aGatewayDark, now)
	setBusProvisionedCondition(agent, busProvisionedWanted, a2a.jobName, now)
	setA2AVerifierCondition(agent, verifierNotReady, verifierKnown, now)

	if err := r.Status().Update(ctx, agent); err != nil {
		return newPhase, err
	}
	r.noteUsageStatusEcho(ctx, agent, newActiveInterfaces)
	return newPhase, nil
}

// usageStatusPruned reports whether the served CRD has been seen to drop
// status.usage for this CR within the last usageStatusReprobeInterval (see
// prunedUsageStatus on the reconciler). A stale record reads false, which is
// what makes the next pass probe.
func (r *PlatformAgentReconciler) usageStatusPruned(agent *agentv1alpha1.PlatformAgent) bool {
	recorded, pruned := r.prunedUsageStatus.Load(client.ObjectKeyFromObject(agent))
	if !pruned {
		return false
	}
	return time.Since(recorded.(time.Time)) < usageStatusReprobeInterval
}

// noteUsageStatusEcho reads the server's copy of the status back after the
// Ready writer's write. controller-runtime decodes the response into agent
// through a decoder that zeroes the target first (apiutil's target-zeroing
// decoder), so a status.usage the served CRD does not know comes back empty
// although a non-empty list was just written — a merging decoder would leave
// the written list in place and this check would never fire. A resolved list
// that is itself empty says nothing either way and is left alone: nil and
// empty compare equal in the gate, so it cannot loop. noteUsageEcho records
// what the echo said.
func (r *PlatformAgentReconciler) noteUsageStatusEcho(ctx context.Context, agent *agentv1alpha1.PlatformAgent, written []string) {
	if len(written) == 0 {
		return
	}
	r.noteUsageEcho(ctx, agent, len(agent.Status.Usage.ActiveInterfaces) != 0)
}

// noteUsageEcho is the one record both status.usage writers keep: echoed
// false is a write whose status.usage fields came back absent, the pruning,
// recorded with the time so that each writer skips the field until the next
// probe and logged once per record; echoed true clears it. The Ready writer
// calls it with whether activeInterfaces came back, the usage counters poller
// with whether the counters did.
func (r *PlatformAgentReconciler) noteUsageEcho(ctx context.Context, agent *agentv1alpha1.PlatformAgent, echoed bool) {
	key := client.ObjectKeyFromObject(agent)
	if echoed {
		r.prunedUsageStatus.Delete(key)
		return
	}
	// Said once per record, not once per write: a status write for any
	// other reason while the record is fresh re-records silently.
	fresh := r.usageStatusPruned(agent)
	r.prunedUsageStatus.Store(key, time.Now())
	if !fresh {
		logf.FromContext(ctx).Info("the served CRD has no status.usage; apply this release's CRD to get status.usage, which is probed again after the interval",
			"platformagent", key.String(), "reprobeAfter", usageStatusReprobeInterval.String())
	}
}

// forgetUsageStatus drops the CR's pruning record when the CR goes away, so the
// map does not keep an entry per deleted name for the life of the process.
func (r *PlatformAgentReconciler) forgetUsageStatus(agent *agentv1alpha1.PlatformAgent) {
	r.prunedUsageStatus.Delete(client.ObjectKeyFromObject(agent))
}

// hostPathDroppedConditionCurrent reports whether the VolumesDropped condition
// on the CR already says msg, where "" means the condition is to be absent.
// Both status writers gate their write on this -- the Degraded one only on the
// passes that rendered, which are the only ones it may write the condition on
// at all -- because a condition rewritten on every pass re-enqueues the CR
// through the unfiltered watch (see updateStatusDegraded for what that costs).
func hostPathDroppedConditionCurrent(agent *agentv1alpha1.PlatformAgent, msg string) bool {
	existing := meta.FindStatusCondition(agent.Status.Conditions, hostPathDroppedConditionType)
	if msg == "" {
		return existing == nil
	}
	return existing != nil && existing.Status == metav1.ConditionTrue &&
		existing.Reason == hostPathDroppedReason && existing.Message == msg
}

// setHostPathDroppedCondition writes the VolumesDropped condition, or removes
// it when msg is "" because the spec carries no hostPath any more. The caller
// does the API write.
func setHostPathDroppedCondition(agent *agentv1alpha1.PlatformAgent, msg string, now metav1.Time) {
	if msg == "" {
		meta.RemoveStatusCondition(&agent.Status.Conditions, hostPathDroppedConditionType)
		return
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type:               hostPathDroppedConditionType,
		Status:             metav1.ConditionTrue,
		Reason:             hostPathDroppedReason,
		Message:            msg,
		ObservedGeneration: agent.Generation,
		LastTransitionTime: now,
	})
}

// oldPodsPossible says whether Pods from a revision older than the template
// this pass rendered may still be running, and is the argument
// hostPathDroppedMessage takes to decide whether the message carries
// hostPathDroppedRollingClause. Named rather than a bare bool because it is
// not a property of the CR the callers are holding: it is read off the
// workload, and a caller that cannot see the workload has to say so rather
// than say no.
type oldPodsPossible bool

const (
	// rolloutIncomplete is the workload not having finished rolling the
	// applied template out, and also the workload not being readable at all
	// -- see gatewayRollIncomplete for why those share an answer.
	rolloutIncomplete oldPodsPossible = true
	// rolloutNotKnownIncomplete is the workload's own status saying the roll
	// is done, which is as far as the operator can see.
	rolloutNotKnownIncomplete oldPodsPossible = false
)

// deploymentRollIncomplete and statefulSetRollIncomplete are the two-term roll
// test, in one place because both status writers ask it. updateStatusReady
// carries the argument for the two terms; neither is sound alone.
func deploymentRollIncomplete(dep *appsv1.Deployment) oldPodsPossible {
	return oldPodsPossible(dep.Status.ObservedGeneration < dep.Generation ||
		dep.Status.UpdatedReplicas < dep.Status.Replicas)
}

func statefulSetRollIncomplete(sts *appsv1.StatefulSet) oldPodsPossible {
	return oldPodsPossible(sts.Status.ObservedGeneration < sts.Generation ||
		sts.Status.UpdatedReplicas < sts.Status.Replicas)
}

// gatewayRollIncomplete answers the same question for a caller that is not
// already holding the gateway workload, by reading it back.
//
// The read does not reach the API server. SetupWithManager Owns both the
// Deployment and the StatefulSet, so the manager's cache already watches them
// and r.Get is served from that informer's store; r.APIReader is the uncached
// reader and this is deliberately not it. That is why the Degraded path can
// afford to qualify its wording on a parked CR, which an earlier round of this
// change assumed it could not.
//
// A workload it cannot read counts as still rolling. NotFound is not sorted
// out from a real read error, because the safe answer is the same for both and
// it is the same direction the two terms already err in on a workload that has
// only just been created: the clause weakens the condition's claim, so a
// spurious one under-reports a drop that did happen rather than asserting one
// that did not.
func (r *PlatformAgentReconciler) gatewayRollIncomplete(ctx context.Context, agent *agentv1alpha1.PlatformAgent) oldPodsPossible {
	key := types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}
	if useStatefulSet(agent) {
		sts := &appsv1.StatefulSet{}
		if err := r.Get(ctx, key, sts); err != nil {
			return rolloutIncomplete
		}
		return statefulSetRollIncomplete(sts)
	}
	dep := &appsv1.Deployment{}
	if err := r.Get(ctx, key, dep); err != nil {
		return rolloutIncomplete
	}
	return deploymentRollIncomplete(dep)
}

// hostPathDroppedMessage is the VolumesDropped condition's message for the
// hostPath entries the render left out of the Pod template, or "" when the
// spec carries none and the condition is to be absent.
func hostPathDroppedMessage(agent *agentv1alpha1.PlatformAgent, oldPods oldPodsPossible) string {
	dropped := hostPathVolumes(agent)
	if len(dropped) == 0 {
		return ""
	}
	entries := make([]string, 0, len(dropped))
	for _, d := range dropped {
		entries = append(entries, fmt.Sprintf(hostPathDroppedEntryFormat, d.field, d.index, d.name, d.path))
	}
	rolling := ""
	if oldPods {
		rolling = hostPathDroppedRollingClause
	}
	return fmt.Sprintf(hostPathDroppedMessageFormat, hostPathDroppedEntryList(entries), rolling)
}

// hostPathDroppedEntryList joins as many entries as fit in
// hostPathDroppedEntryBudget and counts the rest, so that the message stays
// under the 32768 characters the CRD schema allows a condition message
// whatever the spec asks for. See hostPathDroppedEntryBudget for why an
// unbounded list is not an option: the status write that carries it is the
// whole status write.
func hostPathDroppedEntryList(entries []string) string {
	var b strings.Builder
	listed := 0
	for _, entry := range entries {
		want := len(entry)
		if listed > 0 {
			want += len(hostPathDroppedEntrySeparator)
		}
		if b.Len()+want > hostPathDroppedEntryBudget {
			break
		}
		if listed > 0 {
			b.WriteString(hostPathDroppedEntrySeparator)
		}
		b.WriteString(entry)
		listed++
	}
	if listed == 0 {
		// A single entry over the whole budget, which takes one author-chosen
		// name or path longer than the message may be. Say as much of it as
		// fits: a cut name is still something to search the spec for, and a
		// message naming nothing at all is not.
		b.WriteString(truncateToValidUTF8(entries[0], hostPathDroppedEntryBudget-len(hostPathDroppedEntryEllipsis)))
		b.WriteString(hostPathDroppedEntryEllipsis)
		listed = 1
	}
	if rest := len(entries) - listed; rest > 0 {
		fmt.Fprintf(&b, hostPathDroppedOverflowFormat, rest)
	}
	return b.String()
}

// gitProblemList lists ValidateGit's problems under the same budget as the
// dropped hostPath entries. There is one problem per refused list entry, and
// each can quote an author-chosen repository of up to 2048 characters, so the
// joined error is just as able to fail the whole status write.
func gitProblemList(err error) string {
	problems := []error{err}
	if joined, ok := err.(interface{ Unwrap() []error }); ok {
		problems = joined.Unwrap()
	}
	entries := make([]string, 0, len(problems))
	for _, p := range problems {
		entries = append(entries, p.Error())
	}
	return hostPathDroppedEntryList(entries)
}

// gitProblemFields names the fields ValidateGit refuses, within the status
// message budget, and never their values. A declaration the operator cannot
// resolve at all is named by that error, which quotes nothing.
func gitProblemFields(in *agentv1alpha1.IntegrationSpec) string {
	resolved, err := in.ResolveGit()
	if err != nil {
		return err.Error()
	}
	var fields []string
	for _, p := range resolved.Problems() {
		fields = append(fields, "integration."+p.Path.String())
	}
	return hostPathDroppedEntryList(fields)
}

// truncateToValidUTF8 cuts s to at most max bytes, dropping any rune the cut
// lands in the middle of. The API server stores strings as UTF-8, so a message
// ending in half a rune is a write that either fails or is silently rewritten.
func truncateToValidUTF8(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return strings.ToValidUTF8(s[:max], "")
}

func networkPolicyStatusUnchanged(status agentv1alpha1.NetworkPolicyStatus, profile netpolProfile) bool {
	if status.Generated != profile.Generated {
		return false
	}
	if status.DNSClusterIPsSource != profile.DNSSource {
		return false
	}
	if status.MetadataDaemonIP != profile.MetadataDaemonIP {
		return false
	}
	if status.MetadataDaemonPort != profile.MetadataDaemonPort {
		return false
	}
	if status.MetadataDaemonIPSource != profile.MetadataDaemonSource {
		return false
	}
	if len(status.DNSClusterIPs) != len(profile.DNSClusterIPs) {
		return false
	}
	for i := range status.DNSClusterIPs {
		if status.DNSClusterIPs[i] != profile.DNSClusterIPs[i] {
			return false
		}
	}
	return true
}

// capBucketProvisioned is "this install has provisioned the bus at least
// once", which is this operator's only cheap proxy for "the capability bucket
// exists". It qualifies exactly one container's crash loop; see the verifier
// paragraph below. Callers pass the sticky reading -- a2a.done for the pass
// that watched the Job finish, OR the provisioned-once record for every pass
// after it -- because the Job is reaped and re-rendered while the bucket it
// made stays put.
func (r *PlatformAgentReconciler) getDeploymentStatusDetails(ctx context.Context, agent *agentv1alpha1.PlatformAgent, capBucketProvisioned bool) (phase string, reason string, message string) {
	phase = "Provisioning"
	reason = "Provisioning"
	message = "Waiting for deployment replicas to be ready"

	// Every pod Ready is a claim about, gateway first so an install with a fault in
	// more than one of them reports the same sentence it always has. The middle two
	// are here because the faults this function names are exactly the ones the split
	// introduced a new way to hit: a runtimeClassName the cluster has no node pool
	// for, and a sandbox or broker image tag nothing published. Neither is visible
	// from the gateway's own pod any more.
	selectors := []map[string]string{
		{"app": agent.Name + "-gateway"},
		shellSandboxSelector(agent),
		{"app": credentialProxyName(agent)},
	}

	// Appended last, for that same reason, one release later: readSplitWorkloads
	// made the A2A gateway gate Ready, and a workload that gates Ready and is never
	// scanned leaves an operator with nothing to act on. Unscanned, a gateway pod in
	// ImagePullBackOff or CrashLoopBackOff reads as "Waiting for Deployment
	// <agent>-a2a-gateway to become ready" indefinitely -- which is also what the
	// deliberate callout hold says, and what a slow scheduler says, so the phase
	// distinguishes none of the three. Scanned, the container fault names itself.
	//
	// Last rather than first: the ordering above is load-bearing, and an install
	// faulting in more than one workload has to keep reporting the sentence it
	// always did.
	//
	// a2aStackRendering, the same predicate readSplitWorkloads gates on and the same
	// one that renders the Deployment: a today install has no such pod, and a skewed
	// one has its A2A objects frozen and is already Degraded/ModeNotRecognized for
	// the skew itself -- a second reason there would report the freeze as a fault.
	if a2aStackRendering(agent) {
		selectors = append(selectors, map[string]string{"app": a2aGatewayName(agent)})
	}

	// The verifier, for the opposite reason to the gateway's and so worth its own
	// paragraph: it is scanned precisely because it does NOT gate Ready.
	//
	// readSplitWorkloads leaves it out on purpose -- a verifier rollout should not
	// flip a serving install to Provisioning, and the CRD page says so. But every
	// executor turns a Check that gets no answer into a terminal rejected, so a
	// verifier stuck in ImagePullBackOff refuses every submission while the CR
	// reports Ready=True. Not gating is the decision; reporting nothing is not.
	// Scanned, the operator at least reads the container fault instead of a green
	// CR and a bus that rejects everything.
	//
	// The reference is a2aReleaseImage's: A2A_VERIFIER_IMAGE if set, else derived
	// from the operator's tag, else from the agent image (b1ee482e graduated it off
	// the private dev-registry default it had when this paragraph was first
	// written). All three rungs can name something unpullable -- an override typo,
	// a release whose verifier image was never pushed, an unreachable registry --
	// so the fault stays worth scanning for; it is just no longer the default.
	//
	// This is the one selector here whose pod is not counted toward Ready, so it
	// cannot by itself move a CR off Ready, and the ordering note above still
	// holds. It CAN move the phase, though, which is the thing to keep in mind
	// when reading the paragraph above as a safety argument: the reason it adds
	// turns a Provisioning into a Degraded an operator will act on. That is
	// wanted for an unoverridden image and wrong for the bucket wait, and the
	// pod loop below draws the line.
	if a2aStackRendering(agent) {
		selectors = append(selectors, map[string]string{"app": a2aVerifierName(agent)})
	}

	pods := make([]corev1.Pod, 0)
	for _, selector := range selectors {
		podList := &corev1.PodList{}
		if err := r.List(ctx, podList, client.InNamespace(agent.Namespace), client.MatchingLabels(selector)); err != nil {
			continue
		}
		pods = append(pods, podList.Items...)
	}
	if len(pods) == 0 {
		return phase, reason, message
	}

	for _, pod := range pods {
		if !pod.DeletionTimestamp.IsZero() {
			continue
		}

		// 1. Check container waiting states (CrashLoopBackOff, ImagePullBackOff, ErrImagePull, etc.)
		//
		// Init statuses first, and PodInitializing filtered out with
		// ContainerCreating. Both were measured on a cluster rather than reasoned
		// about, because the obvious theory is wrong: while a pod is stuck in Init
		// the kubelet does populate ContainerStatuses -- every app container sits
		// there waiting with reason PodInitializing. So the old code did find a
		// waiting container and did report Degraded. What it reported was
		// "PodInitializing", which names no fault and points at a container that is
		// only waiting its turn, while the init container that actually failed to
		// pull went unmentioned. The credential proxy is a native sidecar now, so
		// the container that strands the pod is usually in the init list.
		//
		// Scanning init first and skipping the two placeholder reasons gets the
		// reason an operator can act on: ImagePullBackOff on the container that has
		// it, rather than PodInitializing on one that does not.
		initThenApp := make([]corev1.ContainerStatus, 0, len(pod.Status.InitContainerStatuses)+len(pod.Status.ContainerStatuses))
		initThenApp = append(initThenApp, pod.Status.InitContainerStatuses...)
		initThenApp = append(initThenApp, pod.Status.ContainerStatuses...)
		// A verifier crash loop in the window before the provision Job completes
		// is the a2a stack still coming up, not a fault, and it is the one crash
		// loop here the scan has to not report.
		//
		// The two dependencies the verifier can outrun no longer restart it at
		// all: a bus that is not answering yet and a bucket the Job has not
		// created yet are both waited out in-process (a2a/cmd/verifier's
		// bindStore). What can still end the process in this same window is
		// authentication. reconcileA2A applies the callout one step before the
		// verifier, and applying it is not the same as it serving; a bus that
		// refuses the same identity twice running aborts nats.go's reconnect
		// loop for good (processAuthError), which the verifier answers by
		// exiting so the pod restarts. That is right of the verifier and
		// transient on a fresh install. Meanwhile `notReady` holds "bus
		// provisioning" until the Job completes, which keeps updateStatusReady
		// in the `case errWorkload == nil:` arm -- the arm that calls this scan.
		// So without this, a fresh `next` install can spend that window
		// reporting Degraded/CrashLoopBackOff for a stack that is merely not up
		// yet. Before the verifier joined the selectors that window read
		// Provisioning, and it has to go on reading Provisioning.
		//
		// Narrow on purpose, three ways: this container, this reason, and only
		// while the bucket is unprovisioned. ImagePullBackOff on the verifier is
		// a real fault at any time -- it is the fault this selector was added
		// for, and unreachable at any of a2aReleaseImage's three rungs -- and a
		// crash loop that outlives the install's first provisioning is the
		// verifier failing at something other than coming up -- which is why
		// capBucketProvisioned has to be the sticky reading and not this
		// pass's Job status, or a TTL re-run would file a real fault here.
		//
		// Suppressing rather than skipping the pod: a verifier pod can carry a
		// genuine fault on another container in the same window, and the loop
		// below still has to find it.
		crashIsTheBucketWait := !capBucketProvisioned && pod.Labels["app"] == a2aVerifierName(agent)

		for _, cs := range initThenApp {
			if crashIsTheBucketWait && cs.Name == a2aVerifierContainerName &&
				cs.State.Waiting != nil && cs.State.Waiting.Reason == reasonCrashLoopBackOff {
				continue
			}
			if cs.State.Waiting != nil && cs.State.Waiting.Reason != "" &&
				cs.State.Waiting.Reason != reasonContainerCreating && cs.State.Waiting.Reason != reasonPodInitializing {
				phase = "Degraded"
				reason = cs.State.Waiting.Reason
				message = fmt.Sprintf("Container '%s' in pod %s is waiting: %s - %s", cs.Name, pod.Name, cs.State.Waiting.Reason, cs.State.Waiting.Message)
				if isPluginStagingContainer(cs.Name) {
					if cs.LastTerminationState.Terminated != nil {
						term := cs.LastTerminationState.Terminated
						if isMissingShellFailure(term.ExitCode, term.Message) {
							message = fmt.Sprintf("Container '%s' in pod %s is waiting: %s - staging failed (exit code %d): plugin image may be outdated or missing /bin/sh (init container staging requires a minimal shell such as busybox:musl or alpine)", cs.Name, pod.Name, cs.State.Waiting.Reason, term.ExitCode)
						}
					} else if isMissingShellFailure(0, cs.State.Waiting.Message) {
						message = fmt.Sprintf("Container '%s' in pod %s is waiting: %s - staging failed: plugin image may be outdated or missing /bin/sh (init container staging requires a minimal shell such as busybox:musl or alpine)", cs.Name, pod.Name, cs.State.Waiting.Reason)
					}
				}
				return phase, reason, message
			}
			if isPluginStagingContainer(cs.Name) && cs.State.Terminated != nil && cs.State.Terminated.ExitCode != 0 {
				phase = "Degraded"
				reason = cs.State.Terminated.Reason
				if reason == "" {
					reason = reasonContainerError
				}
				message = fmt.Sprintf("Container '%s' in pod %s terminated with exit code %d: %s", cs.Name, pod.Name, cs.State.Terminated.ExitCode, cs.State.Terminated.Message)
				if isMissingShellFailure(cs.State.Terminated.ExitCode, cs.State.Terminated.Message) {
					message = fmt.Sprintf("Container '%s' in pod %s failed to stage plugin (exit code %d): plugin image may be outdated or missing /bin/sh (init container staging requires a minimal shell such as busybox:musl or alpine)", cs.Name, pod.Name, cs.State.Terminated.ExitCode)
				}
				return phase, reason, message
			}
		}

		// 2. Check pod scheduling conditions (Unschedulable due to node selector/affinity/gVisor)
		for _, cond := range pod.Status.Conditions {
			if cond.Type == corev1.PodScheduled && cond.Status == corev1.ConditionFalse && cond.Reason == "Unschedulable" {
				phase = "Degraded"
				reason = "PodUnschedulable"
				if requested := requestedRuntimeClasses(agent); len(requested) > 0 {
					// Plural only when the CR really does name two, which takes
					// the agent pod and the sandbox having deliberately been
					// given different runtimes. Every other install reads the
					// sentence this condition has always produced.
					noun := "RuntimeClass"
					if len(requested) > 1 {
						noun = "RuntimeClasses"
					}
					quoted := make([]string, 0, len(requested))
					for _, name := range requested {
						quoted = append(quoted, fmt.Sprintf("'%s'", name))
					}
					message = fmt.Sprintf("Pod %s is waiting to be scheduled because no nodes in the cluster match the requested %s %s. For GKE Standard, enable GKE Sandbox by provisioning a gVisor node pool.", pod.Name, noun, strings.Join(quoted, ", "))
				} else {
					cleanMsg := strings.TrimSuffix(strings.TrimSpace(cond.Message), ".")
					message = fmt.Sprintf("Pod %s cannot be scheduled onto any available node: %s.", pod.Name, cleanMsg)
				}
				return phase, reason, message
			}
		}
	}

	return phase, reason, message
}

// checkShellSandboxKeys returns a Degraded reason and message when the Secret the
// sandbox mounts its authorized_keys from does not exist, or "" when it does.
//
// A read error other than NotFound returns "" as well. This runs after every object
// is applied and its only job is to phrase a status; an API blip must not turn a
// healthy agent Degraded on a claim this function could not check.
//
// The read goes through r.APIReader. Secrets are the one type the manager's cache does not
// already hold, and a cached Get of an unwatched type starts a cluster-wide informer and
// blocks in WaitForCacheSync until it syncs. On the RBAC this operator ships that LIST is
// forbidden, so it never syncs: the call does not fail, it hangs, and with one reconcile
// worker that is the whole controller stopped on an install that reports success. Reading
// live also keeps every Secret in the cluster out of the operator's memory.
func (r *PlatformAgentReconciler) checkShellSandboxKeys(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, string) {
	reader := r.APIReader
	if reader == nil {
		reader = r.Client
	}
	if reader == nil {
		return "", ""
	}
	name := shellSandboxAuthorizedKeysSecretName(agent)
	err := reader.Get(ctx, types.NamespacedName{Name: name, Namespace: agent.Namespace}, &corev1.Secret{})
	if err == nil || !errors.IsNotFound(err) {
		return "", ""
	}
	return reasonShellSandboxKeysMissing, shellSandboxKeysMissingMessage(name)
}

// validateRuntimeClass returns the name it could not resolve alongside the
// error, because the caller's Degraded message names it and the error alone
// does not carry it back.
func (r *PlatformAgentReconciler) validateRuntimeClass(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, error) {
	for _, rcName := range requestedRuntimeClasses(agent) {
		rc := &nodev1.RuntimeClass{}
		if err := r.Get(ctx, types.NamespacedName{Name: rcName}, rc); err != nil {
			return rcName, err
		}
	}
	return "", nil
}

// requestedRuntimeClasses is every RuntimeClass this CR asks for, deduplicated.
//
// Two pods can name one now — the agent's, and the sandbox's, which is a
// separate field because the two workloads do not want the same runtime. Both
// are checked here rather than each at its own builder because the failure is
// the same failure and the operator already has one message for it: a
// RuntimeClass that does not exist leaves the pod Pending with nothing in the CR
// that explains why, and that is worth catching before either object is applied.
func requestedRuntimeClasses(agent *agentv1alpha1.PlatformAgent) []string {
	var names []string
	add := func(name *string) {
		if name == nil || *name == "" {
			return
		}
		if slices.Contains(names, *name) {
			return
		}
		names = append(names, *name)
	}
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.Availability != nil {
		add(agent.Spec.Deployment.Availability.RuntimeClassName)
	}
	add(shellSandboxRuntimeClassName(agent))
	return names
}

// workloadRenderState says whether the reconcile pass parking the agent on
// Degraded reached the render, and is the argument updateStatusDegraded takes
// to decide whether it may touch the VolumesDropped condition. Named rather
// than a bare bool because the answer is not obvious from the call site: the
// refusals read top to bottom and only their position in Reconcile says which
// side of reconcileWorkload they are on.
type workloadRenderState bool

const (
	// workloadRendered is the three refusals below the render:
	// ModeNotRecognized, A2AProvisionFailed, ShellSandboxKeysMissing.
	workloadRendered workloadRenderState = true
	// workloadNotRendered is the four above it, which return with the workload
	// untouched: ForbiddenVolumeMount, ShellSandboxCannotBeDisabled,
	// RuntimeClassNotFound, EgressAllowlistRefused.
	workloadNotRendered workloadRenderState = false
)

// degradedStatusCurrent reports whether the given PlatformAgent's status matches
// what updateStatusDegraded would write. The comparison is keyed on phase, the Ready
// condition's status, reason, message, and observedGeneration, plus the rendered-gated
// VolumesDropped condition.
func degradedStatusCurrent(agent *agentv1alpha1.PlatformAgent, reason, message string, rendered workloadRenderState, hostPathDroppedMsg string) bool {
	existing := meta.FindStatusCondition(agent.Status.Conditions, "Ready")
	if existing == nil ||
		agent.Status.Phase != "Degraded" ||
		existing.Status != metav1.ConditionFalse ||
		existing.Reason != reason ||
		existing.Message != message ||
		existing.ObservedGeneration != agent.Generation {
		return false
	}
	if !rendered {
		return true
	}
	return hostPathDroppedConditionCurrent(agent, hostPathDroppedMsg)
}

// updateStatusDegraded parks the agent on a refusal: phase Degraded, and a
// Ready=False condition carrying the reason and message. It writes only when
// something it is about to write differs from what the status already holds.
//
// The gate matters because the PlatformAgent watch has no predicate, so every
// status write re-enqueues the object at once. Without it a CR held on any
// refusal wrote status on every pass: each requeue tick wrote, the write woke
// an echo pass through the watch, and the echo wrote again. The chain stopped
// there only because metav1.Time serializes to the second — the echo's write
// was byte-identical to the one before it, and the API server drops such an
// update without an etcd write, a resourceVersion bump or a watch event.
// Measured, on envtest and on a live install, that was two reconciles and two
// status-write requests (so two API-server audit entries) per 30s tick, with
// resourceVersion and lastReconcileTime moving every tick, for as long as the
// refusal stood (#1392). The comparison is keyed on the phase, the condition's
// status, reason, message and observedGeneration — everything this function
// writes except the timestamps. LastReconcileTime is deliberately not in the
// key: it is stamped with `now`, so including it would make every pass a
// change; and it is not refreshed on a quiet pass, which is what
// updateStatusReady does and what the field's own doc says — it is the time of
// the last status write, not of the last pass.
// The generation witness is the condition's observedGeneration rather than the
// top-level field, for the reason updateStatusReady gives: a CRD that predates
// status.observedGeneration prunes the top-level copy on every write.
//
// rendered says whether the caller got as far as reconcileWorkload, and gates
// the VolumesDropped condition alone -- everything else here is written either
// way. See workloadRenderState.
func (r *PlatformAgentReconciler) updateStatusDegraded(ctx context.Context, agent *agentv1alpha1.PlatformAgent, reason, message string, rendered workloadRenderState) error {
	// VolumesDropped rides along on a pass that rendered, because the strip it
	// reports happens at render and three of the refusals that land here
	// render first (see the condition's own comment). It is in the comparison
	// as well as the write: without that, a CR parked on one of those refusals
	// with an unchanged Ready would leave the condition unwritten forever.
	//
	// A pre-render refusal neither writes it nor clears it. Not writes,
	// because the Pod still running is the previous pass's and the strip may
	// never have reached it; not clears, because a condition already on the CR
	// was written by a pass that did render, and that render is still what the
	// running Pod is -- so it is left exactly as it stands, stale wording and
	// all, until a pass renders again and refreshes or removes it. That errs
	// towards over-reporting a drop that has happened, never towards claiming
	// one that has not. It drops out of the comparison on those passes for the
	// same reason: a term no write can satisfy would make every requeue tick a
	// status write (#1392).
	hostPathDroppedMsg := ""
	if rendered {
		// Qualified the same way updateStatusReady qualifies it. That function
		// reads the roll off the gateway workload it fetches anyway; this one
		// is handed the CR and the refusal and nothing else, so it reads the
		// workload back through gatewayRollIncomplete, which is a cache hit
		// and not an API request. A CR parked on one of these three refusals
		// has exactly the same window as one reading Ready -- the render
		// applied a template and the apply returns before the Pods carrying
		// the hostPath are gone -- so saying it more strongly here would be
		// the one wording that can claim a security property the cluster does
		// not have.
		//
		// Only asked when there is something to report. On the CRs that never
		// carried a hostPath, which is nearly all of them, a parked pass does
		// no workload read at all.
		oldPods := rolloutNotKnownIncomplete
		if len(hostPathVolumes(agent)) > 0 {
			oldPods = r.gatewayRollIncomplete(ctx, agent)
		}
		hostPathDroppedMsg = hostPathDroppedMessage(agent, oldPods)
	}
	if degradedStatusCurrent(agent, reason, message, rendered, hostPathDroppedMsg) {
		return nil
	}

	// If the cached agent missed the status condition due to watch event lag,
	// check the live object before attempting an Update that would conflict (409).
	if r.liveAgentSatisfies(ctx, agent, func(live *agentv1alpha1.PlatformAgent) bool {
		return degradedStatusCurrent(live, reason, message, rendered, hostPathDroppedMsg)
	}) {
		return nil
	}

	agent.Status.Phase = "Degraded"
	agent.Status.ObservedGeneration = agent.Generation
	now := metav1.Now()
	agent.Status.LastReconcileTime = &now

	condition := metav1.Condition{
		Type:               "Ready",
		Status:             metav1.ConditionFalse,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: agent.Generation,
		LastTransitionTime: now,
	}
	meta.SetStatusCondition(&agent.Status.Conditions, condition)
	if rendered {
		setHostPathDroppedCondition(agent, hostPathDroppedMsg, now)
	}
	return r.Status().Update(ctx, agent)
}

// conditionReasonIntegrationWarning is the Event reason for a declaration that
// is valid but does nothing.
const conditionReasonIntegrationWarning = "IntegrationWarning"

// recordIntegrationWarnings writes each of the integration's Warnings() as a
// Warning Event on the PlatformAgent. Admission returns the same warnings, but
// the chart ships the webhook off, and on such an install a credentialed forge
// the broker is not given would otherwise leave no trace: the status is Ready,
// because nothing is invalid. Each warning names a field path and a provider,
// never a repository value, so nothing a clone URL carries reaches an Event.
// The API server aggregates a repeated Event, so writing it on every
// reconcile raises its count rather than adding one per pass.
func (r *PlatformAgentReconciler) recordIntegrationWarnings(agent *agentv1alpha1.PlatformAgent) {
	if agent.Spec.Integration == nil {
		return
	}
	resolved, err := agent.Spec.Integration.ResolveGit()
	if err != nil {
		return
	}
	for _, warning := range resolved.Warnings() {
		r.recordEvent(agent, corev1.EventTypeWarning, conditionReasonIntegrationWarning, warning)
	}
}

// recordEvent writes an Event on obj through the manager's recorder, and
// nothing when there is none: tests and the golden harness build the
// reconciler without one, and no pass depends on an Event having been
// written.
func (r *PlatformAgentReconciler) recordEvent(obj runtime.Object, eventType, reason, message string) {
	if r.Recorder == nil {
		return
	}
	r.Recorder.Event(obj, eventType, reason, message)
}

// SetupWithManager sets up the controller with the Manager.
func (r *PlatformAgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if r.DiscoveryClient == nil && mgr != nil && mgr.GetConfig() != nil {
		dc, err := discovery.NewDiscoveryClientForConfig(mgr.GetConfig())
		if err != nil {
			// Not fatal — the operator still reconciles agents without plugins. But the
			// ImageVolume probe fails closed, so without this client every AgentPlugin
			// goes Degraded; say why rather than leaving it to be inferred.
			logf.Log.WithName("platformagent-controller").Error(err,
				"Failed to build discovery client; ImageVolume support cannot be detected and "+
					"AgentPlugins will be reported as Degraded unless the "+
					"kubeagents.x-k8s.io/enable-image-volumes annotation is set")
		} else {
			r.DiscoveryClient = dc
		}
	}

	if r.APIReader == nil && mgr != nil {
		r.APIReader = mgr.GetAPIReader()
	}

	bld := ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.PlatformAgent{}).
		Owns(&appsv1.Deployment{}).
		Owns(&appsv1.StatefulSet{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.Service{}).
		Owns(&networkingv1.NetworkPolicy{}).
		Owns(&policyv1.PodDisruptionBudget{})

	enqueueAgentsInNamespace := func(ctx context.Context, namespace string) []reconcile.Request {
		var list agentv1alpha1.PlatformAgentList
		if err := mgr.GetClient().List(ctx, &list, client.InNamespace(namespace)); err != nil {
			return nil
		}
		var reqs []reconcile.Request
		for _, agent := range list.Items {
			reqs = append(reqs, reconcile.Request{
				NamespacedName: types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name},
			})
		}
		return reqs
	}

	// Only register AgentPlugin watch if CRD exists in cluster RESTMapper
	gvk := agentv1alpha1.GroupVersion.WithKind("AgentPlugin")
	if mgr != nil && mgr.GetRESTMapper() != nil {
		if _, err := mgr.GetRESTMapper().RESTMapping(gvk.GroupKind(), gvk.Version); err == nil {
			bld = bld.Watches(
				&agentv1alpha1.AgentPlugin{},
				handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
					ext, ok := obj.(*agentv1alpha1.AgentPlugin)
					if !ok {
						return nil
					}
					if ext.Spec.AgentRef != "" {
						return []reconcile.Request{
							{NamespacedName: types.NamespacedName{Namespace: ext.Namespace, Name: ext.Spec.AgentRef}},
						}
					}
					return enqueueAgentsInNamespace(ctx, ext.Namespace)
				}),
				// Status writes on AgentPlugin come from this controller. Without a
				// generation filter each of those writes would re-enqueue the agent that
				// produced it.
				builder.WithPredicates(predicate.GenerationChangedPredicate{}),
			)
		} else {
			logf.Log.WithName("platformagent-controller").Info(
				"AgentPlugin CRD is not installed on cluster; skipping AgentPlugin watch. " +
					"Restart the operator after installing the CRD to enable plugin reconciliation.")
		}
	}

	return bld.
		Watches(
			&rbacv1.ClusterRoleBinding{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				parts := strings.Split(obj.GetName(), ":") // format: kubeagents:<role>:<namespace>:<name>
				if len(parts) == 4 && parts[0] == "kubeagents" {
					return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: parts[2], Name: parts[3]}}}
				}
				return nil
			}),
		).
		Watches(
			&rbacv1.ClusterRole{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				parts := strings.Split(obj.GetName(), ":") // format: kubeagents:<role>:<namespace>:<name>
				if len(parts) == 4 && parts[0] == "kubeagents" {
					return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: parts[2], Name: parts[3]}}}
				}
				return nil
			}),
		).
		Watches(
			&rbacv1.RoleBinding{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				parts := strings.Split(obj.GetName(), ":") // format: kubeagents:<role>:<namespace>:<name>
				if len(parts) == 4 && parts[0] == "kubeagents" {
					return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: parts[2], Name: parts[3]}}}
				}
				return nil
			}),
		).
		Watches(
			&rbacv1.Role{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				parts := strings.Split(obj.GetName(), ":") // format: kubeagents:<role>:<namespace>:<name>
				if len(parts) == 4 && parts[0] == "kubeagents" {
					return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: parts[2], Name: parts[3]}}}
				}
				return nil
			}),
		).
		Watches(
			&networkingv1.NetworkPolicy{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				if obj.GetName() != litellmNetworkPolicyName {
					return nil
				}
				return enqueueAgentsInNamespace(ctx, obj.GetNamespace())
			}),
			builder.WithPredicates(predicate.ResourceVersionChangedPredicate{}),
		).
		Watches(
			&appsv1.Deployment{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				if obj.GetName() != litellmDeploymentName {
					return nil
				}
				return enqueueAgentsInNamespace(ctx, obj.GetNamespace())
			}),
			builder.WithPredicates(predicate.GenerationChangedPredicate{}),
		).
		Named("platformagent").
		Complete(r)
}

func isCRDNotInstalledError(err error) bool {
	if err == nil {
		return false
	}
	if meta.IsNoMatchError(err) || errors.IsNotFound(err) {
		return true
	}
	msg := err.Error()
	return strings.Contains(msg, "no matches for kind") ||
		strings.Contains(msg, "could not find the requested resource") ||
		strings.Contains(msg, "failed to get restmapping")
}

func (r *PlatformAgentReconciler) resolveAgentPlugins(ctx context.Context, agent *agentv1alpha1.PlatformAgent) ([]*agentv1alpha1.AgentPlugin, error) {
	var extList agentv1alpha1.AgentPluginList
	if err := r.List(ctx, &extList, client.InNamespace(agent.Namespace)); err != nil {
		if isCRDNotInstalledError(err) {
			logf.Log.WithName("platformagent-controller").Info("AgentPlugin CRD is not installed on cluster; skipping plugin resolution", "namespace", agent.Namespace)
			return nil, nil
		}
		return nil, err
	}

	var matching []*agentv1alpha1.AgentPlugin
	for i := range extList.Items {
		ext := &extList.Items[i]
		if ext.Spec.AgentRef == agent.Name {
			matching = append(matching, ext)
		}
	}

	slices.SortFunc(matching, func(a, b *agentv1alpha1.AgentPlugin) int {
		return strings.Compare(a.Name, b.Name)
	})

	return matching, nil
}

// isImageVolumeSupported reports whether OCI image volumes may be attached for the
// given agent.
//
// The check fails closed: if the cluster capability cannot be established, image
// volumes are treated as unsupported. Mounting an unsupported ImageVolume makes the
// API server reject the entire Deployment, which would take the agent down rather
// than merely leaving its plugins unloaded — so an unknown answer must mean "no".
// The enable-image-volumes annotation is an explicit operator override and wins over
// discovery in both directions, which is how 1.33/1.34 clusters that have the feature
// gate turned on manually opt back in.
func isImageVolumeSupported(dc discovery.DiscoveryInterface, agent *agentv1alpha1.PlatformAgent) bool {
	if agent != nil && agent.Annotations != nil {
		if val, ok := agent.Annotations["kubeagents.x-k8s.io/enable-image-volumes"]; ok {
			return strings.EqualFold(strings.TrimSpace(val), "true")
		}
	}
	supported, _ := clusterImageVolumeSupport(dc)
	return supported
}

// isGKEAutopilot probes the API server for GKE Autopilot specific API resources.
// It returns:
//   - isAutopilot: true if allowlistedworkloads is found under the auto.gke.io API group.
//   - determined: true if the determination is authoritative. Returns false if transient
//     discovery errors (network failures, 503, timeouts) prevented establishing cluster type.
func isGKEAutopilot(dc discovery.DiscoveryInterface) (isAutopilot bool, determined bool) {
	if dc == nil {
		return false, false
	}
	defer func() {
		if r := recover(); r != nil {
			isAutopilot = false
			determined = false
		}
	}()

	groups, err := dc.ServerGroups()
	if err != nil || groups == nil {
		return false, false
	}

	var autoGroup *metav1.APIGroup
	for i := range groups.Groups {
		if groups.Groups[i].Name == gkeAutopilotAPIGroup {
			autoGroup = &groups.Groups[i]
			break
		}
	}
	if autoGroup == nil {
		// The API server responded with its API groups and auto.gke.io is absent:
		// authoritatively not an Autopilot cluster.
		return false, true
	}

	// Collect versions to probe, checking PreferredVersion first if available.
	versionsToCheck := make([]string, 0, len(autoGroup.Versions)+1)
	if autoGroup.PreferredVersion.GroupVersion != "" {
		versionsToCheck = append(versionsToCheck, autoGroup.PreferredVersion.GroupVersion)
	}
	for _, gv := range autoGroup.Versions {
		if gv.GroupVersion != "" && !slices.Contains(versionsToCheck, gv.GroupVersion) {
			versionsToCheck = append(versionsToCheck, gv.GroupVersion)
		}
	}
	if len(versionsToCheck) == 0 {
		versionsToCheck = append(versionsToCheck, gkeAutopilotDefaultGroupVersion)
	}

	hasTransientError := false
	for _, gv := range versionsToCheck {
		resList, err := dc.ServerResourcesForGroupVersion(gv)
		if err != nil {
			if !errors.IsNotFound(err) {
				hasTransientError = true
			}
			continue
		}
		if resList == nil {
			continue
		}
		for _, r := range resList.APIResources {
			if r.Name == gkeAutopilotAllowlistedWorkloadsResource {
				return true, true
			}
		}
	}

	if hasTransientError {
		return false, false
	}
	return false, true
}

// clusterImageVolumeSupport probes the API server for ImageVolume support.
//
// determined reports whether the answer is authoritative. When the capability cannot be
// established — no discovery client, an unreachable API server, an unparseable version,
// or a transient discovery failure probing Autopilot resources — supported is false and
// determined is false: the caller must fail closed for this pass but must not remember
// the answer, because the next probe may succeed.
func clusterImageVolumeSupport(dc discovery.DiscoveryInterface) (supported bool, determined bool) {
	log := logf.Log.WithName("platformagent-controller")
	const override = "Set the kubeagents.x-k8s.io/enable-image-volumes annotation to override."

	if dc == nil {
		log.Info("No discovery client available to verify ImageVolume support; assuming unsupported. " + override)
		return false, false
	}
	ver, err := dc.ServerVersion()
	if err != nil {
		log.Error(err, "Failed to query server version to verify ImageVolume support; assuming unsupported. "+override)
		return false, false
	}

	major, errMajor := strconv.Atoi(strings.TrimRight(ver.Major, "+"))
	minorStr := strings.Split(strings.TrimRight(ver.Minor, "+"), ".")[0]
	minor, errMinor := strconv.Atoi(minorStr)
	if errMajor != nil || errMinor != nil {
		log.Info("Could not parse server version to verify ImageVolume support; assuming unsupported. "+override,
			"major", ver.Major, "minor", ver.Minor)
		return false, false
	}

	// Kubernetes < 1.35 does not support native ImageVolumeSource on any cluster type.
	if major < 1 || (major == 1 && minor < 35) {
		return false, true
	}

	// GKE Autopilot clusters enforce GKE Warden admission policies (autopilot-volume-type-limitation)
	// that reject the Image volume type. On Autopilot, fall back to initContainer/emptyDir staging.
	// On GKE Standard (and non-GKE clusters), ImageVolumeSource is supported natively on Kubernetes 1.35+.
	autopilot, determined := isGKEAutopilot(dc)
	if !determined {
		log.Info("Could not determine whether cluster is GKE Autopilot due to discovery failure; assuming unsupported. " + override)
		return false, false
	}
	if autopilot {
		log.Info("GKE Autopilot cluster detected; using initContainer plugin staging fallback. " + override)
		return false, true
	}

	return true, true
}

// imageVolumeSupported resolves the cluster ImageVolume capability and reuses it for
// subsequent reconciles. Only an authoritative answer is cached: a transient discovery
// failure must not pin every plugin to Degraded until the operator restarts. Per-agent
// annotation overrides are evaluated on every call, since those change without a restart.
func (r *PlatformAgentReconciler) imageVolumeSupported(agent *agentv1alpha1.PlatformAgent) bool {
	if agent != nil && agent.Annotations != nil {
		if val, ok := agent.Annotations["kubeagents.x-k8s.io/enable-image-volumes"]; ok {
			return strings.EqualFold(strings.TrimSpace(val), "true")
		}
	}

	r.imageVolumeMu.Lock()
	defer r.imageVolumeMu.Unlock()
	if r.imageVolumeResolved {
		return r.clusterImageVolumes
	}
	supported, determined := clusterImageVolumeSupport(r.DiscoveryClient)
	if determined {
		r.clusterImageVolumes = supported
		r.imageVolumeResolved = true
	}
	return supported
}

type pluginFailure struct {
	reason  string
	message string
}

// evaluatePluginReadiness decides a plugin's Ready condition. failure is the
// detected failure (image pull or staging container exit), nil otherwise.
func evaluatePluginReadiness(
	agent *agentv1alpha1.PlatformAgent,
	plugin *agentv1alpha1.AgentPlugin,
	imageVolumeSupported bool,
	duplicate bool,
	failure *pluginFailure,
) (phase string, condition metav1.Condition) {
	degraded := func(reason, message string) (string, metav1.Condition) {
		return "Degraded", metav1.Condition{
			Type: "Ready", Status: metav1.ConditionFalse, Reason: reason, Message: message,
		}
	}

	switch {
	case !isValidPluginName(plugin.Name):
		return degraded("InvalidPluginName", fmt.Sprintf(
			"Plugin name '%s' must start with a lowercase letter and contain only lowercase letters and digits (max 56 characters).",
			plugin.Name))
	case duplicate:
		return degraded("DuplicatePluginName", fmt.Sprintf(
			"Plugin name '%s' collides with built-in or already registered plugin.", plugin.Name))
	case failure != nil:
		if failure.reason == pluginFailureReasonImagePull {
			// The image volume is part of the agent's pod spec, so an unpullable plugin
			// image keeps the whole agent pod from starting. Reporting Ready here would
			// point whoever is debugging the outage away from its actual cause.
			return degraded(pluginFailureReasonImagePull, fmt.Sprintf(
				"Plugin image '%s' could not be pulled, which is blocking agent %s from starting: %s",
				plugin.Spec.Image, agent.Name, failure.message))
		}
		return degraded(failure.reason, fmt.Sprintf(
			"Plugin staging failed for agent %s: %s", agent.Name, failure.message))
	}

	message := fmt.Sprintf("Plugin successfully applied to agent %s.", agent.Name)
	if !imageVolumeSupported {
		message = fmt.Sprintf("Plugin successfully staged via init container for agent %s (ImageVolumeSource unsupported).", agent.Name)
	}
	if issues := pluginConfigIssues(plugin); len(issues) > 0 {
		message = fmt.Sprintf("%s %s", message, strings.Join(issues, " "))
	}
	return "Ready", metav1.Condition{
		Type: "Ready", Status: metav1.ConditionTrue, Reason: "Applied", Message: message,
	}
}

func (r *PlatformAgentReconciler) updatePluginStatuses(ctx context.Context, agent *agentv1alpha1.PlatformAgent, plugins []*agentv1alpha1.AgentPlugin, imageVolumeSupported bool) {
	now := metav1.Now()
	seenNames := make(map[string]bool)
	pluginFailures := r.detectPluginFailures(ctx, agent, plugins)

	for _, plugin := range plugins {
		original := plugin.DeepCopy()
		patch := client.MergeFrom(original)
		if !slices.Contains(plugin.Status.TargetAgents, agent.Name) {
			plugin.Status.TargetAgents = append(plugin.Status.TargetAgents, agent.Name)
		}
		plugin.Status.ObservedGeneration = plugin.Generation

		normName := normalizePluginName(plugin.Name)
		duplicate := IsBuiltInPlugin(plugin.Name) || seenNames[normName]
		seenNames[normName] = true

		var failure *pluginFailure
		if f, exists := pluginFailures[plugin.Name]; exists {
			failure = &f
		}
		phase, condition := evaluatePluginReadiness(agent, plugin, imageVolumeSupported, duplicate, failure)
		condition.LastTransitionTime = now
		plugin.Status.Phase = phase
		meta.SetStatusCondition(&plugin.Status.Conditions, condition)

		// Only write when something other than the timestamp actually moved. Stamping
		// LastUpdated on every pass would make each reconcile issue a PATCH, and each
		// PATCH re-enqueue the agent through the AgentPlugin watch. Logging is gated on
		// the same check: a standing misconfiguration is already reported in status, so
		// repeating it every reconcile is noise, not signal.
		if pluginStatusEqual(&original.Status, &plugin.Status) {
			continue
		}
		plugin.Status.LastUpdated = &now
		logPluginCondition(plugin, condition)

		if err := r.Status().Patch(ctx, plugin, patch); err != nil {
			logf.Log.WithName("platformagent-controller").Error(err, "Failed to update AgentPlugin status", "plugin", plugin.Name)
		}
	}
}

// logPluginCondition emits one log line per genuine status transition.
func logPluginCondition(plugin *agentv1alpha1.AgentPlugin, condition metav1.Condition) {
	log := logf.Log.WithName("platformagent-controller")
	if condition.Status == metav1.ConditionTrue {
		// Surfaced as an error so operators notice keys silently dropped from config.yaml.
		for _, issue := range pluginConfigIssues(plugin) {
			log.Error(fmt.Errorf("%s", issue), "ignoring plugin config key outside allowed subtrees",
				"plugin", plugin.Name)
		}
		log.Info("AgentPlugin ready", "plugin", plugin.Name, "message", condition.Message)
		return
	}
	log.Error(fmt.Errorf("%s", condition.Message), "AgentPlugin degraded",
		"plugin", plugin.Name, "reason", condition.Reason)
}

// detectPluginFailures maps plugin name to its detected failure when the agent pod
// cannot pull the plugin image or when staging the plugin via init container fails.
func (r *PlatformAgentReconciler) detectPluginFailures(ctx context.Context, agent *agentv1alpha1.PlatformAgent, plugins []*agentv1alpha1.AgentPlugin) map[string]pluginFailure {
	failures := map[string]pluginFailure{}
	if len(plugins) == 0 {
		return failures
	}

	podList := &corev1.PodList{}
	if err := r.List(ctx, podList, client.InNamespace(agent.Namespace),
		client.MatchingLabels{"app": agent.Name + "-gateway"}); err != nil {
		return failures
	}

	for _, pod := range podList.Items {
		if !pod.DeletionTimestamp.IsZero() {
			continue
		}
		// 1. Check init container statuses for staging failures or image pull issues
		for _, cs := range pod.Status.InitContainerStatuses {
			for _, plugin := range plugins {
				if cs.Name != buildPluginStagingContainerName(plugin.Name) {
					continue
				}
				if w := cs.State.Waiting; w != nil {
					if w.Reason == "ImagePullBackOff" || w.Reason == "ErrImagePull" {
						failures[plugin.Name] = pluginFailure{
							reason:  pluginFailureReasonImagePull,
							message: w.Message,
						}
					} else if w.Reason != reasonContainerCreating && w.Reason != reasonPodInitializing {
						msg := w.Message
						if cs.LastTerminationState.Terminated != nil && cs.LastTerminationState.Terminated.ExitCode != 0 {
							term := cs.LastTerminationState.Terminated
							msg = formatStagingFailureMessage(term.ExitCode, term.Message, plugin.Spec.Image)
						} else if isMissingShellFailure(0, w.Message) {
							msg = fmt.Sprintf("staging init container failed (%s): plugin image '%s' may be outdated or missing /bin/sh (init container staging requires a minimal shell such as busybox:musl or alpine)", w.Reason, plugin.Spec.Image)
						} else if msg == "" {
							msg = fmt.Sprintf("staging init container failed: %s", w.Reason)
						}
						failures[plugin.Name] = pluginFailure{
							reason:  pluginFailureReasonStaging,
							message: msg,
						}
					}
				} else if t := cs.State.Terminated; t != nil && t.ExitCode != 0 {
					failures[plugin.Name] = pluginFailure{
						reason:  pluginFailureReasonStaging,
						message: formatStagingFailureMessage(t.ExitCode, t.Message, plugin.Spec.Image),
					}
				}
			}
		}

		// 2. Check main container waiting on image volumes (ImageVolumeSource)
		for _, cs := range pod.Status.ContainerStatuses {
			w := cs.State.Waiting
			if w == nil || (w.Reason != "ImagePullBackOff" && w.Reason != "ErrImagePull") {
				continue
			}
			for _, plugin := range plugins {
				if imageReferencedIn(w.Message, plugin.Spec.Image) {
					failures[plugin.Name] = pluginFailure{
						reason:  pluginFailureReasonImagePull,
						message: w.Message,
					}
				}
			}
		}
	}
	return failures
}

func isPluginStagingContainer(name string) bool {
	return strings.HasPrefix(name, pluginStagingContainerPrefix)
}

func isMissingShellFailure(exitCode int32, msg string) bool {
	if exitCode == exitCodeCommandNotFound {
		return true
	}
	for _, marker := range missingShellMessageMarkers {
		if strings.Contains(msg, marker) {
			return true
		}
	}
	return false
}

// formatStagingFailureMessage constructs an informative error message when a staging init container fails.
// If the container exited with code 127 or the failure indicates a missing shell, it clarifies that
// the plugin image may be outdated or missing /bin/sh (required on clusters using init container staging).
func formatStagingFailureMessage(exitCode int32, termMsg string, pluginImage string) string {
	baseMsg := fmt.Sprintf("staging init container exited with code %d", exitCode)
	if termMsg != "" {
		baseMsg = fmt.Sprintf("%s (%s)", baseMsg, termMsg)
	}
	if isMissingShellFailure(exitCode, termMsg) {
		return fmt.Sprintf("%s: plugin image '%s' may be outdated or missing /bin/sh (init container staging requires a minimal shell such as busybox:musl or alpine)", baseMsg, pluginImage)
	}
	return baseMsg
}

// detectPluginImageFailures maps plugin name to the kubelet's message when the agent's
// pod cannot pull that plugin's image.
func (r *PlatformAgentReconciler) detectPluginImageFailures(ctx context.Context, agent *agentv1alpha1.PlatformAgent, plugins []*agentv1alpha1.AgentPlugin) map[string]string {
	all := r.detectPluginFailures(ctx, agent, plugins)
	images := map[string]string{}
	for name, f := range all {
		if f.reason == pluginFailureReasonImagePull {
			images[name] = f.message
		}
	}
	return images
}

// isImageRefChar reports whether b could be part of an image reference, and so whether a
// match ending or starting next to it is really a match of some longer reference.
func isImageRefChar(b byte) bool {
	switch {
	case b >= 'a' && b <= 'z', b >= 'A' && b <= 'Z', b >= '0' && b <= '9':
		return true
	case b == '.' || b == '-' || b == '_' || b == '/' || b == ':' || b == '@':
		return true
	}
	return false
}

// imageReferencedIn reports whether message names exactly this image.
//
// A plain substring test is wrong here: "repo/x:v1" occurs inside "repo/x:v10", so a
// failure on one tag would be blamed on a sibling plugin using another. Requiring a
// non-reference character on both sides — kubelet quotes the reference — keeps the match
// to whole references without depending on one exact message format.
func imageReferencedIn(message, image string) bool {
	if image == "" {
		return false
	}
	for offset := 0; offset <= len(message)-len(image); {
		idx := strings.Index(message[offset:], image)
		if idx < 0 {
			return false
		}
		start := offset + idx
		end := start + len(image)
		startOK := start == 0 || !isImageRefChar(message[start-1])
		endOK := end == len(message) || !isImageRefChar(message[end])
		if startOK && endOK {
			return true
		}
		offset = start + 1
	}
	return false
}

// markOrphanedPlugins reports plugins whose agentRef names a PlatformAgent that does not
// exist. Nothing else reconciles them — the resolver only ever sees plugins that match an
// existing agent — so without this a typo in agentRef leaves the plugin permanently
// statusless, with no indication that it will never be applied.
func (r *PlatformAgentReconciler) markOrphanedPlugins(ctx context.Context, namespace, agentName string) {
	var list agentv1alpha1.AgentPluginList
	if err := r.List(ctx, &list, client.InNamespace(namespace)); err != nil {
		if !isCRDNotInstalledError(err) {
			logf.Log.WithName("platformagent-controller").Error(err,
				"Failed to list AgentPlugins while checking for orphans", "namespace", namespace)
		}
		return
	}

	now := metav1.Now()
	for i := range list.Items {
		plugin := &list.Items[i]
		if plugin.Spec.AgentRef != agentName {
			continue
		}

		original := plugin.DeepCopy()
		patch := client.MergeFrom(original)
		plugin.Status.ObservedGeneration = plugin.Generation
		plugin.Status.TargetAgents = nil
		plugin.Status.Phase = "Degraded"
		condition := metav1.Condition{
			Type:               "Ready",
			Status:             metav1.ConditionFalse,
			Reason:             "AgentNotFound",
			Message:            fmt.Sprintf("No PlatformAgent named '%s' exists in namespace '%s'; this plugin is not applied to any agent.", agentName, namespace),
			LastTransitionTime: now,
		}
		meta.SetStatusCondition(&plugin.Status.Conditions, condition)

		if pluginStatusEqual(&original.Status, &plugin.Status) {
			continue
		}
		plugin.Status.LastUpdated = &now
		logPluginCondition(plugin, condition)

		if err := r.Status().Patch(ctx, plugin, patch); err != nil {
			logf.Log.WithName("platformagent-controller").Error(err,
				"Failed to update orphaned AgentPlugin status", "plugin", plugin.Name)
		}
	}
}

// pluginStatusEqual compares two AgentPlugin statuses while ignoring LastUpdated and
// condition timestamps, so that a re-reconcile that reaches the same conclusion is not
// mistaken for a change.
func pluginStatusEqual(a, b *agentv1alpha1.AgentPluginStatus) bool {
	if a.Phase != b.Phase ||
		a.ObservedGeneration != b.ObservedGeneration ||
		!slices.Equal(a.TargetAgents, b.TargetAgents) ||
		len(a.Conditions) != len(b.Conditions) {
		return false
	}
	for i := range a.Conditions {
		ac, bc := a.Conditions[i], b.Conditions[i]
		if ac.Type != bc.Type || ac.Status != bc.Status ||
			ac.Reason != bc.Reason || ac.Message != bc.Message ||
			ac.ObservedGeneration != bc.ObservedGeneration {
			return false
		}
	}
	return true
}
