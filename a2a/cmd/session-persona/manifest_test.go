package main

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	personafmt "github.com/gke-labs/kube-agents/a2a/lib/persona"
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
