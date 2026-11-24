package workeradapter

import (
	"bytes"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

func writeSessionHome(t *testing.T, withManifest bool) string {
	t.Helper()
	dir := t.TempDir()
	files := map[string][]byte{"CLAUDE.md": []byte("persona\n"), "skills/triage/SKILL.md": []byte("---\nname: triage\n---\n")}
	for rel, data := range files {
		p := filepath.Join(dir, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, data, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if withManifest {
		if err := os.WriteFile(filepath.Join(dir, persona.IntegrityManifestFile), persona.FormatIntegrityManifest(files), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// Another harness's persona is checked against that harness's scope: here
// one whose persona is GEMINI.md and skills. A changed file in the scope is
// refused.
func TestCheckSessionHomeUsesTheHarnessScope(t *testing.T) {
	agyCovers := func(rel string) bool {
		return rel == "GEMINI.md" || rel == "skills" || strings.HasPrefix(rel, "skills/")
	}
	files := map[string][]byte{"GEMINI.md": []byte("persona\n"), "skills/triage/SKILL.md": []byte("---\nname: triage\n---\n")}
	dir := t.TempDir()
	for rel, data := range files {
		p := filepath.Join(dir, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, data, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(dir, persona.IntegrityManifestFile), persona.FormatIntegrityManifest(files), 0o644); err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil))
	if err := CheckSessionHome(dir, agyCovers, log); err != nil {
		t.Fatalf("an unchanged persona: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "GEMINI.md"), []byte("changed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := CheckSessionHome(dir, agyCovers, log); err == nil || !strings.Contains(err.Error(), "GEMINI.md") {
		t.Errorf("a changed GEMINI.md: err = %v, want it named", err)
	}
}

func TestAMissingPersonaDirectoryIsLoggedAsMissing(t *testing.T) {
	var buf bytes.Buffer
	if err := CheckSessionHome(filepath.Join(t.TempDir(), "absent"), lib.SessionHomeCovers, slog.New(slog.NewTextHandler(&buf, nil))); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(buf.String(), "the persona directory is missing") {
		t.Errorf("log = %q, want the directory named missing", buf.String())
	}
}

func TestCheckSessionHome(t *testing.T) {
	for _, tc := range []struct {
		name     string
		manifest bool
		tamper   bool
		wantErr  bool
		wantLog  string
	}{
		{name: "verified", manifest: true, wantLog: "level=INFO msg=\"session skills match their build-time manifest\""},
		{name: "no manifest", wantLog: "level=WARN msg=\"session skills integrity unverified"},
		{name: "changed skill", manifest: true, tamper: true, wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := writeSessionHome(t, tc.manifest)
			if tc.tamper {
				if err := os.WriteFile(filepath.Join(dir, "skills/triage/SKILL.md"), []byte("changed\n"), 0o644); err != nil {
					t.Fatal(err)
				}
			}
			var buf bytes.Buffer
			err := CheckSessionHome(dir, lib.SessionHomeCovers, slog.New(slog.NewTextHandler(&buf, nil)))
			if tc.wantErr {
				if err == nil || !strings.Contains(err.Error(), "skills/triage/SKILL.md") {
					t.Errorf("err = %v, want the changed skill named", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(buf.String(), tc.wantLog) {
				t.Errorf("log = %q, want %q", buf.String(), tc.wantLog)
			}
		})
	}
}
