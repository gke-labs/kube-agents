// Package antigravity maps a worker's tool grant onto the Antigravity CLI
// (`agy`, #6707): which of agy's tools the model may be offered, and the
// `permissions` rules agy itself applies (#6711).
//
// agy offers its model a fixed set of tools and has no setting that removes
// one, and several of them (search_web, schedule, the subagent tools, swarm,
// send_message, manage_task) run with no permission check at all. So the
// grant is meant to be enforced in two places:
//   - Tools is the set the loopback model proxy (#6714) is to leave in each
//     model request, stripping every other declaration and refusing a model
//     call to a tool it did not offer. That is the boundary, and it lands
//     in #6714's follow-up PR: until then the proxy only forwards, agy's
//     model is offered all its tools, the ungated ones work, and these
//     rules are the only layer. agy runs only with the
//     A2A_HARNESS_UNISOLATED waiver meanwhile;
//   - Allow and Deny are agy's own rules, rendered into --settings. They are
//     defence in depth, never the boundary: the shell's boundary stays the
//     sandbox, the credential broker and the verb allowlist, as for Claude
//     Code.
//
// One rule category covers more than one tool: write_file(<dir>) also
// allows agy's edit_file there, which a session pod's Claude Code denies.
// The proxy strips edit_file; until it does, a grant with WriteFiles lets
// agy edit in the workspace too.
//
// What agy 1.5.13 was seen to do (round_4 probes, 2026-11-24): --settings
// accepts only the `permissions` key, and any other key is a hard error; a
// malformed rule is silently ignored, so every rule here is checked against
// the grammar before it is rendered; an `ask` rule is auto-denied headless,
// so none is ever rendered.
package antigravity

import (
	"encoding/json"
	"fmt"
	"path"
	"regexp"
	"slices"
	"strings"
)

// The tools agy 1.5.13 offers its model: these 14 in every main-agent
// request (in each --mode too), and in a subagent's own requests 12 of them
// plus generate_image. A model request naming any other tool means agy
// changed under us, and the proxy refuses it.
var KnownTools = []string{
	ToolAskQuestion, ToolDefineSubagent, ToolEditFile, ToolGenerateImage, ToolInvokeSubagent,
	ToolManageSubagents, ToolManageTask, ToolReadFile, ToolRunCommand,
	ToolRunWorkflow, ToolSchedule, ToolSearchWeb, ToolSendMessage, ToolSwarm,
	ToolWriteFile,
}

const (
	ToolAskQuestion     = "ask_question"
	ToolDefineSubagent  = "define_subagent"
	ToolEditFile        = "edit_file"
	ToolGenerateImage   = "generate_image"
	ToolInvokeSubagent  = "invoke_subagent"
	ToolManageSubagents = "manage_subagents"
	ToolManageTask      = "manage_task"
	ToolReadFile        = "read_file"
	ToolRunCommand      = "run_command"
	ToolRunWorkflow     = "run_workflow"
	ToolSchedule        = "schedule"
	ToolSearchWeb       = "search_web"
	ToolSendMessage     = "send_message"
	ToolSwarm           = "swarm"
	ToolWriteFile       = "write_file"
)

// Policy is a worker's grant in harness-neutral terms.
type Policy struct {
	// ReadFiles: read files in the workspace.
	ReadFiles bool
	// WriteFiles: create and overwrite files in the workspace. Edits of
	// existing text (agy's edit_file, Claude Code's Edit) are not granted:
	// the session default denies them.
	WriteFiles bool
	// WebSearch: search the web. Fetching a URL is a separate grant no
	// worker has yet, so agy's read-URL tools stay off.
	WebSearch bool
	// ShellPrefixes are the only commands the shell may run, each a binary
	// with optional subcommands ("kubectl", "gcloud"). Empty means no shell.
	ShellPrefixes []string
}

// Grant is a Policy as agy needs it.
type Grant struct {
	// Tools is the set of agy tools the proxy leaves in model requests,
	// sorted.
	Tools []string
	// Allow and Deny are agy permission rules.
	Allow, Deny []string
}

// shellPrefix is a binary with optional subcommands: words of letters,
// digits, dot, dash and underscore. No path, glob, quote or shell syntax.
//
// agy 1.5.13 matches `command(<prefix>)` by whole words, not as a string
// prefix: `command(touch /a)` doesn't allow `touch /ab`. It refuses a
// compound command (`;`, `&&`, a pipe, `$(…)`) even when its first word is
// allowed (probes P2-P4, P10). Even so, the rule only narrows: the shell's
// boundary is the sandbox, the credential broker and the verb allowlist.
var shellPrefix = regexp.MustCompile(`^[A-Za-z0-9._-]+( [A-Za-z0-9._-]+)*$`)

// ruleGrammar is the rule shape agy documents. Anything else agy ignores
// without a word, so it is refused here instead.
var ruleGrammar = regexp.MustCompile(`^(command|read_file|write_file|mcp)\(([^()]+)\)$`)

// Render maps p onto agy for a worker whose workspace is the absolute path
// workspace.
func (p Policy) Render(workspace string) (Grant, error) {
	if !strings.HasPrefix(workspace, "/") || strings.ContainsAny(workspace, "()*") ||
		path.Clean(workspace) != workspace || workspace == "/" {
		return Grant{}, fmt.Errorf("workspace %q is not a clean absolute path below /", workspace)
	}
	var g Grant
	if p.ReadFiles {
		g.Tools = append(g.Tools, ToolReadFile)
	} else {
		// agy reads inside the workspace unasked.
		g.Deny = append(g.Deny, "read_file(*)")
	}
	if p.WriteFiles {
		g.Tools = append(g.Tools, ToolWriteFile)
		g.Allow = append(g.Allow, "write_file("+workspace+")")
	} else {
		g.Deny = append(g.Deny, "write_file(*)")
	}
	if p.WebSearch {
		g.Tools = append(g.Tools, ToolSearchWeb)
	}
	if len(p.ShellPrefixes) > 0 {
		g.Tools = append(g.Tools, ToolRunCommand)
		for _, prefix := range p.ShellPrefixes {
			if !shellPrefix.MatchString(prefix) {
				return Grant{}, fmt.Errorf("shell prefix %q is not a binary and its subcommands", prefix)
			}
			g.Allow = append(g.Allow, "command("+prefix+")")
		}
	} else {
		g.Deny = append(g.Deny, "command(*)")
	}
	for _, r := range slices.Concat(g.Allow, g.Deny) {
		if !ruleGrammar.MatchString(r) {
			return Grant{}, fmt.Errorf("rule %q is not one agy reads", r)
		}
	}
	slices.Sort(g.Tools)
	return g, nil
}

// Settings is g's rules as the value of agy's --settings flag: a JSON object
// with the one key agy accepts there. Never an "ask" list: headless, agy
// denies an ask anyway, and a rule that says ask but means deny misleads.
func (g Grant) Settings() string {
	perms := map[string][]string{}
	if len(g.Allow) > 0 {
		perms["allow"] = g.Allow
	}
	if len(g.Deny) > 0 {
		perms["deny"] = g.Deny
	}
	b, _ := json.Marshal(map[string]any{"permissions": perms})
	return string(b)
}

// ToolPolicy answers, for a tool name in a model request or response,
// whether agy 1.5.13 has it at all and whether this worker's grant offers it.
// The loopback model proxy (#6714) strips what isn't granted and refuses a
// call to anything not offered, or a name it doesn't know.
type ToolPolicy struct {
	granted map[string]bool
}

// NewToolPolicy is g's tools as a ToolPolicy.
func NewToolPolicy(g Grant) ToolPolicy {
	p := ToolPolicy{granted: map[string]bool{}}
	for _, t := range g.Tools {
		p.granted[t] = true
	}
	return p
}

// Known reports whether name is one of agy 1.5.13's tools.
func (ToolPolicy) Known(name string) bool { return slices.Contains(KnownTools, name) }

// Granted reports whether the grant offers name. A name agy doesn't have is
// never granted.
func (p ToolPolicy) Granted(name string) bool {
	return p.granted[name] && slices.Contains(KnownTools, name)
}
