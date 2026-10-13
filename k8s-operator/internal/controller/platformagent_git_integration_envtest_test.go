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

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// TestIntegrationSchemaRulesEnvtest pins the CEL rules on the forge declaration
// against a real API server: the two spellings are exclusive, a repository
// names a declared forge, and at most one repository is the GitOps one. The
// webhook refuses the same CRs, but the chart ships it off, so the schema is
// the refusal every install has; a dropped or mistyped marker would otherwise
// go unnoticed, because no unit test evaluates CEL.
func TestIntegrationSchemaRulesEnvtest(t *testing.T) {
	cl, _ := startEnvtest(t)
	ctx := context.Background()

	const namespace = "git-integration"
	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	newAgent := func(name string, integration agentv1alpha1.IntegrationSpec) *agentv1alpha1.PlatformAgent {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Harness: &agentv1alpha1.HarnessSpec{
					ProjectID:   envtestHarnessProject,
					Location:    envtestHarnessLocation,
					ClusterName: envtestHarnessCluster,
				},
			},
		}
		agent.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{IntegrationSpec: integration}
		return agent
	}

	gh := []agentv1alpha1.ForgeSpec{{Name: "github", Namespace: "gke-labs"}}
	refused := map[string]struct {
		integration agentv1alpha1.IntegrationSpec
		message     string
	}{
		"both": {agentv1alpha1.IntegrationSpec{
			Forges: gh,
			GitHub: &agentv1alpha1.GitHubSpec{GitRepo: "gke-labs/kube-agents"},
		}, "or integration.github, not both"},
		"dangling": {agentv1alpha1.IntegrationSpec{
			Forges: gh,
			Repositories: []agentv1alpha1.RepositorySpec{
				{Forge: "gitlab", Repository: "group/project", Role: "context"}},
		}, "must name a forge declared in integration.forges"},
		"no-forges": {agentv1alpha1.IntegrationSpec{
			Repositories: []agentv1alpha1.RepositorySpec{
				{Forge: "github", Repository: "gke-labs/kube-agents", Role: "gitops"}},
		}, "must name a forge declared in integration.forges"},
		"two-gitops": {agentv1alpha1.IntegrationSpec{
			Forges: gh,
			Repositories: []agentv1alpha1.RepositorySpec{
				{Forge: "github", Repository: "infra", Role: "gitops"},
				{Forge: "github", Repository: "infra2", Role: "gitops"}},
		}, "at most one repository may have role gitops"},
		// The API server's own rules on credentialsRef, which hold with the
		// webhook off -- the chart ships it off.
		"gitlab-no-secret": {agentv1alpha1.IntegrationSpec{
			Forges: []agentv1alpha1.ForgeSpec{{Name: "gl", Provider: "gitlab", Namespace: "acme"}},
		}, "a gitlab forge needs credentialsRef.name"},
		"bad-secret-name": {agentv1alpha1.IntegrationSpec{
			Forges: []agentv1alpha1.ForgeSpec{{Name: "gl", Provider: "gitlab", Namespace: "acme",
				CredentialsRef: &agentv1alpha1.ForgeCredentialsRef{Name: "Bad_Name"}}},
		}, "credentialsRef.name"},
		// caBundleRef is for a gitlab forge, and its names are the
		// API server's own, held with the webhook off.
		"github-ca-bundle": {agentv1alpha1.IntegrationSpec{
			Forges: []agentv1alpha1.ForgeSpec{{Name: "github", Namespace: "gke-labs",
				CABundleRef: &agentv1alpha1.ForgeCABundleRef{Name: "gh-ca"}}},
		}, "caBundleRef is for a gitlab forge"},
		"gitlab-com-ca": {agentv1alpha1.IntegrationSpec{
			Forges: []agentv1alpha1.ForgeSpec{{Name: "gl", Provider: "gitlab", Namespace: "acme",
				CredentialsRef: &agentv1alpha1.ForgeCredentialsRef{Name: "gl-token"},
				CABundleRef:    &agentv1alpha1.ForgeCABundleRef{Name: "gl-ca"}}},
		}, "caBundleRef is for a self-managed host"},
		"gitlab-com-named-ca": {agentv1alpha1.IntegrationSpec{
			Forges: []agentv1alpha1.ForgeSpec{{Name: "gl", Provider: "gitlab", Host: "WWW.GitLab.com", Namespace: "acme",
				CredentialsRef: &agentv1alpha1.ForgeCredentialsRef{Name: "gl-token"},
				CABundleRef:    &agentv1alpha1.ForgeCABundleRef{Name: "gl-ca"}}},
		}, "caBundleRef is for a self-managed host"},
		"bad-ca-name": {agentv1alpha1.IntegrationSpec{
			Forges: []agentv1alpha1.ForgeSpec{{Name: "gl", Provider: "gitlab", Host: "gitlab.example.com", Namespace: "acme",
				CredentialsRef: &agentv1alpha1.ForgeCredentialsRef{Name: "gl-token"},
				CABundleRef:    &agentv1alpha1.ForgeCABundleRef{Name: "Bad_Name"}}},
		}, "caBundleRef.name"},
		"dotdot-ca-key": {agentv1alpha1.IntegrationSpec{
			Forges: []agentv1alpha1.ForgeSpec{{Name: "gl", Provider: "gitlab", Host: "gitlab.example.com", Namespace: "acme",
				CredentialsRef: &agentv1alpha1.ForgeCredentialsRef{Name: "gl-token"},
				CABundleRef:    &agentv1alpha1.ForgeCABundleRef{Name: "gl-ca", Key: ".."}}},
		}, "caBundleRef.key may not be . or start with .."},
		"bad-ca-key": {agentv1alpha1.IntegrationSpec{
			Forges: []agentv1alpha1.ForgeSpec{{Name: "gl", Provider: "gitlab", Host: "gitlab.example.com", Namespace: "acme",
				CredentialsRef: &agentv1alpha1.ForgeCredentialsRef{Name: "gl-token"},
				CABundleRef:    &agentv1alpha1.ForgeCABundleRef{Name: "gl-ca", Key: "no spaces"}}},
		}, "caBundleRef.key"},
	}
	// baseBranch is held to the branch names the broker's
	// providers/validate.validate_branch accepts: the pattern and the length
	// for the characters and the leading one, the CEL rules for HEAD and the
	// sequences git refuses.
	gitops := func(base string) []agentv1alpha1.RepositorySpec {
		return []agentv1alpha1.RepositorySpec{{Forge: "github", Repository: "infra", Role: "gitops", BaseBranch: base}}
	}
	for name, tc := range map[string]struct{ base, message string }{
		"base-leading-dash":   {"-main", "spec.integration.repositories[0].baseBranch"},
		"base-space":          {"my branch", "spec.integration.repositories[0].baseBranch"},
		"base-leading-under":  {"_release", "spec.integration.repositories[0].baseBranch"},
		"base-at":             {"release@2026", "spec.integration.repositories[0].baseBranch"},
		"base-too-long":       {strings.Repeat("a", 201), "spec.integration.repositories[0].baseBranch"},
		"base-head":           {"HEAD", "may not be HEAD"},
		"base-dotdot":         {"a..b", "no '..'"},
		"base-slash-dot":      {"a/.b", "no '..'"},
		"base-double-slash":   {"a//b", "no '..'"},
		"base-lock-component": {"a.lock/b", "no '..'"},
		"base-lock":           {"main.lock", "may not end in"},
		"base-trailing-slash": {"main/", "may not end in"},
		"base-trailing-dot":   {"main.", "may not end in"},
		"base-ref-under":      {"refs/heads/_release", "after refs/heads/"},
		"base-ref-head":       {"refs/heads/HEAD", "after refs/heads/"},
		"base-ref-empty":      {"refs/heads/", "may not end in"},
		// One spelling per branch, as the broker reads it: the name, or
		// refs/heads/ and the name.
		"base-heads":        {"heads/x", "may not start with heads/"},
		"base-ref-ref":      {"refs/heads/refs/heads/x", "one refs/heads/ prefix"},
		"base-ref-ref-head": {"refs/heads/refs/heads/HEAD", "one refs/heads/ prefix"},
		"base-ref-heads":    {"refs/heads/heads/x", "one refs/heads/ prefix"},
	} {
		refused[name] = struct {
			integration agentv1alpha1.IntegrationSpec
			message     string
		}{agentv1alpha1.IntegrationSpec{Forges: gh, Repositories: gitops(tc.base)}, tc.message}
	}
	// A context repository is never written: its branch pin is the ref in the
	// gitops-state ConfigMap, so a base on it is refused.
	refused["base-context"] = struct {
		integration agentv1alpha1.IntegrationSpec
		message     string
	}{agentv1alpha1.IntegrationSpec{Forges: gh, Repositories: []agentv1alpha1.RepositorySpec{
		{Forge: "github", Repository: "kubernetes/kubernetes", Role: "context", BaseBranch: "main"}}},
		"baseBranch may not be set on a context repository"}
	for name, tc := range refused {
		err := cl.Create(ctx, newAgent(name, tc.integration))
		if !apierrors.IsInvalid(err) || !strings.Contains(err.Error(), tc.message) {
			t.Errorf("creating a PlatformAgent (%s) = %v, want the schema's Invalid refusal %q", name, err, tc.message)
		}
	}

	for name, integration := range map[string]agentv1alpha1.IntegrationSpec{
		"lists": {Forges: gh, Repositories: []agentv1alpha1.RepositorySpec{
			{Forge: "github", Repository: "infra", Role: "gitops"},
			{Forge: "github", Repository: "apps", Role: "managed"},
			{Forge: "github", Repository: "kubernetes/kubernetes", Role: "context"}}},
		"forge-only": {Forges: gh},
		"gitlab": {Forges: []agentv1alpha1.ForgeSpec{{Name: "gl", Provider: "gitlab", Namespace: "acme",
			CredentialsRef: &agentv1alpha1.ForgeCredentialsRef{Name: "gitlab-token"}}}},
		"gitlab-ca": {Forges: []agentv1alpha1.ForgeSpec{{Name: "gl", Provider: "gitlab", Host: "gitlab.example.com",
			Namespace:      "acme",
			CredentialsRef: &agentv1alpha1.ForgeCredentialsRef{Name: "gitlab-token"},
			CABundleRef:    &agentv1alpha1.ForgeCABundleRef{Name: "gitlab-forge-ca"}}}},
		"alias":    {GitHub: &agentv1alpha1.GitHubSpec{GitRepo: "gke-labs/kube-agents"}},
		"base":     {Forges: gh, Repositories: gitops("release/2026")},
		"base-max": {Forges: gh, Repositories: gitops(strings.Repeat("a", 200))},
		"base-ref": {Forges: gh, Repositories: gitops("refs/heads/main")},
		// A name that only contains heads/ or refs/heads/ is a branch name.
		"base-ref-nested":  {Forges: gh, Repositories: gitops("refs/heads/release/heads/x")},
		"base-heads-inner": {Forges: gh, Repositories: gitops("team/heads/x")},
		"base-managed": {Forges: gh, Repositories: []agentv1alpha1.RepositorySpec{
			{Forge: "github", Repository: "apps", Role: "managed", BaseBranch: "main"}}},
		"base-context-empty": {Forges: gh, Repositories: []agentv1alpha1.RepositorySpec{
			{Forge: "github", Repository: "kubernetes/kubernetes", Role: "context", BaseBranch: ""}}},
	} {
		if err := cl.Create(ctx, newAgent(name, integration)); err != nil {
			t.Errorf("creating a PlatformAgent (%s) = %v, want it admitted", name, err)
		}
	}
}
