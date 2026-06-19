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

	"cloud.google.com/go/storage"
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

// SetupPlatformAgentWebhookWithManager registers the webhook for PlatformAgent in the manager.
func SetupPlatformAgentWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr).
		For(&agentv1alpha1.PlatformAgent{}).
		WithDefaulter(&PlatformAgentCustomDefaulter{}).
		WithValidator(&PlatformAgentCustomValidator{
			Client:    mgr.GetAPIReader(),
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

	// TODO(user): fill in defaulting logic here

	return nil
}

// +kubebuilder:webhook:path=/validate-kubeagents-x-k8s-io-v1alpha1-platformagent,mutating=false,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=platformagents,verbs=create;update;delete,versions=v1alpha1,name=vplatformagent.kb.io,admissionReviewVersions=v1

// PlatformAgentCustomValidator struct to implement CustomValidator.
type PlatformAgentCustomValidator struct {
	Client    client.Reader
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

	// 1. Enforce 1 PlatformAgent per project limit (enforced at cluster level on the Hub/Management cluster)
	if v.Client != nil {
		var list agentv1alpha1.PlatformAgentList
		if err := v.Client.List(ctx, &list); err != nil {
			return nil, err
		}
		if len(list.Items) > 0 {
			return nil, apierrors.NewInvalid(
				schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "PlatformAgent"},
				platformAgent.Name,
				field.ErrorList{field.Forbidden(field.NewPath(""), "only one PlatformAgent is allowed per project")},
			)
		}
	}

	// 2. Enforce 1 PlatformAgent per project globally (using GCS project-level lock)
	projectID := getProjectID(platformAgent)
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
				return nil, apierrors.NewInvalid(
					schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "PlatformAgent"},
					platformAgent.Name,
					field.ErrorList{field.Forbidden(field.NewPath(""), fmt.Sprintf("only one PlatformAgent is allowed per project; already running in GKE cluster %q", lock.ClusterName))},
				)
			}
		}
	}

	return nil, nil
}

// ValidateUpdate implements admission.CustomValidator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateUpdate(ctx context.Context, oldObj, newObj runtime.Object) (admission.Warnings, error) {
	platformAgent, ok := newObj.(*agentv1alpha1.PlatformAgent)
	if !ok {
		return nil, fmt.Errorf("expected a PlatformAgent object but got %T", newObj)
	}
	platformagentlog.Info("validating PlatformAgent update", "name", platformAgent.Name)

	// TODO(user): fill in validation logic here
	return nil, nil
}

// ValidateDelete implements admission.CustomValidator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateDelete(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
	platformAgent, ok := obj.(*agentv1alpha1.PlatformAgent)
	if !ok {
		return nil, fmt.Errorf("expected a PlatformAgent object but got %T", obj)
	}
	platformagentlog.Info("validating PlatformAgent deletion", "name", platformAgent.Name)

	// TODO(user): fill in validation logic here
	return nil, nil
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
	client, err := storage.NewClient(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed to create GCS client: %w", err)
	}
	defer client.Close()

	bucketName := fmt.Sprintf("%s-kube-agents-lock", projectID)
	rc, err := client.Bucket(bucketName).Object("platform-agent-lock.json").NewReader(ctx)
	if err != nil {
		if err == storage.ErrObjectNotExist {
			return nil, nil
		}
		return nil, fmt.Errorf("failed to read GCS lock: %w", err)
	}
	defer rc.Close()

	var lock PlatformAgentLock
	if err := json.NewDecoder(rc).Decode(&lock); err != nil {
		return nil, fmt.Errorf("failed to decode GCS lock: %w", err)
	}
	return &lock, nil
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
