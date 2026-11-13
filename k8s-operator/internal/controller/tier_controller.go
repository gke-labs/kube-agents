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

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// tierReasonRead is the Accepted reason on a tier the operator has read.
const tierReasonRead = "Read"

// TierReconciler reports each Tier as read (#3659). Nothing consumes a tier
// yet: the capability verifier and the gateway still use their built-in read
// and unattended tiers, so this writes status and nothing else. The CRD's
// schema already refuses an unknown mode or write mode and a name that isn't
// a subject token.
type TierReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=tiers,verbs=get;list;watch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=tiers/status,verbs=get;update;patch

func (r *TierReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var tier agentv1alpha1.Tier
	if err := r.Get(ctx, req.NamespacedName, &tier); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	cond := metav1.Condition{
		Type:               agentv1alpha1.TierConditionAccepted,
		Status:             metav1.ConditionTrue,
		Reason:             tierReasonRead,
		Message:            "the operator read this tier; no component enforces tiers from CRs yet",
		ObservedGeneration: tier.Generation,
	}
	if tier.Status.ObservedGeneration == tier.Generation && meta.IsStatusConditionPresentAndEqual(tier.Status.Conditions, cond.Type, cond.Status) {
		return ctrl.Result{}, nil
	}
	meta.SetStatusCondition(&tier.Status.Conditions, cond)
	tier.Status.ObservedGeneration = tier.Generation
	return ctrl.Result{}, r.Status().Update(ctx, &tier)
}

func (r *TierReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.Tier{}).
		Named("tier").
		Complete(r)
}
