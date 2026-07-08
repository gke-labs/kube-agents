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
	"path"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// buildConfigMap generates the ConfigMap manifest containing openclaw.json
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
			"openclaw.json": renderConfigJSON(agent),
		},
	}
}


// buildSettingsConfigMap generates the ConfigMap manifest containing SETTINGS.md
func buildSettingsConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	gitRepo := ""
	if agent.Spec.Integration != nil && agent.Spec.Integration.GitHub != nil {
		gitRepo = agent.Spec.Integration.GitHub.GitRepo
	}
	if gitRepo == "" {
		gitRepo = "None"
	}
	settingsContent := fmt.Sprintf("# GKE Scope Configuration\n- **Git Repo:** %s\n", gitRepo)
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "ConfigMap",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-settings",
			Namespace: agent.Namespace,
		},
		Data: map[string]string{
			"SETTINGS.md": settingsContent,
		},
	}
}

// OpenClawConfig defines the JSON schema for openclaw.json.
type OpenClawConfig struct {
	Agents struct {
		Defaults struct {
			Workspace string `json:"workspace"`
			Model     struct {
				Primary string `json:"primary"`
			} `json:"model"`
			Sandbox struct {
				Mode            string `json:"mode"`
				Backend         string `json:"backend,omitempty"`
				WorkspaceAccess string `json:"workspaceAccess"`
			} `json:"sandbox"`
		} `json:"defaults"`
	} `json:"agents"`
	Gateway struct {
		Mode      string `json:"mode"`
		Port      int    `json:"port"`
		Bind      string `json:"bind"`
		Auth      struct {
			Mode  string `json:"mode"`
			Token string `json:"token"`
		} `json:"auth"`
		ControlUi struct {
			DangerouslyDisableDeviceAuth bool `json:"dangerouslyDisableDeviceAuth"`
		} `json:"controlUi"`
	} `json:"gateway"`
	Models struct {
		Providers map[string]any `json:"providers"`
	} `json:"models"`
	Auth struct {
		Profiles map[string]any `json:"profiles"`
	} `json:"auth"`
	Channels map[string]any `json:"channels,omitempty"`
	Plugins  struct {
		Entries map[string]any `json:"entries"`
	} `json:"plugins"`
	MCP struct {
		Servers map[string]any `json:"servers"`
	} `json:"mcp"`
	Diagnostics struct {
		Enabled bool `json:"enabled"`
		Otel    struct {
			Enabled     bool   `json:"enabled"`
			Endpoint    string `json:"endpoint"`
			Protocol    string `json:"protocol"`
			ServiceName string `json:"serviceName"`
			Traces      bool   `json:"traces"`
			Metrics     bool   `json:"metrics"`
			Logs        bool   `json:"logs"`
		} `json:"otel"`
	} `json:"diagnostics"`
	Logging struct {
		Level string `json:"level,omitempty"`
		File  string `json:"file,omitempty"`
	} `json:"logging,omitempty"`
}

// renderConfigJSON generates the JSON payload for openclaw.json
func renderConfigJSON(agent *agentv1alpha1.PlatformAgent) string {
	cwd := "/opt/data"
	if agent.Spec.Harness != nil && agent.Spec.Harness.OpenClaw != nil && agent.Spec.Harness.OpenClaw.AgentHome != "" {
		cwd = agent.Spec.Harness.OpenClaw.AgentHome
	}

	openclaw_config := OpenClawConfig{}

	// Set Defaults
	openclaw_config.Agents.Defaults.Workspace = cwd + "/workspace"
	openclaw_config.Agents.Defaults.Model.Primary = "openai/model-default"
	openclaw_config.Agents.Defaults.Sandbox.Mode = "off"
	openclaw_config.Agents.Defaults.Sandbox.Backend = ""
	openclaw_config.Agents.Defaults.Sandbox.WorkspaceAccess = "rw"

	openclaw_config.Gateway.Mode = "local"
	openclaw_config.Gateway.Port = 8642
	openclaw_config.Gateway.Bind = "lan"
	openclaw_config.Gateway.Auth.Mode = "token"
	openclaw_config.Gateway.Auth.Token = "${API_SERVER_KEY}"
	openclaw_config.Gateway.ControlUi.DangerouslyDisableDeviceAuth = true

	// Models Providers
	openclaw_config.Models.Providers = map[string]any{
		"openai": map[string]any{
			"api":     "openai-responses",
			"baseUrl": fmt.Sprintf("http://litellm.%s.svc.cluster.local/v1", agent.Namespace),
			"apiKey":  "none",
			"models": []map[string]string{
				{
					"id":   "model-default",
					"name": "model-default",
				},
			},
		},
	}

	// Auth Profiles
	openclaw_config.Auth.Profiles = map[string]any{
		"openai:default": map[string]any{
			"provider": "openai",
			"mode":     "token",
		},
	}

	// Plugins Load
	openclaw_config.Plugins.Entries = map[string]any{
		"google": map[string]any{"enabled": true},
	}

	// Channels
	if integration := agent.Spec.Integration; integration != nil {
		if gchat := integration.GoogleChat; gchat != nil && gchat.Enabled != nil && *gchat.Enabled {
			audienceType := "project-number"
			audience := gchat.ProjectNumber
			if audience == "" {
				audience = gchat.ProjectID
			}
			if gchat.AppURL != "" {
				audienceType = "app-url"
				audience = gchat.AppURL
			}
			defaultAccountConfig := map[string]any{
				"enabled":      true,
				"audienceType": audienceType,
				"audience":     audience,
				"dm": map[string]any{
					"policy":    "open",
					"allowFrom": []string{"*"},
				},
			}
			if gchat.AppPrincipal != "" {
				defaultAccountConfig["appPrincipal"] = gchat.AppPrincipal
			}
			openclaw_config.Channels = map[string]any{
				"googlechat": map[string]any{
					"enabled":        true,
					"defaultAccount": "default",
					"accounts": map[string]any{
						"default": defaultAccountConfig,
					},
				},
			}
			openclaw_config.Plugins.Entries["googlechat"] = map[string]any{"enabled": true}
		}
	}

	// MCP Servers
	openclaw_config.MCP.Servers = map[string]any{
		"platform_control": map[string]any{
			"command":         "/opt/openclaw/.venv/bin/python3",
			"args":            []string{"/opt/data/scripts/platform_mcp_server.py"},
			"connect_timeout": 120,
			"timeout":         300,
			"env": map[string]string{
				"KUBERNETES_SERVICE_HOST":       "${KUBERNETES_SERVICE_HOST}",
				"KUBERNETES_SERVICE_PORT":       "${KUBERNETES_SERVICE_PORT}",
				"OPENCLAW_HOME":                 "${OPENCLAW_HOME}",
				"GOOGLE_CHAT_PROJECT_ID":        "${GOOGLE_CHAT_PROJECT_ID}",
				"GOOGLE_CHAT_SUBSCRIPTION_NAME": "${GOOGLE_CHAT_SUBSCRIPTION_NAME}",
				"API_SERVER_KEY":                "${API_SERVER_KEY}",
			},
		},
		"agent_common": map[string]any{
			"command": "/opt/openclaw/.venv/bin/python3",
			"args":    []string{"/opt/data/scripts/agent_common_server.py"},
		},
		"developer_knowledge": map[string]any{
			"command": "node",
			"args":    []string{"/opt/mcp-remote/dist/proxy.js", "https://developerknowledge.googleapis.com/mcp"},
		},
	}

	// Diagnostics Telemetry (OTel tracing)
	openclaw_config.Diagnostics.Enabled = true
	openclaw_config.Diagnostics.Otel.Enabled = true
	openclaw_config.Diagnostics.Otel.Endpoint = "http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318"
	openclaw_config.Diagnostics.Otel.Protocol = "http/protobuf"
	openclaw_config.Diagnostics.Otel.ServiceName = agent.Name + "-gateway"
	openclaw_config.Diagnostics.Otel.Traces = true
	openclaw_config.Diagnostics.Otel.Metrics = false
	openclaw_config.Diagnostics.Otel.Logs = false
	
	// Logging output destination configuration
	openclaw_config.Logging.Level = "info"
	openclaw_config.Logging.File = "/opt/data/logs/openclaw.log"

	payload, err := json.MarshalIndent(openclaw_config, "", "  ")
	if err != nil {
		return "{}"
	}
	return string(payload)
}

// resolveGoogleChatDisplayConfig resolves verbosity settings for Google Chat based on mode ("default" or "debug").
func resolveGoogleChatDisplayConfig(mode string) map[string]any {
	resolvedMode := "default"
	if mode != "" {
		resolvedMode = strings.ToLower(mode)
	}

	toolProgress := "off"
	toolProgressGrouping := "accumulate"
	memoryNotifications := "off"
	interimMessages := false

	if resolvedMode == "debug" {
		toolProgress = "all"
		memoryNotifications = "verbose"
		interimMessages = true
	}

	return map[string]any{
		"tool_progress":              toolProgress,
		"tool_progress_grouping":     toolProgressGrouping,
		"memory_notifications":       memoryNotifications,
		"interim_assistant_messages": interimMessages,
		"long_running_notifications": true,
		"busy_ack_detail":            interimMessages,
	}
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
func buildDeployment(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash string) *appsv1.Deployment {
	replicas := int32(1)
	// UID/GID 10000 matches the canonical unprivileged 'openclaw' runtime user created in our Dockerfile
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
	if agent.Spec.Harness != nil && agent.Spec.Harness.OpenClaw != nil && agent.Spec.Harness.OpenClaw.AgentHome != "" {
		homeDir = agent.Spec.Harness.OpenClaw.AgentHome
	}

	envVars := []corev1.EnvVar{
		{
			Name:  "OPENCLAW_HOME",
			Value: homeDir,
		},
		{
			Name:  "HOME",
			Value: strings.TrimSuffix(homeDir, "/") + "/home",
		},

		{
			Name:  "OTEL_SERVICE_NAME",
			Value: agent.Name + "-gateway",
		},
		{
			Name:  "API_SERVER_ENABLED",
			Value: "true",
		},
		{
			Name:  "API_SERVER_HOST",
			Value: "0.0.0.0",
		},
		{
			Name:  "OPENCLAW_CONFIG_PATH",
			Value: path.Join(homeDir, "openclaw.json"),
		},
		{
			Name:  "OPENCLAW_BUNDLED_PLUGINS_DIR",
			Value: "/opt/openclaw/dist/extensions",
		},
		{
			Name: "GOOGLE_CHAT_SERVICE_ACCOUNT",
			ValueFrom: &corev1.EnvVarSource{
				SecretKeyRef: &corev1.SecretKeySelector{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: "platform-agent-secrets",
					},
					Key: "GOOGLE_CHAT_SERVICE_ACCOUNT",
				},
			},
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
		if agent.Spec.Harness.OpenClaw != nil && agent.Spec.Harness.OpenClaw.GatewayTokenSecretRef != nil {
			envVars = append(envVars, corev1.EnvVar{
				Name: "API_SERVER_KEY",
				ValueFrom: &corev1.EnvVarSource{
					SecretKeyRef: agent.Spec.Harness.OpenClaw.GatewayTokenSecretRef,
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
						"kubeagents.x-k8s.io/settings-config-hash":   settingsConfigHash,
					},
				},
				Spec: corev1.PodSpec{
					ServiceAccountName: saName,
					SecurityContext: &corev1.PodSecurityContext{
						FSGroup: &fsGroup,
						// UID 10000 matches canonical 'openclaw' runtime user in container image
						RunAsUser:      ptr.To(int64(10000)),
						RunAsNonRoot:   ptr.To(true),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: []corev1.Container{
						{
							Name:            "platform-agent",
							Image:           image,
							ImagePullPolicy: pullPolicy,
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
									MountPath: fmt.Sprintf("%s/openclaw.json", homeDir),
									SubPath:   "openclaw.json",
								},
								{
									Name:      "settings-volume",
									MountPath: path.Join(homeDir, "SETTINGS.md"),
									SubPath:   "SETTINGS.md",
									ReadOnly:  true,
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
									Name:      "fluent-bit-config",
									MountPath: "/fluent-bit/etc/parsers.conf",
									SubPath:   "parsers.conf",
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
						{
							Name: "settings-volume",
							VolumeSource: corev1.VolumeSource{
								ConfigMap: &corev1.ConfigMapVolumeSource{
									LocalObjectReference: corev1.LocalObjectReference{
										Name: agent.Name + "-settings",
									},
									DefaultMode: ptr.To(int32(0644)),
								},
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
			{
				APIGroups: []string{"apiextensions.k8s.io"},
				Resources: []string{"customresourcedefinitions"},
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
    Name          parser
    Match         agent.logs
    Key_Name      log
    Parser        gchat_event
    Reserve_Data  On
    Preserve_Key  On

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
			"parsers.conf": `[PARSER]
    Name    gchat_event
    Format  regex
    Regex   User=(?<gchat_user>[^,\s]+),\s*Session=(?<gchat_session>[^,\s]+)
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
