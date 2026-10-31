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
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path"
	"slices"
	"sort"
	"strings"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The scope publisher: under mode next, a Job the operator starts resolves
// the folder, organization, Shared VPC and Metrics Scope members of
// spec.scope through the credential broker's `scope` role, and writes the
// result into one ConfigMap, for the A2A gateway to read before it admits a
// task whose target project scope.json does not list explicitly. The Job runs
// agents/platform/scripts/scope_publisher.py; the names, the document,
// memberHash, the Job's env and the schedule below are the two sides of one
// interface, and a change to one is a change to both.
//
// The operator owns the ConfigMap and applies it with no data, as it does the
// home-channel record (platformagent_home_channel_record.go): the publisher
// merge-patches the one data key, so server-side apply never resets it, and
// the ConfigMap is in no rollout hash, so a publish rolls no pod. The
// ConfigMap and the gateway's read of it render on every next pass. The
// publisher's identity and its patch grant render only while a Job may run
// (scopePublisherMayRun), because without the gateway pod admission policy a
// pod could run under that identity and write the document the gateway
// trusts. A today install renders none of it.
const (
	scopeResolvedConfigMapSuffix = "-resolved-scope"
	// scopeResolvedDataKey is the one key the publisher writes.
	scopeResolvedDataKey = "resolved-scope.json"

	scopePublisherSuffix    = "-scope-publisher"
	scopePublisherComponent = "scope-publisher"
	scopePublisherContainer = "scope-publisher"

	// The agent image's interpreter and the publisher's entrypoint
	// (agents/platform/scripts/scope_publisher.py, shipped under
	// /opt/defaults/scripts like the rest of the platform scripts).
	scopePublisherPython = "/opt/hermes/.venv/bin/python3"
	scopePublisherScript = "/opt/defaults/scripts/scope_publisher.py"

	// The publisher container's env, read by scope_publisher.py.
	scopePublisherConfigMapEnvVar       = "KUBEAGENTS_RESOLVED_SCOPE_CONFIGMAP"
	scopePublisherNamespaceEnvVar       = "POD_NAMESPACE"
	scopePublisherBrokerURLEnvVar       = "CREDENTIAL_PROXY_URL"
	scopePublisherBrokerTokenFileEnvVar = "CREDENTIAL_PROXY_TOKEN_FILE" // #nosec G101 -- Environment variable name, not a credential
	// The install's own project, spec.harness.projectId, which the
	// publisher counts against maxProjects as the reconcile does and never
	// probes.
	scopePublisherManagementProjectEnvVar = "KUBEAGENTS_MANAGEMENT_PROJECT"

	// The publisher's broker token: projected for an audience of its own,
	// which the broker confers the `scope` role by, one hour like every
	// broker token.
	credentialProxyScopeAudience        = "kubeagents-credential-proxy-scope"            // #nosec G101 -- Token audience name, not a credential
	scopePublisherBrokerTokenVolume     = "scope-broker-token"                           // #nosec G101 -- Volume name, not a credential
	scopePublisherBrokerTokenMountDir   = "/var/run/secrets/kubeagents/credential-proxy" // #nosec G101 -- Mount path, not a credential
	scopePublisherBrokerTokenKey        = "token"                                        // #nosec G101 -- Projected file name, not a credential
	scopePublisherBrokerTokenTTLSeconds = 3600

	// The broker's side: the audience it confers the scope
	// role by, the publisher's username bound to it both ways, and where it
	// reads the declaration that bounds every scope argument.
	credentialProxyScopeAudienceEnvVar = "CREDENTIAL_PROXY_SCOPE_AUDIENCE" // #nosec G101 -- Environment variable name, not a credential
	credentialProxyScopeCallersEnvVar  = "CREDENTIAL_PROXY_SCOPE_CALLERS"  // #nosec G101 -- Environment variable name, not a credential
	credentialProxyScopeFileEnvVar     = "CREDENTIAL_PROXY_SCOPE_FILE"     // #nosec G101 -- Environment variable name, not a credential
	// brokerScopeDir is where the broker mounts the declaration: a directory
	// of its own beside forgeCADir rather than scopeDir, because the forge CA
	// Secrets mount under scopeDir in the broker, and the runtime cannot
	// create a mount point inside a read-only ConfigMap volume.
	brokerScopeDir = "/etc/kube-agents/scope"

	// The publisher container's requests and limits, as every next-stack
	// pod has them (a2aNATSCPURequest says why). Sized from the script's
	// measured use: about 180Mi at its usual peak and about 250Mi with four
	// lookups near the broker's output cap; the CPU is mostly waiting on
	// the broker.
	scopePublisherCPURequest    = "50m"
	scopePublisherMemoryRequest = "192Mi"
	scopePublisherCPULimit      = "500m"
	scopePublisherMemoryLimit   = "512Mi"

	// The API server's address and ports for the publisher's egress, as the
	// agent's own policy allows them (buildNetworkPolicy): the in-cluster
	// Service VIP when nothing was resolved, and the three ports a control
	// plane serves on.
	defaultAPIServerClusterIP = "10.96.0.1"
	apiServerHTTPSPort        = int32(443)
	apiServerAltPort          = int32(6443)
	apiServerProxyPort        = int32(8443)

	// a2aResolvedScopeConfigMapEnvVar tells the gateway which ConfigMap to
	// read (its get rule on that one name is in buildA2AGatewayRole).
	a2aResolvedScopeConfigMapEnvVar = "A2A_RESOLVED_SCOPE_CONFIGMAP"

	// a2aGatewayPodPolicyEnvVar is set on the operator by the chart, from the
	// same helper that renders the kube-agents-a2a-gateway-pod admission
	// policy. The broker's scope role is conferred only while
	// it is "true": without the policy, the A2A gateway can create a pod
	// under any ServiceAccount in the namespace, the publisher's included,
	// and so could mint the scope audience. Absent, empty or anything else
	// is off, so the kustomize path and clusters too old for the policy keep
	// the role unconferred. It says the chart rendered the policy, not that
	// the API server still enforces it: the same trust as the agent-RBAC
	// policies.
	a2aGatewayPodPolicyEnvVar = "A2A_GATEWAY_POD_POLICY"

	// The schedule: a run every 15 minutes, measured from the
	// newest publisher Job's creation, and one at once when memberHash
	// changes.
	scopePublishInterval = 15 * time.Minute
	// The listing budget the reconcile allows per 100 declared projects
	// (cluster_agent_reconcile.py); the Job's deadline and the freshness
	// bound are both built from it.
	scopeListingBudgetPerBlock  = 300 * time.Second
	scopeProjectsPerBlock       = 100
	scopePublisherDeadlineSlack = 120 * time.Second
	// scopeActiveJobRecheck is the soonest the reconcile looks again at an
	// active Job past its deadline, which the Job controller has yet to
	// mark Failed.
	scopeActiveJobRecheck       = 30 * time.Second
	scopePublisherJobTTLSeconds = 3600
	// scopeJobSuffixLength is the hex characters of the Job name's suffix.
	scopeJobSuffixLength = 10
	// scopeMemberHashAnnotation records on each Job the memberHash it ran
	// for, so a changed scope is told from the newest Job's annotation.
	scopeMemberHashAnnotation = "kubeagents.x-k8s.io/scope-member-hash"

	// The document scope_publisher.py writes, and the condition read off it.
	scopeDocumentVersion        = 1
	scopeStateOK                = "ok"
	scopeStateTooLarge          = "too-large"
	scopeStateError             = "error"
	scopeMemberHashPrefix       = "sha256:"
	scopeResolvedConditionType  = "ScopeResolved"
	scopeResolvedReasonResolved = "Resolved"
	scopeResolvedReasonMissing  = "Missing"
	scopeResolvedReasonStale    = "Stale"
	scopeResolvedReasonMismatch = "HashMismatch"
	scopeResolvedReasonTooLarge = "TooLarge"
	scopeResolvedReasonError    = "Error"
	// The two reasons no publisher Job runs for: the publisher's
	// ServiceAccount name is held by somebody else, or the broker does not
	// confer the scope role (scopeBrokerRoleEnabled).
	scopeResolvedReasonUnowned      = "UnownedServiceAccount"
	scopeResolvedReasonNotConferred = "ScopeRoleNotConferred"
	// scopeDocumentQuoteLimit bounds a document value the condition message
	// quotes: the publisher's ConfigMap is not trusted to keep the message
	// under the CRD's limit, and a status write over it fails whole.
	scopeDocumentQuoteLimit = 64
	// scopeClockSkewAllowance is how far past the operator's clock a
	// resolvedAt may be and still read as fresh. Beyond it the document is
	// not fresh: a stamp in the future would hold Resolved past the bound.
	scopeClockSkewAllowance = time.Minute
)

// scopeResolvedConfigMapName is the resolved-scope ConfigMap's name.
func scopeResolvedConfigMapName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + scopeResolvedConfigMapSuffix
}

// scopePublisherName names the publisher's ServiceAccount, Role,
// RoleBinding and NetworkPolicy.
func scopePublisherName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + scopePublisherSuffix
}

// scopePublisherBrokerCaller is the publisher ServiceAccount's TokenReview
// username, as the broker sees it.
func scopePublisherBrokerCaller(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("system:serviceaccount:%s:%s", agent.Namespace, scopePublisherName(agent))
}

// scopePublisherRendered is whether the publisher's objects render: mode next.
func scopePublisherRendered(agent *agentv1alpha1.PlatformAgent) bool {
	return renderMode(agent, "a2a-scope-publisher") == ModeNext
}

// scopeBrokerRoleEnabled is whether the broker confers the scope role: mode
// next and the gateway pod admission policy rendered
// (a2aGatewayPodPolicyEnvVar).
func scopeBrokerRoleEnabled(agent *agentv1alpha1.PlatformAgent) bool {
	return scopePublisherRendered(agent) && os.Getenv(a2aGatewayPodPolicyEnvVar) == "true"
}

// scopePublisherMayRun is whether a publisher Job may run, and so whether
// its identity and patch grant render: the scope declares something to
// resolve, the publisher's ServiceAccount is this PlatformAgent's (owned),
// and the broker confers the scope role.
func scopePublisherMayRun(agent *agentv1alpha1.PlatformAgent, owned bool) bool {
	return scopeHasResolvableMembers(agent) && owned && scopeBrokerRoleEnabled(agent)
}

// scopeHasResolvableMembers is whether spec.scope declares anything the
// publisher resolves: a folder, an organization, a Shared VPC host or a
// Metrics Scope. A scope of explicit projects alone, or no scope block, has
// nothing to resolve: no Job runs and no condition is set,
// and the gateway admits from scope.json directly.
func scopeHasResolvableMembers(agent *agentv1alpha1.PlatformAgent) bool {
	scope := agent.Spec.Scope
	if scope == nil {
		return false
	}
	return len(scope.Folders)+len(scope.Organizations)+len(scope.SharedVpcHosts)+len(scope.MetricsScopes) > 0
}

// scopeMaxProjects is the cap scope.json carries: spec.scope.maxProjects, or
// the default renderScopeJSON writes.
func scopeMaxProjects(agent *agentv1alpha1.PlatformAgent) int32 {
	if agent.Spec.Scope != nil && agent.Spec.Scope.MaxProjects != nil {
		return *agent.Spec.Scope.MaxProjects
	}
	return defaultScopeMaxProjects
}

// scopeListingBudget is the reconcile's listing budget for the cap: 300 s
// per started block of 100 projects.
func scopeListingBudget(maxProjects int32) time.Duration {
	blocks := (int64(maxProjects) + scopeProjectsPerBlock - 1) / scopeProjectsPerBlock
	if blocks < 1 {
		blocks = 1
	}
	return time.Duration(blocks) * scopeListingBudgetPerBlock
}

// scopeFreshnessBound is how old a document may be before it reads as
// stale: twice the interval plus the listing budget, 40 minutes at
// the default cap.
func scopeFreshnessBound(maxProjects int32) time.Duration {
	return 2 * (scopePublishInterval + scopeListingBudget(maxProjects))
}

// scopeMemberSet is the canonical object memberHash is taken over, the same
// bytes scope_publisher.py hashes. Field order is the sorted key order,
// which encoding/json keeps.
type scopeMemberSet struct {
	Exclude        scopeMemberExclude `json:"exclude"`
	Folders        []string           `json:"folders"`
	MaxProjects    int32              `json:"maxProjects"`
	MetricsScopes  []string           `json:"metricsScopes"`
	Organizations  []string           `json:"organizations"`
	Projects       []string           `json:"projects"`
	SharedVpcHosts []string           `json:"sharedVpcHosts"`
}

type scopeMemberExclude struct {
	Projects []string `json:"projects"`
}

// canonicalScopeList is a list as the hash takes it: sorted, duplicates
// removed, and never nil, so a missing list encodes as [].
func canonicalScopeList(values []string) []string {
	out := append([]string{}, values...)
	sort.Strings(out)
	return slices.Compact(out)
}

// scopeMemberHash is memberHash for the agent's declaration: "sha256:" and
// the hex digest of the canonical JSON of its membership fields. The
// publisher (Python) computes the same bytes from scope.json; the shared test
// vector pins the two together. exclude.clusters is not in it: the gateway
// applies it from scope.json directly, so editing it never invalidates the
// document.
func scopeMemberHash(agent *agentv1alpha1.PlatformAgent) string {
	sum := sha256.Sum256(scopeMemberCanonical(agent))
	return scopeMemberHashPrefix + hex.EncodeToString(sum[:])
}

// scopeMemberCanonical is the canonical JSON memberHash is taken over:
// compact, keys sorted, each list sorted and de-duplicated, maxProjects at
// its default when unset, and exclude.clusters left out.
func scopeMemberCanonical(agent *agentv1alpha1.PlatformAgent) []byte {
	scope := agent.Spec.Scope
	if scope == nil {
		scope = &agentv1alpha1.ScopeSpec{}
	}
	set := scopeMemberSet{
		Folders:        canonicalScopeList(scope.Folders),
		MaxProjects:    scopeMaxProjects(agent),
		MetricsScopes:  canonicalScopeList(scope.MetricsScopes),
		Organizations:  canonicalScopeList(scope.Organizations),
		Projects:       canonicalScopeList(scope.Projects),
		SharedVpcHosts: canonicalScopeList(scope.SharedVpcHosts),
		Exclude:        scopeMemberExclude{Projects: canonicalScopeList(nil)},
	}
	if scope.Exclude != nil {
		set.Exclude.Projects = canonicalScopeList(scope.Exclude.Projects)
	}
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	// Python's json.dumps leaves <, > and & alone; encoding/json escapes them
	// unless told not to, and the two must agree byte for byte.
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(set); err != nil {
		// A struct of strings and an int cannot fail to encode.
		panic(fmt.Sprintf("scope member set did not encode: %v", err))
	}
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n"))
}

// buildResolvedScopeConfigMap is the ConfigMap with no data: the publisher
// owns the key, the operator the object.
func buildResolvedScopeConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      scopeResolvedConfigMapName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, scopePublisherComponent),
		},
	}
}

// buildScopePublisherServiceAccount is the publisher's identity. Nothing is
// mounted from it automatically: the publisher's pod projects its API token
// and the cluster CA explicitly, and its broker token for the scope audience.
func buildScopePublisherServiceAccount(agent *agentv1alpha1.PlatformAgent) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      scopePublisherName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, scopePublisherComponent),
		},
		AutomountServiceAccountToken: ptr.To(false),
	}
}

// buildScopePublisherRole is the publisher's one grant: read and merge-patch
// the resolved-scope ConfigMap, by name. No list, no create, no other object.
func buildScopePublisherRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.Role {
	return &rbacv1.Role{
		TypeMeta: metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "Role"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      scopePublisherName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, scopePublisherComponent),
		},
		Rules: []rbacv1.PolicyRule{{
			APIGroups:     []string{""},
			Resources:     []string{"configmaps"},
			ResourceNames: []string{scopeResolvedConfigMapName(agent)},
			Verbs:         []string{"get", "patch"},
		}},
	}
}

func buildScopePublisherRoleBinding(agent *agentv1alpha1.PlatformAgent) *rbacv1.RoleBinding {
	return &rbacv1.RoleBinding{
		TypeMeta: metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "RoleBinding"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      scopePublisherName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, scopePublisherComponent),
		},
		RoleRef: rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "Role", Name: scopePublisherName(agent)},
		Subjects: []rbacv1.Subject{{
			Kind:      rbacv1.ServiceAccountKind,
			Name:      scopePublisherName(agent),
			Namespace: agent.Namespace,
		}},
	}
}

// scopePublisherPodLabels is what the Job's pods carry and what the
// publisher's NetworkPolicy and the broker's ingress fence select.
func scopePublisherPodLabels(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return a2aLabels(agent, scopePublisherComponent)
}

// buildScopePublisherNetworkPolicy fences the publisher's egress to the three
// things it talks to: cluster DNS, the credential broker's port, and the API
// server (for the merge patch), on the ports the agent's own policy allows
// for it. No ingress: nothing calls the publisher. apiCIDRs and dnsIPs are
// what the reconcile resolved; with none, the same defaults the agent's
// policy falls back to.
func buildScopePublisherNetworkPolicy(agent *agentv1alpha1.PlatformAgent, apiCIDRs, dnsIPs []string) *networkingv1.NetworkPolicy {
	if len(dnsIPs) == 0 {
		dnsIPs = []string{defaultDNSClusterIP}
	}
	apiPeers := formatCIDRPeers(apiCIDRs, true)
	if len(apiPeers) == 0 {
		apiPeers = formatCIDRPeers([]string{defaultAPIServerClusterIP}, true)
	}
	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      scopePublisherName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, scopePublisherComponent),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: scopePublisherPodLabels(agent)},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress, networkingv1.PolicyTypeEgress},
			Egress: []networkingv1.NetworkPolicyEgressRule{
				{
					Ports: []networkingv1.NetworkPolicyPort{udpPort(a2aDNSPort), tcpPort(a2aDNSPort)},
					To:    clusterDNSPeers(dnsIPs),
				},
				{
					Ports: []networkingv1.NetworkPolicyPort{tcpPort(credentialProxyPort)},
					To:    []networkingv1.NetworkPolicyPeer{namespacedPodPeer(agent.Namespace, credentialProxySelector(agent))},
				},
				{
					Ports: []networkingv1.NetworkPolicyPort{
						tcpPort(apiServerHTTPSPort), tcpPort(apiServerAltPort), tcpPort(apiServerProxyPort),
					},
					To: apiPeers,
				},
			},
		},
	}
}

// scopeConfigVolume projects only scope.json from the agent's config
// ConfigMap, at scopeDir, as the agent container's scope volume does. A
// directory mount, not a subPath, so a scope edit reaches the file in place.
// Optional for the same reason as the agent's: a ConfigMap an older operator
// wrote, without the key, mounts as an empty directory.
func scopeConfigVolume(agent *agentv1alpha1.PlatformAgent) corev1.Volume {
	return corev1.Volume{
		Name: scopeVolumeName,
		VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: agent.Name + "-config"},
			Items:                []corev1.KeyToPath{{Key: scopeConfigKey, Path: scopeFileName}},
			Optional:             ptr.To(true),
			DefaultMode:          ptr.To(int32(0444)),
		}},
	}
}

// scopeConfigMount mounts scopeConfigVolume at dir: scopeDir in the
// publisher, brokerScopeDir in the broker.
func scopeConfigMount(dir string) corev1.VolumeMount {
	return corev1.VolumeMount{Name: scopeVolumeName, MountPath: dir, ReadOnly: true}
}

// scopePublisherJobName is the Job's name for a run started at start for
// memberHash: a short hash of the agent, the start and the hash, so names
// never repeat. The Job controller copies the name into a pod label value,
// which holds at most 63 characters, so a long agent name shortens the
// prefix and the hash keeps two such agents apart.
func scopePublisherJobName(agent *agentv1alpha1.PlatformAgent, start time.Time, memberHash string) string {
	sum := sha256.Sum256([]byte(agent.Name + "|" + start.UTC().Format(time.RFC3339Nano) + "|" + memberHash))
	suffix := hex.EncodeToString(sum[:])[:scopeJobSuffixLength]
	prefix := scopePublisherName(agent)
	if limit := validation.DNS1123LabelMaxLength - len(suffix) - 1; len(prefix) > limit {
		prefix = strings.TrimRight(prefix[:limit], "-")
	}
	return prefix + "-" + suffix
}

// buildScopePublisherJob is one publisher run: the agent
// image running scope_publisher.py as the publisher's ServiceAccount, with
// the declaration mounted, the broker token projected for the scope
// audience, and no retry (the next run is the retry).
func buildScopePublisherJob(agent *agentv1alpha1.PlatformAgent, start time.Time, memberHash string) *batchv1.Job {
	deadline := scopeListingBudget(scopeMaxProjects(agent)) + scopePublisherDeadlineSlack
	labels := scopePublisherPodLabels(agent)
	return &batchv1.Job{
		TypeMeta: metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"},
		ObjectMeta: metav1.ObjectMeta{
			Name:        scopePublisherJobName(agent, start, memberHash),
			Namespace:   agent.Namespace,
			Labels:      labels,
			Annotations: map[string]string{scopeMemberHashAnnotation: memberHash},
		},
		Spec: batchv1.JobSpec{
			BackoffLimit:            ptr.To(int32(0)),
			ActiveDeadlineSeconds:   ptr.To(int64(deadline / time.Second)),
			TTLSecondsAfterFinished: ptr.To(int32(scopePublisherJobTTLSeconds)),
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{
					RestartPolicy:                corev1.RestartPolicyNever,
					ServiceAccountName:           scopePublisherName(agent),
					AutomountServiceAccountToken: ptr.To(false),
					// The agent image's UID and group, as the broker runs it:
					// the image names its user, which RunAsNonRoot cannot
					// check, and the group makes the 0400 projected tokens
					// readable.
					SecurityContext: &corev1.PodSecurityContext{
						FSGroup:        ptr.To(agentFSGroup),
						RunAsUser:      ptr.To(sandboxUID),
						RunAsNonRoot:   ptr.To(true),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					// The agent image's pull identity, as the broker's.
					ImagePullSecrets: resolveImagePullSecrets(agent.Spec.Deployment),
					Volumes: []corev1.Volume{
						scopeConfigVolume(agent),
						// The default-audience token and the cluster CA at the
						// in-cluster path, for the merge patch, as the broker
						// projects them for its TokenReview call.
						buildEventWatcherTokenVolume(),
						{
							Name: scopePublisherBrokerTokenVolume,
							VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
								DefaultMode: ptr.To(int32(0400)),
								Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
									Audience:          credentialProxyScopeAudience,
									ExpirationSeconds: ptr.To(int64(scopePublisherBrokerTokenTTLSeconds)),
									Path:              scopePublisherBrokerTokenKey,
								}}},
							}},
						},
					},
					Containers: []corev1.Container{{
						Name:            scopePublisherContainer,
						Image:           resolveAgentImage(agent.Spec.Deployment, defaultPlatformAgentImage()),
						ImagePullPolicy: scopePublisherPullPolicy(agent),
						Command:         []string{scopePublisherPython, scopePublisherScript},
						Env: []corev1.EnvVar{
							{Name: scopeFileEnvKey, Value: path.Join(scopeDir, scopeFileName)},
							{Name: scopePublisherConfigMapEnvVar, Value: scopeResolvedConfigMapName(agent)},
							{Name: scopePublisherNamespaceEnvVar, ValueFrom: &corev1.EnvVarSource{
								FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.namespace"},
							}},
							{Name: scopePublisherBrokerURLEnvVar, Value: credentialProxyBaseURL(agent)},
							{Name: scopePublisherBrokerTokenFileEnvVar, Value: path.Join(scopePublisherBrokerTokenMountDir, scopePublisherBrokerTokenKey)},
							{Name: scopePublisherManagementProjectEnvVar, Value: scopeManagementProject(agent)},
						},
						VolumeMounts: []corev1.VolumeMount{
							scopeConfigMount(scopeDir),
							{Name: buildEventWatcherTokenVolume().Name, MountPath: kubeAPIAccessMountPath, ReadOnly: true},
							{Name: scopePublisherBrokerTokenVolume, MountPath: scopePublisherBrokerTokenMountDir, ReadOnly: true},
						},
						SecurityContext: hardenedSecurityContext(),
						Resources: a2aResources(scopePublisherCPURequest, scopePublisherMemoryRequest,
							scopePublisherCPULimit, scopePublisherMemoryLimit),
					}},
				},
			},
		},
	}
}

// scopeManagementProject is spec.harness.projectId, empty without a harness
// block, the value the gateway gets as its management project.
func scopeManagementProject(agent *agentv1alpha1.PlatformAgent) string {
	if agent.Spec.Harness == nil {
		return ""
	}
	return agent.Spec.Harness.ProjectID
}

// scopePublisherPullPolicy is the agent image's pull policy, as the broker
// takes it: spec.deployment.imagePullPolicy, else Always.
func scopePublisherPullPolicy(agent *agentv1alpha1.PlatformAgent) corev1.PullPolicy {
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.ImagePullPolicy != nil {
		return *agent.Spec.Deployment.ImagePullPolicy
	}
	return corev1.PullAlways
}

// scopePublishDecision is what the schedule decides for one pass: whether to
// start a Job now, and how long until the next look.
type scopePublishDecision struct {
	start   bool
	requeue time.Duration
}

// scopeJobActive is whether a Job is still running: neither its Complete nor
// its Failed condition is true.
func scopeJobActive(job *batchv1.Job) bool {
	for _, cond := range job.Status.Conditions {
		if cond.Status == corev1.ConditionTrue && (cond.Type == batchv1.JobComplete || cond.Type == batchv1.JobFailed) {
			return false
		}
	}
	return true
}

// decideScopePublish is the schedule over the publisher Jobs
// that exist: none while one is active; one when there is none, when the
// newest ran for another memberHash, or when the newest is 15 minutes old.
// The interval is measured from the newest Job's creation, read from the
// cluster, so an operator restart neither skips nor doubles a run. While a
// Job is active the reconcile looks again at its deadline: a run that ends
// by writing the ConfigMap wakes it sooner (ConfigMap is an Owns kind), and
// one killed at the deadline writes nothing, and Jobs are not watched.
func decideScopePublish(jobs []batchv1.Job, memberHash string, now time.Time) scopePublishDecision {
	var newest *batchv1.Job
	for i := range jobs {
		job := &jobs[i]
		if scopeJobActive(job) {
			requeue := scopePublishInterval
			if job.Spec.ActiveDeadlineSeconds != nil {
				deadline := job.CreationTimestamp.Add(time.Duration(*job.Spec.ActiveDeadlineSeconds) * time.Second)
				requeue = min(max(deadline.Sub(now), scopeActiveJobRecheck), scopePublishInterval)
			}
			return scopePublishDecision{requeue: requeue}
		}
		if newest == nil || job.CreationTimestamp.After(newest.CreationTimestamp.Time) {
			newest = job
		}
	}
	if newest == nil || newest.Annotations[scopeMemberHashAnnotation] != memberHash {
		return scopePublishDecision{start: true, requeue: scopePublishInterval}
	}
	elapsed := now.Sub(newest.CreationTimestamp.Time)
	if elapsed >= scopePublishInterval {
		return scopePublishDecision{start: true, requeue: scopePublishInterval}
	}
	return scopePublishDecision{requeue: scopePublishInterval - elapsed}
}

// resolvedScopeDocument is the part of the document the condition reads.
type resolvedScopeDocument struct {
	V          int    `json:"v"`
	ResolvedAt string `json:"resolvedAt"`
	MemberHash string `json:"memberHash"`
	State      string `json:"state"`
}

// scopeResolvedCondition is the ScopeResolved condition for the document in
// data (the ConfigMap's data, nil when the ConfigMap is absent), and how long
// until it would change without any event: until resolvedAt + bound for a
// fresh document, zero otherwise.
func scopeResolvedCondition(agent *agentv1alpha1.PlatformAgent, data map[string]string, now time.Time) (metav1.Condition, time.Duration) {
	condition := metav1.Condition{
		Type:               scopeResolvedConditionType,
		Status:             metav1.ConditionFalse,
		ObservedGeneration: agent.Generation,
	}
	raw, ok := data[scopeResolvedDataKey]
	var doc resolvedScopeDocument
	if !ok || json.Unmarshal([]byte(raw), &doc) != nil || doc.V != scopeDocumentVersion {
		condition.Reason = scopeResolvedReasonMissing
		condition.Message = "the scope publisher has not written a document this operator can read yet"
		return condition, 0
	}
	resolvedAt, err := time.Parse(time.RFC3339, doc.ResolvedAt)
	if err != nil {
		condition.Reason = scopeResolvedReasonMissing
		condition.Message = fmt.Sprintf("the resolved scope's resolvedAt %q is not an RFC 3339 time", truncateToValidUTF8(doc.ResolvedAt, scopeDocumentQuoteLimit))
		return condition, 0
	}
	bound := scopeFreshnessBound(scopeMaxProjects(agent))
	// The parsed time, rendered again: RFC 3339 admits any number of
	// fractional-second digits, so doc.ResolvedAt itself has no length bound.
	stamp := resolvedAt.UTC().Format(time.RFC3339)
	switch {
	case resolvedAt.After(now.Add(scopeClockSkewAllowance)):
		condition.Reason = scopeResolvedReasonMissing
		condition.Message = fmt.Sprintf("the resolved scope's resolvedAt %s is later than the operator's clock, so it is not a fresh document", stamp)
	case doc.State == scopeStateTooLarge:
		condition.Reason = scopeResolvedReasonTooLarge
		condition.Message = fmt.Sprintf("the resolved scope at %s was larger than the ConfigMap allows, so it lists no project", stamp)
	case doc.State == scopeStateError:
		condition.Reason = scopeResolvedReasonError
		condition.Message = fmt.Sprintf("the scope publisher could not resolve the scope (run at %s); see the publisher Job's log", stamp)
	case doc.State != scopeStateOK:
		condition.Reason = scopeResolvedReasonMissing
		condition.Message = fmt.Sprintf("the resolved scope at %s has a state this operator does not know (%q)", stamp, truncateToValidUTF8(doc.State, scopeDocumentQuoteLimit))
	case doc.MemberHash != scopeMemberHash(agent):
		condition.Reason = scopeResolvedReasonMismatch
		condition.Message = fmt.Sprintf("the resolved scope at %s is for an earlier spec.scope; a run for it starts when no run is active: at once if none has run for it yet, else within 15 minutes of the last start", stamp)
	case now.Sub(resolvedAt) > bound:
		condition.Reason = scopeResolvedReasonStale
		condition.Message = fmt.Sprintf("the resolved scope was written at %s, longer ago than the %s bound; the scope publisher is not keeping it fresh", stamp, bound)
	default:
		condition.Status = metav1.ConditionTrue
		condition.Reason = scopeResolvedReasonResolved
		condition.Message = fmt.Sprintf("resolved at %s", stamp)
		return condition, resolvedAt.Add(bound).Sub(now)
	}
	return condition, 0
}

// scopeNotRunCondition is the ScopeResolved condition when no publisher Job
// may run: False, with the reason and message that say why.
func scopeNotRunCondition(agent *agentv1alpha1.PlatformAgent, reason, message string) *metav1.Condition {
	return &metav1.Condition{
		Type:               scopeResolvedConditionType,
		Status:             metav1.ConditionFalse,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: agent.Generation,
	}
}

// scopePublisherIdentityOwned is whether the publisher's ServiceAccount is
// this PlatformAgent's to use: absent (this pass creates it, owned) or
// controlled by it. An account under the reserved name that somebody else
// holds is never adopted or admitted by the broker, as the MCP server's is
// not (a2aMCPIdentityOwned). One cached read: ServiceAccount is an Owns() kind.
func (r *PlatformAgentReconciler) scopePublisherIdentityOwned(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (bool, error) {
	if !scopePublisherRendered(agent) {
		return true, nil
	}
	existing := &corev1.ServiceAccount{}
	err := r.Get(ctx, types.NamespacedName{Name: scopePublisherName(agent), Namespace: agent.Namespace}, existing)
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	if err != nil {
		return false, fmt.Errorf("failed to read the scope publisher's ServiceAccount: %w", err)
	}
	return metav1.IsControlledBy(existing, agent), nil
}

// scopeBrokerEnv is the broker's side of the publisher. Under mode next the
// declaration's path, which the scope role reads to bound every argument;
// while a publisher may run (scopePublisherMayRun), the audience and the
// publisher's username bound to it both ways, as a2aMCPBrokerEnv binds the mcp role. Without the
// pair the broker confers the scope role to nobody.
func scopeBrokerEnv(agent *agentv1alpha1.PlatformAgent, owned bool) []corev1.EnvVar {
	if !scopePublisherRendered(agent) {
		return nil
	}
	env := []corev1.EnvVar{{Name: credentialProxyScopeFileEnvVar, Value: path.Join(brokerScopeDir, scopeFileName)}}
	if scopePublisherMayRun(agent, owned) {
		env = append(env,
			corev1.EnvVar{Name: credentialProxyScopeAudienceEnvVar, Value: credentialProxyScopeAudience},
			corev1.EnvVar{Name: credentialProxyScopeCallersEnvVar, Value: scopePublisherBrokerCaller(agent)},
		)
	}
	return env
}

// scopeBrokerPeers is the publisher's pods as a peer of the broker's ingress
// fence, on the credentialed port, while the role is conferred and the scope
// declares something to resolve; nothing otherwise, so the fence admits
// exactly what it did. Ownership is not read here: the caller list above
// refuses an account this PlatformAgent does not hold.
func scopeBrokerPeers(agent *agentv1alpha1.PlatformAgent) []networkingv1.NetworkPolicyPeer {
	if !scopeBrokerRoleEnabled(agent) || !scopeHasResolvableMembers(agent) {
		return nil
	}
	return []networkingv1.NetworkPolicyPeer{{PodSelector: &metav1.LabelSelector{MatchLabels: scopePublisherPodLabels(agent)}}}
}

// scopePublisherOutcome is what reconcileScopePublisher leaves for the status
// writers and the requeue: the condition (nil when none is set) and the time
// until the next look (zero when nothing is scheduled).
type scopePublisherOutcome struct {
	condition *metav1.Condition
	requeue   time.Duration
}

// reconcileScopePublisher applies the ConfigMap, applies the publisher's
// identity while a Job may run and removes it otherwise, starts a publisher
// Job when the schedule says so, and reads the ConfigMap for the condition. reconcileA2AStack runs it under next only; cleanupA2A removes
// what it rendered.
func (r *PlatformAgentReconciler) reconcileScopePublisher(ctx context.Context, agent *agentv1alpha1.PlatformAgent, now time.Time) (scopePublisherOutcome, error) {
	owned, err := r.scopePublisherIdentityOwned(ctx, agent)
	if err != nil {
		return scopePublisherOutcome{}, err
	}
	mayRun := scopePublisherMayRun(agent, owned)
	objects := []client.Object{buildResolvedScopeConfigMap(agent)}
	if mayRun {
		apiCIDRs := r.apiServerEgressTargets(ctx, agent, false)
		objects = append(objects,
			buildScopePublisherServiceAccount(agent),
			buildScopePublisherRole(agent),
			buildScopePublisherRoleBinding(agent),
			buildScopePublisherNetworkPolicy(agent, apiCIDRs, r.ungatedDNSClusterIPs(ctx, agent)),
		)
	} else {
		if !owned {
			logf.FromContext(ctx).Info("not adopting an unowned ServiceAccount as the scope publisher's identity; no publisher Job runs",
				"namespace", agent.Namespace, "name", scopePublisherName(agent))
		}
		// A grant an earlier pass rendered goes, including one bound by
		// name to an account somebody else now holds.
		if err := r.removeScopePublisherIdentity(ctx, agent); err != nil {
			return scopePublisherOutcome{}, err
		}
	}
	for _, obj := range objects {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return scopePublisherOutcome{}, err
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return scopePublisherOutcome{}, fmt.Errorf("failed to apply scope publisher %T: %w", obj, err)
		}
	}
	if !scopeHasResolvableMembers(agent) {
		return scopePublisherOutcome{}, nil
	}
	// No Job where the broker would refuse it, and the condition says
	// which of the two stops it rather than reading Missing for ever.
	if !owned {
		return scopePublisherOutcome{condition: scopeNotRunCondition(agent, scopeResolvedReasonUnowned,
			fmt.Sprintf("a ServiceAccount named %s exists that this PlatformAgent does not control, so no scope publisher runs; delete it", scopePublisherName(agent)))}, nil
	}
	if !scopeBrokerRoleEnabled(agent) {
		return scopePublisherOutcome{condition: scopeNotRunCondition(agent, scopeResolvedReasonNotConferred,
			"the broker confers the scope role only on an install whose chart renders the kube-agents-a2a-gateway-pod admission policy, so no scope publisher runs")}, nil
	}

	var jobs batchv1.JobList
	if err := r.a2aReader().List(ctx, &jobs, client.InNamespace(agent.Namespace), client.MatchingLabels(scopePublisherPodLabels(agent))); err != nil {
		return scopePublisherOutcome{}, fmt.Errorf("failed to list scope publisher Jobs: %w", err)
	}
	mine := make([]batchv1.Job, 0, len(jobs.Items))
	for i := range jobs.Items {
		if metav1.IsControlledBy(&jobs.Items[i], agent) {
			mine = append(mine, jobs.Items[i])
		}
	}
	memberHash := scopeMemberHash(agent)
	decision := decideScopePublish(mine, memberHash, now)
	if decision.start {
		job := buildScopePublisherJob(agent, now, memberHash)
		if err := ctrl.SetControllerReference(agent, job, r.Scheme); err != nil {
			return scopePublisherOutcome{}, err
		}
		withCommonLabels(job, agent)
		if err := r.Create(ctx, job); err != nil && !apierrors.IsAlreadyExists(err) {
			return scopePublisherOutcome{}, fmt.Errorf("failed to create scope publisher Job: %w", err)
		}
	}
	outcome := scopePublisherOutcome{requeue: decision.requeue}

	cm := &corev1.ConfigMap{}
	var data map[string]string
	if err := r.Get(ctx, types.NamespacedName{Name: scopeResolvedConfigMapName(agent), Namespace: agent.Namespace}, cm); err == nil {
		data = cm.Data
	} else if !apierrors.IsNotFound(err) {
		return scopePublisherOutcome{}, fmt.Errorf("failed to read the resolved scope: %w", err)
	}
	condition, fresh := scopeResolvedCondition(agent, data, now)
	outcome.condition = &condition
	if fresh > 0 && fresh < outcome.requeue {
		outcome.requeue = fresh
	}
	return outcome, nil
}

// removeScopePublisher deletes what reconcileScopePublisher rendered, owned
// objects only: the Jobs first, the identity after them, the ConfigMap last.
func (r *PlatformAgentReconciler) removeScopePublisher(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	var jobs batchv1.JobList
	if err := r.a2aReader().List(ctx, &jobs, client.InNamespace(agent.Namespace), client.MatchingLabels(scopePublisherPodLabels(agent))); err != nil {
		return fmt.Errorf("failed to list scope publisher Jobs: %w", err)
	}
	for i := range jobs.Items {
		job := &jobs.Items[i]
		if job.DeletionTimestamp != nil || !metav1.IsControlledBy(job, agent) {
			continue
		}
		// Background, as deleteA2AProvisionJobs: a Job deleted with no
		// policy orphans its pod.
		if err := client.IgnoreNotFound(r.Delete(ctx, job, client.PropagationPolicy(metav1.DeletePropagationBackground))); err != nil {
			return fmt.Errorf("failed to delete scope publisher Job: %w", err)
		}
	}
	if err := r.removeScopePublisherIdentity(ctx, agent); err != nil {
		return err
	}
	if err := r.deleteA2AObjectIfOwned(ctx, agent, buildResolvedScopeConfigMap(agent), r.Client, nil); err != nil {
		return fmt.Errorf("failed to remove the resolved-scope ConfigMap: %w", err)
	}
	return nil
}

// removeScopePublisherIdentity deletes the publisher's NetworkPolicy,
// RoleBinding, Role and ServiceAccount, owned objects only.
func (r *PlatformAgentReconciler) removeScopePublisherIdentity(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	for _, obj := range []client.Object{
		buildScopePublisherNetworkPolicy(agent, nil, nil),
		buildScopePublisherRoleBinding(agent),
		buildScopePublisherRole(agent),
		buildScopePublisherServiceAccount(agent),
	} {
		if err := r.deleteA2AObjectIfOwned(ctx, agent, obj, r.Client, nil); err != nil {
			return fmt.Errorf("failed to remove scope publisher %T: %w", obj, err)
		}
	}
	return nil
}

// scopeResolvedConditionCurrent and setScopeResolvedCondition carry the
// ScopeResolved condition through both status writers, as the MCP server
// condition is carried: the condition reconcileScopePublisher computed, or
// none (a today install, or a scope with nothing to resolve).
func scopeResolvedConditionCurrent(agent *agentv1alpha1.PlatformAgent, want *metav1.Condition) bool {
	existing := meta.FindStatusCondition(agent.Status.Conditions, scopeResolvedConditionType)
	if want == nil {
		return existing == nil
	}
	return existing != nil && existing.Status == want.Status && existing.Reason == want.Reason &&
		existing.Message == want.Message && existing.ObservedGeneration == want.ObservedGeneration
}

func setScopeResolvedCondition(agent *agentv1alpha1.PlatformAgent, want *metav1.Condition, now metav1.Time) {
	if want == nil {
		meta.RemoveStatusCondition(&agent.Status.Conditions, scopeResolvedConditionType)
		return
	}
	condition := *want
	condition.LastTransitionTime = now
	meta.SetStatusCondition(&agent.Status.Conditions, condition)
}
