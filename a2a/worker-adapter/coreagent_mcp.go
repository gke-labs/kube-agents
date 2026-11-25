package workeradapter

import (
	"maps"
	"slices"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
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
