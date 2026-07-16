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
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func TestBuildConfigMap(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				Hermes: &agentv1alpha1.HermesSpec{
					AgentHome: "/custom/home",
				},
			},
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				GoogleChat: &agentv1alpha1.GoogleChatSpec{
					Enabled: ptr.To(true),
				},
			},
		},
	}

	cm := buildConfigMap(agent)
	if cm.Name != "test-agent-config" {
		t.Errorf("expected configmap name test-agent-config, got %s", cm.Name)
	}

	yamlContent := cm.Data["config.yaml"]
	if !strings.Contains(yamlContent, "provider: custom") {
		t.Errorf("expected config to contain provider: custom, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "default: model-default") {
		t.Errorf("expected config to contain default: model-default, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "model: model-default") {
		t.Errorf("expected config to contain model: model-default, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "base_url: http://litellm.test-ns.svc.cluster.local/v1") {
		t.Errorf("expected config to contain correct base_url, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "api_key: none") {
		t.Errorf("expected config to contain api_key: none, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "cwd: /custom/home") {
		t.Errorf("expected config to contain custom home path, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "enabled: true") {
		t.Errorf("expected config to enable google_chat platform, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "mcp_servers:") {
		t.Errorf("expected config to contain mcp_servers, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "platform_toolsets:") {
		t.Errorf("expected config to contain platform_toolsets, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "cron_mode: approve") {
		t.Errorf("expected config to contain cron_mode: approve, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "backend: ddgs") {
		t.Errorf("expected config to contain web backend: ddgs, got:\n%s", yamlContent)
	}
}

func TestBuildConfigMap_MemoryConfig(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "memory-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				Memory: &agentv1alpha1.MemorySpec{
					MemoryEnabled:      ptr.To(true),
					Provider:           "custom_memory",
					UserProfileEnabled: ptr.To(true),
				},
			},
		},
	}

	cm := buildConfigMap(agent)
	yamlContent := cm.Data["config.yaml"]
	if !strings.Contains(yamlContent, "memory_enabled: true") {
		t.Errorf("expected config to contain memory_enabled: true, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "provider: custom_memory") {
		t.Errorf("expected config to contain provider: custom_memory, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "user_profile_enabled: true") {
		t.Errorf("expected config to contain user_profile_enabled: true, got:\n%s", yamlContent)
	}
}

func TestDisplayMode(t *testing.T) {
	// Test Default (Quiet) Mode
	defaultAgent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "quiet-agent", Namespace: "ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				GoogleChat: &agentv1alpha1.GoogleChatSpec{
					Mode: "default",
				},
			},
		},
	}
	defaultConfig := buildConfigMap(defaultAgent).Data["config.yaml"]
	if !strings.Contains(defaultConfig, "tool_progress: \"off\"") || !strings.Contains(defaultConfig, "memory_notifications: \"off\"") {
		t.Errorf("expected default mode to turn off tool_progress and memory_notifications, got:\n%s", defaultConfig)
	}

	// Test Debug Mode
	debugAgent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "debug-agent", Namespace: "ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				GoogleChat: &agentv1alpha1.GoogleChatSpec{
					Mode: "debug",
				},
			},
		},
	}
	debugConfig := buildConfigMap(debugAgent).Data["config.yaml"]
	if !strings.Contains(debugConfig, "tool_progress: all") || !strings.Contains(debugConfig, "memory_notifications: verbose") {
		t.Errorf("expected debug mode to enable all tool_progress and verbose memory_notifications, got:\n%s", debugConfig)
	}
}

func TestBuildPVC(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	pvc := buildPVC(agent)
	if pvc.Name != "test-agent-data" {
		t.Errorf("expected PVC name test-agent-data, got %s", pvc.Name)
	}
	storageReq := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
	if storageReq.String() != "10Gi" {
		t.Errorf("expected storage request 10Gi, got %s", storageReq.String())
	}
}

func TestBuildSystemPVC(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	pvc := buildSystemPVC(agent)
	if pvc.Name != "system-metadata" {
		t.Errorf("expected PVC name system-metadata, got %s", pvc.Name)
	}
	storageReq := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
	if storageReq.String() != "1Gi" {
		t.Errorf("expected storage request 1Gi, got %s", storageReq.String())
	}
}

func TestBuildDeployment(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "my-agent",
			Namespace: "my-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					RuntimeClassName: ptr.To("gvisor"),
					Image:            "gcr.io/my-proj/agent",
					Tag:              ptr.To("v1.0.0"),
					ImagePullPolicy:  ptr.To(corev1.PullAlways),
					BrowserArgs:      []string{"--no-sandbox", "--disable-gpu"},
					Env: []corev1.EnvVar{
						{
							Name:  "CUSTOM_VAR",
							Value: "custom-value",
						},
						{
							Name:  "PLATFORM_AGENT_DASHBOARD", // Overriding default
							Value: "0",
						},
						{
							Name:  "CUSTOM_VAR", // Duplicate custom var, should override previous
							Value: "new-custom-value",
						},
					},
					InitContainers: []corev1.Container{
						{
							Name:  "init-git",
							Image: "git-image:latest",
						},
						{
							Name:  "init-bootstrap",
							Image: "busybox:1.36",
						},
					},
					Sidecars: []corev1.Container{
						{
							Name:  "my-sidecar",
							Image: "sidecar-image:latest",
						},
					},
					SidecarVolumes: []corev1.Volume{
						{
							Name: "sidecar-vol",
							VolumeSource: corev1.VolumeSource{
								EmptyDir: &corev1.EmptyDirVolumeSource{},
							},
						},
					},
					ExtraVolumes: []corev1.Volume{
						{
							Name: "extra-vol",
							VolumeSource: corev1.VolumeSource{
								EmptyDir: &corev1.EmptyDirVolumeSource{},
							},
						},
					},
					ExtraVolumeMounts: []corev1.VolumeMount{
						{
							Name:      "extra-vol",
							MountPath: "/extra/path",
						},
					},
				},
				Security: &agentv1alpha1.SecuritySpec{
					ServiceAccountName: "custom-sa",
				},
			},
			Harness: &agentv1alpha1.HarnessSpec{
				ClusterName: "gke-cluster",
				Location:    "us-east1",
				Hermes: &agentv1alpha1.HermesSpec{
					DashboardEnabled: ptr.To(true),
					PluginsDebug:     ptr.To(false),
					AgentHome:        "/var/agent",
					ApiServerSecretRef: &corev1.SecretKeySelector{
						LocalObjectReference: corev1.LocalObjectReference{Name: "secrets"},
						Key:                  "api-key",
					},
				},
			},
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				IntegrationSpec: agentv1alpha1.IntegrationSpec{
					GitHub: &agentv1alpha1.GitHubSpec{
						GitRepo: "https://github.com/my-org/my-repo.git",
					},
				},
				GoogleChat: &agentv1alpha1.GoogleChatSpec{
					Enabled:          ptr.To(true),
					ProjectID:        "my-gcp-project",
					SubscriptionName: "chat-sub",
					AllowedUsers:     []string{"alice", "bob"},
					HomeChannel:      "spaces/123",
				},
			},
		},
	}

	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012")

	if dep.Name != "my-agent-gateway" {
		t.Errorf("expected deployment name my-agent-gateway, got %s", dep.Name)
	}

	if dep.Spec.Template.Annotations["kubeagents.x-k8s.io/config-hash"] != "abcd1234" {
		t.Errorf("expected config-hash annotation to be abcd1234, got %s", dep.Spec.Template.Annotations["kubeagents.x-k8s.io/config-hash"])
	}

	if dep.Spec.Template.Annotations["kubeagents.x-k8s.io/fluent-bit-config-hash"] != "efgh5678" {
		t.Errorf("expected fluent-bit-config-hash annotation to be efgh5678, got %s", dep.Spec.Template.Annotations["kubeagents.x-k8s.io/fluent-bit-config-hash"])
	}

	if dep.Spec.Template.Annotations["kubeagents.x-k8s.io/settings-config-hash"] != "ijkl9012" {
		t.Errorf("expected settings-config-hash annotation to be ijkl9012, got %s", dep.Spec.Template.Annotations["kubeagents.x-k8s.io/settings-config-hash"])
	}

	if dep.Spec.Template.Spec.RuntimeClassName == nil || *dep.Spec.Template.Spec.RuntimeClassName != "gvisor" {
		t.Errorf("expected RuntimeClassName gvisor, got %v", dep.Spec.Template.Spec.RuntimeClassName)
	}

	if len(dep.Spec.Template.Spec.Containers) != 3 {
		t.Errorf("expected 3 containers, got %d", len(dep.Spec.Template.Spec.Containers))
	} else {
		sidecarC := dep.Spec.Template.Spec.Containers[2]
		if sidecarC.Name != "my-sidecar" {
			t.Errorf("expected sidecar name my-sidecar, got %s", sidecarC.Name)
		}
		if sidecarC.Image != "sidecar-image:latest" {
			t.Errorf("expected sidecar image sidecar-image:latest, got %s", sidecarC.Image)
		}
	}

	if len(dep.Spec.Template.Spec.InitContainers) != 2 {
		t.Errorf("expected 2 init containers, got %d", len(dep.Spec.Template.Spec.InitContainers))
	} else {
		initC1 := dep.Spec.Template.Spec.InitContainers[0]
		if initC1.Name != "init-git" {
			t.Errorf("expected first init container name init-git, got %s", initC1.Name)
		}
		if initC1.Image != "git-image:latest" {
			t.Errorf("expected first init container image git-image:latest, got %s", initC1.Image)
		}

		initC2 := dep.Spec.Template.Spec.InitContainers[1]
		if initC2.Name != "init-bootstrap" {
			t.Errorf("expected second init container name init-bootstrap, got %s", initC2.Name)
		}
		if initC2.Image != "busybox:1.36" {
			t.Errorf("expected second init container image busybox:1.36, got %s", initC2.Image)
		}
	}

	container := dep.Spec.Template.Spec.Containers[0]
	if container.Image != "gcr.io/my-proj/agent:v1.0.0" {
		t.Errorf("expected container image gcr.io/my-proj/agent:v1.0.0, got %s", container.Image)
	}

	// Verify env vars
	envMap := make(map[string]corev1.EnvVar)
	seen := make(map[string]bool)
	for _, env := range container.Env {
		if seen[env.Name] {
			t.Errorf("duplicate env var found: %s", env.Name)
		}
		seen[env.Name] = true
		envMap[env.Name] = env
	}

	if envMap["PLATFORM_AGENT_HOME"].Value != "/var/agent" {
		t.Errorf("expected PLATFORM_AGENT_HOME /var/agent, got %s", envMap["PLATFORM_AGENT_HOME"].Value)
	}
	if envMap["HOME"].Value != "/var/agent/home" {
		t.Errorf("expected HOME /var/agent/home, got %s", envMap["HOME"].Value)
	}
	if envMap["PLATFORM_AGENT_DASHBOARD"].Value != "0" {
		t.Errorf("expected PLATFORM_AGENT_DASHBOARD to be overridden to 0, got %s", envMap["PLATFORM_AGENT_DASHBOARD"].Value)
	}
	if envMap["PLATFORM_AGENT_PLUGINS_DEBUG"].Value != "0" {
		t.Errorf("expected PLATFORM_AGENT_PLUGINS_DEBUG 0, got %s", envMap["PLATFORM_AGENT_PLUGINS_DEBUG"].Value)
	}
	if envMap["CUSTOM_VAR"].Value != "new-custom-value" {
		t.Errorf("expected CUSTOM_VAR new-custom-value, got %s", envMap["CUSTOM_VAR"].Value)
	}
	if envMap["AGENT_BROWSER_ARGS"].Value != "--no-sandbox --disable-gpu" {
		t.Errorf("expected AGENT_BROWSER_ARGS --no-sandbox --disable-gpu, got %s", envMap["AGENT_BROWSER_ARGS"].Value)
	}
	if envMap["TOKEN_BROKER_URL"].Value != "http://github-token-minter.my-ns.svc.cluster.local:8080/token" {
		t.Errorf("expected TOKEN_BROKER_URL http://github-token-minter.my-ns.svc.cluster.local:8080/token, got %s", envMap["TOKEN_BROKER_URL"].Value)
	}
	if envMap["GKE_CLUSTER_NAME"].Value != "gke-cluster" {
		t.Errorf("expected GKE_CLUSTER_NAME gke-cluster, got %s", envMap["GKE_CLUSTER_NAME"].Value)
	}
	if envMap["GKE_LOCATION"].Value != "us-east1" {
		t.Errorf("expected GKE_LOCATION us-east1, got %s", envMap["GKE_LOCATION"].Value)
	}
	if envMap["API_SERVER_KEY"].ValueFrom.SecretKeyRef.Name != "secrets" {
		t.Errorf("expected API_SERVER_KEY SecretRef secrets, got %s", envMap["API_SERVER_KEY"].ValueFrom.SecretKeyRef.Name)
	}
	if _, ok := envMap["GEMINI_API_KEY"]; ok {
		t.Errorf("expected GEMINI_API_KEY to not be set on platform agent container")
	}
	if envMap["GOOGLE_CHAT_PROJECT_ID"].Value != "my-gcp-project" {
		t.Errorf("expected GOOGLE_CHAT_PROJECT_ID my-gcp-project, got %s", envMap["GOOGLE_CHAT_PROJECT_ID"].Value)
	}
	if envMap["GOOGLE_CHAT_SUBSCRIPTION_NAME"].Value != "projects/my-gcp-project/subscriptions/chat-sub" {
		t.Errorf("expected GOOGLE_CHAT_SUBSCRIPTION_NAME project sub, got %s", envMap["GOOGLE_CHAT_SUBSCRIPTION_NAME"].Value)
	}
	if envMap["GOOGLE_CHAT_ALLOWED_USERS"].Value != "alice,bob" {
		t.Errorf("expected GOOGLE_CHAT_ALLOWED_USERS alice,bob, got %s", envMap["GOOGLE_CHAT_ALLOWED_USERS"].Value)
	}
	if _, ok := envMap["GOOGLE_CHAT_ALLOW_ALL_USERS"]; ok {
		t.Errorf("expected GOOGLE_CHAT_ALLOW_ALL_USERS not to be set when allowed users is populated")
	}
	if envMap["API_SERVER_ENABLED"].Value != "true" {
		t.Errorf("expected API_SERVER_ENABLED true, got %s", envMap["API_SERVER_ENABLED"].Value)
	}
	if envMap["API_SERVER_HOST"].Value != "0.0.0.0" {
		t.Errorf("expected API_SERVER_HOST 0.0.0.0, got %s", envMap["API_SERVER_HOST"].Value)
	}
	if envMap["SESSION_KV_DB_PATH"].Value != "/var/lib/kube-agents/session/session_kv.db" {
		t.Errorf("expected SESSION_KV_DB_PATH /var/lib/kube-agents/session/session_kv.db, got %s", envMap["SESSION_KV_DB_PATH"].Value)
	}

	// Verify volume mounts
	mountsMap := make(map[string]corev1.VolumeMount)
	for _, m := range container.VolumeMounts {
		mountsMap[m.Name] = m
	}
	if _, ok := mountsMap["settings-volume"]; !ok {
		t.Errorf("expected settings-volume mount, not found")
	} else {
		m := mountsMap["settings-volume"]
		if m.MountPath != "/var/agent/SETTINGS.md" {
			t.Errorf("expected settings-volume mount path /var/agent/SETTINGS.md, got %s", m.MountPath)
		}
		if m.SubPath != "SETTINGS.md" {
			t.Errorf("expected settings-volume subpath SETTINGS.md, got %s", m.SubPath)
		}
		if !m.ReadOnly {
			t.Errorf("expected settings-volume to be read-only")
		}
	}
	if _, ok := mountsMap["system-metadata"]; !ok {
		t.Errorf("expected system-metadata mount, not found")
	} else if mountsMap["system-metadata"].MountPath != "/var/lib/kube-agents/session" {
		t.Errorf("expected system-metadata mount path /var/lib/kube-agents/session, got %s", mountsMap["system-metadata"].MountPath)
	} else if mountsMap["system-metadata"].SubPath != "session" {
		t.Errorf("expected system-metadata subpath session, got %s", mountsMap["system-metadata"].SubPath)
	}

	if _, ok := mountsMap["extra-vol"]; !ok {
		t.Errorf("expected extra-vol mount, not found")
	} else {
		m := mountsMap["extra-vol"]
		if m.MountPath != "/extra/path" {
			t.Errorf("expected extra-vol mount path /extra/path, got %s", m.MountPath)
		}
	}

	// Verify Fluent Bit container
	fbContainer := dep.Spec.Template.Spec.Containers[1]
	if fbContainer.Name != "fluent-bit" {
		t.Errorf("expected container name fluent-bit, got %s", fbContainer.Name)
	}
	if fbContainer.Image != "fluent/fluent-bit:5.0.7" {
		t.Errorf("expected fluent-bit image fluent/fluent-bit:5.0.7, got %s", fbContainer.Image)
	}

	// Verify volumes
	volumesMap := make(map[string]corev1.Volume)
	for _, vol := range dep.Spec.Template.Spec.Volumes {
		volumesMap[vol.Name] = vol
	}
	if _, ok := volumesMap["fluent-bit-config"]; !ok {
		t.Errorf("expected fluent-bit-config volume, not found")
	}
	if _, ok := volumesMap["fluent-bit-state"]; !ok {
		t.Errorf("expected fluent-bit-state volume, not found")
	}
	if _, ok := volumesMap["system-metadata"]; !ok {
		t.Errorf("expected system-metadata volume, not found")
	} else {
		v := volumesMap["system-metadata"]
		if v.PersistentVolumeClaim == nil {
			t.Errorf("expected system-metadata to be a PVC")
		} else if v.PersistentVolumeClaim.ClaimName != "system-metadata" {
			t.Errorf("expected system-metadata claim system-metadata, got %s", v.PersistentVolumeClaim.ClaimName)
		}
	}

	if _, ok := volumesMap["settings-volume"]; !ok {
		t.Errorf("expected settings-volume, not found")
	} else {
		v := volumesMap["settings-volume"]
		if v.ConfigMap == nil {
			t.Errorf("expected settings-volume to be ConfigMap")
		} else {
			if v.ConfigMap.Name != "my-agent-settings" {
				t.Errorf("expected settings-volume ConfigMap name my-agent-settings, got %s", v.ConfigMap.Name)
			}
			if v.ConfigMap.DefaultMode == nil {
				t.Errorf("expected settings-volume ConfigMap DefaultMode to be set, got nil")
			} else if *v.ConfigMap.DefaultMode != int32(0644) {
				t.Errorf("expected settings-volume ConfigMap DefaultMode 0644, got %o", *v.ConfigMap.DefaultMode)
			}
		}
	}

	if _, ok := volumesMap["sidecar-vol"]; !ok {
		t.Errorf("expected sidecar-vol volume, not found")
	} else {
		v := volumesMap["sidecar-vol"]
		if v.EmptyDir == nil {
			t.Errorf("expected sidecar-vol to be emptyDir")
		}
	}

	if _, ok := volumesMap["extra-vol"]; !ok {
		t.Errorf("expected extra-vol volume, not found")
	} else {
		v := volumesMap["extra-vol"]
		if v.EmptyDir == nil {
			t.Errorf("expected extra-vol to be emptyDir")
		}
	}
}

func TestBuildDeploymentGoogleChatAllowedUsersEmpty(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "my-agent",
			Namespace: "my-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Image: "gcr.io/my-proj/agent",
				},
			},
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				GoogleChat: &agentv1alpha1.GoogleChatSpec{
					Enabled:          ptr.To(true),
					ProjectID:        "my-gcp-project",
					SubscriptionName: "chat-sub",
					AllowedUsers:     []string{},
					HomeChannel:      "spaces/123",
				},
			},
		},
	}

	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012")
	container := dep.Spec.Template.Spec.Containers[0]
	envMap := make(map[string]corev1.EnvVar)
	for _, env := range container.Env {
		envMap[env.Name] = env
	}

	if envMap["GOOGLE_CHAT_ALLOWED_USERS"].Value != "" {
		t.Errorf("expected GOOGLE_CHAT_ALLOWED_USERS empty, got %s", envMap["GOOGLE_CHAT_ALLOWED_USERS"].Value)
	}
	if envMap["GOOGLE_CHAT_ALLOW_ALL_USERS"].Value != "true" {
		t.Errorf("expected GOOGLE_CHAT_ALLOW_ALL_USERS true, got %s", envMap["GOOGLE_CHAT_ALLOW_ALL_USERS"].Value)
	}
}

func TestBuildDeploymentSlackIntegration(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "my-agent",
			Namespace: "my-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				Slack: &agentv1alpha1.SlackSpec{
					Enabled: ptr.To(true),
					BotTokenSecretRef: &corev1.SecretKeySelector{
						LocalObjectReference: corev1.LocalObjectReference{Name: "custom-slack-secret"},
						Key:                  "bot-token-key",
					},
					AppTokenSecretRef: &corev1.SecretKeySelector{
						LocalObjectReference: corev1.LocalObjectReference{Name: "custom-slack-secret"},
						Key:                  "app-token-key",
					},
					AllowedUsers:    []string{"U123", "U456"},
					HomeChannel:     "C999",
					HomeChannelName: "general",
				},
			},
		},
	}

	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012")
	container := dep.Spec.Template.Spec.Containers[0]
	envMap := make(map[string]corev1.EnvVar)
	for _, env := range container.Env {
		envMap[env.Name] = env
	}

	if envMap["SLACK_BOT_TOKEN"].ValueFrom.SecretKeyRef.Name != "custom-slack-secret" || envMap["SLACK_BOT_TOKEN"].ValueFrom.SecretKeyRef.Key != "bot-token-key" {
		t.Errorf("expected SLACK_BOT_TOKEN custom-slack-secret/bot-token-key, got %v", envMap["SLACK_BOT_TOKEN"].ValueFrom)
	}
	if envMap["SLACK_APP_TOKEN"].ValueFrom.SecretKeyRef.Name != "custom-slack-secret" || envMap["SLACK_APP_TOKEN"].ValueFrom.SecretKeyRef.Key != "app-token-key" {
		t.Errorf("expected SLACK_APP_TOKEN custom-slack-secret/app-token-key, got %v", envMap["SLACK_APP_TOKEN"].ValueFrom)
	}
	if envMap["SLACK_ALLOWED_USERS"].Value != "U123,U456" {
		t.Errorf("expected SLACK_ALLOWED_USERS U123,U456, got %s", envMap["SLACK_ALLOWED_USERS"].Value)
	}
	if envMap["SLACK_HOME_CHANNEL"].Value != "C999" {
		t.Errorf("expected SLACK_HOME_CHANNEL C999, got %s", envMap["SLACK_HOME_CHANNEL"].Value)
	}
	if envMap["SLACK_HOME_CHANNEL_NAME"].Value != "general" {
		t.Errorf("expected SLACK_HOME_CHANNEL_NAME general, got %s", envMap["SLACK_HOME_CHANNEL_NAME"].Value)
	}
}

func TestBuildDeploymentSlackAllowAllUsers(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "my-agent",
			Namespace: "my-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				Slack: &agentv1alpha1.SlackSpec{
					Enabled:      ptr.To(true),
					AllowedUsers: []string{""},
				},
			},
		},
	}

	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012")
	container := dep.Spec.Template.Spec.Containers[0]
	envMap := make(map[string]corev1.EnvVar)
	for _, env := range container.Env {
		envMap[env.Name] = env
	}

	if _, ok := envMap["SLACK_ALLOWED_USERS"]; ok {
		t.Errorf("expected SLACK_ALLOWED_USERS not to be set when allowedUsers is empty")
	}
	if envMap["SLACK_ALLOW_ALL_USERS"].Value != "true" {
		t.Errorf("expected SLACK_ALLOW_ALL_USERS true, got %s", envMap["SLACK_ALLOW_ALL_USERS"].Value)
	}
}

func TestBuildConfigMapSlackEnabled(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				Slack: &agentv1alpha1.SlackSpec{
					Enabled: ptr.To(true),
				},
			},
		},
	}

	cm := buildConfigMap(agent)
	yamlContent := cm.Data["config.yaml"]
	if !strings.Contains(yamlContent, "slack:") || !strings.Contains(yamlContent, "enabled: true") {
		t.Errorf("expected config.yaml to enable slack platform, got:\n%s", yamlContent)
	}
}

func TestBuildFluentBitConfigMap(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}
	cm := buildFluentBitConfigMap(agent)
	if cm.Name != "test-agent-fluent-bit-config" {
		t.Errorf("expected configmap name test-agent-fluent-bit-config, got %s", cm.Name)
	}
	if cm.Namespace != "test-ns" {
		t.Errorf("expected configmap namespace test-ns, got %s", cm.Namespace)
	}
	fbConf, ok := cm.Data["fluent-bit.conf"]
	if !ok {
		t.Fatalf("expected fluent-bit.conf key, not found")
	}
	if !strings.Contains(fbConf, "Name              tail") {
		t.Errorf("expected fluent-bit.conf to contain Input Name tail")
	}
}

func TestBuildPlatformService(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-platform-agent",
			Namespace: "test-ns",
		},
	}

	svc := buildPlatformService(agent)
	if svc.Name != "test-platform-agent" {
		t.Errorf("expected Service name test-platform-agent, got %s", svc.Name)
	}
	if svc.Namespace != "test-ns" {
		t.Errorf("expected Service namespace test-ns, got %s", svc.Namespace)
	}

	if len(svc.Spec.Ports) != 2 {
		t.Errorf("expected 2 service ports, got %d", len(svc.Spec.Ports))
	}

	portsMap := make(map[string]int32)
	for _, port := range svc.Spec.Ports {
		portsMap[port.Name] = port.Port
	}

	if portsMap["api"] != 8642 {
		t.Errorf("expected api port 8642, got %d", portsMap["api"])
	}
	if portsMap["dashboard"] != 9119 {
		t.Errorf("expected dashboard port 9119, got %d", portsMap["dashboard"])
	}

	if svc.Spec.Selector["app"] != "test-platform-agent-gateway" {
		t.Errorf("expected selector app=test-platform-agent-gateway, got %s", svc.Spec.Selector["app"])
	}
}

func TestBuildSettingsConfigMap(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				IntegrationSpec: agentv1alpha1.IntegrationSpec{
					GitHub: &agentv1alpha1.GitHubSpec{
						GitRepo: "https://github.com/my-org/my-repo.git",
					},
				},
			},
		},
	}

	cm := buildSettingsConfigMap(agent)
	if cm.Name != "test-agent-settings" {
		t.Errorf("expected configmap name test-agent-settings, got %s", cm.Name)
	}
	if cm.Namespace != "test-ns" {
		t.Errorf("expected configmap namespace test-ns, got %s", cm.Namespace)
	}
	content, ok := cm.Data["SETTINGS.md"]
	if !ok {
		t.Fatalf("expected SETTINGS.md key, not found")
	}
	expectedContent := "# GKE Scope Configuration\n- **Git Repo:** https://github.com/my-org/my-repo.git\n"
	if content != expectedContent {
		t.Errorf("expected content:\n%q\ngot:\n%q", expectedContent, content)
	}
}

func TestBuildSettingsConfigMapEmptyGitRepo(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				IntegrationSpec: agentv1alpha1.IntegrationSpec{
					GitHub: &agentv1alpha1.GitHubSpec{
						GitRepo: "",
					},
				},
			},
		},
	}

	cm := buildSettingsConfigMap(agent)
	content, ok := cm.Data["SETTINGS.md"]
	if !ok {
		t.Fatalf("expected SETTINGS.md key, not found")
	}
	expectedContent := "# GKE Scope Configuration\n- **Git Repo:** None\n"
	if content != expectedContent {
		t.Errorf("expected content:\n%q\ngot:\n%q", expectedContent, content)
	}
}

func TestBuildSettingsConfigMapNilIntegration(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: nil,
		},
	}

	cm := buildSettingsConfigMap(agent)
	content, ok := cm.Data["SETTINGS.md"]
	if !ok {
		t.Fatalf("expected SETTINGS.md key, not found")
	}
	expectedContent := "# GKE Scope Configuration\n- **Git Repo:** None\n"
	if content != expectedContent {
		t.Errorf("expected content:\n%q\ngot:\n%q", expectedContent, content)
	}
}

func TestBuildSettingsConfigMapNilGitHub(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				IntegrationSpec: agentv1alpha1.IntegrationSpec{
					GitHub: nil,
				},
			},
		},
	}

	cm := buildSettingsConfigMap(agent)
	content, ok := cm.Data["SETTINGS.md"]
	if !ok {
		t.Fatalf("expected SETTINGS.md key, not found")
	}
	expectedContent := "# GKE Scope Configuration\n- **Git Repo:** None\n"
	if content != expectedContent {
		t.Errorf("expected content:\n%q\ngot:\n%q", expectedContent, content)
	}
}

func TestBuildPlatformExplorerRole(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	role := buildPlatformExplorerRole(agent)
	expectedName := "kubeagents:explorer:test-ns:test-agent"
	if role.Name != expectedName {
		t.Errorf("expected ClusterRole name %s, got %s", expectedName, role.Name)
	}

	if len(role.Rules) != 2 {
		t.Fatalf("expected 2 PolicyRules, got %d", len(role.Rules))
	}

	rule := role.Rules[0]
	if len(rule.APIGroups) != 1 || rule.APIGroups[0] != "" {
		t.Errorf("expected APIGroups [''], got %v", rule.APIGroups)
	}

	expectedResources := []string{"nodes", "pods", "namespaces"}
	if len(rule.Resources) != len(expectedResources) {
		t.Errorf("expected Resources %v, got %v", expectedResources, rule.Resources)
	}

	expectedVerbs := []string{"get", "list"}
	if len(rule.Verbs) != len(expectedVerbs) {
		t.Errorf("expected Verbs %v, got %v", expectedVerbs, rule.Verbs)
	}

	rule2 := role.Rules[1]
	if len(rule2.APIGroups) != 1 || rule2.APIGroups[0] != "apiextensions.k8s.io" {
		t.Errorf("expected APIGroups ['apiextensions.k8s.io'], got %v", rule2.APIGroups)
	}

	expectedResources2 := []string{"customresourcedefinitions"}
	if len(rule2.Resources) != len(expectedResources2) {
		t.Errorf("expected Resources %v, got %v", expectedResources2, rule2.Resources)
	}

	if len(rule2.Verbs) != len(expectedVerbs) {
		t.Errorf("expected Verbs %v, got %v", expectedVerbs, rule2.Verbs)
	}
}

func TestBuildClusterRoleBinding(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Security: &agentv1alpha1.SecuritySpec{
					ServiceAccountName: "custom-sa",
				},
			},
		},
	}

	crb := buildClusterRoleBinding(agent, "test-binding", "test-role")
	if crb.Name != "test-binding" {
		t.Errorf("expected ClusterRoleBinding name test-binding, got %s", crb.Name)
	}

	if crb.RoleRef.Name != "test-role" {
		t.Errorf("expected RoleRef name test-role, got %s", crb.RoleRef.Name)
	}
	if crb.RoleRef.Kind != "ClusterRole" {
		t.Errorf("expected RoleRef kind ClusterRole, got %s", crb.RoleRef.Kind)
	}

	if len(crb.Subjects) != 1 {
		t.Fatalf("expected 1 Subject, got %d", len(crb.Subjects))
	}

	subject := crb.Subjects[0]
	if subject.Kind != "ServiceAccount" {
		t.Errorf("expected Subject kind ServiceAccount, got %s", subject.Kind)
	}
	if subject.Name != "custom-sa" {
		t.Errorf("expected Subject name custom-sa, got %s", subject.Name)
	}
	if subject.Namespace != "test-ns" {
		t.Errorf("expected Subject namespace test-ns, got %s", subject.Namespace)
	}
}

func TestBuildClusterRoleBindingDefaultSA(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	crb := buildClusterRoleBinding(agent, "test-binding", "test-role")

	if len(crb.Subjects) != 1 {
		t.Fatalf("expected 1 Subject, got %d", len(crb.Subjects))
	}

	subject := crb.Subjects[0]
	if subject.Name != "test-agent" {
		t.Errorf("expected Subject name test-agent, got %s", subject.Name)
	}
}

func TestGetConfigMapHash(t *testing.T) {
	hashNil, err := getConfigMapHash(nil)
	if err != nil {
		t.Errorf("unexpected error for nil configmap: %v", err)
	}
	if hashNil != "" {
		t.Errorf("expected empty string for nil configmap, got %s", hashNil)
	}

	cm := &corev1.ConfigMap{
		Data: map[string]string{
			"key1": "value1",
		},
	}
	hash1, err := getConfigMapHash(cm)
	if err != nil {
		t.Errorf("unexpected error: %v", err)
	}

	// Add more data to change the hash
	cm.Data["key2"] = "value2"
	hash2, err := getConfigMapHash(cm)
	if err != nil {
		t.Errorf("unexpected error: %v", err)
	}

	if hash1 == hash2 {
		t.Errorf("expected different hashes for different configmap data")
	}
}
