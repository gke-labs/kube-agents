// session-persona builds the session agent's Claude Code home: the persona
// as CLAUDE.md and the cluster agent's read-only skills as
// skills/<name>/SKILL.md, adapted for a session (transform.go). It runs at
// image build time (a2a/Dockerfile.worker), so agents/cluster/skills stays
// the one source of those skills and a2a/persona/session/persona.md the one
// source of the persona. It also writes the tree's content manifest
// (persona.IntegrityManifestFile) over CLAUDE.md and skills/, which the
// session worker checks the shipped tree against at start.
//
//	session-persona -skills agents/cluster/skills -persona a2a/persona/session/persona.md -out /out/claude [-core-agent-out /out/core-agent-session]
//
// With -core-agent-out it also writes the same persona and skills in the
// core-agent harness's layout (coreagent.InstructionsFile and
// coreagent.SkillsDir, with its own content manifest), which the worker
// image ships read-only for a core-agent session pod.
//
// Of each skill, only SKILL.md is shipped. The skills' scripts and assets are not: the
// session pod has no shell beyond the cluster view's kubectl and gcloud, and
// a file the model can see but not use is an invitation to go looking.
package main

import (
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
	personafmt "github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/coreagent"
)

const (
	// skillFile is the file Claude Code loads from each skill directory.
	skillFile = "SKILL.md"
	// personaFile is the user-level memory file Claude Code reads from its
	// config directory ($CLAUDE_CONFIG_DIR, else ~/.claude) on every run.
	// It and skillsDir are lib's names, which the worker's start-up check of
	// the shipped tree (lib.SessionHomeCovers) uses too.
	personaFile = lib.SessionHomePersonaFile
	// skillsDir is the user-level skills directory under that same root.
	skillsDir = lib.SessionHomeSkillsDir
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
	"pod-container-creating",
	"pod-crashloop-oom",
	"pod-pending-scheduling",
	"service-connectivity",
	"workload-triage-scan",
}

func main() {
	skills := flag.String("skills", "", "the cluster agent's skills directory (agents/cluster/skills)")
	persona := flag.String("persona", "", "the session persona source (a2a/persona/session/persona.md)")
	out := flag.String("out", "", "the directory to write CLAUDE.md and skills/ into")
	coreAgentOut := flag.String("core-agent-out", "", "also write the persona and skills in the core-agent harness's layout into this directory")
	flag.Parse()
	if *skills == "" || *persona == "" || *out == "" {
		flag.Usage()
		os.Exit(2)
	}
	if err := build(*skills, *persona, *out); err != nil {
		fmt.Fprintln(os.Stderr, "session-persona:", err)
		os.Exit(1)
	}
	if *coreAgentOut != "" {
		if err := buildLayout(*skills, *persona, *coreAgentOut, coreAgentLayout); err != nil {
			fmt.Fprintln(os.Stderr, "session-persona:", err)
			os.Exit(1)
		}
	}
}

// layout names a harness's persona file and skills directory under the
// tree's root.
type layout struct {
	personaFile, skillsDir string
	// toolNames rewrites the source's tool references (written for Claude
	// Code) to this harness's names, in the persona and every skill. Empty
	// for Claude Code, whose tree is the source as it is.
	toolNames *strings.Replacer
}

var (
	// claudeLayout is Claude Code's config directory: CLAUDE.md and skills/.
	claudeLayout = layout{personaFile: personaFile, skillsDir: skillsDir}
	// coreAgentLayout is a core-agent rendering: instructions.md and skills/,
	// the names coreagent.Snapshot and coreagent.VerifyRendering accept.
	coreAgentLayout = layout{
		personaFile: coreagent.InstructionsFile,
		skillsDir:   coreagent.SkillsDir,
		// core-agent surfaces skills through list_skills, load_skill and
		// load_skill_resource (toolgrant.CoreAgentSkillTools).
		toolNames: strings.NewReplacer("with the Skill tool", "with `load_skill`"),
	}
)

// rename applies the layout's tool-name rewrites to text.
func (l layout) rename(text []byte) []byte {
	if l.toolNames == nil {
		return text
	}
	return []byte(l.toolNames.Replace(string(text)))
}

// build writes the persona and every session skill under out, then the
// tree's content manifest (personafmt.IntegrityManifestFile), which the worker
// checks the shipped tree against at start.
func build(skillsSrc, personaSrc, out string) error {
	return buildLayout(skillsSrc, personaSrc, out, claudeLayout)
}

// buildLayout is build with the file names l gives.
func buildLayout(skillsSrc, personaSrc, out string, l layout) error {
	written := map[string][]byte{}
	persona, err := os.ReadFile(personaSrc) // #nosec G304 -- the -persona flag of a build-time tool
	if err != nil {
		return err
	}
	if err := mkdirOut(out); err != nil {
		return err
	}
	persona = l.rename(persona)
	if err := writeFile(filepath.Join(out, l.personaFile), persona); err != nil {
		return err
	}
	written[l.personaFile] = persona
	for _, name := range sessionSkills {
		src, err := os.ReadFile(filepath.Join(skillsSrc, name, skillFile)) // #nosec G304 -- the -skills flag of a build-time tool
		if err != nil {
			return err
		}
		adapted, err := transformSkill(name, src)
		if err != nil {
			return err
		}
		adapted = l.rename(adapted)
		dir := filepath.Join(out, l.skillsDir, name)
		if err := mkdir(filepath.Join(out, l.skillsDir)); err != nil {
			return err
		}
		if err := mkdir(dir); err != nil {
			return err
		}
		if err := writeFile(filepath.Join(dir, skillFile), adapted); err != nil {
			return err
		}
		written[path.Join(l.skillsDir, name, skillFile)] = adapted
	}
	return writeFile(filepath.Join(out, personafmt.IntegrityManifestFile), personafmt.FormatIntegrityManifest(written))
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
	if err := os.MkdirAll(dir, dirMode); err != nil { // #nosec G301 -- the shipped tree is read by the agent user, so 0755 is the point
		return err
	}
	return os.Chmod(dir, dirMode) // #nosec G302 -- same mode as above
}

func writeFile(path string, data []byte) error {
	if err := os.WriteFile(path, data, fileMode); err != nil { // #nosec G306 G703 -- the -out tree of a build-time tool; the shipped tree is read by the agent user, so 0644 is the point
		return err
	}
	return os.Chmod(path, fileMode) // #nosec G302 -- same mode as writeFile's
}
