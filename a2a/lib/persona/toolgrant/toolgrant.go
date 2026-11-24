// Package toolgrant is a worker's tool grant in harness-neutral terms, and
// its rendering for the core-agent harness (github.com/go-steer/core-agent):
// which built-in tools are registered, the allow-mode permission rules the
// gate applies, the MCP tools each server may expose, and whether Gemini's
// server-side search is on.
//
// The grant is enforced at three layers, each fail-closed on its own:
//   - registration: a tool the grant doesn't name is never registered, so
//     the model is never told it exists (CoreAgentGrant.Builtins, MCP);
//   - the gate, in allow mode: a call no allow rule matches is refused, and
//     deny rules win (CoreAgentGrant.Allow, Deny);
//   - the before-tool check: every call is re-checked against the grant as
//     it happens (CoreAgentGrant.Permits), so a tool that reaches the model
//     by some other path is still refused.
//
// The shell's boundary stays the sandbox, the credential broker and the
// verb allowlist; the gate narrows it. core-agent auto-allows a bash prefix
// rule only for a single simple command with a literal argv (no chaining,
// pipes, redirections or expansions), and deny rules match broadly.
package toolgrant

import (
	"fmt"
	"path"
	"regexp"
	"slices"
	"strings"
)

// Policy is a worker's grant in harness-neutral terms.
type Policy struct {
	// ReadFiles: read, list and search files in the workspace.
	ReadFiles bool
	// WriteFiles: create and overwrite files in the workspace. In-place
	// edits and deletes are not granted.
	WriteFiles bool
	// WebSearch: search the web. Fetching an arbitrary URL is a separate
	// grant no worker has, so URL fetch stays off.
	WebSearch bool
	// ShellPrefixes are the only commands the shell may run, each a binary
	// with optional subcommands ("kubectl", "gh pr list"). Empty: no shell.
	ShellPrefixes []string
	// Scripts are persona skill scripts the shell may run, each an
	// absolute path to an executable (shebang, read-only) under a skill's
	// directory, run directly, never through an interpreter word (which
	// would also allow `python3 -c …`). Granting one registers the shell.
	Scripts []string
	// MCP grants tools of named MCP servers.
	MCP []MCPGrant
}

// MCPGrant is a set of tools one MCP server may expose. Tools must be
// named: a grant of a whole server isn't expressible, so a server can't
// add a tool the grant never saw.
type MCPGrant struct {
	Server string
	Tools  []string
}

// CoreAgentMode is the only permission mode a core-agent worker runs in:
// non-interactive, allowlist only. A call no rule allows is refused.
const CoreAgentMode = "allow"

// coreAgentClass is how a core-agent built-in maps to a grant.
type coreAgentClass int

const (
	classNever coreAgentClass = iota
	classRead
	classWrite
	classShell
)

// coreAgentBuiltins classifies every core-agent built-in tool (pkg/tools
// BuiltinToolNames, v3 @ 37bc9f7). The harness seam's test fails when
// core-agent has a built-in this table doesn't name, so a new tool is never
// registered by default.
var coreAgentBuiltins = map[string]coreAgentClass{
	"bash":              classShell,
	"read_file":         classRead,
	"read_many_files":   classRead,
	"view_file_outline": classRead,
	"write_file":        classWrite,
	"edit_file":         classNever, // in-place edits: the session default denies them
	"delete_file":       classNever,
	"stat":              classRead,
	"list_dir":          classRead,
	"glob":              classRead,
	"grep":              classRead,
	"json_query":        classNever, // not granted unless a persona needs it
	"fetch_url":         classNever, // URL fetch: no worker has the grant
	"alert":             classNever,
	"wait_and_verify":   classNever,
	"todo":              classNever, // not granted unless a persona needs it
	"record_plan":       classNever,
	"sciontool_status":  classNever,
}

// coreAgentSpawnTools are the background-agent tools core-agent registers
// when its background manager is built. A worker never builds one, and no
// MCP tool may take their names.
var coreAgentSpawnTools = []string{"spawn_agent", "stop_agent"}

// CoreAgentSkillTools are the tools a persona's skills toolset offers
// (ADK's skilltoolset). They only read the persona's skills snapshot; a
// skill's script runs, if at all, through bash and its gate. Every worker
// with skills has them, and no MCP tool may take their names.
var CoreAgentSkillTools = []string{"list_skills", "load_skill", "load_skill_resource"}

// maxToolNameLen is the longest function name Gemini accepts.
const maxToolNameLen = 64

// CoreAgentRegistrable reports whether a grant may register the named
// built-in: it is classified, and its class is one a Policy grants.
func CoreAgentRegistrable(name string) bool {
	c, ok := coreAgentBuiltins[name]
	return ok && c != classNever
}

// CoreAgentBuiltinNames is every built-in name the table classifies,
// sorted.
func CoreAgentBuiltinNames() []string {
	names := make([]string, 0, len(coreAgentBuiltins))
	for n := range coreAgentBuiltins {
		names = append(names, n)
	}
	slices.Sort(names)
	return names
}

// CoreAgentGrant is a Policy as core-agent needs it.
type CoreAgentGrant struct {
	// Builtins are the built-in tools to register, sorted.
	Builtins []string
	// Allow and Deny are gate rules, "<tool>:<glob>", for CoreAgentMode.
	Allow, Deny []string
	// MCP maps a server to the tools it may expose (its tool allowlist).
	MCP map[string][]string
	// GeminiGoogleSearch turns on Gemini's server-side search. Its URL
	// context and code execution stay off whatever the grant.
	GeminiGoogleSearch bool
}

var (
	shellPrefix = regexp.MustCompile(`^[A-Za-z0-9._-]+( [A-Za-z0-9._-]+)*$`)
	// scriptPath is an absolute path of plain segments: no spaces, quotes,
	// globs or shell syntax.
	scriptPath = regexp.MustCompile(`^(/[A-Za-z0-9._-]+)+$`)
	// mcpName keeps server and tool names inside the characters a model
	// function name allows, so core-agent's namespacing (<server>_<tool>)
	// never has to rewrite one and two tools can't collide.
	mcpName = regexp.MustCompile(`^[A-Za-z0-9_]{1,64}$`)
)

// CoreAgent renders p for a worker whose workspace is the absolute path
// workspace.
func (p Policy) CoreAgent(workspace string) (CoreAgentGrant, error) {
	if !strings.HasPrefix(workspace, "/") || workspace == "/" || path.Clean(workspace) != workspace ||
		strings.ContainsAny(workspace, "*?[]\\:") {
		return CoreAgentGrant{}, fmt.Errorf("workspace %q is not a clean absolute path below /", workspace)
	}
	// An open-prefix allow rule matches inside the prefix only, after
	// cleaning and symlink resolution (core-agent's globMatchAllow).
	inWorkspace := workspace + "/*"
	g := CoreAgentGrant{MCP: map[string][]string{}, GeminiGoogleSearch: p.WebSearch}
	for _, name := range CoreAgentBuiltinNames() {
		switch c := coreAgentBuiltins[name]; {
		case c == classRead && p.ReadFiles, c == classWrite && p.WriteFiles:
			g.Builtins = append(g.Builtins, name)
			g.Allow = append(g.Allow, name+":"+inWorkspace)
		case c == classShell && (len(p.ShellPrefixes) > 0 || len(p.Scripts) > 0):
			g.Builtins = append(g.Builtins, name)
		case c == classRead, c == classWrite, c == classShell:
			// Not registered, and denied should it reach the gate anyway.
			g.Deny = append(g.Deny, name+":*")
		}
	}
	// core-agent's bash allow rules: "<cmd>" allows exactly that command
	// and "<cmd> *" that command with arguments, each only as a single
	// simple command with a literal argv (no chaining, pipes, redirections,
	// env prefixes or expansions); a command needs both to run with and
	// without arguments.
	for _, prefix := range p.ShellPrefixes {
		if !shellPrefix.MatchString(prefix) {
			return CoreAgentGrant{}, fmt.Errorf("shell prefix %q is not a binary and its subcommands", prefix)
		}
		g.Allow = append(g.Allow, "bash:"+prefix, "bash:"+prefix+" *")
	}
	for _, script := range p.Scripts {
		if !scriptPath.MatchString(script) || path.Clean(script) != script {
			return CoreAgentGrant{}, fmt.Errorf("script %q is not a clean absolute path", script)
		}
		g.Allow = append(g.Allow, "bash:"+script, "bash:"+script+" *")
	}
	for _, m := range p.MCP {
		if !mcpName.MatchString(m.Server) {
			return CoreAgentGrant{}, fmt.Errorf("MCP server name %q", m.Server)
		}
		if _, dup := g.MCP[m.Server]; dup {
			return CoreAgentGrant{}, fmt.Errorf("MCP server %q granted twice", m.Server)
		}
		if len(m.Tools) == 0 {
			return CoreAgentGrant{}, fmt.Errorf("MCP server %q: a grant names its tools", m.Server)
		}
		for _, t := range m.Tools {
			if !mcpName.MatchString(t) {
				return CoreAgentGrant{}, fmt.Errorf("MCP server %q: tool name %q", m.Server, t)
			}
		}
		tools := slices.Clone(m.Tools)
		slices.Sort(tools)
		g.MCP[m.Server] = slices.Compact(tools)
	}
	// core-agent registers an MCP tool as <server>_<tool> (its server part
	// sanitized to [A-Za-z0-9_], which mcpName already guarantees, and the
	// tool part as the server names it), and both may contain "_": server
	// a / tool b_c and server a_b / tool c are both a_b_c. A grant is
	// refused where two names meet, where one meets any core-agent built-in
	// or spawn tool (granted or not: the gate's rules are keyed by name), or
	// where one is longer than a model function name may be.
	seen := map[string]bool{}
	for _, b := range CoreAgentBuiltinNames() {
		seen[b] = true
	}
	for _, s := range slices.Concat(coreAgentSpawnTools, CoreAgentSkillTools) {
		seen[s] = true
	}
	for server, tools := range g.MCP {
		for _, t := range tools {
			name := server + "_" + t
			if seen[name] {
				return CoreAgentGrant{}, fmt.Errorf("MCP tool name %q is ambiguous in this grant", name)
			}
			if len(name) > maxToolNameLen {
				return CoreAgentGrant{}, fmt.Errorf("MCP tool name %q is longer than %d", name, maxToolNameLen)
			}
			seen[name] = true
		}
	}
	return g, nil
}

// Permits reports whether a call to the named tool is within the grant:
// one of its built-ins, or an MCP tool as core-agent names it
// (<server>_<tool>). The before-tool check refuses anything else.
func (g CoreAgentGrant) Permits(name string) bool {
	if slices.Contains(g.Builtins, name) {
		return true
	}
	for server, tools := range g.MCP {
		if t, ok := strings.CutPrefix(name, server+"_"); ok && slices.Contains(tools, t) {
			return true
		}
	}
	return false
}
