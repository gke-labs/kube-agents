package controller

import (
	"os"
	"testing"
)

// TestMain clears the operator-level feature flags before any test runs, so a
// developer's shell that exports A2A_INJECT_BACKEND, A2A_AGENT_DOOR or
// A2A_SESSION_CLUSTER_VIEW (how the flags are set on a locally run operator)
// cannot turn an exact-count render test, or a withheld-gateway test, into its
// opposite. Tests that want a flag on set it with
// t.Setenv, which restores this cleared state when they finish.
func TestMain(m *testing.M) {
	for _, name := range []string{a2aInjectBackendEnvVar, a2aAgentDoorEnvVar, a2aSessionClusterViewEnvVar, a2aBridgeImageEnvVar, a2aBridgeConcurrencyOperatorEnvVar, a2aBridgeExecutorOperatorEnvVar, operatorNamespaceEnvVar, operatorServiceAccountEnvVar} {
		_ = os.Unsetenv(name)
	}
	os.Exit(m.Run())
}
