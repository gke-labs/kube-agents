// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package controller

import (
	"context"
	"slices"
	"testing"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const parkStreamsMissingMsg = "Missing from the A2A bus on 3 polls in a row: TASKS."

// parkedBusStreamsRig is a settled next install parked on one of the two
// refusals that return before updateStatusReady (#5707): the provision Job
// failed, or the sandbox keys Secret is gone.
func parkedBusStreamsRig(t *testing.T, park string) (*PlatformAgentReconciler, client.Client, ctrl.Request) {
	t.Helper()
	return parkedBusStreamsRigWith(t, park, nil)
}

// parkedBusStreamsRigWith is parkedBusStreamsRig with an RBAC checker, nil
// for none.
func parkedBusStreamsRigWith(t *testing.T, park string, checker *RBACChecker) (*PlatformAgentReconciler, client.Client, ctrl.Request) {
	t.Helper()
	r, cl, req := settledStuckPodReconciler(t, checker)
	ctx := context.Background()
	agent := a2aTestAgent()
	var want string
	switch park {
	case "A2AProvisionFailed":
		job := &batchv1.Job{}
		if err := cl.Get(ctx, types.NamespacedName{Name: buildA2AProvisionJob(agent).Name, Namespace: agent.Namespace}, job); err != nil {
			t.Fatalf("get the provision Job: %v", err)
		}
		job.Status.Conditions = []batchv1.JobCondition{{
			Type: batchv1.JobFailed, Status: corev1.ConditionTrue,
			Reason: "BackoffLimitExceeded", Message: "the provision script exited 1",
		}}
		if err := cl.Status().Update(ctx, job); err != nil {
			t.Fatalf("mark the Job failed: %v", err)
		}
		want = "A2AProvisionFailed"
	case reasonShellSandboxKeysMissing:
		if err := cl.Delete(ctx, sandboxKeysSecret(agent)); err != nil {
			t.Fatalf("delete the sandbox keys Secret: %v", err)
		}
		want = reasonShellSandboxKeysMissing
	default:
		t.Fatalf("unknown park %q", park)
	}
	busGatePass(t, ctx, r, req, "park")
	if ready := meta.FindStatusCondition(parkedAgent(t, cl, req).Status.Conditions, "Ready"); ready == nil || ready.Reason != want {
		t.Fatalf("precondition: the install is not parked on %s: Ready = %+v", want, ready)
	}
	return r, cl, req
}

func parkedAgent(t *testing.T, cl client.Client, req ctrl.Request) *agentv1alpha1.PlatformAgent {
	t.Helper()
	agent := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(context.Background(), req.NamespacedName, agent); err != nil {
		t.Fatal(err)
	}
	return agent
}

// setParkedCondition writes a status condition as another writer would (the
// bus-stream poller, reportRBACSkew, an earlier unparked pass).
func setParkedCondition(t *testing.T, cl client.Client, req ctrl.Request, cond metav1.Condition) {
	t.Helper()
	agent := parkedAgent(t, cl, req)
	meta.SetStatusCondition(&agent.Status.Conditions, cond)
	if err := cl.Status().Update(context.Background(), agent); err != nil {
		t.Fatal(err)
	}
}

func busStreamsPresent(status metav1.ConditionStatus) metav1.Condition {
	if status == metav1.ConditionFalse {
		return metav1.Condition{Type: busStreamsPresentConditionType, Status: status, Reason: busStreamsReasonMissing, Message: parkStreamsMissingMsg}
	}
	return metav1.Condition{Type: busStreamsPresentConditionType, Status: status, Reason: busStreamsReasonAllPresent, Message: "All present."}
}

var bothParks = []string{"A2AProvisionFailed", reasonShellSandboxKeysMissing}

// While parked, Degraded follows BusStreamsPresent: it reads
// A2ABusStreamsMissing with the poller's message while the streams are
// missing, a repeat pass writes nothing, and it goes once they are back.
func TestAParkedPassKeepsDegradedWithTheBusStreams(t *testing.T) {
	for _, park := range bothParks {
		t.Run(park, func(t *testing.T) {
			r, cl, req := parkedBusStreamsRig(t, park)
			ctx := context.Background()

			setParkedCondition(t, cl, req, busStreamsPresent(metav1.ConditionFalse))
			busGatePass(t, ctx, r, req, "streams missing")
			agent := parkedAgent(t, cl, req)
			if c := meta.FindStatusCondition(agent.Status.Conditions, degradedConditionType); c == nil || c.Status != metav1.ConditionTrue ||
				c.Reason != conditionReasonA2ABusStreamsMissing || c.Message != parkStreamsMissingMsg {
				t.Fatalf("Degraded = %+v, want True/%s with the poller's message", c, conditionReasonA2ABusStreamsMissing)
			}
			if ready := meta.FindStatusCondition(agent.Status.Conditions, "Ready"); ready == nil || ready.Reason != park {
				t.Fatalf("Ready = %+v: the park itself must stand", ready)
			}

			rv := agent.ResourceVersion
			busGatePass(t, ctx, r, req, "streams still missing")
			if got := parkedAgent(t, cl, req).ResourceVersion; got != rv {
				t.Errorf("a repeat parked pass wrote status (resourceVersion %s -> %s)", rv, got)
			}

			setParkedCondition(t, cl, req, busStreamsPresent(metav1.ConditionTrue))
			busGatePass(t, ctx, r, req, "streams back")
			if c := meta.FindStatusCondition(parkedAgent(t, cl, req).Status.Conditions, degradedConditionType); c != nil {
				t.Errorf("Degraded = %+v after the streams came back, want none", c)
			}
		})
	}
}

// A reason ranked above the streams, which the park does not evaluate, stays
// while the streams are missing.
func TestAParkedPassLeavesAReasonRankedAboveTheStreams(t *testing.T) {
	r, cl, req := parkedBusStreamsRig(t, reasonShellSandboxKeysMissing)
	setParkedCondition(t, cl, req, metav1.Condition{Type: degradedConditionType, Status: metav1.ConditionTrue, Reason: conditionReasonMinterPruningHeld, Message: "held"})
	setParkedCondition(t, cl, req, busStreamsPresent(metav1.ConditionFalse))
	busGatePass(t, context.Background(), r, req, "streams missing under a minter hold")
	if c := meta.FindStatusCondition(parkedAgent(t, cl, req).Status.Conditions, degradedConditionType); c == nil || c.Reason != conditionReasonMinterPruningHeld {
		t.Errorf("Degraded = %+v, want %s kept", c, conditionReasonMinterPruningHeld)
	}
}

// With the streams present, the park removes only its own reason: any other
// Degraded is left, RBACIncomplete in particular being reportRBACSkew's.
func TestAParkClearsOnlyTheStreamsReason(t *testing.T) {
	agent := a2aTestAgent()
	meta.SetStatusCondition(&agent.Status.Conditions, busStreamsPresent(metav1.ConditionTrue))
	for _, reason := range []string{reasonRBACIncomplete, conditionReasonInvalidCredentialProxyResources, conditionReasonMinterPruningHeld} {
		meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{Type: degradedConditionType, Status: metav1.ConditionTrue, Reason: reason, Message: "m"})
		if edit, _ := parkBusStreamsDegraded(agent); edit != busStreamsDegradedKeep {
			t.Errorf("Degraded/%s with the streams present: edit %d, want keep", reason, edit)
		}
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{Type: degradedConditionType, Status: metav1.ConditionTrue, Reason: conditionReasonA2ABusStreamsMissing, Message: "m"})
	if edit, _ := parkBusStreamsDegraded(agent); edit != busStreamsDegradedClear {
		t.Errorf("Degraded/%s with the streams present: edit %d, want clear", conditionReasonA2ABusStreamsMissing, edit)
	}
}

// Parked under an out-of-date ClusterRole: the streams' reason takes Degraded
// from RBACIncomplete while they are missing, as updateStatusReady ranks it,
// and reportRBACSkew takes it back once they return. Neither writer rewrites
// the other's condition on a repeat pass.
func TestAParkedPassHandsDegradedBackToTheRBACCheck(t *testing.T) {
	checker := NewRBACChecker((&fakeAuthorizer{deny: denyPDBPatch}).reviews(), "")
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("boot probe: %v", err)
	}
	r, cl, req := parkedBusStreamsRigWith(t, reasonShellSandboxKeysMissing, checker)
	ctx := context.Background()
	reason := func() string {
		t.Helper()
		c := meta.FindStatusCondition(parkedAgent(t, cl, req).Status.Conditions, degradedConditionType)
		if c == nil {
			return ""
		}
		return c.Reason
	}
	if got := reason(); got != reasonRBACIncomplete {
		t.Fatalf("precondition: Degraded reads %q, want %s", got, reasonRBACIncomplete)
	}
	setParkedCondition(t, cl, req, busStreamsPresent(metav1.ConditionFalse))
	busGatePass(t, ctx, r, req, "streams missing under RBAC skew")
	if got := reason(); got != conditionReasonA2ABusStreamsMissing {
		t.Fatalf("Degraded reads %q with the streams missing, want %s", got, conditionReasonA2ABusStreamsMissing)
	}
	rv := parkedAgent(t, cl, req).ResourceVersion
	busGatePass(t, ctx, r, req, "repeat")
	if got := parkedAgent(t, cl, req).ResourceVersion; got != rv {
		t.Errorf("a repeat pass wrote status (resourceVersion %s -> %s): the two writers fight", rv, got)
	}
	setParkedCondition(t, cl, req, busStreamsPresent(metav1.ConditionTrue))
	busGatePass(t, ctx, r, req, "streams back")
	busGatePass(t, ctx, r, req, "streams back, next pass")
	if got := reason(); got != reasonRBACIncomplete {
		t.Errorf("Degraded reads %q after the streams came back, want %s again", got, reasonRBACIncomplete)
	}
}

// The reasons a park keeps over the streams are the ones updateStatusReady's
// chain checks before them. A reason added ahead of the streams in the chain
// goes ahead of them in degradedConditionReasons too, and this list with it.
func TestTheReasonsRankedAboveTheStreamsAreTheChainsFirstThree(t *testing.T) {
	want := []string{conditionReasonInvalidGitRepoURL, conditionReasonCorruptManagedRepos, conditionReasonMinterPruningHeld}
	var got []string
	for _, reason := range degradedConditionReasons {
		if degradedRanksAboveBusStreams(reason) {
			got = append(got, reason)
		}
	}
	if !slices.Equal(got, want) {
		t.Errorf("ranked above the bus streams: %v, want %v", got, want)
	}
	if degradedRanksAboveBusStreams(conditionReasonA2ABusStreamsMissing) || degradedRanksAboveBusStreams(reasonRBACIncomplete) {
		t.Error("the streams reason and RBACIncomplete must not rank above the streams")
	}
}

// A spec refusal the streams' reason outranked comes back when the streams
// do, as updateStatusReady would show it, rather than leaving Degraded empty
// for the rest of the park.
func TestAParkGivesDegradedBackToTheSpecRefusalTheStreamsOutranked(t *testing.T) {
	agent := a2aTestAgent()
	if agent.Spec.Deployment == nil {
		agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{}
	}
	agent.Spec.Deployment.CredentialProxy = &agentv1alpha1.CredentialProxySpec{Resources: &corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("512Mi")},
	}}
	wantReason, wantMsg := specResourceDegraded(agent)
	if wantReason != conditionReasonInvalidCredentialProxyResources {
		t.Fatalf("precondition: the spec refusal reads %q, want %s", wantReason, conditionReasonInvalidCredentialProxyResources)
	}

	meta.SetStatusCondition(&agent.Status.Conditions, busStreamsPresent(metav1.ConditionFalse))
	if edit, cond := parkBusStreamsDegraded(agent); edit != busStreamsDegradedSet || cond.Reason != conditionReasonA2ABusStreamsMissing {
		t.Fatalf("streams missing over a spec refusal: edit %d %+v, want the streams' reason", edit, cond)
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{Type: degradedConditionType, Status: metav1.ConditionTrue, Reason: conditionReasonA2ABusStreamsMissing, Message: parkStreamsMissingMsg})

	meta.SetStatusCondition(&agent.Status.Conditions, busStreamsPresent(metav1.ConditionTrue))
	edit, cond := parkBusStreamsDegraded(agent)
	if edit != busStreamsDegradedSet || cond.Reason != wantReason || cond.Message != wantMsg {
		t.Errorf("streams back: edit %d %+v, want %s with its message", edit, cond, wantReason)
	}
}

// A poller message that changes while parked (another set of missing
// streams) is rewritten, so Degraded names what is missing now.
func TestAParkFollowsTheStreamsMessage(t *testing.T) {
	agent := a2aTestAgent()
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{Type: degradedConditionType, Status: metav1.ConditionTrue, Reason: conditionReasonA2ABusStreamsMissing, Message: parkStreamsMissingMsg})
	now := busStreamsPresent(metav1.ConditionFalse)
	now.Message = "Missing from the A2A bus on 3 polls in a row: KV_cap."
	meta.SetStatusCondition(&agent.Status.Conditions, now)
	if edit, cond := parkBusStreamsDegraded(agent); edit != busStreamsDegradedSet || cond.Message != now.Message {
		t.Errorf("edit %d %+v, want the new message %q", edit, cond, now.Message)
	}
}

// A spec edit during a park (a new generation) with the streams still
// missing brings Degraded's observedGeneration level with Ready's, once:
// the next pass at the same generation keeps it.
func TestAParkMovesDegradedToANewGeneration(t *testing.T) {
	agent := a2aTestAgent()
	agent.Generation = 4
	meta.SetStatusCondition(&agent.Status.Conditions, busStreamsPresent(metav1.ConditionFalse))
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type: degradedConditionType, Status: metav1.ConditionTrue, Reason: conditionReasonA2ABusStreamsMissing,
		Message: parkStreamsMissingMsg, ObservedGeneration: 3,
	})
	edit, cond := parkBusStreamsDegraded(agent)
	if edit != busStreamsDegradedSet || cond.ObservedGeneration != 4 {
		t.Fatalf("Degraded a generation behind: edit %d %+v, want a write at generation 4", edit, cond)
	}
	applyParkBusStreamsDegraded(agent, edit, cond, metav1.Now())
	if edit, _ := parkBusStreamsDegraded(agent); edit != busStreamsDegradedKeep {
		t.Errorf("the pass after the write: edit %d, want keep", edit)
	}
}
