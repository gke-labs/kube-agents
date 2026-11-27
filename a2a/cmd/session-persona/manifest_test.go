package main

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	personafmt "github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/coreagent"
	"google.golang.org/adk/v2/tool/skilltoolset"
	"google.golang.org/adk/v2/tool/skilltoolset/skill"
)

// The tree ships with a content manifest that covers exactly what was
// written: the persona file and every skill, checked the way the worker
// checks it at start.
func TestShippedTreeVerifiesAgainstItsManifest(t *testing.T) {
	out := buildShipped(t)
	integrity, files, err := personafmt.CheckTree(out, lib.SessionHomeCovers)
	if err != nil {
		t.Fatal(err)
	}
	if integrity != personafmt.IntegrityVerified || files != 1+len(sessionSkills) {
		t.Errorf("CheckTree = %v over %d files, want verified over %d", integrity, files, 1+len(sessionSkills))
	}
}

// The tool's file names and the worker's scope agree: a file written
// outside what lib.SessionHomeCovers selects would never be checked.
func TestShippedTreeIsInsideTheCheckedScope(t *testing.T) {
	out := buildShipped(t)
	err := filepath.WalkDir(out, func(p string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		rel, _ := filepath.Rel(out, p)
		rel = filepath.ToSlash(rel)
		if rel != personafmt.IntegrityManifestFile && !lib.SessionHomeCovers(rel) {
			t.Errorf("%s is shipped but outside the checked scope", rel)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

// A shipped skill changed after the build is caught, and the harness's own
// state beside the tree is not part of the check.
func TestShippedTreeCatchesAChangedSkillNotHarnessState(t *testing.T) {
	out := buildShipped(t)
	if err := os.MkdirAll(filepath.Join(out, "projects"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(out, "projects", "state.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(out, ".credentials.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := personafmt.CheckTree(out, lib.SessionHomeCovers); err != nil {
		t.Fatalf("harness state beside the tree failed the check: %v", err)
	}
	skill := filepath.Join(out, skillsDir, sessionSkills[0], skillFile)
	if err := os.Chmod(skill, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(skill, []byte("changed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, _, err := personafmt.CheckTree(out, lib.SessionHomeCovers)
	var inv *personafmt.InvalidError
	if !errors.As(err, &inv) || !strings.Contains(err.Error(), sessionSkills[0]+"/"+skillFile) {
		t.Errorf("CheckTree = %v, want the changed skill named", err)
	}
}

// The core-agent layout holds the same persona and skills under the names a
// core-agent rendering has, and once its write bits are cleared, as the
// worker image does, it passes the harness's required-persona check and
// reads as every session skill.
func TestTheCoreAgentTreeIsARenderingTheHarnessAccepts(t *testing.T) {
	out := filepath.Join(t.TempDir(), "core-agent-session")
	if err := buildLayout(repoSkillsDir, repoPersona, out, coreAgentLayout); err != nil {
		t.Fatal(err)
	}
	claude := buildShipped(t)
	for _, name := range sessionSkills {
		got, err := os.ReadFile(filepath.Join(out, coreagent.SkillsDir, name, skillFile))
		if err != nil {
			t.Fatal(err)
		}
		want, err := os.ReadFile(filepath.Join(claude, skillsDir, name, skillFile))
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != string(want) {
			t.Errorf("%s differs between the two layouts", name)
		}
	}
	sealTree(t, out)
	if err := coreagent.VerifyRendering(out, -1); err != nil {
		t.Fatal(err)
	}
	snap, err := coreagent.Snapshot(out)
	if err != nil {
		t.Fatal(err)
	}
	entries, err := fs.ReadDir(snap.Skills, ".")
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != len(sessionSkills) {
		t.Errorf("snapshot has %d skills, want %d", len(entries), len(sessionSkills))
	}
	if snap.Instructions == "" {
		t.Error("the snapshot has no instructions")
	}
	// What the harness does with the skills at each turn: its loader lists
	// every skill's frontmatter and builds the skill toolset, and refuses a
	// name outside core-agent's rule, so a skill that would fail every
	// core-agent session turn fails here instead.
	for _, name := range sessionSkills {
		if !coreagent.ValidSkillName(name) {
			t.Errorf("%s is not a skill name core-agent takes", name)
		}
	}
	src := skill.NewFileSystemSource(snap.Skills)
	listed, err := src.ListFrontmatters(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(listed) != len(sessionSkills) {
		t.Errorf("the skill loader lists %d skills, want %d", len(listed), len(sessionSkills))
	}
	if _, err := skilltoolset.New(context.Background(), skilltoolset.Config{Source: src}); err != nil {
		t.Errorf("the skill toolset refuses the session skills: %v", err)
	}
}

// sealTree clears every write bit under root, as the worker image's
// `chmod -R a-w` does, and restores them for cleanup.
func sealTree(t *testing.T, root string) {
	t.Helper()
	var dirs []string
	err := filepath.WalkDir(root, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			dirs = append(dirs, p)
			return nil
		}
		return os.Chmod(p, 0o444)
	})
	if err != nil {
		t.Fatal(err)
	}
	for i := len(dirs) - 1; i >= 0; i-- {
		if err := os.Chmod(dirs[i], 0o555); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() {
		for _, d := range dirs {
			_ = os.Chmod(d, 0o755)
		}
	})
}

// The two layouts carry the same skills, by name: the core-agent tree has
// no skill the Claude Code tree lacks, and lacks none it has.
func TestBothLayoutsShipTheSameSkills(t *testing.T) {
	coreAgent := filepath.Join(t.TempDir(), "core-agent-session")
	if err := buildLayout(repoSkillsDir, repoPersona, coreAgent, coreAgentLayout); err != nil {
		t.Fatal(err)
	}
	names := func(dir string) []string {
		entries, err := os.ReadDir(dir)
		if err != nil {
			t.Fatal(err)
		}
		var out []string
		for _, e := range entries {
			out = append(out, e.Name())
		}
		return out
	}
	claude := names(filepath.Join(buildShipped(t), skillsDir))
	core := names(filepath.Join(coreAgent, coreagent.SkillsDir))
	if strings.Join(claude, ",") != strings.Join(core, ",") {
		t.Errorf("skills differ: Claude Code %v, core-agent %v", claude, core)
	}
	if len(core) != len(sessionSkills) {
		t.Errorf("%d skills shipped, want the %d sessionSkills names", len(core), len(sessionSkills))
	}
}

// claudeOnlyToolNames are Claude Code's tool names as the session text could
// refer to them; none may reach the core-agent tree, whose tools are named
// otherwise (the skills come through load_skill, the shell is the harness's).
var claudeOnlyToolNames = regexp.MustCompile(`\b(Skill|Bash|Read|Glob|Grep|Task|TodoWrite|WebFetch|WebSearch|Edit|Write) tool\b|\bBash\(`)

// The core-agent tree names core-agent's tools, and the Claude Code tree is
// the source as it was: the persona's skill instruction is rewritten only in
// the core-agent layout.
func TestTheCoreAgentTreeCarriesNoClaudeToolNames(t *testing.T) {
	coreAgent := filepath.Join(t.TempDir(), "core-agent-session")
	if err := buildLayout(repoSkillsDir, repoPersona, coreAgent, coreAgentLayout); err != nil {
		t.Fatal(err)
	}
	err := filepath.WalkDir(coreAgent, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		body, err := os.ReadFile(p) // #nosec G304 -- a test's own temp tree
		if err != nil {
			return err
		}
		if m := claudeOnlyToolNames.Find(body); m != nil {
			t.Errorf("%s names a Claude Code tool: %q", strings.TrimPrefix(p, coreAgent+"/"), m)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	instructions, err := os.ReadFile(filepath.Join(coreAgent, coreagent.InstructionsFile)) // #nosec G304 -- a test's own temp tree
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(instructions), "with `load_skill`") {
		t.Error("the core-agent persona doesn't name load_skill for loading a skill")
	}
	source, err := os.ReadFile(repoPersona) // #nosec G304 -- the repository's persona source
	if err != nil {
		t.Fatal(err)
	}
	shipped, err := os.ReadFile(filepath.Join(buildShipped(t), personaFile)) // #nosec G304 -- a test's own temp tree
	if err != nil {
		t.Fatal(err)
	}
	if string(shipped) != string(source) {
		t.Error("the Claude Code persona differs from its source")
	}
}
