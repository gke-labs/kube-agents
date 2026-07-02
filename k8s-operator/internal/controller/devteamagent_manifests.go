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
	"fmt"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// buildDevTeamConfigMap generates the ConfigMap manifest containing config.yaml and SETTINGS.md for DevTeamAgent
func buildDevTeamConfigMap(agent *agentv1alpha1.DevTeamAgent) *corev1.ConfigMap {
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
			"config.yaml": renderDevTeamConfigYAML(agent),
			"SETTINGS.md": renderDevTeamSettingsMD(agent),
		},
	}
}

// renderDevTeamConfigYAML generates the YAML config payload for DevTeamAgent
func renderDevTeamConfigYAML(agent *agentv1alpha1.DevTeamAgent) string {
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
		Plugins struct {
			Enabled []string `json:"enabled"`
		} `json:"plugins"`
	}{}

	cfg.Model.Provider = "custom"
	cfg.Model.Default = "model-default"
	cfg.Model.Model = "model-default"
	cfg.Model.BaseURL = fmt.Sprintf("http://litellm.%s.svc.cluster.local/v1", agent.Namespace)
	cfg.Model.APIKey = "none"
	cfg.Terminal.Backend = "local"
	cfg.Terminal.Cwd = cwd
	cfg.MCPServers = map[string]any{
		"agent_common": map[string]any{
			"command": "/opt/hermes/.venv/bin/python3",
			"args":    []string{"/opt/data/scripts/agent_common_server.py"},
		},
		"developer_knowledge": map[string]any{
			"command": "node",
			"args":    []string{"/opt/mcp-remote/dist/proxy.js", "https://developerknowledge.googleapis.com/mcp"},
		},
	}
	cfg.PlatformToolsets = map[string][]string{
		"cli":        {"hermes-cli", "mcp-agent_common", "mcp-developer_knowledge"},
		"api_server": {"hermes-api-server", "mcp-agent_common", "mcp-developer_knowledge"},
	}
	cfg.Approvals.CronMode = "approve"
	cfg.Web.Backend = "ddgs"
	cfg.Plugins.Enabled = []string{"hermes_otel"}

	data, err := yaml.Marshal(cfg)
	if err != nil {
		return ""
	}
	return string(data)
}

// renderDevTeamSettingsMD generates the SETTINGS.md GKE Scope configuration payload for DevTeamAgent
func renderDevTeamSettingsMD(agent *agentv1alpha1.DevTeamAgent) string {
	clusterName := ""
	location := ""
	namespace := ""
	if agent.Spec.Harness != nil {
		clusterName = agent.Spec.Harness.ClusterName
		location = agent.Spec.Harness.Location
		namespace = agent.Spec.Harness.Namespace
	}
	return fmt.Sprintf(`# GKE Scope Configuration
- **Cluster Name:** %s
- **Cluster Location:** %s
- **Namespace:** %s
`, clusterName, location, namespace)
}

// buildDevTeamPVC generates the PVC manifest for DevTeamAgent data persistence
func buildDevTeamPVC(agent *agentv1alpha1.DevTeamAgent) *corev1.PersistentVolumeClaim {
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

// buildDevTeamDeployment generates the Deployment manifest for DevTeamAgent
func buildDevTeamDeployment(agent *agentv1alpha1.DevTeamAgent, configHash, fluentBitHash string) *appsv1.Deployment {
	replicas := int32(1)
	// UID/GID 10000 matches the canonical unprivileged 'hermes' runtime user created in NousResearch/hermes-agent upstream Dockerfile
	fsGroup := int64(10000)

	saName := agent.Name
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		saName = agent.Spec.Security.ServiceAccountName
	}

	image := resolveAgentImage(agent.Spec.Deployment, defaultDevTeamAgentImage)

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
			Name:  "DEVTEAM_AGENT_HOME",
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
		{
			Name:  "API_SERVER_ENABLED",
			Value: "true",
		},
		{
			Name:  "API_SERVER_HOST",
			Value: "0.0.0.0",
		},
		{
			Name:  "PLATFORM_API_URL",
			Value: "http://platform-agent.kubeagents-system.svc.cluster.local:8642/v1",
		},
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
		if agent.Spec.Harness.Namespace != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name:  "GKE_NAMESPACE",
				Value: agent.Spec.Harness.Namespace,
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
						FSGroup: &fsGroup,
						// UID 10000 matches canonical 'hermes' runtime user in upstream image (NousResearch/hermes-agent Dockerfile line 92)
						RunAsUser:      ptr.To(int64(10000)),
						RunAsNonRoot:   ptr.To(true),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: []corev1.Container{
						{
							Name:            "devteam-agent",
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
									corev1.ResourceCPU:    resource.MustParse("100m"),
									corev1.ResourceMemory: resource.MustParse("512Mi"),
								},
								Limits: corev1.ResourceList{
									corev1.ResourceCPU:    resource.MustParse("500m"),
									corev1.ResourceMemory: resource.MustParse("1Gi"),
								},
							},
							VolumeMounts: []corev1.VolumeMount{
								{
									Name:      "devteam-agent-data-vol",
									MountPath: homeDir,
								},
								{
									Name:      "devteam-agent-config-vol",
									MountPath: fmt.Sprintf("%s/config.yaml", homeDir),
									SubPath:   "config.yaml",
								},
								{
									Name:      "devteam-agent-config-vol",
									MountPath: fmt.Sprintf("%s/SETTINGS.md", homeDir),
									SubPath:   "SETTINGS.md",
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
									Name:      "devteam-agent-data-vol",
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
							Name: "devteam-agent-data-vol",
							VolumeSource: corev1.VolumeSource{
								PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
									ClaimName: agent.Name + "-data",
								},
							},
						},
						{
							Name: "devteam-agent-config-vol",
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

// buildDevTeamFluentBitConfigMap generates the ConfigMap manifest containing fluent-bit.conf
func buildDevTeamFluentBitConfigMap(agent *agentv1alpha1.DevTeamAgent) *corev1.ConfigMap {
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

// buildDevTeamService generates the Service manifest for DevTeamAgent
func buildDevTeamService(agent *agentv1alpha1.DevTeamAgent) *corev1.Service {
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
