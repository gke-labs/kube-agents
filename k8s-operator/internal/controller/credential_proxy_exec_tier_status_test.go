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
	"errors"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func execTierStatusDeployment(ready int32) *appsv1.Deployment {
	agent := execTierTestAgent(ptr.To(int32(1)))
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: credentialProxyExecTierName(agent), Namespace: "test-ns"},
		Status:     appsv1.DeploymentStatus{ReadyReplicas: ready},
	}
}

func execTierStatusPod(name string, conditions ...corev1.PodCondition) *corev1.Pod {
	agent := execTierTestAgent(ptr.To(int32(1)))
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "test-ns", Labels: credentialProxyExecTierSelector(agent)},
		Status:     corev1.PodStatus{Conditions: conditions},
	}
}

func execTierCondition(t *testing.T, replicas *int32, objs ...client.Object) *metav1.Condition {
	t.Helper()
	agent := execTierTestAgent(replicas)
	r, _ := execTierTestReconciler(t, agent, objs...)
	setCredentialProxyExecReadyCondition(agent, r.readCredentialProxyExecReady(context.Background(), agent), metav1.Now())
	return meta.FindStatusCondition(agent.Status.Conditions, credentialProxyExecReadyConditionType)
}

func TestTheExecReadyConditionIsAbsentWhileTheTierIsOff(t *testing.T) {
	for name, replicas := range map[string]*int32{"unset": nil, "zero": ptr.To(int32(0))} {
		t.Run(name, func(t *testing.T) {
			agent := execTierTestAgent(replicas)
			agent.Status.Conditions = []metav1.Condition{{Type: credentialProxyExecReadyConditionType, Status: metav1.ConditionFalse, Reason: "Old"}}
			r, _ := execTierTestReconciler(t, agent)
			setCredentialProxyExecReadyCondition(agent, r.readCredentialProxyExecReady(context.Background(), agent), metav1.Now())
			if c := meta.FindStatusCondition(agent.Status.Conditions, credentialProxyExecReadyConditionType); c != nil {
				t.Fatalf("a tier that is off keeps the condition: %+v", c)
			}
		})
	}
}

func TestTheExecReadyConditionIsTrueWhenEveryReplicaIsReady(t *testing.T) {
	c := execTierCondition(t, ptr.To(int32(2)), execTierStatusDeployment(2))
	if c == nil || c.Status != metav1.ConditionTrue || c.Reason != credentialProxyExecReadyReason || c.Message != "2 of 2 exec-tier replicas are Ready." {
		t.Fatalf("condition = %+v", c)
	}
}

func TestAShortTierReportsItsReadyCount(t *testing.T) {
	scheduled := corev1.PodCondition{Type: corev1.PodScheduled, Status: corev1.ConditionTrue}
	c := execTierCondition(t, ptr.To(int32(2)), execTierStatusDeployment(1), execTierStatusPod("exec-a", scheduled), execTierStatusPod("exec-b", scheduled))
	if c == nil || c.Status != metav1.ConditionFalse || c.Reason != credentialProxyExecUnavailableReason || c.Message != "1 of 2 exec-tier replicas are Ready." {
		t.Fatalf("condition = %+v", c)
	}
}

// The live case behind #4536: one replica fits the node pool and the second
// stays Pending on Insufficient cpu.
func TestAReplicaTheSchedulerCannotPlaceIsNamedWithItsReason(t *testing.T) {
	why := "0/1 nodes are available: 1 Insufficient cpu."
	pending := execTierStatusPod("exec-b", corev1.PodCondition{
		Type: corev1.PodScheduled, Status: corev1.ConditionFalse, Reason: corev1.PodReasonUnschedulable, Message: why,
	})
	c := execTierCondition(t, ptr.To(int32(2)), execTierStatusDeployment(1), pending)
	if c == nil || c.Status != metav1.ConditionFalse || c.Reason != credentialProxyExecUnschedulableReason {
		t.Fatalf("condition = %+v", c)
	}
	for _, want := range []string{"1 of 2", "exec-b", why, "2 CPU"} {
		if !strings.Contains(c.Message, want) {
			t.Errorf("message %q does not name %q", c.Message, want)
		}
	}
}

func TestATierNotRenderedYetCountsNoneReady(t *testing.T) {
	c := execTierCondition(t, ptr.To(int32(1)))
	if c == nil || c.Status != metav1.ConditionFalse || c.Message != "0 of 1 exec-tier replicas are Ready." {
		t.Fatalf("condition = %+v", c)
	}
}

// A read that fails must not overwrite what the last pass found.
func TestAFailedReadLeavesTheConditionAsItWas(t *testing.T) {
	agent := execTierTestAgent(ptr.To(int32(2)))
	before := metav1.Condition{Type: credentialProxyExecReadyConditionType, Status: metav1.ConditionTrue, Reason: credentialProxyExecReadyReason, Message: "2 of 2 exec-tier replicas are Ready."}
	agent.Status.Conditions = []metav1.Condition{before}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithInterceptorFuncs(interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, ok := obj.(*appsv1.Deployment); ok {
				return errors.New("apiserver unavailable")
			}
			return c.Get(ctx, key, obj, opts...)
		},
	}).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	read := r.readCredentialProxyExecReady(context.Background(), agent)
	if !credentialProxyExecReadyCurrent(agent, read) {
		t.Error("a failed read asks for a status write")
	}
	setCredentialProxyExecReadyCondition(agent, read, metav1.Now())
	got := meta.FindStatusCondition(agent.Status.Conditions, credentialProxyExecReadyConditionType)
	if got == nil || got.Status != before.Status || got.Message != before.Message {
		t.Fatalf("condition = %+v, want it unchanged", got)
	}
}

func TestAShortTierAsksForTheSoonerRequeue(t *testing.T) {
	agent := execTierTestAgent(ptr.To(int32(2)))
	if credentialProxyExecShort(agent) {
		t.Fatal("no condition yet, but short")
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{Type: credentialProxyExecReadyConditionType, Status: metav1.ConditionFalse, Reason: credentialProxyExecUnavailableReason})
	if !credentialProxyExecShort(agent) {
		t.Fatal("a False condition does not ask for the sooner requeue")
	}
}

// Through Reconcile and the status writer, which skips its API write when
// every condition is unchanged: a new Ready count has to count as a change,
// or the condition keeps the first pass's count for good.
func TestTheStatusWriterFollowsTheTierAsItBecomesReady(t *testing.T) {
	scheme := setupScheme()
	agent := execTierTestAgent(ptr.To(int32(2)))
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &appsv1.Deployment{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(64)}
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	condition := func(t *testing.T) *metav1.Condition {
		t.Helper()
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile: %v", err)
		}
		got := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
			t.Fatal(err)
		}
		return meta.FindStatusCondition(got.Status.Conditions, credentialProxyExecReadyConditionType)
	}
	setReady := func(t *testing.T, ready int32) {
		t.Helper()
		d := &appsv1.Deployment{}
		if err := cl.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: credentialProxyExecTierName(agent)}, d); err != nil {
			t.Fatalf("the tier's Deployment: %v", err)
		}
		d.Status.ReadyReplicas = ready
		if err := cl.Status().Update(ctx, d); err != nil {
			t.Fatal(err)
		}
	}
	// The first pass adds the finalizer and returns; the second renders.
	condition(t)
	if c := condition(t); c == nil || c.Message != "0 of 2 exec-tier replicas are Ready." {
		got := &agentv1alpha1.PlatformAgent{}
		_ = cl.Get(ctx, req.NamespacedName, got)
		t.Fatalf("after render: %+v (phase %q, conditions %+v)", c, got.Status.Phase, got.Status.Conditions)
	}
	setReady(t, 1)
	if c := condition(t); c == nil || c.Status != metav1.ConditionFalse || c.Message != "1 of 2 exec-tier replicas are Ready." {
		t.Fatalf("after one replica: %+v", c)
	}
	setReady(t, 2)
	if c := condition(t); c == nil || c.Status != metav1.ConditionTrue || c.Message != "2 of 2 exec-tier replicas are Ready." {
		t.Fatalf("after both replicas: %+v", c)
	}
	// A pass that reads the same count writes nothing: a write would wake the
	// next pass, and so on.
	settled := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, settled); err != nil {
		t.Fatal(err)
	}
	condition(t)
	again := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, again); err != nil {
		t.Fatal(err)
	}
	if again.ResourceVersion != settled.ResourceVersion {
		t.Fatalf("an unchanged pass wrote status: ResourceVersion %s -> %s", settled.ResourceVersion, again.ResourceVersion)
	}
}

func TestATerminatingPodIsNotNamedAsUnschedulable(t *testing.T) {
	gone := execTierStatusPod("exec-old", corev1.PodCondition{
		Type: corev1.PodScheduled, Status: corev1.ConditionFalse, Reason: corev1.PodReasonUnschedulable, Message: "old",
	})
	gone.DeletionTimestamp = ptr.To(metav1.Now())
	gone.Finalizers = []string{"test/hold"}
	c := execTierCondition(t, ptr.To(int32(2)), execTierStatusDeployment(1), gone)
	if c == nil || c.Reason != credentialProxyExecUnavailableReason {
		t.Fatalf("condition = %+v, want %s", c, credentialProxyExecUnavailableReason)
	}
}

// A name too long for the tier's objects is refused before render; Degraded
// says so, and there is no tier to count.
func TestANameRefusedForTheTierRemovesTheCondition(t *testing.T) {
	agent := execTierTestAgent(ptr.To(int32(2)))
	agent.Name = strings.Repeat("a", credentialProxyExecTierMaxAgentName+1)
	agent.Status.Conditions = []metav1.Condition{{Type: credentialProxyExecReadyConditionType, Status: metav1.ConditionTrue, Reason: credentialProxyExecReadyReason}}
	r, _ := execTierTestReconciler(t, agent)
	read := r.readCredentialProxyExecReady(context.Background(), agent)
	setCredentialProxyExecReadyCondition(agent, read, metav1.Now())
	if c := meta.FindStatusCondition(agent.Status.Conditions, credentialProxyExecReadyConditionType); c != nil {
		t.Fatalf("a refused name keeps the condition: %+v", c)
	}
}

// A park that rendered the tier (here, no sandbox keypair yet, which can last
// days) still reports it and still removes the condition at 0.
func TestAParkThatRenderedTheTierStillReportsIt(t *testing.T) {
	scheme := setupScheme()
	agent := execTierTestAgent(ptr.To(int32(2)))
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &appsv1.Deployment{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(64)}
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	stored := func(t *testing.T) *agentv1alpha1.PlatformAgent {
		t.Helper()
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile: %v", err)
		}
		got := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
			t.Fatal(err)
		}
		return got
	}
	stored(t)
	got := stored(t)
	if ready := meta.FindStatusCondition(got.Status.Conditions, "Ready"); ready == nil || ready.Reason != "ShellSandboxKeysMissing" {
		t.Fatalf("Ready = %+v, want the ShellSandboxKeysMissing park", ready)
	}
	if c := meta.FindStatusCondition(got.Status.Conditions, credentialProxyExecReadyConditionType); c == nil || c.Message != "0 of 2 exec-tier replicas are Ready." {
		t.Fatalf("on the park: %+v", c)
	}
	got.Spec.Deployment.CredentialProxy.Exec.Replicas = ptr.To(int32(0))
	if err := cl.Update(ctx, got); err != nil {
		t.Fatal(err)
	}
	if c := meta.FindStatusCondition(stored(t).Status.Conditions, credentialProxyExecReadyConditionType); c != nil {
		t.Fatalf("at 0 on the park the condition stays: %+v", c)
	}
}

// earlyParkTierCondition renders an agent at exec.replicas from, then parks it
// on RuntimeClassNotFound, above the broker's reconcile, with the field at to.
// It returns the condition on the park and after the park clears.
func earlyParkTierCondition(t *testing.T, from, to int32) (onPark, afterPark *metav1.Condition) {
	t.Helper()
	scheme := setupScheme()
	agent := execTierTestAgent(ptr.To(from))
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &appsv1.Deployment{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(64)}
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	stored := func() *agentv1alpha1.PlatformAgent {
		t.Helper()
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile: %v", err)
		}
		got := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
			t.Fatal(err)
		}
		return got
	}
	edit := func(got *agentv1alpha1.PlatformAgent, runtimeClass *string, replicas int32) {
		t.Helper()
		got.Spec.Deployment.Availability = &agentv1alpha1.AvailabilitySpec{RuntimeClassName: runtimeClass}
		got.Spec.Deployment.CredentialProxy.Exec.Replicas = ptr.To(replicas)
		if err := cl.Update(ctx, got); err != nil {
			t.Fatal(err)
		}
	}
	stored()
	edit(stored(), ptr.To("missing-runtime-class"), to)
	parked := stored()
	if ready := meta.FindStatusCondition(parked.Status.Conditions, "Ready"); ready == nil || ready.Reason != reasonRuntimeClassNotFound {
		t.Fatalf("Ready = %+v, want the %s park", ready, reasonRuntimeClassNotFound)
	}
	onPark = meta.FindStatusCondition(parked.Status.Conditions, credentialProxyExecReadyConditionType)
	edit(parked, nil, to)
	afterPark = meta.FindStatusCondition(stored().Status.Conditions, credentialProxyExecReadyConditionType)
	return onPark, afterPark
}

// A park above the broker's reconcile leaves the tier running as it was, so
// the condition stays too, until the park clears and the tier goes.
func TestAnEarlyParkKeepsTheConditionWhenTheFieldGoesToZero(t *testing.T) {
	onPark, afterPark := earlyParkTierCondition(t, 2, 0)
	if onPark == nil || onPark.Message != "0 of 2 exec-tier replicas are Ready." {
		t.Fatalf("on the park: %+v, want the condition as it stood", onPark)
	}
	if afterPark != nil {
		t.Fatalf("after the park, at 0: %+v", afterPark)
	}
}

// A park above the broker's reconcile applies no tier, so no condition reads
// as a short one, until the park clears and the tier renders.
func TestAnEarlyParkAddsNoConditionWhenTheFieldGoesAboveZero(t *testing.T) {
	onPark, afterPark := earlyParkTierCondition(t, 0, 2)
	if onPark != nil {
		t.Fatalf("on the park: %+v, want no condition for a tier never applied", onPark)
	}
	if afterPark == nil || afterPark.Message != "0 of 2 exec-tier replicas are Ready." {
		t.Fatalf("after the park: %+v", afterPark)
	}
}

// On a today install nothing else asks for a sooner pass, so the short tier's
// requeue is the one that brings the next read: 30s while short, and the
// steady-state interval again once every replica is Ready.
func TestAShortTierRequeuesSoonerOnATodayInstall(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{Deployment: &agentv1alpha1.DeploymentSpec{
			CredentialProxy: &agentv1alpha1.CredentialProxySpec{Exec: &agentv1alpha1.CredentialProxyExecSpec{Replicas: ptr.To(int32(2))}},
		}},
	}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &appsv1.Deployment{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(64)}
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	pass := func(t *testing.T) ctrl.Result {
		t.Helper()
		res, err := r.Reconcile(ctx, req)
		if err != nil {
			t.Fatalf("Reconcile: %v", err)
		}
		return res
	}
	pass(t)
	if res := pass(t); res.RequeueAfter != credentialProxyExecShortRequeue {
		t.Fatalf("short tier: RequeueAfter = %v, want %v", res.RequeueAfter, credentialProxyExecShortRequeue)
	}
	d := &appsv1.Deployment{}
	if err := cl.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: credentialProxyExecTierName(agent)}, d); err != nil {
		t.Fatal(err)
	}
	d.Status.ReadyReplicas = 2
	if err := cl.Status().Update(ctx, d); err != nil {
		t.Fatal(err)
	}
	if res := pass(t); res.RequeueAfter != secretEnvReprobeInterval {
		t.Fatalf("ready tier: RequeueAfter = %v, want the steady-state %v", res.RequeueAfter, secretEnvReprobeInterval)
	}
}

// The out-of-date ClusterRole path returns before the steady-state requeue,
// so it has to ask for the short tier's sooner pass on its own. A today
// install, because a next install's pending gateway returns sooner still.
func TestAShortTierRequeuesSoonerWhileTheClusterRoleIsOutOfDate(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns", Finalizers: []string{platformAgentFinalizer}},
		Spec: agentv1alpha1.PlatformAgentSpec{Deployment: &agentv1alpha1.DeploymentSpec{
			CredentialProxy: &agentv1alpha1.CredentialProxySpec{Exec: &agentv1alpha1.CredentialProxyExecSpec{Replicas: ptr.To(int32(2))}},
		}},
	}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &appsv1.Deployment{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	authorizer := &fakeAuthorizer{deny: denyPDBPatch}
	checker := NewRBACChecker(authorizer.reviews())
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("boot probe: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(64), RBAC: checker}
	res, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)})
	if err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if res.RequeueAfter != credentialProxyExecShortRequeue {
		t.Fatalf("short tier with an out-of-date ClusterRole: RequeueAfter = %v, want %v", res.RequeueAfter, credentialProxyExecShortRequeue)
	}
}

// The scheduler's verdict can stand for days, and one failed pod list must not
// trade it for a bare count and back.
func TestAFailedPodListKeepsTheSchedulersReason(t *testing.T) {
	agent := execTierTestAgent(ptr.To(int32(2)))
	stored := metav1.Condition{Type: credentialProxyExecReadyConditionType, Status: metav1.ConditionFalse, Reason: credentialProxyExecUnschedulableReason, Message: "1 of 2 … Insufficient cpu"}
	agent.Status.Conditions = []metav1.Condition{stored}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, execTierStatusDeployment(1)).WithInterceptorFuncs(interceptor.Funcs{
		List: func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
			if _, ok := list.(*corev1.PodList); ok {
				return errors.New("apiserver unavailable")
			}
			return c.List(ctx, list, opts...)
		},
	}).Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	read := r.readCredentialProxyExecReady(context.Background(), agent)
	if !credentialProxyExecReadyCurrent(agent, read) {
		t.Error("a failed pod list asks for a status write")
	}
	setCredentialProxyExecReadyCondition(agent, read, metav1.Now())
	if got := meta.FindStatusCondition(agent.Status.Conditions, credentialProxyExecReadyConditionType); got == nil || got.Reason != stored.Reason || got.Message != stored.Message {
		t.Fatalf("condition = %+v, want it unchanged", got)
	}
}

// The tier is applied before the egress allowlist refusal parks with the
// workload unrendered, so the Degraded writer reads it on that park too.
func TestAnUnrenderedParkStillReportsTheTier(t *testing.T) {
	agent := execTierTestAgent(ptr.To(int32(2)))
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, execTierStatusDeployment(2)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	ctx := context.Background()
	if err := r.updateStatusDegraded(ctx, agent, "EgressAllowlistRefused", "refused", workloadNotRendered); err != nil {
		t.Fatal(err)
	}
	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
		t.Fatal(err)
	}
	if c := meta.FindStatusCondition(got.Status.Conditions, credentialProxyExecReadyConditionType); c == nil || c.Status != metav1.ConditionTrue {
		t.Fatalf("on an unrendered park: %+v", c)
	}
}

// During a scale-down the old replicas are still Ready; the count is the field's.
func TestAScaleDownCountsNoMoreReadyThanTheFieldAsks(t *testing.T) {
	c := execTierCondition(t, ptr.To(int32(2)), execTierStatusDeployment(3))
	if c == nil || c.Status != metav1.ConditionTrue || c.Message != "2 of 2 exec-tier replicas are Ready." {
		t.Fatalf("condition = %+v", c)
	}
}
