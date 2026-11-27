package workeradapter

import (
	"bytes"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/coreagent"
)

var discardLog = slog.New(slog.NewTextHandler(io.Discard, nil))

var renderRelay = persona.Relay{URL: "http://broker:8080/v1/mcp", Command: "/usr/local/bin/worker-adapter", Args: []string{MCPRelayCommand, "{url}"}}

// The cluster persona renders for core-agent into a sealed directory that
// coreagent.Snapshot reads, with a manifest beside it that verifies it, and
// nothing in it is writable.
func TestARenderedPersonaIsSealedAndReadable(t *testing.T) {
	out := filepath.Join(t.TempDir(), "persona")
	t.Cleanup(func() { unseal(t, out) })
	if err := RenderPersonaTo("../../agents/cluster/persona", out, persona.CoreAgent, renderRelay, false, discardLog); err != nil {
		t.Fatalf("render: %v", err)
	}
	err := filepath.WalkDir(out, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		if info.Mode().Perm()&0o222 != 0 {
			t.Errorf("%s is writable: %v", p, info.Mode())
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if integrity, _, err := persona.CheckTree(out, coreagent.Covers); err != nil || integrity != persona.IntegrityVerified {
		t.Errorf("the manifest beside the rendering: %v, %v", integrity, err)
	}
	r, err := coreagent.Snapshot(out)
	if err != nil || r.Instructions == "" {
		t.Fatalf("snapshot: %v", err)
	}
	// A script keeps its execute bit, for the shell to run it by its path.
	info, err := os.Stat(filepath.Join(out, "skills", "gke-stall-detection", "scripts", "stall_report.py"))
	if err != nil || info.Mode().Perm() != 0o555 {
		t.Errorf("script mode = %v, %v; want 0555", info, err)
	}
}

func TestARenderThatCantCompleteWritesNothing(t *testing.T) {
	for _, tc := range []struct {
		name  string
		h     persona.Harness
		relay persona.Relay
		want  string
	}{
		{"no relay for a granted server", persona.CoreAgent, persona.Relay{}, "no broker MCP relay"},
		{"an unknown harness", persona.Harness("nope"), renderRelay, "unknown harness"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			out := filepath.Join(t.TempDir(), "persona")
			err := RenderPersonaTo("../../agents/cluster/persona", out, tc.h, tc.relay, false, discardLog)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("err = %v, want %q", err, tc.want)
			}
			if _, err := os.Stat(out); !os.IsNotExist(err) {
				t.Errorf("a failed render left %s: %v", out, err)
			}
		})
	}
}

// A persona that differs from its build-time manifest is refused, as the
// worker refuses it.
// A persona that carries its build-time manifest renders with its source
// logged as verified, and a file changed after the manifest was written is
// refused even with integrity not required: the manifest alone catches it.
func TestARenderChecksThePersonasBuildTimeManifest(t *testing.T) {
	manifested := func(t *testing.T) string {
		in := filepath.Join(t.TempDir(), "persona")
		copyTree(t, "../../agents/cluster/persona", in)
		_ = os.Remove(filepath.Join(in, persona.IntegrityManifestFile))
		writeManifest(t, in)
		return in
	}
	t.Run("verified", func(t *testing.T) {
		var buf bytes.Buffer
		out := filepath.Join(t.TempDir(), "out")
		t.Cleanup(func() { unseal(t, filepath.Dir(out)) })
		if err := RenderPersonaTo(manifested(t), out, persona.CoreAgent, renderRelay, false, slog.New(slog.NewTextHandler(&buf, nil))); err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(buf.String(), `level=INFO msg="persona files match their build-time manifest"`) {
			t.Errorf("log = %q, want the persona logged as verified", buf.String())
		}
	})
	t.Run("unverified", func(t *testing.T) {
		in := filepath.Join(t.TempDir(), "persona")
		copyTree(t, "../../agents/cluster/persona", in)
		_ = os.Remove(filepath.Join(in, persona.IntegrityManifestFile))
		var buf bytes.Buffer
		out := filepath.Join(t.TempDir(), "out")
		t.Cleanup(func() { unseal(t, filepath.Dir(out)) })
		if err := RenderPersonaTo(in, out, persona.CoreAgent, renderRelay, false, slog.New(slog.NewTextHandler(&buf, nil))); err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(buf.String(), `level=WARN msg="persona integrity unverified`) {
			t.Errorf("log = %q, want the persona logged as unverified", buf.String())
		}
	})
	t.Run("tampered", func(t *testing.T) {
		in := manifested(t)
		if err := os.WriteFile(filepath.Join(in, "instructions.md"), []byte("changed\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		out := filepath.Join(t.TempDir(), "out")
		err := RenderPersonaTo(in, out, persona.CoreAgent, renderRelay, false, discardLog)
		if err == nil || !strings.Contains(err.Error(), "instructions.md") {
			t.Fatalf("a tampered persona: err = %v, want instructions.md named", err)
		}
		if _, statErr := os.Stat(out); !os.IsNotExist(statErr) {
			t.Errorf("a refused render left %s behind: %v", out, statErr)
		}
	})
}

// With integrity required, a persona without a manifest is refused.
func TestARenderRequiringIntegrityRefusesAPersonaWithoutAManifest(t *testing.T) {
	in := filepath.Join(t.TempDir(), "persona")
	copyTree(t, "../../agents/cluster/persona", in)
	_ = os.Remove(filepath.Join(in, persona.IntegrityManifestFile))
	if err := RenderPersonaTo(in, filepath.Join(t.TempDir(), "out"), persona.CoreAgent, renderRelay, true, discardLog); err == nil {
		t.Fatal("a persona without a manifest rendered with integrity required")
	}
}

// writeManifest writes dir's build-time manifest over every file in it.
func writeManifest(t *testing.T, dir string) {
	t.Helper()
	files := map[string][]byte{}
	err := filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		rel, _ := filepath.Rel(dir, p)
		data, err := os.ReadFile(p)
		files[filepath.ToSlash(rel)] = data
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, persona.IntegrityManifestFile), persona.FormatIntegrityManifest(files), 0o644); err != nil {
		t.Fatal(err)
	}
}

func copyTree(t *testing.T, from, to string) {
	t.Helper()
	err := filepath.WalkDir(from, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(from, p)
		dst := filepath.Join(to, rel)
		if d.IsDir() {
			return os.MkdirAll(dst, 0o755)
		}
		data, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		return os.WriteFile(dst, data, 0o644)
	})
	if err != nil {
		t.Fatal(err)
	}
}

// unseal makes a sealed rendering removable again, for the test's cleanup.
func unseal(t *testing.T, root string) {
	_ = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err == nil && d.IsDir() {
			_ = os.Chmod(p, 0o755)
		}
		return nil
	})
}

// A rerun in the same pod, as an init container started again, replaces what
// an earlier run left, sealed or half-written, with a whole sealed rendering.
func TestARenderRerunReplacesWhatAnEarlierRunLeft(t *testing.T) {
	parent := t.TempDir()
	out := filepath.Join(parent, "persona")
	t.Cleanup(func() { unseal(t, parent) })
	if err := RenderPersonaTo("../../agents/fleet-audit/persona", out, persona.CoreAgent, renderRelay, false, discardLog); err != nil {
		t.Fatal(err)
	}
	if err := RenderPersonaTo("../../agents/cluster/persona", out, persona.CoreAgent, renderRelay, false, discardLog); err != nil {
		t.Fatalf("a rerun over a sealed rendering: %v", err)
	}
	if _, err := os.Stat(filepath.Join(out, "skills", "gke-stall-detection")); err != nil {
		t.Errorf("the rerun's rendering isn't in place: %v", err)
	}
	// A half-written leftover (a writable directory with a stray file).
	unseal(t, out)
	if err := os.WriteFile(filepath.Join(out, "stray"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := RenderPersonaTo("../../agents/fleet-audit/persona", out, persona.CoreAgent, renderRelay, false, discardLog); err != nil {
		t.Fatalf("a rerun over a partial rendering: %v", err)
	}
	if _, err := os.Stat(filepath.Join(out, "stray")); !os.IsNotExist(err) {
		t.Errorf("the leftover's stray file survived: %v", err)
	}
	if entries, _ := os.ReadDir(parent); len(entries) != 1 {
		t.Errorf("the parent holds %d entries, want only the rendering", len(entries))
	}
	if info, err := os.Stat(out); err != nil || info.Mode().Perm() != 0o555 {
		t.Errorf("the rendering's top is %v, %v; want sealed 0555", info, err)
	}
}
