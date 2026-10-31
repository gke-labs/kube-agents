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

package controller

import (
	"context"
	"fmt"
	"slices"

	admissionregistrationv1 "k8s.io/api/admissionregistration/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// dispatcherJobPolicyName names the dispatcher Job admission policy and its
// binding (config/admission/a2a-dispatcher-job-policy.yaml).
const dispatcherJobPolicyName = "kube-agents-a2a-dispatcher-job"

// reasonDispatcherJobPolicyMissing is the Warning Event on the PlatformAgent
// when the chart signalled the Job admission policy but it isn't in the
// cluster as rendered, so the dispatcher gets no Job mode.
const reasonDispatcherJobPolicyMissing = "DispatcherJobPolicyMissing"

// dispatcherJobPolicyInPlace reports whether the dispatcher Job admission
// policy and its binding are in the cluster as the chart renders them, and if
// not, why. A2A_DISPATCHER_JOB_POLICY says the chart rendered them, not that
// they still hold: a Helm disable deletes them before the operator rolls, and
// anyone with the rights can delete or loosen the binding. So before the
// dispatcher gets its Jobs Role, every reconcile reads both (uncached, by
// name: the operator holds get on these two kinds, nothing else) and checks
// the parts the policy's guarantee rests on: the binding denies, finds params
// only by the operator's labels with no params denied, is scoped to profile
// namespaces, and names the policy; the policy fails closed and reads
// PodTemplate params. A read that fails, including Forbidden on an install
// whose operator role predates the grant, is not in place.
func (r *PlatformAgentReconciler) dispatcherJobPolicyInPlace(ctx context.Context) (bool, string) {
	var policy admissionregistrationv1.ValidatingAdmissionPolicy
	if err := r.a2aReader().Get(ctx, types.NamespacedName{Name: dispatcherJobPolicyName}, &policy); err != nil {
		return false, policyReadFailure("ValidatingAdmissionPolicy", err)
	}
	var binding admissionregistrationv1.ValidatingAdmissionPolicyBinding
	if err := r.a2aReader().Get(ctx, types.NamespacedName{Name: dispatcherJobPolicyName}, &binding); err != nil {
		return false, policyReadFailure("ValidatingAdmissionPolicyBinding", err)
	}
	ps := policy.Spec
	switch {
	case ps.FailurePolicy == nil || *ps.FailurePolicy != admissionregistrationv1.Fail:
		return false, "its failurePolicy is not Fail"
	case ps.ParamKind == nil || ps.ParamKind.APIVersion != agentv1alpha1.GroupVersion.String() || ps.ParamKind.Kind != "DispatchJobTemplate":
		return false, "its params are not the operator's DispatchJobTemplates"
	}
	bs := binding.Spec
	ref := bs.ParamRef
	switch {
	case bs.PolicyName != dispatcherJobPolicyName:
		return false, fmt.Sprintf("its binding names policy %q", bs.PolicyName)
	case !slices.Contains(bs.ValidationActions, admissionregistrationv1.Deny):
		return false, "its binding does not deny"
	case ref == nil || ref.Name != "" || ref.Selector == nil || ref.ParameterNotFoundAction == nil ||
		*ref.ParameterNotFoundAction != admissionregistrationv1.DenyAction:
		return false, "its binding does not select params by label with no params denied"
	case ref.Selector.MatchLabels[labelManagedBy] != fieldOwner || ref.Selector.MatchLabels[dispatchJobTemplateLabel] != "true" ||
		len(ref.Selector.MatchExpressions) != 0:
		return false, "its binding's params are not the operator's labelled PodTemplates alone"
	case bs.MatchResources == nil || bs.MatchResources.NamespaceSelector == nil ||
		bs.MatchResources.NamespaceSelector.MatchLabels[profileNamespaceComponentLabel] != profileNamespaceComponent:
		return false, "its binding is not scoped to profile namespaces"
	}
	return true, ""
}

func policyReadFailure(kind string, err error) string {
	if apierrors.IsNotFound(err) {
		return fmt.Sprintf("the %s %s is not in the cluster", kind, dispatcherJobPolicyName)
	}
	return fmt.Sprintf("reading the %s %s: %v", kind, dispatcherJobPolicyName, err)
}
