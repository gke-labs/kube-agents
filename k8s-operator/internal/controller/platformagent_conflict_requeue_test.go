package controller

import (
	"context"
	"fmt"
	"testing"

	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func platformAgentConflictError(name string) error {
	return errors.NewConflict(
		platformAgentGroupResource,
		name,
		fmt.Errorf("the object has been modified; please apply your changes to the latest version and try again"),
	)
}

// TestPlatformAgentReconciler_Reconcile_ConflictOnStatusUpdateRequeuesCleanly verifies
// that when a status update encounters an optimistic concurrency conflict (409 Conflict),
// Reconcile handles the conflict cleanly by returning ctrl.Result{Requeue: true} and nil error,
// preventing controller-runtime from logging an unhandled Reconciler error (#2281).
func TestPlatformAgentReconciler_Reconcile_ConflictOnStatusUpdateRequeuesCleanly(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "test-agent",
			Namespace:  "test-ns",
			Finalizers: []string{platformAgentFinalizer},
		},
		Spec: agentv1alpha1.PlatformAgentSpec{},
	}

	conflictErr := platformAgentConflictError(agent.Name)

	ssa := fakeServerSideApplyInterceptors()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: ssa.Patch,
			SubResourceUpdate: func(ctx context.Context, c client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
				if subResourceName == "status" {
					if _, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
						return conflictErr
					}
				}
				return c.SubResource(subResourceName).Update(ctx, obj, opts...)
			},
		}).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      agent.Name,
			Namespace: agent.Namespace,
		},
	}

	result, err := r.Reconcile(context.Background(), req)
	if err != nil {
		t.Fatalf("Reconcile returned error on 409 conflict: %v; want nil error with clean requeue", err)
	}
	if !result.Requeue {
		t.Errorf("Reconcile result.Requeue = false; want true on 409 conflict")
	}
	if result.RequeueAfter != 0 {
		t.Errorf("Reconcile result.RequeueAfter = %v; want 0 (rate-limited requeue via workqueue)", result.RequeueAfter)
	}
}

// TestPlatformAgentReconciler_Reconcile_ConflictOnCRUpdateRequeuesCleanly verifies
// that when a CR update (such as adding finalizer) encounters a 409 Conflict,
// Reconcile handles the conflict cleanly by returning ctrl.Result{Requeue: true} and nil error (#2281).
func TestPlatformAgentReconciler_Reconcile_ConflictOnCRUpdateRequeuesCleanly(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{},
	}

	conflictErr := platformAgentConflictError(agent.Name)

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithInterceptorFuncs(interceptor.Funcs{
			Update: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.UpdateOption) error {
				if _, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
					return conflictErr
				}
				return c.Update(ctx, obj, opts...)
			},
		}).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      agent.Name,
			Namespace: agent.Namespace,
		},
	}

	result, err := r.Reconcile(context.Background(), req)
	if err != nil {
		t.Fatalf("Reconcile returned error on 409 conflict during CR update: %v; want nil error with clean requeue", err)
	}
	if !result.Requeue {
		t.Errorf("Reconcile result.Requeue = false; want true on 409 conflict")
	}
	if result.RequeueAfter != 0 {
		t.Errorf("Reconcile result.RequeueAfter = %v; want 0 (rate-limited requeue via workqueue)", result.RequeueAfter)
	}
}

// TestPlatformAgentReconciler_Reconcile_ConflictOnBusCredentialsReadyDeferredRequeuesCleanly verifies
// that when a reconcile pass completes successfully but the deferred syncBusCredentialsReady
// status write encounters a 409 Conflict, Reconcile handles the conflict cleanly by returning
// ctrl.Result{Requeue: true} and nil error (#2281).
func TestPlatformAgentReconciler_Reconcile_ConflictOnBusCredentialsReadyDeferredRequeuesCleanly(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()

	conflictErr := platformAgentConflictError(agent.Name)

	ssa := fakeServerSideApplyInterceptors()
	statusWrites := 0
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, sandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: ssa.Patch,
			SubResourceUpdate: func(ctx context.Context, c client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
				if subResourceName == "status" {
					statusWrites++
					// First status write succeeds (e.g. from updateStatusReady or syncA2AConditions),
					// but deferred syncBusCredentialsReady status write hits 409 conflict:
					if statusWrites > 1 {
						return conflictErr
					}
				}
				return c.SubResource(subResourceName).Update(ctx, obj, opts...)
			},
		}).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      agent.Name,
			Namespace: agent.Namespace,
		},
	}

	ctx := context.Background()

	// 1st Reconcile: adds finalizer
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1: %v", err)
	}

	// 2nd Reconcile: deferred syncBusCredentialsReady hits 409 conflict
	result, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile returned error on deferred 409 conflict: %v; want nil error with clean requeue", err)
	}
	if !result.Requeue {
		t.Errorf("Reconcile result.Requeue = false; want true on deferred 409 conflict")
	}
	if result.RequeueAfter != 0 {
		t.Errorf("Reconcile result.RequeueAfter = %v; want 0 (rate-limited requeue via workqueue)", result.RequeueAfter)
	}
}

// TestPlatformAgentReconciler_Reconcile_ConflictOnOwnedObjectNotSwallowed verifies
// that 409 Conflict errors on owned objects (e.g. Deployments, ConfigMaps, Secrets)
// are NOT swallowed by the PlatformAgent conflict net and continue to propagate to
// controller-runtime to maintain error telemetry and accurate log diagnostics (#2281).
func TestPlatformAgentReconciler_Reconcile_ConflictOnOwnedObjectNotSwallowed(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "test-agent",
			Namespace:  "test-ns",
			Finalizers: []string{platformAgentFinalizer},
		},
		Spec: agentv1alpha1.PlatformAgentSpec{},
	}

	ownedConflictErr := errors.NewConflict(
		schema.GroupResource{Group: "apps", Resource: "deployments"},
		"platform-agent",
		fmt.Errorf("field manager conflict on deployment"),
	)

	ssa := fakeServerSideApplyInterceptors()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				// Inject 409 conflict on owned deployment SSA patch
				if obj.GetObjectKind().GroupVersionKind().Kind == "Deployment" || obj.GetName() == "test-agent" {
					return ownedConflictErr
				}
				return ssa.Patch(ctx, c, obj, patch, opts...)
			},
		}).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      agent.Name,
			Namespace: agent.Namespace,
		},
	}

	result, err := r.Reconcile(context.Background(), req)
	if err == nil {
		t.Fatalf("Reconcile returned nil error on owned object 409 conflict; want error to propagate to controller-runtime")
	}
	if !errors.IsConflict(err) {
		t.Errorf("Reconcile returned err = %v; want 409 Conflict", err)
	}
	if result.Requeue {
		t.Errorf("Reconcile result.Requeue = true; want false when error is returned")
	}
}

// TestPlatformAgentReconciler_Reconcile_ConflictOnDifferentGroupNotSwallowed verifies
// that 409 Conflict errors with an unexpected API group are not swallowed by the PlatformAgent
// conflict net and propagate as reconciler errors to controller-runtime.
func TestPlatformAgentReconciler_Reconcile_ConflictOnDifferentGroupNotSwallowed(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "test-agent",
			Namespace:  "test-ns",
			Finalizers: []string{platformAgentFinalizer},
		},
		Spec: agentv1alpha1.PlatformAgentSpec{},
	}

	diffGroupErr := errors.NewConflict(
		schema.GroupResource{Group: "other.example.com", Resource: "platformagents"},
		agent.Name,
		fmt.Errorf("the object has been modified"),
	)

	ssa := fakeServerSideApplyInterceptors()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: ssa.Patch,
			SubResourceUpdate: func(ctx context.Context, c client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
				if subResourceName == "status" {
					if _, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
						return diffGroupErr
					}
				}
				return c.SubResource(subResourceName).Update(ctx, obj, opts...)
			},
		}).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      agent.Name,
			Namespace: agent.Namespace,
		},
	}

	result, err := r.Reconcile(context.Background(), req)
	if err == nil {
		t.Fatalf("Reconcile returned nil error on different group 409 conflict; want error to propagate to controller-runtime")
	}
	if !errors.IsConflict(err) {
		t.Errorf("Reconcile returned err = %v; want 409 Conflict", err)
	}
	if result.Requeue {
		t.Errorf("Reconcile result.Requeue = true; want false when error is returned")
	}
}

// TestPlatformAgentReconciler_Reconcile_NonConflictErrorDoesNotRequeueCleanly verifies
// that genuine non-conflict errors (e.g. 500 InternalServerError) are not swallowed
// and continue to return an error to controller-runtime for standard handling.
func TestPlatformAgentReconciler_Reconcile_NonConflictErrorDoesNotRequeueCleanly(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "test-agent",
			Namespace:  "test-ns",
			Finalizers: []string{platformAgentFinalizer},
		},
		Spec: agentv1alpha1.PlatformAgentSpec{},
	}

	internalErr := errors.NewInternalError(fmt.Errorf("database connection refused"))

	ssa := fakeServerSideApplyInterceptors()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: ssa.Patch,
			SubResourceUpdate: func(ctx context.Context, c client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
				if subResourceName == "status" {
					if _, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
						return internalErr
					}
				}
				return c.SubResource(subResourceName).Update(ctx, obj, opts...)
			},
		}).
		Build()

	r := &PlatformAgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      agent.Name,
			Namespace: agent.Namespace,
		},
	}

	_, err := r.Reconcile(context.Background(), req)
	if err == nil {
		t.Fatalf("Reconcile returned nil error on 500 InternalServerError; want non-nil error")
	}
	if !errors.IsInternalError(err) {
		t.Errorf("Reconcile returned err = %v; want InternalError", err)
	}
}
