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
	}
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
		"alias":      {GitHub: &agentv1alpha1.GitHubSpec{GitRepo: "gke-labs/kube-agents"}},
	} {
		if err := cl.Create(ctx, newAgent(name, integration)); err != nil {
			t.Errorf("creating a PlatformAgent (%s) = %v, want it admitted", name, err)
		}
	}
}
