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
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// The label the current selector carries and the pre-split one did not.
	// Adding it is the immutable change the recreation exists to get past.
	brokerComponentLabel = "kubeagents.x-k8s.io/component"
	brokerComponentValue = "credential-proxy"
	// The policy hash the rendered Deployment is stamped with. Any value; it is
	// not what these tests are about.
	brokerTestPolicyHash = "policy-hash"
)

// terminatingDeployment is a fake API server that behaves the way the real one
// does around an immutable-field change on a Deployment: the apply comes back
// Invalid for as long as an object of that name exists, and a delete with
// foreground propagation marks the object rather than removing it, leaving it
// readable until its ReplicaSet and pod are gone.
//
// Separate from terminatingStatefulSet next door rather than shared with it.
// The two paths differ in the propagation policy they must use and in what a
// premature re-apply costs, and a fake that served both would have to be told
// which it was being, which is the thing the tests are checking.
type terminatingDeployment struct {
	terminating bool
	// Reads of the terminating object, standing in for the collector's
	// progress: it finishes on the fakeGCReadsBeforeCollection'th.
	reads int
	// Applies the fake refused. The recreation is only interesting because the
	// first one is refused, so a test asserts this is not zero.
	refused int
	// The propagation policy the caller deleted with, so a test can assert it
	// was Foreground. Orphaning would leave the old pod running with the
	// broker's credentials and nothing owning it.
	propagation metav1.DeletionPropagation
}

func (f *terminatingDeployment) funcs() interceptor.Funcs {
	return interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if patch.Type() != types.ApplyPatchType {
				return cl.Patch(ctx, obj, patch, opts...)
			}
			key := client.ObjectKeyFromObject(obj)
			existing := obj.DeepCopyObject().(client.Object)
			if err := cl.Get(ctx, key, existing); err != nil {
				if errors.IsNotFound(err) {
					return cl.Create(ctx, obj)
				}
				return err
			}
			if _, isDeployment := obj.(*appsv1.Deployment); isDeployment {
				f.refused++
				return errors.NewInvalid(
					schema.GroupKind{Group: "apps", Kind: "Deployment"},
					obj.GetName(),
					field.ErrorList{field.Invalid(
						field.NewPath("spec", "selector"),
						nil,
						"field is immutable",
					)},
				)
			}
			obj.SetResourceVersion(existing.GetResourceVersion())
			return cl.Update(ctx, obj)
		},
		Delete: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
			if _, isDeployment := obj.(*appsv1.Deployment); !isDeployment {
				return cl.Delete(ctx, obj, opts...)
			}
			options := &client.DeleteOptions{}
			options.ApplyOptions(opts)
			if options.PropagationPolicy != nil {
				f.propagation = *options.PropagationPolicy
			}
			// Accepted, and the object stays: the `foregroundDeletion`
			// finalizer is on it until the ReplicaSet and the pod are gone.
			f.terminating = true
			return nil
		},
		Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, isDeployment := obj.(*appsv1.Deployment); isDeployment && f.terminating {
				f.reads++
				if f.reads >= fakeGCReadsBeforeCollection {
					f.terminating = false
					gone := &appsv1.Deployment{
						ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace},
					}
					if err := cl.Delete(ctx, gone); client.IgnoreNotFound(err) != nil {
						return err
					}
				}
			}
			return cl.Get(ctx, key, obj, opts...)
		},
	}
}

// preSplitBrokerDeployment is the object an install that ran the broker in its
// own pod before this PR already has: the same Deployment, selecting on `app`
// alone. Rendering it from the current builder and taking the label back off is
// what makes it the object the apply is about to be refused over, rather than an
// unrelated Deployment that happens to share the name.
func preSplitBrokerDeployment(agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	dep := buildCredentialProxyDeployment(agent, brokerTestPolicyHash)
	delete(dep.Spec.Selector.MatchLabels, brokerComponentLabel)
	delete(dep.Spec.Template.Labels, brokerComponentLabel)
	return dep
}

// TestTheBrokerRecreationWaitsForTheDeleteToLand pins the ordering. Delete
// returns as soon as the object is marked, so an apply issued straight
// afterwards addresses an object that is still there and is refused for exactly
// the reason the recreation exists to get past.
func TestTheBrokerRecreationWaitsForTheDeleteToLand(t *testing.T) {
	agent := brokerPodAgent()
	fakeAPI := &terminatingDeployment{}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, preSplitBrokerDeployment(agent)).
		WithInterceptorFuncs(fakeAPI.funcs()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	want := buildCredentialProxyDeployment(agent, brokerTestPolicyHash)
	if err := r.applyCredentialProxyDeployment(context.Background(), agent, want); err != nil {
		t.Fatalf("the recreation must succeed once the delete has landed: %v", err)
	}

	if fakeAPI.refused == 0 {
		t.Fatal("the fake never refused an apply, so this exercised the ordinary update path and proves nothing")
	}
	if fakeAPI.refused > 1 {
		t.Errorf("the replacement was applied into the deletion window and refused again (%d refusals)", fakeAPI.refused)
	}
	if fakeAPI.reads < fakeGCReadsBeforeCollection {
		t.Errorf("the caller stopped after %d reads of the terminating object; it must wait for the object to go", fakeAPI.reads)
	}

	got := &appsv1.Deployment{}
	key := types.NamespacedName{Name: credentialBrokerName(agent), Namespace: agent.Namespace}
	if err := cl.Get(context.Background(), key, got); err != nil {
		t.Fatalf("the broker Deployment must exist after the recreation: %v", err)
	}
	if got.Spec.Selector.MatchLabels[brokerComponentLabel] != brokerComponentValue {
		t.Errorf("the recreated Deployment selects on %v, so the change never landed", got.Spec.Selector.MatchLabels)
	}
}

// TestTheBrokerRecreationDeletesTheOldPodWithIt is the difference from the
// sandbox's recreation. The sandbox orphans, because the replacement adopts the
// running pod by selector; here the selector is what changed, so an orphaned pod
// is one nothing adopts, nothing routes to, and which still holds the broker's
// credentials.
func TestTheBrokerRecreationDeletesTheOldPodWithIt(t *testing.T) {
	agent := brokerPodAgent()
	fakeAPI := &terminatingDeployment{}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, preSplitBrokerDeployment(agent)).
		WithInterceptorFuncs(fakeAPI.funcs()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	want := buildCredentialProxyDeployment(agent, brokerTestPolicyHash)
	if err := r.applyCredentialProxyDeployment(context.Background(), agent, want); err != nil {
		t.Fatalf("the recreation must succeed once the delete has landed: %v", err)
	}
	if fakeAPI.propagation != metav1.DeletePropagationForeground {
		t.Errorf("got propagation %q, want %q: an orphaned broker pod outlives the Deployment holding its credentials",
			fakeAPI.propagation, metav1.DeletePropagationForeground)
	}
}

// TestTheBrokerApplyPassesThroughAnErrorThatIsNotInvalid keeps the recreation
// narrow. Invalid is the one failure a delete can fix; anything else — a
// conflict, a lost connection — has to reach the caller as itself, because
// deleting the broker in response to it takes the agent's credentials down for
// a reason that was never about the selector.
func TestTheBrokerApplyPassesThroughAnErrorThatIsNotInvalid(t *testing.T) {
	agent := brokerPodAgent()
	scheme := setupScheme()
	deleted := false
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, preSplitBrokerDeployment(agent)).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				return errors.NewConflict(
					schema.GroupResource{Group: "apps", Resource: "deployments"},
					obj.GetName(),
					errors.NewBadRequest("the object has been modified"),
				)
			},
			Delete: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
				deleted = true
				return cl.Delete(ctx, obj, opts...)
			},
		}).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	err := r.applyCredentialProxyDeployment(context.Background(), agent, buildCredentialProxyDeployment(agent, brokerTestPolicyHash))
	if !errors.IsConflict(err) {
		t.Errorf("got %v, want the conflict back unchanged", err)
	}
	if deleted {
		t.Error("a conflict deleted the broker Deployment; only an Invalid apply may do that")
	}
}

// TestTheBrokerRecreationGivesTheReconcileBackWhenTheDeleteHangs covers the
// other end: a collector that never finishes must not hold the worker.
func TestTheBrokerRecreationGivesTheReconcileBackWhenTheDeleteHangs(t *testing.T) {
	agent := brokerPodAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, preSplitBrokerDeployment(agent)).
		WithInterceptorFuncs((&terminatingDeployment{}).funcs()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	key := client.ObjectKey{Name: credentialBrokerName(agent), Namespace: agent.Namespace}
	if err := r.awaitCredentialProxyDeploymentGone(ctx, key); err == nil {
		t.Fatal("a wait that cannot finish must return an error, not report the object gone")
	}
}
