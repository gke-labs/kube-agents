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
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The bus pod a rollback leaves behind sits not Ready on a revision the
// StatefulSet no longer renders, and an OrderedReady rolling update never
// replaces it. replaceStuckA2ANATSPod replaces that pod and nothing else.

var stuckPodNow = time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)

const (
	stuckPodCurrentRevision = "test-agent-a2a-nats-new"
	stuckPodOldRevision     = "test-agent-a2a-nats-old"
	stuckPodStatefulSetUID  = types.UID("sts-uid-1")
)

func stuckPodStatefulSet(generation, observed int64) *appsv1.StatefulSet {
	agent := a2aTestAgent()
	return &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSName(agent), Namespace: agent.Namespace, Generation: generation, UID: stuckPodStatefulSetUID},
		Spec:       appsv1.StatefulSetSpec{Replicas: ptr.To(int32(1))},
		Status:     appsv1.StatefulSetStatus{ObservedGeneration: observed, UpdateRevision: stuckPodCurrentRevision},
	}
}

// stuckPod is pod-0 on revision, not Ready since notReadyFor before stuckPodNow.
func stuckPod(revision string, ready bool, notReadyFor time.Duration) *corev1.Pod {
	agent := a2aTestAgent()
	status := corev1.ConditionFalse
	if ready {
		status = corev1.ConditionTrue
	}
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:              a2aNATSName(agent) + "-0",
			Namespace:         agent.Namespace,
			UID:               types.UID("pod-uid-1"),
			Labels:            map[string]string{appsv1.ControllerRevisionHashLabelKey: revision},
			CreationTimestamp: metav1.NewTime(stuckPodNow.Add(-time.Hour)),
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: "apps/v1", Kind: "StatefulSet", Name: a2aNATSName(agent),
				UID: stuckPodStatefulSetUID, Controller: ptr.To(true),
			}},
		},
		Status: corev1.PodStatus{Conditions: []corev1.PodCondition{{
			Type:               corev1.PodReady,
			Status:             status,
			LastTransitionTime: metav1.NewTime(stuckPodNow.Add(-notReadyFor)),
		}}},
	}
}

type stuckPodRig struct {
	r         *PlatformAgentReconciler
	cl        client.Client
	sts       *appsv1.StatefulSet
	recorder  *record.FakeRecorder
	deletes   []client.DeleteOption
	deleteErr error
}

// newStuckPodRig holds pod in a fake API server; sts stands for the
// StatefulSet as its apply returned it.
func newStuckPodRig(t *testing.T, sts *appsv1.StatefulSet, pod *corev1.Pod) *stuckPodRig {
	t.Helper()
	rig := &stuckPodRig{recorder: record.NewFakeRecorder(8), sts: sts}
	objs := []client.Object{a2aTestAgent()}
	if pod != nil {
		objs = append(objs, pod)
	}
	cl := fake.NewClientBuilder().
		WithScheme(setupScheme()).
		WithObjects(objs...).
		WithInterceptorFuncs(interceptor.Funcs{
			Delete: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
				rig.deletes = append(rig.deletes, opts...)
				if rig.deleteErr != nil {
					return rig.deleteErr
				}
				return c.Delete(ctx, obj, opts...)
			},
		}).
		Build()
	rig.cl = cl
	rig.r = &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: setupScheme(), Recorder: rig.recorder}
	return rig
}

func (rig *stuckPodRig) run(t *testing.T) time.Duration {
	t.Helper()
	requeue, err := rig.r.replaceStuckA2ANATSPod(context.Background(), a2aTestAgent(), rig.sts, stuckPodNow)
	if err != nil {
		t.Fatalf("replaceStuckA2ANATSPod: %v", err)
	}
	return requeue
}

func (rig *stuckPodRig) podExists(t *testing.T) bool {
	t.Helper()
	agent := a2aTestAgent()
	err := rig.cl.Get(context.Background(), types.NamespacedName{Name: a2aNATSName(agent) + "-0", Namespace: agent.Namespace}, &corev1.Pod{})
	if errors.IsNotFound(err) {
		return false
	}
	if err != nil {
		t.Fatalf("get pod: %v", err)
	}
	return true
}

func TestAStuckBusPodOnAnOldRevisionIsReplaced(t *testing.T) {
	rig := newStuckPodRig(t, stuckPodStatefulSet(3, 3), stuckPod(stuckPodOldRevision, false, a2aNATSStuckPodGrace+time.Second))
	if requeue := rig.run(t); requeue != 0 {
		t.Errorf("requeue = %s after the replacement, want 0", requeue)
	}
	if rig.podExists(t) {
		t.Fatal("the stuck pod is still there")
	}
	var uid *types.UID
	for _, opt := range rig.deletes {
		if p, ok := opt.(client.Preconditions); ok {
			uid = p.UID
		}
	}
	if uid == nil || *uid != "pod-uid-1" {
		t.Errorf("the delete carried no UID precondition for the pod read (got %v)", uid)
	}
	select {
	case event := <-rig.recorder.Events:
		if !strings.Contains(event, a2aNATSStuckPodReplacedReason) || !strings.Contains(event, stuckPodOldRevision) {
			t.Errorf("event %q does not name the reason and the old revision", event)
		}
	default:
		t.Error("no event recorded for the replacement")
	}
}

func TestABusPodInsideTheGraceIsKeptAndRequeued(t *testing.T) {
	rig := newStuckPodRig(t, stuckPodStatefulSet(3, 3), stuckPod(stuckPodOldRevision, false, 30*time.Second))
	if requeue := rig.run(t); requeue != a2aNATSStuckPodGrace-30*time.Second {
		t.Errorf("requeue = %s, want the grace left, %s", requeue, a2aNATSStuckPodGrace-30*time.Second)
	}
	if !rig.podExists(t) {
		t.Fatal("a pod inside the grace period was deleted")
	}
}

func TestTheBusPodIsLeftAloneOtherwise(t *testing.T) {
	long := a2aNATSStuckPodGrace + time.Hour
	terminating := stuckPod(stuckPodOldRevision, false, long)
	terminating.DeletionTimestamp = ptr.To(metav1.NewTime(stuckPodNow))
	terminating.Finalizers = []string{"test/hold"}
	noCondition := stuckPod(stuckPodOldRevision, false, long)
	noCondition.Status.Conditions = nil
	noCondition.CreationTimestamp = metav1.NewTime(stuckPodNow.Add(-30 * time.Second))
	allUpdated := stuckPodStatefulSet(3, 3)
	allUpdated.Status.UpdatedReplicas = 1
	squatter := stuckPod("", false, long)
	squatter.OwnerReferences = nil
	otherOwner := stuckPod(stuckPodOldRevision, false, long)
	otherOwner.OwnerReferences[0].UID = "someone-else"
	for _, tc := range []struct {
		name string
		sts  *appsv1.StatefulSet
		pod  *corev1.Pod
	}{
		{"on the current revision, not Ready", stuckPodStatefulSet(3, 3), stuckPod(stuckPodCurrentRevision, false, long)},
		{"Ready on an old revision, which the StatefulSet rolls itself", stuckPodStatefulSet(3, 3), stuckPod(stuckPodOldRevision, true, long)},
		{"the StatefulSet has not observed its generation", stuckPodStatefulSet(4, 3), stuckPod(stuckPodOldRevision, false, long)},
		{"already terminating", stuckPodStatefulSet(3, 3), terminating},
		{"no Ready condition, created inside the grace", stuckPodStatefulSet(3, 3), noCondition},
		{"every replica already updated", allUpdated, stuckPod(stuckPodOldRevision, false, long)},
		{"a pod of that name with no controller", stuckPodStatefulSet(3, 3), squatter},
		{"a pod of that name another object controls", stuckPodStatefulSet(3, 3), otherOwner},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rig := newStuckPodRig(t, tc.sts, tc.pod)
			rig.run(t)
			if !rig.podExists(t) {
				t.Fatal("the pod was deleted")
			}
			if len(rig.deletes) != 0 {
				t.Errorf("a delete was sent: %v", rig.deletes)
			}
		})
	}
}

func TestNoPodIsNotAnError(t *testing.T) {
	newStuckPodRig(t, stuckPodStatefulSet(3, 3), nil).run(t)
}

func TestAPendingPodWithNoReadyConditionIsTimedFromCreation(t *testing.T) {
	pending := stuckPod(stuckPodOldRevision, false, 0)
	pending.Status.Conditions = nil
	inside := pending.DeepCopy()
	inside.CreationTimestamp = metav1.NewTime(stuckPodNow.Add(-30 * time.Second))
	rig := newStuckPodRig(t, stuckPodStatefulSet(3, 3), inside)
	if requeue := rig.run(t); requeue != a2aNATSStuckPodGrace-30*time.Second {
		t.Errorf("requeue = %s, want the grace left from creation", requeue)
	}
	rig = newStuckPodRig(t, stuckPodStatefulSet(3, 3), pending) // created an hour before
	rig.run(t)
	if rig.podExists(t) {
		t.Error("a Pending pod on an old revision, created past the grace, was kept")
	}
}

func TestADeleteThatLosesTheRaceIsNotAnError(t *testing.T) {
	pod := stuckPod(stuckPodOldRevision, false, a2aNATSStuckPodGrace+time.Second)
	for _, err := range []error{
		errors.NewConflict(corev1.Resource("pods"), pod.Name, nil),
		errors.NewNotFound(corev1.Resource("pods"), pod.Name),
	} {
		rig := newStuckPodRig(t, stuckPodStatefulSet(3, 3), pod)
		rig.deleteErr = err
		if requeue := rig.run(t); requeue != 0 {
			t.Errorf("%v: requeue = %s, want 0", err, requeue)
		}
		select {
		case event := <-rig.recorder.Events:
			t.Errorf("%v: an event was recorded for a delete that did not happen: %s", err, event)
		default:
		}
	}
}

// Through Reconcile: the bus apply calls the repair with the StatefulSet the
// apply returned, and a pod inside the grace sets the requeue. The fake client
// does not run the StatefulSet controller, so the apply's return is given the
// status a server would report: generation observed, one replica, none of it
// on the update revision. checker, when given, makes the pass RBAC-degraded,
// whose requeue is rbacReprobeInterval unless something is due sooner.
func busApplyReplacesAStuckPod(t *testing.T, checker *RBACChecker) {
	t.Helper()
	agent := a2aTestAgent()
	stsName := a2aNATSName(agent)
	base := assigningUIDsOnCreate(fakeServerSideApplyInterceptors())
	withStatus := base
	withStatus.Patch = func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
		if err := base.Patch(ctx, c, obj, patch, opts...); err != nil {
			return err
		}
		if sts, ok := obj.(*appsv1.StatefulSet); ok && sts.Name == stsName {
			sts.Status = appsv1.StatefulSetStatus{ObservedGeneration: sts.Generation, UpdateRevision: stuckPodCurrentRevision}
		}
		return nil
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, sandboxKeysSecret(agent), discordBotSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(withStatus).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(64)}
	if checker != nil {
		r.RBAC = checker
	}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		busGatePass(t, ctx, r, req, "install")
	}
	reportCalloutServing(t, ctx, cl, agent)
	completeTheProvisionJob(t, ctx, cl, agent)
	busGatePass(t, ctx, r, req, "settled")

	sts := &appsv1.StatefulSet{}
	if err := cl.Get(ctx, types.NamespacedName{Name: stsName, Namespace: agent.Namespace}, sts); err != nil {
		t.Fatalf("get the bus StatefulSet: %v", err)
	}
	// A pod not Ready for 30 s on an old revision: kept, and the pass asks
	// to come back when its grace runs out.
	pod := stuckPod(stuckPodOldRevision, false, 0)
	pod.Name, pod.Namespace = stsName+"-0", agent.Namespace
	pod.OwnerReferences[0].UID = sts.UID
	pod.Status.Conditions[0].LastTransitionTime = metav1.NewTime(time.Now().Add(-30 * time.Second))
	if err := cl.Create(ctx, pod); err != nil {
		t.Fatalf("create the stuck pod: %v", err)
	}
	res := busGatePass(t, ctx, r, req, "inside the grace")
	if res.RequeueAfter <= 0 || res.RequeueAfter > a2aNATSStuckPodGrace-25*time.Second {
		t.Errorf("RequeueAfter = %s, want about the grace left (%s)", res.RequeueAfter, a2aNATSStuckPodGrace-30*time.Second)
	}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(pod), &corev1.Pod{}); err != nil {
		t.Fatalf("the pod inside its grace is gone: %v", err)
	}

	// Past the grace, the next pass replaces it.
	live := &corev1.Pod{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(pod), live); err != nil {
		t.Fatal(err)
	}
	live.Status.Conditions[0].LastTransitionTime = metav1.NewTime(time.Now().Add(-a2aNATSStuckPodGrace - time.Minute))
	if err := cl.Status().Update(ctx, live); err != nil {
		t.Fatalf("age the pod: %v", err)
	}
	busGatePass(t, ctx, r, req, "past the grace")
	if err := cl.Get(ctx, client.ObjectKeyFromObject(pod), &corev1.Pod{}); !errors.IsNotFound(err) {
		t.Errorf("the stuck pod was not replaced by the bus apply (get: %v)", err)
	}
}

func TestTheBusApplyReplacesAStuckPodAndRequeuesForTheGrace(t *testing.T) {
	busApplyReplacesAStuckPod(t, nil)
}

// An out-of-date ClusterRole requeues at rbacReprobeInterval; a bus pod due
// sooner brings the pass forward.
func TestARBACDegradedPassStillRequeuesForTheStuckPod(t *testing.T) {
	checker := NewRBACChecker((&fakeAuthorizer{deny: denyPDBPatch}).reviews())
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("boot probe: %v", err)
	}
	busApplyReplacesAStuckPod(t, checker)
}
