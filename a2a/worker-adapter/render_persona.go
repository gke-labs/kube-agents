package workeradapter

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/render"
)

// RenderPersonaCommand is the worker-adapter subcommand that renders a
// persona into a directory and seals it, run by an init container before the
// worker starts, as a user the harness doesn't run as.
const RenderPersonaCommand = "render-persona"

// Modes a sealed rendering has: nothing in it is writable by anyone.
const (
	sealedDirMode  = 0o555
	sealedFileMode = 0o444
	sealedExecMode = 0o555
)

// RenderPersonaTo loads the persona at in (refusing one that differs from
// its build-time manifest, or has none when requireIntegrity), renders it
// for h with relay as the MCP relay target, writes the rendering into out
// (which must be empty or absent) with a content manifest beside it, and
// seals it: every directory 0555 and every file 0444 (0555 for a script).
// The worker reads the rendering through a read-only mount; the modes are so
// that nothing in it is writable whatever mounts it.
func RenderPersonaTo(in, out string, h persona.Harness, relay persona.Relay, requireIntegrity bool) error {
	p, err := persona.LoadWithOptions(in, persona.Options{RequireIntegrity: requireIntegrity})
	if err != nil {
		return fmt.Errorf("the persona at %s: %w", in, err)
	}
	adapter, err := render.For(h)
	if err != nil {
		return err
	}
	output, err := adapter.Render(p, persona.Target{Relay: relay})
	if err != nil {
		return fmt.Errorf("rendering the persona: %w", err)
	}
	if err := render.CheckOutput(output); err != nil {
		return err
	}
	if err := render.WriteFiles(out, output.Files); err != nil {
		return err
	}
	files := map[string][]byte{}
	for _, f := range output.Files {
		files[f.Path] = f.Data
	}
	manifest := filepath.Join(out, persona.IntegrityManifestFile)
	if err := os.WriteFile(manifest, persona.FormatIntegrityManifest(files), sealedFileMode); err != nil { // #nosec G306 -- read-only, and readable by the harness user by design
		return err
	}
	return seal(out, output.Files)
}

// seal makes every file under root read-only (a script keeps its execute
// bits) and then every directory, deepest first.
func seal(root string, files []persona.File) error {
	exec := map[string]bool{}
	for _, f := range files {
		exec[filepath.FromSlash(f.Path)] = f.Executable
	}
	var dirs []string
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			dirs = append(dirs, p)
			return nil
		}
		rel, err := filepath.Rel(root, p)
		if err != nil {
			return err
		}
		mode := fs.FileMode(sealedFileMode)
		if exec[rel] {
			mode = sealedExecMode
		}
		return os.Chmod(p, mode)
	})
	if err != nil {
		return err
	}
	slices.Reverse(dirs)
	for _, d := range dirs {
		if err := os.Chmod(d, sealedDirMode); err != nil {
			return err
		}
	}
	return nil
}
