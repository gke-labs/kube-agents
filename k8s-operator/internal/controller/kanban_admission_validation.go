/*
Copyright 2026 Google LLC

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
	"fmt"
	"reflect"

	corev1 "k8s.io/api/core/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	"k8s.io/apimachinery/pkg/util/validation/field"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// conditionReasonInvalidKanbanAdmission reports that the resolved
	// kanban.admission block sets memoryBudgetMiB below workerRssCapMiB so a
	// single worker's RSS cap exceeds the pod's admission budget.
	conditionReasonInvalidKanbanAdmission = "InvalidKanbanAdmission"

	// kanbanAdmissionDegradedMsgFmt wraps the bounded field refusal for the
	// PlatformAgent Degraded status condition.
	kanbanAdmissionDegradedMsgFmt = "Invalid kanban.admission configuration (%s); the single-worker RSS cap exceeds the pod's memory admission budget until corrected" // #nosec G101 -- Condition message, not a credential

	// kanbanAdmissionBudgetBelowCapFmt,
	// kanbanAdmissionDerivedBudgetBelowCapFmt, and
	// kanbanAdmissionCapExceedsBudgetFmt name the conflicting figures and
	// actionable fixes so both admission rejections and Degraded conditions
	// point to the mismatched field.
	kanbanAdmissionBudgetBelowCapFmt           = "memoryBudgetMiB (%d MiB) is below workerRssCapMiB (%d MiB), so a single worker's RSS cap exceeds the pod's admission budget (leaving zero worker spawn headroom at sub-%dMiB budgets); raise memoryBudgetMiB to at least %d or lower workerRssCapMiB"                                                                      // #nosec G101 -- Error message format, not a credential
	kanbanAdmissionDerivedBudgetBelowCapFmt    = "%s (%s -> %d MiB) yields memoryBudgetMiB below workerRssCapMiB (%d MiB), so a single worker's RSS cap exceeds the pod's admission budget (leaving zero worker spawn headroom at sub-%dMiB budgets); raise spec.deployment.resources memory to at least %dMi, set spec.harness.tuning.admission.memoryBudgetMiB to at least %d, or lower workerRssCapMiB" // #nosec G101 -- Error message format, not a credential
	kanbanAdmissionCapExceedsBudgetFmt         = "workerRssCapMiB (%d MiB) exceeds effective memoryBudgetMiB (%d MiB), so a single worker's RSS cap exceeds the pod's admission budget; set spec.harness.tuning.admission.memoryBudgetMiB to at least %d (or spec.deployment.resources memory to at least %dMi) or lower workerRssCapMiB"                             // #nosec G101 -- Error message format, not a credential
	kanbanAdmissionCapExceedsExplicitBudgetFmt = "workerRssCapMiB (%d MiB) exceeds explicit memoryBudgetMiB (%d MiB), so a single worker's RSS cap exceeds the pod's admission budget; lower spec.harness.tuning.admission.workerRssCapMiB to at most %d or raise spec.harness.tuning.admission.memoryBudgetMiB to at least %d"                                     // #nosec G101 -- Error message format, not a credential
)

var (
	admissionMemoryBudgetPath    = field.NewPath("spec", "harness", "tuning", "admission", "memoryBudgetMiB")
	admissionWorkerRssCapPath    = field.NewPath("spec", "harness", "tuning", "admission", "workerRssCapMiB")
	deploymentRequestsMemoryPath = field.NewPath("spec", "deployment", "resources", "requests", "memory")
	deploymentLimitsMemoryPath   = field.NewPath("spec", "deployment", "resources", "limits", "memory")
)

// ValidateKanbanAdmission rejects configurations where resolved
// kanban.admission sets memoryBudgetMiB below workerRssCapMiB, which allows
// a single worker's RSS cap to exceed the pod's entire memory admission budget
// (and leaves zero spawn headroom once the gateway idles when under 1536 MiB).
//
// Checked unconditionally (failClosed only governs unreadable counters, while
// workerRssCapMiB and memoryBudgetMiB act whenever counters are readable).
// Checks both explicit spec.harness.tuning.admission.memoryBudgetMiB overrides
// and budgets derived from spec.deployment.resources against the effective
// workerRssCapMiB, attributing the error to the sub-default field.
func ValidateKanbanAdmission(agent *agentv1alpha1.PlatformAgent) field.ErrorList {
	if agent == nil {
		return nil
	}
	resolved := resolveKanbanAdmission(agent)
	if resolved.MemoryBudgetMiB >= resolved.WorkerRssCapMiB {
		return nil
	}

	tuning := agentTuning(agent)
	hasExplicitBudget := tuning != nil && tuning.Admission != nil && tuning.Admission.MemoryBudgetMiB != nil

	// Only blame an explicit or resource-derived memoryBudgetMiB when the
	// budget itself sits below the default worker RSS cap; when the budget is
	// at or above default cap (e.g. default 4Gi or 6Gi) and workerRssCapMiB
	// was raised past it, attribute the error to workerRssCapMiB.
	if resolved.MemoryBudgetMiB < defaultAdmissionWorkerRssCapMiB {
		if hasExplicitBudget {
			msg := fmt.Sprintf(kanbanAdmissionBudgetBelowCapFmt,
				resolved.MemoryBudgetMiB, resolved.WorkerRssCapMiB, defaultAdmissionWorkerRssCapMiB, resolved.WorkerRssCapMiB)
			return field.ErrorList{field.Invalid(admissionMemoryBudgetPath, *tuning.Admission.MemoryBudgetMiB, msg)}
		}

		if agent.Spec.Deployment != nil && agent.Spec.Deployment.Resources != nil {
			res := agent.Spec.Deployment.Resources
			if q, ok := res.Requests[corev1.ResourceMemory]; ok {
				msg := fmt.Sprintf(kanbanAdmissionDerivedBudgetBelowCapFmt,
					deploymentRequestsMemoryPath.String(), q.String(), resolved.MemoryBudgetMiB, resolved.WorkerRssCapMiB,
					defaultAdmissionWorkerRssCapMiB, resolved.WorkerRssCapMiB, resolved.WorkerRssCapMiB)
				return field.ErrorList{field.Invalid(deploymentRequestsMemoryPath, q.String(), msg)}
			}
			if q, ok := res.Limits[corev1.ResourceMemory]; ok {
				msg := fmt.Sprintf(kanbanAdmissionDerivedBudgetBelowCapFmt,
					deploymentLimitsMemoryPath.String(), q.String(), resolved.MemoryBudgetMiB, resolved.WorkerRssCapMiB,
					defaultAdmissionWorkerRssCapMiB, resolved.WorkerRssCapMiB, resolved.WorkerRssCapMiB)
				return field.ErrorList{field.Invalid(deploymentLimitsMemoryPath, q.String(), msg)}
			}
		}
	}

	if hasExplicitBudget {
		msg := fmt.Sprintf(kanbanAdmissionCapExceedsExplicitBudgetFmt,
			resolved.WorkerRssCapMiB, resolved.MemoryBudgetMiB, resolved.MemoryBudgetMiB, resolved.WorkerRssCapMiB)
		return field.ErrorList{field.Invalid(admissionWorkerRssCapPath, resolved.WorkerRssCapMiB, msg)}
	}

	msg := fmt.Sprintf(kanbanAdmissionCapExceedsBudgetFmt,
		resolved.WorkerRssCapMiB, resolved.MemoryBudgetMiB, resolved.WorkerRssCapMiB, resolved.WorkerRssCapMiB)
	return field.ErrorList{field.Invalid(admissionWorkerRssCapPath, resolved.WorkerRssCapMiB, msg)}
}

// KanbanAdmissionInputsEqual reports whether two PlatformAgent specs carry the
// same inputs to resolveKanbanAdmission (spec.deployment.resources and
// spec.harness.tuning.admission). The admission webhook uses this on updates so
// an existing CR written before admission validation can still receive
// unrelated updates with a warning rather than being deadlocked.
func KanbanAdmissionInputsEqual(a, b *agentv1alpha1.PlatformAgent) bool {
	return apiequality.Semantic.DeepEqual(deploymentResources(a), deploymentResources(b)) &&
		reflect.DeepEqual(admissionSpec(a), admissionSpec(b))
}

func deploymentResources(agent *agentv1alpha1.PlatformAgent) *corev1.ResourceRequirements {
	if agent == nil || agent.Spec.Deployment == nil {
		return nil
	}
	return agent.Spec.Deployment.Resources
}

func admissionSpec(agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.AdmissionSpec {
	tuning := agentTuning(agent)
	if tuning == nil {
		return nil
	}
	return tuning.Admission
}

// kanbanAdmissionRefusal returns the bounded refusal string for status
// conditions and Warning Events when the CR sets effective memoryBudgetMiB
// below workerRssCapMiB, or "" when valid.
func kanbanAdmissionRefusal(agent *agentv1alpha1.PlatformAgent) string {
	return boundCredentialProxyRefusal(ValidateKanbanAdmission(agent))
}
