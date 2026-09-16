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
	"testing"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// What updateStatusDegraded's equality gate has to do (#1392). The PlatformAgent
// watch carries no predicate, so a status write on one pass is the event that
// starts the next; a refusal that wrote on every pass therefore wrote twice per
// requeue tick — once for the tick, once for the echo pass its own write woke —
// and moved the object every time, for as long as it stood. These tests pin
// both halves: a pass that changes nothing writes nothing, and a change in
// anything the write carries — reason, message, generation — still writes.
//
// The fake client does not maintain metadata.generation, so the generation
// case sets it by hand. The envtest case in
// platformagent_degraded_loop_envtest_test.go is where the loop itself is
// measured, under the real manager and its watches.

const (
	// degradedGateMissingRuntimeClass is a RuntimeClass no fixture creates, so
	// the reconcile parks on RuntimeClassNotFound: the earliest refusal that
	// reaches updateStatusDegraded through Reconcile with nothing more than
	// the CR in the cluster.
	degradedGateMissingRuntimeClass = "gvisor-not-installed"
	// degradedGateQuietPasses is how many unchanged passes the loop test runs
	// after the one that parks the CR. Any of them writing is the bug.
	degradedGateQuietPasses = 3
)

// degradedGateAgent is a CR whose reconcile refuses at the RuntimeClass check.
// The finalizer is pre-set so the first pass is the refusing one rather than
// the finalizer-adding one.
func degradedGateAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "test-agent",
			Namespace:  "test-ns",
			Finalizers: []string{platformAgentFinalizer},
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Deployment: &agentv1alpha1.DeploymentSpec{
				Availability: &agentv1alpha1.AvailabilitySpec{
					RuntimeClassName: ptr.To(degradedGateMissingRuntimeClass),
				},
			},
		},
	}
}

// degradedGateReconciler is the reconciler under test with a status-write
// counter on its client, and nothing else in the cluster but the agent.
func degradedGateReconciler(agent *agentv1alpha1.PlatformAgent, counter *statusWriteCounter) *PlatformAgentReconciler {
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(counter.interceptors()).
		Build()
	return &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
}

// TestAParkedRefusalDoesNotWriteStatusEveryPass is the regression pin: the
// same refused CR reconciled again, with nothing changed, writes no status.
func TestAParkedRefusalDoesNotWriteStatusEveryPass(t *testing.T) {
	agent := degradedGateAgent()
	counter := &statusWriteCounter{}
	r := degradedGateReconciler(agent, counter)
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("first Reconcile failed: %v", err)
	}
	if counter.writes != 1 {
		t.Fatalf("the parking pass made %d status writes, want 1", counter.writes)
	}
	parked := &agentv1alpha1.PlatformAgent{}
	if err := r.Get(ctx, req.NamespacedName, parked); err != nil {
		t.Fatalf("reading the parked agent back: %v", err)
	}
	if parked.Status.Phase != "Degraded" {
		t.Fatalf("phase = %q after the parking pass, want Degraded: the fixture did not refuse", parked.Status.Phase)
	}
	cond := meta.FindStatusCondition(parked.Status.Conditions, "Ready")
	if cond == nil || cond.Reason != reasonRuntimeClassNotFound {
		t.Fatalf("Ready condition = %+v after the parking pass, want reason %s", cond, reasonRuntimeClassNotFound)
	}
	stamped := parked.Status.LastReconcileTime
	if stamped == nil {
		t.Fatal("lastReconcileTime was not stamped by the parking pass")
	}

	for pass := 2; pass <= 1+degradedGateQuietPasses; pass++ {
		before := counter.writes
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d failed: %v", pass, err)
		}
		if counter.writes != before {
			t.Fatalf("pass %d wrote status %d time(s) with the refusal unchanged; each write re-enqueues the agent through the unfiltered watch (#1392)", pass, counter.writes-before)
		}
	}

	// The quiet passes left the status exactly as the parking pass wrote it,
	// timestamp included: lastReconcileTime is the time of the last write, the
	// same reading updateStatusReady gives it.
	after := &agentv1alpha1.PlatformAgent{}
	if err := r.Get(ctx, req.NamespacedName, after); err != nil {
		t.Fatalf("reading the agent back after the quiet passes: %v", err)
	}
	if after.Status.LastReconcileTime == nil || !after.Status.LastReconcileTime.Equal(stamped) {
		t.Errorf("lastReconcileTime moved from %v to %v across passes that wrote nothing", stamped, after.Status.LastReconcileTime)
	}
}

// TestADegradedChangeStillWrites is the other half of the gate: a quiet pass
// is quiet only because nothing moved. A new message, a new reason, or a new
// generation each get their write, and each is followed by quiet again.
func TestADegradedChangeStillWrites(t *testing.T) {
	agent := observedGenerationAgent(1)
	counter := &statusWriteCounter{}
	r := observedGenerationReconciler(agent, counter)
	ctx := context.Background()

	degrade := func(step, reason, message string, wantWrites int) {
		t.Helper()
		if err := r.updateStatusDegraded(ctx, agent, reason, message); err != nil {
			t.Fatalf("updateStatusDegraded (%s) failed: %v", step, err)
		}
		if counter.writes != wantWrites {
			t.Fatalf("%s: %d status writes in total, want %d", step, counter.writes, wantWrites)
		}
	}

	degrade("first refusal", reasonRuntimeClassNotFound, "RuntimeClass 'gvisor' is not configured", 1)
	degrade("same refusal again", reasonRuntimeClassNotFound, "RuntimeClass 'gvisor' is not configured", 1)
	first := agent.Status.LastReconcileTime
	if first == nil {
		t.Fatal("lastReconcileTime was not stamped by the first write")
	}

	degrade("message changed", reasonRuntimeClassNotFound, "RuntimeClass 'runsc' is not configured", 2)
	degrade("message unchanged", reasonRuntimeClassNotFound, "RuntimeClass 'runsc' is not configured", 2)

	degrade("reason changed", reasonShellSandboxKeysMissing, "RuntimeClass 'runsc' is not configured", 3)
	degrade("reason unchanged", reasonShellSandboxKeysMissing, "RuntimeClass 'runsc' is not configured", 3)
	if cond := meta.FindStatusCondition(agent.Status.Conditions, "Ready"); cond == nil || cond.Reason != reasonShellSandboxKeysMissing {
		t.Fatalf("Ready condition = %+v, want reason %s: the reason change was not written", cond, reasonShellSandboxKeysMissing)
	}

	// A spec edit that changes nothing about the refusal still gets a write,
	// so the status names the generation that was refused (#534). The fake
	// client copies stored metadata back over the in-memory object on every
	// status write, so the bump has to be stored as a real edit would be.
	agent.Generation = 2
	if err := r.Update(ctx, agent); err != nil {
		t.Fatalf("storing the generation bump: %v", err)
	}
	degrade("generation bumped", reasonShellSandboxKeysMissing, "RuntimeClass 'runsc' is not configured", 4)
	degrade("generation unchanged", reasonShellSandboxKeysMissing, "RuntimeClass 'runsc' is not configured", 4)
	if got := agent.Status.ObservedGeneration; got != 2 {
		t.Errorf("status.observedGeneration = %d after the bump, want 2", got)
	}
	if got := readyConditionGeneration(t, agent); got != 2 {
		t.Errorf("Ready condition observedGeneration = %d after the bump, want 2", got)
	}

	stored := &agentv1alpha1.PlatformAgent{}
	if err := r.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatalf("reading the agent back: %v", err)
	}
	if stored.Status.Phase != "Degraded" {
		t.Errorf("persisted phase = %q, want Degraded", stored.Status.Phase)
	}
	if stored.Status.LastReconcileTime == nil {
		t.Fatal("persisted lastReconcileTime is nil after four writes")
	}
	if stored.Status.LastReconcileTime.Before(first) {
		t.Errorf("persisted lastReconcileTime %v is earlier than the first write's %v", stored.Status.LastReconcileTime, first)
	}
}

// TestAPhaseChangeAloneWritesDegraded: the phase is part of the key on its
// own account. A status whose Ready condition already reads as the refusal
// while the phase says something else is corrected rather than left
// inconsistent. None of this controller's writers produces that pairing
// today; the test is what keeps the key honest if a later one does.
func TestAPhaseChangeAloneWritesDegraded(t *testing.T) {
	agent := observedGenerationAgent(1)
	agent.Status.Phase = "Provisioning"
	agent.Status.Conditions = []metav1.Condition{{
		Type:               "Ready",
		Status:             metav1.ConditionFalse,
		Reason:             reasonRuntimeClassNotFound,
		Message:            "RuntimeClass 'gvisor' is not configured",
		ObservedGeneration: 1,
		LastTransitionTime: metav1.Now(),
	}}
	counter := &statusWriteCounter{}
	r := observedGenerationReconciler(agent, counter)

	if err := r.updateStatusDegraded(context.Background(), agent, reasonRuntimeClassNotFound, "RuntimeClass 'gvisor' is not configured"); err != nil {
		t.Fatalf("updateStatusDegraded failed: %v", err)
	}
	if counter.writes != 1 {
		t.Fatalf("%d status writes with the phase reading Provisioning, want 1: the phase is part of the key", counter.writes)
	}
	if agent.Status.Phase != "Degraded" {
		t.Errorf("phase = %q, want Degraded", agent.Status.Phase)
	}
}

// TestAPrunedObservedGenerationDoesNotWriteDegradedEveryPass is the
// operator-ahead-of-its-CRD case for this writer, the same one
// TestAPrunedObservedGenerationDoesNotWriteEveryPass covers for
// updateStatusReady. A CRD that predates status.observedGeneration prunes the
// top-level field on every write; keyed on it, the gate would see
// 0 != generation on every pass and the loop would be back under exactly the
// skew a rolling upgrade produces.
func TestAPrunedObservedGenerationDoesNotWriteDegradedEveryPass(t *testing.T) {
	agent := observedGenerationAgent(1)
	counter := &statusWriteCounter{}
	funcs := counter.interceptors()
	persist := funcs.SubResourceUpdate
	funcs.SubResourceUpdate = func(ctx context.Context, cl client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
		if pa, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
			pa.Status.ObservedGeneration = 0
		}
		return persist(ctx, cl, subResourceName, obj, opts...)
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(funcs).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	ctx := context.Background()

	degrade := func() {
		t.Helper()
		if err := r.updateStatusDegraded(ctx, agent, reasonRuntimeClassNotFound, "RuntimeClass 'gvisor' is not configured"); err != nil {
			t.Fatalf("updateStatusDegraded failed: %v", err)
		}
	}
	degrade()
	if agent.Status.ObservedGeneration != 0 {
		t.Fatalf("the fixture did not prune status.observedGeneration (got %d); the test is not exercising the skew", agent.Status.ObservedGeneration)
	}
	if got := readyConditionGeneration(t, agent); got != 1 {
		t.Fatalf("Ready condition observedGeneration = %d under pruning, want 1: the condition's copy is the witness", got)
	}
	degrade()
	degrade()
	if counter.writes != 1 {
		t.Errorf("%d status writes across three unchanged refusals under a pruning CRD, want 1: this is the write-every-pass loop", counter.writes)
	}
}
