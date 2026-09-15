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
	corev1 "k8s.io/api/core/v1"
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

// The UID the live gateway carries in these tests. Session pods reference their
// gateway Deployment by UID, so a UID that survives the transition is what
// keeps them owned; the tests assert it is the same object afterwards.
const gatewayUIDBeforeTransition = types.UID("gateway-uid-before-the-strategy-changed")

// refusingRecreateOverDefaultedStrategy is a fake API server that behaves the
// way the real one does when a server-side apply sets `strategy.type: Recreate`
// on a Deployment whose live object carries the defaulted rollingUpdate block:
// no field manager owns that block, so the apply leaves it in place and the
// server refuses the result, on every attempt, for as long as the block is
// there. Reproduced against kube-apiserver 1.36 under envtest before this fake
// was written; the fake exists because the fake client neither defaults nor
// validates strategy, so without it the transition is invisible to a unit test.
//
// A merge patch goes through to the fake client, which applies it the way the
// server does (rollingUpdate nulled, type set), so an apply after the patch is
// accepted. A delete is recorded and refused: the path under test must never
// issue one, because the Deployment owns the gateway's session pods.
type refusingRecreateOverDefaultedStrategy struct {
	gateway types.NamespacedName
	refused int
	patched int
	deleted bool
}

func (f *refusingRecreateOverDefaultedStrategy) isGateway(obj client.Object) bool {
	_, isDeployment := obj.(*appsv1.Deployment)
	return isDeployment && client.ObjectKeyFromObject(obj) == f.gateway
}

func (f *refusingRecreateOverDefaultedStrategy) funcs() interceptor.Funcs {
	ssa := fakeServerSideApplyInterceptors().Patch
	return interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if !f.isGateway(obj) {
				return ssa(ctx, cl, obj, patch, opts...)
			}
			if patch.Type() == types.MergePatchType {
				f.patched++
				return cl.Patch(ctx, obj, patch, opts...)
			}
			if patch.Type() != types.ApplyPatchType {
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
			// The server keeps the object's UID across an apply; the fake's
			// apply is an Update of the incoming object, which carries none.
			incoming.UID = existing.UID
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
			if f.isGateway(obj) {
				f.deleted = true
			}
			return cl.Delete(ctx, obj, opts...)
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
	live.UID = gatewayUIDBeforeTransition
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

// TestTheGatewayStrategyIsClearedInPlaceWhenRecreateIsRefused is the upgrade
// every install with a gateway from before the strategy change goes through.
// Applied through applyManaged alone the refusal repeats on every reconcile
// and the rest of the reconcile never runs; through reconcileA2A as it stands
// the defaulted block is cleared by a merge patch and the live object ends on
// Recreate. It is the same object afterwards -- same UID, never deleted --
// because every session pod the gateway spawned references it by UID, and a
// delete would hand them all to the garbage collector.
func TestTheGatewayStrategyIsClearedInPlaceWhenRecreateIsRefused(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, agent, fakeAPI := gatewayRecreateFixture(t, gatewayAppliedBeforeRecreate(agent))

	if _, err := r.reconcileA2A(context.Background(), agent); err != nil {
		t.Fatalf("reconcileA2A must get past the refused apply by clearing the strategy: %v", err)
	}

	if fakeAPI.refused == 0 {
		t.Fatal("the fake never refused an apply, so this exercised the ordinary update path and proves nothing")
	}
	if fakeAPI.refused > 1 {
		t.Errorf("the apply was refused %d times; after the patch the block is gone and the second apply must be accepted", fakeAPI.refused)
	}
	if fakeAPI.patched != 1 {
		t.Errorf("got %d merge patches of the gateway, want 1: one patch clears the block", fakeAPI.patched)
	}
	if fakeAPI.deleted {
		t.Error("the gateway Deployment was deleted: its session pods reference it by UID and would be garbage-collected with it")
	}

	got := &appsv1.Deployment{}
	if err := cl.Get(context.Background(), fakeAPI.gateway, got); err != nil {
		t.Fatalf("the gateway Deployment must exist after the transition: %v", err)
	}
	if got.UID != gatewayUIDBeforeTransition {
		t.Errorf("gateway UID = %q, want %q: a different object orphans every session pod that referenced the old one", got.UID, gatewayUIDBeforeTransition)
	}
	if got.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType || got.Spec.Strategy.RollingUpdate != nil {
		t.Errorf("gateway strategy = %+v, want Recreate with no rollingUpdate block", got.Spec.Strategy)
	}
}

// TestTheGatewayIsLeftAloneOnceItRollsWithRecreate keeps the transition a
// one-time event. A gateway already on Recreate is updated in place by the
// apply alone; neither the patch nor a delete may follow an accepted apply.
func TestTheGatewayIsLeftAloneOnceItRollsWithRecreate(t *testing.T) {
	agent := a2aTestAgent()
	r, _, agent, fakeAPI := gatewayRecreateFixture(t, buildA2AGatewayDeployment(agent))

	if _, err := r.reconcileA2A(context.Background(), agent); err != nil {
		t.Fatalf("reconcileA2A: %v", err)
	}
	if fakeAPI.refused != 0 {
		t.Errorf("the fake refused %d applies of a gateway already on Recreate; the fake models the wrong transition", fakeAPI.refused)
	}
	if fakeAPI.patched != 0 {
		t.Errorf("a gateway already on Recreate was patched %d times; the patch must only follow a refused apply", fakeAPI.patched)
	}
	if fakeAPI.deleted {
		t.Error("a gateway already on Recreate was deleted; nothing on this path may delete the Deployment")
	}
}

// TestTheGatewayStrategyIsClearedInPlaceOnTheAPIServerEnvtest is the same
// transition against a real kube-apiserver, the only place the defaulting and
// the validation the fake above models actually run: apply the gateway with no
// strategy named, as every install before the change did, and read back the
// block the server defaulted; watch the plain apply of Recreate come back
// Invalid; then take the path reconcileA2A takes and read back Recreate on the
// same object, same UID. No garbage collector runs under envtest, which is why
// this test reads the UID rather than watching a session pod: the UID is what
// the pods' ownerReferences name.
func TestTheGatewayStrategyIsClearedInPlaceOnTheAPIServerEnvtest(t *testing.T) {
	cl, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := a2aTestAgent()
	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	key := client.ObjectKeyFromObject(buildA2AGatewayDeployment(agent))
	fetch := func() *appsv1.Deployment {
		t.Helper()
		got := &appsv1.Deployment{}
		if err := cl.Get(ctx, key, got); err != nil {
			t.Fatalf("reading the gateway Deployment: %v", err)
		}
		return got
	}

	before := buildA2AGatewayDeployment(agent)
	before.Spec.Strategy = appsv1.DeploymentStrategy{}
	if err := r.applyManaged(ctx, agent, before); err != nil {
		t.Fatalf("applying the gateway as an install before the change did: %v", err)
	}
	live := fetch()
	if live.Spec.Strategy.RollingUpdate == nil {
		t.Fatal("the server defaulted no rollingUpdate block onto a Deployment naming no strategy; the transition under test does not exist")
	}
	uid := live.UID

	if err := r.applyManaged(ctx, agent, buildA2AGatewayDeployment(agent)); !errors.IsInvalid(err) {
		t.Fatalf("a plain apply of Recreate over the defaulted block: got %v, want Invalid; if the server now accepts it the in-place path has nothing left to do", err)
	}

	if err := r.applyA2AGatewayDeployment(ctx, agent, buildA2AGatewayDeployment(agent)); err != nil {
		t.Fatalf("applyA2AGatewayDeployment over the defaulted block: %v", err)
	}
	after := fetch()
	if after.UID != uid {
		t.Errorf("gateway UID changed from %q to %q: the Deployment was replaced, and every session pod owned by the old one goes with it", uid, after.UID)
	}
	if after.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType || after.Spec.Strategy.RollingUpdate != nil {
		t.Errorf("gateway strategy = %+v, want Recreate with no rollingUpdate block", after.Spec.Strategy)
	}

	if err := r.applyA2AGatewayDeployment(ctx, agent, buildA2AGatewayDeployment(agent)); err != nil {
		t.Fatalf("the reconcile after the transition must apply in place: %v", err)
	}
	if again := fetch(); again.UID != uid {
		t.Errorf("gateway UID changed on the following reconcile, from %q to %q", uid, again.UID)
	}
}
