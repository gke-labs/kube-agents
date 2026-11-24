package main

import (
	"fmt"
	"slices"
	"strings"
	"unicode"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/toolgrant"
)

// The Claude Code tool names a session allowlist uses, as
// sessionToolPolicy reads them (defaultAllowedTools,
// clusterViewAllowedTools).
var claudeReadTools = []string{"Read", "Glob", "Grep", "LS"}

const (
	claudeWriteTool     = "Write"
	claudeWebSearchTool = "WebSearch"
	claudeTodoWriteTool = "TodoWrite"
	claudeBashTool      = "Bash"
	// claudePrefixSuffix ends a scoped Bash grant: Bash(<prefix>:*).
	claudePrefixSuffix = ":*)"
)

// coreAgentSessionGrant is a session pod's tool grant as the core-agent
// harness receives it, read from the same lists harnessCommand gives
// Claude Code, for a pod whose working directory is workdir.
func coreAgentSessionGrant(getenv func(string) string, workdir string) (coreagentwire.Policy, error) {
	allowed, disallowed, _, _ := sessionToolLists(getenv)
	p, err := sessionToolPolicy(allowed, disallowed)
	if err != nil {
		return coreagentwire.Policy{}, err
	}
	if p.WebSearch {
		// The harness's model has every Gemini built-in off; search can't
		// be granted until it's wired.
		return coreagentwire.Policy{}, fmt.Errorf("web search has no core-agent grant yet")
	}
	g, err := p.CoreAgent(workdir)
	if err != nil {
		return coreagentwire.Policy{}, err
	}
	return coreagentwire.Policy{Tools: g.Builtins, Allow: g.Allow, Deny: g.Deny}, nil
}

// sessionToolPolicy reads a Claude Code allowlist/denylist pair as a
// harness-neutral grant. A name the denylist carries grants nothing, as in
// Claude Code. TodoWrite has no grant (no worker is given a to-do tool) and
// is dropped, and so is the delegate tool until the delegate MCP server is
// wired. Anything else this doesn't know, an unrestricted Bash and a scoped
// denylist entry are errors: no guess at a grant.
func sessionToolPolicy(allowed, disallowed string) (toolgrant.Policy, error) {
	deny := splitTools(disallowed)
	for _, d := range deny {
		if strings.Contains(d, "(") {
			return toolgrant.Policy{}, fmt.Errorf("scoped disallowed tool %q has no grant form", d)
		}
	}
	var p toolgrant.Policy
	for _, t := range splitTools(allowed) {
		base, arg, scoped := strings.Cut(t, "(")
		if slices.Contains(deny, base) {
			continue
		}
		switch {
		case !scoped && slices.Contains(claudeReadTools, t):
			p.ReadFiles = true
		case t == claudeWriteTool:
			p.WriteFiles = true
		case t == claudeWebSearchTool:
			p.WebSearch = true
		case t == claudeTodoWriteTool, t == delegateToolID:
		case base == claudeBashTool && scoped && strings.HasSuffix(arg, claudePrefixSuffix):
			p.ShellPrefixes = append(p.ShellPrefixes, strings.TrimSuffix(arg, claudePrefixSuffix))
		case t == claudeBashTool:
			return toolgrant.Policy{}, fmt.Errorf("an unrestricted Bash has no grant")
		default:
			return toolgrant.Policy{}, fmt.Errorf("tool %q has no grant", t)
		}
	}
	return p, nil
}

// splitTools is a tool list's entries, separated as the harness takes them
// (allowsBash): by commas or whitespace, except inside a scoped entry's
// parentheses, where a space belongs to the argument (Bash(gh pr list:*)).
func splitTools(list string) []string {
	var out []string
	var cur strings.Builder
	depth := 0
	flush := func() {
		if t := cur.String(); t != "" {
			out = append(out, t)
		}
		cur.Reset()
	}
	for _, r := range list {
		switch {
		case r == '(':
			depth++
		case r == ')' && depth > 0:
			depth--
		case depth == 0 && (r == ',' || unicode.IsSpace(r)):
			flush()
			continue
		}
		cur.WriteRune(r)
	}
	flush()
	return out
}
