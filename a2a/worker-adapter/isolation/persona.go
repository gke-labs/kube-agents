package isolation

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"syscall"
)

// PersonaNames are the only names a persona directory may hold: the rules
// files and the skills directory a persona renders. Nothing that configures
// behaviour beyond the model's context (hooks, MCP servers, plugins,
// settings) may appear there.
var PersonaNames = []string{"AGENTS.md", "GEMINI.md", "skills"}

// CheckPersonaDir verifies the directory a persona is rendered into before
// a turn: it exists, is a real directory, holds nothing but
// PersonaNames (and, under skills, nothing that is not a directory or a
// regular file), and neither it nor anything in it is writable by uid, the
// harness's user. Another user renders it; a harness that could write it
// would configure its own next turn. It fails closed on anything else.
func CheckPersonaDir(dir string, uid, gid int) error {
	if err := checkRoot("persona dir", dir); err != nil {
		return err
	}
	fi, err := os.Lstat(dir)
	if err != nil {
		return fmt.Errorf("isolation: persona dir: %w", err)
	}
	if !fi.IsDir() {
		return fmt.Errorf("isolation: persona dir %s is not a directory", dir)
	}
	if writableBy(fi, uid, gid) {
		return fmt.Errorf("isolation: persona dir %s is writable by uid %d", dir, uid)
	}
	ents, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("isolation: persona dir: %w", err)
	}
	for _, e := range ents {
		if !slices.Contains(PersonaNames, e.Name()) {
			return fmt.Errorf("isolation: persona dir %s holds %q, which is not a persona entry", dir, e.Name())
		}
	}
	return filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return fmt.Errorf("isolation: persona dir: %w", err)
		}
		info, err := d.Info()
		if err != nil {
			return fmt.Errorf("isolation: persona dir: %w", err)
		}
		if t := info.Mode().Type(); t != 0 && t != fs.ModeDir {
			return fmt.Errorf("isolation: persona entry %s is neither a directory nor a regular file", p)
		}
		if writableBy(info, uid, gid) {
			return fmt.Errorf("isolation: persona entry %s is writable by uid %d", p, uid)
		}
		return nil
	})
}

// writableBy reports whether uid could write fi, failing closed: an entry
// uid owns is writable whatever its mode, since its owner can chmod it, and
// so is any entry with a group or other write bit, since uid's supplementary
// groups (an fsGroup among them) are not known here. gid is accepted for
// callers' symmetry and not trusted to narrow the answer. Root is not
// considered: the harness never runs as root. ACLs are not read; the image
// and the pod spec set none.
func writableBy(fi fs.FileInfo, uid, _ int) bool {
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		return true // unknown ownership: fail closed
	}
	if int(st.Uid) == uid {
		return true
	}
	return fi.Mode().Perm()&0o022 != 0
}
