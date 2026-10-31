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
	"k8s.io/apimachinery/pkg/runtime"
)

// DispatchJobTemplateSpec is one param of the dispatcher Job admission
// policy (config/admission/a2a-dispatcher-job-policy.yaml). The operator
// alone writes these, in the profile namespace; they are not for hand
// editing.
type DispatchJobTemplateSpec struct {
	// Template is the pod template an unattended profile's Jobs must equal
	// exactly: the operator's rendering, as the API server defaults it (a
	// server-side dry run of a PodTemplate), so it compares field for field
	// with a Job's defaulted pod template. Absent on the index.
	// +kubebuilder:pruning:PreserveUnknownFields
	// +kubebuilder:validation:Schemaless
	// +kubebuilder:validation:Type=object
	// +optional
	Template runtime.RawExtension `json:"template,omitempty"`

	// ServiceAccounts, on the index only, are the accounts the templates run
	// as: a dispatcher Job must run as one of them.
	// +optional
	ServiceAccounts []string `json:"serviceAccounts,omitempty"`
}

// +kubebuilder:object:root=true

// DispatchJobTemplate holds what the dispatcher's Jobs in a profile namespace
// are held to: a profile's Job template, or (the index) the accounts they run
// as. A CRD rather than a core PodTemplate because the API server's param
// cache for a built-in kind freezes once its last binding is deleted and
// recreated (kubernetes/kubernetes#141015), and a CRD kind's does not.
type DispatchJobTemplate struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec DispatchJobTemplateSpec `json:"spec,omitempty"`
}

// +kubebuilder:object:root=true

// DispatchJobTemplateList contains a list of DispatchJobTemplate.
type DispatchJobTemplateList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []DispatchJobTemplate `json:"items"`
}

func init() {
	SchemeBuilder.Register(&DispatchJobTemplate{}, &DispatchJobTemplateList{})
}
