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
	// coreAgentPersonaRequiredEnv, "true" on a pod the operator renders a
	// persona for, makes a missing persona dir fail the turn
	// (TODO(#7143): the operator sets it with the persona render).
	coreAgentPersonaRequiredEnv = "A2A_CORE_AGENT_PERSONA_REQUIRED"
)

// coreAgentHarnessPath is where the worker image puts core-agent-harness
// (a2a/Dockerfile.worker).
const coreAgentHarnessPath = "/usr/local/bin/core-agent-harness"

func (coreAgentBuilder) defaultPath() string { return coreAgentHarnessPath }

// command takes no session or history: those are the adapter's and travel
// in the stdin envelope.
func (coreAgentBuilder) command(spec harnessSpec) []string {
	argv := []string{spec.path, "--model", spec.model, "--persona-dir", coreAgentPersonaDir}
	if os.Getenv(coreAgentPersonaRequiredEnv) == "true" {
		argv = append(argv, "--persona-required")
	}
	return append(argv, spec.extra...)
}

// env passes the pod environment through: the model's base URL,
// GOOGLE_GEMINI_BASE_URL, points at the loopback gateway proxy (SEAM:
// tigerteam-dev-2), and the harness sends only a placeholder key.
func (coreAgentBuilder) env(env []string, _ func(string) bool) []string { return env }

// userSettings: none; the harness reads no settings file.
func (coreAgentBuilder) userSettings(func(string) string) string { return "" }
