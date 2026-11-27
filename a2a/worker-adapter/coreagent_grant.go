package workeradapter

import (
	"fmt"
	"slices"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/toolgrant"
)

// CoreAgentWirePolicy is p as the core-agent harness receives it in each
// turn's envelope, for a worker whose workspace is workdir.
func CoreAgentWirePolicy(p toolgrant.Policy, workdir string) (coreagentwire.Policy, error) {
	g, err := p.CoreAgent(workdir)
	if err != nil {
		return coreagentwire.Policy{}, err
	}
	return coreagentwire.Policy{Tools: g.Builtins, Allow: g.Allow, Deny: g.Deny, WebSearch: g.GeminiGoogleSearch, AnyShellCommand: g.AnyShellCommand}, nil
}

// PersonaToolPolicy is a profile worker's tool grant, read from its
// persona's core-agent rendering (coreagent.Adapter): the manifest's tools
// as Output.Allowed passes them, and Output.MCPServers for the tools each
// server may expose. It grants what the Claude Code rendering of the same
// persona grants, mapped to core-agent, and nothing wider:
//   - read_file: the read tools; inside the workspace unless the persona
//     also grants the shell, whose turn runs in a mode that drops the path
//     scope (Claude Code's Read isn't scoped, and the shell reads anywhere
//     it can reach anyway);
//   - shell: any command, compound ones included, as Claude Code's Bash
//     (toolgrant.Policy.Shell, coreagentwire.Policy.AnyShellCommand);
//   - web_search: Gemini's server-side Google Search, as WebSearch is
//     server-side on Claude Code;
//   - an MCP server or one of its tools: the tools its MCPServers entry
//     lists, as Claude Code's allowlist names them.
//
// No file-write tool is granted, as Claude Code's denylist refuses Write and
// Edit (a granted shell can still write, on both).
// Anything the two don't agree on is an error, never a guess: a tool with no
// mapping, a server that is started but not granted or granted but not
// started, a server tool outside its grant, and the delegate server's
// reserved name, which only the worker adds.
func PersonaToolPolicy(out persona.Output) (toolgrant.Policy, error) {
	servers := map[string]persona.MCPServer{}
	for _, s := range out.MCPServers {
		if s.Name == persona.ReservedServer {
			return toolgrant.Policy{}, fmt.Errorf("MCP server %q is the worker's own delegate server, not a persona's", s.Name)
		}
		if _, dup := servers[s.Name]; dup {
			return toolgrant.Policy{}, fmt.Errorf("MCP server %q rendered twice", s.Name)
		}
		servers[s.Name] = s
	}
	var p toolgrant.Policy
	whole := map[string]bool{}
	scoped := map[string][]string{}
	for _, t := range out.Allowed {
		switch t {
		case persona.ToolReadFile:
			p.ReadFiles = true
		case persona.ToolShell:
			p.Shell = true
		case persona.ToolWebSearch:
			p.WebSearch = true
		default:
			server, tool, isScoped := strings.Cut(t, ".")
			if _, ok := servers[server]; !ok {
				return toolgrant.Policy{}, fmt.Errorf("tool %q has no grant: no built-in maps to it and no rendered MCP server is named %q", t, server)
			}
			if isScoped {
				scoped[server] = append(scoped[server], tool)
			} else {
				whole[server] = true
			}
		}
	}
	for _, s := range out.MCPServers {
		if !whole[s.Name] && len(scoped[s.Name]) == 0 {
			return toolgrant.Policy{}, fmt.Errorf("MCP server %q is rendered but no tool grants it", s.Name)
		}
		if !whole[s.Name] {
			for _, t := range s.Tools {
				if !slices.Contains(scoped[s.Name], t) {
					return toolgrant.Policy{}, fmt.Errorf("MCP server %q lists tool %q, which no grant names", s.Name, t)
				}
			}
		}
		p.MCP = append(p.MCP, toolgrant.MCPGrant{Server: s.Name, Tools: slices.Clone(s.Tools)})
	}
	return p, nil
}

// policyDriver is a driver that carries its turns' tool policy in-process,
// in its stdin envelope.
type policyDriver interface {
	Driver
	// WithPolicy is the driver with p as every turn's policy. The receiver
	// is not modified.
	WithPolicy(p coreagentwire.Policy) Driver
}

// coreAgentProfileDriver is d carrying the profile persona's grant (out,
// the core-agent rendering) for a workspace at workdir, in place of the
// empty grant a profile pod's driver starts with. A driver that doesn't
// take a policy, or a grant that won't map or render, is an error: the
// worker stops before it takes a task rather than run with every tool
// refused.
func coreAgentProfileDriver(d Driver, out persona.Output, workdir string) (Driver, error) {
	pd, ok := d.(policyDriver)
	if !ok {
		return nil, fmt.Errorf("the %q driver takes no tool policy", d.Harness())
	}
	p, err := PersonaToolPolicy(out)
	if err != nil {
		return nil, err
	}
	wire, err := CoreAgentWirePolicy(p, workdir)
	if err != nil {
		return nil, err
	}
	return pd.WithPolicy(wire), nil
}
