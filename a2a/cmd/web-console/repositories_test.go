package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
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
}
