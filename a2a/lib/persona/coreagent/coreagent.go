// Package coreagent renders a persona for the core-agent harness: the
// persona's instructions as one file and its skills as a skills directory in
// the Agent Skills layout, which the harness reads once at start (its
// --instructions-file and --skills-dir). The harness adds the instructions on
// top of its own system prompt and lists the skills to the model, loading a
// skill's instructions and files when the model uses it.
//
// Each MCP server the persona grants runs as the worker's relay to the
// credential broker, the same command and environment the Claude Code path
// gives it, with the granted tools listed explicitly.
//
// What the harness can't take is refused, not dropped: an MCP server with no
// relay to reach it, or whose granted tools can't be listed, or whose server
// or tool name the harness would rename; a skill frontmatter key outside the
// Agent Skills set the harness reads; a skill that grants tools; and an
// executable skill file when the persona doesn't grant the shell. The model
// runs a skill's scripts with the shell from the skill's directory on disk
// (the rendering's, which the harness can't write), which the harness gives
// it as the skill's base when the skill loads.
package coreagent

import (
	"fmt"
	"maps"
	"path"
	"regexp"
	"slices"
	"strings"

	"sigs.k8s.io/yaml"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

const (
	// InstructionsFile is the persona's instructions, which the harness adds
	// as a layer above its own system prompt.
	InstructionsFile = "instructions.md"
	// SkillsDir holds one directory per skill, each with its SKILL.md.
	SkillsDir = "skills"
)

// Names are the only top-level entries a rendering holds.
var Names = []string{InstructionsFile, SkillsDir}

// Covers reports whether a path relative to a rendering's root is part of
// it: one of Names, or anything under SkillsDir.
func Covers(rel string) bool {
	return slices.Contains(Names, rel) || strings.HasPrefix(rel, SkillsDir+"/")
}

// frontmatterKeys are the SKILL.md frontmatter keys the harness's skill
// loader reads and this adapter passes on. allowed-tools is one the loader
// reads but this adapter refuses: a skill doesn't grant tools.
var frontmatterKeys = []string{"name", "description", "license", "compatibility", "metadata"}

// Adapter renders for core-agent.
type Adapter struct{}

// Render returns the instructions file and each skill's files under
// SkillsDir. Tools are passed to the worker as the manifest names them, for
// the harness's tool registry to map; the model hint is passed on.
func (Adapter) Render(p *persona.Persona, t persona.Target) (persona.Output, error) {
	servers, err := mcpServers(p, t.Relay)
	if err != nil {
		return persona.Output{}, err
	}
	out := persona.Output{Model: p.Manifest.Model, Allowed: slices.Clone(p.Manifest.Tools), MCPServers: servers}
	out.Files = append(out.Files, persona.File{Path: InstructionsFile, Data: []byte(p.Instructions)})
	for _, s := range p.Skills {
		if !skillNameRE.MatchString(s.Name) || strings.Contains(s.Name, "--") {
			return persona.Output{}, fmt.Errorf("skills/%s: not a skill name core-agent takes (lowercase letters, digits and single '-')", s.Name)
		}
		for _, f := range s.Files {
			if !persona.LocalPath(f.Path) {
				return persona.Output{}, fmt.Errorf("skills/%s/%s: leaves the skill's directory", s.Name, f.Path)
			}
			if f.Executable && !scriptPathRE.MatchString(f.Path) {
				return persona.Output{}, fmt.Errorf("skills/%s/%s: a script's path may hold only letters, digits, '.', '_', '-' and '/', since the shell runs it by that path", s.Name, f.Path)
			}
			if f.Executable && !slices.Contains(p.Manifest.Tools, shellTool) {
				return persona.Output{}, fmt.Errorf("skills/%s/%s: an executable skill file needs the %s tool, which the persona doesn't grant", s.Name, f.Path, shellTool)
			}
			data := f.Data
			if f.Path == "SKILL.md" {
				canonical, err := canonicalSkill(s.Name, f.Data)
				if err != nil {
					return persona.Output{}, err
				}
				data = canonical
			}
			out.Files = append(out.Files, persona.File{Path: path.Join(SkillsDir, s.Name, f.Path), Data: data, Executable: f.Executable})
		}
	}
	return out, nil
}

// canonicalSkill rewrites a SKILL.md as the harness is to read it: its
// frontmatter, which ends at the first line that is exactly "---", decoded
// strictly (a repeated key is refused) and written back with only the keys
// the harness reads, then the body unchanged. A key outside those, and
// allowed-tools, is refused. Writing the frontmatter back rather than
// passing the original bytes leaves no line the harness's own parser could
// read differently.
func canonicalSkill(skill string, data []byte) ([]byte, error) {
	text := strings.ReplaceAll(string(data), "\r\n", "\n")
	if strings.Contains(text, "\ufeff") || strings.ContainsAny(text, "\r\u0085\u2028\u2029") {
		return nil, fmt.Errorf("skills/%s/SKILL.md: a byte-order mark or a line break other than LF or CRLF", skill)
	}
	lines := strings.SplitAfter(text, "\n")
	if len(lines) == 0 || strings.TrimRight(lines[0], "\n") != "---" {
		return nil, fmt.Errorf("skills/%s/SKILL.md: no frontmatter", skill)
	}
	end := -1
	for i := 1; i < len(lines) && end < 0; i++ {
		line := strings.TrimRight(lines[i], "\n")
		switch {
		case line == "---":
			end = i
		case strings.Contains(line, "---"), strings.HasPrefix(line, "..."), strings.HasPrefix(line, "%"):
			return nil, fmt.Errorf("skills/%s/SKILL.md: frontmatter line %q is an ambiguous YAML delimiter or directive", skill, line)
		}
	}
	if end < 0 {
		return nil, fmt.Errorf("skills/%s/SKILL.md: unterminated frontmatter", skill)
	}
	var keys map[string]any
	if err := yaml.UnmarshalStrict([]byte(strings.Join(lines[1:end], "")), &keys); err != nil {
		return nil, fmt.Errorf("skills/%s/SKILL.md: frontmatter: %w", skill, err)
	}
	for _, key := range slices.Sorted(maps.Keys(keys)) {
		if !slices.Contains(frontmatterKeys, key) {
			return nil, fmt.Errorf("skills/%s/SKILL.md: frontmatter key %q can't be rendered for core-agent (it reads %s)", skill, key, strings.Join(frontmatterKeys, ", "))
		}
	}
	// The harness's skill loader reads metadata as a map of strings and
	// refuses the whole skill set over one that isn't.
	if m, ok := keys["metadata"]; ok {
		values, isMap := m.(map[string]any)
		if !isMap {
			return nil, fmt.Errorf("skills/%s/SKILL.md: metadata must be a map", skill)
		}
		for _, k := range slices.Sorted(maps.Keys(values)) {
			if _, isString := values[k].(string); !isString {
				return nil, fmt.Errorf("skills/%s/SKILL.md: metadata.%s must be a string for core-agent", skill, k)
			}
		}
	}
	front, err := yaml.Marshal(keys)
	if err != nil {
		return nil, fmt.Errorf("skills/%s/SKILL.md: frontmatter: %w", skill, err)
	}
	return []byte("---\n" + string(front) + "---\n" + strings.Join(lines[end+1:], "")), nil
}

// scriptPathRE is a script path the shell's allow rule and the model can
// both take literally: no space, quote, separator or newline.
var scriptPathRE = regexp.MustCompile(`^[A-Za-z0-9._/-]+$`)

// shellTool is the persona built-in a skill's scripts need: the model runs
// them with the shell, from the skill's directory on disk.
const shellTool = "shell"

// toolNameRE is a server or tool name the harness registers as given: it
// names an MCP tool <server>_<tool> after replacing anything outside these
// characters in the server name, so a name outside them would change.
var toolNameRE = regexp.MustCompile(`^[A-Za-z0-9_]{1,64}$`)

// skillNameRE is the Agent Skills name rule, as the persona loader applies
// it; the harness's loader also refuses a name with "--".
var skillNameRE = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]{0,62}[a-z0-9])?$`)

// urlPlaceholder is what the relay's args carry where the server's address
// goes, as for Claude Code.
const urlPlaceholder = "{url}"

// mcpServers maps each server the manifest grants to the worker's relay,
// (The <server>_<tool> names the harness registers are checked for
// collisions with each other and with its built-in tools where the tool
// grant is rendered, toolgrant, so the rule lives in one place.)
//
// with the granted tools listed: a grant of "server.tool" names one tool,
// and a grant of the whole server takes its include list. A server nothing
// grants isn't started.
func mcpServers(p *persona.Persona, relay persona.Relay) ([]persona.MCPServer, error) {
	var out []persona.MCPServer
	for _, name := range p.SortedServers() {
		s := p.MCP.Servers[name]
		tools, whole := grantedTools(p.Manifest.Tools, name)
		if !whole && len(tools) == 0 {
			continue
		}
		field := "mcp.yaml: servers." + name
		if whole {
			if len(s.Include) == 0 {
				return nil, fmt.Errorf("%s: granted whole with no include list, so its tools can't be listed for core-agent", field)
			}
			tools = slices.Clone(s.Include)
		}
		if relay.URL == "" || relay.Command == "" {
			return nil, fmt.Errorf("%s: the worker gave no broker MCP relay to reach it", field)
		}
		if !slices.ContainsFunc(relay.Args, func(a string) bool { return strings.Contains(a, urlPlaceholder) }) {
			return nil, fmt.Errorf("%s: the relay's args don't carry %s, so the server wouldn't know its target", field, urlPlaceholder)
		}
		if !toolNameRE.MatchString(name) {
			return nil, fmt.Errorf("%s: core-agent would rename server %q; use letters, digits and '_'", field, name)
		}
		for _, tool := range tools {
			if !toolNameRE.MatchString(tool) {
				return nil, fmt.Errorf("%s: core-agent would rename tool %q; use letters, digits and '_'", field, tool)
			}
		}
		target := strings.TrimRight(relay.URL, "/") + "/" + s.Broker
		args := make([]string, len(relay.Args))
		for i, a := range relay.Args {
			args[i] = strings.ReplaceAll(a, urlPlaceholder, target)
		}
		out = append(out, persona.MCPServer{Name: name, Command: relay.Command, Args: args, Env: maps.Clone(relay.Env), Tools: tools})
	}
	return out, nil
}

// grantedTools is what the manifest's tools grant on server: the tools it
// names as "server.tool", and whether it grants the whole server.
func grantedTools(grants []string, server string) ([]string, bool) {
	var tools []string
	whole := false
	for _, g := range grants {
		s, tool, scoped := strings.Cut(g, ".")
		switch {
		case s != server:
		case !scoped:
			whole = true
		case !slices.Contains(tools, tool):
			tools = append(tools, tool)
		}
	}
	return tools, whole
}

// IsScriptPath reports whether a path relative to a skill's directory is one
// a script may have.
func IsScriptPath(rel string) bool { return scriptPathRE.MatchString(rel) }

// ScriptPaths are the absolute paths, under personaDir, of a rendering's
// executable skill files: the only commands the harness's shell may run from
// the persona, each directly by its path. A path outside personaDir's skills
// directory, or one that isn't clean, is refused.
func ScriptPaths(out persona.Output, personaDir string) ([]string, error) {
	var paths []string
	for _, f := range out.Files {
		if !f.Executable {
			continue
		}
		if path.Clean(f.Path) != f.Path || !strings.HasPrefix(f.Path, SkillsDir+"/") || strings.Count(f.Path, "/") < 2 || !scriptPathRE.MatchString(f.Path) {
			return nil, fmt.Errorf("%s: an executable outside a skill's directory", f.Path)
		}
		paths = append(paths, path.Join(personaDir, f.Path))
	}
	return paths, nil
}
