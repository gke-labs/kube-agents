package main

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

const (
	// shippedPersonas are the persona directories the repository ships as
	// images, relative to this package.
	shippedPersonas = "../../../agents/*/persona"
	// personaDockerfiles are their images' Dockerfiles.
	personaDockerfiles = "../../Dockerfile.persona-*"
	// regenerate is what a failure tells the reader to run, from a2a/.
	regenerate = "go run ./cmd/persona manifest --in ../agents/<name>/persona"
)

// Every shipped persona carries a content manifest that matches it, so the
// image built from it (COPY . /files/) is verified by the profile worker at
// start. A persona edited without regenerating its manifest fails here, not
// in a pod.
func TestShippedPersonasCarryAMatchingManifest(t *testing.T) {
	dirs, err := filepath.Glob(shippedPersonas)
	if err != nil || len(dirs) == 0 {
		t.Fatalf("no shipped personas at %s: %v", shippedPersonas, err)
	}
	for _, dir := range dirs {
		p, err := persona.LoadWithOptions(dir, persona.Options{RequireIntegrity: true})
		if err != nil {
			t.Errorf("%s: %v\nregenerate its manifest: %s", dir, err, regenerate)
			continue
		}
		if p.Integrity != persona.IntegrityVerified {
			t.Errorf("%s: integrity %v, want verified", dir, p.Integrity)
		}
	}
}

// Each persona image copies its whole build context, the persona
// directory, to /files, and its dockerignore leaves out only .DS_Store
// files, which the persona format refuses anyway. So the image carries the
// committed manifest and exactly the files it describes.
func TestPersonaImagesShipTheCommittedManifest(t *testing.T) {
	dockerfiles, err := filepath.Glob(personaDockerfiles)
	if err != nil {
		t.Fatal(err)
	}
	var images int
	for _, df := range dockerfiles {
		if strings.HasSuffix(df, ".dockerignore") {
			continue
		}
		images++
		raw, err := os.ReadFile(df)
		if err != nil {
			t.Fatal(err)
		}
		if !regexp.MustCompile(`(?m)^COPY \. /files/$`).Match(raw) {
			t.Errorf("%s does not copy its whole context to /files/", df)
		}
		ignore, err := os.ReadFile(df + ".dockerignore")
		if err != nil {
			t.Fatalf("%s has no dockerignore: %v", df, err)
		}
		for _, line := range strings.Split(string(ignore), "\n") {
			line = strings.TrimSpace(line)
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			if line != ".DS_Store" && line != "**/.DS_Store" {
				t.Errorf("%s.dockerignore leaves out %q, which the committed manifest would still list", df, line)
			}
		}
	}
	dirs, _ := filepath.Glob(shippedPersonas)
	if images != len(dirs) {
		t.Errorf("%d persona Dockerfiles for %d shipped personas (%v)", images, len(dirs), dirs)
	}
}

// copyDir copies a persona directory for a test to change.
func copyDir(t *testing.T, src string) string {
	t.Helper()
	dst := t.TempDir()
	err := filepath.WalkDir(src, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, p)
		if d.IsDir() {
			return os.MkdirAll(filepath.Join(dst, rel), 0o755)
		}
		data, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		return os.WriteFile(filepath.Join(dst, rel), data, 0o644)
	})
	if err != nil {
		t.Fatal(err)
	}
	return dst
}

// manifest writes a manifest that validate --require-integrity accepts,
// replaces a stale one, and refuses a tree holding a symlink.
func TestManifestCommand(t *testing.T) {
	dir := copyDir(t, "../../lib/persona/testdata/example")
	var out, errb strings.Builder
	if code := run([]string{"validate", "--require-integrity", "--in", dir}, &out, &errb); code != 1 {
		t.Fatalf("validate --require-integrity without a manifest = %d, want 1 (%s)", code, errb.String())
	}
	if code := run([]string{"manifest", "--in", dir}, &out, &errb); code != 0 {
		t.Fatalf("manifest = %d: %s", code, errb.String())
	}
	if code := run([]string{"validate", "--require-integrity", "--in", dir}, &out, &errb); code != 0 {
		t.Fatalf("validate after manifest = %d: %s", code, errb.String())
	}
	if err := os.WriteFile(filepath.Join(dir, "README.md"), []byte("changed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if code := run([]string{"validate", "--in", dir}, &out, &errb); code != 1 {
		t.Fatalf("validate of a changed persona = %d, want 1", code)
	}
	if code := run([]string{"manifest", "--in", dir}, &out, &errb); code != 0 {
		t.Fatalf("manifest over a stale one = %d: %s", code, errb.String())
	}
	if err := os.Symlink("../SKILL.md", filepath.Join(dir, "skills/triage/scripts/link.md")); err != nil {
		t.Fatal(err)
	}
	errb.Reset()
	if code := run([]string{"manifest", "--in", dir}, &out, &errb); code != 1 || !strings.Contains(errb.String(), "not a regular file") {
		t.Errorf("manifest over a symlink = %d (%s), want 1 naming it", code, errb.String())
	}
}
