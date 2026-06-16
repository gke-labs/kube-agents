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

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const operatorAgentFinalizer = "kubeagents.x-k8s.io/operator-finalizer"

// OperatorAgentReconciler reconciles a OperatorAgent object
type OperatorAgentReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=operatoragents,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=operatoragents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=operatoragents/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=serviceaccounts;persistentvolumeclaims;configmaps;services,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=nodes;pods;namespaces;events;persistentvolumes,verbs=get;list;watch

func (r *OperatorAgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	instance := &agentv1alpha1.OperatorAgent{}
	if err := r.Get(ctx, req.NamespacedName, instance); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	log.Info("Reconciling OperatorAgent", "name", instance.Name, "namespace", instance.Namespace)

	// 1. Intercept Deletion
	if !instance.ObjectMeta.DeletionTimestamp.IsZero() {
		return r.handleDeletion(ctx, instance)
	}

	// 2. Add Finalizer if not present
	if !controllerutil.ContainsFinalizer(instance, operatorAgentFinalizer) {
		controllerutil.AddFinalizer(instance, operatorAgentFinalizer)
		if err := r.Update(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{}, nil
	}

	// 3. Reconcile K8s Service Account
	if err := r.reconcileServiceAccount(ctx, instance); err != nil {
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

	// Reconcile Fluent Bit ConfigMap
	fluentBitHash, err := r.reconcileFluentBitConfigMap(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// 7. Reconcile Deployment
	if err := r.reconcileDeployment(ctx, instance, configMapHash, fluentBitHash); err != nil {
		return ctrl.Result{}, err
	}

	// Reconcile Service
	if err := r.reconcileService(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 8. Update status phase to Ready
	return ctrl.Result{}, r.updateStatusReady(ctx, instance)
}

func (r *OperatorAgentReconciler) handleDeletion(ctx context.Context, agent *agentv1alpha1.OperatorAgent) (ctrl.Result, error) {
	if controllerutil.ContainsFinalizer(agent, operatorAgentFinalizer) {
		// Resource is deleted. Safe to remove finalizer and update.
		controllerutil.RemoveFinalizer(agent, operatorAgentFinalizer)
		if err := r.Update(ctx, agent); err != nil {
			return ctrl.Result{}, err
		}
	}
	return ctrl.Result{}, nil
}

func (r *OperatorAgentReconciler) reconcileServiceAccount(ctx context.Context, agent *agentv1alpha1.OperatorAgent) error {
	sa := buildOperatorServiceAccount(agent)
	if err := ctrl.SetControllerReference(agent, sa, r.Scheme); err != nil {
		return err
	}
	return r.Patch(ctx, sa, client.Apply, client.ForceOwnership, client.FieldOwner("operatoragent-controller"))
}

func (r *OperatorAgentReconciler) reconcilePVC(ctx context.Context, agent *agentv1alpha1.OperatorAgent) error {
	pvc := buildOperatorPVC(agent)
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

func (r *OperatorAgentReconciler) reconcileConfigMap(ctx context.Context, agent *agentv1alpha1.OperatorAgent) (string, error) {
	cm := buildOperatorConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.Patch(ctx, cm, client.Apply, client.ForceOwnership, client.FieldOwner("operatoragent-controller"))
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

func (r *OperatorAgentReconciler) reconcileFluentBitConfigMap(ctx context.Context, agent *agentv1alpha1.OperatorAgent) (string, error) {
	cm := buildOperatorFluentBitConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.Patch(ctx, cm, client.Apply, client.ForceOwnership, client.FieldOwner("operatoragent-controller"))
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

func (r *OperatorAgentReconciler) reconcileDeployment(ctx context.Context, agent *agentv1alpha1.OperatorAgent, configHash, fluentBitHash string) error {
	dep := buildOperatorDeployment(agent, configHash, fluentBitHash)
	if err := ctrl.SetControllerReference(agent, dep, r.Scheme); err != nil {
		return err
	}
	return r.Patch(ctx, dep, client.Apply, client.ForceOwnership, client.FieldOwner("operatoragent-controller"))
}

func (r *OperatorAgentReconciler) reconcileService(ctx context.Context, agent *agentv1alpha1.OperatorAgent) error {
	svc := buildService(agent)
	if err := ctrl.SetControllerReference(agent, svc, r.Scheme); err != nil {
		return err
	}
	return r.Patch(ctx, svc, client.Apply, client.ForceOwnership, client.FieldOwner("operatoragent-controller"))
}



func (r *OperatorAgentReconciler) updateStatusReady(ctx context.Context, agent *agentv1alpha1.OperatorAgent) error {
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

	svc := &corev1.Service{}
	errSvc := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name}, svc)
	if errSvc == nil {
		agent.Status.ServiceStatus.Endpoint = fmt.Sprintf("http://%s.%s.svc.cluster.local:8642", svc.Name, svc.Namespace)
		agent.Status.Address = fmt.Sprintf("%s.%s.svc.cluster.local", svc.Name, svc.Namespace)
	}

	if err == nil && dep.Status.ReadyReplicas > 0 {
		agent.Status.Phase = "Ready"
	} else {
		agent.Status.Phase = "Provisioning"
	}
	now := metav1.Now()
	agent.Status.LastReconcileTime = &now

	return r.Status().Update(ctx, agent)
}

// SetupWithManager sets up the controller with the Manager.
func (r *OperatorAgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.OperatorAgent{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.Service{}).
		Named("operatoragent").
		Complete(r)
}
