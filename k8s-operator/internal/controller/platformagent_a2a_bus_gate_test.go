package controller

// The bus's config gate, from the side that waits on it.
//
// A running bus moves to a new config only once the callout's current spec is
// observed and no pod of an older ReplicaSet can still answer
// (a2aBusWaitsForCallout). Readiness is not part of it. Every client reconnects when the bus restarts, and
// whichever callout replica answers a reconnect places it by the identity map
// that replica holds, so a replica still on the previous map would place it by
// that map until it next reconnects. A fresh install has no bus to hold.

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	ctrl "sigs.k8s.io/controller-runtime"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// calloutReplicaSet is a ReplicaSet the Deployment controller would have made
// for the callout: controlled by it, carrying its selector and the given
// template hash and revision.
func calloutReplicaSet(t *testing.T, ctx context.Context, cl client.Client, dep *appsv1.Deployment, hash, revision string) *appsv1.ReplicaSet {
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
	return rs
}

// calloutPod is a callout pod controlled by rs, on its template hash; a nil rs
// is a pod that only carries the labels, with hash as its template hash. A pod
// created with a finalizer and then deleted stays listed, terminating, the way
// a real one does through its grace period.
func calloutPod(t *testing.T, ctx context.Context, cl client.Client, dep *appsv1.Deployment, rs *appsv1.ReplicaSet, name, hash string, terminating bool) *corev1.Pod {
	t.Helper()
	if rs != nil {
		hash = rs.Labels[appsv1.DefaultDeploymentUniqueLabelKey]
	}
	labels := map[string]string{appsv1.DefaultDeploymentUniqueLabelKey: hash}
	for k, v := range dep.Spec.Selector.MatchLabels {
		labels[k] = v
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: dep.Namespace, Labels: labels},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "callout", Image: "callout"}}},
	}
	if rs != nil {
		pod.OwnerReferences = []metav1.OwnerReference{*metav1.NewControllerRef(rs, appsv1.SchemeGroupVersion.WithKind("ReplicaSet"))}
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
	rs := calloutReplicaSet(t, ctx, cl, dep, "steady", "1")
	for i := int32(0); i < ptr.Deref(dep.Spec.Replicas, 1); i++ {
		calloutPod(t, ctx, cl, dep, rs, fmt.Sprintf("callout-steady-%d", i), "", false)
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
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aNATSName(agent), Namespace: agent.Namespace}, sts); err != nil {
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
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aNATSName(agent), Namespace: agent.Namespace}, sts); err != nil {
		t.Fatalf("get bus StatefulSet: %v", err)
	}
	sts.Spec.Template.Annotations[a2aConfigHashAnnotation] = "previous"
	if err := cl.Update(ctx, sts); err != nil {
		t.Fatalf("put the bus on a previous config hash: %v", err)
	}
	cfg := &corev1.Secret{}
	key := types.NamespacedName{Name: a2aNATSConfigSecretName(agent), Namespace: agent.Namespace}
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
	key := types.NamespacedName{Name: a2aNATSConfigSecretName(agent), Namespace: agent.Namespace}
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
// The bus keeps its config while pods of the previous ReplicaSet are serving,
// and while one is still terminating, and moves on the pass after the last
// one goes.
func TestAChangedBusConfigWaitsUntilNoPreviousCalloutPodCanAnswer(t *testing.T) {
	t.Setenv(a2aMCPDoorEnvVar, "true")
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
	oldRS := calloutReplicaSet(t, ctx, cl, dep, "old", "1")
	calloutPod(t, ctx, cl, dep, oldRS, "callout-old-0", "", false)
	old1 := calloutPod(t, ctx, cl, dep, oldRS, "callout-old-1", "", false)
	completeTheProvisionJob(t, ctx, cl, agent)
	settled := busGatePass(t, ctx, r, req, "settled")
	rendered := busConfigHash(t, ctx, cl, agent)
	renderedConf := busConfigText(t, ctx, cl, agent)

	putTheBusOnAPreviousConfig(t, ctx, cl, agent)
	// The MCP server as the previous release rendered it, dialing a user the
	// held bus has; a render for this one names a user only the new config
	// has, so it waits with the bus.
	mcpUser := func() string {
		t.Helper()
		dep := &appsv1.Deployment{}
		if err := cl.Get(ctx, types.NamespacedName{Name: a2aMCPName(agent), Namespace: agent.Namespace}, dep); err != nil {
			t.Fatalf("get the MCP Deployment: %v", err)
		}
		for _, e := range dep.Spec.Template.Spec.Containers[0].Env {
			if e.Name == a2aMCPEnvBusUser {
				return e.Value
			}
		}
		return ""
	}
	rendered0 := mcpUser()
	{
		dep := &appsv1.Deployment{}
		if err := cl.Get(ctx, types.NamespacedName{Name: a2aMCPName(agent), Namespace: agent.Namespace}, dep); err != nil {
			t.Fatalf("get the MCP Deployment: %v", err)
		}
		for i, e := range dep.Spec.Template.Spec.Containers[0].Env {
			if e.Name == a2aMCPEnvBusUser {
				dep.Spec.Template.Spec.Containers[0].Env[i].Value = "previous-user"
			}
		}
		if err := cl.Update(ctx, dep); err != nil {
			t.Fatalf("put the MCP Deployment on the previous render: %v", err)
		}
	}

	// Mid-roll: the new ReplicaSet has its first pod, and both pods of
	// the previous one are still serving.
	reportCalloutStatus(t, ctx, cl, agent, replicas+1, replicas+1, 1)
	newRS := calloutReplicaSet(t, ctx, cl, getCallout(t, ctx, cl, agent), "new", "2")
	calloutPod(t, ctx, cl, dep, newRS, "callout-new-0", "", false)
	completeTheProvisionJob(t, ctx, cl, agent)
	res := busGatePass(t, ctx, r, req, "callout mid-roll")
	if got := busConfigHash(t, ctx, cl, agent); got != "previous" {
		t.Errorf("the bus moved to config %q while pods of the previous callout ReplicaSet were serving", got)
	}
	if busConfigText(t, ctx, cl, agent) != "previous" {
		t.Error("the bus config Secret moved while the StatefulSet was held; a bus pod restarted meanwhile would start on it")
	}
	if got := mcpUser(); got != "previous-user" {
		t.Errorf("the MCP Deployment was re-rendered to dial as %q while the bus was held on a config without that user", got)
	}
	if res.RequeueAfter == 0 || res.RequeueAfter >= settled.RequeueAfter {
		t.Errorf("a held bus requeued after %v, no sooner than the settled install's %v; nothing else guarantees a timely pass that sees the roll finish", res.RequeueAfter, settled.RequeueAfter)
	}

	// Every replica counted on the new spec, but a pod of the previous
	// ReplicaSet is still terminating, and still subscribed.
	reportCalloutServing(t, ctx, cl, agent)
	dep = getCallout(t, ctx, cl, agent)
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
	calloutPod(t, ctx, cl, dep, newRS, "callout-new-1", "", false)
	// A pod that only carries the callout's labels, on an old hash, does
	// not count.
	calloutPod(t, ctx, cl, dep, nil, "lookalike", "old", false)
	stale := calloutPod(t, ctx, cl, dep, oldRS, "callout-old-2", "", true)
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
	busGatePass(t, ctx, r, req, "after the bus moved")
	if got := mcpUser(); got != rendered0 || got == "" {
		t.Errorf("the MCP Deployment dials as %q once the bus has moved, want the current render's %q", got, rendered0)
	}
}

func TestTheCalloutSpecIsObservedOnlyAtTheAppliedGeneration(t *testing.T) {
	dep := func(gen, observed int64) *appsv1.Deployment {
		return &appsv1.Deployment{
			ObjectMeta: metav1.ObjectMeta{Generation: gen},
			Status:     appsv1.DeploymentStatus{ObservedGeneration: observed},
		}
	}
	for _, tc := range []struct {
		name    string
		dep     *appsv1.Deployment
		applied int64
		want    bool
	}{
		{"observed at the applied generation", dep(3, 3), 3, true},
		{"the controller has not observed the spec", dep(3, 2), 3, false},
		{"a copy older than this pass's apply", dep(2, 2), 3, false},
	} {
		if got := a2aCalloutSpecObserved(tc.dep, tc.applied); got != tc.want {
			t.Errorf("%s: a2aCalloutSpecObserved = %v, want %v", tc.name, got, tc.want)
		}
	}
}

// The bounds on the wait. Older pods that answer nothing do not hold the bus:
// one stuck terminating past its deletion deadline, one finished, one that is
// only labelled like the callout. A rollout the Deployment controller has
// given up on does not hold it either, so a credential repair or a key change
// in the bus's config waits at most the callout's progress deadline.
func TestTheBusWaitIsBoundedByWhatCanStillAnswer(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		busGatePass(t, ctx, r, req, "install")
	}
	reportCalloutServing(t, ctx, cl, agent)
	dep := getCallout(t, ctx, cl, agent)
	oldRS := calloutReplicaSet(t, ctx, cl, dep, "old", "1")
	newRS := calloutReplicaSet(t, ctx, cl, dep, "new", "2")
	for i := int32(0); i < ptr.Deref(dep.Spec.Replicas, 1); i++ {
		calloutPod(t, ctx, cl, dep, newRS, fmt.Sprintf("callout-new-%d", i), "", false)
	}
	stuck := calloutPod(t, ctx, cl, dep, oldRS, "callout-old-stuck", "", true)
	sts := buildA2ANATSStatefulSet(agent, "next")

	holds := func(what string) bool {
		t.Helper()
		held, err := r.a2aBusWaitsForCallout(ctx, agent, sts, 0)
		if err != nil {
			t.Fatalf("a2aBusWaitsForCallout (%s): %v", what, err)
		}
		return held
	}
	if !holds("a pod of the old ReplicaSet terminating, inside its deadline") {
		t.Fatal("the rig does not hold to begin with; the cases below would pass on nothing")
	}

	if err := cl.Get(ctx, client.ObjectKeyFromObject(stuck), stuck); err != nil {
		t.Fatalf("get the stuck pod: %v", err)
	}
	past := stuck.DeletionTimestamp.Add(a2aTerminatingCalloutSlack + time.Second)
	defer func(was func() time.Time) { a2aBusGateNow = was }(a2aBusGateNow)
	a2aBusGateNow = func() time.Time { return past }
	if holds("the old pod past its deletion deadline") {
		t.Error("a pod past its deletion deadline held the bus; a finalizer could pin it")
	}
	a2aBusGateNow = time.Now

	stuck.Finalizers = nil
	if err := cl.Update(ctx, stuck); err != nil {
		t.Fatalf("finish the stuck pod: %v", err)
	}
	done := calloutPod(t, ctx, cl, dep, oldRS, "callout-old-done", "", false)
	done.Status.Phase = corev1.PodFailed
	if err := cl.Status().Update(ctx, done); err != nil {
		t.Fatalf("mark callout-old-done failed: %v", err)
	}
	calloutPod(t, ctx, cl, dep, nil, "lookalike", "old", false)
	if holds("a failed old pod and a look-alike") {
		t.Error("a finished pod or a pod that only carries the callout's labels held the bus")
	}

	live := calloutPod(t, ctx, cl, dep, oldRS, "callout-old-live", "", false)
	if !holds("a running pod of the old ReplicaSet") {
		t.Fatalf("a running pod of the old ReplicaSet (%s) did not hold the bus", live.Name)
	}
	dep = getCallout(t, ctx, cl, agent)
	dep.Status.Conditions = []appsv1.DeploymentCondition{{Type: appsv1.DeploymentProgressing, Status: corev1.ConditionFalse, Reason: a2aProgressDeadlineExceeded}}
	if err := cl.Status().Update(ctx, dep); err != nil {
		t.Fatalf("report the callout rollout stalled: %v", err)
	}
	if holds("a stalled callout rollout") {
		t.Error("a callout rollout past its progress deadline held the bus; its config could be carrying a credential repair")
	}

	// The same condition on a copy the controller has not observed yet
	// describes the rollout before this one, and does not release it.
	dep = getCallout(t, ctx, cl, agent)
	dep.Status.ObservedGeneration = dep.Generation - 1
	if err := cl.Status().Update(ctx, dep); err != nil {
		t.Fatalf("report the callout spec unobserved: %v", err)
	}
	if !holds("a stall reported before the current spec was observed") {
		t.Error("a stall from before the current spec was observed released the bus")
	}
}

// A bus whose pod has not been Ready for a2aNATSStuckPodGrace, while no
// callout replica is Ready, is not held: it serves nobody, and no callout pod
// becomes Ready without it. A missing pod, a Ready one, one inside the grace,
// one terminating, one the StatefulSet does not control, or a Ready callout
// replica still holds, and records no event. A callout with no older pods
// does not hold, and records no event either.
func TestADownBusIsNotHeldForTheCallout(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	recorder := record.NewFakeRecorder(8)
	r.Recorder = recorder
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		busGatePass(t, ctx, r, req, "install")
	}
	reportCalloutServing(t, ctx, cl, agent)
	dep := getCallout(t, ctx, cl, agent)
	oldRS := calloutReplicaSet(t, ctx, cl, dep, "old", "1")
	calloutReplicaSet(t, ctx, cl, dep, "new", "2")
	oldPod := calloutPod(t, ctx, cl, dep, oldRS, "callout-old-0", "", false)
	reportCalloutStatus(t, ctx, cl, agent, 3, 0, 1)
	sts := buildA2ANATSStatefulSet(agent, "next")
	live := &appsv1.StatefulSet{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(sts), live); err != nil {
		t.Fatalf("get the bus StatefulSet: %v", err)
	}
	defer func(was func() time.Time) { a2aBusGateNow = was }(a2aBusGateNow)
	a2aBusGateNow = func() time.Time { return stuckPodNow }

	holds := func(what string) bool {
		t.Helper()
		held, err := r.a2aBusWaitsForCallout(ctx, agent, sts, 0)
		if err != nil {
			t.Fatalf("a2aBusWaitsForCallout (%s): %v", what, err)
		}
		return held
	}
	pod := stuckPod(stuckPodOldRevision, false, a2aNATSStuckPodGrace-time.Second)
	pod.Namespace = agent.Namespace
	pod.OwnerReferences[0].UID = live.UID
	setPod := func(ready bool, notReadyFor time.Duration, owner types.UID) {
		t.Helper()
		if err := cl.Get(ctx, client.ObjectKeyFromObject(pod), pod); err != nil {
			t.Fatalf("get the bus pod: %v", err)
		}
		pod.OwnerReferences[0].UID = owner
		if err := cl.Update(ctx, pod); err != nil {
			t.Fatalf("update the bus pod: %v", err)
		}
		pod.Status = stuckPod(stuckPodOldRevision, ready, notReadyFor).Status
		if err := cl.Status().Update(ctx, pod); err != nil {
			t.Fatalf("update the bus pod's status: %v", err)
		}
	}

	if !holds("no bus pod") {
		t.Fatal("the rig does not hold to begin with; the cases below would pass on nothing")
	}
	if err := cl.Create(ctx, pod); err != nil {
		t.Fatalf("create the bus pod: %v", err)
	}
	if !holds("a bus pod inside the grace") {
		t.Error("a bus pod not Ready for less than the grace released the bus")
	}
	setPod(true, a2aNATSStuckPodGrace+time.Hour, live.UID)
	if !holds("a Ready bus pod") {
		t.Error("a Ready bus pod released the bus")
	}
	setPod(false, a2aNATSStuckPodGrace+time.Second, "another-uid")
	if !holds("a pod the StatefulSet does not control") {
		t.Error("a pod the bus StatefulSet does not control released the bus")
	}
	setPod(false, a2aNATSStuckPodGrace, live.UID)
	reportCalloutStatus(t, ctx, cl, agent, 3, 1, 1)
	if !holds("a Ready callout replica") {
		t.Error("the bus was released while a callout replica was Ready, so attached to a bus that still serves")
	}
	reportCalloutStatus(t, ctx, cl, agent, 3, 0, 1)
	select {
	case event := <-recorder.Events:
		t.Errorf("an event was recorded while the bus was held: %s", event)
	default:
	}

	if holds("a bus pod down for the grace") {
		t.Error("a bus pod not Ready for the grace held the bus until the callout's progress deadline")
	}
	select {
	case event := <-recorder.Events:
		if !strings.HasPrefix(event, corev1.EventTypeWarning+" "+a2aBusHoldSkippedReason+" ") || !strings.Contains(event, pod.Name+" has not been Ready for 2m0s") {
			t.Errorf("event = %q, want a %s Warning naming the pod and how long it was down", event, a2aBusHoldSkippedReason)
		}
	default:
		t.Errorf("no %s event when the bus moved without the callout", a2aBusHoldSkippedReason)
	}

	if err := cl.Delete(ctx, oldPod); err != nil {
		t.Fatalf("delete the old callout pod: %v", err)
	}
	if holds("no older callout pod") {
		t.Error("a callout with no older pods held the bus")
	}
	select {
	case event := <-recorder.Events:
		t.Errorf("an event was recorded where no hold was skipped: %s", event)
	default:
	}
	// A callout spec not yet observed holds a running bus, but not a down one.
	dep = getCallout(t, ctx, cl, agent)
	dep.Status.ObservedGeneration = dep.Generation - 1
	if err := cl.Status().Update(ctx, dep); err != nil {
		t.Fatalf("report the callout spec unobserved: %v", err)
	}
	if holds("a callout spec not yet observed, the bus down") {
		t.Error("a callout spec not yet observed held a down bus")
	}
	select {
	case event := <-recorder.Events:
		if !strings.Contains(event, a2aBusHoldSkippedReason) {
			t.Errorf("event = %q, want %s", event, a2aBusHoldSkippedReason)
		}
	default:
		t.Errorf("no %s event when a down bus moved past an unobserved callout spec", a2aBusHoldSkippedReason)
	}
	reportCalloutStatus(t, ctx, cl, agent, 3, 0, 1)
	calloutPod(t, ctx, cl, dep, oldRS, "callout-old-1", "", false)

	pod.Finalizers = []string{"test.kubeagents.x-k8s.io/terminating"}
	if err := cl.Update(ctx, pod); err != nil {
		t.Fatalf("add a finalizer to the bus pod: %v", err)
	}
	if err := cl.Delete(ctx, pod); err != nil {
		t.Fatalf("start terminating the bus pod: %v", err)
	}
	if !holds("a terminating bus pod") {
		t.Error("a terminating bus pod released the bus; the StatefulSet is already replacing it")
	}
}

// A stalled callout rollout is not waited on, and is not passed quietly
// either: the release records a Warning event naming the rollout, and the
// PlatformAgent carries a2aCalloutStalledConditionType while the rollout
// stays past its deadline, removed once it recovers.
func TestAStalledCalloutRolloutReleasesTheBusAndSaysSo(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	recorder := record.NewFakeRecorder(64)
	r.Recorder = recorder
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		busGatePass(t, ctx, r, req, "install")
	}
	theCalloutIsRolledOut(t, ctx, cl, agent)
	completeTheProvisionJob(t, ctx, cl, agent)
	busGatePass(t, ctx, r, req, "settled")
	rendered := busConfigHash(t, ctx, cl, agent)
	putTheBusOnAPreviousConfig(t, ctx, cl, agent)

	// A rollout under way: a newer ReplicaSet with one pod, beside the
	// steady pods of the previous one.
	nextRS := calloutReplicaSet(t, ctx, cl, getCallout(t, ctx, cl, agent), "next", "2")
	calloutPod(t, ctx, cl, getCallout(t, ctx, cl, agent), nextRS, "callout-next-0", "", false)

	stall := func(stalled bool) {
		t.Helper()
		dep := getCallout(t, ctx, cl, agent)
		dep.Status.UpdatedReplicas = 1
		dep.Status.Conditions = []appsv1.DeploymentCondition{{Type: appsv1.DeploymentProgressing, Status: corev1.ConditionTrue, Reason: "ReplicaSetUpdated"}}
		if stalled {
			dep.Status.Conditions[0].Status, dep.Status.Conditions[0].Reason = corev1.ConditionFalse, a2aProgressDeadlineExceeded
		}
		if err := cl.Status().Update(ctx, dep); err != nil {
			t.Fatalf("report the callout rollout (stalled=%v): %v", stalled, err)
		}
	}
	condition := func() *metav1.Condition {
		t.Helper()
		fresh := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
			t.Fatalf("get agent: %v", err)
		}
		return meta.FindStatusCondition(fresh.Status.Conditions, a2aCalloutStalledConditionType)
	}

	stall(false)
	completeTheProvisionJob(t, ctx, cl, agent)
	busGatePass(t, ctx, r, req, "callout mid-roll, progressing")
	if got := busConfigHash(t, ctx, cl, agent); got != "previous" {
		t.Fatalf("the bus moved to %q on a callout still progressing; the rig does not hold to begin with", got)
	}
	if c := condition(); c != nil {
		t.Errorf("the stalled-rollout condition is set on a rollout still progressing: %+v", c)
	}

	stall(true)
	completeTheProvisionJob(t, ctx, cl, agent)
	busGatePass(t, ctx, r, req, "callout rollout stalled")
	if got := busConfigHash(t, ctx, cl, agent); got != rendered {
		t.Errorf("the bus config hash = %q with the callout rollout past its progress deadline, want the rendered %q", got, rendered)
	}
	var warned bool
	for len(recorder.Events) > 0 {
		e := <-recorder.Events
		if strings.HasPrefix(e, corev1.EventTypeWarning+" "+a2aCalloutStalledReason+" ") && strings.Contains(e, a2aCalloutName(agent)) {
			warned = true
		}
	}
	if !warned {
		t.Error("the bus moved past a stalled callout rollout with no Warning event naming it")
	}
	if c := condition(); c == nil || c.Status != metav1.ConditionTrue || !strings.Contains(c.Message, a2aCalloutName(agent)) {
		t.Errorf("%s = %+v with the callout rollout stalled, want True naming the Deployment", a2aCalloutStalledConditionType, c)
	}

	reportCalloutServing(t, ctx, cl, agent)
	stallClear := getCallout(t, ctx, cl, agent)
	stallClear.Status.Conditions = nil
	if err := cl.Status().Update(ctx, stallClear); err != nil {
		t.Fatalf("report the callout recovered: %v", err)
	}
	completeTheProvisionJob(t, ctx, cl, agent)
	busGatePass(t, ctx, r, req, "callout recovered")
	if c := condition(); c != nil {
		t.Errorf("%s stayed after the callout rollout recovered: %+v", a2aCalloutStalledConditionType, c)
	}
}

// Readiness is not what the bus waits on. With every pod on the current spec,
// a callout that is not ready (one pod NotReady in place, or none ready at
// all because the bus they connect to is down) does not hold a pending bus
// config: a pod that is not ready places nobody, and a bus that needs its new
// config to come back must not wait on a callout that needs the bus.
func TestANotReadyCalloutOnTheCurrentSpecDoesNotHoldTheBus(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		busGatePass(t, ctx, r, req, "install")
	}
	theCalloutIsRolledOut(t, ctx, cl, agent)
	completeTheProvisionJob(t, ctx, cl, agent)
	busGatePass(t, ctx, r, req, "settled")
	rendered := busConfigHash(t, ctx, cl, agent)
	replicas := ptr.Deref(getCallout(t, ctx, cl, agent).Spec.Replicas, 1)

	for _, ready := range []int32{replicas - 1, 0} {
		putTheBusOnAPreviousConfig(t, ctx, cl, agent)
		reportCalloutStatus(t, ctx, cl, agent, replicas, ready, replicas)
		completeTheProvisionJob(t, ctx, cl, agent)
		busGatePass(t, ctx, r, req, fmt.Sprintf("%d of %d callout replicas ready", ready, replicas))
		if got := busConfigHash(t, ctx, cl, agent); got != rendered {
			t.Errorf("with %d of %d callout replicas ready, all on the current spec, the bus stayed on config %q; want the rendered %q", ready, replicas, got, rendered)
		}
	}
}
