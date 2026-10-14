package main

import (
	"bytes"
	"fmt"
	"regexp"
	"strings"

	"sigs.k8s.io/yaml"
)

const (
	// frontmatterFence opens and closes a SKILL.md's YAML block.
	frontmatterFence = "---"
	// codeFence opens and closes a Markdown code block.
	codeFence = "```"
	// shellPrompt marks a command line in a transcript (a console or
	// untagged block), as in a worked example that shows a command and its
	// output together. In a shell-tagged block it is stripped from the line.
	shellPrompt = "$ "
	// shellComment, lineContinuation, placeholderOpen and quoteChar are the
	// shell syntax the classifier reads: a comment line, a backslash that
	// joins the next line, a <placeholder> argument, and a quoted argument.
	shellComment     = "#"
	lineContinuation = "\\"
	placeholderOpen  = "<"
	quoteChar        = "\""
	// titlePrefix opens a skill's H1 title, which the preamble follows.
	titlePrefix = "# "
	// maxSkillNameLen and maxSkillDescriptionLen are the Agent Skills limits
	// on the two frontmatter fields Claude Code reads to list a skill.
	maxSkillNameLen        = 64
	maxSkillDescriptionLen = 1024
	// clusterAgentOnlyMarker names a tool only the cluster agent has. A
	// section that calls it is the cluster agent's reporting step, which a
	// session does not have; it is replaced by sessionReportNote.
	clusterAgentOnlyMarker = "kanban_"

	// proposeOnlyNote precedes a code block that changes a cluster or a
	// project. The session's broker refuses writes anyway; the note is what
	// keeps the model from trying, and from presenting the step as done.
	proposeOnlyNote = "> **Propose this to the operator; do not run it.** It changes a cluster or a project, and this session is read-only. Write it out as a proposal, or delegate it to `platform` if the person wants it done."
	// unavailableNote precedes a code block that runs a program the
	// session can't: Bash is limited to kubectl and gcloud, and the scripts
	// the skills name are not shipped.
	unavailableNote = "> **Not available in this session.** This runs a program this session can't run. With the cluster view, make the same reads with single `kubectl` or `gcloud` commands; otherwise say what you couldn't check."
	// pipeNote precedes a read-only block whose commands pipe into another
	// program, which the cluster view's Bash rule does not allow.
	pipeNote = "> Run each `kubectl` or `gcloud` command on its own, one per call, with no pipe, and read the output yourself."
	// sessionReportNote replaces a section that only the cluster agent can
	// carry out, keeping its heading so the skill's cross-references still land.
	sessionReportNote = "_In a session this step is your answer: give the root cause, the evidence that grounds it, and any proposed patch in your reply. The cluster agent's reporting tools don't exist here._"

	// shippedFileProse and shippedFileArg replace a reference to one of the
	// cluster agent's scripts, example files or settings, in prose and in a
	// command respectively. Only SKILL.md ships, and Claude Code hands the
	// model the skill's directory with its text, so a relative link left in
	// place would be an invitation to go looking for a file that isn't there.
	shippedFileProse = "the cluster agent's file (not in this session)"
	shippedFileArg   = "<file not in this session>"
	// filePathPattern is a relative scripts/ or assets/ path, an absolute
	// script path, or the cluster agent's settings file. filePathRE adds the
	// left boundary it is matched under.
	filePathPattern = `(\./)?(assets|scripts)/[\w./-]+|/opt/data/[\w./-]+|SETTINGS\.md`

	// sessionPreamble follows each skill's title. It says, once, what the
	// per-block notes say locally, and covers what they can't mark: prose
	// that names files, tools and scripts this pod doesn't have.
	sessionPreamble = `> **Using this skill in a kube-agents session.** It was written for the cluster agent.
>
> - Run its ` + "`kubectl`" + ` and ` + "`gcloud`" + ` commands only when your system prompt gives you the read-only cluster view, one command per call. Without the view, use the steps to judge what the person showed you, or to say what ` + "`platform`" + ` should check when you delegate.
> - A step that changes a cluster or a project is a proposal, never something you run. Code blocks that do are marked; a step written as prose ("edit", "apply", "enable") is a proposal too.
> - Scripts, example files, settings files, git and tools the skill names (kanban, MCP servers, web search) are not in this session. Don't look for them; where a step describes a script's checks, make the same checks by reading the objects yourself.`
)

var (
	// fileLinkRE is a Markdown link to a relative scripts/ or assets/
	// path. filePathRE is filePathPattern starting a word: at the start of
	// the line or after whitespace, a parenthesis, a backtick, a quote, an
	// `=` (a flag's attached value) or a `,` (a list of paths). It
	// captures that boundary so the replacement keeps it, and it leaves a
	// path inside a URL alone (".../docs/scripts/setup.sh" is documentation,
	// not the cluster agent's file).
	fileLinkRE = regexp.MustCompile(`\[[^\]]*\]\((\./)?(assets|scripts)/[^)]*\)`)
	filePathRE = regexp.MustCompile("(^|[\\s(`\"'=,])(" + filePathPattern + ")")
	// quotedFilePathRE is the same path as inline code in prose, replaced
	// with its backticks so the replacement reads as prose.
	quotedFilePathRE = regexp.MustCompile("`(" + filePathPattern + ")`")
	// skillNameRE is the Agent Skills name rule: lowercase letters, digits
	// and hyphens.
	skillNameRE = regexp.MustCompile(`^[a-z0-9]+(-[a-z0-9]+)*$`)
	// headingRE matches an ATX heading.
	headingRE = regexp.MustCompile(`^(#{1,6})\s`)
	// shellLangs are the info strings whose blocks are all commands, the
	// same set as deploy/docker/check_skill_commands.py's SHELL_LANGUAGES.
	// transcriptLangs are the ones whose blocks are a transcript: only a
	// shellPrompt line is a command, and the rest is what it printed. Any
	// other info string (yaml, json, text) marks an inert block.
	shellLangs      = map[string]bool{"bash": true, "sh": true, "shell": true, "zsh": true}
	transcriptLangs = map[string]bool{"": true, "console": true}
	// The read tables below have to be a subset of what the credential
	// broker lets a session run: agents/platform/scripts/command_policy.py
	// (KUBECTL_READ_VERBS, KUBECTL_REFUSED_SUBCOMMANDS, GCLOUD_READ_COMMANDS),
	// narrowed for the session role by credential_proxy.py
	// (SESSION_KUBECTL_REFUSED_VERBS). A block left unmarked is one the
	// preamble tells the model to run, so a read here that the broker refuses
	// is a refusal the model walks into. TestReadTablesAreASubsetOfTheBroker
	// reads those tables from the Python source and fails on drift.
	//
	// kubectlReadCommands are the kubectl verbs, and verb-subverb pairs, that
	// only read. The classifier is an allowlist: a verb not named here is
	// treated as a write, so a new skill step fails toward "propose" rather
	// than "run". A group verb with a writing member (`auth reconcile`,
	// `config use-context`, `rollout restart`) is listed only by its reading
	// pairs. `config view` is not one: the broker refuses it because
	// `--flatten` prints the token. `wait` and `rollout status` are reads the
	// broker refuses a session, because both hold a broker slot until they
	// finish.
	kubectlReadCommands = map[string]bool{
		"api-resources": true, "api-versions": true, "cluster-info": true, "describe": true,
		"events": true, "explain": true, "get": true, "logs": true, "top": true, "version": true,
		"auth can-i": true, "auth whoami": true,
		"config current-context": true, "config get-contexts": true,
		"rollout history": true,
	}
	// kubectlRefusedSubcommands are pairs whose verb reads on its own but
	// whose subcommand does not: `cluster-info dump --output-directory`
	// writes files wherever it is told. Matched against any later word, so a
	// flag between the two does not hide it.
	kubectlRefusedSubcommands = map[string]bool{
		"cluster-info dump": true,
	}
	// kubectlValueFlags are the global flags that can precede the verb with
	// their value as a separate word. Any other flag in that position makes
	// its value read as the verb, which classifies as a write: fail closed.
	kubectlValueFlags = map[string]bool{
		"-n": true, "--namespace": true, "--context": true, "--kubeconfig": true,
		"--cluster": true, "--user": true,
	}
	// gcloudReadCommands are the gcloud command paths that only read,
	// matched as a prefix of the command's leading words the way the broker
	// matches them, so positional arguments after the path are allowed. A
	// release track (`beta`) is a word like any other: each track's path is
	// listed on its own. get-credentials writes a local kubeconfig, which is
	// how every other read here reaches the cluster; the broker serves it.
	gcloudReadCommands = map[string]bool{
		"artifacts docker images describe":             true,
		"artifacts repositories describe":              true,
		"artifacts repositories list":                  true,
		"asset search-all-resources":                   true,
		"auth list":                                    true,
		"beta compute advice calendar-mode":            true,
		"beta compute advice capacity":                 true,
		"beta compute advice capacity-history":         true,
		"beta monitoring metrics-scopes describe":      true,
		"billing budgets list":                         true,
		"compute addresses describe":                   true,
		"compute addresses list":                       true,
		"compute backend-services list":                true,
		"compute disks describe":                       true,
		"compute disks list":                           true,
		"compute firewall-rules describe":              true,
		"compute firewall-rules list":                  true,
		"compute forwarding-rules describe":            true,
		"compute forwarding-rules list":                true,
		"compute instance-groups managed describe":     true,
		"compute instance-groups managed list":         true,
		"compute instances describe":                   true,
		"compute instances get-serial-port-output":     true,
		"compute instances list":                       true,
		"compute machine-types list":                   true,
		"compute networks describe":                    true,
		"compute networks list":                        true,
		"compute networks subnets describe":            true,
		"compute networks subnets list":                true,
		"compute networks subnets list-usable":         true,
		"compute project-info describe":                true,
		"compute regions describe":                     true,
		"compute regions list":                         true,
		"compute reservations list":                    true,
		"compute routers describe":                     true,
		"compute routers get-nat-mapping-info":         true,
		"compute routers get-status":                   true,
		"compute routers list":                         true,
		"compute security-policies list":               true,
		"compute shared-vpc list-associated-resources": true,
		"compute snapshots describe":                   true,
		"compute snapshots list":                       true,
		"compute sole-tenancy node-groups list":        true,
		"compute sole-tenancy node-groups list-nodes":  true,
		"compute target-pools list":                    true,
		"config get":                                   true,
		"config get-value":                             true,
		"config list":                                  true,
		"container ai profiles list":                   true,
		"container ai profiles manifests create":       true,
		"container ai profiles models list":            true,
		"container clusters describe":                  true,
		"container clusters get-credentials":           true,
		"container clusters list":                      true,
		"container get-server-config":                  true,
		"container node-pools describe":                true,
		"container node-pools list":                    true,
		"container operations list":                    true,
		"info":                                         true,
		"logging read":                                 true,
		"projects describe":                            true,
		"projects get-iam-policy":                      true,
		"projects list":                                true,
		"version":                                      true,
	}
)

// blockClass is what a code block asks the model to do, strictest last.
type blockClass int

const (
	blockInert       blockClass = iota // not commands: YAML, text, output
	blockRead                          // kubectl/gcloud reads, one per line
	blockPipe                          // reads, but piped into another program
	blockUnavailable                   // a program this image doesn't ship
	blockWrite                         // changes a cluster or a project
)

// skillFrontmatter is the part of a SKILL.md's YAML block Claude Code reads
// to list a skill. Anything else in the source block is dropped: fields such
// as allowed-tools or hooks would widen what the skill may do when invoked,
// and a skill with only these two fields is one the Skill tool runs without
// a permission rule naming it.
type skillFrontmatter struct {
	Name        string `json:"name"`
	Description string `json:"description"`
}

// transformSkill turns one cluster-agent SKILL.md into the session's copy:
// the same name and description, a session preamble after the title, a note
// before every code block that is not a plain read, and the cluster agent's
// reporting sections replaced by sessionReportNote.
func transformSkill(dirName string, src []byte) ([]byte, error) {
	fm, body, err := splitFrontmatter(src)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", dirName, err)
	}
	var meta skillFrontmatter
	if err := yaml.Unmarshal(fm, &meta); err != nil {
		return nil, fmt.Errorf("%s: frontmatter: %w", dirName, err)
	}
	if err := validateFrontmatter(dirName, meta); err != nil {
		return nil, err
	}
	// Written field by field so name leads, as in the source; Marshal
	// would sort the keys.
	var out []byte
	for _, field := range []map[string]string{{"name": meta.Name}, {"description": meta.Description}} {
		line, err := yaml.Marshal(field)
		if err != nil {
			return nil, fmt.Errorf("%s: frontmatter: %w", dirName, err)
		}
		out = append(out, line...)
	}

	lines := strings.Split(string(body), "\n")
	lines = replaceClusterAgentSections(lines)
	lines = annotateBlocks(lines)
	lines = replaceFileReferences(lines)
	lines = insertPreamble(lines)

	var b bytes.Buffer
	b.WriteString(frontmatterFence + "\n")
	b.Write(out)
	b.WriteString(frontmatterFence + "\n")
	b.WriteString(strings.Join(lines, "\n"))
	return b.Bytes(), nil
}

// validateFrontmatter holds the two fields to what Claude Code accepts. The
// skill's name is its directory name, so the two have to agree or the model
// would be told to load a skill by a name that does not resolve.
func validateFrontmatter(dirName string, meta skillFrontmatter) error {
	switch {
	case meta.Name != dirName:
		return fmt.Errorf("%s: frontmatter name %q does not match its directory", dirName, meta.Name)
	case len(meta.Name) > maxSkillNameLen || !skillNameRE.MatchString(meta.Name):
		return fmt.Errorf("%s: name %q is not lowercase letters, digits and hyphens of at most %d characters", dirName, meta.Name, maxSkillNameLen)
	case strings.TrimSpace(meta.Description) == "":
		return fmt.Errorf("%s: empty description; Claude Code lists a skill by it", dirName)
	case len(meta.Description) > maxSkillDescriptionLen:
		return fmt.Errorf("%s: description is %d characters, over %d", dirName, len(meta.Description), maxSkillDescriptionLen)
	}
	return nil
}

// splitFrontmatter separates the YAML block from the Markdown body.
func splitFrontmatter(src []byte) (fm, body []byte, err error) {
	s := string(src)
	if !strings.HasPrefix(s, frontmatterFence+"\n") {
		return nil, nil, fmt.Errorf("no frontmatter: the file does not open with %q", frontmatterFence)
	}
	rest := s[len(frontmatterFence)+1:]
	end := strings.Index(rest, "\n"+frontmatterFence+"\n")
	if end < 0 {
		return nil, nil, fmt.Errorf("frontmatter is not closed")
	}
	return []byte(rest[:end]), []byte(rest[end+len("\n"+frontmatterFence+"\n"):]), nil
}

// replaceClusterAgentSections swaps the own text of each section that calls
// a cluster-agent tool for its heading and sessionReportNote. A section's
// own text runs from its heading to the next heading of any level, and that
// is both what decides and what is removed: the subsections under it are
// judged on their own text and kept unless they call one too, so a marker in
// a parent's intro cannot take the diagnostic steps nested under it.
func replaceClusterAgentSections(lines []string) []string {
	// own ends at the next heading of any level: the marker has to be in
	// the section's own text, or the title section, which spans the whole
	// file, would be the one replaced.
	type section struct{ start, own int }
	var sections []section
	inFence := false
	for i, l := range lines {
		if strings.HasPrefix(strings.TrimSpace(l), codeFence) {
			inFence = !inFence
			continue
		}
		if inFence {
			continue
		}
		if headingRE.MatchString(l) {
			if n := len(sections); n > 0 {
				sections[n-1].own = i
			}
			sections = append(sections, section{start: i, own: len(lines)})
		}
	}

	var out []string
	next := 0
	for _, s := range sections {
		if !strings.Contains(strings.Join(lines[s.start:s.own], "\n"), clusterAgentOnlyMarker) {
			continue
		}
		out = append(out, lines[next:s.start]...)
		out = append(out, lines[s.start], "", sessionReportNote, "")
		next = s.own
	}
	return append(out, lines[next:]...)
}

// annotateBlocks puts the strictest applicable note before each code block
// that is not inert or a plain read.
func annotateBlocks(lines []string) []string {
	var out []string
	for i := 0; i < len(lines); i++ {
		trimmed := strings.TrimSpace(lines[i])
		if !strings.HasPrefix(trimmed, codeFence) {
			out = append(out, lines[i])
			continue
		}
		indent := lines[i][:len(lines[i])-len(strings.TrimLeft(lines[i], " "))]
		lang := strings.TrimSpace(strings.TrimPrefix(trimmed, codeFence))
		end := i + 1
		for end < len(lines) && !strings.HasPrefix(strings.TrimSpace(lines[end]), codeFence) {
			end++
		}
		if note := noteFor(classifyBlock(lang, lines[i+1:min(end, len(lines))])); note != "" {
			out = append(out, indent+note, "")
		}
		stop := min(end+1, len(lines))
		out = append(out, lines[i:stop]...)
		i = stop - 1
	}
	return out
}

func noteFor(c blockClass) string {
	switch c {
	case blockWrite:
		return proposeOnlyNote
	case blockUnavailable:
		return unavailableNote
	case blockPipe:
		return pipeNote
	}
	return ""
}

// commandBlock reports whether a code block with this info string holds
// commands, as a shell block or a transcript.
func commandBlock(lang string) bool {
	return shellLangs[lang] || transcriptLangs[lang]
}

// classifyBlock reads a code block's commands and returns the strictest
// class among them. A shell-tagged block is all commands, with a "$ "
// prompt stripped where a line has one; a console or untagged block is a
// transcript and counts only its "$ " lines, as in a worked example. A
// prompted line in a shell block keeps its unprompted neighbours as
// commands, so a block mixing the two is marked by its strictest line
// rather than by the prompted ones alone.
func classifyBlock(lang string, body []string) blockClass {
	if !commandBlock(lang) {
		return blockInert
	}
	transcript := transcriptLangs[lang]
	var cmds []string
	var cur strings.Builder
	for _, raw := range body {
		l := strings.TrimSpace(raw)
		if transcript && cur.Len() == 0 && !strings.HasPrefix(l, shellPrompt) {
			continue
		}
		l = strings.TrimPrefix(l, shellPrompt)
		if cur.Len() == 0 && (l == "" || strings.HasPrefix(l, shellComment)) {
			continue
		}
		if strings.HasSuffix(l, lineContinuation) {
			cur.WriteString(strings.TrimSuffix(l, lineContinuation) + " ")
			continue
		}
		cur.WriteString(l)
		cmds = append(cmds, cur.String())
		cur.Reset()
	}
	if cur.Len() > 0 {
		cmds = append(cmds, cur.String())
	}

	worst := blockInert
	for _, c := range cmds {
		for i, seg := range splitShell(c) {
			class := classifyCommand(seg)
			if i > 0 && class != blockWrite {
				// A filter after a read (grep, jq) is the pipe the
				// cluster view's Bash rule refuses, not a missing tool.
				class = blockPipe
			}
			worst = max(worst, class)
		}
	}
	return worst
}

// splitShell splits a command line on pipes and command separators that sit
// outside quotes. The first segment is the command; a later one is either a
// pipe target or a second command, and both are classified. A backslash
// escapes the next character outside single quotes, as in the shell, so an
// escaped quote inside a double-quoted filter does not end it.
func splitShell(line string) []string {
	var segs []string
	var cur strings.Builder
	var quote rune
	runes := []rune(line)
	for i := 0; i < len(runes); i++ {
		r := runes[i]
		switch {
		case r == '\\' && quote != '\'' && i+1 < len(runes):
			cur.WriteRune(r)
			i++
			cur.WriteRune(runes[i])
		case quote != 0:
			if r == quote {
				quote = 0
			}
			cur.WriteRune(r)
		case r == '\'' || r == '"':
			quote = r
			cur.WriteRune(r)
		case r == '|' || r == ';' || (r == '&' && i+1 < len(runes) && runes[i+1] == '&'):
			if r == '&' || (r == '|' && i+1 < len(runes) && runes[i+1] == '|') {
				i++
			}
			segs = append(segs, strings.TrimSpace(cur.String()))
			cur.Reset()
		default:
			cur.WriteRune(r)
		}
	}
	return append(segs, strings.TrimSpace(cur.String()))
}

// classifyCommand says what one command does. kubectl and gcloud are judged
// by verb against the read allowlists; anything else is a program the
// session image does not have.
func classifyCommand(cmd string) blockClass {
	fields := strings.Fields(cmd)
	if len(fields) == 0 {
		return blockInert
	}
	switch fields[0] {
	case "kubectl":
		// Judged on its own verb first, so `kubectl exec|debug|run ...
		// -- kubectl ...` is the write its verb makes it, whatever it wraps.
		return classifyKubectl(fields[1:])
	case "gcloud":
		return classifyGcloud(fields[1:])
	}
	// A kubectl or gcloud behind a wrapper (env, timeout, xargs) is judged
	// as itself, so a write keeps its propose note; the wrapper is still a
	// program the session can't run.
	for i, f := range fields[1:] {
		if f == "kubectl" || f == "gcloud" {
			if classifyCommand(strings.Join(fields[i+1:], " ")) == blockWrite {
				return blockWrite
			}
			return blockUnavailable
		}
	}
	return blockUnavailable
}

// classifyKubectl judges kubectl's arguments by the verb and the bare word
// after it, against kubectlReadCommands and kubectlRefusedSubcommands.
func classifyKubectl(args []string) blockClass {
	for i := 0; i < len(args); i++ {
		f := args[i]
		if kubectlValueFlags[f] {
			i++ // the flag's value, not the verb
			continue
		}
		if strings.HasPrefix(f, "-") {
			continue
		}
		rest := args[i+1:]
		for _, w := range rest {
			if kubectlRefusedSubcommands[f+" "+w] {
				return blockWrite
			}
		}
		if (len(rest) > 0 && kubectlReadCommands[f+" "+rest[0]]) || kubectlReadCommands[f] {
			return blockRead
		}
		return blockWrite
	}
	return blockWrite
}

// classifyGcloud judges gcloud's arguments by their leading bare words,
// which have to start with a path in gcloudReadCommands. The words stop at
// the first flag, placeholder or quoted argument: a flag's arity is not
// known here, so a path that only appears after one is a write.
func classifyGcloud(args []string) blockClass {
	var path []string
	for _, f := range args {
		if strings.HasPrefix(f, "-") || strings.HasPrefix(f, placeholderOpen) || strings.HasPrefix(f, quoteChar) {
			break
		}
		path = append(path, f)
		if gcloudReadCommands[strings.Join(path, " ")] {
			return blockRead
		}
	}
	return blockWrite
}

// insertPreamble places sessionPreamble after the first H1, or at the top of
// the body if the skill has no title.
func insertPreamble(lines []string) []string {
	inFence := false
	for i, l := range lines {
		if strings.HasPrefix(strings.TrimSpace(l), codeFence) {
			inFence = !inFence
		}
		if !inFence && strings.HasPrefix(l, titlePrefix) {
			out := append([]string{}, lines[:i+1]...)
			out = append(out, "", sessionPreamble)
			return append(out, lines[i+1:]...)
		}
	}
	return append([]string{sessionPreamble, ""}, lines...)
}

// replaceFileReferences rewrites every reference to a file that is not
// shipped: a Markdown link or path in prose becomes shippedFileProse, and a
// path inside a command block (shell or transcript) becomes shippedFileArg.
// A block with any other info string is left as written: a manifest's
// mountPath: /opt/data/scripts is the manifest, not a reference to a script.
func replaceFileReferences(lines []string) []string {
	out := make([]string, len(lines))
	inFence, rewrite := false, false
	for i, l := range lines {
		if trimmed := strings.TrimSpace(l); strings.HasPrefix(trimmed, codeFence) {
			inFence = !inFence
			rewrite = inFence && commandBlock(strings.TrimSpace(strings.TrimPrefix(trimmed, codeFence)))
			out[i] = l
			continue
		}
		if inFence {
			if rewrite {
				l = filePathRE.ReplaceAllString(l, "${1}"+shippedFileArg)
			}
			out[i] = l
			continue
		}
		l = fileLinkRE.ReplaceAllString(l, shippedFileProse)
		l = quotedFilePathRE.ReplaceAllLiteralString(l, shippedFileProse)
		out[i] = filePathRE.ReplaceAllString(l, "${1}"+shippedFileProse)
	}
	return out
}
