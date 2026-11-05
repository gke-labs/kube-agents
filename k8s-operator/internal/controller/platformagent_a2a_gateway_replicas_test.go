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
