package main

import (
	"os"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib/persona/coreagent"
)

// coreAgentBuilder starts core-agent-harness (a2a/cmd/core-agent-harness),
// one process per turn. The turn's tool policy and MCP servers reach it in
// the stdin envelope (workeradapter's coreAgentDriver), never in its argv or
// a file.
type coreAgentBuilder struct{}

const (
	// coreAgentPersonaDir is the persona the harness reads once per turn:
	// rendered by a second UID, read-only to the harness (#6713).
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

// env is the pod environment less coreAgentWithheldPrefixes: no model
// credential, endpoint or Vertex setting. startHarnessGateway then points
// GOOGLE_GEMINI_BASE_URL at the loopback gateway proxy, and the harness
// sends only a placeholder key.
func (coreAgentBuilder) env(env []string, _ func(string) bool) []string {
	out := make([]string, 0, len(env))
	for _, kv := range env {
		if i := strings.Index(kv, "="); i > 0 && coreAgentWithheld(kv[:i]) {
			continue
		}
		out = append(out, kv)
	}
	return out
}

// userSettings: none; the harness reads no settings file.
func (coreAgentBuilder) userSettings(func(string) string) string { return "" }

// configDir is the rendered persona the harness reads, which the session
// check verifies against its manifest; personaCovers is what a rendering
// holds there (coreagent.Names).
func (coreAgentBuilder) configDir(func(string) string) string { return coreAgentPersonaDir }

func (coreAgentBuilder) personaCovers(rel string) bool { return coreagent.Covers(rel) }
