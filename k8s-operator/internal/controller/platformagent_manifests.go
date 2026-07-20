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
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const defaultPlatformAgentSecrets = "platform-agent-secrets"
const sessionKVDBPath = "/var/lib/kube-agents/session/session_kv.db"

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
		MCPServers       map[string]any      `json:"mcp_servers,omitempty"`
		PlatformToolsets map[string][]string `json:"platform_toolsets,omitempty"`
		Approvals        struct {
			CronMode string `json:"cron_mode,omitempty"`
		} `json:"approvals,omitempty"`
		Web struct {
			Backend string `json:"backend,omitempty"`
		} `json:"web,omitempty"`
		Memory struct {
			MemoryEnabled      bool   `json:"memory_enabled"`
			Provider           string `json:"provider"`
			UserProfileEnabled bool   `json:"user_profile_enabled"`
		} `json:"memory"`
		Platforms struct {
			GoogleChat struct {
				Enabled bool `json:"enabled"`
			} `json:"google_chat"`
			Slack struct {
				Enabled bool `json:"enabled"`
			} `json:"slack"`
		} `json:"platforms"`
		Plugins struct {
			Enabled []string `json:"enabled"`
		} `json:"plugins"`
		Display struct {
			Platforms map[string]map[string]any `json:"platforms,omitempty"`
		} `json:"display,omitempty"`
	}{}

	// Model & Terminal configuration
	cfg.Model.Provider = "custom"
	cfg.Model.Default = "model-default"
	cfg.Model.Model = "model-default"
	cfg.Model.BaseURL = fmt.Sprintf("http://litellm.%s.svc.cluster.local/v1", agent.Namespace)
	cfg.Model.APIKey = "none"
	cfg.Terminal.Backend = "local"
	cfg.Terminal.Cwd = cwd

	// MCP Servers & Toolsets configuration
	cfg.MCPServers = map[string]any{
		"platform_control": map[string]any{
			"command":         "/opt/hermes/.venv/bin/python3",
			"args":            []string{"/opt/data/scripts/platform_mcp_server.py"},
			"connect_timeout": 120,
			"timeout":         300,
			"env": map[string]string{
				"KUBERNETES_SERVICE_HOST":       "${KUBERNETES_SERVICE_HOST}",
				"KUBERNETES_SERVICE_PORT":       "${KUBERNETES_SERVICE_PORT}",
				"HERMES_HOME":                   "${HERMES_HOME}",
				"GOOGLE_CHAT_PROJECT_ID":        "${GOOGLE_CHAT_PROJECT_ID}",
				"GOOGLE_CHAT_SUBSCRIPTION_NAME": "${GOOGLE_CHAT_SUBSCRIPTION_NAME}",
				"API_SERVER_KEY":                "${API_SERVER_KEY}",
			},
		},
		"agent_common": map[string]any{
			"command": "/opt/hermes/.venv/bin/python3",
			"args":    []string{"/opt/data/scripts/agent_common_server.py"},
		},
		"developer_knowledge": map[string]any{
			"command": "node",
			"args":    []string{"/opt/mcp-remote/dist/proxy.js", "https://developerknowledge.googleapis.com/mcp"},
		},
		"gke": map[string]any{
			"command": "node",
			"args":    []string{"/opt/mcp-remote/dist/proxy.js", "https://container.googleapis.com/mcp"},
		},
	}
	cfg.PlatformToolsets = map[string][]string{
		"cli":        {"hermes-cli", "mcp-agent_common", "mcp-platform_control", "mcp-developer_knowledge", "mcp-gke"},
		"api_server": {"hermes-api-server", "mcp-agent_common", "mcp-platform_control", "mcp-developer_knowledge", "mcp-gke"},
	}

	// Execution & Display UX configuration
	cfg.Approvals.CronMode = "approve"
	cfg.Web.Backend = "ddgs"
	cfg.Plugins.Enabled = []string{"hermes_otel", "session_store", "session_otel_bridge", "tool_call_audit"}
	cfg.Display.Platforms = map[string]map[string]any{}
	cfg.Memory.MemoryEnabled = false
	cfg.Memory.Provider = "multiuser_memory"
	cfg.Memory.UserProfileEnabled = false

	if agent.Spec.Harness != nil && agent.Spec.Harness.Memory != nil {
		if agent.Spec.Harness.Memory.MemoryEnabled != nil {
			cfg.Memory.MemoryEnabled = *agent.Spec.Harness.Memory.MemoryEnabled
		}
		if agent.Spec.Harness.Memory.Provider != "" {
			cfg.Memory.Provider = agent.Spec.Harness.Memory.Provider
		}
		if agent.Spec.Harness.Memory.UserProfileEnabled != nil {
			cfg.Memory.UserProfileEnabled = *agent.Spec.Harness.Memory.UserProfileEnabled
		}
	}

	if agent.Spec.Integration != nil {
		if gchat := agent.Spec.Integration.GoogleChat; gchat != nil {
			if gchat.Enabled != nil {
				cfg.Platforms.GoogleChat.Enabled = *gchat.Enabled
			}
			cfg.Display.Platforms["google_chat"] = resolveGoogleChatDisplayConfig(gchat.Mode)
		}
		if slack := agent.Spec.Integration.Slack; slack != nil && slack.Enabled != nil {
			cfg.Platforms.Slack.Enabled = *slack.Enabled
		}
	}

	data, err := yaml.Marshal(cfg)
	if err != nil {
		return ""
	}
	return string(data)
}

// resolveGoogleChatDisplayConfig resolves verbosity settings for Google Chat based on mode ("default" or "debug").
func resolveGoogleChatDisplayConfig(mode string) map[string]any {
	resolvedMode := "default"
	if mode != "" {
		resolvedMode = strings.ToLower(mode)
	}

	toolProgress := "off"
	memoryNotifications := "off"
	interimMessages := false

	if resolvedMode == "debug" {
		toolProgress = "all"
		memoryNotifications = "verbose"
		interimMessages = true
	}

	return map[string]any{
		"tool_progress":              toolProgress,
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

func buildSystemPVC(agent *agentv1alpha1.PlatformAgent) *corev1.PersistentVolumeClaim {
	return &corev1.PersistentVolumeClaim{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "PersistentVolumeClaim",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      "system-metadata",
			Namespace: agent.Namespace,
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceStorage: resource.MustParse("1Gi"),
				},
			},
		},
	}
}

// buildDeployment generates the Deployment manifest for the agent payload
func buildDeployment(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash string) *appsv1.Deployment {
	replicas, strategy := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	// UID/GID 10000 matches the canonical unprivileged 'hermes' runtime user created in NousResearch/hermes-agent upstream Dockerfile
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

	var initContainers []corev1.Container
	var sidecars []corev1.Container
	var sidecarVolumes []corev1.Volume
	var extraVolumes []corev1.Volume
	var extraVolumeMounts []corev1.VolumeMount
	var podAnnotations map[string]string
	if agent.Spec.Deployment != nil {
		initContainers = agent.Spec.Deployment.InitContainers
		sidecars = agent.Spec.Deployment.Sidecars
		sidecarVolumes = agent.Spec.Deployment.SidecarVolumes
		extraVolumes = agent.Spec.Deployment.ExtraVolumes
		extraVolumeMounts = agent.Spec.Deployment.ExtraVolumeMounts
		podAnnotations = agent.Spec.Deployment.PodAnnotations
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
			Name:  "HERMES_HOME",
			Value: homeDir,
		},
		{
			Name:  "HOME",
			Value: strings.TrimSuffix(homeDir, "/") + "/home",
		},
		{
			Name:  "HERMES_DASHBOARD",
			Value: dashboardVal,
		},
		{
			Name:  "HERMES_PLUGINS_DEBUG",
			Value: pluginsDebugVal,
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
			Name:  "SESSION_KV_DB_PATH",
			Value: sessionKVDBPath,
		},
		{
			Name:  "HERMES_DASHBOARD_HOST",
			Value: "127.0.0.1",
		},
	}

	envVars = append(envVars, otelTelemetryEnvVars("platform", agent.Name, agent.Namespace)...)

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
		if slack := integration.Slack; slack != nil && slack.Enabled != nil && *slack.Enabled {
			envVars = append(envVars,
				corev1.EnvVar{
					Name:      "SLACK_BOT_TOKEN",
					ValueFrom: &corev1.EnvVarSource{SecretKeyRef: defaultSecretRef(slack.BotTokenSecretRef, defaultPlatformAgentSecrets, "SLACK_BOT_TOKEN")},
				},
				corev1.EnvVar{
					Name:      "SLACK_APP_TOKEN",
					ValueFrom: &corev1.EnvVarSource{SecretKeyRef: defaultSecretRef(slack.AppTokenSecretRef, defaultPlatformAgentSecrets, "SLACK_APP_TOKEN")},
				},
			)
			allowAllSlack := len(slack.AllowedUsers) == 0 || (len(slack.AllowedUsers) == 1 && slack.AllowedUsers[0] == "")
			if allowAllSlack {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "SLACK_ALLOW_ALL_USERS",
					Value: "true",
				})
			} else {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "SLACK_ALLOWED_USERS",
					Value: strings.Join(slack.AllowedUsers, ","),
				})
			}
			if slack.HomeChannel != "" {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "SLACK_HOME_CHANNEL",
					Value: slack.HomeChannel,
				})
			}
			if slack.HomeChannelName != "" {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "SLACK_HOME_CHANNEL_NAME",
					Value: slack.HomeChannelName,
				})
			}
		}
	}

	envVars = append(envVars, corev1.EnvVar{
		Name:  "TOKEN_BROKER_URL",
		Value: fmt.Sprintf("http://github-token-minter.%s.svc.cluster.local:8080/token", agent.Namespace),
	})

	if agent.Spec.Deployment != nil && len(agent.Spec.Deployment.Env) > 0 {
		envVars = mergeEnvVars(envVars, agent.Spec.Deployment.Env)
	}

	var runtimeClassName *string
	if agent.Spec.Deployment != nil {
		runtimeClassName = agent.Spec.Deployment.RuntimeClassName
	}

	containers := buildBaseContainers(image, pullPolicy, envVars, homeDir, extraVolumeMounts)
	defaultAnnotations := map[string]string{
		"kubeagents.x-k8s.io/config-hash":            configHash,
		"kubeagents.x-k8s.io/fluent-bit-config-hash": fluentBitHash,
		"kubeagents.x-k8s.io/settings-config-hash":   settingsConfigHash,
	}

	if len(sidecars) > 0 {
		containers = append(containers, sidecars...)
	}

	volumes := buildDefaultVolumes(agent)
	if len(sidecarVolumes) > 0 {
		volumes = append(volumes, sidecarVolumes...)
	}
	if len(extraVolumes) > 0 {
		volumes = append(volumes, extraVolumes...)
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
			Strategy: strategy,
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
					Annotations: mergeAnnotations(defaultAnnotations, podAnnotations),
				},
				Spec: corev1.PodSpec{
					RuntimeClassName:   runtimeClassName,
					InitContainers:     initContainers,
					ServiceAccountName: saName,
					SecurityContext: &corev1.PodSecurityContext{
						FSGroup: &fsGroup,
						// UID 10000 matches canonical 'hermes' runtime user in upstream image (NousResearch/hermes-agent Dockerfile line 92)
						RunAsUser:      ptr.To(int64(10000)),
						RunAsNonRoot:   ptr.To(true),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: containers,
					Volumes:    volumes,
				},
			},
		},
	}
}

// buildDefaultContainers generates the default containers for PlatformAgent
func buildBaseContainers(image string, pullPolicy corev1.PullPolicy, envVars []corev1.EnvVar, homeDir string, extraVolumeMounts []corev1.VolumeMount) []corev1.Container {
	defaultPlatformAgentVolumeMounts := []corev1.VolumeMount{
		{
			Name:      "platform-agent-data-vol",
			MountPath: homeDir,
		},
		{
			Name:      "platform-agent-config-vol",
			MountPath: fmt.Sprintf("%s/config.yaml", homeDir),
			SubPath:   "config.yaml",
		},
		{
			Name:      "settings-volume",
			MountPath: path.Join(homeDir, "SETTINGS.md"),
			SubPath:   "SETTINGS.md",
			ReadOnly:  true,
		},
		{
			Name:      "system-metadata",
			MountPath: path.Dir(sessionKVDBPath),
			SubPath:   "session",
		},
		{
			Name:      "tmp-run",
			MountPath: "/run",
		},
	}

	return []corev1.Container{
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
			VolumeMounts: append(defaultPlatformAgentVolumeMounts, extraVolumeMounts...),
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
	}
}

// buildDefaultVolumes generates the default volumes for PlatformAgent
func buildDefaultVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	return []corev1.Volume{
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
			Name: "system-metadata",
			VolumeSource: corev1.VolumeSource{
				PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
					ClaimName: "system-metadata",
				},
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
		{
			Name: "tmp-run",
			VolumeSource: corev1.VolumeSource{
				EmptyDir: &corev1.EmptyDirVolumeSource{
					Medium:    corev1.StorageMediumMemory,
					SizeLimit: ptr.To(resource.MustParse("64Mi")),
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
