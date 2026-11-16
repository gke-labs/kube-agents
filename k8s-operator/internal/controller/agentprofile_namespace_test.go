package controller

import (
	"context"
	"reflect"
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
	if got := resolveProfileCeiling(agent); got != defaultKanbanMaxInProgress {
		t.Errorf("ceiling = %d, want maxInProgress's default %d", got, defaultKanbanMaxInProgress)
	}
	// While both exist the ceiling follows maxInProgress, so moving work from
	// kanban to profiles doesn't change an install's footprint.
	kanban := a2aTestAgent()
	kanban.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxInProgress: ptr.To(11)}}
	if got := resolveProfileCeiling(kanban); got != 11 {
		t.Errorf("ceiling = %d, want maxInProgress's 11", got)
	}
	// maxInProgress has no maximum, so a ceiling taken from it is clamped
	// to the ceiling field's own.
	huge := a2aTestAgent()
	huge.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxInProgress: ptr.To(agentv1alpha1.MaxProfileCeiling + 1)}}
	if got := resolveProfileCeiling(huge); got != agentv1alpha1.MaxProfileCeiling {
		t.Errorf("ceiling = %d from a maxInProgress past the ceiling's maximum, want %d", got, agentv1alpha1.MaxProfileCeiling)
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
		{"no profiles: the ceiling alone", a2aTestAgent(), nil, defaultKanbanMaxInProgress},
		// Two read pools (a, b): 2 warm + 2 surge + the ceiling. c has no warm pool.
		{"two read pools at the defaults", a2aTestAgent(), []agentv1alpha1.AgentProfile{readOnly, bothTiers, unattended}, 2*1 + 2 + defaultKanbanMaxInProgress},
		{"tuned", profileTunedAgent(ptr.To(5), ptr.To(3)), []agentv1alpha1.AgentProfile{readOnly}, 1*3 + 1 + 5},
		{"no warm workers", profileTunedAgent(ptr.To(5), ptr.To(0)), []agentv1alpha1.AgentProfile{readOnly}, 0 + 1 + 5},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(a2aDispatcherJobPolicyEnvVar, "")
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

// Where Job mode can run, a task's slot frees once its Job is deleted, while
// that Job's pod may still be in its deletion grace period, which the pods
// quota counts: the next Job's pod would then be refused. The quota adds a
// second ceiling's worth for one terminating Job pod per slot, but only with
// the chart's Job policy signal and a profile allowing the unattended tier.
func TestTheProfileQuotaCoversTerminatingJobPods(t *testing.T) {
	readOnly := testAgentProfile("test-ns", "a")
	unattended := testAgentProfile("test-ns", "c", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierUnattended}
	})
	agent := profileTunedAgent(ptr.To(5), ptr.To(1))
	pods := func(profiles ...agentv1alpha1.AgentProfile) int64 {
		q := buildProfileQuota(agent, profiles)
		return q.Spec.Hard.Pods().Value()
	}
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	if got, want := pods(readOnly, unattended), int64(1*1+1+2*5); got != want {
		t.Errorf("Job mode on, an unattended profile: pods = %d, want %d (a warm pool and its surge, and the ceiling twice)", got, want)
	}
	if got, want := pods(readOnly), int64(1*1+1+5); got != want {
		t.Errorf("Job mode on, no unattended profile: pods = %d, want %d", got, want)
	}
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "")
	if got, want := pods(readOnly, unattended), int64(1*1+1+5); got != want {
		t.Errorf("no Job policy signal: pods = %d, want %d (unattended tasks have no worker)", got, want)
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

// LiteLLM admits profile pods on every install, whatever spec.mode says.
func TestLiteLLMAdmitsProfilePods(t *testing.T) {
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
	for name, mode := range map[string]*string{"next": ptr.To("next"), "no mode": nil, "a stored today": ptr.To("today")} {
		agent := a2aTestAgent()
		agent.Spec.Mode = mode
		if !admits(agent) {
			t.Errorf("LiteLLM's policy does not admit the profile namespace on an install with %s", name)
		}
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
	rulesBefore := len(nats.Spec.Ingress)
	withoutProfilePeer(nats, agent)
	if len(nats.Spec.Ingress) != rulesBefore || len(nats.Spec.Ingress[0].From) != before-1 {
		t.Errorf("NATS fence after: %d rules (was %d), %d peers on 4222 (was %d), want the same rules and one peer fewer", len(nats.Spec.Ingress), rulesBefore, len(nats.Spec.Ingress[0].From), before)
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

// The child reserve and the intervals default as the contract on #3655 says,
// and the reserve stays below the ceiling.
func TestProfileDispatchTuningDefaults(t *testing.T) {
	agent := a2aTestAgent()
	for name, tc := range map[string]struct{ got, want int }{
		"childReserve":         {func() int { c, _ := dispatchReserves(agent, resolveProfileCeiling(agent)); return c }(), 2}, // ceil(6/3)
		"leaseSeconds":         {resolveProfileLeaseSeconds(agent), 90},
		"renewSeconds":         {resolveProfileRenewSeconds(agent), 20},
		"restartGraceSeconds":  {resolveProfileRestartGraceSeconds(agent), 60},
		"acceptTimeoutSeconds": {resolveProfileAcceptTimeoutSeconds(agent), 10},
	} {
		if tc.got != tc.want {
			t.Errorf("%s = %d, want %d", name, tc.got, tc.want)
		}
	}
	for ceiling, want := range map[int]int{1: 0, 2: 1, 3: 1, 4: 2, 9: 3, 10: 4} {
		if got, _ := dispatchReserves(profileTunedAgent(ptr.To(ceiling), nil), ceiling); got != want {
			t.Errorf("ceiling %d: childReserve = %d, want %d", ceiling, got, want)
		}
	}
	tuned := profileTunedAgent(ptr.To(10), nil)
	tuned.Spec.Harness.Tuning.Profiles.ChildReserve = ptr.To(7)
	tuned.Spec.Harness.Tuning.Profiles.LeaseSeconds = ptr.To(300)
	if got, _ := dispatchReserves(tuned, 10); got != 7 {
		t.Errorf("childReserve = %d, want the CR's 7", got)
	}
	if got := resolveProfileLeaseSeconds(tuned); got != 300 {
		t.Errorf("leaseSeconds = %d, want the CR's 300", got)
	}
}
