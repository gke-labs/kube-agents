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
	"context"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// spec.integration.baseBranch reaches the credential broker, which enforces
// it, and nothing else. These tests read the rendered env of every container
// an agent runs, because the property is as much about where the base is
// absent as where it is present: a copy in the sandbox would be a value the
// agent could be told to trust.

func baseBranchAgent(integration agentv1alpha1.IntegrationSpec) *agentv1alpha1.PlatformAgent {
	agent := brokerPodAgent()
	agent.Spec.Integration.IntegrationSpec = integration
	return agent
}

func gitopsIntegration(baseBranch string) agentv1alpha1.IntegrationSpec {
	return agentv1alpha1.IntegrationSpec{
		Forges:       []agentv1alpha1.ForgeSpec{{Name: "github", Namespace: "gke-labs"}},
		Repositories: []agentv1alpha1.RepositorySpec{{Forge: "github", Repository: "infra", Role: agentv1alpha1.RepositoryRoleGitOps}},
		BaseBranch:   baseBranch,
	}
}

// pinningEnvAttempts is spec.deployment.env trying to choose the base and the
// repository it pins.
var pinningEnvAttempts = []corev1.EnvVar{
	{Name: "CREDENTIAL_PROXY_BASE_BRANCH", Value: "attacker"},
	{Name: "CREDENTIAL_PROXY_BASE_REPOSITORY", Value: "attacker/repo"},
}

func TestTheBaseBranchIsRenderedIntoTheBrokerEnv(t *testing.T) {
	for name, integration := range map[string]agentv1alpha1.IntegrationSpec{
		"lists": gitopsIntegration("release"),
		// The deprecated alias declares the GitOps repository too.
		"alias": {
			GitHub:     &agentv1alpha1.GitHubSpec{Org: "gke-labs", GitRepo: "https://github.com/gke-labs/infra.git"},
			BaseBranch: "release",
		},
	} {
		t.Run(name, func(t *testing.T) {
			envVars := buildCredentialProxyEnv(baseBranchAgent(integration))
			for env, want := range map[string]string{
				"CREDENTIAL_PROXY_BASE_BRANCH":     "release",
				"CREDENTIAL_PROXY_BASE_REPOSITORY": "gke-labs/infra",
			} {
				if value, count := envValueCount(envVars, env); count != 1 || value != want {
					t.Errorf("%s = %q (x%d), want exactly one %q", env, value, count, want)
				}
			}
		})
	}
}

func TestNoBaseIsRenderedWithoutAnAcceptedGitOpsRepository(t *testing.T) {
	gh := []agentv1alpha1.ForgeSpec{{Name: "github", Namespace: "gke-labs"}}
	for name, integration := range map[string]agentv1alpha1.IntegrationSpec{
		"unset": gitopsIntegration(""),
		"only a managed repository": {Forges: gh, BaseBranch: "release",
			Repositories: []agentv1alpha1.RepositorySpec{{Forge: "github", Repository: "apps", Role: agentv1alpha1.RepositoryRoleManaged}}},
		"nothing else declared": {BaseBranch: "release"},
		"a refused gitops repository": {Forges: gh, BaseBranch: "release",
			Repositories: []agentv1alpha1.RepositorySpec{{Forge: "github", Repository: "group/subgroup/project", Role: agentv1alpha1.RepositoryRoleGitOps}}},
	} {
		t.Run(name, func(t *testing.T) {
			envVars := buildCredentialProxyEnv(baseBranchAgent(integration))
			for _, env := range pinningEnvAttempts {
				if value, count := envValueCount(envVars, env.Name); count != 0 {
					t.Errorf("%s = %q (x%d), want it absent", env.Name, value, count)
				}
			}
		})
	}
}

// With the field set, the operator's base is managed and wins over a
// spec.deployment.env entry. With it unset, a user CREDENTIAL_PROXY_BASE_BRANCH
// passes through, as GITOPS_BASE_BRANCH does: the broker reads either as a
// protected branch only, because enforcing a base also takes
// CREDENTIAL_PROXY_BASE_REPOSITORY, which spec.deployment.env never sets.
func TestDeploymentEnvCannotChooseTheBase(t *testing.T) {
	for name, tc := range map[string]struct {
		baseBranch string
		want       map[string]string
	}{
		"field set": {"release", map[string]string{
			"CREDENTIAL_PROXY_BASE_BRANCH":     "release",
			"CREDENTIAL_PROXY_BASE_REPOSITORY": "gke-labs/infra",
		}},
		"field unset": {"", map[string]string{
			"CREDENTIAL_PROXY_BASE_BRANCH": "attacker",
		}},
	} {
		t.Run(name, func(t *testing.T) {
			agent := baseBranchAgent(gitopsIntegration(tc.baseBranch))
			agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Env: append([]corev1.EnvVar{
				// Legacy: passed through unchanged. The broker reads it only
				// while CREDENTIAL_PROXY_BASE_BRANCH is unset.
				{Name: "GITOPS_BASE_BRANCH", Value: "legacy"},
			}, pinningEnvAttempts...)}
			envVars := buildCredentialProxyEnv(agent)
			for _, env := range pinningEnvAttempts {
				value, count := envValueCount(envVars, env.Name)
				want, rendered := tc.want[env.Name]
				if rendered && (count != 1 || value != want) {
					t.Errorf("%s = %q (x%d), want exactly one %q", env.Name, value, count, want)
				}
				if !rendered && count != 0 {
					t.Errorf("%s = %q survived from spec.deployment.env", env.Name, value)
				}
			}
			if value, count := envValueCount(envVars, "GITOPS_BASE_BRANCH"); count != 1 || value != "legacy" {
				t.Errorf("GITOPS_BASE_BRANCH = %q (x%d), want the CR's value passed through once", value, count)
			}
		})
	}
}

// Called with an empty managed list, as for the scoped-SA pool variables: the
// explicit entry is what reserves the repository on an install with no base,
// and the branch is left to pass through.
func TestTheReservedListNamesTheBaseRepositoryOnly(t *testing.T) {
	merged := mergeCredentialProxyEnv(nil, pinningEnvAttempts)
	if value, count := envValueCount(merged, "CREDENTIAL_PROXY_BASE_REPOSITORY"); count != 0 {
		t.Errorf("CREDENTIAL_PROXY_BASE_REPOSITORY = %q survived the merge from spec.deployment.env", value)
	}
	if value, count := envValueCount(merged, "CREDENTIAL_PROXY_BASE_BRANCH"); count != 1 || value != "attacker" {
		t.Errorf("CREDENTIAL_PROXY_BASE_BRANCH = %q (x%d), want the CR's value passed through once", value, count)
	}
}

func TestTheAgentAndTheSandboxGetNoBase(t *testing.T) {
	agent := baseBranchAgent(gitopsIntegration("release"))
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Env: pinningEnvAttempts}

	agentSpec := buildPodTemplateSpec(agent, "c", "f", "s", "p", nil, renderOptions{imageVolumeSupported: true}).Spec
	shellSpec := buildShellSandboxStatefulSet(agent, "keys", "http://broker", "s").Spec.Template.Spec
	for pod, spec := range map[string]corev1.PodSpec{"agent": agentSpec, "shell sandbox": shellSpec} {
		for _, container := range append(append([]corev1.Container{}, spec.Containers...), spec.InitContainers...) {
			// A spec.deployment.env CREDENTIAL_PROXY_BASE_BRANCH reaches
			// agent-api-auth as any unreserved name does; what must not reach
			// any container here is the operator's base or the repository.
			if value, count := envValueCount(container.Env, "CREDENTIAL_PROXY_BASE_BRANCH"); count > 1 || (count == 1 && value != "attacker") {
				t.Errorf("%s container %q carries CREDENTIAL_PROXY_BASE_BRANCH %d time(s), last %q", pod, container.Name, count, value)
			}
			if value, count := envValueCount(container.Env, "CREDENTIAL_PROXY_BASE_REPOSITORY"); count != 0 {
				t.Errorf("%s container %q carries CREDENTIAL_PROXY_BASE_REPOSITORY=%q", pod, container.Name, value)
			}
		}
	}
}

// TestChangingTheBaseRollsOnlyTheBroker reconciles an agent, sets the base,
// and reconciles again. Only the broker's pod template may change: the base
// is rendered nowhere else, and a hash over the integration in another
// template would restart the agent for a setting it never reads.
func TestChangingTheBaseRollsOnlyTheBroker(t *testing.T) {
	agent := baseBranchAgent(gitopsIntegration(""))
	r, cl := newSplitReconciler(t, agent)
	ctx := context.Background()
	key := types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}

	templates := func() map[string]corev1.PodTemplateSpec {
		t.Helper()
		if _, err := r.Reconcile(ctx, ctrl.Request{NamespacedName: key}); err != nil {
			t.Fatalf("Reconcile: %v", err)
		}
		out := map[string]corev1.PodTemplateSpec{}
		deployments := &appsv1.DeploymentList{}
		if err := cl.List(ctx, deployments, client.InNamespace(agent.Namespace)); err != nil {
			t.Fatalf("listing Deployments: %v", err)
		}
		for _, d := range deployments.Items {
			out["Deployment/"+d.Name] = d.Spec.Template
		}
		statefulSets := &appsv1.StatefulSetList{}
		if err := cl.List(ctx, statefulSets, client.InNamespace(agent.Namespace)); err != nil {
			t.Fatalf("listing StatefulSets: %v", err)
		}
		for _, s := range statefulSets.Items {
			out["StatefulSet/"+s.Name] = s.Spec.Template
		}
		return out
	}

	before := templates()
	current := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, key, current); err != nil {
		t.Fatalf("getting the agent: %v", err)
	}
	current.Spec.Integration.BaseBranch = "release"
	if err := cl.Update(ctx, current); err != nil {
		t.Fatalf("setting the base: %v", err)
	}
	after := templates()

	broker := "Deployment/" + credentialBrokerName(agent)
	shell := "StatefulSet/" + shellSandboxName(agent)
	// The broker, the shell sandbox, and the agent's own workload at least.
	if _, ok := before[broker]; !ok || len(before) < 3 {
		t.Fatalf("rendered %d workloads, want the broker, the shell sandbox and the agent: %v", len(before), before)
	}
	if _, ok := before[shell]; !ok {
		t.Fatalf("no %s rendered", shell)
	}
	for workload, template := range before {
		changed := !equality.Semantic.DeepEqual(template, after[workload])
		if workload == broker && !changed {
			t.Errorf("setting the base left the broker's pod template unchanged")
		}
		if workload != broker && changed {
			t.Errorf("setting the base changed the pod template of %s", workload)
		}
	}
	if len(after) != len(before) {
		t.Errorf("setting the base changed the set of workloads: %d before, %d after", len(before), len(after))
	}
}
