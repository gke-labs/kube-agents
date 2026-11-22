// Package isolation removes the Antigravity CLI's (agy's) configuration
// entries from the writable directories it reads configuration from: the
// workspace and its per-conversation directories under HOME (#6710). The
// rest of agy's HOME configuration is not scrubbed here; it is meant to be
// on a read-only layout baked into the image. Claude Code needs none of
// this: the adapter's argv limits its setting sources instead.
//
// The entries are agy's discovery paths for one exact version, kept in a
// table. An unknown version is refused rather than handled with another
// version's list.
//
// This package only removes and creates entries. The harness driver calls
// PrepareTurn before each spawn, and only while no harness process or
// anything it started is still running: nothing here guards against a
// concurrent writer replacing an entry or a directory mid-scrub.
package isolation

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// Kind is what agy expects to find under an entry's name.
type Kind int

const (
	// File is an entry agy reads as a file, such as AGENTS.md.
	File Kind = iota
	// Dir is an entry agy reads as a directory, such as .agents.
	Dir
)

// Entry is one name the harness looks up in a configuration directory.
type Entry struct {
	Name string
	Kind Kind
	// Placeholder says an inert entry of the other kind may be left under
	// this name: an empty, read-only directory where agy expects a file,
	// or an empty, read-only file where it expects a directory. Set only
	// where agy was shown to run normally with one.
	Placeholder bool
}

// Table is the set of customization-root entries for one agy version.
type Table struct {
	Version string
	Entries []Entry
}

// ErrUnknownVersion is returned for an agy version with no table.
var ErrUnknownVersion = errors.New("no isolation table for this agy version")

// tables holds one row per agy version. Adding a version means re-running
// the discovery probes against it and the harness-isolation canary suite.
var tables = map[string]Table{
	"1.5.13": {
		Version: "1.5.13",
		Entries: []Entry{
			{Name: ".agents", Kind: Dir, Placeholder: true},
			{Name: ".agent", Kind: Dir, Placeholder: true},
			{Name: "_agents", Kind: Dir, Placeholder: true},
			{Name: "_agent", Kind: Dir, Placeholder: true},
			{Name: "AGENTS.md", Kind: File, Placeholder: true},
			{Name: "GEMINI.md", Kind: File, Placeholder: true},
			{Name: ".gemini", Kind: Dir, Placeholder: true},
			// No placeholder: agy has not been run with one here.
			{Name: ".codeium.yaml", Kind: File},
			{Name: ".codeium.yml", Kind: File},
			{Name: "codeium.yaml", Kind: File},
			{Name: "codeium.yml", Kind: File},
		},
	},
}

// ForVersion returns the table for the output of `agy --version`. Only an
// exact version match counts.
func ForVersion(versionOutput string) (Table, error) {
	v := parseVersion(versionOutput)
	if v == "" {
		if line := firstLine(versionOutput); line != "" {
			return Table{}, fmt.Errorf("%w: unrecognised version line %q", ErrUnknownVersion, line)
		}
		return Table{}, fmt.Errorf("%w: empty version output", ErrUnknownVersion)
	}
	t, ok := tables[v]
	if !ok {
		return Table{}, fmt.Errorf("%w: %q", ErrUnknownVersion, v)
	}
	return t, nil
}

// firstLine is the first non-empty line of out, trimmed.
func firstLine(out string) string {
	for _, line := range strings.Split(out, "\n") {
		if l := strings.TrimSpace(line); l != "" {
			return l
		}
	}
	return ""
}

// parseVersion reads the first non-empty line, which must be exactly
// "<version>" or "agy <version>". Anything else gives "", which no table
// matches.
func parseVersion(out string) string {
	for _, line := range strings.Split(out, "\n") {
		f := strings.Fields(line)
		switch {
		case len(f) == 0:
			continue
		case len(f) == 1:
			return f[0]
		case len(f) == 2 && f[0] == "agy":
			return f[1]
		}
		return ""
	}
	return ""
}

// BrainDir holds agy's per-conversation directories, relative to HOME. Each
// is also a configuration directory.
const BrainDir = ".gemini/antigravity-cli/brain"

// PrepareTurn readies a turn's configuration directories: it scrubs and
// places placeholders in the workspace, then scrubs every conversation
// directory under home's BrainDir. It fails closed: an empty or relative
// workspace or home is an error, as is a home at or under the workspace, a
// symlink among BrainDir's parents, and any entry it cannot remove. Removed
// paths are returned for the caller to log.
func (t Table) PrepareTurn(workspace, home string) ([]string, error) {
	if err := checkRoot("workspace", workspace); err != nil {
		return nil, err
	}
	if err := checkRoot("home", home); err != nil {
		return nil, err
	}
	if rel, err := filepath.Rel(filepath.Clean(workspace), filepath.Clean(home)); err == nil && (rel == "." || !strings.HasPrefix(rel, "..")) {
		return nil, fmt.Errorf("isolation: home %q is at or under the workspace %q", home, workspace)
	}
	brain, err := brainDir(home)
	if err != nil {
		return nil, err
	}
	removed, err := t.Scrub(workspace)
	if err != nil {
		return removed, err
	}
	if err := t.Place(workspace); err != nil {
		return removed, err
	}
	r, err := t.scrubBrain(brain)
	return append(removed, r...), err
}

// brainDir returns home's BrainDir after checking that no directory between
// home and it is a symlink, so a scrub there cannot land elsewhere. BrainDir
// itself may be one: the read-only HOME layout links it to writable storage.
func brainDir(home string) (string, error) {
	parts := strings.Split(BrainDir, "/")
	p := home
	for _, part := range parts[:len(parts)-1] {
		p = filepath.Join(p, part)
		fi, err := os.Lstat(p)
		if errors.Is(err, fs.ErrNotExist) {
			return filepath.Join(home, BrainDir), nil
		}
		if err != nil {
			return "", fmt.Errorf("isolation: %w", err)
		}
		if !fi.IsDir() {
			return "", fmt.Errorf("isolation: %s is not a directory", p)
		}
	}
	return filepath.Join(home, BrainDir), nil
}

func checkRoot(what, dir string) error {
	if dir == "" {
		return fmt.Errorf("isolation: %s is empty", what)
	}
	if !filepath.IsAbs(dir) {
		return fmt.Errorf("isolation: %s %q is not absolute", what, dir)
	}
	return nil
}

// Scrub removes every table entry in root that is not an inert placeholder,
// and returns the removed paths. It never follows a symlink: a link under
// an entry's name is removed, not its target, and a root that is itself a
// symlink is refused (callers pass resolved paths).
func (t Table) Scrub(root string) ([]string, error) {
	if err := checkRoot("root", root); err != nil {
		return nil, err
	}
	fi, err := os.Lstat(root)
	if err != nil {
		return nil, fmt.Errorf("isolation: %w", err)
	}
	if fi.Mode()&fs.ModeSymlink != 0 {
		return nil, fmt.Errorf("isolation: %s is a symlink", root)
	}
	if !fi.IsDir() {
		return nil, fmt.Errorf("isolation: %s is not a directory", root)
	}
	var removed []string
	for _, e := range t.Entries {
		p := filepath.Join(root, e.Name)
		fi, err := os.Lstat(p)
		if errors.Is(err, fs.ErrNotExist) {
			continue
		}
		if err != nil {
			return removed, fmt.Errorf("isolation: %w", err)
		}
		if e.Placeholder && isPlaceholder(p, fi, e.Kind) {
			continue
		}
		if err := removeAll(p); err != nil {
			return removed, fmt.Errorf("isolation: remove %s: %w", p, err)
		}
		removed = append(removed, p)
	}
	return removed, nil
}

// removeAll removes p and everything under it, first restoring the owner
// write bit on directories under p, so a read-only tree there cannot make
// the removal fail. A read-only root (the directory holding p) still fails
// it, which fails the turn closed. It never follows symlinks.
func removeAll(p string) error {
	_ = filepath.WalkDir(p, func(q string, d fs.DirEntry, err error) error {
		if err == nil && d.IsDir() {
			if fi, err := d.Info(); err == nil {
				_ = os.Chmod(q, fi.Mode().Perm()|0o700) // #nosec G122 G302 -- restores the owner bits on a tree about to be removed; PrepareTurn runs with no harness process alive, so nothing swaps entries mid-walk
			}
		}
		return nil
	})
	return os.RemoveAll(p)
}

// Place creates a placeholder for every missing table entry that allows
// one. An existing entry is left alone; Scrub runs first.
func (t Table) Place(root string) error {
	if err := checkRoot("root", root); err != nil {
		return err
	}
	for _, e := range t.Entries {
		if !e.Placeholder {
			continue
		}
		p := filepath.Join(root, e.Name)
		if _, err := os.Lstat(p); err == nil {
			continue
		} else if !errors.Is(err, fs.ErrNotExist) {
			return fmt.Errorf("isolation: %w", err)
		}
		if err := makePlaceholder(p, e.Kind); err != nil {
			return fmt.Errorf("isolation: placeholder %s: %w", p, err)
		}
	}
	return nil
}

// makePlaceholder leaves the opposite kind of entry, empty and read-only.
func makePlaceholder(p string, k Kind) error {
	if k == File {
		if err := os.Mkdir(p, 0o555); err != nil { // #nosec G301 -- a read-only placeholder; 0555 is the mode the harness was shown to run with
			return err
		}
		return os.Chmod(p, 0o555) // #nosec G302 -- umask-independent read-only placeholder, as above
	}
	f, err := os.OpenFile(p, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o444) // #nosec G302 G304 -- an empty read-only placeholder created (O_EXCL) at a table entry's name in the scrubbed root
	if err != nil {
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Chmod(p, 0o444) // #nosec G302 -- umask-independent read-only placeholder, as above
}

// isPlaceholder reports whether fi (from Lstat) is an inert stand-in for an
// entry of kind k: empty, of the other kind, with no write bits. Who owns it
// does not matter, only that agy can load nothing from it now.
func isPlaceholder(p string, fi fs.FileInfo, k Kind) bool {
	if fi.Mode().Perm()&0o222 != 0 {
		return false
	}
	switch k {
	case File:
		return fi.IsDir() && emptyDir(p)
	default:
		return fi.Mode().IsRegular() && fi.Size() == 0
	}
}

func emptyDir(p string) bool {
	f, err := os.Open(p) // #nosec G304 -- only lists a table entry's directory in the scrubbed root to see whether it is empty
	if err != nil {
		return false
	}
	defer f.Close()
	_, err = f.Readdirnames(1)
	return errors.Is(err, io.EOF)
}

// scrubBrain scrubs each conversation directory under brain. agy creates
// only directories there, so a symlink is removed rather than followed. A
// missing brain directory means no conversation has run yet.
func (t Table) scrubBrain(brain string) ([]string, error) {
	ents, err := os.ReadDir(brain)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("isolation: %w", err)
	}
	sort.Slice(ents, func(i, j int) bool { return ents[i].Name() < ents[j].Name() })
	var removed []string
	for _, d := range ents {
		p := filepath.Join(brain, d.Name())
		switch {
		case d.Type()&fs.ModeSymlink != 0:
			if err := os.Remove(p); err != nil {
				return removed, fmt.Errorf("isolation: remove %s: %w", p, err)
			}
			removed = append(removed, p)
		case d.IsDir():
			r, err := t.Scrub(p)
			removed = append(removed, r...)
			if err != nil {
				return removed, err
			}
		}
	}
	return removed, nil
}
