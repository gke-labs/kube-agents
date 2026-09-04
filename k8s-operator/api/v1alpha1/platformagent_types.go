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

// PlatformAgentSpec defines the desired state of PlatformAgent
type PlatformAgentSpec struct {
	AgentSpec `json:",inline"`

	// Harness configures the core execution environment and framework-level settings.
	// +required
	Harness *HarnessSpec `json:"harness,omitempty"`

	// Integration configures platform-specific external connections.
	// +optional
	Integration *PlatformAgentIntegrationSpec `json:"integration,omitempty"`

	// Mode selects which component stack the operator renders.  "today" is the
	// current architecture.  "next" additionally renders the NATS and A2A
	// gateway components, which are otherwise dark.  Absent means "today".
	// +kubebuilder:validation:Enum=today;next
	// +optional
	Mode *string `json:"mode,omitempty"`
}

// PlatformAgentIntegrationSpec extends common IntegrationSpec with platform-specific connections.
type PlatformAgentIntegrationSpec struct {
	IntegrationSpec `json:",inline"`

	// GoogleChat configures the Google Chat integration.
	// +optional
	GoogleChat *GoogleChatSpec `json:"googleChat,omitempty"`

	// Slack configures the Slack integration.
	// +optional
	Slack *SlackSpec `json:"slack,omitempty"`

	// Teams configures the Microsoft Teams integration.
	// +optional
	Teams *TeamsSpec `json:"teams,omitempty"`
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

	// Mode controls output verbosity in Google Chat ("default" or "debug").
	// "default": Quiet mode (silences memory reviews, approval cards, and tool progress).
	// "debug": Full verbosity (surfaces tool progress, memory reviews, interim messages, and approval cards).
	// +kubebuilder:validation:Enum=default;debug
	// +kubebuilder:default:="default"
	// +optional
	Mode string `json:"mode,omitempty"`
}

// SlackSpec contains the configuration for the Slack integration.
// +kubebuilder:validation:XValidation:rule="!has(self.enabled) || self.enabled == false || (has(self.botTokenSecretRef) && has(self.appTokenSecretRef))",message="botTokenSecretRef and appTokenSecretRef are required when Slack integration is enabled"
type SlackSpec struct {
	// Enabled toggles the Slack integration.
	// +kubebuilder:default=false
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// BotTokenSecretRef securely references a Secret containing the SLACK_BOT_TOKEN.
	// +optional
	BotTokenSecretRef *corev1.SecretKeySelector `json:"botTokenSecretRef,omitempty"`

	// AppTokenSecretRef securely references a Secret containing the SLACK_APP_TOKEN.
	// +optional
	AppTokenSecretRef *corev1.SecretKeySelector `json:"appTokenSecretRef,omitempty"`

	// AllowedUsers is a list of allowed member IDs. If not present, all users will be allowed.
	// +listType=set
	// +optional
	AllowedUsers []string `json:"allowedUsers,omitempty"`

	// HomeChannel is the default channel ID for scheduled messages.
	// +optional
	HomeChannel string `json:"homeChannel,omitempty"`

	// HomeChannelName is the human-readable name for the home channel.
	// +optional
	HomeChannelName string `json:"homeChannelName,omitempty"`
}

// TeamsSpec contains the configuration for the Microsoft Teams integration.
// +kubebuilder:validation:XValidation:rule="!has(self.enabled) || self.enabled == false || (has(self.appIdSecretRef) && has(self.appPasswordSecretRef))",message="appIdSecretRef and appPasswordSecretRef are required when Teams integration is enabled"
type TeamsSpec struct {
	// Enabled toggles the Microsoft Teams integration.
	// +kubebuilder:default=false
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// AppIdSecretRef securely references a Secret containing the Microsoft App (Client) ID.
	// +optional
	AppIdSecretRef *corev1.SecretKeySelector `json:"appIdSecretRef,omitempty"`

	// AppPasswordSecretRef securely references a Secret containing the Microsoft App Client Secret.
	// +optional
	AppPasswordSecretRef *corev1.SecretKeySelector `json:"appPasswordSecretRef,omitempty"`

	// TenantId is the Microsoft Entra ID Tenant ID for single-tenant enterprise lock-down.
	// +optional
	TenantId string `json:"tenantId,omitempty"`

	// AllowedUsers is a list of allowed Entra ID Object IDs or User Principal Names.
	// When empty and allowAllUsers is false (default), interactions are rejected.
	// +listType=set
	// +optional
	AllowedUsers []string `json:"allowedUsers,omitempty"`

	// AllowAllUsers allows any authenticated tenant user to interact with the bot.
	// When false (default), users not listed in allowedUsers are rejected.
	// +kubebuilder:default=false
	// +optional
	AllowAllUsers *bool `json:"allowAllUsers,omitempty"`

	// HomeChannel is the default channel ID for scheduled messages.
	// +optional
	HomeChannel string `json:"homeChannel,omitempty"`

	// HomeChannelName is the human-readable name for the home channel.
	// +optional
	HomeChannelName string `json:"homeChannelName,omitempty"`

	// AdaptiveCards toggles rich Adaptive Card rendering for reports and updates.
	// +kubebuilder:default=true
	// +optional
	AdaptiveCards *bool `json:"adaptiveCards,omitempty"`
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
