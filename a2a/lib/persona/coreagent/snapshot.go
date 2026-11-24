package coreagent

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"slices"
	"strings"
	"testing/fstest"
)

// maxSnapshotBytes bounds what Snapshot reads: a persona is prose and a few
// reference files, as the persona loader's own total cap says.
const maxSnapshotBytes = 16 << 20

// Rendering is a rendered persona read into memory once: the instructions,
// and the skills as a file system rooted at the skills directory, in the
// layout the harness's skill loader takes (<name>/SKILL.md and the skill's
// files). Nothing reads the disk after Snapshot returns, so a later change
// to the directory doesn't reach the harness.
type Rendering struct {
	Instructions string
	Skills       fs.FS
}

// Snapshot reads a rendering from root, which holds only Names. It refuses
// any other top-level entry, a symlink or any file that isn't regular or a
// directory anywhere in it, and a rendering over maxSnapshotBytes. A missing
// skills directory is an empty one; a missing instructions file is an error.
func Snapshot(root string) (Rendering, error) {
	rootFS := os.DirFS(root)
	entries, err := fs.ReadDir(rootFS, ".")
	if err != nil {
		return Rendering{}, fmt.Errorf("the rendered persona at %s: %w", root, err)
	}
	for _, e := range entries {
		if !slices.Contains(Names, e.Name()) {
			return Rendering{}, fmt.Errorf("the rendered persona at %s holds %q, which isn't one of %v", root, e.Name(), Names)
		}
		if e.Type()&fs.ModeSymlink != 0 {
			return Rendering{}, fmt.Errorf("the rendered persona at %s: %s is a symlink", root, e.Name())
		}
	}
	total := 0
	read := func(name string) ([]byte, error) {
		info, err := fs.Stat(rootFS, name)
		if err != nil {
			return nil, err
		}
		if !info.Mode().IsRegular() {
			return nil, fmt.Errorf("%s is not a regular file", name)
		}
		total += int(info.Size())
		if total > maxSnapshotBytes {
			return nil, fmt.Errorf("the rendered persona is over %d bytes", maxSnapshotBytes)
		}
		return fs.ReadFile(rootFS, name)
	}
	instructions, err := read(InstructionsFile)
	if err != nil {
		return Rendering{}, fmt.Errorf("the rendered persona at %s: %w", root, err)
	}
	skills := fstest.MapFS{}
	err = fs.WalkDir(rootFS, SkillsDir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			if p == SkillsDir && errors.Is(err, fs.ErrNotExist) {
				return fs.SkipDir
			}
			return err
		}
		switch {
		case d.Type()&fs.ModeSymlink != 0:
			return fmt.Errorf("%s is a symlink", p)
		case d.IsDir():
			return nil
		case !d.Type().IsRegular():
			return fmt.Errorf("%s is not a regular file", p)
		}
		data, err := read(p)
		if err != nil {
			return err
		}
		rel, _ := relUnder(SkillsDir, p)
		skills[rel] = &fstest.MapFile{Data: data, Mode: 0o444}
		return nil
	})
	if err != nil {
		return Rendering{}, fmt.Errorf("the rendered persona at %s: %w", root, err)
	}
	return Rendering{Instructions: string(instructions), Skills: skills}, nil
}

// relUnder is p relative to dir, for a slash path p under dir.
func relUnder(dir, p string) (string, bool) {
	return strings.CutPrefix(path.Clean(p), dir+"/")
}
