package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
)

func TestInferCategory(t *testing.T) {
	cases := []struct {
		repo string
		role string
		want string
	}{
		{"org/iam-policies", "managed", categoryIAM},
		{"org/gke-rbac-roles", "managed", categoryIAM},
		{"org/cloud-security", "managed", categoryIAM},
		{"org/infra-gitops", "gitops", categoryInfra},
		{"org/cluster-fleet", "gitops", categoryInfra},
		{"org/apps-workloads", "managed", categoryWorkloads},
		{"org/microservices-deploy", "managed", categoryWorkloads},
		{"org/unified-monorepo", "gitops", categoryMonorepo},
		{"org/runbooks", "context", categoryContext},
	}

	for _, tc := range cases {
		got := inferCategory(tc.repo, tc.role)
		if got != tc.want {
			t.Errorf("inferCategory(%q, %q) = %q, want %q", tc.repo, tc.role, got, tc.want)
		}
	}
}

func TestValidateBaseBranch(t *testing.T) {
	valid := []string{"", "main", "master", "release/v1.0", "feat-app_v2", "refs/heads/prod"}
	for _, b := range valid {
		if err := validateBaseBranch(b); err != nil {
			t.Errorf("validateBaseBranch(%q) unexpected error: %v", b, err)
		}
	}

	invalid := []string{
		"HEAD",
		"refs/heads/HEAD",
		"refs/heads/_release",
		"refs/heads/-main",
		"refs/heads/.hidden",
		"heads/main",
		"refs/heads/heads/main",
		"refs/heads/refs/heads/main",
		"branch/..",
		"branch/./sub",
		"branch//sub",
		"branch@{1}",
		"branch.lock/file",
		"branch/",
		"branch.",
		"branch.lock",
		strings.Repeat("a", 201),
	}
	for _, b := range invalid {
		if err := validateBaseBranch(b); err == nil {
			t.Errorf("validateBaseBranch(%q) expected error, got nil", b)
		}
	}
}

func TestGenerateHelmValuesYAML(t *testing.T) {
	forges := []forgeInfo{
		{Name: "github", Provider: "github"},
	}
	repos := []repositoryInfo{
		{Forge: "github", Repository: "org/infra", Role: "gitops", BaseBranch: "main"},
		{Forge: "github", Repository: "org/iam-policies", Role: "managed", BaseBranch: "main"},
		{Forge: "github", Repository: "org/workloads", Role: "managed", BaseBranch: "main"},
	}

	yaml := generateHelmValuesYAML(forges, repos)
	if !strings.Contains(yaml, "org/infra") || !strings.Contains(yaml, "org/iam-policies") || !strings.Contains(yaml, "org/workloads") {
		t.Fatalf("generateHelmValuesYAML missing repos:\n%s", yaml)
	}
	if !strings.Contains(yaml, "role: gitops") || !strings.Contains(yaml, "role: managed") {
		t.Fatalf("generateHelmValuesYAML missing roles:\n%s", yaml)
	}
}

func TestRepositoriesEndpoints(t *testing.T) {
	s := newServer(config{
		ListenPort: "8080",
		ModelName:  "gemini-3.5-flash",
	})
	s.kube = nil

	// 1. Initial GET /api/repositories with no repos
	req := httptest.NewRequest(http.MethodGet, "/api/repositories", nil)
	req.Host = "localhost"
	rec := httptest.NewRecorder()
	s.handleRepositoriesGet(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("GET /api/repositories status = %d, want 200", rec.Code)
	}
	var res repositoriesResponse
	if err := json.NewDecoder(rec.Body).Decode(&res); err != nil {
		t.Fatalf("Decode repositoriesResponse: %v", err)
	}
	if res.Topology != "none" {
		t.Errorf("Topology = %q, want 'none'", res.Topology)
	}

	// 2. POST /api/repositories with multiple gitops repos -> must fail
	badBody := repositoriesSetRequest{
		Topology: "split",
		Repositories: []repositoryInfo{
			{Repository: "org/infra", Role: "gitops"},
			{Repository: "org/iam", Role: "gitops"},
		},
	}
	rawBad, _ := json.Marshal(badBody)
	req = httptest.NewRequest(http.MethodPost, "/api/repositories", bytes.NewReader(rawBad))
	req.Host = "localhost"
	rec = httptest.NewRecorder()
	s.handleRepositoriesSet(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("POST /api/repositories status = %d, want 400 for multiple gitops repos", rec.Code)
	}

	// 3. POST /api/repositories with valid split repos (Infra + IAM + Workloads)
	validBody := repositoriesSetRequest{
		Topology: "split",
		Repositories: []repositoryInfo{
			{Repository: "org/infra-gitops", Role: "gitops", BaseBranch: "main", Category: "infra"},
			{Repository: "org/iam-policies", Role: "managed", BaseBranch: "main", Category: "iam"},
			{Repository: "org/apps-workloads", Role: "managed", BaseBranch: "main", Category: "workloads"},
		},
	}
	rawValid, _ := json.Marshal(validBody)
	req = httptest.NewRequest(http.MethodPost, "/api/repositories", bytes.NewReader(rawValid))
	req.Host = "localhost"
	rec = httptest.NewRecorder()
	s.handleRepositoriesSet(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("POST /api/repositories status = %d, want 200; body: %s", rec.Code, rec.Body.String())
	}
	var setRes repositoriesResponse
	if err := json.NewDecoder(rec.Body).Decode(&setRes); err != nil {
		t.Fatalf("Decode repositoriesResponse: %v", err)
	}
	if setRes.Topology != "split" {
		t.Errorf("setRes.Topology = %q, want 'split'", setRes.Topology)
	}
	if len(setRes.Repositories) != 3 {
		t.Fatalf("len(setRes.Repositories) = %d, want 3", len(setRes.Repositories))
	}
	if setRes.Repositories[1].Category != "iam" {
		t.Errorf("setRes.Repositories[1].Category = %q, want 'iam'", setRes.Repositories[1].Category)
	}

	// 4. Verify GET /api/insights now returns the updated repositories
	insReq := httptest.NewRequest(http.MethodGet, "/api/insights", nil)
	insReq.Host = "localhost"
	insRec := httptest.NewRecorder()
	s.handleInsights(insRec, insReq)

	if insRec.Code != http.StatusOK {
		t.Fatalf("GET /api/insights status = %d, want 200", insRec.Code)
	}
	var insRes insightsResponse
	if err := json.NewDecoder(insRec.Body).Decode(&insRes); err != nil {
		t.Fatalf("Decode insightsResponse: %v", err)
	}
	if len(insRes.Repositories) != 3 {
		t.Fatalf("len(insRes.Repositories) = %d, want 3", len(insRes.Repositories))
	}
	if insRes.Repositories[1].Repository != "org/iam-policies" {
		t.Errorf("insRes.Repositories[1].Repository = %q, want 'org/iam-policies'", insRes.Repositories[1].Repository)
	}

	// 5. Test POST Monorepo configuration
	monorepoBody := repositoriesSetRequest{
		Topology: "monorepo",
		Repositories: []repositoryInfo{
			{Repository: "org/fleet-monorepo", Role: "gitops", BaseBranch: "main", Category: "monorepo"},
		},
	}
	rawMono, _ := json.Marshal(monorepoBody)
	req = httptest.NewRequest(http.MethodPost, "/api/repositories", bytes.NewReader(rawMono))
	req.Host = "localhost"
	rec = httptest.NewRecorder()
	s.handleRepositoriesSet(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("POST /api/repositories (monorepo) status = %d, want 200; body: %s", rec.Code, rec.Body.String())
	}
	var monoRes repositoriesResponse
	if err := json.NewDecoder(rec.Body).Decode(&monoRes); err != nil {
		t.Fatalf("Decode monoRes: %v", err)
	}
	if monoRes.Topology != "monorepo" {
		t.Errorf("monoRes.Topology = %q, want 'monorepo'", monoRes.Topology)
	}
	if len(monoRes.Repositories) != 1 || monoRes.Repositories[0].Role != "gitops" {
		t.Errorf("monoRes.Repositories unexpected: %+v", monoRes.Repositories)
	}

	// 6. Test POST /api/repositories/connect
	connectBody := repoConnectRequest{
		Repository: "fkc1e100/kube-agents-apps",
		Role:       "managed",
		Category:   "workloads",
		BaseBranch: "main",
	}
	rawConnect, _ := json.Marshal(connectBody)
	req = httptest.NewRequest(http.MethodPost, "/api/repositories/connect", bytes.NewReader(rawConnect))
	req.Host = "localhost"
	rec = httptest.NewRecorder()
	s.handleRepositoriesConnect(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("POST /api/repositories/connect status = %d, want 200; body: %s", rec.Code, rec.Body.String())
	}
	var connectRes repositoriesResponse
	if err := json.NewDecoder(rec.Body).Decode(&connectRes); err != nil {
		t.Fatalf("Decode connectRes: %v", err)
	}
	if len(connectRes.Repositories) != 2 {
		t.Fatalf("len(connectRes.Repositories) = %d, want 2", len(connectRes.Repositories))
	}

	// 7. Test POST /api/repositories/propose-pr
	prBody := repoProposePRRequest{
		Repository:  "fkc1e100/kube-agents-apps",
		BaseBranch:  "main",
		Title:       "feat(apps): scale hello-app deployment",
		Description: "Scale deployment to 4 replicas for HA",
		Changes:     "apps/simple-app/deployment.yaml: replicas: 4",
	}
	rawPR, _ := json.Marshal(prBody)
	req = httptest.NewRequest(http.MethodPost, "/api/repositories/propose-pr", bytes.NewReader(rawPR))
	req.Host = "localhost"
	rec = httptest.NewRecorder()
	s.handleRepositoriesProposePR(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("POST /api/repositories/propose-pr status = %d, want 200; body: %s", rec.Code, rec.Body.String())
	}
	var prRes repoProposePRResponse
	if err := json.NewDecoder(rec.Body).Decode(&prRes); err != nil {
		t.Fatalf("Decode prRes: %v", err)
	}
	if !strings.Contains(prRes.Prompt, "submit-suggestion") || !strings.Contains(prRes.Prompt, "fkc1e100/kube-agents-apps") {
		t.Errorf("Unexpected prompt: %s", prRes.Prompt)
	}

	// 8. Test POST /api/repositories/disconnect
	disconnectBody := repoDisconnectRequest{
		Repository: "fkc1e100/kube-agents-apps",
	}
	rawDisconnect, _ := json.Marshal(disconnectBody)
	req = httptest.NewRequest(http.MethodPost, "/api/repositories/disconnect", bytes.NewReader(rawDisconnect))
	req.Host = "localhost"
	rec = httptest.NewRecorder()
	s.handleRepositoriesDisconnect(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("POST /api/repositories/disconnect status = %d, want 200; body: %s", rec.Code, rec.Body.String())
	}
	var disconnectRes repositoriesResponse
	if err := json.NewDecoder(rec.Body).Decode(&disconnectRes); err != nil {
		t.Fatalf("Decode disconnectRes: %v", err)
	}
	if len(disconnectRes.Repositories) != 1 {
		t.Fatalf("len(disconnectRes.Repositories) = %d, want 1 after disconnect", len(disconnectRes.Repositories))
	}

	// 9. Context repository with base_branch must be rejected (400)
	ctxWithBranch := repoConnectRequest{
		Repository: "org/runbooks",
		Role:       "context",
		BaseBranch: "main",
	}
	rawCtxBranch, _ := json.Marshal(ctxWithBranch)
	req = httptest.NewRequest(http.MethodPost, "/api/repositories/connect", bytes.NewReader(rawCtxBranch))
	req.Host = "localhost"
	rec = httptest.NewRecorder()
	s.handleRepositoriesConnect(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("connect context repo with base_branch status = %d, want 400", rec.Code)
	}
}

func TestSaveRepositoriesPreservesUnknownClusterFields(t *testing.T) {
	var patchedBody map[string]any
	fakeAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method == http.MethodGet {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"spec": map[string]any{
					"integration": map[string]any{
						"forges": []any{
							map[string]any{
								"name":     "github",
								"provider": "github",
								"auth":     map[string]any{"secretName": "gh-app-creds"},
							},
						},
						"repositories": []any{
							map[string]any{
								"forge":      "github",
								"repository": "org/infra",
								"role":       "gitops",
								"webhook":    map[string]any{"enabled": true},
							},
						},
					},
				},
			})
			return
		}
		if r.Method == http.MethodPatch {
			_ = json.NewDecoder(r.Body).Decode(&patchedBody)
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok"})
			return
		}
		http.NotFound(w, r)
	}))
	defer fakeAPI.Close()

	s := newServer(config{
		PodNamespace:      "kubeagents-system",
		PlatformAgentName: "platform-agent",
	})
	s.kube = &kubeClient{base: fakeAPI.URL, token: "sa-token", client: fakeAPI.Client()}

	err := s.saveRepositoriesToCluster(
		httptest.NewRequest(http.MethodPost, "/api/repositories", nil).Context(),
		"monorepo",
		[]repositoryInfo{{Forge: "github", Provider: "github", Repository: "org/infra", Role: "gitops", BaseBranch: "main"}},
		[]forgeInfo{{Name: "github", Provider: "github"}},
		nil,
	)
	if err != nil {
		t.Fatalf("saveRepositoriesToCluster: %v", err)
	}
	spec, _ := patchedBody["spec"].(map[string]any)
	integ, _ := spec["integration"].(map[string]any)
	if _, hasGitHubField := integ["github"]; hasGitHubField {
		t.Errorf("patch unexpectedly wrote spec.integration.github: %+v", integ)
	}
	forges, _ := integ["forges"].([]any)
	if len(forges) != 1 {
		t.Fatalf("forges = %+v, want 1 entry", forges)
	}
	forge0, _ := forges[0].(map[string]any)
	if forge0["auth"] == nil {
		t.Errorf("forge auth field was stripped: %+v", forge0)
	}
	repos, _ := integ["repositories"].([]any)
	if len(repos) != 1 {
		t.Fatalf("repositories = %+v, want 1 entry", repos)
	}
	repo0, _ := repos[0].(map[string]any)
	if repo0["webhook"] == nil {
		t.Errorf("repository webhook field was stripped: %+v", repo0)
	}
}

func TestDeprecatedGitHubAliasReadAndClearedOnWrite(t *testing.T) {
	var patchedBody map[string]any
	getErr := false
	patchCount := 0
	fakeAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			if getErr {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"spec": map[string]any{
					"integration": map[string]any{
						"github": map[string]any{
							"org":     "acme",
							"gitRepo": "fleet-infra",
						},
					},
				},
			})
			return
		}
		if r.Method == http.MethodPatch {
			patchCount++
			_ = json.NewDecoder(r.Body).Decode(&patchedBody)
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok"})
			return
		}
		http.NotFound(w, r)
	}))
	defer fakeAPI.Close()

	s := newServer(config{
		PodNamespace:      "kubeagents-system",
		PlatformAgentName: "platform-agent",
	})
	s.kube = &kubeClient{base: fakeAPI.URL, token: "sa-token", client: fakeAPI.Client()}

	repos := s.currentRepositories(httptest.NewRequest(http.MethodGet, "/api/repositories", nil).Context())
	if len(repos) != 1 || repos[0].Repository != "acme/fleet-infra" || repos[0].Role != "gitops" {
		t.Fatalf("expected alias acme/fleet-infra (gitops), got %+v", repos)
	}

	// Failed GET before PATCH must abort without issuing a PATCH.
	getErr = true
	if err := s.saveRepositoriesToCluster(
		httptest.NewRequest(http.MethodPost, "/api/repositories", nil).Context(),
		"monorepo",
		repos,
		[]forgeInfo{{Name: "github", Provider: "github"}},
		nil,
	); err == nil {
		t.Fatal("saveRepositoriesToCluster succeeded when pre-patch GET failed")
	}
	if patchCount != 0 {
		t.Fatalf("PATCH was sent (%d times) after failed pre-patch GET", patchCount)
	}

	// Successful GET over deprecated alias clears spec.integration.github in the merge patch.
	getErr = false
	if err := s.saveRepositoriesToCluster(
		httptest.NewRequest(http.MethodPost, "/api/repositories", nil).Context(),
		"monorepo",
		repos,
		[]forgeInfo{{Name: "github", Provider: "github"}},
		nil,
	); err != nil {
		t.Fatalf("saveRepositoriesToCluster: %v", err)
	}
	spec, _ := patchedBody["spec"].(map[string]any)
	integ, _ := spec["integration"].(map[string]any)
	if v, hasGitHubField := integ["github"]; !hasGitHubField || v != nil {
		t.Errorf("expected spec.integration.github: nil to clear deprecated alias, got %+v", integ)
	}
}

// fakeForgeCluster serves one PlatformAgent whose spec.integration is integ on
// every GET, and records the forges of each PATCH. The returned function
// replaces integ, as an administrator's change would.
func fakeForgeCluster(t *testing.T, integ map[string]any) (*server, *[][]map[string]any, func(map[string]any)) {
	t.Helper()
	var patched [][]map[string]any
	fakeAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.Method {
		case http.MethodGet:
			_ = json.NewEncoder(w).Encode(map[string]any{"spec": map[string]any{"integration": integ}})
		case http.MethodPatch:
			var body struct {
				Spec struct {
					Integration struct {
						Forges []map[string]any `json:"forges"`
					} `json:"integration"`
				} `json:"spec"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			patched = append(patched, body.Spec.Integration.Forges)
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok"})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(fakeAPI.Close)
	s := newServer(config{PodNamespace: "kubeagents-system", PlatformAgentName: "platform-agent"})
	s.kube = &kubeClient{base: fakeAPI.URL, token: "sa-token", client: fakeAPI.Client()}
	s.refreshRepositoriesFromCluster(context.Background())
	return s, &patched, func(next map[string]any) { integ = next }
}

func forgeNames(forges []map[string]any) []string {
	names := make([]string, 0, len(forges))
	for _, f := range forges {
		name, _ := f["name"].(string)
		names = append(names, name)
	}
	return names
}

func postJSON(t *testing.T, handler http.HandlerFunc, path string, body any) *httptest.ResponseRecorder {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(raw))
	req.Host = "localhost"
	rec := httptest.NewRecorder()
	handler(rec, req)
	return rec
}

func TestASaveKeepsTheClusterForgeOrder(t *testing.T) {
	s, patched, _ := fakeForgeCluster(t, map[string]any{
		"forges": []any{
			map[string]any{"name": "github", "provider": "github", "namespace": "org"},
			map[string]any{"name": "ghe", "provider": "github", "host": "ghe.corp.example", "namespace": "acme"},
			map[string]any{"name": "gl", "provider": "gitlab", "host": "gitlab.example.com", "namespace": "grp"},
		},
		"repositories": []any{
			map[string]any{"forge": "github", "repository": "org/infra", "role": "gitops"},
			map[string]any{"forge": "ghe", "repository": "acme/app", "role": "managed"},
			map[string]any{"forge": "gl", "repository": "grp/ctx", "role": "context"},
		},
	})
	get := httptest.NewRecorder()
	getReq := httptest.NewRequest(http.MethodGet, "/api/repositories", nil)
	getReq.Host = "localhost"
	s.handleRepositoriesGet(get, getReq)
	var current repositoriesResponse
	if err := json.NewDecoder(get.Body).Decode(&current); err != nil {
		t.Fatal(err)
	}
	want := []string{"github", "ghe", "gl"}

	// The page sends back what it read. Go's map order changes from run to
	// run, so one save could pass by chance; twenty cannot.
	for i := 0; i < 20; i++ {
		rec := postJSON(t, s.handleRepositoriesSet, "/api/repositories",
			repositoriesSetRequest{Repositories: current.Repositories, Forges: current.Forges})
		if rec.Code != http.StatusOK {
			t.Fatalf("save %d: status %d: %s", i, rec.Code, rec.Body)
		}
		if got := forgeNames((*patched)[len(*patched)-1]); !slices.Equal(got, want) {
			t.Fatalf("save %d: forges %v, want the cluster's order %v", i, got, want)
		}
	}

	// A forge the request adds goes after the cluster's.
	added := append(slices.Clone(current.Forges), forgeInfo{Name: "gt", Provider: "gitea", Host: "gitea.example.com"})
	repos := append(slices.Clone(current.Repositories), repositoryInfo{Forge: "gt", Provider: "gitea", Repository: "team/svc", Role: "managed"})
	rec := postJSON(t, s.handleRepositoriesSet, "/api/repositories", repositoriesSetRequest{Repositories: repos, Forges: added})
	if rec.Code != http.StatusOK {
		t.Fatalf("save with a new forge: status %d: %s", rec.Code, rec.Body)
	}
	if got := forgeNames((*patched)[len(*patched)-1]); !slices.Equal(got, []string{"github", "ghe", "gl", "gt"}) {
		t.Errorf("forges %v, want the cluster's three then gt", got)
	}
}

func TestConnectAndDisconnectKeepAForgeWithNoProvider(t *testing.T) {
	s, patched, _ := fakeForgeCluster(t, map[string]any{
		"forges": []any{map[string]any{"name": "github", "namespace": "org"}},
		"repositories": []any{
			map[string]any{"forge": "github", "repository": "org/infra", "role": "gitops"},
		},
	})
	rec := postJSON(t, s.handleRepositoriesConnect, "/api/repositories/connect",
		repoConnectRequest{Repository: "org/app", Role: "managed"})
	if rec.Code != http.StatusOK {
		t.Fatalf("connect: status %d: %s", rec.Code, rec.Body)
	}
	rec = postJSON(t, s.handleRepositoriesDisconnect, "/api/repositories/disconnect",
		repoDisconnectRequest{Repository: "org/app"})
	if rec.Code != http.StatusOK {
		t.Fatalf("disconnect: status %d: %s", rec.Code, rec.Body)
	}
	if len(*patched) != 2 {
		t.Fatalf("%d PATCHes, want 2", len(*patched))
	}
	for i, forges := range *patched {
		for _, f := range forges {
			// The CRD refuses an empty provider; an absent one reads as github.
			if p, ok := f["provider"]; ok && p == "" {
				t.Errorf("PATCH %d: forge %v carries provider \"\"", i, f["name"])
			}
		}
	}
}

func TestASaveFollowsTheClusterOrderOverAnOlderCache(t *testing.T) {
	repos := []any{
		map[string]any{"forge": "github", "repository": "org/infra", "role": "gitops"},
		map[string]any{"forge": "ghe", "repository": "acme/app", "role": "managed"},
	}
	github := map[string]any{"name": "github", "provider": "github", "namespace": "org"}
	ghe := map[string]any{"name": "ghe", "provider": "github", "host": "ghe.corp.example", "namespace": "acme"}
	s, patched, setIntegration := fakeForgeCluster(t, map[string]any{"forges": []any{github, ghe}, "repositories": repos})
	cached := slices.Clone(s.cachedForges)
	cachedRepos := slices.Clone(s.cachedRepos)

	// An administrator reorders the forges after the console read them.
	setIntegration(map[string]any{"forges": []any{ghe, github}, "repositories": repos})
	rec := postJSON(t, s.handleRepositoriesSet, "/api/repositories",
		repositoriesSetRequest{Repositories: cachedRepos, Forges: cached})
	if rec.Code != http.StatusOK {
		t.Fatalf("save: status %d: %s", rec.Code, rec.Body)
	}
	if got := forgeNames((*patched)[0]); !slices.Equal(got, []string{"ghe", "github"}) {
		t.Errorf("forges %v, want the cluster's current order [ghe github]", got)
	}
}
