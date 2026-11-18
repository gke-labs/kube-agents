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

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func singleServerAgent(replicas int32) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: "kubeagents-system"}}
	agent.Status.BusReplicas = replicas
	return agent
}

func singleServerBusPod(agent *agentv1alpha1.PlatformAgent, uid string, phase corev1.PodPhase) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSName(agent) + "-0", Namespace: agent.Namespace, UID: types.UID(uid)},
		Status:     corev1.PodStatus{Phase: phase},
	}
}

// singleServerEvents drains the recorder: the Events written since the last call.
func singleServerEvents(rec *record.FakeRecorder) []string {
	var got []string
	for {
		select {
		case e := <-rec.Events:
			got = append(got, e)
		default:
			return got
		}
	}
}

func singleServerReconciler(t *testing.T, objs ...client.Object) (*PlatformAgentReconciler, client.Client, *record.FakeRecorder) {
	t.Helper()
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(objs...).Build()
	rec := record.NewFakeRecorder(16)
	return &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: setupScheme(), Recorder: rec}, cl, rec
}

const wantSingleServerEvent = "Normal A2ABusSingleServer the A2A bus runs one NATS server, so a node drain or upgrade takes it down, typically for about 2 minutes, and longer after a node scale-down; converting it to three servers is temporarily disabled: https://github.com/gke-labs/kube-agents/issues/6298"

// One Event for a bus pod first seen Running, word for word, and none on the
// passes after it while the same pod runs.
func TestASingleServerBusGetsOneEventPerPod(t *testing.T) {
	agent := singleServerAgent(1)
	r, _, rec := singleServerReconciler(t, agent, singleServerBusPod(agent, "uid-1", corev1.PodRunning))
	ctx := context.Background()
	r.noteA2ABusSingleServer(ctx, agent)
	if got := singleServerEvents(rec); len(got) != 1 || got[0] != wantSingleServerEvent {
		t.Fatalf("first pass: events %q, want exactly %q", got, wantSingleServerEvent)
	}
	for range 3 {
		r.noteA2ABusSingleServer(ctx, agent)
	}
	if got := singleServerEvents(rec); len(got) != 0 {
		t.Fatalf("repeat passes on the same pod wrote %q, want none", got)
	}
}

// A replaced pod (a drain, an upgrade, a scale-down) is a new episode.
func TestAReplacedSingleServerBusPodGetsANewEvent(t *testing.T) {
	agent := singleServerAgent(1)
	r, cl, rec := singleServerReconciler(t, agent, singleServerBusPod(agent, "uid-1", corev1.PodRunning))
	ctx := context.Background()
	r.noteA2ABusSingleServer(ctx, agent)
	singleServerEvents(rec)
	if err := cl.Delete(ctx, singleServerBusPod(agent, "uid-1", corev1.PodRunning)); err != nil {
		t.Fatal(err)
	}
	// The replacement, not yet Running: nothing yet.
	if err := cl.Create(ctx, singleServerBusPod(agent, "uid-2", corev1.PodPending)); err != nil {
		t.Fatal(err)
	}
	r.noteA2ABusSingleServer(ctx, agent)
	if got := singleServerEvents(rec); len(got) != 0 {
		t.Fatalf("a Pending replacement wrote %q, want none until it runs", got)
	}
	pod := singleServerBusPod(agent, "uid-2", corev1.PodRunning)
	if err := cl.Delete(ctx, pod); err != nil {
		t.Fatal(err)
	}
	if err := cl.Create(ctx, pod); err != nil {
		t.Fatal(err)
	}
	r.noteA2ABusSingleServer(ctx, agent)
	if got := singleServerEvents(rec); len(got) != 1 || got[0] != wantSingleServerEvent {
		t.Fatalf("the replacement running: events %q, want one", got)
	}
}

// Three servers keep the bus up through a drain: no Event.
func TestAThreeServerBusGetsNoSingleServerEvent(t *testing.T) {
	agent := singleServerAgent(3)
	r, _, rec := singleServerReconciler(t, agent, singleServerBusPod(agent, "uid-1", corev1.PodRunning))
	r.noteA2ABusSingleServer(context.Background(), agent)
	if got := singleServerEvents(rec); len(got) != 0 {
		t.Fatalf("a three-server bus wrote %q, want none", got)
	}
}

// A migration in flight is the conversion the Event points to: no Event.
func TestASingleServerBusMidMigrationGetsNoEvent(t *testing.T) {
	agent := singleServerAgent(1)
	agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Phase: string(busMigrationFreezing)}
	r, _, rec := singleServerReconciler(t, agent, singleServerBusPod(agent, "uid-1", corev1.PodRunning))
	r.noteA2ABusSingleServer(context.Background(), agent)
	if got := singleServerEvents(rec); len(got) != 0 {
		t.Fatalf("a migrating bus wrote %q, want none", got)
	}
}

// An operator restart (a fresh reconciler, so a fresh map) writes it once
// more for the pod already running, and only once.
func TestAnOperatorRestartWritesTheSingleServerEventOnceMore(t *testing.T) {
	agent := singleServerAgent(1)
	pod := singleServerBusPod(agent, "uid-1", corev1.PodRunning)
	r, cl, rec := singleServerReconciler(t, agent, pod)
	ctx := context.Background()
	r.noteA2ABusSingleServer(ctx, agent)
	singleServerEvents(rec)
	restarted := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: setupScheme(), Recorder: rec}
	restarted.noteA2ABusSingleServer(ctx, agent)
	restarted.noteA2ABusSingleServer(ctx, agent)
	if got := singleServerEvents(rec); len(got) != 1 {
		t.Fatalf("after a restart: events %q, want exactly one", got)
	}
}

// A run that ended stays on the status: a bus that came down to one server
// (Done), or whose move to three was abandoned or failed, still gets it.
func TestASingleServerBusAfterAnEndedMigrationGetsTheEvent(t *testing.T) {
	for _, phase := range []busMigrationPhase{busMigrationDone, busMigrationAbandoned, busMigrationFailed} {
		t.Run(string(phase), func(t *testing.T) {
			agent := singleServerAgent(1)
			agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Phase: string(phase)}
			r, _, rec := singleServerReconciler(t, agent, singleServerBusPod(agent, "uid-1", corev1.PodRunning))
			r.noteA2ABusSingleServer(context.Background(), agent)
			if got := singleServerEvents(rec); len(got) != 1 || got[0] != wantSingleServerEvent {
				t.Fatalf("after a %s run: events %q, want one", phase, got)
			}
		})
	}
}

// A pod already terminating, though still Running, is on its way out: the
// Event waits for its replacement.
func TestATerminatingSingleServerBusPodGetsNoEvent(t *testing.T) {
	agent := singleServerAgent(1)
	pod := singleServerBusPod(agent, "uid-1", corev1.PodRunning)
	now := metav1.Now()
	pod.DeletionTimestamp = &now
	pod.Finalizers = []string{"kubeagents.test/hold"}
	r, _, rec := singleServerReconciler(t, agent, pod)
	r.noteA2ABusSingleServer(context.Background(), agent)
	if got := singleServerEvents(rec); len(got) != 0 {
		t.Fatalf("a terminating pod wrote %q, want none", got)
	}
}

// A bus that went to three servers and back is a new episode, even on the
// same pod: the three-server pass dropped the record.
func TestAReturnToOneServerIsANewEpisode(t *testing.T) {
	agent := singleServerAgent(1)
	r, _, rec := singleServerReconciler(t, agent, singleServerBusPod(agent, "uid-1", corev1.PodRunning))
	ctx := context.Background()
	r.noteA2ABusSingleServer(ctx, agent)
	singleServerEvents(rec)
	agent.Status.BusReplicas = 3
	r.noteA2ABusSingleServer(ctx, agent)
	agent.Status.BusReplicas = 1
	r.noteA2ABusSingleServer(ctx, agent)
	if got := singleServerEvents(rec); len(got) != 1 {
		t.Fatalf("back to one server: events %q, want one", got)
	}
}
