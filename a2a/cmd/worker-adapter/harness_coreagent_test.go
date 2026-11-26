package main

import (
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
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

// A pod configured for a persona passes --persona-required.
func TestACoreAgentPodConfiguredForAPersonaRequiresIt(t *testing.T) {
	t.Setenv(coreAgentPersonaRequiredEnv, "true")
	argv := strings.Join((coreAgentBuilder{}).command(harnessSpec{path: coreAgentHarnessPath, model: "m"}), " ")
	if !strings.Contains(argv, "--persona-required") {
		t.Errorf("argv %q", argv)
	}
}

// coreAgentInstructions is the system-prompt lines the configured driver
// sends in a turn's envelope.
func coreAgentInstructions(t *testing.T, cfg workeradapter.Config) []string {
	t.Helper()
	b, err := cfg.Driver.EncodeUser("hi")
	if err != nil {
		t.Fatal(err)
	}
	var env coreagentwire.Envelope
	if err := json.Unmarshal(b, &env); err != nil {
		t.Fatal(err)
	}
	return env.Instructions
}

// A core-agent session pod gets the same cluster-access line Claude Code
// gets on its argv, in each turn's envelope: the view's line with the view
// on, the no-cluster line without it, neither when an override leaves the
// view's Bash out. Never the delegate line, with the delegate tool on or
// off: core-agent has no delegate tool yet. A profile worker gets none.
func TestACoreAgentSessionGetsItsClusterPromptInTheEnvelope(t *testing.T) {
	t.Setenv("TASK_ID", "task-1")
	t.Setenv("PROFILE", "chat")
	t.Setenv("NATS_URL", "nats://localhost:4222")
	t.Setenv(lib.EnvProfileWorker, "")
	t.Setenv(lib.EnvProfileExecutor, "")
	t.Setenv("A2A_HARNESS_CMD", "")
	t.Setenv(lib.EnvHarness, "core-agent")
	t.Setenv(lib.EnvHarnessUnisolated, lib.HarnessUnisolatedAllow)
	log := slog.New(slog.NewJSONHandler(io.Discard, nil))
	was := activeHarness
	t.Cleanup(func() { activeHarness = was })

	for _, tc := range []struct {
		name, view, allowed, delegate string
		want                          []string
	}{
		{"cluster view", "true", "", "", []string{clusterViewPrompt}},
		{"cluster view, delegate off", "true", "", "off", []string{clusterViewPrompt}},
		{"no cluster view", "", "", "", []string{noClusterPrompt}},
		{"no cluster view, delegate off", "", "", "off", []string{noClusterPrompt}},
		{"view without Bash", "true", "Read,Glob", "", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(lib.EnvClusterView, tc.view)
			t.Setenv("A2A_ALLOWED_TOOLS", tc.allowed)
			t.Setenv("A2A_DELEGATE_TOOL", tc.delegate)
			cfg, ok := configFromEnv(log)
			if !ok {
				t.Fatal("configFromEnv refused the core-agent session")
			}
			got := coreAgentInstructions(t, cfg)
			if !slices.Equal(got, tc.want) {
				t.Errorf("instructions %q, want %q", got, tc.want)
			}
			if slices.Contains(got, delegatePrompt) {
				t.Errorf("a core-agent session was told of the delegate tool it doesn't have")
			}
			if got := coreAgentInstructions(t, profileModeConfig(cfg)); len(got) != 0 {
				t.Errorf("profile worker instructions %q, want none", got)
			}
		})
	}
}
