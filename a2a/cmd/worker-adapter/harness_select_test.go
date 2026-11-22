package main

import (
	"io"
	"log/slog"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// TestTheHarnessSelectorReadsOneNameForBothWorkerKinds: A2A_HARNESS picks the
// harness for a session pod and a profile worker alike. PROFILE_HARNESS, its
// older name, counts only on a profile worker and only when A2A_HARNESS is
// unset. Unset is Claude Code. A value the worker can't run is an error that
// names it, never Claude Code in its place.
func TestTheHarnessSelectorReadsOneNameForBothWorkerKinds(t *testing.T) {
	for _, c := range []struct {
		name    string
		env     map[string]string
		want    persona.Harness
		wantErr string
	}{
		{"unset", nil, persona.ClaudeCode, ""},
		{"claude on a session pod", map[string]string{lib.EnvHarness: "claude-code"}, persona.ClaudeCode, ""},
		{"claude on a profile worker", map[string]string{lib.EnvHarness: "claude-code", lib.EnvProfileWorker: "true"}, persona.ClaudeCode, ""},
		{"the older name on a profile worker", map[string]string{lib.EnvProfileHarness: "claude-code", lib.EnvProfileWorker: "true"}, persona.ClaudeCode, ""},
		{"the older name on a session pod is not read", map[string]string{lib.EnvProfileHarness: "nonsense"}, persona.ClaudeCode, ""},
		{"the new name wins over the older", map[string]string{lib.EnvHarness: "claude-code", lib.EnvProfileHarness: "nonsense", lib.EnvProfileWorker: "true"}, persona.ClaudeCode, ""},
		{"antigravity has no driver yet", map[string]string{lib.EnvHarness: "antigravity"}, "", `harness "antigravity" has no worker driver yet`},
		{"antigravity through the older name", map[string]string{lib.EnvProfileHarness: "antigravity", lib.EnvProfileWorker: "true"}, "", `harness "antigravity" has no worker driver yet`},
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
			if _, ok := builder.(claudeBuilder); !ok {
				t.Errorf("builder %T, want claudeBuilder", builder)
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

	t.Setenv(lib.EnvHarness, "")
	// configFromEnv installs the builder the rest of main reads.
	was := activeHarness
	t.Cleanup(func() { activeHarness = was })
	activeHarness = nil
	cfg, ok := configFromEnv(log)
	if !ok {
		t.Fatal("configFromEnv refused the default harness")
	}
	if cfg.Driver == nil || cfg.Driver.Harness() != persona.ClaudeCode {
		t.Errorf("driver %v, want Claude Code's", cfg.Driver)
	}
	if _, ok := activeHarness.(claudeBuilder); !ok {
		t.Errorf("activeHarness %T after configFromEnv, want claudeBuilder", activeHarness)
	}
}

// TestAHarnessWithoutABuilderIsRefused: a harness that has a driver but no
// argv builder is an error, not Claude Code's argv and env.
func TestAHarnessWithoutABuilderIsRefused(t *testing.T) {
	if b, err := builderFor(persona.ClaudeCode); err != nil || b == nil {
		t.Fatalf("builderFor(claude-code) = %v, %v", b, err)
	}
	for _, h := range []persona.Harness{persona.Antigravity, "", "nonsense"} {
		if b, err := builderFor(h); err == nil || b != nil {
			t.Errorf("builderFor(%q) = %v, %v; want an error and no builder", h, b, err)
		}
	}
}
