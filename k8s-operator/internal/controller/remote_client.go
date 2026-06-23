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
	"encoding/base64"
	"fmt"
	"net/http"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
	"google.golang.org/api/container/v1"
	"google.golang.org/api/option"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// buildRemoteClientDynamically queries the GKE API to find Cluster B's IP and CA data
// and constructs a controller-runtime client using Workload Identity.
func buildRemoteClientDynamically(ctx context.Context, projectID, location, clusterName string) (client.Client, error) {
	if projectID == "" {
		return nil, fmt.Errorf("projectID cannot be empty")
	}
	if location == "" {
		return nil, fmt.Errorf("location cannot be empty")
	}
	if clusterName == "" {
		return nil, fmt.Errorf("clusterName cannot be empty")
	}

	// 1. Get GCP credentials automatically via Workload Identity.
	// Use context.Background() because the tokenSource is cached inside the client transport
	// and needs to outlive the short-lived reconciliation context.
	tokenSource, err := google.DefaultTokenSource(context.Background(), container.CloudPlatformScope)
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

	// 4. Decode the base64 CA certificate returned by the API with a defensive check
	if cluster.MasterAuth == nil || cluster.MasterAuth.ClusterCaCertificate == "" {
		return nil, fmt.Errorf("cluster master auth or CA certificate is not available")
	}
	caData, err := base64.StdEncoding.DecodeString(cluster.MasterAuth.ClusterCaCertificate)
	if err != nil {
		return nil, fmt.Errorf("failed to decode cluster CA cert: %w", err)
	}

	if cluster.Endpoint == "" {
		return nil, fmt.Errorf("cluster endpoint is not available")
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

// reconcileNamespace creates the namespace on the remote cluster if it doesn't exist.
func reconcileNamespace(ctx context.Context, c client.Client, name string) error {
	ns := &corev1.Namespace{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "Namespace",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name: name,
		},
	}
	err := c.Create(ctx, ns)
	if err != nil && !errors.IsAlreadyExists(err) {
		return fmt.Errorf("failed to create namespace %s: %w", name, err)
	}
	return nil
}

// reconcileServiceAccount creates the ServiceAccount on the remote cluster with the specified annotations.
func reconcileServiceAccount(ctx context.Context, c client.Client, name, namespace string, annotations map[string]string) error {
	sa := &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "ServiceAccount",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
		},
	}
	if annotations != nil {
		sa.Annotations = annotations
	}

	err := c.Create(ctx, sa)
	if err != nil {
		if errors.IsAlreadyExists(err) {
			existing := &corev1.ServiceAccount{}
			if err := c.Get(ctx, client.ObjectKey{Name: name, Namespace: namespace}, existing); err != nil {
				return err
			}
			existing.Annotations = annotations
			return c.Update(ctx, existing)
		}
		return fmt.Errorf("failed to create service account %s: %w", name, err)
	}
	return nil
}

// reconcileRole creates or updates the Role on the remote cluster.
func reconcileRole(ctx context.Context, c client.Client, name, namespace string, rules []rbacv1.PolicyRule) error {
	role := &rbacv1.Role{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "Role",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
		},
		Rules: rules,
	}

	err := c.Create(ctx, role)
	if err != nil {
		if errors.IsAlreadyExists(err) {
			existing := &rbacv1.Role{}
			if err := c.Get(ctx, client.ObjectKey{Name: name, Namespace: namespace}, existing); err != nil {
				return err
			}
			existing.Rules = rules
			return c.Update(ctx, existing)
		}
		return fmt.Errorf("failed to create role %s: %w", name, err)
	}
	return nil
}

// reconcileRoleBinding creates or updates the RoleBinding on the remote cluster.
func reconcileRoleBinding(ctx context.Context, c client.Client, name, namespace, saName, roleName string) error {
	rb := &rbacv1.RoleBinding{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "RoleBinding",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
		},
		Subjects: []rbacv1.Subject{
			{
				Kind:      "ServiceAccount",
				Name:      saName,
				Namespace: namespace,
			},
		},
		RoleRef: rbacv1.RoleRef{
			APIGroup: "rbac.authorization.k8s.io",
			Kind:     "Role",
			Name:     roleName,
		},
	}

	err := c.Create(ctx, rb)
	if err != nil {
		if errors.IsAlreadyExists(err) {
			existing := &rbacv1.RoleBinding{}
			if err := c.Get(ctx, client.ObjectKey{Name: name, Namespace: namespace}, existing); err != nil {
				return err
			}
			// RoleRef is immutable. If it changed, we must delete and recreate the RoleBinding.
			if existing.RoleRef != rb.RoleRef {
				if err := c.Delete(ctx, existing); err != nil {
					return fmt.Errorf("failed to delete existing role binding %s due to RoleRef change: %w", name, err)
				}
				return c.Create(ctx, rb)
			}
			existing.Subjects = rb.Subjects
			return c.Update(ctx, existing)
		}
		return fmt.Errorf("failed to create role binding %s: %w", name, err)
	}
	return nil
}

// reconcileRoleBindingToUser creates or updates a RoleBinding for a specific User on the remote cluster.
func reconcileRoleBindingToUser(ctx context.Context, c client.Client, name, namespace, userEmail, roleName string) error {
	rb := &rbacv1.RoleBinding{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "RoleBinding",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
		},
		Subjects: []rbacv1.Subject{
			{
				Kind:     "User",
				Name:     userEmail,
				APIGroup: "rbac.authorization.k8s.io",
			},
		},
		RoleRef: rbacv1.RoleRef{
			APIGroup: "rbac.authorization.k8s.io",
			Kind:     "Role",
			Name:     roleName,
		},
	}

	err := c.Create(ctx, rb)
	if err != nil {
		if errors.IsAlreadyExists(err) {
			existing := &rbacv1.RoleBinding{}
			if err := c.Get(ctx, client.ObjectKey{Name: name, Namespace: namespace}, existing); err != nil {
				return err
			}
			// RoleRef is immutable. If it changed, we must delete and recreate the RoleBinding.
			if existing.RoleRef != rb.RoleRef {
				if err := c.Delete(ctx, existing); err != nil {
					return fmt.Errorf("failed to delete existing role binding %s due to RoleRef change: %w", name, err)
				}
				return c.Create(ctx, rb)
			}
			existing.Subjects = rb.Subjects
			return c.Update(ctx, existing)
		}
		return fmt.Errorf("failed to create role binding %s: %w", name, err)
	}
	return nil
}

// reconcileClusterRole creates or updates the ClusterRole on the remote cluster.
func reconcileClusterRole(ctx context.Context, c client.Client, name string, rules []rbacv1.PolicyRule) error {
	clusterRole := &rbacv1.ClusterRole{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "ClusterRole",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name: name,
		},
		Rules: rules,
	}

	err := c.Create(ctx, clusterRole)
	if err != nil {
		if errors.IsAlreadyExists(err) {
			existing := &rbacv1.ClusterRole{}
			if err := c.Get(ctx, client.ObjectKey{Name: name}, existing); err != nil {
				return err
			}
			existing.Rules = rules
			return c.Update(ctx, existing)
		}
		return fmt.Errorf("failed to create cluster role %s: %w", name, err)
	}
	return nil
}

// reconcileClusterRoleBinding creates or updates the ClusterRoleBinding on the remote cluster.
func reconcileClusterRoleBinding(ctx context.Context, c client.Client, name, saName, saNamespace, clusterRoleName string) error {
	crb := &rbacv1.ClusterRoleBinding{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "ClusterRoleBinding",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name: name,
		},
		Subjects: []rbacv1.Subject{
			{
				Kind:      "ServiceAccount",
				Name:      saName,
				Namespace: saNamespace,
			},
		},
		RoleRef: rbacv1.RoleRef{
			APIGroup: "rbac.authorization.k8s.io",
			Kind:     "ClusterRole",
			Name:     clusterRoleName,
		},
	}

	err := c.Create(ctx, crb)
	if err != nil {
		if errors.IsAlreadyExists(err) {
			existing := &rbacv1.ClusterRoleBinding{}
			if err := c.Get(ctx, client.ObjectKey{Name: name}, existing); err != nil {
				return err
			}
			// RoleRef is immutable. If it changed, we must delete and recreate the ClusterRoleBinding.
			if existing.RoleRef != crb.RoleRef {
				if err := c.Delete(ctx, existing); err != nil {
					return fmt.Errorf("failed to delete existing cluster role binding %s due to RoleRef change: %w", name, err)
				}
				return c.Create(ctx, crb)
			}
			existing.Subjects = crb.Subjects
			return c.Update(ctx, existing)
		}
		return fmt.Errorf("failed to create cluster role binding %s: %w", name, err)
	}
	return nil
}

// reconcileClusterRoleBindingToUser creates or updates a ClusterRoleBinding on the remote cluster that binds to a User (e.g. GSA email).
func reconcileClusterRoleBindingToUser(ctx context.Context, c client.Client, name, userEmail, clusterRoleName string) error {
	crb := &rbacv1.ClusterRoleBinding{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "ClusterRoleBinding",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name: name,
		},
		Subjects: []rbacv1.Subject{
			{
				Kind:     "User",
				Name:     userEmail,
				APIGroup: "rbac.authorization.k8s.io",
			},
		},
		RoleRef: rbacv1.RoleRef{
			APIGroup: "rbac.authorization.k8s.io",
			Kind:     "ClusterRole",
			Name:     clusterRoleName,
		},
	}

	err := c.Create(ctx, crb)
	if err != nil {
		if errors.IsAlreadyExists(err) {
			existing := &rbacv1.ClusterRoleBinding{}
			if err := c.Get(ctx, client.ObjectKey{Name: name}, existing); err != nil {
				return err
			}
			// RoleRef is immutable. If it changed, we must delete and recreate the ClusterRoleBinding.
			if existing.RoleRef != crb.RoleRef {
				if err := c.Delete(ctx, existing); err != nil {
					return fmt.Errorf("failed to delete existing cluster role binding %s due to RoleRef change: %w", name, err)
				}
				return c.Create(ctx, crb)
			}
			existing.Subjects = crb.Subjects
			return c.Update(ctx, existing)
		}
		return fmt.Errorf("failed to create cluster role binding to user %s: %w", name, err)
	}
	return nil
}
