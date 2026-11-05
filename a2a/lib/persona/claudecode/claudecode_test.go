package claudecode

import (
	"encoding/json"
	"flag"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

var update = flag.Bool("update", false, "rewrite the golden files")

const (
	exampleDir = "../testdata/example"
	goldenDir  = "testdata/golden"
)

// relay is the shape the profile worker passes in: the broker's relay
// reached through mcp-remote, as agents/cluster/config.yaml reaches it.
var relay = persona.Relay{
	URL:     "http://credential-proxy:8765/mcp/",
	Command: "node",
	Args:    []string{"/opt/mcp-remote/dist/proxy.js", "{url}", "--allow-http", "--transport", "http-only"},
	Env:     map[string]string{"MCP_REMOTE_BEARER_FILE": "/var/run/secrets/credential-proxy/token"},
}

func load(t *testing.T, dir string) *persona.Persona {
	t.Helper()
	p, err := persona.Load(dir)
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func render(t *testing.T, p *persona.Persona, target persona.Target) persona.Output {
	t.Helper()
	out, err := Adapter{}.Render(p, target)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// golden is what the golden directory holds besides the files: the flags,
// with --mcp-config decoded so a diff reads, and the warnings.
type golden struct {
	Args      []string       `json:"args"`
	MCPConfig map[string]any `json:"mcpConfig"`
	Model     string         `json:"model"`
	Warnings  []string       `json:"warnings"`
}

func goldenOf(t *testing.T, out persona.Output) golden {
	g := golden{Model: out.Model, Warnings: out.Warnings}
	for i := 0; i < len(out.Args); i++ {
		if out.Args[i] == MCPConfigFlag {
			if err := json.Unmarshal([]byte(out.Args[i+1]), &g.MCPConfig); err != nil {
				t.Fatalf("--mcp-config is not JSON: %v", err)
			}
			g.Args = append(g.Args, MCPConfigFlag, "<mcpConfig>")
			i++
			continue
		}
		g.Args = append(g.Args, out.Args[i])
	}
	return g
}

func TestRenderMatchesTheGolden(t *testing.T) {
	out := render(t, load(t, exampleDir), persona.Target{Relay: relay})
	files := filepath.Join(goldenDir, "files")
	meta := filepath.Join(goldenDir, "output.json")
	got, _ := json.MarshalIndent(goldenOf(t, out), "", "  ")
	if *update {
		_ = os.RemoveAll(goldenDir)
		for _, f := range out.Files {
			p := filepath.Join(files, filepath.FromSlash(f.Path))
			if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(p, f.Data, 0o644); err != nil {
				t.Fatal(err)
			}
		}
		if err := os.WriteFile(meta, append(got, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(meta)
	if err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(string(want)) != string(got) {
		t.Errorf("output differs from %s (go test -update to rewrite):\n%s", meta, got)
	}
	var wantPaths []string
	_ = filepath.WalkDir(files, func(p string, d fs.DirEntry, err error) error {
		if err == nil && !d.IsDir() {
			rel, _ := filepath.Rel(files, p)
			wantPaths = append(wantPaths, filepath.ToSlash(rel))
		}
		return nil
	})
	var gotPaths []string
	for _, f := range out.Files {
		gotPaths = append(gotPaths, f.Path)
		data, err := os.ReadFile(filepath.Join(files, filepath.FromSlash(f.Path)))
		if err != nil || string(data) != string(f.Data) {
			t.Errorf("%s differs from the golden file: %v", f.Path, err)
		}
	}
	slices.Sort(gotPaths)
	if !slices.Equal(gotPaths, wantPaths) {
		t.Errorf("files = %v, golden has %v", gotPaths, wantPaths)
	}
}

func TestEveryArgvCarriesTheHardeningFlags(t *testing.T) {
	bare := load(t, exampleDir)
	bare.Manifest.Tools = nil
	bare.MCP = persona.MCP{}
	bare.Skills = nil
	for name, out := range map[string]persona.Output{
		"example":           render(t, load(t, exampleDir), persona.Target{Relay: relay}),
		"no tools, no mcp":  render(t, bare, persona.Target{}),
		"servers but relay": render(t, load(t, exampleDir), persona.Target{}),
	} {
		args := strings.Join(out.Args, " ")
		if !slices.Contains(out.Args, StrictMCPConfigFlag) {
			t.Errorf("%s: no %s in %s", name, StrictMCPConfigFlag, args)
		}
		i := slices.Index(out.Args, SettingSourcesFlag)
		if i < 0 || i+1 >= len(out.Args) || out.Args[i+1] != UserSettingSource {
			t.Errorf("%s: no %s %s in %s", name, SettingSourcesFlag, UserSettingSource, args)
		}
	}
	for name, out := range map[string]persona.Output{
		"example":          render(t, load(t, exampleDir), persona.Target{Relay: relay}),
		"no tools, no mcp": render(t, bare, persona.Target{}),
	} {
		i := slices.Index(out.Args, DisallowedToolsFlag)
		if i < 0 || i+1 >= len(out.Args) {
			t.Errorf("%s: no %s in %v", name, DisallowedToolsFlag, out.Args)
		}
	}
	if slices.Contains(render(t, bare, persona.Target{}).Args, AllowedToolsFlag) {
		t.Errorf("a persona granting nothing still got %s", AllowedToolsFlag)
	}
}

func TestToolsMapToClaudeCodeNames(t *testing.T) {
	out := render(t, load(t, exampleDir), persona.Target{Relay: relay})
	i := slices.Index(out.Args, AllowedToolsFlag)
	if i < 0 {
		t.Fatalf("no %s in %v", AllowedToolsFlag, out.Args)
	}
	want := "Read,Glob,Grep,LS,NotebookRead,mcp__gke__get_cluster,mcp__gke__list_clusters,mcp__docs__search,Skill"
	if out.Args[i+1] != want {
		t.Errorf("%s = %s, want %s", AllowedToolsFlag, out.Args[i+1], want)
	}
}

// One warning for each thing Claude Code cannot take, and none for what
// belongs to another harness.
func TestEachDroppedPartIsAWarning(t *testing.T) {
	out := render(t, load(t, exampleDir), persona.Target{})
	wants := []string{
		"harness.claude-code.maxTurns: not supported",
		"skills/triage/scripts/events.sh: dropped",
		"servers.docs: dropped, the worker gave no broker MCP relay",
		"servers.gke: dropped, the worker gave no broker MCP relay",
		"servers.unused: dropped, the worker gave no broker MCP relay",
		`tools: "gke" dropped with its server`,
		`tools: "docs.search" dropped with its server`,
	}
	for _, w := range wants {
		if !slices.ContainsFunc(out.Warnings, func(s string) bool { return strings.Contains(s, w) }) {
			t.Errorf("no warning containing %q in %q", w, out.Warnings)
		}
	}
	granted := render(t, load(t, exampleDir), persona.Target{Relay: relay})
	if !slices.ContainsFunc(granted.Warnings, func(s string) bool {
		return strings.Contains(s, "servers.unused: dropped, no tools entry grants")
	}) {
		t.Errorf("an ungranted server was not dropped with a warning: %q", granted.Warnings)
	}
	if len(out.Warnings) != len(wants) {
		t.Errorf("got %d warnings, want %d: %q", len(out.Warnings), len(wants), out.Warnings)
	}
	for _, w := range out.Warnings {
		if strings.Contains(w, string(persona.Antigravity)) {
			t.Errorf("warned about another harness's hint: %s", w)
		}
	}
}

func TestAShellKeepsSkillScripts(t *testing.T) {
	p := load(t, exampleDir)
	p.Manifest.Tools = append(p.Manifest.Tools, persona.ToolShell)
	out := render(t, p, persona.Target{Relay: relay})
	i := slices.IndexFunc(out.Files, func(f persona.File) bool { return f.Path == "skills/triage/scripts/events.sh" })
	if i < 0 || !out.Files[i].Executable {
		t.Fatalf("the script is missing or lost its mode with %s granted: %+v", persona.ToolShell, out.Files)
	}
}

func TestOutputStaysInTheConfigDir(t *testing.T) {
	out := render(t, load(t, exampleDir), persona.Target{Relay: relay})
	for _, f := range out.Files {
		if !persona.LocalPath(f.Path) {
			t.Errorf("output path %q leaves the config directory", f.Path)
		}
		if f.Path != MemoryFile && !strings.HasPrefix(f.Path, SkillsDir+"/") {
			t.Errorf("output path %q is neither %s nor under %s/", f.Path, MemoryFile, SkillsDir)
		}
	}
}

func flagValue(t *testing.T, args []string, flag string) []string {
	t.Helper()
	i := slices.Index(args, flag)
	if i < 0 || i+1 >= len(args) {
		t.Fatalf("no %s in %v", flag, args)
	}
	return strings.Split(args[i+1], ",")
}

// Every built-in the persona does not grant is denied, and none it grants is.
func TestUngrantedBuiltinsAreDenied(t *testing.T) {
	out := render(t, load(t, exampleDir), persona.Target{Relay: relay})
	deny := flagValue(t, out.Args, DisallowedToolsFlag)
	for _, tool := range []string{"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Task"} {
		if !slices.Contains(deny, tool) {
			t.Errorf("%s is not denied: %v", tool, deny)
		}
	}
	for _, tool := range []string{"Read", "Glob", "Grep", "LS", "NotebookRead"} {
		if slices.Contains(deny, tool) {
			t.Errorf("%s is denied though read_file grants it", tool)
		}
	}
	p := load(t, exampleDir)
	p.Manifest.Tools = []string{persona.ToolShell}
	deny = flagValue(t, render(t, p, persona.Target{}).Args, DisallowedToolsFlag)
	for _, tool := range []string{"Read", "Glob", "Grep", "LS", "NotebookRead"} {
		if !slices.Contains(deny, tool) {
			t.Errorf("%s is not denied without read_file: %v", tool, deny)
		}
	}
	if slices.Contains(deny, "Bash") {
		t.Errorf("Bash is denied though shell grants it")
	}
}

func TestAnUpperCaseMarkdownFileShipsWithoutAShell(t *testing.T) {
	p := load(t, exampleDir)
	p.Skills[0].Files = append(p.Skills[0].Files, persona.File{Path: "NOTES.MD", Data: []byte("x")})
	out := render(t, p, persona.Target{})
	if !slices.ContainsFunc(out.Files, func(f persona.File) bool { return f.Path == "skills/notes/NOTES.MD" }) {
		t.Errorf("NOTES.MD was dropped as a script")
	}
}

// Allowed and Disallowed are the lists the flags carry, so a worker can
// union its own deny list without parsing Args.
func TestTheToolListsMatchTheFlags(t *testing.T) {
	out := render(t, load(t, exampleDir), persona.Target{Relay: relay})
	if got := flagValue(t, out.Args, AllowedToolsFlag); !slices.Equal(got, out.Allowed) {
		t.Errorf("Allowed = %v, the flag says %v", out.Allowed, got)
	}
	if got := flagValue(t, out.Args, DisallowedToolsFlag); !slices.Equal(got, out.Disallowed) {
		t.Errorf("Disallowed = %v, the flag says %v", out.Disallowed, got)
	}
}

// A persona with skills grants the Skill tool, which headless Claude Code
// refuses off the allowlist; one with none does not.
func TestSkillsGrantTheSkillTool(t *testing.T) {
	p := load(t, exampleDir)
	if !slices.Contains(render(t, p, persona.Target{}).Allowed, skillTool) {
		t.Errorf("a persona with skills does not grant %s", skillTool)
	}
	p.Skills = nil
	if slices.Contains(render(t, p, persona.Target{}).Allowed, skillTool) {
		t.Errorf("a persona with no skills grants %s", skillTool)
	}
}

// A relay that never names the server is refused, not rendered as N
// identical proxies with no target.
func TestARelayWithoutTheURLPlaceholderIsAnError(t *testing.T) {
	bad := relay
	bad.Args = []string{"/opt/mcp-remote/dist/proxy.js", "--transport", "http-only"}
	if _, err := (Adapter{}).Render(load(t, exampleDir), persona.Target{Relay: bad}); err == nil {
		t.Fatal("a relay with no {url} in its args rendered")
	}
}
