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
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// What the CR says while spec.deployment.scaleToZero holds the gateway at zero.
// Before these tests a paused install read Ready=False / Provisioning, the same
// words as a rollout that never finishes, so nothing on the CR told an operator
// which of the two they were looking at.

// scaledToZeroAgent is splitReadinessAgent with the pause asked for.
func scaledToZeroAgent() *agentv1alpha1.PlatformAgent {
	agent := splitReadinessAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{ScaleToZero: ptr.To(true)}
	return agent
}

// drainedGateway is the gateway Deployment after the scale-down converged: the
// object exists, no replica is ready.
func drainedGateway(agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-gateway", Namespace: agent.Namespace},
		Status:     appsv1.DeploymentStatus{ReadyReplicas: 0},
	}
}

// crashLoopingSandboxPod is a shell sandbox pod the status scan names as a
// fault: a container waiting in CrashLoopBackOff.
func crashLoopingSandboxPod(agent *agentv1alpha1.PlatformAgent) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      shellSandboxName(agent) + "-0",
			Namespace: agent.Namespace,
			Labels:    shellSandboxSelector(agent),
		},
		Status: corev1.PodStatus{
			ContainerStatuses: []corev1.ContainerStatus{{
				Name:  "shell",
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: reasonCrashLoopBackOff}},
			}},
			Conditions: []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionTrue}},
		},
	}
}

func TestScaleToZeroReportsPausedNotProvisioning(t *testing.T) {
	agent := scaledToZeroAgent()
	phase, msg := settleStatus(t, agent, drainedGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1))

	if phase != "Paused" {
		t.Errorf("got phase %q, want Paused: the gateway is at zero because the spec asked for it", phase)
	}
	cond := meta.FindStatusCondition(agent.Status.Conditions, "Ready")
	if cond.Status != metav1.ConditionFalse {
		t.Errorf("Ready status = %q, want False: a paused agent answers nothing", cond.Status)
	}
	if cond.Reason != "ScaledToZero" {
		t.Errorf("Ready reason = %q, want ScaledToZero", cond.Reason)
	}
	if !strings.Contains(msg, "spec.deployment.scaleToZero") {
		t.Errorf("Ready message %q does not name the field that paused the install", msg)
	}
	if agent.Status.DeploymentStatus.ReadyReplicas != 0 {
		t.Errorf("deploymentStatus.readyReplicas = %d, want 0", agent.Status.DeploymentStatus.ReadyReplicas)
	}
}

// The pause is reported once the scale-down converged, not from the edit: a
// gateway replica still serving is still Ready, and the CR says so until the
// Deployment controller has taken it away.
func TestScaleToZeroWithAReplicaStillServingIsStillReady(t *testing.T) {
	agent := scaledToZeroAgent()
	phase, _ := settleStatus(t, agent, readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1))

	if phase != "Ready" {
		t.Errorf("got phase %q, want Ready: a replica is still serving while the scale-down drains", phase)
	}
}

// Clearing the field gives the normal phases back: Provisioning while the
// gateway comes up, Ready once it has.
func TestClearingScaleToZeroRestoresTheNormalPhase(t *testing.T) {
	agent := scaledToZeroAgent()
	if phase, _ := settleStatus(t, agent, drainedGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1)); phase != "Paused" {
		t.Fatalf("precondition: got phase %q, want Paused", phase)
	}

	agent.Spec.Deployment.ScaleToZero = nil
	phase, msg := settleStatus(t, agent, drainedGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1))
	if phase != "Provisioning" {
		t.Errorf("got phase %q after clearing the field with the gateway still at zero, want Provisioning", phase)
	}
	if want := "Waiting for deployment replicas to be ready"; msg != want {
		t.Errorf("got message %q, want %q", msg, want)
	}
	if cond := meta.FindStatusCondition(agent.Status.Conditions, "Ready"); cond.Reason == "ScaledToZero" {
		t.Errorf("Ready reason still ScaledToZero after the field was cleared")
	}

	phase, _ = settleStatus(t, agent, readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1))
	if phase != "Ready" {
		t.Errorf("got phase %q once the gateway is back, want Ready", phase)
	}
	if cond := meta.FindStatusCondition(agent.Status.Conditions, "Ready"); cond.Status != metav1.ConditionTrue || cond.Reason != "Reconciled" {
		t.Errorf("Ready = %s/%s once the gateway is back, want True/Reconciled", cond.Status, cond.Reason)
	}
}

// A fault the pod scan names still wins: a paused install whose sandbox is
// crash looping is broken as well as paused, and Degraded is the phase an
// operator acts on.
func TestAFaultOnAPausedInstallStillReadsDegraded(t *testing.T) {
	agent := scaledToZeroAgent()
	phase, msg := settleStatus(t, agent, drainedGateway(agent), shellSandbox(agent, 0), credentialBroker(agent, 1), crashLoopingSandboxPod(agent))

	if phase != "Degraded" {
		t.Errorf("got phase %q, want Degraded: the sandbox is crash looping whatever the gateway's replica count", phase)
	}
	if cond := meta.FindStatusCondition(agent.Status.Conditions, "Ready"); cond.Reason != reasonCrashLoopBackOff {
		t.Errorf("Ready reason = %q, want %q (message %q)", cond.Reason, reasonCrashLoopBackOff, msg)
	}
}

// Without the field a gateway at zero is still the start-up wait it always was.
func TestAGatewayAtZeroWithoutTheFieldIsProvisioning(t *testing.T) {
	agent := splitReadinessAgent()
	phase, _ := settleStatus(t, agent, drainedGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1))

	if phase != "Provisioning" {
		t.Errorf("got phase %q, want Provisioning: nobody asked for zero replicas", phase)
	}
	if cond := meta.FindStatusCondition(agent.Status.Conditions, "Ready"); cond.Reason != "Provisioning" {
		t.Errorf("Ready reason = %q, want Provisioning", cond.Reason)
	}
}

// A workload that keeps running under the pause but has no ready replica, and
// that the pod scan names no fault in (a failing readiness probe, a missing
// StatefulSet), is named in the message: the phase stays Paused, since the
// gateway is at zero on purpose, but "not failing" is not claimed over it.
func TestAPausedInstallNamesAWorkloadThatIsNotReady(t *testing.T) {
	agent := scaledToZeroAgent()
	phase, msg := settleStatus(t, agent, drainedGateway(agent), shellSandbox(agent, 0), credentialBroker(agent, 1))

	if phase != "Paused" {
		t.Errorf("got phase %q, want Paused: the gateway is at zero because the spec asked for it", phase)
	}
	if cond := meta.FindStatusCondition(agent.Status.Conditions, "Ready"); cond.Reason != "ScaledToZero" {
		t.Errorf("Ready reason = %q, want ScaledToZero", cond.Reason)
	}
	if !strings.Contains(msg, "StatefulSet test-agent-shell") {
		t.Errorf("Ready message %q does not name the sandbox that is not ready", msg)
	}
	if strings.Contains(msg, "not failing") {
		t.Errorf("Ready message %q claims the install is not failing while the sandbox has no ready replica", msg)
	}
}
