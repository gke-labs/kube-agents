package claudecode

import (
	"bytes"
	"encoding/json"
	"io/fs"
	"os"
	"path"
	"path/filepath"
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
		for _, banned := range []string{"get-credentials", "kanban_", "/opt/data/", "skill_view", "$HERMES_", "SETTINGS.md", "mcp-"} {
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
		"target cluster only":        "**The target cluster only.**",
		"never wildcard the project": "`projects/-` is refused",
		"read-only":                  "Never mutate cluster state",
		"a denial is terminal":       "**A denial is terminal.**",
		"a refused read is a gap":    "re-run it with a spelling the policy allows, or report it as a gap",
		"no GitOps writes or PRs":    "Never open Pull Requests or push commits",
		"never expose keys":          "Never expose raw passwords or GCP/GKE keys",
	} {
		if !strings.Contains(text, phrase) {
			t.Errorf("instructions.md no longer says %s (%q)", what, phrase)
		}
	}
	// The first step and the read-only rule are pinned whole: a weakened
	// "stop", a refusal turned into "continue", a dropped verb or the lost
	// "never investigate another cluster" line all fail here.
	norm := normalizeMarkdown(text)
	for what, pinned := range map[string]string{"the confirm-the-target block": confirmTargetBlock, "the read-only rule": readOnlyRule} {
		if !strings.Contains(norm, normalizeMarkdown(pinned)) {
			t.Errorf("instructions.md no longer carries %s as pinned in this test", what)
		}
	}
}

// confirmTargetBlock is §1's first step, and readOnlyRule §2's read-only
// rule, as instructions.md states them.
var (
	confirmTargetBlock = `Credentials for the task's target are served by the credential proxy under the task's grant: ` + "`" + `kubectl` + "`" + ` and ` + "`" + `gcloud` + "`" + ` reach it through the proxy, and you never fetch or configure credentials yourself. Which cluster answers is the proxy's job, from the task's grant. Pass ` + "`" + `--project "$PROFILE_TASK_PROJECT"` + "`" + ` to every ` + "`" + `gcloud` + "`" + ` call.

**First, confirm the target.** Before any diagnostics:

1. The worker sets ` + "`" + `PROFILE_TASK_PROJECT` + "`" + `, ` + "`" + `PROFILE_TASK_LOCATION` + "`" + ` and ` + "`" + `PROFILE_TASK_CLUSTER` + "`" + ` from the task's parameters; read them with ` + "`" + `printenv PROFILE_TASK_PROJECT PROFILE_TASK_LOCATION PROFILE_TASK_CLUSTER` + "`" + `. If any env value is empty, stop. If the request names a project, location or cluster, each must match the env value (a zone and its region are not a match; stop and ask); if it names none, the env values are the target. On a mismatch, stop: say plainly that the task's target and the request disagree, and name both.
2. Then make one liveness read through the proxy: ` + "`" + `kubectl version -o json` + "`" + `. A command-policy refusal of that spelling is retried once with an allowed one (§2). If the proxy denies the read (a 403, ` + "`" + `Forbidden` + "`" + `) or it cannot reach the cluster, say that the credential proxy will not serve the target, and stop.

An investigation run against the wrong cluster produces a confident, wrong report. Never investigate a cluster other than the task's target.
`
	readOnlyRule = `- **Read-only.** Never mutate cluster state: no ` + "`" + `kubectl apply` + "`" + `, ` + "`" + `patch` + "`" + `, ` + "`" + `edit` + "`" + `, ` + "`" + `delete` + "`" + `, ` + "`" + `scale` + "`" + `, ` + "`" + `rollout restart` + "`" + `, or ` + "`" + `exec` + "`" + ` into workloads. Your tools are for read-only diagnostics: ` + "`" + `get` + "`" + `, ` + "`" + `describe` + "`" + `, ` + "`" + `logs` + "`" + `, ` + "`" + `events` + "`" + `, ` + "`" + `top` + "`" + `, and equivalent reads.
`
)

// scriptSources maps a persona skill file to the platform script it copies;
// every other file of a persona skill copies the same path under
// agents/cluster/skills.
var scriptSources = map[string]string{
	"gke-stall-detection/scripts/stall_report.py":    "../../../../agents/platform/scripts/stall_report.py",
	"gke-workload-security/scripts/audit_cluster.sh": "../../../../agents/platform/skills/gke-workload-security/scripts/audit_cluster.sh",
}

// Every file of a persona skill but its SKILL.md (the scripts and the assets)
// is a byte-identical copy of its source, so a fix to one is not lost from the
// other. A file with no known source fails.
func TestTheClusterPersonaSkillFilesMatchTheirSources(t *testing.T) {
	root := path.Join(clusterPersonaDir, persona.SkillsDir)
	checked := 0
	err := filepath.WalkDir(root, func(full string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		rel, err := filepath.Rel(root, full)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if path.Base(rel) == persona.SkillFile && path.Dir(path.Dir(rel)) == "." {
			return nil
		}
		source, ok := scriptSources[rel]
		if !ok {
			source = path.Join(clusterSkillsDir, rel)
		}
		a, err := os.ReadFile(full)
		if err != nil {
			return err
		}
		b, err := os.ReadFile(source)
		if err != nil {
			t.Errorf("%s has no source (looked for %s): copy files from agents/cluster/skills or name the source in scriptSources", rel, source)
			return nil
		}
		checked++
		if !bytes.Equal(a, b) {
			t.Errorf("%s differs from %s; copy the source over it", rel, source)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if checked < len(scriptSources) {
		t.Errorf("checked %d files, want at least the %d scripts", checked, len(scriptSources))
	}
}

// The other direction: every file a cluster skill has besides its SKILL.md
// is in the persona's copy, so a script or asset added to the source is not
// left behind.
func TestTheClusterPersonaSkillsLackNoSourceFile(t *testing.T) {
	err := filepath.WalkDir(clusterSkillsDir, func(full string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		rel, err := filepath.Rel(clusterSkillsDir, full)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if path.Base(rel) == persona.SkillFile && path.Dir(path.Dir(rel)) == "." {
			return nil
		}
		// A cluster skill the persona doesn't carry has no copy to hold its
		// other files.
		skill, _, _ := strings.Cut(rel, "/")
		if _, err := os.Stat(path.Join(clusterPersonaDir, persona.SkillsDir, skill)); err != nil {
			return nil
		}
		if _, err := os.Stat(path.Join(clusterPersonaDir, persona.SkillsDir, rel)); err != nil {
			t.Errorf("agents/cluster/skills/%s has no copy in the persona: copy it to agents/cluster/persona/skills/%s", rel, rel)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
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
	// The span runs from start to end (exclusive) or through through
	// (inclusive); one of the two is set, so a rewrite never runs open-ended
	// to the end of the file, and what follows it is still compared.
	start, end, through, text string
}

// getCredentialsLine is the credential fetch the cluster skills open a bash
// block with; fenced blocks are compared line by line, so it carries its
// newline (and, in gke-stall-detection, the blank line after it).
const getCredentialsLine = "gcloud container clusters get-credentials <cluster_name> --region <cluster_location>\n"

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
			{"`/opt/data/scripts/stall_report.py`", "`scripts/stall_report.py`, in this skill's directory (the base directory the harness gives when it loads the skill),", 1},
			{"## Running it\n\n", "## Running it\n\nRun these from this skill's directory (the base directory the harness gives when it loads the skill), where `scripts/stall_report.py` is.\n\n", 1},
			{getCredentialsLine + "\n", "", 1},
			{"/opt/data/scripts/stall_report.py", "scripts/stall_report.py", 4},
		},
		"gke-workload-troubleshooting": troubleshootingEdits,
		"gke-workload-security": {
			{"/opt/data/scripts/audit_cluster.sh", "bash scripts/audit_cluster.sh", 1},
			{"**Command:**", "**Command** (run from this skill's directory, the base directory the harness gives when it loads the skill):", 1},
			// The persona loader reads a word-initial @ as an import.
			{"<gsa-name>@<project-id>", "GSA_NAME@PROJECT_ID", 2},
		},
	}
	// gke-workload-troubleshooting names two things a profile pod does not
	// have: SETTINGS.md and a Hermes toolset name. The persona copy asks the
	// task's parameters and names the MCP server's tools.
	troubleshootingEdits = []skillEdit{
		{"from the user or active `SETTINGS.md` config:", "from the user or the task's parameters:", 1},
		{"query `mcp-developer_knowledge` first", "query the `developer_knowledge` MCP server's tools first", 1},
	}
	allowedSkillRewrites = map[string][]skillRewrite{
		"gke-stall-detection": {
			{start: "## Recording the finding", end: "A report with no rows is a result", text: stallRecording},
		},
		"gke-workload-troubleshooting": {
			{start: "Before running any diagnostics or `kubectl` commands", end: "#### Time Handling", text: troubleshootingTarget},
			{start: "### Step 5:", end: "1. Synthesize the root cause analysis", text: troubleshootingReportIntro},
			// Items 1 and 2 of Step 5 are compared as they stand.
			{start: "3. **Complete the task with the RCA in `result`**", through: "do not duplicate an existing PR.", text: troubleshootingReportResult},
		},
	}
)

var (
	ruleRE     = regexp.MustCompile(`-{3,}`)
	emphasisRE = regexp.MustCompile(`(^|[\s(])\*([^*\s][^*]*?)\*([\s,.;:)]|$)`)
)

// normalizeMarkdown undoes what prettier changes when it rewrites an edited
// file: in prose, runs of spaces inside a line, trailing spaces, table rule
// widths and cell padding, and *emphasis*. Line breaks and the indentation a
// line starts with (a list item's nesting) are kept. A fenced block is kept
// exactly, line by line, apart from the indentation of the fence itself (a
// list item's block moves with the item), so a de-indented yaml key inside
// one still shows.
func normalizeMarkdown(s string) string {
	var out []string
	lines := strings.Split(s, "\n")
	for i := 0; i < len(lines); i++ {
		line := strings.TrimRight(lines[i], " \t")
		trimmed := strings.TrimLeft(line, " ")
		indent := len(line) - len(trimmed)
		if !strings.HasPrefix(trimmed, "```") && !strings.HasPrefix(trimmed, "~~~") {
			body := strings.Join(strings.Fields(trimmed), " ")
			if strings.HasPrefix(body, "|") {
				body = ruleRE.ReplaceAllString(body, "---")
			} else {
				body = strings.Repeat(" ", indent) + body
			}
			out = append(out, emphasisRE.ReplaceAllString(body, "${1}_${2}_${3}"))
			continue
		}
		fence := trimmed[:3]
		out = append(out, trimmed)
		for i++; i < len(lines); i++ {
			body := lines[i]
			if len(body) >= indent && strings.TrimSpace(body[:indent]) == "" {
				body = body[indent:]
			}
			out = append(out, body)
			if strings.HasPrefix(strings.TrimLeft(lines[i], " "), fence) {
				break
			}
		}
	}
	return strings.Join(out, "\n")
}

// The persona's text for each rewritten section: the reporting goes to the
// final answer instead of a kanban card, and the target is checked against
// the task instead of fetched.
var (
	troubleshootingTarget = `Credentials for the target are served by the credential proxy under the task's grant; you never fetch or configure them yourself. Before running any diagnostics, confirm the target: ` + "`" + `PROFILE_TASK_PROJECT` + "`" + `, ` + "`" + `PROFILE_TASK_LOCATION` + "`" + ` and ` + "`" + `PROFILE_TASK_CLUSTER` + "`" + ` must match the cluster the request names, and ` + "`" + `kubectl version -o json` + "`" + ` must answer. Pass ` + "`" + `--project "$PROFILE_TASK_PROJECT"` + "`" + ` to every ` + "`" + `gcloud` + "`" + ` call. On an empty value or a mismatch, or if the proxy denies that read or cannot reach the cluster, stop and say so, naming both clusters. Never investigate a cluster other than the target.

`
	troubleshootingReportIntro = `### Step 5: Report the RCA & Proposed Correction

You operate under a strict **read-only** boundary: **do not apply patches directly to the cluster, and do not open Pull Requests yourself.** Whoever asked for the investigation owns the GitOps write path.

`
	troubleshootingReportResult = `3. **Put the whole RCA in your final answer**: the root cause, the evidence that grounds it, and the proposed patch as a fenced ` + "`" + `yaml` + "`" + ` block. Your instructions have the length budget — aim under 2,000 characters, tightening the prose around the cause, the evidence and the patch rather than dropping any of the three.

   If you cannot proceed — missing input, ambiguous scope — say what you need in your final answer instead.
`
	stallRecording = `## Recording the finding

You work under a strict read-only boundary: no patches applied, no Pull Requests opened. Report in your final answer.

- A request that gives a report format wins: format the answer exactly as it says. A stall alert raised through the Session KV server's ` + "`" + `controller-stall` + "`" + ` inject arrives as such a request, with the ` + "`" + `What's wrong` + "`" + ` / ` + "`" + `Why` + "`" + ` / ` + "`" + `What to do` + "`" + ` template and, when there is a fix, a ` + "`" + `To authorize:` + "`" + ` line; that shape is what gets the report saved so a reply of "apply" can act on it. Quote the scan's ` + "`" + `stalled resources: <count>` + "`" + ` line in ` + "`" + `Why` + "`" + `.
- Otherwise, follow the shape in ` + "`" + `gke-workload-troubleshooting` + "`" + `: which objects are stalled, for how long, on which heuristic, and what each is waiting for (the missing Secret, the unmet condition), grounded in the report rows and the confirming kubectl reads; or the statement that the namespace has no stalled resources. Add YAML creating or correcting the missing reference when one is identifiable.

`
)

func rewriteSpan(t *testing.T, name, s string, r skillRewrite) string {
	t.Helper()
	i := strings.Index(s, r.start)
	if i < 0 {
		t.Fatalf("%s: no %q to cut from", name, r.start)
	}
	marker, inclusive := r.end, false
	if r.through != "" {
		marker, inclusive = r.through, true
	}
	if marker == "" {
		t.Fatalf("%s: the rewrite from %q names no end", name, r.start)
	}
	k := strings.Index(s[i:], marker)
	if k < 0 {
		t.Fatalf("%s: no %q to cut to", name, marker)
	}
	j := i + k
	if inclusive {
		j += len(marker)
	}
	return s[:i] + normalizeMarkdown(r.text) + s[j:]
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
			want = rewriteSpan(t, name, want, r)
		}
		want, got = strings.TrimSpace(want), strings.TrimSpace(got)
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
