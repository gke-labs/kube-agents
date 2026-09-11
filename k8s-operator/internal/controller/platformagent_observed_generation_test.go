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
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// What status.observedGeneration has to mean on a PlatformAgent (#534). Before
// it existed, Ready=True on a CR whose spec had just changed described the
// previous generation and nothing on the object said so. These tests pin the
// two halves of the fix: every status write records the generation it was
// computed from, on the status and on the conditions, and a generation bump
// that changes nothing else still gets a write.
//
// The fake client does not maintain metadata.generation, so the tests set it
// by hand. The envtest case in platformagent_observed_generation_envtest_test.go
// is where a real API server assigns it.

// observedGenerationAgent is the CR under test, already at the generation the
// API server would have assigned after generation-1 spec edits.
func observedGenerationAgent(generation int64) *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns", Generation: generation},
	}
}

// statusWriteCounter wraps the SSA interceptors with a count of status
// subresource writes, so a test can say which passes wrote and which did not.
type statusWriteCounter struct{ writes int }

func (c *statusWriteCounter) interceptors() interceptor.Funcs {
	funcs := fakeServerSideApplyInterceptors()
	funcs.SubResourceUpdate = func(ctx context.Context, cl client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
		c.writes++
		return cl.SubResource(subResourceName).Update(ctx, obj, opts...)
	}
	return funcs
}

// observedGenerationReconciler holds the agent and all three workloads ready,
// so the status under test is Ready and the only thing varying is the generation.
func observedGenerationReconciler(agent *agentv1alpha1.PlatformAgent, counter *statusWriteCounter) *PlatformAgentReconciler {
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1)).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(counter.interceptors()).
		Build()
	return &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
}

func settleReady(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent) string {
	t.Helper()
	ctx := context.Background()
	phase, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent))
	if err != nil {
		t.Fatalf("updateStatusReady failed: %v", err)
	}
	return phase
}

// readyConditionGeneration is the condition's own observedGeneration, which is
// what a `kubectl wait`-style caller reads to tell a stale Ready from a current one.
func readyConditionGeneration(t *testing.T, agent *agentv1alpha1.PlatformAgent) int64 {
	t.Helper()
	cond := meta.FindStatusCondition(agent.Status.Conditions, "Ready")
	if cond == nil {
		t.Fatal("no Ready condition was written")
	}
	return cond.ObservedGeneration
}

// TestReadyStatusRecordsTheGenerationItWasComputedFrom is the field itself: a
// Ready status names the generation it describes, on the status and on the
// condition, and the persisted object agrees with the in-memory one.
func TestReadyStatusRecordsTheGenerationItWasComputedFrom(t *testing.T) {
	agent := observedGenerationAgent(3)
	r := observedGenerationReconciler(agent, &statusWriteCounter{})

	if phase := settleReady(t, r, agent); phase != "Ready" {
		t.Fatalf("got phase %q, want Ready: every workload has a ready replica", phase)
	}
	if got := agent.Status.ObservedGeneration; got != 3 {
		t.Errorf("status.observedGeneration = %d, want 3", got)
	}
	if got := readyConditionGeneration(t, agent); got != 3 {
		t.Errorf("Ready condition observedGeneration = %d, want 3", got)
	}

	stored := &agentv1alpha1.PlatformAgent{}
	if err := r.Get(context.Background(), client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatalf("reading the agent back: %v", err)
	}
	if got := stored.Status.ObservedGeneration; got != 3 {
		t.Errorf("persisted status.observedGeneration = %d, want 3", got)
	}
	if got := readyConditionGeneration(t, stored); got != 3 {
		t.Errorf("persisted Ready condition observedGeneration = %d, want 3", got)
	}
}

// TestAGenerationBumpAloneWritesStatus is the equality half. Nothing the status
// derives from changes between the passes — same workloads, same phase, same
// message — so the only reason the third pass writes is that the generation
// moved. Without that write the status would keep claiming generation 1 forever
// and the field would be decoration.
func TestAGenerationBumpAloneWritesStatus(t *testing.T) {
	agent := observedGenerationAgent(1)
	counter := &statusWriteCounter{}
	r := observedGenerationReconciler(agent, counter)

	settleReady(t, r, agent)
	if counter.writes != 1 {
		t.Fatalf("first pass made %d status writes, want 1", counter.writes)
	}
	settleReady(t, r, agent)
	if counter.writes != 1 {
		t.Fatalf("an unchanged pass made a status write (%d total); the equality check is broken", counter.writes)
	}

	// The fake client does not manage metadata.generation and copies the stored
	// object's metadata back over the in-memory one on every status write, so
	// the bump has to be stored as well as set, as a real spec edit would be.
	agent.Generation = 2
	if err := r.Update(context.Background(), agent); err != nil {
		t.Fatalf("storing the generation bump: %v", err)
	}
	if agent.Generation != 2 {
		t.Fatalf("the fake client rewrote metadata.generation to %d; the fixture needs another way to bump it", agent.Generation)
	}
	settleReady(t, r, agent)
	if counter.writes != 2 {
		t.Fatalf("a generation bump made %d status writes in total, want 2: the bump alone must write", counter.writes)
	}
	if got := agent.Status.ObservedGeneration; got != 2 {
		t.Errorf("status.observedGeneration = %d after the bump, want 2", got)
	}
	if got := readyConditionGeneration(t, agent); got != 2 {
		t.Errorf("Ready condition observedGeneration = %d after the bump, want 2", got)
	}

	settleReady(t, r, agent)
	if counter.writes != 2 {
		t.Errorf("the pass after the bump wrote again (%d total); the new generation should now read as unchanged", counter.writes)
	}
}

// TestDegradedStatusRecordsTheGenerationItWasComputedFrom covers the other
// writer. A refusal is as much a verdict on a generation as Ready is, and a
// caller waiting on the condition needs to know which spec was refused.
func TestDegradedStatusRecordsTheGenerationItWasComputedFrom(t *testing.T) {
	agent := observedGenerationAgent(5)
	r := observedGenerationReconciler(agent, &statusWriteCounter{})

	if err := r.updateStatusDegraded(context.Background(), agent, reasonRuntimeClassNotFound, "RuntimeClass 'gvisor' is not configured in this cluster"); err != nil {
		t.Fatalf("updateStatusDegraded failed: %v", err)
	}
	if agent.Status.Phase != "Degraded" {
		t.Errorf("got phase %q, want Degraded", agent.Status.Phase)
	}
	if got := agent.Status.ObservedGeneration; got != 5 {
		t.Errorf("status.observedGeneration = %d, want 5", got)
	}
	if got := readyConditionGeneration(t, agent); got != 5 {
		t.Errorf("Ready condition observedGeneration = %d, want 5", got)
	}
}

// TestRBACSkewConditionCarriesTheGenerationButDoesNotClaimIt: reportRBACSkew
// writes status before the spec has been acted on, so it stamps its own
// condition and leaves status.observedGeneration alone. Claiming the generation
// there would let a reconcile that errors out afterwards leave a status that
// says "generation N seen" beside a Ready condition computed from N-1 — the
// exact stale success the field exists to expose.
func TestRBACSkewConditionCarriesTheGenerationButDoesNotClaimIt(t *testing.T) {
	agent := observedGenerationAgent(4)
	r := observedGenerationReconciler(agent, &statusWriteCounter{})
	authorizer := &fakeAuthorizer{deny: denyPDBPatch}
	r.RBAC = NewRBACChecker(authorizer.reviews())
	ctx := context.Background()
	if _, err := r.RBAC.Probe(ctx); err != nil {
		t.Fatalf("boot probe: %v", err)
	}

	degraded, err := r.reportRBACSkew(ctx, agent)
	if err != nil {
		t.Fatalf("reportRBACSkew failed: %v", err)
	}
	if !degraded {
		t.Fatal("reportRBACSkew reported no denial against an authorizer that denies")
	}
	cond := meta.FindStatusCondition(agent.Status.Conditions, degradedConditionType)
	if cond == nil {
		t.Fatal("no Degraded condition was written")
	}
	if got := cond.ObservedGeneration; got != 4 {
		t.Errorf("Degraded condition observedGeneration = %d, want 4", got)
	}
	if got := agent.Status.ObservedGeneration; got != 0 {
		t.Errorf("status.observedGeneration = %d after an RBAC-only write, want 0: the phase was not recomputed", got)
	}
}

// TestAPrunedObservedGenerationDoesNotWriteEveryPass is the operator-ahead-of-
// its-CRD case. A CRD that predates status.observedGeneration prunes the
// top-level field on every write, so if the equality check were keyed on it
// the operator would see 0 != generation on every pass and write again, each
// write waking the next reconcile. The Ready condition's observedGeneration
// has always been in the schema, so it is the witness; the pruning is
// simulated by dropping the top-level field before the fake persists it.
func TestAPrunedObservedGenerationDoesNotWriteEveryPass(t *testing.T) {
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
		WithObjects(agent, readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1)).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(funcs).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}

	settleReady(t, r, agent)
	if agent.Status.ObservedGeneration != 0 {
		t.Fatalf("the fixture did not prune status.observedGeneration (got %d); the test is not exercising the skew", agent.Status.ObservedGeneration)
	}
	if got := readyConditionGeneration(t, agent); got != 1 {
		t.Fatalf("Ready condition observedGeneration = %d under pruning, want 1: the condition's copy is the witness", got)
	}
	settleReady(t, r, agent)
	settleReady(t, r, agent)
	if counter.writes != 1 {
		t.Errorf("%d status writes across three unchanged passes under a pruning CRD, want 1: this is the write-every-pass loop", counter.writes)
	}

	agent.Generation = 2
	if err := r.Update(context.Background(), agent); err != nil {
		t.Fatalf("storing the generation bump: %v", err)
	}
	settleReady(t, r, agent)
	settleReady(t, r, agent)
	if counter.writes != 2 {
		t.Errorf("%d status writes after a generation bump under a pruning CRD, want 2: one for the bump, then quiet", counter.writes)
	}
}

// TestEventWatcherConditionCarriesTheGeneration: the emergency-stop condition
// is written in the same pass as Ready and must name the same generation, or
// the doc's claim about the conditions written alongside Ready is false.
func TestEventWatcherConditionCarriesTheGeneration(t *testing.T) {
	agent := observedGenerationAgent(6)
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{EventWatcher: &agentv1alpha1.EventWatcherSpec{Enabled: ptr.To(false)}}
	r := observedGenerationReconciler(agent, &statusWriteCounter{})

	settleReady(t, r, agent)
	cond := meta.FindStatusCondition(agent.Status.Conditions, eventWatcherConditionType)
	if cond == nil {
		t.Fatal("no EventWatcher condition was written with the watcher disabled")
	}
	if got := cond.ObservedGeneration; got != 6 {
		t.Errorf("EventWatcher condition observedGeneration = %d, want 6", got)
	}
}

// TestDegradedGitRepoConditionCarriesTheGeneration covers the Degraded
// condition updateStatusReady itself writes, as distinct from the RBAC one.
func TestDegradedGitRepoConditionCarriesTheGeneration(t *testing.T) {
	agent := observedGenerationAgent(7)
	agent.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{IntegrationSpec: agentv1alpha1.IntegrationSpec{GitHub: &agentv1alpha1.GitHubSpec{Org: "-invalid-org-"}}}
	r := observedGenerationReconciler(agent, &statusWriteCounter{})

	if phase := settleReady(t, r, agent); phase != "Degraded" {
		t.Fatalf("got phase %q with an invalid org, want Degraded", phase)
	}
	cond := meta.FindStatusCondition(agent.Status.Conditions, degradedConditionType)
	if cond == nil {
		t.Fatal("no Degraded condition was written for the invalid org")
	}
	if cond.Reason != conditionReasonInvalidGitRepoURL {
		t.Fatalf("Degraded reason = %q, want %q", cond.Reason, conditionReasonInvalidGitRepoURL)
	}
	if got := cond.ObservedGeneration; got != 7 {
		t.Errorf("Degraded condition observedGeneration = %d, want 7", got)
	}
}
