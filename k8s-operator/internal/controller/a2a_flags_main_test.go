package controller

import (
	"os"
	"testing"
)

// TestMain clears the operator's door flags for the whole package, so a
// shell that exports A2A_INJECT_BACKEND or A2A_AGENT_DOOR (the setting a
// door install needs on the operator) cannot turn a test that expects an
// unarmed render or a withheld gateway into its opposite. Tests that want a
// flag set it with t.Setenv, which restores it afterwards. One place rather
// than a pin per test, which is what let each new flag reopen this.
func TestMain(m *testing.M) {
	os.Unsetenv(a2aInjectBackendEnvVar)
	os.Unsetenv(a2aAgentDoorEnvVar)
	os.Exit(m.Run())
}
