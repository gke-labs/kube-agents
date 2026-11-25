package workeradapter

import (
	"fmt"
	"maps"
	"slices"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/toolgrant"
)

// mcpDriver is a driver that takes a turn's MCP servers in-process, in its
// stdin envelope, rather than as harness flags.
type mcpDriver interface {
	Driver
	// WithMCP is the driver with servers as the turn's MCP servers. The
	// receiver is not modified.
	WithMCP(servers []coreagentwire.MCPServer) Driver
}

// coreAgentTurnMCP is a core-agent turn's MCP servers: the persona's (its
// render's Output.MCPServers, each the worker's relay to the credential
// broker with its tools listed), then the worker's own delegate server when
// the turn may delegate. delegate is nil when it may not.
func coreAgentTurnMCP(servers []persona.MCPServer, delegate *persona.MCPServer) []coreagentwire.MCPServer {
	out := make([]coreagentwire.MCPServer, 0, len(servers)+1)
	for _, s := range servers {
		out = append(out, wireServer(s))
	}
	if delegate != nil {
		out = append(out, wireServer(*delegate))
	}
	return out
}

func wireServer(s persona.MCPServer) coreagentwire.MCPServer {
	return coreagentwire.MCPServer{
		Name: s.Name, Command: s.Command, Args: slices.Clone(s.Args), Env: maps.Clone(s.Env), Tools: slices.Clone(s.Tools),
	}
}

// turnDriver is d with a turn's MCP servers (the persona's, then delegate
// when non-nil) when d takes them in-process, else d unchanged.
func turnDriver(d Driver, personaMCP []persona.MCPServer, delegate *persona.MCPServer) Driver {
	return withTurnMCP(d, coreAgentTurnMCP(personaMCP, delegate))
}

// withTurnMCP is d with servers as its turn's MCP servers when d takes them
// in-process, else d unchanged.
func withTurnMCP(d Driver, servers []coreagentwire.MCPServer) Driver {
	if m, ok := d.(mcpDriver); ok {
		return m.WithMCP(servers)
	}
	return d
}

// turnDelegateServer is one turn's delegate server: tmpl with the turn's
// socket, and the submission's delegateTargets part as JSON when it has one
// that parses, in its env. Nil when there is no template or the turn may not
// delegate (a task with a via, a wake, a pod with the tool off). tmpl is not
// modified.
func turnDelegateServer(tmpl *persona.MCPServer, delegate bool, socket string, origin *lib.Envelope) *persona.MCPServer {
	if tmpl == nil || !delegate || socket == "" {
		return nil
	}
	s := persona.MCPServer{Name: tmpl.Name, Command: tmpl.Command, Args: slices.Clone(tmpl.Args), Env: maps.Clone(tmpl.Env), Tools: slices.Clone(tmpl.Tools)}
	if s.Env == nil {
		s.Env = map[string]string{}
	}
	s.Env[EnvDelegateSocket] = socket
	delete(s.Env, EnvDelegateTargets)
	if raw, ok := delegateTargetsJSON(origin); ok {
		s.Env[EnvDelegateTargets] = raw
	}
	return &s
}

// allowDriver is a driver whose harness gates every call, MCP calls
// included, by its policy's allow rules (core-agent).
type allowDriver interface {
	Driver
	WithAllow(patterns []string) Driver
}

// mcpGrantWorkspace is the workspace toolgrant renders an MCP-only grant
// under. An MCP rule names no path, so its value never reaches a rule.
const mcpGrantWorkspace = "/workspace"

// withDelegateGrant is d with the gate's allow rules for s's tools (the
// delegate tool: "mcp:a2a_delegate" and "mcp:a2a_delegate *", nothing
// wider), rendered by toolgrant as any MCP grant is. A driver whose harness
// has no such gate, or s nil, gives d unchanged.
func withDelegateGrant(d Driver, s *persona.MCPServer) (Driver, error) {
	a, ok := d.(allowDriver)
	if !ok || s == nil {
		return d, nil
	}
	g, err := toolgrant.Policy{MCP: []toolgrant.MCPGrant{{Server: s.Name, Tools: s.Tools}}}.CoreAgent(mcpGrantWorkspace)
	if err != nil {
		return d, fmt.Errorf("the delegate tool's grant: %w", err)
	}
	return a.WithAllow(g.Allow), nil
}
