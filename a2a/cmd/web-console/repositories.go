package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"
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
	if len(b) > 200 {
		return fmt.Errorf("baseBranch exceeds 200 characters")
	}
	if b == "HEAD" || b == "refs/heads/HEAD" {
		return fmt.Errorf("baseBranch may not be HEAD")
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
	return nil
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
	s.reposMu.RLock()
	if s.cachedRepos != nil {
		defer s.reposMu.RUnlock()
		return s.cachedRepos
	}
	s.reposMu.RUnlock()

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
	if s.cachedRepos != nil {
		return
	}

	repos := s.cfg.Repositories
	if repos == nil {
		repos = []repositoryInfo{}
	}
	topology := "none"
	forges := []forgeInfo{{Name: "github", Provider: "github"}}

	if s.kube != nil {
		var pa platformAgentCRForRepos
		path := fmt.Sprintf(platformAgentsAPIPath, s.cfg.PodNamespace, s.cfg.PlatformAgentName)
		if err := s.kube.get(ctx, path, &pa); err == nil && pa.Spec.Integration != nil {
			if len(pa.Spec.Integration.Forges) > 0 {
				forges = pa.Spec.Integration.Forges
			}
			forgeMap := map[string]forgeInfo{}
			for _, f := range forges {
				forgeMap[f.Name] = f
			}

			// Read metadata annotations if present
			var catMap map[string]string
			if rawMeta := pa.Metadata.Annotations[repoMetadataAnnotation]; rawMeta != "" {
				var meta repoMetadataPayload
				if err := json.Unmarshal([]byte(rawMeta), &meta); err == nil {
					catMap = meta.Categories
				}
			}
			if top := pa.Metadata.Annotations[repoTopologyAnnotation]; top != "" {
				topology = top
			}

			if len(pa.Spec.Integration.Repositories) > 0 {
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
				repos = crRepos
			}
		}
	}

	if topology == "none" || topology == "" {
		if len(repos) == 1 && repos[0].Role == "gitops" {
			topology = "monorepo"
		} else if len(repos) > 1 {
			topology = "split"
		} else if len(repos) == 0 {
			topology = "none"
		} else {
			topology = "custom"
		}
	}

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
	s.cfg.Repositories = repos
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

	for _, f := range req.Forges {
		f.Name = strings.TrimSpace(f.Name)
		if f.Name == "" {
			continue
		}
		if f.Provider == "" {
			f.Provider = "github"
		}
		forgesNeeded[f.Name] = f
	}

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
		r.Forge = strings.TrimSpace(r.Forge)
		if r.Forge == "" {
			r.Forge = r.Provider
		}

		if _, exists := forgesNeeded[r.Forge]; !exists {
			forgesNeeded[r.Forge] = forgeInfo{
				Name:     r.Forge,
				Provider: r.Provider,
			}
		}

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
	for _, f := range forgesNeeded {
		forgesList = append(forgesList, f)
	}
	if len(forgesList) == 0 {
		forgesList = append(forgesList, forgeInfo{Name: "github", Provider: "github"})
	}

	topology := strings.ToLower(strings.TrimSpace(req.Topology))
	if topology == "" {
		if len(validatedRepos) == 1 && validatedRepos[0].Role == "gitops" {
			topology = "monorepo"
		} else if len(validatedRepos) > 1 {
			topology = "split"
		} else if len(validatedRepos) == 0 {
			topology = "none"
		} else {
			topology = "custom"
		}
	}

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
		crdRepos := make([]platformAgentRepoSpec, 0, len(validatedRepos))
		for _, vr := range validatedRepos {
			spec := platformAgentRepoSpec{
				Forge:      vr.Forge,
				Repository: vr.Repository,
				Role:       vr.Role,
				BaseBranch: vr.BaseBranch,
			}
			crdRepos = append(crdRepos, spec)
		}

		metadataJSON, _ := json.Marshal(repoMetadataPayload{Categories: categories})

		patch := map[string]any{
			"metadata": map[string]any{
				"annotations": map[string]string{
					repoTopologyAnnotation: topology,
					repoMetadataAnnotation: string(metadataJSON),
				},
			},
			"spec": map[string]any{
				"integration": map[string]any{
					"github":       nil,
					"forges":       forgesList,
					"repositories": crdRepos,
				},
			},
		}

		path := fmt.Sprintf(platformAgentsAPIPath, s.cfg.PodNamespace, s.cfg.PlatformAgentName)
		if _, err := s.kube.doJSON(ctx, http.MethodPatch, path, patch, nil); err != nil {
			return err
		}
	}

	s.reposMu.Lock()
	s.cachedRepos = validatedRepos
	s.cachedTopology = topology
	s.cachedForges = forgesList
	s.cfg.Repositories = validatedRepos
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
	if baseBranch == "" && role != "context" {
		baseBranch = "main"
	}
	if err := validateBaseBranch(baseBranch); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_base_branch", fmt.Sprintf("Invalid base branch: %v", err))
		return
	}

	provider := strings.ToLower(strings.TrimSpace(req.Provider))
	if provider == "" {
		provider = "github"
	}
	forge := strings.TrimSpace(req.Forge)
	if forge == "" {
		forge = provider
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

	s.reposMu.RLock()
	topology := s.cachedTopology
	forges := s.cachedForges
	s.reposMu.RUnlock()

	if topology == "" || topology == "none" {
		if len(existingList) > 1 {
			topology = "split"
		} else {
			topology = "monorepo"
		}
	}

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
	topology := s.cachedTopology
	forges := s.cachedForges
	s.reposMu.RUnlock()

	if len(newList) == 0 {
		topology = "none"
	}

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
