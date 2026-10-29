package persona

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const exampleDir = "testdata/example"

// CopyExample copies the example persona into a temp dir for a test to edit.
func copyExample(t *testing.T) string {
	t.Helper()
	dst := t.TempDir()
	err := filepath.WalkDir(exampleDir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(exampleDir, p)
		target := filepath.Join(dst, rel)
		if d.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		data, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		return os.WriteFile(target, data, info.Mode().Perm())
	})
	if err != nil {
		t.Fatal(err)
	}
	return dst
}

func replaceIn(t *testing.T, dir, file, old, new string) {
	t.Helper()
	p := filepath.Join(dir, file)
	data, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), old) {
		t.Fatalf("%s does not contain %q", file, old)
	}
	if err := os.WriteFile(p, []byte(strings.Replace(string(data), old, new, 1)), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestLoadTheExample(t *testing.T) {
	p, err := Load(exampleDir)
	if err != nil {
		t.Fatal(err)
	}
	if p.Manifest.Name != "example" || p.Manifest.Parameters[0].Name != "cluster" {
		t.Errorf("manifest = %+v", p.Manifest)
	}
	if len(p.Skills) != 2 || p.Skills[0].Name != "notes" || p.Skills[1].Name != "triage" {
		t.Fatalf("skills = %+v, want notes then triage", p.Skills)
	}
	var paths []string
	for _, f := range p.Skills[1].Files {
		paths = append(paths, f.Path)
		if f.Path == "scripts/events.sh" && !f.Executable {
			t.Errorf("events.sh lost its executable bit")
		}
	}
	if strings.Join(paths, ",") != "SKILL.md,scripts/events.sh" {
		t.Errorf("triage files = %v", paths)
	}
	if p.MCP.Servers["gke"].Broker != "gke" || p.MCP.Servers["docs"].Broker != "developer_knowledge" {
		t.Errorf("mcp = %+v", p.MCP)
	}
}

// Each case breaks one thing in a copy of the example and wants Load to
// name it, by file and the words of the problem.
func TestLoadRefuses(t *testing.T) {
	cases := []struct {
		name string
		edit func(t *testing.T, dir string)
		want string
	}{
		{"bad apiVersion", func(t *testing.T, d string) {
			replaceIn(t, d, ManifestFile, "v1alpha1", "v9")
		}, "persona.yaml: apiVersion: is \"persona.kube-agents.x-k8s.io/v9\""},
		{"unknown manifest field", func(t *testing.T, d string) {
			replaceIn(t, d, ManifestFile, "model:", "image: registry.example/x:1\nmodel:")
		}, "unknown field \"image\""},
		{"a tier", func(t *testing.T, d string) {
			replaceIn(t, d, ManifestFile, "model:", "tier: read\nmodel:")
		}, "persona.yaml: tier: is not a persona field"},
		{"an empty tier", func(t *testing.T, d string) {
			replaceIn(t, d, ManifestFile, "model:", "tier:\nmodel:")
		}, "persona.yaml: tier: is not a persona field"},
		{"missing instructions", func(t *testing.T, d string) {
			_ = os.Remove(filepath.Join(d, InstructionsFile))
		}, "instructions.md: is missing"},
		{"empty instructions", func(t *testing.T, d string) {
			_ = os.WriteFile(filepath.Join(d, InstructionsFile), []byte("\n  \n"), 0o644)
		}, "instructions.md: is empty"},
		{"skill name mismatch", func(t *testing.T, d string) {
			replaceIn(t, d, "skills/triage/SKILL.md", "name: triage", "name: triage-v2")
		}, "skills/triage/SKILL.md: name: is \"triage-v2\"; it must be the directory name"},
		{"long skill description", func(t *testing.T, d string) {
			replaceIn(t, d, "skills/notes/SKILL.md", "Write findings up.", strings.Repeat("x", 1025))
		}, "skills/notes/SKILL.md: description: is 1025 bytes"},
		{"skill without SKILL.md", func(t *testing.T, d string) {
			_ = os.Remove(filepath.Join(d, "skills/notes/SKILL.md"))
		}, "skills/notes: has no SKILL.md"},
		{"a direct url", func(t *testing.T, d string) {
			replaceIn(t, d, MCPFile, "broker: developer_knowledge", "url: https://docs.example.com/mcp")
		}, "mcp.yaml: servers.docs.url: direct URLs are not supported; a server names a broker MCP server"},
		{"a url beside a broker", func(t *testing.T, d string) {
			replaceIn(t, d, MCPFile, "broker: gke", "broker: gke\n    url: https://x.example.com")
		}, "servers.gke.url: direct URLs are not supported"},
		{"no broker", func(t *testing.T, d string) {
			replaceIn(t, d, MCPFile, "broker: observability", "include: [x]")
		}, "servers.unused.broker: is required"},
		{"headers in a server", func(t *testing.T, d string) {
			replaceIn(t, d, MCPFile, "broker: gke", "broker: gke\n    headers: {Authorization: x}")
		}, "unknown field \"headers\""},
		{"a server named like a built-in", func(t *testing.T, d string) {
			replaceIn(t, d, MCPFile, "servers:", "servers:\n  shell:\n    broker: gke")
		}, "mcp.yaml: servers.shell: server name is a built-in tool name"},
		{"a backslash in a skill file name", func(t *testing.T, d string) {
			_ = os.WriteFile(filepath.Join(d, `skills/notes/a\b.md`), []byte("x\n"), 0o644)
		}, `skills/notes/a\b.md: is not a plain relative path`},
		{"a symlinked README", func(t *testing.T, d string) {
			_ = os.Remove(filepath.Join(d, ReadmeFile))
			_ = os.Symlink("/etc/hosts", filepath.Join(d, ReadmeFile))
		}, "README.md: is not a regular file"},
		{"allowed-tools in a skill", func(t *testing.T, d string) {
			replaceIn(t, d, "skills/triage/SKILL.md", "description:", "allowed-tools: Bash\ndescription:")
		}, "skills/triage/SKILL.md: allowed-tools: is not an allowed frontmatter key"},
		{"hooks in a skill", func(t *testing.T, d string) {
			replaceIn(t, d, "skills/triage/SKILL.md", "description:", "hooks:\n  PreToolUse: []\ndescription:")
		}, "skills/triage/SKILL.md: hooks: is not an allowed frontmatter key"},
		{"metadata not a map", func(t *testing.T, d string) {
			replaceIn(t, d, "skills/triage/SKILL.md", "description:", "metadata: x\ndescription:")
		}, "skills/triage/SKILL.md: metadata: must be a map"},
		{"absolute import", func(t *testing.T, d string) {
			replaceIn(t, d, InstructionsFile, "and report", "and report. See @/var/run/secrets/token")
		}, "instructions.md: line 3: the import \"@/var/run/secrets/token\" could read a file outside the persona"},
		{"home import", func(t *testing.T, d string) {
			replaceIn(t, d, InstructionsFile, "# Example", "@~/.claude/settings.json\n# Example")
		}, "instructions.md: line 1: the import \"@~/.claude/settings.json\""},
		{"climbing import in a skill", func(t *testing.T, d string) {
			replaceIn(t, d, "skills/notes/SKILL.md", "Keep it short.", "Keep it short. @./../../../etc/hosts")
		}, "skills/notes/SKILL.md: line 7: the import \"@./../../../etc/hosts\""},
		{"climbing import in an upper-case .MD", func(t *testing.T, d string) {
			_ = os.WriteFile(filepath.Join(d, "skills/notes/EXTRA.MD"), []byte("see @../../../x\n"), 0o644)
		}, "skills/notes/EXTRA.MD: line 1: the import \"@../../../x\""},
		{"unknown top-level key in mcp.yaml", func(t *testing.T, d string) {
			replaceIn(t, d, MCPFile, "servers:", "mcpServers: {}\nservers:")
		}, "unknown field \"mcpServers\""},
		{"repeated key in mcp.yaml", func(t *testing.T, d string) {
			replaceIn(t, d, MCPFile, "broker: gke", "broker: gke\n    broker: gke2")
		}, `key "broker" already set in map`},
		{"repeated server in mcp.yaml", func(t *testing.T, d string) {
			replaceIn(t, d, MCPFile, "  unused:", "  gke:\n    broker: x\n  unused:")
		}, `key "gke" already set in map`},
		{"server name case", func(t *testing.T, d string) {
			replaceIn(t, d, MCPFile, "  unused:", "  Unused:")
		}, "servers.Unused: server name is not lowercase"},
		{"double underscore", func(t *testing.T, d string) {
			replaceIn(t, d, MCPFile, "  unused:", "  un__used:")
		}, "servers.un__used: server name must not contain \"__\""},
		{"persona name", func(t *testing.T, d string) {
			replaceIn(t, d, ManifestFile, "name: example", "name: Example")
		}, "persona.yaml: name: \"Example\" is not a DNS label"},
		{"include repeats", func(t *testing.T, d string) {
			replaceIn(t, d, MCPFile, "[get_cluster, list_clusters]", "[get_cluster, get_cluster]")
		}, "servers.gke.include[1]: \"get_cluster\" repeats"},
		{"include tool name", func(t *testing.T, d string) {
			replaceIn(t, d, MCPFile, "[get_cluster, list_clusters]", "[get_cluster, \"rm -rf\"]")
		}, "servers.gke.include[1]: \"rm -rf\" is not an MCP tool name"},
		{"command path", func(t *testing.T, d string) {
			replaceIn(t, d, ManifestFile, "[kubectl]", "[/usr/bin/kubectl]")
		}, "requires.commands[0]: \"/usr/bin/kubectl\" is not a command name"},
		{"harness name", func(t *testing.T, d string) {
			replaceIn(t, d, ManifestFile, "  antigravity:", "  Claude_Code:")
		}, "persona.yaml: harness.Claude_Code: is not a harness name"},
		{"file over the cap", func(t *testing.T, d string) {
			_ = os.WriteFile(filepath.Join(d, "skills/notes/big.md"), make([]byte, maxFileBytes+1), 0o644)
		}, "skills/notes/big.md: is 1048577 bytes; the limit is 1048576"},
		{"persona over the total cap", func(t *testing.T, d string) {
			for i := 0; i <= maxTotalBytes/maxFileBytes; i++ {
				_ = os.WriteFile(filepath.Join(d, "skills/notes", fmt.Sprintf("part%02d.md", i)), make([]byte, maxFileBytes), 0o644)
			}
		}, "the persona passes 16777216 bytes in total"},
		{"file in skills/", func(t *testing.T, d string) {
			_ = os.WriteFile(filepath.Join(d, "skills/stray.md"), []byte("x"), 0o644)
		}, "skills/stray.md: a skill is a directory holding SKILL.md"},
		{"symlinked skill dir", func(t *testing.T, d string) {
			_ = os.Symlink(filepath.Join(d, "skills/notes"), filepath.Join(d, "skills/linked"))
		}, "skills/linked: is a symlink"},
		{"tool on no server", func(t *testing.T, d string) {
			replaceIn(t, d, ManifestFile, "- gke\n", "- gke\n  - github\n")
		}, "\"github\" is neither a built-in tool"},
		{"tool outside include", func(t *testing.T, d string) {
			replaceIn(t, d, ManifestFile, "- gke\n", "- gke.delete_cluster\n")
		}, "names a tool outside servers.gke.include"},
		{"stray top-level file", func(t *testing.T, d string) {
			_ = os.WriteFile(filepath.Join(d, "instruction.md"), []byte("x"), 0o644)
		}, "instruction.md: not part of the persona format"},
		{"symlink", func(t *testing.T, d string) {
			_ = os.Symlink("/etc/hosts", filepath.Join(d, "skills/notes/hosts.md"))
		}, "skills/notes/hosts.md: is not a regular file"},
		{"repeated parameter", func(t *testing.T, d string) {
			replaceIn(t, d, ManifestFile, "requires:", "  - name: cluster\nrequires:")
		}, "parameters[1].name: \"cluster\" repeats"},
		{"a parameter name with a hyphen", func(t *testing.T, d string) {
			replaceIn(t, d, ManifestFile, "requires:", "  - name: target-cluster\nrequires:")
		}, "parameters[1].name: \"target-cluster\" is not lowercase letters"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			dir := copyExample(t)
			c.edit(t, dir)
			_, err := Load(dir)
			if err == nil {
				t.Fatal("Load accepted it")
			}
			if !IsInvalid(err) {
				t.Fatalf("got a non-validation error: %v", err)
			}
			if !strings.Contains(err.Error(), c.want) {
				t.Errorf("error does not say %q:\n%v", c.want, err)
			}
		})
	}
}

func TestLoadReportsEveryProblemAtOnce(t *testing.T) {
	dir := copyExample(t)
	replaceIn(t, dir, ManifestFile, "v1alpha1", "v9")
	replaceIn(t, dir, "skills/triage/SKILL.md", "name: triage", "name: other")
	_, err := Load(dir)
	if err == nil {
		t.Fatal("Load accepted it")
	}
	for _, want := range []string{"apiVersion", "skills/triage/SKILL.md: name"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error lacks %q:\n%v", want, err)
		}
	}
}

func TestLocalPath(t *testing.T) {
	for p, want := range map[string]bool{
		"CLAUDE.md": true, "skills/a/SKILL.md": true,
		"": false, ".": false, "..": false, "../x": false, "/etc/passwd": false,
		"a/../../x": false, "a//b": false, `a\b`: false,
	} {
		if got := LocalPath(p); got != want {
			t.Errorf("LocalPath(%q) = %v, want %v", p, got, want)
		}
	}
}

// Fences are no shelter: the harness's idea of a fence is not ours to mirror.
// An import that stays inside the persona passes.
func TestLoadImportsInsideThePersona(t *testing.T) {
	dir := copyExample(t)
	replaceIn(t, dir, InstructionsFile, "# Example", "```\n@/etc/hosts\n```\n\n# Example")
	var invalid *InvalidError
	if _, err := Load(dir); !errors.As(err, &invalid) || !strings.Contains(err.Error(), `"@/etc/hosts"`) {
		t.Fatalf("an import in a fence: want it refused, got %v", err)
	}
	dir = copyExample(t)
	replaceIn(t, dir, InstructionsFile, "# Example", "See @skills/notes/SKILL.md and mail a@b.example.\n\n# Example")
	if _, err := Load(dir); err != nil {
		t.Fatalf("an import inside the persona: %v", err)
	}
	// Each of these is refused without resolving it the harness's way: a
	// climb after a descent, an @ after a no-break space, a backslash-escaped
	// space, a non-ASCII character.
	for _, line := range []string{
		// Inside the config directory, beside CLAUDE.md, but not the persona's.
		"Read @settings.json and @.credentials.json",
		"@instructions.md",
		"@skills/../../outside",
		"see\u00a0@/etc/hosts",
		`@a\ b/x`,
		"@skills/caf\u00e9.md",
	} {
		dir = copyExample(t)
		replaceIn(t, dir, InstructionsFile, "# Example", line+"\n\n# Example")
		if _, err := Load(dir); !errors.As(err, &invalid) {
			t.Errorf("%q: want it refused, got %v", line, err)
		}
	}
}

// A tier is its only problem: the rest of a valid manifest still decodes,
// so no field the file sets correctly is reported as broken.
func TestATierIsTheOnlyProblem(t *testing.T) {
	dir := copyExample(t)
	replaceIn(t, dir, ManifestFile, "model:", "tier: read\nmodel:")
	_, err := Load(dir)
	var invalid *InvalidError
	if !errors.As(err, &invalid) || len(invalid.Problems) != 1 || invalid.Problems[0].Field != "tier" {
		t.Fatalf("want exactly the tier problem, got %v", err)
	}
}

// Beside a tier, the manifest's other problems are still reported, a
// repeated key among them; a tier nested under a harness's hints is not
// the manifest's tier.
func TestATierHidesNoOtherProblem(t *testing.T) {
	dir := copyExample(t)
	replaceIn(t, dir, ManifestFile, "model:", "tier: read\nimage: registry.example/x:1\nmodel:")
	if _, err := Load(dir); err == nil || !strings.Contains(err.Error(), "tier: is not a persona field") ||
		!strings.Contains(err.Error(), `unknown field "image"`) {
		t.Errorf("a tier beside an unknown field: want both reported, got %v", err)
	}
	dir = copyExample(t)
	replaceIn(t, dir, ManifestFile, "model:", "tier: read\nmodel: a\nmodel:")
	if _, err := Load(dir); err == nil || !strings.Contains(err.Error(), "already set") {
		t.Errorf("a tier beside a repeated key: want the repeat reported, got %v", err)
	}
	dir = copyExample(t)
	replaceIn(t, dir, ManifestFile, "claude-code:", "claude-code:\n    tier: read")
	if _, err := Load(dir); err != nil && strings.Contains(err.Error(), "tier: is not a persona field") {
		t.Errorf("a tier under harness hints was taken for the manifest's: %v", err)
	}
}
