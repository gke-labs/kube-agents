package main

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestARefusedToolGrantLogsItsOwnMessage: when the worker stops because its
// tool grant is refused, the log line names the grant, not the harness
// (#7376). The refusal itself is unchanged; the other selectHarness
// failures keep the harness message.
func TestARefusedToolGrantLogsItsOwnMessage(t *testing.T) {
	coreAgent := map[string]string{
		lib.EnvHarness:           "core-agent",
		lib.EnvHarnessUnisolated: lib.HarnessUnisolatedAllow,
		"A2A_HARNESS_PATH":       "/bin/core-agent-harness",
		"A2A_WORKDIR":            "/work",
		lib.EnvClusterView:       "true",
	}
	with := func(extra map[string]string) map[string]string {
		env := map[string]string{}
		for k, v := range coreAgent {
			env[k] = v
		}
		for k, v := range extra {
			env[k] = v
		}
		return env
	}
	for _, tc := range []struct {
		name    string
		env     map[string]string
		wantMsg string
		wantErr string
	}{
		{
			name:    "unrestricted shell prefix sh",
			env:     with(map[string]string{"A2A_ALLOWED_TOOLS": "Bash(sh:*)"}),
			wantMsg: "the worker's tool grant is refused",
			wantErr: `shell prefix "sh" runs anything: it is an unrestricted shell`,
		},
		{
			name:    "unrestricted shell prefix setsid",
			env:     with(map[string]string{"A2A_ALLOWED_TOOLS": "Bash(setsid:*)"}),
			wantMsg: "the worker's tool grant is refused",
			wantErr: `shell prefix "setsid" runs anything: it is an unrestricted shell`,
		},
		{
			name:    "web search",
			env:     with(map[string]string{"A2A_ALLOWED_TOOLS": "Read,WebSearch"}),
			wantMsg: "the worker's tool grant is refused",
			// The refusal's wording is coreAgentSessionGrant's; this pins
			// only that it names web search.
			wantErr: "web search",
		},
		{
			name:    "unknown harness",
			env:     map[string]string{lib.EnvHarness: "no-such-harness"},
			wantMsg: "the worker's harness is not one it can run",
			wantErr: "no-such-harness",
		},
		{
			name:    "core-agent without its isolation consent",
			env:     with(map[string]string{lib.EnvHarnessUnisolated: ""}),
			wantMsg: "the worker's harness is not one it can run",
			wantErr: "runs without its isolation",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("TASK_ID", "task-grant-log")
			t.Setenv("PROFILE", "chat")
			t.Setenv("NATS_URL", "nats://127.0.0.1:1")
			for _, k := range []string{lib.EnvHarness, lib.EnvHarnessUnisolated, lib.EnvProfileHarness, lib.EnvProfileWorker,
				lib.EnvClusterView, "A2A_HARNESS_PATH", "A2A_HARNESS_CMD", "A2A_WORKDIR", "A2A_ALLOWED_TOOLS", "A2A_DISALLOWED_TOOLS"} {
				t.Setenv(k, tc.env[k])
			}
			var logged bytes.Buffer
			if _, ok := configFromEnv(slog.New(slog.NewJSONHandler(&logged, nil))); ok {
				t.Fatalf("configFromEnv accepted the environment; want a refusal. log: %s", logged.String())
			}
			var found bool
			for _, line := range strings.Split(strings.TrimSpace(logged.String()), "\n") {
				var rec struct {
					Level string `json:"level"`
					Msg   string `json:"msg"`
					Err   string `json:"err"`
				}
				if json.Unmarshal([]byte(line), &rec) != nil || rec.Level != "ERROR" {
					continue
				}
				found = true
				if rec.Msg != tc.wantMsg {
					t.Errorf("msg = %q, want %q", rec.Msg, tc.wantMsg)
				}
				if !strings.Contains(rec.Err, tc.wantErr) {
					t.Errorf("err = %q, want it to carry %q", rec.Err, tc.wantErr)
				}
			}
			if !found {
				t.Fatalf("no ERROR line logged: %s", logged.String())
			}
		})
	}
}
