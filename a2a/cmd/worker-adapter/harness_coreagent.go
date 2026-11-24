package main

import "os"

// coreAgentBuilder starts core-agent-harness (a2a/cmd/core-agent-harness),
// one process per turn. The turn's tool policy and MCP servers reach it in
// the stdin envelope (workeradapter's coreAgentDriver), never in its argv or
// a file.
type coreAgentBuilder struct{}

const (
	// coreAgentPersonaDir is the persona the harness reads once per turn:
	// rendered by a second UID, read-only to the harness (SEAM: dev-2, #6713).
	coreAgentPersonaDir = "/var/lib/agent-persona"
	// coreAgentSessionDBEnv names the session log's path; it defaults to
	// coreAgentSessionDB (SEAM: tigerteam-dev-3, the per-pod state dir).
	coreAgentSessionDBEnv = "A2A_CORE_AGENT_SESSION_DB"
	coreAgentSessionDB    = "/var/lib/agent-state/core-agent-sessions.db"
)

// defaultPath is empty until the worker image ships the binary: a pod names
// it with A2A_HARNESS_PATH.
func (coreAgentBuilder) defaultPath() string { return "" }

func (coreAgentBuilder) command(spec harnessSpec) []string {
	db := os.Getenv(coreAgentSessionDBEnv)
	if db == "" {
		db = coreAgentSessionDB
	}
	argv := []string{spec.path, "--model", spec.model, "--session-db", db, "--persona-dir", coreAgentPersonaDir}
	return append(argv, spec.extra...)
}

// env passes the pod environment through: the model's base URL,
// GOOGLE_GEMINI_BASE_URL, points at the loopback gateway proxy (SEAM:
// tigerteam-dev-2), and the harness sends only a placeholder key.
func (coreAgentBuilder) env(env []string, _ func(string) bool) []string { return env }

// userSettings: none; the harness reads no settings file.
func (coreAgentBuilder) userSettings(func(string) string) string { return "" }
