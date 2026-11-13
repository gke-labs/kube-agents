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

// TierReconciler reports each Tier as read (#3659). The PlatformAgent
// controller renders the Tiers into the tiers file the verifier, gateway and
// auth callout load (platformagent_a2a_tiers.go); no AgentProfile can list one
// yet. This writes status and nothing else. The CRD's
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
		Message:            "the verifier, the gateway and the callout load this tier at its mode's verbs; no AgentProfile can list it yet",
		ObservedGeneration: tier.Generation,
	}
	// The message too, so a tier read under an earlier message is rewritten.
	if old := meta.FindStatusCondition(tier.Status.Conditions, cond.Type); tier.Status.ObservedGeneration == tier.Generation &&
		old != nil && old.Status == cond.Status && old.Reason == cond.Reason && old.Message == cond.Message {
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
