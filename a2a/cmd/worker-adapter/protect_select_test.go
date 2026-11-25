package main

import (
	"errors"
	"io"
	"log/slog"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestEveryWorkerProtectsTheAdapterFirst: whatever the harness, the adapter
// is made non-dumpable before the config is handed back, and core-agent,
// named or by default, is refused if that fails.
func TestEveryWorkerProtectsTheAdapterFirst(t *testing.T) {
	t.Setenv("TASK_ID", "task-1")
	t.Setenv("PROFILE", "chat")
	t.Setenv("NATS_URL", "nats://localhost:4222")
	t.Setenv(lib.EnvProfileWorker, "")
	t.Setenv("A2A_HARNESS_CMD", "")
	t.Setenv(lib.EnvHarnessUnisolated, "allow")
	log := slog.New(slog.NewJSONHandler(io.Discard, nil))
	was, wasProtect := activeHarness, protectAdapter
	t.Cleanup(func() { activeHarness, protectAdapter = was, wasProtect })
	var calls int
	var fail error
	protectAdapter = func() error { calls++; return fail }

	for _, c := range []struct {
		name, harness, path string
		fail                error
		wantCalls           int
		wantOK              bool
	}{
		{"core-agent", "core-agent", "/opt/core-agent/bin/core-agent-harness", nil, 1, true},
		{"core-agent, protect fails", "core-agent", "/opt/core-agent/bin/core-agent-harness", errors.New("prctl refused"), 1, false},
		{"absent, which is core-agent", "", "", nil, 1, true},
		{"absent, protect fails", "", "", errors.New("prctl refused"), 1, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			calls, fail = 0, c.fail
			t.Setenv(lib.EnvHarness, c.harness)
			t.Setenv("A2A_HARNESS_PATH", c.path)
			_, ok := configFromEnv(log)
			if ok != c.wantOK {
				t.Errorf("configFromEnv ok=%v, want %v", ok, c.wantOK)
			}
			if calls != c.wantCalls {
				t.Errorf("protectAdapter called %d times, want %d", calls, c.wantCalls)
			}
		})
	}
}
