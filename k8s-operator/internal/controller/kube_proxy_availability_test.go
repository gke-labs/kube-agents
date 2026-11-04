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
	"reflect"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	policyv1 "k8s.io/api/policy/v1"
	"k8s.io/apimachinery/pkg/labels"
)

// Every sandbox kubectl goes through the kube-proxy, so a rollout or a drain
// must leave one replica serving, and a rollout must not need quota beyond the
// running count.
func TestKubeProxyRollsOneReplicaAtATimeWithoutSurge(t *testing.T) {
	dep := buildKubeProxyDeployment(shellSandboxTestAgent(), "", true, "tls-hash")

	if got := *dep.Spec.Replicas; got != 2 {
		t.Fatalf("replicas = %d, want 2", got)
	}
	if dep.Spec.Strategy.Type != appsv1.RollingUpdateDeploymentStrategyType || dep.Spec.Strategy.RollingUpdate == nil {
		t.Fatalf("strategy = %#v, want RollingUpdate", dep.Spec.Strategy)
	}
	ru := dep.Spec.Strategy.RollingUpdate
	if ru.MaxSurge == nil || ru.MaxSurge.IntValue() != 0 {
		t.Errorf("maxSurge = %v, want 0: a surge pod pends in a namespace at its ResourceQuota", ru.MaxSurge)
	}
	if ru.MaxUnavailable == nil || ru.MaxUnavailable.IntValue() != 1 {
		t.Errorf("maxUnavailable = %v, want 1: the other replica serves while one restarts", ru.MaxUnavailable)
	}

	c := dep.Spec.Template.Spec.Containers[0]
	if c.Lifecycle == nil || c.Lifecycle.PreStop == nil || c.Lifecycle.PreStop.Sleep == nil {
		t.Fatalf("preStop = %#v, want a sleep: a replica must outlive its endpoint removal", c.Lifecycle)
	}
	grace := int64(corev1.DefaultTerminationGracePeriodSeconds)
	if g := dep.Spec.Template.Spec.TerminationGracePeriodSeconds; g != nil {
		grace = *g
	}
	if got := c.Lifecycle.PreStop.Sleep.Seconds; got <= 0 || got >= grace {
		t.Errorf("preStop sleep = %ds, want within the %ds grace period", got, grace)
	}

	spread := dep.Spec.Template.Spec.TopologySpreadConstraints
	if len(spread) != 1 || spread[0].WhenUnsatisfiable != corev1.ScheduleAnyway {
		t.Fatalf("topologySpreadConstraints = %#v, want one ScheduleAnyway constraint", spread)
	}
	sel, err := labels.ValidatedSelectorFromSet(spread[0].LabelSelector.MatchLabels)
	if err != nil {
		t.Fatal(err)
	}
	if !sel.Matches(labels.Set(dep.Spec.Template.Labels)) {
		t.Errorf("spread selector %v does not match the pod labels %v", sel, dep.Spec.Template.Labels)
	}
}

func TestKubeProxyPDBSelectsTheDeploymentsPods(t *testing.T) {
	agent := shellSandboxTestAgent()
	dep := buildKubeProxyDeployment(agent, "", true, "tls-hash")
	pdb := buildKubeProxyPDB(agent)

	if pdb.Name != dep.Name || pdb.Namespace != dep.Namespace {
		t.Errorf("PDB %s/%s, want the Deployment's %s/%s", pdb.Namespace, pdb.Name, dep.Namespace, dep.Name)
	}
	if !reflect.DeepEqual(pdb.Spec.Selector, dep.Spec.Selector) {
		t.Errorf("PDB selector = %v, want the Deployment's %v", pdb.Spec.Selector, dep.Spec.Selector)
	}
	if pdb.Spec.MinAvailable != nil {
		t.Errorf("minAvailable = %v, want unset", pdb.Spec.MinAvailable)
	}
	if pdb.Spec.MaxUnavailable == nil || pdb.Spec.MaxUnavailable.IntValue() != 1 {
		t.Errorf("maxUnavailable = %v, want 1", pdb.Spec.MaxUnavailable)
	}
	if pdb.Spec.UnhealthyPodEvictionPolicy == nil || *pdb.Spec.UnhealthyPodEvictionPolicy != policyv1.AlwaysAllow {
		t.Errorf("unhealthyPodEvictionPolicy = %v, want AlwaysAllow", pdb.Spec.UnhealthyPodEvictionPolicy)
	}
}
