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

package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
)

type HermesSpec struct {
	// DashboardEnabled toggles the AGENT_DASHBOARD environment variable.
	// +kubebuilder:default=true
	// +optional
	DashboardEnabled *bool `json:"dashboardEnabled,omitempty"`

	// PluginsDebug toggles the AGENT_PLUGINS_DEBUG environment variable.
	// +kubebuilder:default=false
	// +optional
	PluginsDebug *bool `json:"pluginsDebug,omitempty"`

	// AgentHome is the path to the AGENT_HOME directory.
	// +kubebuilder:default="/opt/data"
	// +optional
	AgentHome string `json:"agentHome,omitempty"`

	// ApiServerSecretRef securely references a Secret containing the API_SERVER_KEY.
	// +optional
	ApiServerSecretRef *corev1.SecretKeySelector `json:"apiServerSecretRef,omitempty"`
}

// DeploymentSpec abstracts the Kubernetes Pod/Deployment configuration,
// completely decoupling the compute payload from the agent's application logic.
type DeploymentSpec struct {
	// Image specifies the container image repository.
	// +required
	Image string `json:"image"`

	// Tag specifies the container image tag.
	// +kubebuilder:default="latest"
	// +optional
	Tag *string `json:"tag,omitempty"`

	// ImagePullPolicy specifies if the image should be pulled.
	// +kubebuilder:default=IfNotPresent
	// +kubebuilder:validation:Enum=Always;Never;IfNotPresent
	// +optional
	ImagePullPolicy *corev1.PullPolicy `json:"imagePullPolicy,omitempty"`
}

// SecuritySpec manages Kubernetes RBAC, Pod Security, and Cloud Workload Identity,
// decoupling the operator from being strictly tied to GCP.
type SecuritySpec struct {
	// ServiceAccountName is the Kubernetes Service Account bound to the Deployment.
	// +optional
	ServiceAccountName string `json:"serviceAccountName,omitempty"`

	// WorkloadIdentity maps external IAM to the KSA securely.
	// +optional
	WorkloadIdentity *WorkloadIdentitySpec `json:"workloadIdentity,omitempty"`
}

// WorkloadIdentitySpec maps external IAM to the KSA securely.
type WorkloadIdentitySpec struct {
	// Gcp configures Google Cloud Workload Identity.
	// +optional
	Gcp *GcpWorkloadIdentitySpec `json:"gcp,omitempty"`
}

// GcpWorkloadIdentitySpec configures Google Cloud Workload Identity.
type GcpWorkloadIdentitySpec struct {
	// GSAName is the Google Service Account Name.
	// +optional
	GSAName string `json:"gsaName,omitempty"`

	// ProjectID is the GCP Project ID mapping the GSA to the KSA.
	// +optional
	ProjectID string `json:"projectId,omitempty"`
}

// ModelSpec configures the LLM reasoning backend. By utilizing nested providers,
// the AI provider is abstracted away from the core deployment logic.
type ModelSpec struct {
	// Provider is the active AI provider (e.g., "gemini").
	// +kubebuilder:validation:MinLength=1
	// +required
	Provider string `json:"provider"`

	// Default is the primary model to use (e.g., "gemini-3.1-flash-lite").
	// +kubebuilder:validation:MinLength=1
	// +required
	Default string `json:"default"`

	// Gemini configures the Gemini provider.
	// +optional
	Gemini *GeminiSpec `json:"gemini,omitempty"`
}

type GeminiSpec struct {
	// ApiKeySecretRef securely references a Secret containing the GEMINI_API_KEY.
	// +optional
	ApiKeySecretRef *corev1.SecretKeySelector `json:"apiKeySecretRef,omitempty"`
}
type DeploymentStatus struct {
	// Name is the exact name of the underlying Kubernetes Deployment.
	// +optional
	Name string `json:"name,omitempty"`

	// ReadyReplicas indicates how many replicas are fully ready.
	// +optional
	ReadyReplicas int32 `json:"readyReplicas,omitempty"`
}

type ServiceStatus struct {
	// Endpoint is the primary URL or IP (including protocol and port) to reach the agent.
	// +optional
	Endpoint string `json:"endpoint,omitempty"`
}

type StorageStatus struct {
	// Bound indicates if the primary PVC has been successfully provisioned.
	// +optional
	Bound bool `json:"bound,omitempty"`
}