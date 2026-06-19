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
	"encoding/json"
	"fmt"
	"net/http"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
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

// SetupPlatformAgentWebhookWithManager registers the webhook for PlatformAgent in the manager.
func SetupPlatformAgentWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr).
		For(&agentv1alpha1.PlatformAgent{}).
		WithDefaulter(&PlatformAgentCustomDefaulter{}).
		WithValidator(&PlatformAgentCustomValidator{
			Client:    mgr.GetClient(),
			GCSClient: &RealGCSClient{},
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

	// Default Harness settings
	if platformAgent.Spec.Harness == nil {
		platformAgent.Spec.Harness = &agentv1alpha1.PlatformAgentHarnessSpec{}
	}
	if platformAgent.Spec.Harness.Hermes == nil {
		platformAgent.Spec.Harness.Hermes = &agentv1alpha1.HermesSpec{}
	}
	if platformAgent.Spec.Harness.Hermes.DashboardEnabled == nil {
		dashboardEnabled := true
		platformAgent.Spec.Harness.Hermes.DashboardEnabled = &dashboardEnabled
	}
	if platformAgent.Spec.Harness.Hermes.PluginsDebug == nil {
		pluginsDebug := false
		platformAgent.Spec.Harness.Hermes.PluginsDebug = &pluginsDebug
	}
	if platformAgent.Spec.Harness.Hermes.AgentHome == "" {
		platformAgent.Spec.Harness.Hermes.AgentHome = "/opt/data"
	}

	// Default Deployment settings
	if platformAgent.Spec.Deployment != nil {
		if platformAgent.Spec.Deployment.Tag == nil || *platformAgent.Spec.Deployment.Tag == "" {
			tag := "latest"
			platformAgent.Spec.Deployment.Tag = &tag
		}
		if platformAgent.Spec.Deployment.ImagePullPolicy == nil || *platformAgent.Spec.Deployment.ImagePullPolicy == "" {
			pullPolicy := corev1.PullIfNotPresent
			platformAgent.Spec.Deployment.ImagePullPolicy = &pullPolicy
		}
	}

	// Default Integration settings
	if platformAgent.Spec.Integration != nil && platformAgent.Spec.Integration.GoogleChat != nil {
		if platformAgent.Spec.Integration.GoogleChat.Enabled == nil {
			enabled := false
			platformAgent.Spec.Integration.GoogleChat.Enabled = &enabled
		}
	}

	return nil
}

// +kubebuilder:webhook:path=/validate-kubeagents-x-k8s-io-v1alpha1-platformagent,mutating=false,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=platformagents,verbs=create;update;delete,versions=v1alpha1,name=vplatformagent.kb.io,admissionReviewVersions=v1

// PlatformAgentCustomValidator struct to implement CustomValidator.
type PlatformAgentCustomValidator struct {
	Client    client.Client
	GCSClient GCSClient
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

// ValidateDelete implements admission.CustomValidator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateDelete(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
	platformAgent, ok := obj.(*agentv1alpha1.PlatformAgent)
	if !ok {
		return nil, fmt.Errorf("expected a PlatformAgent object but got %T", obj)
	}
	platformagentlog.Info("validating PlatformAgent deletion", "name", platformAgent.Name)

	return nil, nil
}

func (v *PlatformAgentCustomValidator) validatePlatformAgent(ctx context.Context, platformAgent *agentv1alpha1.PlatformAgent) (admission.Warnings, error) {
	// Enforce 1 PlatformAgent per project limit (enforced at cluster level on the Hub/Management cluster)
	if v.Client != nil {
		var list agentv1alpha1.PlatformAgentList
		if err := v.Client.List(ctx, &list); err != nil {
			return nil, err
		}
		for _, item := range list.Items {
			if item.Name != platformAgent.Name || item.Namespace != platformAgent.Namespace {
				return nil, errors.NewInvalid(
					schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "PlatformAgent"},
					platformAgent.Name,
					field.ErrorList{field.Forbidden(field.NewPath(""), "only one PlatformAgent is allowed per project")},
				)
			}
		}
	}

	// Enforce 1 PlatformAgent per project globally (using GCS project-level lock)
	projectID := getProjectID(platformAgent)
	platformagentlog.Info("debug GCS project ID and client", "projectID", projectID, "gcsClientNil", v.GCSClient == nil)
	if projectID != "" && v.GCSClient != nil {
		lock, err := v.GCSClient.GetLock(ctx, projectID)
		if err != nil {
			platformagentlog.Error(err, "failed to check global GCS lock, bypassing cross-cluster validation")
		} else if lock != nil {
			currentCluster := ""
			if platformAgent.Spec.Harness != nil {
				currentCluster = platformAgent.Spec.Harness.ClusterName
			}
			if lock.ClusterName != currentCluster {
				return nil, errors.NewInvalid(
					schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "PlatformAgent"},
					platformAgent.Name,
					field.ErrorList{field.Forbidden(field.NewPath(""), fmt.Sprintf("only one PlatformAgent is allowed per project; already running in GKE cluster %q", lock.ClusterName))},
				)
			}
		}
	}

	var allErrs field.ErrorList
	specPath := field.NewPath("spec")

	// Validate Deployment spec if present
	if platformAgent.Spec.Deployment != nil {
		deployPath := specPath.Child("deployment")
		if platformAgent.Spec.Deployment.Image == "" {
			allErrs = append(allErrs, field.Required(deployPath.Child("image"), "image must be specified"))
		}
		if platformAgent.Spec.Deployment.ImagePullPolicy != nil {
			policy := *platformAgent.Spec.Deployment.ImagePullPolicy
			if policy != corev1.PullAlways && policy != corev1.PullNever && policy != corev1.PullIfNotPresent {
				allErrs = append(allErrs, field.NotSupported(deployPath.Child("imagePullPolicy"), policy, []string{
					string(corev1.PullAlways), string(corev1.PullNever), string(corev1.PullIfNotPresent),
				}))
			}
		}
	}

	// Validate Security spec if present
	if platformAgent.Spec.Security != nil {
		securityPath := specPath.Child("security")
		if platformAgent.Spec.Security.WorkloadIdentity != nil && platformAgent.Spec.Security.WorkloadIdentity.Gcp != nil {
			gcp := platformAgent.Spec.Security.WorkloadIdentity.Gcp
			gcpPath := securityPath.Child("workloadIdentity").Child("gcp")
			if gcp.GSAName == "" {
				allErrs = append(allErrs, field.Required(gcpPath.Child("gsaName"), "gsaName must be specified when GCP Workload Identity is configured"))
			}
			if gcp.ProjectID == "" {
				allErrs = append(allErrs, field.Required(gcpPath.Child("projectId"), "projectId must be specified when GCP Workload Identity is configured"))
			}
		}
	}

	// Validate Google Chat integration settings
	if platformAgent.Spec.Integration != nil && platformAgent.Spec.Integration.GoogleChat != nil {
		gchat := platformAgent.Spec.Integration.GoogleChat
		gchatPath := specPath.Child("integration").Child("googleChat")
		if gchat.Enabled != nil && *gchat.Enabled {
			if gchat.ProjectID == "" {
				allErrs = append(allErrs, field.Required(gchatPath.Child("projectId"), "projectId must be specified when Google Chat integration is enabled"))
			}
			if gchat.TopicName == "" {
				allErrs = append(allErrs, field.Required(gchatPath.Child("topicName"), "topicName must be specified when Google Chat integration is enabled"))
			}
			if gchat.SubscriptionName == "" {
				allErrs = append(allErrs, field.Required(gchatPath.Child("subscriptionName"), "subscriptionName must be specified when Google Chat integration is enabled"))
			}
		}
	}

	// Validate Harness secrets reference if specified
	if platformAgent.Spec.Harness != nil && platformAgent.Spec.Harness.Hermes != nil && platformAgent.Spec.Harness.Hermes.ApiServerSecretRef != nil {
		ref := platformAgent.Spec.Harness.Hermes.ApiServerSecretRef
		refPath := specPath.Child("harness").Child("hermes").Child("apiServerSecretRef")
		if ref.Name == "" {
			allErrs = append(allErrs, field.Required(refPath.Child("name"), "secret name must be specified"))
		}
		if ref.Key == "" {
			allErrs = append(allErrs, field.Required(refPath.Child("key"), "secret key must be specified"))
		}
	}

	if len(allErrs) == 0 {
		return nil, nil
	}

	return nil, errors.NewInvalid(
		schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "PlatformAgent"},
		platformAgent.Name,
		allErrs,
	)
}

// ─── GCS Lock Client Implementation ──────────────────────────────────────────

type PlatformAgentLock struct {
	ClusterName string `json:"clusterName"`
	AgentName   string `json:"agentName"`
	Namespace   string `json:"namespace"`
}

type GCSClient interface {
	GetLock(ctx context.Context, projectID string) (*PlatformAgentLock, error)
}

type RealGCSClient struct{}

func (c *RealGCSClient) GetLock(ctx context.Context, projectID string) (*PlatformAgentLock, error) {
	// 1. Fetch metadata access token
	token, err := fetchMetadataToken(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed to fetch metadata token: %w", err)
	}

	// 2. Query GCS JSON API
	url := fmt.Sprintf("https://storage.googleapis.com/storage/v1/b/%s-kube-agents-lock/o/platform-agent-lock.json?alt=media", projectID)
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, nil // Lock does not exist
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GCS API returned status %d", resp.StatusCode)
	}

	var lock PlatformAgentLock
	if err := json.NewDecoder(resp.Body).Decode(&lock); err != nil {
		return nil, err
	}
	return &lock, nil
}

func fetchMetadataToken(ctx context.Context) (string, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token", nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Metadata-Flavor", "Google")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("metadata server returned status %d", resp.StatusCode)
	}

	var res struct {
		AccessToken string `json:"access_token"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&res); err != nil {
		return "", err
	}
	return res.AccessToken, nil
}

func getProjectID(agent *agentv1alpha1.PlatformAgent) string {
	if agent.Spec.Security != nil && agent.Spec.Security.WorkloadIdentity != nil && agent.Spec.Security.WorkloadIdentity.Gcp != nil {
		return agent.Spec.Security.WorkloadIdentity.Gcp.ProjectID
	}
	if agent.Spec.Integration != nil && agent.Spec.Integration.GoogleChat != nil {
		return agent.Spec.Integration.GoogleChat.ProjectID
	}
	return ""
}
