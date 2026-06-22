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
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// buildConfigMap generates the ConfigMap manifest containing config.yaml
func buildConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "ConfigMap",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-config",
			Namespace: agent.Namespace,
		},
		Data: map[string]string{
			"config.yaml": renderConfigYAML(agent),
		},
	}
}

// renderConfigYAML generates the YAML payload for the agent config
func renderConfigYAML(agent *agentv1alpha1.PlatformAgent) string {
	cwd := "/opt/data"
	if agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.AgentHome != "" {
		cwd = agent.Spec.Harness.Hermes.AgentHome
	}

	cfg := struct {
		Model struct {
			Default  string `json:"default"`
			Provider string `json:"provider"`
			Model    string `json:"model,omitempty"`
			BaseURL  string `json:"base_url,omitempty"`
			APIKey   string `json:"api_key,omitempty"`
		} `json:"model"`
		Terminal struct {
			Backend string `json:"backend"`
			Cwd     string `json:"cwd"`
		} `json:"terminal"`
		Platforms struct {
			GoogleChat struct {
				Enabled bool `json:"enabled"`
			} `json:"google_chat"`
		} `json:"platforms"`
	}{}

	cfg.Model.Provider = "custom"
	cfg.Model.Default = "model-default"
	cfg.Model.Model = "model-default"
	cfg.Model.BaseURL = fmt.Sprintf("http://litellm.%s.svc.cluster.local/v1", agent.Namespace)
	cfg.Model.APIKey = "none"
	cfg.Terminal.Backend = "local"
	cfg.Terminal.Cwd = cwd

	if agent.Spec.Integration != nil && agent.Spec.Integration.GoogleChat != nil {
		gchat := agent.Spec.Integration.GoogleChat
		if gchat.Enabled != nil {
			cfg.Platforms.GoogleChat.Enabled = *gchat.Enabled
		}
	}

	data, err := yaml.Marshal(cfg)
	if err != nil {
		return ""
	}
	return string(data)
}

// buildPVC generates the PVC manifest for agent data persistence
func buildPVC(agent *agentv1alpha1.PlatformAgent) *corev1.PersistentVolumeClaim {
	return &corev1.PersistentVolumeClaim{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "PersistentVolumeClaim",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-data",
			Namespace: agent.Namespace,
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceStorage: resource.MustParse("10Gi"),
				},
			},
		},
	}
}

// buildDeployment generates the Deployment manifest for the agent payload
func buildDeployment(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash string) *appsv1.Deployment {
	replicas := int32(1)
	fsGroup := int64(10000)

	saName := agent.Name
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		saName = agent.Spec.Security.ServiceAccountName
	}

	image := resolveAgentImage(agent.Spec.Deployment, defaultPlatformAgentImage)

	pullPolicy := corev1.PullAlways
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.ImagePullPolicy != nil {
		pullPolicy = *agent.Spec.Deployment.ImagePullPolicy
	}

	homeDir := "/opt/data"
	if agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.AgentHome != "" {
		homeDir = agent.Spec.Harness.Hermes.AgentHome
	}

	dashboardVal := "0"
	if agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.DashboardEnabled != nil {
		if *agent.Spec.Harness.Hermes.DashboardEnabled {
			dashboardVal = "1"
		}
	}

	pluginsDebugVal := "0"
	if agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.PluginsDebug != nil {
		if *agent.Spec.Harness.Hermes.PluginsDebug {
			pluginsDebugVal = "1"
		}
	}

	envVars := []corev1.EnvVar{
		{
			Name:  "PLATFORM_AGENT_HOME",
			Value: homeDir,
		},
		{
			Name:  "HOME",
			Value: strings.TrimSuffix(homeDir, "/") + "/home",
		},
		{
			Name:  "PLATFORM_AGENT_DASHBOARD",
			Value: dashboardVal,
		},
		{
			Name:  "PLATFORM_AGENT_PLUGINS_DEBUG",
			Value: pluginsDebugVal,
		},
		{
			Name:  "OTEL_SERVICE_NAME",
			Value: agent.Name + "-gateway",
		},
	}

	if agent.Spec.Deployment != nil && len(agent.Spec.Deployment.BrowserArgs) > 0 {
		envVars = append(envVars, corev1.EnvVar{
			Name:  "AGENT_BROWSER_ARGS",
			Value: strings.Join(agent.Spec.Deployment.BrowserArgs, " "),
		})
	}

	if agent.Spec.Harness != nil {
		if agent.Spec.Harness.ClusterName != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name:  "GKE_CLUSTER_NAME",
				Value: agent.Spec.Harness.ClusterName,
			})
		}
		if agent.Spec.Harness.Location != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name:  "GKE_LOCATION",
				Value: agent.Spec.Harness.Location,
			})
		}
		if agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.ApiServerSecretRef != nil {
			envVars = append(envVars, corev1.EnvVar{
				Name: "API_SERVER_KEY",
				ValueFrom: &corev1.EnvVarSource{
					SecretKeyRef: agent.Spec.Harness.Hermes.ApiServerSecretRef,
				},
			})
		}
	}

	if integration := agent.Spec.Integration; integration != nil {
		if gchat := integration.GoogleChat; gchat != nil && gchat.Enabled != nil && *gchat.Enabled {
			envVars = append(envVars, []corev1.EnvVar{
				{
					Name:  "GOOGLE_CHAT_PROJECT_ID",
					Value: gchat.ProjectID,
				},
				{
					Name:  "GOOGLE_CHAT_SUBSCRIPTION_NAME",
					Value: fmt.Sprintf("projects/%s/subscriptions/%s", gchat.ProjectID, gchat.SubscriptionName),
				},
				{
					Name:  "GOOGLE_CHAT_ALLOWED_USERS",
					Value: strings.Join(gchat.AllowedUsers, ","),
				},
				{
					Name:  "GOOGLE_CHAT_HOME_CHANNEL",
					Value: gchat.HomeChannel,
				},
			}...)
			allowAll := len(gchat.AllowedUsers) == 0
			if len(gchat.AllowedUsers) == 1 && gchat.AllowedUsers[0] == "" {
				allowAll = true
			}
			if allowAll {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "GOOGLE_CHAT_ALLOW_ALL_USERS",
					Value: "true",
				})
			}
		}
	}

	if agent.Spec.Deployment != nil && len(agent.Spec.Deployment.Env) > 0 {
		envVars = mergeEnvVars(envVars, agent.Spec.Deployment.Env)
	}

	return &appsv1.Deployment{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "apps/v1",
			Kind:       "Deployment",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-gateway",
			Namespace: agent.Namespace,
			Labels: map[string]string{
				"app": agent.Name + "-gateway",
			},
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Strategy: appsv1.DeploymentStrategy{
				Type: appsv1.RecreateDeploymentStrategyType,
			},
			Selector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"app": agent.Name + "-gateway",
				},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: map[string]string{
						"app": agent.Name + "-gateway",
					},
					Annotations: map[string]string{
						"kubeagents.x-k8s.io/config-hash":            configHash,
						"kubeagents.x-k8s.io/fluent-bit-config-hash": fluentBitHash,
					},
				},
				Spec: corev1.PodSpec{
					ServiceAccountName: saName,
					SecurityContext: &corev1.PodSecurityContext{
						FSGroup:        &fsGroup,
						RunAsUser:      ptr.To(int64(1000)),
						RunAsNonRoot:   ptr.To(true),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: []corev1.Container{
						{
							Name:            "platform-agent",
							Image:           image,
							ImagePullPolicy: pullPolicy,
							Command:         []string{"/bin/sh"},
							Args: []string{"-c", `# 1. Copy defaults
if [ -d /opt/defaults ]; then cp -rp /opt/defaults/. /opt/data/; fi

# 1.5. Write secure .env.json for MCP server environment bypass
python3 -c "import json, os; json.dump({k: os.environ.get(k, '') for k in ['API_SERVER_KEY', 'GCP_PROJECT_ID', 'GKE_CLUSTER_NAME', 'GKE_LOCATION', 'HERMES_HOME']}, open('/opt/data/.env.json', 'w'))"

# 2. Merge dynamic ConfigMap settings into config.yaml
if [ -f /opt/config/config.yaml ]; then
  python3 -c "import yaml; d=yaml.safe_load(open('/opt/data/config.yaml')) or {}; c=yaml.safe_load(open('/opt/config/config.yaml')) or {}; d.update(c); yaml.safe_dump(d, open('/opt/data/config.yaml', 'w'))"
fi

# 3. Automatically connect to the target GKE cluster on boot if configured
if [ -n "$GKE_CLUSTER_NAME" ] && [ -n "$GKE_LOCATION" ]; then
  gcloud container clusters get-credentials "$GKE_CLUSTER_NAME" --region "$GKE_LOCATION" --project "${GCP_PROJECT_ID:-kube-agents-gke}" || true
fi

# 4. Execute the gateway
exec hermes gateway run`},
							Ports: []corev1.ContainerPort{
								{
									Name:          "dashboard",
									ContainerPort: 9119,
								},
								{
									Name:          "api",
									ContainerPort: 8642,
								},
							},
							Env: envVars,
							Resources: corev1.ResourceRequirements{
								Requests: corev1.ResourceList{
									corev1.ResourceCPU:    resource.MustParse("500m"),
									corev1.ResourceMemory: resource.MustParse("2Gi"),
								},
								Limits: corev1.ResourceList{
									corev1.ResourceCPU:    resource.MustParse("2"),
									corev1.ResourceMemory: resource.MustParse("4Gi"),
								},
							},
							VolumeMounts: []corev1.VolumeMount{
								{
									Name:      "platform-agent-data-vol",
									MountPath: homeDir,
								},
								{
									Name:      "platform-agent-config-vol",
									MountPath: "/opt/config",
								},
							},
							SecurityContext: &corev1.SecurityContext{
								AllowPrivilegeEscalation: ptr.To(false),
								Capabilities: &corev1.Capabilities{
									Drop: []corev1.Capability{"ALL"},
								},
							},
						},
						{
							Name:  "fluent-bit",
							Image: "fluent/fluent-bit:5.0.7",
							Args: []string{
								"-c",
								"/fluent-bit/etc/fluent-bit.conf",
							},
							Resources: corev1.ResourceRequirements{
								Requests: corev1.ResourceList{
									corev1.ResourceCPU:              resource.MustParse("100m"),
									corev1.ResourceEphemeralStorage: resource.MustParse("1Gi"),
									corev1.ResourceMemory:           resource.MustParse("128Mi"),
								},
								Limits: corev1.ResourceList{
									corev1.ResourceCPU:              resource.MustParse("500m"),
									corev1.ResourceEphemeralStorage: resource.MustParse("1Gi"),
									corev1.ResourceMemory:           resource.MustParse("256Mi"),
								},
							},
							VolumeMounts: []corev1.VolumeMount{
								{
									Name:      "platform-agent-data-vol",
									MountPath: "/opt/data",
									ReadOnly:  true,
								},
								{
									Name:      "fluent-bit-config",
									MountPath: "/fluent-bit/etc/fluent-bit.conf",
									SubPath:   "fluent-bit.conf",
									ReadOnly:  true,
								},
								{
									Name:      "fluent-bit-state",
									MountPath: "/fluent-bit/state",
								},
							},
							SecurityContext: &corev1.SecurityContext{
								AllowPrivilegeEscalation: ptr.To(false),
								Capabilities: &corev1.Capabilities{
									Drop: []corev1.Capability{"ALL"},
								},
							},
						},
					},
					Volumes: []corev1.Volume{
						{
							Name: "platform-agent-data-vol",
							VolumeSource: corev1.VolumeSource{
								PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
									ClaimName: agent.Name + "-data",
								},
							},
						},
						{
							Name: "platform-agent-config-vol",
							VolumeSource: corev1.VolumeSource{
								ConfigMap: &corev1.ConfigMapVolumeSource{
									LocalObjectReference: corev1.LocalObjectReference{
										Name: agent.Name + "-config",
									},
									DefaultMode: ptr.To(int32(0755)),
								},
							},
						},
						{
							Name: "fluent-bit-config",
							VolumeSource: corev1.VolumeSource{
								ConfigMap: &corev1.ConfigMapVolumeSource{
									LocalObjectReference: corev1.LocalObjectReference{
										Name: agent.Name + "-fluent-bit-config",
									},
									DefaultMode: ptr.To(int32(420)),
								},
							},
						},
						{
							Name: "fluent-bit-state",
							VolumeSource: corev1.VolumeSource{
								EmptyDir: &corev1.EmptyDirVolumeSource{},
							},
						},
					},
				},
			},
		},
	}
}

// buildPlatformExplorerRole generates the custom ClusterRole manifest
func buildPlatformExplorerRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.ClusterRole {
	return &rbacv1.ClusterRole{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "ClusterRole",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name: fmt.Sprintf("kubeagents:explorer:%s:%s", agent.Namespace, agent.Name),
		},
		Rules: []rbacv1.PolicyRule{
			{
				APIGroups: []string{""},
				Resources: []string{"nodes", "pods", "namespaces"},
				Verbs:     []string{"get", "list"},
			},
		},
	}
}

// buildClusterRoleBinding generates a ClusterRoleBinding manifest
func buildClusterRoleBinding(agent *agentv1alpha1.PlatformAgent, bindingName, roleName string) *rbacv1.ClusterRoleBinding {
	saName := agent.Name
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		saName = agent.Spec.Security.ServiceAccountName
	}

	return &rbacv1.ClusterRoleBinding{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "ClusterRoleBinding",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name: bindingName,
		},
		Subjects: []rbacv1.Subject{
			{
				Kind:      "ServiceAccount",
				Name:      saName,
				Namespace: agent.Namespace,
			},
		},
		RoleRef: rbacv1.RoleRef{
			APIGroup: "rbac.authorization.k8s.io",
			Kind:     "ClusterRole",
			Name:     roleName,
		},
	}
}

// Helper to calculate the SHA256 hash of ConfigMap Data for rolling restarts.
func getConfigMapHash(configMap *corev1.ConfigMap) (string, error) {
	if configMap == nil {
		return "", nil
	}
	dataBytes, err := json.Marshal(configMap.Data)
	if err != nil {
		return "", err
	}
	hash := sha256.Sum256(dataBytes)
	return fmt.Sprintf("%x", hash), nil
}

// buildFluentBitConfigMap generates the ConfigMap manifest containing fluent-bit.conf
func buildFluentBitConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "ConfigMap",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-fluent-bit-config",
			Namespace: agent.Namespace,
		},
		Data: map[string]string{
			"fluent-bit.conf": `[SERVICE]
    Flush         1
    Daemon        Off
    Log_Level     info
    Parsers_File  parsers.conf

[INPUT]
    Name              tail
    Tag               agent.logs
    Path              /opt/data/logs/*.log
    DB                /fluent-bit/state/fluent-bit.db
    Refresh_Interval  5
    Rotate_Wait       30
    Mem_Buf_Limit     20MB
    Skip_Long_Lines   On
    Read_from_Head    On
    Path_Key          file_path

[FILTER]
    Name              record_modifier
    Match             agent.logs
    Record            app agent
    Record            log_source agent-file

[OUTPUT]
    Name              stdout
    Match             agent.logs
    Format            json_lines
`,
		},
	}
}

// buildPlatformService generates the Service manifest for PlatformAgent
func buildPlatformService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	return &corev1.Service{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "Service",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name,
			Namespace: agent.Namespace,
		},
		Spec: corev1.ServiceSpec{
			Selector: map[string]string{
				"app": agent.Name + "-gateway",
			},
			Ports: []corev1.ServicePort{
				{
					Name:       "api",
					Port:       8642,
					TargetPort: intstr.FromString("api"),
				},
				{
					Name:       "dashboard",
					Port:       9119,
					TargetPort: intstr.FromString("dashboard"),
				},
			},
		},
	}
}
