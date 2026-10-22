package claudecode

import (
	"bytes"
	"encoding/json"
	"os"
	"path"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// clusterPersonaDir is the cluster agent's persona, the example #3656 ships.
const clusterPersonaDir = "../../../../agents/cluster/persona"

// The cluster persona loads, and renders for Claude Code without a warning:
// every skill with its scripts, both broker servers, and the built-ins it
// does not grant denied.
func TestTheClusterPersonaRendersWhole(t *testing.T) {
	p := load(t, clusterPersonaDir)
	out := render(t, p, persona.Target{Relay: relay})
	if len(out.Warnings) != 0 {
		t.Errorf("the cluster persona renders with warnings: %q", out.Warnings)
	}

	wantSkills := []string{
		"gke-load-bearing-change", "gke-observability", "gke-reliability", "gke-stall-detection",
		"gke-storage", "gke-workload-scaling", "gke-workload-security", "gke-workload-troubleshooting",
	}
	var gotSkills []string
	for _, s := range p.Skills {
		gotSkills = append(gotSkills, s.Name)
	}
	if !slices.Equal(gotSkills, wantSkills) {
		t.Errorf("skills = %v, want %v", gotSkills, wantSkills)
	}
	var files []string
	for _, f := range out.Files {
		files = append(files, f.Path)
	}
	for _, want := range []string{
		MemoryFile,
		path.Join(SkillsDir, "gke-stall-detection/scripts/stall_report.py"),
		path.Join(SkillsDir, "gke-workload-security/scripts/audit_cluster.sh"),
	} {
		if !slices.Contains(files, want) {
			t.Errorf("rendered files lack %s", want)
		}
	}

	var cfg struct {
		MCPServers map[string]any `json:"mcpServers"`
	}
	if err := json.Unmarshal([]byte(flagValueRaw(t, out.Args, MCPConfigFlag)), &cfg); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"gke", "developer_knowledge"} {
		if _, ok := cfg.MCPServers[name]; !ok {
			t.Errorf("--mcp-config lacks %s", name)
		}
	}
	if slices.Contains(out.Allowed, "mcp__developer_knowledge__answer_query") {
		t.Error("answer_query is granted; the persona's include list leaves it out")
	}
	for _, tool := range []string{"Edit", "MultiEdit", "Write", "NotebookEdit", "WebFetch", "Task"} {
		if !slices.Contains(out.Disallowed, tool) {
			t.Errorf("%s is not denied to a read-only persona: %v", tool, out.Disallowed)
		}
	}
}

// Nothing cluster-specific is stamped in: the target comes from the task's
// parameters, and no skill still fetches credentials or talks to the board.
func TestTheClusterPersonaNamesNoClusterAndNoBoard(t *testing.T) {
	p := load(t, clusterPersonaDir)
	var params []string
	for _, prm := range p.Manifest.Parameters {
		if prm.Required {
			params = append(params, prm.Name)
		}
	}
	if !slices.Equal(params, []string{"project", "cluster", "location"}) {
		t.Errorf("required parameters = %v, want project, cluster, location", params)
	}
	texts := map[string]string{persona.InstructionsFile: p.Instructions}
	for _, s := range p.Skills {
		for _, f := range s.Files {
			if persona.IsMarkdown(f.Path) {
				texts[path.Join(persona.SkillsDir, s.Name, f.Path)] = string(f.Data)
			}
		}
	}
	for name, text := range texts {
		for _, banned := range []string{"get-credentials <", "kanban_", "/opt/data/", "skill_view", "$HERMES_"} {
			if strings.Contains(text, banned) {
				t.Errorf("%s still says %q", name, banned)
			}
		}
	}
}

// The persona's scripts are copies of the ones the Hermes profiles run, kept
// byte-identical so a fix to one is not lost from the other.
func TestTheClusterPersonaScriptsMatchTheirSources(t *testing.T) {
	for copied, source := range map[string]string{
		"skills/gke-stall-detection/scripts/stall_report.py":    "../../../../agents/platform/scripts/stall_report.py",
		"skills/gke-workload-security/scripts/audit_cluster.sh": "../../../../agents/platform/skills/gke-workload-security/scripts/audit_cluster.sh",
	} {
		a, err := os.ReadFile(path.Join(clusterPersonaDir, copied))
		if err != nil {
			t.Fatal(err)
		}
		b, err := os.ReadFile(source)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(a, b) {
			t.Errorf("%s differs from %s; copy the source over it", copied, source)
		}
	}
}

func flagValueRaw(t *testing.T, args []string, flag string) string {
	t.Helper()
	i := slices.Index(args, flag)
	if i < 0 || i+1 >= len(args) {
		t.Fatalf("no %s in %v", flag, args)
	}
	return args[i+1]
}
