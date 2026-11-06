package testing

import (
	"os"
	"strings"
	"testing"
)

// operatorEnv is the operator environment outside the A2A_ flags that a render
// is known to read: image overrides, the telemetry endpoint and discovery, and
// the operator's own identity. A golden is the render of the example CR, not of
// the developer's shell. A new variable the render reads belongs here.
var operatorEnv = []string{
	"PLATFORM_AGENT_IMAGE", "OPERATOR_IMAGE", "AGENT_SANDBOX_IMAGE", "FLUENT_BIT_IMAGE",
	"CREDENTIAL_PROXY_IMAGE", "IMAGE_PULL_SECRETS",
	"OTEL_COLLECTOR_ENDPOINT", "OTEL_COLLECTOR_DISCOVERY",
	"POD_NAMESPACE", "OPERATOR_SERVICE_ACCOUNT",
}

// TestMain clears the operator's environment before any golden renders, as
// internal/controller's TestMain does for its own tests: a shell that exports
// an A2A_ flag (how they are set on a locally run operator) would otherwise
// change the next-mode render -- the bridge's resources, the provisioning
// Job's name, the gateway's env -- and a re-bless would write that machine's
// values into the golden and, through make chart-sync, into footprint.yaml.
// Every A2A_ variable goes, so a flag added later is covered too.
func TestMain(m *testing.M) {
	for _, kv := range os.Environ() {
		if name, _, _ := strings.Cut(kv, "="); strings.HasPrefix(name, "A2A_") {
			_ = os.Unsetenv(name)
		}
	}
	for _, name := range operatorEnv {
		_ = os.Unsetenv(name)
	}
	os.Exit(m.Run())
}
