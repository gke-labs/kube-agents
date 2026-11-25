package main

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"testing/fstest"

	adktool "google.golang.org/adk/v2/tool"
	"google.golang.org/adk/v2/tool/skilltoolset"
	"google.golang.org/adk/v2/tool/skilltoolset/skill"
)

// loadPersona reads the persona once, at the start of the turn: the
// instructions file's text and every regular file under skills/, into
// memory, so nothing written to those paths later in the turn is read.
// Every read goes through an os.Root opened on dir, so a link inside it
// can't lead a read outside it. core-agent's own skills loader (env
// interpolation, discovery) is not used.
//
// The persona's layout is rendered read-only to this process by a second
// UID (#6713); PrepareTurn checks the directory before each spawn.
// TODO(#6713): replace this reader with coreagent.Snapshot(dir).
//
// A missing dir is no persona, unless required: a pod configured for a
// persona that finds none fails the turn rather than run without it.
func loadPersona(ctx context.Context, dir string, required bool) (string, []adktool.Toolset, error) {
	if dir == "" {
		return "", nil, nil
	}
	root, err := os.OpenRoot(dir)
	if errors.Is(err, fs.ErrNotExist) && !required {
		// No persona rendered for this pod: the turn runs on core-agent's
		// own prompt.
		return "", nil, nil
	}
	if err != nil {
		return "", nil, err
	}
	defer root.Close()
	var instructions string
	switch b, err := root.ReadFile("instructions.md"); {
	case err == nil:
		instructions = string(b)
	case !errors.Is(err, fs.ErrNotExist):
		return "", nil, err
	}
	skills, err := root.OpenRoot("skills")
	if errors.Is(err, fs.ErrNotExist) {
		return instructions, nil, nil
	}
	if err != nil {
		return "", nil, err
	}
	defer skills.Close()
	snapshot := fstest.MapFS{}
	err = fs.WalkDir(skills.FS(), ".", func(path string, d fs.DirEntry, err error) error {
		if err != nil || !d.Type().IsRegular() {
			return err
		}
		b, err := skills.ReadFile(path)
		if err != nil {
			return err
		}
		snapshot[path] = &fstest.MapFile{Data: b, Mode: 0o444}
		return nil
	})
	if err != nil {
		return "", nil, err
	}
	if len(snapshot) == 0 {
		return instructions, nil, nil
	}
	ts, err := skilltoolset.New(ctx, skilltoolset.Config{Source: skill.NewFileSystemSource(snapshot)})
	if err != nil {
		return "", nil, err
	}
	return instructions, []adktool.Toolset{ts}, nil
}
