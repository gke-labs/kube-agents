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
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The operator reports each tier it has read, at the generation it read, and
// writes nothing when the status already says so (#3659).
func TestATierIsReportedAccepted(t *testing.T) {
	tier := &agentv1alpha1.Tier{
		ObjectMeta: metav1.ObjectMeta{Name: "read", Namespace: "ns", Generation: 3},
		Spec:       agentv1alpha1.TierSpec{Mode: agentv1alpha1.TierModeInteractive, WriteMode: agentv1alpha1.TierWriteModePropose},
	}
	c := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(tier).
		WithStatusSubresource(&agentv1alpha1.Tier{}).Build()
	r := &TierReconciler{Client: c, Scheme: setupScheme()}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "ns", Name: "read"}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	var got agentv1alpha1.Tier
	if err := c.Get(context.Background(), req.NamespacedName, &got); err != nil {
		t.Fatal(err)
	}
	cond := meta.FindStatusCondition(got.Status.Conditions, agentv1alpha1.TierConditionAccepted)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.ObservedGeneration != 3 || got.Status.ObservedGeneration != 3 {
		t.Fatalf("status = %+v, want Accepted=True at generation 3", got.Status)
	}
	rv := got.ResourceVersion
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	if err := c.Get(context.Background(), req.NamespacedName, &got); err != nil {
		t.Fatal(err)
	}
	if got.ResourceVersion != rv {
		t.Error("a second reconcile rewrote a status that already said Accepted")
	}
	// A deleted tier is nothing to report.
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "ns", Name: "gone"}}); err != nil {
		t.Fatalf("a missing tier: %v", err)
	}
}

// A tier the components would skip (a legacy name, a built-in under another
// mode) is not Accepted, and says why: status tells what the components do.
func TestATierTheComponentsSkipIsNotAccepted(t *testing.T) {
	for name, mode := range map[string]string{"platform": agentv1alpha1.TierModeInteractive, "read": agentv1alpha1.TierModeUnattended} {
		tier := &agentv1alpha1.Tier{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "ns", Generation: 1}, Spec: agentv1alpha1.TierSpec{Mode: mode}}
		c := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(tier).WithStatusSubresource(&agentv1alpha1.Tier{}).Build()
		r := &TierReconciler{Client: c, Scheme: setupScheme()}
		req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "ns", Name: name}}
		if _, err := r.Reconcile(context.Background(), req); err != nil {
			t.Fatal(err)
		}
		var got agentv1alpha1.Tier
		if err := c.Get(context.Background(), req.NamespacedName, &got); err != nil {
			t.Fatal(err)
		}
		cond := meta.FindStatusCondition(got.Status.Conditions, agentv1alpha1.TierConditionAccepted)
		if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != tierReasonSkipped {
			t.Errorf("%s/%s: Accepted = %+v, want False/%s", name, mode, cond, tierReasonSkipped)
		}
	}
}
