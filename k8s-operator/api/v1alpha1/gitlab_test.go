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

func gitlabProvider(t *testing.T) *GitProvider {
	t.Helper()
	provider, err := LookupGitProvider(GitProviderGitLab)
	if err != nil {
		t.Fatalf("LookupGitProvider(gitlab) = %v", err)
	}
	return provider
}

// glForge is a GitLab forge with a credential, which every valid one has.
func glForge(name, host, namespace string) ForgeSpec {
	return ForgeSpec{
		Name: name, Provider: GitProviderGitLab, Host: host, Namespace: namespace,
		CredentialsRef: &ForgeCredentialsRef{Name: name + "-token"},
	}
}

func TestGitLabResolvesNestedGroupsOnGitLabCom(t *testing.T) {
	provider := gitlabProvider(t)
	for repo, want := range map[string]string{
		"https://gitlab.com/acme/platform/infra.git": "https://gitlab.com/acme/platform/infra",
		"git@gitlab.com:acme/infra.git":              "https://gitlab.com/acme/infra",
		"gitlab.com/acme/platform/infra":             "https://gitlab.com/acme/platform/infra",
		"acme/platform/infra":                        "https://gitlab.com/acme/platform/infra",
		"infra":                                      "https://gitlab.com/acme/infra",
	} {
		ref, err := provider.Resolve("", repo, "acme")
		if err != nil {
			t.Errorf("Resolve(%q) = %v", repo, err)
			continue
		}
		if ref.URL() != want {
			t.Errorf("Resolve(%q) = %q, expected %q", repo, ref.URL(), want)
		}
	}
	if _, err := provider.Resolve("", "infra", ""); err == nil {
		t.Error("a bare project name with no namespace resolved")
	}
	if err := provider.ValidateNamespace("acme/platform.team/sub_group"); err != nil {
		t.Errorf("a nested group path was refused: %v", err)
	}
	for _, namespace := range []string{"-acme", "acme.", "acme//sub", "acme/.hidden"} {
		if err := provider.ValidateNamespace(namespace); err == nil {
			t.Errorf("ValidateNamespace(%q) accepted a path GitLab refuses", namespace)
		}
	}
}

// A GitLab forge never takes a GitHub repository, nor the reverse: each
// refuses the other's host rather than rewriting it onto its own.
func TestGitLabAndGitHubRefuseEachOthersHosts(t *testing.T) {
	gitlab := gitlabProvider(t)
	github, _ := LookupGitProvider(GitProviderGitHub)
	if _, err := gitlab.Resolve("", "https://github.com/acme/infra", ""); err == nil {
		t.Error("gitlab accepted a github.com repository")
	}
	if _, err := github.Resolve("", "https://gitlab.com/acme/infra", ""); err == nil {
		t.Error("github accepted a gitlab.com repository")
	}
	for _, host := range []string{"github.com", "WWW.GitHub.com", "ssh.github.com"} {
		if err := gitlab.ValidateHost(host); err == nil {
			t.Errorf("a gitlab forge was accepted at the GitHub host %q", host)
		}
	}
}

// A self-managed instance is the forge's declared host, and a repository on it
// must name that host or none. The declared host never replaces one the
// repository names: gitlab.com on a forge at gitlab.example.com is refused,
// because moving it would hand a gitlab.com project's name to another server.
func TestASelfManagedHostIsTheForgesOwnAndNeverRewritesAnother(t *testing.T) {
	provider := gitlabProvider(t)
	const host = "GitLab.Example.com"
	for repo, want := range map[string]string{
		"https://gitlab.example.com/acme/infra": "https://gitlab.example.com/acme/infra",
		"git@gitlab.example.com:acme/infra.git": "https://gitlab.example.com/acme/infra",
		"gitlab.example.com/acme/sub/infra":     "https://gitlab.example.com/acme/sub/infra",
		"acme/infra":                            "https://gitlab.example.com/acme/infra",
	} {
		ref, err := provider.Resolve(host, repo, "")
		if err != nil {
			t.Errorf("Resolve(%q) on a self-managed forge = %v", repo, err)
			continue
		}
		if ref.URL() != want {
			t.Errorf("Resolve(%q) = %q, expected %q", repo, ref.URL(), want)
		}
	}
	for _, repo := range []string{
		"https://gitlab.com/acme/infra",
		"gitlab.com/acme/infra",
		"https://other.example.com/acme/infra",
	} {
		if ref, err := provider.Resolve(host, repo, ""); err == nil {
			t.Errorf("Resolve(%q) on a forge at %s = %q, expected a refusal", repo, host, ref.URL())
		}
	}
	// And the reverse: a forge at gitlab.com does not take the instance's.
	if _, err := provider.Resolve("", "https://gitlab.example.com/acme/infra", ""); err == nil {
		t.Error("a gitlab.com forge accepted a self-managed instance's repository")
	}
	// Review round 4: the regex had no label structure, so an empty or
	// dash-edged label passed and reached the egress policy and the broker.
	for _, bad := range []string{"localhost", "gitlab_example.com", "-gitlab.example.com",
		"gitlab..example.com", "gitlab.-x.com", "gitlab.x-.com"} {
		if err := provider.ValidateHost(bad); err == nil {
			t.Errorf("ValidateHost(%q) accepted something that is not a hostname", bad)
		}
	}
}

func TestEgressAddsASelfManagedHostAsALiteral(t *testing.T) {
	got := ForgeEgressPatterns(&IntegrationSpec{Forges: []ForgeSpec{glForge("gl", "gitlab.example.com", "acme")}})
	want := "github.com,*.github.com,*.githubusercontent.com,gitlab.com,*.gitlab.com,gitlab.example.com"
	if strings.Join(got, ",") != want {
		t.Errorf("ForgeEgressPatterns = %v, expected %s", got, want)
	}
}

// Without the Secret the broker has no token to call GitLab with, so the forge
// is refused at its credentialsRef and nothing on it is seeded.
func TestAGitLabForgeWithoutCredentialsIsRefused(t *testing.T) {
	in := &IntegrationSpec{
		Forges:       []ForgeSpec{{Name: "gl", Provider: GitProviderGitLab, Namespace: "acme"}},
		Repositories: []RepositorySpec{repo("gl", "infra", RepositoryRoleGitOps)},
	}
	resolved, err := in.ResolveGit()
	if err != nil {
		t.Fatal(err)
	}
	problems := resolved.Problems()
	if len(problems) != 1 || problems[0].Path.String() != "forges[0].credentialsRef" {
		t.Fatalf("Problems() = %v, expected one at forges[0].credentialsRef", problems)
	}
	if !strings.Contains(problems[0].Err.Error(), `"token"`) {
		t.Errorf("the refusal does not name the Secret key: %v", problems[0].Err)
	}
	if got := resolved.Accepted(RepositoryRoleGitOps); len(got) != 0 {
		t.Errorf("a repository on a forge without credentials was accepted: %v", got)
	}
	in.Forges[0] = glForge("gl", "", "acme")
	resolved, _ = in.ResolveGit()
	if problems := resolved.Problems(); len(problems) != 0 {
		t.Errorf("Problems() with credentials = %v", problems)
	}
	entry, err := resolved.Accepted(RepositoryRoleGitOps)[0].ManagedRepoEntry()
	if err != nil || entry.Type != GitProviderGitLab || entry.URL != "https://gitlab.com/acme/infra" {
		t.Errorf("ManagedRepoEntry = %+v, %v", entry, err)
	}
}

// A GitHub-only declaration hands the broker no configuration at all, which is
// what keeps such an install exactly as it was.
func TestBrokerForgesIsNilWithoutAForgeThatNeedsOne(t *testing.T) {
	for name, in := range map[string]*IntegrationSpec{
		"alias":                   {GitHub: &GitHubSpec{Org: "acme", GitRepo: "infra"}},
		"github list":             {Forges: []ForgeSpec{ghForge("github", "acme")}},
		"gitlab with no secret":   {Forges: []ForgeSpec{{Name: "gl", Provider: GitProviderGitLab}}},
		"gitlab at github's host": {Forges: []ForgeSpec{glForge("gl", "github.com", "")}},
		// Valid, and nothing to serve: no namespace, no repository. An entry
		// for it would have to serve the whole host, which the broker refuses
		// to infer.
		"gitlab with nothing to serve": {Forges: []ForgeSpec{glForge("gl", "", "")}},
	} {
		resolved, _ := in.ResolveGit()
		if got := resolved.BrokerForges("/creds"); got != nil {
			t.Errorf("%s: BrokerForges = %+v, expected none", name, got)
		}
	}
	var none *ResolvedIntegration
	if none.BrokerForges("/creds") != nil {
		t.Error("BrokerForges on no declaration is not nil")
	}
}

// One entry per host: the broker refuses two forges claiming one, so the
// second GitLab forge at gitlab.com is refused at admission, its repository is
// withheld, and the configuration carries the first.
func TestBrokerForgesListsGitHubFirstAndEachGitLabHostOnce(t *testing.T) {
	in := &IntegrationSpec{
		Forges: []ForgeSpec{
			ghForge("github", "acme"),
			glForge("gl", "", "acme"),
			glForge("gl-again", "gitlab.com", "other"),
			glForge("onprem", "gitlab.example.com", ""),
		},
		Repositories: []RepositorySpec{
			repo("gl", "infra", RepositoryRoleGitOps),
			repo("gl", "https://gitlab.com/platform/tools/app", RepositoryRoleManaged),
			repo("gl", "docs/runbooks", RepositoryRoleContext),
			repo("onprem", "team/svc", RepositoryRoleManaged),
			repo("gl-again", "other/thing", RepositoryRoleManaged),
		},
	}
	resolved, err := in.ResolveGit()
	if err != nil {
		t.Fatal(err)
	}
	got, err := json.Marshal(resolved.BrokerForges("/creds"))
	if err != nil {
		t.Fatal(err)
	}
	if problems := resolved.Problems(); len(problems) != 1 || problems[0].Path.String() != "forges[2].host" {
		t.Errorf("Problems() = %v, expected the second gitlab.com forge refused at its host", problems)
	}
	for _, r := range resolved.Accepted(RepositoryRoleManaged) {
		if r.ForgeName == "gl-again" {
			t.Error("a repository on the refused second forge was accepted")
		}
	}
	want := `[{"provider":"github","host":"github.com"},` +
		`{"provider":"gitlab","host":"gitlab.com","tokenPath":"/creds/gl/token","allowedPaths":["acme","docs","platform/tools"]},` +
		`{"provider":"gitlab","host":"gitlab.example.com","tokenPath":"/creds/onprem/token","allowedPaths":["team"]}]`
	if string(got) != want {
		t.Errorf("BrokerForges =\n %s\nexpected\n %s", got, want)
	}
	// Bot review: the repository on the shadowed forge is withheld too, and the
	// Degraded message counts what OnRefusedForge returns.
	if withheld := resolved.OnRefusedForge(); len(withheld) != 1 || withheld[0].ForgeName != "gl-again" {
		t.Errorf("OnRefusedForge() = %v, expected the repository on the shadowed gl-again forge", withheld)
	}
}

// Bot review: allowedPaths took the group of every repository that resolved,
// so one the status refused -- here for its role -- widened what the broker
// would spend the token on.
func TestARefusedRepositoryWidensNothingTheBrokerIsGiven(t *testing.T) {
	in := &IntegrationSpec{
		Forges: []ForgeSpec{glForge("gl", "", "acme")},
		Repositories: []RepositorySpec{
			repo("gl", "acme/infra", RepositoryRoleGitOps),
			repo("gl", "other-group/thing", "manged"),
		},
	}
	resolved, err := in.ResolveGit()
	if err != nil {
		t.Fatal(err)
	}
	got := resolved.BrokerForges("/creds")
	if len(got) != 2 || !slices.Equal(got[1].AllowedPaths, []string{"acme"}) {
		t.Errorf("BrokerForges = %+v, expected the gitlab entry narrowed to acme alone", got)
	}

	// A forge whose only repository is refused, with no namespace of its own,
	// serves nothing: no entry (an empty list is the whole host) and a warning.
	only := &IntegrationSpec{
		Forges:       []ForgeSpec{glForge("gl", "", "")},
		Repositories: []RepositorySpec{repo("gl", "other-group/thing", "manged")},
	}
	resolved, err = only.ResolveGit()
	if err != nil {
		t.Fatal(err)
	}
	if got := resolved.BrokerForges("/creds"); got != nil {
		t.Errorf("BrokerForges = %+v, expected none for a forge that serves nothing accepted", got)
	}
	if warnings := resolved.Warnings(); !slices.ContainsFunc(warnings, func(w string) bool {
		return strings.Contains(w, "no accepted repository")
	}) {
		t.Errorf("Warnings() = %v, expected the serves-nothing warning", warnings)
	}
}

// Review: a Secret name the API server refuses in the broker's volume passed
// admission, and the broker Deployment's apply then failed every reconcile.
func TestAnInvalidSecretNameIsRefusedAndNotMounted(t *testing.T) {
	for _, name := range []string{"Bad_Name", "gitlab token", "-x", strings.Repeat("a", 254)} {
		f := glForge("gl", "", "acme")
		f.CredentialsRef.Name = name
		resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{f}}).ResolveGit()
		problems := resolved.Problems()
		if len(problems) != 1 || problems[0].Path.String() != "forges[0].credentialsRef" {
			t.Errorf("%q: Problems() = %v, expected one at forges[0].credentialsRef", name, problems)
		}
		if got := resolved.BrokerForges("/creds"); got != nil {
			t.Errorf("%q: BrokerForges = %+v, expected none", name, got)
		}
	}
}

// Review: check() let the first credentialed forge claim a host even when it
// was refused or served nothing, while BrokerForges skipped it without
// claiming -- so status refused the second forge whose token was mounted and
// served. One rule now: only a valid forge that serves something claims.
func TestOnlyAForgeTheBrokerIsGivenClaimsItsHost(t *testing.T) {
	for name, tc := range map[string]struct {
		first ForgeSpec
		repos []RepositorySpec
	}{
		"refused first":        {glForge("a", "", "-bad"), nil},
		"serves-nothing first": {glForge("a", "", ""), nil},
		// Review round 2: a's only repository resolves but is refused for its
		// own reason, so a serves nothing the status accepts -- it must not
		// claim the host either, or b is shadowed and the broker gets neither.
		"only repository refused first": {glForge("a", "", ""), []RepositorySpec{repo("a", "x/y", "manged")}},
	} {
		in := &IntegrationSpec{
			Forges:       []ForgeSpec{tc.first, glForge("b", "", "acme")},
			Repositories: append(append([]RepositorySpec{}, tc.repos...), repo("b", "infra", RepositoryRoleManaged)),
		}
		resolved, _ := in.ResolveGit()
		for _, p := range resolved.Problems() {
			if p.Path.String() == "forges[1].host" {
				t.Errorf("%s: the second forge was refused as shadowed: %v", name, p.Err)
			}
		}
		got := resolved.BrokerForges("/creds")
		if len(got) != 2 || got[1].Name != "b" {
			t.Errorf("%s: BrokerForges = %+v, expected github and b", name, got)
		}
		if len(resolved.Accepted(RepositoryRoleManaged)) != 1 {
			t.Errorf("%s: b's repository was not accepted", name)
		}
	}
}

func TestAForgeThatServesNothingIsWarnedAbout(t *testing.T) {
	resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{glForge("gl", "", "")}}).ResolveGit()
	warnings := resolved.Warnings()
	if len(warnings) != 1 || !strings.Contains(warnings[0], "forges[0]") {
		t.Errorf("Warnings() = %v, expected one naming forges[0]", warnings)
	}
}

// Review: only three GitHub spellings were refused as a GitLab host.
func TestNoGitHubNameIsAGitLabHostAndWwwGitLabFolds(t *testing.T) {
	provider := gitlabProvider(t)
	for _, host := range []string{"api.github.com", "gist.github.com", "raw.githubusercontent.com", "githubusercontent.com", "x.y.github.com"} {
		if err := provider.ValidateHost(host); err == nil {
			t.Errorf("ValidateHost(%q) accepted a GitHub name", host)
		}
	}
	ref, err := provider.Resolve("www.gitlab.com", "https://www.gitlab.com/acme/infra", "")
	if err != nil || ref.URL() != "https://gitlab.com/acme/infra" {
		t.Errorf("www.gitlab.com did not fold to gitlab.com: %q, %v", ref.URL(), err)
	}
}

// The broker refuses allowedPaths on a github entry; the operator never
// renders the key there, whatever is declared beside it.
func TestTheGitHubEntryNeverCarriesAllowedPaths(t *testing.T) {
	in := &IntegrationSpec{
		Forges:       []ForgeSpec{ghForge("github", "acme"), glForge("gl", "", "acme")},
		Repositories: []RepositorySpec{repo("github", "infra", RepositoryRoleGitOps), repo("gl", "infra", RepositoryRoleManaged)},
	}
	resolved, _ := in.ResolveGit()
	raw, _ := json.Marshal(resolved.BrokerForges("/creds")[0])
	if string(raw) != `{"provider":"github","host":"github.com"}` {
		t.Errorf("github entry = %s", raw)
	}
}

// Review round 3: GitLab's group grammar admits a dot, so a path or namespace
// starting with a forge host passed as a group, and the broker -- which lifts
// such a segment off as a host -- refused the rendered allowedPaths and did not
// start. A host is never a group, on any spelling the broker could lift.
func TestAForgeHostIsNeverAGitLabGroup(t *testing.T) {
	provider := gitlabProvider(t)
	for _, tc := range []struct{ host, repository, namespace string }{
		{"", "github.com/acme/infra", ""},
		{"", "gitlab.com/acme/infra", "x"},                          // lifted as the forge's own host; accepted
		{"gitlab.example.com", "gitlab.example.com/acme/infra", ""}, // lifted, accepted
		{"gitlab.example.com", "gitlab.com/acme/infra", ""},
		{"", "proj", "gitlab.com/acme"},
		{"", "proj", "github.com"},
		{"gitlab.example.com", "proj", "gitlab.example.com/acme"},
	} {
		_, err := provider.Resolve(tc.host, tc.repository, tc.namespace)
		// The forge's own host, spelled schemeless, lifts as that host.
		lifted := (tc.host == "" && tc.repository == "gitlab.com/acme/infra") ||
			(tc.host == "gitlab.example.com" && tc.repository == "gitlab.example.com/acme/infra")
		if lifted && err != nil {
			t.Errorf("Resolve(%q, %q, %q): the forge's own host should lift, got %v", tc.host, tc.repository, tc.namespace, err)
		}
		if !lifted && err == nil {
			t.Errorf("Resolve(%q, %q, %q) accepted a path starting with a forge host", tc.host, tc.repository, tc.namespace)
		}
	}
	for _, tc := range []struct{ host, namespace string }{
		{"", "gitlab.com"}, {"", "gitlab.com/acme"}, {"", "github.com"},
		{"gitlab.example.com", "gitlab.example.com"},
	} {
		if err := provider.ValidateNamespaceOn(tc.host, tc.namespace); err == nil {
			t.Errorf("ValidateNamespaceOn(%q, %q) accepted a host as a group", tc.host, tc.namespace)
		}
	}
	// A dotted group that is not a host stays a group.
	if _, err := provider.Resolve("", "my.group/infra", ""); err != nil {
		t.Errorf("a dotted group was refused: %v", err)
	}
	if err := provider.ValidateNamespaceOn("", "my.group/sub"); err != nil {
		t.Errorf("a dotted namespace was refused: %v", err)
	}
	// And the forge is refused for it, so nothing reaches the broker.
	in := &IntegrationSpec{Forges: []ForgeSpec{glForge("gl", "", "gitlab.com")}}
	resolved, _ := in.ResolveGit()
	if got := resolved.BrokerForges("/creds"); got != nil {
		t.Errorf("BrokerForges rendered a forge whose namespace is a host: %+v", got)
	}
}

// Review round 3: a GitLab URL longer than the broker's parser reads was
// accepted here and refused on every call there.
func TestARepositoryLongerThanTheBrokerReadsIsRefused(t *testing.T) {
	provider := gitlabProvider(t)
	group := strings.Repeat("g", 60) + "/" + strings.Repeat("h", 60) + "/" + strings.Repeat("i", 60) + "/" + strings.Repeat("j", 60)
	if _, err := provider.Resolve("", "https://gitlab.com/"+group+"/infra", ""); err == nil {
		t.Errorf("a %d-character URL was accepted", len("https://gitlab.com/"+group+"/infra"))
	}
	if _, err := provider.Resolve("", "https://gitlab.com/acme/platform/infra", ""); err != nil {
		t.Errorf("an ordinary nested URL was refused: %v", err)
	}
}

// Review round 3: a shadowed forge with a repository but no namespace was
// warned "serves nothing" on top of its host refusal.
func TestAShadowedForgeIsNotWarnedAsServingNothing(t *testing.T) {
	in := &IntegrationSpec{
		Forges:       []ForgeSpec{glForge("a", "", "acme"), glForge("b", "", "")},
		Repositories: []RepositorySpec{repo("b", "other/thing", RepositoryRoleManaged)},
	}
	resolved, _ := in.ResolveGit()
	for _, w := range resolved.Warnings() {
		if strings.Contains(w, "forges[1]") {
			t.Errorf("the shadowed forge was warned as serving nothing: %s", w)
		}
	}
}

// Review round 3: a repository on a shadowed forge claimed no URL, so a later
// entry naming the same repository -- a duplicate when the claims were measured
// -- was accepted once shadowing applied, and seeded with no broker entry
// serving it.
func TestShadowingDoesNotFreeADuplicate(t *testing.T) {
	in := &IntegrationSpec{
		Forges: []ForgeSpec{glForge("a", "", "other"), glForge("b", "", "acme"), glForge("c", "", "")},
		Repositories: []RepositorySpec{
			repo("b", "acme/x", RepositoryRoleManaged),
			repo("c", "acme/x", RepositoryRoleManaged),
		},
	}
	resolved, _ := in.ResolveGit()
	if got := resolved.Accepted(RepositoryRoleManaged); len(got) != 0 {
		t.Errorf("a repository the claims measured as a duplicate was accepted: %+v", got)
	}
}

func TestAGitLabNamespaceSegmentMayNotEndInAReservedSuffix(t *testing.T) {
	// Review round 4: GitLab refuses a path ending in .git or .atom, and the
	// broker trims a trailing .git off an allowedPaths entry, so a namespace
	// the operator accepted named a different group in the broker.
	provider := gitlabProvider(t)
	for _, bad := range []string{"acme.git", "acme/infra.git", "acme.atom/infra", "acme/INFRA.GIT"} {
		if err := provider.ValidateNamespace(bad); err == nil {
			t.Errorf("ValidateNamespace(%q) accepted a reserved suffix", bad)
		}
		if _, err := provider.Resolve("", "proj", bad); err == nil {
			t.Errorf("Resolve(proj, namespace %q) accepted a reserved suffix", bad)
		}
	}
	for _, ok := range []string{"acme", "acme/git-tools", "acme/infra.gitops"} {
		if err := provider.ValidateNamespace(ok); err != nil {
			t.Errorf("ValidateNamespace(%q) refused a valid group: %v", ok, err)
		}
	}
}
