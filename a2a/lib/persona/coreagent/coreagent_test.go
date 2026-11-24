package coreagent_test

import (
	"reflect"
	"slices"
	"strings"
	"testing"

	"sigs.k8s.io/yaml"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/coreagent"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/render"
)

const (
	fleetAuditPersonaDir = "../../../../agents/fleet-audit/persona"
	clusterPersonaDir    = "../../../../agents/cluster/persona"
)

func load(t *testing.T, dir string) *persona.Persona {
	t.Helper()
	p, err := persona.Load(dir)
	if err != nil {
		t.Fatalf("load %s: %v", dir, err)
	}
	return p
}

// A shipped persona with no MCP servers renders whole: its instructions as
// the instructions file, every skill file under skills/ byte for byte, and
// nothing outside the rendering's names.
func TestAShippedPersonaRendersWhole(t *testing.T) {
	p := load(t, fleetAuditPersonaDir)
	out, err := coreagent.Adapter{}.Render(p, persona.Target{})
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if err := render.CheckOutput(out); err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for _, f := range out.Files {
		if !coreagent.Covers(f.Path) {
			t.Errorf("rendered %q, outside %v", f.Path, coreagent.Names)
		}
		got[f.Path] = string(f.Data)
	}
	if got[coreagent.InstructionsFile] != p.Instructions {
		t.Errorf("%s differs from the persona's instructions", coreagent.InstructionsFile)
	}
	want := 1
	for _, s := range p.Skills {
		for _, f := range s.Files {
			want++
			rendered := got["skills/"+s.Name+"/"+f.Path]
			if f.Path == "SKILL.md" {
				sameSkill(t, s.Name, f.Data, []byte(rendered))
				continue
			}
			if rendered != string(f.Data) {
				t.Errorf("skills/%s/%s differs from the persona's", s.Name, f.Path)
			}
		}
	}
	if len(out.Files) != want {
		t.Errorf("rendered %d files, want %d", len(out.Files), want)
	}
	if !slices.Equal(out.Allowed, p.Manifest.Tools) || out.Model != p.Manifest.Model {
		t.Errorf("tools %v model %q, want the manifest's %v %q", out.Allowed, out.Model, p.Manifest.Tools, p.Manifest.Model)
	}
}

// The cluster persona names an MCP server; core-agent has no MCP path yet,
// so it's refused by name rather than rendered without the server.
func TestAnMCPServerIsRefused(t *testing.T) {
	_, err := coreagent.Adapter{}.Render(load(t, clusterPersonaDir), persona.Target{})
	if err == nil || !strings.Contains(err.Error(), "developer_knowledge") || !strings.Contains(err.Error(), "mcp.yaml") {
		t.Fatalf("err = %v, want mcp.yaml's server named", err)
	}
}

// sameSkill checks a rendered SKILL.md against its source: the same
// frontmatter keys and values, and the same body.
func sameSkill(t *testing.T, skill string, src, rendered []byte) {
	t.Helper()
	split := func(b []byte) (map[string]any, string) {
		parts := strings.SplitN(string(b), "\n---\n", 2)
		var m map[string]any
		if err := yaml.Unmarshal([]byte(strings.TrimPrefix(parts[0], "---\n")), &m); err != nil || len(parts) != 2 {
			t.Fatalf("%s: %v", skill, err)
		}
		return m, parts[1]
	}
	sm, sb := split(src)
	rm, rb := split(rendered)
	if !reflect.DeepEqual(sm, rm) || sb != rb {
		t.Errorf("skills/%s/SKILL.md: rendered frontmatter or body differs from the source's", skill)
	}
}

func skillPersona(skill string, files ...persona.File) *persona.Persona {
	return &persona.Persona{Instructions: "be useful\n", Skills: []persona.Skill{{Name: skill, Files: files}}}
}

func TestWhatCoreAgentCantTakeIsRefused(t *testing.T) {
	for _, tc := range []struct {
		name string
		p    *persona.Persona
		want string
	}{
		{"allowed-tools", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\ndescription: d\nallowed-tools: [Bash]\n---\nbody\n")}), `"allowed-tools"`},
		{"unknown key", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\ndescription: d\nhooks: x\n---\nbody\n")}), `"hooks"`},
		{"no frontmatter", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("body\n")}), "no frontmatter"},
		// A line that only starts with --- doesn't end the frontmatter, so the
		// key after it is read, and refused, rather than hidden.
		{"a near-terminator hiding a key", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\ndescription: d\n---x\nallowed-tools: [Bash]\n---\nbody\n")}), "frontmatter"},
		{"a repeated key", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\ndescription: d\ndescription: e\n---\nbody\n")}), "frontmatter"},
		{"unterminated", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\n")}), "unterminated"},
		{"executable", skillPersona("s",
			persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\ndescription: d\n---\n")},
			persona.File{Path: "run.sh", Data: []byte("#!/bin/sh\n"), Executable: true}), "skills/s/run.sh"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := (coreagent.Adapter{}).Render(tc.p, persona.Target{}); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Errorf("err = %v, want it to name %s", err, tc.want)
			}
		})
	}
}

func TestEveryAgentSkillsKeyTheHarnessReadsPasses(t *testing.T) {
	src := []byte("---\nname: s\ndescription: d\nlicense: Apache-2.0\ncompatibility: any\nmetadata:\n  k: v\n---\nbody\n---\nmore body\n")
	out, err := (coreagent.Adapter{}).Render(skillPersona("s", persona.File{Path: "SKILL.md", Data: src}), persona.Target{})
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	// The rendered SKILL.md is the canonical form: the same keys, and the body
	// (a later --- line included) after one terminator.
	sameSkill(t, "s", src, out.Files[1].Data)
	if !strings.HasPrefix(string(out.Files[1].Data), "---\n") {
		t.Errorf("rendered SKILL.md = %q", out.Files[1].Data)
	}
}

func TestCovers(t *testing.T) {
	for rel, want := range map[string]bool{
		"instructions.md": true, "skills": true, "skills/a/SKILL.md": true,
		"CLAUDE.md": false, "skillsx/a": false, "AGENTS.md": false, ".agents/config.json": false, "": false,
	} {
		if got := coreagent.Covers(rel); got != want {
			t.Errorf("coreagent.Covers(%q) = %v, want %v", rel, got, want)
		}
	}
}

func TestRenderForPicksThisAdapter(t *testing.T) {
	a, err := render.For(persona.CoreAgent)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := a.(coreagent.Adapter); !ok {
		t.Fatalf("render.For(%q) = %T", persona.CoreAgent, a)
	}
}
