package controller

import (
	"context"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	policyv1 "k8s.io/api/policy/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func withGatewayReplicas(agent *agentv1alpha1.PlatformAgent, n int32) *agentv1alpha1.PlatformAgent {
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Availability: &agentv1alpha1.AvailabilitySpec{
		A2AGateway: &agentv1alpha1.A2AGatewayAvailabilitySpec{Replicas: ptr.To(n)},
	}}
	return agent
}

func withGatewayElected(agent *agentv1alpha1.PlatformAgent, image string) *agentv1alpha1.PlatformAgent {
	setGatewayElectedCondition(agent, image, metav1.Now())
	return agent
}

func TestTheGatewayReplicaCountDefaultsToTwo(t *testing.T) {
	if got := a2aGatewayReplicas(a2aTestAgent()); got != 2 {
		t.Errorf("default = %d, want 2", got)
	}
	if got := a2aGatewayReplicas(withGatewayReplicas(a2aTestAgent(), 1)); got != 1 {
		t.Errorf("asked for 1 = %d", got)
	}
	if got := a2aGatewayReplicas(withGatewayReplicas(a2aTestAgent(), 3)); got != 3 {
		t.Errorf("asked for 3 = %d", got)
	}
}

// TestTheElectedImageFollowsTheRunningGateway: the condition names what the
// Lease holder runs, survives a pass that can't tell, and is dropped under
// today, with the election off, or on a roll to a pinned image no elected
// pod has run.
func TestTheElectedImageFollowsTheRunningGateway(t *testing.T) {
	const pinned, other = "example.com/a2a-gateway:pin", "example.com/a2a-gateway:older"
	t.Setenv(a2aGatewayImageEnvVar, pinned)
	t.Setenv(a2aGatewayElectionEnvVar, "")

	if got := gatewayElectedImage(a2aTestAgent(), ptr.To(pinned)); got != pinned {
		t.Errorf("a holder on the rendered image: %q", got)
	}
	if got := gatewayElectedImage(withGatewayElected(a2aTestAgent(), pinned), nil); got != pinned {
		t.Errorf("a pass that couldn't tell dropped the record: %q", got)
	}
	if got := gatewayElectedImage(withGatewayElected(a2aTestAgent(), other), nil); got != "" {
		t.Errorf("a pin to an image no elected pod has run kept %q", got)
	}
	if got := gatewayElectedImage(a2aTestAgent(), ptr.To(other)); got != "" {
		t.Errorf("a holder on another image than the pin counted: %q", got)
	}
	today := withGatewayElected(a2aTestAgent(), pinned)
	today.Spec.Mode = ptr.To("today")
	if got := gatewayElectedImage(today, ptr.To(pinned)); got != "" {
		t.Errorf("under today: %q", got)
	}
	t.Setenv(a2aGatewayElectionEnvVar, "false")
	if got := gatewayElectedImage(withGatewayElected(a2aTestAgent(), pinned), ptr.To(pinned)); got != "" {
		t.Errorf("with the election off: %q", got)
	}
}

// TestTheReleaseImageKeepsAnElectedGatewayScaledOut: an image from the
// operator's own release runs the election by construction, so a roll to it
// keeps an existing record.
func TestTheReleaseImageKeepsAnElectedGatewayScaledOut(t *testing.T) {
	t.Setenv(a2aGatewayImageEnvVar, "")
	t.Setenv(operatorImageEnvVar, "example.com/k8s-operator:v2")
	t.Setenv(a2aGatewayElectionEnvVar, "")
	if got := gatewayElectedImage(withGatewayElected(a2aTestAgent(), "example.com/a2a-gateway:v1"), nil); got != "example.com/a2a-gateway:v1" {
		t.Errorf("a roll to the release image dropped the record: %q", got)
	}
}

func TestTheGatewayScalesOutOnlyOnceElected(t *testing.T) {
	agent := a2aTestAgent()
	dep := buildA2AGatewayDeployment(agent)
	scaleOutA2AGateway(dep, agent, "")
	if *dep.Spec.Replicas != 1 || dep.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType {
		t.Fatalf("unelected: %d replicas, %s", *dep.Spec.Replicas, dep.Spec.Strategy.Type)
	}

	dep = buildA2AGatewayDeployment(agent)
	scaleOutA2AGateway(dep, agent, "example.com/a2a-gateway:v1")
	ru := dep.Spec.Strategy.RollingUpdate
	if *dep.Spec.Replicas != 2 || dep.Spec.Strategy.Type != appsv1.RollingUpdateDeploymentStrategyType ||
		ru.MaxSurge.IntValue() != 1 || ru.MaxUnavailable.IntValue() != 0 {
		t.Fatalf("elected: %d replicas, %+v", *dep.Spec.Replicas, dep.Spec.Strategy)
	}

	one := withGatewayReplicas(a2aTestAgent(), 1)
	dep = buildA2AGatewayDeployment(one)
	scaleOutA2AGateway(dep, one, "example.com/a2a-gateway:v1")
	ru = dep.Spec.Strategy.RollingUpdate
	if *dep.Spec.Replicas != 1 || ru.MaxSurge.IntValue() != 0 || ru.MaxUnavailable.IntValue() != 1 {
		t.Fatalf("elected, one asked: %d replicas, %+v", *dep.Spec.Replicas, dep.Spec.Strategy)
	}
}

func TestTheGatewaySpreadsOverZonesAndDrainsOneAtATime(t *testing.T) {
	agent := a2aTestAgent()
	pod := buildA2AGatewayDeployment(agent).Spec.Template
	if len(pod.Spec.TopologySpreadConstraints) != 1 {
		t.Fatalf("%d spread constraints", len(pod.Spec.TopologySpreadConstraints))
	}
	spread := pod.Spec.TopologySpreadConstraints[0]
	if spread.TopologyKey != "topology.kubernetes.io/zone" || spread.WhenUnsatisfiable != corev1.ScheduleAnyway ||
		spread.LabelSelector.MatchLabels["app"] != pod.Labels["app"] {
		t.Errorf("spread = %+v", spread)
	}
	pdb := buildA2AGatewayPDB(agent)
	if pdb.Spec.MaxUnavailable.IntValue() != 1 || *pdb.Spec.UnhealthyPodEvictionPolicy != policyv1.AlwaysAllow ||
		pdb.Spec.Selector.MatchLabels["app"] != pod.Labels["app"] {
		t.Errorf("budget = %+v", pdb.Spec)
	}
}

// TestAnElectedGatewayScalesOutOnTheNextPass: the first render is one
// replica with Recreate; once a gateway pod on the rendered image holds the
// Lease, the next pass records it and renders two with a surge.
func TestAnElectedGatewayScalesOutOnTheNextPass(t *testing.T) {
	const image = "example.com/a2a-gateway:elected"
	t.Setenv(a2aGatewayImageEnvVar, image)
	t.Setenv(a2aGatewayElectionEnvVar, "")
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().
		WithScheme(setupScheme()).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	state, err := r.reconcileA2A(ctx, agent, homeChannelRecord{})
	if err != nil {
		t.Fatal(err)
	}
	dep := &appsv1.Deployment{}
	depKey := types.NamespacedName{Namespace: agent.Namespace, Name: a2aGatewayName(agent)}
	if err := cl.Get(ctx, depKey, dep); err != nil {
		t.Fatal(err)
	}
	if *dep.Spec.Replicas != 1 || dep.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType {
		t.Fatalf("before an election: %d replicas, %s", *dep.Spec.Replicas, dep.Spec.Strategy.Type)
	}
	if gatewayElectedImage(agent, state.gatewayElected) != "" {
		t.Fatal("an election recorded before any pod held the Lease")
	}

	holder := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent-a2a-gateway-abc", Namespace: agent.Namespace,
			Labels: map[string]string{"app": a2aGatewayName(agent)}},
		Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: a2aGatewayContainerName, Image: image}}},
	}
	if err := cl.Create(ctx, holder); err != nil {
		t.Fatal(err)
	}
	takeLease(t, ctx, cl, agent, holder.Name)

	state, err = r.reconcileA2A(ctx, agent, homeChannelRecord{})
	if err != nil {
		t.Fatal(err)
	}
	if got := gatewayElectedImage(agent, state.gatewayElected); got != image {
		t.Fatalf("elected image = %q, want %q", got, image)
	}
	if err := cl.Get(ctx, depKey, dep); err != nil {
		t.Fatal(err)
	}
	if *dep.Spec.Replicas != 2 || dep.Spec.Strategy.Type != appsv1.RollingUpdateDeploymentStrategyType {
		t.Fatalf("after an election: %d replicas, %s", *dep.Spec.Replicas, dep.Spec.Strategy.Type)
	}
	pdb := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, depKey, pdb); err != nil {
		t.Fatalf("no gateway budget: %v", err)
	}
	if !metav1.IsControlledBy(pdb, agent) {
		t.Error("the gateway budget isn't owned by the PlatformAgent, so the teardown would skip it")
	}

	// The status writer carries it as the GatewayElected condition.
	setGatewayElectedCondition(agent, gatewayElectedImage(agent, state.gatewayElected), metav1.Now())
	if c := meta.FindStatusCondition(agent.Status.Conditions, gatewayElectedConditionType); c == nil || c.Message != image {
		t.Fatalf("condition = %+v", c)
	}
}

// TestALeaseHolderOutsideTheGatewayProvesNothing: a holder that isn't one of
// this gateway's pods, or no holder at all, records no election.
func TestALeaseHolderOutsideTheGatewayProvesNothing(t *testing.T) {
	t.Setenv(a2aGatewayElectionEnvVar, "")
	agent := a2aTestAgent()
	stranger := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "someone-else", Namespace: agent.Namespace, Labels: map[string]string{"app": "other"}},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: a2aGatewayContainerName, Image: "x"}}},
	}
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent, stranger).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	ctx := context.Background()
	lease := buildA2AGatewayLease(agent)
	if err := cl.Create(ctx, lease); err != nil {
		t.Fatal(err)
	}
	if got := r.observeA2AGatewayElection(ctx, agent); got != nil {
		t.Errorf("no holder: %q", *got)
	}
	takeLease(t, ctx, cl, agent, stranger.Name)
	if got := r.observeA2AGatewayElection(ctx, agent); got != nil {
		t.Errorf("a stranger's pod: %q", *got)
	}
}

func takeLease(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent, holder string) {
	t.Helper()
	lease := &coordinationv1.Lease{}
	if err := cl.Get(ctx, gatewayLeaseKey(agent), lease); err != nil {
		t.Fatal(err)
	}
	lease.Spec.HolderIdentity = ptr.To(holder)
	if err := cl.Update(ctx, lease); err != nil {
		t.Fatal(err)
	}
}

// TestTheElectionReachesTheCRStatus: a full reconcile writes the
// GatewayElected condition once a gateway pod on the rendered image holds
// the Lease, and the next renders two replicas from it.
func TestTheElectionReachesTheCRStatus(t *testing.T) {
	const image = "example.com/a2a-gateway:elected"
	t.Setenv(a2aGatewayImageEnvVar, image)
	t.Setenv(a2aGatewayElectionEnvVar, "")
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().
		WithScheme(setupScheme()).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	theCalloutIsServing(t, ctx, cl, r, agent)
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	holder := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent-a2a-gateway-abc", Namespace: agent.Namespace,
			Labels: map[string]string{"app": a2aGatewayName(agent)}},
		Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: a2aGatewayContainerName, Image: image}}},
	}
	if err := cl.Create(ctx, holder); err != nil {
		t.Fatal(err)
	}
	takeLease(t, ctx, cl, agent, holder.Name)
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal(err)
	}
	live := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, live); err != nil {
		t.Fatal(err)
	}
	if c := meta.FindStatusCondition(live.Status.Conditions, gatewayElectedConditionType); c == nil || c.Message != image {
		t.Fatalf("GatewayElected = %+v, want the holder's image", c)
	}
	// The holder's pod gone, the record carries the scale-out on.
	if err := cl.Delete(ctx, holder); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal(err)
	}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aGatewayName(agent)}, dep); err != nil {
		t.Fatal(err)
	}
	if *dep.Spec.Replicas != 2 {
		t.Fatalf("replicas after the record = %d, want 2", *dep.Spec.Replicas)
	}
}

// TestTheElectionReachesTheCRStatusWhileParked: a pass parked on a Degraded
// refusal after the render writes its conditions through
// syncA2AConditions, which carries the election too.
func TestTheElectionReachesTheCRStatusWhileParked(t *testing.T) {
	const image = "example.com/a2a-gateway:elected"
	t.Setenv(a2aGatewayImageEnvVar, image)
	t.Setenv(a2aGatewayElectionEnvVar, "")
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().
		WithScheme(setupScheme()).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	ctx := context.Background()
	// A first pass brings every other condition current, so the second has
	// only the election to write.
	if err := r.syncA2AConditions(ctx, agent, a2aProvisionState{}); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), agent); err != nil {
		t.Fatal(err)
	}
	if err := r.syncA2AConditions(ctx, agent, a2aProvisionState{gatewayElected: ptr.To(image)}); err != nil {
		t.Fatal(err)
	}
	live := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), live); err != nil {
		t.Fatal(err)
	}
	if c := meta.FindStatusCondition(live.Status.Conditions, gatewayElectedConditionType); c == nil || c.Message != image {
		t.Fatalf("GatewayElected = %+v, want the holder's image", c)
	}
}

// TestARecordAloneDoesntScaleOutAnUnelectedGateway: a GatewayElected record
// carried from an earlier operator (rolled back, then forward) over a
// gateway running unelected with Recreate doesn't scale it out; an election
// read this pass does, and a record keeps a gateway already scaled out with
// the election's env.
func TestARecordAloneDoesntScaleOutAnUnelectedGateway(t *testing.T) {
	const image = "example.com/a2a-gateway:v1"
	unelected := buildA2AGatewayDeployment(a2aTestAgent())
	unelected.Spec.Template.Spec.Containers[0].Env = nil
	if a2aGatewayScaleOutAllowed(nil, image, unelected, true) {
		t.Error("a carried record scaled out a Recreate gateway with no election env")
	}
	firstRoll := buildA2AGatewayDeployment(a2aTestAgent())
	firstRoll.Spec.Template.Spec.Containers[0].Env = append(firstRoll.Spec.Template.Spec.Containers[0].Env,
		corev1.EnvVar{Name: a2aGatewayLeaseEnvVar, Value: "x"})
	if a2aGatewayScaleOutAllowed(nil, image, firstRoll, true) {
		t.Error("a carried record scaled out a gateway still on Recreate")
	}
	if !a2aGatewayScaleOutAllowed(ptr.To(image), image, unelected, true) {
		t.Error("an election read this pass didn't scale out")
	}
	if a2aGatewayScaleOutAllowed(ptr.To("example.com/a2a-gateway:v0"), image, unelected, true) {
		t.Error("an election read on another image scaled out")
	}
	t.Setenv(a2aGatewayElectionEnvVar, "")
	agent := a2aTestAgent()
	scaled := buildA2AGatewayDeployment(agent)
	scaleOutA2AGateway(scaled, agent, image)
	if !a2aGatewayScaleOutAllowed(nil, image, scaled, true) {
		t.Error("a carried record didn't keep a scaled-out elected gateway")
	}
	noEnv := scaled.DeepCopy()
	var kept []corev1.EnvVar
	for _, e := range noEnv.Spec.Template.Spec.Containers[0].Env {
		if e.Name != a2aGatewayLeaseEnvVar {
			kept = append(kept, e)
		}
	}
	noEnv.Spec.Template.Spec.Containers[0].Env = kept
	if a2aGatewayScaleOutAllowed(nil, image, noEnv, true) {
		t.Error("a carried record kept a rolling gateway with no election env")
	}
	if a2aGatewayScaleOutAllowed(nil, image, scaled, false) {
		t.Error("a carried record scaled out with no gateway to keep")
	}
}

// TestAHeldGatewayLooksForItsElectionAgain: while the gateway is held at one
// replica with more asked for, the pass comes back to look for the election,
// which no watch reports.
func TestAHeldGatewayLooksForItsElectionAgain(t *testing.T) {
	t.Setenv(a2aGatewayElectionEnvVar, "")
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().
		WithScheme(setupScheme()).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	state, err := r.reconcileA2A(ctx, agent, homeChannelRecord{})
	if err != nil {
		t.Fatal(err)
	}
	if state.leaseRequeue <= 0 || state.leaseRequeue > a2aGatewayElectionRecheck {
		t.Fatalf("requeue while held = %s, want within %s", state.leaseRequeue, a2aGatewayElectionRecheck)
	}
	one := withGatewayReplicas(a2aTestAgent(), 1)
	cl = fake.NewClientBuilder().
		WithScheme(setupScheme()).
		WithObjects(one).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r = &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	theCalloutIsServing(t, ctx, cl, r, one)
	if state, err = r.reconcileA2A(ctx, one, homeChannelRecord{}); err != nil {
		t.Fatal(err)
	}
	if state.leaseRequeue != 0 {
		t.Fatalf("one replica asked, requeue = %s, want none", state.leaseRequeue)
	}
}

// TestACarriedRecordDoesntScaleOutAFreshRender: the CR records an election
// (an operator rolled back and forward), but no pod holds the Lease and the
// gateway isn't scaled out, so the render stays at one replica.
func TestACarriedRecordDoesntScaleOutAFreshRender(t *testing.T) {
	const image = "example.com/a2a-gateway:elected"
	t.Setenv(a2aGatewayImageEnvVar, image)
	t.Setenv(a2aGatewayElectionEnvVar, "")
	agent := withGatewayElected(a2aTestAgent(), image)
	cl := fake.NewClientBuilder().
		WithScheme(setupScheme()).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	if _, err := r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
		t.Fatal(err)
	}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aGatewayName(agent)}, dep); err != nil {
		t.Fatal(err)
	}
	if *dep.Spec.Replicas != 1 || dep.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType {
		t.Fatalf("a carried record scaled out: %d replicas, %s", *dep.Spec.Replicas, dep.Spec.Strategy.Type)
	}
}
