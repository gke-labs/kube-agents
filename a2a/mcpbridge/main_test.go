package mcpbridge

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
)

const (
	testClusterName = "prod-us-central1"
	testKubeContext = "gke_example-project_us-central1_prod-us-central1"
	testNamespace   = "kubeagents-system"
	testPAName      = "platform-agent"
	testProjectID   = "example-project"
	testLocation    = "us-central1"
	testGitHubRepo  = "example-org/example-infra"
	testPRNumber    = 42
	testPRURL       = "https://github.com/example-org/example-infra/pull/42"
)

// TestMain isolates the package tests from the developer's workstation environment and points
// the upstream release lookup at a local stub so no test reaches api.github.com.
func TestMain(m *testing.M) {
	for _, name := range []string{
		envConfigPath,
		EnvMCPToken,
		EnvMCPTokenDeployment,
		envGHToken,
		envGitHubToken,
	} {
		os.Unsetenv(name)
	}

	home, err := os.MkdirTemp("", "kube-agents-mcp-test-home-")
	if err != nil {
		panic(err)
	}
	os.Setenv("HOME", home)

	releaseStub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case githubLatestReleasePath:
			_ = json.NewEncoder(w).Encode(map[string]any{
				"tag_name":         "0.16.0",
				"target_commitish": "main",
				"html_url":         "https://github.com/gke-labs/kube-agents/releases/tag/0.16.0",
				"published_at":     "2026-01-01T00:00:00Z",
			})
		default:
			http.NotFound(w, r)
		}
	}))
	ResetReleaseCacheForTest(releaseStub.URL, releaseStub.Client())

	code := m.Run()
	releaseStub.Close()
	_ = os.RemoveAll(home)
	os.Exit(code)
}
