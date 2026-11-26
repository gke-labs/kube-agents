package coreagent

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"slices"
	"testing/fstest"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// maxSnapshotBytes bounds what Snapshot reads: a persona is prose and a few
// reference files, as the persona loader's own total cap says.
const maxSnapshotBytes = 16 << 20

// Rendering is a rendered persona read into memory once: the instructions,
// and the skills as a read-only file system rooted at the skills directory,
// in the layout the harness's skill loader takes (<name>/SKILL.md and the
// skill's files). Nothing reads the disk after Snapshot returns, so a later
// change to the directory doesn't reach the harness.
type Rendering struct {
	Instructions string
	Skills       fs.FS
}

// Snapshot reads a rendering from root, which holds only Names (an
// instructions file and, optionally, a skills directory of skill
// directories) and, beside them, the rendering's integrity manifest, which
// it skips. It refuses any other entry, a symlink or anything that isn't
// a regular file or a directory anywhere in it, a file directly under
// skills/, and a rendering over maxSnapshotBytes. Every file is opened
// through an os.Root, so nothing outside root is read even if an entry
// changes under the walk.
//
// It doesn't check who can write root: the persona directory's own check,
// run before the harness starts, refuses one the harness's user could write.
func Snapshot(root string) (Rendering, error) {
	r, err := os.OpenRoot(root)
	if err != nil {
		return Rendering{}, fmt.Errorf("the rendered persona at %s: %w", root, err)
	}
	defer r.Close()
	s := &snapshot{root: r, skills: fstest.MapFS{}}
	if err := s.read(); err != nil {
		return Rendering{}, fmt.Errorf("the rendered persona at %s: %w", root, err)
	}
	return Rendering{Instructions: string(s.instructions), Skills: readOnlyFS{s.skills}}, nil
}

type snapshot struct {
	root         *os.Root
	total        int64
	instructions []byte
	skills       fstest.MapFS
}

func (s *snapshot) read() error {
	entries, err := fs.ReadDir(s.root.FS(), ".")
	if err != nil {
		return err
	}
	for _, e := range entries {
		if e.Name() == persona.IntegrityManifestFile {
			// The rendering's build-time manifest, which the worker's session
			// check reads; not part of what the harness is given.
			continue
		}
		if !slices.Contains(Names, e.Name()) {
			return fmt.Errorf("it holds %q, which isn't one of %v", e.Name(), Names)
		}
	}
	if s.instructions, err = s.file(InstructionsFile); err != nil {
		return err
	}
	info, err := s.root.Lstat(SkillsDir)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return nil
	case err != nil:
		return err
	case !info.IsDir():
		return fmt.Errorf("%s is not a directory", SkillsDir)
	}
	return s.dir(SkillsDir, 0)
}

// dir reads a directory under skills/: at depth 0 the skills directory,
// which holds only skill directories, and below that a skill's files.
func (s *snapshot) dir(name string, depth int) error {
	entries, err := fs.ReadDir(s.root.FS(), name)
	if err != nil {
		return err
	}
	for _, e := range entries {
		p := path.Join(name, e.Name())
		info, err := s.root.Lstat(p)
		if err != nil {
			return err
		}
		switch {
		case info.IsDir():
			if err := s.dir(p, depth+1); err != nil {
				return err
			}
		case depth == 0:
			return fmt.Errorf("%s is not a skill directory", p)
		default:
			data, err := s.file(p)
			if err != nil {
				return err
			}
			mode := fs.FileMode(0o444)
			if info.Mode()&0o111 != 0 {
				mode = 0o555
			}
			s.skills[p[len(SkillsDir)+1:]] = &fstest.MapFile{Data: data, Mode: mode}
		}
	}
	return nil
}

// file reads one regular file through the root, refusing a symlink or a
// special file, and stops at the snapshot's byte cap whatever size the file
// claims.
func (s *snapshot) file(name string) ([]byte, error) {
	info, err := s.root.Lstat(name)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("%s is not a regular file", name)
	}
	f, err := s.root.Open(name)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	if opened, err := f.Stat(); err != nil || !opened.Mode().IsRegular() || !os.SameFile(info, opened) {
		return nil, fmt.Errorf("%s changed while it was read", name)
	}
	data, err := io.ReadAll(io.LimitReader(f, maxSnapshotBytes-s.total+1))
	if err != nil {
		return nil, err
	}
	s.total += int64(len(data))
	if s.total > maxSnapshotBytes {
		return nil, fmt.Errorf("the rendered persona is over %d bytes", maxSnapshotBytes)
	}
	return data, nil
}

// readOnlyFS serves a snapshot's skills without exposing the map behind
// them, so nothing that holds the FS can change it.
type readOnlyFS struct{ m fstest.MapFS }

func (r readOnlyFS) Open(name string) (fs.File, error)          { return r.m.Open(name) }
func (r readOnlyFS) ReadFile(name string) ([]byte, error)       { return r.m.ReadFile(name) }
func (r readOnlyFS) ReadDir(name string) ([]fs.DirEntry, error) { return r.m.ReadDir(name) }
func (r readOnlyFS) Stat(name string) (fs.FileInfo, error)      { return r.m.Stat(name) }
