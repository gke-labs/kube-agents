package claudecode

import (
	"bytes"
	"encoding/json"
	"os"
	"path"
	"regexp"
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
		for _, banned := range []string{"get-credentials", "kanban_", "/opt/data/", "skill_view", "$HERMES_"} {
			if strings.Contains(text, banned) {
				t.Errorf("%s still says %q", name, banned)
			}
		}
	}
}

// The instructions still state the red lines the source states, and the
// first step that keeps a task on its target.
func TestTheClusterPersonaStatesItsRedLines(t *testing.T) {
	text := load(t, clusterPersonaDir).Instructions
	for what, phrase := range map[string]string{
		"target cluster only":         "**The target cluster only.**",
		"never wildcard the project":  "`projects/-` is refused",
		"read-only":                   "Never mutate cluster state",
		"a denial is terminal":        "**A denial is terminal.**",
		"a refused read is a gap":     "re-run it with a spelling the policy allows, or report it as a gap",
		"no GitOps writes or PRs":     "Never open Pull Requests or push commits",
		"never expose keys":           "Never expose raw passwords or GCP/GKE keys",
		"target project env":          "`PROFILE_TASK_PROJECT`",
		"target location env":         "`PROFILE_TASK_LOCATION`",
		"target cluster env":          "`PROFILE_TASK_CLUSTER`",
		"stop on a mismatch":          "If any is empty, or any differs, stop",
		"liveness read through proxy": "`kubectl get --raw /version`",
	} {
		if !strings.Contains(text, phrase) {
			t.Errorf("instructions.md no longer says %s (%q)", what, phrase)
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

// clusterSkillsDir holds the skills the persona's copies are made from.
const clusterSkillsDir = "../../../../agents/cluster/skills"

// skillEdit is an allowed difference between a cluster skill and its persona
// copy, applied to the source: old must occur exactly n times (n 0 is any
// number, at least once). A skillRewrite replaces a whole section of the
// source, from its start marker to its end marker (end "" is the end of the
// file), with the persona's text for it, so the rewritten prose is pinned
// here too.
type skillEdit struct {
	old, new string
	n        int
}

type skillRewrite struct {
	start, end, text string
}

const getCredentialsLine = "gcloud container clusters get-credentials <cluster_name> --region <cluster_location> "

// allowedSkillEdits is every way a persona skill may differ from its source:
// no credential fetching, no kanban reporting, "card" read as "request", and
// skill-relative script paths. Any other divergence fails
// TestTheClusterPersonaSkillsMatchTheirSources.
var (
	allowedSkillEdits = map[string][]skillEdit{
		"gke-load-bearing-change": {
			{getCredentialsLine, "", 1},
			{"You are a Cluster Agent under", "You work under", 1},
			{"format `result` exactly as the card says", "format your answer exactly as the request says", 1},
			{"in `metadata.root_cause` and `metadata.evidence`.", "leading the answer.", 1},
			{"on the card.", "in the request.", 1},
			{"card", "request", 0},
		},
		"gke-stall-detection": {
			{"`/opt/data/scripts/stall_report.py`", "`scripts/stall_report.py`, in this skill's directory,", 1},
			{getCredentialsLine, "", 1},
			{"/opt/data/scripts/stall_report.py", "scripts/stall_report.py", 4},
		},
		"gke-workload-security": {
			{"/opt/data/scripts/audit_cluster.sh", "bash scripts/audit_cluster.sh", 1},
			// The persona loader reads a word-initial @ as an import.
			{"<gsa-name>@<project-id>", "GSA_NAME@PROJECT_ID", 2},
		},
	}
	allowedSkillRewrites = map[string][]skillRewrite{
		"gke-stall-detection": {
			{"## Recording the finding", "A report with no rows is a result", stallRecording},
		},
		"gke-workload-troubleshooting": {
			{"Before running any diagnostics or `kubectl` commands", "#### Time Handling", troubleshootingTarget},
			{"### Step 5:", "", troubleshootingReport},
		},
	}
)

var (
	ruleRE     = regexp.MustCompile(`-{3,}`)
	emphasisRE = regexp.MustCompile(`(^|[\s(])\*([^*\s][^*]*?)\*([\s,.;:)]|$)`)
)

// normalizeMarkdown undoes what prettier changes when it reflows an edited
// file: line breaks and indentation, table rule widths, and *emphasis*.
func normalizeMarkdown(s string) string {
	s = strings.Join(strings.Fields(s), " ")
	s = ruleRE.ReplaceAllString(s, "---")
	return emphasisRE.ReplaceAllString(s, "${1}_${2}_${3}")
}

// The persona's text for each rewritten section: the reporting goes to the
// final answer instead of a kanban card, and the target is checked against
// the task instead of fetched.
var (
	troubleshootingTarget = `Credentials for the target are served by the credential proxy under the task's grant; you never fetch or configure them yourself. Before running any diagnostics, confirm the target: ` + "`" + `PROFILE_TASK_PROJECT` + "`" + `, ` + "`" + `PROFILE_TASK_LOCATION` + "`" + ` and ` + "`" + `PROFILE_TASK_CLUSTER` + "`" + ` must match the cluster the request names, and ` + "`" + `kubectl get --raw /version` + "`" + ` must answer. On an empty value or a mismatch, or if that read is refused, stop and say so, naming both clusters. Never investigate a cluster other than the target.

`
	troubleshootingReport = `### Step 5: Report the RCA & Proposed Correction

You operate under a strict **read-only** boundary: **do not apply patches directly to the cluster, and do not open Pull Requests yourself.** Whoever asked for the investigation owns the GitOps write path.

1. Synthesize the root cause analysis (e.g. _"payment-api is failing with exit code 137 because its memory limit is set to 256Mi while actual usage spiked to 270Mi"_), grounded in the exact diagnostic evidence you collected.
2. Generate the corrected YAML manifest patch (e.g. increase memory limits, add missing Secret mounts, or add tolerations for Spot nodes).
3. **Put the whole RCA in your final answer**: the root cause, the evidence that grounds it, and the proposed patch as a fenced ` + "`" + `yaml` + "`" + ` block. Your instructions have the length budget — aim under 2,000 characters, tightening the prose around the cause, the evidence and the patch rather than dropping any of the three.

   If you cannot proceed — missing input, ambiguous scope — say what you need in your final answer instead.
`
	stallRecording = `## Recording the finding

You work under a strict read-only boundary: no patches applied, no Pull Requests opened. Report in your final answer.

- A request that gives a report format wins: format the answer exactly as it says. A stall alert raised through the Session KV server's ` + "`" + `controller-stall` + "`" + ` inject arrives as such a request, with the ` + "`" + `What's wrong` + "`" + ` / ` + "`" + `Why` + "`" + ` / ` + "`" + `What to do` + "`" + ` template and, when there is a fix, a ` + "`" + `To authorize:` + "`" + ` line; that shape is what gets the report saved so a reply of "apply" can act on it. Quote the scan's ` + "`" + `stalled resources: <count>` + "`" + ` line in ` + "`" + `Why` + "`" + `.
- Otherwise, follow the shape in ` + "`" + `gke-workload-troubleshooting` + "`" + `: which objects are stalled, for how long, on which heuristic, and what each is waiting for (the missing Secret, the unmet condition), grounded in the report rows and the confirming kubectl reads; or the statement that the namespace has no stalled resources. Add YAML creating or correcting the missing reference when one is identifiable.

`
)

func rewriteSpan(t *testing.T, name, s, start, end, text string) string {
	t.Helper()
	i := strings.Index(s, start)
	if i < 0 {
		t.Fatalf("%s: no %q to cut from", name, start)
	}
	j := len(s)
	if end != "" {
		k := strings.Index(s[i:], end)
		if k < 0 {
			t.Fatalf("%s: no %q to cut to", name, end)
		}
		j = i + k
	}
	return s[:i] + normalizeMarkdown(text) + " " + s[j:]
}

// Each persona skill is its source with only the allowed edits, so a fix to a
// cluster skill reaches the persona or fails here.
func TestTheClusterPersonaSkillsMatchTheirSources(t *testing.T) {
	entries, err := os.ReadDir(path.Join(clusterPersonaDir, persona.SkillsDir))
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		name := e.Name()
		src, err := os.ReadFile(path.Join(clusterSkillsDir, name, persona.SkillFile))
		if err != nil {
			t.Fatal(err)
		}
		dst, err := os.ReadFile(path.Join(clusterPersonaDir, persona.SkillsDir, name, persona.SkillFile))
		if err != nil {
			t.Fatal(err)
		}
		want, got := normalizeMarkdown(string(src)), normalizeMarkdown(string(dst))
		for _, ed := range allowedSkillEdits[name] {
			if c := strings.Count(want, ed.old); (ed.n == 0 && c == 0) || (ed.n > 0 && c != ed.n) {
				t.Errorf("%s: the allowed edit of %q expects %d occurrences, the source has %d", name, ed.old, ed.n, c)
			}
			want = strings.ReplaceAll(want, ed.old, ed.new)
		}
		for _, r := range allowedSkillRewrites[name] {
			want = strings.TrimSpace(rewriteSpan(t, name, want, r.start, r.end, r.text))
		}
		if want != got {
			i := 0
			for i < len(want) && i < len(got) && want[i] == got[i] {
				i++
			}
			lo := max(0, i-60)
			t.Errorf("%s: the persona copy diverges from its source beyond the allowed edits, near:\n source:  %q\n persona: %q",
				name, want[lo:min(len(want), i+80)], got[lo:min(len(got), i+80)])
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
