// Package claudecode renders a persona for Claude Code, as the profile
// worker runs it: headless, with its settings read from the user source only
// (--setting-sources user) and its MCP servers from --mcp-config only
// (--strict-mcp-config). The worker points CLAUDE_CONFIG_DIR at a per-task
// directory and writes Output.Files there, so the persona's CLAUDE.md and
// skills load under the user source, and nothing a task edits outlives it.
//
// The flags spell what a2a/cmd/worker-adapter passes today: the
// --allowedTools list, an --mcp-config whose server map is keyed
// mcpServers (each server a stdio proxy to the broker's relay), and both
// hardening flags on every argv.
package claudecode

import (
	"encoding/json"
	"fmt"
	"path"
	"slices"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

const (
	// MemoryFile is the user-level memory file Claude Code reads from
	// $CLAUDE_CONFIG_DIR on every run.
	MemoryFile = "CLAUDE.md"
	// SkillsDir is the user-level skills directory under the same root.
	SkillsDir = "skills"

	AllowedToolsFlag    = "--allowedTools"
	DisallowedToolsFlag = "--disallowedTools"
	// skillTool is the tool Claude Code loads a skill with.
	skillTool           = "Skill"
	MCPConfigFlag       = "--mcp-config"
	StrictMCPConfigFlag = "--strict-mcp-config"
	SettingSourcesFlag  = "--setting-sources"
	// UserSettingSource is the only settings source the worker reads: the
	// project and local sources live under the task's cwd, which the agent
	// can write.
	UserSettingSource = "user"

	mcpServersKey  = "mcpServers"
	urlPlaceholder = "{url}"
)

// builtinTools maps the neutral built-in names to Claude Code's tools.
var builtinTools = map[string][]string{
	persona.ToolReadFile:  {"Read", "Glob", "Grep"},
	persona.ToolWebSearch: {"WebSearch"},
	persona.ToolShell:     {"Bash", "BashOutput", "KillShell"},
}

// claudeBuiltins are the Claude Code built-in tools that read, write, run or
// reach out. Every one the persona does not grant through builtinTools goes on
// --disallowedTools: --allowedTools only pre-approves, and Claude Code runs
// some tools (Read, Glob, Grep) without asking. A new Claude Code built-in of
// that kind must be added here, or a persona that never granted it gets it.
var claudeBuiltins = []string{
	"Bash", "BashOutput", "KillShell",
	"Edit", "MultiEdit", "Write", "NotebookEdit",
	"Read", "Glob", "Grep", "LS", "NotebookRead",
	"WebFetch", "WebSearch",
	"Task",
}

// Adapter is the Claude Code persona.Adapter.
type Adapter struct{}

var _ persona.Adapter = Adapter{}

// Render maps p for Claude Code. It never fails on something Claude Code
// cannot express; that is a warning.
func (Adapter) Render(p *persona.Persona, t persona.Target) (persona.Output, error) {
	// A relay whose args never name the server would start every broker
	// server as the same proxy with no target. That is the worker's mistake,
	// not something Claude Code cannot express, so it is an error.
	if t.Relay.URL != "" && !slices.ContainsFunc(t.Relay.Args, func(a string) bool {
		return strings.Contains(a, urlPlaceholder)
	}) {
		return persona.Output{}, fmt.Errorf("the relay's args do not carry %s, so no server would know its target", urlPlaceholder)
	}
	out := persona.Output{Model: p.Manifest.Model}
	warn := func(format string, args ...any) { out.Warnings = append(out.Warnings, fmt.Sprintf(format, args...)) }

	out.Files = append(out.Files, persona.File{Path: MemoryFile, Data: []byte(p.Instructions)})
	shell := slices.Contains(p.Manifest.Tools, persona.ToolShell)
	for _, s := range p.Skills {
		for _, f := range s.Files {
			// Without a shell a skill's scripts are files the model can
			// see and not run, which invites it to go looking for a way
			// to; only its prose ships (session-persona's rule).
			if !shell && !persona.IsMarkdown(f.Path) {
				warn("skills/%s/%s: dropped, the persona grants no %s tool to run it", s.Name, f.Path, persona.ToolShell)
				continue
			}
			out.Files = append(out.Files, persona.File{
				Path: path.Join(SkillsDir, s.Name, f.Path), Data: f.Data, Executable: f.Executable,
			})
		}
	}

	servers, dropped := mcpServers(p, t.Relay)
	for _, name := range dropped {
		warn("mcp.yaml: servers.%s: dropped, the worker gave no broker MCP relay", name)
	}
	allowed := allowedTools(p, dropped, warn)
	// Claude Code loads a skill through its Skill tool, which headless mode
	// refuses unless it is on the allowlist; a persona's own skills are
	// what it is for.
	if len(p.Skills) > 0 {
		allowed = append(allowed, skillTool)
	}
	// A server no tools entry grants is not configured at all. The
	// allowlist only pre-approves; it does not fence what is off it, so not
	// starting the server is what withholds its tools.
	for _, name := range p.SortedServers() {
		if _, ok := servers[name]; ok && !granted(p.Manifest.Tools, name) {
			delete(servers, name)
			warn("mcp.yaml: servers.%s: dropped, no tools entry grants any of its tools", name)
		}
	}

	// No grant, no flag: an empty --allowedTools is not "nothing".
	if len(allowed) > 0 {
		out.Args = append(out.Args, AllowedToolsFlag, strings.Join(allowed, ","))
	}
	out.Allowed, out.Disallowed = allowed, disallowedTools(allowed)
	out.Args = append(out.Args, DisallowedToolsFlag, strings.Join(out.Disallowed, ","))
	if len(servers) > 0 {
		cfg, err := json.Marshal(map[string]any{mcpServersKey: servers})
		if err != nil {
			return persona.Output{}, err
		}
		out.Args = append(out.Args, MCPConfigFlag, string(cfg))
	}
	// Both on every argv, servers or not: without --strict-mcp-config the
	// harness would load an MCP config an earlier task left in the cwd, and
	// without --setting-sources user it would read settings (hooks,
	// permissions) from the cwd's .claude/, which the agent can write.
	out.Args = append(out.Args, StrictMCPConfigFlag, SettingSourcesFlag, UserSettingSource)

	for _, key := range sortedKeys(p.Manifest.Harness[string(persona.ClaudeCode)]) {
		warn("persona.yaml: harness.%s.%s: not supported by this adapter, ignored", persona.ClaudeCode, key)
	}
	return out, nil
}

// mcpServers is the --mcp-config server map, and the broker servers it had
// to drop for want of a relay.
func mcpServers(p *persona.Persona, relay persona.Relay) (map[string]any, []string) {
	servers := map[string]any{}
	var dropped []string
	for _, name := range p.SortedServers() {
		s := p.MCP.Servers[name]
		if relay.URL == "" || relay.Command == "" {
			dropped = append(dropped, name)
			continue
		}
		target := strings.TrimRight(relay.URL, "/") + "/" + s.Broker
		args := make([]string, len(relay.Args))
		for i, a := range relay.Args {
			args[i] = strings.ReplaceAll(a, urlPlaceholder, target)
		}
		entry := map[string]any{"command": relay.Command, "args": args}
		if len(relay.Env) > 0 {
			entry["env"] = relay.Env
		}
		servers[name] = entry
	}
	return servers, dropped
}

// allowedTools maps the manifest's tools list to --allowedTools entries, in
// list order.
func allowedTools(p *persona.Persona, dropped []string, warn func(string, ...any)) []string {
	var allowed []string
	for _, t := range p.Manifest.Tools {
		if mapped, ok := builtinTools[t]; ok {
			allowed = append(allowed, mapped...)
			continue
		}
		server, tool, scoped := strings.Cut(t, ".")
		if slices.Contains(dropped, server) {
			warn("persona.yaml: tools: %q dropped with its server", t)
			continue
		}
		switch include := p.MCP.Servers[server].Include; {
		case scoped:
			allowed = append(allowed, mcpTool(server, tool))
		case len(include) > 0:
			// The harness has no per-server tool filter, so the
			// include list becomes the grant.
			for _, it := range include {
				allowed = append(allowed, mcpTool(server, it))
			}
		default:
			allowed = append(allowed, "mcp__"+server)
		}
	}
	return dedupe(allowed)
}

// disallowedTools is every claudeBuiltins entry allowed does not name.
func disallowedTools(allowed []string) []string {
	var deny []string
	for _, t := range claudeBuiltins {
		if !slices.Contains(allowed, t) {
			deny = append(deny, t)
		}
	}
	return deny
}

// granted reports whether tools grants anything on server.
func granted(tools []string, server string) bool {
	return slices.ContainsFunc(tools, func(t string) bool {
		s, _, _ := strings.Cut(t, ".")
		return s == server
	})
}

func mcpTool(server, tool string) string { return "mcp__" + server + "__" + tool }

func dedupe(in []string) []string {
	var out []string
	for _, s := range in {
		if !slices.Contains(out, s) {
			out = append(out, s)
		}
	}
	return out
}

func sortedKeys(m map[string]any) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}
