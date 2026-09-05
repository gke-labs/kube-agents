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
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// What Ready has to mean now that the agent is three Pods. The gateway holds no
// credential and runs no command, so a CR that reads Ready on the gateway alone
// is telling an operator the agent works when the shell it runs commands in may
// not exist. These tests are that claim, and the sentence a reader gets while it
// is not yet true.

// splitReadinessAgent is the CR these tests report on. Deliberately plainer than
// brokerPodAgent: no chat integration, because nothing here depends on the
// relays and their absence keeps the status under test to the three workloads.
func splitReadinessAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
}

func readyGateway(agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-gateway", Namespace: agent.Namespace},
		Status:     appsv1.DeploymentStatus{ReadyReplicas: 1},
	}
}

func shellSandbox(agent *agentv1alpha1.PlatformAgent, ready int32) *appsv1.StatefulSet {
	return &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{Name: shellSandboxName(agent), Namespace: agent.Namespace},
		Status:     appsv1.StatefulSetStatus{ReadyReplicas: ready},
	}
}

func credentialBroker(agent *agentv1alpha1.PlatformAgent, ready int32) *appsv1.Deployment {
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: credentialBrokerName(agent), Namespace: agent.Namespace},
		Status:     appsv1.DeploymentStatus{ReadyReplicas: ready},
	}
}

// settleStatus runs the status update against a fake holding exactly `objects`,
// and hands back the phase and the Ready condition's message.
func settleStatus(t *testing.T, agent *agentv1alpha1.PlatformAgent, objects ...client.Object) (string, string) {
	t.Helper()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(append([]client.Object{agent}, objects...)...).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}

	ctx := context.Background()
	phase, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent))
	if err != nil {
		t.Fatalf("updateStatusReady failed: %v", err)
	}
	cond := meta.FindStatusCondition(agent.Status.Conditions, "Ready")
	if cond == nil {
		t.Fatal("no Ready condition was written, so there is nothing for an operator to read")
	}
	return phase, cond.Message
}

// TestReadyMeansAllThreeWorkloads is the whole point of the gating: only the
// full set earns the phase.
func TestReadyMeansAllThreeWorkloads(t *testing.T) {
	agent := splitReadinessAgent()
	phase, msg := settleStatus(t, agent, readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1))

	if phase != "Ready" {
		t.Errorf("got phase %q, want Ready: all three workloads have a ready replica", phase)
	}
	if want := "Gateway, shell sandbox and credential broker are all ready"; msg != want {
		t.Errorf("got message %q, want %q", msg, want)
	}
}

// TestAGatewayWithoutItsShellIsNotReady is the regression the split introduced.
// Before it, the credential runtime was a native sidecar and a sandbox that
// could not start held the gateway Pod out of readiness; afterwards the gateway
// becomes Ready on its own while the model cannot run a single command.
func TestAGatewayWithoutItsShellIsNotReady(t *testing.T) {
	agent := splitReadinessAgent()
	phase, msg := settleStatus(t, agent, readyGateway(agent), credentialBroker(agent, 1))

	if phase != "Provisioning" {
		t.Errorf("got phase %q, want Provisioning: the agent has no shell to run commands in", phase)
	}
	if want := "Waiting for StatefulSet test-agent-shell to become ready"; msg != want {
		t.Errorf("got message %q, want %q", msg, want)
	}
}

// TestAGatewayWithoutItsBrokerIsNotReady is the other half. Here the shell
// exists and every credentialed command in it fails, because the Service it
// dials has no endpoints.
func TestAGatewayWithoutItsBrokerIsNotReady(t *testing.T) {
	agent := splitReadinessAgent()
	phase, msg := settleStatus(t, agent, readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 0))

	if phase != "Provisioning" {
		t.Errorf("got phase %q, want Provisioning: nothing in the agent can mint a credential", phase)
	}
	if want := "Waiting for Deployment test-agent-credential-proxy to become ready"; msg != want {
		t.Errorf("got message %q, want %q", msg, want)
	}
}

// TestTheMessageNamesEveryWorkloadItIsWaitingOn matters because the reader's
// next command is `kubectl describe` on whatever the message names. Naming one
// of two sends them back for a second round after they have fixed it.
func TestTheMessageNamesEveryWorkloadItIsWaitingOn(t *testing.T) {
	agent := splitReadinessAgent()
	phase, msg := settleStatus(t, agent, readyGateway(agent))

	if phase != "Provisioning" {
		t.Errorf("got phase %q, want Provisioning", phase)
	}
	want := "Waiting for StatefulSet test-agent-shell and Deployment test-agent-credential-proxy to become ready"
	if msg != want {
		t.Errorf("got message %q, want %q", msg, want)
	}
}

// TestAGatewayThatIsNotUpKeepsItsOwnMessage. The sentence above replaces the
// generic one only when the gateway is the workload that is fine; with nothing
// ready at all, pointing at the other two would bury the fact that the agent
// itself has not started.
func TestAGatewayThatIsNotUpKeepsItsOwnMessage(t *testing.T) {
	agent := splitReadinessAgent()
	gateway := readyGateway(agent)
	gateway.Status.ReadyReplicas = 0
	phase, msg := settleStatus(t, agent, gateway)

	if phase != "Provisioning" {
		t.Errorf("got phase %q, want Provisioning", phase)
	}
	if want := "Waiting for deployment replicas to be ready"; msg != want {
		t.Errorf("got message %q, want %q", msg, want)
	}
}

// TestReadSplitWorkloadsReportsAnAbsentObjectAsNotReady pins the read itself.
// NotFound is the ordinary state between applying the objects and the API server
// serving them back, so it has to read as not-ready rather than fail the
// reconcile — and the name still has to come back, because that is what the
// message is built from.
func TestReadSplitWorkloadsReportsAnAbsentObjectAsNotReady(t *testing.T) {
	agent := splitReadinessAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}

	workloads, err := r.readSplitWorkloads(context.Background(), agent)
	if err != nil {
		t.Fatalf("an absent workload is not a read failure: %v", err)
	}
	want := []splitWorkloadStatus{
		{name: "test-agent-shell", kind: "StatefulSet", ready: 0},
		{name: "test-agent-credential-proxy", kind: "Deployment", ready: 0},
	}
	if len(workloads) != len(want) {
		t.Fatalf("got %d workloads, want %d: %#v", len(workloads), len(want), workloads)
	}
	for i := range want {
		if workloads[i] != want[i] {
			t.Errorf("workload %d: got %#v, want %#v", i, workloads[i], want[i])
		}
	}
}
