package workeradapter

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

const examplePersona = "../lib/persona/testdata/example"

// personaWithManifest copies the example persona and records it the way an
// image build would: one sha256sum line per regular file but the manifest.
func personaWithManifest(t *testing.T) string {
	t.Helper()
	dst := t.TempDir()
	var lines []string
	err := filepath.WalkDir(examplePersona, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(examplePersona, p)
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
		sum := sha256.Sum256(data)
		lines = append(lines, hex.EncodeToString(sum[:])+"  ./"+filepath.ToSlash(rel))
		return os.WriteFile(target, data, info.Mode().Perm())
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(lines)
	if err := os.WriteFile(filepath.Join(dst, persona.IntegrityManifestFile), []byte(strings.Join(lines, "\n")+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return dst
}

func TestParsePersonaIntegrity(t *testing.T) {
	for v, want := range map[string]bool{"": false, lib.PersonaIntegrityRequired: true} {
		got, err := ParsePersonaIntegrity(v)
		if err != nil || got != want {
			t.Errorf("ParsePersonaIntegrity(%q) = %v, %v; want %v", v, got, err, want)
		}
	}
	// A misspelling stops the worker rather than leave the check off.
	for _, v := range []string{"Required", "true", "1", "optional", " required"} {
		if _, err := ParsePersonaIntegrity(v); err == nil {
			t.Errorf("ParsePersonaIntegrity(%q) accepted", v)
		}
	}
}

// A persona whose files don't match its build-time manifest stops the worker
// before it takes a task, and the error names the file.
func TestAPersonaThatDiffersFromItsManifestStopsTheWorker(t *testing.T) {
	dir := personaWithManifest(t)
	skill := filepath.Join(dir, "skills/triage/SKILL.md")
	data, err := os.ReadFile(skill)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(skill, append(data, []byte("\nchanged after the build\n")...), 0o644); err != nil {
		t.Fatal(err)
	}
	url := startServer(t)
	cfg := profileWorkerConfig(t, url, stub(t, "exit 0"))
	err = RunProfileWorker(context.Background(), cfg, ProfileWorkerConfig{
		Tier: pwTier, PersonaDir: dir, Harness: persona.ClaudeCode, ConfigRoot: t.TempDir(),
	})
	if err == nil || !strings.Contains(err.Error(), "skills/triage/SKILL.md") || !strings.Contains(err.Error(), persona.IntegrityManifestFile) {
		t.Errorf("a worker whose persona differs from its manifest = %v, want refused at start naming the file", err)
	}
}

// Under RequirePersonaIntegrity a persona with no manifest stops the worker.
func TestRequiredIntegrityRefusesAPersonaWithoutAManifest(t *testing.T) {
	url := startServer(t)
	cfg := profileWorkerConfig(t, url, stub(t, "exit 0"))
	err := RunProfileWorker(context.Background(), cfg, ProfileWorkerConfig{
		Tier: pwTier, PersonaDir: examplePersona, RequirePersonaIntegrity: true, Harness: persona.ClaudeCode, ConfigRoot: t.TempDir(),
	})
	if err == nil || !strings.Contains(err.Error(), persona.IntegrityManifestFile) {
		t.Errorf("a required-integrity worker with no manifest = %v, want refused at start", err)
	}
}

// Which way the persona loaded is in the pod's log: verified at info with
// the file count, unverified as a warning.
func TestPersonaIntegrityIsLogged(t *testing.T) {
	for _, tc := range []struct {
		dir   string
		level string
		want  string
	}{
		{dir: personaWithManifest(t), level: "level=INFO", want: "match their build-time manifest"},
		{dir: examplePersona, level: "level=WARN", want: "persona integrity unverified"},
	} {
		var buf bytes.Buffer
		log := slog.New(slog.NewTextHandler(&buf, nil))
		if _, err := renderPersona(ProfileWorkerConfig{PersonaDir: tc.dir, Harness: persona.ClaudeCode}, persona.Delegate{}, log); err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(buf.String(), tc.want) || !strings.Contains(buf.String(), tc.level) {
			t.Errorf("%s: log = %q, want %s %q", tc.dir, buf.String(), tc.level, tc.want)
		}
	}
}
