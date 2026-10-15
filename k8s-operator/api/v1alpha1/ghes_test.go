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

package v1alpha1

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"
)

// gheForge is a GitHub forge at a GitHub Enterprise Server host.
func gheForge(name, host, namespace string) ForgeSpec {
	return ForgeSpec{Name: name, Provider: GitProviderGitHub, Host: host, Namespace: namespace}
}

func problemPaths(resolved *ResolvedIntegration) map[string]string {
	paths := map[string]string{}
	for _, problem := range resolved.Problems() {
		paths[problem.Path.String()] = problem.Err.Error()
	}
	return paths
}

// A GitHub forge may be declared at a GitHub Enterprise Server host, and a
// repository on it resolves there: by a URL on that host, or by a bare name.
// A github.com repository is refused on it, never moved onto it, and the
// reverse.
func TestGitHubResolvesOnAGitHubEnterpriseServerHost(t *testing.T) {
	github, _ := LookupGitProvider(GitProviderGitHub)
	const host = "GHE.Example.com"
	if err := github.ValidateHost(host); err != nil {
		t.Fatalf("ValidateHost(%q) = %v", host, err)
	}
	for in, want := range map[string]string{
		"https://ghe.example.com/acme/infra": "https://ghe.example.com/acme/infra",
		"git@ghe.example.com:acme/infra.git": "https://ghe.example.com/acme/infra",
		"ghe.example.com/acme/infra":         "https://ghe.example.com/acme/infra",
		"acme/infra":                         "https://ghe.example.com/acme/infra",
		"infra":                              "https://ghe.example.com/acme/infra",
	} {
		ref, err := github.Resolve(host, in, "acme")
		if err != nil {
			t.Errorf("Resolve(%q) = %v", in, err)
			continue
		}
		if ref.URL() != want {
			t.Errorf("Resolve(%q) = %q, expected %q", in, ref.URL(), want)
		}
	}
	for _, in := range []string{"https://github.com/acme/infra", "github.com/acme/infra", "https://ghe.other.example/acme/infra", "acme/sub/infra"} {
		if ref, err := github.Resolve(host, in, ""); err == nil {
			t.Errorf("Resolve(%q) on %s = %q, expected a refusal", in, host, ref.URL())
		}
	}
	if _, err := github.Resolve("", "https://ghe.example.com/acme/infra", ""); err == nil {
		t.Error("a github.com forge accepted a GitHub Enterprise Server repository")
	}
}

// A spelling of github.com folds to github.com. A host under a domain GitHub
// operates is not a self-managed instance, for GitHub or for GitLab, and
// neither is another provider's host.
func TestGitHubRefusesItsOwnServicesAsASelfManagedHost(t *testing.T) {
	github, _ := LookupGitProvider(GitProviderGitHub)
	gitlab := gitlabProvider(t)
	for _, host := range []string{"www.github.com", "SSH.GitHub.com"} {
		if err := github.ValidateHost(host); err != nil {
			t.Errorf("ValidateHost(%q) = %v", host, err)
		}
		if got := github.canonicalHost(host); got != "github.com" {
			t.Errorf("canonicalHost(%q) = %q, expected github.com", host, got)
		}
	}
	for _, host := range []string{
		"api.github.com", "gist.github.com", "enterprise.github.com",
		"raw.githubusercontent.com", "githubusercontent.com",
		"acme.ghe.com", "ghe.com", "acme.github.io", "githubassets.com",
		"acme.github.dev", "githubapp.com",
	} {
		if err := github.ValidateHost(host); err == nil {
			t.Errorf("github accepted %q as a GitHub Enterprise Server host", host)
		}
		if err := gitlab.ValidateHost(host); err == nil {
			t.Errorf("gitlab accepted GitHub's %q as a self-managed host", host)
		}
	}
	for _, host := range []string{"gitlab.com", "www.gitlab.com", "registry.gitlab.com"} {
		if err := github.ValidateHost(host); err == nil {
			t.Errorf("github accepted the GitLab host %q", host)
		}
	}
	// A host that merely contains GitHub's name is the customer's.
	for _, host := range []string{"github.acme.example", "mygithub.com", "ghe.acme.example"} {
		if err := github.ValidateHost(host); err != nil {
			t.Errorf("ValidateHost(%q) = %v", host, err)
		}
	}
}

// One GitHub host per install: a second GitHub forge at another host is
// refused at its host, with its repositories, whichever of the two hosts is
// github.com. Two GitHub forges at one host still name two organisations.
func TestAnInstallServesOneGitHubHost(t *testing.T) {
	for name, forges := range map[string][]ForgeSpec{
		"dotcom then GHES": {ghForge("github", "acme"), gheForge("ghe", "ghe.example.com", "acme")},
		"GHES then dotcom": {gheForge("github", "ghe.example.com", "acme"), ghForge("ghe", "acme")},
		"two GHES hosts":   {gheForge("github", "ghe.example.com", "acme"), gheForge("ghe", "ghe.other.example", "acme")},
	} {
		in := &IntegrationSpec{
			Forges: forges,
			Repositories: []RepositorySpec{
				repo("github", "infra", RepositoryRoleGitOps),
				repo("ghe", "app", RepositoryRoleManaged),
			},
		}
		resolved, err := in.ResolveGit()
		if err != nil {
			t.Fatal(err)
		}
		paths := problemPaths(resolved)
		if why, ok := paths["forges[1].host"]; !ok || !strings.Contains(why, "one GitHub host") {
			t.Errorf("%s: Problems() = %v, expected forges[1].host refused as a second GitHub host", name, paths)
		}
		if len(paths) != 1 {
			t.Errorf("%s: Problems() = %v, expected one", name, paths)
		}
		if got := resolved.Accepted(RepositoryRoleManaged); len(got) != 0 {
			t.Errorf("%s: a repository on the refused forge was accepted", name)
		}
		// The status counts it as withheld on a refused forge.
		if withheld := resolved.OnRefusedForge(); len(withheld) != 1 || withheld[0].ForgeName != "ghe" {
			t.Errorf("%s: OnRefusedForge() = %v, expected the repository on the refused forge", name, withheld)
		}
		if got, want := resolved.GitHubHost(), resolved.Forges[0].Host; got != want && !(want == "" && got == "github.com") {
			t.Errorf("%s: GitHubHost() = %q, expected the first forge's", name, got)
		}
		egress := strings.Join(ForgeEgressPatterns(in), ",")
		if second := resolved.Forges[1].Host; second != "" && strings.Contains(egress, second) {
			t.Errorf("%s: the refused forge's host reached egress: %s", name, egress)
		}
	}
	same := &IntegrationSpec{Forges: []ForgeSpec{
		gheForge("a", "ghe.example.com", "acme"), gheForge("b", "GHE.example.com", "beta"),
	}}
	resolved, _ := same.ResolveGit()
	if paths := problemPaths(resolved); len(paths) != 0 {
		t.Errorf("two GitHub forges at one host were refused: %v", paths)
	}
}

// One host is one forge's: a GitHub forge and a GitLab forge declared at one
// self-managed host are refused, the later one at its host.
func TestOneHostIsNotClaimedByTwoProviders(t *testing.T) {
	for name, forges := range map[string][]ForgeSpec{
		"github first": {gheForge("a", "git.example.com", "acme"), glForge("b", "git.example.com", "acme")},
		"gitlab first": {glForge("a", "git.example.com", "acme"), gheForge("b", "GIT.example.com", "acme")},
	} {
		resolved, err := (&IntegrationSpec{Forges: forges}).ResolveGit()
		if err != nil {
			t.Fatal(err)
		}
		paths := problemPaths(resolved)
		if why, ok := paths["forges[1].host"]; !ok || !strings.Contains(why, "already the host of the") {
			t.Errorf("%s: Problems() = %v, expected forges[1].host refused", name, paths)
		}
		if paths["forges[0].host"] != "" {
			t.Errorf("%s: the first forge was refused: %v", name, paths)
		}
		for _, entry := range resolved.BrokerForges("/creds", "/ca") {
			if entry.Provider == resolved.Forges[1].Provider && strings.EqualFold(entry.Host, "git.example.com") {
				t.Errorf("%s: the refused forge reached the broker: %+v", name, entry)
			}
		}
	}
}

// A GitHub Enterprise Server install hands the broker a configuration whose
// GitHub entry is at the declared host, with the CA bundle a GitHub forge
// there names. A GitLab forge beside it follows, as before.
func TestBrokerForgesPutsGitHubAtTheEnterpriseHostWithItsCA(t *testing.T) {
	ghe := gheForge("ghe", "GHE.example.com", "acme")
	ghe.CABundleRef = &ForgeCABundleRef{Name: "ghe-ca", Key: "bundle.pem"}
	in := &IntegrationSpec{
		Forges:       []ForgeSpec{ghe, glForge("gl", "", "acme")},
		Repositories: []RepositorySpec{repo("ghe", "infra", RepositoryRoleGitOps)},
	}
	resolved, err := in.ResolveGit()
	if err != nil {
		t.Fatal(err)
	}
	if paths := problemPaths(resolved); len(paths) != 0 {
		t.Fatalf("Problems() = %v", paths)
	}
	got, _ := json.Marshal(resolved.BrokerForges("/creds", "/ca"))
	want := `[{"provider":"github","host":"ghe.example.com","caSecret":"ghe-ca","caKey":"bundle.pem","caFile":"/ca/ghe/ca.crt"},` +
		`{"provider":"gitlab","host":"gitlab.com","tokenPath":"/creds/gl/token","allowedPaths":["acme"]}]`
	if string(got) != want {
		t.Errorf("BrokerForges =\n %s\nexpected\n %s", got, want)
	}
	entry, err := resolved.Accepted(RepositoryRoleGitOps)[0].ManagedRepoEntry()
	if err != nil || entry.Type != GitProviderGitHub || entry.URL != "https://ghe.example.com/acme/infra" {
		t.Errorf("ManagedRepoEntry() = (%+v, %v), expected the full GitHub Enterprise Server URL", entry, err)
	}
	egress := ForgeEgressPatterns(in)
	if !slices.Contains(egress, "ghe.example.com") {
		t.Errorf("egress = %v, expected the GitHub Enterprise Server host as a literal", egress)
	}
}

// A GitHub Enterprise Server install with no CA bundle and no other forge
// still needs a configuration: without one the broker serves github.com.
func TestBrokerForgesNamesAnEnterpriseHostWithNoCA(t *testing.T) {
	resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{gheForge("ghe", "ghe.example.com", "acme")}}).ResolveGit()
	got, _ := json.Marshal(resolved.BrokerForges("/creds", "/ca"))
	if want := `[{"provider":"github","host":"ghe.example.com"}]`; string(got) != want {
		t.Errorf("BrokerForges = %s, expected %s", got, want)
	}
}

// A github.com install renders what it rendered before GitHub Enterprise
// Server: no broker configuration, the same egress, and with a GitLab forge
// the same GitHub entry.
func TestAGitHubDotComInstallIsUnchanged(t *testing.T) {
	for name, in := range map[string]*IntegrationSpec{
		"alias":         {GitHub: &GitHubSpec{Org: "acme", GitRepo: "infra"}},
		"list":          {Forges: []ForgeSpec{ghForge("github", "acme")}, Repositories: []RepositorySpec{repo("github", "infra", RepositoryRoleGitOps)}},
		"explicit host": {Forges: []ForgeSpec{gheForge("github", "github.com", "acme")}},
		"www spelling":  {Forges: []ForgeSpec{gheForge("github", "www.github.com", "acme")}},
	} {
		resolved, _ := in.ResolveGit()
		if got := resolved.BrokerForges("/creds", "/ca"); got != nil {
			t.Errorf("%s: BrokerForges = %+v, expected none", name, got)
		}
		if got := resolved.GitHubHost(); got != "github.com" {
			t.Errorf("%s: GitHubHost() = %q", name, got)
		}
		if got := strings.Join(ForgeEgressPatterns(in), ","); got != "github.com,*.github.com,*.githubusercontent.com" {
			t.Errorf("%s: egress = %s", name, got)
		}
	}
	resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{ghForge("github", "acme"), glForge("gl", "", "acme")}}).ResolveGit()
	got, _ := json.Marshal(resolved.BrokerForges("/creds", "/ca"))
	if !strings.HasPrefix(string(got), `[{"provider":"github","host":"github.com"},`) {
		t.Errorf("BrokerForges = %s, expected the github.com entry first and unchanged", got)
	}
}

// caBundleRef is accepted on a GitHub forge at a GitHub Enterprise Server
// host and refused at github.com in every spelling. Two GitHub forges at one
// host may not name two CA bundles: the broker trusts one per host.
func TestACABundleRefOnAGitHubForge(t *testing.T) {
	ok := gheForge("ok", "ghe.example.com", "acme")
	ok.CABundleRef = &ForgeCABundleRef{Name: "ghe-ca"}
	resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{ok}}).ResolveGit()
	if paths := problemPaths(resolved); len(paths) != 0 {
		t.Errorf("a CA bundle on a GitHub Enterprise Server forge was refused: %v", paths)
	}
	for _, host := range []string{"", "github.com", "WWW.GitHub.com", "ssh.github.com"} {
		f := gheForge("dotcom", host, "acme")
		f.CABundleRef = &ForgeCABundleRef{Name: "ca"}
		resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{f}}).ResolveGit()
		if _, refused := problemPaths(resolved)["forges[0].caBundleRef"]; !refused {
			t.Errorf("a CA bundle at github.com (host %q) was accepted", host)
		}
		if got := resolved.BrokerForges("/creds", "/ca"); got != nil {
			t.Errorf("host %q: BrokerForges = %+v, expected none", host, got)
		}
	}
	other := gheForge("other", "ghe.example.com", "beta")
	other.CABundleRef = &ForgeCABundleRef{Name: "another-ca"}
	same := gheForge("same", "ghe.example.com", "gamma")
	same.CABundleRef = &ForgeCABundleRef{Name: "ghe-ca"}
	resolved, _ = (&IntegrationSpec{Forges: []ForgeSpec{ok, other, same}}).ResolveGit()
	paths := problemPaths(resolved)
	if why, refused := paths["forges[1].host"]; !refused || !strings.Contains(why, "one CA bundle per host") {
		t.Errorf("Problems() = %v, expected the second CA bundle refused", paths)
	}
	if len(paths) != 1 {
		t.Errorf("Problems() = %v, expected only the second CA bundle refused", paths)
	}
}

// A forge at a host other than github.com names its provider. Left to the
// default, a forge written for a GitLab instance would be read as a GitHub
// Enterprise Server and handed a GitHub token. A host the github provider
// refuses anyway is reported at the host alone.
func TestAForgeAtAnotherHostNamesItsProvider(t *testing.T) {
	unnamed := ForgeSpec{Name: "f", Host: "git.example.com", Namespace: "acme"}
	resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{unnamed}}).ResolveGit()
	paths := problemPaths(resolved)
	if why, ok := paths["forges[0].provider"]; !ok || !strings.Contains(why, "names no provider") {
		t.Errorf("Problems() = %v, expected forges[0].provider refused", paths)
	}
	if got := resolved.BrokerForges("/creds", "/ca"); got != nil {
		t.Errorf("an unnamed forge reached the broker: %+v", got)
	}
	if resolved.GitHubHost() != "github.com" {
		t.Errorf("GitHubHost() = %q, expected github.com while the forge is refused", resolved.GitHubHost())
	}
	for name, f := range map[string]ForgeSpec{
		"named github":        gheForge("f", "git.example.com", "acme"),
		"github.com, unnamed": {Name: "f", Host: "WWW.GitHub.com", Namespace: "acme"},
		"no host, unnamed":    {Name: "f", Namespace: "acme"},
	} {
		resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{f}}).ResolveGit()
		if paths := problemPaths(resolved); len(paths) != 0 {
			t.Errorf("%s: Problems() = %v, expected none", name, paths)
		}
	}
	resolved, _ = (&IntegrationSpec{Forges: []ForgeSpec{{Name: "f", Host: "gitlab.com"}}}).ResolveGit()
	if paths := problemPaths(resolved); len(paths) != 1 || paths["forges[0].host"] == "" {
		t.Errorf("Problems() = %v, expected only forges[0].host", paths)
	}
}
