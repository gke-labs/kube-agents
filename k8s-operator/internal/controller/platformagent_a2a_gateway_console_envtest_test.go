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

// The A2A gateway on every next install, against a real API server. The
// gateway starts on the console door alone when no chat backend is configured
// (A2A_CONSOLE_DOOR, a2aConsoleDoorEnvVar), so the operator runs it at one
// replica whatever the install's backends are, and a backend taken away
// leaves the same object running rather than scaling it to zero.
//
// Older operators darkened a gateway with no backend (#1660, #2481): withheld,
// or applied at zero replicas, with the A2AGateway condition (NoChatBackend).
// An install upgraded from one of those comes back through the ordinary
// apply. A gateway that was at zero gets one replica on the same object,
// with the condition kept as WaitingForReplica until that replica is ready,
// and then cleared. A gateway that was withheld is created, and its legacy
// condition clears on that first pass while Ready waits on the new gateway.
//
// envtest runs no Deployment controller, no kubelet and no garbage collector,
// so these tests read what those would act on: spec.replicas, the
// Deployment's UID against the session pod's ownerReference (an owner that is
// still there is one the collector leaves alone), and the managedFields entry
// for spec.replicas. Readiness is reported by hand, as the Deployment
// controller would.

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	autoscalingv1 "k8s.io/api/autoscaling/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// gwEnvtestRequeue is the interval the reconcile's A2A requeue returns, the
// term the way back rides while the scaled-up replica is not ready.
const gwEnvtestRequeue = 30 * time.Second

// gwEnvtestSessionPod is the session pod the gateway spawned before its
// backend went away, or before an older operator darkened it.
const gwEnvtestSessionPod = "test-agent-session-1"

// gwEnvtestForeignManager is a field manager other than the operator's,
// standing in for `kubectl scale`.
const gwEnvtestForeignManager = "kubectl-scale"

// gwEnvtestWakingReason is the A2AGateway reason on the way back from dark,
// and gwEnvtestLegacyReason the one older operators wrote on a dark pass,
// spelled out so the test reads the API rather than the constants it checks.
const (
	gwEnvtestWakingReason = "WaitingForReplica"
	gwEnvtestLegacyReason = "NoChatBackend"
)

// gatewayRig is one PlatformAgent on its own envtest API server.
type gatewayRig struct {
	t     *testing.T
	ctx   context.Context
	cl    client.Client
	r     *PlatformAgentReconciler
	agent *agentv1alpha1.PlatformAgent
	req   ctrl.Request
}

// newGatewayRig starts an API server and creates a mode-next PlatformAgent on it,
// shaped by mutate, with the sandbox keypair a Ready-path pass needs. The
// discord-bot Secret is the caller's to create or not.
func newGatewayRig(t *testing.T, namespace string, mutate func(*agentv1alpha1.PlatformAgent)) *gatewayRig {
	t.Helper()
	cl, scheme := startEnvtest(t)
	ctx := context.Background()
	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	agent := a2aTestAgent()
	agent.Namespace = namespace
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID:   envtestHarnessProject,
		Location:    envtestHarnessLocation,
		ClusterName: envtestHarnessCluster,
	}
	if mutate != nil {
		mutate(agent)
	}
	if err := cl.Create(ctx, agent); err != nil {
		t.Fatalf("creating the PlatformAgent: %v", err)
	}
	if err := cl.Create(ctx, shellSandboxKeysSecret(agent)); err != nil {
		t.Fatalf("creating sandbox keys Secret: %v", err)
	}
	rig := &gatewayRig{
		t: t, ctx: ctx, cl: cl, agent: agent,
		r:   &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme},
		req: ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)},
	}
	return rig
}

// settle brings the install to the state the tests measure from: the callout
// serving (so the gateway's creation gate opens) and the provision Job
// complete (so the !done requeue term is not what any requeue measured later
// comes from), then two passes so the finalizer write and the first render are
// behind it.
func (g *gatewayRig) settle() {
	g.t.Helper()
	g.fresh()
	theCalloutIsServing(g.t, g.ctx, g.cl, g.r, g.agent)
	g.completeTheProvisionJob()
	g.pass()
	g.pass()
}

// fresh re-reads the CR, so a pass sees the status the previous one wrote.
func (g *gatewayRig) fresh() *agentv1alpha1.PlatformAgent {
	g.t.Helper()
	got := &agentv1alpha1.PlatformAgent{}
	if err := g.cl.Get(g.ctx, g.req.NamespacedName, got); err != nil {
		g.t.Fatalf("reading the PlatformAgent: %v", err)
	}
	g.agent = got
	return got
}

// pass runs one Reconcile and returns its result.
func (g *gatewayRig) pass() ctrl.Result {
	g.t.Helper()
	res, err := g.r.Reconcile(g.ctx, g.req)
	if err != nil {
		g.t.Fatalf("Reconcile: %v", err)
	}
	g.fresh()
	return res
}

// completeTheProvisionJob is the package helper's Job completion in the shape
// a 1.36 API server validates: a start time under the completion time, and
// SuccessCriteriaMet beside Complete.
func (g *gatewayRig) completeTheProvisionJob() {
	g.t.Helper()
	job := buildA2AProvisionJob(g.agent)
	key := client.ObjectKeyFromObject(job)
	if err := g.cl.Get(g.ctx, key, job); err != nil {
		if !errors.IsNotFound(err) {
			g.t.Fatalf("get provision Job: %v", err)
		}
		job = buildA2AProvisionJob(g.agent)
		withCommonLabels(job, g.agent)
		if err := g.cl.Create(g.ctx, job); err != nil {
			g.t.Fatalf("create provision Job: %v", err)
		}
	}
	start := metav1.NewTime(time.Now().Add(-time.Minute).Truncate(time.Second))
	done := metav1.NewTime(time.Now().Truncate(time.Second))
	job.Status.StartTime = &start
	job.Status.CompletionTime = &done
	job.Status.Succeeded = 1
	job.Status.Conditions = []batchv1.JobCondition{
		{Type: batchv1.JobSuccessCriteriaMet, Status: corev1.ConditionTrue, LastTransitionTime: done},
		{Type: batchv1.JobComplete, Status: corev1.ConditionTrue, LastTransitionTime: done},
	}
	if err := g.cl.Status().Update(g.ctx, job); err != nil {
		g.t.Fatalf("complete provision Job: %v", err)
	}
}

func (g *gatewayRig) gatewayKey() types.NamespacedName {
	return types.NamespacedName{Name: a2aGatewayName(g.agent), Namespace: g.agent.Namespace}
}

// gateway reads the gateway Deployment, failing if it is absent.
func (g *gatewayRig) gateway() *appsv1.Deployment {
	g.t.Helper()
	dep := &appsv1.Deployment{}
	if err := g.cl.Get(g.ctx, g.gatewayKey(), dep); err != nil {
		g.t.Fatalf("reading the gateway Deployment: %v", err)
	}
	return dep
}

// replicas is the gateway Deployment's spec.replicas.
func (g *gatewayRig) replicas() int32 {
	g.t.Helper()
	dep := g.gateway()
	if dep.Spec.Replicas == nil {
		g.t.Fatal("the gateway Deployment carries no spec.replicas")
	}
	return *dep.Spec.Replicas
}

// reportGatewayReady writes the status the Deployment controller would once
// n replicas are ready on the current template.
func (g *gatewayRig) reportGatewayReady(n int32) {
	g.t.Helper()
	dep := g.gateway()
	dep.Status.ObservedGeneration = dep.Generation
	dep.Status.Replicas = n
	dep.Status.UpdatedReplicas = n
	dep.Status.ReadyReplicas = n
	dep.Status.AvailableReplicas = n
	if err := g.cl.Status().Update(g.ctx, dep); err != nil {
		g.t.Fatalf("reporting the gateway's readiness: %v", err)
	}
}

// spawnSessionPod creates a session pod owned by the gateway Deployment the
// way a2a/gateway/spawn.go does: an ownerReference to the Deployment by UID,
// marked controller.
func (g *gatewayRig) spawnSessionPod() {
	g.t.Helper()
	dep := g.gateway()
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: gwEnvtestSessionPod, Namespace: g.agent.Namespace,
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: "apps/v1", Kind: "Deployment", Name: dep.Name, UID: dep.UID, Controller: ptr.To(true),
			}},
		},
		Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "session", Image: "session:dev"}}},
	}
	if err := g.cl.Create(g.ctx, pod); err != nil {
		g.t.Fatalf("creating the session pod: %v", err)
	}
}

// sessionPodStillOwned asserts the session pod exists and its ownerReference
// names the gateway Deployment that exists now, by UID: the state in which
// the garbage collector, which envtest does not run, leaves it alone.
func (g *gatewayRig) sessionPodStillOwned(step string) {
	g.t.Helper()
	pod := &corev1.Pod{}
	if err := g.cl.Get(g.ctx, types.NamespacedName{Name: gwEnvtestSessionPod, Namespace: g.agent.Namespace}, pod); err != nil {
		g.t.Fatalf("%s: the session pod is gone: %v", step, err)
	}
	dep := g.gateway()
	if len(pod.OwnerReferences) != 1 || pod.OwnerReferences[0].UID != dep.UID {
		g.t.Errorf("%s: the session pod's owner %+v does not name the gateway Deployment that exists (UID %s); the collector would take it",
			step, pod.OwnerReferences, dep.UID)
	}
}

// gatewayCondition is the CR's A2AGateway condition, or nil.
func (g *gatewayRig) gatewayCondition() *metav1.Condition {
	return meta.FindStatusCondition(g.agent.Status.Conditions, a2aGatewayConditionType)
}

// splitHasGateway reports whether the status writer's workload list counts
// the gateway, its ready count, and the A2AGateway condition text it returned
// beside the list.
func (g *gatewayRig) splitHasGateway(state a2aProvisionState) (bool, int32, string) {
	g.t.Helper()
	workloads, msg, err := g.r.readSplitWorkloads(g.ctx, g.agent, state)
	if err != nil {
		g.t.Fatalf("readSplitWorkloads: %v", err)
	}
	for _, w := range workloads {
		if w.name == a2aGatewayName(g.agent) {
			return true, w.ready, msg
		}
	}
	return false, 0, msg
}

// consoleDoorValue is the gateway container's A2A_CONSOLE_DOOR value, and
// whether it is set at all.
func (g *gatewayRig) consoleDoorValue() (string, bool) {
	g.t.Helper()
	for _, e := range g.gateway().Spec.Template.Spec.Containers[0].Env {
		if e.Name == "A2A_CONSOLE_DOOR" {
			return e.Value, true
		}
	}
	return "", false
}

// assertRunning is the state every pass on a next install ends in, backend
// or not: the same object at one replica with the console door armed, and
// no A2AGateway condition.
func (g *gatewayRig) assertRunning(step string, uid types.UID) {
	g.t.Helper()
	dep := g.gateway()
	if uid != "" && dep.UID != uid {
		g.t.Fatalf("%s: the gateway Deployment was replaced (UID %s, was %s); every session pod owned by the old one goes to the collector", step, dep.UID, uid)
	}
	if got := g.replicas(); got != 1 {
		g.t.Errorf("%s: the gateway asks for %d replicas, want 1; every next install runs its gateway", step, got)
	}
	if v, ok := g.consoleDoorValue(); !ok || v != "true" {
		g.t.Errorf("%s: A2A_CONSOLE_DOOR = %q (set=%v), want \"true\"; without it a gateway with no backend refuses to start", step, v, ok)
	}
	if cond := g.gatewayCondition(); cond != nil {
		g.t.Errorf("%s: the CR carries an A2AGateway condition over a running gateway: %+v", step, cond)
	}
}

// TestANextInstallWithNoBackendRunsAConsoleOnlyGatewayEnvtest: no discord-bot
// Secret, no door, neither Chat nor Slack. The gateway is created at one
// replica with the console door armed, no A2AGateway condition is written,
// and Ready counts the gateway as a workload rather than skipping it.
func TestANextInstallWithNoBackendRunsAConsoleOnlyGatewayEnvtest(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	g := newGatewayRig(t, "console-only", nil)
	g.settle()
	g.assertRunning("no backend", "")

	state, err := g.r.reconcileA2A(g.ctx, g.agent)
	if err != nil {
		t.Fatal(err)
	}
	if state.gatewayWaking {
		t.Error("a fresh install with no backend reports the gateway waking; there was never a dark gateway to wake")
	}
	counted, ready, msg := g.splitHasGateway(state)
	if !counted || ready != 0 || msg != "" {
		t.Errorf("no replica ready: Ready's workload list counts the gateway=%v ready=%d with condition text %q, want counted, not ready, none", counted, ready, msg)
	}
	g.reportGatewayReady(1)
	if counted, ready, msg := g.splitHasGateway(state); !counted || ready != 1 || msg != "" {
		t.Errorf("replica ready: Ready's workload list counts the gateway=%v ready=%d with condition text %q, want counted, ready, none", counted, ready, msg)
	}
	g.pass()
	g.assertRunning("no backend, replica ready", "")
}

// TestTheDiscordSecretTakenAwayLeavesTheGatewayRunningEnvtest: the #2057
// shape, the discord-bot Secret taken from under a running gateway. Older
// operators scaled it to zero; now it stays at one replica on the same
// object, which falls back to the console door, and the session pod it
// spawned stays owned.
func TestTheDiscordSecretTakenAwayLeavesTheGatewayRunningEnvtest(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	g := newGatewayRig(t, "console-discord", nil)
	if err := g.cl.Create(g.ctx, discordBotSecret(g.agent)); err != nil {
		t.Fatal(err)
	}
	g.settle()
	g.reportGatewayReady(1)
	g.spawnSessionPod()
	uid := g.gateway().UID
	g.assertRunning("Discord backend", uid)

	if err := g.cl.Delete(g.ctx, discordBotSecret(g.agent)); err != nil {
		t.Fatal(err)
	}
	g.pass()
	g.assertRunning("Secret deleted", uid)
	g.sessionPodStillOwned("Secret deleted")
	g.pass()
	g.assertRunning("Secret deleted, second pass", uid)
}

// TestTheInjectDoorTurnedOffLeavesTheGatewayRunningEnvtest: the trigger in
// #2481. An eval install whose only backend is the inject door turns the door
// off: the gateway stays at one replica on the same object, without the
// door's env, and the door's objects go.
func TestTheInjectDoorTurnedOffLeavesTheGatewayRunningEnvtest(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "true")
	t.Setenv(a2aAgentDoorEnvVar, "")
	g := newGatewayRig(t, "console-inject", nil)
	g.settle()
	g.reportGatewayReady(1)
	g.spawnSessionPod()
	uid := g.gateway().UID
	g.pass()
	g.assertRunning("door on", uid)
	doorKey := types.NamespacedName{Name: a2aInjectName(g.agent), Namespace: g.agent.Namespace}
	if err := g.cl.Get(g.ctx, doorKey, &corev1.Service{}); err != nil {
		t.Fatalf("precondition: the inject door's Service is not rendered: %v", err)
	}

	t.Setenv(a2aInjectBackendEnvVar, "")
	g.pass()
	g.assertRunning("door off", uid)
	for _, obj := range []client.Object{&corev1.Service{}, &corev1.ConfigMap{}, &corev1.Secret{}} {
		if err := g.cl.Get(g.ctx, doorKey, obj); !errors.IsNotFound(err) {
			t.Errorf("door off: the inject door's %T survived (err=%v)", obj, err)
		}
	}
	for _, e := range g.gateway().Spec.Template.Spec.Containers[0].Env {
		if e.Name == a2aInjectListenEnvVar {
			t.Errorf("door off: the gateway still carries %s for a door that is gone", e.Name)
		}
	}
	g.sessionPodStillOwned("door off")
}

// darkenLikeAnOlderOperator puts the install in the state an operator that
// darkened gateways left it in: the gateway applied at zero replicas under
// the operator's field manager, no replica ready, and the A2AGateway
// condition with the legacy NoChatBackend reason.
func (g *gatewayRig) darkenLikeAnOlderOperator() {
	g.t.Helper()
	dep := buildA2AGatewayDeployment(g.agent)
	if err := ctrl.SetControllerReference(g.agent, dep, g.r.Scheme); err != nil {
		g.t.Fatal(err)
	}
	dep.Spec.Replicas = ptr.To(int32(0))
	if err := g.r.applyA2AGatewayDeployment(g.ctx, g.agent, dep); err != nil {
		g.t.Fatalf("applying the gateway at zero replicas: %v", err)
	}
	g.reportGatewayReady(0)
	g.setLegacyCondition()
	if got := g.replicas(); got != 0 {
		g.t.Fatalf("precondition: the darkened gateway asks for %d replicas, want 0", got)
	}
}

// setLegacyCondition writes the A2AGateway condition an older operator wrote
// on a dark pass, with its reason spelled out so the test reads the API
// rather than the constant it checks.
func (g *gatewayRig) setLegacyCondition() {
	g.t.Helper()
	agent := g.fresh()
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type:               a2aGatewayConditionType,
		Status:             metav1.ConditionFalse,
		Reason:             gwEnvtestLegacyReason,
		Message:            "no chat backend is configured for the A2A gateway, so it is not running",
		ObservedGeneration: agent.Generation,
		LastTransitionTime: metav1.Now(),
	})
	if err := g.cl.Status().Update(g.ctx, agent); err != nil {
		g.t.Fatalf("writing the legacy A2AGateway condition: %v", err)
	}
	g.fresh()
}

// assertWaking is the condition on the way back: still there, but no longer
// the legacy remedy, which asks for a backend the gateway no longer needs.
func (g *gatewayRig) assertWaking(step string) {
	g.t.Helper()
	cond := g.gatewayCondition()
	if cond == nil {
		g.t.Errorf("%s: the A2AGateway condition cleared over a gateway with no ready replica", step)
		return
	}
	if cond.Status != metav1.ConditionFalse || cond.Reason != gwEnvtestWakingReason || cond.Message != a2aGatewayWakingMessage {
		g.t.Errorf("%s: A2AGateway condition = %+v, want False/%s with the way back's message", step, cond, gwEnvtestWakingReason)
	}
}

// TestAnUpgradedDarkGatewayComesBackEnvtest: the upgrade path. An install an
// older operator darkened (gateway at zero, NoChatBackend) and still with no
// backend: the first pass scales the same object to one replica, the
// condition becomes WaitingForReplica while no replica is ready, Ready waits
// on the gateway, the pass requeues, and once the replica is ready the
// condition clears.
func TestAnUpgradedDarkGatewayComesBackEnvtest(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	g := newGatewayRig(t, "console-upgrade", nil)
	g.settle()
	g.spawnSessionPod()
	uid := g.gateway().UID
	g.darkenLikeAnOlderOperator()

	res := g.pass()
	if g.gateway().UID != uid {
		t.Fatalf("upgrade: the gateway Deployment was replaced (UID %s, was %s)", g.gateway().UID, uid)
	}
	if got := g.replicas(); got != 1 {
		t.Errorf("upgrade: the gateway asks for %d replicas, want 1", got)
	}
	if v, ok := g.consoleDoorValue(); !ok || v != "true" {
		t.Errorf("upgrade: A2A_CONSOLE_DOOR = %q (set=%v), want \"true\"", v, ok)
	}
	g.assertWaking("upgrade, no replica ready")
	state, err := g.r.reconcileA2A(g.ctx, g.agent)
	if err != nil {
		t.Fatal(err)
	}
	if !state.gatewayWaking {
		t.Error("upgrade, no replica ready: the render does not report the gateway waking")
	}
	if counted, ready, _ := g.splitHasGateway(state); !counted || ready != 0 {
		t.Errorf("upgrade, no replica ready: Ready's workload list counts the gateway=%v ready=%d, want counted and not ready", counted, ready)
	}
	if res.RequeueAfter != gwEnvtestRequeue {
		t.Errorf("upgrade, no replica ready: requeued after %s, want %s while the condition waits on the replica", res.RequeueAfter, gwEnvtestRequeue)
	}
	// A second pass holds the way back rather than flapping.
	g.pass()
	g.assertWaking("upgrade, second pass")

	// The status writer alone, handed a render that still said waking (its
	// informer read predates the replica turning ready): its own read of the
	// ready count wins.
	g.reportGatewayReady(1)
	if counted, ready, msg := g.splitHasGateway(a2aProvisionState{done: true, gatewayWaking: true}); !counted || ready != 1 || msg != "" {
		t.Errorf("replica ready, render still waking: Ready's workload list counts the gateway=%v ready=%d with condition text %q, want counted, ready and none",
			counted, ready, msg)
	}
	g.pass()
	g.assertRunning("upgrade, replica ready", uid)
	g.sessionPodStillOwned("upgrade")
}

// TestAStaleLegacyConditionOnARunningGatewayClearsEnvtest: a NoChatBackend
// condition on a CR whose gateway is already ready -- left by an older
// operator that died between the scale-up and the status write, or restored
// from a backup -- clears on the next pass, and the gateway is untouched.
func TestAStaleLegacyConditionOnARunningGatewayClearsEnvtest(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	g := newGatewayRig(t, "console-stale", nil)
	g.settle()
	g.reportGatewayReady(1)
	uid := g.gateway().UID
	g.setLegacyCondition()

	g.pass()
	g.assertRunning("stale legacy condition", uid)
}

// TestAnUpgradedGatewayParkedDegradedKeepsTheConditionEnvtest: the way back
// on a pass that ends Degraded. The Degraded writer reads no workloads, so it
// has to keep the condition from the render's own answer; if it dropped it,
// the next pass would find no condition to keep and the CR would stop saying
// anything before the replica is ready.
func TestAnUpgradedGatewayParkedDegradedKeepsTheConditionEnvtest(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	g := newGatewayRig(t, "console-degraded", nil)
	g.settle()
	g.darkenLikeAnOlderOperator()

	// Park the install on the missing sandbox keypair: every pass from here
	// ends in the Degraded writer.
	if err := g.cl.Delete(g.ctx, shellSandboxKeysSecret(g.agent)); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		g.pass()
		if ready := meta.FindStatusCondition(g.agent.Status.Conditions, "Ready"); ready == nil || ready.Reason != reasonShellSandboxKeysMissing {
			t.Fatalf("precondition, pass %d: the install is not parked on the missing keypair: %+v", i+1, ready)
		}
		if got := g.replicas(); got != 1 {
			t.Errorf("parked pass %d: the gateway asks for %d replicas, want 1", i+1, got)
		}
		g.assertWaking("parked pass")
	}

	// The replica ready: the condition clears, on the parked path too.
	g.reportGatewayReady(1)
	g.pass()
	if cond := g.gatewayCondition(); cond != nil {
		t.Errorf("replica ready: the CR still carries %+v", cond)
	}
}

// replicasManagers lists the field managers whose managedFields entry for the
// gateway Deployment claims spec.replicas.
func replicasManagers(t *testing.T, dep *appsv1.Deployment) []string {
	t.Helper()
	var managers []string
	for _, mf := range dep.ManagedFields {
		if mf.FieldsV1 == nil {
			continue
		}
		var fields map[string]any
		if err := json.Unmarshal(mf.FieldsV1.Raw, &fields); err != nil {
			t.Fatalf("decoding managedFields for %s: %v", mf.Manager, err)
		}
		if spec, ok := fields["f:spec"].(map[string]any); ok {
			if _, ok := spec["f:replicas"]; ok {
				managers = append(managers, mf.Manager)
			}
		}
	}
	return managers
}

// TestTheReplicaCountComesBackUnderTheOperatorsFieldManagerEnvtest:
// spec.replicas is the operator's field from the zero an older operator left
// to the one this one renders, applied by server-side apply under its one
// field manager, and a scale by another manager in between is taken back by
// the next apply without a conflict error.
func TestTheReplicaCountComesBackUnderTheOperatorsFieldManagerEnvtest(t *testing.T) {
	cl, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := a2aTestAgent()
	agent.Namespace = "console-ssa"
	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	key := client.ObjectKeyFromObject(buildA2AGatewayDeployment(agent))
	apply := func(replicas *int32) *appsv1.Deployment {
		t.Helper()
		dep := buildA2AGatewayDeployment(agent)
		if replicas != nil {
			dep.Spec.Replicas = replicas
		}
		if err := r.applyA2AGatewayDeployment(ctx, agent, dep); err != nil {
			t.Fatalf("applying the gateway: %v", err)
		}
		got := &appsv1.Deployment{}
		if err := cl.Get(ctx, key, got); err != nil {
			t.Fatal(err)
		}
		return got
	}
	check := func(step string, got *appsv1.Deployment, want int32, uid types.UID) {
		t.Helper()
		if got.Spec.Replicas == nil || *got.Spec.Replicas != want {
			t.Errorf("%s: spec.replicas = %v, want %d", step, got.Spec.Replicas, want)
		}
		if uid != "" && got.UID != uid {
			t.Errorf("%s: UID changed to %s, was %s", step, got.UID, uid)
		}
		if m := replicasManagers(t, got); len(m) != 1 || m[0] != fieldOwner {
			t.Errorf("%s: spec.replicas is managed by %v, want only %q", step, m, fieldOwner)
		}
	}

	// What an older operator left: the gateway at zero.
	dark := apply(ptr.To(int32(0)))
	uid := dark.UID
	check("left at 0", dark, 0, "")

	// Another manager scales it through the scale subresource, as
	// `kubectl scale` does.
	scale := &autoscalingv1.Scale{Spec: autoscalingv1.ScaleSpec{Replicas: 2}}
	if err := cl.SubResource("scale").Update(ctx, dark, client.WithSubResourceBody(scale), client.FieldOwner(gwEnvtestForeignManager)); err != nil {
		t.Fatalf("scaling the gateway as another manager: %v", err)
	}
	// This operator's render, replicas as buildA2AGatewayDeployment sets them.
	check("rendered after a foreign scale", apply(nil), 1, uid)
}

// TestAStaleMissOnTheUpgradedGatewayKeepsTheWayBackEnvtest: on the first pass
// over a gateway an older operator left at zero replicas, a cache that misses
// it (an informer that has not caught up) must not drop the legacy condition
// for good. The render confirms the miss live while that condition stands,
// and the status writer keeps the condition, as WaitingForReplica, on a miss
// of its own.
func TestAStaleMissOnTheUpgradedGatewayKeepsTheWayBackEnvtest(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	g := newGatewayRig(t, "console-stale-miss", nil)
	g.settle()
	g.darkenLikeAnOlderOperator()
	g.r.Client = staleDeploymentClient{Client: g.cl, key: types.NamespacedName{Name: a2aGatewayName(g.agent), Namespace: g.agent.Namespace}}

	g.pass()
	if got := g.replicas(); got != 1 {
		t.Errorf("stale miss: the gateway asks for %d replicas, want 1", got)
	}
	g.assertWaking("stale miss, no replica ready")
}
