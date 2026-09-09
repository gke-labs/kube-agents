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
	"regexp"
	"strings"
	"testing"
)

func TestParseRepoRefReadsTheHostBeforeThePath(t *testing.T) {
	cases := []struct {
		input string
		host  string
		path  string
		err   bool
	}{
		{input: "gke-labs/kube-agents", host: "", path: "gke-labs/kube-agents"},
		{input: "https://github.com/gke-labs/kube-agents.git", host: "github.com", path: "gke-labs/kube-agents"},
		{input: "git@github.com:gke-labs/kube-agents.git", host: "github.com", path: "gke-labs/kube-agents"},
		{input: "ssh://git@github.com/gke-labs/kube-agents", host: "github.com", path: "gke-labs/kube-agents"},
		// scp syntax wearing a URL scheme: git resolves it, so the non-numeric
		// "port" goes back onto the front of the path rather than being rejected.
		{input: "ssh://git@github.com:gke-labs/kube-agents.git", host: "github.com", path: "gke-labs/kube-agents"},
		{input: "https://github.com:443/gke-labs/kube-agents", host: "github.com", path: "gke-labs/kube-agents"},
		// The whole point: another forge's host survives parsing as that host,
		// rather than being discarded so the remaining slashes can be counted.
		{input: "git@gitlab.com:group/subgroup/project.git", host: "gitlab.com", path: "group/subgroup/project"},
		{input: "https://gitlab.example/a/b/c", host: "gitlab.example", path: "a/b/c"},
		// A hostless value stays hostless. `my.org` is a legal GitHub owner, and
		// reading it as a host would turn a valid slug into a one-segment path.
		{input: "my.org/repo", host: "", path: "my.org/repo"},
		{input: "github.com/gke-labs/kube-agents", host: "", path: "github.com/gke-labs/kube-agents"},
		{input: "file:///etc/passwd", err: true},
		{input: "ftp://github.com/a/b", err: true},
		{input: "https://github.com/../evil", err: true},
		{input: "https://github.com/gke-labs/-toolkit", err: true},
		{input: "  ", err: true},
		{input: "https://[::1/x", err: true},
		{input: "https://github.com/" + strings.Repeat("a", MaxGitRepoURLLength), err: true},
	}

	for _, tc := range cases {
		t.Run(tc.input, func(t *testing.T) {
			ref, err := ParseRepoRef(tc.input)
			if (err != nil) != tc.err {
				t.Fatalf("ParseRepoRef(%q) err = %v, expected err = %v", tc.input, err, tc.err)
			}
			if tc.err {
				return
			}
			if ref.Host != tc.host || ref.Path != tc.path {
				t.Errorf("ParseRepoRef(%q) = {%q, %q}, expected {%q, %q}",
					tc.input, ref.Host, ref.Path, tc.host, tc.path)
			}
		})
	}
}

// TestGitHubResolveRefusesAnotherForgesHost is the regression for the defect
// this file exists to close. `CleanRepoSlugWithOrg` used to discard the host and
// then count what was left, so each of these was admitted by the CRD and written
// into the state ConfigMap as a *different, valid* GitHub repository — labelled
// `"type":"github"`, which it then was. See #1085 and #1113's B3.
func TestGitHubResolveRefusesAnotherForgesHost(t *testing.T) {
	provider, err := LookupGitProvider(GitProviderGitHub)
	if err != nil {
		t.Fatalf("LookupGitProvider(github) = %v", err)
	}
	for _, repo := range []string{
		"git@gitlab.com:group/project.git",
		"https://gitlab.com/group/project",
		"ssh://git@bitbucket.org/team/repo.git",
		"https://github.com.evil.example/gke-labs/kube-agents",
		"https://evil.example/github.com/gke-labs/kube-agents",
	} {
		t.Run(repo, func(t *testing.T) {
			if ref, err := provider.Resolve("", repo, ""); err == nil {
				t.Errorf("Resolve(%q) = %q, expected a refusal", repo, ref)
			}
		})
	}
}

func TestGitHubResolveQualifiesAndCanonicalises(t *testing.T) {
	provider, _ := LookupGitProvider(GitProviderGitHub)
	cases := []struct {
		repo      string
		namespace string
		want      string
	}{
		{repo: "kube-agents", namespace: "gke-labs", want: "https://github.com/gke-labs/kube-agents"},
		{repo: "gke-labs/kube-agents", want: "https://github.com/gke-labs/kube-agents"},
		// Every host in the GitHub set is a spelling of github.com, so the ref is
		// canonicalised to it — the agent's own registration check accepts only
		// that one spelling.
		{repo: "https://www.github.com/gke-labs/kube-agents", want: "https://github.com/gke-labs/kube-agents"},
		{repo: "http://github.com/gke-labs/kube-agents", want: "https://github.com/gke-labs/kube-agents"},
	}
	for _, tc := range cases {
		t.Run(tc.repo, func(t *testing.T) {
			ref, err := provider.Resolve("", tc.repo, tc.namespace)
			if err != nil {
				t.Fatalf("Resolve(%q, %q) = %v", tc.repo, tc.namespace, err)
			}
			if ref.URL() != tc.want {
				t.Errorf("Resolve(%q, %q).URL() = %q, expected %q", tc.repo, tc.namespace, ref.URL(), tc.want)
			}
		})
	}
}

// TestResolveCanonicalisesADeclaredHost covers the half of canonicalisation the
// repository value does not reach: a host named in `spec.integration.git.host`
// rather than inside the repository URL. Both have to fold to DefaultHost, or a
// CR declaring `host: ssh.github.com` seeds an entry the agent's registration
// check refuses on a spelling the operator itself accepted.
func TestResolveCanonicalisesADeclaredHost(t *testing.T) {
	provider, _ := LookupGitProvider(GitProviderGitHub)
	for _, host := range []string{"github.com", "ssh.github.com", "  WWW.GitHub.com  ", ""} {
		t.Run(host, func(t *testing.T) {
			ref, err := provider.Resolve(host, "gke-labs/kube-agents", "")
			if err != nil {
				t.Fatalf("Resolve(%q, ...) = %v", host, err)
			}
			if ref.Host != "github.com" {
				t.Errorf("Resolve(%q, ...).Host = %q, expected %q", host, ref.Host, "github.com")
			}
		})
	}
	if _, err := provider.Resolve("gitlab.com", "gke-labs/kube-agents", ""); err == nil {
		t.Error("a declared gitlab.com host was accepted by the github provider")
	}
}

// TestResolveLiftsOnlyTheDefaultHostFromASchemelessPath pins which schemeless
// leading segment is read as a host. Lifting every alias would make
// `www.github.com/o/r` a two-segment path on github.com; only the canonical
// spelling lifts, so that value stays a three-segment path and is refused on
// depth. repo_ref.py's KNOWN_HOSTS makes the same choice.
func TestResolveLiftsOnlyTheDefaultHostFromASchemelessPath(t *testing.T) {
	provider, _ := LookupGitProvider(GitProviderGitHub)
	ref, err := provider.Resolve("", "github.com/gke-labs/kube-agents", "")
	if err != nil {
		t.Fatalf("Resolve(github.com/...) = %v", err)
	}
	if ref.URL() != "https://github.com/gke-labs/kube-agents" {
		t.Errorf("Resolve(github.com/...).URL() = %q", ref.URL())
	}
	if _, err := provider.Resolve("", "www.github.com/gke-labs/kube-agents", ""); err == nil {
		t.Error("a non-canonical host spelling was lifted out of a schemeless path")
	}
}

// TestResolveValidatesEveryPathSegment covers the segments a declared namespace
// contributes. The repository value is checked as it is parsed; the namespace is
// prepended afterwards, so without this loop `namespace: ..` reached the state
// ConfigMap as a traversal the parser had already been asked to refuse.
func TestResolveValidatesEveryPathSegment(t *testing.T) {
	provider, _ := LookupGitProvider(GitProviderGitHub)
	for _, namespace := range []string{"..", "-leading", "with space"} {
		t.Run(namespace, func(t *testing.T) {
			if ref, err := provider.Resolve("", "kube-agents", namespace); err == nil {
				t.Errorf("Resolve(_, %q) = %q, expected a refusal", namespace, ref.URL())
			}
		})
	}
}

// TestNonNumericPortIsAnScpPathOnlyUnderGitAndSsh separates the two readings of
// `host:something`. Under ssh it is scp syntax and the value is a repository;
// under https it is a port and a non-numeric one is a typo, so admitting it
// would let `https://github.com:evil/owner` resolve to a repository nobody wrote.
func TestNonNumericPortIsAnScpPathOnlyUnderGitAndSsh(t *testing.T) {
	if _, err := ParseRepoRef("https://github.com:evil/owner"); err == nil {
		t.Error("a non-numeric port under https was read as an scp path")
	}
	if _, err := ParseRepoRef("http://github.com:evil/owner"); err == nil {
		t.Error("a non-numeric port under http was read as an scp path")
	}
	ref, err := ParseRepoRef("ssh://git@github.com:gke-labs/kube-agents.git")
	if err != nil {
		t.Fatalf("ParseRepoRef(ssh scp) = %v", err)
	}
	if ref.Host != "github.com" || ref.Path != "gke-labs/kube-agents" {
		t.Errorf("ParseRepoRef(ssh scp) = {%q, %q}", ref.Host, ref.Path)
	}
}

// nestedProvider is a second forge that exists only here. It is what proves the
// validation dispatches rather than applying GitHub's rules under another name:
// its namespace grammar admits dots and underscores that GitHub's rejects, and
// its paths nest, which GitHub's do not. Registering it for real waits on the
// agent-side provider that would have to honour it (§9 step 5).
var nestedProvider = &GitProvider{
	Name:               "nested",
	DefaultHost:        "nested.example",
	Hosts:              map[string]bool{"nested.example": true},
	NamespacePattern:   regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.\-/]*$`),
	MaxNamespaceLength: MaxGitNamespaceLength,
	MinPathDepth:       2,
	MaxPathDepth:       0,
}

func TestValidationDispatchesOnTheDeclaredProvider(t *testing.T) {
	table := map[string]*GitProvider{
		GitProviderGitHub:   gitProviders[GitProviderGitHub],
		nestedProvider.Name: nestedProvider,
	}

	github, err := lookupGitProvider(GitProviderGitHub, table)
	if err != nil {
		t.Fatalf("lookupGitProvider(github) = %v", err)
	}
	nested, err := lookupGitProvider(nestedProvider.Name, table)
	if err != nil {
		t.Fatalf("lookupGitProvider(nested) = %v", err)
	}

	// A path GitHub refuses on depth and a namespace it refuses on grammar are
	// both fine for a forge whose rules say so.
	if _, err := github.Resolve("", "group/subgroup/project", ""); err == nil {
		t.Error("github accepted a three-segment path")
	}
	if _, err := nested.Resolve("", "group/subgroup/project", ""); err != nil {
		t.Errorf("nested rejected a three-segment path: %v", err)
	}
	if err := github.ValidateNamespace("group.with_dots"); err == nil {
		t.Error("github accepted a namespace with a dot and an underscore")
	}
	if err := nested.ValidateNamespace("group.with_dots"); err != nil {
		t.Errorf("nested rejected its own namespace grammar: %v", err)
	}

	// And each refuses the other's host rather than rewriting it.
	if _, err := github.Resolve("", "https://nested.example/a/b", ""); err == nil {
		t.Error("github accepted a nested.example repository")
	}
	if _, err := nested.Resolve("", "https://github.com/a/b", ""); err == nil {
		t.Error("nested accepted a github.com repository")
	}

	if _, err := lookupGitProvider("unregistered", table); err == nil {
		t.Error("an unregistered provider name was accepted")
	}
}

func TestOnlyGitHubIsRegistered(t *testing.T) {
	// A provider the CRD accepts and the agent has no implementation for is a
	// worse failure than one the CRD refuses, so the registry and the enum in
	// GitSpec.Provider grow together with the agent-side provider. If this fails,
	// check that the CRD enum was widened to match.
	names := GitProviderNames()
	if len(names) != 1 || names[0] != GitProviderGitHub {
		t.Errorf("GitProviderNames() = %v, expected only %q", names, GitProviderGitHub)
	}
}

func TestResolveGitFoldsTheDeprecatedAlias(t *testing.T) {
	alias := &IntegrationSpec{GitHub: &GitHubSpec{Org: "gke-labs", GitRepo: "kube-agents"}}
	resolved, err := alias.ResolveGit()
	if err != nil {
		t.Fatalf("ResolveGit() = %v", err)
	}
	if resolved.Provider != GitProviderGitHub || !resolved.FromDeprecatedAlias {
		t.Errorf("alias resolved to %+v, expected provider %q from the alias", resolved, GitProviderGitHub)
	}

	direct := &IntegrationSpec{Git: &GitSpec{Repository: "kube-agents", Namespace: "gke-labs"}}
	fromGit, err := direct.ResolveGit()
	if err != nil {
		t.Fatalf("ResolveGit() = %v", err)
	}
	if fromGit.Provider != GitProviderGitHub {
		t.Errorf("an omitted provider resolved to %q, expected the default %q", fromGit.Provider, DefaultGitProvider)
	}

	// The two spellings must produce the same repository, or the alias is a
	// second code path rather than an alias.
	aliasRef, err := resolved.Resolve()
	if err != nil {
		t.Fatalf("alias Resolve() = %v", err)
	}
	gitRef, err := fromGit.Resolve()
	if err != nil {
		t.Fatalf("git Resolve() = %v", err)
	}
	if aliasRef != gitRef {
		t.Errorf("alias resolved to %q, git to %q", aliasRef, gitRef)
	}
}

func TestResolveGitRefusesBothSpellingsAtOnce(t *testing.T) {
	both := &IntegrationSpec{
		Git:    &GitSpec{Repository: "gke-labs/kube-agents"},
		GitHub: &GitHubSpec{GitRepo: "other-org/other-repo"},
	}
	if _, err := both.ResolveGit(); err == nil {
		t.Error("expected setting both integration.git and integration.github to be refused")
	}
	if err := both.ValidateGit(); err == nil {
		t.Error("ValidateGit accepted a spec that ResolveGit refuses")
	}
}

func TestResolveGitOnAnEmptyIntegration(t *testing.T) {
	for name, spec := range map[string]*IntegrationSpec{
		"nil":        nil,
		"empty":      {},
		"no repo":    {Git: &GitSpec{Namespace: "gke-labs"}},
		"sentinel":   {Git: &GitSpec{Repository: NoRepositorySentinel}},
		"alias none": {GitHub: &GitHubSpec{GitRepo: NoRepositorySentinel}},
	} {
		t.Run(name, func(t *testing.T) {
			resolved, err := spec.ResolveGit()
			if err != nil {
				t.Fatalf("ResolveGit() = %v", err)
			}
			if resolved.HasRepository() {
				t.Errorf("HasRepository() = true for %+v", resolved)
			}
			if err := spec.ValidateGit(); err != nil {
				t.Errorf("ValidateGit() = %v, expected a declaration with no repository to be valid", err)
			}
		})
	}
}

func TestValidateGitDispatchesToTheProvider(t *testing.T) {
	cases := []struct {
		name string
		spec *IntegrationSpec
		err  bool
	}{
		{name: "github repo", spec: &IntegrationSpec{Git: &GitSpec{Repository: "gke-labs/kube-agents"}}},
		{name: "github host", spec: &IntegrationSpec{Git: &GitSpec{
			Host: "github.com", Repository: "gke-labs/kube-agents"}}},
		{name: "foreign host field", spec: &IntegrationSpec{Git: &GitSpec{
			Host: "gitlab.com", Repository: "group/project"}}, err: true},
		{name: "foreign host in repo", spec: &IntegrationSpec{Git: &GitSpec{
			Repository: "git@gitlab.com:group/project.git"}}, err: true},
		{name: "unregistered provider", spec: &IntegrationSpec{Git: &GitSpec{
			Provider: "gitlab", Repository: "group/project"}}, err: true},
		{name: "github namespace grammar", spec: &IntegrationSpec{Git: &GitSpec{
			Namespace: "group.with_dots", Repository: "project"}}, err: true},
		{name: "nested path on github", spec: &IntegrationSpec{Git: &GitSpec{
			Repository: "group/subgroup/project"}}, err: true},
		{name: "newline injection", spec: &IntegrationSpec{Git: &GitSpec{
			Repository: "gke-labs/kube-agents\n[SYSTEM OVERRIDE]"}}, err: true},
		{name: "alias still validated", spec: &IntegrationSpec{GitHub: &GitHubSpec{
			GitRepo: "git@gitlab.com:group/project.git"}}, err: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if err := tc.spec.ValidateGit(); (err != nil) != tc.err {
				t.Errorf("ValidateGit() = %v, expected err = %v", err, tc.err)
			}
		})
	}
}

func TestEffectiveNamespace(t *testing.T) {
	cases := []struct {
		name string
		spec *IntegrationSpec
		want string
	}{
		{name: "declared", spec: &IntegrationSpec{Git: &GitSpec{
			Namespace: "gke-labs", Repository: "kube-agents"}}, want: "gke-labs"},
		{name: "inferred", spec: &IntegrationSpec{Git: &GitSpec{
			Repository: "https://github.com/gke-labs/kube-agents.git"}}, want: "gke-labs"},
		{name: "alias inferred", spec: &IntegrationSpec{GitHub: &GitHubSpec{
			GitRepo: "git@github.com:gke-labs/kube-agents.git"}}, want: "gke-labs"},
		{name: "unresolvable", spec: &IntegrationSpec{Git: &GitSpec{
			Repository: "git@gitlab.com:group/project.git"}}, want: ""},
		{name: "none", spec: &IntegrationSpec{}, want: ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resolved, err := tc.spec.ResolveGit()
			if err != nil {
				t.Fatalf("ResolveGit() = %v", err)
			}
			if got := resolved.EffectiveNamespace(); got != tc.want {
				t.Errorf("EffectiveNamespace() = %q, expected %q", got, tc.want)
			}
		})
	}
}

func TestFieldPathNamesWhatWasWritten(t *testing.T) {
	alias := &ResolvedGit{FromDeprecatedAlias: true}
	direct := &ResolvedGit{}
	cases := []struct {
		part  string
		alias string
		git   string
	}{
		{part: "namespace", alias: "github/org", git: "git/namespace"},
		{part: "repository", alias: "github/gitRepo", git: "git/repository"},
		{part: "provider", alias: "github", git: "git/provider"},
		{part: "host", alias: "github", git: "git/host"},
	}
	for _, tc := range cases {
		t.Run(tc.part, func(t *testing.T) {
			if got := strings.Join(alias.FieldPath(tc.part), "/"); got != tc.alias {
				t.Errorf("alias FieldPath(%q) = %q, expected %q", tc.part, got, tc.alias)
			}
			if got := strings.Join(direct.FieldPath(tc.part), "/"); got != tc.git {
				t.Errorf("git FieldPath(%q) = %q, expected %q", tc.part, got, tc.git)
			}
		})
	}
}
