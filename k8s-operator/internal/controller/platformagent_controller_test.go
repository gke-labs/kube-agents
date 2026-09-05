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
	"encoding/json"
	"fmt"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	nodev1 "k8s.io/api/node/v1"
	policyv1 "k8s.io/api/policy/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"k8s.io/apimachinery/pkg/version"
	"k8s.io/client-go/discovery"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/utils/ptr"
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

func defaultTestNetpolProfile() netpolProfile {
	return netpolProfile{
		Generated:            true,
		DNSClusterIPs:        []string{defaultDNSClusterIP},
		DNSSource:            netpolSourceDefault,
		MetadataDaemonIP:     metadataDaemonIP,
		MetadataDaemonPort:   metadataDaemonDefaultPort,
		MetadataDaemonSource: netpolSourceDefault,
	}
}

// ssaApplyInterceptor is the shorter name the credential-broker tests use for
// the fake client's Server-Side Apply support. One aliases the other.
func ssaApplyInterceptor() interceptor.Funcs { return fakeServerSideApplyInterceptors() }

// fakeServerSideApplyInterceptors returns interceptor.Funcs to handle Server-Side Apply (SSA) in the controller-runtime fake client.
func fakeServerSideApplyInterceptors() interceptor.Funcs {
	return interceptor.Funcs{
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

	// Create a fake client with the PlatformAgent
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
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
	authC, found := findContainer(dep.Spec.Template.Spec, "agent-api-auth")
	if !found {
		t.Errorf("expected Deployment to contain the agent-API front door")
	} else if authC.RestartPolicy == nil || *authC.RestartPolicy != corev1.ContainerRestartPolicyAlways {
		t.Errorf("the front door must be a native sidecar (restartPolicy: Always) so it binds its ports before the agent container starts")
	}

	// The credential runtime is a Deployment of its own, reconciled alongside.
	brokerDep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-credential-proxy", Namespace: "test-ns"}, brokerDep); err != nil {
		t.Errorf("failed to get the credential broker Deployment: %v", err)
	} else if _, found := findContainer(brokerDep.Spec.Template.Spec, "envoy-credential-proxy"); !found {
		t.Errorf("the broker Deployment does not run the credential runtime")
	}

	// Service
	svc := &corev1.Service{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}, svc); err != nil {
		t.Errorf("failed to get Service: %v", err)
	} else if len(svc.OwnerReferences) != 1 || svc.OwnerReferences[0].Kind != "PlatformAgent" {
		t.Errorf("expected Service to have OwnerReference to PlatformAgent")
	}

	// NetworkPolicy
	netpol := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-gateway-netpol", Namespace: "test-ns"}, netpol); err != nil {
		t.Errorf("failed to get NetworkPolicy: %v", err)
	} else if len(netpol.OwnerReferences) != 1 || netpol.OwnerReferences[0].Kind != "PlatformAgent" {
		t.Errorf("expected NetworkPolicy to have OwnerReference to PlatformAgent")
	}

	// RBAC
	minimalRole := &rbacv1.ClusterRole{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "kubeagents:minimal:test-ns:test-agent"}, minimalRole); err != nil {
		t.Errorf("failed to get minimal ClusterRole: %v", err)
	}

	crbMinimal := &rbacv1.ClusterRoleBinding{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "kubeagents:minimal:test-ns:test-agent"}, crbMinimal); err != nil {
		t.Errorf("failed to get ClusterRoleBinding minimal: %v", err)
	}

	localRole := &rbacv1.Role{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: "test-ns", Name: "kubeagents:local:test-ns:test-agent"}, localRole); err != nil {
		t.Errorf("failed to get local Role: %v", err)
	}

	localRoleBinding := &rbacv1.RoleBinding{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: "test-ns", Name: "kubeagents:local:test-ns:test-agent"}, localRoleBinding); err != nil {
		t.Errorf("failed to get local RoleBinding: %v", err)
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

	// Verify cluster-scoped RBAC roles and bindings are deleted by handleDeletion finalizer
	err = cl.Get(ctx, types.NamespacedName{Name: "kubeagents:minimal:test-ns:test-agent"}, minimalRole)
	if err == nil {
		t.Errorf("expected minimal ClusterRole to be deleted")
	}

	err = cl.Get(ctx, types.NamespacedName{Name: "kubeagents:minimal:test-ns:test-agent"}, crbMinimal)
	if err == nil {
		t.Errorf("expected minimal ClusterRoleBinding to be deleted")
	}

	err = cl.Get(ctx, types.NamespacedName{Namespace: "test-ns", Name: "kubeagents:leader:test-ns:test-agent"}, &rbacv1.RoleBinding{})
	if err == nil {
		t.Errorf("expected leader RoleBinding to be deleted")
	}
}

func TestDeleteLegacyCredentialIsolationResources(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns", UID: types.UID("agent-uid")},
	}
	ownerReference := metav1.OwnerReference{
		APIVersion: agentv1alpha1.GroupVersion.String(),
		Kind:       "PlatformAgent",
		Name:       agent.Name,
		UID:        agent.UID,
		Controller: ptr.To(true),
	}
	removed := []client.Object{
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "test-agent-sandbox", Namespace: "test-ns", OwnerReferences: []metav1.OwnerReference{ownerReference}}},
		&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: "test-agent-sandbox", Namespace: "test-ns", OwnerReferences: []metav1.OwnerReference{ownerReference}}},
	}
	// The credential proxy's own Deployment and Service carry these names again,
	// so the cleanup has to leave them alone; deleting them tore down the pod the
	// same reconcile had just applied.
	live := []client.Object{
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "test-agent-credential-proxy", Namespace: "test-ns", OwnerReferences: []metav1.OwnerReference{ownerReference}}},
		&corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: "test-agent-credential-proxy", Namespace: "test-ns", OwnerReferences: []metav1.OwnerReference{ownerReference}}},
	}
	// The metadata-deny NetworkPolicy is a guardrail this controller does not
	// create, so deleting it is out of bounds and it belongs on the survivor
	// side of this test, not the deleted side. Owned here on purpose: an owner
	// reference is the one thing that would have made deleting it defensible,
	// and it must survive even so.
	guardrail := &networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: "test-agent-sandbox-metadata-deny", Namespace: "test-ns", OwnerReferences: []metav1.OwnerReference{ownerReference}}}

	objects := append([]client.Object{agent, guardrail}, removed...)
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append(objects, live...)...).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.deleteLegacyCredentialIsolationResources(context.Background(), agent); err != nil {
		t.Fatalf("deleteLegacyCredentialIsolationResources failed: %v", err)
	}
	for _, object := range removed {
		err := cl.Get(context.Background(), client.ObjectKeyFromObject(object), object)
		if !errors.IsNotFound(err) {
			t.Errorf("expected legacy %T to be deleted, got %v", object, err)
		}
	}
	for _, object := range live {
		if err := cl.Get(context.Background(), client.ObjectKeyFromObject(object), object); err != nil {
			t.Errorf("expected credential proxy %T %s to survive the legacy cleanup, got %v",
				object, object.GetName(), err)
		}
	}
	surviving := &networkingv1.NetworkPolicy{}
	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(guardrail), surviving); err != nil {
		t.Errorf("the metadata-deny NetworkPolicy is a guardrail the controller does not create; it must survive a reconcile, got %v", err)
	}
}

// TestReconcileDoesNotDeleteTheMetadataDenyGuardrail runs a full Reconcile
// rather than the cleanup helper alone, so the assertion holds no matter which
// step of Reconcile a future change wires the deletion into.
//
// The unowned case is the one that was a live bug rather than only a doctrinal
// one. The operator stopped creating this policy, so a copy applied by hand —
// which the security documentation tells an operator to do — is owned by
// nobody, hit the IsControlledBy guard, and returned "refusing to delete
// unowned legacy *v1.NetworkPolicy" from every reconcile. The cleanup runs
// after the workload and before updateStatusReady, so the CR's status stopped
// tracking reality while the agent itself kept running. Both cases are checked
// here because the fix has to be "the name is off the list", not "the guard
// got friendlier".
func TestReconcileDoesNotDeleteTheMetadataDenyGuardrail(t *testing.T) {
	for _, tc := range []struct {
		name  string
		owned bool
	}{
		{name: "applied by the operator in an earlier release", owned: true},
		{name: "applied by hand, owned by nobody", owned: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			agent := &agentv1alpha1.PlatformAgent{
				ObjectMeta: metav1.ObjectMeta{
					Name:       "test-agent",
					Namespace:  "test-ns",
					UID:        types.UID("agent-uid"),
					Finalizers: []string{platformAgentFinalizer},
				},
				Spec: agentv1alpha1.PlatformAgentSpec{
					Harness: &agentv1alpha1.HarnessSpec{
						ProjectID:   "proj",
						Location:    "us-central1",
						ClusterName: "cluster",
					},
				},
			}
			policy := &networkingv1.NetworkPolicy{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-agent-sandbox-metadata-deny",
					Namespace: "test-ns",
				},
			}
			if tc.owned {
				policy.OwnerReferences = []metav1.OwnerReference{{
					APIVersion: agentv1alpha1.GroupVersion.String(),
					Kind:       "PlatformAgent",
					Name:       agent.Name,
					UID:        agent.UID,
					Controller: ptr.To(true),
				}}
			}
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(agent, policy).
				WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
				WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
				Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

			req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
			if _, err := r.Reconcile(context.Background(), req); err != nil {
				t.Fatalf("Reconcile failed: %v", err)
			}

			if err := cl.Get(context.Background(), client.ObjectKeyFromObject(policy), &networkingv1.NetworkPolicy{}); err != nil {
				t.Fatalf("Reconcile deleted the metadata-deny NetworkPolicy; a controller must not delete a guardrail it did not create: %v", err)
			}

			// The status is the half the hot loop took away: the reconcile
			// returned an error before updateStatusReady, so the CR stopped
			// being updated at all. Asserting only that the policy survived
			// would pass against a controller that still errors out.
			stored := &agentv1alpha1.PlatformAgent{}
			if err := cl.Get(context.Background(), client.ObjectKeyFromObject(agent), stored); err != nil {
				t.Fatalf("failed to re-read the agent: %v", err)
			}
			if stored.Status.Phase == "" {
				t.Error("Reconcile completed without writing a status phase; the legacy cleanup is still " +
					"failing the reconcile before updateStatusReady")
			}
		})
	}
}

func TestReconcileRBAC_DeletesLegacyRBAC(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
	legacyViewer := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name: "kubeagents:viewer:test-ns:test-agent",
			Labels: map[string]string{
				"app.kubernetes.io/instance": "test-ns-test-agent",
				"app.kubernetes.io/part-of":  "kube-agents",
			},
		},
		Subjects: []rbacv1.Subject{{Kind: "ServiceAccount", Name: "test-agent", Namespace: "test-ns"}},
	}
	legacyExplorerCRB := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name: "kubeagents:explorer:test-ns:test-agent",
			Labels: map[string]string{
				"app.kubernetes.io/instance": "test-ns-test-agent",
				"app.kubernetes.io/part-of":  "kube-agents",
			},
		},
		Subjects: []rbacv1.Subject{{Kind: "ServiceAccount", Name: "test-agent", Namespace: "test-ns"}},
	}
	legacyExplorerCR := &rbacv1.ClusterRole{
		ObjectMeta: metav1.ObjectMeta{
			Name: "kubeagents:explorer:test-ns:test-agent",
			Labels: map[string]string{
				"app.kubernetes.io/instance": "test-ns-test-agent",
				"app.kubernetes.io/part-of":  "kube-agents",
			},
		},
	}
	legacyRoleBinding := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "kubeagents-test-agent-rolebinding", Namespace: "test-ns"},
		Subjects: []rbacv1.Subject{
			{Kind: "ServiceAccount", Name: "test-agent", Namespace: "test-ns"},
		},
	}
	unrelatedRoleBinding := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "kubeagents-other-agent-rolebinding", Namespace: "test-ns"},
		Subjects: []rbacv1.Subject{
			{Kind: "ServiceAccount", Name: "other-agent", Namespace: "test-ns"},
		},
	}

	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, legacyViewer, legacyExplorerCRB, legacyExplorerCR, legacyRoleBinding, unrelatedRoleBinding).WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.reconcileRBAC(context.Background(), agent); err != nil {
		t.Fatalf("reconcileRBAC failed: %v", err)
	}

	for _, obj := range []client.Object{legacyViewer, legacyExplorerCRB, legacyExplorerCR, legacyRoleBinding} {
		if err := cl.Get(context.Background(), client.ObjectKeyFromObject(obj), obj); !errors.IsNotFound(err) {
			t.Errorf("expected legacy RBAC %T %s to be deleted, got %v", obj, obj.GetName(), err)
		}
	}

	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(unrelatedRoleBinding), unrelatedRoleBinding); err != nil {
		t.Errorf("expected unrelated RoleBinding %s to be preserved, got error: %v", unrelatedRoleBinding.GetName(), err)
	}
}

func TestReconcileRBAC_DeletesLegacyRBAC_ServiceAccountSwap(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Security: &agentv1alpha1.SecuritySpec{
					ServiceAccountName: "custom-sa",
				},
			},
		},
	}
	oldDefaultSARoleBinding := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "kubeagents-test-agent-rolebinding", Namespace: "test-ns"},
		Subjects: []rbacv1.Subject{
			{Kind: "ServiceAccount", Name: "test-agent", Namespace: "test-ns"},
		},
	}

	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, oldDefaultSARoleBinding).WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.reconcileRBAC(context.Background(), agent); err != nil {
		t.Fatalf("reconcileRBAC failed: %v", err)
	}

	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(oldDefaultSARoleBinding), oldDefaultSARoleBinding); !errors.IsNotFound(err) {
		t.Errorf("expected old default SA RoleBinding %s to be deleted after SA swap, got %v", oldDefaultSARoleBinding.GetName(), err)
	}
}

func TestPlatformAgentReconciler_Reconcile_MissingRuntimeClass(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-missing-rc",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Availability: &agentv1alpha1.AvailabilitySpec{
						RuntimeClassName: ptr.To("gvisor"),
					},
				},
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      "test-agent-missing-rc",
			Namespace: "test-ns",
		},
	}
	ctx := context.Background()

	// 1st Reconcile: Adds finalizer
	_, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}

	// 2nd Reconcile: Validates RuntimeClass and halts deployment creation
	res, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}
	if res.RequeueAfter != 30*time.Second {
		t.Errorf("expected RequeueAfter 30s, got %v", res.RequeueAfter)
	}

	// Verify status is Degraded
	updatedAgent := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, updatedAgent); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	if updatedAgent.Status.Phase != "Degraded" {
		t.Errorf("expected Status.Phase Degraded, got %q", updatedAgent.Status.Phase)
	}
	cond := meta.FindStatusCondition(updatedAgent.Status.Conditions, "Ready")
	if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != "RuntimeClassNotFound" {
		t.Errorf("expected Ready condition False with reason RuntimeClassNotFound, got %v", cond)
	}

	// Verify Deployment was NOT created
	dep := &appsv1.Deployment{}
	err = cl.Get(ctx, types.NamespacedName{Name: "test-agent-missing-rc-gateway", Namespace: "test-ns"}, dep)
	if !errors.IsNotFound(err) {
		t.Errorf("expected Deployment to not be created when RuntimeClass is missing, got err: %v", err)
	}
}

func TestPlatformAgentReconciler_Reconcile_ExistingRuntimeClass(t *testing.T) {
	scheme := setupScheme()

	rc := &nodev1.RuntimeClass{
		ObjectMeta: metav1.ObjectMeta{
			Name: "gvisor",
		},
		Handler: "gvisor",
	}

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-existing-rc",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Availability: &agentv1alpha1.AvailabilitySpec{
						RuntimeClassName: ptr.To("gvisor"),
					},
				},
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent), rc).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      "test-agent-existing-rc",
			Namespace: "test-ns",
		},
	}
	ctx := context.Background()

	// 1st Reconcile: Adds finalizer
	_, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}

	// 2nd Reconcile: Validates existing RuntimeClass and creates resources
	res, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}
	// No plugins, so no 30s plugin recheck. There is no collector Service in the fake
	// client either, so telemetry falls through to the managed default and asks to be
	// re-probed later.
	if res.RequeueAfter != otelRediscoverAfter {
		t.Errorf("expected RequeueAfter %v, got %v", otelRediscoverAfter, res.RequeueAfter)
	}

	// Verify Deployment was created with RuntimeClassName "gvisor"
	dep := &appsv1.Deployment{}
	err = cl.Get(ctx, types.NamespacedName{Name: "test-agent-existing-rc-gateway", Namespace: "test-ns"}, dep)
	if err != nil {
		t.Fatalf("expected Deployment to be created when RuntimeClass exists, got err: %v", err)
	}
	if dep.Spec.Template.Spec.RuntimeClassName == nil || *dep.Spec.Template.Spec.RuntimeClassName != "gvisor" {
		t.Errorf("expected Deployment RuntimeClassName 'gvisor', got %v", dep.Spec.Template.Spec.RuntimeClassName)
	}

	// Verify status is not Degraded
	updatedAgent := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, updatedAgent); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	if updatedAgent.Status.Phase == "Degraded" {
		t.Errorf("expected Status.Phase not Degraded when RuntimeClass exists, got %q", updatedAgent.Status.Phase)
	}
}

func TestPlatformAgentReconciler_Reconcile_PodUnschedulable(t *testing.T) {
	scheme := setupScheme()

	rc := &nodev1.RuntimeClass{
		ObjectMeta: metav1.ObjectMeta{
			Name: "gvisor",
		},
		Handler: "gvisor",
	}

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-unschedulable",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Availability: &agentv1alpha1.AvailabilitySpec{
						RuntimeClassName: ptr.To("gvisor"),
					},
				},
			},
		},
	}

	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-unschedulable-sandbox-pod",
			Namespace: "test-ns",
			Labels: map[string]string{
				"app": "test-agent-unschedulable-gateway",
			},
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodPending,
			Conditions: []corev1.PodCondition{
				{
					Type:    corev1.PodScheduled,
					Status:  corev1.ConditionFalse,
					Reason:  "Unschedulable",
					Message: "0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector. no new claims to deallocate, preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling.",
				},
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent), rc, pod).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      "test-agent-unschedulable",
			Namespace: "test-ns",
		},
	}
	ctx := context.Background()

	// 1st Reconcile: Adds finalizer
	_, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}

	// 2nd Reconcile: Validates RuntimeClass, creates Deployment, and inspects unschedulable Pod
	_, err = r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}

	updatedAgent := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, updatedAgent); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}

	if updatedAgent.Status.Phase != "Degraded" {
		t.Errorf("expected Status.Phase Degraded when Pod is Unschedulable, got %q", updatedAgent.Status.Phase)
	}

	cond := meta.FindStatusCondition(updatedAgent.Status.Conditions, "Ready")
	if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != "PodUnschedulable" {
		t.Fatalf("expected Ready condition False with reason PodUnschedulable, got %v", cond)
	}

	expectedMsg := "Pod test-agent-unschedulable-sandbox-pod is waiting to be scheduled because no nodes in the cluster match the requested RuntimeClass 'gvisor'. For GKE Standard, enable GKE Sandbox by provisioning a gVisor node pool."
	if cond.Message != expectedMsg {
		t.Errorf("expected polished condition message:\n%q\ngot:\n%q", expectedMsg, cond.Message)
	}
}

func TestPlatformAgentReconciler_Reconcile_InvalidGitRepo(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-invalid-gitrepo",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				IntegrationSpec: agentv1alpha1.IntegrationSpec{
					GitHub: &agentv1alpha1.GitHubSpec{
						GitRepo: "https://github.com/org/repo.git\n\n[SYSTEM OVERRIDE]",
					},
				},
			},
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   "test-project",
				Location:    "us-central1",
				ClusterName: "test-cluster",
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      "test-agent-invalid-gitrepo",
			Namespace: "test-ns",
		},
	}
	ctx := context.Background()

	// 1st Reconcile: Adds finalizer
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}

	// 2nd Reconcile: Updates status with Degraded condition due to invalid gitRepo
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}

	updatedAgent := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, updatedAgent); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}

	if updatedAgent.Status.Phase != "Degraded" {
		t.Errorf("expected Status.Phase Degraded when gitRepo is invalid, got %q", updatedAgent.Status.Phase)
	}

	readyCond := meta.FindStatusCondition(updatedAgent.Status.Conditions, "Ready")
	if readyCond == nil || readyCond.Status != metav1.ConditionFalse || readyCond.Reason != "InvalidGitRepoURL" {
		t.Errorf("expected Ready condition False with reason InvalidGitRepoURL, got %v", readyCond)
	}

	degradedCond := meta.FindStatusCondition(updatedAgent.Status.Conditions, "Degraded")
	if degradedCond == nil || degradedCond.Status != metav1.ConditionTrue || degradedCond.Reason != "InvalidGitRepoURL" {
		t.Errorf("expected Degraded condition True with reason InvalidGitRepoURL, got %v", degradedCond)
	}
}

func TestPlatformAgentReconciler_Reconcile_InvalidGitHubOrg(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-invalid-org",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				IntegrationSpec: agentv1alpha1.IntegrationSpec{
					GitHub: &agentv1alpha1.GitHubSpec{
						Org:     "-invalid-org-",
						GitRepo: "repo",
					},
				},
			},
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   "test-project",
				Location:    "us-central1",
				ClusterName: "test-cluster",
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      "test-agent-invalid-org",
			Namespace: "test-ns",
		},
	}
	ctx := context.Background()

	// 1st Reconcile: Adds finalizer
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}

	// 2nd Reconcile: Updates status with Degraded condition due to invalid org
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}

	updatedAgent := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, updatedAgent); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}

	if updatedAgent.Status.Phase != "Degraded" {
		t.Errorf("expected Status.Phase Degraded when org is invalid, got %q", updatedAgent.Status.Phase)
	}

	degradedCond := meta.FindStatusCondition(updatedAgent.Status.Conditions, "Degraded")
	if degradedCond == nil || degradedCond.Status != metav1.ConditionTrue || degradedCond.Reason != "InvalidGitRepoURL" {
		t.Errorf("expected Degraded condition True with reason InvalidGitRepoURL, got %v", degradedCond)
	}
}

func findAPIServerEgressRule(netpol *networkingv1.NetworkPolicy) *networkingv1.NetworkPolicyEgressRule {
	if netpol == nil {
		return nil
	}
	for i := range netpol.Spec.Egress {
		for _, p := range netpol.Spec.Egress[i].Ports {
			if p.Port != nil && p.Port.IntVal == 6443 {
				return &netpol.Spec.Egress[i]
			}
		}
	}
	return nil
}

func findDNSEgressRule(netpol *networkingv1.NetworkPolicy) *networkingv1.NetworkPolicyEgressRule {
	if netpol == nil {
		return nil
	}
	for i := range netpol.Spec.Egress {
		for _, p := range netpol.Spec.Egress[i].Ports {
			if p.Port != nil && p.Port.IntVal == 53 {
				return &netpol.Spec.Egress[i]
			}
		}
	}
	return nil
}

func TestBuildNetworkPolicy(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	netpol := buildNetworkPolicy(agent, nil, defaultTestNetpolProfile(), false, "", false)
	if netpol.Name != "test-agent-gateway-netpol" {
		t.Errorf("expected Name 'test-agent-gateway-netpol', got %s", netpol.Name)
	}
	if netpol.Namespace != "test-ns" {
		t.Errorf("expected Namespace 'test-ns', got %s", netpol.Namespace)
	}
	deploy := buildDeployment(agent, "", "", "", "", nil, renderOptions{imageVolumeSupported: false})
	if !reflect.DeepEqual(netpol.Spec.PodSelector.MatchLabels, deploy.Spec.Selector.MatchLabels) {
		t.Errorf("expected PodSelector %v to match Deployment selector labels %v", netpol.Spec.PodSelector.MatchLabels, deploy.Spec.Selector.MatchLabels)
	}
	if len(netpol.Spec.PolicyTypes) != 2 {
		t.Errorf("expected 2 PolicyTypes, got %d", len(netpol.Spec.PolicyTypes))
	}
	if len(netpol.Spec.Ingress) != 1 {
		t.Fatalf("expected 1 Ingress rule, got %d", len(netpol.Spec.Ingress))
	}
	if len(netpol.Spec.Ingress[0].Ports) != 3 {
		t.Errorf("expected 3 ports in agent namespace ingress rule when dashboard enabled, got %d", len(netpol.Spec.Ingress[0].Ports))
	}
	if len(netpol.Spec.Egress) != 12 {
		t.Errorf("expected 12 Egress rules (DNS, GCP Metadata port 80, GCP Metadata port 988, LiteLLM Gateway, vLLM Gemma, K8s Control Plane, External HTTPS, GKE OTel Collector, GitHub Token Minter, Hindsight API, shell sandbox sshd, credential broker), got %d", len(netpol.Spec.Egress))
	}

	findEgressRule := func(port int32, peerCheck func(networkingv1.NetworkPolicyPeer) bool) *networkingv1.NetworkPolicyEgressRule {
		for i := range netpol.Spec.Egress {
			for _, p := range netpol.Spec.Egress[i].Ports {
				if p.Port != nil && p.Port.IntVal == port {
					for _, peer := range netpol.Spec.Egress[i].To {
						if peerCheck(peer) {
							return &netpol.Spec.Egress[i]
						}
					}
				}
			}
		}
		return nil
	}

	// 5 peers: the kube-dns and node-local-dns selectors, the NodeLocal DNSCache
	// link-local address, the Cloud DNS resolver at 169.254.169.254, and the one
	// resolved ClusterIP.
	ruleDNS := findEgressRule(53, func(p networkingv1.NetworkPolicyPeer) bool {
		return p.PodSelector != nil && p.PodSelector.MatchLabels["k8s-app"] == "kube-dns"
	})
	if ruleDNS == nil || len(ruleDNS.To) != 5 {
		t.Errorf("expected 5 peers in DNS egress rule")
	}
	if findEgressRule(53, func(p networkingv1.NetworkPolicyPeer) bool {
		return p.IPBlock != nil && p.IPBlock.CIDR == "169.254.169.254/32"
	}) == nil {
		t.Error("the DNS rule does not name 169.254.169.254, so a Cloud DNS for GKE cluster " +
			"cannot resolve and every named destination below becomes unreachable")
	}
	ruleMeta80 := findEgressRule(80, func(p networkingv1.NetworkPolicyPeer) bool {
		return p.IPBlock != nil && p.IPBlock.CIDR == "169.254.169.254/32"
	})
	if ruleMeta80 == nil || len(ruleMeta80.To) != 1 {
		t.Errorf("expected 1 peer in GCP Workload Identity egress rule (port 80)")
	}
	// Port 988 is the post-DNAT destination, so it carries the metadata daemon's own
	// address as well as the link-local one even when the cluster has no nodes.
	ruleMeta988 := findEgressRule(988, func(p networkingv1.NetworkPolicyPeer) bool {
		return p.IPBlock != nil && p.IPBlock.CIDR == "169.254.169.252/32"
	})
	if ruleMeta988 == nil || len(ruleMeta988.To) != 2 {
		t.Errorf("expected 2 peers in GCP Workload Identity egress rule (port 988)")
	}
	ruleLiteLLM := findEgressRule(4000, func(p networkingv1.NetworkPolicyPeer) bool {
		return p.PodSelector != nil && p.PodSelector.MatchLabels["app"] == "litellm"
	})
	if ruleLiteLLM == nil || ruleLiteLLM.To[0].PodSelector.MatchLabels["app"] != "litellm" {
		t.Errorf("expected LiteLLM egress rule to match app 'litellm'")
	}
	rulevLLM := findEgressRule(8000, func(p networkingv1.NetworkPolicyPeer) bool {
		return p.PodSelector != nil && p.PodSelector.MatchLabels["app"] == "gemma-server"
	})
	if rulevLLM == nil || rulevLLM.To[0].PodSelector.MatchLabels["app"] != "gemma-server" {
		t.Errorf("expected vLLM Gemma egress rule to match app 'gemma-server'")
	}
	ruleK8s := findEgressRule(6443, func(p networkingv1.NetworkPolicyPeer) bool { return p.IPBlock != nil })
	if ruleK8s == nil || !strings.HasSuffix(ruleK8s.To[0].IPBlock.CIDR, "/32") {
		t.Errorf("expected K8s API server CIDR with /32 suffix")
	}
	ruleHTTPS := findEgressRule(443, func(p networkingv1.NetworkPolicyPeer) bool { return p.IPBlock != nil && p.IPBlock.CIDR == "0.0.0.0/0" })
	if ruleHTTPS == nil || len(ruleHTTPS.To[0].IPBlock.Except) != 5 {
		t.Errorf("expected 5 Except subnets in External HTTPS egress rule")
	}
	ruleOTel := findEgressRule(4317, func(p networkingv1.NetworkPolicyPeer) bool {
		return p.NamespaceSelector != nil && p.NamespaceSelector.MatchLabels["kubernetes.io/metadata.name"] == "gke-managed-otel"
	})
	if ruleOTel == nil || ruleOTel.To[0].NamespaceSelector.MatchLabels["kubernetes.io/metadata.name"] != "gke-managed-otel" {
		t.Errorf("expected GKE OTel Collector egress rule to match namespace 'gke-managed-otel'")
	}
	ruleMinter := findEgressRule(8080, func(p networkingv1.NetworkPolicyPeer) bool {
		return p.PodSelector != nil && p.PodSelector.MatchLabels["app"] == "github-token-minter"
	})
	if ruleMinter == nil || ruleMinter.To[0].PodSelector == nil || ruleMinter.To[0].PodSelector.MatchLabels["app"] != "github-token-minter" {
		t.Errorf("expected GitHub Token Minter egress rule to match app 'github-token-minter'")
	}
	// Both labels, not just the name: the postgresql pod carries
	// app.kubernetes.io/name=hindsight too, and the database is meant to be
	// reachable from the API pod alone (hindsight/networkpolicy.yaml).
	ruleHindsight := findEgressRule(8888, func(p networkingv1.NetworkPolicyPeer) bool {
		return p.PodSelector != nil && p.PodSelector.MatchLabels["app.kubernetes.io/name"] == "hindsight"
	})
	if ruleHindsight == nil || ruleHindsight.To[0].PodSelector.MatchLabels["app.kubernetes.io/component"] != "api" {
		t.Errorf("expected Hindsight egress rule on 8888 to match the api component, not every hindsight pod")
	}
}

func TestBuildNetworkPolicy_DashboardDisabled(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				Hermes: &agentv1alpha1.HermesSpec{
					DashboardEnabled: ptr.To(false),
				},
			},
		},
	}

	netpol := buildNetworkPolicy(agent, nil, defaultTestNetpolProfile(), false, "", false)
	if len(netpol.Spec.Ingress) != 1 {
		t.Fatalf("expected 1 Ingress rule, got %d", len(netpol.Spec.Ingress))
	}
	if len(netpol.Spec.Ingress[0].Ports) != 2 {
		t.Errorf("expected 2 ports in agent namespace ingress rule when dashboard disabled, got %d", len(netpol.Spec.Ingress[0].Ports))
	}
}

func TestBuildNetworkPolicy_FQDNEnabled(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
			Annotations: map[string]string{
				AnnotationEnableFQDNNetworkPolicy: "true",
			},
		},
	}

	netpol := buildNetworkPolicy(agent, nil, defaultTestNetpolProfile(), true, "", false)
	// Expected 11 Egress rules when FQDN is enabled (external HTTPS 0.0.0.0/0:443 is omitted):
	// 1. Cluster DNS (53)
	// 2. GCP WI / Metadata server (80)
	// 3. GKE WI Host Network Daemon (988)
	// 4. LiteLLM Gateway (80, 4000, 8080)
	// 5. vLLM Gemma Server (80, 8000)
	// 6. Kubernetes API Server (443, 6443, 8443)
	// 7. GKE Managed OpenTelemetry Collector (4317, 4318)
	// 8. GitHub Token Minter (8080)
	// 9. Hindsight memory API (8888)
	// 10. The shell sandbox's sshd (2222)
	// 11. The credential broker, which hosts the chat relay (8765)
	if len(netpol.Spec.Egress) != 11 {
		t.Errorf("expected 11 Egress rules when FQDN is enabled (external HTTPS omitted), got %d", len(netpol.Spec.Egress))
	}
	for _, egress := range netpol.Spec.Egress {
		for _, peer := range egress.To {
			if peer.IPBlock != nil && peer.IPBlock.CIDR == "0.0.0.0/0" {
				t.Errorf("expected blanket 0.0.0.0/0 egress rule to be omitted when FQDN is enabled")
			}
		}
	}
}

func TestBuildNetworkPolicy_CustomAPIHost(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	netpolIPv4 := buildNetworkPolicy(agent, []string{"10.0.0.5"}, defaultTestNetpolProfile(), false, "", false)
	ruleIPv4 := findAPIServerEgressRule(netpolIPv4)
	if ruleIPv4 == nil || len(ruleIPv4.To) == 0 || ruleIPv4.To[0].IPBlock == nil || ruleIPv4.To[0].IPBlock.CIDR != "10.0.0.5/32" {
		t.Errorf("expected IPv4 CIDR '10.0.0.5/32', got %v", ruleIPv4)
	}

	netpolIPv6 := buildNetworkPolicy(agent, []string{"fd00::1"}, defaultTestNetpolProfile(), false, "", false)
	ruleIPv6 := findAPIServerEgressRule(netpolIPv6)
	if ruleIPv6 == nil || len(ruleIPv6.To) == 0 || ruleIPv6.To[0].IPBlock == nil || ruleIPv6.To[0].IPBlock.CIDR != "fd00::1/128" {
		t.Errorf("expected IPv6 CIDR 'fd00::1/128', got %v", ruleIPv6)
	}
}

func TestBuildNetworkPolicy_InvalidAPIHost(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	tests := []struct {
		name      string
		apiHosts  []string
		wantCIDRs []string
	}{
		{
			name:      "empty list defaults to 10.96.0.1/32",
			apiHosts:  nil,
			wantCIDRs: []string{"10.96.0.1/32"},
		},
		{
			name:      "valid IPv4",
			apiHosts:  []string{"10.0.0.5"},
			wantCIDRs: []string{"10.0.0.5/32"},
		},
		{
			name:      "valid IPv6",
			apiHosts:  []string{"fd00::1"},
			wantCIDRs: []string{"fd00::1/128"},
		},
		{
			name:      "bracket-wrapped IPv6 stripped to valid",
			apiHosts:  []string{"[fd00::1]"},
			wantCIDRs: []string{"fd00::1/128"},
		},
		{
			name:      "hostname falls back to default",
			apiHosts:  []string{"kubernetes.default.svc"},
			wantCIDRs: []string{"10.96.0.1/32"},
		},
		{
			name:      "garbage falls back to default",
			apiHosts:  []string{"not-an-ip"},
			wantCIDRs: []string{"10.96.0.1/32"},
		},
		{
			name:      "multiple endpoints including clusterIP and endpoints",
			apiHosts:  []string{"10.96.0.1", "172.16.0.2", "172.16.0.3"},
			wantCIDRs: []string{"10.96.0.1/32", "172.16.0.2/32", "172.16.0.3/32"},
		},
		{
			name:      "non-canonical CIDRs normalized and deduplicated",
			apiHosts:  []string{"172.16.0.100/24", "172.16.0.0/24"},
			wantCIDRs: []string{"172.16.0.0/24"},
		},
		{
			name:      "overly broad CIDRs rejected",
			apiHosts:  []string{"10.0.0.0/8", "0.0.0.0/0", "::/0", "172.16.0.0/12"},
			wantCIDRs: []string{"172.16.0.0/12"},
		},
		{
			name:      "IPv6 CIDR normalized",
			apiHosts:  []string{"2001:db8:abcd:0012::1/48"},
			wantCIDRs: []string{"2001:db8:abcd::/48"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			netpol := buildNetworkPolicy(agent, tt.apiHosts, defaultTestNetpolProfile(), false, "", false)
			rule := findAPIServerEgressRule(netpol)
			if rule == nil {
				t.Fatalf("API server egress rule (port 6443) not found in netpol")
			}
			var gotCIDRs []string
			for _, peer := range rule.To {
				if peer.IPBlock != nil {
					gotCIDRs = append(gotCIDRs, peer.IPBlock.CIDR)
				}
			}
			if !reflect.DeepEqual(gotCIDRs, tt.wantCIDRs) {
				t.Errorf("apiHosts=%v: expected CIDRs %v, got %v", tt.apiHosts, tt.wantCIDRs, gotCIDRs)
			}
		})
	}
}

func TestBuildNetworkPolicy_Idempotent(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	np1 := buildNetworkPolicy(agent, []string{"10.0.0.5"}, defaultTestNetpolProfile(), false, "", false)
	np2 := buildNetworkPolicy(agent, []string{"10.0.0.5"}, defaultTestNetpolProfile(), false, "", false)
	if !reflect.DeepEqual(np1.Spec, np2.Spec) {
		t.Errorf("buildNetworkPolicy is not idempotent: consecutive calls produced different specs")
	}
}

func TestBuildNetworkPolicy_ExternalHTTPSExceptList(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}
	netpol := buildNetworkPolicy(agent, nil, defaultTestNetpolProfile(), false, "", false)

	var httpsRule *networkingv1.NetworkPolicyEgressRule
	for i := range netpol.Spec.Egress {
		for _, p := range netpol.Spec.Egress[i].Ports {
			if p.Port != nil && p.Port.IntVal == 443 {
				for _, peer := range netpol.Spec.Egress[i].To {
					if peer.IPBlock != nil && peer.IPBlock.CIDR == "0.0.0.0/0" {
						httpsRule = &netpol.Spec.Egress[i]
					}
				}
			}
		}
	}
	if httpsRule == nil {
		t.Fatal("external HTTPS egress rule not found")
	}

	exceptList := httpsRule.To[0].IPBlock.Except
	requiredExcepts := []string{
		"10.0.0.0/8",
		"172.16.0.0/12",
		"192.168.0.0/16",
		"100.64.0.0/10",
		"169.254.0.0/16",
	}
	for _, required := range requiredExcepts {
		found := false
		for _, e := range exceptList {
			if e == required {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("expected %q in External HTTPS except list, got %v", required, exceptList)
		}
	}

	if len(httpsRule.To) < 2 || httpsRule.To[1].IPBlock == nil || httpsRule.To[1].IPBlock.CIDR != "::/0" {
		t.Fatalf("expected IPv6 ::/0 peer in External HTTPS rule, got %v", httpsRule.To)
	}
	ipv6Excepts := httpsRule.To[1].IPBlock.Except
	for _, req := range []string{"fc00::/7", "fe80::/10", "ff00::/8"} {
		found := false
		for _, e := range ipv6Excepts {
			if e == req {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("expected %q in External HTTPS IPv6 except list, got %v", req, ipv6Excepts)
		}
	}
}

func TestBuildNetworkPolicy_ClusterDNS(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	// 1. IPv4 dynamic DNS clusterIP
	netpolGKE := buildNetworkPolicy(agent, nil, netpolProfile{DNSClusterIPs: []string{"34.118.224.10"}, MetadataDaemonIP: metadataDaemonIP}, false, "", false)
	dnsRuleGKE := findDNSEgressRule(netpolGKE)
	if dnsRuleGKE == nil {
		t.Fatalf("DNS egress rule (port 53) not found in netpolGKE")
	}
	foundExactClusterIP := false
	for _, peer := range dnsRuleGKE.To {
		if peer.IPBlock != nil && peer.IPBlock.CIDR == "34.118.224.10/32" {
			foundExactClusterIP = true
			break
		}
	}
	if !foundExactClusterIP {
		t.Errorf("expected 34.118.224.10/32 exact clusterIP in DNS egress peers")
	}

	// 2. IPv6 dynamic DNS clusterIP
	netpolIPv6 := buildNetworkPolicy(agent, nil, netpolProfile{DNSClusterIPs: []string{"2001:db8::10"}, MetadataDaemonIP: metadataDaemonIP}, false, "", false)
	dnsRuleIPv6 := findDNSEgressRule(netpolIPv6)
	if dnsRuleIPv6 == nil {
		t.Fatalf("DNS egress rule (port 53) not found in netpolIPv6")
	}
	foundIPv6DNS := false
	for _, peer := range dnsRuleIPv6.To {
		if peer.IPBlock != nil && peer.IPBlock.CIDR == "2001:db8::10/128" {
			foundIPv6DNS = true
			break
		}
	}
	if !foundIPv6DNS {
		t.Errorf("expected 2001:db8::10/128 in DNS egress peers for IPv6 clusterIP")
	}

	// 3. Fallback when invalid or empty
	netpolFallback := buildNetworkPolicy(agent, nil, netpolProfile{DNSClusterIPs: []string{"invalid-ip"}, MetadataDaemonIP: metadataDaemonIP}, false, "", false)
	dnsRuleFallback := findDNSEgressRule(netpolFallback)
	if dnsRuleFallback == nil {
		t.Fatalf("DNS egress rule (port 53) not found in netpolFallback")
	}
	foundFallback := false
	for _, peer := range dnsRuleFallback.To {
		if peer.IPBlock != nil && peer.IPBlock.CIDR == "10.96.0.10/32" {
			foundFallback = true
			break
		}
	}
	if !foundFallback {
		t.Errorf("expected fallback 10.96.0.10/32 for invalid DNS clusterIP")
	}

	// 4. Dual-stack: both ClusterIPs reach the port-53 rule.
	// TestResolveNetpolProfile/DiscoveryKubeDNS_DualStack proves the resolver returns
	// two addresses; without this case nothing checked that both survive into the
	// policy, so a read of dnsIPs[0] would leave a dual-stack cluster with IPv4-only
	// DNS egress and a green suite.
	netpolDualStack := buildNetworkPolicy(agent, nil, netpolProfile{DNSClusterIPs: []string{"10.96.0.10", "2001:db8::10"}, MetadataDaemonIP: metadataDaemonIP}, false, "", false)
	dnsRuleDualStack := findDNSEgressRule(netpolDualStack)
	if dnsRuleDualStack == nil {
		t.Fatalf("DNS egress rule (port 53) not found in netpolDualStack")
	}
	wantDualStack := map[string]bool{"10.96.0.10/32": false, "2001:db8::10/128": false}
	for _, peer := range dnsRuleDualStack.To {
		if peer.IPBlock == nil {
			continue
		}
		if _, ok := wantDualStack[peer.IPBlock.CIDR]; ok {
			wantDualStack[peer.IPBlock.CIDR] = true
		}
	}
	for cidr, found := range wantDualStack {
		if !found {
			t.Errorf("expected %s in DNS egress peers for a dual-stack kube-dns", cidr)
		}
	}
}

// The two branches buildNetworkPolicy grew for spec.networkPolicy, tested where
// they are implemented. TestResolveNetpolProfile covers the profile they read;
// nothing covered the policy they build, so deleting either append would have
// shipped green.
func TestBuildNetworkPolicy_ProfileDrivenRules(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	t.Run("AdditionalEgressIsAppended", func(t *testing.T) {
		profile := defaultTestNetpolProfile()
		tcp := corev1.ProtocolTCP
		port := intstr.FromInt32(5432)
		profile.AdditionalEgress = []networkingv1.NetworkPolicyEgressRule{{
			Ports: []networkingv1.NetworkPolicyPort{{Protocol: &tcp, Port: &port}},
			To: []networkingv1.NetworkPolicyPeer{{
				IPBlock: &networkingv1.IPBlock{CIDR: "10.200.0.0/16"},
			}},
		}}

		netpol := buildNetworkPolicy(agent, nil, profile, false, "", false)
		found := false
		for _, rule := range netpol.Spec.Egress {
			for _, peer := range rule.To {
				if peer.IPBlock != nil && peer.IPBlock.CIDR == "10.200.0.0/16" {
					found = true
					if len(rule.Ports) != 1 || rule.Ports[0].Port.IntValue() != 5432 {
						t.Errorf("additional egress rule lost its ports: %+v", rule.Ports)
					}
				}
			}
		}
		if !found {
			t.Errorf("profile.AdditionalEgress was not appended to the generated policy")
		}
	})

	t.Run("EmptyMetadataDaemonIPSuppressesRule3", func(t *testing.T) {
		profile := defaultTestNetpolProfile()
		profile.MetadataDaemonIP = ""
		profile.MetadataDaemonSource = netpolSourceSuppressed

		netpol := buildNetworkPolicy(agent, nil, profile, false, "", false)
		for _, rule := range netpol.Spec.Egress {
			for _, p := range rule.Ports {
				if p.Port != nil && p.Port.IntValue() == 988 {
					t.Fatalf("rule 3 (port 988) survived metadataDaemon.endpoint suppression")
				}
			}
		}

		// The pre-DNAT rule 2 is a separate rule and must NOT go with it.
		withDaemon := buildNetworkPolicy(agent, nil, defaultTestNetpolProfile(), false, "", false)
		if len(netpol.Spec.Egress) != len(withDaemon.Spec.Egress)-1 {
			t.Errorf("suppression removed %d rules, want exactly 1", len(withDaemon.Spec.Egress)-len(netpol.Spec.Egress))
		}
		found80 := false
		for _, rule := range netpol.Spec.Egress {
			for _, p := range rule.Ports {
				if p.Port != nil && p.Port.IntValue() == 80 {
					for _, peer := range rule.To {
						if peer.IPBlock != nil && peer.IPBlock.CIDR == metadataLinkLocalIP+"/32" {
							found80 = true
						}
					}
				}
			}
		}
		if !found80 {
			t.Errorf("rule 2 (link-local metadata on port 80) was removed along with rule 3")
		}
	})
}

func TestBuildNetworkPolicy_MetadataDaemonPeers(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	netpol := buildNetworkPolicy(agent, nil, defaultTestNetpolProfile(), false, "", false)

	// The pre-NAT target belongs on port 80.
	got80 := egressCIDRsForPort(netpol, 80)
	want80 := []string{"169.254.169.254/32"}
	if !reflect.DeepEqual(got80, want80) {
		t.Errorf("expected port 80 metadata peers %v, got %v", want80, got80)
	}

	// Port 988 is the post-DNAT destination on Dataplane V1, carrying the metadata
	// daemon's link-local address (169.254.169.252) and the link-local alias.
	got988 := egressCIDRsForPort(netpol, 988)
	want988 := []string{
		"169.254.169.252/32",
		"169.254.169.254/32",
	}
	if !reflect.DeepEqual(got988, want988) {
		t.Errorf("expected metadata daemon peers %v, got %v", want988, got988)
	}

	// Every port the metadata server is reachable on, across all egress rules. 8080 —
	// the pre-NAT ALTS handshaker port — must not be among them: Dataplane V2 evaluates
	// policy pre-NAT at the socket layer, so pairing 8080 with the link-local address
	// reopens the DirectPath route the sandbox refuses. Asserted here rather than left
	// to the platform goldens, which are snapshots that `go test -update` re-blesses
	// from whatever the code emits.
	//
	// 53 is on the list and is not a credential port: under Cloud DNS for GKE the node
	// answers DNS at this address, and the DNS rule names it for that and nothing else.
	// This assertion is the guard on that — a change that widened the DNS rule to carry
	// 80, or the port-80 rule to carry 53, fails here rather than in review.
	gotPorts := egressPortsForCIDR(netpol, metadataLinkLocalIP+"/32")
	wantPorts := []int32{53, 80, 988}
	if !reflect.DeepEqual(gotPorts, wantPorts) {
		t.Errorf("expected the metadata server reachable on ports %v, got %v", wantPorts, gotPorts)
	}
}

func TestBuildNetworkPolicy_CustomMetadataDaemonPort(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}
	profile := defaultTestNetpolProfile()
	profile.MetadataDaemonPort = 1988

	netpol := buildNetworkPolicy(agent, nil, profile, false, "", false)

	got1988 := egressCIDRsForPort(netpol, 1988)
	want1988 := []string{
		"169.254.169.252/32",
		"169.254.169.254/32",
	}
	if !reflect.DeepEqual(got1988, want1988) {
		t.Errorf("expected custom metadata daemon peers %v, got %v", want1988, got1988)
	}

	gotPorts := egressPortsForCIDR(netpol, metadataLinkLocalIP+"/32")
	wantPorts := []int32{53, 80, 1988}
	if !reflect.DeepEqual(gotPorts, wantPorts) {
		t.Errorf("expected the metadata server reachable on ports %v, got %v", wantPorts, gotPorts)
	}
}

// TestBuildNetworkPolicy_ResolverIsNotDuplicatedByDNSClusterIPs covers the
// operator who reads their Cloud DNS nodes' --cluster-dns and puts that value in
// spec.networkPolicy.dnsClusterIPs. It is the same address the DNS rule already
// grants unconditionally, and the two peers are built by separate
// formatCIDRPeers calls, which dedupe only within themselves.
func TestBuildNetworkPolicy_ResolverIsNotDuplicatedByDNSClusterIPs(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}
	profile := defaultTestNetpolProfile()
	profile.DNSClusterIPs = []string{metadataLinkLocalIP}

	netpol := buildNetworkPolicy(agent, nil, profile, false, "", false)

	occurrences := 0
	for i := range netpol.Spec.Egress {
		if !ruleNamesPort(netpol.Spec.Egress[i], 53) {
			continue
		}
		for _, peer := range netpol.Spec.Egress[i].To {
			if peer.IPBlock != nil && peer.IPBlock.CIDR == metadataLinkLocalIP+"/32" {
				occurrences++
			}
		}
	}
	if occurrences != 1 {
		t.Errorf("expected %s/32 exactly once among the port-53 peers, got %d", metadataLinkLocalIP, occurrences)
	}
}

// egressPortsForCIDR returns the sorted, deduplicated ports every egress rule naming
// cidr as an ipBlock peer opens towards it.
func egressPortsForCIDR(netpol *networkingv1.NetworkPolicy, cidr string) []int32 {
	seen := map[int32]bool{}
	for i := range netpol.Spec.Egress {
		matched := false
		for _, peer := range netpol.Spec.Egress[i].To {
			if peer.IPBlock != nil && peer.IPBlock.CIDR == cidr {
				matched = true
				break
			}
		}
		if !matched {
			continue
		}
		for _, p := range netpol.Spec.Egress[i].Ports {
			if p.Port != nil {
				seen[p.Port.IntVal] = true
			}
		}
	}
	ports := make([]int32, 0, len(seen))
	for p := range seen {
		ports = append(ports, p)
	}
	sort.Slice(ports, func(i, j int) bool { return ports[i] < ports[j] })
	return ports
}

// egressCIDRsForPort returns the ipBlock CIDRs of the first egress rule naming port.
func egressCIDRsForPort(netpol *networkingv1.NetworkPolicy, port int32) []string {
	for i := range netpol.Spec.Egress {
		for _, p := range netpol.Spec.Egress[i].Ports {
			if p.Port == nil || p.Port.IntVal != port {
				continue
			}
			var cidrs []string
			for _, peer := range netpol.Spec.Egress[i].To {
				if peer.IPBlock != nil {
					cidrs = append(cidrs, peer.IPBlock.CIDR)
				}
			}
			return cidrs
		}
	}
	return nil
}

// Pressing the emergency stop has to leave a mark somewhere a human looks. The pod
// stays Ready with the watcher off, so `kubectl describe platformagent` is the only
// place that can distinguish a fleet with no incidents from a fleet that stopped
// looking, and an install left switched off is the failure this condition exists to
// prevent.
func TestPlatformAgentReconciler_Reconcile_EventWatcherDisabledCondition(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-watcher-off",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:    "test-project",
				Location:     "us-central1",
				ClusterName:  "test-cluster",
				EventWatcher: &agentv1alpha1.EventWatcherSpec{Enabled: ptr.To(false)},
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{
		Name:      "test-agent-watcher-off",
		Namespace: "test-ns",
	}}
	ctx := context.Background()

	// First adds the finalizer, second writes status.
	for i := 1; i <= 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d failed: %v", i, err)
		}
	}

	updated := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, updated); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}

	cond := meta.FindStatusCondition(updated.Status.Conditions, eventWatcherConditionType)
	if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != eventWatcherDisabledReason {
		t.Fatalf("expected %s condition False/%s, got %v", eventWatcherConditionType, eventWatcherDisabledReason, cond)
	}
	// The message is what the operator reads at 3am, so it has to name the field
	// rather than only the symptom — nothing else tells them how to undo this.
	if !strings.Contains(cond.Message, "spec.harness.eventWatcher.enabled") {
		t.Errorf("the condition must name the field that turns it back on, got %q", cond.Message)
	}
	// Deliberately off is not degraded. Flipping the phase would make the stop
	// look like a fault and hide a real one behind it.
	if updated.Status.Phase == "Degraded" {
		t.Error("disabling the watcher is a decision, not a degradation")
	}

	// Turning it back on has to clear the condition. A stale one would report an
	// install as blind while it is watching, which is the more dangerous of the
	// two ways to be wrong.
	updated.Spec.Harness.EventWatcher.Enabled = ptr.To(true)
	if err := cl.Update(ctx, updated); err != nil {
		t.Fatalf("failed to re-enable the watcher: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after re-enable failed: %v", err)
	}

	restored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, restored); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	if cond := meta.FindStatusCondition(restored.Status.Conditions, eventWatcherConditionType); cond != nil {
		t.Errorf("expected the %s condition to be removed once watching resumes, got %v", eventWatcherConditionType, cond)
	}
}

// The condition must not exist on an install that never mentions the field, which
// is every install today. A condition present on all of them says nothing, and
// would train readers to ignore the one case it is meant to flag.
func TestPlatformAgentReconciler_Reconcile_NoEventWatcherConditionByDefault(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-watcher-default",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   "test-project",
				Location:    "us-central1",
				ClusterName: "test-cluster",
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{
		Name:      "test-agent-watcher-default",
		Namespace: "test-ns",
	}}
	ctx := context.Background()

	for i := 1; i <= 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d failed: %v", i, err)
		}
	}

	updated := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, updated); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	if cond := meta.FindStatusCondition(updated.Status.Conditions, eventWatcherConditionType); cond != nil {
		t.Errorf("expected no %s condition on a default install, got %v", eventWatcherConditionType, cond)
	}
}

// A condition already carrying the right Status and Reason must still have its
// text refreshed. The message is the recovery instruction — what a reader of
// `kubectl describe` is told to do to undo the stop — so a release that rewords
// it has to reach installs that are already stopped. Nothing else about such an
// install changes between reconciles, so if the no-op comparison ignored the
// message the old wording would be frozen there forever.
func TestPlatformAgentReconciler_Reconcile_EventWatcherMessageIsRefreshed(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-watcher-stale",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:    "test-project",
				Location:     "us-central1",
				ClusterName:  "test-cluster",
				EventWatcher: &agentv1alpha1.EventWatcherSpec{Enabled: ptr.To(false)},
			},
		},
		Status: agentv1alpha1.AgentStatus{
			Conditions: []metav1.Condition{{
				Type:               eventWatcherConditionType,
				Status:             metav1.ConditionFalse,
				Reason:             eventWatcherDisabledReason,
				Message:            "wording from a previous release",
				LastTransitionTime: metav1.Now(),
			}},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{
		Name:      "test-agent-watcher-stale",
		Namespace: "test-ns",
	}}
	ctx := context.Background()

	for i := 1; i <= 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d failed: %v", i, err)
		}
	}

	updated := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, updated); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	cond := meta.FindStatusCondition(updated.Status.Conditions, eventWatcherConditionType)
	if cond == nil {
		t.Fatalf("expected the %s condition to survive, got none", eventWatcherConditionType)
	}
	if cond.Message != eventWatcherDisabledMessage {
		t.Errorf("stale condition message was never refreshed:\n got: %q\nwant: %q", cond.Message, eventWatcherDisabledMessage)
	}
}

func TestResolveAgentPlugins_OptInTargeting(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "target-agent",
			Namespace: "test-ns",
		},
	}

	pMatching := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "pmatching", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: "gcr.io/p-matching:v1"},
	}
	pOther := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "pother", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "other-agent", Image: "gcr.io/p-other:v1"},
	}
	pEmpty := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "p-empty", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "", Image: "gcr.io/p-empty:v1"},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(pMatching, pOther, pEmpty).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	ctx := context.Background()
	matched, err := r.resolveAgentPlugins(ctx, agent)
	if err != nil {
		t.Fatalf("resolveAgentPlugins failed: %v", err)
	}

	if len(matched) != 1 {
		t.Fatalf("expected exactly 1 matched plugin, got %d", len(matched))
	}

	if matched[0].Name != "pmatching" {
		t.Errorf("expected matched plugin 'pmatching', got %s", matched[0].Name)
	}
}

func TestIsImageVolumeSupported(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
	}

	// 1. Nil discovery client fails closed: without a way to confirm the cluster
	// supports ImageVolume, mounting one would have the API server reject the whole
	// Deployment, so the capability is assumed absent.
	if isImageVolumeSupported(nil, agent) {
		t.Errorf("expected isImageVolumeSupported(nil, agent) to be false (fail closed)")
	}

	// 2. Annotation override "true" forces imageVolumeSupported to true
	agentWithAnnotation := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:        "test-agent",
			Namespace:   "default",
			Annotations: map[string]string{"kubeagents.x-k8s.io/enable-image-volumes": "true"},
		},
	}
	if !isImageVolumeSupported(nil, agentWithAnnotation) {
		t.Errorf("expected annotation override 'true' to return true")
	}
}

func TestUpdatePluginStatuses_ImageVolumeUnsupported(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "target-agent", Namespace: "test-ns"},
	}
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "testplugin", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: "gcr.io/plugin:v1"},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(plugin).
		WithStatusSubresource(plugin).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	ctx := context.Background()
	r.updatePluginStatuses(ctx, agent, []*agentv1alpha1.AgentPlugin{plugin}, false /* imageVolumeSupported */)

	var updatedPlugin agentv1alpha1.AgentPlugin
	if err := cl.Get(ctx, types.NamespacedName{Name: plugin.Name, Namespace: plugin.Namespace}, &updatedPlugin); err != nil {
		t.Fatalf("failed to fetch updated plugin: %v", err)
	}

	if updatedPlugin.Status.Phase != "Ready" {
		t.Errorf("expected Status.Phase 'Ready', got '%s'", updatedPlugin.Status.Phase)
	}

	cond := meta.FindStatusCondition(updatedPlugin.Status.Conditions, "Ready")
	if cond == nil {
		t.Fatalf("expected 'Ready' status condition to be set")
	}
	if cond.Status != metav1.ConditionTrue {
		t.Errorf("expected condition Status True, got %s", cond.Status)
	}
	if cond.Reason != "Applied" {
		t.Errorf("expected condition Reason 'Applied', got '%s'", cond.Reason)
	}
	if !strings.Contains(cond.Message, "init container") {
		t.Errorf("expected condition Message to mention init container staging, got '%s'", cond.Message)
	}
}

func TestUpdatePluginStatuses_TargetAgentsDeduplication(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "target-agent", Namespace: "test-ns"},
	}
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "testplugin", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: "gcr.io/plugin:v1"},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(plugin).
		WithStatusSubresource(plugin).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	ctx := context.Background()
	// Call updatePluginStatuses multiple times for the same agent
	r.updatePluginStatuses(ctx, agent, []*agentv1alpha1.AgentPlugin{plugin}, true)
	r.updatePluginStatuses(ctx, agent, []*agentv1alpha1.AgentPlugin{plugin}, true)

	var updatedPlugin agentv1alpha1.AgentPlugin
	if err := cl.Get(ctx, types.NamespacedName{Name: plugin.Name, Namespace: plugin.Namespace}, &updatedPlugin); err != nil {
		t.Fatalf("failed to fetch updated plugin: %v", err)
	}

	targetCount := 0
	for _, target := range updatedPlugin.Status.TargetAgents {
		if target == "target-agent" {
			targetCount++
		}
	}
	if targetCount != 1 {
		t.Errorf("expected 'target-agent' in Status.TargetAgents exactly once, got %d times (%v)", targetCount, updatedPlugin.Status.TargetAgents)
	}
}

func TestUpdatePluginStatuses_DuplicatePluginName(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "target-agent", Namespace: "test-ns"},
	}
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "sessionstore", Namespace: "test-ns"}, // Normalizes onto built-in "session_store"
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: "gcr.io/plugin:v1"},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(plugin).
		WithStatusSubresource(plugin).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	ctx := context.Background()
	r.updatePluginStatuses(ctx, agent, []*agentv1alpha1.AgentPlugin{plugin}, true)

	var updatedPlugin agentv1alpha1.AgentPlugin
	if err := cl.Get(ctx, types.NamespacedName{Name: plugin.Name, Namespace: plugin.Namespace}, &updatedPlugin); err != nil {
		t.Fatalf("failed to fetch updated plugin: %v", err)
	}

	if updatedPlugin.Status.Phase != "Degraded" {
		t.Errorf("expected Status.Phase 'Degraded', got '%s'", updatedPlugin.Status.Phase)
	}

	cond := meta.FindStatusCondition(updatedPlugin.Status.Conditions, "Ready")
	if cond == nil {
		t.Fatalf("expected 'Ready' status condition to be set")
	}
	if cond.Status != metav1.ConditionFalse {
		t.Errorf("expected condition Status False, got %s", cond.Status)
	}
	if cond.Reason != "DuplicatePluginName" {
		t.Errorf("expected condition Reason 'DuplicatePluginName', got '%s'", cond.Reason)
	}
}

type fakeVersionDiscovery struct {
	discovery.DiscoveryInterface
	ver    *version.Info
	groups *metav1.APIGroupList
}

func (f *fakeVersionDiscovery) ServerVersion() (*version.Info, error) {
	return f.ver, nil
}

func (f *fakeVersionDiscovery) ServerGroups() (*metav1.APIGroupList, error) {
	if f.groups != nil {
		return f.groups, nil
	}
	return &metav1.APIGroupList{}, nil
}

func TestIsImageVolumeSupported_DiscoveryVersion(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
	}

	// 1. Server version < 1.35 returns false
	dcOld := &fakeVersionDiscovery{ver: &version.Info{Major: "1", Minor: "31"}}
	if isImageVolumeSupported(dcOld, agent) {
		t.Errorf("expected isImageVolumeSupported to return false for K8s 1.31")
	}

	dc34 := &fakeVersionDiscovery{ver: &version.Info{Major: "1", Minor: "34+"}}
	if isImageVolumeSupported(dc34, agent) {
		t.Errorf("expected isImageVolumeSupported to return false for K8s 1.34+")
	}

	// 2. Server version >= 1.35 returns true
	dc35 := &fakeVersionDiscovery{ver: &version.Info{Major: "1", Minor: "35"}}
	if !isImageVolumeSupported(dc35, agent) {
		t.Errorf("expected isImageVolumeSupported to return true for K8s 1.35")
	}

	dcNew := &fakeVersionDiscovery{ver: &version.Info{Major: "1", Minor: "36+"}}
	if !isImageVolumeSupported(dcNew, agent) {
		t.Errorf("expected isImageVolumeSupported to return true for K8s 1.36+")
	}

	// 3. Annotation override "true" on K8s < 1.35 returns true
	agentEnableAnnot := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:        "test-agent",
			Namespace:   "default",
			Annotations: map[string]string{"kubeagents.x-k8s.io/enable-image-volumes": "true"},
		},
	}
	if !isImageVolumeSupported(dcOld, agentEnableAnnot) {
		t.Errorf("expected annotation override 'true' to force isImageVolumeSupported to true even on K8s 1.31")
	}

	// 4. Annotation override "false" on K8s >= 1.35 returns false
	agentDisableAnnot := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:        "test-agent",
			Namespace:   "default",
			Annotations: map[string]string{"kubeagents.x-k8s.io/enable-image-volumes": "false"},
		},
	}
	if isImageVolumeSupported(dc35, agentDisableAnnot) {
		t.Errorf("expected annotation override 'false' to force isImageVolumeSupported to false even on K8s 1.35")
	}

	// 5. Server version >= 1.35 on GKE Standard returns true (natively supported)
	dcGKEStandard := &fakeVersionDiscovery{ver: &version.Info{Major: "1", Minor: "35", GitVersion: "v1.35.7-gke.1027000"}}
	if !isImageVolumeSupported(dcGKEStandard, agent) {
		t.Errorf("expected isImageVolumeSupported to return true on GKE Standard >= 1.35")
	}

	// 6. GKE Autopilot returns false (falls back to initContainer staging)
	dcGKEAutopilot := &fakeVersionDiscovery{
		ver: &version.Info{Major: "1", Minor: "35", GitVersion: "v1.35.7-gke.1027000"},
		groups: &metav1.APIGroupList{
			Groups: []metav1.APIGroup{{Name: "auto.gke.io"}},
		},
	}
	if isImageVolumeSupported(dcGKEAutopilot, agent) {
		t.Errorf("expected isImageVolumeSupported to return false on GKE Autopilot (falls back to initContainer staging)")
	}

	// 7. Annotation override "true" on GKE Autopilot forces isImageVolumeSupported to true
	if !isImageVolumeSupported(dcGKEAutopilot, agentEnableAnnot) {
		t.Errorf("expected annotation override 'true' to force isImageVolumeSupported to true on GKE Autopilot")
	}
}

func TestResolveAgentPlugins_MissingCRDGracefulHandling(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "target-agent", Namespace: "test-ns"},
	}

	// Intercept List call for AgentPluginList and return NoKindMatchError (simulating missing CRD)
	interceptedClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithInterceptorFuncs(interceptor.Funcs{
			List: func(ctx context.Context, client client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
				if _, ok := list.(*agentv1alpha1.AgentPluginList); ok {
					return &meta.NoKindMatchError{GroupKind: schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "AgentPlugin"}}
				}
				return client.List(ctx, list, opts...)
			},
		}).
		Build()

	r := &PlatformAgentReconciler{
		Client: interceptedClient,
		Scheme: scheme,
	}

	ctx := context.Background()
	plugins, err := r.resolveAgentPlugins(ctx, agent)
	if err != nil {
		t.Fatalf("expected no error when AgentPlugin CRD is not installed on cluster, got: %v", err)
	}

	if len(plugins) != 0 {
		t.Errorf("expected 0 plugins when CRD is missing, got %d", len(plugins))
	}
}

func TestIsCRDNotInstalledError(t *testing.T) {
	if isCRDNotInstalledError(nil) {
		t.Errorf("expected false for nil error")
	}
	if !isCRDNotInstalledError(&meta.NoKindMatchError{GroupKind: schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "AgentPlugin"}}) {
		t.Errorf("expected true for NoKindMatchError")
	}
	if !isCRDNotInstalledError(errors.NewNotFound(schema.GroupResource{Group: "kubeagents.x-k8s.io", Resource: "agentplugins"}, "")) {
		t.Errorf("expected true for NotFound error")
	}
	if !isCRDNotInstalledError(fmt.Errorf("no matches for kind \"AgentPlugin\" in version \"kubeagents.x-k8s.io/v1alpha1\"")) {
		t.Errorf("expected true for 'no matches for kind' error string")
	}
}

// erroringDiscovery simulates an API server that cannot be reached for version discovery.
type erroringDiscovery struct {
	discovery.DiscoveryInterface
}

func (e *erroringDiscovery) ServerVersion() (*version.Info, error) {
	return nil, fmt.Errorf("connection refused")
}

func TestIsImageVolumeSupported_FailsClosed(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
	}

	// A discovery error must not be read as "supported": attaching an ImageVolume the
	// cluster cannot honour makes the API server reject the whole Deployment.
	if isImageVolumeSupported(&erroringDiscovery{}, agent) {
		t.Errorf("expected false when ServerVersion() returns an error")
	}

	// An unparseable version is equally inconclusive.
	garbled := &fakeVersionDiscovery{ver: &version.Info{Major: "v-one", Minor: "thirty"}}
	if isImageVolumeSupported(garbled, agent) {
		t.Errorf("expected false when the server version cannot be parsed")
	}

	// The annotation is an explicit override and still wins over a failed probe.
	agentOverride := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:        "test-agent",
			Namespace:   "default",
			Annotations: map[string]string{"kubeagents.x-k8s.io/enable-image-volumes": "true"},
		},
	}
	if !isImageVolumeSupported(&erroringDiscovery{}, agentOverride) {
		t.Errorf("expected annotation override 'true' to win over a failed discovery probe")
	}
}

// countingDiscovery records how many times ServerVersion() is called.
type countingDiscovery struct {
	discovery.DiscoveryInterface
	calls int
}

func (c *countingDiscovery) ServerVersion() (*version.Info, error) {
	c.calls++
	return &version.Info{Major: "1", Minor: "35"}, nil
}

func TestImageVolumeSupported_CachesDiscovery(t *testing.T) {
	dc := &countingDiscovery{}
	r := &PlatformAgentReconciler{DiscoveryClient: dc}
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
	}

	for i := 0; i < 5; i++ {
		if !r.imageVolumeSupported(agent) {
			t.Fatalf("expected image volumes to be supported on 1.35")
		}
	}
	if dc.calls != 1 {
		t.Errorf("expected ServerVersion() to be called once and cached, got %d calls", dc.calls)
	}

	// Annotation overrides are still evaluated per call, not frozen by the cache.
	agentOff := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:        "test-agent",
			Namespace:   "default",
			Annotations: map[string]string{"kubeagents.x-k8s.io/enable-image-volumes": "false"},
		},
	}
	if r.imageVolumeSupported(agentOff) {
		t.Errorf("expected annotation 'false' to disable image volumes despite the cached cluster capability")
	}
}

func TestUpdatePluginStatuses_InvalidPluginName(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "target-agent", Namespace: "test-ns"},
	}
	// Hyphens are rejected by the CRD, but an object stored before that rule existed
	// must degrade with a clear reason rather than produce an unmountable pod spec.
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "legacy-hyphen-name", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: "gcr.io/plugin:v1"},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(plugin).
		WithStatusSubresource(plugin).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()
	r.updatePluginStatuses(ctx, agent, []*agentv1alpha1.AgentPlugin{plugin}, true)

	var updated agentv1alpha1.AgentPlugin
	if err := cl.Get(ctx, types.NamespacedName{Name: plugin.Name, Namespace: plugin.Namespace}, &updated); err != nil {
		t.Fatalf("failed to fetch updated plugin: %v", err)
	}
	if updated.Status.Phase != "Degraded" {
		t.Errorf("expected Phase 'Degraded', got '%s'", updated.Status.Phase)
	}
	cond := meta.FindStatusCondition(updated.Status.Conditions, "Ready")
	if cond == nil || cond.Reason != "InvalidPluginName" {
		t.Errorf("expected Reason 'InvalidPluginName', got %+v", cond)
	}
}

func TestUpdatePluginStatuses_RepeatedNameIsDegraded(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "target-agent", Namespace: "test-ns"},
	}
	// Defensive guard: object names are unique per namespace, so the resolver cannot
	// normally hand the same identifier over twice. This asserts the guard is keyed
	// correctly if it ever does — it previously wrote seenNames under the raw name and
	// read it under the normalized one, so the second entry was silently accepted.
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "stockout", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: "gcr.io/a:v1"},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(plugin).
		WithStatusSubresource(plugin).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()

	first := plugin.DeepCopy()
	second := plugin.DeepCopy()
	r.updatePluginStatuses(ctx, agent, []*agentv1alpha1.AgentPlugin{first, second}, true)

	if first.Status.Phase != "Ready" {
		t.Errorf("expected first occurrence Phase 'Ready', got '%s'", first.Status.Phase)
	}
	if second.Status.Phase != "Degraded" {
		t.Errorf("expected repeated occurrence Phase 'Degraded', got '%s'", second.Status.Phase)
	}
	cond := meta.FindStatusCondition(second.Status.Conditions, "Ready")
	if cond == nil || cond.Reason != "DuplicatePluginName" {
		t.Errorf("expected repeated occurrence Reason 'DuplicatePluginName', got %+v", cond)
	}
}

func TestNormalizePluginName_CollidesWithBuiltIn(t *testing.T) {
	// The reachable collision case: a CRD-valid name that normalizes onto a built-in
	// whose own name carries underscores.
	if !IsBuiltInPlugin("sessionstore") {
		t.Errorf("expected 'sessionstore' to be recognised as the built-in 'session_store'")
	}
	if !IsBuiltInPlugin("toolcallaudit") {
		t.Errorf("expected 'toolcallaudit' to be recognised as the built-in 'tool_call_audit'")
	}
	if IsBuiltInPlugin("stockouthandler") {
		t.Errorf("did not expect 'stockouthandler' to be treated as a built-in")
	}
}

func TestIsValidPluginName(t *testing.T) {
	valid := []string{"a", "stockout", "stockouthandler", "e2eplugin", "plugin9"}
	for _, n := range valid {
		if !isValidPluginName(n) {
			t.Errorf("expected %q to be a valid plugin name", n)
		}
	}
	invalid := []string{
		"",                      // empty
		"stockout-handler",      // hyphen: not importable as a module
		"stockout_handler",      // underscore: not a legal object name
		"my.plugin",             // dot: not a legal volume-name label
		"Stockout",              // uppercase
		"9lives",                // leading digit
		strings.Repeat("a", 57), // exceeds the 56-char volume-name budget
	}
	for _, n := range invalid {
		if isValidPluginName(n) {
			t.Errorf("expected %q to be rejected as a plugin name", n)
		}
	}
}

func TestUpdatePluginStatuses_NoWriteWhenUnchanged(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "target-agent", Namespace: "test-ns"},
	}
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "stableplugin", Namespace: "test-ns", Generation: 3},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: "gcr.io/plugin:v1"},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(plugin).
		WithStatusSubresource(plugin).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()

	r.updatePluginStatuses(ctx, agent, []*agentv1alpha1.AgentPlugin{plugin}, true)
	var afterFirst agentv1alpha1.AgentPlugin
	if err := cl.Get(ctx, types.NamespacedName{Name: plugin.Name, Namespace: plugin.Namespace}, &afterFirst); err != nil {
		t.Fatalf("get after first: %v", err)
	}
	if afterFirst.Status.ObservedGeneration != 3 {
		t.Errorf("expected ObservedGeneration 3, got %d", afterFirst.Status.ObservedGeneration)
	}
	if afterFirst.Status.LastUpdated == nil {
		t.Errorf("expected LastUpdated to be stamped on the first write")
	}
	rvFirst := afterFirst.ResourceVersion

	// A second pass reaching the same conclusion must not write. A write here would
	// re-enqueue the agent through the AgentPlugin watch on every reconcile.
	fresh := afterFirst.DeepCopy()
	r.updatePluginStatuses(ctx, agent, []*agentv1alpha1.AgentPlugin{fresh}, true)

	var afterSecond agentv1alpha1.AgentPlugin
	if err := cl.Get(ctx, types.NamespacedName{Name: plugin.Name, Namespace: plugin.Namespace}, &afterSecond); err != nil {
		t.Fatalf("get after second: %v", err)
	}
	if afterSecond.ResourceVersion != rvFirst {
		t.Errorf("expected no second status write (resourceVersion %s), got %s", rvFirst, afterSecond.ResourceVersion)
	}

	// A genuine change (message updated to reflect init container staging) must still be written.
	changed := afterSecond.DeepCopy()
	r.updatePluginStatuses(ctx, agent, []*agentv1alpha1.AgentPlugin{changed}, false /* imageVolumeSupported */)
	var afterThird agentv1alpha1.AgentPlugin
	if err := cl.Get(ctx, types.NamespacedName{Name: plugin.Name, Namespace: plugin.Namespace}, &afterThird); err != nil {
		t.Fatalf("get after third: %v", err)
	}
	if afterThird.ResourceVersion == rvFirst {
		t.Errorf("expected a status write when the condition changes, resourceVersion unchanged at %s", rvFirst)
	}
	if afterThird.Status.Phase != "Ready" {
		t.Errorf("expected Phase 'Ready', got '%s'", afterThird.Status.Phase)
	}
}

// flakyDiscovery fails the first n ServerVersion calls, then succeeds.
type flakyDiscovery struct {
	discovery.DiscoveryInterface
	failures int
	calls    int
}

func (f *flakyDiscovery) ServerVersion() (*version.Info, error) {
	f.calls++
	if f.calls <= f.failures {
		return nil, fmt.Errorf("apiserver unreachable")
	}
	return &version.Info{Major: "1", Minor: "35"}, nil
}

func TestImageVolumeSupported_TransientFailureIsNotCached(t *testing.T) {
	// A discovery error means "unknown", and unknown fails closed for that pass. It must
	// not be remembered: caching it would pin every plugin to Degraded until the operator
	// restarts, just because the API server blinked during the first reconcile.
	dc := &flakyDiscovery{failures: 2}
	r := &PlatformAgentReconciler{DiscoveryClient: dc}
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "default"},
	}

	if r.imageVolumeSupported(agent) {
		t.Errorf("expected false while discovery is failing (fail closed)")
	}
	if r.imageVolumeSupported(agent) {
		t.Errorf("expected false on the second failing probe")
	}
	if !r.imageVolumeSupported(agent) {
		t.Errorf("expected true once discovery recovers; the failed probe must not be cached")
	}
	if dc.calls != 3 {
		t.Errorf("expected the probe to be retried until authoritative, got %d calls", dc.calls)
	}

	// Once authoritative, the answer is cached and discovery is not called again.
	if !r.imageVolumeSupported(agent) {
		t.Errorf("expected cached true")
	}
	if dc.calls != 3 {
		t.Errorf("expected no further discovery calls after an authoritative answer, got %d", dc.calls)
	}
}

// newPluginPod builds an agent gateway pod whose platform-agent container is stuck
// pulling image, mirroring how an unpullable OCI image volume surfaces on a real cluster.
func newPluginPod(agentName, namespace, image, reason string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      agentName + "-gateway-abc123",
			Namespace: namespace,
			Labels:    map[string]string{"app": agentName + "-gateway"},
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodPending,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name: "platform-agent",
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{
					Reason:  reason,
					Message: fmt.Sprintf("Back-off pulling image %q: ErrImagePull", image),
				}},
			}},
		},
	}
}

func TestUpdatePluginStatuses_ImagePullFailureIsReported(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "target-agent", Namespace: "test-ns"},
	}
	badImage := "gcr.io/proj/missing:v1"
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "badplugin", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: badImage},
	}
	// A second plugin whose image pulls fine must not be blamed for the first one's failure.
	healthy := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "goodplugin", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: "gcr.io/proj/fine:v1"},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(plugin, healthy, newPluginPod("target-agent", "test-ns", badImage, "ImagePullBackOff")).
		WithStatusSubresource(plugin, healthy).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()
	r.updatePluginStatuses(ctx, agent, []*agentv1alpha1.AgentPlugin{plugin, healthy}, true)

	var bad, good agentv1alpha1.AgentPlugin
	if err := cl.Get(ctx, types.NamespacedName{Name: "badplugin", Namespace: "test-ns"}, &bad); err != nil {
		t.Fatalf("get badplugin: %v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: "goodplugin", Namespace: "test-ns"}, &good); err != nil {
		t.Fatalf("get goodplugin: %v", err)
	}

	if bad.Status.Phase != "Degraded" {
		t.Errorf("expected failing plugin Phase 'Degraded', got %q", bad.Status.Phase)
	}
	cond := meta.FindStatusCondition(bad.Status.Conditions, "Ready")
	if cond == nil || cond.Reason != "ImagePullFailed" {
		t.Fatalf("expected Reason 'ImagePullFailed', got %+v", cond)
	}
	if !strings.Contains(cond.Message, badImage) {
		t.Errorf("expected the failing image in the message, got %q", cond.Message)
	}
	if good.Status.Phase != "Ready" {
		t.Errorf("expected unaffected plugin to stay Ready, got %q", good.Status.Phase)
	}
}

func TestUpdatePluginStatuses_StagingFailureIsReported(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "target-agent", Namespace: "test-ns"},
	}
	failingPlugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "failstage", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: "gcr.io/proj/plugin:v1"},
	}
	healthyPlugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "okstage", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: "gcr.io/proj/plugin:v2"},
	}

	stagingContainerName := buildPluginStagingContainerName("failstage")
	okContainerName := buildPluginStagingContainerName("okstage")

	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "target-agent-gateway-xyz",
			Namespace: "test-ns",
			Labels:    map[string]string{"app": "target-agent-gateway"},
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodPending,
			InitContainerStatuses: []corev1.ContainerStatus{
				{
					Name: stagingContainerName,
					State: corev1.ContainerState{
						Terminated: &corev1.ContainerStateTerminated{
							ExitCode: 1,
							Reason:   "Error",
						},
					},
				},
				{
					Name: okContainerName,
					State: corev1.ContainerState{
						Running: &corev1.ContainerStateRunning{},
					},
				},
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(failingPlugin, healthyPlugin, pod).
		WithStatusSubresource(failingPlugin, healthyPlugin).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()
	r.updatePluginStatuses(ctx, agent, []*agentv1alpha1.AgentPlugin{failingPlugin, healthyPlugin}, false)

	var bad, good agentv1alpha1.AgentPlugin
	if err := cl.Get(ctx, types.NamespacedName{Name: "failstage", Namespace: "test-ns"}, &bad); err != nil {
		t.Fatalf("get bad plugin: %v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: "okstage", Namespace: "test-ns"}, &good); err != nil {
		t.Fatalf("get good plugin: %v", err)
	}

	if bad.Status.Phase != "Degraded" {
		t.Errorf("expected failing staging plugin Phase 'Degraded', got %q", bad.Status.Phase)
	}
	cond := meta.FindStatusCondition(bad.Status.Conditions, "Ready")
	if cond == nil || cond.Reason != "StagingFailed" {
		t.Fatalf("expected Reason 'StagingFailed', got %+v", cond)
	}
	if !strings.Contains(cond.Message, "staging init container exited with code 1") {
		t.Errorf("expected exit code 1 in condition message, got %q", cond.Message)
	}
	if good.Status.Phase != "Ready" {
		t.Errorf("expected healthy staging plugin Phase 'Ready', got %q", good.Status.Phase)
	}
}

func TestDetectPluginImageFailures_IgnoresUnrelatedPullFailures(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "target-agent", Namespace: "test-ns"},
	}
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "myplugin", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: "gcr.io/proj/plugin:v1"},
	}
	// The agent's own image is failing, not the plugin's. Blaming the plugin would send
	// whoever is debugging in the wrong direction.
	pod := newPluginPod("target-agent", "test-ns", "gcr.io/proj/platform-agent:v9", "ErrImagePull")

	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(plugin, pod).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	failures := r.detectPluginImageFailures(context.Background(), agent, []*agentv1alpha1.AgentPlugin{plugin})
	if len(failures) != 0 {
		t.Errorf("expected no plugin blamed for the agent image failing, got %v", failures)
	}
}

func TestMarkOrphanedPlugins(t *testing.T) {
	scheme := setupScheme()
	orphan := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "orphanplugin", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "typoed-agent", Image: "gcr.io/proj/p:v1"},
	}
	other := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "otherplugin", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "real-agent", Image: "gcr.io/proj/p:v1"},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(orphan, other).
		WithStatusSubresource(orphan, other).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()
	r.markOrphanedPlugins(ctx, "test-ns", "typoed-agent")

	var got, untouched agentv1alpha1.AgentPlugin
	if err := cl.Get(ctx, types.NamespacedName{Name: "orphanplugin", Namespace: "test-ns"}, &got); err != nil {
		t.Fatalf("get orphan: %v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: "otherplugin", Namespace: "test-ns"}, &untouched); err != nil {
		t.Fatalf("get other: %v", err)
	}

	if got.Status.Phase != "Degraded" {
		t.Errorf("expected orphan Phase 'Degraded', got %q", got.Status.Phase)
	}
	cond := meta.FindStatusCondition(got.Status.Conditions, "Ready")
	if cond == nil || cond.Reason != "AgentNotFound" {
		t.Fatalf("expected Reason 'AgentNotFound', got %+v", cond)
	}
	// Plugins targeting a different agent must not be touched by this sweep.
	if untouched.Status.Phase != "" {
		t.Errorf("expected plugin for a different agent to be left alone, got phase %q", untouched.Status.Phase)
	}
}

func TestMarkOrphanedPlugins_IsIdempotent(t *testing.T) {
	scheme := setupScheme()
	orphan := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "orphanplugin", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "typoed-agent", Image: "gcr.io/proj/p:v1"},
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).WithObjects(orphan).WithStatusSubresource(orphan).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()

	r.markOrphanedPlugins(ctx, "test-ns", "typoed-agent")
	var first agentv1alpha1.AgentPlugin
	if err := cl.Get(ctx, types.NamespacedName{Name: "orphanplugin", Namespace: "test-ns"}, &first); err != nil {
		t.Fatalf("get: %v", err)
	}
	rv := first.ResourceVersion

	r.markOrphanedPlugins(ctx, "test-ns", "typoed-agent")
	var second agentv1alpha1.AgentPlugin
	if err := cl.Get(ctx, types.NamespacedName{Name: "orphanplugin", Namespace: "test-ns"}, &second); err != nil {
		t.Fatalf("get: %v", err)
	}
	if second.ResourceVersion != rv {
		t.Errorf("expected no repeat write for an unchanged orphan, resourceVersion %s -> %s", rv, second.ResourceVersion)
	}
}

func TestPluginStatusNeedsRecheck(t *testing.T) {
	ready := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "p1"},
		Status: agentv1alpha1.AgentPluginStatus{Conditions: []metav1.Condition{
			{Type: "Ready", Status: metav1.ConditionTrue, Reason: "Applied"},
		}},
	}
	pullFailed := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "p2"},
		Status: agentv1alpha1.AgentPluginStatus{Conditions: []metav1.Condition{
			{Type: "Ready", Status: metav1.ConditionFalse, Reason: "ImagePullFailed"},
		}},
	}
	terminal := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "p3"},
		Status: agentv1alpha1.AgentPluginStatus{Conditions: []metav1.Condition{
			{Type: "Ready", Status: metav1.ConditionFalse, Reason: "InvalidPluginName"},
		}},
	}

	cases := []struct {
		name       string
		plugins    []*agentv1alpha1.AgentPlugin
		agentReady bool
		want       bool
	}{
		{"no plugins never requeues", nil, false, false},
		{"agent still converging", []*agentv1alpha1.AgentPlugin{ready}, false, true},
		{"settled and ready", []*agentv1alpha1.AgentPlugin{ready}, true, false},
		{"pull failure keeps watching for recovery", []*agentv1alpha1.AgentPlugin{pullFailed}, true, true},
		{"terminal misconfiguration settles", []*agentv1alpha1.AgentPlugin{terminal}, true, false},
	}
	for _, tc := range cases {
		if got := pluginStatusNeedsRecheck(tc.plugins, tc.agentReady); got != tc.want {
			t.Errorf("%s: expected %v, got %v", tc.name, tc.want, got)
		}
	}
}

func TestPluginConfigIssues(t *testing.T) {
	none := &agentv1alpha1.AgentPlugin{
		Spec: agentv1alpha1.AgentPluginSpec{Config: "approvals:\n  cron_mode: approve\n"},
	}
	if issues := pluginConfigIssues(none); len(issues) != 0 {
		t.Errorf("expected no issues for an allowlisted subtree, got %v", issues)
	}

	rejected := &agentv1alpha1.AgentPlugin{
		Spec: agentv1alpha1.AgentPluginSpec{Config: "agent:\n  disabled_toolsets: []\nlogging:\n  level: debug\n"},
	}
	issues := pluginConfigIssues(rejected)
	if len(issues) != 1 || !strings.Contains(issues[0], "agent") || !strings.Contains(issues[0], "logging") {
		t.Errorf("expected both disallowed keys reported, got %v", issues)
	}

	broken := &agentv1alpha1.AgentPlugin{
		Spec: agentv1alpha1.AgentPluginSpec{Config: "approvals: [unclosed\n"},
	}
	if issues := pluginConfigIssues(broken); len(issues) != 1 || !strings.Contains(issues[0], "not valid YAML") {
		t.Errorf("expected a parse failure to be reported, got %v", issues)
	}
}

func TestImageReferencedIn(t *testing.T) {
	const msg = `Back-off pulling image "gcr.io/proj/plugin:v10": ErrImagePull: rpc error`
	cases := []struct {
		image string
		want  bool
		why   string
	}{
		{"gcr.io/proj/plugin:v10", true, "exact reference"},
		{"gcr.io/proj/plugin:v1", false, "v1 must not match inside v10"},
		{"gcr.io/proj/plugin", false, "untagged prefix of a tagged reference"},
		{"proj/plugin:v10", false, "suffix of a longer registry path"},
		{"gcr.io/proj/other:v10", false, "unrelated image"},
		{"", false, "empty image never matches"},
	}
	for _, tc := range cases {
		if got := imageReferencedIn(msg, tc.image); got != tc.want {
			t.Errorf("imageReferencedIn(%q) = %v, want %v (%s)", tc.image, got, tc.want, tc.why)
		}
	}

	// Unquoted references still match, so this does not depend on one message format.
	if !imageReferencedIn("failed to resolve reference gcr.io/proj/plugin:v10", "gcr.io/proj/plugin:v10") {
		t.Errorf("expected an unquoted reference to match")
	}
}

func TestDetectPluginImageFailures_DoesNotBlameSiblingTag(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "target-agent", Namespace: "test-ns"},
	}
	// Two plugins whose tags are prefixes of one another. Only v10 is failing.
	v1 := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "pluginone", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: "gcr.io/proj/p:v1"},
	}
	v10 := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "pluginten", Namespace: "test-ns"},
		Spec:       agentv1alpha1.AgentPluginSpec{AgentRef: "target-agent", Image: "gcr.io/proj/p:v10"},
	}
	pod := newPluginPod("target-agent", "test-ns", "gcr.io/proj/p:v10", "ImagePullBackOff")

	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(v1, v10, pod).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	failures := r.detectPluginImageFailures(context.Background(), agent, []*agentv1alpha1.AgentPlugin{v1, v10})
	if _, blamed := failures["pluginone"]; blamed {
		t.Errorf("plugin using :v1 must not be blamed for :v10 failing, got %v", failures)
	}
	if _, blamed := failures["pluginten"]; !blamed {
		t.Errorf("expected the plugin using :v10 to be blamed, got %v", failures)
	}
}

func TestReconcileGitopsStateConfigMap(t *testing.T) {
	scheme := setupScheme()
	fakeClient := fake.NewClientBuilder().WithScheme(scheme).Build()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	r := &PlatformAgentReconciler{
		Client: fakeClient,
		Scheme: scheme,
	}

	ctx := context.Background()

	// 1. Initial reconcile should create the configmap
	err := r.reconcileGitopsStateConfigMap(ctx, agent)
	if err != nil {
		t.Fatalf("unexpected error during reconcile: %v", err)
	}

	// Verify creation
	var cm corev1.ConfigMap
	cmKey := client.ObjectKey{Name: "test-agent-gitops-state", Namespace: "test-ns"}
	if err := fakeClient.Get(ctx, cmKey, &cm); err != nil {
		t.Fatalf("failed to get created ConfigMap: %v", err)
	}

	// 2. Modify the configmap locally (simulation of agent writing to it)
	if cm.Data == nil {
		cm.Data = make(map[string]string)
	}
	cm.Data["managed_repos"] = `[{"type":"github","url":"some-repo"}]`
	if err := fakeClient.Update(ctx, &cm); err != nil {
		t.Fatalf("failed to update ConfigMap: %v", err)
	}

	// 3. Second reconcile should retain existing data
	err = r.reconcileGitopsStateConfigMap(ctx, agent)
	if err != nil {
		t.Fatalf("unexpected error during second reconcile: %v", err)
	}

	var verifyCM corev1.ConfigMap
	if err := fakeClient.Get(ctx, cmKey, &verifyCM); err != nil {
		t.Fatalf("failed to get ConfigMap after second reconcile: %v", err)
	}
	if verifyCM.Data["managed_repos"] != `[{"type":"github","url":"some-repo"}]` {
		t.Errorf("expected ConfigMap to retain its data, but got %v", verifyCM.Data)
	}

	// 4. Updating CR spec with a new repo should append it to the existing ConfigMap
	agent.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{
		IntegrationSpec: agentv1alpha1.IntegrationSpec{
			GitHub: &agentv1alpha1.GitHubSpec{
				Org:     "test-org",
				GitRepo: "new-repo",
			},
		},
	}
	err = r.reconcileGitopsStateConfigMap(ctx, agent)
	if err != nil {
		t.Fatalf("unexpected error during third reconcile: %v", err)
	}
	if err := fakeClient.Get(ctx, cmKey, &verifyCM); err != nil {
		t.Fatalf("failed to get ConfigMap after third reconcile: %v", err)
	}
	expectedMergedJSON := `[{"type":"github","url":"some-repo"},{"type":"github","url":"https://github.com/test-org/new-repo"}]`
	if verifyCM.Data["managed_repos"] != expectedMergedJSON {
		t.Errorf("expected ConfigMap to contain merged repos, but got %v", verifyCM.Data["managed_repos"])
	}

	// 5. Unparseable managed_repos in ConfigMap should return an error and preserve existing data without overwriting
	verifyCM.Data["managed_repos"] = `[invalid-json-text`
	if err := fakeClient.Update(ctx, &verifyCM); err != nil {
		t.Fatalf("failed to update ConfigMap with unparseable data: %v", err)
	}

	err = r.reconcileGitopsStateConfigMap(ctx, agent)
	if err == nil {
		t.Errorf("expected error when managed_repos contains unparseable JSON, got nil")
	}

	var unparseableVerifyCM corev1.ConfigMap
	if err := fakeClient.Get(ctx, cmKey, &unparseableVerifyCM); err != nil {
		t.Fatalf("failed to get ConfigMap after unparseable reconcile: %v", err)
	}
	if unparseableVerifyCM.Data["managed_repos"] != `[invalid-json-text` {
		t.Errorf("expected ConfigMap to preserve unparseable data, but got %v", unparseableVerifyCM.Data["managed_repos"])
	}
}

func TestReconcileNetworkPolicy_APIReader(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}

	k8sEndpoints := &corev1.Endpoints{
		ObjectMeta: metav1.ObjectMeta{Name: "kubernetes", Namespace: "default"},
		Subsets: []corev1.EndpointSubset{
			{
				Addresses: []corev1.EndpointAddress{
					{IP: "172.16.0.5"},
					{IP: "172.16.0.6"},
				},
			},
		},
	}

	k8sSvc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: "kubernetes", Namespace: "default"},
		Spec:       corev1.ServiceSpec{ClusterIP: "10.96.0.1"},
	}

	// APIReader has the Endpoints object, while Client does not (simulating non-cached live read)
	apiReader := fake.NewClientBuilder().WithScheme(scheme).WithObjects(k8sEndpoints).Build()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, k8sSvc).WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()

	r := &PlatformAgentReconciler{
		Client:    cl,
		APIReader: apiReader,
		Scheme:    scheme,
	}

	ctx := context.Background()
	if err := r.reconcileNetworkPolicy(ctx, agent, r.resolveNetpolProfile(ctx, agent), "", false); err != nil {
		t.Fatalf("reconcileNetworkPolicy failed: %v", err)
	}

	netpol := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: "test-ns", Name: "test-agent-gateway-netpol"}, netpol); err != nil {
		t.Fatalf("failed to get generated NetworkPolicy: %v", err)
	}

	rule := findAPIServerEgressRule(netpol)
	if rule == nil {
		t.Fatalf("API server egress rule (port 6443) not found in netpol")
	}

	var gotCIDRs []string
	for _, peer := range rule.To {
		if peer.IPBlock != nil {
			gotCIDRs = append(gotCIDRs, peer.IPBlock.CIDR)
		}
	}

	wantCIDRs := []string{"10.96.0.1/32", "172.16.0.5/32", "172.16.0.6/32"}
	if !reflect.DeepEqual(gotCIDRs, wantCIDRs) {
		t.Errorf("expected API server egress CIDRs %v, got %v", wantCIDRs, gotCIDRs)
	}
}

func TestCleanupAgentRBAC_ReconcilePreservesActiveRBACAndDeletesLegacy(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
	minimalRoleName := "kubeagents:minimal:test-ns:test-agent"
	minimalBindingName := "kubeagents:minimal:test-ns:test-agent"
	localBindingName := "kubeagents:local:test-ns:test-agent"
	leaderBindingName := "kubeagents:leader:test-ns:test-agent"
	legacyRoleName := "kubeagents:explorer:test-ns:test-agent"
	legacyBindingName := "kubeagents-legacy-binding"

	activeMinimalRole := &rbacv1.ClusterRole{
		ObjectMeta: metav1.ObjectMeta{
			Name: minimalRoleName,
			Labels: map[string]string{
				"app.kubernetes.io/instance": "test-ns-test-agent",
				"app.kubernetes.io/part-of":  "kube-agents",
			},
		},
	}
	activeMinimalBinding := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name: minimalBindingName,
			Labels: map[string]string{
				"kubeagents.x-k8s.io/agent-name":      "test-agent",
				"kubeagents.x-k8s.io/agent-namespace": "test-ns",
			},
		},
	}
	activeLocalBinding := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name:      localBindingName,
			Namespace: "test-ns",
			Labels: map[string]string{
				"kubeagents.x-k8s.io/agent-name":      "test-agent",
				"kubeagents.x-k8s.io/agent-namespace": "test-ns",
			},
		},
		Subjects: []rbacv1.Subject{
			{Kind: "ServiceAccount", Name: "test-agent", Namespace: "test-ns"},
		},
	}
	activeLeaderBinding := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name:      leaderBindingName,
			Namespace: "test-ns",
			Labels: map[string]string{
				"kubeagents.x-k8s.io/agent-name":      "test-agent",
				"kubeagents.x-k8s.io/agent-namespace": "test-ns",
			},
		},
		Subjects: []rbacv1.Subject{
			{Kind: "ServiceAccount", Name: "test-agent", Namespace: "test-ns"},
		},
	}
	legacyClusterRole := &rbacv1.ClusterRole{
		ObjectMeta: metav1.ObjectMeta{
			Name: legacyRoleName,
			Labels: map[string]string{
				"app.kubernetes.io/instance": "test-ns-test-agent",
				"app.kubernetes.io/part-of":  "kube-agents",
			},
		},
	}
	legacyBinding := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name: legacyBindingName,
			Labels: map[string]string{
				"kubeagents.x-k8s.io/agent-name":      "test-agent",
				"kubeagents.x-k8s.io/agent-namespace": "test-ns",
			},
		},
		Subjects: []rbacv1.Subject{
			{Kind: "ServiceAccount", Name: "test-agent", Namespace: "test-ns"},
		},
	}

	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(
		activeMinimalRole, activeMinimalBinding, activeLocalBinding, activeLeaderBinding,
		legacyClusterRole, legacyBinding,
	).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	// Run cleanup in reconcile mode (deleteAll = false)
	if err := r.cleanupAgentRBAC(ctx, agent, false); err != nil {
		t.Fatalf("cleanupAgentRBAC(false) failed: %v", err)
	}

	// Verify active RBAC resources are PRESERVED
	if err := cl.Get(ctx, types.NamespacedName{Name: minimalRoleName}, &rbacv1.ClusterRole{}); err != nil {
		t.Errorf("expected active minimal ClusterRole to be preserved, got %v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: minimalBindingName}, &rbacv1.ClusterRoleBinding{}); err != nil {
		t.Errorf("expected active minimal ClusterRoleBinding to be preserved, got %v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: "test-ns", Name: localBindingName}, &rbacv1.RoleBinding{}); err != nil {
		t.Errorf("expected active local RoleBinding to be preserved, got %v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: "test-ns", Name: leaderBindingName}, &rbacv1.RoleBinding{}); err != nil {
		t.Errorf("expected active leader RoleBinding to be preserved, got %v", err)
	}

	// Verify legacy RBAC resources are DELETED
	if err := cl.Get(ctx, types.NamespacedName{Name: legacyRoleName}, &rbacv1.ClusterRole{}); !errors.IsNotFound(err) {
		t.Errorf("expected legacy ClusterRole to be deleted, got err=%v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: legacyBindingName}, &rbacv1.ClusterRoleBinding{}); !errors.IsNotFound(err) {
		t.Errorf("expected legacy ClusterRoleBinding to be deleted, got err=%v", err)
	}
}

func TestCleanupAgentRBAC_DeletionPurgesAllRBAC(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}

	minimalRoleName := "kubeagents:minimal:test-ns:test-agent"
	minimalBindingName := "kubeagents:minimal:test-ns:test-agent"
	localBindingName := "kubeagents:local:test-ns:test-agent"
	leaderRoleName := "kubeagents:leader:test-ns:test-agent"
	leaderBindingName := "kubeagents:leader:test-ns:test-agent"

	activeMinimalRole := &rbacv1.ClusterRole{
		ObjectMeta: metav1.ObjectMeta{
			Name: minimalRoleName,
			Labels: map[string]string{
				"app.kubernetes.io/instance": "test-ns-test-agent",
				"app.kubernetes.io/part-of":  "kube-agents",
			},
		},
	}
	activeMinimalBinding := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name: minimalBindingName,
			Labels: map[string]string{
				"kubeagents.x-k8s.io/agent-name":      "test-agent",
				"kubeagents.x-k8s.io/agent-namespace": "test-ns",
			},
		},
		Subjects: []rbacv1.Subject{
			{Kind: "ServiceAccount", Name: "test-agent", Namespace: "test-ns"},
		},
	}
	activeLocalBinding := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name:      localBindingName,
			Namespace: "test-ns",
			Labels: map[string]string{
				"kubeagents.x-k8s.io/agent-name":      "test-agent",
				"kubeagents.x-k8s.io/agent-namespace": "test-ns",
			},
		},
		Subjects: []rbacv1.Subject{
			{Kind: "ServiceAccount", Name: "test-agent", Namespace: "test-ns"},
		},
	}
	activeLeaderRole := &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{
			Name:      leaderRoleName,
			Namespace: "test-ns",
		},
	}
	activeLeaderBinding := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name:      leaderBindingName,
			Namespace: "test-ns",
			Labels: map[string]string{
				"kubeagents.x-k8s.io/agent-name":      "test-agent",
				"kubeagents.x-k8s.io/agent-namespace": "test-ns",
			},
		},
		Subjects: []rbacv1.Subject{
			{Kind: "ServiceAccount", Name: "test-agent", Namespace: "test-ns"},
		},
	}

	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(
		activeMinimalRole, activeMinimalBinding, activeLocalBinding, activeLeaderRole, activeLeaderBinding,
	).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	// Run cleanup in deletion mode (deleteAll = true)
	if err := r.cleanupAgentRBAC(ctx, agent, true); err != nil {
		t.Fatalf("cleanupAgentRBAC(true) failed: %v", err)
	}

	// Verify ALL RBAC resources are completely DELETED
	if err := cl.Get(ctx, types.NamespacedName{Name: minimalRoleName}, &rbacv1.ClusterRole{}); !errors.IsNotFound(err) {
		t.Errorf("expected minimal ClusterRole to be deleted during finalization, got err=%v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: minimalBindingName}, &rbacv1.ClusterRoleBinding{}); !errors.IsNotFound(err) {
		t.Errorf("expected minimal ClusterRoleBinding to be deleted during finalization, got err=%v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: "test-ns", Name: localBindingName}, &rbacv1.RoleBinding{}); !errors.IsNotFound(err) {
		t.Errorf("expected local RoleBinding to be deleted during finalization, got err=%v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: "test-ns", Name: leaderRoleName}, &rbacv1.Role{}); !errors.IsNotFound(err) {
		t.Errorf("expected leader Role to be deleted during finalization, got err=%v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: "test-ns", Name: leaderBindingName}, &rbacv1.RoleBinding{}); !errors.IsNotFound(err) {
		t.Errorf("expected leader RoleBinding to be deleted during finalization, got err=%v", err)
	}
}

func TestCleanupAgentRBAC_ErrorPropagation(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}

	// 1. Verify List error propagation in reconcile mode
	listErrInterceptors := interceptor.Funcs{
		List: func(ctx context.Context, client client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
			return errors.NewInternalError(fmt.Errorf("api list failure"))
		},
	}
	clListErr := fake.NewClientBuilder().WithScheme(scheme).WithInterceptorFuncs(listErrInterceptors).Build()
	rListErr := &PlatformAgentReconciler{Client: clListErr, Scheme: scheme}
	if err := rListErr.cleanupAgentRBAC(ctx, agent, false); err == nil {
		t.Fatalf("expected error from cleanupAgentRBAC when List fails, got nil")
	}

	// 2. Verify Delete error propagation in finalization mode (deleteAll = true)
	deleteErrInterceptors := interceptor.Funcs{
		Delete: func(ctx context.Context, client client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
			return errors.NewInternalError(fmt.Errorf("api delete failure"))
		},
	}
	rLeader := &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "kubeagents:leader:test-ns:test-agent",
			Namespace: "test-ns",
		},
	}
	clDeleteErr := fake.NewClientBuilder().WithScheme(scheme).WithObjects(rLeader).WithInterceptorFuncs(deleteErrInterceptors).Build()
	rDeleteErr := &PlatformAgentReconciler{Client: clDeleteErr, Scheme: scheme}
	if err := rDeleteErr.cleanupAgentRBAC(ctx, agent, true); err == nil {
		t.Fatalf("expected error from cleanupAgentRBAC when Delete fails during deleteAll, got nil")
	}
}

func TestReconcileNetworkPolicy_DynamicDiscovery(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
			Annotations: map[string]string{
				AnnotationAPIServerCIDR: "172.16.0.100/32",
			},
		},
	}

	kubeDnsSvc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "kube-dns",
			Namespace: "kube-system",
		},
		Spec: corev1.ServiceSpec{
			ClusterIP: "34.118.224.10",
		},
	}

	k8sEndpoints := &corev1.Endpoints{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "kubernetes",
			Namespace: "default",
		},
		Subsets: []corev1.EndpointSubset{
			{
				Addresses: []corev1.EndpointAddress{
					{IP: "192.168.1.50"},
				},
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, kubeDnsSvc, k8sEndpoints).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{
		Client:                cl,
		APIReader:             cl,
		Scheme:                scheme,
		APIServerIP:           "10.0.0.1",
		APIServerCIDROverride: "198.51.100.0/24,203.0.113.1/32",
	}

	err := r.reconcileNetworkPolicy(ctx, agent, r.resolveNetpolProfile(ctx, agent), "", false)
	if err != nil {
		t.Fatalf("reconcileNetworkPolicy failed: %v", err)
	}

	netpol := &networkingv1.NetworkPolicy{}
	err = cl.Get(ctx, types.NamespacedName{Name: "test-agent-gateway-netpol", Namespace: "test-ns"}, netpol)
	if err != nil {
		t.Fatalf("failed to get reconciled NetworkPolicy: %v", err)
	}

	// Verify DNS egress rule has dynamic 34.118.224.10/32
	dnsRule := findDNSEgressRule(netpol)
	if dnsRule == nil {
		t.Fatalf("DNS egress rule (port 53) not found in netpol")
	}
	foundDNS := false
	for _, peer := range dnsRule.To {
		if peer.IPBlock != nil && peer.IPBlock.CIDR == "34.118.224.10/32" {
			foundDNS = true
			break
		}
	}
	if !foundDNS {
		t.Errorf("expected DNS egress rule to contain dynamic clusterIP 34.118.224.10/32")
	}

	// Verify API server egress rule contains all targets:
	// 10.0.0.1/32 (APIServerIP), 192.168.1.50/32 (Endpoints), 172.16.0.100/32 (Annotation), 198.51.100.0/24, 203.0.113.1/32 (APIServerCIDROverride)
	expectedAPICIDRs := map[string]bool{
		"10.0.0.1/32":     false,
		"192.168.1.50/32": false,
		"172.16.0.100/32": false,
		"198.51.100.0/24": false,
		"203.0.113.1/32":  false,
	}

	foundAPIRule := false
	for _, egressRule := range netpol.Spec.Egress {
		// API rule has port 443 & 6443
		for _, port := range egressRule.Ports {
			if port.Port != nil && port.Port.IntVal == 6443 {
				foundAPIRule = true
				for _, peer := range egressRule.To {
					if peer.IPBlock != nil {
						if _, ok := expectedAPICIDRs[peer.IPBlock.CIDR]; ok {
							expectedAPICIDRs[peer.IPBlock.CIDR] = true
						}
					}
				}
				break
			}
		}
	}

	if !foundAPIRule {
		t.Fatalf("expected to find API server egress rule in NetworkPolicy")
	}

	for cidr, found := range expectedAPICIDRs {
		if !found {
			t.Errorf("expected API server egress rule to contain CIDR %s", cidr)
		}
	}
}

func TestReconcileNetworkPolicy_CustomEgressCIDRsAnnotation(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
			Annotations: map[string]string{
				AnnotationCustomEgressCIDRs: "172.16.0.0/12, 10.50.0.0/16",
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{
		Client:      cl,
		APIReader:   cl,
		Scheme:      scheme,
		APIServerIP: "10.96.0.1",
	}

	err := r.reconcileNetworkPolicy(ctx, agent, r.resolveNetpolProfile(ctx, agent), "", false)
	if err != nil {
		t.Fatalf("reconcileNetworkPolicy failed: %v", err)
	}

	netpol := &networkingv1.NetworkPolicy{}
	err = cl.Get(ctx, types.NamespacedName{Name: "test-agent-gateway-netpol", Namespace: "test-ns"}, netpol)
	if err != nil {
		t.Fatalf("failed to get reconciled NetworkPolicy: %v", err)
	}

	expectedCIDRs := map[string]bool{
		"172.16.0.0/12": false,
		"10.50.0.0/16":  false,
		"10.96.0.1/32":  false,
	}

	for _, egressRule := range netpol.Spec.Egress {
		for _, port := range egressRule.Ports {
			if port.Port != nil && port.Port.IntVal == 6443 {
				for _, peer := range egressRule.To {
					if peer.IPBlock != nil {
						if _, ok := expectedCIDRs[peer.IPBlock.CIDR]; ok {
							expectedCIDRs[peer.IPBlock.CIDR] = true
						}
					}
				}
			}
		}
	}

	for cidr, found := range expectedCIDRs {
		if !found {
			t.Errorf("expected API server egress rule to contain custom CIDR %s", cidr)
		}
	}
}

func TestReconcileNetworkPolicy_RejectOverlyBroadCIDR(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
			Annotations: map[string]string{
				AnnotationCustomEgressCIDRs: "0.0.0.0/0, 10.0.0.0/8, ::/0, 172.16.0.0/12",
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{
		Client:      cl,
		APIReader:   cl,
		Scheme:      scheme,
		APIServerIP: "10.96.0.1",
	}

	err := r.reconcileNetworkPolicy(ctx, agent, r.resolveNetpolProfile(ctx, agent), "", false)
	if err != nil {
		t.Fatalf("reconcileNetworkPolicy failed: %v", err)
	}

	netpol := &networkingv1.NetworkPolicy{}
	err = cl.Get(ctx, types.NamespacedName{Name: "test-agent-gateway-netpol", Namespace: "test-ns"}, netpol)
	if err != nil {
		t.Fatalf("failed to get reconciled NetworkPolicy: %v", err)
	}

	for _, egressRule := range netpol.Spec.Egress {
		for _, port := range egressRule.Ports {
			if port.Port != nil && port.Port.IntVal == 6443 {
				for _, peer := range egressRule.To {
					if peer.IPBlock != nil {
						if peer.IPBlock.CIDR == "0.0.0.0/0" || peer.IPBlock.CIDR == "10.0.0.0/8" || peer.IPBlock.CIDR == "::/0" {
							t.Errorf("expected overly broad CIDR %s to be rejected from API server egress rule", peer.IPBlock.CIDR)
						}
					}
				}
			}
		}
	}
}

func TestReconcileNetworkPolicy_FQDNNetworkPolicyReconciliation(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
			Annotations: map[string]string{
				AnnotationEnableFQDNNetworkPolicy: "true",
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{
		Client:      cl,
		APIReader:   cl,
		Scheme:      scheme,
		APIServerIP: "10.96.0.1",
	}

	err := r.reconcileNetworkPolicy(ctx, agent, r.resolveNetpolProfile(ctx, agent), "", false)
	if err != nil {
		t.Fatalf("reconcileNetworkPolicy failed: %v", err)
	}

	// 1. Verify standard NetworkPolicy has external HTTPS omitted
	netpol := &networkingv1.NetworkPolicy{}
	err = cl.Get(ctx, types.NamespacedName{Name: "test-agent-gateway-netpol", Namespace: "test-ns"}, netpol)
	if err != nil {
		t.Fatalf("failed to get reconciled NetworkPolicy: %v", err)
	}
	for _, egress := range netpol.Spec.Egress {
		for _, peer := range egress.To {
			if peer.IPBlock != nil && peer.IPBlock.CIDR == "0.0.0.0/0" {
				t.Errorf("expected blanket 0.0.0.0/0 to be omitted in NetworkPolicy")
			}
		}
	}

	// 2. Verify companion FQDNNetworkPolicy was created
	fqdnNetpol := &unstructured.Unstructured{}
	fqdnNetpol.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   "networking.gke.io",
		Version: "v1alpha1",
		Kind:    "FQDNNetworkPolicy",
	})
	err = cl.Get(ctx, types.NamespacedName{Name: "test-agent-fqdn-netpol", Namespace: "test-ns"}, fqdnNetpol)
	if err != nil {
		t.Fatalf("failed to get reconciled FQDNNetworkPolicy: %v", err)
	}

	spec, ok := fqdnNetpol.Object["spec"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected spec map in FQDNNetworkPolicy, got %T", fqdnNetpol.Object["spec"])
	}
	egressList, ok := spec["egress"].([]interface{})
	if !ok || len(egressList) == 0 {
		t.Fatalf("expected non-empty egress list in FQDNNetworkPolicy spec")
	}
	firstRule := egressList[0].(map[string]interface{})
	ports, ok := firstRule["ports"].([]interface{})
	if !ok || len(ports) == 0 {
		t.Fatalf("expected ports list in FQDNNetworkPolicy egress rule, got %v", firstRule["ports"])
	}
	portObj := ports[0].(map[string]interface{})
	if portObj["port"] != int64(443) || portObj["protocol"] != "TCP" {
		t.Errorf("expected FQDNNetworkPolicy port to be TCP/443, got %v", portObj)
	}

	matches, ok := firstRule["matches"].([]interface{})
	if !ok || len(matches) == 0 {
		t.Fatalf("expected non-empty matches list in FQDNNetworkPolicy")
	}
	patternSet := make(map[string]bool)
	for _, m := range matches {
		if mMap, isMap := m.(map[string]interface{}); isMap {
			if p, isStr := mMap["pattern"].(string); isStr {
				patternSet[p] = true
			}
		}
	}

	// Verify required baseline and chat patterns are present
	for _, required := range []string{"googleapis.com", "*.googleapis.com", "github.com", "*.github.com", "pkg.dev", "*.pkg.dev", "slack.com", "*.slack.com"} {
		if !patternSet[required] {
			t.Errorf("expected required pattern %q in FQDNNetworkPolicy", required)
		}
	}

	// Verify dangerous/unnecessary third-party domains and package registries are excluded
	for _, prohibited := range []string{"pypi.org", "registry.npmjs.org", "api.openai.com", "api.anthropic.com", "huggingface.co"} {
		if patternSet[prohibited] {
			t.Errorf("expected domain %q to be excluded from FQDNNetworkPolicy", prohibited)
		}
	}

	// 3. Verify disabling annotation deletes FQDNNetworkPolicy
	delete(agent.Annotations, AnnotationEnableFQDNNetworkPolicy)
	err = r.reconcileNetworkPolicy(ctx, agent, r.resolveNetpolProfile(ctx, agent), "", false)
	if err != nil {
		t.Fatalf("reconcileNetworkPolicy after disabling FQDN failed: %v", err)
	}
	err = cl.Get(ctx, types.NamespacedName{Name: "test-agent-fqdn-netpol", Namespace: "test-ns"}, fqdnNetpol)
	if !errors.IsNotFound(err) {
		t.Errorf("expected FQDNNetworkPolicy to be deleted when annotation is disabled, got %v", err)
	}
}

func TestReconcileNetworkPolicy_FQDNCRDNotPresentFallback(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
			Annotations: map[string]string{
				AnnotationEnableFQDNNetworkPolicy: "true",
			},
		},
	}

	interceptors := fakeServerSideApplyInterceptors()
	ssaPatch := interceptors.Patch
	interceptors.Patch = func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
		if u, ok := obj.(*unstructured.Unstructured); ok && u.GroupVersionKind().Kind == "FQDNNetworkPolicy" {
			return &meta.NoResourceMatchError{PartialResource: schema.GroupVersionResource{Group: "networking.gke.io", Version: "v1alpha1", Resource: "fqdnnetworkpolicies"}}
		}
		return ssaPatch(ctx, cl, obj, patch, opts...)
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithInterceptorFuncs(interceptors).
		Build()

	r := &PlatformAgentReconciler{
		Client:      cl,
		APIReader:   cl,
		Scheme:      scheme,
		APIServerIP: "10.96.0.1",
	}

	err := r.reconcileNetworkPolicy(ctx, agent, r.resolveNetpolProfile(ctx, agent), "", false)
	if err != nil {
		t.Fatalf("reconcileNetworkPolicy failed: %v", err)
	}

	// Verify standard NetworkPolicy kept the blanket external HTTPS rule (rule 7) because CRD is absent
	netpol := &networkingv1.NetworkPolicy{}
	err = cl.Get(ctx, types.NamespacedName{Name: "test-agent-gateway-netpol", Namespace: "test-ns"}, netpol)
	if err != nil {
		t.Fatalf("failed to get reconciled NetworkPolicy: %v", err)
	}

	if len(netpol.Spec.Egress) != 12 {
		t.Errorf("expected 12 Egress rules when FQDN CRD is not present (fallback to blanket external HTTPS), got %d", len(netpol.Spec.Egress))
	}
	foundBlanketHTTPS := false
	for _, egress := range netpol.Spec.Egress {
		for _, peer := range egress.To {
			if peer.IPBlock != nil && peer.IPBlock.CIDR == "0.0.0.0/0" {
				foundBlanketHTTPS = true
			}
		}
	}
	if !foundBlanketHTTPS {
		t.Errorf("expected blanket 0.0.0.0/0 external HTTPS egress rule to be kept when FQDN CRD is absent")
	}
}

func TestReconcileNetworkPolicy_FQDNCRDWrappedErrorFallback(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
			Annotations: map[string]string{
				AnnotationEnableFQDNNetworkPolicy: "true",
			},
		},
	}

	interceptors := fakeServerSideApplyInterceptors()
	ssaPatch := interceptors.Patch
	interceptors.Patch = func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
		if u, ok := obj.(*unstructured.Unstructured); ok && u.GroupVersionKind().Kind == "FQDNNetworkPolicy" {
			return fmt.Errorf("failed to get restmapping for FQDNNetworkPolicy")
		}
		return ssaPatch(ctx, cl, obj, patch, opts...)
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithInterceptorFuncs(interceptors).
		Build()

	r := &PlatformAgentReconciler{
		Client:      cl,
		APIReader:   cl,
		Scheme:      scheme,
		APIServerIP: "10.96.0.1",
	}

	err := r.reconcileNetworkPolicy(ctx, agent, r.resolveNetpolProfile(ctx, agent), "", false)
	if err != nil {
		t.Fatalf("reconcileNetworkPolicy failed: %v", err)
	}

	netpol := &networkingv1.NetworkPolicy{}
	err = cl.Get(ctx, types.NamespacedName{Name: "test-agent-gateway-netpol", Namespace: "test-ns"}, netpol)
	if err != nil {
		t.Fatalf("failed to get reconciled NetworkPolicy: %v", err)
	}

	if len(netpol.Spec.Egress) != 12 {
		t.Errorf("expected 12 Egress rules when FQDN CRD returns wrapped restmapping error (fallback to blanket external HTTPS), got %d", len(netpol.Spec.Egress))
	}
}

func TestReconcileNetworkPolicy_TruncateMaxCIDRs(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()

	// Generate 70 valid /32 CIDRs (exceeding maxCIDRsPerAnnotation=50)
	var cidrList []string
	for i := 1; i <= 70; i++ {
		cidrList = append(cidrList, fmt.Sprintf("172.16.1.%d/32", i))
	}
	customCIDRs := strings.Join(cidrList, ",")

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-max-cidrs",
			Namespace: "test-ns",
			Annotations: map[string]string{
				AnnotationCustomEgressCIDRs: customCIDRs,
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{
		Client:      cl,
		APIReader:   cl,
		Scheme:      scheme,
		APIServerIP: "10.96.0.1",
	}

	if err := r.reconcileNetworkPolicy(ctx, agent, r.resolveNetpolProfile(ctx, agent), "", false); err != nil {
		t.Fatalf("reconcileNetworkPolicy failed: %v", err)
	}

	netpol := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-max-cidrs-gateway-netpol", Namespace: "test-ns"}, netpol); err != nil {
		t.Fatalf("failed to get reconciled NetworkPolicy: %v", err)
	}

	// Count CIDRs in API server egress rule (port 6443)
	customCount := 0
	for _, egressRule := range netpol.Spec.Egress {
		for _, port := range egressRule.Ports {
			if port.Port != nil && port.Port.IntVal == 6443 {
				for _, peer := range egressRule.To {
					if peer.IPBlock != nil && strings.HasPrefix(peer.IPBlock.CIDR, "172.16.1.") {
						customCount++
					}
				}
			}
		}
	}

	if customCount != 50 {
		t.Errorf("expected exactly 50 custom CIDRs after truncation, got %d", customCount)
	}
}

func TestReconcileNetworkPolicy_PrivateIPOverlap(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()

	// API server has a private ClusterIP in 172.16.0.1
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-private-ip",
			Namespace: "test-ns",
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{
		Client:      cl,
		APIReader:   cl,
		Scheme:      scheme,
		APIServerIP: "172.16.0.1",
	}

	if err := r.reconcileNetworkPolicy(ctx, agent, r.resolveNetpolProfile(ctx, agent), "", false); err != nil {
		t.Fatalf("reconcileNetworkPolicy failed: %v", err)
	}

	netpol := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-private-ip-gateway-netpol", Namespace: "test-ns"}, netpol); err != nil {
		t.Fatalf("failed to get reconciled NetworkPolicy: %v", err)
	}

	// Verify API server rule explicitly allows 172.16.0.1/32
	foundAPIRule := false
	for _, egressRule := range netpol.Spec.Egress {
		for _, port := range egressRule.Ports {
			if port.Port != nil && port.Port.IntVal == 6443 {
				for _, peer := range egressRule.To {
					if peer.IPBlock != nil && peer.IPBlock.CIDR == "172.16.0.1/32" {
						foundAPIRule = true
					}
				}
			}
		}
	}

	if !foundAPIRule {
		t.Errorf("expected 172.16.0.1/32 to be explicitly allowed in API server egress rule")
	}
}

// TestReconcilePodDisruptionBudget_CreatesEvictableBudget covers the ordinary
// path: a single-replica agent gets maxUnavailable: 1, so a node drain is
// permitted rather than blocked.
func TestReconcilePodDisruptionBudget_CreatesEvictableBudget(t *testing.T) {
	ctx := context.Background()
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.reconcilePodDisruptionBudget(ctx, agent); err != nil {
		t.Fatalf("reconcilePodDisruptionBudget failed: %v", err)
	}

	pdb := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}, pdb); err != nil {
		t.Fatalf("failed to get reconciled PodDisruptionBudget: %v", err)
	}
	if pdb.Spec.MaxUnavailable == nil || pdb.Spec.MaxUnavailable.IntValue() != 1 {
		t.Errorf("expected maxUnavailable 1, got %v", pdb.Spec.MaxUnavailable)
	}
	if pdb.Spec.MinAvailable != nil {
		t.Errorf("expected no minAvailable on a single-replica budget, got %v", pdb.Spec.MinAvailable)
	}
	if len(pdb.OwnerReferences) != 1 || pdb.OwnerReferences[0].Name != "test-agent" {
		t.Errorf("expected the PodDisruptionBudget to be owned by the PlatformAgent, got %v", pdb.OwnerReferences)
	}
}

// pdbSSAInterceptors emulates the one server-side-apply rule the plain fake
// client does not: an apply cannot remove a field a different manager owns. The
// real API server merges the applied object over that field rather than
// dropping it, and then rejects the result, because minAvailable and
// maxUnavailable are mutually exclusive. Without this, no test can reproduce
// the wedge clearForeignPDBBudgetField exists to clear — the stock interceptor
// replaces the whole object, so the stray field vanishes on its own.
func pdbSSAInterceptors() interceptor.Funcs {
	base := fakeServerSideApplyInterceptors()
	return interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if desired, ok := obj.(*policyv1.PodDisruptionBudget); ok && patch.Type() == types.ApplyPatchType {
				var live policyv1.PodDisruptionBudget
				if err := cl.Get(ctx, client.ObjectKeyFromObject(desired), &live); err == nil {
					if live.Spec.MinAvailable != nil && desired.Spec.MaxUnavailable != nil {
						return errors.NewInvalid(
							schema.GroupKind{Group: "policy", Kind: "PodDisruptionBudget"},
							desired.Name,
							field.ErrorList{field.Invalid(field.NewPath("spec"), desired.Spec,
								"minAvailable and maxUnavailable cannot be both set")},
						)
					}
				}
			}
			return base.Patch(ctx, cl, obj, patch, opts...)
		},
	}
}

// TestReconcilePodDisruptionBudget_RecoversFromForeignBudgetField is the
// regression test for a permanent reconcile wedge: an administrator hand-sets
// minAvailable on the operator-managed budget, and because a server-side apply
// cannot remove a field it never owned, every apply afterwards merges to an
// object carrying both fields and is rejected. The whole Reconcile fails from
// that point on, so everything after this step stops running too.
//
// It goes through reconcilePodDisruptionBudget rather than calling the helper
// directly, so that deleting the call — not just gutting the helper — fails.
func TestReconcilePodDisruptionBudget_RecoversFromForeignBudgetField(t *testing.T) {
	ctx := context.Background()
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
	// What an administrator tightening the singleton default leaves behind.
	live := &policyv1.PodDisruptionBudget{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec: policyv1.PodDisruptionBudgetSpec{
			MinAvailable: ptr.To(intstr.FromInt32(1)),
			Selector: &metav1.LabelSelector{
				MatchLabels: map[string]string{"app": "test-agent-gateway"},
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, live).
		WithInterceptorFuncs(pdbSSAInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.reconcilePodDisruptionBudget(ctx, agent); err != nil {
		t.Fatalf("reconcilePodDisruptionBudget failed to recover from a foreign budget field: %v", err)
	}

	pdb := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(live), pdb); err != nil {
		t.Fatalf("failed to get PodDisruptionBudget: %v", err)
	}
	if pdb.Spec.MinAvailable != nil {
		t.Errorf("expected minAvailable to be gone, got %v", pdb.Spec.MinAvailable)
	}
	if pdb.Spec.MaxUnavailable == nil || pdb.Spec.MaxUnavailable.IntValue() != 1 {
		t.Errorf("expected maxUnavailable 1, got %v", pdb.Spec.MaxUnavailable)
	}
}

// TestBuildPlatformPDB_MaxUnavailableAtEveryReplicaCount pins the shape the
// Workload Reliability Audit requires: obtainability_audit_sop.md §3.3 is
// "Always maxUnavailable, never minAvailable", at every replica count. Deriving
// the field from the replica count instead reads as safe and produces the §3.4
// drain deadlock the moment a scaled-out agent is scaled back down.
func TestBuildPlatformPDB_MaxUnavailableAtEveryReplicaCount(t *testing.T) {
	for _, tc := range []struct {
		name       string
		deployment *agentv1alpha1.DeploymentSpec
	}{
		{name: "default single replica"},
		{
			name: "high availability",
			deployment: &agentv1alpha1.DeploymentSpec{
				Availability: &agentv1alpha1.AvailabilitySpec{Replicas: ptr.To(int32(3))},
			},
		},
		{
			name:       "scaled to zero",
			deployment: &agentv1alpha1.DeploymentSpec{ScaleToZero: ptr.To(true)},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			pdb := buildPlatformPDB(&agentv1alpha1.PlatformAgent{
				ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
				Spec: agentv1alpha1.PlatformAgentSpec{
					AgentSpec: agentv1alpha1.AgentSpec{Deployment: tc.deployment},
				},
			})
			if pdb.Spec.MinAvailable != nil {
				t.Errorf("minAvailable must never be set (SOP §3.3), got %v", pdb.Spec.MinAvailable)
			}
			if pdb.Spec.MaxUnavailable == nil || pdb.Spec.MaxUnavailable.IntValue() != 1 {
				t.Errorf("expected maxUnavailable 1, got %v", pdb.Spec.MaxUnavailable)
			}
			if pdb.Spec.Selector.MatchLabels["app"] != "test-agent-gateway" {
				t.Errorf("expected the Deployment's selector, got %v", pdb.Spec.Selector.MatchLabels)
			}
		})
	}
}

// TestClearForeignPDBBudgetField_LeavesAgreeingBudgetAlone guards against the
// obvious over-correction: the stripper runs on every reconcile, so it must be
// a no-op when the live object already carries the field the operator sets, and
// when there is no live object at all.
func TestClearForeignPDBBudgetField_LeavesAgreeingBudgetAlone(t *testing.T) {
	ctx := context.Background()
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
	live := buildPlatformPDB(agent)

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, live.DeepCopy()).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	before := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(live), before); err != nil {
		t.Fatalf("failed to get seeded PodDisruptionBudget: %v", err)
	}
	if err := r.clearForeignPDBBudgetField(ctx, live); err != nil {
		t.Fatalf("clearForeignPDBBudgetField failed: %v", err)
	}
	after := &policyv1.PodDisruptionBudget{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(live), after); err != nil {
		t.Fatalf("failed to get PodDisruptionBudget: %v", err)
	}
	if after.ResourceVersion != before.ResourceVersion {
		t.Errorf("expected no write when the live budget already agrees, resourceVersion moved %s -> %s",
			before.ResourceVersion, after.ResourceVersion)
	}

	// Nothing to clear on a first reconcile either.
	missing := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "absent-agent", Namespace: "test-ns"},
	}
	if err := r.clearForeignPDBBudgetField(ctx, buildPlatformPDB(missing)); err != nil {
		t.Fatalf("expected NotFound to be tolerated, got %v", err)
	}
}

// fqdnNetworkPolicyObject builds the unstructured FQDNNetworkPolicy the operator owns
// for an agent. The GVK is not in the scheme -- the CRD is GKE Dataplane V2's -- which is
// the reason the disabled path reads it as unstructured and why it is worth covering
// separately from the typed NetworkPolicy beside it.
func fqdnNetworkPolicyObject(namespace, name string, owner *agentv1alpha1.PlatformAgent) *unstructured.Unstructured {
	u := &unstructured.Unstructured{}
	u.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   "networking.gke.io",
		Version: "v1alpha1",
		Kind:    "FQDNNetworkPolicy",
	})
	u.SetNamespace(namespace)
	u.SetName(name)
	if owner != nil {
		u.SetOwnerReferences([]metav1.OwnerReference{{
			APIVersion: "kubeagents.x-k8s.io/v1alpha1",
			Kind:       "PlatformAgent",
			Name:       owner.Name,
			UID:        owner.UID,
			Controller: ptr.To(true),
		}})
	}
	return u
}

func TestReconcileNetworkPolicy_Disabled_DeletesOwnedOnly(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
			UID:       types.UID("1234-5678"),
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				NetworkPolicy: &agentv1alpha1.NetworkPolicySpec{
					Enabled: ptr.To(false),
				},
			},
		},
	}

	// 1. Owned NetworkPolicy (should be deleted)
	ownedNetpol := &networkingv1.NetworkPolicy{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-gateway-netpol",
			Namespace: "test-ns",
			OwnerReferences: []metav1.OwnerReference{
				{
					APIVersion: "kubeagents.x-k8s.io/v1alpha1",
					Kind:       "PlatformAgent",
					Name:       agent.Name,
					UID:        agent.UID,
					Controller: ptr.To(true),
				},
			},
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, ownedNetpol).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	// Created through the client rather than seeded with WithObjects: the
	// FQDNNetworkPolicy GVK is not in the scheme, and Create is the path that
	// registers it, the same one the reconciler takes on the enabled path.
	ownedFQDN := fqdnNetworkPolicyObject("test-ns", "test-agent-fqdn-netpol", agent)
	if err := cl.Create(ctx, ownedFQDN); err != nil {
		t.Fatalf("failed to seed owned FQDNNetworkPolicy: %v", err)
	}

	r := &PlatformAgentReconciler{
		Client:    cl,
		APIReader: cl,
		Scheme:    scheme,
	}

	profile := r.resolveNetpolProfile(ctx, agent)
	if profile.Generated {
		t.Fatalf("expected profile.Generated=false when enabled=false")
	}

	if err := r.reconcileNetworkPolicy(ctx, agent, profile, "", false); err != nil {
		t.Fatalf("reconcileNetworkPolicy failed: %v", err)
	}

	checkNetpol := &networkingv1.NetworkPolicy{}
	err := cl.Get(ctx, types.NamespacedName{Name: ownedNetpol.Name, Namespace: ownedNetpol.Namespace}, checkNetpol)
	if !errors.IsNotFound(err) {
		t.Errorf("expected owned NetworkPolicy to be deleted, got err=%v", err)
	}

	// 2. Owned FQDNNetworkPolicy (should be deleted). The docs promise enabled:false
	// removes both policies the operator owns; without this the FQDN half could stop
	// matching its owner reference and keep domain filtering on for an agent whose
	// policy management was switched off, with the suite green.
	checkFQDN := fqdnNetworkPolicyObject("test-ns", "test-agent-fqdn-netpol", nil)
	err = cl.Get(ctx, types.NamespacedName{Name: ownedFQDN.GetName(), Namespace: ownedFQDN.GetNamespace()}, checkFQDN)
	if !errors.IsNotFound(err) {
		t.Errorf("expected owned FQDNNetworkPolicy to be deleted, got err=%v", err)
	}

	// 3. Non-owned NetworkPolicy with same name (should NOT be deleted)
	foreignNetpol := &networkingv1.NetworkPolicy{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-gateway-netpol",
			Namespace: "test-ns",
		},
	}
	clForeign := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, foreignNetpol).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	foreignFQDN := fqdnNetworkPolicyObject("test-ns", "test-agent-fqdn-netpol", nil)
	if err := clForeign.Create(ctx, foreignFQDN); err != nil {
		t.Fatalf("failed to seed foreign FQDNNetworkPolicy: %v", err)
	}

	rForeign := &PlatformAgentReconciler{
		Client:    clForeign,
		APIReader: clForeign,
		Scheme:    scheme,
	}

	if err := rForeign.reconcileNetworkPolicy(ctx, agent, profile, "", false); err != nil {
		t.Fatalf("reconcileNetworkPolicy failed: %v", err)
	}

	err = clForeign.Get(ctx, types.NamespacedName{Name: foreignNetpol.Name, Namespace: foreignNetpol.Namespace}, checkNetpol)
	if err != nil {
		t.Errorf("expected unowned/foreign NetworkPolicy to be preserved, got err=%v", err)
	}

	// 4. Non-owned FQDNNetworkPolicy with the same name (should NOT be deleted).
	checkForeignFQDN := fqdnNetworkPolicyObject("test-ns", "test-agent-fqdn-netpol", nil)
	err = clForeign.Get(ctx, types.NamespacedName{Name: foreignFQDN.GetName(), Namespace: foreignFQDN.GetNamespace()}, checkForeignFQDN)
	if err != nil {
		t.Errorf("expected unowned/foreign FQDNNetworkPolicy to be preserved, got err=%v", err)
	}
}

func TestReconcileNetworkPolicy_StatusReporting(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				NetworkPolicy: &agentv1alpha1.NetworkPolicySpec{
					DNSClusterIPs: []string{"10.200.0.10"},
					MetadataDaemon: &agentv1alpha1.MetadataDaemonSpec{
						Endpoint: "169.254.169.245",
					},
				},
			},
		},
	}

	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-gateway",
			Namespace: "test-ns",
		},
		Status: appsv1.DeploymentStatus{
			ReadyReplicas: 1,
		},
	}

	// The other two workloads Ready depends on. This test is about what the
	// NetworkPolicy status fields say, so it wants the phase to reach Ready — and
	// since the credential-broker split that takes all three, because a gateway on
	// its own is an agent that can run no command. readSplitWorkloads reads them.
	shell := &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-shell",
			Namespace: "test-ns",
		},
		Status: appsv1.StatefulSetStatus{ReadyReplicas: 1},
	}
	broker := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-credential-proxy",
			Namespace: "test-ns",
		},
		Status: appsv1.DeploymentStatus{ReadyReplicas: 1},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, dep, shell, broker).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{
		Client:    cl,
		APIReader: cl,
		Scheme:    scheme,
	}

	profile := r.resolveNetpolProfile(ctx, agent)
	phase, err := r.updateStatusReady(ctx, agent, "http://otel:4318", "Spec", profile)
	if err != nil {
		t.Fatalf("updateStatusReady failed: %v", err)
	}
	if phase != "Ready" {
		t.Errorf("got phase %q, want Ready", phase)
	}

	if !agent.Status.NetworkPolicy.Generated {
		t.Errorf("expected Status.NetworkPolicy.Generated to be true")
	}
	if agent.Status.NetworkPolicy.DNSClusterIPsSource != "Spec" {
		t.Errorf("got DNSClusterIPsSource %q, want Spec", agent.Status.NetworkPolicy.DNSClusterIPsSource)
	}
	if !reflect.DeepEqual(agent.Status.NetworkPolicy.DNSClusterIPs, []string{"10.200.0.10"}) {
		t.Errorf("got DNSClusterIPs %v, want [10.200.0.10]", agent.Status.NetworkPolicy.DNSClusterIPs)
	}
	if agent.Status.NetworkPolicy.MetadataDaemonIP != "169.254.169.245" {
		t.Errorf("got MetadataDaemonIP %q, want 169.254.169.245", agent.Status.NetworkPolicy.MetadataDaemonIP)
	}
	if agent.Status.NetworkPolicy.MetadataDaemonPort != metadataDaemonDefaultPort {
		t.Errorf("got MetadataDaemonPort %d, want %d", agent.Status.NetworkPolicy.MetadataDaemonPort, metadataDaemonDefaultPort)
	}
	if agent.Status.NetworkPolicy.MetadataDaemonIPSource != "Spec" {
		t.Errorf("got MetadataDaemonIPSource %q, want Spec", agent.Status.NetworkPolicy.MetadataDaemonIPSource)
	}
}

// TestReconcileNetworkPolicy_StatusReporting_Disabled covers the one state
// status.networkPolicy.generated exists to express.
//
// Generated deliberately carries no omitempty: encoding/json drops a false bool
// under omitempty, so a disabled agent would serialise as `networkPolicy: {}` and
// an operator asking "is the operator managing my policy?" would get silence.
// That choice is defended by a comment in common_types.go and by nothing else --
// re-adding omitempty during a cleanup looks like tidying up next to the field's
// five omitempty neighbours, and every existing assertion is on Generated == true,
// which omitempty does not touch. The JSON check below is the tripwire.
//
// The DNS and metadata assertions cover the same reconcile's early return:
// resolveNetpolProfile bails before either ladder runs when enabled is false, so a
// Spec pin that is set here must still report nothing.
func TestReconcileNetworkPolicy_StatusReporting_Disabled(t *testing.T) {
	scheme := setupScheme()
	ctx := context.Background()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				NetworkPolicy: &agentv1alpha1.NetworkPolicySpec{
					Enabled: ptr.To(false),
					// Set, and must still not be reported: the early return
					// precedes the DNS ladder.
					DNSClusterIPs: []string{"10.200.0.10"},
					MetadataDaemon: &agentv1alpha1.MetadataDaemonSpec{
						Endpoint: "169.254.169.245",
					},
				},
			},
		},
	}

	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-gateway",
			Namespace: "test-ns",
		},
		Status: appsv1.DeploymentStatus{
			ReadyReplicas: 1,
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, dep).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{
		Client:    cl,
		APIReader: cl,
		Scheme:    scheme,
	}

	profile := r.resolveNetpolProfile(ctx, agent)
	if _, err := r.updateStatusReady(ctx, agent, "http://otel:4318", "Spec", profile); err != nil {
		t.Fatalf("updateStatusReady failed: %v", err)
	}

	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}, stored); err != nil {
		t.Fatalf("failed to read the agent back: %v", err)
	}

	netpol := stored.Status.NetworkPolicy
	if netpol.Generated {
		t.Errorf("got Status.NetworkPolicy.Generated=true, want false for enabled=false")
	}
	if len(netpol.DNSClusterIPs) != 0 {
		t.Errorf("got DNSClusterIPs %v, want none: the DNS ladder never runs when generation is off", netpol.DNSClusterIPs)
	}
	if netpol.DNSClusterIPsSource != "" {
		t.Errorf("got DNSClusterIPsSource %q, want empty", netpol.DNSClusterIPsSource)
	}
	if netpol.MetadataDaemonIP != "" {
		t.Errorf("got MetadataDaemonIP %q, want empty", netpol.MetadataDaemonIP)
	}
	if netpol.MetadataDaemonIPSource != "" {
		t.Errorf("got MetadataDaemonIPSource %q, want empty", netpol.MetadataDaemonIPSource)
	}

	// The whole point of the missing omitempty: the key has to survive encoding.
	encoded, err := json.Marshal(netpol)
	if err != nil {
		t.Fatalf("failed to marshal NetworkPolicyStatus: %v", err)
	}
	if got := string(encoded); !strings.Contains(got, `"generated":false`) {
		t.Errorf("encoded status is %s, want it to carry \"generated\":false -- omitempty on Generated would erase the one state this field reports", got)
	}
}

// TestABrokenNativeSidecarIsReportedDegraded guards the status path against the
// container-list split the native sidecar introduced.
//
// The credential proxy is an init container now, so it reports into
// InitContainerStatuses. When it cannot start, the kubelet never creates the app
// containers at all and ContainerStatuses is empty -- so a status check that reads
// only the app list finds nothing wrong, PodScheduled is True, and the CR sits in
// Provisioning saying it is waiting for replicas while the pod is in
// Init:CrashLoopBackOff. That is the pod's worst failure reported as silence, and
// it is the failure this whole change makes more likely to matter, since a proxy
// that will not start now blocks the entire pod rather than one container.
func TestABrokenNativeSidecarIsReportedDegraded(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec:       agentv1alpha1.PlatformAgentSpec{},
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-gateway-abc",
			Namespace: "test-ns",
			Labels:    map[string]string{"app": "test-agent-gateway"},
		},
		Status: corev1.PodStatus{
			// Exactly the shape the kubelet produces: the sidecar is stuck and no
			// app container was ever created.
			InitContainerStatuses: []corev1.ContainerStatus{{
				// A preceding init container that is merely waiting its turn.
				// Measured on a cluster: this is what the list actually looks
				// like, and reporting this entry names no fault.
				Name: "sandbox-credential-cleanup",
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{
					Reason: "PodInitializing",
				}},
			}, {
				Name: "envoy-credential-proxy",
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{
					Reason:  "ImagePullBackOff",
					Message: "Back-off pulling image",
				}},
			}},
			// Not empty while the pod is stuck in Init -- the kubelet fills this
			// in with placeholders, which is why the old code reported
			// PodInitializing rather than nothing at all.
			ContainerStatuses: []corev1.ContainerStatus{{
				Name:  "platform-agent",
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: "PodInitializing"}},
			}},
			Conditions: []corev1.PodCondition{{Type: corev1.PodScheduled, Status: corev1.ConditionTrue}},
		},
	}

	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, pod).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	phase, reason, message := r.getDeploymentStatusDetails(context.Background(), agent)

	if phase != "Degraded" {
		t.Errorf("phase = %q, want Degraded -- a pod stuck in Init reports as healthy", phase)
	}
	if reason != "ImagePullBackOff" {
		t.Errorf("reason = %q, want ImagePullBackOff", reason)
	}
	if !strings.Contains(message, "envoy-credential-proxy") {
		t.Errorf("message does not name the failing container: %q", message)
	}
}

func TestSyncGithubTokenMinterConfigMap(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				IntegrationSpec: agentv1alpha1.IntegrationSpec{
					GitHub: &agentv1alpha1.GitHubSpec{
						Org: "test-org",
					},
				},
			},
		},
	}

	minterCM := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "github-token-minter-config",
			Namespace: "test-ns",
		},
		Data: map[string]string{
			"default.yaml":          "version: 'minty.abcxyz.dev/v2'\nscope:\n  platform-agent-scope:\n    repositories:\n      - 'default-repo'\n",
			"unmanaged-static.yaml": "version: 'minty.abcxyz.dev/v2'\n",
		},
	}

	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, minterCM).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	ctx := context.Background()

	// 0. Sync with empty managed_repos on fresh ConfigMap — should be a no-op and preserve unmanaged-static.yaml
	err := r.syncGithubTokenMinterConfigMap(ctx, agent, "")
	if err != nil {
		t.Fatalf("syncGithubTokenMinterConfigMap with empty repos failed: %v", err)
	}

	updatedCM := &corev1.ConfigMap{}
	if err := cl.Get(ctx, client.ObjectKey{Name: "github-token-minter-config", Namespace: "test-ns"}, updatedCM); err != nil {
		t.Fatalf("failed to get updated ConfigMap: %v", err)
	}
	if _, exists := updatedCM.Data["unmanaged-static.yaml"]; !exists {
		t.Errorf("expected unmanaged-static.yaml to be preserved when managed_repos is empty")
	}

	// 1. Sync with managed_repos JSON: repo-1 and repo-2
	err = r.syncGithubTokenMinterConfigMap(ctx, agent, `[{"type":"github","url":"https://github.com/test-org/repo-1"},{"type":"github","url":"https://github.com/test-org/repo-2"}]`)
	if err != nil {
		t.Fatalf("syncGithubTokenMinterConfigMap failed: %v", err)
	}

	if err := cl.Get(ctx, client.ObjectKey{Name: "github-token-minter-config", Namespace: "test-ns"}, updatedCM); err != nil {
		t.Fatalf("failed to get updated ConfigMap: %v", err)
	}

	// Verify repo-1.yaml and repo-2.yaml were created and scoped to all managed repos in org
	expectedRepo1 := "version: 'minty.abcxyz.dev/v2'\nscope:\n  platform-agent-scope:\n    repositories:\n      - 'repo-1'\n      - 'repo-2'\n"
	expectedRepo2 := "version: 'minty.abcxyz.dev/v2'\nscope:\n  platform-agent-scope:\n    repositories:\n      - 'repo-1'\n      - 'repo-2'\n"
	if updatedCM.Data["repo-1.yaml"] != expectedRepo1 {
		t.Errorf("expected repo-1.yaml to contain all managed repos in org, got %q", updatedCM.Data["repo-1.yaml"])
	}
	if updatedCM.Data["repo-2.yaml"] != expectedRepo2 {
		t.Errorf("expected repo-2.yaml to contain all managed repos in org, got %q", updatedCM.Data["repo-2.yaml"])
	}

	// Verify unmanaged-static.yaml was NOT pruned
	if _, exists := updatedCM.Data["unmanaged-static.yaml"]; !exists {
		t.Errorf("expected unmanaged-static.yaml to be preserved as it is not operator-managed")
	}

	// Verify default.yaml was preserved
	expectedDefault := "version: 'minty.abcxyz.dev/v2'\nscope:\n  platform-agent-scope:\n    repositories:\n      - 'default-repo'\n"
	if updatedCM.Data["default.yaml"] != expectedDefault {
		t.Errorf("expected default.yaml to be preserved, got %q", updatedCM.Data["default.yaml"])
	}

	// Verify annotation tracks operator-managed keys
	expectedAnn := "repo-1.yaml,repo-2.yaml"
	if ann := updatedCM.Annotations[AnnotationManagedMinterKeys]; ann != expectedAnn {
		t.Errorf("expected annotation %q, got %q", expectedAnn, ann)
	}

	// 2. Remove repo-2 from managed_repos
	err = r.syncGithubTokenMinterConfigMap(ctx, agent, `[{"type":"github","url":"https://github.com/test-org/repo-1"}]`)
	if err != nil {
		t.Fatalf("syncGithubTokenMinterConfigMap failed: %v", err)
	}

	if err := cl.Get(ctx, client.ObjectKey{Name: "github-token-minter-config", Namespace: "test-ns"}, updatedCM); err != nil {
		t.Fatalf("failed to get updated ConfigMap: %v", err)
	}

	// Verify repo-1.yaml exists and repo-2.yaml was pruned because it was operator-managed
	if _, exists := updatedCM.Data["repo-1.yaml"]; !exists {
		t.Errorf("expected repo-1.yaml to remain present")
	}
	if _, exists := updatedCM.Data["repo-2.yaml"]; exists {
		t.Errorf("expected repo-2.yaml to be pruned after removal from managed_repos")
	}
	// Verify unmanaged-static.yaml is STILL present
	if _, exists := updatedCM.Data["unmanaged-static.yaml"]; !exists {
		t.Errorf("expected unmanaged-static.yaml to remain untouched")
	}

	expectedAnnAfter := "repo-1.yaml"
	if ann := updatedCM.Annotations[AnnotationManagedMinterKeys]; ann != expectedAnnAfter {
		t.Errorf("expected annotation %q, got %q", expectedAnnAfter, ann)
	}

	// 3. Sync with managed_repos including a cross-org repo (other-org/other-repo) — should skip creating other-repo.yaml
	err = r.syncGithubTokenMinterConfigMap(ctx, agent, `[{"type":"github","url":"https://github.com/test-org/repo-1"},{"type":"github","url":"https://github.com/other-org/other-repo"}]`)
	if err != nil {
		t.Fatalf("syncGithubTokenMinterConfigMap failed with cross-org repo: %v", err)
	}

	if err := cl.Get(ctx, client.ObjectKey{Name: "github-token-minter-config", Namespace: "test-ns"}, updatedCM); err != nil {
		t.Fatalf("failed to get updated ConfigMap: %v", err)
	}

	if _, exists := updatedCM.Data["repo-1.yaml"]; !exists {
		t.Errorf("expected repo-1.yaml to remain present")
	}
	if _, exists := updatedCM.Data["other-repo.yaml"]; exists {
		t.Errorf("expected cross-org other-repo.yaml to be skipped")
	}

	// 4. Sync with empty Org but GitRepo set — should infer primaryOrg from GitRepo and skip cross-org repos
	agentInferred := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent-inferred", Namespace: "test-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				IntegrationSpec: agentv1alpha1.IntegrationSpec{
					GitHub: &agentv1alpha1.GitHubSpec{
						Org:     "",
						GitRepo: "test-org/main-repo",
					},
				},
			},
		},
	}
	err = r.syncGithubTokenMinterConfigMap(ctx, agentInferred, `[{"type":"github","url":"https://github.com/test-org/repo-1"},{"type":"github","url":"https://github.com/forbidden-org/forbidden-repo"}]`)
	if err != nil {
		t.Fatalf("syncGithubTokenMinterConfigMap with inferred org failed: %v", err)
	}

	if err := cl.Get(ctx, client.ObjectKey{Name: "github-token-minter-config", Namespace: "test-ns"}, updatedCM); err != nil {
		t.Fatalf("failed to get updated ConfigMap: %v", err)
	}

	if _, exists := updatedCM.Data["forbidden-repo.yaml"]; exists {
		t.Errorf("expected cross-org forbidden-repo.yaml to be skipped when primaryOrg is inferred from GitRepo")
	}
}

// An unrecognized spec.mode can only reach the reconciler through version skew
// (enum validation rejects it at admission — the fake client, like a newer CRD
// with an older binary, does not). The contract from the mode spec: Degraded
// with reason ModeNotRecognized, today's stack still rendered, and a requeue.
func TestPlatformAgentReconciler_Reconcile_UnrecognizedMode(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec:       agentv1alpha1.PlatformAgentSpec{Mode: ptr.To("quantum")},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	// 1st reconcile adds the finalizer, 2nd does the work.
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}
	result, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}
	if result.RequeueAfter != 30*time.Second {
		t.Errorf("expected 30s requeue while mode is unrecognized, got %v", result.RequeueAfter)
	}

	updated := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, updated); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	if updated.Status.Phase != "Degraded" {
		t.Errorf("expected phase Degraded, got %q", updated.Status.Phase)
	}
	cond := meta.FindStatusCondition(updated.Status.Conditions, "Ready")
	if cond == nil {
		t.Fatal("expected a Ready condition")
	}
	if cond.Reason != "ModeNotRecognized" {
		t.Errorf("expected reason ModeNotRecognized, got %q", cond.Reason)
	}
	if !strings.Contains(cond.Message, "quantum") {
		t.Errorf("condition message must name the unrecognized value, got %q", cond.Message)
	}

	// Fail-closed means the dark stack stays dark AND today's stack still renders:
	// the cluster keeps running what it ran, with the skew visible in status.
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-gateway", Namespace: "test-ns"}, dep); err != nil {
		t.Errorf("today's Deployment should still be rendered under an unrecognized mode: %v", err)
	}

	// Correcting the mode clears the Degraded phase on the next reconcile.
	if err := cl.Get(ctx, req.NamespacedName, updated); err != nil {
		t.Fatalf("failed to refetch agent: %v", err)
	}
	updated.Spec.Mode = nil
	if err := cl.Update(ctx, updated); err != nil {
		t.Fatalf("failed to update agent: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 3 failed: %v", err)
	}
	if err := cl.Get(ctx, req.NamespacedName, updated); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	if updated.Status.Phase == "Degraded" {
		t.Errorf("expected Degraded to clear once the mode is valid, still %q", updated.Status.Phase)
	}
}
