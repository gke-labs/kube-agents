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
	"k8s.io/apimachinery/pkg/api/resource"
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
					Availability: &agentv1alpha1.AvailabilitySpec{
						RuntimeClassName: ptr.To("gvisor"),
					},
					Image:           "gcr.io/my-proj/agent",
					Tag:             ptr.To("v1.0.0"),
					ImagePullPolicy: ptr.To(corev1.PullAlways),
					BrowserArgs:     []string{"--no-sandbox", "--disable-gpu"},
					Env: []corev1.EnvVar{
						{
							Name:  "CUSTOM_VAR",
							Value: "custom-value",
						},
						{
							Name:  "CUSTOM_VAR", // Duplicate custom var, should override previous
							Value: "new-custom-value",
						},
						{
							Name:  "CREDENTIAL_PROXY_STATE_DIR",
							Value: "/var/agent/exposed-proxy-state",
						},
						{
							Name:  "BASH_ENV",
							Value: "/var/agent/untrusted-shell-profile",
						},
						{
							Name:  "KUBERNETES_SERVICE_HOST",
							Value: "attacker.example",
						},
						{
							Name:  "KUBERNETES_SERVICE_PORT",
							Value: "443",
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
				ProjectID:   "my-gcp-project",
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

	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012", "policy3456")

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

	if dep.Spec.Template.Spec.ShareProcessNamespace == nil || !*dep.Spec.Template.Spec.ShareProcessNamespace {
		t.Errorf("expected ShareProcessNamespace true, got %v", dep.Spec.Template.Spec.ShareProcessNamespace)
	}

	if dep.Spec.Template.Spec.RuntimeClassName == nil || *dep.Spec.Template.Spec.RuntimeClassName != "gvisor" {
		t.Errorf("expected RuntimeClassName gvisor, got %v", dep.Spec.Template.Spec.RuntimeClassName)
	}
	if dep.Spec.Template.Spec.ServiceAccountName != "custom-sa" {
		t.Errorf("expected shared pod service account custom-sa, got %s", dep.Spec.Template.Spec.ServiceAccountName)
	}
	if dep.Spec.Template.Spec.AutomountServiceAccountToken == nil || *dep.Spec.Template.Spec.AutomountServiceAccountToken {
		t.Errorf("expected sandbox service account token automount to be disabled")
	}

	if len(dep.Spec.Template.Spec.Containers) != 6 {
		t.Errorf("expected 6 containers, got %d", len(dep.Spec.Template.Spec.Containers))
	} else {
		dashboardC := dep.Spec.Template.Spec.Containers[1]
		if dashboardC.Name != "platform-agent-dashboard" {
			t.Errorf("expected container index 1 name platform-agent-dashboard, got %s", dashboardC.Name)
		}
		if len(dashboardC.Args) != 2 || dashboardC.Args[0] != "hermes" || dashboardC.Args[1] != "dashboard" {
			t.Errorf("expected args [hermes dashboard], got %v", dashboardC.Args)
		}
		if len(dashboardC.Ports) != 1 || dashboardC.Ports[0].Name != "dashboard" || dashboardC.Ports[0].ContainerPort != 9119 {
			t.Errorf("expected dashboard port 9119, got %v", dashboardC.Ports)
		}
		if dashboardC.Image != "gcr.io/my-proj/agent:v1.0.0" {
			t.Errorf("expected dashboard container image gcr.io/my-proj/agent:v1.0.0, got %s", dashboardC.Image)
		}
		if dashboardC.ImagePullPolicy != corev1.PullAlways {
			t.Errorf("expected dashboard container image pull policy Always, got %s", dashboardC.ImagePullPolicy)
		}
		if len(dashboardC.VolumeMounts) != 3 {
			t.Errorf("expected 3 volume mounts on dashboard container (2 base + 1 extra), got %d", len(dashboardC.VolumeMounts))
		}
		if dashboardC.SecurityContext == nil || dashboardC.SecurityContext.AllowPrivilegeEscalation == nil || *dashboardC.SecurityContext.AllowPrivilegeEscalation {
			t.Errorf("expected SecurityContext.AllowPrivilegeEscalation false on dashboard container")
		}
		if dashboardC.Resources.Requests.Cpu().String() != "256m" || dashboardC.Resources.Requests.Memory().String() != "512Mi" {
			t.Errorf("expected CPU 256m and Mem 512Mi requests on dashboard container, got %v", dashboardC.Resources.Requests)
		}
		if dashboardC.Resources.Limits.Cpu().String() != "1" || dashboardC.Resources.Limits.Memory().String() != "2Gi" {
			t.Errorf("expected CPU 1 and Mem 2Gi limits on dashboard container, got %v", dashboardC.Resources.Limits)
		}
		if len(dashboardC.Env) != 3 {
			t.Errorf("expected 3 env vars on dashboard container, got %d", len(dashboardC.Env))
		} else {
			dashboardEnvMap := make(map[string]corev1.EnvVar)
			for _, env := range dashboardC.Env {
				dashboardEnvMap[env.Name] = env
			}
			if dashboardEnvMap["PLATFORM_AGENT_HOME"].Value != "/var/agent" {
				t.Errorf("expected PLATFORM_AGENT_HOME /var/agent, got %s", dashboardEnvMap["PLATFORM_AGENT_HOME"].Value)
			}
			if dashboardEnvMap["HOME"].Value != "/var/agent/home" {
				t.Errorf("expected HOME /var/agent/home, got %s", dashboardEnvMap["HOME"].Value)
			}
			if dashboardEnvMap["SESSION_KV_DB_PATH"].Value != sessionKVDBPath {
				t.Errorf("expected SESSION_KV_DB_PATH %s, got %s", sessionKVDBPath, dashboardEnvMap["SESSION_KV_DB_PATH"].Value)
			}
		}

		watcherC := dep.Spec.Template.Spec.Containers[3]
		if watcherC.Name != "event-watcher" {
			t.Errorf("expected sidecar name event-watcher, got %s", watcherC.Name)
		}
		if watcherC.Image != "gcr.io/my-proj/agent:v1.0.0" {
			t.Errorf("expected watcher image gcr.io/my-proj/agent:v1.0.0, got %s", watcherC.Image)
		}
		if watcherC.Command[0] != "/usr/local/bin/k8s-event-watcher" {
			t.Errorf("expected watcher command /usr/local/bin/k8s-event-watcher, got %s", watcherC.Command[0])
		}
		watcherEnv := make(map[string]corev1.EnvVar)
		for _, env := range watcherC.Env {
			watcherEnv[env.Name] = env
		}
		if watcherEnv["API_SERVER_KEY"].Value != "cluster-internal-trusted" || watcherEnv["API_SERVER_KEY"].ValueFrom != nil {
			t.Errorf("expected watcher to receive the non-secret API sentinel, got %#v", watcherC.Env)
		}
		if len(watcherC.VolumeMounts) != 2 || watcherC.VolumeMounts[0].Name != "event-watcher-kubeconfig" || !watcherC.VolumeMounts[0].ReadOnly ||
			watcherC.VolumeMounts[1].Name != "event-watcher-ksa-token" || !watcherC.VolumeMounts[1].ReadOnly {
			t.Errorf("expected watcher to receive only its isolated kubeconfig and projected Kubernetes token, got %#v", watcherC.VolumeMounts)
		}

		if dep.Spec.Template.Spec.Containers[4].Name != "envoy-credential-proxy" {
			t.Errorf("expected managed Envoy sidecar, got %s", dep.Spec.Template.Spec.Containers[4].Name)
		}
		sidecarC := dep.Spec.Template.Spec.Containers[5]
		if sidecarC.Name != "my-sidecar" {
			t.Errorf("expected sidecar name my-sidecar, got %s", sidecarC.Name)
		}
		if sidecarC.Image != "sidecar-image:latest" {
			t.Errorf("expected sidecar image sidecar-image:latest, got %s", sidecarC.Image)
		}
	}

	if len(dep.Spec.Template.Spec.InitContainers) != 3 {
		t.Errorf("expected managed cleanup plus 2 configured init containers, got %d", len(dep.Spec.Template.Spec.InitContainers))
	} else {
		cleanup := dep.Spec.Template.Spec.InitContainers[0]
		if cleanup.Name != "sandbox-credential-cleanup" {
			t.Errorf("expected managed credential cleanup first, got %s", cleanup.Name)
		}
		if len(cleanup.VolumeMounts) != 1 || cleanup.VolumeMounts[0].Name != "platform-agent-data-vol" {
			t.Errorf("expected cleanup to mount the agent data PVC")
		}

		initC1 := dep.Spec.Template.Spec.InitContainers[1]
		if initC1.Name != "init-git" {
			t.Errorf("expected first init container name init-git, got %s", initC1.Name)
		}
		if initC1.Image != "git-image:latest" {
			t.Errorf("expected first init container image git-image:latest, got %s", initC1.Image)
		}

		initC2 := dep.Spec.Template.Spec.InitContainers[2]
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
		if env.ValueFrom != nil && env.ValueFrom.SecretKeyRef != nil {
			t.Errorf("sandbox must not receive Secret-backed environment variable %s", env.Name)
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
	if envMap["PLATFORM_AGENT_PLUGINS_DEBUG"].Value != "0" {
		t.Errorf("expected PLATFORM_AGENT_PLUGINS_DEBUG 0, got %s", envMap["PLATFORM_AGENT_PLUGINS_DEBUG"].Value)
	}
	if _, ok := envMap["CUSTOM_VAR"]; ok {
		t.Error("expected spec.deployment.env CUSTOM_VAR to be absent from sandbox")
	}
	if envMap["AGENT_BROWSER_ARGS"].Value != "--no-sandbox --disable-gpu" {
		t.Errorf("expected AGENT_BROWSER_ARGS --no-sandbox --disable-gpu, got %s", envMap["AGENT_BROWSER_ARGS"].Value)
	}
	if envMap["CREDENTIAL_PROXY_URL"].Value != "http://127.0.0.1:8765" {
		t.Errorf("expected localhost Envoy CREDENTIAL_PROXY_URL, got %s", envMap["CREDENTIAL_PROXY_URL"].Value)
	}
	proxyEnv := make(map[string]corev1.EnvVar)
	for _, env := range dep.Spec.Template.Spec.Containers[4].Env {
		proxyEnv[env.Name] = env
	}
	if proxyEnv["CUSTOM_VAR"].Value != "new-custom-value" {
		t.Errorf("expected spec.deployment.env only on credential sidecar, got %#v", proxyEnv)
	}
	if proxyEnv["CREDENTIAL_PROXY_STATE_DIR"].Value != "/var/lib/credential-proxy" {
		t.Errorf("reserved proxy state directory was overridden: %#v", proxyEnv["CREDENTIAL_PROXY_STATE_DIR"])
	}
	if _, found := proxyEnv["BASH_ENV"]; found {
		t.Errorf("expected unsafe shell environment override to be rejected")
	}
	for _, name := range []string{"KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT"} {
		if _, found := proxyEnv[name]; found {
			t.Errorf("expected reserved Kubernetes service environment %s to be rejected", name)
		}
	}
	apiKeyRef := proxyEnv["API_SERVER_EXTERNAL_KEY"].ValueFrom.SecretKeyRef
	if apiKeyRef.Name != "secrets" || apiKeyRef.Key != "api-key" {
		t.Errorf("expected external API key only in credential sidecar, got %#v", apiKeyRef)
	}
	for _, mount := range container.VolumeMounts {
		if mount.Name == "credential-proxy-ksa-token" || strings.Contains(mount.MountPath, "serviceaccount") {
			t.Errorf("sandbox must not mount a ServiceAccount token: %#v", mount)
		}
	}
	proxyHasTokenMount := false
	for _, mount := range dep.Spec.Template.Spec.Containers[4].VolumeMounts {
		if mount.Name == "credential-proxy-ksa-token" && mount.ReadOnly {
			proxyHasTokenMount = true
		}
	}
	if !proxyHasTokenMount {
		t.Error("expected projected KSA token to be mounted only by credential sidecar")
	}
	if !strings.HasPrefix(envMap["PATH"].Value, "/opt/credential-proxy/bin:") {
		t.Errorf("expected sandbox PATH to prefer credential proxy shims, got %s", envMap["PATH"].Value)
	}
	if envMap["GKE_CLUSTER_NAME"].Value != "gke-cluster" {
		t.Errorf("expected GKE_CLUSTER_NAME gke-cluster, got %s", envMap["GKE_CLUSTER_NAME"].Value)
	}
	if envMap["GKE_LOCATION"].Value != "us-east1" {
		t.Errorf("expected GKE_LOCATION us-east1, got %s", envMap["GKE_LOCATION"].Value)
	}
	if envMap["GCP_PROJECT_ID"].Value != "my-gcp-project" {
		t.Errorf("expected GCP_PROJECT_ID my-gcp-project, got %s", envMap["GCP_PROJECT_ID"].Value)
	}
	if envMap["API_SERVER_KEY"].Value != "cluster-internal-trusted" || envMap["API_SERVER_KEY"].ValueFrom != nil {
		t.Errorf("expected non-secret cluster trust sentinel, got %#v", envMap["API_SERVER_KEY"])
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
	if envMap["API_SERVER_HOST"].Value != "127.0.0.1" {
		t.Errorf("expected API_SERVER_HOST 127.0.0.1, got %s", envMap["API_SERVER_HOST"].Value)
	}
	if envMap["SESSION_KV_DB_PATH"].Value != "/var/lib/kube-agents/session/session_kv.db" {
		t.Errorf("expected SESSION_KV_DB_PATH /var/lib/kube-agents/session/session_kv.db, got %s", envMap["SESSION_KV_DB_PATH"].Value)
	}

	// Verify volume mounts
	mountsMap := make(map[string]corev1.VolumeMount)
	for _, m := range container.VolumeMounts {
		mountsMap[m.Name] = m
	}
	for _, volume := range dep.Spec.Template.Spec.Volumes {
		if _, mounted := mountsMap[volume.Name]; mounted && volume.Secret != nil {
			t.Errorf("sandbox must not mount Secret volume %s", volume.Name)
		}
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
	fbContainer := dep.Spec.Template.Spec.Containers[2]
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

func TestBuildDeployment_DashboardEnabled(t *testing.T) {
	testCases := []struct {
		name   string
		hermes *agentv1alpha1.HermesSpec
	}{
		{
			name:   "HermesSpec is nil",
			hermes: nil,
		},
		{
			name: "DashboardEnabled is nil",
			hermes: &agentv1alpha1.HermesSpec{
				DashboardEnabled: nil,
			},
		},
		{
			name: "DashboardEnabled is true",
			hermes: &agentv1alpha1.HermesSpec{
				DashboardEnabled: ptr.To(true),
			},
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			agent := &agentv1alpha1.PlatformAgent{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "my-agent",
					Namespace: "my-ns",
				},
				Spec: agentv1alpha1.PlatformAgentSpec{
					Harness: &agentv1alpha1.HarnessSpec{
						Hermes: tc.hermes,
					},
				},
			}

			if !isDashboardEnabled(agent) {
				t.Errorf("expected isDashboardEnabled to be true")
			}

			dep := buildDeployment(agent, "hash1", "hash2", "hash3", "hash4")
			if dep.Spec.Template.Spec.ShareProcessNamespace == nil || !*dep.Spec.Template.Spec.ShareProcessNamespace {
				t.Errorf("expected ShareProcessNamespace to be true, got %v", dep.Spec.Template.Spec.ShareProcessNamespace)
			}
			if len(dep.Spec.Template.Spec.Containers) != 5 {
				t.Fatalf("expected dashboard deployment plus credential sidecar to have 5 containers, got %d", len(dep.Spec.Template.Spec.Containers))
			}
			if dep.Spec.Template.Spec.Containers[0].Name != "platform-agent" {
				t.Errorf("expected container 0 to be platform-agent, got %s", dep.Spec.Template.Spec.Containers[0].Name)
			}
			if dep.Spec.Template.Spec.Containers[1].Name != "platform-agent-dashboard" {
				t.Errorf("expected container 1 to be platform-agent-dashboard, got %s", dep.Spec.Template.Spec.Containers[1].Name)
			}
			if dep.Spec.Template.Spec.Containers[2].Name != "fluent-bit" {
				t.Errorf("expected container 2 to be fluent-bit, got %s", dep.Spec.Template.Spec.Containers[2].Name)
			}
			if dep.Spec.Template.Spec.Containers[3].Name != "event-watcher" {
				t.Errorf("expected container 3 to be event-watcher, got %s", dep.Spec.Template.Spec.Containers[3].Name)
			}
			if dep.Spec.Template.Spec.Containers[4].Name != "envoy-credential-proxy" {
				t.Errorf("expected container 4 to be envoy-credential-proxy, got %s", dep.Spec.Template.Spec.Containers[4].Name)
			}

			svc := buildPlatformService(agent)
			hasDashboardPort := false
			for _, port := range svc.Spec.Ports {
				if port.Name == "dashboard" && port.Port == 9119 {
					hasDashboardPort = true
					break
				}
			}
			if !hasDashboardPort {
				t.Errorf("expected service port 9119 (dashboard) to be present")
			}
		})
	}
}

func TestBuildDeployment_DashboardDisabled(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "my-agent",
			Namespace: "my-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				Hermes: &agentv1alpha1.HermesSpec{
					DashboardEnabled: ptr.To(false),
				},
			},
		},
	}

	if isDashboardEnabled(agent) {
		t.Errorf("expected isDashboardEnabled to be false")
	}

	dep := buildDeployment(agent, "hash1", "hash2", "hash3", "hash4")
	if dep.Spec.Template.Spec.ShareProcessNamespace != nil {
		t.Errorf("expected ShareProcessNamespace to be nil, got %v", *dep.Spec.Template.Spec.ShareProcessNamespace)
	}
	if len(dep.Spec.Template.Spec.Containers) != 4 {
		t.Fatalf("expected dashboard-disabled deployment plus credential sidecar to have 4 containers, got %d", len(dep.Spec.Template.Spec.Containers))
	}
	if dep.Spec.Template.Spec.Containers[0].Name != "platform-agent" {
		t.Errorf("expected container 0 to be platform-agent, got %s", dep.Spec.Template.Spec.Containers[0].Name)
	}
	if dep.Spec.Template.Spec.Containers[1].Name != "fluent-bit" {
		t.Errorf("expected container 1 to be fluent-bit, got %s", dep.Spec.Template.Spec.Containers[1].Name)
	}
	if dep.Spec.Template.Spec.Containers[2].Name != "event-watcher" {
		t.Errorf("expected container 2 to be event-watcher, got %s", dep.Spec.Template.Spec.Containers[2].Name)
	}
	if dep.Spec.Template.Spec.Containers[3].Name != "envoy-credential-proxy" {
		t.Errorf("expected container 3 to be envoy-credential-proxy, got %s", dep.Spec.Template.Spec.Containers[3].Name)
	}

	svc := buildPlatformService(agent)
	for _, port := range svc.Spec.Ports {
		if port.Name == "dashboard" || port.Port == 9119 {
			t.Errorf("expected dashboard port 9119 to be omitted when dashboard disabled")
		}
	}
}

func TestSafeSandboxEnvOverridesRejectsValueFrom(t *testing.T) {
	custom := []corev1.EnvVar{
		{Name: "OTEL_SERVICE_NAME", Value: "platform-agent"},
		{
			Name: "OTEL_EXPORTER_OTLP_ENDPOINT",
			ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: "telemetry-secret"},
				Key:                  "endpoint",
			}},
		},
		{
			Name: "OTEL_RESOURCE_ATTRIBUTES",
			ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{
				FieldPath: "metadata.annotations['telemetry']",
			}},
		},
	}

	got := safeSandboxEnvOverrides(custom)
	if len(got) != 1 || got[0].Name != "OTEL_SERVICE_NAME" || got[0].Value != "platform-agent" {
		t.Fatalf("expected only literal allowlisted telemetry env, got %#v", got)
	}
}

func TestBuildCredentialProxySidecar(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   "example-project",
				ClusterName: "example-cluster",
				Location:    "us-central1",
			},
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{Image: "example/platform-agent", Tag: ptr.To("v1")},
				Security:   &agentv1alpha1.SecuritySpec{ServiceAccountName: "credential-sa"},
			},
		},
	}

	policy := buildCredentialProxyPolicyConfigMap(agent)
	if policy.Name != "test-agent-credential-proxy-policy" || !strings.Contains(policy.Data["policy.json"], "github.token-disclosure") {
		t.Fatalf("unexpected credential proxy policy: %#v", policy)
	}

	container := buildCredentialProxySidecar(agent, "/opt/hermes")
	if container.Name != "envoy-credential-proxy" || container.Image != "example/credential-proxy:v1" {
		t.Errorf("unexpected proxy container: %#v", container)
	}
	if len(container.Command) != 1 || container.Command[0] != "/usr/local/bin/envoy-credential-sidecar" {
		t.Errorf("unexpected proxy command: %v", container.Command)
	}
	env := make(map[string]corev1.EnvVar)
	for _, item := range container.Env {
		env[item.Name] = item
	}
	if env["CREDENTIAL_PROXY_STATE_DIR"].Value != "/var/lib/credential-proxy" {
		t.Errorf("expected private proxy state directory, got %#v", env["CREDENTIAL_PROXY_STATE_DIR"])
	}
	if env["KUBE_CONTEXT_NAME"].Value != "gke_example-project_us-central1_example-cluster" {
		t.Errorf("expected proxy Kubernetes context, got %#v", env["KUBE_CONTEXT_NAME"])
	}
	if env["KUBE_DEFAULT_NAMESPACE"].Value != "test-ns" {
		t.Errorf("expected proxy default namespace, got %#v", env["KUBE_DEFAULT_NAMESPACE"])
	}
	bootstrap := env["CREDENTIAL_PROXY_BOOTSTRAP_COMMAND"].Value
	for _, expected := range []string{"gcloud config set project", "gcloud container clusters get-credentials", "kubectl config use-context", "kubectl config set-context"} {
		if !strings.Contains(bootstrap, expected) {
			t.Errorf("expected generic shell bootstrap to contain %q, got %q", expected, bootstrap)
		}
	}
	stateMounted := false
	for _, mount := range container.VolumeMounts {
		if mount.Name == "credential-proxy-state" && mount.MountPath == "/var/lib/credential-proxy" {
			stateMounted = true
		}
	}
	if !stateMounted {
		t.Errorf("expected private proxy state volume mount, got %#v", container.VolumeMounts)
	}
}

func TestResolveCredentialProxyImagePreservesTag(t *testing.T) {
	if got := resolveCredentialProxyImage(nil); got != "ghcr.io/gke-labs/kube-agents/credential-proxy:latest" {
		t.Fatalf("unexpected default credential sidecar image: %s", got)
	}
	if got := resolveCredentialProxyImage(&agentv1alpha1.DeploymentSpec{Image: "example/platform-agent"}); got != "example/credential-proxy:latest" {
		t.Fatalf("expected explicit latest tag for untagged sidecar image: %s", got)
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

	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012", "policy3456")
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

	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012", "policy3456")
	container := dep.Spec.Template.Spec.Containers[0]
	envMap := make(map[string]corev1.EnvVar)
	for _, env := range container.Env {
		envMap[env.Name] = env
	}

	if _, ok := envMap["SLACK_BOT_TOKEN"]; ok {
		t.Error("expected SLACK_BOT_TOKEN to be absent from sandbox")
	}
	if _, ok := envMap["SLACK_APP_TOKEN"]; ok {
		t.Error("expected SLACK_APP_TOKEN to be absent from sandbox")
	}
	if envMap["SLACK_RELAY_URL"].Value != "http://127.0.0.1:8765" {
		t.Errorf("expected credential-free Slack relay URL, got %v", envMap["SLACK_RELAY_URL"])
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

	proxyEnv := make(map[string]corev1.EnvVar)
	for _, env := range buildCredentialProxySidecar(agent, "/opt/hermes").Env {
		proxyEnv[env.Name] = env
	}
	if proxyEnv["SLACK_BOT_TOKEN"].ValueFrom.SecretKeyRef.Name != "custom-slack-secret" || proxyEnv["SLACK_BOT_TOKEN"].ValueFrom.SecretKeyRef.Key != "bot-token-key" {
		t.Errorf("expected proxy SLACK_BOT_TOKEN custom-slack-secret/bot-token-key, got %v", proxyEnv["SLACK_BOT_TOKEN"].ValueFrom)
	}
	if proxyEnv["SLACK_APP_TOKEN"].ValueFrom.SecretKeyRef.Name != "custom-slack-secret" || proxyEnv["SLACK_APP_TOKEN"].ValueFrom.SecretKeyRef.Key != "app-token-key" {
		t.Errorf("expected proxy SLACK_APP_TOKEN custom-slack-secret/app-token-key, got %v", proxyEnv["SLACK_APP_TOKEN"].ValueFrom)
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

	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012", "policy3456")
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
	t.Run("DashboardEnabled_Default", func(t *testing.T) {
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
			t.Errorf("expected 2 service ports when dashboard enabled, got %d", len(svc.Spec.Ports))
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
		if svc.Spec.Ports[0].TargetPort.IntVal != 8643 {
			t.Errorf("expected api service to terminate at credential proxy port 8643, got %s", svc.Spec.Ports[0].TargetPort.String())
		}

		if svc.Spec.Selector["app"] != "test-platform-agent-gateway" {
			t.Errorf("expected selector app=test-platform-agent-gateway, got %s", svc.Spec.Selector["app"])
		}
	})

	t.Run("DashboardDisabled_Explicit", func(t *testing.T) {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-platform-agent",
				Namespace: "test-ns",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Harness: &agentv1alpha1.HarnessSpec{
					Hermes: &agentv1alpha1.HermesSpec{
						DashboardEnabled: ptr.To(false),
					},
				},
			},
		}

		svc := buildPlatformService(agent)
		if len(svc.Spec.Ports) != 1 {
			t.Errorf("expected 1 service port when dashboard disabled, got %d", len(svc.Spec.Ports))
		}
	})

	t.Run("DashboardEnabled", func(t *testing.T) {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-platform-agent",
				Namespace: "test-ns",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Harness: &agentv1alpha1.HarnessSpec{
					Hermes: &agentv1alpha1.HermesSpec{
						DashboardEnabled: ptr.To(true),
					},
				},
			},
		}

		svc := buildPlatformService(agent)
		if len(svc.Spec.Ports) != 2 {
			t.Errorf("expected 2 service ports when dashboard enabled, got %d", len(svc.Spec.Ports))
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
	})
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

func TestBuildDeploymentHA(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "ha-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Availability: &agentv1alpha1.AvailabilitySpec{
						Replicas: ptr.To(int32(2)),
					},
				},
			},
		},
	}

	dep := buildDeployment(agent, "h1", "h2", "h3", "h4")
	if *dep.Spec.Replicas != 2 {
		t.Errorf("expected 2 replicas for HA deployment, got %d", *dep.Spec.Replicas)
	}

	if dep.Spec.Template.Spec.Affinity != nil {
		t.Fatalf("expected nil pod affinity when not explicitly specified in CR, got %v", dep.Spec.Template.Spec.Affinity)
	}
}

func TestBuildPVCStorageClass(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "sc-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Storages: []agentv1alpha1.StorageSpec{
						{
							Name:             "custom-storage",
							StorageClassName: ptr.To("standard-rwd"),
						},
					},
				},
			},
		},
	}

	pvc := buildPVC(agent)
	if pvc.Spec.StorageClassName != nil {
		t.Errorf("expected StorageClassName nil on default data PVC, got %v", *pvc.Spec.StorageClassName)
	}

	sysPvc := buildSystemPVC(agent)
	if sysPvc.Spec.StorageClassName != nil {
		t.Errorf("expected StorageClassName nil on system metadata PVC, got %v", *sysPvc.Spec.StorageClassName)
	}

	customPvcs, err := buildCustomPVCs(agent)
	if err != nil {
		t.Fatalf("unexpected error from buildCustomPVCs: %v", err)
	}
	if len(customPvcs) != 1 || customPvcs[0].Spec.StorageClassName == nil || *customPvcs[0].Spec.StorageClassName != "standard-rwd" {
		t.Errorf("expected StorageClassName standard-rwd on custom PVC, got %v", *customPvcs[0].Spec.StorageClassName)
	}
}

func TestBuildCustomPVCsInvalidSize(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "invalid-size-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Storages: []agentv1alpha1.StorageSpec{
						{
							Name:        "bad-storage",
							StorageSize: "invalid-size-string",
						},
					},
				},
			},
		},
	}

	pvcs, err := buildCustomPVCs(agent)
	if err != nil {
		t.Fatalf("unexpected error when parsing invalid storage size: %v", err)
	}
	if len(pvcs) != 1 {
		t.Fatalf("expected 1 PVC, got %d", len(pvcs))
	}
	expectedSize := resource.MustParse("5Gi")
	actualSize := pvcs[0].Spec.Resources.Requests[corev1.ResourceStorage]
	if actualSize.Cmp(expectedSize) != 0 {
		t.Errorf("expected size %v, got %v", expectedSize, actualSize)
	}
}

func TestBuildDeploymentReplicasConfig(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "custom-replicas-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Availability: &agentv1alpha1.AvailabilitySpec{
						Replicas: ptr.To(int32(3)),
					},
				},
			},
		},
	}

	dep := buildDeployment(agent, "h1", "h2", "h3", "h4")
	if *dep.Spec.Replicas != 3 {
		t.Errorf("expected 3 replicas when explicitly set, got %d", *dep.Spec.Replicas)
	}

	cm := buildConfigMap(agent)
	yamlContent := cm.Data["config.yaml"]
	if !strings.Contains(yamlContent, "leader_election:") || !strings.Contains(yamlContent, "enabled: true") {
		t.Errorf("expected leader_election enabled in config.yaml for replicas > 1, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "lease_name: custom-replicas-agent-leader") {
		t.Errorf("expected lease_name custom-replicas-agent-leader, got:\n%s", yamlContent)
	}

	container := dep.Spec.Template.Spec.Containers[0]
	envMap := make(map[string]corev1.EnvVar)
	for _, env := range container.Env {
		envMap[env.Name] = env
	}
	if envMap["ENABLE_LEADER_ELECTION"].Value != "true" {
		t.Errorf("expected ENABLE_LEADER_ELECTION true, got %s", envMap["ENABLE_LEADER_ELECTION"].Value)
	}
	if envMap["LEADER_ELECTION_LEASE_NAME"].Value != "custom-replicas-agent-leader" {
		t.Errorf("expected LEADER_ELECTION_LEASE_NAME custom-replicas-agent-leader, got %s", envMap["LEADER_ELECTION_LEASE_NAME"].Value)
	}
}

func TestRWOStoragePerReplica(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "rwo-ha-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Availability: &agentv1alpha1.AvailabilitySpec{
						Replicas: ptr.To(int32(2)),
					},
					Storages: []agentv1alpha1.StorageSpec{
						{
							Name:             "my-rwo-data",
							StorageClassName: ptr.To("standard-rwo"),
						},
					},
				},
			},
		},
	}

	if !useStatefulSet(agent) {
		t.Fatalf("expected useStatefulSet to be true for multi-replica agent with RWO storage")
	}

	pvcs, err := buildCustomPVCs(agent)
	if err != nil {
		t.Fatalf("unexpected error from buildCustomPVCs: %v", err)
	}
	if len(pvcs) != 0 {
		t.Errorf("expected 0 standalone PVCs when using StatefulSet, got %d", len(pvcs))
	}

	vols := buildCustomStorageVolumes(agent)
	if len(vols) != 0 {
		t.Errorf("expected 0 custom storage volumes in pod spec when using StatefulSet RWO, got %d", len(vols))
	}

	sts := buildStatefulSet(agent, "h1", "h2", "h3", "h4")
	if *sts.Spec.Replicas != 2 {
		t.Errorf("expected 2 replicas in StatefulSet, got %d", *sts.Spec.Replicas)
	}
	if len(sts.Spec.VolumeClaimTemplates) != 1 {
		t.Fatalf("expected 1 VolumeClaimTemplate in StatefulSet, got %d", len(sts.Spec.VolumeClaimTemplates))
	}
	if sts.Spec.VolumeClaimTemplates[0].Name != "my-rwo-data-vol" {
		t.Errorf("expected VolumeClaimTemplate name my-rwo-data-vol, got %s", sts.Spec.VolumeClaimTemplates[0].Name)
	}
}

func TestBuildPlatformLeaderRole(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	role := buildPlatformLeaderRole(agent)
	if role.Name != "kubeagents:leader:test-ns:test-agent" || role.Namespace != "test-ns" {
		t.Errorf("expected role name kubeagents:leader:test-ns:test-agent and namespace test-ns, got name %s ns %s", role.Name, role.Namespace)
	}
	if len(role.Rules) != 2 || role.Rules[0].Resources[0] != "leases" || role.Rules[1].Resources[0] != "pods" {
		t.Errorf("expected rules for leases and pods, got %v", role.Rules)
	}

	rb := buildLeaderRoleBinding(agent, role.Name, role.Name)
	if rb.Name != role.Name || rb.Namespace != "test-ns" {
		t.Errorf("expected rolebinding name %s, got %s", role.Name, rb.Name)
	}
	if rb.RoleRef.Name != role.Name {
		t.Errorf("expected roleRef name %s, got %s", role.Name, rb.RoleRef.Name)
	}
}
