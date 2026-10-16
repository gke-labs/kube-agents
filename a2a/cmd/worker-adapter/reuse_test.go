package main

import (
	"io"
	"log/slog"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The env contract for a reused session pod: the spawner's flag reaches
// Config only as the literal "true", and Config carries a second argv for a
// task whose delegate tool is off. That argv is harnessCommand as it is
// under A2A_DELEGATE_TOOL=off, and building it leaves the pod's own argv,
// and the switch, as they were.
func TestConfigFromEnvCarriesTheReuseFlagAndANoDelegateArgv(t *testing.T) {
	for _, key := range []string{"A2A_ALLOWED_TOOLS", "A2A_HARNESS_CMD", "A2A_HARNESS_EXTRA_ARGS", "A2A_DELEGATE_TOOL", lib.EnvClusterView, lib.EnvPrimerFile} {
		t.Setenv(key, "")
	}
	t.Setenv("TASK_ID", "task-env-contract")
	t.Setenv("PROFILE", "chat")
	t.Setenv("NATS_URL", "nats://127.0.0.1:1")
	log := slog.New(slog.NewTextHandler(io.Discard, nil))

	for value, want := range map[string]bool{"": false, "false": false, "TRUE": false, "1": false, "true": true} {
		t.Setenv(lib.EnvSessionReuse, value)
		cfg, ok := configFromEnv(log)
		if !ok {
			t.Fatal("configFromEnv refused the minimal env")
		}
		if cfg.SessionReuse != want {
			t.Errorf("%s=%q: SessionReuse = %v, want %v", lib.EnvSessionReuse, value, cfg.SessionReuse, want)
		}
	}

	cfg, _ := configFromEnv(log)
	with, without := strings.Join(cfg.HarnessCommand, " "), strings.Join(cfg.HarnessCommandNoDelegate, " ")
	if !strings.Contains(with, delegateToolID) || !slices.Contains(cfg.HarnessCommand, "--mcp-config") {
		t.Fatalf("the pod's argv lost the delegate tool: %q", with)
	}
	if strings.Contains(without, delegateToolID) || slices.Contains(cfg.HarnessCommandNoDelegate, "--mcp-config") ||
		strings.Contains(without, delegatePrompt) {
		t.Fatalf("the no-delegate argv still offers the tool: %q", without)
	}
	t.Setenv("A2A_DELEGATE_TOOL", "off")
	if got := strings.Join(harnessCommand(), " "); got != without {
		t.Errorf("the no-delegate argv differs from A2A_DELEGATE_TOOL=off:\n got %q\nwant %q", without, got)
	}
	t.Setenv("A2A_DELEGATE_TOOL", "")
	if !delegateToolEnabled() {
		t.Error("building the no-delegate argv left the delegate switch off")
	}
}
