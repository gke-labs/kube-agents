//go:build ignore

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

package webhook

import (
	"context"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/validation/field"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// log is for logging in this package.
var operatoragentlog = logf.Log.WithName("operatoragent-resource")

// SetupOperatorAgentWebhookWithManager registers the webhook for OperatorAgent in the manager.
func SetupOperatorAgentWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr).
		For(&agentv1alpha1.OperatorAgent{}).
		WithDefaulter(&OperatorAgentCustomDefaulter{}).
		WithValidator(&OperatorAgentCustomValidator{Client: mgr.GetAPIReader()}).
		Complete()
}

// TODO(user): EDIT THIS FILE!  THIS IS SCAFFOLDING FOR YOU TO OWN!

// +kubebuilder:webhook:path=/mutate-kubeagents-x-k8s-io-v1alpha1-operatoragent,mutating=true,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=operatoragents,verbs=create;update,versions=v1alpha1,name=moperatoragent.kb.io,admissionReviewVersions=v1

// OperatorAgentCustomDefaulter struct to implement CustomDefaulter.
type OperatorAgentCustomDefaulter struct {
	// TODO(user): Add fields if needed
}

var _ admission.CustomDefaulter = &OperatorAgentCustomDefaulter{}

// Default implements admission.CustomDefaulter so a webhook will be registered for the type OperatorAgent.
func (d *OperatorAgentCustomDefaulter) Default(ctx context.Context, obj runtime.Object) error {
	operatorAgent, ok := obj.(*agentv1alpha1.OperatorAgent)
	if !ok {
		return fmt.Errorf("expected a OperatorAgent object but got %T", obj)
	}
	operatoragentlog.Info("defaulting OperatorAgent", "name", operatorAgent.Name)

	// TODO(user): fill in defaulting logic here

	return nil
}

// +kubebuilder:webhook:path=/validate-kubeagents-x-k8s-io-v1alpha1-operatoragent,mutating=false,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=operatoragents,verbs=create;update;delete,versions=v1alpha1,name=voperatoragent.kb.io,admissionReviewVersions=v1

// OperatorAgentCustomValidator struct to implement CustomValidator.
type OperatorAgentCustomValidator struct {
	Client client.Reader
}

var _ admission.CustomValidator = &OperatorAgentCustomValidator{}

// ValidateCreate implements admission.CustomValidator so a webhook will be registered for the type OperatorAgent.
func (v *OperatorAgentCustomValidator) ValidateCreate(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
	operatorAgent, ok := obj.(*agentv1alpha1.OperatorAgent)
	if !ok {
		return nil, fmt.Errorf("expected an OperatorAgent object but got %T", obj)
	}
	operatoragentlog.Info("validating OperatorAgent creation", "name", operatorAgent.Name)

	return v.validateOperatorAgent(ctx, operatorAgent)
}

// ValidateUpdate implements admission.CustomValidator so a webhook will be registered for the type OperatorAgent.
func (v *OperatorAgentCustomValidator) ValidateUpdate(ctx context.Context, oldObj, newObj runtime.Object) (admission.Warnings, error) {
	operatorAgent, ok := newObj.(*agentv1alpha1.OperatorAgent)
	if !ok {
		return nil, fmt.Errorf("expected an OperatorAgent object but got %T", newObj)
	}
	operatoragentlog.Info("validating OperatorAgent update", "name", operatorAgent.Name)

	return v.validateOperatorAgent(ctx, operatorAgent)
}

// ValidateDelete implements admission.CustomValidator so a webhook will be registered for the type OperatorAgent.
func (v *OperatorAgentCustomValidator) ValidateDelete(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
	operatorAgent, ok := obj.(*agentv1alpha1.OperatorAgent)
	if !ok {
		return nil, fmt.Errorf("expected an OperatorAgent object but got %T", obj)
	}
	operatoragentlog.Info("validating OperatorAgent deletion", "name", operatorAgent.Name)

	return nil, nil
}

func (v *OperatorAgentCustomValidator) validateOperatorAgent(ctx context.Context, operatorAgent *agentv1alpha1.OperatorAgent) (admission.Warnings, error) {
	// Skip validation for terminating agents to avoid deadlocks during deletion (e.g. finalizer removal)
	if operatorAgent.DeletionTimestamp != nil {
		return nil, nil
	}

	// Enforce 1 OperatorAgent per cluster limit
	if v.Client == nil {
		return nil, fmt.Errorf("webhook validator is misconfigured: client is nil")
	}

	var list agentv1alpha1.OperatorAgentList
	if err := v.Client.List(ctx, &list); err != nil {
		return nil, err
	}
	for _, item := range list.Items {
		// Skip terminating agents to prevent deadlocking new operatoragent deployment
		if item.DeletionTimestamp != nil {
			continue
		}
		if item.Name != operatorAgent.Name || item.Namespace != operatorAgent.Namespace {
			return nil, apierrors.NewInvalid(
				schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "OperatorAgent"},
				operatorAgent.Name,
				field.ErrorList{field.Forbidden(field.NewPath(""), "only one OperatorAgent is allowed per cluster")},
			)
		}
	}
	return nil, nil
}
