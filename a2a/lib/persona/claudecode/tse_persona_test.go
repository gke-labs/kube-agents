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

// The GKE Support Agent's persona and the Hermes profile tree its skills are
// copied from. The helpers here are tse-prefixed copies of the cluster
// persona test's (#3720), so each test stands alone in the package; fold them
// into one set when both have landed.
const (
	tsePersonaDir = "../../../../agents/tse/persona"
	tseSkillsDir  = "../../../../agents/tse/skills"
)

// The tse persona loads, and renders for Claude Code without a warning: its
// skill, both broker servers with their read tools, and the built-ins it does
// not grant denied.
func TestTheTsePersonaRendersWhole(t *testing.T) {
	p := load(t, tsePersonaDir)
	out := render(t, p, persona.Target{Relay: relay})
	if len(out.Warnings) != 0 {
		t.Errorf("the tse persona renders with warnings: %q", out.Warnings)
	}
	var gotSkills []string
	for _, s := range p.Skills {
		gotSkills = append(gotSkills, s.Name)
	}
	if want := []string{"support-escalation-bundle"}; !slices.Equal(gotSkills, want) {
		t.Errorf("skills = %v, want %v", gotSkills, want)
	}
	var files []string
	for _, f := range out.Files {
		files = append(files, f.Path)
	}
	for _, want := range []string{MemoryFile, path.Join(SkillsDir, "support-escalation-bundle", persona.SkillFile)} {
		if !slices.Contains(files, want) {
			t.Errorf("rendered files lack %s", want)
		}
	}

	var cfg struct {
		MCPServers map[string]any `json:"mcpServers"`
	}
	// flagValue splits on commas; the JSON has its own.
	if err := json.Unmarshal([]byte(strings.Join(flagValue(t, out.Args, MCPConfigFlag), ",")), &cfg); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"gke", "developer_knowledge"} {
		if _, ok := cfg.MCPServers[name]; !ok {
			t.Errorf("--mcp-config lacks %s", name)
		}
	}
	for _, tool := range []string{"mcp__developer_knowledge__search_documents", "mcp__developer_knowledge__get_documents", "mcp__gke__get_cluster", "Bash", "Read", "WebSearch", "Skill"} {
		if !slices.Contains(out.Allowed, tool) {
			t.Errorf("%s is not granted: %v", tool, out.Allowed)
		}
	}
	if slices.Contains(out.Allowed, "mcp__developer_knowledge__answer_query") {
		t.Error("answer_query is granted; the persona's include list and its instructions leave it out")
	}
	for _, tool := range []string{"Edit", "MultiEdit", "Write", "NotebookEdit", "WebFetch", "Task"} {
		if !slices.Contains(out.Disallowed, tool) {
			t.Errorf("%s is not denied to a read-only persona: %v", tool, out.Disallowed)
		}
	}
}

// Nothing cluster-specific is stamped in, and none of the Hermes profile's
// machinery (the board, the preflight script, Hermes tool and skill-catalog
// names) survives in the persona's text.
func TestTheTsePersonaNamesNoClusterAndNoBoard(t *testing.T) {
	p := load(t, tsePersonaDir)
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
		for _, banned := range []string{"get-credentials", "kanban_", "/opt/data", "skill_view", "$HERMES_", "SETTINGS.md", "mcp-", "USER.md", "cluster_preflight"} {
			if strings.Contains(text, banned) {
				t.Errorf("%s still says %q", name, banned)
			}
		}
	}
}

// The instructions keep the red lines the Hermes profile states: read-only,
// no secrets, a redaction gate before a bundle leaves, the target only, and
// the first step that keeps a task on its target.
func TestTheTsePersonaStatesItsRedLines(t *testing.T) {
	text := load(t, tsePersonaDir).Instructions
	for what, phrase := range map[string]string{
		"target cluster only":        "**The target cluster only.**",
		"never wildcard the project": "`projects/-` is refused",
		"a denial is terminal":       "**A denial is terminal.**",
		"no GitOps writes or PRs":    "Never open Pull Requests or push commits",
		"negative proof":             "Never hallucinate Google tenant-project telemetry",
		"never expose secrets":       "Never expose raw passwords, Secret payloads, or GCP/GKE keys.",
		"grounded docs":              "never `answer_query`",
		"the self-audit gate":        "## 5. Pre-report self-audit gate",
		"the length budget":          "never exceeding 4,000 characters",
	} {
		if !strings.Contains(text, phrase) {
			t.Errorf("instructions.md no longer says %s (%q)", what, phrase)
		}
	}
	norm := tseNormalizeMarkdown(text)
	for what, pinned := range map[string]string{
		"the confirm-the-target block":  tseConfirmTargetBlock,
		"the read-only rule":            tseReadOnlyRule,
		"the redaction gate":            tseRedactionRule,
		"the target-only rule":          tseTargetOnlyRule,
		"the zero-assumptions rule":     tseZeroAssumptionsRule,
		"the fail-loud rule":            tseFailLoudRule,
		"the skills-are-read-only rule": tseSkillsReadOnlyRule,
	} {
		if !strings.Contains(norm, tseNormalizeMarkdown(pinned)) {
			t.Errorf("instructions.md no longer carries %s as pinned in this test", what)
		}
	}
}

// The first step is the cluster persona's, word for word; the rules after it
// are §2's, pinned whole.
var (
	tseConfirmTargetBlock = `Credentials for the task's target are served by the credential proxy under the task's grant: ` + "`" + `kubectl` + "`" + ` and ` + "`" + `gcloud` + "`" + ` reach it through the proxy, and you never fetch or configure credentials yourself. Which cluster answers is the proxy's job, from the task's grant. Pass ` + "`" + `--project "$PROFILE_TASK_PROJECT"` + "`" + ` to every ` + "`" + `gcloud` + "`" + ` call.

**First, confirm the target.** Before any diagnostics:

1. The worker sets ` + "`" + `PROFILE_TASK_PROJECT` + "`" + `, ` + "`" + `PROFILE_TASK_LOCATION` + "`" + ` and ` + "`" + `PROFILE_TASK_CLUSTER` + "`" + ` from the task's parameters; read them with ` + "`" + `printenv PROFILE_TASK_PROJECT PROFILE_TASK_LOCATION PROFILE_TASK_CLUSTER` + "`" + `. If any env value is empty, stop. If the request names a project, location or cluster, each must match the env value (a zone and its region are not a match; stop and ask); if it names none, the env values are the target. On a mismatch, stop: say plainly that the task's target and the request disagree, and name both.
2. Then make one liveness read through the proxy: ` + "`" + `kubectl version -o json` + "`" + `. A command-policy refusal of that spelling is retried once with an allowed one (§2). If the proxy denies the read (a 403, ` + "`" + `Forbidden` + "`" + `) or it cannot reach the cluster, say that the credential proxy will not serve the target, and stop.

An investigation run against the wrong cluster produces a confident, wrong report. Never investigate a cluster other than the task's target.
`
	tseReadOnlyRule = `- **Read-only.** Never mutate cluster or cloud state: no ` + "`" + `kubectl apply` + "`" + `, ` + "`" + `patch` + "`" + `, ` + "`" + `edit` + "`" + `, ` + "`" + `delete` + "`" + `, ` + "`" + `scale` + "`" + `, ` + "`" + `rollout restart` + "`" + `, or ` + "`" + `exec` + "`" + `, and no mutating ` + "`" + `gcloud` + "`" + ` command. Your tools are for read-only diagnostics: ` + "`" + `get` + "`" + `, ` + "`" + `describe` + "`" + `, ` + "`" + `logs` + "`" + `, ` + "`" + `events` + "`" + `, ` + "`" + `top` + "`" + `, and ` + "`" + `gcloud ... describe` + "`" + `, ` + "`" + `list` + "`" + ` and ` + "`" + `read` + "`" + `.
`
	tseRedactionRule = `- **Redact before anything leaves.** Before an escalation bundle or any quoted evidence goes into your answer, apply the redaction rules of the ` + "`" + `support-escalation-bundle` + "`" + ` skill (its Step 2): no Secret payloads, password or API-key values, bearer or OAuth tokens, or private keys. Keep the resource identifiers Google Cloud Support needs to locate the cluster.
`
	tseTargetOnlyRule = `- **The target cluster only.** Never query or reason about other clusters. If a request concerns another cluster, say in your answer that it is out of your scope.
`
	tseZeroAssumptionsRule = `- **Zero unverified internal assumptions.** Operate strictly on customer-visible telemetry: ` + "`" + `kubectl` + "`" + `, ` + "`" + `gcloud` + "`" + `, Cloud Logging, Cloud Monitoring, GCE serial port output, the ` + "`" + `gke` + "`" + ` and ` + "`" + `developer_knowledge` + "`" + ` MCP servers, and your skills. Never hallucinate Google tenant-project telemetry; instead, prove customer-side hypotheses are ruled out (negative proof) and package the escalation with the ` + "`" + `support-escalation-bundle` + "`" + ` skill.
`
	tseFailLoudRule = `- **Fail loud, never silent.** If you cannot operate — the target is missing, the task's target and the request disagree, or the credential proxy will not serve the target (§1) — say exactly why in your answer before you stop.
`
	tseSkillsReadOnlyRule = `- **Your skills are read-only.** Do not edit them; if one is wrong for the work, say so in your answer, naming the skill and what it should have said.
`
)

// The include lists keep the model's offer to the relay's read tools: gke's is
// exactly agents/platform/scripts/mcp_policy.py's GKE_READ_TOOLS, and
// developer_knowledge's is its DEVELOPER_KNOWLEDGE_READ_TOOLS less
// answer_query, whose small daily quota every agent of an install shares. A
// write tool such as update_cluster added to either fails here.
func TestTheTsePersonaOffersOnlyTheRelaysReadTools(t *testing.T) {
	src, err := os.ReadFile("../../../../agents/platform/scripts/mcp_policy.py")
	if err != nil {
		t.Fatal(err)
	}
	readTools := func(name string) []string {
		m := regexp.MustCompile(`(?s)\n` + name + ` = frozenset\(\s*\{(.*?)\}`).FindSubmatch(src)
		if m == nil {
			t.Fatalf("mcp_policy.py has no %s frozenset", name)
		}
		var tools []string
		for _, q := range regexp.MustCompile(`"([a-z_0-9]+)"`).FindAllSubmatch(m[1], -1) {
			tools = append(tools, string(q[1]))
		}
		if len(tools) == 0 {
			t.Fatalf("read no tools from %s", name)
		}
		slices.Sort(tools)
		return tools
	}
	want := map[string][]string{
		"gke": readTools("GKE_READ_TOOLS"),
		"developer_knowledge": slices.DeleteFunc(readTools("DEVELOPER_KNOWLEDGE_READ_TOOLS"), func(s string) bool {
			return s == "answer_query"
		}),
	}
	servers := load(t, tsePersonaDir).MCP.Servers
	if len(servers) != len(want) {
		t.Errorf("mcp.yaml has %d servers, want %d", len(servers), len(want))
	}
	for name, tools := range want {
		got := slices.Sorted(slices.Values(servers[name].Include))
		if !slices.Equal(got, tools) {
			t.Errorf("%s's include list = %v, want the relay's read tools %v", name, got, tools)
		}
	}
}

// tseSkillEdit is an allowed difference between a tse skill and its persona
// copy, applied to the normalized source: old must occur exactly n times.
type tseSkillEdit struct {
	old, new string
	n        int
}

// tseAllowedSkillEdits is every way a persona skill's SKILL.md may differ
// from its agents/tse/skills source. Any other divergence fails
// TestTheTsePersonaSkillsMatchTheirSources.
var tseAllowedSkillEdits = map[string][]tseSkillEdit{
	"support-escalation-bundle": {
		// The bundle's commands read shell variables; the persona sets them
		// from the task's target.
		{tseBundleLead, tseBundleLead + "\n" + tseBundleVars, 1},
	},
}

const (
	tseBundleLead = "needs to begin root-cause analysis immediately without round-trip clarification questions:\n"
	tseBundleVars = `Set the variables from the task's target first: ` + "`" + `PROJECT_ID="$PROFILE_TASK_PROJECT"` + "`" + `, ` + "`" + `CLUSTER_NAME="$PROFILE_TASK_CLUSTER"` + "`" + ` and ` + "`" + `LOCATION="$PROFILE_TASK_LOCATION"` + "`" + `. ` + "`" + `NODE_NAME` + "`" + ` and ` + "`" + `NODE_ZONE` + "`" + ` come from your evidence.
`
)

// Each persona skill is its source with only the allowed edits, so a fix to a
// tse skill reaches the persona or fails here.
func TestTheTsePersonaSkillsMatchTheirSources(t *testing.T) {
	entries, err := os.ReadDir(path.Join(tsePersonaDir, persona.SkillsDir))
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		name := e.Name()
		src, err := os.ReadFile(path.Join(tseSkillsDir, name, persona.SkillFile))
		if err != nil {
			t.Fatal(err)
		}
		dst, err := os.ReadFile(path.Join(tsePersonaDir, persona.SkillsDir, name, persona.SkillFile))
		if err != nil {
			t.Fatal(err)
		}
		want, got := tseNormalizeMarkdown(string(src)), tseNormalizeMarkdown(string(dst))
		for _, ed := range tseAllowedSkillEdits[name] {
			if c := strings.Count(want, ed.old); c != ed.n {
				t.Errorf("%s: the allowed edit of %q expects %d occurrences, the source has %d", name, ed.old, ed.n, c)
			}
			want = strings.ReplaceAll(want, ed.old, ed.new)
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

// The two trees hold the same files: every file but a SKILL.md is a
// byte-identical copy, and a file added on either side without the other
// fails.
func TestTheTsePersonaSkillTreesHoldTheSameFiles(t *testing.T) {
	list := func(root string) map[string][]byte {
		files := map[string][]byte{}
		err := filepath.WalkDir(root, func(full string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return err
			}
			rel, err := filepath.Rel(root, full)
			if err != nil {
				return err
			}
			data, err := os.ReadFile(full)
			files[filepath.ToSlash(rel)] = data
			return err
		})
		if err != nil {
			t.Fatal(err)
		}
		return files
	}
	src, dst := list(tseSkillsDir), list(path.Join(tsePersonaDir, persona.SkillsDir))
	if len(src) == 0 {
		t.Fatalf("no files under %s", tseSkillsDir)
	}
	for rel, a := range src {
		b, ok := dst[rel]
		switch {
		case !ok:
			t.Errorf("agents/tse/skills/%s has no copy in the persona: copy it to agents/tse/persona/skills/%s", rel, rel)
		case path.Base(rel) != persona.SkillFile && !bytes.Equal(a, b):
			t.Errorf("agents/tse/persona/skills/%s differs from its source; copy the source over it", rel)
		}
	}
	for rel := range dst {
		if _, ok := src[rel]; !ok {
			t.Errorf("agents/tse/persona/skills/%s has no source under agents/tse/skills", rel)
		}
	}
}

var (
	tseRuleRE     = regexp.MustCompile(`-{3,}`)
	tseEmphasisRE = regexp.MustCompile(`(^|[\s(])\*([^*\s][^*]*?)\*([\s,.;:)]|$)`)
)

// tseNormalizeMarkdown undoes what prettier changes when it rewrites an edited
// file: in prose, runs of spaces inside a line, trailing spaces, table rule
// widths and cell padding, and *emphasis*. Line breaks and the indentation a
// line starts with (a list item's nesting) are kept. A fenced block is kept
// exactly, line by line, apart from the indentation of the fence itself (a
// list item's block moves with the item), so a de-indented yaml key inside
// one still shows.
func tseNormalizeMarkdown(s string) string {
	var out []string
	lines := strings.Split(s, "\n")
	for i := 0; i < len(lines); i++ {
		line := strings.TrimRight(lines[i], " \t")
		trimmed := strings.TrimLeft(line, " ")
		indent := len(line) - len(trimmed)
		if !strings.HasPrefix(trimmed, "```") && !strings.HasPrefix(trimmed, "~~~") {
			body := strings.Join(strings.Fields(trimmed), " ")
			if strings.HasPrefix(body, "|") {
				body = tseRuleRE.ReplaceAllString(body, "---")
			} else {
				body = strings.Repeat(" ", indent) + body
			}
			out = append(out, tseEmphasisRE.ReplaceAllString(body, "${1}_${2}_${3}"))
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
