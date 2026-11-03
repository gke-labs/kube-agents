package controller

// The bus's config gate, from the side that waits on it.
//
// A running bus moves to a new config only once every auth callout replica is
// on the current spec and no pod of an older one is left
// (a2aBusWaitsForCallout). Every client reconnects when the bus restarts, and
// whichever callout replica answers a reconnect places it by the identity map
// that replica holds, so a replica still on the previous map would place it by
// that map until it next reconnects. A fresh install has no bus to hold.

import (
	"context"
	"fmt"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	ctrl "sigs.k8s.io/controller-runtime"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// calloutReplicaSet is a ReplicaSet the Deployment controller would have made
// for the callout: controlled by it, carrying its selector and the given
// template hash and revision.
func calloutReplicaSet(t *testing.T, ctx context.Context, cl client.Client, dep *appsv1.Deployment, hash, revision string) {
	t.Helper()
	labels := map[string]string{appsv1.DefaultDeploymentUniqueLabelKey: hash}
	for k, v := range dep.Spec.Selector.MatchLabels {
		labels[k] = v
	}
	rs := &appsv1.ReplicaSet{
		ObjectMeta: metav1.ObjectMeta{
			Name:            dep.Name + "-" + hash,
			Namespace:       dep.Namespace,
			Labels:          labels,
			Annotations:     map[string]string{a2aDeploymentRevisionAnnotation: revision},
			OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(dep, appsv1.SchemeGroupVersion.WithKind("Deployment"))},
		},
		Spec: appsv1.ReplicaSetSpec{Selector: &metav1.LabelSelector{MatchLabels: labels}},
	}
	if err := cl.Create(ctx, rs); err != nil {
		t.Fatalf("create callout ReplicaSet %s: %v", rs.Name, err)
	}
}

// calloutPod is a callout pod on the template with the given hash. A pod
// created with a finalizer and then deleted stays listed, terminating, the way
// a real one does through its grace period.
func calloutPod(t *testing.T, ctx context.Context, cl client.Client, dep *appsv1.Deployment, name, hash string, terminating bool) *corev1.Pod {
	t.Helper()
	labels := map[string]string{appsv1.DefaultDeploymentUniqueLabelKey: hash}
	for k, v := range dep.Spec.Selector.MatchLabels {
		labels[k] = v
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: dep.Namespace, Labels: labels},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "callout", Image: "callout"}}},
	}
	if terminating {
		pod.Finalizers = []string{"test.kubeagents.x-k8s.io/terminating"}
	}
	if err := cl.Create(ctx, pod); err != nil {
		t.Fatalf("create callout pod %s: %v", name, err)
	}
	if terminating {
		if err := cl.Delete(ctx, pod); err != nil {
			t.Fatalf("start terminating callout pod %s: %v", name, err)
		}
	}
	return pod
}

// theCalloutIsRolledOut puts the callout into its steady state: every replica
// ready on the current spec, one ReplicaSet, and its pods. A test that changes
// the bus's config after install needs it, since the bus waits for exactly
// this (a2aBusWaitsForCallout) and the fake client runs no Deployment
// controller to reach it.
func theCalloutIsRolledOut(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	reportCalloutServing(t, ctx, cl, agent)
	dep := getCallout(t, ctx, cl, agent)
	calloutReplicaSet(t, ctx, cl, dep, "steady", "1")
	for i := int32(0); i < ptr.Deref(dep.Spec.Replicas, 1); i++ {
		calloutPod(t, ctx, cl, dep, fmt.Sprintf("callout-steady-%d", i), "steady", false)
	}
}

func getCallout(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	t.Helper()
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aCalloutName(agent), Namespace: agent.Namespace}, dep); err != nil {
		t.Fatalf("get callout Deployment: %v", err)
	}
	return dep
}

func busConfigHash(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent) string {
	t.Helper()
	sts := &appsv1.StatefulSet{}
	if err := cl.Get(ctx, types.NamespacedName{Name: buildA2ANATSStatefulSet(agent, "").Name, Namespace: agent.Namespace}, sts); err != nil {
		t.Fatalf("get bus StatefulSet: %v", err)
	}
	return sts.Spec.Template.Annotations[a2aConfigHashAnnotation]
}

// putTheBusOnAPreviousConfig rewrites the live StatefulSet's config hash and
// the config Secret, the state a bus is in when the operator rolls to a
// release whose render differs.
func putTheBusOnAPreviousConfig(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	sts := &appsv1.StatefulSet{}
	if err := cl.Get(ctx, types.NamespacedName{Name: buildA2ANATSStatefulSet(agent, "").Name, Namespace: agent.Namespace}, sts); err != nil {
		t.Fatalf("get bus StatefulSet: %v", err)
	}
	sts.Spec.Template.Annotations[a2aConfigHashAnnotation] = "previous"
	if err := cl.Update(ctx, sts); err != nil {
		t.Fatalf("put the bus on a previous config hash: %v", err)
	}
	cfg := &corev1.Secret{}
	key := client.ObjectKeyFromObject(buildA2ANATSConfigSecret(agent, a2aFullCreds("a", "1"), a2aTestCalloutKeys(t)))
	if err := cl.Get(ctx, key, cfg); err != nil {
		t.Fatalf("get bus config Secret: %v", err)
	}
	cfg.Data["nats.conf"] = []byte("previous")
	if err := cl.Update(ctx, cfg); err != nil {
		t.Fatalf("put the bus on a previous config: %v", err)
	}
}

func busConfigText(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent) string {
	t.Helper()
	cfg := &corev1.Secret{}
	key := client.ObjectKeyFromObject(buildA2ANATSConfigSecret(agent, a2aFullCreds("a", "1"), a2aTestCalloutKeys(t)))
	if err := cl.Get(ctx, key, cfg); err != nil {
		t.Fatalf("get bus config Secret: %v", err)
	}
	return string(cfg.Data["nats.conf"])
}

func busGatePass(t *testing.T, ctx context.Context, r *PlatformAgentReconciler, req ctrl.Request, what string) ctrl.Result {
	t.Helper()
	res, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile (%s): %v", what, err)
	}
	return res
}

// A fresh install has no bus to hold: the StatefulSet renders on the passes
// where the callout has not reported a single replica.
func TestAFreshInstallRendersTheBusWithoutWaitingForTheCallout(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		busGatePass(t, ctx, r, req, "fresh install")
	}
	if busConfigHash(t, ctx, cl, agent) == "" {
		t.Fatal("the bus StatefulSet rendered with no config hash")
	}
	if dep := getCallout(t, ctx, cl, agent); dep.Status.ReadyReplicas != 0 {
		t.Fatalf("the rig's callout reports %d ready replicas; this test is about a callout that has reported none", dep.Status.ReadyReplicas)
	}
}

// The upgrade: a bus on a previous config, a callout rolling to a new spec.
// The bus keeps its config while the roll is short of every replica, and
// while a pod of the previous ReplicaSet is still terminating, and moves on
// the pass after the last one goes.
func TestAChangedBusConfigWaitsForTheWholeCalloutRollout(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		busGatePass(t, ctx, r, req, "install")
	}
	reportCalloutServing(t, ctx, cl, agent)
	dep := getCallout(t, ctx, cl, agent)
	replicas := ptr.Deref(dep.Spec.Replicas, 1)
	if replicas < 2 {
		t.Fatalf("the callout renders at %d replicas; the mid-roll states below need two", replicas)
	}
	calloutReplicaSet(t, ctx, cl, dep, "old", "1")
	calloutPod(t, ctx, cl, dep, "callout-old-0", "old", false)
	old1 := calloutPod(t, ctx, cl, dep, "callout-old-1", "old", false)
	completeTheProvisionJob(t, ctx, cl, agent)
	settled := busGatePass(t, ctx, r, req, "settled")
	rendered := busConfigHash(t, ctx, cl, agent)
	renderedConf := busConfigText(t, ctx, cl, agent)

	putTheBusOnAPreviousConfig(t, ctx, cl, agent)

	// Mid-roll: one replica on the new spec.
	reportCalloutStatus(t, ctx, cl, agent, replicas, replicas, 1)
	completeTheProvisionJob(t, ctx, cl, agent)
	res := busGatePass(t, ctx, r, req, "callout mid-roll")
	if got := busConfigHash(t, ctx, cl, agent); got != "previous" {
		t.Errorf("the bus moved to config %q while one of %d callout replicas was on the current spec", got, replicas)
	}
	if busConfigText(t, ctx, cl, agent) != "previous" {
		t.Error("the bus config Secret moved while the StatefulSet was held; a bus pod restarted meanwhile would start on it")
	}
	if res.RequeueAfter == 0 || res.RequeueAfter >= settled.RequeueAfter {
		t.Errorf("a held bus requeued after %v, no sooner than the settled install's %v; nothing else guarantees a timely pass that sees the roll finish", res.RequeueAfter, settled.RequeueAfter)
	}

	// Every replica counted on the new spec, but a pod of the previous
	// ReplicaSet is still terminating, and still subscribed.
	reportCalloutServing(t, ctx, cl, agent)
	dep = getCallout(t, ctx, cl, agent)
	calloutReplicaSet(t, ctx, cl, dep, "new", "2")
	if err := cl.Delete(ctx, old1); err != nil {
		t.Fatalf("delete callout-old-1: %v", err)
	}
	old0 := &corev1.Pod{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "callout-old-0", Namespace: agent.Namespace}, old0); err != nil {
		t.Fatalf("get callout-old-0: %v", err)
	}
	if err := cl.Delete(ctx, old0); err != nil {
		t.Fatalf("delete callout-old-0: %v", err)
	}
	calloutPod(t, ctx, cl, dep, "callout-new-0", "new", false)
	calloutPod(t, ctx, cl, dep, "callout-new-1", "new", false)
	stale := calloutPod(t, ctx, cl, dep, "callout-old-2", "old", true)
	completeTheProvisionJob(t, ctx, cl, agent)
	busGatePass(t, ctx, r, req, "an old pod terminating")
	if got := busConfigHash(t, ctx, cl, agent); got != "previous" {
		t.Errorf("the bus moved to config %q while a pod of the previous callout ReplicaSet was terminating", got)
	}

	// The last old pod is gone.
	if err := cl.Get(ctx, client.ObjectKeyFromObject(stale), stale); err != nil {
		t.Fatalf("get callout-old-2: %v", err)
	}
	stale.Finalizers = nil
	if err := cl.Update(ctx, stale); err != nil {
		t.Fatalf("finish terminating callout-old-2: %v", err)
	}
	completeTheProvisionJob(t, ctx, cl, agent)
	busGatePass(t, ctx, r, req, "callout fully rolled")
	if got := busConfigHash(t, ctx, cl, agent); got != rendered {
		t.Errorf("the bus config hash = %q once every callout replica is on the current spec, want the rendered %q", got, rendered)
	}
	if busConfigText(t, ctx, cl, agent) != renderedConf {
		t.Error("the bus config Secret did not move with the StatefulSet")
	}
}

func TestACalloutIsFullyRolledOutOnlyWithEveryReplicaOnTheCurrentSpec(t *testing.T) {
	dep := func(gen, observed int64, desired *int32, replicas, ready, updated int32) *appsv1.Deployment {
		return &appsv1.Deployment{
			ObjectMeta: metav1.ObjectMeta{Generation: gen},
			Spec:       appsv1.DeploymentSpec{Replicas: desired},
			Status:     appsv1.DeploymentStatus{ObservedGeneration: observed, Replicas: replicas, ReadyReplicas: ready, UpdatedReplicas: updated},
		}
	}
	for _, tc := range []struct {
		name    string
		dep     *appsv1.Deployment
		applied int64
		want    bool
	}{
		{"every replica ready and updated", dep(3, 3, ptr.To(int32(2)), 2, 2, 2), 3, true},
		{"desired unset means one", dep(3, 3, nil, 1, 1, 1), 3, true},
		{"one replica still on the previous spec", dep(3, 3, ptr.To(int32(2)), 2, 2, 1), 3, false},
		{"a surge pod beside the desired count", dep(3, 3, ptr.To(int32(2)), 3, 3, 2), 3, false},
		{"one replica not ready", dep(3, 3, ptr.To(int32(2)), 2, 1, 2), 3, false},
		{"the controller has not observed the spec", dep(3, 2, ptr.To(int32(2)), 2, 2, 2), 3, false},
		{"a copy older than this pass's apply", dep(2, 2, ptr.To(int32(2)), 2, 2, 2), 3, false},
	} {
		if got := a2aCalloutFullyRolledOut(tc.dep, tc.applied); got != tc.want {
			t.Errorf("%s: a2aCalloutFullyRolledOut = %v, want %v", tc.name, got, tc.want)
		}
	}
}
