package controller

import (
	"context"
	"fmt"
	"reflect"
	"strings"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The profile namespace (#3655; #3660, "Execution"). Profile worker pods and
// their ServiceAccounts run in a namespace of their own, <agent-ns>-profiles,
// so profile work never takes a session slot: the session quota counts every
// pod in the agent's namespace. The chart creates the namespace, next to the
// release namespace, and the operator never does: creating and deleting
// Namespaces is a cluster-scoped grant that can't be narrowed by name on
// create, which is more than a controller should hold for one namespace it
// knows the name of. What the operator renders inside it (the quota and the
// network policy here, the identities and pods later) carries no owner
// reference, because one can't point across namespaces. Uninstalling the
// release deletes the namespace and everything in it.

// The markers the chart puts on the profile namespace, which the operator
// requires before it treats a namespace of that name as the profile
// namespace. Anyone who can create a Namespace could create one with this
// name first, and its pods would then be admitted to the bus and LiteLLM.
// The markers can be copied by such a creator, so they are not a guarantee:
// they stop a namespace that merely shares the name, not one made to pass.
// Helm won't adopt an existing namespace that lacks its release annotations,
// so a squatter who doesn't copy those surfaces as a failed install; one who
// does is someone with Namespace create, which is close to cluster admin. A
// kustomize or hand install sets the markers too.
const (
	profileNamespaceComponent      = "profiles"
	helmReleaseNamespaceAnnotation = "meta.helm.sh/release-namespace"
	profileNamespaceComponentLabel = "app.kubernetes.io/component"
)

// The component labels of what the operator renders into the profile
// namespace, the test renderedByAgent applies to objects that can't carry
// an owner reference.
const (
	profileQuotaComponent  = "profile-quota"
	profileNetpolComponent = "profile-netpol"
)

// The suffixes the profile namespace and the objects in it are named with.
const (
	profileNamespaceSuffix = "-profiles"
	profileQuotaSuffix     = "-profile-quota"
	profileNetpolSuffix    = "-profile-netpol"
)

// profileNamespaceName is the namespace profile work runs in.
func profileNamespaceName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Namespace + profileNamespaceSuffix
}

// profileNamespaceOf is the profile namespace for a profile: the one beside
// its own namespace, which is its agent's. It needs no agent, so a profile
// deleted after its agent still finds its ServiceAccounts.
func profileNamespaceOf(p *agentv1alpha1.AgentProfile) string {
	return p.Namespace + profileNamespaceSuffix
}

// agentNamespaceOfProfileServiceAccount is the namespace whose agent renders
// a profile ServiceAccount's map entry, the one beside a profile namespace.
// Any other namespace holds no ServiceAccount the map reads.
func agentNamespaceOfProfileServiceAccount(ns string) (string, bool) {
	agentNS, ok := strings.CutSuffix(ns, profileNamespaceSuffix)
	return agentNS, ok && agentNS != ""
}

// resolveProfileCeiling is spec.harness.tuning.profiles.ceiling, defaulted.
func resolveProfileCeiling(agent *agentv1alpha1.PlatformAgent) int {
	if t := agentTuning(agent); t != nil && t.Profiles != nil && t.Profiles.Ceiling != nil {
		return *t.Profiles.Ceiling
	}
	return agentv1alpha1.DefaultProfileCeiling
}

// resolveProfileWarmReplicas is spec.harness.tuning.profiles.warmReplicas,
// defaulted.
func resolveProfileWarmReplicas(agent *agentv1alpha1.PlatformAgent) int {
	if t := agentTuning(agent); t != nil && t.Profiles != nil && t.Profiles.WarmReplicas != nil {
		return *t.Profiles.WarmReplicas
	}
	return agentv1alpha1.DefaultProfileWarmReplicas
}

// interactiveProfileTiers counts the warm pools the profiles need: one per
// (profile, tier) whose tier is interactive. Read is the only interactive
// tier until tiers are CR data (#3659).
func interactiveProfileTiers(profiles []agentv1alpha1.AgentProfile) int {
	n := 0
	for i := range profiles {
		for _, tier := range profiles[i].AllowedTierList() {
			if tier == agentv1alpha1.AgentProfileTierRead {
				n++
			}
		}
	}
	return n
}

// profileQuotaHard is the profile namespace's quota, one entry per key.
//
// pods covers the three kinds of pod the namespace runs: the warm workers
// (warmReplicas for each interactive (profile, tier)), one rollout surge pod
// per warm pool, and the unattended tier's Job pods, which the dispatcher
// holds to the ceiling. Interactive tasks run in the warm workers, so they
// add no pods. Like the session quota, the key is `pods`, which counts
// non-terminal pods only. It is the only key for now; requests.cpu and
// requests.memory join the map once the sizing budget gives them values,
// and adding one is a line here.
func profileQuotaHard(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) map[corev1.ResourceName]int64 {
	pools := interactiveProfileTiers(profiles)
	return map[corev1.ResourceName]int64{
		corev1.ResourcePods: int64(pools*resolveProfileWarmReplicas(agent) + pools + resolveProfileCeiling(agent)),
	}
}

func profileQuotaName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + profileQuotaSuffix
}

func buildProfileQuota(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) *corev1.ResourceQuota {
	hard := corev1.ResourceList{}
	for key, n := range profileQuotaHard(agent, profiles) {
		hard[key] = *resource.NewQuantity(n, resource.DecimalSI)
	}
	return &corev1.ResourceQuota{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ResourceQuota"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      profileQuotaName(agent),
			Namespace: profileNamespaceName(agent),
			Labels:    a2aLabels(agent, profileQuotaComponent),
		},
		Spec: corev1.ResourceQuotaSpec{Hard: hard},
	}
}

func profileNetpolName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + profileNetpolSuffix
}

// buildProfileNetworkPolicy fences every pod in the profile namespace: no
// ingress, and egress to DNS, the bus, LiteLLM and the credential broker's
// credentialed port only. The broker is where a worker's kubectl and gcloud
// run (#3889); its side admits the profile namespace, and confines each call
// to the task's cluster, once a profile identity exists
// (credential_proxy_profiles.go). The egress is unconditional: a namespace
// that exists only for profiles needs no second gate, and the broker's
// TokenReview refuses any pod the identity map doesn't name.
func buildProfileNetworkPolicy(agent *agentv1alpha1.PlatformAgent, dnsClusterIPs []string) *networkingv1.NetworkPolicy {
	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      profileNetpolName(agent),
			Namespace: profileNamespaceName(agent),
			Labels:    a2aLabels(agent, profileNetpolComponent),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{},
			PolicyTypes: []networkingv1.PolicyType{
				networkingv1.PolicyTypeIngress,
				networkingv1.PolicyTypeEgress,
			},
			Egress: []networkingv1.NetworkPolicyEgressRule{
				{
					Ports: []networkingv1.NetworkPolicyPort{udpPort(a2aDNSPort), tcpPort(a2aDNSPort)},
					To:    clusterDNSPeers(dnsClusterIPs),
				},
				{
					Ports: []networkingv1.NetworkPolicyPort{tcpPort(a2aNATSClientPort)},
					To: []networkingv1.NetworkPolicyPeer{
						namespacedPodPeer(agent.Namespace, map[string]string{
							labelPartOf:       a2aPartOf,
							a2aComponentLabel: "nats",
						}),
					},
				},
				{
					Ports: []networkingv1.NetworkPolicyPort{
						tcpPort(a2aLiteLLMServicePort),
						tcpPort(a2aLiteLLMUpstreamPort),
						tcpPort(a2aLiteLLMContainerPort),
					},
					To: []networkingv1.NetworkPolicyPeer{
						namespacedPodPeer(agent.Namespace, map[string]string{"app": "litellm"}),
					},
				},
				{
					Ports: []networkingv1.NetworkPolicyPort{tcpPort(credentialProxyPort)},
					To: []networkingv1.NetworkPolicyPeer{
						namespacedPodPeer(agent.Namespace, credentialProxySelector(agent)),
					},
				},
			},
		},
	}
}

// profileNamespacePeer admits every pod in the profile namespace, for the
// ingress policies of what profile pods reach (the bus and LiteLLM). The
// profile namespace's own fence is what keeps those pods to that list.
func profileNamespacePeer(agent *agentv1alpha1.PlatformAgent) networkingv1.NetworkPolicyPeer {
	return networkingv1.NetworkPolicyPeer{
		NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
			corev1.LabelMetadataName:       profileNamespaceName(agent),
			profileNamespaceComponentLabel: profileNamespaceComponent,
		}},
	}
}

// profileNamespaceReady reads the profile namespace through reader, which
// both callers pass uncached: Namespace is a kind neither controller
// watches, and a cached Get would start a cluster-wide informer for it on
// every install. A namespace that doesn't exist is an install the chart
// didn't make it for (a CR applied by hand, or a chart from before #3655):
// profile work renders nothing there, and the operator never creates it.
func profileNamespaceReady(ctx context.Context, reader client.Reader, agent *agentv1alpha1.PlatformAgent) (bool, error) {
	var ns corev1.Namespace
	if err := reader.Get(ctx, types.NamespacedName{Name: profileNamespaceName(agent)}, &ns); err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, fmt.Errorf("read the profile namespace %s: %w", profileNamespaceName(agent), err)
	}
	// A namespace being deleted is absent: nothing can be created in it, and
	// the bus and LiteLLM shouldn't admit a namespace whose fence is going.
	if ns.DeletionTimestamp != nil {
		return false, nil
	}
	if ns.Labels[profileNamespaceComponentLabel] != profileNamespaceComponent || ns.Annotations[helmReleaseNamespaceAnnotation] != agent.Namespace {
		logf.FromContext(ctx).Info("a namespace has the profile namespace's name without the chart's markers; treating it as absent",
			"namespace", ns.Name, "want label", profileNamespaceComponentLabel+"="+profileNamespaceComponent,
			"want annotation", helmReleaseNamespaceAnnotation+"="+agent.Namespace)
		return false, nil
	}
	return true, nil
}

// profileNamespaceExists is profileNamespaceReady through the uncached reader.
func (r *PlatformAgentReconciler) profileNamespaceExists(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (bool, error) {
	return profileNamespaceReady(ctx, r.a2aReader(), agent)
}

// reconcileProfileFence applies the profile namespace's fence, and reports
// whether one is in place. reconcileA2ANetworkFences calls it ahead of the
// fences that admit the profile namespace, on the refusal path as well as
// the render path, and admits the namespace only when this returns true: a
// policy that lets profile pods reach the bus is never applied over pods
// their own fence doesn't yet confine.
func (r *PlatformAgentReconciler) reconcileProfileFence(ctx context.Context, agent *agentv1alpha1.PlatformAgent, dnsClusterIPs []string) (bool, error) {
	exists, err := r.profileNamespaceExists(ctx, agent)
	if err != nil || !exists {
		if err == nil {
			logf.FromContext(ctx).V(1).Info("profile namespace absent; the chart creates it, and profile work renders nothing until it exists",
				"namespace", profileNamespaceName(agent))
		}
		return false, err
	}
	if err := r.applyManaged(ctx, agent, buildProfileNetworkPolicy(agent, dnsClusterIPs)); err != nil {
		return false, fmt.Errorf("failed to apply the profile NetworkPolicy: %w", err)
	}
	return true, nil
}

// profileFenced reports whether the profile namespace's fence is in place,
// for a policy applied outside reconcileA2ANetworkFences (LiteLLM's): the
// namespace carries the chart's markers, and the fence there is this
// agent's. The same test reconcileProfileFence passes before it applies one.
func (r *PlatformAgentReconciler) profileFenced(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (bool, error) {
	if exists, err := r.profileNamespaceExists(ctx, agent); err != nil || !exists {
		return false, err
	}
	var np networkingv1.NetworkPolicy
	err := r.Get(ctx, types.NamespacedName{Namespace: profileNamespaceName(agent), Name: profileNetpolName(agent)}, &np)
	if apierrors.IsNotFound(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return renderedByAgent(&np, agent, profileNetpolComponent), nil
}

// renderedByAgent reports whether obj carries this agent's labels for the
// given component, the test for objects in the profile namespace, which
// can't carry an owner reference.
func renderedByAgent(obj client.Object, agent *agentv1alpha1.PlatformAgent, component string) bool {
	labels := obj.GetLabels()
	return labels[labelInstance] == instanceLabel(agent.Namespace, agent.Name) && labels[a2aComponentLabel] == component
}

// withoutProfilePeer drops the profile namespace from np's ingress, and any
// rule left with no peer, for a pass where the profile fence isn't in place.
func withoutProfilePeer(np *networkingv1.NetworkPolicy, agent *agentv1alpha1.PlatformAgent) {
	want := profileNamespacePeer(agent)
	var rules []networkingv1.NetworkPolicyIngressRule
	for _, rule := range np.Spec.Ingress {
		var from []networkingv1.NetworkPolicyPeer
		for _, peer := range rule.From {
			if !reflect.DeepEqual(peer, want) {
				from = append(from, peer)
			}
		}
		if len(rule.From) > 0 && len(from) == 0 {
			continue
		}
		rule.From = from
		rules = append(rules, rule)
	}
	np.Spec.Ingress = rules
}

// reconcileProfileQuota renders the quota into the profile namespace, when
// this pass fenced it (reconcileA2ANetworkFences' answer, so the quota adds
// no read of its own; LiteLLM's apply site reads the namespace once more). profiles is the list reconcileA2A already holds,
// under its agentProfilesUnreadable guard; nil sizes the quota for the
// ceiling alone.
func (r *PlatformAgentReconciler) reconcileProfileQuota(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile, fenced bool) error {
	if !fenced {
		return nil
	}
	if err := r.applyManaged(ctx, agent, buildProfileQuota(agent, profiles)); err != nil {
		return fmt.Errorf("failed to apply the profile ResourceQuota: %w", err)
	}
	return nil
}

// deleteProfileQuota and deleteProfileFence remove what this agent rendered
// into the profile namespace, for the flip back to today (deleting the
// PlatformAgent doesn't remove them yet: #3827). Those objects
// carry no owner reference, so nothing else reclaims them, and an install
// that isn't running next should look like it never did. An object of that
// name that this agent's labels don't mark as its own is left to its owner,
// like the callout's unowned ClusterRoleBinding. cleanupA2A removes the
// fence last, after the bus fence that admits the namespace is gone; a pass
// that dies before then leaves a fence that only restricts.
func (r *PlatformAgentReconciler) deleteProfileQuota(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	return r.deleteProfileObject(ctx, agent, &corev1.ResourceQuota{ObjectMeta: metav1.ObjectMeta{Name: profileQuotaName(agent), Namespace: profileNamespaceName(agent)}}, r.a2aReader(), profileQuotaComponent)
}

func (r *PlatformAgentReconciler) deleteProfileFence(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	return r.deleteProfileObject(ctx, agent, &networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: profileNetpolName(agent), Namespace: profileNamespaceName(agent)}}, r.Client, profileNetpolComponent)
}

func (r *PlatformAgentReconciler) deleteProfileObject(ctx context.Context, agent *agentv1alpha1.PlatformAgent, obj client.Object, reader client.Reader, component string) error {
	if err := reader.Get(ctx, client.ObjectKeyFromObject(obj), obj); err != nil {
		return client.IgnoreNotFound(err)
	}
	if !renderedByAgent(obj, agent, component) {
		return nil
	}
	if err := r.Delete(ctx, obj); client.IgnoreNotFound(err) != nil {
		return fmt.Errorf("delete %s/%s: %w", obj.GetNamespace(), obj.GetName(), err)
	}
	return nil
}

// profileFenceRendered reports whether this agent's profile fence exists,
// for cleanupA2A's sentinels: it is the first write of
// reconcileA2ANetworkFences, and it carries no owner reference, so the
// owner test the other sentinels use can't see it.
func (r *PlatformAgentReconciler) profileFenceRendered(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (bool, error) {
	var np networkingv1.NetworkPolicy
	err := r.Get(ctx, types.NamespacedName{Namespace: profileNamespaceName(agent), Name: profileNetpolName(agent)}, &np)
	if apierrors.IsNotFound(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return renderedByAgent(&np, agent, profileNetpolComponent), nil
}

// agentOfProfileFence maps a NetworkPolicy back to the one agent whose
// profile fence it is, for the watch that re-reconciles that agent when its
// fence changes or is deleted: the fence carries no owner reference, so
// Owns() never sees it. Exactly the agent the fence's name names
// (<agent>-profile-netpol in <agent-ns>-profiles), with no List, so a
// NetworkPolicy writer there can reach one agent at most; ok is false for
// anything else.
func agentOfProfileFence(obj client.Object) (types.NamespacedName, bool) {
	ns, ok := strings.CutSuffix(obj.GetNamespace(), profileNamespaceSuffix)
	if !ok || ns == "" || obj.GetLabels()[a2aComponentLabel] != profileNetpolComponent {
		return types.NamespacedName{}, false
	}
	name, ok := strings.CutSuffix(obj.GetName(), profileNetpolSuffix)
	if !ok || name == "" {
		return types.NamespacedName{}, false
	}
	return types.NamespacedName{Namespace: ns, Name: name}, true
}

// The profile namespace is optional (absent renders nothing), so a failure
// confined to it never blocks work in the agent's own namespace: the
// render, the status, the teardown and the finalizer all carry on, and the
// failure is logged. Each site that touches the profile namespace follows
// that one rule, through profileNamespaceFailure, which logs it and writes a Warning Event. A status condition
// naming the failure comes with the worker PR, when something there is
// something a user would notice.
func (r *PlatformAgentReconciler) profileNamespaceFailure(ctx context.Context, agent *agentv1alpha1.PlatformAgent, what string, err error) {
	logf.FromContext(ctx).Error(err, "profile namespace: "+what+"; the agent's own namespace carries on",
		"namespace", profileNamespaceName(agent))
	r.recordEvent(agent, corev1.EventTypeWarning, reasonProfileNamespaceFailed,
		fmt.Sprintf("profile namespace %s: %s failed: %v", profileNamespaceName(agent), what, err))
}

// reasonProfileNamespaceFailed is the Warning Event a failure confined to
// the profile namespace writes on the PlatformAgent, so it shows in
// kubectl describe until a status condition carries it.
const reasonProfileNamespaceFailed = "ProfileNamespaceFailed"

// profileNamespaceError marks a failure confined to the profile namespace
// coming out of reconcileA2ANetworkFences, so its callers can log it rather
// than stop on it.
type profileNamespaceError struct{ err error }

func (e *profileNamespaceError) Error() string { return e.err.Error() }
func (e *profileNamespaceError) Unwrap() error { return e.err }
