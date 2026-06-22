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
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// PlatformAgentSpec defines the desired state of PlatformAgent
type PlatformAgentSpec struct {
	// Harness configures the core execution environment and framework-level settings.
	// +optional
	Harness *PlatformAgentHarnessSpec `json:"harness,omitempty"`

	// Deployment abstracts the Kubernetes Pod/Deployment configuration.
	// +optional
	Deployment *DeploymentSpec `json:"deployment,omitempty"`

	// Security configures RBAC, Pod Security, and Workload Identity.
	// +optional
	Security *SecuritySpec `json:"security,omitempty"`

	// Integration configures platform-specific external connections.
	// +optional
	Integration *IntegrationSpec `json:"integration,omitempty"`
}

// PlatformAgentHarnessSpec configures the core execution environment and framework-level settings for the agent.
// This extracts environmental context that doesn't belong in infrastructure blocks.
type PlatformAgentHarnessSpec struct {
	// ClusterName is the logical name of the cluster where the agent is running.
	// +optional
	ClusterName string `json:"clusterName,omitempty"`

	// Location is the geographical location or cloud region.
	// +optional
	Location string `json:"location,omitempty"`

	// ProjectID is the GCP Project ID of the cluster.
	// +optional
	ProjectID string `json:"projectId,omitempty"`

	// Hermes configures the internal event-routing or agent framework.
	// +optional
	Hermes *HermesSpec `json:"hermes,omitempty"`
}

// IntegrationSpec isolates platform-specific external connections.
type IntegrationSpec struct {
	// GoogleChat configures the Google Chat integration.
	// +optional
	GoogleChat *GoogleChatSpec `json:"googleChat,omitempty"`
}

// GoogleChatSpec contains the configuration for the Google Chat integration,
// enabling communication and event routing via Google Chat.
// +kubebuilder:validation:XValidation:rule="!has(self.enabled) || self.enabled == false || (has(self.projectId) && has(self.topicName) && has(self.subscriptionName))",message="projectId, topicName, and subscriptionName are required when Google Chat integration is enabled"
type GoogleChatSpec struct {
	// Enabled toggles the Google Chat integration.
	// +kubebuilder:default=false
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// ProjectID is the target GCP Project ID for Pub/Sub.
	// +optional
	ProjectID string `json:"projectId,omitempty"`

	// TopicName is the GCP Chat Topic Name.
	// +optional
	TopicName string `json:"topicName,omitempty"`

	// SubscriptionName is the GCP Chat Subscription Name.
	// +optional
	SubscriptionName string `json:"subscriptionName,omitempty"`

	// AllowedUsers is a list of allowed users. If not present, all users will be allowed.
	// +listType=set
	// +optional
	AllowedUsers []string `json:"allowedUsers,omitempty"`

	// HomeChannel is the home channel Chat address.
	// +optional
	HomeChannel string `json:"homeChannel,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// PlatformAgent is the Schema for the platformagents API
type PlatformAgent struct {
	metav1.TypeMeta `json:",inline"`

	// metadata is a standard object metadata
	// +optional
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// spec defines the desired state of PlatformAgent
	// +required
	Spec PlatformAgentSpec `json:"spec"`

	// status defines the observed state of PlatformAgent
	// +optional
	Status AgentStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// PlatformAgentList contains a list of PlatformAgent
type PlatformAgentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitzero"`
	Items           []PlatformAgent `json:"items"`
}

func init() {
	SchemeBuilder.Register(&PlatformAgent{}, &PlatformAgentList{})
}
