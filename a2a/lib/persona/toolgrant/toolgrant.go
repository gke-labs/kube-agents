// Package toolgrant is a worker's tool grant in harness-neutral terms, and
// its rendering for the core-agent harness (github.com/go-steer/core-agent):
// which built-in tools are registered, the allow-mode permission rules the
// gate applies, the MCP tools each server may expose, and whether Gemini's
// server-side search is on.
//
// The grant is enforced at three layers, each fail-closed on its own:
//   - registration: a built-in or MCP tool the grant doesn't name is never
//     registered, so the model is never told it exists (Builtins, MCP); a
//     persona's three read-only skill tools are the one fixed addition;
//   - the gate, in allow mode: a call no allow rule matches is refused, and
//     deny rules win (CoreAgentGrant.Allow, Deny);
//   - the before-tool check: the harness's plugin re-checks every call
//     against the grant's tools as it happens (plus the read-only skill
//     tools), so a tool that reaches the model by some other path is still
//     refused. Permits is the same check, for code outside the harness.
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
	// absolute path under ScriptRoot, run directly, never through an
	// interpreter word (which would also allow `python3 -c …`). Granting
	// one registers the shell. That each is an executable in a skill's
	// directory is the persona renderer's check (coreagent.ScriptPaths).
	Scripts []string
	// ScriptRoot is the directory every script must sit under (the
	// persona's skills directory); required when Scripts is set.
	ScriptRoot string
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
// BuiltinToolNames, v3.2.0 as go.mod pins it). The harness seam's test fails when
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

// coreAgentModelTools are the tools core-agent itself may register beyond
// the built-ins (background agents, remote agents, task completion, alerts,
// memory, agent transfer). A worker registers none of them, and no MCP tool
// may take their names.
var coreAgentModelTools = []string{
	"spawn_agent", "stop_agent", "spawn_remote_agent", "mark_task_done", "finish_task",
	"return_result", "report_alert", "transfer_to_agent", "load_memory", "preload_memory",
}

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
	// GeminiGoogleSearch is whether the grant asks for Gemini's server-side
	// search. Nothing turns it on yet: the harness's model has every Gemini
	// built-in off, and the session grant refuses web search.
	GeminiGoogleSearch bool
}

var (
	shellPrefix = regexp.MustCompile(`^[A-Za-z0-9._-]+( [A-Za-z0-9._-]+)*$`)
	// unrestrictedShellWords run another command or arbitrary code from
	// their arguments, so a prefix grant of one is an unrestricted shell
	// (python* is matched by prefix as well).
	unrestrictedShellWords = []string{".", "source", "sh", "bash", "zsh", "dash", "ksh", "fish", "env", "exec", "eval",
		"command", "builtin", "nohup", "nice", "timeout", "xargs", "sudo", "su", "doas", "perl", "ruby", "node",
		"php", "lua", "awk", "gawk", "busybox", "script", "watch", "time", "stdbuf", "setsid", "chroot"}
	// scriptPath is an absolute path of plain segments: no spaces, quotes,
	// globs or shell syntax.
	scriptPath = regexp.MustCompile(`^(/[A-Za-z0-9._-]+)+$`)
	// mcpName keeps server and tool names inside the characters a model
	// function name allows, so core-agent's namespacing (<server>_<tool>)
	// never has to rewrite one; CheckMCPNames refuses collisions.
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
			// Not registered. The deny is defence in depth for the write
			// and shell classes; core-agent's read check consults only the
			// path scope, so for reads registration and the scope are what
			// hold.
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
		if first, _, _ := strings.Cut(prefix, " "); slices.Contains(unrestrictedShellWords, first) || strings.HasPrefix(first, "python") {
			return CoreAgentGrant{}, fmt.Errorf("shell prefix %q runs anything: it is an unrestricted shell", prefix)
		}
		g.Allow = append(g.Allow, "bash:"+prefix, "bash:"+prefix+" *")
	}
	if len(p.Scripts) > 0 && (!scriptPath.MatchString(p.ScriptRoot) || path.Clean(p.ScriptRoot) != p.ScriptRoot) {
		return CoreAgentGrant{}, fmt.Errorf("script root %q is not a clean absolute path", p.ScriptRoot)
	}
	for _, script := range p.Scripts {
		if !scriptPath.MatchString(script) || path.Clean(script) != script {
			return CoreAgentGrant{}, fmt.Errorf("script %q is not a clean absolute path", script)
		}
		if !strings.HasPrefix(script, p.ScriptRoot+"/") {
			return CoreAgentGrant{}, fmt.Errorf("script %q is not under %s", script, p.ScriptRoot)
		}
		g.Allow = append(g.Allow, "bash:"+script, "bash:"+script+" *")
	}
	// The gate checks an MCP call as namespace "mcp" with the key core-agent's
	// summarizeRequest builds: the tool's name, then a space and its
	// arguments as JSON ("srv_a null" for a call ADK passes with nil args).
	// "mcp:<server>_<tool>" and "mcp:<server>_<tool> *" allow that tool and
	// no other (the space keeps srv_a from matching srv_ab).
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
		for _, t := range g.MCP[m.Server] {
			name := m.Server + "_" + t
			g.Allow = append(g.Allow, "mcp:"+name, "mcp:"+name+" *")
		}
	}
	// core-agent registers an MCP tool as <server>_<tool> (its server part
	// sanitized to [A-Za-z0-9_], which mcpName already guarantees, and the
	// tool part as the server names it), and both may contain "_": server
	// a / tool b_c and server a_b / tool c are both a_b_c. A grant is
	// refused where two names meet, where one meets any core-agent built-in
	// or spawn tool (granted or not: the gate's rules are keyed by name), or
	// where one is longer than a model function name may be.
	if err := CheckMCPNames(g.MCP); err != nil {
		return CoreAgentGrant{}, err
	}
	return g, nil
}

// CheckMCPNames refuses a set of MCP grants, server to tools, whose names
// core-agent would register ambiguously: a server or tool name outside
// [A-Za-z0-9_]{1,64}, a <server>_<tool> name two grants share, one equal to
// any name core-agent itself may register (its built-ins, skill tools and
// model tools, granted or not: the gate's rules are keyed by name), or one
// longer than a model function name may be. The harness runs it again on
// what the envelope carries, so its before-tool check doesn't rest on the
// adapter's.
func CheckMCPNames(servers map[string][]string) error {
	seen := map[string]bool{}
	for _, b := range CoreAgentBuiltinNames() {
		seen[b] = true
	}
	for _, s := range slices.Concat(coreAgentModelTools, CoreAgentSkillTools) {
		seen[s] = true
	}
	names := make([]string, 0, len(servers))
	for server := range servers {
		names = append(names, server)
	}
	slices.Sort(names)
	for _, server := range names {
		if !mcpName.MatchString(server) {
			return fmt.Errorf("MCP server name %q", server)
		}
		for _, t := range servers[server] {
			if !mcpName.MatchString(t) {
				return fmt.Errorf("MCP server %q: tool name %q", server, t)
			}
			name := server + "_" + t
			if seen[name] {
				return fmt.Errorf("MCP tool name %q is ambiguous", name)
			}
			if len(name) > maxToolNameLen {
				return fmt.Errorf("MCP tool name %q is longer than %d", name, maxToolNameLen)
			}
			seen[name] = true
		}
	}
	return nil
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
