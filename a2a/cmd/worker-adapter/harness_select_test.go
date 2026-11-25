package main

import (
	"io"
	"log/slog"
	"os"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// TestTheHarnessSelectorReadsOneNameForBothWorkerKinds: A2A_HARNESS picks the
// harness for a session pod and a profile worker alike. PROFILE_HARNESS, its
// older name, counts only on a profile worker and only when A2A_HARNESS is
// unset. Unset is refused, since the worker image carries no harness of its
// own (#6707), and so is claude-code, the prototype harness removed from it.
// A value the worker can't run is an error that names it, never another
// harness in its place.
func TestTheHarnessSelectorReadsOneNameForBothWorkerKinds(t *testing.T) {
	for _, c := range []struct {
		name    string
		env     map[string]string
		want    persona.Harness
		wantErr string
	}{
		{"unset is core-agent", nil, persona.CoreAgent, ""},
		{"claude on a session pod", map[string]string{lib.EnvHarness: "claude-code"}, "", "the claude-code harness was removed"},
		{"claude on a profile worker", map[string]string{lib.EnvHarness: "claude-code", lib.EnvProfileWorker: "true"}, "", "the claude-code harness was removed"},
		{"the older name on a profile worker", map[string]string{lib.EnvProfileHarness: "claude-code", lib.EnvProfileWorker: "true"}, "", "the claude-code harness was removed"},
		{"the older name on a session pod is not read", map[string]string{lib.EnvProfileHarness: "antigravity"}, persona.CoreAgent, ""},
		{"the new name wins over the older", map[string]string{lib.EnvHarness: "claude-code", lib.EnvProfileHarness: "antigravity", lib.EnvProfileWorker: "true"}, "", "the claude-code harness was removed"},
		{"a whole-argv override doesn't bring claude back", map[string]string{lib.EnvHarness: "claude-code", "A2A_HARNESS_CMD": "stub"}, "", "the claude-code harness was removed"},
		{"a whole-argv override doesn't change the harness", map[string]string{"A2A_HARNESS_CMD": "stub"}, persona.CoreAgent, ""},
		{"antigravity without the isolation waiver", map[string]string{lib.EnvHarness: "antigravity", "A2A_HARNESS_PATH": "/bin/agy"}, "", `harness "antigravity" runs without its isolation until #6710`},
		{"antigravity through the older name without the waiver", map[string]string{lib.EnvProfileHarness: "antigravity", lib.EnvProfileWorker: "true", "A2A_HARNESS_PATH": "/bin/agy"}, "", `harness "antigravity" runs without its isolation until #6710`},
		{"the waiver is the literal allow", map[string]string{lib.EnvHarness: "antigravity", lib.EnvHarnessUnisolated: "true", "A2A_HARNESS_PATH": "/bin/agy"}, "", `A2A_HARNESS_UNISOLATED=allow`},
		{"the waiver's case matters", map[string]string{lib.EnvHarness: "antigravity", lib.EnvHarnessUnisolated: "Allow", "A2A_HARNESS_PATH": "/bin/agy"}, "", `A2A_HARNESS_UNISOLATED=allow`},
		{"antigravity with the waiver", map[string]string{lib.EnvHarness: "antigravity", lib.EnvHarnessUnisolated: "allow", "A2A_HARNESS_PATH": "/bin/agy"}, persona.Antigravity, ""},
		{"antigravity with the waiver and a whole-argv override", map[string]string{lib.EnvHarness: "antigravity", lib.EnvHarnessUnisolated: "allow", "A2A_HARNESS_CMD": "stub"}, persona.Antigravity, ""},
		{"antigravity with no path", map[string]string{lib.EnvHarness: "antigravity", lib.EnvHarnessUnisolated: "allow"}, "", `harness "antigravity" has no default path in this image; set A2A_HARNESS_PATH`},
		{"core-agent without the waiver: no waiver needed", map[string]string{lib.EnvHarness: "core-agent"}, persona.CoreAgent, ""},
		{"core-agent through the older name", map[string]string{lib.EnvProfileHarness: "core-agent", lib.EnvProfileWorker: "true"}, persona.CoreAgent, ""},
		{"core-agent with the waiver empty, as the operator renders it", map[string]string{lib.EnvHarness: "core-agent", lib.EnvHarnessUnisolated: ""}, persona.CoreAgent, ""},
		{"core-agent with the waiver set", map[string]string{lib.EnvHarness: "core-agent", lib.EnvHarnessUnisolated: "allow"}, persona.CoreAgent, ""},
		{"no harness named is core-agent", map[string]string{}, persona.CoreAgent, ""},
		{"the waiver alone is core-agent", map[string]string{lib.EnvHarnessUnisolated: "allow"}, persona.CoreAgent, ""},
		{"a typo", map[string]string{lib.EnvHarness: "claude"}, "", `unknown harness "claude"`},
		{"case matters", map[string]string{lib.EnvHarness: "Claude-Code"}, "", `unknown harness "Claude-Code"`},
		{"no trimming before", map[string]string{lib.EnvHarness: " claude-code"}, "", `unknown harness " claude-code"`},
		{"no trimming after", map[string]string{lib.EnvHarness: "claude-code\n"}, "", `unknown harness "claude-code\n"`},
	} {
		t.Run(c.name, func(t *testing.T) {
			h, driver, builder, err := selectHarness(func(k string) string { return c.env[k] })
			if c.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), c.wantErr) {
					t.Fatalf("err %v, want one containing %q", err, c.wantErr)
				}
				if driver != nil || builder != nil {
					t.Errorf("a refused harness still returned a driver %v or builder %v", driver, builder)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if h != c.want || driver.Harness() != c.want {
				t.Errorf("harness %q, driver %q; want %q", h, driver.Harness(), c.want)
			}
			var want harnessBuilder
			switch c.want {
			case persona.Antigravity:
				want = agyBuilder{}
			case persona.CoreAgent:
				want = coreAgentBuilder{}
			}
			if builder != want {
				t.Errorf("builder %T, want %T", builder, want)
			}
		})
	}
}

// TestAWorkerWhoseHarnessItCantRunRefusesToStart: configFromEnv, which run
// turns into exit 1, refuses an unknown harness rather than starting on
// Claude Code, and hands a known one's driver to the adapter.
func TestAWorkerWhoseHarnessItCantRunRefusesToStart(t *testing.T) {
	t.Setenv("TASK_ID", "task-1")
	t.Setenv("PROFILE", "chat")
	t.Setenv("NATS_URL", "nats://localhost:4222")
	t.Setenv(lib.EnvProfileWorker, "")
	log := slog.New(slog.NewJSONHandler(io.Discard, nil))

	t.Setenv(lib.EnvHarness, "claud-code")
	if _, ok := configFromEnv(log); ok {
		t.Error("configFromEnv accepted an unknown harness")
	}

	was := activeHarness
	t.Cleanup(func() { activeHarness = was })
	t.Setenv(lib.EnvHarness, "claude-code")
	if _, ok := configFromEnv(log); ok {
		t.Errorf("configFromEnv started with %s=claude-code; the worker image no longer carries it", lib.EnvHarness)
	}
}

// TestAHarnessWithoutABuilderIsRefused: a harness the worker can't build an
// argv for is an error, never another harness's argv and env. claude-code has
// none: its builder is dormant (#6707).
func TestAHarnessWithoutABuilderIsRefused(t *testing.T) {
	if b, err := builderFor(persona.Antigravity); err != nil || b != (agyBuilder{}) {
		t.Fatalf("builderFor(antigravity) = %v, %v", b, err)
	}
	for _, h := range []persona.Harness{"", persona.ClaudeCode, "nonsense"} {
		if b, err := builderFor(h); err == nil || b != nil {
			t.Errorf("builderFor(%q) = %v, %v; want an error and no builder", h, b, err)
		}
	}
}

// TestAnAgyWorkerStartsOnlyWithTheWaiver: configFromEnv refuses
// A2A_HARNESS=antigravity until the pod also sets A2A_HARNESS_UNISOLATED=allow
// (#6710), and with it hands the adapter agy's driver and builds agy's argv.
func TestAnAgyWorkerStartsOnlyWithTheWaiver(t *testing.T) {
	t.Setenv("TASK_ID", "task-1")
	t.Setenv("PROFILE", "chat")
	t.Setenv("NATS_URL", "nats://localhost:4222")
	t.Setenv(lib.EnvProfileWorker, "")
	t.Setenv("A2A_HARNESS_CMD", "")
	t.Setenv("A2A_HARNESS_PATH", "/opt/agy/bin/agy")
	t.Setenv(lib.EnvHarness, "antigravity")
	log := slog.New(slog.NewJSONHandler(io.Discard, nil))
	was := activeHarness
	t.Cleanup(func() { activeHarness = was })

	t.Setenv(lib.EnvHarnessUnisolated, "")
	if _, ok := configFromEnv(log); ok {
		t.Fatal("configFromEnv started agy without A2A_HARNESS_UNISOLATED=allow")
	}

	t.Setenv(lib.EnvHarnessUnisolated, "allow")
	cfg, ok := configFromEnv(log)
	if !ok {
		t.Fatal("configFromEnv refused agy with the waiver")
	}
	if cfg.Driver == nil || cfg.Driver.Harness() != persona.Antigravity {
		t.Errorf("driver %v, want agy's", cfg.Driver)
	}
	if len(cfg.HarnessCommand) < 2 || cfg.HarnessCommand[0] != "/opt/agy/bin/agy" || cfg.HarnessCommand[1] != "-p=" {
		t.Errorf("argv %q, want agy's print mode on A2A_HARNESS_PATH", cfg.HarnessCommand)
	}
	// The pod's own environment and nothing more: none of Claude Code's
	// model-key placeholder or switches.
	pod := map[string]bool{}
	for _, kv := range os.Environ() {
		pod[kv] = true
	}
	for _, kv := range cfg.HarnessEnv {
		if !pod[kv] {
			t.Errorf("agy's env adds %s", kv)
		}
	}
	if cfg.HarnessUserSettings != "" {
		t.Errorf("user settings %q, want none for agy", cfg.HarnessUserSettings)
	}
}
