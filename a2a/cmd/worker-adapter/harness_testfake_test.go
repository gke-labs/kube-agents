package main

import (
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// useTestHarness selects a harness configFromEnv accepts, for tests of the
// rest of its wiring, restoring the package's builder afterwards: the worker image carries no harness of its own
// (#6707), so with nothing selected it refuses to start. Antigravity with its
// isolation waiver and a stub path stands in; no task runs it.
func useTestHarness(t *testing.T) {
	t.Helper()
	// configFromEnv installs the selected builder package-wide; put back the
	// one other tests build argvs from.
	was := activeHarness
	t.Cleanup(func() { activeHarness = was })
	t.Setenv(lib.EnvHarness, "antigravity")
	t.Setenv(lib.EnvHarnessUnisolated, lib.HarnessUnisolatedAllow)
	t.Setenv("A2A_HARNESS_PATH", "/bin/true")
}
