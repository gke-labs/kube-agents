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
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func setupScheme() *runtime.Scheme {
	scheme := runtime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(agentv1alpha1.AddToScheme(scheme))
	return scheme
}

func TestPlatformAgentReconciler_Reconcile(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{},
	}

	// Interceptor to handle Server-Side Apply (SSA) in fake client
	interceptors := interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if patch.Type() == types.ApplyPatchType {
				key := client.ObjectKeyFromObject(obj)
				existing := obj.DeepCopyObject().(client.Object)
				err := cl.Get(ctx, key, existing)
				if err != nil {
					if errors.IsNotFound(err) {
						return cl.Create(ctx, obj)
					}
					return err
				}
				obj.SetResourceVersion(existing.GetResourceVersion())
				return cl.Update(ctx, obj)
			}
			return cl.Patch(ctx, obj, patch, opts...)
		},
	}

	// Create a fake client with the PlatformAgent
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptors).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	ctx := context.Background()

	// 1st Reconcile: Adds the finalizer
	_, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}

	// Fetch agent to verify finalizer is added
	updatedAgent := &agentv1alpha1.PlatformAgent{}
	err = cl.Get(ctx, req.NamespacedName, updatedAgent)
	if err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	if !controllerutil.ContainsFinalizer(updatedAgent, platformAgentFinalizer) {
		t.Errorf("expected finalizer %q to be added, but got %v", platformAgentFinalizer, updatedAgent.Finalizers)
	}

	// 2nd Reconcile: creates resources
	_, err = r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}

	// Verify resources were created

	// PVC
	pvc := &corev1.PersistentVolumeClaim{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-data", Namespace: "test-ns"}, pvc); err != nil {
		t.Errorf("failed to get PVC: %v", err)
	} else if len(pvc.OwnerReferences) != 1 || pvc.OwnerReferences[0].Kind != "PlatformAgent" {
		t.Errorf("expected PVC to have OwnerReference to PlatformAgent")
	}

	// ConfigMaps
	configMap := &corev1.ConfigMap{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-config", Namespace: "test-ns"}, configMap); err != nil {
		t.Errorf("failed to get ConfigMap test-agent-config: %v", err)
	} else if len(configMap.OwnerReferences) != 1 || configMap.OwnerReferences[0].Kind != "PlatformAgent" {
		t.Errorf("expected ConfigMap to have OwnerReference to PlatformAgent")
	}

	fluentBitConfigMap := &corev1.ConfigMap{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-fluent-bit-config", Namespace: "test-ns"}, fluentBitConfigMap); err != nil {
		t.Errorf("failed to get ConfigMap test-agent-fluent-bit-config: %v", err)
	} else if len(fluentBitConfigMap.OwnerReferences) != 1 || fluentBitConfigMap.OwnerReferences[0].Kind != "PlatformAgent" {
		t.Errorf("expected FluentBit ConfigMap to have OwnerReference to PlatformAgent")
	}

	settingsConfigMap := &corev1.ConfigMap{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-settings", Namespace: "test-ns"}, settingsConfigMap); err != nil {
		t.Errorf("failed to get ConfigMap test-agent-settings: %v", err)
	} else if len(settingsConfigMap.OwnerReferences) != 1 || settingsConfigMap.OwnerReferences[0].Kind != "PlatformAgent" {
		t.Errorf("expected Settings ConfigMap to have OwnerReference to PlatformAgent")
	}

	// Deployment
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-gateway", Namespace: "test-ns"}, dep); err != nil {
		t.Errorf("failed to get Deployment: %v", err)
	} else {
		if len(dep.OwnerReferences) != 1 || dep.OwnerReferences[0].Kind != "PlatformAgent" {
			t.Errorf("expected Deployment to have OwnerReference to PlatformAgent")
		}
		if len(dep.Spec.Template.Spec.Containers) == 0 || dep.Spec.Template.Spec.Containers[0].Name != "platform-agent" {
			t.Errorf("expected Deployment to have container named 'platform-agent'")
		}
	}

	// Service
	svc := &corev1.Service{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}, svc); err != nil {
		t.Errorf("failed to get Service: %v", err)
	} else if len(svc.OwnerReferences) != 1 || svc.OwnerReferences[0].Kind != "PlatformAgent" {
		t.Errorf("expected Service to have OwnerReference to PlatformAgent")
	}

	// RBAC
	explorerRole := &rbacv1.ClusterRole{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "kubeagents:explorer:test-ns:test-agent"}, explorerRole); err != nil {
		t.Errorf("failed to get ClusterRole: %v", err)
	}

	crbViewer := &rbacv1.ClusterRoleBinding{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "kubeagents:viewer:test-ns:test-agent"}, crbViewer); err != nil {
		t.Errorf("failed to get ClusterRoleBinding viewer: %v", err)
	}

	crbExplorer := &rbacv1.ClusterRoleBinding{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "kubeagents:explorer:test-ns:test-agent"}, crbExplorer); err != nil {
		t.Errorf("failed to get ClusterRoleBinding explorer: %v", err)
	}

	// Test Deletion
	err = cl.Delete(ctx, updatedAgent)
	if err != nil {
		t.Fatalf("failed to delete agent: %v", err)
	}

	// Reconcile after deletion timestamp is set
	_, err = r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile on delete failed: %v", err)
	}

	// Verify agent is deleted completely (because finalizer was removed)
	err = cl.Get(ctx, req.NamespacedName, updatedAgent)
	if err == nil {
		t.Fatalf("expected agent to be deleted, but it still exists")
	} else if !errors.IsNotFound(err) {
		t.Fatalf("expected NotFound error, got: %v", err)
	}

	// Verify RBAC roles are deleted
	err = cl.Get(ctx, types.NamespacedName{Name: "kubeagents:explorer:test-ns:test-agent"}, explorerRole)
	if err == nil {
		t.Errorf("expected ClusterRole to be deleted")
	}
}
