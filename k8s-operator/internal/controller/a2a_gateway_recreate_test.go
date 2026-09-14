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
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The rollingUpdate block the API server defaults onto a Deployment that names
// no strategy, which is what every gateway Deployment applied before the
// strategy became Recreate carries.
const defaultedRollingUpdateFencepost = "25%"

// refusingRecreateOverDefaultedStrategy is a fake API server that behaves the
// way the real one does when a server-side apply sets `strategy.type: Recreate`
// on a Deployment whose live object carries the defaulted rollingUpdate block:
// no field manager owns that block, so the apply leaves it in place and the
// server refuses the result, on every attempt, for as long as the object
// exists. Reproduced against kube-apiserver 1.36 under envtest before this fake
// was written; the fake exists because the fake client neither defaults nor
// validates strategy, so without it the transition is invisible to a unit test.
//
// Separate from terminatingDeployment, which refuses every Deployment apply
// while the object exists. This one refuses only the transition it models, so
// a test can also show that a gateway already on Recreate is left alone.
type refusingRecreateOverDefaultedStrategy struct {
	gateway     types.NamespacedName
	terminating bool
	reads       int
	refused     int
	deleted     bool
	propagation metav1.DeletionPropagation
}

func (f *refusingRecreateOverDefaultedStrategy) isGateway(obj client.Object) bool {
	_, isDeployment := obj.(*appsv1.Deployment)
	return isDeployment && client.ObjectKeyFromObject(obj) == f.gateway
}

func (f *refusingRecreateOverDefaultedStrategy) funcs() interceptor.Funcs {
	ssa := fakeServerSideApplyInterceptors().Patch
	return interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if patch.Type() != types.ApplyPatchType || !f.isGateway(obj) {
				return ssa(ctx, cl, obj, patch, opts...)
			}
			existing := &appsv1.Deployment{}
			if err := cl.Get(ctx, f.gateway, existing); err != nil {
				if errors.IsNotFound(err) {
					return cl.Create(ctx, obj)
				}
				return err
			}
			incoming := obj.(*appsv1.Deployment)
			if incoming.Spec.Strategy.Type == appsv1.RecreateDeploymentStrategyType && existing.Spec.Strategy.RollingUpdate != nil {
				f.refused++
				return errors.NewInvalid(
					schema.GroupKind{Group: "apps", Kind: "Deployment"},
					obj.GetName(),
					field.ErrorList{field.Forbidden(
						field.NewPath("spec", "strategy", "rollingUpdate"),
						"may not be specified when strategy `type` is 'Recreate'",
					)},
				)
			}
			return ssa(ctx, cl, obj, patch, opts...)
		},
		Delete: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
			if !f.isGateway(obj) {
				return cl.Delete(ctx, obj, opts...)
			}
			options := &client.DeleteOptions{}
			options.ApplyOptions(opts)
			if options.PropagationPolicy != nil {
				f.propagation = *options.PropagationPolicy
			}
			f.deleted = true
			// Accepted, and the object stays until the ReplicaSet and pod
			// are gone, as foreground propagation has it.
			f.terminating = true
			return nil
		},
		Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, isDeployment := obj.(*appsv1.Deployment); isDeployment && key == f.gateway && f.terminating {
				f.reads++
				if f.reads >= fakeGCReadsBeforeCollection {
					f.terminating = false
					gone := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace}}
					if err := cl.Delete(ctx, gone); client.IgnoreNotFound(err) != nil {
						return err
					}
				}
			}
			return cl.Get(ctx, key, obj, opts...)
		},
	}
}

// gatewayAppliedBeforeRecreate is the gateway Deployment an install reconciled
// before the strategy became Recreate has on the API server: the current
// render with the strategy the server defaulted in place of the one the
// builder now sets. Rendering it from the builder keeps it the object the
// apply is refused over rather than an unrelated Deployment sharing the name.
func gatewayAppliedBeforeRecreate(agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	dep := buildA2AGatewayDeployment(agent)
	fencepost := intstr.FromString(defaultedRollingUpdateFencepost)
	dep.Spec.Strategy = appsv1.DeploymentStrategy{
		Type:          appsv1.RollingUpdateDeploymentStrategyType,
		RollingUpdate: &appsv1.RollingUpdateDeployment{MaxSurge: &fencepost, MaxUnavailable: &fencepost},
	}
	return dep
}

func gatewayRecreateFixture(t *testing.T, live *appsv1.Deployment) (*PlatformAgentReconciler, client.Client, *agentv1alpha1.PlatformAgent, *refusingRecreateOverDefaultedStrategy) {
	t.Helper()
	agent := a2aTestAgent()
	fakeAPI := &refusingRecreateOverDefaultedStrategy{gateway: client.ObjectKeyFromObject(live)}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, live).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeAPI.funcs()).
		Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl, agent, fakeAPI
}

// TestTheGatewayIsRecreatedWhenRecreateIsRefusedOverTheDefaultedStrategy is
// the upgrade every install with a gateway from before the strategy change
// goes through. Applied through applyManaged alone the refusal repeats on
// every reconcile and the rest of the reconcile never runs; through
// reconcileA2A as it stands the Deployment is replaced once and the live
// object ends on Recreate.
func TestTheGatewayIsRecreatedWhenRecreateIsRefusedOverTheDefaultedStrategy(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, agent, fakeAPI := gatewayRecreateFixture(t, gatewayAppliedBeforeRecreate(agent))

	if _, err := r.reconcileA2A(context.Background(), agent); err != nil {
		t.Fatalf("reconcileA2A must get past the refused apply by recreating the gateway: %v", err)
	}

	if fakeAPI.refused == 0 {
		t.Fatal("the fake never refused an apply, so this exercised the ordinary update path and proves nothing")
	}
	if fakeAPI.refused > 1 {
		t.Errorf("the replacement was applied into the deletion window and refused again (%d refusals)", fakeAPI.refused)
	}
	if fakeAPI.propagation != metav1.DeletePropagationForeground {
		t.Errorf("got propagation %q, want %q: an orphaned gateway pod keeps consuming the relay durable beside its replacement",
			fakeAPI.propagation, metav1.DeletePropagationForeground)
	}
	if fakeAPI.reads < fakeGCReadsBeforeCollection {
		t.Errorf("the caller stopped after %d reads of the terminating object; it must wait for the object to go", fakeAPI.reads)
	}

	got := &appsv1.Deployment{}
	if err := cl.Get(context.Background(), fakeAPI.gateway, got); err != nil {
		t.Fatalf("the gateway Deployment must exist after the recreation: %v", err)
	}
	if got.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType || got.Spec.Strategy.RollingUpdate != nil {
		t.Errorf("recreated gateway strategy = %+v, want Recreate with no rollingUpdate block", got.Spec.Strategy)
	}
}

// TestTheGatewayIsLeftAloneOnceItRollsWithRecreate keeps the recreation a
// one-time transition. A gateway already on Recreate is updated in place; a
// delete here would be an outage on every reconcile.
func TestTheGatewayIsLeftAloneOnceItRollsWithRecreate(t *testing.T) {
	agent := a2aTestAgent()
	r, _, agent, fakeAPI := gatewayRecreateFixture(t, buildA2AGatewayDeployment(agent))

	if _, err := r.reconcileA2A(context.Background(), agent); err != nil {
		t.Fatalf("reconcileA2A: %v", err)
	}
	if fakeAPI.refused != 0 {
		t.Errorf("the fake refused %d applies of a gateway already on Recreate; the fake models the wrong transition", fakeAPI.refused)
	}
	if fakeAPI.deleted {
		t.Error("a gateway already on Recreate was deleted; the recreation must only follow a refused apply")
	}
}
