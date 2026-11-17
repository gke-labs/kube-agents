package controller

import (
	"context"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Moving profile tasks' terminals to the six-token supervisor subject
// (a2a.tasks.p.<profile>.<task>.supervisor, #5301) is ordered so that nothing
// writes the new form before every reader that can run watches it:
//
//  1. The readers: the A2A gateway, the agent pod (its hermes-bridge sidecar
//     folds task ends) and, when rendered, the MCP server, each fully rolled
//     out on the template this operator renders. This release's images read
//     both forms (#5644), so a finished rollout means no older reader is left.
//  2. Then the provision Job writes the gateway relay durable's third filter,
//     so the relay delivers six-token terminals live, not only by heal.
//  3. Then, once that Job has succeeded, the dispatch config's
//     profileSupervisorForm is "profile", and the dispatcher writes the six-
//     token form. Its grant names both forms whatever the switch says, so the
//     switch never comes ahead of the grant.
//
// The order latches on the ProfileSupervisorForm condition: once step 3 is
// reached it stays reached, so a later release's gateway roll doesn't take the
// third filter away and switch the dispatcher back for the length of the roll.
// Only a rollback two releases, past #5644, could bring back a reader that
// doesn't watch the six-token form, which the N->N-1 rollback rule doesn't
// cover.
const (
	a2aProfileSupervisorFormCondition = "ProfileSupervisorForm"
	a2aProfileSupervisorFormReason    = "SixTokenSupervisorSubject"
	a2aProfileSupervisorFormValue     = "profile"
)

// a2aDeploymentFullyOnTemplate reports whether dep has finished rolling out its
// current template: the controller has observed this generation, every replica
// is on it and available, and no pod from an older template is left.
func a2aDeploymentFullyOnTemplate(dep *appsv1.Deployment) bool {
	desired := int32(1)
	if dep.Spec.Replicas != nil {
		desired = *dep.Spec.Replicas
	}
	return dep.Status.ObservedGeneration >= dep.Generation &&
		dep.Status.UpdatedReplicas == desired &&
		dep.Status.Replicas == desired &&
		dep.Status.AvailableReplicas >= desired
}

// a2aProfileSupervisorReaders are the Deployments whose containers read a
// profile task's terminal: the A2A gateway, the agent pod (hermes-bridge), and
// the MCP server when its door is on.
func a2aProfileSupervisorReaders(agent *agentv1alpha1.PlatformAgent) []string {
	names := []string{a2aGatewayName(agent), agent.Name + "-gateway"}
	if a2aMCPDoorEnabled() {
		names = append(names, a2aMCPName(agent))
	}
	return names
}

// a2aSupervisorReadersOnTemplate reports whether every profile-terminal reader
// is fully rolled out on its current template, and names the first that
// isn't. A reader not deployed yet isn't ready.
func (r *PlatformAgentReconciler) a2aSupervisorReadersOnTemplate(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (bool, string, error) {
	for _, name := range a2aProfileSupervisorReaders(agent) {
		dep := &appsv1.Deployment{}
		if err := r.a2aReader().Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: name}, dep); err != nil {
			if apierrors.IsNotFound(err) {
				return false, name, nil
			}
			return false, name, err
		}
		if !a2aDeploymentFullyOnTemplate(dep) {
			return false, name, nil
		}
	}
	return true, "", nil
}

// a2aProfileSupervisorFormFor says how far this pass takes the move: whether
// the provision Job renders the relay's third filter, and the dispatch config's
// profileSupervisorForm. A non-empty form is the latch wanted; the status
// writers save it (setProfileSupervisorFormCondition), as they save
// BusProvisioned, so a settled pass that changes nothing else still writes it.
func (r *PlatformAgentReconciler) a2aProfileSupervisorFormFor(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (thirdFilter bool, form string, err error) {
	latched := meta.IsStatusConditionTrue(agent.Status.Conditions, a2aProfileSupervisorFormCondition)
	if !latched {
		ready, _, err := r.a2aSupervisorReadersOnTemplate(ctx, agent)
		if err != nil || !ready {
			return false, "", err
		}
	}
	if latched {
		return true, a2aProfileSupervisorFormValue, nil
	}
	// Readers ready: the third filter renders. The switch waits for the
	// Job that applies it.
	job := a2aProvisionJobForm(agent, true)
	existing := &batchv1.Job{}
	if err := r.a2aReader().Get(ctx, client.ObjectKeyFromObject(job), existing); err != nil {
		if apierrors.IsNotFound(err) {
			return true, "", nil
		}
		return true, "", err
	}
	if existing.Status.Succeeded == 0 {
		return true, "", nil
	}
	return true, a2aProfileSupervisorFormValue, nil
}

// profileSupervisorFormConditionCurrent reports whether the CR already carries
// the latch as wanted. The latch is only ever added, so not wanting it asks
// for nothing.
func profileSupervisorFormConditionCurrent(agent *agentv1alpha1.PlatformAgent, want bool) bool {
	return !want || meta.IsStatusConditionTrue(agent.Status.Conditions, a2aProfileSupervisorFormCondition)
}

// setProfileSupervisorFormCondition adds the latch when wanted and absent. It
// never removes it.
func setProfileSupervisorFormCondition(agent *agentv1alpha1.PlatformAgent, want bool, now metav1.Time) {
	if !want || meta.IsStatusConditionTrue(agent.Status.Conditions, a2aProfileSupervisorFormCondition) {
		return
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type: a2aProfileSupervisorFormCondition, Status: metav1.ConditionTrue,
		Reason:             a2aProfileSupervisorFormReason,
		Message:            "every profile-task reader watches the six-token supervisor subject and the gateway relay filters it, so the dispatcher writes profile terminals there",
		ObservedGeneration: agent.Generation,
		LastTransitionTime: now,
	})
}
