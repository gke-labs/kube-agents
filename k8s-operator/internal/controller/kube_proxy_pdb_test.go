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
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func reconcileKubeProxyWithFake(t *testing.T, agent *agentv1alpha1.PlatformAgent, seed ...client.Object) (*policyv1.PodDisruptionBudget, *appsv1.Deployment) {
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
	if err := r.reconcileKubeProxy(ctx, agent, "", true); err != nil {
		t.Fatalf("reconcileKubeProxy: %v", err)
	}
	key := types.NamespacedName{Name: kubeProxyName(agent), Namespace: agent.Namespace}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, key, dep); err != nil {
		t.Fatalf("the kube-proxy Deployment was not applied: %v", err)
	}
	pdb := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, key, pdb); err != nil {
		t.Fatalf("no PodDisruptionBudget %s after reconcileKubeProxy: %v", key, err)
	}
	return pdb, dep
}

func TestReconcileKubeProxyRendersAnEvictableBudget(t *testing.T) {
	agent := a2aTestAgent()
	pdb, dep := reconcileKubeProxyWithFake(t, agent)

	if got := ptr.Deref(dep.Spec.Replicas, 0); got != kubeProxyReplicas {
		t.Errorf("kube-proxy replicas = %d, want %d", got, kubeProxyReplicas)
	}
	if dep.Spec.Strategy.Type != appsv1.RollingUpdateDeploymentStrategyType {
		t.Errorf("kube-proxy strategy type = %q, want %q", dep.Spec.Strategy.Type, appsv1.RollingUpdateDeploymentStrategyType)
	}
	ru := dep.Spec.Strategy.RollingUpdate
	if ru == nil || ru.MaxUnavailable == nil || ru.MaxUnavailable.Type != intstr.Int || ru.MaxUnavailable.IntValue() != 1 {
		t.Errorf("kube-proxy RollingUpdate maxUnavailable = %v, want integer 1", ru)
	}
	if ru == nil || ru.MaxSurge == nil || ru.MaxSurge.Type != intstr.Int || ru.MaxSurge.IntValue() != 1 {
		t.Errorf("kube-proxy RollingUpdate maxSurge = %v, want integer 1", ru)
	}
	spreads := dep.Spec.Template.Spec.TopologySpreadConstraints
	if len(spreads) != 1 {
		t.Fatalf("kube-proxy topologySpreadConstraints count = %d, want 1", len(spreads))
	}
	spread := spreads[0]
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

	if pdb.Spec.MinAvailable != nil {
		t.Errorf("the kube-proxy budget sets minAvailable %v; want unset", pdb.Spec.MinAvailable)
	}
	if pdb.Spec.MaxUnavailable == nil || pdb.Spec.MaxUnavailable.Type != intstr.Int || pdb.Spec.MaxUnavailable.IntValue() != 1 {
		t.Errorf("maxUnavailable = %v, want the integer 1", pdb.Spec.MaxUnavailable)
	}
	if got := pdb.Spec.UnhealthyPodEvictionPolicy; got == nil || *got != policyv1.AlwaysAllow {
		t.Errorf("unhealthyPodEvictionPolicy = %v, want %s", ptr.Deref(got, "<nil>"), policyv1.AlwaysAllow)
	}
	if !metav1.IsControlledBy(pdb, agent) {
		t.Errorf("the budget is not controlled by the PlatformAgent: %v", pdb.OwnerReferences)
	}
	if pdb.Spec.Selector == nil || len(pdb.Spec.Selector.MatchLabels) == 0 {
		t.Fatal("the kube-proxy budget has an empty selector")
	}
	if !reflect.DeepEqual(pdb.Spec.Selector, dep.Spec.Selector) {
		t.Errorf("budget selector %v != Deployment selector %v", pdb.Spec.Selector.MatchLabels, dep.Spec.Selector.MatchLabels)
	}
	selector, err := metav1.LabelSelectorAsSelector(pdb.Spec.Selector)
	if err != nil {
		t.Fatalf("kube-proxy budget selector does not parse: %v", err)
	}
	if pods := labels.Set(dep.Spec.Template.Labels); !selector.Matches(pods) {
		t.Errorf("the kube-proxy budget %v does not select the kube-proxy pods %v", pdb.Spec.Selector.MatchLabels, pods)
	}
}

func TestTheKubeProxyBudgetAndEveryOtherBudgetAreDisjoint(t *testing.T) {
	agent := a2aTestAgent()
	pdb, dep := reconcileKubeProxyWithFake(t, agent)

	ours, err := metav1.LabelSelectorAsSelector(pdb.Spec.Selector)
	if err != nil {
		t.Fatalf("kube-proxy budget selector does not parse: %v", err)
	}
	kubeProxyPods := labels.Set(dep.Spec.Template.Labels)

	credentialProxy := buildCredentialProxyDeployment(agent, brokerTestPolicyHash)
	callout := buildA2ACalloutDeployment(agent, "", 1)
	verifier := buildA2AVerifierDeployment(agent)
	platformPDB := buildPlatformPDB(agent)
	others := []struct {
		name     string
		selector *metav1.LabelSelector
		pods     labels.Set
	}{
		{"platform budget", platformPDB.Spec.Selector, labels.Set(platformPDB.Spec.Selector.MatchLabels)},
		{"credential-proxy budget", buildCredentialProxyPDB(agent).Spec.Selector, labels.Set(credentialProxy.Spec.Template.Labels)},
		{"callout budget", buildA2ACalloutPDB(agent).Spec.Selector, labels.Set(callout.Spec.Template.Labels)},
		{"verifier budget", buildA2AVerifierPDB(agent).Spec.Selector, labels.Set(verifier.Spec.Template.Labels)},
	}
	for _, o := range others {
		if ours.Matches(o.pods) {
			t.Errorf("the kube-proxy budget also selects the %s's pods %v", o.name, o.pods)
		}
		theirs, err := metav1.LabelSelectorAsSelector(o.selector)
		if err != nil {
			t.Fatalf("%s does not parse: %v", o.name, err)
		}
		if theirs.Matches(kubeProxyPods) {
			t.Errorf("the %s %v also selects the kube-proxy pods %v", o.name, o.selector.MatchLabels, kubeProxyPods)
		}
	}
}

func TestReconcileKubeProxyRecoversFromAForeignBudgetField(t *testing.T) {
	agent := a2aTestAgent()
	live := &policyv1.PodDisruptionBudget{
		ObjectMeta: metav1.ObjectMeta{Name: kubeProxyName(agent), Namespace: agent.Namespace},
		Spec: policyv1.PodDisruptionBudgetSpec{
			MinAvailable: ptr.To(intstr.FromInt32(1)),
			Selector:     buildKubeProxyDeployment(agent, "", true, "tls-hash").Spec.Selector,
		},
	}
	pdb, _ := reconcileKubeProxyWithFake(t, agent, live)

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

func TestTheKubeProxyBudgetAndSpreadOnARealAPIServerEnvtest(t *testing.T) {
	cl, scheme := startEnvtest(t)
	ctx := context.Background()

	const ns = "kube-proxy-pdb-envtest"
	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: envtestAgentName, Namespace: ns},
		Spec: agentv1alpha1.PlatformAgentSpec{
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

	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	if err := r.reconcileKubeProxy(ctx, agent, "", true); err != nil {
		t.Fatalf("reconcileKubeProxy on envtest: %v", err)
	}

	key := types.NamespacedName{Name: kubeProxyName(agent), Namespace: agent.Namespace}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, key, dep); err != nil {
		t.Fatalf("the API server holds no kube-proxy Deployment after the render: %v", err)
	}
	if got := ptr.Deref(dep.Spec.Replicas, 0); got != 2 {
		t.Errorf("admitted kube-proxy replicas = %d, want 2", got)
	}
	if dep.Spec.Strategy.Type != appsv1.RollingUpdateDeploymentStrategyType {
		t.Errorf("admitted strategy = %q, want RollingUpdate", dep.Spec.Strategy.Type)
	}
	ru := dep.Spec.Strategy.RollingUpdate
	if ru == nil || ru.MaxUnavailable == nil || ru.MaxUnavailable.IntValue() != 1 || ru.MaxSurge == nil || ru.MaxSurge.IntValue() != 1 {
		t.Errorf("admitted RollingUpdate = %#v, want maxUnavailable 1 / maxSurge 1", ru)
	}

	pdb := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, key, pdb); err != nil {
		t.Fatalf("the API server holds no kube-proxy PodDisruptionBudget after the render: %v", err)
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
		t.Fatalf("the admitted kube-proxy pod template carries %d spread constraints, want 1", len(spreads))
	}
	if !reflect.DeepEqual(spreads[0].MatchLabelKeys, []string{"pod-template-hash"}) {
		t.Errorf("admitted spread matchLabelKeys = %v, want [pod-template-hash]", spreads[0].MatchLabelKeys)
	}
	if spreads[0].WhenUnsatisfiable != corev1.ScheduleAnyway || spreads[0].TopologyKey != "kubernetes.io/hostname" {
		t.Errorf("admitted spread = %s on %q, want ScheduleAnyway on kubernetes.io/hostname", spreads[0].WhenUnsatisfiable, spreads[0].TopologyKey)
	}

	pdb.Spec.MaxUnavailable = nil
	pdb.Spec.MinAvailable = ptr.To(intstr.FromInt32(1))
	if err := cl.Update(ctx, pdb, client.FieldOwner("kubectl-edit")); err != nil {
		t.Fatalf("hand-editing the budget to minAvailable: %v", err)
	}
	if err := r.reconcileKubeProxy(ctx, agent, "", true); err != nil {
		t.Fatalf("reconcileKubeProxy after hand edit: %v", err)
	}
	recovered := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, key, recovered); err != nil {
		t.Fatalf("get budget after the hand edit: %v", err)
	}
	if recovered.Spec.MinAvailable != nil || recovered.Spec.MaxUnavailable == nil || recovered.Spec.MaxUnavailable.IntValue() != 1 {
		t.Errorf("after the hand edit and a reconcile the budget is maxUnavailable %v / minAvailable %v, want 1 / unset",
			recovered.Spec.MaxUnavailable, recovered.Spec.MinAvailable)
	}
}
