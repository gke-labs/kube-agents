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
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// spec.integration.repositories[].baseBranch reaches the credential broker,
// which enforces it, and nothing else. These tests read the rendered env of
// every container an agent runs, because the property is as much about where
// the bases are absent as where they are present: a copy in the sandbox would
// be a value the agent could be told to trust.

func baseBranchAgent(integration agentv1alpha1.IntegrationSpec) *agentv1alpha1.PlatformAgent {
	agent := brokerPodAgent()
	agent.Spec.Integration.IntegrationSpec = integration
	return agent
}

var baseBranchForges = []agentv1alpha1.ForgeSpec{{Name: "github", Namespace: "gke-labs"}}

func gitopsIntegration(baseBranch string) agentv1alpha1.IntegrationSpec {
	return agentv1alpha1.IntegrationSpec{
		Forges: baseBranchForges,
		Repositories: []agentv1alpha1.RepositorySpec{
			{Forge: "github", Repository: "infra", Role: agentv1alpha1.RepositoryRoleGitOps, BaseBranch: baseBranch},
		},
	}
}

// pinningEnvAttempts is spec.deployment.env trying to choose the bases, and
// the variables an earlier render carried them in.
var pinningEnvAttempts = []corev1.EnvVar{
	{Name: "CREDENTIAL_PROXY_PINNED_BASES", Value: `[{"repository":"https://github.com/attacker/repo","branch":"attacker"}]`},
	{Name: "CREDENTIAL_PROXY_BASE_BRANCH", Value: "attacker"},
}

func TestEveryBaseIsRenderedIntoTheBrokerEnv(t *testing.T) {
	integration := agentv1alpha1.IntegrationSpec{
		Forges: baseBranchForges,
		Repositories: []agentv1alpha1.RepositorySpec{
			{Forge: "github", Repository: "infra", Role: agentv1alpha1.RepositoryRoleGitOps, BaseBranch: "release"},
			// A remote is named by its URL on the forge's canonical host.
			{Forge: "github", Repository: "git@www.github.com:other-org/tools.git", Role: agentv1alpha1.RepositoryRoleManaged, BaseBranch: "refs/heads/main"},
			{Forge: "github", Repository: "apps", Role: agentv1alpha1.RepositoryRoleManaged, BaseBranch: "develop"},
			{Forge: "github", Repository: "unpinned", Role: agentv1alpha1.RepositoryRoleManaged},
			// The schema refuses a context base; built in Go, it is ignored.
			{Forge: "github", Repository: "reference", Role: agentv1alpha1.RepositoryRoleContext, BaseBranch: "main"},
		},
	}
	envVars := buildCredentialProxyEnv(baseBranchAgent(integration))
	// Sorted by repository, the branch as written.
	want := `[{"repository":"https://github.com/gke-labs/apps","branch":"develop"},` +
		`{"repository":"https://github.com/gke-labs/infra","branch":"release"},` +
		`{"repository":"https://github.com/other-org/tools","branch":"refs/heads/main"}]`
	if value, count := envValueCount(envVars, "CREDENTIAL_PROXY_PINNED_BASES"); count != 1 || value != want {
		t.Errorf("CREDENTIAL_PROXY_PINNED_BASES = %q (x%d), want exactly one %q", value, count, want)
	}
	// The operator renders the bases in the list and nowhere else.
	for _, env := range []string{"CREDENTIAL_PROXY_BASE_BRANCH", "CREDENTIAL_PROXY_BASE_REPOSITORY"} {
		if value, count := envValueCount(envVars, env); count != 0 {
			t.Errorf("%s = %q (x%d), want it absent", env, value, count)
		}
	}
}

func TestNoBaseIsRenderedWithoutAnAcceptedRepositoryThatSetsOne(t *testing.T) {
	for name, integration := range map[string]agentv1alpha1.IntegrationSpec{
		"unset": gitopsIntegration(""),
		// The deprecated alias has no place for a base.
		"alias": {GitHub: &agentv1alpha1.GitHubSpec{Org: "gke-labs", GitRepo: "https://github.com/gke-labs/infra.git"}},
		"only a context repository": {Forges: baseBranchForges, Repositories: []agentv1alpha1.RepositorySpec{
			{Forge: "github", Repository: "reference", Role: agentv1alpha1.RepositoryRoleContext, BaseBranch: "main"}}},
		"a refused gitops repository": {Forges: baseBranchForges, Repositories: []agentv1alpha1.RepositorySpec{
			{Forge: "github", Repository: "group/subgroup/project", Role: agentv1alpha1.RepositoryRoleGitOps, BaseBranch: "release"}}},
	} {
		t.Run(name, func(t *testing.T) {
			envVars := buildCredentialProxyEnv(baseBranchAgent(integration))
			if value, count := envValueCount(envVars, "CREDENTIAL_PROXY_PINNED_BASES"); count != 0 {
				t.Errorf("CREDENTIAL_PROXY_PINNED_BASES = %q (x%d), want it absent", value, count)
			}
		})
	}
}

// The broker refuses to start on a pin its parser cannot read, so the
// operator must never render one. A name over GitHub's 100 characters (whose
// URL can exceed the parser's 256), and a name that still ends in .git once
// one is dropped (which the broker reads as the repository without it, here a
// second pin on gke-labs/foo), are refused against the entry instead: the pin
// is withheld and the refusal named, rather than a crash-looping broker under
// a Ready CR.
func TestARepositoryTheBrokerCannotReadIsNotPinnedButReported(t *testing.T) {
	for name, tc := range map[string]struct {
		repositories []agentv1alpha1.RepositorySpec
		want         string
		fields       string
	}{
		"a name over github's limit": {
			repositories: []agentv1alpha1.RepositorySpec{
				{Forge: "github", Repository: strings.Repeat("a", agentv1alpha1.MaxGitHubRepoNameLength+1), Role: agentv1alpha1.RepositoryRoleGitOps, BaseBranch: "release"},
				{Forge: "github", Repository: strings.Repeat("b", agentv1alpha1.MaxGitHubRepoNameLength), Role: agentv1alpha1.RepositoryRoleManaged, BaseBranch: "main"},
			},
			want:   `[{"repository":"https://github.com/gke-labs/` + strings.Repeat("b", agentv1alpha1.MaxGitHubRepoNameLength) + `","branch":"main"}]`,
			fields: "integration.repositories[0].repository",
		},
		"a name that still ends in .git": {
			repositories: []agentv1alpha1.RepositorySpec{
				{Forge: "github", Repository: "foo", Role: agentv1alpha1.RepositoryRoleGitOps, BaseBranch: "release"},
				{Forge: "github", Repository: "foo.git.git", Role: agentv1alpha1.RepositoryRoleManaged, BaseBranch: "main"},
			},
			want:   `[{"repository":"https://github.com/gke-labs/foo","branch":"release"}]`,
			fields: "integration.repositories[1].repository",
		},
	} {
		t.Run(name, func(t *testing.T) {
			integration := agentv1alpha1.IntegrationSpec{Forges: baseBranchForges, Repositories: tc.repositories}
			envVars := buildCredentialProxyEnv(baseBranchAgent(integration))
			if value, count := envValueCount(envVars, "CREDENTIAL_PROXY_PINNED_BASES"); count != 1 || value != tc.want {
				t.Errorf("CREDENTIAL_PROXY_PINNED_BASES = %q (x%d), want exactly one %q", value, count, tc.want)
			}
			// ValidateGit is what reconcile reads to go Degraded and the
			// webhook's Problems what it names; gitProblemFields names the fields.
			if err := integration.ValidateGit(); err == nil {
				t.Fatalf("ValidateGit() = nil, want the repository refused")
			}
			if fields := gitProblemFields(&integration); fields != tc.fields {
				t.Errorf("gitProblemFields() = %q, want %q", fields, tc.fields)
			}
		})
	}
}

// With a base set, the operator's list is managed and wins over a
// spec.deployment.env entry; with none, the reserved list drops the entry.
// CREDENTIAL_PROXY_BASE_BRANCH, like GITOPS_BASE_BRANCH, passes through either
// way: the broker reads it as a protected branch only.
func TestDeploymentEnvCannotChooseTheBases(t *testing.T) {
	for name, tc := range map[string]struct {
		baseBranch string
		want       map[string]string
	}{
		"base set": {"release", map[string]string{
			"CREDENTIAL_PROXY_PINNED_BASES": `[{"repository":"https://github.com/gke-labs/infra","branch":"release"}]`,
			"CREDENTIAL_PROXY_BASE_BRANCH":  "attacker",
		}},
		"base unset": {"", map[string]string{
			"CREDENTIAL_PROXY_BASE_BRANCH": "attacker",
		}},
	} {
		t.Run(name, func(t *testing.T) {
			agent := baseBranchAgent(gitopsIntegration(tc.baseBranch))
			agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Env: append([]corev1.EnvVar{
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
// explicit entry is what reserves the list on an install with no base.
func TestTheReservedListNamesThePinnedBases(t *testing.T) {
	merged := mergeCredentialProxyEnv(nil, pinningEnvAttempts)
	if value, count := envValueCount(merged, "CREDENTIAL_PROXY_PINNED_BASES"); count != 0 {
		t.Errorf("CREDENTIAL_PROXY_PINNED_BASES = %q survived the merge from spec.deployment.env", value)
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
			if value, count := envValueCount(container.Env, "CREDENTIAL_PROXY_PINNED_BASES"); count != 0 {
				t.Errorf("%s container %q carries CREDENTIAL_PROXY_PINNED_BASES=%q", pod, container.Name, value)
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
	current.Spec.Integration.Repositories[0].BaseBranch = "release"
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
