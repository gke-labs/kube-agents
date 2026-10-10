// session-persona builds the session agent's Claude Code home: the persona
// as CLAUDE.md and the cluster agent's read-only skills as
// skills/<name>/SKILL.md, adapted for a session (transform.go). It runs at
// image build time (a2a/Dockerfile.worker), so agents/cluster/skills stays
// the one source of those skills and a2a/persona/session/persona.md the one
// source of the persona.
//
//	session-persona -skills agents/cluster/skills -persona a2a/persona/session/persona.md -out /out/claude
//
// Only SKILL.md is shipped. The skills' scripts and assets are not: the
// session pod has no shell beyond the cluster view's kubectl and gcloud, and
// a file the model can see but not use is an invitation to go looking.
package main

import (
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
)

const (
	// skillFile is the file Claude Code loads from each skill directory.
	skillFile = "SKILL.md"
	// personaFile is the user-level memory file Claude Code reads from its
	// config directory ($CLAUDE_CONFIG_DIR, else ~/.claude) on every run.
	personaFile = "CLAUDE.md"
	// skillsDir is the user-level skills directory under that same root.
	skillsDir = "skills"
	// dirMode and fileMode leave the shipped tree readable, and the
	// Dockerfile copies it in owned by root, so the harness (uid 1000)
	// cannot edit a shipped file or the skills directory in place. It owns
	// the config directory above them, so this is not a boundary against it.
	dirMode  = 0o755
	fileMode = 0o644
)

// sessionSkills are the cluster-agent skills a session ships, by directory
// name under agents/cluster/skills. Named rather than globbed: a skill added
// to the cluster agent is not a skill the session has until someone decides
// it should be, and the read-only test reads this list.
var sessionSkills = []string{
	"gke-observability",
	"gke-reliability",
	"gke-stall-detection",
	"gke-storage",
	"gke-workload-scaling",
	"gke-workload-security",
	"gke-workload-troubleshooting",
}

func main() {
	skills := flag.String("skills", "", "the cluster agent's skills directory (agents/cluster/skills)")
	persona := flag.String("persona", "", "the session persona source (a2a/persona/session/persona.md)")
	out := flag.String("out", "", "the directory to write CLAUDE.md and skills/ into")
	flag.Parse()
	if *skills == "" || *persona == "" || *out == "" {
		flag.Usage()
		os.Exit(2)
	}
	if err := build(*skills, *persona, *out); err != nil {
		fmt.Fprintln(os.Stderr, "session-persona:", err)
		os.Exit(1)
	}
}

// build writes the persona and every session skill under out.
func build(skillsSrc, personaSrc, out string) error {
	persona, err := os.ReadFile(personaSrc)
	if err != nil {
		return err
	}
	if err := mkdirOut(out); err != nil {
		return err
	}
	if err := writeFile(filepath.Join(out, personaFile), persona); err != nil {
		return err
	}
	for _, name := range sessionSkills {
		src, err := os.ReadFile(filepath.Join(skillsSrc, name, skillFile))
		if err != nil {
			return err
		}
		adapted, err := transformSkill(name, src)
		if err != nil {
			return err
		}
		dir := filepath.Join(out, skillsDir, name)
		if err := mkdir(filepath.Join(out, skillsDir)); err != nil {
			return err
		}
		if err := mkdir(dir); err != nil {
			return err
		}
		if err := writeFile(filepath.Join(dir, skillFile), adapted); err != nil {
			return err
		}
	}
	return nil
}

// mkdirOut creates out if it is missing and sets its mode only then: a
// directory the caller already has (-out /tmp, -out .) is the caller's, and
// its mode is left as it is.
func mkdirOut(out string) error {
	info, err := os.Stat(out)
	switch {
	case err == nil && !info.IsDir():
		return fmt.Errorf("%s is not a directory", out)
	case err == nil:
		return nil
	case !errors.Is(err, fs.ErrNotExist):
		return err
	}
	return mkdir(out)
}

// mkdir and writeFile set the mode explicitly after creating, so the shipped
// modes don't depend on the build's umask.
func mkdir(dir string) error {
	if err := os.MkdirAll(dir, dirMode); err != nil {
		return err
	}
	return os.Chmod(dir, dirMode)
}

func writeFile(path string, data []byte) error {
	if err := os.WriteFile(path, data, fileMode); err != nil {
		return err
	}
	return os.Chmod(path, fileMode)
}
