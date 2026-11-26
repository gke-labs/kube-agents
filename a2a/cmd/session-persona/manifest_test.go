package main

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	personafmt "github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/coreagent"
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
