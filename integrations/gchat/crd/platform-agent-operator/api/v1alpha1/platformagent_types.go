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

// PlatformAgentSpec defines the desired state of PlatformAgent
type PlatformAgentSpec struct {
	ProjectID              string `json:"projectId"`
	ImageURI               string `json:"imageUri"`
	ChatTopicName          string `json:"chatTopicName"`
	ChatSubName            string `json:"chatSubName"`
	GSAName                string `json:"gsaName"`
	KSAName                string `json:"ksaName"`
	GoogleChatAllowedUsers string `json:"googleChatAllowedUsers"`
	GoogleChatHomeChannel  string `json:"googleChatHomeChannel"`
	NumericProjectID       string `json:"numericProjectId"`
	ClusterName            string `json:"clusterName"`
	Location               string `json:"location"`
}

// PlatformAgentStatus defines the observed state of PlatformAgent.
type PlatformAgentStatus struct {
	Phase string `json:"phase,omitempty"` // e.g., "Provisioning", "Ready"
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// PlatformAgent is the Schema for the platformagents API
type PlatformAgent struct {
	metav1.TypeMeta `json:",inline"`

	// metadata is a standard object metadata
	// +optional
	metav1.ObjectMeta `json:"metadata,omitzero"`

	// spec defines the desired state of PlatformAgent
	// +required
	Spec PlatformAgentSpec `json:"spec"`

	// status defines the observed state of PlatformAgent
	// +optional
	Status PlatformAgentStatus `json:"status,omitzero"`
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
