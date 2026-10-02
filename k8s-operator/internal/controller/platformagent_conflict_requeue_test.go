package controller

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/go-logr/logr"
	"github.com/go-logr/logr/funcr"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
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
	// The apiserver constructs 409 Conflict StatusDetails with Group matching the CRD group
	// ("kubeagents.x-k8s.io") and Kind matching the CRD plural resource ("platformagents").
	// We pin these literals explicitly here rather than referencing platformAgentGroupResource
	// to ensure positive tests fail if the reconciler's matcher drifts from the apiserver wire shape (#2281).
	return errors.NewConflict(
		schema.GroupResource{Group: "kubeagents.x-k8s.io", Resource: "platformagents"},
		name,
		fmt.Errorf("the object has been modified; please apply your changes to the latest version and try again"),
	)
}

// TestIsPlatformAgentConflict_WireShape verifies that isPlatformAgentConflict correctly
// matches a 409 Conflict constructed with the apiserver wire shape literals
// (Group: "kubeagents.x-k8s.io", Resource: "platformagents") and rejects singular kind
// or group mismatches, pinning the "platformagents" plural literal against drift (#2281).
func TestIsPlatformAgentConflict_WireShape(t *testing.T) {
	wireConflict := errors.NewConflict(
		schema.GroupResource{Group: "kubeagents.x-k8s.io", Resource: "platformagents"},
		"test-agent",
		fmt.Errorf("conflict"),
	)
	if !isPlatformAgentConflict(wireConflict) {
		t.Errorf("isPlatformAgentConflict(wireConflict) = false; want true for apiserver wire shape")
	}

	singularConflict := errors.NewConflict(
		schema.GroupResource{Group: "kubeagents.x-k8s.io", Resource: "platformagent"},
		"test-agent",
		fmt.Errorf("conflict"),
	)
	if isPlatformAgentConflict(singularConflict) {
		t.Errorf("isPlatformAgentConflict(singularConflict) = true; want false for singular kind mismatch")
	}

	otherGroupConflict := errors.NewConflict(
		schema.GroupResource{Group: "other.example.com", Resource: "platformagents"},
		"test-agent",
		fmt.Errorf("conflict"),
	)
	if isPlatformAgentConflict(otherGroupConflict) {
		t.Errorf("isPlatformAgentConflict(otherGroupConflict) = true; want false for group mismatch")
	}
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

// TestPlatformAgentReconciler_Reconcile_ConflictOnBothStatusAndBusCredentialsReadyRequeuesCleanly verifies
// that when a primary status update (such as updateStatusReady) encounters a 409 conflict AND the deferred
// syncBusCredentialsReady also encounters a 409 conflict on the same stale ResourceVersion, Reconcile
// requeues cleanly without returning an error and without logging a stack trace (#2281).
func TestPlatformAgentReconciler_Reconcile_ConflictOnBothStatusAndBusCredentialsReadyRequeuesCleanly(t *testing.T) {
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
					if _, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
						statusWrites++
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

	var logged strings.Builder
	ctx := logr.NewContext(context.Background(), funcr.New(func(prefix, args string) { logged.WriteString(args + "\n") }, funcr.Options{}))

	// 1st Reconcile: adds finalizer
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1: %v", err)
	}

	logged.Reset()
	statusWrites = 0
	// 2nd Reconcile: primary status write AND deferred syncBusCredentialsReady both hit 409 conflict
	result, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile returned error on 409 conflict: %v; want nil error with clean requeue", err)
	}
	if !result.Requeue {
		t.Errorf("Reconcile result.Requeue = false; want true on 409 conflict")
	}
	if result.RequeueAfter != 0 {
		t.Errorf("Reconcile result.RequeueAfter = %v; want 0 (rate-limited requeue via workqueue)", result.RequeueAfter)
	}
	if statusWrites < 2 {
		t.Errorf("statusWrites = %d; want at least 2 status writes to verify both primary and deferred conflict paths", statusWrites)
	}
	if strings.Contains(logged.String(), "could not write BusCredentialsReady") {
		t.Errorf("logged error 'could not write BusCredentialsReady' with stack trace; want it silenced when pass already requeuing on conflict:\n%s", logged.String())
	}
	if !strings.Contains(logged.String(), "Conflict writing BusCredentialsReady; pass already requeuing on conflict") {
		t.Errorf("did not log info 'Conflict writing BusCredentialsReady; pass already requeuing on conflict':\n%s", logged.String())
	}
	if !strings.Contains(logged.String(), "PlatformAgent update conflict; requeuing cleanly") {
		t.Errorf("did not log info 'PlatformAgent update conflict; requeuing cleanly':\n%s", logged.String())
	}
}

// TestPlatformAgentReconciler_Reconcile_ConflictOnBusCredentialsReadyWhenPassFailsOnNonConflictLogsError verifies
// that when a primary reconcile step fails with a non-conflict error (such as a ServiceAccount patch failure)
// and the deferred syncBusCredentialsReady also encounters a 409 conflict, Reconcile preserves log.Error
// with stack trace ("could not write BusCredentialsReady") and does not quiet the log (#2281).
func TestPlatformAgentReconciler_Reconcile_ConflictOnBusCredentialsReadyWhenPassFailsOnNonConflictLogsError(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()

	conflictErr := platformAgentConflictError(agent.Name)
	saErr := fmt.Errorf("simulated non-conflict service account error")

	ssa := fakeServerSideApplyInterceptors()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, sandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				if _, ok := obj.(*corev1.ServiceAccount); ok {
					return saErr
				}
				return ssa.Patch(ctx, c, obj, patch, opts...)
			},
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

	var logged strings.Builder
	ctx := logr.NewContext(context.Background(), funcr.New(func(prefix, args string) { logged.WriteString(args + "\n") }, funcr.Options{}))

	// 1st Reconcile: adds finalizer
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1: %v", err)
	}

	logged.Reset()
	// 2nd Reconcile: primary error is ServiceAccount failure (non-conflict), deferred syncBusCredentialsReady hits 409 conflict
	_, err := r.Reconcile(ctx, req)
	if err == nil {
		t.Fatalf("Reconcile returned nil error; want primary ServiceAccount error preserved")
	}
	if !strings.Contains(err.Error(), "simulated non-conflict service account error") {
		t.Errorf("Reconcile err = %v; want simulated non-conflict service account error", err)
	}
	if !strings.Contains(logged.String(), "could not write BusCredentialsReady") {
		t.Errorf("did not log error 'could not write BusCredentialsReady':\n%s", logged.String())
	}
	if strings.Contains(logged.String(), "Conflict writing BusCredentialsReady; pass already requeuing on conflict") {
		t.Errorf("logged info 'Conflict writing BusCredentialsReady; pass already requeuing on conflict'; want log.Error preserved on non-conflict failure:\n%s", logged.String())
	}
	if strings.Contains(logged.String(), "PlatformAgent update conflict; requeuing cleanly") {
		t.Errorf("logged 'PlatformAgent update conflict; requeuing cleanly'; want non-conflict failure not requeued cleanly:\n%s", logged.String())
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
				// Inject 409 conflict specifically on owned deployment SSA patch
				if _, ok := obj.(*appsv1.Deployment); ok {
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

// TestPlatformAgentReconciler_Reconcile_ConflictOnDifferentKindNotSwallowed verifies
// that 409 Conflict errors with the correct group but an unexpected kind/resource
// are not swallowed by the PlatformAgent conflict net and propagate as reconciler errors to controller-runtime.
func TestPlatformAgentReconciler_Reconcile_ConflictOnDifferentKindNotSwallowed(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "test-agent",
			Namespace:  "test-ns",
			Finalizers: []string{platformAgentFinalizer},
		},
		Spec: agentv1alpha1.PlatformAgentSpec{},
	}

	diffKindErr := errors.NewConflict(
		schema.GroupResource{Group: platformAgentGroupResource.Group, Resource: "differentkinds"},
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
						return diffKindErr
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
		t.Fatalf("Reconcile returned nil error on different kind 409 conflict; want error to propagate to controller-runtime")
	}
	if !errors.IsConflict(err) {
		t.Errorf("Reconcile returned err = %v; want 409 Conflict", err)
	}
	if result.Requeue {
		t.Errorf("Reconcile result.Requeue = true; want false when error is returned")
	}
}

