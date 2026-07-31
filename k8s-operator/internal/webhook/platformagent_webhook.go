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
var platformagentlog = logf.Log.WithName("platformagent-resource")

var reservedEnvVars = map[string]bool{
	// Authentication & Sentinels
	"API_SERVER_KEY":     true,
	"API_SERVER_HOST":    true,
	"SESSION_KV_DB_PATH": true,
	// Credential Proxy Configuration
	"CREDENTIAL_PROXY_BOOTSTRAP_COMMAND": true,
	"CREDENTIAL_PROXY_POLICY":            true,
	"CREDENTIAL_PROXY_UNIX_SOCKET":       true,
	"CREDENTIAL_PROXY_PORT":              true,
	"CREDENTIAL_PROXY_WORKSPACE_ROOT":    true,
	"CREDENTIAL_PROXY_MAX_OUTPUT_BYTES":  true,
	"CREDENTIAL_PROXY_MAX_REQUEST_BYTES": true,
	"CREDENTIAL_PROXY_STATE_DIR":         true,
	"CREDENTIAL_PROXY_TIMEOUT_SECONDS":   true,
	"KSA_TOKEN_FILE":                     true,
	"TOKEN_BROKER_URL":                   true,
	// Sensitive Identity & Secrets
	"GKE_PROJECT_ID":                true,
	"GKE_CLUSTER_NAME":              true,
	"GKE_LOCATION":                  true,
	"KUBE_CONTEXT_NAME":             true,
	"SLACK_BOT_TOKEN":               true,
	"SLACK_APP_TOKEN":               true,
	"GOOGLE_CHAT_PROJECT_ID":        true,
	"GOOGLE_CHAT_SUBSCRIPTION_NAME": true,
	// Loader & Dynamic Execution Injection
	"LD_PRELOAD":      true,
	"LD_LIBRARY_PATH": true,
	"PYTHONPATH":      true,
	"PATH":            true,
	"BASH_ENV":        true,
	"ENV":             true,
}

var forbiddenCapabilities = map[string]bool{
	"CAP_SYS_ADMIN":  true,
	"CAP_NET_ADMIN":  true,
	"CAP_SYS_PTRACE": true,
	"CAP_SYS_MODULE": true,
	"CAP_SYS_BOOT":   true,
	"ALL":            true,
}

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
type PlatformAgentCustomDefaulter struct {
	// TODO(user): Add fields if needed
}

var _ admission.CustomDefaulter = &PlatformAgentCustomDefaulter{}

// Default implements admission.CustomDefaulter so a webhook will be registered for the type PlatformAgent.
func (d *PlatformAgentCustomDefaulter) Default(ctx context.Context, obj runtime.Object) error {
	platformAgent, ok := obj.(*agentv1alpha1.PlatformAgent)
	if !ok {
		return fmt.Errorf("expected a PlatformAgent object but got %T", obj)
	}
	platformagentlog.Info("defaulting PlatformAgent", "name", platformAgent.Name)

	if platformAgent.Spec.Deployment != nil {
		if platformAgent.Spec.Deployment.Availability == nil {
			platformAgent.Spec.Deployment.Availability = &agentv1alpha1.AvailabilitySpec{}
		}
		if platformAgent.Spec.Deployment.Availability.RuntimeClassName == nil {
			defaultRuntime := "gvisor"
			platformAgent.Spec.Deployment.Availability.RuntimeClassName = &defaultRuntime
		}
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
	if err := v.validateSingleAgentPerProject(ctx, platformAgent); err != nil {
		return nil, err
	}

	// 2. Enforce compute security bounds
	if platformAgent.Spec.Deployment != nil {
		allErrs = append(allErrs, validateInitContainers(platformAgent.Spec.Deployment.InitContainers, field.NewPath("spec", "deployment", "initContainers"))...)
		allErrs = append(allErrs, validateSidecars(platformAgent.Spec.Deployment.Sidecars, field.NewPath("spec", "deployment", "sidecars"))...)
		allErrs = append(allErrs, validateEnvOverrides(platformAgent.Spec.Deployment.Env, field.NewPath("spec", "deployment", "env"))...)
		allErrs = append(allErrs, validateVolumes(platformAgent.Spec.Deployment.SidecarVolumes, field.NewPath("spec", "deployment", "sidecarVolumes"))...)
		allErrs = append(allErrs, validateVolumes(platformAgent.Spec.Deployment.ExtraVolumes, field.NewPath("spec", "deployment", "extraVolumes"))...)
		if platformAgent.Spec.Deployment.Availability != nil {
			allErrs = append(allErrs, validateRuntimeClassName(platformAgent.Spec.Deployment.Availability.RuntimeClassName, field.NewPath("spec", "deployment", "availability", "runtimeClassName"))...)
		}
	}

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

func (v *PlatformAgentCustomValidator) validateSingleAgentPerProject(ctx context.Context, platformAgent *agentv1alpha1.PlatformAgent) error {
	if v.Client == nil {
		return nil
	}
	var list agentv1alpha1.PlatformAgentList
	if err := v.Client.List(ctx, &list); err != nil {
		return err
	}
	for _, item := range list.Items {
		// Skip terminating agents to prevent deadlocking new platformagent deployment
		if item.DeletionTimestamp != nil {
			continue
		}
		if item.Name != platformAgent.Name || item.Namespace != platformAgent.Namespace {
			return apierrors.NewInvalid(
				schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "PlatformAgent"},
				platformAgent.Name,
				field.ErrorList{field.Forbidden(field.NewPath(""), "only one PlatformAgent is allowed per project")},
			)
		}
	}
	return nil
}

func validateInitContainers(containers []corev1.Container, fldPath *field.Path) field.ErrorList {
	var allErrs field.ErrorList
	for i, container := range containers {
		idxPath := fldPath.Index(i)
		allErrs = append(allErrs, validateContainerSecurity(container, idxPath)...)
	}
	return allErrs
}

func validateSidecars(containers []corev1.Container, fldPath *field.Path) field.ErrorList {
	var allErrs field.ErrorList
	for i, container := range containers {
		idxPath := fldPath.Index(i)
		allErrs = append(allErrs, validateContainerSecurity(container, idxPath)...)
		for _, port := range container.Ports {
			if port.ContainerPort == 8699 || port.ContainerPort == 8888 || port.ContainerPort == 8889 {
				allErrs = append(allErrs, field.Forbidden(
					idxPath.Child("ports"),
					fmt.Sprintf("container port %d is reserved by the operator and cannot be used", port.ContainerPort),
				))
			}
		}
	}
	return allErrs
}

func validateContainerSecurity(container corev1.Container, idxPath *field.Path) field.ErrorList {
	var allErrs field.ErrorList
	if container.SecurityContext != nil {
		if container.SecurityContext.Privileged != nil && *container.SecurityContext.Privileged {
			allErrs = append(allErrs, field.Forbidden(idxPath.Child("securityContext", "privileged"), "privileged execution is forbidden"))
		}
		if container.SecurityContext.RunAsUser != nil && *container.SecurityContext.RunAsUser == 0 {
			allErrs = append(allErrs, field.Forbidden(idxPath.Child("securityContext", "runAsUser"), "root execution is forbidden (runAsUser: 0)"))
		}
		if container.SecurityContext.Capabilities != nil {
			for _, cap := range container.SecurityContext.Capabilities.Add {
				upperCap := strings.ToUpper(string(cap))
				if forbiddenCapabilities[upperCap] {
					allErrs = append(allErrs, field.Forbidden(idxPath.Child("securityContext", "capabilities", "add"), fmt.Sprintf("capability %q is forbidden", cap)))
				}
			}
		}
	}
	return allErrs
}

func validateEnvOverrides(envVars []corev1.EnvVar, fldPath *field.Path) field.ErrorList {
	var allErrs field.ErrorList
	for i, env := range envVars {
		if strings.HasPrefix(env.Name, "CREDENTIAL_PROXY_") || reservedEnvVars[env.Name] {
			allErrs = append(allErrs, field.Forbidden(fldPath.Index(i).Child("name"), fmt.Sprintf("environment variable '%s' is reserved by the operator and cannot be overridden", env.Name)))
		}
	}
	return allErrs
}

func validateVolumes(volumes []corev1.Volume, fldPath *field.Path) field.ErrorList {
	var allErrs field.ErrorList
	for i, vol := range volumes {
		idxPath := fldPath.Index(i)
		if vol.HostPath != nil {
			allErrs = append(allErrs, field.Forbidden(idxPath.Child("hostPath"), "HostPath volumes are forbidden for security reasons"))
		} else if !isAllowedVolumeSource(vol.VolumeSource) {
			allErrs = append(allErrs, field.Forbidden(idxPath, "volume source is not allowed; only ConfigMap, Secret, EmptyDir, PersistentVolumeClaim, Projected, and DownwardAPI are permitted"))
		}
	}
	return allErrs
}

func isAllowedVolumeSource(vs corev1.VolumeSource) bool {
	if vs.ConfigMap != nil || vs.Secret != nil || vs.EmptyDir != nil ||
		vs.PersistentVolumeClaim != nil || vs.Projected != nil || vs.DownwardAPI != nil {
		if vs.HostPath != nil || vs.GCEPersistentDisk != nil || vs.AWSElasticBlockStore != nil ||
			vs.NFS != nil || vs.ISCSI != nil || vs.Glusterfs != nil || vs.RBD != nil ||
			vs.FlexVolume != nil || vs.Cinder != nil || vs.CephFS != nil || vs.Flocker != nil ||
			vs.FC != nil || vs.AzureFile != nil || vs.VsphereVolume != nil || vs.Quobyte != nil ||
			vs.AzureDisk != nil || vs.PhotonPersistentDisk != nil || vs.PortworxVolume != nil ||
			vs.ScaleIO != nil || vs.StorageOS != nil || vs.CSI != nil || vs.Ephemeral != nil {
			return false
		}
		return true
	}
	return false
}

func validateRuntimeClassName(rc *string, fldPath *field.Path) field.ErrorList {
	var allErrs field.ErrorList
	if rc == nil || *rc == "" {
		return nil
	}
	allowedRuntimes := map[string]bool{
		"gvisor":      true,
		"kata":        true,
		"gce-sandbox": true,
	}
	if !allowedRuntimes[*rc] {
		allErrs = append(allErrs, field.NotSupported(fldPath, *rc, []string{"gvisor", "kata", "gce-sandbox"}))
	}
	return allErrs
}

// ValidateDelete implements admission.CustomValidator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateDelete(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
	platformAgent, ok := obj.(*agentv1alpha1.PlatformAgent)
	if !ok {
		return nil, fmt.Errorf("expected a PlatformAgent object but got %T", obj)
	}
	platformagentlog.Info("validating PlatformAgent deletion", "name", platformAgent.Name)

	// Keep ValidateDelete side-effect-free (return nil, nil) to conform with Kubernetes admission control semantics (sideEffects=None)
	// and to prevent deadlocks during finalizer removal (see line 103). Distributed lock/lease release is handled by the controller finalizer.
	return nil, nil
}
