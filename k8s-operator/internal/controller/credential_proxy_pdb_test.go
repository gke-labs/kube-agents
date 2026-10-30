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
	policyv1 "k8s.io/api/policy/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// gke-labs/kube-agents#2711: the credential proxy was the one workload of the
// install with no PodDisruptionBudget. On one replica a budget protects
// nothing (credentialProxyPDBMaxUnavailable has the argument), so what these
// tests pin is the shape that cannot hold a drain, and that the proxy's
// reconcile renders it at all.

// reconcileProxyWithFake runs reconcileCredentialProxy against a fake client
// and returns the budget and Deployment it applied, on
// reconcileCalloutWithFake's pattern.
func reconcileProxyWithFake(t *testing.T, agent *agentv1alpha1.PlatformAgent, seed ...client.Object) (*policyv1.PodDisruptionBudget, *appsv1.Deployment) {
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
	if err := r.reconcileCredentialProxy(ctx, agent, brokerTestPolicyHash, nil, true); err != nil {
		t.Fatalf("reconcileCredentialProxy: %v", err)
	}
	key := types.NamespacedName{Name: credentialProxyName(agent), Namespace: agent.Namespace}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, key, dep); err != nil {
		t.Fatalf("the proxy Deployment was not applied: %v", err)
	}
	pdb := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, key, pdb); err != nil {
		t.Fatalf("no PodDisruptionBudget %s after reconcileCredentialProxy: %v", key, err)
	}
	return pdb, dep
}

// The value, the policy, the owner and the selector. maxUnavailable 1, never
// minAvailable (SOP §3.3/§3.4: on one replica that is the budget that wedges
// every drain on the cluster); AlwaysAllow so a proxy that is Running and
// NotReady can still be evicted; a controller reference so the budget goes
// with the CR; and the Deployment's own selector, asserted as equality,
// because a budget that exists but selects the wrong pods passes a test for
// existence and leaves the issue open. The replica count is pinned beside it
// because the budget's whole argument rests on it.
func TestReconcileCredentialProxyRendersAnEvictableBudget(t *testing.T) {
	agent := a2aTestAgent()
	pdb, dep := reconcileProxyWithFake(t, agent)

	if got := ptr.Deref(dep.Spec.Replicas, 0); got != 1 {
		t.Errorf("proxy replicas = %d, want 1; the budget below is argued for one replica", got)
	}
	if pdb.Spec.MinAvailable != nil {
		t.Errorf("the proxy budget sets minAvailable %v; on one replica that is the drain-deadlocking shape", pdb.Spec.MinAvailable)
	}
	if pdb.Spec.MaxUnavailable == nil || pdb.Spec.MaxUnavailable.Type != intstr.Int || pdb.Spec.MaxUnavailable.IntValue() != 1 {
		t.Errorf("maxUnavailable = %v, want the integer 1", pdb.Spec.MaxUnavailable)
	}
	if got := pdb.Spec.UnhealthyPodEvictionPolicy; got == nil || *got != policyv1.AlwaysAllow {
		t.Errorf("unhealthyPodEvictionPolicy = %v, want %s; under the default a proxy that is Running/NotReady has currentHealthy 0 against desiredHealthy 0 and its node cannot drain",
			ptr.Deref(got, "<nil, i.e. IfHealthyBudget>"), policyv1.AlwaysAllow)
	}
	if !metav1.IsControlledBy(pdb, agent) {
		t.Errorf("the budget is not controlled by the PlatformAgent: %v", pdb.OwnerReferences)
	}
	if pdb.Spec.Selector == nil || len(pdb.Spec.Selector.MatchLabels) == 0 {
		t.Fatal("the proxy budget has an empty selector; in policy/v1 that budgets every pod in the namespace")
	}
	if !reflect.DeepEqual(pdb.Spec.Selector, dep.Spec.Selector) {
		t.Errorf("budget selector %v != Deployment selector %v", pdb.Spec.Selector.MatchLabels, dep.Spec.Selector.MatchLabels)
	}
	selector, err := metav1.LabelSelectorAsSelector(pdb.Spec.Selector)
	if err != nil {
		t.Fatalf("proxy budget selector does not parse: %v", err)
	}
	if pods := labels.Set(dep.Spec.Template.Labels); !selector.Matches(pods) {
		t.Errorf("the proxy budget %v does not select the proxy pods %v", pdb.Spec.Selector.MatchLabels, pods)
	}
}

// Disjointness, in both directions, against every other budget the operator
// renders in the namespace (SOP §3.23, pdb-overlapping): an overlap lets an
// eviction be charged to the wrong allowance. The callout's and the
// verifier's tests assert their own pairs; this is the proxy's.
func TestTheCredentialProxyBudgetAndEveryOtherBudgetAreDisjoint(t *testing.T) {
	agent := a2aTestAgent()
	pdb, dep := reconcileProxyWithFake(t, agent)

	ours, err := metav1.LabelSelectorAsSelector(pdb.Spec.Selector)
	if err != nil {
		t.Fatalf("proxy budget selector does not parse: %v", err)
	}
	proxyPods := labels.Set(dep.Spec.Template.Labels)

	callout := buildA2ACalloutDeployment(agent, "")
	verifier := buildA2AVerifierDeployment(agent)
	platformPDB := buildPlatformPDB(agent)
	others := []struct {
		name     string
		selector *metav1.LabelSelector
		pods     labels.Set
	}{
		{"platform budget", platformPDB.Spec.Selector, labels.Set(platformPDB.Spec.Selector.MatchLabels)},
		{"callout budget", buildA2ACalloutPDB(agent).Spec.Selector, labels.Set(callout.Spec.Template.Labels)},
		{"verifier budget", buildA2AVerifierPDB(agent).Spec.Selector, labels.Set(verifier.Spec.Template.Labels)},
	}
	for _, o := range others {
		if ours.Matches(o.pods) {
			t.Errorf("the proxy budget also selects the %s's pods %v", o.name, o.pods)
		}
		theirs, err := metav1.LabelSelectorAsSelector(o.selector)
		if err != nil {
			t.Fatalf("%s does not parse: %v", o.name, err)
		}
		if theirs.Matches(proxyPods) {
			t.Errorf("the %s %v also selects the proxy pods %v; an eviction of the proxy would be charged to it", o.name, o.selector.MatchLabels, proxyPods)
		}
	}
}

// A hand-set minAvailable on the live budget. A forced apply cannot remove a
// field it never owned, so without clearForeignPDBBudgetField every apply
// merges to both fields and is refused, and because the budget sits ahead of
// the NetworkPolicy in reconcileCredentialProxy, the proxy's render fails from
// then on.
func TestReconcileCredentialProxyRecoversFromAForeignBudgetField(t *testing.T) {
	agent := a2aTestAgent()
	live := &policyv1.PodDisruptionBudget{
		ObjectMeta: metav1.ObjectMeta{Name: credentialProxyName(agent), Namespace: agent.Namespace},
		Spec: policyv1.PodDisruptionBudgetSpec{
			MinAvailable: ptr.To(intstr.FromInt32(1)),
			Selector:     buildCredentialProxyDeployment(agent, brokerTestPolicyHash).Spec.Selector,
		},
	}
	pdb, _ := reconcileProxyWithFake(t, agent, live)

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
