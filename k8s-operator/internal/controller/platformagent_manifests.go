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
	_ "embed"
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

const (
	defaultPlatformAgentSecrets = "platform-agent-secrets"
	sessionKVDBPath             = "/var/lib/kube-agents/session/session_kv.db"
	defaultAgentHome            = "/opt/data"
	defaultStorageSize          = "5Gi"
	credentialProxyPort         = 8765
)

// getDefaultStorageConfig returns the access modes and storage class name based on the replica count and user configuration.
func getDefaultStorageConfig(agent *agentv1alpha1.PlatformAgent) ([]corev1.PersistentVolumeAccessMode, *string) {
	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	accessModes := []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce}
	var storageClassName *string

	if agent.Spec.Deployment != nil && agent.Spec.Deployment.DefaultStorageClassName != nil {
		storageClassName = agent.Spec.Deployment.DefaultStorageClassName
	} else if replicas > 1 {
		storageClassName = ptr.To("standard-rwx")
	}

	if replicas > 1 {
		accessModes = []corev1.PersistentVolumeAccessMode{corev1.ReadWriteMany}
	}

	return accessModes, storageClassName
}

var defaultAccessModes = []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce}

// The broker currently receives a shell command string, so these rules allow
// flags between command components. If the protocol is extended to carry argv,
// replace this regex matching with tool-specific argument parsing.
const credentialProxyPolicyJSON = `{
  "apiVersion": "cli.proxy.kubeagents.io/v1alpha1",
  "blockedMessage": "Command blocked for security reasons.",
  "rules": [
    {"id":"gcp.access-token-disclosure","pattern":"\\bgcloud\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+print-(?:access|identity)-token\\b"},
    {"id":"gcp.config-helper-disclosure","pattern":"\\bgcloud\\b(?:\\s+\\S+)*?\\s+config\\b(?:\\s+\\S+)*?\\s+config-helper\\b"},
    {"id":"github.token-disclosure","pattern":"\\bgh\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+token\\b|\\bgh\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+status\\b(?:\\s+\\S+)*?\\s+--show-token\\b"},
    {"id":"kubernetes.token-disclosure","pattern":"\\bkubectl\\b(?:\\s+\\S+)*?\\s+create\\b(?:\\s+\\S+)*?\\s+token\\b|\\bkubectl\\b(?:\\s+\\S+)*?\\s+config\\b(?:\\s+\\S+)*?\\s+view\\b(?:\\s+\\S+)*?\\s+--raw\\b"},
    {"id":"git.credential-disclosure","pattern":"\\bgit\\b(?:\\s+\\S+)*?\\s+credential\\b(?:\\s+\\S+)*?\\s+fill\\b"},
    {"id":"gcp.credential-replacement","pattern":"\\bgcloud\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+(?:login|activate-service-account)\\b"},
    {"id":"github.credential-replacement","pattern":"\\bgh\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+(?:login|refresh|switch|logout)\\b"},
    {"id":"tool.self-modification","pattern":"\\bgcloud\\b(?:\\s+\\S+)*?\\s+components\\b(?:\\s+\\S+)*?\\s+(?:install|update|remove)\\b|\\bgh\\b(?:\\s+\\S+)*?\\s+extension\\b(?:\\s+\\S+)*?\\s+(?:install|upgrade|remove)\\b"}
  ]
}`

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
			"config.yaml":     renderConfigYAML(agent),
			"leader_elect.py": leaderElectScript,
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
	cwd := defaultAgentHome
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
		LeaderElection struct {
			Enabled   bool   `json:"enabled"`
			LeaseName string `json:"lease_name,omitempty"`
			Namespace string `json:"namespace,omitempty"`
		} `json:"leader_election,omitempty"`
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
	// Enable incident_context plugin by default to parse and rewrite GChat/Slack threaded incident replies
	cfg.Plugins.Enabled = []string{"hermes_otel", "session_store", "session_otel_bridge", "tool_call_audit", "incident_context"}
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

	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	if replicas > 1 {
		cfg.LeaderElection.Enabled = true
		cfg.LeaderElection.LeaseName = agent.Name + "-leader"
		cfg.LeaderElection.Namespace = agent.Namespace
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
	accessModes, storageClassName := getDefaultStorageConfig(agent)
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
			AccessModes:      accessModes,
			StorageClassName: storageClassName,
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceStorage: resource.MustParse("10Gi"),
				},
			},
		},
	}
}

func buildSystemPVC(agent *agentv1alpha1.PlatformAgent) *corev1.PersistentVolumeClaim {
	accessModes, storageClassName := getDefaultStorageConfig(agent)
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
			AccessModes:      accessModes,
			StorageClassName: storageClassName,
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceStorage: resource.MustParse("1Gi"),
				},
			},
		},
	}
}

// isRWOStorage checks if a storage configuration specifies ReadWriteOnce access or an RWO StorageClass
func isRWOStorage(storage agentv1alpha1.StorageSpec) bool {
	accessModes := storage.AccessModes
	for _, mode := range accessModes {
		if mode == corev1.ReadWriteOnce {
			return true
		}
	}
	if storage.StorageClassName != nil {
		sc := strings.ToLower(*storage.StorageClassName)
		if strings.Contains(sc, "rwo") {
			return true
		}
	}
	return false
}

// hasCustomRWOStorage returns true if any custom storage spec uses ReadWriteOnce access mode or an RWO StorageClass
func hasCustomRWOStorage(agent *agentv1alpha1.PlatformAgent) bool {
	if agent.Spec.Deployment == nil {
		return false
	}
	for _, storage := range agent.Spec.Deployment.Storages {
		if isRWOStorage(storage) {
			return true
		}
	}
	return false
}

// useStatefulSet returns true if the platform agent workload should be managed as a StatefulSet
func useStatefulSet(agent *agentv1alpha1.PlatformAgent) bool {
	if agent.Spec.Deployment == nil {
		return false
	}
	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	return replicas > 1 && hasCustomRWOStorage(agent)
}

// buildCustomPVCInstance constructs a single PersistentVolumeClaim manifest
func buildCustomPVCInstance(name, namespace string, accessModes []corev1.PersistentVolumeAccessMode, scName *string, parsedSize resource.Quantity) *corev1.PersistentVolumeClaim {
	return &corev1.PersistentVolumeClaim{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "PersistentVolumeClaim",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes:      accessModes,
			StorageClassName: scName,
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceStorage: parsedSize,
				},
			},
		},
	}
}

// buildRWOVolumeClaimTemplates generates VolumeClaimTemplates for RWO custom storage specs in a StatefulSet
func buildRWOVolumeClaimTemplates(agent *agentv1alpha1.PlatformAgent) []corev1.PersistentVolumeClaim {
	if agent.Spec.Deployment == nil || len(agent.Spec.Deployment.Storages) == 0 {
		return nil
	}
	var vcts []corev1.PersistentVolumeClaim
	for _, storage := range agent.Spec.Deployment.Storages {
		if isRWOStorage(storage) {
			accessModes := storage.AccessModes
			if len(accessModes) == 0 {
				accessModes = defaultAccessModes
			}
			storageSize := storage.StorageSize
			if storageSize == "" {
				storageSize = "5Gi"
			}
			parsedSize, err := resource.ParseQuantity(storageSize)
			if err != nil {
				parsedSize = resource.MustParse("5Gi")
			}
			vcts = append(vcts, corev1.PersistentVolumeClaim{
				ObjectMeta: metav1.ObjectMeta{
					Name: storage.Name + "-vol",
				},
				Spec: corev1.PersistentVolumeClaimSpec{
					AccessModes:      accessModes,
					StorageClassName: storage.StorageClassName,
					Resources: corev1.VolumeResourceRequirements{
						Requests: corev1.ResourceList{
							corev1.ResourceStorage: parsedSize,
						},
					},
				},
			})
		}
	}
	return vcts
}

// buildCustomPVCs generates PVC manifests for custom storage definitions specified in DeploymentSpec.Storages
func buildCustomPVCs(agent *agentv1alpha1.PlatformAgent) ([]*corev1.PersistentVolumeClaim, error) {
	if agent.Spec.Deployment == nil || len(agent.Spec.Deployment.Storages) == 0 {
		return nil, nil
	}
	useSts := useStatefulSet(agent)
	var pvcList []*corev1.PersistentVolumeClaim
	for _, storage := range agent.Spec.Deployment.Storages {
		if storage.Name == "" {
			return nil, fmt.Errorf("storage name cannot be empty")
		}
		if useSts && isRWOStorage(storage) {
			continue // Handled by VolumeClaimTemplates in StatefulSet
		}
		scName := storage.StorageClassName
		accessModes := storage.AccessModes
		if len(accessModes) == 0 {
			accessModes = defaultAccessModes
		}
		storageSize := storage.StorageSize
		if storageSize == "" {
			storageSize = defaultStorageSize
		}
		parsedSize, err := resource.ParseQuantity(storageSize)
		if err != nil {
			parsedSize = resource.MustParse(defaultStorageSize)
		}
		pvcList = append(pvcList, buildCustomPVCInstance(storage.Name, agent.Namespace, accessModes, scName, parsedSize))
	}
	return pvcList, nil
}

// buildCustomStorageVolumeMounts generates VolumeMounts for custom storage specs
func buildCustomStorageVolumeMounts(storages []agentv1alpha1.StorageSpec) []corev1.VolumeMount {
	var mounts []corev1.VolumeMount
	for _, storage := range storages {
		if storage.MountPath != "" {
			mounts = append(mounts, corev1.VolumeMount{
				Name:      storage.Name + "-vol",
				MountPath: storage.MountPath,
				SubPath:   storage.SubPath,
				ReadOnly:  storage.ReadOnly,
			})
		}
	}
	return mounts
}

// buildCustomStorageVolumes generates Pod Volumes for custom storage specs
func buildCustomStorageVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	if agent.Spec.Deployment == nil || len(agent.Spec.Deployment.Storages) == 0 {
		return nil
	}
	useSts := useStatefulSet(agent)
	var vols []corev1.Volume
	for _, storage := range agent.Spec.Deployment.Storages {
		if useSts && isRWOStorage(storage) {
			continue // Handled by VolumeClaimTemplates in StatefulSet
		}
		claimName := storage.Name
		vols = append(vols, corev1.Volume{
			Name: storage.Name + "-vol",
			VolumeSource: corev1.VolumeSource{
				PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
					ClaimName: claimName,
					ReadOnly:  storage.ReadOnly,
				},
			},
		})
	}
	return vols
}

// buildPodTemplateSpec generates the shared PodTemplateSpec for Deployment and StatefulSet
func buildPodTemplateSpec(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash, policyHash string) corev1.PodTemplateSpec {
	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
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
	var podAnnotations map[string]string
	if agent.Spec.Deployment != nil {
		initContainers = agent.Spec.Deployment.InitContainers
		sidecars = agent.Spec.Deployment.Sidecars
		sidecarVolumes = agent.Spec.Deployment.SidecarVolumes
		extraVolumes = agent.Spec.Deployment.ExtraVolumes
		podAnnotations = agent.Spec.Deployment.PodAnnotations
	}

	homeDir := "/opt/data"
	if agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.AgentHome != "" {
		homeDir = agent.Spec.Harness.Hermes.AgentHome
	}
	// The data PVC survives upgrades. Remove credential files written by older,
	// credentialed deployments before the agent sandbox can mount the PVC.
	initContainers = append([]corev1.Container{buildSandboxCredentialCleanup(image, pullPolicy)}, initContainers...)

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
			Name:  "PLATFORM_AGENT_PLUGINS_DEBUG",
			Value: pluginsDebugVal,
		},
		{
			Name:  "API_SERVER_ENABLED",
			Value: "true",
		},
		{
			Name:  "API_SERVER_HOST",
			Value: "127.0.0.1",
		},
		{
			// The sidecar authenticates external callers and replaces their bearer
			// key with this non-secret loopback sentinel.
			Name:  "API_SERVER_KEY",
			Value: "cluster-internal-trusted",
		},
		{
			Name:  "SESSION_KV_DB_PATH",
			Value: sessionKVDBPath,
		},
	}

	envVars = append(envVars, otelTelemetryEnvVars("platform", agent.Name, agent.Namespace)...)
	if agent.Spec.Deployment != nil {
		envVars = mergeEnvVars(envVars, safeSandboxEnvOverrides(agent.Spec.Deployment.Env))
	}

	if agent.Spec.Deployment != nil && len(agent.Spec.Deployment.BrowserArgs) > 0 {
		envVars = append(envVars, corev1.EnvVar{
			Name:  "AGENT_BROWSER_ARGS",
			Value: strings.Join(agent.Spec.Deployment.BrowserArgs, " "),
		})
	}

	if agent.Spec.Harness != nil {
		if agent.Spec.Harness.ProjectID != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name:  "GKE_PROJECT_ID",
				Value: agent.Spec.Harness.ProjectID,
			})
		}
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
		if agent.Spec.Harness.ProjectID != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name:  "GCP_PROJECT_ID",
				Value: agent.Spec.Harness.ProjectID,
			})
		}
		if agent.Spec.Harness.ProjectID != "" && agent.Spec.Harness.Location != "" && agent.Spec.Harness.ClusterName != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name: "KUBE_CONTEXT_NAME",
				Value: fmt.Sprintf(
					"gke_%s_%s_%s",
					agent.Spec.Harness.ProjectID,
					agent.Spec.Harness.Location,
					agent.Spec.Harness.ClusterName,
				),
			})
		}
		envVars = append(envVars, corev1.EnvVar{
			Name:  "KUBE_DEFAULT_NAMESPACE",
			Value: agent.Namespace,
		})
	}

	if integration := agent.Spec.Integration; integration != nil {
		if gchat := integration.GoogleChat; gchat != nil && gchat.Enabled != nil && *gchat.Enabled {
			envVars = append(envVars, []corev1.EnvVar{
				{
					Name:  "GOOGLE_CHAT_RELAY_URL",
					Value: fmt.Sprintf("http://127.0.0.1:%d", credentialProxyPort),
				},
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
			envVars = append(envVars, corev1.EnvVar{
				Name:  "SLACK_RELAY_URL",
				Value: fmt.Sprintf("http://127.0.0.1:%d", credentialProxyPort),
			})
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

	if replicas > 1 {
		envVars = append(envVars,
			corev1.EnvVar{
				Name:  "ENABLE_LEADER_ELECTION",
				Value: "true",
			},
			corev1.EnvVar{
				Name:  "LEADER_ELECTION_LEASE_NAME",
				Value: agent.Name + "-leader",
			},
			corev1.EnvVar{
				Name:  "LEADER_ELECTION_NAMESPACE",
				Value: agent.Namespace,
			},
		)
	}

	envVars = append(envVars, corev1.EnvVar{
		Name:  "CREDENTIAL_PROXY_URL",
		Value: fmt.Sprintf("http://127.0.0.1:%d", credentialProxyPort),
	})
	envVars = append(envVars, corev1.EnvVar{
		Name:  "PATH",
		Value: "/opt/credential-proxy/bin:/opt/hermes/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
	})
	envVars = append(envVars, corev1.EnvVar{
		Name:  "PYTHONPATH",
		Value: "/opt/defaults/scripts",
	})

	dashboardEnabled := isDashboardEnabled(agent)

	var shareProcessNamespace *bool
	if dashboardEnabled {
		shareProcessNamespace = ptr.To(true)
	}

	var runtimeClassName *string
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.Availability != nil {
		runtimeClassName = agent.Spec.Deployment.Availability.RuntimeClassName
	}

	containers := buildBaseContainers(agent, image, envVars)
	containers = append(containers, buildCredentialProxySidecar(agent, homeDir))
	defaultAnnotations := map[string]string{
		"kubeagents.x-k8s.io/config-hash":            configHash,
		"kubeagents.x-k8s.io/fluent-bit-config-hash": fluentBitHash,
		"kubeagents.x-k8s.io/settings-config-hash":   settingsConfigHash,
		"kubeagents.x-k8s.io/proxy-policy-hash":      policyHash,
	}
	if len(sidecars) > 0 {
		containers = append(containers, sidecars...)
	}

	volumes := buildDefaultVolumes(agent)
	volumes = append(volumes, buildCustomStorageVolumes(agent)...)
	volumes = append(volumes, buildCredentialProxyVolumes(agent)...)
	if len(sidecarVolumes) > 0 {
		volumes = append(volumes, sidecarVolumes...)
	}
	if len(extraVolumes) > 0 {
		volumes = append(volumes, extraVolumes...)
	}

	var affinity *corev1.Affinity
	var nodeSelector map[string]string
	var tolerations []corev1.Toleration

	if agent.Spec.Deployment != nil && agent.Spec.Deployment.Availability != nil {
		affinity = agent.Spec.Deployment.Availability.Affinity
		nodeSelector = agent.Spec.Deployment.Availability.NodeSelector
		tolerations = agent.Spec.Deployment.Availability.Tolerations
	}

	return corev1.PodTemplateSpec{
		ObjectMeta: metav1.ObjectMeta{
			Labels: map[string]string{
				"app": agent.Name + "-gateway",
				"kubeagents.x-k8s.io/has-credential-proxy": "true",
			},
			Annotations: mergeAnnotations(defaultAnnotations, podAnnotations),
		},
		Spec: corev1.PodSpec{
			ShareProcessNamespace:        shareProcessNamespace,
			RuntimeClassName:             runtimeClassName,
			InitContainers:               initContainers,
			ServiceAccountName:           saName,
			AutomountServiceAccountToken: ptr.To(false),
			SecurityContext: &corev1.PodSecurityContext{
				FSGroup: &fsGroup,
				// UID 10000 matches canonical 'hermes' runtime user in upstream image (NousResearch/hermes-agent Dockerfile line 92)
				RunAsUser:      ptr.To(int64(10000)),
				RunAsNonRoot:   ptr.To(true),
				SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
			},
			Affinity:     affinity,
			NodeSelector: nodeSelector,
			Tolerations:  tolerations,
			Containers:   containers,
			Volumes:      volumes,
		},
	}
}

// buildDeployment generates the Deployment manifest for the agent payload
func buildDeployment(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash, policyHash string) *appsv1.Deployment {
	replicas, strategy := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	podTemplate := buildPodTemplateSpec(agent, configHash, fluentBitHash, settingsConfigHash, policyHash)

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
				"kubeagents.x-k8s.io/has-credential-proxy": "true",
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
			Template: podTemplate,
		},
	}
}

// buildStatefulSet generates the StatefulSet manifest for PlatformAgent when RWO custom storage is used with multiple replicas
func buildStatefulSet(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash, policyHash string) *appsv1.StatefulSet {
	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	podTemplate := buildPodTemplateSpec(agent, configHash, fluentBitHash, settingsConfigHash, policyHash)
	vcts := buildRWOVolumeClaimTemplates(agent)

	return &appsv1.StatefulSet{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "apps/v1",
			Kind:       "StatefulSet",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-gateway",
			Namespace: agent.Namespace,
			Labels: map[string]string{
				"app": agent.Name + "-gateway",
			},
		},
		Spec: appsv1.StatefulSetSpec{
			Replicas:    &replicas,
			ServiceName: agent.Name,
			Selector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"app": agent.Name + "-gateway",
				},
			},
			Template:             podTemplate,
			VolumeClaimTemplates: vcts,
		},
	}
}

// buildDefaultVolumeMounts generates default volume mounts for PlatformAgent
func buildDefaultVolumeMounts(homeDir string) []corev1.VolumeMount {
	return []corev1.VolumeMount{
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
			Name:      "platform-agent-config-vol",
			MountPath: fmt.Sprintf("%s/leader_elect.py", homeDir),
			SubPath:   "leader_elect.py",
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
	}
}

func buildSandboxCredentialCleanup(image string, pullPolicy corev1.PullPolicy) corev1.Container {
	return corev1.Container{
		Name:            "sandbox-credential-cleanup",
		Image:           image,
		ImagePullPolicy: pullPolicy,
		Command:         []string{"sh", "-ec"},
		Args: []string{`rm -rf -- \
  /workspace/home/.config/gcloud \
  /workspace/home/.config/gh \
  /workspace/home/.aws/credentials \
  /workspace/home/.aws/cli/cache \
  /workspace/home/.aws/sso/cache \
  /workspace/home/.azure \
  /workspace/home/.docker/config.json \
  /workspace/home/.git-credentials \
  /workspace/home/.hermes/.env \
  /workspace/home/.kube/config \
  /workspace/home/.netrc \
  /workspace/home/.npmrc \
  /workspace/home/.pypirc`},
		VolumeMounts: []corev1.VolumeMount{{Name: "platform-agent-data-vol", MountPath: "/workspace"}},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false),
			ReadOnlyRootFilesystem:   ptr.To(true),
			Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
	}
}

func buildCredentialProxyPolicyConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-credential-proxy-policy",
			Namespace: agent.Namespace,
		},
		Data: map[string]string{"policy.json": credentialProxyPolicyJSON},
	}
}

// buildCredentialProxySidecar returns the Envoy-fronted credential runtime.
// Its environment and volume mounts are intentionally disjoint from the agent
// container even though both containers share a Pod network namespace.
func buildCredentialProxySidecar(agent *agentv1alpha1.PlatformAgent, homeDir string) corev1.Container {
	image := resolveCredentialProxyImage(agent.Spec.Deployment)
	pullPolicy := corev1.PullAlways
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.ImagePullPolicy != nil {
		pullPolicy = *agent.Spec.Deployment.ImagePullPolicy
	}
	envVars := buildCredentialProxyEnv(agent)
	envVars = append(envVars, corev1.EnvVar{Name: "CREDENTIAL_PROXY_WORKSPACE_ROOT", Value: homeDir})
	return corev1.Container{
		Name:            "envoy-credential-proxy",
		Image:           image,
		ImagePullPolicy: pullPolicy,
		Command:         []string{"/usr/local/bin/envoy-credential-sidecar"},
		Env:             envVars,
		Ports: []corev1.ContainerPort{
			{Name: "cred-proxy", ContainerPort: credentialProxyPort},
			{Name: "proxy-api", ContainerPort: 8643},
		},
		ReadinessProbe: &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{Exec: &corev1.ExecAction{Command: []string{
				"curl", "--fail", "--silent", "--show-error", "http://127.0.0.1:8765/healthz",
			}}},
			InitialDelaySeconds: 5,
			PeriodSeconds:       15,
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse("256Mi")},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU: resource.MustParse("2"), corev1.ResourceMemory: resource.MustParse("2Gi"), corev1.ResourceEphemeralStorage: resource.MustParse("2Gi"),
			},
		},
		VolumeMounts: []corev1.VolumeMount{
			{Name: "credential-proxy-policy", MountPath: "/etc/credential-proxy/policy.json", SubPath: "policy.json", ReadOnly: true},
			{Name: "credential-proxy-tmp", MountPath: "/tmp"},
			{Name: "credential-proxy-state", MountPath: "/var/lib/credential-proxy"},
			{Name: "credential-proxy-runtime", MountPath: "/var/run/credential-proxy"},
			{Name: "event-watcher-kubeconfig", MountPath: "/var/run/event-watcher"},
			{Name: "credential-proxy-ksa-token", MountPath: "/var/run/secrets/kubeagents/serviceaccount", ReadOnly: true},
			{Name: "platform-agent-data-vol", MountPath: homeDir},
		},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false), ReadOnlyRootFilesystem: ptr.To(true), Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
	}
}

func buildCredentialProxyEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	envVars := []corev1.EnvVar{
		{Name: "PLATFORM_AGENT_HOME", Value: "/tmp/credential-proxy"},
		{Name: "HOME", Value: "/tmp/credential-proxy/home"},
		{Name: "CREDENTIAL_PROXY_POLICY", Value: "/etc/credential-proxy/policy.json"},
		{Name: "CREDENTIAL_PROXY_STATE_DIR", Value: "/var/lib/credential-proxy"},
		{Name: "CREDENTIAL_PROXY_UNIX_SOCKET", Value: "/var/run/credential-proxy/backend.sock"},
		{Name: "KUBECONFIG", Value: "/var/run/event-watcher/watcher.config"},
		{Name: "KSA_TOKEN_FILE", Value: "/var/run/secrets/kubeagents/serviceaccount/token"},
		{Name: "TOKEN_BROKER_URL", Value: fmt.Sprintf("http://github-token-minter.%s.svc.cluster.local:8080/token", agent.Namespace)},
		{Name: "AGENT_API_PROXY_PORT", Value: "8643"},
		{Name: "AGENT_API_UPSTREAM_KEY", Value: "cluster-internal-trusted"},
	}
	apiServerSecretRef := defaultSecretRef(nil, defaultPlatformAgentSecrets, "API_SERVER_KEY")
	if harness := agent.Spec.Harness; harness != nil && harness.Hermes != nil && harness.Hermes.ApiServerSecretRef != nil {
		apiServerSecretRef = harness.Hermes.ApiServerSecretRef
	}
	envVars = append(envVars, corev1.EnvVar{
		Name: "API_SERVER_EXTERNAL_KEY",
		ValueFrom: &corev1.EnvVarSource{
			SecretKeyRef: apiServerSecretRef,
		},
	})
	if harness := agent.Spec.Harness; harness != nil && harness.ProjectID != "" && harness.Location != "" && harness.ClusterName != "" {
		envVars = append(envVars,
			corev1.EnvVar{Name: "GKE_PROJECT_ID", Value: harness.ProjectID}, corev1.EnvVar{Name: "GKE_CLUSTER_NAME", Value: harness.ClusterName}, corev1.EnvVar{Name: "GKE_LOCATION", Value: harness.Location},
			corev1.EnvVar{Name: "KUBE_CONTEXT_NAME", Value: fmt.Sprintf("gke_%s_%s_%s", harness.ProjectID, harness.Location, harness.ClusterName)}, corev1.EnvVar{Name: "KUBE_DEFAULT_NAMESPACE", Value: agent.Namespace},
			corev1.EnvVar{Name: "CREDENTIAL_PROXY_BOOTSTRAP_COMMAND", Value: `gcloud config set project "$GKE_PROJECT_ID" >/dev/null &&
gcloud container clusters get-credentials "$GKE_CLUSTER_NAME" --location "$GKE_LOCATION" --project "$GKE_PROJECT_ID" &&
kubectl config use-context "$KUBE_CONTEXT_NAME" >/dev/null &&
kubectl config set-context "$KUBE_CONTEXT_NAME" --namespace="$KUBE_DEFAULT_NAMESPACE" >/dev/null`},
		)
	}
	if integration := agent.Spec.Integration; integration != nil {
		if gchat := integration.GoogleChat; gchat != nil && gchat.Enabled != nil && *gchat.Enabled {
			envVars = append(envVars, corev1.EnvVar{Name: "GOOGLE_CHAT_PROJECT_ID", Value: gchat.ProjectID}, corev1.EnvVar{Name: "GOOGLE_CHAT_SUBSCRIPTION_NAME", Value: fmt.Sprintf("projects/%s/subscriptions/%s", gchat.ProjectID, gchat.SubscriptionName)})
		}
		if slack := integration.Slack; slack != nil && slack.Enabled != nil && *slack.Enabled {
			envVars = append(envVars,
				corev1.EnvVar{Name: "SLACK_BOT_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: defaultSecretRef(slack.BotTokenSecretRef, defaultPlatformAgentSecrets, "SLACK_BOT_TOKEN")}},
				corev1.EnvVar{Name: "SLACK_APP_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: defaultSecretRef(slack.AppTokenSecretRef, defaultPlatformAgentSecrets, "SLACK_APP_TOKEN")}},
			)
		}
	}
	if agent.Spec.Deployment != nil {
		envVars = mergeCredentialProxyEnv(envVars, agent.Spec.Deployment.Env)
	}
	return envVars
}

func mergeCredentialProxyEnv(managed, custom []corev1.EnvVar) []corev1.EnvVar {
	reserved := map[string]struct{}{
		"PATH": {}, "PYTHONPATH": {}, "ENV": {}, "BASH_ENV": {},
		"LD_PRELOAD": {}, "LD_LIBRARY_PATH": {},
		"KUBERNETES_SERVICE_HOST": {}, "KUBERNETES_SERVICE_PORT": {},
	}
	for _, env := range managed {
		reserved[env.Name] = struct{}{}
	}
	for _, name := range []string{
		"CREDENTIAL_PROXY_BOOTSTRAP_COMMAND",
		"CREDENTIAL_PROXY_MAX_OUTPUT_BYTES",
		"CREDENTIAL_PROXY_MAX_REQUEST_BYTES",
		"CREDENTIAL_PROXY_POLICY",
		"CREDENTIAL_PROXY_PORT",
		"CREDENTIAL_PROXY_STATE_DIR",
		"CREDENTIAL_PROXY_TIMEOUT_SECONDS",
		"CREDENTIAL_PROXY_UNIX_SOCKET",
		"CREDENTIAL_PROXY_WORKSPACE_ROOT",
		"KSA_TOKEN_FILE",
		"TOKEN_BROKER_URL",
	} {
		reserved[name] = struct{}{}
	}

	result := append([]corev1.EnvVar{}, managed...)
	for _, env := range custom {
		if _, found := reserved[env.Name]; !found {
			result = append(result, env)
		}
	}
	return result
}

// safeSandboxEnvOverrides preserves non-secret telemetry customization without
// copying arbitrary deployment environment variables into the agent sandbox.
func safeSandboxEnvOverrides(custom []corev1.EnvVar) []corev1.EnvVar {
	allowed := map[string]struct{}{
		"OTEL_EXPORTER_OTLP_ENDPOINT": {},
		"OTEL_EXPORTER_OTLP_PROTOCOL": {},
		"OTEL_RESOURCE_ATTRIBUTES":    {},
		"OTEL_SERVICE_NAME":           {},
	}
	var result []corev1.EnvVar
	for _, env := range custom {
		// Only literal telemetry settings are safe to copy. A ValueFrom source can
		// reference a Secret even when its environment variable name is allowlisted.
		if _, ok := allowed[env.Name]; ok && env.ValueFrom == nil {
			result = append(result, env)
		}
	}
	return result
}

func buildCredentialProxyVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	return []corev1.Volume{
		{Name: "credential-proxy-policy", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: agent.Name + "-credential-proxy-policy"}}}},
		{Name: "credential-proxy-tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: ptr.To(resource.MustParse("2Gi"))}}},
		{Name: "credential-proxy-state", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: ptr.To(resource.MustParse("5Gi"))}}},
		{Name: "credential-proxy-runtime", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{Medium: corev1.StorageMediumMemory, SizeLimit: ptr.To(resource.MustParse("16Mi"))}}},
		{Name: "event-watcher-kubeconfig", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{Medium: corev1.StorageMediumMemory, SizeLimit: ptr.To(resource.MustParse("1Mi"))}}},
		{Name: "credential-proxy-ksa-token", VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: ptr.To(int32(0400)),
			Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
				Audience: "kubeagents-credential-proxy", ExpirationSeconds: ptr.To(int64(3600)), Path: "token",
			}}},
		}}},
		{Name: "event-watcher-ksa-token", VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: ptr.To(int32(0400)),
			Sources: []corev1.VolumeProjection{
				{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{ExpirationSeconds: ptr.To(int64(3600)), Path: "token"}},
				{ConfigMap: &corev1.ConfigMapProjection{
					LocalObjectReference: corev1.LocalObjectReference{Name: "kube-root-ca.crt"},
					Items:                []corev1.KeyToPath{{Key: "ca.crt", Path: "ca.crt"}},
				}},
				{DownwardAPI: &corev1.DownwardAPIProjection{Items: []corev1.DownwardAPIVolumeFile{{
					Path: "namespace", FieldRef: &corev1.ObjectFieldSelector{APIVersion: "v1", FieldPath: "metadata.namespace"},
				}}}},
			},
		}}},
	}
}

func resolveCredentialProxyImage(deployment *agentv1alpha1.DeploymentSpec) string {
	image := defaultPlatformAgentImage
	if deployment != nil && deployment.Image != "" {
		image = deployment.Image
	}
	lastSlash := strings.LastIndex(image, "/")
	prefix, name := "", image
	if lastSlash >= 0 {
		prefix, name = image[:lastSlash+1], image[lastSlash+1:]
	}
	suffix := ""
	if digest := strings.Index(name, "@"); digest >= 0 {
		suffix, name = name[digest:], name[:digest]
	} else if tag := strings.LastIndex(name, ":"); tag >= 0 {
		suffix, name = name[tag:], name[:tag]
	}
	if name == "platform-agent" {
		name = "credential-proxy"
	} else {
		name += "-credential-proxy"
	}
	if deployment != nil && deployment.Tag != nil && *deployment.Tag != "" {
		return prefix + name + ":" + *deployment.Tag
	}
	if suffix == "" {
		suffix = ":latest"
	}
	return prefix + name + suffix
}

// buildBaseContainers generates the base containers for PlatformAgent.
func buildBaseContainers(agent *agentv1alpha1.PlatformAgent, image string, envVars []corev1.EnvVar) []corev1.Container {
	homeDir := defaultAgentHome
	if agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.AgentHome != "" {
		homeDir = agent.Spec.Harness.Hermes.AgentHome
	}

	pullPolicy := corev1.PullAlways
	var extraVolumeMounts []corev1.VolumeMount
	var storages []agentv1alpha1.StorageSpec
	if agent.Spec.Deployment != nil {
		if agent.Spec.Deployment.ImagePullPolicy != nil {
			pullPolicy = *agent.Spec.Deployment.ImagePullPolicy
		}
		extraVolumeMounts = agent.Spec.Deployment.ExtraVolumeMounts
		storages = agent.Spec.Deployment.Storages
	}

	resources := resolveResources(agent.Spec.Deployment)

	volumeMounts := buildDefaultVolumeMounts(homeDir)
	if len(storages) > 0 {
		volumeMounts = append(volumeMounts, buildCustomStorageVolumeMounts(storages)...)
	}
	if len(extraVolumeMounts) > 0 {
		volumeMounts = append(volumeMounts, extraVolumeMounts...)
	}

	var command []string
	var args []string

	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	if replicas > 1 {
		command = []string{"/opt/hermes/.venv/bin/python3"}
		args = []string{fmt.Sprintf("%s/leader_elect.py", homeDir)}
	}

	clusterName := "platform-agent-host"
	if agent.Spec.Harness != nil {
		if agent.Spec.Harness.ClusterName != "" {
			clusterName = agent.Spec.Harness.ClusterName
		}
	}

	containers := []corev1.Container{
		{
			Name:            "platform-agent",
			Image:           image,
			ImagePullPolicy: pullPolicy,
			Command:         command,
			Args:            args,
			Ports: []corev1.ContainerPort{
				{
					Name:          "api",
					ContainerPort: 8642,
				},
			},
			Env:          envVars,
			Resources:    resources,
			VolumeMounts: volumeMounts,
			SecurityContext: &corev1.SecurityContext{
				AllowPrivilegeEscalation: ptr.To(false),
				Capabilities: &corev1.Capabilities{
					Drop: []corev1.Capability{"ALL"},
				},
			},
		},
	}

	if isDashboardEnabled(agent) {
		dashboardEnvVars := []corev1.EnvVar{
			{
				Name:  "PLATFORM_AGENT_HOME",
				Value: homeDir,
			},
			{
				Name:  "HOME",
				Value: strings.TrimSuffix(homeDir, "/") + "/home",
			},
			{
				Name:  "SESSION_KV_DB_PATH",
				Value: sessionKVDBPath,
			},
		}

		dashboardVolumeMounts := []corev1.VolumeMount{
			{
				Name:      "platform-agent-data-vol",
				MountPath: homeDir,
			},
			{
				Name:      "system-metadata",
				MountPath: path.Dir(sessionKVDBPath),
				SubPath:   "session",
			},
		}

		containers = append(containers, corev1.Container{
			Name:            "platform-agent-dashboard",
			Image:           image,
			ImagePullPolicy: pullPolicy,
			Args:            []string{"hermes", "dashboard"},
			Ports: []corev1.ContainerPort{
				{
					Name:          "dashboard",
					ContainerPort: 9119,
				},
			},
			Env: dashboardEnvVars,
			Resources: corev1.ResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("256m"),
					corev1.ResourceMemory: resource.MustParse("512Mi"),
				},
				Limits: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("1"),
					corev1.ResourceMemory: resource.MustParse("2Gi"),
				},
			},
			VolumeMounts: append(dashboardVolumeMounts, extraVolumeMounts...),
			SecurityContext: &corev1.SecurityContext{
				AllowPrivilegeEscalation: ptr.To(false),
				Capabilities: &corev1.Capabilities{
					Drop: []corev1.Capability{"ALL"},
				},
			},
		})
	}

	containers = append(containers, corev1.Container{
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
	})

	// Inject the k8s-event-watcher sidecar container to capture GKE warnings and stream them to the local REST bridge
	containers = append(containers, corev1.Container{
		Name:            "event-watcher",
		Image:           image,
		ImagePullPolicy: pullPolicy,
		Command: []string{
			"/usr/local/bin/k8s-event-watcher",
		},
		Args: []string{
			"--cluster-name=" + clusterName,
			"--daemon-url=http://127.0.0.1:8699",
			"--token-env=API_SERVER_KEY",
			"--owner=platform",
			"--reason=Failed,FailedToDrainNode,CrashLoopBackOff,BackOff,ImagePullBackOff,ErrImagePull,OOMKilled",
			"--kubeconfig=/var/run/event-watcher/watcher.config",
		},
		Env: []corev1.EnvVar{
			{
				Name:  "API_SERVER_KEY",
				Value: "cluster-internal-trusted",
			},
			{
				Name:  "HOME",
				Value: strings.TrimSuffix(homeDir, "/") + "/home",
			},
		},
		VolumeMounts: []corev1.VolumeMount{
			{Name: "event-watcher-kubeconfig", MountPath: "/var/run/event-watcher", ReadOnly: true},
			{Name: "event-watcher-ksa-token", MountPath: "/var/run/secrets/kubernetes.io/serviceaccount", ReadOnly: true},
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("50m"),
				corev1.ResourceMemory: resource.MustParse("64Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("200m"),
				corev1.ResourceMemory: resource.MustParse("128Mi"),
			},
		},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false),
			Capabilities: &corev1.Capabilities{
				Drop: []corev1.Capability{"ALL"},
			},
		},
	})

	return containers
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
	selector := map[string]string{
		"app": agent.Name + "-gateway",
	}

	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	if replicas > 1 {
		selector["kubeagents.io/is-leader"] = "true"
	}
	dashboardEnabled := isDashboardEnabled(agent)

	ports := []corev1.ServicePort{
		{
			Name:       "api",
			Port:       8642,
			TargetPort: intstr.FromInt32(8643),
		},
	}

	if dashboardEnabled {
		ports = append(ports, corev1.ServicePort{
			Name:       "dashboard",
			Port:       9119,
			TargetPort: intstr.FromString("dashboard"),
		})
	}

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
			Selector: selector,
			Ports:    ports,
		},
	}
}

// buildPlatformLeaderRole generates the Role manifest for leader election leases in the agent namespace
func buildPlatformLeaderRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.Role {
	return &rbacv1.Role{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "Role",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("kubeagents:leader:%s:%s", agent.Namespace, agent.Name),
			Namespace: agent.Namespace,
		},
		Rules: []rbacv1.PolicyRule{
			{
				APIGroups: []string{"coordination.k8s.io"},
				Resources: []string{"leases"},
				Verbs:     []string{"get", "list", "watch", "create", "update", "patch", "delete"},
			},
			{
				APIGroups: []string{""},
				Resources: []string{"pods"},
				Verbs:     []string{"get", "patch"},
			},
		},
	}
}

// buildLeaderRoleBinding generates the RoleBinding manifest for leader election in the agent namespace
func buildLeaderRoleBinding(agent *agentv1alpha1.PlatformAgent, bindingName, roleName string) *rbacv1.RoleBinding {
	saName := agent.Name
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		saName = agent.Spec.Security.ServiceAccountName
	}

	return &rbacv1.RoleBinding{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "RoleBinding",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      bindingName,
			Namespace: agent.Namespace,
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
			Kind:     "Role",
			Name:     roleName,
		},
	}
}

func isDashboardEnabled(agent *agentv1alpha1.PlatformAgent) bool {
	if agent != nil && agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.DashboardEnabled != nil {
		return *agent.Spec.Harness.Hermes.DashboardEnabled
	}
	return true
}

//go:embed leader_elect.py
var leaderElectScript string
