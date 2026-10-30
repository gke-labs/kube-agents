package controller

import (
	"context"
	"fmt"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"reflect"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"testing"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func profileTunedAgent(ceiling, warm *int) *agentv1alpha1.PlatformAgent {
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{
		Profiles: &agentv1alpha1.ProfileTuning{Ceiling: ceiling, WarmReplicas: warm},
	}}
	return agent
}

func TestProfileTuningDefaults(t *testing.T) {
	agent := a2aTestAgent()
	if got := resolveProfileCeiling(agent); got != agentv1alpha1.DefaultProfileCeiling {
		t.Errorf("ceiling = %d, want the default %d", got, agentv1alpha1.DefaultProfileCeiling)
	}
	if got := resolveProfileWarmReplicas(agent); got != agentv1alpha1.DefaultProfileWarmReplicas {
		t.Errorf("warm replicas = %d, want the default %d", got, agentv1alpha1.DefaultProfileWarmReplicas)
	}
	tuned := profileTunedAgent(ptr.To(5), ptr.To(0))
	if got := resolveProfileCeiling(tuned); got != 5 {
		t.Errorf("ceiling = %d, want the CR's 5", got)
	}
	if got := resolveProfileWarmReplicas(tuned); got != 0 {
		t.Errorf("warm replicas = %d, want the CR's 0", got)
	}
}

// The quota bounds pods alone, in the profile namespace: the warm workers
// of each interactive (profile, tier), a surge pod per warm pool, and the
// ceiling's worth of Job pods.
func TestBuildProfileQuota(t *testing.T) {
	readOnly := testAgentProfile("test-ns", "a")
	bothTiers := testAgentProfile("test-ns", "b", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	})
	unattended := testAgentProfile("test-ns", "c", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierUnattended}
	})
	for _, tc := range []struct {
		name     string
		agent    *agentv1alpha1.PlatformAgent
		profiles []agentv1alpha1.AgentProfile
		pods     int64
	}{
		{"no profiles: the ceiling alone", a2aTestAgent(), nil, agentv1alpha1.DefaultProfileCeiling},
		// Two read pools (a, b): 2 warm + 2 surge + ceiling 8. c has no warm pool.
		{"two read pools at the defaults", a2aTestAgent(), []agentv1alpha1.AgentProfile{readOnly, bothTiers, unattended}, 2*1 + 2 + 8},
		{"tuned", profileTunedAgent(ptr.To(5), ptr.To(3)), []agentv1alpha1.AgentProfile{readOnly}, 1*3 + 1 + 5},
		{"no warm workers", profileTunedAgent(ptr.To(5), ptr.To(0)), []agentv1alpha1.AgentProfile{readOnly}, 0 + 1 + 5},
	} {
		t.Run(tc.name, func(t *testing.T) {
			q := buildProfileQuota(tc.agent, tc.profiles)
			if q.Namespace != "test-ns-profiles" || q.Name != "test-agent-profile-quota" {
				t.Errorf("quota at %s/%s, want test-ns-profiles/test-agent-profile-quota", q.Namespace, q.Name)
			}
			if len(q.Spec.Hard) != 1 {
				t.Fatalf("quota bounds %v, want exactly pods", q.Spec.Hard)
			}
			if pods := q.Spec.Hard[corev1.ResourcePods]; pods.Value() != tc.pods {
				t.Errorf("pods = %s, want %d", pods.String(), tc.pods)
			}
			if len(q.OwnerReferences) != 0 {
				t.Errorf("the quota carries owner references %v; one can't point across namespaces", q.OwnerReferences)
			}
		})
	}
}

// The fence selects every pod in the profile namespace, admits nothing in,
// and lets out DNS, the bus, LiteLLM and the credential broker only.
func TestBuildProfileNetworkPolicy(t *testing.T) {
	np := buildProfileNetworkPolicy(a2aTestAgent(), []string{"10.96.0.10"})
	if np.Namespace != "test-ns-profiles" || np.Name != "test-agent-profile-netpol" {
		t.Errorf("policy at %s/%s, want test-ns-profiles/test-agent-profile-netpol", np.Namespace, np.Name)
	}
	if len(np.Spec.PodSelector.MatchLabels) != 0 || len(np.Spec.PodSelector.MatchExpressions) != 0 {
		t.Errorf("pod selector %+v, want every pod in the namespace", np.Spec.PodSelector)
	}
	if !reflect.DeepEqual(np.Spec.PolicyTypes, []networkingv1.PolicyType{networkingv1.PolicyTypeIngress, networkingv1.PolicyTypeEgress}) {
		t.Errorf("policy types %v, want ingress and egress", np.Spec.PolicyTypes)
	}
	if len(np.Spec.Ingress) != 0 {
		t.Errorf("ingress rules %+v, want none", np.Spec.Ingress)
	}
	if len(np.Spec.Egress) != 4 {
		t.Fatalf("egress rules %d, want exactly DNS, NATS, LiteLLM and the credential broker: %+v", len(np.Spec.Egress), np.Spec.Egress)
	}
	if p := np.Spec.Egress[0].Ports; len(p) != 2 || p[0].Port.IntVal != a2aDNSPort {
		t.Errorf("first egress rule ports %+v, want DNS", p)
	}
	if !reflect.DeepEqual(np.Spec.Egress[0].To, clusterDNSPeers([]string{"10.96.0.10"})) {
		t.Errorf("DNS peers %+v, want the package's one definition of DNS", np.Spec.Egress[0].To)
	}
	nats := np.Spec.Egress[1]
	if len(nats.Ports) != 1 || nats.Ports[0].Port.IntVal != a2aNATSClientPort || len(nats.To) != 1 ||
		!reflect.DeepEqual(nats.To[0].NamespaceSelector.MatchLabels, map[string]string{"kubernetes.io/metadata.name": "test-ns"}) ||
		nats.To[0].PodSelector.MatchLabels[a2aComponentLabel] != "nats" {
		t.Errorf("second egress rule %+v, want the NATS pods in the agent's namespace on the client port", nats)
	}
	llm := np.Spec.Egress[2]
	var llmPorts []int32
	for _, p := range llm.Ports {
		llmPorts = append(llmPorts, p.Port.IntVal)
	}
	if !reflect.DeepEqual(llmPorts, []int32{a2aLiteLLMServicePort, a2aLiteLLMUpstreamPort, a2aLiteLLMContainerPort}) {
		t.Errorf("LiteLLM egress ports %v, want the service, upstream and container ports, as a session pod has", llmPorts)
	}
	if len(llm.To) != 1 || llm.To[0].PodSelector.MatchLabels["app"] != "litellm" ||
		!reflect.DeepEqual(llm.To[0].NamespaceSelector.MatchLabels, map[string]string{"kubernetes.io/metadata.name": "test-ns"}) {
		t.Errorf("third egress rule %+v, want the LiteLLM pods in the agent's namespace", llm)
	}
	broker := np.Spec.Egress[3]
	if len(broker.Ports) != 1 || broker.Ports[0].Port.IntVal != credentialProxyPort || len(broker.To) != 1 ||
		!reflect.DeepEqual(broker.To[0].PodSelector.MatchLabels, credentialProxySelector(a2aTestAgent())) ||
		!reflect.DeepEqual(broker.To[0].NamespaceSelector.MatchLabels, map[string]string{"kubernetes.io/metadata.name": "test-ns"}) {
		t.Errorf("fourth egress rule %+v, want the broker's pods in the agent's namespace on its credentialed port", broker)
	}
}

// LiteLLM admits profile pods on a next install and not otherwise.
func TestLiteLLMAdmitsProfilePodsOnNextOnly(t *testing.T) {
	profile := netpolProfile{Generated: true, DNSClusterIPs: []string{"10.96.0.10"}}
	admits := func(agent *agentv1alpha1.PlatformAgent) bool {
		for _, rule := range buildLiteLLMNetworkPolicy(agent, profile).Spec.Ingress {
			for _, peer := range rule.From {
				if peer.NamespaceSelector != nil && peer.NamespaceSelector.MatchLabels[corev1.LabelMetadataName] == profileNamespaceName(agent) {
					return true
				}
			}
		}
		return false
	}
	next := a2aTestAgent()
	if !a2aStackRendering(next) {
		t.Fatal("the next fixture doesn't render the A2A stack, so the first assertion would be vacuous")
	}
	if !admits(next) {
		t.Error("LiteLLM's policy does not admit the profile namespace on a next install")
	}
	today := a2aTestAgent()
	today.Spec.Mode = ptr.To("today")
	if admits(today) {
		t.Error("LiteLLM's policy admits the profile namespace on a today install, where no profile pods exist")
	}
	// Version skew freezes the bus fence with its profile peer; LiteLLM's
	// policy must not narrow under it (a2aAgentSurface).
	skew := a2aTestAgent()
	skew.Spec.Mode = ptr.To("later")
	if !admits(skew) {
		t.Error("LiteLLM's policy drops the profile namespace on a skew pass while the bus fence keeps it")
	}
}

// The watch maps a profile fence, and nothing else, back to the one agent
// its name names.
func TestAgentOfProfileFence(t *testing.T) {
	agent := a2aTestAgent()
	fence := buildProfileNetworkPolicy(agent, nil)
	if got, ok := agentOfProfileFence(fence); !ok || got != (types.NamespacedName{Namespace: "test-ns", Name: "test-agent"}) {
		t.Errorf("the profile fence maps to %v %v, want test-ns/test-agent", got, ok)
	}
	for name, mutate := range map[string]func(*networkingv1.NetworkPolicy){
		"another component":             func(np *networkingv1.NetworkPolicy) { np.Labels[a2aComponentLabel] = "session-netpol" },
		"outside a -profiles namespace": func(np *networkingv1.NetworkPolicy) { np.Namespace = "test-ns" },
		"a name without the suffix":     func(np *networkingv1.NetworkPolicy) { np.Name = "something-else" },
		"the bare suffix":               func(np *networkingv1.NetworkPolicy) { np.Name = "-profile-netpol" },
	} {
		np := fence.DeepCopy()
		mutate(np)
		if got, ok := agentOfProfileFence(np); ok {
			t.Errorf("%s maps to %v, want nothing", name, got)
		}
	}
}

// A today install whose only A2A residue is the profile fence (a guardrail
// pass that died after it and before the NATS fence) still runs the
// teardown, and cleanupA2A removes the fence and the quota.
func TestTheProfileFenceAloneDrivesTheTeardown(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	agent.Spec.Mode = ptr.To("today")
	fence := buildProfileNetworkPolicy(agent, nil)
	withCommonLabels(fence, agent)
	quota := buildProfileQuota(agent, nil)
	withCommonLabels(quota, agent)
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, fence, quota).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if err := r.cleanupA2A(context.Background(), agent); err != nil {
		t.Fatalf("cleanupA2A: %v", err)
	}
	var np networkingv1.NetworkPolicy
	if err := cl.Get(context.Background(), types.NamespacedName{Namespace: "test-ns-profiles", Name: fence.Name}, &np); !apierrors.IsNotFound(err) {
		t.Errorf("the profile fence survived a teardown it was the only residue of (err %v)", err)
	}
	var q corev1.ResourceQuota
	if err := cl.Get(context.Background(), types.NamespacedName{Namespace: "test-ns-profiles", Name: quota.Name}, &q); !apierrors.IsNotFound(err) {
		t.Errorf("the profile quota survived the flip to today (err %v)", err)
	}
}

// With no profile namespace (the chart didn't make it) nothing is rendered
// and nothing is created. The case with one is in the envtest file, since
// the fake client can't server-side-apply a NetworkPolicy.
func TestNoProfileNamespaceRendersNothing(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()
	fenced, err := r.reconcileProfileFence(ctx, agent, nil)
	if err != nil || fenced {
		t.Fatalf("with no profile namespace: fenced=%v err=%v, want false and no error", fenced, err)
	}
	if err := r.reconcileProfileQuota(ctx, agent, nil, false); err != nil {
		t.Fatalf("quota on an unfenced pass: %v", err)
	}
	var ns corev1.Namespace
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-ns-profiles"}, &ns); !apierrors.IsNotFound(err) {
		t.Errorf("the profile namespace exists after the reconcile (err %v); the operator must never create it", err)
	}
	var q corev1.ResourceQuota
	if err := cl.Get(ctx, types.NamespacedName{Namespace: "test-ns-profiles", Name: "test-agent-profile-quota"}, &q); !apierrors.IsNotFound(err) {
		t.Errorf("a quota was rendered into a namespace that doesn't exist (err %v)", err)
	}
	if fenced, err := r.profileFenced(ctx, agent); err != nil || fenced {
		t.Errorf("profileFenced = %v, %v with no fence, want false", fenced, err)
	}
}

// withoutProfilePeer takes out the profile namespace and nothing else, and
// drops a rule it leaves with no peer (LiteLLM's profile rule), not one that
// had none to begin with.
func TestWithoutProfilePeer(t *testing.T) {
	agent := a2aTestAgent()
	nats := buildA2ANATSNetworkPolicy(agent)
	before := len(nats.Spec.Ingress[0].From)
	withoutProfilePeer(nats, agent)
	if len(nats.Spec.Ingress) != 2 || len(nats.Spec.Ingress[0].From) != before-1 {
		t.Errorf("NATS fence after: %d rules, %d peers on 4222 (was %d), want 2 rules and one peer fewer", len(nats.Spec.Ingress), len(nats.Spec.Ingress[0].From), before)
	}
	for _, peer := range nats.Spec.Ingress[0].From {
		if reflect.DeepEqual(peer, profileNamespacePeer(agent)) {
			t.Error("the profile peer is still on the NATS fence")
		}
	}
	llm := buildLiteLLMNetworkPolicy(agent, netpolProfile{Generated: true, DNSClusterIPs: []string{"10.96.0.10"}})
	rules := len(llm.Spec.Ingress)
	withoutProfilePeer(llm, agent)
	if len(llm.Spec.Ingress) != rules-1 {
		t.Errorf("LiteLLM ingress rules %d after, want %d (the profile rule dropped)", len(llm.Spec.Ingress), rules-1)
	}
	open := &networkingv1.NetworkPolicy{Spec: networkingv1.NetworkPolicySpec{Ingress: []networkingv1.NetworkPolicyIngressRule{{}}}}
	withoutProfilePeer(open, agent)
	if len(open.Spec.Ingress) != 1 {
		t.Error("a rule with no peers (allow from anywhere) was dropped; only one the profile peer emptied may be")
	}
}

// The flip deletes the profile fence after the bus fence that admits the
// namespace, so no pass leaves the bus admitting a namespace nothing
// confines; and a pass whose quota delete fails keeps the fence, the
// sentinel that brings the teardown back for the quota.
func TestTheFlipDeletesTheProfileFenceAfterTheBusFence(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	agent.Spec.Mode = ptr.To("today")
	agent.UID = "agent-uid"
	nats := buildA2ANATSNetworkPolicy(agent)
	if err := ctrl.SetControllerReference(agent, nats, scheme); err != nil {
		t.Fatal(err)
	}
	fence := buildProfileNetworkPolicy(agent, nil)
	withCommonLabels(fence, agent)
	quota := buildProfileQuota(agent, nil)
	withCommonLabels(quota, agent)
	var order []string
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, nats, fence, quota).WithInterceptorFuncs(interceptor.Funcs{
		Delete: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
			order = append(order, obj.GetNamespace()+"/"+obj.GetName())
			return c.Delete(ctx, obj, opts...)
		},
	}).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if err := r.cleanupA2A(context.Background(), agent); err != nil {
		t.Fatalf("cleanupA2A: %v", err)
	}
	at := func(name string) int {
		for i, o := range order {
			if o == name {
				return i
			}
		}
		return -1
	}
	busAt, fenceAt := at("test-ns/"+nats.Name), at("test-ns-profiles/"+fence.Name)
	if busAt < 0 || fenceAt < 0 || fenceAt < busAt {
		t.Errorf("delete order %v: the profile fence (at %d) must go after the bus fence (at %d)", order, fenceAt, busAt)
	}

	// A transient failure on the quota keeps the fence for the retry.
	fence2 := buildProfileNetworkPolicy(agent, nil)
	withCommonLabels(fence2, agent)
	quota2 := buildProfileQuota(agent, nil)
	withCommonLabels(quota2, agent)
	cl = fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, fence2, quota2).WithInterceptorFuncs(interceptor.Funcs{
		Delete: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
			if obj.GetName() == quota2.Name {
				return apierrors.NewTooManyRequests("throttled", 1)
			}
			return c.Delete(ctx, obj, opts...)
		},
	}).Build()
	r = &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if err := r.cleanupA2A(context.Background(), agent); err != nil {
		t.Fatalf("cleanupA2A with a throttled quota delete: %v", err)
	}
	var np networkingv1.NetworkPolicy
	if err := cl.Get(context.Background(), types.NamespacedName{Namespace: "test-ns-profiles", Name: fence2.Name}, &np); err != nil {
		t.Errorf("the profile fence was deleted on a pass whose quota delete failed, so nothing brings the teardown back for the quota (err %v)", err)
	}
}

// A refused delete in the profile namespace doesn't hold the flip's
// teardown: it carries on and logs it.
func TestAProfileDeleteRefusalHoldsNothing(t *testing.T) {
	scheme := setupScheme()
	refuse := interceptor.Funcs{
		Delete: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
			if obj.GetNamespace() == "test-ns-profiles" {
				return apierrors.NewForbidden(schema.GroupResource{Resource: "resourcequotas"}, obj.GetName(), fmt.Errorf("refused by a policy in the profile namespace"))
			}
			return c.Delete(ctx, obj, opts...)
		},
	}
	// The flip to today: cleanupA2A returns nothing for it.
	today := a2aTestAgent()
	today.Spec.Mode = ptr.To("today")
	fence := buildProfileNetworkPolicy(today, nil)
	withCommonLabels(fence, today)
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(today, fence).WithInterceptorFuncs(refuse).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if err := r.cleanupA2A(context.Background(), today); err != nil {
		t.Errorf("cleanupA2A with a refused profile delete = %v, want it logged", err)
	}
}
