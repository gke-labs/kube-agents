package workeradapter

import (
	"bytes"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

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
			err := CheckSessionHome(dir, slog.New(slog.NewTextHandler(&buf, nil)))
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
