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
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
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
// discordBotSecret is the hand-made Secret that gives the A2A gateway a chat
// backend; without it (or the inject flag) a next install's gateway is
// withheld on purpose (a2aGatewayBackend), which is the point of the tests
// below that leave it out.
func discordBotSecret(agent *agentv1alpha1.PlatformAgent) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: a2aDiscordBotSecretName, Namespace: agent.Namespace},
		Data:       map[string][]byte{"token": []byte("test-token")},
	}
}

func a2aNATS(agent *agentv1alpha1.PlatformAgent, ready int32) *appsv1.StatefulSet {
	return &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSName(agent), Namespace: agent.Namespace},
		Status:     appsv1.StatefulSetStatus{ReadyReplicas: ready},
	}
}

func a2aCallout(agent *agentv1alpha1.PlatformAgent, ready int32) *appsv1.Deployment {
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutName(agent), Namespace: agent.Namespace},
		Status:     appsv1.DeploymentStatus{ReadyReplicas: ready},
	}
}

// a2aProvisionJob is the provisioning Job under the digest name the render
// gives it, complete or still running.
func a2aProvisionJob(agent *agentv1alpha1.PlatformAgent, complete bool) *batchv1.Job {
	job := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: buildA2AProvisionJob(agent).Name, Namespace: agent.Namespace}}
	if complete {
		job.Status.Conditions = []batchv1.JobCondition{{Type: batchv1.JobComplete, Status: corev1.ConditionTrue}}
	}
	return job
}

// a2aStackUp is everything a next install renders beside the gateway, all
// ready, plus the backend Secret that makes the gateway expected.
func a2aStackUp(agent *agentv1alpha1.PlatformAgent) []client.Object {
	return []client.Object{discordBotSecret(agent), a2aNATS(agent, 1), a2aCallout(agent, 1), a2aProvisionJob(agent, true)}
}

// a2aStateFrom is the provision state reconcileA2A would have handed the status
// writer this pass: the Job's digest name, and done when the Job in the fake
// client reports Complete. The status writer no longer reads the Job itself.
func a2aStateFrom(t *testing.T, ctx context.Context, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent) a2aProvisionState {
	t.Helper()
	cl := r.Client
	if !a2aStackRendering(agent) {
		return a2aProvisionState{}
	}
	state := a2aProvisionState{jobName: buildA2AProvisionJob(agent).Name}
	// The gateway decision the render would have made: dark when the
	// Deployment is absent and the install has no backend.
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, &appsv1.Deployment{}); errors.IsNotFound(err) {
		configured, why, berr := r.a2aGatewayBackend(ctx, agent)
		if berr != nil {
			t.Fatal(berr)
		}
		if !configured {
			state.gatewayDark, state.gatewayDarkReason = true, why
		}
	}
	job := &batchv1.Job{}
	if err := cl.Get(ctx, types.NamespacedName{Name: state.jobName, Namespace: agent.Namespace}, job); err != nil {
		return state
	}
	for _, c := range job.Status.Conditions {
		if c.Type == batchv1.JobComplete && c.Status == corev1.ConditionTrue {
			state.done = true
		}
	}
	return state
}

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
	phase, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent), a2aStateFrom(t, ctx, r, agent))
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

	workloads, _, err := r.readSplitWorkloads(context.Background(), agent, a2aProvisionState{})
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

// The A2A gateway is the fourth workload, on the installs that render one. It
// belongs here for the same reason the shell and the broker do — a next install
// without it serves no A2A request, and the agent gateway's own readiness says
// nothing about that — and for one the other two do not have: the operator
// withholds it deliberately, in a2aGatewayWaitsForCallout, while the auth
// callout is short of serving. These tests are what a reader sees during that
// hold.

// splitReadinessNextAgent is the same CR on an install that renders the A2A
// stack.
func splitReadinessNextAgent() *agentv1alpha1.PlatformAgent {
	agent := splitReadinessAgent()
	agent.Spec.Mode = ptr.To(string(ModeNext))
	return agent
}

func a2aGatewayWorkload(agent *agentv1alpha1.PlatformAgent, ready int32) *appsv1.Deployment {
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: a2aGatewayName(agent), Namespace: agent.Namespace},
		Status:     appsv1.DeploymentStatus{ReadyReplicas: ready},
	}
}

// TestANextInstallCountsItsA2AGateway: the full set, and the sentence says so.
func TestANextInstallCountsItsA2AGateway(t *testing.T) {
	agent := splitReadinessNextAgent()
	objects := append(a2aStackUp(agent), readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1), a2aGatewayWorkload(agent, 1))
	phase, msg := settleStatus(t, agent, objects...)

	if phase != "Ready" {
		t.Errorf("got phase %q, want Ready: every workload the mode renders has a ready replica", phase)
	}
	if want := "Gateway, shell sandbox, credential broker, NATS, auth callout, bus provisioning and A2A gateway are all ready"; msg != want {
		t.Errorf("got message %q, want %q", msg, want)
	}
}

// TestAWithheldA2AGatewayIsNotReady is the one that matters. The creation gate
// holds the A2A gateway until one callout replica is ready on the current spec
// (a2aCalloutCanServeANewGateway), which on a callout with no such replica is
// indefinite. Before this, the CR read Ready: True the whole time with no
// dispatcher in the namespace and nothing saying the absent gateway was the
// consequence. The hold is still the behaviour; what changes is that the phase
// admits it and the message names the object.
func TestAWithheldA2AGatewayIsNotReady(t *testing.T) {
	agent := splitReadinessNextAgent()
	objects := append(a2aStackUp(agent), readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1))
	phase, msg := settleStatus(t, agent, objects...)

	if phase != "Provisioning" {
		t.Errorf("got phase %q, want Provisioning: the install renders an A2A gateway and has none", phase)
	}
	if want := "Waiting for Deployment test-agent-a2a-gateway to become ready"; msg != want {
		t.Errorf("got message %q, want %q", msg, want)
	}
}

// TestATodayInstallDoesNotWaitOnAnA2AGateway keeps the dark stack dark. A today
// install renders no A2A gateway, so requiring one would hold every one of them
// at Provisioning forever.
func TestATodayInstallDoesNotWaitOnAnA2AGateway(t *testing.T) {
	agent := splitReadinessAgent()
	phase, msg := settleStatus(t, agent, readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1))

	if phase != "Ready" {
		t.Errorf("got phase %q, want Ready: a today install has no A2A gateway to wait on", phase)
	}
	if want := "Gateway, shell sandbox and credential broker are all ready"; msg != want {
		t.Errorf("got message %q, want %q", msg, want)
	}
}

// TestVersionSkewDoesNotAddASecondReasonToHoldReady. An unrecognized mode leaves
// the A2A objects frozen rather than reconciled, on a CR the reconciler is
// already reporting Degraded/ModeNotRecognized. Counting a gateway that nothing
// is reconciling would report the freeze as a fault of its own, which is why
// this reads a2aStackRendering and not a2aAgentSurface.
func TestVersionSkewDoesNotAddASecondReasonToHoldReady(t *testing.T) {
	agent := splitReadinessAgent()
	agent.Spec.Mode = ptr.To("next-but-newer")
	phase, msg := settleStatus(t, agent, readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1))

	if phase != "Ready" {
		t.Errorf("got phase %q, want Ready: the skew is the CR's problem, not a missing workload", phase)
	}
	if want := "Gateway, shell sandbox and credential broker are all ready"; msg != want {
		t.Errorf("got message %q, want %q", msg, want)
	}
}

// ---- the rest of the next stack, and a gateway dark on purpose (#1660, #1701)

// TestANextInstallWithNoChatBackendIsReadyAndSaysWhy: no discord-bot Secret
// and no door armed means the gateway is withheld on purpose, not missing.
// Ready is true on the rest of the stack, the message says the gateway is
// not rendered, and the A2AGateway condition carries the remedy.
func TestANextInstallWithNoChatBackendIsReadyAndSaysWhy(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := splitReadinessNextAgent()
	phase, msg := settleStatus(t, agent,
		readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1),
		a2aNATS(agent, 1), a2aCallout(agent, 1), a2aProvisionJob(agent, true))

	if phase != "Ready" {
		t.Errorf("got phase %q, want Ready: the gateway is withheld on purpose and everything else is up", phase)
	}
	if want := "Gateway, shell sandbox, credential broker, NATS, auth callout and bus provisioning are all ready; " +
		"the A2A gateway is not rendered (no chat backend, see the A2AGateway condition)"; msg != want {
		t.Errorf("got message %q, want %q", msg, want)
	}
	cond := meta.FindStatusCondition(agent.Status.Conditions, a2aGatewayConditionType)
	if cond == nil {
		t.Fatal("no A2AGateway condition: the operator withheld the gateway and said nothing")
	}
	if cond.Status != metav1.ConditionFalse || cond.Reason != a2aGatewayDarkReason {
		t.Errorf("A2AGateway = %s/%s, want False/%s", cond.Status, cond.Reason, a2aGatewayDarkReason)
	}
	for _, want := range []string{a2aDiscordBotSecretName, a2aInjectBackendEnvVar} {
		if !strings.Contains(cond.Message, want) {
			t.Errorf("the condition's message does not name the remedy %q: %q", want, cond.Message)
		}
	}
}

// TestTheInjectDoorCountsAsAChatBackend: the eval install arms the door and
// has no Discord Secret; its gateway is expected, and its absence is the
// callout gate holding, not a dark gateway.
func TestTheInjectDoorCountsAsAChatBackend(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "true")
	agent := splitReadinessNextAgent()
	phase, msg := settleStatus(t, agent,
		readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1),
		a2aNATS(agent, 1), a2aCallout(agent, 1), a2aProvisionJob(agent, true))
	if phase != "Provisioning" {
		t.Errorf("got phase %q, want Provisioning: the door is a backend, so the missing gateway is being waited on", phase)
	}
	if want := "Waiting for Deployment test-agent-a2a-gateway to become ready"; msg != want {
		t.Errorf("got message %q, want %q", msg, want)
	}
	if meta.FindStatusCondition(agent.Status.Conditions, a2aGatewayConditionType) != nil {
		t.Error("an A2AGateway condition was written for a gateway that is expected")
	}
}

// TestTheDarkGatewayConditionClearsWhenABackendAppears: the condition is
// removed on the pass that finds the Secret, on the EventWatcher pattern.
func TestTheDarkGatewayConditionClearsWhenABackendAppears(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := splitReadinessNextAgent()
	agent.Status.Conditions = []metav1.Condition{{
		Type: a2aGatewayConditionType, Status: metav1.ConditionFalse, Reason: a2aGatewayDarkReason, Message: "stale",
		LastTransitionTime: metav1.Now(),
	}}
	objects := append(a2aStackUp(agent), readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1), a2aGatewayWorkload(agent, 1))
	settleStatus(t, agent, objects...)
	if meta.FindStatusCondition(agent.Status.Conditions, a2aGatewayConditionType) != nil {
		t.Error("the A2AGateway condition survived the backend appearing and the gateway coming up")
	}
}

// TestReadyWaitsOnEveryWorkloadTheModeRenders: NATS, the callout and the
// provisioning Job each hold Ready on their own and are named when they do.
func TestReadyWaitsOnEveryWorkloadTheModeRenders(t *testing.T) {
	agent := splitReadinessNextAgent()
	base := func() []client.Object {
		return []client.Object{discordBotSecret(agent), readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1), a2aGatewayWorkload(agent, 1)}
	}
	cases := []struct {
		name    string
		objects []client.Object
		want    string
	}{
		{"nats not ready", append(base(), a2aNATS(agent, 0), a2aCallout(agent, 1), a2aProvisionJob(agent, true)),
			"Waiting for StatefulSet " + a2aNATSName(agent) + " to become ready"},
		{"callout not ready", append(base(), a2aNATS(agent, 1), a2aCallout(agent, 0), a2aProvisionJob(agent, true)),
			"Waiting for Deployment " + a2aCalloutName(agent) + " to become ready"},
		{"job not complete", append(base(), a2aNATS(agent, 1), a2aCallout(agent, 1), a2aProvisionJob(agent, false)),
			"Waiting for Job " + buildA2AProvisionJob(agent).Name + " to become ready"},
		{"job absent", append(base(), a2aNATS(agent, 1), a2aCallout(agent, 1)),
			"Waiting for Job " + buildA2AProvisionJob(agent).Name + " to become ready"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a := splitReadinessNextAgent()
			phase, msg := settleStatus(t, a, tc.objects...)
			if phase != "Provisioning" {
				t.Errorf("got phase %q, want Provisioning", phase)
			}
			if msg != tc.want {
				t.Errorf("got message %q, want %q", msg, tc.want)
			}
		})
	}
}

// TestAProvisionedBusStaysReadyThroughTheJobReRun: the finished Job's TTL
// removes it a day after completion and the render builds it again; a CR
// that already read Ready must not drop to Provisioning for the minute the
// re-run takes. A CR that has never been Ready still waits on it.
func TestAProvisionedBusStaysReadyThroughTheJobReRun(t *testing.T) {
	agent := splitReadinessNextAgent()
	agent.Status.Conditions = []metav1.Condition{{Type: busProvisionedConditionType, Status: metav1.ConditionTrue, Reason: busProvisionedReason, LastTransitionTime: metav1.Now()}}
	objects := []client.Object{discordBotSecret(agent), readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1),
		a2aGatewayWorkload(agent, 1), a2aNATS(agent, 1), a2aCallout(agent, 1), a2aProvisionJob(agent, false)}
	phase, _ := settleStatus(t, agent, objects...)
	if phase != "Ready" {
		t.Errorf("got phase %q, want Ready: the bus was provisioned once and the Job is only re-running", phase)
	}
	if !busProvisioned(agent) {
		t.Error("the re-run removed the provisioned-once record; the next pass would count the Job again")
	}
}

// TestAReadyInheritedFromAnOlderOperatorDoesNotLatchTheJob: the latch is keyed
// on a condition only this code writes. An install upgraded from an operator
// that never counted the Job arrives with Ready=True/Reconciled and an
// unprovisioned bus (#1701's state), and that Ready must not stand in for a
// completion nobody saw.
func TestAReadyInheritedFromAnOlderOperatorDoesNotLatchTheJob(t *testing.T) {
	agent := splitReadinessNextAgent()
	agent.Status.Conditions = []metav1.Condition{{Type: "Ready", Status: metav1.ConditionTrue, Reason: "Reconciled",
		Message: "Gateway, shell sandbox, credential broker and A2A gateway are all ready", LastTransitionTime: metav1.Now()}}
	objects := []client.Object{discordBotSecret(agent), readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1),
		a2aGatewayWorkload(agent, 1), a2aNATS(agent, 1), a2aCallout(agent, 1), a2aProvisionJob(agent, false)}
	phase, msg := settleStatus(t, agent, objects...)
	if phase != "Provisioning" {
		t.Fatalf("got phase %q, want Provisioning: the inherited Ready says nothing about the bus and the Job is not complete", phase)
	}
	if !strings.Contains(msg, "Job "+buildA2AProvisionJob(agent).Name) {
		t.Errorf("the message does not name the Job Ready waits on: %q", msg)
	}
	if busProvisioned(agent) {
		t.Error("BusProvisioned was written without a completion")
	}
}

// TestTheFirstCompletionRecordsBusProvisioned: the pass that sees the Job
// complete is Ready and writes the record; the record then carries a later
// pass whose Job is re-running.
func TestTheFirstCompletionRecordsBusProvisioned(t *testing.T) {
	agent := splitReadinessNextAgent()
	objects := []client.Object{discordBotSecret(agent), readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1),
		a2aGatewayWorkload(agent, 1), a2aNATS(agent, 1), a2aCallout(agent, 1), a2aProvisionJob(agent, true)}
	phase, _ := settleStatus(t, agent, objects...)
	if phase != "Ready" {
		t.Fatalf("got phase %q, want Ready on the pass that sees the Job complete", phase)
	}
	cond := meta.FindStatusCondition(agent.Status.Conditions, busProvisionedConditionType)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != busProvisionedReason {
		t.Fatalf("the completion was not recorded: %+v", cond)
	}
	if !strings.Contains(cond.Message, buildA2AProvisionJob(agent).Name) {
		t.Errorf("the record does not name the Job that provisioned the bus: %q", cond.Message)
	}

	// The TTL removed the Job and create-if-absent built a new one that has
	// not finished: still Ready, on the record alone.
	rerun := splitReadinessNextAgent()
	rerun.Status.Conditions = append([]metav1.Condition(nil), agent.Status.Conditions...)
	objects = []client.Object{discordBotSecret(rerun), readyGateway(rerun), shellSandbox(rerun, 1), credentialBroker(rerun, 1),
		a2aGatewayWorkload(rerun, 1), a2aNATS(rerun, 1), a2aCallout(rerun, 1), a2aProvisionJob(rerun, false)}
	if phase, msg := settleStatus(t, rerun, objects...); phase != "Ready" {
		t.Errorf("got phase %q (%q) on the re-run, want Ready", phase, msg)
	}
}

// TestBusProvisionedIsPersistedWhenNothingElseChanged: the record has its
// own term in the unchanged-status early return. Without one, a pass whose
// phase, message and generation already match (a same-build restart on an
// install whose record was lost, a Ready written by hand) would see the Job
// complete and skip the write, and the next TTL re-run would count the Job
// again.
func TestBusProvisionedIsPersistedWhenNothingElseChanged(t *testing.T) {
	agent := splitReadinessNextAgent()
	agent.Generation = 3
	agent.Status.Phase = "Ready"
	agent.Status.ObservedGeneration = 3
	agent.Status.Conditions = []metav1.Condition{{Type: "Ready", Status: metav1.ConditionTrue, Reason: "Reconciled",
		Message:            "Gateway, shell sandbox, credential broker, NATS, auth callout, bus provisioning and A2A gateway are all ready",
		ObservedGeneration: 3, LastTransitionTime: metav1.Now()}}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, discordBotSecret(agent), readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1),
			a2aGatewayWorkload(agent, 1), a2aNATS(agent, 1), a2aCallout(agent, 1), a2aProvisionJob(agent, true)).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	ctx := context.Background()
	// Settle the rest of the status the writer compares, then take the
	// record away and persist that, so the record is the only thing the
	// next pass finds different.
	if _, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent), a2aStateFrom(t, ctx, r, agent)); err != nil {
		t.Fatal(err)
	}
	meta.RemoveStatusCondition(&agent.Status.Conditions, busProvisionedConditionType)
	if err := cl.Status().Update(ctx, agent); err != nil {
		t.Fatal(err)
	}
	ready := meta.FindStatusCondition(agent.Status.Conditions, "Ready")
	if ready == nil || ready.Status != metav1.ConditionTrue {
		t.Fatalf("precondition: the settled CR is not Ready: %+v", ready)
	}

	phase, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent), a2aStateFrom(t, ctx, r, agent))
	if err != nil {
		t.Fatal(err)
	}
	if phase != "Ready" {
		t.Fatalf("got phase %q, want Ready", phase)
	}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}, stored); err != nil {
		t.Fatal(err)
	}
	if !busProvisioned(stored) {
		t.Error("BusProvisioned was not persisted on a pass where nothing else about the status changed")
	}
}

// TestTheStatusWriterReportsThePassesOwnGatewayDecision: the render decided
// the gateway was dark; a Secret that landed after that decision and before
// the status write does not make the writer report a gateway the pass did
// not create. Same pass, one answer.
func TestTheStatusWriterReportsThePassesOwnGatewayDecision(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := splitReadinessNextAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, discordBotSecret(agent), readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1),
			a2aNATS(agent, 1), a2aCallout(agent, 1), a2aProvisionJob(agent, true)).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	ctx := context.Background()
	decided := a2aProvisionState{done: true, jobName: buildA2AProvisionJob(agent).Name, gatewayDark: true, gatewayDarkReason: "no chat backend is configured (decided before the Secret landed)"}
	phase, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent), decided)
	if err != nil {
		t.Fatal(err)
	}
	if phase != "Ready" {
		t.Fatalf("got phase %q, want Ready: the pass withheld the gateway on purpose", phase)
	}
	cond := meta.FindStatusCondition(agent.Status.Conditions, a2aGatewayConditionType)
	if cond == nil || cond.Message != decided.gatewayDarkReason {
		t.Fatalf("the writer did not report the pass's own decision: %+v", cond)
	}
}

// TestBusProvisionedLeavesWithTheStack: a flip to today tears the bus down,
// and the record goes with it rather than describing a bus that is gone.
func TestBusProvisionedLeavesWithTheStack(t *testing.T) {
	agent := splitReadinessAgent()
	agent.Status.Conditions = []metav1.Condition{{Type: busProvisionedConditionType, Status: metav1.ConditionTrue, Reason: busProvisionedReason, LastTransitionTime: metav1.Now()}}
	settleStatus(t, agent, readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1))
	if busProvisioned(agent) {
		t.Error("a today install still carries BusProvisioned")
	}
}

// TestTheDarkGatewayConditionClearsEvenWhenTheReadyMessageDoesNot: the flip
// from dark to rendered can leave phase and message unchanged (another
// workload holding Provisioning both passes); the condition still clears.
func TestTheDarkGatewayConditionClearsEvenWhenTheReadyMessageDoesNot(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := splitReadinessNextAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, readyGateway(agent), shellSandbox(agent, 0), credentialBroker(agent, 1),
			a2aNATS(agent, 1), a2aCallout(agent, 1), a2aProvisionJob(agent, true)).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	ctx := context.Background()
	if _, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent), a2aStateFrom(t, ctx, r, agent)); err != nil {
		t.Fatal(err)
	}
	if meta.FindStatusCondition(agent.Status.Conditions, a2aGatewayConditionType) == nil {
		t.Fatal("precondition: the first pass did not write the dark-gateway condition")
	}
	// A backend appears and the gateway comes up; the sandbox is still down,
	// so phase and message are what they were.
	if err := cl.Create(ctx, discordBotSecret(agent)); err != nil {
		t.Fatal(err)
	}
	if err := cl.Create(ctx, a2aGatewayWorkload(agent, 1)); err != nil {
		t.Fatal(err)
	}
	if _, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent), a2aStateFrom(t, ctx, r, agent)); err != nil {
		t.Fatal(err)
	}
	persisted := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), persisted); err != nil {
		t.Fatal(err)
	}
	if meta.FindStatusCondition(persisted.Status.Conditions, a2aGatewayConditionType) != nil {
		t.Error("the NoChatBackend condition survived on a CR whose gateway is rendered and running")
	}
}
