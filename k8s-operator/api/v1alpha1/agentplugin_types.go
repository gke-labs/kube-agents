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
	// AgentRef references the specific PlatformAgent instance name.
	// +required
	AgentRef string `json:"agentRef"`

	// Image is the OCI image reference containing the plugin.
	Image string `json:"image"`

	// ImagePullPolicy specifies if the image should be pulled.
	// Defaults to IfNotPresent.
	// +kubebuilder:validation:Enum=Always;Never;IfNotPresent
	// +optional
	ImagePullPolicy *corev1.PullPolicy `json:"imagePullPolicy,omitempty"`

	// TargetProfile installs this plugin into a named Hermes profile (for example
	// "platform") instead of the default one. Empty means the default profile.
	//
	// A Hermes plugin is only usable by the profile it is installed in: the plugin's
	// register(ctx) hook runs when that profile loads it, and hooks such as
	// ctx.register_skill() are what make its skills resolvable. Mounting alone is not
	// enough — the plugin must also appear in that profile's plugins.enabled — so the
	// operator both stages the image for the profile and emits a config overlay that
	// enables it there. The two are always written together, for every profile name
	// including a cluster-<...> one; a plugin that is present but not enabled is inert and
	// fails only later, at the point of use.
	//
	// The image is mounted at /opt/agent-plugins/<profile>/<plugin>, outside the data PVC,
	// and linked into profiles/<profile>/plugins/<plugin> at startup. Mounting it into the
	// PVC directly had the kubelet create the profile directory before the entrypoint ran,
	// which suppressed the profile's own scaffold — see pluginProfileMountRoot in the
	// controller.
	//
	// The operator cannot validate that the profile exists: profiles are scaffolded at
	// pod startup, not by the operator. A name that matches no profile yields a plugin
	// that is never loaded; the entrypoint warns when an overlay names a missing profile.
	//
	// "default" is rejected rather than accepted as a synonym for the default profile.
	// That profile lives at the agent home root, not under profiles/, so targeting it by
	// name would mount the plugin into profiles/default/ — a directory nothing reads,
	// leaving it silently inert. Leave the field empty for the default profile.
	// +kubebuilder:validation:Pattern=`^[a-z0-9][a-z0-9-]*$`
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:XValidation:rule="self != 'default'",message="targetProfile must not be \"default\": the default profile lives at the agent home root rather than under profiles/, so targeting it by name mounts the plugin where nothing reads it. Omit targetProfile to install into the default profile."
	// +optional
	TargetProfile string `json:"targetProfile,omitempty"`

	// Config allows providing runtime overrides merged into the agent's config.yaml.
	// When TargetProfile is set the overrides are merged into that profile's config
	// instead of the default profile's.
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

	// ObservedGeneration is the .metadata.generation the status was last computed from.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

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
// Where a plugin was installed decides whether its skills resolve at all, and the
// failure is silent — an inert plugin looks identical to a working one. Surfacing the
// target in `kubectl get agentplugins` makes the common misconfiguration (targeting a
// profile that does not exist) visible without reading pod logs. Empty means the
// default profile.
// +kubebuilder:printcolumn:name="Profile",type=string,JSONPath=`.spec.targetProfile`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
// +kubebuilder:validation:XValidation:rule="self.metadata.name.matches('^[a-z][a-z0-9]*$')",message="AgentPlugin name must start with a lowercase letter and contain only lowercase letters and digits (no hyphens, dots, or underscores): the name is used both as the on-disk plugin directory and as the Hermes plugin module identifier"
// +kubebuilder:validation:XValidation:rule="self.metadata.name.size() <= 56",message="AgentPlugin name must be at most 56 characters so the derived 'plugin-<name>' volume name stays within the 63 character limit"

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
