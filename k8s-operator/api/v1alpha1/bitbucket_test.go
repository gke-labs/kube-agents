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
	"strings"
	"testing"
)

// releaseBitbucket lifts this release's refusal of the two Bitbucket
// providers for one test, so that the rules of their table entries can be
// tested as the release that wires the fleet-audit ledger will use them. No
// test in this package runs in parallel, so the swap is safe.
func releaseBitbucket(t *testing.T) {
	t.Helper()
	saved := unreleasedGitProviders
	unreleasedGitProviders = map[string]string{}
	t.Cleanup(func() { unreleasedGitProviders = saved })
}

func bitbucketProvider(t *testing.T, name string) *GitProvider {
	t.Helper()
	provider, err := lookupGitProvider(name, gitProviders)
	if err != nil {
		t.Fatalf("lookupGitProvider(%s) = %v", name, err)
	}
	return provider
}

// bbForge is a Bitbucket Cloud forge with a credential, which every valid one has.
func bbForge(name, namespace string) ForgeSpec {
	return ForgeSpec{
		Name: name, Provider: GitProviderBitbucket, Namespace: namespace,
		CredentialsRef: &ForgeCredentialsRef{Name: name + "-token"},
	}
}

// dcForge is a Bitbucket Data Center forge with a credential.
func dcForge(name, host, namespace string) ForgeSpec {
	return ForgeSpec{
		Name: name, Provider: GitProviderBitbucketDataCenter, Host: host, Namespace: namespace,
		CredentialsRef: &ForgeCredentialsRef{Name: name + "-token"},
	}
}

// The gate: both providers are registered and in the CRD enum, and this
// release refuses each of them by name, at the provider field. A refused
// forge gets no egress and no broker entry, so nothing reaches Bitbucket.
func TestThisReleaseRefusesBothBitbucketProviders(t *testing.T) {
	for _, name := range []string{GitProviderBitbucket, GitProviderBitbucketDataCenter, " Bitbucket "} {
		_, err := LookupGitProvider(name)
		if err == nil || !strings.Contains(err.Error(), "not available in this release") {
			t.Errorf("LookupGitProvider(%q) = %v, expected the release refusal", name, err)
		}
	}
	for _, name := range []string{GitProviderGitHub, GitProviderGitLab, ""} {
		if _, err := LookupGitProvider(name); err != nil {
			t.Errorf("LookupGitProvider(%q) = %v, expected the provider", name, err)
		}
	}
	// What an administrator is told to write instead names only what this
	// release serves.
	if _, err := LookupGitProvider("gitea"); err == nil || strings.Contains(err.Error(), "bitbucket") ||
		!strings.Contains(err.Error(), "must be one of github, gitlab") {
		t.Errorf("LookupGitProvider(gitea) = %v, expected the list without the refused providers", err)
	}
	spec := &IntegrationSpec{
		Forges: []ForgeSpec{bbForge("bb", "acme"), dcForge("dc", "bitbucket.example.com", "ACME")},
		Repositories: []RepositorySpec{
			repo("bb", "infra", RepositoryRoleGitOps),
			repo("dc", "apps", RepositoryRoleManaged),
		},
	}
	resolved, err := spec.ResolveGit()
	if err != nil {
		t.Fatal(err)
	}
	paths := problemPaths(resolved)
	for _, path := range []string{"forges[0].provider", "forges[1].provider"} {
		if !strings.Contains(paths[path], "not available in this release") {
			t.Errorf("Problems()[%s] = %q, expected the release refusal", path, paths[path])
		}
	}
	if len(paths) != 2 {
		t.Errorf("Problems() = %v, expected the two provider refusals only", paths)
	}
	if forges := resolved.BrokerForges("/t", "/c"); forges != nil {
		t.Errorf("BrokerForges() = %+v, expected none for refused forges", forges)
	}
	for _, pattern := range ForgeEgressPatterns(spec) {
		if strings.Contains(pattern, "bitbucket") {
			t.Errorf("ForgeEgressPatterns() has %q for a refused forge", pattern)
		}
	}
	if got := resolved.Accepted(RepositoryRoleGitOps); len(got) != 0 {
		t.Errorf("Accepted(gitops) = %v, expected none on a refused forge", got)
	}
}

func TestBitbucketCloudResolvesWorkspaceAndRepository(t *testing.T) {
	releaseBitbucket(t)
	provider := bitbucketProvider(t, GitProviderBitbucket)
	for repository, want := range map[string]string{
		"https://bitbucket.org/acme/infra.git": "https://bitbucket.org/acme/infra",
		"https://www.bitbucket.org/acme/infra": "https://bitbucket.org/acme/infra",
		"git@bitbucket.org:acme/infra.git":     "https://bitbucket.org/acme/infra",
		"bitbucket.org/acme/infra":             "https://bitbucket.org/acme/infra",
		"acme/infra":                           "https://bitbucket.org/acme/infra",
		"infra":                                "https://bitbucket.org/acme/infra",
	} {
		ref, err := provider.Resolve("", repository, "acme")
		if err != nil {
			t.Errorf("Resolve(%q) = %v", repository, err)
			continue
		}
		if ref.URL() != want {
			t.Errorf("Resolve(%q) = %q, expected %q", repository, ref.URL(), want)
		}
	}
	for _, repository := range []string{
		"acme/team/infra",                   // a project is not in a Cloud path
		"https://github.com/acme/infra",     // another forge's host
		"https://gitlab.com/acme/infra",     // another forge's host
		"https://bitbucket.example.com/a/b", // a host Cloud does not serve
		"Acme/infra",                        // a workspace ID is lowercase
	} {
		if ref, err := provider.Resolve("", repository, ""); err == nil {
			t.Errorf("Resolve(%q) = %q, expected a refusal", repository, ref.URL())
		}
	}
	if err := provider.ValidateNamespace("acme_team-2"); err != nil {
		t.Errorf("a workspace ID was refused: %v", err)
	}
	for _, namespace := range []string{"acme/team", "-acme", "acme.team", "ACME"} {
		if err := provider.ValidateNamespace(namespace); err == nil {
			t.Errorf("ValidateNamespace(%q) accepted a value that is not a workspace ID", namespace)
		}
	}
}

// Bitbucket Cloud is one public host: no self-managed instance, no CA
// bundle, and a credential the administrator supplies.
func TestBitbucketCloudIsOneHostWithACredential(t *testing.T) {
	releaseBitbucket(t)
	provider := bitbucketProvider(t, GitProviderBitbucket)
	for _, host := range []string{"", "bitbucket.org", "WWW.Bitbucket.org"} {
		if err := provider.ValidateHost(host); err != nil {
			t.Errorf("ValidateHost(%q) = %v", host, err)
		}
	}
	for _, host := range []string{"bitbucket.example.com", "api.bitbucket.org", "github.com"} {
		if err := provider.ValidateHost(host); err == nil {
			t.Errorf("ValidateHost(%q) accepted a host Bitbucket Cloud does not serve", host)
		}
	}
	for name, tc := range map[string]struct {
		forge ForgeSpec
		path  string
		want  string
	}{
		"no credential": {ForgeSpec{Name: "bb", Provider: GitProviderBitbucket, Namespace: "acme"},
			"forges[0].credentialsRef", "needs credentialsRef"},
		"a CA bundle": {func() ForgeSpec {
			f := bbForge("bb", "acme")
			f.CABundleRef = &ForgeCABundleRef{Name: "bb-ca"}
			return f
		}(), "forges[0].caBundleRef", "does not read a CA bundle"},
		"another host": {func() ForgeSpec {
			f := bbForge("bb", "acme")
			f.Host = "bitbucket.example.com"
			return f
		}(), "forges[0].host", "is not a bitbucket host"},
	} {
		resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{tc.forge}}).ResolveGit()
		if got := problemPaths(resolved)[tc.path]; !strings.Contains(got, tc.want) {
			t.Errorf("%s: Problems()[%s] = %q, expected %q", name, tc.path, got, tc.want)
		}
	}
}

func TestBitbucketDataCenterNeedsItsHost(t *testing.T) {
	releaseBitbucket(t)
	provider := bitbucketProvider(t, GitProviderBitbucketDataCenter)
	if err := provider.ValidateHost(""); err == nil || !strings.Contains(err.Error(), "no default host") {
		t.Errorf("ValidateHost(\"\") = %v, expected the missing host named", err)
	}
	if _, err := provider.Resolve("", "ACME/infra", ""); err == nil {
		t.Error("a Data Center repository resolved with no host")
	}
	if err := provider.ValidateHost("Bitbucket.Example.com"); err != nil {
		t.Errorf("ValidateHost(a self-managed host) = %v", err)
	}
	resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{dcForge("dc", "", "ACME")}}).ResolveGit()
	if got := problemPaths(resolved)["forges[0].host"]; !strings.Contains(got, "no default host") {
		t.Errorf("Problems()[forges[0].host] = %q, expected the missing host named", got)
	}
}

// D9: the root path on port 443 only. A port or a context path is refused by
// name, so the administrator learns it is not supported, not that the value
// is malformed. The rule is every provider's, because the broker reaches
// every forge that way.
func TestAPortOrAContextPathIsRefusedByName(t *testing.T) {
	releaseBitbucket(t)
	for _, name := range []string{GitProviderBitbucketDataCenter, GitProviderGitLab, GitProviderGitHub} {
		provider := bitbucketProvider(t, name)
		for _, host := range []string{"bitbucket.example.com:7990", "bitbucket.example.com/bitbucket", "https://bitbucket.example.com"} {
			err := provider.ValidateHost(host)
			if err == nil || !strings.Contains(err.Error(), "names a port or a path") {
				t.Errorf("%s: ValidateHost(%q) = %v, expected the port-or-path refusal", name, host, err)
			}
		}
	}
}

// A Data Center host is never a host another provider serves or Atlassian
// operates, and no other self-managed provider takes bitbucket.org.
func TestBitbucketHostsAreNeverAnotherProvidersInstance(t *testing.T) {
	releaseBitbucket(t)
	dc := bitbucketProvider(t, GitProviderBitbucketDataCenter)
	for _, host := range []string{
		"bitbucket.org", "www.bitbucket.org", "api.bitbucket.org", "acme.bitbucket.io",
		"acme.atlassian.net", "bitbucket.atlassian.com", "github.com", "gitlab.com", "acme.ghe.com",
	} {
		if err := dc.ValidateHost(host); err == nil {
			t.Errorf("a bitbucket-datacenter forge was accepted at %q", host)
		}
	}
	for _, name := range []string{GitProviderGitHub, GitProviderGitLab} {
		provider := bitbucketProvider(t, name)
		for _, host := range []string{"bitbucket.org", "api.bitbucket.org", "acme.atlassian.net"} {
			if err := provider.ValidateHost(host); err == nil {
				t.Errorf("a %s forge was accepted at the Bitbucket host %q", name, host)
			}
		}
	}
}

func TestBitbucketDataCenterResolvesProjectKeyAndRepository(t *testing.T) {
	releaseBitbucket(t)
	provider := bitbucketProvider(t, GitProviderBitbucketDataCenter)
	const host = "bitbucket.example.com"
	for repository, want := range map[string]string{
		"ACME/infra": "https://bitbucket.example.com/ACME/infra",
		"infra":      "https://bitbucket.example.com/ACME/infra",
		"https://bitbucket.example.com/ACME/infra": "https://bitbucket.example.com/ACME/infra",
		"bitbucket.example.com/acme_2/infra":       "https://bitbucket.example.com/acme_2/infra",
	} {
		ref, err := provider.Resolve(host, repository, "ACME")
		if err != nil {
			t.Errorf("Resolve(%q) = %v", repository, err)
			continue
		}
		if ref.URL() != want {
			t.Errorf("Resolve(%q) = %q, expected %q", repository, ref.URL(), want)
		}
	}
	for repository, want := range map[string]string{
		"https://bitbucket.example.com/scm/ACME/infra.git":    "clone or web URL",
		"https://bitbucket.example.com/projects/ACME/repos/x": "clone or web URL",
		"https://bitbucket.example.com/users/jdoe/repos/x":    "clone or web URL",
		"USERS/tools":                          "a word of the bitbucket-datacenter URL layout",
		"scm/tools":                            "a word of the bitbucket-datacenter URL layout",
		"ACME/team/infra":                      "at most 2",
		"2ACME/infra":                          "invalid bitbucket-datacenter namespace",
		"https://other.example.com/ACME/infra": "not a bitbucket-datacenter host",
		"https://bitbucket.org/acme/infra":     "not a bitbucket-datacenter host",
	} {
		_, err := provider.Resolve(host, repository, "")
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("Resolve(%q) = %v, expected a refusal with %q", repository, err, want)
		}
	}
}

func TestBitbucketEgress(t *testing.T) {
	releaseBitbucket(t)
	cloud := bitbucketProvider(t, GitProviderBitbucket)
	if got := strings.Join(cloud.EgressPatterns("www.bitbucket.org"), ","); got != "bitbucket.org,*.bitbucket.org" {
		t.Errorf("Cloud EgressPatterns = %s", got)
	}
	dc := bitbucketProvider(t, GitProviderBitbucketDataCenter)
	if got := strings.Join(dc.EgressPatterns("Bitbucket.Example.com"), ","); got != "bitbucket.example.com" {
		t.Errorf("Data Center EgressPatterns = %s", got)
	}
	spec := &IntegrationSpec{
		Forges: []ForgeSpec{bbForge("bb", "acme"), dcForge("dc", "bitbucket.example.com", "ACME")},
	}
	got := strings.Join(ForgeEgressPatterns(spec), ",")
	for _, want := range []string{"bitbucket.org", "*.bitbucket.org", "bitbucket.example.com", "github.com"} {
		if !strings.Contains(","+got+",", ","+want+",") {
			t.Errorf("ForgeEgressPatterns = %s, expected %s in it", got, want)
		}
	}
}

// The broker is given each Bitbucket forge with its workspaces or project
// keys as allowedPaths, its token and read-token paths, its CA (Data Center
// only) and its identity.
func TestBrokerForgesCarriesBothBitbucketProviders(t *testing.T) {
	releaseBitbucket(t)
	cloud := bbForge("bb", "acme")
	cloud.Identity = "{0f3b2c1d-aaaa-bbbb-cccc-0123456789ab}"
	dc := dcForge("dc", "Bitbucket.Example.com", "")
	dc.CABundleRef = &ForgeCABundleRef{Name: "dc-ca"}
	dc.Identity = "svc-agent@example.com"
	spec := &IntegrationSpec{
		Forges: []ForgeSpec{cloud, dc},
		Repositories: []RepositorySpec{
			repo("bb", "infra", RepositoryRoleGitOps),
			repo("bb", "tools/docs", RepositoryRoleContext),
			repo("dc", "OPS/apps", RepositoryRoleManaged),
		},
	}
	resolved, err := spec.ResolveGit()
	if err != nil {
		t.Fatal(err)
	}
	if problems := resolved.Problems(); len(problems) != 0 {
		t.Fatalf("Problems() = %v, expected none", problems)
	}
	forges := resolved.BrokerForges("/t", "/c")
	if len(forges) != 3 {
		t.Fatalf("BrokerForges() = %+v, expected GitHub and the two Bitbucket forges", forges)
	}
	data, _ := json.Marshal(forges[1:])
	want := `[{"provider":"bitbucket","host":"bitbucket.org","tokenPath":"/t/bb/token","readTokenPath":"/t/bb/read-token","allowedPaths":["acme","tools"],"identity":"{0f3b2c1d-aaaa-bbbb-cccc-0123456789ab}"},` +
		`{"provider":"bitbucket-datacenter","host":"bitbucket.example.com","tokenPath":"/t/dc/token","readTokenPath":"/t/dc/read-token","caSecret":"dc-ca","caKey":"ca.crt","caFile":"/c/dc/ca.crt","allowedPaths":["OPS"],"identity":"svc-agent@example.com"}]`
	if string(data) != want {
		t.Errorf("BrokerForges()[1:] =\n%s\nexpected\n%s", data, want)
	}
	entries := map[string]string{}
	for _, r := range resolved.Repositories {
		entry, err := r.ManagedRepoEntry()
		if err != nil {
			t.Fatalf("ManagedRepoEntry(%s) = %v", r.Repository, err)
		}
		entries[entry.URL] = entry.Type
	}
	if entries["https://bitbucket.org/acme/infra"] != GitProviderBitbucket ||
		entries["https://bitbucket.example.com/OPS/apps"] != GitProviderBitbucketDataCenter {
		t.Errorf("managed entries = %v, expected each typed by its provider", entries)
	}
}

// The broker holds one credential per host, so a second Bitbucket Cloud
// forge is refused at its host: a repository token reaches one repository,
// and an install with two needs a workspace token or an account's API token.
func TestASecondBitbucketCloudForgeIsShadowed(t *testing.T) {
	releaseBitbucket(t)
	spec := &IntegrationSpec{
		Forges:       []ForgeSpec{bbForge("one", "acme"), bbForge("two", "other")},
		Repositories: []RepositorySpec{repo("one", "infra", RepositoryRoleGitOps), repo("two", "apps", RepositoryRoleManaged)},
	}
	resolved, _ := spec.ResolveGit()
	if got := problemPaths(resolved)["forges[1].host"]; !strings.Contains(got, "one credential per host") {
		t.Errorf("Problems()[forges[1].host] = %q, expected the one-credential-per-host refusal", got)
	}
	if forges := resolved.BrokerForges("/t", "/c"); len(forges) != 2 || forges[1].Name != "one" {
		t.Errorf("BrokerForges() = %+v, expected GitHub and the first Bitbucket forge only", forges)
	}
}

// identity is read by the Bitbucket brokers only, and holds the broker's rule.
func TestIdentityIsForBitbucketForgesOnly(t *testing.T) {
	releaseBitbucket(t)
	gitlab := glForge("gl", "", "acme")
	gitlab.Identity = "agent"
	github := ghForge("github", "acme")
	github.Identity = "agent"
	bad := bbForge("bb", "acme")
	bad.Identity = "two words"
	notUUID := bbForge("bb", "acme")
	notUUID.Identity = "jdoe"
	long := dcForge("dc", "bitbucket.example.com", "ACME")
	long.Identity = strings.Repeat("a", MaxForgeIdentityLength+1)
	for name, tc := range map[string]struct {
		forge ForgeSpec
		want  string
	}{
		"gitlab":              {gitlab, "does not read identity"},
		"github":              {github, "does not read identity"},
		"a space":             {bad, "is not one login"},
		"not a UUID on Cloud": {notUUID, "is not an account UUID"},
		"too long":            {long, "is not one login"},
	} {
		resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{tc.forge}}).ResolveGit()
		if got := problemPaths(resolved)["forges[0].identity"]; !strings.Contains(got, tc.want) {
			t.Errorf("%s: Problems()[forges[0].identity] = %q, expected %q", name, got, tc.want)
		}
	}
	// A forge refused for its identity is not served: no broker entry, so no
	// token goes out under a login the broker would not compare with.
	refused, _ := (&IntegrationSpec{Forges: []ForgeSpec{bad},
		Repositories: []RepositorySpec{repo("bb", "infra", RepositoryRoleGitOps)}}).ResolveGit()
	if forges := refused.BrokerForges("/t", "/c"); forges != nil {
		t.Errorf("BrokerForges() = %+v, expected none for a forge refused for its identity", forges)
	}
	// A Cloud UUID is accepted with its braces or without them; the broker
	// writes it with them.
	for _, uuid := range []string{"{0f3b2c1d-aaaa-bbbb-cccc-0123456789ab}", "0F3B2C1D-AAAA-BBBB-CCCC-0123456789AB"} {
		cloud := bbForge("bb", "acme")
		cloud.Identity = uuid
		resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{cloud}}).ResolveGit()
		if got := problemPaths(resolved); len(got) != 0 {
			t.Errorf("Problems() = %v for the Cloud identity %q", got, uuid)
		}
	}
	good := dcForge("dc", "bitbucket.example.com", "ACME")
	good.Identity = "svc_agent.1@example.com"
	resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{good}}).ResolveGit()
	if got := problemPaths(resolved); len(got) != 0 {
		t.Errorf("Problems() = %v for a valid identity", got)
	}
}

// A forge at a Data Center host with no provider is told which name to use.
func TestAnUnnamedForgeAtADataCenterHostIsToldItsProvider(t *testing.T) {
	resolved, _ := (&IntegrationSpec{Forges: []ForgeSpec{{Name: "f", Host: "bitbucket.example.com", Namespace: "ACME"}}}).ResolveGit()
	if got := problemPaths(resolved)["forges[0].provider"]; !strings.Contains(got, "bitbucket-datacenter") {
		t.Errorf("Problems()[forges[0].provider] = %q, expected bitbucket-datacenter named", got)
	}
}
