package controller

import (
	"context"
	"fmt"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// With the profile namespace present, the quota and the fence are applied
// into it, against a real API server.
func TestTheProfileQuotaAndFenceLandInTheNamespace(t *testing.T) {
	c, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := a2aTestAgent()
	if err := c.Create(ctx, chartProfileNamespace(agent)); err != nil {
		t.Fatal(err)
	}
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	fenced, err := r.reconcileProfileFence(ctx, agent, nil)
	if err != nil || !fenced {
		t.Fatalf("fenced=%v err=%v, want true and no error", fenced, err)
	}
	if err := r.reconcileProfileQuota(ctx, agent, nil, true); err != nil {
		t.Fatalf("quota: %v", err)
	}
	var q corev1.ResourceQuota
	if err := c.Get(ctx, types.NamespacedName{Namespace: "test-ns-profiles", Name: "test-agent-profile-quota"}, &q); err != nil {
		t.Fatalf("the profile quota: %v", err)
	}
	// No AgentProfiles, so no warm pools: the ceiling alone.
	if pods := q.Spec.Hard[corev1.ResourcePods]; pods.Value() != agentv1alpha1.DefaultProfileCeiling {
		t.Errorf("quota pods = %s, want %d", pods.String(), agentv1alpha1.DefaultProfileCeiling)
	}
	var np networkingv1.NetworkPolicy
	if err := c.Get(ctx, types.NamespacedName{Namespace: "test-ns-profiles", Name: "test-agent-profile-netpol"}, &np); err != nil {
		t.Fatalf("the profile fence: %v", err)
	}
	if len(np.OwnerReferences) != 0 || len(q.OwnerReferences) != 0 {
		t.Errorf("owner references on objects in another namespace: quota %v, fence %v", q.OwnerReferences, np.OwnerReferences)
	}
	if fenced, err := r.profileFenced(ctx, agent); err != nil || !fenced {
		t.Errorf("profileFenced = %v, %v, want true", fenced, err)
	}
}

// The bus admits the profile namespace only once that namespace's own fence
// is in place, on the refusal path's fence reconcile as on the render path.
func TestTheNATSFenceAdmitsProfilePodsOnlyWhenTheyAreFenced(t *testing.T) {
	c, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID: envtestHarnessProject, Location: envtestHarnessLocation, ClusterName: envtestHarnessCluster,
	}
	if err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}}); err != nil {
		t.Fatal(err)
	}
	if err := c.Create(ctx, agent); err != nil {
		t.Fatal(err)
	}
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	admitted := func() bool {
		t.Helper()
		var nats networkingv1.NetworkPolicy
		if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: buildA2ANATSNetworkPolicy(agent).Name}, &nats); err != nil {
			t.Fatalf("the NATS fence: %v", err)
		}
		for _, peer := range nats.Spec.Ingress[0].From {
			if peer.NamespaceSelector != nil && peer.NamespaceSelector.MatchLabels[corev1.LabelMetadataName] == profileNamespaceName(agent) {
				return true
			}
		}
		return false
	}
	if _, _, err := r.reconcileA2ANetworkFences(ctx, agent); err != nil {
		t.Fatalf("fences with no profile namespace: %v", err)
	}
	if admitted() {
		t.Error("the NATS fence admits the profile namespace while no fence confines it")
	}
	if err := c.Create(ctx, chartProfileNamespace(agent)); err != nil {
		t.Fatal(err)
	}
	if _, _, err := r.reconcileA2ANetworkFences(ctx, agent); err != nil {
		t.Fatalf("fences with the profile namespace: %v", err)
	}
	if !admitted() {
		t.Error("the NATS fence does not admit the profile namespace once its fence is in place")
	}
}

// allowedTiers admits read and unattended, once each, and nothing else.
func TestTheAgentProfileCRDBoundsAllowedTiers(t *testing.T) {
	cfg, scheme := startEnvtestConfig(t)
	c, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	const ns = "agentprofile-tiers"
	if err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}); err != nil {
		t.Fatal(err)
	}
	for i, tc := range []struct {
		tiers []string
		ok    bool
	}{
		{nil, true},
		{[]string{"read"}, true},
		{[]string{"read", "unattended"}, true},
		{[]string{"admin"}, false},
		{[]string{"Read"}, false},
		{[]string{"read", "read"}, false},
		{[]string{}, true}, // omitempty: an empty list is absent, which means read
	} {
		p := testAgentProfile(ns, "tiers-"+string(rune('a'+i)), func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = tc.tiers })
		p.ResourceVersion, p.Generation = "", 0
		err := c.Create(ctx, &p)
		if tc.ok && err != nil {
			t.Errorf("allowedTiers %v refused: %v", tc.tiers, err)
		}
		if !tc.ok && (err == nil || !strings.Contains(err.Error(), "allowedTiers")) {
			t.Errorf("allowedTiers %v = %v, want a refusal naming allowedTiers", tc.tiers, err)
		}
	}
}

// LiteLLM admits the profile namespace only once that namespace's fence is
// in place, at its own apply site.
func TestLiteLLMAdmitsProfilePodsOnlyWhenTheyAreFenced(t *testing.T) {
	c, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := a2aTestAgent()
	if err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}}); err != nil {
		t.Fatal(err)
	}
	labels := map[string]string{"app": "litellm"}
	if err := c.Create(ctx, &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: litellmDeploymentName, Namespace: agent.Namespace},
		Spec: appsv1.DeploymentSpec{
			Selector: &metav1.LabelSelector{MatchLabels: labels},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "litellm", Image: "litellm:1"}}},
			},
		},
	}); err != nil {
		t.Fatal(err)
	}
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	profile := netpolProfile{Generated: true, DNSClusterIPs: []string{"10.96.0.10"}}
	admitted := func() bool {
		t.Helper()
		if err := r.reconcileLiteLLMNetworkPolicy(ctx, agent, profile); err != nil {
			t.Fatalf("LiteLLM policy: %v", err)
		}
		var np networkingv1.NetworkPolicy
		if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: litellmNetworkPolicyName}, &np); err != nil {
			t.Fatalf("the LiteLLM policy: %v", err)
		}
		for _, rule := range np.Spec.Ingress {
			for _, peer := range rule.From {
				if peer.NamespaceSelector != nil && peer.NamespaceSelector.MatchLabels[corev1.LabelMetadataName] == profileNamespaceName(agent) {
					return true
				}
			}
		}
		return false
	}
	if admitted() {
		t.Error("LiteLLM admits the profile namespace while no fence confines it")
	}
	if err := c.Create(ctx, chartProfileNamespace(agent)); err != nil {
		t.Fatal(err)
	}
	// The state the gate exists for: the namespace is there and marked, and
	// its fence isn't yet (every first next pass, since LiteLLM's policy is
	// applied before the fence pass).
	if admitted() {
		t.Error("LiteLLM admits the profile namespace before its fence is in place")
	}
	if fenced, err := r.reconcileProfileFence(ctx, agent, nil); err != nil || !fenced {
		t.Fatalf("profile fence: %v %v", fenced, err)
	}
	if !admitted() {
		t.Error("LiteLLM does not admit the profile namespace once its fence is in place")
	}
}

// chartProfileNamespace is the profile namespace as the chart makes it.
func chartProfileNamespace(agent *agentv1alpha1.PlatformAgent) *corev1.Namespace {
	return &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{
		Name:        profileNamespaceName(agent),
		Labels:      map[string]string{profileNamespaceComponentLabel: profileNamespaceComponent},
		Annotations: map[string]string{helmReleaseNamespaceAnnotation: agent.Namespace},
	}}
}

// A namespace with the profile namespace's name but not the chart's markers
// is treated as absent: no fence, no quota, and the bus doesn't admit it.
func TestAnUnmarkedNamespaceIsNotTheProfileNamespace(t *testing.T) {
	c, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := a2aTestAgent()
	for _, ns := range []*corev1.Namespace{
		{ObjectMeta: metav1.ObjectMeta{Name: profileNamespaceName(agent)}},
	} {
		if err := c.Create(ctx, ns); err != nil {
			t.Fatal(err)
		}
	}
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	for _, mark := range []func(*corev1.Namespace){
		func(*corev1.Namespace) {},
		func(ns *corev1.Namespace) {
			ns.Labels = map[string]string{profileNamespaceComponentLabel: profileNamespaceComponent}
		},
		func(ns *corev1.Namespace) {
			ns.Labels = map[string]string{profileNamespaceComponentLabel: profileNamespaceComponent}
			ns.Annotations = map[string]string{helmReleaseNamespaceAnnotation: "some-other-release-ns"}
		},
	} {
		var ns corev1.Namespace
		if err := c.Get(ctx, types.NamespacedName{Name: profileNamespaceName(agent)}, &ns); err != nil {
			t.Fatal(err)
		}
		mark(&ns)
		if err := c.Update(ctx, &ns); err != nil {
			t.Fatal(err)
		}
		fenced, err := r.reconcileProfileFence(ctx, agent, nil)
		if err != nil || fenced {
			t.Errorf("namespace labels %v annotations %v: fenced=%v err=%v, want it treated as absent", ns.Labels, ns.Annotations, fenced, err)
		}
	}
	var np networkingv1.NetworkPolicy
	if err := c.Get(ctx, types.NamespacedName{Namespace: profileNamespaceName(agent), Name: profileNetpolName(agent)}, &np); err == nil {
		t.Error("a fence was rendered into an unmarked namespace")
	}
}

// The delete helpers the flip and the deletion use remove the agent's quota
// and fence from the profile namespace, and leave an object of the same name
// that isn't its. (The flip's call sites are in
// TestTheProfileFenceAloneDrivesTheTeardown, which runs cleanupA2A.)
func TestTheProfileDeleteHelpersTakeOnlyTheAgentsOwn(t *testing.T) {
	c, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := a2aTestAgent()
	if err := c.Create(ctx, chartProfileNamespace(agent)); err != nil {
		t.Fatal(err)
	}
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	if _, err := r.reconcileProfileFence(ctx, agent, nil); err != nil {
		t.Fatal(err)
	}
	// A quota of the agent's name that something else owns.
	foreign := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Name: profileQuotaName(agent), Namespace: profileNamespaceName(agent)},
		Spec:       corev1.ResourceQuotaSpec{Hard: corev1.ResourceList{corev1.ResourcePods: resource.MustParse("3")}},
	}
	if err := c.Create(ctx, foreign); err != nil {
		t.Fatal(err)
	}
	if err := r.deleteProfileQuota(ctx, agent); err != nil {
		t.Fatalf("delete the quota: %v", err)
	}
	if err := r.deleteProfileFence(ctx, agent); err != nil {
		t.Fatalf("delete the fence: %v", err)
	}
	var np networkingv1.NetworkPolicy
	if err := c.Get(ctx, types.NamespacedName{Namespace: profileNamespaceName(agent), Name: profileNetpolName(agent)}, &np); !apierrors.IsNotFound(err) {
		t.Errorf("the profile fence survived the flip (err %v)", err)
	}
	var q corev1.ResourceQuota
	if err := c.Get(ctx, types.NamespacedName{Namespace: profileNamespaceName(agent), Name: profileQuotaName(agent)}, &q); err != nil {
		t.Errorf("a quota this agent didn't render was deleted: %v", err)
	}
}

// profileFenced trusts a fence only in a marked namespace and only if it's
// this agent's.
func TestProfileFencedChecksTheNamespaceAndTheFencesOwner(t *testing.T) {
	c, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := a2aTestAgent()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	// A fence of the right name, unlabelled, in an unmarked namespace.
	if err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: profileNamespaceName(agent)}}); err != nil {
		t.Fatal(err)
	}
	foreign := buildProfileNetworkPolicy(agent, nil)
	foreign.Labels = nil
	if err := c.Create(ctx, foreign); err != nil {
		t.Fatal(err)
	}
	if fenced, err := r.profileFenced(ctx, agent); err != nil || fenced {
		t.Errorf("unmarked namespace: profileFenced = %v, %v, want false", fenced, err)
	}
	// Mark the namespace; the fence still isn't this agent's.
	var ns corev1.Namespace
	if err := c.Get(ctx, types.NamespacedName{Name: profileNamespaceName(agent)}, &ns); err != nil {
		t.Fatal(err)
	}
	marked := chartProfileNamespace(agent)
	ns.Labels, ns.Annotations = marked.Labels, marked.Annotations
	if err := c.Update(ctx, &ns); err != nil {
		t.Fatal(err)
	}
	if fenced, err := r.profileFenced(ctx, agent); err != nil || fenced {
		t.Errorf("a fence this agent didn't render: profileFenced = %v, %v, want false", fenced, err)
	}
	// The agent's own fence.
	if fenced, err := r.reconcileProfileFence(ctx, agent, nil); err != nil || !fenced {
		t.Fatalf("reconcileProfileFence = %v, %v", fenced, err)
	}
	if fenced, err := r.profileFenced(ctx, agent); err != nil || !fenced {
		t.Errorf("the agent's fence in a marked namespace: profileFenced = %v, %v, want true", fenced, err)
	}
	// The agent's own fence, in a namespace whose markers are then removed.
	if err := c.Get(ctx, types.NamespacedName{Name: profileNamespaceName(agent)}, &ns); err != nil {
		t.Fatal(err)
	}
	ns.Labels, ns.Annotations = nil, nil
	if err := c.Update(ctx, &ns); err != nil {
		t.Fatal(err)
	}
	if fenced, err := r.profileFenced(ctx, agent); err != nil || fenced {
		t.Errorf("the agent's fence in a namespace that lost its markers: profileFenced = %v, %v, want false", fenced, err)
	}
}

// A terminating profile namespace is absent, and a failure confined to the
// profile namespace doesn't cost the agent namespace its fences.
func TestATerminatingProfileNamespaceWithholdsNoOtherFence(t *testing.T) {
	c, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID: envtestHarnessProject, Location: envtestHarnessLocation, ClusterName: envtestHarnessCluster,
	}
	if err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}}); err != nil {
		t.Fatal(err)
	}
	if err := c.Create(ctx, agent); err != nil {
		t.Fatal(err)
	}
	ns := chartProfileNamespace(agent)
	ns.Finalizers = []string{"kubeagents.x-k8s.io/test-pin"}
	if err := c.Create(ctx, ns); err != nil {
		t.Fatal(err)
	}
	if err := c.Delete(ctx, ns); err != nil {
		t.Fatal(err)
	}
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	if exists, err := r.profileNamespaceExists(ctx, agent); err != nil || exists {
		t.Errorf("a terminating profile namespace: exists=%v err=%v, want absent", exists, err)
	}
	if _, fenced, err := r.reconcileA2ANetworkFences(ctx, agent); err != nil || fenced {
		t.Fatalf("fences with a terminating profile namespace: fenced=%v err=%v, want unfenced and no error", fenced, err)
	}
	var nats networkingv1.NetworkPolicy
	if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: buildA2ANATSNetworkPolicy(agent).Name}, &nats); err != nil {
		t.Fatalf("the NATS fence was not applied: %v", err)
	}
	for _, peer := range nats.Spec.Ingress[0].From {
		if peer.NamespaceSelector != nil && peer.NamespaceSelector.MatchLabels[corev1.LabelMetadataName] == profileNamespaceName(agent) {
			t.Error("the NATS fence admits a terminating profile namespace")
		}
	}
	// Unpin it so the API server can finish.
	var live corev1.Namespace
	if err := c.Get(ctx, types.NamespacedName{Name: ns.Name}, &live); err == nil {
		live.Finalizers = nil
		_ = c.Update(ctx, &live)
	}
}

// A profile-fence write that fails still lets the other fences land, and
// the error comes back after them.
func TestAProfileFenceFailureStillAppliesTheOtherFences(t *testing.T) {
	cfg, scheme := startEnvtestConfig(t)
	c, err := client.NewWithWatch(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID: envtestHarnessProject, Location: envtestHarnessLocation, ClusterName: envtestHarnessCluster,
	}
	if err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}}); err != nil {
		t.Fatal(err)
	}
	if err := c.Create(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if err := c.Create(ctx, chartProfileNamespace(agent)); err != nil {
		t.Fatal(err)
	}
	failing := interceptor.NewClient(c, interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if obj.GetNamespace() == profileNamespaceName(agent) {
				return fmt.Errorf("refused in the profile namespace")
			}
			return cl.Patch(ctx, obj, patch, opts...)
		},
	})
	r := &PlatformAgentReconciler{Client: failing, Scheme: scheme}
	if _, fenced, err := r.reconcileA2ANetworkFences(ctx, agent); err == nil || fenced {
		t.Fatalf("fences with a failing profile write: fenced=%v err=%v, want unfenced and the error", fenced, err)
	}
	var nats networkingv1.NetworkPolicy
	if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: buildA2ANATSNetworkPolicy(agent).Name}, &nats); err != nil {
		t.Fatalf("the NATS fence was withheld by a failure in the profile namespace: %v", err)
	}
	for _, peer := range nats.Spec.Ingress[0].From {
		if peer.NamespaceSelector != nil && peer.NamespaceSelector.MatchLabels[corev1.LabelMetadataName] == profileNamespaceName(agent) {
			t.Error("the NATS fence admits the profile namespace though its fence failed to apply")
		}
	}
}

// A refusal confined to the profile namespace doesn't stop the rest of the
// stack rendering, and isn't returned: the console and the session quota
// still land, and the pass goes on to the status writes.
func TestAProfileNamespaceRefusalDoesNotStopTheRender(t *testing.T) {
	cfg, scheme := startEnvtestConfig(t)
	c, err := client.NewWithWatch(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID: envtestHarnessProject, Location: envtestHarnessLocation, ClusterName: envtestHarnessCluster,
	}
	if err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}}); err != nil {
		t.Fatal(err)
	}
	if err := c.Create(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if err := c.Create(ctx, chartProfileNamespace(agent)); err != nil {
		t.Fatal(err)
	}
	refusing := interceptor.NewClient(c, interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if obj.GetNamespace() == profileNamespaceName(agent) {
				return fmt.Errorf("refused by a policy in the profile namespace")
			}
			return cl.Patch(ctx, obj, patch, opts...)
		},
	})
	r := &PlatformAgentReconciler{Client: refusing, Scheme: scheme}
	if _, err := r.reconcileA2A(ctx, agent); err != nil && strings.Contains(err.Error(), "refused by a policy in the profile namespace") {
		t.Fatalf("reconcileA2A returned the profile namespace's refusal (%v); it is logged, not returned", err)
	}
	var q corev1.ResourceQuota
	if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aSessionQuotaName(agent)}, &q); err != nil {
		t.Errorf("the session quota, after the fences, was not rendered: %v", err)
	}
	// The console renders right after the fences, which the profile
	// namespace's error used to stop. (The provision Job, later on, waits for
	// a serving callout, which envtest never runs.)
	var console appsv1.Deployment
	if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aConsoleName(agent)}, &console); err != nil {
		t.Errorf("the console, after the fences, was not rendered: %v", err)
	}
}

// LiteLLM's apply site withholds the peer on a profile-namespace read error
// and applies the policy anyway, returning nothing.
func TestLiteLLMWithholdsThePeerOnAProfileReadError(t *testing.T) {
	cfg, scheme := startEnvtestConfig(t)
	c, err := client.NewWithWatch(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	agent := a2aTestAgent()
	if err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}}); err != nil {
		t.Fatal(err)
	}
	labels := map[string]string{"app": "litellm"}
	if err := c.Create(ctx, &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: litellmDeploymentName, Namespace: agent.Namespace},
		Spec: appsv1.DeploymentSpec{
			Selector: &metav1.LabelSelector{MatchLabels: labels},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "litellm", Image: "litellm:1"}}},
			},
		},
	}); err != nil {
		t.Fatal(err)
	}
	failing := interceptor.NewClient(c, interceptor.Funcs{
		Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, ok := obj.(*corev1.Namespace); ok && key.Name == profileNamespaceName(agent) {
				return fmt.Errorf("throttled")
			}
			return cl.Get(ctx, key, obj, opts...)
		},
	})
	r := &PlatformAgentReconciler{Client: failing, Scheme: scheme}
	if err := r.reconcileLiteLLMNetworkPolicy(ctx, agent, netpolProfile{Generated: true, DNSClusterIPs: []string{"10.96.0.10"}}); err != nil {
		t.Fatalf("LiteLLM policy with a failing profile read = %v, want it logged and the policy applied", err)
	}
	var np networkingv1.NetworkPolicy
	if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: litellmNetworkPolicyName}, &np); err != nil {
		t.Fatalf("the LiteLLM policy was not applied: %v", err)
	}
	for _, rule := range np.Spec.Ingress {
		for _, peer := range rule.From {
			if peer.NamespaceSelector != nil && peer.NamespaceSelector.MatchLabels[corev1.LabelMetadataName] == profileNamespaceName(agent) {
				t.Error("LiteLLM admits the profile namespace though its fence couldn't be read")
			}
		}
	}
}
