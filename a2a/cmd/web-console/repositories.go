package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"strings"
	"time"
)

var (
	validBaseBranchPattern      = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._/-]*$`)
	validRefsHeadsPrefixPattern = regexp.MustCompile(`^refs/heads/[A-Za-z0-9]`)
	validForgeNamePattern       = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`)
	allowedForgeProviders       = map[string]bool{
		"github": true,
		"gitlab": true,
		"gitea":  true,
	}
)

const (
	repoTopologyAnnotation = "kubeagents.x-k8s.io/repository-topology"
	repoMetadataAnnotation = "kubeagents.x-k8s.io/repository-metadata"

	categoryMonorepo  = "monorepo"
	categoryInfra     = "infra"
	categoryWorkloads = "workloads"
	categoryIAM       = "iam"
	categoryContext   = "context"
	categoryCustom    = "custom"

	maxRepositoriesCount = 50
	maxBaseBranchLength  = 200
	repositoriesTimeout  = 10 * time.Second
)

type forgeInfo struct {
	Name      string `json:"name"`
	Provider  string `json:"provider"`
	Host      string `json:"host,omitempty"`
	Namespace string `json:"namespace,omitempty"`
}

type repositoriesResponse struct {
	Topology       string           `json:"topology"`
	Repositories   []repositoryInfo `json:"repositories"`
	Forges         []forgeInfo      `json:"forges"`
	HelmValuesYAML string           `json:"helm_values_yaml"`
}

type repositoriesSetRequest struct {
	Topology     string           `json:"topology"`
	Repositories []repositoryInfo `json:"repositories"`
	Forges       []forgeInfo      `json:"forges,omitempty"`
}

type platformAgentRepoSpec struct {
	Forge      string `json:"forge"`
	Repository string `json:"repository"`
	Role       string `json:"role"`
	BaseBranch string `json:"baseBranch,omitempty"`
	Namespace  string `json:"namespace,omitempty"`
}

type platformAgentCRForRepos struct {
	Metadata struct {
		Annotations map[string]string `json:"annotations,omitempty"`
	} `json:"metadata,omitempty"`
	Spec struct {
		Integration *struct {
			Forges       []forgeInfo             `json:"forges,omitempty"`
			Repositories []platformAgentRepoSpec `json:"repositories,omitempty"`
			GitHub       *struct {
				GitRepo string `json:"gitRepo,omitempty"`
				Org     string `json:"org,omitempty"`
			} `json:"github,omitempty"`
		} `json:"integration,omitempty"`
	} `json:"spec,omitempty"`
}

type repoMetadataPayload struct {
	Categories map[string]string `json:"categories"`
}

func inferCategory(repo, role string) string {
	lower := strings.ToLower(repo)
	switch {
	case strings.Contains(lower, "iam") || strings.Contains(lower, "rbac") || strings.Contains(lower, "policy") ||
		strings.Contains(lower, "policies") || strings.Contains(lower, "security") || strings.Contains(lower, "auth"):
		return categoryIAM
	case strings.Contains(lower, "infra") || strings.Contains(lower, "platform") || strings.Contains(lower, "cluster") ||
		strings.Contains(lower, "terraform") || strings.Contains(lower, "vpc") || strings.Contains(lower, "network"):
		return categoryInfra
	case strings.Contains(lower, "workload") || strings.Contains(lower, "app") || strings.Contains(lower, "microservice") ||
		strings.Contains(lower, "service") || strings.Contains(lower, "deploy"):
		return categoryWorkloads
	case strings.Contains(lower, "monorepo") || strings.Contains(lower, "fleet"):
		return categoryMonorepo
	case role == "context":
		return categoryContext
	case role == "gitops":
		return categoryInfra
	default:
		return categoryWorkloads
	}
}

func validateBaseBranch(b string) error {
	if b == "" {
		return nil
	}
	if len(b) > maxBaseBranchLength {
		return fmt.Errorf("baseBranch exceeds %d characters", maxBaseBranchLength)
	}
	if b == "HEAD" || b == "refs/heads/HEAD" {
		return fmt.Errorf("baseBranch may not be HEAD")
	}
	if strings.HasPrefix(b, "refs/heads/") && !validRefsHeadsPrefixPattern.MatchString(b) {
		return fmt.Errorf("baseBranch after refs/heads/ must start with a letter or digit and may not be HEAD")
	}
	if strings.HasPrefix(b, "heads/") {
		return fmt.Errorf("baseBranch may not start with heads/: write the branch name, or refs/heads/ and the name")
	}
	if strings.HasPrefix(b, "refs/heads/refs/heads/") || strings.HasPrefix(b, "refs/heads/heads/") {
		return fmt.Errorf("baseBranch may carry one refs/heads/ prefix, not nested prefixes")
	}
	if strings.Contains(b, "..") || strings.Contains(b, "/.") || strings.Contains(b, "//") ||
		strings.Contains(b, "@{") || strings.Contains(b, ".lock/") {
		return fmt.Errorf("baseBranch contains forbidden git sequences (.., /., //, @{, or .lock/)")
	}
	if strings.HasSuffix(b, "/") || strings.HasSuffix(b, ".") || strings.HasSuffix(b, ".lock") {
		return fmt.Errorf("baseBranch may not end with '/', '.', or '.lock'")
	}
	if !validBaseBranchPattern.MatchString(b) {
		return fmt.Errorf("baseBranch contains invalid characters")
	}
	return nil
}

func deriveTopology(repos []repositoryInfo, explicit string) string {
	top := strings.ToLower(strings.TrimSpace(explicit))
	if len(repos) == 0 {
		return "none"
	}
	if top != "" && top != "none" {
		return top
	}
	if len(repos) == 1 && repos[0].Role == "gitops" {
		return "monorepo"
	}
	if len(repos) > 1 && len(repos) <= 3 {
		hasContext := false
		for _, r := range repos {
			if r.Role == "context" {
				hasContext = true
				break
			}
		}
		if !hasContext {
			return "split"
		}
	}
	return "custom"
}

func generateHelmValuesYAML(forges []forgeInfo, repos []repositoryInfo) string {
	var sb strings.Builder
	sb.WriteString("platformAgent:\n  integration:\n    forges:\n")
	if len(forges) == 0 {
		sb.WriteString("      - name: github\n        provider: github\n")
	} else {
		for _, f := range forges {
			sb.WriteString(fmt.Sprintf("      - name: %s\n        provider: %s\n", f.Name, f.Provider))
			if f.Host != "" {
				sb.WriteString(fmt.Sprintf("        host: %q\n", f.Host))
			}
			if f.Namespace != "" {
				sb.WriteString(fmt.Sprintf("        namespace: %q\n", f.Namespace))
			}
		}
	}
	sb.WriteString("    repositories:\n")
	if len(repos) == 0 {
		sb.WriteString("      []\n")
	} else {
		for _, r := range repos {
			forgeName := r.Forge
			if forgeName == "" {
				forgeName = r.Provider
				if forgeName == "" {
					forgeName = "github"
				}
			}
			sb.WriteString(fmt.Sprintf("      - forge: %s\n        repository: %s\n        role: %s\n", forgeName, r.Repository, r.Role))
			if r.BaseBranch != "" {
				sb.WriteString(fmt.Sprintf("        baseBranch: %s\n", r.BaseBranch))
			}
		}
	}
	return sb.String()
}

func (s *server) currentRepositories(ctx context.Context) []repositoryInfo {
	if s.kube == nil {
		s.reposMu.RLock()
		if s.cachedRepos != nil {
			defer s.reposMu.RUnlock()
			return s.cachedRepos
		}
		s.reposMu.RUnlock()
	}

	s.refreshRepositoriesFromCluster(ctx)

	s.reposMu.RLock()
	defer s.reposMu.RUnlock()
	if s.cachedRepos != nil {
		return s.cachedRepos
	}
	if s.cfg.Repositories != nil {
		return s.cfg.Repositories
	}
	return []repositoryInfo{}
}

func (s *server) refreshRepositoriesFromCluster(ctx context.Context) {
	s.reposMu.Lock()
	defer s.reposMu.Unlock()
	if s.kube == nil && s.cachedRepos != nil {
		return
	}

	repos := s.cfg.Repositories
	if repos == nil {
		repos = []repositoryInfo{}
	}
	explicitTopology := ""
	forges := []forgeInfo{{Name: "github", Provider: "github"}}

	if s.kube != nil {
		var pa platformAgentCRForRepos
		path := fmt.Sprintf(platformAgentsAPIPath, s.cfg.PodNamespace, s.cfg.PlatformAgentName)
		if err := s.kube.get(ctx, path, &pa); err == nil {
			if pa.Metadata.Annotations != nil {
				explicitTopology = pa.Metadata.Annotations[repoTopologyAnnotation]
			}
			if pa.Spec.Integration != nil {
				if len(pa.Spec.Integration.Forges) > 0 {
					forges = pa.Spec.Integration.Forges
				}
				forgeMap := map[string]forgeInfo{}
				for _, f := range forges {
					forgeMap[f.Name] = f
				}

				var catMap map[string]string
				if rawMeta := pa.Metadata.Annotations[repoMetadataAnnotation]; rawMeta != "" {
					var meta repoMetadataPayload
					if err := json.Unmarshal([]byte(rawMeta), &meta); err == nil {
						catMap = meta.Categories
					}
				}

				crRepos := make([]repositoryInfo, 0, len(pa.Spec.Integration.Repositories))
				for _, r := range pa.Spec.Integration.Repositories {
					fName := r.Forge
					if fName == "" {
						fName = "github"
					}
					fInfo := forgeMap[fName]
					provider := fInfo.Provider
					if provider == "" {
						provider = "github"
					}
					cat := ""
					if catMap != nil {
						cat = catMap[r.Repository]
					}
					if cat == "" {
						cat = inferCategory(r.Repository, r.Role)
					}
					info := repositoryInfo{
						Forge:      fName,
						Provider:   provider,
						Host:       fInfo.Host,
						Repository: r.Repository,
						Role:       r.Role,
						BaseBranch: r.BaseBranch,
						Category:   cat,
					}
					info.URL = repositoryURL(info)
					crRepos = append(crRepos, info)
				}
				if len(crRepos) == 0 && pa.Spec.Integration.GitHub != nil {
					if gitRepo := strings.TrimSpace(pa.Spec.Integration.GitHub.GitRepo); gitRepo != "" {
						org := strings.TrimSpace(pa.Spec.Integration.GitHub.Org)
						if org != "" && !strings.Contains(gitRepo, "/") && !strings.Contains(gitRepo, "://") {
							gitRepo = org + "/" + gitRepo
						}
						cat := ""
						if catMap != nil {
							cat = catMap[gitRepo]
						}
						if cat == "" {
							cat = inferCategory(gitRepo, "gitops")
						}
						info := repositoryInfo{
							Forge:      "github",
							Provider:   "github",
							Repository: gitRepo,
							Role:       "gitops",
							Category:   cat,
						}
						info.URL = repositoryURL(info)
						crRepos = append(crRepos, info)
					}
				}
				repos = crRepos
			}
		} else if s.cachedRepos != nil {
			return
		}
	}

	topology := deriveTopology(repos, explicitTopology)

	for i := range repos {
		if repos[i].Category == "" {
			repos[i].Category = inferCategory(repos[i].Repository, repos[i].Role)
		}
		if repos[i].URL == "" {
			repos[i].URL = repositoryURL(repos[i])
		}
	}

	s.cachedRepos = repos
	s.cachedTopology = topology
	s.cachedForges = forges
}

func (s *server) handleRepositoriesGet(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), repositoriesTimeout)
	defer cancel()

	repos := s.currentRepositories(ctx)

	s.reposMu.RLock()
	topology := s.cachedTopology
	forges := s.cachedForges
	s.reposMu.RUnlock()

	if forges == nil {
		forges = []forgeInfo{{Name: "github", Provider: "github"}}
	}

	writeJSON(w, http.StatusOK, repositoriesResponse{
		Topology:       topology,
		Repositories:   repos,
		Forges:         forges,
		HelmValuesYAML: generateHelmValuesYAML(forges, repos),
	})
}

func (s *server) handleRepositoriesSet(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req repositoriesSetRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
		return
	}

	if len(req.Repositories) > maxRepositoriesCount {
		writeError(w, http.StatusBadRequest, "too_many_repositories", fmt.Sprintf("At most %d repositories may be configured.", maxRepositoriesCount))
		return
	}

	gitopsCount := 0
	categories := make(map[string]string)
	validatedRepos := make([]repositoryInfo, 0, len(req.Repositories))
	forgesNeeded := make(map[string]forgeInfo)

	s.reposMu.RLock()
	cachedForges := make([]forgeInfo, len(s.cachedForges))
	copy(cachedForges, s.cachedForges)
	s.reposMu.RUnlock()

	for _, cf := range cachedForges {
		if cf.Name != "" {
			forgesNeeded[cf.Name] = cf
		}
	}

	for _, f := range req.Forges {
		f.Name = strings.TrimSpace(f.Name)
		if f.Name == "" {
			continue
		}
		if !validForgeNamePattern.MatchString(f.Name) {
			writeError(w, http.StatusBadRequest, "invalid_forge", fmt.Sprintf("Forge name %q is invalid.", f.Name))
			return
		}
		f.Provider = strings.ToLower(strings.TrimSpace(f.Provider))
		if f.Provider == "" {
			f.Provider = "github"
		}
		if !allowedForgeProviders[f.Provider] {
			writeError(w, http.StatusBadRequest, "invalid_provider", fmt.Sprintf("Provider %q is invalid; must be github, gitlab, or gitea.", f.Provider))
			return
		}
		forgesNeeded[f.Name] = f
	}

	usedForgeNames := make(map[string]bool)

	for i, r := range req.Repositories {
		r.Repository = strings.TrimSpace(r.Repository)
		if r.Repository == "" {
			writeError(w, http.StatusBadRequest, "invalid_repository", fmt.Sprintf("Repository entry #%d has an empty repository path.", i+1))
			return
		}
		if strings.EqualFold(r.Repository, "none") {
			writeError(w, http.StatusBadRequest, "invalid_repository", fmt.Sprintf("Repository entry #%d cannot be 'None'.", i+1))
			return
		}

		r.Role = strings.ToLower(strings.TrimSpace(r.Role))
		if r.Role == "" {
			if gitopsCount == 0 {
				r.Role = "gitops"
			} else {
				r.Role = "managed"
			}
		}
		if r.Role != "gitops" && r.Role != "managed" && r.Role != "context" {
			writeError(w, http.StatusBadRequest, "invalid_role", fmt.Sprintf("Repository %q has invalid role %q; must be 'gitops', 'managed', or 'context'.", r.Repository, r.Role))
			return
		}
		if r.Role == "gitops" {
			gitopsCount++
			if gitopsCount > 1 {
				writeError(w, http.StatusBadRequest, "multiple_gitops_repos", "At most one repository may have role 'gitops'. Secondary repositories must be 'managed' or 'context'.")
				return
			}
		}

		r.BaseBranch = strings.TrimSpace(r.BaseBranch)
		if err := validateBaseBranch(r.BaseBranch); err != nil {
			writeError(w, http.StatusBadRequest, "invalid_base_branch", fmt.Sprintf("Repository %q has invalid base branch: %v", r.Repository, err))
			return
		}
		if r.Role == "context" && r.BaseBranch != "" {
			writeError(w, http.StatusBadRequest, "invalid_base_branch", fmt.Sprintf("Repository %q has role 'context'; context repositories cannot set a baseBranch.", r.Repository))
			return
		}

		r.Provider = strings.ToLower(strings.TrimSpace(r.Provider))
		if r.Provider == "" {
			r.Provider = "github"
		}
		if !allowedForgeProviders[r.Provider] {
			writeError(w, http.StatusBadRequest, "invalid_provider", fmt.Sprintf("Repository %q has invalid provider %q; must be github, gitlab, or gitea.", r.Repository, r.Provider))
			return
		}
		r.Forge = strings.TrimSpace(r.Forge)
		if r.Forge == "" || r.Forge == r.Provider {
			if _, literalExists := forgesNeeded[r.Provider]; !literalExists {
				for _, cf := range cachedForges {
					if strings.EqualFold(cf.Provider, r.Provider) && cf.Name != "" {
						r.Forge = cf.Name
						break
					}
				}
			}
		}
		if r.Forge == "" {
			r.Forge = r.Provider
		}
		if !validForgeNamePattern.MatchString(r.Forge) {
			writeError(w, http.StatusBadRequest, "invalid_forge", fmt.Sprintf("Repository %q has invalid forge name %q.", r.Repository, r.Forge))
			return
		}

		if existingForge, exists := forgesNeeded[r.Forge]; exists {
			if r.Host == "" {
				r.Host = existingForge.Host
			}
		} else {
			forgesNeeded[r.Forge] = forgeInfo{
				Name:     r.Forge,
				Provider: r.Provider,
			}
		}
		usedForgeNames[r.Forge] = true

		r.Category = strings.ToLower(strings.TrimSpace(r.Category))
		if r.Category == "" {
			r.Category = inferCategory(r.Repository, r.Role)
		}
		categories[r.Repository] = r.Category

		if r.URL == "" {
			r.URL = repositoryURL(r)
		}

		validatedRepos = append(validatedRepos, r)
	}

	forgesList := make([]forgeInfo, 0, len(forgesNeeded))
	for _, f := range req.Forges {
		fName := strings.TrimSpace(f.Name)
		if fName != "" {
			usedForgeNames[fName] = true
		}
	}
	for name, f := range forgesNeeded {
		if usedForgeNames[name] {
			forgesList = append(forgesList, f)
		}
	}
	if len(forgesList) == 0 {
		forgesList = append(forgesList, forgeInfo{Name: "github", Provider: "github"})
	}

	topology := deriveTopology(validatedRepos, req.Topology)

	ctx, cancel := context.WithTimeout(r.Context(), repositoriesTimeout)
	defer cancel()

	if err := s.saveRepositoriesToCluster(ctx, topology, validatedRepos, forgesList, categories); err != nil {
		log.Printf(logPrefix+"could not patch PlatformAgent repositories: %v", err)
		writeError(w, http.StatusInternalServerError, "patch_failed", fmt.Sprintf("Failed to update PlatformAgent in cluster: %v", err))
		return
	}

	writeJSON(w, http.StatusOK, repositoriesResponse{
		Topology:       topology,
		Repositories:   validatedRepos,
		Forges:         forgesList,
		HelmValuesYAML: generateHelmValuesYAML(forgesList, validatedRepos),
	})
}

func (s *server) saveRepositoriesToCluster(ctx context.Context, topology string, validatedRepos []repositoryInfo, forgesList []forgeInfo, categories map[string]string) error {
	if s.kube != nil {
		path := fmt.Sprintf(platformAgentsAPIPath, s.cfg.PodNamespace, s.cfg.PlatformAgentName)
		existingForges := map[string]map[string]any{}
		existingRepos := map[string]map[string]any{}
		hasGitHubAlias := false
		var rawCR map[string]any
		if err := s.kube.get(ctx, path, &rawCR); err != nil {
			return fmt.Errorf("read PlatformAgent before patch: %w", err)
		}
		if spec, ok := rawCR["spec"].(map[string]any); ok {
			if integ, ok := spec["integration"].(map[string]any); ok {
				if gVal, exists := integ["github"]; exists && gVal != nil {
					hasGitHubAlias = true
				}
				if rawForges, ok := integ["forges"].([]any); ok {
					for _, item := range rawForges {
						if fm, ok := item.(map[string]any); ok {
							if name, _ := fm["name"].(string); name != "" {
								existingForges[name] = fm
							}
						}
					}
				}
				if rawRepos, ok := integ["repositories"].([]any); ok {
					for _, item := range rawRepos {
						if rm, ok := item.(map[string]any); ok {
							fName, _ := rm["forge"].(string)
							if fName == "" {
								fName = "github"
							}
							rName, _ := rm["repository"].(string)
							if rName != "" {
								existingRepos[strings.ToLower(fName+":"+rName)] = rm
							}
						}
					}
				}
			}
		}

		mergedForges := make([]map[string]any, 0, len(forgesList))
		for _, f := range forgesList {
			fm := map[string]any{}
			if prev, ok := existingForges[f.Name]; ok {
				for k, v := range prev {
					fm[k] = v
				}
			}
			fm["name"] = f.Name
			fm["provider"] = f.Provider
			if f.Host != "" {
				fm["host"] = f.Host
			}
			if f.Namespace != "" {
				fm["namespace"] = f.Namespace
			}
			mergedForges = append(mergedForges, fm)
		}

		mergedRepos := make([]map[string]any, 0, len(validatedRepos))
		for _, vr := range validatedRepos {
			rm := map[string]any{}
			key := strings.ToLower(vr.Forge + ":" + vr.Repository)
			if prev, ok := existingRepos[key]; ok {
				for k, v := range prev {
					rm[k] = v
				}
			}
			rm["forge"] = vr.Forge
			rm["repository"] = vr.Repository
			rm["role"] = vr.Role
			if vr.BaseBranch != "" {
				rm["baseBranch"] = vr.BaseBranch
			} else {
				delete(rm, "baseBranch")
			}
			mergedRepos = append(mergedRepos, rm)
		}

		metadataJSON, _ := json.Marshal(repoMetadataPayload{Categories: categories})

		integrationPatch := map[string]any{
			"forges":       mergedForges,
			"repositories": mergedRepos,
		}
		if hasGitHubAlias {
			integrationPatch["github"] = nil
		}

		patch := map[string]any{
			"metadata": map[string]any{
				"annotations": map[string]string{
					repoTopologyAnnotation: topology,
					repoMetadataAnnotation: string(metadataJSON),
				},
			},
			"spec": map[string]any{
				"integration": integrationPatch,
			},
		}

		if _, err := s.kube.doJSON(ctx, http.MethodPatch, path, patch, nil); err != nil {
			return err
		}
	}

	s.reposMu.Lock()
	s.cachedRepos = validatedRepos
	s.cachedTopology = topology
	s.cachedForges = forgesList
	s.reposMu.Unlock()
	return nil
}

type repoConnectRequest struct {
	Repository string `json:"repository"`
	Role       string `json:"role,omitempty"`
	Category   string `json:"category,omitempty"`
	BaseBranch string `json:"base_branch,omitempty"`
	Provider   string `json:"provider,omitempty"`
	Forge      string `json:"forge,omitempty"`
}

func (s *server) handleRepositoriesConnect(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req repoConnectRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
		return
	}

	repoName := strings.TrimSpace(req.Repository)
	if repoName == "" || strings.EqualFold(repoName, "none") {
		writeError(w, http.StatusBadRequest, "invalid_repository", "Repository name cannot be empty or 'None'.")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), repositoriesTimeout)
	defer cancel()

	existing := s.currentRepositories(ctx)
	existingList := make([]repositoryInfo, len(existing))
	copy(existingList, existing)

	role := strings.ToLower(strings.TrimSpace(req.Role))
	if role == "" {
		hasGitops := false
		for _, er := range existingList {
			if er.Role == "gitops" {
				hasGitops = true
				break
			}
		}
		if !hasGitops {
			role = "gitops"
		} else {
			role = "managed"
		}
	}
	if role != "gitops" && role != "managed" && role != "context" {
		writeError(w, http.StatusBadRequest, "invalid_role", "Role must be 'gitops', 'managed', or 'context'.")
		return
	}

	baseBranch := strings.TrimSpace(req.BaseBranch)
	if role == "context" && baseBranch != "" {
		writeError(w, http.StatusBadRequest, "invalid_base_branch", fmt.Sprintf("Repository %q has role 'context'; context repositories cannot set a baseBranch.", repoName))
		return
	}
	if err := validateBaseBranch(baseBranch); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_base_branch", fmt.Sprintf("Invalid base branch: %v", err))
		return
	}

	s.reposMu.RLock()
	forges := make([]forgeInfo, len(s.cachedForges))
	copy(forges, s.cachedForges)
	s.reposMu.RUnlock()

	provider := strings.ToLower(strings.TrimSpace(req.Provider))
	forge := strings.TrimSpace(req.Forge)
	if provider == "" && forge == "" {
		if len(forges) > 0 && forges[0].Name != "" {
			forge = forges[0].Name
			provider = strings.ToLower(strings.TrimSpace(forges[0].Provider))
		} else if len(existingList) > 0 && existingList[0].Forge != "" {
			forge = existingList[0].Forge
			provider = strings.ToLower(strings.TrimSpace(existingList[0].Provider))
		}
	}
	if provider == "" && forge != "" {
		for _, f := range forges {
			if f.Name == forge && f.Provider != "" {
				provider = strings.ToLower(strings.TrimSpace(f.Provider))
				break
			}
		}
	}
	if provider == "" {
		provider = "github"
	}
	if !allowedForgeProviders[provider] {
		writeError(w, http.StatusBadRequest, "invalid_provider", fmt.Sprintf("Provider %q is invalid; must be github, gitlab, or gitea.", provider))
		return
	}
	if forge == "" || forge == provider {
		hasLiteral := false
		for _, f := range forges {
			if f.Name == provider {
				hasLiteral = true
				break
			}
		}
		if !hasLiteral {
			for _, f := range forges {
				if strings.EqualFold(f.Provider, provider) && f.Name != "" {
					forge = f.Name
					break
				}
			}
		}
	}
	if forge == "" {
		forge = provider
	}
	if !validForgeNamePattern.MatchString(forge) {
		writeError(w, http.StatusBadRequest, "invalid_forge", fmt.Sprintf("Forge name %q is invalid.", forge))
		return
	}

	category := strings.ToLower(strings.TrimSpace(req.Category))
	if category == "" {
		category = inferCategory(repoName, role)
	}

	// If making this repo 'gitops', demote any previous gitops repo to 'managed'
	if role == "gitops" {
		for i := range existingList {
			if strings.EqualFold(existingList[i].Repository, repoName) {
				continue
			}
			if existingList[i].Role == "gitops" {
				existingList[i].Role = "managed"
			}
		}
	}

	found := false
	for i := range existingList {
		if strings.EqualFold(existingList[i].Repository, repoName) {
			existingList[i].Role = role
			existingList[i].Category = category
			existingList[i].BaseBranch = baseBranch
			existingList[i].Provider = provider
			existingList[i].Forge = forge
			existingList[i].URL = repositoryURL(existingList[i])
			found = true
			break
		}
	}
	if !found {
		newInfo := repositoryInfo{
			Forge:      forge,
			Provider:   provider,
			Repository: repoName,
			Role:       role,
			BaseBranch: baseBranch,
			Category:   category,
		}
		newInfo.URL = repositoryURL(newInfo)
		existingList = append(existingList, newInfo)
	}

	hasForge := false
	for _, f := range forges {
		if f.Name == forge {
			hasForge = true
			break
		}
	}
	if !hasForge {
		forges = append(forges, forgeInfo{Name: forge, Provider: provider})
	}

	topology := deriveTopology(existingList, "")

	categories := make(map[string]string)
	for _, r := range existingList {
		categories[r.Repository] = r.Category
	}

	if err := s.saveRepositoriesToCluster(ctx, topology, existingList, forges, categories); err != nil {
		log.Printf(logPrefix+"could not connect repository: %v", err)
		writeError(w, http.StatusInternalServerError, "patch_failed", fmt.Sprintf("Failed to update repositories: %v", err))
		return
	}

	writeJSON(w, http.StatusOK, repositoriesResponse{
		Topology:       topology,
		Repositories:   existingList,
		Forges:         forges,
		HelmValuesYAML: generateHelmValuesYAML(forges, existingList),
	})
}

type repoDisconnectRequest struct {
	Repository string `json:"repository"`
}

func (s *server) handleRepositoriesDisconnect(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req repoDisconnectRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
		return
	}

	repoName := strings.TrimSpace(req.Repository)
	if repoName == "" {
		writeError(w, http.StatusBadRequest, "invalid_repository", "Repository name is required.")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), repositoriesTimeout)
	defer cancel()

	existing := s.currentRepositories(ctx)
	newList := make([]repositoryInfo, 0, len(existing))
	for _, er := range existing {
		if !strings.EqualFold(er.Repository, repoName) {
			newList = append(newList, er)
		}
	}

	s.reposMu.RLock()
	forges := s.cachedForges
	s.reposMu.RUnlock()

	topology := deriveTopology(newList, "")

	categories := make(map[string]string)
	for _, r := range newList {
		categories[r.Repository] = r.Category
	}

	if err := s.saveRepositoriesToCluster(ctx, topology, newList, forges, categories); err != nil {
		log.Printf(logPrefix+"could not disconnect repository: %v", err)
		writeError(w, http.StatusInternalServerError, "patch_failed", fmt.Sprintf("Failed to update repositories: %v", err))
		return
	}

	writeJSON(w, http.StatusOK, repositoriesResponse{
		Topology:       topology,
		Repositories:   newList,
		Forges:         forges,
		HelmValuesYAML: generateHelmValuesYAML(forges, newList),
	})
}

type repoProposePRRequest struct {
	Repository  string `json:"repository"`
	BaseBranch  string `json:"base_branch,omitempty"`
	Title       string `json:"title"`
	Description string `json:"description,omitempty"`
	Changes     string `json:"changes,omitempty"`
}

type repoProposePRResponse struct {
	Repository string `json:"repository"`
	BaseBranch string `json:"base_branch"`
	Title      string `json:"title"`
	Prompt     string `json:"prompt"`
}

func (s *server) handleRepositoriesProposePR(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req repoProposePRRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
		return
	}

	repoName := strings.TrimSpace(req.Repository)
	if repoName == "" {
		writeError(w, http.StatusBadRequest, "invalid_repository", "Target repository is required.")
		return
	}

	title := strings.TrimSpace(req.Title)
	if title == "" {
		writeError(w, http.StatusBadRequest, "invalid_title", "PR title/intent is required.")
		return
	}

	baseBranch := strings.TrimSpace(req.BaseBranch)
	if baseBranch == "" {
		baseBranch = "main"
	}

	var promptBuilder strings.Builder
	promptBuilder.WriteString(fmt.Sprintf("Open a pull request on repository %q (base branch %q) with title %q.\n\nUse the submit-suggestion skill to branch, modify the manifests, commit, and submit the PR through the credential proxy.\n", repoName, baseBranch, title))

	if desc := strings.TrimSpace(req.Description); desc != "" {
		promptBuilder.WriteString(fmt.Sprintf("\nDescription:\n%s\n", desc))
	}
	if changes := strings.TrimSpace(req.Changes); changes != "" {
		promptBuilder.WriteString(fmt.Sprintf("\nDesired Manifest Changes:\n%s\n", changes))
	}

	writeJSON(w, http.StatusOK, repoProposePRResponse{
		Repository: repoName,
		BaseBranch: baseBranch,
		Title:      title,
		Prompt:     promptBuilder.String(),
	})
}
