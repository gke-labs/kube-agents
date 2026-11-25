package coreagent_test

import (
	"encoding/json"
	"maps"
	"reflect"
	"slices"
	"strings"
	"testing"

	"sigs.k8s.io/yaml"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/claudecode"
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

var relay = persona.Relay{
	URL: "http://broker:8080/v1/mcp", Command: "/worker-adapter", Args: []string{"mcp-relay", "{url}"},
	Env: map[string]string{"CREDENTIAL_PROXY_URL": "http://broker:8080", "CREDENTIAL_PROXY_TOKEN_FILE": "/var/run/token"},
}

// The cluster persona's MCP server runs as the worker's relay to the broker,
// the same command, target and environment the Claude Code path gives it,
// with the include list as its explicit tool allowlist.
func TestAnMCPServerRunsAsTheBrokerRelay(t *testing.T) {
	p := load(t, clusterPersonaDir)
	out, err := coreagent.Adapter{}.Render(p, persona.Target{Relay: relay})
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if len(out.MCPServers) != 1 {
		t.Fatalf("servers = %+v, want one", out.MCPServers)
	}
	got := out.MCPServers[0]
	want := persona.MCPServer{
		Name: "developer_knowledge", Command: relay.Command,
		Args:  []string{"mcp-relay", "http://broker:8080/v1/mcp/developer_knowledge"},
		Env:   relay.Env,
		Tools: p.MCP.Servers["developer_knowledge"].Include,
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("server = %+v\nwant %+v", got, want)
	}
	// The same entry the Claude Code adapter puts in --mcp-config.
	claude, err := claudecode.Adapter{}.Render(p, persona.Target{Relay: relay})
	if err != nil {
		t.Fatal(err)
	}
	cfg := claude.Args[slices.Index(claude.Args, claudecode.MCPConfigFlag)+1]
	var parsed struct {
		MCPServers map[string]struct {
			Command string            `json:"command"`
			Args    []string          `json:"args"`
			Env     map[string]string `json:"env"`
		} `json:"mcpServers"`
	}
	if err := json.Unmarshal([]byte(cfg), &parsed); err != nil {
		t.Fatal(err)
	}
	c := parsed.MCPServers["developer_knowledge"]
	if c.Command != got.Command || !slices.Equal(c.Args, got.Args) || !maps.Equal(c.Env, got.Env) {
		t.Errorf("core-agent's server %+v differs from Claude Code's %+v", got, c)
	}
}

func mcpPersona(tools []string, servers map[string]persona.Server) *persona.Persona {
	return &persona.Persona{Instructions: "x\n", Manifest: persona.Manifest{Tools: tools}, MCP: persona.MCP{Servers: servers}}
}

func TestMCPServersCoreAgentCantTakeAreRefused(t *testing.T) {
	for _, tc := range []struct {
		name  string
		p     *persona.Persona
		relay persona.Relay
		want  string
	}{
		{"no relay", load(t, clusterPersonaDir), persona.Relay{}, "no broker MCP relay"},
		{"relay args without the target", load(t, clusterPersonaDir), persona.Relay{URL: relay.URL, Command: relay.Command, Args: []string{"mcp-relay"}}, "{url}"},
		{"granted whole with no include", mcpPersona([]string{"docs"}, map[string]persona.Server{"docs": {Broker: "developer_knowledge"}}), relay, "no include list"},
		{"a name the harness would rename", mcpPersona([]string{"dev-docs"}, map[string]persona.Server{"dev-docs": {Broker: "developer_knowledge", Include: []string{"search"}}}), relay, "rename server"},
		{"a tool the harness would rename", mcpPersona([]string{"docs.search-all"}, map[string]persona.Server{"docs": {Broker: "developer_knowledge"}}), relay, "rename tool"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := (coreagent.Adapter{}).Render(tc.p, persona.Target{Relay: tc.relay}); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Errorf("err = %v, want %q", err, tc.want)
			}
		})
	}
}

// A grant of one tool lists just that tool; a server nothing grants isn't
// started, relay or not.
func TestOnlyGrantedMCPToolsAreListed(t *testing.T) {
	p := mcpPersona([]string{"docs.search"}, map[string]persona.Server{
		"docs":   {Broker: "developer_knowledge", Include: []string{"search", "fetch"}},
		"unused": {Broker: "observability"},
	})
	out, err := coreagent.Adapter{}.Render(p, persona.Target{Relay: relay})
	if err != nil {
		t.Fatal(err)
	}
	if len(out.MCPServers) != 1 || out.MCPServers[0].Name != "docs" || !slices.Equal(out.MCPServers[0].Tools, []string{"search"}) {
		t.Errorf("servers = %+v, want docs with search only", out.MCPServers)
	}
	none, err := coreagent.Adapter{}.Render(mcpPersona(nil, p.MCP.Servers), persona.Target{})
	if err != nil || len(none.MCPServers) != 0 {
		t.Errorf("ungranted servers: %+v, %v", none.MCPServers, err)
	}
}

// With the shell granted, a skill's script renders executable, for the init
// to write 0555 where the model runs it from.
func TestAScriptRendersExecutableWithTheShell(t *testing.T) {
	p := skillPersona("s",
		persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\ndescription: d\n---\n")},
		persona.File{Path: "scripts/run.sh", Data: []byte("#!/bin/sh\n"), Executable: true})
	p.Manifest.Tools = []string{"shell"}
	out, err := coreagent.Adapter{}.Render(p, persona.Target{})
	if err != nil {
		t.Fatal(err)
	}
	i := slices.IndexFunc(out.Files, func(f persona.File) bool { return f.Path == "skills/s/scripts/run.sh" })
	if i < 0 || !out.Files[i].Executable {
		t.Errorf("files = %+v, want skills/s/scripts/run.sh executable", out.Files)
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
		{"a YAML document end", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\ndescription: d\n...\nallowed-tools: [Bash]\n---\n")}), "ambiguous"},
		{"a YAML directive", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\n%YAML 1.2\nname: s\ndescription: d\n---\n")}), "ambiguous"},
		{"a closing --- with trailing space", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\ndescription: d\n--- \n---\n")}), "ambiguous"},
		{"a byte-order mark", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("\ufeff---\nname: s\ndescription: d\n---\n")}), "byte-order mark"},
		{"a next-line character", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\u0085...\u0085allowed-tools: [Bash]\ndescription: d\n---\n")}), "line break"},
		{"--- inside a frontmatter line", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\ndescription: a---b\n---\n")}), "ambiguous"},
		{"a repeated key", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\ndescription: d\ndescription: e\n---\nbody\n")}), "frontmatter"},
		{"unterminated", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\n")}), "unterminated"},
		{"a merge key smuggling allowed-tools", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\ndescription: d\n<<: {allowed-tools: [Bash]}\n---\n")}), "allowed-tools"},
		{"non-string metadata", skillPersona("s", persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\ndescription: d\nmetadata: {k: {n: 1}}\n---\n")}), "metadata.k"},
		{"a name with --", skillPersona("a--b", persona.File{Path: "SKILL.md", Data: []byte("---\nname: a--b\ndescription: d\n---\n")}), "skills/a--b"},
		{"a skill named ..", skillPersona("..", persona.File{Path: "instructions.md", Data: []byte("x")}), "skills/.."},
		{"a file path climbing out", skillPersona("s", persona.File{Path: "../../instructions.md", Data: []byte("x")}), "leaves"},
		{"a script with a space", shellSkill("s", "scripts/run me.sh"), "may hold only"},
		{"a script with a separator", shellSkill("s", "scripts/run;id"), "may hold only"},
		{"a script with a newline", shellSkill("s", "scripts/run\n- /etc/x"), "may hold only"},
		{"a script without the shell", skillPersona("s",
			persona.File{Path: "SKILL.md", Data: []byte("---\nname: s\ndescription: d\n---\n")},
			persona.File{Path: "run.sh", Data: []byte("#!/bin/sh\n"), Executable: true}), "needs the shell tool"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := (coreagent.Adapter{}).Render(tc.p, persona.Target{}); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Errorf("err = %v, want it to name %s", err, tc.want)
			}
		})
	}
}

// shellSkill is a persona granting the shell whose skill has one script at rel.
func shellSkill(skill, rel string) *persona.Persona {
	p := skillPersona(skill,
		persona.File{Path: "SKILL.md", Data: []byte("---\nname: " + skill + "\ndescription: d\n---\n")},
		persona.File{Path: rel, Data: []byte("#!/bin/sh\n"), Executable: true})
	p.Manifest.Tools = []string{"shell"}
	return p
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
