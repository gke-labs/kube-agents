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
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// gitlabAgent is the broker-pod agent with a GitHub forge and a GitLab one.
func gitlabAgent(secret string) *agentv1alpha1.PlatformAgent {
	agent := brokerPodAgent()
	gitlab := agentv1alpha1.ForgeSpec{Name: "gitlab", Provider: "gitlab", Namespace: "acme"}
	if secret != "" {
		gitlab.CredentialsRef = &agentv1alpha1.ForgeCredentialsRef{Name: secret}
	}
	agent.Spec.Integration.Forges = []agentv1alpha1.ForgeSpec{{Name: "github", Namespace: "acme"}, gitlab}
	agent.Spec.Integration.Repositories = []agentv1alpha1.RepositorySpec{
		{Forge: "github", Repository: "infra", Role: agentv1alpha1.RepositoryRoleGitOps},
		{Forge: "gitlab", Repository: "platform/tools", Role: agentv1alpha1.RepositoryRoleManaged},
	}
	return agent
}

// A GitHub-only install renders none of it: no key, no variable, no mount and
// no volume, so its broker is what it was before GitLab existed.
func TestAGitHubOnlyBrokerRendersNoForgeConfiguration(t *testing.T) {
	for name, agent := range map[string]*agentv1alpha1.PlatformAgent{
		"no integration":        brokerPodAgent(),
		"gitlab with no secret": gitlabAgent(""),
		"github forge list alone": func() *agentv1alpha1.PlatformAgent {
			a := gitlabAgent("x")
			a.Spec.Integration.Forges = a.Spec.Integration.Forges[:1]
			a.Spec.Integration.Repositories = a.Spec.Integration.Repositories[:1]
			return a
		}(),
	} {
		t.Run(name, func(t *testing.T) {
			if _, found := buildCredentialProxyPolicyConfigMap(agent).Data[vcsForgesKey]; found {
				t.Error("the policy ConfigMap carries a forge configuration")
			}
			dep := buildCredentialProxyDeployment(agent, "hash")
			container := dep.Spec.Template.Spec.Containers[0]
			if _, found := brokerEnvValue(container.Env, vcsForgesEnv); found {
				t.Errorf("%s is set", vcsForgesEnv)
			}
			for _, mount := range container.VolumeMounts {
				if mount.SubPath == vcsForgesKey || strings.HasPrefix(mount.Name, forgeCredentialsVolumePrefix) {
					t.Errorf("unexpected forge mount %+v", mount)
				}
			}
			for _, volume := range dep.Spec.Template.Spec.Volumes {
				if strings.HasPrefix(volume.Name, forgeCredentialsVolumePrefix) {
					t.Errorf("unexpected forge volume %q", volume.Name)
				}
			}
		})
	}
}

func TestAGitLabForgeReachesTheBrokerAndOnlyTheBroker(t *testing.T) {
	agent := gitlabAgent("gitlab-forge-token")

	// The configuration the broker's registry reads, GitHub first. Byte for
	// byte the golden file, which test_providers_registry_config.py also
	// builds the broker's registry from: one fixture, read by both sides, so
	// neither can change the shape alone.
	raw := buildCredentialProxyPolicyConfigMap(agent).Data[vcsForgesKey]
	golden, err := os.ReadFile("testdata/vcs-forges.golden.json")
	if err != nil {
		t.Fatal(err)
	}
	if raw != strings.TrimSpace(string(golden)) {
		t.Errorf("the rendered forge configuration is not testdata/vcs-forges.golden.json:\n%s", raw)
	}
	var document struct {
		Forges []map[string]any `json:"forges"`
	}
	if err := json.Unmarshal([]byte(raw), &document); err != nil {
		t.Fatalf("the forge configuration is not JSON: %v\n%s", err, raw)
	}
	want := []map[string]any{
		{"provider": "github", "host": "github.com"},
		{"provider": "gitlab", "host": "gitlab.com",
			"tokenPath":    forgeCredentialsDir + "/gitlab/token",
			"allowedPaths": []any{"acme", "platform"}},
	}
	if !reflect.DeepEqual(document.Forges, want) {
		t.Errorf("forges = %v, expected %v", document.Forges, want)
	}

	// The broker is pointed at it, and every path the configuration names is
	// supplied by a mount of a volume the pod declares.
	dep := buildCredentialProxyDeployment(agent, "hash")
	container := dep.Spec.Template.Spec.Containers[0]
	volumes := dep.Spec.Template.Spec.Volumes
	if value, _ := brokerEnvValue(container.Env, vcsForgesEnv); value != vcsForgesMountPath {
		t.Errorf("%s = %q, expected %q", vcsForgesEnv, value, vcsForgesMountPath)
	}
	for _, path := range []string{vcsForgesMountPath, forgeCredentialsDir + "/gitlab/token"} {
		mount := mountCovering(&container, path)
		if mount == nil {
			t.Errorf("%s is supplied by no mount in the broker container", path)
			continue
		}
		if !hasVolume(volumes, mount.Name) {
			t.Errorf("%s is mounted from %q, which the broker pod does not declare", path, mount.Name)
		}
	}
	var secret *corev1.SecretVolumeSource
	for _, volume := range volumes {
		if volume.Secret != nil && volume.Secret.SecretName == "gitlab-forge-token" {
			secret = volume.Secret
		}
	}
	if secret == nil {
		t.Fatal("the broker pod does not project the forge's Secret")
	}
	if len(secret.Items) != 1 || secret.Items[0].Key != "token" || secret.Optional == nil || !*secret.Optional {
		t.Errorf("the Secret projection is %+v; expected only the token key, optional", secret)
	}

	// And nothing else holds the Secret: not the gateway pod, not the sandbox.
	gateway := buildPodTemplateSpec(agent, "c", "f", "s", "p", nil, renderOptions{})
	sandbox := buildShellSandboxStatefulSet(agent, "keys", credentialProxyURL(agent), "s")
	for name, podVolumes := range map[string][]corev1.Volume{
		"gateway": gateway.Spec.Volumes,
		"sandbox": sandbox.Spec.Template.Spec.Volumes,
	} {
		for _, volume := range podVolumes {
			if volume.Secret != nil && volume.Secret.SecretName == "gitlab-forge-token" {
				t.Errorf("the %s pod mounts the forge's Secret as %q", name, volume.Name)
			}
		}
	}
}

// A CR env entry cannot point the broker at a configuration the operator did
// not render: not beside one the operator rendered, and not on a GitHub-only
// install where the operator renders none.
func TestTheForgeConfigurationVariableIsTheOperators(t *testing.T) {
	for name, tc := range map[string]struct {
		agent *agentv1alpha1.PlatformAgent
		want  []string
	}{
		"gitlab declared": {gitlabAgent("gitlab-forge-token"), []string{vcsForgesMountPath}},
		"github only":     {brokerPodAgent(), nil},
	} {
		t.Run(name, func(t *testing.T) {
			tc.agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
				Env: []corev1.EnvVar{{Name: vcsForgesEnv, Value: "/elsewhere.json"}},
			}
			container := buildCredentialProxyDeployment(tc.agent, "hash").Spec.Template.Spec.Containers[0]
			var got []string
			for _, env := range container.Env {
				if env.Name == vcsForgesEnv {
					got = append(got, env.Value)
				}
			}
			if !reflect.DeepEqual(got, tc.want) {
				t.Errorf("%s = %v, expected %v", vcsForgesEnv, got, tc.want)
			}
		})
	}
}

// Review: the forge configuration rode into the gateway's hash annotation too,
// so a GitLab change restarted the gateway. Only the broker reads it.
func TestTheForgeConfigurationRollsTheBrokerNotTheGateway(t *testing.T) {
	plain := buildCredentialProxyPolicyConfigMap(brokerPodAgent())
	if gatewayPolicyView(plain) != plain {
		t.Error("a GitHub-only ConfigMap is not its own gateway view, so the gateway hash would change")
	}
	withGitLab := buildCredentialProxyPolicyConfigMap(gitlabAgent("gitlab-forge-token"))
	gw, _ := getConfigMapHash(gatewayPolicyView(withGitLab))
	base, _ := getConfigMapHash(plain)
	broker, _ := getConfigMapHash(withGitLab)
	if gw != base {
		t.Error("declaring a GitLab forge changes the gateway's policy hash")
	}
	if broker == base {
		t.Error("declaring a GitLab forge does not change the broker's policy hash")
	}
}

// A forge's CA bundle is the Secret key caBundleRef names, mounted into the
// broker's pod only, at the path its configuration entry names. It comes from
// a Secret so that changing it needs the same rights as changing the token
// beside it; a ConfigMap of the same name is never read. Optional, so a missing
// Secret does not stop the broker, and not a SubPath, so kubelet's refresh of
// the Secret reaches the broker with no restart.
func TestAForgeCABundleIsMountedIntoTheBrokerOnly(t *testing.T) {
	agent := gitlabAgent("gitlab-forge-token")
	// A self-managed host: gitlab.com refuses caBundleRef.
	agent.Spec.Integration.Forges[1].Host = "gitlab.internal.example"
	agent.Spec.Integration.Forges[1].CABundleRef = &agentv1alpha1.ForgeCABundleRef{Name: "gitlab-forge-ca", Key: "root.pem"}

	var document struct {
		Forges []map[string]any `json:"forges"`
	}
	raw := buildCredentialProxyPolicyConfigMap(agent).Data[vcsForgesKey]
	if err := json.Unmarshal([]byte(raw), &document); err != nil {
		t.Fatalf("the forge configuration is not JSON: %v\n%s", err, raw)
	}
	caFile := forgeCADir + "/gitlab/" + agentv1alpha1.ForgeCABundleFileName
	if got := document.Forges[1]["caFile"]; got != caFile {
		t.Errorf("caFile = %v, expected %s", got, caFile)
	}
	if _, found := document.Forges[0]["caFile"]; found {
		t.Error("GitHub's entry carries a caFile")
	}

	dep := buildCredentialProxyDeployment(agent, "hash")
	container := dep.Spec.Template.Spec.Containers[0]
	mount := mountCovering(&container, caFile)
	if mount == nil {
		t.Fatalf("%s is supplied by no mount in the broker container", caFile)
	}
	if mount.SubPath != "" || !mount.ReadOnly {
		t.Errorf("the CA mount is %+v; expected a read-only directory mount", mount)
	}
	var source *corev1.SecretVolumeSource
	for _, volume := range dep.Spec.Template.Spec.Volumes {
		if volume.Name == mount.Name {
			source = volume.Secret
		}
		// A ConfigMap of the same name is not the CA: a ConfigMap write must
		// not choose the broker's trust anchor.
		if volume.ConfigMap != nil && volume.ConfigMap.Name == "gitlab-forge-ca" {
			t.Errorf("the broker pod reads a ConfigMap named like the CA Secret, as %q", volume.Name)
		}
	}
	if source == nil || source.SecretName != "gitlab-forge-ca" {
		t.Fatalf("the CA mount %q is not the Secret gitlab-forge-ca: %+v", mount.Name, source)
	}
	if len(source.Items) != 1 || source.Items[0].Key != "root.pem" || source.Items[0].Path != agentv1alpha1.ForgeCABundleFileName {
		t.Errorf("the Secret projection is %+v; expected only root.pem, as ca.crt", source.Items)
	}
	if source.Optional == nil || !*source.Optional {
		t.Error("the CA Secret projection is not optional, so a missing Secret would stop the broker")
	}

	gateway := buildPodTemplateSpec(agent, "c", "f", "s", "p", nil, renderOptions{})
	sandbox := buildShellSandboxStatefulSet(agent, "keys", credentialProxyURL(agent), "s")
	for name, podVolumes := range map[string][]corev1.Volume{
		"gateway": gateway.Spec.Volumes,
		"sandbox": sandbox.Spec.Template.Spec.Volumes,
	} {
		for _, volume := range podVolumes {
			if (volume.Secret != nil && volume.Secret.SecretName == "gitlab-forge-ca") ||
				(volume.ConfigMap != nil && volume.ConfigMap.Name == "gitlab-forge-ca") {
				t.Errorf("the %s pod mounts the forge's CA as %q", name, volume.Name)
			}
		}
	}
}
