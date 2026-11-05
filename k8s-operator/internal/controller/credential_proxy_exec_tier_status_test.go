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
	for _, want := range []string{"1 of 2", "exec-b", why, "2 CPU", "shell sandbox"} {
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
}
