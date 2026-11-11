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
	"fmt"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

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
	setNATSReady(t, r, stored, 1)
	state, err := r.reconcileA2A(ctx, stored, homeChannelRecord{})
	if err != nil {
		t.Fatal(err)
	}
	return r, stored, state
}

// setNATSReady reports the bus StatefulSet's ready replicas; the fake client
// runs no StatefulSet controller.
func setNATSReady(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent, ready int32) {
	t.Helper()
	ctx := context.Background()
	sts := &appsv1.StatefulSet{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, sts); err != nil {
		t.Fatalf("the bus StatefulSet: %v", err)
	}
	sts.Status.ReadyReplicas = ready
	if err := r.Status().Update(ctx, sts); err != nil {
		t.Fatal(err)
	}
}

// waitingLeader creates an A2A gateway pod, started ago, not ready, and makes
// it the Lease holder from the same moment: the elected gateway waiting.
func waitingLeader(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent, ago time.Duration) *corev1.Pod {
	t.Helper()
	started := time.Now().Add(-ago)
	pod := a2aGatewayPod(agent, a2aGatewayName(agent)+"-abc", &started, false)
	if err := r.Create(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	holdLease(t, r, agent, pod.Name, started)
	return pod
}

// holdLease sets the A2A gateway Lease's holder and acquire time; "" clears
// the holder.
func holdLease(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent, holder string, at time.Time) {
	t.Helper()
	ctx := context.Background()
	lease := &coordinationv1.Lease{}
	key := client.ObjectKey{Namespace: agent.Namespace, Name: a2aGatewayLeaseName(agent)}
	if err := r.Get(ctx, key, lease); err != nil {
		lease = buildA2AGatewayLease(agent)
		if err := r.Create(ctx, lease); err != nil {
			t.Fatalf("the gateway Lease: %v", err)
		}
	}
	if holder == "" {
		lease.Spec.HolderIdentity = nil
	} else {
		lease.Spec.HolderIdentity = ptr.To(holder)
	}
	lease.Spec.AcquireTime = &metav1.MicroTime{Time: at}
	if err := r.Update(ctx, lease); err != nil {
		t.Fatal(err)
	}
}

func TestAGatewayStillWaitingForTasksReadsDegraded(t *testing.T) {
	r, stored, state := settledA2AInstall(t)
	ctx := context.Background()
	waitingLeader(t, r, stored, 10*time.Minute)
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
	waitingLeader(t, r, stored, time.Minute)
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
	waitingLeader(t, r, stored, 10*time.Minute)
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
	waitingLeader(t, r, stored, 10*time.Minute)
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

// TestABusThatIsDownIsNotBlamedOnTheGateway: with the NATS server not
// ready, the gateway waits on its connection, not on TASKS, and this reason
// would point at the wrong thing; it is not written, and no pod is read.
func TestABusThatIsDownIsNotBlamedOnTheGateway(t *testing.T) {
	r, stored, _ := settledA2AInstall(t)
	ctx := context.Background()
	waitingLeader(t, r, stored, 10*time.Minute)
	setNATSReady(t, r, stored, 0)
	if msg := r.a2aGatewayTasksWaitMessage(ctx, stored); msg != "" {
		t.Errorf("a bus with no ready server produced %q", msg)
	}
	if due := r.a2aGatewayTasksWaitRequeue(ctx, stored); due != 24*time.Hour {
		t.Errorf("requeue = %v with the bus down, want none", due)
	}
}

// TestAHigherDegradedReasonStopsTheRequeue: past the threshold under a
// reason that ranks above this one, the pass writes nothing, so a short
// requeue would spin; the requeue backs off instead.
func TestAHigherDegradedReasonStopsTheRequeue(t *testing.T) {
	r, stored, _ := settledA2AInstall(t)
	ctx := context.Background()
	waitingLeader(t, r, stored, 10*time.Minute)
	if due := r.a2aGatewayTasksWaitRequeue(ctx, stored); due != time.Second {
		t.Fatalf("precondition: requeue = %v for a due, unwritten reason", due)
	}
	meta.SetStatusCondition(&stored.Status.Conditions, metav1.Condition{Type: degradedConditionType, Status: metav1.ConditionTrue, Reason: conditionReasonMinterPruningHeld})
	if due := r.a2aGatewayTasksWaitRequeue(ctx, stored); due != 24*time.Hour {
		t.Errorf("requeue = %v under a higher reason, want none", due)
	}
}

// TestAFailedPodReadKeepsTheReason: a pod list that fails keeps the reason
// the CR carries rather than clearing it, as the pod fault scan does.
func TestAFailedPodReadKeepsTheReason(t *testing.T) {
	r, stored, _ := settledA2AInstall(t)
	ctx := context.Background()
	refusing := interceptor.NewClient(r.Client.(client.WithWatch), interceptor.Funcs{
		List: func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
			if _, ok := list.(*corev1.PodList); ok {
				return fmt.Errorf("the API server refused the list")
			}
			return c.List(ctx, list, opts...)
		},
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, ok := obj.(*corev1.Pod); ok {
				return fmt.Errorf("the API server refused the get")
			}
			return c.Get(ctx, key, obj, opts...)
		},
	})
	waitingLeader(t, r, stored, 10*time.Minute)
	blind := &PlatformAgentReconciler{Client: refusing, APIReader: refusing, Scheme: r.Scheme}
	if msg := blind.a2aGatewayTasksWaitMessage(ctx, stored); msg != "" {
		t.Errorf("a failed read with no reason held produced %q", msg)
	}
	meta.SetStatusCondition(&stored.Status.Conditions, metav1.Condition{Type: degradedConditionType, Status: metav1.ConditionTrue,
		Reason: conditionReasonA2AGatewayWaitingForTasks, Message: "held message"})
	if msg := blind.a2aGatewayTasksWaitMessage(ctx, stored); msg != "held message" {
		t.Errorf("a failed read cleared the held reason: %q", msg)
	}
}

// TestAReadyGatewayCostsNoPodList: the cached gate keeps a settled install
// from listing pods for this on every pass, for the message and the requeue.
func TestAReadyGatewayCostsNoPodList(t *testing.T) {
	r, stored, _ := settledA2AInstall(t)
	ctx := context.Background()
	gw := &appsv1.Deployment{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: stored.Namespace, Name: a2aGatewayName(stored)}, gw); err != nil {
		t.Fatal(err)
	}
	gw.Status.ReadyReplicas = *gw.Spec.Replicas
	if err := r.Status().Update(ctx, gw); err != nil {
		t.Fatal(err)
	}
	lists := 0
	counting := interceptor.NewClient(r.Client.(client.WithWatch), interceptor.Funcs{
		List: func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
			if _, ok := list.(*corev1.PodList); ok {
				lists++
			}
			return c.List(ctx, list, opts...)
		},
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, ok := obj.(*corev1.Pod); ok {
				lists++
			}
			return c.Get(ctx, key, obj, opts...)
		},
	})
	counted := &PlatformAgentReconciler{Client: counting, APIReader: counting, Scheme: r.Scheme}
	_ = counted.a2aGatewayTasksWaitMessage(ctx, stored)
	_ = counted.a2aGatewayTasksWaitRequeue(ctx, stored)
	if lists != 0 {
		t.Errorf("a ready gateway cost %d pod reads", lists)
	}
}

// TestTheElectedLeaderIsTheGatewayThatIsRead is the shape seen live on an HA
// install: two replicas, the leader waiting and not ready, the standby
// Ready. The Deployment reads 1/2 ready and the CR read Ready; the reason
// fires on the leader and names it.
func TestTheElectedLeaderIsTheGatewayThatIsRead(t *testing.T) {
	r, stored, state := settledA2AInstall(t)
	ctx := context.Background()
	leader := waitingLeader(t, r, stored, 10*time.Minute)
	started := time.Now().Add(-10 * time.Minute)
	standby := a2aGatewayPod(stored, a2aGatewayName(stored)+"-standby", &started, true)
	if err := r.Create(ctx, standby); err != nil {
		t.Fatal(err)
	}
	if _, err := r.updateStatusReady(ctx, stored, "", otlpSourceNone, r.resolveNetpolProfile(ctx, stored), state); err != nil {
		t.Fatal(err)
	}
	d := meta.FindStatusCondition(stored.Status.Conditions, degradedConditionType)
	if d == nil || d.Reason != conditionReasonA2AGatewayWaitingForTasks {
		t.Fatalf("Degraded = %+v, want %s with a Ready standby", d, conditionReasonA2AGatewayWaitingForTasks)
	}
	if !strings.Contains(d.Message, leader.Name) || strings.Contains(d.Message, standby.Name) {
		t.Errorf("message names the wrong pod: %s", d.Message)
	}

	// The standby is what reads not ready instead: nothing to report.
	holdLease(t, r, stored, standby.Name, started)
	if msg := r.a2aGatewayTasksWaitMessage(ctx, stored); msg != "" {
		t.Errorf("a ready leader produced %q", msg)
	}
}

// No leader to read is no wait to report: a Lease with no holder, or a
// holder whose pod is gone.
func TestNoLeaderIsNoTasksWait(t *testing.T) {
	r, stored, _ := settledA2AInstall(t)
	ctx := context.Background()
	waitingLeader(t, r, stored, 10*time.Minute)
	holdLease(t, r, stored, "", time.Now().Add(-time.Hour))
	if msg := r.a2aGatewayTasksWaitMessage(ctx, stored); msg != "" {
		t.Errorf("no holder produced %q", msg)
	}
	holdLease(t, r, stored, a2aGatewayName(stored)+"-gone", time.Now().Add(-time.Hour))
	if msg := r.a2aGatewayTasksWaitMessage(ctx, stored); msg != "" {
		t.Errorf("a stale holder produced %q", msg)
	}
	if due := r.a2aGatewayTasksWaitRequeue(ctx, stored); due != 24*time.Hour {
		t.Errorf("requeue = %v with a stale holder, want none", due)
	}
}

// A standby promoted a moment ago gets the threshold from its promotion,
// however long its container has run.
func TestAFreshlyElectedLeaderGetsTheThresholdFromItsElection(t *testing.T) {
	r, stored, _ := settledA2AInstall(t)
	ctx := context.Background()
	pod := waitingLeader(t, r, stored, time.Hour)
	holdLease(t, r, stored, pod.Name, time.Now().Add(-30*time.Second))
	if msg := r.a2aGatewayTasksWaitMessage(ctx, stored); msg != "" {
		t.Errorf("a leader elected 30s ago produced %q", msg)
	}
	if due := r.a2aGatewayTasksWaitRequeue(ctx, stored); due <= 4*time.Minute || due > a2aGatewayTasksWaitThreshold {
		t.Errorf("requeue = %v, want ~4.5 minutes from the election", due)
	}
}

// With leader election switched off every pod is read, as on a gateway
// that runs one replica with no Lease.
func TestWithoutElectionEveryPodIsRead(t *testing.T) {
	t.Setenv(a2aGatewayElectionEnvVar, "false")
	r, stored, _ := settledA2AInstall(t)
	ctx := context.Background()
	started := time.Now().Add(-10 * time.Minute)
	if err := r.Create(ctx, a2aGatewayPod(stored, a2aGatewayName(stored)+"-only", &started, false)); err != nil {
		t.Fatal(err)
	}
	if msg := r.a2aGatewayTasksWaitMessage(ctx, stored); !strings.Contains(msg, a2aGatewayName(stored)+"-only") {
		t.Errorf("message = %q, want the one pod named", msg)
	}
}

// No leader clears a reason the CR holds: it is evidence, not a failed read.
func TestNoLeaderClearsAHeldReason(t *testing.T) {
	r, stored, _ := settledA2AInstall(t)
	ctx := context.Background()
	waitingLeader(t, r, stored, 10*time.Minute)
	meta.SetStatusCondition(&stored.Status.Conditions, metav1.Condition{Type: degradedConditionType, Status: metav1.ConditionTrue,
		Reason: conditionReasonA2AGatewayWaitingForTasks, Message: "held message"})
	for name, holder := range map[string]string{"no holder": "", "a stale holder": a2aGatewayName(stored) + "-gone"} {
		holdLease(t, r, stored, holder, time.Now().Add(-time.Hour))
		if msg := r.a2aGatewayTasksWaitMessage(ctx, stored); msg != "" {
			t.Errorf("%s kept %q", name, msg)
		}
	}
}

// A leader whose container is not running (crash looping, in a pod that
// still reads Running) is the pod fault scan's to name.
func TestACrashLoopingLeaderIsTheFaultScans(t *testing.T) {
	r, stored, _ := settledA2AInstall(t)
	ctx := context.Background()
	pod := waitingLeader(t, r, stored, 10*time.Minute)
	pod.Status.ContainerStatuses[0].State = corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: reasonCrashLoopBackOff}}
	if err := r.Status().Update(ctx, pod); err != nil {
		t.Fatal(err)
	}
	if msg := r.a2aGatewayTasksWaitMessage(ctx, stored); msg != "" {
		t.Errorf("a crash-looping leader produced %q", msg)
	}
}
