package isolation

import (
	"fmt"
	"io/fs"
	"os"
	"slices"
	"strings"
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
//
// The path to dir is walked as given, and everything in it is read through
// one handle on the directory the kernel resolved (os.OpenRoot): no step
// rebuilds a path from dir's text, so ".." after a link can't send the
// check to one tree and the kernel to another.
func CheckPersonaDir(dir string, uid, gid int) error {
	root, err := openPersona(dir, uid, gid)
	if err != nil {
		return err
	}
	defer root.Close()
	return checkPersonaTree(root, dir, uid, gid)
}

// openPersona checks the path to dir and dir itself, then opens it.
func openPersona(dir string, uid, gid int) (*os.Root, error) {
	if err := checkRoot("persona dir", dir); err != nil {
		return nil, err
	}
	// The path to it first: a writable ancestor could swap the directory
	// for another after this check.
	if last, err := walkChecked(dir, uid, gid); err != nil { // raw: ".." resolves against the real path
		return nil, fmt.Errorf("isolation: persona dir %s: %w", dir, err)
	} else if last == nil {
		return nil, fmt.Errorf("isolation: persona dir %s does not exist", dir)
	}
	// lstat follows a link written with a trailing "/" or "/.", so those
	// are dropped first: a link to a directory is not a real directory
	// however it is spelled.
	fi, err := os.Lstat(withoutTrailingSlashDots(dir))
	if err != nil {
		return nil, fmt.Errorf("isolation: persona dir: %w", err)
	}
	if !fi.IsDir() {
		return nil, fmt.Errorf("isolation: persona dir %s is not a directory", dir)
	}
	if writableBy(fi, uid, gid) {
		return nil, fmt.Errorf("isolation: persona dir %s is writable by uid %d", dir, uid)
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		return nil, fmt.Errorf("isolation: persona dir: %w", err)
	}
	return root, nil
}

// withoutTrailingSlashDots is p without trailing "/" and "/." components,
// leaving "/" itself.
func withoutTrailingSlashDots(p string) string {
	for {
		switch {
		case len(p) > 1 && strings.HasSuffix(p, "/"):
			p = p[:len(p)-1]
		case len(p) > 2 && strings.HasSuffix(p, "/."):
			p = p[:len(p)-2]
		default:
			return p
		}
	}
}

// checkPersonaTree checks everything in the opened persona directory.
func checkPersonaTree(root *os.Root, dir string, uid, gid int) error {
	ents, err := fs.ReadDir(root.FS(), ".")
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
	return fs.WalkDir(root.FS(), ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return fmt.Errorf("isolation: persona dir: %w", err)
		}
		info, err := root.Lstat(p)
		if err != nil {
			return fmt.Errorf("isolation: persona dir: %w", err)
		}
		if t := info.Mode().Type(); t != 0 && t != fs.ModeDir {
			return fmt.Errorf("isolation: persona entry %s/%s is neither a directory nor a regular file", dir, p)
		}
		if writableBy(info, uid, gid) {
			return fmt.Errorf("isolation: persona entry %s/%s is writable by uid %d", dir, p, uid)
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

// LoadPersona checks dir as CheckPersonaDir does and reads every file in it
// into memory through the same handle the check used (os.OpenRoot, which
// also refuses to follow a link out of it), so the harness loads exactly
// the tree that was checked, once, and later writes to the directory change
// nothing. The result maps a slash-separated path under dir to its bytes.
func LoadPersona(dir string, uid, gid int) (map[string][]byte, error) {
	root, err := openPersona(dir, uid, gid)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	if err := checkPersonaTree(root, dir, uid, gid); err != nil {
		return nil, err
	}
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
