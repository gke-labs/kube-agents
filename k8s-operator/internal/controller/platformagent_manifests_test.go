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
			Harness: &agentv1alpha1.PlatformAgentHarnessSpec{
				Hermes: &agentv1alpha1.HermesSpec{
					AgentHome: "/custom/home",
				},
			},
			Integration: &agentv1alpha1.IntegrationSpec{
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
}

func TestBuildServiceAccount(t *testing.T) {
	tests := []struct {
		name   string
		agent  *agentv1alpha1.PlatformAgent
		verify func(*testing.T, *corev1.ServiceAccount)
	}{
		{
			name: "uses default name if ServiceAccountName is empty",
			agent: &agentv1alpha1.PlatformAgent{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "my-agent",
					Namespace: "my-ns",
				},
				Spec: agentv1alpha1.PlatformAgentSpec{
					Security: &agentv1alpha1.SecuritySpec{
						ServiceAccountName: "",
					},
				},
			},
			verify: func(t *testing.T, sa *corev1.ServiceAccount) {
				if sa.Name != "my-agent" {
					t.Errorf("expected SA name to be my-agent, got %q", sa.Name)
				}
				if len(sa.Annotations) != 0 {
					t.Errorf("expected no annotations, got %v", sa.Annotations)
				}
			},
		},
		{
			name: "uses custom name and injects GKE Workload Identity annotation",
			agent: &agentv1alpha1.PlatformAgent{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "my-agent",
					Namespace: "my-ns",
				},
				Spec: agentv1alpha1.PlatformAgentSpec{
					Security: &agentv1alpha1.SecuritySpec{
						ServiceAccountName: "custom-sa",
						WorkloadIdentity: &agentv1alpha1.WorkloadIdentitySpec{
							Gcp: &agentv1alpha1.GcpWorkloadIdentitySpec{
								GSAName:   "my-gsa",
								ProjectID: "my-gcp-project",
							},
						},
					},
				},
			},
			verify: func(t *testing.T, sa *corev1.ServiceAccount) {
				if sa.Name != "custom-sa" {
					t.Errorf("expected SA name to be custom-sa, got %q", sa.Name)
				}
				expectedAnnotation := "my-gsa@my-gcp-project.iam.gserviceaccount.com"
				actual := sa.Annotations["iam.gke.io/gcp-service-account"]
				if actual != expectedAnnotation {
					t.Errorf("expected annotation %q, got %q", expectedAnnotation, actual)
				}
			},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			sa := buildServiceAccount(tc.agent)
			tc.verify(t, sa)
		})
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

func TestBuildDeployment(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "my-agent",
			Namespace: "my-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Deployment: &agentv1alpha1.DeploymentSpec{
				Image:           "gcr.io/my-proj/agent",
				Tag:             ptr.To("v1.0.0"),
				ImagePullPolicy: ptr.To(corev1.PullAlways),
				BrowserArgs:     []string{"--no-sandbox", "--disable-gpu"},
			},
			Security: &agentv1alpha1.SecuritySpec{
				ServiceAccountName: "custom-sa",
			},
			Harness: &agentv1alpha1.PlatformAgentHarnessSpec{
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

			Integration: &agentv1alpha1.IntegrationSpec{
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

	dep := buildDeployment(agent, "abcd1234", "efgh5678")

	if dep.Name != "my-agent-gateway" {
		t.Errorf("expected deployment name my-agent-gateway, got %s", dep.Name)
	}

	if dep.Spec.Template.Annotations["kubeagents.x-k8s.io/config-hash"] != "abcd1234" {
		t.Errorf("expected config-hash annotation to be abcd1234, got %s", dep.Spec.Template.Annotations["kubeagents.x-k8s.io/config-hash"])
	}

	if dep.Spec.Template.Annotations["kubeagents.x-k8s.io/fluent-bit-config-hash"] != "efgh5678" {
		t.Errorf("expected fluent-bit-config-hash annotation to be efgh5678, got %s", dep.Spec.Template.Annotations["kubeagents.x-k8s.io/fluent-bit-config-hash"])
	}

	if len(dep.Spec.Template.Spec.Containers) != 2 {
		t.Errorf("expected 2 containers, got %d", len(dep.Spec.Template.Spec.Containers))
	}

	container := dep.Spec.Template.Spec.Containers[0]
	if container.Image != "gcr.io/my-proj/agent:v1.0.0" {
		t.Errorf("expected container image gcr.io/my-proj/agent:v1.0.0, got %s", container.Image)
	}

	// Verify env vars
	envMap := make(map[string]corev1.EnvVar)
	for _, env := range container.Env {
		envMap[env.Name] = env
	}

	if envMap["PLATFORM_AGENT_HOME"].Value != "/var/agent" {
		t.Errorf("expected PLATFORM_AGENT_HOME /var/agent, got %s", envMap["PLATFORM_AGENT_HOME"].Value)
	}
	if envMap["PLATFORM_AGENT_DASHBOARD"].Value != "1" {
		t.Errorf("expected PLATFORM_AGENT_DASHBOARD 1, got %s", envMap["PLATFORM_AGENT_DASHBOARD"].Value)
	}
	if envMap["PLATFORM_AGENT_PLUGINS_DEBUG"].Value != "0" {
		t.Errorf("expected PLATFORM_AGENT_PLUGINS_DEBUG 0, got %s", envMap["PLATFORM_AGENT_PLUGINS_DEBUG"].Value)
	}
	if envMap["AGENT_BROWSER_ARGS"].Value != "--no-sandbox --disable-gpu" {
		t.Errorf("expected AGENT_BROWSER_ARGS --no-sandbox --disable-gpu, got %s", envMap["AGENT_BROWSER_ARGS"].Value)
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
}

func TestBuildDeploymentGoogleChatAllowedUsersEmpty(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "my-agent",
			Namespace: "my-ns",
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Deployment: &agentv1alpha1.DeploymentSpec{
				Image: "gcr.io/my-proj/agent",
			},
			Integration: &agentv1alpha1.IntegrationSpec{
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

	dep := buildDeployment(agent, "abcd1234", "efgh5678")
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
