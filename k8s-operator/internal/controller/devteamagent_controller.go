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

	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	devteamv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"

	"encoding/base64"
	"fmt"
	"net/http"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
	"google.golang.org/api/container/v1"
	"google.golang.org/api/option"
	"k8s.io/client-go/rest"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// DevTeamAgentReconciler reconciles a DevTeamAgent object
type DevTeamAgentReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=devteamagents,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=devteamagents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=devteamagents/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=persistentvolumeclaims;services;serviceaccounts,verbs=get;list;watch;create;update;patch;delete

// Reconcile is part of the main kubernetes reconciliation loop which aims to
// move the current state of the cluster closer to the desired state.
func (r *DevTeamAgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	// 1. Fetch the DevTeamAgent instance
	instance := &devteamv1alpha1.DevTeamAgent{}
	err := r.Get(ctx, req.NamespacedName, instance)
	if err != nil {
		if errors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	log.Info("Reconciling DevTeamAgent", "name", instance.Name)

	targetClusterName := "autopilot-cluster-1"
	targetClusterLocation := "us-central1"
	targetProjectId := "ai-platform-1-464114"
	targetNamespace := "test-namespace"

	// saName := "operator-agent"
	// roleName := "operator-agent-readonly"

	if err := r.reconcileRemoteNamespace(ctx, targetProjectId, targetClusterLocation, targetClusterName, targetNamespace); err != nil {
		return ctrl.Result{}, err
	}

	// Update status phase to Ready
	if instance.Status.Phase != "Ready" {
		instance.Status.Phase = "Ready"
		if err := r.Status().Update(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
		log.Info("DevTeamAgent is Ready", "name", instance.Name)
	}

	return ctrl.Result{}, nil
}

// buildRemoteClientDynamically queries the GKE API to find Cluster B's IP and CA data
// and constructs a controller-runtime client using Workload Identity.
func buildRemoteClientDynamically(ctx context.Context, projectID, location, clusterName string) (client.Client, error) {
	// 1. Get GCP credentials automatically via Workload Identity
	tokenSource, err := google.DefaultTokenSource(ctx, container.CloudPlatformScope)
	if err != nil {
		return nil, fmt.Errorf("failed to get default token source: %w", err)
	}

	// 2. Instantiate the GKE Container Service client
	containerSvc, err := container.NewService(ctx, option.WithTokenSource(tokenSource))
	if err != nil {
		return nil, fmt.Errorf("failed to create GKE container service: %w", err)
	}

	// 3. Fetch Cluster B details from the GCP API
	clusterPath := fmt.Sprintf("projects/%s/locations/%s/clusters/%s", projectID, location, clusterName)
	cluster, err := containerSvc.Projects.Locations.Clusters.Get(clusterPath).Context(ctx).Do()
	if err != nil {
		return nil, fmt.Errorf("failed to fetch cluster %s: %w", clusterName, err)
	}

	// 4. Decode the base64 CA certificate returned by the API
	caData, err := base64.StdEncoding.DecodeString(cluster.MasterAuth.ClusterCaCertificate)
	if err != nil {
		return nil, fmt.Errorf("failed to decode cluster CA cert: %w", err)
	}

	// 5. Build the REST config for Cluster B using the discovered IP (Endpoint)
	remoteConfig := &rest.Config{
		Host: fmt.Sprintf("https://%s", cluster.Endpoint),
		TLSClientConfig: rest.TLSClientConfig{
			CAData: caData,
		},
		// Inject the OAuth2 token into every Kubernetes API request
		WrapTransport: func(rt http.RoundTripper) http.RoundTripper {
			return &oauth2.Transport{
				Source: tokenSource,
				Base:   rt,
			}
		},
	}

	// 6. Return the ready-to-use controller-runtime client
	return client.New(remoteConfig, client.Options{})
}

// SetupWithManager sets up the controller with the Manager.
func (r *DevTeamAgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&devteamv1alpha1.DevTeamAgent{}).
		Named("devteamagent").
		Complete(r)
}

// reconcileRemoteNamespace builds a remote client and reconciles a Namespace on Cluster B
func (r *DevTeamAgentReconciler) reconcileRemoteNamespace(ctx context.Context, projectID, location, clusterName, namespace string) error {
	log := logf.FromContext(ctx)

	// 1. Build a remote client using Workload Identity
	remoteClient, err := buildRemoteClientDynamically(
		ctx,
		projectID,
		location,
		clusterName,
	)
	if err != nil {
		log.Error(err, "unable to build remote client for Cluster B")
		return err
	}

	log.Info("successfully built remote client for Cluster B", "client", remoteClient)

	// 2. Create a Namespace on the target Cluster
	if err := reconcileNamespace(ctx, remoteClient, namespace); err != nil {
		return err
	}

	// 3. Create a ServiceAccount on the target Cluster
	if err := reconcileServiceAccount(ctx, remoteClient, namespace); err != nil {
		return err
	}

	// 4. Create a Role on the target Cluster
	if err := reconcileRole(ctx, remoteClient, namespace); err != nil {
		return err
	}

	// 5. Create a RoleBinding on the target Cluster
	if err := reconcileRoleBinding(ctx, remoteClient, namespace); err != nil {
		return err
	}

	return nil
}

// reconcileNamespace creates or updates a Namespace on the target Cluster
func reconcileNamespace(ctx context.Context, remoteClient client.Client, namespace string) error {
	log := logf.FromContext(ctx)
	ns := &corev1.Namespace{
		ObjectMeta: metav1.ObjectMeta{
			Name: namespace,
		},
	}

	op, err := controllerutil.CreateOrUpdate(ctx, remoteClient, ns, func() error {
		if ns.Labels == nil {
			ns.Labels = map[string]string{}
		}
		ns.Labels["managed-by"] = "cross-cluster-controller"
		return nil
	})
	if err != nil {
		return fmt.Errorf("failed to reconcile Namespace: %w", err)
	}
	log.Info("Reconciled Namespace", "operation", op, "name", namespace)
	return nil
}

// reconcileServiceAccount creates or updates a ServiceAccount on the target Cluster
func reconcileServiceAccount(ctx context.Context, remoteClient client.Client, namespace string) error {
	log := logf.FromContext(ctx)
	sa := &corev1.ServiceAccount{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "devteam-agent-sa",
			Namespace: namespace,
		},
	}
	opSA, err := controllerutil.CreateOrUpdate(ctx, remoteClient, sa, func() error {
		return nil
	})
	if err != nil {
		return fmt.Errorf("failed to reconcile ServiceAccount: %w", err)
	}
	log.Info("Reconciled ServiceAccount", "operation", opSA, "name", sa.Name)
	return nil
}

// reconcileRole creates or updates a Role on the target Cluster
func reconcileRole(ctx context.Context, remoteClient client.Client, namespace string) error {
	log := logf.FromContext(ctx)
	role := &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "devteam-agent-read-role",
			Namespace: namespace,
		},
	}
	opRole, err := controllerutil.CreateOrUpdate(ctx, remoteClient, role, func() error {
		role.Rules = []rbacv1.PolicyRule{
			{
				APIGroups: []string{"", "apps"},
				Resources: []string{
					"deployments",
					"services",
					"configmaps",
					"pods",
					"pods/log",
					"events",
					"endpoints",
				},
				Verbs: []string{"get", "list", "watch"},
			},
			{
				APIGroups: []string{"networking.k8s.io"},
				Resources: []string{"networkpolicies"},
				Verbs:     []string{"get", "list", "watch"},
			},
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("failed to reconcile Role: %w", err)
	}
	log.Info("Reconciled Role", "operation", opRole, "name", role.Name)
	return nil
}

// reconcileRoleBinding creates or updates a RoleBinding on the target Cluster
func reconcileRoleBinding(ctx context.Context, remoteClient client.Client, namespace string) error {
	log := logf.FromContext(ctx)
	rb := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "devteam-agent-read-role-binding",
			Namespace: namespace,
		},
	}
	opRB, err := controllerutil.CreateOrUpdate(ctx, remoteClient, rb, func() error {
		rb.Subjects = []rbacv1.Subject{
			{
				Kind:      "ServiceAccount",
				Name:      "devteam-agent-sa",
				Namespace: namespace,
			},
		}
		rb.RoleRef = rbacv1.RoleRef{
			Kind:     "Role",
			Name:     "devteam-agent-read-role",
			APIGroup: "rbac.authorization.k8s.io",
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("failed to reconcile RoleBinding: %w", err)
	}
	log.Info("Reconciled RoleBinding", "operation", opRB, "name", rb.Name)
	return nil
}
