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

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// PreventDeletionAnnotation blocks deletion when set to "true".
const PreventDeletionAnnotation = "kubeagents.x-k8s.io/prevent-deletion"

// log is for logging in this package.
var platformagentlog = logf.Log.WithName("platformagent-resource")

// SetupPlatformAgentWebhookWithManager registers the webhook for PlatformAgent in the manager.
func SetupPlatformAgentWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr).
		For(&agentv1alpha1.PlatformAgent{}).
		WithDefaulter(&PlatformAgentCustomDefaulter{}).
		WithValidator(&PlatformAgentCustomValidator{
			Client: mgr.GetAPIReader(),
		}).
		Complete()
}

// +kubebuilder:webhook:path=/mutate-kubeagents-x-k8s-io-v1alpha1-platformagent,mutating=true,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=platformagents,verbs=create;update,versions=v1alpha1,name=mplatformagent.kb.io,admissionReviewVersions=v1

// PlatformAgentCustomDefaulter struct to implement CustomDefaulter.
type PlatformAgentCustomDefaulter struct{}

var _ admission.CustomDefaulter = &PlatformAgentCustomDefaulter{}

// Default implements admission.CustomDefaulter so a webhook will be registered for the type PlatformAgent.
func (d *PlatformAgentCustomDefaulter) Default(ctx context.Context, obj runtime.Object) error {
	platformAgent, ok := obj.(*agentv1alpha1.PlatformAgent)
	if !ok {
		return fmt.Errorf("expected a PlatformAgent object but got %T", obj)
	}
	platformagentlog.Info("defaulting PlatformAgent", "name", platformAgent.Name)

	if platformAgent.Spec.Deployment == nil {
		platformAgent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{}
	}
	if platformAgent.Spec.Deployment.Tag == nil || *platformAgent.Spec.Deployment.Tag == "" {
		platformAgent.Spec.Deployment.Tag = ptr.To("latest")
	}
	if platformAgent.Spec.Deployment.ImagePullPolicy == nil {
		platformAgent.Spec.Deployment.ImagePullPolicy = ptr.To(corev1.PullIfNotPresent)
	}
	if platformAgent.Spec.Harness != nil && platformAgent.Spec.Harness.Memory != nil && platformAgent.Spec.Harness.Memory.UserProfileEnabled == nil {
		platformAgent.Spec.Harness.Memory.UserProfileEnabled = ptr.To(false)
	}

	return nil
}

// +kubebuilder:webhook:path=/validate-kubeagents-x-k8s-io-v1alpha1-platformagent,mutating=false,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=platformagents,verbs=create;update;delete,versions=v1alpha1,name=vplatformagent.kb.io,admissionReviewVersions=v1

// PlatformAgentCustomValidator struct to implement CustomValidator.
type PlatformAgentCustomValidator struct {
	Client client.Reader
}

var _ admission.CustomValidator = &PlatformAgentCustomValidator{}

// ValidateCreate implements admission.CustomValidator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateCreate(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
	platformAgent, ok := obj.(*agentv1alpha1.PlatformAgent)
	if !ok {
		return nil, fmt.Errorf("expected a PlatformAgent object but got %T", obj)
	}
	platformagentlog.Info("validating PlatformAgent creation", "name", platformAgent.Name)

	return v.validatePlatformAgent(ctx, platformAgent)
}

// ValidateUpdate implements admission.CustomValidator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateUpdate(ctx context.Context, oldObj, newObj runtime.Object) (admission.Warnings, error) {
	platformAgent, ok := newObj.(*agentv1alpha1.PlatformAgent)
	if !ok {
		return nil, fmt.Errorf("expected a PlatformAgent object but got %T", newObj)
	}
	platformagentlog.Info("validating PlatformAgent update", "name", platformAgent.Name)

	return v.validatePlatformAgent(ctx, platformAgent)
}

func (v *PlatformAgentCustomValidator) validatePlatformAgent(ctx context.Context, platformAgent *agentv1alpha1.PlatformAgent) (admission.Warnings, error) {
	// Skip validation for terminating agents to avoid deadlocks during deletion (e.g. finalizer removal)
	if platformAgent.DeletionTimestamp != nil {
		return nil, nil
	}

	var allErrs field.ErrorList

	// 1. Enforce 1 PlatformAgent per project limit (enforced at cluster level on the Hub/Management cluster)
	if v.Client != nil {
		var list agentv1alpha1.PlatformAgentList
		if err := v.Client.List(ctx, &list); err != nil {
			return nil, err
		}
		for _, item := range list.Items {
			// Skip terminating agents to prevent deadlocking new platformagent deployment
			if item.DeletionTimestamp != nil {
				continue
			}
			if item.Name != platformAgent.Name || item.Namespace != platformAgent.Namespace {
				allErrs = append(allErrs, field.Forbidden(field.NewPath(""), "only one PlatformAgent is allowed per project"))
				break
			}
		}
	}

	// 2. Validate Deployment Security Constraints
	if platformAgent.Spec.Deployment != nil {
		depPath := field.NewPath("spec", "deployment")

		// 2a. Validate sensitive environment variable overrides
		for i, env := range platformAgent.Spec.Deployment.Env {
			if env.Name == "API_SERVER_KEY" || env.Name == "HERMES_HOME" {
				allErrs = append(allErrs, field.Forbidden(
					depPath.Child("env").Index(i).Child("name"),
					fmt.Sprintf("overriding sensitive environment variable %q is forbidden", env.Name),
				))
			}
		}

		// 2b. Validate InitContainers security context
		for i, container := range platformAgent.Spec.Deployment.InitContainers {
			allErrs = append(allErrs, validateContainerSecurity(container, depPath.Child("initContainers").Index(i))...)
		}

		// 2c. Validate Sidecars security context
		for i, container := range platformAgent.Spec.Deployment.Sidecars {
			allErrs = append(allErrs, validateContainerSecurity(container, depPath.Child("sidecars").Index(i))...)
		}

		// 2d. Validate ExtraVolumes & SidecarVolumes (hostPath forbidden)
		for i, vol := range platformAgent.Spec.Deployment.ExtraVolumes {
			if vol.HostPath != nil {
				allErrs = append(allErrs, field.Forbidden(
					depPath.Child("extraVolumes").Index(i).Child("hostPath"),
					"hostPath volumes are forbidden for security reasons",
				))
			}
		}
		for i, vol := range platformAgent.Spec.Deployment.SidecarVolumes {
			if vol.HostPath != nil {
				allErrs = append(allErrs, field.Forbidden(
					depPath.Child("sidecarVolumes").Index(i).Child("hostPath"),
					"hostPath volumes are forbidden for security reasons",
				))
			}
		}
	}

	// 3. Validate Security ServiceAccountName
	if platformAgent.Spec.Security != nil && platformAgent.Spec.Security.ServiceAccountName != "" {
		sa := platformAgent.Spec.Security.ServiceAccountName
		if sa == "cluster-admin" || sa == "system:admin" {
			allErrs = append(allErrs, field.Forbidden(
				field.NewPath("spec", "security", "serviceAccountName"),
				fmt.Sprintf("binding to privileged service account %q is forbidden", sa),
			))
		}
	}

	if len(allErrs) > 0 {
		return nil, apierrors.NewInvalid(
			schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "PlatformAgent"},
			platformAgent.Name,
			allErrs,
		)
	}

	return nil, nil
}

func validateContainerSecurity(container corev1.Container, path *field.Path) field.ErrorList {
	var errs field.ErrorList
	if container.SecurityContext == nil {
		return errs
	}

	sc := container.SecurityContext
	if sc.Privileged != nil && *sc.Privileged {
		errs = append(errs, field.Forbidden(
			path.Child("securityContext", "privileged"),
			"privileged containers are forbidden",
		))
	}
	if sc.AllowPrivilegeEscalation != nil && *sc.AllowPrivilegeEscalation {
		errs = append(errs, field.Forbidden(
			path.Child("securityContext", "allowPrivilegeEscalation"),
			"allowPrivilegeEscalation must be false",
		))
	}
	if sc.RunAsUser != nil && *sc.RunAsUser == 0 {
		errs = append(errs, field.Forbidden(
			path.Child("securityContext", "runAsUser"),
			"running containers as root (runAsUser=0) is forbidden",
		))
	}

	return errs
}

// ValidateDelete implements admission.CustomValidator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateDelete(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
	platformAgent, ok := obj.(*agentv1alpha1.PlatformAgent)
	if !ok {
		return nil, fmt.Errorf("expected a PlatformAgent object but got %T", obj)
	}
	platformagentlog.Info("validating PlatformAgent deletion", "name", platformAgent.Name)

	if platformAgent.Annotations != nil && platformAgent.Annotations[PreventDeletionAnnotation] == "true" {
		return nil, apierrors.NewForbidden(
			schema.GroupResource{Group: "kubeagents.x-k8s.io", Resource: "platformagents"},
			platformAgent.Name,
			fmt.Errorf("deletion is blocked by annotation %s=true", PreventDeletionAnnotation),
		)
	}

	return nil, nil
}
