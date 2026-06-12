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
	"fmt"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const platformAgentFinalizer = "kubeagents.x-k8s.io/finalizer"

// PlatformAgentReconciler reconciles a PlatformAgent object
type PlatformAgentReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=platformagents,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=platformagents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=platformagents/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=serviceaccounts;persistentvolumeclaims;configmaps,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=rbac.authorization.k8s.io,resources=clusterroles;clusterrolebindings,verbs=get;list;watch;create;update;patch;delete;bind

func (r *PlatformAgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	instance := &agentv1alpha1.PlatformAgent{}
	if err := r.Get(ctx, req.NamespacedName, instance); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	log.Info("Reconciling PlatformAgent", "name", instance.Name, "namespace", instance.Namespace)

	// 1. Intercept Deletion
	if !instance.ObjectMeta.DeletionTimestamp.IsZero() {
		return r.handleDeletion(ctx, instance)
	}

	// 2. Add Finalizer if not present
	if !controllerutil.ContainsFinalizer(instance, platformAgentFinalizer) {
		controllerutil.AddFinalizer(instance, platformAgentFinalizer)
		if err := r.Update(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
		// Return immediately after update to fetch the fresh ResourceVersion, preventing OptimisticLockErrors
		return ctrl.Result{}, nil
	}

	// 3. Reconcile K8s Service Account (with Workload Identity annotation)
	if err := r.reconcileServiceAccount(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 4. Reconcile RBAC (ClusterRole and ClusterRoleBindings)
	if err := r.reconcileRBAC(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 5. Reconcile PVC for agent persistent data
	if err := r.reconcilePVC(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 6. Reconcile ConfigMap (config.yaml content)
	configMapHash, err := r.reconcileConfigMap(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// 7. Reconcile Deployment (with pod template hash annotation)
	if err := r.reconcileDeployment(ctx, instance, configMapHash); err != nil {
		return ctrl.Result{}, err
	}

	// 8. Update status phase to Ready
	return ctrl.Result{}, r.updateStatusReady(ctx, instance)
}

func (r *PlatformAgentReconciler) handleDeletion(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (ctrl.Result, error) {
	if controllerutil.ContainsFinalizer(agent, platformAgentFinalizer) {
		viewerBindingName := fmt.Sprintf("kubeagents:viewer:%s:%s", agent.Namespace, agent.Name)
		explorerBindingName := fmt.Sprintf("kubeagents:explorer:%s:%s", agent.Namespace, agent.Name)
		explorerRoleName := fmt.Sprintf("kubeagents:explorer:%s:%s", agent.Namespace, agent.Name)

		// Delete Viewer ClusterRoleBinding
		crbViewer := &rbacv1.ClusterRoleBinding{ObjectMeta: metav1.ObjectMeta{Name: viewerBindingName}}
		if err := client.IgnoreNotFound(r.Delete(ctx, crbViewer)); err != nil {
			return ctrl.Result{}, err
		}

		// Delete Explorer ClusterRoleBinding
		crbExplorer := &rbacv1.ClusterRoleBinding{ObjectMeta: metav1.ObjectMeta{Name: explorerBindingName}}
		if err := client.IgnoreNotFound(r.Delete(ctx, crbExplorer)); err != nil {
			return ctrl.Result{}, err
		}

		// Delete Explorer ClusterRole
		crExplorer := &rbacv1.ClusterRole{ObjectMeta: metav1.ObjectMeta{Name: explorerRoleName}}
		if err := client.IgnoreNotFound(r.Delete(ctx, crExplorer)); err != nil {
			return ctrl.Result{}, err
		}

		// Resource is deleted. Safe to remove finalizer and update.
		controllerutil.RemoveFinalizer(agent, platformAgentFinalizer)
		if err := r.Update(ctx, agent); err != nil {
			return ctrl.Result{}, err
		}
	}
	return ctrl.Result{}, nil
}

func (r *PlatformAgentReconciler) reconcileServiceAccount(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	sa := buildServiceAccount(agent)
	if err := ctrl.SetControllerReference(agent, sa, r.Scheme); err != nil {
		return err
	}
	return r.Patch(ctx, sa, client.Apply, client.ForceOwnership, client.FieldOwner("platformagent-controller"))
}

func (r *PlatformAgentReconciler) reconcilePVC(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	pvc := buildPVC(agent)
	if err := ctrl.SetControllerReference(agent, pvc, r.Scheme); err != nil {
		return err
	}

	found := &corev1.PersistentVolumeClaim{}
	err := r.Get(ctx, client.ObjectKey{Name: pvc.Name, Namespace: pvc.Namespace}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			return r.Create(ctx, pvc)
		}
		return err
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcileConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, error) {
	cm := buildConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.Patch(ctx, cm, client.Apply, client.ForceOwnership, client.FieldOwner("platformagent-controller"))
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

func (r *PlatformAgentReconciler) reconcileDeployment(ctx context.Context, agent *agentv1alpha1.PlatformAgent, configHash string) error {
	dep := buildDeployment(agent, configHash)
	if err := ctrl.SetControllerReference(agent, dep, r.Scheme); err != nil {
		return err
	}
	return r.Patch(ctx, dep, client.Apply, client.ForceOwnership, client.FieldOwner("platformagent-controller"))
}

func (r *PlatformAgentReconciler) reconcileRBAC(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	viewerBindingName := fmt.Sprintf("kubeagents:viewer:%s:%s", agent.Namespace, agent.Name)
	crbViewer := buildClusterRoleBinding(agent, viewerBindingName, "view")
	err := r.Patch(ctx, crbViewer, client.Apply, client.ForceOwnership, client.FieldOwner("platformagent-controller"))
	if err != nil {
		return fmt.Errorf("failed to reconcile viewer ClusterRoleBinding: %w", err)
	}

	explorerRole := buildPlatformExplorerRole(agent)
	err = r.Patch(ctx, explorerRole, client.Apply, client.ForceOwnership, client.FieldOwner("platformagent-controller"))
	if err != nil {
		return fmt.Errorf("failed to reconcile explorer ClusterRole: %w", err)
	}

	explorerBindingName := fmt.Sprintf("kubeagents:explorer:%s:%s", agent.Namespace, agent.Name)
	crbExplorer := buildClusterRoleBinding(agent, explorerBindingName, explorerRole.Name)
	err = r.Patch(ctx, crbExplorer, client.Apply, client.ForceOwnership, client.FieldOwner("platformagent-controller"))
	if err != nil {
		return fmt.Errorf("failed to reconcile explorer ClusterRoleBinding: %w", err)
	}

	return nil
}

func (r *PlatformAgentReconciler) updateStatusReady(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	dep := &appsv1.Deployment{}
	err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}, dep)
	if err == nil {
		agent.Status.DeploymentStatus.Name = dep.Name
		agent.Status.DeploymentStatus.ReadyReplicas = dep.Status.ReadyReplicas
	}

	pvc := &corev1.PersistentVolumeClaim{}
	err = r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-data"}, pvc)
	if err == nil {
		agent.Status.StorageStatus.Bound = (pvc.Status.Phase == corev1.ClaimBound)
	}

	agent.Status.Phase = "Ready"
	now := metav1.Now()
	agent.Status.LastReconcileTime = &now

	return r.Status().Update(ctx, agent)
}

// SetupWithManager sets up the controller with the Manager.
func (r *PlatformAgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.PlatformAgent{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&corev1.ConfigMap{}).
		Watches(
			&rbacv1.ClusterRoleBinding{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				parts := strings.Split(obj.GetName(), ":") // format: kubeagents:<role>:<namespace>:<name>
				if len(parts) == 4 && parts[0] == "kubeagents" {
					return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: parts[2], Name: parts[3]}}}
				}
				return nil
			}),
		).
		Watches(
			&rbacv1.ClusterRole{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				parts := strings.Split(obj.GetName(), ":") // format: kubeagents:<role>:<namespace>:<name>
				if len(parts) == 4 && parts[0] == "kubeagents" {
					return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: parts[2], Name: parts[3]}}}
				}
				return nil
			}),
		).
		Named("platformagent").
		Complete(r)
}
