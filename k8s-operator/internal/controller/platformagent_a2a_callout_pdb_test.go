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
	"reflect"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	policyv1 "k8s.io/api/policy/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// calloutPDBEnvtestNamespace is the envtest case's namespace; the CR is
// envtestAgentName, shared with the other envtest files.
const calloutPDBEnvtestNamespace = "callout-pdb"

// These tests reach the callout's budget only through objects the operator
// already exposed before the budget existed — the reconcile, the Deployment
// builder, the teardown — so that they compile against a tree without it and
// fail there on the assertion rather than on a missing symbol.

// reconcileCalloutWithFake runs reconcileA2ACallout against a fake client and
// returns the budget and Deployment it applied.
func reconcileCalloutWithFake(t *testing.T, agent *agentv1alpha1.PlatformAgent, seed ...client.Object) (*policyv1.PodDisruptionBudget, *appsv1.Deployment) {
	t.Helper()
	ctx := context.Background()
	scheme := setupScheme()
	interceptors := fakeServerSideApplyInterceptors()
	if len(seed) > 0 {
		interceptors = pdbSSAInterceptors()
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(append([]client.Object{agent}, seed...)...).
		WithInterceptorFuncs(interceptors).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if _, err := r.reconcileA2ACallout(ctx, agent); err != nil {
		t.Fatalf("reconcileA2ACallout: %v", err)
	}
	key := types.NamespacedName{Name: a2aCalloutName(agent), Namespace: agent.Namespace}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, key, dep); err != nil {
		t.Fatalf("the callout Deployment was not applied: %v", err)
	}
	pdb := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, key, pdb); err != nil {
		t.Fatalf("no PodDisruptionBudget %s after reconcileA2ACallout: %v", key, err)
	}
	return pdb, dep
}

// The value, the owner and the selector. maxUnavailable 1 rather than
// minAvailable (SOP §3.3/§3.4) or 0 (no node holding a callout could ever
// drain); AlwaysAllow so a callout that is NotReady because the bus is down
// does not hold the drain of its node; a controller reference so the budget
// goes with the CR; and the Deployment's own selector, asserted as equality,
// because a budget that exists but selects the wrong pods passes a test for
// existence and leaves §3.3's finding open.
func TestReconcileA2ACalloutRendersAnEvictableBudget(t *testing.T) {
	agent := a2aTestAgent()
	pdb, dep := reconcileCalloutWithFake(t, agent)

	if pdb.Spec.MinAvailable != nil {
		t.Errorf("the callout budget sets minAvailable %v; that is the drain-deadlocking shape", pdb.Spec.MinAvailable)
	}
	if pdb.Spec.MaxUnavailable == nil || pdb.Spec.MaxUnavailable.Type != intstr.Int || pdb.Spec.MaxUnavailable.IntValue() != 1 {
		t.Errorf("maxUnavailable = %v, want the integer 1", pdb.Spec.MaxUnavailable)
	}
	if got := pdb.Spec.UnhealthyPodEvictionPolicy; got == nil || *got != policyv1.AlwaysAllow {
		t.Errorf("unhealthyPodEvictionPolicy = %v, want %s; with the bus down both callouts are Running/NotReady and the default budget refuses the drain of a workload already fully down",
			ptr.Deref(got, "<nil, i.e. IfHealthyBudget>"), policyv1.AlwaysAllow)
	}
	if !metav1.IsControlledBy(pdb, agent) {
		t.Errorf("the budget is not controlled by the PlatformAgent: %v", pdb.OwnerReferences)
	}
	if pdb.Spec.Selector == nil || len(pdb.Spec.Selector.MatchLabels) == 0 {
		t.Fatal("the callout budget has an empty selector; in policy/v1 that budgets every pod in the namespace")
	}
	if !reflect.DeepEqual(pdb.Spec.Selector, dep.Spec.Selector) {
		t.Errorf("budget selector %v != Deployment selector %v", pdb.Spec.Selector.MatchLabels, dep.Spec.Selector.MatchLabels)
	}
	// Labelled as part of the next stack, which is how the residue sweep
	// finds it.
	if got := pdb.Labels[labelPartOf]; got != a2aPartOf {
		t.Errorf("budget part-of label = %q, want %q", got, a2aPartOf)
	}
	if got := pdb.Labels[a2aComponentLabel]; got != "callout" {
		t.Errorf("budget component label = %q, want callout", got)
	}
}

// Disjointness, in both directions. The callout's budget must select none of
// the other next-stack workloads' pods — they all carry the same a2aLabels —
// and no other budget, or selector a budget would be built from, may select
// the callout's pods. Either overlap lets an eviction be charged to the wrong
// allowance: a drain could take both callouts while a budget reads satisfied
// (SOP §3.23, pdb-overlapping). The verifier's test asserts its own pair the
// same way; this is the callout's half.
func TestTheCalloutBudgetAndEveryOtherBudgetAreDisjoint(t *testing.T) {
	agent := a2aTestAgent()
	pdb, dep := reconcileCalloutWithFake(t, agent)

	ours, err := metav1.LabelSelectorAsSelector(pdb.Spec.Selector)
	if err != nil {
		t.Fatalf("callout budget selector does not parse: %v", err)
	}
	calloutPods := labels.Set(dep.Spec.Template.Labels)
	if !ours.Matches(calloutPods) {
		t.Fatalf("the callout budget %v does not select the callout pods %v", pdb.Spec.Selector.MatchLabels, calloutPods)
	}

	// Every other pod-selecting object the operator renders in the
	// namespace that a budget is, or would be built from: the two real
	// budgets, and the other two-replica-capable Deployments' selectors.
	verifier := buildA2AVerifierDeployment(agent)
	gateway := buildA2AGatewayDeployment(agent)
	platformPDB := buildPlatformPDB(agent)
	others := []struct {
		name     string
		selector *metav1.LabelSelector
		pods     labels.Set
	}{
		{"verifier budget", buildA2AVerifierPDB(agent).Spec.Selector, labels.Set(verifier.Spec.Template.Labels)},
		{"a2a gateway selector", gateway.Spec.Selector, labels.Set(gateway.Spec.Template.Labels)},
		{"platform budget", platformPDB.Spec.Selector, labels.Set(platformPDB.Spec.Selector.MatchLabels)},
	}
	for _, o := range others {
		if ours.Matches(o.pods) {
			t.Errorf("the callout budget also selects the %s's pods %v", o.name, o.pods)
		}
		theirs, err := metav1.LabelSelectorAsSelector(o.selector)
		if err != nil {
			t.Fatalf("%s does not parse: %v", o.name, err)
		}
		if theirs.Matches(calloutPods) {
			t.Errorf("the %s %v also selects the callout pods %v; an eviction of a callout would be charged to it", o.name, o.selector.MatchLabels, calloutPods)
		}
	}
}

// The spread: one hostname constraint, advisory, over the Deployment's own
// selector, scoped to one ReplicaSet. ScheduleAnyway is asserted because a
// DoNotSchedule here pends the surge pod of the MaxUnavailable 0 / MaxSurge 1
// rollout on a two-node cluster, and a single-node install's second replica
// forever.
func TestTheCalloutSpreadsAcrossNodesWithoutRefusingToSchedule(t *testing.T) {
	agent := identityTestAgent()
	dep := buildA2ACalloutDeployment(agent)
	pod := dep.Spec.Template.Spec

	if len(pod.TopologySpreadConstraints) != 1 {
		t.Fatalf("the callout pod carries %d topology spread constraints, want 1 on the hostname", len(pod.TopologySpreadConstraints))
	}
	spread := pod.TopologySpreadConstraints[0]
	if spread.TopologyKey != "kubernetes.io/hostname" {
		t.Errorf("spread topologyKey = %q, want kubernetes.io/hostname", spread.TopologyKey)
	}
	if spread.MaxSkew != 1 {
		t.Errorf("spread maxSkew = %d, want 1", spread.MaxSkew)
	}
	if spread.WhenUnsatisfiable != corev1.ScheduleAnyway {
		t.Errorf("spread whenUnsatisfiable = %q, want ScheduleAnyway", spread.WhenUnsatisfiable)
	}
	if !reflect.DeepEqual(spread.LabelSelector, dep.Spec.Selector) {
		t.Errorf("spread selector %v != Deployment selector %v", spread.LabelSelector, dep.Spec.Selector)
	}
	if !reflect.DeepEqual(spread.MatchLabelKeys, []string{"pod-template-hash"}) {
		t.Errorf("spread matchLabelKeys = %v, want [pod-template-hash]", spread.MatchLabelKeys)
	}
	if pod.Affinity != nil && pod.Affinity.PodAntiAffinity != nil {
		t.Error("the callout pod carries pod anti-affinity as well as a topology spread; one mechanism, keyed on one selector")
	}
	// The rollout strategy stays what it was: the budget is the drain-side
	// half, not a replacement for it.
	ru := dep.Spec.Strategy.RollingUpdate
	if ru == nil || ru.MaxUnavailable == nil || ru.MaxUnavailable.IntValue() != 0 {
		t.Errorf("rollout maxUnavailable = %v, want 0", ru)
	}
}

// A hand-set minAvailable on the live budget. A forced apply cannot remove a
// field it never owned, so without clearForeignPDBBudgetField every apply
// merges to both fields and is refused, and because the callout step sits
// ahead of the verifier, the fences and the provision Job in reconcileA2A,
// the whole next render fails from then on.
func TestReconcileA2ACalloutRecoversFromAForeignBudgetField(t *testing.T) {
	agent := a2aTestAgent()
	live := &policyv1.PodDisruptionBudget{
		ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutName(agent), Namespace: agent.Namespace},
		Spec: policyv1.PodDisruptionBudgetSpec{
			MinAvailable: ptr.To(intstr.FromInt32(1)),
			Selector:     buildA2ACalloutDeployment(agent).Spec.Selector,
		},
	}
	pdb, _ := reconcileCalloutWithFake(t, agent, live)

	if pdb.Spec.MinAvailable != nil {
		t.Errorf("minAvailable survived the reconcile: %v", pdb.Spec.MinAvailable)
	}
	if pdb.Spec.MaxUnavailable == nil || pdb.Spec.MaxUnavailable.IntValue() != 1 {
		t.Errorf("maxUnavailable = %v, want 1", pdb.Spec.MaxUnavailable)
	}
	if got := pdb.Spec.UnhealthyPodEvictionPolicy; got == nil || *got != policyv1.AlwaysAllow {
		t.Errorf("unhealthyPodEvictionPolicy after recovery = %v, want %s", ptr.Deref(got, "<nil>"), policyv1.AlwaysAllow)
	}
}

// Rendered under next, gone under today, through the full Reconcile. The
// label sweep in TestNothingA2ALabelledSurvivesAFlipToToday is what catches a
// budget nobody added to the teardown; this is the callout-specific statement.
func TestTheCalloutBudgetComesAndGoesWithTheMode(t *testing.T) {
	ctx := context.Background()
	scheme := setupScheme()
	agent := a2aTestAgent()

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	key := types.NamespacedName{Name: a2aCalloutName(agent), Namespace: agent.Namespace}

	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d under next: %v", i+1, err)
		}
	}
	if err := cl.Get(ctx, key, &policyv1.PodDisruptionBudget{}); err != nil {
		t.Fatalf("no callout budget under mode next: %v", err)
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
		t.Fatalf("Reconcile after the flip: %v", err)
	}
	if err := cl.Get(ctx, key, &policyv1.PodDisruptionBudget{}); !errors.IsNotFound(err) {
		t.Errorf("the callout budget survives a flip to today (get: %v)", err)
	}
}

// The same statements against a real API server: the budget is admitted with
// both its fields and the owner reference, the spread's matchLabelKeys
// survives admission (a server with the gate off would drop it silently, and
// a fake client would never notice), a hand edit to minAvailable made by
// another field manager is recovered by the next reconcile, and the flip to
// today deletes the budget. No disruption controller runs under envtest, so
// the budget's status and the eviction API's answers are not exercised here.
func TestTheCalloutBudgetAndSpreadOnARealAPIServerEnvtest(t *testing.T) {
	cl, scheme := startEnvtest(t)
	ctx := context.Background()

	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: calloutPDBEnvtestNamespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: envtestAgentName, Namespace: calloutPDBEnvtestNamespace},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Mode: ptr.To("next"),
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   envtestHarnessProject,
				Location:    envtestHarnessLocation,
				ClusterName: envtestHarnessCluster,
			},
		},
	}
	if err := cl.Create(ctx, agent); err != nil {
		t.Fatalf("creating the PlatformAgent: %v", err)
	}
	if err := cl.Create(ctx, shellSandboxKeysSecret(agent)); err != nil {
		t.Fatalf("creating sandbox keys Secret: %v", err)
	}

	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	key := types.NamespacedName{Name: a2aCalloutName(agent), Namespace: agent.Namespace}
	// The first pass adds the finalizer; the second renders. Later steps of
	// the next render wait on things no kubelet provides here, so an error
	// is logged and the objects the callout step applied are what is read.
	reconcile := func(pass string) {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Logf("Reconcile (%s) returned %v; reading what the pass applied", pass, err)
		}
	}
	reconcile("finalizer")
	reconcile("render")

	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), agent); err != nil {
		t.Fatalf("re-reading the PlatformAgent: %v", err)
	}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, key, dep); err != nil {
		t.Fatalf("the API server holds no callout Deployment after the render: %v", err)
	}
	pdb := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, key, pdb); err != nil {
		t.Fatalf("the API server holds no callout PodDisruptionBudget after the render: %v", err)
	}
	if pdb.Spec.MaxUnavailable == nil || pdb.Spec.MaxUnavailable.IntValue() != 1 || pdb.Spec.MinAvailable != nil {
		t.Errorf("admitted budget = maxUnavailable %v / minAvailable %v, want 1 / unset", pdb.Spec.MaxUnavailable, pdb.Spec.MinAvailable)
	}
	if got := pdb.Spec.UnhealthyPodEvictionPolicy; got == nil || *got != policyv1.AlwaysAllow {
		t.Errorf("admitted unhealthyPodEvictionPolicy = %v, want %s", ptr.Deref(got, "<nil>"), policyv1.AlwaysAllow)
	}
	if !reflect.DeepEqual(pdb.Spec.Selector, dep.Spec.Selector) {
		t.Errorf("admitted budget selector %v != admitted Deployment selector %v", pdb.Spec.Selector, dep.Spec.Selector)
	}
	if !metav1.IsControlledBy(pdb, agent) {
		t.Errorf("the admitted budget is not controlled by the PlatformAgent (uid %s): %v", agent.UID, pdb.OwnerReferences)
	}
	spreads := dep.Spec.Template.Spec.TopologySpreadConstraints
	if len(spreads) != 1 {
		t.Fatalf("the admitted callout pod template carries %d spread constraints, want 1", len(spreads))
	}
	if !reflect.DeepEqual(spreads[0].MatchLabelKeys, []string{"pod-template-hash"}) {
		t.Errorf("admitted spread matchLabelKeys = %v, want [pod-template-hash]; the API server dropped or rewrote it", spreads[0].MatchLabelKeys)
	}
	if spreads[0].WhenUnsatisfiable != corev1.ScheduleAnyway || spreads[0].TopologyKey != "kubernetes.io/hostname" {
		t.Errorf("admitted spread = %s on %q, want ScheduleAnyway on kubernetes.io/hostname", spreads[0].WhenUnsatisfiable, spreads[0].TopologyKey)
	}

	// A hand edit under another field manager: minAvailable in, maxUnavailable
	// out. The next reconcile has to land the operator's budget again.
	pdb.Spec.MaxUnavailable = nil
	pdb.Spec.MinAvailable = ptr.To(intstr.FromInt32(1))
	if err := cl.Update(ctx, pdb, client.FieldOwner("kubectl-edit")); err != nil {
		t.Fatalf("hand-editing the budget to minAvailable: %v", err)
	}
	reconcile("after the hand edit")
	recovered := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, key, recovered); err != nil {
		t.Fatalf("get budget after the hand edit: %v", err)
	}
	if recovered.Spec.MinAvailable != nil || recovered.Spec.MaxUnavailable == nil || recovered.Spec.MaxUnavailable.IntValue() != 1 {
		t.Errorf("after the hand edit and a reconcile the budget is maxUnavailable %v / minAvailable %v, want 1 / unset",
			recovered.Spec.MaxUnavailable, recovered.Spec.MinAvailable)
	}

	// The flip to today.
	agent.Spec.Mode = nil
	if err := cl.Update(ctx, agent); err != nil {
		t.Fatalf("flip to today: %v", err)
	}
	reconcile("today")
	if err := cl.Get(ctx, key, &policyv1.PodDisruptionBudget{}); !errors.IsNotFound(err) {
		t.Errorf("the callout budget survives a flip to today on a real API server (get: %v)", err)
	}
}
