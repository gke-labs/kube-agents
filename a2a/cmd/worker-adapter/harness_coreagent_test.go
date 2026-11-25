package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The builder's default path is where the worker image puts the binary.
func TestTheCoreAgentHarnessPathIsWhereTheImagePutsIt(t *testing.T) {
	if p := (coreAgentBuilder{}).defaultPath(); p != coreAgentHarnessPath {
		t.Fatalf("defaultPath %q, want %q", p, coreAgentHarnessPath)
	}
	df, err := os.ReadFile(filepath.Join("..", "..", "Dockerfile.worker"))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"-o /out/core-agent-harness ./cmd/core-agent-harness", "COPY --from=build /out/core-agent-harness " + coreAgentHarnessPath} {
		if !strings.Contains(string(df), want) {
			t.Errorf("Dockerfile.worker lacks %q", want)
		}
	}
}

// The argv carries the model and the persona dir, and no session: the
// conversation travels in the stdin envelope.
func TestTheCoreAgentArgvCarriesNoSession(t *testing.T) {
	argv := (coreAgentBuilder{}).command(harnessSpec{path: coreAgentHarnessPath, model: "m"})
	got := strings.Join(argv, " ")
	if !strings.Contains(got, "--model m") || !strings.Contains(got, "--persona-dir "+coreAgentPersonaDir) || strings.Contains(got, "session") {
		t.Errorf("argv %q", got)
	}
}
