package persona

import (
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
			replaceIn(t, d, ManifestFile, "tier:", "image: registry.example/x:1\ntier:")
		}, "unknown field \"image\""},
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
		}, "skills/notes/SKILL.md: description: is 1025 characters"},
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
