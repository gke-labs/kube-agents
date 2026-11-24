// Package coreagent renders a persona for the core-agent harness: the
// persona's instructions as one file and its skills as a skills directory in
// the Agent Skills layout, which the harness reads once at start (its
// --instructions-file and --skills-dir). The harness adds the instructions on
// top of its own system prompt and lists the skills to the model, loading a
// skill's instructions and files when the model uses it.
//
// What the harness can't take is refused, not dropped: an MCP server (the
// harness has no path to the credential broker's MCP relay yet), a skill
// frontmatter key outside the Agent Skills set the harness reads, a skill
// that grants tools, and an executable skill file (the harness reads a
// skill's files; it doesn't run them).
package coreagent

import (
	"fmt"
	"path"
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
func (Adapter) Render(p *persona.Persona, _ persona.Target) (persona.Output, error) {
	if len(p.MCP.Servers) > 0 {
		names := make([]string, 0, len(p.MCP.Servers))
		for name := range p.MCP.Servers {
			names = append(names, name)
		}
		slices.Sort(names)
		return persona.Output{}, fmt.Errorf("mcp.yaml: the core-agent harness has no MCP path yet, so server(s) %s can't be rendered", strings.Join(names, ", "))
	}
	out := persona.Output{Model: p.Manifest.Model, Allowed: slices.Clone(p.Manifest.Tools)}
	out.Files = append(out.Files, persona.File{Path: InstructionsFile, Data: []byte(p.Instructions)})
	for _, s := range p.Skills {
		for _, f := range s.Files {
			if f.Executable {
				return persona.Output{}, fmt.Errorf("skills/%s/%s: an executable skill file can't be rendered for core-agent, which reads a skill's files and doesn't run them", s.Name, f.Path)
			}
			if f.Path == "SKILL.md" {
				if err := checkFrontmatter(s.Name, f.Data); err != nil {
					return persona.Output{}, err
				}
			}
			out.Files = append(out.Files, persona.File{Path: path.Join(SkillsDir, s.Name, f.Path), Data: f.Data})
		}
	}
	return out, nil
}

// checkFrontmatter refuses a SKILL.md frontmatter key the harness doesn't
// read, and allowed-tools. The persona loader has already checked the
// frontmatter's shape; this is what core-agent takes of it.
func checkFrontmatter(skill string, data []byte) error {
	text := strings.ReplaceAll(string(data), "\r\n", "\n")
	rest, ok := strings.CutPrefix(text, "---\n")
	if !ok {
		return fmt.Errorf("skills/%s/SKILL.md: no frontmatter", skill)
	}
	front, _, ok := strings.Cut(rest, "\n---")
	if !ok {
		return fmt.Errorf("skills/%s/SKILL.md: unterminated frontmatter", skill)
	}
	var keys map[string]any
	if err := yaml.Unmarshal([]byte(front), &keys); err != nil {
		return fmt.Errorf("skills/%s/SKILL.md: frontmatter: %w", skill, err)
	}
	for key := range keys {
		if !slices.Contains(frontmatterKeys, key) {
			return fmt.Errorf("skills/%s/SKILL.md: frontmatter key %q can't be rendered for core-agent (it reads %s)", skill, key, strings.Join(frontmatterKeys, ", "))
		}
	}
	return nil
}
