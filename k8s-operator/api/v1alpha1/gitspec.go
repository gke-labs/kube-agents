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

// gitspec.go — the one place the two spellings of a forge declaration become one.
//
// `spec.integration.github` is a deprecated alias for `spec.integration.git`.
// Folding them here, rather than at each of the five places that read the
// integration, is what keeps the alias from being a second code path: a
// consumer calls ResolveGit and never learns which field the administrator
// wrote. See docs/designs/multi-forge-support.md §6.

import (
	"fmt"
	"strings"
)

// ResolvedGit is one declared forge repository, after the deprecated GitHub
// alias has been folded in and the provider defaulted.
//
// Derived from the spec rather than part of it.
// +kubebuilder:object:generate=false
type ResolvedGit struct {
	// Provider is the registered provider name, never empty.
	Provider string
	// Host is the declared host, empty for the provider's default.
	Host string
	// Repository is as declared: a URL, an scp remote, a path, or a bare name.
	// Empty, or the NoRepositorySentinel, means none was declared.
	Repository string
	// Namespace is the declared owning organisation, user, or group path.
	Namespace string
	// FromDeprecatedAlias records that this came from `spec.integration.github`,
	// so a warning can name the field the administrator actually wrote.
	FromDeprecatedAlias bool
}

// ResolveGit folds the two forge fields into one declaration.
//
// It returns (nil, nil) when neither field is set, which is a valid
// PlatformAgent: repositories can be registered in the gitops-state ConfigMap
// instead. It returns an error when both are set, because there is no
// precedence rule that would not surprise somebody.
func (in *IntegrationSpec) ResolveGit() (*ResolvedGit, error) {
	if in == nil {
		return nil, nil
	}
	switch {
	case in.Git != nil && in.GitHub != nil:
		return nil, fmt.Errorf("set at most one of integration.git and integration.github; " +
			"integration.github is a deprecated alias for integration.git with provider " + GitProviderGitHub)
	case in.Git != nil:
		provider := strings.TrimSpace(in.Git.Provider)
		if provider == "" {
			provider = DefaultGitProvider
		}
		return &ResolvedGit{
			Provider:   strings.ToLower(provider),
			Host:       strings.TrimSpace(in.Git.Host),
			Repository: strings.TrimSpace(in.Git.Repository),
			Namespace:  strings.TrimSpace(in.Git.Namespace),
		}, nil
	case in.GitHub != nil:
		return &ResolvedGit{
			Provider:            GitProviderGitHub,
			Repository:          strings.TrimSpace(in.GitHub.GitRepo),
			Namespace:           strings.TrimSpace(in.GitHub.Org),
			FromDeprecatedAlias: true,
		}, nil
	default:
		return nil, nil
	}
}

// HasRepository reports whether a repository was actually declared, as opposed
// to the field being absent or holding the "no repository" sentinel.
func (r *ResolvedGit) HasRepository() bool {
	return r != nil && r.Repository != "" && r.Repository != NoRepositorySentinel
}

// GitProvider returns the registered rules for this declaration's provider.
func (r *ResolvedGit) GitProvider() (*GitProvider, error) {
	if r == nil {
		return nil, fmt.Errorf("no git integration declared")
	}
	return LookupGitProvider(r.Provider)
}

// Resolve fully qualifies the declared repository under its provider's rules.
// It is an error to call it when no repository was declared; check
// HasRepository first.
func (r *ResolvedGit) Resolve() (RepoRef, error) {
	if !r.HasRepository() {
		return RepoRef{}, fmt.Errorf("no repository declared")
	}
	provider, err := r.GitProvider()
	if err != nil {
		return RepoRef{}, err
	}
	return provider.Resolve(r.Host, r.Repository, r.Namespace)
}

// EffectiveNamespace is the declared namespace, or the one the repository
// implies. It is empty when neither is available, which is legitimate: an
// install may declare no repository at all.
func (r *ResolvedGit) EffectiveNamespace() string {
	if r == nil {
		return ""
	}
	if r.Namespace != "" {
		return r.Namespace
	}
	ref, err := r.Resolve()
	if err != nil {
		return ""
	}
	segments := ref.Segments()
	if len(segments) < minNamespacedPathDepth {
		return ""
	}
	return strings.Join(segments[:len(segments)-1], pathSeparator)
}

// minNamespacedPathDepth is the shortest path from which a namespace can be
// read: one namespace segment and the repository name.
const minNamespacedPathDepth = 2

// Field paths in the two spellings of the declaration. Admission reports
// against the field the administrator actually wrote, not the resolved form,
// so a deprecated-alias user is not told about a field they did not set.
const (
	gitFieldRoot       = "git"
	gitHubFieldRoot    = "github"
	gitProviderField   = "provider"
	gitHostField       = "host"
	gitNamespaceField  = "namespace"
	gitHubOrgField     = "org"
	gitRepositoryField = "repository"
	gitHubRepoField    = "gitRepo"
)

// FieldPath renders the spec path of one part of this declaration —
// "provider", "host", "namespace", or "repository" — in whichever spelling it
// was written. The parts a deprecated GitHub declaration cannot express map
// onto the `github` root itself.
func (r *ResolvedGit) FieldPath(part string) []string {
	if r == nil || !r.FromDeprecatedAlias {
		return []string{gitFieldRoot, part}
	}
	switch part {
	case gitNamespaceField:
		return []string{gitHubFieldRoot, gitHubOrgField}
	case gitRepositoryField:
		return []string{gitHubFieldRoot, gitHubRepoField}
	default:
		return []string{gitHubFieldRoot}
	}
}

// ValidateNamespace applies the declared provider's namespace grammar, in place
// of the GitHub pattern the CRD schema used to apply to every forge.
func (r *ResolvedGit) ValidateNamespace() error {
	if r == nil {
		return nil
	}
	provider, err := r.GitProvider()
	if err != nil {
		return err
	}
	if err := validateDeclaredValue(gitNamespaceField, r.Namespace, MaxGitNamespaceLength); err != nil {
		return err
	}
	return provider.ValidateNamespace(r.Namespace)
}

// ValidateRepository applies the declared provider's hosts and path depth. It
// is a no-op when no repository was declared, which is a valid PlatformAgent.
func (r *ResolvedGit) ValidateRepository() error {
	if !r.HasRepository() {
		return nil
	}
	provider, err := r.GitProvider()
	if err != nil {
		return err
	}
	if err := provider.ValidateHost(r.Host); err != nil {
		return err
	}
	if err := validateDeclaredValue(gitRepositoryField, r.Repository, MaxGitRepoURLLength); err != nil {
		return err
	}
	if _, err := provider.Resolve(r.Host, r.Repository, r.Namespace); err != nil {
		return fmt.Errorf("invalid %s repository %q: %w", provider.Name, r.Repository, err)
	}
	return nil
}

// Validate applies the declared provider's own rules — its hosts, its namespace
// grammar, its path depth — in place of the host-blind shape check that used to
// stand in for all of them.
func (r *ResolvedGit) Validate() error {
	if r == nil {
		return nil
	}
	if _, err := r.GitProvider(); err != nil {
		return err
	}
	if err := r.ValidateHost(); err != nil {
		return err
	}
	if err := r.ValidateNamespace(); err != nil {
		return err
	}
	return r.ValidateRepository()
}

// ValidateHost reports a declared host that the declared provider does not
// serve. It is checked on its own as well as inside ValidateRepository, because
// a host may be declared without a repository.
func (r *ResolvedGit) ValidateHost() error {
	if r == nil {
		return nil
	}
	provider, err := r.GitProvider()
	if err != nil {
		return err
	}
	if err := validateDeclaredValue(gitHostField, r.Host, MaxGitHostLength); err != nil {
		return err
	}
	return provider.ValidateHost(r.Host)
}

// ValidateGit is ResolveGit followed by Validate — the whole check on the forge
// declaration, for the two callers (admission and reconcile) that want it as
// one call.
func (in *IntegrationSpec) ValidateGit() error {
	resolved, err := in.ResolveGit()
	if err != nil {
		return err
	}
	return resolved.Validate()
}
