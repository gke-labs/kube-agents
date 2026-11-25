package main

import (
	"context"
	"fmt"

	adktool "google.golang.org/adk/v2/tool"

	"github.com/go-steer/core-agent/v3/pkg/permissions"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// buildMCP starts the turn's MCP servers and returns their toolsets, gated,
// and a func that stops them; the process exits after the turn, so an MCP
// server lives for one turn.
//
// Not built yet (#7158): starting the servers from the Envelope and
// stopping them, grandchildren included. Until then a turn that names one
// fails rather than running without it.
func buildMCP(_ context.Context, servers []coreagentwire.MCPServer, _ *permissions.Gate) ([]adktool.Toolset, func(), error) {
	if len(servers) > 0 {
		return nil, func() {}, fmt.Errorf("MCP servers are not wired yet (%d named)", len(servers))
	}
	return nil, func() {}, nil
}
