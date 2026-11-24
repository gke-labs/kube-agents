package main

import (
	"context"
	"fmt"
	"io/fs"
	"path/filepath"
	"strings"

	adktool "google.golang.org/adk/v2/tool"
	"google.golang.org/adk/v2/tool/skilltoolset"
	"google.golang.org/adk/v2/tool/skilltoolset/skill"

	"github.com/gke-labs/kube-agents/a2a/lib/persona/coreagent"
)

// loadPersona reads the persona once, at the start of the turn, through
// coreagent.Snapshot: the instructions file's text and every skill's files,
// into memory, so nothing written to those paths later in the turn is read.
// The directory is rendered read-only to this process by a second UID
// (#6713), and PrepareTurn checks it before each spawn. core-agent's own
// skills loader (env interpolation, discovery) is not used.
//
// A skill's listing and instructions come from memory. When the model loads
// a skill, its instructions start with the skill's directory on disk, which
// is where the skill's scripts are run from with the shell (as on Claude
// Code, whose skills name "the base directory the harness gives").
func loadPersona(ctx context.Context, dir string) (string, []adktool.Toolset, error) {
	if dir == "" {
		return "", nil, nil
	}
	r, err := coreagent.Snapshot(dir)
	if err != nil {
		return "", nil, err
	}
	exec, err := executables(r.Skills)
	if err != nil {
		return "", nil, fmt.Errorf("the persona's skills: %w", err)
	}
	src := baseDirSource{Source: skill.NewFileSystemSource(r.Skills), base: filepath.Join(dir, coreagent.SkillsDir), exec: exec}
	listed, err := src.ListFrontmatters(ctx)
	if err != nil {
		return "", nil, fmt.Errorf("the persona's skills: %w", err)
	}
	if len(listed) == 0 {
		return r.Instructions, nil, nil
	}
	ts, err := skilltoolset.New(ctx, skilltoolset.Config{Source: src})
	if err != nil {
		return "", nil, err
	}
	return r.Instructions, []adktool.Toolset{ts}, nil
}

// skillBaseDirPrefix opens a loaded skill's instructions, naming the
// directory its scripts are run from.
const skillBaseDirPrefix = "Base directory for this skill: "

// baseDirSource is a skill source whose instructions start with the skill's
// directory on disk under base.
type baseDirSource struct {
	skill.Source
	base string
	exec map[string][]string
}

func (s baseDirSource) LoadInstructions(ctx context.Context, name string) (string, error) {
	text, err := s.Source.LoadInstructions(ctx, name)
	if err != nil {
		return "", err
	}
	base := filepath.Join(s.base, name)
	header := skillBaseDirPrefix + base + "\n"
	if scripts := s.scripts(name); len(scripts) > 0 {
		// The shell runs a skill's script only by its absolute path, with
		// no interpreter word in front, so say so where the skill's own text
		// may say otherwise.
		header += skillScriptsNote
		for _, rel := range scripts {
			header += "- " + filepath.Join(base, rel) + "\n"
		}
	}
	return header + "\n" + text, nil
}

// skillScriptsNote introduces the list of a skill's scripts.
const skillScriptsNote = "Run this skill's scripts directly by these absolute paths, with their arguments after them; a command that starts with an interpreter such as python3 or sh is refused:\n"

// scripts are the executable files of skill name, relative to its directory.
func (s baseDirSource) scripts(name string) []string { return s.exec[name] }

// executables maps each skill in skills to its executable files, by path
// relative to the skill's directory, sorted.
func executables(skills fs.FS) (map[string][]string, error) {
	exec := map[string][]string{}
	err := fs.WalkDir(skills, ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		if info.Mode()&0o111 != 0 {
			name, rel, _ := strings.Cut(p, "/")
			exec[name] = append(exec[name], rel)
		}
		return nil
	})
	return exec, err
}
