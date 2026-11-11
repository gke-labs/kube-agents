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

const (
	// TierModeInteractive is a tier people's tasks run at: it may run
	// interactive profile tasks.
	TierModeInteractive = "interactive"
	// TierModeUnattended is a trigger's tier (the event watcher, a named
	// cron): it only starts profile tasks.
	TierModeUnattended = "unattended"

	// TierWriteModePropose lets a task propose a change (a pull request, a
	// ledger entry, a notice). Nothing enforces it yet (#3659).
	TierWriteModePropose = "propose"
	// TierWriteModeNone is explain-only: no proposals.
	TierWriteModeNone = "none"

	// TierConditionAccepted reports whether the operator read the tier.
	TierConditionAccepted = "Accepted"
)

// TierSpec is what a tier lets the tasks that run at it do
// (docs/designs/spec-profiles-dispatch.md, "Tiers and identity"). A tier's
// verb set follows from its mode, so a tier can't name a verb of its own and
// the vocabulary stays closed.
//
// This is the first of the tier work (#3659): no component reads a Tier yet,
// and the capability verifier and the gateway still use the read and
// unattended tiers built into them.
type TierSpec struct {
	// Mode is whether people or triggers run at this tier. interactive runs
	// interactive profile tasks; unattended only starts profile tasks.
	// +kubebuilder:validation:Enum=interactive;unattended
	// +required
	Mode string `json:"mode"`

	// WriteMode is what a task at this tier may do with a change it finds:
	// propose it (the default), or explain only (none). Not enforced yet:
	// the VCS broker reads it in a later change (#3659), and until then the
	// install-level setting is the only one that applies.
	// +kubebuilder:validation:Enum=none;propose
	// +kubebuilder:default=propose
	// +optional
	WriteMode string `json:"writeMode,omitempty"`
}

// TierStatus is the operator's view of the tier.
type TierStatus struct {
	// ObservedGeneration is the .metadata.generation the status was last
	// computed from.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Conditions represent the latest available observations of the tier.
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Mode",type=string,JSONPath=`.spec.mode`
// +kubebuilder:printcolumn:name="WriteMode",type=string,JSONPath=`.spec.writeMode`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
// +kubebuilder:validation:XValidation:rule="self.metadata.name.matches('^[a-z0-9]([-a-z0-9]*[a-z0-9])?$') && self.metadata.name.size() <= 63",message="Tier name must be a DNS-1123 label (lowercase letters, digits and hyphens, at most 63 characters): it is a token in the task submission subject"

// Tier is a set of what a task may do, which an AgentProfile allows and a
// requester runs at. It lives in the install's namespace. Only cluster
// administrators should write it: no agent ServiceAccount gets a verb on
// tiers, and its RBAC does not aggregate to the admin or edit roles.
type Tier struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   TierSpec   `json:"spec,omitempty"`
	Status TierStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// TierList contains a list of Tier.
type TierList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Tier `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Tier{}, &TierList{})
}
