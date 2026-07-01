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
	"sync"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// OperatorAgentReconciler reconciles a OperatorAgent object
type OperatorAgentReconciler struct {
	client.Client
	Scheme        *runtime.Scheme
	RemoteClients sync.Map
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

	// Reconcile remote cluster namespace/SA/roles if spec.harness.clusterName is specified
	if instance.Spec.Harness != nil && instance.Spec.Harness.ClusterName != "" {
		projectID := instance.Spec.Harness.ProjectID
		if projectID == "" {
			err := fmt.Errorf("spec.harness.projectId is required for remote cluster provisioning")
			log.Error(err, "missing project ID")
			instance.Status.Phase = "Failed"
			_ = r.Status().Update(ctx, instance)
			return ctrl.Result{}, err
		}

		location := instance.Spec.Harness.Location
		clusterName := instance.Spec.Harness.ClusterName
		remoteNamespace := instance.Namespace

		if err := r.reconcileRemoteResources(ctx, instance, projectID, location, clusterName, remoteNamespace); err != nil {
			return ctrl.Result{}, err
		}
	}

	// 4b. Reconcile Service Account (with Workload Identity annotation)
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

func (r *OperatorAgentReconciler) reconcileServiceAccount(ctx context.Context, agent *agentv1alpha1.OperatorAgent) error {
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" && len(agent.Spec.Security.ServiceAccountAnnotations) == 0 {
		return nil
	}

	saName := agent.Name
	var annotations map[string]string
	if agent.Spec.Security != nil {
		if agent.Spec.Security.ServiceAccountName != "" {
			saName = agent.Spec.Security.ServiceAccountName
		}
		annotations = agent.Spec.Security.ServiceAccountAnnotations
	}

	return ReconcileHostServiceAccount(ctx, r.Client, r.Scheme, agent, saName, agent.Namespace, annotations, "operatoragent-controller")
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
	errDep := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}, dep)
	var newPhase string
	var readyReplicas int32
	if errDep == nil {
		readyReplicas = dep.Status.ReadyReplicas
		if dep.Status.ReadyReplicas > 0 {
			newPhase = "Ready"
		} else {
			newPhase = "Provisioning"
		}
	} else {
		newPhase = "Provisioning"
	}

	pvc := &corev1.PersistentVolumeClaim{}
	errPVC := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-data"}, pvc)
	var pvcBound bool
	if errPVC == nil {
		pvcBound = (pvc.Status.Phase == corev1.ClaimBound)
	}

	svc := &corev1.Service{}
	errSvc := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name}, svc)
	var newEndpoint, newAddress string
	if errSvc == nil {
		newEndpoint = fmt.Sprintf("http://%s.%s.svc.cluster.local:8642", svc.Name, svc.Namespace)
		newAddress = fmt.Sprintf("%s.%s.svc.cluster.local", svc.Name, svc.Namespace)
	}

	// Break the infinite reconciliation loop by returning early if status has not changed
	if agent.Status.Phase == newPhase &&
		agent.Status.DeploymentStatus.Name == agent.Name+"-gateway" &&
		agent.Status.DeploymentStatus.ReadyReplicas == readyReplicas &&
		agent.Status.StorageStatus.Bound == pvcBound &&
		agent.Status.ServiceStatus.Endpoint == newEndpoint &&
		agent.Status.Address == newAddress &&
		agent.Status.LastReconcileTime != nil {
		return nil
	}

	agent.Status.DeploymentStatus.Name = agent.Name + "-gateway"
	agent.Status.DeploymentStatus.ReadyReplicas = readyReplicas
	agent.Status.StorageStatus.Bound = pvcBound
	agent.Status.ServiceStatus.Endpoint = newEndpoint
	agent.Status.Address = newAddress
	agent.Status.Phase = newPhase

	now := metav1.Now()
	agent.Status.LastReconcileTime = &now

	return r.Status().Update(ctx, agent)
}

func (r *OperatorAgentReconciler) reconcileRemoteResources(ctx context.Context, agent *agentv1alpha1.OperatorAgent, projectID, location, clusterName, namespace string) error {
	log := logf.FromContext(ctx)

	// 1. Get or build a remote client using the client cache
	key := fmt.Sprintf("%s/%s/%s", projectID, location, clusterName)
	var remoteClient client.Client
	if val, ok := r.RemoteClients.Load(key); ok {
		remoteClient = val.(client.Client)
	} else {
		var err error
		remoteClient, err = buildRemoteClientDynamically(ctx, projectID, location, clusterName)
		if err != nil {
			log.Error(err, "unable to build remote client for Cluster B", "project", projectID, "location", location, "cluster", clusterName)
			return err
		}
		r.RemoteClients.Store(key, remoteClient)
		log.Info("successfully built remote client for Cluster B", "project", projectID, "location", location, "cluster", clusterName)
	}

	// 2. Reconcile Namespace on target cluster
	if err := reconcileNamespace(ctx, remoteClient, namespace); err != nil {
		return err
	}

	// 3. Resolve remote identity subject
	remoteIdentity := ""
	if agent.Spec.Security != nil {
		remoteIdentity = agent.Spec.Security.RemoteIdentitySubject
	}

	// 4. Reconcile ClusterRole on target cluster
	rules := []rbacv1.PolicyRule{
		{
			APIGroups: []string{""},
			Resources: []string{"nodes", "namespaces", "pods", "services", "events", "persistentvolumes", "serviceaccounts"},
			Verbs:     []string{"get", "list", "watch"},
		},
		{
			APIGroups: []string{"apps"},
			Resources: []string{"deployments", "daemonsets", "statefulsets"},
			Verbs:     []string{"get", "list", "watch"},
		},
		{
			APIGroups: []string{"networking.k8s.io"},
			Resources: []string{"networkpolicies"},
			Verbs:     []string{"get", "list", "watch"},
		},
		{
			APIGroups: []string{"policy"},
			Resources: []string{"poddisruptionbudgets"},
			Verbs:     []string{"get", "list", "watch"},
		},
	}
	remoteRoleName := "operator-agent-role"
	if err := reconcileClusterRole(ctx, remoteClient, remoteRoleName, rules); err != nil {
		return err
	}

	// 5. Bind the custom ClusterRole directly to the remote identity on the remote cluster
	if remoteIdentity != "" {
		remoteGsaRBName := "operator-agent-gsa-rolebinding"
		if err := reconcileClusterRoleBindingToUser(ctx, remoteClient, remoteGsaRBName, remoteIdentity, remoteRoleName); err != nil {
			return err
		}
	}

	return nil
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
