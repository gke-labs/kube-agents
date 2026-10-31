// Package render picks the persona adapter for a harness: the one seam the
// profile worker and `persona render` both go through, so a harness is
// added in one place.
package render

import (
	"fmt"

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
