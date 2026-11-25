package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// The persona reader takes the instructions and the skills under dir, and a
// link inside dir can't lead it outside.
func TestThePersonaReaderStaysInsideItsDir(t *testing.T) {
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "secret"), []byte("SECRET"), 0o600); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "instructions.md"), []byte("be brief"), 0o600); err != nil {
		t.Fatal(err)
	}
	skill := filepath.Join(dir, "skills", "probe")
	if err := os.MkdirAll(skill, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(skill, "SKILL.md"), []byte("---\nname: probe\ndescription: a probe\n---\nbody\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(outside, "secret"), filepath.Join(skill, "leak.md")); err != nil {
		t.Fatal(err)
	}
	instructions, toolsets, err := loadPersona(context.Background(), dir, true)
	if err != nil {
		t.Fatal(err)
	}
	if instructions != "be brief" || len(toolsets) != 1 {
		t.Fatalf("instructions %q, %d toolsets", instructions, len(toolsets))
	}

	// A link standing in for instructions.md that points outside is refused.
	linked := t.TempDir()
	if err := os.Symlink(filepath.Join(outside, "secret"), filepath.Join(linked, "instructions.md")); err != nil {
		t.Fatal(err)
	}
	if got, _, err := loadPersona(context.Background(), linked, true); err == nil {
		t.Errorf("read instructions through a link outside the dir: %q", got)
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
