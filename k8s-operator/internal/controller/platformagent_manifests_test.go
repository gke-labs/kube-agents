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
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"slices"
	"sort"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"gopkg.in/yaml.v3"
	k8syaml "sigs.k8s.io/yaml"
)

// The default profile's rendered config used to be a `config.yaml` key mounted
// straight over the agent's own config.yaml. That made the file read-only, so every
// runtime write to it failed — `/sethome` with an EACCES, `monitoring.install_id`
// silently. It now ships as the managed scope, mounted read-only at /etc/hermes and
// overlaid by Hermes per leaf key, so the assertions below read the same rendered YAML
// under a different key.
func defaultProfileYAML(t *testing.T, cm *corev1.ConfigMap) string {
	t.Helper()
	key := managedConfigKey
	content, ok := cm.Data[key]
	if !ok {
		t.Fatalf("%s missing from ConfigMap data, got keys %v", key, mapKeys(cm.Data))
	}
	return content
}

// containerByName finds a container by name instead of by position. Positional
// indices have now broken twice on a container being added or removed, and the
// failure mode is bad: the assertion silently reads a different container and
// fails somewhere unrelated, or nil-derefs on a field that container never had.
func containerByName(t *testing.T, containers []corev1.Container, name string) corev1.Container {
	t.Helper()
	for _, c := range containers {
		if c.Name == name {
			return c
		}
	}
	got := make([]string, 0, len(containers))
	for _, c := range containers {
		got = append(got, c.Name)
	}
	t.Fatalf("no container named %q; got %v", name, got)
	return corev1.Container{}
}

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

	cm := buildConfigMap(agent, nil)
	if cm.Name != "test-agent-config" {
		t.Errorf("expected configmap name test-agent-config, got %s", cm.Name)
	}

	yamlContent := defaultProfileYAML(t, cm)
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
	if !strings.Contains(yamlContent, "api_mode: chat_completions") {
		t.Errorf("expected config to pin the wire protocol, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "enabled: true") {
		t.Errorf("expected config to enable google_chat platform, got:\n%s", yamlContent)
	}
	if !strings.Contains(yamlContent, "cron_mode: approve") {
		t.Errorf("expected config to contain cron_mode: approve, got:\n%s", yamlContent)
	}
	// The managed scope is machine-global (see renderConfigYAML), so a key that
	// describes ONE profile's tool surface must not appear in it: this same file is
	// overlaid on the privileged platform profile and on every cluster agent, and a
	// leaf replaces rather than merges. This is the regression guard for #658's
	// follow-up — the front door's delegation surface belongs in the image's
	// agents/chat/config.yaml, which only the default profile reads.
	for _, forbidden := range []string{
		"mcp_servers:", "platform_toolsets:", "toolsets:", "disabled_toolsets:",
		"environment_probe:", "kanban:", "terminal:", "memory:", "plugins:",
		"leader_election:", "web:",
	} {
		if strings.Contains(yamlContent, forbidden) {
			t.Errorf("managed config must not carry profile-shaped key %q — it is overlaid "+
				"on the platform and cluster profiles too, got:\n%s", forbidden, yamlContent)
		}
	}
	// The front door must NOT hold privileged/runtime tools — those live in the
	// separate platform/cluster profiles, not the default (chat) profile.
	for _, forbidden := range []string{"platform_control", "agent_common", "hermes-api-server", "hermes-cli"} {
		if strings.Contains(yamlContent, forbidden) {
			t.Errorf("default (chat) profile must not contain %q, got:\n%s", forbidden, yamlContent)
		}
	}
}

// The memory subtree is no longer rendered into the managed scope: it is
// profile-shaped (the front door's per-user provider is exactly what a
// kanban-spawned specialist must not get), and the scope is machine-global. The
// front door takes it from agents/chat/config.yaml; the specialists take theirs
// from the profile overlays, which TestBuildConfigMapDataPlatformOverlayFollowsProvider
// covers. spec.harness.memory therefore no longer reaches the default profile —
// tracked as follow-up work, together with the rest of the CR surface that used to
// ride on this render.
func TestManagedConfigCarriesNoMemorySubtree(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "memory-agent", Namespace: "test-ns"},
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

	yamlContent := defaultProfileYAML(t, buildConfigMap(agent, nil))
	for _, forbidden := range []string{"memory:", "memory_enabled:", "user_profile_enabled:", "custom_memory"} {
		if strings.Contains(yamlContent, forbidden) {
			t.Errorf("managed config must not carry %q — it would overwrite every "+
				"specialist profile's memory settings, got:\n%s", forbidden, yamlContent)
		}
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
	defaultConfig := defaultProfileYAML(t, buildConfigMap(defaultAgent, nil))
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
	debugConfig := defaultProfileYAML(t, buildConfigMap(debugAgent, nil))
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
						{
							Name:  "API_SERVER_KEY",
							Value: "malicious-api-key",
						},
						{
							Name:  "HERMES_HOME",
							Value: "/tmp/malicious-hermes",
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

	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012", "policy3456", nil, renderOptions{imageVolumeSupported: true})

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

	if len(dep.Spec.Template.Spec.Containers) != 5 {
		t.Errorf("expected 5 containers, got %d", len(dep.Spec.Template.Spec.Containers))
	} else {
		dashboardC := containerByName(t, dep.Spec.Template.Spec.Containers, "platform-agent-dashboard")
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
		if len(dashboardC.VolumeMounts) != 4 {
			t.Errorf("expected 4 volume mounts on dashboard container (3 base + 1 extra), got %d", len(dashboardC.VolumeMounts))
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
		if len(dashboardC.Env) != 6 {
			t.Errorf("expected 6 env vars on dashboard container, got %d", len(dashboardC.Env))
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
			// The dashboard has no `command:`, so it re-runs the image ENTRYPOINT
			// against the same volume as the agent container. Without this the
			// entrypoint treats it as a second primary: two session KV servers
			// racing for :8699, and an OTel service-name stamp blanked by the
			// container that has no OTEL_SERVICE_NAME.
			if dashboardEnvMap["PLATFORM_AGENT_ROLE"].Value != "sidecar" {
				t.Errorf("expected PLATFORM_AGENT_ROLE sidecar, got %q", dashboardEnvMap["PLATFORM_AGENT_ROLE"].Value)
			}
			// And the shared-tree gate itself: the dashboard must never run the
			// setup pass. TestSharedStateOwnershipIsDeclaredNotInferred pins the
			// full contract for both containers.
			if dashboardEnvMap[sharedStateSetupEnvVar].Value != sharedStateSetupSkip {
				t.Errorf("expected %s=%s, got %q", sharedStateSetupEnvVar, sharedStateSetupSkip, dashboardEnvMap[sharedStateSetupEnvVar].Value)
			}
		}

		// The watcher is not a container of its own: it runs inside the credential
		// proxy, which carries its arguments and its API server credentials.
		for _, c := range dep.Spec.Template.Spec.Containers {
			if c.Name == "event-watcher" {
				t.Errorf("event-watcher should no longer be a standalone container")
			}
		}
		proxyC := containerByName(t, dep.Spec.Template.Spec.Containers, "envoy-credential-proxy")
		if proxyC.Name != "envoy-credential-proxy" {
			t.Errorf("expected managed Envoy sidecar, got %s", proxyC.Name)
		}
		// The watcher's loopback flags live in the entrypoint, not here — the
		// container passes no arguments at all. Only the per-install cluster
		// name is plumbed through, as an explicit env var.
		if len(proxyC.Args) != 0 {
			t.Errorf("credential proxy should take no arguments; the entrypoint owns the watcher's flags, got %v", proxyC.Args)
		}
		proxyEnv := make(map[string]corev1.EnvVar)
		for _, env := range proxyC.Env {
			proxyEnv[env.Name] = env
		}
		// Sourced from resolveHarnessClusterName, not GKE_CLUSTER_NAME: the
		// latter is only set when projectID and location are also present, so a
		// CR naming its cluster without them would be mislabelled.
		if proxyEnv["EVENT_WATCHER_CLUSTER_NAME"].Value != "gke-cluster" {
			t.Errorf("expected the watcher to be told its cluster name, got %#v", proxyEnv["EVENT_WATCHER_CLUSTER_NAME"])
		}
		if proxyEnv["API_SERVER_KEY"].Value != "cluster-internal-trusted" || proxyEnv["API_SERVER_KEY"].ValueFrom != nil {
			t.Errorf("expected the watcher's non-secret API sentinel, got %#v", proxyC.Env)
		}
		var watcherToken bool
		for _, m := range proxyC.VolumeMounts {
			if m.Name == "event-watcher-ksa-token" && m.MountPath == "/var/run/secrets/kubernetes.io/serviceaccount" && m.ReadOnly {
				watcherToken = true
			}
		}
		if !watcherToken {
			t.Errorf("expected the default-audience token mounted where InClusterConfig reads it, got %#v", proxyC.VolumeMounts)
		}

		sidecarC := containerByName(t, dep.Spec.Template.Spec.Containers, "my-sidecar")
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
		// The allowlist is two entries long and stays that way unless someone
		// argues the same case again. Both are pod-scoped: SESSION_KV_API_KEY
		// authenticates callers of the Session KV server on this pod's
		// loopback, and SESSION_KV_SALT is the HMAC salt for pseudonymising
		// chat identities, which has to be here because the hashing is here.
		// Neither reaches a cloud API, a repository, or anything off the pod —
		// which is what the isolation boundary is for. A Secret-backed variable
		// that does not meet that bar belongs in the credential-proxy container.
		if env.ValueFrom != nil && env.ValueFrom.SecretKeyRef != nil &&
			env.Name != "SESSION_KV_API_KEY" && env.Name != "SESSION_KV_SALT" {
			t.Errorf("sandbox must not receive Secret-backed environment variable %s", env.Name)
		}
		seen[env.Name] = true
		envMap[env.Name] = env
	}

	for _, name := range []string{"SESSION_KV_API_KEY", "SESSION_KV_SALT"} {
		ref := envMap[name].ValueFrom
		if ref == nil || ref.SecretKeyRef == nil {
			t.Fatalf("expected sandbox %s to come from a Secret, got %v", name, envMap[name])
		}
		if ref.SecretKeyRef.Name != "platform-agent-secrets" || ref.SecretKeyRef.Key != name {
			t.Errorf("expected sandbox %s from platform-agent-secrets/%s, got %v", name, name, ref.SecretKeyRef)
		}
		if ref.SecretKeyRef.Optional == nil || !*ref.SecretKeyRef.Optional {
			t.Errorf("expected sandbox %s to be optional so a missing key degrades rather than blocks startup", name)
		}
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
	proxyC := containerByName(t, dep.Spec.Template.Spec.Containers, "envoy-credential-proxy")
	proxyEnv := make(map[string]corev1.EnvVar)
	for _, env := range proxyC.Env {
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
	for _, name := range []string{"KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT", "HERMES_HOME"} {
		if _, found := proxyEnv[name]; found {
			t.Errorf("expected reserved environment %s to be rejected from credential proxy", name)
		}
	}
	// API_SERVER_KEY used to be asserted absent, but the proxy now sets it
	// deliberately: the event watcher it hosts reads it via --token-env. It is a
	// non-secret loopback sentinel, not a credential — the real secret is
	// API_SERVER_EXTERNAL_KEY below. The guard that mattered was "a user cannot
	// supply this through spec.deployment.env", so assert the value rather than
	// its absence; a user-supplied override still fails here.
	if proxyEnv["API_SERVER_KEY"].Value != "cluster-internal-trusted" || proxyEnv["API_SERVER_KEY"].ValueFrom != nil {
		t.Errorf("credential proxy must carry the non-secret sentinel, not a user-supplied value: %#v", proxyEnv["API_SERVER_KEY"])
	}
	apiKeyRef := proxyEnv["API_SERVER_EXTERNAL_KEY"].ValueFrom.SecretKeyRef
	if apiKeyRef.Name != "secrets" || apiKeyRef.Key != "api-key" {
		t.Errorf("expected external API key only in credential sidecar, got %#v", apiKeyRef)
	}
	// The watcher hosted here posts to the Session KV server in the sandbox
	// container, and that server authenticates now. Both containers must resolve
	// the same Secret key, or the watcher's every POST is a 401 and no incident
	// is ever triaged — a failure that is silent from the outside.
	proxySessionKV := proxyEnv["SESSION_KV_API_KEY"].ValueFrom
	if proxySessionKV == nil || proxySessionKV.SecretKeyRef == nil {
		t.Fatalf("expected credential proxy SESSION_KV_API_KEY from a Secret, got %#v", proxyEnv["SESSION_KV_API_KEY"])
	}
	sandboxSessionKV := envMap["SESSION_KV_API_KEY"].ValueFrom.SecretKeyRef
	// DeepEqual rather than `*a != *b`: SecretKeySelector carries Optional as a
	// *bool, so struct equality compares two separately allocated pointers by
	// address and never matches, however identical the keys are.
	if !reflect.DeepEqual(proxySessionKV.SecretKeyRef, sandboxSessionKV) {
		t.Errorf("sandbox and credential proxy disagree on the Session KV key: %#v vs %#v",
			sandboxSessionKV, proxySessionKV.SecretKeyRef)
	}
	for _, mount := range container.VolumeMounts {
		if mount.Name == "credential-proxy-ksa-token" || strings.Contains(mount.MountPath, "serviceaccount") {
			t.Errorf("sandbox must not mount a ServiceAccount token: %#v", mount)
		}
	}
	proxyHasTokenMount := false
	for _, mount := range proxyC.VolumeMounts {
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
	// Emitted with the real answer rather than omitted. Omitting it is what let a
	// leftover GOOGLE_CHAT_ALLOW_ALL_USERS=true from an earlier, allowlist-free spec
	// survive on the PVC's .env and beat the allowlist this CR sets — the authz check
	// reads the per-platform allow-all before it reads any allowlist at all.
	if got, ok := envMap["GOOGLE_CHAT_ALLOW_ALL_USERS"]; !ok || got.Value != "false" {
		t.Errorf("expected GOOGLE_CHAT_ALLOW_ALL_USERS=false when allowed users is populated, got %#v", got)
	}
	if envMap["API_SERVER_ENABLED"].Value != "true" {
		t.Errorf("expected API_SERVER_ENABLED true, got %s", envMap["API_SERVER_ENABLED"].Value)
	}
	if envMap["API_SERVER_HOST"].Value != "127.0.0.1" {
		t.Errorf("expected API_SERVER_HOST 127.0.0.1, got %s", envMap["API_SERVER_HOST"].Value)
	}
	// Must equal the model renderConfigYAML pins, not merely be non-empty: a
	// session created without an explicit model persists this string and sends
	// it upstream, so anything LiteLLM does not serve 400s the session's first
	// turn. See the env var's comment in platformagent_manifests.go.
	if envMap["API_SERVER_MODEL_NAME"].Value != "model-default" {
		t.Errorf("expected API_SERVER_MODEL_NAME model-default, got %s", envMap["API_SERVER_MODEL_NAME"].Value)
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
	fbContainer := containerByName(t, dep.Spec.Template.Spec.Containers, "fluent-bit")
	if fbContainer.Name != "fluent-bit" {
		t.Errorf("expected container name fluent-bit, got %s", fbContainer.Name)
	}
	if fbContainer.Image != "fluent/fluent-bit:5.1.0" {
		t.Errorf("expected fluent-bit image fluent/fluent-bit:5.1.0, got %s", fbContainer.Image)
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

			dep := buildDeployment(agent, "hash1", "hash2", "hash3", "hash4", nil, renderOptions{imageVolumeSupported: true})
			if dep.Spec.Template.Spec.ShareProcessNamespace == nil || !*dep.Spec.Template.Spec.ShareProcessNamespace {
				t.Errorf("expected ShareProcessNamespace to be true, got %v", dep.Spec.Template.Spec.ShareProcessNamespace)
			}
			if len(dep.Spec.Template.Spec.Containers) != 4 {
				t.Fatalf("expected dashboard deployment plus credential sidecar to have 4 containers, got %d", len(dep.Spec.Template.Spec.Containers))
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
			if dep.Spec.Template.Spec.Containers[3].Name != "envoy-credential-proxy" {
				t.Errorf("expected container 3 to be envoy-credential-proxy, got %s", dep.Spec.Template.Spec.Containers[3].Name)
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

	dep := buildDeployment(agent, "hash1", "hash2", "hash3", "hash4", nil, renderOptions{imageVolumeSupported: true})
	if dep.Spec.Template.Spec.ShareProcessNamespace != nil {
		t.Errorf("expected ShareProcessNamespace to be nil, got %v", *dep.Spec.Template.Spec.ShareProcessNamespace)
	}
	if len(dep.Spec.Template.Spec.Containers) != 3 {
		t.Fatalf("expected dashboard-disabled deployment plus credential sidecar to have 3 containers, got %d", len(dep.Spec.Template.Spec.Containers))
	}
	if dep.Spec.Template.Spec.Containers[0].Name != "platform-agent" {
		t.Errorf("expected container 0 to be platform-agent, got %s", dep.Spec.Template.Spec.Containers[0].Name)
	}
	if dep.Spec.Template.Spec.Containers[1].Name != "fluent-bit" {
		t.Errorf("expected container 1 to be fluent-bit, got %s", dep.Spec.Template.Spec.Containers[1].Name)
	}
	if dep.Spec.Template.Spec.Containers[2].Name != "envoy-credential-proxy" {
		t.Errorf("expected container 2 to be envoy-credential-proxy, got %s", dep.Spec.Template.Spec.Containers[2].Name)
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

func TestSafeSandboxEnvOverridesPassesOtelSdkDisabled(t *testing.T) {
	// The chart documents OTEL_SDK_DISABLED as the off-switch for clusters
	// with no OTLP collector, where the exporter otherwise retries an
	// unresolvable hostname for the life of the pod. Off the allowlist the
	// documented recipe renders, validates, and silently does nothing.
	got := safeSandboxEnvOverrides([]corev1.EnvVar{
		{Name: "OTEL_SDK_DISABLED", Value: "true"},
	})
	if len(got) != 1 || got[0].Name != "OTEL_SDK_DISABLED" || got[0].Value != "true" {
		t.Fatalf("expected OTEL_SDK_DISABLED to survive the allowlist, got %#v", got)
	}
}

func TestSafeSandboxEnvOverridesPassesAlertLimits(t *testing.T) {
	// The session server reads its daily alert ceilings from the environment,
	// so an operator has to be able to tune or disable them on the CR. Without
	// these names on the allowlist the documented override silently does
	// nothing and the only way to change a limit is a new image. One name per
	// severity the server caps, Info included.
	custom := []corev1.EnvVar{
		{Name: "ALERT_DAILY_LIMIT_CRITICAL", Value: "25"},
		{Name: "ALERT_DAILY_LIMIT_INFO", Value: "3"},
		{Name: "ALERT_DAILY_LIMIT_WARNING", Value: "0"},
		{Name: "SESSION_KV_DB_PATH", Value: "/tmp/hijacked.db"},
		{
			Name: "ALERT_DAILY_LIMIT_CRITICAL",
			ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: "s"},
				Key:                  "k",
			}},
		},
	}

	got := safeSandboxEnvOverrides(custom)
	values := map[string]string{}
	for _, e := range got {
		if e.ValueFrom != nil {
			t.Errorf("ValueFrom must never survive the allowlist, got %#v", e)
		}
		values[e.Name] = e.Value
	}

	if values["ALERT_DAILY_LIMIT_CRITICAL"] != "25" {
		t.Errorf("expected the critical ceiling to be overridable, got %q", values["ALERT_DAILY_LIMIT_CRITICAL"])
	}
	// Info is capped too — nothing on the watcher path filters on Event.Type,
	// so Normal-type events with an allowlisted reason arrive as Info.
	if values["ALERT_DAILY_LIMIT_INFO"] != "3" {
		t.Errorf("expected the info ceiling to be overridable, got %q", values["ALERT_DAILY_LIMIT_INFO"])
	}
	// 0 is how a severity's cap is turned off; it must survive as a literal
	// rather than being dropped as an empty-ish value.
	if v, ok := values["ALERT_DAILY_LIMIT_WARNING"]; !ok || v != "0" {
		t.Errorf("expected the warning ceiling to be settable to 0, got %q (present=%v)", v, ok)
	}
	// Widening the allowlist must not have widened it to everything.
	if _, ok := values["SESSION_KV_DB_PATH"]; ok {
		t.Errorf("SESSION_KV_DB_PATH must stay operator-owned, got %#v", got)
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
	if len(container.Command) != 1 || container.Command[0] != "/usr/local/bin/start-services" {
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
	// A tag embedded in the image wins over the tag field, matching the agent container.
	if got := resolveCredentialProxyImage(&agentv1alpha1.DeploymentSpec{Image: "example/platform-agent:v2", Tag: ptr.To("latest")}); got != "example/credential-proxy:v2" {
		t.Fatalf("expected sidecar tag to follow the agent image's embedded tag: %s", got)
	}
	// The agent image's digest cannot name the proxy image; fall back to latest.
	digestImage := "example/platform-agent@sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40"
	if got := resolveCredentialProxyImage(&agentv1alpha1.DeploymentSpec{Image: digestImage}); got != "example/credential-proxy:latest" {
		t.Fatalf("expected latest tag for digest-pinned agent image: %s", got)
	}
	if got := resolveCredentialProxyImage(&agentv1alpha1.DeploymentSpec{Image: digestImage, Tag: ptr.To("v3")}); got != "example/credential-proxy:v3" {
		t.Fatalf("expected tag field for digest-pinned agent image: %s", got)
	}
}

func TestImageEnvOverrides(t *testing.T) {
	t.Setenv("PLATFORM_AGENT_IMAGE", "registry.corp/mirror/platform-agent:v1.2.3")

	if got := defaultPlatformAgentImage(); got != "registry.corp/mirror/platform-agent:v1.2.3" {
		t.Fatalf("expected PLATFORM_AGENT_IMAGE to override the default agent image, got %s", got)
	}
	// The credential proxy follows the overridden agent image's registry.
	if got := resolveCredentialProxyImage(nil); got != "registry.corp/mirror/credential-proxy:v1.2.3" {
		t.Fatalf("expected credential proxy derived from PLATFORM_AGENT_IMAGE, got %s", got)
	}
	// A deployment block without an image gets tag: "latest" defaulted by the
	// CRD/webhook; the sidecar must still follow PLATFORM_AGENT_IMAGE's tag,
	// exactly like the agent container does.
	if got := resolveCredentialProxyImage(&agentv1alpha1.DeploymentSpec{Tag: ptr.To("latest")}); got != "registry.corp/mirror/credential-proxy:v1.2.3" {
		t.Fatalf("expected sidecar tag in lockstep with PLATFORM_AGENT_IMAGE despite defaulted tag field, got %s", got)
	}
	// A CR-level image still wins over the operator-level default.
	if got := resolveAgentImage(&agentv1alpha1.DeploymentSpec{Image: "gcr.io/my-proj/agent:v9"}, defaultPlatformAgentImage()); got != "gcr.io/my-proj/agent:v9" {
		t.Fatalf("expected spec.deployment.image to win over PLATFORM_AGENT_IMAGE, got %s", got)
	}

	// An explicit proxy override beats derivation, including from a CR image.
	t.Setenv("CREDENTIAL_PROXY_IMAGE", "registry.corp/mirror/kube-agents-proxy:v1.2.3")
	if got := resolveCredentialProxyImage(&agentv1alpha1.DeploymentSpec{Image: "example/platform-agent"}); got != "registry.corp/mirror/kube-agents-proxy:v1.2.3" {
		t.Fatalf("expected CREDENTIAL_PROXY_IMAGE to win, got %s", got)
	}
}

func TestFluentBitImageEnvOverride(t *testing.T) {
	if got := fluentBitImage(); got != "fluent/fluent-bit:5.1.0" {
		t.Fatalf("unexpected default fluent-bit image: %s", got)
	}
	t.Setenv("FLUENT_BIT_IMAGE", "registry.corp/mirror/fluent-bit:5.1.0")

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "my-ns"},
	}
	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012", "policy3456", nil, renderOptions{imageVolumeSupported: true})
	found := false
	for _, c := range dep.Spec.Template.Spec.Containers {
		if c.Name == "fluent-bit" {
			found = true
			if c.Image != "registry.corp/mirror/fluent-bit:5.1.0" {
				t.Fatalf("expected FLUENT_BIT_IMAGE override on sidecar, got %s", c.Image)
			}
		}
	}
	if !found {
		t.Fatal("fluent-bit sidecar container not found")
	}
}

// TestNoPublicRegistryWhenMirrored is the end-to-end guard that #501's review
// found missing: when the operator is configured for a private mirror (the
// air-gapped install), a PlatformAgent that omits spec.deployment.image must
// render EVERY container off that mirror, with no public-registry reference
// leaking through. It also fails loudly if a new container is later added
// without an override path.
func TestNoPublicRegistryWhenMirrored(t *testing.T) {
	const mirror = "registry.corp/mirror"
	t.Setenv("PLATFORM_AGENT_IMAGE", mirror+"/platform-agent:v1.2.3")
	t.Setenv("FLUENT_BIT_IMAGE", mirror+"/fluent-bit:5.1.0")
	// CREDENTIAL_PROXY_IMAGE deliberately left unset: the sidecar must derive
	// its registry from PLATFORM_AGENT_IMAGE, not fall back to ghcr.io.

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "mirrored-agent", Namespace: "my-ns"},
	}
	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012", "policy3456", nil, renderOptions{imageVolumeSupported: true})

	var images []string
	for _, c := range dep.Spec.Template.Spec.InitContainers {
		images = append(images, c.Image)
	}
	for _, c := range dep.Spec.Template.Spec.Containers {
		images = append(images, c.Image)
	}
	if len(images) == 0 {
		t.Fatal("no container images rendered")
	}
	for _, img := range images {
		if !strings.HasPrefix(img, mirror+"/") {
			t.Errorf("image %q is not served from the configured mirror %q; a public-registry reference leaks on the air-gapped install path", img, mirror)
		}
	}
}

// TestExampleCRDoesNotPinPublicRegistry guards the copy-paste entry point from
// #501's review issue 1: the shipped example CR must not hardcode a public
// registry in spec.deployment.image, or a behind-the-firewall user who copies
// it silently pins ghcr.io regardless of every override. Omitting the image
// (the safe default) lets the operator's PLATFORM_AGENT_IMAGE / compiled-in
// default apply.
func TestExampleCRDoesNotPinPublicRegistry(t *testing.T) {
	path := filepath.Join("..", "..", "examples", "platformagent.yaml")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading example CR: %v", err)
	}
	var agent agentv1alpha1.PlatformAgent
	if err := k8syaml.Unmarshal(data, &agent); err != nil {
		t.Fatalf("unmarshaling %s: %v", path, err)
	}
	if agent.Spec.Deployment == nil || agent.Spec.Deployment.Image == "" {
		return // image omitted — the safe default
	}
	img := agent.Spec.Deployment.Image
	for _, host := range []string{"ghcr.io", "docker.io", "quay.io", "registry.k8s.io"} {
		if strings.HasPrefix(img, host+"/") {
			t.Errorf("example CR pins spec.deployment.image to a public registry (%q); omit the field so private-registry installs are not silently overridden", img)
		}
	}
}

// TestEventWatcherTokenEnvMatchesStartServices ties the two halves of the
// watcher's bearer-token wiring together. deploy/shared/start-services.sh names
// the variable in --token-env; this package injects a variable of that name into
// the credential-proxy container. Nothing else reads the shell script, so a
// rename on this side passes `go test` while the script keeps the old name — and
// the watcher then exits on every start with "bearer token env var ... is empty",
// in a container that stays Ready. Deriving the expected name from the script
// rather than hardcoding it is the point: both sides have to move together.
func TestEventWatcherTokenEnvMatchesStartServices(t *testing.T) {
	path := filepath.Join("..", "..", "..", "deploy", "shared", "start-services.sh")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading %s: %v", path, err)
	}
	match := regexp.MustCompile(`--token-env=([A-Za-z_][A-Za-z0-9_]*)`).FindSubmatch(data)
	if match == nil {
		t.Fatalf("%s no longer passes --token-env to k8s-event-watcher; the watcher cannot authenticate to the Session KV server without it", path)
	}
	tokenEnv := string(match[1])

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "my-ns"},
	}
	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012", "policy3456", nil, renderOptions{imageVolumeSupported: true})
	proxyC := containerByName(t, dep.Spec.Template.Spec.Containers, "envoy-credential-proxy")
	for _, env := range proxyC.Env {
		if env.Name != tokenEnv {
			continue
		}
		if env.Value == "" && env.ValueFrom == nil {
			t.Fatalf("credential proxy sets %s to nothing; the watcher treats an empty token as fatal", tokenEnv)
		}
		return
	}
	t.Fatalf("%s passes --token-env=%s, but the credential proxy container has no such variable; the watcher will exit on every start", path, tokenEnv)
}

func TestKustomizeNetworkPolicies_PodSelectorMatchesCommonLabels(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "platform-agent",
			Namespace: "kubeagents-system",
		},
	}
	expectedLabels := commonLabels(agent)
	expectedName := expectedLabels[labelName] // "platform-agent"

	policyFiles := []string{
		filepath.Join("..", "..", "..", "deploy", "kustomize", "platform", "networkpolicy-ingress.yaml"),
		filepath.Join("..", "..", "..", "deploy", "kustomize", "platform", "networkpolicy-core-egress.yaml"),
		filepath.Join("..", "..", "..", "deploy", "kustomize", "platform", "networkpolicy-internal-egress.yaml"),
		filepath.Join("..", "..", "..", "deploy", "kustomize", "platform", "networkpolicy-apiserver-egress.yaml"),
		filepath.Join("..", "..", "..", "deploy", "kustomize", "platform", "networkpolicy-external-egress.yaml"),
		filepath.Join("..", "..", "..", "deploy", "kustomize", "gke-dataplane-v2", "fqdn-networkpolicy.yaml"),
	}

	for _, path := range policyFiles {
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("failed to read policy file %s: %v", path, err)
		}
		var manifest struct {
			Metadata struct {
				Name string `yaml:"name"`
			} `yaml:"metadata"`
			Spec struct {
				PodSelector struct {
					MatchLabels map[string]string `yaml:"matchLabels"`
				} `yaml:"podSelector"`
			} `yaml:"spec"`
		}
		if err := yaml.Unmarshal(data, &manifest); err != nil {
			t.Fatalf("failed to unmarshal YAML %s: %v", path, err)
		}
		got := manifest.Spec.PodSelector.MatchLabels[labelName]
		if got != expectedName {
			t.Errorf("policy %s (%s): expected podSelector.matchLabels[%q]=%q, got %q", manifest.Metadata.Name, path, labelName, expectedName, got)
		}
	}
}

// fqdnPatternsFromPolicy returns the egress match patterns buildFQDNNetworkPolicy emits.
func fqdnPatternsFromPolicy(t *testing.T) []string {
	t.Helper()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "platform-agent",
			Namespace: "kubeagents-system",
		},
	}
	u := buildFQDNNetworkPolicy(agent)
	spec, ok := u.Object["spec"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected spec map in FQDN policy")
	}
	egressList, ok := spec["egress"].([]interface{})
	if !ok || len(egressList) == 0 {
		t.Fatalf("expected egress list in FQDN policy")
	}
	firstRule := egressList[0].(map[string]interface{})
	matchesList, ok := firstRule["matches"].([]interface{})
	if !ok || len(matchesList) == 0 {
		t.Fatalf("expected matches list in FQDN policy")
	}
	var patterns []string
	for _, m := range matchesList {
		if mMap, isMap := m.(map[string]interface{}); isMap {
			if p, isStr := mMap["pattern"].(string); isStr {
				patterns = append(patterns, p)
			}
		}
	}
	return patterns
}

// fqdnPatternToRegexp compiles an FQDNNetworkPolicy match pattern the way the
// Dataplane V2 (Cilium) engine does: dots are literal and a wildcard spans DNS
// characters only, so it stops at a label boundary. GKE documents the same rule
// — "*.company.com" matches "api.company.com" but not "eu.api.company.com".
func fqdnPatternToRegexp(t *testing.T, pattern string) *regexp.Regexp {
	t.Helper()
	escaped := strings.ReplaceAll(pattern, ".", "[.]")
	escaped = strings.ReplaceAll(escaped, "*", "[-a-zA-Z0-9_]*")
	re, err := regexp.Compile("^" + escaped + "$")
	if err != nil {
		t.Fatalf("pattern %q does not compile: %v", pattern, err)
	}
	return re
}

// TestFQDNPatternList_MatchesRealHostnames pins the egress allowlist against
// hostnames the gateway actually dials. TestFQDNPatternList_MatchesKustomizeManifest
// only proves the two copies of the list agree — it would pass just as happily
// if both were wrong, which is how "*.gke.goog" was first shipped one label
// short of every DNS control-plane endpoint it was added to allow.
func TestFQDNPatternList_MatchesRealHostnames(t *testing.T) {
	patterns := fqdnPatternsFromPolicy(t)

	hostnames := []string{
		// GKE DNS-based control plane endpoint: <cluster-hash>-<project-number>.<region>.gke.goog
		//
		// Synthetic, but the shape is copied from live clusters: a 36-character
		// hash, a project number, and a location label. Do not paste a real
		// endpoint in — the hostname carries the project number of whoever's
		// cluster it came from, and what this test needs is the shape.
		"gke-0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b-123456789012.us-central1.gke.goog",
		// A zonal cluster puts the zone where the region sits, so the label is
		// longer but the shape is unchanged. Keeping both means a pattern
		// narrowed to a region-shaped label fails here rather than in the field.
		"gke-9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e-210987654321.us-central1-a.gke.goog",
		"container.googleapis.com",
		"oauth2.googleapis.com",
		"accounts.google.com",
		"us-central1-docker.pkg.dev",
		"us.gcr.io",
		"github.com",
		"api.github.com",
		"objects.githubusercontent.com",
		"slack.com",
	}

	for _, host := range hostnames {
		matched := false
		for _, p := range patterns {
			if fqdnPatternToRegexp(t, p).MatchString(host) {
				matched = true
				break
			}
		}
		if !matched {
			t.Errorf("no FQDN egress pattern matches %q; the gateway cannot reach it under FQDN network policy (patterns: %v)", host, patterns)
		}
	}
}

func TestFQDNPatternList_MatchesKustomizeManifest(t *testing.T) {
	goPatterns := fqdnPatternsFromPolicy(t)

	path := filepath.Join("..", "..", "..", "deploy", "kustomize", "gke-dataplane-v2", "fqdn-networkpolicy.yaml")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("failed to read %s: %v", path, err)
	}
	var manifest struct {
		Spec struct {
			Egress []struct {
				Matches []struct {
					Pattern string `yaml:"pattern"`
				} `yaml:"matches"`
			} `yaml:"egress"`
		} `yaml:"spec"`
	}
	if err := yaml.Unmarshal(data, &manifest); err != nil {
		t.Fatalf("failed to unmarshal %s: %v", path, err)
	}
	if len(manifest.Spec.Egress) == 0 {
		t.Fatalf("expected egress in YAML manifest %s", path)
	}
	var yamlPatterns []string
	for _, m := range manifest.Spec.Egress[0].Matches {
		yamlPatterns = append(yamlPatterns, m.Pattern)
	}

	if !reflect.DeepEqual(goPatterns, yamlPatterns) {
		t.Errorf("FQDN patterns diverge between Go code and YAML manifest: Go=%v, YAML=%v", goPatterns, yamlPatterns)
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

	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012", "policy3456", nil, renderOptions{imageVolumeSupported: true})
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

	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012", "policy3456", nil, renderOptions{imageVolumeSupported: true})
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

	dep := buildDeployment(agent, "abcd1234", "efgh5678", "ijkl9012", "policy3456", nil, renderOptions{imageVolumeSupported: true})
	container := dep.Spec.Template.Spec.Containers[0]
	envMap := make(map[string]corev1.EnvVar)
	for _, env := range container.Env {
		envMap[env.Name] = env
	}

	// Emitted, and empty. The managed .env pins SLACK_ALLOWED_USERS on every reconcile so
	// the agent cannot write its own; a container env that omits the key when the CR sets
	// no allowlist would leave the two renders disagreeing about a key that decides who
	// may talk to the agent.
	v, ok := envMap["SLACK_ALLOWED_USERS"]
	if !ok || v.Value != "" {
		t.Errorf("expected SLACK_ALLOWED_USERS present and empty, got %q (present=%v)", v.Value, ok)
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

	cm := buildConfigMap(agent, nil)
	yamlContent := defaultProfileYAML(t, cm)
	if !strings.Contains(yamlContent, "slack:") || !strings.Contains(yamlContent, "enabled: true") {
		t.Errorf("expected config.yaml to enable slack platform, got:\n%s", yamlContent)
	}
}

// The rendered config.yaml is subPath-mounted OVER $HERMES_HOME/config.yaml, so it —
// not the image's agents/chat/config.yaml — is what the Slack adapter reads on a
// deployed agent. Without `extra.rich_blocks` here, every fleet report reaches Slack
// as flat mrkdwn with its tables as literal `|---|` rows.
func TestBuildConfigMapSlackRichBlocks(t *testing.T) {
	for _, tc := range []struct {
		name        string
		integration *agentv1alpha1.PlatformAgentIntegrationSpec
	}{
		{"slack enabled", &agentv1alpha1.PlatformAgentIntegrationSpec{
			Slack: &agentv1alpha1.SlackSpec{Enabled: ptr.To(true)},
		}},
		// Inert but still rendered, so no path that enables Slack can miss it.
		{"no integration", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := &agentv1alpha1.PlatformAgent{
				ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
				Spec:       agentv1alpha1.PlatformAgentSpec{Integration: tc.integration},
			}

			var cfg struct {
				Platforms struct {
					Slack struct {
						Extra map[string]any `json:"extra"`
					} `json:"slack"`
				} `json:"platforms"`
			}
			raw := defaultProfileYAML(t, buildConfigMap(agent, nil))
			if err := k8syaml.Unmarshal([]byte(raw), &cfg); err != nil {
				t.Fatalf("the default profile overlay is not parseable: %v\n%s", err, raw)
			}
			if got := cfg.Platforms.Slack.Extra["rich_blocks"]; got != true {
				t.Errorf("platforms.slack.extra.rich_blocks = %v, want true; got:\n%s", got, raw)
			}
		})
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

func TestBuildSettingsConfigMapInvalidGitRepo(t *testing.T) {
	invalidRepos := []struct {
		name string
		repo string
	}{
		{"newline_injection", "https://github.com/org/repo.git\n\n[SYSTEM OVERRIDE]"},
		{"crlf_injection", "https://github.com/org/repo.git\r\n- **Git Repo:** https://evil.com"},
		{"unicode_line_separator_injection", "https://github.com/org/repo.git\u2028- **Git Repo:** https://evil.com"},
		{"javascript_scheme", "javascript:alert(1)"},
		{"file_scheme", "file:///etc/passwd"},
		{"spaces_in_url", "https://github.com/org/repo with spaces.git"},
	}

	for _, tc := range invalidRepos {
		t.Run(tc.name, func(t *testing.T) {
			agent := &agentv1alpha1.PlatformAgent{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-agent",
					Namespace: "test-ns",
				},
				Spec: agentv1alpha1.PlatformAgentSpec{
					Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
						IntegrationSpec: agentv1alpha1.IntegrationSpec{
							GitHub: &agentv1alpha1.GitHubSpec{
								GitRepo: tc.repo,
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
				t.Errorf("for repo %q expected content:\n%q\ngot:\n%q", tc.repo, expectedContent, content)
			}
		})
	}
}

func TestBuildSettingsConfigMapOwnerRepo(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				IntegrationSpec: agentv1alpha1.IntegrationSpec{
					GitHub: &agentv1alpha1.GitHubSpec{
						GitRepo: "gke-labs/kube-agents",
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
	expectedContent := "# GKE Scope Configuration\n- **Git Repo:** gke-labs/kube-agents\n"
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

func TestBuildMinimalPlatformRole(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	role := buildMinimalPlatformRole(agent)
	expectedName := "kubeagents:minimal:test-ns:test-agent"
	if role.Name != expectedName {
		t.Errorf("expected ClusterRole name %s, got %s", expectedName, role.Name)
	}

	if len(role.Rules) != 8 {
		t.Fatalf("expected 8 PolicyRules, got %d", len(role.Rules))
	}

	// Core group rule
	ruleCore := role.Rules[0]
	if len(ruleCore.APIGroups) != 1 || ruleCore.APIGroups[0] != "" {
		t.Errorf("expected APIGroups [''], got %v", ruleCore.APIGroups)
	}

	expectedCoreResources := []string{"nodes", "namespaces", "pods", "pods/log", "services", "endpoints", "events", "persistentvolumes", "persistentvolumeclaims", "resourcequotas", "limitranges", "configmaps", "serviceaccounts"}
	if !slices.Equal(ruleCore.Resources, expectedCoreResources) {
		t.Errorf("expected Resources %v, got %v", expectedCoreResources, ruleCore.Resources)
	}

	expectedVerbs := []string{"get", "list", "watch"}
	if !slices.Equal(ruleCore.Verbs, expectedVerbs) {
		t.Errorf("expected Verbs %v, got %v", expectedVerbs, ruleCore.Verbs)
	}

	// Metrics group rule
	ruleMetrics := role.Rules[1]
	if len(ruleMetrics.APIGroups) != 1 || ruleMetrics.APIGroups[0] != "metrics.k8s.io" {
		t.Errorf("expected APIGroups ['metrics.k8s.io'], got %v", ruleMetrics.APIGroups)
	}
	expectedMetricsResources := []string{"nodes", "pods"}
	if !slices.Equal(ruleMetrics.Resources, expectedMetricsResources) {
		t.Errorf("expected Metrics Resources %v, got %v", expectedMetricsResources, ruleMetrics.Resources)
	}
	expectedMetricsVerbs := []string{"get", "list"}
	if !slices.Equal(ruleMetrics.Verbs, expectedMetricsVerbs) {
		t.Errorf("expected Metrics Verbs %v, got %v", expectedMetricsVerbs, ruleMetrics.Verbs)
	}
}

func TestBuildPlatformLocalRole(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	role := buildPlatformLocalRole(agent)
	expectedName := "kubeagents:local:test-ns:test-agent"
	if role.Name != expectedName {
		t.Errorf("expected Role name %s, got %s", expectedName, role.Name)
	}

	if len(role.Rules) != 1 {
		t.Fatalf("expected 1 PolicyRule, got %d", len(role.Rules))
	}

	rule := role.Rules[0]
	if len(rule.APIGroups) != 1 || rule.APIGroups[0] != "kubeagents.x-k8s.io" {
		t.Errorf("expected APIGroups ['kubeagents.x-k8s.io'], got %v", rule.APIGroups)
	}

	expectedVerbs := []string{"get", "list", "watch"}
	if !slices.Equal(rule.Verbs, expectedVerbs) {
		t.Errorf("expected Verbs %v, got %v", expectedVerbs, rule.Verbs)
	}
}

func TestBuildRoleBinding(t *testing.T) {
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

	rb := buildRoleBinding(agent, "test-binding", "test-role")
	if rb.Name != "test-binding" {
		t.Errorf("expected RoleBinding name test-binding, got %s", rb.Name)
	}
	if rb.Namespace != "test-ns" {
		t.Errorf("expected RoleBinding namespace test-ns, got %s", rb.Namespace)
	}
	if rb.RoleRef.Name != "test-role" || rb.RoleRef.Kind != "Role" {
		t.Errorf("expected RoleRef to Role test-role, got %v", rb.RoleRef)
	}
	if len(rb.Subjects) != 1 || rb.Subjects[0].Name != "custom-sa" {
		t.Errorf("expected Subject custom-sa, got %v", rb.Subjects)
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
	if crb.Labels["kubeagents.x-k8s.io/agent-namespace"] != "test-ns" {
		t.Errorf("expected label agent-namespace test-ns, got %s", crb.Labels["kubeagents.x-k8s.io/agent-namespace"])
	}
	if crb.Labels["kubeagents.x-k8s.io/agent-name"] != "test-agent" {
		t.Errorf("expected label agent-name test-agent, got %s", crb.Labels["kubeagents.x-k8s.io/agent-name"])
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

	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
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

	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
	if *dep.Spec.Replicas != 3 {
		t.Errorf("expected 3 replicas when explicitly set, got %d", *dep.Spec.Replicas)
	}

	// Leadership is the Lease that leader_elect.py holds, not a config key: nothing in
	// Hermes or in this repo reads a `leader_election` block out of config.yaml, and the
	// operator drove it through the container env below all along. The render used to
	// emit the block anyway; in a machine-global managed scope that put one profile's
	// lease name on every profile in the pod for no reader at all.
	yamlContent := defaultProfileYAML(t, buildConfigMap(agent, nil))
	if strings.Contains(yamlContent, "leader_election:") {
		t.Errorf("managed config must not carry a leader_election block, got:\n%s", yamlContent)
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

// haAgent builds a PlatformAgent with `replicas` replicas and the dashboard left at its
// default (enabled), which is the shape both shared-state regressions needed.
func haAgent(name string, replicas int32) *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "test-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{
					Availability: &agentv1alpha1.AvailabilitySpec{Replicas: ptr.To(replicas)},
				},
			},
		},
	}
}

func containerNamed(t *testing.T, dep *appsv1.Deployment, name string) corev1.Container {
	t.Helper()
	for _, c := range dep.Spec.Template.Spec.Containers {
		if c.Name == name {
			return c
		}
	}
	t.Fatalf("no container named %q in the pod spec", name)
	return corev1.Container{}
}

func envValue(c corev1.Container, name string) (string, bool) {
	// Last wins, as the kubelet resolves duplicates.
	value, found := "", false
	for _, env := range c.Env {
		if env.Name == name {
			value, found = env.Value, true
		}
	}
	return value, found
}

// TestLeaderElectionKeepsTheImageEntrypoint guards the HA path against the regression
// where nothing built the shared tree.
//
// Setting Command on this container replaces the image ENTRYPOINT
// (/usr/local/bin/agent-entrypoint), so the setup that seeds $HERMES_HOME from
// /opt/defaults, scaffolds the platform profile, links plugin volumes, merges the config
// overlays and starts the Session KV server on 8699 never ran at all — leader_elect.py
// went straight to Hermes. The dashboard was quietly covering for it by running the setup
// itself; once the dashboard was correctly gated out, an HA pod had no container doing it.
func TestLeaderElectionKeepsTheImageEntrypoint(t *testing.T) {
	dep := buildDeployment(haAgent("ha-agent", 2), "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
	gateway := containerNamed(t, dep, "platform-agent")

	if len(gateway.Command) != 0 {
		t.Errorf("Command must stay unset so the image ENTRYPOINT runs the shared-state "+
			"setup before leader_elect.py; got %v", gateway.Command)
	}
	want := []string{"/opt/hermes/.venv/bin/python3", "/opt/data/leader_elect.py"}
	if !reflect.DeepEqual(gateway.Args, want) {
		t.Errorf("expected the leader-election wrapper as the entrypoint's exec target %v, got %v", want, gateway.Args)
	}
}

func TestSingleReplicaGatewayUsesTheImageCMD(t *testing.T) {
	dep := buildDeployment(haAgent("solo-agent", 1), "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
	gateway := containerNamed(t, dep, "platform-agent")

	if len(gateway.Command) != 0 || len(gateway.Args) != 0 {
		t.Errorf("expected the image CMD (`hermes gateway run`) to stand, got command=%v args=%v",
			gateway.Command, gateway.Args)
	}
}

// TestSharedStateOwnershipIsDeclaredNotInferred pins the contract in step 1.5 of
// deploy/shared/docker-entrypoint.sh: the operator names the owner, at every replica
// count, rather than leaving the entrypoint to infer it from argv.
//
// Inference cannot get the HA case right — the gateway's argv is `python3
// leader_elect.py` there and the word `gateway` appears nowhere in it — so the container
// that must do the setup reads as a sidecar and is skipped.
func TestSharedStateOwnershipIsDeclaredNotInferred(t *testing.T) {
	for _, replicas := range []int32{1, 2} {
		t.Run(fmt.Sprintf("replicas=%d", replicas), func(t *testing.T) {
			dep := buildDeployment(haAgent("owner-agent", replicas), "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})

			for _, tc := range []struct{ container, want string }{
				{"platform-agent", "owner"},
				{"platform-agent-dashboard", "skip"},
			} {
				got, found := envValue(containerNamed(t, dep, tc.container), "AGENT_SHARED_STATE_SETUP")
				if !found {
					t.Errorf("%s: AGENT_SHARED_STATE_SETUP is unset, leaving the entrypoint to guess from argv", tc.container)
				} else if got != tc.want {
					t.Errorf("%s: expected AGENT_SHARED_STATE_SETUP=%s, got %s", tc.container, tc.want, got)
				}
			}
		})
	}
}

// TestAPIServerModelMatchesTheProfileModel pins the two halves of the model name
// together. The gateway's API server resolves its model once at startup, preferring
// API_SERVER_MODEL_NAME and falling back to a hardcoded "hermes-agent" that LiteLLM
// does not serve; the profile name in between is skipped because the provider is
// custom. Without the variable every session created through the API asks for a model
// that does not exist and dies on its first completion, while Chat keeps working
// because it resolves per message — so the failure is invisible in manual testing.
func TestAPIServerModelMatchesTheProfileModel(t *testing.T) {
	agent := haAgent("model-agent", 1)
	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})

	got, found := envValue(containerNamed(t, dep, "platform-agent"), "API_SERVER_MODEL_NAME")
	if !found {
		t.Fatal("API_SERVER_MODEL_NAME is unset; the API server will fall back to hermes-agent " +
			"and LiteLLM will reject every session it creates")
	}

	// The default profile's whole-file rendering is the managed-scope key; there is no
	// `config.yaml` key in the ConfigMap any more.
	yamlContent := buildConfigMap(agent, nil).Data[managedConfigKey]
	if !strings.Contains(yamlContent, "model: "+got) {
		t.Errorf("API_SERVER_MODEL_NAME=%s does not match the model in the generated profile config; "+
			"the two must agree or API-created sessions request a model LiteLLM does not serve:\n%s",
			got, yamlContent)
	}
}

// chatAgent builds a PlatformAgent with both chat platforms enabled and an allowlist on
// each, which is what the managed-scope assertions below need to distinguish a pinned
// value from a defaulted one.
func chatAgent() *agentv1alpha1.PlatformAgent {
	a := newTestPlatformAgent()
	a.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{
		GoogleChat: &agentv1alpha1.GoogleChatSpec{
			Enabled:          ptr.To(true),
			ProjectID:        "proj",
			SubscriptionName: "sub",
			AllowedUsers:     []string{"users/alice"},
			HomeChannel:      "spaces/SEEDED",
		},
		Slack: &agentv1alpha1.SlackSpec{
			Enabled:         ptr.To(true),
			AllowedUsers:    []string{"U123"},
			HomeChannel:     "C0SEEDED",
			HomeChannelName: "#seeded",
		},
	}
	return a
}

// The whole point of issue #658: the front door's config.yaml must NOT be mounted, at
// any key, over the agent's own file. A mount point is read-only, and `/sethome`,
// `monitoring.install_id` and every saved slash-command preference are writes to that
// exact path. The operator's rendering reaches the agent as the managed scope instead —
// a separate directory Hermes overlays at load time.
func TestRenderedConfigIsNotMountedOverTheAgentsOwn(t *testing.T) {
	dep := buildDeployment(chatAgent(), "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
	gateway := containerNamed(t, dep, "platform-agent")

	for _, m := range gateway.VolumeMounts {
		if m.MountPath == defaultAgentHome+"/config.yaml" {
			t.Fatalf("the agent's config.yaml is a mount point again, so every runtime write to it "+
				"fails (issue #658): %+v", m)
		}
	}
}

// The managed scope has to arrive as a DIRECTORY mount, and read-only.
//
// Not a subPath: a subPath mount never receives kubelet ConfigMap updates, so an
// operator-side policy change would sit in the ConfigMap and never reach the running
// pod. managed_scope.py caches on (mtime_ns, size) precisely so a directory mount can be
// picked up in place.
func TestManagedScopeIsADirectoryMount(t *testing.T) {
	dep := buildDeployment(chatAgent(), "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
	gateway := containerNamed(t, dep, "platform-agent")

	var mount *corev1.VolumeMount
	for i, m := range gateway.VolumeMounts {
		if m.MountPath == managedScopeDir {
			mount = &gateway.VolumeMounts[i]
		}
	}
	if mount == nil {
		t.Fatalf("no mount at %s, so hermes finds no managed scope and every pinned key is "+
			"silently writable (managed scope fails open); mounts were %+v", managedScopeDir, gateway.VolumeMounts)
	}
	if mount.SubPath != "" {
		t.Errorf("the managed scope is subPath-mounted (%q); the kubelet does not update a subPath, "+
			"so a policy change would never reach a running pod", mount.SubPath)
	}
	if !mount.ReadOnly {
		t.Error("the managed scope must be read-only: it is what the agent is not allowed to change")
	}
	if got, found := envValue(gateway, "HERMES_MANAGED_DIR"); !found || got != managedScopeDir {
		t.Errorf("HERMES_MANAGED_DIR = %q (found=%v), want %q", got, found, managedScopeDir)
	}

	// Both keys have to be projected, and under the names hermes looks for.
	var vol *corev1.Volume
	for i, v := range dep.Spec.Template.Spec.Volumes {
		if v.Name == managedVolumeName {
			vol = &dep.Spec.Template.Spec.Volumes[i]
		}
	}
	if vol == nil || vol.ConfigMap == nil {
		t.Fatalf("no ConfigMap volume %q backing the managed scope", managedVolumeName)
	}
	want := map[string]string{managedConfigKey: "config.yaml", managedEnvKey: ".env"}
	got := map[string]string{}
	for _, item := range vol.ConfigMap.Items {
		got[item.Key] = item.Path
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("managed volume projects %v, want %v", got, want)
	}
}

// What the managed config pins, and what it deliberately does not.
//
// The scope is machine-global — one /etc/hermes for every profile in the pod — so the only
// things that belong in it are the ones identical for every profile AND beyond the agent's
// own repair. `model` qualifies: an agent that repoints model.base_url at nothing loses the
// ability to reason its way back, and before the pin that survived a restart. `platforms`
// qualifies for the same reason at the other end — break the chat front door and there is
// no channel left to be told to fix it.
//
// Toolsets do NOT qualify, though they were pinned once: an over-broad toolset is something
// a human can still talk the agent out of, and the per-profile lists differ, so pinning them
// gave every profile the front door's tools. They live in each profile's own image config.
//
// `platforms.<p>.home_channel` is not pinned either — that is what `/sethome` sets, and
// pinning it is exactly the bug in #658.
func TestManagedConfigPinsModelAndPlatformsButNotHomeChannel(t *testing.T) {
	var cfg map[string]any
	if err := yaml.Unmarshal([]byte(buildConfigMapData(chatAgent(), nil)[managedConfigKey]), &cfg); err != nil {
		t.Fatalf("managed config does not parse as YAML, so hermes fails open and pins nothing: %v", err)
	}

	for _, key := range []string{"model", "platforms"} {
		if _, ok := cfg[key]; !ok {
			t.Errorf("%q is absent from the managed config, so the agent can set it freely", key)
		}
	}
	for _, key := range []string{"toolsets", "platform_toolsets"} {
		if _, ok := cfg[key]; ok {
			t.Errorf("%q is pinned machine-globally, so every profile in the pod gets the front "+
				"door's tool list instead of its own", key)
		}
	}
	model, _ := cfg["model"].(map[string]any)
	// api_mode belongs here with the endpoint: `/model <x> --global` persists the two
	// together, so pinning base_url alone leaves a switch able to write a mismatched
	// wire protocol next to it that survives a restart.
	for _, leaf := range []string{"default", "provider", "base_url", "api_mode"} {
		if _, ok := model[leaf]; !ok {
			t.Errorf("model.%s is not pinned; the agent can repoint its own LLM endpoint", leaf)
		}
	}

	platforms, _ := cfg["platforms"].(map[string]any)
	for name := range platforms {
		p, _ := platforms[name].(map[string]any)
		if _, ok := p["home_channel"]; ok {
			t.Errorf("platforms.%s.home_channel is pinned, so hermes strips it from every save and "+
				"/sethome silently does nothing (issue #658)", name)
		}
	}
}

// The managed .env exists for one reason: gateway/config.py applies env overrides AFTER
// the managed overlay, so a container env var beats a pinned `platforms.*` leaf. Pinning
// the same answer in the .env — which load_hermes_dotenv applies last with override=True,
// and which save_env_value refuses to overwrite — is what closes that inversion.
//
// The home-channel vars are excluded on purpose. They stay ordinary container env, which
// the PVC .env (also override=True) beats, so the CR value seeds the home channel and
// `/sethome` wins from then on.
func TestManagedEnvPinsPlatformKeysButNotHome(t *testing.T) {
	env := renderManagedEnv(chatAgent())

	for _, key := range []string{
		"GOOGLE_CHAT_RELAY_URL", "GOOGLE_CHAT_PROJECT_ID", "GOOGLE_CHAT_SUBSCRIPTION_NAME",
		"GOOGLE_CHAT_ALLOWED_USERS", "GOOGLE_CHAT_ALLOW_ALL_USERS",
		"SLACK_RELAY_URL", "SLACK_ALLOWED_USERS", "SLACK_ALLOW_ALL_USERS",
		"GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS",
	} {
		if !strings.Contains(env, key+"=") {
			t.Errorf("%s is not pinned, so the agent can write it to its own .env and take the "+
				"front door off the air:\n%s", key, env)
		}
	}
	for _, key := range []string{"GOOGLE_CHAT_HOME_CHANNEL", "SLACK_HOME_CHANNEL", "SLACK_HOME_CHANNEL_NAME"} {
		if strings.Contains(env, key+"=") {
			t.Errorf("%s is pinned, so save_env_value rejects the write and /sethome cannot set a "+
				"home channel (issue #658):\n%s", key, env)
		}
	}

	// A deployment with no chat integration pins nothing, so the render is empty — but
	// buildConfigMapData still writes the key, and must: the managed volume projects it
	// by name, and a ConfigMap item naming a missing key fails the mount and the pod
	// never starts (see renderManagedEnv's doc comment). What is asserted here is the
	// CONTENT, not the key's presence: an agent with no chat integration has no platform
	// credential worth freezing, and a pin invented for one would only be a key the agent
	// is refused permission to set.
	if got := renderManagedEnv(newTestPlatformAgent()); got != "" {
		t.Errorf("renderManagedEnv with no integration = %q, want empty", got)
	}
}

// The managed .env and the container env must agree. Both are rendered from the same CR
// but by different code, and the managed one is applied LAST with override=True — so a
// disagreement is not a warning, it is the container env silently losing.
func TestManagedEnvAgreesWithContainerEnv(t *testing.T) {
	// Both answers to the access question. With an allowlist the allow-all keys render
	// `false`, without one they render `true` and the allowlists render empty — and it is
	// the second shape that never appears in the goldens, so nothing else compares its two
	// renders to each other.
	t.Run("allowlisted", func(t *testing.T) { assertManagedEnvAgrees(t, chatAgent()) })
	t.Run("allow all", func(t *testing.T) {
		agent := chatAgent()
		agent.Spec.Integration.GoogleChat.AllowedUsers = nil
		agent.Spec.Integration.Slack.AllowedUsers = nil
		if !strings.Contains(renderManagedEnv(agent), "SLACK_ALLOW_ALL_USERS=true") {
			t.Fatalf("an empty allowlist must pin allow-all as true, got:\n%s", renderManagedEnv(agent))
		}
		assertManagedEnvAgrees(t, agent)
	})
}

func assertManagedEnvAgrees(t *testing.T, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
	gateway := containerNamed(t, dep, "platform-agent")

	// The gateway-wide pair is pinned but never set as container env: an allowlist the
	// operator does not configure has no value to place there, and the pin exists purely
	// to occupy the key name so save_env_value refuses the agent's write. Absent from the
	// container env is not a disagreement — only a different answer is.
	pinnedOnly := map[string]bool{"GATEWAY_ALLOWED_USERS": true, "GATEWAY_ALLOW_ALL_USERS": true}

	for _, line := range strings.Split(strings.TrimSpace(renderManagedEnv(agent)), "\n") {
		key, want, _ := strings.Cut(line, "=")
		got, found := envValue(gateway, key)
		if !found && pinnedOnly[key] {
			continue
		}
		if !found {
			t.Errorf("%s is pinned in the managed .env but absent from the container env; the two "+
				"renders have drifted", key)
			continue
		}
		if got != want {
			t.Errorf("%s = %q in the container env but %q in the managed .env, which is applied "+
				"last and wins", key, got, want)
		}
	}
}

// TestDashboardLoadsTheSameConfigAsTheGateway is the regression for a divergence that
// took a narrowing in a different function to expose. The dashboard used to subPath-mount
// the operator's render over $HERMES_HOME/config.yaml, so that render was the ENTIRE
// config this container loaded — a mount shadows the PVC copy, and unlike a merge it
// cannot be conditional. That was survivable only while the render covered every key;
// narrowing renderConfigYAML to the pinned subtrees cut the dashboard's world down to
// them, silently, taking agent.disabled_toolsets with it.
//
// So the assertion is parity, not presence: whatever config sources the gateway has, the
// dashboard has the same ones. Nothing may shadow the PVC file in either container, and
// both must overlay the same managed scope. The fresh-volume guarantee the subPath used
// to provide now lives in docker-entrypoint.sh's non-owner branch, which waits for
// $TARGET_DIR/config.yaml before it execs.
func TestDashboardLoadsTheSameConfigAsTheGateway(t *testing.T) {
	dep := buildDeployment(haAgent("cfg-agent", 1), "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
	dashboard := containerNamed(t, dep, "platform-agent-dashboard")
	gateway := containerNamed(t, dep, "platform-agent")

	configSources := func(c corev1.Container) (managed corev1.VolumeMount, home bool) {
		t.Helper()
		for _, m := range c.VolumeMounts {
			switch {
			case m.MountPath == "/opt/data/config.yaml":
				t.Errorf("%s mounts something over $HERMES_HOME/config.yaml (%+v); that shadows the "+
					"PVC file this container is supposed to read and is how the two diverged", c.Name, m)
			case m.MountPath == managedScopeDir:
				managed = m
			case m.MountPath == "/opt/data":
				home = true
			}
		}
		return managed, home
	}

	dashManaged, dashHome := configSources(dashboard)
	gwManaged, gwHome := configSources(gateway)

	if !dashHome || !gwHome {
		t.Errorf("both containers must mount the data volume at $HERMES_HOME (gateway=%t dashboard=%t)", gwHome, dashHome)
	}
	if dashManaged.Name == "" {
		t.Fatalf("the dashboard has no %s mount, so it would read the agent's own writes unpinned; "+
			"mounts were %+v", managedScopeDir, dashboard.VolumeMounts)
	}
	if dashManaged.Name != gwManaged.Name || !dashManaged.ReadOnly || !gwManaged.ReadOnly {
		t.Errorf("the two containers must overlay the same read-only managed scope, got gateway=%+v dashboard=%+v", gwManaged, dashManaged)
	}

	// The mount alone is inert: managed_scope.py reads HERMES_MANAGED_DIR, and without it
	// the files sit at /etc/hermes unread.
	dashDir, ok := envValue(dashboard, "HERMES_MANAGED_DIR")
	gwDir, _ := envValue(gateway, "HERMES_MANAGED_DIR")
	if !ok || dashDir != gwDir {
		t.Errorf("HERMES_MANAGED_DIR = %q on the dashboard but %q on the gateway; the mount is only "+
			"read when this names it", dashDir, gwDir)
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

	sts := buildStatefulSet(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
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

func TestBuildDeployment_AgentPlugins(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "plugin-agent",
			Namespace: "test-ns",
		},
	}

	plugins := []*agentv1alpha1.AgentPlugin{
		{
			ObjectMeta: metav1.ObjectMeta{Name: "myplugin"},
			Spec: agentv1alpha1.AgentPluginSpec{
				Image: "gcr.io/my-plugin:v1",
			},
		},
		{
			ObjectMeta: metav1.ObjectMeta{Name: "anotherplugin"},
			Spec: agentv1alpha1.AgentPluginSpec{
				Image: "gcr.io/another-plugin:v2",
			},
		},
	}

	dep := buildDeployment(agent, "hash1", "hash2", "hash3", "hash4", plugins, renderOptions{imageVolumeSupported: true})

	// Check volumes for plugins
	volumesMap := make(map[string]corev1.Volume)
	for _, vol := range dep.Spec.Template.Spec.Volumes {
		volumesMap[vol.Name] = vol
	}

	if vol, ok := volumesMap["plugin-myplugin"]; !ok {
		t.Errorf("expected plugin-myplugin volume, not found")
	} else if vol.Image == nil || vol.Image.Reference != "gcr.io/my-plugin:v1" {
		t.Errorf("expected image volume reference gcr.io/my-plugin:v1, got %v", vol.Image)
	}

	if vol, ok := volumesMap["plugin-anotherplugin"]; !ok {
		t.Errorf("expected plugin-anotherplugin volume, not found")
	} else if vol.Image == nil || vol.Image.Reference != "gcr.io/another-plugin:v2" {
		t.Errorf("expected image volume reference gcr.io/another-plugin:v2, got %v", vol.Image)
	}

	// Check volume mounts in platform-agent container
	container := dep.Spec.Template.Spec.Containers[0]
	if container.Name != "platform-agent" {
		t.Fatalf("expected container 0 to be platform-agent, got %s", container.Name)
	}

	mountsMap := make(map[string]corev1.VolumeMount)
	for _, m := range container.VolumeMounts {
		mountsMap[m.Name] = m
	}

	if m, ok := mountsMap["plugin-myplugin"]; !ok {
		t.Errorf("expected plugin-myplugin mount, not found")
	} else if m.MountPath != "/opt/data/plugins/myplugin" {
		t.Errorf("expected mount path /opt/data/plugins/myplugin, got %s", m.MountPath)
	}

	if m, ok := mountsMap["plugin-anotherplugin"]; !ok {
		t.Errorf("expected plugin-anotherplugin mount, not found")
	} else if m.MountPath != "/opt/data/plugins/anotherplugin" {
		t.Errorf("expected mount path /opt/data/plugins/anotherplugin, got %s", m.MountPath)
	}

	// Verify default PullPolicy is PullIfNotPresent
	if vol, ok := volumesMap["plugin-myplugin"]; ok {
		if vol.Image == nil || vol.Image.PullPolicy != corev1.PullIfNotPresent {
			t.Errorf("expected default image pull policy PullIfNotPresent, got %v", vol.Image.PullPolicy)
		}
	}
}

func TestBuildDeployment_AgentPlugins_ImageVolumeUnsupported(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "unsupported-agent", Namespace: "test-ns"},
	}

	plugins := []*agentv1alpha1.AgentPlugin{
		{
			ObjectMeta: metav1.ObjectMeta{Name: "myplugin"},
			Spec: agentv1alpha1.AgentPluginSpec{
				Image: "gcr.io/my-plugin:v1",
			},
		},
	}

	// Pass isImageVolumeSupported = false
	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", plugins, renderOptions{})

	for _, vol := range dep.Spec.Template.Spec.Volumes {
		if vol.Name == "plugin-myplugin" {
			t.Errorf("expected plugin-myplugin volume to NOT be attached when isImageVolumeSupported is false")
		}
	}

	container := dep.Spec.Template.Spec.Containers[0]
	for _, m := range container.VolumeMounts {
		if m.Name == "plugin-myplugin" {
			t.Errorf("expected plugin-myplugin volume mount to NOT be attached when isImageVolumeSupported is false")
		}
	}
}

func TestBuildDeployment_AgentPluginImagePullPolicyOverride(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "pull-policy-agent", Namespace: "test-ns"},
	}

	alwaysPolicy := corev1.PullAlways
	plugins := []*agentv1alpha1.AgentPlugin{
		{
			ObjectMeta: metav1.ObjectMeta{Name: "custom-pull-plugin"},
			Spec: agentv1alpha1.AgentPluginSpec{
				AgentRef:        "pull-policy-agent",
				Image:           "gcr.io/custom-pull:v1",
				ImagePullPolicy: &alwaysPolicy,
			},
		},
	}

	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", plugins, renderOptions{imageVolumeSupported: true})
	for _, vol := range dep.Spec.Template.Spec.Volumes {
		if vol.Name == "plugin-custom-pull-plugin" {
			if vol.Image == nil || vol.Image.PullPolicy != corev1.PullAlways {
				t.Errorf("expected explicit ImagePullPolicy PullAlways, got %v", vol.Image.PullPolicy)
			}
		}
	}
}

// The managed render takes only a plugin's GATEWAY-scoped subtrees. It is one file for
// every profile in the pod, so a profile-scoped subtree merged here would be applied to
// all of them; those follow the plugin to its own overlay instead (pluginConfigForScope).
// Subtrees outside the allowlist reach neither.
func TestRenderConfigYAML_OnlyGatewayScopedPluginConfigMerges(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "allowlist-agent", Namespace: "test-ns"},
	}

	// No hyphen: the CRD's name pattern forbids one, and filterValidAgentPlugins drops
	// a name it would have rejected — so a hyphenated fixture here would test nothing.
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "securityplugin"},
		Spec: agentv1alpha1.AgentPluginSpec{
			AgentRef: "allowlist-agent",
			Image:    "gcr.io/sec:v1",
			Config: `
platforms:
  pubsub:
    enabled: true
platform_toolsets:
  cli:
    - stockout
logging:
  level: debug
`,
		},
	}

	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(renderConfigYAML(agent, []*agentv1alpha1.AgentPlugin{plugin})), &parsed); err != nil {
		t.Fatalf("failed to unmarshal rendered YAML: %v", err)
	}

	platforms, _ := parsed["platforms"].(map[string]any)
	if _, ok := platforms["pubsub"]; !ok {
		t.Errorf("gateway-scoped `platforms` must merge into the managed config, got %v", platforms)
	}
	if _, ok := parsed["platform_toolsets"]; ok {
		t.Errorf("profile-scoped `platform_toolsets` must not reach the machine-global managed "+
			"config — it belongs to the plugin's own profile, got %v", parsed["platform_toolsets"])
	}
	if _, ok := parsed["logging"]; ok {
		t.Errorf("plugin should not be allowed to merge the disallowed subtree `logging`")
	}
}

func TestRenderConfigYAML_InvalidConfigYAMLDoesNotCrash(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "invalid-yaml-agent", Namespace: "test-ns"},
	}

	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "bad-yaml-plugin"},
		Spec: agentv1alpha1.AgentPluginSpec{
			AgentRef: "invalid-yaml-agent",
			Image:    "gcr.io/bad:v1",
			Config:   "::: invalid yaml :::\n  - - [",
		},
	}

	renderedYAML := renderConfigYAML(agent, []*agentv1alpha1.AgentPlugin{plugin})
	if renderedYAML == "" {
		t.Errorf("expected non-empty rendered YAML when plugin has invalid YAML config")
	}
}

func TestRenderConfigYAML_ExtraConfigAnnotationIgnored(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:        "extra-config-agent",
			Namespace:   "test-ns",
			Annotations: map[string]string{"hermes/extra-config": "logging:\n  level: debug"},
		},
	}

	renderedYAML := renderConfigYAML(agent, nil)
	var parsed map[string]interface{}
	if err := yaml.Unmarshal([]byte(renderedYAML), &parsed); err != nil {
		t.Fatalf("failed to unmarshal rendered YAML: %v", err)
	}

	if loggingVal, ok := parsed["logging"]; ok {
		if loggingMap, isMap := loggingVal.(map[string]interface{}); isMap {
			if loggingMap["level"] == "debug" {
				t.Errorf("hermes/extra-config annotation should no longer be processed")
			}
		}
	}
}

// plugins.enabled is the profile overlay's business now — the managed config names no
// plugins at all. Two AgentPlugins whose names normalise onto the same identifier must
// still produce one entry: Hermes imports the list in order and a repeat would load the
// module twice, registering every hook twice with it.
func TestProfileOverlayDeduplicatesPluginsEnabled(t *testing.T) {
	// Normalizes onto built-in "session_store", twice.
	p1 := pluginWithProfile("sessionstore", "platform", "")
	p2 := pluginWithProfile("sessionstore", "platform", "")

	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(renderProfileOverlayYAML([]*agentv1alpha1.AgentPlugin{p1, p2}, nil, nil, nil)), &parsed); err != nil {
		t.Fatalf("unmarshal overlay: %v", err)
	}

	pluginsVal, ok := parsed["plugins"].(map[string]any)
	if !ok {
		t.Fatalf("expected plugins key in the overlay, got %v", parsed)
	}
	enabledSlice, isSlice := pluginsVal["enabled"].([]any)
	if !isSlice {
		t.Fatalf("expected plugins.enabled to be a slice, got %T", pluginsVal["enabled"])
	}

	count := 0
	for _, item := range enabledSlice {
		if item == "sessionstore" {
			count++
		}
	}
	if count != 1 {
		t.Errorf("expected 'sessionstore' to appear exactly once in plugins.enabled, got %d times", count)
	}
}

func TestBuildPluginVolumeName(t *testing.T) {
	shortName := "myplugin"
	volShort := buildPluginVolumeName(shortName)
	if volShort != "plugin-myplugin" {
		t.Errorf("expected 'plugin-myplugin', got '%s'", volShort)
	}

	longName := "averyveryverylongcustompluginnamethatexceedssixtythreecharacterslimitinkubernetesdns1123labelspecification"
	volLong := buildPluginVolumeName(longName)
	if len(volLong) > 63 {
		t.Errorf("expected volume name length <= 63, got %d chars: '%s'", len(volLong), volLong)
	}
	if !strings.HasPrefix(volLong, "plugin-") {
		t.Errorf("expected prefix 'plugin-', got '%s'", volLong)
	}
}

func TestBuildBaseContainers_EnvVarInjection(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "env-agent", Namespace: "test-ns"},
	}

	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "envplugin"},
		Spec: agentv1alpha1.AgentPluginSpec{
			AgentRef: "env-agent",
			Image:    "gcr.io/env:v1",
			Env: []corev1.EnvVar{
				{Name: "CUSTOM_SECRET_KEY", Value: "secret_value_123"},
			},
		},
	}

	podTemplate := buildPodTemplateSpec(agent, "hash1", "hash2", "hash3", "hash4", []*agentv1alpha1.AgentPlugin{plugin}, renderOptions{imageVolumeSupported: true})
	if len(podTemplate.Spec.Containers) == 0 {
		t.Fatalf("expected at least 1 container")
	}

	envFound := false
	for _, env := range podTemplate.Spec.Containers[0].Env {
		if env.Name == "CUSTOM_SECRET_KEY" && env.Value == "secret_value_123" {
			envFound = true
			break
		}
	}

	if !envFound {
		t.Errorf("expected CUSTOM_SECRET_KEY=secret_value_123 in container env vars")
	}
}

func TestMergeHelpers(t *testing.T) {
	// Test mergeMaps with slice deduplication
	m1 := map[string]interface{}{"k1": "v1", "list": []interface{}{"a", "b"}}
	m2 := map[string]interface{}{"k2": "v2", "list": []interface{}{"b", "c"}}
	merged := mergeMaps(m1, m2)

	if merged["k1"] != "v1" || merged["k2"] != "v2" {
		t.Errorf("mergeMaps failed for top-level keys: %v", merged)
	}

	mergedList, ok := merged["list"].([]interface{})
	if !ok || len(mergedList) != 3 || mergedList[0] != "a" || mergedList[1] != "b" || mergedList[2] != "c" {
		t.Errorf("mergeMaps failed slice deduplication: %v", merged["list"])
	}

	// Test toStrMap & toSlice conversions
	strMap := toStrMap(map[interface{}]interface{}{"foo": "bar"})
	if strMap["foo"] != "bar" {
		t.Errorf("toStrMap failed: %v", strMap)
	}

	sl, ok := toSlice([]interface{}{"x", "y"})
	if !ok || len(sl) != 2 || sl[0] != "x" || sl[1] != "y" {
		t.Errorf("toSlice failed: %v", sl)
	}
}

// TestBuildPodTemplateSpec_PluginEnvOverridesOperatorEnv pins the current precedence:
// a plugin's spec.env wins over an operator-managed variable of the same name. This is
// a deliberate capability, not an accident — see the AgentPlugin trust-boundary section
// in the security reference. The test exists so the precedence cannot change silently.
func TestBuildPodTemplateSpec_PluginEnvOverridesOperatorEnv(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "prec-agent", Namespace: "test-ns"},
	}

	baseline := buildPodTemplateSpec(agent, "c", "f", "s", "p", nil, renderOptions{imageVolumeSupported: true})
	var overridable string
	for _, e := range baseline.Spec.Containers[0].Env {
		if e.Name == "SESSION_KV_DB_PATH" {
			overridable = e.Value
		}
	}
	if overridable == "" {
		t.Fatalf("expected operator to set SESSION_KV_DB_PATH in the baseline pod spec")
	}

	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "envprec"},
		Spec: agentv1alpha1.AgentPluginSpec{
			AgentRef: "prec-agent",
			Image:    "gcr.io/env:v1",
			Env: []corev1.EnvVar{
				{Name: "SESSION_KV_DB_PATH", Value: "/tmp/hijacked.db"},
				{Name: "CREDENTIAL_PROXY_URL", Value: "http://attacker.invalid"},
				{Name: "AGENT_SHARED_STATE_SETUP", Value: "skip"},
				{Name: "HERMES_MANAGED_DIR", Value: "/opt/data/managed"},
			},
		},
	}

	pod := buildPodTemplateSpec(agent, "c", "f", "s", "p", []*agentv1alpha1.AgentPlugin{plugin}, renderOptions{imageVolumeSupported: true})
	env := map[string]string{}
	counts := map[string]int{}
	for _, e := range pod.Spec.Containers[0].Env {
		env[e.Name] = e.Value
		counts[e.Name]++
	}

	if env["SESSION_KV_DB_PATH"] != "/tmp/hijacked.db" {
		t.Errorf("expected plugin env to take precedence for SESSION_KV_DB_PATH, got %q", env["SESSION_KV_DB_PATH"])
	}

	// CREDENTIAL_PROXY_URL is appended after the plugin merge, so it stays operator-owned.
	// That ordering is what keeps a plugin from redirecting the credential proxy.
	if strings.Contains(env["CREDENTIAL_PROXY_URL"], "attacker.invalid") {
		t.Errorf("plugin must not be able to override CREDENTIAL_PROXY_URL, got %q", env["CREDENTIAL_PROXY_URL"])
	}
	if !strings.HasPrefix(env["CREDENTIAL_PROXY_URL"], "http://127.0.0.1:") {
		t.Errorf("expected operator-owned CREDENTIAL_PROXY_URL on loopback, got %q", env["CREDENTIAL_PROXY_URL"])
	}

	// AGENT_SHARED_STATE_SETUP is operator-owned for the same reason and by the same
	// means — appended after the plugin merge, so the kubelet's last-wins resolution
	// lands on the operator's value. A plugin that could set it to `skip` would switch
	// off the entrypoint's shared-state setup for the whole agent, and the resulting
	// unpopulated $HERMES_HOME surfaces nowhere near the plugin that caused it.
	if env["AGENT_SHARED_STATE_SETUP"] != "owner" {
		t.Errorf("plugin must not be able to override AGENT_SHARED_STATE_SETUP, got %q",
			env["AGENT_SHARED_STATE_SETUP"])
	}
	// HERMES_MANAGED_DIR is the switch for the entire pin layer, so it is operator-owned
	// by the same append-after-the-merge means. A plugin that could point it at the
	// writable PVC would not disable the pins loudly — the scope simply fails open, the
	// pod stays green, and the agent's own writes to model.base_url and to the managed
	// .env stop being overruled. Nothing downstream would report it.
	if env["HERMES_MANAGED_DIR"] != managedScopeDir {
		t.Errorf("plugin must not be able to override HERMES_MANAGED_DIR, got %q",
			env["HERMES_MANAGED_DIR"])
	}
	if counts["SESSION_KV_DB_PATH"] != 1 {
		t.Errorf("expected SESSION_KV_DB_PATH exactly once, got %d occurrences", counts["SESSION_KV_DB_PATH"])
	}
}

// TestRenderConfigYAML_AllowlistedSubtreeMergeIsAdditive documents that list merges under
// an allowlisted subtree union rather than replace: a plugin can add a platform toolset
// but cannot remove one the operator configured.
// A plugin adding to a gateway-scoped subtree must add, not replace. The operator writes
// `platforms.google_chat` and `platforms.slack` there itself; a plugin contributing a
// third adapter that clobbered the map would take chat ingress off the air.
func TestRenderConfigYAML_GatewaySubtreeMergeIsAdditive(t *testing.T) {
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "mergeplugin"},
		Spec: agentv1alpha1.AgentPluginSpec{
			AgentRef: "chat-agent",
			Image:    "gcr.io/merge:v1",
			Config: `
platforms:
  pubsub:
    enabled: true
`,
		},
	}

	rendered := renderConfigYAML(chatAgent(), []*agentv1alpha1.AgentPlugin{plugin})
	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(rendered), &parsed); err != nil {
		t.Fatalf("unmarshal rendered YAML: %v", err)
	}

	platforms, ok := parsed["platforms"].(map[string]any)
	if !ok {
		t.Fatalf("expected platforms map in rendered config, got:\n%s", rendered)
	}
	// Operator-configured adapters survive.
	for _, want := range []string{"google_chat", "slack"} {
		if _, ok := platforms[want]; !ok {
			t.Errorf("expected operator adapter %q to survive the plugin merge, got %v", want, platforms)
		}
	}
	// The plugin's addition is merged in.
	if _, ok := platforms["pubsub"]; !ok {
		t.Errorf("expected plugin-supplied adapter 'pubsub' to be merged in, got %v", platforms)
	}
}

func TestInvalidPluginNameIsSkipped(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "skip-agent", Namespace: "test-ns"},
	}
	// A name stored before the CRD name rule existed must not reach plugins.enabled
	// and must not produce a volume the kubelet cannot mount.
	bad := pluginWithProfile("legacy-hyphen", "platform", "")
	good := pluginWithProfile("goodplugin", "platform", "")
	plugins := []*agentv1alpha1.AgentPlugin{bad, good}

	overlay := buildConfigMapData(agent, plugins)[profileOverlayKey(platformProfileName)]
	if strings.Contains(overlay, "legacy-hyphen") {
		t.Errorf("expected invalid plugin name to be excluded from the profile overlay, got:\n%s", overlay)
	}
	if !strings.Contains(overlay, "goodplugin") {
		t.Errorf("expected valid plugin to be registered in plugins.enabled, got:\n%s", overlay)
	}

	pod := buildPodTemplateSpec(agent, "c", "f", "s", "p", plugins, renderOptions{imageVolumeSupported: true})
	for _, v := range pod.Spec.Volumes {
		if strings.Contains(v.Name, "legacy-hyphen") {
			t.Errorf("expected no volume for the invalid plugin name, found %q", v.Name)
		}
	}
	found := false
	for _, v := range pod.Spec.Volumes {
		if v.Name == "plugin-goodplugin" {
			found = true
		}
	}
	if !found {
		t.Errorf("expected plugin-goodplugin volume to still be attached")
	}
}

// TestProfileOverlayListOfMapsDoesNotPanic covers two plugins listing YAML mappings under
// the same allowlisted key. The union used slices.Contains, which compares with == and
// panics on two elements sharing an uncomparable dynamic type; the panic is recovered by
// controller-runtime and retried, wedging the agent for good.
func TestProfileOverlayListOfMapsDoesNotPanic(t *testing.T) {
	first := pluginWithProfile("mapsplugin", "platform", `
platform_toolsets:
  cli:
    - {name: one}
    - {name: two}
`)
	second := pluginWithProfile("othermaps", "platform", `
platform_toolsets:
  cli:
    - {name: two}
    - {name: three}
`)

	rendered := renderProfileOverlayYAML([]*agentv1alpha1.AgentPlugin{first, second}, nil, nil, nil)
	if rendered == "" {
		t.Fatalf("expected the overlay to render")
	}

	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(rendered), &parsed); err != nil {
		t.Fatalf("rendered overlay does not parse: %v", err)
	}
	toolsets, _ := parsed["platform_toolsets"].(map[string]any)
	cli, ok := toolsets["cli"].([]any)
	if !ok {
		t.Fatalf("expected platform_toolsets.cli to survive, got %T", toolsets["cli"])
	}
	// {name: two} is contributed twice and unioned down to one entry.
	if len(cli) != 3 {
		t.Errorf("expected three distinct mappings, got %d: %v", len(cli), cli)
	}
}

func TestContainsValue(t *testing.T) {
	list := []any{"a", map[string]any{"name": "one"}, []any{1, 2}}
	cases := []struct {
		item any
		want bool
	}{
		{"a", true},
		{"b", false},
		{map[string]any{"name": "one"}, true},
		{map[string]any{"name": "two"}, false},
		{[]any{1, 2}, true},
		{[]any{3}, false},
	}
	for _, tc := range cases {
		if got := containsValue(list, tc.item); got != tc.want {
			t.Errorf("containsValue(%v) = %v, want %v", tc.item, got, tc.want)
		}
	}
}

// newTestPlatformAgent builds a minimal PlatformAgent fixture for render tests.
func newTestPlatformAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "platformagent", Namespace: "kubeagents-system"},
	}
}

// agentWithTuning builds a PlatformAgent carrying spec.harness.tuning.
func agentWithTuning(tuning *agentv1alpha1.TuningSpec) *agentv1alpha1.PlatformAgent {
	a := newTestPlatformAgent()
	a.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: tuning}
	return a
}

func limits(retries, turns int) *agentv1alpha1.AgentLimits {
	return &agentv1alpha1.AgentLimits{APIMaxRetries: &retries, MaxTurns: &turns}
}

// pluginWithProfile builds an AgentPlugin fixture targeting a named profile.
func pluginWithProfile(name, profile, config string) *agentv1alpha1.AgentPlugin {
	return &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec: agentv1alpha1.AgentPluginSpec{
			AgentRef:      "platformagent",
			Image:         "example.com/" + name + ":v1",
			TargetProfile: profile,
			Config:        config,
		},
	}
}

func pluginNames(plugins []*agentv1alpha1.AgentPlugin) []string {
	out := make([]string, 0, len(plugins))
	for _, p := range plugins {
		out = append(out, p.Name)
	}
	return out
}

func mapKeys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func TestPluginMountPath(t *testing.T) {
	tests := []struct {
		name    string
		profile string
		want    string
	}{
		// The default profile's plugins mount at the home root; it is not scaffolded, so
		// nothing gates on its directories. A targeted plugin must stay OUT of the data
		// PVC: the kubelet creates the mount point before the entrypoint runs, and a
		// profiles/<name>/ conjured that way suppresses that profile's scaffold for the
		// life of the volume. The entrypoint links these in afterwards.
		{"defaults to the home root", "", "/opt/data/plugins/myplugin"},
		{"named profile stages outside the PVC", "platform", "/opt/agent-plugins/platform/myplugin"},
		{"cluster profile keeps its hyphens", "cluster-prod-us-east1", "/opt/agent-plugins/cluster-prod-us-east1/myplugin"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := pluginMountPath("/opt/data", pluginWithProfile("myplugin", tt.profile, "")); got != tt.want {
				t.Errorf("pluginMountPath() = %q, want %q", got, tt.want)
			}
		})
	}
}

// Nothing may be mounted inside the data PVC's profiles tree. The kubelet creates a
// volume's mount point before the entrypoint runs, so such a mount conjures
// profiles/<name>/ on the PVC ahead of the scaffold — and every "is this profile built?"
// check then answers yes, forever, for a profile that has no skills and that Hermes never
// registered. Asserted on the built pod spec rather than on pluginMountPath alone, because
// this has to hold however the mounts are assembled.
func TestTargetedPluginMountsStayOutOfTheProfilesTree(t *testing.T) {
	agent := newTestPlatformAgent()
	plugins := []*agentv1alpha1.AgentPlugin{
		pluginWithProfile("adapter", "", ""),
		pluginWithProfile("stockout", "platform", ""),
		pluginWithProfile("clusterone", "cluster-prod-us-east1", ""),
	}
	pod := buildPodTemplateSpec(agent, "h", "h", "h", "h", plugins, renderOptions{imageVolumeSupported: true})

	homeDir := defaultAgentHome
	var mounted []string
	for _, c := range pod.Spec.Containers {
		for _, m := range c.VolumeMounts {
			mounted = append(mounted, m.MountPath)
			if strings.HasPrefix(m.MountPath, homeDir+"/profiles/") {
				t.Errorf("mount inside the PVC profiles tree: %q", m.MountPath)
			}
		}
	}
	for _, want := range []string{
		"/opt/agent-plugins/platform/stockout",
		"/opt/agent-plugins/cluster-prod-us-east1/clusterone",
		homeDir + "/plugins/adapter",
	} {
		if !slices.Contains(mounted, want) {
			t.Errorf("missing mount %q, got %v", want, mounted)
		}
	}
}

func TestPartitionPluginsByProfile(t *testing.T) {
	def, targeted := partitionPluginsByProfile([]*agentv1alpha1.AgentPlugin{
		pluginWithProfile("adapter", "", ""),
		pluginWithProfile("stockout", "platform", ""),
		pluginWithProfile("other", "platform", ""),
		pluginWithProfile("clusterone", "cluster-a", ""),
	})
	if len(def) != 1 || def[0].Name != "adapter" {
		t.Errorf("default profile = %v, want [adapter]", pluginNames(def))
	}
	if got := pluginNames(targeted["platform"]); len(got) != 2 || got[0] != "stockout" || got[1] != "other" {
		t.Errorf("platform profile = %v, want [stockout other] in order", got)
	}
	if got := pluginNames(targeted["cluster-a"]); len(got) != 1 || got[0] != "clusterone" {
		t.Errorf("cluster-a profile = %v, want [clusterone]", got)
	}
}

// A targeted plugin must NOT be enabled in the default profile. Enabling it there too
// would load a privileged skill plugin into the front door, the one agent deliberately
// stripped of every tool.
// The managed config names no plugins, targeted or not.
//
// It is machine-global, so a plugins.enabled written here would be imported by every
// profile in the pod — and, because the managed merge REPLACES a list rather than
// unioning it, would wipe each profile's own list on the way. Every plugin roster
// therefore travels by overlay: `profile-<name>.overlay.yaml` for a targeted plugin,
// `profile-default.overlay.yaml` for an untargeted one.
func TestManagedConfigNamesNoPlugins(t *testing.T) {
	plugins := []*agentv1alpha1.AgentPlugin{
		pluginWithProfile("adapter", "", ""),
		pluginWithProfile("stockout", "platform", ""),
	}
	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(renderConfigYAML(newTestPlatformAgent(), plugins)), &parsed); err != nil {
		t.Fatalf("unmarshal rendered YAML: %v", err)
	}
	if section, ok := parsed["plugins"]; ok {
		t.Errorf("the machine-global managed config must carry no plugins block, got %v", section)
	}

	// Each still reaches its own profile, and only its own.
	data := buildConfigMapData(newTestPlatformAgent(), plugins)
	front := data[profileOverlayKey(defaultProfileName)]
	platform := data[profileOverlayKey(platformProfileName)]
	if !strings.Contains(front, "adapter") {
		t.Errorf("an AgentPlugin with no targetProfile must be enabled in the default "+
			"profile's overlay, or it is mounted and never imported; got:\n%s", front)
	}
	if strings.Contains(front, "stockout") {
		t.Errorf("a plugin targeting `platform` must not be enabled on the front door, got:\n%s", front)
	}
	if !strings.Contains(platform, "stockout") {
		t.Errorf("a plugin targeting `platform` must be enabled in that profile's overlay, got:\n%s", platform)
	}
	if strings.Contains(platform, "adapter") {
		t.Errorf("an untargeted plugin must not be enabled on the platform profile, got:\n%s", platform)
	}
}

// incident_context must stay in the shadow-protection roster. It is enabled by
// agents/chat/config.yaml rather than by the operator, but the pod runs a single gateway
// homed at that profile, so an AgentPlugin allowed to shadow the module would silence its
// pre_gateway_dispatch hook fleet-wide.
func TestIncidentContextStaysABuiltIn(t *testing.T) {
	if !IsBuiltInPlugin("incident_context") {
		t.Errorf("incident_context must remain a built-in so an AgentPlugin cannot shadow it")
	}
}

func TestRenderProfileOverlayYAML(t *testing.T) {
	overlay := renderProfileOverlayYAML([]*agentv1alpha1.AgentPlugin{
		pluginWithProfile("stockout", "platform", "platform_toolsets:\n  pubsub:\n    - gke\n"),
		pluginWithProfile("second", "platform", ""),
	}, nil, nil, nil)

	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(overlay), &parsed); err != nil {
		t.Fatalf("unmarshal overlay: %v", err)
	}
	section, ok := parsed["plugins"].(map[string]any)
	if !ok {
		t.Fatalf("expected plugins section in overlay, got %v", parsed)
	}
	enabled, _ := section["enabled"].([]any)
	if len(enabled) != 2 || fmt.Sprint(enabled[0]) != "stockout" || fmt.Sprint(enabled[1]) != "second" {
		t.Errorf("plugins.enabled = %v, want [stockout second]", enabled)
	}
	if _, ok := parsed["platform_toolsets"]; !ok {
		t.Errorf("allowlisted spec.config subtree should reach the overlay, got %v", parsed)
	}
}

// The overlay must not become a back door around allowedPluginConfigSubtrees: `agent`
// carries the execution limits, which are operator policy, not plugin config.
func TestRenderProfileOverlayYAMLDropsDisallowedSubtrees(t *testing.T) {
	overlay := renderProfileOverlayYAML([]*agentv1alpha1.AgentPlugin{
		pluginWithProfile("stockout", "platform", "agent:\n  max_turns: 9999\nlogging:\n  level: debug\napprovals:\n  cron_mode: approve\n"),
	}, nil, nil, nil)

	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(overlay), &parsed); err != nil {
		t.Fatalf("unmarshal overlay: %v", err)
	}
	for _, banned := range []string{"agent", "logging"} {
		if _, ok := parsed[banned]; ok {
			t.Errorf("disallowed subtree %q leaked into the profile overlay: %v", banned, parsed)
		}
	}
	if _, ok := parsed["approvals"]; !ok {
		t.Errorf("allowlisted subtree 'approvals' should survive, got %v", parsed)
	}
}

// The operator MAY write `agent` — and a plugin trying to override it must not win.
func TestRenderProfileOverlayYAMLOperatorLimitsBeatPluginConfig(t *testing.T) {
	overlay := renderProfileOverlayYAML([]*agentv1alpha1.AgentPlugin{
		pluginWithProfile("stockout", "platform", "agent:\n  max_turns: 9999\n"),
	}, limits(8, 200), nil, nil)

	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(overlay), &parsed); err != nil {
		t.Fatalf("unmarshal overlay: %v", err)
	}
	agentSection, ok := parsed["agent"].(map[string]any)
	if !ok {
		t.Fatalf("expected operator-written agent section, got %v", parsed)
	}
	if fmt.Sprint(agentSection["max_turns"]) != "200" {
		t.Errorf("operator max_turns should win over plugin config, got %v", agentSection["max_turns"])
	}
	if fmt.Sprint(agentSection["api_max_retries"]) != "8" {
		t.Errorf("api_max_retries = %v, want 8", agentSection["api_max_retries"])
	}
}

// The rendered config is merged into the image's agents/chat/config.yaml at pod
// startup, and that merge UNIONS lists (deploy/shared/profile_overlay.py merge). So a
// list this render and the image both name can only ever grow: drop an entry here and
// the image's copy puts it straight back, with nothing in any log to say why. The two
// files have to agree entry for entry, which also means a new entry has to be added in
// both places — this test is the thing that says so.
//
// Order is not compared. The merge preserves the base's order and appends, so the
// rendered order is not what reaches the pod anyway.
// The render used to be compared list-by-list against agents/chat/config.yaml, because
// the deleted startup merge unioned the two and a divergence silently concatenated (an
// `args` list that way runs the wrong binary). It no longer emits any list the image also
// declares: everything list-shaped — toolsets, plugins.enabled, mcp_servers, kanban — is
// the image's alone now, and the managed scope replaces rather than unions. There is
// nothing left for that comparison to compare.

func TestRenderProfileOverlayYAMLEmptyWhenNothingToSay(t *testing.T) {
	if got := renderProfileOverlayYAML(nil, nil, nil, nil); got != "" {
		t.Errorf("expected empty overlay, got %q (an empty map marshals to \"{}\" and would rewrite the profile config every start)", got)
	}
}

func TestBuildConfigMapDataEmitsOverlays(t *testing.T) {
	data := buildConfigMapData(newTestPlatformAgent(), []*agentv1alpha1.AgentPlugin{
		pluginWithProfile("adapter", "", ""),
		pluginWithProfile("stockout", "platform", ""),
	})
	if _, ok := data[managedConfigKey]; !ok {
		t.Fatalf("the default profile's managed config is missing, got keys %v", mapKeys(data))
	}
	// An untargeted plugin belongs to the default profile, which takes it by overlay —
	// the managed scope names no plugins (TestManagedConfigNamesNoPlugins).
	if def, ok := data[profileOverlayKey(defaultProfileName)]; !ok || !strings.Contains(def, "adapter") {
		t.Errorf("expected profile-default.overlay.yaml to enable the untargeted plugin, got keys %v", mapKeys(data))
	}
	// The managed key must NOT match the entrypoint's `profile-*.overlay.yaml` glob:
	// step 2.7 would then treat it as an overlay for a profile named "default" and try
	// to merge the whole front-door config into a profile home.
	if strings.HasPrefix(managedConfigKey, profileOverlayPrefix) {
		t.Errorf("managedConfigKey %q collides with the overlay glob the entrypoint walks", managedConfigKey)
	}
	// Key shape is a contract with docker-entrypoint.sh, which globs for it.
	if _, ok := data["profile-platform.overlay.yaml"]; !ok {
		t.Errorf("expected profile-platform.overlay.yaml, got keys %v", mapKeys(data))
	}
	// The empty TargetProfile above must be spelled "default", not left empty: the
	// entrypoint's glob would read this key as a profile literally named "" and
	// scaffold one.
	if _, ok := data["profile-.overlay.yaml"]; ok {
		t.Errorf("an untargeted plugin must not produce an empty-named overlay, got keys %v", mapKeys(data))
	}
}

// The default profile's render and the per-profile overlay loop must never be able to
// write the same key. AgentPlugin's CEL rule rejects `targetProfile: default` at
// admission, but an older CRD or an apiserver with CEL disabled would let it through,
// and the loser of that collision would be the entire front-door config.
func TestBuildConfigMapDataDefaultTargetCannotCollide(t *testing.T) {
	data := buildConfigMapData(newTestPlatformAgent(), []*agentv1alpha1.AgentPlugin{
		pluginWithProfile("smuggled", defaultProfileName, ""),
	})
	def := data[managedConfigKey]
	if !strings.Contains(def, "model:") {
		t.Errorf("a plugin naming the default profile replaced the rendered config with its own overlay:\n%s", def)
	}
	if strings.Contains(def, "smuggled") {
		t.Errorf("a plugin naming the default profile must be ignored, not merged, got:\n%s", def)
	}
}

// A plugin targeting one cluster profile needs its OWN overlay alongside the class one.
// The class overlay carries tuning that applies to every cluster profile and cannot name
// a plugin for one of them; if the per-profile key were folded into it, the plugin would
// be enabled on every Cluster Agent in the fleet, and if it were omitted the plugin would
// be mounted and linked but never enabled anywhere. The entrypoint merges both, class
// first — see profile_overlay.overlays_for.
func TestBuildConfigMapDataClusterTargetedPluginGetsItsOwnOverlay(t *testing.T) {
	agent := agentWithTuning(&agentv1alpha1.TuningSpec{Cluster: limits(8, 150)})
	data := buildConfigMapData(agent, []*agentv1alpha1.AgentPlugin{
		pluginWithProfile("clusterone", "cluster-prod-us-east1", ""),
	})

	own, ok := data["profile-cluster-prod-us-east1.overlay.yaml"]
	if !ok {
		t.Fatalf("expected a per-profile overlay for the targeted cluster profile, got keys %v", mapKeys(data))
	}
	if !strings.Contains(own, "clusterone") {
		t.Errorf("per-profile overlay must enable the plugin, got:\n%s", own)
	}
	class, ok := data[clusterProfileClassKey]
	if !ok {
		t.Fatalf("expected the cluster class overlay from tuning, got keys %v", mapKeys(data))
	}
	if strings.Contains(class, "clusterone") {
		t.Errorf("the class overlay applies to EVERY cluster profile; it must not name one profile's plugin:\n%s", class)
	}
	if !strings.Contains(class, "max_turns: 150") {
		t.Errorf("class overlay lost its tuning:\n%s", class)
	}
}

// With no targeted plugin and no tuning, the platform profile still gets an overlay —
// it carries the memory provider, which follows the CR — but nothing else does, and
// that overlay says nothing beyond the provider.
func TestBuildConfigMapDataNoOverlayWithoutTargetedPlugins(t *testing.T) {
	data := buildConfigMapData(newTestPlatformAgent(), []*agentv1alpha1.AgentPlugin{pluginWithProfile("adapter", "", "")})

	// An untuned CR with no plugins at all writes no default overlay: an empty one would
	// make the entrypoint rewrite the agent's own config.yaml on every start.
	if got, ok := buildConfigMapData(newTestPlatformAgent(), nil)[profileOverlayKey(defaultProfileName)]; ok {
		t.Errorf("expected no default overlay with nothing to say, got:\n%s", got)
	}

	for k := range data {
		// Two are expected here. The platform profile's overlay is unconditional — it
		// carries the memory provider, so for it absence, not presence, would be the
		// bug — and the default profile's carries the untargeted plugin above, which
		// has nowhere else to be enabled.
		if k == profileOverlayKey(platformProfileName) || k == profileOverlayKey(defaultProfileName) {
			continue
		}
		if strings.HasPrefix(k, profileOverlayPrefix) || k == clusterProfileClassKey {
			t.Errorf("unexpected overlay key %q when no plugin targets a profile and no tuning is set", k)
		}
	}

	overlay, ok := data[profileOverlayKey(platformProfileName)]
	if !ok {
		t.Fatalf("the platform overlay must always be written, got keys %v", mapKeys(data))
	}
	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(overlay), &parsed); err != nil {
		t.Fatalf("unmarshal platform overlay: %v", err)
	}
	if len(parsed) != 1 {
		t.Errorf("platform overlay must carry memory alone here, got %v", parsed)
	}
	// The default provider is the per-user file store, which a specialist has no
	// identity to key on, so the overlay blanks it — the key is still written, and
	// writing it is the point: it overrides whatever the image baked in.
	memory, _ := parsed["memory"].(map[string]any)
	if fmt.Sprint(memory["provider"]) != "" {
		t.Errorf("provider = %v, want %q", memory["provider"], "")
	}
}

// The specialist profiles only get a provider that can be made read-only and scoped by
// tag. A per-user file provider has no gateway identity to key on there, so the overlay
// blanks it rather than passing it through — see memoryOverlay.
func TestBuildConfigMapDataPlatformOverlayFollowsProvider(t *testing.T) {
	for _, tc := range []struct{ provider, want string }{
		{"", ""},
		{"kube_agents_memory", kubeAgentsMemoryProvider},
		{"hindsight", "hindsight"},
		{"multiuser_memory", ""},
		{"none", ""},
		{"NONE", ""},
	} {
		agent := newTestPlatformAgent()
		if agent.Spec.Harness == nil {
			agent.Spec.Harness = &agentv1alpha1.HarnessSpec{}
		}
		agent.Spec.Harness.Memory = &agentv1alpha1.MemorySpec{Provider: tc.provider}

		overlay := buildConfigMapData(agent, nil)[profileOverlayKey(platformProfileName)]
		var parsed map[string]any
		if err := yaml.Unmarshal([]byte(overlay), &parsed); err != nil {
			t.Fatalf("provider %q: unmarshal platform overlay: %v", tc.provider, err)
		}
		memory, _ := parsed["memory"].(map[string]any)
		got := ""
		if memory["provider"] != nil {
			got = fmt.Sprint(memory["provider"])
		}
		if got != tc.want {
			t.Errorf("provider %q: platform overlay provider = %q, want %q", tc.provider, got, tc.want)
		}
	}
}

// `none` is the only way to say "no external provider": an empty field takes the CRD
// default, so the sentinel has to survive resolution and become the empty string Hermes
// itself uses.
//
// Asserted on MEMORY_PROVIDER rather than on a rendered config, because that env var is
// now the only place the front door's answer appears. The managed scope carries no
// `memory` block at all — it is machine-global, and a per-user file store is exactly the
// kind of setting that must not be handed to every specialist — so the front door's
// provider is the image's (agents/chat/config.yaml). The entrypoint reads this variable
// to decide whether to run the one-way MEMORY.md import, and there "" is a real answer
// distinct from the variable being absent.
func TestMemoryProviderNoneMeansNoProvider(t *testing.T) {
	for _, tc := range []struct{ provider, want string }{
		{"", defaultMemoryProvider},
		{"none", ""},
		{"None", ""},
		{"  none  ", ""},
		{"multiuser_memory", "multiuser_memory"},
		{"mem0", "mem0"},
	} {
		agent := newTestPlatformAgent()
		if agent.Spec.Harness == nil {
			agent.Spec.Harness = &agentv1alpha1.HarnessSpec{}
		}
		agent.Spec.Harness.Memory = &agentv1alpha1.MemorySpec{Provider: tc.provider}

		dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{})
		found := false
		for _, env := range dep.Spec.Template.Spec.Containers[0].Env {
			if env.Name != "MEMORY_PROVIDER" {
				continue
			}
			found = true
			if env.Value != tc.want {
				t.Errorf("provider %q: MEMORY_PROVIDER = %q, want %q", tc.provider, env.Value, tc.want)
			}
		}
		if !found {
			t.Errorf("provider %q: MEMORY_PROVIDER was not set at all; the entrypoint cannot "+
				"distinguish \"no provider\" from \"nothing said\"", tc.provider)
		}
	}
}

// Tuning alone must produce an overlay: limits apply to a profile hosting no plugins.
func TestBuildConfigMapDataTuningOnlyOverlay(t *testing.T) {
	agent := agentWithTuning(&agentv1alpha1.TuningSpec{
		Platform: limits(8, 200),
		Cluster:  limits(8, 150),
	})
	data := buildConfigMapData(agent, nil)

	platform, ok := data["profile-platform.overlay.yaml"]
	if !ok {
		t.Fatalf("expected a platform overlay from tuning alone, got keys %v", mapKeys(data))
	}
	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(platform), &parsed); err != nil {
		t.Fatalf("unmarshal platform overlay: %v", err)
	}
	agentSection, _ := parsed["agent"].(map[string]any)
	if fmt.Sprint(agentSection["max_turns"]) != "200" {
		t.Errorf("platform max_turns = %v, want 200", agentSection["max_turns"])
	}

	// Cluster profiles are named at runtime, so they share one class overlay.
	cluster, ok := data[clusterProfileClassKey]
	if !ok {
		t.Fatalf("expected %s from tuning.cluster, got keys %v", clusterProfileClassKey, mapKeys(data))
	}
	var clusterParsed map[string]any
	if err := yaml.Unmarshal([]byte(cluster), &clusterParsed); err != nil {
		t.Fatalf("unmarshal cluster overlay: %v", err)
	}
	clusterAgent, _ := clusterParsed["agent"].(map[string]any)
	if fmt.Sprint(clusterAgent["max_turns"]) != "150" {
		t.Errorf("cluster max_turns = %v, want 150", clusterAgent["max_turns"])
	}
}

// tuning.default reaches the front door through its overlay, and nothing else.
//
// It cannot go in the managed scope: that scope is machine-global, so one profile's turn
// budget would become every profile's. The overlay is merged into the agent's own
// config.yaml at startup instead, which is also why it must not leak into any other
// profile's overlay — those carry tuning.platform and tuning.cluster.
func TestDefaultTuningReachesTheDefaultOverlayOnly(t *testing.T) {
	agent := agentWithTuning(&agentv1alpha1.TuningSpec{Default: limits(30, 120)})

	var managed map[string]any
	if err := yaml.Unmarshal([]byte(renderConfigYAML(agent, nil)), &managed); err != nil {
		t.Fatalf("unmarshal rendered YAML: %v", err)
	}
	if section, ok := managed["agent"]; ok {
		t.Errorf("the machine-global managed config must carry no per-profile `agent` limits, got %v", section)
	}

	data := buildConfigMapData(agent, nil)
	var overlay map[string]any
	if err := yaml.Unmarshal([]byte(data[profileOverlayKey(defaultProfileName)]), &overlay); err != nil {
		t.Fatalf("unmarshal default overlay: %v", err)
	}
	agentSection, _ := overlay["agent"].(map[string]any)
	if fmt.Sprint(agentSection["api_max_retries"]) != "30" {
		t.Errorf("api_max_retries = %v, want 30", agentSection["api_max_retries"])
	}
	if fmt.Sprint(agentSection["max_turns"]) != "120" {
		t.Errorf("max_turns = %v, want 120", agentSection["max_turns"])
	}

	for k, v := range data {
		if k == profileOverlayKey(defaultProfileName) {
			continue
		}
		if strings.HasPrefix(k, profileOverlayPrefix) && strings.Contains(v, "max_turns") {
			t.Errorf("tuning.default must not leak into another profile's overlay, got key %q:\n%s", k, v)
		}
	}
}

// spec.harness.tuning.maxInProgress travels the same road, and only when the CR sets it:
// an unset one must leave agents/chat/config.yaml's cap in force rather than have the
// operator restate the same number on every reconcile.
func TestMaxInProgressReachesTheDefaultOverlay(t *testing.T) {
	if got := buildConfigMapData(newTestPlatformAgent(), nil)[profileOverlayKey(defaultProfileName)]; strings.Contains(got, "max_in_progress") {
		t.Errorf("an untuned CR must not render a cap, got:\n%s", got)
	}

	eight := 8
	if eight == defaultKanbanMaxInProgress {
		t.Fatalf("test value %d must differ from the image's default to prove the override", eight)
	}
	agent := agentWithTuning(&agentv1alpha1.TuningSpec{MaxInProgress: &eight})

	var overlay map[string]any
	if err := yaml.Unmarshal([]byte(buildConfigMapData(agent, nil)[profileOverlayKey(defaultProfileName)]), &overlay); err != nil {
		t.Fatalf("unmarshal default overlay: %v", err)
	}
	kanban, _ := overlay["kanban"].(map[string]any)
	if fmt.Sprint(kanban["max_in_progress"]) != "8" {
		t.Errorf("max_in_progress = %v, want 8", kanban["max_in_progress"])
	}

	// Raising the cap and lowering it must both work — a one-sided test would pass even
	// if the render silently took the minimum of the CR and the image's default.
	one := 1
	lowered := agentWithTuning(&agentv1alpha1.TuningSpec{MaxInProgress: &one})
	if err := yaml.Unmarshal([]byte(buildConfigMapData(lowered, nil)[profileOverlayKey(defaultProfileName)]), &overlay); err != nil {
		t.Fatalf("unmarshal default overlay: %v", err)
	}
	kanban, _ = overlay["kanban"].(map[string]any)
	if fmt.Sprint(kanban["max_in_progress"]) != "1" {
		t.Errorf("max_in_progress = %v, want 1", kanban["max_in_progress"])
	}
}

// Unset tuning must leave Hermes' own per-run limits alone. The operator pins no
// execution limits of its own: what a fleet needs depends on its model quota and on
// what its agents do, so an untuned deployment gets vanilla Hermes behaviour.
func TestRenderConfigYAMLNoTuningLeavesHermesDefaults(t *testing.T) {
	agent := agentWithTuning(&agentv1alpha1.TuningSpec{Platform: limits(8, 200)})
	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(buildConfigMapData(agent, nil)[profileOverlayKey(platformProfileName)]), &parsed); err != nil {
		t.Fatalf("unmarshal platform overlay: %v", err)
	}
	agentSection, _ := parsed["agent"].(map[string]any)
	if fmt.Sprint(agentSection["max_turns"]) != "200" {
		t.Errorf("tuned max_turns = %v, want 200", agentSection["max_turns"])
	}

	var untuned map[string]any
	if err := yaml.Unmarshal([]byte(buildConfigMapData(newTestPlatformAgent(), nil)[profileOverlayKey(platformProfileName)]), &untuned); err != nil {
		t.Fatalf("unmarshal untuned platform overlay: %v", err)
	}
	untunedAgent, _ := untuned["agent"].(map[string]any)
	for _, key := range []string{"api_max_retries", "max_turns"} {
		if v, ok := untunedAgent[key]; ok {
			t.Errorf("%s must be omitted without tuning so Hermes' default applies, got %v", key, v)
		}
	}
}

// Dispatch concurrency is capped, and the cap is the image's.
//
// Upstream leaves it unbounded, which lets a burst of queued cards spawn one full agent
// process per card until the cgroup OOM killer takes them — a failure that produces no
// container restart and no Kubernetes event, only a stranded card. The operator used to
// render the cap; it no longer can (the managed scope is machine-global and kanban is
// profile-shaped), so agents/chat/config.yaml carries the untuned default and this test
// is what keeps it there. spec.harness.tuning.maxInProgress overrides it through the
// default profile's overlay — see TestMaxInProgressReachesTheDefaultOverlay — and this
// number must stay equal to defaultKanbanMaxInProgress, which that test compares against.
//
// wake_on_events is asserted here too. The front door is woken for a follow-up turn only
// by terminal events it can act on; `completed` is not one of them, because the notifier
// has already delivered the worker's summary to the thread, so waking on it buys a model
// turn that paraphrases a message the user is looking at (5.9s / 32,460 input tokens,
// measured on t_c31a1f00). The key is only honoured because
// deploy/docker/patches/kanban_notifier.py patches it in — upstream hardcodes the set.
func TestChatConfigCapsTheBoardAndWakesOnFailuresOnly(t *testing.T) {
	path := filepath.Join("..", "..", "..", "agents", "chat", "config.yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading %s: %v", path, err)
	}
	var image map[string]any
	if err := yaml.Unmarshal(raw, &image); err != nil {
		t.Fatalf("unmarshaling %s: %v", path, err)
	}
	kanban, _ := image["kanban"].(map[string]any)

	cap, ok := kanban["max_in_progress"]
	if !ok {
		t.Fatalf("%s must cap max_in_progress — nothing else does now; got kanban block %v", path, kanban)
	}
	// A 0 would be worse than no key at all: Hermes ignores anything below 1, so the
	// file would read as a capped board while behaving as an unbounded one.
	if n, isInt := cap.(int); !isInt || n < 1 {
		t.Errorf("max_in_progress = %v, want a positive integer", cap)
	} else if n != defaultKanbanMaxInProgress {
		t.Errorf("%s caps the board at %d but the operator's defaultKanbanMaxInProgress is %d; "+
			"the two are the same number by contract", path, n, defaultKanbanMaxInProgress)
	}

	raw2, ok := kanban["wake_on_events"].([]any)
	if !ok {
		t.Fatalf("wake_on_events = %v (%T), want a list", kanban["wake_on_events"], kanban["wake_on_events"])
	}
	got := make([]string, 0, len(raw2))
	for _, v := range raw2 {
		got = append(got, fmt.Sprint(v))
	}
	want := []string{"gave_up", "crashed", "timed_out", "blocked"}
	if !slices.Equal(got, want) {
		t.Errorf("wake_on_events = %v, want %v", got, want)
	}
}

// A platform adapter's `platforms` config must stay on the default profile even when
// its plugin targets another one: adapters are gateway singletons read from the default
// profile, so a subscription placed elsewhere is configured where nothing listens —
// ingress stops silently while every CR still looks correct.
func TestGatewayScopedConfigStaysOnDefaultProfile(t *testing.T) {
	const cfgYAML = `
platforms:
  pubsub:
    enabled: true
    extra:
      subscriptions:
        alerts:
          topic: my-topic
approvals:
  cron_mode: approve
`
	plugin := pluginWithProfile("stockout", "platform", cfgYAML)
	agent := newTestPlatformAgent()

	var rendered map[string]any
	if err := yaml.Unmarshal([]byte(renderConfigYAML(agent, []*agentv1alpha1.AgentPlugin{plugin})), &rendered); err != nil {
		t.Fatalf("unmarshal default config: %v", err)
	}
	pubsub, _ := ((rendered["platforms"].(map[string]any))["pubsub"]).(map[string]any)
	subs, _ := ((pubsub["extra"].(map[string]any))["subscriptions"]).(map[string]any)
	if _, ok := subs["alerts"]; !ok {
		t.Errorf("targeted plugin's pubsub subscription must reach the DEFAULT profile, got %v", rendered["platforms"])
	}
	var overlay map[string]any
	if err := yaml.Unmarshal([]byte(renderProfileOverlayYAML([]*agentv1alpha1.AgentPlugin{plugin}, nil, nil, nil)), &overlay); err != nil {
		t.Fatalf("unmarshal overlay: %v", err)
	}
	if _, ok := overlay["platforms"]; ok {
		t.Errorf("`platforms` must NOT be routed to a profile overlay, got %v", overlay)
	}
	if _, ok := overlay["approvals"]; !ok {
		t.Errorf("profile-scoped `approvals` should follow the plugin to its overlay, got %v", overlay)
	}
}

// pluginConfigForScope decides whether a subtree follows a plugin to its profile or
// stays with the gateway. Getting it wrong is invisible in the CR and fatal at runtime:
// a subscription routed to a named profile is configured where nothing listens.
func TestPluginConfigForScope(t *testing.T) {
	cfg := map[string]any{
		"platforms":         map[string]any{"pubsub": map[string]any{"enabled": true}},
		"approvals":         map[string]any{"cron_mode": "approve"},
		"platform_toolsets": map[string]any{"pubsub": []any{"gke"}},
		"agent":             map[string]any{"max_turns": 9999}, // never allowed from a plugin
		"logging":           map[string]any{"level": "debug"},  // not allowlisted
	}

	gateway := pluginConfigForScope(cfg, true)
	if got := sortedKeys(gateway); len(got) != 1 || got[0] != "platforms" {
		t.Errorf("gateway scope = %v, want [platforms] only", got)
	}

	profile := pluginConfigForScope(cfg, false)
	want := []string{"approvals", "platform_toolsets"}
	if got := sortedKeys(profile); !slices.Equal(got, want) {
		t.Errorf("profile scope = %v, want %v", got, want)
	}

	// Neither scope may carry a subtree outside the allowlist.
	for _, scope := range []map[string]any{gateway, profile} {
		for _, banned := range []string{"agent", "logging"} {
			if _, ok := scope[banned]; ok {
				t.Errorf("disallowed subtree %q escaped scoping: %v", banned, sortedKeys(scope))
			}
		}
	}
}

func sortedKeys(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func TestAgentLimitsOverlay(t *testing.T) {
	// Nil and empty must produce nothing: an empty overlay would be written as a
	// ConfigMap key and rewrite the profile config on every start for no reason.
	if got := agentLimitsOverlay(nil); got != nil {
		t.Errorf("agentLimitsOverlay(nil) = %v, want nil", got)
	}
	if got := agentLimitsOverlay(&agentv1alpha1.AgentLimits{}); got != nil {
		t.Errorf("agentLimitsOverlay(empty) = %v, want nil", got)
	}

	// Partial limits emit only what was set, so an unset field falls through to Hermes.
	turns := 200
	got := agentLimitsOverlay(&agentv1alpha1.AgentLimits{MaxTurns: &turns})
	agentSection, _ := got["agent"].(map[string]any)
	if fmt.Sprint(agentSection["max_turns"]) != "200" {
		t.Errorf("max_turns = %v, want 200", agentSection["max_turns"])
	}
	if _, ok := agentSection["api_max_retries"]; ok {
		t.Errorf("unset api_max_retries must be omitted, got %v", agentSection["api_max_retries"])
	}
}

// The key shape is a contract with docker-entrypoint.sh, which globs for it.
func TestProfileOverlayKey(t *testing.T) {
	if got := profileOverlayKey("platform"); got != "profile-platform.overlay.yaml" {
		t.Errorf("profileOverlayKey(platform) = %q", got)
	}
	if clusterProfileClassKey != "profileclass-cluster.overlay.yaml" {
		t.Errorf("clusterProfileClassKey = %q", clusterProfileClassKey)
	}
	// Distinct prefixes: a real profile named "cluster" must not collide with the class
	// overlay applied to every cluster-* profile.
	if profileOverlayKey("cluster") == clusterProfileClassKey {
		t.Error("per-profile and class overlay keys must not collide")
	}
	// The default profile is the one the operator does NOT emit an overlay for — its
	// render is the managed scope — and the managed key must stay clear of the glob the
	// entrypoint walks, or step 2.7 would pick it up as an overlay for a profile named
	// "default".
	if strings.HasPrefix(managedConfigKey, profileOverlayPrefix) || strings.HasSuffix(managedConfigKey, ".overlay.yaml") {
		t.Errorf("managedConfigKey = %q, which the entrypoint's overlay glob would match", managedConfigKey)
	}
}

func TestOtlpCollectorNamespace(t *testing.T) {
	tests := []struct {
		endpoint string
		want     string
	}{
		{"", "gke-managed-otel"},
		{"http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318", "gke-managed-otel"},
		{"http://otel-collector.observability.svc.cluster.local:4318", "observability"},
		{"https://my-custom-collector.monitoring:4317", "monitoring"},
		{"http://my-collector.custom.svc:4318/v1/traces", "custom"},
		{"http://just-host:4318", ""},
		{"just-host:4318", ""},
		{"https://foo.bar.com:4317", ""},
		{"http://my-collector.custom.svc.cluster.local:4318/v1/traces", "custom"},
	}

	for _, tc := range tests {
		t.Run(tc.endpoint, func(t *testing.T) {
			got := otlpCollectorNamespace(tc.endpoint)
			if got != tc.want {
				t.Errorf("otlpCollectorNamespace(%q) = %q; want %q", tc.endpoint, got, tc.want)
			}
		})
	}
}

// agentWithEventWatcher builds a PlatformAgent whose harness names the emergency
// stop explicitly. A nil `enabled` stands for the CR that writes the object but
// not the key, which the CRD default covers rather than this code.
func agentWithEventWatcher(enabled *bool) *agentv1alpha1.PlatformAgent {
	a := newTestPlatformAgent()
	a.Spec.Harness = &agentv1alpha1.HarnessSpec{EventWatcher: &agentv1alpha1.EventWatcherSpec{Enabled: enabled}}
	return a
}

// The default has to be "watching". Every install today omits the field, so a
// resolver that read absence as off would silently end incident detection fleet-wide
// on the upgrade that introduced it.
func TestEventWatcherEnabledDefaultsOnWhenUnspecified(t *testing.T) {
	noHarness := newTestPlatformAgent()
	if !eventWatcherEnabled(noHarness) {
		t.Error("an agent with no harness at all must still watch events")
	}
	if !eventWatcherEnabled(agentWithTuning(nil)) {
		t.Error("a harness that says nothing about the watcher must still watch events")
	}
	if !eventWatcherEnabled(agentWithEventWatcher(nil)) {
		t.Error("an eventWatcher block with no enabled key must still watch events")
	}
}

func TestEventWatcherEnabledHonoursAnExplicitFalse(t *testing.T) {
	if eventWatcherEnabled(agentWithEventWatcher(ptr.To(false))) {
		t.Error("enabled: false must turn the watcher off")
	}
	if !eventWatcherEnabled(agentWithEventWatcher(ptr.To(true))) {
		t.Error("enabled: true must leave the watcher on")
	}
}

// The entrypoint reads EVENT_WATCHER_ENABLED; nothing else carries the decision
// into the pod. The value is written on every reconcile rather than only when the
// stop is pressed, so that a Deployment answers "is the watcher meant to be
// running?" without anyone reading the CR — from outside the container a
// deliberately silent install and a broken one look identical.
func TestCredentialProxyCarriesTheEventWatcherSwitch(t *testing.T) {
	for _, tc := range []struct {
		name  string
		agent *agentv1alpha1.PlatformAgent
		want  string
	}{
		{"unspecified", newTestPlatformAgent(), "true"},
		{"explicitly on", agentWithEventWatcher(ptr.To(true)), "true"},
		{"emergency stop", agentWithEventWatcher(ptr.To(false)), "false"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sidecar := buildCredentialProxySidecar(tc.agent, "/opt/data")
			var found []corev1.EnvVar
			for _, env := range sidecar.Env {
				if env.Name == "EVENT_WATCHER_ENABLED" {
					found = append(found, env)
				}
			}
			if len(found) != 1 {
				t.Fatalf("expected exactly one EVENT_WATCHER_ENABLED, got %#v", found)
			}
			if found[0].Value != tc.want {
				t.Errorf("EVENT_WATCHER_ENABLED = %q, want %q", found[0].Value, tc.want)
			}
		})
	}
}

// spec.deployment.env is merged into the sidecar's environment, and the name is not
// in the reserved set. It does not need to be: the operator appends its own entry
// afterwards, and Kubernetes resolves a duplicated name to the last one. Pinning it
// because the guarantee lives in the ordering of two append calls, where a later
// refactor could reasonably move one above the merge and hand the switch to whoever
// can edit the CR's env list.
// The switch is the operator's to set, so a same-named entry in
// spec.deployment.env has to be dropped — not merely out-voted.
//
// Counting the entries is the whole point of this test. `containers[].env` is a
// listType=map keyed on name, and the operator applies the Deployment with
// server-side apply, which rejects a duplicate key outright rather than taking
// the last one. So a merge that leaves two `EVENT_WATCHER_ENABLED` entries does
// not quietly prefer the operator's value: it fails every reconcile from then
// on, freezing the Deployment against this change and every later one. An
// earlier version of this test read the last matching entry and passed while
// exactly that was happening, which is why it asserts a count now.
func TestDeploymentEnvCannotOverrideTheEventWatcherSwitch(t *testing.T) {
	for _, userValue := range []string{"false", "true", "wat"} {
		t.Run(userValue, func(t *testing.T) {
			agent := newTestPlatformAgent()
			agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
				Env: []corev1.EnvVar{{Name: "EVENT_WATCHER_ENABLED", Value: userValue}},
			}

			var found []string
			for _, e := range buildCredentialProxySidecar(agent, "/opt/data").Env {
				if e.Name == "EVENT_WATCHER_ENABLED" {
					found = append(found, e.Value)
				}
			}
			if len(found) != 1 {
				t.Fatalf("want exactly one EVENT_WATCHER_ENABLED entry, got %d (%q); server-side apply rejects a duplicate key in env", len(found), found)
			}
			if found[0] != "true" {
				t.Errorf("the operator's value must win over spec.deployment.env, got %q", found[0])
			}
		})
	}
}

// The same hole, on the variable next to it. EVENT_WATCHER_CLUSTER_NAME is
// appended after the same merge and was unreserved for as long as it has
// existed; it is pinned here so the two cannot drift apart again.
func TestDeploymentEnvCannotDuplicateTheEventWatcherClusterName(t *testing.T) {
	agent := newTestPlatformAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Env: []corev1.EnvVar{{Name: "EVENT_WATCHER_CLUSTER_NAME", Value: "not-the-operators-idea"}},
	}

	var found []string
	for _, e := range buildCredentialProxySidecar(agent, "/opt/data").Env {
		if e.Name == "EVENT_WATCHER_CLUSTER_NAME" {
			found = append(found, e.Value)
		}
	}
	if len(found) != 1 {
		t.Fatalf("want exactly one EVENT_WATCHER_CLUSTER_NAME entry, got %d (%q)", len(found), found)
	}
	if found[0] == "not-the-operators-idea" {
		t.Errorf("spec.deployment.env overrode the operator's cluster name, got %q", found[0])
	}
}

// Turning the watcher off must not disturb the wiring around it. The volumes, the
// token projection, and the kubeconfig path are shared with the credential proxy or
// needed the moment the switch goes back on, so the stop is a decision about one
// process rather than a teardown of the sidecar.
func TestTheEmergencyStopLeavesTheSidecarWiringIntact(t *testing.T) {
	off := buildCredentialProxySidecar(agentWithEventWatcher(ptr.To(false)), "/opt/data")

	var tokenMount, kubeconfigMount bool
	for _, m := range off.VolumeMounts {
		if m.Name == "event-watcher-ksa-token" && m.MountPath == "/var/run/secrets/kubernetes.io/serviceaccount" {
			tokenMount = true
		}
		if m.Name == "event-watcher-kubeconfig" {
			kubeconfigMount = true
		}
	}
	if !tokenMount || !kubeconfigMount {
		t.Errorf("disabling the watcher must not strip its mounts, got %#v", off.VolumeMounts)
	}

	var sawClusterName, sawSessionKey bool
	for _, e := range off.Env {
		if e.Name == "EVENT_WATCHER_CLUSTER_NAME" {
			sawClusterName = true
		}
		if e.Name == "SESSION_KV_API_KEY" {
			sawSessionKey = true
		}
	}
	if !sawClusterName || !sawSessionKey {
		t.Errorf("disabling the watcher must not strip its configuration, got %#v", off.Env)
	}
}

// --- spec.harness.experimental.platformFrontDoor -----------------------------------

// frontDoorAgent builds a PlatformAgent with Google Chat on and the experimental flag
// set as given, so the on/off pair differs in exactly one field.
func frontDoorAgent(name string, replicas int32, on bool) *agentv1alpha1.PlatformAgent {
	agent := haAgent(name, replicas)
	agent.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{
		GoogleChat: &agentv1alpha1.GoogleChatSpec{Enabled: ptr.To(true)},
	}
	if on {
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
			Experimental: &agentv1alpha1.ExperimentalSpec{PlatformFrontDoor: ptr.To(true)},
		}
	}
	return agent
}

// platformOverlay parses the platform profile's overlay out of the ConfigMap.
func platformOverlay(t *testing.T, agent *agentv1alpha1.PlatformAgent) map[string]any {
	t.Helper()
	raw, ok := buildConfigMapData(agent, nil)[profileOverlayKey(platformProfileName)]
	if !ok {
		t.Fatalf("no %s in the ConfigMap", profileOverlayKey(platformProfileName))
	}
	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(raw), &parsed); err != nil {
		t.Fatalf("unmarshal platform overlay: %v\n%s", err, raw)
	}
	return parsed
}

func lookup(m map[string]any, path []string) (any, bool) {
	var cur any = m
	for _, key := range path {
		asMap, ok := cur.(map[string]any)
		if !ok {
			return nil, false
		}
		if cur, ok = asMap[key]; !ok {
			return nil, false
		}
	}
	return cur, true
}

func listAt(t *testing.T, m map[string]any, path []string) []any {
	t.Helper()
	v, _ := lookup(m, path)
	list, _ := v.([]any)
	return list
}

func sortedStrings(t *testing.T, list []any) []string {
	t.Helper()
	out := make([]string, 0, len(list))
	for _, v := range list {
		out = append(out, fmt.Sprint(v))
	}
	sort.Strings(out)
	return out
}

// TestPlatformFrontDoorArgsSelectTheProfile pins where the profile name reaches the
// gateway from, which differs by replica count.
//
// `--profile` is a GLOBAL flag: hermes_cli/main.py::_apply_profile_override pre-parses it
// out of argv before any import and re-points HERMES_HOME. `hermes gateway run --profile
// platform` is a different command — the subcommand has no such flag — so the position
// here is load-bearing, not stylistic.
//
// Above one replica the argv belongs to leader_elect.py, which starts the gateway as a
// child; the profile reaches it through HERMES_GATEWAY_PROFILE instead, and putting it in
// the container args there would replace the wrapper and lose leader election entirely.
func TestPlatformFrontDoorArgsSelectTheProfile(t *testing.T) {
	for _, tc := range []struct {
		name     string
		replicas int32
		on       bool
		want     []string
	}{
		{"off at one replica keeps the image CMD", 1, false, nil},
		{"on at one replica names the profile", 1, true, []string{"hermes", "--profile", "platform", "gateway", "run"}},
		{"off above one replica keeps the wrapper", 2, false, []string{"/opt/hermes/.venv/bin/python3", "/opt/data/leader_elect.py"}},
		{"on above one replica keeps the wrapper", 2, true, []string{"/opt/hermes/.venv/bin/python3", "/opt/data/leader_elect.py"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dep := buildDeployment(frontDoorAgent("fd-agent", tc.replicas, tc.on), "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
			gateway := containerNamed(t, dep, "platform-agent")

			if len(gateway.Command) != 0 {
				t.Errorf("Command must stay unset so the image ENTRYPOINT still runs the "+
					"shared-state setup; got %v", gateway.Command)
			}
			if len(tc.want) == 0 {
				if len(gateway.Args) != 0 {
					t.Errorf("expected the image CMD to stand, got args=%v", gateway.Args)
				}
				return
			}
			if !reflect.DeepEqual(gateway.Args, tc.want) {
				t.Errorf("args = %v, want %v", gateway.Args, tc.want)
			}
		})
	}
}

// TestPlatformFrontDoorProfileEnvIsGatewayOnly pins the variable to the one container
// that has readers for it.
//
// In the gateway it has two: leader_elect.py builds its argv from it, and
// docker-entrypoint.sh step 2.6 reads it to stop force-syncing profiles/platform/config.yaml
// from the image, because as the front door that file is one the agent itself writes to.
//
// The dashboard sidecar must NOT get it. It carries AGENT_SHARED_STATE_SETUP=skip and execs
// out of the entrypoint at the step-1.5 gate, so it would never act on it — but it also does
// not mount the operator's overlay directory, and a container that reached step 2.6b without
// one would back-fill the platform config from image defaults alone while the primary was
// writing the same file.
//
// On the gateway it is present either way, and the off case is the half worth pinning: the
// operator emitting nothing leaves an AgentPlugin's spec.env — copied verbatim, no allowlist
// — as the only writer of the name, and last-wins cannot settle a duplicate that does not
// exist. Empty is the off answer both readers already understand.
func TestPlatformFrontDoorProfileEnvIsGatewayOnly(t *testing.T) {
	for _, replicas := range []int32{1, 2} {
		t.Run(fmt.Sprintf("replicas=%d", replicas), func(t *testing.T) {
			on := buildDeployment(frontDoorAgent("fd-env", replicas, true), "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
			got, found := envValue(containerNamed(t, on, "platform-agent"), gatewayProfileEnvVar)
			if !found || got != platformProfileName {
				t.Errorf("gateway %s = %q (found=%v), want %q", gatewayProfileEnvVar, got, found, platformProfileName)
			}
			if _, found := envValue(containerNamed(t, on, "platform-agent-dashboard"), gatewayProfileEnvVar); found {
				t.Errorf("the dashboard sidecar must not carry %s: it has no overlay mount, so "+
					"acting on it would mean rebuilding the platform config from image defaults alone",
					gatewayProfileEnvVar)
			}

			off := buildDeployment(frontDoorAgent("fd-env", replicas, false), "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
			got, found = envValue(containerNamed(t, off, "platform-agent"), gatewayProfileEnvVar)
			if !found || got != "" {
				t.Errorf("gateway %s = %q (found=%v) with the flag off, want an empty value that "+
					"is still emitted: a name the operator never writes is one an AgentPlugin's "+
					"spec.env can claim outright", gatewayProfileEnvVar, got, found)
			}
			if _, found := envValue(containerNamed(t, off, "platform-agent-dashboard"), gatewayProfileEnvVar); found {
				t.Errorf("the dashboard sidecar must not carry %s with the flag off either",
					gatewayProfileEnvVar)
			}
		})
	}
}

// TestPlatformFrontDoorOverlayCarriesTheIngressKeys covers the config half: the profile the
// gateway is now homed at has to have the toolsets and the ingress plugins that until now
// only the default profile's rendering carried.
func TestPlatformFrontDoorOverlayCarriesTheIngressKeys(t *testing.T) {
	overlay := platformOverlay(t, frontDoorAgent("fd-overlay", 1, true))

	// The toolsets key is the one that cannot be left to the fallback: an absent key puts
	// the platform on the auto-generated `hermes-google_chat` composite and unions in every
	// enabled MCP server, which is broader than the worker surface this pins, not narrower.
	toolsets, _ := overlay["platform_toolsets"].(map[string]any)
	for _, platform := range []string{"google_chat", "slack"} {
		got := sortedStrings(t, listAt(t, overlay, []string{"platform_toolsets", platform}))
		want := slices.Sorted(slices.Values(frontDoorToolsets))
		if !slices.Equal(got, want) {
			t.Errorf("platform_toolsets.%s = %v, want %v", platform, got, want)
		}
	}
	if len(toolsets) == 0 {
		t.Error("platform_toolsets is empty")
	}

	enabled := sortedStrings(t, listAt(t, overlay, []string{"plugins", "enabled"}))
	for _, plugin := range frontDoorPlugins {
		if !slices.Contains(enabled, plugin) {
			t.Errorf("plugins.enabled is missing %q; it hooks ingress, so a message reaching "+
				"this profile without it silently loses the behaviour. Got %v", plugin, enabled)
		}
	}
}

// TestPlatformFrontDoorOverlayLeavesTheAdaptersToTheManagedScope is the other half of the
// same contract, and the one that is easy to get wrong: the adapters the gateway needs are
// already on this profile, so rendering them here too is a second writer, not a fix.
//
// managed-config.yaml is machine-global — get_managed_dir() takes no profile argument — so
// `platforms.*` and `display.platforms` land on every profile including this one, and a
// managed leaf REPLACES rather than merges. An overlay copy would either lose to it or
// silently diverge from it depending on which config the reader resolves first.
func TestPlatformFrontDoorOverlayLeavesTheAdaptersToTheManagedScope(t *testing.T) {
	overlay := platformOverlay(t, frontDoorAgent("fd-adapters", 1, true))
	for _, key := range []string{"platforms", "display"} {
		if _, present := overlay[key]; present {
			t.Errorf("%s is rendered into the platform overlay; the managed scope already "+
				"lands it on every profile, and two writers for one key is what its contract "+
				"forbids:\n%v", key, overlay)
		}
	}

	// Same reasoning, different reader: nothing consumes `leader_election` from config at
	// all any more. Leadership is the Lease held by leader_elect.py, driven by container env.
	if _, present := platformOverlay(t, frontDoorAgent("fd-adapters", 2, true))["leader_election"]; present {
		t.Error("leader_election is rendered into the platform overlay, but no reader remains " +
			"for it; leadership is the Lease leader_elect.py holds")
	}
}

// The flag is only worth having if it comes back off. profile_overlay.py unapplies a
// withdrawn key from its last-applied record, so the revert is exactly "the operator stops
// rendering these" — which is worth a test of its own, because the platform overlay is
// written unconditionally and it would be easy to leave the keys in it.
func TestPlatformFrontDoorOverlayIsEmptyWhenOff(t *testing.T) {
	overlay := platformOverlay(t, frontDoorAgent("fd-off", 1, false))
	for _, key := range []string{"platform_toolsets", "kanban"} {
		if _, present := overlay[key]; present {
			t.Errorf("%s is rendered into the platform overlay with the flag off, so turning "+
				"the flag off would not undo it:\n%v", key, overlay)
		}
	}
	if plugins, ok := overlay["plugins"].(map[string]any); ok {
		t.Errorf("plugins is rendered with no plugin targeting the profile: %v", plugins)
	}
}

// TestPlatformFrontDoorOverlayKeepsTargetedPlugins guards a clobber that only appears when
// both features are in use. The overlay's plugins.enabled used to be ASSIGNED after the
// front-door keys were merged, so an AgentPlugin targeting the platform profile replaced the
// ingress plugins wholesale — and the symptom is not a missing plugin but chat sessions that
// stop persisting, on an install where the flag had been working.
func TestPlatformFrontDoorOverlayKeepsTargetedPlugins(t *testing.T) {
	agent := frontDoorAgent("fd-plugins", 1, true)
	overlay := map[string]any{}
	raw := buildConfigMapData(agent, []*agentv1alpha1.AgentPlugin{
		pluginWithProfile("extratool", platformProfileName, ""),
	})[profileOverlayKey(platformProfileName)]
	if err := yaml.Unmarshal([]byte(raw), &overlay); err != nil {
		t.Fatalf("unmarshal platform overlay: %v\n%s", err, raw)
	}

	enabled := sortedStrings(t, listAt(t, overlay, []string{"plugins", "enabled"}))
	for _, want := range append(slices.Clone(frontDoorPlugins), "extratool") {
		if !slices.Contains(enabled, want) {
			t.Errorf("plugins.enabled = %v, missing %q", enabled, want)
		}
	}
}

// TestPlatformFrontDoorOverlayMergesTargetedPluginToolsets guards the second half of the
// clobber TestPlatformFrontDoorOverlayKeepsTargetedPlugins covers for plugins.enabled.
//
// `platform_toolsets` is allowlisted and not gateway-scoped, so a plugin targeting this
// profile reaches the same merge — and mergeMaps only recurses into a nested map that
// toStrMap recognises. Built as map[string][]string the front door's own subtree was
// REPLACED rather than merged, and the symptom is not a missing key: the chat platforms fall
// back to the auto-generated `hermes-google_chat` composite — the full core bundle plus every
// enabled MCP server (see frontDoorToolsets) — so the front door quietly widens rather than
// failing shut, on an install where the flag had been working.
func TestPlatformFrontDoorOverlayMergesTargetedPluginToolsets(t *testing.T) {
	agent := frontDoorAgent("fd-toolsets", 1, true)
	overlay := map[string]any{}
	raw := buildConfigMapData(agent, []*agentv1alpha1.AgentPlugin{
		pluginWithProfile("pubsubtool", platformProfileName,
			"platform_toolsets:\n  pubsub:\n    - kanban\n  google_chat:\n    - mcp-extra\n"),
	})[profileOverlayKey(platformProfileName)]
	if err := yaml.Unmarshal([]byte(raw), &overlay); err != nil {
		t.Fatalf("unmarshal platform overlay: %v\n%s", err, raw)
	}

	// The plugin's own key arrives, and the front door's survive alongside it.
	if got := sortedStrings(t, listAt(t, overlay, []string{"platform_toolsets", "pubsub"})); !slices.Equal(got, []string{"kanban"}) {
		t.Errorf("platform_toolsets.pubsub = %v, want the plugin's own list", got)
	}
	if got := sortedStrings(t, listAt(t, overlay, []string{"platform_toolsets", "slack"})); !slices.Equal(got, slices.Sorted(slices.Values(frontDoorToolsets))) {
		t.Errorf("platform_toolsets.slack = %v, want the front door's toolsets left intact", got)
	}

	// Within a subtree the two sides union, which is the contract the AgentPlugin CRD page
	// states: "list values are unioned with the operator's own entries rather than
	// replacing them".
	want := slices.Sorted(slices.Values(append(slices.Clone(frontDoorToolsets), "mcp-extra")))
	if got := sortedStrings(t, listAt(t, overlay, []string{"platform_toolsets", "google_chat"})); !slices.Equal(got, want) {
		t.Errorf("platform_toolsets.google_chat = %v, want %v", got, want)
	}
}

// TestFrontDoorKanbanMatchesChatConfig is the drift guard frontDoorKanban names.
//
// The dispatcher and the notifier run in the gateway process and read `kanban` through
// load_config(), which resolves from get_hermes_home() — so the block has to be on whichever
// profile the gateway is homed at, and it has to say the same thing there. The image copy
// lives in agents/chat/config.yaml and reaches the default profile only; neither
// agents/platform/config.yaml nor the managed scope declares the key, so a block that fails
// to follow the gateway does not fall back to the chat profile's: it falls back to upstream,
// where dispatch is unbounded, the tick is 60s, and spec.harness.tuning.maxInProgress
// silently stops meaning anything.
//
// The comparison is against the image file rather than against the default profile's
// overlay because the operator does not render the block there at all — it defers to
// agents/chat/config.yaml, which is what makes that file the one source to track.
func TestFrontDoorKanbanMatchesChatConfig(t *testing.T) {
	path := filepath.Join("..", "..", "..", "agents", "chat", "config.yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading %s: %v", path, err)
	}
	var image struct {
		Kanban map[string]any `yaml:"kanban"`
	}
	if err := yaml.Unmarshal(raw, &image); err != nil {
		t.Fatalf("unmarshaling %s: %v", path, err)
	}
	if len(image.Kanban) == 0 {
		t.Fatalf("kanban is gone from %s; frontDoorKanban has no source to track and this test "+
			"would pass against nothing", path)
	}

	got, ok := platformOverlay(t, frontDoorAgent("fd-kanban", 1, true))["kanban"].(map[string]any)
	if !ok {
		t.Fatal("the front-door overlay carries no kanban block, so the dispatcher reverts to " +
			"upstream's unbounded behaviour")
	}
	if !reflect.DeepEqual(got, image.Kanban) {
		t.Errorf("the front door's kanban block differs from the one the chat profile gets "+
			"from %s:\n  overlay: %v\n  image:   %v", path, got, image.Kanban)
	}

	// The CR field is the reason equality with the image is not enough on its own: it is
	// documented as "board-wide cap on concurrent kanban workers", and reaching only a
	// profile the gateway is not homed at is the same as not reaching anything.
	withCap := frontDoorAgent("fd-kanban-cap", 1, true)
	withCap.Spec.Harness.Tuning = &agentv1alpha1.TuningSpec{MaxInProgress: ptr.To(7)}
	capped, _ := platformOverlay(t, withCap)["kanban"].(map[string]any)
	if capped["max_in_progress"] != 7 {
		t.Errorf("max_in_progress = %v, want spec.harness.tuning.maxInProgress (7) to reach the "+
			"profile the gateway runs as", capped["max_in_progress"])
	}
}

// TestFrontDoorPluginsMatchChatConfig is the drift guard frontDoorPlugins names.
//
// The list is derived, not chosen: every ingress plugin the chat profile enables, less the
// two named below, less the three agents/platform/config.yaml already enables. Adding an
// ingress plugin to the chat profile and not here is the drift that matters — it hooks a
// message the front door is the one receiving, so the behaviour is simply lost, with
// nothing logged.
//
// The two exclusions are listed here rather than assumed, so that adding a third is a
// deliberate edit to a test that states its reason and not a quiet trim of a var.
func TestFrontDoorPluginsMatchChatConfig(t *testing.T) {
	read := func(name string) []string {
		t.Helper()
		path := filepath.Join("..", "..", "..", "agents", name, "config.yaml")
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("reading %s: %v", path, err)
		}
		var image struct {
			Plugins struct {
				Enabled []string `yaml:"enabled"`
			} `yaml:"plugins"`
		}
		if err := yaml.Unmarshal(raw, &image); err != nil {
			t.Fatalf("unmarshaling %s: %v", path, err)
		}
		if len(image.Plugins.Enabled) == 0 {
			t.Fatalf("plugins.enabled is gone from %s; this test would pass against nothing", path)
		}
		return image.Plugins.Enabled
	}

	// agent_roster: delegation only, and the front door does the work itself.
	// bootstrap_onboarding: its markers and its delivery job both resolve from the home
	// the flag moves away from, so enabling it here greets an onboarded install and
	// promises a report the dead `default` roster can never deliver.
	excluded := []string{"agent_roster", "bootstrap_onboarding"}

	onPlatform := read("platform")
	want := []string{}
	for _, plugin := range read("chat") {
		if slices.Contains(excluded, plugin) || slices.Contains(onPlatform, plugin) {
			continue
		}
		want = append(want, plugin)
	}
	for _, plugin := range excluded {
		if !slices.Contains(read("chat"), plugin) {
			t.Errorf("%q is excluded from frontDoorPlugins but the chat profile no longer "+
				"enables it; the exclusion and its reasoning are now stale", plugin)
		}
	}
	if got := slices.Sorted(slices.Values(frontDoorPlugins)); !slices.Equal(got, slices.Sorted(slices.Values(want))) {
		t.Errorf("frontDoorPlugins is no longer the chat profile's ingress plugins:\n"+
			"  operator: %v\n  derived:  %v", got, slices.Sorted(slices.Values(want)))
	}
}

// TestFrontDoorToolsetsMatchPlatformConfig is the drift guard frontDoorToolsets names.
//
// The list is the image's own `cli` toolsets verbatim, and that equality IS the contract:
// a chat message should reach the surface a kanban worker on this profile already has, no
// more. Add an MCP server to agents/platform/config.yaml and chat ingress would otherwise
// silently not get it, which reads as the agent choosing not to use a tool.
func TestFrontDoorToolsetsMatchPlatformConfig(t *testing.T) {
	path := filepath.Join("..", "..", "..", "agents", "platform", "config.yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading %s: %v", path, err)
	}
	var image struct {
		PlatformToolsets map[string][]string `yaml:"platform_toolsets"`
	}
	if err := yaml.Unmarshal(raw, &image); err != nil {
		t.Fatalf("unmarshaling %s: %v", path, err)
	}
	want, ok := image.PlatformToolsets["cli"]
	if !ok {
		t.Fatalf("platform_toolsets.cli is gone from %s; frontDoorToolsets has no source to "+
			"track and this test would pass against nothing", path)
	}
	if got, want := slices.Sorted(slices.Values(frontDoorToolsets)), slices.Sorted(slices.Values(want)); !slices.Equal(got, want) {
		t.Errorf("frontDoorToolsets differs from platform_toolsets.cli in %s:\n  operator: %v\n  image:    %v",
			path, got, want)
	}
}
