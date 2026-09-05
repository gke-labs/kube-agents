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
	"k8s.io/apimachinery/pkg/api/resource"
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
	// Reads of the terminating object the fake garbage collector makes the
	// caller do before it clears the finalizer. More than one, so a caller that
	// reads once and gives up is not accidentally green.
	fakeGCReadsBeforeCollection = 3
	// The claim size the install already has, and the one the recreation is
	// trying to move it to. Any two different quantities would do; these are
	// the pair growShellSandboxDataClaim actually spans.
	oldClaimSize    = "5Gi"
	wantedClaimSize = "20Gi"
)

// terminatingStatefulSet is a fake API server that behaves the way the real one
// does around an immutable-field change on a StatefulSet: an apply comes back
// Invalid for as long as an object of that name exists, and a delete with
// orphan propagation marks the object rather than removing it, leaving it
// readable until the garbage collector has finished with the pod and the
// claims.
type terminatingStatefulSet struct {
	terminating bool
	// Reads of the terminating object, standing in for the collector's
	// progress: it finishes on the fakeGCReadsBeforeCollection'th.
	reads int
	// Applies the fake refused. The recreation is only interesting because the
	// first one is refused, so a test asserts this is not zero.
	refused int
}

func (f *terminatingStatefulSet) funcs() interceptor.Funcs {
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
			if _, isSTS := obj.(*appsv1.StatefulSet); isSTS {
				f.refused++
				return errors.NewInvalid(
					schema.GroupKind{Group: "apps", Kind: "StatefulSet"},
					obj.GetName(),
					field.ErrorList{field.Forbidden(
						field.NewPath("spec", "volumeClaimTemplates"),
						"updates to statefulset spec for fields other than 'replicas', 'ordinals', 'template', "+
							"'updateStrategy', 'persistentVolumeClaimRetentionPolicy' and 'minReadySeconds' are forbidden",
					)},
				)
			}
			obj.SetResourceVersion(existing.GetResourceVersion())
			return cl.Update(ctx, obj)
		},
		Delete: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
			if _, isSTS := obj.(*appsv1.StatefulSet); !isSTS {
				return cl.Delete(ctx, obj, opts...)
			}
			// Accepted, and the object stays: the `orphan` finalizer is on it
			// until the collector has taken the ownerReferences off the pod.
			f.terminating = true
			return nil
		},
		Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, isSTS := obj.(*appsv1.StatefulSet); isSTS && f.terminating {
				f.reads++
				if f.reads >= fakeGCReadsBeforeCollection {
					f.terminating = false
					gone := &appsv1.StatefulSet{
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

// sandboxSTSWithClaim renders the sandbox StatefulSet with its data claim sized
// to `size`, which is the field the recreation exists to change.
func sandboxSTSWithClaim(agent *agentv1alpha1.PlatformAgent, settingsHash, size string) *appsv1.StatefulSet {
	sts := buildShellSandboxStatefulSet(
		agent,
		shellSandboxAuthorizedKeysSecretName(agent),
		credentialProxySandboxURL(agent),
		settingsHash,
	)
	for i := range sts.Spec.VolumeClaimTemplates {
		sts.Spec.VolumeClaimTemplates[i].Spec.Resources.Requests[corev1.ResourceStorage] = resource.MustParse(size)
	}
	return sts
}

// TestTheSandboxRecreationWaitsForTheDeleteToLand pins the ordering the
// recreation depends on. Delete returns as soon as the object is marked, so an
// apply issued straight afterwards addresses an object that is still there —
// and is refused for exactly the reason the recreation exists to get past.
func TestTheSandboxRecreationWaitsForTheDeleteToLand(t *testing.T) {
	agent := brokerPodAgent()
	fakeAPI := &terminatingStatefulSet{}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, sandboxSTSWithClaim(agent, "old-hash", oldClaimSize)).
		WithInterceptorFuncs(fakeAPI.funcs()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	want := sandboxSTSWithClaim(agent, "new-hash", wantedClaimSize)
	if err := r.applyShellSandboxStatefulSet(context.Background(), agent, want); err != nil {
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

	got := &appsv1.StatefulSet{}
	key := types.NamespacedName{Name: shellSandboxName(agent), Namespace: agent.Namespace}
	if err := cl.Get(context.Background(), key, got); err != nil {
		t.Fatalf("the sandbox StatefulSet must exist after the recreation: %v", err)
	}
	size := got.Spec.VolumeClaimTemplates[0].Spec.Resources.Requests[corev1.ResourceStorage]
	if size.String() != wantedClaimSize {
		t.Errorf("the recreated StatefulSet still carries the %s claim template, so the change never landed", size.String())
	}
}

// TestTheSandboxRecreationGivesTheReconcileBackWhenTheDeleteHangs covers the
// other end: a collector that never finishes must not hold the worker.
func TestTheSandboxRecreationGivesTheReconcileBackWhenTheDeleteHangs(t *testing.T) {
	agent := brokerPodAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, sandboxSTSWithClaim(agent, "old-hash", oldClaimSize)).
		WithInterceptorFuncs((&terminatingStatefulSet{}).funcs()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	key := client.ObjectKey{Name: shellSandboxName(agent), Namespace: agent.Namespace}
	if err := r.awaitStatefulSetGone(ctx, key); err == nil {
		t.Fatal("a wait that cannot finish must return an error, not report the object gone")
	}
}
