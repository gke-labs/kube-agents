package controller

import (
	"os"
	"testing"
)

// TestMain clears the operator-level feature flags before any test runs, so a
// developer's shell that exports A2A_INJECT_BACKEND or A2A_SESSION_CLUSTER_VIEW
// (how the flags are set on a locally run operator) cannot turn an exact-count
// render test into its opposite. Tests that want a flag on set it with
// t.Setenv, which restores this cleared state when they finish.
func TestMain(m *testing.M) {
	for _, name := range []string{a2aInjectBackendEnvVar, a2aSessionClusterViewEnvVar} {
		_ = os.Unsetenv(name)
	}
	os.Exit(m.Run())
}
