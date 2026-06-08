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

// DevTeamAgentSpec defines the desired state of DevTeamAgent
type DevTeamAgentSpec struct {
}

// DevTeamAgentStatus defines the observed state of DevTeamAgent.
type DevTeamAgentStatus struct {
	// Phase represents the current phase of the agent (e.g., Provisioning, Ready, Failed).
	// +optional
	Phase string `json:"phase,omitempty"`

	// conditions represent the current state of the DevTeamAgent resource.
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// DevTeamAgent is the Schema for the devteamagents API
type DevTeamAgent struct {
	metav1.TypeMeta `json:",inline"`

	// metadata is a standard object metadata
	// +optional
	metav1.ObjectMeta `json:"metadata,omitzero"`

	// spec defines the desired state of DevTeamAgent
	// +required
	Spec DevTeamAgentSpec `json:"spec"`

	// status defines the observed state of DevTeamAgent
	// +optional
	Status DevTeamAgentStatus `json:"status,omitzero"`
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
