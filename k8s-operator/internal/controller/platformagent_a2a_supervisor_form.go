package controller

import (
	"context"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
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

// a2aSupervisorReader is one workload whose container reads a profile task's
// terminal, and the image this operator renders for that container from its
// own release defaults.
type a2aSupervisorReader struct {
	name      string
	container string
	image     string
	// optional readers that aren't deployed don't hold the gate: one
	// rendered later is this release's, which reads both forms.
	optional bool
}

// a2aProfileSupervisorReaders are the workloads whose containers read a
// profile task's terminal: the A2A gateway, the agent pod's hermes-bridge (a
// Deployment, or a StatefulSet for a multi-replica agent on RWO storage), and
// the MCP server when its door is on.
func a2aProfileSupervisorReaders(agent *agentv1alpha1.PlatformAgent) []a2aSupervisorReader {
	readers := []a2aSupervisorReader{
		{name: a2aGatewayName(agent), image: a2aGatewayImage()},
		{name: agent.Name + "-gateway", container: a2aBridgeContainerName, image: a2aBridgeImage(defaultPlatformAgentImage())},
	}
	if a2aMCPDoorEnabled() {
		readers = append(readers, a2aSupervisorReader{name: a2aMCPName(agent), image: a2aConsoleImage(), optional: true})
	}
	return readers
}

// templateRunsImage reports whether spec has the container (any container when
// name is empty) on image.
func templateRunsImage(spec corev1.PodSpec, name, image string) bool {
	for _, c := range spec.Containers {
		if (name == "" || c.Name == name) && c.Image == image {
			return true
		}
	}
	return false
}

// a2aStatefulSetFullyOnTemplate is a2aDeploymentFullyOnTemplate for a
// StatefulSet: every replica on the update revision and ready.
func a2aStatefulSetFullyOnTemplate(sts *appsv1.StatefulSet) bool {
	desired := int32(1)
	if sts.Spec.Replicas != nil {
		desired = *sts.Spec.Replicas
	}
	return sts.Status.ObservedGeneration >= sts.Generation &&
		sts.Status.UpdatedReplicas == desired && sts.Status.Replicas == desired &&
		sts.Status.ReadyReplicas >= desired && sts.Status.CurrentRevision == sts.Status.UpdateRevision
}

// a2aSupervisorReadersOnTemplate reports whether every profile-terminal reader
// runs the image this operator renders for it and has finished rolling it out,
// and names the first that doesn't. Comparing the live template's image, not
// only the rollout, keeps a reader from an older release that happens to be
// fully rolled out (a pass that failed before re-applying it) from opening the
// gate; an install that pins an older agent image, and so an older bridge,
// stays on the five-token form.
func (r *PlatformAgentReconciler) a2aSupervisorReadersOnTemplate(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (bool, string, error) {
	for _, rd := range a2aProfileSupervisorReaders(agent) {
		key := client.ObjectKey{Namespace: agent.Namespace, Name: rd.name}
		dep := &appsv1.Deployment{}
		err := r.a2aReader().Get(ctx, key, dep)
		switch {
		case err == nil:
			if !templateRunsImage(dep.Spec.Template.Spec, rd.container, rd.image) || !a2aDeploymentFullyOnTemplate(dep) {
				return false, rd.name, nil
			}
			continue
		case !apierrors.IsNotFound(err):
			return false, rd.name, err
		}
		sts := &appsv1.StatefulSet{}
		if err := r.a2aReader().Get(ctx, key, sts); err != nil {
			if apierrors.IsNotFound(err) {
				if rd.optional {
					continue
				}
				return false, rd.name, nil
			}
			return false, rd.name, err
		}
		if !templateRunsImage(sts.Spec.Template.Spec, rd.container, rd.image) || !a2aStatefulSetFullyOnTemplate(sts) {
			return false, rd.name, nil
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
