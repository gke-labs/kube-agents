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

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// A provision Job whose pods keep failing is retried up to its backoffLimit
// before JobFailed (and A2AProvisionFailed) appears, which can take over an
// hour. Meanwhile A2AProvisionRetrying says so, with one Warning event per
// newly failed pod, and leaves BusProvisioned and the phase alone (#7100).
func TestAFailingProvisionJobRerunIsReportedWithoutUnprovisioningTheBus(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	recorder := record.NewFakeRecorder(64)
	r.Recorder = recorder
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		busGatePass(t, ctx, r, req, "install")
	}
	theCalloutIsRolledOut(t, ctx, cl, agent)
	completeTheProvisionJob(t, ctx, cl, agent)
	busGatePass(t, ctx, r, req, "provisioned")

	stored := func() *agentv1alpha1.PlatformAgent {
		t.Helper()
		fresh := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
			t.Fatalf("get agent: %v", err)
		}
		return fresh
	}
	if !meta.IsStatusConditionTrue(stored().Status.Conditions, busProvisionedConditionType) {
		t.Fatalf("%s is not True after the Job completed; the rig does not reach a provisioned bus", busProvisionedConditionType)
	}
	jobName := buildA2AProvisionJob(agent).Name
	setJob := func(failed int32, cond batchv1.JobConditionType) {
		t.Helper()
		job := &batchv1.Job{}
		if err := cl.Get(ctx, types.NamespacedName{Name: jobName, Namespace: agent.Namespace}, job); err != nil {
			t.Fatalf("get provision Job: %v", err)
		}
		job.Status.Succeeded, job.Status.CompletionTime, job.Status.Conditions = 0, nil, nil
		job.Status.Failed = failed
		if cond != "" {
			job.Status.Conditions = []batchv1.JobCondition{{Type: cond, Status: corev1.ConditionTrue, Reason: "BackoffLimitExceeded", LastTransitionTime: metav1.Now()}}
		}
		if err := cl.Status().Update(ctx, job); err != nil {
			t.Fatalf("update provision Job status: %v", err)
		}
	}
	warnings := func() int {
		t.Helper()
		n := 0
		for len(recorder.Events) > 0 {
			if e := <-recorder.Events; strings.HasPrefix(e, corev1.EventTypeWarning+" "+a2aProvisionRetryingReason+" ") {
				n++
			}
		}
		return n
	}
	_ = warnings()

	// The re-run (the TTL's, or a digest change's) has three failed pods.
	setJob(3, "")
	busGatePass(t, ctx, r, req, "re-run retrying")
	got := stored()
	c := meta.FindStatusCondition(got.Status.Conditions, a2aProvisionRetryingConditionType)
	if c == nil || c.Status != metav1.ConditionTrue || c.Reason != a2aProvisionRetryingReason ||
		!strings.Contains(c.Message, jobName) || !strings.Contains(c.Message, "3 failed pod") || !strings.Contains(c.Message, "kubectl logs") {
		t.Errorf("%s = %+v with the Job retrying after 3 failed pods, want True naming the Job, the count and the logs command", a2aProvisionRetryingConditionType, c)
	}
	if !meta.IsStatusConditionTrue(got.Status.Conditions, busProvisionedConditionType) {
		t.Errorf("%s was dropped by a retrying re-run; it is a provisioned-once record", busProvisionedConditionType)
	}
	if got.Status.Phase == "Degraded" {
		t.Errorf("phase = Degraded while the Job is still retrying; only a Job that gave up parks the install")
	}
	if n := warnings(); n != 1 {
		t.Errorf("%d %s Warning events on the first failed pods, want 1", n, a2aProvisionRetryingReason)
	}

	// Nothing new failed: no second event.
	busGatePass(t, ctx, r, req, "re-run retrying, unchanged")
	if n := warnings(); n != 0 {
		t.Errorf("%d %s Warning events on a pass with no new failed pod, want 0", n, a2aProvisionRetryingReason)
	}

	// Another pod failed: the count moves and one more event.
	setJob(4, "")
	busGatePass(t, ctx, r, req, "re-run retrying, one more failure")
	if c := meta.FindStatusCondition(stored().Status.Conditions, a2aProvisionRetryingConditionType); c == nil || !strings.Contains(c.Message, "4 failed pod") {
		t.Errorf("%s = %+v after a fourth failed pod, want the count updated", a2aProvisionRetryingConditionType, c)
	}
	if n := warnings(); n != 1 {
		t.Errorf("%d %s Warning events on a newly failed pod, want 1", n, a2aProvisionRetryingReason)
	}

	// It completes: the condition goes.
	completeTheProvisionJob(t, ctx, cl, agent)
	busGatePass(t, ctx, r, req, "re-run complete")
	if c := meta.FindStatusCondition(stored().Status.Conditions, a2aProvisionRetryingConditionType); c != nil {
		t.Errorf("%s stayed after the Job completed: %+v", a2aProvisionRetryingConditionType, c)
	}

	// Retrying again, then it gives up: A2AProvisionFailed takes over and
	// the retrying condition goes with it, through the Degraded path.
	setJob(2, "")
	busGatePass(t, ctx, r, req, "retrying before giving up")
	if c := meta.FindStatusCondition(stored().Status.Conditions, a2aProvisionRetryingConditionType); c == nil {
		t.Fatalf("%s absent while retrying; the give-up assertion below would be inert", a2aProvisionRetryingConditionType)
	}
	setJob(20, batchv1.JobFailed)
	busGatePass(t, ctx, r, req, "gave up")
	got = stored()
	if ready := meta.FindStatusCondition(got.Status.Conditions, "Ready"); ready == nil || ready.Reason != "A2AProvisionFailed" {
		t.Errorf("Ready = %+v after the Job failed, want reason A2AProvisionFailed", ready)
	}
	if c := meta.FindStatusCondition(got.Status.Conditions, a2aProvisionRetryingConditionType); c != nil {
		t.Errorf("%s stayed after the Job gave up, beside A2AProvisionFailed: %+v", a2aProvisionRetryingConditionType, c)
	}
}

// A Job with failed pods that has already reached a terminal condition is
// not retrying: Complete after earlier failures, or Failed.
func TestProvisionRetryingIsOnlyForAJobStillRetrying(t *testing.T) {
	for _, tc := range []struct {
		name   string
		failed int32
		cond   batchv1.JobConditionType
		want   bool
	}{
		{"no failed pods", 0, "", false},
		{"failed pods, still running", 2, "", true},
		{"complete after failed pods", 2, batchv1.JobComplete, false},
		{"failed", 20, batchv1.JobFailed, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := a2aTestAgent()
			r, cl, _ := a2aGateTestReconciler(t, agent)
			ctx := context.Background()
			theCalloutIsServing(t, ctx, cl, r, agent)
			if _, err := r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
				t.Fatalf("reconcileA2A: %v", err)
			}
			job := &batchv1.Job{}
			if err := cl.Get(ctx, types.NamespacedName{Name: buildA2AProvisionJob(agent).Name, Namespace: agent.Namespace}, job); err != nil {
				t.Fatalf("get provision Job (the rig did not create it): %v", err)
			}
			job.Status.Failed = tc.failed
			if tc.cond != "" {
				job.Status.Conditions = []batchv1.JobCondition{{Type: tc.cond, Status: corev1.ConditionTrue}}
			}
			if err := cl.Status().Update(ctx, job); err != nil {
				t.Fatalf("update Job status: %v", err)
			}
			state, err := r.reconcileA2A(ctx, agent, homeChannelRecord{})
			if err != nil {
				t.Fatalf("reconcileA2A: %v", err)
			}
			if got := state.provisionRetrying != ""; got != tc.want {
				t.Errorf("provisionRetrying = %q, want set=%v", state.provisionRetrying, tc.want)
			}
		})
	}
}
