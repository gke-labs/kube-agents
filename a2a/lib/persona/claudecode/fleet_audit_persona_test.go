package claudecode

import (
	"bytes"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// The fleet-audit persona (#5623): the parent of the install's fleet-wide
// audits. Its report scripts are in the worker image (/opt/kube-agents/scripts/fleet-audit,
// a2a/Dockerfile.worker), copied from their sources at build time, so the
// persona carries only text.
const fleetAuditPersonaDir = "../../../../agents/fleet-audit/persona"

// fleetAuditVendored maps each vendored file of the persona's fleet-audit
// skill to the source it copies: its source with only
// fleetAuditReferenceEdits applied.
var fleetAuditVendored = map[string]string{
	"references/findings-document.md": "../../../../agents/platform/skills/fleet-audit/references/findings-document.md",
}

// fleetAuditReferenceEdits is every way a vendored reference may differ from
// its source, as {old, new, occurrences in the source}: the platform agent's
// path to its own skill, and links to references the persona doesn't carry.
var fleetAuditReferenceEdits = map[string][]tseSkillEdit{
	"references/findings-document.md": {
		{"(`/opt/data/profiles/platform/skills/fleet-audit/SKILL.md`)", "(`SKILL.md`, in this skill's directory)", 1},
		{"collector stream ([finish.md](finish.md#step-3--finish))", "collector stream (one that takes a collector manifest)", 1},
		{"(see [The clean run](clean-run.md#the-clean-run))", "(the platform skill's clean-run reference)", 1},
		{"see the answers list under [Remediation pull requests](remediation.md#remediation-pull-requests).", "see the platform skill's remediation reference.", 1},
	},
}

// The persona loads and renders for Claude Code without a warning, with its
// one skill, no MCP server, and one optional parameter, dry_run, a name the
// gateway accepts (lib.TaskParameterNameRE): it runs fleet-wide, so a task
// names no target.
func TestTheFleetAuditPersonaRendersWhole(t *testing.T) {
	p := load(t, fleetAuditPersonaDir)
	out := render(t, p, persona.Target{Relay: relay})
	if len(out.Warnings) != 0 {
		t.Errorf("the fleet-audit persona renders with warnings: %q", out.Warnings)
	}
	var skills []string
	for _, s := range p.Skills {
		skills = append(skills, s.Name)
	}
	if want := []string{"fleet-audit"}; !slices.Equal(skills, want) {
		t.Errorf("skills = %v, want %v", skills, want)
	}
	if len(p.Manifest.Parameters) != 1 || p.Manifest.Parameters[0].Name != "dry_run" || p.Manifest.Parameters[0].Required {
		t.Errorf("parameters = %+v, want dry_run alone, not required", p.Manifest.Parameters)
	}
	for _, prm := range p.Manifest.Parameters {
		if !lib.TaskParameterNameRE.MatchString(prm.Name) {
			t.Errorf("parameter %q isn't a name the gateway accepts", prm.Name)
		}
	}
	if len(p.MCP.Servers) != 0 {
		t.Errorf("MCP servers = %v, want none", p.MCP.Servers)
	}
}

// No persona text points at the platform agent's home, its board or its
// Hermes settings, none of which a profile worker has.
func TestTheFleetAuditPersonaNamesNoBoardOrPlatformHome(t *testing.T) {
	p := load(t, fleetAuditPersonaDir)
	texts := map[string]string{persona.InstructionsFile: p.Instructions}
	for _, s := range p.Skills {
		for _, f := range s.Files {
			if persona.IsMarkdown(f.Path) {
				texts[path.Join(persona.SkillsDir, s.Name, f.Path)] = string(f.Data)
			}
		}
	}
	for name, text := range texts {
		for _, banned := range []string{"get-credentials", "kanban_", "/opt/data", "skill_view", "$HERMES_", "SETTINGS.md", "mcp-", "USER.md", "cluster_preflight"} {
			if strings.Contains(text, banned) {
				t.Errorf("%s still says %q", name, banned)
			}
		}
	}
}

// Every vendored file is its source with only its listed edits. A change to the platform agent's copy reaches the
// persona or fails here.
func TestTheFleetAuditPersonaVendoredFilesMatchTheirSources(t *testing.T) {
	skill := path.Join(fleetAuditPersonaDir, persona.SkillsDir, "fleet-audit")
	for rel, srcPath := range fleetAuditVendored {
		src, err := os.ReadFile(srcPath)
		if err != nil {
			t.Fatal(err)
		}
		dst, err := os.ReadFile(path.Join(skill, rel))
		if err != nil {
			t.Errorf("%s: %v (copy %s there)", rel, err, srcPath)
			continue
		}
		want := string(src)
		for _, ed := range fleetAuditReferenceEdits[rel] {
			if c := strings.Count(want, ed.old); c != ed.n {
				t.Errorf("%s: the edit of %q expects %d occurrences in the source, found %d", rel, ed.old, ed.n, c)
			}
			want = strings.ReplaceAll(want, ed.old, ed.new)
		}
		if !bytes.Equal([]byte(want), dst) {
			t.Errorf("%s differs from %s beyond its listed edits; copy the source over it", rel, srcPath)
		}
	}
}

// The skill carries nothing but its own SKILL.md and the vendored files, so
// nothing in it is unpinned.
func TestTheFleetAuditPersonaSkillHoldsOnlyPinnedFiles(t *testing.T) {
	root := path.Join(fleetAuditPersonaDir, persona.SkillsDir, "fleet-audit")
	err := filepath.WalkDir(root, func(full string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		rel, err := filepath.Rel(root, full)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if _, ok := fleetAuditVendored[rel]; !ok && rel != persona.SkillFile {
			t.Errorf("skills/fleet-audit/%s is neither the skill's SKILL.md nor a vendored file in fleetAuditVendored", rel)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}
