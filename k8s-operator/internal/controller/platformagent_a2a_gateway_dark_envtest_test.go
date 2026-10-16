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

// A gateway whose last chat backend goes away, against a real API server
// (#2481). The gateway binary exits on "no chat backend", so a Deployment
// left at one replica crash-loops; deleting it hands every session pod that
// hangs off its UID to the garbage collector. The operator applies it at zero
// replicas instead, keeps the object and its UID, and reports it dark with
// the condition the creation path writes. When a backend returns, the next
// pass applies one replica on the same object, and the condition stays until
// that replica is ready.
//
// envtest runs no Deployment controller, no kubelet and no garbage collector,
// so these tests read what those would act on: spec.replicas (zero runs no
// pod, so nothing crash-loops), the Deployment's UID against the session
// pod's ownerReference (an owner that is still there is one the collector
// leaves alone), and the managedFields entry for spec.replicas. Readiness is
// reported by hand, as the Deployment controller would.

import (
	"context"
	"encoding/json"
	"strings"
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

// darkEnvtestRequeue is the interval the reconcile's A2A requeue returns: the
// one term that brings an unwatched backend Secret into view.
const darkEnvtestRequeue = 30 * time.Second

// darkEnvtestSessionPod is the session pod the gateway spawned before its
// backend went away.
const darkEnvtestSessionPod = "test-agent-session-1"

// darkEnvtestForeignManager is a field manager other than the operator's,
// standing in for `kubectl scale`.
const darkEnvtestForeignManager = "kubectl-scale"

// darkEnvtestWakingReason is the A2AGateway reason on the way back from dark,
// spelled out so the test reads the API rather than the constant it checks.
const darkEnvtestWakingReason = "WaitingForReplica"

// darkRig is one PlatformAgent on its own envtest API server.
type darkRig struct {
	t     *testing.T
	ctx   context.Context
	cl    client.Client
	r     *PlatformAgentReconciler
	agent *agentv1alpha1.PlatformAgent
	req   ctrl.Request
}

// newDarkRig starts an API server and creates a mode-next PlatformAgent on it,
// shaped by mutate, with the sandbox keypair a Ready-path pass needs. The
// discord-bot Secret is the caller's to create or not.
func newDarkRig(t *testing.T, namespace string, mutate func(*agentv1alpha1.PlatformAgent)) *darkRig {
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
	rig := &darkRig{
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
func (g *darkRig) settle() {
	g.t.Helper()
	g.fresh()
	theCalloutIsServing(g.t, g.ctx, g.cl, g.r, g.agent)
	g.completeTheProvisionJob()
	g.pass()
	g.pass()
}

// fresh re-reads the CR, so a pass sees the status the previous one wrote.
func (g *darkRig) fresh() *agentv1alpha1.PlatformAgent {
	g.t.Helper()
	got := &agentv1alpha1.PlatformAgent{}
	if err := g.cl.Get(g.ctx, g.req.NamespacedName, got); err != nil {
		g.t.Fatalf("reading the PlatformAgent: %v", err)
	}
	g.agent = got
	return got
}

// pass runs one Reconcile and returns its result.
func (g *darkRig) pass() ctrl.Result {
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
func (g *darkRig) completeTheProvisionJob() {
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

func (g *darkRig) gatewayKey() types.NamespacedName {
	return types.NamespacedName{Name: a2aGatewayName(g.agent), Namespace: g.agent.Namespace}
}

// gateway reads the gateway Deployment, failing if it is absent.
func (g *darkRig) gateway() *appsv1.Deployment {
	g.t.Helper()
	dep := &appsv1.Deployment{}
	if err := g.cl.Get(g.ctx, g.gatewayKey(), dep); err != nil {
		g.t.Fatalf("reading the gateway Deployment: %v", err)
	}
	return dep
}

// replicas is the gateway Deployment's spec.replicas.
func (g *darkRig) replicas() int32 {
	g.t.Helper()
	dep := g.gateway()
	if dep.Spec.Replicas == nil {
		g.t.Fatal("the gateway Deployment carries no spec.replicas")
	}
	return *dep.Spec.Replicas
}

// reportGatewayReady writes the status the Deployment controller would once
// n replicas are ready on the current template.
func (g *darkRig) reportGatewayReady(n int32) {
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
func (g *darkRig) spawnSessionPod() {
	g.t.Helper()
	dep := g.gateway()
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: darkEnvtestSessionPod, Namespace: g.agent.Namespace,
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
func (g *darkRig) sessionPodStillOwned(step string) {
	g.t.Helper()
	pod := &corev1.Pod{}
	if err := g.cl.Get(g.ctx, types.NamespacedName{Name: darkEnvtestSessionPod, Namespace: g.agent.Namespace}, pod); err != nil {
		g.t.Fatalf("%s: the session pod is gone: %v", step, err)
	}
	dep := g.gateway()
	if len(pod.OwnerReferences) != 1 || pod.OwnerReferences[0].UID != dep.UID {
		g.t.Errorf("%s: the session pod's owner %+v does not name the gateway Deployment that exists (UID %s); the collector would take it",
			step, pod.OwnerReferences, dep.UID)
	}
}

// gatewayCondition is the CR's A2AGateway condition, or nil.
func (g *darkRig) gatewayCondition() *metav1.Condition {
	return meta.FindStatusCondition(g.agent.Status.Conditions, a2aGatewayConditionType)
}

// creationPathReason is the text the creation path writes for this install as
// it stands: a2aGatewayBackend's remedy, which the dark condition on an
// existing gateway must repeat word for word.
func (g *darkRig) creationPathReason() string {
	g.t.Helper()
	configured, why, err := g.r.a2aGatewayBackend(g.ctx, g.agent)
	if err != nil || configured {
		g.t.Fatalf("precondition: the install has a backend or the question failed (configured=%v err=%v)", configured, err)
	}
	return why
}

// splitHasGateway reports whether the status writer's workload list counts
// the gateway, and the dark text it returned beside the list.
func (g *darkRig) splitHasGateway(state a2aProvisionState) (bool, int32, string) {
	g.t.Helper()
	workloads, dark, err := g.r.readSplitWorkloads(g.ctx, g.agent, state)
	if err != nil {
		g.t.Fatalf("readSplitWorkloads: %v", err)
	}
	for _, w := range workloads {
		if w.name == a2aGatewayName(g.agent) {
			return true, w.ready, dark
		}
	}
	return false, 0, dark
}

// assertDark is the state every dark pass on an existing gateway ends in:
// the same object at zero replicas, the creation path's condition text, the
// gateway out of Ready's workload list, and the requeue.
func (g *darkRig) assertDark(step string, uid types.UID, res ctrl.Result) {
	g.t.Helper()
	dep := g.gateway()
	if dep.UID != uid {
		g.t.Fatalf("%s: the gateway Deployment was replaced (UID %s, was %s); every session pod owned by the old one goes to the collector", step, dep.UID, uid)
	}
	if got := g.replicas(); got != 0 {
		g.t.Errorf("%s: the gateway asks for %d replicas with no chat backend, want 0; one replica exits on \"no chat backend\" and crash-loops", step, got)
	}
	want := g.creationPathReason()
	cond := g.gatewayCondition()
	if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != a2aGatewayDarkReason || cond.Message != want {
		g.t.Errorf("%s: A2AGateway condition = %+v, want False/%s with the creation path's text %q", step, cond, a2aGatewayDarkReason, want)
	}
	state, err := g.r.reconcileA2A(g.ctx, g.agent)
	if err != nil {
		g.t.Fatalf("%s: reconcileA2A: %v", step, err)
	}
	counted, _, dark := g.splitHasGateway(state)
	if counted || dark != want {
		g.t.Errorf("%s: Ready's workload list counts the gateway=%v with dark text %q; a gateway at zero on purpose is reported, not waited on", step, counted, dark)
	}
	if res.RequeueAfter != darkEnvtestRequeue {
		g.t.Errorf("%s: the dark pass requeued after %s, want %s; an unwatched backend Secret would never be seen", step, res.RequeueAfter, darkEnvtestRequeue)
	}
}

// TestTheInjectDoorTurnedOffScalesTheGatewayToZeroAndBackEnvtest: the trigger
// in #2481. An eval install whose only backend is the inject door turns the
// door off: the gateway goes to zero replicas on the same object, dark with
// the creation path's reason, the door's objects go, and the session pod it
// spawned stays owned. The door back on: one replica on the same object, the
// condition held until that replica is ready, and then cleared.
func TestTheInjectDoorTurnedOffScalesTheGatewayToZeroAndBackEnvtest(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "true")
	t.Setenv(a2aAgentDoorEnvVar, "")
	g := newDarkRig(t, "dark-inject", nil)
	g.settle()
	if got := g.replicas(); got != 1 {
		t.Fatalf("precondition: the door-armed gateway asks for %d replicas, want 1", got)
	}
	g.reportGatewayReady(1)
	g.spawnSessionPod()
	uid := g.gateway().UID
	g.pass()
	if cond := g.gatewayCondition(); cond != nil {
		t.Fatalf("precondition: a running door gateway carries %+v", cond)
	}
	doorKey := types.NamespacedName{Name: a2aInjectName(g.agent), Namespace: g.agent.Namespace}
	if err := g.cl.Get(g.ctx, doorKey, &corev1.Service{}); err != nil {
		t.Fatalf("precondition: the inject door's Service is not rendered: %v", err)
	}

	// (a) The door off, which is what restarting the operator without the
	// flag does.
	t.Setenv(a2aInjectBackendEnvVar, "")
	res := g.pass()
	g.assertDark("door off", uid, res)
	// What the Deployment controller reports once the pod is gone.
	g.reportGatewayReady(0)
	for _, obj := range []client.Object{&corev1.Service{}, &corev1.ConfigMap{}, &corev1.Secret{}} {
		if err := g.cl.Get(g.ctx, doorKey, obj); !errors.IsNotFound(err) {
			t.Errorf("door off: the inject door's %T survived the dark pass (err=%v)", obj, err)
		}
	}
	for _, e := range g.gateway().Spec.Template.Spec.Containers[0].Env {
		if e.Name == a2aInjectListenEnvVar {
			t.Errorf("door off: the gateway at zero still carries %s; the next scale-up would arm a door that is gone", e.Name)
		}
	}
	g.sessionPodStillOwned("door off")
	// A second dark pass holds the same state rather than flapping.
	res = g.pass()
	g.assertDark("door off, second pass", uid, res)

	// (b) The door back on.
	t.Setenv(a2aInjectBackendEnvVar, "true")
	res = g.pass()
	if g.gateway().UID != uid {
		t.Fatalf("door on: the gateway Deployment was replaced (UID %s, was %s)", g.gateway().UID, uid)
	}
	if got := g.replicas(); got != 1 {
		t.Errorf("door on: the gateway asks for %d replicas with the door armed, want 1", got)
	}
	// The Deployment controller has not reported the new replica: the
	// condition stays, Ready waits on the gateway, and the pass requeues.
	g.assertWaking("door on, no replica ready")
	state, err := g.r.reconcileA2A(g.ctx, g.agent)
	if err != nil {
		t.Fatal(err)
	}
	if counted, ready, _ := g.splitHasGateway(state); !counted || ready != 0 {
		t.Errorf("door on, no replica ready: Ready's workload list counts the gateway=%v ready=%d, want counted and not ready", counted, ready)
	}
	if res.RequeueAfter != darkEnvtestRequeue {
		t.Errorf("door on, no replica ready: requeued after %s, want %s while the condition waits on the replica", res.RequeueAfter, darkEnvtestRequeue)
	}
	// Ready: the condition clears. First the status writer alone, handed a
	// render that still said waking (its informer read predates the replica
	// turning ready): its own read of the ready count wins, so the gateway
	// counts as ready and no condition text comes back.
	g.reportGatewayReady(1)
	if counted, ready, dark := g.splitHasGateway(a2aProvisionState{done: true, gatewayWaking: true}); !counted || ready != 1 || dark != "" {
		t.Errorf("replica ready, render still waking: Ready's workload list counts the gateway=%v ready=%d with condition text %q, want counted, ready and none",
			counted, ready, dark)
	}
	g.pass()
	if cond := g.gatewayCondition(); cond != nil {
		t.Errorf("door on, replica ready: the CR still says the gateway is dark: %+v", cond)
	}
	if g.gateway().UID != uid {
		t.Errorf("door on: the gateway Deployment UID changed to %s, was %s", g.gateway().UID, uid)
	}
	// (e) The session pod survived the whole round trip.
	g.sessionPodStillOwned("round trip")
}

// assertWaking is the condition on the way back from dark: still there, but
// no longer the dark pass's remedy, which by now asks for a backend that
// exists.
func (g *darkRig) assertWaking(step string) {
	g.t.Helper()
	cond := g.gatewayCondition()
	if cond == nil {
		g.t.Errorf("%s: the A2AGateway condition cleared over a gateway with no ready replica", step)
		return
	}
	if cond.Status != metav1.ConditionFalse || cond.Reason != darkEnvtestWakingReason ||
		strings.Contains(cond.Message, a2aDiscordBotSecretName) || !strings.Contains(cond.Message, "configured again") {
		g.t.Errorf("%s: A2AGateway condition = %+v, want False/%s saying the backend is back and the replica is not ready, without the dark remedy",
			step, cond, darkEnvtestWakingReason)
	}
}

// TestAWakingPassParkedDegradedKeepsTheConditionEnvtest: the way back from
// dark on a pass that ends Degraded. The Degraded writer reads no workloads,
// so it has to keep the condition from the render's own answer; if it
// dropped it, the next pass would find no condition to keep and the CR would
// stop saying anything before the replica is ready.
func TestAWakingPassParkedDegradedKeepsTheConditionEnvtest(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	g := newDarkRig(t, "dark-degraded", nil)
	if err := g.cl.Create(g.ctx, discordBotSecret(g.agent)); err != nil {
		t.Fatal(err)
	}
	g.settle()
	g.reportGatewayReady(1)
	uid := g.gateway().UID
	if err := g.cl.Delete(g.ctx, discordBotSecret(g.agent)); err != nil {
		t.Fatal(err)
	}
	g.assertDark("Secret deleted", uid, g.pass())
	g.reportGatewayReady(0)

	// Park the install on the missing sandbox keypair, then bring the
	// backend back: every pass from here ends in the Degraded writer.
	if err := g.cl.Delete(g.ctx, shellSandboxKeysSecret(g.agent)); err != nil {
		t.Fatal(err)
	}
	if err := g.cl.Create(g.ctx, discordBotSecret(g.agent)); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		g.pass()
		if ready := meta.FindStatusCondition(g.agent.Status.Conditions, "Ready"); ready == nil || ready.Reason != reasonShellSandboxKeysMissing {
			t.Fatalf("precondition, pass %d: the install is not parked on the missing keypair: %+v", i+1, ready)
		}
		if got := g.replicas(); got != 1 {
			t.Errorf("parked pass %d: the gateway asks for %d replicas with the Secret back, want 1", i+1, got)
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

// TestTheDiscordSecretRoundTripScalesTheGatewayEnvtest: the #2057 shape, the
// discord-bot Secret taken from under a running gateway. The gateway goes to
// zero; the Secret recreated brings it back on the requeued pass, which is
// the only pass that can see it, since Secrets are not watched.
func TestTheDiscordSecretRoundTripScalesTheGatewayEnvtest(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	g := newDarkRig(t, "dark-discord", nil)
	if err := g.cl.Create(g.ctx, discordBotSecret(g.agent)); err != nil {
		t.Fatal(err)
	}
	g.settle()
	if got := g.replicas(); got != 1 {
		t.Fatalf("precondition: the Discord gateway asks for %d replicas, want 1", got)
	}
	g.reportGatewayReady(1)
	g.spawnSessionPod()
	uid := g.gateway().UID

	if err := g.cl.Delete(g.ctx, discordBotSecret(g.agent)); err != nil {
		t.Fatal(err)
	}
	res := g.pass()
	g.assertDark("Secret deleted", uid, res)
	g.sessionPodStillOwned("Secret deleted")

	if err := g.cl.Create(g.ctx, discordBotSecret(g.agent)); err != nil {
		t.Fatal(err)
	}
	// The requeue the dark pass returned is the pass that runs next; nothing
	// else would wake the reconcile for a Secret.
	if res.RequeueAfter != darkEnvtestRequeue {
		t.Fatalf("the dark pass asked for no requeue (%s); the recreated Secret would wait for an unrelated event", res.RequeueAfter)
	}
	g.pass()
	if g.gateway().UID != uid {
		t.Fatalf("Secret recreated: the gateway Deployment was replaced (UID %s, was %s)", g.gateway().UID, uid)
	}
	if got := g.replicas(); got != 1 {
		t.Errorf("Secret recreated: the gateway asks for %d replicas on the requeued pass, want 1", got)
	}
	g.sessionPodStillOwned("Secret recreated")
}

// TestChatOnTheCRBringsTheGatewayBackEnvtest: Google Chat under next is a
// backend by the CR alone. Disabling it darkens the existing gateway at
// zero; enabling it again, a CR edit the reconcile is woken by, brings it
// back on the same object.
func TestChatOnTheCRBringsTheGatewayBackEnvtest(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	g := newDarkRig(t, "dark-chat", func(a *agentv1alpha1.PlatformAgent) {
		a.Spec.Integration = gchatTestAgent("next", true).Spec.Integration
	})
	g.settle()
	if got := g.replicas(); got != 1 {
		t.Fatalf("precondition: the Chat gateway asks for %d replicas, want 1", got)
	}
	g.reportGatewayReady(1)
	uid := g.gateway().UID

	setChat := func(enabled bool) {
		t.Helper()
		agent := g.fresh()
		agent.Spec.Integration.GoogleChat.Enabled = ptr.To(enabled)
		if err := g.cl.Update(g.ctx, agent); err != nil {
			t.Fatalf("editing spec.integration.googleChat.enabled to %v: %v", enabled, err)
		}
		g.fresh()
	}
	setChat(false)
	res := g.pass()
	g.assertDark("Chat disabled", uid, res)
	if cond := g.gatewayCondition(); cond == nil || !strings.Contains(cond.Message, "spec.integration.googleChat") {
		t.Errorf("Chat disabled: the condition does not name the Chat integration as a remedy: %+v", cond)
	}

	setChat(true)
	g.pass()
	if g.gateway().UID != uid {
		t.Fatalf("Chat enabled: the gateway Deployment was replaced (UID %s, was %s)", g.gateway().UID, uid)
	}
	if got := g.replicas(); got != 1 {
		t.Errorf("Chat enabled: the gateway asks for %d replicas, want 1", got)
	}
}

// TestNoGatewayAndNoBackendStillWithholdsTheCreationEnvtest: the creation
// path is what it was. No Deployment and no backend: nothing is created, not
// even at zero replicas, and the condition says why.
func TestNoGatewayAndNoBackendStillWithholdsTheCreationEnvtest(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	g := newDarkRig(t, "dark-creation", nil)
	g.settle()
	res := g.pass()
	if err := g.cl.Get(g.ctx, g.gatewayKey(), &appsv1.Deployment{}); !errors.IsNotFound(err) {
		t.Fatalf("a gateway Deployment exists on an install that never had a backend (err=%v); the creation path withholds it", err)
	}
	want := g.creationPathReason()
	if cond := g.gatewayCondition(); cond == nil || cond.Reason != a2aGatewayDarkReason || cond.Message != want {
		t.Errorf("A2AGateway condition = %+v, want %s with %q", cond, a2aGatewayDarkReason, want)
	}
	if res.RequeueAfter != darkEnvtestRequeue {
		t.Errorf("the withheld pass requeued after %s, want %s", res.RequeueAfter, darkEnvtestRequeue)
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

// TestTheReplicaCountRoundTripsUnderTheOperatorsFieldManagerEnvtest: spec.replicas
// is the operator's field through 1, 0 and 1 again, applied by server-side
// apply under its one field manager, and a scale by another manager in
// between is taken back by the next apply without a conflict error.
func TestTheReplicaCountRoundTripsUnderTheOperatorsFieldManagerEnvtest(t *testing.T) {
	cl, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := a2aTestAgent()
	agent.Namespace = "dark-ssa"
	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	key := client.ObjectKeyFromObject(buildA2AGatewayDeployment(agent))
	apply := func(replicas int32) *appsv1.Deployment {
		t.Helper()
		dep := buildA2AGatewayDeployment(agent)
		dep.Spec.Replicas = ptr.To(replicas)
		if err := r.applyA2AGatewayDeployment(ctx, agent, dep); err != nil {
			t.Fatalf("applying the gateway at %d replicas: %v", replicas, err)
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

	up := apply(1)
	uid := up.UID
	check("created at 1", up, 1, "")
	check("scaled to 0", apply(0), 0, uid)

	// Another manager scales it through the scale subresource, as
	// `kubectl scale` does.
	scale := &autoscalingv1.Scale{Spec: autoscalingv1.ScaleSpec{Replicas: 2}}
	if err := cl.SubResource("scale").Update(ctx, up, client.WithSubResourceBody(scale), client.FieldOwner(darkEnvtestForeignManager)); err != nil {
		t.Fatalf("scaling the gateway as another manager: %v", err)
	}
	check("dark pass after a foreign scale", apply(0), 0, uid)
	check("scaled back to 1", apply(1), 1, uid)
}
