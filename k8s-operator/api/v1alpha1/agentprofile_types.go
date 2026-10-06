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
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// AgentProfile is the A2A side's profile: one kind of agent pod, described once,
// that the operator renders a ServiceAccount, a bus identity and a directory card
// from. It is not a Hermes profile (a directory under $HERMES_HOME/profiles that
// the platform agent scaffolds); those stay as they are until the Hermes-side
// profile model is retired. docs/designs/spec-subagent-profiles.md is the design
// of record, and AgentProfileSpec is its field table, field for field
// (TestAgentProfileFieldsMatchTheSpecTable).
//
// Everything an AgentProfile renders is dark unless the PlatformAgent in its
// namespace runs `spec.mode: next`.

// DefaultAgentProfileQueueTimeoutSeconds is the spec's default for
// AgentProfileSpec.QueueTimeoutSeconds.
const DefaultAgentProfileQueueTimeoutSeconds = 3600

// AgentProfileTopicPattern is a topic grant as a profile writes it:
// shared.{topic} or agent.{agent}.{topic}, each token a dot-free DNS-1123
// label. The operator adds the a2a.topics. prefix; the callout refuses
// anything else in the same position.
const AgentProfileTopicPattern = `^(shared\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?|agent\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)$`

// AgentProfileSpec defines the desired state of AgentProfile.
type AgentProfileSpec struct {
	// Description is the routing blurb, rendered into the A2A agent card.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:XValidation:rule="self.trim().size() > 0",message="description is the routing blurb the agent card is rendered from and must not be blank"
	// +required
	Description string `json:"description"`

	// Persona is the OCI artifact holding SOUL.md, AGENTS.md, skills and the
	// persona config, mounted read-only via image volume.
	// +required
	Persona AgentProfilePersona `json:"persona"`

	// Harness is the worker image and its model routing.
	// +required
	Harness AgentProfileHarness `json:"harness"`

	// Bus holds topic grants beyond the task subjects every executor gets.
	// Absent or empty grants no topic at all.
	// +optional
	Bus AgentProfileBus `json:"bus,omitempty"`

	// Identity names the ServiceAccount the profile's pods run as. Absent, the
	// operator creates one with zero RBAC bindings: the token exists to
	// authenticate to the bus, not to talk to the API server.
	// +optional
	Identity AgentProfileIdentity `json:"identity,omitempty"`

	// ClusterRef is set on cluster agents only: the cluster this profile is
	// scoped to, as structured data rather than a parsed name.
	// +optional
	ClusterRef *AgentProfileClusterRef `json:"clusterRef,omitempty"`

	// Lifecycle bounds one task's wall clock and how long the finished Job
	// lingers.
	// +required
	Lifecycle AgentProfileLifecycle `json:"lifecycle"`

	// QueueTimeoutSeconds is how stale a queued submission may be, by its
	// JetStream server ingest time, before the dispatcher refuses to run it.
	// Absent or zero means DefaultAgentProfileQueueTimeoutSeconds.
	// +kubebuilder:validation:Minimum=0
	// +optional
	QueueTimeoutSeconds int32 `json:"queueTimeoutSeconds,omitempty"`

	// Concurrency is the most pods this profile runs at once.
	// +kubebuilder:validation:Minimum=1
	// +required
	Concurrency int32 `json:"concurrency"`

	// Resources is the pod resource class.
	// +required
	Resources AgentProfileResources `json:"resources"`
}

// AgentProfilePersona is the persona artifact.
type AgentProfilePersona struct {
	// Image is the OCI reference of the persona artifact.
	// +kubebuilder:validation:MinLength=1
	// +required
	Image string `json:"image"`
}

// AgentProfileHarness is the worker image and its model routing.
type AgentProfileHarness struct {
	// Image is the worker image: a harness speaking the headless CLI contract,
	// wrapped by the bus adapter.
	// +kubebuilder:validation:MinLength=1
	// +required
	Image string `json:"image"`

	// Model is the model route, via LiteLLM.
	// +optional
	Model string `json:"model,omitempty"`

	// MaxTurns is the turn budget.
	// +kubebuilder:validation:Minimum=0
	// +optional
	MaxTurns int32 `json:"maxTurns,omitempty"`
}

// AgentProfileBus holds the profile's blackboard grants, written without the
// a2a.topics. prefix.
type AgentProfileBus struct {
	// PublishTopics are the topics the profile's pods may write.
	// +kubebuilder:validation:MaxItems=32
	// +kubebuilder:validation:items:Pattern=`^(shared\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?|agent\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)$`
	// +kubebuilder:validation:items:MaxLength=134
	// +listType=set
	// +optional
	PublishTopics []string `json:"publishTopics,omitempty"`

	// SubscribeTopics are the topics the profile's pods may read.
	// +kubebuilder:validation:MaxItems=32
	// +kubebuilder:validation:items:Pattern=`^(shared\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?|agent\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)$`
	// +kubebuilder:validation:items:MaxLength=134
	// +listType=set
	// +optional
	SubscribeTopics []string `json:"subscribeTopics,omitempty"`
}

// AgentProfileIdentity names the ServiceAccount the profile's pods run as.
type AgentProfileIdentity struct {
	// ServiceAccountName is an existing ServiceAccount in the profile's
	// namespace. Naming one hands this profile's pods whatever RBAC it holds;
	// absent, the operator creates a ServiceAccount with none.
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`
	// +optional
	ServiceAccountName string `json:"serviceAccountName,omitempty"`
}

// AgentProfileClusterRef identifies one GKE cluster.
type AgentProfileClusterRef struct {
	// +kubebuilder:validation:MinLength=1
	// +required
	ProjectID string `json:"projectId"`
	// +kubebuilder:validation:MinLength=1
	// +required
	Cluster string `json:"cluster"`
	// +kubebuilder:validation:MinLength=1
	// +required
	Location string `json:"location"`
}

// AgentProfileLifecycle bounds one task's Job.
type AgentProfileLifecycle struct {
	// ActiveDeadlineSeconds is the hard ceiling on one task's wall clock,
	// enforced by Kubernetes from Job spawn.
	// +kubebuilder:validation:Minimum=1
	// +required
	ActiveDeadlineSeconds int64 `json:"activeDeadlineSeconds"`

	// TTLSecondsAfterFinished is how long a finished Job lingers for
	// inspection before garbage collection.
	// +kubebuilder:validation:Minimum=0
	// +required
	TTLSecondsAfterFinished int32 `json:"ttlSecondsAfterFinished"`
}

// AgentProfileResources is the pod resource class: cpu and memory, both
// required, for requests and limits.
type AgentProfileResources struct {
	// +required
	Requests AgentProfileResourceList `json:"requests"`
	// +required
	Limits AgentProfileResourceList `json:"limits"`
}

// AgentProfileResourceList is a cpu and memory pair.
type AgentProfileResourceList struct {
	// +required
	CPU resource.Quantity `json:"cpu"`
	// +required
	Memory resource.Quantity `json:"memory"`
}

// AgentProfile condition types.
const (
	// AgentProfileConditionIdentityReady is True when the profile's
	// ServiceAccount and its bus identity are rendered.
	AgentProfileConditionIdentityReady = "IdentityReady"
	// AgentProfileConditionCardPublished is True when the profile's agent card
	// is on the directory.
	AgentProfileConditionCardPublished = "CardPublished"
)

// AgentProfileStatus defines the observed state of AgentProfile.
type AgentProfileStatus struct {
	// ObservedGeneration is the .metadata.generation the status was last
	// computed from.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// AgentRef is the PlatformAgent this profile is bound to: the one in its
	// namespace.
	// +optional
	AgentRef string `json:"agentRef,omitempty"`

	// ServiceAccountName is the ServiceAccount the profile's pods run as,
	// whether named in spec.identity or created by the operator.
	// +optional
	ServiceAccountName string `json:"serviceAccountName,omitempty"`

	// Conditions represent the latest available observations of the profile.
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=apr
// +kubebuilder:printcolumn:name="ServiceAccount",type=string,JSONPath=`.status.serviceAccountName`
// +kubebuilder:printcolumn:name="Identity",type=string,JSONPath=`.status.conditions[?(@.type=="IdentityReady")].status`
// +kubebuilder:printcolumn:name="Card",type=string,JSONPath=`.status.conditions[?(@.type=="CardPublished")].status`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
// The name is the addressee token in a2a.tasks.{profile}.… and a2a.agents.{profile},
// so it is a single subject token: a dot-free DNS-1123 label.
// +kubebuilder:validation:XValidation:rule="self.metadata.name.matches('^[a-z0-9]([-a-z0-9]*[a-z0-9])?$') && self.metadata.name.size() <= 63",message="AgentProfile name must be a dot-free DNS-1123 label of at most 63 characters: it is the addressee token on the task and directory subjects"
// `platform` is the Hermes bridge's addressee: a profile under that name would put a
// second executor on a2a.tasks.platform.*. The platform persona as a profile is not a goal.
// +kubebuilder:validation:XValidation:rule="self.metadata.name != 'platform'",message="AgentProfile name 'platform' is reserved: the Hermes bridge is the executor for that addressee"

// AgentProfile is the Schema for the agentprofiles API.
type AgentProfile struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AgentProfileSpec   `json:"spec,omitempty"`
	Status AgentProfileStatus `json:"status,omitempty"`
}

// QueueTimeout reports the effective queue deadline, applying the spec's
// default when the field is absent.
func (p *AgentProfile) QueueTimeout() int32 {
	if p.Spec.QueueTimeoutSeconds == 0 {
		return DefaultAgentProfileQueueTimeoutSeconds
	}
	return p.Spec.QueueTimeoutSeconds
}

// +kubebuilder:object:root=true

// AgentProfileList contains a list of AgentProfile.
type AgentProfileList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []AgentProfile `json:"items"`
}

func init() {
	SchemeBuilder.Register(&AgentProfile{}, &AgentProfileList{})
}
