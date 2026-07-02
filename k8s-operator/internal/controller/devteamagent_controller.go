//go:build ignore

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

// DevTeamAgentReconciler reconciles a DevTeamAgent object
type DevTeamAgentReconciler struct {
	client.Client
	Scheme        *runtime.Scheme
	RemoteClients sync.Map
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=devteamagents,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=devteamagents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=devteamagents/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=serviceaccounts;persistentvolumeclaims;configmaps;services,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=nodes;pods;namespaces;events;persistentvolumes,verbs=get;list;watch

func (r *DevTeamAgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	instance := &agentv1alpha1.DevTeamAgent{}
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
		remoteNamespace := instance.Spec.Harness.Namespace
		if remoteNamespace == "" {
			remoteNamespace = instance.Namespace
		}

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

	// 6. Reconcile ConfigMap (config.yaml and SETTINGS.md content)
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

func (r *DevTeamAgentReconciler) reconcileServiceAccount(ctx context.Context, agent *agentv1alpha1.DevTeamAgent) error {
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

	return ReconcileHostServiceAccount(ctx, r.Client, r.Scheme, agent, saName, agent.Namespace, annotations, "devteamagent-controller")
}

func (r *DevTeamAgentReconciler) reconcilePVC(ctx context.Context, agent *agentv1alpha1.DevTeamAgent) error {
	pvc := buildDevTeamPVC(agent)
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

func (r *DevTeamAgentReconciler) reconcileConfigMap(ctx context.Context, agent *agentv1alpha1.DevTeamAgent) (string, error) {
	cm := buildDevTeamConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.Patch(ctx, cm, client.Apply, client.ForceOwnership, client.FieldOwner("devteamagent-controller"))
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

func (r *DevTeamAgentReconciler) reconcileFluentBitConfigMap(ctx context.Context, agent *agentv1alpha1.DevTeamAgent) (string, error) {
	cm := buildDevTeamFluentBitConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.Patch(ctx, cm, client.Apply, client.ForceOwnership, client.FieldOwner("devteamagent-controller"))
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

func (r *DevTeamAgentReconciler) reconcileDeployment(ctx context.Context, agent *agentv1alpha1.DevTeamAgent, configHash, fluentBitHash string) error {
	dep := buildDevTeamDeployment(agent, configHash, fluentBitHash)
	if err := ctrl.SetControllerReference(agent, dep, r.Scheme); err != nil {
		return err
	}
	return r.Patch(ctx, dep, client.Apply, client.ForceOwnership, client.FieldOwner("devteamagent-controller"))
}

func (r *DevTeamAgentReconciler) reconcileService(ctx context.Context, agent *agentv1alpha1.DevTeamAgent) error {
	svc := buildDevTeamService(agent)
	if err := ctrl.SetControllerReference(agent, svc, r.Scheme); err != nil {
		return err
	}
	return r.Patch(ctx, svc, client.Apply, client.ForceOwnership, client.FieldOwner("devteamagent-controller"))
}

func (r *DevTeamAgentReconciler) updateStatusReady(ctx context.Context, agent *agentv1alpha1.DevTeamAgent) error {
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

func (r *DevTeamAgentReconciler) reconcileRemoteResources(ctx context.Context, agent *agentv1alpha1.DevTeamAgent, projectID, location, clusterName, namespace string) error {
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

	// 4. Reconcile Role on target cluster
	rules := []rbacv1.PolicyRule{
		{
			APIGroups: []string{"", "apps"},
			Resources: []string{"deployments", "services", "configmaps", "pods", "pods/log", "events", "endpoints"},
			Verbs:     []string{"get", "list", "watch"},
		},
		{
			APIGroups: []string{"networking.k8s.io"},
			Resources: []string{"networkpolicies"},
			Verbs:     []string{"get", "list", "watch"},
		},
	}
	remoteRoleName := "devteam-agent-role"
	if err := reconcileRole(ctx, remoteClient, remoteRoleName, namespace, rules); err != nil {
		return err
	}

	// 5. Bind the custom Role directly to the remote identity on the remote cluster
	if remoteIdentity != "" {
		remoteGsaRBName := "devteam-agent-gsa-rolebinding"
		if err := reconcileRoleBindingToUser(ctx, remoteClient, remoteGsaRBName, namespace, remoteIdentity, remoteRoleName); err != nil {
			return err
		}
	}

	return nil
}

// SetupWithManager sets up the controller with the Manager.
func (r *DevTeamAgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.DevTeamAgent{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.Service{}).
		Named("devteamagent").
		Complete(r)
}
