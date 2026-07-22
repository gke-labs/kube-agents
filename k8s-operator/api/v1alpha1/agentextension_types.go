/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
handling conditions and limitations under the License.
*/

package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// AgentExtensionSpec defines the desired state of AgentExtension.
type AgentExtensionSpec struct {
	// AgentRef optionally references a specific PlatformAgent instance name.
	// If empty, this extension applies to all PlatformAgents in the same namespace.
	// +optional
	AgentRef string `json:"agentRef,omitempty"`

	// Config contains raw YAML configuration to be merged into the agent's config.yaml.
	// +optional
	Config string `json:"config,omitempty"`

	// Files maps relative file paths (e.g. "skills/gke-stockout-handler/SKILL.md") to file contents.
	// +optional
	Files map[string]string `json:"files,omitempty"`

	// Env specifies additional environment variables (including secret references) for the agent.
	// +optional
	Env []corev1.EnvVar `json:"env,omitempty"`
}

// AgentExtensionStatus defines the observed state of AgentExtension.
type AgentExtensionStatus struct {
	// Phase is the status phase of the extension (e.g. "Ready", "Error").
	// +optional
	Phase string `json:"phase,omitempty"`

	// TargetAgents lists the names of PlatformAgent instances this extension is applied to.
	// +optional
	TargetAgents []string `json:"targetAgents,omitempty"`

	// LastUpdated is the timestamp when the extension was last processed.
	// +optional
	LastUpdated *metav1.Time `json:"lastUpdated,omitempty"`

	// Conditions represent the latest available observations of the extension's state.
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=ae

// AgentExtension is the Schema for the agentextensions API.
type AgentExtension struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AgentExtensionSpec   `json:"spec,omitempty"`
	Status AgentExtensionStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AgentExtensionList contains a list of AgentExtension.
type AgentExtensionList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitzero"`
	Items           []AgentExtension `json:"items"`
}

func init() {
	SchemeBuilder.Register(&AgentExtension{}, &AgentExtensionList{})
}
