package controller

import (
	"context"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The operator's ServiceAccount (UID a) was deleted and a hand-made one (UID b)
// created under the same name, and the cache still holds a. The ownership
// check passes on a, so the delete must be held to a's UID and fail against b.
// The fake client does not enforce a UID precondition, so the interceptor
// does what the API server does: 409 when the precondition's UID is not the
// live object's.
func TestDeletingTheProfilesServiceAccountIsHeldToTheUIDItChecked(t *testing.T) {
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "auditor")
	p.UID = "profile-uid"
	name := agentProfileServiceAccountPrefix + p.Name
	live := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: p.Namespace, Name: name, UID: "b"}}
	stale := live.DeepCopy()
	stale.UID = "a"
	stale.OwnerReferences = []metav1.OwnerReference{{
		APIVersion: agentv1alpha1.GroupVersion.String(), Kind: "AgentProfile",
		Name: p.Name, UID: p.UID, Controller: ptr.To(true),
	}}

	var preconditionUID *types.UID
	c := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(live).
		WithInterceptorFuncs(interceptor.Funcs{
			Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
				if sa, ok := obj.(*corev1.ServiceAccount); ok && key.Name == name {
					stale.DeepCopyInto(sa)
					return nil
				}
				return cl.Get(ctx, key, obj, opts...)
			},
			Delete: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
				del := &client.DeleteOptions{}
				del.ApplyOptions(opts)
				if del.Preconditions != nil {
					preconditionUID = del.Preconditions.UID
				}
				var current corev1.ServiceAccount
				if err := cl.Get(ctx, client.ObjectKeyFromObject(obj), &current); err != nil {
					return err
				}
				if preconditionUID != nil && *preconditionUID != current.UID {
					return apierrors.NewConflict(corev1.Resource("serviceaccounts"), obj.GetName(), nil)
				}
				return cl.Delete(ctx, obj, opts...)
			},
		}).Build()
	r := &AgentProfileReconciler{Client: c, Scheme: setupScheme()}

	err := r.deleteOwnServiceAccount(context.Background(), &p)
	if preconditionUID == nil || *preconditionUID != "a" {
		t.Errorf("delete precondition UID = %v, want the checked copy's a", preconditionUID)
	}
	if err == nil {
		t.Error("the delete against a replacement under the same name succeeded")
	}
	// Gets for this name are answered with the stale copy, so read the live
	// one back by List.
	var all corev1.ServiceAccountList
	if err := c.List(context.Background(), &all, client.InNamespace(p.Namespace)); err != nil {
		t.Fatal(err)
	}
	if len(all.Items) != 1 || all.Items[0].UID != "b" {
		t.Errorf("ServiceAccounts after the delete = %+v, want the replacement b untouched", all.Items)
	}
}
