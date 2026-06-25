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

// DevTeamHarnessSpec configures the target remote environment and framework-level settings for the devteam agent.
type DevTeamHarnessSpec struct {

	// ClusterName is the logical name of the target cluster.
	// +required
	ClusterName string `json:"clusterName,omitempty"`

	// Location is the geographical location or cloud region of the target cluster.
	// +required
	Location string `json:"location,omitempty"`

	// ProjectID is the GCP Project ID of the target cluster.
	// +optional
	ProjectID string `json:"projectId,omitempty"`

	// Namespace is the target remote namespace managed by this agent.
	// +required
	Namespace string `json:"namespace,omitempty"`

	// Hermes configures the internal event-routing or agent framework.
	// +optional
	Hermes *HermesSpec `json:"hermes,omitempty"`
}

// DevTeamAgentSpec defines the desired state of DevTeamAgent
type DevTeamAgentSpec struct {
	// Harness configures the core execution environment and framework-level settings.
	// +required
	Harness *DevTeamHarnessSpec `json:"harness,omitempty"`

	// Deployment abstracts the Kubernetes Pod/Deployment configuration.
	// +optional
	Deployment *DeploymentSpec `json:"deployment,omitempty"`

	// Security manages Kubernetes RBAC, Pod Security, and Cloud Workload Identity.
	// +optional
	Security *SecuritySpec `json:"security,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced

// DevTeamAgent is the Schema for the devteamagents API
type DevTeamAgent struct {
	metav1.TypeMeta `json:",inline"`

	// metadata is a standard object metadata
	// +optional
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// spec defines the desired state of DevTeamAgent
	// +required
	Spec DevTeamAgentSpec `json:"spec"`

	// status defines the observed state of DevTeamAgent
	// +optional
	Status AgentStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// DevTeamAgentList contains a list of DevTeamAgent
type DevTeamAgentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitzero"`
	Items           []DevTeamAgent `json:"items"`
}

func init() {
	SchemeBuilder.Register(&DevTeamAgent{}, &DevTeamAgentList{})
}
