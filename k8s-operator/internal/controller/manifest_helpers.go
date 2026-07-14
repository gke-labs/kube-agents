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
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	defaultPlatformAgentImage = "ghcr.io/gke-labs/kube-agents/platform-agent:latest"
)

// resolveAgentImage determines the full image reference using the optional deployment spec and a fallback default.
func resolveAgentImage(deployment *agentv1alpha1.DeploymentSpec, defaultImage string) string {
	image := defaultImage
	if deployment != nil && deployment.Image != "" {
		image = deployment.Image
		hasTagOrDigest := false
		lastSlash := strings.LastIndex(image, "/")
		refPart := image
		if lastSlash != -1 {
			refPart = image[lastSlash+1:]
		}
		if strings.Contains(refPart, ":") || strings.Contains(refPart, "@") {
			hasTagOrDigest = true
		}

		if !hasTagOrDigest {
			tag := "latest"
			if deployment.Tag != nil && *deployment.Tag != "" {
				tag = *deployment.Tag
			}
			image = fmt.Sprintf("%s:%s", image, tag)
		}
	}
	return image
}

// mergeEnvVars merges custom env vars into defaults. Custom env vars override defaults with the same name.
func mergeEnvVars(defaults []corev1.EnvVar, custom []corev1.EnvVar) []corev1.EnvVar {
	if len(custom) == 0 {
		return defaults
	}
	if len(defaults) == 0 {
		return custom
	}

	customMap := make(map[string]corev1.EnvVar, len(custom))
	for _, env := range custom {
		customMap[env.Name] = env
	}

	merged := make([]corev1.EnvVar, 0, len(defaults)+len(custom))
	for _, env := range defaults {
		if customEnv, exists := customMap[env.Name]; exists {
			merged = append(merged, customEnv)
			delete(customMap, env.Name)
		} else {
			merged = append(merged, env)
		}
	}

	// Append remaining custom env vars in their original order
	for _, env := range custom {
		if customEnv, exists := customMap[env.Name]; exists {
			merged = append(merged, customEnv)
			delete(customMap, env.Name)
		}
	}

	return merged
}

// mergeAnnotations merges custom annotations into defaults. Custom annotations override defaults with the same key.
func mergeAnnotations(defaults map[string]string, custom map[string]string) map[string]string {
	if len(defaults) == 0 && len(custom) == 0 {
		return nil
	}
	merged := make(map[string]string, len(defaults)+len(custom))
	for k, v := range defaults {
		merged[k] = v
	}
	for k, v := range custom {
		merged[k] = v
	}
	return merged
}

// resolveDeploymentReplicasAndStrategy determines the replica count and deployment strategy
// based on ScaleToZero settings in the DeploymentSpec.
func resolveDeploymentReplicasAndStrategy(deployment *agentv1alpha1.DeploymentSpec) (int32, appsv1.DeploymentStrategy) {
	replicas := int32(1)
	strategy := appsv1.DeploymentStrategy{
		Type: appsv1.RecreateDeploymentStrategyType,
	}

	if deployment != nil {
		if deployment.ScaleToZero != nil && *deployment.ScaleToZero {
			replicas = int32(0)
		}
	}
	return replicas, strategy
}

// ReconcileServiceAccount is a shared helper to reconcile a ServiceAccount on the host cluster
// with Server-Side Apply and OwnerReference.
func ReconcileServiceAccount(
	ctx context.Context,
	c client.Client,
	scheme *runtime.Scheme,
	owner client.Object,
	name,
	namespace string,
	annotations map[string]string,
	fieldOwner string,
) error {
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

	if err := controllerutil.SetControllerReference(owner, sa, scheme); err != nil {
		return err
	}

	return c.Patch(ctx, sa, client.Apply, client.ForceOwnership, client.FieldOwner(fieldOwner))
}

// defaultSecretRef returns ref if provided, otherwise defaults to secretName with defaultKey.
func defaultSecretRef(ref *corev1.SecretKeySelector, secretName, defaultKey string) *corev1.SecretKeySelector {
	if ref != nil {
		return ref
	}
	return &corev1.SecretKeySelector{
		LocalObjectReference: corev1.LocalObjectReference{Name: secretName},
		Key:                  defaultKey,
		Optional:             ptr.To(true),
	}
}
