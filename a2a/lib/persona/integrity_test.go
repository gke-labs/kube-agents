package persona

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// writeIntegrityManifest records dir the way an image build does
// (`find . -type f ! -name skills_manifest.sha256 -exec sha256sum {} +`,
// sorted): every regular file but the root manifest, "./"-prefixed.
func writeIntegrityManifest(t *testing.T, dir string) {
	t.Helper()
	var lines []string
	err := filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || !d.Type().IsRegular() {
			return err
		}
		rel, _ := filepath.Rel(dir, p)
		rel = filepath.ToSlash(rel)
		if rel == IntegrityManifestFile {
			return nil
		}
		data, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		sum := sha256.Sum256(data)
		lines = append(lines, hex.EncodeToString(sum[:])+"  ./"+rel)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(lines)
	writeFile(t, dir, IntegrityManifestFile, strings.Join(lines, "\n")+"\n")
}

func writeFile(t *testing.T, dir, rel, content string) {
	t.Helper()
	p := filepath.Join(dir, rel)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// problems returns Load's problems, failing the test on any other outcome.
func problems(t *testing.T, dir string, opts Options) []Problem {
	t.Helper()
	_, err := LoadWithOptions(dir, opts)
	var inv *InvalidError
	if !errors.As(err, &inv) {
		t.Fatalf("Load = %v, want an *InvalidError", err)
	}
	return inv.Problems
}

func hasProblem(ps []Problem, file, msg string) bool {
	for _, p := range ps {
		if p.File == file && strings.Contains(p.Msg, msg) {
			return true
		}
	}
	return false
}

func TestIntegrityVerifiesAMatchingPersona(t *testing.T) {
	dir := copyExample(t)
	writeIntegrityManifest(t, dir)
	for _, opts := range []Options{{}, {RequireIntegrity: true}} {
		p, err := LoadWithOptions(dir, opts)
		if err != nil {
			t.Fatalf("%+v: %v", opts, err)
		}
		if p.Integrity != IntegrityVerified {
			t.Errorf("%+v: Integrity = %v, want verified", opts, p.Integrity)
		}
		// The example's every file: persona.yaml, instructions.md, mcp.yaml,
		// README.md and the skills' files. README.md is never rendered but
		// still covered.
		want := 0
		_ = filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
			if err == nil && !d.IsDir() && filepath.Base(p) != IntegrityManifestFile {
				want++
			}
			return nil
		})
		if p.IntegrityFiles != want {
			t.Errorf("%+v: IntegrityFiles = %d, want %d", opts, p.IntegrityFiles, want)
		}
	}
}

// A persona built without a manifest keeps loading: nothing it ships today
// carries one.
func TestIntegrityNoManifestLoadsUnverified(t *testing.T) {
	p, err := Load(exampleDir)
	if err != nil {
		t.Fatal(err)
	}
	if p.Integrity != IntegrityUnverified || p.IntegrityFiles != 0 {
		t.Errorf("Integrity = %v (%d files), want unverified (0)", p.Integrity, p.IntegrityFiles)
	}
}

func TestIntegrityNoManifestRefusedWhenRequired(t *testing.T) {
	ps := problems(t, copyExample(t), Options{RequireIntegrity: true})
	if len(ps) != 1 || !hasProblem(ps, IntegrityManifestFile, "is missing") {
		t.Errorf("problems = %v, want only the missing manifest", ps)
	}
}

func TestIntegrityRefuses(t *testing.T) {
	cases := []struct {
		name string
		// edit runs after the manifest is written.
		edit func(t *testing.T, dir string)
		file string
		msg  string
	}{
		{
			name: "a changed skill file",
			edit: func(t *testing.T, dir string) {
				replaceIn(t, dir, "skills/triage/SKILL.md", "description:", "description: changed,")
			},
			file: "skills/triage/SKILL.md", msg: "differs from",
		},
		{
			name: "a changed file that is never rendered",
			edit: func(t *testing.T, dir string) { writeFile(t, dir, ReadmeFile, "changed\n") },
			file: ReadmeFile, msg: "differs from",
		},
		{
			name: "a changed script",
			edit: func(t *testing.T, dir string) {
				writeFile(t, dir, "skills/triage/scripts/events.sh", "#!/bin/sh\necho changed\n")
			},
			file: "skills/triage/scripts/events.sh", msg: "differs from",
		},
		{
			name: "a missing file",
			edit: func(t *testing.T, dir string) {
				if err := os.Remove(filepath.Join(dir, "skills/triage/scripts/events.sh")); err != nil {
					t.Fatal(err)
				}
			},
			file: "skills/triage/scripts/events.sh", msg: "recorded in " + IntegrityManifestFile + " but missing",
		},
		{
			name: "an extra file",
			edit: func(t *testing.T, dir string) { writeFile(t, dir, "skills/notes/extra.md", "new\n") },
			file: "skills/notes/extra.md", msg: "not in " + IntegrityManifestFile,
		},
		{
			name: "a file named like the manifest below the root",
			edit: func(t *testing.T, dir string) {
				writeFile(t, dir, "skills/notes/"+IntegrityManifestFile, "deadbeef  ./x\n")
			},
			file: "skills/notes/" + IntegrityManifestFile, msg: "not in " + IntegrityManifestFile,
		},
		{
			name: "a malformed line",
			edit: func(t *testing.T, dir string) { appendTo(t, dir, IntegrityManifestFile, "nonsense\n") },
			file: IntegrityManifestFile, msg: "not a sha256sum line",
		},
		{
			name: "a digest that isn't SHA-256 hex",
			edit: func(t *testing.T, dir string) {
				appendTo(t, dir, IntegrityManifestFile, strings.Repeat("Z", 64)+"  ./x\n")
			},
			file: IntegrityManifestFile, msg: "not a lowercase hex SHA-256 digest",
		},
		{
			name: "a path that leaves the persona",
			edit: func(t *testing.T, dir string) {
				appendTo(t, dir, IntegrityManifestFile, strings.Repeat("0", 64)+"  ../outside\n")
			},
			file: IntegrityManifestFile, msg: "not a plain path inside the persona",
		},
		{
			name: "a repeated path",
			edit: func(t *testing.T, dir string) {
				data, err := os.ReadFile(filepath.Join(dir, IntegrityManifestFile))
				if err != nil {
					t.Fatal(err)
				}
				first, _, _ := strings.Cut(string(data), "\n")
				appendTo(t, dir, IntegrityManifestFile, first+"\n")
			},
			file: IntegrityManifestFile, msg: "listed twice",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir := copyExample(t)
			writeIntegrityManifest(t, dir)
			tc.edit(t, dir)
			// A present manifest is checked with or without RequireIntegrity.
			for _, opts := range []Options{{}, {RequireIntegrity: true}} {
				ps := problems(t, dir, opts)
				if !hasProblem(ps, tc.file, tc.msg) {
					t.Errorf("%+v: problems = %v, want %s: %q", opts, ps, tc.file, tc.msg)
				}
			}
		})
	}
}

func TestIntegrityReportsEveryDifferenceAtOnce(t *testing.T) {
	dir := copyExample(t)
	writeIntegrityManifest(t, dir)
	writeFile(t, dir, ReadmeFile, "changed\n")
	writeFile(t, dir, "skills/notes/extra.md", "new\n")
	if err := os.Remove(filepath.Join(dir, "skills/triage/scripts/events.sh")); err != nil {
		t.Fatal(err)
	}
	ps := problems(t, dir, Options{})
	for _, want := range []string{ReadmeFile, "skills/notes/extra.md", "skills/triage/scripts/events.sh"} {
		if !hasProblem(ps, want, "") {
			t.Errorf("problems = %v, missing one for %s", ps, want)
		}
	}
}

// The manifest is read the way sha256sum -c reads it: the binary-mode "*"
// marker, no "./" prefix, blank lines and comments are all accepted.
func TestIntegrityReadsSha256sumVariants(t *testing.T) {
	dir := copyExample(t)
	writeIntegrityManifest(t, dir)
	data, err := os.ReadFile(filepath.Join(dir, IntegrityManifestFile))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	for i, line := range lines {
		digest, name, _ := strings.Cut(line, "  ")
		switch i % 2 {
		case 0:
			lines[i] = digest + " *" + name
		case 1:
			lines[i] = digest + "  " + strings.TrimPrefix(name, "./")
		}
	}
	writeFile(t, dir, IntegrityManifestFile, "# built by test\n\n"+strings.Join(lines, "\n")+"\n")
	p, err := Load(dir)
	if err != nil {
		t.Fatal(err)
	}
	if p.Integrity != IntegrityVerified {
		t.Errorf("Integrity = %v, want verified", p.Integrity)
	}
}

func TestIntegrityRefusesASymlinkedManifest(t *testing.T) {
	dir := copyExample(t)
	writeIntegrityManifest(t, dir)
	real := filepath.Join(t.TempDir(), "manifest")
	if err := os.Rename(filepath.Join(dir, IntegrityManifestFile), real); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(real, filepath.Join(dir, IntegrityManifestFile)); err != nil {
		t.Fatal(err)
	}
	ps := problems(t, dir, Options{})
	if !hasProblem(ps, IntegrityManifestFile, "not a regular file") {
		t.Errorf("problems = %v, want the manifest refused as a symlink", ps)
	}
}

// The integrity walk refuses a symlink anywhere, not just where the format
// reader looks: a link into the persona from a skill's subdirectory.
func TestIntegrityRefusesASymlinkInTheTree(t *testing.T) {
	dir := copyExample(t)
	writeIntegrityManifest(t, dir)
	if err := os.Symlink("../SKILL.md", filepath.Join(dir, "skills/triage/scripts/link.md")); err != nil {
		t.Fatal(err)
	}
	ps := problems(t, dir, Options{})
	if !hasProblem(ps, "skills/triage/scripts/link.md", "symlink") {
		t.Errorf("problems = %v, want the link refused", ps)
	}
}

func appendTo(t *testing.T, dir, rel, content string) {
	t.Helper()
	f, err := os.OpenFile(filepath.Join(dir, rel), os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if _, err := fmt.Fprint(f, content); err != nil {
		t.Fatal(err)
	}
}

// A persona directory that is itself a symlink (a mount path pointing at
// the payload) is walked through, as the format reader reads through it.
func TestIntegrityFollowsASymlinkedPersonaRoot(t *testing.T) {
	dir := copyExample(t)
	writeIntegrityManifest(t, dir)
	link := filepath.Join(t.TempDir(), "persona")
	if err := os.Symlink(dir, link); err != nil {
		t.Fatal(err)
	}
	p, err := Load(link)
	if err != nil {
		t.Fatal(err)
	}
	if p.Integrity != IntegrityVerified {
		t.Errorf("Integrity = %v, want verified", p.Integrity)
	}
}

// sha256sum -c splits a line on the first run of blanks, tabs included.
func TestIntegrityAcceptsATabSeparatedLine(t *testing.T) {
	dir := copyExample(t)
	writeIntegrityManifest(t, dir)
	replaceIn(t, dir, IntegrityManifestFile, "  ./", "\t./")
	if p, err := Load(dir); err != nil || p.Integrity != IntegrityVerified {
		t.Fatalf("Load = %v, %v; want verified", p, err)
	}
}
