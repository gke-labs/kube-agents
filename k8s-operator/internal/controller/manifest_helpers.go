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
	"os"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

var (
	// DefaultPlatformAgentVersion is injected at build time via -ldflags "-X ...DefaultPlatformAgentVersion=vX.Y.Z"
	// or defaults to "latest" during local development.
	DefaultPlatformAgentVersion = "latest"
)

// fallbackPlatformAgentImage derives its tag from DefaultPlatformAgentVersion
// at call time (not folded at init), so release builds default to the matching
// versioned image and tests can pin the derivation by overriding the variable.
func fallbackPlatformAgentImage() string {
	return "ghcr.io/gke-labs/kube-agents/platform-agent:" + DefaultPlatformAgentVersion
}

const (
	fallbackFluentBitImage = "fluent/fluent-bit:5.0.7"

	// Operator-level image overrides for installs that mirror images into a
	// private registry. Set on the controller-manager Deployment; a CR's
	// spec.deployment.image still takes precedence over PLATFORM_AGENT_IMAGE.
	platformAgentImageEnvVar   = "PLATFORM_AGENT_IMAGE"
	credentialProxyImageEnvVar = "CREDENTIAL_PROXY_IMAGE"
	fluentBitImageEnvVar       = "FLUENT_BIT_IMAGE"

	// managedOTelEndpoint is the OTLP/HTTP endpoint of the GKE Managed OpenTelemetry
	// collector. The same endpoint is already used by the LiteLLM integration, so agent
	// traces and LLM-call telemetry land in the same place (Cloud Trace/Logging).
	managedOTelEndpoint = "http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318"

	defaultSurgePercent = "25%"

	// fieldOwner identifies this controller in Server-Side Apply managedFields.
	fieldOwner = "platformagent-controller"
)

// The Kubernetes recommended labels, stamped on every object this controller
// creates so the project's whole cluster footprint is selectable with
// -l app.kubernetes.io/part-of=kube-agents. See
// https://kubernetes.io/docs/concepts/overview/working-with-objects/common-labels/
//
// component and version are deliberately absent: there is no build-time version
// to report, and image references may carry a digest, whose '@' and ':' are not
// legal in a label value.
const (
	labelName      = "app.kubernetes.io/name"
	labelInstance  = "app.kubernetes.io/instance"
	labelPartOf    = "app.kubernetes.io/part-of"
	labelManagedBy = "app.kubernetes.io/managed-by"

	appNamePlatformAgent = "platform-agent"
	partOfKubeAgents     = "kube-agents"

	// maxLabelValueLength is the Kubernetes limit on a label value.
	maxLabelValueLength = 63
)

// instanceLabel builds the app.kubernetes.io/instance value for an agent.
//
// The namespace is included because the controller also writes cluster-scoped
// ClusterRoles and ClusterRoleBindings, where a bare CR name is ambiguous
// between two agents of the same name in different namespaces. Nothing bounds
// a PlatformAgent name to a length that fits a label value, so the result is
// truncated rather than left to be rejected by the API server.
func instanceLabel(namespace, name string) string {
	value := namespace + "-" + name
	if len(value) <= maxLabelValueLength {
		return value
	}
	value = value[:maxLabelValueLength]
	// A label value must end in an alphanumeric, which truncation can break.
	return strings.TrimRight(value, "-_.")
}

// commonLabels returns the recommended labels identifying an object as part of
// the agent installation owned by this PlatformAgent.
func commonLabels(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return map[string]string{
		labelName:      appNamePlatformAgent,
		labelInstance:  instanceLabel(agent.Namespace, agent.Name),
		labelPartOf:    partOfKubeAgents,
		labelManagedBy: fieldOwner,
	}
}

// withCommonLabels merges the recommended labels into obj, leaving any key the
// object already sets untouched — buildDeployment and buildPodTemplateSpec set
// selector-bearing labels of their own that must not be overwritten.
func withCommonLabels(obj metav1.Object, agent *agentv1alpha1.PlatformAgent) {
	labels := obj.GetLabels()
	if labels == nil {
		labels = map[string]string{}
	}
	for k, v := range commonLabels(agent) {
		if _, exists := labels[k]; !exists {
			labels[k] = v
		}
	}
	obj.SetLabels(labels)
}

// otelTelemetryEnvVars returns the OpenTelemetry configuration for an agent container: the
// service name, the GKE Managed OpenTelemetry collector endpoint, and resource attributes
// carrying the agent's identity. These defaults can be overridden per-agent via Deployment.Env
// (see mergeEnvVars).
func otelTelemetryEnvVars(agentType, name, namespace string) []corev1.EnvVar {
	return []corev1.EnvVar{
		{
			Name:  "OTEL_SERVICE_NAME",
			Value: name + "-gateway",
		},
		{
			Name:  "OTEL_EXPORTER_OTLP_ENDPOINT",
			Value: managedOTelEndpoint,
		},
		{
			Name:  "OTEL_EXPORTER_OTLP_PROTOCOL",
			Value: "http/protobuf",
		},
		{
			Name: "OTEL_RESOURCE_ATTRIBUTES",
			Value: fmt.Sprintf(
				"service.namespace=%s,k8s.namespace.name=%s,kubeagents.agent_type=%s,kubeagents.agent_name=%s",
				namespace, namespace, agentType, name,
			),
		},
	}
}

// defaultPlatformAgentImage returns the agent image used when a CR omits
// spec.deployment.image: the PLATFORM_AGENT_IMAGE env var if set, else the
// public ghcr.io default.
func defaultPlatformAgentImage() string {
	if img := os.Getenv(platformAgentImageEnvVar); img != "" {
		return img
	}
	return fallbackPlatformAgentImage()
}

// fluentBitImage returns the logging sidecar image: the FLUENT_BIT_IMAGE env
// var if set, else the public Docker Hub default.
func fluentBitImage() string {
	if img := os.Getenv(fluentBitImageEnvVar); img != "" {
		return img
	}
	return fallbackFluentBitImage
}

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
			// Deliberately "latest", not DefaultPlatformAgentVersion: this is a
			// user-supplied image, and our release version must not be stamped
			// on third-party repositories.
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
// based on HighAvailability and ScaleToZero settings in the DeploymentSpec.
func resolveDeploymentReplicasAndStrategy(deployment *agentv1alpha1.DeploymentSpec) (int32, appsv1.DeploymentStrategy) {
	replicas := int32(1)
	strategy := appsv1.DeploymentStrategy{
		Type: appsv1.RecreateDeploymentStrategyType,
	}

	if deployment != nil {
		intendedReplicas := int32(1)
		if deployment.Availability != nil && deployment.Availability.Replicas != nil {
			intendedReplicas = *deployment.Availability.Replicas
		}

		replicas = intendedReplicas
		if deployment.ScaleToZero != nil && *deployment.ScaleToZero {
			replicas = 0
		}

		if intendedReplicas > 1 {
			strategy = appsv1.DeploymentStrategy{
				Type: appsv1.RollingUpdateDeploymentStrategyType,
				RollingUpdate: &appsv1.RollingUpdateDeployment{
					MaxSurge:       &intstr.IntOrString{Type: intstr.String, StrVal: defaultSurgePercent},
					MaxUnavailable: &intstr.IntOrString{Type: intstr.String, StrVal: defaultSurgePercent},
				},
			}
		}
	}
	return replicas, strategy
}

// resolveResources returns custom container resources if specified, or default PlatformAgent resources if nil.
func resolveResources(deployment *agentv1alpha1.DeploymentSpec) corev1.ResourceRequirements {
	if deployment != nil && deployment.Resources != nil {
		return *deployment.Resources.DeepCopy()
	}

	return corev1.ResourceRequirements{
		Requests: corev1.ResourceList{
			corev1.ResourceCPU:    resource.MustParse("500m"),
			corev1.ResourceMemory: resource.MustParse("2Gi"),
		},
		Limits: corev1.ResourceList{
			corev1.ResourceCPU:    resource.MustParse("2"),
			corev1.ResourceMemory: resource.MustParse("4Gi"),
		},
	}
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
	labels map[string]string,
	owningFieldManager string,
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
	if labels != nil {
		sa.Labels = labels
	}

	if err := controllerutil.SetControllerReference(owner, sa, scheme); err != nil {
		return err
	}

	return c.Patch(ctx, sa, client.Apply, client.ForceOwnership, client.FieldOwner(owningFieldManager))
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
