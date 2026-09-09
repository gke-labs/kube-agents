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
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// agentContainerNameForTest is the sandbox container, as buildPodTemplateSpec
// spells it. Declared here rather than exported from the manifests file: the
// name is a literal there and lifting it is a change to production code for a
// test's benefit.
const agentContainerNameForTest = "platform-agent"

// forgeAgent is the smallest PlatformAgent that renders a credential proxy env.
func forgeAgent(integration *agentv1alpha1.PlatformAgentIntegrationSpec) *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "agent", Namespace: "kubeagents-system"},
		Spec:       agentv1alpha1.PlatformAgentSpec{Integration: integration},
	}
}

func TestTheConfiguredForgeReachesTheSidecar(t *testing.T) {
	cases := map[string]struct {
		integration *agentv1alpha1.PlatformAgentIntegrationSpec
		want        string
	}{
		"no integration at all": {nil, agentv1alpha1.DefaultGitProvider},
		"git with no provider": {&agentv1alpha1.PlatformAgentIntegrationSpec{
			Git: &agentv1alpha1.GitSpec{Repository: "acme/infra"},
		}, agentv1alpha1.DefaultGitProvider},
		"git naming github": {&agentv1alpha1.PlatformAgentIntegrationSpec{
			Git: &agentv1alpha1.GitSpec{Provider: "github", Repository: "acme/infra"},
		}, agentv1alpha1.GitProviderGitHub},
		// The deprecated alias means GitHub and nothing else, so it must render
		// the same value the explicit spelling does. A blank here would strip
		// `gh` from every install that has not migrated off it.
		"the deprecated github alias": {&agentv1alpha1.PlatformAgentIntegrationSpec{
			GitHub: &agentv1alpha1.GitHubSpec{Org: "acme", GitRepo: "acme/infra"},
		}, agentv1alpha1.GitProviderGitHub},
		// Admission refuses both of these, so neither should reach a render.
		// Rendering nothing when one does would turn a validation fault into a
		// credential outage: the sidecar would come up without `gh`.
		"both spellings set": {&agentv1alpha1.PlatformAgentIntegrationSpec{
			Git:    &agentv1alpha1.GitSpec{Repository: "acme/infra"},
			GitHub: &agentv1alpha1.GitHubSpec{Org: "acme"},
		}, agentv1alpha1.DefaultGitProvider},
		"an unregistered provider": {&agentv1alpha1.PlatformAgentIntegrationSpec{
			Git: &agentv1alpha1.GitSpec{Provider: "nonesuch", Repository: "acme/infra"},
		}, agentv1alpha1.DefaultGitProvider},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			envVars := buildCredentialProxyEnv(forgeAgent(tc.integration))
			value, count := envValueCount(envVars, credentialProxyForgeProvidersEnv)
			if count != 1 {
				t.Fatalf("%s appears %d times, want exactly 1", credentialProxyForgeProvidersEnv, count)
			}
			if value != tc.want {
				t.Errorf("%s = %q, want %q", credentialProxyForgeProvidersEnv, value, tc.want)
			}
		})
	}
}

func TestTheSandboxShimIsGivenTheSameForgeListAsTheSidecar(t *testing.T) {
	// `credential_proxy_client.py` runs in the agent container and derives the
	// executables it will forward from this variable, exactly as the sidecar
	// derives the ones it will run. Sharing `forge_clis.py` closes the gap only
	// if the two also share the input: rendered into the sidecar alone, the shim
	// read an unset variable and silently fell back to the GitHub set — on a
	// non-GitHub install, refusing the tool that install is entitled to while
	// offering one it is not.
	agent := forgeAgent(&agentv1alpha1.PlatformAgentIntegrationSpec{
		Git: &agentv1alpha1.GitSpec{Provider: "github", Repository: "acme/infra"},
	})
	pod := buildPodTemplateSpec(agent, "", "", "", "", nil, renderOptions{})

	var sandbox *corev1.Container
	for i := range pod.Spec.Containers {
		if pod.Spec.Containers[i].Name == agentContainerNameForTest {
			sandbox = &pod.Spec.Containers[i]
		}
	}
	if sandbox == nil {
		t.Fatalf("no %q container in the rendered Pod", agentContainerNameForTest)
	}

	sandboxValue, count := envValueCount(sandbox.Env, credentialProxyForgeProvidersEnv)
	if count != 1 {
		t.Fatalf("%s appears %d times on the agent container, want exactly 1",
			credentialProxyForgeProvidersEnv, count)
	}
	sidecarValue, _ := envValueCount(buildCredentialProxyEnv(agent), credentialProxyForgeProvidersEnv)
	if sandboxValue != sidecarValue {
		t.Errorf("the shim is told %q and the enforcer %q; they must agree",
			sandboxValue, sidecarValue)
	}
}

func TestAPluginCannotChooseWhichForgeToolsTheSidecarRuns(t *testing.T) {
	// This is the variable that selects the sidecar's executable allowlist, so
	// a spec.deployment.env entry that survived the merge would hand a plugin a
	// forge CLI the install was never provisioned for.
	//
	// Called with an empty managed list on purpose. The variable is in `managed`
	// on every render, so the loop in mergeCredentialProxyEnv already reserves
	// it and an end-to-end assertion would pass with the explicit entry deleted
	// — leaving the entry looking load-bearing while nothing checks it. This
	// shape is the only one in which the explicit entry is the thing under test.
	merged := mergeCredentialProxyEnv(nil, []corev1.EnvVar{
		{Name: credentialProxyForgeProvidersEnv, Value: "github,gitlab"},
		{Name: "HARMLESS_PLUGIN_SETTING", Value: "kept"},
	})
	if _, count := envValueCount(merged, credentialProxyForgeProvidersEnv); count != 0 {
		t.Errorf("%s survived the merge from spec.deployment.env", credentialProxyForgeProvidersEnv)
	}
	if value, count := envValueCount(merged, "HARMLESS_PLUGIN_SETTING"); count != 1 || value != "kept" {
		t.Errorf("the merge dropped an unrelated plugin variable; the reserved list is too wide")
	}
}

func TestEveryRegisteredProviderIsNamedByItsProviderNameNotItsBinary(t *testing.T) {
	// The operator configures provider names and the sidecar holds the
	// name-to-executable table. If a provider's rendered name were its binary,
	// the sidecar would look it up in that table, miss, and start without the
	// tool — a GitHub install with no `gh` and no error saying why.
	for _, name := range agentv1alpha1.GitProviderNames() {
		provider, err := agentv1alpha1.LookupGitProvider(name)
		if err != nil {
			t.Fatalf("LookupGitProvider(%q): %v", name, err)
		}
		if provider.Name != name {
			t.Errorf("provider registered under %q calls itself %q", name, provider.Name)
		}
		if provider.CLI == name {
			t.Errorf("provider %q shares its name with its binary; the rendered value "+
				"would be ambiguous between a selector and an executable", name)
		}
	}
}
