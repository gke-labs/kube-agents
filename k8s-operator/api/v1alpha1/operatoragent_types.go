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

// EDIT THIS FILE!  THIS IS SCAFFOLDING FOR YOU TO OWN!
// NOTE: json tags are required.  Any new fields you add must have json tags for the fields to be serialized.

// OperatorHarnessSpec configures the target cluster environment and framework-level settings for the operator agent.
type OperatorHarnessSpec struct {

	// ClusterName is the logical name of the target cluster.
	// +required
	ClusterName string `json:"clusterName,omitempty"`

	// Location is the geographical location or cloud region of the target cluster.
	// +required
	Location string `json:"location,omitempty"`

	// ProjectID is the GCP Project ID of the target cluster.
	// +optional
	ProjectID string `json:"projectId,omitempty"`

	// Hermes configures the internal event-routing or agent framework.
	// +optional
	Hermes *HermesSpec `json:"hermes,omitempty"`
}

// OperatorAgentSpec defines the desired state of OperatorAgent
type OperatorAgentSpec struct {
	// Harness configures the core execution environment and framework-level settings.
	// +required
	Harness *OperatorHarnessSpec `json:"harness,omitempty"`

	// Deployment abstracts the Kubernetes Pod/Deployment configuration.
	// +optional
	Deployment *DeploymentSpec `json:"deployment,omitempty"`

	// Security configures RBAC, Pod Security, and Workload Identity.
	// +optional
	Security *SecuritySpec `json:"security,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// OperatorAgent is the Schema for the operatoragents API
type OperatorAgent struct {
	metav1.TypeMeta `json:",inline"`

	// metadata is a standard object metadata
	// +optional
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// spec defines the desired state of OperatorAgent
	// +required
	Spec OperatorAgentSpec `json:"spec"`

	// status defines the observed state of OperatorAgent
	// +optional
	Status AgentStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// OperatorAgentList contains a list of OperatorAgent
type OperatorAgentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []OperatorAgent `json:"items"`
}

func init() {
	SchemeBuilder.Register(&OperatorAgent{}, &OperatorAgentList{})
}
