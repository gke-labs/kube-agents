package main

import (
	"context"
	"io/fs"
	"iter"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"

	"google.golang.org/adk/v2/tool/skilltoolset/skill"
	"google.golang.org/genai"

	"github.com/go-steer/core-agent/v3/pkg/agent"
	"github.com/go-steer/core-models/llm"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/coreagent"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/render"
)

var shippedPersonas = []string{"cluster", "tse", "fleet-audit"}

var testRelay = persona.Relay{URL: "http://broker:8080/v1/mcp", Command: "/worker-adapter", Args: []string{"mcp-relay", "{url}"}}

// renderPersona writes a shipped persona's core-agent rendering into a
// temporary directory, as the render step does.
func renderPersona(t *testing.T, name string) (string, *persona.Persona) {
	t.Helper()
	p, err := persona.Load(filepath.Join("../../../agents", name, "persona"))
	if err != nil {
		t.Fatal(err)
	}
	out, err := coreagent.Adapter{}.Render(p, persona.Target{Relay: testRelay})
	if err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	dir := filepath.Join(t.TempDir(), "agent-persona")
	if err := render.WriteFiles(dir, out.Files); err != nil {
		t.Fatal(err)
	}
	sealRendering(t, dir, out.Files)
	return dir, p
}

// sealRendering does what the render step does after writing: a content
// manifest beside the files, and nothing writable. The owner check is off,
// since the test wrote the files as itself.
func sealRendering(t *testing.T, dir string, files []persona.File) {
	t.Helper()
	contents := map[string][]byte{}
	for _, f := range files {
		contents[f.Path] = f.Data
	}
	if err := os.WriteFile(filepath.Join(dir, persona.IntegrityManifestFile), persona.FormatIntegrityManifest(contents), 0o444); err != nil {
		t.Fatal(err)
	}
	var dirs []string
	_ = filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			dirs = append(dirs, p)
			return nil
		}
		info, _ := d.Info()
		mode := fs.FileMode(0o444)
		if info.Mode()&0o111 != 0 {
			mode = 0o555
		}
		return os.Chmod(p, mode)
	})
	for i := len(dirs) - 1; i >= 0; i-- {
		_ = os.Chmod(dirs[i], 0o555)
	}
	t.Cleanup(func() {
		for _, d := range dirs {
			_ = os.Chmod(d, 0o755)
		}
	})
	prev := personaOwnerUID
	personaOwnerUID = -1
	t.Cleanup(func() { personaOwnerUID = prev })
}

// Every shipped persona renders for core-agent, and every rendered skill is
// one ADK's own skill parser accepts: one it refused would take the whole
// skill toolset down at the first turn, after render had passed.
func TestEveryShippedPersonaLoadsIntoCoreAgent(t *testing.T) {
	for _, name := range shippedPersonas {
		t.Run(name, func(t *testing.T) {
			dir, p := renderPersona(t, name)
			r, err := coreagent.Snapshot(dir)
			if err != nil {
				t.Fatal(err)
			}
			for _, s := range p.Skills {
				data, err := fs.ReadFile(r.Skills, s.Name+"/SKILL.md")
				if err != nil {
					t.Fatal(err)
				}
				fm, _, err := skill.ParseBytes(data)
				if err != nil {
					t.Fatalf("ADK refuses skills/%s/SKILL.md: %v", s.Name, err)
				}
				if fm.Name != s.Name {
					t.Errorf("skills/%s: ADK reads the name %q", s.Name, fm.Name)
				}
			}
			instructions, sets, err := loadPersona(context.Background(), dir, true)
			if err != nil {
				t.Fatalf("loadPersona: %v", err)
			}
			if instructions != p.Instructions {
				t.Error("instructions differ from the persona's")
			}
			if (len(p.Skills) > 0) != (len(sets) == 1) {
				t.Errorf("%d skills gave %d toolsets", len(p.Skills), len(sets))
			}
		})
	}
}

// A loaded skill's instructions start with its directory on disk, where its
// scripts are run from; the rest is the skill's body.
func TestALoadedSkillNamesItsBaseDirectory(t *testing.T) {
	dir, p := renderPersona(t, "cluster")
	r, err := coreagent.Snapshot(dir)
	if err != nil {
		t.Fatal(err)
	}
	exec, err := executables(r.Skills)
	if err != nil {
		t.Fatal(err)
	}
	src := baseDirSource{Source: skill.NewFileSystemSource(r.Skills), base: filepath.Join(dir, coreagent.SkillsDir), exec: exec}
	for _, s := range p.Skills {
		text, err := src.LoadInstructions(context.Background(), s.Name)
		if err != nil {
			t.Fatal(err)
		}
		base := filepath.Join(dir, "skills", s.Name)
		if !strings.HasPrefix(text, skillBaseDirPrefix+base+"\n") {
			t.Errorf("%s: instructions don't open with its base directory", s.Name)
		}
		// A skill with scripts lists each by its absolute path. A persona
		// that ships scripts grants the shell, which runs any command, so
		// the note tells the model nothing is refused that the shell would
		// in fact run (an interpreter in front of a script, for one).
		header, _, _ := strings.Cut(text, "\n\n")
		if strings.Contains(header, "refused") || strings.Contains(header, "python3") {
			t.Errorf("%s: the skill's header tells the model a command is refused: %q", s.Name, header)
		}
		for _, f := range s.Files {
			if f.Executable && !strings.Contains(text, "- "+filepath.Join(base, f.Path)+"\n") {
				t.Errorf("%s: script %s isn't listed by its absolute path", s.Name, f.Path)
			}
		}
	}
	if len(exec) == 0 {
		t.Error("the cluster persona's scripts weren't found executable in the snapshot")
	}
}

// ScriptPaths names exactly the rendering's scripts, absolute under the
// persona directory: what the shell rules grant.
func TestScriptPathsAreTheRenderedScripts(t *testing.T) {
	p, err := persona.Load("../../../agents/cluster/persona")
	if err != nil {
		t.Fatal(err)
	}
	out, err := coreagent.Adapter{}.Render(p, persona.Target{Relay: testRelay})
	if err != nil {
		t.Fatal(err)
	}
	paths, err := coreagent.ScriptPaths(out, "/var/lib/agent-persona")
	if err != nil {
		t.Fatal(err)
	}
	var want []string
	for _, s := range p.Skills {
		for _, f := range s.Files {
			if f.Executable {
				want = append(want, "/var/lib/agent-persona/skills/"+s.Name+"/"+f.Path)
			}
		}
	}
	if len(want) == 0 || !slices.Equal(paths, want) {
		t.Errorf("ScriptPaths = %v, want %v", paths, want)
	}
}

// promptModel records the system instruction of every request.
type promptModel struct {
	mu      sync.Mutex
	prompts []string
}

func (m *promptModel) Name() string { return "gemini-2.5-flash" }

func (m *promptModel) GenerateContent(_ context.Context, req *llm.Request, _ bool) iter.Seq2[*llm.Response, error] {
	var sys []string
	if req.Config != nil && req.Config.SystemInstruction != nil {
		for _, p := range req.Config.SystemInstruction.Parts {
			sys = append(sys, p.Text)
		}
	}
	m.mu.Lock()
	m.prompts = append(m.prompts, strings.Join(sys, ""))
	m.mu.Unlock()
	return func(yield func(*llm.Response, error) bool) {
		yield(&llm.Response{Content: genai.NewContentFromText("reply", genai.RoleModel), TurnComplete: true}, nil)
	}
}

// The persona is a layer on core-agent's own system prompt, not a
// replacement: the model is sent the core contract, the autonomous overlay
// and then the persona's instructions.
func TestThePersonaIsALayerOnTheCoreContract(t *testing.T) {
	m := &promptModel{}
	withModel(t, m)
	dir, p := renderPersona(t, "fleet-audit")
	runTurnWith(t, t.TempDir(), coreagentwire.Envelope{Message: "hello"}, "--persona-dir", dir)
	if len(m.prompts) == 0 {
		t.Fatal("no model call")
	}
	sys := m.prompts[0]
	core := strings.Index(sys, strings.TrimSpace(agent.CoreInstruction))
	overlay := strings.Index(sys, strings.TrimSpace(agent.AutonomousOverlay))
	own := strings.Index(sys, strings.TrimSpace(p.Instructions))
	if core < 0 || overlay < 0 || own < 0 {
		t.Fatalf("system prompt holds core %v, overlay %v, persona %v; want all three", core >= 0, overlay >= 0, own >= 0)
	}
	if !(core < overlay && overlay < own) {
		t.Errorf("layers out of order: core %d, overlay %d, persona %d", core, overlay, own)
	}
}

// The persona reader never reads through a link out of its directory: a link
// inside a skill, or standing in for instructions.md, refuses the persona
// rather than being followed or skipped.
func TestThePersonaReaderStaysInsideItsDir(t *testing.T) {
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "secret"), []byte("SECRET"), 0o600); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "instructions.md"), []byte("be brief"), 0o600); err != nil {
		t.Fatal(err)
	}
	skillDir := filepath.Join(dir, "skills", "probe")
	if err := os.MkdirAll(skillDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(skillDir, "SKILL.md"), []byte("---\nname: probe\ndescription: a probe\n---\nbody\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if instructions, sets, err := loadPersona(context.Background(), dir, false); err != nil || instructions != "be brief" || len(sets) != 1 {
		t.Fatalf("a plain persona: %q, %d toolsets, %v", instructions, len(sets), err)
	}
	if err := os.Symlink(filepath.Join(outside, "secret"), filepath.Join(skillDir, "leak.md")); err != nil {
		t.Fatal(err)
	}
	for _, required := range []bool{false, true} {
		if _, _, err := loadPersona(context.Background(), dir, required); err == nil {
			t.Errorf("required=%v: read a persona holding a link out of its directory", required)
		}
	}

	linked := t.TempDir()
	if err := os.Symlink(filepath.Join(outside, "secret"), filepath.Join(linked, "instructions.md")); err != nil {
		t.Fatal(err)
	}
	// Not required, so the sealed-rendering check (which refuses this
	// writable temp dir first) stays out of it and the reader's own
	// confinement is what refuses the link.
	if got, _, err := loadPersona(context.Background(), linked, false); err == nil {
		t.Errorf("read instructions through a link outside the dir: %q", got)
	}
}

// The scripts a loaded skill lists are exactly the ones ScriptPaths grants
// the shell, for the same rendering on disk.
func TestTheListedScriptsAreTheGrantedOnes(t *testing.T) {
	p, err := persona.Load("../../../agents/cluster/persona")
	if err != nil {
		t.Fatal(err)
	}
	out, err := coreagent.Adapter{}.Render(p, persona.Target{Relay: testRelay})
	if err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(t.TempDir(), "agent-persona")
	if err := render.WriteFiles(dir, out.Files); err != nil {
		t.Fatal(err)
	}
	granted, err := coreagent.ScriptPaths(out, dir)
	if err != nil {
		t.Fatal(err)
	}
	r, err := coreagent.Snapshot(dir)
	if err != nil {
		t.Fatal(err)
	}
	exec, err := executables(r.Skills)
	if err != nil {
		t.Fatal(err)
	}
	var listed []string
	for name, rels := range exec {
		for _, rel := range rels {
			listed = append(listed, filepath.Join(dir, "skills", name, rel))
		}
	}
	slices.Sort(listed)
	slices.Sort(granted)
	if len(granted) == 0 || !slices.Equal(listed, granted) {
		t.Errorf("listed %v, granted %v", listed, granted)
	}
}

func TestARelativePersonaDirectoryIsRefused(t *testing.T) {
	if _, _, err := loadPersona(context.Background(), "agent-persona", true); err == nil || !strings.Contains(err.Error(), "not absolute") {
		t.Errorf("err = %v, want a relative directory refused", err)
	}
}

// A skill name the model supplies that isn't a skill is refused, not read.
func TestLoadingANameThatIsntASkillFails(t *testing.T) {
	dir, _ := renderPersona(t, "cluster")
	r, err := coreagent.Snapshot(dir)
	if err != nil {
		t.Fatal(err)
	}
	src := baseDirSource{Source: skill.NewFileSystemSource(r.Skills), base: filepath.Join(dir, coreagent.SkillsDir)}
	for _, name := range []string{"../x", "x/scripts", ""} {
		if _, err := src.LoadInstructions(context.Background(), name); err == nil {
			t.Errorf("LoadInstructions(%q) succeeded", name)
		}
	}
}

// A pod with no persona directory runs the turn with no persona.
func TestAMissingPersonaDirIsNoPersona(t *testing.T) {
	absent := filepath.Join(t.TempDir(), "absent")
	instructions, toolsets, err := loadPersona(context.Background(), absent, false)
	if err != nil || instructions != "" || len(toolsets) != 0 {
		t.Fatalf("instructions %q, %d toolsets, err %v", instructions, len(toolsets), err)
	}
	// A pod configured for a persona fails the turn when it isn't there.
	if _, _, err := loadPersona(context.Background(), absent, true); err == nil {
		t.Error("a required persona that is missing did not fail the turn")
	}
}

// A pod configured for a persona reads it only sealed and matching its
// manifest: a writable file, a changed file, or a rendering owned by the
// harness's own user refuses the turn.
func TestARequiredPersonaMustBeSealedAndVerified(t *testing.T) {
	dir, _ := renderPersona(t, "fleet-audit")
	if _, _, err := loadPersona(context.Background(), dir, true); err != nil {
		t.Fatalf("a sealed, verified rendering: %v", err)
	}
	personaOwnerUID = os.Getuid()
	if _, _, err := loadPersona(context.Background(), dir, true); err == nil || !strings.Contains(err.Error(), "owned by uid") {
		t.Errorf("a rendering owned by the harness's user: err = %v", err)
	}
	personaOwnerUID = -1
	instr := filepath.Join(dir, "instructions.md")
	if err := os.Chmod(instr, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, _, err := loadPersona(context.Background(), dir, true); err == nil || !strings.Contains(err.Error(), "writable") {
		t.Errorf("a writable instructions file: err = %v", err)
	}
	if err := os.WriteFile(instr, []byte("changed\n"), 0o444); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(instr, 0o444); err != nil {
		t.Fatal(err)
	}
	if _, _, err := loadPersona(context.Background(), dir, true); err == nil {
		t.Error("a rendering that differs from its manifest was read")
	}
}
