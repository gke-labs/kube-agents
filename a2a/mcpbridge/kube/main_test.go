package kube

import (
	"os"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/mcpbridge"
)

// Shared fixtures for the package tests.
const (
	testClusterName    = "prod-us-central1"
	testKubeContext    = "gke_example-project_us-central1_prod-us-central1"
	testUnreachableCtx = "gke_example-project_us-central1_unreachable"
	testNamespace      = "kubeagents-system"
	testPAName         = "platform-agent"
	testProjectID      = "example-project"
	testLocation       = "us-central1"
	testGitHubRepo     = "example-org/example-infra"
	testMCPToken       = "mcp-bearer-secret-token"
)

func TestMain(m *testing.M) {
	os.Unsetenv(mcpbridge.EnvMCPToken)
	os.Unsetenv(mcpbridge.EnvMCPTokenDeployment)
	os.Exit(m.Run())
}
