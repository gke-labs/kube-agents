package main

import (
	"context"
	"io/fs"
	"os"
	"path/filepath"
	"testing/fstest"

	adktool "google.golang.org/adk/v2/tool"
	"google.golang.org/adk/v2/tool/skilltoolset"
	"google.golang.org/adk/v2/tool/skilltoolset/skill"
)

// loadPersona reads the persona once, at the start of the turn: the
// instructions file's text and every file under the skills directory, into
// memory, so nothing written to those paths later in the turn is read.
// core-agent's own skills loader (env interpolation, discovery) is not used.
//
// SEAM (dev-2): the persona's layout, rendered read-only to this process by
// a second UID (#6713); PrepareTurn checks the directory before each spawn.
// TODO(dev-2, #6713): replace this reader with coreagent.Snapshot(dir).
func loadPersona(ctx context.Context, dir string) (string, []adktool.Toolset, error) {
	if dir == "" {
		return "", nil, nil
	}
	instructionsFile, skillsDir := filepath.Join(dir, "instructions.md"), filepath.Join(dir, "skills")
	var instructions string
	if _, err := os.Stat(instructionsFile); err == nil {
		b, err := os.ReadFile(instructionsFile)
		if err != nil {
			return "", nil, err
		}
		instructions = string(b)
	}
	if _, err := os.Stat(skillsDir); err != nil {
		return instructions, nil, nil
	}
	snapshot := fstest.MapFS{}
	err := filepath.WalkDir(skillsDir, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		if !d.Type().IsRegular() {
			return nil
		}
		rel, err := filepath.Rel(skillsDir, path)
		if err != nil {
			return err
		}
		b, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		snapshot[filepath.ToSlash(rel)] = &fstest.MapFile{Data: b, Mode: 0o444}
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
