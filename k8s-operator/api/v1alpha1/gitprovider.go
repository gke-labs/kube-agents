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

// gitprovider.go — what a forge states about itself, so validation can dispatch.
//
// Before this, the CRD asserted GitHub's rules without saying so: `Org` carried
// GitHub's namespace grammar in a `+kubebuilder:validation:Pattern`, and
// `GitRepo` was checked for length, non-graphic runes, and "exactly one slash
// once the host has been discarded". None of that is a host check, and none of
// it is stated as GitHub's rather than as every forge's, so there was nowhere
// for a second forge's rules to go.
//
// A `GitProvider` states four things: which hosts are its own, what its default
// host is, what a namespace may look like, and how deep a repository path may
// be. Validation is then a lookup and a dispatch, and adding a forge is adding
// a table entry rather than widening a shared check.
// `docs/designs/version-control-support.md` §6 is the design.
//
// GitHub and Gitea are registered. Each entry lands with the agent-side
// provider it needs to be honest, because a provider the CRD accepts and the
// agent discards is a worse failure than one the CRD refuses.
//
// Gitea is the first self-managed forge: it has no default host, it serves
// whatever host the administrator declares and no other, it may be reached
// over plain http when that is declared, and its credential is a token the
// administrator supplies in a Secret rather than one the minter issues.

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
)

const (
	// GitProviderGitHub is the `provider` value naming GitHub, and the `type` of
	// a `managed_repos` entry the agent has a provider for.
	GitProviderGitHub = "github"

	// GitProviderGitea is the `provider` value naming a self-managed Gitea.
	GitProviderGitea = "gitea"

	// ForgeSchemeHTTPS and ForgeSchemeHTTP are the `scheme` values a forge may
	// declare. HTTPS is the default; HTTP is accepted only where the provider
	// allows it and only when written out.
	ForgeSchemeHTTPS = "https"
	ForgeSchemeHTTP  = "http"

	// DefaultForgeScheme is assumed when a forge's `scheme` is omitted.
	DefaultForgeScheme = ForgeSchemeHTTPS

	// MinForgePort and MaxForgePort bound a forge's declared `port`.
	MinForgePort = 1
	MaxForgePort = 65535

	// ForgeTokenSecretKey is the key a self-managed forge's credentialsRef
	// Secret holds its API token under.
	ForgeTokenSecretKey = "token" // #nosec G101 -- Secret key name, not a credential

	// MaxGiteaNamespaceLength is Gitea's limit on a user or organisation name.
	MaxGiteaNamespaceLength = 40

	// giteaPathDepth is Gitea's rule: a repository is exactly `owner/name`.
	giteaPathDepth = 2

	// DefaultGitProvider is assumed when a forge's `provider` is
	// omitted, and is what the deprecated `spec.integration.github` alias means.
	DefaultGitProvider = GitProviderGitHub

	// MaxGitHostLength bounds a forge's `host` at the DNS limit.
	MaxGitHostLength = 253

	// MaxGitNamespaceLength bounds a forge's or repository's `namespace` in the CRD
	// schema. It is deliberately looser than any provider's own limit — a nested
	// GitLab group path is longer than a GitHub org — because the tight bound is
	// the provider's to apply, and a schema pattern cannot dispatch on a sibling
	// field.
	MaxGitNamespaceLength = 255

	// githubPathDepth is GitHub's rule: a repository is exactly `owner/name`.
	githubPathDepth = 2
)

// githubHosts is every spelling of GitHub that can appear in a remote this
// install produces. `ssh.github.com` is the SSH-over-443 endpoint. An
// enterprise host is absent on purpose: Minty issues tokens for github.com
// installations only.
var githubHosts = map[string]bool{
	"github.com":     true,
	"www.github.com": true,
	"ssh.github.com": true,
}

// GitProvider is one forge's rules — the unit validation dispatches on.
//
// Not part of the API surface: it is a compiled-in table, not a CRD field, and
// its regexp member has no DeepCopy.
// +kubebuilder:object:generate=false
type GitProvider struct {
	// Name is the `provider` value, and the `type` written into the
	// `managed_repos` state ConfigMap.
	Name string
	// DefaultHost is assumed for a repository that names no host.
	DefaultHost string
	// Hosts are the spellings a repository may name. A repository naming a host
	// outside this set is rejected rather than rewritten, which is the defect
	// repo_ref.go's header describes.
	Hosts map[string]bool
	// NamespacePattern is the forge's grammar for an owning organisation, user,
	// or group path.
	NamespacePattern *regexp.Regexp
	// MaxNamespaceLength bounds a namespace under this forge's own rules.
	MaxNamespaceLength int
	// MinPathDepth and MaxPathDepth bound the repository path in segments.
	// MaxPathDepth of 0 means unbounded, for a forge with nested groups.
	MinPathDepth int
	MaxPathDepth int
	// Egress are the FQDN patterns the provider's traffic needs: its API,
	// its clone endpoints, and any host it serves content from. They cover
	// every entry in Hosts, so a declaration naming one of those adds nothing.
	Egress []string
	// SelfManaged marks a forge an administrator runs at a hostname of their
	// choosing. It has no DefaultHost, so a declaration must name a host; any
	// DNS name outside the hosted providers' reserved domains is accepted, and
	// a repository may name that host and no other. A port may be declared,
	// and the credential is administrator-supplied, so a declaration without
	// credentialsRef is refused.
	SelfManaged bool
	// Schemes are the `scheme` values a declaration may use. An empty scheme
	// is always DefaultForgeScheme, which every provider must list.
	Schemes map[string]bool
}

// giteaNamespaceRegex is Gitea's user and organisation name grammar: letters,
// digits, `_`, `-` and `.`, not starting or ending with `-` or `.`.
var giteaNamespaceRegex = regexp.MustCompile(`^[A-Za-z0-9_]([A-Za-z0-9_.-]*[A-Za-z0-9_])?$`)

// selfManagedHostRegex is a lowercase DNS name, the only kind of host a
// self-managed forge declaration names. An address literal is refused: the
// egress policy is FQDN-based and could not admit it.
var selfManagedHostRegex = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$`)

// reservedHostedForgeDomains are well-known hosted forge domains that a
// self-managed provider may not claim as its host.
var reservedHostedForgeDomains = map[string]string{
	"github.com":     GitProviderGitHub,
	"www.github.com": GitProviderGitHub,
	"ssh.github.com": GitProviderGitHub,
	"gitlab.com":     "gitlab",
	"www.gitlab.com": "gitlab",
}

// gitProviders is the registry. Adding a forge is adding an entry here and the
// agent-side provider that honours it.
var gitProviders = map[string]*GitProvider{
	GitProviderGitHub: {
		Name:               GitProviderGitHub,
		DefaultHost:        "github.com",
		Hosts:              githubHosts,
		NamespacePattern:   githubOrgRegex,
		MaxNamespaceLength: MaxGitHubOrgLength,
		MinPathDepth:       githubPathDepth,
		MaxPathDepth:       githubPathDepth,
		// raw.githubusercontent.com and the release/archive download hosts
		// sit under githubusercontent.com; api.github.com and codeload under
		// the wildcard.
		Egress:  []string{"github.com", "*.github.com", "*.githubusercontent.com"},
		Schemes: map[string]bool{ForgeSchemeHTTPS: true},
	},
	GitProviderGitea: {
		Name:               GitProviderGitea,
		NamespacePattern:   giteaNamespaceRegex,
		MaxNamespaceLength: MaxGiteaNamespaceLength,
		MinPathDepth:       giteaPathDepth,
		MaxPathDepth:       giteaPathDepth,
		// The API and the clone endpoint are both on the declared host,
		// which EgressPatterns adds as a literal.
		SelfManaged: true,
		Schemes:     map[string]bool{ForgeSchemeHTTPS: true, ForgeSchemeHTTP: true},
	},
}

// GitProviderNames lists the registered providers in sorted order, for an error
// message that tells an administrator what they could have written instead.
func GitProviderNames() []string {
	return providerNames(gitProviders)
}

func providerNames(table map[string]*GitProvider) []string {
	names := make([]string, 0, len(table))
	for name := range table {
		names = append(names, name)
	}
	// Sorted so the message is stable across runs; a map range order is not.
	sort.Strings(names)
	return names
}

// LookupGitProvider returns the rules for a declared provider name. An empty
// name means the default, which is what the deprecated GitHub alias resolves to.
func LookupGitProvider(name string) (*GitProvider, error) {
	return lookupGitProvider(name, gitProviders)
}

func lookupGitProvider(name string, table map[string]*GitProvider) (*GitProvider, error) {
	trimmed := strings.TrimSpace(name)
	if trimmed == "" {
		trimmed = DefaultGitProvider
	}
	provider, ok := table[strings.ToLower(trimmed)]
	if !ok {
		return nil, fmt.Errorf("unsupported git provider %q; must be one of %s",
			name, strings.Join(providerNames(table), ", "))
	}
	return provider, nil
}

// ValidateHost reports whether a declared host is one this provider serves.
// An empty host is the provider's default, allowed wherever there is one. A
// self-managed forge has none, so it requires a host, and serves any DNS name
// outside the hosted providers' reserved domains.
func (p *GitProvider) ValidateHost(host string) error {
	trimmed := lowerASCII(strings.TrimSpace(host))
	if trimmed == "" {
		if p.DefaultHost == "" {
			return fmt.Errorf("%s has no default host; host is required", p.Name)
		}
		return nil
	}
	if p.SelfManaged {
		if !selfManagedHostRegex.MatchString(trimmed) {
			return fmt.Errorf("host %q is not a DNS name", host)
		}
		if owner, reserved := reservedHostedForgeDomains[trimmed]; reserved {
			return fmt.Errorf("host %q is a hosted %s domain and cannot be used for self-managed provider %s", host, owner, p.Name)
		}
		return nil
	}
	if !p.Hosts[trimmed] {
		return fmt.Errorf("host %q is not a %s host", host, p.Name)
	}
	return nil
}

// ValidateScheme reports whether a declared scheme is one this provider may be
// reached over. An empty scheme is DefaultForgeScheme.
func (p *GitProvider) ValidateScheme(scheme string) error {
	trimmed := lowerASCII(strings.TrimSpace(scheme))
	if trimmed == "" || p.Schemes[trimmed] {
		return nil
	}
	allowed := make([]string, 0, len(p.Schemes))
	for s := range p.Schemes {
		allowed = append(allowed, s)
	}
	sort.Strings(allowed)
	return fmt.Errorf("scheme %q is not supported for %s; must be one of %s",
		scheme, p.Name, strings.Join(allowed, ", "))
}

// ValidatePort reports whether a declared port is allowed. Zero means none
// was declared. Only a self-managed forge may declare one: a hosted forge's
// endpoints are fixed, and a port on them would be a clone URL nobody serves.
func (p *GitProvider) ValidatePort(port int32) error {
	if port == 0 {
		return nil
	}
	if !p.SelfManaged {
		return fmt.Errorf("%s serves fixed endpoints; port may not be set", p.Name)
	}
	if port < MinForgePort || port > MaxForgePort {
		return fmt.Errorf("port %d is outside %d-%d", port, MinForgePort, MaxForgePort)
	}
	return nil
}

// canonicalHost is the host a declaration on host resolves to: the declared
// one on a self-managed forge, DefaultHost on any other, which every spelling
// in Hosts folds to.
func (p *GitProvider) canonicalHost(host string) string {
	if trimmed := lowerASCII(strings.TrimSpace(host)); p.SelfManaged && trimmed != "" {
		return trimmed
	}
	return p.DefaultHost
}

// ValidateNamespace applies this forge's grammar to an owning organisation,
// user, or group path. An empty namespace is allowed: it may be inferable from
// the repository, and the caller decides whether it had to be present.
func (p *GitProvider) ValidateNamespace(namespace string) error {
	trimmed := strings.TrimSpace(namespace)
	if trimmed == "" {
		return nil
	}
	if len([]rune(trimmed)) > p.MaxNamespaceLength {
		return fmt.Errorf("%s namespace exceeds maximum length of %d characters",
			p.Name, p.MaxNamespaceLength)
	}
	if !p.NamespacePattern.MatchString(trimmed) {
		return fmt.Errorf("invalid %s namespace %q", p.Name, trimmed)
	}
	return nil
}

// Resolve turns a declared repository into a fully-qualified ref under this
// provider's rules: the declared host, or this provider's default; a namespace
// supplied from the declaration when the repository gave only a bare name; and
// a path this forge admits.
//
// It refuses a host belonging to another forge rather than discarding it. That
// refusal is the point of the function — the code it replaces stripped the host
// and then counted slashes, so a GitLab remote became a GitHub repository.
//
// The returned host is the canonical one, not whichever spelling was used,
// because everything downstream compares hosts by string. Every entry in Hosts
// is an alternative spelling of DefaultHost, so a host from either source — the
// repository or the declaration — folds to DefaultHost. Declaring
// a forge `host: ssh.github.com` and writing it inside the repository URL therefore
// agree, where before the declared spelling was carried through verbatim and
// seeded a clone URL git cannot fetch over HTTPS.
//
// On a hosted forge ValidateHost admits only a host in Hosts, so every host
// resolves to DefaultHost. On a self-managed forge the canonical host is the
// declared one, and a repository naming any other host is refused: the
// declared host never replaces one the repository names.
//
// The namespace this produces is checked against the provider's grammar
// wherever it came from. Checking only the declared `namespace` field would
// leave `gitlab.com/project` resolving to `https://github.com/gitlab.com/project`
// — a namespace GitHub's own rules reject, arriving as a repository path
// segment instead of as the field the rules are attached to.
func (p *GitProvider) Resolve(host, repository, namespace string) (RepoRef, error) {
	if err := p.ValidateHost(host); err != nil {
		return RepoRef{}, err
	}
	canonical := p.canonicalHost(host)

	// Every spelling of this provider's host lifts out of a schemeless path,
	// not just DefaultHost. The parser this replaces stripped both
	// `github.com/` and `www.github.com/`, and a CR written either way has to
	// keep resolving after an operator upgrade; none of these spellings can be
	// a namespace, because each contains a dot and GitHub's owner grammar
	// allows none. repo_ref.py's KNOWN_HOSTS is narrower, and the Go side is
	// the one bound by what the CRD already admitted.
	ref, err := parseRepoRef(repository, p.schemelessHosts(canonical))
	if err != nil {
		return RepoRef{}, err
	}
	if ref.Host != "" && ref.Host != canonical && !p.Hosts[ref.Host] {
		return RepoRef{}, fmt.Errorf("repository %q names host %q, which is not a %s host",
			repository, ref.Host, p.Name)
	}
	// Only a bare name is qualified by the namespace. A value that names a
	// host and one segment, such as an organisation URL, is a URL missing its
	// repository, and the depth check below refuses it as that.
	bare := ref.Host == ""
	// `github.com/` is a host and nothing else, refused as empty in its other
	// spellings (`https://github.com/`). Its trailing separator is what says
	// so: without one, `github.com` is a repository name GitHub allows, and
	// `/github.com` and `github.com.git/` spell that name as `/infra` and
	// `infra.git/` spell `infra`.
	if raw := strings.TrimSpace(repository); bare && strings.HasSuffix(raw, pathSeparator) &&
		p.schemelessHosts(canonical)[lowerASCII(strings.Trim(raw, pathSeparator))] {
		return RepoRef{}, fmt.Errorf("repository %q names the host %q and no repository", repository, strings.Trim(raw, pathSeparator))
	}
	// A dotted first segment the namespace grammar refuses is a host, most
	// often another forge's (`gitlab.com/group/project`). Said here, the
	// refusal names it; left to the depth check, it would send the
	// administrator to shorten the path instead.
	if first, _, isPath := strings.Cut(ref.Path, pathSeparator); bare && isPath && strings.Contains(first, ".") && p.ValidateNamespace(first) != nil {
		return RepoRef{}, fmt.Errorf("repository %q starts with %q, which reads as a host %s does not serve; a repository on another forge needs a forge of its own",
			repository, first, p.Name)
	}
	ref.Host = canonical

	if bare && !strings.Contains(ref.Path, pathSeparator) {
		trimmed := strings.TrimSpace(namespace)
		if trimmed == "" {
			return RepoRef{}, fmt.Errorf("repository %q names no namespace and none was declared", repository)
		}
		ref.Path = strings.Trim(trimmed, pathSeparator) + pathSeparator + ref.Path
	}

	segments := ref.Segments()
	for _, segment := range segments {
		if !safeRepoSegment(segment) {
			return RepoRef{}, fmt.Errorf("invalid repository path segment %q", segment)
		}
	}
	if len(segments) < p.MinPathDepth {
		return RepoRef{}, fmt.Errorf("repository %q has %d path segments; %s requires at least %d",
			repository, len(segments), p.Name, p.MinPathDepth)
	}
	if p.MaxPathDepth > 0 && len(segments) > p.MaxPathDepth {
		return RepoRef{}, fmt.Errorf("repository %q has %d path segments; %s allows at most %d",
			repository, len(segments), p.Name, p.MaxPathDepth)
	}
	resolvedNamespace := strings.Join(segments[:len(segments)-1], pathSeparator)
	if err := p.ValidateNamespace(resolvedNamespace); err != nil {
		return RepoRef{}, fmt.Errorf("repository %q resolves to %w", repository, err)
	}
	return ref, nil
}

// ParseRepoRef reads a repository as Resolve does before it applies any of
// this provider's rules: a schemeless first segment spelling one of its hosts
// is lifted out, so `github.com//o/r` reads as host github.com and path
// `o/r`, as it does to the agent. The package-level ParseRepoRef lifts none.
// A self-managed forge has no host of its own until one is declared, so it
// lifts none here either.
func (p *GitProvider) ParseRepoRef(value string) (RepoRef, error) {
	return parseRepoRef(value, p.schemelessHosts(p.DefaultHost))
}

// schemelessHosts is the canonical host plus every alternative spelling in
// Hosts. The canonical host is DefaultHost on a hosted forge and the declared
// host on a self-managed one; an empty one lifts nothing.
func (p *GitProvider) schemelessHosts(canonical string) map[string]bool {
	hosts := map[string]bool{}
	if canonical != "" {
		hosts[canonical] = true
	}
	for host := range p.Hosts {
		hosts[host] = true
	}
	return hosts
}

// EgressPatterns returns the FQDN patterns a declaration on host needs. A host
// this provider serves, or none, is covered by the provider's own patterns; any
// other host is added as a literal, which is the case a self-managed forge at a
// customer-chosen hostname needs and no pattern in this repository could cover.
func (p *GitProvider) EgressPatterns(host string) []string {
	patterns := append([]string(nil), p.Egress...)
	trimmed := strings.ToLower(strings.TrimSpace(host))
	if trimmed == "" || p.Hosts[trimmed] {
		return patterns
	}
	return append(patterns, trimmed)
}
