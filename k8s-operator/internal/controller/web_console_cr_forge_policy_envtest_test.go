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
	"time"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	webConsoleForgePolicyNamespace = "web-console-forge-policy"
	// The forges the CR declares before each case, as JSON list items.
	forgeGitHub = `{"name":"github","namespace":"gke-labs"}`
	forgeGitLab = `{"name":"gl","provider":"gitlab","host":"gitlab.example.com","namespace":"acme","credentialsRef":{"name":"gl-token"},"caBundleRef":{"name":"gl-ca"}}`
	// Port and scheme are fields of a Gitea forge only, so that is where they are tried.
	forgeGitea     = `{"name":"gt","provider":"gitea","host":"gitea.gitea.svc.cluster.local","namespace":"acme","credentialsRef":{"name":"gt-token"}}`
	reposBothForge = `[{"forge":"github","repository":"gke-labs/kube-agents","role":"gitops"},` +
		`{"forge":"gl","repository":"acme/infra","role":"context"}]`
)

// TestTheWebConsoleCRPolicyHoldsWhereAForgeCredentialGoes runs the shipped
// policy's forge checks in a real API server. The console may carry a forge
// over, add one with no credential, change a forge's namespace when it has no
// credential, and remove one; it may not move a forge's credential to another
// destination, change the account it acts as (identity), add a forge with a
// credential, or move the install's GitHub host, where the broker spends the
// GitHub App token.
func TestTheWebConsoleCRPolicyHoldsWhereAForgeCredentialGoes(t *testing.T) {
	cfg, scheme := startEnvtestConfig(t)
	admin, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	applyAdmissionPolicyFile(ctx, t, admin, webConsoleCRPolicyFile)

	ns := webConsoleForgePolicyNamespace
	mustCreate(ctx, t, admin, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}})
	for _, sa := range []string{webConsoleCRPolicyConsole, "other-controller"} {
		mustCreate(ctx, t, admin, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: sa, Namespace: ns}})
	}
	mustCreate(ctx, t, admin, &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{Name: "cr-patch", Namespace: ns},
		Rules: []rbacv1.PolicyRule{{
			APIGroups: []string{platformAgentGVK.Group}, Resources: []string{"platformagents"},
			ResourceNames: []string{webConsoleCRPolicyAgent}, Verbs: []string{"get", "patch"},
		}},
	})
	mustCreate(ctx, t, admin, &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "cr-patch", Namespace: ns},
		RoleRef:    rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "Role", Name: "cr-patch"},
		Subjects: []rbacv1.Subject{
			{Kind: rbacv1.ServiceAccountKind, Name: webConsoleCRPolicyConsole, Namespace: ns},
			{Kind: rbacv1.ServiceAccountKind, Name: "other-controller", Namespace: ns},
		},
	})
	mustCreate(ctx, t, admin, &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: webConsoleCRPolicyAgent, Namespace: ns},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID: envtestHarnessProject, Location: envtestHarnessLocation, ClusterName: envtestHarnessCluster,
			},
		},
	})
	patch := func(c client.Client, body string) error {
		pa := &unstructured.Unstructured{}
		pa.SetGroupVersionKind(platformAgentGVK)
		pa.SetNamespace(ns)
		pa.SetName(webConsoleCRPolicyAgent)
		return c.Patch(ctx, pa, client.RawPatch(types.MergePatchType, []byte(body)))
	}
	integration := func(forges, repos string) string {
		return `{"spec":{"integration":{"forges":[` + forges + `],"repositories":` + repos + `}}}`
	}
	// Each case starts from the same declaration, set by the admin.
	reset := func() {
		t.Helper()
		if err := patch(admin, integration(forgeGitHub+","+forgeGitLab+","+forgeGitea, reposBothForge)); err != nil {
			t.Fatalf("reset the forges: %v", err)
		}
	}
	reset()

	console := impersonatedClient(t, cfg, ns, webConsoleCRPolicyConsole)
	hostMoved := integration(forgeGitHub+","+strings.Replace(forgeGitLab, "gitlab.example.com", "gitlab.attacker.example", 1)+","+forgeGitea, reposBothForge)
	// The policy is stored before it is enforced; wait until a moved host is refused.
	deadline := time.Now().Add(webConsoleCRPolicyActivation)
	for {
		err := patch(console, hostMoved)
		if apierrors.IsForbidden(err) || apierrors.IsInvalid(err) {
			break
		}
		if err == nil {
			reset()
		}
		if time.Now().After(deadline) {
			t.Fatalf("the policy was not enforced within %s (last patch: %v)", webConsoleCRPolicyActivation, err)
		}
		time.Sleep(time.Second)
	}

	// What the repositories page writes: each forge carried over from the CR
	// (repositories.go copies a same-name forge's fields), a forge it adds
	// with no credential, a namespace on a forge with none, a forge removed.
	for _, tc := range []struct{ name, body string }{
		{"the forges carried over, a repository added", integration(forgeGitHub+","+forgeGitLab+","+forgeGitea,
			`[{"forge":"github","repository":"gke-labs/kube-agents","role":"gitops"},`+
				`{"forge":"gl","repository":"acme/infra","role":"context"},`+
				`{"forge":"gl","repository":"acme/apps","role":"context"}]`)},
		{"the provider spelled out on a forge that left it out", integration(
			`{"name":"github","provider":"github","namespace":"gke-labs"},`+forgeGitLab+","+forgeGitea, reposBothForge)},
		{"a GitHub forge added with no credential", integration(
			forgeGitHub+","+forgeGitLab+","+forgeGitea+`,{"name":"gh-two","namespace":"kubernetes"}`, reposBothForge)},
		{"a namespace changed on a forge with no credential", integration(
			`{"name":"github","namespace":"kubernetes"},`+forgeGitLab+","+forgeGitea, reposBothForge)},
		{"a credentialed forge's host spelled in another case", integration(forgeGitHub+","+
			strings.Replace(forgeGitLab, "gitlab.example.com", "GitLab.Example.com", 1)+","+forgeGitea, reposBothForge)},
		{"the GitHub forge's host spelled out as www.github.com", integration(
			`{"name":"github","host":"www.github.com","namespace":"gke-labs"},`+forgeGitLab+","+forgeGitea, reposBothForge)},
		{"a forge removed", integration(forgeGitHub,
			`[{"forge":"github","repository":"gke-labs/kube-agents","role":"gitops"}]`)},
	} {
		reset()
		if err := patch(console, tc.body); err != nil {
			t.Errorf("%s: refused, want admitted: %v", tc.name, err)
		}
	}

	const (
		destinationRule = "may not change where an existing forge's credential goes"
		newForgeRule    = "a forge the web console adds may not carry a credentialsRef or a caBundleRef"
		gitHubHostRule  = "may not change the install's GitHub host"
		newGitHubRule   = "a GitHub forge the web console adds must sit at a host a GitHub forge already uses"
	)
	gitLabWith := func(old, new string) string {
		return integration(forgeGitHub+","+strings.Replace(forgeGitLab, old, new, 1)+","+forgeGitea, reposBothForge)
	}
	giteaWith := func(old, new string) string {
		return integration(forgeGitHub+","+forgeGitLab+","+strings.Replace(forgeGitea, old, new, 1), reposBothForge)
	}
	for _, tc := range []struct{ name, body, rule string }{
		{"a credentialed forge's host", hostMoved, destinationRule},
		{"a credentialed forge's credentialsRef", gitLabWith(`"gl-token"`, `"platform-agent-secrets"`), destinationRule},
		{"a credentialed forge's caBundleRef", gitLabWith(`"gl-ca"`, `"evil-ca"`), destinationRule},
		{"a credentialed forge's provider", gitLabWith(`"provider":"gitlab"`, `"provider":"gitea"`), destinationRule},
		{"a credentialsRef added to a forge with none", integration(
			`{"name":"github","namespace":"gke-labs","credentialsRef":{"name":"gl-token"}},`+forgeGitLab+","+forgeGitea, reposBothForge), destinationRule},
		{"a forge added with only a CA bundle", integration(forgeGitHub+","+forgeGitLab+","+forgeGitea+
			`,{"name":"gh-ca","provider":"github","host":"ghe.attacker.example","caBundleRef":{"name":"evil-ca"}}`, reposBothForge), newForgeRule},
		{"a credentialed forge's namespace", gitLabWith(`"namespace":"acme"`, `"namespace":"other-group"`), destinationRule},
		{"a credentialed forge's port", giteaWith(`"namespace":"acme"`, `"namespace":"acme","port":3000`), destinationRule},
		{"a credentialed forge's scheme", giteaWith(`"namespace":"acme"`, `"namespace":"acme","scheme":"http"`), destinationRule},
		{"a forge added with an existing credential", integration(forgeGitHub+","+forgeGitLab+","+forgeGitea+
			`,{"name":"evil","provider":"gitlab","host":"gitlab.attacker.example","namespace":"acme","credentialsRef":{"name":"gl-token"}}`,
			reposBothForge), newForgeRule},
		{"the GitHub forge's host", integration(
			`{"name":"github","provider":"github","host":"github.attacker.example","namespace":"gke-labs"},`+forgeGitLab+","+forgeGitea,
			reposBothForge), destinationRule},
		{"the GitHub forge replaced by one at another host", integration(
			`{"name":"gh-ghes","provider":"github","host":"github.attacker.example","namespace":"gke-labs"},`+forgeGitLab+","+forgeGitea,
			`[{"forge":"gh-ghes","repository":"gke-labs/kube-agents","role":"gitops"},{"forge":"gl","repository":"acme/infra","role":"context"}]`),
			newGitHubRule},
	} {
		reset()
		err := patch(console, tc.body)
		if !apierrors.IsForbidden(err) && !apierrors.IsInvalid(err) {
			t.Errorf("%s: %v, want refused", tc.name, err)
			continue
		}
		if !strings.Contains(err.Error(), tc.rule) {
			t.Errorf("%s: refused with %q, want the rule %q", tc.name, err, tc.rule)
		}
	}

	// Cases that start from their own declaration, set by the admin.
	const forgeBitbucket = `{"name":"bb","provider":"bitbucket","namespace":"acme",` +
		`"credentialsRef":{"name":"bb-token"},"identity":"{0f3b2c1d-aaaa-bbbb-cccc-0123456789ab}"}`
	bitbucketWith := func(old, new string) string {
		return integration(forgeGitHub+","+strings.Replace(forgeBitbucket, old, new, 1), `[]`)
	}
	for _, tc := range []struct{ name, setup, body, rule string }{
		{
			name:  "a Bitbucket forge's identity changed",
			setup: integration(forgeGitHub+","+forgeBitbucket, `[]`),
			body:  bitbucketWith(`0f3b2c1d-aaaa`, `99999999-aaaa`),
			rule:  destinationRule,
		},
		{
			name:  "a Bitbucket forge's identity removed",
			setup: integration(forgeGitHub+","+forgeBitbucket, `[]`),
			body:  bitbucketWith(`,"identity":"{0f3b2c1d-aaaa-bbbb-cccc-0123456789ab}"`, ``),
			rule:  destinationRule,
		},
		{
			name: "an identity added to a Bitbucket forge with none",
			setup: integration(forgeGitHub+","+
				strings.Replace(forgeBitbucket, `,"identity":"{0f3b2c1d-aaaa-bbbb-cccc-0123456789ab}"`, ``, 1), `[]`),
			body: integration(forgeGitHub+","+forgeBitbucket, `[]`),
			rule: destinationRule,
		},
		{
			// The operator refuses a GitHub forge at a reserved domain and falls
			// back to the first usable one, so a new forge must not become it.
			name:  "a GitHub forge added after a refused first one",
			setup: integration(`{"name":"gh-res","provider":"github","host":"acme.ghe.com","namespace":"gke-labs"}`, `[]`),
			body: integration(`{"name":"gh-res","provider":"github","host":"acme.ghe.com","namespace":"gke-labs"},`+
				`{"name":"x","provider":"github","host":"github.attacker.example","namespace":"org1"}`,
				`[{"forge":"x","repository":"org1/infra","role":"gitops"}]`),
			rule: newGitHubRule,
		},
		{
			name:  "the only GitHub Enterprise forge removed",
			setup: integration(`{"name":"ghe","provider":"github","host":"ghe.corp.example","namespace":"acme"},`+forgeGitLab, `[]`),
			body:  integration(forgeGitLab, `[]`),
			rule:  gitHubHostRule,
		},
	} {
		if err := patch(admin, tc.setup); err != nil {
			t.Fatalf("%s: setting the declaration: %v", tc.name, err)
		}
		err := patch(console, tc.body)
		if !apierrors.IsForbidden(err) && !apierrors.IsInvalid(err) {
			t.Errorf("%s: %v, want refused", tc.name, err)
		} else if !strings.Contains(err.Error(), tc.rule) {
			t.Errorf("%s: refused with %q, want the rule %q", tc.name, err, tc.rule)
		}
	}
	// The page's write carries a Bitbucket forge's identity over unchanged.
	if err := patch(admin, integration(forgeGitHub+","+forgeBitbucket, `[]`)); err != nil {
		t.Fatalf("setting the Bitbucket forge: %v", err)
	}
	if err := patch(console, integration(forgeGitHub+","+forgeBitbucket,
		`[{"forge":"bb","repository":"acme/infra","role":"context"}]`)); err != nil {
		t.Errorf("the Bitbucket forge carried over, a repository added: refused, want admitted: %v", err)
	}

	// The deprecated alias moved to forges, with github.com spelled another
	// way: the GitHub host does not change, so the console may write it.
	if err := patch(admin, `{"spec":{"integration":{"forges":null,"repositories":null,"github":{"org":"gke-labs","gitRepo":"kube-agents"}}}}`); err != nil {
		t.Fatalf("setting the alias: %v", err)
	}
	if err := patch(console, `{"spec":{"integration":{"forges":[{"name":"github","host":"GitHub.com","namespace":"gke-labs"}],`+
		`"repositories":[{"forge":"github","repository":"gke-labs/kube-agents","role":"gitops"}],"github":null}}}`); err != nil {
		t.Errorf("the alias moved to forges at GitHub.com: refused, want admitted: %v", err)
	}

	// Another identity holding the same Role is not selected.
	reset()
	other := impersonatedClient(t, cfg, ns, "other-controller")
	if err := patch(other, hostMoved); err != nil {
		t.Errorf("another ServiceAccount: refused, want admitted: %v", err)
	}
}
