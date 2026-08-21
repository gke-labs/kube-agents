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
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
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
// Note that this serves as an accidental-deletion guardrail rather than an authorization control,
// as a principal with update permissions can remove the annotation before deleting.
const PreventDeletionAnnotation = "kubeagents.x-k8s.io/prevent-deletion"

// DefaultPort is the port the webhook server binds to unless --webhook-port says otherwise.
//
// It is 10250 rather than controller-runtime's 9443 because GKE's automatic
// control-plane-to-node firewall rule permits only tcp:443 and tcp:10250. On a private
// cluster the API server dials the endpoint pod IP on the Service's targetPort, so a
// webhook on any other port is unreachable until someone adds a VPC firewall rule per
// cluster — and with failurePolicy=Fail an unreachable webhook blocks every PlatformAgent
// create, update, and delete. 10250 is the kubelet's port, but the kubelet binds it on the
// node IP in a separate network namespace, so a pod listening on 10250 does not collide.
//
// The manifests must agree with this value: config/manager/manager.yaml sets it as the
// container port and config/webhook/service.yaml as the Service targetPort. A mismatch
// reproduces exactly the outage described above, so TestWebhookPortsMatchDefault guards it.
const DefaultPort = 10250

// restrictedServiceAccounts is the set of high-privilege service account names forbidden in PlatformAgent spec.
var restrictedServiceAccounts = map[string]struct{}{
	"cluster-admin": {},
	"system:admin":  {},
}

// log is for logging in this package.
var platformagentlog = logf.Log.WithName("platformagent-resource")

// SetupPlatformAgentWebhookWithManager registers the webhook for PlatformAgent in the manager.
func SetupPlatformAgentWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr, &agentv1alpha1.PlatformAgent{}).
		WithDefaulter(&PlatformAgentCustomDefaulter{}).
		WithValidator(&PlatformAgentCustomValidator{
			Client: mgr.GetAPIReader(),
		}).
		Complete()
}

// +kubebuilder:webhook:path=/mutate-kubeagents-x-k8s-io-v1alpha1-platformagent,mutating=true,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=platformagents,verbs=create;update,versions=v1alpha1,name=mplatformagent.kb.io,admissionReviewVersions=v1

// PlatformAgentCustomDefaulter struct to implement admission.Defaulter.
type PlatformAgentCustomDefaulter struct{}

var _ admission.Defaulter[*agentv1alpha1.PlatformAgent] = &PlatformAgentCustomDefaulter{}

// Default implements admission.Defaulter so a webhook will be registered for the type PlatformAgent.
func (d *PlatformAgentCustomDefaulter) Default(ctx context.Context, platformAgent *agentv1alpha1.PlatformAgent) error {
	platformagentlog.Info("defaulting PlatformAgent", "name", platformAgent.Name)

	if platformAgent.Spec.Deployment != nil {
		// Tag is deliberately not defaulted: persisting "latest" would be
		// misleading when Image is omitted and the operator falls back to its
		// default platform-agent version (see resolveAgentImage).
		if platformAgent.Spec.Deployment.ImagePullPolicy == nil || *platformAgent.Spec.Deployment.ImagePullPolicy == "" {
			platformAgent.Spec.Deployment.ImagePullPolicy = ptr.To(corev1.PullIfNotPresent)
		}
	}
	if platformAgent.Spec.Harness != nil {
		if platformAgent.Spec.Harness.Memory == nil {
			platformAgent.Spec.Harness.Memory = &agentv1alpha1.MemorySpec{}
		}
		if platformAgent.Spec.Harness.Memory.UserProfileEnabled == nil {
			platformAgent.Spec.Harness.Memory.UserProfileEnabled = ptr.To(false)
		}
	}

	return nil
}

// +kubebuilder:webhook:path=/validate-kubeagents-x-k8s-io-v1alpha1-platformagent,mutating=false,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=platformagents,verbs=create;update;delete,versions=v1alpha1,name=vplatformagent.kb.io,admissionReviewVersions=v1

// PlatformAgentCustomValidator struct to implement admission.Validator.
type PlatformAgentCustomValidator struct {
	Client client.Reader
}

var _ admission.Validator[*agentv1alpha1.PlatformAgent] = &PlatformAgentCustomValidator{}

// ValidateCreate implements admission.Validator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateCreate(ctx context.Context, platformAgent *agentv1alpha1.PlatformAgent) (admission.Warnings, error) {
	platformagentlog.Info("validating PlatformAgent creation", "name", platformAgent.Name)

	return v.validatePlatformAgent(ctx, platformAgent)
}

// ValidateUpdate implements admission.Validator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateUpdate(ctx context.Context, oldObj, platformAgent *agentv1alpha1.PlatformAgent) (admission.Warnings, error) {
	platformagentlog.Info("validating PlatformAgent update", "name", platformAgent.Name)

	return v.validatePlatformAgent(ctx, platformAgent)
}

func (v *PlatformAgentCustomValidator) validatePlatformAgent(ctx context.Context, platformAgent *agentv1alpha1.PlatformAgent) (admission.Warnings, error) {
	// Skip validation for terminating agents to avoid deadlocks during deletion (e.g. finalizer removal)
	if platformAgent.DeletionTimestamp != nil {
		return nil, nil
	}

	var allErrs field.ErrorList

	// 1. Enforce 1 PlatformAgent per cluster limit (enforced at cluster level on the Hub/Management cluster)
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
				allErrs = append(allErrs, field.Forbidden(field.NewPath(""), "only one PlatformAgent is allowed per cluster"))
				break
			}
		}
	}

	// 2. Validate Deployment Security Constraints
	if platformAgent.Spec.Deployment != nil {
		depPath := field.NewPath("spec", "deployment")

		// 2a. Validate sensitive environment variable overrides
		for i, env := range platformAgent.Spec.Deployment.Env {
			if _, isSensitive := agentv1alpha1.SensitiveEnvVars[env.Name]; isSensitive {
				allErrs = append(allErrs, field.Forbidden(
					depPath.Child("env").Index(i).Child("name"),
					fmt.Sprintf("overriding sensitive environment variable %q is forbidden", env.Name),
				))
			}
		}

		// 2b. Validate InitContainers security context
		for i := range platformAgent.Spec.Deployment.InitContainers {
			allErrs = append(allErrs, validateContainerSecurity(platformAgent.Spec.Deployment.InitContainers[i].SecurityContext, depPath.Child("initContainers").Index(i))...)
		}

		// 2c. Validate Sidecars security context
		for i := range platformAgent.Spec.Deployment.Sidecars {
			allErrs = append(allErrs, validateContainerSecurity(platformAgent.Spec.Deployment.Sidecars[i].SecurityContext, depPath.Child("sidecars").Index(i))...)
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

		// 2e. Validate ImagePullSecrets name a Secret, each of them exactly once.
		// Neither shape is caught anywhere below: corev1.LocalObjectReference
		// makes Name optional, so the CRD schema admits `- {}` and `- name: ""`,
		// and core PodSpec validation does not reliably reject them either — on
		// GKE 1.35.6 an empty name is a warning rather than an error. The kubelet
		// then looks for a Secret named "", fails, and pulls anonymously, so the
		// agent lands in ImagePullBackOff against a CR that looks like it
		// configured a pull identity. A repeat fails further away still:
		// PodSpec.imagePullSecrets is a server-side-apply list-map keyed on name,
		// so two identical entries make every apply of the generated Deployment
		// fail with `duplicate entries for key` — a reconcile error on an object
		// the author never wrote. The controller normalizes both away for installs
		// running without this webhook (resolveImagePullSecrets, which the chart's
		// default leaves as the only line of defence); rejecting here puts the
		// error on the object that has the typo.
		seenPullSecrets := make(map[string]struct{}, len(platformAgent.Spec.Deployment.ImagePullSecrets))
		for i, ref := range platformAgent.Spec.Deployment.ImagePullSecrets {
			namePath := depPath.Child("imagePullSecrets").Index(i).Child("name")
			name := strings.TrimSpace(ref.Name)
			if name == "" {
				allErrs = append(allErrs, field.Required(
					namePath,
					"an imagePullSecrets entry must name a Secret in the agent's namespace",
				))
				continue
			}
			if _, dup := seenPullSecrets[name]; dup {
				allErrs = append(allErrs, field.Duplicate(namePath, name))
				continue
			}
			seenPullSecrets[name] = struct{}{}
		}
	}

	// 3. Validate Security ServiceAccountName
	// Note: This check serves as a name-based tripwire against obvious misconfigurations
	// (e.g., binding to literal names like "cluster-admin" or "system:admin"). It is NOT
	// full security enforcement against privileged ServiceAccounts. Genuine RBAC enforcement
	// requires inspecting RoleBinding / ClusterRoleBinding resources, which is controller
	// and admission-policy territory to avoid time-of-check to time-of-use (TOCTOU) issues
	// at webhook admission time.
	if platformAgent.Spec.Security != nil && platformAgent.Spec.Security.ServiceAccountName != "" {
		sa := platformAgent.Spec.Security.ServiceAccountName
		if _, isRestricted := restrictedServiceAccounts[sa]; isRestricted {
			allErrs = append(allErrs, field.Forbidden(
				field.NewPath("spec", "security", "serviceAccountName"),
				fmt.Sprintf("binding to privileged service account %q is forbidden", sa),
			))
		}
	}

	// 4. Validate GitHub Repository URL
	if platformAgent.Spec.Integration != nil && platformAgent.Spec.Integration.GitHub != nil {
		if err := agentv1alpha1.ValidateGitRepoURL(platformAgent.Spec.Integration.GitHub.GitRepo); err != nil {
			allErrs = append(allErrs, field.Invalid(
				field.NewPath("spec", "integration", "github", "gitRepo"),
				platformAgent.Spec.Integration.GitHub.GitRepo,
				err.Error(),
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

func validateContainerSecurity(sc *corev1.SecurityContext, path *field.Path) field.ErrorList {
	var errs field.ErrorList
	if sc == nil {
		return errs
	}
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
	if sc.Capabilities != nil && len(sc.Capabilities.Add) > 0 {
		errs = append(errs, field.Forbidden(
			path.Child("securityContext", "capabilities", "add"),
			"adding capabilities is forbidden",
		))
	}

	return errs
}

// ValidateDelete implements admission.Validator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateDelete(ctx context.Context, platformAgent *agentv1alpha1.PlatformAgent) (admission.Warnings, error) {
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
