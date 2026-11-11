/*
Copyright 2026 Google LLC.

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

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// a2aGatewayPod is an A2A gateway pod whose gateway container started at
// started and reads ready as given. A nil started leaves the container
// waiting (ContainerCreating), the state the pod fault scan owns.
func a2aGatewayPod(agent *agentv1alpha1.PlatformAgent, name string, started *time.Time, ready bool) *corev1.Pod {
	state := corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: "ContainerCreating"}}
	phase := corev1.PodPending
	if started != nil {
		state = corev1.ContainerState{Running: &corev1.ContainerStateRunning{StartedAt: metav1.NewTime(*started)}}
		phase = corev1.PodRunning
	}
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: map[string]string{"app": a2aGatewayName(agent)}},
		Status: corev1.PodStatus{
			Phase:             phase,
			ContainerStatuses: []corev1.ContainerStatus{{Name: a2aGatewayContainerName, State: state, Ready: ready}},
		},
	}
}

// provisionedAt records BusProvisioned True from at. Set by hand:
// meta.SetStatusCondition keeps an existing True condition's transition time.
func provisionedAt(agent *agentv1alpha1.PlatformAgent, at time.Time) {
	meta.RemoveStatusCondition(&agent.Status.Conditions, busProvisionedConditionType)
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type: busProvisionedConditionType, Status: metav1.ConditionTrue, Reason: "ProvisionJobComplete",
		LastTransitionTime: metav1.NewTime(at),
	})
}

func TestTheGatewayWaitForTasksIsReadFromItsPods(t *testing.T) {
	t.Parallel()
	now := time.Now()
	ago := func(d time.Duration) *time.Time { at := now.Add(-d); return &at }
	cases := []struct {
		name        string
		provisioned *time.Time
		pods        func(agent *agentv1alpha1.PlatformAgent) []corev1.Pod
		wantWaiting bool
		wantSince   *time.Time
	}{
		{"a gateway not ready for ten minutes after the bus was provisioned is waiting", ago(time.Hour),
			func(a *agentv1alpha1.PlatformAgent) []corev1.Pod {
				return []corev1.Pod{*a2aGatewayPod(a, "gw-1", ago(10*time.Minute), false)}
			}, true, ago(10 * time.Minute)},
		{"the clock starts at the later of the pod start and BusProvisioned", ago(2 * time.Minute),
			func(a *agentv1alpha1.PlatformAgent) []corev1.Pod {
				return []corev1.Pod{*a2aGatewayPod(a, "gw-1", ago(time.Hour), false)}
			}, true, ago(2 * time.Minute)},
		{"a bus not yet provisioned is the install's own wait", nil,
			func(a *agentv1alpha1.PlatformAgent) []corev1.Pod {
				return []corev1.Pod{*a2aGatewayPod(a, "gw-1", ago(time.Hour), false)}
			}, false, nil},
		{"a ready gateway is not waiting", ago(time.Hour),
			func(a *agentv1alpha1.PlatformAgent) []corev1.Pod {
				return []corev1.Pod{*a2aGatewayPod(a, "gw-1", ago(time.Hour), true)}
			}, false, nil},
		{"a container that never started is the fault scan's", ago(time.Hour),
			func(a *agentv1alpha1.PlatformAgent) []corev1.Pod {
				return []corev1.Pod{*a2aGatewayPod(a, "gw-1", nil, false)}
			}, false, nil},
		{"a crash-looping container in a Running pod is the fault scan's", ago(time.Hour),
			func(a *agentv1alpha1.PlatformAgent) []corev1.Pod {
				p := a2aGatewayPod(a, "gw-1", ago(time.Hour), false)
				p.Status.ContainerStatuses[0].State = corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: reasonCrashLoopBackOff}}
				return []corev1.Pod{*p}
			}, false, nil},
		{"no gateway pod is not a wait", ago(time.Hour),
			func(a *agentv1alpha1.PlatformAgent) []corev1.Pod { return nil }, false, nil},
		{"a pod on its way out is not read", ago(time.Hour),
			func(a *agentv1alpha1.PlatformAgent) []corev1.Pod {
				p := a2aGatewayPod(a, "gw-1", ago(time.Hour), false)
				p.DeletionTimestamp = &metav1.Time{Time: now}
				p.Finalizers = []string{"test"}
				return []corev1.Pod{*p}
			}, false, nil},
		{"the youngest pod sets the clock", ago(time.Hour),
			func(a *agentv1alpha1.PlatformAgent) []corev1.Pod {
				return []corev1.Pod{*a2aGatewayPod(a, "gw-1", ago(time.Hour), false), *a2aGatewayPod(a, "gw-2", ago(3*time.Minute), false)}
			}, true, ago(3 * time.Minute)},
		{"one ready pod among them is not a wait", ago(time.Hour),
			func(a *agentv1alpha1.PlatformAgent) []corev1.Pod {
				return []corev1.Pod{*a2aGatewayPod(a, "gw-1", ago(time.Hour), false), *a2aGatewayPod(a, "gw-2", ago(time.Hour), true)}
			}, false, nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			agent := a2aTestAgent()
			if tc.provisioned != nil {
				provisionedAt(agent, *tc.provisioned)
			}
			since, waiting := a2aGatewayTasksWait(agent, tc.pods(agent))
			if waiting != tc.wantWaiting {
				t.Fatalf("waiting = %v, want %v", waiting, tc.wantWaiting)
			}
			if tc.wantSince != nil && !since.Truncate(time.Second).Equal(tc.wantSince.Truncate(time.Second)) {
				t.Errorf("since = %v, want %v", since, *tc.wantSince)
			}
		})
	}

	// BusProvisioned recorded False is not provisioned either.
	unprovisioned := a2aTestAgent()
	meta.SetStatusCondition(&unprovisioned.Status.Conditions, metav1.Condition{Type: busProvisionedConditionType, Status: metav1.ConditionFalse, Reason: "ProvisionJobRunning"})
	if _, waiting := a2aGatewayTasksWait(unprovisioned, []corev1.Pod{*a2aGatewayPod(unprovisioned, "gw-1", ago(time.Hour), false)}); waiting {
		t.Error("a BusProvisioned=False install read as a gateway waiting for TASKS")
	}

	// A today install renders no A2A gateway, so nothing it lists is one.
	today := a2aTestAgent()
	today.Spec.Mode = nil
	provisionedAt(today, now.Add(-time.Hour))
	if _, waiting := a2aGatewayTasksWait(today, []corev1.Pod{*a2aGatewayPod(today, "gw-1", ago(time.Hour), false)}); waiting {
		t.Error("a today install read as a gateway waiting for TASKS")
	}
}

func TestTheGatewayWaitForTasksTurnsDueAtTheThreshold(t *testing.T) {
	t.Parallel()
	now := time.Now()
	for _, tc := range []struct {
		waited   time.Duration
		wantFire bool
	}{
		{a2aGatewayTasksWaitThreshold - time.Second, false},
		{a2aGatewayTasksWaitThreshold, true},
		{time.Hour, true},
	} {
		fires, due := a2aGatewayTasksWaitDue(now.Add(-tc.waited), now)
		if fires != tc.wantFire {
			t.Errorf("waited %v: fires = %v, want %v", tc.waited, fires, tc.wantFire)
		}
		if !fires && (due <= 0 || due > time.Second+time.Millisecond) {
			t.Errorf("waited %v: due = %v, want the second left", tc.waited, due)
		}
	}
}

// settledA2AInstall reconciles a next install until the bus is provisioned,
// then backdates BusProvisioned so the wait clock is the pod's.
func settledA2AInstall(t *testing.T) (*PlatformAgentReconciler, *agentv1alpha1.PlatformAgent, a2aProvisionState) {
	t.Helper()
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	letTheGatewayThrough(t, ctx, cl, r, req, agent)
	completeTheProvisionJob(t, ctx, cl, agent)
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal(err)
	}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, stored); err != nil {
		t.Fatal(err)
	}
	if !busProvisioned(stored) {
		t.Fatal("precondition: the install did not record the bus provisioned")
	}
	provisionedAt(stored, time.Now().Add(-time.Hour))
	state, err := r.reconcileA2A(ctx, stored, homeChannelRecord{})
	if err != nil {
		t.Fatal(err)
	}
	return r, stored, state
}

func TestAGatewayStillWaitingForTasksReadsDegraded(t *testing.T) {
	r, stored, state := settledA2AInstall(t)
	ctx := context.Background()
	started := time.Now().Add(-10 * time.Minute)
	if err := r.Create(ctx, a2aGatewayPod(stored, a2aGatewayName(stored)+"-abc", &started, false)); err != nil {
		t.Fatal(err)
	}
	if _, err := r.updateStatusReady(ctx, stored, "", otlpSourceNone, r.resolveNetpolProfile(ctx, stored), state); err != nil {
		t.Fatal(err)
	}
	degraded := meta.FindStatusCondition(stored.Status.Conditions, degradedConditionType)
	if degraded == nil || degraded.Status != metav1.ConditionTrue || degraded.Reason != conditionReasonA2AGatewayWaitingForTasks {
		t.Fatalf("Degraded = %+v, want True/%s", degraded, conditionReasonA2AGatewayWaitingForTasks)
	}
	for _, want := range []string{a2aGatewayName(stored), "TASKS", "/readyz", "account"} {
		if !strings.Contains(degraded.Message, want) {
			t.Errorf("message lacks %q: %s", want, degraded.Message)
		}
	}
	// Degraded only: Ready keeps what the workload says.
	if ready := meta.FindStatusCondition(stored.Status.Conditions, readyConditionType); ready == nil || ready.Reason == conditionReasonA2AGatewayWaitingForTasks {
		t.Errorf("Ready = %+v, want the workload's reason left alone", ready)
	}

	// Once it binds, the reason clears.
	pod := &corev1.Pod{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: stored.Namespace, Name: a2aGatewayName(stored) + "-abc"}, pod); err != nil {
		t.Fatal(err)
	}
	pod.Status.ContainerStatuses[0].Ready = true
	if err := r.Status().Update(ctx, pod); err != nil {
		t.Fatal(err)
	}
	if _, err := r.updateStatusReady(ctx, stored, "", otlpSourceNone, r.resolveNetpolProfile(ctx, stored), state); err != nil {
		t.Fatal(err)
	}
	if d := meta.FindStatusCondition(stored.Status.Conditions, degradedConditionType); d != nil && d.Reason == conditionReasonA2AGatewayWaitingForTasks {
		t.Errorf("Degraded still %s after the gateway bound", d.Reason)
	}
}

func TestAGatewayWaitingUnderTheThresholdIsNotDegraded(t *testing.T) {
	r, stored, state := settledA2AInstall(t)
	ctx := context.Background()
	started := time.Now().Add(-time.Minute)
	if err := r.Create(ctx, a2aGatewayPod(stored, a2aGatewayName(stored)+"-abc", &started, false)); err != nil {
		t.Fatal(err)
	}
	if _, err := r.updateStatusReady(ctx, stored, "", otlpSourceNone, r.resolveNetpolProfile(ctx, stored), state); err != nil {
		t.Fatal(err)
	}
	if d := meta.FindStatusCondition(stored.Status.Conditions, degradedConditionType); d != nil && d.Reason == conditionReasonA2AGatewayWaitingForTasks {
		t.Fatalf("Degraded %s a minute in", d.Reason)
	}
	due := r.a2aGatewayTasksWaitRequeue(ctx, stored)
	if due <= 3*time.Minute || due > a2aGatewayTasksWaitThreshold {
		t.Errorf("requeue = %v, want the ~4 minutes left", due)
	}
}

func TestTheTasksWaitRanksBelowTheReasonsAboveIt(t *testing.T) {
	r, stored, state := settledA2AInstall(t)
	ctx := context.Background()
	started := time.Now().Add(-10 * time.Minute)
	if err := r.Create(ctx, a2aGatewayPod(stored, a2aGatewayName(stored)+"-abc", &started, false)); err != nil {
		t.Fatal(err)
	}
	// A refused credential-proxy override ranks below it.
	stored.Spec.Deployment = &agentv1alpha1.DeploymentSpec{CredentialProxy: &agentv1alpha1.CredentialProxySpec{
		Resources: &corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Mi")}},
	}}
	if _, err := r.updateStatusReady(ctx, stored, "", otlpSourceNone, r.resolveNetpolProfile(ctx, stored), state); err != nil {
		t.Fatal(err)
	}
	if d := meta.FindStatusCondition(stored.Status.Conditions, degradedConditionType); d == nil || d.Reason != conditionReasonA2AGatewayWaitingForTasks {
		t.Fatalf("Degraded = %+v, want the TASKS wait over a resource refusal", d)
	}
}

// TestTheTasksWaitPointsAtBusStreamsPresent: when #5474's condition is on the
// CR and not True, the message names its reason, since it says whether the
// stream is there at all; True or absent adds nothing.
func TestTheTasksWaitPointsAtBusStreamsPresent(t *testing.T) {
	r, stored, _ := settledA2AInstall(t)
	ctx := context.Background()
	started := time.Now().Add(-10 * time.Minute)
	if err := r.Create(ctx, a2aGatewayPod(stored, a2aGatewayName(stored)+"-abc", &started, false)); err != nil {
		t.Fatal(err)
	}
	if msg := r.a2aGatewayTasksWaitMessage(ctx, stored); msg == "" || strings.Contains(msg, "BusStreamsPresent") {
		t.Fatalf("message without the condition = %q", msg)
	}
	meta.SetStatusCondition(&stored.Status.Conditions, metav1.Condition{Type: a2aBusStreamsPresentConditionType, Status: metav1.ConditionFalse, Reason: "DetectionUnavailable"})
	if msg := r.a2aGatewayTasksWaitMessage(ctx, stored); !strings.Contains(msg, "BusStreamsPresent reads False (DetectionUnavailable)") {
		t.Errorf("message = %q", msg)
	}
	meta.SetStatusCondition(&stored.Status.Conditions, metav1.Condition{Type: a2aBusStreamsPresentConditionType, Status: metav1.ConditionTrue, Reason: "AllPresent"})
	if msg := r.a2aGatewayTasksWaitMessage(ctx, stored); strings.Contains(msg, "BusStreamsPresent") {
		t.Errorf("a True BusStreamsPresent was named: %q", msg)
	}
}
