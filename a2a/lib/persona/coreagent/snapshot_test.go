package coreagent_test

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/coreagent"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/render"
)

// renderTo writes the fleet-audit persona's core-agent rendering into a
// temporary directory, as the render step does.
func renderTo(t *testing.T) (string, *persona.Persona) {
	t.Helper()
	p := load(t, fleetAuditPersonaDir)
	out, err := coreagent.Adapter{}.Render(p, persona.Target{})
	if err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(t.TempDir(), "agent-persona")
	if err := render.WriteFiles(dir, out.Files); err != nil {
		t.Fatal(err)
	}
	return dir, p
}

// What the render step writes reads back whole, in the skill loader's
// layout, and a change to the directory afterwards doesn't reach it.
func TestARenderingReadsBackIntoMemory(t *testing.T) {
	dir, p := renderTo(t)
	r, err := coreagent.Snapshot(dir)
	if err != nil {
		t.Fatalf("snapshot: %v", err)
	}
	if r.Instructions != p.Instructions {
		t.Error("instructions differ from the persona's")
	}
	for _, s := range p.Skills {
		for _, f := range s.Files {
			got, err := fs.ReadFile(r.Skills, s.Name+"/"+f.Path)
			if err != nil {
				t.Errorf("%s/%s: %v", s.Name, f.Path, err)
				continue
			}
			if f.Path == "SKILL.md" {
				sameSkill(t, s.Name, f.Data, got)
			} else if string(got) != string(f.Data) {
				t.Errorf("%s/%s differs", s.Name, f.Path)
			}
		}
	}
	skill := p.Skills[0].Name + "/SKILL.md"
	if err := os.WriteFile(filepath.Join(dir, "skills", skill), []byte("changed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got, _ := fs.ReadFile(r.Skills, skill); string(got) == "changed\n" {
		t.Error("a change on disk after the snapshot reached it")
	}
}

func TestASnapshotRefusesWhatARenderingDoesntHold(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(t *testing.T, dir string)
		want  string
	}{
		{"an extra name", func(t *testing.T, dir string) {
			write(t, filepath.Join(dir, "hooks.json"), "{}")
		}, `"hooks.json"`},
		{"a symlinked skill file", func(t *testing.T, dir string) {
			outside := filepath.Join(t.TempDir(), "x")
			write(t, outside, "x")
			if err := os.Symlink(outside, filepath.Join(dir, "skills", "fleet-audit", "planted.md")); err != nil {
				t.Fatal(err)
			}
		}, "not a regular file"},
		{"a symlinked instructions file", func(t *testing.T, dir string) {
			outside := filepath.Join(t.TempDir(), "x")
			write(t, outside, "x")
			if err := os.Remove(filepath.Join(dir, coreagent.InstructionsFile)); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(outside, filepath.Join(dir, coreagent.InstructionsFile)); err != nil {
				t.Fatal(err)
			}
		}, "not a regular file"},
		{"a symlinked skills directory", func(t *testing.T, dir string) {
			outside := t.TempDir()
			if err := os.RemoveAll(filepath.Join(dir, "skills")); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(outside, filepath.Join(dir, "skills")); err != nil {
				t.Fatal(err)
			}
		}, "not a directory"},
		{"a symlinked directory inside a skill", func(t *testing.T, dir string) {
			outside := t.TempDir()
			write(t, filepath.Join(outside, "x.md"), "x")
			if err := os.Symlink(outside, filepath.Join(dir, "skills", "fleet-audit", "planted")); err != nil {
				t.Fatal(err)
			}
		}, "not a regular file"},
		{"a FIFO", func(t *testing.T, dir string) {
			if err := syscall.Mkfifo(filepath.Join(dir, "skills", "fleet-audit", "pipe"), 0o644); err != nil {
				t.Skip(err)
			}
		}, "not a regular file"},
		{"skills as a file", func(t *testing.T, dir string) {
			if err := os.RemoveAll(filepath.Join(dir, "skills")); err != nil {
				t.Fatal(err)
			}
			write(t, filepath.Join(dir, "skills"), "x")
		}, "not a directory"},
		{"a file directly under skills", func(t *testing.T, dir string) {
			write(t, filepath.Join(dir, "skills", "loose.md"), "x")
		}, "not a skill directory"},
		{"over the size cap", func(t *testing.T, dir string) {
			write(t, filepath.Join(dir, "skills", "fleet-audit", "big.md"), strings.Repeat("x", 17<<20))
		}, "over"},
		{"no instructions", func(t *testing.T, dir string) {
			if err := os.Remove(filepath.Join(dir, coreagent.InstructionsFile)); err != nil {
				t.Fatal(err)
			}
		}, coreagent.InstructionsFile},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir, _ := renderTo(t)
			tc.setup(t, dir)
			if _, err := coreagent.Snapshot(dir); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Errorf("err = %v, want %s", err, tc.want)
			}
		})
	}
}

func TestARenderingWithNoSkillsHasAnEmptySkillsFS(t *testing.T) {
	dir := t.TempDir()
	write(t, filepath.Join(dir, coreagent.InstructionsFile), "be useful\n")
	r, err := coreagent.Snapshot(dir)
	if err != nil {
		t.Fatal(err)
	}
	if entries, err := fs.ReadDir(r.Skills, "."); err != nil || len(entries) != 0 {
		t.Errorf("skills = %v, %v; want empty", entries, err)
	}
}

func write(t *testing.T, path, data string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(data), 0o644); err != nil {
		t.Fatal(err)
	}
}

// The worker's session check and the harness's snapshot read one rendering:
// its manifest sits beside it, the check verifies the rendering against it,
// and the snapshot skips it.
func TestTheSessionCheckAndTheSnapshotReadOneRendering(t *testing.T) {
	p := load(t, fleetAuditPersonaDir)
	out, err := coreagent.Adapter{}.Render(p, persona.Target{})
	if err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(t.TempDir(), "agent-persona")
	if err := render.WriteFiles(dir, out.Files); err != nil {
		t.Fatal(err)
	}
	files := map[string][]byte{}
	for _, f := range out.Files {
		files[f.Path] = f.Data
	}
	write(t, filepath.Join(dir, persona.IntegrityManifestFile), string(persona.FormatIntegrityManifest(files)))
	integrity, n, err := persona.CheckTree(dir, coreagent.Covers)
	if err != nil || integrity != persona.IntegrityVerified || n != len(out.Files) {
		t.Fatalf("CheckTree = %v, %d, %v; want verified over %d files", integrity, n, err, len(out.Files))
	}
	if _, err := coreagent.Snapshot(dir); err != nil {
		t.Fatalf("snapshot beside a manifest: %v", err)
	}
}

// A file with any execute bit is a script in the snapshot.
func TestAGroupExecutableFileIsAScript(t *testing.T) {
	dir := t.TempDir()
	write(t, filepath.Join(dir, coreagent.InstructionsFile), "x\n")
	write(t, filepath.Join(dir, "skills", "s", "SKILL.md"), "---\nname: s\ndescription: d\n---\n")
	write(t, filepath.Join(dir, "skills", "s", "run.sh"), "#!/bin/sh\n")
	if err := os.Chmod(filepath.Join(dir, "skills", "s", "run.sh"), 0o614); err != nil {
		t.Fatal(err)
	}
	r, err := coreagent.Snapshot(dir)
	if err != nil {
		t.Fatal(err)
	}
	info, err := fs.Stat(r.Skills, "s/run.sh")
	if err != nil || info.Mode()&0o111 == 0 {
		t.Errorf("run.sh mode = %v, %v; want executable", info, err)
	}
}
