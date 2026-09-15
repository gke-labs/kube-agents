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

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The provision Job's name moves when its render changes, and the Job it
// moved past has to leave (#1389). A superseded Job whose pod never ran has no
// terminal condition, so the TTL never starts and it holds a pod-quota slot
// until a mode flip or agent deletion. reconcileA2A now sweeps every provision
// Job of this agent that is not the current render's, with background
// propagation so the pod goes with it, and leaves everything else alone.

// a2aProvisionJobOwnedBy returns a provision Job under the agent's labels and
// controller reference, the shape reconcileA2A would have created for an
// earlier render, named as the caller says. The fake client ignores
// propagation policy on Delete, so the interceptor in the tests below is what
// observes it.
func a2aProvisionJobOwnedBy(t *testing.T, agent *agentv1alpha1.PlatformAgent, name string) *batchv1.Job {
	t.Helper()
	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aLabels(agent, a2aProvisionComponent)},
	}
	if err := ctrl.SetControllerReference(agent, job, setupScheme()); err != nil {
		t.Fatalf("owner reference on %s: %v", name, err)
	}
	return job
}

// recordingDeletes captures each Delete the reconciler issues, by object
// name, with the propagation policy it asked for.
type recordingDeletes struct {
	propagation map[string]*metav1.DeletionPropagation
}

func (rec *recordingDeletes) funcs() interceptor.Funcs {
	return interceptor.Funcs{
		Patch: fakeServerSideApplyInterceptors().Patch,
		Delete: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
			options := &client.DeleteOptions{}
			options.ApplyOptions(opts)
			rec.propagation[obj.GetName()] = options.PropagationPolicy
			return c.Delete(ctx, obj, opts...)
		},
	}
}

// TestReconcileA2ADeletesSupersededProvisionJobs is the fix seen from the
// reconciler. Four Jobs sit in the namespace before the pass: a superseded
// provision Job of this agent, which must go; a provision Job of another
// agent instance, a non-provision Job of this agent, and a provision-labelled
// Job this agent does not own, each of which must survive. The two subtests
// are the two ways the current Job can stand when the sweep runs: absent, so
// this pass creates it (a render change on a running operator), and already
// present (an operator carrying this sweep for the first time over an install
// whose superseded Jobs predate it, where no create will ever happen again
// under the current name).
func TestReconcileA2ADeletesSupersededProvisionJobs(t *testing.T) {
	for _, tc := range []struct {
		name           string
		currentPresent bool
	}{
		{"current Job created by this pass", false},
		{"current Job already present", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			agent := a2aTestAgent()
			current := buildA2AProvisionJob(agent)
			if got := current.Labels[a2aComponentLabel]; got != a2aProvisionComponent {
				t.Fatalf("the builder labels the provision Job %q, the sweep selects %q; they must agree or the sweep is inert", got, a2aProvisionComponent)
			}

			superseded := a2aProvisionJobOwnedBy(t, agent, "test-agent-a2a-provision-00000000")
			if superseded.Name == current.Name {
				t.Fatalf("the superseded name collides with the current render %q; pick another", current.Name)
			}

			otherAgent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "other-agent", Namespace: agent.Namespace, UID: "other-uid"}}
			otherInstance := a2aProvisionJobOwnedBy(t, otherAgent, "other-agent-a2a-provision-00000000")

			nonProvision := a2aProvisionJobOwnedBy(t, agent, "test-agent-unrelated-job")
			nonProvision.Labels[a2aComponentLabel] = "not-provision"

			unowned := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{
				Name: "test-agent-a2a-provision-unowned", Namespace: agent.Namespace,
				Labels: a2aLabels(agent, a2aProvisionComponent),
			}}

			objects := []client.Object{agent, superseded, otherInstance, nonProvision, unowned}
			if tc.currentPresent {
				preexisting := a2aProvisionJobOwnedBy(t, agent, current.Name)
				preexisting.Spec = current.Spec
				objects = append(objects, preexisting)
			}

			rec := &recordingDeletes{propagation: map[string]*metav1.DeletionPropagation{}}
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(objects...).
				WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
				WithInterceptorFuncs(rec.funcs()).
				Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
			ctx := context.Background()

			if _, err := r.reconcileA2A(ctx, agent); err != nil {
				t.Fatalf("reconcileA2A: %v", err)
			}

			got := &batchv1.Job{}
			err := cl.Get(ctx, client.ObjectKeyFromObject(superseded), got)
			if !errors.IsNotFound(err) {
				t.Errorf("the superseded provision Job survived the reconcile (err=%v); it has no terminal condition, so nothing else will ever remove it", err)
			}
			policy, deleted := rec.propagation[superseded.Name]
			if !deleted {
				t.Errorf("no Delete was issued for the superseded Job %q", superseded.Name)
			} else if policy == nil || *policy != metav1.DeletePropagationBackground {
				t.Errorf("superseded Job deleted with propagation %v, want %s: without propagation the stuck pod outlives the Job and keeps the quota slot", policy, metav1.DeletePropagationBackground)
			}

			for _, survivor := range []struct {
				what string
				job  *batchv1.Job
			}{
				{"the current render's Job", current},
				{"another agent instance's provision Job", otherInstance},
				{"this agent's non-provision Job", nonProvision},
				{"a provision-labelled Job this agent does not own", unowned},
			} {
				if err := cl.Get(ctx, client.ObjectKeyFromObject(survivor.job), &batchv1.Job{}); err != nil {
					t.Errorf("%s (%s) did not survive the sweep: %v", survivor.what, survivor.job.Name, err)
				}
				if _, wasDeleted := rec.propagation[survivor.job.Name]; wasDeleted {
					t.Errorf("a Delete was issued for %s (%s)", survivor.what, survivor.job.Name)
				}
			}
			if len(rec.propagation) != 1 {
				t.Errorf("%d Deletes issued, want exactly 1 (the superseded Job): %v", len(rec.propagation), rec.propagation)
			}

			// A second pass has nothing left to sweep and must issue no
			// Delete: the current Job is the one name the sweep keeps.
			if _, err := r.reconcileA2A(ctx, agent); err != nil {
				t.Fatalf("reconcileA2A, second pass: %v", err)
			}
			if len(rec.propagation) != 1 {
				t.Errorf("Deletes after the second pass = %d, want 1: the pass with nothing superseded must issue none", len(rec.propagation))
			}
			if err := cl.Get(ctx, client.ObjectKeyFromObject(current), &batchv1.Job{}); err != nil {
				t.Errorf("the current Job is gone after the second pass: %v", err)
			}
		})
	}
}

// TestReconcileA2ASupersededProvisionJobFailureDoesNotParkThePhase is the
// property the sweep must not break, and the reason it reads the current name
// only: the live case is a crash-looping old generation next to a healthy new
// one. A superseded Job carrying Failed is deleted by the pass and its
// condition never reaches the returned state; a sweep that folded the listed
// Jobs' conditions into the phase would park the agent on A2AProvisionFailed
// for a Job the operator has already moved past.
func TestReconcileA2ASupersededProvisionJobFailureDoesNotParkThePhase(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	superseded := a2aProvisionJobOwnedBy(t, agent, "test-agent-a2a-provision-00000000")

	// Job is registered with a status subresource so the Failed condition
	// goes through Status().Update the way the Job controller writes it.
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, superseded).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &batchv1.Job{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()

	stored := &batchv1.Job{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(superseded), stored); err != nil {
		t.Fatalf("read the superseded Job: %v", err)
	}
	stored.Status.Conditions = []batchv1.JobCondition{{
		Type: batchv1.JobFailed, Status: corev1.ConditionTrue,
		Reason: "BackoffLimitExceeded", Message: "Job has reached the specified backoff limit",
	}}
	if err := cl.Status().Update(ctx, stored); err != nil {
		t.Fatalf("mark the superseded Job failed: %v", err)
	}
	// Guard against an inert assertion below: if the fake dropped the
	// condition on write, "not failed" would prove nothing.
	if err := cl.Get(ctx, client.ObjectKeyFromObject(superseded), stored); err != nil {
		t.Fatalf("re-read the superseded Job: %v", err)
	}
	if len(stored.Status.Conditions) == 0 || stored.Status.Conditions[0].Type != batchv1.JobFailed {
		t.Fatal("the Failed condition did not persist on the superseded Job; the phase assertion below would be inert")
	}

	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatalf("reconcileA2A: %v", err)
	}
	if state.failed || state.done {
		t.Errorf("state = {done:%v failed:%v}, want neither: the superseded Job's Failed condition must not reach the phase, and the current Job has not run", state.done, state.failed)
	}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(superseded), &batchv1.Job{}); !errors.IsNotFound(err) {
		t.Errorf("the failed superseded Job survived the reconcile (err=%v)", err)
	}
	current := buildA2AProvisionJob(agent)
	if err := cl.Get(ctx, client.ObjectKeyFromObject(current), &batchv1.Job{}); err != nil {
		t.Errorf("the current render's Job %q was not created: %v", current.Name, err)
	}
}
