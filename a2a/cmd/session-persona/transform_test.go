package main

import (
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"

	"sigs.k8s.io/yaml"
)

// The real sources, relative to this package: the tests build the shipped
// tree from them rather than from fixtures, so a change to a cluster skill
// that breaks the session's copy fails here, not in an image build.
const (
	repoSkillsDir   = "../../../agents/cluster/skills"
	repoPersona     = "../../persona/session/persona.md"
	repoDockerfile  = "../../Dockerfile.worker"
	harnessHomeEnv  = "CLAUDE_CONFIG_DIR"
	buildOutFlag    = "-out"
	sessionToolPath = "./cmd/session-persona"
)

// writeCommandRE finds a command that changes a cluster or a project. It is a
// denylist, written independently of the transform's allowlists, so the two
// have to agree for the read-only test to pass.
var writeCommandRE = regexp.MustCompile(
	`\bkubectl\s+(apply|create|delete|patch|edit|replace|scale|autoscale|annotate|label|set|rollout\s+(?:restart|undo|pause|resume)|cordon|uncordon|drain|taint|expose|run|cp|exec|debug|attach)\b` +
		`|\bgcloud\b[^\n` + "`" + `]*\s(create|delete|update|upgrade|resize|add-iam-policy-binding|remove-iam-policy-binding|set-iam-policy|enable|disable|deploy)\b`)

// otherProgramRE finds a command line that runs a program the worker image
// does not ship. The build has already rewritten a ./scripts/ path to the
// not-shipped placeholder, so a line that starts with the placeholder is a
// program too; the literal is spelled out rather than taken from
// shippedFileArg so the check does not move with the constant.
var otherProgramRE = regexp.MustCompile(`^\s*(\$\s+)?(\./|python3?\s|git\s|jq\s|bash\s|sh\s|<file not in this session>)`)

// unshippedRefRE finds a reference to a file the session does not ship: a
// scripts/ or assets/ path, the cluster agent's /opt/data tree or its
// settings file, starting a word. Written independently of filePathRE. A
// path inside a URL is documentation and is not a reference.
var unshippedRefRE = regexp.MustCompile("(^|[\\s(`\"'=,])((\\./)?(assets|scripts)/|/opt/data/|SETTINGS\\.md)")

// The shipped-tree check has to see the shapes the rewrite handles, or a
// path the rewrite missed would ship with the check green.
func TestUnshippedRefRESeesAttachedAndListedPaths(t *testing.T) {
	for _, l := range []string{
		"kubectl apply --filename=assets/x.yaml",
		"kubectl apply -f=./assets/x.yaml",
		"kubectl apply -f <file not in this session>,assets/b.yaml",
	} {
		if !unshippedRefRE.MatchString(l) {
			t.Errorf("unshippedRefRE misses %q", l)
		}
	}
}

// buildShipped builds into a directory that does not exist yet, as the
// Dockerfile does (/out/claude), so build creates it and sets its mode.
func buildShipped(t *testing.T) string {
	t.Helper()
	out := filepath.Join(t.TempDir(), "claude")
	if err := build(repoSkillsDir, repoPersona, out); err != nil {
		t.Fatal(err)
	}
	return out
}

// The shipped tree is what Claude Code loads: CLAUDE.md at the root and one
// SKILL.md per skill whose frontmatter carries exactly the name, matching its
// directory, and the source's description. A field beyond those two would
// either be ignored or, like allowed-tools, widen what the skill may do.
func TestShippedTreeIsLoadableByClaudeCode(t *testing.T) {
	out := buildShipped(t)

	persona, err := os.ReadFile(filepath.Join(out, personaFile))
	if err != nil {
		t.Fatal(err)
	}
	src, _ := os.ReadFile(repoPersona)
	if string(persona) != string(src) {
		t.Error("CLAUDE.md is not the persona source byte for byte")
	}

	entries, err := os.ReadDir(filepath.Join(out, skillsDir))
	if err != nil {
		t.Fatal(err)
	}
	var shipped []string
	for _, e := range entries {
		shipped = append(shipped, e.Name())
	}
	if !slices.Equal(shipped, sessionSkills) {
		t.Fatalf("shipped skills = %v, want %v", shipped, sessionSkills)
	}

	for _, name := range sessionSkills {
		body, err := os.ReadFile(filepath.Join(out, skillsDir, name, skillFile))
		if err != nil {
			t.Fatal(err)
		}
		fm, rest, err := splitFrontmatter(body)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		var meta map[string]any
		if err := yaml.Unmarshal(fm, &meta); err != nil {
			t.Fatalf("%s: frontmatter: %v", name, err)
		}
		keys := make([]string, 0, len(meta))
		for k := range meta {
			keys = append(keys, k)
		}
		slices.Sort(keys)
		if !slices.Equal(keys, []string{"description", "name"}) {
			t.Errorf("%s: frontmatter keys = %v, want exactly name and description", name, keys)
		}
		if meta["name"] != name {
			t.Errorf("%s: name = %v", name, meta["name"])
		}
		srcBody, _ := os.ReadFile(filepath.Join(repoSkillsDir, name, skillFile))
		srcFM, _, _ := splitFrontmatter(srcBody)
		var srcMeta skillFrontmatter
		_ = yaml.Unmarshal(srcFM, &srcMeta)
		if meta["description"] != srcMeta.Description || srcMeta.Description == "" {
			t.Errorf("%s: description changed in the build: %q", name, meta["description"])
		}
		// Only SKILL.md ships, and Claude Code gives the model the skill's
		// directory with its text: a path left in would send it looking.
		// Checked in prose and command blocks; a manifest or other inert
		// block is left as written.
		inFence, check := false, true
		for i, l := range strings.Split(string(rest), "\n") {
			if trimmed := strings.TrimSpace(l); strings.HasPrefix(trimmed, codeFence) {
				inFence = !inFence
				check = !inFence || commandBlock(strings.TrimSpace(strings.TrimPrefix(trimmed, codeFence)))
				continue
			}
			if check && unshippedRefRE.MatchString(l) {
				t.Errorf("%s:%d: still points at a file that is not shipped: %s", name, i+1, strings.TrimSpace(l))
			}
		}
		if !strings.Contains(string(rest), sessionPreamble) {
			t.Errorf("%s: no session preamble", name)
		}
		// A literal, not clusterAgentOnlyMarker: the check must not move
		// with the constant it checks.
		for _, tool := range []string{"kanban_complete", "kanban_block"} {
			if strings.Contains(string(rest), tool) {
				t.Errorf("%s: still calls the cluster agent's %s", name, tool)
			}
		}
	}
}

// Every command in a shipped skill that changes a cluster or a project sits
// in a code block the propose-only note precedes, and none appears in prose
// or inline code where no note can reach it. A program the image does not
// ship is marked too. Checked against the real skills, so a cluster-skill
// edit that adds an unmarked write fails here.
func TestShippedSkillsMarkEveryWrite(t *testing.T) {
	out := buildShipped(t)
	writes := 0
	for _, name := range sessionSkills {
		body, err := os.ReadFile(filepath.Join(out, skillsDir, name, skillFile))
		if err != nil {
			t.Fatal(err)
		}
		lines := strings.Split(string(body), "\n")
		lastNote := ""
		inFence := false
		for i, l := range lines {
			trimmed := strings.TrimSpace(l)
			if strings.HasPrefix(trimmed, codeFence) {
				if !inFence {
					lastNote = precedingNote(lines, i)
				}
				inFence = !inFence
				continue
			}
			if writeCommandRE.MatchString(l) {
				writes++
				if !inFence {
					t.Errorf("%s:%d: a write outside a code block, where no note marks it: %s", name, i+1, trimmed)
				} else if lastNote != proposeOnlyNote {
					t.Errorf("%s:%d: a write in a block not marked propose-only: %s", name, i+1, trimmed)
				}
			}
			if inFence && otherProgramRE.MatchString(l) && lastNote != unavailableNote && lastNote != proposeOnlyNote {
				t.Errorf("%s:%d: a program the image does not ship, unmarked: %s", name, i+1, trimmed)
			}
		}
	}
	// The source skills do contain writes; zero would mean the scan matched
	// nothing, not that the skills are clean.
	if writes == 0 {
		t.Fatal("found no write commands at all; the scan is not reading the skills")
	}
}

// precedingNote returns the note line just above a code fence, skipping one
// blank line, or "".
func precedingNote(lines []string, fence int) string {
	for j := fence - 1; j >= 0 && j >= fence-2; j-- {
		if s := strings.TrimSpace(lines[j]); s != "" {
			return s
		}
	}
	return ""
}

// The persona names the skills the image ships, no more and no fewer, and
// says nothing that points the model at files: vamp-49's no-view check
// (gke-labs#2831) expects no Read, Glob or Grep on a cluster question, and
// skills load through the Skill tool.
func TestPersonaNamesTheShippedSkillsAndNoFiles(t *testing.T) {
	persona, err := os.ReadFile(repoPersona)
	if err != nil {
		t.Fatal(err)
	}
	p := string(persona)
	for _, name := range sessionSkills {
		if !strings.Contains(p, "`"+name+"`") {
			t.Errorf("persona does not name %s", name)
		}
	}
	for _, m := range regexp.MustCompile("`(gke-[a-z-]+)`").FindAllStringSubmatch(p, -1) {
		if !slices.Contains(sessionSkills, m[1]) {
			t.Errorf("persona names %s, which the image does not ship", m[1])
		}
	}
	for _, path := range []string{"SKILL.md", ".claude", "/home/node", "skills/"} {
		if strings.Contains(p, path) {
			t.Errorf("persona names a file path (%q); skills load through the Skill tool", path)
		}
	}
}

// The image builds the tree with this tool and copies it to the directory
// Claude Code reads, which the Dockerfile names as CLAUDE_CONFIG_DIR. Read
// from the Dockerfile itself; every extraction must find something.
func TestWorkerImageShipsTheTreeWhereTheHarnessReadsIt(t *testing.T) {
	raw, err := os.ReadFile(repoDockerfile)
	if err != nil {
		t.Fatal(err)
	}
	df := string(raw)

	home := regexp.MustCompile(harnessHomeEnv + `=(\S+)`).FindStringSubmatch(df)
	if home == nil {
		t.Fatalf("Dockerfile sets no %s", harnessHomeEnv)
	}
	run := regexp.MustCompile(`RUN go run ` + regexp.QuoteMeta(sessionToolPath) + `\s.*` + buildOutFlag + `\s+(\S+)`).FindStringSubmatch(df)
	if run == nil {
		t.Fatalf("Dockerfile does not run %s with %s", sessionToolPath, buildOutFlag)
	}
	if !strings.Contains(df, "COPY agents/cluster/skills/ ") {
		t.Error("Dockerfile does not copy agents/cluster/skills into the build stage")
	}
	for _, want := range []string{
		"COPY --from=build " + run[1] + "/" + personaFile + " " + home[1] + "/" + personaFile,
		"COPY --from=build " + run[1] + "/" + skillsDir + "/ " + home[1] + "/" + skillsDir + "/",
	} {
		if !strings.Contains(df, want) {
			t.Errorf("Dockerfile lacks %q", want)
		}
	}
	// After the chown, so the copies stay root-owned, and nothing after
	// them hands the tree back to the harness's user.
	chown := strings.Index(df, "chown -R node:node /home/node")
	copied := strings.Index(df, "COPY --from=build "+run[1])
	if chown < 0 || copied < 0 {
		t.Fatalf("chown at %d, persona COPY at %d; both must exist", chown, copied)
	}
	if chown > copied {
		t.Error("the persona is copied before the chown, so the harness would own it")
	}
	for _, l := range strings.Split(df[copied:], "\n") {
		if regexp.MustCompile(`chown|chmod`).MatchString(l) && regexp.MustCompile(`/home/node|\.claude|--chown`).MatchString(l) {
			t.Errorf("after the persona COPY, %q changes who can write the harness home", strings.TrimSpace(l))
		}
	}
}

// The tree is built group- and world-unwritable. COPY --from keeps these
// modes, so this plus root ownership is what stops the harness (uid 1000)
// editing a shipped file or the skills directory in place. The harness owns
// the config directory above them, so it is not a boundary against it.
func TestShippedTreeIsNotWritableByOthers(t *testing.T) {
	out := buildShipped(t)
	n := 0
	err := filepath.Walk(out, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		n++
		want := os.FileMode(fileMode)
		if info.IsDir() {
			want = dirMode
		}
		if info.Mode().Perm()&0o022 != 0 || info.Mode().Perm() != want {
			t.Errorf("%s is mode %o, want %o; group or others must not write it", path, info.Mode().Perm(), want)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if n < len(sessionSkills)+2 {
		t.Fatalf("walked %d entries; the tree is not there", n)
	}
}

// An -out directory that already exists is the caller's: build writes into
// it and leaves its mode alone (-out /tmp must keep /tmp's sticky bit).
func TestBuildLeavesAnExistingOutDirsMode(t *testing.T) {
	out := t.TempDir()
	const callerMode = 0o750
	if err := os.Chmod(out, callerMode); err != nil {
		t.Fatal(err)
	}
	if err := build(repoSkillsDir, repoPersona, out); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(out)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != callerMode {
		t.Errorf("build changed the existing -out dir to %o, want %o", info.Mode().Perm(), callerMode)
	}
	skills, err := os.Stat(filepath.Join(out, skillsDir))
	if err != nil {
		t.Fatal(err)
	}
	if skills.Mode().Perm() != dirMode {
		t.Errorf("%s is %o, want %o", skillsDir, skills.Mode().Perm(), dirMode)
	}
}

func TestTransformDropsFrontmatterBeyondNameAndDescription(t *testing.T) {
	src := "---\nname: demo\ndescription: A demo.\nallowed-tools: Bash\nhooks:\n  PreToolUse: []\n---\n\n# Demo\n\nBody.\n"
	out, err := transformSkill("demo", []byte(src))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(out), "allowed-tools") || strings.Contains(string(out), "hooks") {
		t.Errorf("a widening field survived:\n%s", out)
	}
}

func TestTransformRefusesABrokenSkill(t *testing.T) {
	for name, src := range map[string]string{
		"name mismatch":     "---\nname: other\ndescription: d\n---\n# T\n",
		"no description":    "---\nname: demo\n---\n# T\n",
		"no frontmatter":    "# T\n",
		"unclosed":          "---\nname: demo\ndescription: d\n# T\n",
		"bad yaml":          "---\nname: demo\ndescription: a: b: c\n---\n# T\n",
		"description > max": "---\nname: demo\ndescription: " + strings.Repeat("x", maxSkillDescriptionLen+1) + "\n---\n# T\n",
	} {
		if _, err := transformSkill("demo", []byte(src)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

// The classifier is an allowlist: a kubectl or gcloud verb it does not know
// is a write, and a filter after a read is a pipe, not a missing program.
func TestClassifyBlock(t *testing.T) {
	for _, tc := range []struct {
		lang string
		body string
		want blockClass
	}{
		{"bash", "kubectl get pods -n x", blockRead},
		{"bash", "kubectl -n x describe pod p", blockRead},
		{"bash", "kubectl frobnicate pods", blockWrite},
		{"bash", "kubectl rollout restart deploy/x", blockWrite},
		{"bash", "gcloud container clusters describe c --region r", blockRead},
		{"bash", "gcloud logging read \"a | b\" --project p", blockRead},
		{"bash", "gcloud container clusters update c \\\n  --enable-x", blockWrite},
		{"bash", "gcloud services enable x", blockWrite},
		{"bash", "kubectl get deploy x -o yaml | grep probe", blockPipe},
		{"bash", "kubectl get cm x -o yaml | kubectl apply -f -", blockWrite},
		{"bash", "./scripts/audit.sh a b", blockUnavailable},
		{"bash", "# a comment\n\nkubectl get ns", blockRead},
		{"yaml", "kind: Pod", blockInert},
		{"", "$ python3 x.py\nOBJECT ROW", blockUnavailable},
		{"", "plain output", blockInert},
		{"bash", "kubectl auth can-i list pods", blockRead},
		{"bash", "kubectl auth reconcile -f rbac.yaml", blockWrite},
		{"bash", "kubectl config view", blockWrite},
		{"bash", "kubectl config get-clusters", blockWrite},
		{"bash", "kubectl config current-context", blockRead},
		{"bash", "kubectl cluster-info", blockRead},
		{"bash", "kubectl cluster-info dump", blockWrite},
		{"bash", "kubectl cluster-info --output-directory=/tmp/x dump", blockWrite},
		{"bash", "kubectl rollout history deploy/x", blockRead},
		{"bash", "kubectl rollout status deploy/x", blockWrite},
		{"bash", "kubectl wait --for=condition=Ready pod/p", blockWrite},
		{"bash", "gcloud iam service-accounts describe sa@p.iam.gserviceaccount.com", blockWrite},
		{"bash", "gcloud sql instances list", blockWrite},
		{"bash", "gcloud run services describe s", blockWrite},
		{"bash", "gcloud container clusters get-credentials c --region r", blockRead},
		{"bash", "gcloud container clusters list", blockRead},
		{"bash", "gcloud beta compute advice capacity --region r", blockRead},
		{"bash", "gcloud compute advice capacity --region r", blockWrite},
		{"bash", "gcloud --project p container clusters list", blockWrite},
		// A kubectl write verb that wraps another kubectl or gcloud is the
		// write its own verb makes it, not a wrapper.
		{"bash", "kubectl exec p -- gcloud auth list", blockWrite},
		{"bash", "kubectl debug node/n -it --image=busybox -- kubectl get pods", blockWrite},
		{"bash", "kubectl run tmp --image=bitnami/kubectl -- kubectl version", blockWrite},
		// An escaped quote inside a double-quoted filter does not end it,
		// so a pipe later in the filter is not a pipe.
		{"bash", `gcloud logging read "resource.labels.pod_name=\"p\" AND textPayload=~\"a|b\"" --project p`, blockRead},
		{"bash", `gcloud logging read "x=\"y\"" | head`, blockPipe},
		{"bash", "kubectl config use-context other", blockWrite},
		{"bash", "env FOO=1 kubectl delete pod p", blockWrite},
		{"bash", "kubectl get pods -o name | xargs kubectl delete", blockWrite},
		{"bash", "timeout 5 kubectl get pods", blockUnavailable},
		// A console block is a transcript: only its "$ " lines are commands,
		// and the rest is output. Same for an untagged block.
		{"console", "$ kubectl get pods -n x\nNAME READY STATUS", blockRead},
		{"console", "$ kubectl delete pod p -n x\npod \"p\" deleted", blockWrite},
		{"console", "NAME READY STATUS", blockInert},
		{"", "$ kubectl get pods -n x\nNAME READY STATUS", blockRead},
		{"", "$ kubectl get pods \\\n  -n x\nNAME READY STATUS", blockRead},
		// A "$ " prompt in a shell block is stripped from that line; its
		// unprompted neighbours are still commands.
		{"bash", "$ kubectl get pods -n x", blockRead},
		{"bash", "$ kubectl get pods -n x\nkubectl delete pod p", blockWrite},
	} {
		if got := classifyBlock(tc.lang, strings.Split(tc.body, "\n")); got != tc.want {
			t.Errorf("classifyBlock(%q, %q) = %d, want %d", tc.lang, tc.body, got, tc.want)
		}
	}
}

// A cluster-agent section's own text is replaced by its heading and the
// session note; its siblings survive, the title section that contains it is
// not the one replaced, and a subsection that does not call the tool itself
// is kept.
func TestReplaceClusterAgentSections(t *testing.T) {
	src := strings.Split("# Title\n\nIntro.\n\n## Step 1\n\nKeep.\n\n## Step 2\n\nCall kanban_complete(x).\n\n### Detail\n\nMore.\n\n## Step 3\n\nKeep too.", "\n")
	got := strings.Join(replaceClusterAgentSections(src), "\n")
	for _, want := range []string{"Intro.", "## Step 1", "Keep.", "## Step 2", sessionReportNote, "### Detail", "More.", "## Step 3", "Keep too."} {
		if !strings.Contains(got, want) {
			t.Errorf("lost %q:\n%s", want, got)
		}
	}
	if strings.Contains(got, "kanban_complete") {
		t.Errorf("kept kanban_complete:\n%s", got)
	}
}

// A marker in a parent section's intro replaces that intro only: the
// diagnostic steps nested under it are the skill, and they ship.
func TestReplaceClusterAgentSectionsKeepsAParentsChildren(t *testing.T) {
	src := strings.Split("# Title\n\nIntro.\n\n## Diagnostic Workflow\n\nFile the result with `kanban_complete` when you are done.\n\n### Step 0\n\n```bash\nkubectl get pods -n x\n```\n\n#### Step 0a\n\nCheck events.\n\n### Step 1\n\nRead the logs.\n\n## Notes\n\nLast.", "\n")
	got := strings.Join(replaceClusterAgentSections(src), "\n")
	for _, want := range []string{"Intro.", "## Diagnostic Workflow", sessionReportNote, "### Step 0", "kubectl get pods -n x", "#### Step 0a", "Check events.", "### Step 1", "Read the logs.", "## Notes", "Last."} {
		if !strings.Contains(got, want) {
			t.Errorf("lost %q:\n%s", want, got)
		}
	}
	if strings.Contains(got, "kanban_") {
		t.Errorf("kept the marker:\n%s", got)
	}
}

func TestPreambleSkipsHeadingsInsideCodeBlocks(t *testing.T) {
	got := insertPreamble(strings.Split("```bash\n# a comment\n```\n# Title\nBody", "\n"))
	i := slices.Index(got, sessionPreamble)
	if i < 1 || got[i-2] != "# Title" {
		t.Errorf("preamble placed at %d:\n%s", i, strings.Join(got, "\n"))
	}
}

// A console transcript of a read ships with no note: the session can run it.
func TestConsoleTranscriptOfAReadIsNotMarked(t *testing.T) {
	src := "---\nname: demo\ndescription: A demo.\n---\n\n# Demo\n\n```console\n$ kubectl get pods -n x\nNAME READY STATUS\n```\n"
	out, err := transformSkill("demo", []byte(src))
	if err != nil {
		t.Fatal(err)
	}
	for _, note := range []string{unavailableNote, proposeOnlyNote, pipeNote} {
		if strings.Contains(string(out), note) {
			t.Errorf("a read-only console block was marked %q:\n%s", note, out)
		}
	}
}

// A path is rewritten where it starts a word in prose or a command block,
// and left alone inside a URL or a block that is not commands.
func TestReplaceFileReferences(t *testing.T) {
	for _, tc := range []struct{ name, in, want string }{
		{"url in prose",
			"See https://cloud.google.com/kubernetes-engine/docs/tutorials/scripts/setup.sh for more.",
			"See https://cloud.google.com/kubernetes-engine/docs/tutorials/scripts/setup.sh for more."},
		{"manifest in a yaml block",
			"```yaml\nmountPath: /opt/data/scripts\n```",
			"```yaml\nmountPath: /opt/data/scripts\n```"},
		{"json block",
			"```json\n{\"path\": \"scripts/x.sh\"}\n```",
			"```json\n{\"path\": \"scripts/x.sh\"}\n```"},
		{"text block",
			"```text\nscripts/x.sh\n```",
			"```text\nscripts/x.sh\n```"},
		{"path in prose",
			"Run scripts/x.sh first.",
			"Run " + shippedFileProse + " first."},
		{"path at line start",
			"SETTINGS.md holds it.",
			shippedFileProse + " holds it."},
		{"path in parentheses",
			"(see ./assets/a.yaml)",
			"(see " + shippedFileProse + ")"},
		{"inline code",
			"Use `scripts/x.sh`.",
			"Use " + shippedFileProse + "."},
		{"markdown link",
			"Use [the script](scripts/x.sh).",
			"Use " + shippedFileProse + "."},
		{"bash block",
			"```bash\n./scripts/audit.sh a\npython3 /opt/data/scripts/r.py\n```",
			"```bash\n" + shippedFileArg + " a\npython3 " + shippedFileArg + "\n```"},
		{"console block",
			"```console\n$ kubectl apply -f assets/a.yaml\n```",
			"```console\n$ kubectl apply -f " + shippedFileArg + "\n```"},
		{"untagged block",
			"```\n$ python3 /opt/data/scripts/r.py\n```",
			"```\n$ python3 " + shippedFileArg + "\n```"},
		{"attached long flag",
			"```bash\nkubectl apply --filename=assets/x.yaml\n```",
			"```bash\nkubectl apply --filename=" + shippedFileArg + "\n```"},
		{"attached short flag",
			"```bash\nkubectl apply -f=./assets/x.yaml\n```",
			"```bash\nkubectl apply -f=" + shippedFileArg + "\n```"},
		{"comma list",
			"```bash\nkubectl apply -f assets/a.yaml,assets/b.yaml\n```",
			"```bash\nkubectl apply -f " + shippedFileArg + "," + shippedFileArg + "\n```"},
		{"comma list in prose",
			"Apply assets/a.yaml,assets/b.yaml first.",
			"Apply " + shippedFileProse + "," + shippedFileProse + " first."},
		{"url in a bash block",
			"```bash\ncurl -O https://example.com/scripts/x.sh\n```",
			"```bash\ncurl -O https://example.com/scripts/x.sh\n```"},
	} {
		got := strings.Join(replaceFileReferences(strings.Split(tc.in, "\n")), "\n")
		if got != tc.want {
			t.Errorf("%s:\n got %q\nwant %q", tc.name, got, tc.want)
		}
	}
}
