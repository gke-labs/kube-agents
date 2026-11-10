// Package render picks the persona adapter for a harness: the one seam the
// profile worker and `persona render` both go through, so a harness is
// added in one place.
package render

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/claudecode"
)

// For returns the adapter for h, the harness value the worker is started
// with.
// Antigravity is a known harness without an adapter yet: its agent file
// format is being settled, and refusing it by name is clearer than
// treating it as unknown.
func For(h persona.Harness) (persona.Adapter, error) {
	switch h {
	case persona.ClaudeCode:
		return claudecode.Adapter{}, nil
	case persona.Antigravity:
		return nil, fmt.Errorf("harness %q has no persona adapter yet", h)
	}
	return nil, fmt.Errorf("unknown harness %q (want %q or %q)", h, persona.ClaudeCode, persona.Antigravity)
}

// CheckOutput refuses an Output with a file path that is absolute, unclean
// or climbs out of the config directory. Every adapter's Output passes
// through it before anything is written.
func CheckOutput(out persona.Output) error {
	for _, f := range out.Files {
		if !persona.LocalPath(f.Path) {
			return fmt.Errorf("adapter output path %q leaves the config directory", f.Path)
		}
	}
	return nil
}

// File modes for WriteFiles.
const (
	writeDirMode  = 0o755
	writeFileMode = 0o644
	writeExecMode = 0o755
)

// WriteFiles puts a rendering's files under dir, which must be empty or
// absent: a stale file from an earlier rendering would load as part of this
// one. cmd/persona and the profile worker both write through it.
func WriteFiles(dir string, files []persona.File) error {
	if entries, err := os.ReadDir(dir); err == nil && len(entries) > 0 {
		return fmt.Errorf("%s is not empty", dir)
	}
	if err := os.MkdirAll(dir, writeDirMode); err != nil {
		return err
	}
	for _, f := range files {
		full := filepath.Join(dir, filepath.FromSlash(f.Path))
		if err := os.MkdirAll(filepath.Dir(full), writeDirMode); err != nil {
			return err
		}
		mode := os.FileMode(writeFileMode)
		if f.Executable {
			mode = writeExecMode
		}
		if err := os.WriteFile(full, f.Data, mode); err != nil {
			return err
		}
	}
	return nil
}
