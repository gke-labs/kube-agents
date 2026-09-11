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
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The provision Job's name is the only lever the operator has on it: the pod
// template is immutable and reconcileA2A is create-if-absent, so a rendered
// change reaches an existing install only if the name moves (#1347). Three
// properties have to hold at once, and each guards against a different way of
// getting the digest wrong: it must not move between two renders of the same
// agent, it must move when only the pod spec changes, and it must still move
// when only the script changes.

// a2aProvisionScriptOf returns the container command's script body, and fails
// the test if the command is not the `sh -c <script>` shape the render uses,
// so an edit to the shape is a loud failure rather than a test that quietly
// compares two empty strings.
func a2aProvisionScriptOf(t *testing.T, job *batchv1.Job) string {
	t.Helper()
	cmd := job.Spec.Template.Spec.Containers[0].Command
	if len(cmd) != 3 || cmd[0] != "sh" || cmd[1] != "-c" {
		t.Fatalf("provision command is %q, want sh -c <script>", cmd)
	}
	return cmd[2]
}

// TestA2AProvisionJobNameIsDeterministic is the failure mode the issue warned
// about: a map iterated in render order or a timestamp in the template would
// digest differently on every call, and a name that moves between reconciles
// creates a Job on every pass. Rendered repeatedly, and then through the
// reconciler against a fake API, where the symptom would be a growing Job
// list.
func TestA2AProvisionJobNameIsDeterministic(t *testing.T) {
	agent := a2aTestAgent()
	first := buildA2AProvisionJob(agent).Name
	const wantPrefix = "test-agent-a2a-provision-"
	if !strings.HasPrefix(first, wantPrefix) {
		t.Fatalf("job name = %q, want prefix %q", first, wantPrefix)
	}
	if got := len(strings.TrimPrefix(first, wantPrefix)); got != a2aProvisionJobNameHashLength {
		t.Errorf("digest suffix is %d characters, want %d", got, a2aProvisionJobNameHashLength)
	}
	for i := 0; i < 8; i++ {
		if got := buildA2AProvisionJob(agent).Name; got != first {
			t.Fatalf("render %d named the Job %q, the first render %q; the digest is not deterministic", i+2, got, first)
		}
	}

	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		if _, err := r.reconcileA2A(ctx, agent); err != nil {
			t.Fatalf("reconcileA2A %d: %v", i+1, err)
		}
	}
	jobs := &batchv1.JobList{}
	if err := cl.List(ctx, jobs); err != nil {
		t.Fatalf("list Jobs: %v", err)
	}
	if len(jobs.Items) != 1 {
		t.Fatalf("three reconciles left %d provision Jobs, want 1; a name that moves between renders creates one per pass", len(jobs.Items))
	}
	if got := jobs.Items[0].Name; got != first {
		t.Errorf("the reconciler created %q, the builder renders %q", got, first)
	}
}

// TestA2AProvisionJobNameTracksThePodSpec is the invariant #1347 asked for:
// two renders that differ only in pod spec get two names. The image is the
// change because it is the one the issue predicts will recur — a nats-box
// bump for a CVE — and because it leaves the script untouched, which the
// guard below checks so the test cannot pass on a script difference by
// accident. On the script-only digest this rendered the same name twice.
func TestA2AProvisionJobNameTracksThePodSpec(t *testing.T) {
	agent := a2aTestAgent()
	before := buildA2AProvisionJob(agent)
	t.Setenv(a2aProvisionImageEnvVar, "example.com/nats-box:pinned")
	after := buildA2AProvisionJob(agent)

	if a2aProvisionScriptOf(t, before) != a2aProvisionScriptOf(t, after) {
		t.Fatal("the image override changed the script too; this test would not isolate the pod spec")
	}
	if before.Spec.Template.Spec.Containers[0].Image == after.Spec.Template.Spec.Containers[0].Image {
		t.Fatal("the image override did not reach the render; the comparison below is inert")
	}
	if before.Name == after.Name {
		t.Errorf("image changed and the Job is still %q; the name does not cover the pod spec, so this change never reaches an existing install", before.Name)
	}
}

// TestA2AProvisionJobNameTracksTheScript keeps what the script-only digest
// already had: a payload change is a new Job. The render takes no script
// input, so this edits the rendered command and re-derives the name through
// a2aProvisionJobName, the same call the builder makes. A second case edits a
// field of the pod spec the image test does not reach, so a digest narrowed
// to "image plus script" would fail here.
func TestA2AProvisionJobNameTracksTheScript(t *testing.T) {
	agent := a2aTestAgent()
	job := buildA2AProvisionJob(agent)
	base := a2aProvisionJobName(agent, job.Spec)
	if base != job.Name {
		t.Fatalf("a2aProvisionJobName renders %q, the builder named the Job %q", base, job.Name)
	}

	script := job.DeepCopy()
	script.Spec.Template.Spec.Containers[0].Command[2] += "\necho edited\n"
	if got := a2aProvisionJobName(agent, script.Spec); got == base {
		t.Errorf("script changed and the name is still %q", got)
	}

	// Derived from what the render produced, not written as a literal. This
	// case was a literal "/tmp" until #1272 moved the rendered WorkingDir to
	// /tmp, which made the assignment a no-op and left the check below
	// comparing a spec against itself. Appending keeps it a real edit
	// wherever the render moves next, the same way the script case above
	// appends rather than substituting.
	workdir := job.DeepCopy()
	workdir.Spec.Template.Spec.Containers[0].WorkingDir += "/subdir"
	if got := a2aProvisionJobName(agent, workdir.Spec); got == base {
		t.Errorf("WorkingDir changed and the name is still %q; this is the #1259 fix the old digest could not deliver", got)
	}
}

// TestReconcileA2ACreatesANewProvisionJobWhenThePodSpecChanges is the naming
// change seen from the reconciler: an install carrying the Job rendered by
// one operator build gets a second Job, under a new name and with the new
// spec, from the next. The superseded Job is left where it is — the operator
// does not delete it — which this test pins as the current behaviour rather
// than a goal. The superseded Job is also marked Failed before the second
// reconcile, because the live case is a crash-looping old generation next to
// a healthy new one, and the status scan has to read the current Job only: a
// scan that folded conditions across generations would park the phase on
// A2AProvisionFailed for a Job the operator has already moved past.
func TestReconcileA2ACreatesANewProvisionJobWhenThePodSpecChanges(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	// Job is registered with a status subresource so the Failed condition
	// below goes through Status().Update the way the Job controller writes
	// it; a plain Update on this fake drops the status.
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &batchv1.Job{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()

	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatalf("reconcileA2A with the default image: %v", err)
	}
	jobs := &batchv1.JobList{}
	if err := cl.List(ctx, jobs); err != nil {
		t.Fatalf("list Jobs: %v", err)
	}
	if len(jobs.Items) != 1 {
		t.Fatalf("%d provision Jobs after the first reconcile, want 1", len(jobs.Items))
	}
	original := jobs.Items[0]
	original.Status.Conditions = []batchv1.JobCondition{{
		Type: batchv1.JobFailed, Status: corev1.ConditionTrue,
		Reason: "BackoffLimitExceeded", Message: "Job has reached the specified backoff limit",
	}}
	if err := cl.Status().Update(ctx, &original); err != nil {
		t.Fatalf("mark the original Job failed: %v", err)
	}
	// Guard against an inert assertion below: if the fake dropped the
	// condition on write, "not failed" would prove nothing.
	stored := &batchv1.Job{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(&original), stored); err != nil {
		t.Fatalf("re-read the original Job: %v", err)
	}
	if len(stored.Status.Conditions) == 0 || stored.Status.Conditions[0].Type != batchv1.JobFailed {
		t.Fatalf("the Failed condition did not persist on the original Job; the phase assertion below would be inert")
	}

	const pinned = "example.com/nats-box:pinned"
	t.Setenv(a2aProvisionImageEnvVar, pinned)
	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatalf("reconcileA2A with the image override: %v", err)
	}
	if state.failed || state.done {
		t.Errorf("state after the rename = {done:%v failed:%v}, want neither: the superseded Job's Failed condition must not reach the phase, and the new Job has not run", state.done, state.failed)
	}
	if err := cl.List(ctx, jobs); err != nil {
		t.Fatalf("list Jobs: %v", err)
	}
	if len(jobs.Items) != 2 {
		t.Fatalf("%d provision Jobs after the image changed, want 2: the old one left alone and a new one carrying the change", len(jobs.Items))
	}
	for _, job := range jobs.Items {
		image := job.Spec.Template.Spec.Containers[0].Image
		switch job.Name {
		case original.Name:
			if image == pinned {
				t.Errorf("the original Job %q now carries the new image; Jobs are immutable, so something rewrote it", job.Name)
			}
		default:
			if image != pinned {
				t.Errorf("the new Job %q carries %q, want %q", job.Name, image, pinned)
			}
		}
	}
}
