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
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// AgentPluginSpec defines the desired state of AgentPlugin.
type AgentPluginSpec struct {
	// AgentRef optionally references a specific PlatformAgent instance name.
	// If empty, this plugin applies to all PlatformAgents in the same namespace.
	// +optional
	AgentRef string `json:"agentRef,omitempty"`

	// Image is the OCI image reference containing the plugin.
	Image string `json:"image"`

	// Config allows providing runtime overrides merged into the main agent config.yaml.
	// +optional
	Config string `json:"config,omitempty"`

	// Env specifies additional environment variables for the agent.
	// +optional
	Env []corev1.EnvVar `json:"env,omitempty"`
}

// AgentPluginStatus defines the observed state of AgentPlugin.
type AgentPluginStatus struct {
	// Phase is the status phase of the plugin (e.g. "Ready", "Error").
	// +optional
	Phase string `json:"phase,omitempty"`

	// TargetAgents lists the names of PlatformAgent instances this plugin is applied to.
	// +optional
	TargetAgents []string `json:"targetAgents,omitempty"`

	// LastUpdated is the timestamp when the plugin was last processed.
	// +optional
	LastUpdated *metav1.Time `json:"lastUpdated,omitempty"`

	// Conditions represent the latest available observations of the plugin's state.
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=ap

// AgentPlugin is the Schema for the agentplugins API.
type AgentPlugin struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AgentPluginSpec   `json:"spec,omitempty"`
	Status AgentPluginStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AgentPluginList contains a list of AgentPlugin.
type AgentPluginList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []AgentPlugin `json:"items"`
}

func init() {
	SchemeBuilder.Register(&AgentPlugin{}, &AgentPluginList{})
}
