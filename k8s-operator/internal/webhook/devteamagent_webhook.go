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

	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// log is for logging in this package.
var devteamagentlog = logf.Log.WithName("devteamagent-resource")

// SetupDevTeamAgentWebhookWithManager registers the webhook for DevTeamAgent in the manager.
func SetupDevTeamAgentWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr).
		For(&agentv1alpha1.DevTeamAgent{}).
		WithDefaulter(&DevTeamAgentCustomDefaulter{}).
		WithValidator(&DevTeamAgentCustomValidator{}).
		Complete()
}

// TODO(user): EDIT THIS FILE!  THIS IS SCAFFOLDING FOR YOU TO OWN!

// +kubebuilder:webhook:path=/mutate-kubeagents-x-k8s-io-v1alpha1-devteamagent,mutating=true,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=devteamagents,verbs=create;update,versions=v1alpha1,name=mdevteamagent.kb.io,admissionReviewVersions=v1

// DevTeamAgentCustomDefaulter struct to implement CustomDefaulter.
type DevTeamAgentCustomDefaulter struct {
	// TODO(user): Add fields if needed
}

var _ admission.CustomDefaulter = &DevTeamAgentCustomDefaulter{}

// Default implements admission.CustomDefaulter so a webhook will be registered for the type DevTeamAgent.
func (d *DevTeamAgentCustomDefaulter) Default(ctx context.Context, obj runtime.Object) error {
	devTeamAgent, ok := obj.(*agentv1alpha1.DevTeamAgent)
	if !ok {
		return fmt.Errorf("expected a DevTeamAgent object but got %T", obj)
	}
	devteamagentlog.Info("defaulting DevTeamAgent", "name", devTeamAgent.Name)

	// TODO(user): fill in defaulting logic here

	return nil
}

// +kubebuilder:webhook:path=/validate-kubeagents-x-k8s-io-v1alpha1-devteamagent,mutating=false,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=devteamagents,verbs=create;update;delete,versions=v1alpha1,name=vdevteamagent.kb.io,admissionReviewVersions=v1

// DevTeamAgentCustomValidator struct to implement CustomValidator.
type DevTeamAgentCustomValidator struct {
	// TODO(user): Add fields if needed
}

var _ admission.CustomValidator = &DevTeamAgentCustomValidator{}

// ValidateCreate implements admission.CustomValidator so a webhook will be registered for the type DevTeamAgent.
func (v *DevTeamAgentCustomValidator) ValidateCreate(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
	devTeamAgent, ok := obj.(*agentv1alpha1.DevTeamAgent)
	if !ok {
		return nil, fmt.Errorf("expected a DevTeamAgent object but got %T", obj)
	}
	devteamagentlog.Info("validating DevTeamAgent creation", "name", devTeamAgent.Name)

	// TODO(user): fill in validation logic here
	return nil, nil
}

// ValidateUpdate implements admission.CustomValidator so a webhook will be registered for the type DevTeamAgent.
func (v *DevTeamAgentCustomValidator) ValidateUpdate(ctx context.Context, oldObj, newObj runtime.Object) (admission.Warnings, error) {
	devTeamAgent, ok := newObj.(*agentv1alpha1.DevTeamAgent)
	if !ok {
		return nil, fmt.Errorf("expected a DevTeamAgent object but got %T", newObj)
	}
	devteamagentlog.Info("validating DevTeamAgent update", "name", devTeamAgent.Name)

	// TODO(user): fill in validation logic here
	return nil, nil
}

// ValidateDelete implements admission.CustomValidator so a webhook will be registered for the type DevTeamAgent.
func (v *DevTeamAgentCustomValidator) ValidateDelete(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
	devTeamAgent, ok := obj.(*agentv1alpha1.DevTeamAgent)
	if !ok {
		return nil, fmt.Errorf("expected a DevTeamAgent object but got %T", obj)
	}
	devteamagentlog.Info("validating DevTeamAgent deletion", "name", devTeamAgent.Name)

	// TODO(user): fill in validation logic here
	return nil, nil
}
