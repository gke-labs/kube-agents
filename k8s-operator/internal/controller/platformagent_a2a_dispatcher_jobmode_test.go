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
	"errors"
	"strings"
	"testing"

	admissionregistrationv1 "k8s.io/api/admissionregistration/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// jobModeFixture is a next install with the dispatcher on, the chart's Job
// policy signal set, and the policy, its binding and the profile namespace
// in place, reconciled through Reconcile. getErr, when set, answers reads of
// the binding; statusWrites counts the PlatformAgent's status writes.
type jobModeFixture struct {
	r            *PlatformAgentReconciler
	c            client.Client
	rec          *record.FakeRecorder
	req          ctrl.Request
	getErr       error
	statusWrites int
}

func newJobModeFixture(t *testing.T, objs ...client.Object) *jobModeFixture {
	t.Helper()
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	scheme := setupScheme()
	agent := a2aTestAgent()
	policy, binding := shippedDispatcherJobPolicy(t)
	f := &jobModeFixture{
		rec: record.NewFakeRecorder(100),
		req: ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)},
	}
	f.c = fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(append([]client.Object{agent, chartProfileNamespace(agent), policy, binding, jobTestNATSCABundle(agent)}, objs...)...).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: fakeServerSideApplyInterceptors().Patch,
			Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
				if _, isBinding := obj.(*admissionregistrationv1.ValidatingAdmissionPolicyBinding); isBinding && f.getErr != nil {
					return f.getErr
				}
				return cl.Get(ctx, key, obj, opts...)
			},
			SubResourceUpdate: func(ctx context.Context, cl client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
				if _, ok := obj.(*agentv1alpha1.PlatformAgent); ok && subResourceName == "status" {
					f.statusWrites++
				}
				return cl.SubResource(subResourceName).Update(ctx, obj, opts...)
			},
		}).Build()
	f.r = &PlatformAgentReconciler{Client: f.c, Scheme: scheme, Recorder: f.rec}
	return f
}

func (f *jobModeFixture) reconcile(t *testing.T) error {
	t.Helper()
	_, err := f.r.Reconcile(context.Background(), f.req)
	return err
}

func (f *jobModeFixture) mustReconcile(t *testing.T) {
	t.Helper()
	if err := f.reconcile(t); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
}

func (f *jobModeFixture) agent(t *testing.T) *agentv1alpha1.PlatformAgent {
	t.Helper()
	got := &agentv1alpha1.PlatformAgent{}
	if err := f.c.Get(context.Background(), f.req.NamespacedName, got); err != nil {
		t.Fatal(err)
	}
	return got
}

func (f *jobModeFixture) condition(t *testing.T) *metav1.Condition {
	t.Helper()
	return meta.FindStatusCondition(f.agent(t).Status.Conditions, dispatcherJobModeCondition)
}

// lastPolicyEvent is the message of the last DispatcherJobPolicyMissing
// Event recorded, "" if none.
func (f *jobModeFixture) lastPolicyEvent() string {
	last := ""
	for {
		select {
		case e := <-f.rec.Events:
			if msg, ok := strings.CutPrefix(e, corev1.EventTypeWarning+" "+reasonDispatcherJobPolicyMissing+" "); ok {
				last = msg
			}
		default:
			return last
		}
	}
}

func wantJobMode(t *testing.T, got *metav1.Condition, status metav1.ConditionStatus, reason string) {
	t.Helper()
	if got == nil {
		t.Fatalf("no %s condition, want %s/%s", dispatcherJobModeCondition, status, reason)
	}
	if got.Status != status || got.Reason != reason {
		t.Fatalf("%s = %s/%s (%s), want %s/%s", dispatcherJobModeCondition, got.Status, got.Reason, got.Message, status, reason)
	}
}

// With the policy and its binding in place and the profile namespace
// selected, the dispatcher holds its Jobs Role and the condition says Job
// mode is on.
func TestDispatcherJobModeIsTrueWithThePolicyInPlace(t *testing.T) {
	agent := a2aTestAgent()
	f := newJobModeFixture(t, sandboxKeysSecret(agent))
	f.mustReconcile(t)
	f.mustReconcile(t)
	wantJobMode(t, f.condition(t), metav1.ConditionTrue, jobModeReasonInPlace)
	if err := f.c.Get(context.Background(), types.NamespacedName{Namespace: profileNamespaceName(agent), Name: a2aDispatcherJobsRoleName(agent)}, &rbacv1.RoleBinding{}); err != nil {
		t.Errorf("the condition says InPlace with no Jobs RoleBinding: %v", err)
	}
}

// The condition is written on the way out of Reconcile, so neither
// updateStatusReady nor updateStatusDegraded, both later in the pass than
// the dispatcher step, leaves it unwritten: one pass is enough on either.
func TestDispatcherJobModeOutlivesTheStatusWritersOfItsPass(t *testing.T) {
	for name, objs := range map[string][]client.Object{
		"Ready":    {sandboxKeysSecret(a2aTestAgent())},
		"Degraded": nil, // no sandbox keypair: the pass parks Degraded after the A2A render
	} {
		t.Run(name, func(t *testing.T) {
			f := newJobModeFixture(t, objs...)
			f.mustReconcile(t)
			f.mustReconcile(t) // the first pass adds the finalizer and returns
			agent := f.agent(t)
			if name == "Degraded" {
				if ready := meta.FindStatusCondition(agent.Status.Conditions, "Ready"); ready == nil || ready.Status != metav1.ConditionFalse {
					t.Fatalf("precondition: want Ready=False from the missing keypair, got %+v", ready)
				}
			}
			wantJobMode(t, meta.FindStatusCondition(agent.Status.Conditions, dispatcherJobModeCondition), metav1.ConditionTrue, jobModeReasonInPlace)
		})
	}
}

// With the policy missing, unreadable or loosened the condition is False,
// PolicyMissing, with the DispatcherJobPolicyMissing Event's message.
func TestDispatcherJobModeIsFalseWithThePolicyMissing(t *testing.T) {
	for name, breakIt := range map[string]func(t *testing.T, f *jobModeFixture){
		"the binding gone": func(t *testing.T, f *jobModeFixture) {
			if err := f.c.Delete(context.Background(), &admissionregistrationv1.ValidatingAdmissionPolicyBinding{ObjectMeta: metav1.ObjectMeta{Name: dispatcherJobPolicyName}}); err != nil {
				t.Fatal(err)
			}
		},
		"the binding unreadable": func(t *testing.T, f *jobModeFixture) {
			f.getErr = apierrors.NewForbidden(admissionregistrationv1.Resource("validatingadmissionpolicybindings"), dispatcherJobPolicyName, errors.New("no grant"))
		},
		"the policy failing open": func(t *testing.T, f *jobModeFixture) {
			var policy admissionregistrationv1.ValidatingAdmissionPolicy
			if err := f.c.Get(context.Background(), types.NamespacedName{Name: dispatcherJobPolicyName}, &policy); err != nil {
				t.Fatal(err)
			}
			policy.Spec.FailurePolicy = ptr.To(admissionregistrationv1.Ignore)
			if err := f.c.Update(context.Background(), &policy); err != nil {
				t.Fatal(err)
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			f := newJobModeFixture(t, sandboxKeysSecret(a2aTestAgent()))
			f.mustReconcile(t)
			f.mustReconcile(t)
			wantJobMode(t, f.condition(t), metav1.ConditionTrue, jobModeReasonInPlace)
			f.lastPolicyEvent()
			breakIt(t, f)
			f.mustReconcile(t)
			got := f.condition(t)
			wantJobMode(t, got, metav1.ConditionFalse, jobModeReasonPolicyMissing)
			if event := f.lastPolicyEvent(); event == "" || got.Message != event {
				t.Errorf("message %q, want the Event's %q", got.Message, event)
			}
		})
	}
}

// A profile namespace without a label the binding selects it by: False,
// NamespaceNotSelected, with the Event's message.
func TestDispatcherJobModeIsFalseInANamespaceThePolicyDoesNotSelect(t *testing.T) {
	agent := a2aTestAgent()
	f := newJobModeFixture(t, sandboxKeysSecret(agent))
	f.mustReconcile(t)
	f.mustReconcile(t)
	f.lastPolicyEvent()
	var ns corev1.Namespace
	if err := f.c.Get(context.Background(), types.NamespacedName{Name: profileNamespaceName(agent)}, &ns); err != nil {
		t.Fatal(err)
	}
	delete(ns.Labels, labelPartOf)
	if err := f.c.Update(context.Background(), &ns); err != nil {
		t.Fatal(err)
	}
	f.mustReconcile(t)
	got := f.condition(t)
	wantJobMode(t, got, metav1.ConditionFalse, jobModeReasonNamespaceNotSelected)
	if event := f.lastPolicyEvent(); event == "" || got.Message != event {
		t.Errorf("message %q, want the Event's %q", got.Message, event)
	}
}

// A read of the policy that answers neither way changes nothing on the
// cluster, so it changes nothing in status either: a blip doesn't flap the
// condition.
func TestATransientPolicyReadLeavesDispatcherJobModeAlone(t *testing.T) {
	f := newJobModeFixture(t, sandboxKeysSecret(a2aTestAgent()))
	f.mustReconcile(t)
	f.mustReconcile(t)
	before := f.condition(t)
	wantJobMode(t, before, metav1.ConditionTrue, jobModeReasonInPlace)
	f.getErr = apierrors.NewServerTimeout(admissionregistrationv1.Resource("validatingadmissionpolicybindings"), "get", 1)
	if err := f.reconcile(t); err == nil {
		t.Fatal("precondition: a timed-out read of the binding was swallowed")
	}
	if got := f.condition(t); got == nil || *got != *before {
		t.Errorf("condition moved on a transient read: %+v, was %+v", got, before)
	}
}

// Absent while the chart's signal is off, as BusCredentialsReady is on a
// today install, and removed when the signal goes or the dispatcher does.
func TestDispatcherJobModeIsAbsentWithoutTheSignal(t *testing.T) {
	t.Run("never on", func(t *testing.T) {
		f := newJobModeFixture(t, sandboxKeysSecret(a2aTestAgent()))
		t.Setenv(a2aDispatcherJobPolicyEnvVar, "")
		f.mustReconcile(t)
		f.mustReconcile(t)
		if got := f.condition(t); got != nil {
			t.Errorf("%s without the signal: %+v", dispatcherJobModeCondition, got)
		}
	})
	for name, turnOff := range map[string]func(t *testing.T, f *jobModeFixture){
		"the signal off":     func(t *testing.T, _ *jobModeFixture) { t.Setenv(a2aDispatcherJobPolicyEnvVar, "") },
		"the dispatcher off": func(t *testing.T, _ *jobModeFixture) { t.Setenv(a2aDispatcherImageEnvVar, "") },
		"the mode back to today": func(t *testing.T, f *jobModeFixture) {
			agent := f.agent(t)
			agent.Spec.Mode = nil
			if err := f.c.Update(context.Background(), agent); err != nil {
				t.Fatal(err)
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			f := newJobModeFixture(t, sandboxKeysSecret(a2aTestAgent()))
			f.mustReconcile(t)
			f.mustReconcile(t)
			wantJobMode(t, f.condition(t), metav1.ConditionTrue, jobModeReasonInPlace)
			turnOff(t, f)
			f.mustReconcile(t)
			if got := f.condition(t); got != nil {
				t.Errorf("%s survived %s: %+v", dispatcherJobModeCondition, name, got)
			}
		})
	}
}

// The other ways a pass with the signal on ends without Job mode each give
// False with a reason of their own: a profile namespace not ready, a
// foreign DispatchJobTemplate under a param's name, and a Jobs Role the
// pass couldn't apply.
func TestEveryJobModeRevocationSaysWhy(t *testing.T) {
	agent := a2aTestAgent()
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}
	t.Run("namespace not ready", func(t *testing.T) {
		r, _ := jobTemplateTestReconciler(t, agent)
		got, _, err := r.reconcileA2ADispatcherWithJobMode(context.Background(), agent, profiles, false)
		if err != nil {
			t.Fatal(err)
		}
		wantJobMode(t, got, metav1.ConditionFalse, jobModeReasonNamespaceNotReady)
	})
	t.Run("foreign template", func(t *testing.T) {
		foreign := &agentv1alpha1.DispatchJobTemplate{ObjectMeta: metav1.ObjectMeta{
			Name: dispatchJobTemplatePrefix + "tse", Namespace: profileNamespaceName(agent), Labels: map[string]string{"x": "y"},
		}}
		r, _ := jobTemplateTestReconciler(t, agent, foreign)
		got, _, err := r.reconcileA2ADispatcherWithJobMode(context.Background(), agent, profiles, true)
		if err != nil {
			t.Fatal(err)
		}
		wantJobMode(t, got, metav1.ConditionFalse, jobModeReasonForeignJobTemplate)
		if !strings.Contains(got.Message, foreign.Name) {
			t.Errorf("message %q does not name the foreign template", got.Message)
		}
	})
	t.Run("Jobs Role refused", func(t *testing.T) {
		r, c := jobTemplateTestReconciler(t, agent)
		r.Client = interceptor.NewClient(c.(client.WithWatch), interceptor.Funcs{
			Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				if obj.GetName() == a2aDispatcherJobsRoleName(agent) {
					return apierrors.NewForbidden(rbacv1.Resource("roles"), obj.GetName(), errors.New("escalation"))
				}
				return fakeServerSideApplyInterceptors().Patch(ctx, cl, obj, patch, opts...)
			},
		})
		got, _, err := r.reconcileA2ADispatcherWithJobMode(context.Background(), agent, profiles, true)
		if err != nil {
			t.Fatal(err)
		}
		wantJobMode(t, got, metav1.ConditionFalse, jobModeReasonGrantFailed)
		if !strings.Contains(got.Message, "escalation") {
			t.Errorf("message %q does not carry the error", got.Message)
		}
	})
}

// The condition is synced on every pass, so it writes status only when it
// changes.
func TestDispatcherJobModeWritesStatusOnlyOnAChange(t *testing.T) {
	f := newJobModeFixture(t)
	ctx := context.Background()
	agent := f.agent(t)
	on := jobModeOn(profileNamespaceName(agent))
	for i, want := range []int{1, 0} {
		f.statusWrites = 0
		if err := f.r.syncDispatcherJobMode(ctx, agent, true, on); err != nil {
			t.Fatal(err)
		}
		if f.statusWrites != want {
			t.Errorf("sync %d with the same verdict wrote status %d times, want %d", i+1, f.statusWrites, want)
		}
	}
	// No verdict this pass: nothing to write.
	f.statusWrites = 0
	if err := f.r.syncDispatcherJobMode(ctx, agent, true, nil); err != nil {
		t.Fatal(err)
	}
	if f.statusWrites != 0 {
		t.Errorf("sync with no verdict wrote status %d times", f.statusWrites)
	}
	// Removed once, then nothing left to remove.
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "")
	for i, want := range []int{1, 0} {
		f.statusWrites = 0
		if err := f.r.syncDispatcherJobMode(ctx, agent, true, nil); err != nil {
			t.Fatal(err)
		}
		if f.statusWrites != want {
			t.Errorf("removal %d wrote status %d times, want %d", i+1, f.statusWrites, want)
		}
	}
}
