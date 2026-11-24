package controller

import (
	"context"
	"os"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// TestMain clears the operator-level feature flags before any test runs, so a
// developer's shell that exports A2A_INJECT_BACKEND, A2A_AGENT_DOOR,
// A2A_SESSION_CLUSTER_VIEW, A2A_MCP_DOOR, A2A_PROFILE_DISPATCHER or
// A2A_DISPATCHER_JOB_POLICY (how the flags are set on a locally
// run operator) cannot turn an exact-count render test, or a test of which
// doors the gateway renders, into its opposite. Tests that want a flag on set it with
// t.Setenv, which restores this cleared state when they finish.
func TestMain(m *testing.M) {
	for _, name := range []string{a2aInjectBackendEnvVar, a2aAgentDoorEnvVar, a2aSessionClusterViewEnvVar, a2aMCPDoorEnvVar, a2aDispatcherImageEnvVar, a2aProfileDispatcherEnvVar, a2aDispatcherJobPolicyEnvVar, a2aCuratorEnvVar, a2aGatewayElectionEnvVar, a2aBridgeImageEnvVar, a2aBridgeConcurrencyOperatorEnvVar, a2aBridgeExecutorOperatorEnvVar, a2aBridgeResourcesOperatorEnvVar, operatorNamespaceEnvVar, operatorServiceAccountEnvVar} {
		_ = os.Unsetenv(name)
	}
	// No test runs a bus monitoring endpoint. A reconciler built without a
	// stream seam reads a bus whose TASKS has no consumer limit, so the
	// session cap is maxSessions; tests of the cap set busStreamState.
	a2aBusStreamStateDefault = func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
		return map[string]a2aBusStream{a2aTasksStream: {MaxConsumers: -1}}, nil
	}
	os.Exit(m.Run())
}
