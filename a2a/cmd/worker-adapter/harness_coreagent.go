package main

import (
	"os"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/coreagent"
)

// coreAgentBuilder starts core-agent-harness (a2a/cmd/core-agent-harness),
// one process per turn. The turn's tool policy and MCP servers reach it in
// the stdin envelope (workeradapter's coreAgentDriver), never in its argv or
// a file.
type coreAgentBuilder struct{}

// coreAgentPersonaRequiredEnv, "true" on a core-agent profile pod (set by
// the operator with its persona-render init container), makes the harness
// refuse a missing, writable or unmanifested persona rather than run without
// it. A session pod's persona is always required.
const coreAgentPersonaRequiredEnv = "A2A_CORE_AGENT_PERSONA_REQUIRED"

// The two places a core-agent worker's persona can be, one per role, so a
// pod reads only its own: a profile pod whose rendered persona is missing
// fails, rather than find the session's persona the image carries and run
// the profile with it. Variables so a test can point them elsewhere.
var (
	// coreAgentProfilePersonaDir is a profile pod's persona, rendered by
	// the operator's persona-render init container as a second UID and
	// mounted read-only (#6713).
	coreAgentProfilePersonaDir = "/var/lib/agent-persona"
	// coreAgentSessionPersonaDir is a session pod's persona, built into the
	// worker image root-owned with no write bit (a2a/Dockerfile.worker,
	// cmd/session-persona -core-agent-out): the same persona and skills a
	// Claude Code session pod ships.
	coreAgentSessionPersonaDir = "/usr/share/kube-agents/persona/session"
)

// coreAgentPersonaDir is the persona this pod's harness reads, by role.
func coreAgentPersonaDir(getenv func(string) string) string {
	if getenv(lib.EnvProfileWorker) == "true" {
		return coreAgentProfilePersonaDir
	}
	return coreAgentSessionPersonaDir
}

// coreAgentPersonaRequired is whether this pod's harness refuses a missing,
// writable or unmanifested persona: always on a session pod, whose persona
// is in the image; on a profile pod, when the operator says it rendered one.
func coreAgentPersonaRequired(getenv func(string) string) bool {
	if getenv(lib.EnvProfileWorker) == "true" {
		return getenv(coreAgentPersonaRequiredEnv) == "true"
	}
	return true
}

// coreAgentHarnessPath is where the worker image puts core-agent-harness
// (a2a/Dockerfile.worker).
const coreAgentHarnessPath = "/usr/local/bin/core-agent-harness"

func (coreAgentBuilder) defaultPath() string { return coreAgentHarnessPath }

// command takes no session or history: those are the adapter's and travel
// in the stdin envelope.
func (coreAgentBuilder) command(spec harnessSpec) []string {
	argv := []string{spec.path, "--model", spec.model, "--persona-dir", coreAgentPersonaDir(os.Getenv)}
	if coreAgentPersonaRequired(os.Getenv) {
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

// configDir is the persona the harness reads, by role, which the session
// check verifies against its manifest; personaCovers is what a rendering
// holds there (coreagent.Names).
func (coreAgentBuilder) configDir(getenv func(string) string) string {
	return coreAgentPersonaDir(getenv)
}

func (coreAgentBuilder) personaCovers(rel string) bool { return coreagent.Covers(rel) }
