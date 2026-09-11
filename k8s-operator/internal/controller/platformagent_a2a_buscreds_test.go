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

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The ordering property, at the granularity A1 can honestly assert it.
//
// A workload must not be dispatched against a bus that cannot yet authenticate
// it, and the condition is how anything downstream asks. False while the
// callout is absent or not fully ready; true, naming the rendered map version,
// once every replica is serving.
// sandboxKeysSecret satisfies the reconcile step that reports a missing shell
// sandbox keypair, so that the tests using it exercise a Ready install rather
// than a Degraded one. It no longer has to exist for BusCredentialsReady to be
// written — that was the defect the four tests at the bottom of this file pin —
// but Ready=True is the state most of these assertions are about.
func sandboxKeysSecret(agent *agentv1alpha1.PlatformAgent) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      shellSandboxAuthorizedKeysSecretName(agent),
			Namespace: agent.Namespace,
		},
	}
}

func TestBusCredentialsReadyTracksTheCallout(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, sandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}

	// The fake client does not run the Deployment controller, so no replica
	// is ready: the condition must be false rather than optimistic. This is
	// the state a real install spends its first seconds in, and dispatching
	// into it is exactly what the condition exists to prevent.
	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	cond := meta.FindStatusCondition(fresh.Status.Conditions, busCredentialsReadyCondition)
	if cond == nil {
		t.Fatal("no BusCredentialsReady condition under next")
	}
	if cond.Status != metav1.ConditionFalse {
		t.Errorf("condition = %s with no ready callout replica, want False", cond.Status)
	}

	// Now report the callout fully ready, as its readiness probe would once
	// it is serving a map.
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-callout", Namespace: "test-ns"}, dep); err != nil {
		t.Fatalf("get callout Deployment: %v", err)
	}
	dep.Status.Replicas = 2
	dep.Status.ReadyReplicas = 2
	if err := cl.Status().Update(ctx, dep); err != nil {
		t.Fatalf("update callout status: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after callout ready: %v", err)
	}

	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	cond = meta.FindStatusCondition(fresh.Status.Conditions, busCredentialsReadyCondition)
	if cond.Status != metav1.ConditionTrue {
		t.Fatalf("condition = %s with every callout replica ready, want True (message: %s)", cond.Status, cond.Message)
	}
	// The rendered version travels in the message so an operator can compare
	// it against what the callout reports at runtime.
	authMapVersion, err := renderA2AAuthMap(a2aTestAgent())
	if err != nil {
		t.Fatalf("renderA2AAuthMap: %v", err)
	}
	if !strings.Contains(cond.Message, authMapVersion.Version) {
		t.Errorf("condition message %q does not name the rendered map version %q", cond.Message, authMapVersion.Version)
	}
}

// TestTheBusConditionMessageNamesOnlyWhatItCanKnow pins each branch's message
// against what the CRD reference says the condition reports.
//
// The reference used to say the message "names the rendered map version and,
// when not ready, the replica counts", which read as though every message
// carried both. Only CalloutServing names the version, only the two
// CalloutUnavailable branches carry counts, and CalloutAbsent carries neither
// because there is no Deployment to read either from -- and someone building an
// alert on the version string would have found that out at the moment the
// callout went away.
//
// So the docs now state it per reason, and this is what keeps the two together:
// a branch that starts or stops naming one of them reds here.
func TestTheBusConditionMessageNamesOnlyWhatItCanKnow(t *testing.T) {
	// Distinctive, so "the message does not name the version" is an assertion
	// rather than a coincidence of the version being a common substring.
	const mapVersion = "map-version-sentinel-7"

	depWith := func(generation, observed int64, ready int32) *appsv1.Deployment {
		return &appsv1.Deployment{
			ObjectMeta: metav1.ObjectMeta{
				Name:       a2aCalloutName(a2aTestAgent()),
				Namespace:  "test-ns",
				Generation: generation,
			},
			Spec: appsv1.DeploymentSpec{Replicas: ptr.To(int32(2))},
			Status: appsv1.DeploymentStatus{
				ObservedGeneration: observed,
				Replicas:           ready,
				ReadyReplicas:      ready,
			},
		}
	}

	for _, tc := range []struct {
		name        string
		dep         *appsv1.Deployment
		absent      bool
		wantReason  string
		wantVersion bool
		wantCounts  string
	}{
		{
			name:       "no callout deployed",
			dep:        &appsv1.Deployment{},
			absent:     true,
			wantReason: busCredsReasonAbsent,
		},
		{
			name:        "every replica serving",
			dep:         depWith(3, 3, 2),
			wantReason:  busCredsReasonServing,
			wantVersion: true,
		},
		{
			name:       "spec not yet observed",
			dep:        depWith(4, 3, 2),
			wantReason: busCredsReasonUnavailable,
			wantCounts: "2 of 2",
		},
		{
			name:       "short of the replicas asked for",
			dep:        depWith(3, 3, 1),
			wantReason: busCredsReasonUnavailable,
			wantCounts: "1 of 2",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			agent := a2aTestAgent()
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(agent).
				WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
				Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

			if err := r.setBusCredentialsReady(context.Background(), agent, tc.dep, tc.absent, mapVersion); err != nil {
				t.Fatalf("setBusCredentialsReady: %v", err)
			}
			cond := meta.FindStatusCondition(agent.Status.Conditions, busCredentialsReadyCondition)
			if cond == nil {
				t.Fatal("no BusCredentialsReady condition written")
			}
			if cond.Reason != tc.wantReason {
				t.Fatalf("reason = %q, want %q (message: %s)", cond.Reason, tc.wantReason, cond.Message)
			}

			if got := strings.Contains(cond.Message, mapVersion); got != tc.wantVersion {
				t.Errorf("names the map version = %v, want %v; the CRD reference's per-reason table says otherwise: %q",
					got, tc.wantVersion, cond.Message)
			}
			hasCounts := strings.Contains(cond.Message, "replicas ready")
			if hasCounts != (tc.wantCounts != "") {
				t.Errorf("names replica counts = %v, want %v; the CRD reference's per-reason table says otherwise: %q",
					hasCounts, tc.wantCounts != "", cond.Message)
			}
			if tc.wantCounts != "" && !strings.Contains(cond.Message, tc.wantCounts) {
				t.Errorf("message %q does not carry %q -- ready against the count asked for is the number an operator sizes the outage by",
					cond.Message, tc.wantCounts)
			}
		})
	}
}

// The darkness property reaches status. A today install must not carry a
// condition describing a component it does not have — a reviewer reading
// `kubectl describe` on a normal install would see the feature named.
func TestBusCredentialsReadyIsAbsentUnderToday(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, sandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}

	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	fresh.Spec.Mode = nil
	if err := cl.Update(ctx, fresh); err != nil {
		t.Fatalf("flip to today: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after flip: %v", err)
	}

	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	if cond := meta.FindStatusCondition(fresh.Status.Conditions, busCredentialsReadyCondition); cond != nil {
		t.Errorf("BusCredentialsReady survives a flip to today: %+v", cond)
	}
}

// Both tests below are the same defect from its two ends: the condition was
// written at the very bottom of Reconcile, under every early return, so a
// reconcile that parked Degraded above it neither wrote it nor cleared it.
//
// The workaround is visible in this file's own history: sandboxKeysSecret
// exists so the tests above reach the write at all. A helper that exists to
// step over an early return is evidence about the production path, not just
// about the fixture -- an install with no sandbox keypair is an ordinary
// install, not a broken one.

// A missing shell sandbox keypair parks the reconcile Degraded. It must not
// also decide whether anything can be dispatched onto the bus: those are
// unrelated components, and the condition is the only thing that says whether
// the callout can authenticate. Reported here even while Ready is False --
// especially then, since that is when someone is reading conditions.
func TestBusCredentialsReadyIsWrittenEvenWhenAnEarlierStepParksDegraded(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()

	// Deliberately no sandboxKeysSecret: this is the install the helper above
	// exists to avoid, and it is a supported one.
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}

	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	if ready := meta.FindStatusCondition(fresh.Status.Conditions, "Ready"); ready == nil ||
		ready.Status != metav1.ConditionFalse {
		t.Fatalf("precondition: want Ready=False from the missing keypair, got %+v", ready)
	}
	if meta.FindStatusCondition(fresh.Status.Conditions, busCredentialsReadyCondition) == nil {
		t.Error("no BusCredentialsReady condition on a next install parked Degraded by an " +
			"unrelated step; nothing downstream can tell whether the bus can authenticate")
	}
}

// The other end: a condition already written must not survive the component it
// describes. A flip back to today tears the callout down, and if the same
// reconcile parks Degraded above the clear, the CR goes on reporting that a
// callout which no longer exists is serving a map -- which reads as healthy.
func TestBusCredentialsReadyIsClearedOnAFlipToTodayThatAlsoParksDegraded(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	keys := sandboxKeysSecret(agent)

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, keys).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	// Twice: the first pass creates the callout objects, the second observes
	// them, which is when the condition is written.
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d under next: %v", i+1, err)
		}
	}
	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	if meta.FindStatusCondition(fresh.Status.Conditions, busCredentialsReadyCondition) == nil {
		t.Fatal("precondition: no BusCredentialsReady under next")
	}

	// Flip to today and remove the keypair in the same step, so the reconcile
	// that tears the callout down is also one that parks Degraded.
	fresh.Spec.Mode = ptr.To("today")
	if err := cl.Update(ctx, fresh); err != nil {
		t.Fatalf("flip mode: %v", err)
	}
	if err := cl.Delete(ctx, keys); err != nil {
		t.Fatalf("delete keypair: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after flip: %v", err)
	}

	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	if cond := meta.FindStatusCondition(fresh.Status.Conditions, busCredentialsReadyCondition); cond != nil {
		t.Errorf("BusCredentialsReady survived the flip to today as %s/%s (%q); "+
			"the CR describes a callout it no longer has",
			cond.Status, cond.Reason, cond.Message)
	}
}

// The two above were only two of the parks, and moving the write up the
// sequence fixed those two rather than the class. Four refusals return above
// the bus step and cannot be moved below it -- they are refusals of today's
// stack, and the bus step is at the end because it renders on top of what they
// withhold. So the condition cannot be written in sequence at all: it has to be
// written on the way out, whichever exit the reconcile takes.
//
// ShellSandboxCannotBeDisabled stands for all four here. It is a hard refusal:
// no requeue, no rendering, and every step after it withheld -- including both
// the bus step and the teardown.

// First direction: a next CR refused before the bus step ever runs. The callout
// does not exist because nothing rendered it, and saying so is the whole point
// -- a CR with no BusCredentialsReady at all is indistinguishable from a today
// install to anything reading conditions.
func TestBusCredentialsReadyIsWrittenWhenARefusalReturnsAboveTheBusStep(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		Experimental: &agentv1alpha1.ExperimentalSpec{
			ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: ptr.To(false)},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, sandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}

	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	if ready := meta.FindStatusCondition(fresh.Status.Conditions, "Ready"); ready == nil ||
		ready.Reason != reasonShellSandboxCannotBeDisabled {
		t.Fatalf("precondition: want Ready parked by the refusal, got %+v", ready)
	}
	cond := meta.FindStatusCondition(fresh.Status.Conditions, busCredentialsReadyCondition)
	if cond == nil {
		t.Fatal("no BusCredentialsReady on a next install refused above the bus step; " +
			"nothing downstream can tell the bus is not there")
	}
	if cond.Status != metav1.ConditionFalse || cond.Reason != busCredsReasonAbsent {
		t.Errorf("condition = %s/%s, want False/%s: the refusal withheld the callout, so it is absent",
			cond.Status, cond.Reason, busCredsReasonAbsent)
	}
}

// Second direction, and the worse one: a condition that was true stops being
// re-derived. updateStatusDegraded writes Ready alone and preserves the rest,
// so the last value stands unchallenged for as long as the refusal does -- the
// CR reports a callout serving a named map version while every replica of it
// has gone. Absent is a gap somebody notices; this reads as healthy.
func TestBusCredentialsReadyIsNotLeftStaleByARefusalAboveTheBusStep(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, sandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d under next: %v", i+1, err)
		}
	}
	dep := &appsv1.Deployment{}
	depKey := types.NamespacedName{Name: "test-agent-a2a-callout", Namespace: "test-ns"}
	if err := cl.Get(ctx, depKey, dep); err != nil {
		t.Fatalf("get callout Deployment: %v", err)
	}
	dep.Status.Replicas = 2
	dep.Status.ReadyReplicas = 2
	if err := cl.Status().Update(ctx, dep); err != nil {
		t.Fatalf("update callout status: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile with the callout ready: %v", err)
	}
	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	if cond := meta.FindStatusCondition(fresh.Status.Conditions, busCredentialsReadyCondition); cond == nil ||
		cond.Status != metav1.ConditionTrue {
		t.Fatalf("precondition: want BusCredentialsReady=True before the refusal, got %+v", cond)
	}

	// Now the two events that make the condition a lie: the callout loses
	// every replica, and the CR picks up a refusal that returns above the step
	// which would have noticed.
	if err := cl.Get(ctx, depKey, dep); err != nil {
		t.Fatalf("re-get callout Deployment: %v", err)
	}
	dep.Status.ReadyReplicas = 0
	if err := cl.Status().Update(ctx, dep); err != nil {
		t.Fatalf("zero the ready replicas: %v", err)
	}
	fresh.Spec.Harness = &agentv1alpha1.HarnessSpec{
		Experimental: &agentv1alpha1.ExperimentalSpec{
			ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: ptr.To(false)},
		},
	}
	if err := cl.Update(ctx, fresh); err != nil {
		t.Fatalf("introduce the refusal: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile under the refusal: %v", err)
	}

	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	cond := meta.FindStatusCondition(fresh.Status.Conditions, busCredentialsReadyCondition)
	if cond == nil {
		t.Fatal("BusCredentialsReady removed by a refusal; the callout is still deployed")
	}
	if cond.Status != metav1.ConditionFalse || cond.Reason != busCredsReasonUnavailable {
		t.Errorf("condition = %s/%s (%q) with no callout replica ready; the refusal above the "+
			"bus step left the last value standing", cond.Status, cond.Reason, cond.Message)
	}
}

// The condition must read the callout's DESIRED replica count, not the number
// of pods that happen to exist, because those two differ on every rollout.
//
// The rendered Deployment is `Replicas: 2, MaxUnavailable: 0, MaxSurge: 1`
// (platformagent_a2a_callout.go), so a routine image or config-hash roll
// surges to three pods: two old ones still ready, one new one starting.
// Status.Replicas counts non-terminated pods matching the selector, so that
// window reads 2 of 3 — and a condition comparing ready against it reports
// CalloutUnavailable, "new connections to the bus may be refused", through a
// rollout whose entire purpose is that the ready count never drops. The
// surge strategy is the redundancy; a monitor that reads it as an outage is
// worse than no monitor, because it teaches people the alarm is noise.
//
// The same comparison fails the other way, which is the one that matters:
// while a callout is coming up, or after a node takes a pod, one pod that
// exists and is ready satisfies ReadyReplicas == Status.Replicas and the
// condition reports CalloutServing on half a callout.
func TestBusCredentialsReadyReadsTheDesiredReplicaCountNotThePodsThatExist(t *testing.T) {
	agent := a2aTestAgent()

	cases := []struct {
		name       string
		desired    int32
		replicas   int32
		ready      int32
		generation int64
		observed   int64
		want       metav1.ConditionStatus
		wantReason string
		why        string
	}{
		{
			name: "a surging rollout is not an outage",
			// maxSurge:1 over two ready pods. Nothing is degraded.
			desired: 2, replicas: 3, ready: 2, generation: 4, observed: 4,
			want: metav1.ConditionTrue, wantReason: busCredsReasonServing,
			why: "every desired replica is ready; the third pod is the surge",
		},
		{
			name: "one pod up out of two wanted is not serving",
			// The window a fresh install and a lost pod both pass through.
			desired: 2, replicas: 1, ready: 1, generation: 4, observed: 4,
			want: metav1.ConditionFalse, wantReason: busCredsReasonUnavailable,
			why: "half the callout is a single point of failure in front of every new connection",
		},
		{
			name:    "steady state, both replicas ready",
			desired: 2, replicas: 2, ready: 2, generation: 4, observed: 4,
			want: metav1.ConditionTrue, wantReason: busCredsReasonServing,
			why: "the state the condition exists to report",
		},
		{
			name:    "no pod ready at all",
			desired: 2, replicas: 2, ready: 0, generation: 4, observed: 4,
			want: metav1.ConditionFalse, wantReason: busCredsReasonUnavailable,
			why: "the bus accepts no new client",
		},
		{
			name: "a spec change the Deployment controller has not seen yet",
			// Status still describes the previous spec. Reporting Serving
			// off it is the stale read this whole file was written against:
			// the counts are true of a Deployment that no longer exists.
			desired: 2, replicas: 2, ready: 2, generation: 5, observed: 4,
			want: metav1.ConditionFalse, wantReason: busCredsReasonUnavailable,
			why: "these counts belong to the spec before the roll",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			cr := a2aTestAgent()
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(cr).
				WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
				Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

			dep := &appsv1.Deployment{
				ObjectMeta: metav1.ObjectMeta{
					Name:       a2aCalloutName(agent),
					Namespace:  agent.Namespace,
					Generation: tc.generation,
				},
				Spec: appsv1.DeploymentSpec{Replicas: ptr.To(tc.desired)},
				Status: appsv1.DeploymentStatus{
					ObservedGeneration: tc.observed,
					Replicas:           tc.replicas,
					ReadyReplicas:      tc.ready,
				},
			}
			if err := r.setBusCredentialsReady(context.Background(), cr, dep, false, "v-under-test"); err != nil {
				t.Fatalf("setBusCredentialsReady: %v", err)
			}
			cond := meta.FindStatusCondition(cr.Status.Conditions, busCredentialsReadyCondition)
			if cond == nil {
				t.Fatal("no BusCredentialsReady condition written")
			}
			if cond.Status != tc.want || cond.Reason != tc.wantReason {
				t.Errorf("desired=%d replicas=%d ready=%d generation=%d/%d: condition = %s/%s, want %s/%s\n%s\nmessage: %s",
					tc.desired, tc.replicas, tc.ready, tc.observed, tc.generation,
					cond.Status, cond.Reason, tc.want, tc.wantReason, tc.why, cond.Message)
			}
			// A message that reports a count nobody asked for is the same
			// defect in the operator's own words.
			if tc.want == metav1.ConditionFalse && !strings.Contains(cond.Message, "of 2") {
				t.Errorf("message %q does not size the callout against the 2 replicas it wants", cond.Message)
			}
		})
	}
}
