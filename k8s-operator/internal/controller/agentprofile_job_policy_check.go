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
	"maps"
	"slices"

	admissionregistrationv1 "k8s.io/api/admissionregistration/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
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
// DispatchJobTemplate params. Gone (NotFound), unreadable for want of the
// grant (Forbidden), or loosened is not in place. Any other read error (a
// timeout, throttling, a control-plane upgrade) says nothing either way and
// comes back as err: the caller changes nothing, rather than tear down Job
// mode, running Jobs included, on a transient failure.
func (r *PlatformAgentReconciler) dispatcherJobPolicyInPlace(ctx context.Context) (bool, string, error) {
	var policy admissionregistrationv1.ValidatingAdmissionPolicy
	if err := r.a2aReader().Get(ctx, types.NamespacedName{Name: dispatcherJobPolicyName}, &policy); err != nil {
		return policyReadFailure("ValidatingAdmissionPolicy", err)
	}
	var binding admissionregistrationv1.ValidatingAdmissionPolicyBinding
	if err := r.a2aReader().Get(ctx, types.NamespacedName{Name: dispatcherJobPolicyName}, &binding); err != nil {
		return policyReadFailure("ValidatingAdmissionPolicyBinding", err)
	}
	ps := policy.Spec
	switch {
	case ps.FailurePolicy == nil || *ps.FailurePolicy != admissionregistrationv1.Fail:
		return false, "its failurePolicy is not Fail", nil
	case ps.ParamKind == nil || ps.ParamKind.APIVersion != agentv1alpha1.GroupVersion.String() || ps.ParamKind.Kind != "DispatchJobTemplate":
		return false, "its params are not the operator's DispatchJobTemplates", nil
	}
	bs := binding.Spec
	ref := bs.ParamRef
	switch {
	case bs.PolicyName != dispatcherJobPolicyName:
		return false, fmt.Sprintf("its binding names policy %q", bs.PolicyName), nil
	case !slices.Contains(bs.ValidationActions, admissionregistrationv1.Deny):
		return false, "its binding does not deny", nil
	case ref == nil || ref.Name != "" || ref.Selector == nil || ref.ParameterNotFoundAction == nil ||
		*ref.ParameterNotFoundAction != admissionregistrationv1.DenyAction:
		return false, "its binding does not select params by label with no params denied", nil
	case ref.Selector.MatchLabels[labelManagedBy] != fieldOwner || ref.Selector.MatchLabels[dispatchJobTemplateLabel] != "true" ||
		len(ref.Selector.MatchExpressions) != 0:
		return false, "its binding's params are not the operator's labelled DispatchJobTemplates alone", nil
	case bs.MatchResources == nil || bs.MatchResources.NamespaceSelector == nil ||
		!maps.Equal(bs.MatchResources.NamespaceSelector.MatchLabels, profileNamespaceSelector) ||
		len(bs.MatchResources.NamespaceSelector.MatchExpressions) != 0:
		// Exactly these labels: one more, or any expression, narrows the
		// binding past the profile namespace profileNamespaceSelectedByJobPolicy
		// checks, which then gets the Jobs Role with no policy over it.
		return false, "its binding is not scoped to profile namespaces by exactly the profile namespace's labels", nil
	}
	return true, "", nil
}

func policyReadFailure(kind string, err error) (bool, string, error) {
	switch {
	case apierrors.IsNotFound(err):
		return false, fmt.Sprintf("the %s %s is not in the cluster", kind, dispatcherJobPolicyName), nil
	case apierrors.IsForbidden(err):
		return false, fmt.Sprintf("the operator can't read the %s %s (its role lacks the get grant): %v", kind, dispatcherJobPolicyName, err), nil
	}
	return false, "", fmt.Errorf("read the %s %s: %w", kind, dispatcherJobPolicyName, err)
}

// profileNamespaceSelectedByJobPolicy reports whether the profile namespace
// carries every label the policy's binding selects it by: one that lacks one
// is not held to the policy, so the dispatcher must not create Jobs there.
func (r *PlatformAgentReconciler) profileNamespaceSelectedByJobPolicy(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (bool, error) {
	var ns corev1.Namespace
	if err := r.a2aReader().Get(ctx, types.NamespacedName{Name: profileNamespaceName(agent)}, &ns); err != nil {
		return false, fmt.Errorf("read the profile namespace %s: %w", profileNamespaceName(agent), err)
	}
	for k, v := range profileNamespaceSelector {
		if ns.Labels[k] != v {
			return false, nil
		}
	}
	return true, nil
}

// profileNamespaceSelector is the binding's namespaceSelector, exactly: the
// labels the operator requires of a profile namespace.
var profileNamespaceSelector = map[string]string{
	profileNamespaceComponentLabel: profileNamespaceComponent,
	labelPartOf:                    partOfKubeAgents,
}

// dispatcherJobModeCondition says on the PlatformAgent whether the dispatcher
// runs unattended profile tasks as Jobs. The DispatcherJobPolicyMissing Event
// says why not on each pass, but Events expire, and nothing else in status
// said whether Job mode was on. Written only while the chart signals the
// policy (A2A_DISPATCHER_JOB_POLICY) and the dispatcher runs, and removed
// only by a pass that tore Job mode down (jobModeTornDown).
//
// It follows what the dispatcher pass did to the Jobs Role, and nothing
// else: True when the pass granted it, False with the reason when the pass
// withheld or revoked it, and unchanged when the pass did neither -- a read
// of the policy that answered neither way, or a pass that returned before
// the dispatcher step. So a transient failure that leaves Job mode standing
// doesn't flap the condition, and one that revokes the Role does, because
// Job mode is then off.
const dispatcherJobModeCondition = "DispatcherJobMode"

const (
	// The pass found the policy and its binding in place and the profile
	// namespace selected by the binding, and granted the Jobs Role.
	jobModeReasonInPlace = "InPlace"
	// The policy or its binding is missing, unreadable (Forbidden) or
	// loosened; the message is the DispatcherJobPolicyMissing Event's.
	jobModeReasonPolicyMissing = "PolicyMissing"
	// The profile namespace lacks a label the binding selects it by; the
	// message is the DispatcherJobPolicyMissing Event's.
	jobModeReasonNamespaceNotSelected = "NamespaceNotSelected"
	// The profile namespace is absent or lacks the chart's markers, or the
	// pass didn't apply its network fence (a failed fence write or namespace
	// read included), so nothing renders there.
	jobModeReasonNamespaceNotReady = "NamespaceNotReady"
	// A DispatchJobTemplate under a param's name isn't this agent's
	// (errForeignDispatchJobParam). It stands until somebody removes it.
	jobModeReasonForeignJobTemplate = "ForeignJobTemplate"
	// Any other failure granting Job mode (the CA bundle copy, the params,
	// the Jobs Role, a read of the profile namespace): the Role is revoked
	// for the pass, and the message carries the error.
	jobModeReasonGrantFailed = "GrantFailed"
)

// jobModeOffConsequence is what Job mode being off means for tasks, the tail
// of every False message and of the DispatcherJobPolicyMissing Event.
const jobModeOffConsequence = "so unattended profile tasks have no worker and are refused no-worker at admission"

func jobModeOn(profileNamespace string) *metav1.Condition {
	return &metav1.Condition{
		Type:   dispatcherJobModeCondition,
		Status: metav1.ConditionTrue,
		Reason: jobModeReasonInPlace,
		Message: fmt.Sprintf("the last pass that settled Job mode found the dispatcher Job admission policy in place and granted the dispatcher its Jobs Role in the profile namespace %s",
			profileNamespace),
	}
}

func jobModeOff(reason, message string) *metav1.Condition {
	return &metav1.Condition{Type: dispatcherJobModeCondition, Status: metav1.ConditionFalse, Reason: reason, Message: message}
}

func jobModeGrantFailed(err error) *metav1.Condition {
	return jobModeOff(jobModeReasonGrantFailed, fmt.Sprintf("the dispatcher's Job mode could not be granted (%v), %s", err, jobModeOffConsequence))
}

// jobModeTornDown is the verdict of a pass that tore Job mode down, or found
// nothing of it left to tear down: removeA2ADispatcher with the dispatcher
// disabled, the gate-off sweep with the signal off, or cleanupA2A on a flip
// to today, each run to completion. It alone removes the condition. A pass
// that returns before any of them (a refusal above the A2A step, a sweep
// that failed) leaves the condition as it was, as BusCredentialsReady is
// left on a refused flip: the Jobs Role and the templates are still live,
// and an absent condition would say Job mode is off. Compared by identity.
var jobModeTornDown = &metav1.Condition{Type: dispatcherJobModeCondition}

// syncDispatcherJobMode brings the DispatcherJobMode condition into line with
// the dispatcher pass's verdict, and is called on the way out of Reconcile,
// as syncBusCredentialsReady is and for its reason: written mid-sequence, an
// early return would skip it, and on the way out it lands after
// updateStatusReady and updateStatusDegraded rather than racing them.
//
// verdict decides everything: nil when the pass made nothing of Job mode
// (the condition stays as it was), jobModeTornDown when it tore Job mode
// down (the condition goes), and otherwise the condition's new value. It
// writes status only on a change: this runs on every pass.
func (r *PlatformAgentReconciler) syncDispatcherJobMode(ctx context.Context, agent *agentv1alpha1.PlatformAgent, verdict *metav1.Condition) error {
	switch verdict {
	case nil:
		return nil
	case jobModeTornDown:
		if meta.RemoveStatusCondition(&agent.Status.Conditions, dispatcherJobModeCondition) {
			return r.Status().Update(ctx, agent)
		}
		return nil
	}
	condition := *verdict
	condition.LastTransitionTime = metav1.Now()
	if !meta.SetStatusCondition(&agent.Status.Conditions, condition) {
		return nil
	}
	return r.Status().Update(ctx, agent)
}
