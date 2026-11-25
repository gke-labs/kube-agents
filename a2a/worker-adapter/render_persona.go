package workeradapter

import (
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
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
// for h with relay as the MCP relay target, and puts the rendering at out
// with a content manifest beside it, sealed: every directory 0555 and every
// file 0444 (0555 for a script). The worker reads it through a read-only
// mount; the modes are so that nothing in it is writable whatever mounts it.
//
// It renders into a new directory beside out and renames it into place, so
// out is either the whole sealed rendering or absent, and a rerun in the same
// pod (an init container started again) replaces what an earlier run left.
// It logs whether the persona's files matched its build-time manifest.
func RenderPersonaTo(in, out string, h persona.Harness, relay persona.Relay, requireIntegrity bool, log *slog.Logger) error {
	p, err := persona.LoadWithOptions(in, persona.Options{RequireIntegrity: requireIntegrity})
	if err != nil {
		return fmt.Errorf("the persona at %s: %w", in, err)
	}
	// The same line the profile worker logs for a persona it loads itself,
	// so a rendered persona's source says verified or unverified too.
	logPersonaIntegrity(log, in, p)
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
	if err := os.MkdirAll(filepath.Dir(out), 0o755); err != nil { // #nosec G301 -- the parent the rendering is renamed into
		return err
	}
	tmp, err := os.MkdirTemp(filepath.Dir(out), "."+filepath.Base(out)+"-")
	if err != nil {
		return err
	}
	done := false
	defer func() {
		if !done {
			_ = removeSealed(tmp)
		}
	}()
	dir := filepath.Join(tmp, "r")
	if err := render.WriteFiles(dir, output.Files); err != nil {
		return err
	}
	files := map[string][]byte{}
	for _, f := range output.Files {
		files[f.Path] = f.Data
	}
	manifest := filepath.Join(dir, persona.IntegrityManifestFile)
	if err := os.WriteFile(manifest, persona.FormatIntegrityManifest(files), sealedFileMode); err != nil { // #nosec G306 -- read-only, and readable by the harness user by design
		return err
	}
	if err := seal(dir, output.Files); err != nil {
		return err
	}
	// What an earlier run left goes first: a sealed rendering, or a part of one.
	if err := removeSealed(out); err != nil {
		return err
	}
	// A directory moves to another parent only while it is writable itself
	// (its ".." changes), so the top is sealed once it is in place.
	if err := os.Chmod(dir, 0o700); err != nil { // #nosec G302 -- sealed below, once renamed
		return err
	}
	if err := os.Rename(dir, out); err != nil {
		return err
	}
	done = true
	if err := os.Chmod(out, sealedDirMode); err != nil {
		return err
	}
	return os.Remove(tmp)
}

// removeSealed removes root, a rendering this user wrote and may have
// sealed, making its directories writable again first. An absent root is
// nothing to remove.
func removeSealed(root string) error {
	if _, err := os.Lstat(root); errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	_ = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err == nil && d.IsDir() {
			_ = os.Chmod(p, 0o700) // #nosec G302 -- the directory is about to be removed
		}
		return nil
	})
	return os.RemoveAll(root)
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
