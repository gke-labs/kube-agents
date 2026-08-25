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
	// DefaultPlatformAgentVersion is the fallback tag for local development
	// or environments where OPERATOR_IMAGE is not set (defaults to "latest").
	DefaultPlatformAgentVersion = "latest"
)

// fallbackPlatformAgentImage returns the default platform-agent image using
// DefaultPlatformAgentVersion at call time (not folded at init), serving as the
// static fallback when neither PLATFORM_AGENT_IMAGE nor OPERATOR_IMAGE is configured.
// Tests can pin the derivation by overriding DefaultPlatformAgentVersion.
func fallbackPlatformAgentImage() string {
	return "ghcr.io/gke-labs/kube-agents/platform-agent:" + DefaultPlatformAgentVersion
}

const (
	fallbackFluentBitImage = "fluent/fluent-bit:5.1.0"

	// Operator-level image overrides for installs that mirror images into a
	// private registry. Set on the controller-manager Deployment; a CR's
	// spec.deployment.image still takes precedence over PLATFORM_AGENT_IMAGE.
	platformAgentImageEnvVar   = "PLATFORM_AGENT_IMAGE"
	operatorImageEnvVar        = "OPERATOR_IMAGE"
	credentialProxyImageEnvVar = "CREDENTIAL_PROXY_IMAGE" // #nosec G101 -- Environment variable name, not hardcoded credentials
	fluentBitImageEnvVar       = "FLUENT_BIT_IMAGE"

	// The fleet-wide pull identity for those mirrors, as a comma-separated list
	// of Secret names in the agent's namespace. Set on the controller-manager
	// Deployment like the three above; a CR's spec.deployment.imagePullSecrets
	// replaces it outright.
	imagePullSecretsEnvVar = "IMAGE_PULL_SECRETS" // #nosec G101 -- Environment variable name, not hardcoded credentials

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
// service name, the collector endpoint, and resource attributes carrying the agent's
// identity. These defaults can be overridden per-agent via Deployment.Env (see mergeEnvVars).
//
// endpoint is the value the controller resolved (see resolveOTLPEndpoint); an empty one
// means the GKE Managed OpenTelemetry collector. Defaulting here rather than at the call
// sites keeps this function total, so no caller can emit an empty
// OTEL_EXPORTER_OTLP_ENDPOINT, and keeps the manifest builders pure — they take no client
// and cannot discover anything themselves.
//
// disabled says the controller resolved otlpSourceNone: discovery probed, this cluster
// has no collector, and nothing configured one. Then no endpoint is emitted at all and
// the SDK is switched off instead.
//
// What this silences is the stock OpenTelemetry SDK's *metric* exporter, which reads
// OTEL_EXPORTER_OTLP_ENDPOINT directly and is what actually floods the log: on a
// collector-less cluster it POSTs /v1/metrics to a name that never resolves, once per
// export interval, for the life of the pod. Omitting the endpoint on its own would not
// stop it — with the variable unset the SDK falls back to its own default of
// http://localhost:4318 and trades the DNS failure for a refused connection at the same
// rate — so the variable has to be set, not merely skipped. OTEL_SDK_DISABLED=true is
// also already the off switch charts/kube-agents/README.md tells operators to set by hand
// for exactly this cluster shape; this makes it the default there.
//
// It does NOT reach the hermes_otel plugin, which is where agent *spans* go. That
// plugin does not read OTEL_EXPORTER_OTLP_ENDPOINT at all — its backend is baked into the
// image and rewritten at start-up by deploy/shared/otel_config.py, which leaves the baked
// value alone when the endpoint is empty. So on this path the plugin keeps pointing at
// the managed collector. That is latent rather than noisy (the plugin exports only when
// there are spans, and it logs no retry storm), but it is not fixed here.
//
// It stays overridable: mergeEnvVars applies spec.deployment.env last, so an operator who
// wants the exporter pointed somewhere regardless can set either variable themselves.
func otelTelemetryEnvVars(agentType, name, namespace, endpoint string, disabled bool) []corev1.EnvVar {
	envs := []corev1.EnvVar{
		{
			Name:  "OTEL_SERVICE_NAME",
			Value: name + "-gateway",
		},
	}

	if disabled {
		envs = append(envs, corev1.EnvVar{
			Name:  "OTEL_SDK_DISABLED",
			Value: "true",
		})
	} else {
		if endpoint == "" {
			endpoint = managedOTelEndpoint
		}
		envs = append(envs,
			corev1.EnvVar{
				Name:  "OTEL_EXPORTER_OTLP_ENDPOINT",
				Value: endpoint,
			},
			corev1.EnvVar{
				Name:  "OTEL_EXPORTER_OTLP_PROTOCOL",
				Value: "http/protobuf",
			},
		)
	}

	// Identity, not export configuration: kept in both cases. OTEL_SERVICE_NAME in
	// particular is what docker-entrypoint passes to otel_config.py as --service-name.
	return append(envs, corev1.EnvVar{
		Name: "OTEL_RESOURCE_ATTRIBUTES",
		Value: fmt.Sprintf(
			"service.namespace=%s,k8s.namespace.name=%s,kubeagents.agent_type=%s,kubeagents.agent_name=%s",
			namespace, namespace, agentType, name,
		),
	})
}

// deriveAgentImageFromOperator derives the platform-agent image from an operator image reference.
// It maps the operator image to the platform-agent image while preserving the registry prefix
// and tag. If the operator image is digest-pinned (@sha256:...), the digest cannot name the
// platform-agent manifest, so it falls back to a tag if present before the digest (e.g. :v1@sha256:...)
// or :latest.
// E.g.:
//   "ghcr.io/gke-labs/kube-agents/k8s-operator:0.2.0"                -> "ghcr.io/gke-labs/kube-agents/platform-agent:0.2.0"
//   "ghcr.io/gke-labs/kube-agents/k8s-operator:rc_2608201147_1c06e1a" -> "ghcr.io/gke-labs/kube-agents/platform-agent:rc_2608201147_1c06e1a"
//   "ghcr.io/gke-labs/kube-agents/k8s-operator@sha256:111111..."    -> "ghcr.io/gke-labs/kube-agents/platform-agent:latest"
//   "mirror.corp.internal:5000/kube-agents/k8s-operator:0.2.0"       -> "mirror.corp.internal:5000/kube-agents/platform-agent:0.2.0"
//   "k8s-operator:1c06e1ab71fdeea55e6100e61c0394206188a5ba"          -> "platform-agent:1c06e1ab71fdeea55e6100e61c0394206188a5ba"
func deriveAgentImageFromOperator(operatorImage string) string {
	lastSlash := strings.LastIndex(operatorImage, "/")
	prefix := ""
	refPart := operatorImage
	if lastSlash >= 0 {
		prefix = operatorImage[:lastSlash+1]
		refPart = operatorImage[lastSlash+1:]
	}
	// Digest pins (@sha256:...) cannot name a different repository manifest.
	// Strip digest and fall back to tag or :latest.
	if digestIdx := strings.Index(refPart, "@"); digestIdx >= 0 {
		refPart = refPart[:digestIdx]
	}
	suffix := ":latest"
	if tagIdx := strings.Index(refPart, ":"); tagIdx >= 0 {
		suffix = refPart[tagIdx:]
	}
	return prefix + appNamePlatformAgent + suffix
}

// defaultPlatformAgentImage returns the agent image used when a CR omits
// spec.deployment.image: the PLATFORM_AGENT_IMAGE env var if set, else derived
// from OPERATOR_IMAGE if set, else the public ghcr.io default.
func defaultPlatformAgentImage() string {
	if img := os.Getenv(platformAgentImageEnvVar); img != "" {
		return img
	}
	if opImg := os.Getenv(operatorImageEnvVar); opImg != "" {
		return deriveAgentImageFromOperator(opImg)
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
//
// qualify_image_ref() in k8s-operator/scripts/common.sh is the provisioning-time
// twin of this rule and must agree on how a reference is split. The no-tag
// fallback deliberately differs: this path is serving a live CR and settles for
// "latest", while the shell helper can still abort the run and does.
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

// normalizeImagePullSecrets trims each name, drops the blanks, and collapses
// repeats, returning nil when nothing survives.
//
// Both halves are load-bearing, and nothing below this layer does either. A
// blank or space-padded name sends the kubelet looking for a Secret that
// cannot exist; it gives up and pulls anonymously, so the agent lands in
// ImagePullBackOff against a spec that looks like it configured a pull
// identity. Core PodSpec validation does not reliably stop that — measured
// against GKE 1.35.6, `name: ""` is a warning rather than an error and
// `name: " regcred "` passes clean. A repeat is worse, because it fails
// somewhere else entirely: PodSpec.imagePullSecrets is a server-side-apply
// list-map keyed on name, so two identical entries make every apply of the
// generated Deployment fail with `duplicate entries for key`, several layers
// from whatever wrote them.
//
// Both inputs make these ordinary typos. IMAGE_PULL_SECRETS is hand-written
// onto a Deployment or joined by a Helm template, where "a,,b" and a trailing
// comma cost nothing to produce; the CR's list is hand-written YAML that the
// admission webhook rejects for exactly these two shapes — but the chart
// leaves that webhook off by default, so this is the layer that has to hold.
func normalizeImagePullSecrets(refs []corev1.LocalObjectReference) []corev1.LocalObjectReference {
	var secrets []corev1.LocalObjectReference
	seen := make(map[string]struct{}, len(refs))
	for _, ref := range refs {
		name := strings.TrimSpace(ref.Name)
		if name == "" {
			continue
		}
		if _, dup := seen[name]; dup {
			continue
		}
		seen[name] = struct{}{}
		secrets = append(secrets, corev1.LocalObjectReference{Name: name})
	}
	return secrets
}

// defaultImagePullSecrets returns the pull identity for agents whose CR omits
// spec.deployment.imagePullSecrets: the comma-separated IMAGE_PULL_SECRETS env
// var, or nil.
func defaultImagePullSecrets() []corev1.LocalObjectReference {
	raw := os.Getenv(imagePullSecretsEnvVar)
	if raw == "" {
		return nil
	}

	parts := strings.Split(raw, ",")
	refs := make([]corev1.LocalObjectReference, 0, len(parts))
	for _, name := range parts {
		refs = append(refs, corev1.LocalObjectReference{Name: name})
	}
	return normalizeImagePullSecrets(refs)
}

// resolveImagePullSecrets picks the pod's pull identity: the CR's list when it
// has one, otherwise the operator-wide default.
//
// Replacement, not merge — the same precedence resolveAgentImage gives
// spec.deployment.image over PLATFORM_AGENT_IMAGE, and for the same reason. The
// field is documented that way on DeploymentSpec.ImagePullSecrets.
//
// A list that normalizes away to nothing counts as unset and falls through to
// the default, because a CR carrying only `- name: ""` has not named an
// identity — it has a typo, and the fleet default is likelier to pull than
// nothing at all.
func resolveImagePullSecrets(deployment *agentv1alpha1.DeploymentSpec) []corev1.LocalObjectReference {
	if deployment != nil {
		if secrets := normalizeImagePullSecrets(deployment.ImagePullSecrets); len(secrets) > 0 {
			return secrets
		}
	}
	return defaultImagePullSecrets()
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

	// Headroom for kanban fan-out, not a latency fix for a single card. Every
	// dispatched card is a fresh `hermes chat` subprocess that boots its own MCP
	// servers (four on the platform profile, two of them node), and the pod runs
	// under gVisor, where process spawn is far more expensive than on runc. A
	// five-way fan-out was observed degrading to 57-63s per card against a solo
	// 17-23s — but that was never traced to the CPU limit, and a solo card on
	// the 2-CPU ceiling recorded 0 throttled periods out of 35,069, so do not
	// expect this to speed anything up on its own.
	//
	// The CPU limit is 3, not 4: nodes in the reference gVisor pool advertise
	// 3920m allocatable, so a 4-core limit is a ceiling the container can never
	// reach and reads as more headroom than exists. The 1-core request stops the
	// container being scheduled with shares it cannot work with.
	//
	// Memory goes to 8Gi because the pod's idle working set already measures
	// 1.80GiB sandbox-wide; the previous 4Gi limit left roughly 2.2GiB to cover
	// five concurrent workers, each carrying a Python and two node runtimes.
	// Requests stay at 2Gi, so scheduling is unchanged either way.
	return corev1.ResourceRequirements{
		Requests: corev1.ResourceList{
			corev1.ResourceCPU:    resource.MustParse("1"),
			corev1.ResourceMemory: resource.MustParse("2Gi"),
		},
		Limits: corev1.ResourceList{
			corev1.ResourceCPU:    resource.MustParse("3"),
			corev1.ResourceMemory: resource.MustParse("8Gi"),
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
