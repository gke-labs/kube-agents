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
	"fmt"
	"net"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	nodev1 "k8s.io/api/node/v1"
	policyv1 "k8s.io/api/policy/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/discovery"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	platformAgentFinalizer = "kubeagents.x-k8s.io/finalizer"
	minIPv4CIDRPrefix      = 12
	minIPv6CIDRPrefix      = 48
	maxCIDRsPerAnnotation  = 50

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

	AnnotationAPIServerCIDR           = "kubeagents.x-k8s.io/apiserver-cidr"
	AnnotationCustomEgressCIDRs       = "kubeagents.x-k8s.io/custom-egress-cidrs"
	AnnotationEnableFQDNNetworkPolicy = "kubeagents.x-k8s.io/enable-fqdn-network-policy"
	AnnotationManagedMinterKeys       = "kubeagents.x-k8s.io/managed-minter-keys"

	// GKE Autopilot API groups used to detect Autopilot clusters where Warden restricts Image volumes.
	gkeAutopilotAPIGroup = "auto.gke.io"
	gkeWardenAPIGroup    = "warden.gke.io"

	pluginFailureReasonImagePull = "ImagePullFailed"
	pluginFailureReasonStaging   = "StagingFailed"

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
)

// PlatformAgentReconciler reconciles a PlatformAgent object
type PlatformAgentReconciler struct {
	client.Client
	Scheme          *runtime.Scheme
	DiscoveryClient discovery.DiscoveryInterface

	// APIReader reads straight from the API server, bypassing the manager's cache.
	// Collector discovery looks at Services in namespaces this operator otherwise never
	// touches, and a cached read there would have the manager start — and keep — an
	// informer watching every Service in the cluster, to serve a handful of reads an
	// hour. Nil falls back to the cached client, which is what tests supply.
	APIReader client.Reader

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
// `nodes` is still required: buildMinimalPlatformRole grants it to the agent audit
// ClusterRole, and RBAC escalation-prevention needs the operator to hold it to apply that.
// +kubebuilder:rbac:groups="",resources=namespaces;nodes;events;persistentvolumes;resourcequotas;limitranges;endpoints;pods/log,verbs=get;list;watch
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
// +kubebuilder:rbac:groups=apiextensions.k8s.io,resources=customresourcedefinitions,verbs=get;list;watch

func (r *PlatformAgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

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

	// 2b. Validate the mode gate once at the top; everything downstream asks
	// renderMode, which fails closed. An error here is version skew — a newer
	// CRD's mode value this binary does not know (see mode.go). Today's stack
	// still renders below, so the cluster keeps running what it ran; status
	// reports Degraded/ModeNotRecognized at the end instead of Ready.
	_, modeErr := resolveMode(instance)
	if modeErr != nil {
		log.Info("Unrecognized spec.mode; rendering today's stack and reporting Degraded", "error", modeErr.Error())
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

	// 10. Validate RuntimeClass if specified
	if err := r.validateRuntimeClass(ctx, instance); err != nil {
		if errors.IsNotFound(err) {
			rcName := *instance.Spec.Deployment.Availability.RuntimeClassName
			msg := fmt.Sprintf("RuntimeClass '%s' is not configured in this cluster. For GKE Standard, enable GKE Sandbox by provisioning a gVisor node pool first. In GKE Autopilot, gVisor is supported automatically.", rcName)
			log.Info(msg)
			if statusErr := r.updateStatusDegraded(ctx, instance, "RuntimeClassNotFound", msg); statusErr != nil {
				return ctrl.Result{}, statusErr
			}
			return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
		}
		return ctrl.Result{}, fmt.Errorf("failed to validate RuntimeClass: %w", err)
	}

	// 10b. Refuse a broker split that would strand the event watcher.
	//
	// Before the workload, deliberately: an operator who asked for the broker to
	// leave the agent Pod must not get a running agent whose cluster events have
	// silently stopped. The one refusable configuration is named in
	// validateCredentialBrokerSplit.
	if reason, msg := validateCredentialBrokerSplit(instance); reason != "" {
		log.Info(msg)
		if statusErr := r.updateStatusDegraded(ctx, instance, reason, msg); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}

	// 10c. Refuse an egress policy whose layout the broker reconcile below
	// would otherwise dismantle.
	//
	// Before reconcileCredentialBroker, deliberately, and the order is the
	// finding this step answers: on the reconcile that flips
	// splitCredentialBrokerPod off while egressPolicy is still Allowlist —
	// the single-field edit warnSplitNeedsSharedFilesystem itself suggests —
	// validating after the broker reconcile deletes the broker Deployment and
	// Service first and refuses second. The refusal then withholds the
	// workload, so the agent Deployment keeps its split shape, wired to a
	// Service that no longer exists, every proxied command failing, and the
	// 30-second requeue repeating the same refusal without ever putting the
	// broker back. Refusing here leaves the broker running instead: the CR
	// parks Degraded, the agent keeps working, and the message names the two
	// ways out.
	//
	// The guardrail note on the allowlist refusal below applies here too, so
	// this path reconciles the same two policies before returning.
	if reason, msg := validateEgressPolicyLayout(instance); reason != "" {
		log.Info(msg)
		if err := r.reconcileAgentNetworkGuardrails(ctx, instance, reason); err != nil {
			return ctrl.Result{}, err
		}
		if statusErr := r.updateStatusDegraded(ctx, instance, reason, msg); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}

	// 10d. Reconcile the credential broker's own Pod, if it has one.
	//
	// Before the agent's workload, not after. On the reconcile that first turns
	// the split on, the agent Deployment is re-rendered pointing at the broker
	// Service; creating that Service afterwards leaves the restarted agent
	// failing every proxied command with a connection refused until the next
	// pass. The other direction is safe either way, because turning the split
	// off deletes a broker the re-rendered agent has already stopped using —
	// step 10c has already refused the one shape where it has not.
	if err := r.reconcileCredentialBroker(ctx, instance, proxyPolicyHash); err != nil {
		return ctrl.Result{}, err
	}

	// 10e. Refuse an allowlist destination the policy will not render.
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
		// which step 11b renders.
		//
		// Both have to survive a refusal for the same reason. An operator
		// triaging an EgressAllowlistRefused who deletes them gets neither back
		// until the spec is fixed, and with nothing selecting the agent Pod
		// NetworkPolicy permits all egress — so the outcome is wide-open egress
		// behind a Degraded status that names only the allowlist. The gateway
		// policy is unconditional because it has nothing to do with either
		// refusal; it is the Pod's baseline and it predates this field.
		//
		// This closes the hazard at the two egress refusals only — this one
		// and step 10c's. The two refusals above them — step 10's
		// RuntimeClassNotFound and step 10b's SplitBrokerStrandsEventWatcher —
		// return without reconciling the gateway policy and still have it.
		// Issue #964 tracks that; do not read the rule stated here as one the
		// whole function keeps yet.
		if err := r.reconcileAgentNetworkGuardrails(ctx, instance, reason); err != nil {
			return ctrl.Result{}, err
		}
		if statusErr := r.updateStatusDegraded(ctx, instance, reason, msg); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}

	// 11. Reconcile the Agent Sandbox Pod with its Envoy credential sidecar.
	otlpEndpoint, otlpSource := r.resolveOTLPEndpoint(ctx, instance)
	otlpDisabled := otlpSource == otlpSourceNone
	netpolProf := r.resolveNetpolProfile(ctx, instance)
	if err := r.reconcileWorkload(ctx, instance, configMapHash, fluentBitHash, settingsHash, proxyPolicyHash, agentPlugins, otlpEndpoint, otlpDisabled); err != nil {
		return ctrl.Result{}, err
	}

	// 11b. Reconcile the agent Pod's default-deny egress policy, if it has one.
	if err := r.reconcileAgentEgressPolicy(ctx, instance, r.agentEgressDNSClusterIPs(ctx, instance, netpolProf)); err != nil {
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
	if err := r.deleteLegacyCredentialIsolationResources(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 9. Update status phase. While the mode is unrecognized the phase is
	// Degraded with a named reason — silently rendering today at that point
	// would leave nothing in `kubectl describe` saying the cluster runs
	// something other than what the spec asks. Requeue: the skew resolves by
	// an operator upgrade or a spec correction, neither of which is an event
	// on this object's watches.
	if modeErr != nil {
		msg := modeErr.Error() + " (version skew); rendering today's stack until the operator is upgraded or spec.mode is corrected"
		if statusErr := r.updateStatusDegraded(ctx, instance, "ModeNotRecognized", msg); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}
	phase, err := r.updateStatusReady(ctx, instance, otlpEndpoint, otlpSource, netpolProf)
	if err != nil {
		return ctrl.Result{}, err
	}

	// A plugin image that cannot be pulled only surfaces on the pod seconds after the
	// workload is written, and Pods are not watched here. Requeue while the picture is
	// still incomplete so both the failure and the later recovery reach plugin status.
	if pluginStatusNeedsRecheck(agentPlugins, phase == "Ready") {
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}

	// Default and None are the telemetry outcomes that can improve without anything else
	// changing — someone installs a collector and nothing about this agent is touched.
	// Reconciles are event-driven and can be quiet for hours, so nudge the probe rather
	// than wait for an unrelated event. Every other source is explicit or already found
	// something, and needs no polling. None especially: it is the outcome that leaves the
	// agent exporting nowhere, so it is the one an operator most wants picked up promptly
	// once they install a collector.
	if otlpSource == otlpSourceDefault || otlpSource == otlpSourceNone {
		return ctrl.Result{RequeueAfter: otelRediscoverAfter}, nil
	}
	return ctrl.Result{}, nil
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

		// Resource is deleted. Safe to remove finalizer and update.
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
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	if !strings.HasPrefix(raw, "[") {
		return nil, fmt.Errorf("managed_repos JSON must be an array starting with '['")
	}
	var entries []agentv1alpha1.ManagedRepoEntry
	if err := json.Unmarshal([]byte(raw), &entries); err != nil {
		return nil, fmt.Errorf("failed to unmarshal managed_repos JSON: %w", err)
	}
	var res []agentv1alpha1.ManagedRepoEntry
	for _, e := range entries {
		u := strings.TrimSpace(e.URL)
		t := strings.TrimSpace(e.Type)
		if u != "" && t != "" {
			res = append(res, agentv1alpha1.ManagedRepoEntry{Type: t, URL: u})
		}
	}
	return res, nil
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
// managed repositories. If spec.integration.github.gitRepo is defined on the CR, it is seeded
// into managed_repos and kept present on subsequent reconciles without removing any additional
// repositories added to the ConfigMap.
//
// Repository lifecycle and removal:
// The reconciler appends any repository declared in spec.integration.github.gitRepo to managed_repos
// if it is not already present in the ConfigMap, preserving all existing entries.
// Repository removal/unregistration is administrator-driven via the ConfigMap: to unregister a
// repository, remove its entry directly from managed_repos in the <agent-name>-gitops-state ConfigMap.
// If the repository to be removed was declared in spec.integration.github.gitRepo on the CR, clear or
// update gitRepo on the CR as well so the reconciler does not re-append it on subsequent passes.
func (r *PlatformAgentReconciler) reconcileGitopsStateConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
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
			return r.syncGithubTokenMinterConfigMap(ctx, agent, cm.Data["managed_repos"])
		}
		return err
	}

	// If the CR spec provides a repository and the existing ConfigMap does not include it,
	// ensure the repository is recorded without overwriting other dynamically added repositories.
	if cmRepo, ok := cm.Data["managed_repos"]; ok && cmRepo != "" {
		if found.Data == nil {
			found.Data = map[string]string{}
		}
		existing := strings.TrimSpace(found.Data["managed_repos"])
		if existing == "" {
			found.Data["managed_repos"] = cmRepo
			if err := r.Update(ctx, found); err != nil {
				return err
			}
			return r.syncGithubTokenMinterConfigMap(ctx, agent, cmRepo)
		}
		specEntries, err := parseManagedRepoEntries(cmRepo)
		if err != nil {
			return fmt.Errorf("failed to parse spec repository JSON: %w", err)
		}
		existingEntries, err := parseManagedRepoEntries(existing)
		if err != nil {
			return fmt.Errorf("failed to parse existing managed_repos in ConfigMap %s: %w", found.Name, err)
		}
		updated := false
		for _, se := range specEntries {
			present := false
			for _, ee := range existingEntries {
				if ee.URL == se.URL {
					present = true
					break
				}
			}
			if !present {
				existingEntries = append(existingEntries, se)
				updated = true
			}
		}
		if updated {
			if jsonBytes, err := json.Marshal(existingEntries); err == nil {
				found.Data["managed_repos"] = string(jsonBytes)
			}
			if err := r.Update(ctx, found); err != nil {
				return err
			}
			return r.syncGithubTokenMinterConfigMap(ctx, agent, found.Data["managed_repos"])
		}
	}

	return r.syncGithubTokenMinterConfigMap(ctx, agent, found.Data["managed_repos"])
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

// syncGithubTokenMinterConfigMap ensures that for every repository in managed_repos that belongs
// to the primary GitHub organization (spec.integration.github.org), a corresponding <repo>.yaml
// entry exists in github-token-minter-config ConfigMap.
// Repositories belonging to a different organization are skipped because the minter instance is
// bound to the primary organization directory (/etc/minty/<primary-org>/).
// Operator-managed <repo>.yaml entries for repositories that are no longer managed are pruned.
func (r *PlatformAgentReconciler) syncGithubTokenMinterConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent, managedReposStr string) error {
	logger := logf.FromContext(ctx)
	minterCM := &corev1.ConfigMap{}
	err := r.Get(ctx, client.ObjectKey{Name: "github-token-minter-config", Namespace: agent.Namespace}, minterCM)
	if err != nil {
		if errors.IsNotFound(err) {
			return nil
		}
		return err
	}

	if minterCM.Data == nil {
		return nil
	}

	baseTemplate, ok := minterCM.Data["default.yaml"]
	if !ok || strings.TrimSpace(baseTemplate) == "" {
		return nil
	}

	managedReposStr = strings.TrimSpace(managedReposStr)

	// Read operator-managed keys from annotation
	existingAnn := ""
	if minterCM.Annotations != nil {
		existingAnn = minterCM.Annotations[AnnotationManagedMinterKeys]
	}
	operatorManagedKeys := parseManagedKeysAnnotation(existingAnn)

	// An empty managed_repos with no previously operator-managed keys is a no-op to avoid wiping unmanaged keys.
	if managedReposStr == "" && len(operatorManagedKeys) == 0 {
		return nil
	}

	primaryOrg := ""
	if agent.Spec.Integration != nil && agent.Spec.Integration.GitHub != nil {
		github := agent.Spec.Integration.GitHub
		primaryOrg = strings.TrimSpace(github.Org)
		if primaryOrg == "" && github.GitRepo != "" {
			if cleaned, err := agentv1alpha1.CleanRepoSlug(github.GitRepo); err == nil {
				parts := strings.SplitN(cleaned, "/", 2)
				if len(parts) == 2 {
					primaryOrg = parts[0]
				}
			}
		}
	}

	repos, err := parseManagedRepos(managedReposStr)
	if err != nil {
		return fmt.Errorf("failed to parse managed_repos for minter policy sync: %w", err)
	}
	var allBareRepos []string
	activeKeys := make(map[string]string, len(repos))
	for _, fullRepo := range repos {
		fullRepo = strings.TrimSpace(fullRepo)
		if fullRepo == "" {
			continue
		}
		slug, err := agentv1alpha1.CleanRepoSlugWithOrg(fullRepo, primaryOrg)
		if err != nil {
			logger.V(1).Info("skipping invalid repo in managed_repos for minter policy sync", "repo", fullRepo, "error", err)
			continue
		}
		parts := strings.SplitN(slug, "/", 2)
		if len(parts) == 2 {
			repoOrg := parts[0]
			bareRepo := parts[1]
			if primaryOrg != "" && !strings.EqualFold(repoOrg, primaryOrg) {
				logger.Info("skipping cross-org repository in minter policy sync; minter is scoped to primary org",
					"repo", fullRepo, "repoOrg", repoOrg, "primaryOrg", primaryOrg)
				continue
			}
			if _, exists := activeKeys[bareRepo+".yaml"]; !exists {
				activeKeys[bareRepo+".yaml"] = bareRepo
				allBareRepos = append(allBareRepos, bareRepo)
			}
		}
	}
	sort.Strings(allBareRepos)

	updated := false

	// Ensure all active managed repositories have policy entries containing all same-org managed repositories
	expectedContent := renderRepoPolicy(baseTemplate, allBareRepos)
	for key := range activeKeys {
		currentVal, exists := minterCM.Data[key]
		_, managed := operatorManagedKeys[key]
		if !exists {
			minterCM.Data[key] = expectedContent
			operatorManagedKeys[key] = struct{}{}
			updated = true
		} else if managed && currentVal != expectedContent {
			minterCM.Data[key] = expectedContent
			updated = true
		}
	}

	// Prune policy entries ONLY for repositories that were previously managed by the operator but are no longer active
	for key := range operatorManagedKeys {
		if key == "default.yaml" {
			continue
		}
		if _, active := activeKeys[key]; !active {
			delete(minterCM.Data, key)
			delete(operatorManagedKeys, key)
			updated = true
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

	// Note: Switching between Deployment and StatefulSet causes a full delete+recreate of the workload.
	// This will incur downtime and potentially stuck pods if RWO volumes take time to unbind.
	// This is an acceptable tradeoff since switching replicas/storage requires an explicit CRD update.
	if useStatefulSet(agent) {
		dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-gateway", Namespace: agent.Namespace}}
		if err := client.IgnoreNotFound(r.Delete(ctx, dep)); err != nil {
			return fmt.Errorf("failed to cleanup legacy Deployment: %w", err)
		}

		sts := buildStatefulSet(agent, configHash, fluentBitHash, settingsHash, policyHash, agentPlugins, opts)
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
	if err := ctrl.SetControllerReference(agent, dep, r.Scheme); err != nil {
		return err
	}
	return r.applyManaged(ctx, agent, dep)
}

// deleteLegacyCredentialIsolationResources removes the workload objects left
// behind by the two-pod layout that shipped in fb99cd1 and was collapsed back
// into a sidecar in 9b2b7e8. Nothing recreates these names, so leaving them
// running would leave a second, unreconciled copy of the agent alive.
//
// The <name>-credential-proxy Deployment and Service used to be on this list.
// They are not legacy any more — they are what reconcileCredentialBroker
// renders when the split is enabled, and it owns them in both directions.
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

// reconcileCredentialBroker renders, or removes, the broker's own Pod.
//
// It owns <name>-credential-proxy in both directions: applied when
// spec.security.splitCredentialBrokerPod is true, deleted when it is false, so
// that turning the gate back off does not leave a second broker running against
// the same workspace. That is cleanup of this controller's own workload, not the
// removal of a guardrail it did not create — see
// deleteLegacyCredentialIsolationResources.
func (r *PlatformAgentReconciler) reconcileCredentialBroker(ctx context.Context, agent *agentv1alpha1.PlatformAgent, policyHash string) error {
	log := logf.FromContext(ctx)
	tokenReviewName := fmt.Sprintf("kubeagents:tokenreview:%s:%s", agent.Namespace, agent.Name)

	if !credentialBrokerIsSplit(agent) {
		owned := []client.Object{
			&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: credentialBrokerName(agent), Namespace: agent.Namespace}},
			&corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: credentialBrokerName(agent), Namespace: agent.Namespace}},
		}
		for _, object := range owned {
			if err := r.deleteIfOwned(ctx, agent, object); err != nil {
				return err
			}
		}
		for _, object := range []client.Object{
			&rbacv1.ClusterRoleBinding{ObjectMeta: metav1.ObjectMeta{Name: tokenReviewName}},
			&rbacv1.ClusterRole{ObjectMeta: metav1.ObjectMeta{Name: tokenReviewName}},
		} {
			if err := r.deleteIfManaged(ctx, object); err != nil {
				return err
			}
		}
		return nil
	}

	r.warnSplitNeedsSharedFilesystem(ctx, agent)

	// The broker verifies its callers with a TokenReview, which needs one verb
	// on one virtual resource and grants no read access to anything.
	role := buildCredentialBrokerTokenReviewRole(agent)
	if err := r.applyManaged(ctx, agent, role); err != nil {
		return fmt.Errorf("failed to reconcile credential broker TokenReview ClusterRole: %w", err)
	}
	binding := buildClusterRoleBinding(agent, tokenReviewName, role.Name)
	if err := r.applyManaged(ctx, agent, binding); err != nil {
		return fmt.Errorf("failed to reconcile credential broker TokenReview ClusterRoleBinding: %w", err)
	}

	homeDir := defaultAgentHome
	if h := agent.Spec.Harness; h != nil && h.Hermes != nil && h.Hermes.AgentHome != "" {
		homeDir = h.Hermes.AgentHome
	}
	deployment := buildCredentialBrokerDeployment(agent, policyHash, homeDir)
	if err := ctrl.SetControllerReference(agent, deployment, r.Scheme); err != nil {
		return err
	}
	if err := r.applyManaged(ctx, agent, deployment); err != nil {
		return fmt.Errorf("failed to reconcile credential broker Deployment: %w", err)
	}

	service := buildCredentialBrokerService(agent)
	if err := ctrl.SetControllerReference(agent, service, r.Scheme); err != nil {
		return err
	}
	if err := r.applyManaged(ctx, agent, service); err != nil {
		return fmt.Errorf("failed to reconcile credential broker Service: %w", err)
	}

	log.Info("credential broker runs in its own Pod",
		"deployment", deployment.Name, "service", service.Name)
	return nil
}

// warnSplitNeedsSharedFilesystem says so, loudly, when the split is enabled
// while the two Pods cannot see the same files.
//
// The broker runs proxied commands with a working directory the agent created
// on this volume, so today both Pods have to mount it read-write at the same
// path. A ReadWriteOnce claim cannot do that across nodes: the broker Pod stays
// Pending with a Multi-Attach error and never becomes a Service endpoint, so
// the agent sees a connection refused on every command. The containment check
// in the broker does not catch it either — it is lexical, both Pods are
// configured with the same workspace root, so the path always looks right and
// what is missing is the data behind it.
//
// The access mode is what this reads, because it is the one signal available.
// It is not a product requirement, and the fix is not to go and buy a
// ReadWriteMany volume — that is one way to satisfy today's design and an
// operator may pick it, but the managed options bill on provisioned capacity
// with a floor far above what a workspace needs. The supported answer is to
// leave the split off until the broker owns the workspace on a volume of its
// own and takes content rather than a directory, a separate change that
// removes the coupling entirely. Co-scheduling both Pods on one node is
// not the answer: it deadlocks the next rolling update on the volume and makes
// the two Pods a single failure domain.
//
// A log line rather than a Degraded status, because unlike the event-watcher
// refusal there is nothing the operator can set to make this correct — the
// access mode of an existing claim cannot be changed in place, and refusing to
// reconcile would not help them.
func (r *PlatformAgentReconciler) warnSplitNeedsSharedFilesystem(ctx context.Context, agent *agentv1alpha1.PlatformAgent) {
	log := logf.FromContext(ctx)
	pvc := &corev1.PersistentVolumeClaim{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-data"}, pvc); err != nil {
		return
	}
	if slices.Contains(pvc.Spec.AccessModes, corev1.ReadWriteMany) {
		return
	}
	log.Info("WARNING: splitCredentialBrokerPod is enabled, and the agent Pod and the broker Pod cannot see the "+
		"same files: the broker runs proxied commands in a directory the agent created on this claim, and its "+
		"access mode does not let both Pods mount it read-write at once. The broker Pod will stay Pending with a "+
		"Multi-Attach error and every proxied command will report the credential proxy as unavailable. Turn the "+
		"split off. A ReadWriteMany claim also satisfies today's design and is a choice available to you, but it "+
		"is not what this product asks for; the supported path is to wait for the broker to own the workspace and "+
		"take content rather than a directory. Do not co-schedule the two Pods to work around this — it deadlocks "+
		"the next rolling update on the volume.",
		"claim", pvc.Name, "accessModes", pvc.Spec.AccessModes)
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

const (
	// reasonEgressPolicyRequiresSplitBroker refuses the layout: the policy
	// cannot be rendered at all, because it would govern the credential broker
	// sharing the Pod.
	reasonEgressPolicyRequiresSplitBroker = "EgressPolicyRequiresSplitBroker"

	// reasonEgressAllowlistRefused refuses the contents: the policy is fine and
	// still gets rendered, minus the destinations that were refused.
	reasonEgressAllowlistRefused = "EgressAllowlistRefused"
)

// refusalStillRendersTheGuardrail reports whether the egress policy should be
// reconciled despite the agent's spec being refused.
//
// The distinction is between refusing a layout and refusing a value. A refused
// value leaves a perfectly good policy to render — the builder has already
// dropped the offending destination — and withholding it would mean the
// operator's mistake in one field silently removes the whole control.
func refusalStillRendersTheGuardrail(reason string) bool {
	return reason == reasonEgressAllowlistRefused
}

// validateEgressPolicy returns a Degraded reason and message when
// spec.security.egressPolicy asks for something the operator cannot honestly
// render, or "" when it can.
//
// There are two such cases.
//
// The first, and the important one: the rendered policy denies the agent Pod
// the link-local metadata server by not listing it, and a NetworkPolicy selects
// Pods, never containers. With the credential broker still a sidecar the two
// share a network namespace, so the same policy governs both — and the broker
// reaches the metadata server on purpose, because minting the cloud token is
// its entire job. Rendering it there would take the agent's credentials away
// and every proxied command would fail.
//
// The second: an operator-supplied destination the policy refuses to render.
// The builder drops those rather than narrowing them, and a silently dropped
// rule is its own failure — an operator who added a rule to restore GitHub
// would get a Ready agent, an unreachable github.com, and nothing in
// kubectl describe to connect the two. So the refusal is surfaced here rather
// than left in a log line the operator has no reason to read.
//
// The alternative to refusing was to render anyway and let the operator find
// out, or to render and quietly permit the metadata server so nothing breaks.
// The second is worse than doing nothing: it is a control that appears on
// kubectl get netpol and protects nothing.
func validateEgressPolicy(agent *agentv1alpha1.PlatformAgent) (string, string) {
	if reason, msg := validateEgressPolicyLayout(agent); reason != "" {
		return reason, msg
	}
	return validateEgressAllowlist(agent)
}

// validateEgressPolicyLayout is the first case alone. Reconcile checks it
// before reconcileCredentialBroker rather than with the allowlist check below,
// because on the reconcile that flips splitCredentialBrokerPod off under a
// live egressPolicy the broker teardown is the mutation this refusal exists to
// stop — validated afterwards, the refusal arrives one step too late: the
// broker Deployment and Service are already deleted, the refusal withholds the
// workload, and the running agent is left wired to a Service that no longer
// exists with nothing on the requeue path that puts it back.
func validateEgressPolicyLayout(agent *agentv1alpha1.PlatformAgent) (string, string) {
	if !agentEgressPolicyEnabled(agent) {
		return "", ""
	}
	if !credentialBrokerIsSplit(agent) {
		return reasonEgressPolicyRequiresSplitBroker, "spec.security.egressPolicy: Allowlist requires " +
			"spec.security.splitCredentialBrokerPod: true. The policy denies the agent Pod the link-local " +
			"metadata server, and a NetworkPolicy cannot tell two containers in one Pod apart — with the " +
			"credential broker still a sidecar it would lose the metadata server too, and minting the cloud " +
			"token there is what the broker is for. Enable the split or set egressPolicy: None and accept " +
			"that the agent can reach the metadata server."
	}
	return "", ""
}

// validateEgressAllowlist is the second case alone. It stays below
// reconcileCredentialBroker in Reconcile, deliberately: a refusal about one
// destination should not stop the broker being reconciled.
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
// <name>-gateway-netpol is reconciled whatever the refusal was. It is the
// Pod's baseline policy, it predates spec.security.egressPolicy, and neither
// refusal is an objection to it. <name>-sandbox-metadata-deny is reconciled
// only when refusalStillRendersTheGuardrail says so — see validateEgressPolicy
// for why EgressPolicyRequiresSplitBroker is the case where rendering it is
// itself the harm.
func (r *PlatformAgentReconciler) reconcileAgentNetworkGuardrails(ctx context.Context, agent *agentv1alpha1.PlatformAgent, reason string) error {
	otlpEndpoint, otlpSource := r.resolveOTLPEndpoint(ctx, agent)
	netpolProf := r.resolveNetpolProfile(ctx, agent)
	if err := r.reconcileNetworkPolicy(ctx, agent, netpolProf, otlpEndpoint, otlpSource == otlpSourceNone); err != nil {
		return err
	}
	if !refusalStillRendersTheGuardrail(reason) {
		return nil
	}
	return r.reconcileAgentEgressPolicy(ctx, agent, r.agentEgressDNSClusterIPs(ctx, agent, netpolProf))
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
// The cost is a stale policy after an opt-out. That is fail-closed on its own,
// but it is not harmless if splitCredentialBrokerPod is reverted in the same
// edit: the broker returns to the agent Pod, the leftover policy selects that
// Pod, and the broker loses the metadata server along with the sandbox. The
// egressPolicy CRD field description carries the warning and the three-step
// revert order, so it reaches kubectl explain.
func (r *PlatformAgentReconciler) reconcileAgentEgressPolicy(ctx context.Context, agent *agentv1alpha1.PlatformAgent, dnsClusterIPs []string) error {
	if !agentEgressPolicyEnabled(agent) {
		return nil
	}
	log := logf.FromContext(ctx)

	// validateEgressPolicy has already refused the reconcile if any of these
	// fired, so reaching the loop below means something calls this builder on a
	// path that skipped validation. Log it rather than assume: the drop is what
	// keeps the rendered object safe, and a silent drop is the failure mode
	// this guard exists for.
	policy, dropped := buildAgentEgressNetworkPolicy(agent, dnsClusterIPs)
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
	// The split credential broker's TokenReview grant is applied by
	// reconcileCredentialBroker on every reconcile, through applyManaged,
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

// updateStatusReady writes the agent's status and returns the phase it settled on, so
// the caller can decide whether the agent is still converging. otlpEndpoint, otlpSource,
// and netpolProfile are the resolved telemetry and network policy wiring; they are reported
// rather than derived because discovery is otherwise invisible to anyone reading the CR.
func (r *PlatformAgentReconciler) updateStatusReady(ctx context.Context, agent *agentv1alpha1.PlatformAgent, otlpEndpoint, otlpSource string, netpolProfile netpolProfile) (string, error) {
	newDeploymentStatusName := ""
	newDeploymentStatusReadyReplicas := int32(0)
	var errWorkload error

	if useStatefulSet(agent) {
		sts := &appsv1.StatefulSet{}
		errWorkload = r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}, sts)
		if errWorkload != nil && !errors.IsNotFound(errWorkload) {
			return "", fmt.Errorf("failed to get StatefulSet for status update: %w", errWorkload)
		}
		if errWorkload == nil {
			newDeploymentStatusName = sts.Name
			newDeploymentStatusReadyReplicas = sts.Status.ReadyReplicas
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

	// Determine Phase and Condition
	newPhase := "Provisioning"
	condStatus := metav1.ConditionFalse
	condReason := "Provisioning"
	condMsg := "Waiting for deployment replicas to be ready"
	if errWorkload == nil && newDeploymentStatusReadyReplicas > 0 {
		newPhase = "Ready"
		condStatus = metav1.ConditionTrue
		condReason = "Reconciled"
		condMsg = "Agent sandbox and Envoy credential sidecar are fully reconciled"
	} else if errWorkload == nil {
		if phaseOverride, reasonOverride, msgOverride := r.getDeploymentStatusDetails(ctx, agent); reasonOverride != "Provisioning" {
			newPhase = phaseOverride
			condReason = reasonOverride
			condMsg = msgOverride
		}
	}

	gitRepoErr := error(nil)
	if agent.Spec.Integration != nil && agent.Spec.Integration.GitHub != nil {
		if err := agentv1alpha1.ValidateGitHubOrg(agent.Spec.Integration.GitHub.Org); err != nil {
			gitRepoErr = err
		} else if err := agentv1alpha1.ValidateGitRepoURLWithOrg(agent.Spec.Integration.GitHub.GitRepo, agent.Spec.Integration.GitHub.Org); err != nil {
			gitRepoErr = err
		}
	}

	degradedStatus := metav1.ConditionFalse
	if gitRepoErr != nil {
		newPhase = "Degraded"
		condStatus = metav1.ConditionFalse
		condReason = "InvalidGitRepoURL"
		condMsg = fmt.Sprintf("Invalid gitRepo URL or org (%s); GitOps disabled in config", gitRepoErr.Error())
		degradedStatus = metav1.ConditionTrue
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

	existingCond := meta.FindStatusCondition(agent.Status.Conditions, "Ready")
	existingDegradedCond := meta.FindStatusCondition(agent.Status.Conditions, "Degraded")
	degradedUnchanged := (degradedStatus == metav1.ConditionFalse && existingDegradedCond == nil) ||
		(degradedStatus == metav1.ConditionTrue && existingDegradedCond != nil && existingDegradedCond.Status == metav1.ConditionTrue && existingDegradedCond.Reason == "InvalidGitRepoURL" && existingDegradedCond.Message == condMsg)

	// Check if anything actually changed
	if agent.Status.Phase == newPhase &&
		agent.Status.DeploymentStatus.Name == newDeploymentStatusName &&
		agent.Status.DeploymentStatus.ReadyReplicas == newDeploymentStatusReadyReplicas &&
		agent.Status.StorageStatus.Bound == newStorageStatusBound &&
		agent.Status.ServiceStatus.Endpoint == newServiceStatusEndpoint &&
		agent.Status.Address == newAddress &&
		agent.Status.Telemetry.OTLPEndpoint == otlpEndpoint &&
		agent.Status.Telemetry.OTLPEndpointSource == otlpSource &&
		networkPolicyStatusUnchanged(agent.Status.NetworkPolicy, netpolProfile) &&
		degradedUnchanged &&
		eventWatcherUnchanged &&
		existingCond != nil && existingCond.Status == condStatus && existingCond.Reason == condReason && existingCond.Message == condMsg {
		return newPhase, nil
	}

	// Apply updates
	agent.Status.Phase = newPhase
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

	now := metav1.Now()
	agent.Status.LastReconcileTime = &now

	condition := metav1.Condition{
		Type:               "Ready",
		Status:             condStatus,
		Reason:             condReason,
		Message:            condMsg,
		LastTransitionTime: now,
	}
	meta.SetStatusCondition(&agent.Status.Conditions, condition)

	if degradedStatus == metav1.ConditionTrue {
		degradedCond := metav1.Condition{
			Type:               "Degraded",
			Status:             metav1.ConditionTrue,
			Reason:             "InvalidGitRepoURL",
			Message:            condMsg,
			LastTransitionTime: now,
		}
		meta.SetStatusCondition(&agent.Status.Conditions, degradedCond)
	} else {
		meta.RemoveStatusCondition(&agent.Status.Conditions, "Degraded")
	}

	if eventWatcherOn {
		meta.RemoveStatusCondition(&agent.Status.Conditions, eventWatcherConditionType)
	} else {
		meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
			Type:               eventWatcherConditionType,
			Status:             metav1.ConditionFalse,
			Reason:             eventWatcherDisabledReason,
			Message:            eventWatcherDisabledMessage,
			LastTransitionTime: now,
		})
	}

	return newPhase, r.Status().Update(ctx, agent)
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

func (r *PlatformAgentReconciler) getDeploymentStatusDetails(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (phase string, reason string, message string) {
	phase = "Provisioning"
	reason = "Provisioning"
	message = "Waiting for deployment replicas to be ready"

	podList := &corev1.PodList{}
	err := r.List(ctx, podList, client.InNamespace(agent.Namespace), client.MatchingLabels{"app": agent.Name + "-gateway"})
	if err != nil || len(podList.Items) == 0 {
		return phase, reason, message
	}

	for _, pod := range podList.Items {
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
		for _, cs := range initThenApp {
			if cs.State.Waiting != nil && cs.State.Waiting.Reason != "" &&
				cs.State.Waiting.Reason != "ContainerCreating" && cs.State.Waiting.Reason != "PodInitializing" {
				phase = "Degraded"
				reason = cs.State.Waiting.Reason
				message = fmt.Sprintf("Container '%s' in pod %s is waiting: %s - %s", cs.Name, pod.Name, cs.State.Waiting.Reason, cs.State.Waiting.Message)
				return phase, reason, message
			}
		}

		// 2. Check pod scheduling conditions (Unschedulable due to node selector/affinity/gVisor)
		for _, cond := range pod.Status.Conditions {
			if cond.Type == corev1.PodScheduled && cond.Status == corev1.ConditionFalse && cond.Reason == "Unschedulable" {
				phase = "Degraded"
				reason = "PodUnschedulable"
				if agent.Spec.Deployment != nil && agent.Spec.Deployment.Availability != nil && agent.Spec.Deployment.Availability.RuntimeClassName != nil && *agent.Spec.Deployment.Availability.RuntimeClassName != "" {
					rcName := *agent.Spec.Deployment.Availability.RuntimeClassName
					message = fmt.Sprintf("Pod %s is waiting to be scheduled because no nodes in the cluster match the requested RuntimeClass '%s'. For GKE Standard, enable GKE Sandbox by provisioning a gVisor node pool.", pod.Name, rcName)
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

func (r *PlatformAgentReconciler) validateRuntimeClass(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	if agent.Spec.Deployment == nil || agent.Spec.Deployment.Availability == nil || agent.Spec.Deployment.Availability.RuntimeClassName == nil || *agent.Spec.Deployment.Availability.RuntimeClassName == "" {
		return nil
	}

	rcName := *agent.Spec.Deployment.Availability.RuntimeClassName
	rc := &nodev1.RuntimeClass{}
	err := r.Get(ctx, types.NamespacedName{Name: rcName}, rc)
	if err != nil {
		return err
	}
	return nil
}

func (r *PlatformAgentReconciler) updateStatusDegraded(ctx context.Context, agent *agentv1alpha1.PlatformAgent, reason, message string) error {
	agent.Status.Phase = "Degraded"
	now := metav1.Now()
	agent.Status.LastReconcileTime = &now

	condition := metav1.Condition{
		Type:               "Ready",
		Status:             metav1.ConditionFalse,
		Reason:             reason,
		Message:            message,
		LastTransitionTime: now,
	}
	meta.SetStatusCondition(&agent.Status.Conditions, condition)
	return r.Status().Update(ctx, agent)
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
					var list agentv1alpha1.PlatformAgentList
					if err := mgr.GetClient().List(ctx, &list, client.InNamespace(ext.Namespace)); err != nil {
						return nil
					}
					var reqs []reconcile.Request
					for _, agent := range list.Items {
						reqs = append(reqs, reconcile.Request{
							NamespacedName: types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name},
						})
					}
					return reqs
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

// isGKEAutopilot probes the API server for GKE Autopilot specific API groups.
func isGKEAutopilot(dc discovery.DiscoveryInterface) bool {
	if dc == nil {
		return false
	}
	defer func() {
		_ = recover()
	}()
	groups, err := dc.ServerGroups()
	if err != nil || groups == nil {
		return false
	}
	for _, g := range groups.Groups {
		if g.Name == gkeAutopilotAPIGroup || g.Name == gkeWardenAPIGroup {
			return true
		}
	}
	return false
}

// clusterImageVolumeSupport probes the API server for ImageVolume support.
//
// determined reports whether the answer is authoritative. When the capability cannot be
// established — no discovery client, an unreachable API server, an unparseable version —
// supported is false and determined is false: the caller must fail closed for this pass
// but must not remember the answer, because the next probe may succeed.
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

	// GKE Autopilot clusters enforce GKE Warden admission policies (autopilot-volume-type-limitation)
	// that reject the Image volume type. On Autopilot, fall back to initContainer/emptyDir staging.
	// On GKE Standard (and non-GKE clusters), ImageVolumeSource is supported natively on Kubernetes 1.35+.
	if isGKEAutopilot(dc) {
		log.Info("GKE Autopilot cluster detected; using initContainer plugin staging fallback. " + override)
		return false, true
	}

	if major > 1 {
		return true, true
	}
	return major == 1 && minor >= 35, true
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
					} else if w.Reason == "CrashLoopBackOff" {
						msg := w.Message
						if cs.LastTerminationState.Terminated != nil && cs.LastTerminationState.Terminated.ExitCode != 0 {
							msg = fmt.Sprintf("staging init container exited with code %d", cs.LastTerminationState.Terminated.ExitCode)
							if cs.LastTerminationState.Terminated.Message != "" {
								msg = fmt.Sprintf("%s: %s", msg, cs.LastTerminationState.Terminated.Message)
							}
						} else if msg == "" {
							msg = "staging init container crashed"
						}
						failures[plugin.Name] = pluginFailure{
							reason:  pluginFailureReasonStaging,
							message: msg,
						}
					}
				} else if t := cs.State.Terminated; t != nil && t.ExitCode != 0 {
					msg := fmt.Sprintf("staging init container exited with code %d", t.ExitCode)
					if t.Message != "" {
						msg = fmt.Sprintf("%s: %s", msg, t.Message)
					}
					failures[plugin.Name] = pluginFailure{
						reason:  pluginFailureReasonStaging,
						message: msg,
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
