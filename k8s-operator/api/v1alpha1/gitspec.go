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

// gitspec.go — the one place the two spellings of the forge declaration become one.
//
// `spec.integration.github` is a deprecated alias for one entry in
// `spec.integration.forges` and, when it names a repository, one GitOps entry
// in `spec.integration.repositories`. Folding them here, rather than at each of
// the places that read the integration, is what keeps the alias from being a
// second code path: a consumer calls ResolveGit and never learns which fields
// the administrator wrote. See docs/designs/version-control-support.md §6.

import (
	"errors"
	"fmt"
	"slices"
	"strings"
)

// ResolvedIntegration is the forge declaration after the deprecated GitHub
// alias has been folded in and every provider defaulted.
//
// Derived from the spec rather than part of it.
// +kubebuilder:object:generate=false
type ResolvedIntegration struct {
	// Forges are the declared forges, in declaration order.
	Forges []*ResolvedForge
	// Repositories are the declared repositories, in declaration order. The
	// deprecated alias contributes none when it names no repository.
	Repositories []*ResolvedRepository
	// FromDeprecatedAlias records that this came from `spec.integration.github`,
	// so an error can name the field the administrator actually wrote.
	FromDeprecatedAlias bool
}

// ResolvedForge is one declared forge.
// +kubebuilder:object:generate=false
type ResolvedForge struct {
	// Index is the position in `spec.integration.forges`, for field paths.
	Index int
	Name  string
	// Provider is the provider name as declared, lowercased, never empty. It
	// may be unregistered; Problems reports that.
	Provider string
	// Host is the declared host, empty for the provider's default.
	Host string
	// Namespace is the declared organisation, user, or group path.
	Namespace string
	// CredentialsSecret is the name credentialsRef points at, if any.
	CredentialsSecret string
}

// ResolvedRepository is one declared repository.
// +kubebuilder:object:generate=false
type ResolvedRepository struct {
	// Index is the position in `spec.integration.repositories`, for field paths.
	Index int
	// ForgeName is the forge the repository names.
	ForgeName string
	// Forge is that forge, or nil when no declared forge has the name.
	Forge *ResolvedForge
	// Repository is as declared: a URL, an scp remote, a path, or a bare name.
	Repository string
	// Namespace is the repository's own namespace override, if any.
	Namespace string
	// Role is one of the RepositoryRole constants.
	Role string
	// BaseBranch is the branch every pull request onto the repository must
	// target, as declared, or empty.
	BaseBranch string
}

// deprecatedAliasForgeName is the forge the `github` alias declares, so an
// administrator moving to the list spelling can keep the same name.
const deprecatedAliasForgeName = "github"

// ResolveGit folds the two spellings of the forge declaration into one.
//
// It returns (nil, nil) when nothing is declared, which is a valid
// PlatformAgent: repositories can be registered in the gitops-state ConfigMap
// instead. It returns an error only when both spellings are set, because there
// is no precedence rule that would not surprise somebody. Everything else —
// an unknown provider, a repository naming no declared forge — resolves, and
// Problems reports it, so admission can say which field is wrong.
func (in *IntegrationSpec) ResolveGit() (*ResolvedIntegration, error) {
	if in == nil {
		return nil, nil
	}
	// An empty list is still a list: `forges: []` beside `github` is two
	// spellings to the CRD's CEL rule, whose has() is true for it, and the
	// two have to agree.
	listed := in.Forges != nil || in.Repositories != nil
	switch {
	case listed && in.GitHub != nil:
		return nil, fmt.Errorf("set integration.forges and integration.repositories, or integration.github, not both; " +
			"integration.github is a deprecated alias for one forge with provider " + GitProviderGitHub)
	case in.GitHub != nil:
		return resolveDeprecatedAlias(in.GitHub), nil
	case listed:
		return resolveLists(in.Forges, in.Repositories), nil
	default:
		return nil, nil
	}
}

func resolveDeprecatedAlias(github *GitHubSpec) *ResolvedIntegration {
	forge := &ResolvedForge{
		Index:     -1,
		Name:      deprecatedAliasForgeName,
		Provider:  GitProviderGitHub,
		Namespace: strings.TrimSpace(github.Org),
	}
	resolved := &ResolvedIntegration{Forges: []*ResolvedForge{forge}, FromDeprecatedAlias: true}
	if repo := strings.TrimSpace(github.GitRepo); repo != "" && repo != NoRepositorySentinel {
		resolved.Repositories = []*ResolvedRepository{{
			Index:      -1,
			ForgeName:  forge.Name,
			Forge:      forge,
			Repository: repo,
			Role:       RepositoryRoleGitOps,
		}}
	}
	return resolved
}

func resolveLists(forges []ForgeSpec, repositories []RepositorySpec) *ResolvedIntegration {
	resolved := &ResolvedIntegration{}
	byName := make(map[string]*ResolvedForge, len(forges))
	for i, f := range forges {
		provider := strings.ToLower(strings.TrimSpace(f.Provider))
		if provider == "" {
			provider = DefaultGitProvider
		}
		forge := &ResolvedForge{
			Index:     i,
			Name:      strings.TrimSpace(f.Name),
			Provider:  provider,
			Host:      strings.TrimSpace(f.Host),
			Namespace: strings.TrimSpace(f.Namespace),
		}
		if f.CredentialsRef != nil {
			forge.CredentialsSecret = strings.TrimSpace(f.CredentialsRef.Name)
		}
		resolved.Forges = append(resolved.Forges, forge)
		// The schema keys the list on name, so a repeat cannot be stored; the
		// first one wins here only for a spec built in Go.
		if _, seen := byName[forge.Name]; !seen {
			byName[forge.Name] = forge
		}
	}
	for i, r := range repositories {
		name := strings.TrimSpace(r.Forge)
		resolved.Repositories = append(resolved.Repositories, &ResolvedRepository{
			Index:      i,
			ForgeName:  name,
			Forge:      byName[name],
			Repository: strings.TrimSpace(r.Repository),
			Namespace:  strings.TrimSpace(r.Namespace),
			Role:       strings.TrimSpace(r.Role),
			BaseBranch: strings.TrimSpace(r.BaseBranch),
		})
	}
	return resolved
}

// GitProvider returns the registered rules for this forge's provider.
func (f *ResolvedForge) GitProvider() (*GitProvider, error) {
	if f == nil {
		return nil, fmt.Errorf("no forge declared")
	}
	return LookupGitProvider(f.Provider)
}

// valid reports whether the forge's own fields pass its provider's rules, which
// is what a repository on it, and its egress, depend on.
func (f *ResolvedForge) valid() bool {
	provider, err := f.GitProvider()
	if err != nil {
		return false
	}
	return validateDeclaredValue(gitHostField, f.Host, MaxGitHostLength) == nil &&
		provider.ValidateHost(f.Host) == nil &&
		validateDeclaredValue(gitNamespaceField, f.Namespace, MaxGitNamespaceLength) == nil &&
		provider.ValidateNamespace(f.Namespace) == nil
}

// EffectiveNamespace is the namespace a bare repository name is qualified by:
// the repository's own, else its forge's.
func (r *ResolvedRepository) EffectiveNamespace() string {
	if r.Namespace != "" || r.Forge == nil {
		return r.Namespace
	}
	return r.Forge.Namespace
}

// Resolve fully qualifies the repository under its forge's provider rules.
func (r *ResolvedRepository) Resolve() (RepoRef, error) {
	if r.Forge == nil {
		return RepoRef{}, fmt.Errorf("forge %q is not declared in integration.forges", r.ForgeName)
	}
	provider, err := r.Forge.GitProvider()
	if err != nil {
		return RepoRef{}, err
	}
	return provider.Resolve(r.Forge.Host, r.Repository, r.EffectiveNamespace())
}

// ManagedRepoEntry is the gitops-state ConfigMap entry the repository seeds.
func (r *ResolvedRepository) ManagedRepoEntry() (ManagedRepoEntry, error) {
	ref, err := r.Resolve()
	if err != nil {
		return ManagedRepoEntry{}, err
	}
	return ManagedRepoEntry{Type: r.Forge.Provider, URL: ref.URL()}, nil
}

// GitOps returns the repository with role gitops, or nil.
func (ri *ResolvedIntegration) GitOps() *ResolvedRepository {
	if ri == nil {
		return nil
	}
	for _, r := range ri.Repositories {
		if r.Role == RepositoryRoleGitOps {
			return r
		}
	}
	return nil
}

// WithRole returns the repositories with one role, in declaration order.
func (ri *ResolvedIntegration) WithRole(role string) []*ResolvedRepository {
	if ri == nil {
		return nil
	}
	var out []*ResolvedRepository
	for _, r := range ri.Repositories {
		if r.Role == role {
			out = append(out, r)
		}
	}
	return out
}

// PrimaryForge returns the forge of one provider the agent acts for first: the
// forge of the first accepted repository the agent writes to on that provider,
// the GitOps repository before the managed ones; else the first valid forge
// declared with it that names a namespace; else the first valid forge declared
// with it. Nil when no valid forge has the provider.
//
// A GitHub App token is scoped to one organisation, and GITHUB_ORG names one,
// so where several GitHub forges are declared something has to pick; the
// repository the agent's GitOps work lands in is the one that must work.
//
// Only accepted repositories and valid forges count, as for Accepted and the
// egress policy. A refused forge's namespace would otherwise become GITHUB_ORG
// and the minter's primary organisation, and the minter prunes every policy
// outside that organisation: with the webhook off, one mistyped namespace would
// revoke tokens for repositories that were working. A refused GitOps
// repository must not pin the choice to a forge with nothing else to offer
// while another forge has an organisation that would answer.
func (ri *ResolvedIntegration) PrimaryForge(provider string) *ResolvedForge {
	if ri == nil {
		return nil
	}
	for _, role := range writeRoles {
		for _, r := range ri.Accepted(role) {
			if r.Forge != nil && r.Forge.Provider == provider {
				return r.Forge
			}
		}
	}
	var first *ResolvedForge
	for _, f := range ri.Forges {
		if f.Provider != provider || !f.valid() {
			continue
		}
		if f.Namespace != "" {
			return f
		}
		if first == nil {
			first = f
		}
	}
	return first
}

// PrimaryNamespace is the namespace of PrimaryForge(provider): as declared, or
// else the one the first accepted repository the agent writes to on that forge
// implies, the GitOps repository before the managed ones. Only accepted
// repositories count, so a value validation refuses never becomes GITHUB_ORG.
// Context repositories do not count: reading another organisation's repository
// is what they are for.
//
// It is empty when none of that is available, which an empty minter primary
// organisation reads as "accept every organisation". That is legitimate only
// where there is nothing on the forge to scope: an install may declare no
// repository at all. It is empty too while ScopeRefused(provider) holds, so a
// refusal leaves the namespace unset rather than moving it to whatever forge
// or repository validation happened to leave standing.
func (ri *ResolvedIntegration) PrimaryNamespace(provider string) string {
	if ri.ScopeRefused(provider) {
		return ""
	}
	forge := ri.PrimaryForge(provider)
	if forge == nil {
		return ""
	}
	if forge.Namespace != "" {
		return forge.Namespace
	}
	for _, role := range writeRoles {
		for _, r := range ri.Accepted(role) {
			if r.Forge != forge {
				continue
			}
			ref, err := r.Resolve()
			if err != nil {
				continue
			}
			if segments := ref.Segments(); len(segments) >= minNamespacedPathDepth {
				return strings.Join(segments[:len(segments)-1], pathSeparator)
			}
		}
	}
	return ""
}

// ScopeRefused reports that validation refused something the choice of
// PrimaryNamespace(provider) depends on: a forge of that provider, or a
// repository the agent writes to on one. The one declaration a refusal cannot
// move is every forge of the provider valid and naming the same namespace.
//
// The minter prunes every policy outside its primary organisation, and the
// webhook is off by default. Were the choice made from what validation left
// standing, a typo in the GitOps forge's namespace, or in the GitOps
// repository, would move the organisation to another forge's or another
// repository's and revoke the tokens of the repositories that were working.
func (ri *ResolvedIntegration) ScopeRefused(provider string) bool {
	if ri == nil {
		return false
	}
	fixed, declared := true, ""
	for _, f := range ri.Forges {
		if f.Provider != provider {
			continue
		}
		if !f.valid() {
			return true
		}
		if f.Namespace == "" || (declared != "" && f.Namespace != declared) {
			fixed = false
		}
		declared = f.Namespace
	}
	if fixed {
		return false
	}
	_, rejected := ri.check()
	// Only a write repository names the organisation, so only an accepted one
	// can stand in for a refused declaration of the same repository.
	accepted := map[string]bool{}
	for _, r := range ri.Repositories {
		if !rejected[r] && r.Role != RepositoryRoleContext {
			if ref, err := r.Resolve(); err == nil {
				accepted[strings.ToLower(ref.URL())] = true
			}
		}
	}
	// A repository naming no declared forge has no provider to exclude it by,
	// so it counts against every provider: it may be this one's GitOps
	// repository under a mistyped forge name.
	return slices.ContainsFunc(ri.Repositories, func(r *ResolvedRepository) bool {
		return rejected[r] && r.Role != RepositoryRoleContext && (r.Forge == nil || r.Forge.Provider == provider) &&
			!r.restatesAccepted(accepted)
	})
}

// restatesAccepted reports that a refused repository names, by itself, a write
// repository that validation accepted: a second declaration of it, or a full URL of it
// beside a namespace override the grammar refused and the URL never used. Its
// refusal cannot move the organisation, which the accepted one already names.
// It is read without the namespace, which a full URL never uses, and then with
// its effective namespace, so a bare name that qualified to the accepted URL
// and was refused only as a second declaration counts too. A bare name beside
// a refused override does not: the grammar refuses the override again.
func (r *ResolvedRepository) restatesAccepted(accepted map[string]bool) bool {
	if r.Forge == nil || !r.Forge.valid() {
		return false
	}
	provider, err := r.Forge.GitProvider()
	if err != nil {
		return false
	}
	for _, namespace := range []string{"", r.EffectiveNamespace()} {
		if ref, err := provider.Resolve(r.Forge.Host, r.Repository, namespace); err == nil && accepted[strings.ToLower(ref.URL())] {
			return true
		}
	}
	return false
}

// OnRefusedForge returns the repositories withheld because their forge's own
// fields are refused. Problems reports the forge, not them, so a caller that
// explains what is not seeded has to count them itself.
func (ri *ResolvedIntegration) OnRefusedForge() []*ResolvedRepository {
	if ri == nil {
		return nil
	}
	var withheld []*ResolvedRepository
	for _, r := range ri.Repositories {
		if r.Forge != nil && !r.Forge.valid() {
			withheld = append(withheld, r)
		}
	}
	return withheld
}

// minNamespacedPathDepth is the shortest path from which a namespace can be
// read: one namespace segment and the repository name.
const minNamespacedPathDepth = 2

// Field names in the two spellings of the declaration. Admission reports
// against the field the administrator actually wrote, not the resolved form,
// so a deprecated-alias user is not told about a field they did not set.
const (
	forgesField           = "forges"
	repositoriesField     = "repositories"
	gitHubFieldRoot       = "github"
	gitProviderField      = "provider"
	gitHostField          = "host"
	gitNamespaceField     = "namespace"
	gitCredentialsField   = "credentialsRef"
	gitRepositoryField    = "repository"
	gitRepoForgeField     = "forge"
	gitRepoRoleField      = "role"
	gitHubOrgField        = "org"
	gitHubRepoField       = "gitRepo"
	noIndex               = -1
	noRepositorySentinelQ = `"` + NoRepositorySentinel + `"`
)

// IntegrationFieldPath locates a problem under `spec.integration`: a list or
// the alias, an index into the list, and a field of the entry. Index is -1 and
// Field is empty where they do not apply.
// +kubebuilder:object:generate=false
type IntegrationFieldPath struct {
	List  string
	Index int
	Field string
}

// String renders the path the way field.Path does, relative to
// `spec.integration`.
func (p IntegrationFieldPath) String() string {
	var b strings.Builder
	b.WriteString(p.List)
	if p.Index >= 0 {
		fmt.Fprintf(&b, "[%d]", p.Index)
	}
	if p.Field != "" {
		b.WriteString("." + p.Field)
	}
	return b.String()
}

// IntegrationProblem is one field of the declaration its provider refuses.
// +kubebuilder:object:generate=false
type IntegrationProblem struct {
	Path  IntegrationFieldPath
	Value string
	Err   error
}

// forgePath renders the path of one field of a forge in whichever spelling it
// was written. The fields the alias cannot express map onto `github` itself.
func (ri *ResolvedIntegration) forgePath(f *ResolvedForge, field string) IntegrationFieldPath {
	if !ri.FromDeprecatedAlias {
		return IntegrationFieldPath{List: forgesField, Index: f.Index, Field: field}
	}
	if field == gitNamespaceField {
		return IntegrationFieldPath{List: gitHubFieldRoot, Index: noIndex, Field: gitHubOrgField}
	}
	return IntegrationFieldPath{List: gitHubFieldRoot, Index: noIndex}
}

func (ri *ResolvedIntegration) repositoryPath(r *ResolvedRepository, field string) IntegrationFieldPath {
	if !ri.FromDeprecatedAlias {
		return IntegrationFieldPath{List: repositoriesField, Index: r.Index, Field: field}
	}
	if field == gitRepositoryField {
		return IntegrationFieldPath{List: gitHubFieldRoot, Index: noIndex, Field: gitHubRepoField}
	}
	return IntegrationFieldPath{List: gitHubFieldRoot, Index: noIndex}
}

// Problems applies each declared forge's own rules — its hosts, its namespace
// grammar, its path depth — and reports every field that fails, rather than
// stopping at the first. A repository on a forge that is itself invalid is not
// checked: the forge's error is the one to fix, and a second error on every
// repository would bury it.
func (ri *ResolvedIntegration) Problems() []IntegrationProblem {
	problems, _ := ri.check()
	return problems
}

// Accepted returns the repositories with one role that Problems finds nothing
// wrong with, in declaration order: the ones the operator seeds. A repository
// on a forge with a problem is not accepted either, and neither is the second
// of two declarations of one repository. The webhook is off by default, so
// this -- not admission -- is what keeps a refused entry out of the state
// ConfigMap and out of the minter's scopes.
func (ri *ResolvedIntegration) Accepted(role string) []*ResolvedRepository {
	_, rejected := ri.check()
	var out []*ResolvedRepository
	for _, r := range ri.WithRole(role) {
		if !rejected[r] {
			out = append(out, r)
		}
	}
	return out
}

func (ri *ResolvedIntegration) check() ([]IntegrationProblem, map[*ResolvedRepository]bool) {
	if ri == nil {
		return nil, nil
	}
	var problems []IntegrationProblem
	rejected := map[*ResolvedRepository]bool{}
	add := func(path IntegrationFieldPath, value string, err error) {
		problems = append(problems, IntegrationProblem{Path: path, Value: value, Err: err})
	}
	reject := func(r *ResolvedRepository, path IntegrationFieldPath, value string, err error) {
		add(path, value, err)
		rejected[r] = true
	}

	for _, f := range ri.Forges {
		provider, err := f.GitProvider()
		if err != nil {
			add(ri.forgePath(f, gitProviderField), f.Provider, err)
			continue
		}
		if err := validateDeclaredValue(gitHostField, f.Host, MaxGitHostLength); err != nil {
			add(ri.forgePath(f, gitHostField), f.Host, err)
		} else if err := provider.ValidateHost(f.Host); err != nil {
			add(ri.forgePath(f, gitHostField), f.Host, err)
		}
		if err := validateDeclaredValue(gitNamespaceField, f.Namespace, MaxGitNamespaceLength); err != nil {
			add(ri.forgePath(f, gitNamespaceField), f.Namespace, err)
		} else if err := provider.ValidateNamespace(f.Namespace); err != nil {
			add(ri.forgePath(f, gitNamespaceField), f.Namespace, err)
		}
	}

	gitops := 0
	seen := map[string]int{}
	for _, r := range ri.Repositories {
		if r.Role == RepositoryRoleGitOps {
			gitops++
			if gitops > 1 {
				reject(r, ri.repositoryPath(r, gitRepoRoleField), r.Role,
					fmt.Errorf("at most one repository may have role %s", RepositoryRoleGitOps))
			}
		} else if r.Role != RepositoryRoleManaged && r.Role != RepositoryRoleContext {
			reject(r, ri.repositoryPath(r, gitRepoRoleField), r.Role, fmt.Errorf("unsupported role %q; must be one of %s, %s, %s",
				r.Role, RepositoryRoleGitOps, RepositoryRoleManaged, RepositoryRoleContext))
		}
		if r.Forge == nil {
			reject(r, ri.repositoryPath(r, gitRepoForgeField), r.ForgeName,
				fmt.Errorf("forge %q is not declared in integration.forges", r.ForgeName))
			continue
		}
		if !r.Forge.valid() {
			// The forge's own problem is reported against the forge.
			rejected[r] = true
			continue
		}
		provider, _ := r.Forge.GitProvider()
		if err := validateDeclaredValue(gitNamespaceField, r.Namespace, MaxGitNamespaceLength); err != nil {
			reject(r, ri.repositoryPath(r, gitNamespaceField), r.Namespace, err)
			continue
		} else if err := provider.ValidateNamespace(r.Namespace); err != nil {
			reject(r, ri.repositoryPath(r, gitNamespaceField), r.Namespace, err)
			continue
		}
		if r.Repository == "" {
			reject(r, ri.repositoryPath(r, gitRepositoryField), r.Repository, errors.New("repository is required"))
			continue
		}
		if r.Repository == NoRepositorySentinel && !ri.FromDeprecatedAlias {
			// The alias has always read it as "no repository", and still does.
			// In a list the way to declare no repository is no entry, and
			// reading it as a bare name would qualify it into a real path.
			reject(r, ri.repositoryPath(r, gitRepositoryField), r.Repository,
				fmt.Errorf("%s is the deprecated github.gitRepo's \"no repository\" value; omit the entry instead",
					noRepositorySentinelQ))
			continue
		}
		if err := validateDeclaredValue(gitRepositoryField, r.Repository, MaxGitRepoURLLength); err != nil {
			reject(r, ri.repositoryPath(r, gitRepositoryField), r.Repository, err)
			continue
		}
		ref, err := r.Resolve()
		if err != nil {
			reject(r, ri.repositoryPath(r, gitRepositoryField), r.Repository,
				fmt.Errorf("invalid %s repository %q: %w", provider.Name, r.Repository, err))
			continue
		}
		// One repository in two roles would be seeded into managed_repos and
		// context_repos both, and the minter would render it one scope or the
		// other depending on which list it read first.
		key := strings.ToLower(ref.URL())
		if first, dup := seen[key]; dup {
			reject(r, ri.repositoryPath(r, gitRepositoryField), r.Repository,
				fmt.Errorf("repository %s is already declared at %s", ref.URL(),
					ri.repositoryPath(ri.Repositories[first], "").String()))
			continue
		}
		// An entry refused for its role is still checked for everything else,
		// but it claims no URL: a later entry naming the same repository in a
		// role that is accepted is not its duplicate.
		if !rejected[r] {
			seen[key] = r.Index
		}
	}
	return problems, rejected
}

// Warnings are declarations that are valid but do nothing.
func (ri *ResolvedIntegration) Warnings() []string {
	if ri == nil {
		return nil
	}
	var warnings []string
	for _, f := range ri.Forges {
		if f.CredentialsSecret != "" && f.Provider == GitProviderGitHub {
			warnings = append(warnings, fmt.Sprintf(
				"spec.integration.%s is ignored for provider %s: GitHub credentials come from the install's GitHub App through the token minter",
				ri.forgePath(f, gitCredentialsField), GitProviderGitHub))
		}
	}
	return warnings
}

// ValidateGit is ResolveGit followed by Problems, joined into one error — the
// whole check on the forge declaration, for the reconcile-time callers that
// want it as one call. Admission uses Problems directly, to report per field.
func (in *IntegrationSpec) ValidateGit() error {
	resolved, err := in.ResolveGit()
	if err != nil {
		return err
	}
	var errs []error
	for _, p := range resolved.Problems() {
		errs = append(errs, fmt.Errorf("integration.%s: %w", p.Path, p.Err))
	}
	return errors.Join(errs...)
}

// ForgeEgressPatterns derives the forge half of the operator's FQDN egress
// allowlist from the declaration, rather than listing hosts where the policy is
// written.
//
// GitHub is always included. A repository can be registered in the
// gitops-state ConfigMap without being declared, an install with no
// declaration at all still reaches GitHub today, and a declaration that fails
// validation must not be the thing that takes egress away from a running
// install — the reconcile warning and the webhook are how that gets fixed, not
// a pod that silently loses its forge. So GitHub's patterns come first, and
// each declared forge whose own fields are valid adds its patterns to them.
func ForgeEgressPatterns(in *IntegrationSpec) []string {
	github, err := LookupGitProvider(GitProviderGitHub)
	if err != nil {
		// The registry always carries GitHub; a table without it is a build
		// defect, and gitspec_test.go fails on it.
		panic(err)
	}
	patterns := github.EgressPatterns("")
	resolved, err := in.ResolveGit()
	if err != nil || resolved == nil {
		return patterns
	}
	seen := make(map[string]bool, len(patterns))
	for _, p := range patterns {
		seen[p] = true
	}
	for _, f := range resolved.Forges {
		if !f.valid() {
			continue
		}
		provider, _ := f.GitProvider()
		for _, p := range provider.EgressPatterns(f.Host) {
			if !seen[p] {
				seen[p] = true
				patterns = append(patterns, p)
			}
		}
	}
	return patterns
}
