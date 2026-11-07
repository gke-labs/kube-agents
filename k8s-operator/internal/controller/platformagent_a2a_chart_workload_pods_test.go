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
	"math"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
)

const chartWorkloadTestOperatorNS = "kubeagents-system"

func chartLabels() map[string]string {
	return map[string]string{labelManagedBy: "Helm", labelPartOf: partOfKubeAgents, "helm.sh/chart": "kube-agents-0.1.0"}
}

func chartDeployment(ns, name string, replicas int32, maxSurge *intstr.IntOrString) *appsv1.Deployment {
	d := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns, Labels: chartLabels()},
		Spec:       appsv1.DeploymentSpec{Replicas: ptr.To(replicas)},
	}
	if maxSurge != nil {
		d.Spec.Strategy = appsv1.DeploymentStrategy{
			Type:          appsv1.RollingUpdateDeploymentStrategyType,
			RollingUpdate: &appsv1.RollingUpdateDeployment{MaxSurge: maxSurge},
		}
	}
	return d
}

func chartStatefulSet(ns, name string, replicas int32) *appsv1.StatefulSet {
	return &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns, Labels: chartLabels()},
		Spec:       appsv1.StatefulSetSpec{Replicas: ptr.To(replicas)},
	}
}

func chartWorkloadReconciler(objs ...client.Object) *PlatformAgentReconciler {
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(objs...).Build()
	return &PlatformAgentReconciler{Client: cl, OperatorNamespace: chartWorkloadTestOperatorNS}
}

// A PlatformAgent outside the operator's namespace with no annotation is
// sized from the chart's workloads there (#5006): their replicas and each
// Deployment's surge pods. The operator's own workloads, unlabelled ones and
// a chart ConfigMap there are not counted.
func TestA2AQuotaAgentCountsTheChartWorkloadsOutsideTheOperatorNamespace(t *testing.T) {
	agent := a2aTestAgent()
	ns := agent.Namespace
	one := intstr.FromInt32(1)
	own := chartDeployment(ns, "test-agent-a2a-gateway", 3, nil)
	own.Labels = commonLabels(agent)
	plain := chartDeployment(ns, "unrelated", 4, nil)
	plain.Labels = map[string]string{"app": "unrelated"}
	unset := chartDeployment(ns, "minter", 0, &one)
	unset.Spec.Replicas = nil // the API server's default of 1
	r := chartWorkloadReconciler(agent,
		unset,                                   // 1 + 1
		chartDeployment(ns, "litellm", 2, &one), // 2 + 1
		chartDeployment(ns, "hindsight-api", 1, nil), // 1 + 25% of 1, rounded up
		chartStatefulSet(ns, "hindsight", 1),         // 1, no surge
		own, plain,
		chartPodsConfigMap(ns, map[string]string{a2aChartPodsConfigMapKey: "40"}),
	)
	sized, why, err := r.a2aQuotaAgent(context.Background(), agent)
	if err != nil {
		t.Fatal(err)
	}
	if why != "" {
		t.Errorf("why = %q, want the count used", why)
	}
	if got := a2aChartPods(sized); got != 8 {
		t.Errorf("chart pods = %d, want 8 (minter 1+1, litellm 2+1, hindsight-api 1+1, hindsight 1)", got)
	}
	if _, ok := agent.GetAnnotations()[a2aChartPodsAnnotation]; ok {
		t.Error("the CR itself was annotated; only the sized copy may be")
	}
}

// Each Deployment's surge pods follow kube-agents.surgePods for the values
// the API server admits (#4876).
func TestA2ADeploymentSurgePods(t *testing.T) {
	for _, tc := range []struct {
		name     string
		replicas int32
		surge    *intstr.IntOrString
		recreate bool
		want     int64
	}{
		{"an integer", 4, ptr.To(intstr.FromInt32(2)), false, 2},
		{"a percentage rounded up", 3, ptr.To(intstr.FromString("50%")), false, 2},
		{"a percentage just past a whole pod", 3, ptr.To(intstr.FromString("34%")), false, 2},
		{"past the replicas", 3, ptr.To(intstr.FromInt32(100)), false, 3},
		{"zero", 2, ptr.To(intstr.FromInt32(0)), false, 0},
		{"unset, Kubernetes' 25%", 4, nil, false, 1},
		{"Recreate", 4, ptr.To(intstr.FromInt32(2)), true, 0},
		{"zero replicas", 0, ptr.To(intstr.FromInt32(2)), false, 0},
		{"malformed, bounded by the replicas", 3, ptr.To(intstr.FromString("abc")), false, 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := chartDeployment("ns", "d", tc.replicas, tc.surge)
			if tc.recreate {
				d.Spec.Strategy = appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType}
			}
			if got := a2aDeploymentSurgePods(d); got != tc.want {
				t.Errorf("surge pods = %d, want %d", got, tc.want)
			}
		})
	}
}

// With no chart workloads in its namespace, the CR gets the default, as
// before #5006.
func TestA2AQuotaAgentWithoutChartWorkloadsCountsTheDefault(t *testing.T) {
	agent := a2aTestAgent()
	r := chartWorkloadReconciler(agent)
	sized, why, err := r.a2aQuotaAgent(context.Background(), agent)
	if err != nil {
		t.Fatal(err)
	}
	if got := a2aChartPods(sized); got != a2aDefaultChartPods || why != "" {
		t.Errorf("(%d, %q), want the default and no reason", got, why)
	}
}

// The annotation still wins over the chart's workloads.
func TestA2AQuotaAgentAnnotationWinsOverTheChartWorkloads(t *testing.T) {
	agent := a2aTestAgent()
	agent.Annotations = map[string]string{a2aChartPodsAnnotation: "9"}
	r := chartWorkloadReconciler(agent, chartDeployment(agent.Namespace, "litellm", 2, nil))
	sized, _, err := r.a2aQuotaAgent(context.Background(), agent)
	if err != nil {
		t.Fatal(err)
	}
	if got := a2aChartPods(sized); got != 9 {
		t.Errorf("chart pods = %d, want the annotation's 9", got)
	}
}

// In the operator's own namespace the chart's ConfigMap gives the count, as
// #4883 made it; the workloads there are not counted instead.
func TestA2AQuotaAgentInTheOperatorNamespaceReadsTheConfigMap(t *testing.T) {
	agent := a2aTestAgent()
	agent.Namespace = chartWorkloadTestOperatorNS
	r := chartWorkloadReconciler(agent,
		chartDeployment(agent.Namespace, "litellm", 2, nil),
		chartPodsConfigMap(agent.Namespace, map[string]string{a2aChartPodsConfigMapKey: "11"}),
	)
	sized, _, err := r.a2aQuotaAgent(context.Background(), agent)
	if err != nil {
		t.Fatal(err)
	}
	if got := a2aChartPods(sized); got != 11 {
		t.Errorf("chart pods = %d, want the ConfigMap's 11", got)
	}
}

// A count past the cap is discarded with a Warning naming the workloads,
// through the annotation's parse.
func TestA2AQuotaAgentDiscardsAWorkloadCountPastTheCap(t *testing.T) {
	agent := a2aTestAgent()
	r := chartWorkloadReconciler(agent, chartDeployment(agent.Namespace, "litellm", 70, ptr.To(intstr.FromInt32(0))))
	sized, why, err := r.a2aQuotaAgent(context.Background(), agent)
	if err != nil {
		t.Fatal(err)
	}
	if got := a2aChartPods(sized); got != a2aDefaultChartPods {
		t.Errorf("chart pods = %d, want the default", got)
	}
	if !strings.Contains(why, "Deployments and StatefulSets in namespace test-ns") || !strings.Contains(why, "outside 0-64") {
		t.Errorf("why = %q, want it to name the workloads and the cap", why)
	}
}

// A failed List can't fail the reconcile outside the operator's namespace:
// the quota counts the default and the reason says why.
func TestA2AQuotaAgentListErrorCountsTheDefault(t *testing.T) {
	for _, kind := range []string{"Deployment", "StatefulSet"} {
		t.Run(kind, func(t *testing.T) {
			agent := a2aTestAgent()
			cl := fake.NewClientBuilder().WithScheme(setupScheme()).
				WithObjects(agent, chartDeployment(agent.Namespace, "litellm", 2, nil)).
				WithInterceptorFuncs(interceptor.Funcs{
					List: func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
						_, dep := list.(*appsv1.DeploymentList)
						_, sts := list.(*appsv1.StatefulSetList)
						if (kind == "Deployment" && dep) || (kind == "StatefulSet" && sts) {
							return errors.New("apiserver unavailable")
						}
						return c.List(ctx, list, opts...)
					},
				}).Build()
			r := &PlatformAgentReconciler{Client: cl, OperatorNamespace: chartWorkloadTestOperatorNS}
			sized, why, err := r.a2aQuotaAgent(context.Background(), agent)
			if err != nil {
				t.Fatalf("a failed List failed the reconcile: %v", err)
			}
			if got := a2aChartPods(sized); got != a2aDefaultChartPods {
				t.Errorf("chart pods = %d, want the default", got)
			}
			if !strings.Contains(why, "could not be listed") || !strings.Contains(why, "apiserver unavailable") {
				t.Errorf("why = %q, want it to say the List failed and why", why)
			}
		})
	}
}

func TestA2ASatAddStopsAtMaxInt64(t *testing.T) {
	if got := a2aSatAdd(math.MaxInt64-1, 5); got != math.MaxInt64 {
		t.Errorf("a2aSatAdd = %d, want MaxInt64", got)
	}
	if got := a2aSatAdd(2, 3); got != 5 {
		t.Errorf("a2aSatAdd(2, 3) = %d", got)
	}
}
