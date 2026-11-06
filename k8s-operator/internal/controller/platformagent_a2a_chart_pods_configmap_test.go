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
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func chartPodsConfigMap(ns string, data map[string]string) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: a2aChartPodsConfigMapName, Namespace: ns},
		Data:       data,
	}
}

// A ConfigMap of the chart's name without its key was not written by the
// chart, so the default it falls back to is reported, not silent (#4883).
func TestA2AQuotaAgentReportsAChartConfigMapWithoutItsKey(t *testing.T) {
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).
		WithObjects(agent, chartPodsConfigMap(agent.Namespace, map[string]string{"other": "11"})).Build()
	r := &PlatformAgentReconciler{Client: cl, OperatorNamespace: agent.Namespace}
	sized, why, err := r.a2aQuotaAgent(context.Background(), agent)
	if err != nil {
		t.Fatal(err)
	}
	if got := a2aChartPods(sized); got != a2aDefaultChartPods {
		t.Errorf("chart pods = %d, want the default", got)
	}
	if !strings.Contains(why, "is missing") {
		t.Errorf("why = %q, want it to say the key is missing", why)
	}
}

// A failed read of the ConfigMap fails the reconcile in the operator's own
// namespace, as the other uncached reads on this path do; in any other
// namespace the count is not read, so no answer from the lookup can fail it.
func TestA2AQuotaAgentReadErrors(t *testing.T) {
	failing := interceptor.Funcs{
		Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, ok := obj.(*corev1.ConfigMap); ok && key.Name == a2aChartPodsConfigMapName {
				return errors.New("apiserver unavailable")
			}
			return cl.Get(ctx, key, obj, opts...)
		},
	}
	for _, tc := range []struct {
		operatorNS string
		wantErr    bool
	}{{"test-ns", true}, {"kubeagents-system", false}} {
		t.Run(tc.operatorNS, func(t *testing.T) {
			agent := a2aTestAgent()
			cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).WithInterceptorFuncs(failing).Build()
			r := &PlatformAgentReconciler{Client: cl, OperatorNamespace: tc.operatorNS}
			sized, why, err := r.a2aQuotaAgent(context.Background(), agent)
			if (err != nil) != tc.wantErr {
				t.Fatalf("err = %v, want an error %v", err, tc.wantErr)
			}
			if !tc.wantErr && (a2aChartPods(sized) != a2aDefaultChartPods || why != "") {
				t.Errorf("(%d, %q), want the default and no reason", a2aChartPods(sized), why)
			}
		})
	}
}

// A discarded ConfigMap value warns once, and again when it changes to
// another discarded value, as an annotation does.
func TestA2AChartConfigMapDiscardRewarnsOnAChangedValue(t *testing.T) {
	agent := a2aTestAgent()
	cm := chartPodsConfigMap(agent.Namespace, map[string]string{a2aChartPodsConfigMapKey: "70"})
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, discordBotSecret(agent), cm).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	rec := record.NewFakeRecorder(256)
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: rec, OperatorNamespace: agent.Namespace}
	ctx := context.Background()
	warnings := func() int {
		n := 0
		for {
			select {
			case e := <-rec.Events:
				if strings.Contains(e, a2aChartPodsDiscardedReason) {
					n++
				}
			default:
				return n
			}
		}
	}
	for range 2 {
		if _, err := r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
			t.Fatal(err)
		}
	}
	if got := warnings(); got != 1 {
		t.Fatalf("the same discarded value warned %d times, want once", got)
	}
	cm.Data[a2aChartPodsConfigMapKey] = "80"
	if err := cl.Update(ctx, cm); err != nil {
		t.Fatal(err)
	}
	if _, err := r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
		t.Fatal(err)
	}
	if got := warnings(); got != 1 {
		t.Fatalf("a changed discarded value warned %d times, want once more", got)
	}
}
