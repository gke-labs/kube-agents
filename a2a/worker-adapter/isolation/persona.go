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
	// The path to it first: a writable ancestor could swap the directory
	// for another after this check.
	if last, err := walkChecked(dir, uid, gid); err != nil { // raw: ".." resolves against the real path
		return fmt.Errorf("isolation: persona dir %s: %w", dir, err)
	} else if last == nil {
		return fmt.Errorf("isolation: persona dir %s does not exist", dir)
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
		// skills is a directory of skills; the rules files are files.
		wantDir := e.Name() == "skills"
		if e.Type().IsDir() != wantDir || (!wantDir && !e.Type().IsRegular()) {
			return fmt.Errorf("isolation: persona entry %s/%s is not a %s", dir, e.Name(), map[bool]string{true: "directory", false: "regular file"}[wantDir])
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

// LoadPersona checks dir (CheckPersonaDir) and reads every file in it into
// memory through one handle on that directory (os.OpenRoot, which also
// refuses to follow a link out of it), so the harness loads exactly the
// tree that was checked, once, and later writes to the directory change
// nothing. The result maps a slash-separated path under dir to its bytes.
func LoadPersona(dir string, uid, gid int) (map[string][]byte, error) {
	if err := CheckPersonaDir(dir, uid, gid); err != nil {
		return nil, err
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		return nil, fmt.Errorf("isolation: persona dir: %w", err)
	}
	defer root.Close()
	files := map[string][]byte{}
	err = fs.WalkDir(root.FS(), ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		if !d.Type().IsRegular() {
			return fmt.Errorf("persona entry %s is not a regular file", p)
		}
		b, err := fs.ReadFile(root.FS(), p)
		if err != nil {
			return err
		}
		files[p] = b
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("isolation: persona dir: %w", err)
	}
	return files, nil
}
